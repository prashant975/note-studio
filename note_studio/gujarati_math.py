"""Shape Gujarati formula labels while retaining the copied math renderer.

Mathtext handles symbols and numeric expressions. Gujarati labels use the
same font and HarfBuzz shaping as the notes, composed with fraction bars.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import re
import threading

_GUJARATI = re.compile(r"[\u0a80-\u0aff]")
_TEXT = re.compile(r"\\(?:text(?:rm|bf|it)?|mathrm|mathbf|mathit)\s*")
_FRACTION = re.compile(r"\\(?:dfrac|tfrac|frac)\s*")
_LOCK = threading.RLock()
_INSTALLED = False
_CACHE = {}
_INK = (26, 42, 74)


@dataclass
class Box:
    width: float
    up: float
    down: float
    marks: list


def _group(text, index):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "{":
        raise ValueError("A formula argument needs braces")
    start, depth = index + 1, 1
    index += 1
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in "{}":
            index += 2
            continue
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if not depth:
                return text[start:index], index + 1
        index += 1
    raise ValueError("Unclosed formula argument")


def _shift(marks, x, y):
    return [(kind, mx + x, my + y, data) for kind, mx, my, data in marks]


def _row(boxes, size):
    x, marks = 0.0, []
    gap = size * .16
    for box in boxes:
        marks.extend(_shift(box.marks, x, 0))
        x += box.width + gap
    return Box(max(0, x - gap), max((b.up for b in boxes), default=size),
               max((b.down for b in boxes), default=0), marks)


def _fraction(top, bottom, size):
    pad, gap, axis = size * .12, size * .13, -size * .22
    width = max(top.width, bottom.width) + pad * 2
    top_y = axis - gap - top.down
    bottom_y = axis + gap + bottom.up
    marks = _shift(top.marks, (width - top.width) / 2, top_y)
    marks += _shift(bottom.marks, (width - bottom.width) / 2, bottom_y)
    marks.append(("line", 0, axis, (width, max(.45, size * .045))))
    return Box(width, max(0, top.up - top_y), max(0, bottom_y + bottom.down), marks)


def _label(text):
    for command, replacement in ((r"\times", "×"), (r"\cdot", "·"), (r"\%", "%")):
        text = text.replace(command, replacement)
    text = re.sub(r"\\[ ,;:!]", " ", text)
    if "\\" in text or "{" in text or "}" in text:
        raise ValueError("Unsupported command inside a Gujarati label")
    return " ".join(text.split())


def _compose(text, size, measure, render_symbol, depth=0):
    if depth > 12:
        raise ValueError("Formula nesting is too deep")
    boxes, buffer, index = [], [], 0

    def flush():
        value = "".join(buffer).strip()
        buffer.clear()
        if value:
            png, width, height = render_symbol(value, size)
            # A cropped operator PNG has almost no ascent/descent. Align its
            # centre to the formula axis, rather than its bottom to the label
            # baseline (which made subtraction look like an underscore).
            operator = re.fullmatch(r"(?:[=+\-−<>]|\\(?:times|cdot|div|leq?|geq?|neq))\s*", value)
            up = height / 2 + size * .22 if operator else height * .75
            boxes.append(Box(width, up, height - up,
                             [("image", 0, -up, (png, width, height))]))

    def label(value):
        value = _label(value)
        measure.set_font("Gujarati", size=size)
        missing = {ord(character) for character in value if character not in "\u200c\u200d"} - set(measure.current_font.cmap)
        if missing:
            raise ValueError("A formula label contains glyphs missing from the Gujarati font")
        width = measure.get_string_width(value)
        boxes.append(Box(width, size * 1.12, size * .35, [("text", 0, 0, (value, size))]))

    while index < len(text):
        fraction = _FRACTION.match(text, index)
        if fraction:
            flush()
            numerator, after_top = _group(text, fraction.end())
            denominator, index = _group(text, after_top)
            child_size = size * .88
            boxes.append(_fraction(_compose(numerator, child_size, measure, render_symbol, depth + 1),
                                   _compose(denominator, child_size, measure, render_symbol, depth + 1), size))
            continue
        wrapper = _TEXT.match(text, index)
        if wrapper:
            value, end = _group(text, wrapper.end())
            if _GUJARATI.search(value):
                flush()
                label(value)
                index = end
                continue
            buffer.append(text[index:end])
            index = end
            continue
        if _GUJARATI.match(text, index):
            flush()
            end = index + 1
            while end < len(text) and (_GUJARATI.match(text, end) or text[end].isspace()):
                end += 1
            label(text[index:end])
            index = end
            continue
        buffer.append(text[index])
        index += 1
    flush()
    return _row(boxes, size)


def render(latex, size, font_path, render_symbol, normalize):
    """Return a transparent formula PNG and its physical size in points."""
    from fpdf import FPDF
    import pypdfium2 as pdfium
    from notesbot.pdfium_guard import PDFIUM_LOCK

    key = (latex, float(size), str(font_path))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        text = re.sub(r"\\(?:left|right)\s*", "", normalize(latex))
        pdf = FPDF(unit="pt")
        pdf.set_margins(0, 0, 0)
        pdf.set_auto_page_break(False)
        pdf.add_font("Gujarati", fname=str(font_path))
        pdf.set_text_shaping(True)
        box = _compose(text, float(size), pdf, render_symbol)
        pad = 2.0
        pdf.add_page(format=(max(1, box.width + pad * 2), max(1, box.up + box.down + pad * 2)))
        pdf.set_text_color(*_INK)
        pdf.set_draw_color(*_INK)
        baseline = pad + box.up
        for kind, x, y, data in box.marks:
            x, y = x + pad, y + baseline
            if kind == "text":
                value, fontsize = data
                pdf.set_font("Gujarati", size=fontsize)
                pdf.set_xy(x, y - fontsize * .8)
                pdf.cell(w=pdf.get_string_width(value) + .1, h=fontsize, text=value)
            elif kind == "image":
                png, width, height = data
                pdf.image(io.BytesIO(png), x=x, y=y, w=width, h=height)
            else:
                width, stroke = data
                pdf.set_line_width(stroke)
                pdf.line(x, y, x + width, y)
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(bytes(pdf.output()))
            try:
                page = document[0]
                try:
                    bitmap = page.render(scale=300 / 72, fill_color=(0, 0, 0, 0))
                    try:
                        image = bitmap.to_pil().convert("RGBA")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
        bbox = image.getbbox()
        if bbox:
            image = image.crop((max(0, bbox[0] - 4), max(0, bbox[1] - 4),
                                min(image.width, bbox[2] + 4), min(image.height, bbox[3] + 4)))
        output = io.BytesIO()
        image.save(output, format="PNG")
        result = (output.getvalue(), image.width * 72 / 300, image.height * 72 / 300)
        if len(_CACHE) < 256:
            _CACHE[key] = result
        return result


def install(font_path):
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        from notesbot import math_render
        original = math_render.render_latex

        def render_latex(latex, fontsize_pt=15.0):
            if not _GUJARATI.search(latex):
                return original(latex, fontsize_pt)
            return render(latex, fontsize_pt, font_path, original, math_render.normalize_latex)

        math_render.render_latex = render_latex
        _INSTALLED = True
