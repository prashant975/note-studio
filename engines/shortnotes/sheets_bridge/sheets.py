"""Sheets values-API client: resolve tab by gid, read the tab, write only the
bridge's own cells.

Never read via CSV/markdown export: column A contains " | " separators and any
text export shreds it into fake columns. Never resolve the tab by name: names
get truncated and renamed; the gid is stable.

Cells are addressed through the tab's :class:`~sheets_bridge.columns.Layout`
(set by the runner once it has read the header row), so a tab whose columns
sit in a different order is written in the right place.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import columns as C
from .errors import BridgeError

_RETRIES = 3

# What a cell holds, as seen through spreadsheets.get(includeGridData=True):
KIND_EMPTY = "empty"       # nothing
KIND_LITERAL = "literal"   # a value someone typed
KIND_FORMULA = "formula"   # a formula in this very cell
KIND_SPILL = "spill"       # a value produced by an array formula in another cell


def guard_cell(value: object) -> object:
    """Prevent a string from being parsed as a formula under USER_ENTERED."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def a1_sheet_prefix(title: str) -> str:
    """Quote a tab title for A1 notation ('' doubles a single quote)."""
    return "'" + title.replace("'", "''") + "'"


def cell_kinds(api_cells: list) -> dict[int, str]:
    """Classify the cells of one ``rowData`` entry by index.

    A cell with a ``userEnteredValue`` was typed (or holds its own formula);
    one with only an ``effectiveValue`` is being *filled* by an array formula
    elsewhere - writing into it breaks that formula into ``#REF!``.
    """
    kinds: dict[int, str] = {}
    for index, cell in enumerate(api_cells or []):
        entered = cell.get("userEnteredValue") if isinstance(cell, dict) else None
        effective = cell.get("effectiveValue") if isinstance(cell, dict) else None
        if entered and "formulaValue" in entered:
            kinds[index] = KIND_FORMULA
        elif entered:
            kinds[index] = KIND_LITERAL
        elif effective:
            kinds[index] = KIND_SPILL
        else:
            kinds[index] = KIND_EMPTY
    return kinds


def apply_cell_kinds(layout: C.Layout, kinds: dict[int, str]) -> tuple[C.Layout, list[str]]:
    """Decide whether this tab can be written, from what its cells hold.

    * The status columns being *spilled* by an array formula means the tab is
      a computed roll-up of other tabs (a "Master"); marking it Done would
      shatter the formula. Refused.
    * ``Final File Status`` holding a formula cannot be set to Done. Refused.
    * The final timestamp holding a formula (``=IF(P2,P2,IF(M2="Done",now(),))``
      is common) means the sheet stamps itself; the bridge leaves the cell alone.

    Returns the (possibly adjusted) layout and human-readable notes.
    """
    notes: list[str] = []
    for name in ("bm_status", "final_status"):
        index = layout.index(name)
        if index is not None and kinds.get(index) == KIND_SPILL:
            raise BridgeError(
                "computed_tab",
                f"{C.LABELS[name]!r} is filled by an array formula from other tabs; "
                "run the source tabs instead",
            )
    final = layout.index("final_status")
    if final is not None and kinds.get(final) == KIND_FORMULA:
        raise BridgeError(
            "computed_status_column",
            f"{C.LABELS['final_status']!r} is a formula, so it cannot be marked Done",
        )
    stamp = layout.index("final_timestamp")
    if stamp is not None and kinds.get(stamp) == KIND_FORMULA:
        notes.append(
            f"column {C.column_letter(stamp)} stamps itself with a formula - leaving it to the sheet"
        )
        layout = layout.without_timestamp()
    return layout, notes


def control_cells_in_use(layout: C.Layout, header_row: list, rows) -> list[str]:
    """Letters of would-be control columns that have no header yet hold data.

    Without a control header the columns are created where the header row
    ends - but a column someone uses without a header (a 'Remarks' column, a
    header cell that was cleared) would then be overwritten row by row.
    Only slots whose header cell is blank are checked; an existing control
    column legitimately holds 'claimed' / 'done' values.
    """
    used: list[str] = []
    for index in layout.control_indexes:
        header = header_row[index] if index < len(header_row) else None
        if str(header or "").strip():
            continue
        if any(row.cell(index).strip() for row in rows):
            used.append(C.column_letter(index))
    return used


@dataclass(frozen=True)
class TabInfo:
    title: str
    sheet_id: int
    column_count: int
    row_count: int


class SheetClient:
    def __init__(self, service, spreadsheet_id: str, gid: int) -> None:
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.gid = int(gid)
        self._tab: TabInfo | None = None
        # The runner replaces this with the layout resolved from the header
        # row before any write happens.
        self.layout: C.Layout = C.DEFAULT_LAYOUT

    # -- tab resolution -----------------------------------------------------
    def tab(self) -> TabInfo:
        if self._tab is None:
            self._tab = self._resolve_tab()
        return self._tab

    def _resolve_tab(self) -> TabInfo:
        meta = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
            )
            .execute(num_retries=_RETRIES)
        )
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if int(props.get("sheetId", -1)) == self.gid:
                grid = props.get("gridProperties", {})
                return TabInfo(
                    title=str(props.get("title", "")),
                    sheet_id=self.gid,
                    column_count=int(grid.get("columnCount", 0)),
                    row_count=int(grid.get("rowCount", 0)),
                )
        raise BridgeError("tab_not_found", f"no tab with gid={self.gid}")

    def _range(self, a1: str) -> str:
        return f"{a1_sheet_prefix(self.tab().title)}!{a1}"

    def _cell_range(self, index: int, row_number: int) -> str:
        return self._range(f"{C.column_letter(index)}{row_number}")

    # -- reading --------------------------------------------------------------
    def read_grid(self) -> list[list]:
        """Every row of the tab (row 1 = header) in one call, sized to the tab.

        Trailing blank cells are trimmed by the API, so callers must index
        defensively (see rows.SheetRow).
        """
        last = max(self.tab().column_count, C.TOTAL_COLUMN_COUNT) - 1
        result = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(f"A1:{C.column_letter(last)}"),
                majorDimension="ROWS",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute(num_retries=_RETRIES)
        )
        return result.get("values", [])

    def read_row_identity(self, row_number: int) -> tuple[str, str, str]:
        """The identity cells of one row as they are *now* - (Drive Link,
        Lecture Number, Chapter), or (link, Lecture Topic, Chapter) on a
        catalog tab - one request, so a write can refuse a row that has moved."""
        layout = self.layout
        indexes = [layout.index(name) for name in layout.identity_columns]
        present = [i for i in indexes if i is not None]
        if not present:
            return ("", "", "")
        lo, hi = min(present), max(present)
        result = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(f"{C.column_letter(lo)}{row_number}:{C.column_letter(hi)}{row_number}"),
                majorDimension="ROWS",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute(num_retries=_RETRIES)
        )
        values = (result.get("values") or [[]])[0]

        def at(index):
            if index is None or not 0 <= index - lo < len(values):
                return ""
            value = values[index - lo]
            return ("" if value is None else str(value)).strip()

        return (at(indexes[0]), at(indexes[1]), at(indexes[2]))

    def probe_cell_kinds(self, row_number: int, last_index: int) -> dict[int, str]:
        """What each cell of one row holds (typed / formula / spilled / empty).

        One request; used on the first data row to tell a computed roll-up tab
        from a real one before anything is written.
        """
        a1 = f"A{row_number}:{C.column_letter(max(last_index, 0))}{row_number}"
        result = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                ranges=[self._range(a1)],
                includeGridData=True,
                fields="sheets(data(rowData(values(userEnteredValue,effectiveValue))))",
            )
            .execute(num_retries=_RETRIES)
        )
        for sheet in result.get("sheets", []):
            for data in sheet.get("data", []):
                for row in data.get("rowData", []):
                    return cell_kinds(row.get("values", []))
        return {}

    def cell_hyperlink(self, row_number: int, col_index: int) -> str:
        """URL behind a cell whose *value* is only display text.

        Covers plain hyperlinks, rich-text link runs and Drive smart chips.
        Returns "" when the cell carries no link. Used only as a fallback when
        the cell text itself is not a parseable Drive link.
        """
        result = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                ranges=[self._cell_range(col_index, row_number)],
                includeGridData=True,
                fields=(
                    "sheets(data(rowData(values("
                    "hyperlink,"
                    "textFormatRuns(format(link(uri))),"
                    "chipRuns(chip(richLinkProperties(uri)))"
                    "))))"
                ),
            )
            .execute(num_retries=_RETRIES)
        )
        for sheet in result.get("sheets", []):
            for data in sheet.get("data", []):
                for row in data.get("rowData", []):
                    for cell in row.get("values", []):
                        link = str(cell.get("hyperlink") or "").strip()
                        if link:
                            return link
                        for run in cell.get("textFormatRuns", []) or []:
                            uri = ((run.get("format") or {}).get("link") or {}).get("uri")
                            if uri:
                                return str(uri).strip()
                        for run in cell.get("chipRuns", []) or []:
                            uri = ((run.get("chip") or {}).get("richLinkProperties") or {}).get("uri")
                            if uri:
                                return str(uri).strip()
        return ""

    # -- control columns ------------------------------------------------------
    def _control_slots(self, layout: C.Layout) -> tuple[tuple[int, str], ...]:
        return tuple(zip(layout.control_indexes, layout.control_headers))

    def check_control_headers(self, header_row: list) -> tuple[bool, list[str]]:
        """Return (all_present, conflicting_headers).

        A control header cell that is blank is created; a cell holding some
        *other* text is a conflict (we never overwrite human data).
        """
        layout = C.Layout.from_header(header_row)
        conflicts: list[str] = []
        complete = True
        for (index, expected), name in zip(self._control_slots(layout), layout.control_names):
            raw = header_row[index] if index < len(header_row) else None
            actual = str(raw).strip() if raw is not None else ""
            if not actual:
                complete = False
            elif C.normalize_header(actual) == C.normalize_header(expected):
                continue
            elif layout.indexes.get(name) == index:
                # Our column under the name a team gave it ("Review Status (by
                # ARD Team)", or an older spelling): read and written as ours.
                continue
            else:
                conflicts.append(f"{C.column_letter(index)}1={actual!r}")
        return complete, conflicts

    def ensure_control_headers(self, header_row: list) -> bool:
        """Create the Auto Status / Auto Output Link headers if absent.
        Returns True if a write happened."""
        complete, conflicts = self.check_control_headers(header_row)
        if conflicts:
            raise BridgeError("control_header_conflict", ", ".join(conflicts))
        if complete:
            return False
        layout = C.Layout.from_header(header_row)
        self._ensure_column_capacity(max(layout.control_indexes) + 1)
        # Only blank header cells are written: a header a team renamed stays theirs.
        data = [
            {"range": self._cell_range(index, 1), "values": [[expected]]}
            for index, expected in self._control_slots(layout)
            if not str(header_row[index] if index < len(header_row) else "").strip()
        ]
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute(num_retries=_RETRIES)
        return True

    def _ensure_column_capacity(self, needed: int) -> None:
        tab = self.tab()
        if tab.column_count >= needed:
            return
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "appendDimension": {
                            "sheetId": tab.sheet_id,
                            "dimension": "COLUMNS",
                            "length": needed - tab.column_count,
                        }
                    }
                ]
            },
        ).execute(num_retries=_RETRIES)
        self._tab = TabInfo(tab.title, tab.sheet_id, needed, tab.row_count)

    def clear_legacy_columns(self, row_count: int) -> bool:
        """Remove the retired Auto Run ID / Error Code / Timestamp / Cost columns.

        Only when the header row still carries all four, side by side, so a
        sheet that never had them - or that someone repurposed - is left alone.
        """
        del row_count
        legacy = self.layout.legacy
        if len(legacy) != len(C.LEGACY_CONTROL_HEADERS):
            return False
        indexes = sorted(legacy.values())
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            return False
        first, last = C.column_letter(indexes[0]), C.column_letter(indexes[-1])
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=self._range(f"{first}1:{last}"),
            body={},
        ).execute(num_retries=_RETRIES)
        return True

    def ensure_review_validation(self, row_count: int) -> None:
        """Make the review column a Review/Done dropdown on the data rows.

        Best effort: a sheet that refuses the request still receives the plain
        "Review" text, so the run is never blocked by cosmetics.
        """
        tab = self.tab()
        index = self.layout.review_status_index
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"setDataValidation": {
                "range": {
                    "sheetId": tab.sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": max(int(row_count), 2),
                    "startColumnIndex": index,
                    "endColumnIndex": index + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": value} for value in C.REVIEW_OPTIONS],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }}]},
        ).execute(num_retries=_RETRIES)

    def write_folder_links(self, links: dict[int, str]) -> None:
        """Catalog tabs: fill only the Lecture Folder Link cell of these rows
        (row number -> link), in one request. Status and review stay as they are."""
        index = self.layout.notes_folder_index
        if index is None:
            raise ValueError("this tab has no Lecture Folder Link column")
        if any(number < 2 for number in links):
            raise ValueError("refusing to write into the header row")
        data = [{"range": self._cell_range(index, number), "values": [[guard_cell(link)]]}
                for number, link in sorted(links.items())]
        if not data:
            return
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute(num_retries=_RETRIES)

    def write_control(self, row_number: int, values: list, *, final_timestamp: str | None = None,
                      notes_link: str | None = None, folder_link: str | None = None) -> None:
        """One batched values update per state change: the two control cells,
        plus ``Final File Status`` = Done and its timestamp when the row is
        finished.

        USER_ENTERED so the review cell matches its dropdown values and the
        timestamp becomes a real date; strings are guarded against being read
        as formulas. The timestamp is skipped when the tab computes it itself.
        """
        if len(values) != C.CONTROL_COLUMN_COUNT:
            raise ValueError(f"control row must have exactly {C.CONTROL_COLUMN_COUNT} values")
        if row_number < 2:
            raise ValueError("refusing to write into the header row")
        layout = self.layout
        cells = [guard_cell(v) for v in values]
        status_index, review_index = layout.auto_status_index, layout.review_status_index
        if review_index == status_index + 1:
            first, last = C.column_letter(status_index), C.column_letter(review_index)
            data = [{"range": self._range(f"{first}{row_number}:{last}{row_number}"), "values": [cells]}]
        else:
            data = [
                {"range": self._cell_range(status_index, row_number), "values": [[cells[0]]]},
                {"range": self._cell_range(review_index, row_number), "values": [[cells[1]]]},
            ]
        if notes_link is not None and layout.notes_link_index is not None:
            # Catalog tabs: the Drive link to the notes filed for this row.
            data.append({"range": self._cell_range(layout.notes_link_index, row_number),
                         "values": [[guard_cell(notes_link)]]})
        if folder_link is not None and layout.notes_folder_index is not None:
            # ... and to the lecture's folder in the Drive tree.
            data.append({"range": self._cell_range(layout.notes_folder_index, row_number),
                         "values": [[guard_cell(folder_link)]]})
        if final_timestamp is not None:
            data.append({"range": self._cell_range(layout.require("final_status"), row_number), "values": [[C.DONE_TEXT]]})
            stamp_index = layout.index("final_timestamp")
            if stamp_index is not None and layout.write_final_timestamp:
                data.append({"range": self._cell_range(stamp_index, row_number), "values": [[guard_cell(final_timestamp)]]})
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute(num_retries=_RETRIES)
