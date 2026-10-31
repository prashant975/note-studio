import io
import json

import pytest
from reportlab.pdfgen.canvas import Canvas

from notesbot.errors import ModelBudgetError, NotesCoverageError, NotesQualityError
from notesbot.final_quality import (QualityEdit, QualityIssue, QualityReview, apply_quality_edits,
                                    finalize_notes_pdf, valid_receipt)
from notesbot.prompts import Section, ShortNotes, WorkedExample


def notes():
    return ShortNotes(lecture_no="1", title="Matrices", subject="Maths", sections=[
        Section(heading="Multiplication", points=["The rows is horizontal.",
            "For $AB$, columns of $A$ must equal rows of $B$."])])


def pdf_for(candidate=None, pages=1):
    buf = io.BytesIO()
    canvas = Canvas(buf)
    for _ in range(pages):
        canvas.drawString(50, 750, candidate.sections[0].points[0] if candidate else "Lecture source.")
        canvas.showPage()
    canvas.save()
    return buf.getvalue()


def review(issues=(), edits=(), pages=(1,)):
    return QualityReview(reviewed_pages=list(pages), unreadable_pages=[], issues=list(issues), edits=list(edits))


def grammar_edit():
    return QualityEdit(path="/sections/0/points/0", before="The rows is horizontal.", after="The rows are horizontal.")


def test_finalization_binds_approval_to_the_exact_pdf_and_notes():
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=lambda *_: review())
    raw = current.model_dump_json().encode()
    assert valid_receipt(raw, pdf, receipt)
    assert not valid_receipt(raw, pdf + b"changed", receipt)
    assert not valid_receipt(raw + b" ", pdf, receipt)
    assert receipt["correction_passes"] == 0


def test_exhausted_gateway_budget_cannot_approve_or_ship_an_unreviewed_pdf():
    calls = []
    def exhausted(*args):
        calls.append(args)
        raise ModelBudgetError()
    with pytest.raises(ModelBudgetError):
        finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for, generate=exhausted)
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [
    QualityReview(reviewed_pages=[], unreadable_pages=[], issues=[], edits=[]),
    QualityReview(reviewed_pages=[1], unreadable_pages=[1], issues=[], edits=[]),
    review([QualityIssue(page=2, kind="layout", detail="Wrong page reference")]),
])
def test_unread_or_invalid_page_review_ships_unreviewed(bad):
    lines = []
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=lambda *_: bad, log=lines.append)
    assert receipt["verdict"] == "unreviewed"
    assert valid_receipt(current.model_dump_json().encode(), pdf, receipt)
    assert any("did not cover every page" in line for line in lines)


def test_grammar_repair_preserves_theory_then_reviews_the_new_pdf():
    original = notes()
    theory = original.sections[0].points[1]
    answers = iter([review([QualityIssue(page=1, kind="grammar", detail="Subject-verb agreement")], [grammar_edit()]), review()])
    rendered = []
    def render(candidate):
        rendered.append(candidate.sections[0].points[0])
        return pdf_for(candidate)
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), original, language="english", render=render,
                                               generate=lambda *_: next(answers))
    assert rendered == ["The rows is horizontal.", "The rows are horizontal."]
    assert current.sections[0].points[1] == theory
    assert receipt["correction_passes"] == 1 and receipt["verdict"] == "passed"
    assert valid_receipt(current.model_dump_json().encode(), pdf, receipt)


def test_invalid_patch_is_atomic_and_cannot_delete_steps_or_visual_metadata():
    candidate = notes()
    original = candidate.model_dump()
    invalid = QualityEdit(path="/sections/0/source_local_page", before="0", after="1")
    with pytest.raises(NotesQualityError):
        apply_quality_edits(candidate, [grammar_edit(), invalid])
    assert candidate.model_dump() == original
    stale = grammar_edit().model_copy(update={"before": "Different text"})
    with pytest.raises(NotesQualityError):
        apply_quality_edits(candidate, [stale])


def test_a_reviewer_that_cannot_run_ships_the_notes_unreviewed():
    def time_out(*_a, **_kw):
        raise TimeoutError("gateway")
    lines = []
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=time_out, log=lines.append)
    assert receipt["verdict"] == "unreviewed"
    assert valid_receipt(current.model_dump_json().encode(), pdf, receipt)
    assert any("quality check could not run" in line and "TimeoutError" in line for line in lines)


def test_unfixed_render_problem_ships_with_the_issue_noted_even_if_model_says_passed():
    candidate = notes()
    candidate.sections[0].points = [r"$\unknowncommand{x}$"]
    lines = []
    _, _, receipt = finalize_notes_pdf(pdf_for(), candidate, language="english", render=pdf_for,
                                       generate=lambda *_: review(), log=lines.append)
    assert receipt["verdict"] == "shipped_with_issues" and receipt["open_issues"] == {"formula_render": 1}
    assert any("shipping with issues noted (formula_render x1)" in line for line in lines)


def test_persistent_quality_problem_ships_after_one_correction():
    issue = QualityIssue(page=1, kind="layout", detail="Clipped text")
    responses = iter([review([issue], [grammar_edit()]), review([issue])])
    current, _, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                             generate=lambda *_: next(responses))
    assert current.sections[0].points[0] == "The rows are horizontal."
    assert receipt["correction_passes"] == 1 and receipt["verdict"] == "shipped_with_issues"
    assert receipt["open_issues"] == {"layout": 1}


def test_finalized_cache_reuses_exact_bytes_and_rejects_tampering(tmp_path, monkeypatch):
    from sheets_bridge import output_cache
    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", str(tmp_path))
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for, generate=lambda *_: review())
    raw = current.model_dump_json().encode()
    assert output_cache.store_finalized("sample.json", raw, pdf, receipt)
    assert output_cache.load_finalized("sample.json", raw) == (pdf, receipt)
    assert output_cache.load_finalized("sample.json", raw + b" ") is None
    bundle_path = tmp_path / "sample.final.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["pdf"] = "dGFtcGVyZWQ="
    bundle_path.write_text(json.dumps(bundle))
    assert output_cache.load_finalized("sample.json", raw) is None


def test_runner_never_uploads_or_marks_done_without_quality_approval(monkeypatch, tmp_path):
    from sheets_bridge import output_cache
    from sheets_bridge.generator import GenerationResult
    from sheets_bridge.runner import Runner, RunOptions, OUTCOME_FAILED
    from test_sheets_bridge import FakeDrive, FakeSheet, _grid, _row, _notes_json
    monkeypatch.setenv("SHEETS_BRIDGE_CACHE", str(tmp_path))
    drive = FakeDrive()
    sheet = FakeSheet(_grid(_row(2)))
    runner = Runner(sheet, drive, RunOptions(), generate_fn=lambda *_a, **_kw: GenerationResult(
        pdf=b"%PDF unchecked", notes_json=_notes_json(), cost_inr=2), out=lambda *_: None)
    result = runner.run()
    assert result.outcomes[0].outcome == OUTCOME_FAILED
    assert result.outcomes[0].cost_inr == 2
    assert drive.pdf_uploads == [] and list(tmp_path.glob("*.json")) == []


def test_generation_failure_at_final_quality_keeps_the_correct_stage_and_cost(monkeypatch):
    from notesbot import final_quality
    from sheets_bridge import coverage, generator
    monkeypatch.setattr(coverage, "generate_complete_notes", lambda *_a, **_kw: notes())
    monkeypatch.setattr(generator, "_cost", lambda *_: 4.5)
    def reject(*_a, **_kw):
        raise NotesQualityError()
    monkeypatch.setattr(final_quality, "finalize_notes_pdf", reject)
    with pytest.raises(NotesQualityError) as caught:
        generator.generate_notes_pdf(pdf_for(), language="english", subject="Maths", signature="")
    assert caught.value.failed_stage == "quality"
    assert caught.value.partial_cost_inr == 4.5


# --------------------------------------------------------------------------- #
# A finished lecture must not be thrown away for something unfixable here
# --------------------------------------------------------------------------- #
def test_reported_duplicates_are_recorded_not_fatal():
    """A QualityEdit can only rewrite a string, so a repetition report had no
    possible fix and failed every lecture it appeared in. Repetition is already
    audited per page group and across the whole lecture before this point."""
    answer = review([QualityIssue(page=1, kind="repetition", detail="Bullet repeats the key term"),
                     QualityIssue(page=1, kind="clarity", detail="Could be shorter")])
    lines = []
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=lambda *_: answer, log=lines.append)
    assert receipt["advisory_issues"] == {"repetition": 1, "clarity": 1}
    assert valid_receipt(current.model_dump_json().encode(), pdf, receipt)
    assert any("style-level remarks" in line for line in lines)


@pytest.mark.parametrize("kind", ["missing_content", "layout", "unreadable", "excluded_content", "language"])
def test_a_flagged_pdf_still_ships_with_the_issue_noted_when_no_fix_is_offered(kind):
    answer = review([QualityIssue(page=1, kind=kind, detail="Real defect")])
    lines = []
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=lambda *_: answer, log=lines.append)
    assert valid_receipt(current.model_dump_json().encode(), pdf, receipt)
    assert receipt["verdict"] == "shipped_with_issues" and receipt["open_issues"] == {kind: 1}
    assert any(f"shipping with issues noted ({kind} x1)" in line for line in lines)


def test_second_pass_may_still_carry_style_remarks():
    answers = iter([review([QualityIssue(page=1, kind="grammar", detail="Agreement")], [grammar_edit()]),
                    review([QualityIssue(page=1, kind="repetition", detail="Still repeats")])])
    current, pdf, receipt = finalize_notes_pdf(pdf_for(), notes(), language="english", render=pdf_for,
                                               generate=lambda *_: next(answers))
    assert receipt["correction_passes"] == 1 and receipt["advisory_issues"] == {"repetition": 1}
    assert current.sections[0].points[0] == "The rows are horizontal."


def test_hindi_notes_ship_at_the_share_generation_actually_reaches():
    """Generation keeps its best answer even when the re-asks fall short, so a
    final gate at the same 0.90 target rejected lectures it had accepted."""
    from sheets_bridge.coverage import MIN_HINDI_SHARE, MIN_HINDI_SHARE_FINAL, notes_hindi_share

    assert MIN_HINDI_SHARE_FINAL < MIN_HINDI_SHARE

    # Hindi prose that keeps its English exam terms, like the real lectures
    mixed = ShortNotes(lecture_no="2", title="कोशिका", subject="जीव विज्ञान", sections=[
        Section(heading="कोशिका की संरचना", points=[
            "कोशिका जीवन की मूल इकाई है और इसमें केंद्रक उपस्थित होता है।",
            "माइटोकॉन्ड्रिया कोशिका में ऊर्जा बनाती है (mitochondria, powerhouse of the cell)।",
            "कोशिका भित्ति पादप कोशिका को दृढ़ता प्रदान करती है।"])])
    share = notes_hindi_share(mixed)
    assert MIN_HINDI_SHARE_FINAL <= share < MIN_HINDI_SHARE, share

    _, _, receipt = finalize_notes_pdf(pdf_for(), mixed, language="hindi", render=pdf_for,
                                       generate=lambda *_: review())
    assert receipt["status"] == "passed"


def test_notes_written_in_the_wrong_language_ship_with_a_language_remark():
    english_for_hindi = ShortNotes(lecture_no="2", title="Cell", subject="Biology", sections=[
        Section(heading="Cell structure", points=[
            "The cell is the basic unit of life and contains a nucleus.",
            "Mitochondria generate energy through cellular respiration."])])
    lines = []
    _, _, receipt = finalize_notes_pdf(pdf_for(), english_for_hindi, language="hindi", render=pdf_for,
                                       generate=lambda *_: review(), log=lines.append)
    assert receipt["verdict"] == "shipped_with_issues" and "language" in receipt["open_issues"]
    assert any("shipping with issues noted" in line for line in lines)



def test_the_quality_check_reads_only_the_rendered_notes():
    sent = []
    def reviewer(files, prompt, *_):
        sent.append(files)
        return review()
    source = pdf_for(pages=3)
    current, pdf, receipt = finalize_notes_pdf(source, notes(), language="english",
                                               render=pdf_for, generate=reviewer)
    assert len(sent) == 1 and len(sent[0]) == 1
    assert sent[0][0].data == pdf and sent[0][0].data != source
    assert receipt["source_sha256"]                      # the receipt still names the lecture
