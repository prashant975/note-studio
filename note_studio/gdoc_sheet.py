"""A sheet tab whose rows link final-notes Google Docs (the "Notes Testing" layout, 16-24 Nov 2026).

Each row's lecture link column (downloadable_link: a hyperlink, a linked text run, a Docs smart chip, or the
link as plain text) names its source Google Doc. A made Doc's link goes into the column
"Google Doc (B.Sc. style)" of that row:

- it is never written over a cell holding something else, nor into a row whose source link changed;
- a missing column is added right after the last header in use, and only if the copied sheet automation
  (sheets_bridge.columns.Layout, read only) reads the tab exactly as before.

The made Doc's PDF (gdoc_pdf.py) goes into the tab's own "Concise Notes Link" column, under the same rules.
"""
from __future__ import annotations

import re

from .typeset.gdoc_make import doc_id

HEADER = "Google Doc (B.Sc. style)"
_SHEET = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_GID = re.compile(r"[#&?]gid=(\d+)")
_SAME_READING = ("indexes", "legacy", "control_start", "ambiguous", "kind", "write_final_timestamp")
MAX_ROWS = 5000


class SheetError(ValueError):
    """A plain-words problem with the sheet or its link."""


def parse_link(link: str) -> tuple[str, int | None]:
    """(spreadsheet id, tab gid or None) from a Google Sheets link."""
    text = str(link or "").strip()
    if not text:
        raise SheetError("Paste the link of the Google Sheet.")
    found = _SHEET.search(text)
    if not found:
        raise SheetError("That is not a Google Sheets link. Copy the link from the sheet's address bar.")
    gid = _GID.search(text)
    return found.group(1), (int(gid.group(1)) if gid else None)


def parse_rows(spec: str) -> set[int] | None:
    """'16-21, 25' -> {16, ..., 21, 25}; '' -> None (every row)."""
    text = str(spec or "").strip()
    if not text:
        return None
    rows: set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise SheetError("Write rows like 16-21 or 16, 18, 20.")
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if first < 2 or last < first:
            raise SheetError("Rows start at 2 (row 1 is the header), and a range goes from low to high.")
        if last - first > MAX_ROWS:
            raise SheetError(f"Choose at most {MAX_ROWS} rows at a time.")
        rows.update(range(first, last + 1))
    return rows


def letter(index: int) -> str:
    out, index = "", index + 1
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(65 + rest) + out
    return out


def _layout(header: list[str]):
    from . import engine_catalog

    engine_catalog.put_shortnotes_on_path()
    from sheets_bridge.columns import Layout

    return Layout.from_header(header)


def same_reading(before: list[str], after: list[str]) -> bool:
    """Whether the copied sheet automation reads both headers the same way."""
    a, b = _layout(before), _layout(after)
    return all(getattr(a, k, None) == getattr(b, k, None) for k in _SAME_READING)


def tab_title(sheets, spreadsheet_id: str, gid: int | None) -> tuple[str, int]:
    """(tab title, gid): the tab the link names, else the first tab."""
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id,
                                     fields="sheets(properties(title,sheetId,index))").execute()
    tabs = [s["properties"] for s in meta.get("sheets") or []]
    if not tabs:
        raise SheetError("That sheet has no tabs.")
    chosen = next((t for t in tabs if t.get("sheetId") == gid), None) if gid is not None else None
    if gid is not None and chosen is None:
        raise SheetError("The tab in that link was not found. Copy the link again from the tab you want.")
    chosen = chosen or min(tabs, key=lambda t: t.get("index", 0))
    return chosen["title"], int(chosen.get("sheetId", 0))


def _links(cell: dict) -> list[str]:
    return ([cell.get("hyperlink") or ""] +
            [((r.get("chip") or {}).get("richLinkProperties") or {}).get("uri", "") for r in cell.get("chipRuns") or []] +
            [((r.get("format") or {}).get("link") or {}).get("uri", "") for r in cell.get("textFormatRuns") or []] +
            [cell.get("formattedValue") or ""])


def read(sheets, spreadsheet_id: str, tab: str) -> dict:
    """{"header", "column" (index of HEADER or None), "link_column", "rows": [{row, subject, topic,
    chapter, source_id, link}]} for every row whose lecture link is a Google Doc."""
    grid = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, ranges=[f"'{tab}'"], includeGridData=True,
                                     fields="sheets(data(rowData(values(formattedValue,hyperlink,chipRuns,"
                                            "textFormatRuns))))").execute()
    data = ((grid.get("sheets") or [{}])[0].get("data") or [{}])[0].get("rowData") or []
    if not data:
        raise SheetError("That tab is empty.")
    header = [(c.get("formattedValue") or "") for c in data[0].get("values") or []]
    layout = _layout(header)
    link_column, pdf_column = layout.index("drive_link"), layout.index("notes_link")
    if link_column is None:
        raise SheetError("That tab has no lecture link column (downloadable_link).")
    names = {name.strip().lower(): i for i, name in enumerate(header)}
    column = header.index(HEADER) if HEADER in header else None
    found = []
    for n, row in enumerate(data[1:], start=2):
        cells = row.get("values") or []

        def text(name):
            i = names.get(name)
            return (cells[i].get("formattedValue") or "").strip() if i is not None and i < len(cells) else ""
        cell = cells[link_column] if link_column < len(cells) else {}
        source_id = next((doc_id(u) for u in _links(cell) if doc_id(u)), "")
        if not source_id:
            continue
        done = (cells[column].get("formattedValue") or "").strip() if column is not None and column < len(cells) else ""
        pdf = (cells[pdf_column].get("formattedValue") or "").strip() \
            if pdf_column is not None and pdf_column < len(cells) else ""
        found.append({"row": n, "subject": text("subject"), "topic": text("lecture_topic"), "chapter": text("chapter"),
                      "source_id": source_id, "link": done, "pdf": pdf})
    return {"header": header, "column": column, "link_column": link_column, "pdf_column": pdf_column, "rows": found}


def write_link(sheets, spreadsheet_id: str, tab: str, row_number: int, source_id: str, url: str) -> str:
    """The made Doc's link into its row. Returns what happened, in plain words."""
    current = read(sheets, spreadsheet_id, tab)
    row = next((r for r in current["rows"] if r["row"] == row_number), None)
    if row is None or row["source_id"] != source_id:
        return "not written: the row's Google Doc link changed"
    header, column = current["header"], current["column"]
    data = []
    if column is None:
        used = len(header)
        while used and not header[used - 1].strip():
            used -= 1
        column = used
        new_header = list(header[:used]) + [HEADER]
        if not same_reading(header, new_header):
            return "not written: a new column would change how the sheet automation reads this tab"
        data.append({"range": f"'{tab}'!{letter(column)}1", "values": [[HEADER]]})
    if row["link"] == url:
        return "already there"
    if row["link"]:
        return f"not written: {letter(column)}{row_number} holds something else"
    data.append({"range": f"'{tab}'!{letter(column)}{row_number}", "values": [[url]]})
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body={
        "valueInputOption": "USER_ENTERED", "data": data}).execute()
    return f"written to {letter(column)}{row_number}"


def asked_again(call, tries: int, pause: float):
    """call(), asked again after a dropped connection or a busy Google (408, 429, 5xx): `tries` in all,
    `pause` times the attempt between them. A SheetError, or a refusal that asking again cannot change, is not."""
    import time

    for attempt in range(1, tries + 1):
        try:
            return call()
        except SheetError:
            raise
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "resp", None), "status", None)
            if attempt == tries or (status is not None and int(status) not in (408, 429, 500, 502, 503, 504)):
                raise
            time.sleep(pause * attempt)
    raise AssertionError("unreachable")


def write_pdf_link(sheets, spreadsheet_id: str, tab: str, row_number: int, made_id: str, url: str) -> str:
    """A made Doc's PDF link into its row's "Concise Notes Link" cell: only while that row's column
    "Google Doc (B.Sc. style)" still links the same Doc, only into an empty cell, and never by adding a
    column (the sheet automation's own column must already be there)."""
    current = read(sheets, spreadsheet_id, tab)
    column = current["pdf_column"]
    if column is None:
        return "not written: the tab has no Concise Notes Link column"
    row = next((r for r in current["rows"] if r["row"] == row_number), None)
    if row is None or doc_id(row["link"]) != made_id:
        return "not written: the row's Google Doc (B.Sc. style) link changed"
    if row["pdf"] == url:
        return "already there"
    if row["pdf"]:
        return f"not written: {letter(column)}{row_number} holds something else"
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body={
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"'{tab}'!{letter(column)}{row_number}", "values": [[url]]}]}).execute()
    return f"written to {letter(column)}{row_number}"
