"""The PW OnlyIAS DMP sheet, drawn to the house format.

Everything in this module is FIXED house style — page size, margins, fonts,
the two shaded bars, the candidate and evaluator tables, the approach box, the
footer strip. Only the content passed in changes between documents.

The layout follows the content team's required sample (2026-10): a blue
"<Subject> DMP <No>" box with a cream topic box joined under it, the question
with its marks and words inline after it, and a bordered "How to Approach the
Question?" box — all in Times New Roman.

Why this is a second writer rather than more markers handed to the DCN writer:
the notes house style and the DMP house style are different documents. The notes
are set in a handwritten face (`Coming Soon` / `Kalam`) with a notes-box layout;
a DMP sheet is a serif exam sheet on US Letter with an evaluation table a
student fills in by hand. Expressing the second through the first's marker
vocabulary would mean teaching that writer about candidate-detail tables it
must never draw for notes.

Two fidelity rules taken from the reference sheets, both of which read as bugs
if you get them backwards:

* **The evaluation furniture is always English.** On a Hindi sheet the title,
  question, approach and answer are Devanagari, but `Name*:`, `Parameters`,
  `Introduction Competence` and `DMP Question` stay English — that is how the
  real sheets are printed, because the evaluator's workflow is English.
* **The two sheets have different footers.** The question paper says
  `Daily Mains Practice`; the model answer says `DMP Model Answer`.

Devanagari is carried by `w:cs` (Word classifies it as a complex script), so one
run can hold Hindi and the English terms embedded in it and each gets the right
face with no per-character decision.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, Twips

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# ---------------------------------------------------------------- page setup
PAGE_WIDTH_DXA = 12240          # US Letter, 8.5in
PAGE_HEIGHT_DXA = 15840         # 11in
MARGIN_DXA = 1440               # 1in on all four sides
HEADER_DIST_DXA = 454
FOOTER_DIST_DXA = 283
#: Page width minus both margins. Every full-width table is exactly this, and
#: the footer's right-aligned tab stop sits on it.
USABLE_DXA = PAGE_WIDTH_DXA - 2 * MARGIN_DXA        # 9360

# ------------------------------------------------------------------ palette
BLUE = "CFE2F3"                 # title bars, evaluator header rows, footer strip
CREAM = "FFF2CC"                # the topic bar on the answer sheet
BLACK = "000000"

# -------------------------------------------------------------------- fonts
#: The one face on the sheet, in every language: the required sample (2026-10)
#: sets the title, the candidate table, the question, the approach and the
#: answer all in Times New Roman. English used to be Arial throughout, with
#: Times reserved for Devanagari content.
#:
#: Times New Roman carries NO Devanagari glyphs — the renderer substitutes a
#: Devanagari face for those runs, which is precisely what the reference sheets
#: show: "DMP 05" in Times serif beside Devanagari in a substituted face. The
#: house style names it on all four attributes anyway, so the Latin fragments
#: embedded in Hindi prose ("(1775-1783)", "Example:") come out in Times.
SHEET_FONT = "Times New Roman"
#: The English administrative shell — cover sheet, evaluator table, the
#: "DMP Question" bar and the footer. Kept as a name so callers that style the
#: shell say so; it is the sheet's face like everything else now.
SHELL_FONT = SHEET_FONT
#: All Hindi/Marathi academic content (the same face; see SHEET_FONT).
CONTENT_FONT_DEVANAGARI = SHEET_FONT
#: Kalam belongs to the Notes product, not to a DMP. Named here so the rule is
#: greppable from this file rather than living only in a spec document.
FORBIDDEN_FONTS = ("Kalam", "Baloo")
BODY_PT = 12
BAR_PT = 20
#: The topic box under the title is a step smaller, as in the sample.
TOPIC_PT = 18
#: The "DMP Question" bar is smaller than the title and topic bars.
QUESTION_BAR_PT = 16


def content_font(language: str | None) -> str:
    """The face for academic content in `language` — the sheet's one face.

    Still a function, and still asked per language, so a later house rule
    that splits the faces again is one edit here rather than at every caller.
    """
    return SHEET_FONT


#: Back-compat alias. `LATIN_FONT` named the one font when there was one.
LATIN_FONT = SHELL_FONT
#: The bullet glyphs (● ○ ▪) are drawn in Arial whatever the text face is:
#: it is the face every renderer on every machine has them in.
_BULLET_GLYPH_FONT = "Arial"

# ------------------------------------------------------------------ bullets
_BULLET_GLYPHS = ("●", "○", "▪")     # ● filled, ○ hollow, ▪ square
_BULLET_INDENT_STEP = 360
_BULLET_HANGING = 360
_BULLET_NUM_NAME = "PWDmpBullets"

#: Closes the model answer, exactly as the reference sheets do. Not a rule —
#: the reference prints four hollow circles, centred.
ANSWER_END_MARK = "○○○○"


# ---------------------------------------------------------------------------
# language-dependent text (content-facing only — never the evaluator furniture)
# ---------------------------------------------------------------------------
#: The question prefix. Hindi sheets open a question with "प्र.", not "Q.".
_Q_PREFIX = {"en": "Q.", "hi": "प्र.", "mr": "प्र."}

#: The approach box's title, as the required sample words it.
APPROACH_HEADING = {
    "en": "How to Approach the Question?",
    "hi": "प्रश्न को हल करने का दृष्टिकोण",
    "mr": "प्रश्न सोडवण्याचा दृष्टिकोन",
}

_APPROACH_LABELS = {
    "en": {"introduction": "Introduction", "body": "Body", "conclusion": "Conclusion"},
    "hi": {"introduction": "भूमिका", "body": "मुख्य भाग", "conclusion": "निष्कर्ष"},
    "mr": {"introduction": "प्रस्तावना", "body": "मुख्य भाग", "conclusion": "निष्कर्ष"},
}

ANSWER_LABEL = {"en": "Answer", "hi": "उत्तर", "mr": "उत्तर"}

_MARKS_TAG = {
    # Marks first, then words, run on after the question — the required
    # sample's "(10Marks, 150 words)". The older sheets printed words first,
    # on a line of their own.
    "en": "({marks} Marks, {words} words)",
    "hi": "({marks} अंक, {words} शब्द)",
    "mr": "({marks} गुण, {words} शब्द)",
}

_UNANSWERED = {
    "en": "Not answered yet.",
    "hi": "अभी तक उत्तर नहीं लिखा गया।",
    "mr": "अद्याप उत्तर तयार केलेले नाही.",
}

# ---------------------------------------------------------------------------
# fixed English furniture — identical on every sheet, in every language
# ---------------------------------------------------------------------------
#: An empty right-hand cell under "Marks Scored:" is not a blank cell: the
#: marks box is one cell spanning the Contact No and Reg No rows, as in the
#: required sample, so there is room to write a score in it.
CANDIDATE_ROWS = (
    ("Name*:", ""),
    ("Contact No*:", "Marks Scored:"),
    ("Reg No:", ""),
    ("Email Id*:", "Date:"),
    ("Batch Name*:", "Evaluator's Id:"),
)
#: Measured off the reference sheet rather than guessed: the divider between
#: the two columns sits on the same x as the evaluator table's second divider,
#: which is what makes the two tables read as one block.
CANDIDATE_COL_DXA = (5250, 4110)

EVALUATOR_TITLE = "(For Evaluator to fill)"
EVALUATOR_HEADERS = ("Parameters", "(Rate out of 5)", "Instructions for students")
EVALUATOR_PARAMETERS = (
    "Introduction Competence",
    "Content Coverage",
    "Conclusion Competence",
    "Presentation & Structure Competence",
    "Language Proficiency",
)
#: Also measured off the reference. The middle column is wider than it looks
#: it needs to be, and that is deliberate: at 1560 the heading "(Rate out of 5)"
#: wraps onto two lines and the header row grows a second line the reference
#: does not have.
EVALUATOR_COL_DXA = (3240, 2010, 4110)
EVALUATOR_INSTRUCTIONS_LEAD = (
    "Please make sure that following details are mentioned in Answer sheet:")
EVALUATOR_INSTRUCTIONS_ITEMS = (
    "Student's Name", "Contact Number", "Email Id", "Batch Name")

QUESTION_BAR_TEXT = "DMP Question"
FOOTER_LEFT = "PW OnlyIAS"
FOOTER_RIGHT_QUESTIONS = "Daily Mains Practice"
FOOTER_RIGHT_ANSWERS = "DMP Model Answer"


def footer_right(kind: str, language: str) -> str:
    """What the right of the footer strip says.

    The English answer sheet is a companion to a separate question paper and
    names itself "DMP Model Answer". The Hindi sheet is the whole deliverable
    in one file — answer, cover and question together — so calling it a model
    answer would be wrong on two of its three pages, and the reference prints
    "Daily Mains Practice" throughout. The house rule for Devanagari sheets is
    spelled out in the spec, and the sample agrees with it.
    """
    if kind == "answers" and _lang(language) == "en":
        return FOOTER_RIGHT_ANSWERS
    return FOOTER_RIGHT_QUESTIONS

BAR_ROW_HEIGHT_DXA = 700
#: The title box on the cover is taller than on the question pages — about
#: 0.85in in the required sample against 0.5in.
COVER_BAR_ROW_HEIGHT_DXA = 1200


def _lang(language: str | None) -> str:
    code = (language or "en").strip().lower()[:2]
    return code if code in {"en", "hi", "mr"} else "en"


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def _style_run(run, *, bold: bool = False, size_pt: int = BODY_PT,
               font: str = SHELL_FONT) -> None:
    """One run, in one font, named on all four attributes.

    ascii/hAnsi/eastAsia/cs all carry the same family deliberately: Word reads
    `w:cs` for complex scripts, so a Hindi run whose `w:cs` still said Arial
    would render its Devanagari in whatever the renderer felt like.
    """
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attribute), font)
    # Complex-script size has to be set too, or Word renders Devanagari at the
    # style default while the Latin in the same run honours w:sz.
    szcs = rpr.find(qn("w:szCs"))
    if szcs is None:
        szcs = OxmlElement("w:szCs")
        rpr.append(szcs)
    szcs.set(qn("w:val"), str(int(size_pt * 2)))
    if bold:
        bcs = rpr.find(qn("w:bCs"))
        if bcs is None:
            bcs = OxmlElement("w:bCs")
            rpr.append(bcs)


def _add_run(paragraph, text: str, *, bold: bool = False, size_pt: int = BODY_PT,
             font: str = SHELL_FONT):
    run = paragraph.add_run(text)
    _style_run(run, bold=bold, size_pt=size_pt, font=font)
    return run


def _para(container, text: str = "", *, bold: bool = False, size_pt: int = BODY_PT,
          align=None, space_after: int = 6, space_before: int = 0,
          font: str = SHELL_FONT):
    paragraph = container.add_paragraph()
    if align is not None:
        paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.space_before = Pt(space_before)
    if text:
        _add_run(paragraph, text, bold=bold, size_pt=size_pt, font=font)
    return paragraph


#: The order w:pPr's children must appear in. WordprocessingML fixes it, and
#: Word repairs (or silently ignores) a paragraph whose properties are out of
#: sequence — which is subtle, because the file still opens. Only the elements
#: this module writes need to be listed, plus the ones python-docx writes
#: around them.
_PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
    "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
    "suppressOverlap", "jc", "textDirection", "textAlignment",
    "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
    "pPrChange",
)


def _ppr_insert(ppr, element) -> None:
    """Put `element` in its schema position inside w:pPr.

    Appending instead is what put w:shd after w:tabs in the footer and left the
    blue strip's right-hand text sitting in the middle of the page.
    """
    tag = element.tag.split("}", 1)[-1]
    try:
        index = _PPR_ORDER.index(tag)
    except ValueError:
        ppr.append(element)
        return
    for existing in ppr:
        existing_tag = existing.tag.split("}", 1)[-1]
        if existing_tag not in _PPR_ORDER:
            continue
        if _PPR_ORDER.index(existing_tag) > index:
            existing.addprevious(element)
            return
    ppr.append(element)


def _shade(element, fill: str) -> None:
    """Apply CLEAR shading. Never SOLID — that fills the foreground instead."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    if element.tag.endswith("}pPr"):
        _ppr_insert(element, shd)
    else:
        element.append(shd)


def _shade_cell(cell, fill: str) -> None:
    _shade(cell._tc.get_or_add_tcPr(), fill)


def _cell_margins(cell, *, top: int = 120, bottom: int = 120,
                  left: int = 108, right: int = 108) -> None:
    tc_mar = OxmlElement("w:tcMar")
    for name, value in (("top", top), ("left", left),
                        ("bottom", bottom), ("right", right)):
        node = OxmlElement(f"w:{name}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        tc_mar.append(node)
    cell._tc.get_or_add_tcPr().append(tc_mar)


def _table_borders(table, size: int = 8) -> None:
    """Black single-line borders on every edge and every inside line."""
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), str(size))
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), BLACK)
        borders.append(node)
    table._tbl.tblPr.append(borders)


def _fix_widths(table, widths_dxa) -> None:
    """Pin the grid AND every cell.

    python-docx writes a tblGrid from the first row only, and Word will happily
    re-flow a table whose cells disagree with it. Both have to be set, in DXA —
    percentage widths render differently once the file reaches Google Docs.
    """
    table.autofit = False
    table.allow_autofit = False
    for index, width in enumerate(widths_dxa):
        if index < len(table.columns):
            table.columns[index].width = Twips(width)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            if index < len(widths_dxa):
                cell.width = Twips(widths_dxa[index])


def _row_height(row, dxa: int) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    height = OxmlElement("w:trHeight")
    height.set(qn("w:val"), str(dxa))
    height.set(qn("w:hRule"), "atLeast")
    tr_pr.append(height)


def _clear_cell(cell) -> None:
    """Drop the empty paragraph python-docx puts in every new cell."""
    for paragraph in list(cell.paragraphs):
        paragraph._element.getparent().remove(paragraph._element)


def _cell_text(cell, text: str, *, bold: bool = False, size_pt: int = BODY_PT,
               align=None, space_after: int = 2, font: str = SHELL_FONT) -> None:
    _clear_cell(cell)
    _para(cell, text, bold=bold, size_pt=size_pt, align=align,
          space_after=space_after, font=font)


# ---------------------------------------------------------------------------
# bullets
# ---------------------------------------------------------------------------

def _abstract_num_xml(abstract_id: int) -> str:
    levels = []
    for ilvl in range(9):
        glyph = _BULLET_GLYPHS[ilvl % len(_BULLET_GLYPHS)]
        left = _BULLET_INDENT_STEP * (ilvl + 1)
        levels.append(
            f'<w:lvl w:ilvl="{ilvl}">'
            f'<w:start w:val="1"/><w:numFmt w:val="bullet"/>'
            f'<w:lvlText w:val="{glyph}"/><w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{left}" w:hanging="{_BULLET_HANGING}"/></w:pPr>'
            f'<w:rPr><w:rFonts w:ascii="{_BULLET_GLYPH_FONT}"'
            f' w:hAnsi="{_BULLET_GLYPH_FONT}" w:cs="{_BULLET_GLYPH_FONT}"/></w:rPr>'
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


def ensure_bullet_numbering(doc, warnings: list[str]) -> int | None:
    """numId for the house bullet ladder, defining it once per document.

    Real Word numbering rather than a typed "● " is what makes wrapped bullet
    text line up under the text instead of under the glyph.
    """
    try:
        numbering = doc.part.numbering_part.element
    except Exception as exc:  # noqa: BLE001 — fall back to literal glyphs
        warnings.append(f"No numbering part available ({exc}); bullets use literal glyphs.")
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

    abstract_ids = [_int_attr(a, "w:abstractNumId")
                    for a in numbering.findall(qn("w:abstractNum"))]
    num_ids = [_int_attr(n, "w:numId") for n in numbering.findall(qn("w:num"))]
    abstract_id = max([i for i in abstract_ids if i is not None], default=-1) + 1
    num_id = max([i for i in num_ids if i is not None], default=0) + 1
    try:
        abstract = parse_xml(_abstract_num_xml(abstract_id))
        first_num = numbering.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(abstract)
        else:
            numbering.append(abstract)
        numbering.append(parse_xml(
            f'<w:num xmlns:w="{_NS_W}" w:numId="{num_id}">'
            f'<w:abstractNumId w:val="{abstract_id}"/></w:num>'))
    except Exception as exc:  # noqa: BLE001 — defensive
        warnings.append(f"Could not define bullet numbering ({exc}).")
        return None
    return num_id


def _apply_numbering(paragraph, num_id: int, ilvl: int) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    numpr = ppr.get_or_add_numPr()
    ilvl_el = numpr.get_or_add_ilvl()
    ilvl_el.set(qn("w:val"), str(ilvl))
    num_el = numpr.get_or_add_numId()
    num_el.set(qn("w:val"), str(num_id))


def add_bullet(container, num_id: int | None, level: int, segments,
               *, justify: bool = True, font: str = SHELL_FONT):
    """One bullet. `segments` is [(text, bold), ...] so a bold lead-in label
    and its regular explanation live in the same paragraph."""
    paragraph = container.add_paragraph()
    if justify:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    paragraph.paragraph_format.space_after = Pt(4)
    if num_id is not None:
        _apply_numbering(paragraph, num_id, level)
    else:
        # No numbering part: fall back to a typed glyph so the document is
        # still readable rather than a flat list of sentences.
        indent = _BULLET_INDENT_STEP * (level + 1)
        paragraph.paragraph_format.left_indent = Twips(indent)
        _add_run(paragraph, f"{_BULLET_GLYPHS[level % len(_BULLET_GLYPHS)]} ",
                 font=font)
    for chunk, bold in segments:
        if chunk:
            _add_run(paragraph, chunk, bold=bold, font=font)
    return paragraph


# ---------------------------------------------------------------------------
# page furniture
# ---------------------------------------------------------------------------

def apply_page_setup(section) -> None:
    section.page_width = Twips(PAGE_WIDTH_DXA)
    section.page_height = Twips(PAGE_HEIGHT_DXA)
    section.top_margin = Twips(MARGIN_DXA)
    section.bottom_margin = Twips(MARGIN_DXA)
    section.left_margin = Twips(MARGIN_DXA)
    section.right_margin = Twips(MARGIN_DXA)
    section.header_distance = Twips(HEADER_DIST_DXA)
    section.footer_distance = Twips(FOOTER_DIST_DXA)


def _paragraph_border(paragraph, edge: str, size: int = 6) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        _ppr_insert(ppr, pbdr)
    node = OxmlElement(f"w:{edge}")
    node.set(qn("w:val"), "single")
    node.set(qn("w:sz"), str(size))
    node.set(qn("w:space"), "1")
    node.set(qn("w:color"), BLACK)
    pbdr.append(node)


def build_header(section, logo_path: Path | None, warnings: list[str]) -> None:
    """Centred PW OnlyIAS logo with a thin rule under it, on every page."""
    header = section.header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    for run in list(paragraph.runs):
        run._element.getparent().remove(run._element)

    placed = False
    if logo_path and Path(logo_path).exists():
        try:
            run = paragraph.add_run()
            # 2.87in x 0.70in, the reference lockup size.
            run.add_picture(str(logo_path), width=Emu(2757488), height=Emu(671875))
            placed = True
        except Exception as exc:  # noqa: BLE001 — a missing logo is not fatal
            warnings.append(f"Header logo could not be placed ({exc}).")
    if not placed:
        _add_run(paragraph, FOOTER_LEFT, bold=True, size_pt=BAR_PT)
    _paragraph_border(paragraph, "bottom", size=6)


#: The built-in `Footer` style defines its own tab stops — a CENTRE stop at
#: 4680 and a right stop at 9360. Paragraph tabs are MERGED with the style's,
#: not substituted for them, so a single tab lands on the centre stop and the
#: sheet name sits in the middle of the page instead of against the right
#: margin. The centre stop has to be explicitly cleared.
_FOOTER_STYLE_CENTRE_TAB_DXA = 4680


def _footer_tab_stops(paragraph) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    for existing in ppr.findall(qn("w:tabs")):
        ppr.remove(existing)
    tabs = OxmlElement("w:tabs")
    clear = OxmlElement("w:tab")
    clear.set(qn("w:val"), "clear")
    clear.set(qn("w:pos"), str(_FOOTER_STYLE_CENTRE_TAB_DXA))
    tabs.append(clear)
    right = OxmlElement("w:tab")
    right.set(qn("w:val"), "right")
    right.set(qn("w:pos"), str(USABLE_DXA))
    tabs.append(right)
    _ppr_insert(ppr, tabs)


def build_footer(section, right_text: str) -> None:
    """Blue strip, top rule, `PW OnlyIAS` left and the sheet's name right.

    The two are pushed apart by a right-aligned tab stop on the usable width,
    not by spaces — spaces move as soon as the font does.
    """
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_after = Pt(0)
    for run in list(paragraph.runs):
        run._element.getparent().remove(run._element)
    _footer_tab_stops(paragraph)
    _shade(paragraph._p.get_or_add_pPr(), BLUE)
    _paragraph_border(paragraph, "top", size=6)
    _add_run(paragraph, FOOTER_LEFT)
    _add_run(paragraph, "\t")
    _add_run(paragraph, right_text)


def add_page_break(doc) -> None:
    from docx.enum.text import WD_BREAK

    paragraph = doc.add_paragraph()
    paragraph_format = paragraph.paragraph_format
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(0)
    # One point high. At the body's 12 pt, a page that the answer had filled
    # to the last line pushed this paragraph onto the next page, where its
    # break then threw a second one — a blank page between two answers.
    paragraph_format.line_spacing = Pt(1)
    run = paragraph.add_run()
    run.font.size = Pt(1)
    run.add_break(WD_BREAK.PAGE)


# ---------------------------------------------------------------------------
# the blocks
# ---------------------------------------------------------------------------

def add_bar(doc, text: str, fill: str, *, font: str = SHELL_FONT,
            size_pt: int = BAR_PT, row_height: int = BAR_ROW_HEIGHT_DXA) -> None:
    """One full-width shaded bar: bold, centred, in a single-cell table."""
    table = doc.add_table(rows=1, cols=1)
    _table_borders(table)
    _fix_widths(table, (USABLE_DXA,))
    _row_height(table.rows[0], row_height)
    cell = table.cell(0, 0)
    _shade_cell(cell, fill)
    _cell_margins(cell, top=150, bottom=150)
    _cell_text(cell, text, bold=True, size_pt=size_pt,
               align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0, font=font)
    _vertical_center(cell)


def add_title_block(doc, title: str, topic: str = "", *, font: str = SHEET_FONT,
                    row_height: int = BAR_ROW_HEIGHT_DXA) -> None:
    """The blue "<Subject> DMP <No>" box, and the cream topic box joined under it.

    One table with a row each, not two tables. Two tables with nothing between
    them are joined by Word and drawn apart by LibreOffice; the sample has the
    two boxes sharing one border, and a single table does that in both. No
    topic, no cream row — an empty cream box is a label over nothing.
    """
    rows = [(title, BLUE, BAR_PT, row_height)]
    if (topic or "").strip():
        rows.append((topic.strip(), CREAM, TOPIC_PT, BAR_ROW_HEIGHT_DXA))
    table = doc.add_table(rows=len(rows), cols=1)
    _table_borders(table)
    _fix_widths(table, (USABLE_DXA,))
    for index, (text, fill, size_pt, height) in enumerate(rows):
        _row_height(table.rows[index], height)
        cell = table.cell(index, 0)
        _shade_cell(cell, fill)
        _cell_margins(cell, top=100, bottom=100)
        _cell_text(cell, text, bold=True, size_pt=size_pt,
                   align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0, font=font)
        _vertical_center(cell)


def add_gap(doc, pt: int = 6) -> None:
    """A thin fixed-height paragraph between two tables.

    Without one, the table that follows is drawn as more rows of the one above
    — the "DMP Question" bar would read as a third row of the title box.
    """
    paragraph = doc.add_paragraph()
    paragraph_format = paragraph.paragraph_format
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(0)
    paragraph_format.line_spacing = Pt(pt)


def add_question_bar(doc) -> None:
    """The "DMP Question" bar — English shell, and smaller than the title bars.

    A shorter row too: at the title bars' height a 16 pt line floats in the
    middle of a band sized for 20 pt.
    """
    add_bar(doc, QUESTION_BAR_TEXT, BLUE, font=SHELL_FONT,
            size_pt=QUESTION_BAR_PT, row_height=420)


def _vertical_center(cell) -> None:
    valign = OxmlElement("w:vAlign")
    valign.set(qn("w:val"), "center")
    cell._tc.get_or_add_tcPr().append(valign)


def add_candidate_table(doc) -> None:
    """The details the student fills in. Labels bold, values left blank.

    Two merges, both from the required sample: the name runs the full width,
    and "Marks Scored:" is one tall cell beside Contact No and Reg No.
    """
    table = doc.add_table(rows=len(CANDIDATE_ROWS), cols=2)
    _table_borders(table)
    _fix_widths(table, CANDIDATE_COL_DXA)
    marks_row = None
    for index, (left, right) in enumerate(CANDIDATE_ROWS):
        row = table.rows[index]
        _row_height(row, 400)
        if not right and index == 0:
            # The name runs the full width in the reference — there is no
            # divider on that row, and nothing to put on the right of it.
            cell = row.cells[0].merge(row.cells[1])
            _cell_margins(cell, top=60, bottom=60)
            _cell_text(cell, left, bold=True)
            continue
        _cell_text(row.cells[0], left, bold=True)
        _cell_margins(row.cells[0], top=60, bottom=60)
        if not right and marks_row is not None:
            # The cell under "Marks Scored:" — joined to it rather than drawn
            # as a blank box of its own.
            merged = table.cell(marks_row, 1).merge(row.cells[1])
            _clear_cell(merged)
            _para(merged, CANDIDATE_ROWS[marks_row][1], bold=True, space_after=2)
            continue
        if right == "Marks Scored:":
            marks_row = index
        _cell_text(row.cells[1], right, bold=True)
        _cell_margins(row.cells[1], top=60, bottom=60)


def add_evaluator_table(doc) -> None:
    """The five rated parameters, with the instructions cell spanning them."""
    rows = 2 + len(EVALUATOR_PARAMETERS)
    table = doc.add_table(rows=rows, cols=3)
    _table_borders(table)
    _fix_widths(table, EVALUATOR_COL_DXA)

    # Row 1 — one shaded cell across all three columns.
    title_cell = table.cell(0, 0).merge(table.cell(0, 2))
    _shade_cell(title_cell, BLUE)
    _cell_text(title_cell, EVALUATOR_TITLE, bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER)

    # Row 2 — the column headings. Tight side margins: "(Rate out of 5)" is
    # one line in the reference, and the default 108-twip padding either side
    # is enough to wrap it in a 1560-twip column.
    for index, heading in enumerate(EVALUATOR_HEADERS):
        cell = table.cell(1, index)
        _shade_cell(cell, BLUE)
        _cell_margins(cell, top=40, bottom=40, left=40, right=40)
        _cell_text(cell, heading, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    # Rows 3-7 — the parameters, and an empty box to write the score in.
    for offset, parameter in enumerate(EVALUATOR_PARAMETERS):
        row_index = 2 + offset
        _cell_text(table.cell(row_index, 0), parameter, bold=True)
        _cell_text(table.cell(row_index, 1), "")

    # The right-hand column is ONE cell spanning every parameter row.
    instructions = table.cell(2, 2)
    for offset in range(1, len(EVALUATOR_PARAMETERS)):
        instructions = instructions.merge(table.cell(2 + offset, 2))
    _clear_cell(instructions)
    _para(instructions, EVALUATOR_INSTRUCTIONS_LEAD, space_after=2)
    for number, item in enumerate(EVALUATOR_INSTRUCTIONS_ITEMS, start=1):
        paragraph = _para(instructions, space_after=1)
        paragraph.paragraph_format.left_indent = Twips(300)
        _add_run(paragraph, f"{number}. ")
        _add_run(paragraph, item)


def marks_tag(marks: int, words: int, language: str) -> str:
    return _MARKS_TAG[_lang(language)].format(words=int(words), marks=int(marks))


def add_question(doc, question: str, marks: int, words: int, language: str,
                 *, space_before: int = 10) -> None:
    """`Q.` then the question in bold, its marks and words run on after it.

    The required sample prints "Q. <question>   (10Marks, 150 words)" as one
    paragraph. The tag's own spaces are non-breaking, so it wraps to the next
    line whole rather than leaving "(10 Marks," at the end of one line and
    "150 words)" at the start of the next.
    """
    code = _lang(language)
    font = content_font(code)
    paragraph = _para(doc, align=WD_ALIGN_PARAGRAPH.LEFT, space_after=6,
                      space_before=space_before)
    _add_run(paragraph, f"{_Q_PREFIX[code]} ", font=font)
    _add_run(paragraph, question.strip(), bold=True, font=font)
    _add_run(paragraph, "   " + marks_tag(marks, words, code).replace(" ", "\u00a0"),
             bold=True, font=font)


def add_approach_box(doc, approach: dict, language: str,
                     num_id: int | None) -> None:
    """The bordered "How to Approach the Question?" box before the answer.

    Laid out as the required sample is: the title bold and centred, then
    "Introduction:" as a paragraph, "Body:" with the parts of the body as
    bullets under it, and "Conclusion:" as a paragraph. It was three bullets
    under the word "Approach".

    Drawn only when there is an approach to draw. An empty box would be a
    heading over nothing, which is worse than the box being absent.
    """
    code = _lang(language)
    font = content_font(code)
    labels = _APPROACH_LABELS[code]
    texts = {key: (approach.get(key) or "").strip()
             for key in ("introduction", "body", "conclusion")}
    subpoints = [str(s).strip() for s in (approach.get("body_subpoints") or [])
                 if str(s).strip()]
    if not any(texts.values()) and not subpoints:
        return

    table = doc.add_table(rows=1, cols=1)
    _table_borders(table)
    _fix_widths(table, (USABLE_DXA,))
    cell = table.cell(0, 0)
    _clear_cell(cell)
    _cell_margins(cell, top=160, bottom=100, left=400, right=400)
    _para(cell, APPROACH_HEADING[code], bold=True,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10, font=font)

    for key in ("introduction", "body", "conclusion"):
        text = texts[key]
        has_points = key == "body" and subpoints
        if not text and not has_points:
            continue
        paragraph = _para(cell, space_after=6 if has_points else 10, font=font)
        _add_run(paragraph, f"{labels[key]}:", bold=True, font=font)
        if text:
            _add_run(paragraph, f" {text}", font=font)
        if has_points:
            for point in subpoints:
                add_bullet(cell, num_id, 0, [(point, False)],
                           justify=False, font=font)
            # The gap the sample leaves between the last bullet and the
            # conclusion, which a bullet's own 4 pt does not give.
            cell.paragraphs[-1].paragraph_format.space_after = Pt(10)


def add_answer_body(doc, answer_blocks, num_id: int | None,
                    *, font: str = SHELL_FONT) -> None:
    """The model answer, from the parsed block list.

    Each block is one of:
      ("paragraph", [(text, bold), ...])
      ("heading",   "text")
      ("bullet",    level, [(text, bold), ...])

    `answer_parse.parse` no longer produces headings — a Mains answer is a
    paragraph, points and a paragraph — but a caller handing in raw blocks
    still gets them drawn rather than dropped.
    """
    for block in answer_blocks:
        kind = block[0]
        if kind == "heading":
            _para(doc, block[1], bold=True, space_before=6, space_after=3,
                  font=font)
        elif kind == "bullet":
            add_bullet(doc, num_id, block[1], block[2], font=font)
        else:
            paragraph = _para(doc, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=6)
            for chunk, bold in block[1]:
                if chunk:
                    _add_run(paragraph, chunk, bold=bold, font=font)


def add_answer_label(doc, language: str) -> None:
    """The bold `Answer:` / `उत्तर:` line that opens the model answer."""
    _para(doc, f"{ANSWER_LABEL[_lang(language)]}:", bold=True,
          space_before=6, space_after=3, font=content_font(language))


def add_answer_end_mark(doc, language: str = "en") -> None:
    _para(doc, ANSWER_END_MARK, align=WD_ALIGN_PARAGRAPH.CENTER,
          space_before=10, space_after=0, font=content_font(language))


def add_unanswered_note(doc, language: str) -> None:
    _para(doc, _UNANSWERED[_lang(language)], space_after=6,
          font=content_font(language))


def answer_label(language: str) -> str:
    return ANSWER_LABEL[_lang(language)]


def new_document(warnings: list[str], logo_path: Path | None,
                 footer_right: str):
    """A blank house-format document: page set up, header and footer drawn."""
    doc = Document()
    section = doc.sections[0]
    apply_page_setup(section)
    build_header(section, logo_path, warnings)
    build_footer(section, footer_right)
    _set_default_style(doc)
    _use_current_table_layout(doc)
    return doc


#: Word 2013+ layout. python-docx's template says Word 2010 (14), and in that
#: mode Word places a table so the TEXT in its first cell meets the margin —
#: the border hangs out to the left by the cell's padding. The approach box's
#: wide padding pushed it a quarter-inch past every other box on the page when
#: the PDF came from Word; LibreOffice drew it aligned either way.
_COMPATIBILITY_MODE = "15"


def _use_current_table_layout(doc) -> None:
    for setting in doc.settings.element.iter(qn("w:compatSetting")):
        if setting.get(qn("w:name")) == "compatibilityMode":
            setting.set(qn("w:val"), _COMPATIBILITY_MODE)


def _set_default_style(doc) -> None:
    """Times New Roman 12 pt as the document default.

    The shell font, not a content font: every content run names its own face
    explicitly, so what this governs is the furniture and anything that somehow
    reaches the page unstyled — both of which are English.
    """
    style = doc.styles["Normal"]
    style.font.name = SHELL_FONT
    style.font.size = Pt(BODY_PT)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attribute), SHELL_FONT)
    paragraph_format = style.paragraph_format
    paragraph_format.space_after = Pt(8)
    paragraph_format.line_spacing = 1.08
