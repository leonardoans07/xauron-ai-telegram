import os
import math
import requests
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "PT").upper().strip()  # PT or EN


# -------------------- Helpers --------------------
def is_crypto_symbol(sym: str) -> bool:
    s = sym.upper()
    return s.endswith("USDT")


def fmt(x, decimals=4):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{decimals}f}"
    except Exception:
        return str(x)


def t(lang: str, pt: str, en: str) -> str:
    return pt if lang == "PT" else en


# -------------------- Market data (CryptoCompare) --------------------
def fetch_cryptocompare_ohlc(symbol: str, interval: str = "5m", limit: int = 300):
    """
    symbol: BTCUSDT, ETHUSDT ...
    interval: 1m,3m,5m,15m,30m,1h,2h,4h
    """
    sym = symbol.upper().strip()
    if not sym.endswith("USDT"):
        raise ValueError("Use symbols ending with USDT (e.g., BTCUSDT, ETHUSDT).")

    base = sym.replace("USDT", "")
    quote = "USDT"

    interval = interval.lower().strip()

    if interval in ("1m", "3m", "5m", "15m", "30m"):
        agg = int(interval.replace("m", ""))
        url = "https://min-api.cryptocompare.com/data/v2/histominute"
        params = {"fsym": base, "tsym": quote, "limit": int(limit), "aggregate": int(agg)}
    elif interval in ("1h", "2h", "4h"):
        agg = int(interval.replace("h", ""))
        url = "https://min-api.cryptocompare.com/data/v2/histohour"
        params = {"fsym": base, "tsym": quote, "limit": int(limit), "aggregate": int(agg)}
    else:
        raise ValueError("Timeframe inválido. Use 1m,3m,5m,15m,30m,1h,2h,4h")

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


# -------------------- Indicators --------------------
def ema(values, period):
    k = 2 / (period + 1)
    out = []
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(values, period=14):
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

    # pad to match
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


def linreg_slope(values, lookback=80):
    if len(values) < lookback:
        return 0.0
    y = values[-lookback:]
    x = list(range(lookback))
    x_mean = sum(x) / lookback
    y_mean = sum(y) / lookback
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(lookback))
    den = sum((x[i] - x_mean) ** 2 for i in range(lookback))
    return num / den if den > 1e-12 else 0.0


# -------------------- "AI" Decision Engine (scoring) --------------------
def analyze_symbol(symbol: str, interval: str = "5m"):
    sym = symbol.upper().strip()

    if not is_crypto_symbol(sym):
        return {
            "ok": False,
            "error": (
                "Por enquanto eu rodo 100% grátis em CRYPTO (ex: BTCUSDT, ETHUSDT). "
                "Mande um símbolo terminando com USDT."
            ),
        }

    o, h, l, c = fetch_cryptocompare_ohlc(sym, interval=interval, limit=400)
    last = c[-1]

    ema50 = ema(c, 50)[-1]
    rsi14 = rsi(c, 14)[-1]
    atr14 = atr(h, l, c, 14)[-1]
    slope = linreg_slope(c, 80)

    trend_up = slope > 0 and last > ema50
    trend_dn = slope < 0 and last < ema50

    # Confidence (0..100)
    conf_buy = 0
    conf_sell = 0

    # trend
    conf_buy += 35 if trend_up else 0
    conf_sell += 35 if trend_dn else 0

    # momentum
    if rsi14 >= 55:
        conf_buy += 25
    elif rsi14 <= 45:
        conf_sell += 25
    else:
        conf_buy += 8
        conf_sell += 8

    # volatility sanity
    conf_buy += 15 if atr14 > 0 else 0
    conf_sell += 15 if atr14 > 0 else 0

    # cleanliness: distance from EMA
    dist = abs(last - ema50)
    if atr14 > 1e-12 and dist > 0.25 * atr14:
        conf_buy += 15
        conf_sell += 15
    else:
        conf_buy += 8
        conf_sell += 8

    conf_buy = min(100, conf_buy)
    conf_sell = min(100, conf_sell)

    side = "WAIT"
    conf = max(conf_buy, conf_sell)
    if conf_buy >= 70 and conf_buy > conf_sell:
        side = "BUY"
        conf = conf_buy
    elif conf_sell >= 70 and conf_sell > conf_buy:
        side = "SELL"
        conf = conf_sell

    entry = last if side != "WAIT" else None
    stop = None
    tp1 = tp2 = tp3 = None
    protect = None

    if side != "WAIT" and atr14 > 1e-12:
        k = 1.6
        if side == "BUY":
            stop = entry - k * atr14
            risk = entry - stop
            tp1 = entry + 1.5 * risk
            tp2 = entry + 2.0 * risk
            tp3 = entry + 3.0 * risk
            protect = entry + 1.0 * risk
        else:
            stop = entry + k * atr14
            risk = stop - entry
            tp1 = entry - 1.5 * risk
            tp2 = entry - 2.0 * risk
            tp3 = entry - 3.0 * risk
            protect = entry - 1.0 * risk

    reasons = [
        ("trend", "up" if trend_up else "down" if trend_dn else "side"),
        ("rsi", f"{rsi14:.1f}"),
        ("ema50", f"{ema50:.2f}"),
        ("atr14", f"{atr14:.2f}"),
        ("slope", f"{slope:.6f}"),
    ]

    return {
        "ok": True,
        "symbol": sym,
        "interval": interval,
        "side": side,
        "confidence": int(conf),
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "protect": protect,
        "reasons": reasons,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def format_reply(result, lang="PT"):
    if not result["ok"]:
        return "❌ " + result["error"]

    sym = result["symbol"]
    tf = result["interval"]
    side = result["side"]
    conf = result["confidence"]

    if side == "WAIT":
        return (
            f"🧠 *IA — {sym}* ({tf})\n\n"
            f"📌 {t(lang,'Sinal','Signal')}: *WAIT*\n"
            f"🎯 {t(lang,'Confiança','Confidence')}: *{conf}%*\n\n"
            f"{t(lang,'Motivos','Reasons')}:\n"
            + "\n".join([f"• {k}: {v}" for k, v in result["reasons"]])
            + f"\n\n⏱ {result['ts']}"
        )

    entry = result["entry"]
    stop = result["stop"]
    tp1 = result["tp1"]
    tp2 = result["tp2"]
    tp3 = result["tp3"]
    protect = result["protect"]

    return (
        f"🧠 *IA — {sym}* ({tf})\n\n"
        f"📌 {t(lang,'Sinal','Signal')}: *{side}*\n"
        f"🎯 {t(lang,'Confiança','Confidence')}: *{conf}%*\n\n"
        f"📍 Entry: `{fmt(entry)}`\n"
        f"🛑 Stop: `{fmt(stop)}`\n"
        f"✅ TP1: `{fmt(tp1)}`\n"
        f"✅ TP2: `{fmt(tp2)}`\n"
        f"✅ TP3: `{fmt(tp3)}`\n\n"
        f"🛡 {t(lang,'Proteger (BE) em','Protect (BE) at')}: `{fmt(protect)}`\n\n"
        f"{t(lang,'Motivos','Reasons')}:\n"
        + "\n".join([f"• {k}: {v}" for k, v in result["reasons"]])
        + f"\n\n⏱ {result['ts']}"
    )


# -------------------- Telegram handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Xauron AI online.\n\n"
        "Envie um símbolo (ex: BTCUSDT) ou use:\n"
        "/tf 5m  (ou 15m, 1h)\n"
        "/lang PT ou /lang EN\n\n"
        "Exemplo: BTCUSDT"
    )
    await update.message.reply_text(msg)


async def set_tf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /tf 5m  (ou 15m, 1h, 4h)")
        return
    tf = context.args[0].strip()
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
        reply = format_reply(res, lang=lg)
        await update.message.reply_text(reply, parse_mode="Markdown")
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
