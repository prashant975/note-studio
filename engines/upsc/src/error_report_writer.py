"""The Error Report XLSX — the only report a reviewer is given.

One sheet, eight columns, one row per thing that was actually wrong in the
lecture. Everything else the pipeline produces (quality report, equation
report, QC dumps, raw notes) stays in the run folder for developers.

The layout is copied from ``Error Report Format (1).xlsx``. Every colour and
font here was read out of that file rather than guessed:

    header fill   1F4E78   white Carlito 11 bold, centred, wrapped
    borders       44B3E1   thin, all four edges
    row banding   C0E6F5   on alternating body rows
    severity      High FCA5A5 | Medium FDE68A | Low D9EAD3

Two things in here are defensive rather than cosmetic, and both matter:

* Cell values are written with an explicit string type. openpyxl turns any
  value starting with ``=`` into a *formula*, so a correction that reads
  ``= GVA + product taxes`` would land in the sheet as ``#NAME?`` instead of
  the text a reviewer needs to read.
* Body row heights are deliberately left unset. Excel auto-fits a wrapped row
  only while no explicit height is stored; pinning a "readable" height would
  silently clip the long correction and URL cells this report exists to carry.
"""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from pathlib import Path
import json
import logging
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

log = logging.getLogger(__name__)

SHEET_NAME = "Error Report"

#: The eight columns, in order. This tuple is the contract — the AI prompt, the
#: pipeline and the tests all read it from here.
COLUMNS: tuple[str, ...] = (
    "Error. No.",
    "Original statement",
    "Error type",
    "Timestamp",
    "Severity",
    "Error / concern",
    "Correct information / correction",
    "Source URL(s)",
)

#: Keys of a row dict, aligned index-for-index with COLUMNS[1:].
FIELDS: tuple[str, ...] = (
    "original_statement",
    "error_type",
    "timestamp",
    "severity",
    "error_concern",
    "correct_information_correction",
    "source_urls",
)

# Templates produced before the timestamp request have this exact seven-column
# shape. They are upgraded in place so a user's existing style sheet remains
# useful instead of being silently replaced by the generic workbook.
LEGACY_COLUMNS: tuple[str, ...] = COLUMNS[:3] + COLUMNS[4:]

# --- palette, measured from the reference workbook ---------------------------
HEADER_FILL = "1F4E78"
HEADER_FONT_COLOR = "FFFFFF"
BORDER_COLOR = "44B3E1"
BAND_FILL = "C0E6F5"
BODY_FONT_COLOR = "000000"
SEVERITY_FILLS = {"High": "FCA5A5", "Medium": "FDE68A", "Low": "D9EAD3"}

#: Carlito in the reference is the Google Sheets clone of Calibri; Calibri is
#: metrically identical and actually present on Windows.
BODY_FONT_NAME = "Calibri"
BODY_FONT_SIZE = 11
HEADER_ROW_HEIGHT = 30

#: Widths in Excel character units, per column letter.
COLUMN_WIDTHS = {
    "A": 11, "B": 40, "C": 25, "D": 22, "E": 15,
    "F": 45, "G": 52, "H": 60,
}

TIMESTAMP_COLUMN = COLUMNS.index("Timestamp") + 1
SEVERITY_COLUMN = COLUMNS.index("Severity") + 1

SEVERITIES = ("High", "Medium", "Low")
DEFAULT_SEVERITY = "Medium"

#: Excel refuses a cell longer than this.
MAX_CELL_CHARS = 32_767
#: Everything XML 1.0 forbids, plus the surrogate range openpyxl chokes on.
_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")

NO_SOURCE_TEXT = "Source needed"
NO_CORRECTION_TEXT = "Needs verification from official source before finalisation."
LATEST_FIGURE_TEXT = "Use the latest official figure available at finalisation."

# --- controlled vocabulary ---------------------------------------------------
#: The error types the report is allowed to use. Anything else the model
#: invents is kept verbatim but does not participate in strictness filtering.
ERROR_TYPES: tuple[str, ...] = (
    "Conceptual",
    "Numerical/outdated",
    "Source-year precision",
    "Conceptual/formula precision",
    "Factual",
    "Outdated data",
    "Scheme/ministry mismatch",
    "Constitutional/legal mismatch",
    "Definition error",
    "Overgeneralised claim",
    "Missing qualifier",
    "Unsupported claim",
    "Terminology issue",
    "Current-affairs update",
    "Diagram/table mismatch",
    "Unit/formula/symbol issue",
    "Language clarity",
    # ---- named categories a reviewer asked for -------------------------------
    # These overlap the older names above rather than replacing them: a model
    # that says "Numerical/outdated" and one that says "Number/Data Error" mean
    # the same thing, and both spellings have to survive normalisation. They are
    # listed because normalise_rows FILTERS by this vocabulary — an unlisted
    # type was reported only when it happened to be High severity.
    "Factual Error",
    "Conceptual Error",
    "Terminology Error",
    "Date/Year Error",
    "Number/Data Error",
    "Constitutional/Legal Error",
    "Scheme/Policy Error",
    "Committee/Report Error",
    "Person/Institution Error",
    "Historical Error",
    "Geography Error",
    "Mismatch with Source",
    "Missing Important Information",
    "AI Hallucination",
    "Incorrect Interpretation",
    "Language/Translation Error",
    "Diagram/Flow Error",
    # Not a mistake the lecturer made — a mistake in how the note is WRITTEN:
    # it narrates an edit instead of stating the fact. Reported because the
    # alternative used to be failing the whole run.
    "Correction meta-language",
    # Not a finding about the lecture at all: a statement about how much of the
    # lecture this report actually covers.
    "Report coverage",
)

STRICTNESS_LEVELS = ("basic", "standard", "strict")
DEFAULT_STRICTNESS = "standard"

#: What each level is willing to report. Cumulative: standard adds to basic,
#: strict adds to standard.
_BASIC_TYPES = frozenset({
    "Conceptual",
    "Factual",
    "Definition error",
    "Scheme/ministry mismatch",
    "Constitutional/legal mismatch",
    "Unit/formula/symbol issue",
    "Diagram/table mismatch",
    # A wrong year, a wrong number, a wrong committee or a fabricated claim is
    # plainly wrong at any strictness — there is no reading of "basic" under
    # which a student should be shown one uncorrected.
    "Factual Error",
    "Conceptual Error",
    "Date/Year Error",
    "Number/Data Error",
    "Constitutional/Legal Error",
    "Scheme/Policy Error",
    "Committee/Report Error",
    "Person/Institution Error",
    "Historical Error",
    "Geography Error",
    "Mismatch with Source",
    "AI Hallucination",
    "Diagram/Flow Error",
    "Correction meta-language",
    "Report coverage",
})
_STANDARD_TYPES = _BASIC_TYPES | frozenset({
    "Numerical/outdated",
    "Outdated data",
    "Current-affairs update",
    "Missing qualifier",
    "Overgeneralised claim",
    # Judgement calls rather than plain errors: worth reporting by default, but
    # not the thing "basic" exists to catch.
    "Missing Important Information",
    "Incorrect Interpretation",
    "Terminology Error",
    # Distinct from "Language clarity", which stays strict-only. Clarity is
    # phrasing; a TRANSLATION error changes what the sentence says, and the
    # notes can now be written in a language the class was not taught in.
    "Language/Translation Error",
})
_STRICT_TYPES = _STANDARD_TYPES | frozenset(ERROR_TYPES)

STRICTNESS_TYPES: dict[str, frozenset[str]] = {
    "basic": _BASIC_TYPES,
    "standard": _STANDARD_TYPES,
    "strict": _STRICT_TYPES,
}

#: Rows the report writes about itself rather than about the lecture. They are
#: never filtered out and never carry "Source needed".
_PLACEHOLDER_TYPES = frozenset({"No issue", "Report parsing"})


# =============================================================================
# normalisation
# =============================================================================

def _clean(value: object) -> str:
    """A single cell's text: no illegal characters, no runaway length."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = "\n".join(_clean(v) for v in value if _clean(v))
    elif isinstance(value, bool):
        text = "Yes" if value else "No"
    else:
        text = str(value)
    text = _ILLEGAL_CHARS_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 1] + "…"
    return text


def normalise_severity(value: object) -> str:
    """Map whatever the model said onto exactly High / Medium / Low.

    An unrecognised severity becomes Medium rather than being dropped: losing a
    reported error because its label was misspelled is the worse failure.
    """
    text = _clean(value).lower()
    if not text:
        return DEFAULT_SEVERITY
    for level in SEVERITIES:
        if text.startswith(level.lower()):
            return level
    if text in {"critical", "severe", "major", "blocker", "h"}:
        return "High"
    if text in {"minor", "trivial", "info", "informational", "nit", "l"}:
        return "Low"
    if text in {"moderate", "med", "m"}:
        return "Medium"
    return DEFAULT_SEVERITY


def normalise_error_type(value: object) -> str:
    """Snap onto the controlled vocabulary when it is recognisably the same
    category; otherwise keep what was given so the reviewer still sees it."""
    text = _clean(value)
    if not text:
        return "Factual"
    lowered = text.lower().strip(" .")
    for known in (*ERROR_TYPES, *_PLACEHOLDER_TYPES):
        if lowered == known.lower():
            return known
    collapsed = re.sub(r"[^a-z]", "", lowered)
    for known in (*ERROR_TYPES, *_PLACEHOLDER_TYPES):
        if collapsed == re.sub(r"[^a-z]", "", known.lower()):
            return known
    return text


def format_source_urls(value: object) -> str:
    """One URL per line, in the order given, without duplicates.

    Non-URL prose is kept — a model that answers "Source needed" or names a
    press release is giving the reviewer something real, and silently deleting
    it would look like the source was never mentioned.
    """
    if value is None:
        return ""
    items: list[str]
    if isinstance(value, (list, tuple, set)):
        items = [_clean(v) for v in value]
    else:
        items = [part for part in re.split(r"[\n,;]+", _clean(value))]
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip().strip("<>()[]").rstrip(".,;")
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
    return "\n".join(out)


def normalise_rows(
    raw_rows: object,
    *,
    strictness: str = DEFAULT_STRICTNESS,
) -> list[dict]:
    """Clean, filter and number the rows that will become the sheet.

    Rows that carry no usable content at all are dropped. Filtering by
    strictness never drops a High-severity row: a wrong fact is worth reporting
    whatever category the model filed it under.
    """
    strictness = (strictness or DEFAULT_STRICTNESS).strip().lower()
    if strictness not in STRICTNESS_TYPES:
        strictness = DEFAULT_STRICTNESS
    allowed = STRICTNESS_TYPES[strictness]

    rows: list[dict] = []
    for raw in raw_rows if isinstance(raw_rows, (list, tuple)) else []:
        if not isinstance(raw, dict):
            continue
        row = {
            "original_statement": _clean(raw.get("original_statement")),
            "error_type": normalise_error_type(raw.get("error_type")),
            "timestamp": _clean(raw.get("timestamp")),
            "severity": normalise_severity(raw.get("severity")),
            "error_concern": _clean(raw.get("error_concern")),
            "correct_information_correction": _clean(
                raw.get("correct_information_correction")),
            "source_urls": format_source_urls(raw.get("source_urls")),
        }
        # A row with neither a statement nor an explanation says nothing.
        if not row["original_statement"] and not row["error_concern"]:
            continue
        placeholder = row["error_type"] in _PLACEHOLDER_TYPES
        if not placeholder:
            if row["severity"] != "High" and row["error_type"] not in allowed:
                continue
            if not row["correct_information_correction"]:
                row["correct_information_correction"] = NO_CORRECTION_TEXT
            if not row["source_urls"]:
                row["source_urls"] = NO_SOURCE_TEXT
        rows.append(row)
    return _dedupe(rows)


def _dedupe(rows: list[dict]) -> list[dict]:
    """Drop repeats, keeping the first and richest of each.

    The key is the statement AND the error type. One sentence can carry two
    genuinely different faults — a wrong year and a wrong attribution — and a
    key of statement alone would report one and silently drop the other.

    Where a duplicate carries a correction or a source the first did not, those
    are merged in: the later row is redundant, but the fields it filled are not.
    """
    out: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (
            re.sub(r"\W+", "", row["original_statement"].lower())[:160],
            row["error_type"].strip().lower(),
        )
        kept = index.get(key)
        if kept is None:
            index[key] = row
            out.append(row)
            continue
        for field in ("timestamp", "correct_information_correction", "source_urls", "error_concern"):
            if not _informative(kept.get(field)) and _informative(row.get(field)):
                kept[field] = row[field]
    return out


def _informative(value: object) -> bool:
    """Real content, as opposed to one of the sheet's own placeholders."""
    text = (value or "").strip() if isinstance(value, str) else ""
    return bool(text) and text not in {NO_CORRECTION_TEXT, NO_SOURCE_TEXT}


def coverage_row(detail: str) -> dict:
    """The row that admits the report is partial.

    High severity on purpose: "some of this lecture was not checked" outranks
    most individual findings, because it changes how every other row should be
    read.
    """
    return {
        "original_statement": "Coverage of this report is incomplete.",
        "error_type": "Report coverage",
        "severity": "High",
        "error_concern": detail,
        "correct_information_correction": (
            "Re-run the file to get a complete check, or review the parts named "
            "above by hand. Do NOT read the rows below as the full list of errors."
        ),
        "source_urls": "",
    }


def no_issue_rows() -> list[dict]:
    """The single row written when the checks ran and found nothing."""
    return [{
        "original_statement": "No reportable factual/conceptual error found.",
        "error_type": "No issue",
        "severity": "Low",
        "error_concern": (
            "The generated note did not contain any reportable error based on "
            "the enabled checks."
        ),
        "correct_information_correction": "No correction required.",
        "source_urls": "",
    }]


def parse_failure_rows() -> list[dict]:
    """The single row written when the model's JSON could not be recovered."""
    return [{
        "original_statement": "Error report could not be parsed.",
        "error_type": "Report parsing",
        "severity": "Low",
        "error_concern": "AI did not return valid ERROR_REPORT JSON.",
        "correct_information_correction": "Regenerate or review manually.",
        "source_urls": "",
    }]


def summarise(rows: list[dict]) -> dict:
    """Counts for the UI card and for run_log.json.

    The placeholder rows are real rows in the sheet but they are not issues, so
    they are counted as zero — showing "1 issue" for a clean lecture would be a
    lie the reviewer acts on.
    """
    real = [r for r in rows if r.get("error_type") not in _PLACEHOLDER_TYPES]
    return {
        "total": len(real),
        "high": sum(1 for r in real if r.get("severity") == "High"),
        "medium": sum(1 for r in real if r.get("severity") == "Medium"),
        "low": sum(1 for r in real if r.get("severity") == "Low"),
    }


# =============================================================================
# parsing the AI response
# =============================================================================

_ERROR_START_RE = re.compile(r"^[ \t]*#*[ \t]*ERROR_REPORT_START[ \t]*#*[ \t]*$", re.I | re.M)
_ERROR_END_RE = re.compile(r"^[ \t]*#*[ \t]*ERROR_REPORT_END[ \t]*#*[ \t]*$", re.I | re.M)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def extract_error_report_block(text: str) -> tuple[str, str, bool]:
    """Peel the error report off an AI response.

    Returns ``(remaining text, block, found)``. ``found`` is False when no
    ``#ERROR_REPORT_START`` marker was present, which the caller must keep
    distinct from an empty block: "the model gave no report" and "the model
    reported no errors" mean different things and produce different sheets.

    The remaining text is handed on to the existing notes/quality splitter
    untouched, so a model that skips the error markers behaves exactly as it
    did before this feature existed. Cutting the block out also protects the
    document itself — without it a JSON array would be typeset into the notes.
    """
    if not text:
        return "", "", False
    start, end = _ERROR_START_RE.search(text), _ERROR_END_RE.search(text)
    if start is None:
        # An END marker on its own still means the model tried; drop the tail so
        # a half-written array cannot reach the notes.
        if end is not None:
            return (text[: end.start()] + text[end.end():]).strip(), "", False
        return text, "", False
    if end is not None and end.start() >= start.end():
        block = text[start.end(): end.start()]
        remaining = text[: start.start()] + text[end.end():]
    else:
        block = text[start.end():]
        remaining = text[: start.start()]
    return remaining.strip(), block.strip(), True


def _balanced_spans(text: str, opener: str, closer: str) -> list[str]:
    """Every balanced ``opener...closer`` run at nesting depth zero.

    Quote-aware: a bracket inside a string value is not structure. Without that,
    an error_concern reading ``the Act [sic] applies`` ends the array early and
    truncates the report at that row.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            if depth == 0:
                start = i
            depth += 1
        elif ch == closer and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start: i + 1])
                start = -1
    return spans


def _loads(candidate: str):
    """json.loads, retried once without trailing commas. None if unreadable."""
    for attempt in (candidate, re.sub(r",(\s*[\]}])", r"\1", candidate)):
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return None


def _rows_from(data) -> list[dict]:
    """Row dicts out of whatever shape the model chose."""
    if isinstance(data, dict):
        for key in ("rows", "errors", "error_report", "items"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [data]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


@dataclass(frozen=True)
class ParsedReport:
    """What could be read out of the model's report, and what could not.

    ``arrays_found``/``arrays_parsed`` exist so the sheet can say "this covers 2
    of the 3 parts of the lecture" instead of quietly presenting two thirds of
    the findings as the whole answer.
    """

    rows: list[dict]
    ok: bool
    arrays_found: int = 0
    arrays_parsed: int = 0
    salvaged: bool = False          # complete rows recovered from a cut-off reply

    @property
    def complete(self) -> bool:
        return self.ok and not self.salvaged and self.arrays_parsed >= self.arrays_found


def parse_error_report(block: str) -> ParsedReport:
    """Read every finding the model managed to send.

    A long lecture is generated in chunks and each chunk returns its OWN array,
    which the pipeline concatenates. Reading only the first one — which is what
    this did — reported the first chunk's errors and called it a complete report.
    """
    if not block or not block.strip():
        # The prompt asks for [] when there is nothing to say, and a model that
        # emits the markers with nothing between them meant the same thing.
        return ParsedReport([], True)

    stripped = _FENCE_RE.sub("", block).strip()

    # One well-formed document (the common case) — unchanged behaviour.
    whole = _loads(stripped)
    if whole is not None:
        return ParsedReport(_rows_from(whole), True, 1, 1)

    # Several arrays, one per chunk of the lecture. ALL of them.
    arrays = _balanced_spans(stripped, "[", "]")
    array_rows: list[dict] = []
    parsed = 0
    for array in arrays:
        data = _loads(array)
        if data is None:
            continue
        parsed += 1
        array_rows.extend(_rows_from(data))

    # Every complete row object, wherever it sits. This is both the truncation
    # salvage and the backstop for a malformed bracket: one unclosed "[" makes
    # the rest of the reply part of an array that never ends, and reading only
    # arrays would drop perfectly good rows that happen to follow it.
    object_rows: list[dict] = []
    for obj in _balanced_spans(stripped, "{", "}"):
        data = _loads(obj)
        if isinstance(data, dict) and not _looks_like_wrapper(data):
            object_rows.append(data)

    if len(object_rows) > len(array_rows):
        # Rows exist outside anything readable as an array, so whatever the
        # structure was meant to be, it did not survive. Keep the rows, and be
        # honest that the report is not known to be complete.
        return ParsedReport(object_rows, True, max(len(arrays), 1), parsed,
                            salvaged=True)
    if array_rows:
        return ParsedReport(array_rows, True, len(arrays), parsed)
    if object_rows:
        return ParsedReport(object_rows, True, max(len(arrays), 1), 0, salvaged=True)
    return ParsedReport([], False, len(arrays), 0)


def _looks_like_wrapper(data: dict) -> bool:
    """A container object ({"errors": [...]}) rather than a finding.

    Counted as a row it would inflate the object tally and make a perfectly
    healthy wrapped report look partial.
    """
    return any(isinstance(data.get(k), list)
               for k in ("rows", "errors", "error_report", "items"))


def parse_error_report_json(block: str) -> tuple[list[dict], bool]:
    """(rows, parsed_ok) — the older shape, kept for existing callers."""
    parsed = parse_error_report(block)
    return parsed.rows, parsed.ok





def build_report_rows(
    block: str,
    *,
    marker_found: bool,
    strictness: str = DEFAULT_STRICTNESS,
) -> tuple[list[dict], str]:
    """(rows ready for the sheet, status) for one AI response.

    Status is one of ``ok`` / ``no_issues`` / ``parse_failed`` / ``missing`` and
    is recorded in run_log.json, because a report of zero rows means something
    different in each case.
    """
    if not marker_found:
        return parse_failure_rows(), "missing"
    parsed = parse_error_report(block)
    if not parsed.ok:
        return parse_failure_rows(), "parse_failed"
    rows = normalise_rows(parsed.rows, strictness=strictness)
    if not rows and parsed.complete:
        return no_issue_rows(), "no_issues"

    # The report is partial. Say so IN the sheet: a reviewer who reads "3 issues"
    # and stops looking has been misled by a file that never mentioned it was
    # missing a third of the lecture.
    if not parsed.complete:
        if parsed.salvaged:
            detail = (
                "The AI's reply was cut off before it finished. The complete "
                f"findings it had already written ({len(parsed.rows)}) are listed "
                "below; any it had not yet written are missing entirely."
            )
        else:
            unread = parsed.arrays_found - parsed.arrays_parsed
            detail = (
                f"This lecture was checked in {parsed.arrays_found} part(s) and "
                f"{unread} of them could not be read. The rows below cover only "
                f"the {parsed.arrays_parsed} part(s) that were."
            )
        return [coverage_row(detail), *rows], "partial"
    return rows, "ok"


# =============================================================================
# writing the workbook
# =============================================================================

def _thin_border() -> Border:
    side = Side(style="thin", color=BORDER_COLOR)
    return Border(left=side, right=side, top=side, bottom=side)


def _set_text(cell, text: str) -> None:
    """Write text that stays text.

    openpyxl types any string starting with ``=`` as a formula, so a correction
    like ``= GVA at basic prices + product taxes`` would be saved as one and
    render as an error in Excel.
    """
    cell.value = text
    if text and cell.data_type != "s":
        cell.data_type = "s"


def _apply_programmatic_styles(ws: Worksheet, row_count: int) -> None:
    border = _thin_border()
    header_font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE, bold=True,
                       color=HEADER_FONT_COLOR)
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_align = Alignment(horizontal="center", vertical="top", wrap_text=True)

    for col in range(1, len(COLUMNS) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = border
    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT

    body_font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE, color=BODY_FONT_COLOR)
    band_fill = PatternFill("solid", fgColor=BAND_FILL)
    for offset in range(row_count):
        excel_row = 2 + offset
        banded = offset % 2 == 0  # first data row is shaded, as in the reference
        for col in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=excel_row, column=col)
            cell.font = body_font
            cell.border = border
            cell.alignment = Alignment(
                horizontal="right" if col == 1 else "left",
                vertical="top",
                wrap_text=True,
            )
            if banded:
                cell.fill = band_fill


def _template_styles(ws: Worksheet) -> tuple[list | None, list | None]:
    """(banded-row styles, plain-row styles) lifted from the template's own
    example rows, or (None, None) when it has none to lift."""
    if ws.max_row < 2:
        return None, None
    banded = [copy(ws.cell(row=2, column=c)._style) for c in range(1, len(COLUMNS) + 1)]
    plain_row = 3 if ws.max_row >= 3 else 2
    plain = [copy(ws.cell(row=plain_row, column=c)._style) for c in range(1, len(COLUMNS) + 1)]
    return banded, plain


def _apply_severity(ws: Worksheet, row_count: int) -> None:
    """Colour-code and bold the Severity cell. Always last, so it wins over
    whatever banding the template or the programmatic pass put down."""
    for offset in range(row_count):
        cell = ws.cell(row=2 + offset, column=SEVERITY_COLUMN)
        fill = SEVERITY_FILLS.get(cell.value)
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        cell.font = Font(
            name=cell.font.name or BODY_FONT_NAME,
            size=cell.font.size or BODY_FONT_SIZE,
            bold=True,
            color=BODY_FONT_COLOR,
        )


def _finish(ws: Worksheet, *, keep_widths: set[str] | None = None) -> None:
    # A template's own column widths are its author's choice and are kept. The
    # reference sets none for "Error type" or "Severity", though, so those fall
    # back to Google's 12.63 default and break mid-word ("Numerical/ou tdated").
    # Filling in only the columns the template left unset is not an override.
    keep_widths = keep_widths or set()
    for letter, width in COLUMN_WIDTHS.items():
        if letter not in keep_widths:
            ws.column_dimensions[letter].width = width
    # Let Excel auto-fit the wrapped body rows. The reference workbook is a
    # Google Sheets export and carries sheetFormatPr customHeight="1", which
    # suppresses auto-fit — inherit it and every row clips to a single line,
    # hiding most of the correction and the whole of the second URL. That is
    # visible in the reference itself; it must not be copied into the report.
    ws.sheet_format.customHeight = None
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"


def _header_values(ws: Worksheet, count: int) -> list[str]:
    return [
        (ws.cell(row=1, column=c).value or "").strip()
        if isinstance(ws.cell(row=1, column=c).value, str) else ""
        for c in range(1, count + 1)
    ]


def _shift_column_dimensions_right(ws: Worksheet, first_column: int) -> None:
    """Keep legacy template widths attached to their shifted data columns."""
    moved: list[tuple[int, object]] = []
    for letter, dimension in list(ws.column_dimensions.items()):
        if column_index_from_string(letter) < first_column:
            continue
        moved.append((column_index_from_string(letter), copy(dimension)))
        del ws.column_dimensions[letter]
    for index, dimension in sorted(moved, reverse=True):
        new_letter = get_column_letter(index + 1)
        dimension.index = new_letter
        # A loaded worksheet stores the actual column range separately from
        # its human-readable index.  Keeping the old min/max would save the
        # moved width back onto (for example) G even though the key says H.
        dimension.min = index + 1
        dimension.max = index + 1
        ws.column_dimensions[new_letter] = dimension


def _add_timestamp_to_legacy_template(ws: Worksheet) -> None:
    """Insert the requested Timestamp column into an older seven-column sheet."""
    _shift_column_dimensions_right(ws, TIMESTAMP_COLUMN)
    ws.insert_cols(TIMESTAMP_COLUMN)
    for row in range(1, ws.max_row + 1):
        # The neighbouring Error type cell supplies a visually compatible
        # header/body style for the new column. Values are written later.
        ws.cell(row=row, column=TIMESTAMP_COLUMN)._style = copy(
            ws.cell(row=row, column=TIMESTAMP_COLUMN - 1)._style)
    _set_text(ws.cell(row=1, column=TIMESTAMP_COLUMN), "Timestamp")


def _load_template(template_path: Path) -> Worksheet | None:
    """The template's sheet, or None if it cannot serve as one.

    A template that does not carry the expected headers is refused rather
    than half-used: writing our rows into someone else's column order would
    produce a plausible-looking sheet with the data in the wrong places.
    """
    try:
        wb = load_workbook(template_path)
    except Exception as exc:  # noqa: BLE001 - any bad file falls back
        log.warning("Error report template could not be opened (%s); "
                    "building the workbook programmatically.", exc)
        return None
    ws = wb.active
    headers = _header_values(ws, len(COLUMNS))
    if [h.lower() for h in headers] != [c.lower() for c in COLUMNS]:
        legacy_headers = _header_values(ws, len(LEGACY_COLUMNS))
        if [h.lower() for h in legacy_headers] == [c.lower() for c in LEGACY_COLUMNS]:
            _add_timestamp_to_legacy_template(ws)
            return ws
        log.warning("Error report template header is %s, expected %s; "
                    "building the workbook programmatically.", headers, list(COLUMNS))
        return None
    return ws


def write_error_report_xlsx(
    rows: list[dict],
    output_path: Path,
    template_path: Path | None = None,
) -> Path:
    """Write ``rows`` as the Error Report workbook and return its path.

    With a usable ``template_path`` the template's own header, widths, borders
    and banding are kept and only its example rows are replaced. Otherwise an
    identical layout is built from the constants at the top of this module. In
    both cases severity colouring is reapplied last.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = rows or no_issue_rows()

    ws = None
    if template_path is not None:
        template_path = Path(template_path)
        if template_path.exists():
            ws = _load_template(template_path)
        else:
            log.warning("Error report template %s not found; building the "
                        "workbook programmatically.", template_path)

    from_template = ws is not None
    template_widths: set[str] = set()
    if from_template:
        # Read before anything else touches column_dimensions: openpyxl creates
        # an entry on first access, so a later read cannot tell "the template set
        # this" from "something asked about it".
        template_widths = {
            letter for letter, dim in ws.column_dimensions.items()
            if dim.customWidth and dim.width
        }
        banded_styles, plain_styles = _template_styles(ws)
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row)
        # The reference sheet declares A1:Z1000, so a plain round-trip carries
        # 19 empty columns and a stray used-range into every report.
        if ws.max_column > len(COLUMNS):
            ws.delete_cols(len(COLUMNS) + 1, ws.max_column - len(COLUMNS))
        ws.title = SHEET_NAME
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        for col, heading in enumerate(COLUMNS, start=1):
            _set_text(ws.cell(row=1, column=col), heading)
        banded_styles = plain_styles = None

    for offset, row in enumerate(rows):
        excel_row = 2 + offset
        number = ws.cell(row=excel_row, column=1)
        number.value = offset + 1
        for col, field in enumerate(FIELDS, start=2):
            _set_text(ws.cell(row=excel_row, column=col), _clean(row.get(field)))
        if banded_styles and plain_styles:
            styles = banded_styles if offset % 2 == 0 else plain_styles
            for col in range(1, len(COLUMNS) + 1):
                ws.cell(row=excel_row, column=col)._style = copy(styles[col - 1])

    if not from_template:
        _apply_programmatic_styles(ws, len(rows))
    _apply_severity(ws, len(rows))
    _finish(ws, keep_widths=template_widths)

    ws.parent.save(output_path)
    return output_path
