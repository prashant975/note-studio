"""Gujarati-only typography and pagination hooks for the native PDF renderer."""
from __future__ import annotations

import re


_NUMBER = re.compile(r"[+\-−]?[0-9૦-૯]+(?:[.,:/\-–][0-9૦-૯]+)*(?:\s*[:/]\s*[0-9૦-૯]+)?\s*(?:\\?%)?")


def prose_math(text):
    """Years, percentages and currency abbreviations use the surrounding face."""
    text = re.sub(r"\\(?:mathrm|text)\s*\{US(?:\s|\\\s)*\}", "US", text)
    def replace(match):
        value = match.group(1).strip()
        if _NUMBER.fullmatch(value):
            return value.replace(r"\%", "%")
        if value == "US":
            return "US"
        return match.group(0)
    # Match inline spans only. Never unwrap the inside of display $$...$$.
    return re.sub(r"(?<!\$)\$(?!\$)([^$\n]+)\$(?!\$)", replace, text)


def _table(pdf, table, original):
    ncols = max([len(table.headers)] + [len(row) for row in table.rows] + [1])
    col_w, pad, vpad, reserve = pdf.epw / ncols, 1.8, 1.1, 5.2

    def layouts(cells, size):
        return [pdf._rich_layout(cells[i] if i < len(cells) else "",
                                 col_w - 2 * pad, size, 12.5) for i in range(ncols)]

    def height(row):
        return max((pdf._rich_lines_height(cell["lines"], cell["gap"]) for cell in row), default=0) + 2 * vpad

    header = layouts(table.headers, 12.5)
    rows = [layouts(row, 11.5) for row in table.rows]
    header_h = height(header)
    fresh_room = pdf.page_break_trigger - pdf._body_top - reserve - 1.0
    # Exceptionally tall source rows retain the engine's line-slicing fallback.
    # Ordinary rows are always indivisible, including their name/first column.
    if any(header_h + height(row) > fresh_room for row in rows) or header_h > fresh_room:
        return original(pdf, table)

    def draw(row, fill, ink):
        x, y, h = pdf.l_margin, pdf.get_y(), height(row)
        pdf.set_draw_color(170, 180, 205)
        pdf.set_line_width(.25)
        for i, cell in enumerate(row):
            pdf.set_fill_color(*fill)
            pdf.rect(x + i * col_w, y, col_w, h, style="DF")
            pdf._draw_rich_lines(cell, cell["lines"], x + i * col_w + pad, y + vpad, ink)
        pdf.set_xy(pdf.l_margin, y + h)

    total = header_h + sum(height(row) for row in rows)
    lead = total if total <= fresh_room else header_h + (height(rows[0]) if rows else 0)
    if pdf.get_y() + .8 + lead + reserve > pdf.page_break_trigger:
        pdf._break()
    pdf.ln(.8)
    draw(header, (226, 233, 246), (35, 69, 120))
    for index, row in enumerate(rows):
        if pdf.get_y() + height(row) + reserve > pdf.page_break_trigger:
            pdf._break()
            draw(header, (226, 233, 246), (35, 69, 120))
        draw(row, (255, 255, 255) if index % 2 == 0 else (246, 248, 252), (26, 42, 74))
    if pdf.get_y() + 2 <= pdf.page_break_trigger:
        pdf.ln(2)


def _glossary(pdf, terms):
    terms = [term for term in terms or [] if term and term.strip()]
    if not terms:
        return
    pad_x, pad_y, reserve = 3.0, 2.0, 5.2
    width = pdf.epw
    columns, layouts = pdf._key_terms_grid(terms, width - 2 * pad_x)
    cell_w = (width - 2 * pad_x - (columns - 1) * pdf.KEY_TERM_COL_GAP) / columns
    rows = [layouts[i:i + columns] for i in range(0, len(layouts), columns)]
    index, start, first = 0, 0, True
    while index < len(rows):
        head_h = 5.4 if first else 0.0
        x, y = pdf.l_margin, pdf.get_y() + .8
        available = pdf.page_break_trigger - y - reserve - head_h - 2 * pad_y
        slices, used = [], 0.0
        while index < len(rows):
            row = rows[index]
            row_h = max(pdf._rich_lines_height(cell["lines"][start:], cell["gap"]) for cell in row)
            fresh_room = pdf.page_break_trigger - pdf._body_top - reserve - head_h - 2 * pad_y - .8
            if row_h > available - used:
                if row_h <= fresh_room or columns > 1:
                    break
                end, row_h = pdf._rich_line_slice(row[0], start, available - used)
                if end == start:
                    break
                slices.append((row, start, end, row_h))
                used += row_h
                start = end
                if start == len(row[0]["lines"]):
                    index += 1
                    start = 0
                break
            slices.append((row, start, None, row_h))
            used += row_h
            index += 1
            start = 0
        if not slices:
            pdf._break()
            continue
        height = head_h + used + 2 * pad_y
        if first:
            pdf._key_terms_frame(x, y, width, height, pad_y, head_h)
        else:
            pdf.set_draw_color(124, 78, 168)
            pdf.set_fill_color(250, 247, 255)
            pdf.set_line_width(.45)
            pdf.set_dash_pattern(dash=1.6, gap=1.2)
            try:
                pdf.rect(x, y, width, height, style="DF", round_corners=True, corner_radius=2)
            finally:
                pdf.set_dash_pattern()
        ty = y + pad_y + head_h
        for row, begin, end, row_h in slices:
            for column, cell in enumerate(row):
                cx = x + pad_x + column * (cell_w + pdf.KEY_TERM_COL_GAP)
                if begin == 0:
                    pdf._sparkle(cx + .6, ty + 2, 1.4)
                pdf._draw_rich_lines(cell, cell["lines"][begin:end], cx + 3.5, ty, (26, 42, 74))
            ty += row_h
        first = False
        complete = index >= len(rows)
        pdf.set_xy(pdf.l_margin, y + height + (1.6 if complete else .6))
        if not complete:
            pdf._break()


def _flow(pdf, steps):
    """A connected vertical sequence; wrapping never loses the previous arrow."""
    steps = [step.strip() for step in steps or [] if step and step.strip()]
    if not steps:
        return
    width, pad, vpad, gap = pdf.epw * .88, 2.2, 1.4, 5.0
    layouts = [pdf._rich_layout(step, width - 2 * pad, 11.5, 12.5) for step in steps]
    heights = [pdf._rich_lines_height(row["lines"], row["gap"]) + 2 * vpad for row in layouts]
    fresh_room = pdf.page_break_trigger - pdf._body_top - 5.2
    total = sum(heights) + gap * (len(steps) - 1) + 1
    if total <= fresh_room and pdf.get_y() + total + 5.2 > pdf.page_break_trigger:
        pdf._break()
    pdf.ln(1)
    for index, (layout, height) in enumerate(zip(layouts, heights)):
        if height > fresh_room:
            # A pathologically long label uses the engine's bounded text flow.
            pdf._flow_text(steps[index], pdf.l_margin + pad, width - 2 * pad, 11.5, 12.5, (26, 42, 74))
            continue
        if pdf.get_y() + height + (gap if index else 0) + 5.2 > pdf.page_break_trigger:
            pdf._break()
        x, y = pdf.l_margin + (pdf.epw - width) / 2, pdf.get_y()
        pdf.set_draw_color(120, 140, 180)
        pdf.set_line_width(.35)
        if index:
            centre = x + width / 2
            pdf.line(centre, y, centre, y + gap - 1.5)
            pdf.set_fill_color(120, 140, 180)
            pdf.polygon([(centre - 1.3, y + gap - 2), (centre + 1.3, y + gap - 2),
                         (centre, y + gap - .3)], style="F")
            y += gap
        pdf.set_fill_color(240, 245, 252)
        pdf.rect(x, y, width, height, style="DF")
        pdf._draw_rich_lines(layout, layout["lines"], x + pad, y + vpad, (26, 42, 74))
        pdf.set_xy(pdf.l_margin, y + height)
    pdf.ln(2.5)


def install(notes_pdf):
    cls = notes_pdf.NotesPDF
    original_atoms, original_font = cls._atoms, cls.set_font
    original_table, original_terms, original_flow = cls._render_table, cls._key_terms_box, cls._render_flow
    original_heading = cls._section_heading

    def atoms(pdf, text, body_pt, math_pt):
        if pdf.language == "gujarati":
            text = prose_math(text)
        return original_atoms(pdf, text, body_pt, math_pt)

    def font(pdf, family=None, style="", size=0):
        if (getattr(pdf, "language", "") == "gujarati" and family
                and family.lower() == "patrick" and "gujarati" in pdf.fonts):
            family = "Gujarati"
        return original_font(pdf, family, style, size)

    def table(pdf, value):
        return _table(pdf, value, original_table) if pdf.language == "gujarati" else original_table(pdf, value)

    def terms(pdf, value):
        return _glossary(pdf, value) if pdf.language == "gujarati" else original_terms(pdf, value)

    def flow(pdf, value):
        return _flow(pdf, value) if pdf.language == "gujarati" else original_flow(pdf, value)

    def heading(pdf, text, icon=""):
        if pdf.language == "gujarati":
            from .gujarati_editorial import clean_heading
            text = clean_heading(text)
        return original_heading(pdf, text, icon)

    cls._atoms, cls.set_font = atoms, font
    cls._render_table, cls._key_terms_box, cls._render_flow = table, terms, flow
    cls._section_heading = heading
