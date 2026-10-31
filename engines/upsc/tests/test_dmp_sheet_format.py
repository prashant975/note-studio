"""The DMP sheet against the content team's required sample (2026-10).

Two reported issues, both checked here on the DOCX that comes out:

* **Issue 8, the format.** "<Subject> DMP <No>" in the blue box, the lecture
  topic in a cream box joined under it, "Marks Scored" one tall cell, the
  marks run on after the question, and a "How to Approach the Question?" box
  laid out as Introduction / Body with bullets / Conclusion. The subject, topic
  and number come from the panel's boxes, with graceful fallbacks when a box
  is empty.
* **Issue 10, the answer.** Exam-length answers, and no sub-headings or
  "Point:" labels on the page — including for answers saved under the old
  prompt — with a word-count flag (never a cut) for an answer well over its
  limit.
"""
from pathlib import Path
import json
import sys

import pytest
from docx import Document
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp import house_docx as H
from src.dmp.export import (
    build_answer_document,
    build_question_document,
    export,
    length_warnings,
    normalise_dmp_number,
    sheet_defaults,
    title_bar_text,
    topic_bar_text,
)
from src.dmp.models import (
    STATUS_ANSWERED,
    DmpSession,
    Question,
    new_question_id,
    utc_now_iso,
)
from src.dmp.service import answer_overrun

NEW_STYLE = """# APPROACH
- Introduction: Introduce the answer by discussing the adoption of globalisation.
- Body:
  - In the first part discuss how diversity is threatened by Globalisation.
  - In the next part discuss how Globalisation promotes pluralism.
- Conclusion: Discuss how the challenges can be judiciously mitigated.

# ANSWER
## INTRODUCTION
Since the **1991 reforms**, India has integrated with global flows of capital and culture.
## BODY
- **Cultural homogenisation:** Global media spreads uniform lifestyles. For example, fast food.
- **Hybrid cultures:** Exposure creates new blends. For example, Indian films on OTT.
## CONCLUSION
Inclusive cultural policy lets India gain from globalisation while protecting its diversity."""

#: The shape the content team reported: a per-demand sub-heading and a
#: "Point:" label on every bullet, as the old prompt asked for.
OLD_STYLE = """# APPROACH
- Introduction: Open with the Budget's function.
- Body: Establish the conceptual test.
  - Include the nuance that recurrence is only a broad tendency.
- Conclusion: Close by linking classification with borrowing.

# ANSWER
## Introduction
The Budget records receipts and expenditure.

## Conceptual Basis of the Distinction
-----**Point:** **Revenue expenditure** meets running costs. For example, salaries.
-----**Point:** **Capital expenditure** creates assets. For example, highways.

## Conclusion
Sound classification keeps borrowing accountable."""


def _session(answer: str = NEW_STYLE, *, language: str = "en", marks: int = 10,
             count: int = 2, note_id: str = "run_20261005_101500_fmt12345") -> DmpSession:
    now = utc_now_iso()
    session = DmpSession(
        dmp_session_id="dmp_fmt", note_id=note_id,
        note_title="Urbanization (Part 03)", subject="society",
        subject_line="Society, Social Issues, Social Justice",
        topic="Society, Social Issues, Social Justice — Urbanization (Part 03)",
        language=language, created_at=now, updated_at=now)
    session.set_level("easy", [
        Question(id=new_question_id(), level="easy", marks=marks,
                 text=f"Question {i}: how far do you agree with this statement?",
                 answer=answer, status=STATUS_ANSWERED)
        for i in range(count)])
    return session


def _one_column_rows(doc):
    """(table, row cell) for every one-column table — the shaded boxes."""
    return [(t, row.cells[0]) for t in doc.tables if len(t.columns) == 1
            for row in t.rows]


def _fill(cell) -> str:
    shd = cell._tc.get_or_add_tcPr().find(qn("w:shd"))
    return (shd.get(qn("w:fill")) or "").upper() if shd is not None else ""


def _has_numbering(paragraph) -> bool:
    ppr = paragraph._p.find(qn("w:pPr"))
    return ppr is not None and ppr.find(qn("w:numPr")) is not None


def _approach_cell(doc):
    return next(c for _t, c in _one_column_rows(doc)
                if c.paragraphs and c.paragraphs[0].text.startswith("How to Approach"))


def _all_paragraph_texts(doc) -> list[str]:
    texts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.extend(p.text for p in cell.paragraphs)
    return [t.replace(" ", " ") for t in texts]


# ------------------------------------------------------- the title and topic

def test_the_title_box_prints_subject_dmp_and_number():
    session = _session()
    assert title_bar_text(session, subject="Society, Social Issues, Social Justice",
                          dmp_no="19") == "Society, Social Issues, Social Justice DMP 19"


@pytest.mark.parametrize("typed,expected", [
    ("19", "19"), ("3", "03"), ("DMP 7", "07"), ("", ""), ("  ", "")])
def test_the_typed_number_is_printed_the_way_the_lecture_number_is(typed, expected):
    assert normalise_dmp_number(typed) == expected


def test_an_empty_subject_falls_back_to_daily_mains_practice():
    """The box was cleared on purpose: print the series name, never a
    leading blank before "DMP"."""
    session = _session()
    assert title_bar_text(session, subject="", dmp_no="19") == "Daily Mains Practice DMP 19"
    assert title_bar_text(session, subject="", dmp_no="") == "Daily Mains Practice DMP"


def test_an_empty_topic_drops_the_cream_box():
    doc, _ = build_answer_document(_session(count=1), subject="Polity", topic="",
                                   dmp_no="04")
    fills = [_fill(c) for _t, c in _one_column_rows(doc) if _fill(c)]
    assert H.CREAM not in fills
    assert any(c.text.strip() == "Polity DMP 04" for _t, c in _one_column_rows(doc))


def test_the_default_topic_does_not_repeat_the_subject_above_it():
    """note_context() builds the session topic as "subject — title" for the
    directive prompts; the cream box sits under a box naming the subject."""
    assert topic_bar_text(_session()) == "Urbanization (Part 03)"
    assert topic_bar_text(_session(), "  Cities   and Slums ") == "Cities and Slums"


def test_the_blue_and_cream_boxes_are_one_table():
    doc, _ = build_answer_document(_session(count=1), topic="Urbanization (Part 03)",
                                   subject="Society", dmp_no="19")
    first = doc.tables[0]
    assert [_fill(row.cells[0]) for row in first.rows] == [H.BLUE, H.CREAM]
    assert [row.cells[0].text.strip() for row in first.rows] == [
        "Society DMP 19", "Urbanization (Part 03)"]


def test_question_paper_pages_after_the_first_carry_the_topic_too():
    doc, _ = build_question_document(_session(count=3), subject="Society",
                                     topic="Urbanization (Part 03)", dmp_no="19")
    cream = [c.text.strip() for _t, c in _one_column_rows(doc) if _fill(c) == H.CREAM]
    # Pages two and three; the cover goes straight from title to details.
    assert cream == ["Urbanization (Part 03)"] * 2
    cover_title = doc.tables[0]
    assert len(cover_title.rows) == 1 and cover_title.rows[0].cells[0].text.strip() == "Society DMP 19"


# ------------------------------------------------------- the candidate table

def test_marks_scored_is_one_cell_beside_contact_and_reg_no():
    doc, _ = build_question_document(_session(count=1))
    table = next(t for t in doc.tables if len(t.columns) == 2)
    assert table.cell(1, 1)._tc is table.cell(2, 1)._tc, "Marks Scored is not merged"
    assert table.cell(1, 1).text.strip() == "Marks Scored:"
    assert table.cell(0, 0)._tc is table.cell(0, 1)._tc, "Name runs the full width"
    assert table.cell(2, 0).text.strip() == "Reg No:"
    assert [table.cell(r, 1).text.strip() for r in (3, 4)] == ["Date:", "Evaluator's Id:"]


# -------------------------------------------------------- the approach box

def test_the_approach_box_is_laid_out_like_the_sample():
    doc, _ = build_answer_document(_session(count=1))
    cell = _approach_cell(doc)
    paragraphs = [p for p in cell.paragraphs if p.text.strip()]
    texts = [p.text for p in paragraphs]
    assert texts[0] == "How to Approach the Question?"
    assert paragraphs[0].runs[0].bold
    assert texts[1].startswith("Introduction: Introduce the answer")
    assert texts[2] == "Body:"
    assert texts[5].startswith("Conclusion: Discuss how")
    # Introduction, Body and Conclusion are paragraphs; the parts are bullets.
    assert [_has_numbering(p) for p in paragraphs[1:]] == [False, False, True, True, False]
    assert "first part" in texts[3] and "next part" in texts[4]
    for label in ("Introduction:", "Body:", "Conclusion:"):
        run = next(r for p in paragraphs for r in p.runs if r.text == label)
        assert run.bold, label


def test_an_older_approach_keeps_its_body_line_and_its_nuance():
    doc, _ = build_answer_document(_session(OLD_STYLE, count=1))
    texts = [p.text for p in _approach_cell(doc).paragraphs if p.text.strip()]
    assert "Body: Establish the conceptual test." in texts
    assert any("broad tendency" in t for t in texts)


# --------------------------------------------------------- the model answer

def test_an_old_style_answer_prints_without_point_labels_or_sub_headings():
    """The reported sheet, re-exported: no "Point:", no "Conceptual Basis of
    the Distinction" standing as a heading, no "Introduction" heading."""
    doc, _ = build_answer_document(_session(OLD_STYLE, count=1))
    texts = _all_paragraph_texts(doc)
    assert not [t for t in texts if "Point:" in t]
    for heading in ("Conceptual Basis of the Distinction", "Introduction", "Conclusion"):
        assert heading not in [t.strip() for t in texts], heading
    point = next(p for p in doc.paragraphs if "meets running costs" in p.text)
    assert point.text.startswith("Conceptual Basis of the Distinction: Revenue expenditure")
    assert _has_numbering(point)


def test_a_new_style_answer_is_a_paragraph_points_and_a_paragraph():
    doc, _ = build_answer_document(_session(count=1))
    body = doc.paragraphs
    start = next(i for i, p in enumerate(body) if p.text == "Answer:")
    end = next(i for i, p in enumerate(body) if p.text == H.ANSWER_END_MARK)
    shape = [_has_numbering(p) for p in body[start + 1:end] if p.text.strip()]
    assert shape == [False, True, True, False]
    assert not [p for p in body if p.text.strip() in ("INTRODUCTION", "BODY", "CONCLUSION")]


# ----------------------------------------------------- the length post-check

def _words(n: int) -> str:
    return "# ANSWER\n" + " ".join(["word"] * n)


@pytest.mark.parametrize("marks,words,flagged", [
    (10, 150, False), (10, 180, False), (10, 181, True),
    (15, 300, False), (15, 301, True)])
def test_an_answer_is_flagged_only_when_well_past_its_limit(marks, words, flagged):
    """More than a fifth over: 180 words is the last unflagged length for a
    150-word question, 300 for a 250-word one."""
    result = answer_overrun(_words(words), marks)
    assert bool(result) is flagged
    if flagged:
        assert result == (words, 150 if marks == 10 else 250)


def test_a_long_answer_is_reported_and_printed_in_full():
    long_answer = _words(400) + " LASTWORD"
    session = _session(long_answer, count=1)
    doc, warnings = build_answer_document(session)
    assert warnings == length_warnings(session.questions)
    assert "401 words against the 150-word limit for 10 marks" in warnings[0]
    assert any("LASTWORD" in p.text for p in doc.paragraphs), "the answer was cut"


def test_an_answer_within_its_limit_raises_nothing():
    _doc, warnings = build_answer_document(_session(count=1))
    assert warnings == []


# ------------------------------------------------------------ export and file

def _write_meta(runs: Path, note_id: str, meta: dict, notes: str = "") -> None:
    folder = runs / note_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if notes:
        (folder / "notes_final.md").write_text(notes, encoding="utf-8")


def test_export_prints_and_names_the_file_by_what_the_boxes_say(tmp_path):
    session = _session(count=1)
    runs = tmp_path / "runs"
    _write_meta(runs, session.note_id, {"dmp_no": "04"})
    docx_path, _pdf, _warnings = export(
        session, "answers", tmp_path / "out", runs_dir=runs,
        subject="Society, Social Issues, Social Justice",
        topic="Urbanization (Part 03)", dmp_no="19")
    assert "DMP_19" in docx_path.name, "the typed number wins over the saved one"
    saved = Document(str(docx_path))
    assert saved.tables[0].rows[0].cells[0].text.strip() == (
        "Society, Social Issues, Social Justice DMP 19")
    assert saved.tables[0].rows[1].cells[0].text.strip() == "Urbanization (Part 03)"


def test_the_panel_boxes_start_from_the_lecture(tmp_path):
    """The subject the DPP builder would print (note_context's subject line),
    the lecture title as topic, and the lecture's saved DMP No."""
    session = _session(count=1)
    _write_meta(tmp_path, session.note_id,
                {"language": "en", "subject_line": "Indian Society", "dmp_no": "7"},
                notes="#LECTURE_TITLE: Urbanization (Part 03)\n\nSome notes.")
    assert sheet_defaults(session, tmp_path) == {
        "subject": "Indian Society", "topic": "Urbanization (Part 03)", "dmp_no": "07"}


def test_the_panel_boxes_fall_back_when_the_run_folder_is_gone(tmp_path):
    defaults = sheet_defaults(_session(count=1), tmp_path / "nowhere")
    assert defaults == {"subject": "Society, Social Issues, Social Justice",
                        "topic": "Urbanization (Part 03)", "dmp_no": ""}


# --------------------------------------------------------- page mechanics

def test_word_lays_the_tables_out_on_the_margin():
    """Word 2010 mode hangs a table's border out by its cell padding; the
    approach box's wide padding put it a quarter-inch left of everything."""
    doc, _ = build_answer_document(_session(count=1))
    modes = [s.get(qn("w:val")) for s in doc.settings.element.iter(qn("w:compatSetting"))
             if s.get(qn("w:name")) == "compatibilityMode"]
    assert modes == ["15"]


def test_a_page_break_cannot_spill_onto_a_page_of_its_own():
    """At 12 pt the break paragraph did not fit under a full answer, moved to
    the next page and broke again — a blank page between two answers."""
    doc, _ = build_answer_document(_session(count=2))
    breaks = [p for p in doc.paragraphs
              if p._p.xpath('.//w:br[@w:type="page"]')]
    assert breaks
    for paragraph in breaks:
        assert paragraph.paragraph_format.line_spacing == H.Pt(1)
