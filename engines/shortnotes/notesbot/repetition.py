"""Apply review-backed duplicate removals; never edit a worked solution.

Semantic decisions come from the source reviewer, not word-overlap heuristics.
Every retained reference is protected, and a fresh source audit must follow.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .note_quality import text_identity


class TheoryItem(BaseModel):
    section_index: int = Field(ge=0, description="Zero-based MAIN NOTES section index.")
    field: Literal["points", "tricks", "notes", "key_terms", "flow", "table", "compare", "worked_examples", "where"]
    item_index: int = Field(ge=0, description="Zero-based list index; worked_examples refers to a WHOLE duplicate question and solution, never individual steps. For flow/table/compare, use 0 for the ENTIRE block.")

    def key(self):
        return self.section_index, self.field, self.item_index


class Repetition(BaseModel):
    remove: TheoryItem
    covered_by: list[TheoryItem] = Field(min_length=1, description="Retained theory containing ALL information, assumptions and conditions of the removed item. Prefer the most complete, relevant version.")
    reason: str = Field(min_length=1, description="Why this is the same source-taught fact in the same context, without any unique teaching detail.")
    solution_summary: bool = Field(default=False, description="True ONLY when removing a bullet/callout that merely summarizes what happens in a particular retained worked example/proof. Never true for a general definition, rule, formula, condition or theorem.")


_BLOCKS = {"flow", "table", "compare"}
_LISTS = ("points", "key_terms", "tricks", "notes")


def _valid_item(notes, item: TheoryItem) -> bool:
    if item.section_index >= len(notes.sections):
        return False
    value = getattr(notes.sections[item.section_index], item.field, None)
    if item.field in _BLOCKS:
        return item.item_index == 0 and bool(value)
    return bool(value) and item.item_index < len(value)


def remove_reviewed_repetition(notes, repetitions: list[Repetition]) -> int:
    """Delete only valid, fully covered theory items; keep all cited evidence.

    References are resolved against one immutable snapshot before deletion.
    Cyclic/contradictory proposals retain both facts rather than deleting both.
    A whole duplicate example can only cite another example of the same kind.
    Its individual steps/results and visuals are ineligible. Repeated symbol
    labels may cite a fuller legend, but an equation cannot cite only a symbol.
    """
    def compatible(repeat):
        worked = repeat.remove.field == "worked_examples"
        if any((item.field == "worked_examples") != worked for item in repeat.covered_by):
            if worked or not repeat.solution_summary or repeat.remove.field not in _LISTS:
                return False  # A worked use never replaces an explicit general rule.
        if worked:
            target = notes.sections[repeat.remove.section_index].worked_examples[repeat.remove.item_index]
            return all(notes.sections[item.section_index].worked_examples[item.item_index].kind == target.kind
                       for item in repeat.covered_by)
        legend = repeat.remove.field == "where"
        if any((item.field == "where") != legend for item in repeat.covered_by):
            if legend or repeat.remove.field not in _LISTS or not all(item.field == "where" for item in repeat.covered_by):
                return False
            value = getattr(notes.sections[repeat.remove.section_index], repeat.remove.field)[repeat.remove.item_index]
            spans = re.findall(r"\$([^$]+)\$", value)
            symbols = {re.sub(r"\s+", "", notes.sections[item.section_index].where[item.item_index].symbol.strip("$"))
                       for item in repeat.covered_by}
            # Only the exact symbols named in the retained legend qualify.
            # A full equation such as F=ma is not equivalent to a legend for F.
            return bool(spans) and all(re.sub(r"\s+", "", span) in symbols for span in spans)
        if legend:
            symbol = notes.sections[repeat.remove.section_index].where[repeat.remove.item_index].symbol
            return all(text_identity(notes.sections[item.section_index].where[item.item_index].symbol) == text_identity(symbol)
                       for item in repeat.covered_by)
        return True

    valid = [repeat for repeat in repetitions
             if _valid_item(notes, repeat.remove)
             and all(_valid_item(notes, item) for item in repeat.covered_by)
             and repeat.remove.key() not in {item.key() for item in repeat.covered_by}
             and compatible(repeat)]
    protected = {item.key() for repeat in valid for item in repeat.covered_by}
    removals = {repeat.remove.key() for repeat in valid if repeat.remove.key() not in protected}
    for section_index, section in enumerate(notes.sections):
        for field in (*_LISTS, "worked_examples", "where"):
            # ``where`` is set to None when a detached legend is cleared; a
            # TypeError here used to throw away a paid duplicate pass.
            setattr(section, field, [item for index, item in enumerate(getattr(section, field) or [])
                                    if (section_index, field, index) not in removals])
        for field in _BLOCKS:
            if (section_index, field, 0) in removals:
                setattr(section, field, [] if field == "flow" else None)
    if removals:
        # An old revision hook must not be promoted back into the body on render.
        notes.mindmap = None
    return len(removals)


def remove_exact_repetition(notes) -> int:
    """Remove exact cross-field copies within a topic; preserve math and context.

    Never compare against worked steps, other topics, or partial sentences.
    Paraphrases and equations in different notation need the source reviewer.
    """
    removed = 0
    for section in notes.sections:
        seen = set()
        for field in _LISTS:
            kept = []
            for item in getattr(section, field) or []:
                identity = text_identity(item)
                if identity and identity in seen:
                    removed += 1
                else:
                    kept.append(item)
                    if identity:
                        seen.add(identity)
            setattr(section, field, kept)
    return removed
