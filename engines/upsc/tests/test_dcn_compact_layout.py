"""The compact DCN layout, the two-level title, and yellow highlighting in the DOCX.

These are the checks for the layout pass that made the notes ~25% denser without
touching the note-generation pipeline. They fall into two kinds, and the
difference is deliberate:

* **Invariants** — things that must hold whatever the numbers are tuned to:
  body text stays at a readable size, the ladder of headings stays ordered, the
  content column never reaches the notes box, a bullet glyph never grows wider
  than the space reserved for it. These are the tests that keep a future
  tightening pass honest.
* **Wiring** — the document really carries what config says, so an edit to
  config moves the document rather than only the docstring.

What is deliberately NOT asserted is a particular inch or point value. Those
live in src/config.py; copying them here would mean every tuning pass is also a
test-editing pass, and a test that has to be edited to pass is not a test.
"""
from pathlib import Path
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    DCN_BULLET_GLYPH_SIZE_PT,
    DCN_BULLET_HANGING_TWIPS,
    DCN_BULLET_INDENT_STEP_TWIPS,
    DCN_COMPACTNESS,
    DCN_CONTENT_WIDTH_IN,
    DCN_HIGHLIGHT_COLOR,
    DCN_LAYOUT,
    DCN_LINE_SPACING,
    DCN_MARGIN_BOTTOM_IN,
    DCN_MARGIN_LEFT_IN,
    DCN_MARGIN_RIGHT_IN,
    DCN_MARGIN_TOP_IN,
    DCN_HEADER_DISTANCE_IN,
    DCN_LOGO_BOX_H_IN,
    DCN_MIN_READABLE_BODY_PT,
    DCN_NOTES_BOX_LEFT_IN,
    DCN_NOTES_BOX_WIDTH_IN,
    DCN_PAGE_HEIGHT_IN,
    DCN_PAGE_WIDTH_IN,
    DCN_SIZE_BODY_PT,
    DCN_SIZE_SUBJECT_PT,
    DCN_SIZE_TITLE_PT,
    DCN_TABLE_CELL_MARGIN_TWIPS,
)
from src.upsc_dcn_docx_writer import write_upsc_dcn_docx

TWIPS_PER_INCH = 1440
EMU_PER_INCH = 914400

# The geometry the layout used to ship, kept only so the compaction can be
# asserted as a change rather than as a set of numbers. Not imported from
# anywhere: it no longer exists in the code, which is the point.
#: Measured off the published PW OnlyIAS Daily Class Note this layout copies
#: (an Indian Society lecture, printed from Google Docs). Every number here came
#: out of that PDF with PyMuPDF, converted from points; they are the reference
#: the assertions below compare against, not preferences.
REFERENCE_TEXT_TOP_IN = 72.0 / 72        # body text begins here on every page
REFERENCE_TEXT_LEFT_IN = 36.0 / 72
REFERENCE_COLUMN_RIGHT_IN = 451.5 / 72   # where its full-width tables end
REFERENCE_BULLET_MARKER_IN = [(54.0 - 36.0) / 72, (90.0 - 36.0) / 72]   # levels 1, 2
REFERENCE_BULLET_TEXT_IN = [(72.0 - 36.0) / 72, (108.0 - 36.0) / 72]
REFERENCE_CELL_PAD_PT = 45.0 - 36.4      # cell text inset from the rule

LEGACY_MARGINS_IN = {"left": 0.5, "right": 2.5, "top": 1.0, "bottom": 1.0}
LEGACY_BULLET_INDENTS_TWIPS = (720, 1440, 2160)
LEGACY_LINE_SPACING = 1.15

EN_NOTES = """#LECTURE_TITLE: L04_DCN04_Major Features of Indian Society (Part 1)

#H1: Social Institutions

#H2: Meaning and Scope

- **Definition:** an institution is a settled pattern of relationships.
  - The **Environment (Protection) Act, 1986** is the governing statute.
    - A third-level point, to prove the ladder still steps.
- **Scope:** household, market and state.

#H3: Committees

1. Balwant Rai Mehta Committee, 1957.
2. Ashok Mehta Committee, 1978.

#TABLE_START
| Dimension | Traditional form | Contemporary form |
| Authority | Ascriptive status fixed at birth and rarely renegotiated later | Achieved position tested repeatedly through the market |
| Mobility | Low, mediated through kinship | Higher, mediated through education |
#TABLE_END

#FLOW_START
Socialisation
↓
Sanction
#FLOW_END

- UPSC caution: the columns are types, not stages.
"""

#: The same notes with no caution line. Cautions are shaded too, so a test that
#: asks "is there any yellow on this page?" needs a page that has no other
#: reason to carry any.
EN_NOTES_NO_CAUTION = EN_NOTES.replace(
    "- UPSC caution: the columns are types, not stages.\n", "")

HI_NOTES = """#LECTURE_TITLE: L04_DCN04_भारतीय समाज की प्रमुख विशेषताएँ (भाग-1)

#H1: सामाजिक संस्थाएँ

#H2: अर्थ एवं विस्तार

- **परिभाषा:** संस्था एक स्थिर संबंध-प्रतिमान है।
  - **पर्यावरण (संरक्षण) अधिनियम, 1986** इस संदर्भ की प्रमुख विधि है।

#TABLE_START
| आयाम | पारंपरिक रूप | समकालीन रूप |
| सत्ता का आधार | जन्मजात प्रदत्त प्रस्थिति | अर्जित स्थिति |
#TABLE_END
"""


def _parts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8", "replace")
                for n in z.namelist() if n.endswith(".xml")}


def _style_block(styles_xml: str, style_id: str) -> str:
    match = re.search(rf'<w:style [^>]*w:styleId="{style_id}".*?</w:style>', styles_xml, re.S)
    assert match, f"style {style_id} not found"
    return match.group(0)


def _plain(xml: str) -> str:
    return re.sub(r"<[^>]+>", "", xml).replace("​", "")


@pytest.fixture
def written_en(tmp_path):
    out, warnings = write_upsc_dcn_docx(
        EN_NOTES, tmp_path / "en.docx", language_code="en",
        subject_line="Indian Society")
    return out, warnings, _parts(out)


@pytest.fixture
def written_hi(tmp_path):
    out, warnings = write_upsc_dcn_docx(
        HI_NOTES, tmp_path / "hi.docx", language_code="hi",
        subject_line="भारतीय समाज")
    return out, warnings, _parts(out)


# =====================================================================
# 1. Title hierarchy: SUBJECT above LECTURE N + TITLE
# =====================================================================

def test_the_subject_is_printed_above_the_lecture_line(written_en):
    _, _, parts = written_en
    text = _plain(parts["word/document.xml"])
    subject_at = text.index("Indian Society")
    lecture_at = text.index("Lecture 4")
    assert subject_at < lecture_at, "the subject must come first"
    assert "Major Features of Indian Society (Part 1)" in text


def test_the_subject_and_lecture_line_are_one_title_block(written_en):
    """Both lines at one size, as the published note sets them.

    An earlier pass made the subject larger than the lecture line, reading the
    two as a heading and its subheading. In the original they are the same
    26 pt green — one title block that happens to wrap — and the subject is
    told apart by sitting first, not by being bigger.
    """
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    assert DCN_SIZE_SUBJECT_PT == DCN_SIZE_TITLE_PT == 26
    assert (f'<w:sz w:val="{int(DCN_SIZE_SUBJECT_PT * 2)}"/>'
            in _style_block(styles, "UPSC_DCN_Subject"))
    assert (f'<w:sz w:val="{int(DCN_SIZE_TITLE_PT * 2)}"/>'
            in _style_block(styles, "UPSC_DCN_Title"))


def test_the_machine_prefix_never_reaches_the_page(written_en):
    """L04_DCN04_ stays in the marker and the metadata, not in front of a student."""
    _, _, parts = written_en
    assert "L04_DCN04_" not in _plain(parts["word/document.xml"])


def test_hindi_gets_the_hindi_lecture_wording(written_hi):
    _, _, parts = written_hi
    text = _plain(parts["word/document.xml"])
    assert "भारतीय समाज" in text
    # A colon, as the published note punctuates it: "लेक्चर-4: ...".
    assert "लेक्चर-4:" in text, text[:300]
    assert "Lecture" not in text


def test_no_subject_means_the_document_opens_on_the_lecture_line(tmp_path):
    """Subject is metadata, not a guess: without one, nothing is invented."""
    out, _ = write_upsc_dcn_docx(EN_NOTES, tmp_path / "n.docx", language_code="en")
    text = _plain(_parts(out)["word/document.xml"])
    assert "Lecture 4" in text
    assert "Indian Society" in text  # only as part of the lecture title
    assert text.count("Indian Society") == 1


def test_a_missing_lecture_number_is_omitted_not_invented(tmp_path):
    """Spec §8: gracefully omit rather than generate a false lecture number."""
    notes = EN_NOTES.replace(
        "#LECTURE_TITLE: L04_DCN04_Major Features of Indian Society (Part 1)",
        "#LECTURE_TITLE: Major Features of Indian Society")
    out, _ = write_upsc_dcn_docx(notes, tmp_path / "n.docx", language_code="en",
                                 subject_line="Indian Society")
    text = _plain(_parts(out)["word/document.xml"])
    assert "Major Features of Indian Society" in text
    assert "Lecture 0" not in text
    assert not re.search(r"Lecture\s+\d", text), text[:300]


# =====================================================================
# 2. Compact layout
# =====================================================================

def test_the_page_geometry_matches_the_published_note():
    """The text block is where the original puts it, to a twentieth of an inch.

    Measured off the published PDF: body text starts at 72 pt (1.0 in) down and
    36 pt (0.5 in) in on every page, and the column runs to 451.5 pt (6.27 in),
    which is where its tables end. Those three numbers are the frame everything
    else is set inside, so they are pinned here rather than left to drift with
    the next spacing change.
    """
    assert DCN_MARGIN_TOP_IN == pytest.approx(REFERENCE_TEXT_TOP_IN, abs=0.05)
    assert DCN_MARGIN_LEFT_IN == pytest.approx(REFERENCE_TEXT_LEFT_IN, abs=0.05)
    column_right = DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN
    assert column_right == pytest.approx(REFERENCE_COLUMN_RIGHT_IN, abs=0.05)
    # Still a full 9.5 in of usable height, which is what the original has and
    # what the compact passes had also arrived at from the other direction.
    assert (DCN_PAGE_HEIGHT_IN - DCN_MARGIN_TOP_IN
            - DCN_MARGIN_BOTTOM_IN) == pytest.approx(9.5, abs=0.05)
    # The header furniture has to fit inside the top margin or Word pushes the
    # body back down and the measurement above stops being true.
    assert DCN_HEADER_DISTANCE_IN + DCN_LOGO_BOX_H_IN <= DCN_MARGIN_TOP_IN


def test_margins_leave_a_professional_safe_edge():
    """Compact, not edge-to-edge. Nothing may print inside a quarter inch of the
    paper's left edge, and the notes box must not run off the right."""
    assert DCN_MARGIN_LEFT_IN >= 0.35, "left margin is too tight to print"
    assert DCN_MARGIN_TOP_IN >= 0.5 and DCN_MARGIN_BOTTOM_IN >= 0.5
    assert DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN <= DCN_NOTES_BOX_LEFT_IN - 0.1
    assert DCN_NOTES_BOX_LEFT_IN + DCN_NOTES_BOX_WIDTH_IN <= DCN_PAGE_WIDTH_IN - 0.15
    assert DCN_MARGIN_RIGHT_IN > 1.5, "the notes column is no longer reserved"


def test_body_text_stays_at_or_above_the_readable_floor():
    """Round one held body at 11 pt and bought its compaction from spacing,
    indents and margins alone. Round two asked for another 15-20% off the page
    count, and those were already as tight as they read well at — so body moved
    to DCN_MIN_READABLE_BODY_PT.

    That floor is the line this test defends. It is set by printed Devanagari,
    not by arithmetic: below it Kalam's matras start to fill in, and no amount
    of page-count saving is worth Hindi a student cannot read.
    """
    assert DCN_SIZE_BODY_PT >= DCN_MIN_READABLE_BODY_PT
    assert DCN_MIN_READABLE_BODY_PT >= 10, "the floor itself was lowered"


def test_the_document_carries_the_configured_margins(written_en):
    _, _, parts = written_en
    mar = re.search(r"<w:pgMar[^>]*/>", parts["word/document.xml"]).group(0)
    got = {k: int(v) for k, v in re.findall(r'w:(top|bottom|left|right)="(\d+)"', mar)}
    assert got["left"] == int(round(DCN_MARGIN_LEFT_IN * TWIPS_PER_INCH))
    assert got["right"] == int(round(DCN_MARGIN_RIGHT_IN * TWIPS_PER_INCH))
    assert got["top"] == int(round(DCN_MARGIN_TOP_IN * TWIPS_PER_INCH))
    assert got["bottom"] == int(round(DCN_MARGIN_BOTTOM_IN * TWIPS_PER_INCH))


def test_every_paragraph_style_tightened_its_spacing(written_en):
    """No style kept the old rhythm. The old values were 12/10/8 before a
    heading and 4/2 after a paragraph; nothing may still be at or above them."""
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    legacy_before = {"UPSC_DCN_Title": 12, "UPSC_DCN_H1_Red": 12,
                     "UPSC_DCN_H2_Blue": 10, "UPSC_DCN_H3_Black": 8}
    for style_id, legacy in legacy_before.items():
        spacing = re.search(r"<w:spacing [^>]*/>", _style_block(styles, style_id)).group(0)
        before = int(re.search(r'w:before="(\d+)"', spacing).group(1))
        assert before < legacy * 20, (style_id, before)   # w:before is in 20ths of a pt


def test_bullets_indent_exactly_as_the_published_note_does(written_en):
    """The measured ladder: marker and text, per level, in inches from the margin.

    In the original, a level-1 disc sits 0.25 in inside the text margin with its
    text at 0.5 in; level 2 puts the circle at 0.75 in and the text at 1.0 in.
    Marker position is (left - hanging), which is the number a reader actually
    sees, so it is what is asserted here rather than the raw indent.
    """
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    markers, texts = [], []
    for level in (1, 2, 3):
        block = _style_block(styles, f"UPSC_DCN_Bullet_Level_{level}")
        ind = re.search(r"<w:ind [^>]*/>", block).group(0)
        left = int(re.search(r'w:left="(\d+)"', ind).group(1))
        hanging = int(re.search(r'w:hanging="(\d+)"', ind).group(1))
        assert hanging == DCN_BULLET_HANGING_TWIPS
        assert left == DCN_BULLET_INDENT_STEP_TWIPS * level
        markers.append((left - hanging) / TWIPS_PER_INCH)
        texts.append(left / TWIPS_PER_INCH)

    assert markers[:2] == pytest.approx(REFERENCE_BULLET_MARKER_IN, abs=0.02)
    assert texts[:2] == pytest.approx(REFERENCE_BULLET_TEXT_IN, abs=0.02)
    assert texts == sorted(texts), "the hierarchy must still step outwards"
    # The deepest bullet must not eat the column it is written in.
    assert texts[-1] < DCN_CONTENT_WIDTH_IN * 0.3


def test_the_bullet_glyph_fits_the_space_reserved_for_it():
    """Word tabs from the glyph to the text. A glyph wider than the hanging
    indent overshoots to the next default tab stop, and the whole ladder jumps
    half an inch right — the exact failure a smaller indent invites."""
    glyph_width_twips = DCN_BULLET_GLYPH_SIZE_PT * 20      # 1 pt = 20 twips, square worst case
    assert glyph_width_twips < DCN_BULLET_HANGING_TWIPS, (
        glyph_width_twips, DCN_BULLET_HANGING_TWIPS)


def test_the_bullet_marker_is_sized_explicitly_and_never_larger_than_the_text(written_en):
    """The marker carries its own size, and that size is written into the
    numbering rather than inherited.

    Round one set it two points under body and swapped in smaller marks; on the
    page that read as a speck, so both went back. What must not come back is the
    opposite failure — a marker LARGER than the text, which is decoration rather
    than structure — hence the bound rather than a fixed value.
    """
    _, _, parts = written_en
    numbering = parts["word/numbering.xml"]
    assert DCN_BULLET_GLYPH_SIZE_PT <= DCN_SIZE_BODY_PT
    half = int(round(DCN_BULLET_GLYPH_SIZE_PT * 2))
    assert f'<w:sz w:val="{half}"/>' in numbering
    assert f'<w:szCs w:val="{half}"/>' in numbering, "Devanagari reads only the Cs twin"


def test_the_layout_config_dict_describes_the_real_layout():
    """DCN_LAYOUT is documentation; documentation that drifts is worse than none."""
    assert DCN_LAYOUT["margins_in"]["left"] == DCN_MARGIN_LEFT_IN
    assert DCN_LAYOUT["content_in"][0] == DCN_CONTENT_WIDTH_IN
    assert DCN_LAYOUT["highlight_color"] == DCN_HIGHLIGHT_COLOR
    assert DCN_LAYOUT["compactness"] == DCN_COMPACTNESS


# =====================================================================
# 3. Yellow correction highlighting, in the document
# =====================================================================

def _shaded_runs(doc: str) -> list[str]:
    return [r for r in re.findall(r"<w:r>(?:(?!</w:r>).)*?</w:r>", doc, re.S)
            if f'w:fill="{DCN_HIGHLIGHT_COLOR}"' in r]


def test_a_correction_span_is_shaded_light_yellow(tmp_path):
    out, _ = write_upsc_dcn_docx(
        EN_NOTES_NO_CAUTION, tmp_path / "hl.docx", language_code="en",
        subject_line="Indian Society",
        highlight_spans=["Environment (Protection) Act, 1986"])
    doc = _parts(out)["word/document.xml"]
    runs = _shaded_runs(doc)
    assert len(runs) == 1, doc[:600]
    assert "Environment (Protection) Act, 1986" in _plain(runs[0])


def test_the_highlight_colour_is_light_enough_to_read_through():
    """Spec §13: light yellow, not Word's FFFF00 highlight."""
    assert DCN_HIGHLIGHT_COLOR.upper() != "FFFF00"
    r, g, b = (int(DCN_HIGHLIGHT_COLOR[i:i + 2], 16) for i in (0, 2, 4))
    assert r > 230 and g > 210, "not a light tint"
    assert b > 120, "too saturated to print black text over"


def test_highlighting_inside_bold_keeps_the_bold(tmp_path):
    """The phrase is inside ** ** in the source. Shading it must not cost it
    its weight — matching happens on visible text for exactly this reason."""
    out, _ = write_upsc_dcn_docx(
        EN_NOTES, tmp_path / "hl.docx", language_code="en",
        highlight_spans=["Environment (Protection) Act, 1986"])
    doc = _parts(out)["word/document.xml"]
    run = next(r for r in re.findall(r"<w:r>(?:(?!</w:r>).)*?</w:r>", doc, re.S)
               if f'w:fill="{DCN_HIGHLIGHT_COLOR}"' in r)
    assert "<w:b/>" in run, run
    assert "**" not in _plain(doc), "orphaned emphasis markers reached the page"


def test_highlighting_works_inside_a_table_cell(tmp_path):
    span = "Achieved position tested repeatedly through the market"
    out, _ = write_upsc_dcn_docx(EN_NOTES, tmp_path / "hl.docx", language_code="en",
                                 highlight_spans=[span])
    doc = _parts(out)["word/document.xml"]
    cell = re.search(r"<w:tc>(?:(?!</w:tc>).)*?" + re.escape(span[:20]) +
                     r"(?:(?!</w:tc>).)*?</w:tc>", doc, re.S)
    assert cell, "the span is not in a table cell any more"
    assert f'w:fill="{DCN_HIGHLIGHT_COLOR}"' in cell.group(0)


def test_highlighting_works_in_hindi(tmp_path):
    span = "पर्यावरण (संरक्षण) अधिनियम, 1986"
    out, _ = write_upsc_dcn_docx(HI_NOTES, tmp_path / "hl.docx", language_code="hi",
                                 subject_line="भारतीय समाज", highlight_spans=[span])
    doc = _parts(out)["word/document.xml"]
    run = next((r for r in re.findall(r"<w:r>(?:(?!</w:r>).)*?</w:r>", doc, re.S)
                if f'w:fill="{DCN_HIGHLIGHT_COLOR}"' in r), None)
    assert run is not None, "no highlight in the Hindi build"
    assert span in _plain(run)


def test_a_span_that_is_not_in_the_notes_shades_nothing(tmp_path):
    out, _ = write_upsc_dcn_docx(EN_NOTES_NO_CAUTION, tmp_path / "hl.docx",
                                 language_code="en",
                                 highlight_spans=["a sentence that was never written"])
    assert f'w:fill="{DCN_HIGHLIGHT_COLOR}"' not in _parts(out)["word/document.xml"]


def test_a_run_with_no_corrections_carries_no_yellow_at_all(tmp_path):
    """Spec §27: no accidental highlighting. A lecture with no corrections and
    no cautions is a page with no yellow on it anywhere."""
    out, _ = write_upsc_dcn_docx(EN_NOTES_NO_CAUTION, tmp_path / "clean.docx",
                                 language_code="en", subject_line="Indian Society")
    assert f'w:fill="{DCN_HIGHLIGHT_COLOR}"' not in _parts(out)["word/document.xml"]


def test_a_caution_is_shaded_and_loses_its_label(written_en):
    """The label said in words what the shading now says in colour. The sentence
    survives; only the announcement goes."""
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    text = _plain(doc)
    assert "the columns are types, not stages" in text, "the caution itself was dropped"
    assert "UPSC caution" not in text.lower(), "the label reached the page"
    shaded = " ".join(_plain(r) for r in _shaded_runs(doc))
    assert "the columns are types, not stages" in shaded, "the caution was not shaded"


def test_highlighting_still_composes_with_a_caution_on_the_same_page(tmp_path):
    """Two things are yellow now. Neither may swallow the other: the correction
    keeps its own run, and the caution keeps its whole sentence."""
    out, _ = write_upsc_dcn_docx(
        EN_NOTES, tmp_path / "both.docx", language_code="en",
        subject_line="Indian Society",
        highlight_spans=["Environment (Protection) Act, 1986"])
    shaded = [_plain(r) for r in _shaded_runs(_parts(out)["word/document.xml"])]
    joined = " ".join(shaded)
    assert "Environment (Protection) Act, 1986" in joined
    assert "the columns are types, not stages" in joined


def test_highlighting_does_not_change_the_words_on_the_page(tmp_path):
    plain_out, _ = write_upsc_dcn_docx(EN_NOTES, tmp_path / "a.docx", language_code="en")
    shaded_out, _ = write_upsc_dcn_docx(
        EN_NOTES, tmp_path / "b.docx", language_code="en",
        highlight_spans=["Environment (Protection) Act, 1986", "Ashok Mehta Committee"])
    assert (_plain(_parts(plain_out)["word/document.xml"])
            == _plain(_parts(shaded_out)["word/document.xml"]))


# =====================================================================
# 4. Tables
# =====================================================================

def test_tables_use_the_full_content_column(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    tbl_w = re.search(r"<w:tblW [^>]*/>", doc)
    assert tbl_w, "no table width was written"
    width = int(re.search(r'w:w="(\d+)"', tbl_w.group(0)).group(1))
    assert width == int(round(DCN_CONTENT_WIDTH_IN * TWIPS_PER_INCH))
    columns = [int(w) for w in re.findall(r'<w:gridCol w:w="(\d+)"/>', doc)]
    assert sum(columns[:3]) == width, "columns must add up to the table width"


def test_table_cell_padding_matches_the_published_note(written_en):
    """Text touching a gridline reads as clipped even when it is not.

    The original starts its cell text 8.6 pt inside the rule. A compact pass had
    cut that to 3.6 pt, which ran Devanagari almost into the gridline — matras
    sit wide, so the visible gap is smaller than the number suggests.
    """
    _, _, parts = written_en
    margins = re.search(r"<w:tblCellMar>.*?</w:tblCellMar>", parts["word/document.xml"], re.S)
    assert margins, "no cell margins were written"
    for edge in ("left", "right"):
        value = int(re.search(rf'<w:{edge} w:w="(\d+)"', margins.group(0)).group(1))
        assert value == DCN_TABLE_CELL_MARGIN_TWIPS[edge]
        assert value / 20 == pytest.approx(REFERENCE_CELL_PAD_PT, abs=1.0), (edge, value)


def test_a_table_row_never_splits_across_a_page(written_en):
    """Half a comparison on each page reads as clipped content."""
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    rows = re.findall(r"<w:tr[ >](?:(?!</w:tr>).)*?</w:tr>", doc, re.S)
    assert rows, "no table rows were written"
    for row in rows:
        assert "<w:cantSplit/>" in row, row[:200]


def test_the_header_row_repeats_on_every_page(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert doc.count("<w:tblHeader/>") == 1, "exactly the first row repeats"
    first_row = re.search(r"<w:tr[ >](?:(?!</w:tr>).)*?</w:tr>", doc, re.S).group(0)
    assert "<w:tblHeader/>" in first_row


def test_table_text_wraps_rather_than_being_clipped(written_en):
    """No w:noWrap anywhere, and no fixed row heights: both turn a long cell
    into hidden content instead of a taller row."""
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert "<w:noWrap/>" not in doc
    assert "<w:trHeight" not in doc
    # The longest cell survived in full.
    assert "Ascriptive status fixed at birth and rarely renegotiated later" in _plain(doc)


# =====================================================================
# 5. Page-break safety and the furniture
# =====================================================================

@pytest.mark.parametrize("style_id", [
    "UPSC_DCN_Subject", "UPSC_DCN_Title", "UPSC_DCN_H1_Red",
    "UPSC_DCN_H2_Blue", "UPSC_DCN_H3_Black", "UPSC_DCN_Figure",
])
def test_headings_and_figures_stay_with_what_follows(written_en, style_id):
    """A heading alone at the foot of a page is the ugly break compaction
    invites; keepNext is what stops it."""
    _, _, parts = written_en
    assert "<w:keepNext/>" in _style_block(parts["word/styles.xml"], style_id)


def test_run_shading_lands_in_schema_position():
    """w:rPr is an XSD sequence: w:shd must precede w:vertAlign / w:rtl / w:cs /
    w:lang. Appending it blindly was valid only while no run carried one of
    those, so a superscript added upstream would have made the part unreadable."""
    from docx import Document as _Document
    from docx.oxml import OxmlElement as _El
    from lxml import etree

    from src.upsc_dcn_docx_writer import _set_run_shading

    run = _Document().add_paragraph().add_run("x")
    rpr = run._element.get_or_add_rPr()
    for tag in ("w:vertAlign", "w:rtl", "w:lang"):
        rpr.append(_El(tag))
    _set_run_shading(run, DCN_HIGHLIGHT_COLOR)
    order = [etree.QName(child).localname for child in rpr]
    assert order.index("shd") < min(order.index(t) for t in ("vertAlign", "rtl", "lang")), order


def test_repositioning_the_notes_box_leaves_extension_lists_alone():
    """a:ext is the shape's size inside a:xfrm — and, with the same tag name, the
    child of an a:extLst, where it may carry only @uri. Word writes a:extLst into
    the shapes it authors, so a template box is a live trigger for this."""
    from lxml import etree

    from src.upsc_dcn_docx_writer import _NS_A, _NS_WP, _reposition_notes_box

    element = etree.fromstring(
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        f'xmlns:a="{_NS_A}" xmlns:wp="{_NS_WP}">'
        '<wp:anchor><wp:positionH><wp:posOffset>1</wp:posOffset></wp:positionH>'
        '<wp:extent cx="1" cy="2"/></wp:anchor>'
        '<a:xfrm><a:ext cx="1" cy="2"/></a:xfrm>'
        '<a:ln><a:extLst><a:ext uri="{BAR}"/></a:extLst></a:ln>'
        "</w:r>".encode())
    _reposition_notes_box(element)
    shape_ext = element.find(f"{{{_NS_A}}}xfrm/{{{_NS_A}}}ext")
    assert shape_ext.get("cx") != "1", "the shape was not resized"
    for ext in element.iter(f"{{{_NS_A}}}ext"):
        if ext is shape_ext:
            continue
        assert set(ext.keys()) == {"uri"}, dict(ext.attrib)


def test_the_notes_box_and_the_footer_strip_survive_the_compaction(written_en):
    _, _, parts = written_en
    header = "".join(v for k, v in parts.items() if re.match(r"word/header\d+\.xml", k))
    footer = "".join(v for k, v in parts.items() if re.match(r"word/footer\d+\.xml", k))
    assert "Space for Notes" in header and "<wp:anchor" in header
    assert "PW OnlyIAS" in footer and "Daily Class Notes" in footer
    assert "<w:drawing" in header, "the logo went missing"


def test_the_notes_box_is_anchored_where_config_says(written_en):
    """The offset is measured from the text column, so it has to be recomputed
    whenever the left margin moves — a stale value sits the box on the text."""
    _, _, parts = written_en
    header = "".join(v for k, v in parts.items() if re.match(r"word/header\d+\.xml", k))
    expected = int(round((DCN_NOTES_BOX_LEFT_IN - DCN_MARGIN_LEFT_IN) * EMU_PER_INCH))
    assert f"<wp:posOffset>{expected}</wp:posOffset>" in header, header[:400]


def test_nothing_in_the_notes_was_dropped_by_the_compaction(written_en):
    """The compaction is a layout change. Every heading, bullet, table cell and
    flow step in the source has to still be on the page."""
    _, _, parts = written_en
    text = _plain(parts["word/document.xml"])
    for fragment in ("Social Institutions", "Meaning and Scope", "Committees",
                     "an institution is a settled pattern of relationships",
                     "A third-level point, to prove the ladder still steps",
                     "Balwant Rai Mehta Committee, 1957",
                     "Contemporary form", "Socialisation", "Sanction",
                     # The caution's sentence survives; only its label is dropped.
                     "the columns are types, not stages"):
        assert fragment in text, fragment


def test_the_writer_still_accepts_its_old_call_shape(tmp_path):
    """Backward compatibility: subject_line and highlight_spans are additions,
    not requirements. Every existing caller must keep working untouched."""
    out, warnings = write_upsc_dcn_docx(EN_NOTES, tmp_path / "legacy.docx")
    assert out.exists() and not warnings
    assert "Lecture 4" in _plain(_parts(out)["word/document.xml"])
