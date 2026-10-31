from __future__ import annotations

from src.notes_to_dpp.exporting import export_ids_csv, export_ids_text
from src.notes_to_dpp.models import Difficulty, SelectionSlot


def test_csv_and_text_exports_contain_only_selected_qbg_ids() -> None:
    slots = (
        SelectionSlot("slot-1", Difficulty.EASY, "QBG-100", "ROW-1"),
        SelectionSlot("slot-2", Difficulty.HARD, "QBG-900", "ROW-9"),
    )

    csv_output = export_ids_csv(slots)
    text_output = export_ids_text(slots)

    assert csv_output == b"qbg_id\nQBG-100\nQBG-900\n"
    assert text_output == b"QBG-100\nQBG-900\n"
    assert b"Easy" not in csv_output + text_output
    assert b"ROW-1" not in csv_output + text_output
    assert b"slot-1" not in csv_output + text_output
