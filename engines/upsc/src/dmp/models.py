"""What a practice session is, on disk and in memory.

Two dataclasses and their JSON round-trip. Deliberately plain: this is the shape
that has to survive a server restart and a version upgrade, so it holds data and
no behaviour beyond serialisation.

On ids: a question's id is generated once, at creation, and is what the UI sends
back to say "answer this one". Positions are not used for that. A user who
generates questions, walks away, and returns to a rebuilt session would
otherwise have their tick on question 3 answer whatever is third TODAY.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from ..config import DMP_LEVELS, DMP_MARKS

#: A question that has not been answered yet, and one that has. Answering is the
#: only transition — there is no un-answer, because the answer stays on disk and
#: re-requesting it is what `regenerate` is for.
STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"


def utc_now_iso() -> str:
    """Real server time, ISO 8601, timezone-aware.

    Written through one function so no caller reaches for `datetime.now()` and
    records a naive local timestamp that reads as UTC three months later.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def new_question_id() -> str:
    return f"q_{_short_uuid()}"


def new_session_id() -> str:
    return f"dmp_{_short_uuid()}"


def normalise_marks(value: Any) -> int:
    """Snap whatever the model returned to a real Mains mark.

    A model asked for "10 or 15" occasionally answers 12, or "15 marks", or
    "10.0". Every one of those is a question worth keeping with the wrong label
    on it, so the label is corrected rather than the question discarded.
    """
    try:
        marks = int(float(str(value).strip().split()[0]))
    except (TypeError, ValueError, IndexError):
        return DMP_MARKS[0]
    if marks in DMP_MARKS:
        return marks
    return min(DMP_MARKS, key=lambda m: abs(m - marks))


@dataclass
class Question:
    """One practice question, and its answer once someone asks for it."""

    id: str
    level: str
    text: str
    marks: int = 10
    directive: str | None = None      # set only when directive mode produced it
    status: str = STATUS_PENDING
    answer: str | None = None
    answer_generated_at: str | None = None
    model_used: str | None = None     # which model wrote the answer, for traceability

    @property
    def answered(self) -> bool:
        return self.status == STATUS_ANSWERED and bool(self.answer)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Question":
        return cls(
            id=str(data.get("id") or new_question_id()),
            level=str(data.get("level") or DMP_LEVELS[0]),
            text=str(data.get("text") or ""),
            marks=normalise_marks(data.get("marks")),
            directive=data.get("directive") or None,
            # Trust the stored answer over the stored status: a file written
            # during a crash can carry one without the other, and an answer that
            # exists should be shown rather than silently re-billed.
            status=(STATUS_ANSWERED if data.get("answer")
                    else str(data.get("status") or STATUS_PENDING)),
            answer=data.get("answer") or None,
            answer_generated_at=data.get("answer_generated_at") or None,
            model_used=data.get("model_used") or None,
        )


@dataclass
class DmpSession:
    """One practice set generated from one note.

    `levels` is a mapping rather than a flat list because the panel groups by
    level and the generation is per level: a level that failed to generate is
    then an absent key, distinguishable from a level that generated nothing.
    """

    dmp_session_id: str
    note_id: str
    note_title: str = ""
    subject: str = ""
    #: The subject as the notes PRINT it, already in the note's language. Stored
    #: rather than re-derived: subject_display_name needs both the raw key and
    #: the language, and a practice sheet that disagrees with its own notes
    #: about the subject looks like a different document.
    subject_line: str = ""
    topic: str = ""
    language: str = "en"
    app_version: str = ""
    created_at: str = ""
    updated_at: str = ""
    levels: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- questions ---------------------------------------------------------

    @property
    def questions(self) -> list[Question]:
        """Every question, in level order then generation order."""
        out: list[Question] = []
        for level in DMP_LEVELS:
            out.extend(self.level_questions(level))
        return out

    def level_questions(self, level: str) -> list[Question]:
        return list((self.levels.get(level) or {}).get("questions") or [])

    def question(self, question_id: str) -> Question | None:
        for q in self.questions:
            if q.id == question_id:
                return q
        return None

    def set_level(self, level: str, questions: list[Question]) -> None:
        self.levels[level] = {"generated_at": utc_now_iso(), "questions": list(questions)}
        self.updated_at = utc_now_iso()

    # ---- counts the panel and the index need -------------------------------

    @property
    def answered_count(self) -> int:
        return sum(1 for q in self.questions if q.answered)

    @property
    def pending_count(self) -> int:
        return sum(1 for q in self.questions if not q.answered)

    # ---- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "dmp_session_id": self.dmp_session_id,
            "note_id": self.note_id,
            "note_title": self.note_title,
            "subject": self.subject,
            "subject_line": self.subject_line,
            "topic": self.topic,
            "language": self.language,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "levels": {
                level: {
                    "generated_at": payload.get("generated_at", ""),
                    "questions": [q.to_dict() for q in (payload.get("questions") or [])],
                }
                for level, payload in self.levels.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DmpSession":
        levels: dict[str, dict[str, Any]] = {}
        for level, payload in (data.get("levels") or {}).items():
            payload = payload or {}
            levels[level] = {
                "generated_at": payload.get("generated_at", ""),
                "questions": [Question.from_dict(q)
                              for q in (payload.get("questions") or [])],
            }
        return cls(
            dmp_session_id=str(data.get("dmp_session_id") or new_session_id()),
            note_id=str(data.get("note_id") or ""),
            note_title=str(data.get("note_title") or ""),
            subject=str(data.get("subject") or ""),
            subject_line=str(data.get("subject_line") or ""),
            topic=str(data.get("topic") or ""),
            language=str(data.get("language") or "en"),
            app_version=str(data.get("app_version") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            levels=levels,
        )

    def index_entry(self) -> dict[str, Any]:
        """The one-line summary the history list reads.

        Counts are stored rather than recomputed on load so listing past sets
        does not mean opening and parsing every session file in the folder.
        """
        return {
            "dmp_session_id": self.dmp_session_id,
            "note_title": self.note_title,
            "subject": self.subject,
            "language": self.language,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total": len(self.questions),
            "answered": self.answered_count,
            "pending": self.pending_count,
        }
