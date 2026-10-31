"""Run the real preview with controlled pools, including an exhausted topic."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.notes_to_dpp.models import Difficulty, QuestionTag, SelectionSlot


class Rerun(Exception):
    pass


class FakeStreamlit:
    def __init__(self, clicked=False):
        self.clicked = clicked
        self.buttons = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def columns(self, spec):
        return [self] * len(spec)

    def markdown(self, text):
        pass

    def divider(self):
        pass

    def button(self, label, **kwargs):
        self.buttons.append((label, kwargs))
        return self.clicked and label == "Replace" and not kwargs.get("disabled")

    def rerun(self):
        raise Rerun


def _preview(st, bank):
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    function = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "_render_dpp_preview")
    state, invalidated = {}, []
    namespace = {
        "st": st, "_dpp_question_map": lambda bank: {q.question_id: q for q in bank.questions},
        "_dpp_state": lambda scope, key, default=None: state.get(key, default),
        "_dpp_set": lambda scope, key, value: state.__setitem__(key, value),
        "_dpp_invalidate_exports": invalidated.append,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "dpp_preview", "exec"), namespace)
    return namespace["_render_dpp_preview"], state, invalidated


@pytest.mark.parametrize("duplicate", [False, True])
def test_an_exhausted_replacement_is_disabled_and_explained(
    taxonomy_row_factory, question_factory, duplicate,
):
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    q = question_factory("CURRENT", Difficulty.EASY, tag, fingerprint="current")
    questions = [q]
    if duplicate:
        questions.append(question_factory("COPY", Difficulty.EASY, tag, fingerprint="current"))
    st = FakeStreamlit()
    render, state, invalidated = _preview(st, questions)
    render("scope", [SelectionSlot("slot", Difficulty.EASY, q.question_id, row.row_id)],
           SimpleNamespace(questions=questions), [row], None)
    replace = next(kw for label, kw in st.buttons if label == "Replace")
    assert replace["disabled"]
    assert replace["help"] == "No unused Easy replacement remains."
    assert not state and not invalidated


def test_an_available_replacement_changes_the_slot_and_invalidates_exports(
    taxonomy_row_factory, question_factory,
):
    row = taxonomy_row_factory()
    tag = QuestionTag(row.subject_id, row.chapter_id, row.topic_id)
    current = question_factory("CURRENT", Difficulty.EASY, tag)
    unused = question_factory("UNUSED", Difficulty.EASY, tag)
    questions = [current, unused]
    st = FakeStreamlit(clicked=True)
    render, state, invalidated = _preview(st, questions)
    with pytest.raises(Rerun):
        render("scope", [SelectionSlot("slot", Difficulty.EASY, current.question_id, row.row_id)],
               SimpleNamespace(questions=questions), [row], None)
    assert state["slots"][0].question_id == unused.question_id
    assert unused.question_id in state["seen_ids"]
    assert invalidated == ["scope"]
