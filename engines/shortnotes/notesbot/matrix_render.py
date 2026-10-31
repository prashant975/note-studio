"""Typeset LaTeX matrix environments using the existing mathtext cell renderer.

No external TeX process: retain rows, columns and delimiters, then compose
transparent cell images. Unsupported/malformed structures fail to the caller.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

_ENV = re.compile(
    r"(?P<left>\\left\s*[\[|(])?\s*"
    r"\\begin\{(?P<kind>matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|smallmatrix|array|aligned|align\*?|gathered|cases)\}"
    r"(?P<body>.*?)\\end\{(?P=kind)\}"
    r"\s*(?P<right>\\right\s*[\]|)])?", re.S,
)
_INK = (26, 42, 74, 255)


def _row_cells(body: str) -> list[list[str]]:
    rows, cells, buffer, depth = [], [], [], 0
    index = 0
    while index < len(body):
        char = body[index]
        if char == "{" and (index == 0 or body[index - 1] != "\\"):
            depth += 1
        elif char == "}" and (index == 0 or body[index - 1] != "\\"):
            depth -= 1
        if depth == 0 and (char == "&" or body[index:index + 2] == "\\\\"):
            cells.append("".join(buffer).strip())
            buffer = []
            if char != "&":
                rows.append(cells)
                cells = []
                index += 1
        else:
            buffer.append(char)
        index += 1
    if buffer or cells:
        cells.append("".join(buffer).strip())
        rows.append(cells)
    if depth or not rows or len(rows) > 40 or max(map(len, rows)) > 40:
        raise ValueError("Invalid or oversized matrix")
    return rows


def render_expression(latex: str, fontsize_pt: float, render_cell, dpi: int) -> Image.Image:
    """Render a matrix or a chain of equations containing multiple matrices."""
    def cell(value):
        if not value.strip():
            return Image.new("RGBA", (1, max(1, round(fontsize_pt * dpi / 72))))
        png, _, _ = render_cell(value, fontsize_pt)
        return Image.open(io.BytesIO(png)).convert("RGBA")

    pieces = []
    end = 0
    for match in _ENV.finditer(latex):
        if latex[end:match.start()].strip():
            pieces.append(cell(latex[end:match.start()]))
        kind, body = match["kind"], match["body"]
        if kind == "array":
            spec = re.match(r"\s*\{([lcr| ]+)\}", body)
            if not spec:
                raise ValueError("Unsupported array column specification")
            body = body[spec.end():]
        rows = _row_cells(body)
        cols = max(map(len, rows))
        if kind not in ("aligned", "align", "align*", "gathered", "cases") and any(len(row) != cols for row in rows):
            raise ValueError("Unequal matrix row lengths")
        rendered = [[cell(value) for value in row] for row in rows]
        widths = [max((row[col].width for row in rendered if col < len(row)), default=1) for col in range(cols)]
        heights = [max(item.height for item in row) for row in rendered]
        pad = max(5, round(fontsize_pt * dpi / 72 * .22))
        gutter = pad * 2
        w = sum(widths) + gutter * (cols - 1) + pad * 4
        h = sum(heights) + pad * (len(rows) + 1)
        canvas = Image.new("RGBA", (w, h))
        y = pad
        for row, row_h in zip(rendered, heights):
            x = pad * 2
            for col, item in enumerate(row):
                canvas.alpha_composite(item, (x + (widths[col] - item.width) // 2, y + (row_h - item.height) // 2))
                x += widths[col] + gutter
            y += row_h + pad
        delimiters = {"bmatrix": "[]", "pmatrix": "()", "Bmatrix": "{}", "vmatrix": "||", "Vmatrix": "‖‖", "cases": "{ "}.get(kind, "  ")
        if match["left"] and match["right"]:
            delimiters = match["left"][-1] + match["right"][-1]
        draw = ImageDraw.Draw(canvas)
        stroke = max(2, round(dpi / 120))
        for side, mark in enumerate(delimiters):
            x = 2 if side == 0 else w - 3
            inward = 1 if side == 0 else -1
            if mark in "[]":
                draw.line([(x + inward * pad, 2), (x, 2), (x, h - 3), (x + inward * pad, h - 3)], fill=_INK, width=stroke)
            elif mark in "|‖":
                draw.line((x, 2, x, h - 3), fill=_INK, width=stroke)
                if mark == "‖":
                    draw.line((x + inward * 5, 2, x + inward * 5, h - 3), fill=_INK, width=stroke)
            elif mark in "()":
                box = (x, 1, x + 2 * pad, h - 2) if side == 0 else (x - 2 * pad, 1, x, h - 2)
                draw.arc(box, 90 if side == 0 else -90, 270 if side == 0 else 90, fill=_INK, width=stroke)
            elif mark in "{}":
                draw.line([(x + inward * pad, 2), (x + inward * pad // 2, h // 5),
                           (x + inward * pad // 2, h * 2 // 5), (x, h // 2),
                           (x + inward * pad // 2, h * 3 // 5), (x + inward * pad // 2, h * 4 // 5),
                           (x + inward * pad, h - 3)], fill=_INK, width=stroke)
        pieces.append(canvas)
        end = match.end()
    if end == 0:
        raise ValueError("Unsupported matrix environment")
    if latex[end:].strip():
        pieces.append(cell(latex[end:]))
    gap = max(3, round(fontsize_pt * dpi / 72 * .08))
    out = Image.new("RGBA", (sum(p.width for p in pieces) + gap * (len(pieces) - 1), max(p.height for p in pieces)))
    x = 0
    for piece in pieces:
        out.alpha_composite(piece, (x, (out.height - piece.height) // 2))
        x += piece.width + gap
    return out
