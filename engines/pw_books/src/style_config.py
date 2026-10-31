"""Central visual identity of the PW concise-notes template.

Every page/font/colour/spacing constant the DOCX renderer uses lives here, so
the whole note series looks identical whatever the subject is. The values are
taken from the reference document "Basic Maths & Calculus-02_Concise Notes.docx".

Nothing in this module knows anything about a subject or a chapter — content is
dynamic, formatting is fixed. See docx_layout.py (furniture) and docx_writer.py
(body) for the consumers.
"""
from __future__ import annotations

import glob
import os
import unicodedata
from functools import lru_cache

from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
PAGE_SIZE = "A4"
ORIENTATION = "portrait"
PAGE_WIDTH_IN = 8.27
PAGE_HEIGHT_IN = 11.69

MARGIN_TOP_IN = 1.0
MARGIN_BOTTOM_IN = 1.0
MARGIN_LEFT_IN = 1.0
MARGIN_RIGHT_IN = 1.0

# ~6.27 in — nothing (text, equation, table or image) may cross this.
USABLE_WIDTH_IN = round(PAGE_WIDTH_IN - MARGIN_LEFT_IN - MARGIN_RIGHT_IN, 3)
USABLE_HEIGHT_IN = round(PAGE_HEIGHT_IN - MARGIN_TOP_IN - MARGIN_BOTTOM_IN, 3)

# Compact header/footer distances, as in the reference document.
HEADER_DISTANCE_IN = 0.20
FOOTER_DISTANCE_IN = 0.12

# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
FONT_PRIMARY = "Kalam"          # the handwritten identity of the whole series
FONT_MATH = "Cambria Math"      # equations + any glyph Kalam cannot render
FONT_SYMBOL = "Cambria"         # text-mode companion of the maths font

SUBJECT_SIZE_PT = 26
LECTURE_TITLE_SIZE_PT = 24
SECTION_HEADING_SIZE_PT = 18
BODY_SIZE_PT = 16
FOOTER_SIZE_PT = 12
PAGE_NUMBER_SIZE_PT = 11

SUBJECT_SIZE = Pt(SUBJECT_SIZE_PT)
LECTURE_TITLE_SIZE = Pt(LECTURE_TITLE_SIZE_PT)
SECTION_HEADING_SIZE = Pt(SECTION_HEADING_SIZE_PT)
BODY_SIZE = Pt(BODY_SIZE_PT)

# Expanded character spacing = 1.5 pt (1 pt = 20 twips) — part of the
# handwritten look; applied to every run the renderer creates.
CHAR_SPACING_TWIPS = 30

# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------
TITLE_COLOR = RGBColor(0xFF, 0x00, 0x00)        # #FF0000 subject + chapter
BODY_COLOR = RGBColor(0x00, 0x00, 0x99)         # #000099 body + bullets
HEADING_COLOR = RGBColor(0x00, 0x00, 0x00)      # #000000 section headings
BOLD_COLOR = RGBColor(0xE9, 0x71, 0x32)         # inline **bold** accent
FOOTER_COLOR = RGBColor(0x1F, 0x4E, 0x79)       # "Master NCERT with PW Books APP"
PAGE_NUMBER_COLOR = RGBColor(0x33, 0x33, 0x33)
DTP_COLOR = RGBColor(0xFF, 0x00, 0x00)          # visible DTP instruction note
HEADING_HIGHLIGHT = WD_COLOR_INDEX.BRIGHT_GREEN  # Word bright-green text highlight
DTP_HIGHLIGHT = WD_COLOR_INDEX.YELLOW            # visible DTP instruction note

# --------------------------------------------------------------------------
# Paragraph geometry
# --------------------------------------------------------------------------
TITLE_LINE_SPACING = 1.0
HEADING_LINE_SPACING = 1.15
BODY_LINE_SPACING = 1.2
SPACE_AFTER_PT = 0
SPACE_BEFORE_PT = 0

BULLET_LEFT_INDENT_IN = 0.236
BULLET_HANGING_INDENT_IN = -0.236
# Each nesting level shifts the bullet right by this much (the hanging indent
# is unchanged, so wrapped lines still align with the text, not the marker).
BULLET_LEVEL_STEP_IN = 0.276
BULLET_MARKERS = ("•", "◦", "▪", "–")

# --------------------------------------------------------------------------
# Branding furniture
# --------------------------------------------------------------------------
# Reference watermark: 358.85 x 362.25 pt == 4.98 x 5.03 in, centred behind the
# text. Anchored from the header so it never becomes a body image.
WATERMARK_WIDTH_IN = 4.98
WATERMARK_MAX_HEIGHT_IN = 5.03
HEADER_LOGO_HEIGHT_PT = 48
# The footer icon is the PW mark itself, not an emoji: the old \U0001F4D8 book
# glyph exists in no document font and printed as a missing-glyph box in the
# exported PDF. A real image renders identically in Word and in the PDF.
FOOTER_ICON_HEIGHT_PT = 13
FOOTER_TEXT = "Master NCERT with PW Books APP"

# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
# Reference-document diagrams occupy roughly 5.0-6.5 in of width. Width is
# chosen per image from its own resolution (see image_filter.fit_image_size),
# never forced to one value, and never allowed past the usable width.
IMAGE_MAX_WIDTH_IN = USABLE_WIDTH_IN
IMAGE_MIN_WIDTH_IN = 2.20
IMAGE_MAX_HEIGHT_IN = 6.50          # keeps a diagram + its section on one page
IMAGE_MAX_UPSCALE = 1.60            # never blow a low-res crop up beyond this
IMAGE_ASSUMED_DPI = 96


# --------------------------------------------------------------------------
# Font fallback for maths / scientific glyphs
# --------------------------------------------------------------------------
# Kalam is a Latin + Devanagari handwriting face. It has no Greek block, no
# maths operators, and — critically — no combining accents, which is what used
# to erase vector arrows and unit-vector hats. Anything outside the ranges
# below is rendered in FONT_MATH so the notation stays correct; the surrounding
# prose keeps its handwritten Kalam identity.
_SAFE_RANGES = (
    (0x0020, 0x007E),   # ASCII printable
    (0x00C0, 0x00FF),   # Latin-1 letters (accented)
    (0x0100, 0x024F),   # Latin Extended A/B
    (0x0900, 0x097F),   # Devanagari (Hindi notes)
    (0x2000, 0x200B),   # spaces
)
_SAFE_EXTRAS = set(" –—‘’“”…")

# Latin-1 characters that ARE maths even though they sit in a "safe" range.
# Rendering them in Cambria Math keeps a formula visually uniform and dodges
# Kalam's weak coverage of these particular glyphs.
_FORCE_MATH = set("°±×÷µ·"
                  "¹²³¼½¾")


def _char_is_kalam_safe(ch: str) -> bool:
    if ch in _FORCE_MATH:
        return False
    if ch in _SAFE_EXTRAS:
        return True
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _SAFE_RANGES)


def _clusters(text: str):
    """Yield (cluster, safe) — a base character plus any combining marks.

    Combining marks (the vector arrow U+20D7, hat U+0302, bar U+0304) must never
    be split away from their base into another run, and a base that carries one
    must be drawn in the maths font because Kalam has no combining glyphs.
    """
    i = 0
    n = len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        cluster = text[i:j]
        yield cluster, all(_char_is_kalam_safe(c) for c in cluster)
        i = j


def split_font_runs(text: str, *, primary: str = FONT_PRIMARY, math: str = FONT_MATH) -> list[tuple[str, str]]:
    """Split `text` into (chunk, font_name) pieces.

    Consecutive characters sharing a font are merged, so a normal sentence still
    produces exactly one run. Returns [] for empty input.
    """
    if not text:
        return []
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    cur: str | None = None
    for cluster, safe in _clusters(text):
        font = primary if safe else math
        if font != cur:
            if buf:
                out.append(("".join(buf), cur or primary))
            buf = [cluster]
            cur = font
        else:
            buf.append(cluster)
    if buf:
        out.append(("".join(buf), cur or primary))
    return out


# --------------------------------------------------------------------------
# Shared docx helpers
# --------------------------------------------------------------------------
def apply_page_setup(section) -> None:
    """A4 portrait, 1 in margins, compact header/footer distances."""
    # Order matters: set orientation, then the explicit page size. python-docx's
    # default template is US Letter, so this must be applied on every document.
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Inches(PAGE_WIDTH_IN)
    section.page_height = Inches(PAGE_HEIGHT_IN)
    section.top_margin = Inches(MARGIN_TOP_IN)
    section.bottom_margin = Inches(MARGIN_BOTTOM_IN)
    section.left_margin = Inches(MARGIN_LEFT_IN)
    section.right_margin = Inches(MARGIN_RIGHT_IN)
    section.header_distance = Inches(HEADER_DISTANCE_IN)
    section.footer_distance = Inches(FOOTER_DISTANCE_IN)
    section.gutter = Inches(0)


def set_char_spacing(run, twips: int = CHAR_SPACING_TWIPS) -> None:
    rpr = run._element.get_or_add_rPr()
    sp = rpr.find(qn("w:spacing"))
    if sp is None:
        sp = OxmlElement("w:spacing")
        rpr.append(sp)
    sp.set(qn("w:val"), str(twips))


def set_run_fonts(run, font_name: str) -> None:
    """Pin one font family on all four script slots of a run."""
    run.font.name = font_name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), font_name)


# --------------------------------------------------------------------------
# Text measurement — used to keep justification from opening ugly word gaps
# --------------------------------------------------------------------------
FONT_SEARCH_DIRS = (
    r"C:\Windows\Fonts",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    "/usr/share/fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
)

# Fallback advance width per character, in em, when the real font file cannot be
# found. Calibrated against rendered Kalam output; only used to decide
# alignment, never to position anything.
_FALLBACK_EM_PER_CHAR = 0.50

# Word justifies by widening the spaces. Once a space has to grow by more than
# this fraction of its natural width the gaps read as holes, and the paragraph
# is left-aligned instead — the reference look is justified text, but
# readability wins on a bad line. Calibrated on a real lecture: at 0.40 about a
# third of the multi-line bullets fall back to left, which is the point where
# no visibly stretched line survives and the notes still read as justified.
MAX_SPACE_STRETCH = 0.40


@lru_cache(maxsize=1)
def find_primary_font_file() -> str | None:
    """Locate an installed Kalam TTF, or None."""
    for d in FONT_SEARCH_DIRS:
        if d and os.path.isdir(d):
            hits = glob.glob(os.path.join(d, "**", "[Kk]alam*.ttf"), recursive=True)
            if hits:
                # Prefer the regular weight over Bold/Light when present.
                hits.sort(key=lambda p: ("regular" not in os.path.basename(p).lower(), p))
                return hits[0]
    return None


@lru_cache(maxsize=1)
def _measuring_font():
    """A PIL font object at a fixed size, or None when unavailable."""
    path = find_primary_font_file()
    if not path:
        return None
    try:
        from PIL import ImageFont

        return ImageFont.truetype(path, 64)
    except Exception:
        return None


def measure_text_in(text: str, size_pt: float = BODY_SIZE_PT) -> float:
    """Approximate rendered width of `text` in INCHES at `size_pt`.

    Uses the real Kalam metrics when the font is installed and falls back to an
    average-character model otherwise. Accuracy only has to be good enough to
    choose an alignment, so a missing font degrades the look, never the output.
    """
    if not text:
        return 0.0
    font = _measuring_font()
    em = None
    if font is not None:
        try:
            em = font.getlength(text) / 64.0        # width in em units
        except Exception:
            em = None
    if em is None:
        em = len(text) * _FALLBACK_EM_PER_CHAR
    # Every run carries expanded character spacing, which at 1.5 pt/char adds
    # nearly an inch to a full line. Leaving it out made the model think a line
    # still fitted when Word had already wrapped it.
    tracking_in = len(text) * (CHAR_SPACING_TWIPS / 20.0) / 72.0
    return em * size_pt / 72.0 + tracking_in


def _greedy_wrap(words: list[str], avail_in: float, size_pt: float) -> list[list[str]]:
    lines: list[list[str]] = []
    current: list[str] = []
    for word in words:
        trial = current + [word]
        if current and measure_text_in(" ".join(trial), size_pt) > avail_in:
            lines.append(current)
            current = [word]
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def justification_is_natural(text: str, avail_in: float, size_pt: float = BODY_SIZE_PT,
                             max_stretch: float = MAX_SPACE_STRETCH) -> bool:
    """Whether justifying `text` in a column `avail_in` wide still reads well.

    Word stretches every line EXCEPT the last one out to the full column width,
    and it does so by widening the spaces. A line with few words — or long ones
    — has few gaps to absorb that slack, so each one opens into a hole. Such
    paragraphs are left-aligned instead, which is exactly what the spec asks
    for: keep the justified look, but not at the cost of readability.
    """
    words = (text or "").split()
    if not words or avail_in <= 0:
        return True
    lines = _greedy_wrap(words, avail_in, size_pt)
    if len(lines) < 2:
        return True                       # a single line is never stretched
    space = measure_text_in(" ", size_pt) or 0.09
    for line in lines[:-1]:
        gaps = len(line) - 1
        if gaps < 1:
            return False                  # one word stretched across the column
        slack = avail_in - measure_text_in(" ".join(line), size_pt)
        if slack / gaps > max_stretch * space:
            return False
    return True
