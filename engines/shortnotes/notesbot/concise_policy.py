"""Shared theory-only selection policy and conservative Q&A safeguards."""
from __future__ import annotations

import re


CONCISE_POLICY = """CONCISE NOTES — THEORY ONLY
The student explicitly excludes ALL questions and solutions. Do not include
MCQs, options, question prompts, answers, answer keys, worked examples, solved
numericals, solution/proof/derivation steps or question-and-answer blocks in
ANY field, table, mind map or visual. Always leave worked_examples empty.
Retain every unique source-taught definition, general formula, theorem statement,
condition, exception, concept and result needed for theory revision. When a
question or its solution teaches a new general fact, extract that fact once as
declarative theory with its source-stated scope. Do not copy the question, its
specific givens/calculation, options, answer label or steps. Never infer a new
general rule from one numerical example. Conceptual examples such as 'blood,
bone and cartilage are connective tissues' are theory and should be retained.
Omitting questions, answers and solution/proof/derivation steps is intentional,
NOT a coverage gap. Question-only slides may yield little or no new theory.
Preserve actual biological/chemical process steps; these are not worked solutions.
Exclude lecturer biographies, greetings, course agendas, study schedules and
administrative material. Keep topic-specific source-taught warnings and mnemonics.
Keep the most complete relevant statement of repeated theory once, merging any
unique conditions. Never repeat the same information in multiple formats.
If a bullet combines a duplicated fact with a unique fact, keep only its unique
part in that bullet. Do not retain the duplicated clause for context alone.
Always leave mindmap null: the app builds any revision mind map itself from
the finished notes.
"""

# Where a category/subject asks for them (``solved_examples`` in
# notesbot/format_rules/rules.json, e.g. Foundation Maths) the teacher's solved
# examples are kept; everything else about concise notes stays the same.
SOLVED_EXAMPLES_POLICY = """CONCISE NOTES — THEORY + SOLVED EXAMPLES
For this lecture the student wants the teacher's SOLVED EXAMPLES in the notes,
together with the concise theory. This overrides every other instruction to
leave worked_examples empty or to omit worked examples and solution steps.
- Put each example the teacher actually solves in the lecture into the
  worked_examples list of the section it belongs to: statement = the complete
  question as given (with its numbers), steps = the teacher's solution steps in
  order, one step per entry with its equation, result = the final answer the
  source states. Use kind "proof" for a proof the teacher works through.
- Copy only what the source shows. Never invent an example, never solve an
  unsolved question, never complete missing steps or answers.
- Leave out unsolved exercises, practice or homework questions, MCQ lists,
  options and answer keys: they are not solved examples.
- Keep every unique source-taught definition, formula, theorem statement,
  condition, exception and result as theory points. Examples never replace the
  theory, and an example's steps are not repeated as theory bullets.
Exclude lecturer biographies, greetings, course agendas, study schedules and
administrative material. Keep topic-specific source-taught warnings and mnemonics.
Keep the most complete relevant statement of repeated theory once, merging any
unique conditions. Never repeat the same information in multiple formats.
Always leave mindmap null: the app builds any revision mind map itself from
the finished notes.
"""

# Added to review prompts that otherwise treat any solution as excluded content.
SOLVED_EXAMPLES_REVIEW_NOTE = """SOLVED EXAMPLES ARE REQUIRED CONTENT HERE: the question, steps and answer in
worked_examples are part of these notes. Do not report or remove them as
excluded_content; still flag unsolved questions, options or answer keys.
"""


def solved_examples_allowed() -> bool:
    from . import categories

    return categories.solved_examples()


def policy() -> str:
    """The selection policy for the current category and subject."""
    return SOLVED_EXAMPLES_POLICY if solved_examples_allowed() else CONCISE_POLICY


# Deliberately narrow: 'solution' is also a legitimate chemistry term. Semantic
# questions without explicit markers are checked by the PDF-backed model review.
_QA_MARKER = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:\*\*)?"
    r"(?:(?:question|प्रश्न)\s*(?:\d+|[०-९]+)\s*[:.)-]"
    r"|Q\s*\.?\s*\d+\s*[:.)-]"
    r"|(?:answer|उत्तर)\s*:\s*(?:option\b|विकल्प\b|\([A-Da-d]\))"
    r"|(?:solution|हल)\s*:\s*(?:\n|\d+[.)]))",
    re.IGNORECASE,
)


def question_content(notes) -> list[tuple[int, str]]:
    """Identify explicit Q&A without silently deleting possibly unique theory.

    Solved examples are Q&A only where the format keeps them out; theory
    fields are checked either way.
    """
    findings = []
    examples_allowed = solved_examples_allowed()
    for index, section in enumerate(notes.sections):
        if getattr(section, "worked_examples", None) and not examples_allowed:
            findings.append((index, "worked_examples contains excluded questions/solutions; extract unique general theory and empty this list."))
        data = section.model_dump()
        data.pop("worked_examples", None)
        def walk(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from walk(item)
        if any(_QA_MARKER.search(value) for value in walk(data)):
            findings.append((index, "Explicit question/solution markers remain in theory fields; keep only their unique general teaching."))
    return findings


def require_concise_notes(notes) -> None:
    from .errors import NotesQualityError
    if question_content(notes):
        raise NotesQualityError()
