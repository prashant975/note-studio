"""Conservative presentation checks; never infer or rewrite lecture facts."""
from __future__ import annotations

import re


def text_identity(value: str) -> str:
    """Ignore emphasis and spacing, but preserve signs, case and Indic marks."""
    return re.sub(r"\s+", " ", str(value or "").replace("**", "")).strip()


def notes_are_hindi(notes) -> bool:
    text = " ".join([notes.title, notes.subject, *(s.heading for s in notes.sections)])
    text = re.sub(r"\$[^$]*\$", "", text)
    return len(re.findall(r"[\u0900-\u097f]", text)) > len(re.findall(r"[A-Za-z]", text))


def prefer_worked_revision(notes) -> bool:
    """A worked lecture uses its ordered solutions, not a synthetic mind map."""
    sections = notes.sections or []
    worked = sum(bool(getattr(s, "worked_examples", None)) for s in sections)
    return bool(worked and worked * 2 >= len(sections))
