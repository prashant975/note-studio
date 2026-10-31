from __future__ import annotations

from src.notes_to_dpp import models, selection


def _localized(language, marker: str):  # noqa: ANN001, ANN202
    return models.LocalizedQuestion(
        language=language,
        prompt=f"{marker} prompt",
        options=(f"{marker} option A", f"{marker} option B"),
        correct_option_indexes=(0,),
        solution=f"{marker} solution",
    )


def _question(
    question_id: str,
    row: models.TaxonomyRow,
    languages: tuple,
    *,
    fingerprint: str | None = None,
) -> models.QuestionRecord:
    localized = tuple(
        _localized(language, f"{question_id}-{language.value}")
        for language in languages
    )
    primary = localized[0]
    return models.QuestionRecord(
        question_id=question_id,
        prompt=primary.prompt,
        options=primary.options,
        difficulty=models.Difficulty.EASY,
        tags=(
            models.QuestionTag(
                row.subject_id,
                row.chapter_id,
                row.topic_id,
                row.subtopic_id,
            ),
        ),
        source_tags=("DPP",),
        content_fingerprint=fingerprint or f"fp-{question_id}",
        verified=True,
        localized_content=localized,
    )


def _row() -> models.TaxonomyRow:
    return models.TaxonomyRow(
        row_id="ROW-1",
        subject="Geography",
        subject_id="S-1",
        chapter="Rivers",
        chapter_id="C-1",
        topic="River Systems",
        topic_id="T-1",
    )


def test_language_filter_applies_to_counts_and_generation_with_legacy_default() -> None:
    row = _row()
    english = models.Language.ENGLISH
    hindi = models.Language.HINDI
    questions = (
        _question("Q-EN", row, (english,)),
        _question("Q-HI", row, (hindi,)),
        _question("Q-BI", row, (english, hindi)),
        _question("Q-HI-2", row, (hindi,)),
    )

    all_counts = selection.available_counts(questions, (row,))
    english_counts = selection.available_counts(
        questions,
        (row,),
        language=english,
    )
    hindi_counts = selection.available_counts(
        questions,
        (row,),
        language=hindi,
    )

    assert all_counts[models.Difficulty.EASY] == 4
    assert english_counts[models.Difficulty.EASY] == 2
    assert hindi_counts[models.Difficulty.EASY] == 3

    generated = selection.generate_selection(
        questions,
        (row,),
        {models.Difficulty.EASY: 3},
        language=hindi,
        seed=9,
    )

    assert {slot.question_id for slot in generated.slots} == {
        "Q-HI",
        "Q-BI",
        "Q-HI-2",
    }


def test_replacement_cannot_cross_the_requested_language() -> None:
    row = _row()
    english = models.Language.ENGLISH
    hindi = models.Language.HINDI
    questions = (
        _question("Q-EN", row, (english,)),
        _question("Q-HI-1", row, (hindi,)),
        _question("Q-HI-2", row, (hindi,)),
        _question("Q-BI", row, (english, hindi)),
    )
    question_by_id = {question.question_id: question for question in questions}
    initial = selection.generate_selection(
        questions,
        (row,),
        {models.Difficulty.EASY: 1},
        language=hindi,
        seed=3,
    )

    replacement = selection.replace_question(
        initial.slots,
        initial.slots[0].slot_id,
        questions,
        (row,),
        seen_ids=initial.seen_ids,
        seen_fingerprints=initial.seen_fingerprints,
        language=hindi,
        seed=4,
    )
    replaced_question = question_by_id[replacement.slots[0].question_id]

    assert replacement.replaced
    assert replacement.slots[0].difficulty is models.Difficulty.EASY
    assert any(
        item.language is hindi for item in replaced_question.localized_content
    )
    assert replacement.slots[0].question_id != "Q-EN"


def test_remove_slot_preserves_history_and_blocks_id_and_content_reuse() -> None:
    row = _row()
    hindi = models.Language.HINDI
    removed_question = _question(
        "Q-REMOVED",
        row,
        (hindi,),
        fingerprint="duplicate-content",
    )
    duplicate_content = _question(
        "Q-DUPLICATE",
        row,
        (hindi,),
        fingerprint="duplicate-content",
    )
    unused = _question("Q-UNUSED", row, (hindi,))
    slot = models.SelectionSlot(
        slot_id="slot-to-remove",
        difficulty=models.Difficulty.EASY,
        question_id=removed_question.question_id,
        preferred_row_id=row.row_id,
    )
    seen_ids = frozenset({removed_question.question_id})
    seen_fingerprints = frozenset({removed_question.content_fingerprint})

    removed = selection.remove_question(
        (slot,),
        slot.slot_id,
        seen_ids=seen_ids,
        seen_fingerprints=seen_fingerprints,
    )

    assert isinstance(removed, selection.RemovalOutcome)
    assert removed.removed
    assert removed.slots == ()
    assert removed.seen_ids == seen_ids
    assert removed.seen_fingerprints == seen_fingerprints

    regenerated = selection.generate_selection(
        (removed_question, duplicate_content, unused),
        (row,),
        {models.Difficulty.EASY: 1},
        seen_ids=removed.seen_ids,
        seen_fingerprints=removed.seen_fingerprints,
        language=hindi,
        seed=5,
    )
    assert tuple(slot.question_id for slot in regenerated.slots) == ("Q-UNUSED",)


def test_remove_unknown_slot_is_a_transactional_no_op() -> None:
    slot = models.SelectionSlot(
        slot_id="existing-slot",
        difficulty=models.Difficulty.MEDIUM,
        question_id="Q-1",
        preferred_row_id="ROW-1",
    )
    seen_ids = frozenset({"Q-1", "Q-OLD"})
    seen_fingerprints = frozenset({"fp-1", "fp-old"})

    outcome = selection.remove_question(
        (slot,),
        "missing-slot",
        seen_ids=seen_ids,
        seen_fingerprints=seen_fingerprints,
    )

    assert not outcome.removed
    assert outcome.slots == (slot,)
    assert outcome.seen_ids == seen_ids
    assert outcome.seen_fingerprints == seen_fingerprints
