"""Durable storage for the PW proxy's 7-day session pass.

Why this exists
---------------
`pw_access` exchanges the signed-in user's Google token for a proxy-issued
7-day session pass and rides on that pass, so Google's ~1-hour token expiry
can't interrupt a run. But it caches the pass in a module-level global, which
dies with the process.

This app ships as a desktop EXE that gets closed and reopened constantly, and
Streamlit's own auth cookie outlives the Google token stored inside it. So
after a restart the app still believed the user was signed in, while the only
credential it could produce was an hour-old Google token — every proxy call
came back 401 and the user was told to sign in again, days early.

Persisting the pass closes that gap: sign in once, stay signed in for the full
seven days, across as many restarts as you like.

What is stored
--------------
Only the proxy-issued session pass, its expiry, and the email it belongs to.
Never a Google token, never a provider API key. Storing a session token locally
on a desktop app is what the PW onboarding guide prescribes.

Coupling note
-------------
Reading/writing `pw_access._session` reaches into a private attribute of the
drop-in kit. That is deliberate and contained to this module: the kit offers no
public accessor for the pass. If a future kit revision renames it, only
`_seed`/`_snapshot` below need updating — everything degrades to "mint a fresh
pass", which is the current behaviour anyway.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pw_access

# Refuse a stored pass that is within this many seconds of expiring, so a run
# can't start on a credential that dies mid-flight.
_SAFETY_MARGIN_SECONDS = 15 * 60


def _store_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) / "UPSC_Notes" if base else Path.home() / ".upsc_notes"
    return root / "proxy_session.json"


def _read() -> dict[str, Any]:
    try:
        return json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _seed(token: str, expiry: float) -> None:
    pw_access._session["token"] = token
    pw_access._session["expiry"] = expiry


def _snapshot() -> tuple[str, float]:
    s = getattr(pw_access, "_session", {}) or {}
    return str(s.get("token") or ""), float(s.get("expiry") or 0.0)


def _normalise(email: str) -> str:
    return (email or "").strip().lower()


# Which signed-in email the pass currently sitting in pw_access._session belongs
# to. pw_access._session is a module-level global shared by every browser
# session in the process, and pw_access._auth_token() reuses whatever pass it
# finds there WITHOUT looking at the Google token it was handed. So without
# tracking ownership, a second user signing in reuses the first user's pass:
# the proxy answers /api/allowlist for the wrong identity, a user who is not on
# the whitelist is let in, and their spend is billed to the first user.
_owner_email = ""


def attach(email: str) -> bool:
    """Seed pw_access with this user's stored pass, if one is still good.

    Call before the first proxy request of a run. Returns True when a usable
    pass was restored, False when the caller should let pw_access mint a fresh
    one from the Google token (which requires that token to still be valid).

    Either way this leaves pw_access holding a pass that belongs to `email` and
    nobody else: when there is no usable stored pass, any other user's cached
    pass is evicted rather than left to be reused. That eviction is what makes
    the allowlist gate answer for the person actually signed in, so it must
    happen even when callers ignore the return value.
    """
    global _owner_email
    email = _normalise(email)
    data = _read()
    if data and data.get("email") == email:
        token = str(data.get("token") or "")
        expiry = float(data.get("expiry") or 0.0)
        if token and expiry - time.time() > _SAFETY_MARGIN_SECONDS:
            _seed(token, expiry)
            _owner_email = email
            return True

    # No usable stored pass for THIS user. Drop whatever is in memory (it is
    # someone else's, or stale) so pw_access mints a fresh pass from this
    # user's own Google token. Only the in-memory copy is dropped — the stored
    # file may belong to another user of this machine and is not ours to erase.
    _seed("", 0.0)
    _owner_email = email
    return False


def persist(email: str) -> None:
    """Write whatever pass pw_access currently holds. Never raises — failing to
    cache a pass must not break the app, it just costs a re-login later."""
    email = _normalise(email)
    if _owner_email and _owner_email != email:
        # The in-memory pass was minted for somebody else; writing it under
        # this email would bind the wrong credential to this account on disk
        # and survive restarts for the pass's whole 7-day life.
        return
    token, expiry = _snapshot()
    if not token or expiry <= time.time():
        return
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"email": (email or "").strip().lower(), "token": token, "expiry": expiry}
        path.write_text(json.dumps(payload), encoding="utf-8")
        # POSIX: owner-only. On Windows the per-user LOCALAPPDATA tree already
        # restricts access, and os.chmod there can't express the same thing.
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception:
        pass


def clear() -> None:
    """Drop the stored pass and the in-process copy. Used on sign-out, so the
    next user of a shared machine can't inherit this one's session."""
    global _owner_email
    _owner_email = ""
    _seed("", 0.0)
    try:
        _store_path().unlink(missing_ok=True)
    except Exception:
        pass


def expires_at() -> float:
    """Unix seconds at which the active pass dies (0.0 if there isn't one)."""
    return _snapshot()[1]
