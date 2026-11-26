"""A made Google Doc (B.Sc. style) as one PDF in Google Drive, its link in the sheet (26 Nov 2026).

- The PDF is the Doc's tabs exported one at a time and joined in order: exactly the pages the quality
  check saw and passed (a whole-Doc export breaks the pages differently). A Doc with several tabs gets
  a bookmark per tab.
- It goes into one Drive folder per sheet tab ("<tab> - PDFs (B.Sc. style)") of the signed-in account.
  Anyone at the account's domain can open it with the link, read only: the Doc stays the copy to edit.
- Each Doc is uploaded once: ~/.note_studio/gdoc/pdfs.json remembers its PDF, so asking again only
  writes the links still missing.
- The link goes into the row's "Concise Notes Link" cell (gdoc_sheet.write_pdf_link): never over
  something else, never into a row whose Doc link changed.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import gdoc_sheet, paths
from .typeset import gdoc_make, gdoc_source, gdoc_writer
from .typeset.gdoc_make import doc_id

FOLDER_MIME = "application/vnd.google-apps.folder"
PARALLEL = 3                      # PDFs made at the same time (Google limits exports per minute)
LINK_TRIES, LINK_PAUSE = 3, 3.0   # writing a row's link: tries, and seconds times the attempt between them
UPLOAD_RETRIES = 5                # a resumable upload carries on from where a dropped connection left it
_lock = threading.Lock()
log = logging.getLogger("note_studio.gdoc_pdf")


class PdfError(RuntimeError):
    """A plain-words problem with one PDF."""


# ------------------------------------------------------------------ record
def record_path() -> Path:
    return paths.home() / "gdoc" / "pdfs.json"


def _record() -> dict:
    try:
        found = json.loads(record_path().read_text("utf-8"))
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(key: str, value: dict) -> None:
    with _lock:
        found = _record()
        found[key] = value
        path = record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(found, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(path)


# ------------------------------------------------------------------ Drive
def pdf_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _alive(google, file_id: str) -> bool:
    """The file is still there and not in the bin."""
    try:
        meta = gdoc_writer._execute(google.drive.files().get(fileId=file_id, fields="id,trashed",
                                                             supportsAllDrives=True))
    except Exception as exc:  # noqa: BLE001 - only "not found" means gone
        if getattr(getattr(exc, "resp", None), "status", 0) == 404:
            return False
        raise
    return not meta.get("trashed")


def share_read(google, file_id: str, domain: str) -> str:
    """Anyone at `domain` can open it with the link; not searchable for the whole company."""
    existing = gdoc_writer._execute(google.drive.permissions().list(
        fileId=file_id, fields="permissions(type,domain,role)", supportsAllDrives=True)).get("permissions", [])
    if any(p.get("type") == "domain" and p.get("domain") == domain for p in existing):
        return "already shared"
    gdoc_writer._execute(google.drive.permissions().create(fileId=file_id, supportsAllDrives=True, fields="id", body={
        "type": "domain", "domain": domain, "role": "reader", "allowFileDiscovery": False}))
    return "now shared"


def folder_for(google, name: str, domain: str) -> str:
    """The Drive folder `name` in the account's My Drive: the remembered one, else one found by name,
    else a new one. Shared like its PDFs."""
    key = "folder:" + name
    known = (_record().get(key) or {}).get("id")
    if known and _alive(google, known):
        return known
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    found = gdoc_writer._execute(google.drive.files().list(
        q=f"name = '{quoted}' and mimeType = '{FOLDER_MIME}' and 'root' in parents and trashed = false",
        fields="files(id)", spaces="drive")).get("files") or []
    folder = found[0]["id"] if found else gdoc_writer._execute(google.drive.files().create(
        body={"name": name, "mimeType": FOLDER_MIME}, fields="id"), outages=False)["id"]
    if domain:
        share_read(google, folder, domain)
    _remember(key, {"id": folder})
    return folder


def upload(google, folder_id: str, name: str, data: bytes) -> str:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf", resumable=True,
                              chunksize=8 * 1024 * 1024)
    request = google.drive.files().create(body={"name": name, "parents": [folder_id], "mimeType": "application/pdf"},
                                          media_body=media, fields="id", supportsAllDrives=True)
    return request.execute(num_retries=UPLOAD_RETRIES)["id"]


# ------------------------------------------------------------------ one Doc
def doc_pdf(google, document_id: str, say: Callable[[str], None]) -> tuple[bytes, dict, int]:
    """(PDF, the Doc's Drive name and modifiedTime, pages): every tab in order, a bookmark per tab when
    there are several."""
    from pypdf import PdfReader, PdfWriter

    meta = gdoc_writer._execute(google.drive.files().get(fileId=document_id, fields="name,modifiedTime,trashed",
                                                         supportsAllDrives=True))
    if meta.get("trashed"):
        raise PdfError("The Google Doc is in the bin, so no PDF was made. Restore it, then try again.")
    document = gdoc_writer._get(google.docs, document_id, includeTabsContent=True,
                                fields="tabs(tabProperties,childTabs(tabProperties))")
    tabs = gdoc_source.all_tabs(document)
    if not tabs:
        raise PdfError("The Google Doc has no tabs, so no PDF was made.")
    writer, expected = PdfWriter(), 0
    for tab in tabs:
        reader = PdfReader(io.BytesIO(gdoc_make.export_tab(google, document_id, tab["tab_id"], say)))
        expected += len(reader.pages)
        writer.append(reader, outline_item=tab["title"] if len(tabs) > 1 else None)
    writer.add_metadata({"/Title": meta.get("name") or ""})
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    pages = len(PdfReader(io.BytesIO(data)).pages) if gdoc_make.complete_pdf(data) else 0
    if pages != expected:
        raise PdfError(f"The joined PDF has {pages} pages but its tabs have {expected}, so it was not saved. "
                       "Try again.")
    return data, meta, pages


def make_pdf(google, document_id: str, folder_id: str, domain: str, say: Callable[[str], None]) -> dict:
    """{file_id, url, pages, bytes, name, ...}: the Doc's PDF in the folder, uploaded once."""
    key = "doc:" + document_id
    known = _record().get(key)
    if known and known.get("folder") == folder_id and _alive(google, known["file_id"]):
        return dict(known, again=True)
    data, meta, pages = doc_pdf(google, document_id, say)
    name = (meta.get("name") or document_id) + ".pdf"
    file_id = upload(google, folder_id, name, data)
    if domain:
        share_read(google, file_id, domain)
    found = {"file_id": file_id, "url": pdf_url(file_id), "folder": folder_id, "pages": pages, "bytes": len(data),
             "name": name, "doc_modified": meta.get("modifiedTime", ""), "made_at": time.time()}
    _remember(key, found)
    return found


# ------------------------------------------------------------------ a sheet tab
def folder_name(tab: str) -> str:
    return f"{tab} - PDFs (B.Sc. style)"


def pending(google, spreadsheet_id: str, tab: str, rows: set[int] | None = None) -> dict:
    """{"ready": rows with a made Doc and an empty Concise Notes Link, "done": rows that have both,
    "missing_column": no Concise Notes Link column}."""
    found = gdoc_sheet.read(google.sheets, spreadsheet_id, tab)
    chosen = [r for r in found["rows"] if doc_id(r["link"]) and (rows is None or r["row"] in rows)]
    return {"ready": [dict(r, made_id=doc_id(r["link"])) for r in chosen if not r["pdf"]],
            "done": [r for r in chosen if r["pdf"]], "missing_column": found["pdf_column"] is None}


def sheet_pdfs(google, link: str, domain: str, rows_spec: str = "", say: Callable[[str], None] = print,
               workers: int = PARALLEL, stop: Callable[[], bool] = lambda: False) -> list[dict]:
    """A PDF and its link for every chosen row of the tab that has a made Doc and no Concise Notes Link yet.
    Returns one {row, topic, state (done/failed/stopped), message, url} per row; one row's trouble never
    stops the others."""
    spreadsheet_id, gid = gdoc_sheet.parse_link(link)
    tab, _ = gdoc_sheet.tab_title(google.sheets, spreadsheet_id, gid)
    todo = pending(google, spreadsheet_id, tab, gdoc_sheet.parse_rows(rows_spec))
    if todo["missing_column"]:
        raise gdoc_sheet.SheetError(f"Tab “{tab}” has no Concise Notes Link column, so PDF links have nowhere to go.")
    folder = folder_for(google, folder_name(tab), domain)
    say(f"Tab “{tab}”: {len(todo['ready'])} rows need a PDF ({len(todo['done'])} already have a link)")

    def one(row: dict) -> dict:
        out = {"row": row["row"], "topic": row["topic"], "made_id": row["made_id"]}
        if stop():
            return dict(out, state="stopped", message="Stopped before this row.")
        try:
            made = make_pdf(google, row["made_id"], folder, domain, lambda words: say(f"Row {row['row']}: {words}"))
            wrote = gdoc_sheet.asked_again(lambda: gdoc_sheet.write_pdf_link(
                google.sheets, spreadsheet_id, tab, row["row"], row["made_id"], made["url"]), LINK_TRIES, LINK_PAUSE)
        except Exception as exc:  # noqa: BLE001 - one row's trouble is reported, the others carry on
            log.exception("PDF for row %s (Doc %s)", row["row"], row["made_id"])
            words = str(exc) if isinstance(exc, (PdfError, gdoc_make.GdocError, gdoc_sheet.SheetError)) else \
                f"Google did not finish this PDF ({type(exc).__name__}). Try again."
            return dict(out, state="failed", message=words)
        ok = wrote.startswith("written") or wrote == "already there"
        say(f"Row {row['row']}: {made['pages']} pages, {made['bytes'] // 1024:,} KB; link {wrote}")
        return dict(out, state="done" if ok else "failed", url=made["url"], pages=made["pages"],
                    message="" if ok else f"The PDF is made ({made['url']}) but its link was {wrote}.")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, todo["ready"]))
