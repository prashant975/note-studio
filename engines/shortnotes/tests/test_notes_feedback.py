"""Regressions from Notes __ AI.xlsx, Hindi and common English feedback."""
import io

import pytest
from PIL import Image
from pypdf import PdfReader

from notesbot.gemini_client import _ensure_mindmap, _section_has_theory
from notesbot.latex_repair import autowrap_math, repair_notes
from notesbot.math_render import normalize_latex, render_latex
from notesbot.matrix_render import render_expression
from notesbot.notes_pdf import NotesPDF, _split_math
from notesbot.prompts import Section, ShortNotes, Table, WorkedExample, build_prompt
from sheets_bridge.coverage import dedupe_sections, hindi_share, is_thin


def notes_for(section):
    return ShortNotes(lecture_no="1", subject="Maths", title="Matrices", sections=[section])


@pytest.mark.parametrize("kind", ["bmatrix", "pmatrix", "vmatrix", "Vmatrix", "matrix", "Bmatrix"])
def test_matrix_renders_with_two_rows_and_columns(kind):
    expression = rf"A=\begin{{{kind}}}1 & -2 \\ 3 & 4\end{{{kind}}}"
    png, width, height = render_latex(expression)
    one_row = render_latex(r"A=\begin{bmatrix}1 & -2 & 3 & 4\end{bmatrix}")
    assert width > 0 and height > one_row[2] * 1.5
    assert Image.open(io.BytesIO(png)).getbbox()
    assert r"\\" in normalize_latex(expression)


def test_matrix_compositor_preserves_exact_cell_order_and_signs():
    observed = []
    def record(value, size):
        observed.append(value)
        return render_latex(value, size)
    render_expression(r"A=\begin{bmatrix}a & -b \\ c & \frac{1}{2}\end{bmatrix}", 15, record, 300)
    assert observed == ["A=", "a", "-b", "c", r"\frac{1}{2}"]


@pytest.mark.parametrize("expression", [
    r"\left[\begin{array}{cc}1 & 2 \\ 3 & 4\end{array}\right]",
    r"\begin{aligned}A&=B \\ B&=C\end{aligned}",
    r"\begin{bmatrix}1&2\\3&4\end{bmatrix}+\begin{bmatrix}5&6\\7&8\end{bmatrix}=\begin{bmatrix}6&8\\10&12\end{bmatrix}",
])
def test_array_alignment_and_matrix_operations_render(expression):
    png, width, height = render_latex(expression)
    assert png.startswith(b"\x89PNG") and width > 5 and height > 15


@pytest.mark.parametrize("text", [r"$$A=B$$", r"\[A=B\]", r"\(A=B\)"])
def test_display_delimiters_do_not_leak_into_prose(text):
    assert _split_math(text) == [("math", "A=B")]
    assert autowrap_math(text) == "$A=B$"


def test_hindi_prose_wrapped_as_latex_returns_to_shaped_text():
    fixed = autowrap_math(r"$\text{यह आव्यूह का परिवर्त है} A^T$")
    assert _split_math(fixed) == [("text", "यह आव्यूह का परिवर्त है "), ("math", "A^T")]
    assert autowrap_math(fixed) == fixed
    bare = autowrap_math(r"दिया गया आव्यूह A=\begin{bmatrix}1&2\\3&4\end{bmatrix}")
    assert bare.startswith("दिया गया आव्यूह $A=")


def test_hindi_quality_check_ignores_matrix_commands():
    assert hindi_share(r"आव्यूह $\begin{bmatrix}\alpha & \beta \\ \gamma & \delta\end{bmatrix}$") == 1
    assert hindi_share("This is an English definition of matrix") == 0


@pytest.mark.parametrize("formula", [r"$i^{\text{th}}$", r"$\frac{1}{|A|}\text{adj}(A)$", r"$x_{\text{initial value}}$"])
def test_text_commands_inside_formulas_preserve_grouping(formula):
    assert autowrap_math(formula) == formula
    assert _split_math(formula) == [("math", formula[1:-1])]
    assert render_latex(formula[1:-1])[0].startswith(b"\x89PNG")


def test_deduplication_preserves_different_math_and_hindi_facts():
    sections = [Section(heading="Properties", points=["First", "Second", "First", "$A=B$", "$A=-B$", "$a=b$"]),
                Section(heading="Properties", points=["**First**", "Third"])]
    kept = dedupe_sections(sections)
    assert kept[0].points == ["First", "Second", "$A=B$", "$A=-B$", "$a=b$", "Third"]
    hindi = notes_for(Section(heading="आव्यूह", points=["पंक्तियाँ क्षैतिज होती हैं।", "स्तंभ ऊर्ध्वाधर होते हैं।"]))
    assert _section_has_theory(hindi.sections[0])
    _ensure_mindmap(hindi)
    assert hindi.mindmap and len(hindi.mindmap.branches[0].children) == 2


def test_related_types_and_different_tables_are_not_lost_in_merging():
    assert len(dedupe_sections([Section(heading="Symmetric matrix", points=["$A^T=A$"]),
                               Section(heading="Skew symmetric matrix", points=["$A^T=-A$"])])) == 2
    first = Section(heading="Properties", table=Table(headers=["A", "B"], rows=[["1", "2"]]))
    second = Section(heading="Properties", table=Table(headers=["A", "B"], rows=[["3", "4"]]))
    assert len(dedupe_sections([first, second])) == 2
    assert len(dedupe_sections([Section(heading="A", points=["First"]),
                               Section(heading="B", points=["Second"])])) == 2


def test_worked_examples_survive_repair_merge_coverage_and_optional_mindmap():
    example = WorkedExample(kind="proof", statement="Given invertible A, prove its inverse is unique.",
                            steps=["Assume $AB=BA=I$ and $AC=CA=I$.", r"\[B=B(AC)=(BA)C=C\]"],
                            result="The inverse is unique.")
    first = Section(heading="Inverse", worked_examples=[example])
    second = Section(heading="Inverse", points=["An inverse is unique."])
    notes = notes_for(first)
    notes.sections = dedupe_sections([first, second])
    repair_notes(notes)
    assert notes.sections[0].worked_examples[0].steps[1] == "$B=B(AC)=(BA)C=C$"
    assert not is_thin(notes, 1)
    _ensure_mindmap(notes)
    assert notes.mindmap is None and _section_has_theory(first)
    assert NotesPDF._is_wide(first)


def test_question_proof_steps_and_conclusion_render_in_order():
    notes = notes_for(Section(heading="Inverse", worked_examples=[WorkedExample(
        kind="proof", statement="Show that an inverse is unique.",
        steps=["Assume two inverses.", "Use associativity."], result="The two inverses coincide.")]))
    pdf = NotesPDF(notes, "", watermark=False)
    pdf.add_page()
    pdf.render_body()
    content = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(bytes(pdf.output()))).pages)
    markers = ["Theorem 1", "Show that", "Proof:", "1. Assume", "2. Use", "Conclusion:", "The two inverses"]
    positions = [content.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_long_proof_retains_every_numbered_step_across_pages():
    notes = notes_for(Section(heading="Long proof", worked_examples=[WorkedExample(
        kind="proof", statement="A long source proof.",
        steps=[f"Source step {index} preserves its given reasoning and equation $A=B$." for index in range(1, 56)],
        result="End of the source proof.")]))
    pdf = NotesPDF(notes, "", watermark=False)
    pdf.add_page()
    pdf.render_body()
    reader = PdfReader(io.BytesIO(bytes(pdf.output())))
    content = " ".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) >= 2
    assert all(f"{index}. Source step {index}" in content for index in range(1, 56))
    assert "End of the source proof." in content


def test_generation_cache_revision_prevents_reusing_old_notes(monkeypatch):
    from sheets_bridge import output_cache
    kwargs = dict(file_id="source", out_name="notes.pdf", language="hindi", subject="maths", signature="")
    current = output_cache.key(**kwargs)
    monkeypatch.setattr(output_cache, "GENERATION_REVISION", "older-prompt")
    assert current != output_cache.key(**kwargs)


@pytest.mark.parametrize("language", ["hindi", "english"])
def test_generation_request_keeps_definitions_and_excludes_worked_questions(language):
    prompt = build_prompt(language, "")
    assert "omit the question statement" not in prompt
    assert "Always leave worked_examples empty" in prompt
    assert "previous-class" in prompt
    assert "leave `mindmap` null" in prompt
    if language == "hindi":
        assert "विषम-सममित" in prompt and "Devanagari spelling" in prompt
