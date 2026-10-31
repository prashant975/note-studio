"""Reading a model answer into the blocks the house-format writer draws.

A model returns Markdown-ish prose. The sheet needs structure: which lines are
section headings, which are bullets and how deep, and which spans are bold. Get
this wrong and the document still renders — as an undifferentiated wall with
asterisks in it, which is exactly the failure that is easy to ship and hard to
notice in a test that only checks the file opens.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp.answer_parse import (
    answer_word_count,
    fold_answer_blocks,
    is_section_marker,
    parse,
    parse_answer_blocks,
    parse_approach,
    split_approach,
    split_bold,
    strip_point_label,
)


# ------------------------------------------------------------------- bold

def test_plain_text_is_one_unbolded_segment():
    assert split_bold("just a sentence") == [("just a sentence", False)]


def test_a_bold_span_is_split_out_and_keeps_its_neighbours():
    assert split_bold("see the **RTI Act** today") == [
        ("see the ", False), ("RTI Act", True), (" today", False)]


def test_a_bold_lead_in_label_keeps_the_text_after_it():
    segments = split_bold("**Accountability:** it reverses secrecy.")
    assert segments[0] == ("Accountability:", True)
    assert segments[1][1] is False and "reverses secrecy" in segments[1][0]


def test_several_bold_spans_in_one_line_all_survive():
    segments = split_bold("**NITI Aayog** and the **NSSO** both publish")
    assert [s for s in segments if s[1]] == [("NITI Aayog", True), ("NSSO", True)]


def test_empty_text_produces_nothing_rather_than_an_empty_run():
    assert split_bold("") == []
    assert split_bold(None) == []


# --------------------------------------------------------------- approach

def test_the_approach_and_the_answer_are_separated_on_their_markers():
    approach, body = split_approach(
        "# APPROACH\n- Introduction: open it\n\n# ANSWER\nThe answer itself.")
    assert "Introduction: open it" in approach
    assert body.strip() == "The answer itself."


def test_an_answer_with_no_approach_section_is_all_answer():
    """Every answer generated before the approach was asked for. It must come
    back whole, not be silently swallowed as an approach."""
    approach, body = split_approach("Just an answer with no markers at all.")
    assert approach == ""
    assert body.strip() == "Just an answer with no markers at all."


def test_the_three_approach_lines_are_recognised():
    parsed = parse_approach(
        "- Introduction: define it\n- Body: establish it\n- Conclusion: close it")
    assert parsed["introduction"] == "define it"
    assert parsed["body"] == "establish it"
    assert parsed["conclusion"] == "close it"


def test_a_hyphen_separated_label_keeps_its_content():
    """The label regex accepts ":", "：" and "-" as the separator, but content
    extraction used to look only for an ASCII colon — so the very lines the
    regex matched leniently came back empty, and an incidental colon later in
    the sentence cut everything before it."""
    parsed = parse_approach(
        "- Introduction - open with the Kesavananda ruling\n"
        "- Body: contrast Art 368: procedure vs basic structure\n"
        "- Conclusion： close on the doctrine")
    assert parsed["introduction"] == "open with the Kesavananda ruling"
    assert parsed["body"] == "contrast Art 368: procedure vs basic structure"
    assert parsed["conclusion"] == "close on the doctrine"


def test_an_indented_line_under_body_becomes_a_sub_point():
    parsed = parse_approach(
        "- Introduction: define it\n- Body: establish it\n"
        "  - note the limitation\n- Conclusion: close it")
    assert parsed["body_subpoints"] == ["note the limitation"]


def test_the_hindi_approach_labels_are_recognised():
    """The prompt asks for the section markers in English but the CONTENT in
    the answer's language, so the labels arrive in Devanagari."""
    parsed = parse_approach(
        "- भूमिका: परिचय दीजिए\n- मुख्य भाग: स्पष्ट कीजिए\n- निष्कर्ष: समापन कीजिए")
    assert parsed["introduction"] == "परिचय दीजिए"
    assert parsed["body"] == "स्पष्ट कीजिए"
    assert parsed["conclusion"] == "समापन कीजिए"


def test_a_bolded_approach_label_is_still_recognised():
    parsed = parse_approach("- **Introduction:** define it")
    assert parsed["introduction"] == "define it"


def test_an_absent_approach_parses_to_empty_strings():
    parsed = parse_approach("")
    assert parsed == {"introduction": "", "body": "", "conclusion": "",
                      "body_subpoints": []}


# ----------------------------------------------------------------- blocks

def test_a_markdown_heading_becomes_a_heading_block():
    assert parse_answer_blocks("## Why It Matters") == [("heading", "Why It Matters")]


def test_a_line_that_is_only_a_bold_label_becomes_a_heading():
    """The common shape. Models bold a section name far more often than they
    use a hash, and reading it as body text is what flattens the answer."""
    assert parse_answer_blocks("**Limitations of the Act:**") == [
        ("heading", "Limitations of the Act")]


def test_a_bullet_keeps_its_bold_lead_in_and_does_not_become_a_heading():
    blocks = parse_answer_blocks("- **Accountability:** it reverses secrecy.")
    assert len(blocks) == 1
    kind, level, segments = blocks[0]
    assert (kind, level) == ("bullet", 0)
    assert segments[0] == ("Accountability:", True)


def test_indentation_sets_the_bullet_level():
    blocks = parse_answer_blocks("- top\n  - second\n    - third")
    assert [b[1] for b in blocks] == [0, 1, 2]


def test_the_level_is_capped_at_the_three_glyphs_we_draw():
    blocks = parse_answer_blocks("        - very deep")
    assert blocks[0][1] == 2


@pytest.mark.parametrize("marker", ["-", "*", "•", "▪", "1.", "2)"])
def test_the_markers_a_model_actually_uses_are_all_bullets(marker):
    blocks = parse_answer_blocks(f"{marker} a point")
    assert blocks and blocks[0][0] == "bullet"


def test_emphasis_is_not_mistaken_for_a_bullet():
    """`* item` is a list marker and `**item**` is emphasis. A naive
    startswith("*") reads every bolded label as a bullet."""
    blocks = parse_answer_blocks("**Conclusion:** a progressive step.")
    assert blocks[0][0] == "paragraph"


def test_a_plain_line_is_a_paragraph():
    blocks = parse_answer_blocks("A consumption line remains a serviceable floor.")
    assert blocks[0][0] == "paragraph"
    assert blocks[0][1] == [("A consumption line remains a serviceable floor.", False)]


@pytest.mark.parametrize("prefix", ["-----", "----- ", "  -----"])
def test_faculty_point_format_keeps_explanation_and_example_in_one_bullet(prefix):
    blocks = parse_answer_blocks(
        prefix + "**Accountability:** Citizens inspect expenditure. "
        "For example, MGNREGA muster rolls support social audits.")
    assert len(blocks) == 1
    kind, level, segments = blocks[0]
    assert kind == "bullet"
    assert level == (1 if prefix.startswith("  ") else 0)
    assert segments[0] == ("Accountability:", True)
    assert segments[1] == (
        " Citizens inspect expenditure. For example, MGNREGA muster rolls support social audits.", False)


def test_blank_lines_do_not_become_empty_paragraphs():
    assert parse_answer_blocks("one\n\n\ntwo") == [
        ("paragraph", [("one", False)]), ("paragraph", [("two", False)])]


def test_a_bold_sentence_on_its_own_line_is_not_a_heading():
    """A fully bold definition ends like a sentence. Read as a heading, it was
    printed as one — and now would be folded into the point under it."""
    blocks = parse_answer_blocks("**Secularism separates the state from religion.**")
    assert blocks == [("paragraph", [("Secularism separates the state from religion.", True)])]


def test_a_body_with_no_text_still_collects_its_parts():
    """The new approach asks for "Body:" with the parts as bullets under it,
    so the label line itself is empty."""
    parsed = parse_approach(
        "- Introduction: open it\n- Body:\n  - first part\n  - second part\n"
        "- Conclusion: close it")
    assert parsed["body"] == ""
    assert parsed["body_subpoints"] == ["first part", "second part"]
    assert parsed["conclusion"] == "close it"


# -------------------------------------------------- the UPSC answer shape

@pytest.mark.parametrize("bullet,expected", [
    ("-----**Point:** **Revenue expenditure** is an outflow.",
     [("Revenue expenditure", True), (" is an outflow.", False)]),
    ("- **Point: Capital expenditure** creates assets.",
     [("Capital expenditure", True), (" creates assets.", False)]),
    ("- **Point**: An **asset** gives benefits.",
     [("An ", False), ("asset", True), (" gives benefits.", False)]),
    ("- Point 2: plain text.", [("plain text.", False)]),
    ("- **बिंदु:** **संकर संस्कृतियाँ** बनती हैं।",
     [("संकर संस्कृतियाँ", True), (" बनती हैं।", False)]),
])
def test_the_point_label_is_removed_and_the_bold_survives(bullet, expected):
    """The old prompt's "-----**Point:**" put the word "Point:" before every
    bullet, which the content team rejected. It arrives in three bold shapes
    and each has to come out as just the point."""
    blocks = fold_answer_blocks(parse_answer_blocks(bullet))
    assert blocks == [("bullet", 0, expected)]


def test_a_word_that_merely_starts_with_point_is_left_alone():
    segments = [("Point of order: the Speaker rules.", False)]
    assert strip_point_label(segments) is segments


def test_a_paragraph_carrying_a_point_label_becomes_a_bullet():
    blocks = fold_answer_blocks(parse_answer_blocks("**Point:** Citizens inspect."))
    assert blocks == [("bullet", 0, [("Citizens inspect.", False)])]


@pytest.mark.parametrize("marker", [
    "## INTRODUCTION", "## Body", "## Conclusion", "**Conclusion:**",
    "## भूमिका", "## मुख्य भाग", "## निष्कर्ष", "## प्रस्तावना"])
def test_the_three_section_markers_are_never_printed(marker):
    assert is_section_marker(parse_answer_blocks(marker)[0][1])
    assert fold_answer_blocks(parse_answer_blocks(marker + "\nText.")) == [
        ("paragraph", [("Text.", False)])]


def test_a_heading_that_only_starts_like_a_marker_is_content():
    assert not is_section_marker("Introduction to Federalism")


def test_a_stray_sub_heading_folds_into_the_point_under_it():
    """The reported answer: "Conceptual Basis of the Distinction" printed as a
    heading inside the body. It is not printed as one any more — its words
    become the bold lead-in of the first point beneath it, so an older answer's
    for/against split survives without a heading."""
    blocks = fold_answer_blocks(parse_answer_blocks(
        "## Conceptual Basis of the Distinction\n"
        "-----**Point:** **Revenue expenditure** meets running costs.\n"
        "-----**Point:** **Capital expenditure** creates assets."))
    assert [b[0] for b in blocks] == ["bullet", "bullet"]
    assert blocks[0][2][0] == ("Conceptual Basis of the Distinction: ", True)
    assert blocks[0][2][1] == ("Revenue expenditure", True)
    assert blocks[1][2][0] == ("Capital expenditure", True)


def test_a_bold_label_sub_heading_folds_into_a_paragraph_too():
    blocks = fold_answer_blocks(parse_answer_blocks(
        "**Fiscal implications:**\nBorrowing for assets can pay for itself."))
    assert blocks == [("paragraph", [("Fiscal implications: ", True),
                                     ("Borrowing for assets can pay for itself.", False)])]


def test_a_sub_heading_with_nothing_under_it_is_dropped():
    blocks = fold_answer_blocks(parse_answer_blocks(
        "Opening paragraph.\n## Way Forward\n## CONCLUSION\nClosing paragraph."))
    assert [b[0] for b in blocks] == ["paragraph", "paragraph"]
    assert "Way Forward" not in str(blocks)


def test_a_new_style_answer_is_a_paragraph_points_and_a_paragraph():
    _approach, blocks = parse(
        "# APPROACH\n- Introduction: open\n- Body:\n  - part one\n- Conclusion: close\n\n"
        "# ANSWER\n## INTRODUCTION\nThe opening.\n## BODY\n"
        "- **First idea:** one. For example, a.\n- **Second idea:** two.\n"
        "## CONCLUSION\nThe close.")
    assert [b[0] for b in blocks] == ["paragraph", "bullet", "bullet", "paragraph"]
    assert "INTRODUCTION" not in str(blocks) and "BODY" not in str(blocks)


# ------------------------------------------------------------- word count

def test_the_word_count_is_the_answer_only():
    """The approach box, the markers and the Markdown are not words a
    candidate writes, and the app's own cut-off note is not either."""
    answer = ("# APPROACH\n- Introduction: these words do not count at all\n\n"
              "# ANSWER\n## INTRODUCTION\nOne two three.\n## BODY\n"
              "-----**Point:** **Four** five — six.\n## CONCLUSION\nSeven eight.\n\n"
              "> NOTE: This answer reached the generation length limit.")
    assert answer_word_count(answer) == 8


def test_devanagari_words_are_counted_like_any_other():
    assert answer_word_count("# ANSWER\nभारत में विविधता है।") == 4


def test_an_answer_with_no_markers_is_counted_whole():
    assert answer_word_count("Just five words right here.") == 5
    assert answer_word_count("") == 0


# ------------------------------------------------------------- end to end

def test_a_full_answer_parses_into_an_approach_and_a_structured_body():
    approach, blocks = parse(
        "# APPROACH\n"
        "- Introduction: define it\n"
        "- Body: establish it\n"
        "  - note the gap\n"
        "- Conclusion: close it\n\n"
        "# ANSWER\n"
        "**The Act made information a right.** It is administered by the **CIC**.\n\n"
        "## Why It Matters\n"
        "- **Accountability:** it reverses secrecy.\n"
        "  - proactive, not responsive.\n\n"
        "A progressive step.")
    assert approach["introduction"] == "define it"
    assert approach["body_subpoints"] == ["note the gap"]
    kinds = [b[0] for b in blocks]
    # The sub-heading is folded into the point under it, never a heading.
    assert kinds == ["paragraph", "bullet", "bullet", "paragraph"]
    assert blocks[1][2][:2] == [("Why It Matters: ", True), ("Accountability:", True)]
    assert blocks[2][1] == 1, "the nested bullet kept its level"
    assert ("The Act made information a right.", True) in blocks[0][1]


def test_the_panel_preview_shows_what_the_sheet_prints():
    from src.dmp.answer_parse import preview_markdown

    shown = preview_markdown(
        "# APPROACH\n- Introduction: define it\n- Body: explain it\n- Conclusion: close it\n\n"
        "# ANSWER\n## INTRODUCTION\nThe Budget records receipts.\n\n## BODY\n"
        "- **Point:** **Revenue expenditure** meets running costs, e.g. salaries.\n"
        "## CONCLUSION\nSound classification keeps borrowing accountable.",
        approach_title="How to Approach the Question?", answer_title="Answer")
    assert "INTRODUCTION" not in shown and "## " not in shown and "Point:" not in shown
    assert shown.startswith("**How to Approach the Question?**")
    assert "- **Revenue expenditure** meets running costs, e.g. salaries." in shown
    assert "Sound classification keeps borrowing accountable." in shown
