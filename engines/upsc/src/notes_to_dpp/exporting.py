from __future__ import annotations

import csv
from io import StringIO
from typing import Iterable

from .models import SelectionSlot


def selected_qbg_ids(slots: Iterable[SelectionSlot]) -> tuple[str, ...]:
    ids = tuple(slot.question_id for slot in slots)
    if not ids:
        raise ValueError("No questions are selected.")
    if any(not question_id for question_id in ids):
        raise ValueError("Every selected question must have a QBG ID.")
    if len(ids) != len(set(ids)):
        raise ValueError("Selected QBG IDs must be unique.")
    return ids


def export_ids_csv(slots: Iterable[SelectionSlot]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["qbg_id"])
    writer.writerows((question_id,) for question_id in selected_qbg_ids(slots))
    return buffer.getvalue().encode("utf-8")


def export_ids_text(slots: Iterable[SelectionSlot]) -> bytes:
    return ("\n".join(selected_qbg_ids(slots)) + "\n").encode("utf-8")

