# main.py
import logging
import os
import re
from bot import build_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")


def _read_token() -> str:
    return (os.getenv("TOKEN") or os.getenv("TELEGRAM") or "").strip()


def _validate_token(token: str) -> None:
    if not token:
        raise RuntimeError("TOKEN vazio. Crie a variável TOKEN no Railway e redeploy.")
    if token.lower() == "token":
        raise RuntimeError("TOKEN está como 'token' (placeholder). Cole o token real do @BotFather.")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise RuntimeError(f"TOKEN inválido (formato inesperado). Caracteres lidos: {len(token)}")


def main() -> None:
    token = _read_token()
    _validate_token(token)

    app = build_application(token)

    log.info("Bot iniciando (polling). Token lido com %s caracteres.", len(token))
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
