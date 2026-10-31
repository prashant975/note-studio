"""Render structured short notes into a handwritten-style PDF (fpdf2).

Body text uses the Patrick Hand handwriting font. Any text wrapped in $...$ is a
LaTeX equation, rendered to an image by math_render and embedded inline.
Sections can carry bullets, a TABLE (comparisons/trends) and/or a FLOW CHART
(process steps in boxes joined by arrows).
"""
from __future__ import annotations

import io
import json
import math
import os
import re
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from fpdf import FPDF
from fpdf.enums import MethodReturnValue, XPos, YPos

from . import config, graph_render, math_render
from .errors import VisualFidelityError
from .prompts import ShortNotes
from .visual_manifest import (
    RenderedVisual,
    SourceVisual,
    VisualAsset,
    VisualManifest,
    VisualMemoryBudget,
    VisualRunBudget,
    manifest_ids,
)

try:  # doodle icon library (optional — renderer works without it)
    from . import doodles
except Exception:  # noqa: BLE001
    doodles = None


def _emph_runs(text: str) -> "list[tuple[str, bool]]":
    """Split text into (segment, is_emphasised) runs on **markers**."""
    runs: list[tuple[str, bool]] = []
    i = 0
    for m in _EMPH_RE.finditer(text):
        if m.start() > i:
            runs.append((text[i:m.start()], False))
        runs.append((m.group(1), True))
        i = m.end()
    if i < len(text):
        runs.append((text[i:], False))
    runs = runs or [(text, False)]
    # An unpaired ** (odd marker count, e.g. a highlight broken across a
    # wrapped line) must never print as literal asterisks.
    return [(segment.replace("**", ""), emphasised) for segment, emphasised in runs]

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
LOGO_WATERMARK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "assets",
    "watermarks",
    "center-logo.png",
)
PT_TO_MM = 0.3527996


@dataclass(frozen=True)
class _PreparedRenderAsset:
    """Pre-page validated immutable input for one terminal PDF slot."""

    visual_id: str
    primary_png: bytes | None
    fallback_png: bytes
    width_px: int
    height_px: int

# Glyphs Patrick Hand lacks -> safe ASCII (plain text only; maths handled separately).
_REPLACEMENTS = {
    "→": "->", "➜": "->", "⟶": "->", "←": "<-", "↔": "<->",
    "⇌": "<=>", "⇋": "<=>", "⇄": "<=>", "⇒": "=>",
    "×": "x", "·": ".", "∙": ".", "•": "-", "∘": "°",
    "Δ": "delta ", "δ": "delta ", "∆": "delta ",
    "“": '"', "”": '"', "‘": "'", "’": "'", "≈": "~", "≤": "<=", "≥": ">=",
    "≠": "!=", "≡": "=", "∝": " prop to ", "∞": "infinity", "√": "sqrt",
    "±": "+/-", "∑": "sum", "∫": "integral", "∴": "therefore", "∵": "because",
    "−": "-", "α": "alpha", "β": "beta", "γ": "gamma", "θ": "theta",
    "λ": "lambda", "μ": "mu", "π": "pi", "σ": "sigma", "ω": "omega", "Ω": "ohm",
}
# Unicode sub/superscripts -> inline markers so the text path renders them as
# real sub/superscripts in the handwriting font.
_SUBS = {"₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4", "₅": "_5",
         "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9", "₊": "_+", "₋": "_-"}
_SUPS = {"⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4", "⁵": "^5",
         "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9", "⁺": "^+", "⁻": "^-"}
_MATH_RE = re.compile(r"\$([^$]+)\$")
_MARKER_RE = re.compile(r"[_^]\{([^}]*)\}")
# A subscript/superscript piece: braced (_{..}) or single char (_x / ^-).
_PIECE_RE = re.compile(r"([_^])(?:\{([^}]*)\}|([A-Za-z0-9+\-=]))")
_SCRIPT_SCALE = 0.72  # sub/superscript font is 72% of body size

INK = (15, 15, 15)
HEAD_BLUE = (20, 45, 95)
LINE_GRAY = (190, 200, 220)
PAGE_CREAM = (253, 251, 245)     # notebook-paper page background
EMPH = (198, 28, 70)             # **key term** colour (marker red-pink)
TITLE_INK = (34, 34, 44)         # near-black marker title
SUBTITLE_GREEN = (27, 122, 74)   # "(Full Notes ...)" style subtitle
_EMPH_RE = re.compile(r"\*\*(.+?)\*\*")
# Trick / star-point highlight colours.
TRICK_INK = (146, 64, 6)        # deep amber-brown text
TRICK_FILL = (255, 248, 216)    # soft yellow highlight
TRICK_EDGE = (238, 201, 120)    # amber border
STAR_GOLD = (232, 152, 22)      # star colour

PAGE_MARGIN = 13.0               # tighter margins: more room for content
COL_GUTTER = 6.0                 # space between the two text columns
QUOTE_BUBBLE_W = 46.0            # top-right speech bubble width (mm)
DOT_GRID = (223, 227, 235)       # dotted-grid paper dots
BOX_INK = (28, 28, 34)           # thin black outline on headings/boxes
FORMULA_FILL = (255, 252, 232)   # faint yellow behind formulas
NOTE_FILL = (244, 245, 248)      # sticky-note grey
NOTE_EDGE = (176, 182, 196)
# Soft pastel fills for section heading boxes (cycled): pink, lavender,
# baby blue, mint, peach, sky.
SECTION_PASTELS = [
    ((252, 226, 236), (150, 30, 76)),
    ((233, 226, 250), (78, 48, 132)),
    ((222, 238, 252), (22, 66, 122)),
    ((225, 246, 233), (18, 96, 56)),
    ((253, 238, 220), (140, 78, 14)),
    ((226, 244, 246), (14, 96, 108)),
]

# Mind-map branch palette: (edge, fill, ink) per branch, cycled.
MINDMAP_PALETTE = [
    ((34, 69, 156), (232, 238, 250), (24, 48, 110)),    # blue
    ((185, 29, 71), (250, 231, 236), (140, 20, 52)),    # crimson
    ((26, 122, 60), (228, 244, 233), (18, 86, 42)),     # green
    ((204, 124, 10), (253, 241, 220), (150, 90, 8)),    # amber
    ((106, 52, 158), (240, 232, 249), (78, 38, 116)),   # purple
    ((12, 124, 134), (224, 243, 244), (8, 90, 98)),     # teal
]


def _sanitize_text(text: str) -> str:
    """Make a plain-text run safe for Patrick Hand, KEEPING _/^ sub/sup markers."""
    if not text:
        return ""
    # LaTeX commands that sometimes leak OUTSIDE $...$ -> readable handwriting.
    text = re.sub(r"\\?xrightarrow\s*\{([^}]*)\}", r" ->[\1] ", text)
    # "xrightarrow"/"rightarrow" may be bare; "\to" needs the backslash so we
    # never touch the ordinary English word "to".
    text = re.sub(r"(?<![A-Za-z])\\?(?:xrightarrow|rightarrow)(?![A-Za-z])",
                  " -> ", text)
    text = re.sub(r"\\to(?![A-Za-z])", " -> ", text)
    text = re.sub(r"\\?rightleftharpoons", " <=> ", text)
    text = text.replace("`", "")  # strip markdown code backticks around formulas
    for bad, good in _REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = "".join(_SUBS.get(c, _SUPS.get(c, c)) for c in text)
    return "".join(
        c for c in text if c == "\n" or c in "_^{}" or ord(c) < 0x250 or c in "–—…"
    )


def _word_pieces(word: str) -> "list[tuple[str, str]]":
    """Split a word into (text, mode) pieces; mode in {'n','sub','sup'}."""
    pieces: list[tuple[str, str]] = []
    i = 0
    for m in _PIECE_RE.finditer(word):
        if m.start() > i:
            pieces.append((word[i:m.start()], "n"))
        content = m.group(2) if m.group(2) is not None else m.group(3)
        pieces.append((content, "sub" if m.group(1) == "_" else "sup"))
        i = m.end()
    if i < len(word):
        pieces.append((word[i:], "n"))
    return pieces or [(word, "n")]


def _plain(text: str) -> str:
    """Sanitize, strip $...$, and FLATTEN sub/sup markers (header/caption use)."""
    from .latex_repair import normalize_math_delimiters, normalize_science_notation
    text = normalize_science_notation(normalize_math_delimiters(text or ""))
    s = _sanitize_text(_MATH_RE.sub(lambda m: _latex_fallback(m[1]), text))
    s = _MARKER_RE.sub(r"\1", s)
    s = re.sub(r"[_^]([A-Za-z0-9+\-=])", r"\1", s)
    return s.strip()


def _split_math(text: str) -> "list[tuple[str, str]]":
    from .latex_repair import normalize_math_delimiters, normalize_science_notation
    text = normalize_science_notation(normalize_math_delimiters(text))
    out: list[tuple[str, str]] = []
    i = 0
    for m in _MATH_RE.finditer(text):
        if m.start() > i:
            out.append(("text", text[i:m.start()]))
        out.append(("math", m.group(1)))
        i = m.end()
    if i < len(text):
        out.append(("text", text[i:]))
    return out


def _latex_fallback(latex: str) -> str:
    s = math_render.normalize_latex(latex)
    # Keep the contents of text wrappers, never their command names. Repeated
    # passes handle nested wrappers without destroying superscript grouping.
    wrapper = re.compile(r"\\(?:mathrm|mathbf|mathit|text(?:rm|bf|it)?)\{([^{}]*)\}")
    while wrapper.search(s):
        s = wrapper.sub(lambda m: m[1], s)
    s = re.sub(r"\^\s*(?:\{\s*\\circ\s*\}|\\circ)", "°", s)
    s = s.replace(r"\circ", "°").replace(r"\%", "%")
    s = s.replace(r"\geq", ">=").replace(r"\leq", "<=")
    s = re.sub(r"\\[ ,;:!]", " ", s)
    s = re.sub(r"\\sqrt\{([^}]*)\}", r"sqrt(\1)", s)
    s = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"(\1)/(\2)", s)
    s = (s.replace("\\cdot", "*").replace("\\times", "x")
           .replace("\\rightarrow", "->").replace("\\to", "->")
           .replace("\\pi", "pi").replace("\\Delta", "delta").replace("\\,", " "))
    s = re.sub(r"[{}\\]", "", s)
    return re.sub(r",(?=\S)", ", ", s).strip()


def rendered_notation_findings(pdf: bytes) -> list[tuple[int, str]]:
    """Check every page for command debris in the actual delivered text."""
    from pypdf import PdfReader
    from .latex_repair import notation_artifacts
    return [
        (number, artifact)
        for number, page in enumerate(PdfReader(io.BytesIO(pdf)).pages, 1)
        for artifact in notation_artifacts(page.extract_text() or "")
    ]


class NotesPDF(FPDF):
    CARD_GUTTER = 6.0
    CARD_MIN_WIDTH = 60.0
    # A card may start at a page end and continue overleaf once this much of
    # it (heading plus about two lines) fits on the current page.
    CARD_MIN_LEAD = 24.0
    # Glossary grid: gap between term columns and the narrowest term column.
    KEY_TERM_COL_GAP = 4.0
    KEY_TERM_MIN_COL_W = 38.0
    # Mind map: centre node width, gap either side of it, gap between branch
    # boxes, and the font sizes tried (largest first) to keep it on one page.
    MINDMAP_CENTER_W = 38.0
    MINDMAP_GAP_X = 9.0
    MINDMAP_VGAP = 6.0
    MINDMAP_SCALES = (1.0, 0.94, 0.88)
    VISUAL_MIN_READABLE_WIDTH_RATIO = 0.70

    def __init__(self, notes: ShortNotes, signature: str,
                 watermark: bool = True, logo_watermark: bool = False,
                 show_page_numbers: bool = True):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.notes = notes
        self.show_page_numbers = show_page_numbers
        self.language = getattr(notes, "output_language", "")
        from .sanskrit import require_sanskrit, sanskrit_signature
        require_sanskrit(notes)
        self.signature = (_plain(sanskrit_signature(signature)) if self.language == "sanskrit"
                          else _plain(signature) or "by PW Faculty")
        if self.language == "sanskrit":
            logo_watermark = False
        self.watermark_on = watermark
        self.logo_watermark_on = logo_watermark
        self._logo_watermark_png = self._load_logo_watermark() if logo_watermark else None
        self.set_auto_page_break(True, margin=16)
        self.set_margins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        # Two-column flow: content fills the left column, then the right one,
        # then the next page — the dense studygram look.
        self._col = 0
        self._body_top = 30.0
        self._full_width = True
        self._col_y = [30.0, 30.0]   # how far each column has been filled
        self._in_band = False        # inside a full-width (wide) section
        self._band_frame_top = None  # current page's dotted-frame start
        # A responsive card owns its region even when one of its internal
        # widgets reaches a page boundary.  This prevents the old column
        # paginator from leaking into independent-card rendering.
        self._responsive_card = None
        self._responsive_frame_top = None
        self._responsive_frame_min_bottom = None
        self._card_first_break_y = None
        self.add_font("Patrick", "", os.path.join(FONT_DIR, "PatrickHand-Regular.ttf"))
        self._section_no = 0
        self._sec_edge = (70, 90, 140)   # current section accent (bullet dots)
        self._sec_ink = HEAD_BLUE
        self._layout_preflight_depth = 0
        self._rendered_visuals: list[RenderedVisual] = []
        self._visual_manifest: VisualManifest | None = None
        self._visual_run_budget: VisualRunBudget | None = None
        self._visual_assets = MappingProxyType(OrderedDict())
        self._visual_records = MappingProxyType(OrderedDict())
        self._visual_by_id = MappingProxyType(OrderedDict())
        self._initialize_visual_runtime()

    def set_font(self, family=None, style="", size=0):
        if (getattr(self, "language", "") == "sanskrit" and family
                and family.lower() == "patrick" and "kalam" in self.fonts):
            family = "Kalam"
        return super().set_font(family, style, size)

    def _check_visual_deadline(self) -> None:
        if self._visual_run_budget is not None:
            self._visual_run_budget.check_deadline()

    @staticmethod
    def _positive_dimension(value: object) -> bool:
        return type(value) is int and value > 0

    @staticmethod
    def _decode_visual_png(
        png: bytes,
        width_px: int,
        height_px: int,
        error_code: str,
    ) -> None:
        """Fully decode one exact PNG and enforce metadata plus configured caps."""
        if (
            type(png) is not bytes
            or not png
            or len(png) > config.MAX_VISUAL_PNG_BYTES
            or not NotesPDF._positive_dimension(width_px)
            or not NotesPDF._positive_dimension(height_px)
            or width_px * height_px > config.MAX_VISUAL_CROP_PIXELS
        ):
            raise VisualFidelityError(error_code)
        try:
            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(png)) as image:
                if image.format != "PNG":
                    raise ValueError("not png")
                actual_size = image.size
                try:
                    actual_width, actual_height = actual_size
                except (TypeError, ValueError):
                    raise ValueError("invalid png dimensions") from None
                if (
                    not NotesPDF._positive_dimension(actual_width)
                    or not NotesPDF._positive_dimension(actual_height)
                    or actual_size != (width_px, height_px)
                    or actual_width * actual_height
                    > config.MAX_VISUAL_CROP_PIXELS
                ):
                    raise ValueError("invalid png dimensions")
                image.load()
        except VisualFidelityError:
            raise
        except Exception:
            raise VisualFidelityError(error_code) from None

    def _initialize_visual_runtime(self) -> None:
        """Snapshot and validate terminal manifest assets before page 1 exists."""
        runtime_names = (
            "_visual_manifest",
            "_source_crops",
            "_visual_assets",
            "_visual_run_budget",
            "_visual_memory_budget",
        )
        present = tuple(name in self.notes.__dict__ for name in runtime_names)
        linked_sections = any(
            bool(getattr(section, "visual_ids", ()))
            for section in self.notes.sections
        )
        if not any(present) and not linked_sections:
            return
        if not all(present):
            raise VisualFidelityError("link_invalid")

        manifest = getattr(self.notes, "_visual_manifest", None)
        supplied_assets = getattr(self.notes, "_visual_assets", None)
        budget = getattr(self.notes, "_visual_run_budget", None)
        memory = getattr(self.notes, "_visual_memory_budget", None)
        source_crops = getattr(self.notes, "_source_crops", None)
        if (
            not isinstance(manifest, VisualManifest)
            or not isinstance(supplied_assets, Mapping)
            or not isinstance(budget, VisualRunBudget)
            or not isinstance(memory, VisualMemoryBudget)
            or not isinstance(source_crops, Mapping)
        ):
            raise VisualFidelityError("link_invalid")

        expected = manifest_ids(manifest)
        if len(set(expected)) != len(expected):
            raise VisualFidelityError("link_invalid")
        try:
            supplied_keys = tuple(supplied_assets.keys())
        except Exception:
            raise VisualFidelityError("link_invalid") from None
        if supplied_keys != expected:
            raise VisualFidelityError("link_invalid")

        assets: OrderedDict[str, VisualAsset] = OrderedDict()
        records: OrderedDict[str, _PreparedRenderAsset] = OrderedDict()
        visuals: OrderedDict[str, SourceVisual] = OrderedDict()
        for visual in manifest.visuals:
            if not isinstance(visual, SourceVisual):
                raise VisualFidelityError("link_invalid")
            visuals[visual.visual_id] = visual

        for visual_id in expected:
            budget.check_deadline()
            try:
                asset = supplied_assets[visual_id]
            except Exception:
                raise VisualFidelityError("link_invalid") from None
            if (
                not isinstance(asset, VisualAsset)
                or type(visual_id) is not str
                or asset.visual_id != visual_id
                or asset.state not in (
                    "verified_redraw",
                    "sketch_source_fallback",
                )
                or not self._positive_dimension(asset.width_px)
                or not self._positive_dimension(asset.height_px)
                or type(asset.primary_png) is not bytes
                or type(asset.fallback_png) is not bytes
            ):
                raise VisualFidelityError("link_invalid")

            self._decode_visual_png(
                asset.fallback_png,
                asset.width_px,
                asset.height_px,
                "fallback_damaged",
            )
            budget.check_deadline()
            primary_png: bytes | None = None
            if asset.state == "verified_redraw":
                budget.check_deadline()
                try:
                    self._decode_visual_png(
                        asset.primary_png,
                        asset.width_px,
                        asset.height_px,
                        "render_failed",
                    )
                except VisualFidelityError as error:
                    if error.code == "deadline_exceeded":
                        raise
                else:
                    primary_png = asset.primary_png
                budget.check_deadline()
            assets[visual_id] = asset
            records[visual_id] = _PreparedRenderAsset(
                visual_id=visual_id,
                primary_png=primary_png,
                fallback_png=asset.fallback_png,
                width_px=asset.width_px,
                height_px=asset.height_px,
            )
            budget.check_deadline()

        self._visual_manifest = manifest
        self._visual_run_budget = budget
        self._visual_assets = MappingProxyType(assets)
        self._visual_records = MappingProxyType(records)
        self._visual_by_id = MappingProxyType(visuals)

    # ---------------- two-column flow ----------------
    @property
    def _col_w(self) -> float:
        return (self.w - 2 * PAGE_MARGIN - COL_GUTTER) / 2

    def _use_full_width(self) -> None:
        """Headers, mind map and other page-wide blocks."""
        self._full_width = True
        self.set_margins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        self.set_x(PAGE_MARGIN)

    def _use_column(self, i: int) -> None:
        """Point l_margin/epw at column i, so every renderer stays inside it."""
        self._full_width = False
        self._col = i
        left = PAGE_MARGIN + i * (self._col_w + COL_GUTTER)
        self.set_margins(left, PAGE_MARGIN, self.w - left - self._col_w)
        self.set_xy(left, self._body_top if i else self.get_y())

    def _use_region(self, x: float, width: float) -> None:
        """Constrain the next card to an independent horizontal region."""
        self._full_width = abs(width - self.epw) < 0.01 and abs(x - PAGE_MARGIN) < 0.01
        self._col = 0
        self.set_margins(x, PAGE_MARGIN, self.w - x - width)
        self.set_x(x)

    def _note_card_break(self) -> None:
        """Remember where the current card's content first reached a page end."""
        if self._card_first_break_y is None:
            self._card_first_break_y = self.get_y()

    def _break(self) -> None:
        """Content ran out of room: move to the next column, else next page."""
        if self._responsive_card is not None:
            self._note_card_break()
            self._close_responsive_frame(self.get_y() + 1.2)
            self.add_page()
            return
        if self._full_width or self._col == 1:
            if self._in_band:
                self._close_band_frame()
            self.add_page()          # header() resets us to column 0
        else:
            self._col_y[0] = self.get_y()
            self._use_column(1)
            self.set_y(self._body_top)

    @property
    def accept_page_break(self) -> bool:
        if not self.auto_page_break:
            return False
        # fpdf's own auto-break must also flow into column 2 first.
        if self._responsive_card is not None:
            self._note_card_break()
            self._close_responsive_frame(self.get_y() + 1.2)
            return True
        if self._full_width or self._col == 1:
            if self._in_band:
                self._close_band_frame()
            return True
        self._col_y[0] = self.get_y()
        self._use_column(1)
        self.set_y(self._body_top)
        return False

    # ---------------- full-width band (wide sections) ----------------
    def _band_start(self) -> float:
        """Leave the columns and continue across the whole page width."""
        ys = list(self._col_y)
        ys[self._col] = self.get_y()
        top = max(ys)
        self._in_band = True
        self._use_full_width()
        if top + 40 > self.page_break_trigger:   # no useful room left
            self.add_page()
            top = self.get_y()
        self.set_y(top)
        return top

    def _band_end(self) -> None:
        """Back to two columns, both starting under the band."""
        bottom = self.get_y()
        self._in_band = False
        self._band_frame_top = None
        self._col_y = [bottom, bottom]
        self._use_column(0)
        self.set_y(bottom)

    # ---------------- page furniture ----------------
    def header(self) -> None:
        # Dotted-grid paper (bullet-journal look), drawn under everything.
        self.set_fill_color(*PAGE_CREAM)
        self.rect(0, 0, self.w, self.h, style="F")
        self._dot_grid()
        self._use_full_width()
        self._logo_watermark()
        self._watermark()
        if self.show_page_numbers:
            self._page_badge()
        if self.page_no() == 1:
            self._full_header()
        else:
            self._slim_header()
        # Everything after the header flows in two columns.
        self._body_top = self.get_y()
        self._col_y = [self._body_top, self._body_top]
        if self._responsive_card is not None:
            x, width = self._responsive_card
            self._use_region(x, width)
            self.set_y(self._body_top)
            self._responsive_frame_top = self._body_top
            self._responsive_frame_min_bottom = None
        elif self._in_band:
            self._use_full_width()   # a wide section continues on the new page
            self.set_y(self._body_top)
            if self._band_frame_top is None:
                self._band_frame_top = self._body_top
        else:
            self._use_column(0)
            self.set_y(self._body_top)

    def _dot_grid(self, step: float = 5.0, r: float = 0.22) -> None:
        """Faint dotted grid, like bullet-journal paper."""
        self.set_fill_color(*DOT_GRID)
        y = step
        while y < self.h - 2:
            x = step
            while x < self.w - 2:
                self.ellipse(x - r, y - r, 2 * r, 2 * r, style="F")
                x += step
            y += step

    def _page_badge(self) -> None:
        """"Page N" written inside a small ribbon banner, top-left."""
        label = f"Page {self.page_no()}"
        self.set_font("Patrick", "", 11.5)
        w = self.get_string_width(label) + 7.0
        x, y, h = self.l_margin - 6, 5.6, 7.2
        self.set_draw_color(*BOX_INK)
        self.set_line_width(0.4)
        self.set_fill_color(233, 226, 250)  # lavender ribbon
        self.polygon([(x, y), (x + w, y), (x + w - 3.0, y + h / 2),
                      (x + w, y + h), (x, y + h), (x + 3.0, y + h / 2)],
                     style="DF")
        self.set_text_color(70, 52, 110)
        self.set_xy(x, y + 0.4)
        self.cell(w, h - 0.8, label, align="C")
        self.set_text_color(0, 0, 0)

    def _watermark(self) -> None:
        if not getattr(self, "watermark_on", True):
            return
        self.set_font("Patrick", "", 20)
        self.set_text_color(233, 233, 236)
        with self.rotation(45, self.w / 2, self.h / 2):
            y = -30.0
            while y < self.h + 40:
                x = -40.0
                while x < self.w + 40:
                    if self.language == "sanskrit":
                        self._draw_word(x, y, [(self.signature, "n")], 20)
                    else:
                        self.text(x, y, self.signature)
                    x += 80.0
                y += 46.0
        self.set_text_color(0, 0, 0)

    def _full_header(self) -> None:
        n = self.notes
        quote = _plain(getattr(n, "quote", "") or "")
        title = _plain(n.title).upper()
        subject = _plain(n.subject)
        audience = _plain(getattr(n, "audience", "") or "")

        # Measure/draw the cloud first, then place every later row below its
        # real footprint. It owns only the right gutter.
        quote_bounds = self._quote_bubble(quote) if quote else None

        # The quote bubble owns the top-right corner, so the title gets a
        # narrower, still-centred column (inset symmetrically) and shrinks
        # until it fits — long lecture titles must never run under the bubble.
        # The title stays centred on the PAGE. Both sides reserve an equal
        # zone: the quote cloud on the right, a sticker doodle on the left.
        side = (QUOTE_BUBBLE_W + 7.0) if quote else 31.0
        centre_x = self.l_margin + side
        centre_w = self.epw - 2 * side
        chip_pad_x = 5.0
        title_x = centre_x + chip_pad_x
        avail = centre_w - 2 * chip_pad_x
        by = 12.4
        bh = 0.0
        central_bottom = by
        if title:
            size = 29.0
            self.set_text_color(*TITLE_INK)
            while size > 17.0:
                self.set_font("Patrick", "", size)
                lines = self.multi_cell(
                    avail, size * 0.38, title, align="C", dry_run=True,
                    output=MethodReturnValue.LINES,
                )
                if len(lines) <= 2:
                    break
                size -= 1.5
            self.set_font("Patrick", "", size)
            line_h = size * 0.38
            lines = self.multi_cell(
                avail, line_h, title, align="C", dry_run=True,
                output=MethodReturnValue.LINES,
            )
            # fpdf2's dry-run wrapper is the source of truth: the title chip is
            # based on those exact final-size lines, never a hand-written estimate.
            tw = min(
                max((self.get_string_width(line) for line in lines), default=0.0),
                avail,
            )
            th = max(1, len(lines)) * line_h
            chip_w = tw + 2 * chip_pad_x
            bx = centre_x + (centre_w - chip_w) / 2
            bh = th + 3.6
            self._title_chip_bounds = (bx, by, chip_w, bh)
            self.set_fill_color(255, 255, 255)
            self.set_draw_color(*BOX_INK)
            self.set_line_width(0.5)
            try:
                self.rect(bx, by, chip_w, bh, style="DF",
                          round_corners=True, corner_radius=4.0)
            except TypeError:
                self.rect(bx, by, chip_w, bh, style="DF")
            self.set_text_color(*TITLE_INK)
            self.set_y(by + 1.8)
            self.set_x(title_x)
            self.multi_cell(avail, line_h, title, align="C",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            rendered_title_bottom = self.get_y()
            # Never let a sourced subtitle ride on the chip's bottom edge.
            self.set_y(max(self.get_y(), by + bh) + 0.8)
            self._title_text_bottom = rendered_title_bottom
            central_bottom = self.get_y()
        else:
            self.set_y(by)

        # Render only source metadata. Parentheses are visual punctuation;
        # no synthetic "Full Notes" or subject fallback is introduced.
        subtitle_source = audience or subject
        if subtitle_source:
            self.set_font("Patrick", "", 15)
            self.set_text_color(*SUBTITLE_GREEN)
            self.set_x(centre_x)
            subtitle = f"( {subtitle_source} )"
            self._subtitle_top = self.get_y()
            self.multi_cell(centre_w, 7, subtitle, align="C",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            # Little pink heartbeat/ECG squiggle under the subtitle.
            yq = self.get_y() + 1.2
            cx = centre_x + centre_w / 2
            self.set_draw_color(214, 64, 110)
            self.set_line_width(0.45)
            pts = [(-24, 0), (-14, 0), (-10, -2.6), (-6, 2.6), (-2, -1.2),
                   (0, 0), (10, 0), (14, -1.8), (18, 1.4), (24, 0)]
            for (ax, ay), (px, py) in zip(pts, pts[1:]):
                self.line(cx + ax, yq + ay, cx + px, yq + py)
            self._tiny_heart(cx + 26.5, yq, 2.4)
            central_bottom = yq + 3.0
        left_bottom = 0.0
        cloud_bottom = 0.0
        if quote_bounds is not None:
            _qx, qy, _qw, qh = quote_bounds
            cloud_bottom = qy + qh * 1.05  # _cloud is 10% taller than nominal

        # The left topic sticker is deliberately prominent and stays in its
        # reserved gutter.
        topic = f"{title} {subject}".strip()
        if doodles is not None and topic:
            icons = doodles.pick_icons(topic, count=1, subject=subject)
            left_icon = "book" if self.language == "sanskrit" else (icons[0] if icons else doodles.subject_fallback_icon(subject))
            left_size = 23.0
            title_mid = by + bh / 2 if title else by + left_size / 2
            left_cy = max(title_mid, 12.8 + 2.0 + left_size / 2)
            doodles.draw_icon(
                self, left_icon, self.l_margin + 11.5, left_cy, left_size,
            )
            left_bottom = left_cy + left_size / 2

        # Long metadata values wrap in independent half-width regions, below
        # the tallest title/quote/doodle element.
        y = max(central_bottom, cloud_bottom, left_bottom) + 3.0
        self.set_font("Patrick", "", 14)
        self.set_text_color(150, 30, 70)
        meta_gap = 6.0
        meta_w = (self.epw - meta_gap) / 2
        sig_lines = self.multi_cell(
            meta_w, 6, self.signature, align="L", dry_run=True,
            output=MethodReturnValue.LINES,
        )
        lecture = _plain(n.lecture_no)
        lecture_lines = (
            self.multi_cell(
                meta_w, 6, lecture, align="R", dry_run=True,
                output=MethodReturnValue.LINES,
            ) if lecture else []
        )
        meta_h = max(len(sig_lines), len(lecture_lines), 1) * 6.0
        self.set_xy(self.l_margin, y)
        self.multi_cell(meta_w, 6, self.signature, align="L",
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if lecture:
            self.set_text_color(0, 0, 0)
            self.set_xy(self.l_margin + meta_w + meta_gap, y)
            self.multi_cell(meta_w, 6, lecture, align="R",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(y + meta_h + 2.0)
        self.set_draw_color(150, 160, 185)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def _cloud(self, x: float, y: float, w: float, h: float, fill, edge,
               bumps: int = 13) -> None:
        """Soft cloud-shaped blob (speech bubble / title chip)."""
        cx, cy = x + w / 2, y + h / 2
        rx, ry = w / 2 * 1.02, h / 2 * 1.10
        pts = []
        steps = bumps * 8
        for i in range(steps):
            a = 2 * math.pi * i / steps
            wob = 1.0 + 0.075 * math.sin(bumps * a) + 0.02 * math.sin(3 * a)
            pts.append((cx + rx * wob * math.cos(a), cy + ry * wob * math.sin(a)))
        self.set_fill_color(*fill)
        self.set_draw_color(*edge)
        self.set_line_width(0.45)
        self.polygon(pts, style="DF")

    def _tiny_heart(self, cx: float, cy: float, r: float) -> None:
        self.set_fill_color(214, 64, 110)
        self.ellipse(cx - r, cy - r * 0.9, r, r, style="F")
        self.ellipse(cx, cy - r * 0.9, r, r, style="F")
        self.polygon([(cx - r, cy - r * 0.15), (cx + r, cy - r * 0.15),
                      (cx, cy + r * 1.15)], style="F")

    def _quote_bubble(self, quote: str) -> tuple[float, float, float, float]:
        """Hand-drawn cloud with a subject quote (page 1, top right).

        Decoration only: the text cursor is restored so the header keeps
        flowing below the title, not up beside the bubble.
        """
        save_x, save_y = self.get_x(), self.get_y()
        pt, pad = 9.5, 2.4
        bw = QUOTE_BUBBLE_W
        text_w = bw * 0.74          # the cloud's flat inner area
        self.set_font("Patrick", "", pt)
        lines = self.multi_cell(
            text_w, 4.4, quote, align="C", dry_run=True,
            output=MethodReturnValue.LINES,
        )
        bh = len(lines) * 4.4 + 3.2 * pad
        x = self.w - self.r_margin - bw
        y = 8.0
        self._cloud(x, y, bw, bh, (255, 255, 255), BOX_INK)
        self.set_text_color(90, 84, 100)
        ty = y + pad
        text_x = x + (bw - text_w) / 2
        for ln in lines:
            self.set_xy(text_x, ty)
            self.cell(text_w, 4.4, ln, align="C")
            ty += 4.4
        self.set_text_color(0, 0, 0)
        self.set_xy(save_x, save_y)
        return x, y, bw, bh

    @staticmethod
    def _load_logo_watermark() -> "bytes | None":
        """Load the repo asset and reduce only its alpha for a faint watermark."""
        try:
            from PIL import Image

            with Image.open(LOGO_WATERMARK_PATH) as source:
                logo = source.convert("RGBA")
                # The supplied square PNG has generous transparent margins.
                # Remove only those empty pixels so the visible emblem, not
                # its padded canvas, fills the requested 124 mm box.
                bounds = logo.getchannel("A").getbbox()
                if not bounds:
                    return None
                logo = logo.crop(bounds)
                alpha = logo.getchannel("A").point(
                    lambda value: max(1, round(value * 0.055)) if value else 0,
                )
                logo.putalpha(alpha)
                output = io.BytesIO()
                logo.save(output, format="PNG")
                return output.getvalue()
        except (OSError, ValueError):
            # A missing or unreadable optional asset must never prevent a PDF.
            return None

    def _logo_watermark(self) -> None:
        if not getattr(self, "logo_watermark_on", False):
            return
        png = getattr(self, "_logo_watermark_png", None)
        if not png:
            return
        size = 124.0
        self.image(
            io.BytesIO(png),
            x=(self.w - size) / 2,
            y=(self.h - size) / 2,
            w=size,
            h=size,
            keep_aspect_ratio=True,
        )

    def _slim_header(self) -> None:
        n = self.notes
        # Start below the page ribbon, then give the running title and
        # signature independent measured halves so neither can overwrite the
        # other on continuation pages.
        y = 14.5
        self.set_y(y)
        self.set_font("Patrick", "", 12)
        self.set_text_color(90, 90, 90)
        left = _plain(n.title) or _plain(n.subject)
        gap = 6.0
        col_w = (self.epw - gap) / 2
        left_lines = (
            self.multi_cell(
                col_w, 6, left, align="L", dry_run=True,
                output=MethodReturnValue.LINES,
            ) if left else []
        )
        signature_lines = self.multi_cell(
            col_w, 6, self.signature, align="R", dry_run=True,
            output=MethodReturnValue.LINES,
        )
        header_h = max(len(left_lines), len(signature_lines), 1) * 6.0
        if left:
            self.set_xy(self.l_margin, y)
            self.multi_cell(col_w, 6, left, align="L",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_xy(self.l_margin + col_w + gap, y)
        self.multi_cell(col_w, 6, self.signature, align="R",
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(y + header_h + 1.5)
        self.set_draw_color(200, 205, 215)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Patrick", "", 10)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, self.signature, align="L")
        self.set_text_color(0, 0, 0)

    # ---------------- rich text engine (text + inline $math$) ----------------
    def _word_width(self, pieces, body_pt) -> float:
        w = 0.0
        for s, mode in pieces:
            self.set_font("Patrick", "",
                          body_pt if mode == "n" else body_pt * _SCRIPT_SCALE)
            w += self.get_string_width(s)
        self.set_font("Patrick", "", body_pt)
        return w

    def _draw_word(self, x, baseline, pieces, body_pt) -> None:
        sub_dy = body_pt * PT_TO_MM * 0.17
        sup_dy = -body_pt * PT_TO_MM * 0.33
        for s, mode in pieces:
            if mode == "n":
                self.set_font("Patrick", "", body_pt)
                self.text(x, baseline, s)
            else:
                self.set_font("Patrick", "", body_pt * _SCRIPT_SCALE)
                self.text(x, baseline + (sub_dy if mode == "sub" else sup_dy), s)
            x += self.get_string_width(s)
        self.set_font("Patrick", "", body_pt)

    def _atoms(self, text: str, body_pt: float, math_pt: float) -> list[dict]:
        self.set_font("Patrick", "", body_pt)
        text_h = body_pt * PT_TO_MM
        atoms: list[dict] = []
        for kind, content in _split_math(text):
            if kind == "text":
                for seg, emph in _emph_runs(content):
                    for part in re.split(r"(\s+)", _sanitize_text(seg)):
                        if not part:
                            continue
                        if part.isspace():
                            atoms.append({"sp": True})
                        else:
                            pieces = _word_pieces(part)
                            atom = {"k": "t",
                                    "w": self._word_width(pieces, body_pt),
                                    "h": text_h, "pieces": pieces}
                            if emph:
                                atom["c"] = EMPH
                            atoms.append(atom)
            else:
                try:
                    png, w_pt, h_pt = math_render.render_latex(content.strip(), math_pt)
                    atoms.append({"k": "m", "w": w_pt * PT_TO_MM,
                                  "h": h_pt * PT_TO_MM, "png": png,
                                  "latex": content.strip(), "pt": math_pt})
                except Exception:  # noqa: BLE001 - degrade to readable text
                    # Split on whitespace exactly like the text branch, so the
                    # fallback can word-wrap instead of overflowing its
                    # cell/box as one giant unbreakable atom.
                    fb = _sanitize_text(_latex_fallback(content))
                    for part in re.split(r"(\s+)", fb):
                        if not part:
                            continue
                        if part.isspace():
                            atoms.append({"sp": True})
                        else:
                            pieces = _word_pieces(part)
                            atoms.append({"k": "t",
                                          "w": self._word_width(pieces, body_pt),
                                          "h": text_h, "pieces": pieces})
        return atoms

    @staticmethod
    def _latex_wrap_chunks(latex: str) -> list[str]:
        r"""Split a wide equation at top-level relations, spaces or list commas.

        Braced constructs remain intact, so expressions such as
        ``\mathrm{bonding orbital}`` never get split into invalid LaTeX.
        """
        relation_commands = {
            r"\approx", r"\geq", r"\leq", r"\leftrightarrow",
            r"\neq", r"\rightarrow", r"\rightleftharpoons",
        }
        chunks: list[str] = []
        current: list[str] = []
        depth = 0
        bracket_depth = 0

        def flush() -> None:
            value = "".join(current).strip()
            if value:
                chunks.append(value)
            current.clear()

        index = 0
        while index < len(latex):
            char = latex[index]
            if char == "\\":
                end = index + 1
                while end < len(latex) and latex[end].isalpha():
                    end += 1
                command = latex[index:end]
                if depth == 0 and command in relation_commands:
                    flush()
                    chunks.append(command)
                else:
                    current.append(command)
                index = end
                continue
            if char == "{":
                depth += 1
                current.append(char)
            elif char == "}":
                depth = max(0, depth - 1)
                current.append(char)
            elif char in "([":
                bracket_depth += 1
                current.append(char)
            elif char in ")]":
                bracket_depth = max(0, bracket_depth - 1)
                current.append(char)
            elif char == "," and depth == 0 and bracket_depth == 0:
                current.append(char)
                flush()
            elif depth == 0 and (char.isspace() or char in "<>="):
                flush()
                if char in "<>=":
                    chunks.append(char)
            else:
                current.append(char)
            index += 1
        flush()
        return chunks

    @staticmethod
    def _scaled_atom(atom: dict, width: float) -> dict:
        """Return an intact image atom scaled to the available width."""
        fitted = dict(atom)
        if fitted["w"] > width:
            scale = width / fitted["w"]
            fitted["w"] = width
            fitted["h"] *= scale
        return fitted

    def _split_math_atom(self, atom: dict, width: float) -> list[dict]:
        """Wrap a wide formula at meaningful operators, preserving all text."""
        chunks = self._latex_wrap_chunks(atom.get("latex", ""))
        if len(chunks) <= 1:
            return [self._scaled_atom(atom, width)]

        fitted: list[dict] = []
        try:
            for chunk in chunks:
                png, w_pt, h_pt = math_render.render_latex(chunk, atom["pt"])
                if fitted:
                    fitted.append({"sp": True})
                fitted.append(self._scaled_atom({
                    "k": "m",
                    "w": w_pt * PT_TO_MM,
                    "h": h_pt * PT_TO_MM,
                    "png": png,
                    "latex": chunk,
                    "pt": atom["pt"],
                }, width))
        except Exception:  # noqa: BLE001 - retain the complete original image
            return [self._scaled_atom(atom, width)]
        return fitted

    def _split_text_atom(self, atom: dict, width: float,
                         body_pt: float) -> list[dict]:
        """Hard-wrap one generated no-whitespace token by visible glyphs."""
        fragments: list[dict] = []
        pieces: list[tuple[str, str]] = []
        fragment_w = 0.0
        text_h = body_pt * PT_TO_MM

        def flush() -> None:
            nonlocal fragment_w
            if not pieces:
                return
            fragment = {
                "k": "t", "w": fragment_w, "h": text_h,
                "pieces": list(pieces),
            }
            if atom.get("c"):
                fragment["c"] = atom["c"]
            fragments.append(fragment)
            pieces.clear()
            fragment_w = 0.0

        for text, mode in atom["pieces"]:
            point_size = body_pt if mode == "n" else body_pt * _SCRIPT_SCALE
            self.set_font("Patrick", "", point_size)
            for char in text:
                char_w = self.get_string_width(char)
                if pieces and fragment_w + char_w > width:
                    flush()
                if pieces and pieces[-1][1] == mode:
                    previous, _ = pieces[-1]
                    pieces[-1] = (previous + char, mode)
                else:
                    pieces.append((char, mode))
                fragment_w += char_w
        flush()
        self.set_font("Patrick", "", body_pt)
        return fragments

    def _fit_atoms(self, atoms: list[dict], width: float,
                   body_pt: float) -> list[dict]:
        """Make every indivisible atom fit before normal line packing."""
        fitted: list[dict] = []
        for atom in atoms:
            if atom.get("sp") or atom["w"] <= width:
                fitted.append(atom)
            elif atom["k"] == "m":
                fitted.extend(self._split_math_atom(atom, width))
            else:
                fitted.extend(self._split_text_atom(atom, width, body_pt))
        return fitted

    def _pack(self, atoms: list[dict], width: float, body_pt: float):
        width = max(float(width), 0.1)
        atoms = self._fit_atoms(atoms, width, body_pt)
        self.set_font("Patrick", "", body_pt)
        space_w = self.get_string_width(" ")
        text_h = body_pt * PT_TO_MM
        lines: list[tuple[list[dict], float]] = []
        cur: list[dict] = []
        cur_w = 0.0
        for a in atoms:
            if a.get("sp"):
                if cur:
                    cur.append(a); cur_w += space_w
                continue
            if cur and cur_w + a["w"] > width:
                while cur and cur[-1].get("sp"):
                    cur.pop()
                lh = max([text_h] + [x["h"] for x in cur if not x.get("sp")])
                lines.append((cur, lh))
                cur = [a]; cur_w = a["w"]
            else:
                cur.append(a); cur_w += a["w"]
        if cur:
            while cur and cur[-1].get("sp"):
                cur.pop()
            lh = max([text_h] + [x["h"] for x in cur if not x.get("sp")])
            lines.append((cur, lh))
        return lines, space_w, text_h

    def _rich_layout(self, text, width, body_pt, math_pt) -> dict:
        """Pack rich text once for identical measurement and sliced drawing."""
        lines, space_w, text_h = self._pack(
            self._atoms(text, body_pt, math_pt), width, body_pt,
        )
        return {
            "lines": lines,
            "width": float(width),
            "space_w": space_w,
            "text_h": text_h,
            "gap": body_pt * PT_TO_MM * 0.45,
            "body_pt": body_pt,
        }

    @staticmethod
    def _rich_lines_height(lines, gap: float) -> float:
        return sum(line_h + gap for _line, line_h in lines)

    @classmethod
    def _rich_line_slice(cls, layout: dict, start: int,
                         max_height: float) -> tuple[int, float]:
        """Return the largest complete line slice bounded by ``max_height``."""
        used = 0.0
        end = start
        lines = layout["lines"]
        while end < len(lines):
            needed = lines[end][1] + layout["gap"]
            if used + needed > max_height + 1e-7:
                break
            used += needed
            end += 1
        return end, used

    def _draw_rich_lines(self, layout: dict, lines, x_left: float,
                         y_top: float, color, align: str = "L") -> float:
        """Draw a pre-packed line slice and return its exact consumed height."""
        space_w = layout["space_w"]
        text_h = layout["text_h"]
        body_pt = layout["body_pt"]
        gap = layout["gap"]
        ascent = text_h * 0.74
        y = y_top
        for line, line_h in lines:
            x = x_left
            if align == "C":
                line_width = sum(
                    space_w if atom.get("sp") else atom["w"]
                    for atom in line
                )
                x = x_left + max(0.0, (layout["width"] - line_width) / 2)
            for atom in line:
                if atom.get("sp"):
                    x += space_w
                    continue
                if atom["k"] == "t":
                    self.set_text_color(*(atom.get("c") or color))
                    baseline = y + (line_h - atom["h"]) / 2 + ascent
                    self._draw_word(x, baseline, atom["pieces"], body_pt)
                else:
                    self.image(
                        io.BytesIO(atom["png"]), x=x,
                        y=y + (line_h - atom["h"]) / 2,
                        w=atom["w"], h=atom["h"],
                    )
                x += atom["w"]
            y += line_h + gap
        return y - y_top

    def _measure_rich(self, text, width, body_pt, math_pt) -> float:
        layout = self._rich_layout(text, width, body_pt, math_pt)
        return self._rich_lines_height(layout["lines"], layout["gap"])

    def _draw_rich(self, text, x_left, y_top, width, body_pt, math_pt, color,
                   align: str = "L") -> float:
        """Draw mixed text+math at (x_left, y_top) within width. Returns height."""
        layout = self._rich_layout(text, width, body_pt, math_pt)
        align = getattr(self, "_rich_align_override", align)
        return self._draw_rich_lines(
            layout, layout["lines"], x_left, y_top, color, align,
        )

    def _draw_rich_aligned(self, text, x_left, y_top, width, body_pt, math_pt,
                           color, align: str) -> float:
        """Apply alignment without changing the legacy ``_draw_rich`` call shape."""
        missing = object()
        previous = getattr(self, "_rich_align_override", missing)
        self._rich_align_override = align
        try:
            return self._draw_rich(
                text, x_left, y_top, width, body_pt, math_pt, color,
            )
        finally:
            if previous is missing:
                del self._rich_align_override
            else:
                self._rich_align_override = previous

    def _flow_text(self, text, x_left, width, body_pt, math_pt, color) -> None:
        """Draw rich text at the cursor with page-break support; move cursor."""
        layout = self._rich_layout(text, width, body_pt, math_pt)
        for line, line_h in layout["lines"]:
            if self.get_y() + line_h + layout["gap"] > self.page_break_trigger:
                self._break()
            y = self.get_y()
            consumed = self._draw_rich_lines(layout, [(line, line_h)],
                                               x_left, y, color)
            self.set_xy(self.l_margin, y + consumed)

    # ---------------- body ----------------
    @staticmethod
    def _is_wide(section) -> bool:
        """Sections that need the whole page width, not a narrow column."""
        if getattr(section, "schematic", None) or getattr(section, "worked_examples", None):
            return True
        if any(r"\begin{" in point for point in section.points or []):
            return True
        if getattr(section, "visual_ids", None):
            return True
        if getattr(section, "_svg", None) or getattr(section, "_image", None):
            return True
        if getattr(section, "diagram", ""):
            return True
        if section.graph and section.graph.curves:
            return True
        if section.table and (len(section.table.headers) >= 3 or len(section.table.rows) > 4):
            return True
        if getattr(section, "compare", None):
            return True
        return bool(section.flow and len(section.flow) > 3)

    def render_body(self) -> None:
        # Body cards own their width and height.  This deliberately bypasses
        # the historical permanent two-column flow.
        self._use_full_width()
        self.set_y(max(self.get_y(), self._body_top))
        for row in self._pack_card_rows(self.notes.sections):
            self._render_card_row(row)
        mindmap = getattr(self.notes, "mindmap", None)
        if mindmap and mindmap.branches:
            self._render_mindmap(mindmap)

    def _card_weight(self, section) -> int:
        """A deterministic content weight used only for ordinary card widths."""
        weight = 1 + len(section.points or []) + len(getattr(section, "tricks", []) or [])
        weight += 2 * len(getattr(section, "notes", []) or [])
        weight += len(getattr(section, "where", []) or [])
        if section.table:
            weight += len(section.table.rows) + 2
        if section.flow:
            weight += len(section.flow)
        return max(1, weight)

    def _pack_card_rows(self, sections):
        """Pack source-order sections into deterministic one/two-card rows."""
        self._use_full_width()
        rows = []
        index = 0
        sections = list(sections or [])
        while index < len(sections):
            first = sections[index]
            if self._is_wide(first) or index + 1 >= len(sections) or self._is_wide(sections[index + 1]):
                rows.append([(first, self.epw)])
                index += 1
                continue
            second = sections[index + 1]
            usable = self.epw - self.CARD_GUTTER
            left_weight, right_weight = self._card_weight(first), self._card_weight(second)
            left_ratio = left_weight / (left_weight + right_weight)
            left_ratio = max(0.38, min(0.62, left_ratio))
            left_width = usable * left_ratio
            right_width = usable - left_width
            if min(left_width, right_width) < self.CARD_MIN_WIDTH:
                rows.append([(first, self.epw)])
                index += 1
            else:
                rows.append([(first, left_width), (second, right_width)])
                index += 2
        return rows

    def _preflight_card(self, section, x: float, y: float, width: float,
                        number: int) -> tuple[float, bool, float]:
        """Reversibly render a card at its real region to measure it exactly.

        Rich maths, wrapping tables, images and nested boxes all use the normal
        renderer here.  Restoring the complete FPDF state makes this a dry run
        while retaining the page-break decisions the real card will make.
        Returns (height, spills to another page, height on its first page).
        """
        # Activated notes own thread locks and immutable mapping proxies.  They
        # are job-runtime authority, not mutable FPDF layout state, so preserve
        # them by identity while cloning only the reversible renderer state.
        identity_state = {
            "notes",
            "_visual_manifest",
            "_visual_run_budget",
            "_visual_assets",
            "_visual_records",
            "_visual_by_id",
            "_rendered_visuals",
        }
        state = {
            name: (value if name in identity_state else deepcopy(value))
            for name, value in vars(self).items()
        }
        try:
            self._check_visual_deadline()
            start_page = self.page_no()
            self._layout_preflight_depth += 1
            self._section_no = number - 1
            self._responsive_card = (x, width)
            self._responsive_frame_top = y
            self._responsive_frame_min_bottom = None
            self._card_first_break_y = None
            self._use_region(x, width)
            self.set_xy(x, y + 1.3)
            self._render_section(section)
            page_count = self.page_no() - start_page
            consumed = max(18.0, self.get_y() - y + page_count * self.eph + 1.5)
            first_break = self._card_first_break_y
            lead = consumed if not page_count or first_break is None else first_break - y
            self._check_visual_deadline()
            return consumed, page_count > 0, lead
        finally:
            current = vars(self)
            current.clear()
            current.update(state)

    def _measure_card(self, section, width: float, number: int) -> float:
        """Compatibility wrapper returning the real dry-run card footprint."""
        height, _, _ = self._preflight_card(section, self.l_margin, self.get_y(),
                                            width, number)
        return height

    def _soft_card_frame(self, x: float, y: float, width: float, height: float) -> None:
        """Pale rounded dotted outline behind one complete section card."""
        self.set_draw_color(184, 190, 208)
        self.set_line_width(0.38)
        try:
            self.set_dash_pattern(dash=1.1, gap=1.15)
            self.rect(x, y, width, height, style="D", round_corners=True,
                      corner_radius=3.0)
        except Exception:  # noqa: BLE001
            self.rect(x, y, width, height, style="D")
        finally:
            try:
                self.set_dash_pattern()
            except Exception:  # noqa: BLE001
                pass

    def _render_card(self, section, x: float, y: float, width: float,
                      height: float) -> float:
        """Render exactly one independent card and return its consumed height."""
        self._responsive_card = (x, width)
        self._responsive_frame_top = y
        self._responsive_frame_min_bottom = y + height
        self._use_region(x, width)
        self.set_xy(x, y + 1.3)
        self._render_section(section)
        self._close_responsive_frame()
        self._responsive_card = None
        self._responsive_frame_top = None
        self._responsive_frame_min_bottom = None
        return max(height, self.get_y() - y + 1.5)

    def _render_oversized_card(self, section, x: float, y: float,
                               width: float) -> None:
        """Render a card that genuinely spans pages with one frame per page."""
        self._responsive_card = (x, width)
        self._responsive_frame_top = y
        self._responsive_frame_min_bottom = None
        self._use_region(x, width)
        self.set_xy(x, y + 1.3)
        self._render_section(section)
        self._close_responsive_frame()
        self._responsive_card = None
        self._responsive_frame_top = None
        self._responsive_frame_min_bottom = None
        self._use_full_width()
        self.set_xy(PAGE_MARGIN, self.get_y() + 2.4)

    def _preflight_row(self, row, y: float) -> list[tuple[float, bool, float]]:
        """Dry-run every card of a row side by side from ``y``."""
        x = PAGE_MARGIN
        layouts = []
        for i, (section, width) in enumerate(row):
            layouts.append(self._preflight_card(
                section, x, y, width, self._section_no + i + 1,
            ))
            x += width + self.CARD_GUTTER
        return layouts

    def _render_card_row(self, row, *, reuse_fresh_page: bool = False) -> None:
        """Preflight and render a row; the body cursor finishes below its tall card."""
        y = self.get_y()
        layouts = self._preflight_row(row, y)
        row_height = max(height for height, _, _ in layouts)

        # A pair whose real wrapped content will not fit is no longer a
        # compatible pair.  Decide that *before* turning the page: the first
        # card usually fits right here, and turning first abandons the rest of
        # this page (a short opening section used to leave page 1 empty, with
        # the tall sibling starting page 2 anyway).  Each promoted card then
        # makes its own placement decision below.
        if len(row) > 1 and any(spills for _, spills, _ in layouts):
            for section, _width in row:
                self._render_card_row([(section, self.epw)])
            return

        needs_fresh_page = (any(spills for _, spills, _ in layouts)
                            or y + row_height + 2.0 > self.page_break_trigger)
        keep_visual_together = False
        if needs_fresh_page and len(row) == 1:
            section = row[0][0]
            if section.graph or section.schematic:
                fresh = self._preflight_row(row, self._body_top)[0]
                keep_visual_together = (
                    not fresh[1]
                    and self._body_top + fresh[0] + 2 <= self.page_break_trigger
                )
        # A card that runs past the page end starts here and continues on the
        # next page whenever its heading and a few lines fit: turning the page
        # first left up to half of it blank.  A card whose first page would
        # hold little more than its heading (the next line, table or diagram
        # cannot fit) still moves whole.  Pairs keep their fresh-page promotion.
        if (needs_fresh_page and len(row) == 1
                and layouts[0][2] >= self.CARD_MIN_LEAD and not keep_visual_together):
            needs_fresh_page = False
        if needs_fresh_page and not reuse_fresh_page:
            self.add_page()
            self._use_full_width()
            y = self._body_top
            self.set_y(y)
            layouts = self._preflight_row(row, y)
            row_height = max(height for height, _, _ in layouts)

        # The pair may only have become unviable after the page turn above.
        if len(row) > 1 and any(spills for _, spills, _ in layouts):
            for index, (section, _) in enumerate(row):
                self._render_card_row(
                    [(section, self.epw)], reuse_fresh_page=index == 0,
                )
            return

        # A one-card row that continues overleaf (it started near a page end,
        # or is larger than a printable page) gets an explicit continuation
        # policy: same region, fresh frame on every page.
        if len(row) == 1 and layouts[0][1]:
            section, width = row[0]
            self._render_oversized_card(section, PAGE_MARGIN, y, width)
            return

        measurements = [height for height, _, _ in layouts]
        x = PAGE_MARGIN
        actual_heights = []
        for (section, width), height in zip(row, measurements):
            actual_heights.append(self._render_card(section, x, y, width, height))
            x += width + self.CARD_GUTTER
        self._use_full_width()
        self.set_xy(PAGE_MARGIN, y + max(actual_heights) + 2.4)

    def _dotted_frame(self, top: float, bottom: float) -> None:
        """Dashed frame around a full-width section."""
        self.set_draw_color(168, 172, 190)
        self.set_line_width(0.4)
        try:
            self.set_dash_pattern(dash=1.5, gap=1.3)
            self.rect(self.l_margin - 1.5, top - 1.0,
                      self.epw + 3.0, bottom - top + 2.0, style="D",
                      round_corners=True, corner_radius=2.0)
        except Exception:  # noqa: BLE001
            self.rect(self.l_margin - 1.5, top - 1.0,
                      self.epw + 3.0, bottom - top + 2.0, style="D")
        finally:
            try:
                self.set_dash_pattern()
            except Exception:  # noqa: BLE001
                pass

    def _close_responsive_frame(self, bottom: float | None = None) -> None:
        """Close the current responsive card's page-local frame, if any."""
        if self._responsive_card is None or self._responsive_frame_top is None:
            return
        x, width = self._responsive_card
        frame_bottom = self.get_y() + 1.5 if bottom is None else bottom
        if self._responsive_frame_min_bottom is not None:
            frame_bottom = max(frame_bottom, self._responsive_frame_min_bottom)
        if frame_bottom > self._responsive_frame_top:
            self._soft_card_frame(x, self._responsive_frame_top, width,
                                  frame_bottom - self._responsive_frame_top)
        self._responsive_frame_top = None
        self._responsive_frame_min_bottom = None

    def _close_band_frame(self, bottom: float | None = None) -> None:
        """Finish this page's dotted frame before a band changes page."""
        if self._band_frame_top is None:
            return
        frame_bottom = self.get_y() if bottom is None else bottom
        self._dotted_frame(self._band_frame_top, frame_bottom + 1.2)
        self._band_frame_top = None

    def _render_section(self, section) -> None:
        self._check_visual_deadline()
        heading = (section.heading or "").strip()
        if heading:
            self._section_heading(
                section.heading,
                getattr(section, "icon", "") or "",
            )

        visual_ids = tuple(getattr(section, "visual_ids", ()) or ())
        if visual_ids:
            for visual_id in visual_ids:
                self._check_visual_deadline()
                self._render_source_visual(visual_id)
                self._check_visual_deadline()
        else:
            svg = getattr(section, "_svg", None)
            if svg:
                try:
                    from . import svg_gen

                    w = self.epw - 8.0
                    h = w * svg_gen.aspect(svg)
                    if h > 108.0:
                        w, h = 108.0 / svg_gen.aspect(svg), 108.0
                    if self.get_y() + h + 4 > self.page_break_trigger:
                        self._break()
                    x = self.l_margin + (self.epw - w) / 2
                    y = self.get_y() + 1.5
                    self._draw_svg(svg, x, y, w, 108.0)
                    self.set_xy(self.l_margin, y + h + 2.4)
                except Exception:  # noqa: BLE001 - a bad SVG never breaks notes
                    pass
            img = getattr(section, "_image", None)
            if img:
                self._wide_image(img)
            if getattr(section, "diagram", ""):
                self._diagram_box(section.diagram)
        for point in section.points:
            self._bullet(point)
        if getattr(section, "key_terms", None):
            self._key_terms_box(section.key_terms)
        if getattr(section, "where", None):
            self._where_list(section.where)
        if getattr(section, "compare", None):
            self._compare_columns(section.compare)
        if section.table and section.table.headers and section.table.rows:
            self._render_table(section.table)
        if section.flow:
            self._render_flow(section.flow)
        for index, example in enumerate(getattr(section, "worked_examples", []) or [], 1):
            self._render_worked_example(example, index)
        has_drawn_source_graph = any(
            visual_id in self._visual_by_id
            and self._visual_by_id[visual_id].kind == "graph"
            for visual_id in visual_ids
        )
        if section.graph and section.graph.curves and not has_drawn_source_graph:
            self._render_graph(section.graph)
        if section.schematic:
            from .schematic_render import render_schematic
            render_schematic(self, section.schematic)
        for trick in getattr(section, "tricks", []) or []:
            self._trick(trick)
        for note in getattr(section, "notes", []) or []:
            self._note_box(note)
        self.ln(0.8)
        self._check_visual_deadline()

    def _render_worked_example(self, example, index: int) -> None:
        """Keep the question and ordered work readable across page boundaries."""
        from .note_quality import notes_are_hindi
        hindi = notes_are_hindi(self.notes)
        proof = example.kind == "proof"
        question, solution, result = (
            (("प्रमेय", "प्रमाण", "निष्कर्ष") if proof else ("प्रश्न", "हल", "उत्तर"))
            if hindi else
            (("Theorem", "Proof", "Conclusion") if proof else ("Question", "Solution", "Answer"))
        )
        if self.language == "sanskrit":
            question, solution, result = (("प्रमेयम्", "प्रमाणम्", "निष्कर्षः") if proof
                                          else ("प्रश्नः", "समाधानम्", "उत्तरम्"))
        width = self.epw - 6
        first_height = self._measure_rich(f"**{question} {index}:** {example.statement}", width, 13, 14.5)
        if self.get_y() + min(first_height, 25) + 12 > self.page_break_trigger:
            self._break()
        for text in [f"**{question} {index}:** {example.statement}",
                     *([f"**{solution}:**"] if example.steps else []),
                     *(f"{step_no}. {step}" for step_no, step in enumerate(example.steps, 1)),
                     *([f"**{result}:** {example.result}"] if example.result else [])]:
            self._flow_text(text, self.l_margin + 3, width, 13, 14.5, INK)
            self.ln(1.2)
        self.ln(2)

    # ---------------- mind map (last-page infographic) ----------------
    def _render_mindmap(self, mm) -> None:
        branches = [b for b in mm.branches if (b.label or "").strip()]
        if not branches:
            return
        # Callers may arrive from a narrow body column. Resolve every map
        # measurement from the actual page-wide region before sizing cards.
        self._use_full_width()
        gap_x, center_w = self.MINDMAP_GAP_X, self.MINDMAP_CENTER_W
        col_w = (self.epw - center_w - 2 * gap_x) / 2
        pad_x, pad_y = 2.4, 1.8
        bottom_reserve = 6.0
        title_h = 9.0 + 2.0 + 1.5   # title line, gap, first-branch offset

        def branch_states(scale: float) -> list[dict]:
            label_pt, child_pt, math_pt = 12.5 * scale, 10.5 * scale, 11.5 * scale
            states: list[dict] = []
            for branch in branches:
                blocks = [{
                    "layout": self._rich_layout(
                        branch.label, col_w - 2 * pad_x, label_pt, math_pt,
                    ),
                    "label": True,
                }]
                blocks.extend({
                    "layout": self._rich_layout(
                        f"- {child}", col_w - 2 * pad_x - 2.5,
                        child_pt, math_pt,
                    ),
                    "label": False,
                } for child in (branch.children or []))
                states.append({"blocks": blocks, "block": 0, "line": 0})
            return states

        def whole_map_height(states: list[dict], vgap: float) -> float:
            """Tallest side when every branch is placed whole, as below."""
            used = [0.0, 0.0]
            for state in states:
                side = 0 if used[0] <= used[1] else 1
                used[side] += (vgap if used[side] else 0.0) + 2 * pad_y + sum(
                    self._rich_lines_height(block["layout"]["lines"],
                                            block["layout"]["gap"])
                    for block in state["blocks"]
                )
            return max(used)

        def fitting_scale(room: float):
            """Largest allowed size at which the whole map fits ``room``."""
            for scale in self.MINDMAP_SCALES:
                states = branch_states(scale)
                vgap = self.MINDMAP_VGAP * scale
                # 34 mm is the least column height drawn round the centre node.
                if max(whole_map_height(states, vgap), 34.0) <= room + 1e-7:
                    return states, vgap
            return None

        # Under the notes when the whole map fits the rest of this page;
        # otherwise on its own page, shrunk a little if that keeps it to one.
        map_here = fitting_scale(
            self.page_break_trigger - self.get_y() - 3.0 - title_h - bottom_reserve,
        )
        if map_here is not None:
            states, vgap = map_here
        else:
            states, vgap = branch_states(1.0), self.MINDMAP_VGAP

        def plan_segment(branch_index: int, capacity: float) -> dict:
            state = states[branch_index]
            block_index, line_index = state["block"], state["line"]
            records: list[tuple[int, int, int, float]] = []
            used = 0.0
            while block_index < len(state["blocks"]):
                layout = state["blocks"][block_index]["layout"]
                if line_index >= len(layout["lines"]):
                    block_index += 1
                    line_index = 0
                    continue
                end, height = self._rich_line_slice(
                    layout, line_index, capacity - used,
                )
                if end == line_index:
                    break
                records.append((block_index, line_index, end, height))
                used += height
                if end == len(layout["lines"]):
                    block_index += 1
                    line_index = 0
                else:
                    line_index = end
                    break
            return {
                "records": records,
                "used": used,
                "block": block_index,
                "line": line_index,
                "complete": block_index >= len(state["blocks"]),
            }

        def connector(x1: float, y1: float, x2: float, y2: float, edge) -> None:
            self.set_draw_color(*edge)
            self.set_line_width(0.55)
            bend = (x2 - x1) * 0.45
            try:
                self.curve(x1, y1, x1 + bend, y1, x2 - bend, y2, x2, y2)
            except Exception:  # noqa: BLE001 - fall back to a straight line
                self.line(x1, y1, x2, y2)

        def draw_branch_segment(branch_index: int, plan: dict,
                                x_box: float, y: float, h: float) -> None:
            palette = MINDMAP_PALETTE[branch_index % len(MINDMAP_PALETTE)]
            edge, fill, ink = palette
            self.set_draw_color(*edge)
            self.set_fill_color(*fill)
            self.set_line_width(0.45)
            try:
                self.rect(x_box, y, col_w, h, style="DF",
                          round_corners=True, corner_radius=2.2)
            except TypeError:
                self.rect(x_box, y, col_w, h, style="DF")
            ty = y + pad_y
            for block_index, start, end, _height in plan["records"]:
                block = states[branch_index]["blocks"][block_index]
                layout = block["layout"]
                is_label = block["label"]
                ty += self._draw_rich_lines(
                    layout, layout["lines"][start:end],
                    x_box + pad_x + (0 if is_label else 2.5),
                    ty, edge if is_label else ink,
                )

        center_txt = _plain(mm.center) or _plain(self.notes.title) or "Lecture"
        branch_index = 0
        first_page = True
        while branch_index < len(branches):
            if first_page and map_here is not None:
                self.set_y(self.get_y() + 3.0)
            else:
                self.add_page()
                self._use_full_width()
                fresh = first_page and fitting_scale(
                    self.page_break_trigger - self.get_y() - title_h - bottom_reserve,
                )
                if fresh:
                    states, vgap = fresh
            first_page = False
            self.set_font("Patrick", "", 19)
            self.set_text_color(*HEAD_BLUE)
            self.set_x(self.l_margin)
            self.multi_cell(0, 9, "Mind Map — Quick Revision", align="C",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(2)
            top = self.get_y() + 1.5
            max_col_h = self.page_break_trigger - top - bottom_reserve
            used = [0.0, 0.0]
            placements: list[dict] = []

            while branch_index < len(branches):
                placed = False
                for side in sorted(range(2), key=lambda item: used[item]):
                    before = vgap if used[side] else 0.0
                    capacity = max_col_h - used[side] - before - 2 * pad_y
                    plan = plan_segment(branch_index, capacity)
                    if not plan["records"]:
                        continue
                    height = plan["used"] + 2 * pad_y
                    y = top + used[side] + before
                    placements.append({
                        "branch": branch_index,
                        "side": side,
                        "y": y,
                        "height": height,
                        "plan": plan,
                    })
                    used[side] += before + height
                    states[branch_index]["block"] = plan["block"]
                    states[branch_index]["line"] = plan["line"]
                    placed = True
                    if plan["complete"]:
                        branch_index += 1
                    break
                if not placed or (placements and not placements[-1]["plan"]["complete"]):
                    break

            # A normal text/math line always fits a fresh map column. This
            # guard prevents an accidental endless loop if an upstream image
            # renderer ever returns a pathological line height.
            if not placements:
                raise ValueError("Mind-map content cannot fit inside an A4 page")

            col_h = max(max(used), 34.0)
            cx = self.l_margin + col_w + gap_x + center_w / 2
            cy = top + col_h / 2
            for placement in placements:
                side = placement["side"]
                x_box = (
                    self.l_margin if side == 0
                    else self.l_margin + col_w + 2 * gap_x + center_w
                )
                edge = MINDMAP_PALETTE[
                    placement["branch"] % len(MINDMAP_PALETTE)
                ][0]
                if side == 0:
                    connector(
                        cx - center_w / 2, cy,
                        x_box + col_w, placement["y"] + placement["height"] / 2,
                        edge,
                    )
                else:
                    connector(
                        cx + center_w / 2, cy,
                        x_box, placement["y"] + placement["height"] / 2,
                        edge,
                    )
            for placement in placements:
                side = placement["side"]
                x_box = (
                    self.l_margin if side == 0
                    else self.l_margin + col_w + 2 * gap_x + center_w
                )
                draw_branch_segment(
                    placement["branch"], placement["plan"], x_box,
                    placement["y"], placement["height"],
                )

            # Central node is repeated as navigation furniture on continuation
            # pages; every source branch/child remains unique in its card.
            self.set_font("Patrick", "", 13.5)
            clines = self.multi_cell(
                center_w - 5, 6.2, center_txt, align="C", dry_run=True,
                output=MethodReturnValue.LINES,
            )
            ch = len(clines) * 6.2 + 4.4
            self.set_fill_color(*HEAD_BLUE)
            self.set_draw_color(*HEAD_BLUE)
            try:
                self.rect(
                    cx - center_w / 2, cy - ch / 2, center_w, ch, style="DF",
                    round_corners=True, corner_radius=3.0,
                )
            except TypeError:
                self.rect(cx - center_w / 2, cy - ch / 2,
                          center_w, ch, style="DF")
            self.set_text_color(255, 255, 255)
            ty = cy - ch / 2 + 2.2
            for line in clines:
                self.set_xy(cx - center_w / 2, ty)
                self.cell(center_w, 6.2, line, align="C")
                ty += 6.2
            self.set_text_color(0, 0, 0)
            self.set_xy(self.l_margin, top + col_h + bottom_reserve)
        self._use_column(0)

    def _draw_star(self, cx: float, cy: float, r_out: float) -> None:
        r_in = r_out * 0.42
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            r = r_out if i % 2 == 0 else r_in
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        self.set_fill_color(*STAR_GOLD)
        self.polygon(pts, style="F")

    def _trick(self, point: str) -> None:
        """A highlighted star-point: soft yellow box, gold star, amber text."""
        point = (point or "").strip()
        if not point:
            return
        pad_x, pad_y = 2.2, 1.3
        star_w = 6.4
        text_w = self.epw - star_w - 2 * pad_x
        body_pt, math_pt = 12.5, 14.0
        h = self._measure_rich(point, text_w, body_pt, math_pt) + 2 * pad_y
        if self.get_y() + h > self.page_break_trigger:
            self._break()
        x, y = self.l_margin, self.get_y()
        self.set_draw_color(*TRICK_EDGE)
        self.set_line_width(0.42)
        self.set_fill_color(*TRICK_FILL)
        try:
            self.set_dash_pattern(dash=1.5, gap=1.2)
            self.rect(x, y, self.epw, h, style="DF",
                      round_corners=True, corner_radius=1.8)
        except Exception:  # noqa: BLE001
            self.rect(x, y, self.epw, h, style="DF")
        finally:
            try:
                self.set_dash_pattern()
            except Exception:  # noqa: BLE001
                pass
        drew_icon = False
        if doodles is not None:
            try:
                doodles.draw_icon(self, "bulb", x + pad_x + 1.9, y + h / 2, 5.6)
                drew_icon = True
            except Exception:  # noqa: BLE001
                pass
        if not drew_icon:
            self._draw_star(x + pad_x + 1.8, y + h / 2, 2.2)
        self._draw_rich(point, x + star_w + pad_x, y + pad_y,
                        text_w, body_pt, math_pt, TRICK_INK)
        self.set_xy(self.l_margin, y + h + 1.6)

    def _section_heading(self, heading: str, icon: str = "") -> None:
        heading = (heading or "").strip()
        if not heading:
            return
        self._section_no += 1
        fill, ink = SECTION_PASTELS[(self._section_no - 1) % len(SECTION_PASTELS)]
        edge = BOX_INK
        self._sec_edge, self._sec_ink = ink, ink

        label = f"{self._section_no}. {heading.upper()}"
        pad_x, pad_y = 3.0, 1.4
        icon_w = 9.0 if doodles is not None else 0.0
        avail = self.epw - icon_w - 14.0  # room for the arrow tip
        raw_width = self._measure_line_width(label, 14.5, 16)
        text_width = min(raw_width, avail - 2 * pad_x)
        inner_width = text_width + 2.0
        bw = inner_width + 2 * pad_x
        th = self._measure_rich(label, inner_width, 14.5, 16)
        bh = th + 2 * pad_y
        self.ln(0.6)
        if self.get_y() + bh + 2 > self.page_break_trigger:
            self._break()
        y = self.get_y()
        x0 = self.l_margin + icon_w
        if doodles is not None:
            name = "book" if self.language == "sanskrit" else (icon or (doodles.pick_icon(heading) or ""))
            if name:
                try:
                    doodles.draw_icon(self, name, self.l_margin + 3.6,
                                      y + bh / 2, 7.6)
                except Exception:  # noqa: BLE001
                    pass
        self.set_font("Patrick", "", 14.5)
        self._crayon_box(x0, y, bw, bh, fill, edge)
        tip = 5.0
        self.set_fill_color(*fill)
        self.set_draw_color(*edge)
        self.set_line_width(0.35)
        self.polygon([(x0 + bw, y + 0.2), (x0 + bw + tip, y + bh / 2),
                      (x0 + bw, y + bh - 0.2)], style="DF")
        self._draw_rich_aligned(label, x0 + pad_x, y + pad_y, inner_width,
                                14.5, 16, ink, "C")
        self.set_xy(self.l_margin, y + bh + 2.4)

    def _wrap_count(self, text: str, width: float) -> int:
        """How many lines `text` needs at the current font inside `width`."""
        lines, cur = 0, ""
        for w in text.split():
            cand = (cur + " " + w).strip()
            if cur and self.get_string_width(cand) > width:
                lines += 1
                cur = w
            else:
                cur = cand
        return lines + (1 if cur else 0)

    def _widest_line_width(self, text: str, width: float) -> float:
        """Width of the widest wrapped line of plain text at the current font."""
        words, lines, cur = text.split(), [], ""
        for w in words:
            cand = (cur + " " + w).strip()
            if cur and self.get_string_width(cand) > width:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return max((self.get_string_width(l) for l in lines), default=0.0)

    def _crayon_box(self, x: float, y: float, w: float, h: float,
                    fill, edge) -> None:
        """A heading box that looks crayon-shaded inside a hand-drawn outline.

        The fill is built from diagonal pencil strokes (two passes at slightly
        different angles and lightness) instead of one flat colour, and the
        outline is drawn as four wobbly double-strokes.
        """
        r, g, b = fill
        # Clean soft-pastel block with rounded corners and a thin dark
        # outline — exactly the reference studygram heading chip. A single
        # faint highlight band along the top gives it a crayon-ish softness
        # without turning into visible hatching.
        self.set_fill_color(r, g, b)
        try:
            self.rect(x, y, w, h, style="F", round_corners=True,
                      corner_radius=1.8)
        except TypeError:
            self.rect(x, y, w, h, style="F")
        self.set_fill_color(min(255, r + 7), min(255, g + 7), min(255, b + 7))
        self.rect(x + 0.6, y + 0.5, w - 1.2, h * 0.34, style="F")

        self.set_draw_color(*edge)
        self.set_line_width(0.4)
        try:
            self.rect(x, y, w, h, style="D", round_corners=True,
                      corner_radius=1.8)
        except TypeError:
            self.rect(x, y, w, h, style="D")

    def _sketch_rect(self, x: float, y: float, w: float, h: float, edge,
                     width: float = 0.35) -> None:
        """Hand-drawn looking rectangle: slightly wobbly, double-stroked."""
        self.set_draw_color(*edge)
        self.set_line_width(width)
        j = 0.35  # jitter, mm
        corners = [
            (x + j * 0.6, y), (x + w - j, y + j * 0.4),
            (x + w, y + h - j * 0.5), (x + j * 0.4, y + h - j * 0.2),
        ]
        for k in range(4):
            ax, ay = corners[k]
            bx, by = corners[(k + 1) % 4]
            self.line(ax, ay, bx, by)
        # faint second pass, offset — the "drawn twice" pencil feel
        self.set_line_width(width * 0.7)
        for k in range(4):
            ax, ay = corners[k]
            bx, by = corners[(k + 1) % 4]
            self.line(ax + 0.25, ay + 0.25, bx + 0.2, by + 0.2)

    def _measure_line_width(self, text: str, body_pt: float, math_pt: float) -> float:
        atoms = self._atoms(text, body_pt, math_pt)
        self.set_font("Patrick", "", body_pt)
        sp = self.get_string_width(" ")
        return sum(sp if a.get("sp") else a["w"] for a in atoms)

    def _bullet(self, point: str) -> None:
        point = (point or "").strip()
        if not point:
            return
        # A "pure formula" point becomes a boxed formula callout (like the
        # red-bordered E_cell box in classic toppers' notes).
        if re.fullmatch(r"\s*\$[^$]+\$[\s.:;]*", point):
            self._formula_box(point)
            return
        x0, indent = self.l_margin, 6.0
        # Page-break on the REAL first-line height (a tall inline-math atom can
        # far exceed a plain text line), so the dot is never orphaned on the
        # previous page while _flow_text moves the line to the next one.
        atoms = self._atoms(point, 13, 14.5)
        lines, _, _ = self._pack(atoms, self.epw - indent, 13)
        first_h = lines[0][1] if lines else 6.0
        if self.get_y() + first_h > self.page_break_trigger:
            self._break()
        self.set_fill_color(*BOX_INK)
        self.ellipse(x0 + 1.4, self.get_y() + 2.5, 1.5, 1.5, style="F")
        self._flow_text(point, x0 + indent, self.epw - indent, 13, 14.5, INK)
        self.ln(0.4)

    def _formula_box(self, point: str) -> None:
        """Important standalone formula inside a thin red-bordered box."""
        pad_x, pad_y = 3.2, 1.6
        # A single math image never wraps, so the box must be sized from its
        # REAL width (clamped only by the page) or the formula spills outside.
        max_w = self.epw - 2 * pad_x
        tw = min(self._measure_line_width(point, 13.5, 15.5), max_w)
        w_avail = tw
        h = self._measure_rich(point, w_avail, 13.5, 15.5) + 2 * pad_y
        bw = tw + 2 * pad_x
        if self.get_y() + h + 2 > self.page_break_trigger:
            self._break()
        x = self.l_margin + (self.epw - bw) / 2
        y = self.get_y() + 0.6
        self.set_draw_color(*BOX_INK)
        self.set_fill_color(*FORMULA_FILL)
        self.set_line_width(0.4)
        self.rect(x, y, bw, h, style="DF")
        self._draw_rich(point, x + pad_x, y + pad_y, w_avail, 13.5, 15.5, INK)
        self._sparkle(x - 3.2, y + 2.2, 2.2)   # tiny star beside the formula
        self.set_xy(self.l_margin, y + h + 1.8)

    def _draw_svg(self, svg: str, x: float, y: float, w: float,
                  max_h: float = 999.0) -> float:
        """Draw a model-written SVG diagram as vector art. Returns its height."""
        from fpdf.svg import SVGObject

        from . import svg_gen

        from fpdf.drawing import Transform

        h = w * svg_gen.aspect(svg)
        if h > max_h:
            w, h = max_h / svg_gen.aspect(svg), max_h
        obj = SVGObject(svg)
        # Fit the art to OUR box, then move it there. (draw_to_page() would
        # re-fit the drawing to the whole page and land it in the wrong spot.)
        _, _, path = obj.transform_to_rect_viewport(scale=1, width=w, height=h)
        path.transform = path.transform @ Transform.translation(x, y)
        self.draw_path(path)
        return h

    def _img_size(self, png: bytes, max_w: float, max_h: float):
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(png)) as im:
            w_px, h_px = im.size
        w = max_w
        h = w * h_px / max(w_px, 1)
        if h > max_h:
            h = max_h
            w = h * w_px / max(h_px, 1)
        return w, h

    def _preflight_visual_png(
        self,
        png: bytes,
        width_mm: float,
        height_mm: float,
    ) -> None:
        """Prove fpdf can encode one candidate without touching this PDF."""
        scratch = FPDF(orientation="P", unit="mm", format="A4")
        scratch.set_auto_page_break(False)
        scratch.add_page()
        scratch.image(
            io.BytesIO(png),
            x=10.0,
            y=10.0,
            w=width_mm,
            h=height_mm,
        )
        bytes(scratch.output())

    def _source_visual_slot(
        self,
        record: _PreparedRenderAsset,
    ) -> tuple[float, float, float, float]:
        """Measure one aspect-preserving full-width slot, breaking before shrink."""
        self._check_visual_deadline()
        content_width = float(self.epw)
        if not math.isfinite(content_width) or content_width <= 0.0:
            raise VisualFidelityError("render_failed")
        width = content_width
        height = width * record.height_px / record.width_px
        self._check_visual_deadline()

        top_gap = 1.0
        bottom_gap = 2.4
        y = self.get_y() + top_gap
        room = self.page_break_trigger - y - bottom_gap
        at_fresh_body_top = self.get_y() <= self._body_top + 2.0

        self._check_visual_deadline()
        if height > room and not at_fresh_body_top:
            was_full_width = self._full_width
            self._break()
            if was_full_width and self._responsive_card is None:
                self._use_full_width()
            content_width = float(self.epw)
            width = content_width
            height = width * record.height_px / record.width_px
            y = self.get_y() + top_gap
            room = self.page_break_trigger - y - bottom_gap
        self._check_visual_deadline()

        if not math.isfinite(room) or room <= 0.0:
            raise VisualFidelityError("render_failed")
        if height > room:
            width = room * record.width_px / record.height_px
            height = room
            if width < self.VISUAL_MIN_READABLE_WIDTH_RATIO * content_width:
                raise VisualFidelityError("render_failed")
        x = self.l_margin + (self.epw - width) / 2.0
        if not all(math.isfinite(value) for value in (x, y, width, height)):
            raise VisualFidelityError("render_failed")
        self._check_visual_deadline()
        return x, y, width, height

    def _render_source_visual(self, visual_id: str) -> None:
        """Render primary PNG, or its prepared fallback in the same slot, once."""
        self._check_visual_deadline()
        if (
            self._visual_manifest is None
            or type(visual_id) is not str
            or visual_id not in self._visual_records
        ):
            raise VisualFidelityError("render_failed")
        record = self._visual_records[visual_id]
        if self._layout_preflight_depth == 0:
            expected = manifest_ids(self._visual_manifest)
            next_index = len(self._rendered_visuals)
            if next_index >= len(expected) or visual_id != expected[next_index]:
                raise VisualFidelityError("render_failed")
        x, y, width, height = self._source_visual_slot(record)

        # Responsive card preflight owns only geometry.  It must not choose a
        # final state, touch an image operator, or append the exact-once ledger.
        if self._layout_preflight_depth > 0:
            self.set_xy(self.l_margin, y + height + 2.4)
            self._check_visual_deadline()
            return

        if any(item.visual_id == visual_id for item in self._rendered_visuals):
            raise VisualFidelityError("render_failed")

        chosen_png = record.fallback_png
        chosen_state = "sketch_source_fallback"
        if record.primary_png is not None:
            primary_ok = False
            try:
                self._decode_visual_png(
                    record.primary_png,
                    record.width_px,
                    record.height_px,
                    "render_failed",
                )
                self._check_visual_deadline()
                self._preflight_visual_png(record.primary_png, width, height)
                self._check_visual_deadline()
                primary_ok = True
            except VisualFidelityError as error:
                self._check_visual_deadline()
                if error.code == "deadline_exceeded":
                    raise
            except Exception:
                self._check_visual_deadline()
                primary_ok = False
            if primary_ok:
                chosen_png = record.primary_png
                chosen_state = "verified_redraw"

        if chosen_state == "sketch_source_fallback":
            try:
                self._decode_visual_png(
                    record.fallback_png,
                    record.width_px,
                    record.height_px,
                    "fallback_damaged",
                )
                self._check_visual_deadline()
                self._preflight_visual_png(record.fallback_png, width, height)
                self._check_visual_deadline()
            except VisualFidelityError as error:
                self._check_visual_deadline()
                if error.code == "deadline_exceeded":
                    raise
                raise VisualFidelityError("render_failed") from None
            except Exception:
                self._check_visual_deadline()
                raise VisualFidelityError("render_failed") from None

        # Revalidate immediately before the sole real write.  A surprising
        # post-preflight failure aborts the document; cursor rollback is unsafe.
        try:
            self._decode_visual_png(
                chosen_png,
                record.width_px,
                record.height_px,
                "render_failed",
            )
            self._check_visual_deadline()
            self.image(
                io.BytesIO(chosen_png),
                x=x,
                y=y,
                w=width,
                h=height,
            )
            self._check_visual_deadline()
        except VisualFidelityError:
            raise
        except Exception:
            raise VisualFidelityError("render_failed") from None

        self._rendered_visuals.append(RenderedVisual(
            visual_id=visual_id,
            state=chosen_state,
            page_number=self.page_no(),
            x_mm=x,
            y_mm=y,
            width_mm=width,
            height_mm=height,
        ))
        self.set_xy(self.l_margin, y + height + 2.4)
        self._check_visual_deadline()

    def _assert_visual_render_integrity(self) -> None:
        """Require exact manifest order, count one, and terminal final states."""
        if self._visual_manifest is None:
            return
        self._check_visual_deadline()
        expected = manifest_ids(self._visual_manifest)
        if any(
            not isinstance(item, RenderedVisual)
            for item in self._rendered_visuals
        ):
            raise VisualFidelityError("render_failed")
        actual = tuple(item.visual_id for item in self._rendered_visuals)
        if (
            actual != expected
            or len(actual) != len(set(actual))
            or any(
                item.state not in (
                    "verified_redraw",
                    "sketch_source_fallback",
                )
                for item in self._rendered_visuals
            )
        ):
            raise VisualFidelityError("render_failed")
        self._check_visual_deadline()

    def _wide_image(self, png: bytes) -> None:
        """Render a centred full-width diagram before its explanatory bullets."""
        try:
            available_h = self.page_break_trigger - self.get_y() - 18.0
            if available_h < 56.0:
                self._break()
                available_h = self.page_break_trigger - self.get_y() - 18.0
            iw, ih = self._img_size(
                png, self.epw - 8.0, max(56.0, min(108.0, available_h))
            )
        except Exception:  # noqa: BLE001
            return
        x = self.l_margin + (self.epw - iw) / 2
        y = self.get_y() + 1.0
        self.image(io.BytesIO(png), x=x, y=y, w=iw, h=ih)
        self.set_xy(self.l_margin, y + ih + 2.4)

    def _sparkle(self, cx: float, cy: float, r: float) -> None:
        self.set_draw_color(*STAR_GOLD)
        self.set_line_width(0.45)
        self.line(cx - r, cy, cx + r, cy)
        self.line(cx, cy - r, cx, cy + r)
        self.line(cx - r * 0.6, cy - r * 0.6, cx + r * 0.6, cy + r * 0.6)
        self.line(cx - r * 0.6, cy + r * 0.6, cx + r * 0.6, cy - r * 0.6)

    def _where_list(self, items) -> None:
        """Neat "Where," legend under a formula."""
        items = [i for i in items if (i.symbol or i.meaning)]
        if not items:
            return
        self.set_font("Patrick", "", 12)
        self.set_text_color(*INK)
        if self.get_y() + 6 > self.page_break_trigger:
            self._break()
        self.set_xy(self.l_margin + 4, self.get_y())
        self.cell(0, 5.4, "Where,", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        for it in items:
            sym = (it.symbol or "").strip()
            mean = (it.meaning or "").strip()
            line = f"{sym} = {mean}" if sym else mean
            if self.get_y() + 6 > self.page_break_trigger:
                self._break()
            self._flow_text(line, self.l_margin + 10, self.epw - 12,
                            11.5, 13, (70, 70, 82))
        if self.get_y() + 1.0 <= self.page_break_trigger:
            self.ln(1)

    def _key_terms_box(self, terms) -> None:
        """Dashed KEY TERMS glossary box (the reference's purple panel)."""
        terms = [t for t in (terms or []) if (t or "").strip()]
        if not terms:
            return
        pad_x, pad_y = 3.0, 2.0
        w = self.epw
        head_h = 5.4
        bottom_reserve = 5.2
        columns, layouts = self._key_terms_grid(terms, w - 2 * pad_x)
        if columns > 1:
            self._key_terms_rows(layouts, columns, w, pad_x, pad_y, head_h,
                                 bottom_reserve)
            return
        term_index = line_index = 0

        while term_index < len(layouts):
            x, y = self.l_margin, self.get_y() + 0.8
            available = (
                self.page_break_trigger - y - bottom_reserve
                - head_h - 2 * pad_y
            )
            slices: list[tuple[int, int, int, float]] = []
            used = 0.0
            next_term, next_line = term_index, line_index
            while next_term < len(layouts):
                layout = layouts[next_term]
                if next_line >= len(layout["lines"]):
                    next_term += 1
                    next_line = 0
                    continue
                end, height = self._rich_line_slice(
                    layout, next_line, available - used,
                )
                if end == next_line:
                    break
                slices.append((next_term, next_line, end, height))
                used += height
                if end == len(layout["lines"]):
                    next_term += 1
                    next_line = 0
                else:
                    next_line = end
                    break

            if not slices:
                self._break()
                continue

            h = head_h + used + 2 * pad_y
            self._key_terms_frame(x, y, w, h, pad_y, head_h)
            ty = y + pad_y + head_h
            for index, start, end, _height in slices:
                if start == 0:
                    self._sparkle(x + pad_x + 0.6, ty + 2.0, 1.4)
                ty += self._draw_rich_lines(
                    layouts[index], layouts[index]["lines"][start:end],
                    x + pad_x + 3.5, ty, INK,
                )
            term_index, line_index = next_term, next_line
            complete = term_index >= len(layouts)
            self.set_xy(self.l_margin, y + h + (1.6 if complete else 0.6))
            if not complete:
                self._break()

    def _key_terms_frame(self, x: float, y: float, w: float, h: float,
                         pad_y: float, head_h: float) -> None:
        """The dashed purple panel and its KEY TERMS caption."""
        self.set_draw_color(124, 78, 168)
        self.set_fill_color(250, 247, 255)
        self.set_line_width(0.45)
        try:
            self.set_dash_pattern(dash=1.6, gap=1.2)
            self.rect(x, y, w, h, style="DF", round_corners=True,
                      corner_radius=2.0)
        except Exception:  # noqa: BLE001
            self.rect(x, y, w, h, style="DF")
        finally:
            try:
                self.set_dash_pattern()
            except Exception:  # noqa: BLE001
                pass
        self.set_font("Patrick", "", 12)
        self.set_text_color(124, 78, 168)
        self.set_xy(x, y + pad_y - 0.6)
        self.cell(w, head_h, "KEY TERMS", align="C")

    def _key_terms_grid(self, terms, avail_w: float) -> tuple[int, list[dict]]:
        """Pick 1-3 glossary columns: short terms (a word list) share rows.

        A multi-column grid is used only when every term stays within two
        lines in its cell and the box comes out clearly shorter than a
        single column; long definitions keep the full width.
        """
        def grid_height(layouts, columns):
            return sum(
                max(self._rich_lines_height(l["lines"], l["gap"])
                    for l in layouts[i:i + columns])
                for i in range(0, len(layouts), columns)
            )

        best_columns = 1
        best_layouts = [self._rich_layout(t, avail_w - 3.5, 11.5, 12.5) for t in terms]
        best_height = grid_height(best_layouts, 1)
        for columns in (2, 3):
            if len(terms) < columns:
                break
            text_w = (avail_w - (columns - 1) * self.KEY_TERM_COL_GAP) / columns - 3.5
            if text_w < self.KEY_TERM_MIN_COL_W:
                break
            layouts = [self._rich_layout(t, text_w, 11.5, 12.5) for t in terms]
            if any(len(l["lines"]) > 2 for l in layouts):
                break
            height = grid_height(layouts, columns)
            if height <= 0.8 * best_height:
                best_columns, best_layouts, best_height = columns, layouts, height
        return best_columns, best_layouts

    def _key_terms_rows(self, layouts, columns: int, w: float, pad_x: float,
                        pad_y: float, head_h: float, bottom_reserve: float) -> None:
        """Draw glossary terms row by row in ``columns`` cells, paginating whole rows."""
        gap = self.KEY_TERM_COL_GAP
        cell_w = (w - 2 * pad_x - (columns - 1) * gap) / columns
        rows = [layouts[i:i + columns] for i in range(0, len(layouts), columns)]
        heights = [
            max(self._rich_lines_height(l["lines"], l["gap"]) for l in row)
            for row in rows
        ]
        index = 0
        while index < len(rows):
            x, y = self.l_margin, self.get_y() + 0.8
            available = (
                self.page_break_trigger - y - bottom_reserve
                - head_h - 2 * pad_y
            )
            end, used = index, 0.0
            while end < len(rows) and used + heights[end] <= available + 1e-7:
                used += heights[end]
                end += 1
            if end == index:
                self._break()
                continue

            h = head_h + used + 2 * pad_y
            self._key_terms_frame(x, y, w, h, pad_y, head_h)
            ty = y + pad_y + head_h
            for row, row_h in zip(rows[index:end], heights[index:end]):
                for column, layout in enumerate(row):
                    cx = x + pad_x + column * (cell_w + gap)
                    self._sparkle(cx + 0.6, ty + 2.0, 1.4)
                    self._draw_rich_lines(layout, layout["lines"], cx + 3.5,
                                          ty, INK)
                ty += row_h
            index = end
            complete = index >= len(rows)
            self.set_xy(self.l_margin, y + h + (1.6 if complete else 0.6))
            if not complete:
                self._break()

    def _note_box(self, text: str) -> None:
        """Sticky-note callout for Note: / Important! / Key Difference:."""
        text = (text or "").strip()
        if not text:
            return
        pad_x, pad_y, icon_w = 2.6, 1.6, 6.4
        w = self.epw * 0.92
        inner_w = w - icon_w - 2 * pad_x
        layout = self._rich_layout(text, inner_w, 12, 13.5)
        start = 0
        bottom_reserve = 5.2

        while start < len(layout["lines"]):
            x = self.l_margin + (self.epw - w) / 2
            y = self.get_y() + 0.6
            available = (
                self.page_break_trigger - y - bottom_reserve - 2 * pad_y
            )
            end, used = self._rich_line_slice(layout, start, available)
            if end == start:
                self._break()
                continue

            h = used + 2 * pad_y
            self.set_draw_color(*NOTE_EDGE)
            self.set_fill_color(*NOTE_FILL)
            self.set_line_width(0.4)
            try:
                self.set_dash_pattern(dash=1.2, gap=1.0)
                self.rect(x, y, w, h, style="DF")
            except Exception:  # noqa: BLE001
                self.rect(x, y, w, h, style="DF")
            finally:
                try:
                    self.set_dash_pattern()
                except Exception:  # noqa: BLE001
                    pass
            if doodles is not None:
                try:
                    doodles.draw_icon(
                        self, "bulb", x + pad_x + 1.9, y + h / 2, 5.2,
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._draw_rich_lines(
                layout, layout["lines"][start:end],
                x + icon_w + pad_x, y + pad_y, (64, 66, 78),
            )
            start = end
            self.set_xy(
                self.l_margin,
                y + h + (1.8 if start == len(layout["lines"]) else 0.6),
            )
            if start < len(layout["lines"]):
                self._break()

    def _compare_columns(self, cmp) -> None:
        """Two pastel side-by-side cards (Primary vs Secondary style)."""
        gap = 4.0
        col_w = (self.epw - gap) / 2
        pad_x, pad_y = 2.6, 1.8
        head_pt, body_pt, math_pt = 12.5, 11.5, 13
        cols = [
            (cmp.left_title, cmp.left_points, SECTION_PASTELS[0]),
            (cmp.right_title, cmp.right_points, SECTION_PASTELS[3]),
        ]
        title_layouts = [
            self._rich_layout((title or "").upper(), col_w - 2 * pad_x,
                              head_pt, math_pt)
            for title, _points, _palette in cols
        ]
        point_layouts = [
            [
                self._rich_layout(point or "", col_w - 2 * pad_x - 4.0,
                                  body_pt, math_pt)
                for point in (points or [])
            ]
            for _title, points, _palette in cols
        ]
        title_pending = [True, True]
        point_indices = [0, 0]
        line_indices = [0, 0]
        bottom_reserve = 6.2

        def done(side: int) -> bool:
            return (
                not title_pending[side]
                and point_indices[side] >= len(point_layouts[side])
            )

        def plan_side(side: int, capacity: float) -> dict:
            used = 0.0
            draw_title = False
            point_slices: list[tuple[int, int, int, float]] = []
            next_point = point_indices[side]
            next_line = line_indices[side]
            if title_pending[side]:
                title_h = self._rich_lines_height(
                    title_layouts[side]["lines"], title_layouts[side]["gap"],
                ) + 0.6
                if title_h > capacity + 1e-7:
                    return {
                        "used": 0.0, "title": False, "slices": [],
                        "point": next_point, "line": next_line,
                    }
                used += title_h
                draw_title = True

            while next_point < len(point_layouts[side]):
                layout = point_layouts[side][next_point]
                if next_line >= len(layout["lines"]):
                    next_point += 1
                    next_line = 0
                    continue
                end, height = self._rich_line_slice(
                    layout, next_line, capacity - used,
                )
                if end == next_line:
                    break
                point_slices.append((next_point, next_line, end, height))
                used += height
                if end == len(layout["lines"]):
                    next_point += 1
                    next_line = 0
                else:
                    next_line = end
                    break
            return {
                "used": used,
                "title": draw_title,
                "slices": point_slices,
                "point": next_point,
                "line": next_line,
            }

        self.ln(1)
        while not all(done(side) for side in range(2)):
            y = self.get_y()
            capacity = (
                self.page_break_trigger - y - bottom_reserve - 2 * pad_y
            )
            plans = [plan_side(side, capacity) for side in range(2)]
            if not any(plan["title"] or plan["slices"] for plan in plans):
                self._break()
                continue

            h = max(plan["used"] for plan in plans) + 2 * pad_y
            for side, (_title, _points, (fill, ink)) in enumerate(cols):
                x = self.l_margin + side * (col_w + gap)
                self._crayon_box(x, y, col_w, h, fill, BOX_INK)
                ty = y + pad_y
                plan = plans[side]
                if plan["title"]:
                    ty += self._draw_rich_lines(
                        title_layouts[side], title_layouts[side]["lines"],
                        x + pad_x, ty, ink,
                    ) + 0.6
                for point_index, start, end, _height in plan["slices"]:
                    if start == 0:
                        self.set_fill_color(*BOX_INK)
                        self.ellipse(
                            x + pad_x + 0.6, ty + 1.9, 1.3, 1.3, style="F",
                        )
                    layout = point_layouts[side][point_index]
                    ty += self._draw_rich_lines(
                        layout, layout["lines"][start:end],
                        x + pad_x + 4.0, ty, INK,
                    )
                if plan["title"]:
                    title_pending[side] = False
                point_indices[side] = plan["point"]
                line_indices[side] = plan["line"]

            complete = all(done(side) for side in range(2))
            self.set_xy(self.l_margin, y + h + (2.4 if complete else 0.6))
            if not complete:
                self._break()

    def _diagram_box(self, instruction: str) -> None:
        """Labelled draw-it-yourself area for a diagram."""
        instruction = (instruction or "").strip()
        if not instruction:
            return
        pad, h_box = 2.6, 30.0
        cap_h = self._measure_rich(instruction, self.epw * 0.8 - 2 * pad, 11, 12)
        h = h_box + cap_h + 2 * pad
        if self.get_y() + h + 2 > self.page_break_trigger:
            self._break()
        w = self.epw * 0.8
        x, y = self.l_margin + (self.epw - w) / 2, self.get_y() + 1.0
        self.set_draw_color(150, 158, 176)
        self.set_line_width(0.45)
        try:
            self.set_dash_pattern(dash=2.0, gap=1.6)
            self.rect(x, y, w, h, style="D")
        except Exception:  # noqa: BLE001
            self.rect(x, y, w, h, style="D")
        finally:
            try:
                self.set_dash_pattern()
            except Exception:  # noqa: BLE001
                pass
        self.set_font("Patrick", "", 10.5)
        self.set_text_color(120, 128, 146)
        self.set_xy(x, y + 1.4)
        self.cell(w, 5.0, "DIAGRAM — draw here", align="C")
        self._draw_rich(instruction, x + pad, y + h_box,
                        w - 2 * pad, 11, 12, (110, 116, 134))
        self.set_xy(self.l_margin, y + h + 2.2)

    # ---------------- table ----------------
    def _render_table(self, table) -> None:
        # Widest row wins, so ragged rows never silently lose cells.
        ncols = max([len(table.headers)] + [len(r) for r in table.rows] + [1])
        col_w = self.epw / ncols
        pad, vpad = 1.8, 1.1
        body_pt, math_pt = 11.5, 12.5
        bottom_reserve = 5.2
        HDR_FILL = (226, 233, 246)

        def layouts_for(cells, pt) -> list[dict]:
            return [
                self._rich_layout(
                    cells[index] if index < len(cells) else "",
                    col_w - 2 * pad, pt, math_pt,
                )
                for index in range(ncols)
            ]

        header_layouts = layouts_for(table.headers, 12.5)
        header_h = max(
            (self._rich_lines_height(layout["lines"], layout["gap"])
             for layout in header_layouts),
            default=0.0,
        ) + 2 * vpad

        def draw_segment(layouts, starts, ends, pt, fill_rgb,
                         text_rgb, segment_h) -> None:
            x, y = self.l_margin, self.get_y()
            self.set_draw_color(170, 180, 205)
            self.set_line_width(0.25)
            for i in range(ncols):
                self.set_fill_color(*fill_rgb)
                self.rect(x + i * col_w, y, col_w, segment_h, style="DF")
            for i in range(ncols):
                self._draw_rich_lines(
                    layouts[i], layouts[i]["lines"][starts[i]:ends[i]],
                    x + i * col_w + pad, y + vpad, text_rgb,
                )
            self.set_xy(self.l_margin, y + segment_h)

        def draw_header() -> None:
            if self.get_y() + header_h + bottom_reserve > self.page_break_trigger:
                self._break()
            starts = [0] * ncols
            ends = [len(layout["lines"]) for layout in header_layouts]
            draw_segment(
                header_layouts, starts, ends, 12.5,
                HDR_FILL, HEAD_BLUE, header_h,
            )

        self.ln(0.8)
        draw_header()
        for idx, row in enumerate(table.rows):
            fill = (255, 255, 255) if idx % 2 == 0 else (246, 248, 252)
            layouts = layouts_for(row, body_pt)
            starts = [0] * ncols
            line_counts = [len(layout["lines"]) for layout in layouts]

            # Even a completely blank source row retains a visible bounded row.
            if not any(line_counts):
                if self.get_y() + 2 * vpad + bottom_reserve > self.page_break_trigger:
                    self._break()
                    draw_header()
                draw_segment(
                    layouts, starts, starts, body_pt, fill, INK, 2 * vpad,
                )
                continue

            while any(start < count for start, count in zip(starts, line_counts)):
                available = (
                    self.page_break_trigger - self.get_y()
                    - bottom_reserve - 2 * vpad
                )
                ends: list[int] = []
                heights: list[float] = []
                for layout, start in zip(layouts, starts):
                    end, used = self._rich_line_slice(layout, start, available)
                    ends.append(end)
                    heights.append(used)

                if ends == starts:
                    self._break()
                    draw_header()
                    continue

                segment_h = max(heights, default=0.0) + 2 * vpad
                draw_segment(
                    layouts, starts, ends, body_pt, fill, INK, segment_h,
                )
                starts = ends
                if any(start < count for start, count in zip(starts, line_counts)):
                    self._break()
                    draw_header()
        if self.get_y() + 2.0 <= self.page_break_trigger:
            self.ln(2)

    # ---------------- graph ----------------
    def _render_graph(self, graph) -> None:
        try:
            result = graph_render.render_graph(graph)
        except Exception:  # noqa: BLE001 - a bad graph never kills the notes
            result = None
        if not result:
            return
        png, w_pt, h_pt = result
        w_mm = min(w_pt * PT_TO_MM, self.epw * (0.62 if self._full_width else 1.0))
        h_mm = h_pt * PT_TO_MM * (w_mm / (w_pt * PT_TO_MM))
        # Shrink rather than push a graph onto a fresh (mostly empty) page.
        room = self.page_break_trigger - self.get_y() - 3.0
        if h_mm > room:
            if room > 45.0:
                scale = room / h_mm
                w_mm, h_mm = w_mm * scale, room
            else:
                self._break()
        self.ln(0.8)
        x = self.l_margin + (self.epw - w_mm) / 2
        self.image(io.BytesIO(png), x=x, y=self.get_y(), w=w_mm, h=h_mm)
        self.set_xy(self.l_margin, self.get_y() + h_mm + 1.8)

    # ---------------- flow chart ----------------
    def _render_flow(self, steps: list[str]) -> None:
        steps = [s.strip() for s in steps if s and s.strip()]
        if not steps:
            return
        body_pt, math_pt = 11.5, 12.5
        pad, vpad = 2.2, 1.4
        arrow_w = 8.0
        max_box_w = self.epw * 0.46

        # Pre-measure each step box.
        boxes = []
        for s in steps:
            atoms = self._atoms(s, body_pt, math_pt)
            self.set_font("Patrick", "", body_pt)
            sp = self.get_string_width(" ")
            natural = 0.0
            for a in atoms:
                natural += sp if a.get("sp") else a["w"]
            w = min(natural + 2 * pad + 0.8, max_box_w)
            h = self._measure_rich(s, w - 2 * pad, body_pt, math_pt) + 2 * vpad
            boxes.append((s, w, h))

        self.ln(1)
        x = self.l_margin
        row_y = self.get_y()
        row_h = 0.0
        prev_h = 0.0  # height of the immediately-preceding box in this row
        for i, (s, w, h) in enumerate(boxes):
            need = w if i == 0 else arrow_w + w
            if x > self.l_margin and x + need > self.l_margin + self.epw:
                row_y += row_h + 3.0
                x = self.l_margin
                row_h = 0.0
                prev_h = 0.0
            if row_y + h > self.page_break_trigger:
                self._break()
                row_y = self.get_y()
                x = self.l_margin
                row_h = 0.0
                prev_h = 0.0
            if x > self.l_margin:  # arrow from previous box in this row
                # Centre on the ADJACENT box pair (not the row max), so the
                # arrow always meets both boxes' edges.
                ay = row_y + min(prev_h or h, h) / 2
                self.set_draw_color(120, 140, 180)
                self.set_line_width(0.45)
                self.line(x + 1.0, ay, x + arrow_w - 2.2, ay)
                self.set_fill_color(120, 140, 180)
                self.polygon([(x + arrow_w - 2.2, ay - 1.4),
                              (x + arrow_w - 2.2, ay + 1.4),
                              (x + arrow_w - 0.4, ay)], style="F")
                x += arrow_w
            self.set_draw_color(120, 140, 180)
            self.set_line_width(0.35)
            self.set_fill_color(240, 245, 252)
            self.rect(x, row_y, w, h, style="DF")
            self._draw_rich(s, x + pad, row_y + vpad, w - 2 * pad,
                            body_pt, math_pt, INK)
            x += w
            row_h = max(row_h, h)
            prev_h = h
        self.set_xy(self.l_margin, row_y + row_h + 2.5)


def build_notes_pdf(notes: ShortNotes, signature: str,
                    watermark: bool = True,
                    logo_watermark: bool = False,
                    show_page_numbers: bool = True) -> bytes:
    from .sanskrit import require_sanskrit, SanskritOutputError, has_latin
    require_sanskrit(notes)
    if getattr(notes, "output_language", "") == "sanskrit":
        from sheets_bridge import pdf_fonts
        if not pdf_fonts.install():
            raise SanskritOutputError()
        # Fail instead of silently falling back to unshaped Devanagari.
        import uharfbuzz  # noqa: F401
    memory_candidate = getattr(notes, "_visual_memory_budget", None)
    memory = (
        memory_candidate
        if isinstance(memory_candidate, VisualMemoryBudget)
        else None
    )
    close_attempted = False
    try:
        pdf = NotesPDF(
            notes,
            signature,
            watermark=watermark,
            logo_watermark=logo_watermark,
            show_page_numbers=show_page_numbers,
        )
        pdf._check_visual_deadline()
        pdf.add_page()
        pdf.render_body()
        pdf._check_visual_deadline()
        pdf._assert_visual_render_integrity()
        pdf._check_visual_deadline()
        output = bytes(pdf.output())
        # This local check runs for desktop, web and cached re-renders, even
        # when optional model QC is off. Never deliver the reported artifacts.
        if rendered_notation_findings(output):
            from .errors import NotesQualityError
            raise NotesQualityError()
        if getattr(notes, "output_language", "") == "sanskrit":
            from .sanskrit import map_shaping_only_glyphs
            output = map_shaping_only_glyphs(pdf, output)
            from pypdf import PdfReader
            if any(has_latin(page.extract_text() or "") for page in PdfReader(io.BytesIO(output)).pages):
                raise SanskritOutputError()
        pdf._check_visual_deadline()
        if memory is not None:
            close_attempted = True
            memory.close()
        if pdf._visual_manifest is not None:
            verified = sum(
                item.state == "verified_redraw"
                for item in pdf._rendered_visuals
            )
            fallback = sum(
                item.state == "sketch_source_fallback"
                for item in pdf._rendered_visuals
            )
            payload = {
                "manifest": len(pdf._visual_assets),
                "rendered": len(pdf._rendered_visuals),
                "verified_redraw": verified,
                "sketch_source_fallback": fallback,
            }
            print(
                "[notesbot.visual-render] "
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        return output
    finally:
        if memory is not None and not close_attempted:
            close_attempted = True
            memory.close()
