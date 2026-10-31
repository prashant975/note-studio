"""Configuration for the standalone UPSC PDF QC app.

Copied from the note-generation project's ``src/config.py`` and trimmed to the
constants the QC modules actually read. Every name and value below is the value
the original app runs with — the QC checks are calibrated against these numbers
(page geometry, DPI floors, chunk sizes, model names), so changing one here
changes what the checks decide.

What was dropped: note-generation settings (prompt/note types, workflows, video
and transcription options, DOCX writer options, diagram/visual generation).
Nothing here is exposed in the UI — requirement: the frontend shows no settings.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT_DIR = Path(os.getenv("UPSC_PDF_QC_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()
ENV_PATH = Path(os.getenv("UPSC_PDF_QC_ENV", str(ROOT_DIR / ".env"))).expanduser()
if load_dotenv:
    load_dotenv(ENV_PATH)


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Gemini through the shared PW proxy (pw_access.py). The app ships no API key;
# the proxy holds it and verifies the signed-in @pw.live Google account. These
# env vars only choose which model the proxy calls — they are NOT secrets, and
# they carry the same names as the note-generation project so one .env serves
# both.
# ---------------------------------------------------------------------------
DEFAULT_MODEL = (os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")).strip() or "gemini-3.5-flash"
_fallback_env = os.getenv("FALLBACK_MODEL_NAME")
DEFAULT_FALLBACK_MODEL = (_fallback_env if _fallback_env is not None else "gemini-3.6-flash").strip()

RUNS_DIR = Path(os.getenv("RUNS_DIR", str(ROOT_DIR / "runs"))).expanduser()
OUTPUTS_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT_DIR / "output"))).expanduser()

APP_NAME = os.getenv("APP_NAME", "UPSC PDF QC")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "y"}

APP_VERSION = "1.0.0"
APP_VERSION_DATE = "2026-08-19"

#: Every timestamp the app shows or logs is Asia/Kolkata, as in the original.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def stamp_ist() -> dict:
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
    """"v1.0.0 · 19 Aug 2026" — falls back to the raw ISO date if unparseable."""
    from datetime import date

    try:
        pretty = date.fromisoformat(APP_VERSION_DATE).strftime("%d %b %Y")
    except ValueError:
        pretty = APP_VERSION_DATE
    return f"v{APP_VERSION} · {pretty}"


def header_datetime() -> str:
    """"19 August 2026 | 01:20 IST" — the live clock shown in the header.

    Read at render time, never cached, so the header always shows the real
    system date/time.
    """
    now = now_ist()
    return f"{now.strftime('%d %B %Y')} | {now.strftime('%H:%M')} IST"


# ---------------------------------------------------------------------------
# Error Report XLSX strictness (src/error_report_writer.py).
#
# Standard is the note-generation app's default and is what the standalone app
# runs with; it is deliberately not exposed in the UI.
# ---------------------------------------------------------------------------
ERROR_REPORT_STRICTNESS = {
    "basic": "Basic — clear factual/conceptual errors only",
    "standard": "Standard — plus outdated data and missing qualifiers",
    "strict": "Strict — plus unsupported claims and precision issues",
}
#: "strict" reports every error type. At "standard" the writer silently drops
#: Unsupported claim, Source-year precision, Conceptual/formula precision,
#: Terminology issue and Language clarity unless they are High severity — real
#: findings, deleted for their category. Recall is the whole point of this tool,
#: and the verification pass is what controls the false positives, so nothing is
#: filtered out of the report by default.
DEFAULT_ERROR_REPORT_STRICTNESS = "strict"

#: The QC ladder level the standalone app runs at. "strict" is what the
#: note-generation app defaults to, and it is the level at which the visual PDF
#: check and the independent AI review both run.
DEFAULT_QUALITY_LEVEL = "strict"

# ---------------------------------------------------------------------------
# Long-document handling. A 99-page notes PDF is far past a single call's
# useful input, so the error-report pass is chunked with the same word budgets
# the note-generation pipeline uses for long transcripts, and the per-chunk JSON
# arrays are merged (duplicates dropped) exactly as the pipeline merges them.
# ---------------------------------------------------------------------------
MAX_SINGLE_CALL_TRANSCRIPT_WORDS = _int_env("MAX_SINGLE_CALL_TRANSCRIPT_WORDS", 6000)
CHUNK_TRANSCRIPT_WORDS = _int_env("CHUNK_TRANSCRIPT_WORDS", 3000)
CHUNK_OVERLAP_WORDS = _int_env("CHUNK_OVERLAP_WORDS", 200)
#: Output ceiling for one error-report call. The old 16,000 was sized for a
#: lecture's worth of notes and is not enough for a document that genuinely has
#: many errors — which is exactly the document you least want to under-report.
#: Raising it is the cheap half of the fix; detecting the cut-off (see
#: GeminiResponse.truncated) is the half that matters, because no ceiling is
#: high enough for every input.
#: Pages per error-report call — THE recall lever.
#:
#: A model asked to audit a whole document in one answer does not audit a whole
#: document: it reads the start closely, skims, and returns a plausible-length
#: list. A graded 19-page trial went in as ONE call and came back with 21 of 40
#: seeded errors, the reviewer noting "there are pages and pages not considered".
#: Small windows force the model to actually look at every page — the same pages,
#: the same model, several times more of them found.
#:
#: Lower = more thorough and more expensive (one call per this many pages).
PAGES_PER_ERROR_REPORT_CALL = _int_env("PAGES_PER_ERROR_REPORT_CALL", 2)

#: Differently-focused AI passes over each text window. Pass one asks the open
#: question — "what is wrong here?" — and reliably returns the headline
#: mistakes. Pass two reads the SAME pages against a fixed checklist (proper
#: nouns and their attributes, acronym expansions, definitional ranges and
#: orderings, defining characteristics, grammar and tense, impossible figures),
#: because a graded trial showed the misses cluster in exactly those classes: a
#: wrong city, a wrong acronym expansion, a wrong tense all survived the open
#: pass. The union is deduplicated and every claim still faces the verification
#: pass, so the second pass costs calls, not precision. Set to 1 to run the open
#: pass only.
ERROR_REPORT_PASSES = _int_env("ERROR_REPORT_PASSES", 2)

ERROR_REPORT_MAX_OUTPUT_TOKENS = _int_env("ERROR_REPORT_MAX_OUTPUT_TOKENS", 32000)

#: The verification pass — a second opinion that tries to REFUTE each claimed
#: error before it reaches the workbook. It exists because a single generation
#: at temperature 0 reports its own uncertainty as fact, most often calling a
#: correct, current figure "outdated" because the model's training predates it.
#: Set VERIFY_FINDINGS=0 to skip it (faster and cheaper, and measurably noisier).
VERIFY_FINDINGS = _int_env("VERIFY_FINDINGS", 1) != 0
#: Claims per verification call. Batched because one call per finding is the
#: dominant cost of the pass, and a short numbered list is something a model
#: keeps aligned reliably.
VERIFY_BATCH_SIZE = _int_env("VERIFY_BATCH_SIZE", 8)
VERIFY_MAX_OUTPUT_TOKENS = _int_env("VERIFY_MAX_OUTPUT_TOKENS", 8000)

#: Web searches per document, at most. A grounded lookup is a Claude call with
#: search running inside it, so it costs more than a verification batch and
#: takes seconds. Only claims the verifier could not settle are looked up —
#: measured, five to ten per document — and this caps a pathological run.
#: Set GROUNDED_LOOKUP_MAX=0 to switch grounded checking off entirely.
GROUNDED_LOOKUP_MAX = _int_env("GROUNDED_LOOKUP_MAX", 12)

#: The web fact-check over standalone claims (src/qc/fact_sweep.py).
#:
#: Graded against a marked document, every error the app missed was the same
#: shape: a lone figure, year or attribution with nothing on the page to
#: contradict it — "weighs approximately 500 kg", "between 1982 and 2023",
#: "contributed by Thailand". Reading the page cannot find those; only a
#: search can. Claims are checked ten to a call, so this many claims costs
#: about a tenth as many calls. Set FACT_SWEEP_MAX_CLAIMS=0 to switch it off.
FACT_SWEEP_MAX_CLAIMS = _int_env("FACT_SWEEP_MAX_CLAIMS", 70)
FACT_SWEEP_BATCH = _int_env("FACT_SWEEP_BATCH", 10)

MAX_PARALLEL_AI_CALLS = _int_env("MAX_PARALLEL_AI_CALLS", 4)

#: How many PDFs are checked at once. Each PDF is an independent job, and much
#: of its work — rendering every page, scanning the layout, finding tables — is
#: local and overlaps well with another file's waiting.
#:
#: This does NOT multiply the load on the proxy: src/pipeline.py holds a single
#: run-wide semaphore of MAX_PARALLEL_AI_CALLS slots that every AI call must
#: take, whichever file it belongs to. So this number buys parallel *local*
#: work, and the AI ceiling stays exactly where MAX_PARALLEL_AI_CALLS puts it.
MAX_PARALLEL_PDFS = _int_env("MAX_PARALLEL_PDFS", 3)

LANGUAGE_CODES = {"English": "en", "Hindi": "hi", "en": "en", "hi": "hi"}

# ---------------------------------------------------------------------------
# Visual QC thresholds (src/qc/format_check.py, src/pdf/analyzer.py).
# ---------------------------------------------------------------------------
#: Pages sent to the visual model per PDF. Matches the note-generation app's
#: max_visual_check_pages limit in config/upsc_qc_rules.yaml.
#: Pages sent to the AI visual check, per PDF. **0 means every page**, which is
#: the default: a QC tool that samples cannot answer "is this file correct?",
#: only "were the pages I happened to look at correct?". The old default of 6
#: was 15% coverage on a 40-page set of notes, and two of those slots were
#: always the first and last page, leaving four chosen for risk.
#:
#: This costs real money — every page is rendered and sent to the model — so it
#: is left configurable. Set a positive number to sample instead, and the run
#: will record how many pages of how many it examined.
MAX_VISUAL_CHECK_PAGES = _int_env("MAX_VISUAL_CHECK_PAGES", 0)

#: Pages per visual-check call. Full coverage means a 40-page file cannot go in
#: one request — forty inline images would exceed the proxy's body budget, and
#: one short reply could not describe forty pages of problems anyway.
VISUAL_BATCH_PAGES = _int_env("VISUAL_BATCH_PAGES", 6)
VISUAL_MAX_OUTPUT_TOKENS = _int_env("VISUAL_MAX_OUTPUT_TOKENS", 4000)

#: How many pages the layout check rasterises to PNG in the run folder. This is
#: an ARTIFACT budget, not a coverage one: the overflow scan itself already
#: walks every page (see pdf_layout_check.verify_pdf_layout, which loops over
#: range(page_count)). Raised from 3 so a reviewer chasing an overflow reported
#: on page 20 has the picture of page 20 to look at.
LAYOUT_RENDER_PAGES = _int_env("LAYOUT_RENDER_PAGES", 12)
VISUAL_RENDER_DPI = _int_env("VISUAL_RENDER_DPI", 110)
VISUAL_MIN_READABLE_DPI = 96

# ---------------------------------------------------------------------------
# DCN page geometry, measured from the reference document and shared by the
# writer and the render check in the original project. The layout scan in
# src/pdf_layout_check.py is calibrated to these and to nothing else.
# ---------------------------------------------------------------------------
DCN_PAGE_WIDTH_IN = 8.5
DCN_PAGE_HEIGHT_IN = 11.0
DCN_MARGIN_LEFT_IN = 0.5
DCN_MARGIN_RIGHT_IN = 2.5      # wide on purpose: reserves the notes-box column
DCN_MARGIN_TOP_IN = 1.0
DCN_MARGIN_BOTTOM_IN = 1.0
DCN_CONTENT_WIDTH_IN = DCN_PAGE_WIDTH_IN - DCN_MARGIN_LEFT_IN - DCN_MARGIN_RIGHT_IN  # 5.5

DCN_COLOR_TITLE = "274E13"     # dark green
DCN_COLOR_H1 = "990000"        # dark red
DCN_COLOR_H2 = "0B5394"        # blue
DCN_COLOR_H3 = "000000"        # black
DCN_COLOR_BODY = "1F1F1F"
DCN_COLOR_FOOTER_FILL = "CFE2F3"
DCN_COLOR_NOTES_BOX = "0B5394"

DCN_SIZE_TITLE_PT = 26
DCN_SIZE_H1_PT = 16
DCN_SIZE_H2_PT = 14
DCN_SIZE_H3_PT = 11
DCN_SIZE_BODY_PT = 11
DCN_SIZE_NOTES_CAPTION_PT = 14

#: Minimum words a set of notes must carry before the length check complains.
#: The same floor src/upsc_quality_checker.check_dcn_notes() defaults to.
MIN_NOTES_WORDS = _int_env("MIN_NOTES_WORDS", 400)
