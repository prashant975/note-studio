"""The text rules the QBG bridge lives or dies by.

The comparison keys decide whether an overwrite is declared verified, and the
HTML-to-text rules decide what a MathType image becomes. Each behaviour here
was copied from the production Solution Addition Bot, so these tests pin the
copy to the original's semantics.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qbg_bridge import textops


class TestIdsAndLinks:
    def test_a_real_bank_qbgid_is_recognised(self):
        assert textops.looks_like_qbg_id("a8hri1z75sqihd0v9vphoisa5")

    def test_prose_an_upper_case_id_and_a_url_are_not_ids(self):
        assert not textops.looks_like_qbg_id("what is the answer")
        assert not textops.looks_like_qbg_id("A8HRI1Z75SQIHD0V9VPHOISA5")
        assert not textops.looks_like_qbg_id("https://qbg-admin.penpencil.co/x")

    def test_only_the_admin_host_counts_as_a_qbg_link(self):
        assert textops.is_qbg_link("https://qbg-admin.penpencil.co/anything")
        assert not textops.is_qbg_link("https://api.penpencil.co/qbg/questions")
        assert not textops.is_qbg_link("qbg-admin.penpencil.co/no-scheme")

    def test_question_link_is_empty_until_the_template_is_confirmed(self, monkeypatch):
        monkeypatch.setattr(textops, "QBG_QUESTION_LINK_TEMPLATE", "")
        assert textops.question_link("a8hri1z75sqihd0v9vphoisa5") == ""
        monkeypatch.setattr(
            textops, "QBG_QUESTION_LINK_TEMPLATE",
            "UNCONFIRMED::https://qbg-admin.penpencil.co/question-search::{qbg_id}")
        assert textops.question_link("a8hri1z75sqihd0v9vphoisa5") == ""

    def test_a_confirmed_template_substitutes_the_id(self, monkeypatch):
        monkeypatch.setattr(
            textops, "QBG_QUESTION_LINK_TEMPLATE",
            "https://qbg-admin.penpencil.co/question/{qbg_id}/edit")
        assert textops.question_link(" abc123 ") == (
            "https://qbg-admin.penpencil.co/question/abc123/edit")


class TestCompareKeys:
    def test_html_and_plain_text_agree(self):
        html_side = "<p>The Act protects <strong>wild</strong> species.</p>"
        plain_side = "The Act protects wild species."
        assert (textops.solution_compare_key(html_side)
                == textops.solution_compare_key(plain_side))

    def test_inequalities_are_not_mistaken_for_html(self):
        # The bot's own regression: 0<u<4 with a later > must survive intact.
        value = "For 0<u<4 the result is x>2 in every case."
        assert textops.solution_compare_key(value) == value

    def test_display_math_delimiters_are_normalised(self):
        assert (textops.solution_compare_key(r"\[x+1\]")
                == textops.solution_compare_key(r"\(x+1\)"))
        assert (textops.solution_compare_key("$$x+1$$")
                == textops.solution_compare_key(r"\(x+1\)"))

    def test_nbsp_and_whitespace_collapse(self):
        assert (textops.solution_compare_key("a  b\n\nc")
                == "a b c")


class TestEditorHtml:
    def test_lines_become_paragraphs_and_html_is_escaped(self):
        assert (textops.html_for_editor("first < second\nthird")
                == "<p>first &lt; second</p><p>third</p>")

    def test_blank_input_stays_blank(self):
        assert textops.html_for_editor("  \n  ") == ""

    def test_roundtrip_through_the_compare_key(self):
        text = "Line one\nLine two"
        assert (textops.solution_compare_key(textops.html_for_editor(text))
                == textops.solution_compare_key(text))


class TestMathImages:
    def test_alt_text_wins_and_math_gets_latex_wrapped(self):
        tag = '<img alt="x^2 + 1" src="foo.png">'
        assert textops.math_text_from_img_tag(tag) == "\\(x^2 + 1\\)"

    def test_a_wiris_image_without_text_is_named_not_dropped(self):
        tag = '<img class="Wirisformula" src="data:image/png;base64,x">'
        assert textops.math_text_from_img_tag(tag) == (
            "[unreadable MathType formula image]")

    def test_an_ordinary_image_becomes_a_placeholder(self):
        assert textops.math_text_from_img_tag('<img src="a.png">') == "[image]"

    def test_html_to_ai_text_inlines_the_image_label(self):
        value = '<p>Solve <img alt="\\(\\frac{a}{b}\\)" src="x"> now</p>'
        assert textops.html_to_ai_text(value) == "Solve \\(\\frac{a}{b}\\) now"


class TestSolutionStripping:
    def test_code_fences_and_labels_are_removed(self):
        assert textops.strip_for_qbg_solution(
            "```text\nSolution: The answer is B.\n```") == "The answer is B."

    def test_inline_dollar_math_is_converted(self):
        assert textops.strip_for_qbg_solution("value of $x+1$ here") == (
            "value of \\(x+1\\) here")


class TestAnswerLetters:
    def test_letters_are_parsed_from_prose(self):
        assert textops.parse_option_letters_from_answer("A and C") == ["A", "C"]
        assert textops.parse_option_letters_from_answer("(b)") == ["B"]
        assert textops.parse_option_letters_from_answer("Correct option is D") == ["D"]

    def test_non_letter_answers_yield_nothing(self):
        assert textops.parse_option_letters_from_answer("42") == []
        assert textops.parse_option_letters_from_answer("E") == []

    def test_normalize_deduplicates_and_uppercases(self):
        assert textops.normalize_option_letters(["a", "A", "c"]) == ["A", "C"]

    def test_bank_indexes_become_letters(self):
        assert textops.indexes_to_letters((0, 2)) == ("A", "C")
        assert textops.indexes_to_letters((3, 3, 9, -1, "x")) == ("D",)
        assert textops.indexes_to_letters(()) == ()


class TestNames:
    def test_language_names(self):
        assert textops.normalize_language(" en ") == "English"
        assert textops.normalize_language("HINDI") == "Hindi"
        assert textops.normalize_language("Both") == ""

    def test_question_types_match_the_portal_labels(self):
        assert textops.normalize_question_type_value("Single Choice (SCQ)") == "SCQ"
        assert textops.normalize_question_type_value("comprehension") == "COMP"
        assert textops.normalize_question_type_value("-Select-") == ""
        assert textops.normalize_question_type_value("Matching List") == "ML"
