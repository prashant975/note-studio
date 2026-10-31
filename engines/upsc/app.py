from __future__ import annotations

import re
import hashlib
import io
import tempfile
import traceback
import os
import base64
import html
import time
import zipfile
from collections import Counter
from contextlib import nullcontext
import warnings
import json
from pathlib import Path

# Streamlit's OAuth login imports `authlib.jose`, which Authlib 1.6+ deprecated
# (removal only in 2.0). The warning is cosmetic — silence it here, before
# Streamlit is imported, rather than downgrading Authlib (older versions break
# Streamlit 1.58's login token handling). See requirements.txt.
try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except Exception:
    warnings.filterwarnings("ignore", message=r".*authlib\.jose.*")

import streamlit as st

import pw_access
from src.ai_client import GeminiClient, GeminiError
from src.auth_session import session_days_remaining, session_expired
from src import drive_auth
from src import proxy_session
from src.config import (
    ADVANCED_QC_OPTIONS,
    APP_NAME,
    DCN_LAYOUT_OPTIONS,
    DEFAULT_ADVANCED_QC_OPTIONS,
    DEFAULT_DCN_LAYOUT_OPTIONS,
    DEFAULT_ERROR_REPORT_STRICTNESS,
    DEFAULT_GENERATE_ERROR_REPORT,
    DEVELOPER_MODE,
    ERROR_REPORT_STRICTNESS,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_QUALITY_LEVEL,
    DEFAULT_QUALITY_OPTIONS,
    DEFAULT_UPSC_FACT_MODE,
    DEFAULT_UPSC_NOTE_DEPTH,
    DEFAULT_UPSC_NOTE_TYPE,
    DEFAULT_UPSC_OUTPUT_FORMAT,
    QUALITY_LEVEL_LABELS,
    QUALITY_LEVELS,
    QUALITY_OPTIONS,
    UPSC_AREAS,
    UPSC_FACT_MODES,
    UPSC_NOTE_DEPTHS,
    UPSC_NOTE_TYPE_LABELS,
    UPSC_NOTE_TYPES,
    UPSC_OUTPUT_FORMATS,
    APP_VERSION,
    build_label,
    version_label,
)
from src.config import DCN_SOURCE_OPTIONS, stamp_ist
from src import app_defaults as defaults
from src.app_defaults import developer_mode_enabled
from src.pipeline import run_pipeline
from src.transcript import MIN_TRANSCRIPT_WORDS
from src.utils import format_duration
from src.video_sources import (
    SOURCE_DIRECT_VIDEO,
    SOURCE_GOOGLE_DRIVE,
    SOURCE_LABELS,
    detect_video_source,
)
from src.video_sources.errors import VideoSourceError

# Keep the Drive refresh token Google returns at sign-in, which Streamlit itself
# throws away (see src/drive_auth.py). Wraps once; every rerun after is a no-op.
drive_auth.install_signin_capture()

st.set_page_config(page_title=APP_NAME, layout="wide")

# The proxy authorises users under this name (kept separate from the product
# name so the UPSC rebrand doesn't lock existing whitelisted users out). Shown
# in the "ask an admin" messages so users add their email to the right column.
PROXY_APP_NAME = pw_access.APP_NAME

# Build stamp shown in the header and on the login page ("v1.0.0 · 28 Jul 2026"),
# so a teammate reporting a problem can say which build they are running.
# Bump APP_VERSION / APP_VERSION_DATE in src/config.py on each release.
APP_VERSION_LABEL = version_label()
BUILD_LABEL = build_label()
# Version + IST date/time for the header. Read once per rerun so the
# clock in the header is the clock at the top of this render.
_STAMP = stamp_ist()

BASE_DIR = Path(os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).parent))).expanduser()


def _ui_logo_path() -> Path | None:
    """Logo for the app UI: a custom upsc_logo.png if present, else the PW mark."""
    for name in ("upsc_logo.png", "pw_logo.png"):
        candidate = BASE_DIR / "assets" / name
        if candidate.exists():
            return candidate
    return None


LOGO_PATH = _ui_logo_path()
ALLOWED_EMAIL_DOMAIN = "pw.live"


# The local email-allowlist subsystem (old Google sheet / workbook / text file)
# has been removed. The PW proxy's "Whitelisted" sheet is now the SINGLE source
# of truth for who may use this app (checked via pw_access.check_allowed at
# generate time). If the proxy is unreachable, the app fails closed.


def _google_auth_configured() -> bool:
    try:
        google_auth = st.secrets.get("auth", {}).get("google", {})
        client_id = str(google_auth.get("client_id", "")).strip()
        client_secret = str(google_auth.get("client_secret", "")).strip()
    except Exception:
        return False

    placeholder_values = ("", "your-google-oauth-client-id", "your-google-oauth-client-secret")
    return (
        client_id.endswith(".apps.googleusercontent.com")
        and client_secret not in placeholder_values
        and not client_id.startswith("your-google-oauth-client-id")
    )


def _current_user_email() -> str:
    email = ""
    try:
        email = st.user.get("email", "")
    except Exception:
        email = ""
    return str(email).strip().lower()


def _google_proxy_token() -> str:
    """The Google token passed to the PW proxy. The proxy verifies a Google
    **id_token** (a JWT with the user's email), so prefer that and fall back to
    the access token. Requires `expose_tokens = ["access", "id"]` in secrets."""
    try:
        tokens = st.user.get("tokens", {})
    except Exception:
        tokens = {}

    def _get(key: str) -> str:
        try:
            return str(tokens.get(key, "") or "").strip()
        except Exception:
            return str(getattr(tokens, key, "") or "").strip()

    return _get("id") or _get("access")


ALLOWLIST_CACHE_TTL_SECONDS = 300  # re-check the proxy whitelist at most every 5 min


def _proxy_access_status(force: bool = False) -> str:
    """Return the PW proxy's authorization status ("allowed"/"denied"/"error")
    for the signed-in user, caching an "allowed" result in the session for
    ALLOWLIST_CACHE_TTL_SECONDS so we don't call the proxy on every Streamlit
    rerun. Non-allowed results are never cached, so a newly-whitelisted user is
    admitted on their next interaction. Does NOT touch the AI/usage pipeline."""
    email = _current_user_email()
    now = time.time()
    cache = st.session_state.get("_allow_cache")
    if (
        not force
        and isinstance(cache, dict)
        and cache.get("status") == "allowed"
        and cache.get("email") == email
        and (now - float(cache.get("ts", 0))) < ALLOWLIST_CACHE_TTL_SECONDS
    ):
        return "allowed"
    # Restore this user's stored 7-day pass before the first proxy call, so a
    # restarted app doesn't fall back to the (long-expired) Google token in the
    # auth cookie. If nothing usable is stored, pw_access mints a fresh pass
    # from the Google token during the call below — persist() then keeps it.
    proxy_session.attach(email)
    status = pw_access.check_allowed_status(_google_proxy_token())
    proxy_session.persist(email)
    st.session_state["_allow_cache"] = {"email": email, "status": status, "ts": now}
    return status


def _usage_tracking_label(result) -> str:
    """Describe where this run's usage was recorded.

    Under pw-app-kit v2 the proxy logs every call it serves, so the app has no
    write of its own to succeed or fail — claiming failure here (as the old
    combined-row label did) would be wrong. Show the task id instead: it is
    what groups this run's rows in the Raw Usage Ledger Export.
    """
    task_id = (result.metadata or {}).get("task_id")
    if task_id:
        return f"PW proxy — raw rows logged under task {task_id}"
    return "PW proxy (no paid calls were made for this run)"


def _logout_user() -> None:
    # Drop the stored 7-day proxy pass too, otherwise the next person to sign in
    # on this machine would inherit the previous user's session.
    proxy_session.clear()
    # And this person's Drive access, for the same reason.
    drive_auth.forget(_current_user_email())
    st.session_state.pop("_allow_cache", None)
    if bool(getattr(st.user, "is_logged_in", False)):
        st.logout()
    st.rerun()


def _drive_access() -> tuple[str, str]:
    """(the signed-in person's own Drive read token, their email) for one run.

    An empty token means the run can open only public links; a Drive link that
    is limited to PW then says how to allow Drive access.
    """
    email = _current_user_email()
    try:
        google_auth = st.secrets.get("auth", {}).get("google", {})
        client_id = str(google_auth.get("client_id", "")).strip()
        client_secret = str(google_auth.get("client_secret", "")).strip()
    except Exception:
        return "", email
    token, _problem = drive_auth.access_token(email, client_id, client_secret)
    return token, email


def _auth_session_expired() -> bool:
    """Expire seven days after Google authentication, not after token renewal."""
    try:
        return session_expired(st.user)
    except Exception:
        return False


# No proactive token renewal. Google's id/access token expires after ~1 hour,
# but pw_access exchanges it for a proxy-issued 7-day session pass on the first
# call and rides on that pass from then on — so an expired Google token no
# longer interrupts anything. Interrupting the user hourly to re-run OIDC is
# exactly the wall the session pass exists to remove. If the pass ever cannot
# be minted or is rejected, _proxy_access_status() returns "error" and the
# handler below offers a click-initiated sign-in.


def _active_theme() -> str:
    theme = st.session_state.get("ui_theme", "Dark")
    return "Dark" if theme == "Dark" else "Light"


def _render_theme_picker(key: str) -> None:
    current = _active_theme()
    st.radio(
        "Appearance",
        ["Light", "Dark"],
        index=0 if current == "Light" else 1,
        key=key,
        horizontal=True,
    )
    if st.session_state.get(key) != st.session_state.get("ui_theme"):
        st.session_state.ui_theme = st.session_state[key]
        st.rerun()


def _render_global_styles() -> None:
    is_dark = _active_theme() == "Dark"
    theme_vars = """
            --page: #eef2f8;
            --surface: #ffffff;
            --surface-muted: #f3f6fb;
            --surface-alt: #fafbfe;
            --border: #e4e9f1;
            --border-strong: #cbd5e2;
            --text: #0f1b2e;
            --text-soft: #46566b;
            --text-muted: #74839a;
            --brand: #17408b;
            --brand-strong: #0f2f6b;
            --brand-soft: #eaf1fb;
            --accent: #2f6bd8;
            --accent-strong: #1f56bd;
            --warning-bg: #fff8e6;
            --error-bg: #fdecec;
            --success-bg: #e9f9f1;
            --shadow-sm: 0 1px 2px rgba(16, 32, 58, 0.06);
            --shadow: 0 6px 20px rgba(16, 32, 58, 0.08);
            --shadow-lg: 0 18px 44px rgba(16, 32, 58, 0.12);
            --ring: rgba(47, 107, 216, 0.28);
    """
    if is_dark:
        theme_vars = """
            --page: #0b1220;
            --surface: #111a2b;
            --surface-muted: #16223a;
            --surface-alt: #0f1829;
            --border: #24324a;
            --border-strong: #35486a;
            --text: #eef3fb;
            --text-soft: #c2cddd;
            --text-muted: #8b9bb4;
            --brand: #5b9bff;
            --brand-strong: #8ab8ff;
            --brand-soft: #15233c;
            --accent: #3b82f6;
            --accent-strong: #60a5fa;
            --warning-bg: #3a2f0d;
            --error-bg: #3a1620;
            --success-bg: #0b2e22;
            --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
            --shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
            --shadow-lg: 0 20px 50px rgba(0, 0, 0, 0.5);
            --ring: rgba(96, 165, 250, 0.35);
        """

    st.markdown(
        """
        <style>
        :root {
__THEME_VARS__
            --radius: 14px;
            --radius-sm: 10px;
        }
        .stApp {
            background:
                radial-gradient(1100px 460px at 100% -8%, var(--brand-soft) 0%, transparent 55%),
                var(--page);
            color: var(--text);
            font-family: "Inter", "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
            -webkit-font-smoothing: antialiased;
        }
        [data-testid="stHeader"] { background: transparent; border: 0; }
        [data-testid="stMainBlockContainer"] {
            max-width: 900px;
            padding-top: 2rem;
            padding-bottom: 4rem;
        }
        [data-testid="stSidebar"] {
            background: var(--surface);
            border-right: 1px solid var(--border);
        }
        [data-testid="stSidebar"] [data-testid="stImage"] { display: flex; justify-content: center; }
        h1, h2, h3, h4, h5, h6,
        p, label, span, small,
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] * {
            color: var(--text);
        }
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] * {
            color: var(--text-muted) !important;
        }

        /* Header */
        .app-header {
            position: relative;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 1.5rem;
            padding: 1.35rem 1.6rem;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            background: linear-gradient(180deg, var(--surface) 0%, var(--surface-alt) 100%);
            box-shadow: var(--shadow);
            overflow: hidden;
        }
        .app-header::before {
            content: "";
            position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
            background: linear-gradient(180deg, var(--accent), var(--brand));
        }
        .app-brand { display: flex; align-items: center; gap: 0.95rem; min-width: 0; }
        .app-logo {
            width: 46px; height: 46px; object-fit: contain; border-radius: 12px;
            background: #ffffff; border: 1px solid var(--border); padding: 0.3rem;
            box-shadow: var(--shadow-sm);
        }
        .app-title {
            color: var(--text);
            font-size: clamp(1.5rem, 2.6vw, 2rem);
            line-height: 1.12; font-weight: 800; letter-spacing: -0.02em; margin: 0;
        }
        .app-title-row { display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap; }
        .app-version {
            display: inline-block; color: var(--text-muted); font-size: 0.7rem;
            font-weight: 700; letter-spacing: 0.04em; white-space: nowrap;
            padding: 0.15rem 0.5rem; border: 1px solid var(--border);
            border-radius: 999px; background: var(--surface-2, transparent);
        }
        .app-subtitle { color: var(--text-soft); font-size: 0.92rem; margin-top: 0.28rem; }
        .user-box {
            max-width: 16rem; color: var(--text-muted); font-size: 0.72rem;
            text-transform: uppercase; letter-spacing: 0.06em; text-align: right;
            line-height: 1.5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .user-box b {
            display: block; color: var(--text-soft); font-size: 0.9rem;
            text-transform: none; letter-spacing: 0; font-weight: 700;
        }

        /* Cards / panels */
        [data-testid="stVerticalBlockBorderWrapper"] {
            padding: 1.5rem 1.6rem !important;
            border: 1px solid var(--border) !important;
            border-radius: var(--radius) !important;
            background: var(--surface) !important;
            box-shadow: var(--shadow);
        }
        .panel-title {
            margin: 0 0 0.3rem; color: var(--text);
            font-size: 1.15rem; font-weight: 800; letter-spacing: -0.01em;
        }
        .panel-copy { margin: 0 0 1.1rem; color: var(--text-soft); font-size: 0.92rem; }

        /* Buttons */
        div.stButton > button,
        div.stDownloadButton > button,
        [data-testid="stFileUploader"] button {
            border-radius: var(--radius-sm); font-weight: 650; min-height: 2.7rem;
            transition: transform 0.16s ease, box-shadow 0.16s ease, filter 0.16s ease, background 0.16s ease, border-color 0.16s ease;
            letter-spacing: 0.01em;
        }
        div.stButton > button[kind="primary"],
        div.stDownloadButton > button {
            background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
            border: 1px solid var(--accent-strong); color: #ffffff;
            box-shadow: 0 6px 16px rgba(31, 86, 189, 0.28);
        }
        div.stButton > button[kind="primary"]:hover,
        div.stDownloadButton > button:hover {
            filter: brightness(1.05); transform: translateY(-1px);
            box-shadow: 0 10px 22px rgba(31, 86, 189, 0.34); color: #ffffff;
        }
        div.stButton > button[kind="primary"]:active,
        div.stDownloadButton > button:active { transform: translateY(0); }
        div.stButton > button:not([kind="primary"]),
        [data-testid="stFileUploader"] button {
            background: var(--surface); border: 1px solid var(--border-strong); color: var(--brand);
        }
        div.stButton > button:not([kind="primary"]):hover {
            border-color: var(--accent); color: var(--accent); background: var(--brand-soft);
        }
        div.stButton > button:focus-visible,
        div.stDownloadButton > button:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--ring); }
        div.stButton > button:disabled,
        div.stButton > button[kind="primary"]:disabled {
            background: var(--surface-muted) !important; border: 1px solid var(--border) !important;
            color: var(--text-muted) !important; opacity: 1 !important; box-shadow: none; transform: none;
        }

        /* File uploader */
        [data-testid="stFileUploader"] { padding: 0; background: transparent; border: 0; }
        [data-testid="stFileUploader"] section {
            min-height: 7rem; border: 1.5px dashed var(--border-strong);
            border-radius: var(--radius-sm); background: var(--surface-muted);
            transition: border-color 0.16s ease, background 0.16s ease;
        }
        [data-testid="stFileUploader"] section:hover { border-color: var(--accent); background: var(--brand-soft); }
        [data-testid="stFileUploader"] section * { color: var(--text) !important; }
        /* Native selected-file chips (Streamlit stFileChip) - theme aware */
        [data-testid="stFileChips"] { margin-top: 0.75rem; display: grid; gap: 0.5rem; }
        [data-testid="stFileChip"] {
            background: var(--surface-alt) !important; border: 1px solid var(--border) !important;
            border-radius: var(--radius-sm) !important; box-shadow: var(--shadow-sm);
            padding: 0.55rem 0.7rem !important;
        }
        [data-testid="stFileChip"] * { color: var(--text) !important; }
        [data-testid="stFileChipName"] { color: var(--text) !important; font-weight: 650; }
        [data-testid="stFileChipDeleteBtn"] svg { color: var(--text-muted) !important; fill: var(--text-muted) !important; }
        [data-testid="stFileChipDeleteBtn"]:hover svg { color: var(--text) !important; fill: var(--text) !important; }

        /* Selected files */
        .selected-files { display: grid; gap: 0.55rem; margin: 0.9rem 0 0.4rem; }
        .selected-file {
            display: flex; align-items: center; gap: 0.85rem; padding: 0.8rem 0.9rem;
            border: 1px solid var(--border); border-radius: var(--radius-sm);
            background: var(--surface-alt); box-shadow: var(--shadow-sm);
        }
        .selected-file-icon {
            display: grid; place-items: center; width: 2.6rem; height: 2.6rem; border-radius: 10px;
            background: linear-gradient(180deg, var(--accent), var(--brand)); color: #ffffff;
            font-weight: 800; font-size: 0.7rem; letter-spacing: 0.03em; flex: 0 0 auto;
        }
        .selected-file-name { color: var(--text); font-weight: 700; overflow-wrap: anywhere; }
        .selected-file-size { margin-top: 0.15rem; color: var(--text-muted); font-size: 0.8rem; }

        /* Alerts + expander + status + progress */
        [data-testid="stExpander"] {
            background: var(--surface); border: 1px solid var(--border) !important;
            border-radius: var(--radius-sm); box-shadow: var(--shadow-sm);
        }
        /* The header of an OPEN expander keeps Streamlit's own light-theme
           background, because the app themes itself in CSS rather than through
           a config.toml. Against the near-white label colour set above that is
           1.06:1 — white on white — so every expanded header rendered blank.
           Collapsed headers are transparent and were always fine, which is what
           made it look like only some titles were missing. */
        [data-testid="stExpander"] details > summary,
        [data-testid="stExpander"] details[open] > summary {
            background: var(--surface-muted) !important;
            color: var(--text) !important;
        }
        [data-testid="stExpander"] details > summary * { color: var(--text) !important; }
        [data-testid="stExpander"] details > summary:hover {
            background: var(--surface-alt) !important;
        }
        [data-testid="stAlert"] {
            border: 1px solid var(--border); border-left-width: 4px; border-radius: var(--radius-sm);
        }
        [data-testid="stAlert"] * { color: var(--text) !important; }
        div[data-testid="stAlert"][kind="warning"] { background: var(--warning-bg); border-left-color: #d9a625; }
        div[data-testid="stAlert"][kind="error"] { background: var(--error-bg); border-left-color: #d64545; }
        div[data-testid="stAlert"][kind="success"] { background: var(--success-bg); border-left-color: #1f9d63; }
        div[data-testid="stAlert"][kind="info"] { background: var(--brand-soft); border-left-color: var(--accent); }
        [data-testid="stProgress"] div[role="progressbar"] > div,
        [data-testid="stProgress"] > div > div > div > div {
            background: linear-gradient(90deg, var(--accent), var(--brand));
        }

        /* Tabs.
           BaseWeb renders these as bare text in a row with a hairline
           underline, so three long labels — "Notes generation", "Daily Mains
           Practice (DMP)", "Daily Prelims Practice (DPP)" — read as one
           sentence rather than three places to go. Each is given its own
           padded box, and the row is given room to breathe under it. */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 0.5rem;
            flex-wrap: wrap;
            row-gap: 0.5rem;
            padding: 0.3rem 0 0.9rem;
            margin-bottom: 1.1rem;
            border-bottom: 1px solid var(--border);
            background: transparent;
        }
        [data-testid="stTabs"] [data-baseweb="tab"] {
            padding: 0.55rem 1.05rem !important;
            font-weight: 650;
            line-height: 1.2;
            color: var(--text-soft) !important;
            background: var(--surface-muted) !important;
            border: 1px solid var(--border) !important;
            border-radius: var(--radius-sm);
            box-shadow: var(--shadow-sm);
            transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
        }
        [data-testid="stTabs"] [data-baseweb="tab"]:hover {
            background: var(--surface-alt) !important;
            border-color: var(--border-strong) !important;
            color: var(--text) !important;
        }
        [data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {
            color: var(--accent) !important;
            background: var(--brand-soft) !important;
            border-color: var(--accent) !important;
            font-weight: 750;
        }
        [data-testid="stTabs"] [data-baseweb="tab"] p {
            font-size: 0.95rem;
            font-weight: inherit;
            color: inherit !important;
            margin: 0;
        }
        /* The sliding underline and the row rule underneath it both fight the
           boxes above; the row keeps one border of its own. */
        [data-testid="stTabs"] [data-baseweb="tab-highlight"],
        [data-testid="stTabs"] [data-baseweb="tab-border"] {
            display: none !important;
        }
        [data-testid="stTabs"] [data-baseweb="tab-panel"] {
            padding-top: 0.35rem;
        }

        hr { border-color: var(--border); }

        /* Login page */
        .auth-kicker {
            display: inline-block; margin-bottom: 0.6rem; padding: 0.25rem 0.7rem;
            font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
            color: var(--accent); background: var(--brand-soft); border: 1px solid var(--border);
            border-radius: 999px;
        }
        .auth-title { font-size: 1.7rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text); margin: 0.2rem 0 0.5rem; }
        .auth-copy { color: var(--text-soft); font-size: 0.95rem; line-height: 1.6; margin-bottom: 1.25rem; max-width: 30rem; }

        @media (max-width: 720px) {
            .app-header { align-items: flex-start; flex-direction: column; }
            .user-box { max-width: 100%; text-align: left; }
        }
        </style>
        """.replace("__THEME_VARS__", theme_vars),
        unsafe_allow_html=True,
    )
def _render_login_page() -> None:
    _render_global_styles()
    google_configured = _google_auth_configured()
    with st.container():
        st.markdown('<div class="auth-shell">', unsafe_allow_html=True)
        if LOGO_PATH:
            st.image(str(LOGO_PATH), width=120)
        _render_theme_picker("login_theme")
        st.markdown(
            """
            <div class="auth-kicker">Restricted profile login</div>
            <div class="auth-title">UPSC_Notes</div>
            <div class="auth-copy">
                Sign in with your Google account to generate detailed UPSC study notes.
                Access is limited to approved email addresses from the app allowlist.
            </div>
            """,
            unsafe_allow_html=True,
        )

        if google_configured:
            if st.button("Continue with Google", type="primary", use_container_width=True):
                try:
                    st.login("google")
                except Exception as exc:
                    st.error("Google login failed. Check `.streamlit/secrets.toml` and restart the app.")
                    st.caption(str(exc))
        else:
            st.error(
                "Google OAuth is not configured yet. Replace the placeholder "
                "`client_id` and `client_secret` in `.streamlit/secrets.toml` "
                "with real Google OAuth credentials, then restart the app."
            )

        st.caption(APP_VERSION_LABEL)
        st.markdown("</div>", unsafe_allow_html=True)


def _render_profile_panel() -> None:
    """Sidebar profile block: who is signed in, whether the proxy still says
    they're authorized, how much of the six-day session is left, and a live
    Gemini round-trip they can run on demand."""
    st.markdown("### Profile")
    st.markdown(f"[{_current_user_email()}](mailto:{_current_user_email()})")

    # Cached (5 min) — this is the same check the generate gate uses, so the
    # panel can't claim OK while a run would be denied.
    status = _proxy_access_status()
    if status == "allowed":
        st.success("Auth status: OK")
    elif status == "denied":
        st.error("Auth status: not authorized")
    else:
        st.warning("Auth status: could not reach the PW proxy")

    days = _session_days_left()
    if days is not None:
        st.caption(
            f"Session valid for {days} more day{'s' if days != 1 else ''}. "
            "Signing in again isn't needed until then."
        )
    if drive_auth.is_connected(_current_user_email()):
        st.caption("Google Drive: connected. Class recordings your PW account can open "
                   "will open here too.")
    else:
        st.caption("Google Drive: not connected. To use class recordings shared only "
                   "inside PW, sign out, sign in again, and allow Drive access when "
                   "Google asks.")

    if st.button("Sign out", key="sidebar_sign_out", use_container_width=True):
        _logout_user()

    if st.button("Test AI connection", key="sidebar_test_gemini", use_container_width=True):
        _run_gemini_selftest()

    st.divider()
    st.caption(BUILD_LABEL)


def _session_days_left() -> int | None:
    try:
        return session_days_remaining(st.user)
    except Exception:
        return None


def _run_gemini_selftest() -> None:
    """One tiny round-trip through the proxy, shown inline in the sidebar.

    Gated by check_allowed first: this is a paid provider call outside
    run_pipeline, and PW onboarding requires the live whitelist check before
    every paid action, not just at login.
    """
    token = _google_proxy_token()
    with st.spinner(f"Calling {DEFAULT_MODEL} through the PW proxy…"):
        try:
            if not pw_access.check_allowed(token):
                st.error("Not authorized for this app right now — the proxy denied the check.")
                return
            # Its own task id: this is a one-off diagnostic, not part of a
            # notes run, and the ledger row should say so rather than being
            # mistaken for real note-generation spend. No fallback: this
            # button exists to show whether the configured model itself works.
            client = GeminiClient(
                token,
                model=DEFAULT_MODEL,
                fallback_model="",
                task_id=pw_access.new_task_id("connection-test"),
                filename="(connection test)",
                input_unit="No. of pages",
                count=0,
            )
            resp = client.test_connection()
        except GeminiError as e:
            st.error(f"AI connection test failed: {e}")
            return
        except Exception as e:  # network/token problems shouldn't crash the app
            st.error(f"AI connection test failed: {e}")
            return
    st.success(f"AI connection OK — replied via {resp.model}")
    with st.expander("Raw text reply"):
        st.code(json.dumps((resp.raw or {}).get("_gpt_raw") or resp.raw, indent=2, default=str)[:3000])


def _require_allowed_google_user() -> None:
    is_logged_in = bool(getattr(st.user, "is_logged_in", False))
    if not is_logged_in:
        _render_login_page()
        st.stop()

    if _auth_session_expired():
        _logout_user()
        st.stop()

    # Step 1 - identity: must be a @pw.live Google account (fast, friendly check).
    email = _current_user_email()
    if not email or not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        _render_global_styles()
        st.error(f"Please sign in with your @{ALLOWED_EMAIL_DOMAIN} Google account.")
        if email:
            st.caption(f"Signed in as {email}")
        if st.button("Sign out", use_container_width=False):
            _logout_user()
        st.stop()

    # Step 2 - authorization: the PW proxy's "Whitelisted" sheet is the single
    # source of truth. Only users listed under this app's column may log in;
    # everyone else is stopped here, before they can see the app. Fails closed
    # if the proxy is unreachable. (Result is cached for a few minutes so this
    # doesn't call the proxy on every rerun.)
    status = _proxy_access_status()
    if status != "allowed":
        _render_global_styles()
        if status == "denied":
            st.error(
                "This account isn't authorized for this app. Ask an admin to add "
                f"your email to the '{PROXY_APP_NAME}' column of the Whitelisted sheet."
            )
        else:  # "error" - proxy unreachable / token problem
            st.error(
                "Couldn't verify your access with the PW proxy (it may be "
                "temporarily unreachable, or your sign-in may have lapsed). "
                "Try again in a moment, or sign in again."
            )
        st.caption(f"Signed in as {email}")
        cols = st.columns(2)
        with cols[0]:
            # The only case that still needs a fresh Google token is a failed
            # session-pass mint (e.g. the app restarted after the cookie's token
            # had already expired). Offer it on a click — never automatically.
            if status == "error" and st.button("Sign in again", type="primary", use_container_width=True):
                st.login("google")
        with cols[1]:
            if st.button("Sign out", use_container_width=True):
                _logout_user()
        st.stop()


if "ui_theme" not in st.session_state:
    st.session_state.ui_theme = "Dark"
_require_allowed_google_user()
_render_global_styles()
user_email = _current_user_email()
user_initial = (user_email[:1] or "U").upper()
logo_html = ""
if LOGO_PATH:
    logo_data = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    logo_html = f'<img class="app-brand-logo" src="data:image/png;base64,{logo_data}" alt="UPSC_Notes logo">'

with st.sidebar:
    if LOGO_PATH:
        st.image(str(LOGO_PATH), width=92)
    _render_theme_picker("sidebar_theme")
    _render_profile_panel()

# NOTE: keep this HTML flush-left. Markdown treats a 4+ space indented line as a
# code block, so when the logo is absent (logo_html == "") an indented block would
# render as raw HTML on the page instead of the header.
st.markdown(
    f"""<div class="app-header">
<div class="app-brand">
{logo_html.replace("app-brand-logo", "app-logo")}
<div>
<div class="app-title-row"><div class="app-title">UPSC Notes Automation</div><span class="app-version">Version: v{APP_VERSION}</span></div>
<div class="app-subtitle">Date: {_STAMP["date"]} &nbsp;·&nbsp; Time: {_STAMP["time"]}</div>
</div>
</div>
<div class="user-box">Signed in as<b>{user_email}</b></div>
</div>""",
    unsafe_allow_html=True,
)
# ---- Configuration ---------------------------------------------------------
# Notes language is chosen in the upload panel below; everything else is fixed.
# Label -> value understood by run_pipeline (see src/config.LANGUAGE_CODES).
#
# The two explicit choices come first and Auto last, because Auto is the one
# that can surprise you: it follows the language the class was *taught* in, so a
# Hindi-medium class with English slides produces Hindi notes. That is right
# often enough to keep offering, and wrong often enough not to default to.
#: Note languages, in the order they are offered. There is deliberately NO
#: "Auto (match the class)" option any more: the output language is now the
#: user's decision and is independent of the language the class was taught in,
#: which is the whole point of being able to ask a Hindi class for Marathi
#: notes. The pipeline still accepts "auto" for any non-UI caller.
LANGUAGE_CHOICES = {
    "English": "English",
    "हिंदी / Hindi": "Hindi",
    "मराठी / Marathi": "Marathi",
}
#: Shown first and selected by nothing. Streamlit's radio always has a
#: selection, so the empty choice has to be a real option rather than an absent
#: default — otherwise whichever language sits at index 0 becomes a silent
#: default, and a run in the wrong language is invisible until the notes exist.
LANGUAGE_PROMPT = "— Select —"
MODE = "complete"           # detailed Prelims+Mains notes (use "summary" for revision)
# This build only ships the UPSC workflow; the constant is what the format /
# layout controls below are gated on, so a future workflow picker only has to
# set it rather than move any UI.
WORKFLOW = "upsc"
SUBJECT = "auto"            # auto-detected from the file
SEND_IMAGES = True
STRICT_FILTER = True
DTP_POLICY = "hide_note_insert_image"
IMAGE_MODE = "smart_crop"
AI_REDRAW = False           # keep original maps/diagrams; no handwritten redraw for clean typed notes
IMAGE_MODEL = DEFAULT_IMAGE_MODEL

if "results" not in st.session_state:
    st.session_state.results = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0


def _default_dcn_logo_path() -> str | None:
    """The logo used for DCN output when the user uploads none.

    Asks the writer rather than naming a file. This path is passed to the
    pipeline as an explicit logo_path, which outranks the writer's own fallback
    — so hardcoding assets/pw_logo.png here put the bare PW mark in the header
    even once the full OnlyIAS lockup was installed beside it.
    """
    from src.upsc_dcn_docx_writer import default_logo_path

    logo = default_logo_path()
    return str(logo) if logo else None


def _persist_upload(uploaded, prefix: str) -> str | None:
    """Write an uploaded file to a stable, session-scoped temp path.

    The pipeline needs a real path on disk, and Streamlit re-runs this script on
    every widget change — so the bytes go into one directory per session that is
    rewritten in place, instead of a new temp file per rerun that would pile up
    (or a context-managed one that would already be deleted by run time).
    """
    if uploaded is None:
        return None
    holder = st.session_state.get("_upload_tmpdir")
    if not holder or not Path(holder).exists():
        holder = tempfile.mkdtemp(prefix="upsc_notes_uploads_")
        st.session_state["_upload_tmpdir"] = holder
    # Path(...).name strips any directory part a crafted filename might carry.
    target = Path(holder) / f"{prefix}_{Path(uploaded.name).name}"
    target.write_bytes(uploaded.getbuffer())
    return str(target)


def _render_check_info(result) -> dict:
    """Normalise the pipeline's render-verification outcome for the UI.

    The check is optional (it needs a working DOCX→PDF renderer and the user can
    switch it off), so "did not run" has to stay distinguishable from "ran and
    was clean" — the card shows an em-dash for the former and never invents a
    status. Accepts either a RenderCheck object or its dict form.
    """
    raw = getattr(result, "render_check", None)
    if raw is None:
        # The pipeline stores it in run_metadata under "dcn_render_check";
        # "render_check" is accepted too so either spelling works.
        meta = result.metadata or {}
        raw = meta.get("dcn_render_check") or meta.get("render_check")
    if raw is None:
        return {"ran": False}
    if not isinstance(raw, dict):
        raw = {
            "ok": getattr(raw, "ok", False),
            "page_count": getattr(raw, "page_count", 0),
            "overlap_pages": getattr(raw, "overlap_pages", []),
            "warnings": getattr(raw, "warnings", []),
        }
    overlap: list[int] = []
    for page in raw.get("overlap_pages") or []:
        try:
            overlap.append(int(page))
        except (TypeError, ValueError):
            continue
    try:
        page_count = int(raw.get("page_count") or 0)
    except (TypeError, ValueError):
        page_count = 0
    return {
        "ran": True,
        "ok": bool(raw.get("ok")),
        "page_count": page_count,
        "overlap_pages": overlap,
        "warnings": [str(w) for w in (raw.get("warnings") or [])],
    }


def _result_to_dict(name: str, result, output_format: str = "") -> dict:
    notes = ""
    if result.raw_notes_path and result.raw_notes_path.exists():
        notes = result.raw_notes_path.read_text(encoding="utf-8")
    return {
        "name": name,
        "output_format": output_format,
        # Render verification of the finished DOCX (DCN format only). Stored as
        # plain data so the results card never has to touch the pipeline again.
        "render": _render_check_info(result),
        "run_dir": str(result.run_dir),
        "docx": str(result.docx_path) if result.docx_path else None,
        "pdf": str(result.pdf_path) if result.pdf_path else None,
        "zip": str(result.zip_path) if result.zip_path else None,
        "notes": notes,
        "warnings": list(result.warnings or []),
        # UPSC workflow artefacts. Stored as paths/plain data so every download
        # button below reads session state instead of rerunning the pipeline.
        "quality_docx": str(result.quality_report_docx_path) if result.quality_report_docx_path else None,
        "quality_md": str(result.quality_report_md_path) if result.quality_report_md_path else None,
        "quality_summary": dict(result.quality_summary or {}),
        "error_docx": str(result.error_report_docx_path) if result.error_report_docx_path else None,
        "error_md": str(result.error_report_md_path) if result.error_report_md_path else None,
        "error_fixed_docx": str(result.error_fixed_report_docx_path) if result.error_fixed_report_docx_path else None,
        "error_fixed_md": str(result.error_fixed_report_md_path) if result.error_fixed_report_md_path else None,
        "qc_summary": dict(result.qc_summary or {}),
        "transcript_gaps": sum(1 for i in (result.transcript_issues or []) if i.time_range),
        # Error Report XLSX — the only report a normal user sees.
        "error_xlsx": str(result.error_report_xlsx_path) if result.error_report_xlsx_path else None,
        # The same findings with their source locations — the report a reviewer
        # actually checks against the lecture.
        "error_detailed": (str(result.error_report_detailed_path)
                           if result.error_report_detailed_path else None),
        "error_counts": dict(result.error_report_summary or {}),
        "error_status": result.error_report_status or "",
        # Facts about the finished document, for the notices below. Taken as
        # DATA rather than by searching `warnings` for phrases: the warning
        # wording changes, and a results card that reads prose to decide what
        # to show is a card that goes quietly wrong when someone edits a
        # sentence in the pipeline.
        "note_length": dict((result.metadata or {}).get("note_length") or {}),
        "visuals": {
            "found": (result.metadata or {}).get("visuals_found") or 0,
            # The count BEFORE placement. `visuals_selected` is measured after
            # the section mapper has deselected everything it could not place,
            # so a run that chose 13 diagrams and placed none reports 0 there —
            # and "0 selected, 0 inserted" is indistinguishable from a lecture
            # with no diagrams at all.
            "selected": ((result.metadata or {}).get("visuals_selected_for_placement")
                         or (result.metadata or {}).get("visuals_selected") or 0),
            "inserted": (result.metadata or {}).get("visuals_inserted") or 0,
        },
        # Why a missing output is missing, straight from the run.
        "pdf_reason": getattr(result, "pdf_error", "") or "",
        "error_report_reason": getattr(result, "error_report_error", "") or "",
        "tracking": None,
        "error": None,
    }


def _notes_to_markdown(notes: str) -> str:
    out: list[str] = []
    for raw in notes.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        leading = len(raw) - len(raw.lstrip(" \t"))
        level = min(3, leading // 4)
        content = line.strip()
        bullet_m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", content)
        if bullet_m:
            out.append("  " * level + "- " + bullet_m.group(2).strip())
        elif content[0].isdigit() and content[1:2] in {".", ")"}:
            out.append("  " * level + content)
        elif "note to dtp" in content.lower():
            continue  # hidden in output, so hide in preview too
        else:
            out.append(content)
    return "\n".join(out)


def _format_file_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"


def _render_selected_files(files) -> None:
    if not files:
        return
    rows = []
    for uploaded in files:
        name = html.escape(uploaded.name)
        size = html.escape(_format_file_size(uploaded.size))
        suffix = html.escape(Path(uploaded.name).suffix.replace(".", "").upper() or "FILE")
        rows.append(
            f"""
            <div class="selected-file">
                <div class="selected-file-icon">{suffix[:4]}</div>
                <div>
                    <div class="selected-file-name">{name}</div>
                    <div class="selected-file-size">{size}</div>
                </div>
            </div>
            """
        )
    st.markdown('<div class="selected-files">' + "".join(rows) + "</div>", unsafe_allow_html=True)


#: Technical failures, mapped to what the user can actually do about them.
#: The exception text names models, endpoints and file paths; it belongs in the
#: run folder, not on the page.
_FRIENDLY_ERRORS = (
    ("unsupported file type", "That file type is not supported. Please upload a PPTX or PDF."),
    ("no slides", "No readable pages were found in that file. Please check the upload."),
    ("not authorized", "Your account is not authorised for this app yet."),
    ("permissionerror", "Your account is not authorised for this app yet."),
    ("quota", "The service is busy right now. Please try again in a few minutes."),
    ("timed out", "That took too long and was stopped. Please try again."),
    ("timeout", "That took too long and was stopped. Please try again."),
)


def _friendly_error(exc: Exception) -> str:
    """A short, actionable sentence for the page.

    This is the headline only. It never replaces the reason — see
    _error_details, which every caller stores alongside it. A message that says
    "please try again" and nothing else is unactionable when the real answer is
    "ffmpeg is not on PATH", and it wastes the one moment the user is looking.
    """
    # Ingestion errors already carry the sentence written for this exact
    # failure — an invalid link, a private Drive file, a video with no audio.
    # Matching them against the generic table would replace a specific, true
    # message with a vaguer one.
    if isinstance(exc, VideoSourceError):
        return exc.user_message
    text = str(exc).lower()
    for needle, message in _FRIENDLY_ERRORS:
        if needle in text:
            return message
    return "The notes could not be generated for this file."


def _error_details(exc: Exception) -> dict:
    """The actual cause, plus the traceback, ready to store on the result."""
    reason = str(exc).strip() or exc.__class__.__name__
    return {
        "error_reason": f"{exc.__class__.__name__}: {reason}",
        "error_trace": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _missing_file_reason(r: dict, kind: str) -> str:
    """Why an output the user expected is not in the run.

    Read from fields the pipeline sets deliberately, not searched for in its
    `warnings` prose. The first version of this did search, and matching a PDF
    failure on the substring "word" picked out "Transcript source: YouTube
    captions (hi, auto-generated) (14,663 words)" — a confident, wrong answer,
    which is worse than the silence it replaced.
    """
    if kind == "pdf":
        return r.get("pdf_reason") or ""
    if kind == "xlsx":
        detail = r.get("error_report_reason") or ""
        summary = _XLSX_STATUS_REASONS.get(r.get("error_status") or "", "")
        return f"{summary} {detail}".strip() if summary else detail
    return ""


#: Why the Error Report XLSX is missing, by the status the pipeline recorded.
_XLSX_STATUS_REASONS = {
    "failed": "The sheet could not be written.",
    "missing": "The AI's answer carried no error-report section, so there was "
               "nothing to fill the sheet with.",
    "parse_failed": "The AI's error report was not valid JSON.",
    "disabled": "The error report was switched off for this run.",
    "pending": "The run ended before the error report was built.",
}


def _download_from_path(
    label: str, path: str | None, key: str, missing: str, reason: str = "",
) -> None:
    """Download button backed by a path stored in st.session_state.

    The bytes are read from the finished run folder when the card renders, so a
    click only re-serves the existing file — it can never re-enter run_pipeline.

    When the file is not there, `missing` says what is missing and `reason` says
    why. The why is not optional in practice: "PDF could not be created on this
    machine" is the same sentence whether Word is missing, Word crashed, or the
    machine is not Windows, and only one of those is worth acting on.
    """
    if path and Path(path).exists():
        st.download_button(
            label,
            Path(path).read_bytes(),
            file_name=Path(path).name,
            key=key,
            use_container_width=True,
        )
        return
    st.caption(missing)
    if reason:
        st.caption(f"Reason: {reason}")


#: Reports the pipeline still writes but no normal user is shown. Each entry is
#: (result key, button label). They live in the run folder either way; developer
#: mode only decides whether the app offers them.
_DEVELOPER_DOWNLOADS = (
    ("quality_docx", "Quality Report DOCX"),
    ("quality_md", "Quality Report MD"),
    ("error_docx", "Error Report DOCX (legacy)"),
    ("error_md", "Error Report MD (legacy)"),
    ("error_fixed_docx", "Error Fixed Report DOCX"),
    ("error_fixed_md", "Error Fixed Report MD"),
    ("zip", "Full Run ZIP"),
)


def _render_error_report_summary(r: dict) -> None:
    """The four counts, and an honest line when there is nothing to count.

    A status of no_issues is a real result and says so. missing / parse_failed /
    failed are reported as such rather than as "0 issues", because a reviewer
    who reads "0 issues" stops looking.
    """
    status = r.get("error_status") or ""
    if status == "disabled":
        st.caption("Error report: switched off for this run.")
        return
    if status in {"missing", "parse_failed", "failed"}:
        st.warning(
            "Error report: the AI did not return a usable report for this file, so the "
            "sheet records that instead of a list of errors. It is **not** a statement "
            "that the lecture was error-free — re-run the file, or review it by hand."
        )
        return
    counts = r.get("error_counts") or {}
    st.markdown("**Error Report**")
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Total issues", counts.get("total", 0))
    e2.metric("High", counts.get("high", 0))
    e3.metric("Medium", counts.get("medium", 0))
    e4.metric("Low", counts.get("low", 0))
    if status == "no_issues":
        st.caption(
            "No reportable error was found at this strictness. The sheet still "
            "downloads, with one row saying so."
        )
        return

    # How far each finding can be trusted, and how fast it can be checked. Both
    # numbers were computed on every run and shown to nobody: they lived in
    # JSON files in the run folder, and the card said only how many issues there
    # were — which is the least useful of the three.
    total = int(counts.get("total") or 0)
    if not total:
        return

    notes: list[str] = []
    located = counts.get("located")
    if located is not None:
        against = counts.get("locatable_against") or ""
        where = {"recording": "a timestamp in the recording",
                 "transcript": "a numbered place in the transcript"}.get(against)
        if where:
            notes.append(
                f"**{located} of {total}** traced to {where} — see the "
                f"*with locations* sheet.")
        else:
            notes.append(
                "None could be traced to a place in the lecture: this run had no "
                "transcript text or slides to search. That is a missing source, "
                "not a failed search.")

    if counts.get("verified"):
        demoted = int(counts.get("verify_demoted") or 0)
        checked = int(counts.get("verify_checked") or 0)
        if demoted:
            notes.append(
                f"**{demoted} of {checked}** could not be supported on a second, "
                f"independent check and were downgraded to Low, with the doubt "
                f"written into the row.")
        else:
            notes.append(
                f"All {checked} checkable finding(s) held up on a second, "
                f"independent check.")

    for note in notes:
        st.caption(note)


def _run_notices(r: dict) -> list[str]:
    """The few things about a finished run a normal user can act on.

    The full warnings list is developer-only, and deliberately: a run produces
    a dozen lines about chunk counts and transcript sources, and printing them
    on the results card recreates the wall of text the three-button page exists
    to remove.

    But a handful of them are not developer detail — they are the document not
    being what the user was promised, and hiding those is the opposite problem.
    A run that could not hold the page limit, or that found thirteen diagrams
    and inserted none, currently looks identical to a run that did both.

    Built from structured metadata, never by searching the warning prose.
    """
    notices: list[str] = []

    length = r.get("note_length") or {}
    limit = length.get("page_limit")
    pages = length.get("final_pages")
    if length.get("page_limit_enforced") is False and limit:
        notices.append(
            f"**The {limit}-page limit could not be checked.** The notes could not "
            f"be rendered to a PDF on this machine, and the page count is measured "
            f"from that render — so this document was written at its full length. "
            f"The PDF export error above explains why page limiting could not run.")
    elif pages and limit and pages > limit:
        notices.append(
            f"**These notes are {pages} pages, over the {limit}-page limit.** They "
            f"were compressed as far as could be done safely; shortening them "
            f"further would have dropped what the class covered last.")

    visuals = r.get("visuals") or {}
    selected, inserted = visuals.get("selected") or 0, visuals.get("inserted") or 0
    if selected and not inserted:
        notices.append(
            f"**None of the {selected} slide diagram(s) could be placed in the "
            f"notes.** Each one was left out rather than dropped into a section it "
            f"does not belong to. The slides still have them.")
    elif selected and inserted < selected:
        notices.append(
            f"**{inserted} of {selected} slide diagram(s) were placed in the "
            f"notes.** The rest matched no section here and were left out rather "
            f"than put somewhere they do not belong.")

    if r.get("pdf_reason"):
        notices.append(f"**The PDF could not be created.** {r['pdf_reason']}")

    return notices


def _render_run_notices(r: dict) -> None:
    """The notices above, or nothing at all when there are none."""
    notices = _run_notices(r)
    if not notices:
        return
    with st.expander(f"About this document ({len(notices)})", expanded=True):
        for notice in notices:
            st.markdown(f"- {notice}")


def _render_developer_downloads(r: dict, idx: int) -> None:
    """Everything else the run produced, behind the developer-mode flag.

    The caller decides whether developer mode is on — it resolves the env var
    *and* the ?dev=1 URL parameter, so gating again on the env var alone here
    would silently disable the URL switch.
    """
    available = [
        (key, label) for key, label in _DEVELOPER_DOWNLOADS
        if r.get(key) and Path(r[key]).exists()
    ]
    with st.expander(f"Developer files ({len(available)})", expanded=False):
        st.caption(
            "Not shown to normal users. The run folder holds these either way: "
            f"`{r.get('run_dir') or 'unknown'}`"
        )
        if not available:
            st.caption("This run produced none of them.")
            return
        cols = st.columns(3)
        for i, (key, label) in enumerate(available):
            with cols[i % 3]:
                _download_from_path(label, r[key], f"dev_{key}_{idx}", "")


# ---------------------------------------------------------------------------
# Daily Mains Practice
# ---------------------------------------------------------------------------
# One panel under the downloads: generate questions, tick the ones you want,
# get answers for those. Nothing about the model, the temperature or the retry
# count appears here, and nothing here can re-enter run_pipeline.
#
# The panel holds ids in session state, never the session object. Every render
# reloads it from disk, which costs one small JSON read and buys the whole
# "come back tomorrow" story for free — a browser refresh, a Streamlit rerun and
# a restarted server all land in the same place.


def _dmp_state_key(idx) -> str:
    return f"dmp_open_session_{idx}"


def _dmp_client():
    """The app's own Gemini client, built for one practice request.

    Same class, same proxy, same fallback and backoff as the notes pipeline —
    only the task_id differs, so the proxy's usage ledger can tell practice
    generation apart from note generation.
    """
    from src.ai_client import GeminiClient

    token = _google_proxy_token()
    if not token:
        return None
    return GeminiClient(
        google_token=token, model=DEFAULT_MODEL,
        task_id=pw_access.new_task_id("upsc-dmp"), input_unit="question",
    )


def _dmp_pick_key(scope, session_id: str, question_id: str) -> str:
    """The tick box for one question, in one copy of the panel.

    ``scope`` is what keeps the copies apart. The same practice set is on the
    page twice whenever the lecture that just finished is also the one the
    library picker opens on — the results card and the library each draw the
    whole set, and without the scope both would claim this key for the same
    question and take the page down before either was drawn.
    """
    return f"dmp_pick_{scope}_{session_id}_{question_id}"


def _dmp_selected_ids(session, scope) -> list[str]:
    return [q.id for q in session.questions
            if st.session_state.get(
                _dmp_pick_key(scope, session.dmp_session_id, q.id))]


def _dmp_unanswered_selected_ids(session, selected_ids: list[str]) -> list[str]:
    """Selected questions that still need an answer generated.

    The checkboxes also control the question-paper download.  Keeping a pick
    after its answer is generated lets a teacher download exactly that same
    selection, while this filter prevents a second click from purchasing an
    answer that already exists.
    """
    selected = set(selected_ids)
    return [q.id for q in session.questions if q.id in selected and not q.answered]


def _render_dmp_questions(session, lang: str, scope) -> None:
    """The three levels, each collapsible, each question a checkbox."""
    from src.config import DMP_LEVELS
    from src.dmp import service as dmp_service
    from src.dmp.answer_parse import preview_markdown as answer_preview_markdown
    from src.dmp.house_docx import ANSWER_LABEL, APPROACH_HEADING
    from src.dmp.labels import level_label, text

    number = 0
    for level in DMP_LEVELS:
        questions = session.level_questions(level)
        if not questions:
            continue
        answered = sum(1 for q in questions if q.answered)
        heading = text("level_progress", lang, level=level_label(level, lang),
                       answered=answered, total=len(questions))
        with st.expander(heading, expanded=(level == DMP_LEVELS[0])):
            for question in questions:
                number += 1
                label = f"**Q{number}.** ({question.marks}) {question.text}"
                if question.answered:
                    # A completed question remains selectable so the teacher
                    # can download a paper containing exactly these questions.
                    st.checkbox(
                        f"{label} &nbsp; :green[**{text('answered_badge', lang)}**]",
                        key=_dmp_pick_key(
                            scope, session.dmp_session_id, question.id))
                    # Said here, beside the answer, because the sheet prints
                    # it in full: nothing cuts a long answer down.
                    overrun = dmp_service.answer_overrun(
                        question.answer or "", question.marks)
                    if overrun:
                        st.warning(text("over_limit", lang, words=overrun[0],
                                        limit=overrun[1], marks=question.marks))
                    with st.expander(text("model_answer", lang), expanded=False):
                        # As the sheet prints it: the raw text carries section
                        # markers the sheet never shows.
                        st.markdown(answer_preview_markdown(
                            question.answer or "",
                            approach_title=APPROACH_HEADING.get(lang, APPROACH_HEADING["en"]),
                            answer_title=ANSWER_LABEL.get(lang, ANSWER_LABEL["en"])))
                else:
                    st.checkbox(
                        label, key=_dmp_pick_key(
                            scope, session.dmp_session_id, question.id))
                st.divider()


def _render_dmp_sheet_labels(session, idx, lang: str,
                             runs_dir: Path | None = None) -> dict[str, str]:
    """The subject, topic and DMP No. the downloaded sheets print.

    Prefilled the way the DPP builder prefills its subject — from the
    lecture's own subject line — with the lecture title as the topic and the
    lecture's saved DMP No. Each can be overwritten. A blank subject prints
    "Daily Mains Practice DMP <No>", and a blank topic drops the cream box.
    """
    from src.dmp import export as dmp_export
    from src.dmp.labels import text

    defaults = None
    values: dict[str, str] = {}
    fields = (("subject", "sheet_subject", "Society, Social Issues, Social Justice"),
              ("topic", "sheet_topic", "Urbanization (Part 03)"),
              ("dmp_no", "sheet_number", "19"))
    for col, (name, label_key, placeholder) in zip(st.columns([4, 4, 1]), fields):
        key = f"dmp_print_{name}_{idx}"
        if key not in st.session_state:
            if defaults is None:
                defaults = dmp_export.sheet_defaults(session, runs_dir)
            st.session_state[key] = defaults.get(name) or ""
        with col:
            values[name] = " ".join(str(st.text_input(
                text(label_key, lang), key=key, placeholder=placeholder) or "").split())
    return values


def _render_dmp_downloads(session, note_id: str, idx, lang: str,
                          selected_ids: list[str],
                          runs_dir: Path | None = None) -> None:
    """The question paper, and the answer sheet once something is answered.

    Rendered through the notes' own DOCX writer, so the sheets carry the same
    fonts, margins and page numbers as the notes they came from.
    """
    from src.dmp import export as dmp_export
    from src.dmp import store as dmp_store
    from src.dmp.labels import text

    kinds = [("questions", text("download_questions", lang))]
    if session.answered_count:
        kinds.append(("answers", text("download_answers", lang)))

    printed = _render_dmp_sheet_labels(session, idx, lang, runs_dir)
    cols = st.columns(len(kinds))
    for col, (kind, label) in zip(cols, kinds):
        with col:
            if not st.button(
                label, key=f"dmp_build_{kind}_{idx}",
                disabled=(kind == "questions" and not selected_ids),
                use_container_width=True,
            ):
                continue
            try:
                out_dir = dmp_store.dmp_dir(note_id, runs_dir) / "sheets"
                docx_path, pdf_path, _warnings = dmp_export.export(
                    session, kind, out_dir, runs_dir=runs_dir,
                    selected_ids=selected_ids if kind == "questions" else None,
                    subject=printed["subject"], topic=printed["topic"],
                    dmp_no=printed["dmp_no"])
            except Exception as exc:  # noqa: BLE001 — a download is never worth a crash
                st.error(text("sheet_failed", lang))
                st.caption(f"Reason: {exc}")
                continue
            st.session_state[f"dmp_sheet_{kind}_{idx}"] = {
                "docx": str(docx_path) if docx_path else None,
                "pdf": str(pdf_path) if pdf_path else None,
                "selected_ids": list(selected_ids) if kind == "questions" else None,
                "printed": dict(printed),
            }

    for kind, _label in kinds:
        built = st.session_state.get(f"dmp_sheet_{kind}_{idx}") or {}
        if kind == "questions" and built.get("selected_ids") != list(selected_ids):
            # Do not leave a prior selection available after the user has
            # changed the question ticks.
            continue
        if built.get("printed", printed) != printed:
            # Nor a sheet whose title or topic no longer says what the boxes
            # above it say.
            continue
        if not built:
            continue
        b1, b2 = st.columns(2)
        with b1:
            _download_from_path(
                f"{kind.title()} DOCX", built.get("docx"), f"dmp_dl_{kind}_{idx}",
                "The sheet could not be created.")
        with b2:
            _download_from_path(
                f"{kind.title()} PDF", built.get("pdf"), f"dmp_dlpdf_{kind}_{idx}",
                "PDF export failed — the DOCX is still available.")


def _render_dmp_body(note_id: str, idx, lang: str,
                     runs_dir: Path | None = None) -> None:
    """The panel's contents, once we know there are notes to practise on.

    Split out from the expander so the same body serves the results card and the
    standalone picker. The two differ only in how the note was chosen.
    """
    from src.dmp import service as dmp_service
    from src.dmp.labels import text

    st.caption(text("intro", lang))

    rows = dmp_service.list_sessions(note_id, runs_dir)
    state_key = _dmp_state_key(idx)
    if rows and not st.session_state.get(state_key):
        st.session_state[state_key] = rows[0]["dmp_session_id"]
    if len(rows) > 1:
        labels = {
            row["dmp_session_id"]: text(
                "history_row", lang,
                when=str(row.get("created_at") or "")[:16].replace("T", " "),
                answered=row.get("answered", 0), total=row.get("total", 0))
            for row in rows
        }
        chosen = st.selectbox(
            text("history", lang), list(labels), format_func=labels.get,
            key=f"dmp_history_{idx}",
            index=list(labels).index(st.session_state[state_key])
            if st.session_state.get(state_key) in labels else 0,
        )
        st.session_state[state_key] = chosen

    session = None
    if st.session_state.get(state_key):
        session = dmp_service.get_session(
            note_id, st.session_state[state_key], runs_dir)

    # The set's own language wins over the note's: it is what the questions were
    # actually written in, and a set generated before a language change should
    # still be labelled in the language a reader is looking at.
    if session is not None:
        lang = session.language or lang

    if session is None:
        if st.button(text("generate_questions", lang), key=f"dmp_gen_{idx}",
                     type="primary", use_container_width=True):
            client = _dmp_client()
            if client is None:
                st.error(text("sign_in_questions", lang))
                return
            started = time.monotonic()
            with st.spinner(text("spinner_questions", lang)):
                try:
                    session = dmp_service.generate_questions(
                        note_id, client, runs_dir=runs_dir)
                except dmp_service.DmpError as exc:
                    st.error(str(exc))
                    return
                except Exception:  # noqa: BLE001
                    st.error(text("failed_questions", lang))
                    return
            # Parked in session state because the rerun below throws away every
            # local the measurement lives in.
            st.session_state[f"dmp_questions_seconds_{note_id}"] = (
                time.monotonic() - started)
            st.session_state[state_key] = session.dmp_session_id
            st.rerun()
        return

    st.markdown(text("counts", lang, total=len(session.questions),
                     answered=session.answered_count, pending=session.pending_count))
    for state_suffix, label_key in (("questions", "questions_time"),
                                    ("answers", "answers_time")):
        measured = st.session_state.get(f"dmp_{state_suffix}_seconds_{note_id}")
        if measured is not None:
            st.caption(text(label_key, lang, duration=format_duration(measured)))
    _render_dmp_questions(session, lang, idx)

    selected = _dmp_selected_ids(session, idx)
    unanswered_selected = _dmp_unanswered_selected_ids(session, selected)
    a1, a2 = st.columns([3, 2])
    with a1:
        answer_clicked = st.button(
            text("generate_answers", lang, n=len(unanswered_selected)),
            key=f"dmp_answer_{idx}", type="primary",
            disabled=not unanswered_selected, use_container_width=True)
    with a2:
        new_clicked = st.button(text("new_set", lang), key=f"dmp_new_{idx}",
                                use_container_width=True)

    if answer_clicked and unanswered_selected:
        client = _dmp_client()
        if client is None:
            st.error(text("sign_in_answers", lang))
            return
        started = time.monotonic()
        with st.spinner(text("spinner_answers", lang, n=len(unanswered_selected))):
            try:
                session = dmp_service.generate_answers(
                    note_id, session.dmp_session_id, unanswered_selected, client,
                    runs_dir=runs_dir)
            except dmp_service.DmpError as exc:
                st.error(str(exc))
                return
            except Exception:  # noqa: BLE001
                st.error(text("failed_answers", lang))
                return
        st.session_state[f"dmp_answers_seconds_{note_id}"] = (
            time.monotonic() - started)
        st.rerun()

    if new_clicked:
        # Deliberately does not generate: it clears the selection so the
        # button above appears. A "new set" button that spends three model
        # calls on a mis-click is a button that gets mis-clicked.
        st.session_state.pop(state_key, None)
        st.session_state.pop(f"dmp_questions_seconds_{note_id}", None)
        st.session_state.pop(f"dmp_answers_seconds_{note_id}", None)
        st.rerun()

    if not selected and session.pending_count:
        st.caption(text("tick_hint", lang))

    _render_dmp_downloads(session, note_id, idx, lang, selected, runs_dir)


def _render_dmp_panel(r: dict, idx, *, embedded: bool = False) -> None:
    """Practice questions from the notes this run just produced.

    ``embedded`` drops the expander: inside the practice tabs on the results
    card the panel is already behind one click, and a second one to open an
    expander inside the tab is a click that says nothing.
    """
    from src.dmp import service as dmp_service
    from src.dmp.labels import text

    note_id = dmp_service.note_id_from_run_dir(r.get("run_dir"))
    lang = "en"
    if note_id:
        try:
            lang = dmp_service.note_context(note_id)["language"]
        except Exception:  # noqa: BLE001 — a label is never worth a crash
            lang = "en"
    shell = (nullcontext() if embedded
             else st.expander(text("panel_title", lang), expanded=False))
    with shell:
        if not note_id or not dmp_service.has_notes(note_id):
            st.caption(text("no_notes", lang))
            return
        _render_dmp_body(note_id, idx, lang)


def _render_dmp_library() -> None:
    """Practice on a lecture generated earlier, without generating anything new.

    Someone who wants only questions — a teacher setting a test, a student
    revising last week's class — should not have to spend a full pipeline run to
    reach them. The notes are already on disk; this just points the same panel
    at one of them.
    """
    from src.dmp import service as dmp_service

    notes = dmp_service.list_notes()
    if not notes:
        return
    with st.expander("Practice questions from an earlier lecture", expanded=False):
        st.caption(
            "Pick a lecture you have already generated notes for. Nothing is "
            "regenerated — the questions are written from the notes on disk, in "
            "the language those notes were written in.")
        # Ids, not dicts — see the note in _render_dpp_library. A dict-valued
        # selectbox silently re-pointed itself at a different lecture on the
        # next rerun, which here would mean generating questions for a class
        # the user was not looking at.
        by_id = {note["note_id"]: note for note in notes}
        labels = dmp_service.note_labels(notes)
        chosen_id = st.selectbox(
            "Lecture", list(by_id), format_func=labels.get,
            key="dmp_library_pick", label_visibility="collapsed")
        chosen = by_id.get(chosen_id)
        if not chosen:
            return
        note_id = chosen["note_id"]
        # Keyed by note id, not by position: the list reorders as new notes are
        # generated, and a position key would silently move a user's practice
        # set from under them.
        _render_dmp_body(note_id, f"lib_{note_id}", chosen.get("language") or "en")


# ---------------------------------------------------------------------------
# Daily Prelims Practice (DPP)
# ---------------------------------------------------------------------------
# The same four steps as the standalone builder — coverage, counts, curate,
# export. Saved lectures read their text and language from the finished run;
# the separate upload workspace below uses the standalone extractor (including
# optional OCR) and keeps that source temporary. Both routes share the same
# QBG tagging, question-bank selection and PDF export code.


def _dpp_state(note_id: str, key: str, default=None):
    return st.session_state.get(f"dpp_{key}_{note_id}", default)


def _dpp_set(note_id: str, key: str, value) -> None:
    st.session_state[f"dpp_{key}_{note_id}"] = value


def _dpp_invalidate_exports(note_id: str) -> None:
    """Stop serving files for a set whose visible details have changed."""
    st.session_state.pop(f"dpp_exports_{note_id}", None)


def _dpp_reset_selection(note_id: str) -> None:
    """Drop the built set, keeping what has already been shown.

    `seen` deliberately survives: a question shown once in this session should
    not reappear in the next set, which is the rule the builder is built around.
    """
    for key in ("slots", "flash", "build_seconds"):
        st.session_state.pop(f"dpp_{key}_{note_id}", None)
    _dpp_invalidate_exports(note_id)


def _dpp_topics_changed(note_id: str, *also: str) -> None:
    """A filter changed the topic list: start the topic picker again from its
    defaults, since its old ticks may not exist. ``also`` names narrower
    filters to reset with it — a new subject has different chapters."""
    _dpp_reset_selection(note_id)
    for name in ("rows", *also):
        st.session_state.pop(f"dpp_{name}_{note_id}", None)


def _dpp_question_map(bank):
    return {q.question_id: q for q in bank.questions}


def _render_dpp_coverage(note_id: str, taxonomy, runs_dir=None, document=None,
                         scope: str | None = None):
    """Step 2 — the taxonomy rows this lecture covers, for review before use."""
    from src import dpp_builder

    # note_id says which lecture to read; scope says which copy of the panel
    # this is. They differ only when one lecture is on the page twice.
    scope = scope or note_id

    rows_by_id = {r.row_id: r for r in taxonomy.rows}
    candidates = _dpp_state(scope, "coverage")
    if candidates is None:
        coverage_clock = time.monotonic()
        with st.spinner("Matching these notes against the QBG taxonomy..."):
            try:
                if document is None:
                    candidates = list(dpp_builder.coverage(note_id, taxonomy.rows))
                else:
                    candidates = list(dpp_builder.coverage_document(
                        document, taxonomy.rows))
            except dpp_builder.DppError as exc:
                st.error(str(exc))
                return None
        _dpp_set(scope, "coverage", candidates)
        _dpp_set(scope, "coverage_seconds", time.monotonic() - coverage_clock)

    if not candidates:
        st.warning(
            "No chapter or topic in the QBG taxonomy matched these notes, so "
            "there is nothing to draw questions from. This usually means the "
            "lecture's subject is not in the tag workbook.")
        return None

    candidates = [c for c in candidates if c.row_id in rows_by_id]
    # One subject per lecture is the norm. The matcher already marks the rows
    # that sit outside the lecture's subject, or matched only weakly, as not
    # recommended — ticking all of them was what flooded the picker with
    # Polity, Science and Social Issues for a single Environment class.
    weight: dict[str, float] = {}
    for cand in candidates:
        subject = rows_by_id[cand.row_id].subject
        weight[subject] = weight.get(subject, 0.0) + cand.confidence * (
            2 if cand.recommended else 1)
    subjects = sorted(weight, key=weight.get, reverse=True)
    all_subjects = "All subjects"
    subject = st.selectbox(
        "Subject", subjects + ([all_subjects] if len(subjects) > 1 else []),
        key=f"dpp_subject_{scope}",
        format_func=lambda s: s if s == all_subjects else (
            f"{s}  ({sum(rows_by_id[c.row_id].subject == s for c in candidates)} topics)"),
        on_change=_dpp_topics_changed, args=(scope, "chapter"))
    show_weak = st.checkbox(
        "Show weaker matches", value=False, key=f"dpp_weak_{scope}",
        help="Topics the notes only mention in passing, or that matched "
             "loosely. Off by default so only what the lecture taught is listed.",
        on_change=_dpp_topics_changed, args=(scope,))

    in_subject = [c for c in candidates
                  if subject == all_subjects or rows_by_id[c.row_id].subject == subject]
    # "Current Affairs" alone holds a quarter of the workbook, and its
    # chapters (Polity and Governance, Science and Tech, Social Issues…) are
    # what a person means by "subject" there — so a subject with several
    # matched chapters can be narrowed one step further.
    chapter_count = Counter(rows_by_id[c.row_id].chapter for c in in_subject)
    if subject != all_subjects and len(chapter_count) > 1:
        all_chapters = "All chapters"
        chapter = st.selectbox(
            "Chapter", [all_chapters] + [c for c, _ in chapter_count.most_common()],
            key=f"dpp_chapter_{scope}",
            format_func=lambda c: c if c == all_chapters else (
                f"{c}  ({chapter_count[c]} topics)"),
            on_change=_dpp_topics_changed, args=(scope,))
        if chapter != all_chapters:
            in_subject = [c for c in in_subject
                          if rows_by_id[c.row_id].chapter == chapter]
    strong = [c for c in in_subject if c.recommended]
    shown = in_subject if show_weak or not strong else strong

    def label(cand) -> str:
        # Most specific first: the chip is cut off at ~25 characters, so the
        # part that tells two rows apart has to come before the chapter.
        row = rows_by_id[cand.row_id]
        path = [p for p in (row.subtopic, row.topic, row.chapter) if p]
        prefix = f"{row.subject}: " if subject == all_subjects else ""
        return f"{prefix}{' | '.join(path)}  ·  {cand.confidence:.0%}"

    labels = {cand.row_id: label(cand) for cand in shown}

    matched_in = _dpp_state(scope, "coverage_seconds")
    if matched_in is not None:
        st.caption(f"Notes matched to the taxonomy in {format_duration(matched_in)}.")
    hidden = len(in_subject) - len(shown)
    st.caption(
        f"{len(labels)} topic(s) from the tag workbook"
        + (f" ({hidden} weaker match(es) hidden)" if hidden else "")
        + ". Every one is an existing row — the app never invents a tag. "
          "Untick anything this lecture did not really cover.")
    chosen = st.multiselect(
        "Topics to draw questions from", list(labels),
        default=[c.row_id for c in shown if c.recommended] or list(labels),
        format_func=labels.get, key=f"dpp_rows_{scope}",
        on_change=_dpp_reset_selection, args=(scope,),
        label_visibility="collapsed")
    return [rows_by_id[rid] for rid in chosen if rid in rows_by_id]


def _render_dpp_counts(scope: str, bank, rows, language):
    """Step 3 — how many of each difficulty, bounded by what actually exists."""
    from src.notes_to_dpp.models import Difficulty
    from src.notes_to_dpp.selection import available_counts

    available = available_counts(
        bank.questions, rows,
        seen_ids=_dpp_state(scope, "seen_ids", frozenset()),
        seen_fingerprints=_dpp_state(scope, "seen_fps", frozenset()),
        language=language,
    )
    total = sum(available.values())
    if not total:
        st.warning(
            f"The question bank has no {language.value} questions for the "
            f"topics selected above. Pick different topics, or check that the "
            f"bank holds questions in this language.")
        return None

    st.caption(
        f"Available for these topics in {language.value}: "
        + " · ".join(f"{d.value} {available[d]}" for d in Difficulty))
    cols = st.columns(3)
    requested = {}
    for col, difficulty in zip(cols, Difficulty):
        with col:
            cap = int(available[difficulty])
            count_key = f"dpp_n_{difficulty.value}_{scope}"
            # A topic change can reduce a cap after the count widget has
            # already been used. Clamp its retained Streamlit state before
            # creating the widget, otherwise its old value is invalid.
            default = min(5, cap)
            if count_key in st.session_state:
                invalid = False
                try:
                    current = int(st.session_state[count_key])
                except (TypeError, ValueError):
                    current = default
                    invalid = True
                clamped = max(0, min(current, cap))
                if invalid or current != clamped:
                    # Let the new widget default initialise the corrected
                    # value; assigning a widget key directly here makes
                    # Streamlit warn that two defaults were supplied.
                    st.session_state.pop(count_key, None)
                    default = clamped
            requested[difficulty] = st.number_input(
                difficulty.value, min_value=0, max_value=cap,
                value=default, step=1,
                key=count_key,
                disabled=cap == 0)
    return requested


_DPP_PICK_PAGE = 25


def _dpp_toggle_pick(scope: str, question_id: str) -> None:
    # A tuple, not a set: the paper follows the order the questions were ticked.
    picked = tuple(_dpp_state(scope, "picked", ()))
    _dpp_set(scope, "picked", tuple(q for q in picked if q != question_id)
             if question_id in picked else picked + (question_id,))


def _render_dpp_manual_pick(scope: str, bank, rows, language) -> None:
    """Step 3, by hand — tick the exact questions the set should hold.

    Ticks are kept in their own set rather than in the checkbox keys: a
    checkbox that a filter or a page change hides loses its widget state, and
    the question would silently drop out of the set.
    """
    from src.notes_to_dpp.models import Difficulty
    from src.notes_to_dpp.pdf_export import prompt_preview_markdown
    from src.notes_to_dpp.selection import eligible_questions, manual_selection

    seen_ids = _dpp_state(scope, "seen_ids", frozenset())
    seen_fps = _dpp_state(scope, "seen_fps", frozenset())
    pool = eligible_questions(bank.questions, rows, seen_ids=seen_ids,
                              seen_fingerprints=seen_fps, language=language)
    if not pool:
        st.warning(
            f"The question bank has no unused {language.value} questions for the "
            f"topics selected above.")
        return
    eligible_ids = {q.question_id for q, _ in pool}
    picked = [qid for qid in _dpp_state(scope, "picked", ()) if qid in eligible_ids]

    topics = {row.row_id: " | ".join(p for p in (row.subtopic, row.topic) if p)
              for _, row in pool}
    c1, c2 = st.columns(2)
    with c1:
        level = st.selectbox("Difficulty", ["All"] + [d.value for d in Difficulty],
                             key=f"dpp_pick_level_{scope}")
    with c2:
        topic = st.selectbox("Topic", ["All"] + list(topics),
                             format_func=lambda r: "All" if r == "All" else topics[r],
                             key=f"dpp_pick_topic_{scope}")
    listed = [(q, row) for q, row in pool
              if (level == "All" or q.difficulty.value == level)
              and (topic == "All" or row.row_id == topic)]
    pages = max(1, -(-len(listed) // _DPP_PICK_PAGE))
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Page (of {pages})", min_value=1, max_value=pages,
                                   value=1, step=1, key=f"dpp_pick_page_{scope}"))
    st.caption(f"{len(listed)} question(s) match · {len(picked)} ticked.")

    ticked = set(picked)
    start = (page - 1) * _DPP_PICK_PAGE
    for question, row in listed[start:start + _DPP_PICK_PAGE]:
        localized = question.content_for(language)
        prompt = getattr(localized, "prompt", "") or question.prompt
        options = getattr(localized, "options", ()) or question.options
        # The builder already sits inside an expander, and Streamlit refuses a
        # nested one — so the whole question is shown, not folded away.
        with st.container(border=True):
            st.checkbox(
                f"[{question.difficulty.value}] {topics[row.row_id]}",
                value=question.question_id in ticked,
                key=f"dpp_pick_{question.question_id}_{scope}",
                on_change=_dpp_toggle_pick, args=(scope, question.question_id))
            st.markdown(prompt_preview_markdown(
                prompt, getattr(localized, "prompt_markup", None)))
            st.markdown("  \n".join(
                f"{chr(65 + i)}. {option}" for i, option in enumerate(options)))

    if st.button(f"Build practice set ({len(picked)} ticked)",
                 key=f"dpp_build_picked_{scope}", type="primary",
                 disabled=not picked, use_container_width=True):
        build_clock = time.monotonic()
        try:
            outcome = manual_selection(bank.questions, rows, picked,
                                       seen_ids=seen_ids, seen_fingerprints=seen_fps,
                                       language=language)
        except ValueError as exc:
            st.error(str(exc))
            return
        _dpp_set(scope, "slots", list(outcome.slots))
        _dpp_set(scope, "seen_ids", outcome.seen_ids)
        _dpp_set(scope, "seen_fps", outcome.seen_fingerprints)
        _dpp_set(scope, "build_seconds", time.monotonic() - build_clock)
        st.session_state.pop(f"dpp_picked_{scope}", None)
        st.rerun()


def _render_dpp_preview(scope: str, slots, bank, rows, language):
    """Step 4 — the built set, with replace and remove per question."""
    from src.notes_to_dpp.selection import available_counts, remove_question, replace_question
    from src.notes_to_dpp.pdf_export import prompt_preview_markdown

    questions = _dpp_question_map(bank)
    selected_ids = {slot.question_id for slot in slots}
    selected_fps = {questions[qid].content_fingerprint for qid in selected_ids if qid in questions}
    remaining = available_counts(
        bank.questions, rows,
        seen_ids=set(_dpp_state(scope, "seen_ids", frozenset())) | selected_ids,
        seen_fingerprints=set(_dpp_state(scope, "seen_fps", frozenset())) | selected_fps,
        language=language)
    for index, slot in enumerate(slots, start=1):
        question = questions.get(slot.question_id)
        if question is None:
            continue
        localized = question.content_for(language)
        prompt = getattr(localized, "prompt", "") or question.prompt
        prompt = prompt_preview_markdown(prompt, getattr(localized, "prompt_markup", None))
        options = getattr(localized, "options", ()) or question.options

        st.markdown(f"**Q{index}.** :gray[[{slot.difficulty.value}]] {prompt}")
        for position, option in enumerate(options):
            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;{chr(65 + position)}. {option}")

        c1, c2, c3 = st.columns([1, 1, 6])
        with c1:
            exhausted = remaining.get(slot.difficulty, 0) == 0
            if st.button("Replace", key=f"dpp_rep_{slot.slot_id}_{scope}",
                         disabled=exhausted,
                         help=f"No unused {slot.difficulty.value} replacement remains." if exhausted else None):
                outcome = replace_question(
                    slots, slot.slot_id, bank.questions, rows,
                    seen_ids=_dpp_state(scope, "seen_ids", frozenset()),
                    seen_fingerprints=_dpp_state(scope, "seen_fps", frozenset()),
                    language=language)
                _dpp_set(scope, "slots", list(outcome.slots))
                _dpp_set(scope, "seen_ids", outcome.seen_ids)
                _dpp_set(scope, "seen_fps", outcome.seen_fingerprints)
                _dpp_set(scope, "flash", outcome.message)
                if outcome.replaced:
                    _dpp_invalidate_exports(scope)
                st.rerun()
        with c2:
            if st.button("Remove", key=f"dpp_del_{slot.slot_id}_{scope}"):
                outcome = remove_question(
                    slots, slot.slot_id,
                    seen_ids=_dpp_state(scope, "seen_ids", frozenset()),
                    seen_fingerprints=_dpp_state(scope, "seen_fps", frozenset()))
                _dpp_set(scope, "slots", list(outcome.slots))
                _dpp_set(scope, "seen_ids", outcome.seen_ids)
                _dpp_set(scope, "seen_fps", outcome.seen_fingerprints)
                if outcome.removed:
                    _dpp_invalidate_exports(scope)
                st.rerun()
        st.divider()


def _dpp_normalized_number(value: object) -> str:
    """Return the printable two-digit DPP number, or an empty fallback."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(2) if digits else ""


def _dpp_saved_number(note_id: str) -> str:
    """Use the number saved with a newly generated lecture when available."""
    from src.dmp import store as dmp_store

    try:
        import json

        metadata_path = dmp_store.note_run_dir(note_id) / "run_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return ""
        return _dpp_normalized_number(metadata.get("dpp_no"))
    except (AttributeError, OSError, ValueError, TypeError):
        # Older library notes did not have a DPP identifier. The frontend field
        # below deliberately lets a teacher supply one for that export.
        return ""


def _dpp_export_signature(slots, language, dpp_no: str, title: str,
                          subject: str = "") -> tuple:
    """Everything that changes the bytes or name of a DPP download."""
    return (
        tuple((slot.slot_id, slot.question_id) for slot in slots),
        getattr(language, "value", str(language)),
        dpp_no,
        title or "",
        subject or "",
    )


def _render_dpp_subject_input(scope: str, rows, lecture_subject: str = "") -> str:
    """The subject that titles every PDF, on one line with the DPP box.

    Prefilled with the lecture's own subject line; an uploaded source has
    none, so it falls back to the tag-workbook subject of most of the chosen
    topics. Either way the teacher can overwrite it. Drawn whenever topics
    are, so it never vanishes between one set and the next.
    """
    key = f"dpp_export_subject_{scope}"
    if key not in st.session_state:
        tagged = Counter(row.subject for row in rows)
        st.session_state[key] = lecture_subject or (
            tagged.most_common(1)[0][0] if tagged else "")
    return " ".join(st.text_input(
        "Subject printed on the PDFs", key=key, placeholder="Geography",
        help=("The title of every PDF, beside the DPP box. Left empty, the "
              "title reads \"Practice Questions\"."),
        on_change=_dpp_invalidate_exports, args=(scope,),
    ).split())


def _render_dpp_number_input(note_id: str, scope: str | None = None) -> str:
    """A per-lecture override that also makes older library notes numberable."""
    scope = scope or note_id
    number_key = f"dpp_export_no_{scope}"
    if number_key not in st.session_state:
        st.session_state[number_key] = _dpp_saved_number(note_id)
    return _dpp_normalized_number(st.text_input(
        "DPP No. for this practice set", key=number_key, placeholder="03",
        help=("Printed in the PDFs and used in their filenames. It is prefilled "
              "from the lecture when available, and can be set for older notes."),
        on_change=_dpp_invalidate_exports, args=(scope,),
    ).strip())


def _render_dpp_exports(note_id: str, slots, bank, language, title: str,
                        dpp_no: str, output_dir: Path | None = None,
                        scope: str | None = None, subject: str = ""):
    """Step 5 — write the IDs, the question paper and the solutions, then serve.

    Saved-lecture files live next to their notes. Uploaded-note files live in
    a session-scoped temporary folder. In both cases downloads re-serve a
    finished file, so a click cannot re-enter question selection or generation.
    """
    from src.dmp import store as dmp_store
    from src.notes_to_dpp.exporting import export_ids_csv, export_ids_text
    from src.notes_to_dpp.pdf_export import export_practice_pdfs
    from src.utils import ensure_dir, safe_name

    scope = scope or note_id

    named = title if subject and subject.casefold() in (title or "").casefold() else (
        " ".join(p for p in (subject, title) if p))
    stem = safe_name(named or note_id, default="dpp")
    dpp_suffix = f"_DPP_{dpp_no}" if dpp_no else "_dpp"
    export_signature = _dpp_export_signature(slots, language, dpp_no, title, subject)
    built = _dpp_state(scope, "exports")
    if not isinstance(built, dict) or built.get("signature") != export_signature:
        # The previous in-memory shape did not carry a signature, so it is
        # intentionally considered stale after this frontend integration.
        _dpp_invalidate_exports(scope)
        built = None

    if st.button("Build the practice set files", key=f"dpp_build_files_{scope}",
                 type="primary", use_container_width=True):
        export_clock = time.monotonic()
        with st.spinner("Writing the question paper and solutions..."):
            try:
                out_dir = ensure_dir(
                    Path(output_dir) if output_dir is not None
                    else dmp_store.note_run_dir(note_id).joinpath("dpp"))
                export = export_practice_pdfs(
                    slots, _dpp_question_map(bank), language, dpp_no=dpp_no,
                    subject=subject)
                files = {
                    "ids_csv": (out_dir / f"{stem}{dpp_suffix}_ids.csv", export_ids_csv(slots)),
                    "ids_txt": (out_dir / f"{stem}{dpp_suffix}_ids.txt", export_ids_text(slots)),
                    "questions": (out_dir / f"{stem}{dpp_suffix}_questions.pdf",
                                  export.question_pdf),
                    "solutions": (out_dir / f"{stem}{dpp_suffix}_solutions.pdf",
                                  export.solutions_pdf),
                    "combined": (out_dir / f"{stem}{dpp_suffix}_questions_with_solutions.pdf",
                                 export.combined_pdf),
                }
                for path, payload in files.values():
                    path.write_bytes(payload)

                # The everything-in-one download: whoever receives it gets
                # the same files this folder holds, bit for bit.
                bundle = io.BytesIO()
                with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path, payload in files.values():
                        archive.writestr(path.name, payload)
                bundle_path = out_dir / f"{stem}{dpp_suffix}_all_files.zip"
                bundle_path.write_bytes(bundle.getvalue())
                files["bundle"] = (bundle_path, bundle.getvalue())
                built = {
                    "signature": export_signature,
                    "files": {name: str(path) for name, (path, _) in files.items()},
                    "seconds": time.monotonic() - export_clock,
                }
                _dpp_set(scope, "exports", built)
                # Both copies of this lecture's builder — the results card and
                # the library — write these files to the SAME paths under the
                # note's dpp folder. This build has just replaced the bytes
                # behind any other copy's still-offered download buttons, so
                # that copy must stop serving them and offer a rebuild rather
                # than quietly handing out files from a different set.
                for state_key in list(st.session_state.keys()):
                    if not str(state_key).startswith("dpp_exports_"):
                        continue
                    other_scope = str(state_key)[len("dpp_exports_"):]
                    if other_scope != scope and (
                            other_scope == note_id
                            or other_scope.startswith(f"{note_id}_card")):
                        st.session_state.pop(state_key, None)
            except Exception as exc:  # noqa: BLE001 — a download is never worth a crash
                st.error("Those PDFs could not be built.")
                st.caption(f"Reason: {exc}")
                built = None

    if not built:
        return
    if built.get("seconds") is not None:
        st.caption(
            f"Practice set files written in {format_duration(built['seconds'])}.")
    files = built["files"]
    labels = (
        ("questions", "Question paper (PDF)"),
        ("solutions", "Answers and solutions (PDF)"),
        ("combined", "Questions with answers (PDF)"),
        ("bundle", "All files (ZIP)"),
        ("ids_csv", "QBG IDs (CSV)"),
        ("ids_txt", "QBG IDs (TXT)"),
    )
    for row in (labels[:2], labels[2:4], labels[4:]):
        cols = st.columns(len(row))
        for col, (name, label) in zip(cols, row):
            with col:
                _download_from_path(
                    label, files.get(name), f"dpp_dl_{name}_{scope}",
                    "That file could not be created.")


def _render_dpp_body(
    note_id: str,
    title: str = "",
    *,
    document=None,
    question_language=None,
    output_dir: Path | None = None,
    scope: str | None = None,
) -> None:
    """The builder for either a saved lecture or one temporary uploaded source.

    ``scope`` names this copy of the panel. It is the note id everywhere
    except the results card, where the lecture that just finished is also
    the one the library picker opens on — two live copies of the same
    builder, whose widgets would otherwise claim the same keys and crash
    the page.
    """
    from src import dpp_builder
    from src.notes_to_dpp.models import Language
    from src.notes_to_dpp.selection import InsufficientPoolError, generate_selection

    scope = scope or note_id

    if not dpp_builder.masters_ready():
        st.caption(
            "The QBG tag workbook and the question bank are not on this "
            "machine. Put them in the app's `data` folder, or set "
            "QBG_TAGS_PATH and QUESTION_BANK_PATH.")
        return

    # ---- the gate -------------------------------------------------------
    # Streamlit executes an expander's body whether or not it is open, so
    # everything below this point used to run on every page load: parsing the
    # 55 MB question bank takes ~39 s the first time, and it was happening to
    # people who never opened the panel — including in the library expander,
    # which renders BEFORE the notes downloads. The whole page, Generate button
    # included, sat frozen behind a practice set nobody had asked for.
    #
    # Nothing heavy happens now until someone says they want it, and when they
    # do they are told what they are waiting for instead of watching a
    # spinner with no end in sight.
    if not _dpp_state(scope, "opened"):
        if st.button("Open the practice builder", key=f"dpp_open_{scope}",
                     use_container_width=True):
            _dpp_set(scope, "opened", True)
            st.rerun()
        st.caption(
            "The question bank is a 55 MB workbook and takes about 40 seconds to "
            "read the first time in a session. It is read once and then reused, "
            "so every practice set after the first is immediate.")
        return

    dpp_no = _render_dpp_number_input(note_id, scope)
    # The bank ships 6,906 rows that have not been through verification.
    # They stay out unless a teacher asks for them, and asking reparses the
    # workbook because the two pools are cached separately.
    verified_only = st.checkbox(
        "Verified questions only", value=True,
        key=f"dpp_verified_only_{scope}",
        help=("Recommended. Unticking widens the pool with unverified "
              "bank rows and re-reads the workbook once."),
        on_change=_dpp_reset_selection, args=(scope,))

    try:
        language, code = dpp_builder.note_language(note_id)
    except Exception:  # noqa: BLE001
        language, code = Language.ENGLISH, "en"

    if language is Language.UNKNOWN:
        # Marathi. The bank holds English and Hindi rows only, and serving
        # Hindi for a Marathi note is the cross-language fallback the bank is
        # built to refuse — so the choice is the user's, made visibly.
        st.info(
            "These notes are in Marathi. The question bank holds English and "
            "Hindi questions only, so there is no Marathi set to draw from. "
            "Choose which language of questions you want instead.")
        picked = st.radio("Question language", ["English", "Hindi"],
                          horizontal=True, key=f"dpp_lang_{scope}",
                          on_change=_dpp_reset_selection, args=(scope,))
        language = Language.ENGLISH if picked == "English" else Language.HINDI

    if document is not None:
        # The upload workspace asks for this choice explicitly. Do not infer
        # Hindi from a Devanagari upload: Marathi uses the same script.
        language = question_language or Language.ENGLISH

    with st.spinner(
        "Reading the tag workbook and the 26,000-question bank. This takes about "
        "40 seconds the first time in a session, and is instant after that."
    ):
        bank_clock = time.monotonic()
        try:
            taxonomy, bank = dpp_builder.load_masters(
                verified_only=verified_only)
        except dpp_builder.DppError as exc:
            st.error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            st.error("The master workbooks could not be read.")
            st.caption(f"Reason: {exc}")
            return

    # setdefault, so the caption keeps the real parse rather than being
    # overwritten by the instant cache hit on the next widget click.
    bank_key = f"dpp_bank_seconds_{bool(verified_only)}"
    st.session_state.setdefault(bank_key, time.monotonic() - bank_clock)
    st.caption(
        f"Question bank read in {format_duration(st.session_state[bank_key])} "
        f"(once per session).")

    rows = _render_dpp_coverage(note_id, taxonomy, document=document, scope=scope)
    if not rows:
        return
    lecture_subject = ""
    if document is None:
        from src.dmp import service as dmp_service
        try:
            lecture_subject = dmp_service.note_context(note_id).get("subject_line") or ""
        except Exception:  # noqa: BLE001 — a missing label must not block the builder
            lecture_subject = ""
    subject = _render_dpp_subject_input(scope, rows, lecture_subject)

    flash = _dpp_state(scope, "flash")
    if flash:
        st.caption(flash)
        st.session_state.pop(f"dpp_flash_{scope}", None)

    slots = _dpp_state(scope, "slots")
    if not slots:
        mode = st.radio(
            "How to choose the questions", ["By difficulty", "Pick questions myself"],
            horizontal=True, key=f"dpp_mode_{scope}")
        if mode == "Pick questions myself":
            _render_dpp_manual_pick(scope, bank, rows, language)
            return
        requested = _render_dpp_counts(scope, bank, rows, language)
        if requested is None:
            return
        if st.button(f"Build practice set ({sum(requested.values())} questions)",
                     key=f"dpp_build_{scope}", type="primary",
                     disabled=sum(requested.values()) == 0,
                     use_container_width=True):
            build_clock = time.monotonic()
            try:
                outcome = generate_selection(
                    bank.questions, rows, requested,
                    seen_ids=_dpp_state(scope, "seen_ids", frozenset()),
                    seen_fingerprints=_dpp_state(scope, "seen_fps", frozenset()),
                    language=language)
            except InsufficientPoolError as exc:
                st.error(f"Not enough questions for that request. {exc}")
                return
            _dpp_set(scope, "slots", list(outcome.slots))
            _dpp_set(scope, "seen_ids", outcome.seen_ids)
            _dpp_set(scope, "seen_fps", outcome.seen_fingerprints)
            _dpp_set(scope, "build_seconds", time.monotonic() - build_clock)
            st.rerun()
        return

    built_in = _dpp_state(scope, "build_seconds")
    suffix = f" · built in {format_duration(built_in)}" if built_in is not None else ""
    st.markdown(f"**{len(slots)} questions** · {language.value}{suffix}")
    _render_dpp_preview(scope, slots, bank, rows, language)
    _render_dpp_exports(
        note_id, slots, bank, language, title, dpp_no,
        output_dir=output_dir, scope=scope, subject=subject)
    if st.button("Start a new set", key=f"dpp_new_{scope}",
                 use_container_width=True):
        _dpp_reset_selection(scope)
        st.rerun()


def _render_dpp_panel(r: dict, idx, *, embedded: bool = False) -> None:
    """Prelims practice questions drawn from the tagged question bank."""
    from src.dmp import service as dmp_service

    note_id = dmp_service.note_id_from_run_dir(r.get("run_dir"))
    shell = (nullcontext() if embedded
             else st.expander("Daily Prelims Practice (DPP)", expanded=False))
    with shell:
        if not note_id or not dmp_service.has_notes(note_id):
            st.caption(
                "Prelims practice needs the notes' run folder, which is not on "
                "this machine any more.")
            return
        st.caption(
            "MCQs from the QBG question bank, matched to what this lecture "
            "actually covered. Nothing is generated — every question is an "
            "existing bank question, tagged to an existing taxonomy row.")
        context = dmp_service.note_context(note_id)
        _render_dpp_body(
            note_id, context.get("note_title") or r.get("name") or "",
            scope=f"{note_id}_card{idx}")


def _render_dpp_library() -> None:
    """Build a practice set from any lecture already on this machine."""
    from src import dpp_builder
    from src.dmp import service as dmp_service

    if not dpp_builder.masters_ready():
        return
    notes = dmp_service.list_notes()
    if not notes:
        return
    with st.expander("Daily Prelims Practice from an earlier lecture", expanded=False):
        st.caption(
            "Pick a lecture you have already generated notes for. The questions "
            "come from the QBG bank, matched against those notes.")
        # Options are note ids, not the note dicts. A selectbox holding dicts
        # did not survive a rerun: pressing "Open the practice builder" moved
        # the selection to the NEXT lecture in the list, so the panel came back
        # showing the gate again and the builder was unreachable — the user
        # pressed a button and silently changed which class they were working
        # on. An id is hashable and stable, and the label is looked up from it.
        by_id = {note["note_id"]: note for note in notes}
        labels = dmp_service.note_labels(notes)
        chosen_id = st.selectbox(
            "Lecture", list(by_id), format_func=labels.get,
            key="dpp_library_pick", label_visibility="collapsed")
        chosen = by_id.get(chosen_id)
        if chosen:
            _render_dpp_body(chosen["note_id"], chosen.get("note_title") or "")


def _dpp_clear_uploaded_source_state(source_id: str) -> None:
    """Forget one temporary upload's builder state without touching saved runs."""
    for key in ("coverage", "coverage_seconds", "slots", "flash", "exports",
                "seen_ids", "seen_fps", "opened", "build_seconds", "subject", "chapter",
                "weak", "mode", "picked", "pick_level", "pick_topic", "pick_page",
                "export_subject"):
        st.session_state.pop(f"dpp_{key}_{source_id}", None)
    st.session_state.pop(f"dpp_export_no_{source_id}", None)
    st.session_state.pop(f"dpp_upload_language_{source_id}", None)
    st.session_state.pop(f"dpp_rows_{source_id}", None)
    for difficulty in ("Easy", "Medium", "Hard"):
        st.session_state.pop(f"dpp_n_{difficulty}_{source_id}", None)


def _dpp_uploaded_output_dir(source_id: str) -> Path:
    """A session-scoped home for DPP downloads made from an uploaded file."""
    holder = st.session_state.get("_dpp_upload_tmpdir")
    if not holder or not Path(holder).exists():
        holder = tempfile.mkdtemp(prefix="upsc_dpp_upload_")
        st.session_state["_dpp_upload_tmpdir"] = holder
    output_dir = Path(holder) / source_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _dpp_uploaded_source() -> dict | None:
    source = st.session_state.get("dpp_uploaded_source")
    return source if isinstance(source, dict) else None


def _render_dpp_uploaded_notes() -> None:
    """A no-pipeline DPP source: extract notes into this browser session only."""
    from src import dpp_builder
    from src.notes_to_dpp.models import Language
    from src.notes_to_dpp.notes import detect_note_language

    st.markdown("**Build a DPP from uploaded notes**")
    st.caption(
        "Upload a PDF, TXT or Markdown notes file. The app only reads the notes "
        "and matches them to the question bank; it does not generate Notes DOCX/PDF "
        "or call the AI model.")
    uploaded = st.file_uploader(
        "Upload notes for DPP", type=["pdf", "txt", "md"],
        key="dpp_upload_notes")
    title = st.text_input(
        "Lecture title for this DPP", key="dpp_upload_title",
        placeholder="Use a short topic name for the downloaded files").strip()
    enable_ocr = st.checkbox(
        "Use OCR for scanned PDFs", value=True, key="dpp_upload_ocr")

    if st.button("Read uploaded notes", key="dpp_upload_read",
                 type="primary", use_container_width=True):
        if uploaded is None:
            st.warning("Upload a PDF, TXT or Markdown notes file first.")
        else:
            data = uploaded.getvalue()
            try:
                with st.spinner("Reading the uploaded notes..."):
                    document = dpp_builder.extract_uploaded_document(
                        data, uploaded.name, enable_ocr=enable_ocr)
            except dpp_builder.DppError as exc:
                st.error(str(exc))
            else:
                source_id = f"dpp_upload_{hashlib.sha256(data).hexdigest()[:16]}"
                previous = _dpp_uploaded_source()
                if previous:
                    _dpp_clear_uploaded_source_state(str(previous.get("source_id") or ""))
                _dpp_clear_uploaded_source_state(source_id)
                detected = detect_note_language(document).language
                st.session_state["dpp_uploaded_source"] = {
                    "source_id": source_id,
                    "filename": Path(uploaded.name).name,
                    "title": title or Path(uploaded.name).stem or "Uploaded notes",
                    "document": document,
                    "detected_language": detected.value,
                    "warnings": tuple(document.warnings),
                    "output_dir": str(_dpp_uploaded_output_dir(source_id)),
                }
                st.rerun()

    source = _dpp_uploaded_source()
    if not source:
        return
    source_id = str(source.get("source_id") or "")
    document = source.get("document")
    if not source_id or document is None:
        st.session_state.pop("dpp_uploaded_source", None)
        return

    st.success(f"Uploaded notes ready: {source.get('filename') or 'notes'}")
    detected = str(source.get("detected_language") or "Unknown")
    st.caption(
        f"Detected script: {detected}. Choose the question language explicitly; "
        "Marathi and Hindi use the same script.")
    for warning in source.get("warnings") or ():
        st.warning(str(warning))

    default_language = 1 if detected == Language.HINDI.value else 0
    picked = st.radio(
        "DPP question language", ["English", "Hindi"], index=default_language,
        horizontal=True, key=f"dpp_upload_language_{source_id}",
        on_change=_dpp_reset_selection, args=(source_id,))
    question_language = (
        Language.ENGLISH if picked == "English" else Language.HINDI)
    _render_dpp_body(
        source_id, str(source.get("title") or "Uploaded notes"),
        document=document, question_language=question_language,
        output_dir=Path(str(source.get("output_dir") or _dpp_uploaded_output_dir(source_id))),
    )
    if st.button("Clear uploaded notes", key="dpp_upload_clear",
                 use_container_width=True):
        _dpp_clear_uploaded_source_state(source_id)
        st.session_state.pop("dpp_uploaded_source", None)
        st.rerun()


def _dmp_uploaded_runs_dir() -> Path:
    """A throwaway runs folder for notes that were uploaded, not generated.

    Every DMP service call already accepts a ``runs_dir``, so an upload only
    needs a run folder of its own to become a first-class note: sessions,
    answers, history and the DOCX/PDF sheets all work unchanged against it.
    Keeping it temporary is the same choice DPP's upload makes — the file is a
    source for practice, not a lecture this app generated, and it does not
    belong in the library beside real runs.
    """
    holder = st.session_state.get("_dmp_upload_runsdir")
    if not holder or not Path(holder).exists():
        holder = tempfile.mkdtemp(prefix="upsc_dmp_upload_")
        st.session_state["_dmp_upload_runsdir"] = holder
    return Path(holder)


def _dmp_clear_uploaded_source_state(source_id: str) -> None:
    """Forget one upload's panel state without touching any saved run."""
    for key in (
        _dmp_state_key(source_id),
        f"dmp_questions_seconds_{source_id}",
        f"dmp_answers_seconds_{source_id}",
        f"dmp_history_{source_id}",
    ):
        st.session_state.pop(key, None)
    for kind in ("questions", "answers"):
        st.session_state.pop(f"dmp_sheet_{kind}_{source_id}", None)
    # The printed subject/topic/number boxes, so a new upload is prefilled
    # from its own title rather than keeping the last one's.
    for name in ("subject", "topic", "dmp_no"):
        st.session_state.pop(f"dmp_print_{name}_{source_id}", None)


def _dmp_write_uploaded_run(source_id: str, text_body: str, title: str,
                            language: str) -> Path:
    """Lay the upload out on disk the way a finished run looks."""
    import json

    run_dir = _dmp_uploaded_runs_dir() / source_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # The lecture-title marker is how the rest of the app names a note, and it
    # is what the practice sheets print at the top of the page.
    header = f"#LECTURE_TITLE: {title}\n\n" if title else ""
    (run_dir / "notes_final.md").write_text(header + text_body, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(
        json.dumps({
            "language": language,
            "subject": "",
            "area": "",
            # Left blank on purpose. note_context() builds its topic as
            # "subject - title", so repeating the title here prints it twice.
            "subject_line": "",
            "note_title": title,
            "source": "uploaded notes",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return run_dir


def _dmp_uploaded_source() -> dict | None:
    source = st.session_state.get("dmp_uploaded_source")
    return source if isinstance(source, dict) else None


def _render_dmp_uploaded_notes() -> None:
    """Mains practice from a notes file, with no pipeline run behind it."""
    from src import dpp_builder
    from src.config import DMP_MIN_NOTE_WORDS
    from src.dmp import service as dmp_service
    from src.languages import SUPPORTED

    st.markdown("**Build a DMP from uploaded notes**")
    st.caption(
        "Upload a PDF, TXT or Markdown notes file. The app reads the notes and "
        "writes Mains questions from them; it does not generate Notes DOCX/PDF.")
    uploaded = st.file_uploader(
        "Upload notes for DMP", type=["pdf", "txt", "md"],
        key="dmp_upload_notes")
    title = st.text_input(
        "Lecture title for this DMP", key="dmp_upload_title",
        placeholder="Use a short topic name for the downloaded sheets").strip()
    c1, c2 = st.columns([2, 3])
    with c1:
        enable_ocr = st.checkbox(
            "Use OCR for scanned PDFs", value=True, key="dmp_upload_ocr")
    with c2:
        # Asked, never inferred: Hindi and Marathi share a script, so reading it
        # off the page is exactly the mistake that hands a Marathi class a set
        # of Hindi questions.
        names = {"en": "English", "hi": "Hindi", "mr": "Marathi"}
        picked = st.radio(
            "Question language", [code for code in SUPPORTED if code in names],
            format_func=names.get, horizontal=True, key="dmp_upload_language")

    if st.button("Read uploaded notes", key="dmp_upload_read",
                 type="primary", use_container_width=True):
        if uploaded is None:
            st.warning("Upload a PDF, TXT or Markdown notes file first.")
        else:
            data = uploaded.getvalue()
            try:
                with st.spinner("Reading the uploaded notes..."):
                    document = dpp_builder.extract_uploaded_document(
                        data, uploaded.name, enable_ocr=enable_ocr)
            except dpp_builder.DppError as exc:
                st.error(str(exc))
            else:
                body = document.text
                words = len(body.split())
                if words < DMP_MIN_NOTE_WORDS:
                    # Said here rather than behind the Generate button: five
                    # confident questions about nothing is the failure this
                    # threshold exists to prevent.
                    st.error(
                        f"These notes have only {words} readable word(s). Mains "
                        f"questions need at least {DMP_MIN_NOTE_WORDS} to be "
                        f"grounded in something.")
                else:
                    source_id = f"dmp_upload_{hashlib.sha256(data).hexdigest()[:16]}"
                    previous = _dmp_uploaded_source()
                    if previous:
                        _dmp_clear_uploaded_source_state(
                            str(previous.get("source_id") or ""))
                    _dmp_clear_uploaded_source_state(source_id)
                    chosen_title = title or Path(uploaded.name).stem
                    _dmp_write_uploaded_run(
                        source_id, body, chosen_title, picked)
                    st.session_state["dmp_uploaded_source"] = {
                        "source_id": source_id,
                        "filename": Path(uploaded.name).name,
                        "title": chosen_title,
                        "language": picked,
                        "warnings": tuple(document.warnings),
                    }
                    st.rerun()

    source = _dmp_uploaded_source()
    if not source:
        return
    source_id = str(source.get("source_id") or "")
    runs_dir = _dmp_uploaded_runs_dir()
    if not source_id or not dmp_service.has_notes(source_id, runs_dir):
        # The temporary folder is gone — a restart, or a cleaned temp drive.
        st.session_state.pop("dmp_uploaded_source", None)
        return

    st.success(f"Uploaded notes ready: {source.get('filename') or 'notes'}")
    for warning in source.get("warnings") or ():
        st.warning(str(warning))
    _render_dmp_body(source_id, source_id, str(source.get("language") or "en"),
                     runs_dir=runs_dir)
    if st.button("Clear uploaded notes", key="dmp_upload_clear",
                 use_container_width=True):
        _dmp_clear_uploaded_source_state(source_id)
        st.session_state.pop("dmp_uploaded_source", None)
        st.rerun()


def _render_dmp_workspace() -> None:
    """The dedicated Daily Mains Practice tab, laid out like the DPP one."""
    from src.dmp import service as dmp_service

    upload_tab, saved_tab = st.tabs(("Upload notes", "Saved lectures"))
    with upload_tab:
        _render_dmp_uploaded_notes()
    with saved_tab:
        st.caption(
            "Or use a lecture already generated by this app. Nothing is "
            "regenerated - the questions are written from the notes on disk.")
        if dmp_service.list_notes():
            _render_dmp_library()
        else:
            st.info(
                "No saved lectures are available yet. Upload notes above to "
                "practise without generating one.")


def _render_dpp_workspace() -> None:
    """The dedicated Daily Prelims Practice tab."""
    from src import dpp_builder
    from src.dmp import service as dmp_service

    upload_tab, saved_tab = st.tabs(("Upload notes", "Saved lectures"))
    with upload_tab:
        _render_dpp_uploaded_notes()
    with saved_tab:
        st.caption(
            "Or use a lecture already generated by this app, with the same "
            "coverage, curation and export workflow as before.")
        if not dpp_builder.masters_ready():
            st.info(
                "DPP needs the QBG tag workbook and question bank in the data "
                "folder before a practice set can be built.")
        elif dmp_service.list_notes():
            _render_dpp_library()
        else:
            st.info("No saved lectures are available yet. Upload notes above to test DPP.")


def _render_dcn_render_status(info: dict) -> None:
    """Show what the render check found: a warning per overlapping page or raw
    warning, a short caption when it rendered clean, and an em-dash when the
    check did not run at all (never a fabricated pass)."""
    if not info or not info.get("ran"):
        st.caption("Layout render check: — (did not run for this file).")
        return
    overlap = info.get("overlap_pages") or []
    if overlap:
        pages = ", ".join(str(page) for page in overlap)
        st.warning(
            f"Layout render check: text crosses into the notes-box column on page {pages}. "
            "Open the DOCX and shorten that content before sharing it with students."
        )
    for warning in info.get("warnings") or []:
        st.warning(f"Layout render check: {warning}")
    if info.get("ok") and not overlap and not (info.get("warnings") or []):
        pages_checked = info.get("page_count") or 0
        suffix = f" — {pages_checked} page(s) checked" if pages_checked else ""
        st.caption(f"Layout render check: clean, nothing overlaps the notes box{suffix}.")


# ---------------------------------------------------------------------------
# The page.
#
# Upload a lecture, press one button, download three files. Every setting that
# used to live here is now a decision in src/app_defaults.py, because each one
# had a right answer that was the same on nearly every run — and a model picker
# or a chunk-size box is mostly a way to get a worse result by accident.
#
# Developer mode (DEVELOPER_MODE=1, or ?dev=1 in the URL) puts the overrides
# back, inside one collapsed expander.
# ---------------------------------------------------------------------------
DEV_MODE = developer_mode_enabled(dict(st.query_params))

# --- backend defaults; the normal page never shows or changes these ----------
SUBJECT = defaults.DEFAULT_SUBJECT
AREA = defaults.DEFAULT_UPSC_AREA
# Note language is NOT defaulted here. It is the one setting with no defensible
# default, so it starts empty and the picker below has to fill it; a value here
# would be exactly the silent default the picker exists to prevent.
LANGUAGE = ""
LANGUAGE_CHOSEN = False
MODE = "complete"
WORKFLOW = "upsc"
NOTE_TYPE = DEFAULT_UPSC_NOTE_TYPE
UPSC_OUTPUT_FORMAT = defaults.DEFAULT_OUTPUT_FORMAT
NOTE_DEPTH = defaults.DEFAULT_NOTE_DEPTH
FACT_MODE = DEFAULT_UPSC_FACT_MODE
QUALITY_LEVEL = defaults.DEFAULT_QUALITY_MODE
QUALITY_OPTION_VALUES = dict(DEFAULT_QUALITY_OPTIONS)
QC_OPTION_VALUES = dict(DEFAULT_ADVANCED_QC_OPTIONS)
GENERATE_QUALITY_REPORT = True
SHOW_CORRECTIONS = True
DCN_OPTIONS = defaults.dcn_layout_options()
SOURCE_OPTIONS = defaults.source_options()
GENERATE_ERROR_REPORT = defaults.GENERATE_ERROR_REPORT
ERROR_REPORT_STRICTNESS_VALUE = DEFAULT_ERROR_REPORT_STRICTNESS
ERROR_REPORT_TEMPLATE_PATH: str | None = None
TEMPLATE_PATH: str | None = None
DCN_LOGO_PATH: str | None = _default_dcn_logo_path()
SEND_IMAGES = True
STRICT_FILTER = True
IMAGE_MODE = "smart_crop"
DTP_POLICY = "keep_note_and_insert_image"
AI_REDRAW = False
IMAGE_MODEL = DEFAULT_IMAGE_MODEL

# These are sibling destinations, rather than two workflows grouped beneath a
# single Practice section. Keeping Notes first preserves the existing default
# page while DMP and DPP remain reachable without generating another lecture.
notes_tab, dmp_tab, dpp_tab = st.tabs((
    "Notes generation",
    "Daily Mains Practice (DMP)",
    "Daily Prelims Practice (DPP)",
))
# The existing Notes form and results handler are intentionally kept together
# below. Enter the tab once so their several early-return/conditional paths do
# not have to be duplicated just to change the page layout.
notes_tab.__enter__()

with st.container(border=True):
    # ---- Lecture file ----------------------------------------------------
    st.markdown(
        """<div class="panel-title">Upload lecture file <span
style="font-weight:400;opacity:.7">(optional)</span></div>
<div class="panel-copy">PPTX or PDF. The slides add the diagrams to insert and the correct
spelling of terms, Articles and schemes. Leave this empty for a class that has no deck —
the notes are then written from the recording alone.</div>""",
        unsafe_allow_html=True,
    )
    uploaded_files = st.file_uploader(
        "Upload lecture file(s)",
        type=["pdf", "pptx", "ppt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key=f"lecture_files_{st.session_state.uploader_key}",
    )

    # ---- Lecture video link ----------------------------------------------
    # One field, three services. The kind of link is detected rather than
    # chosen: a person with a lecture link should not have to know whether
    # their CDN is CloudFront, or what a direct video URL is.
    st.markdown(
        """<div class="panel-title" style="margin-top:18px">Lecture video link</div>
<div class="panel-copy">Paste the class recording link. The transcript is produced from it
automatically — there is nothing to download, extract or upload.<br>
<span style="opacity:.75">Supported: Google Drive &middot; YouTube &middot; direct video URL</span></div>""",
        unsafe_allow_html=True,
    )
    class_video_url = st.text_input(
        "Lecture video link",
        key=f"video_url_{st.session_state.uploader_key}",
        placeholder="Paste a Google Drive, YouTube or direct video link",
        label_visibility="collapsed",
    ).strip()

    # Detection is pure and instant, so the user is told what was recognised
    # while typing rather than after pressing Generate and waiting.
    VIDEO_SOURCE = detect_video_source(class_video_url) if class_video_url else ""
    if class_video_url and not VIDEO_SOURCE:
        st.caption(
            ":red[That link is not a supported lecture video link.] "
            "Paste a Google Drive share link, a YouTube link, or a direct video URL.")
    elif VIDEO_SOURCE == SOURCE_DIRECT_VIDEO:
        # "Direct video URL" is the catch-all, not a recognition: classification
        # is by host, and any host that is not YouTube or Drive lands here. A
        # CDN link, an article and a typo are indistinguishable until the
        # download starts, so this must not read as "we checked, it is a video"
        # — the user would only learn otherwise after a long wait.
        st.caption(
            "Not a YouTube or Google Drive link, so it will be **tried as a "
            "direct video link**. Whether it really is one is only known once "
            "the download starts.")
    elif VIDEO_SOURCE:
        st.caption(f"Detected: **{SOURCE_LABELS[VIDEO_SOURCE]}**")

    supplied_drive_transcript = ""
    if VIDEO_SOURCE == SOURCE_GOOGLE_DRIVE:
        with st.expander("Use existing Drive transcript (optional)"):
            st.caption(
                "If the transcript is visible in Drive but cannot be read here, "
                "copy its full text from the Transcript panel and paste it below. "
                "The notes will use this text directly and skip audio extraction.")
            supplied_drive_transcript = st.text_area(
                "Existing Drive transcript", height=200,
                key=f"drive_transcript_{st.session_state.uploader_key}_{class_video_url}",
                placeholder="Paste the full lecture transcript from Google Drive").strip()
    transcript_ready = (
        not supplied_drive_transcript
        or len(supplied_drive_transcript.split()) >= MIN_TRANSCRIPT_WORDS)
    if supplied_drive_transcript:
        if transcript_ready:
            st.caption("The pasted transcript will be used directly. Audio extraction will be skipped.")
        else:
            st.caption("Paste the full lecture transcript; this text is too short to generate notes.")

    # ---- Subject and practice identifiers --------------------------------
    # These belong to the finished notes and practice sheets, not to a file
    # name. The dedicated practice-number section keeps the two inputs visible
    # immediately before the note-generation controls.
    SUBJECT_NAME = st.text_input(
        "Subject name", key="subject_name",
        placeholder="Ethics, Integrity & Aptitude").strip()
    lecture_title_col, dcn_number_col = st.columns([4, 1])
    with lecture_title_col:
        LECTURE_TITLE = st.text_input(
            "Lecture title", key="lecture_title",
            placeholder="Probity in Governance, RTI and Citizens Charter").strip()
    with dcn_number_col:
        DCN_NO = st.text_input("DCN No.", key="dcn_no", placeholder="03").strip()
    st.markdown(
        """<div class="panel-title" style="margin-top:18px">Practice sheet numbers</div>
<div class="panel-copy">Optional identifiers printed on the DMP and DPP downloads.</div>""",
        unsafe_allow_html=True,
    )
    dmp_number_col, dpp_number_col = st.columns(2)
    with dmp_number_col:
        DMP_NO = st.text_input("DMP No.", key="dmp_no", placeholder="03").strip()
    with dpp_number_col:
        DPP_NO = st.text_input("DPP No.", key="dpp_no", placeholder="03").strip()
    # A typed subject is both the prompt's topic and the text printed above the
    # notes title. Leaving it blank preserves the existing default workflow.
    SUBJECT = SUBJECT_NAME or defaults.DEFAULT_SUBJECT

    # ---- Notes language ---------------------------------------------------
    # The one setting that stayed on the page. Everything else here has a right
    # answer that is the same on every run; this one does not, it is invisible
    # until the notes are already written, and getting it wrong wastes the whole
    # run.
    st.markdown(
        """<div class="panel-title" style="margin-top:18px">Note language <span
style="color:#b91c1c">*</span></div>
<div class="panel-copy">The language the notes are <em>written</em> in — independent of the
language the class was taught in. A Hindi class can produce Marathi notes.</div>""",
        unsafe_allow_html=True,
    )
    language_label = st.radio(
        "Select Note Language", [LANGUAGE_PROMPT, *LANGUAGE_CHOICES],
        index=0, horizontal=True, label_visibility="collapsed",
        key="notes_language",
    )
    LANGUAGE = LANGUAGE_CHOICES.get(language_label, "")
    LANGUAGE_CHOSEN = bool(LANGUAGE)

    AI_REDRAW = st.checkbox(
        "Make diagram backgrounds transparent", value=True,
        key="notes_transparent_diagrams",
        help="Automatically cleans diagrams before creating Word and PDF files. Image editing can add time.")

    # One link names ONE class, so a second lecture file has no recording of its
    # own and cannot be processed here. The run loop reads class_video_url
    # directly — there is only ever one class per run, with or without a deck.
    if len(uploaded_files or []) > 1:
        st.caption(
            ":orange[One video link belongs to one class.] Only the first file "
            "will be processed. Upload the other lectures separately, each with "
            "its own video link.")

    # ---- Developer options -----------------------------------------------
    if DEV_MODE:
        with st.expander("Developer Options", expanded=False):
            st.caption(
                "Overrides for this run. Hidden unless DEVELOPER_MODE=1 or ?dev=1 is "
                "in the URL - a normal user never sees any of it.")
            d1, d2, d3 = st.columns(3)
            with d1:
                area_labels = list(UPSC_AREAS)
                AREA = UPSC_AREAS[st.selectbox(
                    "UPSC area", area_labels,
                    index=area_labels.index("Mixed / Auto-detect"), key="dev_area")]
                # No language control here: it is on the page now, and this
                # block runs after it, so a second one would silently overrule
                # whatever the user chose above.
            with d2:
                depth_keys = list(UPSC_NOTE_DEPTHS)
                NOTE_DEPTH = st.selectbox(
                    "Note depth", depth_keys,
                    index=depth_keys.index(defaults.DEFAULT_NOTE_DEPTH),
                    format_func=lambda k: UPSC_NOTE_DEPTHS[k], key="dev_depth")
                quality_keys = list(QUALITY_LEVELS)
                QUALITY_LEVEL = st.selectbox(
                    "Quality check", quality_keys,
                    index=quality_keys.index(defaults.DEFAULT_QUALITY_MODE),
                    format_func=lambda k: QUALITY_LEVEL_LABELS[k], key="dev_quality")
                strictness_keys = list(ERROR_REPORT_STRICTNESS)
                ERROR_REPORT_STRICTNESS_VALUE = st.selectbox(
                    "Error report strictness", strictness_keys,
                    index=strictness_keys.index(DEFAULT_ERROR_REPORT_STRICTNESS),
                    format_func=lambda k: ERROR_REPORT_STRICTNESS[k], key="dev_strictness")
            with d3:
                SEND_IMAGES = st.checkbox("Send slide images to the AI", value=True,
                                          key="dev_vision")
                GENERATE_ERROR_REPORT = st.checkbox(
                    "Generate Error Report XLSX", value=defaults.GENERATE_ERROR_REPORT,
                    key="dev_error_report")
                for src_key, src_label in DCN_SOURCE_OPTIONS.items():
                    SOURCE_OPTIONS[src_key] = st.checkbox(
                        src_label, value=SOURCE_OPTIONS[src_key], key=f"dev_src_{src_key}")
            st.divider()
            u1, u2 = st.columns(2)
            with u1:
                TEMPLATE_PATH = _persist_upload(st.file_uploader(
                    "DCN layout template (.docx)", type=["docx"],
                    key=f"dev_template_{st.session_state.uploader_key}"), "template")
            with u2:
                ERROR_REPORT_TEMPLATE_PATH = _persist_upload(st.file_uploader(
                    "Error Report template (.xlsx)", type=["xlsx"],
                    key=f"dev_error_template_{st.session_state.uploader_key}"), "error_template")


    # The recording is the required input: it carries what was actually taught,
    # and notes written without it are not the product. The deck is optional —
    # many classes simply do not have one, and a run from the recording alone is
    # a complete set of notes minus the slide diagrams.
    # Two required things now: the recording, and a language. The language has
    # no defensible default — every option is wrong for most runs — and it is
    # invisible until the notes are already written, so it blocks the run rather
    # than being guessed.
    READY = bool(VIDEO_SOURCE) and LANGUAGE_CHOSEN and transcript_ready
    run_button = st.button(
        "Generate UPSC notes", type="primary", disabled=not READY,
        use_container_width=True)
    if not LANGUAGE_CHOSEN:
        st.caption("Please select the note language before generating notes.")
    elif not class_video_url:
        st.caption("Paste the lecture video link to generate notes. The PPT/PDF is optional.")
    elif not VIDEO_SOURCE:
        st.caption("The lecture video link is not a supported link.")
    elif not uploaded_files and not supplied_drive_transcript:
        st.caption(
            "No lecture file — the notes will be written from the recording alone "
            "(no slide diagrams).")

if run_button and not LANGUAGE_CHOSEN:
    st.error("Please select the note language before generating notes.")
elif run_button and READY:
    google_token = _google_proxy_token()
    # Re-check authorization at generate time as defense-in-depth (login already
    # gated on this). The PW proxy Whitelisted sheet is the single source of
    # truth; uses the cached result to avoid an extra proxy round-trip.
    access_status = _proxy_access_status()
    authorized = access_status == "allowed"
    if access_status == "denied":
        st.error(
            "Your Google account is not authorized for this app on the PW proxy. "
            "Ask an admin to add your email under the "
            f"'{PROXY_APP_NAME}' column of the Whitelisted sheet."
        )
    elif access_status == "error":
        # The PW proxy Whitelisted sheet is the ONLY source of truth — there is
        # no local fallback. If the proxy can't be reached, fail closed.
        st.error(
            "Couldn't verify your access with the PW proxy (it may be temporarily "
            "unreachable, or you need to sign in again). Please retry in a moment."
        )

    if authorized:
        st.session_state.results = []
        # One lecture per run: the video link names one class. The deck is
        # optional, so a run is (name, uploaded-file-or-None) rather than a file.
        if uploaded_files:
            jobs = [(uploaded_files[0].name, uploaded_files[0])]
        else:
            jobs = [("Class recording", None)]
        total = len(jobs)
        # Four coarse steps, the same four for every file. The pipeline reports
        # far more than this, but "Transcript source: YouTube captions (hi,
        # auto-generated) (14,663 words)" is not something the person who
        # uploaded a PPT can act on.
        # Transcribing a two-hour class is the one genuinely long wait, so the
        # ingestion stages are named individually instead of hiding inside
        # "Reading file" — a progress line that does not move for ten minutes
        # reads as a hang. No percentage is shown for them: the real progress is
        # not knowable, and a made-up number is worse than an honest stage name.
        STEPS = {
            "Extracting lecture": "Step 1/5: Reading lecture file",
            "Extracting lecture (recording only)": "Step 1/5: Preparing the lecture",
            "Detecting video source": "Step 2/5: Detecting video source",
            "Accessing video": "Step 2/5: Accessing the lecture video",
            "Fetching captions": "Step 2/5: Fetching video captions",
            # Drive-native path: no audio, no STT — the wait the user sees must
            # not claim there is.
            "Retrieving existing transcript": "Step 2/5: Retrieving the existing transcript",
            "Using supplied transcript": "Step 2/5: Using the supplied transcript",
            "Extracting audio": "Step 2/5: Extracting lecture audio",
            "Preparing transcript": "Step 2/5: Transcribing the lecture",
            "Selecting lecture diagrams": "Step 3/5: Picking the important diagrams",
            "Generating draft notes": "Step 3/5: Building notes",
            "Tightening notes to the page budget": "Step 3/5: Fitting the page budget",
            "Running structural checks": "Step 3/5: Building notes",
            "Creating DOCX/PDF": "Step 4/5: Creating DOCX/PDF",
            "Verifying layout": "Step 4/5: Creating DOCX/PDF",
            # Emitted between "Verifying layout" and "Building error report".
            # It was missing, so it fell through to the default below and the
            # counter ran 4/5 -> 3/5 -> 5/5. A step number that goes BACKWARDS
            # reads as "something failed and restarted".
            "Checking final formatting": "Step 4/5: Creating DOCX/PDF",
            "Building error report": "Step 5/5: Preparing downloads",
            "Preparing reports": "Step 5/5: Preparing downloads",
        }

        # How long the person actually waited. Monotonic, not wall clock: a
        # clock adjustment mid-run must not turn a ten-minute run negative.
        batch_started = time.monotonic()
        with st.status("Processing your lecture...", expanded=True) as status:
            progress = st.progress(0.0)
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, (job_name, uploaded) in enumerate(jobs, start=1):
                    progress.progress((i - 1) / total)
                    video_url = class_video_url
                    tmp_path = None
                    if uploaded is not None:
                        tmp_path = Path(tmpdir) / uploaded.name
                        tmp_path.write_bytes(uploaded.getbuffer())
                    stage_line = st.empty()

                    def _show_stage(label: str, _slot=stage_line):
                        # Two stages count their own parts — transcription and
                        # note building. Keep their "(3 of 6)" shape: they are
                        # the two long waits, and a line that never changes
                        # during them reads as a hang.
                        friendly = STEPS.get(label)
                        if friendly is None:
                            if label.startswith("Transcribing the lecture"):
                                friendly = f"Step 2/5: {label}"
                            elif label.startswith("Building notes"):
                                friendly = f"Step 3/5: {label}"
                            elif label.startswith("Editing diagrams"):
                                friendly = f"Step 4/5: {label}"
                        _slot.write(friendly or "Step 3/5: Building notes")

                    job_started = time.monotonic()
                    try:
                        # Fetched per job, not once per batch: a token lives an
                        # hour, and a batch of classes can run longer than that.
                        # Refresh failures belong to this job's error handler.
                        drive_token, drive_account = (
                            _drive_access()
                            if video_url and not supplied_drive_transcript
                            and detect_video_source(video_url) == SOURCE_GOOGLE_DRIVE
                            else ("", ""))
                        result = run_pipeline(
                            tmp_path, subject=SUBJECT, language=LANGUAGE, mode=MODE,
                            google_token=google_token, model=DEFAULT_MODEL,
                            send_images_to_ai=SEND_IMAGES, strict_filter=STRICT_FILTER,
                            allow_mock=False, image_insert_mode=IMAGE_MODE, dtp_note_policy=DTP_POLICY,
                            ai_redraw_diagrams=AI_REDRAW, image_model=IMAGE_MODEL,
                            video_url=video_url,
                            transcript_text_override=supplied_drive_transcript,
                            drive_token=drive_token, drive_account=drive_account,
                            workflow=WORKFLOW, note_type=NOTE_TYPE, area=AREA,
                            quality_level=QUALITY_LEVEL, quality_options=QUALITY_OPTION_VALUES,
                            generate_quality_report=GENERATE_QUALITY_REPORT,
                            show_corrections=SHOW_CORRECTIONS,
                            qc_options=QC_OPTION_VALUES,
                            upsc_output_format=UPSC_OUTPUT_FORMAT,
                            note_depth=NOTE_DEPTH,
                            fact_mode=FACT_MODE,
                            dcn_options=DCN_OPTIONS,
                            template_path=TEMPLATE_PATH,
                            logo_path=DCN_LOGO_PATH,
                            generate_error_report=GENERATE_ERROR_REPORT,
                            error_report_strictness=ERROR_REPORT_STRICTNESS_VALUE,
                            error_report_template_path=ERROR_REPORT_TEMPLATE_PATH,
                            dcn_no=DCN_NO,
                            lecture_title=LECTURE_TITLE,
                            dmp_no=DMP_NO,
                            dpp_no=DPP_NO,
                            source_options=SOURCE_OPTIONS,
                            stage_callback=_show_stage,
                        )
                        result_dict = _result_to_dict(job_name, result, UPSC_OUTPUT_FORMAT)
                        result_dict["tracking"] = _usage_tracking_label(result)
                        result_dict["elapsed_seconds"] = time.monotonic() - job_started
                        st.session_state.results.append(result_dict)
                    except GeminiError as e:
                        st.session_state.results.append({
                            "name": job_name, "notes": "", "run_dir": None, "tracking": None,
                            "elapsed_seconds": time.monotonic() - job_started,
                            "error": "The notes could not be generated just now. "
                                     "Please try again in a moment.",
                            **_error_details(e)})
                    except Exception as e:
                        st.session_state.results.append({
                            "name": job_name, "notes": "", "run_dir": None, "tracking": None,
                            "elapsed_seconds": time.monotonic() - job_started,
                            "error": _friendly_error(e), **_error_details(e)})
                    progress.progress(i / total)
            ok = sum(1 for r in st.session_state.results if not r.get("error"))
            batch_elapsed = format_duration(time.monotonic() - batch_started)
            status.update(
                label=f"Notes generated successfully in {batch_elapsed}." if ok == total
                      else f"Finished {ok} of {total} file(s) in {batch_elapsed}.",
                state="complete" if ok else "error",
                expanded=False,
            )



results = st.session_state.results
if results:
    st.divider()
    # Do not announce success over a run that produced no notes, or that failed
    # for one file out of several. The banner is the first thing read, and a
    # green one stops people looking at the card underneath it.
    _failed = [r for r in results if r.get("error")]
    _no_docx = [r for r in results if not r.get("error") and not r.get("docx")]
    if _failed or _no_docx:
        _bad = len(_failed) + len(_no_docx)
        st.warning(
            f"{len(results) - _bad} of {len(results)} file(s) finished. "
            f"The reason for each problem is shown below.")
    else:
        st.success("Notes generated successfully. Download your files:")
    for idx, r in enumerate(results):
        header = r["name"] if len(results) > 1 else "Your files"
        with st.expander(header, expanded=True):
            if r.get("elapsed_seconds") is not None:
                st.caption(
                    f"Notes generation time: {format_duration(r['elapsed_seconds'])}")
            if r.get("error"):
                # Headline, then the actual cause. The cause is shown to
                # everyone, not just developers: the person who can act on
                # "ffmpeg is not on PATH" is the person at the machine.
                st.error(r["error"])
                if r.get("error_reason"):
                    st.caption(f"Reason: {r['error_reason']}")
                if r.get("run_dir"):
                    st.caption(f"Run folder: `{r['run_dir']}`")
                if r.get("error_trace"):
                    with st.expander("Technical details", expanded=False):
                        st.caption(
                            "Send this to whoever maintains the app if the reason "
                            "above is not enough to act on.")
                        st.code(r["error_trace"], language="text")
                continue

            # ---- Downloads ---------------------------------------------------
            # Exactly three. Each button reads a path stored when the run
            # finished, so a click re-serves that file and can never re-enter
            # run_pipeline.
            d1, d2, d3 = st.columns(3)
            with d1:
                _download_from_path(
                    "Download Notes DOCX", r.get("docx"), f"docx_{idx}",
                    "The notes file could not be created.",
                    _missing_file_reason(r, "docx"),
                )
            with d2:
                _download_from_path(
                    "Download Notes PDF", r.get("pdf"), f"pdf_{idx}",
                    "PDF could not be created on this machine. Please download the DOCX.",
                    _missing_file_reason(r, "pdf"),
                )
            with d3:
                _download_from_path(
                    "Download Error Report XLSX", r.get("error_xlsx"), f"errxlsx_{idx}",
                    "The error report could not be created.",
                    _missing_file_reason(r, "xlsx"),
                )
                # Second file, same findings: the sheet sorts and filters, this
                # one carries each error's timestamp / slide / page so a
                # reviewer can check it against the class.
                if r.get("error_detailed"):
                    _download_from_path(
                        "Download Error Report (with locations)",
                        r.get("error_detailed"), f"errloc_{idx}",
                        "", "",
                    )

            # ---- What this document is not ----------------------------------
            # Shown to everyone, not just developers, and only when there is
            # something to say. These are the cases where the document differs
            # from what the page promised — over the page limit, diagrams the
            # slides had and the notes do not — and a run where that happened
            # used to look exactly like a run where it did not.
            _render_run_notices(r)

            # ---- Error report summary ---------------------------------------
            # Counts only. The sheet is the report; printing it here would
            # recreate the wall of text this whole change exists to remove.
            _render_error_report_summary(r)

            # ---- Practice from these notes ----------------------------------
            # The run that just finished is the one a teacher wants to practise
            # on, so both builders are here, in the order they are used, rather
            # than only at the top of the page behind a lecture picker. The
            # tabs up there remain the way in when this flow is not available —
            # an older lecture, or a run whose folder has since been cleaned up.
            if r.get("run_dir"):
                st.markdown("**Practice from these notes**")
                dmp_here, dpp_here = st.tabs((
                    "Daily Mains Practice (DMP)",
                    "Daily Prelims Practice (DPP)",
                ))
                with dmp_here:
                    _render_dmp_panel(r, idx, embedded=True)
                with dpp_here:
                    _render_dpp_panel(r, idx, embedded=True)

            # ---- Everything else is developer-only ---------------------------
            if DEV_MODE:
                _render_dcn_render_status(r.get("render") or {})
                _render_developer_downloads(r, idx)
                warnings_list = r.get("warnings") or []
                with st.expander(f"Run details ({len(warnings_list)} warnings)", expanded=False):
                    st.caption(f"Run folder: `{r.get('run_dir') or 'unknown'}`")
                    for warning in warnings_list:
                        st.write(f"- {warning}")
                    if r.get("notes"):
                        st.markdown("**Raw notes**")
                        st.code(r["notes"][:20000], language="markdown")


notes_tab.__exit__(None, None, None)

with dmp_tab:
    _render_dmp_workspace()

with dpp_tab:
    _render_dpp_workspace()
