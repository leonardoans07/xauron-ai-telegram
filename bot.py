# bot.py
import logging
import re
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("bot")


# =========================
# SUA LÓGICA AQUI
# Troque analyze_symbol() pela sua função real.
# =========================
def analyze_symbol(symbol: str) -> str:
    """
    Retorna uma análise em texto.
    Substitua isso pela sua lógica real (sinais/indicadores/etc).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Placeholder bem “pronto pra vender”: limpo e objetivo.
    return (
        f"📊 *Xauron AI* — Análise rápida\n"
        f"• Símbolo: *{symbol}*\n"
        f"• Hora: `{now}`\n\n"
        f"✅ *Sinal:* (placeholder)\n"
        f"• Tendência: _Aguardando sua lógica_\n"
        f"• Entrada: _Aguardando sua lógica_\n"
        f"• SL/TP: _Aguardando sua lógica_\n\n"
        f"_Obs: Este texto é modelo. Substitua pela análise real do seu sistema._"
    )


def _extract_symbol(text: str) -> Optional[str]:
    """
    Extrai um símbolo do texto do usuário.
    Aceita: XAUUSD, EURUSD, BTCUSD, NAS100, etc.
    """
    if not text:
        return None

    t = text.strip().upper()

    # Se vier com / (comando) não é símbolo
    if t.startswith("/"):
        return None

    # Pega o primeiro "token" (primeira palavra)
    token = t.split()[0]

    # Limpa caracteres comuns
    token = token.replace("#", "").replace("$", "")

    # Validação simples de símbolo
    if re.match(r"^[A-Z0-9._-]{3,15}$", token):
        return token

    return None


# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 *Xauron AI Telegram*\n\n"
        "Digite um ativo pra eu te mandar a análise, por exemplo:\n"
        "• `XAUUSD`\n"
        "• `EURUSD`\n"
        "• `BTCUSD`\n\n"
        "Comandos:\n"
        "• /help"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "ℹ️ *Como usar*\n\n"
        "✅ Só mandar o símbolo do ativo:\n"
        "Ex: `XAUUSD`\n\n"
        "Dica: você pode mandar também com texto junto:\n"
        "Ex: `XAUUSD manda sinal`\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    symbol = _extract_symbol(text)

    if not symbol:
        await update.message.reply_text(
            "Manda só o ativo (ex: `XAUUSD`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        analysis = analyze_symbol(symbol)
        await update.message.reply_text(analysis, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.exception("Erro na análise do símbolo %s: %s", symbol, e)
        await update.message.reply_text(
            "Deu um erro ao gerar a análise. Tenta de novo em instantes."
        )


# =========================
# Factory
# =========================
def build_application(token: str) -> Application:
    """
    Cria o Application do python-telegram-bot.
    """
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
