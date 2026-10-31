"""Diagrams drawn as vectors, like the samples' TikZ ones: a tree (a hierarchy:
root box above its children, arrows down) and a flow (steps joined by arrows,
or a cycle round an ellipse). Labels are Computer Modern Sans, like the samples.

Each function returns (width, height, draw(x, y)) or None when it cannot fit;
the caller then writes the same content as a list instead."""
from __future__ import annotations

import math

NAVY = (32, 74, 135)
ROOT_FILL, NODE_FILL = (210, 219, 231), (244, 246, 249)
LINE, RADIUS, PAD_X, PAD_Y, ARROW = 0.9, 4.0, 7.0, 5.0, 4.5
SIZES = (11.0, 10.0, 9.0, 8.0)


class _Box:
    def __init__(self, canvas, label: str, size: float, bold: bool, max_w: float):
        self.label, self.size, self.bold = label, size, bold
        self.lines = canvas.layout(label, max_w - 2 * PAD_X, size=size, family="cmss", bold=bold, align="C",
                                   leading=size * 1.25)
        self.w = max(l.natural for l in self.lines) + 2 * PAD_X
        self.h = sum(l.height for l in self.lines) + 2 * PAD_Y
        self.x = self.y = 0.0

    def draw(self, canvas, fill):
        pdf = canvas.pdf
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(LINE)
        pdf.rect(self.x, self.y, self.w, self.h, style="DF", round_corners=True, corner_radius=RADIUS)
        top = self.y + PAD_Y
        for line in self.lines:
            canvas.draw_line(line, self.x + PAD_X, top + line.ascent, self.w - 2 * PAD_X, "C")
            top += line.height

    def edge_point(self, toward_x: float, toward_y: float) -> tuple[float, float]:
        """Where a line from this box's centre toward a point leaves the box."""
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        dx, dy = toward_x - cx, toward_y - cy
        if not dx and not dy:
            return cx, cy
        scale = min(abs(self.w / 2 / dx) if dx else math.inf, abs(self.h / 2 / dy) if dy else math.inf)
        return cx + dx * scale, cy + dy * scale


def arrow(pdf, x1: float, y1: float, x2: float, y2: float) -> None:
    """A line with a filled arrowhead at (x2, y2)."""
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    bx, by = x2 - ux * ARROW, y2 - uy * ARROW
    pdf.set_draw_color(*NAVY)
    pdf.set_fill_color(*NAVY)
    pdf.set_line_width(LINE)
    pdf.line(x1, y1, bx, by)
    half = ARROW * 0.45
    pdf.polygon([(x2, y2), (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)], style="F")


def tree(canvas, nodes: list[dict], width: float):
    """A layered tree, each parent centred over its children."""
    children: dict[str, list[dict]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        if node["parent"] in children:
            children[node["parent"]].append(node)
    root = next(n for n in nodes if not n["parent"])
    for size in SIZES:
        boxes = {n["id"]: _Box(canvas, n["label"], size, n is root, 118.0) for n in nodes}
        gap_x, gap_y = size * 1.1, size * 2.4
        span: dict[str, float] = {}

        def measure(node):
            kids = children[node["id"]]
            inner = sum(measure(k) for k in kids) + gap_x * max(0, len(kids) - 1)
            span[node["id"]] = max(boxes[node["id"]].w, inner)
            return span[node["id"]]

        total = measure(root)
        if total <= width:
            break
    else:
        return None
    depth_h: dict[int, float] = {}

    def level_heights(node, depth=0):
        depth_h[depth] = max(depth_h.get(depth, 0.0), boxes[node["id"]].h)
        for kid in children[node["id"]]:
            level_heights(kid, depth + 1)

    level_heights(root)
    tops = [0.0]
    for depth in range(1, len(depth_h)):
        tops.append(tops[-1] + depth_h[depth - 1] + gap_y)
    height = tops[-1] + depth_h[len(depth_h) - 1]

    def place(node, left, depth=0):
        box = boxes[node["id"]]
        box.x, box.y = left + (span[node["id"]] - box.w) / 2, tops[depth] + (depth_h[depth] - box.h) / 2
        kids = children[node["id"]]
        inner = sum(span[k["id"]] for k in kids) + gap_x * max(0, len(kids) - 1)
        cursor = left + (span[node["id"]] - inner) / 2
        for kid in kids:
            place(kid, cursor, depth + 1)
            cursor += span[kid["id"]] + gap_x

    place(root, 0.0)

    def draw(x: float, y: float) -> None:
        for box in boxes.values():
            box.x, box.y = box.x + x, box.y + y
        for node in nodes:
            if node["parent"] in boxes:
                parent, child = boxes[node["parent"]], boxes[node["id"]]
                arrow(canvas.pdf, parent.x + parent.w / 2, parent.y + parent.h, child.x + child.w / 2, child.y)
        for node in nodes:
            boxes[node["id"]].draw(canvas, ROOT_FILL if node is root else NODE_FILL)
        for box in boxes.values():
            box.x, box.y = box.x - x, box.y - y

    return total, height, draw


def flow(canvas, steps: list[str], width: float, cycle: bool):
    """Steps in a row when they fit, otherwise in a column; a cycle round an ellipse."""
    if cycle and 3 <= len(steps) <= 6:
        return _cycle(canvas, steps, width)
    for size in SIZES[:2]:
        boxes = [_Box(canvas, s, size, False, 112.0) for s in steps]
        gap = size * 2.2
        row = sum(b.w for b in boxes) + gap * (len(boxes) - 1)
        if row <= width:
            height = max(b.h for b in boxes)
            cursor = 0.0
            for box in boxes:
                box.x, box.y = cursor, (height - box.h) / 2
                cursor += box.w + gap
            return row, height, _drawer(canvas, boxes, cycle=False)
    boxes = [_Box(canvas, s, SIZES[0], False, min(width, 300.0)) for s in steps]
    column, gap = max(b.w for b in boxes), SIZES[0] * 1.6
    cursor = 0.0
    for box in boxes:
        box.x, box.y = (column - box.w) / 2, cursor
        cursor += box.h + gap
    return column, cursor - gap, _drawer(canvas, boxes, cycle=False)


def _cycle(canvas, steps: list[str], width: float):
    for size in SIZES:
        boxes = [_Box(canvas, s, size, False, 104.0) for s in steps]
        widest, tallest = max(b.w for b in boxes), max(b.h for b in boxes)
        rx = min(width - widest, 330.0) / 2
        ry = max(tallest * 1.6, 24.0 * len(steps) / 2 + tallest)
        if rx >= widest * 0.75:
            break
    else:
        return None
    total_w, total_h = 2 * rx + widest, 2 * ry + tallest
    for n, box in enumerate(boxes):
        angle = -math.pi / 2 + 2 * math.pi * n / len(boxes)
        cx, cy = total_w / 2 + rx * math.cos(angle), total_h / 2 + ry * math.sin(angle)
        box.x, box.y = cx - box.w / 2, cy - box.h / 2
    return total_w, total_h, _drawer(canvas, boxes, cycle=True)


def _drawer(canvas, boxes: list[_Box], *, cycle: bool):
    def draw(x: float, y: float) -> None:
        for box in boxes:
            box.x, box.y = box.x + x, box.y + y
        pairs = list(zip(boxes, boxes[1:])) + ([(boxes[-1], boxes[0])] if cycle else [])
        for a, b in pairs:
            ax, ay = a.edge_point(b.x + b.w / 2, b.y + b.h / 2)
            bx, by = b.edge_point(a.x + a.w / 2, a.y + a.h / 2)
            length = math.hypot(bx - ax, by - ay) or 1
            pad = 2.0
            arrow(canvas.pdf, ax + (bx - ax) / length * pad, ay + (by - ay) / length * pad,
                  bx - (bx - ax) / length * pad, by - (by - ay) / length * pad)
        for box in boxes:
            box.draw(canvas, NODE_FILL)
        for box in boxes:
            box.x, box.y = box.x - x, box.y - y

    return draw
