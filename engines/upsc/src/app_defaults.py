"""Everything the frontend used to ask, decided here instead.

The app has one job: take a lecture, give back notes. Every setting that used to
be a control on the page is a decision with a right answer, and the right answer
is the same on almost every run — detailed notes, strict checking, slides as the
source of truth, DCN layout, error report on.

Leaving those on the page cost more than it gave. A model picker, a chunk-size
box and a quality radio ask the user to have an opinion about machinery they
cannot see the effects of, and every one of them is a way to get a worse result
by accident.

Each value can still be overridden by an environment variable, which is how a
developer changes one without touching the UI. Nothing here is read at import
time by the pipeline — the app resolves these and passes them in, so a test can
call run_pipeline with whatever it likes.
"""
from __future__ import annotations

import os

__all__ = [
    "APP_MODE", "DEFAULT_OUTPUT_FORMAT", "DEFAULT_NOTE_DEPTH", "DEFAULT_QUALITY_MODE",
    "GENERATE_ERROR_REPORT", "SHOW_DEVELOPER_OPTIONS", "AUTO_DETECT_LANGUAGE",
    "AUTO_DETECT_UPSC_AREA", "SLIDES_SOURCE_OF_TRUTH", "TRANSCRIPT_FOR_EXPLANATION_ONLY",
    "AUTO_REPAIR_LAYOUT", "CONVERT_WIDE_TABLES_TO_BULLETS", "SHOW_ONLY_FINAL_DOWNLOADS",
    "DEFAULT_SUBJECT", "DEFAULT_UPSC_AREA", "DEFAULT_LANGUAGE",
    "developer_mode_enabled", "source_options", "dcn_layout_options",
]


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _text(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


# --- what the app is ---------------------------------------------------------
APP_MODE = _text("APP_MODE", "upsc_dcn")
DEFAULT_OUTPUT_FORMAT = _text("DEFAULT_OUTPUT_FORMAT", "dcn")
DEFAULT_SUBJECT = _text("DEFAULT_SUBJECT", "upsc")
DEFAULT_UPSC_AREA = _text("DEFAULT_UPSC_AREA", "auto")
#: Which language the notes are written in, before the user touches the control.
#: "English", "Hindi", or "Auto" to follow the language the class was taught in.
#: English rather than Auto: Auto reads the transcript, so a Hindi-medium class
#: with English slides silently produced Hindi notes for someone who wanted
#: English. A wrong default the user can see and change beats a clever one they
#: cannot.
DEFAULT_LANGUAGE = _text("DEFAULT_LANGUAGE", "English")

# --- how the notes are written ----------------------------------------------
DEFAULT_NOTE_DEPTH = _text("DEFAULT_NOTE_DEPTH", "detailed")
DEFAULT_QUALITY_MODE = _text("DEFAULT_QUALITY_MODE", "strict")
GENERATE_ERROR_REPORT = _flag("GENERATE_ERROR_REPORT", True)

# --- how the sources are used ------------------------------------------------
AUTO_DETECT_LANGUAGE = _flag("AUTO_DETECT_LANGUAGE", True)
AUTO_DETECT_UPSC_AREA = _flag("AUTO_DETECT_UPSC_AREA", True)
SLIDES_SOURCE_OF_TRUTH = _flag("SLIDES_SOURCE_OF_TRUTH", True)
TRANSCRIPT_FOR_EXPLANATION_ONLY = _flag("TRANSCRIPT_FOR_EXPLANATION_ONLY", True)

# --- how problems are handled ------------------------------------------------
AUTO_REPAIR_LAYOUT = _flag("AUTO_REPAIR_LAYOUT", True)
CONVERT_WIDE_TABLES_TO_BULLETS = _flag("CONVERT_WIDE_TABLES_TO_BULLETS", True)

# --- what the page shows -----------------------------------------------------
SHOW_ONLY_FINAL_DOWNLOADS = _flag("SHOW_ONLY_FINAL_DOWNLOADS", True)
SHOW_DEVELOPER_OPTIONS = _flag("SHOW_DEVELOPER_OPTIONS", False)


def developer_mode_enabled(query_params: dict | None = None) -> bool:
    """Developer mode, from the environment or a ``?dev=1`` in the URL.

    Two switches rather than one because they serve different people: the
    environment variable is for a machine that is always a developer's, the URL
    parameter is for looking at one run on a machine that is not.
    """
    if SHOW_DEVELOPER_OPTIONS:
        return True
    if _flag("DEVELOPER_MODE", False) or _flag("UPSC_DEVELOPER_MODE", False):
        return True
    if not query_params:
        return False
    value = query_params.get("dev")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def source_options() -> dict:
    """The slides/transcript/repair flags, as run_pipeline expects them."""
    return {
        "slides_source_of_truth": SLIDES_SOURCE_OF_TRUTH,
        "transcript_explanation_only": TRANSCRIPT_FOR_EXPLANATION_ONLY,
        "auto_repair_layout": AUTO_REPAIR_LAYOUT,
        "tables_to_bullets": CONVERT_WIDE_TABLES_TO_BULLETS,
    }


def dcn_layout_options() -> dict:
    """The DCN layout toggles. All on — they are what the reference looks like."""
    return {
        "use_notes_box": True,
        "add_footer_strip": True,
        "embed_corrections": True,
        "separate_quality_report": True,
        "render_verify": True,
    }
