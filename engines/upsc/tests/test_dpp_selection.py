from __future__ import annotations

from collections import Counter

import pytest

from src.notes_to_dpp.models import Difficulty, QuestionTag
from src.notes_to_dpp.selection import (
    InsufficientPoolError,
    available_counts,
    generate_selection,
    question_matches_row,
    replace_question,
)


def test_question_join_uses_full_hierarchy_when_leaf_ids_are_reused(
    taxonomy_row_factory,
    question_factory,
) -> None:
    selected_row = taxonomy_row_factory(
        row_id="ROW-INDIA",
        subject_id="S-INDIA",
        chapter_id="C-INDIA-RIVERS",
        topic_id="T-SHARED",
        subtopic_id="ST-SHARED",
    )
    correct = question_factory(
        "Q-CORRECT",
        Difficulty.EASY,
        QuestionTag("S-INDIA", "C-INDIA-RIVERS", "T-SHARED", "ST-SHARED"),
    )
    same_leaf_wrong_parent = question_factory(
        "Q-WRONG-PARENT",
        Difficulty.EASY,
        QuestionTag("S-WORLD", "C-WORLD-RIVERS", "T-SHARED", "ST-SHARED"),
    )
    same_topic_wrong_subtopic = question_factory(
        "Q-WRONG-SUBTOPIC",
        Difficulty.EASY,
        QuestionTag("S-INDIA", "C-INDIA-RIVERS", "T-SHARED", "ST-OTHER"),
    )

    assert question_matches_row(correct, selected_row)
    assert not question_matches_row(same_leaf_wrong_parent, selected_row)
    assert not question_matches_row(same_topic_wrong_subtopic, selected_row)
    assert available_counts(
        (correct, same_leaf_wrong_parent, same_topic_wrong_subtopic),
        (selected_row,),
    )[Difficulty.EASY] == 1


def test_generate_selection_returns_exact_requested_difficulty_counts(
    taxonomy_row_factory,
    question_factory,
) -> None:
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    questions = tuple(
        question_factory(question_id, difficulty, tag)
        for difficulty, ids in (
            (Difficulty.EASY, ("E-1", "E-2", "E-3")),
            (Difficulty.MEDIUM, ("M-1", "M-2")),
            (Difficulty.HARD, ("H-1", "H-2")),
        )
        for question_id in ids
    )
    requested = {
        Difficulty.EASY: 2,
        Difficulty.MEDIUM: 1,
        Difficulty.HARD: 2,
    }

    outcome = generate_selection(questions, (row,), requested, seed=41)
    question_by_id = {question.question_id: question for question in questions}

    assert Counter(slot.difficulty for slot in outcome.slots) == Counter(requested)
    assert len(outcome.slots) == sum(requested.values())
    assert len({slot.question_id for slot in outcome.slots}) == len(outcome.slots)
    assert all(
        question_by_id[slot.question_id].difficulty == slot.difficulty
        for slot in outcome.slots
    )
    assert outcome.seen_ids == frozenset(slot.question_id for slot in outcome.slots)


def test_replacement_keeps_difficulty_and_never_repeats_a_shown_question(
    taxonomy_row_factory,
    question_factory,
) -> None:
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    questions = tuple(
        [
            question_factory(question_id, Difficulty.EASY, tag)
            for question_id in ("E-1", "E-2", "E-3")
        ]
        + [
            question_factory(question_id, Difficulty.HARD, tag)
            for question_id in ("H-1", "H-2")
        ]
    )
    initial = generate_selection(
        questions,
        (row,),
        {Difficulty.EASY: 1, Difficulty.MEDIUM: 0, Difficulty.HARD: 1},
        seed=7,
    )
    easy_slot = next(slot for slot in initial.slots if slot.difficulty == Difficulty.EASY)
    hard_slot = next(slot for slot in initial.slots if slot.difficulty == Difficulty.HARD)

    first = replace_question(
        initial.slots,
        easy_slot.slot_id,
        questions,
        (row,),
        seen_ids=initial.seen_ids,
        seen_fingerprints=initial.seen_fingerprints,
        seed=11,
    )
    first_easy = next(slot for slot in first.slots if slot.slot_id == easy_slot.slot_id)
    unchanged_hard = next(slot for slot in first.slots if slot.slot_id == hard_slot.slot_id)

    assert first.replaced
    assert first_easy.difficulty == Difficulty.EASY
    assert first_easy.question_id != easy_slot.question_id
    assert unchanged_hard == hard_slot
    assert first_easy.question_id in first.seen_ids

    second = replace_question(
        first.slots,
        easy_slot.slot_id,
        questions,
        (row,),
        seen_ids=first.seen_ids,
        seen_fingerprints=first.seen_fingerprints,
        seed=13,
    )
    second_easy = next(slot for slot in second.slots if slot.slot_id == easy_slot.slot_id)

    assert second.replaced
    assert second_easy.difficulty == Difficulty.EASY
    assert len(
        {easy_slot.question_id, first_easy.question_id, second_easy.question_id}
    ) == 3


def test_exhausted_replacement_is_a_transactional_no_op(
    taxonomy_row_factory,
    question_factory,
) -> None:
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    questions = (question_factory("ONLY-EASY", Difficulty.EASY, tag),)
    initial = generate_selection(
        questions,
        (row,),
        {Difficulty.EASY: 1},
        seed=1,
    )

    replacement = replace_question(
        initial.slots,
        initial.slots[0].slot_id,
        questions,
        (row,),
        seen_ids=initial.seen_ids,
        seen_fingerprints=initial.seen_fingerprints,
        seed=2,
    )

    assert not replacement.replaced
    assert replacement.slots == initial.slots
    assert replacement.seen_ids == initial.seen_ids
    assert replacement.seen_fingerprints == initial.seen_fingerprints
    assert replacement.message == "No unused Easy replacement remains."


def test_duplicate_content_fingerprints_count_and_select_only_once(
    taxonomy_row_factory,
    question_factory,
) -> None:
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    duplicate_one = question_factory(
        "E-DUP-1",
        Difficulty.EASY,
        tag,
        fingerprint="same-content",
    )
    duplicate_two = question_factory(
        "E-DUP-2",
        Difficulty.EASY,
        tag,
        fingerprint="same-content",
    )
    questions = (duplicate_one, duplicate_two)

    counts = available_counts(questions, (row,))
    assert counts[Difficulty.EASY] == 1

    with pytest.raises(InsufficientPoolError) as exc_info:
        generate_selection(questions, (row,), {Difficulty.EASY: 2})
    assert exc_info.value.shortages == {Difficulty.EASY: (2, 1)}

    selected = generate_selection(questions, (row,), {Difficulty.EASY: 1}, seed=19)
    assert available_counts(
        questions,
        (row,),
        seen_ids=selected.seen_ids,
        seen_fingerprints=selected.seen_fingerprints,
    )[Difficulty.EASY] == 0

    replacement = replace_question(
        selected.slots,
        selected.slots[0].slot_id,
        questions,
        (row,),
        seen_ids=selected.seen_ids,
        seen_fingerprints=selected.seen_fingerprints,
    )
    assert not replacement.replaced
    assert replacement.slots == selected.slots


def test_replacement_cannot_repeat_current_content_if_seen_fingerprints_are_missing(
    taxonomy_row_factory, question_factory,
):
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    original = question_factory("ORIGINAL", Difficulty.EASY, tag, fingerprint="same")
    duplicate = question_factory("DUPLICATE", Difficulty.EASY, tag, fingerprint="same")
    selected = generate_selection((original,), (row,), {Difficulty.EASY: 1})
    outcome = replace_question(
        selected.slots, selected.slots[0].slot_id, (original, duplicate), (row,),
        seen_ids=(), seen_fingerprints=(),
    )
    assert not outcome.replaced
    assert outcome.slots == selected.slots
