"""PW OnlyIAS "Daily Class Notes" (DCN) DOCX writer.

WHY this lives beside docx_writer.py instead of inside it: the DCN look is not a
theme, it is a page *geometry*. The right margin is 2.5 in purely to reserve a
column for a "Space for Notes" box that is anchored in the **header** — that is
how the reference document repeats the box on every page without it drifting as
body text grows. Folding that into the legacy writer would have made every
legacy layout decision conditional; a separate module leaves the legacy path
byte-for-byte unchanged.

Two ways in, both producing the same visual result:

* **template mode** (preferred) — open the real reference DOCX, delete only its
  body content, and keep its sectPr, header, footer, styles, numbering and the
  embedded ``word/fonts/ComingSoon-regular.ttf`` part. Reusing the real furniture
  is what makes the output match the reference on machines that do not have the
  handwritten face installed.
* **programmatic mode** — rebuild the same geometry from the measurements in
  src.config. python-docx cannot express a header-anchored text box, so that one
  shape is emitted as raw OOXML copied from the reference's header1.xml.

Nothing here raises on malformed marker input: every degradation is recorded in
the returned warnings list so the caller can surface it next to the download
button.
"""
from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor, Twips

from .config import (
    CORRECTIONS_ENABLED,
    DCN_BULLET_GLYPHS,
    DCN_BULLET_GLYPH_SIZE_PT,
    DCN_BULLET_HANGING_TWIPS,
    DCN_BULLET_INDENT_STEP_TWIPS,
    DCN_BULLET_LEVELS,
    DCN_CAUTION_INDENT_TWIPS,
    DCN_COLOR_BODY,
    DCN_COLOR_FOOTER_FILL,
    DCN_COLOR_H1,
    DCN_COLOR_H2,
    DCN_COLOR_H3,
    DCN_COLOR_H4,
    DCN_COLOR_NOTES_BOX,
    DCN_COLOR_TABLE_RULE,
    DCN_COLOR_TITLE,
    DCN_CONTENT_WIDTH_IN,
    DCN_FOOTER_DISTANCE_IN,
    DCN_BULLET_FONT,
    DCN_FONT_CHAIN,
    DCN_FONT_FALLBACKS,
    DCN_FONT_PRIMARY,
    DCN_FONT_SYMBOL,
    DCN_FOOTER_SHOW_PAGE_NUMBER,
    DCN_HEADER_DISTANCE_IN,
    DCN_HIGHLIGHT_COLOR,
    DCN_LINE_SPACING,
    DCN_LINE_SPACING_TIGHT,
    DCN_LOGO_BOX_H_IN,
    DCN_LOGO_BOX_W_IN,
    DCN_MARGIN_BOTTOM_IN,
    DCN_MARGIN_LEFT_IN,
    DCN_MARGIN_RIGHT_IN,
    DCN_MARGIN_TOP_IN,
    DCN_NOTES_BOX_HEIGHT_IN,
    DCN_NOTES_BOX_LEFT_IN,
    DCN_NOTES_BOX_TOP_IN,
    DCN_NOTES_BOX_WIDTH_IN,
    DCN_PAGE_HEIGHT_IN,
    DCN_PAGE_WIDTH_IN,
    DCN_SIZE_BODY_PT,
    DCN_SIZE_H1_PT,
    DCN_SIZE_H2_PT,
    DCN_SIZE_H3_PT,
    DCN_SIZE_FOOTER_PT,
    DCN_SIZE_H4_PT,
    DCN_SIZE_NOTES_CAPTION_PT,
    DCN_SIZE_SUBJECT_PT,
    DCN_SIZE_TITLE_PT,
    DCN_SPACE_BODY_PT,
    DCN_SPACE_BULLET_PT,
    DCN_SPACE_CAUTION_PT,
    DCN_SPACE_FIGURE_CAPTION_PT,
    DCN_SPACE_FIGURE_PT,
    DCN_SPACE_FLOW_PT,
    DCN_SPACE_H1_PT,
    DCN_SPACE_H2_PT,
    DCN_SPACE_H3_PT,
    DCN_SPACE_H4_PT,
    DCN_SPACE_SUBJECT_PT,
    DCN_SPACE_TABLE_PT,
    DCN_SPACE_TITLE_PT,
    DCN_TABLE_CELL_MARGIN_TWIPS,
    DCN_TABLE_HEADER_FILL,
    ROOT_DIR,
    VISUAL_MAX_HEIGHT_IN,
    VISUAL_MAX_WIDTH_IN,
    VISUAL_MIN_WIDTH_IN,
    VISUAL_TARGET_DPI,
)
from .docx_layout import DEVANAGARI_FONT, is_font_installed
from .languages import is_devanagari
from .notes_markup import normalize_notes_markup
from .title_utils import (
    is_placeholder,
    lecture_heading,
    looks_like_hex_identifier,
    parse_marker_title,
    wrap_for_display,
)

# Markers the DCN prompt (prompts/upsc_dcn_en.txt) is told to emit. Exported so
# the quality checker and the UI can validate model output against one list.
DCN_MARKERS = (
    "#LECTURE_TITLE:",
    "#TOPICS_COVERED_START",
    "#TOPICS_COVERED_END",
    "#H1:",
    "#H2:",
    "#H3:",
    # The original's fourth level, grey. The prompt does not emit #H4:
    # today — it is accepted here so the ladder is complete and a later
    # prompt change needs no writer change, and so a model that reaches for
    # it anyway gets a heading rather than a body line reading "#H4:".
    "#H4:",
    "#TABLE_START",
    "#TABLE_END",
    "#FLOW_START",
    "#FLOW_END",
    "#SEPARATOR",
    "#FIGURE:",
    "#CORRECTION_START",
    "#CORRECTION_END",
)

#: "Figure:" caption prefix, per output language.
FIGURE_CAPTION_PREFIX = {"en": "Figure: ", "hi": "चित्र: "}

# --------------------------------------------------------------------------
# Style names. Other modules (and tests) address styles by these exact names.
# --------------------------------------------------------------------------
STYLE_SUBJECT = "UPSC_DCN_Subject"
STYLE_TITLE = "UPSC_DCN_Title"
STYLE_H1 = "UPSC_DCN_H1_Red"
STYLE_H2 = "UPSC_DCN_H2_Blue"
STYLE_H3 = "UPSC_DCN_H3_Black"
STYLE_H4 = "UPSC_DCN_H4_Grey"
STYLE_BODY = "UPSC_DCN_Body"
STYLE_BULLET_1 = "UPSC_DCN_Bullet_Level_1"
STYLE_BULLET_2 = "UPSC_DCN_Bullet_Level_2"
STYLE_BULLET_3 = "UPSC_DCN_Bullet_Level_3"
STYLE_TABLE = "UPSC_DCN_Table"
STYLE_FLOW = "UPSC_DCN_Flow"
STYLE_CAUTION = "UPSC_DCN_Caution"
STYLE_TOPICS_HEADING = "UPSC_DCN_Topics_Heading"
STYLE_FIGURE = "UPSC_DCN_Figure"
STYLE_FIGURE_CAPTION = "UPSC_DCN_Figure_Caption"

_BULLET_STYLES = (STYLE_BULLET_1, STYLE_BULLET_2, STYLE_BULLET_3)
# Level 1 solid disc, level 2 hollow circle, level 3 small filled square, drawn
# in DCN_BULLET_FONT at body size — which is what the original does.
_BULLET_GLYPHS = DCN_BULLET_GLYPHS
# Indents as the original measures them: 0.5 / 1.0 / 1.5 in from the text
# margin, with a 0.25 in hang, so a level-1 disc sits at 0.25 in and its text at
# 0.5 in. A wrapped line aligns under the text, not under the glyph.
#
# The hanging distance is also the widest the glyph may be: Word advances from
# the glyph to the text with a TAB, and a glyph wider than the hanging indent
# overshoots to the next default tab stop. Arial's disc is 9.7 pt at 11 pt
# against an 18 pt hang, so it clears comfortably at body size.
_BULLET_INDENTS_TWIPS = tuple(
    DCN_BULLET_INDENT_STEP_TWIPS * (level + 1) for level in range(DCN_BULLET_LEVELS)
)
_BULLET_HANGING_TWIPS = DCN_BULLET_HANGING_TWIPS
_BULLET_NUM_NAME = "UPSC_DCN_Bullets"

# Corrections stay inside the student notes (hard rule), so they get their own
# on-palette treatment rather than being hidden or moved to an appendix.
_COLOR_CAUTION = DCN_COLOR_H1
_COLOR_RULE = DCN_COLOR_TABLE_RULE      # table gridlines
# The original leaves its header row unfilled and lets bold carry it; this
# app had been shading the band in the footer's blue. None means no shading.
_COLOR_TABLE_HEADER_FILL = DCN_TABLE_HEADER_FILL

_TWIPS_PER_IN = 1440
_CONTENT_WIDTH_TWIPS = int(round(DCN_CONTENT_WIDTH_IN * _TWIPS_PER_IN))  # 8424
# The footer strip is a one-row, three-cell table spanning the full page width
# (see _build_footer). In page coordinates the original starts its left label at
# the 0.5 in margin, centres the page number at 4.125 in and ends its right label
# at 7.75 in, i.e. 0.75 in inside the page edge; the cells are cut so the centre
# cell is centred on that 4.125 in.
_FOOTER_PAGE_WIDTH_TWIPS = int(DCN_PAGE_WIDTH_IN * _TWIPS_PER_IN)
_FOOTER_LEFT_INSET_TWIPS = int(DCN_MARGIN_LEFT_IN * _TWIPS_PER_IN)
_FOOTER_RIGHT_INSET_TWIPS = int(0.75 * _TWIPS_PER_IN)
_FOOTER_NUMBER_CELL_TWIPS = int(1.5 * _TWIPS_PER_IN)
_FOOTER_LEFT_CELL_TWIPS = int(4.125 * _TWIPS_PER_IN) - _FOOTER_NUMBER_CELL_TWIPS // 2
_FOOTER_RIGHT_CELL_TWIPS = (_FOOTER_PAGE_WIDTH_TWIPS - _FOOTER_LEFT_CELL_TWIPS
                            - _FOOTER_NUMBER_CELL_TWIPS)

# Header furniture (see DCN contract §1), now sized from config: the logo band
# has to fit inside DCN_MARGIN_TOP_IN or Word pushes the body back down and the
# compact top margin buys nothing.
_HEADER_DISTANCE_IN = DCN_HEADER_DISTANCE_IN
_FOOTER_DISTANCE_IN = DCN_FOOTER_DISTANCE_IN
_LOGO_BOX_W_IN = DCN_LOGO_BOX_W_IN
_LOGO_BOX_H_IN = DCN_LOGO_BOX_H_IN
# Where the reference puts the logo: its header anchors the image 1647825 EMU
# from the text column's left edge. Centring the paragraph instead is only the
# same thing for a logo exactly as wide as the box — the square PW mark landed
# near enough by luck, the full lockup does not. Held as twips because that is
# what w:ind takes: 1647825 / 914400 * 1440.
_LOGO_INDENT_TWIPS = int(round(1647825 / 914400 * _TWIPS_PER_IN))  # 2595

NOTES_BOX_CAPTION = "Space for Notes"
#: The face for page furniture — footer labels, page number, notes-box caption.
#: Always the Latin handwritten face, never the body face: the original prints
#: "PW OnlyIAS / Daily Class Notes / Space for Notes" in Coming Soon on its
#: Hindi notes too, because those words are the institute's, not the lecture's.
_FURNITURE_FONT = DCN_FONT_PRIMARY
_EMU_PER_IN = 914400
_EMU_PER_PT = 12700
# The anchor's horizontal offset is measured from the TEXT COLUMN, not from the
# paper, so it has to move with the left margin — the reference's literal
# 5267325 EMU was only right while that margin was 0.5 in. Config holds the box
# in page coordinates (the thing that actually has to stay true: clear of the
# content column on the left, clear of the paper edge on the right) and the
# conversion happens here.
_NOTES_BOX_X_EMU = int(round((DCN_NOTES_BOX_LEFT_IN - DCN_MARGIN_LEFT_IN) * _EMU_PER_IN))
_NOTES_BOX_Y_EMU = int(round(DCN_NOTES_BOX_TOP_IN * _EMU_PER_IN))
_NOTES_BOX_CX_EMU = int(round(DCN_NOTES_BOX_WIDTH_IN * _EMU_PER_IN))
_NOTES_BOX_CY_EMU = int(round(DCN_NOTES_BOX_HEIGHT_IN * _EMU_PER_IN))

# Namespaces python-docx does not register in its own nsmap; the raw-OOXML
# helpers below declare them locally on the elements they build.
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_NS_V = "urn:schemas-microsoft-com:vml"

_TAG_ALTERNATE_CONTENT = f"{{{_NS_MC}}}AlternateContent"
_TAG_WPS_TXBX = f"{{{_NS_WPS}}}txbx"
_TAG_V_TEXTBOX = f"{{{_NS_V}}}textbox"

_MARKER_TEXT_RE = re.compile(r"^#(LECTURE_TITLE|H1|H2|H3|H4)\s*:\s*(.*)$", re.I)
_MARKER_BARE_RE = re.compile(
    r"^#(TABLE_START|TABLE_END|FLOW_START|FLOW_END|SEPARATOR"
    r"|TOPICS_COVERED_START|TOPICS_COVERED_END"
    r"|CORRECTION_START|CORRECTION_END)\s*$", re.I)
_FIGURE_LINE_RE = re.compile(r"^#FIGURE:\s*(\S+)\s*$", re.I)
_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*•●○◦▪■–])[ \t]+(?P<text>.*)$")
#: A genuine sequence step ("1. heat the mixture"). Only 1-2 digits: a year
#: ("1972.") must never be read as a step number.
_ORDERED_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<num>\d{1,2})[.)][ \t]+(?P<text>.*)$")
#: Field lines inside a #CORRECTION block.
_CORRECTION_FIELD_RE = re.compile(r"^\s*(SOURCE|CORRECTED|REASON)\s*:\s*(.*)$", re.I)
# Longest emphasis run first so ***x*** is consumed before **x** before *x*.
_MD_RE = re.compile(r"(\*\*\*[^*]+?\*\*\*|\*\*[^*]+?\*\*|\*[^*\s][^*]*?\*)")
_STRAY_EMPHASIS_RE = re.compile(r"\*{2,}")
_TABLE_RULE_ROW_RE = re.compile(r"^[\s|:.-]+$")

_CAUTION_PREFIX = "[Needs Verification:"
_CAUTION_PHRASES = ("UPSC caution:", "Important distinction:", "Important current update:")
#: The label is DETECTED but never PRINTED. Writing "UPSC Caution:" in front of
#: the sentence says in words what the highlight already says in colour, and it
#: is the model's scaffolding rather than something the faculty taught. The
#: paragraph keeps its full meaning without it.
#: Matches the label with its emphasis markers however the model wrapped them:
#: ``UPSC caution:``, ``**UPSC Caution:**`` and ``**UPSC Caution**:`` all occur.
#: The closing ``**`` is only consumed as part of one of those shapes — a bare
#: ``*`` starting an italic word right after the colon must survive, or the
#: emphasis it opens is orphaned and the asterisk prints.
_CAUTION_LABEL_RE = re.compile(
    r"^\s*\*{0,3}\s*"
    r"(?:UPSC\s+caution|Important\s+distinction|Important\s+current\s+update)"
    r"\s*(?:\*{2,3}\s*:|:\s*\*{2,3}|:|\*{2,3})\s*[-–]?\s*", re.I)
#: "[Needs Verification: the figure moves every cycle]" -> the sentence inside.
_CAUTION_BRACKET_RE = re.compile(
    r"^\s*\[\s*Needs\s+Verification\s*:?\s*(?P<body>.*?)\s*\]\s*$", re.I | re.S)

# Codepoints the handwritten face is assumed to carry. The embedded TTF is
# obfuscated per ECMA-376 §15.2.13 so its cmap cannot be read back — this is a
# deliberately conservative stand-in: Basic Latin, Latin-1 Supplement (covers
# ° ² ³ µ × ÷) and the punctuation block every Latin text face ships. Anything
# else (₹ ≥ ≤ ≠ → ↓ and subscript digits) gets DCN_FONT_SYMBOL for that run only.
_TYPOGRAPHIC_PUNCTUATION = frozenset(
    "‐‑‒–—―‘’‚‛“”„"
    "†‡•…‰‹›€™"
)


# --------------------------------------------------------------------------
# Parsed representation
# --------------------------------------------------------------------------
@dataclass
class _Block:
    """One rendered unit of the notes: a paragraph, a table or a rule."""

    kind: str
    text: str = ""
    level: int = 0
    caution: bool = False
    rows: list[list[str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _StyleSpec:
    name: str
    size_pt: float
    color: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    alignment: int = WD_ALIGN_PARAGRAPH.LEFT
    space_before_pt: float = DCN_SPACE_BODY_PT[0]
    space_after_pt: float = DCN_SPACE_BODY_PT[1]
    line_spacing: float = DCN_LINE_SPACING
    left_indent_twips: int = 0
    hanging_twips: int = 0
    keep_with_next: bool = False


# Sizes, colours AND spacing all come from config so a single edit moves the
# writer, the render check and the acceptance tests together. Nothing in this
# table is a literal measurement any more — that is what makes the compact pass
# reversible. Indents are explicit everywhere because the reference template's
# docDefaults carry ind left=1440: anything that does not override it inherits a
# 1 in indent it never asked for, which is most of the wasted left margin the
# compact pass exists to remove.
#
# Headings are intentionally NOT underlined. The PW reference template
# underlines them; product decision is to drop it. Do not "fix" back.
#
# Line spacing is DCN_LINE_SPACING (was a flat 1.15). Tables and flow steps run
# at DCN_LINE_SPACING_TIGHT: they are short, already boxed by their borders, and
# were the single largest source of dead vertical space.
_STYLE_SPECS: tuple[_StyleSpec, ...] = (
    # Level 1 of the title block: the SUBJECT. Sits above the lecture line, and
    # is the only thing on the page allowed to be larger than the title.
    _StyleSpec(STYLE_SUBJECT, DCN_SIZE_SUBJECT_PT, DCN_COLOR_TITLE, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.CENTER,
               space_before_pt=DCN_SPACE_SUBJECT_PT[0], space_after_pt=DCN_SPACE_SUBJECT_PT[1],
               keep_with_next=True),
    # Level 2: lecture number + lecture title.
    _StyleSpec(STYLE_TITLE, DCN_SIZE_TITLE_PT, DCN_COLOR_TITLE, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.CENTER,
               space_before_pt=DCN_SPACE_TITLE_PT[0], space_after_pt=DCN_SPACE_TITLE_PT[1],
               keep_with_next=True),
    _StyleSpec(STYLE_H1, DCN_SIZE_H1_PT, DCN_COLOR_H1, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_H1_PT[0], space_after_pt=DCN_SPACE_H1_PT[1],
               keep_with_next=True),
    _StyleSpec(STYLE_H2, DCN_SIZE_H2_PT, DCN_COLOR_H2, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_H2_PT[0], space_after_pt=DCN_SPACE_H2_PT[1],
               keep_with_next=True),
    _StyleSpec(STYLE_H3, DCN_SIZE_H3_PT, DCN_COLOR_H3, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_H3_PT[0], space_after_pt=DCN_SPACE_H3_PT[1],
               keep_with_next=True),
    # Fourth level: grey, one point above body. Separated from H3 by colour
    # rather than by size, which is how the original keeps four levels legible
    # in a column this narrow.
    _StyleSpec(STYLE_H4, DCN_SIZE_H4_PT, DCN_COLOR_H4, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_H4_PT[0], space_after_pt=DCN_SPACE_H4_PT[1],
               keep_with_next=True),
    _StyleSpec(STYLE_BODY, DCN_SIZE_BODY_PT, DCN_COLOR_BODY),
    _StyleSpec(STYLE_BULLET_1, DCN_SIZE_BODY_PT, DCN_COLOR_BODY,
               space_after_pt=DCN_SPACE_BULLET_PT[1],
               left_indent_twips=_BULLET_INDENTS_TWIPS[0], hanging_twips=_BULLET_HANGING_TWIPS),
    _StyleSpec(STYLE_BULLET_2, DCN_SIZE_BODY_PT, DCN_COLOR_BODY,
               space_after_pt=DCN_SPACE_BULLET_PT[1],
               left_indent_twips=_BULLET_INDENTS_TWIPS[1], hanging_twips=_BULLET_HANGING_TWIPS),
    _StyleSpec(STYLE_BULLET_3, DCN_SIZE_BODY_PT, DCN_COLOR_BODY,
               space_after_pt=DCN_SPACE_BULLET_PT[1],
               left_indent_twips=_BULLET_INDENTS_TWIPS[2], hanging_twips=_BULLET_HANGING_TWIPS),
    _StyleSpec(STYLE_TABLE, DCN_SIZE_BODY_PT, DCN_COLOR_BODY, alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_TABLE_PT[0], space_after_pt=DCN_SPACE_TABLE_PT[1],
               line_spacing=DCN_LINE_SPACING_TIGHT),
    _StyleSpec(STYLE_FLOW, DCN_SIZE_BODY_PT, DCN_COLOR_BODY, alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_after_pt=DCN_SPACE_FLOW_PT[1], line_spacing=DCN_LINE_SPACING_TIGHT),
    # A caution used to be red italic behind a written-out "UPSC Caution:"
    # label. It is now ordinary body text with the label removed and the
    # sentence shaded — the highlight IS the marker, so saying it in words as
    # well was saying it twice. Italic stays: it is an aside, and the slant
    # separates it from a shaded QC correction inside a normal sentence.
    _StyleSpec(STYLE_CAUTION, DCN_SIZE_BODY_PT, DCN_COLOR_BODY, italic=True,
               space_before_pt=DCN_SPACE_CAUTION_PT[0], space_after_pt=DCN_SPACE_CAUTION_PT[1],
               left_indent_twips=DCN_CAUTION_INDENT_TWIPS),
    # "Topics Covered in the Class:" — no longer rendered (the reference has no
    # such block); the style stays defined so a customised template that maps
    # it keeps working.
    _StyleSpec(STYLE_TOPICS_HEADING, DCN_SIZE_H2_PT, DCN_COLOR_H1, bold=True,
               alignment=WD_ALIGN_PARAGRAPH.LEFT,
               space_before_pt=DCN_SPACE_H2_PT[0], space_after_pt=DCN_SPACE_H2_PT[1],
               keep_with_next=True),
    # A slide diagram and its caption. keep_with_next binds the image paragraph
    # to the caption, so the pair moves to the next page as one block (the
    # image itself is inline and can never split across pages).
    _StyleSpec(STYLE_FIGURE, DCN_SIZE_BODY_PT, DCN_COLOR_BODY,
               alignment=WD_ALIGN_PARAGRAPH.CENTER,
               space_before_pt=DCN_SPACE_FIGURE_PT[0], space_after_pt=DCN_SPACE_FIGURE_PT[1],
               keep_with_next=True),
    _StyleSpec(STYLE_FIGURE_CAPTION, DCN_SIZE_BODY_PT, DCN_COLOR_BODY, italic=True,
               alignment=WD_ALIGN_PARAGRAPH.CENTER,
               space_before_pt=DCN_SPACE_FIGURE_CAPTION_PT[0],
               space_after_pt=DCN_SPACE_FIGURE_CAPTION_PT[1]),
)

# Keep the custom DCN colours and spacing while making the marker hierarchy a
# real Word outline. A larger font alone is not a heading to Word's navigation
# pane, screen readers, or exported document structure.
_HEADING_OUTLINE_LEVELS = {
    STYLE_H1: 0,
    STYLE_H2: 1,
    STYLE_H3: 2,
    STYLE_H4: 3,
}


# --------------------------------------------------------------------------
# Small OOXML helpers
# --------------------------------------------------------------------------
def _warn(warnings: list[str], message: str) -> None:
    """Record a warning once — malformed input tends to repeat the same fault."""
    if message not in warnings:
        warnings.append(message)


# w:pPr, w:tcPr and w:tblPr are XSD *sequences*, not bags: a child appended out
# of order makes Word reject the part. These are the schema orders for the
# elements this module touches.
_PPR_ORDER = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl",
    "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens",
    "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
    "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection",
    "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
)
# EG_RPrBase, in schema order. Only the members this module can meet are listed
# up to w:shd; anything after it just has to sort later, which the tail covers.
_RPR_ORDER = (
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps",
    "w:strike", "w:dstrike", "w:outline", "w:shadow", "w:emboss", "w:imprint",
    "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing",
    "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang",
    "w:eastAsianLayout", "w:specVanish", "w:oMath",
)
_TCPR_ORDER = (
    "w:cnfStyle", "w:tcW", "w:gridSpan", "w:hMerge", "w:vMerge", "w:tcBorders", "w:shd",
    "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark",
)
_TBLPR_ORDER = (
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize",
    "w:tblStyleColBandSize", "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd",
    "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
    "w:tblCaption", "w:tblDescription", "w:tblPrChange",
)


def _ordered_child(parent, tag: str, order: tuple[str, ...]):
    """Fetch, or create in schema position, a direct child of a properties element."""
    existing = parent.find(qn(tag))
    if existing is not None:
        return existing
    element = OxmlElement(tag)
    for successor in order[order.index(tag) + 1:]:
        found = parent.find(qn(successor))
        if found is not None:
            found.addprevious(element)
            return element
    parent.append(element)
    return element


def _set_ind(ppr, *, left: int = 0, right: int = 0, first_line: int = 0, hanging: int = 0) -> None:
    ind = _ordered_child(ppr, "w:ind", _PPR_ORDER)
    ind.set(qn("w:left"), str(left))
    ind.set(qn("w:right"), str(right))
    if hanging:
        ind.attrib.pop(qn("w:firstLine"), None)
        ind.set(qn("w:hanging"), str(hanging))
    else:
        ind.attrib.pop(qn("w:hanging"), None)
        ind.set(qn("w:firstLine"), str(first_line))


def _set_outline_level(ppr, level: int) -> None:
    """Mark a custom visual style as a real Word heading level."""
    outline = _ordered_child(ppr, "w:outlineLvl", _PPR_ORDER)
    outline.set(qn("w:val"), str(level))


def _set_shading(pr, fill: str, order: tuple[str, ...] = _PPR_ORDER) -> None:
    """Paint a solid background on a paragraph (pPr) or a table cell (tcPr)."""
    shd = _ordered_child(pr, "w:shd", order)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)


def _sync_cs_twins(rpr) -> None:
    """Mirror w:sz→w:szCs and w:b→w:bCs on a run or style rPr.

    Devanagari resolves through the complex-script slot and reads ONLY the
    *Cs twins: without them every Hindi heading renders at the docDefaults
    szCs (11pt) and bold is dropped. The reference template carries szCs on
    every style; this keeps run-level formatting to the same standard.
    """
    sz = rpr.find(qn("w:sz"))
    if sz is not None:
        szcs = rpr.find(qn("w:szCs"))
        if szcs is None:
            szcs = OxmlElement("w:szCs")
            sz.addnext(szcs)
        szcs.set(qn("w:val"), sz.get(qn("w:val")))
    b = rpr.find(qn("w:b"))
    if b is not None and b.get(qn("w:val")) not in ("0", "false"):
        if rpr.find(qn("w:bCs")) is None:
            b.addnext(OxmlElement("w:bCs"))


def _set_run_fonts(run, family: str) -> None:
    """Name the face on every script slot of a single run.

    w:cs stays on the Devanagari face: Word classifies Devanagari as a complex
    script and reads w:cs for it, so one run can carry English in the
    handwritten face and any stray Hindi term in Kalam without being split.
    (In the Hindi build `family` IS Kalam, so all four slots agree.)
    """
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), family)
    rfonts.set(qn("w:hAnsi"), family)
    rfonts.set(qn("w:eastAsia"), family)
    rfonts.set(qn("w:cs"),
               family if family in (DCN_FONT_SYMBOL, DCN_BULLET_FONT) else DEVANAGARI_FONT)
    _sync_cs_twins(rpr)


def _set_run_shading(run, fill: str) -> None:
    """Paint a background behind one run — the yellow correction highlight.

    Run shading (w:shd on rPr), not w:highlight: Word's highlight attribute
    takes a NAME from a fixed sixteen-colour palette whose "yellow" is FFFF00,
    which is too dark to read Devanagari through and prints as a solid block.
    w:shd takes any RGB, is honoured by Word and LibreOffice alike, and both
    carry it into PDF.
    """
    # Placed through _ordered_child, not appended: w:rPr is an XSD sequence, and
    # w:shd has to precede w:vertAlign / w:rtl / w:cs / w:lang. A bare append
    # happens to be valid only while no run carries one of those — a superscript
    # or a w:lang added upstream would silently make the part unreadable.
    shd = _ordered_child(run._element.get_or_add_rPr(), "w:shd", _RPR_ORDER)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)


# --------------------------------------------------------------------------
# Text runs: emphasis, then glyph coverage
# --------------------------------------------------------------------------
def _strip_stray_emphasis(text: str) -> str:
    """Drop unbalanced ** so no literal asterisk pair reaches the student.

    The surrounding whitespace is collapsed with it: "an unbalanced ** pair"
    would otherwise render as "an unbalanced  pair", and a double space is
    visible on the page — especially in justified text, where it widens the
    gap the line is stretched around.
    """
    cleaned = _STRAY_EMPHASIS_RE.sub("", text)
    if cleaned == text:
        return cleaned
    # Only runs of spaces/tabs are collapsed; newlines are structural here.
    collapsed = re.sub(r"[ \t]{2,}", " ", cleaned)
    # A marker at the very start or end leaves an edge space behind.
    if text[:1] not in (" ", "\t") and collapsed[:1] == " ":
        collapsed = collapsed.lstrip(" \t")
    if text[-1:] not in (" ", "\t") and collapsed[-1:] == " ":
        collapsed = collapsed.rstrip(" \t")
    return collapsed


def _has_handwritten_glyph(ch: str) -> bool:
    cp = ord(ch)
    if cp <= 0x7E:
        return True
    if 0xA0 <= cp <= 0xFF:
        return True
    if ch in _TYPOGRAPHIC_PUNCTUATION:
        return True
    if 0x0900 <= cp <= 0x097F:      # Devanagari — handled by w:cs, not a split
        return True
    return False


def _split_by_font(text: str, primary_font: str = DCN_FONT_PRIMARY) -> list[tuple[str, str]]:
    """Chop text into (chunk, font family) runs at glyph-coverage boundaries."""
    return [("".join(chars), family)
            for family, chars in groupby(
                text, key=lambda ch: primary_font if _has_handwritten_glyph(ch) else DCN_FONT_SYMBOL)]


# Markdown link: [label](https://...). The QUICK REVISION facts and Key Sources
# carry their citations this way; anywhere else in the notes works too.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")
# Match emphasis and links together so emphasis around a link is not split
# into orphan markers, and emphasis inside a link label is retained.
_INLINE_RE = re.compile(_MD_LINK_RE.pattern + "|" + _MD_RE.pattern)
_COLOR_HYPERLINK = DCN_COLOR_H2


def _add_hyperlink(paragraph, label: str, url: str, *, primary_font: str,
                   highlight: bool = False, bold: bool = False,
                   italic: bool = False) -> None:
    """One real w:hyperlink: blue, underlined, opening `url`.

    Degrades to plain text when the relationship cannot be created — a dead
    label is better than a crash, and the sentence still reads.
    """
    try:
        r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    except Exception:  # noqa: BLE001 — e.g. header/footer part refuses the rel
        r_id = ""
    hyperlink = None
    if r_id:
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), r_id)
        paragraph._p.append(hyperlink)
    for chunk, family in _split_by_font(label, primary_font):
        run = paragraph.add_run(chunk)
        run.bold = bold
        run.italic = italic
        run.underline = True
        run.font.color.rgb = RGBColor.from_string(_COLOR_HYPERLINK)
        _set_run_fonts(run, family)
        if highlight:
            _set_run_shading(run, DCN_HIGHLIGHT_COLOR)
        if hyperlink is not None:
            hyperlink.append(run._r)


@dataclass(frozen=True)
class _Piece:
    """One stretch of VISIBLE text, with what the markdown decided about it.

    "Visible" is the load-bearing word: ``text`` is what a reader sees, with the
    ``**`` markers and the ``(url)`` half of a link already removed. Highlight
    matching runs over these, never over the raw marker text — see _Highlighter.
    """

    text: str
    bold: bool = False
    italic: bool = False
    url: str = ""
    highlight: bool = False


def _parse_pieces(text: str, *, bold: bool = False, italic: bool = False) -> list[_Piece]:
    """Markdown and links resolved into visible pieces, in reading order."""
    out: list[_Piece] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        plain = _strip_stray_emphasis(text[position:match.start()])
        if plain:
            out.append(_Piece(plain, bold, italic))
        token = match.group(0)
        link = _MD_LINK_RE.fullmatch(token)
        if link:
            label, url = link.groups()
            pieces = _parse_pieces(label, bold=bold, italic=italic)
            out.extend(replace(piece, url=url) for piece in pieces)
        else:
            width = 3 if token.startswith("***") else 2 if token.startswith("**") else 1
            out.extend(_parse_pieces(
                token[width:-width], bold=bold or width >= 2,
                italic=italic or width in (1, 3)))
        position = match.end()
    plain = _strip_stray_emphasis(text[position:])
    if plain:
        out.append(_Piece(plain, bold, italic))
    return out


def visible_text(text: str) -> str:
    """What ``text`` looks like on the page: no ``**``, no URLs, just words."""
    return "".join(piece.text for piece in _parse_pieces(text))


class _Highlighter:
    """Locates the QC-corrected spans inside one paragraph's visible text.

    The spans arrive already narrowed and already proven to occur in the notes
    (see src/correction_highlights.py); this class only has to find them again
    inside a paragraph and say which characters they cover.

    Matching is done on VISIBLE text, on both sides. The first attempt at this
    matched the raw marker text and split it before the markdown pass, which
    worked until a correction landed inside a ``**bold**`` phrase: cutting
    between the ``**`` and the words orphaned both markers, so the phrase came
    out highlighted but no longer bold. Comparing what a reader sees against
    what a reader sees has no such seam — a highlight now composes with bold,
    italic, links, Devanagari and the symbol-font split instead of competing
    with them.

    Matching is leftmost-longest and non-overlapping: with the spans sorted by
    length, a correction that is a substring of a longer one can never split it
    in two. A span that straddles a block boundary (a table cell, a bullet) is
    simply not found and that text renders unhighlighted — the correction is
    still in the notes and still in the QC report, so a missed highlight costs a
    reviewer a lookup, never a fact.
    """

    __slots__ = ("_spans", "_starts")

    def __init__(self, spans) -> None:
        cleaned = set()
        for span in spans or ():
            if not isinstance(span, str):
                continue
            visible = visible_text(span).strip()
            if visible:
                cleaned.add(visible)
        self._spans = sorted(cleaned, key=len, reverse=True)
        self._starts = {s[0] for s in self._spans}

    def __bool__(self) -> bool:
        return bool(self._spans)

    def ranges(self, text: str) -> list[tuple[int, int]]:
        """Non-overlapping (start, end) offsets of every span found in `text`."""
        if not text or not self._spans:
            return []
        found: list[tuple[int, int]] = []
        index = 0
        length = len(text)
        while index < length:
            if text[index] in self._starts:
                hit = next((s for s in self._spans if text.startswith(s, index)), None)
                if hit is not None:
                    found.append((index, index + len(hit)))
                    index += len(hit)
                    continue
            index += 1
        return found

    def apply(self, pieces: list[_Piece]) -> list[_Piece]:
        """The same pieces, cut at highlight boundaries and flagged."""
        if not self._spans or not pieces:
            return pieces
        found = self.ranges("".join(piece.text for piece in pieces))
        if not found:
            return pieces
        out: list[_Piece] = []
        offset = 0
        for piece in pieces:
            start, end = offset, offset + len(piece.text)
            offset = end
            covered = [r for r in found if r[0] < end and r[1] > start]
            if not covered:
                out.append(piece)
                continue
            if piece.url:
                # A link is one run or none: splitting it would emit the same
                # relationship twice and break the label in half mid-click.
                out.append(_Piece(piece.text, piece.bold, piece.italic, piece.url,
                                  any(a <= start and end <= b for a, b in covered)))
                continue
            cuts = sorted({start, end}
                          | {c for a, b in covered for c in (a, b) if start < c < end})
            for a, b in zip(cuts, cuts[1:]):
                chunk = piece.text[a - start:b - start]
                if not chunk:
                    continue
                hot = any(r_start <= a and b <= r_end for r_start, r_end in covered)
                out.append(_Piece(chunk, piece.bold, piece.italic, "", hot))
        return out


def _add_text_runs(
    paragraph,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    underline: bool | None = None,
    color: str | None = None,
    primary_font: str = DCN_FONT_PRIMARY,
    highlighter: "_Highlighter | None" = None,
) -> None:
    """Add `text` to `paragraph` as real runs: links, **bold**, *italic*, glyph-safe.

    Anything not overridden here is left to inherit from the paragraph style, so
    a heading keeps its own size and colour. ``highlighter`` additionally shades
    the QC-corrected spans light yellow.
    """
    pieces = _parse_pieces(text, bold=bold, italic=italic)
    if highlighter is not None and highlighter:
        pieces = highlighter.apply(pieces)
    for piece in pieces:
        if piece.url:
            _add_hyperlink(paragraph, piece.text, piece.url, primary_font=primary_font,
                           highlight=piece.highlight, bold=piece.bold, italic=piece.italic)
            continue
        for chunk, family in _split_by_font(piece.text, primary_font):
            run = paragraph.add_run(chunk)
            run.bold = piece.bold
            run.italic = piece.italic
            if underline is not None:
                run.underline = underline
            if color is not None:
                run.font.color.rgb = RGBColor.from_string(color)
            _set_run_fonts(run, family)
            if piece.highlight:
                _set_run_shading(run, DCN_HIGHLIGHT_COLOR)


# --------------------------------------------------------------------------
# Styles and numbering
# --------------------------------------------------------------------------
def _style_exists(doc, name: str) -> bool:
    try:
        doc.styles[name]
    except KeyError:
        return False
    return True


def _ensure_styles(doc, warnings: list[str], *, primary_font: str = DCN_FONT_PRIMARY) -> None:
    """Create the DCN paragraph styles, reusing any the template already has.

    ``primary_font`` fills w:ascii / w:hAnsi / w:eastAsia; w:cs is always the
    Devanagari face (which in the Hindi build is the same font, so all four
    slots agree). Every style also carries the complex-script twins
    (w:szCs = w:sz, w:bCs beside w:b) — Devanagari reads ONLY those, so a
    style without them renders Hindi headings at 11pt regular.
    """
    for spec in _STYLE_SPECS:
        if _style_exists(doc, spec.name):
            continue                       # a customised template wins
        try:
            style = doc.styles.add_style(spec.name, WD_STYLE_TYPE.PARAGRAPH)
        except Exception as exc:           # pragma: no cover - malformed styles.xml
            _warn(warnings, f"Could not create style {spec.name} ({exc}); used direct formatting.")
            continue
        style.quick_style = True
        font = style.font
        font.name = primary_font
        font.size = Pt(spec.size_pt)
        font.bold = spec.bold
        font.italic = spec.italic
        # Headings are intentionally NOT underlined. The PW reference template
        # underlines them; product decision is to drop it. Do not "fix" back.
        font.underline = spec.underline
        font.color.rgb = RGBColor.from_string(spec.color)
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        rfonts.set(qn("w:ascii"), primary_font)
        rfonts.set(qn("w:hAnsi"), primary_font)
        rfonts.set(qn("w:eastAsia"), primary_font)
        rfonts.set(qn("w:cs"), DEVANAGARI_FONT if primary_font != DCN_FONT_SYMBOL else primary_font)
        _sync_cs_twins(rpr)

        pf = style.paragraph_format
        pf.alignment = spec.alignment
        pf.space_before = Pt(spec.space_before_pt)
        pf.space_after = Pt(spec.space_after_pt)
        pf.line_spacing = spec.line_spacing
        pf.keep_with_next = spec.keep_with_next
        ppr = style.element.get_or_add_pPr()
        _set_ind(ppr, left=spec.left_indent_twips, hanging=spec.hanging_twips)

    # Templates may supply the visual styles themselves, so do this in a
    # separate pass rather than only while creating a missing style.
    for style_name, level in _HEADING_OUTLINE_LEVELS.items():
        try:
            style = doc.styles[style_name]
        except KeyError:
            continue
        _set_outline_level(style.element.get_or_add_pPr(), level)


_STYLES_WITH_EFFECTS_RELTYPE = (
    "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects"
)


def _drop_styles_with_effects(document) -> None:
    """Drop the Word-2010 compatibility stylesheet python-docx's template ships.

    word/stylesWithEffects.xml duplicates every built-in style — drawn borders
    included — outside the reach of the styles API, so the border scrub below
    cannot see it. The reference document carries no such part, and Word
    regenerates it on demand; unreferencing it removes it from the package.
    """
    part = document.part
    for rel_id, rel in list(part.rels.items()):
        if rel.reltype == _STYLES_WITH_EFFECTS_RELTYPE:
            part.drop_rel(rel_id)


def _strip_single_borders(document) -> None:
    """Remove every drawn (w:val="single") paragraph border from the stylesheet.

    The generated body emits no rules at all, but python-docx's default
    template ships built-in styles (Title, Intense Quote...) that carry one.
    They are never applied here, yet they keep a drawn <w:pBdr> in the package
    — and "no horizontal rules" is a contract on the file, not on our code
    paths. Inert borders (w:val="none", as the reference carries) are left.
    """
    for pbdr in document.styles.element.iter(qn("w:pBdr")):
        for edge in list(pbdr):
            if edge.get(qn("w:val")) == "single":
                pbdr.remove(edge)
        if not len(pbdr):
            pbdr.getparent().remove(pbdr)


def _abstract_num_xml(abstract_id: int) -> str:
    glyph_half_points = int(round(DCN_BULLET_GLYPH_SIZE_PT * 2))
    levels = []
    for ilvl in range(9):
        glyph = _BULLET_GLYPHS[ilvl % len(_BULLET_GLYPHS)]
        # Nine levels from a three-level ladder: the indent keeps stepping even
        # though the glyphs repeat, so a (never-emitted) level 4 still sits in.
        left = DCN_BULLET_INDENT_STEP_TWIPS * (ilvl + 1)
        levels.append(
            f'<w:lvl w:ilvl="{ilvl}">'
            f'<w:start w:val="1"/><w:numFmt w:val="bullet"/>'
            f'<w:lvlText w:val="{glyph}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{left}" w:hanging="{_BULLET_HANGING_TWIPS}"/></w:pPr>'
            # The bullet glyph is named explicitly: the handwritten face may
            # not carry the discs, and an embedded font gives the renderer no
            # chance to substitute silently. w:sz/w:szCs size the MARKER only —
            # the text of the bullet keeps the paragraph style's body size, so
            # the two can still be tuned apart if they ever need to be.
            f'<w:rPr><w:rFonts w:ascii="{DCN_BULLET_FONT}" w:hAnsi="{DCN_BULLET_FONT}"'
            f' w:cs="{DCN_BULLET_FONT}"/>'
            f'<w:sz w:val="{glyph_half_points}"/><w:szCs w:val="{glyph_half_points}"/>'
            f'<w:u w:val="none"/></w:rPr>'
            f"</w:lvl>"
        )
    return (
        f'<w:abstractNum xmlns:w="{_NS_W}" w:abstractNumId="{abstract_id}">'
        f'<w:multiLevelType w:val="hybridMultilevel"/>'
        f'<w:name w:val="{_BULLET_NUM_NAME}"/>'
        f'{"".join(levels)}</w:abstractNum>'
    )


def _int_attr(element, attr: str) -> int | None:
    try:
        return int(element.get(qn(attr)))
    except (TypeError, ValueError):
        return None


def _ensure_bullet_numbering(doc, warnings: list[str]) -> int | None:
    """Return the numId of the DCN bullet list, defining it if absent.

    Real Word numbering (rather than typed glyphs) is what makes the hanging
    indent line wrapped text up under the text instead of under the bullet.
    """
    try:
        numbering = doc.part.numbering_part.element
    except Exception as exc:
        _warn(warnings, f"No numbering part available ({exc}); bullets use literal glyphs.")
        return None

    for abstract in numbering.findall(qn("w:abstractNum")):
        name = abstract.find(qn("w:name"))
        if name is None or name.get(qn("w:val")) != _BULLET_NUM_NAME:
            continue
        abstract_id = abstract.get(qn("w:abstractNumId"))
        for num in numbering.findall(qn("w:num")):
            ref = num.find(qn("w:abstractNumId"))
            if ref is not None and ref.get(qn("w:val")) == abstract_id:
                existing = _int_attr(num, "w:numId")
                if existing is not None:
                    return existing

    abstract_ids = [_int_attr(a, "w:abstractNumId") for a in numbering.findall(qn("w:abstractNum"))]
    num_ids = [_int_attr(n, "w:numId") for n in numbering.findall(qn("w:num"))]
    abstract_id = max([i for i in abstract_ids if i is not None], default=-1) + 1
    num_id = max([i for i in num_ids if i is not None], default=0) + 1

    try:
        abstract = parse_xml(_abstract_num_xml(abstract_id))
        # Schema order is numPicBullet*, abstractNum*, num* — a new abstractNum
        # must land before the first w:num, not at the end of the part.
        first_num = numbering.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(abstract)
        else:
            numbering.append(abstract)
        num = parse_xml(
            f'<w:num xmlns:w="{_NS_W}" w:numId="{num_id}">'
            f'<w:abstractNumId w:val="{abstract_id}"/></w:num>'
        )
        numbering.append(num)
    except Exception as exc:               # pragma: no cover - defensive
        _warn(warnings, f"Could not define bullet numbering ({exc}); bullets use literal glyphs.")
        return None
    return num_id


def _apply_numbering(paragraph, num_id: int, ilvl: int) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    numpr = ppr.get_or_add_numPr()
    numpr.get_or_add_ilvl().val = ilvl
    numpr.get_or_add_numId().val = num_id


# --------------------------------------------------------------------------
# Page furniture
# --------------------------------------------------------------------------
def _apply_page_geometry(section) -> None:
    section.page_width = Inches(DCN_PAGE_WIDTH_IN)
    section.page_height = Inches(DCN_PAGE_HEIGHT_IN)
    section.left_margin = Inches(DCN_MARGIN_LEFT_IN)
    section.right_margin = Inches(DCN_MARGIN_RIGHT_IN)
    section.top_margin = Inches(DCN_MARGIN_TOP_IN)
    section.bottom_margin = Inches(DCN_MARGIN_BOTTOM_IN)
    section.header_distance = Inches(_HEADER_DISTANCE_IN)
    section.footer_distance = Inches(_FOOTER_DISTANCE_IN)


def _notes_box_run_xml(primary_font: str = DCN_FONT_PRIMARY) -> str:
    """Raw OOXML for the header-anchored "Space for Notes" box.

    Structure, offsets and extents are copied from the reference's header1.xml;
    python-docx has no API for a floating shape, and putting the box in the body
    would make it slide down the page as notes grow.

    ``primary_font`` is accepted for call-site symmetry but the caption is set
    in _FURNITURE_FONT: it is three English words that appear on every note the
    institute prints, and in the original they do not change with the language.
    """
    primary_font = _FURNITURE_FONT
    caption_para = (
        '<w:p><w:pPr>'
        '<w:spacing w:after="0" w:before="0" w:line="240" w:lineRule="auto"/>'
        '<w:ind w:left="0" w:right="0" w:firstLine="0"/>'
        '<w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr>'
        f'<w:rFonts w:ascii="{primary_font}" w:hAnsi="{primary_font}"'
        f' w:eastAsia="{primary_font}" w:cs="{primary_font}"/>'
        '<w:b w:val="0"/><w:i w:val="0"/>'
        f'<w:color w:val="{DCN_COLOR_NOTES_BOX}"/>'
        f'<w:sz w:val="{int(DCN_SIZE_NOTES_CAPTION_PT * 2)}"/>'
        f'<w:szCs w:val="{int(DCN_SIZE_NOTES_CAPTION_PT * 2)}"/>'
        '<w:u w:val="single"/>'
        f'</w:rPr><w:t xml:space="preserve">{NOTES_BOX_CAPTION}</w:t></w:r></w:p>'
    )
    anchor_attrs = (
        'distT="114300" distB="114300" distL="114300" distR="114300" simplePos="0"'
        ' relativeHeight="251658240" behindDoc="1" locked="0" layoutInCell="1"'
        ' allowOverlap="1" hidden="0"'
    )
    position = (
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="column"><wp:posOffset>{_NOTES_BOX_X_EMU}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="paragraph"><wp:posOffset>{_NOTES_BOX_Y_EMU}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{_NOTES_BOX_CX_EMU}" cy="{_NOTES_BOX_CY_EMU}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:wrapNone/>'
        '<wp:docPr id="1001" name="DCN Space for Notes"/>'
    )
    shape = (
        '<wps:wsp>'
        '<wps:cNvPr id="1001" name="DCN Space for Notes"/>'
        '<wps:cNvSpPr txBox="1"/>'
        '<wps:spPr>'
        f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{_NOTES_BOX_CX_EMU}" cy="{_NOTES_BOX_CY_EMU}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:noFill/>'
        '<a:ln w="9525" cap="flat" cmpd="sng">'
        f'<a:solidFill><a:srgbClr val="{DCN_COLOR_NOTES_BOX}"/></a:solidFill>'
        '<a:prstDash val="solid"/><a:round/>'
        '<a:headEnd type="none" w="sm" len="sm"/><a:tailEnd type="none" w="sm" len="sm"/>'
        '</a:ln>'
        '</wps:spPr>'
        f'<wps:txbx><w:txbxContent>{caption_para}</w:txbxContent></wps:txbx>'
        '<wps:bodyPr wrap="square" lIns="91425" tIns="91425" rIns="91425" bIns="91425"'
        ' anchor="t" anchorCtr="0" spcFirstLastPara="1"><a:noAutofit/></wps:bodyPr>'
        '</wps:wsp>'
    )
    vml_style = (
        "position:absolute;"
        f"margin-left:{_NOTES_BOX_X_EMU / _EMU_PER_PT:.2f}pt;"
        f"margin-top:{_NOTES_BOX_Y_EMU / _EMU_PER_PT:.2f}pt;"
        f"width:{_NOTES_BOX_CX_EMU / _EMU_PER_PT:.2f}pt;"
        f"height:{_NOTES_BOX_CY_EMU / _EMU_PER_PT:.2f}pt;"
        "z-index:-251658240;mso-position-horizontal-relative:text;"
        "mso-position-vertical-relative:text;v-text-anchor:top"
    )
    return (
        f'<w:r xmlns:w="{_NS_W}" xmlns:mc="{_NS_MC}" xmlns:wp="{_NS_WP}" xmlns:a="{_NS_A}"'
        f' xmlns:wps="{_NS_WPS}" xmlns:v="{_NS_V}">'
        "<mc:AlternateContent>"
        '<mc:Choice Requires="wps">'
        f'<w:drawing><wp:anchor {anchor_attrs}>{position}'
        f'<a:graphic><a:graphicData uri="{_NS_WPS}">{shape}</a:graphicData></a:graphic>'
        "</wp:anchor></w:drawing>"
        "</mc:Choice>"
        "<mc:Fallback><w:pict>"
        f'<v:rect id="dcn_notes_box" style="{vml_style}" filled="f" stroked="t"'
        f' strokecolor="#{DCN_COLOR_NOTES_BOX}" strokeweight=".75pt">'
        f'<v:textbox inset="7.2pt,7.2pt,7.2pt,7.2pt"><w:txbxContent>{caption_para}</w:txbxContent></v:textbox>'
        "</v:rect></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def _reposition_notes_box(run_element) -> None:
    """Move an already-drawn notes box onto the current page geometry.

    Patches the DrawingML anchor (offset + extents) and the VML fallback that
    rides beside it. Anything the shape does not carry is left alone: a
    partially-recognised box that stays put is a cosmetic fault, whereas raising
    here would fail a run over furniture.
    """
    for position_h in run_element.iter(f"{{{_NS_WP}}}positionH"):
        for offset in position_h.iter(f"{{{_NS_WP}}}posOffset"):
            offset.text = str(_NOTES_BOX_X_EMU)
    for extent in run_element.iter(f"{{{_NS_WP}}}extent"):
        extent.set("cx", str(_NOTES_BOX_CX_EMU))
        extent.set("cy", str(_NOTES_BOX_CY_EMU))
    # Only the a:ext INSIDE an a:xfrm is the shape's size. The same tag name is
    # also the child of a:extLst (CT_OfficeArtExtension), which takes a @uri and
    # nothing else — and Word routinely writes an a:extLst into the spPr and the
    # a:ln of shapes it authors. Writing cx/cy onto those produced invalid XML.
    for xfrm in run_element.iter(f"{{{_NS_A}}}xfrm"):
        for ext in xfrm.findall(f"{{{_NS_A}}}ext"):
            ext.set("cx", str(_NOTES_BOX_CX_EMU))
            ext.set("cy", str(_NOTES_BOX_CY_EMU))
    for rect in run_element.iter(f"{{{_NS_V}}}rect"):
        style = rect.get("style") or ""
        replacements = {
            "margin-left": f"{_NOTES_BOX_X_EMU / _EMU_PER_PT:.2f}pt",
            "width": f"{_NOTES_BOX_CX_EMU / _EMU_PER_PT:.2f}pt",
            "height": f"{_NOTES_BOX_CY_EMU / _EMU_PER_PT:.2f}pt",
        }
        parts = []
        for declaration in style.split(";"):
            key = declaration.split(":", 1)[0].strip()
            parts.append(f"{key}:{replacements[key]}" if key in replacements else declaration)
        rect.set("style", ";".join(parts))


def _notes_box_runs(part_element) -> list:
    """Every w:r in a header that carries a floating text box."""
    found = []
    for run in part_element.iter(qn("w:r")):
        for node in run.iter():
            if node.tag in (_TAG_ALTERNATE_CONTENT, _TAG_WPS_TXBX, _TAG_V_TEXTBOX):
                found.append(run)
                break
    return found


def _has_picture(part_element) -> bool:
    return part_element.find(f".//{qn('pic:pic')}") is not None


# Pixels per rendered inch to keep. 300 is print quality; beyond it the extra
# data cannot be seen and is simply carried in every generated file. The
# shipped assets/pw_logo.png is 1254x1254 (796 KB) but renders at 0.735 in
# square, which is ~220 px — the untouched original alone made each
# programmatic-mode DOCX ~825 KB against ~53 KB for template mode.
_LOGO_TARGET_DPI = 300


def _rendered_logo_inches(pixel_w: int, pixel_h: int) -> tuple[float, float]:
    """Size the logo will actually occupy, mirroring _add_logo's fitting."""
    if pixel_w <= 0 or pixel_h <= 0:
        return _LOGO_BOX_W_IN, _LOGO_BOX_H_IN
    width = _LOGO_BOX_W_IN
    height = width * (pixel_h / pixel_w)
    if height > _LOGO_BOX_H_IN:
        height = _LOGO_BOX_H_IN
        width = height * (pixel_w / pixel_h)
    return width, height


def _downscaled_logo(logo_path: Path) -> Path:
    """A print-resolution copy of the logo, or the original if that fails.

    Never raises: a logo that cannot be re-encoded is still worth embedding at
    full size, and losing the branding over an optimisation would be a poor
    trade.
    """
    try:
        from PIL import Image

        with Image.open(logo_path) as image:
            width_in, height_in = _rendered_logo_inches(*image.size)
            cap = max(64, int(round(max(width_in, height_in) * _LOGO_TARGET_DPI)))
            if max(image.size) <= cap:
                return logo_path
            copy = image.copy()
            copy.thumbnail((cap, cap), Image.LANCZOS)
            target = Path(tempfile.gettempdir()) / f"dcn_logo_{copy.width}x{copy.height}.png"
            if not target.exists():
                copy.save(target, format="PNG", optimize=True)
            return target
    except Exception:
        return logo_path


def _add_logo(paragraph, logo_path: Path) -> bool:
    """Insert the logo scaled to fit the 3.0 x 0.735 in box without distortion."""
    try:
        shape = paragraph.add_run().add_picture(
            str(_downscaled_logo(logo_path)), width=Inches(_LOGO_BOX_W_IN))
    except Exception:
        return False
    max_h = Inches(_LOGO_BOX_H_IN)
    if shape.height > max_h:
        width, height = shape.width, shape.height
        shape.height = max_h
        shape.width = Emu(int(width * (max_h / height)))
    return True


def _resolve_logo(logo_path: Path | None) -> Path | None:
    """The caller's logo, else whichever shipped one is installed.

    The fallback goes through default_logo_path() rather than naming a file, so
    every entry point — the app, rebuild_outputs, the CLI, a test — picks the
    same one. Naming assets/pw_logo.png here meant a caller that passed no logo
    got the bare PW mark even when the full OnlyIAS lockup was installed.
    """
    if logo_path and Path(logo_path).exists():
        return Path(logo_path)
    return default_logo_path()


def _build_header(
    section,
    logo_path: Path | None,
    *,
    use_notes_box: bool,
    from_template: bool,
    warnings: list[str],
    primary_font: str = DCN_FONT_PRIMARY,
) -> None:
    header = section.header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    element = header._element

    existing_boxes = _notes_box_runs(element)
    if not use_notes_box:
        for run in existing_boxes:
            run.getparent().remove(run)
    elif not existing_boxes:
        try:
            paragraph._p.append(parse_xml(_notes_box_run_xml(primary_font)))
        except Exception as exc:           # pragma: no cover - defensive
            _warn(warnings, f"Could not add the notes box ({exc}); the right column is blank.")
    else:
        # Template mode. The template's own box was positioned for the page
        # geometry the template was drawn at, and _apply_page_geometry has just
        # replaced that geometry — leaving the box where it was would sit it on
        # top of the (now wider) content column. Move it to where config says
        # the reserved column is.
        for run in existing_boxes:
            _reposition_notes_box(run)

    if from_template and _has_picture(element):
        return                             # the template's own logo is already in place
    logo = _resolve_logo(logo_path)
    if not logo:
        _warn(warnings, "No logo image was found; the header shows no logo.")
        return
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    _set_ind(paragraph._p.get_or_add_pPr(), left=_LOGO_INDENT_TWIPS)
    if not _add_logo(paragraph, logo):
        _warn(warnings, f"Logo {logo.name} could not be embedded; the header shows no logo.")


def _add_page_number_field(paragraph, *, primary_font: str) -> None:
    """Append a Word PAGE field — the live page number — to ``paragraph``.

    A field rather than typed text, because the number is different on every
    page and nothing in this writer knows how many pages the body will come to.

    The full five-run form (begin / instruction / separate / cached value /
    end) rather than the short one: the cached "1" is what a reader sees in
    Google Docs, LibreOffice or a phone preview that will not recalculate
    fields on open. Word and the PDF export both replace it with the real
    number, so the only cost is being right everywhere instead of blank.
    """
    def _run(child):
        run = paragraph.add_run()
        run.font.size = Pt(DCN_SIZE_FOOTER_PT)
        _set_run_fonts(run, primary_font)
        run._r.append(child)

    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    cached = OxmlElement("w:t")
    cached.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")

    for child in (begin, instruction, separate, cached, end):
        _run(child)


def _build_footer(document, section, footer_left: str, footer_right: str, *,
                  enabled: bool, primary_font: str = DCN_FONT_PRIMARY) -> None:
    # Same reasoning as the notes-box caption: the strip reads "PW OnlyIAS" and
    # "Daily Class Notes" whatever language the lecture was taught in, and the
    # original sets it in the Latin handwritten face on its Hindi notes.
    primary_font = _FURNITURE_FONT
    footer = section.footer
    footer.is_linked_to_previous = False
    # Rebuilt from nothing, so a template's own footer (paragraphs, tables,
    # tab stops) cannot leak into the strip.
    for child in list(footer._element):
        footer._element.remove(child)
    if not enabled:
        footer.add_paragraph()
        return

    # A table, not one paragraph with tab stops. The strip has to reach past the
    # 2.25 in right margin that holds the notes box, and a paragraph only gets
    # there through a negative indent. Google Docs and other viewers clamp that
    # indent to the margin, the right-hand tab stop then lies outside the line
    # and is dropped, and the page number runs into the label ("5Daily Class
    # Notes"). A cell keeps each label in its own place in every viewer.
    table = footer.add_table(rows=1, cols=3, width=Twips(_FOOTER_PAGE_WIDTH_TWIPS))
    table.autofit = False                  # writes w:tblLayout type="fixed"
    tbl_pr = table._tbl.tblPr
    tbl_w = _ordered_child(tbl_pr, "w:tblW", _TBLPR_ORDER)
    tbl_w.set(qn("w:w"), str(_FOOTER_PAGE_WIDTH_TWIPS))
    tbl_w.set(qn("w:type"), "dxa")
    # Pulled out to the page's left edge so the strip bleeds the full width the
    # way the reference footer does.
    tbl_ind = _ordered_child(tbl_pr, "w:tblInd", _TBLPR_ORDER)
    tbl_ind.set(qn("w:w"), str(-_FOOTER_LEFT_INSET_TWIPS))
    tbl_ind.set(qn("w:type"), "dxa")
    borders = _ordered_child(tbl_pr, "w:tblBorders", _TBLPR_ORDER)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    margins = _ordered_child(tbl_pr, "w:tblCellMar", _TBLPR_ORDER)
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), "0")
        el.set(qn("w:type"), "dxa")
        margins.append(el)

    widths = (_FOOTER_LEFT_CELL_TWIPS, _FOOTER_NUMBER_CELL_TWIPS, _FOOTER_RIGHT_CELL_TWIPS)
    for grid_col, width in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths):
        grid_col.set(qn("w:w"), str(width))

    def _footer_cell(index: int, alignment, *, left: int = 0, right: int = 0):
        cell = table.rows[0].cells[index]
        cell.width = Twips(widths[index])
        _set_shading(cell._tc.get_or_add_tcPr(), DCN_COLOR_FOOTER_FILL, _TCPR_ORDER)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = alignment
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1.0
        _set_ind(paragraph._p.get_or_add_pPr(), left=left, right=right)
        return paragraph

    def _footer_run(paragraph, text: str):
        run = paragraph.add_run(text)
        run.font.size = Pt(DCN_SIZE_FOOTER_PT)
        _set_run_fonts(run, primary_font)

    _footer_run(_footer_cell(0, WD_ALIGN_PARAGRAPH.LEFT, left=_FOOTER_LEFT_INSET_TWIPS),
                footer_left)
    number = _footer_cell(1, WD_ALIGN_PARAGRAPH.CENTER)
    # The original numbers its pages, centred between the two labels.
    if DCN_FOOTER_SHOW_PAGE_NUMBER:
        _add_page_number_field(number, primary_font=primary_font)
    _footer_run(_footer_cell(2, WD_ALIGN_PARAGRAPH.RIGHT, right=_FOOTER_RIGHT_INSET_TWIPS),
                footer_right)

    # A footer must end with a paragraph. This one is shrunk to a 1 pt line so
    # it does not add a blank line under the strip.
    tail = footer.add_paragraph()
    tail.paragraph_format.space_before = Pt(0)
    tail.paragraph_format.space_after = Pt(0)
    tail.paragraph_format.line_spacing = Pt(1)
    tail_rpr = _ordered_child(tail._p.get_or_add_pPr(), "w:rPr", _PPR_ORDER)
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "2")
    tail_rpr.append(size)


# --------------------------------------------------------------------------
# Marker parser
# --------------------------------------------------------------------------
def _is_caution(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith(_CAUTION_PREFIX):
        return True
    lowered = stripped.lower()
    return any(phrase.lower() in lowered for phrase in _CAUTION_PHRASES)


def strip_caution_label(text: str) -> str:
    """The caution sentence without the label that announced it.

    The label is how the model marks a caution for this writer; it is not
    content. Once the paragraph is shaded, printing "UPSC Caution:" as well
    states the same thing twice — so it is consumed here.

    A line that is ONLY a label keeps its original text: an empty paragraph
    tells the reader nothing, and a caution that survives as a visible sentence
    is better than one that silently disappears.
    """
    stripped = (text or "").strip()
    bracket = _CAUTION_BRACKET_RE.match(stripped)
    if bracket:
        return bracket.group("body").strip() or stripped
    without_label = _CAUTION_LABEL_RE.sub("", stripped, count=1).strip()
    return without_label or stripped


def _detect_indent_step(lines: list[str]) -> int:
    """Whether the model nests bullets in 2-space or 4-space steps.

    Both appear in real model output; guessing wrong collapses level 2 into
    level 3, so the step is measured from the document's own indents.
    """
    widths = set()
    for line in lines:
        match = _BULLET_RE.match(line.rstrip())
        if not match:
            continue
        indent = match.group("indent")
        spaces = len(indent) - indent.count("\t")
        if spaces:
            widths.add(spaces)
    if widths and all(w % 4 == 0 for w in widths):
        return 4
    return 2


def _bullet_level(indent: str, step: int) -> int:
    tabs = indent.count("\t")
    spaces = len(indent) - tabs
    return max(1, min(3, 1 + tabs + spaces // step))


def _split_table_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _parse_blocks(notes_text: str, warnings: list[str]) -> list[_Block]:
    """Turn marker text into blocks, degrading (never raising) on bad input."""
    lines = (notes_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    step = _detect_indent_step(lines)
    blocks: list[_Block] = []
    pending: list[str] = []                # raw lines of an open TABLE/FLOW run
    mode: str | None = None

    def flush_as_body(reason: str) -> None:
        nonlocal mode, pending
        if reason:
            _warn(warnings, reason)
        for raw in pending:
            blocks.extend(_plain_line_blocks(raw, step))
        pending = []
        mode = None

    def close_table() -> None:
        nonlocal mode, pending
        rows = [_split_table_row(r) for r in pending if not _TABLE_RULE_ROW_RE.match(r)]
        rows = [r for r in rows if any(c for c in r)]
        if rows:
            blocks.append(_Block(kind="table", rows=rows))
        else:
            _warn(warnings, "A #TABLE_START block held no usable rows and was skipped.")
        pending = []
        mode = None

    for raw in lines:
        line = raw.rstrip()
        bare = _MARKER_BARE_RE.match(line.strip())
        marker = bare.group(1).upper() if bare else None

        if mode == "table":
            if marker == "TABLE_END":
                close_table()
                continue
            if (marker in ("TABLE_START", "FLOW_START", "FLOW_END", "SEPARATOR")
                    or _MARKER_TEXT_RE.match(line) or _FIGURE_LINE_RE.match(line)):
                flush_as_body("An unclosed #TABLE_START was found; its rows are rendered as body text.")
            elif line.strip():
                pending.append(line)
                continue
            else:
                continue
        elif mode == "flow":
            if marker == "FLOW_END":
                if pending:
                    blocks.append(_Block(kind="flow", lines=list(pending)))
                else:
                    _warn(warnings, "A #FLOW_START block was empty and was skipped.")
                pending = []
                mode = None
                continue
            if (marker in ("FLOW_START", "TABLE_START", "TABLE_END", "SEPARATOR")
                    or _MARKER_TEXT_RE.match(line) or _FIGURE_LINE_RE.match(line)):
                flush_as_body("An unclosed #FLOW_START was found; its lines are rendered as body text.")
            elif line.strip():
                pending.append(line.strip())
                continue
            else:
                continue
        elif mode == "correction":
            if marker == "CORRECTION_END":
                blocks.extend(_correction_block(pending, warnings))
                pending = []
                mode = None
                continue
            if marker or _MARKER_TEXT_RE.match(line):
                _warn(warnings, "An unclosed #CORRECTION_START was found; the block was dropped.")
                pending = []
                mode = None
            elif line.strip():
                pending.append(line.strip())
                continue
            else:
                continue
        elif mode == "topics":
            if marker == "TOPICS_COVERED_END":
                if pending:
                    blocks.append(_Block(kind="topics", lines=list(pending)))
                else:
                    _warn(warnings, "A #TOPICS_COVERED_START block was empty and was skipped.")
                pending = []
                mode = None
                continue
            if marker or _MARKER_TEXT_RE.match(line):
                # An unclosed topics block would otherwise swallow the notes.
                if pending:
                    blocks.append(_Block(kind="topics", lines=list(pending)))
                _warn(warnings, "A #TOPICS_COVERED_START was never closed; "
                                "the lines up to the next marker were used as the topic list.")
                pending = []
                mode = None
            elif line.strip():
                entry = _BULLET_RE.match(line)
                pending.append((entry.group("text") if entry else line).strip())
                continue
            else:
                continue

        if marker == "TOPICS_COVERED_START":
            mode = "topics"
            continue
        if marker == "TOPICS_COVERED_END":
            _warn(warnings, "A #TOPICS_COVERED_END without a matching start was ignored.")
            continue
        if marker == "TABLE_START":
            mode = "table"
            continue
        if marker == "FLOW_START":
            mode = "flow"
            continue
        if marker == "CORRECTION_START":
            mode = "correction"
            continue
        if marker == "CORRECTION_END":
            _warn(warnings, "A #CORRECTION_END without a matching start was ignored.")
            continue
        if marker == "TABLE_END":
            _warn(warnings, "A #TABLE_END without a matching #TABLE_START was ignored.")
            continue
        if marker == "FLOW_END":
            _warn(warnings, "A #FLOW_END without a matching #FLOW_START was ignored.")
            continue
        if marker == "SEPARATOR":
            # Consumed, rendered as nothing: the reference draws no rule
            # between topics (its own pBdr blocks are all w:val="none" noise).
            continue

        figure_marker = _FIGURE_LINE_RE.match(line.strip())
        if figure_marker:
            blocks.append(_Block(kind="figure", text=figure_marker.group(1)))
            continue

        text_marker = _MARKER_TEXT_RE.match(line)
        if text_marker:
            kind = {"LECTURE_TITLE": "title", "H1": "h1", "H2": "h2",
                    "H3": "h3", "H4": "h4"}[text_marker.group(1).upper()]
            heading = text_marker.group(2).strip()
            if heading:
                blocks.append(_Block(kind=kind, text=heading))
            else:
                _warn(warnings, f"An empty #{text_marker.group(1).upper()}: marker was skipped.")
            continue

        blocks.extend(_plain_line_blocks(line, step))

    if mode == "table":
        flush_as_body("An unclosed #TABLE_START was found; its rows are rendered as body text.")
    elif mode == "flow":
        flush_as_body("An unclosed #FLOW_START was found; its lines are rendered as body text.")
    elif mode == "correction":
        _warn(warnings, "An unclosed #CORRECTION_START was found; the block was dropped.")
        pending = []
    elif mode == "topics" and pending:
        blocks.append(_Block(kind="topics", lines=list(pending)))
        _warn(warnings, "A #TOPICS_COVERED_START was never closed; the rest of the "
                        "response was used as the topic list.")
    if not blocks and (notes_text or "").strip():
        _warn(warnings, "No DCN markers or text were recognised; the document is empty.")
    return blocks


def _correction_block(lines: list[str], warnings: list[str]) -> list[_Block]:
    """Parse SOURCE:/CORRECTED:/REASON: fields; honour the feature gate.

    With CORRECTIONS_ENABLED off the block is consumed and dropped — the
    shipping contract is silent corrections, and a disabled feature must not
    leak marker text onto a student's page.
    """
    fields = {"source": "", "corrected": "", "reason": ""}
    current: str | None = None
    for line in lines:
        match = _CORRECTION_FIELD_RE.match(line)
        if match:
            current = {"SOURCE": "source", "CORRECTED": "corrected",
                       "REASON": "reason"}[match.group(1).upper()]
            fields[current] = match.group(2).strip()
        elif current:
            fields[current] = f"{fields[current]} {line.strip()}".strip()
    if not CORRECTIONS_ENABLED:
        _warn(warnings, "A #CORRECTION block was dropped (CORRECTIONS_ENABLED is off).")
        return []
    if not (fields["source"] and fields["corrected"]):
        _warn(warnings, "A #CORRECTION block was missing SOURCE: or CORRECTED: and was dropped.")
        return []
    return [_Block(kind="correction",
                   lines=[fields["source"], fields["corrected"], fields["reason"]])]


def _plain_line_blocks(line: str, step: int) -> list[_Block]:
    if not line.strip():
        return []
    match = _BULLET_RE.match(line)
    if match:
        text = match.group("text").strip()
        if not text:
            return []
        return [_Block(kind="bullet", text=text, level=_bullet_level(match.group("indent"), step),
                       caution=_is_caution(text))]
    ordered = _ORDERED_RE.match(line)
    if ordered:
        text = ordered.group("text").strip()
        if text:
            # `level` carries the step number: ordered lists are flat, so the
            # nesting slot is free, and the renderer needs the number as typed.
            return [_Block(kind="ordered", text=text, level=int(ordered.group("num")))]
    text = line.strip()
    return [_Block(kind="body", text=text, caution=_is_caution(text))]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _set_table_borders(table) -> None:
    borders = _ordered_child(table._tbl.tblPr, "w:tblBorders", _TBLPR_ORDER)
    for old in list(borders):
        borders.remove(old)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")            # 0.5 pt — subtle, like the reference
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), _COLOR_RULE)
        borders.append(el)


def _set_table_cell_margins(table) -> None:
    """Shrink the padding inside every cell of a table.

    Word's default is 0 / 108 / 0 / 108 twips. On a three-column comparison
    table in a 5.85 in column that is 0.3 in — more width spent on air than some
    cells spend on text — and the top/bottom pair adds a line's worth of height
    to every row. The values are small but never zero: text touching a gridline
    reads as clipped even when it is not.
    """
    margins = _ordered_child(table._tbl.tblPr, "w:tblCellMar", _TBLPR_ORDER)
    for edge in ("top", "left", "bottom", "right"):
        el = margins.find(qn(f"w:{edge}"))
        if el is None:
            el = OxmlElement(f"w:{edge}")
            margins.append(el)
        el.set(qn("w:w"), str(DCN_TABLE_CELL_MARGIN_TWIPS[edge]))
        el.set(qn("w:type"), "dxa")


def _render_table(doc, rows: list[list[str]], warnings: list[str],
                  *, primary_font: str = DCN_FONT_PRIMARY,
                  highlighter: "_Highlighter | None" = None) -> None:
    columns = max(len(r) for r in rows)
    if any(len(r) != columns for r in rows):
        _warn(warnings, "A table had rows with different cell counts; short rows were padded.")
    table = doc.add_table(rows=0, cols=columns)
    table.autofit = False                  # writes w:tblLayout type="fixed"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _set_table_borders(table)
    _set_table_cell_margins(table)

    # Clamp to the content column so a wide table can never reach the notes
    # box; the last column absorbs the integer-division remainder.
    widths = [_CONTENT_WIDTH_TWIPS // columns] * columns
    widths[-1] += _CONTENT_WIDTH_TWIPS - sum(widths)
    tbl_w = _ordered_child(table._tbl.tblPr, "w:tblW", _TBLPR_ORDER)
    tbl_w.set(qn("w:w"), str(_CONTENT_WIDTH_TWIPS))
    tbl_w.set(qn("w:type"), "dxa")
    for grid_col, width in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths):
        grid_col.set(qn("w:w"), str(width))

    for index, cells in enumerate(rows):
        row = table.add_row()
        tr_pr = row._tr.get_or_add_trPr()
        # A row is one unit of meaning; splitting it over a page boundary puts
        # half a comparison on each page and reads as clipped text. Word instead
        # moves the whole row down — and repeats the header above it, because of
        # the w:tblHeader below.
        tr_pr.append(OxmlElement("w:cantSplit"))
        if index == 0:
            tr_pr.append(OxmlElement("w:tblHeader"))   # repeat on every page
        for column, cell in enumerate(row.cells):
            cell.width = Twips(widths[column])
            paragraph = cell.paragraphs[0]
            paragraph.style = STYLE_TABLE
            if index == 0:
                if _COLOR_TABLE_HEADER_FILL:
                    _set_shading(cell._tc.get_or_add_tcPr(),
                                 _COLOR_TABLE_HEADER_FILL, _TCPR_ORDER)
            text = cells[column] if column < len(cells) else ""
            _add_text_runs(paragraph, text, bold=index == 0, primary_font=primary_font,
                           highlighter=highlighter)


#: Correction callout tints. The accent red is the palette's existing red
#: (_COLOR_CAUTION / DCN_COLOR_H1) — one red per document, not a second one.
_CORRECTION_FILL = "FDEBEF"
_COLOR_MUTED = "7F7F7F"


def _render_correction(doc, source: str, corrected: str, reason: str,
                       *, primary_font: str = DCN_FONT_PRIMARY) -> None:
    """A red correction callout: struck-through source, red fix, grey reason.

    Drawn as a single-cell table because that is the one bordered container
    the DCN contract allows — paragraph borders (w:pBdr) are banned outright,
    while table borders are already legitimate. Text goes through
    _add_text_runs so markdown, glyph coverage and Devanagari all keep working.
    """
    table = doc.add_table(rows=1, cols=1)
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    borders = _ordered_child(table._tbl.tblPr, "w:tblBorders", _TBLPR_ORDER)
    for old in list(borders):
        borders.remove(old)
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "8")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), _COLOR_CAUTION)
        borders.append(el)
    tbl_w = _ordered_child(table._tbl.tblPr, "w:tblW", _TBLPR_ORDER)
    tbl_w.set(qn("w:w"), str(_CONTENT_WIDTH_TWIPS))
    tbl_w.set(qn("w:type"), "dxa")
    cell = table.rows[0].cells[0]
    cell.width = Twips(_CONTENT_WIDTH_TWIPS)
    _set_shading(cell._tc.get_or_add_tcPr(), _CORRECTION_FILL, _TCPR_ORDER)

    heading = cell.paragraphs[0]
    heading.style = STYLE_TABLE
    _add_text_runs(heading, "Correction", bold=True, color=_COLOR_CAUTION,
                   primary_font=primary_font)
    source_para = cell.add_paragraph(style=STYLE_TABLE)
    _add_text_runs(source_para, source, color=_COLOR_MUTED, primary_font=primary_font)
    for run in source_para.runs:
        run.font.strike = True
    corrected_para = cell.add_paragraph(style=STYLE_TABLE)
    _add_text_runs(corrected_para, corrected, color=_COLOR_CAUTION,
                   primary_font=primary_font)
    if reason:
        reason_para = cell.add_paragraph(style=STYLE_TABLE)
        _add_text_runs(reason_para, reason, italic=True, color=_COLOR_MUTED,
                       primary_font=primary_font)
        for run in reason_para.runs:
            run.font.size = Pt(9)
            _sync_cs_twins(run._element.get_or_add_rPr())


def figure_display_width_in(width_px: int) -> float:
    """Display width for a figure, from its pixel width.

    Aims at VISUAL_TARGET_DPI so labels print sharp, then clamps into the
    [min, max] band the 5.5 in content column allows. Height is never set
    directly — add_picture keeps the aspect ratio, and only a follow-up
    proportional rescale caps it (see _render_figure) — so a figure can be
    scaled but never stretched.
    """
    if width_px <= 0:
        return min(4.5, VISUAL_MAX_WIDTH_IN)
    natural = width_px / VISUAL_TARGET_DPI
    return max(VISUAL_MIN_WIDTH_IN, min(VISUAL_MAX_WIDTH_IN, natural))


def _render_figure(doc, visual_id: str, visuals: dict[str, dict] | None,
                   warnings: list[str], *, language_code: str = "en",
                   primary_font: str = DCN_FONT_PRIMARY) -> None:
    """One slide diagram: centred image + short caption, kept together.

    Degrades exactly like the rest of this writer: an unknown id or an
    unreadable image file drops the figure with a warning — the surrounding
    notes are complete without it (spec §9), so nothing else changes.
    """
    visual = (visuals or {}).get(visual_id)
    if not visual:
        _warn(warnings, f"Figure {visual_id} was referenced but is not in the visual "
                        "registry; it was skipped.")
        return
    image_path = Path(visual.get("path") or "")
    if not image_path.exists():
        _warn(warnings, f"Figure {visual_id}: image file {image_path.name} is missing; "
                        "the figure was skipped.")
        return

    caption = (visual.get("caption") or "").strip()
    paragraph = doc.add_paragraph(style=STYLE_FIGURE)
    if not caption:
        # No caption to keep with — do not chain the figure to whatever block
        # happens to follow it.
        paragraph.paragraph_format.keep_with_next = False
        paragraph.paragraph_format.space_after = Pt(10)
    width_in = figure_display_width_in(int(visual.get("width_px") or 0))
    try:
        shape = paragraph.add_run().add_picture(str(image_path), width=Inches(width_in))
    except Exception as exc:  # noqa: BLE001 — a bad PNG must not kill the document
        paragraph._p.getparent().remove(paragraph._p)
        _warn(warnings, f"Figure {visual_id} could not be embedded ({exc}); it was skipped.")
        return
    max_h = Inches(VISUAL_MAX_HEIGHT_IN)
    if shape.height > max_h:
        width, height = shape.width, shape.height
        shape.height = max_h
        shape.width = Emu(int(width * (max_h / height)))

    if caption:
        prefix = FIGURE_CAPTION_PREFIX.get(
            (language_code or "en").lower()[:2], FIGURE_CAPTION_PREFIX["en"])
        caption_para = doc.add_paragraph(style=STYLE_FIGURE_CAPTION)
        _add_text_runs(caption_para, f"{prefix}{caption}", italic=True,
                       primary_font=primary_font)


def _render_title_block(doc, raw_title: str, *, subject_line: str, language_code: str,
                        primary_font: str, lecture_no: str = "") -> None:
    """The two-level heading a DCN opens with.

        Indian Society                      <- subject, when one is known
        Lecture 4 - Major Features ...      <- lecture number + lecture title

    The ``#LECTURE_TITLE:`` marker keeps carrying the canonical
    ``L04_DCN04_Major Features...`` form — file naming, the run metadata and
    every existing parser still see exactly what they saw before. Only the
    PRESENTATION is split here, by parsing that value back into its parts.

    A missing lecture number is dropped, never invented: ``lecture_heading``
    returns the bare title, and the line reads as a title rather than as
    "Lecture 0".
    """
    subject = (subject_line or "").strip()
    if subject:
        paragraph = doc.add_paragraph(style=STYLE_SUBJECT)
        _add_text_runs(paragraph, wrap_for_display(subject), bold=True, underline=False,
                       primary_font=primary_font)
    parsed = parse_marker_title(raw_title)
    if lecture_no and not parsed.lecture_no:
        # The number is known but the title never carried it. That is the normal
        # shape for a class with no deck: the marker holds only the title the
        # model wrote, while the number came from what the user typed. Only the
        # NUMBER is taken from the caller — an absent one is still omitted rather
        # than invented (spec §8).
        parsed = replace(parsed, lecture_no=lecture_no)
    heading = lecture_heading(parsed, language_code) or raw_title
    paragraph = doc.add_paragraph(style=STYLE_TITLE)
    # A title is the one heading that can arrive as an unbreakable run
    # (Lxx_DCNxx_Probity_in_Governance_...). Word cannot break inside a word, so
    # without this it overhangs into the notes-box column.
    _add_text_runs(paragraph, wrap_for_display(heading), bold=True, underline=False,
                   primary_font=primary_font)


def _render_blocks(doc, blocks: list[_Block], *, num_id: int | None, warnings: list[str],
                   language_code: str = "en", visuals: dict[str, dict] | None = None,
                   primary_font: str = DCN_FONT_PRIMARY, subject_line: str = "",
                   lecture_no: str = "",
                   highlighter: "_Highlighter | None" = None) -> None:
    heading_styles = {"h1": STYLE_H1, "h2": STYLE_H2, "h3": STYLE_H3,
                      "h4": STYLE_H4}
    for block in blocks:
        if block.kind == "topics":
            # Parsed so the raw markers never reach the page, rendered as
            # nothing: the reference goes straight from title to the first H1.
            continue
        elif block.kind == "title":
            _render_title_block(doc, block.text, subject_line=subject_line,
                                language_code=language_code, primary_font=primary_font,
                                lecture_no=lecture_no)
        elif block.kind in heading_styles:
            paragraph = doc.add_paragraph(style=heading_styles[block.kind])
            _add_text_runs(paragraph, block.text, bold=True, underline=False,
                           primary_font=primary_font)
        elif block.kind == "bullet" and not block.caution:
            style = _BULLET_STYLES[block.level - 1]
            paragraph = doc.add_paragraph(style=style)
            if num_id is not None:
                _apply_numbering(paragraph, num_id, block.level - 1)
            else:
                glyph = paragraph.add_run(f"{_BULLET_GLYPHS[block.level - 1]}\t")
                glyph.font.size = Pt(DCN_BULLET_GLYPH_SIZE_PT)
                _set_run_fonts(glyph, DCN_BULLET_FONT)
                _sync_cs_twins(glyph._element.get_or_add_rPr())
            _add_text_runs(paragraph, block.text, primary_font=primary_font,
                           highlighter=highlighter)
        elif block.kind == "body" or block.kind == "bullet":
            # Caution notes — bulleted or not — go to the Caution style rather
            # than being faked with inline italics inside an ordinary bullet.
            paragraph = doc.add_paragraph(style=STYLE_CAUTION if block.caution else STYLE_BODY)
            if block.caution:
                # The label is dropped and the sentence shaded instead. The
                # shading goes on every run rather than on the paragraph so it
                # tracks the text the way a marker pen does, and so a wrapped
                # last line is not shaded out to the full column width.
                _add_text_runs(paragraph, strip_caution_label(block.text),
                               primary_font=primary_font)
                for run in paragraph.runs:
                    _set_run_shading(run, DCN_HIGHLIGHT_COLOR)
            else:
                _add_text_runs(paragraph, block.text, primary_font=primary_font,
                               highlighter=highlighter)
        elif block.kind == "ordered":
            # A genuine sequence: the step number where the bullet would sit,
            # at the same indent so wrapped lines still align under the text.
            #
            # Bold body colour, NOT the accent red this once used: the original
            # sets its numbered steps in the same ink as the sentence they
            # introduce, and red is the H1 colour — a red numeral beside black
            # text reads as a heading that has lost its line.
            paragraph = doc.add_paragraph(style=STYLE_BULLET_1)
            number = paragraph.add_run(f"{block.level}.\t")
            number.bold = True
            number.font.color.rgb = RGBColor.from_string(DCN_COLOR_BODY)
            _set_run_fonts(number, primary_font)
            _add_text_runs(paragraph, block.text, primary_font=primary_font,
                           highlighter=highlighter)
        elif block.kind == "correction":
            _render_correction(doc, *block.lines, primary_font=primary_font)
        elif block.kind == "flow":
            for index, line in enumerate(block.lines):
                paragraph = doc.add_paragraph(style=STYLE_FLOW)
                # FLOW markers can contain Markdown bullet prefixes. The
                # flow's arrows already carry the sequence; remove the stray
                # hyphens rather than printing a second, literal list marker.
                text = re.sub(r"^[-*•]\s+", "", line)
                if re.fullmatch(r"[↓↑→←↕↔\s]+", text):
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.keep_with_next = index < len(block.lines) - 1
                _add_text_runs(paragraph, text, primary_font=primary_font,
                               highlighter=highlighter)
        elif block.kind == "table":
            _render_table(doc, block.rows, warnings, primary_font=primary_font,
                          highlighter=highlighter)
        elif block.kind == "figure":
            _render_figure(doc, block.text, visuals, warnings,
                           language_code=language_code, primary_font=primary_font)


# --------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------
def _clear_body(document) -> None:
    """Empty the body but keep w:sectPr.

    sectPr is a *child of w:body*, not of the last paragraph — deleting body
    children blindly takes the page size, margins and the header/footer
    references with it, which is why it is skipped explicitly here.
    """
    body = document.element.body
    for child in list(body):
        if child.tag == qn("w:sectPr"):
            continue
        body.remove(child)


def _drop_orphan_body_rels(document) -> None:
    """Drop image/hyperlink relationships the emptied body no longer references.

    The reference template carries ~770 KB of diagrams from its own lecture, plus
    its source hyperlinks; without this every generated file would ship them
    invisibly. Only images and hyperlinks are candidates — styles, numbering,
    settings, fontTable and theme are not referenced by r:id from the body at
    all, so a "not mentioned in document.xml" rule alone would delete them.
    """
    part = document.part
    xml = part.element.xml
    for rel_id, rel in list(part.rels.items()):
        if rel.reltype not in (RT.IMAGE, RT.HYPERLINK):
            continue
        if f'"{rel_id}"' not in xml:
            part.drop_rel(rel_id)


def _open_document(template_path: Path | None, warnings: list[str]):
    if template_path:
        path = Path(template_path)
        if not path.exists():
            _warn(warnings, f"DCN template {path.name} was not found; built the layout from scratch.")
        else:
            try:
                document = Document(str(path))
                _clear_body(document)
                _drop_orphan_body_rels(document)
                return document, True
            except Exception as exc:
                _warn(warnings, f"DCN template could not be opened ({exc}); built the layout from scratch.")
    return Document(), False


def _set_doc_defaults(document, *, primary_font: str = DCN_FONT_PRIMARY) -> None:
    """Author docDefaults: face, 11pt with its szCs twin, and paragraph rhythm.

    The paragraph default matters as much as the run default: python-docx's
    own template ships ``w:after="160"`` (Word's is 200), which silently adds
    8–10pt below every paragraph and was the main page-count bloat. The DCN
    rhythm is w:after="0" with DCN_LINE_SPACING; the per-style w:after values
    stay in the styles.
    """
    styles = document.styles.element
    doc_defaults = styles.find(qn("w:docDefaults"))
    if doc_defaults is None:
        doc_defaults = OxmlElement("w:docDefaults")
        styles.insert(0, doc_defaults)
    rpr_default = doc_defaults.find(qn("w:rPrDefault"))
    if rpr_default is None:
        rpr_default = OxmlElement("w:rPrDefault")
        doc_defaults.insert(0, rpr_default)
    rpr = rpr_default.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        rpr_default.append(rpr)
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), primary_font)
    rfonts.set(qn("w:hAnsi"), primary_font)
    rfonts.set(qn("w:eastAsia"), primary_font)
    rfonts.set(qn("w:cs"), DEVANAGARI_FONT)
    for tag in ("w:sz", "w:szCs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            rpr.append(el)
        el.set(qn("w:val"), str(int(DCN_SIZE_BODY_PT * 2)))

    ppr_default = doc_defaults.find(qn("w:pPrDefault"))
    if ppr_default is None:
        ppr_default = OxmlElement("w:pPrDefault")
        doc_defaults.append(ppr_default)
    ppr = ppr_default.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        ppr_default.append(ppr)
    spacing = _ordered_child(ppr, "w:spacing", _PPR_ORDER)
    spacing.set(qn("w:after"), "0")
    # w:line is in 240ths of a line, so a "multiple" of 1.08 is 259.
    spacing.set(qn("w:line"), str(int(round(DCN_LINE_SPACING * 240))))
    spacing.set(qn("w:lineRule"), "auto")


# ---------------------------------------------------------------------------
# Font embedding (ECMA-376 §17.8; wiring copied from the reference document,
# which embeds a plain TTF with a null fontKey rather than an obfuscated
# .odttf — Word and LibreOffice both accept it)
# ---------------------------------------------------------------------------
_FONTS_DIR = ROOT_DIR / "assets" / "fonts"
_NULL_FONT_KEY = "{00000000-0000-0000-0000-000000000000}"
_FONT_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: (family, embed element, file in assets/fonts). Kalam rides in the ENGLISH
#: documents too: the English styles declare w:cs="Kalam", so any Devanagari
#: that appears in an English document (a scheme name, a place name) needs it.
_EMBED_FONTS: tuple[tuple[str, str, str], ...] = (
    ("Coming Soon", "w:embedRegular", "ComingSoon-regular.ttf"),
    ("Kalam", "w:embedRegular", "Kalam-Regular.ttf"),
    ("Kalam", "w:embedBold", "Kalam-Bold.ttf"),
)

# w:settings children that may legally precede w:embedTrueTypeFonts (CT_Settings
# is an XSD sequence like pPr — an out-of-order child makes Word reject the part).
_SETTINGS_BEFORE_EMBED = (
    "w:writeProtection", "w:view", "w:zoom", "w:removePersonalInformation",
    "w:removeDateAndTime", "w:doNotDisplayPageBoundaries", "w:displayBackgroundShape",
    "w:printPostScriptOverText", "w:printFractionalCharacterWidth", "w:printFormsData",
)


def _embed_fonts(output_path: Path, *, primary_font: str, warnings: list[str]) -> None:
    """Add the handwritten faces to the saved package so any machine shows them.

    Every run references Coming Soon / Kalam by name, but a name is only a
    request — without the bytes in ``word/fonts/`` a machine that never
    installed the face silently falls back to Calibri, which is the single
    most visible way the output can go wrong. Never raises: a failed embed
    leaves a valid (if font-dependent) document and a warning.
    """
    from lxml import etree

    # Every face the document can reference, regardless of the body language: a
    # Hindi note sets its body in Kalam but its footer and notes-box caption in
    # Coming Soon, so dropping Coming Soon from a Hindi build would leave the
    # furniture to fall back to Calibri on any machine without it installed.
    # (primary_font no longer selects; it stays in the signature for callers.)
    wanted = list(_EMBED_FONTS)
    try:
        with zipfile.ZipFile(output_path) as archive:
            entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}

        # --- fontTable.xml.rels: one font relationship per file -------------
        rels_name = "word/_rels/fontTable.xml.rels"
        if rels_name in entries:
            rels_root = etree.fromstring(entries[rels_name])
        else:
            rels_root = etree.fromstring(
                f'<Relationships xmlns="{_NS_REL}"/>'.encode())
        rel_by_target = {rel.get("Target"): rel.get("Id")
                         for rel in rels_root.findall(f"{{{_NS_REL}}}Relationship")}
        used_ids = {rel.get("Id") for rel in rels_root.findall(f"{{{_NS_REL}}}Relationship")}

        def next_rid() -> str:
            index = len(used_ids) + 1
            while f"rId{index}" in used_ids:
                index += 1
            used_ids.add(f"rId{index}")
            return f"rId{index}"

        # --- fontTable.xml: name the family, point at the part --------------
        table_root = etree.fromstring(entries["word/fontTable.xml"])
        w = f"{{{_NS_W}}}"

        def font_element(family: str):
            for el in table_root.findall(f"{w}font"):
                if el.get(f"{w}name") == family:
                    return el
            el = etree.SubElement(table_root, f"{w}font")
            el.set(f"{w}name", family)
            return el

        embedded_any = False
        for family, embed_tag, filename in wanted:
            source = _FONTS_DIR / filename
            part_name = f"word/fonts/{filename}"
            if part_name not in entries:
                if not source.exists():
                    _warn(warnings, f"Font file {filename} is missing from assets/fonts; "
                                    f"{family} is named but not embedded.")
                    continue
                entries[part_name] = source.read_bytes()
            target = f"fonts/{filename}"
            r_id = rel_by_target.get(target)
            if r_id is None:
                r_id = next_rid()
                rel = etree.SubElement(rels_root, f"{{{_NS_REL}}}Relationship")
                rel.set("Id", r_id)
                rel.set("Type", _FONT_REL_TYPE)
                rel.set("Target", target)
                rel_by_target[target] = r_id
            font_el = font_element(family)
            tag = f"{w}{embed_tag.split(':')[1]}"
            if font_el.find(tag) is None:
                embed_el = etree.SubElement(font_el, tag)
                embed_el.set(f"{{{_NS_R}}}id", r_id)
                embed_el.set(f"{w}fontKey", _NULL_FONT_KEY)
                embed_el.set(f"{w}subsetted", "0")
            embedded_any = True
        if not embedded_any:
            return
        entries[rels_name] = etree.tostring(rels_root, xml_declaration=True,
                                            encoding="UTF-8", standalone=True)
        entries["word/fontTable.xml"] = etree.tostring(
            table_root, xml_declaration=True, encoding="UTF-8", standalone=True)

        # --- [Content_Types].xml: register the ttf default -------------------
        ct_root = etree.fromstring(entries["[Content_Types].xml"])
        has_ttf = any(d.get("Extension") == "ttf"
                      for d in ct_root.findall(f"{{{_NS_CT}}}Default"))
        if not has_ttf:
            default = etree.SubElement(ct_root, f"{{{_NS_CT}}}Default")
            default.set("Extension", "ttf")
            default.set("ContentType", "application/x-font-ttf")
        entries["[Content_Types].xml"] = etree.tostring(
            ct_root, xml_declaration=True, encoding="UTF-8", standalone=True)

        # --- settings.xml: tell Word the package carries its fonts ----------
        settings_root = etree.fromstring(entries["word/settings.xml"])
        if settings_root.find(f"{w}embedTrueTypeFonts") is None:
            embed_flag = etree.Element(f"{w}embedTrueTypeFonts")
            embed_flag.set(f"{w}val", "1")
            anchor = None
            for tag in _SETTINGS_BEFORE_EMBED:
                found = settings_root.find(f"{w}{tag.split(':')[1]}")
                if found is not None:
                    anchor = found
            if anchor is not None:
                anchor.addnext(embed_flag)
            else:
                settings_root.insert(0, embed_flag)
            entries["word/settings.xml"] = etree.tostring(
                settings_root, xml_declaration=True, encoding="UTF-8", standalone=True)

        temp_path = output_path.with_suffix(".fonts.tmp")
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        temp_path.replace(output_path)
    except Exception as exc:  # noqa: BLE001 — embedding must never lose the document
        _warn(warnings, f"Could not embed fonts ({exc}); the document names them only.")


def _verify_embedded_fonts(output_path: Path, warnings: list[str]) -> None:
    """The embed step is only worth trusting if the bytes are really there."""
    try:
        with zipfile.ZipFile(output_path) as archive:
            embedded = [n for n in archive.namelist() if n.startswith("word/fonts/")]
    except Exception as exc:               # pragma: no cover - defensive
        _warn(warnings, f"Could not inspect the saved DOCX ({exc}).")
        return
    if not embedded:
        _warn(warnings, "No font survived embedding; "
                        f"install {DCN_FONT_PRIMARY} before exporting to PDF.")


def resolve_handwritten_font() -> tuple[str, bool, str]:
    """(font to use, is_substitute, note for the developer log).

    Walks DCN_FONT_CHAIN and returns the first face actually installed. A
    substitution is normal on a machine that never installed Coming Soon and is
    reported as information, not as a failure — the old wording ("the PDF export
    will substitute a face") read to users as something being broken.
    """
    for candidate in DCN_FONT_CHAIN:
        if is_font_installed(candidate):
            if candidate == DCN_FONT_CHAIN[0]:
                return candidate, False, f"Using {candidate}."
            return candidate, True, (
                f"{DCN_FONT_CHAIN[0]} is not installed; using {candidate} instead. "
                "The layout is unchanged."
            )
    return DCN_FONT_CHAIN[-1], True, (
        f"None of {', '.join(DCN_FONT_CHAIN[:-1])} is installed; "
        f"falling back to {DCN_FONT_CHAIN[-1]}."
    )


#: Header logo, best first. The full PW OnlyIAS lockup (mark + wordmark) is
#: preferred because it is what the reference notes carry; the bare PW mark is
#: the fallback for installs that only have that file.
LOGO_CANDIDATES = ("pw_onlyias_logo.png", "pw_onlyias_logo.jpg", "pw_logo.png")


def default_logo_path() -> Path | None:
    """The header logo that ships with the app, or None if none is installed."""
    for name in LOGO_CANDIDATES:
        candidate = ROOT_DIR / "assets" / name
        if candidate.exists():
            return candidate
    return None


# Everything XML 1.0 rejects, minus the three whitespace controls it allows
# (tab, LF, CR). Surrogates and the non-characters U+FFFE/U+FFFF are included
# because lxml refuses those too.
_XML_ILLEGAL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]"
)


def _strip_control_chars(text: str) -> tuple[str, int]:
    """Drop characters XML cannot carry; return (clean text, count removed)."""
    if not isinstance(text, str):
        return "", 0
    cleaned, count = _XML_ILLEGAL_RE.subn("", text)
    return cleaned, count


_TITLE_LINE_RE = re.compile(r"^([ \t]*)#LECTURE_TITLE:[ \t]*(.*)$", re.I | re.M)


def _apply_lecture_title(notes_text: str, lecture_title: str) -> tuple[str, str]:
    """Force the resolved title onto the notes; return (notes, note for the log).

    Three cases, in order:
      * a resolved title was passed  -> it replaces whatever is there;
      * the model wrote a placeholder -> it is rewritten into a real sentence;
      * the model wrote nothing at all -> the resolved title is inserted.
    """
    text = notes_text or ""
    resolved = (lecture_title or "").strip()
    match = _TITLE_LINE_RE.search(text)

    if match:
        existing = match.group(2).strip()
        if resolved:
            if existing == resolved:
                return text, ""
            replaced = _TITLE_LINE_RE.sub(
                lambda m: f"{m.group(1)}#LECTURE_TITLE: {resolved}", text, count=1)
            note = ("The response's lecture title was a placeholder and was replaced."
                    if is_placeholder(existing) else "")
            return replaced, note
        if is_placeholder(existing):
            # Nothing better was supplied, so salvage the human half rather than
            # printing "Lxx" — parse_marker_title drops the fake numbers.
            rebuilt = parse_marker_title(existing).display
            return (_TITLE_LINE_RE.sub(
                lambda m: f"{m.group(1)}#LECTURE_TITLE: {rebuilt}", text, count=1),
                "The response's lecture title was a placeholder; "
                "it was rewritten from the part that was real.")
        return text, ""

    if resolved:
        return f"#LECTURE_TITLE: {resolved}\n\n{text}".strip(), ""
    return text, ""


def write_upsc_dcn_docx(
    notes_text: str,
    output_path: Path,
    logo_path: Path | None = None,
    template_path: Path | None = None,
    footer_left: str = "PW OnlyIAS",
    footer_right: str = "Daily Class Notes",
    *,
    use_notes_box: bool = True,
    add_footer_strip: bool = True,
    language_code: str = "en",
    lecture_title: str = "",
    lecture_no: str = "",
    subject_line: str = "",
    visuals: dict[str, dict] | None = None,
    highlight_spans: "list[str] | tuple[str, ...]" = (),
) -> tuple[Path, list[str]]:
    """Write marker-formatted DCN notes to a PW OnlyIAS-style DOCX.

    Returns (output_path, warnings). Malformed markers never raise: they are
    degraded to body text and reported through the warnings list. The one
    deliberate exception is a title that is really an upload UUID — that
    raises, because shipping ``Lecture: b950d402 bc5d ...`` to a student is
    worse than failing the run.

    ``lecture_title`` overrides whatever ``#LECTURE_TITLE:`` the model produced,
    which is how a placeholder is kept off the page even when the model ignores
    the prompt.

    ``subject_line`` is the SUBJECT printed above the lecture title ("Indian
    Society" / "भारतीय समाज"). Empty means the document opens on the lecture
    line exactly as it did before this parameter existed — a subject is never
    guessed here.

    ``lecture_no`` supplies the number for the printed "Lecture N" when the
    title itself does not carry one. That is the normal case for a class with no
    slide deck: the title comes from the model, the number from what the user
    typed. It is used ONLY when the title has no number of its own, and an
    absent number is still omitted rather than invented.

    ``visuals`` maps each ``#FIGURE:`` id to its image (``path``, ``caption``,
    ``width_px``...) — see VisualRegistry.writer_map(). With no mapping, figure
    markers are skipped with a warning rather than typeset as text.

    ``highlight_spans`` are the exact stretches of ``notes_text`` that a QC
    correction produced; each is shaded light yellow. Every occurrence of a span
    is shaded, which is safe because src/correction_highlights.py only emits a
    span that occurs in the notes EXACTLY once — it widens or drops anything
    ambiguous. It builds the list from the QC audit records, so the highlight and
    the Error Report always describe the same set of changes.

    The Hindi build (``language_code="hi"``) is identical except for the face:
    Kalam fills every rFonts slot where the English build names Coming Soon.
    """
    warnings: list[str] = []
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # The question is which SCRIPT this prints in, not which language it is:
    # Marathi needs Kalam and the complex-script run properties for exactly
    # the same reason Hindi does.
    primary_font = (DEVANAGARI_FONT if is_devanagari(language_code)
                    else DCN_FONT_PRIMARY)

    # Model output occasionally carries stray control bytes. XML 1.0 forbids
    # them outright, so python-docx raises deep inside lxml on add_run() — far
    # from the cause and after a partial document has been built. Strip them
    # once, here, and the whole pipeline below is safe.
    notes_text, stripped = _strip_control_chars(notes_text)
    if stripped:
        warnings.append(
            f"Removed {stripped} control character(s) that XML cannot represent."
        )
    # The pipeline already did this; repeated here because the writer has other
    # callers, and "## H2:" or "$\rightarrow$" must never reach the page as text.
    notes_text, _ = normalize_notes_markup(notes_text, dcn=True)

    # The title on the page is decided here, not by the model: a #LECTURE_TITLE
    # the model invented (or copied from the prompt's example) is replaced by
    # the resolved one before anything is rendered.
    notes_text, title_note = _apply_lecture_title(notes_text, lecture_title)
    if title_note:
        _warn(warnings, title_note)
    title_match = _TITLE_LINE_RE.search(notes_text)
    final_title = title_match.group(2).strip() if title_match else ""
    if looks_like_hex_identifier(final_title):
        raise ValueError(
            f"Refusing to write a DCN document titled {final_title!r}: that is a "
            "file identifier, not a lecture title. Derive the topic from the "
            "content (or pass lecture_title) and retry."
        )

    document, from_template = _open_document(template_path, warnings)
    _apply_page_geometry(document.sections[0])
    _drop_styles_with_effects(document)
    _set_doc_defaults(document, primary_font=primary_font)
    _ensure_styles(document, warnings, primary_font=primary_font)
    _strip_single_borders(document)
    num_id = _ensure_bullet_numbering(document, warnings)
    _build_header(document.sections[0], logo_path, use_notes_box=use_notes_box,
                  from_template=from_template, warnings=warnings,
                  primary_font=primary_font)
    _build_footer(document, document.sections[0], footer_left, footer_right,
                  enabled=add_footer_strip, primary_font=primary_font)

    highlighter = _Highlighter(highlight_spans)
    _render_blocks(document, _parse_blocks(notes_text, warnings), num_id=num_id,
                   warnings=warnings, language_code=language_code, visuals=visuals,
                   primary_font=primary_font, subject_line=subject_line,
                   lecture_no=lecture_no, highlighter=highlighter)

    document.save(str(output_path))
    _embed_fonts(output_path, primary_font=primary_font, warnings=warnings)
    _verify_embedded_fonts(output_path, warnings)
    return output_path, warnings
