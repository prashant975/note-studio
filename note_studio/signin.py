"""Google sign-in for the PW proxy, through the copied sheets_bridge.auth_session.

The same account the old app uses: its token lives in the folder chosen in
Settings (the old app's, or NoteCraft's own). Restoring a saved sign-in checks
the PW allowlist over the network, so it runs in the background at start-up and
pages only read the result (`snapshot`). Sign in opens the browser once.
"""
from __future__ import annotations

import os
import threading

from . import old_app

_lock = threading.Lock()
_state: dict = {"auth": None, "folder": None}


class SignInNeeded(RuntimeError):
    """Plain words for the run page: what to do before notes can be made."""


def _offline() -> bool:
    return os.environ.get("NOTE_STUDIO_OFFLINE") == "1"


def auth():
    """The process-wide AutomationAuth for the chosen folder; None while undecided."""
    folder = old_app.state_dir()
    if folder is None:
        return None
    with _lock:
        if _state["auth"] is None or _state["folder"] != folder:
            from sheets_bridge.auth_session import AutomationAuth
            from sheets_bridge.settings import Settings

            _state.update(auth=AutomationAuth(Settings.from_env(), state_dir=folder), folder=folder)
        return _state["auth"]


def reset() -> None:
    """The folder changed (Settings choice): forget the old account, restore anew."""
    with _lock:
        _state.update(auth=None, folder=None)
    warm()


def warm() -> None:
    """Restore a saved sign-in in the background; never on a page render."""
    if _offline():
        return
    current = auth()
    if current is not None:
        threading.Thread(target=current.status, name="signin-restore", daemon=True).start()


def snapshot() -> dict:
    """{'state': undecided|checking|pending|ready|signed_out|error, 'email', 'error'}."""
    if not old_app.choice():
        return {"state": "undecided", "email": "", "error": ""}
    current = _state["auth"]
    if current is None:
        return {"state": "checking", "email": "", "error": ""}
    with current._lock:
        account, state, error, restored = current._account, current._state, current._error, current._restored
    if account is not None:
        return {"state": "ready", "email": account.email, "error": ""}
    if state == "pending":
        return {"state": "pending", "email": "", "error": ""}
    if state == "error":
        return {"state": "error", "email": "", "error": error}
    if not restored and not current.signed_out:
        return {"state": "checking", "email": "", "error": ""}
    return {"state": "signed_out", "email": "", "error": ""}


def start() -> None:
    current = auth()
    if current is None:
        raise SignInNeeded("Choose first whether NoteCraft uses the old app's sign-in.")
    current.start_sign_in()


def sign_out() -> None:
    current = auth()
    if current is not None:
        current.sign_out()


def google_token() -> str:
    """A fresh Google token for an engine that runs in its own process. It goes
    into that process's environment only, never into a file."""
    current = auth()
    if current is None:
        raise SignInNeeded("Choose in Settings whether NoteCraft uses the old app's sign-in, then sign in.")
    account = current.account()
    if account is None:
        raise SignInNeeded("Sign in with Google in Settings, then press Try again.")
    try:
        _renew_if_short(getattr(account, "creds", None))
        token = account.access.token_provider()
    except Exception as exc:
        raise SignInNeeded("The Google sign-in has expired. Sign in again in Settings, then press Try again.") from exc
    if not token:
        raise SignInNeeded("Sign in with Google in Settings, then press Try again.")
    return token


#: The engine reads a Drive class recording with this token in its first minutes,
#: and a two-hour recording can take a while to stream: a token with less life
#: than this is renewed before it is handed over.
_MIN_TOKEN_LIFE = 50 * 60


def _renew_if_short(creds) -> None:
    """google-auth only renews a token in its last few minutes; renew sooner."""
    expiry = getattr(creds, "expiry", None)
    if creds is None or expiry is None:
        return
    from datetime import datetime, timezone

    from google.auth.transport.requests import Request

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    left = (expiry - datetime.now(timezone.utc)).total_seconds()
    if left < _MIN_TOKEN_LIFE:
        creds.refresh(Request())


def proxy_token_provider():
    """The signed-in account's PW proxy token provider, for an engine in this
    process that calls the proxy itself (the typeset notes)."""
    current = auth()
    if current is None:
        raise SignInNeeded("Choose in Settings whether NoteCraft uses the old app's sign-in, then sign in.")
    account = current.account()
    if account is None:
        raise SignInNeeded("Sign in with Google in Settings, then press Try again.")
    return account.access.token_provider


def ensure_proxy(model: str = "", fallbacks=()) -> None:
    """Right before a paid run: route this run's model calls through the PW
    proxy as the signed-in account (the old app's _ensure_proxy), with this
    run's own model and fallbacks."""
    from sheets_bridge import pw_transport

    current = auth()
    if current is None:
        raise SignInNeeded("Choose in Settings whether NoteCraft uses the old app's sign-in, then sign in.")
    account = current.account()
    if account is None:
        raise SignInNeeded("Sign in with Google in Settings, then press Try again.")
    account.access.model = pw_transport.install(account.access.token_provider, model=model,
                                                fallbacks=tuple(fallbacks))
