"""Question papers must contain only the questions explicitly ticked in the UI."""
import pytest

from src.config import DMP_LEVELS
from src.dmp.export import build_question_document
from src.dmp.models import DmpSession, Question, new_session_id, utc_now_iso


def _session() -> DmpSession:
    now = utc_now_iso()
    session = DmpSession(
        dmp_session_id=new_session_id(),
        note_id="run_20260821_101500_abc12345",
        note_title="Selected questions",
        subject_line="Ethics",
        language="en",
        created_at=now,
        updated_at=now,
    )
    for index, level in enumerate(DMP_LEVELS, start=1):
        session.set_level(level, [
            Question(
                id=f"selected-{index}", level=level,
                text=f"Keep question {index}.", marks=10,
            ),
            Question(
                id=f"skip-{index}", level=level,
                text=f"Skip question {index}.", marks=15,
            ),
        ])
    return session


def _document_text(doc) -> str:
    parts = [paragraph.text for paragraph in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.extend(paragraph.text for paragraph in cell.paragraphs)
    return "\n".join(parts)


def test_question_document_includes_only_selected_ids_in_session_order():
    session = _session()
    doc, _ = build_question_document(
        session, selected_ids=["selected-3", "selected-1"])
    text = _document_text(doc)

    assert "Keep question 1." in text
    assert "Keep question 3." in text
    assert "Skip question 1." not in text
    assert "Skip question 2." not in text
    assert "Skip question 3." not in text
    assert text.index("Keep question 1.") < text.index("Keep question 3.")


def test_an_explicit_empty_selection_never_falls_back_to_all_questions():
    with pytest.raises(ValueError, match="Select at least one DMP question"):
        build_question_document(_session(), selected_ids=[])
