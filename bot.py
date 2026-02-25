# bot.py
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

log = logging.getLogger("bot")

TWELVE_API_KEY = (os.getenv("TWELVE_API_KEY") or "").strip()
DEFAULT_INTERVAL = (os.getenv("DEFAULT_INTERVAL") or "5min").strip()

VI_LENGTH = int(os.getenv("VI_LENGTH") or "14")
ATR_LENGTH = int(os.getenv("ATR_LENGTH") or "14")

ATR_SL_MULT = float(os.getenv("ATR_SL_MULT") or "1.5")
ATR_TP1_MULT = float(os.getenv("ATR_TP1_MULT") or "1.0")
ATR_TP2_MULT = float(os.getenv("ATR_TP2_MULT") or "2.0")
ATR_TP3_MULT = float(os.getenv("ATR_TP3_MULT") or "3.0")


@dataclass
class Candle:
    t: str
    o: float
    h: float
    l: float
    c: float


def _extract_symbol_and_interval(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Aceita:
      "XAUUSD"
      "XAUUSD 5min"
      "XAUUSD M5" -> vira 5min
      "XAUUSD 1h"
    """
    if not text:
        return None, None
    parts = text.strip().upper().split()
    if not parts or parts[0].startswith("/"):
        return None, None

    sym = parts[0].replace("#", "").replace("$", "")
    if not re.match(r"^[A-Z0-9._-]{3,15}$", sym):
        return None, None

    interval = None
    if len(parts) >= 2:
        raw = parts[1]
        m_map = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
                 "H1": "1h", "H4": "4h", "D1": "1day"}
        interval = m_map.get(raw, raw.lower())

    return sym, interval


async def fetch_candles_twelve(symbol: str, interval: str, outputsize: int = 200) -> List[Candle]:
    if not TWELVE_API_KEY:
        raise RuntimeError("TWELVE_API_KEY não configurada no Railway.")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }

    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if "status" in data and data["status"] == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message', 'unknown error')}")

    values = data.get("values")
    if not values:
        raise RuntimeError("TwelveData não retornou candles (values vazio).")

    # Twelve retorna do mais recente -> mais antigo. Vamos inverter.
    candles: List[Candle] = []
    for row in reversed(values):
        candles.append(Candle(
            t=row["datetime"],
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
        ))
    return candles


def _true_range(curr: Candle, prev_close: float) -> float:
    return max(curr.h - curr.l, abs(curr.h - prev_close), abs(curr.l - prev_close))


def atr(candles: List[Candle], length: int) -> float:
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para ATR.")
    trs: List[float] = []
    for i in range(1, len(candles)):
        trs.append(_true_range(candles[i], candles[i - 1].c))
    # média simples dos últimos length TRs
    window = trs[-length:]
    return sum(window) / len(window)


def vortex(candles: List[Candle], length: int) -> Tuple[float, float]:
    """
    Vortex Indicator clássico:
      VM+ = |High(i) - Low(i-1)|
      VM- = |Low(i) - High(i-1)|
      TR = True Range
      VI+ = sum(VM+)/sum(TR) ; VI- = sum(VM-)/sum(TR)
    """
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para Vortex.")
    vm_plus = []
    vm_minus = []
    tr = []

    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        vm_plus.append(abs(c.h - p.l))
        vm_minus.append(abs(c.l - p.h))
        tr.append(_true_range(c, p.c))

    vm_plus_w = vm_plus[-length:]
    vm_minus_w = vm_minus[-length:]
    tr_w = tr[-length:]

    sum_tr = sum(tr_w) if sum(tr_w) != 0 else 1e-9
    vi_plus = sum(vm_plus_w) / sum_tr
    vi_minus = sum(vm_minus_w) / sum_tr
    return vi_plus, vi_minus


def build_trade_plan(last_price: float, direction: str, atr_val: float) -> Dict[str, float]:
    """
    direction: "BUY" ou "SELL"
    """
    entry = last_price
    if direction == "BUY":
        sl = entry - atr_val * ATR_SL_MULT
        tp1 = entry + atr_val * ATR_TP1_MULT
        tp2 = entry + atr_val * ATR_TP2_MULT
        tp3 = entry + atr_val * ATR_TP3_MULT
    else:
        sl = entry + atr_val * ATR_SL_MULT
        tp1 = entry - atr_val * ATR_TP1_MULT
        tp2 = entry - atr_val * ATR_TP2_MULT
        tp3 = entry - atr_val * ATR_TP3_MULT

    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}


def fmt_price(x: float) -> str:
    # XAUUSD geralmente 2 casas, FX 5, índices 2… aqui fica genérico mas bonito:
    return f"{x:.2f}" if x >= 100 else f"{x:.5f}"


def format_message(symbol: str, interval: str, vi_p: float, vi_m: float, atr_val: float, plan: Dict[str, float]) -> str:
    direction = "BUY" if vi_p > vi_m else "SELL"
    strength = abs(vi_p - vi_m)

    # “probabilidade” simples baseada na separação das linhas (heurística)
    if strength >= 0.25:
        prob = "Alta"
    elif strength >= 0.12:
        prob = "Média"
    else:
        prob = "Baixa"

    confs = []
    confs.append(f"VI+ `{vi_p:.3f}` vs VI- `{vi_m:.3f}` → *{direction}*")
    confs.append(f"Separação (força) `{strength:.3f}`")
    confs.append(f"ATR({ATR_LENGTH}) `{atr_val:.3f}` (define alvos)")

    conf_txt = "\n".join([f"• {c}" for c in confs])

    msg = (
        f"📌 *Xauron Vortex — Tempo real (TwelveData)*\n"
        f"• Símbolo: *{symbol}*\n"
        f"• Timeframe: *{interval}*\n\n"
        f"✅ *Sinal:* *{direction}*\n"
        f"🎯 *Entrada:* `{fmt_price(plan['entry'])}`\n"
        f"🛡 *Stop:* `{fmt_price(plan['sl'])}`\n\n"
        f"🏁 *Alvos*\n"
        f"• TP1: `{fmt_price(plan['tp1'])}`\n"
        f"• TP2: `{fmt_price(plan['tp2'])}`\n"
        f"• TP3: `{fmt_price(plan['tp3'])}`\n\n"
        f"🔎 *Confirmações*\n{conf_txt}\n\n"
        f"📈 *Qualidade do setup:* *{prob}*\n"
        f"_Obs: Alvos/stop calculados por ATR. Se quiser, eu adapto para o padrão exato do Vortex de vocês._"
    )
    return msg


# ===== Handlers =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Xauron Vortex*\n\nDigite um ativo pra eu calcular o sinal em tempo real.\n"
        "Ex:\n• `XAUUSD`\n• `XAUUSD 5min`\n• `EURUSD 1h`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Use assim:\n"
        "• `XAUUSD`\n"
        "• `XAUUSD 5min`\n"
        "• `BTCUSD 15min`\n\n"
        "Retorno: Sinal (Vortex), Entrada, Stop, TP1/TP2/TP3.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    symbol, interval = _extract_symbol_and_interval(text)
    if not symbol:
        await update.message.reply_text("Manda só o ativo (ex: `XAUUSD`).", parse_mode=ParseMode.MARKDOWN)
        return

    interval = interval or DEFAULT_INTERVAL

    try:
        await update.message.reply_text("⏳ Pegando candles + calculando Vortex…", parse_mode=ParseMode.MARKDOWN)

        candles = await fetch_candles_twelve(symbol, interval, outputsize=220)

        vi_p, vi_m = vortex(candles, VI_LENGTH)
        atr_val = atr(candles, ATR_LENGTH)

        last_price = candles[-1].c
        direction = "BUY" if vi_p > vi_m else "SELL"
        plan = build_trade_plan(last_price, direction, atr_val)

        msg = format_message(symbol, interval, vi_p, vi_m, atr_val, plan)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        log.exception("Erro: %s", e)
        await update.message.reply_text(
            f"Deu erro ao analisar `{symbol}` no `{interval}`.\n"
            f"Motivo: `{str(e)}`",
            parse_mode=ParseMode.MARKDOWN,
        )


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
