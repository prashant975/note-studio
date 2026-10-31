"""Every QBG-portal constant and tunable in one place.

Copied from the QBG Solution Addition Bot so the two stay explainable side by
side: the environment variable names are the ones the bot already documents,
and a value proven on one machine carries over to the other unchanged.

The one deliberately different name is ``QBG_LONG_TIMEOUT`` (the bot calls it
``LONG_TIMEOUT``): inside a Streamlit app a bare ``LONG_TIMEOUT`` environment
variable is too generic to claim.
"""
from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y"}


# ---------------------------------------------------------------- portal ----
QBG_HOST = "qbg-admin.penpencil.co"
QBG_BASE_URL = f"https://{QBG_HOST}"

# The question-search page is the only per-question entry point confirmed to
# exist (the creation pipeline's own config points at it). The direct link
# format is still unconfirmed portal-side; once one real "QBG Link" cell has
# been copied from a working sheet, set the template and the search page is
# never needed again. {qbg_id} is replaced with the question's unique_id.
QBG_SEARCH_URL = os.getenv("QBG_QUESTION_SEARCH_URL",
                           f"{QBG_BASE_URL}/question-search")
QBG_QUESTION_LINK_TEMPLATE = os.getenv("QBG_QUESTION_LINK_TEMPLATE", "").strip()

# --------------------------------------------------------------- browser ----
CHROME_CDP_URL = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9222")
QBG_CHROME_PROFILE = os.getenv(
    "QBG_CHROME_PROFILE", os.path.expanduser("~/qbg_chrome_debug_profile"))
QBG_HEADLESS = _bool_env("QBG_HEADLESS", False)

# -------------------------------------------------------------- timeouts ----
SHORT_TIMEOUT = 1500
NORMAL_TIMEOUT = 5000
LONG_TIMEOUT = _int_env("QBG_LONG_TIMEOUT", 30000)

# Adaptive waits: short defaults plus state-based checks, exactly as tuned in
# the bot. Raise through the environment only for an unusually slow session.
QBG_INITIAL_SETTLE_MS = _int_env("QBG_INITIAL_SETTLE_MS", 400)
QBG_FORM_SETTLE_MS = _int_env("QBG_FORM_SETTLE_MS", 250)
QBG_TAB_SETTLE_MS = _int_env("QBG_TAB_SETTLE_MS", 200)
QBG_STABLE_POLL_MS = _int_env("QBG_STABLE_POLL_MS", 250)
QBG_STABLE_CHECKS = max(1, _int_env("QBG_STABLE_CHECKS", 2))
QBG_SAVE_RESPONSE_WAIT_SECONDS = _float_env("QBG_SAVE_RESPONSE_WAIT_SECONDS", 12)
QBG_SAVE_GRACE_MS = _int_env("QBG_SAVE_GRACE_MS", 750)
QBG_PATCH_OBSERVED_GRACE_MS = _int_env("QBG_PATCH_OBSERVED_GRACE_MS", 900)
X_CATEGORY_SETTLE_MS = _int_env("X_CATEGORY_SETTLE_MS", 900)

# ---------------------------------------------------------------- policy ----
# Three stages are required: normal save -> tick/untick X Category -> AI Guru.
OVERWRITE_RETRIES = max(3, _int_env("QBG_OVERWRITE_RETRIES", 3))
VERIFY_OVERWRITE = _bool_env("QBG_VERIFY_OVERWRITE", True)

# ----------------------------------------------------------- diagnostics ----
# The bot dropped screenshots in its working directory; inside the app they
# would land wherever Streamlit was launched from, so give them a home.
_DEFAULT_DEBUG_DIR = Path(__file__).resolve().parents[2] / "runs" / "_qbg_debug"
QBG_DEBUG_DIR = Path(os.getenv("QBG_DEBUG_DIR", "") or _DEFAULT_DEBUG_DIR)


def debug_path(name: str) -> str:
    """An absolute path for a diagnostic screenshot, creating the folder."""
    try:
        QBG_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return name
    return str(QBG_DEBUG_DIR / name)
