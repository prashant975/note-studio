"""The Daily Mains Practice panel, rendered against a real practice set.

tests/test_dmp_panel.py asserts that app.py's source contains certain strings.
That is worth something, and it is not this: the panel reads a session off disk
on every render, groups it by level, and decides per question whether to show a
checkbox or an answer. None of that is exercised by a substring.

Generation and answering both cost model calls, so they are not driven here. The
set is written to disk directly — which is also the more honest test of the
panel's actual contract, that it reloads from disk every render and therefore
survives a refresh, a rerun and a restarted server.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from src.config import DMP_LEVELS
from src.dmp import store as dmp_store
from src.dmp.models import DmpSession, Question, new_session_id, utc_now_iso
from src.dmp import service as dmp_service

HARNESS = str(Path(__file__).resolve().parent / "app_harness.py")

pytestmark = pytest.mark.skipif(
    not dmp_service.list_notes(), reason="no generated lectures on this machine")


@pytest.fixture(scope="module")
def note_id():
    return dmp_service.list_notes()[0]["note_id"]


@pytest.fixture(scope="module")
def seeded_session(note_id):
    """One practice set on disk: two questions per level, one already answered.

    Cleaned up afterwards so a developer's runs folder is not left holding a
    fabricated practice set that the library picker would then offer them.
    """
    session = DmpSession(
        dmp_session_id=new_session_id(),
        note_id=note_id,
        note_title="Test lecture",
        language="en",
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    for index, level in enumerate(DMP_LEVELS):
        session.set_level(level, [
            Question(id=f"{level}-1", level=level,
                     text=f"A pending {level} question about Article 21.", marks=10),
            Question(id=f"{level}-2", level=level,
                     text=f"An answered {level} question about federalism.",
                     marks=15,
                     status="answered",
                     answer=f"The model answer for the {level} question."),
        ])
    dmp_store.save(session)
    yield session
    # Remove the file, then rebuild the index FROM the files that remain — the
    # index is a convenience derived from them, and hand-editing it is how the
    # two get out of step.
    try:
        dmp_store.session_path(note_id, session.dmp_session_id).unlink()
    except OSError:
        pass
    try:
        dmp_store.rebuild_index(note_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup of test data
        pass


def _page_showing(note_id):
    """The page with the library picker pointed at `note_id`.

    Not `list_notes()[0]`: other tests in this suite run the pipeline, so new
    run folders appear WHILE these tests execute and the newest lecture is not
    the one this file seeded. Naming the note is also what the picker is for.
    """
    at = AppTest.from_file(HARNESS, default_timeout=600)
    at.session_state["dmp_library_pick"] = note_id
    at.run()
    return at


@pytest.fixture(scope="module")
def page(seeded_session, note_id):
    return _page_showing(note_id)


def _page_text(at):
    """Everything the reader can see.

    A pending question is a CHECKBOX and an answered one is markdown, so
    reading only markdown sees half the set — which is what the first version
    of this file did, and it failed for the right reason.
    """
    return " ".join([*(m.value for m in at.markdown),
                     *(c.label for c in at.checkbox)])


# ------------------------------------------------------------- it renders

def test_the_panel_renders_a_set_that_is_already_on_disk(page, seeded_session):
    assert not page.exception, [str(e.value) for e in page.exception]
    body = _page_text(page)
    assert "A pending easy question about Article 21." in body, body[:400]


def test_every_question_in_the_set_is_shown(page, seeded_session):
    body = _page_text(page)
    for question in seeded_session.questions:
        assert question.text in body, f"missing from the page: {question.text}"


def test_an_answered_question_stays_selectable_without_reopening_answer_generation(page):
    """Offering to buy an answer that is already paid for is the one mistake
    this panel must not make."""
    checkbox_labels = " ".join(c.label for c in page.checkbox)
    assert "A pending easy question" in checkbox_labels, checkbox_labels
    assert "An answered easy question" in checkbox_labels, (
        "an answered question cannot be included in a selected question paper")
    assert "The model answer for the easy question." in _page_text(page)


def test_the_counts_reflect_what_is_on_disk(page, seeded_session):
    assert seeded_session.answered_count == 3
    assert seeded_session.pending_count == 3
    body = _page_text(page)
    assert "3" in body


def test_generating_answers_is_refused_until_something_is_ticked(page):
    """Every pending question starts unticked, so the button must start
    disabled — a press with nothing selected would be a paid no-op."""
    # "Generate", not merely "answer": the downloads say "Answers" too, and a
    # download button is supposed to be enabled.
    generate = [b for b in page.button
                if b.label.lower().startswith("generate model answer")]
    assert generate, sorted({b.label for b in page.button})
    assert all(b.disabled for b in generate), (
        [(b.label, b.disabled) for b in generate])


def test_ticking_a_question_enables_the_answer_button(seeded_session, note_id):
    at = _page_showing(note_id)
    pending = [c for c in at.checkbox if "A pending easy question" in c.label]
    assert pending, [c.label for c in at.checkbox]
    pending[0].set_value(True).run()
    assert not at.exception
    generate = [b for b in at.button
                if b.label.lower().startswith("generate model answer")]
    assert generate, sorted({b.label for b in at.button})
    assert any(not b.disabled for b in generate), (
        [(b.label, b.disabled) for b in generate])


# ------------------------------------------------- the picker stays where it is

def test_the_library_picker_does_not_move_when_the_page_reruns(seeded_session, note_id):
    """The bug that made the DPP builder unreachable lived in this picker too.

    Several lectures shared one label, so Streamlit resolved a selection to a
    different lecture on the next rerun. Here that would mean generating — and
    billing — a practice set for a class the user was not looking at.
    """
    at = _page_showing(note_id)
    before = at.session_state.filtered_state.get("dmp_library_pick")
    assert before, "the library picker did not render"

    theme = [r for r in at.radio if r.label == "Appearance"][0]
    theme.set_value("Light").run()
    at.run()

    after = at.session_state.filtered_state.get("dmp_library_pick")
    assert after == before, (
        f"an unrelated rerun moved the selected lecture from {before} to {after}")
