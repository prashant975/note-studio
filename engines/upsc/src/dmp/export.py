"""DMP question papers and model-answer sheets, in the PW OnlyIAS house format.

The layout is fixed and is documented in `house_docx.py`; this module decides
what goes into it. Two documents come out of one practice set:

* **the question paper** — the cover the student fills in (name, contact,
  evaluator's five rated parameters) followed by the questions, one per page,
  each under its own `DMP Question` bar;
* **the model answers** — one page per answered question: the blue subject bar,
  the cream topic bar, the question, the approach box, and the answer.

What the two bars say — the subject, the DMP number and the topic — can be
handed in by the caller (the panel's three boxes); otherwise it is derived from
the session and the lecture's run metadata, as it always was.

The house sheets are built around ONE question; a practice set holds ten.
The reconciliation is the page break: every question gets the full house
treatment on its own page, and the evaluation table — which a student fills in
once, for the sheet they submit — appears once at the front.

This replaced a version that rendered the practice sheets through the DCN notes
writer. That reuse was the right instinct and the wrong writer: notes are set in
a handwritten face with a notes-box layout, and no amount of marker vocabulary
turns that into a serif evaluation sheet on US Letter.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Iterable

from ..config import DMP_LEVELS, DMP_MARK_WORDS
from ..languages import normalise
from ..utils import ensure_dir, safe_name
from . import house_docx as H
from .answer_parse import parse as parse_answer
from .models import DmpSession

#: What the sheet is called, for the file name only — the page itself is titled
#: from the subject and the DMP number, exactly as the reference sheets are.
_KIND_SUFFIX = {"questions": "Question_Paper", "answers": "Model_Answers"}


def words_for(marks: int) -> int:
    return DMP_MARK_WORDS.get(int(marks), DMP_MARK_WORDS[10])


def dmp_number(session: DmpSession, runs_dir: Path | None = None) -> str:
    """The `04` in `Ethics DMP 04`.

    Taken from the lecture the notes came from — a DMP belongs to a class, and
    the reference sheets are numbered to match it (`L-04-DMP-04`). Read from run
    metadata at export time rather than stored on the session, so sets written
    before this existed still get their number.
    """
    from . import store

    try:
        run_dir = store.note_run_dir(session.note_id, runs_dir)
        meta_path = run_dir / "run_metadata.json"
        if not meta_path.exists():
            return ""
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return ""
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    # A DMP number is explicit metadata now.  Older runs did not have it, so
    # their DCN/lecture number remains a backwards-compatible fallback.
    for key in ("dmp_no", "dcn_no", "lecture_no"):
        raw = str(meta.get(key) or "").strip()
        digits = re.sub(r"\D", "", raw)
        if digits:
            return digits.zfill(2)
    return ""


#: The title when there is no subject to print: "Daily Mains Practice DMP 19".
UNTITLED_SUBJECT = "Daily Mains Practice"


def normalise_dmp_number(raw) -> str:
    """"19", "3" -> "03", "DMP 19" -> "19" — the form the title prints.

    A value with no digits at all is kept as typed: it is what the teacher
    asked to see, and dropping it would print a number nobody chose.
    """
    text = " ".join(str(raw or "").split())
    digits = re.sub(r"\D", "", text)
    return digits.zfill(2) if digits else text


def title_bar_text(session: DmpSession, runs_dir: Path | None = None, *,
                   subject: str | None = None, dmp_no: str | None = None) -> str:
    """`{subject} DMP {NN}` — the blue bar at the top of every page.

    ``subject`` / ``dmp_no`` are what the panel's boxes say; ``None`` means
    "not asked", and the subject line and the lecture's number are used. An
    empty subject prints "Daily Mains Practice", and no number drops the
    number rather than printing `DMP` followed by a blank.
    """
    from ..title_utils import subject_display_name

    if subject is None:
        subject = (session.subject_line
                   or subject_display_name(session.subject, session.language)
                   or session.note_title)
    subject = " ".join(str(subject or "").split()) or UNTITLED_SUBJECT
    number = (dmp_number(session, runs_dir) if dmp_no is None
              else normalise_dmp_number(dmp_no))
    return f"{subject} DMP {number}" if number else f"{subject} DMP"


def topic_bar_text(session: DmpSession, topic: str | None = None) -> str:
    """The cream bar under the title: what the class covered.

    ``topic`` is the panel's box; an empty one means no cream bar. Without one,
    the session's topic is used — minus the "subject — " that note_context()
    puts in front of it for directive prompts, because under a bar that
    already names the subject it is a repeat.
    """
    if topic is not None:
        return " ".join(str(topic).split())
    text = (session.topic or session.note_title or session.subject_line or "").strip()
    repeat = f"{session.subject_line} — " if session.subject_line else ""
    if repeat and text.startswith(repeat) and text[len(repeat):].strip():
        text = text[len(repeat):].strip()
    return text


def sheet_defaults(session: DmpSession, runs_dir: Path | None = None) -> dict[str, str]:
    """What the panel's Subject, Topic and DMP No. boxes start out holding.

    The subject is found the way the DPP builder finds the subject it prints —
    ``note_context(...)["subject_line"]`` — so a lecture's two practice sheets
    agree. The topic is the lecture title, and the number is the lecture's
    saved DMP No. (or its DCN/lecture number for older runs).
    """
    from ..title_utils import subject_display_name
    from .service import note_context

    subject = ""
    try:
        subject = note_context(session.note_id, runs_dir).get("subject_line") or ""
    except Exception:  # noqa: BLE001 — a prefill is never worth a crash
        subject = ""
    subject = (subject or session.subject_line
               or subject_display_name(session.subject, session.language) or "")
    return {
        "subject": subject,
        "topic": session.note_title or topic_bar_text(session),
        "dmp_no": dmp_number(session, runs_dir),
    }


def length_warnings(questions) -> list[str]:
    """One line per answer that runs well past its word limit.

    The check the panel shows next to each answer, repeated for the export so
    a caller with no panel still hears about it. Printed in full regardless.
    """
    from .service import answer_overrun

    warnings: list[str] = []
    for number, question in enumerate(questions, start=1):
        if not question.answered:
            continue
        overrun = answer_overrun(question.answer or "", question.marks)
        if overrun:
            words, limit = overrun
            warnings.append(
                f"Answer {number} is {words} words against the {limit}-word "
                f"limit for {question.marks} marks; it is printed in full.")
    return warnings


# ---------------------------------------------------------------------------
# the two documents
# ---------------------------------------------------------------------------

def _questions_for_export(
    session: DmpSession, selected_ids: Iterable[str] | None = None,
):
    """Questions in their stable session order, optionally limited to a pick.

    ``None`` retains the historical "entire set" export for callers outside
    the app.  An explicit collection, including an empty one, means the caller
    requested a selection and must never silently turn into all questions.
    """
    questions = [q for level in DMP_LEVELS for q in session.level_questions(level)]
    if selected_ids is None:
        return questions
    picked = {str(question_id).strip() for question_id in selected_ids
              if str(question_id).strip()}
    selected = [q for q in questions if q.id in picked]
    if not selected:
        raise ValueError("Select at least one DMP question before exporting it.")
    return selected


def build_question_document(session: DmpSession, *, logo_path: Path | None = None,
                            runs_dir: Path | None = None,
                            selected_ids: Iterable[str] | None = None,
                            subject: str | None = None,
                            topic: str | None = None,
                            dmp_no: str | None = None):
    """(document, warnings) for the question paper.

    ``subject``, ``topic`` and ``dmp_no`` override what the bars print; see
    :func:`title_bar_text` and :func:`topic_bar_text`.
    """
    warnings: list[str] = []
    language = normalise(session.language) or "en"
    doc = H.new_document(warnings, logo_path,
                         H.footer_right("questions", language))
    # Defined even though the question paper draws no bullets: the numbering
    # part is cheap, and a later change that adds one to this sheet should not
    # have to remember to define it.
    H.ensure_bullet_numbering(doc, warnings)
    title = title_bar_text(session, runs_dir, subject=subject, dmp_no=dmp_no)
    topic_text = topic_bar_text(session, topic)

    add_cover_sheet(doc, title, language)

    questions = _questions_for_export(session, selected_ids)

    for index, question in enumerate(questions):
        if index:
            # Every question gets the full house treatment on its own page:
            # the subject box with the topic under it, as on the answers.
            H.add_page_break(doc)
            H.add_title_block(doc, title, topic_text,
                              font=H.content_font(language))
        H.add_gap(doc)
        H.add_question_bar(doc)
        H.add_question(doc, question.text, question.marks,
                       words_for(question.marks), language, space_before=6)
    return doc, warnings


def add_cover_sheet(doc, title: str, language: str) -> None:
    """Title bar, candidate details, evaluator table.

    The title box is the taller one the required sample opens on, with the
    candidate table straight under it and no topic box between them.
    """
    H.add_title_block(doc, title, "", font=H.content_font(language),
                      row_height=H.COVER_BAR_ROW_HEIGHT_DXA)
    H.add_candidate_table(doc)
    H.add_evaluator_table(doc)


#: Where the cover / evaluation sheet sits in the answer document.
#:
#: The two reference sheets genuinely differ, and neither is a mistake. The
#: English answer sheet carries no cover at all — it is a companion to a
#: separate question paper. The Hindi sample is the whole deliverable in one
#: file: the answer, the four-circle separator, then the cover and a closing
#: question page. Defaulting per language reproduces each reference exactly,
#: and the flag is here so either order can be asked for explicitly.
COVER_NONE, COVER_FIRST, COVER_LAST = "none", "first", "last"
COVER_POSITIONS = (COVER_NONE, COVER_FIRST, COVER_LAST)


def default_cover_position(language: str) -> str:
    return COVER_NONE if (normalise(language) or "en") == "en" else COVER_LAST


def build_answer_document(session: DmpSession, *, only_answered: bool = True,
                          logo_path: Path | None = None,
                          runs_dir: Path | None = None,
                          cover_sheet_position: str | None = None,
                          subject: str | None = None,
                          topic: str | None = None,
                          dmp_no: str | None = None):
    """(document, warnings) for the model answers.

    `only_answered` is the default because a sheet whose pages mostly read "not
    answered yet" is a question paper with extra steps. ``subject``, ``topic``
    and ``dmp_no`` override what the bars print, as on the question paper.
    """
    warnings: list[str] = []
    language = normalise(session.language) or "en"
    cover = cover_sheet_position or default_cover_position(language)
    if cover not in COVER_POSITIONS:
        raise ValueError(f"Unknown cover sheet position: {cover_sheet_position!r}")
    doc = H.new_document(warnings, logo_path,
                         H.footer_right("answers", language))
    num_id = H.ensure_bullet_numbering(doc, warnings)
    title = title_bar_text(session, runs_dir, subject=subject, dmp_no=dmp_no)
    topic = topic_bar_text(session, topic)

    font = H.content_font(language)

    questions = [q for level in DMP_LEVELS for q in session.level_questions(level)
                 if q.answered or not only_answered]
    if not questions:
        H.add_title_block(doc, title, topic, font=font)
        H.add_unanswered_note(doc, language)
        return doc, warnings
    warnings.extend(length_warnings(questions))

    if cover == COVER_FIRST:
        add_cover_sheet(doc, title, language)
        H.add_page_break(doc)

    for index, question in enumerate(questions):
        if index:
            H.add_page_break(doc)
        H.add_title_block(doc, title, topic, font=font)
        H.add_question(doc, question.text, question.marks,
                       words_for(question.marks), language, space_before=14)

        if not question.answered:
            H.add_unanswered_note(doc, language)
            continue

        approach, blocks = parse_answer(question.answer or "")
        # Drawn only when the model supplied one. Answers written before the
        # approach was asked for have none, and an empty box would be a heading
        # over nothing.
        H.add_approach_box(doc, approach, language, num_id)
        H.add_answer_label(doc, language)
        H.add_answer_body(doc, blocks, num_id, font=font)
        H.add_answer_end_mark(doc, language)

    if cover == COVER_LAST:
        # The Hindi sample's order: the answer, then the blank sheet the
        # student submits, closing on the question being answered.
        H.add_page_break(doc)
        add_cover_sheet(doc, title, language)
        H.add_gap(doc)
        H.add_question_bar(doc)
        first = questions[0]
        H.add_question(doc, first.text, first.marks,
                       words_for(first.marks), language, space_before=6)
    return doc, warnings


# ---------------------------------------------------------------------------
# writing them out
# ---------------------------------------------------------------------------

def export(session: DmpSession, kind: str, output_dir: Path,
           *, logo_path: Path | None = None,
           only_answered: bool = True,
           runs_dir: Path | None = None,
           cover_sheet_position: str | None = None,
           selected_ids: Iterable[str] | None = None,
           subject: str | None = None,
           topic: str | None = None,
           dmp_no: str | None = None,
           ) -> tuple[Path | None, Path | None, list[str]]:
    """Write the DOCX and its standalone PDF.

    Returns (docx, pdf, warnings). A failed PDF conversion is a warning and not
    an error: the DOCX is preserved, and if the bundled PDF renderer fails it is
    the only one there was ever going to be.

    ``subject``, ``topic`` and ``dmp_no`` are what the panel's boxes say; left
    as ``None`` they are worked out from the session, as before. The number
    names the file as well as the title, so the two never disagree.
    """
    from ..pdf_exporter import export_docx_to_pdf
    from ..upsc_dcn_docx_writer import default_logo_path

    if kind not in _KIND_SUFFIX:
        raise ValueError(f"Unknown DMP export kind: {kind!r}")

    logo = logo_path or default_logo_path()
    if kind == "questions":
        doc, warnings = build_question_document(
            session, logo_path=logo, runs_dir=runs_dir,
            selected_ids=selected_ids, subject=subject, topic=topic,
            dmp_no=dmp_no)
    else:
        doc, warnings = build_answer_document(
            session, only_answered=only_answered, logo_path=logo,
            runs_dir=runs_dir, cover_sheet_position=cover_sheet_position,
            subject=subject, topic=topic, dmp_no=dmp_no)

    output_dir = ensure_dir(Path(output_dir))
    number = (dmp_number(session, runs_dir) if dmp_no is None
              else normalise_dmp_number(dmp_no))
    stem = safe_name(
        f"{session.note_title or session.subject or 'lecture'}"
        f"_DMP{('_' + number) if number else ''}_{_KIND_SUFFIX[kind]}",
        default=f"DMP_{kind}")
    docx_path = output_dir / f"{stem}.docx"
    doc.save(str(docx_path))

    pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
    if pdf_warning:
        warnings.append(pdf_warning)
    return docx_path, pdf_path, warnings
