"""Regression tests for the concise-notes visual identity.

These lock the formatting described by the reference document
"Basic Maths & Calculus-02_Concise Notes.docx": A4 portrait, 1 in margins,
Kalam typography, the red/green/blue hierarchy, PW furniture, and the rule that
mathematical glyphs fall back to the maths font instead of vanishing.

The notes CONTENT changes per subject; the formatting must not. Every test here
therefore uses generic content and asserts only on the template.
"""
from __future__ import annotations

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX

from src.docx_writer import write_notes_docx
from src.models import SlideData
from src.quality_checker import check_docx_formatting
from src.style_config import (
    BODY_COLOR,
    BODY_LINE_SPACING,
    BODY_SIZE_PT,
    BULLET_LEFT_INDENT_IN,
    FONT_MATH,
    FONT_PRIMARY,
    HEADING_COLOR,
    LECTURE_TITLE_SIZE_PT,
    SECTION_HEADING_SIZE_PT,
    SUBJECT_SIZE_PT,
    TITLE_COLOR,
    USABLE_WIDTH_IN,
)

NOTES = """Concepts Covered in the Class:
• Angle and its measurement
• Trigonometric ratios

Trigonometric Ratio
• The ratio of two sides of a right triangle for an angle theta.
• Standard identity: [[MATH_INLINE: \\sin^2\\theta + \\cos^2\\theta = 1]]
• Vectors keep their arrows: [[MATH_INLINE: \\vec{A} \\times \\vec{B}]]

Graph
• The sine curve repeats every 2π radians.
"""


def _render(tmp_path, *, subject="Physics", chapter="Basic Maths & Calculus-02", notes=NOTES,
            slides=None):
    out, warnings = write_notes_docx(
        notes, tmp_path / "notes.docx", slides or [],
        run_dir=tmp_path, subject=subject, chapter_title=chapter,
    )
    return Document(str(out)), warnings, out


def _runs(doc):
    for para in doc.paragraphs:
        for run in para.runs:
            if (run.text or "").strip():
                yield para, run


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def test_page_is_a4_portrait_with_one_inch_margins(tmp_path):
    doc, _, _ = _render(tmp_path)
    section = doc.sections[0]
    assert round(section.page_width.inches, 2) == 8.27
    assert round(section.page_height.inches, 2) == 11.69
    assert section.page_height > section.page_width          # portrait
    for margin in (section.top_margin, section.bottom_margin,
                   section.left_margin, section.right_margin):
        assert round(margin.inches, 2) == 1.00
    assert round(section.header_distance.inches, 2) == 0.20
    assert round(section.footer_distance.inches, 2) == 0.12


def test_usable_text_column_is_six_point_two_seven_inches():
    assert round(USABLE_WIDTH_IN, 2) == 6.27


# --------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------
@pytest.mark.parametrize("subject", ["Physics", "Chemistry", "Mathematics", "Biology"])
def test_subject_title_is_dynamic_and_styled(tmp_path, subject):
    doc, _, _ = _render(tmp_path, subject=subject)
    para = doc.paragraphs[0]
    run = para.runs[0]
    assert run.text == subject                       # never hard-coded to Physics
    assert para.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert run.font.size.pt == SUBJECT_SIZE_PT
    assert run.font.bold and run.font.underline
    assert run.font.color.rgb == TITLE_COLOR
    assert run.font.name == FONT_PRIMARY


def test_chapter_title_is_24pt_red_centered(tmp_path):
    doc, _, _ = _render(tmp_path, chapter="Cell - The Unit of Life 02")
    para = doc.paragraphs[1]
    assert "".join(r.text for r in para.runs) == "Cell - The Unit of Life 02"
    assert para.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert para.runs[0].font.size.pt == LECTURE_TITLE_SIZE_PT
    assert para.runs[0].font.bold and para.runs[0].font.underline
    assert para.runs[0].font.color.rgb == TITLE_COLOR


# --------------------------------------------------------------------------
# Headings and body
# --------------------------------------------------------------------------
def _heading_para(doc, text):
    for para in doc.paragraphs:
        if "".join(r.text for r in para.runs).strip() == text:
            return para
    raise AssertionError(f"heading {text!r} not found")


@pytest.mark.parametrize("heading", ["Concepts Covered in the Class:", "Trigonometric Ratio", "Graph"])
def test_section_headings_are_green_highlighted_black_18pt(tmp_path, heading):
    doc, _, _ = _render(tmp_path)
    para = _heading_para(doc, heading)
    assert para.alignment == WD_ALIGN_PARAGRAPH.LEFT
    assert para.paragraph_format.keep_with_next is True      # no orphan headings
    for run in para.runs:
        assert run.font.size.pt == SECTION_HEADING_SIZE_PT
        assert run.font.bold and run.font.underline
        assert run.font.color.rgb == HEADING_COLOR
        assert run.font.highlight_color == WD_COLOR_INDEX.BRIGHT_GREEN


def test_body_bullets_are_16pt_dark_blue_justified(tmp_path):
    doc, _, _ = _render(tmp_path)
    bullets = [p for p in doc.paragraphs
               if "".join(r.text for r in p.runs).startswith("•")]
    assert bullets, "no bullets rendered"
    # Justified is the template default; a bullet only drops to left when
    # justifying it would open holes (see the alignment tests below).
    assert any(p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY for p in bullets)
    for para in bullets:
        assert para.alignment in (WD_ALIGN_PARAGRAPH.JUSTIFY, WD_ALIGN_PARAGRAPH.LEFT)
        assert round(para.paragraph_format.line_spacing, 2) == BODY_LINE_SPACING
        assert para.paragraph_format.space_after.pt == 0
        assert round(para.paragraph_format.left_indent.inches, 3) == BULLET_LEFT_INDENT_IN
        assert round(para.paragraph_format.first_line_indent.inches, 3) == -BULLET_LEFT_INDENT_IN
        for run in para.runs:
            assert run.font.size.pt == BODY_SIZE_PT
            assert run.font.color.rgb == BODY_COLOR
            assert run.font.highlight_color is None


def test_a_bullet_that_would_justify_badly_falls_back_to_left(tmp_path):
    """Spec: keep the justified look, but not at the cost of huge word gaps."""
    ugly = "• Phasor diagrams represent sinusoidal quantities graphically."
    fine = ("• Widely applied in Alternating Current (A/C), Simple Harmonic Motion (SHM), "
            "Wave Motion, Wave Optics, Electromagnetic (EM) Waves, and Vectors.")
    doc, _, _ = _render(tmp_path, notes=f"Phasors\n{ugly}\n{fine}\n")
    # The rendered bullet is "•\ttext", so match on the wording, not the marker.
    aligns = {" ".join(p.text.split()): p.alignment
              for p in doc.paragraphs if p.text.startswith("•")}
    assert aligns["• Phasor diagrams represent sinusoidal quantities graphically."] == WD_ALIGN_PARAGRAPH.LEFT
    assert [v for k, v in aligns.items() if k.startswith("• Widely")][0] == WD_ALIGN_PARAGRAPH.JUSTIFY


def test_single_line_bullets_stay_justified(tmp_path):
    doc, _, _ = _render(tmp_path, notes="Angle\n• One radian is the SI unit.\n")
    para = [p for p in doc.paragraphs if p.text.startswith("•")][0]
    assert para.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY


def test_the_footer_icon_is_an_image_not_a_missing_glyph(tmp_path):
    """The old book emoji had no glyph in any document font and printed as a box."""
    doc, _, _ = _render(tmp_path)
    footer_xml = doc.sections[0].footer._element.xml
    assert "\U0001F4D8" not in footer_xml
    assert "blip" in footer_xml, "footer branding icon should be a real image"


def test_only_the_four_template_font_sizes_are_used(tmp_path):
    doc, _, _ = _render(tmp_path)
    sizes = {r.font.size.pt for _, r in _runs(doc) if r.font.size is not None}
    assert sizes <= {BODY_SIZE_PT, SECTION_HEADING_SIZE_PT,
                     LECTURE_TITLE_SIZE_PT, SUBJECT_SIZE_PT}, sizes


# --------------------------------------------------------------------------
# Fonts: Kalam primary, maths fallback for glyphs it cannot draw
# --------------------------------------------------------------------------
def test_prose_uses_kalam_and_symbols_fall_back_to_the_maths_font(tmp_path):
    doc, _, _ = _render(tmp_path, notes="Angle\n• The angle θ satisfies sinθ ≤ 1 and Δx ± 2.\n")
    by_font: dict[str, str] = {}
    for _, run in _runs(doc):
        by_font.setdefault(run.font.name, "")
        by_font[run.font.name] += run.text
    assert FONT_PRIMARY in by_font
    assert "satisfies" in by_font[FONT_PRIMARY]
    assert FONT_MATH in by_font, "Greek/operators must not stay in Kalam"
    for symbol in "θ≤Δ±":
        assert symbol in by_font[FONT_MATH]
        assert symbol not in by_font[FONT_PRIMARY]


def test_vector_arrows_stay_attached_to_their_base_letter(tmp_path):
    # A combining arrow split into its own run renders as a stray mark; the base
    # letter must travel with it into the maths font.
    doc, _, _ = _render(tmp_path, notes="Vectors\n• Resultant A⃗ + B⃗ = C⃗.\n")
    math_text = "".join(r.text for _, r in _runs(doc) if r.font.name == FONT_MATH)
    assert "A⃗" in math_text and "B⃗" in math_text


# --------------------------------------------------------------------------
# Furniture
# --------------------------------------------------------------------------
def test_header_footer_watermark_and_page_number_are_present(tmp_path):
    doc, warnings, _ = _render(tmp_path)
    section = doc.sections[0]
    header_xml = section.header._element.xml
    footer_xml = section.footer._element.xml
    assert "blip" in header_xml, "PW logo/watermark image missing from the header"
    assert "wp:anchor" in header_xml, "watermark must be anchored behind the text"
    assert "behindDoc=\"1\"" in header_xml
    assert "Master NCERT" in footer_xml
    assert "PAGE" in footer_xml, "automatic page-number field missing"
    assert "[" in footer_xml and "]" in footer_xml
    assert not any("branding is missing" in w for w in warnings)


def test_docx_stays_editable_text_not_a_picture_of_a_page(tmp_path):
    doc, _, _ = _render(tmp_path)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Trigonometric Ratio" in text
    assert "sine curve repeats" in text


# --------------------------------------------------------------------------
# QC
# --------------------------------------------------------------------------
def test_formatting_qc_passes_on_a_freshly_rendered_document(tmp_path):
    _, _, path = _render(tmp_path)
    assert check_docx_formatting(path) == []


def test_formatting_qc_flags_a_letter_sized_page(tmp_path):
    _, _, path = _render(tmp_path)
    doc = Document(str(path))
    from docx.shared import Inches

    doc.sections[0].page_width = Inches(8.5)
    doc.sections[0].page_height = Inches(11.0)
    doc.save(str(path))
    problems = check_docx_formatting(path)
    assert any("not A4" in p for p in problems), problems


def test_notes_render_identically_for_every_subject(tmp_path):
    """Same template, different content — the acceptance criterion."""
    shape = []
    for subject in ("Physics", "Chemistry", "Mathematics", "Biology"):
        doc, _, _ = _render(tmp_path / subject.lower(), subject=subject)
        section = doc.sections[0]
        shape.append((
            round(section.page_width.inches, 2),
            round(section.left_margin.inches, 2),
            doc.paragraphs[0].runs[0].font.size.pt,
            doc.paragraphs[1].runs[0].font.size.pt,
            str(doc.paragraphs[0].runs[0].font.color.rgb),
        ))
    assert len(set(shape)) == 1, shape


def test_slides_without_a_logo_still_get_pw_branding(tmp_path):
    """A text-only PDF has no corner logo; the approved asset must fill in."""
    slide = SlideData(slide_no=1, heading="Angle", text="Angle definition.")
    doc, warnings, _ = _render(tmp_path, slides=[slide])
    assert "blip" in doc.sections[0].header._element.xml
    assert not any("branding is missing" in w for w in warnings)
