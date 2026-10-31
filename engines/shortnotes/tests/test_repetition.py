import json

import pytest
from pydantic import ValidationError

from notesbot.errors import NotesCoverageError
from notesbot.gemini_client import _ensure_mindmap
from notesbot.prompts import Branch, MindMap, Section, ShortNotes, Table, WorkedExample, WhereItem
from notesbot.repetition import Repetition, TheoryItem, remove_exact_repetition, remove_reviewed_repetition
from notesbot.source_coverage import CoverageIssue, CoverageReview, ensure_source_coverage
from test_source_coverage import source_pdf


def ref(section=0, field="points", index=0):
    return TheoryItem(section_index=section, field=field, item_index=index)


def duplicate(remove, *keep):
    return Repetition(remove=remove, covered_by=list(keep), reason="Retained statement contains all the same information.")


def candidate():
    return ShortNotes(lecture_no="1", subject="Maths", title="Matrix multiplication", sections=[
        Section(heading="Multiplication", source_local_page=1, points=["Match the inner dimensions."],
                worked_examples=[WorkedExample(statement="Calculate AB.", steps=["Match the inner dimensions.", "$AB=C$"], result="$AB=C$")]),
        Section(heading="Multiplication condition", source_local_page=2,
                points=["Multiplication requires columns of A to equal rows of B; the outer dimensions give the product order.", "AB defined does not guarantee BA is defined."]),
    ])


def theory_candidate():
    notes = candidate()
    notes.sections[0].worked_examples = []
    # Keep a distinct theory fact so duplicate cleanup need not drop the section.
    notes.sections[0].notes = ["The order of factors matters."]
    return notes


def review(repetitions=(), issues=()):
    return CoverageReview(reviewed_pages=[1, 2], unreadable_pages=[], issues=list(issues), repetitions=list(repetitions))


def test_later_complete_version_replaces_earlier_slide_hint_without_touching_solution():
    notes = candidate()
    original_work = notes.sections[0].worked_examples[0].model_dump()
    assert remove_reviewed_repetition(notes, [duplicate(ref(), ref(1))]) == 1
    assert notes.sections[0].points == []
    assert len(notes.sections[1].points) == 2  # retains the distinct exception
    assert notes.sections[0].worked_examples[0].model_dump() == original_work


def test_exact_cross_field_cleanup_preserves_signs_case_and_hindi_marks():
    notes = candidate()
    section = notes.sections[0]
    section.points = ["$A=B$", "$A=-B$", "$a=b$", "कुल", "कूल"]
    section.tricks = ["**$A=B$**", "कूल"]
    assert remove_exact_repetition(notes) == 2
    assert len(section.points) == 5 and section.tricks == []
    assert section.worked_examples[0].steps == ["Match the inner dimensions.", "$AB=C$"]
    assert remove_exact_repetition(notes) == 0


def test_invalid_or_cyclic_references_cannot_delete_all_versions():
    notes = candidate()
    original = notes.model_dump()
    cycles = [duplicate(ref(), ref(1)), duplicate(ref(1), ref()), duplicate(ref(), ref(99))]
    assert remove_reviewed_repetition(notes, cycles) == 0
    assert notes.model_dump() == original
    with pytest.raises(ValidationError):
        TheoryItem(section_index=0, field="steps", item_index=0)


def test_whole_duplicate_example_keeps_complete_solution_and_cannot_replace_general_rule():
    notes = candidate()
    full = notes.sections[0].worked_examples[0].model_copy(deep=True)
    full.steps.append("Explain the source conclusion.")
    notes.sections[1].worked_examples = [full]
    assert remove_reviewed_repetition(notes, [duplicate(ref(), ref(1, "worked_examples"))]) == 0
    assert remove_reviewed_repetition(notes, [duplicate(ref(field="worked_examples"), ref(1, "worked_examples"))]) == 1
    assert notes.sections[0].worked_examples == []
    assert notes.sections[1].worked_examples[0].model_dump() == full.model_dump()


def test_solution_specific_summary_can_be_removed_without_changing_proof_steps():
    notes = candidate()
    notes.sections[0].points = ["In this worked example, first match the inner dimensions."]
    before = notes.sections[0].worked_examples[0].model_dump()
    proposal = duplicate(ref(), ref(field="worked_examples"))
    assert remove_reviewed_repetition(notes, [proposal]) == 0
    proposal.solution_summary = True
    assert remove_reviewed_repetition(notes, [proposal]) == 1
    assert notes.sections[0].worked_examples[0].model_dump() == before


def test_repeated_symbol_legend_can_be_removed_without_replacing_general_theory():
    notes = candidate()
    notes.sections[0].where = [WhereItem(symbol="$n$", meaning="Number of shared inner dimensions")]
    notes.sections[1].where = [WhereItem(symbol="$n$", meaning="Number of shared inner dimensions")]
    assert remove_reviewed_repetition(notes, [duplicate(ref(), ref(1, "where"))]) == 0
    assert remove_reviewed_repetition(notes, [duplicate(ref(field="where"), ref(1, "where"))]) == 1
    assert notes.sections[0].where == []
    assert len(notes.sections[1].where) == 1


def test_bare_symbol_label_can_keep_fuller_legend_but_full_equation_cannot():
    notes = candidate()
    notes.sections[0].points = ["सारणिक $|A|$", "$F=ma$"]
    notes.sections[0].where = [WhereItem(symbol="$|A|$", meaning="आव्यूह A का सारणिक"), WhereItem(symbol="$F$", meaning="Force")]
    assert remove_reviewed_repetition(notes, [duplicate(ref(index=1), ref(field="where", index=1))]) == 0
    assert remove_reviewed_repetition(notes, [duplicate(ref(), ref(field="where"))]) == 1
    assert notes.sections[0].points == ["$F=ma$"]


def test_deletions_use_original_indices_and_leave_all_retained_evidence():
    notes = candidate()
    notes.sections[0].points = ["Short A", "Full A with condition", "Shorter A", "Distinct B"]
    assert remove_reviewed_repetition(notes, [duplicate(ref(index=0), ref(index=1)), duplicate(ref(index=2), ref(index=1))]) == 2
    assert notes.sections[0].points == ["Full A with condition", "Distinct B"]


def test_duplicate_cleanup_needs_no_second_audit_and_is_render_idempotent():
    notes = theory_candidate()
    notes.mindmap = MindMap(center="Matrices", branches=[Branch(label="Multiplication", children=["Match the inner dimensions."])])
    answers = iter([review([duplicate(ref(), ref(1))])])
    prompts = []
    def generate(prompt, *_):
        prompts.append(prompt)
        return next(answers)
    cleaned = ensure_source_coverage(source_pdf(), notes, language="english", generate=generate)
    assert len(prompts) == 1
    assert cleaned.sections[0].points == []
    first = cleaned.model_dump()
    _ensure_mindmap(cleaned, include_revision_map=False)
    assert cleaned.model_dump() == first


def test_a_review_with_issues_goes_to_the_repair_and_leaves_its_repeats_to_it():
    """Repeat indices refer to the notes before the repair, so they are not
    applied locally; the repair prompt carries them instead."""
    notes = theory_candidate()
    missing = CoverageIssue(source_page=1, kind="condition", detail="A distinct source condition is missing.")
    answers = iter([review([duplicate(ref(), ref(1))], issues=[missing]), theory_candidate()])
    prompts = []
    def generate(prompt, *_):
        prompts.append(prompt)
        return next(answers)
    cleaned = ensure_source_coverage(source_pdf(), notes, language="english", generate=generate)
    assert cleaned.sections[0].points == ["Match the inner dimensions."]
    assert len(prompts) == 2 and "Retained statement contains all the same information." in prompts[1]


def test_unresolved_duplicate_review_cannot_pass_silently():
    """An unusable duplicate list (stale indices, a keeper that does not exist)
    keeps the notes and says so. It used to raise, which threw away a lecture
    the same review had just confirmed complete - duplicates are a tidiness
    problem, and this review reported no coverage gap."""
    notes = theory_candidate()
    invalid = review([duplicate(ref(), ref(99))])
    lines = []
    result = ensure_source_coverage(source_pdf(), notes, language="english",
                                    generate=lambda *_: invalid, log=lines.append)
    assert result.sections[0].points == ["Match the inner dimensions."]
    assert any("could not apply" in line for line in lines)


def test_whole_flow_can_be_removed_but_individual_step_is_not_addressable():
    notes = candidate()
    notes.sections[0].flow = ["Match inner dimensions", "Use outer dimensions"]
    assert remove_reviewed_repetition(notes, [duplicate(ref(field="flow", index=1), ref(1))]) == 0
    assert remove_reviewed_repetition(notes, [duplicate(ref(field="flow"), ref(1))]) == 1
    assert notes.sections[0].flow == []


def test_generated_table_revision_hooks_do_not_become_duplicate_body_points():
    notes = candidate()
    notes.sections = [Section(heading="Orders", table=Table(headers=["A", "B"], rows=[["3 x 4", "4 x 2"]]))]
    _ensure_mindmap(notes)
    first = notes.model_dump()
    _ensure_mindmap(notes)
    assert notes.model_dump() == first
    assert notes.sections[0].points == []


def test_whole_lecture_audit_sees_repeats_across_page_group_boundary(monkeypatch):
    from sheets_bridge import coverage
    from notesbot import source_coverage
    monkeypatch.setattr(coverage, "pages_per_call", lambda: 1)
    first, second = candidate().sections
    def groups(groups, **kwargs):
        return [(ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[section]), None)
                for section in (first, second)]
    monkeypatch.setattr(coverage, "_generate_groups", groups)
    observed = []
    def check(pdf, notes, **kwargs):
        observed.append((pdf, [section.heading for section in notes.sections]))
        remove_reviewed_repetition(notes, [duplicate(ref(), ref(1))])
        return notes
    monkeypatch.setattr(source_coverage, "consolidate_lecture", check)
    source = source_pdf()
    cleaned = coverage.generate_complete_notes(source, language="english", subject_hint="Maths", verify_source=True)
    assert observed == [(source, ["Multiplication", "Multiplication condition"])]
    assert cleaned.sections[0].points == []


def test_focused_repetition_review_is_the_only_lecture_wide_call():
    from notesbot.source_coverage import consolidate_lecture, RepetitionReview
    answers = iter([RepetitionReview(repetitions=[duplicate(ref(), ref(1))])])
    schemas = []
    def generate(prompt, schema, *_):
        schemas.append(schema)
        return next(answers)
    notes = consolidate_lecture(source_pdf(), theory_candidate(), language="english", generate=generate)
    assert schemas == [RepetitionReview]        # groups were already source-checked
    assert notes.sections[0].points == []
    assert notes.sections[1].points[0].endswith("product order.")



def test_the_lecture_wide_duplicate_pass_sends_no_pdf(monkeypatch):
    from notesbot import gemini_client
    from notesbot.source_coverage import RepetitionReview, consolidate_lecture
    sent = []
    def fake_schema_call(files, prompt, schema, *_):
        sent.append((files, prompt, schema))
        return RepetitionReview(repetitions=[])
    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", fake_schema_call)
    consolidate_lecture(source_pdf(), theory_candidate(), language="english")
    assert len(sent) == 1
    files, prompt, schema = sent[0]
    assert files == [] and schema is RepetitionReview
    assert "Only the notes JSON is given" in prompt and "MAIN NOTES JSON:" in prompt



def test_duplicate_removal_survives_a_legend_emptied_to_none():
    """Row 189: the lecture-wide duplicate pass was paid for, then thrown away
    on a TypeError, because an earlier step had set a section's ``where`` to None."""
    notes = theory_candidate()
    notes.sections[1].where = None
    notes.sections[0].notes = None
    removed = remove_reviewed_repetition(notes, [duplicate(ref(), ref(1))])
    assert removed == 1 and notes.sections[0].points == []
    assert notes.sections[1].where == [] and notes.sections[0].notes == []
    assert remove_exact_repetition(notes) == 0
