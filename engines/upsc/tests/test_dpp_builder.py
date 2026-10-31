"""Driving the DPP builder from notes this app produced.

The standalone tool starts from an uploaded PDF and guesses the language from
whatever OCR returned. Here the notes are already on disk and the run metadata
already records the language, so both steps are replaced by reading. These
tests are mostly about that seam — everything past it is the notes_to_dpp
package, which has its own suite.
"""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import dpp_builder
from src.notes_to_dpp.models import Language

NOTE_ID = "run_20260822_101500_abc12345"

NOTES = "\n".join([
    "#LECTURE_TITLE: L09_DCN09_Ecology and Ecosystem",
    "",
    "#H1: Ecosystem",
    "",
    "#FIGURE:fig1",
    "- **Homeostasis** keeps an ecosystem's internal conditions stable.",
    "- Producers, consumers and decomposers form the trophic structure.",
    "",
    "#H1: Wild Life (Protection) Act",
    "",
    "- The **Wild Life (Protection) Act, 1972** is the principal legal framework.",
])


@pytest.fixture
def runs(tmp_path: Path, monkeypatch) -> Path:
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(
        json.dumps({"language": "en", "subject": "environment"}), encoding="utf-8")
    from src.dmp import service as dmp_service
    from src.dmp import store as dmp_store

    monkeypatch.setattr(dmp_store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(dmp_service, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(dpp_builder, "RUNS_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------- the notes

def test_the_notes_come_from_disk_with_no_pdf_and_no_ocr(runs: Path):
    """The whole point of sourcing from our own runs: the text is already
    correct, so the extraction step that OCR exists to rescue never happens."""
    doc = dpp_builder.note_document(NOTE_ID, runs)
    assert doc.ocr_page_count == 0
    assert "Homeostasis" in doc.text
    assert "Wild Life (Protection) Act" in doc.text


def test_the_apps_own_markers_do_not_reach_the_matcher(runs: Path):
    """`#H1:` and `#FIGURE:` are layout instructions. Left in, they become
    tokens the matcher weighs against real taxonomy labels."""
    doc = dpp_builder.note_document(NOTE_ID, runs)
    assert "#H1:" not in doc.text
    assert "#FIGURE:" not in doc.text
    assert "#LECTURE_TITLE:" not in doc.text


def test_headings_survive_because_they_are_the_strongest_evidence(runs: Path):
    doc = dpp_builder.note_document(NOTE_ID, runs)
    assert "Ecosystem" in doc.text


def test_a_deleted_run_is_explained_rather_than_crashed(runs: Path):
    with pytest.raises(dpp_builder.DppError, match="no longer on this machine"):
        dpp_builder.note_document("run_20200101_000000_deadbeef", runs)


def test_uploaded_text_notes_are_read_without_creating_a_run():
    document = dpp_builder.extract_uploaded_document(
        b"Ecosystem services include provisioning and regulating services.",
        "revision-notes.txt",
        enable_ocr=False,
    )

    assert document.filename == "revision-notes.txt"
    assert document.text.startswith("Ecosystem services")


def test_an_invalid_uploaded_note_has_the_same_friendly_dpp_error():
    with pytest.raises(dpp_builder.DppError, match="empty"):
        dpp_builder.extract_uploaded_document(b"", "empty.txt")


# ------------------------------------------------------------- the language

def test_the_language_is_read_from_the_run_not_guessed_from_the_page(runs: Path):
    """Detection exists for uploaded PDFs of unknown origin. This app chose the
    language before writing a word, so reading it back is strictly better."""
    language, code = dpp_builder.note_language(NOTE_ID, runs)
    assert language is Language.ENGLISH
    assert code == "en"


def test_hindi_notes_ask_the_bank_for_hindi(runs: Path):
    (runs / NOTE_ID / "run_metadata.json").write_text(
        json.dumps({"language": "hi", "subject": "environment"}), encoding="utf-8")
    language, code = dpp_builder.note_language(NOTE_ID, runs)
    assert language is Language.HINDI and code == "hi"


def test_marathi_is_unknown_to_the_bank_rather_than_quietly_hindi(runs: Path):
    """The bank holds English and Hindi rows only. Marathi and Hindi share a
    script, so resolving Marathi to Hindi would be invisible — and it is exactly
    the cross-language fallback the question bank refuses to do."""
    (runs / NOTE_ID / "run_metadata.json").write_text(
        json.dumps({"language": "mr", "subject": "environment"}), encoding="utf-8")
    language, code = dpp_builder.note_language(NOTE_ID, runs)
    assert language is Language.UNKNOWN
    assert code == "mr"


# -------------------------------------------------------------- the masters

def test_master_resolution_prefers_an_upload_over_disk():
    data, source = dpp_builder.resolve_master("taxonomy", uploaded=b"xlsx-bytes")
    assert data == b"xlsx-bytes"
    assert "session" in source


def test_an_env_var_pointing_nowhere_is_an_error_not_a_silent_fallback(monkeypatch):
    """Falling through to a different workbook than the one configured is how a
    practice set gets built from the wrong taxonomy."""
    monkeypatch.setenv("QBG_TAGS_PATH", str(Path("no-such-file.xlsx").resolve()))
    with pytest.raises(dpp_builder.DppError, match="not there"):
        dpp_builder.resolve_master("taxonomy")


def test_the_env_var_names_match_the_standalone_tool():
    """A machine already configured for the standalone builder keeps working."""
    assert dpp_builder.MASTERS["taxonomy"][0] == "QBG_TAGS_PATH"
    assert dpp_builder.MASTERS["questions"][0] == "QUESTION_BANK_PATH"


def test_a_missing_master_names_the_file_it_wants(monkeypatch):
    monkeypatch.setattr(dpp_builder, "master_path", lambda kind: None)
    with pytest.raises(dpp_builder.DppError, match="QBG Tags"):
        dpp_builder.load_masters()


def test_a_frozen_build_finds_workbooks_beside_the_exe_at_lookup_time(tmp_path, monkeypatch):
    """The launcher exports the data folder beside the EXE through the env var,
    but only for files present at start-up. A rebuilt dist/ tree (PyInstaller
    wipes it) or workbooks copied in later left the panel saying "put them in
    the app's data folder" — the folder they were already in — until the EXE
    was restarted."""
    exe_dir = tmp_path / "UpscNotesApp"
    (exe_dir / "data").mkdir(parents=True)
    workbook = exe_dir / "data" / "UPSC QBG Tags.xlsx"
    workbook.write_bytes(b"xlsx")
    monkeypatch.delenv("QBG_TAGS_PATH", raising=False)
    monkeypatch.setattr(dpp_builder, "ROOT_DIR", exe_dir / "_internal")  # the bundle: no data there
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "UpscNotesApp.exe"))

    assert dpp_builder.master_path("taxonomy") == workbook.resolve()
    data, source = dpp_builder.resolve_master("taxonomy")
    assert data == b"xlsx" and source == "data/UPSC QBG Tags.xlsx"
    assert dpp_builder.masters_ready() is False, "the question bank is still missing"


def test_an_admins_env_var_still_wins_over_the_folder_beside_the_exe(tmp_path, monkeypatch):
    exe_dir = tmp_path / "UpscNotesApp"
    (exe_dir / "data").mkdir(parents=True)
    (exe_dir / "data" / "UPSC QBG Tags.xlsx").write_bytes(b"local")
    shared = tmp_path / "share" / "tags.xlsx"
    shared.parent.mkdir()
    shared.write_bytes(b"shared")
    monkeypatch.setenv("QBG_TAGS_PATH", str(shared))
    monkeypatch.setattr(dpp_builder, "ROOT_DIR", exe_dir / "_internal")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "UpscNotesApp.exe"))

    assert dpp_builder.master_path("taxonomy") == shared.resolve()
    assert dpp_builder.resolve_master("taxonomy")[0] == b"shared"


def test_a_checkout_does_not_look_beside_the_interpreter(tmp_path, monkeypatch):
    """Outside a bundle sys.executable is python.exe; its folder is nowhere a
    workbook would be, and a stray file there must not be picked up."""
    monkeypatch.delenv("QBG_TAGS_PATH", raising=False)
    monkeypatch.setattr(dpp_builder, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert dpp_builder.master_path("taxonomy") is None
    assert dpp_builder._disk_candidates("UPSC QBG Tags.xlsx") == (
        tmp_path / "data" / "UPSC QBG Tags.xlsx", tmp_path / "UPSC QBG Tags.xlsx")


def test_the_bank_is_cached_on_path_and_mtime():
    """It is ~55 MB and takes about 23 seconds to parse. Streamlit reruns the
    script on every checkbox, so an uncached load makes the panel unusable."""
    assert hasattr(dpp_builder._load_questions, "cache_info")
    assert hasattr(dpp_builder._load_taxonomy, "cache_info")


# ---------------------------------------------------------------- coverage

def test_coverage_only_ever_returns_rows_that_exist(runs: Path):
    """Closed-set tagging: the matcher ranks the supplied rows, it never writes
    a label of its own."""
    from src.notes_to_dpp.models import TaxonomyRow

    rows = (
        TaxonomyRow(row_id="R1", subject="Environment", subject_id="S1",
                    chapter="Ecosystem", chapter_id="C1",
                    topic="Functions of an Ecosystem", topic_id="T1",
                    subtopic="Homeostasis", subtopic_id="ST1"),
        TaxonomyRow(row_id="R2", subject="Polity", subject_id="S2",
                    chapter="Parliament", chapter_id="C2",
                    topic="Money Bill", topic_id="T2",
                    subtopic=None, subtopic_id=None),
    )
    found = dpp_builder.coverage(NOTE_ID, rows, runs_dir=runs)
    assert {c.row_id for c in found} <= {"R1", "R2"}
    assert any(c.row_id == "R1" for c in found), "the ecosystem row should match"


def test_uploaded_document_coverage_uses_the_same_closed_taxonomy():
    from src.notes_to_dpp.models import TaxonomyRow

    document = dpp_builder.extract_uploaded_document(
        b"Ecosystem homeostasis depends on producers and decomposers.",
        "ecology.txt",
        enable_ocr=False,
    )
    rows = (
        TaxonomyRow(row_id="R1", subject="Environment", subject_id="S1",
                    chapter="Ecosystem", chapter_id="C1",
                    topic="Functions of an Ecosystem", topic_id="T1",
                    subtopic="Homeostasis", subtopic_id="ST1"),
        TaxonomyRow(row_id="R2", subject="Polity", subject_id="S2",
                    chapter="Parliament", chapter_id="C2",
                    topic="Money Bill", topic_id="T2",
                    subtopic=None, subtopic_id=None),
    )

    found = dpp_builder.coverage_document(document, rows)

    assert {candidate.row_id for candidate in found} <= {"R1", "R2"}
    assert any(candidate.row_id == "R1" for candidate in found)
