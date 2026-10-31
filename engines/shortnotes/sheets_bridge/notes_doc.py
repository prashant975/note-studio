"""Build an editable review document from generated notes.

The automation's first artifact is a Google Doc a person can read, correct and
sign off before the final PDF is made. Google Docs cannot be written directly
with useful formatting from Python, so a .docx is built here and uploaded to
Drive *with conversion*, which produces a native Google Doc.

Every element carries over as editable content: headings, bullets, real
tables, comparison blocks, symbol lists and the mind map. The PW logo sits in
the page header and a faint copy of it sits behind the text as a watermark.
"""
from __future__ import annotations

import copy
import io
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Emu, Inches, Pt, RGBColor

from .inline_text import bold_runs, inline_runs, plain_text, segments

LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "watermarks" / "center-logo.png"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

_INK = RGBColor(0x1F, 0x29, 0x37)
_ACCENT = RGBColor(0x8B, 0x29, 0x42)
_MUTED = RGBColor(0x6B, 0x72, 0x80)
_MATH = RGBColor(0x0F, 0x76, 0x6E)


def output_doc_name(source_name: str) -> str:
    """'<source without .pdf> - Concise Notes (Review)' - Docs carry no extension."""
    base = (source_name or "").strip()
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return f"{base.strip() or 'Lecture'} - Concise Notes (Review)"


# --------------------------------------------------------------------------- #
# Watermark
# --------------------------------------------------------------------------- #
def _watermark_xml(relationship_id: str, width_pt: float = 320, height_pt: float = 320) -> str:
    """A classic Word picture watermark (VML shape inside the header)."""
    return (
        f'<w:p {nsdecls("w", "r")}>'
        "<w:pPr><w:pStyle w:val=\"Header\"/></w:pPr>"
        "<w:r><w:pict>"
        f'<v:shape xmlns:v="urn:schemas-microsoft-com:vml" '
        f'xmlns:o="urn:schemas-microsoft-com:office:office" '
        f'id="PWWatermark" o:spid="_x0000_s2049" type="#_x0000_t75" '
        f'style="position:absolute;margin-left:0;margin-top:0;width:{width_pt}pt;height:{height_pt}pt;'
        f'z-index:-251658752;mso-position-horizontal:center;mso-position-horizontal-relative:margin;'
        f'mso-position-vertical:center;mso-position-vertical-relative:margin" o:allowincell="f">'
        f'<v:imagedata r:id="{relationship_id}" o:title="PW" gain="19661f" blacklevel="22938f"/>'
        "</v:shape>"
        "</w:pict></w:r></w:p>"
    )


WATERMARK_OPACITY = 0.10
WATERMARK_POINTS = 260


def faded_logo(image_path: Path, opacity: float = WATERMARK_OPACITY) -> io.BytesIO:
    """A pale copy of the logo, faded in the pixels themselves.

    Word's VML washout attributes (gain/blacklevel) are dropped when Google
    converts the file, which would leave a solid black logo over the text, so
    the fade has to be baked in before upload.
    """
    from PIL import Image

    with Image.open(image_path) as source:
        logo = source.convert("RGBA")
    canvas = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    faint = logo.copy()
    alpha = faint.getchannel("A").point(lambda value: int(value * max(0.0, min(1.0, opacity))))
    faint.putalpha(alpha)
    canvas.alpha_composite(faint)
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _add_watermark(document: Document, image_path: Path) -> bool:
    """Put a faint centred logo behind the text of every page."""
    try:
        header = document.sections[0].header
        header.is_linked_to_previous = False
        # get_or_add_image returns (rId, image) and already registers the
        # relationship on this header part.
        rel_id, _image = header.part.get_or_add_image(faded_logo(image_path))
        header._element.append(parse_xml(_watermark_xml(rel_id, WATERMARK_POINTS, WATERMARK_POINTS)))
        return True
    except Exception:  # noqa: BLE001 - a missing watermark must never fail a job
        return False


def _add_header_logo(document: Document, image_path: Path, signature: str) -> None:
    """Small logo plus the teacher signature at the top of every page."""
    header = document.sections[0].header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = paragraph.add_run()
    try:
        run.add_picture(str(image_path), height=Inches(0.32))
    except Exception:  # noqa: BLE001
        pass
    if signature:
        tail = paragraph.add_run("    " + signature)
        tail.font.size = Pt(9)
        tail.font.color.rgb = _MUTED


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #
def _style_text(run, *, size: int, bold: bool = False, color: RGBColor = _INK, italic: bool = False) -> None:
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    run.font.color.rgb = color


def _write_rich(paragraph, text: object, *, size: int, color: RGBColor = _INK, bold: bool = False) -> None:
    """Add the text with its markup resolved: **bold** and maths.

    Formulas become **native Word equations** (equation_kit's OMML), which
    Drive keeps when it converts the file into a Google Doc: real fractions,
    vector arrows and true sub/superscripts, still editable by the reviewer.
    If a formula cannot be built, the kit writes its Unicode form instead.
    """
    from equation_kit import RENDER_OMML, add_inline_math

    for kind, payload in segments(text):
        if kind == "text":
            for chunk, is_bold in bold_runs(payload):
                _style_text(paragraph.add_run(chunk), size=size, bold=bold or is_bold, color=color)
            continue
        try:
            add_inline_math(paragraph, payload, mode=RENDER_OMML, size=Pt(size))
        except Exception:  # noqa: BLE001 - never lose a formula over a render error
            from .inline_text import latex_to_text

            _style_text(paragraph.add_run(latex_to_text(payload)), size=size, bold=bold, color=_MATH)


def _para(document: Document, text: str, *, size: int = 11, bold: bool = False, color: RGBColor = _INK,
          italic: bool = False, space_after: int = 4, align=None, rich: bool = False):
    paragraph = document.add_paragraph()
    if align is not None:
        paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(space_after)
    if rich:
        _write_rich(paragraph, text, size=size, color=color, bold=bold)
    else:
        _style_text(paragraph.add_run(text), size=size, bold=bold, color=color, italic=italic)
    return paragraph


def _bullets(document: Document, items, *, style: str = "List Bullet", size: int = 11) -> None:
    for item in items or ():
        if not str(item or "").strip():
            continue
        paragraph = document.add_paragraph(style=style)
        paragraph.paragraph_format.space_after = Pt(2)
        _write_rich(paragraph, item, size=size)


def _table(document: Document, table) -> None:
    headers = [str(h) for h in (getattr(table, "headers", None) or [])]
    rows = [[str(c) for c in row] for row in (getattr(table, "rows", None) or [])]
    if not headers and not rows:
        return
    columns = max([len(headers)] + [len(r) for r in rows] or [0])
    if columns <= 0:
        return
    docx_table = document.add_table(rows=0, cols=columns)
    docx_table.style = "Table Grid"
    if headers:
        cells = docx_table.add_row().cells
        for index in range(columns):
            text = headers[index] if index < len(headers) else ""
            _write_rich(cells[index].paragraphs[0], text, size=10, bold=True)
    for row in rows:
        cells = docx_table.add_row().cells
        for index in range(columns):
            text = row[index] if index < len(row) else ""
            _write_rich(cells[index].paragraphs[0], text, size=10)
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def _section_body(document: Document, section) -> None:
    _bullets(document, getattr(section, "points", None))

    table = getattr(section, "table", None)
    if table is not None:
        _table(document, table)

    flow = [plain_text(step) for step in (getattr(section, "flow", None) or []) if str(step).strip()]
    if flow:
        _para(document, "Flow:  " + "  →  ".join(flow), size=10, color=_MUTED)

    compare = getattr(section, "compare", None)
    if compare is not None:
        comparison = document.add_table(rows=1, cols=2)
        comparison.style = "Table Grid"
        left, right = comparison.rows[0].cells
        _write_rich(left.paragraphs[0], getattr(compare, "left_title", "") or "", size=10, bold=True)
        _write_rich(right.paragraphs[0], getattr(compare, "right_title", "") or "", size=10, bold=True)
        body = comparison.add_row().cells
        for cell, points in ((body[0], getattr(compare, "left_points", None)), (body[1], getattr(compare, "right_points", None))):
            for point in points or ():
                paragraph = cell.add_paragraph()
                _style_text(paragraph.add_run("• "), size=10)
                _write_rich(paragraph, point, size=10)
        document.add_paragraph().paragraph_format.space_after = Pt(2)

    where = getattr(section, "where", None) or []
    if where:
        _para(document, "Where:", size=10, bold=True, color=_MUTED, space_after=2)
        for item in where:
            symbol = plain_text(getattr(item, "symbol", "") or "")
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.space_after = Pt(1)
            _style_text(paragraph.add_run(f"{symbol} = "), size=10, color=_MATH)
            _write_rich(paragraph, getattr(item, "meaning", "") or "", size=10)

    key_terms = getattr(section, "key_terms", None) or []
    if key_terms:
        _para(document, "Key terms:  " + ", ".join(plain_text(term) for term in key_terms), size=10, color=_MUTED)

    tricks = getattr(section, "tricks", None) or []
    if tricks:
        _para(document, "Tricks / memory hooks:", size=10, bold=True, color=_ACCENT, space_after=2)
        _bullets(document, tricks, size=10)

    notes = getattr(section, "notes", None) or []
    if notes:
        _bullets(document, notes, size=10)

    graph = getattr(section, "graph", None)
    if graph is not None:
        _para(document, "[graph described in the source - check the final PDF]", size=9, color=_MUTED, italic=True)

    diagram = str(getattr(section, "diagram", "") or "").strip()
    if diagram:
        _para(document, f"[diagram: {diagram}]", size=9, color=_MUTED, italic=True)


def build_review_docx(notes, *, signature: str = "", source_name: str = "", logo_path: Path | None = None) -> bytes:
    """Render ``ShortNotes`` into a reviewer-editable .docx."""
    document = Document()

    base = document.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(11)

    section = document.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)

    logo = logo_path or LOGO_PATH
    if logo.is_file():
        _add_watermark(document, logo)
        _add_header_logo(document, logo, signature)

    title = plain_text(getattr(notes, "title", "") or "").strip() or "Short Notes"
    _para(document, title, size=20, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)

    meta = [part for part in (
        str(getattr(notes, "subject", "") or "").strip(),
        str(getattr(notes, "lecture_no", "") or "").strip(),
        str(getattr(notes, "audience", "") or "").strip(),
    ) if part]
    if meta:
        _para(document, "  ·  ".join(meta), size=10, color=_MUTED, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    if signature:
        _para(document, signature, size=10, color=_MUTED, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    if source_name:
        _para(document, f"Source: {source_name}", size=9, color=_MUTED, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _para(document, "Review copy - correct anything wrong here before the final PDF is made.",
          size=9, italic=True, color=_MUTED, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)

    quote = plain_text(getattr(notes, "quote", "") or "").strip()
    if quote:
        _para(document, f"“{quote}”", size=11, italic=True, color=_ACCENT, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)

    for index, section_notes in enumerate(getattr(notes, "sections", None) or [], start=1):
        heading = plain_text(getattr(section_notes, "heading", "") or "").strip() or f"Section {index}"
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(10)
        paragraph.paragraph_format.space_after = Pt(3)
        _style_text(paragraph.add_run(f"{index}. {heading}"), size=13, bold=True, color=_ACCENT)
        _section_body(document, section_notes)

    mindmap = getattr(notes, "mindmap", None)
    if mindmap is not None:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(12)
        _style_text(paragraph.add_run("Mind map"), size=13, bold=True, color=_ACCENT)
        center = plain_text(getattr(mindmap, "center", "") or "").strip()
        if center:
            _para(document, center, size=11, bold=True, space_after=2)
        for branch in getattr(mindmap, "branches", None) or []:
            label = plain_text(getattr(branch, "label", "") or "").strip()
            if label:
                paragraph = document.add_paragraph(style="List Bullet")
                paragraph.paragraph_format.space_after = Pt(1)
                _style_text(paragraph.add_run(label), size=11, bold=True)
            for child in getattr(branch, "children", None) or []:
                paragraph = document.add_paragraph(style="List Bullet 2")
                paragraph.paragraph_format.space_after = Pt(1)
                _write_rich(paragraph, child, size=10)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
