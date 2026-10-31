"""Catalog sheets: exam / batch / subject / chapter / topic / PDF-link rows,
notes filed in a Drive tree and linked back into the sheet."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace

import pytest

from sheets_bridge import catalog as K
from sheets_bridge import columns as C
from sheets_bridge import http_source
from sheets_bridge.catalog_flow import RENDER_PROPERTY, SOURCE_PROPERTY
from sheets_bridge.destination import FolderTree
from sheets_bridge.drive import DriveFile
from sheets_bridge.errors import BridgeError
from sheets_bridge.generator import GenerationResult
from sheets_bridge.rows import SheetRow, is_eligible
from sheets_bridge.runner import Runner, RunOptions

HEADER = ["exam", "batch", "subject", "chapter", "Lecture_Date", "lecture_topic", "downloadable_link"]
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_ID = "1RootNotesFolderxxxxx"
URL1 = "https://static.pw.live/5eb393ee95fab7468a79d189/ADMIN/aaaa-1111.pdf"
URL2 = "https://static.pw.live/5eb393ee95fab7468a79d189/ADMIN/bbbb-2222.pdf"
TOPIC = "Capacitance 3: Work done by Battery || Heat loss"
NAME = "Capacitance 3: Work done by Battery Concise Notes.pdf"


@pytest.fixture(autouse=True)
def _no_notes_cache(monkeypatch):
    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", "off")


def _catalog_row(exam="IIT-JEE", batch="Lakshya JEE 2027", subject="Physics By Saleem Ahmad Sir",
                 chapter="Capacitance", topic=TOPIC, link=URL1, status="", review="", notes=""):
    return [exam, batch, subject, chapter, "23 Sep, 2026", topic, link, status, review, notes]


class CatalogSheet:
    """Sheet double that keeps what the bridge writes, like the real tab."""

    def __init__(self, rows, headers_present=False):
        header = list(HEADER) + (list(C.CATALOG_CONTROL_HEADERS) if headers_present else [])
        self.grid = [header] + [list(r) for r in rows]
        self.writes: list[tuple[int, list, str | None]] = []
        self.folder_writes: list[dict] = []
        self.header_writes = 0
        self.validations: list[int] = []

    def read_grid(self):
        return [list(r) for r in self.grid]

    def check_control_headers(self, header_row):
        return len(header_row) >= 11, []

    def ensure_control_headers(self, header_row):
        if len(self.grid[0]) >= 11:
            return False
        self.grid[0] = list(HEADER) + list(C.CATALOG_CONTROL_HEADERS)
        self.header_writes += 1
        return True

    def clear_legacy_columns(self, row_count):
        return False

    def ensure_review_validation(self, row_count):
        self.validations.append(row_count)

    def write_control(self, row_number, values, *, final_timestamp=None, notes_link=None, folder_link=None):
        assert final_timestamp is None, "a catalog tab has no final-status column"
        self.writes.append((row_number, list(values), notes_link))
        cells = self.grid[row_number - 1]
        cells.extend([""] * (11 - len(cells)))
        cells[7], cells[8] = values
        if notes_link is not None:
            cells[9] = notes_link
        if folder_link is not None:
            cells[10] = folder_link

    def write_folder_links(self, links):
        self.folder_writes.append(dict(links))
        for number, link in links.items():
            cells = self.grid[number - 1]
            cells.extend([""] * (11 - len(cells)))
            cells[10] = link

    def labels(self, row_number):
        return [w[1][0].split(" ")[0] for w in self.writes if w[0] == row_number]


class TreeDrive:
    """Drive double with real folder semantics, properties and copies."""

    def __init__(self):
        self.items = {ROOT_ID: DriveFile(ROOT_ID, "notes ", FOLDER_MIME, None, ("root",), "", "")}
        self.kids: dict[str, list] = {ROOT_ID: []}
        self.created_folders: list[str] = []
        self.uploads: list[tuple] = []
        self.copies: list[tuple] = []
        self.updates: list[tuple] = []
        self.list_calls: Counter = Counter()
        self.searches: list[tuple] = []
        self._n = 0

    def _new_id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n:04d}xxxxxxxxxx"

    def add_folder(self, parent, name, modified=""):
        folder = DriveFile(self._new_id("1Fold"), name, FOLDER_MIME, None, (parent,), "", "", modified_time=modified)
        self.items[folder.id] = folder
        self.kids.setdefault(parent, []).append(folder)
        self.kids[folder.id] = []
        return folder

    def add_file(self, parent, name, properties=None):
        file_id = self._new_id("1File")
        entry = DriveFile(file_id, name, "application/pdf", 10, (parent,), "",
                          f"https://drive.google.com/file/d/{file_id}/view",
                          properties=tuple(sorted((properties or {}).items())))
        self.items[file_id] = entry
        self.kids.setdefault(parent, []).append(entry)
        return entry

    def get_file(self, file_id):
        if file_id not in self.items:
            raise BridgeError("drive_not_found")
        return self.items[file_id]

    def update_pdf(self, file_id, data, *, name="", properties=None):
        self.updates.append((file_id, data, dict(properties or {})))
        entry = self.items[file_id]
        return {"id": entry.id, "name": entry.name, "webViewLink": entry.web_view_link}

    def list_folder(self, folder_id, drive_id=""):
        self.list_calls[folder_id] += 1
        return list(self.kids.get(folder_id, []))

    def create_folder(self, parent_id, name, drive_id=""):
        self.created_folders.append(name)
        return self.add_folder(parent_id, name)

    def upload_pdf(self, folder_id, name, data, drive_id="", *, properties=None):
        entry = self.add_file(folder_id, name, properties)
        self.uploads.append((folder_id, name, data, dict(properties or {})))
        return {"id": entry.id, "name": name, "webViewLink": entry.web_view_link}

    def copy_file(self, file_id, folder_id, name, drive_id="", *, properties=None):
        entry = self.add_file(folder_id, name, properties)
        self.copies.append((file_id, folder_id, name))
        return {"id": entry.id, "name": name, "webViewLink": entry.web_view_link}

    def find_by_property(self, key, value, drive_id=""):
        self.searches.append((key, value))
        return [f for f in self.items.values() if f.prop(key) == value]

    def download_pdf(self, file, max_bytes):
        return b"%PDF-1.4 from drive"

    def find_in_folder(self, folder_id, name, drive_id=""):
        return [{"id": f.id, "name": f.name, "webViewLink": f.web_view_link}
                for f in self.kids.get(folder_id, []) if f.name == name]

    def path_of(self, file_id):
        names, current = [], self.items[file_id]
        while current.parents and current.parents[0] != "root":
            names.append(current.name)
            current = self.items[current.parents[0]]
        return list(reversed(names))


def _notes_json() -> bytes:
    from notesbot.prompts import Section, ShortNotes

    return ShortNotes(lecture_no="L03", subject="Physics", title="Capacitance",
                      sections=[Section(heading="Capacitance", points=["C = Q/V"])]).model_dump_json().encode("utf-8")


def _approved(**kwargs):
    from notesbot.final_quality import QUALITY_VERSION, digest

    kwargs["quality_report"] = {"status": "passed", "version": QUALITY_VERSION,
                                "notes_sha256": digest(kwargs["notes_json"]),
                                "pdf_sha256": digest(kwargs["pdf"]), "reviewed_pages": [1]}
    return GenerationResult(**kwargs)


def _generator(calls):
    def generate(pdf_bytes, *, language, subject, signature, **extra):
        from notesbot import generation_profile

        calls.append({"language": language, "subject": subject, "signature": signature,
                      "category": generation_profile.current_category(), "header": extra.get("header"),
                      "filename": extra.get("filename"), "source": pdf_bytes})
        return _approved(pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=12.5, usage={"inr": 12.5})
    return generate


def _run(sheet, drive, calls, fetched=None, **options):
    fetched = [] if fetched is None else fetched
    log: list[str] = []

    def fetch(url, max_bytes):
        fetched.append(url)
        return b"%PDF-1.5 from the web"

    options.setdefault("output_folder_id", ROOT_ID)
    runner = Runner(sheet, drive, RunOptions(**options), generate_fn=_generator(calls),
                    fetch_url=fetch, out=log.append)
    summary = runner.run()
    return summary, log


# --------------------------------------------------------------------------- #
# Layout, eligibility, names
# --------------------------------------------------------------------------- #
def test_catalog_header_is_recognised_and_the_classic_layout_is_unchanged():
    layout = C.Layout.from_header(HEADER)
    assert layout.catalog and not layout.problems()
    assert layout.describe() == {"exam": "A", "batch": "B", "subject": "C", "chapter": "D",
                                 "lecture_topic": "F", "drive_link": "G"}
    assert layout.control_indexes == (7, 8, 9, 10) and layout.control_headers[-2:] == (
        "Concise Notes Link", "Lecture Folder Link")
    assert layout.identity_columns == ("drive_link", "lecture_topic", "chapter")
    written = C.Layout.from_header(HEADER + list(C.CATALOG_CONTROL_HEADERS))
    assert written.catalog and written.control_present and written.control_indexes == (7, 8, 9, 10)
    # A tab written before the folder link existed gets it in the free K column ...
    older = C.Layout.from_header(HEADER + list(C.CATALOG_CONTROL_HEADERS[:3]))
    assert older.notes_folder_index == 10 and older.control_headers == C.CATALOG_CONTROL_HEADERS
    # ... but a K column of someone else's is left alone: the folder link goes to L.
    taken = C.Layout.from_header(HEADER + list(C.CATALOG_CONTROL_HEADERS[:3]) + ["Remarks"])
    assert taken.notes_folder_index == 11 and taken.control_indexes == (7, 8, 9, 11)
    assert taken.control_headers == C.CATALOG_CONTROL_HEADERS


def test_a_catalog_whose_team_renamed_review_and_added_an_upload_column_still_runs():
    """Class_Notes || JEE_NEET: I renamed "Review Status (by ARD Team)" and K
    "Uploading status (by BM Team)" added - once read as the batch sheets' BM
    trigger, which turned the tab into a refused batch tab."""
    from sheets_bridge.sheets import SheetClient, TabInfo

    header = HEADER + ["Auto Status", "Review Status\n(by ARD Team)", "Concise Notes Link",
                       "Uploading status\n(by BM Team)"]
    layout = C.Layout.from_header(header)
    assert layout.catalog and layout.problems() == []
    assert "bm_status" not in layout.indexes
    assert [C.column_letter(i) for i in layout.control_indexes] == ["H", "I", "J", "L"]

    bodies = []

    class _Values:
        def batchUpdate(self, spreadsheetId, body):
            bodies.append(body)
            return type("E", (), {"execute": lambda self, num_retries=0: {}})()

    class _Sheets:
        def values(self):
            return _Values()

    client = SheetClient(type("S", (), {"spreadsheets": lambda self: _Sheets()})(), "sid", 0)
    client._tab = TabInfo("Notes", 0, 26, 1000)
    assert client.check_control_headers(header) == (False, [])      # our columns, renamed: no conflict
    assert client.ensure_control_headers(header)
    written = {d["range"]: d["values"] for d in bodies[-1]["data"]}
    assert written == {"'Notes'!L1": [["Lecture Folder Link"]]}    # the ARD team's I1 is kept as they named it
    # Someone else's column where ours would go is still refused, never overwritten.
    assert client.check_control_headers(HEADER + ["Auto Status", "Remarks"])[1] == ["I1='Remarks'"]
    classic = C.DEFAULT_LAYOUT
    assert not classic.catalog and classic.control_indexes == (16, 17) and classic.notes_link_index is None
    assert classic.identity_columns == ("drive_link", "lecture_number", "chapter")
    # A tab with neither shape keeps the classic complaint.
    assert "no column for 'Drive Link'" in C.Layout.from_header(["Batch Name", "GID"]).problems()


def test_catalog_rows_are_ready_when_they_have_a_link_and_no_result_yet():
    layout = C.Layout.from_header(HEADER + list(C.CATALOG_CONTROL_HEADERS))

    def row(**kwargs):
        return SheetRow.from_values(2, _catalog_row(**kwargs), layout)

    assert is_eligible(row())
    assert not is_eligible(row(link=""))
    assert not is_eligible(row(status="review", review="Review"))
    assert not is_eligible(row(status="skipped: not_a_lecture"))
    assert is_eligible(row(status="failed: download:http_404"))
    assert is_eligible(row(status="review", review="Review"), redo=True)
    assert row(notes="https://drive.google.com/file/d/x/view").notes_link.endswith("/x/view")


@pytest.mark.parametrize("topic, title, number", [
    (" Alcohol, Phenols and Ether 06 : Grignard Reagents || NO DPP || Reschedule @08:00 AM ",
     "Alcohol, Phenols and Ether 06 : Grignard Reagents", 6),
    ("Class 11th Revision Some Basic Concepts Of Chemistry 1: Type of Fission || Types",
     "Class 11th Revision Some Basic Concepts Of Chemistry 1: Type of Fission", 1),
    ("Mechanical properties of solids 02 : || NO DPP", "Mechanical properties of solids 02", 2),
    ("Units And Measurements  : Summary Lecture || (One shot) || (NO DPP)", "Units And Measurements : Summary Lecture", None),
    ("वैद्युत-रसायन 8: सेलो के प्रकार || सान्द्रता सेल || No DPP", "वैद्युत-रसायन 8: सेलो के प्रकार", 8),
    ("Rotational Motion 13 : Topping || No DPP || Extra Recorded Lecture ", "Rotational Motion 13 : Topping", 13),
    ("Reschedule @08:00 AM", "Reschedule @08:00 AM", None),
])
def test_lecture_title_and_number_come_from_the_topic(topic, title, number):
    assert K.lecture_title(topic) == title
    assert K.lecture_number(topic) == number
    assert K.output_name(topic) == f"{title} Concise Notes.pdf"


def test_long_names_are_cut_at_a_word_and_never_leave_a_bracket_open():
    topic = "Aldehydes, Ketones and Carboxylic Acids 13 : " + "Chemical properties of aldehydes " * 5 + "(Part 07)"
    name = K.output_name(topic)
    assert name.endswith(" Concise Notes.pdf") and len(name) <= K.MAX_STEM_LENGTH + len(" Concise Notes.pdf")
    assert name.count("(") == name.count(")")
    assert K.output_name(TOPIC, ordinal=2) == "Capacitance 3: Work done by Battery (2) Concise Notes.pdf"


def test_exam_category_folders_subject_teacher_and_page_one_header():
    # NEET and JEE share the NEET-JEE format but keep their own exam folders.
    assert K.category_for_exam("NEET") == ("neet_jee", "NEET")
    assert K.category_for_exam("IIT-JEE") == ("neet_jee", "JEE") == K.category_for_exam(" jee ")
    assert K.category_for_exam("UPSC CSE") == ("", "UPSC CSE")        # unknown: the exam's own name
    assert K.exam_folder_names("IIT-JEE")[:2] == ("JEE", "IIT-JEE")
    assert K.folder_path("NEET", " Yakeen 2.0 2027 ", "Zoology By Dr. Akanksha Agarwal Ma'am", "Excretion") == (
        ("NEET", "NEET UG"), "Yakeen 2.0 2027", "Zoology", "Excretion")
    # One subject is one folder, whichever teacher or AIR tag the cell carries.
    assert {K.folder_path("JEE", "B", cell, "C")[2] for cell in (
        "Maths", "(AIR) Maths", "Maths by Tarun Khandelwal Sir", "Mathematics", "Mat")} == {"Maths"}
    assert {K.folder_path("NEET", "B", cell, "C")[2] for cell in (
        "Zoology AIR", "Zoology By Samapti Sinha Ma'am", " zoology ")} == {"Zoology"}
    assert K.faculty("Physics by Neeraj Kumar Chaudhary Sir (NKC Sir)") == "Neeraj Kumar Chaudhary Sir (NKC Sir)"
    assert K.faculty("(AIR) Physics") == "" and K.header_subject("(AIR) Physics") == "Physics"
    assert K.header_subject("Physical Chemistry By Rahul Dudi Sir") == "Physical Chemistry"
    assert K.header_chapter("Ch-09 : Parabola") == "Parabola"
    assert K.header_chapter("Biomoleculesजैव-अणु") == "Biomolecules\nजैव-अणु"
    assert K.header_chapter("કિરણપ્રકાશશાસ્ત્ર અને પ્રકાશીય ઉપકરણો (Ray Optics and Optical Instruments)") == \
        "Ray Optics and Optical Instruments"
    assert K.header_chapter("রাসায়নিক বন্ধন") == ""                     # no drawable name: the slides' title is used


def test_language_and_skip_list_follow_the_batch():
    assert K.language_for_batch("Lakshya JEE Gujarati 2027") == "english"
    assert K.language_for_batch("Telangana Achievers 2027 Class 12th - EAPCET/JEE(Tinglish)") == "english"
    assert K.language_for_batch("Yakeen NEET Hindi 2.0 2027") == "hindi"
    assert K.language_for_batch("Lakshya JEE 2027") == "auto"
    assert K.skip_reason("Pi Lens") and K.skip_reason("NEW BATCH UXCC (Old Player Configuration)")
    assert K.skip_reason("ITA - Smart DPP") and K.skip_reason("VP LIVE Batch ( 2024 )")
    assert K.skip_reason("Lakshya JEE 2027") == ""
    assert K.skip_reason("Pi Lens", []) == ""                             # an empty list skips nothing


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def test_a_catalog_row_is_generated_filed_in_the_tree_and_linked_in_the_sheet():
    sheet, drive, calls, fetched = CatalogSheet([_catalog_row()]), TreeDrive(), [], []
    summary, log = _run(sheet, drive, calls, fetched)

    assert summary.counts()["done"] == 1 and summary.total_cost_inr == 12.5
    assert fetched == [URL1]
    assert drive.created_folders == ["JEE", "Lakshya JEE 2027", "Physics", "Capacitance"]
    folder_id, name, data, props = drive.uploads[0]
    assert name == NAME and data == b"%PDF-1.7 notes"
    assert drive.path_of(folder_id) == ["JEE", "Lakshya JEE 2027", "Physics", "Capacitance"]
    assert props[SOURCE_PROPERTY] == http_source.source_key(URL1) and len(props[RENDER_PROPERTY]) == 32
    (call,) = calls
    assert call["category"] == "neet_jee" and call["signature"] == "by Saleem Ahmad Sir"
    assert call["language"] == "auto" and call["subject"] == "physics"
    assert (call["header"].subject, call["header"].chapter, call["header"].lecture) == ("Physics", "Capacitance", "Lecture - 03")
    assert call["filename"] == "Capacitance 3: Work done by Battery.pdf"
    # Claimed first, then review + the notes link + the lecture's folder link in H, I, J, K.
    assert sheet.header_writes == 1
    assert sheet.labels(2) == ["claimed", "review"]
    link = summary.outcomes[0].link
    assert link.startswith("https://drive.google.com/file/d/") and sheet.writes[-1] == (2, ["review", "Review"], link)
    assert sheet.grid[1][7:11] == ["review", "Review", link, f"https://drive.google.com/drive/folders/{folder_id}"]
    assert any("created folder JEE / Lakshya JEE 2027" in line for line in log)


def test_existing_folders_are_reused_whatever_their_case_and_an_iit_jee_folder_is_the_jee_one():
    drive = TreeDrive()
    exam = drive.add_folder(ROOT_ID, "IIT-JEE")
    batch = drive.add_folder(exam.id, "lakshya jee 2027 ")
    summary, _log = _run(CatalogSheet([_catalog_row()]), drive, [])
    assert summary.counts()["done"] == 1
    assert drive.created_folders == ["Physics", "Capacitance"]
    assert drive.path_of(drive.uploads[0][0])[:2] == ["IIT-JEE", "lakshya jee 2027 "]
    assert drive.list_calls[ROOT_ID] == 1 and drive.list_calls[batch.id] == 1


def test_the_same_pdf_in_two_batches_is_generated_once_and_copied():
    rows = [_catalog_row(), _catalog_row(batch="Prayas 2.0 2027")]
    sheet, drive, calls = CatalogSheet(rows), TreeDrive(), []
    summary, log = _run(sheet, drive, calls)
    assert len(calls) == 1 and len(drive.uploads) == 1 and len(drive.copies) == 1
    assert [o.note for o in summary.outcomes] == ["", "copied"] and summary.total_cost_inr == 12.5
    copy_folder = drive.copies[0][1]
    assert drive.path_of(copy_folder)[1] == "Prayas 2.0 2027" and drive.copies[0][2] == NAME
    assert sheet.grid[2][7:9] == ["review", "Review"] and sheet.grid[2][9] != sheet.grid[1][9]
    assert any("no AI cost" in line for line in log)


def test_the_same_row_listed_twice_is_filed_once():
    sheet, drive, calls = CatalogSheet([_catalog_row(), _catalog_row()]), TreeDrive(), []
    summary, _log = _run(sheet, drive, calls)
    assert len(calls) == 1 and len(drive.uploads) == 1 and not drive.copies
    assert [o.note for o in summary.outcomes] == ["", "existing"]
    assert sheet.grid[1][9] == sheet.grid[2][9]


def test_different_pdfs_with_one_lecture_name_get_numbered_files():
    rows = [_catalog_row(), _catalog_row(link=URL2)]
    drive, calls = TreeDrive(), []
    _run(CatalogSheet(rows), drive, calls)
    assert len(calls) == 2
    assert [u[1] for u in drive.uploads] == [NAME, "Capacitance 3: Work done by Battery (2) Concise Notes.pdf"]
    assert drive.uploads[0][0] == drive.uploads[1][0]


def test_notes_filed_by_an_earlier_run_are_adopted_without_cost_even_when_renamed():
    drive = TreeDrive()
    folder = drive.add_folder(drive.add_folder(drive.add_folder(drive.add_folder(
        ROOT_ID, "JEE").id, "Lakshya JEE 2027").id, "Physics").id, "Capacitance")
    kept = drive.add_file(folder.id, "renamed by the reviewer.pdf", {SOURCE_PROPERTY: http_source.source_key(URL1)})
    sheet, calls = CatalogSheet([_catalog_row()]), []
    summary, _log = _run(sheet, drive, calls)
    assert not calls and not drive.uploads and summary.outcomes[0].note == "existing"
    assert sheet.grid[1][7:10] == ["review", "Review", kept.web_view_link]


def test_a_hand_made_file_with_the_name_is_adopted_but_another_lectures_file_is_not():
    drive = TreeDrive()
    folder = drive.add_folder(drive.add_folder(drive.add_folder(drive.add_folder(
        ROOT_ID, "JEE").id, "Lakshya JEE 2027").id, "Physics").id, "Capacitance")
    drive.add_file(folder.id, NAME, {SOURCE_PROPERTY: "another-lecture"})
    calls = []
    _run(CatalogSheet([_catalog_row()]), drive, calls)
    assert len(calls) == 1 and drive.uploads[0][1].endswith("(2) Concise Notes.pdf")
    hand = TreeDrive()
    folder = hand.add_folder(hand.add_folder(hand.add_folder(hand.add_folder(
        ROOT_ID, "JEE").id, "Lakshya JEE 2027").id, "Physics").id, "Capacitance")
    hand.add_file(folder.id, NAME)
    calls = []
    summary, _log = _run(CatalogSheet([_catalog_row()]), hand, calls)
    assert not calls and summary.outcomes[0].note == "existing"


def test_a_later_run_copies_notes_an_earlier_run_made_for_another_batch():
    rows = [_catalog_row(), _catalog_row(batch="Prayas 2.0 2027")]
    sheet, drive, calls = CatalogSheet(rows), TreeDrive(), []
    _run(sheet, drive, calls, limit=1)                      # run 1: only the first row
    assert len(calls) == 1 and sheet.grid[2][7] == ""
    summary, _log = _run(sheet, drive, calls)              # run 2: the other batch
    assert len(calls) == 1 and len(drive.copies) == 1 and summary.outcomes[0].note == "copied"
    assert drive.searches and drive.searches[0][0] == RENDER_PROPERTY


def test_rows_on_the_skip_list_are_marked_and_never_paid_for():
    rows = [_catalog_row(exam="IIT-JEE", batch="Pi Lens", subject="Pi lens", chapter="Pi lens",
                         topic="Current Affairs Today | 23 September Current Affairs 2026")]
    sheet, drive, calls, fetched = CatalogSheet(rows), TreeDrive(), [], []
    summary, _log = _run(sheet, drive, calls, fetched)
    assert summary.counts()["skipped"] == 1 and not calls and not fetched and not drive.created_folders
    assert sheet.writes == [(2, ["skipped: not_a_lecture", ""], None)]
    summary, _log = _run(sheet, drive, calls)              # marked once, then left alone
    assert not summary.outcomes and summary.skipped_not_eligible["not a lecture"] == 1


def test_a_regional_batch_gets_english_notes_under_its_own_folder_names():
    chapter = "કિરણપ્રકાશશાસ્ત્ર અને પ્રકાશીય ઉપકરણો (Ray Optics and Optical Instruments)"
    rows = [_catalog_row(batch="Lakshya JEE Gujarati 2027", subject="Physics", chapter=chapter,
                         topic=f"{chapter} 02 : x")]
    drive, calls = TreeDrive(), []
    _run(CatalogSheet(rows), drive, calls)
    assert calls[0]["language"] == "english" and calls[0]["signature"] == "by PW Faculty"
    assert calls[0]["header"].chapter == "Ray Optics and Optical Instruments"
    assert drive.created_folders[-1] == chapter


def test_a_catalog_row_marked_regenerate_gets_fresh_notes_over_its_filed_pdf():
    """Review Status "Regenerate": new notes (no copy of another row's), the
    filed PDF overwritten in its chapter folder, both links kept, back to Review."""
    rows = [_catalog_row(), _catalog_row(batch="Prayas 2.0 2027")]     # same PDF on two rows
    sheet, drive, calls = CatalogSheet(rows), TreeDrive(), []
    _run(sheet, drive, calls)
    assert len(calls) == 1 and len(drive.uploads) == 1 and len(drive.copies) == 1
    folder_id = drive.uploads[0][0]
    filed_link = sheet.grid[1][9]
    sheet.grid[1][8] = "Regenerate"          # a reviewer asks for fresh notes on row 2

    summary, log = _run(sheet, drive, calls, dry_run=True)
    assert any("marked Regenerate -> would make fresh notes and replace the filed PDF" in line for line in log)

    summary, log = _run(sheet, drive, calls)
    assert [o.row for o in summary.outcomes] == [2]
    assert len(calls) == 2, "fresh notes, not a copy of the other row's"
    assert len(drive.uploads) == 1 and len(drive.copies) == 1       # nothing new beside the old file
    (file_id, _data, props), = drive.updates
    assert drive.items[file_id].parents == (folder_id,) and props[SOURCE_PROPERTY]
    assert sheet.grid[1][7:11] == ["review", "Review", filed_link, f"https://drive.google.com/drive/folders/{folder_id}"]
    assert sheet.labels(2)[-2:] == ["claimed", "review"]
    assert [w[1][1] for w in sheet.writes if w[0] == 2][-2] == "Regenerate"   # kept while claimed


def test_rows_filed_before_the_folder_column_get_their_folder_link_without_new_notes():
    drive = TreeDrive()
    exam = drive.add_folder(ROOT_ID, "JEE")
    chapter = drive.add_folder(drive.add_folder(drive.add_folder(exam.id, "Lakshya JEE 2027").id, "Physics").id,
                               "Capacitance")
    filed = drive.add_file(chapter.id, NAME)
    rows = [_catalog_row(status="review", review="Done", notes=filed.web_view_link)]

    def sheet_as_before():
        sheet = CatalogSheet(rows)
        sheet.grid[0] = list(HEADER) + list(C.CATALOG_CONTROL_HEADERS[:3])   # H:J, no K yet
        return sheet

    sheet, calls = sheet_as_before(), []
    summary, log = _run(sheet, drive, calls, dry_run=True)
    assert not sheet.folder_writes and not sheet.writes
    assert "would fill the Lecture Folder Link of 1 row(s) filed earlier" in "\n".join(log)

    sheet = sheet_as_before()
    summary, log = _run(sheet, drive, calls)
    assert sheet.grid[0][10] == "Lecture Folder Link"
    assert sheet.folder_writes == [{2: f"https://drive.google.com/drive/folders/{chapter.id}"}]
    assert sheet.grid[1][7:11] == ["review", "Done", filed.web_view_link,
                                   f"https://drive.google.com/drive/folders/{chapter.id}"]
    assert not sheet.writes and not calls and not drive.uploads     # nothing regenerated, review left alone
    # A second run finds the link already there and writes nothing.
    _run(sheet, drive, calls)
    assert len(sheet.folder_writes) == 1


def test_neet_rows_follow_the_neet_jee_format_and_a_blank_exam_uses_the_run_category():
    rows = [_catalog_row(exam="NEET", batch="Yakeen 2.0 2027", subject="Zoology"),
            _catalog_row(exam="", batch="Foundation Batch", subject="Maths", link=URL2)]
    drive, calls = TreeDrive(), []
    _run(CatalogSheet(rows), drive, calls, category="foundation")
    assert [c["category"] for c in calls] == ["neet_jee", "foundation"]
    assert [drive.path_of(u[0])[0] for u in drive.uploads] == ["NEET", "Foundation"]


def test_a_drive_file_link_in_a_catalog_is_downloaded_from_drive():
    drive = TreeDrive()
    source = drive.add_file(drive.add_folder(ROOT_ID, "sources").id, "Capacitance 03 Class Notes.pdf")
    rows = [_catalog_row(link=f"https://drive.google.com/file/d/{source.id}/view")]
    calls, fetched = [], []
    _run(CatalogSheet(rows), drive, calls, fetched)
    assert not fetched and calls[0]["source"] == b"%PDF-1.4 from drive"


def test_a_failed_pdf_is_not_paid_for_twice_in_one_run():
    rows = [_catalog_row(), _catalog_row(batch="Prayas 2.0 2027")]
    sheet, drive = CatalogSheet(rows), TreeDrive()

    def broken(url, max_bytes):
        raise BridgeError("download:http_404")

    runner = Runner(sheet, drive, RunOptions(output_folder_id=ROOT_ID), generate_fn=_generator([]),
                    fetch_url=broken, out=lambda _line: None)
    summary = runner.run()
    assert [o.code for o in summary.outcomes] == ["download:http_404", "download:http_404"]
    assert sheet.grid[2][7] == "failed: download:http_404"


def test_dry_run_plans_paths_and_copies_without_touching_sheet_or_drive():
    rows = [_catalog_row(), _catalog_row(batch="Prayas 2.0 2027"),
            _catalog_row(batch="Pi Lens", subject="Pi lens", chapter="Pi lens", topic="x")]
    sheet, drive, calls = CatalogSheet(rows), TreeDrive(), []
    summary, log = _run(sheet, drive, calls, dry_run=True)
    assert [o.outcome for o in summary.outcomes] == ["planned", "planned", "skipped"]
    assert not sheet.writes and not sheet.header_writes and not drive.created_folders and not calls
    text = "\n".join(log)
    assert f"would generate: JEE / Lakshya JEE 2027 / Physics / Capacitance / {NAME}" in text
    assert "same lecture PDF as row 2 -> would copy its notes" in text
    assert "control headers H1:K1 are missing" in text


def test_a_catalog_tab_without_an_output_folder_is_refused_before_anything_is_written():
    sheet = CatalogSheet([_catalog_row()])
    runner = Runner(sheet, TreeDrive(), RunOptions(), generate_fn=_generator([]), out=lambda _line: None)
    with pytest.raises(BridgeError) as err:
        runner.run()
    assert err.value.code == "output_folder_missing" and not sheet.writes and not sheet.header_writes
    with pytest.raises(BridgeError) as err:
        Runner(sheet, TreeDrive(), RunOptions(output_folder_id="1NoSuchFolderxxxxx"), out=lambda _l: None).run()
    assert err.value.code == "output_folder_unavailable"


def test_folder_tree_lists_each_folder_once_and_prefers_the_oldest_duplicate():
    drive = TreeDrive()
    newer = drive.add_folder(ROOT_ID, "NEET", modified="2026-09-20T00:00:00Z")
    older = drive.add_folder(ROOT_ID, "neet", modified="2026-09-01T00:00:00Z")
    tree = FolderTree.open(drive, ROOT_ID)
    assert tree.find_folder(ROOT_ID, ("NEET", "NEET UG")).id == newer.id      # exact spelling first
    assert tree.find_folder(ROOT_ID, "Neet").id == older.id                   # else the oldest
    assert tree.doubled == ["NEET", "Neet"] and drive.list_calls[ROOT_ID] == 1
    made = tree.ensure_path((("NEET",), "Yakeen 2.0 2027", "Zoology", "Excretion"))
    assert drive.created_folders == ["Yakeen 2.0 2027", "Zoology", "Excretion"]
    assert tree.ensure_path((("NEET",), "Yakeen 2.0 2027", "Zoology", "Excretion")).id == made.id
    assert drive.created_folders == ["Yakeen 2.0 2027", "Zoology", "Excretion"]


# --------------------------------------------------------------------------- #
# Sheet client, tab listing, profiles, web download
# --------------------------------------------------------------------------- #
def test_the_sheet_client_writes_the_notes_link_beside_the_status_columns():
    from sheets_bridge.sheets import SheetClient, TabInfo

    bodies = []

    class _Exec:
        def __init__(self, body):
            self.body = body

        def execute(self, num_retries=0):
            bodies.append(self.body)
            return {}

    class _Values:
        def batchUpdate(self, spreadsheetId, body):
            return _Exec(body)

    class _Sheets:
        def values(self):
            return _Values()

    class _Service:
        def spreadsheets(self):
            return _Sheets()

    client = SheetClient(_Service(), "sid", 0)
    client._tab = TabInfo("Notes", 0, 26, 1000)
    client.layout = C.Layout.from_header(HEADER)
    client.write_control(5, ["review", "Review"], notes_link="https://drive.google.com/file/d/abc/view",
                         folder_link="https://drive.google.com/drive/folders/chapter1")
    data = bodies[-1]["data"]
    assert [d["range"] for d in data] == ["'Notes'!H5:I5", "'Notes'!J5", "'Notes'!K5"]
    assert data[1]["values"] == [["https://drive.google.com/file/d/abc/view"]]
    assert data[2]["values"] == [["https://drive.google.com/drive/folders/chapter1"]]
    client.write_control(6, ["claimed 2026-09-28 10:00:00 +0530", ""])
    assert [d["range"] for d in bodies[-1]["data"]] == ["'Notes'!H6:I6"]       # the links stay untouched
    assert client._control_slots(client.layout)[-2:] == ((9, "Concise Notes Link"), (10, "Lecture Folder Link"))
    # Rows filed earlier: only their K cells, in one request.
    client.write_folder_links({12: "https://drive.google.com/drive/folders/a", 3: "https://drive.google.com/drive/folders/b"})
    assert [d["range"] for d in bodies[-1]["data"]] == ["'Notes'!K3", "'Notes'!K12"]


def test_the_tab_listing_counts_ready_catalog_rows():
    from sheets_bridge import tabs as T

    layout = C.Layout.from_header(HEADER + list(C.CATALOG_CONTROL_HEADERS))
    assert T._count_columns(layout) == (6, 7, 8, 8)
    assert T._ready(layout, URL1, "", "") and T._ready(layout, URL1, "failed: download:http_404", "")
    assert not T._ready(layout, URL1, "review", "Review")
    assert T._ready(layout, URL1, "review", "Regenerate")                # asked for fresh notes
    assert not T._ready(layout, URL1, "skipped: not_a_lecture", "")
    assert not T._ready(layout, "", "", "")


def test_profile_output_folder_and_skip_list_round_trip_and_reach_the_run(tmp_path):
    from sheets_bridge import profiles as P
    from sheets_bridge.batch import options_for

    path = tmp_path / "sheets.json"
    path.write_text(json.dumps({"profiles": [{
        "name": "Class Notes Catalog", "spreadsheetId": "1dPYUsfG9orAjUWvwcX8qOu7rKpruqOmaQDiaHqn-k9k", "gid": 0,
        "outputFolder": "https://drive.google.com/drive/folders/1T6H3Iir9amxkTkYpD6jVHRCiDXpu3doD?usp=sharing",
        "skipBatches": ["Pi Lens", " "],
    }]}), encoding="utf-8")
    (target,) = P.load_profiles(path)
    assert target.output_folder == "1T6H3Iir9amxkTkYpD6jVHRCiDXpu3doD" and target.skip_batches == ("Pi Lens",)
    assert target.to_json()["outputFolder"] == target.output_folder and target.public()["outputFolder"]
    P.save_profiles((target,), path)
    assert P.load_profiles(path) == (target,)
    options = options_for(target, RunOptions(), spent=0.0)
    assert options.output_folder_id == target.output_folder and options.skip_batches == ("Pi Lens",)
    for bad in ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view", "not a link"):
        with pytest.raises(BridgeError) as err:
            P.parse_folder_link(bad)
        assert err.value.code == "bad_output_folder"


def test_adding_a_catalog_tab_needs_its_output_folder(monkeypatch):
    from sheets_bridge import profiles as P
    from sheets_bridge import sheets as S

    class _Client:
        def __init__(self, service, spreadsheet_id, gid):
            pass

        def tab(self):
            return S.TabInfo("Notes", 0, 26, 100)

        def read_grid(self):
            return [HEADER, _catalog_row()[:7]]

        def probe_cell_kinds(self, row_number, last_index):
            return {}

    monkeypatch.setattr(S, "SheetClient", _Client)
    target = P.SheetTarget(name="Catalog", spreadsheet_id="sid", gid=0)
    with pytest.raises(BridgeError) as err:
        P.verify_target(None, target)
    assert err.value.code == "output_folder_required"
    checked = P.verify_target(None, replace(target, output_folder=ROOT_ID))
    assert checked["catalog"] is True and checked["eligible"] == 1


class _Response:
    def __init__(self, status=200, body=b"%PDF-1.7 lecture", length=None):
        self.status_code = status
        self._body = body
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        for start in range(0, len(self._body), 4):
            yield self._body[start:start + 4]


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def test_web_pdf_download_limits_and_retries():
    naps = []
    fetch = lambda session, limit=100: http_source.download_pdf(URL1, limit, session=session, sleep=naps.append)  # noqa: E731
    assert fetch(_Session(_Response())) == b"%PDF-1.7 lecture"
    cases = [
        (_Session(_Response(length=1000)), "too_large"),
        (_Session(_Response(body=b"%PDF" + b"x" * 200)), "too_large"),
        (_Session(_Response(body=b"<html>login</html>")), "not_pdf"),
        (_Session(_Response(status=404)), "download:http_404"),
        (_Session(*[ConnectionError()] * 4), "download:unreachable"),
    ]
    for session, code in cases:
        with pytest.raises(BridgeError) as err:
            fetch(session)
        assert err.value.code == code
    flaky = _Session(_Response(status=503), _Response())
    assert fetch(flaky) == b"%PDF-1.7 lecture" and flaky.calls == 2 and naps
    with pytest.raises(BridgeError) as err:
        http_source.download_pdf("ftp://example.com/a.pdf", 100, session=_Session())
    assert err.value.code == "invalid_link"
    assert http_source.source_key(URL1) == http_source.source_key(f"  {URL1} ") != http_source.source_key(URL2)


# --------------------------------------------------------------------------- #
# Faculty columns (Class_Notes, 1 October 2026)
# --------------------------------------------------------------------------- #
FACULTY_HEADER = ["exam", "batch", "subject", "faculty_id", "faculty_name", "faculty_email", "chapter",
                  "Lecture_Date", "lecture_topic", "downloadable_link", "Auto Status",
                  "Review Status\n(by ARD Team)", "Concise Notes Link", "Uploading status\n(by BM Team)",
                  "Lecture Folder Link"]


class FacultyCatalogSheet(CatalogSheet):
    """The catalog after its team added faculty_id / faculty_name /
    faculty_email after the subject, shifting the later columns right."""

    def __init__(self, rows):
        super().__init__([])
        self.grid = [list(FACULTY_HEADER)] + [list(r) for r in rows]
        self.layout = None      # set by the runner

    def check_control_headers(self, header_row):
        return True, []

    def ensure_control_headers(self, header_row):
        return False

    def write_control(self, row_number, values, *, final_timestamp=None, notes_link=None, folder_link=None):
        self.writes.append((row_number, list(values), notes_link))
        cells = self.grid[row_number - 1]
        cells.extend([""] * (len(FACULTY_HEADER) - len(cells)))
        auto, review, link, folder = self.layout.control_indexes
        cells[auto], cells[review] = values
        if notes_link is not None:
            cells[link] = notes_link
        if folder_link is not None:
            cells[folder] = folder_link


def _faculty_row(name="Rohit Agarwal Sir", subject="Organic Chemistry", link=URL1, topic="GOC 04 : Resonating Structure"):
    return ["JEE", "Prayas 2027", subject, "637cc360a3039607af5e2524", name, "rohit.agarwal@pw.live", "GOC",
            "29 Sep, 2026", topic, link, "", "", "", "", ""]


def test_the_faculty_name_column_signs_the_notes_and_the_shifted_columns_are_read():
    layout = C.Layout.from_header(FACULTY_HEADER)
    assert layout.catalog and layout.problems() == []
    assert {name: layout.letter(name) for name in ("subject", "faculty", "chapter", "lecture_topic", "drive_link")} == {
        "subject": "C", "faculty": "E", "chapter": "G", "lecture_topic": "I", "drive_link": "J"}
    assert [C.column_letter(i) for i in layout.control_indexes] == ["K", "L", "M", "O"]   # N is the BM team's
    # Without faculty_name, neither the id nor the email is taken for a teacher.
    assert C.Layout.from_header([h for h in FACULTY_HEADER if h != "faculty_name"]).index("faculty") is None

    sheet = FacultyCatalogSheet([
        _faculty_row(),
        _faculty_row(name="", subject="Physics By Saleem Ahmad Sir", link=URL2, topic=TOPIC),
    ])
    drive, calls = TreeDrive(), []
    summary, _log = _run(sheet, drive, calls)
    assert [c["signature"] for c in calls] == ["by Rohit Agarwal Sir", "by Saleem Ahmad Sir"]
    assert drive.uploads[0][1] == "GOC 04 : Resonating Structure Concise Notes.pdf"
    assert drive.path_of(drive.uploads[0][0]) == ["JEE", "Prayas 2027", "Organic Chemistry", "GOC"]
    row = sheet.grid[1]
    assert row[10] == "review" and row[11] == "Review" and row[12].startswith("https://drive.google.com/file/d/")
    assert row[13] == "" and row[14].startswith("https://drive.google.com/drive/folders/")
    assert row[4] == "Rohit Agarwal Sir" and row[9] == URL1          # the human columns are untouched


def test_a_signature_typed_in_the_studio_is_fixed_for_the_whole_run():
    from notesbot import generation_profile
    from sheets_bridge import output_cache

    seen = []

    def generate(pdf_bytes, *, language, subject, signature, **extra):
        seen.append((signature, generation_profile.signature_fixed()))
        return _approved(pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=1.0, usage={"inr": 1.0})

    for override, expected in (("", ("by Rohit Agarwal Sir", False)), ("Typed Name", ("by Typed Name", True))):
        sheet = FacultyCatalogSheet([_faculty_row()])
        Runner(sheet, TreeDrive(), RunOptions(output_folder_id=ROOT_ID, signature_override=override),
               generate_fn=generate, fetch_url=lambda url, limit: b"%PDF-1.5 web", out=lambda _l: None).run()
        assert seen[-1] == expected
    assert not generation_profile.signature_fixed()                  # never leaks out of the row
    base = dict(file_id="f", out_name="n", language="english", subject="physics", signature="by A")
    assert output_cache.key(**base) == output_cache.key(**base, signature_fixed=False)
    assert output_cache.key(**base) != output_cache.key(**base, signature_fixed=True)
