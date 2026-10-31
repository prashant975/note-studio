from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any


# Seven days of uninterrupted sign-in — one sign-in, then nothing until the
# week is up. Google's own id/access token dies after ~1 hour and Streamlit
# exposes no refresh token, so the app does not depend on that token staying
# fresh: pw_access exchanges it for a proxy-issued 7-day session pass, and
# src/proxy_session.py persists that pass across app restarts.
#
# This matches the pass's own 7-day life exactly, so both end together. The
# 15-minute safety margin in proxy_session.attach() stops a run from starting
# on a pass that would die mid-flight; at that point the user re-signs-in,
# which is the intended end of a seven-day session.
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _timestamp(claims: Mapping[str, Any], key: str) -> int:
    try:
        return int(claims.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def session_started_at(claims: Mapping[str, Any]) -> int:
    """Return the provider authentication time, falling back to token issue time."""
    return _timestamp(claims, "auth_time") or _timestamp(claims, "iat")


def session_expired(
    claims: Mapping[str, Any],
    *,
    now: float | None = None,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> bool:
    started = session_started_at(claims)
    current = time.time() if now is None else now
    return started > 0 and current - started >= max_age_seconds


def session_days_remaining(
    claims: Mapping[str, Any],
    *,
    now: float | None = None,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> int | None:
    """Whole days left before the sign-in expires, for the profile panel.

    Rounded UP, so a session one minute old reads "6 more days" rather than
    "5". Returns None when the token carries no auth_time/iat to measure from —
    callers hide the line instead of guessing.
    """
    import math

    started = session_started_at(claims)
    if started <= 0:
        return None
    current = time.time() if now is None else now
    left = max_age_seconds - (current - started)
    return max(0, math.ceil(left / 86400))
