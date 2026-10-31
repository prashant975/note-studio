"""Tests for the PW OnlyIAS DCN writer (src/upsc_dcn_docx_writer.py).

The layout is the product here, so these assert on the saved OOXML rather than
on Python return values: page geometry, the anchored notes box in the header,
the footer strip, heading styles, bullet levels, tables clamped to the content
column, and the two things that silently ruin a page — a literal ``**`` left in
the text and a run whose glyphs the handwritten font cannot show.

Everything runs offline and without Word: the reference template is used when
present, and every assertion that needs it is skipped rather than failed when
it is absent, so the suite still passes on a machine that does not have it.
"""
from pathlib import Path
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    DCN_CONTENT_WIDTH_IN,
    DCN_LOGO_BOX_H_IN,
    DCN_LOGO_BOX_W_IN,
    DCN_MARGIN_BOTTOM_IN,
    DCN_MARGIN_LEFT_IN,
    DCN_MARGIN_RIGHT_IN,
    DCN_MARGIN_TOP_IN,
    DCN_NOTES_BOX_LEFT_IN,
    DCN_NOTES_BOX_WIDTH_IN,
    DCN_PAGE_WIDTH_IN,
)
from src.upsc_dcn_docx_writer import (
    DCN_MARKERS,
    _LOGO_INDENT_TWIPS,
    _resolve_logo,
    default_logo_path,
    write_upsc_dcn_docx,
)

REFERENCE = Path(r"C:/Users/Laptop/Downloads/Pulkit Jain Sir Environment DCN.docx")

NOTES = """#LECTURE_TITLE: L01_DCN01_Introduction to Ecology:

#H1: Core Concepts

- **Ecology** is the study of organisms and their environment.
  - Second level point that explains the first one further.
    - Third level detail that supports the second level point.

#H2: Indicators

- Values are reported in **mg/m³** and **dB(A)**, with **CO₂** and **N₂O** tracked.

#TABLE_START
Indicator | Status
Installed capacity | 513.73 GW
Non-fossil capacity | 266.79 GW
#TABLE_END

#H3: Process

#FLOW_START
Sun
↓
Producers
#FLOW_END

#SEPARATOR

- UPSC caution: non-fossil is not the same as renewable.
- [Needs Verification: capacity figure needs checking]
"""

EMU_PER_INCH = 914400
TWIPS_PER_INCH = 1440


def _parts(path: Path) -> dict[str, str]:
    """Every XML part of the saved DOCX, decoded."""
    with zipfile.ZipFile(path) as z:
        return {
            n: z.read(n).decode("utf-8", "replace")
            for n in z.namelist()
            if n.endswith(".xml")
        }


def _headers(parts: dict[str, str]) -> str:
    return "".join(v for k, v in parts.items() if re.match(r"word/header\d+\.xml", k))


def _footers(parts: dict[str, str]) -> str:
    return "".join(v for k, v in parts.items() if re.match(r"word/footer\d+\.xml", k))


@pytest.fixture
def written(tmp_path):
    """Programmatic mode — no template, so it must build the layout itself."""
    out, warnings = write_upsc_dcn_docx(NOTES, tmp_path / "notes.docx")
    return out, warnings, _parts(out)


# --------------------------------------------------------------- page geometry

def test_page_geometry_matches_the_reference(written):
    _, _, parts = written
    doc = parts["word/document.xml"]
    size = re.search(r'<w:pgSz[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"', doc) or re.search(
        r'<w:pgSz[^>]*w:h="(\d+)"[^>]*w:w="(\d+)"', doc)
    assert size, "no page size recorded"
    mar = re.search(r"<w:pgMar[^>]*/>", doc).group(0)
    got = {k: int(v) for k, v in re.findall(r'w:(top|bottom|left|right)="(\d+)"', mar)}
    # 8.5 x 11 in, still with the wide right margin that reserves the notes
    # column. The numbers come from config: this test proves the document
    # carries the geometry the project decided on, not that the geometry is a
    # particular set of inches.
    assert got["left"] == int(round(DCN_MARGIN_LEFT_IN * TWIPS_PER_INCH))
    assert got["right"] == int(round(DCN_MARGIN_RIGHT_IN * TWIPS_PER_INCH))
    assert got["top"] == int(round(DCN_MARGIN_TOP_IN * TWIPS_PER_INCH))
    assert got["bottom"] == int(round(DCN_MARGIN_BOTTOM_IN * TWIPS_PER_INCH))
    # Content must never reach the reserved column, and the box must never
    # reach the paper edge — the two failures the wide right margin exists to
    # prevent, and the two that a compaction pass could quietly reintroduce.
    content_right = DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN
    assert content_right <= DCN_NOTES_BOX_LEFT_IN - 0.1, "body text runs into the notes box"
    assert DCN_NOTES_BOX_LEFT_IN + DCN_NOTES_BOX_WIDTH_IN <= DCN_PAGE_WIDTH_IN - 0.15, (
        "the notes box runs off the page")


def test_notes_box_is_anchored_in_the_header_so_it_repeats(written):
    _, _, parts = written
    header = _headers(parts)
    assert "<wp:anchor" in header, "notes box must float, not sit in the text flow"
    assert "Space for Notes" in header
    # In the header it repeats on every page and cannot be pushed by content.
    assert "txbxContent" in header


# -------------------------------------------------------------- header logo
# The PW OnlyIAS lockup has to appear at the top of *every* page. That is a
# property of where it lives, not of how it is drawn: a picture in the default
# header repeats, the same picture in the body does not, and a stray
# <w:titlePg> would quietly confine it to page one.

def test_the_shipped_logo_is_the_onlyias_lockup():
    logo = default_logo_path()
    assert logo is not None and logo.exists(), "no logo installed in assets/"
    assert logo.name == "pw_onlyias_logo.png", f"preferred the wrong file: {logo.name}"
    from PIL import Image

    with Image.open(logo) as image:
        width, height = image.size
    # The lockup is a wide mark+wordmark; the bare PW circle is square. This is
    # what tells the two apart without comparing pixels.
    assert width / height > 3, f"{logo.name} is not the wide lockup ({width}x{height})"


def test_a_caller_that_passes_no_logo_still_gets_the_lockup():
    """rebuild_outputs, the CLI and the tests all reach the writer without a
    logo_path. They must land on the same file the app uses."""
    assert _resolve_logo(None) == default_logo_path()
    assert _resolve_logo(Path("does-not-exist.png")) == default_logo_path()


def test_the_logo_is_embedded_in_the_header(written):
    _, warnings, parts = written
    header = _headers(parts)
    assert "<w:drawing" in header, "no image in the header at all"
    assert not [w for w in warnings if "logo" in w.lower()], warnings
    # Sized to the configured logo box, and — the part that matters after the
    # compact pass — short enough to fit inside the top margin. A logo band
    # taller than the margin makes Word push the body back down, which silently
    # undoes the compaction.
    assert DCN_LOGO_BOX_H_IN < DCN_MARGIN_TOP_IN, "logo band is taller than the top margin"
    extents = [(int(cx), int(cy)) for cx, cy in
               re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"', header)]
    logo_extent = [e for e in extents
                   if e[1] <= int((DCN_LOGO_BOX_H_IN + 0.05) * EMU_PER_INCH)]
    assert logo_extent, f"no header image the size of a logo: {extents}"
    cx, cy = logo_extent[0]
    assert cx == int(DCN_LOGO_BOX_W_IN * EMU_PER_INCH)
    assert cy <= int(0.735 * EMU_PER_INCH)


def test_the_header_logo_repeats_on_every_page(written):
    _, _, parts = written
    doc = parts["word/document.xml"]
    # "Different first page" would leave every page after the first bare.
    assert "<w:titlePg" not in doc, "titlePg confines the logo to page 1"
    assert "<w:evenAndOddHeaders" not in parts.get("word/settings.xml", "")
    # The picture must be in the *default* header, which is the one Word applies
    # to every page that has no more specific header.
    refs = re.findall(r'<w:headerReference[^>]*w:type="(\w+)"[^>]*r:id="(\w+)"', doc)
    assert refs, "no header referenced by the section"
    assert any(kind == "default" for kind, _ in refs), f"only {refs}"
    assert "<w:drawing" in _headers(parts)


def test_the_logo_sits_where_the_reference_puts_it(written):
    """The reference anchors the logo 1647825 EMU from the text margin. Centring
    the paragraph is only equivalent for a logo as wide as its box — the square
    PW mark happened to land close, the lockup does not."""
    _, _, parts = written
    header = _headers(parts)
    indents = [int(v) for v in re.findall(r'<w:ind[^>]*w:left="(\d+)"', header)]
    assert _LOGO_INDENT_TWIPS in indents, f"logo indent {_LOGO_INDENT_TWIPS} not in {indents}"
    assert _LOGO_INDENT_TWIPS == 2595


def test_footer_strip_carries_both_labels(written):
    _, _, parts = written
    footer = _footers(parts)
    assert "PW OnlyIAS" in footer and "Daily Class Notes" in footer
    assert "cfe2f3" in footer.lower(), "footer strip must be the light blue fill"


def test_footer_keeps_each_label_in_its_own_cell(written):
    """Not tab stops: a viewer that clamps the strip's negative indent (Google
    Docs) dropped the right-hand stop and printed "5Daily Class Notes"."""
    _, _, parts = written
    footer = _footers(parts)
    assert "<w:tab/>" not in footer and "<w:tabs>" not in footer
    cells = re.findall(r"<w:tc>.*?</w:tc>", footer, re.S)
    assert len(cells) == 3
    assert "PW OnlyIAS" in cells[0]
    assert "PAGE" in cells[1] and 'w:val="center"' in cells[1]
    assert "Daily Class Notes" in cells[2]
    assert re.search(r'<w:tblInd w:w="-\d+"', footer), "the strip must bleed to the page edge"


def test_footer_labels_are_configurable(tmp_path):
    out, _ = write_upsc_dcn_docx(
        NOTES, tmp_path / "n.docx", footer_left="Left Co", footer_right="Right Co")
    footer = _footers(_parts(out))
    assert "Left Co" in footer and "Right Co" in footer


def test_layout_toggles_are_honoured(tmp_path):
    plain, _ = write_upsc_dcn_docx(
        NOTES, tmp_path / "plain.docx", use_notes_box=False, add_footer_strip=False)
    parts = _parts(plain)
    assert "Space for Notes" not in _headers(parts)
    assert "Daily Class Notes" not in _footers(parts)


# ------------------------------------------------------------------- content

def test_every_marker_produces_its_own_style(written):
    _, _, parts = written
    used = set(re.findall(r'<w:pStyle w:val="([^"]+)"', parts["word/document.xml"]))
    for style in (
        "UPSC_DCN_Title", "UPSC_DCN_H1_Red", "UPSC_DCN_H2_Blue", "UPSC_DCN_H3_Black",
        "UPSC_DCN_Bullet_Level_1", "UPSC_DCN_Bullet_Level_2", "UPSC_DCN_Bullet_Level_3",
        "UPSC_DCN_Flow",
    ):
        assert style in used, f"{style} was never applied"


def test_heading_colours_match_the_reference(written):
    _, _, parts = written
    styles = parts["word/styles.xml"]
    for style_id, colour in (
        ("UPSC_DCN_Title", "274E13"),      # dark green
        ("UPSC_DCN_H1_Red", "990000"),     # dark red
        ("UPSC_DCN_H2_Blue", "0B5394"),    # blue
    ):
        block = re.search(
            rf'<w:style [^>]*w:styleId="{style_id}".*?</w:style>', styles, re.S)
        assert block, f"{style_id} not defined"
        found = [c.upper() for c in re.findall(
            r'<w:color w:val="([0-9A-Fa-f]{6})"', block.group(0))]
        assert colour in found, f"{style_id} should be #{colour}, got {found}"


def test_bold_markers_become_real_bold_runs(written):
    _, _, parts = written
    doc = parts["word/document.xml"]
    assert "**" not in doc, "literal ** leaked into the document"
    assert "<w:b/>" in doc or '<w:b w:val="1"/>' in doc, "no bold run was produced"
    assert "Ecology" in doc


def test_unbalanced_emphasis_does_not_leave_double_spaces(tmp_path):
    out, _ = write_upsc_dcn_docx(
        "#LECTURE_TITLE: L01_DCN01_T:\n\n- an unbalanced ** pair here.\n",
        tmp_path / "odd.docx")
    doc = _parts(out)["word/document.xml"]
    assert "**" not in doc
    texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", doc)
    assert not any("  " in t for t in texts), "stripping ** left a doubled space"


def test_table_is_a_real_table_clamped_to_the_content_column(written):
    _, _, parts = written
    doc = parts["word/document.xml"]
    assert "<w:tbl>" in doc, "#TABLE_START did not produce a Word table"
    widths = [int(w) for w in re.findall(r'<w:gridCol w:w="(\d+)"', doc)]
    assert widths, "table has no column widths"
    total_in = sum(widths) / TWIPS_PER_INCH
    assert total_in <= DCN_CONTENT_WIDTH_IN + 0.01, (
        f"table is {total_in:.2f} in wide and would reach the notes box")


def test_symbols_survive(written):
    _, _, parts = written
    doc = parts["word/document.xml"]
    for glyph in ("₂", "³", "↓", "°" if "°" in NOTES else "₂"):
        assert glyph in doc, f"{glyph!r} was lost"


# ---------------------------------------------------------------- robustness

@pytest.mark.parametrize("text", [
    "",
    "   \n\n  ",
    "plain prose with no markers at all",
    "#TABLE_START\nunclosed | table\n",
    "#FLOW_START\nunclosed flow\n",
    "#TABLE_END\n",
    "#LECTURE_TITLE:\n",
    "#H1:\n#H2:\n#H3:\n",
])
def test_malformed_input_degrades_instead_of_raising(tmp_path, text):
    out, warnings = write_upsc_dcn_docx(text, tmp_path / "odd.docx")
    assert out.exists()
    assert isinstance(warnings, list)


@pytest.mark.parametrize("codepoint", [0x00, 0x07, 0x08, 0x0b, 0x1b, 0x7f])
def test_control_characters_are_stripped_not_fatal(tmp_path, codepoint):
    """lxml rejects these outright, so they must never reach a run."""
    text = f"#LECTURE_TITLE: L01_DCN01_T:\n\n- a{chr(codepoint)}bullet.\n"
    out, warnings = write_upsc_dcn_docx(text, tmp_path / "ctrl.docx")
    assert out.exists()
    assert any("control character" in w for w in warnings)


def test_markers_tuple_is_the_documented_contract():
    # "#FIGURE:" joined with the lecture-visuals feature; "#CORRECTION_*" with
    # the gated corrections callout (CORRECTIONS_ENABLED). TOPICS/SEPARATOR
    # remain in the parsing vocabulary even though the prompt retired them —
    # a model remembering the old contract must degrade cleanly, not leak
    # marker text onto the page. "#H4:" is the reverse case: the published note
    # has a fourth (grey) heading level, so the writer accepts one before the
    # prompt emits any.
    assert DCN_MARKERS == (
        "#LECTURE_TITLE:", "#TOPICS_COVERED_START", "#TOPICS_COVERED_END",
        "#H1:", "#H2:", "#H3:", "#H4:",
        "#TABLE_START", "#TABLE_END", "#FLOW_START", "#FLOW_END", "#SEPARATOR",
        "#FIGURE:", "#CORRECTION_START", "#CORRECTION_END",
    )


# ------------------------------------------------------------- template mode

@pytest.mark.skipif(not REFERENCE.exists(), reason="reference template not on this machine")
def test_template_mode_keeps_the_embedded_font_and_drops_old_body(tmp_path):
    """The embedded font is what makes the handwritten look survive on a
    machine without Coming Soon installed, so losing it would be invisible
    here but obvious to every reader."""
    out, _ = write_upsc_dcn_docx(NOTES, tmp_path / "t.docx", template_path=REFERENCE)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        doc = z.read("word/document.xml").decode("utf-8", "replace")
    assert any(n.startswith("word/fonts/") for n in names), "embedded font was dropped"
    # The heading is the printed two-level form; the L01_DCN01_ prefix stays in
    # the marker and the metadata, not on the page.
    assert "Introduction to Ecology" in doc
    # The template's own lecture content must be gone.
    assert "Levels of ecological organisation" not in doc
    assert "<w:sectPr" in doc, "removing the body must not take the section properties"


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference template not on this machine")
def test_template_mode_applies_the_projects_page_geometry(tmp_path):
    """Template mode inherits the reference's furniture, NOT its page geometry.

    It used to inherit both, because both were the same numbers. They are not
    any more: the compact layout is a project decision that has to hold whether
    or not someone uploaded a template, or a templated run would silently ship
    the old 1.0 in margins. What template mode still brings is the header,
    footer, styles, numbering and the embedded font.
    """
    out, _ = write_upsc_dcn_docx(NOTES, tmp_path / "t.docx", template_path=REFERENCE)
    ours = re.search(r"<w:pgMar[^>]*/>", _parts(out)["word/document.xml"]).group(0)
    got = {k: int(v) for k, v in re.findall(r'w:(top|bottom|left|right)="(\d+)"', ours)}
    assert got["left"] == int(round(DCN_MARGIN_LEFT_IN * TWIPS_PER_INCH))
    assert got["right"] == int(round(DCN_MARGIN_RIGHT_IN * TWIPS_PER_INCH))
    assert got["top"] == int(round(DCN_MARGIN_TOP_IN * TWIPS_PER_INCH))
    assert got["bottom"] == int(round(DCN_MARGIN_BOTTOM_IN * TWIPS_PER_INCH))


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference template not on this machine")
def test_template_mode_moves_the_templates_notes_box_onto_the_new_geometry(tmp_path):
    """A box left where the template drew it would sit on the body text.

    The anchor's horizontal offset is measured from the text column, so it does
    not move when the margins do — the box has to be re-anchored explicitly, or
    the wider content column runs straight into it.
    """
    out, _ = write_upsc_dcn_docx(NOTES, tmp_path / "t.docx", template_path=REFERENCE)
    parts = _parts(out)
    header = "".join(v for k, v in parts.items() if re.match(r"word/header\d+\.xml", k))
    offsets = [int(v) for v in re.findall(r"<wp:posOffset>(\d+)</wp:posOffset>", header)]
    expected = int(round((DCN_NOTES_BOX_LEFT_IN - DCN_MARGIN_LEFT_IN) * EMU_PER_INCH))
    assert expected in offsets, (expected, offsets)


def test_missing_template_falls_back_and_warns(tmp_path):
    out, warnings = write_upsc_dcn_docx(
        NOTES, tmp_path / "n.docx", template_path=tmp_path / "nope.docx")
    assert out.exists(), "a missing template must not lose the notes"
    assert any("template" in w.lower() for w in warnings)
