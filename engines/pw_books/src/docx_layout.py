"""PW-style document furniture: title block, header, footer, and watermark.

Reproduces the Physics Wallah notes layout — subject/chapter title, PW logo in
the header, "Master NCERT with PW Books APP" footer, page numbers, and a faint
PW-logo watermark behind the text on every page.

All sizes/colours/distances come from style_config so the whole note series
looks identical whatever the subject is.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt

from .style_config import (
    CHAR_SPACING_TWIPS,
    FONT_PRIMARY,
    FOOTER_COLOR,
    FOOTER_ICON_HEIGHT_PT,
    FOOTER_SIZE_PT,
    FOOTER_TEXT,
    HEADER_LOGO_HEIGHT_PT,
    LECTURE_TITLE_SIZE,
    PAGE_NUMBER_COLOR,
    PAGE_NUMBER_SIZE_PT,
    SPACE_AFTER_PT,
    SUBJECT_SIZE,
    TITLE_COLOR,
    TITLE_LINE_SPACING,
    WATERMARK_MAX_HEIGHT_IN,
    WATERMARK_WIDTH_IN,
    apply_page_setup,          # re-exported: callers set the page through here
    set_char_spacing,
    set_run_fonts,
    split_font_runs,
)

__all__ = [
    "add_title_block", "add_watermark", "apply_page_setup", "derive_chapter_title",
    "is_kalam_installed", "setup_footer", "setup_header",
]

# Kept as module-level names because older call sites and tests import them.
BODY_FONT = FONT_PRIMARY


def is_kalam_installed() -> bool:
    """Best-effort check for the Kalam handwritten font on Windows/Linux/Mac."""
    import glob
    import os

    dirs = [
        r"C:\Windows\Fonts",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
        "/usr/share/fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]
    for d in dirs:
        if d and os.path.isdir(d):
            if glob.glob(os.path.join(d, "**", "[Kk]alam*.ttf"), recursive=True):
                return True
    return False


def derive_chapter_title(stem: str) -> str:
    """Turn an uploaded file's stem into a readable chapter title (shown on the
    document cover only — the output file name uses the uploaded name as-is).

    'Cell_-_The_Unit_of_Life_02_Class_Notes_1' -> 'Cell - The Unit of Life 02'
    """
    name = stem.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    # Drop a trailing copy index like "(1)" first.
    name = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", name).strip()
    # Then drop trailing boilerplate like "Class Notes" / "Concise Notes"
    # (and anything after it), but keep lecture numbers that are part of the title.
    name = re.sub(r"[\s_-]*\b(class|concise|handwritten|lecture)\b\s+notes\b.*$", "", name, flags=re.I).strip()
    return name.strip(" -_") or stem


def _apply_font(run, *, size, bold=False, italic=False, underline=False, color=None, font=BODY_FONT):
    set_run_fonts(run, font)
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    run.font.underline = underline
    if color is not None:
        run.font.color.rgb = color
    set_char_spacing(run, CHAR_SPACING_TWIPS)


def _add_text(paragraph, text, **kw):
    """Add `text`, moving any glyph Kalam cannot draw to the maths font."""
    for chunk, font in split_font_runs(text):
        _apply_font(paragraph.add_run(chunk), font=font, **kw)


def add_title_block(doc, subject: str, chapter_title: str):
    """Subject + chapter, centered, red bold underlined (PW cover style).

    Subject and chapter are both DYNAMIC — nothing here is hard-coded to one
    subject. Both paragraphs use 0 pt space-after per the reference document;
    one controlled blank line separates the block from the notes below it.
    """
    subj = (subject or "").strip().capitalize()
    added = False
    if subj:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(SPACE_AFTER_PT)
        p.paragraph_format.line_spacing = TITLE_LINE_SPACING
        p.paragraph_format.keep_with_next = True
        _add_text(p, subj, size=SUBJECT_SIZE, bold=True, underline=True, color=TITLE_COLOR)
        added = True
    if chapter_title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(SPACE_AFTER_PT)
        p.paragraph_format.line_spacing = TITLE_LINE_SPACING
        p.paragraph_format.keep_with_next = True
        _add_text(p, chapter_title, size=LECTURE_TITLE_SIZE, bold=True, underline=True, color=TITLE_COLOR)
        added = True
    if added:
        spacer = doc.add_paragraph()
        spacer.paragraph_format.space_before = Pt(0)
        spacer.paragraph_format.space_after = Pt(0)
        spacer.paragraph_format.line_spacing = TITLE_LINE_SPACING
    return added


def _add_page_field(paragraph, *, bracket=True, color=None, size=None):
    size = size or Pt(PAGE_NUMBER_SIZE_PT)
    if bracket:
        _apply_font(paragraph.add_run("["), size=size, color=color)
    run = paragraph.add_run()
    _apply_font(run, size=size, color=color)
    begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = "PAGE"
    end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end")
    run._r.append(begin); run._r.append(instr); run._r.append(end)
    if bracket:
        _apply_font(paragraph.add_run("]"), size=size, color=color)


def setup_header(section, logo_path: Path | None):
    """PW logo in the top-right corner (matches the PW notes layout).

    Lives in the header, so it never moves with the body text and never affects
    the flow. Aspect ratio is preserved (height only is given to Word).
    """
    header = section.header
    header.is_linked_to_previous = False
    p = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    if logo_path and Path(logo_path).exists():
        try:
            p.add_run().add_picture(str(logo_path), height=Pt(HEADER_LOGO_HEIGHT_PT))
        except Exception:
            pass


def setup_footer(section, logo_path: Path | None = None):
    """'Master NCERT with PW Books APP' then a centered [n] page number.

    `logo_path` supplies the small branding icon beside the text. It is a real
    image rather than an emoji character: the book emoji used previously has no
    glyph in any document font and printed as an empty box in the PDF.
    """
    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    if logo_path and Path(logo_path).exists():
        try:
            p.add_run().add_picture(str(logo_path), height=Pt(FOOTER_ICON_HEIGHT_PT))
            _apply_font(p.add_run("  "), size=Pt(FOOTER_SIZE_PT))
        except Exception:
            pass
    _apply_font(p.add_run(FOOTER_TEXT), size=Pt(FOOTER_SIZE_PT), bold=True, color=FOOTER_COLOR)
    p2 = footer.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p2.paragraph_format.space_before = Pt(0)
    p2.paragraph_format.space_after = Pt(0)
    _add_page_field(p2, bracket=True, color=PAGE_NUMBER_COLOR)


def add_watermark(section, watermark_path: Path | None):
    """Insert a faint, centered PW logo BEHIND the text on every page.

    Anchored from the header with behindDoc=1, so it is page furniture rather
    than a body image: it cannot push content around or land inside a paragraph.
    Sized to the reference document's ~4.98 x 5.03 in.
    """
    if not watermark_path or not Path(watermark_path).exists():
        return False
    try:
        from PIL import Image
        iw, ih = Image.open(watermark_path).size
    except Exception:
        iw, ih = 1, 1
    width_emu = int(Inches(WATERMARK_WIDTH_IN))
    height_emu = int(width_emu * (ih / iw)) if iw else width_emu
    max_h = int(Inches(WATERMARK_MAX_HEIGHT_IN))
    if height_emu > max_h:                      # keep the aspect ratio, fit height
        width_emu = int(max_h * (iw / ih)) if ih else width_emu
        height_emu = max_h
    header = section.header
    header.is_linked_to_previous = False
    p = header.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    run = p.add_run()
    try:
        run.add_picture(str(watermark_path), width=Emu(width_emu), height=Emu(height_emu))
    except Exception:
        return False
    drawing = run._r.find(qn("w:drawing"))
    inline = drawing.find(qn("wp:inline")) if drawing is not None else None
    if inline is None:
        return False
    extent = inline.find(qn("wp:extent"))
    docPr = inline.find(qn("wp:docPr"))
    graphic = inline.find(qn("a:graphic"))

    anchor = OxmlElement("wp:anchor")
    for k, v in {"distT": "0", "distB": "0", "distL": "0", "distR": "0", "simplePos": "0",
                 "relativeHeight": "0", "behindDoc": "1", "locked": "0", "layoutInCell": "1",
                 "allowOverlap": "1"}.items():
        anchor.set(k, v)
    sp = OxmlElement("wp:simplePos"); sp.set("x", "0"); sp.set("y", "0"); anchor.append(sp)
    posH = OxmlElement("wp:positionH"); posH.set("relativeFrom", "page")
    alignH = OxmlElement("wp:align"); alignH.text = "center"; posH.append(alignH); anchor.append(posH)
    posV = OxmlElement("wp:positionV"); posV.set("relativeFrom", "page")
    alignV = OxmlElement("wp:align"); alignV.text = "center"; posV.append(alignV); anchor.append(posV)
    if extent is not None:
        anchor.append(extent)
    eff = OxmlElement("wp:effectExtent")
    for a in ("l", "t", "r", "b"):
        eff.set(a, "0")
    anchor.append(eff)
    anchor.append(OxmlElement("wp:wrapNone"))
    if docPr is not None:
        anchor.append(docPr)
    if graphic is not None:
        anchor.append(graphic)
    drawing.remove(inline)
    drawing.append(anchor)
    return True
