"""The Error Report XLSX — the only report a reviewer is given.

One sheet, seven columns, one row per thing that was actually wrong in the
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
from pathlib import Path
import json
import logging
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

log = logging.getLogger(__name__)

SHEET_NAME = "Error Report"

#: The seven columns, in order. This tuple is the contract — the AI prompt, the
#: pipeline and the tests all read it from here.
COLUMNS: tuple[str, ...] = (
    "Error. No.",
    "Original statement",
    "Error type",
    "Severity",
    "Error / concern",
    "Correct information / correction",
    "Source URL(s)",
)

#: Keys of a row dict, aligned index-for-index with COLUMNS[1:].
FIELDS: tuple[str, ...] = (
    "original_statement",
    "error_type",
    "severity",
    "error_concern",
    "correct_information_correction",
    "source_urls",
)

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
COLUMN_WIDTHS = {"A": 11, "B": 40, "C": 25, "D": 15, "E": 45, "F": 52, "G": 60}

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
})
_STANDARD_TYPES = _BASIC_TYPES | frozenset({
    "Numerical/outdated",
    "Outdated data",
    "Current-affairs update",
    "Missing qualifier",
    "Overgeneralised claim",
})
_STRICT_TYPES = _STANDARD_TYPES | frozenset(ERROR_TYPES)

STRICTNESS_TYPES: dict[str, frozenset[str]] = {
    "basic": _BASIC_TYPES,
    "standard": _STANDARD_TYPES,
    "strict": _STRICT_TYPES,
}

#: Rows the report writes about itself rather than about the lecture. They are
#: never filtered out and never carry "Source needed".
#:
#: "Check not run" is the one addition this copy makes to the note-generation
#: project's writer. That app always had a model to ask, so its three outcomes
#: (found issues / found none / could not parse the answer) covered every case.
#: This app can be run with the AI stages unavailable — no sign-in, a denied
#: proxy, a failed call — and reporting that as "could not be parsed" would be a
#: lie about what happened. It is a placeholder type, so like the other two it
#: is written into the sheet, never filtered, and never counted as an issue.
_PLACEHOLDER_TYPES = frozenset({"No issue", "Report parsing", "Check not run",
                                "Incomplete scan"})

#: Every error type this app recognises. A type outside this set was invented or
#: paraphrased by the model ("Factual error" for "Factual"), and is therefore
#: EXEMPT from strictness filtering rather than deleted by it — an unfamiliar
#: label is not a statement about scope, and dropping the row loses a real error
#: over a wording difference. This is what the ERROR_TYPES note above always
#: promised; until now the filter did the opposite.
_ALL_KNOWN_TYPES = frozenset(ERROR_TYPES)


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
    dropped: list[dict] | None = None,
) -> list[dict]:
    """Clean, filter and number the rows that will become the sheet.

    Rows that carry no usable content at all are dropped. Filtering by
    strictness never drops a High-severity row: a wrong fact is worth reporting
    whatever category the model filed it under, and never drops a row whose
    type this app does not recognise, because an unfamiliar label is not a
    statement about scope.

    Pass ``dropped`` to receive every row the strictness filter removed. The
    caller is expected to report that count: a report filtered down to nothing
    must never be presentable as a clean document.
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
            # Strictness filtering applies only to types this app RECOGNISES.
            # A type the model invented or paraphrased ("Factual error" for
            # "Factual") is not evidence the finding is out of scope, and
            # deleting it loses a real error over a wording difference — see
            # the ERROR_TYPES note above, which this code used to contradict.
            known = row["error_type"] in _ALL_KNOWN_TYPES
            if (row["severity"] != "High" and known
                    and row["error_type"] not in allowed):
                # Out of scope for this strictness. Deliberate — but never
                # silent: the caller counts these and reports the number, so a
                # report filtered down to nothing can never read as "clean".
                if dropped is not None:
                    dropped.append(row)
                continue
            if not row["correct_information_correction"]:
                row["correct_information_correction"] = NO_CORRECTION_TEXT
            if not row["source_urls"]:
                row["source_urls"] = NO_SOURCE_TEXT
        rows.append(row)
    return rows


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


def unavailable_rows(reason: str = "") -> list[dict]:
    """The single row written when the content check could not run at all.

    Distinct from parse_failure_rows(): "nobody looked" and "the answer came
    back unreadable" are different facts, and a reviewer acts differently on
    each. Added by the standalone app — see _PLACEHOLDER_TYPES above.
    """
    return [{
        "original_statement": "The factual check did not run for this file.",
        "error_type": "Check not run",
        "severity": "Low",
        "error_concern": (
            reason or "The AI content check was unavailable for this run, so the "
            "notes were not checked for factual or conceptual errors."
        ) + " An empty report here does NOT mean the notes are error-free.",
        "correct_information_correction": (
            "Sign in and run QC again to have the content checked."),
        "source_urls": "",
    }]


def incomplete_scan_row(detail: str = "") -> dict:
    """The row that says this report is not the whole story.

    A per-file Error Report is often read on its own, away from the summary
    sheet that carries the run's warnings. Without this row, a scan that covered
    half the document looks exactly like a scan that found only a few errors.
    It is a placeholder type, so it is excluded from the severity counts.
    """
    return {
        "original_statement": "THIS REPORT IS INCOMPLETE — part of the document was not checked.",
        "error_type": "Incomplete scan",
        "severity": "High",
        "error_concern": (
            (detail or "Part of this document was never checked.")
            + " The rows below are only what was found in the part that was "
            "checked. Treat the error count as a FLOOR, not a total — this is "
            "NOT evidence that the rest of the document is clean."
        ),
        "correct_information_correction": (
            "Re-run QC on this file. If it keeps happening, raise "
            "ERROR_REPORT_MAX_OUTPUT_TOKENS."),
        "source_urls": "",
    }


def all_refuted_rows(count: int) -> list[dict]:
    """The row written when the verification pass rejected every finding.

    Distinct from no_issue_rows(): "the AI found nothing" and "the AI found
    things and the audit threw all of them out" are different facts. The second
    is usually right — it is what stops recency false positives reaching the
    sheet — but a reviewer is entitled to know it happened, and to go and look
    at verification.json if the file matters.
    """
    return [{
        "original_statement": (
            f"No errors are listed: all {count} finding(s) were rejected on review."),
        "error_type": "No issue",
        "severity": "Low",
        "error_concern": (
            f"The AI reported {count} possible error(s). A second pass examined "
            f"each one and judged none of them to be real — most often because a "
            f"figure or scheme was called 'outdated' when it is simply newer than "
            f"the model's knowledge. Each rejected claim and its reason is in "
            f"verification.json in the run folder."),
        "correct_information_correction": (
            "No correction needed. Review verification.json if this file is "
            "high-stakes."),
        "source_urls": "",
    }]


def filtered_out_rows(count: int, strictness: str) -> list[dict]:
    """The row written when strictness removed every finding there was.

    An empty report and an emptied report are different facts. Without this a
    run that found five Low-severity issues and filtered all five would be
    indistinguishable from a run that found nothing wrong.
    """
    return [{
        "original_statement": (
            f"{count} finding(s) were removed by the '{strictness}' strictness level."),
        "error_type": "No issue",
        "severity": "Low",
        "error_concern": (
            f"The AI reported {count} issue(s), but none of them are in scope at "
            f"the '{strictness}' strictness level, so none are listed. This is NOT "
            f"the same as the notes being error-free. Re-run at 'strict' to see them."),
        "correct_information_correction": (
            "Re-run this file with strictness set to 'strict' to see what was removed."),
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


def _iter_json_candidates(block: str):
    """Progressively less trusting attempts to read a JSON array out of text."""
    stripped = _FENCE_RE.sub("", block).strip()
    yield stripped
    # A bare array somewhere inside prose.
    depth = 0
    start = -1
    for i, ch in enumerate(stripped):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                yield stripped[start: i + 1]
                start = -1
    # Trailing commas are the single most common malformation.
    yield re.sub(r",(\s*[\]}])", r"\1", stripped)


def parse_error_report_json(block: str) -> tuple[list[dict], bool]:
    """(rows, parsed_ok) from the raw text between the ERROR_REPORT markers.

    An empty or whitespace-only block is a *valid* empty report — the prompt
    asks for ``[]`` when there is nothing to say, and a model that emits the
    markers with nothing between them meant the same thing.
    """
    if not block or not block.strip():
        return [], True
    for candidate in _iter_json_candidates(block):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            for key in ("rows", "errors", "error_report", "items"):
                if isinstance(data.get(key), list):
                    return [r for r in data[key] if isinstance(r, dict)], True
            return [data], True
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)], True
    return [], False


def build_report_rows(
    block: str,
    *,
    marker_found: bool,
    strictness: str = DEFAULT_STRICTNESS,
    dropped: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """(rows ready for the sheet, status) for one AI response.

    Status is one of ``ok`` / ``no_issues`` / ``parse_failed`` / ``missing`` and
    is recorded in run_log.json, because a report of zero rows means something
    different in each case.
    """
    if not marker_found:
        return parse_failure_rows(), "missing"
    raw, parsed_ok = parse_error_report_json(block)
    if not parsed_ok:
        return parse_failure_rows(), "parse_failed"
    rows = normalise_rows(raw, strictness=strictness, dropped=dropped)
    if not rows:
        # Distinguish "the model found nothing" from "everything it found was
        # filtered out by this strictness level". Reporting the second as the
        # first certifies a file clean on the strength of rows we deleted.
        if dropped:
            return filtered_out_rows(len(dropped), strictness), "filtered_out"
        return no_issue_rows(), "no_issues"
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
        cell = ws.cell(row=2 + offset, column=4)
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


def _load_template(template_path: Path) -> Worksheet | None:
    """The template's sheet, or None if it cannot serve as one.

    A template that does not carry the seven expected headers is refused rather
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
    headers = [
        (ws.cell(row=1, column=c).value or "").strip() if isinstance(
            ws.cell(row=1, column=c).value, str) else ""
        for c in range(1, len(COLUMNS) + 1)
    ]
    if [h.lower() for h in headers] != [c.lower() for c in COLUMNS]:
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
