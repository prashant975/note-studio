"""Large review tables retain their formatting without quadratic cell access."""
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.table import Table

from src import docx_writer


def test_large_report_table_resolves_the_grid_once_and_keeps_every_cell(monkeypatch):
    original = Table._cells.fget
    calls = []

    def counted(table):
        calls.append(1)
        return original(table)

    monkeypatch.setattr(Table, "_cells", property(counted))
    rows = [[f"Point {r}, field {c}" for c in range(7)] for r in range(66)]
    # A short row still receives the empty cells needed for a rectangular table.
    rows[-1] = ["Final point"]
    table = docx_writer._add_table(Document(), rows)
    assert len(calls) == 1
    cells = original(table)
    assert cells[0].text == "Point 0, field 0"
    assert cells[7].text == "Point 1, field 0"
    assert cells[-7].text == "Final point"
    assert all(not c.text for c in cells[-6:])
    assert cells[0].paragraphs[0].runs[0].bold
    assert not cells[7].paragraphs[0].runs[0].bold
    assert cells[0]._tc.tcPr.find(qn("w:shd")).get(qn("w:fill")) == docx_writer.TABLE_HEADER_FILL


def test_cached_font_properties_are_independent_between_runs():
    doc = Document()
    p = doc.add_paragraph()
    a, b = p.add_run("first"), p.add_run("second")
    options = dict(size=Pt(13), bold=True, italic=True, color=RGBColor(0x12, 0x34, 0x56))
    docx_writer._set_run_font(a, **options)
    docx_writer._set_run_font(b, **options)
    a.font.highlight_color = WD_COLOR_INDEX.YELLOW
    a.bold = False
    assert b.bold and b.italic
    assert b.font.size.pt == 13
    assert b.font.highlight_color is None
    assert b.font.color.rgb == options["color"]
    assert b._element.rPr.rFonts.get(qn("w:cs")) == docx_writer.DEVANAGARI_FONT


def test_formatting_an_existing_run_preserves_its_other_properties():
    p = Document().add_paragraph()
    run = p.add_run("a reviewed statement")
    run.font.strike = True
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    docx_writer._set_run_font(run, size=Pt(11), bold=False)
    assert run.font.strike
    assert run.font.highlight_color == WD_COLOR_INDEX.YELLOW
    assert run.font.size.pt == 11
