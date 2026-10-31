"""The DMP sheets, checked against the PW OnlyIAS house format.

These read the DOCX that comes out, not the code that writes it. The format is
the deliverable — a faculty member holds the printed sheet next to last term's
and every bar, border and label has to line up — so the assertions are about
shading fills, column widths, which strings are bold and what the footer says,
rather than about which helper was called.

The two rules most likely to be "fixed" into a bug by someone tidying up later,
and therefore the two asserted hardest:

* the candidate and evaluator tables stay ENGLISH on a Hindi sheet;
* the question paper and the answer sheet have DIFFERENT footers.

The layout itself follows the content team's required sample (2026-10): the
blue "<Subject> DMP <No>" box with the cream topic box joined under it, the
marks run on after the question, the "How to Approach the Question?" box, and
Times New Roman throughout.
"""
from pathlib import Path
import sys

import pytest
from docx import Document
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp import house_docx as H
from src.dmp.export import (
    build_answer_document,
    build_question_document,
    dmp_number,
    export,
    title_bar_text,
    topic_bar_text,
    words_for,
)
from src.dmp.models import (
    STATUS_ANSWERED,
    DmpSession,
    Question,
    new_question_id,
    new_session_id,
    utc_now_iso,
)

ANSWER_WITH_APPROACH = """# APPROACH
- Introduction: Define the concept and its statutory basis.
- Body: Establish the provisions, then the friction.
  - Note the implementation gap in the smaller states.
- Conclusion: Argue for a stronger appellate tier.

# ANSWER
**The RTI Act, 2005 made information a statutory right.** It is administered by the **Central Information Commission**.

## Why the Act Matters
- **Accountability:** It reverses the default of official secrecy. **Example:** MGNREGA muster rolls.
  - The obligation is proactive, not merely responsive.
- **Citizen standing:** No locus standi is required.

A progressive step, but the appellate tier needs strengthening."""

ANSWER_WITHOUT_APPROACH = """## Introduction
The **RTI Act, 2005** is a statutory right.

- First point about the provision.
- Second point about the provision.

**Conclusion:** A progressive step."""


def test_faculty_point_marker_exports_as_a_real_bullet(tmp_path):
    """An answer saved under the old prompt: a per-demand heading and the
    five-hyphen marker. The point is still a real bullet; the heading is no
    longer printed as one but leads the point under it."""
    session = _session(answered=1, answer=(
        "# ANSWER\n## Accountability\n"
        "-----**Social audits:** Citizens inspect expenditure. "
        "For instance, MGNREGA muster rolls enable verification."))
    doc, _ = build_answer_document(session)
    path = tmp_path / "answer.docx"
    doc.save(path)
    saved = Document(path)
    point = next(p for p in saved.paragraphs if "Citizens inspect expenditure" in p.text)
    assert point.text.startswith("Accountability: Social audits:")
    assert "For instance, MGNREGA muster rolls enable verification." in point.text
    assert point._p.find(qn("w:pPr")).find(qn("w:numPr")) is not None
    assert next(r for r in point.runs if "Social audits:" in r.text).bold
    assert not [p for p in saved.paragraphs if p.text.strip() == "Accountability"]


def _session(language: str = "en", answered: int = 0,
             answer: str = ANSWER_WITH_APPROACH,
             questions_per_level: int = 5) -> DmpSession:
    now = utc_now_iso()
    session = DmpSession(
        dmp_session_id=new_session_id(), note_id="run_20260821_101500_abc12345",
        note_title="Indian Society", subject="society",
        subject_line="Indian Society", topic="Tribal Communities",
        language=language, app_version="2.7.0", created_at=now, updated_at=now,
    )
    for level in ("easy", "moderate", "applied"):
        session.set_level(level, [
            Question(id=new_question_id(), level=level,
                     text=f"A {level} question number {i} about tribal communities?",
                     marks=10 if i % 2 else 15)
            for i in range(questions_per_level)
        ])
    for question in session.questions[:answered]:
        question.answer = answer
        question.status = STATUS_ANSWERED
    return session


# --------------------------------------------------------------- inspection

def _doc_text(doc) -> str:
    """Every paragraph's text, tables included. The marks tag's spaces are
    non-breaking on the page, and read here as plain spaces."""
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
    return "\n".join(parts).replace("\u00a0", " ")


def _fill(cell) -> str:
    shd = cell._tc.get_or_add_tcPr().find(qn("w:shd"))
    return (shd.get(qn("w:fill")) or "").upper() if shd is not None else ""


def _bar_cells(doc):
    """Every shaded bar, in order — each shaded row of a one-column table.

    The blue title and the cream topic are two rows of ONE table now, so
    they share a border; the "DMP Question" bar is a table of its own.
    """
    return [row.cells[0] for t in doc.tables if len(t.columns) == 1
            for row in t.rows if _fill(row.cells[0])]


def _footer_text(doc) -> str:
    return "\n".join(p.text for p in doc.sections[0].footer.paragraphs)


def _bold_strings(doc) -> set[str]:
    found = set()
    for paragraph in doc.paragraphs:
        for run in paragraph.runs:
            if run.bold and run.text.strip():
                found.add(run.text.strip())
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        if run.bold and run.text.strip():
                            found.add(run.text.strip())
    return found


def _body_text_in_order(doc) -> str:
    """The document read top to bottom, paragraphs and tables interleaved.

    `_doc_text` lists every paragraph and THEN every table, which is fine for
    "does this string appear" and useless for "does this come before that" —
    the cover sheet is tables and the end mark is a paragraph, so comparing
    their positions in that string compares nothing.
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parts = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            parts.append(Paragraph(child, doc).text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            for row in table.rows:
                for cell in row.cells:
                    parts.extend(p.text for p in cell.paragraphs)
    return "\n".join(parts)


def _page_breaks(doc) -> int:
    return len(doc.element.body.findall(".//" + qn("w:br") + "[@" + qn("w:type") + "='page']"))


# ------------------------------------------------------------- page setup

def test_the_page_is_us_letter_with_one_inch_margins():
    doc, _ = build_question_document(_session())
    section = doc.sections[0]
    assert section.page_width == H.Twips(12240)
    assert section.page_height == H.Twips(15840)
    for margin in (section.top_margin, section.bottom_margin,
                   section.left_margin, section.right_margin):
        assert margin == H.Twips(1440)
    assert section.header_distance == H.Twips(454)
    assert section.footer_distance == H.Twips(283)


def test_every_full_width_table_spans_the_usable_width():
    """9360 twips. A table that disagrees with the others is visible as a
    ragged edge down the page."""
    doc, _ = build_question_document(_session())
    for table in doc.tables:
        total = sum(c.width for c in table.columns if c.width is not None)
        assert total == H.Twips(H.USABLE_DXA), [c.width for c in table.columns]


# ------------------------------------------------------ the question paper

def test_the_question_paper_opens_on_the_blue_subject_bar():
    doc, _ = build_question_document(_session())
    first = _bar_cells(doc)[0]
    assert _fill(first) == H.BLUE
    assert first.text.strip().startswith("Indian Society DMP")


def test_the_candidate_table_asks_for_the_five_mandatory_details():
    doc, _ = build_question_document(_session())
    text = _doc_text(doc)
    for label in ("Name*:", "Contact No*:", "Reg No:", "Email Id*:",
                  "Batch Name*:", "Marks Scored:", "Date:", "Evaluator's Id:"):
        assert label in text, label


def test_the_evaluator_table_rates_five_parameters_out_of_five():
    doc, _ = build_question_document(_session())
    text = _doc_text(doc)
    assert "(For Evaluator to fill)" in text
    assert "(Rate out of 5)" in text
    for parameter in H.EVALUATOR_PARAMETERS:
        assert parameter in text, parameter


def test_the_instructions_cell_spans_every_parameter_row():
    """One cell beside five rows, not five copies of the same paragraph."""
    doc, _ = build_question_document(_session())
    evaluator = next(t for t in doc.tables if len(t.columns) == 3)
    instructions = [evaluator.cell(row, 2).text for row in range(2, 7)]
    # A vertically merged cell reports the same text for every row it spans.
    assert len(set(instructions)) == 1, instructions
    assert "Student's Name" in instructions[0]
    assert "Batch Name" in instructions[0]


def test_every_question_gets_its_own_dmp_question_bar():
    session = _session(questions_per_level=2)
    doc, _ = build_question_document(session)
    bars = [c.text.strip() for c in _bar_cells(doc)]
    assert bars.count(H.QUESTION_BAR_TEXT) == 6


def test_each_question_after_the_first_starts_on_a_new_page():
    session = _session(questions_per_level=2)
    doc, _ = build_question_document(session)
    assert _page_breaks(doc) == 5, "six questions should be five page breaks apart"


def test_the_marks_tag_runs_on_after_the_question_marks_first():
    """The required sample prints "Q. <question>   (10Marks, 150 words)" — one
    paragraph, marks before words. It used to be "(150 Words, 10 Marks)" on a
    right-aligned line of its own."""
    doc, _ = build_question_document(_session(questions_per_level=1))
    question = next(p for p in doc.paragraphs if p.text.startswith("Q."))
    text = question.text.replace("\u00a0", " ")
    assert text.endswith("(15 Marks, 250 words)"), text
    assert "tribal communities?" in text
    runs = [r for r in question.runs if r.text.strip()]
    assert runs[0].text.strip() == "Q." and not runs[0].bold
    assert runs[1].bold and runs[-1].bold
    # The tag never splits across two lines.
    assert " " not in runs[-1].text.strip()


def test_the_question_paper_footer_says_daily_mains_practice():
    doc, _ = build_question_document(_session())
    footer = _footer_text(doc)
    assert H.FOOTER_LEFT in footer
    assert H.FOOTER_RIGHT_QUESTIONS in footer


# --------------------------------------------------------- the answer sheet

def test_the_answer_sheet_stacks_a_blue_bar_over_a_cream_one():
    doc, _ = build_answer_document(_session(answered=1))
    bars = _bar_cells(doc)
    assert _fill(bars[0]) == H.BLUE
    assert _fill(bars[1]) == H.CREAM
    assert bars[1].text.strip() == "Tribal Communities"
    # One table, two rows: the boxes share a border in Word and LibreOffice.
    assert bars[0]._tc.getparent().getparent() is bars[1]._tc.getparent().getparent()


def test_the_answer_sheet_shows_only_what_was_answered_by_default():
    doc, _ = build_answer_document(_session(answered=2))
    assert _doc_text(doc).count("Approach") == 2


def test_the_full_set_is_available_for_writing_by_hand():
    doc, _ = build_answer_document(_session(answered=2), only_answered=False)
    text = _doc_text(doc)
    assert "Not answered yet." in text


def test_the_approach_box_is_drawn_when_the_model_supplied_one():
    doc, _ = build_answer_document(_session(answered=1))
    text = _doc_text(doc)
    assert "Approach" in text
    assert "Define the concept and its statutory basis." in text
    assert "implementation gap in the smaller states" in text, "sub-point missing"


def test_an_answer_written_before_approaches_existed_gets_no_empty_box():
    """Answers saved before the approach was asked for have none. An empty
    bordered box with a heading over nothing is worse than no box."""
    doc, _ = build_answer_document(
        _session(answered=1, answer=ANSWER_WITHOUT_APPROACH))
    text = _doc_text(doc)
    assert "Approach" not in text
    assert "A progressive step." in text, "the answer itself must still be there"


def test_the_answer_keeps_its_bullets_and_bold_lead_ins_but_no_sub_headings():
    """A Mains answer is introduction, points and conclusion. The content team
    rejected sub-headings inside it; one in a saved answer leads the point
    under it instead of standing as a heading."""
    doc, _ = build_answer_document(_session(answered=1))
    text = _doc_text(doc)
    assert "Accountability:" in text
    assert not [p for p in doc.paragraphs if p.text.strip() == "Why the Act Matters"]
    lead = next(p for p in doc.paragraphs if "reverses the default" in p.text)
    assert lead.text.startswith("Why the Act Matters: Accountability:")
    bold = _bold_strings(doc)
    assert any(s.startswith("Accountability") for s in bold), sorted(bold)[:12]
    assert "Example:" in bold, "the reference bolds the word Example"


def test_the_opening_definition_sentence_stays_bold():
    doc, _ = build_answer_document(_session(answered=1))
    assert "The RTI Act, 2005 made information a statutory right." in _bold_strings(doc)


def test_each_answer_is_closed_with_the_house_end_mark():
    doc, _ = build_answer_document(_session(answered=2))
    assert _doc_text(doc).count(H.ANSWER_END_MARK) == 2


def test_the_answer_sheet_footer_says_dmp_model_answer():
    """Different from the question paper's. Sharing one footer between the two
    is the obvious simplification and it is wrong."""
    doc, _ = build_answer_document(_session(answered=1))
    footer = _footer_text(doc)
    assert H.FOOTER_RIGHT_ANSWERS in footer
    assert H.FOOTER_RIGHT_QUESTIONS not in footer


def test_an_answer_sheet_with_nothing_answered_still_produces_a_document():
    doc, warnings = build_answer_document(_session(answered=0))
    assert "Not answered yet." in _doc_text(doc)
    assert not [w for w in warnings if "Traceback" in w]


# ------------------------------------------------------------- the language

@pytest.mark.parametrize("language,prefix", [("en", "Q."), ("hi", "प्र."), ("mr", "प्र.")])
def test_the_question_prefix_follows_the_note_language(language, prefix):
    doc, _ = build_question_document(_session(language=language,
                                              questions_per_level=1))
    assert prefix in _doc_text(doc)


@pytest.mark.parametrize("language,tag", [
    ("en", "(15 Marks, 250 words)"),
    ("hi", "(15 अंक, 250 शब्द)"),
    ("mr", "(15 गुण, 250 शब्द)"),
])
def test_the_marks_tag_is_written_in_the_note_language(language, tag):
    doc, _ = build_question_document(_session(language=language,
                                              questions_per_level=1))
    assert tag in _doc_text(doc)


@pytest.mark.parametrize("language", ["hi", "mr"])
def test_the_evaluation_furniture_stays_english_on_a_devanagari_sheet(language):
    """The evaluator's workflow is English and the printed sheets say so. A
    well-meaning translation pass here would produce a sheet no evaluator in
    the building has seen before."""
    doc, _ = build_question_document(_session(language=language))
    text = _doc_text(doc)
    for label in ("Name*:", "Contact No*:", "Marks Scored:", "Evaluator's Id:",
                  "(For Evaluator to fill)", "Parameters", "(Rate out of 5)",
                  "Instructions for students", "Introduction Competence",
                  H.QUESTION_BAR_TEXT):
        assert label in text, f"{label!r} was translated on the {language} sheet"


@pytest.mark.parametrize("language,heading", [
    ("en", "How to Approach the Question?"), ("hi", "दृष्टिकोण"), ("mr", "दृष्टिकोन")])
def test_the_approach_heading_follows_the_note_language(language, heading):
    doc, _ = build_answer_document(_session(language=language, answered=1))
    assert heading in _doc_text(doc)


# ----------------------------------------------------------------- the fonts

def _fonts_used(doc) -> set[str]:
    """Every family named anywhere in the body, tables included."""
    families = set()

    def collect(paragraphs):
        for paragraph in paragraphs:
            for run in paragraph.runs:
                rfonts = run._element.get_or_add_rPr().find(qn("w:rFonts"))
                if rfonts is not None:
                    for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                        value = rfonts.get(qn(attribute))
                        if value:
                            families.add(value)

    collect(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                collect(cell.paragraphs)
    return families


def test_a_hindi_sheet_sets_times_new_roman_on_all_four_font_attributes():
    """Word reads w:cs for complex scripts, so naming the face only on w:ascii
    leaves the Devanagari to whatever the renderer picks."""
    doc, _ = build_answer_document(_session(language="hi", answered=1))
    checked = 0
    for paragraph in doc.paragraphs:
        for run in paragraph.runs:
            rfonts = run._element.get_or_add_rPr().find(qn("w:rFonts"))
            if rfonts is None:
                continue
            values = {rfonts.get(qn(a)) for a in
                      ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs")}
            assert len(values) == 1, f"a run named more than one family: {values}"
            checked += 1
    assert checked, "no styled runs were produced"


@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_every_sheet_is_set_in_times_new_roman(language):
    """The required sample is serif throughout — title, candidate table,
    question, approach and answer. English used to be Arial, with Times New
    Roman kept for Devanagari content."""
    for build in (build_answer_document, build_question_document):
        doc, _ = build(_session(language=language, answered=1))
        assert _fonts_used(doc) == {"Times New Roman"}, build.__name__


@pytest.mark.parametrize("forbidden", H.FORBIDDEN_FONTS)
def test_no_dmp_sheet_ever_names_a_notes_font(forbidden):
    """Kalam belongs to the Notes product. A DMP that picks it up has been
    contaminated by the other writer."""
    for language in ("en", "hi", "mr"):
        doc, _ = build_answer_document(_session(language=language, answered=1))
        assert forbidden not in _fonts_used(doc)
        doc, _ = build_question_document(_session(language=language))
        assert forbidden not in _fonts_used(doc)


# ------------------------------------------------------------ the DMP number

def _write_meta(runs_dir: Path, note_id: str, meta: dict) -> None:
    import json

    folder = runs_dir / note_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run_metadata.json").write_text(json.dumps(meta), encoding="utf-8")


@pytest.mark.parametrize("meta,expected", [
    ({"dcn_no": "04", "lecture_no": "07"}, "04"),
    ({"lecture_no": "7"}, "07"),
    ({"dcn_no": "DCN 12"}, "12"),
    ({}, ""),
])
def test_the_dmp_number_comes_from_the_lecture(tmp_path, meta, expected):
    """A DMP belongs to a class, and the printed sheets are numbered to match
    it (L-04 -> DMP 04)."""
    session = _session()
    _write_meta(tmp_path, session.note_id, meta)
    assert dmp_number(session, tmp_path) == expected


def test_the_title_bar_drops_the_number_rather_than_printing_a_blank(tmp_path):
    session = _session()
    _write_meta(tmp_path, session.note_id, {})
    assert title_bar_text(session, tmp_path) == "Indian Society DMP"


def test_the_title_bar_reads_subject_then_dmp_then_number(tmp_path):
    session = _session()
    _write_meta(tmp_path, session.note_id, {"dcn_no": "04"})
    assert title_bar_text(session, tmp_path) == "Indian Society DMP 04"


def test_the_topic_bar_falls_back_when_a_note_has_no_topic():
    session = _session()
    session.topic = ""
    assert topic_bar_text(session) == "Indian Society"


# ------------------------------------------------------------------ on disk

@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_a_real_docx_is_produced_in_every_language(language, tmp_path: Path):
    for kind in ("questions", "answers"):
        docx_path, _pdf, _warnings = export(
            _session(language=language, answered=1), kind, tmp_path)
        assert docx_path and docx_path.exists()
        assert docx_path.stat().st_size > 5_000
        Document(str(docx_path))          # it opens


def test_the_written_file_is_named_for_the_lecture_and_the_dmp_number(tmp_path):
    session = _session()
    runs = tmp_path / "runs"
    _write_meta(runs, session.note_id, {"dcn_no": "04"})
    docx_path, _pdf, _warnings = export(
        session, "questions", tmp_path / "out", runs_dir=runs)
    assert "DMP_04" in docx_path.name
    assert "Question_Paper" in docx_path.name


def test_an_unknown_export_kind_is_refused(tmp_path: Path):
    with pytest.raises(ValueError):
        export(_session(), "everything", tmp_path)


def test_words_follow_the_marks():
    assert words_for(10) == 150
    assert words_for(15) == 250
    assert words_for(99) == 150, "an unknown mark falls back rather than raising"


# ---------------------------------------------------------------------------
# The Hindi variant (spec section 11)
# ---------------------------------------------------------------------------

def _bar_size_pt(cell) -> float:
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                return run.font.size.pt
    return 0.0


def test_the_dmp_question_bar_is_smaller_than_the_title_bars():
    """A 20 pt title, an 18 pt topic under it, a 16 pt question bar. At the
    same size the question bar competes with the title for the top of the
    page."""
    doc, _ = build_answer_document(_session(language="hi", answered=1),
                                   cover_sheet_position="last")
    bars = {c.text.strip(): _bar_size_pt(c) for c in _bar_cells(doc)}
    assert bars[H.QUESTION_BAR_TEXT] == H.QUESTION_BAR_PT == 16
    assert bars["Tribal Communities"] == H.TOPIC_PT == 18
    assert bars["Indian Society DMP"] == H.BAR_PT == 20


def test_a_hindi_answer_sheet_ends_with_the_cover_and_the_question():
    """The Hindi reference is the whole deliverable in one file: the answer,
    the four-circle separator, then the blank sheet the student submits,
    closing on the question they are answering."""
    doc, _ = build_answer_document(_session(language="hi", answered=1))
    text = _body_text_in_order(doc)
    assert "Name*:" in text, "the cover sheet is missing"
    assert H.QUESTION_BAR_TEXT in text, "the closing question page is missing"
    # Order: the end mark comes before the cover, not after it.
    assert text.index(H.ANSWER_END_MARK) < text.index("Name*:")
    assert text.index("Name*:") < text.index(H.QUESTION_BAR_TEXT)


@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_trailing_evaluation_cover_starts_on_a_new_page(language):
    doc, _ = build_answer_document(_session(language=language, answered=1),
                                   cover_sheet_position="last")
    body = list(doc._element.body)
    end = next(i for i, element in enumerate(body)
               if H.ANSWER_END_MARK in "".join(element.xpath(".//w:t/text()")))
    following = body[end + 1]
    assert following.tag == qn("w:p")
    assert following.xpath('.//w:br[@w:type="page"]')
    assert _page_breaks(doc) == 1


def test_an_english_answer_sheet_carries_no_cover_by_default():
    """The English reference answer sheet is a companion to a separate question
    paper and has no cover on it. Adding one would change every existing
    English sheet."""
    doc, _ = build_answer_document(_session(language="en", answered=1))
    text = _doc_text(doc)
    assert "Name*:" not in text
    assert "(For Evaluator to fill)" not in text


@pytest.mark.parametrize("language,expected", [
    ("en", "none"), ("hi", "last"), ("mr", "last")])
def test_the_cover_position_defaults_per_language(language, expected):
    from src.dmp.export import default_cover_position

    assert default_cover_position(language) == expected


def test_the_cover_can_be_put_first_when_asked_for():
    doc, _ = build_answer_document(_session(language="hi", answered=1),
                                   cover_sheet_position="first")
    text = _body_text_in_order(doc)
    assert text.index("Name*:") < text.index(H.ANSWER_END_MARK)


def test_an_unknown_cover_position_is_refused():
    with pytest.raises(ValueError):
        build_answer_document(_session(answered=1),
                              cover_sheet_position="somewhere")


def test_a_hindi_sheet_keeps_the_daily_mains_practice_footer():
    """The Hindi reference prints "Daily Mains Practice" on every page,
    including the answer — because the file is not only a model answer, it
    carries the cover and the question too."""
    doc, _ = build_answer_document(_session(language="hi", answered=1))
    footer = _footer_text(doc)
    assert H.FOOTER_RIGHT_QUESTIONS in footer
    assert H.FOOTER_RIGHT_ANSWERS not in footer


def test_the_english_answer_sheet_still_names_itself_a_model_answer():
    doc, _ = build_answer_document(_session(language="en", answered=1))
    assert H.FOOTER_RIGHT_ANSWERS in _footer_text(doc)


@pytest.mark.parametrize("kind,language,expected", [
    ("questions", "en", "Daily Mains Practice"),
    ("questions", "hi", "Daily Mains Practice"),
    ("answers", "en", "DMP Model Answer"),
    ("answers", "hi", "Daily Mains Practice"),
    ("answers", "mr", "Daily Mains Practice"),
])
def test_the_footer_rule_is_stated_in_one_place(kind, language, expected):
    assert H.footer_right(kind, language) == expected


def test_the_title_bar_and_the_shell_below_it_share_the_sheet_face():
    """The title is academic content and is Devanagari; the candidate table
    under it is administrative and stays English. Both are Times New Roman
    since the required sample — named on w:cs too, so the Devanagari title
    is not left to the renderer's choice."""
    doc, _ = build_question_document(_session(language="hi"))
    title_cell = _bar_cells(doc)[0]
    title_fonts = {
        run._element.get_or_add_rPr().find(qn("w:rFonts")).get(qn("w:cs"))
        for paragraph in title_cell.paragraphs for run in paragraph.runs}
    assert title_fonts == {H.CONTENT_FONT_DEVANAGARI}

    candidate = next(t for t in doc.tables if len(t.columns) == 2)
    shell_fonts = {
        run._element.get_or_add_rPr().find(qn("w:rFonts")).get(qn("w:cs"))
        for row in candidate.rows for cell in row.cells
        for paragraph in cell.paragraphs for run in paragraph.runs}
    assert shell_fonts == {H.SHELL_FONT}
