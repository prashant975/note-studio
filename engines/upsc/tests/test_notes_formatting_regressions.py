"""Guard the filename and export defects found in the two IR lecture notes."""
import json

import fitz
import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from PIL import Image

from src.title_utils import filename_subject, infer_from_filename, parse_marker_title, resolve_lecture_title
from src.upsc_dcn_docx_writer import STYLE_FIGURE, STYLE_FIGURE_CAPTION, STYLE_H1, write_upsc_dcn_docx


@pytest.mark.parametrize("number,topic", [
    (1, "Introduction Class"), (2, "India Pakistan and Afghanistan"),
    (3, "Relations in 2024 Part 2"),
])
@pytest.mark.parametrize("separator", ["_", " "])
def test_labelled_filenames_keep_the_number_and_only_the_human_topic(number, topic, separator):
    name = f"International Relations Lecture no. {number} Topic Name- {topic} upsc notes"
    name = name.replace(" ", separator)
    title = infer_from_filename(name)
    assert title.lecture_no == str(number).zfill(2)
    assert title.title == topic
    assert filename_subject(name) == "International Relations"


def test_subject_is_not_guessed_from_an_unlabelled_filename():
    assert filename_subject("L03_DCN03_Probity_in_Governance") == ""
    title = infer_from_filename("L03_DCN03_Probity_in_Governance")
    assert (title.lecture_no, title.dcn_no, title.title) == ("03", "03", "Probity in Governance")


def test_old_label_noise_is_repaired_but_a_custom_title_still_wins():
    name = "International_Relations_Lecture_no._1_Topic_Name-_Introduction_Class"
    title = resolve_lecture_title(
        marker_title="Lecture: International Relations no Topic Name Introduction", filename_stem=name)
    assert (title.lecture_no, title.title) == ("01", "Introduction Class")
    assert resolve_lecture_title(title="Our introductory session", filename_stem=name).title == "Our introductory session"
    assert parse_marker_title("Lecture 2: India and Pakistan").lecture_no == "02"
    assert resolve_lecture_title(
        marker_title="Lecture: no Topic Name Introduction",
        filename_stem="Lecture_no._1_Topic_Name-_Introduction_Class").title == "Introduction Class"


def test_rebuilding_an_old_run_repairs_the_saved_title_and_preserves_an_explicit_empty_subject(tmp_path, monkeypatch):
    from src import pipeline

    (tmp_path / "slides_raw.json").write_text("[]", encoding="utf-8")
    metadata = dict(dcn_mode=True, language="en", subject="upsc", subject_line="",
                    input_file="International Relations Lecture no. 1 Topic Name- Introduction Class.pdf",
                    lecture_title="Lecture: International Relations no Topic Name Introduction")
    (tmp_path / "run_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(pipeline, "export_docx_to_pdf", lambda *args: (None, None))
    output, _, _ = pipeline.rebuild_outputs(tmp_path, "#H1: Topic\n- A point.", stem="lecture")
    text = "\n".join(p.text for p in Document(output).paragraphs)
    assert "International Relations\nLecture 1: Introduction Class" in text
    assert "Topic Name" not in text

    metadata["lecture_title"] = "Lecture: A deliberate custom title"
    (tmp_path / "run_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    output, _, _ = pipeline.rebuild_outputs(tmp_path, "#H1: Topic\n- A point.", stem="custom")
    text = "\n".join(p.text for p in Document(output).paragraphs)
    assert "Lecture 1: A deliberate custom title" in text
    assert "International Relations" not in text


def test_paragraph_spacing_and_flow_markup_are_readable_in_the_word_source(tmp_path):
    notes = """#LECTURE_TITLE: Test
#H1: Historical sequence
- A main point with **emphasis**.
  - A nested point.
#FLOW_START
- First chronological event.
↓
- Second chronological event.
#FLOW_END
"""
    path, _ = write_upsc_dcn_docx(notes, tmp_path / "notes.docx")
    doc = Document(path)
    for name in ("UPSC_DCN_Body", "UPSC_DCN_Bullet_Level_1", "UPSC_DCN_Bullet_Level_2"):
        assert doc.styles[name].paragraph_format.alignment == WD_ALIGN_PARAGRAPH.LEFT
        assert doc.styles[name].paragraph_format.space_after.pt > 0
    assert doc.styles[STYLE_H1].paragraph_format.space_after.pt > 0
    flows = [p for p in doc.paragraphs if p.style.name == "UPSC_DCN_Flow"]
    assert [p.text for p in flows] == ["First chronological event.", "↓", "Second chronological event."]
    assert flows[0].paragraph_format.keep_with_next
    assert flows[1].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert not flows[-1].paragraph_format.keep_with_next


@pytest.mark.real_pdf
def test_handwriting_bold_is_visible_and_does_not_duplicate_searchable_text(tmp_path):
    from src.pdf_exporter import export_docx_to_pdf

    doc = Document()
    p = doc.add_paragraph()
    for text, bold in [("Regular words ", False), ("Critical term", True), (" ordinary again", False)]:
        run = p.add_run(text)
        run.font.name = "Coming Soon"
        run.font.size = Pt(11)
        run.bold = bold
    source = tmp_path / "weight.docx"
    doc.save(source)
    pdf, error = export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        page = rendered[0]
        assert page.get_text().count("Critical term") == 1
        # Stroke/fill render mode gives the single-weight font visible emphasis;
        # returning to regular text must restore fill-only rendering.
        trace = page.get_texttrace()
        strokes = [span for span in trace if span["type"] == 1]
        stroked = "".join(chr(char[0]) for span in strokes for char in span["chars"])
        assert stroked == "Critical term"
        assert "2 Tr" in page.read_contents().decode("latin-1")


@pytest.mark.real_pdf
def test_a_small_latin_overflow_does_not_create_an_almost_empty_page(tmp_path):
    from src.pdf_exporter import export_docx_to_pdf

    notes = "#LECTURE_TITLE: Topic\n" + "- A short factual point.\n" * 46
    source, _ = write_upsc_dcn_docx(notes, tmp_path / "tail.docx")
    pdf, error = export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) == 1
        assert rendered[0].get_text().count("A short factual point.") == 46


@pytest.mark.real_pdf
def test_devanagari_tail_retains_context_and_every_point(tmp_path):
    from src.pdf_exporter import export_docx_to_pdf

    sentence = "भारतीय संविधान जनता का प्रतिनिधित्व करता है।"
    notes = "#LECTURE_TITLE: उदाहरण\n" + f"- {sentence}\n" * 48
    source, _ = write_upsc_dcn_docx(notes, tmp_path / "hindi_tail.docx", language_code="hi")
    pdf, error = export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) == 2
        assert sum(p.get_text().count(sentence) for p in rendered) == 48
        assert rendered[-1].get_text().count(sentence) >= 10


@pytest.mark.real_pdf
def test_explicit_user_page_breaks_are_preserved_even_with_a_small_last_page(tmp_path):
    from src.pdf_exporter import export_docx_to_pdf

    source, _ = write_upsc_dcn_docx("#LECTURE_TITLE: Topic\n- First point.\n- Last point.", tmp_path / "break.docx")
    doc = Document(source)
    doc.paragraphs[-1].paragraph_format.page_break_before = True
    doc.save(source)
    pdf, error = export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        assert len(rendered) == 2
        assert "Last point." in rendered[1].get_text()
        assert "First point." not in rendered[1].get_text()


@pytest.mark.real_pdf
def test_an_image_and_its_caption_move_to_the_next_page_together(tmp_path):
    from src.pdf_exporter import export_docx_to_pdf

    notes = "#LECTURE_TITLE: Topic\n" + "- A short factual point.\n" * 37
    source, _ = write_upsc_dcn_docx(notes, tmp_path / "figure.docx")
    image = tmp_path / "square.png"
    Image.new("RGB", (200, 200), "red").save(image)
    doc = Document(source)
    doc.add_paragraph(style=STYLE_FIGURE).add_run().add_picture(str(image), width=Inches(110/72))
    caption = "Figure: A detailed caption describing the three stages of a process, its inputs, intermediate steps, and final results. " * 2
    doc.add_paragraph(caption, style=STYLE_FIGURE_CAPTION)
    doc.save(source)
    pdf, error = export_docx_to_pdf(source, tmp_path)
    assert error is None
    with fitz.open(pdf) as rendered:
        pages = [p for p in rendered if "Figure: A detailed caption" in p.get_text()]
        assert len(pages) == 1
        assert any(item[2:4] == (200, 200) for item in pages[0].get_images())
        assert "three stages of a process" in pages[0].get_text()
