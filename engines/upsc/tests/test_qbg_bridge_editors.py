"""Editor classification over synthetic QBG forms.

get_visible_editors reads real DOM geometry; everything downstream of it is
pure Python over EditorField values, so the layouts the bot was built against
are reproduced here as fixtures: the labeled form, the label-less form where
only position identifies each editor, and the traps (Video Solution, Correct
Answer, --Not Available--) that used to swallow writes.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qbg_bridge.editors import (
    EditorField,
    find_option_editors,
    find_question_editor,
    find_text_solution_editor,
    has_solution_hint,
    is_safe_solution_position_candidate,
)


def field(index, top, *, marker="", role="", section="", near="", text="", label=""):
    return EditorField(
        index=index, label=label, html="", text=text, option_marker=marker,
        near_label=near, section_label=section, role_hint=role,
        top=float(top), left=10.0)


def labeled_scq_form():
    """The hydrated QBG SCQ form with every role hint present."""
    return [
        field(0, 100, role="question", near="Question"),
        field(1, 200, marker="A", role="option"),
        field(2, 260, marker="B", role="option"),
        field(3, 320, marker="C", role="option"),
        field(4, 380, marker="D", role="option"),
        field(5, 500, role="solution", section="Solutions", text="Old solution text"),
        field(6, 620, near="Video Solution (URL)"),
    ]


def bare_scq_form():
    """The same form before Angular labels hydrate: geometry only."""
    return [
        field(0, 100),
        field(1, 200, marker="A"),
        field(2, 260, marker="B"),
        field(3, 320, marker="C"),
        field(4, 380, marker="D"),
        field(5, 500, text="Old solution text"),
    ]


class TestSolutionEditor:
    def test_the_labeled_form_picks_the_solutions_editor(self):
        assert find_text_solution_editor(labeled_scq_form()).index == 5

    def test_the_bare_form_falls_back_to_position_after_the_options(self):
        assert find_text_solution_editor(bare_scq_form()).index == 5

    def test_a_video_solution_editor_is_never_chosen(self):
        fields = [
            field(0, 100, role="question"),
            field(1, 200, marker="A"),
            field(2, 260, marker="B"),
            field(3, 320, marker="C"),
            field(4, 380, marker="D"),
            field(5, 500, near="Video Solution (URL)"),
        ]
        assert find_text_solution_editor(fields) is None

    def test_the_numerical_layout_skips_the_correct_answer_editor(self):
        # An unlabeled trailing editor is only trusted while it is empty; one
        # with content and no Solutions heading is context, not the solution.
        fields = [
            field(0, 100, role="question"),
            field(1, 300, near="Correct Answer", text="4"),
            field(2, 500),
        ]
        chosen = find_text_solution_editor(fields)
        assert chosen is not None and chosen.index == 2

        labeled = [
            field(0, 100, role="question"),
            field(1, 300, near="Correct Answer", text="4"),
            field(2, 500, section="Solutions", text="the old solution"),
        ]
        chosen = find_text_solution_editor(labeled)
        assert chosen is not None and chosen.index == 2

    def test_not_available_placeholders_are_rejected(self):
        placeholder = field(5, 500, text="--Not Available--")
        assert not is_safe_solution_position_candidate(placeholder)

    def test_hindi_headings_carry_the_solution_hint(self):
        assert has_solution_hint(field(0, 0, section="हल"))
        assert not has_solution_hint(field(0, 0, near="Video Solution"))


class TestQuestionEditor:
    def test_the_labeled_role_wins(self):
        assert find_question_editor(labeled_scq_form()).index == 0

    def test_position_above_the_options_is_the_fallback(self):
        assert find_question_editor(bare_scq_form()).index == 0

    def test_a_correct_answer_editor_above_options_is_not_the_question(self):
        fields = [
            field(0, 50, near="Correct Answer"),
            field(1, 100),
            field(2, 200, marker="A"),
            field(3, 260, marker="B"),
        ]
        assert find_question_editor(fields).index == 1

    def test_no_options_and_no_label_means_no_guess(self):
        assert find_question_editor([field(0, 100), field(1, 300)]) is None


class TestOptionEditors:
    def test_all_four_letters_are_found(self):
        editors = find_option_editors(labeled_scq_form())
        assert sorted(editors) == ["A", "B", "C", "D"]
        assert editors["C"].index == 3

    def test_duplicate_markers_keep_the_first_by_position(self):
        fields = [
            field(0, 200, marker="A"),
            field(1, 900, marker="A"),
        ]
        assert find_option_editors(fields)["A"].index == 0

    def test_unmarked_editors_are_not_options(self):
        assert find_option_editors(bare_scq_form()[:1]) == {}
