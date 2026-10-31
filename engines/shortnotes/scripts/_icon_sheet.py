"""Render every doodle icon in a labelled grid to out_icons.pdf (visual check)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpdf import FPDF

from notesbot.doodles import ICONS, draw_icon, pick_icon

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "out_icons.pdf")

COLS = 4
CELL_W, CELL_H = 46, 40
MARGIN_X, MARGIN_Y = 13, 18


def main() -> None:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(30, 30, 30)
    pdf.set_xy(MARGIN_X, 8)
    pdf.cell(0, 8, "doodles.py icon sheet")

    for i, name in enumerate(ICONS):
        col, row = i % COLS, i // COLS
        cx = MARGIN_X + col * CELL_W + CELL_W / 2
        cy = MARGIN_Y + row * CELL_H + CELL_H / 2 - 4

        # cell frame + icon box guide (light grey)
        pdf.set_draw_color(210, 210, 210)
        pdf.set_line_width(0.2)
        pdf.rect(MARGIN_X + col * CELL_W, MARGIN_Y + row * CELL_H, CELL_W, CELL_H)

        # draw at two sizes: big (24 mm, for inspection) is centred,
        # small (8 mm, real usage) sits at bottom-right of the cell
        for (icx, icy, size) in ((cx - 3, cy, 24), (cx + 16, cy + 10, 8)):
            pdf.set_draw_color(228, 228, 228)
            pdf.set_line_width(0.15)
            pdf.rect(icx - size / 2, icy - size / 2, size, size)
            draw_icon(pdf, name, icx, icy, size)

        # label
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(60, 60, 60)
        pdf.set_xy(MARGIN_X + col * CELL_W, MARGIN_Y + row * CELL_H + CELL_H - 6)
        pdf.cell(CELL_W, 5, name, align="C")

    # quick pick_icon sanity check, printed to console
    tests = {
        "Electrolysis of brine": "battery",
        "Acid-Base Titration": "flask",
        "Quantum numbers & orbitals": "atom",
        "Enthalpy of formation": "bolt",
        "Photochemical reactions": "bulb",
        "Chemical Equilibrium": "balance",
        "Thermodynamics basics": "fire",
        "Magnetic properties": "magnet",
        "Hydrolysis of salts": "drop",
        "Organic reaction mechanism": "test_tube",
        "Raoult's law": "book",
        "Chemical kinetics": "gear",
        "Mole concept formulas": "star",
        "Random heading xyz": None,
    }
    ok = True
    for text, want in tests.items():
        got = pick_icon(text)
        if got != want:
            ok = False
            print(f"pick_icon MISMATCH: {text!r} -> {got!r}, expected {want!r}")
    print("pick_icon checks:", "all OK" if ok else "FAILED")

    pdf.output(OUT)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
