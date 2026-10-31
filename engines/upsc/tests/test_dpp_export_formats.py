"""The third DPP download: questions with their answers and solutions inline.

The question paper and the answer key existed already and have their own
tests. This file pins the combined document — the take-home study copy — to
what those two promise separately: every question, every option, the answer
letter, and the full solution, in selection order, in one PDF.
"""
from __future__ import annotations

import importlib

import pytest

from src.notes_to_dpp import models


def _question(
    question_id: str,
    prompt: str,
    options: tuple[str, ...],
    solution: str,
) -> models.QuestionRecord:
    localized = models.LocalizedQuestion(
        language=models.Language.ENGLISH,
        prompt=prompt,
        options=options,
        correct_option_indexes=(1,),
        solution=solution,
    )
    return models.QuestionRecord(
        question_id=question_id,
        prompt=prompt,
        options=options,
        difficulty=models.Difficulty.EASY,
        tags=(models.QuestionTag("S-1", "C-1", "T-1"),),
        source_tags=("DPP",),
        content_fingerprint=f"fp-{question_id}",
        verified=True,
        localized_content=(localized,),
    )


def _slots(questions) -> tuple[models.SelectionSlot, ...]:
    return tuple(
        models.SelectionSlot(
            slot_id=f"slot-{index}",
            difficulty=question.difficulty,
            question_id=question.question_id,
            preferred_row_id="ROW-1",
        )
        for index, question in enumerate(questions, start=1)
    )


def _pdf_text(payload: bytes) -> str:
    """Extracted text with whitespace collapsed, so a sentence the two-column
    layout wraps mid-phrase still matches as a substring."""
    pymupdf = pytest.importorskip("pymupdf")
    assert payload.startswith(b"%PDF")
    document = pymupdf.open(stream=payload, filetype="pdf")
    try:
        assert document.page_count >= 1
        raw = "\n".join(page.get_text() for page in document)
    finally:
        document.close()
    return " ".join(raw.split())


def _export(questions):
    pdf_export = importlib.import_module("src.notes_to_dpp.pdf_export")
    return pdf_export.export_practice_pdfs(
        _slots(questions), questions, models.Language.ENGLISH, dpp_no="3")


def test_the_combined_pdf_carries_question_answer_and_solution_together() -> None:
    questions = (
        _question(
            "Q-1",
            "Which Article abolishes untouchability?",
            ("Article 15", "Article 17", "Article 19", "Article 21"),
            "Article 17 abolishes untouchability in all forms.",
        ),
        _question(
            "Q-2",
            "Which river is called the Sorrow of Bengal?",
            ("Kosi", "Damodar", "Teesta", "Hooghly"),
            "The Damodar's floods earned it the name Sorrow of Bengal.",
        ),
    )
    exported = _export(questions)
    combined = _pdf_text(exported.combined_pdf)

    # It is its own document, not a rename of either existing export.
    assert exported.combined_pdf != exported.question_pdf
    assert exported.combined_pdf != exported.solutions_pdf
    assert "DPP: 3" in combined
    # Named under the header rule, as the solutions document names its parts.
    assert "Questions with Solutions" in combined

    # Everything the question paper shows...
    assert "Which Article abolishes untouchability?" in combined
    for option in ("Article 15", "Article 17", "Article 19", "Article 21"):
        assert option in combined
    # ...and everything the solutions document shows, in the same place.
    assert "Article 17 abolishes untouchability in all forms." in combined
    assert "The Damodar's floods earned it the name Sorrow of Bengal." in combined

    # Selection order is preserved: Q1's material before Q2's.
    assert combined.index("untouchability?") < combined.index("Sorrow of Bengal?")


def test_the_combined_pdf_states_the_answer_the_key_states() -> None:
    questions = (
        _question(
            "Q-1",
            "Pick the second option.",
            ("First", "Second", "Third", "Fourth"),
            "The second option is correct.",
        ),
    )
    exported = _export(questions)
    combined = _pdf_text(exported.combined_pdf)
    key = _pdf_text(exported.solutions_pdf)

    # The answer line in the combined copy agrees with the answer key.
    assert "Ans: B" in combined, combined
    assert "Q1. B" in key, key


def test_a_missing_solution_is_said_not_papered_over() -> None:
    questions = (
        _question(
            "Q-1",
            "A question whose bank row has no solution.",
            ("Yes", "No"),
            "",
        ),
    )
    exported = _export(questions)
    combined = _pdf_text(exported.combined_pdf)
    assert "Detailed solution unavailable for this question." in combined
