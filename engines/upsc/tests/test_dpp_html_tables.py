"""Regression coverage for question stems that carry HTML tables.

The question bank contains matching questions whose columns live in a real
``<table>``.  A printable DPP must retain that structure: flattening cells into
one paragraph makes a row's number, institution, and objective appear one
under another.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.notes_to_dpp import models
from src.notes_to_dpp.workbooks import load_question_bank


_HEADERS = [
    "unique_id",
    "qbgid",
    "content",
    "bilingual_options",
    "solutions",
    "difficulty",
    "subject_id",
    "chapter_id",
    "topic_id",
    "subtopic_id",
    "qcm_tag",
    "verification_status",
    "langgg",
]

_TABLE_STEM = """\
<p>Consider the following pairs:</p>
<table>
  <tr><th>HNUM</th><th>HINST</th><th>HGOAL</th></tr>
  <tr><td>RONE</td><td>RALPHA</td><td>RDELTA</td></tr>
  <tr><td>RTWO</td><td>RBETA</td><td>RGAMMA</td></tr>
</table>
<p>How many of the pairs above are correctly matched?</p>
"""


def _row() -> list[object]:
    return [
        "U-TABLE-1",
        "QBG-TABLE-1",
        json.dumps({"english": _TABLE_STEM}),
        json.dumps(
            {
                "english": [
                    {"text": "One", "isCorrect": True},
                    {"text": "Two", "isCorrect": False},
                ]
            }
        ),
        json.dumps({"english": "The first option is correct."}),
        1,
        "S-1",
        "C-1",
        "T-1",
        None,
        "DPP",
        "yes",
        "English",
    ]


def _word_positions(pdf: bytes, wanted: set[str]) -> dict[str, tuple[float, float]]:
    """Return each distinctive table token's top-left PDF coordinate."""
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        positions: dict[str, tuple[float, float]] = {}
        for page in document:
            for x0, y0, _x1, _y1, word, *_rest in page.get_text("words"):
                if word in wanted:
                    assert word not in positions, f"{word!r} printed more than once"
                    positions[word] = (x0, y0)
    finally:
        document.close()

    assert positions.keys() == wanted
    return positions


def test_html_table_stem_survives_workbook_loading_and_prints_in_columns(
    xlsx_bytes,
) -> None:
    """Each source table row remains one horizontal row in the DPP PDF."""
    loaded = load_question_bank(xlsx_bytes(_HEADERS, [_row()], sheet_name="Questions"))
    question = loaded.questions[0]
    localized = question.content_for(models.Language.ENGLISH)
    assert localized is not None

    # The display-text fallback remains useful for selection/search, while the
    # original markup carries the table structure needed by the PDF renderer.
    assert localized.prompt_markup is not None
    assert "<table" in localized.prompt_markup.casefold()
    assert "RALPHA" in localized.prompt_markup
    assert "RBETA" in localized.prompt_markup

    from src.notes_to_dpp.pdf_export import export_practice_pdfs

    export = export_practice_pdfs(
        (
            models.SelectionSlot(
                slot_id="slot-table",
                difficulty=models.Difficulty.EASY,
                question_id=question.question_id,
                preferred_row_id="ROW-1",
            ),
        ),
        (question,),
        models.Language.ENGLISH,
        dpp_no="1",
    )
    positions = _word_positions(
        export.question_pdf,
        {
            "HNUM",
            "HINST",
            "HGOAL",
            "RONE",
            "RALPHA",
            "RDELTA",
            "RTWO",
            "RBETA",
            "RGAMMA",
        },
    )

    for row in (
        ("HNUM", "HINST", "HGOAL"),
        ("RONE", "RALPHA", "RDELTA"),
        ("RTWO", "RBETA", "RGAMMA"),
    ):
        xs = [positions[token][0] for token in row]
        ys = [positions[token][1] for token in row]
        assert xs[0] < xs[1] < xs[2], f"table cells lost their column order: {row}"
        assert max(ys) - min(ys) < 4, f"table cells were flattened vertically: {row}"


def test_html_table_spans_keep_a_rectangular_grid() -> None:
    """The small subset of bank tables using spans remains printable too."""
    from src.notes_to_dpp.pdf_export import _prompt_parts

    parts = _prompt_parts(
        "<p>Before</p><table>"
        "<tr><th colspan='2'>TOP</th><th>RIGHT</th></tr>"
        "<tr><td rowspan='2'>LEFT</td><td>MID1</td><td>END1</td></tr>"
        "<tr><td>MID2</td><td>END2</td></tr>"
        "</table><p>After</p>"
    )
    table = next(part.table for part in parts if part.table is not None)
    assert table is not None
    assert table.rows == (
        ("TOP", "", "RIGHT"),
        ("LEFT", "MID1", "END1"),
        ("", "MID2", "END2"),
    )
    assert table.spans == ((0, 0, 1, 0), (0, 1, 0, 2))


def test_duplicate_question_rows_keep_table_markup_when_editor_attributes_differ(
    xlsx_bytes,
) -> None:
    """Cosmetic HTML differences must not discard an otherwise valid QBG ID."""
    first = _row()
    second = _row()
    second[0] = "U-TABLE-2"
    second[2] = json.dumps({"english": _TABLE_STEM.replace("<table>", "<table class='copy'>")})

    loaded = load_question_bank(xlsx_bytes(_HEADERS, [first, second], sheet_name="Questions"))
    assert len(loaded.questions) == 1
    localized = loaded.questions[0].content_for(models.Language.ENGLISH)
    assert localized is not None
    assert localized.prompt_markup is not None


def test_world_bank_question_from_reported_screenshot_keeps_its_columns(xlsx_bytes):
    """Actual QBG stem as stored, including td/p/strong tags and escaped attrs."""
    from src.notes_to_dpp.pdf_export import export_practice_pdfs, _prompt_parts

    content = json.loads((Path(__file__).parent / "fixtures" / "dpp_world_bank_prompt.json")
                         .read_text(encoding="utf-8"))
    row = _row()
    row[2] = json.dumps(content)
    loaded = load_question_bank(xlsx_bytes(_HEADERS, [row], sheet_name="Questions"))
    question = loaded.questions[0]
    localized = question.content_for(models.Language.ENGLISH)
    table = next(p.table for p in _prompt_parts(localized.prompt_markup) if p.table)
    assert table.rows[0] == ("Sr. No", "Institutions", "Objectives")
    assert len(table.rows) == 4
    assert table.rows[1] == (
        "1", "International Finance Corporation (IFC)",
        "Reduce poverty by providing zero to low-interest loans")
    assert "(IDA)" in table.rows[2][1] and "(MIGA)" in table.rows[3][1]

    export = export_practice_pdfs(
        (models.SelectionSlot("slot-world-bank", question.difficulty, question.question_id, "ROW-1"),),
        (question,), models.Language.ENGLISH)
    for payload in (export.question_pdf, export.combined_pdf):
        positions = _word_positions(payload, {"Institutions", "Objectives", "Reduce", "Focuses", "Multilateral", "Promote"})
        assert positions["Institutions"][0] < positions["Objectives"][0]
        assert abs(positions["Institutions"][1] - positions["Objectives"][1]) < 4
        assert positions["Reduce"][1] < positions["Focuses"][1] < positions["Promote"][1]
        assert positions["Multilateral"][0] < positions["Promote"][0]
        assert abs(positions["Multilateral"][1] - positions["Promote"][1]) < 4


def test_preview_preserves_table_and_surrounding_question_text():
    from src.notes_to_dpp.pdf_export import prompt_preview_markdown

    preview = prompt_preview_markdown("flattened fallback", _TABLE_STEM)
    assert "| HNUM | HINST | HGOAL |" in preview
    assert "| RONE | RALPHA | RDELTA |" in preview
    assert preview.index("Consider the following pairs") < preview.index("| HNUM")
    assert preview.index("| RTWO") < preview.index("How many of the pairs")
    assert "flattened fallback" not in preview
    assert prompt_preview_markdown("Plain question") == "Plain question"


def test_preview_escapes_cell_delimiters_and_retains_td_header_row():
    from src.notes_to_dpp.pdf_export import prompt_preview_markdown

    preview = prompt_preview_markdown("fallback", "<table><tr><td>A | B</td><td>&lt;x&gt;</td></tr></table>")
    assert r"| A \| B | &lt;x&gt; |" in preview
