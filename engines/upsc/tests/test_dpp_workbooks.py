from __future__ import annotations

from src.notes_to_dpp.models import Difficulty, QuestionTag
from src.notes_to_dpp.workbooks import load_question_bank, load_taxonomy


def test_taxonomy_xlsx_generates_row_ids_and_preserves_blank_subtopic(
    xlsx_bytes,
) -> None:
    data = xlsx_bytes(
        [
            "Subject Name",
            "Subject ID",
            "Chapter Name",
            "Chapter ID",
            "Topic Name",
            "Topic ID",
            "Subtopic Name",
            "Subtopic ID",
        ],
        [
            [
                "Indian Physical Geography",
                "S-IND",
                "Drainage Systems of India",
                "C-IND-RIVERS",
                "River Systems",
                "T-REUSED",
                None,
                None,
            ],
            [
                "Physical Geography",
                "S-WORLD",
                "Drainage Patterns",
                "C-WORLD-RIVERS",
                "River Systems",
                "T-REUSED",
                "Dendritic Pattern",
                "ST-DENDRITIC",
            ],
        ],
        sheet_name="FINAL",
    )

    result = load_taxonomy(data)

    assert result.sheet_name == "FINAL"
    assert result.source_row_count == 2
    assert tuple(row.row_id for row in result.rows) == ("FINAL:2", "FINAL:3")
    assert result.rows[0].subtopic is None
    assert result.rows[0].subtopic_id is None
    assert result.rows[1].subtopic == "Dendritic Pattern"
    assert result.rows[0].topic_id == result.rows[1].topic_id == "T-REUSED"
    assert result.rows[0].composite_key != result.rows[1].composite_key
    assert any("generated" in warning.casefold() for warning in result.warnings)
    assert ("row_id", "<generated/optional>") in result.column_mapping


def test_question_bank_filters_invalid_rows_and_deduplicates_qbg_ids(
    xlsx_bytes,
) -> None:
    headers = [
        "unique_id",
        "qbgid",
        "content",
        "bilingual_options",
        "difficulty",
        "subject_id",
        "chapter_id",
        "topic_id",
        "subtopic_id",
        "qcm_tag",
        "verification_status",
    ]
    prompt = '{"english":"<p>What is ecology?</p>"}'
    options = '{"english":["<b>Organism</b>","Ecosystem"]}'
    rows = [
        ["U-1", "QBG-1", prompt, options, 1, "S-1", "C-1", "T-1", "ST-1", "DPP", "yes"],
        [
            "U-2",
            "QBG-1",
            prompt,
            options,
            "easy",
            "S-1",
            "C-1",
            "T-2",
            None,
            "Infinite Practice",
            "true",
        ],
        ["U-3", "QBG-2", "Medium prompt", "[]", 2, "S-1", "C-1", "T-2", None, "Infinite Practice", 1],
        ["U-4", "QBG-3", "Hard prompt", "[]", 3, "S-2", "C-2", "T-3", None, "DPP", "verified"],
        ["U-5", "QBG-4", "Wrong source", "[]", 1, "S-1", "C-1", "T-1", None, "Mock Test", "yes"],
        ["U-6", None, "No QBG ID", "[]", 1, "S-1", "C-1", "T-1", None, "DPP", "yes"],
        ["U-7", "QBG-5", "Bad difficulty", "[]", 4, "S-1", "C-1", "T-1", None, "DPP", "yes"],
        ["U-8", "QBG-6", "Unverified", "[]", 1, "S-1", "C-1", "T-1", None, "DPP", "no"],
    ]

    result = load_question_bank(xlsx_bytes(headers, rows, sheet_name="Questions"))
    by_id = {question.question_id: question for question in result.questions}

    assert result.sheet_name == "Questions"
    assert result.source_row_count == 8
    assert result.accepted_row_count == 4
    assert result.excluded_row_count == 4
    assert tuple(by_id) == ("QBG-1", "QBG-2", "QBG-3")
    assert by_id["QBG-1"].difficulty == Difficulty.EASY
    assert by_id["QBG-2"].difficulty == Difficulty.MEDIUM
    assert by_id["QBG-3"].difficulty == Difficulty.HARD
    assert by_id["QBG-1"].prompt == "What is ecology?"
    assert by_id["QBG-1"].options == ("Organism", "Ecosystem")
    assert set(by_id["QBG-1"].source_tags) == {"DPP", "Infinite Practice"}
    assert set(by_id["QBG-1"].tags) == {
        QuestionTag("S-1", "C-1", "T-1", "ST-1"),
        QuestionTag("S-1", "C-1", "T-2", None),
    }
    assert all(question.verified for question in result.questions)
    warnings = "\n".join(result.warnings)
    assert "non dpp source" in warnings
    assert "missing qbgid" in warnings
    assert "invalid difficulty" in warnings
    assert "unverified" in warnings
