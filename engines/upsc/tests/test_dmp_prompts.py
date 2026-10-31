"""The DMP prompt library, and the two things about it that are easy to get wrong.

The prompts came from a spreadsheet a faculty member wrote. Most of what is
tested here is that the transcription is complete and that the app's own
additions — the JSON contract and the language lock — are appended to every
prompt rather than to most of them.

The other half is the mislabelled cell. The sheet's "DMP MODEL ANSWER" is a
notes-refinement prompt, not an answer writer, and wiring it into the answer
path would produce a tidied copy of the notes under the heading "Model answer".
That is the kind of wrong that looks right.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DMP_LEVELS, dmp_question_count
from src.dmp.prompts import (
    DMP_CONTENT_REFINE_PROMPT,
    DMP_DIRECTIVE_QUESTION_PROMPTS,
    DMP_LEVEL_ANSWER_PROMPTS,
    DMP_LEVEL_QUESTION_PROMPTS,
    build_answer_prompt,
    build_directive_prompt,
    build_question_prompt,
    language_directive,
    question_format_block,
)

NOTES = "## Fundamental Rights\n- Articles 12 to 35 cover the six fundamental rights."


# ------------------------------------------------------------- the transcription

def test_every_level_can_both_ask_and_answer():
    """One missing half of one level is a level that generates questions nobody
    can get an answer to — and it fails only when a user ticks that box."""
    assert set(DMP_LEVEL_QUESTION_PROMPTS) == set(DMP_LEVELS)
    assert set(DMP_LEVEL_ANSWER_PROMPTS) == set(DMP_LEVELS)


def test_the_level_prompts_keep_the_wording_that_makes_them_different():
    """Easy/moderate/applied are not three intensities of one prompt; they ask
    for different kinds of thinking, and that is carried in specific phrases."""
    assert "foundational conceptual clarity" in DMP_LEVEL_QUESTION_PROMPTS["easy"]
    assert "multi-layered" in DMP_LEVEL_QUESTION_PROMPTS["moderate"]
    assert "think like an administrator" in DMP_LEVEL_QUESTION_PROMPTS["applied"]
    assert "foundational conceptual clarity" in DMP_LEVEL_ANSWER_PROMPTS["easy"]
    assert "multi-layered" in DMP_LEVEL_ANSWER_PROMPTS["moderate"]
    assert "Think like an administrator" in DMP_LEVEL_ANSWER_PROMPTS["applied"]


def test_every_answer_prompt_is_grounded_to_the_notes_only():
    """The clause that makes the answer worth reading. Without it the model
    answers from general knowledge and teaches the student material the class
    never covered — which they then carry into the exam as if it had."""
    for level, template in DMP_LEVEL_ANSWER_PROMPTS.items():
        assert "ONLY the provided daily class notes" in template, level
        assert "Do not invent facts beyond the notes." in template, level


def test_all_twenty_two_directives_survived_the_move_off_the_spreadsheet():
    assert len(DMP_DIRECTIVE_QUESTION_PROMPTS) == 22
    for directive, template in DMP_DIRECTIVE_QUESTION_PROMPTS.items():
        assert "UPSC" in template, directive
        # Each one must still carry a placeholder to fill.
        assert "[" in template and "]" in template, directive


# ----------------------------------------------------- what the code appends

@pytest.mark.parametrize("level", DMP_LEVELS)
def test_a_question_prompt_carries_the_notes_the_format_and_the_language(level):
    prompt = build_question_prompt(level, NOTES, language_code="en")
    count = dmp_question_count(level)
    # The sheet's wording survives except for the count, which is substituted
    # (4/3/3) so the template and the JSON contract never state two numbers.
    before, after = DMP_LEVEL_QUESTION_PROMPTS[level].split("exactly 5", 1)
    assert before in prompt and after in prompt
    assert f"exactly {count}" in prompt
    assert "exactly 5" not in prompt
    assert "Return ONLY valid JSON" in prompt
    assert f"Exactly {count} items" in prompt
    assert "-----BEGIN NOTES-----" in prompt and NOTES in prompt


def test_the_levels_split_four_three_three():
    """Ten questions per set — 4 easy, 3 moderate, 3 applied — as asked for
    on 2026-08-31, replacing the sheet's five per level."""
    assert [dmp_question_count(lv) for lv in DMP_LEVELS] == [4, 3, 3]


def test_an_answer_prompt_states_the_word_target_as_a_number():
    """The exam's own limit for the marks, with its ±10% band. The content team
    reported 150-word questions answered at several times the limit — the old
    prompt asked for a ~500-word "teaching answer" on purpose."""
    ten = build_answer_prompt("easy", "Define secularism.", 10, NOTES)
    fifteen = build_answer_prompt("easy", "Define secularism.", 15, NOTES)
    assert "about 150 words" in ten and "between 135 and 165 words" in ten
    assert "about 250 words" in fifteen and "between 225 and 275 words" in fifteen
    assert "COMPLETE model answer" in ten
    assert "QUESTION (10 marks): Define secularism." in ten
    for prompt in (ten, fifteen):
        assert "500" not in prompt
        assert "teaching answer" not in prompt
        assert "longer than the exam" not in prompt


@pytest.mark.parametrize("marks,expected", [
    (10, {"words": 150, "low": 135, "high": 165, "intro": 25, "body": 100,
          "conclusion": 25, "points": "4 to 5"}),
    (15, {"words": 250, "low": 225, "high": 275, "intro": 40, "body": 170,
          "conclusion": 40, "points": "6 to 7"}),
])
def test_the_section_budget_adds_up_to_the_limit(marks, expected):
    """A section budget that sums past the limit is how the old prompt got to
    500 words: 60 + five points per demand + 60."""
    from src.dmp.prompts import answer_plan

    plan = answer_plan(marks)
    for key, value in expected.items():
        assert plan[key] == value, key
    assert plan["intro"] + plan["body"] + plan["conclusion"] == plan["words"]


def test_an_answer_prompt_carries_the_depth_contract():
    """The regression this guards: answers that read as outlines. Every answer
    prompt must demand a full, developed, substantiated answer."""
    prompt = build_answer_prompt("moderate", "Assess the reforms.", 15, NOTES)
    assert "not an outline" in prompt
    assert "Substantiate" in prompt
    assert "Be comprehensive" in prompt
    assert "never from inventing beyond it" in prompt
    # The sheet's exam-length instruction is substituted out, so the prompt
    # never states two lengths and lets the model pick the shorter one.
    assert "Respect the word limit implied by the marks." not in prompt
    assert "Model Answer Framework" not in prompt
    # The depth contract must come from developing the notes, not replacing
    # the grounding rule the sheet's cells are built on.
    assert "ONLY the provided daily class notes" in prompt


@pytest.mark.parametrize("level", DMP_LEVELS)
@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_requested_answer_structure_reaches_every_level_and_language(level, language):
    prompt = build_answer_prompt(level, "Assess the reforms.", 15, NOTES,
                                 language_code=language)
    # Read with the line wrapping flattened: the rules are sentences, and
    # where the source happens to break a line is not part of them.
    flat = " ".join(prompt.split())
    for rule in ("Introduction: one short paragraph of about 40 words",
                 "Conclusion: one short paragraph of about 40",
                 "6 to 7 distinct, concise points", "India-relevant",
                 "## INTRODUCTION", "## BODY", "## CONCLUSION",
                 "Write NO other heading or sub-heading",
                 'never start a point with a label such as "Point:"',
                 "The body follows the approach",
                 "For example,", "For instance,",
                 "positive", "action-oriented", "rather than by exceeding the limit",
                 "instead of fabricating", "Count only # ANSWER"):
        assert rule in flat, rule
    # The three things the content team rejected are no longer asked for.
    for gone in ("-----**Point:**", "exactly 60 words", "exactly 5 distinct",
                 "Give each demand its own heading", "exceeding the overall target",
                 "a separate ## heading for each demand"):
        assert gone not in flat, gone
    assert language_directive(language) in prompt
    assert NOTES in prompt
    assert "Open with a one-sentence" not in prompt


def test_marathi_is_locked_as_marathi_and_not_as_devanagari():
    """A model told "write in Devanagari" writes Hindi. Both Devanagari
    languages therefore name the other one and refuse it."""
    marathi = language_directive("mr")
    assert "Marathi (Devanagari)" in marathi
    assert "must NOT read as Hindi" in marathi
    hindi = language_directive("hi")
    assert "Hindi (Devanagari)" in hindi
    assert "must NOT read as Marathi" in hindi


def test_english_output_is_told_not_to_force_translate_the_terms():
    directive = language_directive("en")
    assert "English" in directive
    assert "do not force-translate" in directive


def test_the_json_contract_names_the_language_where_the_model_is_looking():
    """The format block is the last thing before the notes, and a language
    directive twenty lines earlier loses to the nearer instruction."""
    assert "Marathi" in question_format_block("mr", count=4)
    assert "Hindi" in question_format_block("hi", count=4)


# ------------------------------------------------------------ directive mode

def test_a_directive_prompt_fills_its_topic():
    prompt = build_directive_prompt("DISCUSS", "Tribal Communities", language_code="en")
    assert "[Topic Name]" not in prompt
    assert "Tribal Communities" in prompt
    assert "Return ONLY valid JSON" in prompt


def test_a_two_topic_directive_is_filled_on_both_sides():
    prompt = build_directive_prompt("COMPARE", "NITI Aayog",
                                    topic_b="Planning Commission")
    assert "[Topic A]" not in prompt and "[Topic B]" not in prompt
    assert "NITI Aayog" in prompt and "Planning Commission" in prompt


def test_an_unknown_directive_is_refused_rather_than_substituted():
    """A silently swapped directive returns five perfectly plausible questions
    of the wrong kind, and nothing about the output says so."""
    with pytest.raises(KeyError):
        build_directive_prompt("PONDER", "Federalism")


# -------------------------------------------------------- the mislabelled cell

def test_the_dmp_model_answer_cell_is_kept_but_is_not_an_answer_writer():
    """Cell B6 is headed "DMP MODEL ANSWER" and contains "You are an expert UPSC
    notes maker". It reflows notes. It answers nothing."""
    assert DMP_CONTENT_REFINE_PROMPT.startswith("You are an expert UPSC notes maker")
    assert "question" not in DMP_CONTENT_REFINE_PROMPT.lower()
    # And it is not what any level uses to answer.
    for template in DMP_LEVEL_ANSWER_PROMPTS.values():
        assert DMP_CONTENT_REFINE_PROMPT not in template


def test_the_answer_path_never_reaches_for_the_refinement_prompt():
    source = (Path(__file__).resolve().parents[1] / "src" / "dmp" / "service.py").read_text(
        encoding="utf-8")
    assert "DMP_CONTENT_REFINE_PROMPT" not in source, (
        "the notes-refinement prompt was wired into the answer path")
