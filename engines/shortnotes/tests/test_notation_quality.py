"""Real alloy/temperature defects reported in the mixtures notes PDF."""
import io

import pytest
from pypdf import PdfReader

from notesbot.errors import NotesQualityError
from notesbot.final_quality import local_findings
from notesbot.latex_repair import autowrap_math, notation_artifacts, repair_notes
from notesbot.math_render import normalize_latex, render_latex
from notesbot.notes_pdf import NotesPDF, _latex_fallback, _plain, build_notes_pdf, rendered_notation_findings
from notesbot.prompts import Compare, Section, ShortNotes, Table


ALLOY_FORMULAS = [
    r"80\%\text{ Cu},20\%\text{ Zn}",
    r"80\%\text{ Cu},20\%\text{ Sn}",
    r"\text{Fe}+\text{C}(0.03\text{--}0.8\%),\text{Cr}(16\text{--}18\%),"
    r"\text{Ni}(10.0\text{--}14.0\%),\text{Mo}(2.0\text{--}3.0\%)",
]
TEMPERATURE_FORMULA = r"\ge 25\text{ }^\circ\text{C}"


def mixtures_notes():
    return ShortNotes(lecture_no="1", subject="Science",
                      title="Exploring Mixtures and their Separation", sections=[
        Section(heading="Alloy compositions", table=Table(
            headers=["Alloy", "Typical Composition"], rows=[
                [name, "approx $" + formula + "$"]
                for name, formula in zip(["Brass", "Bronze", "Stainless Steel"], ALLOY_FORMULAS)
            ])),
        Section(heading="Distillation and fractional distillation", points=[
            "Distillation recovers a liquid by vaporising it and condensing the vapour.",
            "Simple distillation is used when boiling points differ by **at least 25 °C**.",
            r"Acetone boils at $56^\circ C$ and water at $100^\circ C$.",
            "Fractional distillation is used when the difference is **less than 25 degC**.",
        ], key_terms=["distillation : vaporising a liquid and condensing its vapour"], compare=Compare(
            left_title="Simple distillation", right_title="Fractional distillation",
            left_points=["Boiling point difference $" + TEMPERATURE_FORMULA + "$"],
            right_points=[r"Boiling point difference $<25^\circ\mathrm{C}$"],
        )),
    ])


@pytest.mark.parametrize("formula", ALLOY_FORMULAS + ["80% Cu, 20% Zn"])
def test_alloy_percentages_render_and_remain_escaped(formula):
    fixed = normalize_latex(formula)
    assert r"\%" in fixed and normalize_latex(fixed) == fixed
    png, width, height = render_latex(formula)
    assert png.startswith(b"\x89PNG") and width > 20 and height > 5


@pytest.mark.parametrize("operator,canonical", [(r"\ge", r"\geq"), (r"\le", r"\leq")])
def test_tex_comparison_aliases_render(operator, canonical):
    formula = operator + r"25^\circ\mathrm{C}"
    assert canonical in normalize_latex(formula)
    assert render_latex(formula)[0].startswith(b"\x89PNG")


@pytest.mark.parametrize("temperature", ["25 °C", "25°C", "25 ° C", "25 degC", "25 degrees C", "25℃"])
def test_celsius_in_prose_keeps_real_degree_symbol(temperature):
    fixed = autowrap_math("At least " + temperature + ".")
    assert fixed == "At least 25 °C."
    assert autowrap_math(fixed) == fixed
    notes = ShortNotes(lecture_no="1", subject="Science", title="Temperature",
                       sections=[Section(heading="Distillation", points=[fixed])])
    output = build_notes_pdf(notes, "", watermark=False)
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(output)).pages)
    assert "25 °C" in text and "degC" not in text


def test_fallback_preserves_percentages_labels_degree_and_comparison():
    assert _latex_fallback(ALLOY_FORMULAS[0]) == "80% Cu, 20% Zn"
    assert _latex_fallback(TEMPERATURE_FORMULA).replace(" ", "") == ">=25°C"
    assert not notation_artifacts(_latex_fallback(ALLOY_FORMULAS[2]))
    assert _plain(r"Threshold $\ge25^\circ\mathrm{C}$") == "Threshold >=25°C"


def test_long_alloy_formula_wraps_between_components_without_losing_values():
    chunks = NotesPDF._latex_wrap_chunks(ALLOY_FORMULAS[2])
    assert len(chunks) == 4
    assert "".join(chunks) == ALLOY_FORMULAS[2]
    for chunk in chunks:
        assert render_latex(chunk)[0].startswith(b"\x89PNG")


def test_comma_wrapping_preserves_function_arguments_and_text_groups():
    formula = r"f(x,y),\text{Cu, Zn},\frac{a,b}{c}"
    assert NotesPDF._latex_wrap_chunks(formula) == [r"f(x,y),", r"\text{Cu, Zn},", r"\frac{a,b}{c}"]


def test_alloy_range_dash_is_one_dash_and_keeps_both_endpoints():
    fixed = normalize_latex(ALLOY_FORMULAS[2])
    assert "--" not in fixed and "–" in fixed
    assert "0.03" in fixed and "0.8" in fixed and r"\%" in fixed


def test_reported_pdf_renders_without_fallback_and_preserves_all_source_strings(monkeypatch):
    from notesbot import notes_pdf
    candidate = mixtures_notes()
    repair_notes(candidate)
    before = candidate.model_dump()
    def unexpected_fallback(_):
        pytest.fail("The reported formulas must render without falling back to prose")
    monkeypatch.setattr(notes_pdf, "_latex_fallback", unexpected_fallback)
    output = build_notes_pdf(candidate, "", watermark=False)
    assert candidate.model_dump() == before
    assert rendered_notation_findings(output) == []
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(output)).pages)
    assert "Brass" in text and "Bronze" in text and "Stainless Steel" in text
    assert "25 °C" in text and "degC" not in text


def test_renderer_failure_uses_readable_alloy_and_temperature_fallback(monkeypatch):
    from notesbot import math_render
    def failed_renderer(*_):
        raise ValueError("Forced fallback")
    monkeypatch.setattr(math_render, "render_latex", failed_renderer)
    output = build_notes_pdf(mixtures_notes(), "", watermark=False)
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(output)).pages)
    for token in ("80%", "20%", "Cu", "Zn", "Sn", "Fe", "Cr", "Ni", "Mo", ">=", "25", "°C"):
        assert token in text
    assert rendered_notation_findings(output) == []


@pytest.mark.parametrize("artifact", ["80%text Cu", "textFe", "25 degC", "ge 25text circtextC"])
def test_local_quality_check_reports_artifacts_on_the_actual_pdf_page(artifact):
    from test_final_quality import pdf_for
    notes = mixtures_notes()
    notes.sections[0].points = [artifact]
    _, issues = local_findings(notes, pdf_for(notes), "english")
    assert any(item.page == 1 and item.kind == "formula_render" for item in issues)


def test_delivery_check_rejects_printed_command_debris():
    notes = mixtures_notes()
    notes.sections[0].points = ["Composition: 80%text Cu"]
    with pytest.raises(NotesQualityError):
        build_notes_pdf(notes, "", watermark=False)


@pytest.mark.parametrize("text", [
    "The textbook contains text and context.", "The degree of a polynomial is two.",
    "Use C for carbon and Fe for iron.", "A percentage is 80% of the total.",
])
def test_notation_check_does_not_flag_ordinary_prose(text):
    assert not notation_artifacts(text)

