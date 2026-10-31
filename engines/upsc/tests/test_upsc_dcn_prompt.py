"""Contract tests for the PW OnlyIAS DCN prompt builder.

The DCN DOCX writer parses the model's answer by marker, so the two things that
must never regress are: every template slot is filled (a leaked "{slides_block}"
would ship the placeholder to the model) and the marker list still reaches the
model at the end of a long prompt.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import UPSC_NOTE_DEPTHS
from src.models import SlideData
from src.prompt_builder import (
    PROMPT_DIR,
    build_upsc_dcn_prompt,
    build_dcn_chunk_prompt,
    build_dcn_merge_prompt,
    build_upsc_prompt,
    load_upsc_prompt_template,
)

SLIDES = [
    SlideData(slide_no=1, heading="Ozone Depletion", text="Montreal Protocol, 1987"),
    SlideData(slide_no=2, heading="Air Pollutants", text="PM2.5 limit 60 mg/m3"),
]
TRANSCRIPT = "Today we cover the Montreal Protocol and the CPCB standards for PM2.5."


@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_keyword_links_are_requested_and_preserved_in_every_generation_path(language):
    common = dict(subject="environment", depth="detailed", language_code=language)
    prompts = [
        build_upsc_dcn_prompt(**common, slides=SLIDES, transcript=TRANSCRIPT),
        build_dcn_chunk_prompt(**common, chunk_index=1, chunk_total=2,
                               transcript_chunk=TRANSCRIPT, slides=SLIDES),
        build_dcn_merge_prompt(**common, parts=["#H1: A\n- A point."],
                               lecture_title="Environment"),
    ]
    for prompt in prompts:
        assert "IMPORTANT KEYWORD HYPERLINKS" in prompt
        assert "[**keyword**](https://source/path)" in prompt
        assert "first substantive body mention" in prompt
        assert "constitutional Articles, Acts, schemes" in prompt
        assert "Preserve existing useful keyword hyperlinks" in prompt
        assert "bold and unlinked" in prompt
        assert "confidence in a domain alone" in prompt


def test_template_file_loads_through_the_shared_loader():
    template = load_upsc_prompt_template("dcn", "en")
    assert template == (PROMPT_DIR / "upsc_dcn_en.txt").read_text(encoding="utf-8")
    for slot in ("{subject}", "{depth}", "{transcript_block}", "{slides_block}"):
        assert slot in template


def test_all_placeholders_are_substituted():
    prompt = build_upsc_dcn_prompt(
        subject="environment",
        depth="detailed",
        slides=SLIDES,
        transcript=TRANSCRIPT,
    )
    for slot in ("{subject}", "{depth}", "{transcript_block}", "{slides_block}",
                 "{lecture_title}", "{glossary_block}"):
        assert slot not in prompt
    # Nothing brace-shaped may survive anywhere in the assembled prompt.
    assert re.search(r"\{[A-Za-z_]+\}", prompt) is None
    assert "Environment" in prompt
    # {depth} now carries the depth *rules*, not the bare label — substituting
    # the label alone left the picker inert.
    assert "NOTE DEPTH — DETAILED" in prompt
    assert "DETAILED: this is the default" in prompt


def test_sources_are_assembled_like_the_note_type_builder():
    prompt = build_upsc_dcn_prompt(subject="environment", slides=SLIDES, transcript=TRANSCRIPT)
    assert "CLASS TRANSCRIPT STARTS" in prompt and "CLASS TRANSCRIPT ENDS" in prompt
    assert TRANSCRIPT in prompt
    assert "SLIDE REFERENCE STARTS" in prompt and "SLIDE REFERENCE ENDS" in prompt
    assert "[Slide 2] heading: Air Pollutants" in prompt
    assert "PM2.5 limit 60 mg/m3" in prompt


def test_missing_transcript_and_slides_are_stated_explicitly():
    prompt = build_upsc_dcn_prompt(subject="auto")
    assert "CLASS TRANSCRIPT: [not available for this class]" in prompt
    assert "SLIDE REFERENCE STARTS" in prompt
    assert "Not specified — infer it from the lecture" in prompt


def test_each_fact_mode_injects_its_own_instruction():
    source_only = build_upsc_dcn_prompt(subject="environment", fact_mode="source_only", slides=SLIDES)
    verify = build_upsc_dcn_prompt(subject="environment", fact_mode="verify", slides=SLIDES)
    mark = build_upsc_dcn_prompt(subject="environment", fact_mode="mark_uncertain", slides=SLIDES)

    assert "FACT MODE: SOURCE ONLY" in source_only
    assert "not in the lecture (transcript or slides)" in source_only
    assert "Still flag internal contradictions" in source_only
    # The verify-mode source roster (CPCB, IUCN...) must not leak into
    # source-only mode. (MoEFCC itself now appears in the base CITATIONS
    # section, so it is no longer a usable discriminator.)
    assert "CPCB" not in source_only

    assert "FACT MODE: VERIFY CURRENT FACTS WHERE POSSIBLE" in verify
    for source in ("gov.in", "PIB", "MoEFCC", "CPCB", "NITI Aayog", "IPCC", "IUCN", "Ramsar", "CBD", "CITES"):
        assert source in verify
    assert "[Needs Verification: reason]" in verify

    assert "FACT MODE: MARK UNCERTAIN CURRENT DATA" in mark
    assert "Do NOT silently update current data" in mark
    assert "Mark EVERY current-data claim" in mark
    # The one carve-out: a claim the lecture itself dates stays unmarked.
    assert "The single exception is a claim the lecture itself dates" in mark


def test_unknown_fact_mode_falls_back_to_the_default():
    fallback = build_upsc_dcn_prompt(subject="environment", fact_mode="nonsense")
    assert "FACT MODE: VERIFY CURRENT FACTS WHERE POSSIBLE" in fallback


# #SEPARATOR and #TOPICS_COVERED are deliberately absent: the layout draws no
# rules and has no contents block, so the prompt no longer teaches them.
DCN_MARKER_LIST = (
    "#LECTURE_TITLE:", "#H1:", "#H2:", "#H3:",
    "#TABLE_START", "#TABLE_END", "#FLOW_START", "#FLOW_END",
)


def test_marker_list_survives_into_the_final_prompt():
    """With the error report off, the response is marker text and nothing else —
    the original DCN contract, unchanged."""
    prompt = build_upsc_dcn_prompt(
        subject="environment", slides=SLIDES, transcript=TRANSCRIPT, error_report=False)
    for marker in DCN_MARKER_LIST:
        assert marker in prompt
    tail = prompt[-1200:]
    assert "OUTPUT PROTOCOL [FINAL REMINDER" in tail
    assert "#LECTURE_TITLE:" in tail
    # The marker-only rule must not be contradicted by the appended blocks.
    assert "Nothing outside the markers and the note content" in tail
    assert "NOTES_START" not in prompt
    assert "QUALITY_REPORT_START" not in prompt
    assert "ERROR_REPORT" not in prompt
    assert "Concepts Covered" in prompt and "Add a Concepts Covered" not in prompt


def test_error_report_fences_the_dcn_notes_so_the_json_cannot_reach_them():
    """DCN notes have no wrapper of their own, so switching the error report on
    has to add one. Without it there is no boundary between the last bullet and
    the JSON array, and the array gets typeset as a bullet."""
    prompt = build_upsc_dcn_prompt(
        subject="environment", slides=SLIDES, transcript=TRANSCRIPT, error_report=True)
    for marker in DCN_MARKER_LIST:
        assert marker in prompt
    assert "# NOTES_START" in prompt and "# NOTES_END" in prompt
    assert "# ERROR_REPORT_START" in prompt and "# ERROR_REPORT_END" in prompt
    # Still no AI-written quality report: DCN corrections stay inline.
    assert "QUALITY_REPORT_START" not in prompt
    # The contract block is last, so nothing after it can relax the JSON rules.
    assert prompt.rstrip().endswith("that is not an error.")


def test_error_report_is_on_by_default_for_dcn():
    assert "# ERROR_REPORT_START" in build_upsc_dcn_prompt(subject="environment")


@pytest.mark.parametrize("level,needle", [
    ("basic", "SCOPE FOR THIS RUN: BASIC"),
    ("standard", "SCOPE FOR THIS RUN: STANDARD"),
    ("strict", "SCOPE FOR THIS RUN: STRICT"),
    ("nonsense", "SCOPE FOR THIS RUN: STANDARD"),
])
def test_strictness_reaches_the_prompt(level, needle):
    prompt = build_upsc_dcn_prompt(
        subject="environment", error_report=True, error_report_strictness=level)
    assert needle in prompt


def test_quality_options_become_short_directives():
    prompt = build_upsc_dcn_prompt(
        subject="environment",
        slides=SLIDES,
        quality_level="strict",
        quality_options={"add_tables": False, "add_flowcharts": False, "check_duplicates": False},
    )
    assert "QUALITY CHECK LEVEL: STRICT" in prompt
    assert "Do not use #TABLE_START / #TABLE_END; write the same content as bullets." in prompt
    assert "Do not use #FLOW_START / #FLOW_END; write the same content as bullets." in prompt
    assert "Merge repeated or duplicated points into a single bullet." not in prompt.split(
        "Switched off for this run"
    )[0]
    assert "Bold the Prelims-facing keywords" in prompt


def test_quality_level_off_suppresses_corrections_but_keeps_fact_mode():
    prompt = build_upsc_dcn_prompt(
        subject="environment",
        slides=SLIDES,
        quality_level="off",
        quality_options={"check_facts": True, "add_correction_pointers": True},
    )
    assert "QUALITY CHECK LEVEL: OFF" in prompt
    assert "Correction wording is disabled" in prompt
    assert "Fact-check each substantive faculty point" not in prompt.split("Switched off for this run")[0]
    assert "FACT MODE: VERIFY CURRENT FACTS WHERE POSSIBLE" in prompt


def test_note_type_builder_is_unchanged():
    prompt = build_upsc_prompt(
        note_type="complete",
        area="environment",
        language_code="en",
        slides=SLIDES,
        transcript=TRANSCRIPT,
    )
    assert "# NOTES_START" in prompt
    assert "# QUALITY_REPORT_END" in prompt
    assert f"CLASS TRANSCRIPT STARTS\n{TRANSCRIPT}\nCLASS TRANSCRIPT ENDS" in prompt
    assert "SLIDE REFERENCE STARTS\n[Slide 1] heading: Ozone Depletion" in prompt
    # The error report is appended after the marker protocol, so the protocol is
    # the end of the prompt only when the report is switched off.
    assert "# ERROR_REPORT_START" in prompt
    assert prompt.rstrip().endswith("that is not an error.")
    without = build_upsc_prompt(
        note_type="complete", area="environment", language_code="en",
        slides=SLIDES, transcript=TRANSCRIPT, error_report=False,
    )
    assert "ERROR_REPORT" not in without
    assert without.rstrip().endswith("its own line between steps.")


# --------------------------------------------------------------- output language
# The reported failure: a Hindi-taught class produced Hindi notes for someone who
# wanted English. Two separate things have to hold — the choice has to reach the
# right template, and that template has to hold the line against a source in the
# other language, because a model asked to write from a Hindi transcript will
# happily answer in Hindi.

DEVANAGARI = re.compile(r"[\u0900-\u097F]")

HINDI_TRANSCRIPT = (
    "तो देखिए बच्चों, probity का मतलब होता है ईमानदारी। "
    "RTI Act 2005 के तहत नागरिक सूचना माँग सकते हैं। "
) * 20


def _dcn(language_code: str, transcript: str = TRANSCRIPT) -> str:
    return build_upsc_dcn_prompt(
        subject="ethics", depth="detailed", fact_mode="balanced",
        slides=SLIDES, transcript=transcript, quality_level="strict",
        quality_options={}, error_report=True, error_report_strictness="standard",
        language_code=language_code, lecture_title="L03_DCN03_Probity", glossary=[],
    )


def test_the_language_code_picks_the_matching_template():
    assert "in ENGLISH" in _dcn("en")
    assert "in HINDI" in _dcn("hi")
    assert _dcn("en") != _dcn("hi")


def test_the_english_prompt_refuses_to_mirror_a_hindi_transcript():
    """Before this, the English template said "Language: English" once, in a
    metadata line at the very end. The Hindi one led with a LANGUAGE section;
    the English one has to as well, or the transcript sets the output language."""
    prompt = _dcn("en", HINDI_TRANSCRIPT)
    head = prompt[:prompt.index("SOURCE PRIORITY")]
    assert "LANGUAGE [READ THIS FIRST]" in head, "the language rule must come first"
    assert "Write every word of the notes in English." in head
    # It has to name the situation, not just the desired output.
    assert "Hindi" in head and "transcript" in head.lower()
    assert "no Hinglish" in head


def test_the_english_prompt_carries_hindi_only_as_a_worked_example():
    """The examples are Devanagari, which is the point — they show the model the
    input it will get. They must sit under the "Wrong" heading or as source
    text, never as the shape of the answer."""
    prompt = _dcn("en", TRANSCRIPT)          # an English transcript
    head = prompt[:prompt.index("SOURCE PRIORITY")]
    assert DEVANAGARI.search(head), "the rule needs a concrete before/after"
    for line in head.split("\n"):
        if DEVANAGARI.search(line):
            assert any(m in line for m in ("Transcript:", "Wrong", "-", "प्रोबिटी")), line


def test_the_hindi_prompt_keeps_its_own_language_contract():
    prompt = _dcn("hi", HINDI_TRANSCRIPT)
    assert "LANGUAGE [READ THIS FIRST — DEVANAGARI ONLY IN THE BODY]" in prompt
    assert "in HINDI (Devanagari)" in prompt
    assert "स्पेसएक्स" in prompt            # the transliteration rule, by example
    assert "यूएनएफसीसीसी (UNFCCC)" in prompt  # bracketed first-use pattern
    assert "Write every word of the notes in English." not in prompt
