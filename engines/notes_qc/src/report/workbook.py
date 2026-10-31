"""The QC workbook: the same Error Report, for one PDF or for twenty.

The single-file case is the note-generation project's report, unchanged. Each
PDF gets its own workbook written by that project's own
``write_error_report_xlsx()`` — same sheet name, same seven columns, same
palette, same severity colouring, same defensive string typing. A report this
app produces for one PDF is meant to be comparable, row for row, with the
report the original app produced for the same lecture.

The combined workbook is the only addition, and it exists because this app
accepts a batch. Its "Error Report" sheet carries the same seven columns in the
same order — a "Source PDF" column is appended *after* them, and only when there
is more than one file, so the contract's column positions never move. The two
other sheet is a per-file summary. There is no format/visual sheet: this app
reports what is wrong in the NOTES, and how the page is drawn is a typesetting
matter for a different tool.
"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..error_report_writer import (
    BAND_FILL,
    BODY_FONT_COLOR,
    BODY_FONT_NAME,
    BODY_FONT_SIZE,
    BORDER_COLOR,
    COLUMNS,
    FIELDS,
    HEADER_FILL,
    HEADER_FONT_COLOR,
    HEADER_ROW_HEIGHT,
    incomplete_scan_row,
    SEVERITY_FILLS,
    _clean,
    _set_text,
    write_error_report_xlsx,
)
from ..models import PdfQcResult
from ..utils import safe_name

SUMMARY_SHEET = "QC Summary"
ERRORS_SHEET = "Error Report"

SUMMARY_COLUMNS: tuple[str, ...] = (
    "PDF Name",
    "Status",
    "Pages",
    "Total Errors",
    "High",
    "Medium",
    "Low",
    "Notes",
)
SUMMARY_WIDTHS = {"A": 46, "B": 24, "C": 8, "D": 13, "E": 8, "F": 9, "G": 8, "H": 70}


#: Excel forbids these in a sheet name, and caps the name at 31 characters.
_BAD_SHEET_CHARS = str.maketrans({c: "-" for c in r"[]:*?/\'"})

#: How the per-file status reads in the summary sheet.
_STATUS_LABEL = {
    "completed": "Clean",
    "completed_with_warnings": "Issues found",
    "failed": "QC FAILED",
    "pending": "Not run",
}
_STATUS_FILL = {
    "Clean": "D9EAD3",
    "Issues found": "FDE68A",
    "QC FAILED": "FCA5A5",
    "Not run": "E5E7EB",
    # Orange, not green or yellow: this file's numbers cannot be read as a
    # verdict, and the colour has to stop a reviewer the way a failure would.
    "PARTIAL SCAN": "FDBA74",
}


def _status_label(r: PdfQcResult) -> str:
    """The status text for one file, in the one place both callers read it.

    A partial scan is not a verdict, so it never borrows "Clean" or "Issues
    found". This is computed once because the label and its colour are set in
    two separate passes, and the two drifting apart is how a partial file ends
    up wearing a green cell.
    """
    if not getattr(r, "error_report_complete", True) and r.status != "failed":
        return "PARTIAL SCAN"
    return _STATUS_LABEL.get(r.status, r.status)


def _rows_for(r: PdfQcResult) -> list[dict]:
    """A file's error rows, with the incomplete-scan banner in front if needed.

    Used by every sheet that lists errors — the combined Error Report, the
    per-file sheets, and the standalone per-file workbook — so a partial scan
    announces itself wherever its rows are read, not only on the summary sheet
    the reader may never open.
    """
    rows = list(r.error_rows or [])
    if rows and not getattr(r, "error_report_complete", True):
        detail = next((w for w in r.warnings
                       if w.startswith("INCOMPLETE COVERAGE")), "")
        return [incomplete_scan_row(detail)] + rows
    return rows


def _border() -> Border:
    side = Side(style="thin", color=BORDER_COLOR)
    return Border(left=side, right=side, top=side, bottom=side)


def _write_header(ws: Worksheet, columns: tuple[str, ...],
                  widths: dict[str, int]) -> None:
    border = _border()
    font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE, bold=True,
                color=HEADER_FONT_COLOR)
    fill = PatternFill("solid", fgColor=HEADER_FILL)
    align = Alignment(horizontal="center", vertical="top", wrap_text=True)
    for col, heading in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col)
        _set_text(cell, heading)
        cell.font = font
        cell.fill = fill
        cell.alignment = align
        cell.border = border
    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width
    # Left unset on purpose, exactly as in the original writer: Excel auto-fits a
    # wrapped row only while no explicit height is stored, and pinning one would
    # clip the long problem and correction cells this report exists to carry.
    ws.sheet_format.customHeight = None
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"


def _style_body(ws: Worksheet, columns: tuple[str, ...], row_count: int,
                *, right_align_first: bool = False) -> None:
    border = _border()
    font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE, color=BODY_FONT_COLOR)
    band = PatternFill("solid", fgColor=BAND_FILL)
    for offset in range(row_count):
        excel_row = 2 + offset
        banded = offset % 2 == 0      # first data row is shaded, as in the reference
        for col in range(1, len(columns) + 1):
            cell = ws.cell(row=excel_row, column=col)
            cell.font = font
            cell.border = border
            cell.alignment = Alignment(
                horizontal="right" if (right_align_first and col == 1) else "left",
                vertical="top", wrap_text=True)
            if banded:
                cell.fill = band


def _colour_severity(ws: Worksheet, column: int, row_count: int) -> None:
    """Same severity colours and bolding as the original writer, applied last."""
    for offset in range(row_count):
        cell = ws.cell(row=2 + offset, column=column)
        fill = SEVERITY_FILLS.get(cell.value)
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        cell.font = Font(name=cell.font.name or BODY_FONT_NAME,
                         size=cell.font.size or BODY_FONT_SIZE,
                         bold=True, color=BODY_FONT_COLOR)


def _sheet_title(name: str, used: set[str]) -> str:
    base = Path(name).stem.translate(_BAD_SHEET_CHARS).strip() or "PDF"
    base = base[:31]
    title = base
    n = 2
    while title.casefold() in used:
        suffix = f" ({n})"
        title = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(title.casefold())
    return title


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------

def _write_summary(ws: Worksheet, results: list[PdfQcResult]) -> None:
    _write_header(ws, SUMMARY_COLUMNS, SUMMARY_WIDTHS)
    for offset, r in enumerate(results):
        row = 2 + offset
        counts = r.error_report_summary or {}
        status = _status_label(r)
        incomplete = not getattr(r, "error_report_complete", True)
        note = r.error
        if not note:
            bits = []
            # Coverage first: it changes how every other number on the row
            # should be read, so it cannot be buried behind them.
            coverage = [w for w in r.warnings if w.startswith("INCOMPLETE COVERAGE")]
            bits.extend(coverage)
            if incomplete:
                bits.append("The error counts on this row are a FLOOR, not a "
                            "total — part of the document was never checked.")
            # Everything else the run wanted to say about this file. Filtering
            # the Notes column to coverage alone hid the verification drops and
            # the strictness drops, which change how the counts should be read
            # just as much.
            bits.extend(w for w in r.warnings
                        if w not in coverage
                        and ("verification pass" in w
                             or "strictness level" in w
                             or "recovered from an AI reply" in w))
            if r.error_report_status in ("missing", "parse_failed", "failed"):
                bits.append(f"Error report: {r.error_report_status.replace('_', ' ')}")
            skipped = [name for name, state in r.stages.items()
                       if state in ("failed", "unavailable")]
            if skipped:
                bits.append("Stages not completed: " + ", ".join(sorted(skipped)))
            note = " · ".join(bits)
        values = [
            r.file_name, status, r.page_count,
            int(counts.get("total", 0)), int(counts.get("high", 0)),
            int(counts.get("medium", 0)), int(counts.get("low", 0)),
            note,
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col)
            if isinstance(value, int):
                cell.value = value
            else:
                _set_text(cell, _clean(value))
    _style_body(ws, SUMMARY_COLUMNS, len(results))
    for offset, r in enumerate(results):
        cell = ws.cell(row=2 + offset, column=2)
        fill = _STATUS_FILL.get(_status_label(r))
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        cell.font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE, bold=True,
                         color=BODY_FONT_COLOR)



#: A source cell can hold several URLs, separated by "; ". Excel gives one cell
#: one hyperlink, so the cell links to the FIRST and shows them all — a
#: reviewer clicks to open the primary source and can still copy the rest.
_URL_RE = re.compile(r"https?://[^\s;,]+")


def _link_sources(cell) -> None:
    """Turn a Source URL cell into a clickable link, if it holds one.

    Left alone when the cell is the writer's "Source needed" placeholder or
    holds no URL at all: styling those blue and underlined would promise a
    link that is not there.
    """
    text = str(cell.value or "")
    match = _URL_RE.search(text)
    if not match:
        return
    url = match.group(0).rstrip(").,;")
    try:
        cell.hyperlink = url
    except Exception:
        return
    cell.font = Font(name=BODY_FONT_NAME, size=BODY_FONT_SIZE,
                     color="0563C1", underline="single")


def _link_all_sources(ws: Worksheet, row_count: int) -> None:
    """Link every Source URL cell, AFTER the body styling has been applied.

    _style_body repaints the font of every cell it touches, so linking before
    it leaves a working hyperlink with no blue underline — clickable, but with
    nothing to tell a reader it is clickable.
    """
    column = 1 + len(FIELDS)
    for offset in range(row_count):
        _link_sources(ws.cell(row=2 + offset, column=column))


def _write_errors(ws: Worksheet, results: list[PdfQcResult],
                  *, with_source_column: bool) -> int:
    columns = COLUMNS + (("Source PDF",) if with_source_column else ())
    widths = {"A": 11, "B": 40, "C": 25, "D": 15, "E": 45, "F": 52, "G": 60}
    if with_source_column:
        widths[get_column_letter(len(columns))] = 40
    _write_header(ws, columns, widths)

    written = 0
    for r in results:
        for row in _rows_for(r):
            excel_row = 2 + written
            ws.cell(row=excel_row, column=1).value = written + 1
            for col, field in enumerate(FIELDS, start=2):
                _set_text(ws.cell(row=excel_row, column=col), _clean(row.get(field)))
            if with_source_column:
                _set_text(ws.cell(row=excel_row, column=len(columns)),
                          _clean(r.file_name))
            written += 1
    _style_body(ws, columns, written, right_align_first=True)
    _colour_severity(ws, 4, written)
    _link_all_sources(ws, written)
    return written


def _write_per_file(ws: Worksheet, result: PdfQcResult) -> None:
    """One file's rows in exactly the original seven columns."""
    _write_header(ws, COLUMNS, {"A": 11, "B": 40, "C": 25, "D": 15,
                                "E": 45, "F": 52, "G": 60})
    rows = _rows_for(result)
    for offset, row in enumerate(rows):
        excel_row = 2 + offset
        ws.cell(row=excel_row, column=1).value = offset + 1
        for col, field in enumerate(FIELDS, start=2):
            _set_text(ws.cell(row=excel_row, column=col), _clean(row.get(field)))
    _style_body(ws, COLUMNS, len(rows), right_align_first=True)
    _colour_severity(ws, 4, len(rows))
    _link_all_sources(ws, len(rows))


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def write_individual_report(result: PdfQcResult, output_dir: Path) -> Path | None:
    """One PDF's Error Report, written by the original writer, unchanged.

    This is the file to compare against the note-generation app's own
    ``<stem>_error_report.xlsx`` — it is produced by the same function, from the
    same rows, with the same normalisation applied.
    """
    if not result.error_rows:
        return None
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_name(Path(result.file_name).stem, default="notes")
    path = output_dir / f"{stem}_error_report.xlsx"

    # This file is usually read on its own, away from the summary sheet that
    # carries the run's warnings, so the banner has to travel with the rows.
    return write_error_report_xlsx(_rows_for(result), path)


def write_qc_workbook(results: list[PdfQcResult], output_path: Path) -> Path:
    """The run's combined workbook.

    Sheets: QC Summary, Error Report, Format & Visual QC, then one sheet per
    file when the run carried more than one.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    multi = len(results) > 1

    wb = Workbook()
    _write_summary(wb.active, results)
    wb.active.title = SUMMARY_SHEET
    _write_errors(wb.create_sheet(ERRORS_SHEET), results, with_source_column=multi)

    if multi:
        used = {SUMMARY_SHEET.casefold(), ERRORS_SHEET.casefold()}
        for r in results:
            _write_per_file(wb.create_sheet(_sheet_title(r.file_name, used)), r)

    wb.save(output_path)
    return output_path
