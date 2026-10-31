from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

from .docx_layout import (
    DEVANAGARI_FONT,
    LATIN_FONT,
    add_title_block,
    add_watermark,
    missing_note_fonts,
    setup_footer,
    setup_header,
)
from .dtp_parser import parse_dtp_note
from .image_tools import extract_pw_logo, load_logo_override, smart_crop_image
from .models import SlideData

# Both are handwriting fonts: they render smaller and lighter than a text face,
# so the body/heading sizes are larger than they would be for a typed serif.
# English/Latin runs use LATIN_FONT, Hindi/Devanagari runs use DEVANAGARI_FONT;
# see the note in docx_layout for how one run carries both.
BODY_FONT = LATIN_FONT
BODY_SIZE = Pt(16)
HEADING_SIZE = Pt(18)
DARK_GREY = RGBColor(0x1A, 0x1A, 0x1A)    # body text (near-black)
HEADING_COLOR = RGBColor(0x1F, 0x38, 0x64)  # navy headings
BOLD_COLOR = RGBColor(0x8B, 0x00, 0x00)   # maroon for inline emphasis
BULLET_PREFIXES = ("•", "·", "-", "–")
BULLET_MARKERS = ("•", "◦", "▪", "–")
CHAR_SPACING_TWIPS = 30  # expanded character spacing = 1.5 pt (1 pt = 20 twips)
_FONT_PROFILES: dict[tuple, object] = {}

# Tables carry a lot of columns (the correction log has eight), so they render a
# few points smaller than the body to keep wide tables inside the page margins.
TABLE_SIZE = Pt(11)
TABLE_HEADER_FILL = "E8ECF4"   # pale navy tint behind the header row
FLOW_FILL = "EAF1F8"           # pale blue box for a flowchart step
FLOW_BORDER = "9DB8D2"

# Correction pointer blocks: marker text -> (background fill, marker text colour).
CORRECTION_STYLES: dict[str, tuple[str, RGBColor]] = {
    "faculty point corrected": ("D9EAD3", RGBColor(0x1E, 0x4D, 0x2B)),  # light green
    "faculty point removed": ("F8D7D7", RGBColor(0x8B, 0x00, 0x00)),    # light red
    "needs human review": ("FFF2CC", RGBColor(0x7F, 0x60, 0x00)),       # yellow warning
}
# Labelled fields that belong to a pointer block; used to decide where it ends.
CORRECTION_FIELDS = (
    "original faculty point", "correction", "reason", "confidence",
    "removed point", "suggested replacement", "faculty point",
    "issue", "suggested action",
)


def _set_char_spacing(run, twips: int = CHAR_SPACING_TWIPS):
    rpr = run._element.get_or_add_rPr()
    sp = rpr.find(qn("w:spacing"))
    if sp is None:
        sp = OxmlElement("w:spacing")
        rpr.append(sp)
    sp.set(qn("w:val"), str(twips))


def _set_no_same_style_spacing(p):
    ppr = p._element.get_or_add_pPr()
    if ppr.find(qn("w:contextualSpacing")) is None:
        ppr.append(OxmlElement("w:contextualSpacing"))


def _set_run_font(run, *, size=BODY_SIZE, bold=False, italic=False, underline=False, color=DARK_GREY):
    # Reports contain thousands of fresh runs with the same few font settings.
    # Clone the completed properties instead of repeating every XML lookup.
    # Existing run properties still use the setters to retain unrelated fields.
    fresh = run._element.rPr is None
    key = (int(size), bold, italic, underline, str(color), LATIN_FONT, DEVANAGARI_FONT)
    profile = _FONT_PROFILES.get(key) if fresh else None
    if profile is not None:
        run._element.insert(0, deepcopy(profile))
        return
    run.font.name = LATIN_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), LATIN_FONT)
    run._element.rPr.rFonts.set(qn("w:cs"), DEVANAGARI_FONT)
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    run.font.underline = underline
    run.font.color.rgb = color
    _set_char_spacing(run)
    if fresh:
        if len(_FONT_PROFILES) >= 128:
            _FONT_PROFILES.clear()
        _FONT_PROFILES.setdefault(key, deepcopy(run._element.rPr))


def _set_paragraph_base(p, *, bullet: bool = False, indent_level: int = 0,
                        preserve_style: bool = False):
    if not preserve_style:
        try:
            p.style = "Body Text"
        except Exception:
            pass
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.2
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _set_no_same_style_spacing(p)
    if bullet:
        # Each nesting level shifts the bullet right by ~0.7 cm while keeping the
        # hanging indent so wrapped lines align under the text, not the marker.
        base = 0.6 + 0.7 * max(0, indent_level)
        p.paragraph_format.left_indent = Cm(base)
        p.paragraph_format.first_line_indent = Cm(-0.6)
    else:
        p.paragraph_format.left_indent = Cm(0)
        p.paragraph_format.first_line_indent = Cm(0)


# Order matters: try ***bold-italic*** before **bold** before *italic* so the
# alternation consumes the longest marker first at each position.
_MD_RE = re.compile(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*.+?\*)")


def _add_markdown_runs(p, text: str, *, size=BODY_SIZE, base_color=DARK_GREY, default_bold: bool = False, underline: bool = False, highlight=None):
    for part in _MD_RE.split(text):
        if part == "":
            continue
        bold = default_bold
        italic = False
        clean = part
        if part.startswith("***") and part.endswith("***") and len(part) >= 6:
            bold = italic = True
            clean = part[3:-3]
        elif part.startswith("**") and part.endswith("**") and len(part) >= 4:
            bold = True
            clean = part[2:-2]
        elif part.startswith("*") and part.endswith("*") and len(part) >= 2:
            italic = True
            clean = part[1:-1]
        run = p.add_run(clean)
        _set_run_font(run, size=size, bold=bold, italic=italic, underline=underline, color=BOLD_COLOR if bold and not default_bold else base_color)
        if highlight is not None:
            run.font.highlight_color = highlight


def _set_shading(element, fill: str):
    """Paint a solid background on a paragraph or a table cell.

    python-docx has no shading API, so the w:shd element is added by hand to the
    pPr/tcPr of the element that was passed in.
    """
    pr = element.get_or_add_pPr() if hasattr(element, "get_or_add_pPr") else element.get_or_add_tcPr()
    for old in pr.findall(qn("w:shd")):
        pr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def _set_paragraph_border(p, *, color: str, sides=("left",), size: int = 12):
    """Draw a thin rule on the given sides of a paragraph (a box or a side bar)."""
    ppr = p._element.get_or_add_pPr()
    bdr = ppr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        ppr.append(bdr)
    for side in sides:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "4")
        el.set(qn("w:color"), color)
        bdr.append(el)


def _is_dtp(line: str) -> bool:
    return "note to dtp" in line.lower()


def _is_bullet(line: str) -> bool:
    s = line.strip()
    if s.startswith(BULLET_PREFIXES):
        return True
    # Markdown-style "* item" bullet (a single asterisk + space), NOT "**bold**".
    if re.match(r"^\*\s+\S", s):
        return True
    return bool(re.match(r"^\s*\d+[.)]\s+", line))


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or _is_bullet(s) or _is_dtp(s):
        return False
    if len(s) > 140:
        return False
    if s.startswith("#"):
        return True
    if s.endswith(".") or s.endswith("।"):
        return False
    return True


def _add_heading(doc: Document, line: str):
    # Retain the handwritten visual treatment below, but give Word a real H1
    # paragraph so navigation, outline view and accessibility tools recognise
    # it as a heading instead of merely enlarged body text.
    p = doc.add_paragraph(style="Heading 1")
    _set_paragraph_base(p, preserve_style=True)
    clean = line.strip().strip("#").strip()
    _add_markdown_runs(
        p,
        clean,
        size=HEADING_SIZE,
        base_color=HEADING_COLOR,
        default_bold=True,
        underline=False,
    )
    return p


def _add_blank_line(doc: Document):
    p = doc.add_paragraph()
    _set_paragraph_base(p)
    return p


def _add_body(doc: Document, line: str, *, bullet: bool = False, indent_level: int = 0):
    p = doc.add_paragraph()
    _set_paragraph_base(p, bullet=bullet, indent_level=indent_level)
    if bullet:
        s = line.strip()
        # Normalise bullet markers and make nested bullets visibly different.
        m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", s)
        if m:
            marker = BULLET_MARKERS[min(max(0, indent_level), len(BULLET_MARKERS) - 1)]
            s = f"{marker}\t{m.group(2)}"
        _add_markdown_runs(p, s)
    else:
        _add_markdown_runs(p, line.strip())
    return p


def _add_dtp(doc: Document, line: str, policy: str):
    if policy == "hide_note_insert_image":
        return None
    p = doc.add_paragraph()
    _set_paragraph_base(p)
    run = p.add_run(line.strip())
    _set_run_font(run, size=BODY_SIZE, bold=False, color=RGBColor(255, 0, 0))
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return p


# ---------------------------------------------------------------- tables ----

def _is_table_row(line: str) -> bool:
    return line.count("|") >= 2 and bool(line.strip())


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    if s.count("|") < 2 or "-" not in s:
        return False
    return all(ch in "-:| \t" for ch in s)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _table_block(lines: list[str], start: int) -> tuple[int, list[list[str]]]:
    """Return (lines consumed, rows) for a markdown pipe table at `start`.

    A table is a header row, a dash/colon separator row, then body rows; anything
    else returns (0, []) so the caller falls back to normal paragraph rendering.
    """
    if start + 1 >= len(lines):
        return 0, []
    if not _is_table_row(lines[start]) or _is_table_separator(lines[start]):
        return 0, []
    if not _is_table_separator(lines[start + 1]):
        return 0, []
    rows = [_split_row(lines[start])]
    i = start + 2
    while i < len(lines) and _is_table_row(lines[i]) and not _is_table_separator(lines[i]):
        rows.append(_split_row(lines[i]))
        i += 1
    return i - start, rows


def _add_table(doc: Document, rows: list[list[str]]):
    """Render parsed markdown rows as a real Word table (header row bolded)."""
    ncols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncols)
    try:
        table.style = "Table Grid"
    except Exception:
        pass  # style missing from the template: keep the borderless default
    table.autofit = True
    # This new table has no merges. Resolve its cell grid once: Table.cell()
    # rebuilds the entire grid on every access, making large reports quadratic.
    cells = table._cells
    for r_i, row in enumerate(rows):
        for c_i in range(ncols):
            cell = cells[r_i * ncols + c_i]
            text = row[c_i] if c_i < len(row) else ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.05
            _add_markdown_runs(
                p,
                text,
                size=TABLE_SIZE,
                base_color=HEADING_COLOR if r_i == 0 else DARK_GREY,
                default_bold=(r_i == 0),
            )
            if r_i == 0:
                _set_shading(cell._tc, TABLE_HEADER_FILL)
    return table


# ------------------------------------------------------------ flowcharts ----

_ARROW_RE = re.compile(r"^(?:[↓⬇⇓⇩]+|\|+|[vV])$")


def _is_arrow(line: str) -> bool:
    return bool(_ARROW_RE.match(line.strip()))


def _flowchart_block(lines: list[str], start: int) -> tuple[int, list[str]]:
    """Return (lines consumed, steps) for a `step / ↓ / step` run at `start`."""
    i = start
    steps: list[str] = []
    while i < len(lines):
        step = lines[i].strip()
        if not step or _is_arrow(step) or _is_table_row(step) or _is_dtp(step):
            break
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and _is_arrow(lines[j]):
            steps.append(step)
            i = j + 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            continue
        if steps:  # the tail step: it closes the chain, nothing follows it
            steps.append(step)
            i += 1
        break
    if len(steps) >= 2:
        return i - start, steps
    return 0, []


def _add_flowchart(doc: Document, steps: list[str]):
    """Centred shaded boxes with a centred arrow between them."""
    for idx, step in enumerate(steps):
        if idx:
            arrow = doc.add_paragraph()
            _set_paragraph_base(arrow)
            arrow.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(arrow.add_run("↓"), size=HEADING_SIZE, bold=True, color=HEADING_COLOR)
        p = doc.add_paragraph()
        _set_paragraph_base(p)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.left_indent = Cm(3.0)
        p.paragraph_format.right_indent = Cm(3.0)
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        _set_shading(p._element, FLOW_FILL)
        _set_paragraph_border(p, color=FLOW_BORDER, sides=("top", "left", "bottom", "right"), size=8)
        _add_markdown_runs(p, step, default_bold=True, base_color=HEADING_COLOR)


# ------------------------------------------------ correction pointer blocks ----

_CORRECTION_MARKER_RE = re.compile(
    r"^[\s>*•·\-–]*\[\s*(" + "|".join(re.escape(k) for k in CORRECTION_STYLES) + r")\s*\]\s*\**\s*$",
    re.I,
)
_CORRECTION_FIELD_RE = re.compile(
    r"^[\s>*•·\-–]*\**\s*(" + "|".join(re.escape(f) for f in CORRECTION_FIELDS) + r")\s*\**\s*:\s*(.*)$",
    re.I,
)


def _correction_kind(line: str) -> str | None:
    m = _CORRECTION_MARKER_RE.match(line.strip())
    return m.group(1).lower() if m else None


def _correction_block(lines: list[str], start: int) -> tuple[int, str, list[tuple[str, str]]]:
    """Return (lines consumed, kind, fields) for a pointer block at `start`.

    Fields are the `Label: value` lines that follow the marker; blank lines inside
    the block are tolerated only when another field line follows them.
    """
    kind = _correction_kind(lines[start])
    if not kind:
        return 0, "", []
    fields: list[tuple[str, str]] = []
    i = start + 1
    while i < len(lines):
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        m = _CORRECTION_FIELD_RE.match(lines[j].strip())
        if not m:
            break
        fields.append((m.group(1).strip(), m.group(2).strip()))
        i = j + 1
    return i - start, kind, fields


def _add_correction_block(doc: Document, kind: str, fields: list[tuple[str, str]]):
    fill, marker_color = CORRECTION_STYLES[kind]
    label = kind.title()
    bar_color = str(marker_color)  # RGBColor stringifies to the 'RRGGBB' hex Word wants

    def shaded(indent_extra: float = 0.0):
        p = doc.add_paragraph()
        _set_paragraph_base(p)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.left_indent = Cm(1.0 + indent_extra)
        p.paragraph_format.right_indent = Cm(0.4)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        _set_shading(p._element, fill)
        _set_paragraph_border(p, color=bar_color, sides=("left",))
        return p

    p = shaded()
    _set_run_font(p.add_run(f"[{label}]"), size=BODY_SIZE, bold=True, color=marker_color)
    for name, value in fields:
        fp = shaded(0.4)
        _set_run_font(fp.add_run(f"{name}: "), size=BODY_SIZE, bold=True, color=marker_color)
        _add_markdown_runs(fp, value, size=BODY_SIZE, base_color=DARK_GREY)


def _has_alpha(image_path: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    except Exception:
        return False


def _insert_slide_image(doc: Document, image_path: Path, run_dir: Path, mode: str = "smart_crop"):
    if not image_path or not Path(image_path).exists():
        return False
    img = Path(image_path)
    # Skip smart-crop for transparent (AI-redrawn) images: cropping flattens the
    # alpha channel and we'd lose the see-through background over the watermark.
    if mode == "smart_crop" and not _has_alpha(img):
        img = smart_crop_image(img, run_dir / "inserted_images")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    try:
        run = p.add_run()
        run.add_picture(str(img), width=Inches(5.8))
        return True
    except Exception:
        return False


_FONT_DOWNLOAD = {
    LATIN_FONT: "https://fonts.google.com/specimen/Coming+Soon",
    DEVANAGARI_FONT: "https://fonts.google.com/specimen/Kalam",
}


def _font_warnings(what: str) -> list[str]:
    """One warning per handwriting font missing from this machine. The DOCX only
    names a font — Word/LibreOffice need it installed to render and to export a
    faithful PDF, so a silent fallback is worth telling the user about."""
    out = []
    for family in missing_note_fonts():
        script = "English" if family == LATIN_FONT else "Hindi"
        out.append(
            f"{family} handwriting font is not installed, so {script} text in the "
            f"{what} renders in a fallback font. Install it from "
            f"{_FONT_DOWNLOAD[family]} for the handwritten style."
        )
    return out


def _new_document() -> Document:
    """A4-ish margins plus the handwriting faces on the Normal style: Coming
    Soon for Latin text, Kalam for Devanagari."""
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    style = doc.styles["Normal"]
    style.font.name = LATIN_FONT
    style._element.rPr.rFonts.set(qn("w:eastAsia"), LATIN_FONT)
    style._element.rPr.rFonts.set(qn("w:cs"), DEVANAGARI_FONT)
    style.font.size = BODY_SIZE
    return doc


def _render_body(doc: Document, text: str, *, show_corrections: bool = True, dtp_handler=None) -> bool:
    """Render markdown-ish notes into `doc`; returns True if anything was added.

    Block constructs (pipe tables, arrow flowcharts, correction pointer blocks)
    are matched first and consume all their lines, so they are never also emitted
    as ordinary paragraphs. `dtp_handler(line)` handles "(Note to DTP: ...)" lines
    when the caller has slides to insert.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    content_added = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        used, rows = _table_block(lines, i)
        if used:
            _add_table(doc, rows)
            _add_blank_line(doc)  # keeps two adjacent tables from merging in Word
            content_added = True
            i += used
            continue

        used, kind, fields = _correction_block(lines, i)
        if used:
            if show_corrections:
                _add_correction_block(doc, kind, fields)
                _add_blank_line(doc)
                content_added = True
            i += used
            continue

        used, steps = _flowchart_block(lines, i)
        if used:
            _add_flowchart(doc, steps)
            _add_blank_line(doc)
            content_added = True
            i += used
            continue

        if _is_dtp(line):
            if dtp_handler is not None:
                dtp_handler(line)
            content_added = True
        elif _is_heading(line):
            if content_added:
                _add_blank_line(doc)
            _add_heading(doc, line)
            content_added = True
        elif _is_bullet(line):
            leading = len(line) - len(line.lstrip(" \t"))
            indent_level = min(3, leading // 4) if leading else 0
            _add_body(doc, line, bullet=True, indent_level=indent_level)
            content_added = True
        else:
            _add_body(doc, line, bullet=False)
            content_added = True
        i += 1
    return content_added


def write_notes_docx(notes_text: str, output_path: Path, slides: list[SlideData], *, run_dir: Path, image_insert_mode: str = "smart_crop", dtp_note_policy: str = "keep_note_and_insert_image", subject: str = "", chapter_title: str = "", show_corrections: bool = True) -> tuple[Path, list[str]]:
    warnings: list[str] = []
    slide_map = {s.slide_no: s for s in slides}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = _new_document()
    section = doc.sections[0]

    # Branding: prefer the high-res logo in assets/pw_logo.png; only if that is
    # absent, fall back to extracting the mark from a slide (trying several,
    # since the title slide often lacks a clean top-right mark). Then apply the
    # header logo, the footer, and the faint background watermark.
    logo_path, wm_path = load_logo_override(run_dir / "branding")
    if not logo_path:
        candidates = [s.image_path for s in slides if s.image_path and Path(s.image_path).exists()]
        for img in candidates[:6]:
            logo_path, wm_path = extract_pw_logo(Path(img), run_dir / "branding")
            if logo_path:
                break
    try:
        setup_header(section, logo_path)
        setup_footer(section)
        if wm_path:
            add_watermark(section, wm_path)
    except Exception as e:
        warnings.append(f"Branding (header/footer/watermark) could not be applied: {e}")
    if not logo_path:
        warnings.append("No logo found (assets/pw_logo.png missing and none detected on slides).")

    add_title_block(doc, subject, chapter_title)

    counters = {"images": 0}

    def handle_dtp(line: str):
        note = parse_dtp_note(line)
        _add_dtp(doc, line, dtp_note_policy)
        if dtp_note_policy not in {"keep_note_and_insert_image", "hide_note_insert_image"}:
            return
        if note and note.slide_no and note.slide_no in slide_map and slide_map[note.slide_no].image_path:
            if _insert_slide_image(doc, slide_map[note.slide_no].image_path, run_dir, mode=image_insert_mode):
                counters["images"] += 1
            else:
                warnings.append(f"Could not insert image for slide {note.slide_no}.")
        else:
            warnings.append(f"DTP note found but slide image could not be matched: {line[:140]}")

    _render_body(doc, notes_text, show_corrections=show_corrections, dtp_handler=handle_dtp)
    inserted_images = counters["images"]
    doc.save(str(output_path))
    warnings.append(f"Inserted {inserted_images} image(s) from DTP notes.")
    warnings.extend(_font_warnings("notes"))
    return output_path, warnings


def write_quality_report_docx(report_md: str, output_path: Path, *, title: str = "Quality Check Report", subject: str = "", chapter_title: str = "") -> tuple[Path, list[str]]:
    """Render the AI quality report (markdown) as a standalone DOCX.

    Reuses the notes renderers, so the Summary bullets and the Correction Log
    pipe table come out as real bullets and a real Word table.
    """
    warnings: list[str] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = _new_document()
    try:
        setup_footer(doc.sections[0])
    except Exception as e:
        warnings.append(f"Report footer could not be applied: {e}")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    _set_run_font(p.add_run(title), size=Pt(22), bold=True, color=HEADING_COLOR)
    subtitle = " — ".join(x for x in (subject, chapter_title) if x)
    if subtitle:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sp.paragraph_format.space_after = Pt(10)
        _set_run_font(sp.add_run(subtitle), size=Pt(14), bold=True, color=HEADING_COLOR)
    rule = doc.add_paragraph()
    rule.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rule.paragraph_format.space_after = Pt(10)
    _set_run_font(rule.add_run("_______________________________"), size=Pt(10), color=HEADING_COLOR)

    if not _render_body(doc, report_md or ""):
        warnings.append("Quality report was empty; the DOCX has only a title page.")
    doc.save(str(output_path))
    warnings.extend(_font_warnings("report"))
    return output_path, warnings
