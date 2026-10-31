"""Classify every tab of a workbook in a handful of requests.

A production workbook holds one tab per batch plus a computed "Master", an
index tab, empty "Format" templates and the odd scratch sheet. To let someone
pick the right tab the bridge looks at all of them at once:

1. one ``spreadsheets.get`` for the tab list,
2. one ``values.batchGet`` for every tab's header row -> a Layout per tab,
3. one ``values.batchGet`` for the status columns of the tabs that could run
   -> data-row and eligible-row counts,
4. one ``spreadsheets.get`` with grid data for the first data row of those
   tabs -> whether the tab is typed into or computed from other tabs.

Four round trips for a 28-tab workbook instead of one per tab.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import columns as C
from .rows import _is_done, is_regenerate_request
from .sheets import KIND_SPILL, a1_sheet_prefix, cell_kinds

_RETRIES = 3

KIND_NOTES = "notes"          # a real notes tab: can be run
KIND_TEMPLATE = "template"    # right header, no data rows
KIND_COMPUTED = "computed"    # filled by array formulas from other tabs
KIND_OTHER = "other"          # not a notes tab (index, scratch, audio ...)


@dataclass(frozen=True)
class TabSummary:
    gid: int
    title: str
    rows: int                       # non-blank data rows
    columns: int
    kind: str
    supported: bool                 # kind == notes: can be run on its own
    complete: bool                  # supported AND has every recommended column - safe to run unattended
    eligible: int | None            # J = Done and M != Done (None when unknown)
    missing: tuple[str, ...]        # required columns not found
    mapping: dict                   # logical column -> letter
    note: str = ""
    catalog: bool = False           # PDF-link catalog: notes are filed in a Drive tree

    def public(self) -> dict:
        data = asdict(self)
        data["missing"] = list(self.missing)
        return data


def _headers(service, spreadsheet_id: str, titles: list[str]) -> list[list]:
    if not titles:
        return []
    result = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"{a1_sheet_prefix(t)}!1:1" for t in titles],
            majorDimension="ROWS",
        )
        .execute(num_retries=_RETRIES)
    )
    out: list[list] = []
    for value_range in result.get("valueRanges", []):
        values = value_range.get("values") or []
        out.append(list(values[0]) if values else [])
    while len(out) < len(titles):
        out.append([])
    return out


def _count_columns(layout: C.Layout) -> tuple[int, int, int, int]:
    """Columns the ready-row count reads: the link, the trigger, the final
    status and the review status - or, on a catalog tab, the link and the two
    status columns the bridge writes itself (the review one twice)."""
    if layout.catalog:
        return (layout.require("drive_link"), layout.auto_status_index, layout.review_status_index,
                layout.review_status_index)
    return (layout.require("drive_link"), layout.require("bm_status"), layout.require("final_status"),
            layout.review_status_index)


def _ready(layout: C.Layout, link: str, first: str, second: str, review: str = "") -> bool:
    """Mirrors rows.is_eligible on the cells the count reads; a row whose
    Review Status says "Regenerate" is ready whatever its status."""
    if not link:
        return False
    if layout.catalog:
        if is_regenerate_request(second):
            return True
        status = first.strip().partition(" ")[0].rstrip(":").lower()
        return status not in (C.STATUS_DONE, "done", C.STATUS_SKIPPED) and not second.strip()
    if is_regenerate_request(review):
        return _is_done(first)
    return _is_done(first) and not _is_done(second)


def _status_blocks(service, spreadsheet_id: str, entries: list[tuple[str, C.Layout]]) -> list[tuple[int, int, int]]:
    """(non-blank rows, eligible rows, first data row number) per entry."""
    if not entries:
        return []
    ranges = []
    for title, layout in entries:
        indexes = _count_columns(layout)
        lo, hi = min(indexes), max(indexes)
        ranges.append(f"{a1_sheet_prefix(title)}!{C.column_letter(lo)}2:{C.column_letter(hi)}")
    result = (
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges, majorDimension="ROWS")
        .execute(num_retries=_RETRIES)
    )
    out: list[tuple[int, int, int]] = []
    value_ranges = result.get("valueRanges", [])
    for position, (title, layout) in enumerate(entries):
        values = value_ranges[position].get("values") or [] if position < len(value_ranges) else []
        columns = _count_columns(layout)
        lo = min(columns)
        link_at, first_at, second_at, review_at = (index - lo for index in columns)
        non_blank = eligible = 0
        first_row = 0
        for offset, row in enumerate(values):
            cells = [str(c).strip() if c is not None else "" for c in row]
            if not any(cells):
                continue
            non_blank += 1
            if not first_row:
                first_row = offset + 2
            get = lambda i: cells[i] if i < len(cells) else ""  # noqa: E731
            if _ready(layout, get(link_at), get(first_at), get(second_at), get(review_at)):
                eligible += 1
        out.append((non_blank, eligible, first_row))
    return out


def _first_row_kinds(service, spreadsheet_id: str, entries: list[tuple[int, str, int, int]]) -> dict[int, dict[int, str]]:
    """gid -> cell kinds of one row, for (gid, title, row_number, last_index)."""
    if not entries:
        return {}
    ranges = [
        f"{a1_sheet_prefix(title)}!A{row}:{C.column_letter(max(last, 0))}{row}"
        for _gid, title, row, last in entries
    ]
    result = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            includeGridData=True,
            fields="sheets(properties(sheetId),data(rowData(values(userEnteredValue,effectiveValue))))",
        )
        .execute(num_retries=_RETRIES)
    )
    kinds: dict[int, dict[int, str]] = {}
    for sheet in result.get("sheets", []):
        gid = int((sheet.get("properties") or {}).get("sheetId", -1))
        for data in sheet.get("data", []):
            for row in data.get("rowData", []):
                kinds[gid] = cell_kinds(row.get("values", []))
                break
    return kinds


def list_tabs(service, spreadsheet_id: str) -> tuple[str, list[TabSummary]]:
    """(workbook title, one summary per tab) - see the module docstring."""
    meta = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="properties(title),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
        )
        .execute(num_retries=_RETRIES)
    )
    workbook = str((meta.get("properties") or {}).get("title", "") or "")
    props = [sheet.get("properties", {}) for sheet in meta.get("sheets", [])]
    titles = [str(p.get("title", "")) for p in props]
    layouts = [C.Layout.from_header(h) for h in _headers(service, spreadsheet_id, titles)]

    runnable = [(i, titles[i], layouts[i]) for i in range(len(props)) if not layouts[i].problems()]
    counts = _status_blocks(service, spreadsheet_id, [(t, l) for _i, t, l in runnable])
    probes = [
        (int(props[i].get("sheetId", -1)), titles[i], first_row, layouts[i].last_index)
        for (i, _t, _l), (_n, _e, first_row) in zip(runnable, counts)
        if first_row
    ]
    kinds = _first_row_kinds(service, spreadsheet_id, probes)

    by_index = {i: (n, e, first) for (i, _t, _l), (n, e, first) in zip(runnable, counts)}
    summaries: list[TabSummary] = []
    for i, p in enumerate(props):
        gid = int(p.get("sheetId", -1))
        grid = p.get("gridProperties", {}) or {}
        layout = layouts[i]
        problems = layout.problems()
        if problems:
            summaries.append(TabSummary(
                gid=gid, title=titles[i], rows=0, columns=int(grid.get("columnCount", 0)),
                kind=KIND_OTHER, supported=False, complete=False, eligible=None,
                missing=tuple(layout.missing()), mapping=layout.describe(), note="; ".join(problems),
            ))
            continue
        non_blank, eligible, first_row = by_index.get(i, (0, 0, 0))
        note = ""
        kind = KIND_NOTES
        if not non_blank:
            kind = KIND_TEMPLATE
            note = "no data rows"
        else:
            row_kinds = kinds.get(gid, {})
            for name in ("bm_status", "final_status"):
                if row_kinds.get(layout.index(name)) == KIND_SPILL:
                    kind = KIND_COMPUTED
                    note = "filled by an array formula from other tabs (read-only)"
                    break
        if kind == KIND_NOTES and layout.missing_recommended():
            note = "no " + " / ".join(C.LABELS[n] for n in layout.missing_recommended()) + " column: folder links cannot be resolved"
        elif kind == KIND_NOTES and layout.catalog:
            note = "catalog of PDF links: notes are filed in the profile's output Drive folder"
        summaries.append(TabSummary(
            gid=gid, title=titles[i], rows=non_blank, columns=int(grid.get("columnCount", 0)),
            kind=kind, supported=kind == KIND_NOTES,
            complete=kind == KIND_NOTES and not layout.missing_recommended(),
            eligible=eligible if kind != KIND_OTHER else None,
            missing=(), mapping=layout.describe(), note=note, catalog=layout.catalog,
        ))
    return workbook, summaries
