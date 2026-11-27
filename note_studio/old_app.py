"""The old Short Notes app's folder (~/.shortnotes_bridge): sheet list, Google
sign-in and notes cache.

NoteCraft uses it only when the user agrees (setting `share_old_app` = "yes").
Then the copied sheet automation is pointed at the same files the old exe's
launcher uses (SHEETS_BRIDGE_PROFILES / _TOKEN / _CLIENT_SECRET), so both apps
see one sheet list and one sign-in. With "no", NoteCraft keeps its own files
under ~/.note_studio/sheets/. Undecided: nothing is set.

This module only READS the shared folder. Saving the sheet list (Phase 3) will
keep unknown keys and write a .bak first.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from functools import lru_cache

from . import db, engine_catalog, paths

SHARE_KEY = "share_old_app"            # "yes" | "no" | "" (not decided yet)


def choice() -> str:
    value = db.get_setting(SHARE_KEY)
    return value if value in ("yes", "no") else ""


def adopt_default() -> str:
    """The user chose (4 October 2026, Phase 3) that NoteCraft uses the old app's
    sheets and sign-in. Where that folder exists and nothing was chosen yet, use
    it; Settings can still switch to "Keep separate"."""
    if not choice() and paths.old_app_home().is_dir():
        db.set_setting(SHARE_KEY, "yes")
    return choice()


@lru_cache(maxsize=1)
def engine_defaults() -> dict:
    """The non-secret generation defaults the old exe starts with, read from the
    engine copy's own launcher so there is one source of truth."""
    launcher = engine_catalog.engine_dir("shortnotes") / "packaging" / "studio_launcher.py"
    if not launcher.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("_shortnotes_studio_launcher", launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(getattr(module, "DEFAULTS", {}))


def state_dir(decision: str | None = None):
    """Where the sheet list and Google sign-in live, or None while undecided."""
    decision = choice() if decision is None else decision
    if decision == "yes":
        return paths.old_app_home()
    if decision == "no":
        folder = paths.home() / "sheets"
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return None


def environment(decision: str | None = None) -> dict:
    """The sheet-list and sign-in paths the copied automation reads; {} while
    undecided. (The engine's generation defaults: settings_store.)"""
    folder = state_dir(decision)
    if folder is None:
        return {}
    env = {}
    env["SHEETS_BRIDGE_PROFILES"] = str(folder / "sheets.json")
    env["SHEETS_BRIDGE_TOKEN"] = str(folder / "token.json")
    # The Google sign-in registration (never anyone's sign-in): the original app's
    # file on this computer, else the one an installed NoteCraft carries, as QBG
    # One and the old Short Notes exe carry theirs (packaging/build_installer.py).
    # An explicit per-computer import wins over the legacy installation.
    explicit = os.environ.get("NOTE_STUDIO_GOOGLE_CLIENT", "").strip()
    configured = Path(explicit).expanduser() if explicit else paths.home() / "google-client.json"
    for client in (configured, paths.shortnotes_original() / "client_secret.json", paths.signin_registration()):
        if client.is_file():
            env["SHEETS_BRIDGE_CLIENT_SECRET"] = str(client)
            break
    return env


def apply_environment() -> dict:
    """Set the variables for this process. A value already set outside wins,
    exactly like the old launcher's setdefault."""
    env = environment()
    for key, value in env.items():
        os.environ.setdefault(key, value)
    return env


def status() -> dict:
    """What the shared folder holds, in counts only: never tokens or contents."""
    folder = paths.old_app_home()
    info = {"folder": str(folder), "exists": folder.is_dir(), "choice": choice(),
            "sheets": 0, "accounts": 0, "account": "", "cache": False}
    if not info["exists"]:
        return info
    try:
        data = json.loads((folder / "sheets.json").read_text(encoding="utf-8-sig"))
        info["sheets"] = len(data.get("profiles", [])) if isinstance(data, dict) else 0
    except (OSError, ValueError):
        pass
    tokens = folder / "tokens"
    info["accounts"] = len(list(tokens.glob("*.json"))) if tokens.is_dir() else 0
    info["cache"] = (folder / "pending").is_dir()
    if info["choice"] == "yes":
        try:
            info["account"] = json.loads((folder / "last_account.json").read_text(encoding="utf-8")).get("email", "")
        except (OSError, ValueError, AttributeError):
            pass
    return info
