from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, replace
from typing import Iterable

from .models import (
    Difficulty,
    Language,
    QuestionRecord,
    QuestionTag,
    SelectionSlot,
    TaxonomyRow,
)


class InsufficientPoolError(ValueError):
    def __init__(self, shortages: dict[Difficulty, tuple[int, int]]) -> None:
        self.shortages = shortages
        detail = ", ".join(
            f"{difficulty.value}: requested {requested}, available {available}"
            for difficulty, (requested, available) in shortages.items()
        )
        super().__init__(f"Not enough unused eligible questions ({detail}).")


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    slots: tuple[SelectionSlot, ...]
    seen_ids: frozenset[str]
    seen_fingerprints: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReplacementOutcome:
    slots: tuple[SelectionSlot, ...]
    seen_ids: frozenset[str]
    seen_fingerprints: frozenset[str]
    replaced: bool
    message: str


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    slots: tuple[SelectionSlot, ...]
    seen_ids: frozenset[str]
    seen_fingerprints: frozenset[str]
    removed: bool
    message: str


def _supports_language(question: QuestionRecord, language: Language | None) -> bool:
    if language is None:
        return True
    return question.supports_language(language)


def _tag_matches_row(tag: QuestionTag, row: TaxonomyRow) -> bool:
    if (
        tag.subject_id != row.subject_id
        or tag.chapter_id != row.chapter_id
        or tag.topic_id != row.topic_id
    ):
        return False
    return row.subtopic_id is None or tag.subtopic_id == row.subtopic_id


def question_matches_row(question: QuestionRecord, row: TaxonomyRow) -> bool:
    return any(_tag_matches_row(tag, row) for tag in question.tags)


def _row_pools(
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    *,
    language: Language | None = None,
) -> dict[str, list[QuestionRecord]]:
    question_list = tuple(questions)
    output: dict[str, list[QuestionRecord]] = {}
    for row in rows:
        output[row.row_id] = [
            question
            for question in question_list
            if _supports_language(question, language) and question_matches_row(question, row)
        ]
    return output


def eligible_questions(
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    *,
    seen_ids: Iterable[str] = (),
    seen_fingerprints: Iterable[str] = (),
    language: Language | None = None,
) -> tuple[tuple[QuestionRecord, TaxonomyRow], ...]:
    """Every unused question these rows can offer, once each, with its row.

    One entry per content fingerprint, so a question stored twice in the bank
    is offered once. Ordered easy → hard, then by row, which is how a person
    picking by hand wants to read the list.
    """
    row_list = tuple(rows)
    seen_id_set = set(seen_ids)
    seen_fingerprint_set = set(seen_fingerprints)
    picked_ids: set[str] = set()
    picked_fingerprints: set[str] = set()
    output: list[tuple[QuestionRecord, TaxonomyRow, int]] = []
    for question in questions:
        if (
            question.question_id in seen_id_set
            or question.question_id in picked_ids
            or question.content_fingerprint in seen_fingerprint_set
            or question.content_fingerprint in picked_fingerprints
        ):
            continue
        position, row = next(
            ((i, row) for i, row in enumerate(row_list)
             if question_matches_row(question, row)),
            (-1, None),
        )
        if row is None or not _supports_language(question, language):
            continue
        picked_ids.add(question.question_id)
        picked_fingerprints.add(question.content_fingerprint)
        output.append((question, row, position))
    order = {difficulty: i for i, difficulty in enumerate(Difficulty)}
    output.sort(key=lambda item: (order[item[0].difficulty], item[2]))
    return tuple((question, row) for question, row, _ in output)


def available_counts(
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    *,
    seen_ids: Iterable[str] = (),
    seen_fingerprints: Iterable[str] = (),
    language: Language | None = None,
) -> dict[Difficulty, int]:
    counts = {difficulty: 0 for difficulty in Difficulty}
    for question, _ in eligible_questions(
        questions, rows, seen_ids=seen_ids,
        seen_fingerprints=seen_fingerprints, language=language,
    ):
        counts[question.difficulty] += 1
    return counts


def manual_selection(
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    question_ids: Iterable[str],
    *,
    seen_ids: Iterable[str] = (),
    seen_fingerprints: Iterable[str] = (),
    language: Language | None = None,
) -> SelectionOutcome:
    """A set made of exactly the questions a person ticked, in their order.

    Held to the same rules as a random set: an id that is not an unused,
    in-language question of these rows is refused rather than printed.
    """
    eligible = {
        question.question_id: (question, row)
        for question, row in eligible_questions(
            questions, rows, seen_ids=seen_ids,
            seen_fingerprints=seen_fingerprints, language=language,
        )
    }
    wanted = list(dict.fromkeys(str(qid) for qid in question_ids))
    if not wanted:
        raise ValueError("Tick at least one question.")
    missing = [qid for qid in wanted if qid not in eligible]
    if missing:
        raise ValueError(
            "These questions are no longer available for the chosen topics: "
            + ", ".join(missing))
    slots = tuple(
        SelectionSlot(
            slot_id=uuid.uuid4().hex,
            difficulty=eligible[qid][0].difficulty,
            question_id=qid,
            preferred_row_id=eligible[qid][1].row_id,
        )
        for qid in wanted
    )
    return SelectionOutcome(
        slots=slots,
        seen_ids=frozenset(seen_ids) | {slot.question_id for slot in slots},
        seen_fingerprints=frozenset(seen_fingerprints)
        | {eligible[qid][0].content_fingerprint for qid in wanted},
    )


def generate_selection(
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    requested: dict[Difficulty, int],
    *,
    seen_ids: Iterable[str] = (),
    seen_fingerprints: Iterable[str] = (),
    language: Language | None = None,
    seed: int = 0,
) -> SelectionOutcome:
    question_list = tuple(questions)
    row_list = tuple(rows)
    current_seen_ids = set(seen_ids)
    current_seen_fingerprints = set(seen_fingerprints)
    availability = available_counts(
        question_list,
        row_list,
        seen_ids=current_seen_ids,
        seen_fingerprints=current_seen_fingerprints,
        language=language,
    )
    shortages = {
        difficulty: (max(0, int(requested.get(difficulty, 0))), availability[difficulty])
        for difficulty in Difficulty
        if max(0, int(requested.get(difficulty, 0))) > availability[difficulty]
    }
    if shortages:
        raise InsufficientPoolError(shortages)

    rng = random.Random(seed)
    pools = _row_pools(question_list, row_list, language=language)
    for pool in pools.values():
        rng.shuffle(pool)

    selected_ids: set[str] = set()
    selected_fingerprints: set[str] = set()
    slots: list[SelectionSlot] = []
    difficulty_order = (Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD)

    for difficulty in difficulty_order:
        target = max(0, int(requested.get(difficulty, 0)))
        chosen = 0
        while chosen < target:
            made_progress = False
            for row in row_list:
                pool = pools[row.row_id]
                candidate = next(
                    (
                        question
                        for question in pool
                        if question.difficulty == difficulty
                        and question.question_id not in current_seen_ids
                        and question.question_id not in selected_ids
                        and question.content_fingerprint not in current_seen_fingerprints
                        and question.content_fingerprint not in selected_fingerprints
                    ),
                    None,
                )
                if candidate is None:
                    continue
                pool.remove(candidate)
                slot_uuid = uuid.UUID(int=rng.getrandbits(128)).hex
                slots.append(
                    SelectionSlot(
                        slot_id=slot_uuid,
                        difficulty=difficulty,
                        question_id=candidate.question_id,
                        preferred_row_id=row.row_id,
                    )
                )
                selected_ids.add(candidate.question_id)
                selected_fingerprints.add(candidate.content_fingerprint)
                chosen += 1
                made_progress = True
                if chosen >= target:
                    break
            if not made_progress:
                # The availability pre-check makes this defensive branch unlikely,
                # but keeping it transactional prevents silent partial output.
                raise InsufficientPoolError({difficulty: (target, chosen)})

    current_seen_ids.update(selected_ids)
    current_seen_fingerprints.update(selected_fingerprints)
    return SelectionOutcome(
        slots=tuple(slots),
        seen_ids=frozenset(current_seen_ids),
        seen_fingerprints=frozenset(current_seen_fingerprints),
    )


def replace_question(
    slots: Iterable[SelectionSlot],
    slot_id: str,
    questions: Iterable[QuestionRecord],
    rows: Iterable[TaxonomyRow],
    *,
    seen_ids: Iterable[str],
    seen_fingerprints: Iterable[str],
    language: Language | None = None,
    seed: int = 0,
) -> ReplacementOutcome:
    slot_list = list(slots)
    question_list = tuple(questions)
    row_list = tuple(rows)
    target_index = next(
        (index for index, slot in enumerate(slot_list) if slot.slot_id == slot_id),
        None,
    )
    if target_index is None:
        return ReplacementOutcome(
            slots=tuple(slot_list),
            seen_ids=frozenset(seen_ids),
            seen_fingerprints=frozenset(seen_fingerprints),
            replaced=False,
            message="The selected preview slot no longer exists.",
        )

    target = slot_list[target_index]
    current_selected = {slot.question_id for slot in slot_list}
    blocked_ids = set(seen_ids) | current_selected
    blocked_fingerprints = set(seen_fingerprints) | {
        question.content_fingerprint for question in question_list
        if question.question_id in current_selected
    }
    rng = random.Random(seed)

    preferred_rows = [row for row in row_list if row.row_id == target.preferred_row_id]
    fallback_rows = [row for row in row_list if row.row_id != target.preferred_row_id]

    def candidates(candidate_rows: list[TaxonomyRow]) -> list[tuple[QuestionRecord, TaxonomyRow]]:
        found: dict[str, tuple[QuestionRecord, TaxonomyRow]] = {}
        for row in candidate_rows:
            for question in question_list:
                if not _supports_language(question, language):
                    continue
                if question.difficulty != target.difficulty:
                    continue
                if question.question_id in blocked_ids:
                    continue
                if question.content_fingerprint in blocked_fingerprints:
                    continue
                if question_matches_row(question, row):
                    found.setdefault(question.question_id, (question, row))
        return list(found.values())

    options = candidates(preferred_rows)
    if not options:
        options = candidates(fallback_rows)
    if not options:
        return ReplacementOutcome(
            slots=tuple(slot_list),
            seen_ids=frozenset(seen_ids),
            seen_fingerprints=frozenset(seen_fingerprints),
            replaced=False,
            message=f"No unused {target.difficulty.value} replacement remains.",
        )

    replacement_question, replacement_row = rng.choice(options)
    slot_list[target_index] = replace(
        target,
        question_id=replacement_question.question_id,
        preferred_row_id=replacement_row.row_id,
    )
    updated_seen_ids = set(seen_ids)
    updated_seen_ids.add(replacement_question.question_id)
    updated_fingerprints = set(seen_fingerprints)
    updated_fingerprints.add(replacement_question.content_fingerprint)
    return ReplacementOutcome(
        slots=tuple(slot_list),
        seen_ids=frozenset(updated_seen_ids),
        seen_fingerprints=frozenset(updated_fingerprints),
        replaced=True,
        message=f"Replaced with another {target.difficulty.value} question.",
    )


def remove_question(
    slots: Iterable[SelectionSlot],
    slot_id: str,
    *,
    seen_ids: Iterable[str],
    seen_fingerprints: Iterable[str],
) -> RemovalOutcome:
    """Remove one preview slot without making its question eligible again."""
    slot_list = tuple(slots)
    remaining = tuple(slot for slot in slot_list if slot.slot_id != slot_id)
    if len(remaining) == len(slot_list):
        return RemovalOutcome(
            slots=slot_list,
            seen_ids=frozenset(seen_ids),
            seen_fingerprints=frozenset(seen_fingerprints),
            removed=False,
            message="The selected preview slot no longer exists.",
        )
    return RemovalOutcome(
        slots=remaining,
        seen_ids=frozenset(seen_ids),
        seen_fingerprints=frozenset(seen_fingerprints),
        removed=True,
        message="Removed the question. It remains excluded for this session.",
    )
