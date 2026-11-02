"""OAuth installed-app (desktop) flow with a locally cached, refreshable token.

Not a service account: the Drive folders are already shared with the human
account that runs this. The browser is opened only on first run (or when the
cached token cannot be refreshed / was issued for different scopes).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .errors import BridgeError
from .pw_gate import ALLOWED_DOMAIN
from .settings import SCOPES

# An abandoned consent tab must not block the sign-in button for ever.
SIGN_IN_TIMEOUT_SECONDS = 300


def token_path_for(email: str, base: Path | None = None) -> Path:
    """Where one account's token lives.

    Several people may use the same machine, so tokens are filed per account
    instead of overwriting a single ``token.json``. The email is hashed rather
    than used as a filename: it keeps the path safe on every filesystem and
    avoids scattering addresses through the directory listing.
    """
    root = (base or (Path.home() / ".shortnotes_bridge")) / "tokens"
    digest = hashlib.sha256((email or "").strip().casefold().encode("utf-8")).hexdigest()
    return root / f"{digest[:32]}.json"


def client_id_of(client_secret_path: Path) -> str:
    """The OAuth client this install is configured to use, or ""."""
    import json

    try:
        info = json.loads(Path(client_secret_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    block = info.get("installed") or info.get("web") or {}
    return str(block.get("client_id", "") or "")


def _load_cached(token_path: Path, client_id: str = ""):
    from google.oauth2.credentials import Credentials

    # A sidecar preserves older standalone apps' shared files. New credentials
    # are never written in plaintext; those apps keep their independent login.
    secure = token_path.with_suffix(".protected.json")
    if os.name == "nt" and not token_path.name.endswith(".protected.json") and secure.is_file():
        token_path = secure

    if not token_path.is_file():
        return None
    try:
        info = json.loads(token_path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            return None
        if sys.platform == "darwin":
            from .macos_credentials import unprotect
            info = json.loads(unprotect(info))
        elif os.name == "nt" and info.get("enc") == "dpapi":
            from .windows_credentials import transform
            info = json.loads(transform(info["value"], decrypt=True))
        elif isinstance(info, dict) and info.get("enc") == "keychain-aesgcm-v1":
            return None
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    # A cached token belongs to the OAuth client that issued it. Moving the
    # app to another client (e.g. from a personal project to the pw.live
    # organisation's Internal one) leaves tokens that Google rejects with an
    # opaque invalid_client at refresh time, which reads like a broken app.
    # Treat a foreign token as "no token" so the operator is simply asked to
    # sign in again.
    if client_id and str(info.get("client_id", "")) not in ("", client_id):
        return None
    granted = info.get("scopes") or []
    if isinstance(granted, str):
        granted = granted.split()
    if not set(SCOPES).issubset(set(granted)):
        return None  # token issued for other scopes (e.g. drive.file) -> re-auth
    try:
        return Credentials.from_authorized_user_info(info, list(SCOPES))
    except ValueError:
        return None


def _save(creds, token_path: Path) -> None:
    if os.name == "nt" and not token_path.name.endswith(".protected.json"):
        token_path = token_path.with_suffix(".protected.json")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_path.with_suffix(token_path.suffix + ".tmp")
    value = creds.to_json()
    if sys.platform == "darwin":
        from .macos_credentials import protect
        value = json.dumps(protect(value))
    elif os.name == "nt":
        from .windows_credentials import transform
        value = json.dumps({"enc": "dpapi", "value": transform(value)})
    tmp.write_text(value, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, token_path)


def load_credentials(client_secret_path: Path, token_path: Path, *, interactive: bool = True):
    """Return valid google.oauth2 credentials, refreshing or prompting as needed."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    creds = _load_cached(token_path, client_id_of(client_secret_path))
    if creds is not None and creds.valid:
        return creds
    if creds is not None and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
        else:
            _save(creds, token_path)
            return creds

    # The page's sign-in files its token per account, so the legacy single
    # path is often empty on a machine that is perfectly well signed in.
    shared = load_any_cached(client_secret_path, token_path)
    if shared is not None:
        return shared

    if not interactive:
        raise BridgeError("auth_required", "no valid cached token and interactive auth disabled")
    if not client_secret_path.is_file():
        raise BridgeError(
            "client_secret_missing",
            f"OAuth client file not found at {client_secret_path}. Create a Desktop-app "
            "OAuth client in Google Cloud Console and save its JSON there "
            "(or set SHEETS_BRIDGE_CLIENT_SECRET).",
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(SCOPES))
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    _save(creds, token_path)
    return creds


def _refreshed(creds, token_path: Path):
    """Return valid creds (refreshing and re-saving if needed), else None."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    if creds is None:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            return None
        _save(creds, token_path)
        return creds
    return None


def save_token(creds, email: str, base: Path | None = None) -> None:
    """File one account's token under its own path (see token_path_for)."""
    _save(creds, token_path_for(email, base))


def load_cached_for(email: str, base: Path | None = None):
    """Credentials for one account from its own cache, or None."""
    path = token_path_for(email, base)
    return _refreshed(_load_cached(path), path)


def load_any_cached(client_secret_path: Path, token_path: Path, base: Path | None = None):
    """The most recently used cached credential for the configured client.

    Sign-ins file their token per account under ``tokens/``; only the CLI's
    older path writes ``token.json``. Restoring must look at both, newest
    first, or a machine that signed in through the page would be asked to sign
    in again after every restart.
    """
    root = (base or (token_path.parent if token_path else Path.home() / ".shortnotes_bridge")) / "tokens"
    try:
        candidates = sorted(root.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        candidates = []
    if token_path is not None:
        candidates.append(token_path)          # the legacy single-token path, last
    client_id = client_id_of(client_secret_path)
    for path in candidates:
        creds = _refreshed(_load_cached(path, client_id), path)
        if creds is not None:
            return creds
    return None


def interactive_sign_in(
    client_secret_path: Path,
    *,
    base: Path | None = None,
    open_browser: bool = True,
    verify=None,
):
    """Run the browser sign-in and file the token under the account's own path.

    This is the same installed-app (Desktop) flow the CLI uses - it spins a
    throwaway local server on a free port and opens a tab - so the Studio's
    "Continue with Google" button needs no separate Web OAuth client.

    Two things keep a stranger's credentials off this machine:

    * the account chooser is asked to show only the allowed domain (``hd``).
      Google treats that as a *hint*, so it is convenience, never the check;
    * ``verify(creds, email)`` runs **before the token is written to disk**.
      It raises for an account that is not allowed, and the refresh token is
      never persisted - previously a rejected sign-in still left a usable
      token in ``~/.shortnotes_bridge/tokens``.

    Returns ``(creds, email)``.
    """
    if not client_secret_path.is_file():
        raise BridgeError(
            "client_secret_missing",
            f"OAuth client file not found at {client_secret_path}. Create a Desktop-app "
            "OAuth client in Google Cloud Console and save its JSON there "
            "(or set SHEETS_BRIDGE_CLIENT_SECRET).",
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(SCOPES))
    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            prompt="consent",
            timeout_seconds=SIGN_IN_TIMEOUT_SECONDS,
            hd=ALLOWED_DOMAIN,          # ask Google to offer only @pw.live accounts
        )
    except Exception as exc:  # noqa: BLE001 - the library's timeout class name varies by version
        if "timeout" in type(exc).__name__.lower():
            raise BridgeError(
                "sign_in_timed_out",
                "no Google sign-in was completed in time; press Continue with Google again",
            ) from exc
        raise
    email = user_email(creds)
    if not email:
        raise BridgeError("sign_in_failed", "signed in, but Google did not return an email address")
    if verify is not None:
        verify(creds, email)         # raises for a non-allowed account: nothing is saved
    _save(creds, token_path_for(email, base))
    return creds, email


def token_provider(creds):
    """Return a zero-arg callable giving a fresh Google access token.

    The PW kit accepts a callable so long runs keep working after Google's
    short-lived token expires (the kit trades it for a 7-day proxy pass).
    """
    from google.auth.transport.requests import Request

    def provide() -> str:
        if not creds.valid:
            creds.refresh(Request())
        return creds.token or ""

    return provide


def user_email(creds) -> str:
    """Signed-in account's email (for display and the @pw.live pre-check)."""
    import requests as _requests

    try:
        response = _requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_provider(creds)()}"},
            timeout=20,
        )
        if response.status_code == 200:
            return str(response.json().get("email", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def build_services(creds):
    """Return (sheets_service, drive_service) discovery clients."""
    from googleapiclient.discovery import build

    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive
