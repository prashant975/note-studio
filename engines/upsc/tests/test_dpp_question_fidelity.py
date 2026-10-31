"""What a printed practice set says a question is.

Every case here comes from the shipped question bank rather than from
imagination: the markup shapes, the counts and the contradictions are the ones
``data/DPP and Infinite Practice Questions.xlsx`` actually contains.
"""
from __future__ import annotations

import importlib

import pytest

from src.notes_to_dpp import models
from src.notes_to_dpp.workbooks import _strip_html, load_question_bank

BANK_HEADERS = [
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


def _bank_row(unique_id: str, qbg_id: str, content: str, options: str) -> list[object]:
    return [
        unique_id, qbg_id, content, options, "[]", 1,
        "S-1", "C-1", "T-1", None, "DPP", "yes", "English",
    ]


# ---------------------------------------------------------------- numbering

def test_ordered_statements_keep_the_numbers_their_options_refer_to() -> None:
    """The stem's numbering lives in the ``<ol>``, and the options need it.

    Two thirds of the bank writes its statements this way. Dropping the
    numbering leaves "1 only" and "2 only" pointing at nothing.
    """
    stem = (
        "<p>Consider the following statements:</p>\n"
        "<ol>\n<li>It was established in 1972.</li>\n"
        "<li>It empowers the State government.</li>\n</ol>\n"
        "<p>Which of the statements given above is/are correct?</p>"
    )

    assert _strip_html(stem) == (
        "Consider the following statements:\n"
        "1. It was established in 1972.\n"
        "2. It empowers the State government.\n"
        "Which of the statements given above is/are correct?"
    )


def test_ordered_list_honours_a_real_start_but_ignores_editor_offsets() -> None:
    """``start`` renumbers; ``data-start`` is an offset left by the editor."""
    assert _strip_html('<ol start="2">\n<li>Two</li>\n<li>Three</li>\n</ol>') == (
        "2. Two\n3. Three"
    )
    assert _strip_html('<ol data-start="166" data-end="589">\n<li>One</li>\n</ol>') == (
        "1. One"
    )


def test_a_container_item_carries_no_marker_of_its_own() -> None:
    """How the bank nests: an outer item styled to show no marker."""
    nested = (
        '<ol>\n<li style="list-style-type: none;">\n<ol>\n'
        "<li>Inner A</li>\n<li>Inner B</li>\n</ol>\n</li>\n</ol>"
    )

    assert _strip_html(nested) == "1. Inner A\n2. Inner B"


def test_a_statement_that_types_its_own_number_is_not_numbered_twice() -> None:
    assert _strip_html("<ol>\n<li>1.It consists of items</li>\n</ol>") == (
        "1.It consists of items"
    )


def test_unordered_items_are_marked_without_being_numbered() -> None:
    assert _strip_html("<ul>\n<li>Alpha</li>\n<li>Beta</li>\n</ul>") == (
        "\u2022 Alpha\n\u2022 Beta"
    )


def test_a_marker_reaches_its_text_through_the_block_tags_between_them() -> None:
    """Cells wrap their item text in ``<p>``, which must not orphan a marker."""
    assert _strip_html(
        "<ol>\n<li><p>Wrapped</p></li>\n<li><p>Second</p></li>\n</ol>"
    ) == "1. Wrapped\n2. Second"


def test_prose_without_a_list_is_left_alone() -> None:
    assert _strip_html("<p>Plain <strong>bold</strong> text</p>") == "Plain bold text"


# -------------------------------------------------------- image-only stems

def test_a_stem_that_keeps_its_content_in_a_picture_is_refused(xlsx_bytes) -> None:
    """These PDFs are built offline, so a CDN image can never be shown.

    What survives the strip is a question with its content removed, which
    cannot be answered on paper — so the language is declined rather than
    printed as a dangling stem.
    """
    options = (
        '{"english":[{"isCorrect":true,"text":"A"},'
        '{"isCorrect":false,"text":"B"}]}'
    )
    image_stem = (
        '{"english":"<p>Consider the following pairs:</p>'
        '<p><img src=\\"https://cdn.example/x.png\\" /></p>'
        '<p>How many of the pairs above are correctly matched?</p>"}'
    )
    text_stem = '{"english":"<p>Which body is statutory?</p>"}'

    result = load_question_bank(
        xlsx_bytes(
            BANK_HEADERS,
            [
                _bank_row("U-1", "QBG-IMG", image_stem, options),
                _bank_row("U-2", "QBG-TXT", text_stem, options),
            ],
            sheet_name="Questions",
        )
    )

    assert tuple(q.question_id for q in result.questions) == ("QBG-TXT",)
    assert any("image-only stem in english" in w for w in result.warnings)


# ----------------------------------------------------- the restated answer

@pytest.fixture
def pdf_export():
    return importlib.import_module("src.notes_to_dpp.pdf_export")


def test_a_restatement_that_agrees_with_the_key_is_not_printed_twice(
    pdf_export,
) -> None:
    """Nearly every solution in the bank opens by restating its own answer."""
    body = "Ans: D\nExp:\nStatement 1 is incorrect: the Act came first."

    assert pdf_export._strip_restated_answer(body, (3,)) == (
        "Statement 1 is incorrect: the Act came first."
    )


def test_a_hindi_restatement_is_recognised_through_its_spellings(
    pdf_export,
) -> None:
    body = "उत्तर : D\nव्याख्या:\nविकल्प D सही है"

    assert pdf_export._strip_restated_answer(body, (3,)) == "विकल्प D सही है"


def test_a_restatement_that_contradicts_the_key_stays_on_the_page(
    pdf_export,
) -> None:
    """About 200 bank questions disagree with their own answer key.

    Which side is right is not decidable here, so the duplicate is left where
    a reader can see it rather than tidied into a single, possibly wrong,
    answer.
    """
    body = "Ans: B\nExp:\nStatement 1 is incorrect: the Act came first."

    assert pdf_export._strip_restated_answer(body, (3,)) == body


def test_an_explanation_sharing_the_answer_line_is_kept(pdf_export) -> None:
    body = "Ans: D is correct because the Act came first."

    assert pdf_export._strip_restated_answer(body, (3,)) == body


def test_the_printed_practice_set_numbers_statements_and_answers_once(
    pdf_export,
) -> None:
    """End to end: the exporter's own label is the only one on the page."""
    localized = models.LocalizedQuestion(
        language=models.Language.ENGLISH,
        prompt="Consider the following statements:\n1. First.\n2. Second.",
        options=("1 only", "2 only", "Both", "Neither"),
        correct_option_indexes=(3,),
        solution="Ans: D\nExp:\nBoth statements are wrong.",
    )
    question = models.QuestionRecord(
        question_id="QBG-1",
        prompt=localized.prompt,
        options=localized.options,
        difficulty=models.Difficulty.EASY,
        tags=(models.QuestionTag("S-1", "C-1", "T-1"),),
        source_tags=("DPP",),
        content_fingerprint="fp-1",
        verified=True,
        localized_content=(localized,),
    )
    slot = models.SelectionSlot(
        slot_id="slot-1",
        difficulty=models.Difficulty.EASY,
        question_id="QBG-1",
        preferred_row_id="row-1",
    )

    export = pdf_export.export_practice_pdfs(
        (slot,), {"QBG-1": question}, models.Language.ENGLISH, dpp_no="3"
    )
    pymupdf = pytest.importorskip("pymupdf")

    def read(payload: bytes) -> str:
        document = pymupdf.open(stream=payload, filetype="pdf")
        try:
            return "\n".join(page.get_text() for page in document)
        finally:
            document.close()

    solutions = read(export.solutions_pdf)
    assert solutions.count("Exp:") == 1
    assert "Both statements are wrong." in solutions
    assert "DPP: 3" in solutions

    printed = read(export.question_pdf)
    assert "1. First." in printed
    assert "2. Second." in printed
