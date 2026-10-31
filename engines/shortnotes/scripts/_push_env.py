"""Push env vars from .env to Vercel (Production). Values are sent via stdin,
not the command line, so they are not echoed."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notesbot import config  # noqa: F401  (loads .env into os.environ)

KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN_2",
    "TELEGRAM_WEBHOOK_SECRET",
    # Provider credentials are intentionally absent: model access goes through
    # the PW shared proxy (pw_access.py), so no provider key is pushed anywhere.
    "TEACHER_SIGNATURE",
    "NOTES_LANGUAGE",
    "DEFAULT_SUBJECT",
]

for key in KEYS:
    value = os.environ.get(key, "")
    if not value:
        print(f"  {key}: SKIP (empty)")
        continue
    # Remove any existing value first (ignore errors), then add fresh.
    subprocess.run(
        f"vercel env rm {key} production -y",
        input="\n", text=True, capture_output=True, shell=True,
    )
    proc = subprocess.run(
        f"vercel env add {key} production",
        input=value + "\n", text=True, capture_output=True, shell=True,
    )
    if proc.returncode == 0:
        print(f"  {key}: added OK")
    else:
        print(f"  {key}: FAILED (Vercel CLI output redacted)")
