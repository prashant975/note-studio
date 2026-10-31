"""The DPP topic step and hand-picking, driven on a synthetic bank.

The complaint this guards: one lecture offered 69 topics across Polity,
Science, Social Issues and Biology, all ticked. Those are CHAPTERS of the
single tag-workbook subject "Current Affairs", so a subject filter alone was
not enough — the chapter filter and the strong-match default are what fix it.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from src.dmp import service as dmp_service  # noqa: E402

HARNESS = str(Path(__file__).resolve().parent / "dpp_synthetic_harness.py")

pytestmark = pytest.mark.skipif(not dmp_service.list_notes(),
                                reason="no generated lectures on this machine")


@pytest.fixture()
def page():
    note_id = dmp_service.list_notes()[0]["note_id"]
    at = AppTest.from_file(HARNESS, default_timeout=300)
    at.session_state["dpp_library_pick"] = note_id
    at.session_state[f"dpp_opened_{note_id}"] = True
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at, note_id


def _run(at):
    at.run()
    at.run()  # st.rerun() is not followed by AppTest on its own
    assert not at.exception, [str(e.value) for e in at.exception]


def _topics(at, note_id):
    return at.multiselect(key=f"dpp_rows_{note_id}")


def test_the_lecture_subject_leads_and_only_strong_matches_are_ticked(page):
    at, note_id = page
    subject = at.selectbox(key=f"dpp_subject_{note_id}")
    assert subject.options[0].startswith("Current Affairs")
    assert subject.options[-1] == "All subjects"
    topics = _topics(at, note_id)
    assert set(topics.value) == {"ca-st-1", "ca-st-2", "ca-pg-1"}, (
        "the weak Social Issues row, or the other subject, was ticked")
    assert not any("Polity:" in o or "Money Bill" in o for o in topics.options)


def test_a_chapter_narrows_current_affairs_to_one_section(page):
    at, note_id = page
    at.selectbox(key=f"dpp_chapter_{note_id}").set_value("Science and Tech")
    _run(at)
    topics = _topics(at, note_id)
    assert set(topics.value) == {"ca-st-1", "ca-st-2"}
    assert len(topics.options) == 2


def test_weaker_matches_appear_only_when_asked(page):
    at, note_id = page
    at.checkbox(key=f"dpp_weak_{note_id}").check()
    _run(at)
    topics = _topics(at, note_id)
    assert any("Child Rights" in o for o in topics.options)
    assert "ca-si-1" not in topics.value, "a weak match must not be pre-ticked"


def test_questions_can_be_picked_by_hand_and_the_pdf_names_the_subject(page):
    at, note_id = page
    at.radio(key=f"dpp_mode_{note_id}").set_value("Pick questions myself")
    _run(at)
    picks = [c for c in at.checkbox if c.key and c.key.startswith("dpp_pick_")]
    assert len(picks) == 9, [c.key for c in picks]  # 3 topics x 3 difficulties
    picks[4].check()
    _run(at)
    [c for c in at.checkbox if c.key and c.key.startswith("dpp_pick_")][0].check()
    _run(at)
    at.button(key=f"dpp_build_picked_{note_id}").click()
    _run(at)

    slots = at.session_state[f"dpp_slots_{note_id}"]
    assert len(slots) == 2
    # The preview keeps the stem off statement 2.
    shown = [m.value for m in at.markdown if m.value.startswith("**Q")]
    assert all("2\\. Two.  \nWhich of" in q for q in shown), shown

    subject = at.text_input(key=f"dpp_export_subject_{note_id}")
    assert subject.value, "the PDF subject was not prefilled"
    subject.set_value("Environment & Ecology")
    _run(at)
    at.button(key=f"dpp_build_files_{note_id}").click()
    _run(at)
    built = at.session_state[f"dpp_exports_{note_id}"]
    assert built["signature"][-1] == "Environment & Ecology"
    assert "Environment" in Path(built["files"]["questions"]).name
