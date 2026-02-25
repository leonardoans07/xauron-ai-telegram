import os
import requests
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ================== ENV ==================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "PT").upper().strip()  # PT or EN
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()  # needed for XAU/Forex

# ================== Helpers ==================
def t(lang: str, pt: str, en: str) -> str:
    return pt if lang == "PT" else en

def fmt(x, decimals=2):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{decimals}f}"
    except Exception:
        return str(x)

def is_crypto_symbol(sym: str) -> bool:
    s = sym.upper().strip()
    return s.endswith("USDT")

def is_forex_like(sym: str) -> bool:
    s = sym.upper().strip()
    return len(s) == 6 and s.isalpha()  # EURUSD, GBPUSD etc.

def normalize_symbol_for_twelvedata(sym: str) -> str:
    """
    User sends: XAUUSD, EURUSD
    TwelveData expects: XAU/USD, EUR/USD (most commonly)
    """
    s = sym.upper().strip()
    if s == "XAUUSD":
        return "XAU/USD"
    if is_forex_like(s):
        return s[:3] + "/" + s[3:]
    return s

def map_interval_cc(tf: str) -> str:
    tf = tf.lower().strip()
    # CryptoCompare supports histominute/histohour in our earlier logic.
    # Keep same accepted tfs:
    ok = {"1m","3m","5m","15m","30m","1h","2h","4h"}
    if tf not in ok:
        return "5m"
    return tf

def map_interval_twelvedata(tf: str) -> str:
    tf = tf.lower().strip()
    # TwelveData intervals:
    mapping = {
        "1m": "1min",
        "3m": "3min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "2h": "2h",
        "4h": "4h",
    }
    return mapping.get(tf, "5min")

# ================== Data Providers ==================
def fetch_cryptocompare_ohlc(symbol: str, interval: str = "5m", limit: int = 300):
    """
    Free crypto OHLC via CryptoCompare.
    symbol: BTCUSDT, ETHUSDT...
    interval: 1m,3m,5m,15m,30m,1h,2h,4h
    """
    sym = symbol.upper().strip()
    if not sym.endswith("USDT"):
        raise ValueError("Use symbols ending with USDT (e.g., BTCUSDT, ETHUSDT).")

    base = sym.replace("USDT", "")
    quote = "USDT"
    interval = map_interval_cc(interval)

    if interval.endswith("m"):
        agg = int(interval.replace("m", ""))
        url = "https://min-api.cryptocompare.com/data/v2/histominute"
        params = {"fsym": base, "tsym": quote, "limit": int(limit), "aggregate": int(agg)}
    else:
        agg = int(interval.replace("h", ""))
        url = "https://min-api.cryptocompare.com/data/v2/histohour"
        params = {"fsym": base, "tsym": quote, "limit": int(limit), "aggregate": int(agg)}

    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    j = r.json()

    if j.get("Response") != "Success":
        raise RuntimeError(str(j.get("Message", "CryptoCompare error")))

    data = j["Data"]["Data"]
    o, h, l, c = [], [], [], []
    for k in data:
        o.append(float(k["open"]))
        h.append(float(k["high"]))
        l.append(float(k["low"]))
        c.append(float(k["close"]))
    return o, h, l, c


def fetch_twelvedata_ohlc(symbol: str, interval: str = "5m", limit: int = 300):
    """
    TwelveData for XAUUSD + Forex.
    Needs TWELVEDATA_API_KEY env var.
    """
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("Missing TWELVEDATA_API_KEY env var (needed for XAUUSD/Forex).")

    td_symbol = normalize_symbol_for_twelvedata(symbol)
    td_interval = map_interval_twelvedata(interval)

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol,
        "interval": td_interval,
        "outputsize": int(limit),
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON",
    }

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()

    if "status" in j and j["status"] == "error":
        raise RuntimeError(j.get("message", "TwelveData error"))

    values = j.get("values", [])
    if not values:
        raise RuntimeError("No data returned from TwelveData.")

    # TwelveData returns newest first -> reverse to oldest->newest
    values = list(reversed(values))

    o, h, l, c = [], [], [], []
    for k in values:
        o.append(float(k["open"]))
        h.append(float(k["high"]))
        l.append(float(k["low"]))
        c.append(float(k["close"]))
    return o, h, l, c


# ================== Indicators ==================
def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    out = []
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def rsi(values, period=7):
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    if len(gains) < period + 1:
        return [50.0] * len(values)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsis = [50.0] * (period + 1)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 1e-12 else 999.0
        rsis.append(100 - (100 / (1 + rs)))

    while len(rsis) < len(values):
        rsis.insert(0, 50.0)
    return rsis[-len(values):]

def atr(high, low, close, period=14):
    trs = []
    for i in range(len(close)):
        if i == 0:
            tr = high[i] - low[i]
        else:
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        trs.append(tr)

    if len(trs) < period + 1:
        return [0.0] * len(close)

    s = sum(trs[:period]) / period
    out = [s] * period
    for i in range(period, len(trs)):
        s = (s * (period - 1) + trs[i]) / period
        out.append(s)

    while len(out) < len(close):
        out.insert(0, out[0])
    return out[-len(close):]

def linreg_slope(values, lookback=30):
    if len(values) < lookback:
        return 0.0
    y = values[-lookback:]
    x = list(range(lookback))
    x_mean = sum(x) / lookback
    y_mean = sum(y) / lookback
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(lookback))
    den = sum((x[i] - x_mean) ** 2 for i in range(lookback))
    return num / den if den > 1e-12 else 0.0


# ================== SCALPING v2 Decision Engine ==================
def analyze_symbol(symbol: str, interval: str = "5m"):
    sym = symbol.upper().strip()

    # choose provider
    if is_crypto_symbol(sym):
        o, h, l, c = fetch_cryptocompare_ohlc(sym, interval=interval, limit=500)
        decimals = 2 if sym.startswith("BTC") else 4
    else:
        # XAUUSD + Forex via TwelveData
        o, h, l, c = fetch_twelvedata_ohlc(sym, interval=interval, limit=500)
        decimals = 2 if sym == "XAUUSD" else 5

    last = c[-1]

    # scalping indicators
    ema9 = ema(c, 9)[-1]
    ema21 = ema(c, 21)[-1]
    rsi7 = rsi(c, 7)[-1]
    atr14 = atr(h, l, c, 14)[-1]
    slope = linreg_slope(c, 30)

    # micro structure (breakout/pullback)
    lookback = 20
    recent_high = max(h[-lookback:]) if len(h) >= lookback else max(h)
    recent_low = min(l[-lookback:]) if len(l) >= lookback else min(l)

    # conditions
    trend_up = ema9 > ema21 and last > ema21 and slope > 0
    trend_dn = ema9 < ema21 and last < ema21 and slope < 0

    # momentum trigger (scalping)
    mom_buy = rsi7 >= 55
    mom_sell = rsi7 <= 45

    # breakout confirmation
    brk_buy = last >= recent_high * 0.9995
    brk_sell = last <= recent_low * 1.0005

    # volatility filter
    vol_ok = atr14 > 0

    # confidence score (0..100) focused on scalping
    conf_buy = 0
    conf_sell = 0

    conf_buy += 35 if trend_up else 0
    conf_sell += 35 if trend_dn else 0

    conf_buy += 20 if mom_buy else 0
    conf_sell += 20 if mom_sell else 0

    conf_buy += 20 if brk_buy else 0
    conf_sell += 20 if brk_sell else 0

    conf_buy += 15 if vol_ok else 0
    conf_sell += 15 if vol_ok else 0

    # avoid chop: distance from ema21 relative to ATR
    dist = abs(last - ema21)
    if atr14 > 1e-12:
        conf_buy += 10 if dist > 0.20 * atr14 else 4
        conf_sell += 10 if dist > 0.20 * atr14 else 4
    else:
        conf_buy += 4
        conf_sell += 4

    conf_buy = min(100, conf_buy)
    conf_sell = min(100, conf_sell)

    # decision thresholds for scalping
    side = "WAIT"
    conf = max(conf_buy, conf_sell)
    if conf_buy >= 70 and conf_buy > conf_sell:
        side = "BUY"
        conf = conf_buy
    elif conf_sell >= 70 and conf_sell > conf_buy:
        side = "SELL"
        conf = conf_sell

    # trade plan (tighter for scalping)
    entry = last if side != "WAIT" else None
    stop = None
    tp1 = tp2 = tp3 = None
    protect = None

    if side != "WAIT" and atr14 > 1e-12:
        stop_mult = 1.05  # tighter stop for scalping
        if side == "BUY":
            stop = entry - stop_mult * atr14
            risk = entry - stop
            tp1 = entry + 1.0 * risk
            tp2 = entry + 1.5 * risk
            tp3 = entry + 2.0 * risk
            protect = entry + 0.7 * risk
        else:
            stop = entry + stop_mult * atr14
            risk = stop - entry
            tp1 = entry - 1.0 * risk
            tp2 = entry - 1.5 * risk
            tp3 = entry - 2.0 * risk
            protect = entry - 0.7 * risk

    reasons = [
        ("ema9", f"{ema9:.{decimals}f}"),
        ("ema21", f"{ema21:.{decimals}f}"),
        ("rsi7", f"{rsi7:.1f}"),
        ("atr14", f"{atr14:.{decimals}f}"),
        ("slope30", f"{slope:.6f}"),
        ("breakout", "yes" if (brk_buy or brk_sell) else "no"),
    ]

    return {
        "ok": True,
        "symbol": sym,
        "interval": interval,
        "side": side,
        "confidence": int(conf),
        "entry": entry,
        "stop": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "protect": protect,
        "reasons": reasons,
        "decimals": decimals,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

def format_reply(res, lang="PT"):
    if not res.get("ok"):
        return "❌ " + str(res.get("error", "Unknown error"))

    sym = res["symbol"]
    tf = res["interval"]
    side = res["side"]
    conf = res["confidence"]
    decimals = res.get("decimals", 2)

    if side == "WAIT":
        return (
            f"🧠 *IA — {sym}* ({tf})\n\n"
            f"📌 {t(lang,'Sinal','Signal')}: *WAIT*\n"
            f"🎯 {t(lang,'Confiança','Confidence')}: *{conf}%*\n\n"
            f"{t(lang,'Motivos','Reasons')}:\n"
            + "\n".join([f"• {k}: {v}" for k, v in res["reasons"]])
            + f"\n\n⏱ {res['ts']}"
        )

    def f(x): return fmt(x, decimals)

    return (
        f"🧠 *IA — {sym}* ({tf})\n\n"
        f"📌 {t(lang,'Sinal','Signal')}: *{side}*\n"
        f"🎯 {t(lang,'Confiança','Confidence')}: *{conf}%*\n\n"
        f"📍 Entry: `{f(res['entry'])}`\n"
        f"🛑 Stop: `{f(res['stop'])}`\n"
        f"✅ TP1: `{f(res['tp1'])}`\n"
        f"✅ TP2: `{f(res['tp2'])}`\n"
        f"✅ TP3: `{f(res['tp3'])}`\n\n"
        f"🛡 {t(lang,'Proteger (BE) em','Protect (BE) at')}: `{f(res['protect'])}`\n\n"
        f"{t(lang,'Motivos','Reasons')}:\n"
        + "\n".join([f"• {k}: {v}" for k, v in res["reasons"]])
        + f"\n\n⏱ {res['ts']}"
    )


# ================== Telegram ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Xauron AI (SCALPING v2) online.\n\n"
        "Comandos:\n"
        "/tf 1m  (ou 3m, 5m, 15m, 1h)\n"
        "/lang PT ou /lang EN\n\n"
        "Teste:\n"
        "BTCUSDT\n"
        "XAUUSD (precisa TWELVEDATA_API_KEY)\n"
        "EURUSD (precisa TWELVEDATA_API_KEY)"
    )
    await update.message.reply_text(msg)

async def set_tf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /tf 1m  (ou 3m,5m,15m,1h,2h,4h)")
        return
    tf = context.args[0].strip().lower()
    context.user_data["tf"] = tf
    await update.message.reply_text(f"✅ Timeframe definido: {tf}")

async def set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /lang PT  ou  /lang EN")
        return
    lg = context.args[0].strip().upper()
    if lg not in ("PT", "EN"):
        await update.message.reply_text("Use: PT ou EN")
        return
    context.user_data["lang"] = lg
    await update.message.reply_text(f"✅ Idioma definido: {lg}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    if not text:
        return

    tf = context.user_data.get("tf", "5m")
    lg = context.user_data.get("lang", DEFAULT_LANG)

    try:
        res = analyze_symbol(text, interval=tf)
        await update.message.reply_text(format_reply(res, lang=lg), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")

def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tf", set_tf))
    app.add_handler(CommandHandler("lang", set_lang))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()
