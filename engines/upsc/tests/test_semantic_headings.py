"""Heading semantics must survive the visual formatting of both DOCX writers."""
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from src.docx_writer import write_notes_docx
from src.upsc_dcn_docx_writer import (
    STYLE_H1,
    STYLE_H2,
    STYLE_H3,
    STYLE_H4,
    write_upsc_dcn_docx,
)


def test_legacy_notes_writer_uses_word_heading_one(tmp_path: Path):
    out, _ = write_notes_docx(
        "# Main topic\nA supporting explanation.",
        tmp_path / "notes.docx",
        [],
        run_dir=tmp_path,
    )
    doc = Document(out)
    heading = next(paragraph for paragraph in doc.paragraphs
                   if paragraph.text == "Main topic")
    assert heading.style.name == "Heading 1"


def test_dcn_headings_have_real_word_outline_levels(tmp_path: Path):
    out, _ = write_upsc_dcn_docx(
        "\n".join((
            "#LECTURE_TITLE: Heading test",
            "#H1: Main topic",
            "#H2: Section",
            "#H3: Subsection",
            "#H4: Detail",
        )),
        tmp_path / "dcn.docx",
    )
    doc = Document(out)
    for style_name, expected_level in (
        (STYLE_H1, "0"), (STYLE_H2, "1"),
        (STYLE_H3, "2"), (STYLE_H4, "3"),
    ):
        style = doc.styles[style_name]
        ppr = style.element.find(qn("w:pPr"))
        outline = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
        assert outline is not None, style_name
        assert outline.get(qn("w:val")) == expected_level
