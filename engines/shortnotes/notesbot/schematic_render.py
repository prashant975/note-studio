"""Render bounded, declarative teaching diagrams as PDF vectors (no model call)."""
from __future__ import annotations

import math


def render_schematic(pdf, diagram) -> None:
    """Use the PDF's rich text engine so labels share its math and Hindi fonts."""
    width = pdf.epw - 10
    height = min(72.0, width * 0.45)
    caption_h = pdf._measure_rich(diagram.caption, width, 11, 12) if diagram.caption else 0
    if pdf.get_y() + height + caption_h + 8 > pdf.page_break_trigger:
        pdf._break()
    left = pdf.l_margin + 5
    top = pdf.get_y() + 3
    # Inner drawing plane leaves room for labels at the outer coordinates.
    x0, y0 = left + 12, top + 8
    plane_w, plane_h = width - 24, height - 16

    def point(p):
        return x0 + p.x * plane_w / 100, y0 + p.y * plane_h / 100

    with pdf.local_context():
        pdf.set_draw_color(26, 42, 74)
        pdf.set_line_width(0.45)
        for stroke in diagram.strokes:
            pdf.set_dash_pattern(dash=1.4, gap=1) if stroke.dashed else pdf.set_dash_pattern()
            vertices = [point(p) for p in stroke.points]
            if stroke.kind == "ellipse":
                (xa, ya), (xb, yb) = vertices[:2]
                if xa != xb and ya != yb:
                    pdf.ellipse(min(xa, xb), min(ya, yb), abs(xb - xa), abs(yb - ya))
                continue
            for (xa, ya), (xb, yb) in zip(vertices, vertices[1:]):
                pdf.line(xa, ya, xb, yb)
            if stroke.kind == "arrow":
                (xa, ya), (xb, yb) = vertices[-2:]
                if (xa, ya) != (xb, yb):
                    angle = math.atan2(yb - ya, xb - xa)
                    pdf.set_dash_pattern()
                    for offset in (-0.45, 0.45):
                        pdf.line(xb, yb, xb - 2.5 * math.cos(angle + offset),
                                 yb - 2.5 * math.sin(angle + offset))
        pdf.set_dash_pattern()
        for label in diagram.labels:
            cx, cy = point(label)
            label_w = min(width, max(24.0, pdf._measure_line_width(label.text, 10, 11) + 2))
            label_h = pdf._measure_rich(label.text, label_w, 10, 11)
            lx = max(left, min(cx - label_w / 2, left + width - label_w))
            ly = max(top, min(cy - label_h / 2, top + height - label_h))
            pdf._draw_rich_aligned(label.text, lx, ly, label_w, 10, 11,
                                   (26, 42, 74), align="C")
    pdf.set_xy(pdf.l_margin, top + height)
    if diagram.caption:
        pdf._flow_text(diagram.caption, left, width, 11, 12, (26, 42, 74))
    pdf.ln(3)
