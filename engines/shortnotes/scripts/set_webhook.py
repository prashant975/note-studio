"""Register (or delete) the Telegram webhook for the bot(s).

Run AFTER deploying to Vercel:

    python scripts/set_webhook.py https://your-app.vercel.app           # main bot
    python scripts/set_webhook.py https://your-app.vercel.app --bot2    # ask-name bot
    python scripts/set_webhook.py --info            (add --bot2 for second bot)
    python scripts/set_webhook.py --delete          (add --bot2 for second bot)

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_TOKEN_2 and TELEGRAM_WEBHOOK_SECRET
from .env / environment.
"""
from __future__ import annotations

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notesbot import config  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    use_bot2 = "--bot2" in args
    args = [a for a in args if a != "--bot2"]

    token = config.TELEGRAM_BOT_TOKEN_2 if use_bot2 else config.TELEGRAM_BOT_TOKEN
    which = "bot2 (ask-name)" if use_bot2 else "main bot"
    if not token:
        sys.exit(f"Token for {which} is not set (.env or environment).")
    api = f"https://api.telegram.org/bot{token}"

    def post(method: str, **params):
        resp = requests.post(f"{api}/{method}", json=params, timeout=30)
        print(method, "->", resp.status_code)
        print(resp.json())

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--info":
        post("getWebhookInfo")
        return
    if args[0] == "--delete":
        post("deleteWebhook", drop_pending_updates=True)
        return

    base = args[0].rstrip("/")
    url = f"{base}/api?bot=ask" if use_bot2 else f"{base}/api"
    if not config.TELEGRAM_WEBHOOK_SECRET:
        print("WARNING: TELEGRAM_WEBHOOK_SECRET is empty — webhook will be unprotected.")
    post(
        "setWebhook",
        url=url,
        secret_token=config.TELEGRAM_WEBHOOK_SECRET or None,
        allowed_updates=["message"],
        drop_pending_updates=True,
        max_connections=10,
    )
    print(f"\n[{which}] webhook set to: {url}")
    post("getWebhookInfo")


if __name__ == "__main__":
    main()
