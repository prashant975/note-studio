"""Catalog planning must resolve a PDF chip like its underlying Drive URL."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from note_studio import automation_links

HEADERS = ["Exam", "Batch", "subject", "faculty_id", "faculty_name", "faculty_email",
           "chapter", "Lecture_Date", "lecture_topic", "downloadable_link"]
FILE_ID = "1LecturePdf0123456789"
DRIVE_URL = f"https://drive.google.com/open?id={FILE_ID}&usp=drive_copy"


class CellService:
    """Only Sheets metadata reads are available; a write would fail the test."""

    def __init__(self, cell):
        self.cell = cell
        self.reads = []

    def spreadsheets(self):
        return self

    def get(self, **kwargs):
        self.reads.append(kwargs)
        return self

    def execute(self, **kwargs):
        if isinstance(self.cell, Exception):
            raise self.cell
        return {"sheets": [{"data": [{"rowData": [{"values": [self.cell]}]}]}]}


@pytest.fixture
def catalog(client):
    from sheets_bridge.catalog_flow import CatalogFlow
    from sheets_bridge.columns import Layout
    from sheets_bridge.drive import DriveFile
    from sheets_bridge.rows import SheetRow
    from sheets_bridge.runner import RunOptions
    from sheets_bridge.sheets import SheetClient, TabInfo

    source = DriveFile(FILE_ID, "Partnership.pdf", "application/pdf", 100)

    def make(cell):
        service = CellService(cell)
        sheet = SheetClient(service, "spreadsheet", 0)
        sheet._tab = TabInfo("Notes", 0, 29, 1478)
        calls = []

        def get_file(file_id):
            calls.append(file_id)
            assert file_id == FILE_ID
            return source

        runner = SimpleNamespace(options=RunOptions(), sheet=sheet,
                                 drive=SimpleNamespace(get_file=get_file))
        row = SheetRow.from_values(2, ["Gujarat Board", "Gujarat Board", "Accountancy", "", "", "",
                                      "Partnership", "", "Partnership", "01_Partnership.pdf"],
                                   Layout.from_header(HEADERS))
        return CatalogFlow(runner, None, {}), row, service, calls

    return make


@pytest.mark.parametrize("cell", [
    {"chipRuns": [{"chip": {"richLinkProperties": {"uri": DRIVE_URL}}}]},
    {"hyperlink": DRIVE_URL},
    {"textFormatRuns": [{"format": {"link": {"uri": DRIVE_URL}}}]},
])
def test_pdf_attachments_use_the_same_source_and_cache_identity_as_plain_urls(catalog, cell):
    flow, row, service, calls = catalog(cell)
    attached = flow.plan(row)
    direct_values = list(row.values)
    direct_values[9] = DRIVE_URL
    direct = flow.plan(replace(row, values=tuple(direct_values)))

    assert attached == direct
    assert attached.drive_file.id == FILE_ID and attached.url == DRIVE_URL
    assert calls == [FILE_ID, FILE_ID]
    assert row.drive_link == "01_Partnership.pdf"
    assert len(service.reads) == 1 and service.reads[0]["ranges"] == ["'Notes'!J2"]
    assert "chipRuns" in service.reads[0]["fields"]


def test_a_linked_web_pdf_keeps_the_original_download_and_cache_path(catalog):
    url = "https://static.pw.live/lecture.pdf"
    flow, row, service, calls = catalog({"hyperlink": url})
    attached = flow.plan(row)
    values = list(row.values)
    values[9] = url
    direct = flow.plan(replace(row, values=tuple(values)))
    assert attached == direct and attached.drive_file is None
    assert not calls and len(service.reads) == 1


@pytest.mark.parametrize("cell", [{}, {"hyperlink": "not a URL"}])
def test_a_filename_without_a_valid_link_still_fails_before_drive_access(catalog, cell):
    from sheets_bridge.errors import BridgeError

    flow, row, service, calls = catalog(cell)
    with pytest.raises(BridgeError) as problem:
        flow.plan(row)
    assert problem.value.code == "invalid_link"
    assert not calls and len(service.reads) == 1


def test_link_metadata_errors_are_reported_without_guessing_a_source(catalog):
    failure = RuntimeError("Google did not answer")
    flow, row, _, calls = catalog(failure)
    with pytest.raises(RuntimeError) as problem:
        flow.plan(row)
    assert problem.value is failure and not calls


def test_preparing_the_app_again_does_not_stack_link_readers(catalog):
    from sheets_bridge.catalog_flow import CatalogFlow

    installed = CatalogFlow.plan
    automation_links.install()
    assert CatalogFlow.plan is installed
    flow, row, service, _ = catalog({"hyperlink": DRIVE_URL})
    flow.plan(row)
    assert len(service.reads) == 1


def test_gujarati_catalog_chips_keep_their_chapter_and_use_the_gujarati_cache_key(catalog):
    flow, row, _, _ = catalog({"hyperlink": DRIVE_URL})
    values = list(row.values)
    values[6] = "ભાગીદારી"
    values[8] = "ભાગીદારી 1: વિષય પ્રવેશ"
    row = replace(row, values=tuple(values))
    gujarati = flow.plan(row)
    assert gujarati.language == "gujarati" and gujarati.header.chapter == "ભાગીદારી"
    assert gujarati.header.subject == "હિસાબશાસ્ત્ર"
    values[1] = "English batch"
    english_row = replace(row, values=tuple(values))
    english = flow.plan(english_row)
    assert gujarati.render_key != english.render_key
    assert flow.plan(english_row).language == "english"
    values[1] = "Gujarati batch"
    assert "English" not in flow.plan(replace(row, values=tuple(values))).note
