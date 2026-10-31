"""Keyword citations retain destinations, emphasis and correction shading."""
from pathlib import Path
import sys

import pytest
from docx import Document
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.upsc_dcn_docx_writer import (
    _Highlighter, _add_text_runs, visible_text, write_upsc_dcn_docx,
)


@pytest.mark.parametrize("markup,bold,italic", [
    ("[**Article 21**](https://example.org/constitution)", True, False),
    ("**[Article 21](https://example.org/constitution)**", True, False),
    ("[***Article 21***](https://example.org/constitution)", True, True),
    ("*[Article 21](https://example.org/constitution)*", False, True),
])
def test_link_emphasis_survives_save_and_reopen(tmp_path, markup, bold, italic):
    doc = Document()
    p = doc.add_paragraph()
    _add_text_runs(p, "See " + markup + " today.",
                   highlighter=_Highlighter(["Article 21"]))
    path = tmp_path / "links.docx"
    doc.save(path)
    saved = Document(path)
    p = saved.paragraphs[0]
    link = p._p.find(qn("w:hyperlink"))
    assert link is not None
    assert saved.part.rels[link.get(qn("r:id"))].target_ref == "https://example.org/constitution"
    assert "".join(p._p.xpath(".//w:t/text()")) == "See Article 21 today."
    rpr = link.find(qn("w:r")).find(qn("w:rPr"))
    for tag, expected in (("w:b", bold), ("w:i", italic)):
        node = rpr.find(qn(tag))
        actual = node is not None and node.get(qn("w:val")) not in ("0", "false")
        assert actual == expected
    assert rpr.find(qn("w:u")) is not None
    assert rpr.find(qn("w:shd")) is not None


def test_emphasis_spanning_a_link_keeps_neighbouring_words_bold():
    doc = Document()
    p = doc.add_paragraph()
    _add_text_runs(p, "**Read [Article 21](https://example.org/a) carefully**.")
    for run in p._p.xpath(".//w:r")[:-1]:
        assert run.find(qn("w:rPr")).find(qn("w:b")).get(qn("w:val")) != "0"
    assert visible_text("**Read [Article 21](https://example.org/a) carefully**.") == "Read Article 21 carefully."


def test_keyword_links_in_bullets_and_table_cells_keep_full_urls(tmp_path):
    url = "https://example.org/reports/ecology_(India)?year=2024&lang=hi"
    path = tmp_path / "dcn.docx"
    write_upsc_dcn_docx(
        "#LECTURE_TITLE: L01_DCN01_Ecology\n#H1: Concepts\n"
        f"- [**पर्यावरण**]({url}) supports life.\n"
        "#TABLE_START\nTerm | Meaning\n"
        f"[**Ecology**]({url}) | Study of relationships\n#TABLE_END",
        path,
    )
    doc = Document(path)
    links = doc._element.xpath(".//w:hyperlink")
    assert len(links) == 2
    assert ["".join(link.xpath(".//w:t/text()")) for link in links] == ["पर्यावरण", "Ecology"]
    for link in links:
        assert doc.part.rels[link.get(qn("r:id"))].target_ref == url
        assert link.xpath(".//w:b")
    text = "".join(doc._element.xpath(".//w:t/text()"))
    assert "**" not in text and "https://" not in text
