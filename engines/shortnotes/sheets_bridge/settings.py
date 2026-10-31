"""Environment-derived defaults for the bridge.

Importing ``notesbot.config`` first loads the repo ``.env`` (LITELLM_API_KEY
etc.) exactly the way the generator expects; the bridge then reads its own
``SHEETS_BRIDGE_*`` variables from the same environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from notesbot import config as _notes_config  # noqa: F401  (loads .env)

from .errors import BridgeError

# Deliberately no built-in target: a fresh clone that has not been configured
# must never claim rows on somebody's real sheet.
DEFAULT_SPREADSHEET_ID = ""
DEFAULT_GID = 0

# Scopes must be exactly these two. ``drive.file`` is NOT enough: it only sees
# files the app itself created, so every download of an existing lecture PDF
# would 404.
SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    # Identity scopes: the PW proxy verifies the signed-in user's Google token
    # and requires an @pw.live email before any provider call.
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lstrip("﻿").strip()


def _optional_float(text: str) -> float | None:
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"SHEETS_BRIDGE_MAX_COST_INR must be a number, got {text!r}") from exc
    return value if value > 0 else None


@dataclass(frozen=True)
class Settings:
    spreadsheet_id: str
    gid: int
    signature: str            # "" -> use column E (Lecture Faculty)
    max_cost_inr: float | None
    client_secret_path: Path
    token_path: Path
    max_input_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        gid_text = _env("SHEETS_BRIDGE_GID", str(DEFAULT_GID)) or str(DEFAULT_GID)
        try:
            gid = int(gid_text)
        except ValueError as exc:
            raise ValueError(f"SHEETS_BRIDGE_GID must be an integer, got {gid_text!r}") from exc
        default_token = Path.home() / ".shortnotes_bridge" / "token.json"
        return cls(
            spreadsheet_id=_env("SHEETS_BRIDGE_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID)
            or DEFAULT_SPREADSHEET_ID,
            gid=gid,
            signature=_env("SHEETS_BRIDGE_SIGNATURE", ""),
            max_cost_inr=_optional_float(_env("SHEETS_BRIDGE_MAX_COST_INR", "")),
            client_secret_path=Path(
                _env("SHEETS_BRIDGE_CLIENT_SECRET", "client_secret.json") or "client_secret.json"
            ).expanduser(),
            token_path=Path(_env("SHEETS_BRIDGE_TOKEN", str(default_token)) or str(default_token)).expanduser(),
            max_input_bytes=int(getattr(_notes_config, "MAX_PDF_BYTES", 100 * 1024 * 1024)),
        )


# NOTE: provider credentials were removed from this app during PW proxy
# onboarding. All model calls go through pw_access (see pw_transport.py) using
# the signed-in user's Google token; the app holds no provider API keys.
