"""UPSC_Notes document furniture: title block, header, footer, and watermark.

Handwritten-style layout — a subject/chapter title block, a light "UPSC_Notes"
footer with page numbers, and an optional faint watermark. Uses two handwriting
fonts, picked per script: Coming Soon for English/Latin and Kalam for
Hindi/Devanagari. Both must be installed on the machine to render and to export
a faithful PDF (see missing_note_fonts). No third-party branding.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor

BRAND_NAME = "UPSC_Notes"
TITLE_COLOR = RGBColor(0x1F, 0x38, 0x64)   # deep navy for subject/chapter title
FOOTER_COLOR = RGBColor(0x55, 0x55, 0x55)  # muted grey footer
# Handwriting fonts, one per script. A run's Latin characters are governed by
# w:ascii/w:hAnsi and its Devanagari characters by w:cs (Word and LibreOffice
# both classify Devanagari as a complex script), so setting both on every run
# means a single Hindi note renders its Devanagari in Kalam and the English
# terms embedded in it in Coming Soon — no per-run language decision needed.
LATIN_FONT = "Coming Soon"        # English / Latin text
DEVANAGARI_FONT = "Kalam"         # Hindi / Devanagari text
BODY_FONT = LATIN_FONT            # back-compat alias for the Latin default

# Filename globs used to detect each family on disk (.ttf or .otf).
_FONT_FILE_GLOBS = {
    LATIN_FONT: "[Cc]oming*[Ss]oon*.[to]tf",
    DEVANAGARI_FONT: "[Kk]alam*.[to]tf",
    # The rest of the DCN fallback chain. Without these entries the lookup
    # returns False for fonts that ship with Windows, so the chain skipped
    # straight past them to the last resort.
    "Comic Sans MS": "[Cc]omic*.[to]tf",
    "Segoe Print": "[Ss]egoepr*.[to]tf",
    "Calibri": "[Cc]alibri*.[to]tf",
    "Arial": "[Aa]rial*.[to]tf",
}

CHAR_SPACING_TWIPS = 30  # expanded character spacing = 1.5 pt, suits handwriting


def _font_dirs() -> list[str]:
    import os

    return [
        r"C:\Windows\Fonts",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
        "/usr/share/fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]


def is_font_installed(family: str) -> bool:
    """Best-effort check for a handwriting font on Windows/Linux/Mac.

    The DOCX only *names* the font — the PDF converter (Word/LibreOffice) needs
    it installed locally, otherwise the export silently falls back to another
    font. Callers surface a warning when this returns False.
    """
    import glob
    import os

    pattern = _FONT_FILE_GLOBS.get(family)
    if not pattern:
        return False
    for d in _font_dirs():
        if d and os.path.isdir(d):
            if glob.glob(os.path.join(d, "**", pattern), recursive=True):
                return True
    return False


def missing_note_fonts() -> list[str]:
    """Which of the two note fonts are absent from this machine."""
    return [f for f in (LATIN_FONT, DEVANAGARI_FONT) if not is_font_installed(f)]




def derive_chapter_title(stem: str) -> str:
    """Turn an uploaded file's stem into a readable chapter title.

    'Cell_-_The_Unit_of_Life_02_Class_Notes_1' -> 'Cell - The Unit of Life 02'
    """
    from .title_utils import filename_subject, infer_from_filename

    if filename_subject(stem):
        return infer_from_filename(stem).title
    name = stem.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    # Drop a trailing copy index like "(1)" or " 1" first.
    name = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", name).strip()
    # Then drop trailing boilerplate like "Class Notes" / "Concise Notes"
    # (and anything after it), but keep lecture numbers that are part of the title.
    name = re.sub(r"[\s_-]*\b(class|concise|handwritten|lecture)\b\s+notes\b.*$", "", name, flags=re.I).strip()
    return name.strip(" -_") or stem


def _apply_font(run, *, size, bold=False, italic=False, underline=False, color=None):
    run.font.name = LATIN_FONT
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)
    rfonts.set(qn("w:eastAsia"), LATIN_FONT)
    rfonts.set(qn("w:cs"), DEVANAGARI_FONT)
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    run.font.underline = underline
    if color is not None:
        run.font.color.rgb = color
    sp = rpr.find(qn("w:spacing"))
    if sp is None:
        sp = OxmlElement("w:spacing")
        rpr.append(sp)
    sp.set(qn("w:val"), str(CHAR_SPACING_TWIPS))


def add_title_block(doc, subject: str, chapter_title: str):
    """Subject + chapter, centered navy — a clean typed cover header."""
    from .upsc_subjects import subject_label

    subj = subject_label(subject) if subject else ""
    if subj:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(4)
        _apply_font(p.add_run(subj), size=Pt(22), bold=True, color=TITLE_COLOR)
    if chapter_title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(14)
        _apply_font(p.add_run(chapter_title), size=Pt(18), bold=True, color=TITLE_COLOR)
    # Thin rule under the title block.
    rule = doc.add_paragraph()
    rule.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rule.paragraph_format.space_after = Pt(10)
    _apply_font(rule.add_run("_______________________________"), size=Pt(10), color=TITLE_COLOR)


def _add_page_field(paragraph, *, bracket=True, color=None, size=Pt(11)):
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
    """PW logo in the top-right corner (matches the PW notes layout)."""
    header = section.header
    header.is_linked_to_previous = False
    p = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if logo_path and Path(logo_path).exists():
        try:
            p.add_run().add_picture(str(logo_path), height=Pt(48))
        except Exception:
            pass


def setup_footer(section):
    """A light 'UPSC_Notes' line and a centered page number."""
    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_font(p.add_run(BRAND_NAME), size=Pt(10), bold=True, color=FOOTER_COLOR)
    p2 = footer.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_field(p2, bracket=True, color=RGBColor(0x55, 0x55, 0x55))


def add_watermark(section, watermark_path: Path | None):
    """Insert a faint, centered PW logo behind the text on every page."""
    if not watermark_path or not Path(watermark_path).exists():
        return False
    try:
        from PIL import Image
        iw, ih = Image.open(watermark_path).size
    except Exception:
        iw, ih = 1, 1
    # ~1.5x the previous 0.55 page-width watermark.
    width_emu = int(section.page_width * 0.82)
    height_emu = int(width_emu * (ih / iw)) if iw else width_emu
    header = section.header
    header.is_linked_to_previous = False
    p = header.add_paragraph()
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
