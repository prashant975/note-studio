"""One summary row per **run** in the PW platform workbook.

The proxy already writes a raw row per provider call into ``Usage Cost 2.0``
(that is server-side and cannot be turned off from here - the kit's
``UsageSession.flush`` is a documented no-op). Those rows answer "what did each
model call cost"; nobody can read "what did one press of the button cost" off
them without grouping by hand.

So a finished run appends exactly one row of its own, to a tab of its own:

    Run ID | Finished At | Operator | Sheets | Lectures Done | Failed | Total Cost (INR)

``Usage Cost 2.0`` is machine-synced by another system and is never touched.
Every call of one run also shares a single ``task_id``, so the raw rows group
into one task per run - the way the kit itself says to combine them.
"""
from __future__ import annotations

import os
from datetime import datetime

# The PW platform workbook: 'App Allowed Emails', which also holds the
# Whitelisted allowlist and the synced Usage Cost tabs.
DEFAULT_SHEET_ID = "1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE"
DEFAULT_TAB_TITLE = "Handwritten Notes Runs"

HEADERS = (
    "Run ID",
    "Finished At",
    "Operator",
    "Sheets",
    "Lectures Done",
    "Failed",
    "Total Cost (INR)",
    # Last on purpose: a tab written by an older version keeps its rows aligned
    # and simply has this cell empty.
    "Status",
)


def sheet_id() -> str:
    return (os.environ.get("SHEETS_BRIDGE_LOG_SHEET", "").strip() or DEFAULT_SHEET_ID)


def tab_title() -> str:
    return (os.environ.get("SHEETS_BRIDGE_LOG_TAB", "").strip() or DEFAULT_TAB_TITLE)


def enabled() -> bool:
    """``SHEETS_BRIDGE_LOG_SHEET=off`` turns the summary row off entirely."""
    return sheet_id().lower() not in ("off", "none", "0", "false")


TIMESTAMP_COLUMN = 1          # B, "Finished At"
TIMESTAMP_PATTERN = "yyyy-mm-dd h:mm:ss"


def _a1(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def ensure_tab(service, spreadsheet_id: str, title: str) -> bool:
    """Create the log tab with its header row when missing. True if created."""
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute()
    )
    for sheet in meta.get("sheets", []) or []:
        props = sheet.get("properties") or {}
        if str(props.get("title", "")) == title:
            _upgrade_header(service, spreadsheet_id, title)
            _format_timestamps(service, spreadsheet_id, props.get("sheetId"))
            return False
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # Another run created it between the check and the request.
        if "already exists" in str(exc).lower():
            return False
        raise
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_a1(title)}!A1",
        valueInputOption="RAW",
        body={"values": [list(HEADERS)]},
    ).execute()
    return True


def _format_timestamps(service, spreadsheet_id: str, gid) -> None:
    """Keep "Finished At" readable.

    USER_ENTERED makes Sheets store the stamp as a real datetime - correct for
    sorting, but a cell with no date format renders it as its serial number
    (``46260.72166``). Formatting the whole column is idempotent and repairs a
    row that landed without inheriting a format.
    """
    if gid is None:
        return
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"repeatCell": {
                "range": {
                    "sheetId": int(gid),
                    "startRowIndex": 1,
                    "startColumnIndex": TIMESTAMP_COLUMN,
                    "endColumnIndex": TIMESTAMP_COLUMN + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {
                    "type": "DATE_TIME", "pattern": TIMESTAMP_PATTERN,
                }}},
                "fields": "userEnteredFormat.numberFormat",
            }}]},
        ).execute()
    except Exception:  # noqa: BLE001 - cosmetic; never block the log
        pass


def _next_row(service, spreadsheet_id: str, title: str) -> int:
    """First free row, counted rather than left to append()'s table detection.

    A blank row anywhere above the end splits the "table", and append() then
    writes *into that gap* instead of at the bottom - which once put a newer
    run above an older one.
    """
    got = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{_a1(title)}!A:A")
        .execute()
    )
    return len(got.get("values") or []) + 1


def _upgrade_header(service, spreadsheet_id: str, title: str) -> None:
    """Widen a header row written before a column was added."""
    try:
        got = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{_a1(title)}!1:1")
            .execute()
        )
        header = (got.get("values") or [[]])[0]
        if len(header) >= len(HEADERS):
            return
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{_a1(title)}!A1",
            valueInputOption="RAW",
            body={"values": [list(HEADERS)]},
        ).execute()
    except Exception:  # noqa: BLE001 - a stale header never blocks logging
        pass


def append_run(
    service,
    *,
    run_id: str,
    operator: str,
    sheets: str,
    lectures_done: int,
    failed: int,
    total_cost_inr: float,
    status: str = "completed",
    finished_at: str | None = None,
    spreadsheet_id: str | None = None,
    title: str | None = None,
) -> str:
    """Append one row for a whole run. Callers wrap this: a logging failure
    must never turn a finished run into a failed one."""
    target = spreadsheet_id or sheet_id()
    tab = title or tab_title()
    ensure_tab(service, target, tab)
    row = [
        run_id,
        finished_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        operator or "",
        sheets or "",
        int(lectures_done),
        int(failed),
        round(float(total_cost_inr), 2),
        status,
    ]
    service.spreadsheets().values().update(
        spreadsheetId=target,
        range=f"{_a1(tab)}!A{_next_row(service, target, tab)}",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()
    return tab
