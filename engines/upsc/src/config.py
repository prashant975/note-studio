from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT_DIR = Path(os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()
ENV_PATH = Path(os.getenv("HANDWRITTEN_NOTES_ENV", str(ROOT_DIR / ".env"))).expanduser()
if load_dotenv:
    load_dotenv(ENV_PATH)


def extract_api_key(value: str | None) -> str:
    """Accept raw key, full URL, or pasted curl and return only the API key."""
    if not value:
        return ""
    v = str(value).strip().strip('"').strip("'")
    if not v:
        return ""
    v = v.replace("\\\n", " ").replace("\n", " ").strip()
    m = re.search(r"[?&]key=([^\s'\"&]+)", v)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m = re.search(r"x-goog-api-key\s*[:=]\s*([^\s'\"]+)", v, flags=re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m = re.search(r"(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*([^\s'\"]+)", v, flags=re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return v


def mask_secret(value: str | None) -> str:
    key = extract_api_key(value)
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# AI provider keys no longer live in this app. All Gemini calls go through the
# shared PW proxy (see pw_access.py), which holds GEMINI_API_KEY on its side.
# The env vars below only pick which model the proxy should call — they are NOT
# secrets. They are named without a provider prefix so the onboarding key-scan
# never mistakes them for a leaked key.
#
# A model name starting with "gpt" is served by the proxy's GPT route, anything
# else by its Gemini route (see src/ai_client.py).
DEFAULT_MODEL = (os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")).strip() or "gemini-3.5-flash"
# Notes generate on DEFAULT_MODEL. If that model is unavailable (404 — not
# served in this project/region) or stays capacity-choked after pw_access has
# exhausted its own 429/503 retries, the run falls back to this model once
# rather than failing. Set FALLBACK_MODEL_NAME="" to disable the fallback.
_fallback_env = os.getenv("FALLBACK_MODEL_NAME")
DEFAULT_FALLBACK_MODEL = (_fallback_env if _fallback_env is not None else "gemini-3.6-flash").strip()
DEFAULT_IMAGE_MODEL = (os.getenv("IMAGE_MODEL_NAME") or os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")).strip() or "gemini-2.5-flash-image"
# The proxy has no GPT speech route, so lecture audio is transcribed on Gemini
# whenever MODEL_NAME is a GPT model.
DEFAULT_AUDIO_MODEL = (os.getenv("AUDIO_MODEL_NAME") or "gemini-3.5-flash").strip() or "gemini-3.5-flash"
_audio_fallback_env = os.getenv("AUDIO_FALLBACK_MODEL_NAME")
DEFAULT_AUDIO_FALLBACK_MODEL = (_audio_fallback_env if _audio_fallback_env is not None else "gemini-3.6-flash").strip()

RUNS_DIR = Path(os.getenv("RUNS_DIR", str(ROOT_DIR / "runs"))).expanduser()
OUTPUTS_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT_DIR / "outputs"))).expanduser()

# Product / display name (UI titles, output branding, page title). Purely
# cosmetic — safe to change. NOTE: the name the PW *proxy* uses to authorize
# users and log usage is separate (the APP_NAME literal in pw_access.py) so
# that renaming the product here never locks users out of the shared whitelist.
APP_NAME = os.getenv("APP_NAME", "UPSC_Notes")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "y"}

# Build identity shown in the app header, so a teammate reporting a problem can
# say which build they are on. Bump BOTH on every release you hand out.
# APP_VERSION_DATE is ISO (YYYY-MM-DD); the UI renders it as "28 Jul 2026".
APP_VERSION = "2.13.3"
APP_VERSION_DATE = "2026-10-12"

#: Every timestamp the app shows or logs is Asia/Kolkata — the team is in IST,
#: and a UTC stamp in a run log has repeatedly been read as local time.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def stamp_ist() -> dict:
    """Version, date and time for the sidebar and for run_log.json."""
    now = now_ist()
    return {
        "app": APP_NAME,
        "version": f"v{APP_VERSION}",
        "date": now.strftime("%d-%m-%Y"),
        "time": now.strftime("%I:%M:%S %p") + " IST",
        "timezone": "Asia/Kolkata",
        "iso": now.isoformat(timespec="seconds"),
    }


def version_label() -> str:
    """"v1.0.0 · 28 Jul 2026" — falls back to the raw ISO date if it can't be
    parsed, so a typo in APP_VERSION_DATE can never break the header."""
    from datetime import date

    try:
        pretty = date.fromisoformat(APP_VERSION_DATE).strftime("%d %b %Y")
    except ValueError:
        pretty = APP_VERSION_DATE
    return f"v{APP_VERSION} · {pretty}"


def build_label() -> str:
    """"Build 2026.07.28 · UPSC_Notes" — the sidebar footer stamp. Dotted date
    so it sorts and reads as a build number rather than a calendar date."""
    return f"Build {APP_VERSION_DATE.replace('-', '.')} · {APP_NAME}"

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt"}
#: Accepted spellings of a note language. src/languages.py is the authority
#: on what a code MEANS; this stays because callers pass UI labels here.
LANGUAGE_CODES = {
    "English": "en", "Hindi": "hi", "Marathi": "mr",
    "en": "en", "hi": "hi", "mr": "mr",
}

# UPSC General Studies subject set. Keys are single tokens used in prompt file
# names / auto-detection; labels are shown in the UI and on the notes cover.
SUBJECTS = [
    "polity", "economy", "geography", "ancient", "medieval", "modern",
    "culture", "environment", "science", "ir", "society", "security", "ethics",
]
SUBJECT_LABELS = {
    "polity": "Indian Polity & Governance",
    "economy": "Indian Economy",
    "geography": "Geography",
    "ancient": "Ancient History",
    "medieval": "Medieval History",
    "modern": "Modern History & Freedom Struggle",
    "culture": "Art & Culture",
    "environment": "Environment & Ecology",
    "science": "Science & Technology",
    "ir": "International Relations",
    "society": "Indian Society",
    "security": "Internal Security",
    "ethics": "Ethics, Integrity & Aptitude",
    # Broader UI-facing areas. "history" is the umbrella an aspirant picks when
    # the class is not clearly ancient/medieval/modern; auto-detection may still
    # narrow it to one of those finer keys.
    "history": "History",
    "governance": "Governance & Public Administration",
    "current_affairs": "Current Affairs",
}

#: The same subjects in Devanagari, for the subject line at the top of a Hindi
#: note. A key missing here falls back to the English label rather than to a
#: machine translation — a wrong subject name on the cover of a student's notes
#: is worse than an English one.
SUBJECT_LABELS_HI = {
    "polity": "भारतीय राजव्यवस्था एवं शासन",
    "economy": "भारतीय अर्थव्यवस्था",
    "geography": "भूगोल",
    "ancient": "प्राचीन भारत का इतिहास",
    "medieval": "मध्यकालीन भारत का इतिहास",
    "modern": "आधुनिक भारत एवं स्वतंत्रता संग्राम",
    "culture": "कला एवं संस्कृति",
    "environment": "पर्यावरण एवं पारिस्थितिकी",
    "science": "विज्ञान एवं प्रौद्योगिकी",
    "ir": "अंतर्राष्ट्रीय संबंध",
    "society": "भारतीय समाज",
    "security": "आंतरिक सुरक्षा",
    "ethics": "नीतिशास्त्र, सत्यनिष्ठा एवं अभिरुचि",
    "history": "इतिहास",
    "governance": "शासन एवं लोक प्रशासन",
    "current_affairs": "समसामयिकी",
}

#: The same subjects in Marathi. Marathi and Hindi share Devanagari and most of
#: this Sanskrit-derived vocabulary, so several entries are identical — but not
#: all of them ("राज्यव्यवस्था", "अंतरराष्ट्रीय", "समकालीन घडामोडी" differ), and a
#: Marathi note carrying a Hindi cover line is exactly the kind of near-miss a
#: student notices. Written out in full rather than diffed against the Hindi
#: table, so a later edit to one cannot silently change the other.
SUBJECT_LABELS_MR = {
    "polity": "भारतीय राज्यव्यवस्था व शासन",
    "economy": "भारतीय अर्थव्यवस्था",
    "geography": "भूगोल",
    "ancient": "प्राचीन भारताचा इतिहास",
    "medieval": "मध्ययुगीन भारताचा इतिहास",
    "modern": "आधुनिक भारत व स्वातंत्र्यलढा",
    "culture": "कला व संस्कृती",
    "environment": "पर्यावरण व पारिस्थितिकी",
    "science": "विज्ञान व तंत्रज्ञान",
    "ir": "आंतरराष्ट्रीय संबंध",
    "society": "भारतीय समाज",
    "security": "अंतर्गत सुरक्षा",
    "ethics": "नीतिशास्त्र, सचोटी व अभिवृत्ती",
    "history": "इतिहास",
    "governance": "शासन व लोक प्रशासन",
    "current_affairs": "चालू घडामोडी",
}

# "complete" = detailed Prelims+Mains notes; "summary" = quick revision notes.
MODES = ["complete", "summary"]
MODE_LABELS = {"complete": "Detailed notes", "summary": "Revision notes"}

# ---------------------------------------------------------------------------
# UPSC note types (new pipeline). Separate from MODES so the older
# complete/summary path keeps working untouched.
# ---------------------------------------------------------------------------
UPSC_NOTE_TYPES = ["complete", "concise", "mains_ready", "prelims_ready", "revision"]
UPSC_NOTE_TYPE_LABELS = {
    "complete": "Complete Notes",
    "concise": "Concise Notes",
    "mains_ready": "Mains-ready Notes",
    "prelims_ready": "Prelims-ready Notes",
    "revision": "Revision Notes",
}

# UI-facing area picker: label -> internal subject key. Ordered, because the UI
# renders it in this exact sequence. "auto" means "let language/subject
# detection decide" and has no SUBJECT_LABELS entry by design.
UPSC_AREAS = {
    "Polity": "polity",
    "Economy": "economy",
    "History": "history",
    "Geography": "geography",
    "Environment": "environment",
    "Science & Tech": "science",
    "International Relations": "ir",
    "Ethics": "ethics",
    "Society": "society",
    "Governance": "governance",
    "Current Affairs": "current_affairs",
    "Mixed / Auto-detect": "auto",
}

# How hard the AI should fact-check the faculty's points. "off" skips checking
# entirely; "strict" is the default because a wrong fact in UPSC notes costs
# more than an over-cautious review flag.
QUALITY_LEVELS = ["off", "basic", "strict"]
QUALITY_LEVEL_LABELS = {"off": "OFF", "basic": "Basic", "strict": "Strict"}

# Individual quality toggles shown as checkboxes. check_* ask the AI to look for
# a problem; add_* ask it to enrich the notes.
QUALITY_OPTIONS = {
    "check_facts": "Fact-check faculty points",
    "check_concept_flow": "Check concept flow",
    "check_missing_context": "Check missing context",
    "check_factual_mismatch": "Check factual mismatch",
    "check_outdated": "Check outdated information",
    "check_duplicates": "Check duplicated/repeated points",
    "add_correction_pointers": "Add correction pointers in notes",
    "add_tables": "Add tables where useful",
    "add_flowcharts": "Add flowcharts where useful",
    "add_upsc_relevance": "Add UPSC exam relevance",
    "add_prelims_keywords": "Add Prelims keywords",
    "add_mains_value": "Add Mains value-add points",
    "add_examples": "Add examples/case studies if relevant",
}

# Everything is ON by default: the default level is Strict, and the add_* items
# are the core UPSC value the notes are generated for.
DEFAULT_QUALITY_OPTIONS = {
    "check_facts": True,
    "check_concept_flow": True,
    "check_missing_context": True,
    "check_factual_mismatch": True,
    "check_outdated": True,
    "check_duplicates": True,
    "add_correction_pointers": True,
    "add_tables": True,
    "add_flowcharts": True,
    "add_upsc_relevance": True,
    "add_prelims_keywords": True,
    "add_mains_value": True,
    "add_examples": True,
}

DEFAULT_QUALITY_LEVEL = "strict"
DEFAULT_UPSC_NOTE_TYPE = "complete"

# ---------------------------------------------------------------------------
# UPSC output format. "dcn" reproduces the PW OnlyIAS Daily Class Notes layout
# (green centred title, red/blue/black heading ladder, right-hand "Space for
# Notes" box, footer strip) and is written by src/upsc_dcn_docx_writer.py. The
# other formats keep using the legacy writer, so this is purely additive.
# ---------------------------------------------------------------------------
UPSC_OUTPUT_FORMATS = {
    "dcn": "PW OnlyIAS DCN Style",
    "simple": "Simple Notes",
    "mains": "Mains Notes",
    "prelims": "Prelims Notes",
}
DEFAULT_UPSC_OUTPUT_FORMAT = "dcn"

# Which note_type each non-DCN output format maps to in the legacy prompt set,
# so picking a format never leaves the prompt builder without a template.
UPSC_FORMAT_NOTE_TYPE = {
    "simple": "concise",
    "mains": "mains_ready",
    "prelims": "prelims_ready",
}

# How much detail the DCN prompt asks for. "detailed" is the default because the
# reference notes are long-form; over-compression is the failure mode users
# complain about.
UPSC_NOTE_DEPTHS = {
    "detailed": "Detailed",
    "concise": "Concise",
    "revision": "Revision",
}
DEFAULT_UPSC_NOTE_DEPTH = "detailed"

# What the notes may do about facts that need current data.
UPSC_FACT_MODES = {
    "source_only": "Source only",
    "verify": "Verify current facts if possible",
    "mark_uncertain": "Mark uncertain current facts for review",
}
DEFAULT_UPSC_FACT_MODE = "verify"

# DCN layout toggles surfaced as checkboxes; all ON by default because they are
# what makes the output match the reference document.
DCN_LAYOUT_OPTIONS = {
    "use_notes_box": "Use PW OnlyIAS-style right notes box",
    "add_footer_strip": "Add footer strip",
    "embed_corrections": "Embed corrections inside notes",
    "separate_quality_report": "Generate separate quality report",
    "render_verify": "Render and verify DOCX before download",
}
DEFAULT_DCN_LAYOUT_OPTIONS = {
    "use_notes_box": True,
    "add_footer_strip": True,
    "embed_corrections": True,
    "separate_quality_report": True,
    "render_verify": True,
}

# ---------------------------------------------------------------------------
# Error Report XLSX (src/error_report_writer.py).
#
# The only report a normal user is given. Every other report the pipeline still
# writes stays in the run folder and is surfaced only in developer mode.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Long-transcript handling (src/pipeline.py).
#
# A 2-hour class is ~15,000 words. Sending that plus the slides in one call and
# asking for DETAILED notes puts the output ceiling, not the lecture, in charge
# of how much detail survives — the model runs out of room and compresses the
# back half. Above the threshold the run is split, each chunk written in full,
# and the pieces merged.
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


MAX_SINGLE_CALL_TRANSCRIPT_WORDS = _int_env("MAX_SINGLE_CALL_TRANSCRIPT_WORDS", 6000)
CHUNK_TRANSCRIPT_WORDS = _int_env("CHUNK_TRANSCRIPT_WORDS", 3000)
CHUNK_OVERLAP_WORDS = _int_env("CHUNK_OVERLAP_WORDS", 200)

#: Hindi / Marathi notes: send the lines that slipped into English back to the
#: model once, to be rewritten in Devanagari (src/script_repair.py). One short
#: call, made only when such lines exist. Set to false to only warn about them.
DEVANAGARI_SCRIPT_REPAIR = os.getenv(
    "DEVANAGARI_SCRIPT_REPAIR", "true").strip().lower() in {"1", "true", "yes", "on"}

#: How many Gemini calls a run may have in flight at once.
#:
#: The chunked stages — transcribing a long lecture's audio, and writing the
#: notes for each stretch of it — are independent per chunk and were each run
#: one after another. On a 2-hour class that is 5 audio calls and 5 notes calls
#: in series, and the notes calls are the slowest in the run (32k output tokens
#: apiece), so the wall clock was close to the sum of all ten.
#:
#: 8, raised from 4 (2026-08-31) to cut notes generation time. The evidence
#: from run logs: a typical 2-3 h lecture makes 5-8 note chunks and ~7 STT
#: chunks, so a cap of 4 meant exactly TWO waves in both stages and 8 makes
#: each a single wave. Higher risks 429s from the proxy; the client retries
#: those with backoff, so overshooting costs time rather than correctness —
#: if runs start logging busy-retries, set MAX_PARALLEL_AI_CALLS=4 in .env.
#: Set to 1 to restore the old strictly-sequential behaviour.
MAX_PARALLEL_AI_CALLS = _int_env("MAX_PARALLEL_AI_CALLS", 8)

# ---------------------------------------------------------------------------
# Lecture video ingestion + speech-to-text (src/video_sources, src/media,
# src/transcription).
#
# The app takes a lecture PPT/PDF and a video link; the transcript is produced
# from the video rather than uploaded. Read in exactly one place — the STT
# router — so the engine is never decided twice with two different answers.
# ---------------------------------------------------------------------------
TRANSCRIPTION_ENGINES = {
    "gemini": "Gemini (via the PW proxy)",
    "faster_whisper": "Faster-Whisper (local)",
}
#: Gemini is the default because it is the route the proxy already serves.
#: faster_whisper transcribes locally with no proxy quota and works offline, but
#: it is markedly slower on CPU and its accuracy on code-switched Hinglish falls
#: off sharply at the small model sizes that finish in a sane time — so it is
#: opt-in via this variable rather than the shipped default.
#:
#: Measured on this project's reference lecture (2h 01m, CPU, int8, VAD on):
#: "tiny" transcribed it in 4.5 min (~27x realtime) but mangles Hindi terms;
#: "small" is several times slower again and is the lowest size worth using.
DEFAULT_TRANSCRIPTION_ENGINE = "gemini"
_engine_env = (os.getenv("TRANSCRIPTION_ENGINE") or "").strip().lower()
TRANSCRIPTION_ENGINE = (
    _engine_env if _engine_env in TRANSCRIPTION_ENGINES else DEFAULT_TRANSCRIPTION_ENGINE
)

#: When the chosen engine is unavailable (faster-whisper not installed, or the
#: proxy refusing), try the other one rather than losing the run.
TRANSCRIPTION_FALLBACK_ENABLED = os.getenv(
    "TRANSCRIPTION_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}

# --- faster-whisper settings (ignored unless that engine is selected) --------
#: "small" is the honest default for CPU: "large-v3" is materially better on
#: Hinglish but is several times slower again, which on a 2-hour lecture is the
#: difference between inconvenient and unusable.
WHISPER_MODEL = (os.getenv("WHISPER_MODEL") or "small").strip() or "small"
#: int8 on CPU, float16 on GPU. "auto" lets the engine pick per device.
WHISPER_COMPUTE_TYPE = (os.getenv("WHISPER_COMPUTE_TYPE") or "auto").strip() or "auto"
WHISPER_DEVICE = (os.getenv("WHISPER_DEVICE") or "auto").strip() or "auto"
WHISPER_BEAM_SIZE = _int_env("WHISPER_BEAM_SIZE", 5)
#: Empty = let Whisper detect. A UPSC class is code-switched Hindi/English, and
#: forcing "hi" makes it transliterate English terms into Devanagari — the exact
#: corruption src/transcript_cleaner.py then has to undo.
WHISPER_LANGUAGE = (os.getenv("WHISPER_LANGUAGE") or "").strip()
#: Voice activity detection skips silence, which on a class recording (breaks,
#: dead air, question pauses) is a large share of the runtime.
WHISPER_VAD_FILTER = os.getenv(
    "WHISPER_VAD_FILTER", "true").strip().lower() in {"1", "true", "yes", "on"}

#: How long a stretch of audio one Gemini request carries. 25 min at 12 kbps
#: keeps the base64 body ~2.4 MB against the proxy's ~4 MB ceiling.
STT_CHUNK_SECONDS = _int_env("STT_CHUNK_SECONDS", 1500)

# Source priority + repair toggles, all ON because they are what makes the
# output correct rather than merely different.
DCN_SOURCE_OPTIONS = {
    "slides_source_of_truth": "Use slides as source of truth",
    "transcript_explanation_only": "Use transcript only for explanation",
    "auto_repair_layout": "Auto-repair layout issues",
    "tables_to_bullets": "Convert wide tables to bullets",
}
DEFAULT_DCN_SOURCE_OPTIONS = {
    "slides_source_of_truth": True,
    "transcript_explanation_only": True,
    "auto_repair_layout": True,
    "tables_to_bullets": True,
}

# ---------------------------------------------------------------------------
# Lecture visuals (src/visuals): diagrams, maps, flowcharts and charts lifted
# from the slides into the final DCN notes. All internal — none of these are
# exposed on the frontend; env overrides exist for tuning only.
# ---------------------------------------------------------------------------
#: Master switch. Off = the notes come out exactly as before this feature.
VISUALS_ENABLED = os.getenv(
    "VISUALS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
#: A candidate below this relevance is not inserted (0.60-0.79 = include if
#: useful, 0.80+ = strongly include — the classifier is told the same scale).
#: Deliberately below the "include if useful" band: reviewers edit these
#: documents by hand and want the full set in front of them. A missing image
#: costs them a round trip to the deck; an extra one costs a keystroke.
VISUAL_RELEVANCE_THRESHOLD = 0.45
#: Caps are a backstop against a 90-slide deck, not a curation tool — the
#: curation is the classifier's exclusion of furniture/title/agenda slides.
VISUAL_MAX_PER_LECTURE = _int_env("VISUAL_MAX_PER_LECTURE", 40)
VISUAL_MAX_PER_SLIDE = _int_env("VISUAL_MAX_PER_SLIDE", 3)
#: Display size limits, in inches, are derived from the DCN content column and
#: live with the rest of that geometry — see VISUAL_MIN_WIDTH_IN below the
#: "DCN page geometry" block. Defined there because a figure may use the full
#: column width, and the column moved when the margins did.
#: Sizing aims at this effective print resolution; below the readable floor the
#: visual QC flags the figure instead of silently shipping an unreadable one.
VISUAL_TARGET_DPI = 150
VISUAL_MIN_READABLE_DPI = 96
#: How many candidate crops one classification call carries. Images travel
#: inline through the proxy (~4.5 MB body budget), so batches stay small.
VISUAL_CLASSIFY_BATCH = _int_env("VISUAL_CLASSIFY_BATCH", 6)

# Handwritten face, best first. Coming Soon is the reference document's font;
# Kalam is the metrically-similar Indic-capable substitute the team installs.
# A missing font is a substitution, not a failure, and is logged as one.
DCN_FONT_CHAIN = ("Coming Soon", "Kalam", "Comic Sans MS", "Segoe Print", "Calibri")

ERROR_REPORT_STRICTNESS = {
    "basic": "Basic — clear factual/conceptual errors only",
    "standard": "Standard — plus outdated data and missing qualifiers",
    "strict": "Strict — plus unsupported claims and precision issues",
}
DEFAULT_ERROR_REPORT_STRICTNESS = "standard"
DEFAULT_GENERATE_ERROR_REPORT = True

#: Reports kept for developers but never offered to a normal user. Read by the
#: Streamlit download area so adding a report here hides it in one place.
DEVELOPER_ONLY_REPORTS = (
    "quality_md",
    "quality_docx",
    "error_md",
    "error_docx",
    "error_fixed_md",
    "error_fixed_docx",
    "zip",
)

#: Developer mode also unlocks the run folder and raw notes. Off unless the
#: environment says otherwise, so a packaged build never shows it by accident.
DEVELOPER_MODE = os.getenv("UPSC_DEVELOPER_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# DCN page geometry and palette. The proportions started as measurements of the
# reference document ("Pulkit Jain Sir Environment DCN.docx"); the numbers below
# are that geometry after the COMPACT pass (see DCN_LAYOUT at the end of this
# block) — roughly 25% more content per page at the same body size.
#
# Everything the writer, the render check and the visual sizing need is here.
# Do not re-derive a margin, an indent or a spacing anywhere else: the whole
# point of this block is that one edit moves the document and its checks
# together.
# ---------------------------------------------------------------------------
DCN_PAGE_WIDTH_IN = 8.5
DCN_PAGE_HEIGHT_IN = 11.0
# Left/right were 0.5 / 2.5 before the compact pass, then 0.4 / 2.25, which
# review of the printed output found a shade too tight at the left edge. The
# right margin still reserves the notes-box column — trimmed, not removed.
# Content column: 5.5 in -> 5.75 in.
DCN_MARGIN_LEFT_IN = 0.5
DCN_MARGIN_RIGHT_IN = 2.25     # still reserves the notes-box column, just tighter
# Top/bottom are measured off the PW original: body text begins at exactly
# 72 pt on every page of it, and the content block runs 9.5 in. The floor on the
# top margin is the header furniture — the logo band is DCN_LOGO_BOX_H_IN tall
# and sits DCN_HEADER_DISTANCE_IN from the paper edge, and a top margin below
# their sum makes Word push the body down again, losing the space instead of
# saving it. 0.1 + 0.74 = 0.84 < 1.0, so it holds.
DCN_MARGIN_TOP_IN = 1.0
DCN_MARGIN_BOTTOM_IN = 0.5
DCN_CONTENT_WIDTH_IN = DCN_PAGE_WIDTH_IN - DCN_MARGIN_LEFT_IN - DCN_MARGIN_RIGHT_IN  # 5.75
DCN_CONTENT_HEIGHT_IN = DCN_PAGE_HEIGHT_IN - DCN_MARGIN_TOP_IN - DCN_MARGIN_BOTTOM_IN  # 9.5

# Header/footer furniture, measured from the original: its logo lockup occupies
# 217.6 x 53.3 pt (3.02 x 0.74 in) starting 8.7 pt (0.12 in) below the paper
# edge, centred on the text column. The band had been shrunk to 2.25 x 0.55 in
# to fit under a 0.8 in top margin; with the margin back at the original's 1.0 in
# the full-size lockup fits again.
DCN_HEADER_DISTANCE_IN = 0.1
DCN_FOOTER_DISTANCE_IN = 0.1
DCN_LOGO_BOX_W_IN = 3.0
DCN_LOGO_BOX_H_IN = 0.74

# The "Space for Notes" box, in inches from the PAGE's left edge. The writer
# converts to the EMU offsets the OOXML anchor wants (which are measured from
# the text column, i.e. from DCN_MARGIN_LEFT_IN). Held here in page coordinates
# because that is what has to stay true: the box must clear the content column
# on its left and the paper edge on its right, and both move with the margins.
DCN_NOTES_BOX_LEFT_IN = 6.42     # content column ends at 8.5 - 2.25 = 6.25 in
DCN_NOTES_BOX_WIDTH_IN = 1.88    # right edge 8.30 in — 0.20 in of paper to spare
DCN_NOTES_BOX_TOP_IN = 0.76      # from the header paragraph, as the reference has it
DCN_NOTES_BOX_HEIGHT_IN = 9.40

# Figure display limits (src/visuals, and figure_display_width_in in the DCN
# writer). Width may use the whole content column — that is the hard cap the
# acceptance checks enforce, so it has to be derived from the column rather
# than repeated as a literal. Height keeps a figure and its caption on one
# page; it grew with the taller text block.
VISUAL_MIN_WIDTH_IN = 2.5
VISUAL_MAX_WIDTH_IN = DCN_CONTENT_WIDTH_IN
VISUAL_MAX_HEIGHT_IN = 7.2

DCN_COLOR_TITLE = "274E13"     # dark green
DCN_COLOR_H1 = "990000"        # dark red
DCN_COLOR_H2 = "0B5394"        # blue
DCN_COLOR_H3 = "000000"        # black
#: Fourth heading level: grey. Present in the original (12 pt #666666) as the
#: step below black — a sub-section inside an H3, e.g. the two viewpoints under
#: a policy heading.
DCN_COLOR_H4 = "666666"        # grey
DCN_COLOR_BODY = "1F1F1F"
#: Table gridlines. The original rules its tables in light grey at 0.75 pt and
#: leaves the header row UNFILLED — the header reads as a header through bold
#: alone. A shaded header band is not part of this house style, so the fill is
#: None rather than a colour; set it to a hex string to bring one back.
DCN_COLOR_TABLE_RULE = "C4C7C5"
DCN_TABLE_HEADER_FILL = None
DCN_COLOR_FOOTER_FILL = "CFE2F3"   # light blue strip
DCN_COLOR_NOTES_BOX = "0B5394"     # notes-box border + caption

# Type scale, read off a published PW OnlyIAS Daily Class Note rather than
# chosen: every size here is what that document actually sets. Two earlier
# passes tuned this scale down to save pages (26/16/14/11/11, then
# 20/16/14/12/10/10); the instruction now is to match the house style, so the
# scale is the original's again and page count follows from it rather than
# driving it.
#
# Subject and lecture line are the SAME size in the original — the two together
# are one title block, not a heading and a subheading. The ladder below them
# steps 18 / 16 / 14 / 12, each level separated by colour as much as by size,
# and body sits at 11 pt.
#
# DCN_MIN_READABLE_BODY_PT stays at 10: it is the floor below which Devanagari
# matras start to fill in, not a target. Body is above it by a point.
DCN_SIZE_SUBJECT_PT = 26
DCN_SIZE_TITLE_PT = 26
DCN_SIZE_H1_PT = 18
DCN_SIZE_H2_PT = 16
DCN_SIZE_H3_PT = 14
DCN_SIZE_H4_PT = 12
DCN_SIZE_BODY_PT = 11
DCN_SIZE_NOTES_CAPTION_PT = 14
#: The footer strip. 12 pt in the original, which is a point above body — the
#: strip is furniture and reads at a glance rather than being read.
DCN_SIZE_FOOTER_PT = 12
#: Below this the Devanagari build stops being safely legible in print.
DCN_MIN_READABLE_BODY_PT = 10

# ---------------------------------------------------------------------------
# Compact layout metrics. Every vertical/horizontal measurement the DCN writer
# applies is one of these names — none of them is written at a call site.
# ---------------------------------------------------------------------------
#: Kept for the run log. It recorded how far the compact pass had pulled away
#: from the house style; the layout is now measured from the original instead,
#: so there is nothing to compact against and the value is 0.
DCN_COMPACTNESS = 0.0

#: Was 1.15 on every style, then 1.08. Devanagari needs real leading (matras sit
#: above and below the line), so this stops well short of single spacing — 1.0
#: clips Kalam in LibreOffice. 1.05 is the tightest value that still renders
#: Hindi cleanly at DCN_SIZE_BODY_PT; it is checked by eye on printed Hindi, not
#: derived, so treat it as measured rather than as a knob.
DCN_LINE_SPACING = 1.05
#: Table cells and flow steps: short, already boxed, and the biggest single
#: source of dead vertical space in the old layout.
DCN_LINE_SPACING_TIGHT = 1.02

#: Paragraph spacing, in points, as (before, after) per style.
#:
#: Keep the original font scale, with small explicit gaps so headings and
#: successive bullets remain distinct in both Word and standalone PDF export.
DCN_SPACE_SUBJECT_PT = (0.0, 0.0)
DCN_SPACE_TITLE_PT = (0.0, 2.0)
DCN_SPACE_H1_PT = (5.0, 2.0)
DCN_SPACE_H2_PT = (4.0, 2.0)
DCN_SPACE_H3_PT = (3.0, 1.5)
DCN_SPACE_H4_PT = (3.0, 1.5)
DCN_SPACE_BODY_PT = (0.0, 1.0)
DCN_SPACE_BULLET_PT = (0.0, 1.0)
DCN_SPACE_TABLE_PT = (3.0, 3.0)
DCN_SPACE_FLOW_PT = (0.0, 1.0)
DCN_SPACE_CAUTION_PT = (1.5, 1.5)
DCN_SPACE_FIGURE_PT = (3.0, 1.0)
DCN_SPACE_FIGURE_CAPTION_PT = (0.0, 3.5)

#: Bullets, measured from the original: its level-1 disc sits 0.25 in inside the
#: text margin with the text at 0.5 in, and its level-2 circle at 0.75 in with
#: the text at 1.0 in. That is a 0.5 in step per level with a 0.25 in hang — so
#: the step is back to 720 twips after the compact pass had halved it to 360,
#: which had left a full-size marker with too little room beside it.
DCN_BULLET_INDENT_STEP_TWIPS = 720      # 0.5 in per level
DCN_BULLET_LEVELS = 3
#: Distance from the glyph to the text, and therefore the widest the glyph may
#: be: Word advances to the hanging position with a tab, and a glyph wider than
#: this pushes the first line out to the next default tab stop instead.
DCN_BULLET_HANGING_TWIPS = 360          # 0.25 in
#: The original sets its markers at body size — the disc measures 9.7 pt wide at
#: 11 pt, comfortably inside the 18 pt hang, so it never tabs out. The marker is
#: the thing a reader's eye tracks down the page and has to be visible at arm's
#: length; a compact pass once set it at 9 pt with smaller marks and it read as
#: a speck.
DCN_BULLET_GLYPH_SIZE_PT = DCN_SIZE_BODY_PT
#: Level 1 solid disc, level 2 hollow circle — both as the original has them.
#: Level 3 is a small filled square: the original never goes that deep, and a
#: full square crowds the text beside it.
DCN_BULLET_GLYPHS = ("●", "○", "▪")
#: The original draws its markers in Arial, not in the handwritten face and not
#: in the symbol font this app uses for the currency and maths glyphs. Arial is
#: present everywhere and its disc is round and evenly weighted at text sizes,
#: which is the whole job of a bullet.
DCN_BULLET_FONT = "Arial"
#: Caution notes hang off the left margin by this much (was 0.25 in).
DCN_CAUTION_INDENT_TWIPS = 180

#: Table cell padding, in twips, measured from the original: its cell text
#: starts 8.6 pt (about 172 twips) inside the rule, and rows are padded roughly
#: 5 pt top and bottom. The compact pass had cut this to 3.6 pt horizontally,
#: which ran Devanagari almost into the gridline.
DCN_TABLE_CELL_MARGIN_TWIPS = {"top": 72, "left": 173, "bottom": 72, "right": 173}

#: How long a set of notes may run, in RENDERED pages.
#:
#: The layout is fixed to the published house style, so length is now a content
#: decision rather than a typographic one: the only way to hit a page count is
#: to write fewer words. This is the band the notes are written to.
#:
#: The lower bound is NOT a floor to pad up to. A short class should produce
#: short notes — inventing material to reach ten pages is the one failure worse
#: than a thin note. It is recorded because it says what a normal full lecture
#: should look like, and because a run that lands far below it is worth a look.
DCN_TARGET_PAGES = (10, 15)

#: Words per rendered page at the house layout, PER LANGUAGE.
#:
#: Measured by rendering real generated notes from past runs through Word and
#: counting pages — not from synthetic samples, which carry far more headings per
#: word than a real class does and gave a rate roughly 40% too low.
#:
#:     Hindi    6,435 w -> 18 pp (358)   5,414 w -> 15 pp (361)   3,227 w -> 9 pp (359)
#:     English  4,694 w -> 15 pp (313)   5,076 w -> 17 pp (299)   3,211 w -> 11 pp (292)
#:
#: Hindi fits noticeably more words per page: Devanagari words are shorter, so a
#: line of Hindi carries more of them than the same measure of English. One rate
#: for both would have made the budget wrong for whichever language it was not
#: calibrated on.
#:
#: Page counts are whole numbers, so each of these carries about half a page of
#: quantisation; treat the rates as good to a few per cent, not exact.
#: Marathi shares Devanagari and Hindi's average word length closely enough
#: that it inherits the Hindi rate rather than getting a guessed one of its
#: own. It is marked as inherited so the first real Marathi run can replace
#: it with a measurement instead of someone assuming it was measured.
DCN_WORDS_PER_PAGE = {"en": 295, "hi": 355, "mr": 355}   # mr inherited from hi
DCN_WORDS_PER_PAGE_DEFAULT = 295

#: How far past the word budget a DRAFT may run before the pipeline spends a
#: model call condensing it, before anything has been rendered. Re-running the
#: model is not free, and this stage is working from an estimate, so a draft a
#: few per cent over is left for the rendered-page check below to judge on the
#: real number instead.
DCN_NOTES_OVERRUN_TOLERANCE = 0.08

#: How many times a RENDERED document that came out over DCN_TARGET_PAGES[1] may
#: be sent back to the model to be compressed.
#:
#: The word budget above is a prediction, and predictions miss: a class heavy in
#: tables or figures takes more pages than its word count implies. This is the
#: check that uses the real number — render, count the pages Word actually
#: produced, compress if it is over, render again.
#:
#: Two, not "until it fits". Each pass costs a model call and a Word render; the
#: second pass has a much smaller job than the first; and an unbounded loop is
#: how a run hangs on notes the model will not shorten any further. A document
#: still over after two passes ships at its real length with a warning, because
#: truncating it would drop what the class actually taught.
DCN_MAX_COMPRESSION_PASSES = 2

#: The original numbers its pages, centred in the footer strip between
#: "PW OnlyIAS" and "Daily Class Notes". This app had left the number out.
#: How many pages the layout check rasterises.
#:
#: It was three. A note is capped at DCN_TARGET_PAGES[1] pages, so three meant
#: the check covered a fifth of the document and reported "layout verified" for
#: the whole of it — and the pages it skipped were the middle ones, which is
#: exactly where a table or a figure pushes body text into the notes column.
#:
#: Covering the document is affordable because the PDF already exists by then:
#: the check is handed the student's own PDF, so this is PyMuPDF rasterisation
#: at 110 dpi, not a second trip through Word.
DCN_RENDER_CHECK_MAX_PAGES = DCN_TARGET_PAGES[1] + 1

DCN_FOOTER_SHOW_PAGE_NUMBER = True

#: Light enough to read black Devanagari and Latin through, and to print. Word's
#: named highlight palette has no such value (its "yellow" is FFFF00), so this is
#: applied as run shading instead — see the DCN writer.
DCN_HIGHLIGHT_COLOR = "FFF2A8"

#: One dict for the whole layout, so a reviewer can see the shape of the
#: decision without reading the writer. The writer imports the names above, not
#: this — it exists for discoverability and for the run log.
DCN_LAYOUT = {
    "source": "measured from a published PW OnlyIAS Daily Class Note",
    "compactness": DCN_COMPACTNESS,
    "page_in": (DCN_PAGE_WIDTH_IN, DCN_PAGE_HEIGHT_IN),
    "margins_in": {
        "left": DCN_MARGIN_LEFT_IN,
        "right": DCN_MARGIN_RIGHT_IN,
        "top": DCN_MARGIN_TOP_IN,
        "bottom": DCN_MARGIN_BOTTOM_IN,
    },
    "content_in": (DCN_CONTENT_WIDTH_IN, DCN_CONTENT_HEIGHT_IN),
    "line_spacing": DCN_LINE_SPACING,
    "bullet_indent_step_twips": DCN_BULLET_INDENT_STEP_TWIPS,
    "bullet_hanging_twips": DCN_BULLET_HANGING_TWIPS,
    "bullet_glyph_size_pt": DCN_BULLET_GLYPH_SIZE_PT,
    "bullet_font": DCN_BULLET_FONT,
    "table_cell_margin_twips": DCN_TABLE_CELL_MARGIN_TWIPS,
    "table_rule_color": DCN_COLOR_TABLE_RULE,
    "table_header_fill": DCN_TABLE_HEADER_FILL,
    "footer_page_number": DCN_FOOTER_SHOW_PAGE_NUMBER,
    "highlight_color": DCN_HIGHLIGHT_COLOR,
    "sizes_pt": {
        "subject": DCN_SIZE_SUBJECT_PT,
        "title": DCN_SIZE_TITLE_PT,
        "h1": DCN_SIZE_H1_PT,
        "h2": DCN_SIZE_H2_PT,
        "h3": DCN_SIZE_H3_PT,
        "h4": DCN_SIZE_H4_PT,
        "body": DCN_SIZE_BODY_PT,
        "footer": DCN_SIZE_FOOTER_PT,
    },
}

# Red correction callouts in the student DOCX. OFF by default on purpose: the
# shipping contract is silent corrections (state the fact, log the fix to the
# error report). Turning this on makes the model record a correction ONLY when
# the source contradicts itself, rendered as a red callout box — an explicit,
# reviewer-facing mode that can be switched off again without a redeploy.
CORRECTIONS_ENABLED = os.getenv(
    "CORRECTIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# Handwritten face used by the reference, with fallbacks for machines that do
# not have it installed. Template mode carries the embedded font, so the
# fallback chain only matters for the programmatic path.
DCN_FONT_PRIMARY = "Coming Soon"
DCN_FONT_FALLBACKS = ["Comic Sans MS", "Kalam", "Segoe Print"]
# Used only for runs whose glyphs the handwritten face lacks (₹, ≥, ≤, ≠, ↓...).
DCN_FONT_SYMBOL = "Segoe UI Symbol"

# Advanced QC controls (src/qc engine). Shown under "Advanced Quality
# Settings" in the UI; these are the Strict-mode defaults. Only
# high-confidence, source-supported errors are ever corrected automatically —
# uncertain points go to human review (see src/qc/correction_policy.py).
ADVANCED_QC_OPTIONS = {
    "verify_sources": "Verify factual claims from approved sources",
    "latest_data_check": "Check latest data for date-sensitive claims",
    "visual_pdf_check": "Check final PDF visually",
    "auto_correct": "Allow high-confidence automatic corrections",
}
DEFAULT_ADVANCED_QC_OPTIONS = {
    "verify_sources": True,
    "latest_data_check": True,
    "visual_pdf_check": True,
    "auto_correct": True,
}


# ---------------------------------------------------------------------------
# Daily Mains Practice (DMP)
# ---------------------------------------------------------------------------
# Practice questions generated FROM a finished set of notes, and model answers
# generated only for the questions the user actually ticked.
#
# The split matters to the bill, not just to the UI. Questions are one call per
# level; answers are one call per question. Answering all ten up front would
# cost more than three times what the questions did, for answers nobody asked
# to read — so the levels are generated together and the answers strictly on
# demand.

#: Difficulty levels, in the order the panel shows them.
DMP_LEVELS = ("easy", "moderate", "applied")

#: What each level is called on screen.
DMP_LEVEL_LABELS = {
    "easy": "Easy / Direct",
    "moderate": "Moderate / Multi-layered",
    "applied": "Applied / Analytical",
}

#: Questions per level: 4 easy, 3 moderate, 3 applied — ten in all, the split
#: the faculty asked for on 2026-08-31 (the sheet's cells said five per level;
#: build_question_prompt substitutes these counts into their wording). The
#: parser still enforces the count per level — a model that returns fewer is
#: not silently accepted, because a short set reads as "the notes only
#: supported that many" when in fact the model just stopped early.
DMP_QUESTIONS_PER_LEVEL = {"easy": 4, "moderate": 3, "applied": 3}


def dmp_question_count(level: str) -> int:
    """How many questions one level generates (defensive for unknown levels)."""
    return int(DMP_QUESTIONS_PER_LEVEL.get(level, 3))


#: The two Mains marks a question can carry, with the word limits UPSC
#: attaches to them. These are what the printed sheets show next to each
#: question — the exam's own convention, so they never change with taste.
DMP_MARKS = (10, 15)
DMP_MARK_WORDS = {10: 150, 15: 250}

#: How long a model answer is asked to be: the exam's own limit for its marks.
#: This was a ~500-word "teaching answer" for both marks until the content team
#: (2026-10) asked for answers a candidate could actually write in the hall —
#: a 150-word question answered in 500 words teaches the wrong habit.
DMP_ANSWER_TARGET_WORDS = {10: 150, 15: 250}

#: The band the prompt allows around that target (±10%: 135-165 words for 10
#: marks, 225-275 for 15).
DMP_ANSWER_WORD_TOLERANCE = 0.10

#: An answer whose text runs more than this far over its limit is flagged on
#: the panel. Flagged, never cut: a truncated model answer stops mid-point,
#: and that is worse than a long one a teacher can trim.
DMP_ANSWER_OVERRUN_FLAG = 0.20

#: Below this, a note has not taught enough to set questions on. A ten-word
#: "note" would still produce five confident questions — about nothing.
DMP_MIN_NOTE_WORDS = 120

#: How much of the note is sent as grounding. Long lectures overrun the request
#: budget, and the trim is by WORDS from the end rather than by characters, so a
#: sentence is never cut in half mid-clause.
DMP_MAX_GROUNDING_WORDS = 9000
