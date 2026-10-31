"""The signed-in person's own Google Drive read access.

Why this exists
---------------
Class recordings are usually shared "inside PW" or with named people, not with
"anyone with the link", and an anonymous request for those gets Google's
sign-in page. The person who pasted the link can open it because they are
signed in; so the app asks for the same right at its own sign-in — read-only
Drive (`drive.readonly`) — and opens the video as that person.

The sign-in hands Streamlit an access token that dies after about an hour, and
Streamlit keeps no refresh token (its auth cookie holds only the id and access
tokens). A sign-in lasts seven days, so on its own that hour would make Drive
work only straight after signing in. The sign-in therefore asks Google for
offline access too (`access_type=offline` in .streamlit/secrets.toml), and
:func:`install_signin_capture` keeps the refresh token Google sends back at the
moment Streamlit's sign-in callback receives it. Each run then gets a fresh
access token from it (:func:`access_token`).

What is stored, and how
-----------------------
Per email: the refresh token and the scopes it was granted with, in the same
per-user folder as the PW proxy pass (src/proxy_session.py). On Windows the
token is encrypted with DPAPI, so only this Windows account on this machine
can read it back. Signing out of the app deletes it.

Coupling note
-------------
The capture wraps Authlib's public `StarletteOAuth2App.authorize_access_token`,
which Streamlit's sign-in route calls with the full token response. If a future
Streamlit stops using it, nothing is captured and runs behave as before this
module existed: public links only, with a message saying so.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
#: Either Drive scope opens a file for reading; the full one is what the
#: shortnotes sign-in that Note Studio shares already holds.
_DRIVE_SCOPES = (DRIVE_SCOPE, "https://www.googleapis.com/auth/drive")
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
#: A run reads the video in its first minutes, but a two-hour recording can take
#: a while to stream; a token with less life than this is renewed before a run.
MIN_LIFETIME_SECONDS = 50 * 60

_lock = threading.Lock()
#: email -> (access token, unix expiry). In memory only.
_cache: dict[str, tuple[str, float]] = {}


def _normalise(email: str) -> str:
    return (email or "").strip().lower()


def _store_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) / "UPSC_Notes" if base else Path.home() / ".upsc_notes"
    return root / "drive_access.json"


# ------------------------------------------------------------------ at rest
def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Windows DPAPI: bytes only this Windows user on this machine can decrypt."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not function(ctypes.byref(source), None, None, None, None,
                    0x1,  # CRYPTPROTECT_UI_FORBIDDEN: never show a prompt
                    ctypes.byref(result)):
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _seal(secret: str) -> dict[str, str]:
    raw = secret.encode("utf-8")
    if os.name == "nt":
        return {"protection": "dpapi",
                "value": base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")}
    return {"protection": "none", "value": base64.b64encode(raw).decode("ascii")}


def _open(sealed: dict[str, Any]) -> str:
    raw = base64.b64decode(str(sealed.get("value") or ""))
    if sealed.get("protection") == "dpapi":
        raw = _dpapi(raw, protect=False)
    return raw.decode("utf-8")


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts", {})
    if not isinstance(accounts, dict):
        return {}
    data["accounts"] = {
        email: entry for email, entry in accounts.items() if isinstance(entry, dict)
    }
    return data


def _write(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        os.chmod(temp, 0o600)
    os.replace(temp, path)


def _has_drive_scope(scope: str) -> bool:
    granted = set((scope or "").split())
    return any(s in granted for s in _DRIVE_SCOPES)


# --------------------------------------------------------------- the account
def remember(email: str, refresh_token: str, scope: str) -> None:
    """Keep this person's refresh token. Never raises: failing to store it only
    means the next run uses public links, as it did before."""
    email = _normalise(email)
    if not (email and refresh_token and _has_drive_scope(scope)):
        return
    with _lock:
        try:
            data = _read()
            accounts = data.setdefault("accounts", {})
            accounts[email] = {"refresh": _seal(refresh_token), "scope": scope,
                               "saved_at": time.time()}
            _write(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("[DRIVE] Could not store the Drive sign-in for %s: %s", email,
                        type(exc).__name__)


def forget(email: str) -> None:
    """Sign-out: drop this person's stored and cached Drive access."""
    email = _normalise(email)
    with _lock:
        _cache.pop(email, None)
        try:
            data = _read()
            if email in (data.get("accounts") or {}):
                del data["accounts"][email]
                _write(data)
        except Exception:  # noqa: BLE001 — a sign-out must never fail on this
            pass


def connected_emails() -> list[str]:
    """Everyone on this Windows account whose Drive sign-in is stored."""
    accounts = _read().get("accounts") or {}
    return sorted(email for email in accounts if is_connected(email))


def is_connected(email: str) -> bool:
    """Has this person allowed Drive access at a sign-in this app captured?"""
    email = _normalise(email)
    if email in _cache:
        return True
    entry = (_read().get("accounts") or {}).get(email) or {}
    return bool(entry.get("refresh")) and _has_drive_scope(str(entry.get("scope") or ""))


def capture(token: dict[str, Any]) -> bool:
    """Take the Drive access out of a sign-in's token response, if it has any."""
    if not isinstance(token, dict):
        return False
    email = _normalise(str((token.get("userinfo") or {}).get("email") or ""))
    scope = str(token.get("scope") or "")
    if not email or not _has_drive_scope(scope):
        return False
    access = str(token.get("access_token") or "")
    expires = float(token.get("expires_at") or 0) or (
        time.time() + float(token.get("expires_in") or 0))
    if access and expires:
        with _lock:
            _cache[email] = (access, expires)
    refresh = str(token.get("refresh_token") or "")
    if refresh:
        remember(email, refresh, scope)
    return True


def access_token(email: str, client_id: str, client_secret: str, *,
                 now: float | None = None) -> tuple[str, str]:
    """(a Drive access token with at least MIN_LIFETIME_SECONDS left, problem).

    The problem is "" on success, else "not_connected" (no Drive sign-in was
    captured), "expired" (Google refused the stored refresh token: revoked, or
    the password changed — it is dropped), or "unavailable" (Google could not be
    reached, or answered with an error).
    """
    email = _normalise(email)
    current = time.time() if now is None else now
    with _lock:
        cached = _cache.get(email)
    if cached and cached[1] - current > MIN_LIFETIME_SECONDS:
        return cached[0], ""

    entry = (_read().get("accounts") or {}).get(email) or {}
    if not entry.get("refresh"):
        return "", "not_connected"
    try:
        refresh = _open(entry["refresh"])
    except Exception:  # noqa: BLE001 — another Windows user's file, or corrupted
        return "", "not_connected"
    try:
        response = requests.post(_TOKEN_ENDPOINT, timeout=20, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token"})
    except requests.RequestException as exc:
        log.warning("[DRIVE] Could not renew the Drive access token: %s", type(exc).__name__)
        return "", "unavailable"
    if response.status_code == 400 and "invalid_grant" in (response.text or ""):
        forget(email)
        return "", "expired"
    if response.status_code != 200:
        log.warning("[DRIVE] Google answered HTTP %s when renewing the Drive access token",
                    response.status_code)
        return "", "unavailable"
    try:
        body = response.json()
    except (ValueError, TypeError):
        log.warning("[DRIVE] Google returned an unreadable token response")
        return "", "unavailable"
    if not isinstance(body, dict):
        return "", "unavailable"
    token = body.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return "", "unavailable"
    try:
        lifetime = float(body.get("expires_in", 3600))
    except (ValueError, TypeError, OverflowError):
        return "", "unavailable"
    if not math.isfinite(lifetime) or lifetime <= MIN_LIFETIME_SECONDS:
        return "", "unavailable"
    with _lock:
        _cache[email] = (token, current + lifetime)
    return token, ""


# ---------------------------------------------------------------- the capture
def install_signin_capture() -> bool:
    """Keep the refresh token Google returns at sign-in (see the module docstring).

    Safe to call on every Streamlit rerun: it wraps the method once.
    """
    try:
        from authlib.integrations.starlette_client import StarletteOAuth2App
    except Exception:  # noqa: BLE001 — no sign-in support installed: nothing to keep
        return False
    original = StarletteOAuth2App.authorize_access_token
    if getattr(original, "_keeps_drive_access", False):
        return True

    async def authorize_access_token(self, request, **kwargs):
        token = await original(self, request, **kwargs)
        try:
            capture(token)
        except Exception as exc:  # noqa: BLE001 — never break a sign-in over this
            log.warning("[DRIVE] Could not keep the Drive sign-in: %s", type(exc).__name__)
        return token

    authorize_access_token._keeps_drive_access = True
    StarletteOAuth2App.authorize_access_token = authorize_access_token
    return True
