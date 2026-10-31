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
        language=models.Language.HINDI,
        prompt=prompt,
        options=options,
        correct_option_indexes=(0,),
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


def _pdf_text(payload: bytes) -> str:
    pymupdf = pytest.importorskip("pymupdf")
    assert payload.startswith(b"%PDF")
    document = pymupdf.open(stream=payload, filetype="pdf")
    try:
        assert document.page_count >= 1
        return "\n".join(page.get_text() for page in document)
    finally:
        document.close()


def test_question_and_solution_pdfs_are_separate_ordered_and_hindi_safe() -> None:
    pdf_export = importlib.import_module("src.notes_to_dpp.pdf_export")
    questions = (
        _question(
            "Q-1",
            "भारत की सबसे लंबी नदी कौन-सी है?",
            ("गंगा", "गोदावरी"),
            "समाधान-एक: सही उत्तर गंगा है।",
        ),
        _question(
            "Q-2",
            "हिमालय किस प्रकार का पर्वत है?",
            ("नवीन वलित पर्वत", "ब्लॉक पर्वत"),
            "समाधान-दो: हिमालय नवीन वलित पर्वत है।",
        ),
    )
    slots = tuple(
        models.SelectionSlot(
            slot_id=f"slot-{index}",
            difficulty=question.difficulty,
            question_id=question.question_id,
            preferred_row_id="ROW-1",
        )
        for index, question in enumerate(questions, start=1)
    )

    exported = pdf_export.export_practice_pdfs(
        slots,
        questions,
        models.Language.HINDI,
        dpp_no="7",
    )
    question_text = _pdf_text(exported.questions_pdf)
    solution_text = _pdf_text(exported.solutions_pdf)

    assert exported.questions_pdf != exported.solutions_pdf
    # The badge drops the app's zero padding, as the house sample does.
    assert "DPP: 7" in question_text
    assert "DPP: 7" in solution_text
    assert "भारत की सबसे लंबी नदी कौन-सी है?" in question_text
    assert "हिमालय किस प्रकार का पर्वत है?" in question_text
    assert "गंगा" in question_text
    assert question_text.index("भारत की") < question_text.index("हिमालय")
    assert "समाधान-एक" not in question_text
    assert "समाधान-दो" not in question_text

    assert "समाधान-एक: सही उत्तर गंगा है।" in solution_text
    assert "समाधान-दो: हिमालय नवीन वलित पर्वत है।" in solution_text
    assert solution_text.index("समाधान-एक") < solution_text.index("समाधान-दो")

    # The combined take-home copy must survive Devanagari extraction too — a
    # font or style regression confined to its builder would otherwise render
    # tofu in Hindi while the two documents above stay perfect.
    combined_text = _pdf_text(exported.combined_pdf)
    assert "भारत की सबसे लंबी नदी कौन-सी है?" in combined_text
    assert "गंगा" in combined_text
    assert "समाधान-एक" in combined_text
    assert combined_text.index("भारत की") < combined_text.index("हिमालय")


def test_pdf_export_rejects_an_empty_selection() -> None:
    pdf_export = importlib.import_module("src.notes_to_dpp.pdf_export")

    with pytest.raises(ValueError, match="selected|selection|question"):
        pdf_export.export_practice_pdfs((), (), models.Language.HINDI)
