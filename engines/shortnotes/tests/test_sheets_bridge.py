"""Network-free unit tests for the Sheets/Drive -> notes generator bridge."""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from sheets_bridge import columns as C
from sheets_bridge.drive import DriveFile, escape_query_value
from sheets_bridge.errors import BridgeError, describe_exception, error_code_for
from sheets_bridge.generator import GenerationResult
from sheets_bridge.lecture_match import choose_lecture_pdf, lecture_numbers, parse_lecture_number
from sheets_bridge.links import KIND_FILE, KIND_FOLDER, parse_drive_file_link, parse_drive_link
from sheets_bridge.mapping import (
    map_language,
    map_subject,
    normalize_signature,
    output_name,
    resolve_subject,
    subject_hint,
)
from sheets_bridge.rows import SheetRow, claim_is_stale, is_eligible, rows_from_grid
from sheets_bridge.runner import (
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_PLANNED,
    OUTCOME_STOPPED,
    RowOutcome,
    RunOptions,
    Runner,
)
from sheets_bridge.sheets import a1_sheet_prefix


@pytest.fixture(autouse=True)
def _restore_generator_renderer():
    """Undo the runtime patches this bridge installs into `notesbot`.

    `pdf_fonts.install()` and `pdf_branding.install()` deliberately patch the
    renderer for the life of the process — correct in production, but it must
    not leak into the repo's own renderer tests when the whole suite runs.
    """
    from notesbot import notes_pdf

    from sheets_bridge import pdf_branding, pdf_fonts

    # Raw class-dict entries, not attribute lookups: `_load_logo_watermark` is a
    # staticmethod, and restoring the bare function would turn it into an
    # instance method for every later test in the session.
    saved = {name: notes_pdf.NotesPDF.__dict__[name]
             for name in ("__init__", "header", "_load_logo_watermark", "_word_width", "_draw_word")}
    # pdf_fonts also wraps the drawing primitives so headings and headers shape
    # Devanagari. Those are inherited from FPDF, so undoing the patch means
    # removing the wrapper from NotesPDF, not assigning the inherited one back.
    inherited = {name: notes_pdf.NotesPDF.__dict__.get(name)
                 for name in ("cell", "multi_cell", "text", "get_string_width")}
    saved_sanitize = notes_pdf._sanitize_text
    saved_flags = (pdf_fonts._INSTALLED, pdf_branding._INSTALLED)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(notes_pdf.NotesPDF, name, value)
        for name, value in inherited.items():
            if value is not None:
                setattr(notes_pdf.NotesPDF, name, value)
            elif name in notes_pdf.NotesPDF.__dict__:
                delattr(notes_pdf.NotesPDF, name)
        notes_pdf._sanitize_text = saved_sanitize
        pdf_fonts._INSTALLED, pdf_branding._INSTALLED = saved_flags


@pytest.fixture(autouse=True)
def _isolated_profiles(monkeypatch, tmp_path):
    """Never read or write the operator's real sheets.json from a test."""
    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(tmp_path / "sheets.test.json"))


@pytest.fixture(autouse=True)
def _no_pacing_sleeps(monkeypatch):
    """The generator paces its calls; tests must not wait for that.

    Patch the module's own ``time`` reference, not ``time.sleep`` itself —
    the latter is the real module and would stop every other wait in the
    process, including this suite's own polling loops.
    """
    from types import SimpleNamespace

    from sheets_bridge import coverage

    monkeypatch.setattr(coverage, "time", SimpleNamespace(sleep=lambda _seconds: None))


@pytest.fixture(autouse=True)
def _isolated_output_cache(tmp_path, monkeypatch):
    """Never touch the real ~/.shortnotes_bridge/pending during tests.

    The cache is deliberately shared *within* a test (a retry must find the
    previous attempt's PDF), so it is scoped per test, not per run.
    """
    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", str(tmp_path / "pending"))


# --------------------------------------------------------------------------- #
# subject / language mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Physical Chemistry", "chemistry"),   # "chem" must win over "physic"
        ("Physics ", "physics"),               # trailing space in the sheet
        (" physics", "physics"),
        ("Chemistry", "chemistry"),
        ("Organic Chemistry", "chemistry"),
        ("Botany", "biology"),
        ("Zoology", "biology"),
        ("Biology", "biology"),
        ("Mathematics", "mathematics"),
        ("Maths", "mathematics"),
        ("गणित", "mathematics"),
        ("", "auto"),
        ("   ", "auto"),
        ("History", "social_science"),  # a Foundation subject since 2026-09-25
        ("Mental Ability", "auto"),
        ("Phyiscs", "physics"),         # typo in the NEET sheets
        ("Mat", "mathematics"),         # Pre-Foundation sheet
        ("(AIR) Physical Chemistry", "chemistry"),
        ("Computer Science", "auto"),   # not the school "science"
        (None, "auto"),
        (42, "auto"),
    ],
)
def test_map_subject(raw, expected):
    assert map_subject(raw) == expected


@pytest.mark.parametrize(
    "batch, expected",
    [
        ("Lakshya NEET Hindi 2027", "hindi"),
        ("Arjuna JEE Hinglish 2026", "hinglish"),
        ("Yakeen NEET English 2027", "english"),
        ("Prayas 2.0 2027 (OG)", "auto"),
        ("", "auto"),
        (None, "auto"),
    ],
)
def test_map_language(batch, expected):
    assert map_language(batch) == expected


def test_resolve_subject_modes():
    assert resolve_subject("sheet", "Physical Chemistry") == "chemistry"
    assert resolve_subject("auto", "Physical Chemistry") == "auto"
    assert resolve_subject("biology", "Physics") == "biology"
    with pytest.raises(ValueError):
        resolve_subject("astrology", "Physics")
    assert subject_hint("auto") == ""
    assert subject_hint("physics") == "physics"


# --------------------------------------------------------------------------- #
# link parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "link",
    [
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing",
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view",
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv",
        "https://drive.google.com/file/u/0/d/1AbCdEfGhIjKlMnOpQrStUv/view",
        "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUv&usp=drive_copy",
        "https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOpQrStUv&export=download",
        "  https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view  ",
    ],
)
def test_file_links_resolve(link):
    assert parse_drive_file_link(link) == "1AbCdEfGhIjKlMnOpQrStUv"


@pytest.mark.parametrize(
    "link",
    [
        "https://drive.google.com/drive/folders/1FoLdErIdFoLdErIdFoLdEr",
        "https://drive.google.com/drive/folders/1FoLdErIdFoLdErIdFoLdEr?usp=sharing",
        "https://drive.google.com/drive/u/1/folders/1FoLdErIdFoLdErIdFoLdEr",
    ],
)
def test_folder_links_parse_as_folders_and_strict_variant_refuses(link):
    parsed = parse_drive_link(link)
    assert parsed.kind == KIND_FOLDER and parsed.id == "1FoLdErIdFoLdErIdFoLdEr"
    with pytest.raises(BridgeError) as info:
        parse_drive_file_link(link)
    assert info.value.code == "folder_link_needs_resolver"


def test_file_links_parse_as_files():
    parsed = parse_drive_link("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view")
    assert parsed.kind == KIND_FILE and parsed.id == "1AbCdEfGhIjKlMnOpQrStUv"


@pytest.mark.parametrize("link, code", [("", "blank_link"), ("   ", "blank_link"), (None, "blank_link"), ("Physics", "invalid_link"), ("https://example.com/x", "invalid_link")])
def test_bad_links(link, code):
    with pytest.raises(BridgeError) as info:
        parse_drive_file_link(link)
    assert info.value.code == code


# --------------------------------------------------------------------------- #
# columns
# --------------------------------------------------------------------------- #
def test_control_columns_are_q_and_r():
    assert C.control_column_letters() == ("Q", "R")
    assert C.control_range(7) == "Q7:R7"
    assert C.column_letter(0) == "A" and C.column_letter(25) == "Z" and C.column_letter(26) == "AA"
    assert C.COL_AUTO_STATUS == 16 and C.COL_REVIEW_STATUS == 17
    # the retired columns are still read so they can be cleared once
    assert C.read_range() == "A1:V" and C.legacy_range(7) == "S7:V7"


def test_a1_prefix_quotes_titles():
    assert a1_sheet_prefix("Sheet1") == "'Sheet1'"
    assert a1_sheet_prefix("Rahul's tab") == "'Rahul''s tab'"


# --------------------------------------------------------------------------- #
# naming / escaping / signature
# --------------------------------------------------------------------------- #
def test_output_name():
    assert output_name("L01_Determinants_Class_Notes.pdf") == "L01_Determinants Concise Notes.pdf"
    assert output_name("भौतिकी कक्षा नोट्स.PDF") == "भौतिकी कक्षा नोट्स Concise Notes.pdf"
    assert output_name("no-extension") == "no-extension Concise Notes.pdf"
    assert output_name("") == "Lecture Concise Notes.pdf"


def test_escape_query_value():
    assert escape_query_value("L'Hospital") == "L\\'Hospital"
    assert escape_query_value("a\\b") == "a\\\\b"


def test_normalize_signature():
    assert normalize_signature("  Rahul   Sir ") == "by Rahul Sir"
    assert normalize_signature("By Rahul") == "By Rahul"
    assert normalize_signature("") == "by PW Faculty"


def test_error_helpers_never_leak_messages():
    try:
        raise RuntimeError("SECRET student content sk-abcdefghijklmnop")
    except RuntimeError as exc:
        text = describe_exception(exc)
        assert "SECRET" not in text and "sk-" not in text
        assert text.startswith("RuntimeError @ test_sheets_bridge.py:")
        assert error_code_for(exc, "generate") == "generate:RuntimeError"
    assert error_code_for(BridgeError("too_large"), "download") == "too_large"


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #
def _row(
    number: int,
    status: str = "",
    link: str = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view",
    *,
    bm: str = "Done",
    final: str = "",
    lecture: str = "L01",
    stamp: str = "",
    chapter: str = "",
    review: str = "",
) -> SheetRow:
    values = ["P1 | X", "Proj", "Lakshya NEET Hindi 2027", "Physics ", "Rahul Sir", "1/1/2026", chapter, lecture, link, bm]
    values += ["SPOC", "", final, "", "", ""]          # K L M N O P
    assert len(values) == C.HUMAN_COLUMN_COUNT
    values += [status, review, "", "", stamp, ""]
    return SheetRow.from_values(number, values)


def test_trigger_is_j_done_and_m_not_done():
    assert is_eligible(_row(2))                                   # J Done, M empty
    assert not is_eligible(_row(3, bm=""))                       # J blank
    assert not is_eligible(_row(4, bm="Pending"))
    assert is_eligible(_row(5, bm=" done. "))                    # tolerant match
    assert not is_eligible(_row(6, final="Done"))                # already finished
    assert not is_eligible(_row(7, final="DONE"))
    assert not is_eligible(SheetRow.from_values(8, []))          # blank grid row
    assert not is_eligible(SheetRow.from_values(9, []), redo=True)


def test_done_rows_are_skipped_unless_redo():
    """A row already marked done (M = Done) is skipped unless --redo."""
    done = _row(2, "done", final="Done")
    assert not is_eligible(done)
    assert is_eligible(done, redo=True)
    assert not is_eligible(_row(3, "", final="Done", bm=""), redo=True)   # redo never overrides J
    # the Q ledger: failed rows are retried; a fresh claim is not; a stale claim is
    assert is_eligible(_row(4, "failed"))
    assert not is_eligible(_row(5, "claimed", stamp="2026-08-18 10:00:00 +0530"), now=__import__("datetime").datetime.fromisoformat("2026-08-18T10:30:00+05:30"))
    assert is_eligible(_row(6, "claimed", stamp="2026-08-18 10:00:00 +0530"), now=__import__("datetime").datetime.fromisoformat("2026-08-18T14:30:00+05:30"))
    assert is_eligible(_row(7, "claimed"))                       # no timestamp -> stale
    # M is no longer written by the automation, so Q/R are what stop a second
    # generation: "done" in Q, or anything in the review dropdown.
    assert not is_eligible(_row(8, "done"))
    assert not is_eligible(_row(9, "", review="Review"))
    assert not is_eligible(_row(10, "", review="Done"))
    assert is_eligible(_row(11, "done"), redo=True)


def test_claim_is_stale():
    import datetime as dt
    now = dt.datetime.fromisoformat("2026-08-18T12:00:00+05:30")
    assert claim_is_stale("", now=now)
    assert claim_is_stale("garbage", now=now)
    assert not claim_is_stale("'2026-08-18 11:00:00 +0530", now=now)
    assert claim_is_stale("2026-08-18 08:00:00 +0530", now=now)


# --------------------------------------------------------------------------- #
# lecture matching inside a folder
# --------------------------------------------------------------------------- #
def _pdf(name, fid=None):
    return DriveFile(fid or name, name, "application/pdf", 10, (FOLDER_ID,), "", "")


def test_a_file_named_exactly_as_the_chapter_beats_one_that_only_contains_it():
    """Class 10 Maths folder, no lecture numbers: "Circles" is CIRCLES.pdf,
    and "Areas related to circles" is the other one - never lecture_number_missing."""
    files = [_pdf("CIRCLES.pdf"), _pdf("AREAS RELATED TO CIRCLES.pdf"), _pdf("TRIANGLES Concise Notes.pdf"),
             _pdf("SURFACE AREAS AND VOLUMES.pdf")]
    assert choose_lecture_pdf(files, None, chapter="Circles", folder_name="Maths").file.name == "CIRCLES.pdf"
    assert choose_lecture_pdf(files, None, chapter="Areas related to circles",
                              folder_name="Maths").file.name == "AREAS RELATED TO CIRCLES.pdf"


def test_a_chapter_title_wrapped_onto_two_lines_is_one_name():
    """G = "Earth as a System: Energy, Matter, \\nand Life": the second line alone
    ("and Life") matched every "... Life" lecture in the folder."""
    files = [_pdf(name) for name in ("Cell: The Building Block of Life", "Tissues in Action ",
                                     "Reproduction: How Life Continues",
                                     "Patterns in Life: Diversity and Classification",
                                     "Earth as a System: Energy, Matter,  and Life")]
    chosen = choose_lecture_pdf(files, None, chapter="Earth as a System: Energy, Matter, \nand Life",
                                folder_name="Class 9")
    assert chosen.file.name == "Earth as a System: Energy, Matter,  and Life"
    # Two spellings on two lines (English / Hindi) still work as before.
    bilingual = [_pdf("Electric Charges and Fields.pdf"), _pdf("Current Electricity.pdf")]
    assert choose_lecture_pdf(bilingual, None, chapter="Electric Charges and Fields\nवैद्युत आवेश तथा क्षेत्र",
                              folder_name="Physics").file.name == "Electric Charges and Fields.pdf"


@pytest.mark.parametrize("text, expected", [("L01", 1), ("Lecture 12", 12), ("7", 7), ("L-3", 3), ("१२", 12), ("", None), (None, None), ("abc", None)])
def test_parse_lecture_number(text, expected):
    assert parse_lecture_number(text) == expected


def test_lecture_numbers_in_names():
    assert lecture_numbers("L05 Class Notes.pdf") == {5}
    assert lecture_numbers("Lecture-12 Thermodynamics.pdf") == {12}
    assert lecture_numbers("Lec 3 (Part 2).pdf") == {3}
    assert lecture_numbers("Class 12 Physics L07.pdf") == {7}         # "Class 12" is the grade
    assert lecture_numbers("व्याख्यान ०३ भौतिकी.pdf") == {3}
    assert lecture_numbers("Chapter 4 notes.pdf") == set()
    assert lecture_numbers("L10.pdf") == {10} and 1 not in lecture_numbers("L10.pdf")


def test_choose_lecture_pdf_by_token_and_prefers_notes():
    files = [_pdf("L01 Class Notes.pdf"), _pdf("L02 Class Notes.pdf"), _pdf("L02 DPP.pdf"), _pdf("Lecture 2.pptx").__class__("p", "Lecture 2.pptx", "application/vnd.ms-powerpoint", 5, (FOLDER_ID,), "", ""), _pdf("L02 Class Notes - Concise Notes.pdf")]
    assert choose_lecture_pdf(files, 1).file.name == "L01 Class Notes.pdf"
    m = choose_lecture_pdf(files, 2)
    assert m.file.name == "L02 Class Notes.pdf" and m.how == "lecture_token"   # DPP loses the tie-break; PPT and our output ignored
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(files, 3)
    assert info.value.code == "lecture_not_found"


def test_choose_lecture_pdf_ambiguity_and_fallbacks():
    # an "(old)" copy of the same lecture is not ambiguous: the current one is used
    match = choose_lecture_pdf([_pdf("L02 Class Notes.pdf"), _pdf("L02 Class Notes (old).pdf")], 2)
    assert match.file.name == "L02 Class Notes.pdf" and "2 copies" in match.note
    # two DIFFERENT lectures with the same number still fail, and say which
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf([_pdf("L02 Circles.pdf"), _pdf("L02 Triangles.pdf")], 2)
    assert info.value.code == "lecture_ambiguous"
    assert "'L02 Circles.pdf'" in info.value.detail and "'L02 Triangles.pdf'" in info.value.detail
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf([], 2)
    assert info.value.code == "no_pdf_in_folder"
    # loose numbers only when the folder has no lecture-token numbering at all
    assert choose_lecture_pdf([_pdf("Thermo 04 notes.pdf"), _pdf("Thermo 05 notes.pdf")], 5).how == "loose_number"
    with pytest.raises(BridgeError) as info:                       # token numbering present elsewhere -> no loose match
        choose_lecture_pdf([_pdf("L01 notes.pdf"), _pdf("Thermo 05 notes.pdf")], 5)
    assert info.value.code == "lecture_not_found"
    # single un-numbered PDF is accepted; missing lecture number needs a single file
    assert choose_lecture_pdf([_pdf("Class Notes.pdf")], 9).how == "single_file"
    assert choose_lecture_pdf([_pdf("Class Notes.pdf")], None).how == "single_file"
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf([_pdf("L01.pdf"), _pdf("L02.pdf")], None)
    assert info.value.code == "lecture_number_missing"


def test_rows_from_grid_numbering_and_short_rows():
    grid = [["Project Code"], ["a", "b", "c"], []]
    rows = rows_from_grid(grid)
    assert [r.number for r in rows] == [2, 3]
    assert rows[0].batch == "c" and rows[0].drive_link == "" and rows[1].is_blank


# --------------------------------------------------------------------------- #
# runner with fakes
# --------------------------------------------------------------------------- #
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUv"
FOLDER_ID = "1FoLdErIdFoLdErIdFoLdEr"


class FakeSheet:
    def __init__(self, grid, headers_present=True):
        self.grid = grid
        self.writes: list[tuple[int, list]] = []
        self.finals: list[tuple[int, str]] = []
        self.validations: list[int] = []
        self.header_writes = 0
        self.headers_present = headers_present

    def read_grid(self):
        return self.grid

    def check_control_headers(self, header_row):
        return self.headers_present, []

    def clear_legacy_columns(self, row_count):
        return False

    def ensure_control_headers(self, header_row):
        if self.headers_present:
            return False
        self.header_writes += 1
        self.headers_present = True
        return True

    def write_control(self, row_number, values, *, final_timestamp=None):
        self.writes.append((row_number, list(values)))
        if final_timestamp is not None:
            self.finals.append((row_number, final_timestamp))

    def ensure_review_validation(self, row_count):
        self.validations.append(int(row_count))


SOURCE = DriveFile(FILE_ID, "L01 भौतिकी Class Notes.pdf", "application/pdf", 1234, (FOLDER_ID,), "", "https://drive.google.com/file/d/x/view")
FOLDER = DriveFile(FOLDER_ID, "Chapter 3", "application/vnd.google-apps.folder", None, ("root",), "", "")


DISTINCT_FILES = {
    f"1DistinctSource{index}xxxxxxxxxx": DriveFile(
        f"1DistinctSource{index}xxxxxxxxxx", f"L0{index} Lecture {index} Class Notes.pdf",
        "application/pdf", 1234, (FOLDER_ID,), "", "",
    )
    for index in range(2, 6)
}


def _distinct_row(number: int, **kwargs) -> SheetRow:
    """A row whose source file is unique, so it has its own cache entry."""
    file_id = f"1DistinctSource{number}xxxxxxxxxx"
    return _row(number, link=f"https://drive.google.com/file/d/{file_id}/view", **kwargs)


class FakeDrive:
    def __init__(self, files=None, existing=None, fail_download=False, children=None):
        self.files = files or {FILE_ID: SOURCE, FOLDER_ID: FOLDER, **DISTINCT_FILES}
        self.children = {FOLDER_ID: [SOURCE, DriveFile("f2", "L02 भौतिकी Class Notes.pdf", "application/pdf", 99, (FOLDER_ID,), "", "")]} if children is None else children
        self.existing = existing or {}
        self.fail_download = fail_download
        self.uploads: list[tuple[str, str, bytes]] = []      # review documents
        self.pdf_uploads: list[tuple[str, str, bytes]] = []  # studygram PDFs
        self.list_calls: list[tuple[str, str, str]] = []
        self.downloads = 0

    def get_file(self, file_id):
        if file_id not in self.files:
            raise BridgeError("drive_not_found")
        return self.files[file_id]

    def find_in_folder(self, folder_id, name, drive_id=""):
        self.list_calls.append((folder_id, name, drive_id))
        return list(self.existing.get((folder_id, name), []))

    def list_folder(self, folder_id, drive_id=""):
        return list(self.children.get(folder_id, []))

    def download_pdf(self, file, max_bytes):
        self.downloads += 1
        if self.fail_download:
            raise BridgeError("too_large")
        return b"%PDF-1.4 fake"

    def upload_pdf(self, folder_id, name, data, drive_id=""):
        self.pdf_uploads.append((folder_id, name, data))
        return {"id": "newpdf", "name": name, "webViewLink": "https://drive.google.com/file/d/newpdf/view"}

    def upload_doc(self, folder_id, name, docx_bytes, drive_id=""):   # retired path
        raise AssertionError("the automation must not upload a Doc any more")


def _status_labels(sheet) -> list:
    """Q labels written so far, with the claim stamp folded away."""
    return [re.sub(r"^claimed .*$", "claimed", w[1][0]) for w in sheet.writes]


def _grid(*rows: SheetRow):
    header = list(C.HUMAN_HEADERS) + list(C.CONTROL_HEADERS)
    return [header] + [list(r.values) for r in rows]


def _notes_json() -> bytes:
    from notesbot.prompts import Section, ShortNotes

    return ShortNotes(
        lecture_no="L01", subject="Physics", title="Test Notes",
        sections=[Section(heading="Kinematics", points=["v = u + at"])],
    ).model_dump_json().encode("utf-8")


def _approved_result(**kwargs):
    from notesbot.final_quality import QUALITY_VERSION, digest
    kwargs["quality_report"] = {"status": "passed", "version": QUALITY_VERSION,
                                "notes_sha256": digest(kwargs["notes_json"]),
                                "pdf_sha256": digest(kwargs["pdf"]), "reviewed_pages": [1]}
    return GenerationResult(**kwargs)


def _fake_generate(calls):
    def generate(pdf_bytes, *, language, subject, signature, **extra):
        calls.append({"language": language, "subject": subject, "signature": signature})
        return _approved_result(
            pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=4.5, usage={"inr": 4.5},
        )
    return generate


def test_page_number_option_is_default_on_strictly_boolean_and_forwarded(monkeypatch):
    from types import SimpleNamespace
    from sheets_bridge import webui
    settings = SimpleNamespace(max_cost_inr=None)
    assert webui._clean_options({}, settings)["showPageNumbers"] is True
    assert webui._clean_options({"showPageNumbers": False}, settings)["showPageNumbers"] is False
    for invalid in ("false", 0, None, []):
        with pytest.raises(ValueError):
            webui._clean_options({"showPageNumbers": invalid}, settings)
    seen = []
    def generate(*args, **kwargs):
        seen.append(kwargs["show_page_numbers"])
        return _approved_result(pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=0)
    for show in (True, False):
        sheet = FakeSheet(_grid(_row(2)))
        runner = Runner(sheet, FakeDrive(), RunOptions(show_page_numbers=show),
                        generate_fn=generate, out=lambda _: None)
        assert runner.run().counts()["done"] == 1
    assert seen == [True, False]


def test_category_option_is_checked_and_reaches_the_prompt_of_each_row(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from notesbot import categories
    from notesbot.prompts import build_prompt
    from sheets_bridge import webui
    settings = SimpleNamespace(max_cost_inr=None)
    assert webui._clean_options({}, settings)["category"] == "neet_jee"
    assert webui._clean_options({"category": "neet-jee"}, settings)["category"] == "neet_jee"
    assert webui._clean_options({"category": "JEE"}, settings)["category"] == "neet_jee"   # a pre-merge id
    with pytest.raises(ValueError):
        webui._clean_options({"category": "ias"}, settings)
    import json
    rules = tmp_path / "rules"
    (rules / "neet_jee").mkdir(parents=True)
    (rules / "neet_jee" / "all-subjects.txt").write_text("Put every formula in a box.", encoding="utf-8")
    (rules / "rules.json").write_text(json.dumps(
        {"formats": {"foundation/mathematics": {"solved_examples": True}}}), encoding="utf-8")
    monkeypatch.setattr(categories, "RULES_DIR", rules)
    categories._cache.update(signature=None, rules=None)
    seen = []
    def generate(*args, **kwargs):
        seen.append(build_prompt("english", "sig"))
        return _approved_result(pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=0)
    _clear_cache()
    runs = [RunOptions(category="neet_jee"), RunOptions(category="foundation"),
            # The Studio's Foundation subject dropdown arrives as a fixed subject.
            RunOptions(category="foundation", subject_mode="mathematics")]
    for options in runs:
        sheet = FakeSheet(_grid(_row(2)))
        runner = Runner(sheet, FakeDrive(), options, generate_fn=generate, out=lambda _: None)
        assert runner.run().counts()["done"] == 1
    categories._cache.update(signature=None, rules=None)
    assert "Put every formula in a box." in seen[0]
    assert "CATEGORY: NEET-JEE" in seen[0]
    assert "CATEGORY:" not in seen[1]
    assert "THEORY + SOLVED EXAMPLES" in seen[2] and "THEORY + SOLVED EXAMPLES" not in seen[0]
    assert webui._clean_options({"category": "foundation", "subject": "science"}, settings)["subject"] == "science"


def test_numbered_and_unnumbered_pdfs_do_not_share_a_cache_entry():
    from sheets_bridge.output_cache import key
    args = dict(file_id="source", out_name="notes.pdf", language="sanskrit", subject="sanskrit", signature="")
    assert key(**args) == key(**args, show_page_numbers=True)
    assert key(**args) != key(**args, show_page_numbers=False)


def _clear_cache():
    """Start a batch with no cached notes (the cache is deliberately kept
    after a successful run, which would otherwise skip generation here)."""
    from sheets_bridge import output_cache

    for entry in output_cache.cache_dir().glob("*.json"):
        entry.unlink()


def _run(sheet, drive, calls=None, **opts):
    calls = [] if calls is None else calls
    options = RunOptions(**opts)
    lines: list[str] = []
    runner = Runner(sheet, drive, options, generate_fn=_fake_generate(calls), run_id="run-1", out=lines.append, clock=lambda: "T", final_clock=lambda: "F")
    return runner.run(), calls, lines


def test_happy_path_claims_before_generation_and_uploads_next_to_source():
    sheet = FakeSheet(_grid(_row(2)))
    drive = FakeDrive()
    summary, calls, _ = _run(sheet, drive)

    assert summary.counts()[OUTCOME_DONE] == 1
    # claimed first, then done - each a single Q:R row-range write
    assert _status_labels(sheet) == ["claimed", C.STATUS_DONE]
    assert all(w[0] == 2 and len(w[1]) == 2 for w in sheet.writes)
    assert sheet.writes[0][1] == ["claimed T", ""]
    # R is the review dropdown, not a link
    assert sheet.writes[1][1] == [C.STATUS_DONE, C.REVIEW_PENDING]
    # generation used the mapped values
    assert calls == [{"language": "hindi", "subject": "physics", "signature": "by Rahul Sir"}]
    # the studygram PDF is the only artifact, and it goes to the source's folder
    assert drive.uploads == []
    assert drive.pdf_uploads == [(FOLDER_ID, "L01 भौतिकी Concise Notes.pdf", b"%PDF-1.7 notes")]
    # the new name first, then the name outputs had before the rename
    assert drive.list_calls == [(FOLDER_ID, "L01 भौतिकी Concise Notes.pdf", ""),
                                (FOLDER_ID, "L01 भौतिकी Class Notes - Concise Notes.pdf", "")]
    assert summary.total_cost_inr == 4.5
    # M = Done + P timestamp go with the final control write
    assert sheet.finals == [(2, "F")]
    assert sheet.validations == [len(sheet.grid)]


def test_done_row_skipped_unless_redo_and_redo_respects_existing_output():
    sheet = FakeSheet(_grid(_row(2, "done", final="Done")))
    drive = FakeDrive()
    summary, calls, _ = _run(sheet, drive)
    assert summary.outcomes == [] and calls == [] and sheet.writes == []
    assert summary.skipped_not_eligible == {"M already Done": 1}

    # --redo makes it eligible; the output already exists -> marked done, no generation
    drive.existing[(FOLDER_ID, "L01 भौतिकी Class Notes - Concise Notes.pdf")] = [{"id": "old", "webViewLink": "https://drive.google.com/file/d/old/view"}]
    summary, calls, _ = _run(sheet, drive, redo=True)
    assert summary.counts()[OUTCOME_DONE] == 1 and calls == [] and drive.pdf_uploads == []
    assert sheet.writes == [(2, [C.STATUS_DONE, C.REVIEW_PENDING])]
    assert sheet.finals == [(2, "F")]


def test_a_row_marked_regenerate_gets_fresh_notes_written_over_its_old_pdf():
    """Review Status "Regenerate": new notes (never the cached ones), the old
    PDF overwritten in place so its link stays, and R back to Review."""
    drive = FakeDrive()
    summary, calls, _ = _run(FakeSheet(_grid(_row(2))), drive)        # first notes, now cached
    assert len(calls) == 1 and len(drive.pdf_uploads) == 1
    name = drive.pdf_uploads[0][1]
    drive.existing[(FOLDER_ID, name)] = [{"id": "oldpdf", "mimeType": "application/pdf",
                                          "webViewLink": "https://drive.google.com/file/d/oldpdf/view"}]
    replaced = []

    def update_pdf(file_id, data, **kwargs):
        replaced.append((file_id, data, kwargs))
        return {"id": file_id, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}

    drive.update_pdf = update_pdf

    marked = FakeSheet(_grid(_row(2, C.STATUS_DONE, final="Done", review="Regenerate")))
    summary, _dry_calls, lines = _run(marked, drive, calls, dry_run=True)
    assert any("marked Regenerate -> would make fresh notes and replace the old PDF" in line for line in lines)
    assert marked.writes == [] and len(calls) == 1

    summary, calls, lines = _run(marked, drive, calls)
    assert summary.counts()[OUTCOME_DONE] == 1
    assert len(calls) == 2, "fresh notes, not the copy cached by the first run"
    assert [(file_id, kwargs["name"]) for file_id, _data, kwargs in replaced] == [("oldpdf", name)]
    assert len(drive.pdf_uploads) == 1                               # no second file beside the old one
    assert marked.writes[0][1] == ["claimed T", C.REVIEW_REGENERATE]  # the request stays until it is done
    assert marked.writes[-1][1] == [C.STATUS_DONE, C.REVIEW_PENDING]
    assert summary.outcomes[0].link == "https://drive.google.com/file/d/oldpdf/view"

    # Once back to Review the row is finished again: the next run leaves it alone.
    done = FakeSheet(_grid(_row(2, C.STATUS_DONE, final="Done", review="Review")))
    summary, calls, _ = _run(done, drive, calls)
    assert summary.outcomes == [] and len(calls) == 2


def test_a_failed_regeneration_keeps_the_request_for_the_next_run():
    drive = FakeDrive(fail_download=True)
    marked = FakeSheet(_grid(_row(2, C.STATUS_DONE, review="regenerate")))
    summary, calls, _ = _run(marked, drive)
    assert summary.outcomes[0].outcome == OUTCOME_FAILED and calls == []
    assert marked.writes[-1][1][1] == C.REVIEW_REGENERATE


def test_rows_with_j_not_done_are_never_touched():
    sheet = FakeSheet(_grid(_row(2, bm=""), _row(3, bm="Pending"), _row(4)))
    summary, calls, _ = _run(sheet, FakeDrive())
    assert [o.row for o in summary.outcomes] == [4] and len(calls) == 1
    assert summary.skipped_not_eligible == {"J not Done": 2}


FOLDER_LINK = f"https://drive.google.com/drive/folders/{FOLDER_ID}"


def test_folder_link_resolves_lecture_by_number_and_uploads_into_that_folder():
    sheet = FakeSheet(_grid(_row(2, link=FOLDER_LINK, lecture="L02"), _row(3, link=FOLDER_LINK, lecture="1")))
    drive = FakeDrive()
    summary, calls, lines = _run(sheet, drive)
    assert [o.outcome for o in summary.outcomes] == [OUTCOME_DONE, OUTCOME_DONE] and len(calls) == 2
    assert [u[1] for u in drive.pdf_uploads] == ["L02 भौतिकी Concise Notes.pdf", "L01 भौतिकी Concise Notes.pdf"]
    assert all(u[0] == FOLDER_ID for u in drive.pdf_uploads)
    assert any("(lecture_token)" in line for line in lines)


def test_folder_link_with_unmatched_or_ambiguous_lecture_fails_and_batch_continues():
    drive = FakeDrive()
    # a different lecture that also carries L02 (another subject), not a copy
    drive.children[FOLDER_ID].append(DriveFile("f3", "L02 रसायन Class Notes.pdf", "application/pdf", 9, (FOLDER_ID,), "", ""))
    sheet = FakeSheet(_grid(_row(2, link=FOLDER_LINK, lecture="L09"), _row(3, link=FOLDER_LINK, lecture="L02"), _row(4, link=FOLDER_LINK, lecture="")))
    summary, calls, _ = _run(sheet, drive)
    codes = {o.row: o.code for o in summary.outcomes}
    assert codes == {2: "lecture_not_found", 3: "lecture_ambiguous", 4: "lecture_number_missing"}
    assert calls == [] and drive.uploads == []
    assert sheet.writes[0] == (2, ["failed: lecture_not_found", ""])
    assert sheet.finals == []          # M is never touched on failure


def test_two_copies_of_the_same_lecture_generate_once_from_one_of_them():
    """Row 188: the folder held the same lecture twice (same name, same size,
    same checksum). That used to fail the row as lecture_ambiguous."""
    name = "Cell Cycle and Cell Division 06  Class Notes  Botany By Archana Rathi ma'am.pdf"
    drive = FakeDrive(children={FOLDER_ID: [
        DriveFile("copy-a", name, "application/pdf", 4123480, (FOLDER_ID,), "", "", "abc123", "2026-09-17T07:42:15.000Z"),
        DriveFile("copy-b", name, "application/pdf", 4123480, (FOLDER_ID,), "", "", "abc123", "2026-09-17T07:42:15.000Z"),
    ]})
    sheet = FakeSheet(_grid(_row(2, link=FOLDER_LINK, lecture="6", chapter="Cell Cycle and Cell Division")))
    summary, calls, lines = _run(sheet, drive)
    assert [o.outcome for o in summary.outcomes] == [OUTCOME_DONE] and len(calls) == 1
    assert len(drive.pdf_uploads) == 1
    assert drive.pdf_uploads[0][1] == "Cell Cycle and Cell Division 06  Concise Notes  Botany By Archana Rathi ma'am.pdf"
    assert any("2 identical copies of this lecture in the folder - using one" in line for line in lines)


def test_the_newest_or_highest_version_copy_is_the_one_used():
    from sheets_bridge.lecture_match import choose_lecture_pdf as choose

    old = DriveFile("old", "L04 Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "m1", "2026-09-01T10:00:00Z")
    newer = DriveFile("new", "L04 Class Notes (1).pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "m2", "2026-09-10T10:00:00Z")
    v2 = DriveFile("v2", "L04 Class Notes v2.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "m3", "2026-09-05T10:00:00Z")
    # "(1)" marks a download copy: the plain name wins over it even though it is newer
    assert choose([old, newer], 4).file.id == "old"
    # an explicit version beats both
    assert choose([old, newer, v2], 4).file.id == "v2"
    # same name, different content: the most recently changed upload is used
    later = DriveFile("later", "L04 Class Notes.pdf", "application/pdf", 12, (FOLDER_ID,), "", "", "m4", "2026-09-12T10:00:00Z")
    match = choose([old, later], 4)
    assert match.file.id == "later" and match.note.startswith("2 copies")


def test_a_part_or_language_in_the_name_keeps_files_apart():
    from sheets_bridge.lecture_match import choose_lecture_pdf as choose, lecture_identity

    assert lecture_identity("Optics 05 Part 1.pdf") != lecture_identity("Optics 05 Part 2.pdf")
    assert lecture_identity("Optics 05 (Hindi).pdf") != lecture_identity("Optics 05 (English).pdf")
    assert lecture_identity("Optics 05 Class Notes (2).pdf") == lecture_identity("optics 5 class notes - Copy.PDF")
    with pytest.raises(BridgeError) as info:
        choose([_pdf("L05 Optics Part 1.pdf"), _pdf("L05 Optics Part 2.pdf")], 5)
    assert info.value.code == "lecture_ambiguous"
    # identical content under two unrelated names is still one lecture
    a = DriveFile("a", "L05 Optics.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "same", "")
    b = DriveFile("b", "L05 Light and Mirrors.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "same", "")
    assert "identical copies" in choose([a, b], 5).note


def test_a_resolve_failure_names_the_clashing_files_in_the_log():
    drive = FakeDrive()
    drive.children[FOLDER_ID].append(DriveFile("f3", "L02 रसायन Class Notes.pdf", "application/pdf", 9, (FOLDER_ID,), "", ""))
    sheet = FakeSheet(_grid(_row(2, link=FOLDER_LINK, lecture="L02")))
    summary, calls, lines = _run(sheet, drive)
    assert summary.outcomes[0].code == "lecture_ambiguous" and calls == []
    failure = next(line for line in lines if "resolve failed" in line)
    assert "L02 भौतिकी Class Notes.pdf" in failure and "L02 रसायन Class Notes.pdf" in failure


def test_file_link_pointing_at_a_folder_is_resolved_as_folder():
    sheet = FakeSheet(_grid(_row(2, link=f"https://drive.google.com/open?id={FOLDER_ID}", lecture="L01")))
    summary, calls, _ = _run(sheet, FakeDrive())
    assert summary.outcomes[0].outcome == OUTCOME_DONE and len(calls) == 1


def test_generation_error_is_recorded_with_partial_cost_and_batch_continues():
    sheet = FakeSheet(_grid(_row(2), _row(3)))
    drive = FakeDrive()
    calls: list = []

    def flaky(pdf_bytes, *, language, subject, signature, **extra):
        calls.append(subject)
        if len(calls) == 1:
            exc = RuntimeError("model said something with student content")
            exc.partial_cost_inr = 1.25
            exc.failed_stage = "generate"
            raise exc
        return _approved_result(pdf=b"%PDF ok", notes_json=_notes_json(), cost_inr=2.0)

    lines: list[str] = []
    runner = Runner(sheet, drive, RunOptions(), generate_fn=flaky, run_id="run-1", out=lines.append, clock=lambda: "T")
    summary = runner.run()
    by_row = {o.row: o for o in summary.outcomes}
    assert by_row[2].outcome == OUTCOME_FAILED and by_row[2].code == "generate:RuntimeError" and by_row[2].cost_inr == 1.25
    assert by_row[3].outcome == OUTCOME_DONE
    assert (2, ["failed: generate:RuntimeError", ""]) in sheet.writes
    assert summary.total_cost_inr == 3.25
    assert not any("student content" in line for line in lines)  # message never logged


def test_dry_run_writes_nothing_and_generates_nothing():
    sheet = FakeSheet(_grid(_row(2)), headers_present=False)
    drive = FakeDrive()
    summary, calls, lines = _run(sheet, drive, dry_run=True)
    assert summary.counts()[OUTCOME_PLANNED] == 1
    assert sheet.writes == [] and sheet.header_writes == 0 and calls == [] and drive.uploads == [] and drive.downloads == 0
    assert any("PLAN would generate" in line for line in lines)


def test_live_run_creates_missing_control_headers():
    sheet = FakeSheet(_grid(_row(2)), headers_present=False)
    _run(sheet, FakeDrive())
    assert sheet.header_writes == 1


def test_limit_row_and_cost_cap():
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3), _distinct_row(4)))
    summary, calls, _ = _run(sheet, FakeDrive(), limit=2)
    assert [o.row for o in summary.outcomes] == [2, 3]

    _clear_cache()
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3), _distinct_row(4)))
    summary, calls, _ = _run(sheet, FakeDrive(), row=3)
    assert [o.row for o in summary.outcomes] == [3]

    _clear_cache()
    with pytest.raises(BridgeError) as info:
        _run(FakeSheet(_grid(_row(2))), FakeDrive(), row=9)
    assert info.value.code == "row_not_found"

    # each row costs 4.5; cap 5 -> after row 2 (4.5) continue, after row 3 (9.0) stop
    _clear_cache()
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3), _distinct_row(4)))
    summary, calls, _ = _run(sheet, FakeDrive(), max_cost_inr=5.0)
    outcomes = [(o.row, o.outcome) for o in summary.outcomes]
    assert outcomes == [(2, OUTCOME_DONE), (3, OUTCOME_DONE), (4, OUTCOME_STOPPED)]
    assert len(calls) == 2


def test_download_failure_marks_failed_after_claim():
    sheet = FakeSheet(_grid(_row(2)))
    drive = FakeDrive(fail_download=True)
    summary, calls, _ = _run(sheet, drive)
    assert summary.outcomes[0].outcome == OUTCOME_FAILED and summary.outcomes[0].code == "too_large"
    assert _status_labels(sheet) == ["claimed", "failed: too_large"] and calls == []


def test_signature_override_and_subject_mode():
    sheet = FakeSheet(_grid(_row(2)))
    _, calls, _ = _run(sheet, FakeDrive(), signature_override="Team PW", subject_mode="auto")
    assert calls == [{"language": "hindi", "subject": "auto", "signature": "by Team PW"}]


def test_link_cell_hyperlink_fallback_for_chips_and_rich_links():
    """A Drive smart chip shows the file title as the cell value; the URL is
    only in the cell's hyperlink metadata. Folder links are still refused."""

    class ChipSheet(FakeSheet):
        def __init__(self, grid, url):
            super().__init__(grid)
            self.url = url
            self.lookups: list[tuple[int, int]] = []

        def cell_hyperlink(self, row_number, col_index):
            self.lookups.append((row_number, col_index))
            return self.url

    sheet = ChipSheet(_grid(_row(2, link="L01 Class Notes")), f"https://drive.google.com/file/d/{FILE_ID}/view")
    summary, calls, _ = _run(sheet, FakeDrive())
    assert summary.outcomes[0].outcome == OUTCOME_DONE and len(calls) == 1
    assert sheet.lookups == [(2, C.COL_DRIVE_LINK)]

    sheet = ChipSheet(_grid(_row(2, link="Chapter folder", lecture="L02")), f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    summary, calls, _ = _run(sheet, FakeDrive())
    assert summary.outcomes[0].outcome == OUTCOME_DONE and len(calls) == 1      # chip -> folder -> L02

    sheet = ChipSheet(_grid(_row(2, link="Chapter folder", lecture="L02")), f"https://drive.google.com/drive/folders/{FOLDER_ID}")
    summary, calls, _ = _run(sheet, FakeDrive(children={FOLDER_ID: []}))
    assert summary.outcomes[0].code == "no_pdf_in_folder" and calls == []

    sheet = ChipSheet(_grid(_row(2, link="just text")), "")
    summary, calls, _ = _run(sheet, FakeDrive())
    assert summary.outcomes[0].code == "invalid_link" and calls == []


def test_keyboard_interrupt_marks_row_and_records_partial_cost():
    sheet = FakeSheet(_grid(_row(2), _row(3)))

    def interrupt(pdf_bytes, *, language, subject, signature, **extra):
        exc = KeyboardInterrupt()
        exc.partial_cost_inr = 0.75
        raise exc

    runner = Runner(sheet, FakeDrive(), RunOptions(), generate_fn=interrupt, run_id="run-1", out=lambda _l: None, clock=lambda: "T")
    summary = runner.run()
    assert summary.interrupted
    assert [(o.row, o.outcome, o.code) for o in summary.outcomes] == [(2, OUTCOME_FAILED, "interrupted")]
    assert sheet.writes[-1] == (2, ["failed: interrupted", ""])
    assert summary.total_cost_inr == 0.75
    assert not any(w[0] == 3 for w in sheet.writes)   # row 3 untouched


def test_guard_cell_blocks_formula_injection():
    from sheets_bridge.sheets import guard_cell

    assert guard_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert guard_cell("+1") == "'+1" and guard_cell("-x") == "'-x" and guard_cell("@x") == "'@x"
    assert guard_cell("https://drive.google.com/x") == "https://drive.google.com/x"
    assert guard_cell(4.5) == 4.5 and guard_cell("") == ""


def test_stop_check_halts_between_rows():
    sheet = FakeSheet(_grid(_row(2), _row(3), _row(4)))
    calls: list = []
    stop = {"flag": False}

    def generate(pdf_bytes, *, language, subject, signature, **extra):
        calls.append(subject)
        stop["flag"] = True                    # request stop during row 2
        return _approved_result(pdf=b"%PDF ok", notes_json=_notes_json(), cost_inr=1.0)

    runner = Runner(sheet, FakeDrive(), RunOptions(), generate_fn=generate, run_id="run-1", out=lambda _l: None, clock=lambda: "T", final_clock=lambda: "F", stop_check=lambda: stop["flag"])
    summary = runner.run()
    assert [(o.row, o.outcome) for o in summary.outcomes] == [(2, OUTCOME_DONE), (3, OUTCOME_STOPPED), (4, OUTCOME_STOPPED)]
    assert len(calls) == 1 and summary.interrupted


def test_a_finished_lecture_reads_review_in_both_control_columns():
    """Q says "review" rather than "done" - the file exists but nobody has
    checked it - while M = Done tells the content team it was produced."""
    sheet = FakeSheet(_grid(_row(2)))
    _run(sheet, FakeDrive())
    assert _status_labels(sheet) == ["claimed", "review"]
    assert [w[1][1] for w in sheet.writes] == ["", C.REVIEW_PENDING]
    assert sheet.finals == [(2, "F")]
    assert [w[1][1] for w in sheet.writes] == ["", C.REVIEW_PENDING]


def test_webui_controller_runs_batch_in_background(monkeypatch):
    """The one-button controller: start -> auth -> running -> finished with a summary."""
    import time
    from types import SimpleNamespace

    from sheets_bridge import google_auth, pw_gate, webui

    class FakeAccess:
        def __init__(self, token_provider, email=""):
            self.email = email
            self.checks = 0

        def domain_ok(self):
            return True

        def install_transport(self, model=""):
            return "gemini-2.5-flash"

        def ensure_allowed(self):
            self.checks += 1

    monkeypatch.setattr(google_auth, "token_provider", lambda creds: (lambda: "google-token"))
    monkeypatch.setattr(google_auth, "user_email", lambda creds: "tester@pw.live")
    monkeypatch.setattr(pw_gate, "ProxyAccess", FakeAccess)
    monkeypatch.setattr(pw_gate, "new_task_id", lambda prefix="handwritten-notes": "task-fixed")

    sheet = FakeSheet(_grid(_row(2), _row(3, final="Done")))
    drive = FakeDrive()
    monkeypatch.setattr(google_auth, "load_credentials", lambda *_a, **_k: object())
    monkeypatch.setattr(google_auth, "build_services", lambda _c: (object(), object()))
    import sheets_bridge.sheets as sheets_mod
    import sheets_bridge.drive as drive_mod
    import sheets_bridge.runner as runner_mod
    monkeypatch.setattr(sheets_mod, "SheetClient", lambda *_a, **_k: sheet)
    monkeypatch.setattr(drive_mod, "DriveClient", lambda *_a, **_k: drive)

    calls: list = []
    real_runner = runner_mod.Runner

    def runner_with_fake_generate(*args, **kwargs):
        kwargs["generate_fn"] = _fake_generate(calls)
        kwargs["final_clock"] = lambda: "F"
        return real_runner(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "Runner", runner_with_fake_generate)
    import sheets_bridge.batch as batch_mod

    monkeypatch.setattr(batch_mod, "Runner", runner_with_fake_generate)   # batch bound Runner at import

    settings = SimpleNamespace(client_secret_path="x", token_path="y", spreadsheet_id="sid", gid=0,
                               signature="", max_cost_inr=None, max_input_bytes=10_000_000)
    controller = webui.RunController(settings)
    controller.start(webui._clean_options({"dryRun": False, "limit": 5}, settings))
    with pytest.raises(BridgeError):
        controller.start({})            # only one run at a time
    for _ in range(600):                        # the worker imports the model stack
        if controller.status()["state"] in ("finished", "error"):
            break
        time.sleep(0.02)
    status = controller.status()
    assert status["state"] == "finished", status["error"]
    assert status["account"] == "tester@pw.live"
    assert status["summary"]["counts"] == {"done": 1}
    assert status["summary"]["skipped"] == {"M already Done": 1}
    assert status["summary"]["totalCostInr"] == 4.5
    assert len(calls) == 1 and sheet.finals == [(2, "F")]
    assert any("done:" in line for line in status["log"])


# --------------------------------------------------------------------------- #
# PW proxy onboarding: allowlist gate, task ids, transport translation
# --------------------------------------------------------------------------- #
class GateStub:
    def __init__(self, status="allowed"):
        self.status = status
        self.calls = 0

    def ensure_allowed(self):
        self.calls += 1
        if self.status == "allowed":
            return
        if self.status == "denied":
            raise BridgeError("not_authorized", "email not listed")
        raise BridgeError("allowlist_undecided", "proxy unreachable")


def _run_with_gate(sheet, drive, gate, **opts):
    calls: list = []
    lines: list[str] = []
    runner = Runner(
        sheet, drive, RunOptions(**opts), generate_fn=_fake_generate(calls),
        run_id="run-1", out=lines.append, clock=lambda: "T", final_clock=lambda: "F", access=gate,
    )
    return runner.run(), calls, lines


def test_allowlist_is_checked_before_every_paid_row():
    sheet = FakeSheet(_grid(_row(2), _row(3)))
    gate = GateStub("allowed")
    summary, calls, _ = _run_with_gate(sheet, FakeDrive(), gate)
    assert gate.calls == 2                      # one check per paid row
    assert [o.outcome for o in summary.outcomes] == [OUTCOME_DONE, OUTCOME_DONE]


def test_denied_row_never_claims_generates_or_marks_done():
    sheet = FakeSheet(_grid(_row(2)))
    drive = FakeDrive()
    summary, calls, _ = _run_with_gate(sheet, drive, GateStub("denied"))
    assert summary.outcomes[0].code == "not_authorized"
    assert calls == [] and drive.uploads == [] and drive.downloads == 0
    # the code travels in the status cell now; no "claimed" was written first
    assert _status_labels(sheet) == ["failed: not_authorized"]
    assert sheet.finals == []                              # M untouched


def test_undecidable_allowlist_fails_closed():
    sheet = FakeSheet(_grid(_row(2)))
    summary, calls, _ = _run_with_gate(sheet, FakeDrive(), GateStub("error"))
    assert summary.outcomes[0].code == "allowlist_undecided" and calls == []


def test_one_task_id_per_run_reaches_the_generator(monkeypatch):
    """The proxy writes a raw usage row per provider call and says to combine
    them by task_id, so a whole run - not each lecture - is one task."""
    from sheets_bridge import pw_gate

    issued = iter(["task-A", "task-B"])
    monkeypatch.setattr(pw_gate, "new_task_id", lambda prefix="handwritten-notes": next(issued))
    seen: list = []

    def generate(pdf_bytes, *, language, subject, signature, filename="", task_id="", **extra):
        seen.append((task_id, filename))
        return _approved_result(pdf=b"%PDF ok", notes_json=_notes_json(), cost_inr=1.0, task_id=task_id)

    _clear_cache()
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3)))     # different sources = different cache keys
    Runner(sheet, FakeDrive(), RunOptions(), generate_fn=generate, run_id="r", out=lambda _l: None,
           clock=lambda: "T", final_clock=lambda: "F", access=GateStub()).run()
    assert [t for t, _ in seen] == ["task-A", "task-A"]              # one id for the run
    assert [name for _, name in seen] == ["L02 Lecture 2 Class Notes.pdf", "L03 Lecture 3 Class Notes.pdf"]

    # an explicit id (the batch passes one for the whole button press) wins
    seen.clear()
    _clear_cache()
    Runner(FakeSheet(_grid(_distinct_row(2))), FakeDrive(), RunOptions(), generate_fn=generate,
           run_id="r", out=lambda _l: None, clock=lambda: "T", final_clock=lambda: "F",
           access=GateStub(), task_id="batch-1").run()
    assert [t for t, _ in seen] == ["batch-1"]


def test_transport_translates_request_response_and_errors():
    from sheets_bridge import pw_transport as T

    body = T.build_gemini_request({
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "PROMPT"},
            {"type": "file", "file": {"filename": "l.pdf", "file_data": "data:application/pdf;base64,QUJD"}},
        ]}],
        "temperature": 0.3,
        "max_tokens": 1024,
        "response_format": {"type": "json_schema", "json_schema": {"name": "S", "schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}}},
    })
    assert body["contents"][0]["parts"][0] == {"text": "PROMPT"}
    assert body["contents"][0]["parts"][1]["inline_data"] == {"mime_type": "application/pdf", "data": "QUJD"}
    assert body["generationConfig"]["maxOutputTokens"] == 1024
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["properties"]["a"]["type"] == "string"

    converted = T.gemini_schema({
        "$defs": {"Leaf": {"type": "object", "title": "Leaf", "properties": {"v": {"type": "integer", "default": 0, "minimum": 1}}, "required": ["v"]}},
        "type": "object",
        "properties": {
            "leaf": {"$ref": "#/$defs/Leaf"},
            "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["leaf"],
    })
    assert converted["properties"]["leaf"]["properties"]["v"] == {"type": "integer"}
    assert converted["properties"]["maybe"] == {"type": "string", "nullable": True}
    assert "title" not in converted["properties"]["leaf"]

    shaped = T.openai_shaped({"candidates": [{"content": {"parts": [{"text": "he"}, {"text": "llo"}]}, "finishReason": "MAX_TOKENS"}],
                              "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3, "thoughtsTokenCount": 2, "cachedContentTokenCount": 4}})
    assert shaped.choices[0].message.content == "hello"
    assert shaped.choices[0].finish_reason == "length"
    assert (shaped.usage.prompt_tokens, shaped.usage.completion_tokens) == (10, 5)
    assert shaped.usage.prompt_tokens_details.cached_tokens == 4

    assert T.openai_shaped({"result": {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}}).choices[0].message.content == "x"

    from openai import RateLimitError
    assert isinstance(T._translate_error(Exception("gemini proxy error 429: slow down")), RateLimitError)
    assert "proxy call failed" in str(T._translate_error(Exception("gemini proxy error 500: boom")))


def test_transport_sends_task_metadata_and_never_carries_keys(monkeypatch):
    from sheets_bridge import pw_transport as T

    captured: dict = {}

    def fake_generate(token_provider, **kwargs):
        captured.update(kwargs)
        captured["token"] = token_provider() if callable(token_provider) else token_provider
        return {"candidates": [{"content": {"parts": [{"text": "OK"}]}}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}

    monkeypatch.setattr(T.pw_access, "gemini_generate", fake_generate)
    T.set_task(task_id="task-9", filename="lecture.pdf", input_unit="No. of pages", count=12)
    client = T.ProxyClient(lambda: "google-token", "gemini-2.5-flash")
    response = client.with_options(timeout=30).chat.completions.create(
        model="ignored", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}], max_tokens=8,
    )
    assert response.choices[0].message.content == "OK"
    assert captured["task_id"] == "task-9" and captured["filename"] == "lecture.pdf"
    assert captured["input_unit"] == "No. of pages" and captured["count"] == 12
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["token"] == "google-token"          # the user's Google token, never a provider key


def test_install_points_the_unmodified_generator_at_the_proxy():
    from notesbot import gemini_client

    from sheets_bridge import pw_transport as T

    original = (gemini_client._use_litellm, gemini_client._lite_model,
                gemini_client._lite_client, gemini_client._build_visual_lite_client)
    try:
        model = T.install(lambda: "tok", model="vertex_ai/gemini-2.5-flash")
        assert model == "gemini-2.5-flash"               # vertex_ai/ prefix is a gateway-ism
        assert gemini_client._use_litellm() is True
        assert gemini_client._lite_model() == "gemini-2.5-flash"
        assert isinstance(gemini_client._lite_client(), T.ProxyClient)
        assert isinstance(gemini_client._build_visual_lite_client(), T.ProxyClient)
    finally:
        (gemini_client._use_litellm, gemini_client._lite_model,
         gemini_client._lite_client, gemini_client._build_visual_lite_client) = original


def test_app_name_matches_the_whitelisted_header():
    import pw_access

    assert pw_access.APP_NAME == "Handwritten Notes Automation"
    assert pw_access.PROXY_BASE_URL == "https://pw-apps-proxy.vercel.app"


# --------------------------------------------------------------------------- #
# Chapter-aware lecture matching (real production file names)
# --------------------------------------------------------------------------- #
REAL_FOLDER = [
    "विद्युत चुम्बकीय प्रेरण 06  Class Notes.pdf",
    "मानव स्वास्थ्य तथा रोग 06  Class Notes.pdf",
    "विद्युत चुम्बकीय प्रेरण 05  Class Notes.pdf",
    "Circles 06  Class Notes.pdf",
    "Cell - The Unit of Life 02  Class Notes.pdf",
    "Laws of Motion 02 - Class Notes.pdf",
]


def _real_files():
    return [DriveFile(name, name, "application/pdf", 10, (FOLDER_ID,), "", "") for name in REAL_FOLDER]


@pytest.mark.parametrize(
    "chapter, lecture, expected",
    [
        ("Human Health and Diseases\nमानव स्वास्थ्य तथा रोग", 6, "मानव स्वास्थ्य तथा रोग 06  Class Notes.pdf"),
        ("Electromagnetic Induction\nविद्युत चुम्बकीय प्रेरण", 6, "विद्युत चुम्बकीय प्रेरण 06  Class Notes.pdf"),
        ("Electromagnetic Induction\nविद्युत चुम्बकीय प्रेरण", 5, "विद्युत चुम्बकीय प्रेरण 05  Class Notes.pdf"),
        ("Circles", 6, "Circles 06  Class Notes.pdf"),
        ("Cell - The Unit of Life", 2, "Cell - The Unit of Life 02  Class Notes.pdf"),
        ("Laws of Motion", 2, "Laws of Motion 02 - Class Notes.pdf"),
    ],
)
def test_chapter_disambiguates_lectures_sharing_a_number(chapter, lecture, expected):
    """Three files carry '06'; only the row's Chapter (column G) tells them apart."""
    match = choose_lecture_pdf(_real_files(), lecture, chapter=chapter)
    assert match.file.name == expected and match.how == "chapter_number"


def test_number_alone_is_ambiguous_without_the_chapter():
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(_real_files(), 6)
    assert info.value.code == "lecture_ambiguous"


def _sst_folder():
    """The Pre Foundation class 7 social-science folder, as it is in Drive."""
    names = [
        "Banks and the Magic of Finance.pdf", "Class7thcivicsupdated.pdf", "Class7thgeography.pdf",
        "Class7thhistory.pdf", "Empires and Kingdoms: 6th to 10th Centuries.pdf", "India and Her Neighbours.pdf",
        "India, a Home to Many.pdf", "The State, the Government, and You.pdf", "The Story of Indian Farming.pdf",
        "Turning Tides: 11th and 12th Centuries",
    ]
    return [_pdf(name, fid=f"sst-{i}") for i, name in enumerate(names)]


def test_the_closest_chapter_name_wins_over_a_weaker_match():
    """Row 48: the exact file scored 1.00, "Empires and Kingdoms ..." shared a
    few filler words, and the row failed as if two files matched equally."""
    match = choose_lecture_pdf(_sst_folder(), None, chapter="Turning Tides: 11th and 12th Centuries")
    assert match.file.name == "Turning Tides: 11th and 12th Centuries" and match.how == "chapter_number"
    # the file as it was named before, with a typo, still wins
    typo = [f for f in _sst_folder() if not f.name.startswith("Turning")] + [_pdf("Turning Tides: 11thand 12th Centuries.pdf")]
    assert choose_lecture_pdf(typo, None, chapter="Turning Tides: 11th and 12th Centuries").file.name.startswith("Turning Tides")


def test_filler_words_and_ordinals_no_longer_make_unrelated_titles_match():
    from sheets_bridge.lecture_match import CHAPTER_MATCH_THRESHOLD, chapter_score, chapter_variants

    turning = chapter_variants("Turning Tides: 11th and 12th Centuries")
    assert chapter_score("Empires and Kingdoms: 6th to 10th Centuries.pdf", turning) < CHAPTER_MATCH_THRESHOLD
    # a one-letter spelling difference in a long word still matches
    assert chapter_score("India and Her Neighbors.pdf", chapter_variants("India and Her Neighbours")) >= 0.8


def test_every_chapter_in_the_sst_folder_resolves_to_its_own_file():
    folder = _sst_folder()
    for chapter in ("The Story of Indian Farming", "India and Her Neighbours", "Empires and Kingdoms: 6th to 10th Centuries",
                    "India, a Home to Many", "The State, the Government, and You", "Banks and the Magic of Finance"):
        assert choose_lecture_pdf(folder, None, chapter=chapter).file.name.startswith(chapter.split(":")[0]), chapter


def test_a_chapter_missing_from_the_folder_names_the_closest_file_and_is_never_guessed():
    """Row 51: no file is this chapter. Using the closest name would upload
    another chapter's notes and mark the row done."""
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(_sst_folder(), None, chapter="Infrastructure: Engine of India’s Development")
    assert info.value.code == "chapter_not_found"
    assert "closest:" in info.value.detail


def test_a_lecture_number_in_the_sheet_does_not_block_a_chapter_named_file():
    """The file names carry no lecture number; column H does."""
    match = choose_lecture_pdf(_sst_folder(), 3, chapter="The Story of Indian Farming")
    assert match.file.name == "The Story of Indian Farming.pdf"
    assert "no lecture number" in match.note
    # a number that belongs to the chapter title is not a lecture number
    match = choose_lecture_pdf(_sst_folder(), 2, chapter="Empires and Kingdoms: 6th to 10th Centuries")
    assert match.file.name.startswith("Empires and Kingdoms")


def test_versions_of_the_same_chapter_and_lecture_use_the_newest():
    older = DriveFile("en", "Circles 06 Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "m1", "2026-09-01T10:00:00Z")
    newer = DriveFile("hi", "Circles 06 Class Notes Hindi.pdf", "application/pdf", 10, (FOLDER_ID,), "", "", "m2", "2026-09-09T10:00:00Z")
    match = choose_lecture_pdf([older, newer], 6, chapter="Circles")
    assert match.file.id == "hi" and "using the newest" in match.note


def test_different_lectures_of_one_chapter_still_need_the_lecture_number():
    files = [_pdf("Circles 01 Class Notes.pdf"), _pdf("Circles 02 Class Notes.pdf")]
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(files, None, chapter="Circles")
    assert info.value.code == "lecture_number_missing" and "column H" in info.value.detail
    assert choose_lecture_pdf(files, 2, chapter="Circles").file.name == "Circles 02 Class Notes.pdf"
    # part 1 and part 2 of one lecture are different files, not versions
    parts = [_pdf("Circles 06 Part 1.pdf"), _pdf("Circles 06 Part 2.pdf")]
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(parts, 6, chapter="Circles")
    assert info.value.code == "lecture_ambiguous"


def test_two_sub_chapters_with_the_same_lecture_number_are_never_confused():
    """The "Equilibrium" folder holds "Ionic Equilibrium 01" and "Chemical
    Equilibrium 01": both contain the chapter word, both carry lecture 1, and
    they are different lectures. Picking the newer one would be a guess."""
    files = [_pdf("Ionic Equilibrium 01 - Class Notes.pdf"), _pdf("Chemical Equilibrium 01 Class Notes.pdf")]
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(files, 1, chapter="Equilibrium")
    assert info.value.code == "lecture_ambiguous"
    assert "Ionic Equilibrium 01" in info.value.detail and "Chemical Equilibrium 01" in info.value.detail
    # naming the sub-chapter in the sheet resolves it
    assert choose_lecture_pdf(files, 1, chapter="Ionic Equilibrium").file.name.startswith("Ionic")


def test_a_one_letter_chapter_cell_matches_nothing_instead_of_every_name_with_that_letter():
    files = [_pdf("Tales by Dots and Lines (Pages)"), _pdf("Fractions in Disguise(Pages)"), _pdf("Area(Pages)")]
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(files, None, chapter="d")
    assert info.value.code == "chapter_not_found"


def test_a_file_of_another_lecture_of_the_chapter_is_never_used_for_a_missing_one():
    files = [_pdf("Circles 05 Class Notes.pdf"), _pdf("Circles 06 Class Notes.pdf")]
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(files, 7, chapter="Circles")
    assert info.value.code == "lecture_not_found"


def test_chapter_present_but_lecture_missing_never_borrows_another_chapter():
    with pytest.raises(BridgeError) as info:
        choose_lecture_pdf(_real_files(), 5, chapter="Circles")     # only Circles 06 exists
    # the other chapter's lecture 05 is never handed back; the message says why
    assert info.value.code == "lecture_not_found"
    assert "Circles 06" in info.value.detail and "not lecture 5" in info.value.detail

    with pytest.raises(BridgeError) as info:
        # English chapter, Hindi-only file names, no lecture 9 anywhere: nothing
        # in the folder is this chapter, and the message names the closest file
        choose_lecture_pdf(_real_files(), 9, chapter="Electromagnetic Induction")
    assert info.value.code == "chapter_not_found" and "closest:" in info.value.detail


def test_chapter_helpers():
    from sheets_bridge.lecture_match import chapter_score, chapter_variants, file_stem

    assert chapter_variants("Human Health and Diseases\nमानव स्वास्थ्य तथा रोग") == [
        "human health and diseases", "मानव स्वास्थ्य तथा रोग",
    ]
    assert file_stem("Cell - The Unit of Life 02  Class Notes.pdf") == "cell the unit of life"
    assert chapter_score("Circles 06  Class Notes.pdf", ["circles"]) == 1.0
    assert chapter_score("Circles 06  Class Notes.pdf", ["laws of motion"]) == 0.0


def test_runner_passes_the_row_chapter_to_the_matcher():
    """Row 2 of the live sheet: Zoology L6 in a folder with three '06' files."""
    drive = FakeDrive(children={FOLDER_ID: _real_files()})
    sheet = FakeSheet(_grid(_row(2, link=FOLDER_LINK, lecture="6",
                                 chapter="Human Health and Diseases\nमानव स्वास्थ्य तथा रोग")))
    summary, calls, _ = _run(sheet, drive)
    assert summary.outcomes[0].outcome == OUTCOME_DONE
    assert drive.pdf_uploads[0][1] == "मानव स्वास्थ्य तथा रोग 06 Concise Notes.pdf"


# --------------------------------------------------------------------------- #
# Cost is never lost, and a failed upload never costs a second generation
# --------------------------------------------------------------------------- #
def test_upload_failure_keeps_the_cost_already_spent():
    class BadUpload(FakeDrive):
        def upload_pdf(self, folder_id, name, data, drive_id=""):
            raise BridgeError("upload:ConnectionAbortedError")

    sheet = FakeSheet(_grid(_row(2)))
    summary, calls, _ = _run(sheet, BadUpload())
    outcome = summary.outcomes[0]
    assert outcome.outcome == OUTCOME_FAILED and outcome.code == "upload:ConnectionAbortedError"
    assert outcome.cost_inr == 4.5                      # generation was paid for
    assert summary.total_cost_inr == 4.5                # still counted in the run total


def test_retry_after_a_failed_upload_reuses_the_generated_notes():
    class BadUpload(FakeDrive):
        def upload_pdf(self, folder_id, name, data, drive_id=""):
            raise BridgeError("upload:ConnectionAbortedError")

    sheet = FakeSheet(_grid(_row(2)))
    _run(sheet, BadUpload())                            # attempt 1: generated, upload failed

    sheet2 = FakeSheet(_grid(_row(2, "failed")))
    drive2 = FakeDrive()
    summary, calls, lines = _run(sheet2, drive2)        # attempt 2: same row
    assert summary.outcomes[0].outcome == OUTCOME_DONE
    assert calls == []                                  # no second generation = no second charge
    assert drive2.pdf_uploads[0][2] == b"%PDF-1.7 notes"  # exact previously approved bytes
    assert any("reusing the notes generated by an earlier attempt" in line for line in lines)
    assert summary.total_cost_inr == 0.0


def test_generated_notes_are_kept_after_a_successful_upload():
    """Re-rendering or reviewing a lecture must never cost a second generation."""
    from sheets_bridge import output_cache

    sheet = FakeSheet(_grid(_row(2)))
    _run(sheet, FakeDrive())
    kept = list(output_cache.cache_dir().glob("*.json"))
    assert len(kept) == 2 and all(item.read_bytes().startswith(b"{") for item in kept)
    assert any(item.name.endswith(".final.json") for item in kept)


def test_proxy_reported_cost_wins_over_the_local_estimate(monkeypatch):
    from sheets_bridge import pw_transport as T

    def fake_generate(token_provider, **kwargs):
        return {
            "ok": True,
            "cost_inr": 7.25,
            "result": {"candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                       "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10}},
        }

    monkeypatch.setattr(T.pw_access, "gemini_generate", fake_generate)
    T.set_task(task_id="t", filename="f.pdf")
    client = T.ProxyClient(lambda: "tok", "gemini-2.5-flash")
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}])
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": [{"type": "text", "text": "y"}]}])
    assert T.task_cost_inr() == 14.5                    # summed across the task's calls

    from sheets_bridge.generator import _cost
    assert _cost({"inr": 0.42}) == 14.5                 # proxy figure beats the estimate
    T.set_task(task_id="t2")                            # a new task starts from zero
    assert _cost({"inr": 0.42}) == 0.42


def test_gateway_budget_error_stops_run_and_preserves_regeneration(monkeypatch):
    from notesbot.errors import ModelBudgetError

    calls = []
    def exhausted(*args, **kwargs):
        calls.append(kwargs)
        error = ModelBudgetError()
        error.partial_cost_inr = 1.25
        raise error

    sheet = FakeSheet(_grid(_distinct_row(2, review="Regenerate"),
                            _distinct_row(3, review="Regenerate")))
    drive = FakeDrive()
    logs = []
    summary = Runner(sheet, drive, RunOptions(), generate_fn=exhausted, out=logs.append).run()

    assert len(calls) == drive.downloads == 1
    assert not drive.pdf_uploads and not sheet.finals
    assert [(o.row, o.outcome, o.code) for o in summary.outcomes] == [
        (2, OUTCOME_FAILED, "generate:model_budget_exceeded"),
        (3, OUTCOME_STOPPED, "model_budget_exceeded"),
    ]
    assert summary.interrupted and summary.total_cost_inr == 1.25
    assert all(number == 2 and values[1] == "Regenerate" for number, values in sheet.writes)
    assert any("administrator to renew the budget" in line for line in logs)


# --------------------------------------------------------------------------- #
# Drive transfers survive a dropped connection
# --------------------------------------------------------------------------- #
def test_upload_repeats_a_transfer_aborted_by_the_connection(monkeypatch):
    from sheets_bridge.drive import DriveClient

    monkeypatch.setattr("time.sleep", lambda _s: None)
    attempts = {"n": 0}

    class Files:
        def create(self, **kwargs):
            attempts["n"] += 1

            class Request:
                def execute(self, num_retries=0):
                    if attempts["n"] == 1:
                        raise ConnectionAbortedError(10053, "An established connection was aborted")
                    return {"id": "new", "name": kwargs["body"]["name"], "webViewLink": "https://drive/new"}

            return Request()

        def list(self, **kwargs):
            class Request:
                def execute(self, num_retries=0):
                    return {"files": []}

            return Request()

    class Service:
        def files(self):
            return Files()

    created = DriveClient(Service()).upload_pdf(FOLDER_ID, "out.pdf", b"%PDF-small")
    assert created["id"] == "new" and attempts["n"] == 2


def test_upload_retry_returns_the_file_a_previous_attempt_already_created(monkeypatch):
    from sheets_bridge.drive import DriveClient

    monkeypatch.setattr("time.sleep", lambda _s: None)

    class Files:
        def create(self, **kwargs):
            class Request:
                def execute(self, num_retries=0):
                    raise ConnectionAbortedError(10053, "aborted")

            return Request()

        def list(self, **kwargs):
            class Request:
                def execute(self, num_retries=0):
                    return {"files": [{"id": "already", "name": "out.pdf", "webViewLink": "https://drive/already"}]}

            return Request()

    class Service:
        def files(self):
            return Files()

    created = DriveClient(Service()).upload_pdf(FOLDER_ID, "out.pdf", b"%PDF-small")
    assert created["id"] == "already"          # no duplicate is created


def test_transient_classification():
    from sheets_bridge.drive import _is_transient

    assert _is_transient(ConnectionAbortedError(10053, "aborted"))
    assert _is_transient(TimeoutError())
    assert not _is_transient(ValueError("nope"))


# --------------------------------------------------------------------------- #
# Local Blob stand-in: the Studio's own flow running without Vercel
# --------------------------------------------------------------------------- #
def _store(tmp_path):
    from sheets_bridge.local_blob import LocalBlobStore

    return LocalBlobStore("store_localdevstore01", root=tmp_path / "blob")


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def test_local_store_urls_keep_the_blob_shape(tmp_path):
    store = _store(tmp_path)
    assert store.store_host == "localdevstore01.private.blob.vercel-storage.com"
    ref = store.put_bytes("shortnotes/v1/inputs/" + "a" * 32 + "/" + "b" * 32 + ".pdf", PDF_BYTES, "application/pdf")
    assert ref.url.startswith("https://localdevstore01.private.blob.vercel-storage.com/shortnotes/v1/inputs/")
    assert store.get_pdf(ref.url, 1_000_000) == PDF_BYTES


def test_local_store_refuses_paths_outside_the_app_prefix(tmp_path):
    store = _store(tmp_path)
    for bad in ("other/v1/x.pdf", "shortnotes/v1/../../escape.pdf", "shortnotes/v1//x.pdf"):
        with pytest.raises(ValueError):
            store.put_bytes(bad, PDF_BYTES, "application/pdf")


def test_local_store_is_create_only_unless_told_otherwise(tmp_path):
    from notesbot.blob_store import BlobConflictError

    store = _store(tmp_path)
    path = "shortnotes/v1/inputs/" + "a" * 32 + "/" + "b" * 32 + ".pdf"
    store.put_bytes(path, PDF_BYTES, "application/pdf")
    with pytest.raises(BlobConflictError):
        store.put_bytes(path, PDF_BYTES, "application/pdf")            # immutable input
    store.put_bytes(path, PDF_BYTES + b"x", "application/pdf", True)   # explicit overwrite is fine


def test_local_store_if_match_is_a_real_compare_and_set(tmp_path):
    from notesbot.blob_store import BlobConflictError

    store = _store(tmp_path)
    path = "shortnotes/v1/jobs/" + "a" * 32 + "/" + "b" * 32 + ".json"
    first = store.put_json(path, {"state": "queued"})
    second = store.put_json(path, {"state": "running"}, if_match=first.etag)
    assert store.get_json(store.url_for(path), 10_000).value == {"state": "running"}
    with pytest.raises(BlobConflictError):
        store.put_json(path, {"state": "complete"}, if_match=first.etag)   # stale version loses
    assert store.get_json(store.url_for(path), 10_000).etag == second.etag


def test_local_store_missing_and_type_mismatches(tmp_path):
    from notesbot.blob_store import BlobNotFoundError

    store = _store(tmp_path)
    missing = store.url_for("shortnotes/v1/outputs/" + "a" * 32 + "/" + "b" * 32 + ".pdf")
    with pytest.raises(BlobNotFoundError):
        store.get_bytes(missing, 1000)
    json_path = "shortnotes/v1/jobs/" + "a" * 32 + "/" + "c" * 32 + ".json"
    store.put_json(json_path, {"a": 1})
    with pytest.raises(ValueError):
        store.get_pdf(store.url_for(json_path), 1000)          # JSON is not a PDF
    with pytest.raises(ValueError):
        store.get_json(store.url_for(json_path).replace(store.store_host, "evil.example"), 1000)


def test_local_store_delete_is_bounded_and_forgiving(tmp_path):
    from notesbot.blob_store import BlobNotFoundError

    store = _store(tmp_path)
    path = "shortnotes/v1/outputs/" + "a" * 32 + "/" + "b" * 32 + ".pdf"
    ref = store.put_bytes(path, PDF_BYTES, "application/pdf")
    store.delete([ref.url, "https://evil.example/x", "not a url"])   # bad entries are ignored
    with pytest.raises(BlobNotFoundError):
        store.get_bytes(ref.url, 1000)


def test_local_store_size_ceiling(tmp_path):
    store = _store(tmp_path)
    ref = store.put_bytes("shortnotes/v1/inputs/" + "a" * 32 + "/" + "d" * 32 + ".pdf", PDF_BYTES, "application/pdf")
    with pytest.raises(ValueError):
        store.get_bytes(ref.url, len(PDF_BYTES) - 1)


# --------------------------------------------------------------------------- #
# Review document: inline markup, structure, logo and watermark
# --------------------------------------------------------------------------- #
def test_markup_splits_into_text_and_maths_segments():
    from sheets_bridge.inline_text import bold_runs, plain_text, segments

    # exactly the shapes seen in the live Hindi notes
    assert plain_text(r"रोगजनक **मृत या कमजोर** रूप") == "रोगजनक मृत या कमजोर रूप"
    assert plain_text(r"$IgE \uparrow\uparrow\uparrow$") == "IgE ↑↑↑"
    assert plain_text(r"$\theta = 90^\circ$") == "θ = 90°"
    assert plain_text(r"$H_2SO_4$") == "H₂SO₄"
    assert plain_text(r"$v \geq 10\,m/s$") == "v ≥ 10 m/s"

    # maths becomes its own segment so it can be drawn as a real equation
    assert segments(r"**यीस्ट** से $Hepatitis\,B$ टीका") == [
        ("text", "**यीस्ट** से "), ("inline", r"Hepatitis\,B"), ("text", " टीका"),
    ]
    assert bold_runs("**यीस्ट** से ") == [("यीस्ट", True), (" से ", False)]
    assert segments("") == [] and plain_text(None) == ""


def test_plain_text_formulas_are_rescued_before_rendering():
    """The kit's repair pass catches maths the model wrote as prose."""
    from sheets_bridge.inline_text import segments

    kinds = dict((payload, kind) for kind, payload in segments("टीकाकरण से H2O और CO2 बनता है"))
    assert kinds.get("H_2O") == "inline" and kinds.get("CO_2") == "inline"
    # Hindi prose is left alone
    assert segments("प्रतिरक्षी निर्माण होता है") == [("text", "प्रतिरक्षी निर्माण होता है")]


def test_latex_helpers_never_leave_raw_commands():
    from sheets_bridge.inline_text import latex_to_text

    assert "\\" not in latex_to_text(r"\alpha + \beta \rightarrow \gamma")
    assert latex_to_text(r"\alpha + \beta \rightarrow \gamma") == "α + β → γ"


def _sample_notes():
    from notesbot.prompts import Branch, Compare, MindMap, Section, ShortNotes, Table, WhereItem

    return ShortNotes(
        lecture_no="Lecture 6", subject="Zoology", title="Human Health",
        audience="NEET", quote="Prevention is better than cure",
        sections=[Section(
            heading="Vaccines",
            points=[r"**Killed** pathogen entry", r"$IgE \uparrow$ rises"],
            tricks=[r"**TAP** = Typhoid, Amoebiasis, Pneumonia"],
            table=Table(headers=["Generation", "Feature"], rows=[["1st", r"**killed** form"]]),
            flow=["Entry", "Multiplication"],
            where=[WhereItem(symbol=r"$IgE$", meaning=r"**specific** antibody")],
            key_terms=[r"$IgE$", "antigen"],
            compare=Compare(left_title="Innate", left_points=["Non-specific"],
                            right_title="Acquired", right_points=[r"**Memory**"]),
            notes=["Vaccines induce memory cells"],
        )],
        mindmap=MindMap(center="Health", branches=[Branch(label="Vaccines", children=[r"$Hepatitis\,B$"])]),
    )


def test_review_docx_carries_every_part_of_the_notes(tmp_path):
    import io

    from docx import Document

    from sheets_bridge.notes_doc import build_review_docx, output_doc_name

    data = build_review_docx(_sample_notes(), signature="by Dr. A", source_name="L06 Class Notes.pdf")
    assert data.startswith(b"PK")                       # a real .docx package
    document = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in document.paragraphs)

    assert "Human Health" in text and "by Dr. A" in text
    assert "L06 Class Notes.pdf" in text                # the reviewer sees the source
    assert "Review copy" in text
    assert "Killed pathogen entry" in text and "**" not in text      # markup resolved
    assert "$" not in text                                           # no raw LaTeX left
    assert "Vaccines" in text and "Mind map" in text
    # the formulas became native Word equations rather than characters
    assert "oMath" in document.element.xml
    assert len(document.tables) == 2                     # the table and the comparison
    assert output_doc_name("L06 Class Notes.pdf") == "L06 Class Notes - Concise Notes (Review)"


def test_review_docx_has_the_pw_logo_and_a_faded_watermark():
    import io

    from docx import Document

    from sheets_bridge.notes_doc import LOGO_PATH, build_review_docx, faded_logo

    assert LOGO_PATH.is_file(), "the PW logo asset must ship with the app"
    document = Document(io.BytesIO(build_review_docx(_sample_notes(), signature="by Dr. A")))
    header_xml = document.sections[0].header._element.xml
    assert "PWWatermark" in header_xml                   # watermark shape behind the text
    assert "imagedata" in header_xml and "blip" in header_xml   # watermark + header logo

    # the fade is baked into the pixels: Google drops Word's washout attributes
    from PIL import Image

    faded = Image.open(faded_logo(LOGO_PATH, 0.1))
    original = Image.open(LOGO_PATH).convert("RGBA")
    assert faded.mode == "RGB"
    darkest = min(sum(pixel) for pixel in faded.convert("RGB").getcolors(maxcolors=1 << 24) and
                  [color for _count, color in faded.convert("RGB").getcolors(maxcolors=1 << 24)])
    assert darkest > 3 * 200, "watermark must be pale enough to read text through"
    assert faded.size == original.size


def test_bold_stays_bold_and_maths_becomes_a_word_equation():
    import io

    from docx import Document

    from sheets_bridge.notes_doc import build_review_docx

    document = Document(io.BytesIO(build_review_docx(_sample_notes(), signature="by Dr. A")))
    runs = [run for paragraph in document.paragraphs for run in paragraph.runs]
    assert any(run.text == "Killed" and run.bold for run in runs)          # **bold** -> bold run

    xml = document.element.xml
    assert xml.count("oMath") >= 2                                          # IgE ↑ and Hepatitis B
    assert "Cambria Math" in xml                                            # equations use the maths face


# --------------------------------------------------------------------------- #
# Coverage: the whole lecture must reach the notes
# --------------------------------------------------------------------------- #
def _pdf_with_pages(count: int) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for index in range(count):
        pdf.add_page()
        pdf.set_font("Helvetica", "", 14)
        pdf.cell(0, 10, f"Slide {index + 1}")
    return bytes(pdf.output())


def _pdf_with_text_pages(texts: list[str]) -> bytes:
    """One page per entry, with that text drawn in a Devanagari-capable face."""
    import os

    from fpdf import FPDF

    pdf = FPDF()
    font = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts", "Kalam-Regular.ttf")
    pdf.add_font("Kalam", "", font)
    for text in texts:
        pdf.add_page()
        pdf.set_font("Kalam", "", 14)
        pdf.cell(0, 10, text)
    return bytes(pdf.output())


def test_lecture_is_split_into_page_groups():
    from sheets_bridge.coverage import split_pages

    source = _pdf_with_pages(21)
    groups = split_pages(source, 4)
    assert [(g.first_page, g.last_page) for g in groups] == [
        (1, 4), (5, 8), (9, 12), (13, 16), (17, 20), (21, 21),
    ]
    assert all(g.pdf_bytes.startswith(b"%PDF") for g in groups)
    assert [g.page_count for g in groups] == [4, 4, 4, 4, 4, 1]

    # a short lecture is passed through untouched
    short = _pdf_with_pages(3)
    single = split_pages(short, 4)
    assert len(single) == 1 and single[0].pdf_bytes is short


def test_thin_answers_are_recognised():
    from types import SimpleNamespace

    from sheets_bridge.coverage import content_size, is_thin

    def section(points):
        return SimpleNamespace(heading="H", points=points, tricks=[], notes=[], table=None)

    full = SimpleNamespace(sections=[section([f"point {i}" for i in range(8)])])
    thin = SimpleNamespace(sections=[section(["only one"])])
    empty = SimpleNamespace(sections=[])

    assert not is_thin(full, page_count=4)
    assert is_thin(thin, page_count=4)          # 1 point for 4 pages
    assert is_thin(empty, page_count=1)
    assert content_size(full) > content_size(thin) > content_size(empty)


def test_generate_complete_notes_retries_a_thin_group_and_merges(monkeypatch):
    """Every page group reaches the model, and a thin answer is asked again."""
    from types import SimpleNamespace

    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import coverage

    calls: list[int] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        from pypdf import PdfReader
        import io

        pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
        calls.append(pages)
        # the second group answers thinly the first time it is asked
        thin = len(calls) == 2
        points = ["p"] if thin else [f"point {i}" for i in range(6)]
        return ShortNotes(lecture_no="", subject="Physics", title=f"Part {len(calls)}",
                          sections=[Section(heading=f"H{len(calls)}", points=points)])

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")   # these assert call ORDER
    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(
        _pdf_with_pages(9), language="english", subject_hint="physics",   # English fake content
        group_size=4, log=lines.append,
    )
    assert calls == [4, 4, 4, 1]                 # 3 groups + one retry of group 2
    assert len(merged.sections) == 3             # one kept section per group
    assert merged.title == "Part 1"              # metadata from the first group
    assert any("thin answer" in line for line in lines)
    assert any("merged: 3 section" in line for line in lines)


def test_pages_per_call_is_configurable(monkeypatch):
    from sheets_bridge.coverage import DEFAULT_PAGES_PER_CALL, pages_per_call

    monkeypatch.delenv("SHEETS_BRIDGE_PAGES_PER_CALL", raising=False)
    assert pages_per_call() == DEFAULT_PAGES_PER_CALL
    monkeypatch.setenv("SHEETS_BRIDGE_PAGES_PER_CALL", "6")
    assert pages_per_call() == 6
    monkeypatch.setenv("SHEETS_BRIDGE_PAGES_PER_CALL", "nonsense")
    assert pages_per_call() == DEFAULT_PAGES_PER_CALL
    monkeypatch.setenv("SHEETS_BRIDGE_PAGES_PER_CALL", "0")
    assert pages_per_call() == 1                 # never zero


def test_a_group_without_study_content_never_fails_the_lecture(monkeypatch):
    """Cover / 'Thank you' slides yield nothing; the lecture must still finish."""
    from notesbot.errors import UserError
    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import coverage

    calls: list[int] = []

    from pypdf import PdfReader
    import io as _io

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        calls.append(len(calls) + 1)
        # A real cover slide yields nothing however the pages are sliced, so
        # this fails on CONTENT, not on a call counter - otherwise the
        # split-and-retry would "rescue" it and the skip would never happen.
        if "Slide 1" in PdfReader(_io.BytesIO(pdf_bytes)).pages[0].extract_text():
            raise UserError("Gemini se complete theory notes nahi mile.")
        return ShortNotes(lecture_no="", subject="Zoology", title="Lecture",
                          sections=[Section(heading=f"H{len(calls)}", points=["a", "b", "c", "d", "e", "f"])])

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")   # these assert call ORDER
    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(
        _pdf_with_pages(8), language="hindi", subject_hint="biology", group_size=4, log=lines.append)
    assert len(merged.sections) == 1              # the second group's content survives
    assert any("no study content" in line for line in lines)
    # An empty answer is a verdict: ONE look at the cover group (an edge of
    # the lecture, so it is never sliced smaller), then the real group
    # answers first time -> 1+1. (It used to be 12 calls, then 6.)
    assert len(calls) == 2
    assert any("no study content found (1 look(s))" in line for line in lines)
    assert any("pages 1-4: no study content - skipped" in line for line in lines)
    assert not any("smaller call(s)" in line for line in lines)


def test_a_lecture_that_yields_nothing_still_reports_the_failure(monkeypatch):
    from notesbot.errors import UserError

    from sheets_bridge import coverage

    def always_fails(pdf_bytes, instruction, language, subject_hint):
        raise UserError("Gemini se complete theory notes nahi mile.")

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", always_fails)
    with pytest.raises(UserError):
        coverage.generate_complete_notes(_pdf_with_pages(8), language="hindi", subject_hint="biology", group_size=4)


def test_a_middle_group_without_study_content_is_skipped_and_the_lecture_ships(monkeypatch):
    """A question-only stretch in the middle has nothing for concise notes.
    The pages around it still make the lecture; nothing fails."""
    from notesbot.errors import UserError
    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)

    def flaky(pdf_bytes, instruction, language, subject_hint):
        from pypdf import PdfReader
        import io as _io

        # pages 5-6 never answer; 7-8 do
        pages = PdfReader(_io.BytesIO(pdf_bytes)).pages
        if "Slide 5" in pages[0].extract_text():
            raise UserError("Gemini se complete theory notes nahi mile.")
        return ShortNotes(lecture_no="", subject="Zoology", title="Lecture",
                          sections=[Section(heading="H", points=["a", "b", "c", "d", "e", "f"])])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", flaky)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(12), language="hindi",
                                              subject_hint="biology", group_size=4, log=lines.append)
    assert merged.sections                                    # the lecture shipped
    # a MIDDLE group is sliced once, and the half that answers is kept
    assert any("pages 5-8: failed as one group - retrying as 2 smaller call(s)" in line for line in lines)
    assert any("pages 5-8: recovered 1 of 2 smaller call(s)" in line for line in lines)
    assert not any("incomplete" in line for line in lines)


def test_duplicate_topics_from_overlapping_groups_are_folded_together():
    """Adjacent page groups repeat a topic; every point must survive once."""
    from types import SimpleNamespace

    from sheets_bridge.coverage import dedupe_sections, heading_key

    def section(heading, points, **extra):
        return SimpleNamespace(heading=heading, points=list(points), tricks=[], notes=[],
                               key_terms=[], table=None, compare=None, graph=None, **extra)

    sections = [
        section("ऐलर्जी एवं ऐलर्जन", ["एलर्जी क्या है", "एलर्जन के उदाहरण"]),
        section("स्वप्रतिरक्षा रोग (Autoimmune Disorder)", ["शरीर स्वयं पर हमला"]),
        section("ऐलर्जी एवं ऐलर्जन (Allergy and Allergens)", ["एलर्जन के उदाहरण", "Mast cells"]),
    ]
    kept = dedupe_sections(sections)
    assert [s.heading for s in kept] == ["ऐलर्जी एवं ऐलर्जन", "स्वप्रतिरक्षा रोग (Autoimmune Disorder)"]
    assert kept[0].points == ["एलर्जी क्या है", "एलर्जन के उदाहरण", "Mast cells"]   # no loss, no repeat

    # unrelated headings are never folded
    assert not (heading_key("ऐलर्जी एवं ऐलर्जन") & heading_key("स्वप्रतिरक्षा रोग"))
    assert len(dedupe_sections([section("टीके की पीढ़ियाँ", ["a"]), section("प्रतिरक्षा का वर्गीकरण", ["b"])])) == 2


def test_hindi_lectures_ask_for_hindi_headings(monkeypatch):
    """A Hindi lecture must not come back with English headings."""
    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import coverage

    assert "Hindi (Devanagari)" in coverage.language_instruction("hindi")
    assert coverage.language_instruction("auto") == ""

    seen: list[str] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        seen.append(instruction)
        return ShortNotes(lecture_no="", subject="Zoology", title="T",
                          sections=[Section(heading=f"H{len(seen)}", points=["a", "b", "c", "d", "e", "f"])])

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")   # these assert call ORDER
    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    coverage.generate_complete_notes(_pdf_with_pages(8), language="hindi", subject_hint="biology", group_size=4)
    assert seen and all("Devanagari" in instruction for instruction in seen)


def test_bilingual_headings_are_matched_by_their_english_gloss():
    """Hindi spelling varies between groups; the gloss is what identifies them."""
    from sheets_bridge.coverage import heading_key

    left = heading_key("एलर्जी एवं एलर्जेन्स (Allergy and Allergens)")
    right = heading_key("एलर्जी एवं एलर्जन (Allergy and Allergens)")
    assert len(left & right) / min(len(left), len(right)) >= 0.6
    # different topics that merely share a bracket word stay apart
    vaccines = heading_key("टीके (Vaccines)")
    dna = heading_key("पुनर्योगज डीएनए (Recombinant DNA Vaccines)")
    assert len(vaccines & dna) / min(len(vaccines), len(dna)) < 0.6


def test_cached_notes_reload_even_when_the_generator_cleared_a_list():
    """notesbot nulls `where` while repairing a section; the cache must survive it."""
    import json

    from sheets_bridge.generator import notes_from_json

    payload = json.dumps({
        "lecture_no": "L06", "subject": "Zoology", "title": "T", "audience": "", "quote": "",
        "sections": [{
            "heading": "H", "source_local_page": 1, "source_order": 0,
            "points": ["a"], "where": None, "tricks": None, "notes": None,
            "key_terms": None, "flow": None, "visual_ids": None,
        }],
        "mindmap": None,
    }).encode("utf-8")

    notes = notes_from_json(payload)
    assert notes.sections[0].heading == "H"
    assert notes.sections[0].where == [] and notes.sections[0].tricks == []


# --------------------------------------------------------------------------- #
# PW branding on the studygram PDF
# --------------------------------------------------------------------------- #
def test_pw_watermark_is_visible_not_the_shipped_5_percent(monkeypatch):
    """The renderer's own logo watermark is invisible at 5.5% opacity."""
    import io

    from PIL import Image

    from sheets_bridge import pdf_branding
    from sheets_bridge.notes_doc import LOGO_PATH

    monkeypatch.delenv("SHEETS_BRIDGE_WATERMARK_OPACITY", raising=False)
    assert pdf_branding.watermark_opacity() == pdf_branding.DEFAULT_OPACITY > 0.055

    monkeypatch.setenv("SHEETS_BRIDGE_WATERMARK_OPACITY", "0.2")
    assert pdf_branding.watermark_opacity() == 0.2
    monkeypatch.setenv("SHEETS_BRIDGE_WATERMARK_OPACITY", "nonsense")
    assert pdf_branding.watermark_opacity() == pdf_branding.DEFAULT_OPACITY
    monkeypatch.setenv("SHEETS_BRIDGE_WATERMARK_OPACITY", "9")
    assert pdf_branding.watermark_opacity() <= 0.5          # never opaque

    faded = Image.open(io.BytesIO(pdf_branding._faded_png(str(LOGO_PATH), 0.1)))
    solid = Image.open(io.BytesIO(pdf_branding._corner_png(str(LOGO_PATH))))
    assert max(faded.getchannel("A").getdata()) <= 0.1 * 255 + 1     # watermark is faint
    assert max(solid.getchannel("A").getdata()) == 255               # corner logo is not
    assert faded.size == solid.size                                  # both trimmed the same


def test_branding_puts_the_logo_on_the_page_and_drops_the_name_tiling():
    """The PW logo is stamped in the corner; the teacher name is no longer tiled."""
    from notesbot import notes_pdf
    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import pdf_branding

    original_header = notes_pdf.NotesPDF.header
    original_loader = notes_pdf.NotesPDF._load_logo_watermark
    pdf_branding._INSTALLED = False

    assert pdf_branding.install() is True
    assert notes_pdf.NotesPDF.header is not original_header          # corner logo hook
    assert notes_pdf.NotesPDF._load_logo_watermark is not original_loader

    notes = ShortNotes(lecture_no="L1", subject="Physics", title="T",
                       sections=[Section(heading="H", points=["p"])])
    branded = notes_pdf.build_notes_pdf(notes, "by A", watermark=False, logo_watermark=True)
    assert branded.startswith(b"%PDF")


def test_title_and_header_shape_devanagari_like_the_body():
    """The same word must not render in two formats on one page.

    The body flows through `_draw_word`, but the title chip, the running header
    and the mind-map nodes are drawn with cell()/multi_cell(), which apply the
    fallback face yet shape only when text shaping is switched on — and it is
    off by default. Unshaped Devanagari keeps codepoint order, so conjuncts
    never form and every virama prints as a naked halant: the title read
    `स् वा स् थ् य` while the body drew `स्वास्थ्य` correctly.
    """
    import fpdf

    from notesbot import notes_pdf
    from notesbot.prompts import Section, ShortNotes

    from sheets_bridge import pdf_fonts

    pytest.importorskip("uharfbuzz")
    pdf_fonts._INSTALLED = False
    assert pdf_fonts.install() is True
    assert "multi_cell" in notes_pdf.NotesPDF.__dict__      # the chrome path is wrapped

    notes = ShortNotes(lecture_no="L1", subject="जीव विज्ञान",
                       title="मानव स्वास्थ्य एवं रोग",
                       sections=[Section(heading="प्रतिरक्षा", points=["सक्रिय प्रतिरक्षा"])])
    pdf = notes_pdf.NotesPDF(notes, "by X")
    pdf.add_page()
    pdf.set_font("Patrick", "", 12)

    # Shaping merges "स्वास्थ्य" into conjunct clusters, so the measured run is
    # shorter than the per-character sum fpdf2 produces with shaping off.
    unshaped = fpdf.FPDF.get_string_width(pdf, "स्वास्थ्य")
    assert pdf.get_string_width("स्वास्थ्य") < unshaped

    # Latin never enters the shaper, so its metrics are byte-for-byte the old ones.
    assert pdf.get_string_width("Immunity") == fpdf.FPDF.get_string_width(pdf, "Immunity")

    assert notes_pdf.build_notes_pdf(notes, "by X").startswith(b"%PDF")


# --- multi-sheet profiles, the sign-in gate and the run log -------------------
def test_sheet_links_yield_the_id_and_never_guess_the_gid():
    """A link copied from any tab but the first carries its gid; a missing one
    must stay unknown rather than silently becoming 0 (i.e. the wrong tab)."""
    from sheets_bridge import profiles as P

    assert P.parse_sheet_link(
        "https://docs.google.com/spreadsheets/d/1N6juvFsmHgLd8NfzWSU4gIOdmPcXrc159DuRxsVg4Ek/edit?gid=559490731#gid=559490731"
    ) == ("1N6juvFsmHgLd8NfzWSU4gIOdmPcXrc159DuRxsVg4Ek", 559490731)
    assert P.parse_sheet_link(
        "https://docs.google.com/spreadsheets/d/1JMPycMZdBZaKmut_vHHC0BOyurYMoxhOgQpb7N2Hips/edit"
    ) == ("1JMPycMZdBZaKmut_vHHC0BOyurYMoxhOgQpb7N2Hips", None)

    for bad in ("", "   ", "https://example.com/x", "https://drive.google.com/drive/folders/abcdefghij"):
        with pytest.raises(BridgeError):
            P.parse_sheet_link(bad)


def test_profiles_round_trip_and_refuse_duplicates(tmp_path):
    from sheets_bridge import profiles as P

    path = tmp_path / "sheets.json"
    first = P.SheetTarget(name="neet-hindi", spreadsheet_id="1" + "a" * 20, gid=0)
    second = P.SheetTarget(name="new-batch", spreadsheet_id="1" + "b" * 20, gid=559490731)
    P.save_profiles([first, second], path)

    loaded = P.load_profiles(path)
    assert [t.name for t in loaded] == ["neet-hindi", "new-batch"]
    assert loaded[1].gid == 559490731

    with pytest.raises(BridgeError) as excinfo:
        P.add_profile(P.SheetTarget(name="NEET-Hindi", spreadsheet_id="1" + "c" * 20, gid=1), path)
    assert excinfo.value.code == "duplicate_profile"      # names are case-insensitive

    assert [t.name for t in P.remove_profile("neet-hindi", path)] == ["new-batch"]
    with pytest.raises(BridgeError):
        P.remove_profile("gone", path)


def test_selecting_profiles_never_silently_runs_every_sheet():
    """Running every sheet because a flag was forgotten spends real money."""
    from sheets_bridge import profiles as P

    one = P.SheetTarget(name="a", spreadsheet_id="1" + "a" * 20, gid=0)
    two = P.SheetTarget(name="b", spreadsheet_id="1" + "b" * 20, gid=1)

    assert P.select((one,), None) == (one,)               # the only sheet is unambiguous
    with pytest.raises(BridgeError) as excinfo:
        P.select((one, two), None)
    assert excinfo.value.code == "profile_required"

    assert P.select((one, two), ["b"]) == (two,)
    assert P.select((one, two), None, all_profiles=True) == (one, two)
    with pytest.raises(BridgeError) as excinfo:
        P.select((one, two), ["nope"])
    assert excinfo.value.code == "profile_not_found"


def test_one_unusable_sheet_does_not_stop_the_others():
    import types

    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch

    def target(name, gid):
        return types.SimpleNamespace(name=name, spreadsheet_id="1" + name * 20, gid=gid,
                                     signature="", max_cost_inr=None, also_pdf=None, enabled=True)

    def fake_sheet_client(service, spreadsheet_id, gid):
        if gid == 99:
            raise BridgeError("tab_not_found", "no tab with gid=99")
        return FakeSheet(_grid(_row(2)))

    original_sheet, original_drive = sheets_module.SheetClient, drive_module.DriveClient
    sheets_module.SheetClient = fake_sheet_client
    drive_module.DriveClient = lambda service: FakeDrive()
    try:
        result = batch.run_targets(
            [target("b", 99), target("g", 0)], None, None, RunOptions(dry_run=True)
        )
    finally:
        sheets_module.SheetClient, drive_module.DriveClient = original_sheet, original_drive

    assert [r.name for r in result.results] == ["b", "g"]
    assert result.results[0].error and result.results[0].summary is None
    assert result.results[1].summary is not None           # the batch carried on


def test_the_cost_cap_is_a_budget_for_the_whole_batch_not_per_sheet():
    """Without carrying spend forward, --max-cost would silently multiply by
    the number of sheets."""
    import types

    from sheets_bridge import batch

    options = RunOptions(max_cost_inr=100.0)
    target = types.SimpleNamespace(name="t", spreadsheet_id="x", gid=0, signature="",
                                   max_cost_inr=None, also_pdf=None, enabled=True)
    assert batch.options_for(target, options, spent=0.0).cost_spent_before == 0.0
    assert batch.options_for(target, options, spent=60.0).cost_spent_before == 60.0

    # A sheet entering the batch with the cap already spent runs nothing.
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3)))
    summary = Runner(sheet, FakeDrive(), RunOptions(max_cost_inr=100.0, cost_spent_before=120.0)).run()
    assert summary.outcomes and all(
        o.outcome == OUTCOME_STOPPED and o.code == "cost_cap" for o in summary.outcomes
    )


def test_the_automation_is_closed_until_someone_signs_in(tmp_path):
    """Every automation route spends money or reads someone's Drive, so it stays
    shut until a Google account has passed the PW allowlist."""
    from sheets_bridge import auth_session, webui
    from sheets_bridge.settings import Settings

    from dataclasses import replace

    # No cached token anywhere the gate might look, so nothing restores silently.
    settings = replace(Settings.from_env(), token_path=tmp_path / "no-token.json")
    auth = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    controller = webui.RunController(settings, auth=auth)

    for method, route in (("POST", "run"), ("GET", "profiles"), ("POST", "profiles"),
                          ("POST", "profiles/delete"), ("POST", "tabs")):
        status, payload = webui.api_response(controller, method, route, {})
        assert status == 403 and payload["error"] == "sign_in_required"

    # ... but the page can always ask who is signed in.
    status, payload = webui.api_response(controller, "GET", "session", None)
    assert status == 200 and payload["signedIn"] is False

    # With no usable token the page offers the one-off sign-in ...
    status, payload = webui.api_response(controller, "GET", "config", None)
    assert status == 200 and payload["signInRequired"] is True

    with pytest.raises(BridgeError) as excinfo:
        auth.require()
    assert excinfo.value.code == "sign_in_required"

    # ... but once the server has its cached account there is no gate at all:
    # the Whitelisted sheet is what decides who may use the tool.
    auth._restored = True
    auth._account = auth_session.Account("a@pw.live", object(), object(), "sheets", "drive")
    status, payload = webui.api_response(controller, "GET", "config", None)
    assert status == 200 and payload["signInRequired"] is False
    status, payload = webui.api_response(controller, "GET", "session", None)
    assert payload["signedIn"] is True and payload["email"] == "a@pw.live"


def test_a_saved_sheet_can_be_removed_from_the_studio(tmp_path, monkeypatch):
    from sheets_bridge import profiles as P
    from sheets_bridge import webui
    from sheets_bridge.settings import Settings

    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(tmp_path / "sheets.json"))
    P.add_profile(P.SheetTarget(name="Lakshya NEET 2027", spreadsheet_id="1" + "a" * 20, gid=0))
    P.add_profile(P.SheetTarget(name="Arjuna JEE 2027", spreadsheet_id="1" + "b" * 20, gid=5))
    controller = webui.RunController(Settings.from_env())

    status, payload = webui.api_response(controller, "POST", "profiles/delete", {"name": "lakshya neet 2027"})
    assert status == 200 and payload["ok"]
    assert [t.name for t in P.load_profiles()] == ["Arjuna JEE 2027"]
    status, payload = webui.api_response(controller, "GET", "profiles", None)
    assert [p["name"] for p in payload["profiles"]] == ["Arjuna JEE 2027"]
    status, payload = webui.api_response(controller, "POST", "profiles/delete", {"name": "Lakshya NEET 2027"})
    assert status == 400 and payload["error"] == "profile_not_found"


def test_without_the_gate_the_standalone_page_behaves_as_before():
    from sheets_bridge import webui
    from sheets_bridge.settings import Settings

    controller = webui.RunController(Settings.from_env())      # no auth
    status, payload = webui.api_response(controller, "GET", "config", None)
    assert status == 200 and payload["signInRequired"] is False
    assert payload["profiles"], "the .env sheet is still offered as a profile"


# --- header-driven columns, the formula guard, tab classification -----------
_REAL_HEADER = [
    "Project Code", "Project Name", "Batch", "Subject", "Lecture Faculty", "Lecture Date",
    "Chapter", "Lecture Number", "Drive Link", "Class Notes Status\n(By BM)", "Content SPOC",
    "Time Taken", "Final File Status\n(by Content Team)", "Batch Upload status\n(For BM)",
    "Class Notes Status\n(By BM)\nTime Stamp", "Final File Status\n(by Content Team)\nTime Stamp",
]
_AUDIO_HEADER = [
    "Project Code", "Project Name", "Batch", "Subject", "Lecture Faculty", "Chapter_Name",
    "Drive Link", "Chapter Completion Status (by BM)", "Content SPOC", "Time Taken",
    "Final File Status\n(by Content Team)", "Batch Upload status\n(For BM)",
    "Class Notes Status(By BM) Time Stamp ", "Final File Status (by Content Team) Time Stamp",
]


def test_a_blank_faculty_header_after_subject_still_reads_the_faculty():
    """Lakshya NEET 2027 keeps its teachers in column E under an empty E1."""
    header = list(_REAL_HEADER)
    header[4] = "  "
    layout = C.Layout.from_header(header)
    assert layout.letter("faculty") == "E"
    values = ["P", "N", "Lakshya NEET 2027", "Physical Chemistry", "Sudhanshu Kumar Sir", "d", "Solution", "1",
              "https://drive.google.com/drive/folders/1abcdefghij", "Done", "spoc", "", "", "", "", ""]
    assert SheetRow.from_values(2, values, layout).faculty == "Sudhanshu Kumar Sir"
    # A catalog has no such column: its subject is followed by the chapter.
    catalog_layout = C.Layout.from_header(
        ["exam", "batch", "subject", "chapter", "Lecture_Date", "lecture_topic", "downloadable_link"])
    assert catalog_layout.index("faculty") is None


def test_a_row_without_a_faculty_is_signed_with_the_teacher_on_the_cover():
    import io

    from pypdf import PdfReader

    from notesbot.prompts import ShortNotes
    from sheets_bridge.generator import render_artifacts
    from sheets_bridge.mapping import signature_for

    notes = ShortNotes(lecture_no="", subject="Zoology", title="Animal Tissues", sections=[],
                       faculty="By – Venky Anna")
    assert signature_for(normalize_signature(""), notes) == "by Venky Anna"
    assert signature_for(normalize_signature(""), notes.model_copy(update={"faculty": ""})) == "by PW Faculty"
    assert signature_for(normalize_signature(""), notes.model_copy(update={"faculty": "---"})) == "by PW Faculty"
    # The institute's branding on a slide footer is no teacher.
    for brand in ("Physics Wallah", "PW Faculty", "Sir"):
        assert signature_for(normalize_signature(""), notes.model_copy(update={"faculty": brand})) == "by PW Faculty"

    pdf = render_artifacts(notes, signature=normalize_signature(""), language="english")
    text = "".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Venky Anna" in text and "PW Faculty" not in text


def test_the_slides_teacher_wins_when_the_sheet_names_someone_else():
    from notesbot.prompts import ShortNotes
    from sheets_bridge.mapping import choose_signature, signature_for

    notes = ShortNotes(lecture_no="", subject="Chemistry", title="GOC", sections=[], faculty="By - Pankaj Sijariya Sir")
    chosen, why = choose_signature("by Rohit Agarwal Sir", notes)
    assert chosen == "by Pankaj Sijariya Sir" and "different teacher" in why
    # The same teacher, spelt differently on the slides: the sheet's spelling stays.
    for printed in ("ROHIT AGRAWAL", "Faculty: Rohit Sir", "रोहित अग्रवाल सर"):
        assert signature_for("by Rohit Agarwal Sir", notes.model_copy(update={"faculty": printed})) == \
            "by Rohit Agarwal Sir", printed
    # No teacher on the slides: the sheet's name.
    assert signature_for("by Rohit Agarwal Sir", notes.model_copy(update={"faculty": ""})) == "by Rohit Agarwal Sir"
    # A signature typed in the Studio is never replaced.
    assert choose_signature("by Rohit Agarwal Sir", notes, fixed=True) == ("by Rohit Agarwal Sir", "typed in the Studio")


@pytest.mark.parametrize("sheet, slides, same", [
    ("Rohit Agarwal Sir", "Rohit Agarwal", True),
    ("Rohit Agarwal Sir", "Rohit Sir", True),
    ("Vinay Soni Sir", "विनय सोनी सर", True),
    ("Tanuj Bansal Sir", "तनुज बंसल", True),
    ("Neeraj Kumar Chaudhary Sir (NKC Sir)", "NKC Sir", True),
    ("Neeraj Kumar Chaudhary Sir", "नीरज चौधरी", True),
    ("Dr. Akanksha Agarwal Ma'am", "Akanksha Agarwal", True),
    ("Rohit Agarwal Sir", "Pankaj Sijariya Sir", False),
    ("Shivam Mishra Sir", "Shivam Sharma", False),
    ("Rahul Dudi Sir", "Rahul Yadav Sir", False),
    ("Saleem Ahmad Sir", "Sir", False),
])
def test_teacher_names_are_compared_ignoring_titles_spelling_and_script(sheet, slides, same):
    from sheets_bridge.mapping import same_teacher

    assert same_teacher(sheet, slides) is same


def test_columns_are_found_by_header_not_by_letter():
    """Production tabs carry line breaks in their headers, and a copied tab may
    have a column inserted; both must read the right cells."""
    layout = C.Layout.from_header(_REAL_HEADER)
    assert layout.problems() == []
    letters = layout.describe()
    assert (letters["drive_link"], letters["lecture_number"], letters["chapter"]) == ("I", "H", "G")
    assert (letters["bm_status"], letters["final_status"], letters["final_timestamp"]) == ("J", "M", "P")
    assert (layout.auto_status_index, layout.review_status_index) == (16, 17)   # created at Q:R
    assert C.DEFAULT_LAYOUT.describe() == C.Layout.from_header(C.HUMAN_HEADERS + C.CONTROL_HEADERS + C.LEGACY_CONTROL_HEADERS).describe()

    shifted = ["Sr No"] + list(_REAL_HEADER)                # a column inserted at A
    values = ["1", "P", "N", "Batch X", "Physics", "Sir", "d", "Ch", "5",
              "https://drive.google.com/drive/folders/1abcdefghij", "Done", "spoc", "", "", "", "", ""]
    row = SheetRow.from_values(2, values, C.Layout.from_header(shifted))
    assert row.drive_link.startswith("https://drive") and row.lecture_number_text == "5"
    assert row.bm_done and not row.final_done and not row.is_blank

    audio = C.Layout.from_header(_AUDIO_HEADER)
    assert audio.problems() == [] and audio.missing_recommended() == ["lecture_number"]
    assert audio.letter("drive_link") == "G" and audio.letter("bm_status") == "H"

    swapped = list(C.HUMAN_HEADERS)
    swapped[6], swapped[8] = swapped[8], swapped[6]
    assert C.validate_header(swapped) == []                 # re-ordered is fine now
    assert any("Drive Link" in p for p in C.validate_header(["Batch Name", "GID"]))


def test_a_tab_without_the_needed_columns_is_refused_before_anything_is_touched():
    sheet = FakeSheet([["Batch Name", "GID"], ["Arjuna NEET 3.0 2027", "1719015327"]])
    with pytest.raises(BridgeError) as excinfo:
        Runner(sheet, FakeDrive(), RunOptions(dry_run=True)).run()
    assert excinfo.value.code == "column_layout_mismatch" and "Drive Link" in str(excinfo.value)
    assert sheet.writes == []


class _ProbingSheet(FakeSheet):
    """FakeSheet that also answers the first-row cell-kind probe."""

    def __init__(self, grid, kinds):
        super().__init__(grid)
        self.kinds = kinds
        self.probed: list[int] = []

    def probe_cell_kinds(self, row_number, last_index):
        self.probed.append(row_number)
        return self.kinds


def test_computed_tabs_are_refused_and_self_stamping_columns_left_alone():
    from sheets_bridge import sheets as S

    layout = C.DEFAULT_LAYOUT
    assert S.cell_kinds([
        {"userEnteredValue": {"stringValue": "Done"}},
        {"userEnteredValue": {"formulaValue": "=NOW()"}},
        {"effectiveValue": {"stringValue": "x"}},
        {},
    ]) == {0: S.KIND_LITERAL, 1: S.KIND_FORMULA, 2: S.KIND_SPILL, 3: S.KIND_EMPTY}

    spill = {layout.index("bm_status"): S.KIND_SPILL, layout.index("final_status"): S.KIND_SPILL}
    with pytest.raises(BridgeError) as excinfo:
        S.apply_cell_kinds(layout, spill)
    assert excinfo.value.code == "computed_tab"
    with pytest.raises(BridgeError) as excinfo:
        S.apply_cell_kinds(layout, {layout.index("final_status"): S.KIND_FORMULA})
    assert excinfo.value.code == "computed_status_column"
    adjusted, notes = S.apply_cell_kinds(layout, {layout.index("final_timestamp"): S.KIND_FORMULA})
    assert adjusted.write_final_timestamp is False and notes

    # through the runner: a Master-style roll-up never gets a single write ...
    sheet = _ProbingSheet(_grid(_row(2)), spill)
    with pytest.raises(BridgeError) as excinfo:
        Runner(sheet, FakeDrive(), RunOptions(dry_run=True)).run()
    assert excinfo.value.code == "computed_tab" and sheet.writes == [] and sheet.probed == [2]

    # ... and a self-stamping P column is detected on the first data row
    sheet = _ProbingSheet(_grid(_row(2)), {layout.index("final_timestamp"): S.KIND_FORMULA})
    Runner(sheet, FakeDrive(), RunOptions(dry_run=True)).run()
    assert sheet.layout.write_final_timestamp is False


def test_write_control_skips_the_timestamp_the_sheet_computes_itself():
    """Overwriting =IF(P2,P2,IF(M2="Done",now(),)) with a value destroys the
    formula the content team relies on."""
    import types

    from sheets_bridge.sheets import SheetClient, TabInfo

    bodies: list[dict] = []

    class _Values:
        def batchUpdate(self, spreadsheetId, body):
            bodies.append(body)
            return types.SimpleNamespace(execute=lambda num_retries=0: {})

    class _Spreadsheets:
        def values(self):
            return _Values()

    class _Service:
        def spreadsheets(self):
            return _Spreadsheets()

    client = SheetClient(_Service(), "sid", 0)
    client._tab = TabInfo("Tab", 0, 18, 100)

    client.layout = C.DEFAULT_LAYOUT
    client.write_control(5, [C.STATUS_DONE, C.REVIEW_PENDING])
    ranges = [d["range"] for d in bodies[-1]["data"]]
    assert ranges == ["'Tab'!Q5:R5"]           # M and P are never written

    # a tab with the columns elsewhere writes elsewhere
    shifted = C.Layout.from_header(["Sr No"] + list(C.HUMAN_HEADERS))
    client.layout = shifted
    client.write_control(7, ["claimed", ""])
    assert [d["range"] for d in bodies[-1]["data"]] == ["'Tab'!R7:S7"]


def test_folder_listings_are_fetched_once_per_run_and_answer_the_duplicate_check():
    from sheets_bridge.mapping import output_name

    lecture1 = DriveFile("1Lecture0000000001", "L01 Kinematics Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "")
    lecture2 = DriveFile("1Lecture0000000002", "L02 Kinematics Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "")
    # the row's output is the studygram PDF, so that is what the duplicate
    # check looks for in the listing
    review = DriveFile("1Review00000000001", output_name(lecture1.name), "application/pdf", 10, (FOLDER_ID,), "", "https://drive.google.com/file/d/1Review00000000001/view")

    class _CountingDrive:
        def __init__(self):
            self.list_calls = 0
            self.find_calls = 0

        def get_file(self, file_id):
            assert file_id == FOLDER_ID
            return FOLDER

        def list_folder(self, folder_id, drive_id=""):
            self.list_calls += 1
            return [lecture1, lecture2, review]

        def find_in_folder(self, *args, **kwargs):
            self.find_calls += 1
            return []

    link = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
    sheet = FakeSheet(_grid(_row(2, link=link, lecture="L01"), _row(3, link=link, lecture="L02")))
    drive = _CountingDrive()
    summary = Runner(sheet, drive, RunOptions(dry_run=True)).run()

    assert drive.list_calls == 1 and drive.find_calls == 0        # one listing served both rows
    by_row = {o.row: o for o in summary.outcomes}
    assert by_row[2].outcome == OUTCOME_PLANNED and by_row[2].link == review.web_view_link
    assert by_row[3].outcome == OUTCOME_PLANNED and by_row[3].link == ""


def test_every_tab_of_a_workbook_is_classified_in_four_requests():
    from sheets_bridge import tabs as T

    calls: list[str] = []

    class _Exec:
        def __init__(self, payload):
            self.payload = payload

        def execute(self, num_retries=0):
            return self.payload

    HEADERS = {"'Master'": _REAL_HEADER, "'Arjuna'": _REAL_HEADER, "'Batch List'": ["Batch Name", "GID"], "'Format'": _REAL_HEADER}
    BLOCKS = {                                   # I2:M -> [link, J, K, L, M]
        "'Master'": [["https://f", "Done", "", "", "Done"], ["https://f", "Done", "", "", ""]],
        "'Arjuna'": [["https://f", "Done", "", "", ""], ["", "", "", "", ""]],
        "'Format'": [],
    }

    class _Values:
        def batchGet(self, spreadsheetId, ranges, majorDimension="ROWS"):
            calls.append("batchGet")
            if all(r.endswith("!1:1") for r in ranges):
                return _Exec({"valueRanges": [{"values": [HEADERS[r.split("!")[0]]]} for r in ranges]})
            # Drive Link (I) through Review Status (R): a "Regenerate" in R counts as ready.
            assert all(r.split("!")[1].startswith("I2:R") for r in ranges), ranges
            return _Exec({"valueRanges": [{"values": BLOCKS[r.split("!")[0]]} for r in ranges]})

    def _cells(kind):
        if kind == "spill":
            return {"values": [{"effectiveValue": {"stringValue": "x"}} for _ in range(18)]}
        return {"values": [{"userEnteredValue": {"stringValue": "x"}} for _ in range(18)]}

    class _Spreadsheets:
        def get(self, spreadsheetId, fields="", ranges=None, includeGridData=False):
            calls.append("get")
            if not includeGridData:
                return _Exec({"properties": {"title": "WB"}, "sheets": [
                    {"properties": {"sheetId": 1, "title": "Master", "gridProperties": {"rowCount": 10, "columnCount": 18}}},
                    {"properties": {"sheetId": 2, "title": "Arjuna", "gridProperties": {"rowCount": 10, "columnCount": 18}}},
                    {"properties": {"sheetId": 3, "title": "Batch List", "gridProperties": {"rowCount": 10, "columnCount": 2}}},
                    {"properties": {"sheetId": 4, "title": "Format", "gridProperties": {"rowCount": 10, "columnCount": 18}}},
                ]})
            assert ranges and all(r.split("!")[1].startswith("A2:") for r in ranges), ranges
            return _Exec({"sheets": [
                {"properties": {"sheetId": 1}, "data": [{"rowData": [_cells("spill")]}]},
                {"properties": {"sheetId": 2}, "data": [{"rowData": [_cells("literal")]}]},
            ]})

        def values(self):
            return _Values()

    class _Service:
        def spreadsheets(self):
            return _Spreadsheets()

    title, summaries = T.list_tabs(_Service(), "sid")
    by = {s.title: s for s in summaries}
    assert title == "WB" and calls == ["get", "batchGet", "batchGet", "get"]
    assert by["Master"].kind == T.KIND_COMPUTED and not by["Master"].supported
    assert by["Arjuna"].kind == T.KIND_NOTES and by["Arjuna"].supported
    assert by["Arjuna"].eligible == 1 and by["Arjuna"].rows == 1 and by["Arjuna"].mapping["drive_link"] == "I"
    assert by["Batch List"].kind == T.KIND_OTHER and "Drive Link" in by["Batch List"].note
    assert by["Format"].kind == T.KIND_TEMPLATE and by["Format"].supported is False


def test_the_cached_token_is_adopted_and_still_checked_against_the_allowlist(tmp_path, monkeypatch):
    """No interactive gate: the server runs as the cached account - but that
    account must still pass the domain + Whitelisted-sheet check."""
    from dataclasses import replace

    import sheets_bridge.google_auth as google_auth
    import sheets_bridge.pw_gate as pw_gate
    from sheets_bridge import auth_session
    from sheets_bridge.settings import Settings

    settings = replace(Settings.from_env(), token_path=tmp_path / "token.json")
    monkeypatch.setattr(google_auth, "load_any_cached", lambda *a, **k: object())
    monkeypatch.setattr(google_auth, "build_services", lambda creds: ("sheets", "drive"))
    monkeypatch.setattr(google_auth, "user_email", lambda creds: "listed@pw.live")

    allowed = {"ok": True}

    class _Access:
        def __init__(self, token_provider, *, email=""):
            self.email = email

        def domain_ok(self):
            return self.email.endswith("@pw.live")

        def ensure_allowed(self):
            if not allowed["ok"]:
                raise BridgeError("not_authorized", "ask the owner to add the email")

    monkeypatch.setattr(pw_gate, "ProxyAccess", _Access)

    auth = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    status = auth.status()
    assert status["signedIn"] and status["email"] == "listed@pw.live"
    assert auth.account().sheets_service == "sheets"

    # an account that is not on the allowlist is not adopted, cached or not
    allowed["ok"] = False
    other = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    assert other.status()["signedIn"] is False
    with pytest.raises(BridgeError) as excinfo:
        other.require()
    assert excinfo.value.code == "sign_in_required"

    # a personal account never reaches the allowlist check
    allowed["ok"] = True
    monkeypatch.setattr(google_auth, "user_email", lambda creds: "someone@gmail.com")
    personal = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    assert personal.status()["signedIn"] is False

def test_a_chapter_named_folder_accepts_terse_file_names():
    """Production: folder 'Structural Organization in Animals' holds 'SO 01' ..
    'SO 10'. Demanding the chapter name inside each file refused every row."""
    files = [DriveFile(f"1SO{n:02d}xxxxxxxxxxxxxxx", f"SO {n:02d}.pdf", "application/pdf", 10, (FOLDER_ID,), "", "") for n in range(1, 11)]
    chapter = "Structural Organization in Animals"

    # the folder name settles it ...
    match = choose_lecture_pdf(files, 7, chapter=chapter, folder_name=chapter)
    assert match.file.name == "SO 07.pdf"
    # ... and so do the initials on their own (a mixed folder, no folder hint)
    match = choose_lecture_pdf(files, 7, chapter=chapter)
    assert match.file.name == "SO 07.pdf"

    # a folder named for ANOTHER chapter still refuses to hand back its lecture 7
    other = [DriveFile("1Other07xxxxxxxxxxxx", "Laws of Motion 07 Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "")]
    with pytest.raises(BridgeError) as excinfo:
        choose_lecture_pdf(other, 7, chapter=chapter, folder_name="Physics Lectures")
    assert excinfo.value.code == "chapter_not_found"

    # inside a chapter folder, a file that DOES name the chapter still wins a tie
    mixed = files + [DriveFile("1SOfull07xxxxxxxxxxx", "Structural Organization in Animals 07 Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", "")]
    match = choose_lecture_pdf(mixed, 7, chapter=chapter, folder_name=chapter)
    assert match.file.name.startswith("Structural Organization")


def test_the_picked_tab_beats_the_gid_in_the_pasted_link():
    """The link always carries the gid of whichever tab was open - usually the
    'Batch List' index - so an explicit choice from the tab picker must win.
    (Caught live: every add landed on the index tab.)"""
    from sheets_bridge.webui import chosen_gid

    assert chosen_gid(1461744415, 559490731) == 1461744415
    assert chosen_gid("1461744415", 559490731) == 1461744415
    assert chosen_gid(None, 559490731) == 559490731
    assert chosen_gid(None, None) is None
    assert chosen_gid(True, 5) == 5                          # a bool is not a gid
    assert chosen_gid(-1, 5) == 5


# --- audit fixes -------------------------------------------------------------
def test_a_live_claim_is_honoured_and_a_stale_one_is_retried():
    """The claim guard used to be dead: the stamp lived in a legacy column
    nothing wrote. It now rides inside the Q cell."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    fresh = _row(2, "claimed 2026-08-25 11:40:00 +0530")
    stale = _row(3, "claimed 2026-08-25 07:00:00 +0530")
    bare = _row(4, "claimed")                              # a crash left no stamp

    assert fresh.auto_status == "claimed" and fresh.claim_stamp == "2026-08-25 11:40:00 +0530"
    assert not is_eligible(fresh, now=now)                 # another run is on it
    assert is_eligible(stale, now=now)                     # abandoned 5 h ago
    assert is_eligible(bare, now=now)
    assert _row(5, "failed: too_large").auto_status == "failed"

    sheet = FakeSheet(_grid(_row(2)))
    runner = Runner(sheet, FakeDrive(), RunOptions(), clock=lambda: "2026-08-25 12:00:00 +0530")
    assert runner._control(C.STATUS_CLAIMED) == ["claimed 2026-08-25 12:00:00 +0530", ""]
    assert runner._control(C.STATUS_DONE, link="https://doc") == [C.STATUS_DONE, C.REVIEW_PENDING]


def test_a_row_that_moved_under_the_run_is_never_written():
    """People edit the sheet during an hours-long run; a row addressed by
    number must be re-identified before every write."""

    class _MovingSheet(FakeSheet):
        def __init__(self, grid):
            super().__init__(grid)
            self.identity = None

        def read_row_identity(self, row_number):
            return self.identity

    sheet = _MovingSheet(_grid(_row(2, chapter="Kinematics", lecture="L01")))
    row = rows_from_grid(sheet.grid)[0]
    runner = Runner(sheet, FakeDrive(), RunOptions())

    sheet.identity = (row.drive_link, "L01", "Kinematics")
    assert runner._write(row, C.STATUS_CLAIMED) is True and len(sheet.writes) == 1

    sheet.identity = ("https://drive.google.com/file/d/1SomeoneElse/view", "L07", "Optics")
    assert runner._write(row, C.STATUS_DONE, link="x", final=True) is False
    assert len(sheet.writes) == 1                          # nothing landed on the wrong lecture


def test_an_interrupt_ends_the_whole_batch_not_just_one_sheet():
    import types

    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch

    def target(name):
        return types.SimpleNamespace(name=name, spreadsheet_id="1" + name * 20, gid=0,
                                     signature="", max_cost_inr=None, also_pdf=None, enabled=True)

    class _InterruptedRunner:
        def __init__(self, sheet, drive, options, **kwargs):
            self.options = options

        def run(self):
            from sheets_bridge.runner import RunSummary

            summary = RunSummary(run_id="R")
            summary.interrupted = True                     # Ctrl+C landed inside this sheet
            return summary

    original = (sheets_module.SheetClient, drive_module.DriveClient, batch.Runner)
    sheets_module.SheetClient = lambda *a, **k: FakeSheet(_grid(_row(2)))
    drive_module.DriveClient = lambda service: FakeDrive()
    batch.Runner = _InterruptedRunner
    try:
        result = batch.run_targets([target("a"), target("b"), target("c")], None, None, RunOptions(dry_run=True))
    finally:
        sheets_module.SheetClient, drive_module.DriveClient, batch.Runner = original

    assert result.interrupted
    assert [r.error for r in result.results] == ["", "stop_requested", "stop_requested"]


def test_a_profile_cap_is_for_that_sheet_and_a_global_cap_for_the_batch():
    import types

    from sheets_bridge import batch

    capped = types.SimpleNamespace(name="b", spreadsheet_id="x", gid=0, signature="",
                                   max_cost_inr=200.0, also_pdf=None, enabled=True)
    # global --max-cost: the batch budget travels with the spend so far
    opts = batch.options_for(capped, RunOptions(max_cost_inr=500.0), spent=400.0)
    assert (opts.max_cost_inr, opts.cost_spent_before) == (500.0, 400.0)
    # no global cap: the profile's own cap applies to its own spend only
    opts = batch.options_for(capped, RunOptions(), spent=400.0)
    assert (opts.max_cost_inr, opts.cost_spent_before) == (200.0, 0.0)


def test_an_explicit_signature_flag_beats_env_and_profile_signatures():
    from dataclasses import replace

    from sheets_bridge import profiles as P
    from sheets_bridge.settings import Settings

    env = replace(Settings.from_env(), spreadsheet_id="1" + "e" * 20, signature="From Env")
    target = P.env_target(env)
    assert target is not None and target.signature == ""        # never re-injected

    assert P.env_target(replace(env, spreadsheet_id="")) is None  # no built-in sheet any more
    assert P.resolve_targets(replace(env, spreadsheet_id=""), path=None) == ()
    with pytest.raises(BridgeError) as excinfo:
        P.select((), None)
    assert excinfo.value.code == "no_profiles"


def test_each_google_identity_gets_its_own_proxy_pass(monkeypatch):
    import types

    import pw_access

    pw_access.reset_session()

    def fake_post(url, headers=None, json=None, timeout=None):
        google = headers["Authorization"].split(" ", 1)[1]
        return types.SimpleNamespace(status_code=200, json=lambda: {"session_token": f"pass-for-{google}", "expires_at_ms": 4102444800000})

    monkeypatch.setattr(pw_access.requests, "post", fake_post)
    assert pw_access._auth_token("google-A") == "pass-for-google-A"
    assert pw_access._auth_token("google-B") == "pass-for-google-B"   # not A's pass
    assert pw_access._auth_token("google-A") == "pass-for-google-A"   # still cached
    pw_access.reset_session()
    assert pw_access._sessions == {}


def test_bad_profile_caps_and_pending_cache_sweep(tmp_path, monkeypatch):
    import json
    import os
    import time

    from sheets_bridge import output_cache
    from sheets_bridge import profiles as P

    path = tmp_path / "sheets.json"
    path.write_text(json.dumps({"profiles": [{"name": "x", "spreadsheetId": "1" + "a" * 20, "gid": 0, "maxCostInr": "lots"}]}), encoding="utf-8")
    with pytest.raises(BridgeError) as excinfo:
        P.load_profiles(path)
    assert "maxCostInr" in str(excinfo.value)

    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", str(tmp_path / "pending"))
    old = tmp_path / "pending" / "old.json"
    fresh = tmp_path / "pending" / "fresh.json"
    old.parent.mkdir()
    old.write_bytes(b"{}")
    fresh.write_bytes(b"{}")
    stale = time.time() - 8 * 24 * 3600
    os.utime(old, (stale, stale))
    assert output_cache.sweep() == 1
    assert not old.exists() and fresh.exists()


# --- review fixes ------------------------------------------------------------
def test_a_subject_folder_is_not_mistaken_for_the_chapter_folder():
    """'Chemistry' is a word inside 'Chemistry in Everyday Life', not that
    chapter: treating it as the chapter folder handed back Hydrocarbons L05."""
    from sheets_bridge.lecture_match import folder_is_chapter

    files = [
        DriveFile("1Hydro05notesxxxxxxxx", "Hydrocarbons L05 Class Notes.pdf", "application/pdf", 10, (FOLDER_ID,), "", ""),
        DriveFile("1Hydro05dppxxxxxxxxxx", "Hydrocarbons L05 DPP.pdf", "application/pdf", 10, (FOLDER_ID,), "", ""),
    ]
    with pytest.raises(BridgeError) as excinfo:
        choose_lecture_pdf(files, 5, chapter="Chemistry in Everyday Life", folder_name="Chemistry")
    assert excinfo.value.code == "chapter_not_found"

    assert not folder_is_chapter("Chemistry", "Chemistry in Everyday Life")
    assert not folder_is_chapter("Physics Lectures", "Circles")
    assert folder_is_chapter("Structural Organization in Animals", "Structural Organisation in Animals")   # spelling
    assert folder_is_chapter("SO", "Structural Organization in Animals")                                   # initials
    assert folder_is_chapter("Chemistry in Everyday Life - Class 12", "Chemistry in Everyday Life")


def test_headerless_columns_holding_data_are_never_used_as_control_columns():
    """With no 'Auto Status' header the control columns are created where the
    header ends - which must not be on top of a column someone is using."""
    header = list(C.HUMAN_HEADERS)                              # no control headers at all
    row = list(_row(2).values[:C.HUMAN_COLUMN_COUNT]) + ["Uploaded", "2026-08-01"]

    sheet = FakeSheet([header, row])
    with pytest.raises(BridgeError) as excinfo:
        Runner(sheet, FakeDrive(), RunOptions(dry_run=True)).run()
    assert excinfo.value.code == "control_column_conflict" and "Q" in str(excinfo.value)
    assert sheet.writes == [] and sheet.header_writes == 0

    # the same data under real control headers is an ordinary tab
    fine = FakeSheet([header + list(C.CONTROL_HEADERS), row])
    Runner(fine, FakeDrive(), RunOptions(dry_run=True)).run()

    # and an empty would-be control column is created as before
    empty = FakeSheet([header, list(_row(2).values[:C.HUMAN_COLUMN_COUNT])], headers_present=False)
    Runner(empty, FakeDrive(), RunOptions()).run()
    assert empty.header_writes == 1


def test_gid_alone_is_a_configuration_error_not_a_run_against_nothing(capsys):
    from sheets_bridge import cli

    code = cli.main(["--spreadsheet-id", "", "--gid", "12345", "--dry-run", "--no-browser"])
    assert code == 2
    assert "no_sheet" in capsys.readouterr().err


def test_signing_out_is_not_undone_by_the_silent_restore(tmp_path, monkeypatch):
    from dataclasses import replace

    import sheets_bridge.google_auth as google_auth
    import sheets_bridge.pw_gate as pw_gate
    from sheets_bridge import auth_session
    from sheets_bridge.settings import Settings

    class _Access:
        def __init__(self, token_provider, *, email=""):
            self.email = email

        def domain_ok(self):
            return True

        def ensure_allowed(self):
            return None

    monkeypatch.setattr(pw_gate, "ProxyAccess", _Access)
    monkeypatch.setattr(google_auth, "load_any_cached", lambda *a, **k: object())
    monkeypatch.setattr(google_auth, "build_services", lambda creds: ("sheets", "drive"))
    monkeypatch.setattr(google_auth, "user_email", lambda creds: "a@pw.live")

    auth = auth_session.AutomationAuth(
        replace(Settings.from_env(), token_path=tmp_path / "t.json"), state_dir=tmp_path,
    )
    assert auth.status()["signedIn"] is True
    auth.sign_out()
    assert auth.status()["signedIn"] is False and auth.signed_out is True

def _tab(gid, title, kind="notes", eligible=0, supported=None, complete=None):
    from sheets_bridge.tabs import TabSummary

    supported = (kind == "notes") if supported is None else supported
    return TabSummary(gid=gid, title=title, rows=10, columns=18, kind=kind,
                      supported=supported, complete=supported if complete is None else complete,
                      eligible=eligible, missing=(), mapping={"drive_link": "I"}, note="")


_WORKBOOK = ("NEET English", [
    _tab(1, "Batch List", kind="other"),
    _tab(2, "(Concise Notes) Master", kind="computed", eligible=8),
    _tab(3, "Arjuna 3.0 (Concise Notes)", eligible=0),
    _tab(4, "Yakeen 3.0 (Concise Notes)", eligible=2),
    _tab(5, "Yakeen 2.0 (Concise Notes)", eligible=4),
    _tab(6, "Format (Concise Notes)", kind="template"),
    _tab(7, "Lakshya 2027 (Audio Notes)", eligible=2, complete=False),   # Drive Link but no Lecture Number
])


def test_a_workbook_profile_round_trips_and_expands_to_the_tabs_with_rows_ready(tmp_path, monkeypatch):
    import sheets_bridge.tabs as tabs_module
    from sheets_bridge import profiles as P

    wb = P.SheetTarget(name="NEET English", spreadsheet_id="1" + "w" * 20, gid=0, all_tabs=True, max_cost_inr=500.0)
    path = tmp_path / "sheets.json"
    P.save_profiles([wb], path)
    assert '"allTabs": true' in path.read_text(encoding="utf-8") and '"gid"' not in path.read_text(encoding="utf-8")
    loaded = P.load_profiles(path)[0]
    assert loaded.all_tabs and loaded.gid == 0 and loaded.public()["allTabs"] is True

    monkeypatch.setattr(tabs_module, "list_tabs", lambda service, sid: _WORKBOOK)
    title, subs, skipped = P.expand_workbook(object(), wb)
    assert title == "NEET English"
    assert [(t.name, t.gid) for t in subs] == [("NEET English / Yakeen 3.0 (Concise Notes)", 4), ("NEET English / Yakeen 2.0 (Concise Notes)", 5)]
    assert all(not t.all_tabs and t.max_cost_inr is None for t in subs)    # the cap is the workbook's, applied by the batch
    assert skipped == ["(Concise Notes) Master (computed)", "Arjuna 3.0 (Concise Notes) (nothing ready)",
                       "Format (Concise Notes) (template)", "Lakshya 2027 (Audio Notes) (no Lecture Number column)"]

    checked = P.verify_workbook(object(), wb)
    assert (checked["runnable"], checked["eligible"], checked["computed"]) == (3, 6, ["(Concise Notes) Master"])


def test_a_workbook_runs_each_ready_tab_under_one_budget(monkeypatch):
    import sheets_bridge.drive as drive_module
    import sheets_bridge.profiles as profiles_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch
    from sheets_bridge import profiles as P

    wb = P.SheetTarget(name="NEET English", spreadsheet_id="1" + "w" * 20, gid=0, all_tabs=True, max_cost_inr=100.0)
    subs = [P.SheetTarget(name="NEET English / A", spreadsheet_id=wb.spreadsheet_id, gid=4),
            P.SheetTarget(name="NEET English / B", spreadsheet_id=wb.spreadsheet_id, gid=5)]
    monkeypatch.setattr(profiles_module, "expand_workbook", lambda service, target: ("NEET English", subs, ["Master (computed)"]))

    seen: list = []

    class _Runner:
        def __init__(self, sheet, drive, options, **kwargs):
            seen.append(options)

        def run(self):
            from sheets_bridge.runner import RunSummary

            summary = RunSummary(run_id="R")
            summary.total_cost_inr = 60.0
            return summary

    monkeypatch.setattr(sheets_module, "SheetClient", lambda *a, **k: FakeSheet(_grid(_row(2))))
    monkeypatch.setattr(drive_module, "DriveClient", lambda service: FakeDrive())
    monkeypatch.setattr(batch, "Runner", _Runner)

    lines: list[str] = []
    result = batch.run_targets([wb], None, None, RunOptions(), out=lines.append)
    assert [r.name for r in result.results] == ["NEET English / A", "NEET English / B"]
    # the workbook's cap is one budget across its tabs: B starts with A's spend booked
    assert [(o.max_cost_inr, o.cost_spent_before) for o in seen] == [(100.0, 0.0), (100.0, 60.0)]
    assert any("skipped: Master (computed)" in line for line in lines)

    # nothing ready is a note, not a failure
    monkeypatch.setattr(profiles_module, "expand_workbook", lambda service, target: ("NEET English", [], ["A (nothing ready)"]))
    result = batch.run_targets([wb], None, None, RunOptions(), out=lines.append)
    assert result.results[0].note == "nothing ready" and result.results[0].error == ""


def test_cli_targets_accept_a_link_and_all_tabs():
    import types

    from dataclasses import replace

    from sheets_bridge.cli import explicit_target
    from sheets_bridge.settings import Settings

    defaults = replace(Settings.from_env(), spreadsheet_id="", gid=0)
    link = "https://docs.google.com/spreadsheets/d/1N6juvFsmHgLd8NfzWSU4gIOdmPcXrc159DuRxsVg4Ek/edit#gid=1461744415"

    assert explicit_target(types.SimpleNamespace(spreadsheet_id="", gid=0, all_tabs=False), defaults) is None
    target = explicit_target(types.SimpleNamespace(spreadsheet_id=link, gid=0, all_tabs=False), defaults)
    assert (target.spreadsheet_id, target.gid, target.all_tabs) == ("1N6juvFsmHgLd8NfzWSU4gIOdmPcXrc159DuRxsVg4Ek", 1461744415, False)
    target = explicit_target(types.SimpleNamespace(spreadsheet_id=link, gid=0, all_tabs=True), defaults)
    assert target.all_tabs and target.gid == 0
    with pytest.raises(BridgeError) as excinfo:
        explicit_target(types.SimpleNamespace(spreadsheet_id="", gid=12345, all_tabs=False), defaults)
    assert excinfo.value.code == "no_sheet"


def _signed_in_controller(tmp_path, monkeypatch):
    from dataclasses import replace

    from sheets_bridge import auth_session, webui
    from sheets_bridge.settings import Settings

    settings = replace(Settings.from_env(), token_path=tmp_path / "no-token.json")
    auth = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    auth._restored = True
    auth._account = auth_session.Account("a@pw.live", object(), object(), "sheets", "drive")
    monkeypatch.setattr(auth_session.Account, "fresh_services", lambda self: ("sheets", "drive"))
    return webui.RunController(settings, auth=auth)


def test_a_signed_in_operator_can_add_a_workbook_from_the_studio(tmp_path, monkeypatch):
    """The router passed ``owner`` to tabs and preview but not to the add
    route, which then died on a NameError the moment an account was signed in:
    the tab picker filled, Save answered "Failed to fetch"."""
    import sheets_bridge.tabs as tabs_module
    from sheets_bridge import profiles as P
    from sheets_bridge import webui

    path = tmp_path / "sheets.json"
    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(path))
    monkeypatch.setattr(tabs_module, "list_tabs", lambda service, sid: _WORKBOOK)
    controller = _signed_in_controller(tmp_path, monkeypatch)
    link = "https://docs.google.com/spreadsheets/d/1" + "w" * 20 + "/edit#gid=3"

    status, payload = webui.api_response(
        controller, "POST", "profiles", {"link": link, "name": "prefoundation", "allTabs": True}, "o1")

    assert status == 201, payload
    assert payload["profile"]["name"] == "prefoundation" and payload["profile"]["allTabs"] is True
    assert payload["checked"]["runnable"] >= 1                     # the access/column check really ran
    saved = P.load_profiles(path)
    assert [(t.name, t.all_tabs) for t in saved] == [("prefoundation", True)]


def test_a_signed_in_operator_can_add_one_tab_and_the_check_uses_their_account(tmp_path, monkeypatch):
    from sheets_bridge import profiles as P
    from sheets_bridge import webui

    path = tmp_path / "sheets.json"
    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(path))
    seen = []

    def fake_verify(sheets_service, target):
        seen.append((sheets_service, target.gid))
        return {"tabTitle": "Batch A", "eligible": 14, "columns": {"drive_link": "I"}}

    monkeypatch.setattr(P, "verify_target", fake_verify)
    controller = _signed_in_controller(tmp_path, monkeypatch)
    link = "https://docs.google.com/spreadsheets/d/1" + "w" * 20 + "/edit#gid=3"

    status, payload = webui.api_response(controller, "POST", "profiles", {"link": link, "name": "one tab"}, "o1")

    assert status == 201, payload
    assert seen == [("sheets", 3)]                                 # the signed-in account's client
    assert payload["checked"]["eligible"] == 14
    assert [t.name for t in P.load_profiles(path)] == ["one tab"]


def test_a_sheet_google_refuses_is_reported_and_not_saved(tmp_path, monkeypatch):
    from sheets_bridge import profiles as P
    from sheets_bridge import webui

    path = tmp_path / "sheets.json"
    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(path))

    def refuse(sheets_service, target):
        raise PermissionError("403 for https://sheets.googleapis.com/secret")

    monkeypatch.setattr(P, "verify_target", refuse)
    controller = _signed_in_controller(tmp_path, monkeypatch)
    link = "https://docs.google.com/spreadsheets/d/1" + "w" * 20 + "/edit#gid=3"

    status, payload = webui.api_response(controller, "POST", "profiles", {"link": link, "name": "no access"}, "o1")

    assert status == 502 and payload["error"] == "sheets_unavailable"
    assert payload["detail"].startswith("PermissionError") and "secret" not in payload["detail"]
    assert P.load_profiles(path) == ()                             # an unchecked sheet is never saved


def test_a_bug_inside_a_route_becomes_a_readable_500_not_a_dropped_connection(tmp_path, monkeypatch):
    """An uncaught exception in a request handler closes the socket, and the
    page can only say "Failed to fetch"."""
    from sheets_bridge import webui

    controller = _signed_in_controller(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise NameError("name 'owner' is not defined, with /private/path")

    monkeypatch.setattr(webui, "_profiles_list", broken)
    status, payload = webui.safe_api_response(controller, "GET", "profiles", None, "o1")

    assert status == 500 and payload["ok"] is False and payload["error"] == "internal_error"
    assert payload["detail"].startswith("NameError @ ")
    assert "private" not in payload["detail"]                       # class and location only
    # a healthy route is passed through untouched
    assert webui.safe_api_response(controller, "GET", "status", None, "o1")[0] == 200


def test_both_http_servers_answer_through_the_safe_wrapper():
    import inspect

    from sheets_bridge import studio_server, webui

    assert "safe_api_response(controller, method, route, body, owner)" in inspect.getsource(studio_server)
    handler_source = inspect.getsource(webui.make_handler) if hasattr(webui, "make_handler") else inspect.getsource(webui)
    assert handler_source.count("safe_api_response(controller,") >= 2


def test_the_run_preview_lists_every_tab_a_run_would_touch(tmp_path, monkeypatch):
    import types

    import sheets_bridge.tabs as tabs_module
    from dataclasses import replace

    from sheets_bridge import auth_session, webui
    from sheets_bridge import profiles as P
    from sheets_bridge.settings import Settings

    path = tmp_path / "sheets.json"
    monkeypatch.setenv("SHEETS_BRIDGE_PROFILES", str(path))
    P.save_profiles([
        P.SheetTarget(name="wb", spreadsheet_id="1" + "w" * 20, gid=0, all_tabs=True),
        P.SheetTarget(name="one", spreadsheet_id="1" + "w" * 20, gid=3),
        P.SheetTarget(name="gone", spreadsheet_id="1" + "w" * 20, gid=99),
    ], path)
    monkeypatch.setattr(tabs_module, "list_tabs", lambda service, sid: _WORKBOOK)

    settings = replace(Settings.from_env(), token_path=tmp_path / "no-token.json")
    auth = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    auth._restored = True
    auth._account = auth_session.Account("a@pw.live", object(), object(), "sheets", "drive")
    monkeypatch.setattr(auth_session.Account, "fresh_services", lambda self: ("sheets", "drive"))
    controller = webui.RunController(settings, auth=auth)

    status, payload = webui.api_response(controller, "POST", "preview", {"profiles": ["wb", "one", "gone"]}, "o1")
    assert status == 200, payload
    assert [(i["tab"], i["ready"], i["problem"]) for i in payload["items"]] == [
        ("Yakeen 3.0 (Concise Notes)", 2, ""),
        ("Yakeen 2.0 (Concise Notes)", 4, ""),
        ("Arjuna 3.0 (Concise Notes)", 0, ""),
        ("?", 0, "tab not found"),
    ]
    assert payload["totalReady"] == 6


# --- review dropdown ---------------------------------------------------------
def test_the_review_column_is_made_a_dropdown_on_the_data_rows():
    """R is a Review/Done chip a human clicks, not free text."""
    from sheets_bridge.sheets import SheetClient, TabInfo

    sent: list = []

    class _Service:
        def spreadsheets(self):
            return self

        def batchUpdate(self, **kwargs):  # noqa: N802 - Google's spelling
            sent.append(kwargs["body"])
            return self

        def execute(self, **kwargs):
            return {}

    client = SheetClient(_Service(), "sid", 0)
    client._tab = TabInfo("Tab", 0, 18, 100)
    client.layout = C.DEFAULT_LAYOUT
    client.ensure_review_validation(240)

    rule = sent[0]["requests"][0]["setDataValidation"]
    assert rule["range"] == {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 240,
                            "startColumnIndex": 17, "endColumnIndex": 18}     # R, data rows only
    assert rule["rule"]["condition"]["type"] == "ONE_OF_LIST"
    assert [v["userEnteredValue"] for v in rule["rule"]["condition"]["values"]] == ["Review", "Done", "Regenerate"]
    assert rule["rule"]["showCustomUi"] is True and rule["rule"]["strict"] is False


def test_a_sheet_that_refuses_the_dropdown_still_runs():
    """The dropdown is cosmetic: losing it must not cost a lecture."""
    class _Fussy(FakeSheet):
        def ensure_review_validation(self, row_count):
            raise RuntimeError("no permission for data validation")

    sheet = _Fussy(_grid(_row(2)), headers_present=False)
    summary, _calls, lines = _run(sheet, FakeDrive())
    assert summary.counts()[OUTCOME_DONE] == 1
    assert any("could not set the review dropdown" in line for line in lines)
    assert sheet.writes[-1][1] == [C.STATUS_DONE, C.REVIEW_PENDING]


def test_the_old_auto_output_link_header_is_read_as_the_review_column():
    """Sheets created before the dropdown keep their header until it is
    renamed; it must map to the review column, not look like a human one."""
    layout = C.Layout.from_header(list(C.HUMAN_HEADERS) + ["Auto Status", "Auto Output Link"])
    assert layout.problems() == []
    assert (layout.auto_status_index, layout.review_status_index) == (16, 17)
    assert layout.describe()["review_status"] == "R"


# --- live progress -----------------------------------------------------------
def test_each_row_reports_its_place_in_the_run():
    """A lecture takes minutes: the log and the page must show how far the
    batch has got while it is still working, not only at the end."""
    sheet = FakeSheet(_grid(_row(2), _row(3)))
    events: list = []
    lines: list[str] = []
    runner = Runner(
        sheet, FakeDrive(), RunOptions(), generate_fn=_fake_generate([]),
        run_id="run-1", out=lines.append, clock=lambda: "T",
        on_progress=events.append,
    )
    runner.run()

    assert any(line.startswith("[1/2 row 2]") for line in lines)
    assert any(line.startswith("[2/2 row 3]") for line in lines)
    # row 3 points at the same source file, so its notes come from the cache
    assert "progress: 1 done, 0 failed, 1 left (INR 4.50 so far)" in lines
    assert any(line.startswith("progress: 2 done, 0 failed, 0 left") for line in lines)
    assert [(e["position"], e["total"], e["done"]) for e in events] == [(1, 2, 1), (2, 2, 2)]


def test_a_broken_progress_callback_never_fails_the_run():
    def explode(_event):
        raise RuntimeError("the page went away")

    sheet = FakeSheet(_grid(_row(2)))
    runner = Runner(sheet, FakeDrive(), RunOptions(), generate_fn=_fake_generate([]),
                    run_id="run-1", out=lambda _l: None, clock=lambda: "T", on_progress=explode)
    assert runner.run().counts()[OUTCOME_DONE] == 1


def test_the_batch_tags_progress_with_the_sheet_it_is_on(monkeypatch):
    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch, profiles

    monkeypatch.setattr(sheets_module, "SheetClient", lambda *a, **k: FakeSheet(_grid(_row(2))))
    monkeypatch.setattr(drive_module, "DriveClient", lambda service: FakeDrive())

    class _Runner:
        def __init__(self, sheet, drive, options, **kwargs):
            self.on_progress = kwargs.get("on_progress")

        def run(self):
            from sheets_bridge.runner import RunSummary

            self.on_progress({"row": 2, "outcome": "done", "position": 1, "total": 1, "done": 1, "failed": 0})
            return RunSummary(run_id="R")

    monkeypatch.setattr(batch, "Runner", _Runner)
    seen: list = []
    target = profiles.SheetTarget(name="Yakeen 3.0", spreadsheet_id="1" + "s" * 20, gid=7)
    batch.run_targets([target], None, None, RunOptions(), on_progress=seen.append)
    assert seen == [{"row": 2, "outcome": "done", "position": 1, "total": 1,
                     "done": 1, "failed": 0, "sheet": "Yakeen 3.0"}]


# --- one summary row per run -------------------------------------------------
def test_one_summary_row_is_written_per_run_not_per_lecture(monkeypatch):
    """The proxy already logs every model call; the run's own total is what
    nobody can read off those rows, so it gets exactly one row of its own."""
    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch, profiles, run_log

    monkeypatch.setattr(sheets_module, "SheetClient", lambda *a, **k: FakeSheet(_grid(_row(2))))
    monkeypatch.setattr(drive_module, "DriveClient", lambda service: FakeDrive())

    class _Runner:
        def __init__(self, sheet, drive, options, **kwargs):
            pass

        def run(self):
            from sheets_bridge.runner import RowOutcome, RunSummary

            summary = RunSummary(run_id="R")
            summary.outcomes = [RowOutcome(2, OUTCOME_DONE, cost_inr=9.5), RowOutcome(3, OUTCOME_DONE, cost_inr=4.5)]
            summary.total_cost_inr = 14.0
            return summary

    monkeypatch.setattr(batch, "Runner", _Runner)
    appended: list = []
    monkeypatch.setattr(run_log, "append_run", lambda service, **kw: appended.append(kw) or "Handwritten Notes Runs")

    targets = [profiles.SheetTarget(name="Yakeen 3.0", spreadsheet_id="1" + "s" * 20, gid=7),
               profiles.SheetTarget(name="Yakeen 2.0", spreadsheet_id="1" + "s" * 20, gid=8)]
    lines: list[str] = []
    batch.run_targets(targets, None, None, RunOptions(), operator="a@pw.live", out=lines.append)

    assert len(appended) == 1                      # two sheets, four lectures, ONE row
    row = appended[0]
    assert (row["lectures_done"], row["failed"], row["total_cost_inr"]) == (4, 0, 28.0)
    assert row["operator"] == "a@pw.live" and row["sheets"] == "Yakeen 3.0, Yakeen 2.0"
    assert any("run logged to 'Handwritten Notes Runs'" in line for line in lines)

    # a dry run writes nothing at all
    appended.clear()
    batch.run_targets(targets, None, None, RunOptions(dry_run=True), out=lines.append)
    assert appended == []

    # nor does a run that found everything already generated
    class _Idle(_Runner):
        def run(self):
            from sheets_bridge.runner import RunSummary

            return RunSummary(run_id="R")

    monkeypatch.setattr(batch, "Runner", _Idle)
    batch.run_targets(targets, None, None, RunOptions(), out=lines.append)
    assert appended == []


def test_the_run_summary_never_fails_the_run_and_can_be_switched_off(monkeypatch):
    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch, profiles, run_log

    monkeypatch.setattr(sheets_module, "SheetClient", lambda *a, **k: FakeSheet(_grid(_row(2))))
    monkeypatch.setattr(drive_module, "DriveClient", lambda service: FakeDrive())
    target = profiles.SheetTarget(name="s", spreadsheet_id="1" + "s" * 20, gid=7)

    def explode(service, **kw):
        raise RuntimeError("no access to the log workbook")

    monkeypatch.setattr(run_log, "append_run", explode)
    lines: list[str] = []
    summary = batch.run_targets([target], None, None, RunOptions(), out=lines.append)
    assert summary.results[0].error == ""                       # the run still succeeded
    assert any("could not write the run summary" in line for line in lines)

    monkeypatch.setenv("SHEETS_BRIDGE_LOG_SHEET", "off")
    assert run_log.enabled() is False
    monkeypatch.setenv("SHEETS_BRIDGE_LOG_SHEET", "1other")
    assert run_log.enabled() and run_log.sheet_id() == "1other"


def test_the_run_log_targets_the_pw_workbook_and_never_usage_cost():
    from sheets_bridge import run_log

    assert run_log.DEFAULT_SHEET_ID == "1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE"
    assert run_log.DEFAULT_TAB_TITLE == "Handwritten Notes Runs"   # never 'Usage Cost 2.0'
    assert run_log.HEADERS[4:] == ("Lectures Done", "Failed", "Total Cost (INR)", "Status")


# --- sign-in is restricted to allowed accounts -------------------------------
def _flow_stub(monkeypatch, email, *, captured=None):
    """Stand in for Google's browser flow, returning one account."""
    import sheets_bridge.google_auth as ga

    class _Flow:
        def run_local_server(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return object()

    class _Loader:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return _Flow()

    import google_auth_oauthlib.flow as flow_module
    monkeypatch.setattr(flow_module, "InstalledAppFlow", _Loader)
    monkeypatch.setattr(ga, "user_email", lambda creds: email)


def test_the_account_chooser_is_asked_for_the_allowed_domain(tmp_path, monkeypatch):
    import sheets_bridge.google_auth as ga
    registration = tmp_path / "client.json"
    registration.write_text('{"installed": {}}')

    seen: dict = {}
    _flow_stub(monkeypatch, "someone@pw.live", captured=seen)
    monkeypatch.setattr(ga, "_save", lambda creds, path: None)
    ga.interactive_sign_in(registration, base=tmp_path)
    assert seen["hd"] == "pw.live" and seen["prompt"] == "consent"


def test_a_rejected_sign_in_leaves_no_token_on_the_machine(tmp_path, monkeypatch):
    """The refresh token must not be written before the account is allowed:
    a stranger who completes Google consent would otherwise leave a usable
    credential in ~/.shortnotes_bridge/tokens."""
    import sheets_bridge.google_auth as ga
    registration = tmp_path / "client.json"
    registration.write_text('{"installed": {}}')

    _flow_stub(monkeypatch, "outsider@gmail.com")
    saved: list = []
    monkeypatch.setattr(ga, "_save", lambda creds, path: saved.append(path))

    def verify(creds, email):
        raise BridgeError("not_pw_account", f"{email} is not an @pw.live account")

    with pytest.raises(BridgeError) as excinfo:
        ga.interactive_sign_in(registration, base=tmp_path, verify=verify)
    assert excinfo.value.code == "not_pw_account"
    assert saved == []                                   # nothing persisted
    assert list((tmp_path / "tokens").glob("*.json")) == []

    # an allowed account is saved exactly once
    _flow_stub(monkeypatch, "ok@pw.live")
    ga.interactive_sign_in(registration, base=tmp_path, verify=lambda creds, email: None)
    assert len(saved) == 1


def test_sign_in_checks_the_domain_then_the_allowlist(tmp_path, monkeypatch):
    from dataclasses import replace

    import sheets_bridge.pw_gate as pw_gate
    from sheets_bridge import auth_session
    from sheets_bridge.settings import Settings

    calls: list = []

    class _Access:
        def __init__(self, token_provider, *, email=""):
            self.email = email

        def domain_ok(self):
            calls.append(("domain", self.email))
            return self.email.endswith("@pw.live")

        def ensure_allowed(self):
            calls.append(("allowlist", self.email))
            if self.email == "notlisted@pw.live":
                raise BridgeError("not_authorized", "ask the owner to add the email")

    monkeypatch.setattr(pw_gate, "ProxyAccess", _Access)
    auth = auth_session.AutomationAuth(replace(Settings.from_env(), token_path=tmp_path / "t.json"),
                                       state_dir=tmp_path)

    # a personal account never reaches the allowlist check
    with pytest.raises(BridgeError) as outsider:
        auth._verify(object(), "someone@gmail.com")
    assert outsider.value.code == "not_pw_account" and calls == [("domain", "someone@gmail.com")]

    # a PW account that is not whitelisted is refused too
    calls.clear()
    with pytest.raises(BridgeError) as unlisted:
        auth._verify(object(), "notlisted@pw.live")
    assert unlisted.value.code == "not_authorized"
    assert calls == [("domain", "notlisted@pw.live"), ("allowlist", "notlisted@pw.live")]

    calls.clear()
    auth._verify(object(), "listed@pw.live")
    assert calls == [("domain", "listed@pw.live"), ("allowlist", "listed@pw.live")]


# --- moving to another OAuth client ------------------------------------------
def test_a_token_from_another_oauth_client_is_ignored(tmp_path):
    """Swapping to the pw.live Internal client must ask for a fresh sign-in,
    not fail later with Google's opaque invalid_client."""
    import json

    from sheets_bridge import google_auth as ga
    from sheets_bridge.settings import SCOPES

    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "NEW.apps.googleusercontent.com",
                                                "client_secret": "s"}}), encoding="utf-8")
    assert ga.client_id_of(secret) == "NEW.apps.googleusercontent.com"
    assert ga.client_id_of(tmp_path / "missing.json") == ""

    def write(client_id):
        (tmp_path / "token.json").write_text(json.dumps({
            "client_id": client_id, "client_secret": "s", "refresh_token": "r",
            "scopes": list(SCOPES),
        }), encoding="utf-8")
        return tmp_path / "token.json"

    # a token from the old client is ignored, so the operator just signs in again
    assert ga._load_cached(write("OLD.apps.googleusercontent.com"), "NEW.apps.googleusercontent.com") is None
    # the same file under its own client still loads
    assert ga._load_cached(write("NEW.apps.googleusercontent.com"), "NEW.apps.googleusercontent.com") is not None
    # and with no client configured nothing is rejected on that basis
    assert ga._load_cached(write("OLD.apps.googleusercontent.com"), "") is not None


def test_check_setup_reports_tokens_from_a_different_client(tmp_path, capsys):
    import json
    from dataclasses import replace

    from sheets_bridge.settings import Settings
    from sheets_bridge.setup_check import _token_clients

    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "NEW", "client_secret": "s"}}), encoding="utf-8")
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "a.json").write_text('{"client_id": "OLD"}', encoding="utf-8")
    (tmp_path / "tokens" / "b.json").write_text('{"client_id": "NEW"}', encoding="utf-8")
    settings = replace(Settings.from_env(), client_secret_path=secret, token_path=tmp_path / "token.json")
    assert _token_clients(settings) == (1, 1)


# --- the end-of-run cost summary ---------------------------------------------
def _batch_with(outcomes_per_sheet, *, interrupted=False, monkeypatch=None):
    """Run batch.run_targets over fake sheets, capturing the logged row."""
    import sheets_bridge.drive as drive_module
    import sheets_bridge.sheets as sheets_module
    from sheets_bridge import batch, profiles, run_log

    seq = iter(outcomes_per_sheet)

    class _Runner:
        def __init__(self, *a, **k):
            pass

        def run(self):
            from sheets_bridge.runner import RunSummary

            summary = RunSummary(run_id="R")
            summary.outcomes = next(seq)
            summary.total_cost_inr = sum(o.cost_inr for o in summary.outcomes)
            summary.interrupted = interrupted
            return summary

    monkeypatch.setattr(sheets_module, "SheetClient", lambda *a, **k: object())
    monkeypatch.setattr(drive_module, "DriveClient", lambda service: object())
    monkeypatch.setattr(batch, "Runner", _Runner)
    rows: list = []
    monkeypatch.setattr(run_log, "append_run", lambda service, **kw: rows.append(kw) or run_log.DEFAULT_TAB_TITLE)
    targets = [profiles.SheetTarget(name=f"S{i}", spreadsheet_id="1" + "s" * 20, gid=i)
               for i in range(len(outcomes_per_sheet))]
    lines: list[str] = []
    summary = batch.run_targets(targets, None, None, RunOptions(), operator="a@pw.live", out=lines.append)
    return summary, rows, lines


def test_the_run_reports_one_combined_cost_and_lecture_count(monkeypatch):
    """One press of the button -> one row: how many lectures, what it cost."""
    summary, rows, lines = _batch_with([
        [RowOutcome(2, OUTCOME_DONE, cost_inr=10.61), RowOutcome(3, OUTCOME_DONE, cost_inr=14.34),
         RowOutcome(4, OUTCOME_FAILED, code="generate:x", cost_inr=2.10)],
        [RowOutcome(2, OUTCOME_DONE, cost_inr=8.82), RowOutcome(3, OUTCOME_DONE, cost_inr=11.32)],
    ], monkeypatch=monkeypatch)

    assert len(rows) == 1                       # two sheets, five rows, ONE summary
    row = rows[0]
    assert row["lectures_done"] == 4 and row["failed"] == 1
    assert round(row["total_cost_inr"], 2) == 47.19      # includes the failed row's partial spend
    assert row["sheets"] == "S0, S1" and row["status"] == "completed"
    assert any("4 lecture(s), 1 failed, INR 47.19" in line for line in lines)


def test_a_run_you_stopped_is_logged_as_stopped_with_what_it_did(monkeypatch):
    """Pressing Stop still reports the lectures already paid for - and says the
    run was cut short, so the log cannot be misread as a full run."""
    _summary, rows, _lines = _batch_with([
        [RowOutcome(2, OUTCOME_DONE, cost_inr=10.61), RowOutcome(3, OUTCOME_DONE, cost_inr=9.40),
         RowOutcome(4, OUTCOME_STOPPED, code="stop_requested")],
    ], interrupted=True, monkeypatch=monkeypatch)

    assert rows[0]["status"] == "stopped"
    assert rows[0]["lectures_done"] == 2 and round(rows[0]["total_cost_inr"], 2) == 20.01

    # the cost cap is a different reason to stop, and says so
    _summary, rows, _lines = _batch_with([
        [RowOutcome(2, OUTCOME_DONE, cost_inr=30.0), RowOutcome(3, OUTCOME_STOPPED, code="cost_cap")],
    ], monkeypatch=monkeypatch)
    assert rows[0]["status"] == "stopped: cost cap" and rows[0]["lectures_done"] == 1


def test_the_summary_row_lands_at_the_bottom_rounded_and_readable(monkeypatch):
    """A blank row once split the table and append() wrote a newer run ABOVE an
    older one; the stamp also rendered as its serial number. The row is now
    placed by counting, and the timestamp column is formatted."""
    from sheets_bridge import run_log

    sent: dict = {"updated": [], "formats": [], "read": []}
    # column A: header, one run, a blank row, another run -> next free row is 5
    column_a = [["Run ID"], ["20260826-1"], [], ["20260826-2"]]

    class _Values:
        def get(self, **kw):
            sent["read"].append(kw["range"])
            self._range = kw["range"]
            return self

        def update(self, **kw):
            sent["updated"].append((kw["range"], kw["body"]["values"][0]))
            return self

        def execute(self, **kw):
            if getattr(self, "_range", "").endswith("!A:A"):
                return {"values": column_a}
            return {"values": [list(run_log.HEADERS)]}      # header already current

    class _Sheets:
        def values(self):
            return _Values()

        def get(self, **kw):
            return self

        def batchUpdate(self, **kw):  # noqa: N802 - Google's spelling
            sent["formats"].append(kw["body"]["requests"][0])
            return self

        def execute(self, **kw):
            return {"sheets": [{"properties": {"sheetId": 77, "title": run_log.DEFAULT_TAB_TITLE}}]}

    class _Service:
        def spreadsheets(self):
            return _Sheets()

    run_log.append_run(_Service(), run_id="R1", operator="a@pw.live", sheets="S",
                       lectures_done=2, failed=0, total_cost_inr=20.009999999999998, status="stopped")

    where, row = sent["updated"][0]
    assert where.endswith("!A5")                       # appended below the blank row, not into it
    assert row[-2:] == [20.01, "stopped"]              # rounded, and labelled
    fmt = sent["formats"][0]["repeatCell"]
    assert fmt["range"] == {"sheetId": 77, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2}
    assert fmt["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "DATE_TIME"

def test_the_run_uses_the_signed_in_account_without_a_second_sign_in(tmp_path, monkeypatch):
    """_services must take the account the run was started with. It once
    referenced an `account` local that did not exist there, so every run with
    a signed-in server died with UnboundLocalError before touching a sheet."""
    from dataclasses import replace

    from sheets_bridge import auth_session, webui
    from sheets_bridge.settings import Settings

    installed: list = []

    class _Access:
        def install_transport(self, model=""):
            installed.append(True)
            return "gemini-3.6-flash"

    settings = replace(Settings.from_env(), token_path=tmp_path / "none.json")
    auth = auth_session.AutomationAuth(settings, state_dir=tmp_path)
    auth._restored = True                      # nothing to restore in this test
    account = auth_session.Account("a@pw.live", object(), _Access(), "sheets-svc", "drive-svc")
    controller = webui.RunController(settings, auth=auth)

    # the account handed to the run is used, and no browser sign-in happens
    monkeypatch.setattr(webui.RunController, "_emit", lambda self, line: None)
    assert controller._services({"dryRun": True}, account) == ("sheets-svc", "drive-svc", None, "a@pw.live")
    assert installed == []                     # a dry run needs no provider transport

    sheets, drive, access, email = controller._services({}, account)
    assert (sheets, drive, email) == ("sheets-svc", "drive-svc", "a@pw.live")
    assert access is account.access and installed == [True]

    # with no account passed it falls back to the server's current one
    auth._account = account
    assert controller._services({"dryRun": True})[3] == "a@pw.live"

    # and with neither, it refuses instead of half-running
    auth._account = None
    with pytest.raises(BridgeError) as excinfo:
        controller._services({"dryRun": True})
    assert excinfo.value.code == "sign_in_required"


# --- the shared ledger: one unit (pages), the same count on every call --------
def test_the_ledger_records_pages_with_one_stable_unit(monkeypatch):
    """The proxy rolls a task's calls up per file; the app must use ONE unit
    and repeat the lecture's page count on every call of that lecture. The
    old 'No. of rows' variant is what made this app's volume unreadable."""
    import sheets_bridge.pw_transport as transport
    from sheets_bridge import generator

    tasks: list = []
    monkeypatch.setattr(transport, "set_task", lambda **kw: tasks.append(kw))
    monkeypatch.setattr(generator, "page_count", lambda data: 17)
    monkeypatch.setattr(generator, "generate_complete_notes",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after set_task")),
                        raising=False)

    def call(**extra):
        tasks.clear()
        try:
            generator.generate_notes_pdf(b"%PDF", language="english", subject="physics",
                                         signature="by A", filename="L01.pdf", task_id="task-run-1", **extra)
        except Exception:  # noqa: BLE001 - we only care about what set_task received
            pass
        return tasks[0]

    sent = call()
    assert sent["input_unit"] == "No. of pages" and sent["count"] == 17
    assert sent["task_id"] == "task-run-1" and sent["filename"] == "L01.pdf"


def test_the_runner_shares_one_task_id_across_the_run(monkeypatch):
    """One task per button press; the proxy splits it per lecture file."""
    seen: list = []

    def generate(pdf_bytes, *, language, subject, signature, filename="", task_id="", **extra):
        assert "row_count" not in extra                 # the rows unit is gone
        seen.append((task_id, filename))
        return _approved_result(pdf=b"%PDF ok", notes_json=_notes_json(), cost_inr=1.0, task_id=task_id)

    _clear_cache()
    sheet = FakeSheet(_grid(_distinct_row(2), _distinct_row(3)))
    Runner(sheet, FakeDrive(), RunOptions(), generate_fn=generate, run_id="r", out=lambda _l: None,
           clock=lambda: "T", final_clock=lambda: "F", access=GateStub(), task_id="task-run-1").run()

    assert [task for task, _ in seen] == ["task-run-1", "task-run-1"]
    assert len({name for _, name in seen}) == 2                 # two lecture files


def test_a_restart_restores_the_token_the_page_signed_in_with(tmp_path, monkeypatch):
    """The page files tokens per account; only the older CLI path writes
    token.json. Restoring must look at both, or every restart would ask the
    operator to sign in again."""
    import json

    from sheets_bridge import google_auth as ga
    from sheets_bridge.settings import SCOPES

    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "CID", "client_secret": "s"}}), encoding="utf-8")

    def write(path, client_id="CID"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"client_id": client_id, "client_secret": "s",
                                    "refresh_token": "r", "scopes": list(SCOPES)}), encoding="utf-8")
        return path

    monkeypatch.setattr(ga, "_refreshed", lambda creds, path: creds)   # treat any cached token as usable

    # nothing anywhere
    assert ga.load_any_cached(secret, tmp_path / "token.json", tmp_path) is None

    # a token filed by the page's sign-in is found, with no legacy token.json
    write(tmp_path / "tokens" / "aaa.json")
    assert ga.load_any_cached(secret, tmp_path / "token.json", tmp_path) is not None

    # a token from a different OAuth client is skipped, not adopted
    (tmp_path / "tokens" / "aaa.json").unlink()
    write(tmp_path / "tokens" / "old.json", client_id="OTHER")
    assert ga.load_any_cached(secret, tmp_path / "token.json", tmp_path) is None

    # ... and the legacy path still works on its own
    write(tmp_path / "token.json")
    assert ga.load_any_cached(secret, tmp_path / "token.json", tmp_path) is not None


def test_latin_after_devanagari_keeps_the_latin_face():
    """Drawing Devanagari selects the fallback face in the content stream, and
    asking for the base family again is a no-op inside fpdf2 - so the next
    purely Latin run was drawn through Kalam and came out as Devanagari. The
    mind-map centre showed 'मानव जनन (Human' then nonsense for 'Reproduction)'."""
    from notesbot import notes_pdf
    from sheets_bridge import pdf_fonts

    pdf_fonts._INSTALLED = False
    assert pdf_fonts.install() is True

    pdf = notes_pdf.NotesPDF(_sample_notes(), "by X", watermark=False)
    pdf.add_page()
    pdf.set_font("Patrick", "", 13.5)

    # a Devanagari draw marks the instance as having used the fallback ...
    pdf.cell(60, 6.2, "मानव जनन (Human")
    assert pdf._after_fallback_run is True

    # ... and the next Latin draw clears it, having reselected the base face
    pdf.cell(60, 6.2, "Reproduction)")
    assert pdf._after_fallback_run is False
    assert (pdf.font_family, round(pdf.font_size_pt, 1)) == ("patrick", 13.5)

    # a Latin-only document never triggers the bounce
    fresh = notes_pdf.NotesPDF(_sample_notes(), "by X", watermark=False)
    fresh.add_page()
    fresh.set_font("Patrick", "", 13.5)
    fresh.cell(60, 6.2, "Reproduction)")
    assert getattr(fresh, "_after_fallback_run", False) is False


# --- starting fresh -----------------------------------------------------------
def test_the_notes_cache_can_be_cleared_and_switched_off(tmp_path, monkeypatch):
    """After bad notes are deleted, the next run must pay to generate again
    rather than re-rendering the same wrong bytes from disk."""
    from sheets_bridge import output_cache

    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", str(tmp_path))
    key = output_cache.key(file_id="f", out_name="o", language="hindi", subject="biology", signature="s")
    assert output_cache.enabled() is True
    assert output_cache.store(key, b'{"notes": 1}') is True
    assert output_cache.load(key) == b'{"notes": 1}'

    assert output_cache.clear() == 1
    assert output_cache.load(key) is None
    assert output_cache.clear() == 0                       # idempotent

    # switched off: nothing is read and nothing is written
    output_cache.store(key, b'{"notes": 1}')
    for value in ("off", "none", "0", "false", "NO"):
        monkeypatch.setenv("SHEETS_BRIDGE_CACHE", value)
        assert output_cache.enabled() is False
        assert output_cache.load(key) is None
        assert output_cache.store(key, b'{"notes": 2}') is False
        assert output_cache.cache_dir() == output_cache.DEFAULT_DIR   # not a directory named "off"


def test_the_cli_finds_a_token_filed_by_the_page(tmp_path, monkeypatch):
    """The Studio's sign-in writes tokens/<hash>.json, so a CLI reading only
    the legacy token.json refused to run on a machine that was signed in."""
    import json

    from sheets_bridge import google_auth as ga
    from sheets_bridge.settings import SCOPES

    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "CID", "client_secret": "s"}}), encoding="utf-8")
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "acct.json").write_text(json.dumps({
        "client_id": "CID", "client_secret": "s", "refresh_token": "r", "scopes": list(SCOPES),
    }), encoding="utf-8")

    sentinel = object()
    # stand in for the refresh, but only when a token actually loaded
    monkeypatch.setattr(ga, "_refreshed", lambda creds, path: sentinel if creds is not None else None)
    # no legacy token.json at all, and interactive disabled: it still resolves
    assert ga.load_credentials(secret, tmp_path / "token.json", interactive=False) is sentinel

    # with nothing anywhere it still refuses rather than silently continuing
    (tokens / "acct.json").unlink()
    with pytest.raises(BridgeError) as excinfo:
        ga.load_credentials(secret, tmp_path / "token.json", interactive=False)
    assert excinfo.value.code == "auth_required"


# --- speed: page groups run concurrently --------------------------------------
def test_page_groups_run_concurrently_but_merge_in_lecture_order(monkeypatch):
    """A 44-page lecture was twelve calls back to back - minutes for one row.
    Groups now overlap, yet the merge order must stay the lecture's own."""
    import threading
    import time as _time

    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "3")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)

    inflight, peak, lock = 0, [0], threading.Lock()
    started: list[int] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        nonlocal inflight
        from pypdf import PdfReader
        import io as _io

        first = PdfReader(_io.BytesIO(pdf_bytes)).pages[0]
        with lock:
            inflight += 1
            peak[0] = max(peak[0], inflight)
            started.append(len(started))
        _time.sleep(0.05)
        with lock:
            inflight -= 1
        # tag the notes so the merge order is checkable
        n = len(started)
        return ShortNotes(lecture_no="", subject="Zoology", title="L",
                          sections=[Section(heading=f"G{n}", points=["a"] * 8)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    coverage.generate_complete_notes(_pdf_with_pages(24), language="hindi",
                                     subject_hint="biology", group_size=4, log=lines.append)

    assert peak[0] > 1, "groups must overlap"
    assert peak[0] <= 3, "never more than the configured workers"
    assert any("up to 3 page group(s) at a time" in line for line in lines)
    # the per-group log lines still name their own pages, in lecture order
    reported = [l for l in lines if "section(s)," in l]
    assert [l.split(":")[0] for l in reported] == [
        "pages 1-4", "pages 5-8", "pages 9-12", "pages 13-16", "pages 17-20", "pages 21-24",
    ]


def test_the_worker_count_is_bounded_and_tunable(monkeypatch):
    from sheets_bridge import coverage

    monkeypatch.delenv("SHEETS_BRIDGE_GROUP_WORKERS", raising=False)
    assert coverage.group_workers() == coverage.DEFAULT_GROUP_WORKERS
    for raw, expected in (("1", 1), ("4", 4), ("0", 1), ("-2", 1), ("99", 6), ("abc", coverage.DEFAULT_GROUP_WORKERS)):
        monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", raw)
        assert coverage.group_workers() == expected, raw


def test_each_group_is_judged_by_its_own_failure(monkeypatch):
    """last_error used to be shared across groups, so an edge group could be
    skipped - or failed - on the strength of a different group's error."""
    from notesbot.gemini_client import UserError
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(coverage, "RETRY_BACKOFF_SECONDS", 0.0)

    from pypdf import PdfReader
    import io as _io

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        # the LAST group is a cover slide with no study content; the middle
        # groups are fine. The first group also fails, but on a gateway error.
        pages = len(PdfReader(_io.BytesIO(pdf_bytes)).pages)
        if pages == 1:                      # the trailing single page
            raise UserError("Gemini se complete theory notes nahi mile.")
        return ShortNotes(lecture_no="", subject="Zoology", title="L",
                          sections=[Section(heading="H", points=["a"] * 8)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(9), language="hindi",
                                              subject_hint="biology", group_size=4, log=lines.append)
    # 9 pages -> 4 + 4 + 1; the trailing 1-page group is dropped as an edge
    assert any("pages 9-9: no study content" in line for line in lines)
    assert merged.sections and len(merged.sections) >= 1


def test_a_group_the_model_chokes_on_is_retried_as_halves(monkeypatch):
    """A whole group timing out is often the group's size, not its content, so
    the pages that do carry content are rescued instead of failing the row."""
    from notesbot.errors import VisualFidelityError
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(coverage, "RETRY_BACKOFF_SECONDS", 0.0)

    from pypdf import PdfReader
    import io as _io

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        # four pages at once is too much; any smaller slice answers fine
        if len(PdfReader(_io.BytesIO(pdf_bytes)).pages) >= 4:
            raise VisualFidelityError("deadline_exceeded")
        return ShortNotes(lecture_no="", subject="Zoology", title="L",
                          sections=[Section(heading="H", points=["a"] * 8)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(8), language="hindi",
                                              subject_hint="biology", group_size=4, log=lines.append)
    assert merged.sections                                   # the lecture survived
    assert any("retrying as 2 smaller call(s)" in line for line in lines)
    assert any("recovered as 2 smaller call(s)" in line for line in lines)


def test_a_half_that_answers_is_kept_when_the_other_half_has_nothing(monkeypatch):
    """Partial notes beat no notes: the slice that answers ships, the empty
    one is named in the log."""
    from notesbot.errors import UserError
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(coverage, "RETRY_BACKOFF_SECONDS", 0.0)

    from pypdf import PdfReader
    import io as _io

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        # pages 5-6 never answer, whatever the slice
        text = PdfReader(_io.BytesIO(pdf_bytes)).pages[0].extract_text()
        if "Slide 5" in text:
            raise UserError("Gemini se complete theory notes nahi mile.")
        return ShortNotes(lecture_no="", subject="Zoology", title="L",
                          sections=[Section(heading="H", points=["a"] * 8)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(12), language="hindi",
                                              subject_hint="biology", group_size=4, log=lines.append)
    assert merged.sections
    assert any("recovered 1 of 2 smaller call(s) - the rest had no study content" in line for line in lines)
    assert not any("would be incomplete" in line for line in lines)


def test_the_hindi_rules_demand_devanagari_everywhere_including_the_mindmap():
    from notesbot.prompts import _LANGUAGE_RULES
    from sheets_bridge.coverage import language_instruction

    rule = _LANGUAGE_RULES["hindi"]
    assert "90%" in rule
    for field in ("key_terms", "mindmap", "notes", "tricks", "table"):
        assert field in rule, field
    # the old rule sent every technical term to English - that is what filled
    # Hindi notes with English definitions
    assert "keep all technical terms" not in rule.lower()

    hinglish_request = language_instruction("hindi")
    assert "90%" in hinglish_request and "mindmap" in hinglish_request
    assert language_instruction("english").startswith("Write every section heading")


# --- Hindi is measured, not just requested ------------------------------------
def test_hindi_share_counts_words_and_ignores_maths_symbols():
    from sheets_bridge.coverage import hindi_share

    assert hindi_share("आव्यूह का योग दो आव्यूहों को जोड़ने की प्रक्रिया है") == 1.0
    assert hindi_share("Scalar multiplication requires multiplying every element") == 0.0
    # single-letter symbols are maths, not English
    assert hindi_share("आव्यूह A का ऋणात्मक -A = (-1)A होता है, kA = k[aij], sin x") == 1.0
    assert hindi_share("") == 0.0
    mixed = hindi_share("आव्यूह का योग (Addition) संगत अवयवों को जोड़कर")
    assert 0.5 < mixed < 1.0


def test_the_lectures_own_language_is_detected_when_the_sheet_says_auto():
    from sheets_bridge.coverage import detect_source_language

    assert detect_source_language(_pdf_with_text_pages(["आव्यूह का योग एवं अंतर", "ऋणात्मक आव्यूह"])) == "hindi"
    assert detect_source_language(_pdf_with_text_pages(["Matrix basics and order", "Row and column"])) == ""
    assert detect_source_language(b"not a pdf at all") == ""


def test_an_english_heavy_answer_for_a_hindi_lecture_is_asked_again(monkeypatch):
    """The prompt asks for 90% Hindi; nothing used to check. A matrices lecture
    came back with 12 of 16 headings in English and was uploaded as-is."""
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)

    english = ShortNotes(lecture_no="", subject="Maths", title="Matrices",
                         sections=[Section(heading="Matrix Basics and Order",
                                           points=["Corresponding elements equal"] * 6)])
    hindi = ShortNotes(lecture_no="", subject="गणित", title="आव्यूह",
                       sections=[Section(heading="आव्यूह की मूल बातें",
                                         points=["संगत अवयव समान होते हैं"] * 6)])
    seen: list[str] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        seen.append(instruction)
        return hindi if "DHYAN DEIN" in instruction else english

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(4), language="hindi",
                                              subject_hint="maths", group_size=4, log=lines.append)

    assert len(seen) == 2 and "DHYAN DEIN" in seen[1]        # asked once more, harder
    assert merged.sections[0].heading == "आव्यूह की मूल बातें"  # the Hindi answer won
    assert any("only 0% Hindi - asking again in Hindi (1/1)" in line for line in lines)
    assert any("now 100% Hindi" in line for line in lines)

    # an answer that is already Hindi is not re-asked
    seen.clear()
    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", lambda *a: hindi)
    coverage.generate_complete_notes(_pdf_with_pages(4), language="hindi", subject_hint="maths", group_size=4)
    # (the lambda records nothing; the absence of a re-ask line is the check)
    lines.clear()
    coverage.generate_complete_notes(_pdf_with_pages(4), language="hindi",
                                     subject_hint="maths", group_size=4, log=lines.append)
    assert not any("asking again" in line for line in lines)


def test_a_hindi_answer_above_the_shipping_floor_is_not_asked_again(monkeypatch):
    """74-87% Hindi used to buy a full re-ask for a 2-point gain; the notes
    ship at 70%, so only answers below that are asked again."""
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    calls = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        calls.append(instruction)
        return ShortNotes(lecture_no="", subject="", title="", sections=[Section(heading="कोशिका", points=[
            "कोशिका जीवन की मूल इकाई है और इसमें केंद्रक उपस्थित होता है।",
            "माइटोकॉन्ड्रिया कोशिका में ऊर्जा बनाती है (mitochondria, powerhouse)।"])])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    share = coverage.notes_hindi_share(fake_generate(b"", "", "", ""))
    assert coverage.HINDI_REASK_BELOW <= share < coverage.MIN_HINDI_SHARE
    calls.clear()
    lines = []
    group = coverage.split_pages(_pdf_with_pages(1), 1)[0]      # 1 page: no thin-answer retry
    notes, _ = coverage._generate_group(group, instruction="hindi", language="hindi",
                                        subject_hint="", emit=lines.append, allow_split=0)
    assert notes is not None and len(calls) == 1
    assert not any("asking again" in line for line in lines)


def test_a_short_last_group_gets_no_review_of_its_own(monkeypatch):
    monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", "on")   # this test covers the review path
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", lambda *_a: ShortNotes(
        lecture_no="", subject="", title="", sections=[Section(heading="H", points=["a"] * 8)]))
    reviewed = []
    monkeypatch.setattr("notesbot.source_coverage.ensure_source_coverage",
                        lambda pdf, notes, **_: reviewed.append(pdf) or notes)
    monkeypatch.setattr("notesbot.source_coverage.consolidate_lecture", lambda _pdf, notes, **_: notes)
    lines = []
    coverage.generate_complete_notes(_pdf_with_pages(11), language="english", subject_hint="",
                                     group_size=8, log=lines.append, verify_source=True)
    assert len(reviewed) == 1                                    # pages 1-8 only
    assert any("pages 9-11: short last group - no separate source review" in line for line in lines)
    # a lecture that is one short group is still reviewed
    reviewed.clear()
    coverage.generate_complete_notes(_pdf_with_pages(3), language="english", subject_hint="",
                                     group_size=8, verify_source=True)
    assert len(reviewed) == 1


def test_the_per_group_source_review_is_off_by_default(monkeypatch):
    """Review plus repair was 55% of a lecture's cost; the automation now skips
    it unless SHEETS_BRIDGE_SOURCE_REVIEW=on. The duplicate pass still runs and
    the generator's own inner review stays off."""
    from notesbot.generation_profile import inline_source_check_enabled
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.delenv("SHEETS_BRIDGE_SOURCE_REVIEW", raising=False)
    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    inner = []

    def fake_generate(*_a):
        inner.append(inline_source_check_enabled())
        return ShortNotes(lecture_no="", subject="", title="", sections=[Section(heading="H", points=["a"] * 2)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    reviewed, deduped = [], []
    monkeypatch.setattr("notesbot.source_coverage.ensure_source_coverage",
                        lambda pdf, notes, **_: reviewed.append(pdf) or notes)
    monkeypatch.setattr("notesbot.source_coverage.consolidate_lecture",
                        lambda _pdf, notes, **_: deduped.append(1) or notes)
    lines = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(16), language="english", subject_hint="",
                                              group_size=8, log=lines.append, verify_source=True)
    assert merged.sections
    assert reviewed == []                                   # no review calls at all
    assert deduped == [1]                                   # the duplicate pass still runs
    assert inner == [False, False]                          # nor the generator's inner review
    assert any("per-group source review is off" in line for line in lines)
    assert not any("checking theory and formulas" in line for line in lines)
    assert not any("thin answer" in line for line in lines)  # no thin-answer re-ask either

    for raw, expected in (("on", True), ("ON", True), ("1", True), ("off", False), ("", False)):
        monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", raw)
        assert coverage.source_review_enabled() is expected, raw


def test_eight_pages_per_call_is_the_default():
    from sheets_bridge import coverage
    assert coverage.DEFAULT_PAGES_PER_CALL == 8
    assert len(coverage.split_pages(_pdf_with_pages(27), coverage.DEFAULT_PAGES_PER_CALL)) == 4


def test_every_proxy_call_is_logged_with_its_cost_and_summed_by_type():
    from notesbot.generation_profile import call_label, call_purpose
    from sheets_bridge import pw_transport

    lines = []
    pw_transport.set_task(task_id="t-1", filename="lecture.pdf")
    pw_transport.set_call_log(lines.append)
    with call_label("pages 1-8"):
        pw_transport._record_cost({"cost_inr": 4.5}, "ShortNotes")
        pw_transport._record_cost({"cost_inr": 1.25}, "CoverageReview")
        with call_purpose("repair"):
            pw_transport._record_cost({"cost_inr": 5.0}, "ShortNotes")
    pw_transport._record_cost({"cost_inr": 2.0}, "QualityReview")
    pw_transport._record_cost({"cost_inr": "n/a"}, "QualityReview")          # ignored
    assert lines == [
        "pages 1-8: notes call - INR 4.50",
        "pages 1-8: source review call - INR 1.25",
        "pages 1-8: repair call - INR 5.00",
        "lecture: quality check call - INR 2.00",
    ]
    assert pw_transport.task_cost_inr() == 12.75
    assert pw_transport.call_breakdown() == (
        "cost by call type (4 call(s)): repair 1 x INR 5.00, notes 1 x INR 4.50, "
        "quality check 1 x INR 2.00, source review 1 x INR 1.25")
    # a new task starts clean and logs nowhere until asked to
    pw_transport.set_task(task_id="t-2")
    pw_transport._record_cost({"cost_inr": 1.0}, "ShortNotes")
    assert len(lines) == 4 and pw_transport.call_breakdown().startswith("cost by call type (1 call(s))")


def test_the_row_generator_logs_the_cost_breakdown_and_uses_lean_requests(monkeypatch):
    from notesbot.generation_profile import lean
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import generator, pw_transport

    seen = {}

    def fake_complete(pdf_bytes, **kwargs):
        seen["lean"] = lean()
        pw_transport._record_cost({"cost_inr": 3.0}, "ShortNotes")
        return ShortNotes(lecture_no="", subject="", title="", sections=[Section(heading="H", points=["a"])])

    def fake_finalize(pdf_bytes, notes, **kwargs):
        pw_transport._record_cost({"cost_inr": 1.0}, "QualityReview")
        return notes, b"%PDF-1.4 notes", {"status": "passed"}

    monkeypatch.setattr("sheets_bridge.coverage.generate_complete_notes", fake_complete)
    monkeypatch.setattr("notesbot.final_quality.finalize_notes_pdf", fake_finalize)
    monkeypatch.setattr(generator, "page_count", lambda _b: 2)
    lines = []
    result = generator.generate_notes_pdf(b"%PDF", language="english", subject="", signature="",
                                          filename="l.pdf", task_id="t", log=lines.append)
    assert seen["lean"] is True and lean() is False
    assert result.cost_inr == 4.0
    assert "lecture: notes call - INR 3.00" in lines
    assert lines[-1] == "cost by call type (2 call(s)): notes 1 x INR 3.00, quality check 1 x INR 1.00"


def test_an_auto_row_on_a_hindi_pdf_generates_in_hindi(monkeypatch):
    """Row 11 ran as lang=auto because its Batch cell was blank, so no Hindi
    rule applied at all. The source PDF is the authority now."""
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    languages: list[str] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        languages.append(language)
        return ShortNotes(lecture_no="", subject="गणित", title="आव्यूह",
                          sections=[Section(heading="आव्यूह", points=["आव्यूह का योग"] * 6)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    coverage.generate_complete_notes(_pdf_with_text_pages(["आव्यूह का योग एवं अंतर"] * 4),
                                     language="auto", subject_hint="", group_size=4, log=lines.append)
    assert languages == ["hindi"]
    assert any("looks like a Hindi lecture" in line for line in lines)



def test_a_hindi_document_gets_hindi_page_furniture():
    """"KEY TERMS", "Where," and the mind-map title were the last English on a
    Hindi page. They are swapped at draw time, and measured the same way."""
    from notesbot import notes_pdf
    from sheets_bridge import pdf_fonts

    pdf_fonts._INSTALLED = False
    assert pdf_fonts.install() is True
    pdf = notes_pdf.NotesPDF(_sample_notes(), "by X", watermark=False)
    pdf.add_page()
    pdf.set_font("Patrick", "", 12)

    pdf_fonts.set_render_language("hindi")
    try:
        assert pdf_fonts.localise_label("KEY TERMS") == "मुख्य शब्द"
        assert pdf_fonts.localise_label("Where,") == "जहाँ,"
        assert pdf_fonts.localise_label("Mind Map — Quick Revision") == "माइंड मैप — त्वरित पुनरावृत्ति"
        assert pdf_fonts.localise_label("Page 3") == "पृष्ठ 3"
        assert pdf_fonts.localise_label("Reproduction)") == "Reproduction)"      # content is untouched
        # the swap happens inside the measuring path too, so a label measures
        # exactly as its Hindi form and layout cannot drift
        assert pdf.get_string_width("KEY TERMS") == pdf.get_string_width("मुख्य शब्द")
        assert pdf.get_string_width("Page 3") == pdf.get_string_width("पृष्ठ 3")
        pdf_fonts.set_render_language("english")
        assert pdf_fonts.localise_label("KEY TERMS") == "KEY TERMS"
        assert pdf.get_string_width("KEY TERMS") != pdf.get_string_width("मुख्य शब्द")
    finally:
        pdf_fonts.set_render_language("")

def test_a_group_that_times_out_is_retried_once_as_halves_not_single_pages(monkeypatch):
    """A TRANSPORT failure (not a content verdict) gets one halving. Slicing
    further down to single pages tripled the bill for the rare dense deck, so
    it is no longer done: the halves that answer are kept."""
    from notesbot.errors import VisualFidelityError
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    monkeypatch.setenv("SHEETS_BRIDGE_GROUP_WORKERS", "1")
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(coverage, "RETRY_BACKOFF_SECONDS", 0.0)
    from pypdf import PdfReader
    import io as _io

    calls: list[int] = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        pages = len(PdfReader(_io.BytesIO(pdf_bytes)).pages)
        calls.append(pages)
        if pages > 2:                                            # only halves answer
            raise VisualFidelityError("deadline_exceeded")
        return ShortNotes(lecture_no="", subject="Maths", title="L",
                          sections=[Section(heading="H", points=["a"] * 8)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines: list[str] = []
    merged = coverage.generate_complete_notes(_pdf_with_pages(4), language="english",
                                              subject_hint="maths", group_size=4, log=lines.append)
    assert merged.sections
    assert calls == [4, 4, 2, 2]                                 # 2 tries, then the halves
    assert any("pages 1-4: recovered as 2 smaller call(s)" in line for line in lines)
    assert not any("part 1: failed as one group" in line for line in lines)   # no second level


def test_describe_exception_names_the_cause_without_any_message():
    from sheets_bridge.errors import describe_exception

    try:
        try:
            raise TimeoutError("request to https://example/secret timed out")
        except TimeoutError as inner:
            raise RuntimeError("wrapper carrying request text") from inner
    except RuntimeError as outer:
        described = describe_exception(outer)

    assert described.startswith("RuntimeError @ test_sheets_bridge.py:")
    assert " <- TimeoutError @ test_sheets_bridge.py:" in described
    assert "secret" not in described and "wrapper" not in described
    # a plain failure still reads exactly as before
    assert describe_exception(ValueError()) == "ValueError"


# --------------------------------------------------------------------------- #
# Model dropdown: pick the model before the run, and never let a typo through
# --------------------------------------------------------------------------- #
def test_selectable_models_marks_the_configured_one(monkeypatch):
    from sheets_bridge import pw_transport as T

    monkeypatch.setenv("PW_PROXY_MODEL", "gpt-5.6-terra-ard-content")
    listed = T.selectable_models()
    assert [m["id"] for m in listed if m["current"]] == ["gpt-5.6-terra-ard-content"]
    assert {"gemini-3.7-flash", "gpt-5.6-luna-ard-content"} <= {m["id"] for m in listed}
    # image and TTS ids from the proxy catalog are not offered: the pipeline
    # cannot use them
    assert not any("image" in m["id"] or m["id"].startswith("tts") for m in listed)

    # a model set by hand in .env stays selectable instead of silently changing
    monkeypatch.setenv("PW_PROXY_MODEL", "gemini-9.9-experimental")
    listed = T.selectable_models()
    assert listed[0]["id"] == "gemini-9.9-experimental" and listed[0]["current"]


def test_run_options_accept_only_offered_models(monkeypatch):
    from sheets_bridge import webui
    from sheets_bridge.settings import Settings

    monkeypatch.setenv("PW_PROXY_MODEL", "gemini-3.7-flash")
    settings = Settings.from_env()

    assert webui._clean_options({"model": "gpt-5.6-sol-ard-content"}, settings)["model"] == "gpt-5.6-sol-ard-content"
    assert webui._clean_options({}, settings)["model"] == ""      # keep what is configured

    for bad in ("gpt-5.6-terra", "tts-ard-content", "gpt-image-2-litellm", "../etc/passwd"):
        with pytest.raises(ValueError):
            webui._clean_options({"model": bad}, settings)


def test_config_offers_the_models_and_chosen_one_reaches_the_transport(monkeypatch):
    from sheets_bridge import webui
    from sheets_bridge.settings import Settings

    monkeypatch.setenv("PW_PROXY_MODEL", "gemini-3.7-flash")
    controller = webui.RunController(Settings.from_env())
    status, payload = webui.api_response(controller, "GET", "config", None)
    assert status == 200 and any(m["id"] == "gpt-5.6-terra-ard-content" for m in payload["models"])

    # the choice also lands in the environment, because the notes cache keys on
    # PW_PROXY_MODEL - otherwise a run could reuse another model's notes
    assert controller._chosen_model({"model": "gpt-5.6-luna-ard-content"}) == "gpt-5.6-luna-ard-content"
    assert os.environ["PW_PROXY_MODEL"] == "gpt-5.6-luna-ard-content"
    assert controller._chosen_model({}) == ""
    assert os.environ["PW_PROXY_MODEL"] == "gpt-5.6-luna-ard-content"   # unchanged


def test_dropdown_choice_is_what_install_transport_receives(monkeypatch):
    from notesbot import gemini_client

    from sheets_bridge import pw_gate, pw_transport

    original = (gemini_client._use_litellm, gemini_client._lite_model,
                gemini_client._lite_client, gemini_client._build_visual_lite_client)
    try:
        access = pw_gate.ProxyAccess(lambda: "tok", email="someone@pw.live")
        assert access.install_transport("gpt-5.6-sol-ard-content") == "gpt-5.6-sol-ard-content"
        assert gemini_client._lite_model() == "gpt-5.6-sol-ard-content"
        from sheets_bridge.pw_openai import OpenAIProxyClient

        assert isinstance(gemini_client._lite_client(), OpenAIProxyClient)
    finally:
        (gemini_client._use_litellm, gemini_client._lite_model,
         gemini_client._lite_client, gemini_client._build_visual_lite_client) = original


# -- page-1 header from the sheet --------------------------------------------

def test_the_header_takes_the_chapter_line_in_the_notes_language():
    from sheets_bridge.mapping import display_chapter

    cell = "Structural Organisation in Animals\nप्राणियों में संरचनात्मक संगठन"
    assert display_chapter(cell, "english") == "Structural Organisation in Animals"
    assert display_chapter(cell, "hindi") == "प्राणियों में संरचनात्मक संगठन"
    assert display_chapter("  Thermodynamics   and  Kinetics ", "") == "Thermodynamics and Kinetics"
    assert display_chapter("Matrices", "hindi") == "Matrices"      # one spelling: used as is
    assert display_chapter("", "english") == ""


def test_the_header_lecture_number_ignores_the_batch_year():
    from sheets_bridge.mapping import display_lecture

    assert display_lecture("L01") == "Lecture - 01"
    assert display_lecture("Lecture 12") == "Lecture - 12"
    assert display_lecture("7") == "Lecture - 07"
    assert display_lecture("2027 L05") == "Lecture - 05"
    assert display_lecture("JEE 2027 - 3") == "Lecture - 03"
    assert display_lecture("") == ""
    assert display_lecture("Revision") == ""


def test_the_header_subject_is_the_standard_name_and_hindi_for_hindi_notes():
    from sheets_bridge.mapping import display_subject

    assert display_subject("Physical Chemistry ", "english") == "Physical Chemistry"
    assert display_subject("Maths", "hindi") == "गणित"
    assert display_subject("Physics", "hindi") == "भौतिक विज्ञान"
    assert display_subject("Organic Chemistry", "hindi") == "कार्बनिक रसायन"   # the branch stays
    assert display_subject("रसायन", "hindi") == "रसायन विज्ञान"
    assert display_subject("रसायन", "english") == "Chemistry"
    # One spelling whatever the cell says.
    assert {display_subject(cell, "english") for cell in (
        "Maths", "Mathematics", "Mat", "(AIR) Maths", "maths by Sachin Jakhar Sir")} == {"Maths"}
    assert {display_subject(cell, "english") for cell in ("Sanskrit", "sanskrit")} == {"Sanskrit"}
    assert display_subject("Phyiscs", "english") == "Physics"
    assert display_subject("Zoology AIR", "english") == "Zoology"
    assert display_subject("Cpacut Mastery", "english") == "Cpacut Mastery"   # unknown: as typed
    assert display_subject("sanskrit", "sanskrit") == "संस्कृतम्"
    assert display_subject("", "english") == ""


def test_the_sheet_header_replaces_the_models_metadata():
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge.mapping import apply_sheet_header, sheet_header

    notes = ShortNotes(lecture_no="", subject="Chemistry", title="Thermo", audience="JEE Main 2026",
                       sections=[Section(heading="H", points=["a"])])
    apply_sheet_header(notes, sheet_header(subject="Physical Chemistry", chapter="Thermodynamics and Thermochemistry",
                                           lecture="L04", language="english"))
    assert (notes.title, notes.subject, notes.lecture_no, notes.audience) == (
        "Thermodynamics and Thermochemistry", "Physical Chemistry", "Lecture - 04", "")


def test_a_blank_sheet_cell_keeps_the_models_value_but_never_its_year():
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge.mapping import apply_sheet_header, sheet_header

    notes = ShortNotes(lecture_no="Lecture - 02", subject="Physics", title="Kinematics", audience="JEE 2026",
                       sections=[Section(heading="H", points=["a"])])
    apply_sheet_header(notes, sheet_header(subject="", chapter="", lecture="", language="english"))
    assert (notes.title, notes.subject, notes.lecture_no, notes.audience) == (
        "Kinematics", "Physics", "Lecture - 02", "JEE")


def test_the_runner_hands_the_sheet_header_to_the_generator():
    seen = []

    def generate(pdf_bytes, *, language, subject, signature, header=None, **extra):
        seen.append(header)
        return _approved_result(pdf=b"%PDF-1.7 notes", notes_json=_notes_json(), cost_inr=1.0, usage={})

    sheet = FakeSheet(_grid(_row(2, chapter="Motion in a Plane\nसमतल में गति", lecture="L03")))
    Runner(sheet, FakeDrive(), RunOptions(), generate_fn=generate, run_id="r", out=lambda _l: None,
           clock=lambda: "T", final_clock=lambda: "F").run()
    # the fixture batch is "Lakshya NEET Hindi 2027" -> Hindi notes
    assert [(h.subject, h.chapter, h.lecture) for h in seen] == [("भौतिक विज्ञान", "समतल में गति", "Lecture - 03")]
