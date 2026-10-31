"""Draws a typeset document (document.py) in the B.Sc. Wallah notes' look, measured
from the sample PDFs (LaTeX, pdfTeX): A4 on a faint blue page, Lato 10.91 pt
text on a 14.35 pt line, Computer Modern formulas, CM Sans bold box titles,
the six coloured boxes (3 mm corners, 0.6 pt frame), booktabs tables, figures
with "Figure N:" captions, and a header and footer on every page.

The paginator keeps a heading with what follows it, never leaves one or two
lines of a paragraph or box alone at a page's top or bottom, moves a short box
whole rather than splitting it, and lets a figure that does not fit wait for
the next page while the text after it fills the gap (as LaTeX floats do)."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .. import paths
from . import diagrams, formulas
from .document import Span, parse_inline

FONT_DIR = paths.PACKAGE_DIR / "static" / "fonts" / "typeset"
PAGE_W, PAGE_H = 595.276, 841.89
LEFT, RIGHT = 32.4, 562.88
TEXT_W = RIGHT - LEFT
TOP, BOTTOM = 46.8, 798.7
BODY, LEAD = 10.91, 14.35
BG, NAVY, GREY, BLACK, WHITE = (250, 252, 255), (32, 74, 135), (75, 85, 95), (0, 0, 0), (255, 255, 255)
MARK = (252, 237, 223)
# style: frame, title bar, title text, body, indent of the title and the text
BOXES = {
    "definition": ((32, 74, 135), (32, 74, 135), WHITE, (246, 248, 250), 11.9),
    "concept": ((235, 135, 45), (235, 135, 45), WHITE, (254, 248, 242), 11.9),
    "formula": ((110, 80, 160), (110, 80, 160), WHITE, (249, 248, 251), 14.8),
    "warning": ((190, 70, 70), (190, 70, 70), WHITE, (252, 246, 246), 11.9),
    "summary": ((62, 139, 210), (62, 139, 210), WHITE, (245, 249, 253), 14.8),
    "note": ((165, 95, 32), (251, 231, 213), GREY, (255, 253, 236), 11.8),
}
FRAME, ARC, TITLE_BAR, TITLE_LINE = 0.6, 8.5, 15.73, 13.0      # a longer title adds a line to the bar
BOX_PAD_TOP, BOX_PAD_BOTTOM = 7.4, 8.3
HEAD_ROW, GRID_HEAD = (233, 237, 243), (222, 228, 237)
RULES = {"top": (0.0, 0.87, 3.0), "mid": (1.8, 0.54, 3.3), "bottom": (1.5, 0.87, 2.0)}   # booktabs: gap, width, gap
# Vertical space before/after each kind of block: the larger of two neighbours'
# wins, except that a heading after a box, table or figure adds both (as LaTeX does).
SPACE = {"title": (0, 16), "section": (17.1, 13.9), "subsection": (16.1, 10.9), "subsubsection": (12, 8),
         "heading": (14.7, 8), "paragraph": (3, 3), "bullets": (2, 2), "numbered": (2, 2), "math": (3, 4.4),
         "box": (5, 4), "table": (24, 14), "figure": (12, 12), "tree": (12, 12), "flow": (12, 12)}
ADDS = ("box", "table", "figure", "tree", "flow")
HEADING_SIZE = {"section": 14.35, "subsection": 11.96, "subsubsection": 10.91, "heading": 11.96}
LIST_INDENT, NUMBER_INDENT, LABEL_SEP, SUB_LIST_GAP = 11.8, 14.4, 5.6, 1.9
MATH_STRUT, MATH_PAD = (13.5, 4.1), (5.0, 1.9)      # a display formula's least ascent/depth, space above/below
HEAD_RIGHT = RIGHT - 2.6            # where the header and footer end on the right
SMALL_BOX = 170.0           # a box no taller than this moves to the next page whole
SHRINK = 1 / 3              # a justified line's spaces may shrink by this much (TeX's interword shrink)
FIGURE_SHRINK = 0.72        # a figure may shrink to this share of its size to fit a page's end

FACES = {"lato": {"": "Lato-Regular.ttf", "B": "Lato-Bold.ttf", "I": "Lato-Italic.ttf", "BI": "Lato-BoldItalic.ttf"},
         "cmss": {"": "cmunss.otf", "B": "cmunsx.otf", "I": "cmunsi.otf", "BI": "cmunso.otf"},
         "hind": {"": "Hind-Regular.ttf", "B": "Hind-Bold.ttf"}}


def _dejavu() -> dict[str, Path]:
    import matplotlib

    folder = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    return {"": folder / "DejaVuSans.ttf", "B": folder / "DejaVuSans-Bold.ttf",
            "I": folder / "DejaVuSans-Oblique.ttf", "BI": folder / "DejaVuSans-BoldOblique.ttf"}


@lru_cache(maxsize=None)
def _coverage(path: str) -> frozenset:
    from fontTools.ttLib import TTFont

    return frozenset(TTFont(path, lazy=True).getBestCmap() or {})


def _devanagari(cp: int) -> bool:
    return 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF or 0x1CD0 <= cp <= 0x1CFF or cp in (0x200C, 0x200D)


@dataclass
class Branding:
    header_left: str = "B.Sc. Wallah"
    header_right: str = "B.Sc. Notes"
    footer_left: str = "B.Sc. Wallah"
    footer_right: str = "Concept • Understand • Apply"


# ------------------------------------------------------------ text pieces
@dataclass
class Frag:
    text: str
    w: float
    family: str = "lato"
    style: str = ""
    size: float = BODY
    color: tuple = BLACK
    mark: bool = False
    formula: formulas.Formula | None = None
    logical: str | None = None


@dataclass
class Line:
    words: list[list[Frag]]
    natural: float
    space: float
    ascent: float
    descent: float
    last: bool = False
    actual_text: str | None = None

    @property
    def height(self) -> float:
        return self.ascent + self.descent


@dataclass
class Row:
    """One unbreakable strip of a page (a line, a formula, a figure), drawn at its top y."""
    height: float
    draw: Callable[[float], None]
    nobreak_after: bool = False
    kind: str = "line"


@dataclass
class Glue:
    height: float
    after: float = 0.0          # what it was made of, to remake it if the block before it moves away
    before: float = 0.0
    kinds: tuple = ("", "")


@dataclass
class Frame:
    """A coloured box; its rows may be split across pages."""
    style: str
    title: list[Line]
    rows: list
    indent: float
    source_id: str = ""

    def bar_height(self) -> float:
        return TITLE_BAR + TITLE_LINE * max(0, len(self.title) - 1)

    def title_height(self) -> float:
        return FRAME + self.bar_height() + FRAME if self.title else 0.0

    def height(self, start: int = 0, end: int | None = None, first: bool = True) -> float:
        rows = _trim(self.rows[start:end])
        return (self.title_height() if first else FRAME) + BOX_PAD_TOP + _sum(rows) + BOX_PAD_BOTTOM


@dataclass
class Table:
    head: Row
    rows: list[Row]
    caption: list[Row]
    rule: Callable[[float, str], float]     # draws a rule at y; returns its height
    source_id: str = ""


@dataclass
class Figure:
    """An image (or a diagram) with its caption; it may wait for the next page."""
    draw: Callable[[float, float], float]   # draw(y, scale of the picture) -> height used
    picture: float                          # the picture's height at full size
    extra: float                            # its caption and the gap to it
    can_shrink: bool = True
    page: int = 0                           # the page it waited on

    @property
    def height(self) -> float:
        return self.picture + self.extra

    def scale_for(self, room: float) -> float:
        return min(1.0, max(0.05, (room - self.extra) / self.picture))


def gap(after: float, before: float, kinds: tuple) -> float:
    """The space between two blocks of these kinds."""
    return after + before if kinds[0] in ADDS and kinds[1] in HEADING_SIZE else max(after, before)


def _sum(items) -> float:
    return sum(i.height for i in items)


def _trim(items: list) -> list:
    """No space at the top or bottom of a page part."""
    start, end = 0, len(items)
    while start < end and isinstance(items[start], Glue):
        start += 1
    while end > start and isinstance(items[end - 1], Glue):
        end -= 1
    return items[start:end]


# ------------------------------------------------------------- the canvas
class Canvas:
    def __init__(self, branding: Branding, title: str):
        from fpdf import FPDF

        canvas = self

        class _Pdf(FPDF):
            def file_id(self):
                return None

            def header(self):
                canvas._page_top()

            def footer(self):
                canvas._page_bottom()

        self.branding, self.title = branding, title
        self.pdf = _Pdf(unit="pt", format="A4")
        self.pdf.set_margins(0, 0, 0)
        self.pdf.set_auto_page_break(False)
        self.pdf.c_margin = 0
        for family, styles in FACES.items():
            for style, name in styles.items():
                self.pdf.add_font(family, style, str(FONT_DIR / name))
        for style, path in _dejavu().items():
            self.pdf.add_font("dejavu", style, str(path))
        self.pdf.set_text_shaping(True)
        self._widths: dict = {}
        self.issues: list[str] = []
        self.pdf.set_title(title)
        self.pdf.set_author(branding.header_left)
        self.pdf.set_creator("NoteCraft typeset notes")

    # ---- fonts
    def _face(self, family: str, style: str) -> tuple[str, str]:
        styles = FACES.get(family) or _dejavu()
        if style not in styles:
            style = "B" if "B" in style and "B" in styles else ""
        return family, style

    def width(self, family: str, style: str, size: float, text: str) -> float:
        key = (family, style, size, text)
        if key not in self._widths:
            self.pdf.set_font(family, style, size)
            self._widths[key] = self.pdf.get_string_width(text)
        return self._widths[key]

    def _chunks(self, text: str, family: str, style: str) -> list[tuple[str, str, str]]:
        """(family, style, text) runs: Devanagari in Hind, anything the face lacks in DejaVu."""
        base = str(FONT_DIR / FACES[family][""])
        dejavu = str(_dejavu()[""])
        out: list[list[str]] = []
        for char in text:
            cp = ord(char)
            if _devanagari(cp):
                face = "hind"
            elif cp in _coverage(base) or char.isspace():
                face = family
            elif cp in _coverage(dejavu):
                face = "dejavu"
            else:
                face, char = family, "?"
            if out and out[-1][0] == face:
                out[-1][1] += char
            else:
                out.append([face, char])
        return [(*self._face(face, style), piece) for face, piece in out]

    # ---- inline layout
    def words(self, text: str, *, size: float = BODY, family: str = "lato", bold: bool = False,
              italic: bool = False, color: tuple = BLACK, literal: bool = False, inline_math: bool = False) -> list[list[Frag]]:
        words: list[list[Frag]] = [[]]
        spans = [Span(s, math=bool(i%2)) for i,s in enumerate(re.split(r"(?<!\\)\$([^$]+)\$", text))] \
            if literal and inline_math else [Span(text)] if literal else parse_inline(text)
        for span in spans:
            if span.math:
                frag = self._formula_frag(span.text, size)
                words[-1].append(frag)
                continue
            style = ("B" if bold or span.bold or span.key or span.mark else "") + ("I" if italic or span.italic else "")
            tint = NAVY if span.key else color
            for n, piece in enumerate(span.text.split(" ")):
                if n:
                    words.append([])
                if piece:
                    symbol = {r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒"}.get(piece) \
                        if literal and inline_math else None
                    for face, face_style, chunk in self._chunks(symbol or piece, family, style):
                        words[-1].append(Frag(chunk, self.width(face, face_style, size, chunk), face, face_style,
                                              size, tint, span.mark, logical=piece if symbol else None))
        return [w for w in words if w]

    def _formula_frag(self, latex: str, size: float) -> Frag:
        try:
            made = formulas.render(latex, size)
            return Frag(latex, made.width, size=size, formula=made)
        except formulas.FormulaError:
            self.issues.append(f"A formula could not be drawn and is printed as text: {latex[:60]}")
            text = latex.replace("\\", "").replace("{", "").replace("}", "")
            return Frag(text, self.width("lato", "I", size, text), "lato", "I", size)

    def layout(self, text: str, width: float, *, size: float = BODY, family: str = "lato", bold: bool = False,
               italic: bool = False, color: tuple = BLACK, align: str = "J", leading: float = LEAD,
               first_width: float | None = None, literal: bool = False, inline_math: bool = False) -> list[Line]:
        """Lines of one text, filled greedily (first_width: a narrower first line)."""
        words = self.words(text, size=size, family=family, bold=bold, italic=italic, color=color,
                           literal=literal, inline_math=inline_math)
        space = self.width(family, "B" if bold else "", size, " ")
        lines: list[Line] = []
        current: list[list[Frag]] = []
        used = 0.0
        for word in words:
            word_w = sum(f.w for f in word)
            room = first_width if (first_width is not None and not lines) else width
            shrink = space * SHRINK * len(current) if align == "J" else 0.0
            if current and used + space + word_w - shrink > room + 0.01:
                lines.append(self._line(current, used, space, size, leading))
                current, used = [], 0.0
            current.append(word)
            used = word_w if len(current) == 1 else used + space + word_w
        if current:
            lines.append(self._line(current, used, space, size, leading))
        if lines:
            lines[-1].last = True
        if literal:
            for line in lines:
                line.actual_text = " ".join("".join("$"+f.text+"$" if inline_math and f.formula else f.text
                                                   for f in word) for word in line.words)
        return lines

    @staticmethod
    def _line(words, natural, space, size, leading) -> Line:
        """A formula taller than the text opens the line up only as far as it must (TeX's lineskip)."""
        ascent, descent = leading * 0.732, leading * 0.268
        for frag in (f for w in words for f in w if f.formula):
            ascent = max(ascent, frag.formula.ascent + 0.2)
            descent = max(descent, frag.formula.depth + 0.2)
        return Line(words, natural, space, ascent, descent)

    def draw_line(self, line: Line, x: float, baseline: float, width: float, align: str = "J") -> None:
        self._draw_line(line, x, baseline, width, align)

    def _draw_line(self, line: Line, x: float, baseline: float, width: float, align: str = "J") -> None:
        gaps = len(line.words) - 1
        extra = width - line.natural
        gap = line.space
        if gaps and extra < 0:                               # spaces shrunk to fit (at most to 2/3)
            gap += max(extra / gaps, -line.space * SHRINK)
        elif align == "J" and not line.last and gaps and 0 < extra <= gaps * line.space * 3.2:
            gap += extra / gaps
        elif align == "C":
            x += max(0.0, extra) / 2
        elif align == "R":
            x += max(0.0, extra)
        placed: list[tuple[float, Frag]] = []
        groups = []
        cursor = x
        for n, word in enumerate(line.words):
            group = []
            if n:
                cursor += gap
            for frag in word:
                placed.append((cursor, frag))
                group.append((cursor, frag))
                cursor += frag.w
            groups.append(group)
        self._marks(placed, baseline)
        pdf = self.pdf
        for group in groups:
            if line.actual_text is not None:
                # Scope the logical Unicode to one actual painted word, avoiding
                # reader spacing heuristics across independently positioned words.
                logical = "".join(f.logical if f.logical is not None else "$"+f.text+"$" if f.formula else f.text
                                  for _,f in group)
                encoded = ("\ufeff"+logical).encode("utf-16-be").hex().upper()
                pdf._out(f"/Span << /ActualText <{encoded}> >> BDC")
            for left, frag in group:
                if frag.formula is not None:
                    f = frag.formula
                    pdf.image(io.BytesIO(f.png), left, baseline-f.ascent, w=f.width, h=f.height)
                    continue
                pdf.set_font(frag.family, frag.style, frag.size)
                pdf.set_text_color(*frag.color)
                pdf.set_xy(left, baseline-0.3*frag.size)
                pdf.cell(w=frag.w, h=0, text=frag.text)
            if line.actual_text is not None:
                pdf._out("EMC")

    def _marks(self, placed: list[tuple[float, Frag]], baseline: float) -> None:
        """One highlight behind each run of ==marked== words (spaces between them included)."""
        runs: list[list[float]] = []
        previous = False
        for left, frag in placed:
            if frag.mark:
                if previous and runs:
                    runs[-1][1] = left + frag.w
                else:
                    runs.append([left, left + frag.w, frag.size])
            previous = frag.mark
        self.pdf.set_fill_color(*MARK)
        for x0, x1, size in runs:
            self.pdf.rect(x0 - 3.0, baseline - size * 1.03, x1 - x0 + 6.0, size * 1.49, style="F")

    def draw_text(self, lines: list[Line], x: float, top: float, width: float, align: str = "J") -> float:
        for line in lines:
            self.draw_line(line, x, top + line.ascent, width, align)
            top += line.height
        return top

    # ---- page furniture
    def _page_top(self) -> None:
        pdf, b = self.pdf, self.branding
        pdf.set_fill_color(*BG)
        pdf.rect(0, 0, PAGE_W, PAGE_H, style="F")
        self._label(b.header_left, LEFT, 18.3, "B", NAVY)
        self._label(b.header_right, HEAD_RIGHT, 18.3, "I", GREY, right=True)
        pdf.set_draw_color(*BLACK)
        pdf.set_line_width(0.5)
        pdf.line(LEFT, 22.1, RIGHT, 22.1)

    def _page_bottom(self) -> None:
        b = self.branding
        self._label(b.footer_left, LEFT, 823.6, "", GREY)
        number = str(self.pdf.page_no())
        self._label(number, (PAGE_W - self.width("lato", "", 9.96, number)) / 2, 823.6, "", GREY)
        self._label(b.footer_right, HEAD_RIGHT, 823.6, "I", GREY, right=True)

    def _label(self, text: str, x: float, baseline: float, style: str, color: tuple, right: bool = False) -> None:
        if not text:
            return
        lines = self.layout(text, TEXT_W / 2, size=9.96, bold="B" in style, italic="I" in style, color=color,
                            align="L")[:1]
        if lines:
            width = lines[0].natural
            self.draw_line(lines[0], x - width if right else x, baseline, width, "L")


# --------------------------------------------------------- the document
class Renderer:
    def __init__(self, doc: dict, branding: Branding, figures_dir: Path | None = None):
        self.doc, self.figures_dir = doc, figures_dir
        self.canvas = Canvas(branding, doc.get("title") or "Notes")
        self.pdf = self.canvas.pdf
        self.counts = {"section": 0, "subsection": 0, "subsubsection": 0, "definition": 0, "figure": 0, "table": 0}
        self.y = TOP
        self.waiting: list[Figure] = []
        self.fills: list[float] = []
        self.drawn = {"figures": 0, "diagrams": 0, "tables": 0, "boxes": 0}
        self.source_emitted: list[str] = []

    # ---- building
    def build(self) -> list:
        items: list = []
        after, previous = 0.0, ""
        blocks = self.doc.get("blocks", [])
        if not self.doc.get("preserve_source"):
            blocks = [{"kind": "title", "text": self.doc.get("title") or ""}, *blocks]
        number_from = 1
        for block in blocks:
            if block["kind"] == "numbered":
                # A numbered list broken only by a box or formula goes on counting (enumitem's resume).
                block = {**block, "start": number_from}
                number_from += len(block["items"])
            elif block["kind"] not in ("box", "math"):
                number_from = 1
            made = self._block(block)
            if not made:
                continue
            # Adjacent locked summary/concept points share the reference's box,
            # while every individual source ID remains audited in paint order.
            if block["kind"] == "source_box" and len(made) == 1 and items and isinstance(items[-1], Frame):
                prior, current = items[-1], made[0]
                if isinstance(current, Frame) and prior.source_id and current.source_id and \
                        prior.style == current.style and not prior.title and not current.title:
                    first = next((row for row in current.rows if isinstance(row, Row)), None)
                    if first:
                        original, sid = first.draw, current.source_id
                        def draw(top, original=original, sid=sid):
                            self.source_emitted.append(sid)
                            original(top)
                        first.draw = draw
                        prior.rows.extend([Glue(3), *current.rows])
                        self.drawn["boxes"] -= 1
                        continue
            spacing_kind = (block.get("role", "paragraph") if block["kind"] == "source_text" else
                            "box" if block["kind"] == "source_box" else
                            block.get("spacing", "figure") if block["kind"] == "source_panel" else block["kind"])
            before, next_after = SPACE.get(spacing_kind, (5, 5))
            if items:
                kinds = (previous, spacing_kind)
                items.append(Glue(gap(after, before, kinds), after, before, kinds))
            items.extend(made)
            after, previous = next_after, spacing_kind
        return items

    def _block(self, block: dict) -> list:
        kind = block["kind"]
        if kind in ("source_text", "source_box", "source_panel", "source_table"):
            return self._source_block(block)
        if kind == "title":
            lines = self.canvas.layout(block["text"], TEXT_W * 0.9, size=20.66, bold=True, color=NAVY, align="C",
                                       leading=21.3)
            return self._text_rows(lines, LEFT, TEXT_W, "C", keep_all=True, kind="heading")
        if kind in HEADING_SIZE:
            return self._heading(block)
        if kind == "paragraph":
            return self._paragraph(block["text"], LEFT, TEXT_W)
        if kind in ("bullets", "numbered"):
            return self._list(block, LEFT, TEXT_W)
        if kind == "math":
            return self._math(block["text"], LEFT, TEXT_W)
        if kind == "box":
            return [self._box(block)]
        if kind == "table":
            return [self._table(block)]
        if kind == "figure":
            figure = self._figure(block)
            return [figure] if figure else []
        if kind in ("tree", "flow"):
            return self._diagram(block)
        return []

    def _source_block(self, block: dict) -> list:
        """Render locked source content literally; no generated numbers or floating images."""
        source_id = block["source_id"]
        if block["kind"] == "source_table":
            return [self._table(block)]
        if block["kind"] == "source_panel":
            path = self.figures_dir / block["image"]
            frame_style = block.get("frame_style")
            indent = BOXES[frame_style][4] if frame_style else 0
            available = TEXT_W-2*indent
            width = min(available, block["width_pt"])
            height = width * block["height_pt"] / block["width_pt"]
            max_height = BOTTOM-TOP-1-(TITLE_BAR+BOX_PAD_TOP+BOX_PAD_BOTTOM+4 if frame_style else 0)
            if height > max_height:
                width *= max_height/height
                height = max_height

            def draw(top):
                offset = (available-width)/2 if block.get("center") else min(block.get("offset_pt",0), available-width)
                self.pdf.image(str(path), LEFT+indent+max(0,offset), top, w=width, h=height)
                self.source_emitted.append(source_id)

            if frame_style:
                title = self.canvas.layout(block.get("frame_title", ""), available, family="cmss", bold=True,
                                           color=BOXES[frame_style][2], align="L", leading=TITLE_LINE, literal=True)
                self.drawn["boxes"] += 1
                return [Frame(frame_style, title, [Row(height, draw, kind="source")], indent)]
            return [Row(height, draw, kind="source")]
        if block["kind"] == "source_box":
            style = block["style"]
            indent = BOXES[style][4]
            width = TEXT_W - 2*indent
            title = self.canvas.layout(block["title"], width, family="cmss", bold=True,
                                       color=BOXES[style][2], align="L", leading=TITLE_LINE, literal=True)
            rows = []
            for text in block["parts"]:
                if rows:
                    rows.append(Glue(3))
                rows.extend(self._source_part(text, LEFT+indent, width, math_markup=block.get("math_markup", False)))
            self.drawn["boxes"] += 1
            return [Frame(style, title, rows, indent, source_id=source_id)]
        role = block.get("role", "paragraph")
        size = 20.66 if role == "title" else HEADING_SIZE.get(role, BODY)
        align = "C" if role == "title" else "L" if role != "paragraph" else "J"
        if role == "paragraph":
            rows = []
            for text in block.get("parts", [block["text"]]):
                rows.extend(self._source_part(text, LEFT, TEXT_W, math_markup=block.get("math_markup", False)))
        else:
            lines = self.canvas.layout(block["text"], TEXT_W, size=size, bold=True,
                                       color=NAVY if role == "title" else BLACK, align=align,
                                       leading=21.3 if role == "title" else LEAD, literal=True)
            rows = self._text_rows(lines, LEFT, TEXT_W, align, kind="heading")
        if rows:
            original = rows[0].draw

            def draw(top):
                original(top)
                self.source_emitted.append(source_id)

            rows[0].draw = draw
        return rows

    def _source_part(self, text: str, x: float, width: float, *, math_markup: bool = False) -> list[Row]:
        match = re.match(r"^([•●▪◦]|\d+[.)])\s+(.+)$", text)
        if not match:
            return self._text_rows(self.canvas.layout(text, width, literal=True, inline_math=math_markup), x, width, "J")
        label, body = match.groups()
        indent = LIST_INDENT if len(label) == 1 else max(NUMBER_INDENT, self.canvas.width("lato", "", BODY, label)+5)
        lines = self.canvas.layout(body, width-indent, literal=True, inline_math=math_markup)
        label_lines = self.canvas.layout(label, indent, align="L", literal=True)

        def prefix(baseline):
            self.canvas.draw_line(label_lines[0], x, baseline, indent, "L")

        return self._text_rows(lines, x+indent, width-indent, "J", prefix=prefix)

    def _text_rows(self, lines: list[Line], x: float, width: float, align: str, *, keep_all: bool = False,
                   kind: str = "line", prefix: Callable[[float], None] | None = None) -> list[Row]:
        """A row per line; a paragraph's first two and last two lines stay together."""
        rows = []
        for n, line in enumerate(lines):
            def draw(top, line=line, first=n == 0):
                if first and prefix:
                    prefix(top + line.ascent)
                self.canvas.draw_line(line, x, top + line.ascent, width, align)
            keep = keep_all or n == 0 or n == len(lines) - 2
            rows.append(Row(line.height, draw, nobreak_after=keep and n < len(lines) - 1, kind=kind))
        if rows and kind == "heading":
            rows[-1].nobreak_after = True
        return rows

    def _heading(self, block: dict) -> list[Row]:
        kind, size = block["kind"], HEADING_SIZE[block["kind"]]
        number = ""
        if kind != "heading":
            if kind == "section":
                self.counts.update(section=self.counts["section"] + 1, subsection=0, subsubsection=0)
                number = str(self.counts["section"])
            elif kind == "subsection":
                self.counts.update(subsection=self.counts["subsection"] + 1, subsubsection=0)
                number = f"{max(1, self.counts['section'])}.{self.counts['subsection']}"
            else:
                self.counts["subsubsection"] += 1
                number = f"{max(1, self.counts['section'])}.{max(1, self.counts['subsection'])}." \
                         f"{self.counts['subsubsection']}"
        indent = self.canvas.width("lato", "B", size, number) + size if number else 0.0
        lines = self.canvas.layout(block["text"], TEXT_W - indent, size=size, bold=True, align="L",
                                   leading=max(LEAD, size * 1.25))

        def prefix(baseline):
            if number:
                self.pdf.set_font("lato", "B", size)
                self.pdf.set_text_color(*BLACK)
                self.pdf.set_xy(LEFT, baseline - 0.3 * size)
                self.pdf.cell(w=indent, h=0, text=number)

        return self._text_rows(lines, LEFT + indent, TEXT_W - indent, "L", keep_all=True, kind="heading",
                               prefix=prefix)

    def _paragraph(self, text: str, x: float, width: float, align: str = "J") -> list[Row]:
        return self._text_rows(self.canvas.layout(text, width, align=align), x, width, align)

    def _math(self, latex: str, x: float, width: float) -> list:
        try:
            made = formulas.render(latex, BODY, display=True)
        except formulas.FormulaError:
            self.canvas.issues.append(f"A formula could not be drawn and is printed as text: {latex[:60]}")
            return self._paragraph(latex, x, width, "C")
        scale = min(1.0, width / made.width)
        w, h = made.width * scale, made.height * scale
        # Placed by its baseline on a strut, as TeX sets a display (mathtext's box is the ink only).
        ascent, depth = max(made.ascent * scale, MATH_STRUT[0]), max(made.depth * scale, MATH_STRUT[1])

        def draw(top):
            self.pdf.image(io.BytesIO(made.png), x + (width - w) / 2, top + MATH_PAD[0] + ascent - made.ascent * scale,
                           w=w, h=h)

        return [Row(MATH_PAD[0] + ascent + depth + MATH_PAD[1], draw, kind="math")]

    def _list(self, block: dict, x: float, width: float) -> list[Row]:
        """Bullets (text 11.8 pt in) or numbers (text 14.4 pt in, numbers right-aligned);
        sub-points start under the point's text: a dash under a bullet, a bullet under a number."""
        rows: list = []
        numbered = block["kind"] == "numbered"
        start = int(block.get("start") or 1)
        widest = self.canvas.width("lato", "", BODY, f"{start + len(block['items']) - 1}.")
        indent = max(NUMBER_INDENT, widest + LABEL_SEP) if numbered else LIST_INDENT

        def marker(label: str, left: float, style: str = "", right: bool = False):
            label_w = self.canvas.width("lato", style, BODY, label)

            def draw(baseline):
                self.pdf.set_font("lato", style, BODY)
                self.pdf.set_text_color(*BLACK)
                self.pdf.set_xy(left - label_w if right else left, baseline - 0.3 * BODY)
                self.pdf.cell(w=label_w, h=0, text=label)
            return draw

        text_x, text_w = x + indent, width - indent
        for n, item in enumerate(block["items"], start=start):
            label = (marker(f"{n}.", text_x - LABEL_SEP, right=True) if numbered else marker("•", x))
            rows += self._text_rows(self.canvas.layout(item["text"], text_w), text_x, text_w, "J", prefix=label)
            subs = item.get("sub") or []
            for sub in subs:
                if sub is subs[0]:
                    rows.append(Glue(SUB_LIST_GAP))
                sub_label = marker("•", text_x) if numbered else marker("–", text_x, "B")
                rows += self._text_rows(self.canvas.layout(sub, text_w - LIST_INDENT), text_x + LIST_INDENT,
                                        text_w - LIST_INDENT, "J", prefix=sub_label)
            if subs and n < start + len(block["items"]) - 1:
                rows.append(Glue(SUB_LIST_GAP))
        return rows

    def _box(self, block: dict) -> Frame:
        style = block["style"]
        frame, bar, ink, body, indent = BOXES[style]
        title = block.get("title") or ""
        if style == "definition":
            self.counts["definition"] += 1
            title = f"Definition {self.counts['definition']}" + (f": {title}" if title else "")
        title_lines = self.canvas.layout(title, TEXT_W - 2 * indent, size=BODY, family="cmss", bold=True, color=ink,
                                         align="L", leading=TITLE_LINE) if title else []
        x, width = LEFT + indent, TEXT_W - 2 * indent
        rows: list = []
        after = 0.0
        for part in block["body"]:
            made = (self._math(part["text"], x, width) if part["kind"] == "math" else
                    self._list(part, x, width) if part["kind"] in ("bullets", "numbered") else
                    self._paragraph(part["text"], x, width))
            before, next_after = SPACE[part["kind"]]
            if rows:
                rows.append(Glue(max(after, before)))
            rows.extend(made)
            after = next_after
        self.drawn["boxes"] += 1
        return Frame(style, title_lines, rows, indent)

    def _table(self, block: dict) -> Table:
        self.counts["table"] += 1
        grid = block["style"] == "grid"
        columns, rows = block["columns"], block["rows"]
        literal = block["kind"] == "source_table"
        pad = 6.0 if not grid else 3.0
        bold = (lambda c, head: head if grid else c == 0)          # noqa: E731
        natural = [0.0] * len(columns)
        least = [0.0] * len(columns)
        space = self.canvas.width("lato", "", BODY, " ")
        for row in [columns, *rows]:
            for c, cell in enumerate(row):
                words = self.canvas.words(cell, bold=bold(c, row is columns), literal=literal)
                total = sum(sum(f.w for f in w) for w in words) + space * max(0, len(words) - 1)
                natural[c] = max(natural[c], total + 2 * pad)
                least[c] = max([least[c], *[sum(f.w for f in w) + 2 * pad for w in words]])
        widths = self._column_widths(natural, least, grid)
        align = ["L"] + (["L"] * (len(columns) - 2) + ["C"] if grid else ["C"] * (len(columns) - 1))
        row_pad = 2.2 if not grid else 0.0

        def make_row(cells, head: bool) -> Row:
            laid = [self.canvas.layout(cell, widths[c] - 2 * pad, bold=bold(c, head),
                                       color=NAVY if (c == 0 and not grid) else BLACK, align=align[c], literal=literal)
                    for c, cell in enumerate(cells)]
            first = max((lines[0].ascent for lines in laid if lines), default=LEAD * 0.732)
            height = max(first - lines[0].ascent + sum(l.height for l in lines) if lines else 0.0
                         for lines in laid) + 2 * row_pad

            def draw(top):
                left = LEFT
                if head:
                    self.pdf.set_fill_color(*(GRID_HEAD if grid else HEAD_ROW))
                    self.pdf.rect(LEFT, top, TEXT_W, height, style="F")
                for c, lines in enumerate(laid):
                    if lines:                       # every cell's first line on one baseline
                        self.canvas.draw_text(lines, left + pad, top + row_pad + first - lines[0].ascent,
                                              widths[c] - 2 * pad, align[c])
                    left += widths[c]
                if grid:
                    self.pdf.set_draw_color(*BLACK)
                    self.pdf.set_line_width(0.4)
                    edge = LEFT
                    for w in [0.0, *widths]:
                        edge += w
                        self.pdf.line(edge, top, edge, top + height)
                    self.pdf.line(LEFT, top + height, RIGHT, top + height)

            return Row(height, draw, kind="table")

        def rule(top: float, where: str) -> float:
            if grid:
                if where == "top":
                    self.pdf.set_draw_color(*BLACK)
                    self.pdf.set_line_width(0.4)
                    self.pdf.line(LEFT, top, RIGHT, top)
                return 0.0
            before, width, after = RULES[where]
            self.pdf.set_draw_color(*BLACK)
            self.pdf.set_line_width(width)
            self.pdf.line(LEFT, top + before + width / 2, RIGHT, top + before + width / 2)
            return before + width + after

        caption = []
        if block.get("title"):
            caption = self._text_rows(self.canvas.layout(f"Table {self.counts['table']}: {block['title']}", TEXT_W,
                                                         align="C"), LEFT, TEXT_W, "C", keep_all=True)
        self.drawn["tables"] += 1
        return Table(make_row(columns, True), [make_row(r, False) for r in rows], caption, rule, block.get("source_id", ""))

    @staticmethod
    def _column_widths(natural: list[float], least: list[float], grid: bool) -> list[float]:
        """Ruled tables: the first column as wide as it needs, the rest equal (the
        samples' tabularx). Grid tables: each column as it needs, the last takes the rest."""
        total = sum(natural)
        if grid and total <= TEXT_W:
            return natural[:-1] + [natural[-1] + TEXT_W - total]
        first = min(natural[0], TEXT_W * 0.34)
        rest = (TEXT_W - first) / max(1, len(natural) - 1)
        if not grid and first >= least[0] and all(l <= rest for l in least[1:]):
            return [first] + [rest] * (len(natural) - 1)
        if total <= TEXT_W:
            return [n * TEXT_W / total for n in natural]
        floor = sum(least)
        if floor >= TEXT_W:
            return [l * TEXT_W / floor for l in least]
        # Too wide: wrap the widest columns first, down to a common cap (never below a column's longest word).
        low, high = 0.0, max(natural)
        for _ in range(40):
            cap = (low + high) / 2
            if sum(min(n, max(cap, l)) for n, l in zip(natural, least)) > TEXT_W:
                high = cap
            else:
                low = cap
        widths = [min(n, max(low, l)) for n, l in zip(natural, least)]
        return [w + (TEXT_W - sum(widths)) * w / sum(widths) for w in widths]

    def _caption_rows(self, text: str) -> tuple[list[Line], float]:
        lines = self.canvas.layout(text, TEXT_W, align="C")
        return lines, sum(l.height for l in lines)

    def _figure(self, block: dict) -> Figure | None:
        path = (self.figures_dir / block["image"]) if (self.figures_dir and block.get("image")) else None
        if not path or not path.is_file():
            return None
        from PIL import Image

        with Image.open(path) as image:
            px_w, px_h = image.size
        self.counts["figure"] += 1
        self.drawn["figures"] += 1
        aspect = px_h / px_w
        width = TEXT_W if px_w / px_h >= 1.55 else TEXT_W * 0.75
        height = width * aspect
        if height > PAGE_H * 0.46:
            height = PAGE_H * 0.46
            width = height / aspect
        lines, caption_h = self._caption_rows(f"Figure {self.counts['figure']}: {block['title']}")
        gap = 9.0

        def draw(top, scale):
            w, h = width * scale, height * scale
            self.pdf.image(str(path), LEFT + (TEXT_W - w) / 2, top, w=w, h=h)
            self.canvas.draw_text(lines, LEFT, top + h + gap, TEXT_W, "C")
            return h + gap + caption_h

        return Figure(draw, height, gap + caption_h)

    def _diagram(self, block: dict) -> list:
        made = (diagrams.tree(self.canvas, block["nodes"], TEXT_W) if block["kind"] == "tree"
                else diagrams.flow(self.canvas, block["items"], TEXT_W, block.get("cycle", False)))
        if made is None:
            self.canvas.issues.append("A diagram was too wide to draw and is written as a list.")
            if block["kind"] == "tree":
                items = [{"text": f"**{n['label']}**" + (f" (under {p['label']})" if p else ""), "sub": []}
                         for n in block["nodes"] for p in [next((m for m in block["nodes"] if m["id"] == n["parent"]), None)]]
            else:
                items = [{"text": step, "sub": []} for step in block["items"]]
            return self._list({"kind": "numbered" if block["kind"] == "flow" else "bullets", "items": items},
                              LEFT, TEXT_W)
        width, height, paint = made
        title = block.get("title") or ""
        lines = self.canvas.layout(title, TEXT_W, bold=True, color=NAVY, align="C") if title else []
        title_h = sum(l.height for l in lines) + (6.0 if lines else 0.0)
        self.drawn["diagrams"] += 1

        def draw(top, scale=1.0):
            self.canvas.draw_text(lines, LEFT, top, TEXT_W, "C")
            paint(LEFT + (TEXT_W - width) / 2, top + title_h)
            return title_h + height

        return [Figure(draw, height, title_h, can_shrink=False)]

    # ---- placing
    def new_page(self, flush: bool = True) -> None:
        """The next page; waiting figures go at its top (not while a box or table continues)."""
        if self.pdf.page:
            self.fills.append((self.y - TOP) / (BOTTOM - TOP))
        self.pdf.add_page()
        self.y = TOP
        while flush and self.waiting:
            figure = self.waiting[0]
            if self.y > TOP + 0.01 and self.y + figure.height > BOTTOM:
                break
            self.y += self.waiting.pop(0).draw(self.y, figure.scale_for(BOTTOM - self.y))
            self.y = min(BOTTOM, self.y + SPACE["figure"][1])

    def room(self) -> float:
        return BOTTOM - self.y

    def lead(self, items: list, i: int) -> float:
        """How much must fit with item i: it, and what it is kept with."""
        total = 0.0
        while i < len(items):
            item = items[i]
            if isinstance(item, Glue):
                total += item.height
                i += 1
                continue
            if isinstance(item, Row):
                total += item.height
                if not item.nobreak_after:
                    return total
                i += 1
                continue
            if isinstance(item, Frame):
                if item.height() <= SMALL_BOX:
                    return total + item.height()
                return total + min(item.height(), item.title_height() + BOX_PAD_TOP + self._first_rows(item.rows))
            if isinstance(item, Table):
                return total + item.head.height + (item.rows[0].height if item.rows else 0) + 6
            if isinstance(item, Figure):
                return total + item.extra + item.picture * (FIGURE_SHRINK if item.can_shrink else 1.0)
            return total
        return total

    @staticmethod
    def _first_rows(rows: list) -> float:
        """The first rows of a box that must stay with its title (to the first allowed break, at least two lines)."""
        total, lines = 0.0, 0
        for row in rows:
            total += row.height
            if isinstance(row, Row):
                lines += 1
                if lines >= 2 and not row.nobreak_after:
                    break
        return total

    def render(self) -> bytes:
        items = self.build()
        self.new_page()
        last_glue: tuple[Glue, float] | None = None         # the space just added, and how much of it
        moved_away = None                                   # the space before a figure that went to wait
        for i, item in enumerate(items):
            if isinstance(item, Glue):
                if moved_away is not None:                  # the figure is gone: one space, as if it never was
                    kinds = (moved_away.kinds[0], item.kinds[1])
                    item = Glue(gap(moved_away.after, item.before, kinds), moved_away.after, item.before, kinds)
                    moved_away = None
                added = min(item.height, BOTTOM - self.y) if self.y > TOP + 0.01 else 0.0
                self.y += max(0.0, added)
                last_glue = (item, max(0.0, added))
                continue
            if isinstance(item, Row):
                if self.y + self.lead(items, i) > BOTTOM + 0.01 and self.y > TOP + 0.01:
                    self.new_page()
                item.draw(self.y)
                self.y += item.height
            elif isinstance(item, Frame):
                self._place_frame(item, items, i)
            elif isinstance(item, Table):
                self._place_table(item, items, i)
            elif isinstance(item, Figure):
                if not self._place_figure(item) and last_glue:
                    self.y -= last_glue[1]
                    moved_away = last_glue[0]
            last_glue = None
            self._fill_waiting()
        while self.waiting:
            self.new_page()
        self.fills.append((self.y - TOP) / (BOTTOM - TOP))
        from fpdf.output import OutputProducer

        class StableOutput(OutputProducer):
            def _add_info(self):
                info = super()._add_info()
                # Omit the optional clock metadata without invalidating FPDF's
                # internal date object. Unchanged renders reuse paid reviews.
                info.creation_date = None
                return info
        return bytes(self.pdf.output(output_producer_class=StableOutput))

    def _fill_waiting(self) -> None:
        """A figure that waited goes in at the first place on a later page where it fits."""
        while self.waiting and self.pdf.page > self.waiting[0].page:
            figure = self.waiting[0]
            above = SPACE["figure"][0] if self.y > TOP + 0.01 else 0.0
            if self.y + above + figure.height > BOTTOM:
                return
            self.y += above
            self.y += self.waiting.pop(0).draw(self.y, 1.0)
            self.y = min(BOTTOM, self.y + SPACE["figure"][1])

    def _place_figure(self, figure: Figure) -> bool:
        """Draw it here, a little smaller, or at the top of the next page. False: it waits."""
        if self.waiting:                                    # keep the figures' order
            figure.page = self.pdf.page
            self.waiting.append(figure)
            return False
        if self.y + figure.height <= BOTTOM:
            self.y += figure.draw(self.y, 1.0)
            return True
        if figure.can_shrink and figure.scale_for(self.room()) >= FIGURE_SHRINK:
            self.y += figure.draw(self.y, figure.scale_for(self.room()))
            return True
        if self.y <= TOP + 0.01 or self.room() < (BOTTOM - TOP) * 0.25:
            self.new_page()
            self.y += figure.draw(self.y, figure.scale_for(BOTTOM - self.y))
            return True
        figure.page = self.pdf.page
        self.waiting.append(figure)                         # the text after it fills this page
        return False

    def _place_frame(self, frame: Frame, items: list, i: int) -> None:
        start, first = 0, True
        page = BOTTOM - TOP
        while True:
            whole = frame.height(start, None, first)
            if self.y + whole <= BOTTOM + 0.01:
                self._draw_frame(frame, start, len(frame.rows), first, True)
                return
            fresh = self.y <= TOP + 0.01
            if not fresh and first and whole <= page and (whole <= SMALL_BOX or self.room() < whole * 0.4):
                self.new_page()
                continue
            end = self._split(frame, start, first)
            if end is None:
                if not fresh:
                    self.new_page(flush=first)
                    continue
                end = self._split(frame, start, first, forced=True)
            self._draw_frame(frame, start, end, first, False)
            self.new_page(flush=False)
            start, first = end, False

    def _split(self, frame: Frame, start: int, first: bool, forced: bool = False) -> int | None:
        """The last row index that ends a part fitting this page: a break allowed
        there, two lines at least on each side."""
        rows = frame.rows
        lines_after = [0] * (len(rows) + 1)
        for n in range(len(rows) - 1, -1, -1):
            lines_after[n] = lines_after[n + 1] + isinstance(rows[n], Row)
        best = None
        for end in range(start + 1, len(rows)):
            if frame.height(start, end, first) - BOX_PAD_BOTTOM > self.room():
                break
            last = rows[end - 1]
            if isinstance(last, Row) and last.nobreak_after and not forced:
                continue
            shown = sum(isinstance(r, Row) for r in rows[start:end])
            if forced or (shown >= 2 and lines_after[end] >= 2):
                best = end
        if best is None and forced:
            best = start + 1
        return best

    def _draw_frame(self, frame: Frame, start: int, end: int, first: bool, last: bool) -> None:
        if first and frame.source_id:
            self.source_emitted.append(frame.source_id)
        colour, bar, _ink, body, indent = BOXES[frame.style]
        rows = _trim(frame.rows[start:end])
        height = frame.height(start, end, first)
        if not last:
            height = BOTTOM - self.y
        pdf, top = self.pdf, self.y
        corners = (("TOP_LEFT", "TOP_RIGHT") if first else ()) + (("BOTTOM_LEFT", "BOTTOM_RIGHT") if last else ())
        pdf.set_fill_color(*colour)
        pdf.rect(LEFT, top, TEXT_W, height, style="F", round_corners=corners or False, corner_radius=ARC)
        inner_top = top + (FRAME if first else 0.0)
        bottom = top + height - (FRAME if last else 0.0)
        if first and frame.title:
            pdf.set_fill_color(*bar)
            pdf.rect(LEFT + FRAME, inner_top, TEXT_W - 2 * FRAME, frame.bar_height(), style="F",
                     round_corners=("TOP_LEFT", "TOP_RIGHT"), corner_radius=ARC)
            baseline = inner_top + 10.85
            for line in frame.title:
                self.canvas.draw_line(line, LEFT + indent, baseline, TEXT_W - 2 * indent, "L")
                baseline += TITLE_LINE
            inner_top += frame.bar_height() + FRAME
        body_corners = (("TOP_LEFT", "TOP_RIGHT") if first and not frame.title else ()) + \
                       (("BOTTOM_LEFT", "BOTTOM_RIGHT") if last else ())
        pdf.set_fill_color(*body)
        pdf.rect(LEFT + FRAME, inner_top, TEXT_W - 2 * FRAME, bottom - inner_top, style="F",
                 round_corners=body_corners or False, corner_radius=ARC)
        y = inner_top + BOX_PAD_TOP
        for row in rows:
            if isinstance(row, Row):
                row.draw(y)
            y += row.height
        self.y = top + height

    def _place_table(self, table: Table, items: list, i: int) -> None:
        """Rows split across pages between rows, the header repeated on each part."""
        if table.source_id:
            self.source_emitted.append(table.source_id)
        rows, n = table.rows, 0
        caption_h = _sum(table.caption) + (6.0 if table.caption else 0.0)
        head_h = sum(RULES["top"]) + table.head.height + sum(RULES["mid"])
        bottom_rule = sum(RULES["bottom"])
        while True:
            first_row = rows[n].height if n < len(rows) else 0.0
            if self.y + head_h + first_row > BOTTOM and self.y > TOP + 0.01:
                self.new_page(flush=n == 0)
            self.y += table.rule(self.y, "top")
            table.head.draw(self.y)
            self.y += table.head.height
            self.y += table.rule(self.y, "mid")
            placed = 0
            while n < len(rows):
                height = rows[n].height
                # the last row takes the caption with it to the next page
                with_caption = height + bottom_rule + (caption_h if n == len(rows) - 1 else 0.0)
                if placed and self.y + with_caption > BOTTOM:
                    break
                rows[n].draw(self.y)
                self.y += height
                n, placed = n + 1, placed + 1
            self.y += table.rule(self.y, "bottom")
            if n >= len(rows):
                break
            self.new_page(flush=False)
        if table.caption:
            self.y += 6.0
            if self.y + caption_h > BOTTOM + 0.01:
                self.new_page(flush=False)
            for row in table.caption:
                row.draw(self.y)
                self.y += row.height


def render_pdf(doc: dict, branding: Branding, figures_dir: Path | None = None) -> tuple[bytes, dict]:
    """The PDF of one document, and what was drawn (pages, how full each is, issues)."""
    renderer = Renderer(doc, branding, figures_dir)
    data = renderer.render()
    return data, {"pages": renderer.pdf.page, "fills": [round(f, 3) for f in renderer.fills],
                  "issues": list(dict.fromkeys(renderer.canvas.issues)),
                  "source_emitted": renderer.source_emitted, **renderer.drawn}
