"""Make the studygram PDF able to print Devanagari.

The renderer in ``notesbot.notes_pdf`` is Latin-only: it registers a single
handwriting font (Patrick Hand) and its text sanitiser drops every character
at or above U+0250 — which silently removes all Hindi, so a Hindi lecture came
out with only the English words and empty bullets.

The repo already ships Kalam (a Devanagari handwriting face) in ``fonts/``, it
was simply never registered. This module wires it in **at runtime** so the
generator package itself stays unmodified:

* Kalam is registered and set as the fallback face, so every glyph Patrick Hand
  lacks is drawn by Kalam in a matching handwritten style;
* HarfBuzz shaping is switched on for every Devanagari draw, without which
  conjuncts and matras are placed as loose glyphs. Both painters need it: the
  body word painter (``_draw_word``) and the cell()/multi_cell() calls behind
  the title chip, the running header and the mind-map nodes. Fixing only the
  first left the same word looking different in a heading than in the body;
* the sanitiser keeps Indic characters (and the joiners conjuncts need) while
  performing all of its other substitutions exactly as before.

Layout, colours, cards, doodles and watermarks are untouched.
"""
from __future__ import annotations

import os
import re
import threading

_LOCK = threading.Lock()
_INSTALLED = False

# Kept in addition to everything the original filter keeps (ord < 0x250).
_EXTRA_RANGES = (
    (0x0900, 0x097F),   # Devanagari
    (0xA8E0, 0xA8FF),   # Devanagari Extended
    (0x1CD0, 0x1CFF),   # Vedic Extensions
    (0x0980, 0x09FF),   # Bengali
    (0x0A80, 0x0AFF),   # Gujarati
    (0x0B80, 0x0BFF),   # Tamil
    (0x0C00, 0x0C7F),   # Telugu
    (0x0C80, 0x0CFF),   # Kannada
    (0x0D00, 0x0D7F),   # Malayalam
)
_EXTRA_CHARS = "\u200c\u200d–—…"      # ZWNJ / ZWJ hold conjuncts together


_INDIC_RE = re.compile(r"[ऀ-ॿ꣠-ꣿ᳐-᳿ঀ-෿]")
_SHAPER: dict = {}


def _shaped_width_mm(font_path: str, text: str, size_mm: float) -> float | None:
    """Width of a shaped run, in millimetres.

    fpdf2's ``get_string_width`` sums per-character advances, which
    over-measures Devanagari because shaping merges consonant clusters into
    single conjunct glyphs. Measuring what will actually be drawn keeps word
    spacing tight.
    """
    try:
        import uharfbuzz as hb
    except ImportError:
        return None
    try:
        entry = _SHAPER.get(font_path)
        if entry is None:
            face = hb.Face(hb.Blob.from_file_path(font_path))
            shaper = hb.Font(face)
            shaper.scale = (face.upem, face.upem)
            entry = _SHAPER[font_path] = (shaper, face.upem)
        shaper, upem = entry
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        hb.shape(shaper, buffer)
        advance = sum(position.x_advance for position in buffer.glyph_positions)
        return advance / upem * size_mm
    except Exception:  # noqa: BLE001 - fall back to the unshaped measurement
        return None


import re as _re
import threading as _threading

_RENDER = _threading.local()

# The renderer prints a few fixed English labels. In a Hindi document they are
# the only English left on the page, so they are swapped at draw time. Widths
# are measured through the same swap, so layout stays consistent.
HINDI_LABELS = {
    "KEY TERMS": "मुख्य शब्द",
    "Where,": "जहाँ,",
    "Mind Map — Quick Revision": "माइंड मैप — त्वरित पुनरावृत्ति",
    "DIAGRAM — draw here": "चित्र — यहाँ बनाएँ",
}
_PAGE_RE = _re.compile(r"^Page (\d+)$")
SANSKRIT_LABELS = {
    "KEY TERMS": "शब्दार्थाः",
    "Where,": "अत्र,",
    "Mind Map — Quick Revision": "पुनरावृत्तिः",
    "DIAGRAM — draw here": "अत्र चित्रं लिखत",
    "Lecture": "पाठः",
}


def set_render_language(language: str) -> None:
    """Tell the draw-time label swap which language the document is in."""
    _RENDER.language = (language or "").strip().lower()


def render_language() -> str:
    return getattr(_RENDER, "language", "") or ""


def localise_label(text, language=None):
    """The Hindi form of a fixed renderer label, or the text unchanged."""
    language = render_language() if language is None else language
    if not isinstance(text, str):
        return text
    if language == "sanskrit":
        from notesbot.sanskrit import devanagari_number
        m = _PAGE_RE.match(text)
        return devanagari_number(f"पृष्ठम् {m.group(1)}" if m else SANSKRIT_LABELS.get(text, text))
    if language != "hindi":
        return text
    if text in HINDI_LABELS:
        return HINDI_LABELS[text]
    m = _PAGE_RE.match(text)
    return f"पृष्ठ {m.group(1)}" if m else text


def _is_indic(text: str) -> bool:
    return bool(_INDIC_RE.search(text or ""))


def _font_for(text: str) -> str:
    """Kalam draws the Indic glyphs; Patrick Hand keeps the Latin look."""
    return "Kalam" if _is_indic(text) else "Patrick"


def _keep(character: str) -> bool:
    code = ord(character)
    if character == "\n" or character in "_^{}" or code < 0x250 or character in _EXTRA_CHARS:
        return True
    return any(low <= code <= high for low, high in _EXTRA_RANGES)


def install() -> bool:
    """Register Kalam + shaping and widen the sanitiser. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        from notesbot import notes_pdf

        regular = os.path.join(notes_pdf.FONT_DIR, "Kalam-Regular.ttf")
        bold = os.path.join(notes_pdf.FONT_DIR, "Kalam-Bold.ttf")
        if not os.path.isfile(regular):
            return False

        # 1) keep Indic text instead of deleting it
        original_sanitize = notes_pdf._sanitize_text

        def sanitize(text: str) -> str:
            if not text:
                return ""
            text = re.sub(r"\\?xrightarrow\s*\{([^}]*)\}", r" ->[\1] ", text)
            text = re.sub(r"(?<![A-Za-z])\\?(?:xrightarrow|rightarrow)(?![A-Za-z])", " -> ", text)
            text = re.sub(r"\\to(?![A-Za-z])", " -> ", text)
            text = re.sub(r"\\?rightleftharpoons", " <=> ", text)
            text = text.replace("`", "")
            for bad, good in notes_pdf._REPLACEMENTS.items():
                text = text.replace(bad, good)
            text = "".join(notes_pdf._SUBS.get(c, notes_pdf._SUPS.get(c, c)) for c in text)
            return "".join(c for c in text if _keep(c))

        sanitize.__wrapped__ = original_sanitize
        notes_pdf._sanitize_text = sanitize

        # 2) register the Devanagari handwriting face
        original_init = notes_pdf.NotesPDF.__init__

        def __init__(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            try:
                self.add_font("Kalam", "", regular)
                self.add_font("Kalam", "B", bold if os.path.isfile(bold) else regular)
                self.set_fallback_fonts(["Kalam"])   # covers cell()/write() paths
            except Exception:  # noqa: BLE001 - never break a render over fonts
                pass

        notes_pdf.NotesPDF.__init__ = __init__

        # 3) draw Devanagari words through a shaping-capable call.
        #
        # The body renderer uses FPDF.text(), which applies neither font
        # fallback nor text shaping: Devanagari came out either blank or with
        # matras in the wrong place ("प्रतरिक्षी" instead of "प्रतिरक्षी").
        # cell() shapes correctly, and its baseline sits at
        # y + 0.5*h + 0.3*font_size, so the word lands exactly where text()
        # would have put it. Latin words keep the original text() path, so
        # nothing about the existing look changes.
        original_word_width = notes_pdf.NotesPDF._word_width
        original_draw_word = notes_pdf.NotesPDF._draw_word
        script_scale = notes_pdf._SCRIPT_SCALE
        pt_to_mm = notes_pdf.PT_TO_MM

        def _word_width(self, pieces, body_pt):
            if not any(_is_indic(piece) for piece, _mode in pieces):
                return original_word_width(self, pieces, body_pt)
            width = 0.0
            for piece, mode in pieces:
                size = body_pt if mode == "n" else body_pt * script_scale
                font = _font_for(piece)
                self.set_font(font, "", size)
                shaped = _shaped_width_mm(regular, piece, self.font_size) if font == "Kalam" else None
                width += shaped if shaped is not None else self.get_string_width(piece)
            self.set_font("Patrick", "", body_pt)
            return width

        def _draw_word(self, x, baseline, pieces, body_pt):
            if not any(_is_indic(piece) for piece, _mode in pieces):
                return original_draw_word(self, x, baseline, pieces, body_pt)
            sub_dy = body_pt * pt_to_mm * 0.17
            sup_dy = -body_pt * pt_to_mm * 0.33
            auto_break, self.auto_page_break = self.auto_page_break, False
            shaping = getattr(self, "text_shaping", None)
            # cell() insets its text by c_margin; the body layout positions
            # words itself, so that inset has to be zero or every Devanagari
            # word drifts right and swallows the space after it.
            cell_margin, self.c_margin = self.c_margin, 0
            try:
                for piece, mode in pieces:
                    size = body_pt if mode == "n" else body_pt * script_scale
                    offset = 0.0 if mode == "n" else (sub_dy if mode == "sub" else sup_dy)
                    font = _font_for(piece)
                    self.set_font(font, "", size)
                    if font == "Patrick":
                        self.text(x, baseline + offset, piece)
                    else:
                        try:
                            self.set_text_shaping(True)
                        except Exception:  # noqa: BLE001 - no uharfbuzz: unshaped, but drawn
                            pass
                        saved = (self.x, self.y)
                        height = self.font_size
                        drawn = _shaped_width_mm(regular, piece, height)
                        if drawn is None:
                            drawn = self.get_string_width(piece)
                        self.set_xy(x, baseline + offset - 0.8 * height)
                        self.cell(drawn + 0.5, height, piece, border=0, align="L")
                        self.set_xy(*saved)
                        self.set_text_shaping(bool(shaping))
                        x += drawn
                        continue
                    x += self.get_string_width(piece)
            finally:
                self.auto_page_break = auto_break
                self.c_margin = cell_margin
                self.set_font("Patrick", "", body_pt)

        notes_pdf.NotesPDF._word_width = _word_width
        notes_pdf.NotesPDF._draw_word = _draw_word

        # 4) shape Devanagari in the *other* drawing paths too.
        #
        # Only the body flows through _draw_word. The title chip, the slim
        # running header, section headings, table cells, the mind-map nodes and
        # the "Where," / "KEY TERMS" blocks are drawn with cell()/multi_cell(),
        # which apply the fallback face - so the glyphs do appear - but fpdf2
        # shapes only when text shaping is switched on, and it is off by
        # default. Unshaped Devanagari keeps codepoint order: conjuncts never
        # form and every virama prints as a naked halant, so the title drew
        # "स्वास्थ्य" as स् वा स् थ् य while the body drew the same word
        # correctly - the same word in two different formats on one page.
        #
        # Shaping is switched on per call and only for strings that actually
        # contain Indic text, so every Latin measurement and draw keeps the
        # exact metrics it had before. Measurement (dry-run multi_cell,
        # get_string_width) is wrapped as well: fpdf2 sums per-character
        # advances when unshaped, which over-measures a conjunct cluster, and
        # measuring differently from drawing is what makes text drift or wrap
        # a word early.
        def _shaped_call(original, index, keyword):
            def wrapper(self, *args, **kwargs):
                piece = kwargs.get(keyword)
                if piece is None and len(args) > index:
                    piece = args[index]
                swapped = localise_label(piece, getattr(self, "language", "") or None)
                if swapped is not piece:
                    piece = swapped
                    if keyword in kwargs:
                        kwargs[keyword] = swapped
                    else:
                        args = args[:index] + (swapped,) + args[index + 1:]
                indic = isinstance(piece, str) and _is_indic(piece)
                if not indic:
                    if getattr(self, "_after_fallback_run", False):
                        self._after_fallback_run = False
                        _reselect_base_font(self)
                    return original(self, *args, **kwargs)
                self._after_fallback_run = True
                previous = bool(getattr(self, "text_shaping", None))
                try:
                    self.set_text_shaping(True)
                except Exception:  # noqa: BLE001 - no uharfbuzz: draw as before
                    return original(self, *args, **kwargs)
                try:
                    return original(self, *args, **kwargs)
                finally:
                    try:
                        self.set_text_shaping(previous)
                    except Exception:  # noqa: BLE001 - never break a render
                        pass

            return wrapper

        # 5) put the base face back after a fallback run.
        #
        # Drawing Devanagari selects the FALLBACK face in the content stream.
        # Asking for the base family again is a no-op inside fpdf2 - family,
        # style and size all still match - so no font operator is written and
        # the next, purely Latin, run is drawn with the fallback still
        # selected: its glyph ids resolve through Kalam and Latin comes out as
        # Devanagari. That is what turned the mind-map centre's second line,
        # "Reproduction)", into nonsense while the Devanagari line above it was
        # perfect. Bouncing through the other embedded face forces the operator
        # out. Only done on the first Latin draw after an Indic one, so ordinary
        # Latin documents are untouched.
        _base_set_font = notes_pdf.NotesPDF.set_font

        def _reselect_base_font(pdf) -> None:
            family = pdf.font_family or "Patrick"
            style = pdf.font_style or ""
            size = pdf.font_size_pt or 10
            other = "Kalam" if family.lower() != "kalam" else "Patrick"
            try:
                _base_set_font(pdf, other, "", size)
                _base_set_font(pdf, family, style, size)
            except Exception:  # noqa: BLE001 - never break a render over this
                pass

        for _name, _index, _keyword in (
            ("cell", 2, "text"),
            ("multi_cell", 2, "text"),
            ("text", 2, "text"),
            ("get_string_width", 0, "s"),
        ):
            setattr(
                notes_pdf.NotesPDF,
                _name,
                _shaped_call(getattr(notes_pdf.NotesPDF, _name), _index, _keyword),
            )

        _INSTALLED = True
        return True


def shaping_available() -> bool:
    try:
        import uharfbuzz  # noqa: F401
    except ImportError:
        return False
    return True
