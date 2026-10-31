"""UPSC PDF QC — upload notes PDFs, run QC, download the report.

Deliberately minimal. There are three controls on this page: choose files, run,
download. No model picker, no thresholds, no prompts, no strictness dial, no
paths, no debug panel — the backend defaults are the note-generation app's
defaults and are not something a reviewer should be asked about.

The sign-in flow is the note-generation app's, unchanged: Google via Streamlit's
native OIDC, restricted to @pw.live, then authorised against the PW proxy's
Whitelisted sheet. The proxy holds the Gemini key; this app ships none.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import streamlit as st

import pw_access
from src import proxy_session
from src.auth_session import session_expired
from src.config import (
    APP_NAME,
    OUTPUTS_DIR,
    header_datetime,
    version_label,
)
from src.input_normalize import UPLOADER_TYPES, describe_support
from src.pipeline import run_pdf_qc_batch

st.set_page_config(page_title=APP_NAME, page_icon="🔍", layout="centered")

ALLOWED_EMAIL_DOMAIN = "pw.live"
PROXY_APP_NAME = pw_access.APP_NAME
ALLOWLIST_CACHE_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# sign-in (copied from the note-generation app)
# ---------------------------------------------------------------------------

def _current_user_email() -> str:
    try:
        return str(st.user.get("email", "") or "").strip().lower()
    except Exception:
        return ""


def _google_proxy_token() -> str:
    """The Google token passed to the PW proxy. The proxy verifies an id_token
    (a JWT carrying the email), so prefer that and fall back to the access
    token. Requires `expose_tokens = ["access", "id"]` in secrets."""
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


def _proxy_access_status(force: bool = False) -> str:
    """"allowed" / "denied" / "error" for the signed-in user, cached briefly."""
    email = _current_user_email()
    now = time.time()
    cache = st.session_state.get("_allow_cache")
    if (not force and isinstance(cache, dict) and cache.get("status") == "allowed"
            and cache.get("email") == email
            and (now - float(cache.get("ts", 0))) < ALLOWLIST_CACHE_TTL_SECONDS):
        return "allowed"
    proxy_session.attach(email)
    status = pw_access.check_allowed_status(_google_proxy_token())
    proxy_session.persist(email)
    st.session_state["_allow_cache"] = {"email": email, "status": status, "ts": now}
    return status


def _logout() -> None:
    proxy_session.clear()
    st.session_state.pop("_allow_cache", None)
    if bool(getattr(st.user, "is_logged_in", False)):
        st.logout()
    st.rerun()


def _header() -> None:
    st.markdown(
        f"<div style='text-align:center;padding:0.5rem 0 1.5rem'>"
        f"<div style='font-size:1.9rem;font-weight:700;letter-spacing:-0.01em'>{APP_NAME}</div>"
        f"<div style='opacity:0.75;font-size:0.95rem;margin-top:0.15rem'>{version_label()}</div>"
        f"<div style='opacity:0.6;font-size:0.85rem;margin-top:0.1rem'>{header_datetime()}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _require_allowed_google_user() -> None:
    if not bool(getattr(st.user, "is_logged_in", False)):
        _header()
        st.write("Sign in with your @pw.live Google account to check notes PDFs.")
        if st.button("Continue with Google", type="primary", use_container_width=True):
            st.login("google")
        st.stop()

    try:
        expired = session_expired(st.user)
    except Exception:
        expired = False
    if expired:
        _logout()
        st.stop()

    email = _current_user_email()
    if not email or not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        _header()
        st.error(f"Please sign in with your @{ALLOWED_EMAIL_DOMAIN} Google account.")
        if email:
            st.caption(f"Signed in as {email}")
        if st.button("Sign out"):
            _logout()
        st.stop()

    status = _proxy_access_status()
    if status != "allowed":
        _header()
        if status == "denied":
            st.error(
                "This account isn't authorized for this app. Ask an admin to add "
                f"your email to the '{PROXY_APP_NAME}' column of the Whitelisted sheet."
            )
        else:
            st.error(
                "Couldn't verify your access with the PW proxy (it may be "
                "temporarily unreachable, or your sign-in may have lapsed). "
                "Try again in a moment, or sign in again."
            )
        st.caption(f"Signed in as {email}")
        cols = st.columns(2)
        with cols[0]:
            if status == "error" and st.button("Sign in again", type="primary",
                                               use_container_width=True):
                st.login("google")
        with cols[1]:
            if st.button("Sign out", use_container_width=True):
                _logout()
        st.stop()


_require_allowed_google_user()
_header()

with st.sidebar:
    st.caption(f"Signed in as {_current_user_email()}")
    if st.button("Sign out", use_container_width=True):
        _logout()


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

st.subheader("Upload UPSC Notes File(s)")
uploaded = st.file_uploader(
    "Upload Files",
    type=UPLOADER_TYPES,
    accept_multiple_files=True,
    label_visibility="collapsed",
)

if uploaded:
    st.markdown("**Selected:**")
    for f in uploaded:
        st.markdown(f"✓ {f.name}")

run_clicked = st.button("RUN QC", type="primary", disabled=not uploaded,
                        use_container_width=True)
if not uploaded:
    st.caption(f"Choose one or more UPSC notes files to check. Accepts {describe_support()}. Anything that is not a PDF is converted to one before checking.")

if run_clicked and uploaded:
    token = _google_proxy_token()
    if _proxy_access_status() != "allowed":
        st.error("Couldn't verify your access with the PW proxy. Please try again.")
        st.stop()

    total = len(uploaded)
    progress = st.progress(0.0)
    line = st.empty()
    detail = st.empty()

    state = {"index": 0, "name": ""}

    def _on_file(index: int, of: int, name: str, finished: bool = False) -> None:
        state["index"], state["name"] = index, name
        if finished:
            # Several files are in flight, so what is knowable is how many are
            # done — not which one is "current".
            progress.progress(index / of)
            line.markdown(f"**Checked {index} / {of}**")
            detail.write(f"finished {name}")
        else:
            progress.progress((index - 1) / of)
            line.markdown(f"**Processing {index} / {of}**")
            detail.write(name)

    def _on_stage(label: str) -> None:
        # Stage names only. No logs, no model chatter, no file paths.
        detail.write(f"{state['name']} — {label.lower()}")

    with tempfile.TemporaryDirectory(prefix="upsc_pdf_qc_") as tmpdir:
        paths: list[Path] = []
        for f in uploaded:
            p = Path(tmpdir) / f.name
            p.write_bytes(f.getbuffer())
            paths.append(p)

        try:
            run = run_pdf_qc_batch(
                paths,
                google_token=token,
                output_dir=OUTPUTS_DIR,
                progress_callback=_on_file,
                stage_callback=_on_stage,
            )
        except Exception as e:
            progress.empty()
            line.empty()
            detail.empty()
            st.error(f"QC could not be completed: {e}")
            st.stop()

    progress.progress(1.0)
    line.empty()
    detail.empty()
    st.session_state["qc_run"] = {
        "workbook": str(run.workbook_path) if run.workbook_path else "",
        "rows": [
            {
                "name": r.file_name,
                "status": r.status,
                "errors": int((r.error_report_summary or {}).get("total", 0)),
                "error": r.error,
                # Without these the page cannot tell a checked file from one
                # that was only partly checked, and would print a tick for both.
                "complete": bool(getattr(r, "error_report_complete", True)),
                "report_status": r.error_report_status,
                "coverage": [w for w in r.warnings
                             if w.startswith("INCOMPLETE COVERAGE")],
            }
            for r in run.results
        ],
    }

run_state = st.session_state.get("qc_run")
if run_state:
    st.success("QC completed")

    for row in run_state["rows"]:
        # A tick is a claim that the file was checked. It is only printed when
        # that is true: a partial scan, an unreadable AI reply and a skipped
        # content check each get their own marker, because a green tick beside
        # "0 content errors" is read as "this file is clean" — and for those
        # three outcomes that is exactly what nobody knows.
        if row["status"] == "failed":
            st.markdown(f"✗ **{row['name']}** — QC failed: {row['error']}")
            continue

        report_status = row.get("report_status", "")
        if not row.get("complete", True):
            st.markdown(
                f"⚠️ **{row['name']}** — **PARTIAL SCAN**: {row['errors']} error(s) "
                f"found *so far*. Part of this file was not checked — the count "
                f"is a floor, not a total.")
            for note in row.get("coverage", [])[:3]:
                st.caption(note)
        elif report_status in ("missing", "parse_failed", "failed"):
            st.markdown(
                f"⚠️ **{row['name']}** — the AI's check did not produce a readable "
                f"report ({report_status.replace('_', ' ')}). This is NOT a "
                f"clean result — see the workbook.")
        elif report_status in ("unavailable", "disabled"):
            st.markdown(
                f"⚠️ **{row['name']}** — check not run ({report_status}). The notes "
                f"were NOT checked for errors.")
        elif report_status == "filtered_out":
            st.markdown(
                f"⚠️ **{row['name']}** — findings were reported but all were out "
                f"of scope at this strictness level, so none are listed.")
        elif report_status == "all_refuted":
            st.markdown(
                f"✓ **{row['name']}** — no confirmed errors (every claimed error "
                f"was rejected on review)")
        else:
            st.markdown(
                f"✓ **{row['name']}** — {row['errors']} error(s)")

    workbook = run_state.get("workbook")
    if workbook and Path(workbook).exists():
        st.download_button(
            "Download Error Report",
            data=Path(workbook).read_bytes(),
            file_name=Path(workbook).name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )
    else:
        st.error("The report could not be written. Please run QC again.")
