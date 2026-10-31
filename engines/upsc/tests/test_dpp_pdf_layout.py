"""The DPP PDFs keep the house layout the content team sent as the sample.

Reported as "DPP PDF wrong format": the papers carried a fixed "Practice
Questions" title, the DPP number boxed on a line of its own, bold option
letters and statements run flush with the stem. These tests read positions and
fonts back out of the PDF, because only those show the layout; the extracted
text alone was the same in the wrong format and the right one.
"""
from __future__ import annotations

import pytest

from src.notes_to_dpp import models
from src.notes_to_dpp.pdf_export import PAGE_WIDTH, export_practice_pdfs

pymupdf = pytest.importorskip("pymupdf")

STEM = ("With reference to the Administration of Cholas, consider the "
        "following statements:")
STATEMENT_1 = ("The Chola king held supreme power, enjoying all authority within "
               "the dynasty, supported by only a single minister.")
QUESTION = "Which of the statements given above is/are correct?"
SOLUTION = (
    "Ans: B\nExp:\n"
    "Statement 1 is incorrect: The Chola king held supreme power in the "
    "central administration.\n"
    "Statement 2 is correct: The empire was divided into mandals.\n"
    "• Under the reign of Rajaraja Chola and his son Rajendra Chola, the "
    "temples of Brihadeswara at Thanjavur and Gangaikonda Cholapuram were built."
)


def _question(qid: str, prompt: str, options: tuple[str, ...],
              solution: str = SOLUTION,
              language: models.Language = models.Language.ENGLISH) -> models.QuestionRecord:
    localized = models.LocalizedQuestion(
        language=language, prompt=prompt, options=options,
        correct_option_indexes=(1,), solution=solution)
    return models.QuestionRecord(
        question_id=qid, prompt=prompt, options=options,
        difficulty=models.Difficulty.EASY, tags=(models.QuestionTag("S", "C", "T"),),
        source_tags=("DPP",), content_fingerprint=f"fp-{qid}", verified=True,
        localized_content=(localized,))


def _export(questions, *, language=models.Language.ENGLISH, subject="Medieval History"):
    slots = tuple(
        models.SelectionSlot(f"slot-{i}", q.difficulty, q.question_id, "ROW")
        for i, q in enumerate(questions, start=1))
    return export_practice_pdfs(slots, questions, language, dpp_no="01", subject=subject)


def _spans(payload: bytes, page_number: int = 0) -> list[dict]:
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        page = document[page_number]
        return [span for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", ()) for span in line["spans"]
                if span["text"].strip()]


def _span(spans: list[dict], text: str) -> dict:
    return next(s for s in spans if s["text"].strip().startswith(text))


def _words(payload: bytes, page_number: int = 0) -> list[tuple]:
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        return document[page_number].get_text("words")


def _word(words, text: str, nth: int = 0) -> tuple:
    return [w for w in words if w[4] == text][nth]


def _is_bold(span: dict) -> bool:
    return "bold" in span["font"].casefold()


STATEMENTS_Q = _question(
    "Q-STATEMENTS",
    f"{STEM}\n1. {STATEMENT_1}\n2. In provincial administration, the Chola Empire "
    f"was divided into mandals.\n{QUESTION}",
    ("1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"),
)
SHORT_OPTIONS_Q = _question(
    "Q-SHORT",
    "Which one among the following coins was token currency introduced by "
    "Muhammad bin Tughlaq?",
    ("Dramma", "Pana", "Dinara", "Jittal"),
)
LONG_OPTIONS_Q = _question(
    "Q-LONG",
    "The Gangaikonda Cholapuram temple was built under the patronage of",
    ("Raja Raja I", "Rajendra Chola I", "Raj Raja Chola I", "Rajendra Raja II"),
)


@pytest.fixture(scope="module")
def export():
    return _export((SHORT_OPTIONS_Q, STATEMENTS_Q, LONG_OPTIONS_Q))


# ------------------------------------------------------------------ header

def test_the_subject_is_the_title_on_one_line_with_the_dpp_box(export) -> None:
    for payload in (export.question_pdf, export.solutions_pdf, export.combined_pdf):
        spans = _spans(payload)
        title = _span(spans, "Medieval History")
        badge = _span(spans, "DPP: 1")
        upsc = _span(spans, "UPSC")
        assert _is_bold(title) and title["size"] >= 14, title
        # Same header line: the two baselines sit within a few points.
        assert abs(title["origin"][1] - badge["origin"][1]) < 6, (title, badge)
        # The title is centred on the page; the badge is at its right end.
        middle = (title["bbox"][0] + title["bbox"][2]) / 2
        assert abs(middle - PAGE_WIDTH / 2) < 3, title["bbox"]
        assert badge["bbox"][0] > title["bbox"][2], "the badge is not right of the title"
        assert upsc["bbox"][3] < title["bbox"][1], "UPSC is not above the title line"
        assert not any("Practice Questions" in s["text"] for s in spans)


def test_a_blank_subject_keeps_the_layout_with_a_generic_title() -> None:
    exported = _export((SHORT_OPTIONS_Q,), subject="")
    spans = _spans(exported.question_pdf)
    title = _span(spans, "Practice Questions")
    badge = _span(spans, "DPP: 1")
    assert abs(title["origin"][1] - badge["origin"][1]) < 6


def test_a_hindi_subject_titles_the_hindi_paper() -> None:
    hindi = _question(
        "Q-HI", "निम्नलिखित कथनों पर विचार कीजिए:\n1. पहला कथन।\n2. दूसरा कथन।\n"
        "उपर्युक्त कथनों में से कौन-सा/से सही है/हैं?",
        ("केवल 1", "केवल 2", "1 और 2 दोनों", "न तो 1 और न ही 2"),
        solution="कथन 1 सही है: पहला कारण।", language=models.Language.HINDI)
    exported = _export((hindi,), language=models.Language.HINDI, subject="भारतीय भूगोल")
    with pymupdf.open(stream=exported.question_pdf, filetype="pdf") as document:
        text = document[0].get_text()
    assert "भारतीय भूगोल" in text
    assert "DPP: 1" in text
    assert "Practice Questions" not in text
    words = _words(exported.question_pdf)
    # Numbered Hindi statements are indented under the stem like English ones.
    assert _word(words, "1.")[0] > _word(words, "निम्नलिखित")[0] + 2


# ---------------------------------------------------------------- questions

def test_q_numbers_hang_in_a_gutter_and_statements_are_a_list(export) -> None:
    words = _words(export.question_pdf)
    q2 = _word(words, "Q2")
    stem = _word(words, "With")
    assert q2[2] < stem[0], "the stem must start after the Q2 gutter"
    assert abs(q2[3] - stem[3]) < 1, "Q2 and its stem share a line"

    number = _word(words, "1.")
    assert number[0] > stem[0] + 2, "statements are indented under the stem"
    first_text = _word(words, "The")
    # Statement 1 wraps; its second line hangs after the number.
    wrapped = next(w for w in words if w[1] > first_text[1] + 5 and w[0] > number[0]
                   and w[4] in STATEMENT_1.split())
    assert abs(wrapped[0] - first_text[0]) < 1, (wrapped, first_text)

    which = _word(words, "Which", nth=1)  # Q1 opens with "Which" too
    assert abs(which[0] - stem[0]) < 1, "the question line returns to the stem indent"


def test_short_options_form_a_two_by_two_grid(export) -> None:
    words = _words(export.question_pdf)
    a, b, c = (_word(words, label) for label in ("(A)", "(B)", "(C)"))
    stem = _word(words, "Which")
    assert abs(a[3] - b[3]) < 1 and b[0] > a[0] + 50, "(A) and (B) share a row"
    assert abs(a[0] - c[0]) < 1 and c[3] > a[3] + 5, "(C) sits under (A)"
    assert abs(a[0] - stem[0]) < 1, "options start at the stem's indent"


def test_long_options_go_one_per_line_with_plain_letters(export) -> None:
    words = _words(export.question_pdf)
    a = _word(words, "(A)", nth=2)  # the third question's options
    b = _word(words, "(B)", nth=2)
    assert abs(a[0] - b[0]) < 1 and b[3] > a[3] + 5, "one option per line"
    spans = _spans(export.question_pdf)
    assert not any(_is_bold(s) for s in spans if s["text"].strip().startswith("(A)"))


def test_q10_and_up_keep_their_stem_in_line() -> None:
    long_stem = ("Consider the following statements about the administrative "
                 "divisions of the Chola empire and the assemblies of its villages:")
    questions = tuple(_question(f"Q-{n}", long_stem, ("Yes", "No", "Both", "Neither"))
                      for n in range(1, 11))
    exported = _export(questions)
    with pymupdf.open(stream=exported.question_pdf, filetype="pdf") as document:
        pages = [page.get_text("words") for page in document]
    checked = 0
    for words in pages:
        for first in (w for w in words if w[4] == "Consider"):
            # The first word of the stem's second line, in the same column.
            right_column = first[0] > PAGE_WIDTH / 2
            second_line = [w for w in words
                           if 10 < w[1] - first[1] < 25
                           and (w[0] > PAGE_WIDTH / 2) == right_column]
            start = min(second_line, key=lambda w: w[0])
            assert abs(first[0] - start[0]) < 1, "a stem's first line is out of line"
            checked += 1
    assert checked == 10


# ---------------------------------------------------------------- solutions

def _page_with(payload: bytes, text: str) -> int:
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        return next(i for i, page in enumerate(document) if text in page.get_text())


def test_solutions_read_text_solution_ans_exp_with_marked_verdicts(export) -> None:
    payload = export.solutions_pdf
    page_number = _page_with(payload, "Text Solution:")
    spans = _spans(payload, page_number)
    heading = _span(spans, "Text Solution:")
    q1 = _span(spans, "Q1")
    assert _is_bold(heading) and _is_bold(q1)
    assert q1["bbox"][2] < heading["bbox"][0] and abs(q1["origin"][1] - heading["origin"][1]) < 1
    answer = _span(spans, "Ans:")
    assert _is_bold(answer) and "B" in answer["text"]
    assert _is_bold(_span(spans, "Exp:"))
    assert answer["bbox"][0] == pytest.approx(heading["bbox"][0], abs=1)

    verdict = _span(spans, "Statement 1 is incorrect:")
    assert _is_bold(verdict)
    rest = _span(spans, "The Chola king held")
    assert not _is_bold(rest), "only the verdict is bold, not the explanation"
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        drawings = document[page_number].get_drawings()
    x0, _, x1, bottom = verdict["bbox"]
    underlined = any(
        d["rect"].height < 2 and d["rect"].x0 <= x0 + 1 and d["rect"].x1 >= x1 - 1
        and bottom - 6 <= d["rect"].y0 <= bottom + 3
        for d in drawings)
    assert underlined, "the verdict lead-in is not underlined"


def test_solution_bullets_are_round_with_hanging_text(export) -> None:
    payload = export.solutions_pdf
    words = _words(payload, _page_with(payload, "Rajaraja"))
    bullet = _word(words, "•")
    under = _word(words, "Under")
    assert bullet[2] < under[0] and abs(bullet[3] - under[3]) < 2
    wrapped = next(w for w in words if w[1] > under[1] + 5 and w[4] in SOLUTION.split()
                   and w[0] > bullet[0])
    assert abs(wrapped[0] - under[0]) < 1, "bullet text must hang after the bullet"


def test_the_columns_are_split_by_a_rose_rule(export) -> None:
    with pymupdf.open(stream=export.question_pdf, filetype="pdf") as document:
        drawings = document[0].get_drawings()
    rules = [d for d in drawings
             if d.get("color") and abs(d["rect"].x0 - PAGE_WIDTH / 2) < 1
             and d["rect"].height > 300]
    assert rules, "no vertical column rule"
    red, green, blue = rules[0]["color"]
    assert red > green and red > blue and red < 0.9, "the rule is not the muted rose"
