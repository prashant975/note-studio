"""The page budget: how long notes may run, and what happens when they run longer.

The layout is fixed to the published PW house style, so pages can no longer be
bought back with smaller type — the only lever left is how many words get
written. These tests defend the two halves of that:

* the arithmetic (a page band and a measured, per-language words-per-page rate
  become a word budget that reaches all three prompts), and
* the safety rule on the condensing pass, which is the one stage in the run that
  can silently throw away something the faculty taught.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    DCN_TARGET_PAGES,
    DCN_WORDS_PER_PAGE,
    DCN_WORDS_PER_PAGE_DEFAULT,
)
from src.note_budget import (
    MIN_RETAINED_FRACTION,
    accept_condensed,
    build_condense_prompt,
    budget_block,
    chunk_budget_line,
    chunk_word_budget,
    condense_threshold,
    count_words,
    estimated_pages,
    is_over_budget,
    word_budget,
    words_per_page,
)
from src.prompt_builder import (
    build_dcn_chunk_prompt,
    build_dcn_merge_prompt,
    build_upsc_dcn_prompt,
)

NOTES = """#LECTURE_TITLE: L04_DCN04_Tribal Structure

#TOPICS_COVERED_START
- tribal diversity
#TOPICS_COVERED_END

#H1: Tribal Structure

#H2: Why it matters

- Tribal structure is a **defining** feature of Indian society.
  - The Munda of West Bengal speak Bengali.

#TABLE_START
Group | Origin
Negroids | Africa
#TABLE_END
"""


# ------------------------------------------------------------ counting words

def test_markers_are_not_counted_as_words():
    """The budget has to mean the same thing the reader sees.

    "#H2: Why it matters" is three words of notes and one of scaffolding. If the
    scaffolding counted, a heavily structured note would hit the ceiling several
    per cent earlier than a flat one saying exactly as much.
    """
    assert count_words("#H2: Why it matters") == 3
    assert count_words("- One short point here") == 4
    assert count_words("  - nested point") == 2
    assert count_words("1. numbered step") == 2
    assert count_words("#TOPICS_COVERED_START") == 0
    assert count_words("#TABLE_START") == 0
    assert count_words("#FIGURE: fig_01") == 0


def test_bold_markup_does_not_inflate_the_count():
    assert count_words("- a **defining** feature") == 3
    assert count_words("Group | Origin") == 2


def test_devanagari_counts_the_same_way():
    """One counter for both builds: Hindi splits on whitespace like English."""
    assert count_words("#H2: जनजातीय संरचना एवं विविधता") == 4
    assert count_words("- भारतीय समाज की **प्रमुख** विशेषता है।") == 6


def test_an_empty_or_missing_draft_counts_zero():
    assert count_words("") == 0
    assert count_words(None) == 0


# --------------------------------------------------------------- the budget

def test_the_word_budget_is_the_page_band_times_the_measured_rate():
    """Config holds a page band; everything else is derived from it.

    Written as arithmetic rather than as literals so that moving the band moves
    the budget, which is the whole point of keeping it in one place.
    """
    low, high = DCN_TARGET_PAGES
    assert (low, high) == (10, 15)
    for language_code in ("en", "hi"):
        rate = words_per_page(language_code)
        assert word_budget(language_code) == (low * rate, high * rate)


def test_hindi_is_allowed_more_words_than_english_for_the_same_pages():
    """The reason the rate is per language at all.

    Devanagari words are shorter, so a Hindi page carries about 20% more of them.
    A single shared rate would have made one of the two builds systematically
    wrong — Hindi notes cut short, or English notes let run long.
    """
    assert words_per_page("hi") > words_per_page("en")
    assert word_budget("hi")[1] > word_budget("en")[1]
    assert words_per_page("hi") == DCN_WORDS_PER_PAGE["hi"]
    # Marathi is Devanagari too and inherits the Hindi rate until a real Marathi
    # run is measured. It used to stand in for "unknown" here; it is supported now.
    assert words_per_page("mr") == DCN_WORDS_PER_PAGE["hi"]
    assert words_per_page("ta") == DCN_WORDS_PER_PAGE_DEFAULT, "unknown language falls back"
    assert words_per_page("") == DCN_WORDS_PER_PAGE_DEFAULT


@pytest.mark.parametrize("language_code", ["en", "hi", "mr"])
def test_a_note_at_the_ceiling_estimates_to_the_top_of_the_band(language_code):
    low, high = word_budget(language_code)
    assert estimated_pages(high, language_code) == pytest.approx(DCN_TARGET_PAGES[1])
    assert estimated_pages(low, language_code) == pytest.approx(DCN_TARGET_PAGES[0])


@pytest.mark.parametrize("language_code", ["en", "hi"])
def test_a_note_slightly_over_budget_is_left_alone(language_code):
    """"A little plus minus will be okay" — a run is not re-billed to the model
    to shave off two per cent."""
    _, high = word_budget(language_code)
    assert high < condense_threshold(language_code) < high * 1.25
    assert not is_over_budget("word " * (high + 10), language_code)
    assert is_over_budget("word " * (condense_threshold(language_code) + 50), language_code)


def test_each_part_of_a_long_lecture_gets_a_share_not_the_whole_budget():
    """The long-lecture path writes parts separately and joins them. A budget
    applied only at the merge arrives too late to do anything but delete work."""
    _, high = word_budget("en")
    assert chunk_word_budget(4, "en") == high // 4
    assert chunk_word_budget(1, "en") == high
    assert sum(chunk_word_budget(4, "en") for _ in range(4)) <= high


def test_a_class_split_very_finely_still_gets_a_usable_share():
    """A floor, or a class split 40 ways asks each part for barely a paragraph —
    less than it takes to state the topic it covers."""
    assert chunk_word_budget(40, "en") >= 250


# ------------------------------------------------- the budget reaches the model

@pytest.mark.parametrize("language_code", ["en", "hi"])
def test_the_single_call_prompt_carries_the_budget(language_code):
    prompt = build_upsc_dcn_prompt(subject="society", language_code=language_code,
                                   transcript="lecture " * 40)
    assert "{length_budget}" not in prompt, "the placeholder was left unfilled"
    assert f"{word_budget(language_code)[1]:,}" in prompt
    assert str(DCN_TARGET_PAGES[1]) in prompt


@pytest.mark.parametrize("language_code", ["en", "hi"])
def test_the_chunk_prompt_asks_for_a_share_and_no_longer_forbids_tightening(language_code):
    """The old wording was "Do NOT compress", with no budget at all — which is
    why a long class produced notes that scaled with the recording."""
    prompt = build_dcn_chunk_prompt(subject="society", depth="balanced",
                                    chunk_index=2, chunk_total=4,
                                    transcript_chunk="lecture " * 40,
                                    language_code=language_code)
    assert f"{chunk_word_budget(4, language_code):,}" in prompt
    assert "Do NOT compress" not in prompt


def test_the_merge_prompt_no_longer_asks_for_the_sum_of_its_parts():
    prompt = build_dcn_merge_prompt(subject="society", depth="balanced",
                                    parts=["a", "b"], lecture_title="T")
    assert "as long as the parts added together" not in prompt
    assert f"{word_budget('en')[1]:,}" in prompt
    # It must still refuse to summarise: the failure this replaced was a merge
    # that returned an overview of four parts.
    assert "NOT A SUMMARY" in prompt


@pytest.mark.parametrize("language_code", ["en", "hi"])
def test_the_budget_block_names_the_real_numbers(language_code):
    block = budget_block(language_code)
    assert f"{word_budget(language_code)[1]:,}" in block
    assert f"{DCN_TARGET_PAGES[0]}-{DCN_TARGET_PAGES[1]}" in block
    line = chunk_budget_line(1, 3, language_code)
    assert f"{chunk_word_budget(3, language_code):,}" in line


def test_the_hindi_budget_is_written_in_hindi():
    """An English instruction inside a Hindi prompt is the one that gets ignored."""
    assert "शब्द" in budget_block("hi")
    assert "LENGTH BUDGET" not in budget_block("hi")


# ------------------------------------------------------- the condensing pass

def test_the_condense_prompt_protects_what_a_student_is_examined_on():
    prompt = build_condense_prompt(NOTES, language_code="en")
    for protected in ("Article", "Act", "scheme", "committee", "definition",
                      "table", "date"):
        assert protected.lower() in prompt.lower(), protected
    assert "#LECTURE_TITLE" in prompt
    assert NOTES.strip() in prompt, "the notes to tighten were not included"


def test_the_condense_prompt_states_the_actual_overrun():
    long_notes = "- " + ("word " * 6000)
    prompt = build_condense_prompt(long_notes, language_code="en")
    assert f"{count_words(long_notes):,} words" in prompt
    assert f"{word_budget('en')[1]:,}" in prompt


# ----------------------------------------- the guard on replacing the notes

def test_a_genuinely_tightened_draft_is_accepted():
    draft = "- " + ("word " * 1000)
    tightened = "- " + ("word " * 700)
    assert accept_condensed(draft, tightened)


def test_a_draft_that_came_back_longer_is_rejected():
    """Nothing was gained, and the result is now further over budget."""
    draft = "- " + ("word " * 1000)
    assert not accept_condensed(draft, "- " + ("word " * 1200))
    assert not accept_condensed(draft, draft)


def test_a_summary_masquerading_as_a_condensed_draft_is_rejected():
    """The real risk in this stage. A model that reads "shorten" as "summarise"
    returns something a third the length, and swapping it in would drop the
    facts the notes exist for — silently, because the run still succeeds.
    """
    draft = "- " + ("word " * 1000)
    assert not accept_condensed(draft, "- " + ("word " * 300))
    # The boundary itself is allowed: exactly half is a hard tightening, not a
    # summary, and the caller has already decided it wants the pages back.
    assert accept_condensed(draft, "- " + ("word " * int(1000 * MIN_RETAINED_FRACTION)))


def test_an_empty_or_failed_response_never_replaces_the_notes():
    draft = "- " + ("word " * 1000)
    for bad in ("", "   ", "\n"):
        assert not accept_condensed(draft, bad)


def test_nothing_is_ever_truncated():
    """The module offers no way to cut notes at a word count, on purpose.

    Truncation is the obvious implementation and the wrong one: it removes
    whatever the class happened to cover last, and nothing downstream can tell
    that it happened.
    """
    import src.note_budget as budget

    assert not [name for name in dir(budget)
                if "truncat" in name.lower() or "trim" in name.lower()]


# ------------------------------------------------- the gate on an actual run

def test_a_run_records_what_it_measured(tmp_path, monkeypatch):
    """The arithmetic above is worth nothing if the pipeline never calls it.

    A mock run has no client, so nothing is condensed — which is the point: this
    covers the measuring and recording half, and proves the gate sits on the
    path every DCN run takes rather than in a module nobody imports.
    """
    from src.pipeline import run_pipeline

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))

    result = run_pipeline(
        None, subject="", language="en", mode="complete", allow_mock=True,
        workflow="upsc", note_type="dcn", upsc_output_format="dcn",
        quality_level="off", generate_error_report=False,
        transcript_text_override=("Indian Society tribal communities caste "
                                  "system urbanisation Article 342. " * 60),
    )

    length = result.metadata.get("note_length")
    assert length, "the run did not record what it measured"
    assert length["draft_words"] > 0
    assert length["budget_words"] == list(word_budget("en"))
    assert length["words_per_page"] == words_per_page("en")
    assert length["final_words"] == length["draft_words"], "a mock run condenses nothing"
    assert length["estimated_pages"] == pytest.approx(
        length["final_words"] / words_per_page("en"), abs=0.1)
