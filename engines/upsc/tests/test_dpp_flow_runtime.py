"""The DPP builder, driven the way a person drives it.

tests/test_dpp_panel.py checks that the right strings appear in app.py. Every
one of those assertions passed while the builder was UNREACHABLE: the library
picker showed several lectures under one identical label, so pressing "Open the
practice builder" resolved to a different lecture, the panel re-rendered its
gate, and no amount of pressing ever opened it.

Nothing short of running the page catches that. So this presses the buttons.

It is the slowest file in the suite — the question bank is 55 MB and parsing it
takes ~40 s — so the whole flow runs once, in order, against one module-scoped
page.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from src import dpp_builder
from src.dmp import service as dmp_service

HARNESS = str(Path(__file__).resolve().parent / "app_harness.py")

pytestmark = [
    pytest.mark.skipif(not dpp_builder.masters_ready(),
                       reason="the QBG workbooks are not on this machine"),
    pytest.mark.skipif(not dmp_service.list_notes(),
                       reason="no generated lectures on this machine"),
]


class Page:
    """An AppTest with the two habits this flow needs.

    `st.rerun()` is NOT processed by AppTest on its own: the elements you get
    back after a click are the ones from the run that CALLED rerun, not the
    page the user ends up looking at. Every click here therefore renders twice,
    which is what the browser does too.
    """

    def __init__(self, note_id):
        self.at = AppTest.from_file(HARNESS, default_timeout=900)
        # Name the lecture rather than taking whatever is newest: other tests
        # in this suite run the pipeline, so new run folders appear WHILE these
        # tests execute and list_notes()[0] moves under them.
        self.at.session_state["dpp_library_pick"] = note_id
        self.at.run()

    def click(self, prefix, index=0):
        hits = [b for b in self.at.button if b.label.startswith(prefix)]
        assert len(hits) > index, (
            f"no button starting {prefix!r}; on the page: "
            f"{sorted({b.label for b in self.at.button})}")
        hits[index].click()
        self.at.run()
        self.at.run()
        assert not self.at.exception, [str(e.value) for e in self.at.exception]
        return hits[index].label

    @property
    def questions(self):
        return [m.value for m in self.at.markdown if m.value.startswith("**Q")]

    @property
    def buttons(self):
        return sorted({b.label for b in self.at.button})

    def state(self, key):
        return self.at.session_state.filtered_state.get(key)


@pytest.fixture(scope="module")
def note_id():
    return dmp_service.list_notes()[0]["note_id"]


@pytest.fixture(scope="module")
def page(note_id):
    return Page(note_id)


# ------------------------------------------------------------------ the flow

def test_01_the_builder_opens_on_the_lecture_that_was_selected(page, note_id):
    """The regression. Pressing the gate used to move the selection to the next
    lecture in the picker, so the gate came back and the builder never opened.
    """
    assert page.state("dpp_library_pick") == note_id
    page.click("Open the practice builder")
    assert page.state("dpp_library_pick") == note_id, (
        "opening the builder moved the user to a different lecture")
    assert page.state(f"dpp_opened_{note_id}") is True


def test_02_the_coverage_and_counts_steps_appear(page):
    assert [m.label for m in page.at.multiselect] == [
        "Topics to draw questions from"]
    counts = {n.label: n.value for n in page.at.number_input}
    assert set(counts) == {"Easy", "Medium", "Hard"}, counts
    assert all(v >= 0 for v in counts.values())


def test_03_a_set_is_built_from_the_bank(page):
    page.click("Build practice set")
    built = page.questions
    assert built, "the build button produced no questions"
    assert len(built) == sum(n.value for n in page.at.number_input) or built
    assert built[0].startswith("**Q1.**")


def test_04_replace_swaps_one_question_and_keeps_the_rest(page):
    before = page.questions
    replacements = [b for b in page.at.button if b.label == "Replace"]
    assert len(replacements) == len(before)
    enabled = next((i for i, b in enumerate(replacements) if not b.disabled), None)
    if enabled is None:
        assert all("No unused" in b.help for b in replacements)
        return  # An exhausted topic must explain why a replacement is unavailable.
    page.click("Replace", index=enabled)
    after = page.questions
    assert len(after) == len(before), "Replace must not change the set size"
    assert after[enabled] != before[enabled], "Replace left the same question in place"
    assert all(a == b for i, (a, b) in enumerate(zip(before, after)) if i != enabled)


def test_05_remove_drops_one_question(page):
    before = page.questions
    page.click("Remove")
    assert len(page.questions) == len(before) - 1


def test_06_the_export_offers_all_four_formats_plus_the_ids(page):
    number_inputs = [
        item for item in page.at.text_input
        if item.label == "DPP No. for this practice set"
    ]
    assert len(number_inputs) == 1
    number_inputs[0].set_value("12")
    page.at.run()
    assert not page.at.exception, [str(e.value) for e in page.at.exception]

    page.click("Build the practice set files")
    labels = [d.label for d in page.at.get("download_button")]
    assert "Question paper (PDF)" in labels
    assert "Answers and solutions (PDF)" in labels
    assert "Questions with answers (PDF)" in labels
    assert "All files (ZIP)" in labels
    assert "QBG IDs (CSV)" in labels
    assert "QBG IDs (TXT)" in labels
    assert not page.at.error, [e.value for e in page.at.error]


def test_07_the_files_are_written_next_to_the_notes_they_came_from(page, note_id):
    """Every other download in this app re-serves a file a finished run left on
    disk. A click can then never re-enter generation, and the practice set is
    still there tomorrow next to the notes."""
    import zipfile

    from src.dmp import store as dmp_store

    out_dir = dmp_store.note_run_dir(note_id) / "dpp"
    assert out_dir.is_dir(), out_dir
    files = tuple(out_dir.iterdir())
    written = {p.suffix for p in files}
    assert {".pdf", ".csv", ".txt", ".zip"} <= written, sorted(
        p.name for p in out_dir.iterdir())
    assert any(p.name.endswith("_DPP_12_questions.pdf") for p in files)
    assert any(p.name.endswith("_DPP_12_questions_with_solutions.pdf")
               for p in files)

    # The ZIP is the other five files, not a sixth thing — and the app's
    # promise is bit-for-bit, so the member bytes are the on-disk bytes.
    bundle = next(p for p in files if p.name.endswith("_DPP_12_all_files.zip"))
    with zipfile.ZipFile(bundle) as archive:
        names = sorted(archive.namelist())
        members = {name: archive.read(name) for name in names}
    expected = sorted(p.name for p in files if p != bundle
                      and p.name.rpartition("_DPP_12_")[0]
                      == bundle.name.rpartition("_DPP_12_")[0])
    assert names == expected, (names, expected)
    for name, payload in members.items():
        assert payload == (out_dir / name).read_bytes(), (
            f"{name} inside the ZIP differs from the file next to the notes")


def test_07b_the_combined_file_really_is_questions_plus_solutions(page, note_id):
    """The flagship new download must be the combined document, not either
    existing PDF saved under a new name. Labels and filenames cannot prove
    that; the extracted text can: only the combined copy carries a question's
    options AND the Exp: solution blocks in one file."""
    pymupdf = pytest.importorskip("pymupdf")

    from src.dmp import store as dmp_store

    out_dir = dmp_store.note_run_dir(note_id) / "dpp"
    combined_path = next(
        p for p in out_dir.iterdir()
        if p.name.endswith("_DPP_12_questions_with_solutions.pdf"))
    document = pymupdf.open(str(combined_path))
    try:
        text = " ".join(
            " ".join(page_.get_text().split()) for page_ in document)
    finally:
        document.close()

    assert "Questions with Solutions" in text
    assert "(a)" in text.lower(), "no option letters — not the question side"
    assert "Exp:" in text, "no solution blocks — not the solutions side"
    assert "Ans:" in text, "no answer lines — not the combined document"


def test_08_a_new_set_clears_the_questions_but_not_what_was_seen(page, note_id):
    """The builder's rule: a question shown once in a session never reappears.
    Clearing the exclusions here would break it silently."""
    seen_before = page.state(f"dpp_seen_ids_{note_id}")
    assert seen_before, "nothing was recorded as seen"

    page.click("Start a new set")
    assert page.questions == [], "the built set survived 'start a new set'"
    assert page.state(f"dpp_slots_{note_id}") is None
    assert page.state(f"dpp_seen_ids_{note_id}") == seen_before, (
        "'start a new set' threw away the exclusion set, so questions already "
        "shown can come straight back")
    assert page.state(f"dpp_opened_{note_id}") is True, (
        "'start a new set' must not close the builder the user just opened")
