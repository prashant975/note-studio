import io

import pytest
from pypdf import PdfWriter

from notesbot.errors import NotesCoverageError
from notesbot.gemini_client import _ensure_mindmap, _generation_schema_json
from notesbot.prompts import Branch, MindMap, Section, ShortNotes, WorkedExample
from notesbot.source_coverage import CoverageIssue, CoverageReview, ensure_source_coverage
from sheets_bridge.coverage import _content_verdict


def source_pdf():
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=600, height=800)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def notes(formula=False):
    sections = [Section(heading="Definition", points=["Source definition."], source_local_page=1)]
    if formula:
        sections.append(Section(heading="Formula", points=["$C=AB$"], source_local_page=2))
    return ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=sections)


def review(issues=()):
    return CoverageReview(reviewed_pages=[1, 2], unreadable_pages=[], issues=list(issues))


def test_worked_revision_recovers_main_formula_before_removing_map():
    worked = WorkedExample(statement="Prove the identity.", kind="proof", steps=["$AI=A$"] * 7)
    n = notes()
    n.sections = [Section(heading="Identity", worked_examples=[worked]), Section(heading="Inverse Formula")]
    n.mindmap = MindMap(center="Matrices", branches=[Branch(label="Inverse Formula", children=["$A^{-1}=\\frac{1}{|A|}\\operatorname{adj}(A)$"])])
    _ensure_mindmap(n)
    assert n.sections[1].points == ["$A^{-1}=\\frac{1}{|A|}\\operatorname{adj}(A)$"]
    assert n.mindmap is None
    _ensure_mindmap(n)
    assert len(n.sections[1].points) == 1


def test_coverage_repair_is_one_call_with_no_confirmation_review():
    gap = CoverageIssue(source_page=2, kind="formula", detail="The source teaches C=AB.")
    answers = iter([review([gap]), notes(True)])
    calls = []
    def generate(prompt, schema, max_tokens):
        calls.append((prompt, schema))
        return next(answers)
    result = ensure_source_coverage(source_pdf(), notes(), language="english", generate=generate)
    assert result.sections[1].points == ["$C=AB$"]
    assert [schema for _, schema in calls] == [CoverageReview, ShortNotes]
    assert "Source definition." in calls[1][0]          # the repair sees the candidate
    assert "C=AB" in calls[1][0]                        # ...and the review it must fix


def test_map_formula_recovery_does_not_duplicate_inline_formula_or_drop_sign():
    n = notes()
    n.sections = [Section(heading="Identity", points=["The identity gives $AI = A$."], worked_examples=[WorkedExample(statement="Calculate AI.", steps=["$AI=A$"])])]
    n.mindmap = MindMap(center="Matrices", branches=[Branch(label="Identity", children=["$AI=A$", "$AI=-A$"])])
    _ensure_mindmap(n)
    assert n.sections[0].points == ["The identity gives $AI = A$.", "$AI=-A$"]


def test_cache_separates_model_choices(monkeypatch):
    from sheets_bridge.output_cache import key
    args = dict(file_id="source", out_name="notes", language="hindi", subject="Maths", signature="")
    monkeypatch.setenv("PW_PROXY_MODEL", "gemini-3.6-flash")
    previous = key(**args)
    monkeypatch.setenv("PW_PROXY_MODEL", "gemini-3.8-flash")
    assert key(**args) != previous


def test_a_repair_that_loses_content_is_not_kept_over_the_original():
    """With no confirmation review, a local size check guards the repair: a
    repair asked only to add theory must not come back clearly thinner."""
    gap = CoverageIssue(source_page=2, kind="definition", detail="Missing a definition")
    answers = iter([review([gap]), notes()])                    # the repair is thinner
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(True), language="english",
                                    generate=lambda *_: next(answers), log=lines.append)
    assert result.sections[1].points == ["$C=AB$"]              # the fuller original survives
    assert any("the repair dropped content" in line for line in lines)


def test_an_empty_repair_is_refused_unless_questions_were_to_be_removed():
    gap = CoverageIssue(source_page=1, kind="condition", detail="Missing condition")
    empty = ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[])
    answers = iter([review([gap]), empty])
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(), language="english",
                                    generate=lambda *_: next(answers), log=lines.append)
    assert [section.heading for section in result.sections] == ["Definition"]
    assert any("came back empty" in line for line in lines)


def test_a_repair_that_cannot_run_keeps_the_notes():
    gap = CoverageIssue(source_page=2, kind="formula", detail="The source teaches C=AB.")
    calls = []
    def generate(prompt, schema, max_tokens):
        calls.append(schema)
        if schema is ShortNotes:
            raise TimeoutError("gateway")
        return review([gap])
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(), language="english",
                                    generate=generate, log=lines.append)
    assert calls == [CoverageReview, ShortNotes]
    assert [section.heading for section in result.sections] == ["Definition"]
    assert any("repair could not run (TimeoutError)" in line for line in lines)


@pytest.mark.parametrize("r", [
    CoverageReview(reviewed_pages=[1], unreadable_pages=[], issues=[]),
    CoverageReview(reviewed_pages=[1, 2], unreadable_pages=[2], issues=[]),
    CoverageReview(reviewed_pages=[1, 2], unreadable_pages=[], issues=[CoverageIssue(source_page=3, kind="formula", detail="Wrong page")]),
])
def test_incomplete_or_invalid_page_audit_keeps_the_notes_as_generated(r):
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(True), language="english",
                                    generate=lambda *_: r, log=lines.append)
    assert [section.heading for section in result.sections] == ["Definition", "Formula"]
    assert any("did not cover every page" in line for line in lines)


def test_failed_coverage_is_not_a_cover_page_verdict():
    assert not _content_verdict(NotesCoverageError())


def test_new_generation_requires_theory_examples_and_provenance_but_old_notes_load():
    required = _generation_schema_json(ShortNotes)["$defs"]["Section"]["required"]
    assert {"points", "worked_examples", "source_local_page", "source_order"} <= set(required)
    assert Section(heading="Legacy").points == []


def test_automation_ships_the_group_even_when_the_source_check_cannot_run(monkeypatch):
    monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", "on")   # this test covers the review path
    from sheets_bridge import coverage
    from notesbot import gemini_client, source_coverage
    monkeypatch.setattr(gemini_client, "generate_short_notes", lambda *_a, **_kw: notes(True))
    monkeypatch.setattr(coverage, "is_thin", lambda *_: False)
    monkeypatch.setattr(source_coverage, "ensure_source_coverage", lambda *_a, **_kw: (_ for _ in ()).throw(NotesCoverageError()))
    monkeypatch.setattr(source_coverage, "consolidate_lecture", lambda _pdf, current, **_kw: current)
    lines = []
    merged = coverage.generate_complete_notes(source_pdf(), language="english", subject_hint="Maths",
                                              verify_source=True, log=lines.append)
    assert [section.heading for section in merged.sections] == ["Definition", "Formula"]
    assert any("source check could not run" in line and "NotesCoverageError" in line for line in lines)


def test_parallel_groups_keep_proxy_task_context(monkeypatch):
    from sheets_bridge import coverage, pw_transport
    pw_transport.set_task(task_id="coverage-test", filename="lecture.pdf", count=2)
    def generate_group(group, **kwargs):
        assert kwargs["verify_source"] is True
        return pw_transport.current_task(), None
    monkeypatch.setattr(coverage, "_generate_group", generate_group)
    monkeypatch.setattr(coverage, "GROUP_PAUSE_SECONDS", 0)
    groups = coverage.split_pages(source_pdf(), 1)
    result = coverage._generate_groups(groups, instruction="", language="english", subject_hint="Maths", emit=lambda *_: None, workers=2, verify_source=True)
    assert [task["task_id"] for task, _ in result] == ["coverage-test", "coverage-test"]


def test_an_empty_answer_is_skipped_without_a_source_check(monkeypatch):
    """Pages the generator finds empty are skipped at once; reviewing nothing
    against the source was one more paid call for every cover slide."""
    from notesbot import gemini_client, source_coverage
    from notesbot.errors import UserError
    from sheets_bridge import coverage
    monkeypatch.setattr(coverage, "GROUP_ATTEMPTS", 1)
    monkeypatch.setattr(gemini_client, "generate_short_notes", lambda *_: (_ for _ in ()).throw(UserError("No study content")))
    observed=[]
    def check(pdf, candidate, **kwargs):
        observed.append(candidate.sections)
        return notes(True)
    monkeypatch.setattr(source_coverage, "ensure_source_coverage", check)
    group=coverage.split_pages(source_pdf(), 2)[0]
    recovered, error=coverage._generate_group(group, instruction="", language="english", subject_hint="Maths", emit=lambda *_: None, allow_split=0, verify_source=True)
    assert recovered is None and isinstance(error, UserError)
    assert observed == []


# --------------------------------------------------------------------------- #
# Duplicates and empty pages must not cost a source-verified lecture
# --------------------------------------------------------------------------- #
def repeated_notes():
    return ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[
        Section(heading="Multiplication", source_local_page=1, points=[
            "Match the inner dimensions.",
            "The inner dimensions must match.",
            "Inner numbers match; outer numbers give the order."])])


def repetition_review(remove_index: int):
    from notesbot.repetition import Repetition, TheoryItem

    return CoverageReview(
        reviewed_pages=[1, 2], unreadable_pages=[], issues=[],
        repetitions=[Repetition(
            remove=TheoryItem(section_index=0, field="points", item_index=remove_index),
            covered_by=[TheoryItem(section_index=0, field="points", item_index=0)],
            reason="The retained bullet states the same rule with its conditions.")])


def test_reported_repeats_are_removed_locally_without_another_review():
    """The reviewer found no coverage gap, so these notes are complete. The
    repeats it names are removed here; no further review call is paid for."""
    answers = iter([repetition_review(2)])
    calls, lines = [], []

    def generate(prompt, schema, max_tokens):
        calls.append(schema)
        return next(answers)

    result = ensure_source_coverage(source_pdf(), repeated_notes(), language="english",
                                    generate=generate, log=lines.append)

    assert result.sections[0].points == ["Match the inner dimensions.", "The inner dimensions must match."]
    assert calls == [CoverageReview]              # one review, nothing after it


def test_unapplicable_duplicate_list_keeps_the_notes():
    from notesbot.repetition import Repetition, TheoryItem

    unusable = CoverageReview(
        reviewed_pages=[1, 2], unreadable_pages=[], issues=[],
        repetitions=[Repetition(
            remove=TheoryItem(section_index=9, field="points", item_index=0),
            covered_by=[TheoryItem(section_index=9, field="points", item_index=1)],
            reason="Stale indices from an earlier draft.")])
    lines = []
    result = ensure_source_coverage(source_pdf(), repeated_notes(), language="english",
                                    generate=lambda *_: unusable, log=lines.append)
    assert len(result.sections[0].points) == 3
    assert any("could not apply" in line for line in lines)


def one_page_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_single_page_repair_may_echo_the_printed_slide_number():
    """A one-page chunk has exactly one page, but the model writes the number
    printed on the slide ("25"). That is not a reason to fail the lecture."""
    repaired = ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[
        Section(heading="Definition", points=["Source definition."], source_local_page=25)])
    answers = iter([
        CoverageReview(reviewed_pages=[1], unreadable_pages=[],
                       issues=[CoverageIssue(source_page=1, kind="definition", detail="Missing definition.")]),
        repaired,
        CoverageReview(reviewed_pages=[1], unreadable_pages=[], issues=[]),
    ])
    result = ensure_source_coverage(one_page_pdf(), notes(), language="english",
                                    generate=lambda *_a: next(answers))
    assert [section.source_local_page for section in result.sections] == [1]


def test_pages_without_eligible_theory_are_rechecked_not_rejected():
    """Question-only, cover and closing pages legitimately yield nothing. The
    next review decides whether that is true; a second repair is not allowed."""
    empty = ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[])
    answers = iter([
        review([CoverageIssue(source_page=1, kind="excluded_content", detail="Only questions here.")]),
        empty,
        review(),
    ])
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(), language="english",
                                    generate=lambda *_a: next(answers), log=lines.append)
    assert result.sections == []
    assert any("no eligible theory" in line for line in lines)


def test_a_review_budget_already_spent_skips_the_repair(monkeypatch):
    from notesbot import generation_policy
    gap = CoverageIssue(source_page=2, kind="formula", detail="The source teaches C=AB.")
    calls = []
    def generate(prompt, schema, max_tokens):
        calls.append(schema)
        return review([gap])
    monkeypatch.setattr(generation_policy.GenerationRun, "remaining_seconds", lambda self: 0.0)
    lines = []
    result = ensure_source_coverage(source_pdf(), notes(), language="english",
                                    generate=generate, log=lines.append)
    assert calls == [CoverageReview]
    assert [section.heading for section in result.sections] == ["Definition"]
    assert any("no time left for a repair" in line for line in lines)


def test_a_failed_source_check_keeps_the_notes_and_names_the_real_reason(monkeypatch):
    """A timeout, a gateway error or a malformed answer is not a verdict that
    the notes miss content: the notes ship as generated and the log says why
    the check did not run."""
    monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", "on")   # this test covers the review path
    from notesbot import gemini_client, source_coverage
    from sheets_bridge import coverage

    monkeypatch.setattr(gemini_client, "generate_short_notes", lambda *_a: notes())

    def time_out(*_a, **_kw):
        from notesbot.errors import VisualFidelityError

        raise VisualFidelityError("deadline_exceeded")

    monkeypatch.setattr(source_coverage, "ensure_source_coverage", time_out)
    lines = []
    group = coverage.split_pages(source_pdf(), 2)[0]
    recovered, error = coverage._generate_group(
        group, instruction="", language="english", subject_hint="Maths",
        emit=lines.append, allow_split=0, verify_source=True)

    assert recovered is not None and recovered.sections[0].heading == "Definition"
    assert error is None
    assert any("source check could not run" in line and "VisualFidelityError" in line for line in lines)


def test_a_coverage_verdict_never_drops_the_group(monkeypatch):
    monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", "on")   # this test covers the review path
    from notesbot import gemini_client, source_coverage
    from sheets_bridge import coverage

    monkeypatch.setattr(gemini_client, "generate_short_notes", lambda *_a: notes())
    monkeypatch.setattr(source_coverage, "ensure_source_coverage",
                        lambda *_a, **_kw: (_ for _ in ()).throw(NotesCoverageError()))
    lines = []
    group = coverage.split_pages(source_pdf(), 2)[0]
    recovered, error = coverage._generate_group(group, instruction="", language="english",
                                                subject_hint="Maths", emit=lines.append,
                                                allow_split=0, verify_source=True)
    assert recovered is not None and error is None
    assert any("NotesCoverageError" in line for line in lines)


def test_group_review_reporting_is_wired_end_to_end(monkeypatch):
    """The run log must name what each review found. The callbacks run inside
    the group's try/except, so a broken one would surface as a coverage
    failure rather than an error - this exercises the real call path."""
    monkeypatch.setenv("SHEETS_BRIDGE_SOURCE_REVIEW", "on")   # this test covers the review path
    from notesbot import gemini_client
    from sheets_bridge import coverage

    monkeypatch.setattr(gemini_client, "generate_short_notes", lambda *_a: notes())

    def fake_model(files, prompt, schema, max_tokens, temperature, budget):
        assert schema is CoverageReview
        return review()

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", fake_model)
    lines = []
    group = coverage.split_pages(source_pdf(), 2)[0]
    recovered, _ = coverage._generate_group(
        group, instruction="", language="english", subject_hint="Maths",
        emit=lines.append, allow_split=0, verify_source=True)

    assert recovered is not None
    assert any("review 1: 0 issue(s), 0 repeat(s)" in line for line in lines), lines



def test_example_or_proof_findings_never_buy_a_repair():
    """Concise notes leave worked examples and proofs out on purpose; a review
    that only asks for them used to cost a full repair call anyway."""
    wanted = [CoverageIssue(source_page=1, kind="example", detail="Worked example 1 is missing."),
              CoverageIssue(source_page=2, kind="proof", detail="The proof steps are missing.")]
    calls, lines = [], []
    def generate(prompt, schema, max_tokens):
        calls.append(schema)
        return review(wanted)
    result = ensure_source_coverage(source_pdf(), notes(), language="english",
                                    generate=generate, log=lines.append)
    assert calls == [CoverageReview]
    assert [section.heading for section in result.sections] == ["Definition"]
    assert any("ignored 2 example/proof issue(s)" in line for line in lines)


def test_a_real_gap_is_still_repaired_without_asking_for_the_example():
    mixed = [CoverageIssue(source_page=1, kind="example", detail="Worked example 7 is missing."),
             CoverageIssue(source_page=2, kind="formula", detail="The source teaches C=AB.")]
    answers = iter([review(mixed), notes(True)])
    prompts = []
    def generate(prompt, schema, max_tokens):
        prompts.append((prompt, schema))
        return next(answers)
    result = ensure_source_coverage(source_pdf(), notes(), language="english", generate=generate)
    assert [schema for _, schema in prompts] == [CoverageReview, ShortNotes]
    assert "C=AB" in prompts[1][0] and "Worked example 7" not in prompts[1][0]
    assert result.sections[1].points == ["$C=AB$"]
