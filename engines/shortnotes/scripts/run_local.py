"""Run the bot LOCALLY (long-polling) so you can test on Telegram without deploying.

    python scripts/run_local.py

Keep this running, then message your bot on Telegram and send a lecture PDF.
Press Ctrl+C to stop. (Stop this before deploying the webhook version — a bot
can use either polling OR a webhook, not both at once.)
"""
from __future__ import annotations

import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notesbot import config
from notesbot.handler import process_update

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def main() -> None:
    missing = config.missing_required()
    if missing:
        sys.exit(f"Missing env vars in .env: {missing}")

    # A webhook and getUpdates can't both be active — clear any webhook first.
    requests.post(f"{API}/deleteWebhook", json={"drop_pending_updates": True}, timeout=30)

    me = requests.get(f"{API}/getMe", timeout=30).json()
    uname = me.get("result", {}).get("username", "?")
    print(f"✅ Bot @{uname} is live (local polling). Model: {config.GEMINI_MODEL}")
    print("   Send a lecture PDF to the bot on Telegram. Ctrl+C to stop.\n")

    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API}/getUpdates", params=params, timeout=60)
            data = resp.json()
            if not data.get("ok"):
                print("getUpdates error:", data)
                time.sleep(3)
                continue
            for update in data["result"]:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                who = msg.get("from", {}).get("first_name", "?")
                kind = "PDF" if msg.get("document") else ("text" if msg.get("text") else "?")
                print(f"-> update {update['update_id']} from {who} ({kind})")
                try:
                    process_update(update)
                except Exception as exc:  # noqa: BLE001
                    print(f"   process error: {type(exc).__name__}: {exc}")
        except requests.exceptions.ReadTimeout:
            continue  # normal long-poll timeout
        except KeyboardInterrupt:
            print("\n👋 Stopped.")
            return
        except Exception as exc:  # noqa: BLE001
            print("loop error:", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
