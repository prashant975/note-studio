"""PW proxy access: identity, allowlist gate, and transport installation.

Every paid/main action calls :meth:`ProxyAccess.ensure_allowed` first and fails
closed - a denial *or* an undecidable answer (proxy unreachable, token bad)
stops the action.
"""
from __future__ import annotations

import pw_access

from .errors import BridgeError

ALLOWED_DOMAIN = "pw.live"


class ProxyAccess:
    """Wraps the signed-in Google user's token for PW proxy calls."""

    def __init__(self, token_provider, *, email: str = "") -> None:
        self.token_provider = token_provider
        self.email = email or ""
        self.model = ""

    # -- identity -----------------------------------------------------------
    def domain_ok(self) -> bool:
        return self.email.lower().endswith("@" + ALLOWED_DOMAIN)

    # -- gate ---------------------------------------------------------------
    def allow_status(self) -> str:
        """'allowed' | 'denied' | 'error' - never raises."""
        try:
            return pw_access.check_allowed_status(self.token_provider)
        except Exception:  # noqa: BLE001
            return "error"

    def ensure_allowed(self) -> None:
        """Fail closed before a paid/main action."""
        status = self.allow_status()
        if status == "allowed":
            return
        if status == "denied":
            raise BridgeError(
                "not_authorized",
                f"{self.email or 'this account'} is not allowed for "
                f"'{pw_access.APP_NAME}'. Ask the owner to add the email under that "
                "column in the Whitelisted sheet.",
            )
        raise BridgeError(
            "allowlist_undecided",
            "the PW proxy could not confirm access (network, token or server issue); "
            "refusing to run",
        )

    # -- transport ----------------------------------------------------------
    def install_transport(self, model: str = "") -> str:
        from .pw_transport import install

        self.model = install(self.token_provider, model=model)
        return self.model


def new_task_id(prefix: str = "handwritten-notes") -> str:
    return pw_access.new_task_id(prefix)
