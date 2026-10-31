"""The model's own markup, repaired before it reaches a page.

Every case here is a line the content team found printed literally in the
notes (DCN/DPP/DMP issue list, October 2026): heading markers with extra
hashes, LaTeX for arrows and formulas, a markdown table typed as pipes, and a
flowchart typed with sideways arrows.
"""
from __future__ import annotations

import re
import zipfile

import pytest

from src.notes_markup import normalize_notes_markup


def _dcn(text: str) -> str:
    return normalize_notes_markup(text, dcn=True)[0]


@pytest.mark.parametrize("line, expected", [
    ("## H2: How Climate Change Can Manifest", "#H2: How Climate Change Can Manifest"),
    ("##H2: संयुक्त राष्ट्र की वित्तीय निर्भरता", "#H2: संयुक्त राष्ट्र की वित्तीय निर्भरता"),
    ("### H3 - Minor heading", "#H3: Minor heading"),
    ("**#H2: Bold wrapped**", "#H2: Bold wrapped"),
    ("#H2: words **bold**", "#H2: words **bold**"),
    ("## Plain markdown heading", "#H2: Plain markdown heading"),
    ("# TABLE_START", "#TABLE_START"),
    ("#H1: Already right", "#H1: Already right"),
])
def test_heading_markers_are_rewritten_for_the_dcn_writer(line, expected):
    assert _dcn(line) == expected


@pytest.mark.parametrize("line", [
    "- H2: is used as rocket fuel",          # chemistry in a bullet, not a heading
    "#MeToo movement reached India in 2018",  # a hashtag, not a heading
    "#FIGURE: slide_3_img",
    "#LECTURE_TITLE: Lecture 21",
])
def test_lines_that_are_not_headings_are_left_alone(line):
    assert _dcn(line) == line


def test_the_markdown_formats_get_markdown_headings():
    text, _ = normalize_notes_markup("#H2: Climate\n## H3: Weather", dcn=False)
    assert text == "## Climate\n### Weather"


@pytest.mark.parametrize("tex, expected", [
    ("liquid oxygen ($LOX$) as oxidizer", "liquid oxygen (LOX) as oxidizer"),
    (r"detonates $\rightarrow$ generates heat", "detonates → generates heat"),
    (r"$H_2O$ and $CO_{2}$", "H₂O and CO₂"),
    (r"$10^{-3}$ m", "10⁻³ m"),
    (r"a rise of $1.5^\circ C$", "a rise of 1.5°C"),
    (r"$\ce{CH4 + 2O2 -> CO2 + 2H2O}$", "CH₄ + 2O₂ → CO₂ + 2H₂O"),
    (r"$\Delta T \geq 2$", "Δ T ≥ 2"),
    (r"bare \rightarrow arrow", "bare → arrow"),
    (r"$\frac{a}{b}$", "a/b"),
])
def test_latex_becomes_plain_characters(tex, expected):
    assert _dcn(tex) == expected


@pytest.mark.parametrize("tex, expected", [
    (r"$\frac{a+b}{c+d}$", "(a+b)/(c+d)"),
    (r"$\frac{a}{b+c}$", "a/(b+c)"),
    (r"$\frac{1}{\frac{2}{3}}$", "1/(2/3)"),
    (r"$\frac{\frac{a}{b}}{c}$", "(a/b)/c"),
    (r"$\frac{x_{1}+y_{2}}{z_{3}}$", "(x₁+y₂)/z₃"),
    (r"$\dfrac{\Delta GDP}{GDP}\times100$", "(Δ GDP)/GDP×100"),
    (r"$\frac{2a}{3b}$", "(2a)/(3b)"),
])
def test_fraction_conversion_preserves_grouping(tex, expected):
    once = _dcn(tex)
    assert once == expected
    assert _dcn(once) == once


@pytest.mark.parametrize("money", [
    "Worth $5 billion and $7 billion.",
    "US$ 4 trillion economy",
    "costs between $10–$20 a barrel",
])
def test_dollar_amounts_are_not_mistaken_for_math(money):
    assert _dcn(money) == money


def test_a_markdown_table_is_fenced_for_the_dcn_writer():
    text = _dcn("\n".join([
        "- परमाणु-सक्षम SLBM दूसरी प्रहार क्षमता देती है।",
        "| मिसाइल | लगभग रेंज | प्रमुख तथ्य |",
        "|---|---:|---|",
        "| K-15 | लगभग 750 किमी | समुद्र से प्रक्षेपित |",
        "| K-4 | लगभग 3,500 किमी | लंबी दूरी की SLBM |",
        "- K-15 को सागरिका नाम से जाना जाता है।",
    ]))
    lines = text.split("\n")
    start, end = lines.index("#TABLE_START"), lines.index("#TABLE_END")
    assert end - start == 4                       # header + two rows, rule row dropped
    assert not any("---" in line for line in lines)
    assert lines[0].startswith("- ") and lines[-1].startswith("- ")


def test_a_fenced_table_is_not_fenced_twice():
    text = "#TABLE_START\n| a | b |\n|---|---|\n| c | d |\n#TABLE_END"
    assert _dcn(text).count("#TABLE_START") == 1


def test_an_arrow_typed_flow_becomes_steps():
    text = _dcn("\n".join([
        "#FLOW_START",
        "Concentration of all state powers in a single authority",
        "→ High risk of authoritarianism",
        "→ Safe and balanced democratic governance",
        "#FLOW_END",
    ]))
    assert text.split("\n") == [
        "#FLOW_START",
        "Concentration of all state powers in a single authority",
        "↓",
        "High risk of authoritarianism",
        "↓",
        "Safe and balanced democratic governance",
        "#FLOW_END",
    ]


def test_a_well_formed_flow_is_untouched():
    text = "#FLOW_START\nA\n↓\nB\n#FLOW_END"
    assert _dcn(text) == text


def test_the_repair_is_idempotent_and_reports_what_it_did():
    raw = "## H2: X\n$\\rightarrow$\n| a | b |\n|---|---|\n| c | d |"
    once, repairs = normalize_notes_markup(raw, dcn=True)
    twice, again = normalize_notes_markup(once, dcn=True)
    assert once == twice and again == []
    assert len(repairs) == 3


def test_the_dcn_writer_never_prints_the_model_markup(tmp_path):
    from src.upsc_dcn_docx_writer import write_upsc_dcn_docx

    notes = "\n".join([
        "#LECTURE_TITLE: Lecture 21 Climate Change",
        "#H1: Climate Change",
        "## H2: How Climate Change Can Manifest",
        "- liquid oxygen ($LOX$) as oxidizer; detonates $\\rightarrow$ heat",
        "| Missile | Range |",
        "|---|---:|",
        "| K-15 | 750 km |",
    ])
    out, _ = write_upsc_dcn_docx(notes, tmp_path / "n.docx", add_footer_strip=False)
    with zipfile.ZipFile(out) as z:
        body = re.sub(r"<[^>]+>", "", z.read("word/document.xml").decode("utf-8"))
    for leaked in ("##", "H2:", "$", "\\rightarrow", "|---"):
        assert leaked not in body, leaked
    assert "How Climate Change Can Manifest" in body and "LOX" in body and "→" in body
    with zipfile.ZipFile(out) as z:
        assert "<w:tbl>" in z.read("word/document.xml").decode("utf-8")
