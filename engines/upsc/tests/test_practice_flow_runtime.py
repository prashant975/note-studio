"""Practice reachable from the run that just finished, not only from the top.

The results card is where a teacher already is when the notes land, so both
builders open there. The workspaces at the top of the page stay the way in when
that flow is not available — an older lecture, or a run whose folder has been
cleaned up.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from src.dmp import service as dmp_service

ROOT = Path(__file__).resolve().parents[1]
HARNESS = str(ROOT / "tests" / "app_harness.py")

pytestmark = pytest.mark.skipif(
    not dmp_service.list_notes(), reason="no generated lectures on this machine")


def _result(note_id: str) -> dict:
    return {
        "name": "Class recording",
        "run_dir": str(ROOT / "runs" / note_id),
        "notes": "x",
        "docx": None, "pdf": None, "zip": None,
        "warnings": [], "tracking": None,
        "elapsed_seconds": 372.5,
        "output_format": "dcn",
    }


@pytest.fixture(scope="module")
def note_id() -> str:
    return dmp_service.list_notes()[0]["note_id"]


@pytest.fixture(scope="module")
def page_without_results():
    at = AppTest.from_file(HARNESS, default_timeout=900)
    at.run()
    return at


@pytest.fixture(scope="module")
def page_with_result(note_id):
    at = AppTest.from_file(HARNESS, default_timeout=900)
    at.session_state["results"] = [_result(note_id)]
    at.run()
    return at


def test_a_finished_run_offers_both_builders_in_its_own_card(
    page_with_result, page_without_results
) -> None:
    """Two more tabs than the same page carries with nothing generated."""
    assert not page_with_result.exception, [
        str(e.value) for e in page_with_result.exception]

    before = [t.label for t in page_without_results.get("tab")]
    after = [t.label for t in page_with_result.get("tab")]
    assert len(after) == len(before) + 2, (before, after)
    assert after.count("Daily Mains Practice (DMP)") == 2
    assert after.count("Daily Prelims Practice (DPP)") == 2

    headings = [m.value for m in page_with_result.markdown
                if "Practice from these notes" in m.value]
    assert headings, "the practice section is missing from the results card"


def test_the_same_lecture_can_be_open_in_the_card_and_the_library_at_once(
    page_with_result, note_id
) -> None:
    """The regression this guards.

    The library picker opens on the newest lecture, which is precisely the one
    the results card is showing, so both copies of the DPP builder rendered at
    the same time. Every widget in that panel was keyed by the note id alone,
    so the second copy claimed keys the first already held and the page died
    with StreamlitDuplicateElementKey before any of it was drawn.
    """
    assert not page_with_result.exception, [
        str(e.value) for e in page_with_result.exception]

    opens = [b for b in page_with_result.button
             if b.label == "Open the practice builder"]
    assert len(opens) == 2, [b.key for b in opens]
    assert len({b.key for b in opens}) == 2, "both copies claimed the same key"
    # The card's copy is scoped to the card; the library's keeps the note id.
    assert any(b.key == f"dpp_open_{note_id}" for b in opens)
    assert any(b.key.startswith(f"dpp_open_{note_id}_card") for b in opens)


def test_the_card_reports_how_long_the_run_took(page_with_result) -> None:
    captions = [c.value for c in page_with_result.caption
                if "Notes generation time" in c.value]
    assert captions == ["Notes generation time: 6 m 12 s"], captions


def test_a_run_whose_folder_is_gone_shows_no_practice_section() -> None:
    """Nothing to practise on, so the section that promises it is not drawn."""
    at = AppTest.from_file(HARNESS, default_timeout=900)
    result = _result("does_not_matter")
    result["run_dir"] = None
    at.session_state["results"] = [result]
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert not [m for m in at.markdown if "Practice from these notes" in m.value]


@pytest.fixture
def lecture_with_a_saved_set():
    """A lecture with a practice set, built here rather than looked for.

    Searching the machine for one was tried first and is not dependable: a run
    can lose the text the panel needs, and a session index can outlive the file
    it points at, so the search either skipped or found a set that drew no tick
    boxes — indistinguishable from the collision being fixed. This builds both
    halves and removes them afterwards.
    """
    import json
    import shutil

    from src.config import RUNS_DIR
    from src.dmp import store as dmp_store
    from src.dmp.models import DmpSession, Question, new_question_id, utc_now_iso

    # Dated far ahead on purpose: the library picker lists the 40 most recent
    # lectures, and a test note with an old stamp is simply not among them, so
    # the library copy of the panel never draws and the collision goes unseen.
    note_id = "run_29991231_235959_flowtest"
    run_dir = Path(RUNS_DIR) / note_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    try:
        body = " ".join(
            f"The Wildlife Protection Act of 1972 names authority {i} for the "
            f"conservation of species and habitats across India."
            for i in range(30)
        )
        (run_dir / "notes_final.md").write_text(
            "#LECTURE_TITLE: Flow test lecture" + chr(10) * 2 + body,
            encoding="utf-8")
        (run_dir / "run_metadata.json").write_text(
            json.dumps({"language": "en", "subject": "", "area": "",
                        "subject_line": "", "note_title": "Flow test lecture"}),
            encoding="utf-8")

        session = DmpSession(
            dmp_session_id="dmp_flowtest",
            note_id=note_id,
            note_title="Flow test lecture",
            language="en",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        session.set_level("easy", [
            Question(id=new_question_id(), level="easy",
                     text=f"Discuss aspect {i} of the Act.", marks=10)
            for i in range(3)
        ])
        dmp_store.save(session)

        assert dmp_service.has_notes(note_id), "the built note is not usable"
        assert dmp_service.get_session(note_id, session.dmp_session_id) is not None
        assert note_id in {n["note_id"] for n in dmp_service.list_notes()}, (
            "the library picker cannot offer a lecture it does not list")
        yield note_id, session
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_a_saved_practice_set_can_be_drawn_twice_on_one_page(
    lecture_with_a_saved_set,
) -> None:
    """The second half of the duplicate-key regression.

    Scoping the DPP builder was not enough. A DMP set that already exists is
    drawn in full — one tick box per question — the moment either copy of the
    panel opens, and those boxes were keyed by the practice session and the
    question alone. So the card and the library drew the same boxes with the
    same keys, and the page died on the first one.
    """
    note_id, session = lecture_with_a_saved_set
    total = len(session.questions)

    at = AppTest.from_file(HARNESS, default_timeout=900)
    at.session_state["results"] = [_result(note_id)]
    # Both copies open on the same set: the card by index, the library by name.
    at.session_state["dmp_open_session_0"] = session.dmp_session_id
    at.session_state[f"dmp_open_session_lib_{note_id}"] = session.dmp_session_id
    at.session_state["dmp_library_pick"] = note_id
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]

    ticks = [c.key for c in at.checkbox if c.key and "dmp_pick_" in c.key]
    assert len(ticks) == 2 * total, (
        f"expected both copies to draw {total} questions", ticks)
    assert len(ticks) == len(set(ticks)), "two copies claimed the same tick key"


def test_both_copies_of_the_prelims_builder_can_be_open_at_once() -> None:
    """Every widget in the DPP panel, drawn twice, with no key collision.

    The gate means the builder's own widgets — the topic picker, the three
    count inputs, the verified filter, the DPP number — are not on the page
    until someone opens it. Opening both copies is what actually exercises
    them, and it is the only way this file can prove the scope reaches all of
    them rather than just the button that opens the panel.
    """
    from src import dpp_builder

    if not dpp_builder.masters_ready():
        pytest.skip("the QBG workbooks are not on this machine")

    note_id = dmp_service.list_notes()[0]["note_id"]
    at = AppTest.from_file(HARNESS, default_timeout=1800)
    at.session_state["results"] = [_result(note_id)]
    at.session_state["dpp_library_pick"] = note_id
    # Both copies past the gate: the library's keeps the note id, the card's
    # carries the card scope.
    at.session_state[f"dpp_opened_{note_id}"] = True
    at.session_state[f"dpp_opened_{note_id}_card0"] = True
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]

    keys = [w.key for w in
            (list(at.multiselect) + list(at.number_input) + list(at.checkbox)
             + list(at.text_input) + list(at.button) + list(at.radio))
            if w.key]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, sorted(duplicates)

    # And both copies really did draw their own builder, not one of them.
    topics = [w.key for w in at.multiselect if w.key.startswith("dpp_rows_")]
    assert len(topics) == 2, topics


def test_building_files_in_one_copy_withdraws_the_others_downloads() -> None:
    """Both copies write their export files to the SAME paths under the
    note's dpp folder. When the second copy builds, the bytes behind the
    first copy's download buttons have just been replaced by a different
    set — so the first copy must stop serving and offer a rebuild, not hand
    out files that no longer match the preview beside them."""
    from src import dpp_builder

    if not dpp_builder.masters_ready():
        pytest.skip("the QBG workbooks are not on this machine")

    note_id = dmp_service.list_notes()[0]["note_id"]
    card_scope = f"{note_id}_card0"
    at = AppTest.from_file(HARNESS, default_timeout=1800)
    at.session_state["results"] = [_result(note_id)]
    at.session_state["dpp_library_pick"] = note_id
    at.session_state[f"dpp_opened_{note_id}"] = True
    at.session_state[f"dpp_opened_{card_scope}"] = True
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    def build_set_and_files(scope: str) -> None:
        build = next((b for b in at.button if b.key == f"dpp_build_{scope}"),
                     None)
        if build is None or build.disabled:
            pytest.skip("the bank has no questions for this lecture's topics")
        build.click()
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
        at.button(key=f"dpp_build_files_{scope}").click()
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]

    build_set_and_files(note_id)
    state = at.session_state.filtered_state
    assert state.get(f"dpp_exports_{note_id}"), "the library build served nothing"

    build_set_and_files(card_scope)
    state = at.session_state.filtered_state
    assert state.get(f"dpp_exports_{card_scope}"), "the card build served nothing"
    assert not state.get(f"dpp_exports_{note_id}"), (
        "the library copy kept serving downloads whose bytes the card build "
        "had just replaced on disk")
