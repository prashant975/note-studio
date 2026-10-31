"""Pure-Hindi vocabulary, the DPP preview line breaks, hand-picked question
sets, and the subject printed on the practice PDFs."""
from __future__ import annotations

import pytest

from src.dmp.prompts import language_directive
from src.languages import vocabulary_rule
from src.notes_to_dpp import models
from src.notes_to_dpp.pdf_export import export_practice_pdfs, prompt_preview_markdown
from src.notes_to_dpp.selection import (
    available_counts,
    eligible_questions,
    manual_selection,
)
from src.prompt_builder import _language_rule

ROW_A = models.TaxonomyRow("r-a", "Geography", "S-1", "Climate", "C-1", "Monsoon", "T-1")
ROW_B = models.TaxonomyRow("r-b", "Geography", "S-1", "Climate", "C-1", "Cyclones", "T-2")


def _question(qid: str, difficulty: models.Difficulty, topic_id: str = "T-1",
              fingerprint: str | None = None) -> models.QuestionRecord:
    localized = models.LocalizedQuestion(
        language=models.Language.ENGLISH,
        prompt=f"Consider the following statements about {qid}:\n1. One.\n2. Two.\n"
               "Which of the statements given above is/are correct?",
        options=("1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"),
        correct_option_indexes=(0,),
        solution="Because.",
    )
    return models.QuestionRecord(
        question_id=qid, prompt=localized.prompt, options=localized.options,
        difficulty=difficulty, tags=(models.QuestionTag("S-1", "C-1", topic_id),),
        source_tags=("DPP",), content_fingerprint=fingerprint or f"fp-{qid}",
        verified=True, localized_content=(localized,),
    )


BANK = (
    _question("q-hard", models.Difficulty.HARD),
    _question("q-easy", models.Difficulty.EASY, "T-2"),
    _question("q-med", models.Difficulty.MEDIUM),
    _question("q-dupe", models.Difficulty.EASY, fingerprint="fp-q-med"),
    _question("q-elsewhere", models.Difficulty.EASY, "T-9"),
)


# ------------------------------------------------------------- Hindi prompts

def test_hindi_notes_ask_for_hindi_words_with_english_in_brackets():
    rule = _language_rule("hi")
    assert "standard form" not in rule, "the old rule GPT read as 'keep English'"
    assert "राजकोषीय घाटा (Fiscal Deficit)" in rule
    assert "FIRST time" in rule
    assert "Do NOT output English notes" in rule


def test_hindi_practice_questions_share_the_same_vocabulary_rule():
    directive = language_directive("hi")
    assert "Keep established English technical terms" not in directive
    assert vocabulary_rule("hi") in directive


def test_english_output_has_no_vocabulary_rule():
    assert vocabulary_rule("en") == ""
    assert "do not force-translate" in language_directive("en")


# ------------------------------------------------------------ preview layout

def test_the_question_stem_is_not_folded_into_the_last_statement():
    preview = prompt_preview_markdown("1. One.\n2. Two.\nWhich of these is correct?")
    assert preview == "1\\. One.  \n2\\. Two.  \nWhich of these is correct?"


# ------------------------------------------------------------- manual picks

def test_eligible_questions_are_unique_in_language_and_easy_first():
    pool = eligible_questions(BANK, (ROW_A, ROW_B), language=models.Language.ENGLISH,
                              seen_ids={"q-hard"})
    ids = [q.question_id for q, _ in pool]
    assert ids == ["q-easy", "q-med"], "seen, duplicate and off-topic rows leak in"
    assert dict((q.question_id, row.row_id) for q, row in pool)["q-easy"] == "r-b"


def test_available_counts_agree_with_the_pickable_pool():
    counts = available_counts(BANK, (ROW_A, ROW_B), language=models.Language.ENGLISH)
    assert counts == {models.Difficulty.EASY: 1, models.Difficulty.MEDIUM: 1,
                      models.Difficulty.HARD: 1}


def test_a_manual_set_keeps_the_ticked_order_and_marks_them_seen():
    outcome = manual_selection(BANK, (ROW_A, ROW_B), ["q-hard", "q-easy"],
                               language=models.Language.ENGLISH)
    assert [s.question_id for s in outcome.slots] == ["q-hard", "q-easy"]
    assert [s.difficulty for s in outcome.slots] == [models.Difficulty.HARD,
                                                     models.Difficulty.EASY]
    assert {"q-hard", "q-easy"} <= outcome.seen_ids
    assert "fp-q-hard" in outcome.seen_fingerprints


@pytest.mark.parametrize("ids", [[], ["q-elsewhere"], ["q-dupe"]])
def test_a_manual_set_refuses_nothing_or_ineligible_questions(ids):
    with pytest.raises(ValueError):
        manual_selection(BANK, (ROW_A, ROW_B), ids, language=models.Language.ENGLISH)


# ------------------------------------------------------------- PDF subject

def _pdf_text(payload: bytes) -> str:
    pymupdf = pytest.importorskip("pymupdf")
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


def test_every_pdf_prints_the_subject_beside_the_dpp_number():
    outcome = manual_selection(BANK, (ROW_A, ROW_B), ["q-easy"],
                               language=models.Language.ENGLISH)
    export = export_practice_pdfs(outcome.slots, BANK, models.Language.ENGLISH,
                                  dpp_no="31", subject="Environment & Ecology")
    for payload in (export.question_pdf, export.solutions_pdf, export.combined_pdf):
        text = _pdf_text(payload)
        assert "Environment & Ecology" in text
        assert "DPP: 31" in text
        # The subject IS the title now; the generic one must not sit above it.
        assert "Practice Questions" not in text


def test_a_pdf_without_a_subject_still_builds():
    outcome = manual_selection(BANK, (ROW_A,), ["q-med"],
                               language=models.Language.ENGLISH)
    export = export_practice_pdfs(outcome.slots, BANK, models.Language.ENGLISH)
    text = _pdf_text(export.question_pdf)
    assert "Environment" not in text
    assert "Practice Questions" in text, "a blank subject needs a generic title"
