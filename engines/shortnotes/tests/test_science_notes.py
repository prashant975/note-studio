"""Regressions from Foundation motion/optics feedback, shared with NEET/JEE."""
import io

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from notesbot import generation_profile
from notesbot.gemini_client import _generation_schema_json, _section_has_theory
from notesbot.latex_repair import autowrap_math, repair_notes
from notesbot.math_render import normalize_latex, render_latex
from notesbot.notes_pdf import build_notes_pdf, _split_math
from notesbot.prompts import (
    Curve, Graph, Schematic, Section, ShortNotes, build_prompt,
)
from sheets_bridge.coverage import dedupe_sections
from sheets_bridge.generator import notes_from_json


@pytest.mark.parametrize("raw,power", [
    ("m s -1", "1"), ("ms^-1", "1"), ("m s −2", "2"),
    ("m s^{-2}", "2"), ("m s⁻²", "2"), ("km h –1", "1"),
])
def test_units_are_one_math_atom_with_attached_exponent(raw, power):
    fixed = autowrap_math("SI unit: " + raw + ".")
    atoms = _split_math(fixed)
    assert atoms[0] == ("text", "SI unit: ")
    assert atoms[1][0] == "math" and atoms[1][1].endswith("^{-" + power + "}")
    assert len(atoms) == 3
    assert autowrap_math(fixed) == fixed
    assert render_latex(atoms[1][1])[0].startswith(b"\x89PNG")


@pytest.mark.parametrize("text", ["mass -1", "items -2", "$s-1$", "$m-s-1$", "The graph has a negative slope.", "$Ca^{2+}$"])
def test_notation_repair_does_not_reinterpret_subtraction_or_prose(text):
    assert autowrap_math(text) == text


def test_word_fraction_spaces_survive_mathtext_and_repeated_repairs():
    raw = r"$\frac{totaldistancetravelled}{timeinterval}$"
    repaired = autowrap_math(raw)
    assert r"\mathrm{total distance travelled}" in repaired
    assert r"\mathrm{time interval}" in repaired
    assert autowrap_math(repaired) == repaired
    normalized = normalize_latex(repaired[1:-1])
    assert r"total\ distance\ travelled" in normalized
    assert r"time\ interval" in normalized
    assert normalize_latex(normalized) == normalized
    spaced = render_latex(normalized)
    collapsed = render_latex(r"\frac{\mathrm{totaldistancetravelled}}{\mathrm{timeinterval}}")
    assert spaced[1] > collapsed[1] + 5


@pytest.mark.parametrize("category", ["foundation", "neet_jee", "neet", "jee"])
@pytest.mark.parametrize("lean", [True, False])
def test_shared_instructions_and_diagram_schema_reach_every_category(category, lean):
    with generation_profile.exam_category(category), generation_profile.lean_requests(lean):
        prompt = build_prompt("english", "", subject_hint="Physics")
        schema = _generation_schema_json(ShortNotes)
    assert "<<SCIENCE_QUALITY>>" not in prompt
    assert "Retardation requires acceleration opposite to velocity" in prompt
    assert "Word-form fractions must preserve spaces" in prompt
    assert "schematic" in schema["$defs"]["Section"]["properties"]
    assert "DiagramPoint" in schema["$defs"]


def reflection_diagram():
    return Schematic.model_validate({
        "caption": "Reflection at a plane mirror",
        "strokes": [
            {"points": [{"x": 10, "y": 78}, {"x": 90, "y": 78}]},
            {"dashed": True, "points": [{"x": 50, "y": 12}, {"x": 50, "y": 78}]},
            {"kind": "arrow", "points": [{"x": 15, "y": 20}, {"x": 50, "y": 78}]},
            {"kind": "arrow", "points": [{"x": 50, "y": 78}, {"x": 85, "y": 20}]},
        ],
        "labels": [
            {"x": 15, "y": 7, "text": "Incident ray"},
            {"x": 85, "y": 7, "text": "Reflected ray"},
            {"x": 50, "y": 1, "text": "Normal"},
            {"x": 50, "y": 92, "text": "Plane mirror"},
            {"x": 41, "y": 48, "text": "$i$"},
            {"x": 59, "y": 48, "text": "$r$"},
        ],
    })


def science_fixture():
    return ShortNotes(lecture_no="1", subject="Physics", title="Motion and light - formatting check", sections=[
        Section(heading="Average speed", points=[
            "Average speed is total distance travelled divided by the time interval.",
            r"$v_{av}=\frac{totaldistancetravelled}{timeinterval}$",
            "Speed and velocity units: m s -1 (or km h^-1).",
            "Acceleration unit: m s −2.",
        ]),
        Section(heading="Position-time graphs", points=[
            "The slope of a position-time graph gives **velocity**.",
            "A horizontal line represents rest; a changing slope represents changing velocity.",
        ], graph=Graph(x_label="Time", y_label="Position", x_min=0, x_max=1, qualitative=True, curves=[
            Curve(label="At rest", expression="0.25"),
            Curve(label="Constant velocity", expression="x"),
            Curve(label="Changing velocity", expression="x**2"),
        ])),
        Section(heading="Velocity-time graphs", points=[
            "The slope gives **acceleration**; signed area gives displacement.",
            "Negative acceleration means the chosen negative direction; retardation requires acceleration opposite to velocity.",
        ], graph=Graph(x_label="Time", y_label="Velocity", x_min=0, x_max=1, qualitative=True, curves=[
            Curve(label="Positive acceleration", expression="x"),
            Curve(label="Zero acceleration", expression="0.5"),
            Curve(label="Negative acceleration", expression="1-x"),
        ])),
        Section(heading="Laws of Reflection", points=[
            "The angle of incidence equals the angle of reflection: $i=r$.",
            "The incident ray, reflected ray and normal lie in the same plane.",
        ], schematic=reflection_diagram()),
    ])


def test_schematic_survives_cache_deduplication_and_pdf_rendering():
    notes = science_fixture()
    repair_notes(notes)
    restored = notes_from_json(notes.model_dump_json().encode())
    assert restored.sections[-1].schematic == notes.sections[-1].schematic
    assert _section_has_theory(Section(heading="Reflection", schematic=reflection_diagram()))
    first = Section(heading="Same", points=["One"], schematic=reflection_diagram())
    second = first.model_copy(deep=True)
    second.schematic.labels[0].text = "Different source label"
    assert len(dedupe_sections([first, second])) == 2
    pdf = build_notes_pdf(restored, "", watermark=False)
    reader = PdfReader(io.BytesIO(pdf))
    text = " ".join(page.extract_text() for page in reader.pages)
    for label in ("Incident ray", "Reflected ray", "Normal", "Plane mirror"):
        assert label in text
    assert "\\frac" not in text and "m s -1" not in text
    # The short graph section moves with its heading, rather than starting
    # at the foot of page 1 with its figure alone on the next page.
    assert "VELOCITY-TIME GRAPHS" not in reader.pages[0].extract_text()
    assert "VELOCITY-TIME GRAPHS" in reader.pages[1].extract_text()


def test_schematic_rejects_nonfinite_and_out_of_bounds_coordinates():
    for x in (-1, 101, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Schematic.model_validate({"strokes": [{"points": [{"x": x, "y": 0}, {"x": 50, "y": 50}]}]})


def test_qualitative_graph_hides_only_numeric_ticks(monkeypatch):
    from matplotlib.axes import Axes
    from notesbot.graph_render import render_graph
    seen = []
    original = Axes.set_xticks
    def record(self, ticks, *args, **kwargs):
        seen.append(list(ticks))
        return original(self, ticks, *args, **kwargs)
    monkeypatch.setattr(Axes, "set_xticks", record)
    graph = Graph(x_label="Time", y_label="Position", qualitative=True, curves=[Curve(expression="x")])
    assert render_graph(graph)[0].startswith(b"\x89PNG")
    assert [] in seen
    seen.clear()
    graph.qualitative = False
    render_graph(graph)
    assert [] not in seen
