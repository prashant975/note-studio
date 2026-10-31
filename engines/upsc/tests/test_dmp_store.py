"""Practice sets on disk — the "come back tomorrow" half of the feature.

A practice set is money already spent: three model calls for the questions and
one more for every answer generated. Everything here is about not losing that —
across a restart, across a half-written file, across an index that disagrees with
the folder it is supposed to describe.
"""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp import store
from src.dmp.models import (
    STATUS_ANSWERED,
    DmpSession,
    Question,
    new_question_id,
    new_session_id,
    normalise_marks,
    utc_now_iso,
)

NOTE_ID = "run_20260821_101500_abc12345"


def _session(note_id: str = NOTE_ID, answered: int = 0) -> DmpSession:
    now = utc_now_iso()
    session = DmpSession(
        dmp_session_id=new_session_id(), note_id=note_id,
        note_title="Indian Society", subject="society", subject_line="Indian Society",
        language="mr", app_version="2.7.0", created_at=now, updated_at=now,
    )
    for level in ("easy", "moderate", "applied"):
        session.set_level(level, [
            Question(id=new_question_id(), level=level, text=f"{level} question {i}",
                     marks=10 if i % 2 else 15)
            for i in range(5)
        ])
    for question in session.questions[:answered]:
        question.answer = "A model answer."
        question.status = STATUS_ANSWERED
        question.answer_generated_at = utc_now_iso()
        question.model_used = "gemini-3.5-flash"
    return session


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    (tmp_path / NOTE_ID).mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------- round trip

def test_a_saved_session_comes_back_exactly_as_it_went_in(runs: Path):
    original = _session(answered=2)
    store.save(original, runs)

    loaded = store.load(NOTE_ID, original.dmp_session_id, runs)
    assert loaded is not None
    assert loaded.to_dict() == original.to_dict()
    assert loaded.answered_count == 2
    assert loaded.pending_count == 13


def test_it_lands_in_the_notes_own_run_folder(runs: Path):
    """Not a side directory: a practice set that does not travel with the run
    when the folder is zipped or copied is the one artefact that goes missing."""
    session = _session()
    path = store.save(session, runs)
    assert path == runs / NOTE_ID / "dmp" / f"{session.dmp_session_id}.json"
    assert path.exists()


def test_persistence_survives_a_restart(runs: Path):
    """Nothing is held in memory. A brand-new process reading the same folder
    sees the same answers."""
    session = _session(answered=3)
    store.save(session, runs)

    # Simulates the restart: no shared state, just the bytes on disk.
    raw = json.loads((runs / NOTE_ID / "dmp" /
                      f"{session.dmp_session_id}.json").read_text(encoding="utf-8"))
    revived = DmpSession.from_dict(raw)
    assert revived.answered_count == 3
    assert all(q.answer for q in revived.questions if q.answered)


# ------------------------------------------------------------- the history

def test_the_index_lists_past_sets_newest_first(runs: Path):
    older = _session()
    older.created_at = "2026-08-01T09:00:00+00:00"
    newer = _session(answered=5)
    newer.created_at = "2026-08-20T09:00:00+00:00"
    store.save(older, runs)
    store.save(newer, runs)

    rows = store.list_sessions(NOTE_ID, runs)
    assert [r["dmp_session_id"] for r in rows] == [newer.dmp_session_id,
                                                   older.dmp_session_id]
    assert rows[0]["answered"] == 5 and rows[0]["pending"] == 10
    assert rows[1]["answered"] == 0


def test_answering_updates_the_same_set_rather_than_starting_a_new_one(runs: Path):
    """The spec's rule, and the reason history stays readable: a user who
    answers three questions on Monday and two on Tuesday has ONE practice set."""
    session = _session()
    store.save(session, runs)
    session.questions[0].answer = "Answered on Tuesday."
    session.questions[0].status = STATUS_ANSWERED
    store.save(session, runs)

    assert len(list((runs / NOTE_ID / "dmp").glob("dmp_*.json"))) == 1
    rows = store.list_sessions(NOTE_ID, runs)
    assert len(rows) == 1 and rows[0]["answered"] == 1


def test_the_latest_set_is_what_reopening_finds(runs: Path):
    older = _session()
    older.created_at = "2026-01-01T00:00:00+00:00"
    newer = _session()
    newer.created_at = "2026-08-20T00:00:00+00:00"
    store.save(older, runs)
    store.save(newer, runs)
    assert store.latest_session(NOTE_ID, runs).dmp_session_id == newer.dmp_session_id


# ------------------------------------------------------ when things go wrong

def test_a_corrupt_index_does_not_hide_the_sessions_behind_it(runs: Path):
    """The index is a convenience; the session files are the record. A truncated
    index must not read as "you have no practice sets"."""
    session = _session(answered=1)
    store.save(session, runs)
    store.index_path(NOTE_ID, runs).write_text("{ this is not json", encoding="utf-8")

    rows = store.list_sessions(NOTE_ID, runs)
    assert [r["dmp_session_id"] for r in rows] == [session.dmp_session_id]
    assert rows[0]["answered"] == 1


def test_a_missing_index_is_rebuilt_from_the_files_that_are_there(runs: Path):
    """The normal state of a folder restored from a zip or copied off a laptop."""
    session = _session()
    store.save(session, runs)
    store.index_path(NOTE_ID, runs).unlink()

    rows = store.list_sessions(NOTE_ID, runs)
    assert [r["dmp_session_id"] for r in rows] == [session.dmp_session_id]
    assert store.index_path(NOTE_ID, runs).exists(), "the rebuild should persist"


def test_a_corrupt_session_file_reads_as_absent_not_as_a_crash(runs: Path):
    session = _session()
    store.save(session, runs)
    store.session_path(NOTE_ID, session.dmp_session_id, runs).write_text(
        "half a fi", encoding="utf-8")
    assert store.load(NOTE_ID, session.dmp_session_id, runs) is None


def test_a_write_that_fails_cannot_destroy_the_previous_version(runs: Path):
    """Writes go through a temp file and a replace. The failure this prevents is
    a crash midway through overwriting a set with fifteen paid-for answers."""
    session = _session(answered=15)
    store.save(session, runs)
    path = store.session_path(NOTE_ID, session.dmp_session_id, runs)
    before = path.read_text(encoding="utf-8")

    # A tmp file left behind by an interrupted write is not the live file.
    assert not list(path.parent.glob("*.tmp"))
    assert path.read_text(encoding="utf-8") == before
    assert store.load(NOTE_ID, session.dmp_session_id, runs).answered_count == 15


def test_a_note_with_no_practice_sets_lists_nothing_and_does_not_crash(runs: Path):
    assert store.list_sessions(NOTE_ID, runs) == []
    assert store.latest_session(NOTE_ID, runs) is None


@pytest.mark.parametrize("bad", ["../../etc", "run/../../x", "", "  ", "a\\b"])
def test_an_id_that_is_really_a_path_is_refused(bad, runs: Path):
    """These ids come from our own session state today. They are read back off
    disk tomorrow, and "../.." is a path segment too."""
    with pytest.raises(store.DmpStoreError):
        store.note_run_dir(bad, runs)


def test_a_session_id_must_look_like_one(runs: Path):
    with pytest.raises(store.DmpStoreError):
        store.session_path(NOTE_ID, "../../secrets", runs)


# ------------------------------------------------------------ the model layer

def test_an_answer_without_a_status_is_still_an_answer():
    """A file written during a crash can carry one without the other. Trusting
    the status would re-bill the user for an answer already sitting in the file."""
    question = Question.from_dict({"id": "q_1", "level": "easy", "text": "Q",
                                   "answer": "An answer.", "status": "pending"})
    assert question.status == STATUS_ANSWERED
    assert question.answered


@pytest.mark.parametrize("given,expected", [
    (10, 10), (15, 15), ("10", 10), ("15 marks", 15), (10.0, 10),
    (12, 10), (13, 15), (None, 10), ("", 10), ("many", 10), (250, 15),
])
def test_odd_marks_are_relabelled_rather_than_dropped(given, expected):
    """A model asked for "10 or 15" sometimes says 12. That is a good question
    with a wrong label, and the label is the cheap thing to fix."""
    assert normalise_marks(given) == expected
