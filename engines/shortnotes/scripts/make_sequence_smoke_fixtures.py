"""Create deterministic, non-private PDFs for release smoke testing."""
from __future__ import annotations

import argparse
from pathlib import Path

from fpdf import FPDF


MARKER = "SYNTHETIC RELEASE QA - NO PRIVATE CONTENT"
_FILENAMES = {
    "text-order": "text-order.pdf",
    "one-diagram": "one-diagram.pdf",
    "three-diagrams": "three-diagrams.pdf",
}


def _page(pdf: FPDF, title: str) -> None:
    pdf.add_page()
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 6, MARKER, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_text_color(20, 35, 60)
    pdf.set_font("Helvetica", style="B", size=18)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)


def _topic(pdf: FPDF, heading: str, point: str) -> None:
    pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 8, heading, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 7, point)
    pdf.ln(3)


def _arrow(pdf: FPDF, x1: float, y: float, x2: float) -> None:
    pdf.line(x1, y, x2, y)
    pdf.line(x2, y, x2 - 4, y - 3)
    pdf.line(x2, y, x2 - 4, y + 3)


def _diagram_page(pdf: FPDF, title: str, left: str, right: str) -> None:
    _page(pdf, title)
    pdf.set_draw_color(30, 65, 110)
    pdf.set_line_width(0.8)
    y = 75
    pdf.rect(28, y, 55, 32)
    pdf.rect(127, y, 55, 32)
    _arrow(pdf, 85, y + 16, 125)
    pdf.set_font("Helvetica", style="B", size=12)
    pdf.set_xy(28, y + 11)
    pdf.cell(55, 8, left, align="C")
    pdf.set_xy(127, y + 11)
    pdf.cell(55, 8, right, align="C")
    pdf.set_xy(50, 122)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(110, 7, f"Source-shown sequence: {left} to {right}", align="C")


def _write(pdf: FPDF, path: Path) -> None:
    pdf.output(str(path))


def build_fixtures(output_dir: Path) -> dict[str, Path]:
    """Create deterministic synthetic PDFs and return their exact paths."""
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    paths = {name: target / filename for name, filename in _FILENAMES.items()}

    text_order = FPDF()
    _page(text_order, "Teacher Sequence Check")
    _topic(text_order, "Topic C", "Teacher starts with the third catalogue topic.")
    _topic(text_order, "Topic A", "Teacher then explains the first catalogue topic.")
    _topic(text_order, "Topic B", "Teacher closes with the second catalogue topic.")
    _write(text_order, paths["text-order"])

    one_diagram = FPDF()
    _diagram_page(one_diagram, "Diagram One", "Node A", "Node B")
    _write(one_diagram, paths["one-diagram"])

    three_diagrams = FPDF()
    for index, (left, right) in enumerate(
        (("Input 1", "Output 1"), ("Input 2", "Output 2"), ("Input 3", "Output 3")),
        start=1,
    ):
        _diagram_page(three_diagrams, f"Diagram {index}", left, right)
    _write(three_diagrams, paths["three-diagrams"])

    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name, path in build_fixtures(args.output).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
