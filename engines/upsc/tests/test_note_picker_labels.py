"""The lecture picker has to name one lecture, distinctly.

This looks cosmetic and was not. `note_label` timed a lecture to the MINUTE, so
a real runs folder produced 40 lectures under 14 labels — eight of them sharing
the line "2026-08-23 22:32 · Core Concepts and Definitions [EN]".

Two consequences, and the second is the one that cost a feature:

* a person could not tell those eight apart, so choosing between them was
  guesswork;
* neither could Streamlit. Selecting one and pressing a button landed on a
  DIFFERENT lecture with the same label, and because the panel keys its state
  by note id, the state written for the lecture you chose was read back for the
  lecture you were moved to. The DPP builder rendered its "Open the practice
  builder" gate again on every press, so it could not be opened at all through
  the library picker.

The fix is uniqueness, guaranteed rather than hoped for.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp.service import note_label, note_labels


def _note(note_id, created, title="Indian Polity", language="en", sets=0):
    return {"note_id": note_id, "created": created, "note_title": title,
            "language": language, "practice_sets": sets}


# ------------------------------------------------------------- the label itself

def test_the_label_names_the_lecture_and_when_it_was_made():
    label = note_label(_note("run_20260823_223413_2679b222", "20260823_223413"))
    assert "2026-08-23" in label
    assert "Indian Polity" in label
    assert "[EN]" in label


def test_the_label_is_timed_to_the_second():
    """Minute precision is what put eight lectures on one row. Two runs of the
    same class a few seconds apart is not a corner case — it is what happens
    when someone regenerates after a bad transcript."""
    label = note_label(_note("a", "20260823_223413"))
    assert "22:34:13" in label, label


def test_a_short_or_missing_timestamp_does_not_crash_the_picker():
    assert note_label(_note("a", "")) .endswith("[EN]")
    assert note_label(_note("a", "20260823_2234"))  # minute-only, older runs
    assert note_label({"note_id": "a"})


def test_practice_sets_are_shown_when_some_exist():
    assert "2 practice set(s)" in note_label(_note("a", "20260823_223413", sets=2))
    assert "practice set" not in note_label(_note("a", "20260823_223413", sets=0))


# ----------------------------------------------------------- guaranteed distinct

def test_labels_are_distinct_even_when_everything_else_matches():
    """Same second, same title, same language — the case note_label alone
    cannot solve, because it sees one note and cannot know about the other."""
    notes = [
        _note("run_20260823_223413_aaaaaaaa", "20260823_223413"),
        _note("run_20260823_223413_bbbbbbbb", "20260823_223413"),
        _note("run_20260823_223413_cccccccc", "20260823_223413"),
    ]
    labels = note_labels(notes)
    assert len(set(labels.values())) == 3, labels
    assert set(labels) == {n["note_id"] for n in notes}


def test_the_tiebreaker_names_the_run_so_the_rows_can_be_told_apart():
    notes = [_note("run_x_aaaaaaaa", "20260823_223413"),
             _note("run_x_bbbbbbbb", "20260823_223413")]
    labels = note_labels(notes)
    assert "aaaaaaaa" in labels["run_x_aaaaaaaa"]
    assert "bbbbbbbb" in labels["run_x_bbbbbbbb"]


def test_labels_that_already_differ_are_left_alone():
    """The tiebreaker is ugly. It must not appear on the normal case."""
    notes = [_note("run_a", "20260823_223413", title="Polity"),
             _note("run_b", "20260823_223519", title="Economy")]
    labels = note_labels(notes)
    assert all("run_" not in label for label in labels.values()), labels
    assert labels["run_a"] == note_label(notes[0])


def test_an_empty_list_is_not_an_error():
    assert note_labels([]) == {}


def test_a_real_runs_folder_produces_one_row_per_lecture():
    """The regression itself, against whatever is actually on this machine."""
    from src.dmp import service

    notes = service.list_notes()
    if len(notes) < 2:
        import pytest

        pytest.skip("needs at least two generated lectures")
    labels = note_labels(notes)
    assert len(set(labels.values())) == len(notes), (
        f"{len(notes)} lectures collapsed into {len(set(labels.values()))} "
        f"picker rows; a user cannot choose between rows that read identically")
