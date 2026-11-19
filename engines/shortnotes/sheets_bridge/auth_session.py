"""Which Google account the automation runs as, and may it be used.

The Studio's password guards the shell exactly as the deployed app does. On
top of that the app still enforces **the allowlist PW already maintains**: the
account must be `@pw.live` and listed under this app's column in the
`Whitelisted` sheet (:meth:`sheets_bridge.pw_gate.ProxyAccess.ensure_allowed`),
checked before the account is adopted and again before every paid row. That is
the access control - not Google's consent screen.

There is deliberately **no interactive sign-in gate in the page**. The account
is the one whose token is cached on this machine, restored silently at
start-up, the same way the CLI has always worked: the browser opens once, on
the first run that has no usable token, and never again.

The trade-off is explicit: anyone who can reach this loopback-only server and
knows the Studio password acts as that cached account. A per-browser-session
gate existed here briefly and was removed on request; the allowlist check is
what remains, and it is what decides who may use the tool.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError, describe_exception

STATE_IDLE = "idle"
STATE_PENDING = "pending"
STATE_READY = "ready"
STATE_ERROR = "error"


@dataclass
class Account:
    """A signed-in, allowlisted operator and the clients built from their token."""

    email: str
    creds: object
    access: object            # pw_gate.ProxyAccess
    sheets_service: object
    drive_service: object

    def fresh_services(self):
        """New Sheets/Drive clients for a request-handler thread.

        googleapiclient clients wrap one httplib2.Http, which is not safe to
        share between threads; the run thread keeps the account's clients and
        each browser request builds its own (cheap: discovery is not cached).
        """
        from .google_auth import build_services

        return build_services(self.creds)


class AutomationAuth:
    """The Google account this server runs the automation as.

    Every method takes an optional ``owner`` (the browser session id) so the
    routes can pass one, but the account is process-wide: the parameter is
    accepted and ignored.
    """

    def __init__(self, settings, *, state_dir: Path | None = None) -> None:
        self.settings = settings
        self._dir = state_dir or (Path.home() / ".shortnotes_bridge")
        self._lock = threading.Lock()
        self._account: Account | None = None
        self._state = STATE_IDLE
        self._error = ""
        self._thread: threading.Thread | None = None
        self._restored = False
        # An explicit sign-out must not be undone by the silent restore.
        self.signed_out = False

    # -- observation ---------------------------------------------------------
    def status(self, owner: str = "") -> dict:
        self._restore_once()
        with self._lock:
            return {
                "state": self._state,
                "signedIn": self._account is not None,
                "email": self._account.email if self._account else "",
                "allowed": self._account is not None,
                "error": self._error,
            }

    def account(self, owner: str = "") -> Account | None:
        self._restore_once()
        with self._lock:
            return self._account

    def require(self, owner: str = "") -> Account:
        account = self.account()
        if account is None:
            raise BridgeError(
                "sign_in_required",
                "no usable Google token on this machine - run "
                "'python -m sheets_bridge --check-setup' once to sign in",
            )
        return account

    def signed_in_emails(self) -> list[str]:
        account = self.account()
        return [account.email] if account else []

    # -- control -------------------------------------------------------------
    def start_sign_in(self, owner: str = "", *, open_browser=True, on_url=None) -> None:
        """Open the browser once, for an install with no cached token."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise BridgeError("sign_in_pending", "a sign-in is already in progress")
            self._state = STATE_PENDING
            self._error = ""
            self._open_browser = open_browser
            self._on_url = on_url
            self._thread = threading.Thread(target=self._work, name="automation-signin", daemon=True)
            self._thread.start()

    def sign_out(self, owner: str = "") -> None:
        with self._lock:
            self._account = None
            self._state = STATE_IDLE
            self._error = ""
            self.signed_out = True
        try:
            import pw_access

            with pw_access._sessions_lock:  # pw-app-kit 2026-11-18: one cached pass per Google token
                pw_access._sessions.clear()
        except Exception:  # noqa: BLE001 - the kit may be absent in tests
            pass

    # -- workers -------------------------------------------------------------
    def _work(self) -> None:
        from .google_auth import interactive_sign_in

        try:
            creds, email = interactive_sign_in(
                self.settings.client_secret_path, base=self._dir, verify=self._verify,
                **({"open_browser":False, "on_url":self._on_url} if not getattr(self, "_open_browser", True) else {}),
            )
            self._adopt(creds, email)
        except BridgeError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - class + location only, never content
            self._fail(describe_exception(exc))

    def _restore_once(self) -> None:
        """Adopt the cached token, so a restart needs no browser."""
        with self._lock:
            if self._restored or self._account is not None or self._state == STATE_PENDING:
                return
            if self.signed_out:
                return
            self._restored = True
        from .google_auth import load_any_cached, user_email

        try:
            creds = load_any_cached(
                self.settings.client_secret_path, self.settings.token_path, self._dir,
            )
            if creds is None:
                return
            email = user_email(creds)
            if not email:
                return
            self._adopt(creds, email)
        except Exception:  # noqa: BLE001 - no usable token just means "sign in once"
            with self._lock:
                self._account = None
                self._state = STATE_IDLE

    def _verify(self, creds, email: str):
        """Domain + allowlist, in that order. Raises for anyone not allowed.

        This is the real access control: the `Whitelisted` sheet's column for
        this app. It runs before a token is ever written to disk, so a rejected
        account leaves nothing behind on this machine.
        """
        from .google_auth import token_provider
        from .pw_gate import ALLOWED_DOMAIN, ProxyAccess

        access = ProxyAccess(token_provider(creds), email=email)
        if not access.domain_ok():
            raise BridgeError(
                "not_pw_account",
                f"{email} is not an @{ALLOWED_DOMAIN} account - sign in with your PW account",
            )
        access.ensure_allowed()          # fails closed, exactly like a paid row
        return access

    def _adopt(self, creds, email: str) -> None:
        """Verify the account against the allowlist, then build its clients."""
        from .google_auth import build_services

        access = self._verify(creds, email)
        sheets_service, drive_service = build_services(creds)
        with self._lock:
            self._account = Account(email, creds, access, sheets_service, drive_service)
            self._state = STATE_READY
            self._error = ""
            self.signed_out = False

    def _fail(self, message: str) -> None:
        with self._lock:
            self._account = None
            self._state = STATE_ERROR
            self._error = message
