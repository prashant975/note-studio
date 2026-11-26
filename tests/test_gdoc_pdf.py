"""PDFs of the made Google Docs (B.Sc. style), their links in the sheet's Concise Notes Link column
(gdoc_pdf.py, 26 Nov 2026), with Google replaced by fakes."""
from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter

from note_studio import gdoc_pdf, gdoc_service, gdoc_sheet
from note_studio.typeset import gdoc_make
from tests.test_gdoc_app import DOC_A, DOC_B, DOC_C, HEADER, SHEET, FakeSheets, hyperlink, row

MADE_A = "1MADEtestDocAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MADE_B = "1MADEtestDocBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
MADE_C = "1MADEtestDocCccccccccccccccccccccccccccccccc"
DOMAIN = "example.org"
LINK = f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=689523849"


def pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=596, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=0):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class NotFound(Exception):
    resp = SimpleNamespace(status=404)


class FakeFiles:
    def __init__(self, drive):
        self.drive = drive

    def get(self, fileId, fields="", supportsAllDrives=True):
        found = self.drive.store.get(fileId)
        return _Call(dict(found, id=fileId) if found else NotFound(fileId))

    def list(self, q, fields="", spaces="drive"):
        name = re.search(r"name = '(.+?)' and", q).group(1)
        return _Call({"files": [{"id": i} for i, f in self.drive.store.items()
                                if f["name"] == name and f.get("mimeType") == gdoc_pdf.FOLDER_MIME and not f["trashed"]]})

    def create(self, body, fields="id", media_body=None, supportsAllDrives=True):
        file_id = f"1FILE{len(self.drive.store):02d}" + "x" * 30
        data = media_body.getbytes(0, media_body.size()) if media_body is not None else None
        self.drive.store[file_id] = dict(body, trashed=False, data=data)
        if data is not None:
            self.drive.uploads.append(body["name"])
        return _Call({"id": file_id})


class FakePermissions:
    def __init__(self, drive):
        self.drive = drive

    def list(self, fileId, fields="", supportsAllDrives=True):
        return _Call({"permissions": self.drive.perms.get(fileId, [])})

    def create(self, fileId, body, fields="id", supportsAllDrives=True):
        self.drive.perms.setdefault(fileId, []).append(body)
        return _Call({"id": "perm"})


class FakeDrive:
    def __init__(self, docs: dict):
        self.store = {doc: {"name": f"{title} - Nursing Notes (B.Sc. style)", "modifiedTime": "2026-11-26T07:00:00Z",
                            "mimeType": "application/vnd.google-apps.document", "trashed": False}
                      for doc, (title, _tabs) in docs.items()}
        self.perms, self.uploads = {}, []

    def files(self):
        return FakeFiles(self)

    def permissions(self):
        return FakePermissions(self)


class FakeDocs:
    def __init__(self, docs: dict):
        self.docs = docs

    def documents(self):
        return self

    def get(self, documentId, **fields):
        tabs = self.docs[documentId][1]
        return _Call({"tabs": [{"tabProperties": {"tabId": tab, "title": title}} for tab, title, _ in tabs]})


class FakeGoogle:
    """Made Docs as {id: (title, [(tab id, tab title, pages)])}; a tab's export is a blank PDF of that size."""

    def __init__(self, docs: dict, sheets: FakeSheets):
        self.drive, self.docs, self.sheets = FakeDrive(docs), FakeDocs(docs), sheets
        self.exports = []
        self.authorized = self

    def get(self, url, timeout=None):
        document_id, tab = re.search(r"id=([^&]+)&tab=(.+)$", url).groups()
        self.exports.append((document_id, tab))
        pages = next(p for t, _, p in self.docs.docs[document_id][1] if t == tab)
        return SimpleNamespace(status_code=200, content=pdf_bytes(pages))


def cells(made: str = "", pdf: str = "") -> list:
    """Cells K..P of a row: Concise Notes Link is M, Google Doc (B.Sc. style) is P."""
    return [{}, {}, {"formattedValue": pdf} if pdf else {}, {}, {},
            {"formattedValue": gdoc_make.doc_url(made)} if made else {}]


def sheet(*rows) -> FakeSheets:
    return FakeSheets(HEADER + [gdoc_sheet.HEADER], list(rows))


DOCS = {MADE_A: ("Clinical Enzymology", [("t.a1", "Enzymes", 2), ("t.a2", "Clinical Use", 3)]),
        MADE_B: ("Introduction to Nutrition", [("t.b1", "Tab 1", 4)]),
        MADE_C: ("Vitamins", [("t.c1", "Tab 1", 1)])}


# ------------------------------------------------------------------ the sheet
def test_a_pdf_link_goes_only_into_an_empty_concise_notes_cell_of_a_row_that_still_links_the_doc():
    sheets = sheet(row("A", hyperlink(DOC_A), extra=cells(MADE_A)), row("B", hyperlink(DOC_B), extra=cells(MADE_B, "kept")))
    url = gdoc_pdf.pdf_url("1PDF" + "a" * 30)
    assert gdoc_sheet.write_pdf_link(sheets, SHEET, sheets.tab, 2, MADE_A, url) == "written to M2"
    assert gdoc_sheet.write_pdf_link(sheets, SHEET, sheets.tab, 2, MADE_A, url) == "already there"
    assert gdoc_sheet.write_pdf_link(sheets, SHEET, sheets.tab, 3, MADE_B, url) == "not written: M3 holds something else"
    assert gdoc_sheet.write_pdf_link(sheets, SHEET, sheets.tab, 2, MADE_B, url).startswith("not written: the row's")
    assert sheets.writes == [("'Bsc Nursing Notes'!M2", url)]
    bare = FakeSheets([h for h in HEADER if h != "Concise Notes Link"] + [gdoc_sheet.HEADER],
                      [row("A", hyperlink(DOC_A), extra=[{}] * 4 + [{"formattedValue": gdoc_make.doc_url(MADE_A)}])])
    assert gdoc_sheet.write_pdf_link(bare, SHEET, bare.tab, 2, MADE_A, url) == \
        "not written: the tab has no Concise Notes Link column"
    assert bare.writes == []


# ------------------------------------------------------------------ one Doc
def test_the_pdf_is_every_tab_in_order_with_a_bookmark_for_each():
    google = FakeGoogle(DOCS, sheet())
    data, meta, pages = gdoc_pdf.doc_pdf(google, MADE_A, lambda words: None)
    reader = PdfReader(io.BytesIO(data))
    assert pages == len(reader.pages) == 5 and google.exports == [(MADE_A, "t.a1"), (MADE_A, "t.a2")]
    assert [item.title for item in reader.outline] == ["Enzymes", "Clinical Use"]
    assert [reader.get_destination_page_number(item) for item in reader.outline] == [0, 2]
    assert reader.metadata.title == meta["name"] == "Clinical Enzymology - Nursing Notes (B.Sc. style)"
    single, _, _ = gdoc_pdf.doc_pdf(google, MADE_B, lambda words: None)
    assert PdfReader(io.BytesIO(single)).outline == []


# ------------------------------------------------------------------ a sheet tab
def test_a_sheet_gets_each_pdf_once_and_links_only_where_the_cell_is_empty():
    sheets = sheet(row("A", hyperlink(DOC_A), extra=cells(MADE_A)), row("B", hyperlink(DOC_B), extra=cells(MADE_B)),
                   row("No Doc yet", hyperlink(DOC_C)), row("C", hyperlink(DOC_C), extra=cells(MADE_C, "already linked")))
    google = FakeGoogle(DOCS, sheets)
    done = gdoc_pdf.sheet_pdfs(google, LINK, DOMAIN, say=lambda words: None)
    assert [(r["row"], r["state"]) for r in done] == [(2, "done"), (3, "done")]
    assert sorted(w[0] for w in sheets.writes) == ["'Bsc Nursing Notes'!M2", "'Bsc Nursing Notes'!M3"]   # made in parallel
    folder = next(i for i, f in google.drive.store.items() if f.get("mimeType") == gdoc_pdf.FOLDER_MIME)
    assert google.drive.store[folder]["name"] == "Bsc Nursing Notes - PDFs (B.Sc. style)"
    pdfs = {i: f for i, f in google.drive.store.items() if f.get("mimeType") == "application/pdf"}
    assert sorted(f["name"] for f in pdfs.values()) == ["Clinical Enzymology - Nursing Notes (B.Sc. style).pdf",
                                                         "Introduction to Nutrition - Nursing Notes (B.Sc. style).pdf"]
    assert all(f["parents"] == [folder] for f in pdfs.values())
    for file_id in [folder, *pdfs]:
        assert google.drive.perms[file_id] == [{"type": "domain", "domain": DOMAIN, "role": "reader",
                                                "allowFileDiscovery": False}]
    assert {r["url"] for r in done} == {gdoc_pdf.pdf_url(i) for i in pdfs}
    assert set(json.loads(gdoc_pdf.record_path().read_text("utf-8"))) == {
        f"doc:{MADE_A}", f"doc:{MADE_B}", "folder:Bsc Nursing Notes - PDFs (B.Sc. style)"}

    # Asked again: nothing left to do, nothing uploaded again.
    assert gdoc_pdf.sheet_pdfs(google, LINK, DOMAIN, say=lambda words: None) == []
    # A link someone cleared is written again with the same PDF; a PDF put in the bin is made again.
    sheets.grid[2][12] = {}
    sheets.grid[1][12] = {}
    google.drive.store[next(i for i, f in pdfs.items() if f["name"].startswith("Clinical"))]["trashed"] = True
    again = gdoc_pdf.sheet_pdfs(google, LINK, DOMAIN, say=lambda words: None)
    assert [(r["row"], r["state"]) for r in again] == [(2, "done"), (3, "done")]
    assert sorted(google.drive.uploads[:2]) == ["Clinical Enzymology - Nursing Notes (B.Sc. style).pdf",
                                                "Introduction to Nutrition - Nursing Notes (B.Sc. style).pdf"]
    assert google.drive.uploads[2:] == ["Clinical Enzymology - Nursing Notes (B.Sc. style).pdf"]
    assert again[1]["url"] == done[1]["url"] and again[0]["url"] != done[0]["url"]


def test_one_rows_trouble_is_reported_in_plain_words_and_the_others_carry_on():
    sheets = sheet(row("A", hyperlink(DOC_A), extra=cells(MADE_A)), row("B", hyperlink(DOC_B), extra=cells(MADE_B)))
    google = FakeGoogle(DOCS, sheets)
    google.drive.store[MADE_A]["trashed"] = True
    done = gdoc_pdf.sheet_pdfs(google, LINK, DOMAIN, say=lambda words: None)
    assert [(r["row"], r["state"]) for r in done] == [(2, "failed"), (3, "done")]
    assert "in the bin" in done[0]["message"] and sheets.writes == [("'Bsc Nursing Notes'!M3", done[1]["url"])]


def test_a_tab_without_a_concise_notes_link_column_is_refused_before_anything_is_made():
    bare = FakeSheets([h for h in HEADER if h != "Concise Notes Link"] + [gdoc_sheet.HEADER],
                      [row("A", hyperlink(DOC_A), extra=[{}] * 4 + [{"formattedValue": gdoc_make.doc_url(MADE_A)}])])
    google = FakeGoogle(DOCS, bare)
    try:
        gdoc_pdf.sheet_pdfs(google, LINK, DOMAIN, say=lambda words: None)
    except gdoc_sheet.SheetError as exc:
        assert "no Concise Notes Link column" in str(exc)
    else:
        raise AssertionError("expected a SheetError")
    assert google.drive.uploads == [] and google.exports == []


def test_writing_a_made_docs_link_still_asks_again_through_the_shared_helper(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionResetError(54, "Connection reset by peer")
        return "written to M2"
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    assert gdoc_sheet.asked_again(flaky, gdoc_service.LINK_TRIES, 0) == "written to M2" and len(calls) == 3
    refused = SimpleNamespace(resp=SimpleNamespace(status=403))
    error = type("Refused", (Exception,), {"resp": refused.resp})
    calls.clear()

    def denied():
        calls.append(1)
        raise error()
    try:
        gdoc_sheet.asked_again(denied, 3, 0)
    except error:
        assert len(calls) == 1
