"""Standalone exports retain content and never return a stale successful file."""
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from docx import Document
from docx.shared import Inches
from PIL import Image

from src import pdf_exporter

pytestmark = pytest.mark.real_pdf


def test_a_fresh_render_replaces_the_old_pdf_without_office(tmp_path, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("An Office/external process was started")

    monkeypatch.setattr(subprocess, "run", forbidden)
    path = tmp_path / "notes.docx"
    pdf = tmp_path / "notes.pdf"
    pdf.write_bytes(b"old PDF")
    doc = Document()
    doc.add_paragraph("New lecture content")
    doc.save(path)
    actual, error = pdf_exporter.export_docx_to_pdf(path, tmp_path)
    assert (actual, error) == (pdf, None)
    with fitz.open(pdf) as rendered:
        assert "New lecture content" in rendered[0].get_text()
    assert not list(tmp_path.glob(".pdf-*"))


def test_an_invalid_docx_does_not_return_or_delete_the_previous_pdf(tmp_path):
    docx = tmp_path / "notes.docx"
    docx.write_bytes(b"broken DOCX")
    previous = tmp_path / "notes.pdf"
    previous.write_bytes(b"previous export")
    pdf, error = pdf_exporter.export_docx_to_pdf(docx, tmp_path)
    assert pdf is None and error
    assert previous.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".pdf-*"))


def test_text_tables_merged_cells_images_and_page_breaks_survive(tmp_path):
    doc = Document()
    doc.add_heading("Legislative powers", 1)
    doc.add_paragraph("Indian Parliament: भारतीय संविधान और संसदीय प्रणाली।")
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Merged title"
    table.cell(1, 0).text = "Lok Sabha"
    table.cell(1, 1).text = "Represents the people"
    table.cell(2, 0).text = "Rajya Sabha"
    table.cell(2, 1).text = "Represents the states"
    image = tmp_path / "diagram.png"
    Image.new("RGB", (320, 160), "navy").save(image)
    doc.add_picture(str(image), width=Inches(3))
    doc.add_page_break()
    doc.add_paragraph("The second page starts here.")
    source = tmp_path / "content.docx"
    doc.save(source)
    pdf, error = pdf_exporter.export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) == 2
        text = rendered[0].get_text()
        assert "Merged title" in text and text.count("Merged title") == 1
        assert "भारतीय संविधान" in text
        assert "Represents the states" in text
        assert rendered[0].get_images()
        assert "second page starts here" in rendered[1].get_text()


def test_a_tall_table_row_splits_without_losing_its_end(tmp_path):
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Topic"
    table.cell(0, 1).text = "Explanation"
    table.cell(1, 0).text = "Review"
    table.cell(1, 1).text = ("Detailed discussion of constitutional accountability. " * 300) + "LAST POINT"
    source = tmp_path / "long.docx"
    doc.save(source)
    pdf, error = pdf_exporter.export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) > 1
        assert "LAST POINT" in " ".join(" ".join(p.get_text().split()) for p in rendered)


def test_dcn_notes_sidebar_and_headers_repeat_without_body_overlap(tmp_path):
    from src import config
    from src.docx_render_check import render_and_verify
    from src.upsc_dcn_docx_writer import write_upsc_dcn_docx

    notes = "#LECTURE_TITLE: Parliament\n#H1: Legislative powers\n" + (
        "- Parliament holds the executive accountable through debates and questions.\n" * 90)
    source, _ = write_upsc_dcn_docx(notes, tmp_path / "dcn.docx", language_code="en")
    result = render_and_verify(source, tmp_path / "rendered")
    assert result.ok and result.page_count > 1, result.warnings
    assert not result.overlap_pages
    with fitz.open(result.pdf_path) as rendered:
        for i, p in enumerate(rendered, 1):
            text = p.get_text()
            assert "Space for Notes" in text and "Daily Class Notes" in text
            assert "<font" not in text
            assert str(i) in text
            assert p.rect.width == pytest.approx(config.DCN_PAGE_WIDTH_IN * 72)


def test_parallel_exports_keep_each_lectures_content(tmp_path):
    sources = []
    for i in range(3):
        doc = Document()
        doc.add_paragraph(f"Unique lecture {i}")
        source = tmp_path / f"lecture_{i}.docx"
        doc.save(source)
        sources.append(source)
    with ThreadPoolExecutor(max_workers=3) as pool:
        outputs = list(pool.map(lambda p: pdf_exporter.export_docx_to_pdf(p, tmp_path), sources))
    for i, (pdf, error) in enumerate(outputs):
        assert error is None
        with fitz.open(pdf) as rendered:
            assert f"Unique lecture {i}" in rendered[0].get_text()


def test_handwritten_devanagari_copies_as_unicode_even_across_pages(tmp_path):
    from docx.shared import Pt

    doc = Document()
    p = doc.add_paragraph()
    sentence = "भारतीय संविधान और संसदीय प्रणाली जनता का प्रतिनिधित्व करते हैं। "
    run = p.add_run(sentence * 160 + "अंतिम बिंदु")
    run.font.name = "Kalam"
    run.font.size = Pt(12)
    source = tmp_path / "hindi.docx"
    doc.save(source)
    pdf, error = pdf_exporter.export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) > 1
        text = " ".join(" ".join(p.get_text().split()) for p in rendered)
        assert "भारतीय संविधान" in text and "अंतिम बिंदु" in text
        assert not any(0xF000 <= ord(c) <= 0xF8FF for c in text)
        assert text.count("अंतिम बिंदु") == 1


def test_legacy_footer_keeps_its_live_page_numbers(tmp_path):
    from src.docx_layout import setup_footer

    doc = Document()
    setup_footer(doc.sections[0])
    doc.add_paragraph("First page.")
    doc.add_page_break()
    doc.add_paragraph("Second page.")
    source = tmp_path / "legacy.docx"
    doc.save(source)
    pdf, error = pdf_exporter.export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert "[1]" in rendered[0].get_text()
        assert "[2]" in rendered[1].get_text()


@pytest.mark.parametrize("name,text", [
    ("Coming Soon", "CO₂, H₂O and ₹24 support legible scientific symbols."),
    ("Kalam", "संसद(Parliament) जनता का प्रतिनिधित्व करती है। CO₂ और H₂O।"),
])
def test_mixed_scripts_and_subscripts_do_not_break_font_shaping(tmp_path, name, text):
    doc = Document()
    doc.add_paragraph().add_run(text).font.name = name
    source = tmp_path / "symbols.docx"
    doc.save(source)
    pdf, error = pdf_exporter.export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        copied = " ".join(rendered[0].get_text().split())
        assert "CO₂" in copied and "H₂O" in copied
        if name == "Kalam":
            assert "संसद(Parliament)" in copied
