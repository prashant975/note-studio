"""Regressions for the covalent-bond legend and lead chloride state feedback."""
import io

import pytest
from pypdf import PdfReader

from notesbot import generation_profile
from notesbot.chemistry_repair import chemistry_findings, normalize_chemistry_notation
from notesbot.final_quality import local_findings
from notesbot.latex_repair import autowrap_math, repair_notes
from notesbot.notes_pdf import _plain, _split_math, build_notes_pdf
from notesbot.prompts import Compare, Section, ShortNotes, Table, build_prompt
from sheets_bridge.generator import notes_from_json


BOND = "Double Covalent Bond: Sharing two pairs of electrons in an $O_2$ molecule ($= O$) completes the octet."
REACTION = r"$Pb(s) + CuCl_2(aq) \rightarrow PbCl_2(aq) + Cu(s)$"


@pytest.mark.parametrize("symbol", ["$= O$", r"$=\mathrm{O}$", "$(=O)$", "(= O)"])
def test_extra_oxygen_is_removed_only_from_the_standalone_bond_legend(symbol):
    raw = BOND.replace("$= O$", symbol)
    fixed = autowrap_math(raw)
    assert "O_2" in fixed and "octet" in fixed
    assert "=O" not in fixed.replace(" ", "")
    assert any(kind == "math" and "=" in value for kind, value in _split_math(fixed)) or "(=)" in fixed
    assert autowrap_math(fixed) == fixed
    assert chemistry_findings(raw) and not chemistry_findings(fixed)


@pytest.mark.parametrize("equation", [
    REACTION,
    r"$Pb(s)+CuCl_{2}(aq)\longrightarrow PbCl_{2}(aq)+Cu(s)$",
    r"$\mathrm{Pb}(s)+\mathrm{CuCl}_{2}(aq)\to\mathrm{PbCl}_{2}(aq)+\mathrm{Cu}(s)$",
    r"$\mathrm{Pb(s)}+\mathrm{CuCl_2(aq)}\rightarrow\mathrm{PbCl_2(aq)}+\mathrm{Cu(s)}$",
    r"$Pb(s)+CuCl_2(\mathrm{aq})\rightarrow PbCl_2(\mathrm{aq})+Cu(s)$",
    "Pb(s) + CuCl₂(aq) → PbCl₂(aq) + Cu(s)",
    "Pb(s) + CuCl2(aq) -> PbCl2(aq) + Cu(s)",
    r"$CuCl_2(aq)+Pb(s)\rightarrow Cu(s)+PbCl_2(aq)$",
    r"$Pb(s)$ + $CuCl_2(aq)$ $\rightarrow$ $PbCl_2(aq)$ + $Cu(s)$",
])
def test_only_the_lead_chloride_product_state_changes(equation):
    fixed = normalize_chemistry_notation(equation)
    # This reaction has two aq labels; only the second (product) changes.
    product = equation.rfind("aq")
    assert fixed == equation[:product] + "s" + equation[product + 2:]
    assert normalize_chemistry_notation(fixed) == fixed
    assert chemistry_findings(equation) and not chemistry_findings(fixed)


@pytest.mark.parametrize("text", [
    "Double Covalent Bond: an $O_2$ molecule has the structure $O=O$.",
    "Double Covalent Bond: a carbonyl structure is $C=O$ or $C(=O)O$.",
    "Carbonyl groups contain a double bond (=O).",
    "$x=O$", "An oxygen label: $=O$.",
    r"$PbCl_2(s)\rightleftharpoons Pb^{2+}(aq)+2Cl^{-}(aq)$",
    r"$PbCl_2(aq)\rightarrow Pb^{2+}(aq)+2Cl^{-}(aq)$",
    "Lead chloride dissolves in hot water: $PbCl_2(aq)$.",
    r"$Zn(s)+CuCl_2(aq)\rightarrow ZnCl_2(aq)+Cu(s)$",
    r"$Pb(s)+CuCl_2(aq)\rightarrow PbCl_2(s)+Cu(s)$",
    r"$Pb(s)+CuCl_2(aq)\rightleftharpoons PbCl_2(aq)+Cu(s)$",
    r"$Pb(s)+CuCl_2(aq) {\rightarrow}^{heat} PbCl_2(aq)+Cu(s)$",
])
def test_other_structures_equations_and_solution_conditions_are_preserved(text):
    assert normalize_chemistry_notation(text) == text
    assert not chemistry_findings(text)


def chemistry_notes():
    return ShortNotes(lecture_no="1", subject="Chemistry", title="Covalent bonds and displacement", sections=[
        Section(heading="Double Covalent Bond", points=[BOND]),
        Section(heading="Lead displaces copper", points=[REACTION]),
    ])


def test_json_and_all_theory_fields_receive_the_shared_repairs():
    candidate = chemistry_notes()
    section = candidate.sections[0]
    section.notes = [BOND]
    section.tricks = [BOND]
    section.key_terms = [BOND]
    section.flow = [BOND]
    section.table = Table(headers=["Reaction"], rows=[[REACTION]])
    section.compare = Compare(left_title="Bond", right_title="Reaction", left_points=[BOND], right_points=[REACTION])
    loaded = notes_from_json(candidate.model_dump_json().encode())
    # The cached JSON loader preserves content; the shared render preparation
    # invokes repair_notes before writing a new PDF.
    repair_notes(loaded)
    assert "$=$" in loaded.sections[0].points[0]
    assert "PbCl_2(s)" in loaded.sections[1].points[0]
    for field in ("notes", "tricks", "key_terms", "flow"):
        assert "$=$" in getattr(loaded.sections[0], field)[0]
    assert "PbCl_2(s)" in loaded.sections[0].table.rows[0][0]
    assert "$=$" in loaded.sections[0].compare.left_points[0]
    assert "PbCl_2(s)" in loaded.sections[0].compare.right_points[0]
    before = loaded.model_dump()
    repair_notes(loaded)
    assert loaded.model_dump() == before


@pytest.mark.parametrize("lean", [False, True])
@pytest.mark.parametrize("category", ["foundation", "neet_jee"])
def test_generation_and_review_share_the_explicit_chemistry_corrections(lean, category):
    from notesbot.final_quality import _PROMPT
    from notesbot.source_coverage import _AUDIT
    with generation_profile.lean_requests(lean), generation_profile.exam_category(category):
        prompt = build_prompt("english", "", subject_hint="Chemistry")
    for text in (prompt, _PROMPT, _AUDIT):
        assert "PbCl_2(s)" in text
        assert "standalone double-covalent-bond symbol" in text
        assert "explicit exceptions to source-only" in text


def test_local_review_detects_semantic_errors_even_when_the_formulas_render():
    candidate = chemistry_notes()
    output = build_notes_pdf(candidate, "", watermark=False)
    _, issues = local_findings(candidate, output, "english")
    chemistry = [issue for issue in issues if "Chemistry error at" in issue.detail]
    assert len(chemistry) == 2
    repair_notes(candidate)
    assert not local_findings(candidate, output, "english")[1]


def test_direct_render_and_fallback_use_the_corrected_formulas_without_model_qc(monkeypatch):
    from notesbot import math_render
    from notesbot.notes_pdf import _latex_fallback
    candidate = chemistry_notes()
    original = candidate.model_dump()
    # Inspect the exact expressions sent to the normal math renderer.
    rendered = []
    original_render = math_render.render_latex
    def capture(expression, *args, **kwargs):
        rendered.append(expression)
        return original_render(expression, *args, **kwargs)
    monkeypatch.setattr(math_render, "render_latex", capture)
    output = build_notes_pdf(candidate, "", watermark=False)
    assert candidate.model_dump() == original
    assert "=" in rendered and "= O" not in rendered
    assert any("PbCl_2(s)" in value for value in rendered)
    assert "$" not in _plain(BOND) and "(=)" in _plain(BOND)
    assert "PbCl_2(s)" in _latex_fallback(_split_math(REACTION)[0][1])

    def force_fallback(*_args, **_kwargs):
        raise ValueError("Exercise the readable fallback")
    monkeypatch.setattr(math_render, "render_latex", force_fallback)
    output = build_notes_pdf(candidate, "", watermark=False)
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(output)).pages)
    assert "PbCl2(s)" in text.replace(" ", "")
    assert "PbCl2(aq)" not in text.replace(" ", "")
    assert "(=)" in text.replace(" ", "")
