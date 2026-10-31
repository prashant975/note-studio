"""`python -m sheets_bridge --check-setup`: verify the local prerequisites.

Prints a checklist (nothing is modified) so a first click on the button does
not fail half-way. Provider keys are *not* expected anywhere: all model calls
go through the PW shared proxy with the signed-in user's Google token.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

PROVIDER_KEY_HINTS = ("GEMINI", "MATHPIX", "SARVAM", "ELEVEN", "ANTHROPIC", "CLAUDE", "LITELLM", "OPENAI")


def _ok(flag: bool) -> str:
    return "[OK]  " if flag else "[!!]  "


def check_client_secret(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, (
            f"not found at {path}. Download the Desktop-app OAuth client JSON from "
            "https://console.cloud.google.com/auth/clients and save it there."
        )
    try:
        info = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, f"{path} is not valid JSON - re-download it from the console."
    if not isinstance(info, dict):
        return False, f"{path} has an unexpected shape."
    if "installed" in info:
        block = info["installed"]
        if isinstance(block, dict) and block.get("client_id") and block.get("client_secret"):
            cid = str(block["client_id"])
            return True, f"Desktop-app client {cid[:14]}...{cid[-28:]}"
        return False, "the 'installed' block is missing client_id/client_secret."
    if "web" in info:
        return False, (
            "this is a *Web application* client. Create a new client with "
            "Application type = Desktop app and download that JSON instead."
        )
    return False, "unrecognised client JSON (expected an 'installed' block)."


def provider_keys_in_env(env_path: Path) -> list[str]:
    """Non-empty provider key assignments still present in a .env file."""
    if not env_path.is_file():
        return []
    leaked: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.partition("=")
        if value.strip() and any(hint in name.upper() for hint in PROVIDER_KEY_HINTS):
            leaked.append(name.strip())
    return leaked


def _token_clients(settings) -> tuple[int, int]:
    """(tokens from another OAuth client, tokens from this one)."""
    import json

    from .google_auth import client_id_of

    current = client_id_of(settings.client_secret_path)
    stale = live = 0
    root = settings.token_path.parent
    for path in list((root / "tokens").glob("*.json")) + [settings.token_path]:
        try:
            issued = str(json.loads(path.read_text(encoding="utf-8")).get("client_id", ""))
        except (OSError, ValueError):
            continue
        if current and issued and issued != current:
            stale += 1
        else:
            live += 1
    return stale, live


def run_check(settings) -> int:
    from notesbot import config as notes_config

    problems = 0

    ok, msg = check_client_secret(settings.client_secret_path)
    print(_ok(ok) + f"OAuth client JSON: {msg}")
    problems += not ok

    # After swapping to another OAuth client, tokens issued by the old one are
    # dead. Say so plainly instead of letting them fail at refresh time.
    stale, live = _token_clients(settings)
    if stale:
        print(_ok(False) + f"{stale} cached token(s) were issued by a DIFFERENT OAuth client - "
                           "they are ignored; each operator signs in once more")
    elif live:
        print(_ok(True) + f"{live} cached token(s) match this OAuth client")

    token_present = settings.token_path.is_file()
    print(_ok(True) + f"Google token cache: {'present' if token_present else 'not yet (browser sign-in happens on first run)'} -> {settings.token_path}")

    libs = all(importlib.util.find_spec(m) is not None for m in ("googleapiclient", "google_auth_oauthlib", "google.auth"))
    print(_ok(libs) + ("Google client libraries installed" if libs else "Google client libraries missing: python -m pip install -r sheets_bridge/requirements.txt"))
    problems += not libs
    docx = importlib.util.find_spec("docx") is not None
    print(_ok(docx) + ("python-docx installed (review Doc)" if docx else "python-docx missing - every run would crash: python -m pip install -r sheets_bridge/requirements.txt"))
    problems += not docx
    hb = importlib.util.find_spec("uharfbuzz") is not None
    print(_ok(hb) + ("uharfbuzz installed (Hindi shaping)" if hb else "uharfbuzz missing - Hindi PDFs render unshaped: python -m pip install -r sheets_bridge/requirements.txt"))
    problems += not hb

    # --- PW proxy onboarding -------------------------------------------------
    try:
        import pw_access

        registered = False
        try:
            import requests

            response = requests.get(f"{pw_access.PROXY_BASE_URL}/api/apps", timeout=20)
            apps = response.json().get("apps", []) if response.status_code == 200 else []
            registered = pw_access.APP_NAME in apps
        except Exception:  # noqa: BLE001
            print("[!!]  could not reach the proxy /api/apps")
        print(_ok(registered) + f"PW proxy app: {pw_access.APP_NAME!r} " + ("registered" if registered else "NOT in /api/apps - ask the owner to add the exact column to Whitelisted"))
        print(_ok(True) + f"PW proxy URL: {pw_access.PROXY_BASE_URL}")
        problems += not registered
    except Exception as exc:  # noqa: BLE001
        print(f"[!!]  pw_access client not importable: {type(exc).__name__}")
        problems += 1

    repo_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(notes_config.__file__))))
    leaked = provider_keys_in_env(repo_root / ".env")
    print(_ok(not leaked) + ("no provider keys in .env (all model calls go through the PW proxy)" if not leaked else f"provider keys still set in .env: {', '.join(leaked)} - remove them"))
    problems += bool(leaked)

    try:
        from . import profiles as _profiles

        targets = _profiles.resolve_targets(settings)
        if not targets:
            print("[!!]  no sheet configured: add one in the Studio (+ Add sheet) or set SHEETS_BRIDGE_SPREADSHEET_ID / SHEETS_BRIDGE_GID in .env")
            problems += 1
        elif len(targets) == 1 and targets[0].name == "default":
            print(_ok(True) + f"target sheet {targets[0].spreadsheet_id} gid {targets[0].gid} (from .env)")
        else:
            print(_ok(True) + f"{len(targets)} sheet profile(s) in {_profiles.profiles_path().name}:")
            for target in targets:
                flag = "" if target.enabled else "  (disabled)"
                print(f"          {target.name}: {target.spreadsheet_id} gid {target.gid}{flag}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!!]  sheet profiles: {type(exc).__name__} - {exc}")
        problems += 1
    print()
    if problems:
        print(f"{problems} item(s) need attention before the first run.")
        return 1
    print("All set. Run: python -m sheets_bridge --serve   (or --test-model for one live proxy call)")
    return 0
