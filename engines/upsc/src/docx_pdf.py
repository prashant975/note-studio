"""Render this app's notes and DMP DOCX files without an Office installation.

The Word builders remain the source of content, styles, images and tables.
ReportLab lays that content out directly, using the same bundled shaped fonts
as the DPP exporter. This supports the app's generated documents, rather than
attempting to implement every feature of an arbitrary Word document.
"""
from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
import re
from threading import RLock

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as WordTable
from docx.text.paragraph import Paragraph as WordParagraph
from docx.text.run import Run
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.textobject import PDFTextObject
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, LongTable, PageBreak, PageTemplate,
    Paragraph as ReportParagraph, Spacer, TableStyle,
)

from .notes_to_dpp import pdf_export as fonts

_LOCK = RLock()
_HAND = "NotesComingSoon"
_HAND_BOLD = _HAND + "-Bold"
_KALAM = "NotesKalam"


class _HandwrittenTextObject(PDFTextObject):
    """Emulate Word's bold for the bundled single-weight handwriting font.

    Fill and stroke the same glyph once: its width and Unicode mapping stay
    unchanged, unlike overprinting a second copy of the text.
    """

    def _weight(self, name, size):
        self._synthetic_bold = name == _HAND_BOLD
        self.setTextRenderMode(2 if self._synthetic_bold else 0)
        if self._synthetic_bold:
            self._code.append(f"{size * 0.018:.4f} w")
            self.setStrokeColor(getattr(self, "_fillColorObj", self._canvas._fillColorObj))

    def _setFont(self, name, size):
        super()._setFont(name, size)
        self._weight(name, size)

    def setFont(self, name, size, leading=None):
        super().setFont(name, size, leading)
        self._weight(name, size)

    def setFillColor(self, color, alpha=None):
        super().setFillColor(color, alpha)
        if getattr(self, "_synthetic_bold", False):
            self.setStrokeColor(color, alpha)


class Paragraph(ReportParagraph):
    """Preserve Unicode with an invisible text layer over shaped Devanagari.

    The visible conjunct glyphs carry private font codes. A source-text layer
    keeps copying and searching independent of a reader's handling of those
    glyphs. Each split carries its own words at the displayed line positions.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._actual_text = "".join(
            "\n" if getattr(frag, "lineBreak", False) else getattr(frag, "text", "")
            for frag in self.frags)

    def split(self, availWidth, availHeight):
        pieces = super().split(availWidth, availHeight)
        if len(pieces) > 1:
            # Shaping changes glyph counts, but preserves word boundaries. Carry
            # only each split's words, so a table continued on the next page does
            # not copy the whole paragraph twice.
            tokens = re.findall(r"\S+\s*", self._actual_text)
            offset = 0
            for index, piece in enumerate(pieces):
                if hasattr(piece, "blPara"):
                    # Simple split fragments hold .words instead of .text, so
                    # getPlainText() is empty for them. Count the laid-out words.
                    count = sum(len(line[1]) if isinstance(line, tuple) else
                                len("".join(getattr(f, "text", "") for f in line.words).split())
                                for line in piece.blPara.lines)
                else:
                    count = len(piece.getPlainText().split())
                end = len(tokens) if index == len(pieces) - 1 else offset + count
                piece._actual_text = "".join(tokens[offset:end])
                offset = end
        return pieces

    def beginText(self, x, y):
        # Only visible paragraph text uses synthetic weight; the searchable
        # Devanagari carrier below retains its invisible rendering mode.
        return _HandwrittenTextObject(self.canv, x, y)

    def draw(self):
        self.canv.saveState()
        try:
            self._draw_text()
        finally:
            self.canv.restoreState()

    def _draw_text(self):
        actual = self._actual_text
        if not re.search(r"[\u0900-\u097f]", actual):
            return super().draw()
        # Empty ActualText suppresses the private glyph codes in extraction.
        # A single long ActualText replacement can duplicate tail glyphs in some
        # readers, so emit ordinary unshaped Unicode text one line at a time.
        self.canv._code.append("/Span << /ActualText () >> BDC")
        try:
            super().draw()
        finally:
            self.canv._code.append("EMC")
        words, offset = actual.split(), 0
        size = getattr(self.style, "sourceFontSize", self.style.fontSize)
        for index, line in enumerate(self.blPara.lines):
            count = (len(line[1]) if isinstance(line, tuple) else
                     len("".join(getattr(f, "text", "") for f in line.words).split()))
            end = len(words) if index == len(self.blPara.lines) - 1 else offset + count
            text = " ".join(words[offset:end])
            offset = end
            if not text:
                continue
            routes = [("NotesSearchDevanagari" if name == fonts._FONT_REGULAR else
                       name or fonts._body_fonts()[0], run) for name, run in fonts._runs(text)]
            natural = sum(pdfmetrics.stringWidth(run, name, size) for name, run in routes)
            first_indent = self.style.firstLineIndent if index == 0 else 0
            available = self.width - self.style.leftIndent - self.style.rightIndent - first_indent
            spare = line[0] if isinstance(line, tuple) else line.extraSpace
            target = available if self.style.alignment == 4 and index < len(self.blPara.lines) - 1 else available - spare
            target = min(available, max(1, target))
            x = self.style.leftIndent + first_indent
            if self.style.alignment == 1:
                x += (available - target) / 2
            elif self.style.alignment == 2:
                x += available - target
            y = self.height - size - index * self.style.leading
            self.canv.saveState()
            carrier = self.canv.beginText(x, y)
            carrier.setTextRenderMode(3)
            carrier.setHorizScale(100 * target / max(1, natural))
            for name, run in routes:
                carrier.setFont(name, size)
                carrier.textOut(run)
            self.canv.drawText(carrier)
            self.canv.restoreState()


def _register_fonts() -> None:
    fonts._ensure_devanagari_fonts()
    fonts._body_fonts()
    if _HAND not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_HAND, str(fonts._find_font("ComingSoon-regular.ttf")), shapable=False))
        pdfmetrics.registerFont(TTFont(_HAND_BOLD, str(fonts._find_font("ComingSoon-regular.ttf")), shapable=False))
        pdfmetrics.registerFontFamily(_HAND, normal=_HAND, bold=_HAND_BOLD,
                                     italic=_HAND, boldItalic=_HAND_BOLD)
        for name, filename in ((_KALAM, "Kalam-Regular.ttf"),
                               (_KALAM + "-Bold", "Kalam-Bold.ttf")):
            pdfmetrics.registerFont(TTFont(name, str(fonts._find_font(filename)), shapable=True))
        pdfmetrics.registerFontFamily(_KALAM, normal=_KALAM, bold=_KALAM + "-Bold",
                                     italic=_KALAM, boldItalic=_KALAM + "-Bold")
        pdfmetrics.registerFont(TTFont("NotesSearchDevanagari",
                                      str(fonts._find_font("NotoSansDevanagari-Regular.ttf")),
                                      shapable=False))
        fonts._register_face("NotesTimes", "NotesTimes-Bold", ("times.ttf", "timesbd.ttf"))


def _inherited(paragraph, attr, *, run=None, font=False, default=None):
    sources = []
    if run is not None:
        sources.append(run.font if font else run)
        if run.style is not None:
            sources.append(run.style.font if font else run.style.paragraph_format)
    if not font:
        sources.append(paragraph.paragraph_format)
    style = paragraph.style
    while style is not None:
        sources.append(style.font if font else style.paragraph_format)
        style = style.base_style
    for source in sources:
        value = getattr(source, attr, None)
        if value is not None:
            return value
    return default


def _pt(value, default=0.0):
    return float(value.pt) if value is not None else default


def _face(name, bold=False, italic=False):
    name = (name or "").casefold()
    if "coming" in name or "comic" in name or "print" in name:
        return _HAND_BOLD if bold else _HAND
    if "kalam" in name:
        return _KALAM + ("-Bold" if bold else "")
    if "times" in name or "serif" in name:
        if "NotesTimes" in pdfmetrics.getRegisteredFontNames():
            return "NotesTimes-Bold" if bold else "NotesTimes"
        return "Times-BoldItalic" if bold and italic else (
            "Times-Bold" if bold else "Times-Italic" if italic else "Times-Roman")
    regular, heavy = fonts._body_fonts()
    return heavy if bold else regular


def _text_markup(text, face, bold=False, devanagari_face=None):
    groups = []

    def append(name, chunk):
        if groups and groups[-1][0] == name:
            groups[-1] = (name, groups[-1][1] + chunk)
        else:
            groups.append((name, chunk))

    coverage = {}

    def covers(name, text):
        if name not in coverage:
            font = pdfmetrics.getFont(name)
            coverage[name] = (fonts._ttf_covers(name) if getattr(font.face, "charToGlyph", None)
                              else fonts._encodes("cp1252"))
        return all(coverage[name](c) for c in text if not c.isspace())

    devanagari = (_KALAM + ("-Bold" if bold else "")
                 if (devanagari_face or face).startswith(_KALAM)
                 else fonts._FONT_BOLD if bold else fonts._FONT_REGULAR)
    # Shape each word in a single face. ReportLab's shaper assumes that; mixing
    # fonts inside "संसद(Parliament)" or "CO₂" can misroute glyph IDs or raise.
    chunks = []
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        preferred = devanagari if re.search(r"[\u0900-\u097f]", token) else face
        if covers(preferred, token):
            chunks.append((preferred, token))
            continue
        fallback = next((name for name in (fonts._body_fonts()[bool(bold)], devanagari)
                         if covers(name, token)), None)
        if fallback:
            chunks.append((fallback, token))
        else:
            chunks.extend((name or face, chunk) for name, chunk in fonts._runs(token))
    for fallback, chunk in chunks:
        chosen = fonts._BOLD_OF.get(fallback, fallback) if bold else fallback
        append(chosen, chunk)
    return "".join(f'<font name="{name}">{escape(chunk)}</font>' for name, chunk in groups
                   ).replace("\n", "<br/>").replace("\t", " &#160; ")


def _complex_face(paragraph, run, bold):
    properties = [run._r.rPr]
    style = paragraph.style
    while style is not None:
        properties.append(style.element.find(qn("w:rPr")))
        style = style.base_style
    for prop in properties:
        names = prop.find(qn("w:rFonts")) if prop is not None else None
        name = names.get(qn("w:cs")) if names is not None else None
        if name:
            return _face(name, bold)
    return None


def _color(paragraph, run=None):
    for source in [run.font if run is not None else None, paragraph.style.font]:
        if source is not None and source.color.rgb is not None:
            return str(source.color.rgb)
    style = paragraph.style.base_style
    while style is not None:
        if style.font.color.rgb is not None:
            return str(style.font.color.rgb)
        style = style.base_style
    return "202124"


def _style(paragraph, *, furniture=False):
    size = _pt(_inherited(paragraph, "size", font=True), 11)
    spacing = _inherited(paragraph, "line_spacing")
    leading = (_pt(spacing) if hasattr(spacing, "pt") else
               size * (float(spacing) if spacing else 1.22))
    leading = max(size * 1.15, leading)
    alignment = int(paragraph.alignment if paragraph.alignment is not None else
                    _inherited(paragraph, "alignment", default=0))
    name = _inherited(paragraph, "name", font=True, default="Calibri")
    style = ParagraphStyle(
        "DOCX", fontName=_face(name), fontSize=size, leading=leading,
        textColor=HexColor("#" + _color(paragraph)), alignment={0: 0, 1: 1, 2: 2, 3: 4}.get(alignment, 0),
        spaceBefore=_pt(_inherited(paragraph, "space_before")),
        spaceAfter=_pt(_inherited(paragraph, "space_after")),
        leftIndent=_pt(_inherited(paragraph, "left_indent")),
        rightIndent=_pt(_inherited(paragraph, "right_indent")),
        firstLineIndent=_pt(_inherited(paragraph, "first_line_indent")),
        keepWithNext=bool(_inherited(paragraph, "keep_with_next", default=False)),
        allowWidows=0, allowOrphans=0, splitLongWords=1, shaping=True,
    )
    if furniture:
        style.spaceBefore = style.spaceAfter = 0
        style.leftIndent = style.rightIndent = style.firstLineIndent = 0
    return style


def _numbering(paragraph, counters):
    ppr = paragraph._p.pPr
    numpr = ppr.find(qn("w:numPr")) if ppr is not None else None
    style = paragraph.style
    while numpr is None and style is not None:
        ppr = style.element.find(qn("w:pPr"))
        numpr = ppr.find(qn("w:numPr")) if ppr is not None else None
        style = style.base_style
    if numpr is None:
        return ""
    num = numpr.find(qn("w:numId"))
    lvl = numpr.find(qn("w:ilvl"))
    num_id = num.get(qn("w:val")) if num is not None else "0"
    level = lvl.get(qn("w:val")) if lvl is not None else "0"
    if num_id == "0":
        return ""
    root = paragraph.part.numbering_part.element
    instance = next((n for n in root.findall(qn("w:num"))
                     if n.get(qn("w:numId")) == num_id), None)
    if instance is None:
        return "\u2022"
    abstract_id = instance.find(qn("w:abstractNumId")).get(qn("w:val"))
    abstract = next((n for n in root.findall(qn("w:abstractNum"))
                     if n.get(qn("w:abstractNumId")) == abstract_id), None)
    definition = next((n for n in abstract.findall(qn("w:lvl"))
                       if n.get(qn("w:ilvl")) == level), None) if abstract is not None else None
    if definition is None:
        return "\u2022"
    fmt, text = definition.find(qn("w:numFmt")), definition.find(qn("w:lvlText"))
    label = text.get(qn("w:val"), "\u2022") if text is not None else "\u2022"
    if fmt is not None and fmt.get(qn("w:val")) == "bullet":
        return label
    key = (num_id, level)
    start = definition.find(qn("w:start"))
    counters[key] = counters.get(key, int(start.get(qn("w:val"), "1")) - 1
                                 if start is not None else 0) + 1
    return label.replace("%" + str(int(level) + 1), str(counters[key]))


def _images(element, part, width, height, *, skip_background=False):
    for drawing in element.iter(qn("w:drawing")):
        if skip_background and drawing.xpath('./wp:anchor[@behindDoc="1"]'):
            continue
        blips = list(drawing.iter(qn("a:blip")))
        extents = list(drawing.iter(qn("wp:extent")))
        if not blips or not extents:
            continue
        image_part = part.related_parts[blips[0].get(qn("r:embed"))]
        cx, cy = (float(extents[0].get(axis)) / 12700 for axis in ("cx", "cy"))
        scale = min(1, width / max(cx, 1), height / max(cy, 1))
        yield Image(BytesIO(image_part.blob), width=cx * scale, height=cy * scale)


def _paragraph(paragraph, width, height, counters, *, furniture=False, page=1):
    style = _style(paragraph, furniture=furniture)
    parts, result = [], []
    label = "" if furniture else _numbering(paragraph, counters)
    page_field = "PAGE" in " ".join(paragraph._p.xpath(".//w:instrText/text()"))
    page_field = page_field or any("PAGE" in n.get(qn("w:instr"), "")
                                  for n in paragraph._p.iter(qn("w:fldSimple")))
    field_cached = False
    missing_page_cache = page_field and not any(
        (node.text or "").strip().isdigit() for node in paragraph._p.iter(qn("w:t")))

    def flush():
        nonlocal parts, label
        if parts:
            bullet = label or None
            if label:
                routes = fonts._runs(label)
                style.bulletFontName = (routes[0][0] if routes else None) or fonts._body_fonts()[0]
                style.bulletFontSize = style.fontSize
            if label and style.firstLineIndent < 0:
                style.bulletIndent = max(0, style.leftIndent + style.firstLineIndent)
                style.firstLineIndent = 0
            result.append(Paragraph("".join(parts), style, bulletText=bullet))
            parts = []
            label = ""

    for element in paragraph._p.iter(qn("w:r")):
        # Floating notes-box text is drawn separately in the repeating header.
        if element.xpath(".//w:txbxContent|ancestor::w:txbxContent"):
            continue
        run = Run(element, paragraph)
        size = _pt(_inherited(paragraph, "size", run=run, font=True), style.fontSize)
        style.leading = max(style.leading, size * 1.2)
        style.sourceFontSize = max(getattr(style, "sourceFontSize", style.fontSize), size)
        bold = bool(_inherited(paragraph, "bold", run=run, font=True, default=False))
        italic = bool(_inherited(paragraph, "italic", run=run, font=True, default=False))
        name = _inherited(paragraph, "name", run=run, font=True, default="Calibri")
        face = _face(name, bold, italic)
        for child in element:
            if child.tag == qn("w:t"):
                text = child.text or ""
                if page_field and text.strip().isdigit() and not field_cached:
                    text, field_cached = str(page), True
                markup = _text_markup(text, face, bold, _complex_face(paragraph, run, bold))
                markup = f'<font size="{size}" color="#{_color(paragraph, run)}">{markup}</font>'
                if run.underline:
                    markup = f"<u>{markup}</u>"
                if run.font.superscript:
                    markup = f"<super>{markup}</super>"
                elif run.font.subscript:
                    markup = f"<sub>{markup}</sub>"
                parent = element.getparent()
                if parent.tag == qn("w:hyperlink"):
                    rid = parent.get(qn("r:id"))
                    if rid and rid in paragraph.part.rels:
                        target = escape(paragraph.part.rels[rid].target_ref, quote=True)
                        markup = f'<link href="{target}">{markup}</link>'
                parts.append(markup)
            elif child.tag == qn("w:tab"):
                parts.append(" &#160; ")
            elif child.tag == qn("w:instrText") and missing_page_cache and "PAGE" in (child.text or ""):
                parts.append(_text_markup(str(page), face))
            elif child.tag in (qn("w:br"), qn("w:cr")):
                if child.get(qn("w:type")) == "page":
                    flush()
                    result.append(PageBreak())
                else:
                    parts.append("<br/>")
            elif child.tag == qn("w:drawing"):
                flush()
                for image in _images(child, paragraph.part, width, height - 10, skip_background=furniture):
                    image.hAlign = ("LEFT", "CENTER", "RIGHT")[min(style.alignment, 2)]
                    image.spaceBefore = style.spaceBefore
                    image.keepWithNext = style.keepWithNext
                    gap = Spacer(1, max(2, style.spaceAfter))
                    gap.keepWithNext = style.keepWithNext
                    result.extend([image, gap])
    flush()
    if not result:
        result.append(Spacer(1, min(height / 4, style.leading + style.spaceAfter)))
    if _inherited(paragraph, "page_break_before", default=False) and not furniture:
        result.insert(0, PageBreak())
    if (_inherited(paragraph, "keep_together", default=False) and not furniture
            and not any(isinstance(item, PageBreak) for item in result)):
        return [KeepTogether(result)]
    return result


def _table(table, width, height, counters):
    cols = len(table.columns)
    if not cols or not table.rows:
        return Spacer(1, 1)
    widths = [_pt(col.width, width / cols) for col in table.columns]
    scale = min(1.0, width / max(sum(widths), 1))
    widths = [max(1, value * scale) for value in widths]
    data, positions = [], {}
    commands = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    spans, heights, repeats = {}, [], 0
    for row_index, row in enumerate(table.rows):
        values = []
        heights.append(_pt(row.height))
        trpr = row._tr.trPr
        if row_index == repeats and trpr is not None and trpr.find(qn("w:tblHeader")) is not None:
            repeats += 1
        for col_index, cell in enumerate(row.cells):
            identity = cell._tc
            if identity in positions:
                start = positions[identity]
                spans[start] = (col_index, row_index)
                values.append("")
                continue
            positions[identity] = (col_index, row_index)
            span_width = sum(widths[i] for i, other in enumerate(row.cells)
                             if other._tc is identity)
            contents = []
            for child in cell._tc:
                if child.tag == qn("w:p"):
                    contents.extend(_paragraph(WordParagraph(child, cell), max(1, span_width - 12),
                                               height - 12, counters))
                elif child.tag == qn("w:tbl"):
                    contents.append(_table(WordTable(child, cell), span_width - 12, height - 12, counters))
            values.append(contents or "")
            tcpr = cell._tc.tcPr
            margins = tcpr.find(qn("w:tcMar")) if tcpr is not None else None
            if margins is None:
                margins = table._tbl.tblPr.find(qn("w:tblCellMar"))
            if margins is not None:
                for edge in ("left", "right", "top", "bottom"):
                    node = margins.find(qn("w:" + edge))
                    if node is not None:
                        commands.append((edge.upper() + "PADDING", (col_index, row_index),
                                         (col_index, row_index), float(node.get(qn("w:w"), "0")) / 20))
            shading = tcpr.find(qn("w:shd")) if tcpr is not None else None
            fill = shading.get(qn("w:fill")) if shading is not None else None
            if fill and fill not in ("auto", "none"):
                commands.append(("BACKGROUND", (col_index, row_index), (col_index, row_index), HexColor("#" + fill)))
        data.append(values)
    for start, end in spans.items():
        commands.append(("SPAN", start, end))
    borders = table._tbl.tblPr.find(qn("w:tblBorders"))
    if borders is None and table.style is not None:
        style_properties = table.style.element.find(qn("w:tblPr"))
        if style_properties is not None:
            borders = style_properties.find(qn("w:tblBorders"))
    if borders is not None:
        for name, command in (("top", "LINEABOVE"), ("bottom", "LINEBELOW"),
                              ("left", "LINEBEFORE"), ("right", "LINEAFTER"),
                              ("insideH", "INNERGRID"), ("insideV", "INNERGRID")):
            border = borders.find(qn("w:" + name))
            if border is not None and border.get(qn("w:val")) not in ("nil", "none"):
                color = border.get(qn("w:color"), "808080")
                color = "808080" if color == "auto" else color
                size = float(border.get(qn("w:sz"), "4")) / 8
                start, end = (0, 0), (-1, -1)
                if name == "top": end = (-1, 0)
                elif name == "bottom": start = (0, -1)
                elif name == "left": end = (0, -1)
                elif name == "right": start = (-1, 0)
                commands.append((command, start, end, size, HexColor("#" + color)))
    return LongTable(data, colWidths=widths, minRowHeights=heights,
                     repeatRows=min(repeats, len(data) - 1), splitInRow=1,
                     hAlign={0: "LEFT", 1: "CENTER", 2: "RIGHT"}.get(table.alignment, "CENTER"),
                     spaceAfter=4, style=TableStyle(commands))


def _draw_furniture(canvas, doc, section, page_width, page_height, notes_box):
    canvas.saveState()
    width = page_width - _pt(section.left_margin) - _pt(section.right_margin)
    for drawing in section.header._element.iter(qn("w:drawing")):
        if drawing.xpath('./wp:anchor[@behindDoc="1"]'):
            for image in _images(drawing, section.header.part, page_width, page_height):
                iw, ih = image.wrap(page_width, page_height)
                image.drawOn(canvas, (page_width - iw) / 2, (page_height - ih) / 2)
    if notes_box:
        from . import config
        x, y = config.DCN_NOTES_BOX_LEFT_IN * 72, page_height - (
            config.DCN_NOTES_BOX_TOP_IN + config.DCN_NOTES_BOX_HEIGHT_IN) * 72
        w, h = config.DCN_NOTES_BOX_WIDTH_IN * 72, config.DCN_NOTES_BOX_HEIGHT_IN * 72
        canvas.setStrokeColor(HexColor("#" + config.DCN_COLOR_NOTES_BOX))
        canvas.setLineWidth(.75)
        canvas.rect(x, y, w, h)
        caption = Paragraph("Space for Notes", ParagraphStyle(
            "NotesBox", fontName=_HAND, fontSize=config.DCN_SIZE_NOTES_CAPTION_PT,
            leading=18, alignment=1, textColor=HexColor("#" + config.DCN_COLOR_NOTES_BOX)))
        _, ch = caption.wrap(w - 8, h)
        caption.drawOn(canvas, x + 4, y + h - ch - 5)
    for paragraph in section.header.paragraphs:
        indent = max(0, _pt(_inherited(paragraph, "left_indent")))
        header_width = max(1, width - indent)
        elements = _paragraph(paragraph, header_width, _pt(section.top_margin), {}, furniture=True, page=doc.page)
        y = page_height - _pt(section.header_distance)
        for element in elements:
            ew, eh = element.wrap(header_width, _pt(section.top_margin))
            align = _style(paragraph).alignment
            x = _pt(section.left_margin) + indent + (header_width - ew) * (.5 if align == 1 else 1 if align == 2 else 0)
            element.drawOn(canvas, x, y - eh)
            y -= eh
        if paragraph._p.xpath("./w:pPr/w:pBdr/w:bottom"):
            canvas.setStrokeColor(HexColor("#202124"))
            canvas.line(_pt(section.left_margin), y - 3, _pt(section.left_margin) + width, y - 3)
    footer = section.footer
    if footer.tables:
        cells = footer.tables[0].rows[0].cells
        color_node = cells[0]._tc.xpath("./w:tcPr/w:shd")
        fill = color_node[0].get(qn("w:fill"), "CFE2F3") if color_node else "CFE2F3"
        canvas.setFillColor(HexColor("#" + fill))
        canvas.rect(0, _pt(section.footer_distance), page_width, 22, stroke=0, fill=1)
        for index, cell in enumerate(cells):
            text = str(doc.page) if index == 1 and cell._tc.xpath(".//w:instrText") else cell.text
            st = _style(cell.paragraphs[0], furniture=True)
            first_run = next(iter(cell.paragraphs[0].runs), None)
            st.fontName = _face(_inherited(cell.paragraphs[0], "name", run=first_run, font=True))
            st.alignment = 0 if index == 0 else 1 if index == 1 else 2
            st.fontSize = min(12, st.fontSize)
            box = Paragraph(_text_markup(text, st.fontName), st)
            fw = page_width * .38
            _, fh = box.wrap(fw, 40)
            x = _pt(section.left_margin) if index == 0 else page_width / 2 - fw / 2 if index == 1 else page_width - 54 - fw
            box.drawOn(canvas, x, _pt(section.footer_distance) + (22 - fh) / 2)
    else:
        plain_paragraphs = [p for p in footer.paragraphs
                            if p.text.strip() or p._p.xpath(".//w:instrText")]
        if plain_paragraphs and not any(
            "\t" in p.text or p._p.xpath("./w:pPr/w:shd") for p in plain_paragraphs
        ):
            elements = [element for p in plain_paragraphs for element in _paragraph(
                p, width, _pt(section.bottom_margin), {}, furniture=True, page=doc.page)]
            measured = [(element, element.wrap(width, 100)[1]) for element in elements]
            y = _pt(section.footer_distance) + sum(h for _, h in measured)
            for element, eh in measured:
                y -= eh
                element.drawOn(canvas, _pt(section.left_margin), y)
            canvas.restoreState()
            return
        for p in footer.paragraphs:
            if not p.text.strip():
                continue
            st = _style(p, furniture=True)
            first_run = next(iter(p.runs), None)
            st.fontName = _face(_inherited(p, "name", run=first_run, font=True))
            labels = p.text.split("\t")
            entries = [(labels[0], 0)]
            if len(labels) > 1:
                entries.append((labels[-1], 2))
            if len(labels) == 3:
                middle = str(doc.page) if p._p.xpath(".//w:instrText") else labels[1]
                entries.append((middle, 1))
            fill = p._p.xpath("./w:pPr/w:shd")
            if fill:
                canvas.setFillColor(HexColor("#" + fill[0].get(qn("w:fill"), "CFE2F3")))
                canvas.rect(_pt(section.left_margin), _pt(section.footer_distance), width, 22, stroke=0, fill=1)
            for text, align in entries:
                if not text:
                    continue
                st.alignment = align
                box = Paragraph(_text_markup(text, st.fontName), st)
                _, fh = box.wrap(width, 40)
                box.drawOn(canvas, _pt(section.left_margin), _pt(section.footer_distance) + (22 - fh) / 2)
    canvas.restoreState()


def render_docx(source: Path, destination: Path) -> None:
    """Lay out a generated document, including repeating headers and footers."""
    # Registration and ReportLab's dynamic glyph subsets are shared state.
    # Serialize this renderer across sessions to keep fonts reliable.
    with _LOCK:
        _register_fonts()
        document = Document(str(source))
        section = document.sections[0]
        pw, ph = _pt(section.page_width, 612), _pt(section.page_height, 792)
        left, right = _pt(section.left_margin, 36), _pt(section.right_margin, 36)
        top, bottom = _pt(section.top_margin, 72), _pt(section.bottom_margin, 36)
        width, height = pw - left - right, ph - top - bottom
        if min(width, height) <= 0:
            raise ValueError("The document margins leave no printable area.")
        notes_box = "Space for Notes" in section.header._element.xml

        def build(target, *, compact=False, balance_tail=False):
            doc = BaseDocTemplate(target, pagesize=(pw, ph),
                                  leftMargin=left, rightMargin=right, topMargin=top,
                                  bottomMargin=bottom, title=document.core_properties.title or source.stem)
            frame = Frame(left, bottom, width, height, leftPadding=0, rightPadding=0,
                          topPadding=0, bottomPadding=0)
            fills = []
            doc.addPageTemplates(PageTemplate(
                id="body", frames=[frame],
                onPage=lambda canvas, current: _draw_furniture(canvas, current, section, pw, ph, notes_box),
                onPageEnd=lambda canvas, current: fills.append(height - max(0, frame._y - frame._y1p))))
            story, counters = [], {}
            explicit_breaks = False
            for child in document.element.body:
                if child.tag == qn("w:p"):
                    paragraph = WordParagraph(child, document)
                    elements = _paragraph(paragraph, width, height, counters)
                    explicit_breaks |= any(isinstance(item, PageBreak) for item in elements)
                    if compact and paragraph.style.name in (
                            "UPSC_DCN_Body", "UPSC_DCN_Bullet_Level_1",
                            "UPSC_DCN_Bullet_Level_2", "UPSC_DCN_Bullet_Level_3"):
                        for element in elements:
                            if (isinstance(element, Paragraph)
                                    and not re.search(r"[\u0900-\u097f]", element._actual_text)):
                                # A bounded change to Latin leading only. Keep
                                # font sizes, content, images and Hindi leading.
                                size = getattr(element.style, "sourceFontSize", element.style.fontSize)
                                element.style.leading = max(size * 1.15, element.style.leading * 0.96)
                    story.extend(elements)
                elif child.tag == qn("w:tbl"):
                    story.append(_table(WordTable(child, document), width, height, counters))
            if balance_tail:
                # When tightening cannot remove a tiny final page, move enough
                # preceding text with it to retain context. Limit the group to
                # half a page; images and tables keep their normal rules.
                tail_height = 0
                for count, element in enumerate(reversed(story), 1):
                    if not isinstance(element, (Paragraph, Spacer)):
                        break
                    _, element_height = element.wrap(width, height)
                    element_height += element.getSpaceBefore() + element.getSpaceAfter()
                    if tail_height + element_height > height * 0.5:
                        break
                    tail_height += element_height
                    if tail_height >= height * 0.25:
                        story[-count:] = [KeepTogether(story[-count:])]
                        break
            doc.build(story or [Spacer(1, 1)])
            return fills, explicit_breaks

        fills, explicit_breaks = build(str(destination))
        # A tiny continuation page can result from rounding and line-height
        # differences between Word and PDF. Retry once with at most 4% tighter
        # Latin leading, and accept it only if it removes the extra page.
        # Explicit user page breaks and Devanagari text are left alone.
        if notes_box and not explicit_breaks and len(fills) > 1 and fills[-1] < height * 0.25:
            candidate = BytesIO()
            compact_fills, _ = build(candidate, compact=True)
            if len(compact_fills) < len(fills):
                destination.write_bytes(candidate.getvalue())
            else:
                balanced = BytesIO()
                balanced_fills, _ = build(balanced, balance_tail=True)
                if len(balanced_fills) <= len(fills) and balanced_fills[-1] >= height * 0.25:
                    destination.write_bytes(balanced.getvalue())
