"""Generating practice questions, and answering only the ones that were ticked.

The expensive rule is tested hardest. Questions cost three model calls; answers
cost one each. A bug that answers all ten when the user ticked two is not a
wrong output — the answers would all be fine — it is a five-fold bill that
nobody notices until the month ends. So the call count is asserted, not just the
result.
"""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp import service, store
from src.dmp.models import STATUS_ANSWERED
from src.dmp.service import DmpError

NOTE_ID = "run_20260821_101500_abc12345"

NOTES = "\n".join([
    "#LECTURE_TITLE: L04_DCN04_Indian Society:",
    "",
    "#H1: Tribal Communities",
    "",
    "#FIGURE:fig1",
    "- **Article 342** empowers the President to notify Scheduled Tribes for a State.",
    "- The **RTI Act, 2005** gave citizens a statutory right to seek information.",
] + [f"- Point {i} about the social structure discussed in this class in detail."
     for i in range(60)])


def _questions_json(prefix: str = "Q") -> str:
    items = ", ".join(
        f'{{"marks": {10 if i % 2 else 15}, "text": "{prefix} question number {i}?"}}'
        for i in range(5))
    return f'{{"questions": [{items}]}}'


class Resp:
    def __init__(self, text: str):
        self.text, self.model, self.provider, self.usage = text, "gemini-3.5-flash", "fake", {}


class Client:
    """Records every prompt it is asked for, so the tests can count calls."""

    def __init__(self, *, question_reply=None, answer_reply="A model answer.",
                 fail_with: Exception | None = None):
        self.question_reply = question_reply or _questions_json()
        self.answer_reply = answer_reply
        self.fail_with = fail_with
        self.model = "gemini-3.5-flash"
        self.fallback_notices: list[str] = []
        self.prompts: list[str] = []

    def generate(self, prompt, image_paths=None, **kw):
        self.prompts.append(prompt)
        if self.fail_with:
            raise self.fail_with
        if "Return ONLY valid JSON" in prompt:
            return Resp(self.question_reply)
        return Resp(self.answer_reply)

    @property
    def answer_calls(self) -> int:
        return sum(1 for p in self.prompts if "QUESTION (" in p)

    @property
    def question_calls(self) -> int:
        return sum(1 for p in self.prompts if "Return ONLY valid JSON" in p)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "language": "mr", "subject": "society", "subject_line": "भारतीय समाज",
    }), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------ grounding content

def test_the_grounding_is_the_notes_that_actually_shipped(runs: Path):
    """notes_final.md, not notes_raw. Between them sit the QC corrections — and
    setting questions on the uncorrected draft grounds them in known-wrong facts."""
    (runs / NOTE_ID / "notes_raw.md").write_text(
        "#H1: Draft\n" + "- wrong fact about everything here. " * 60, encoding="utf-8")
    text = service.grounding_content(NOTE_ID, runs)
    assert "Article 342" in text
    assert "wrong fact" not in text


def test_the_app_s_own_markers_are_resolved_not_shipped_to_the_model(runs: Path):
    text = service.grounding_content(NOTE_ID, runs)
    assert "#H1:" not in text and "#FIGURE:" not in text
    assert "# Tribal Communities" in text or "## Tribal Communities" in text


def test_an_older_run_without_the_final_file_still_works(runs: Path):
    """Runs generated before notes_final.md existed are the majority of what is
    on any installed machine."""
    (runs / NOTE_ID / "notes_final.md").unlink()
    (runs / NOTE_ID / "notes_repaired.md").write_text(NOTES, encoding="utf-8")
    assert "Article 342" in service.grounding_content(NOTE_ID, runs)


def test_a_note_too_thin_to_examine_says_so(runs: Path):
    (runs / NOTE_ID / "notes_final.md").write_text("#H1: Short\n- One line.",
                                                   encoding="utf-8")
    with pytest.raises(DmpError, match="enough content"):
        service.grounding_content(NOTE_ID, runs)


def test_a_deleted_run_folder_is_explained_rather_than_crashed(runs: Path):
    with pytest.raises(DmpError, match="no longer on this machine"):
        service.grounding_content("run_20200101_000000_deadbeef", runs)


def test_the_note_context_follows_the_notes_language_and_title(runs: Path):
    context = service.note_context(NOTE_ID, runs)
    assert context["language"] == "mr"
    assert context["note_title"] == "Indian Society"
    assert "भारतीय समाज" in context["topic"]
    assert "L04_DCN04" not in context["topic"]


# --------------------------------------------------------------- parsing replies

@pytest.mark.parametrize("reply", [
    _questions_json(),
    "```json\n" + _questions_json() + "\n```",
    "Here are the questions:\n\n" + _questions_json() + "\n\nHope this helps!",
])
def test_json_is_recovered_from_the_wrappings_models_add(reply):
    """Fences and a friendly sentence are the two commonest malformations, and
    both are recoverable for free — no second call needed. The five-question
    reply is clipped to the level's own count (easy takes four)."""
    assert len(service.parse_questions(reply, "easy")) == 4


def test_trailing_commas_are_repaired_rather_than_billed_for():
    reply = '{"questions": [{"marks": 10, "text": "A question?"},]}'
    assert len(service.parse_questions(reply, "easy")) == 1


def test_a_repeated_question_is_dropped():
    """A set with the same question twice reads as a bug in the app. A short set
    is cheaper to explain."""
    reply = ('{"questions": [{"marks": 10, "text": "Same question?"},'
             '{"marks": 15, "text": "same question"},'
             '{"marks": 10, "text": "A different one?"}]}')
    assert len(service.parse_questions(reply, "easy")) == 2


def test_an_unusable_reply_yields_nothing_rather_than_a_traceback():
    assert service.parse_questions("I cannot help with that.", "easy") == []
    assert service.parse_questions("", "easy") == []


# ------------------------------------------------------------ generating questions

def test_four_three_three_questions_per_level_in_the_notes_language(runs: Path):
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)

    assert [len(session.level_questions(lv)) for lv in ("easy", "moderate", "applied")] \
        == [4, 3, 3]
    assert client.question_calls == 3, "one call per level, not one per question"
    assert client.answer_calls == 0, "no answer may be generated up front"
    assert session.language == "mr"
    assert all("Marathi (Devanagari)" in p for p in client.prompts)
    assert {q.marks for q in session.questions} <= {10, 15}


def test_the_new_set_is_on_disk_before_the_call_returns(runs: Path):
    session = service.generate_questions(NOTE_ID, Client(), runs_dir=runs)
    assert store.load(NOTE_ID, session.dmp_session_id, runs) is not None
    assert store.list_sessions(NOTE_ID, runs)[0]["pending"] == 10


def test_a_malformed_reply_is_retried_exactly_once(runs: Path):
    """Once, because the failure is a formatting slip. A model that has ignored
    the format twice is not about to comply on the third ask — it is going to
    keep billing."""
    class Flaky(Client):
        def generate(self, prompt, image_paths=None, **kw):
            self.prompts.append(prompt)
            if "could not be read as JSON" in prompt:
                return Resp(_questions_json())
            return Resp("Sorry, here is a list instead: 1. What is federalism?")

    client = Flaky()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    assert len(session.questions) == 10
    assert client.question_calls == 6, "three levels, one repair each"


def test_a_dead_service_is_reported_in_words_a_person_can_act_on(runs: Path):
    from src.ai_client import GeminiError

    client = Client(fail_with=GeminiError("gemini proxy error 429: rate limited"))
    with pytest.raises(DmpError) as excinfo:
        service.generate_questions(NOTE_ID, client, runs_dir=runs)
    message = str(excinfo.value)
    assert "rate-limited" in message or "quota" in message
    assert "429" not in message and "Traceback" not in message


def test_generating_without_a_client_asks_the_user_to_sign_in(runs: Path):
    with pytest.raises(DmpError, match="Sign in"):
        service.generate_questions(NOTE_ID, None, runs_dir=runs)


# -------------------------------------------------------------- generating answers

def test_only_the_ticked_questions_are_answered(runs: Path):
    """The rule the whole design exists for."""
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    chosen = [session.questions[0].id, session.questions[7].id]

    updated = service.generate_answers(NOTE_ID, session.dmp_session_id, chosen,
                                       client, runs_dir=runs)
    assert client.answer_calls == 2, "one call per ticked question, and no more"
    answered = [q.id for q in updated.questions if q.answered]
    assert answered == chosen
    assert updated.pending_count == 8


def test_an_answered_question_is_never_re_billed(runs: Path):
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    first = session.questions[0].id
    service.generate_answers(NOTE_ID, session.dmp_session_id, [first], client,
                             runs_dir=runs)
    assert client.answer_calls == 1

    service.generate_answers(NOTE_ID, session.dmp_session_id, [first], client,
                             runs_dir=runs)
    assert client.answer_calls == 1, "the stored answer should have been reused"


def test_regenerate_is_the_explicit_way_to_pay_again(runs: Path):
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    first = session.questions[0].id
    service.generate_answers(NOTE_ID, session.dmp_session_id, [first], client,
                             runs_dir=runs)
    client.answer_reply = "A better answer."
    updated = service.generate_answers(NOTE_ID, session.dmp_session_id, [first],
                                       client, regenerate=True, runs_dir=runs)
    assert client.answer_calls == 2
    assert updated.question(first).answer == "A better answer."


def test_an_answer_records_what_wrote_it_and_when(runs: Path):
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    updated = service.generate_answers(NOTE_ID, session.dmp_session_id,
                                       [session.questions[0].id], client, runs_dir=runs)
    answered = updated.questions[0]
    assert answered.status == STATUS_ANSWERED
    assert answered.model_used == "gemini-3.5-flash"
    assert answered.answer_generated_at


def test_each_answer_is_saved_as_it_arrives(runs: Path):
    """Answers arrive over tens of seconds each. A browser closed midway through
    five of them should keep the ones already paid for."""
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    chosen = [q.id for q in session.questions[:3]]

    saves: list[int] = []
    original_save = store.save

    def counting_save(sess, runs_dir=None):
        saves.append(sess.answered_count)
        return original_save(sess, runs_dir)

    store.save = counting_save
    try:
        service.generate_answers(NOTE_ID, session.dmp_session_id, chosen, client,
                                 runs_dir=runs)
    finally:
        store.save = original_save
    assert saves == [1, 2, 3], "saved once per answer, not once at the end"


def test_answering_nothing_is_a_hint_not_a_request(runs: Path):
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    with pytest.raises(DmpError, match="Select at least one"):
        service.generate_answers(NOTE_ID, session.dmp_session_id, [], client,
                                 runs_dir=runs)
    assert client.answer_calls == 0


def test_a_missing_practice_set_is_a_friendly_message(runs: Path):
    with pytest.raises(DmpError, match="could not be found"):
        service.generate_answers(NOTE_ID, "dmp_deadbeef", ["q_1"], Client(),
                                 runs_dir=runs)


def test_the_answer_prompt_matches_the_question_s_own_level(runs: Path):
    """An applied question answered with the easy template gets a three-part
    outline where the question needed five dimensions and a way forward."""
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    applied = session.level_questions("applied")[0]
    service.generate_answers(NOTE_ID, session.dmp_session_id, [applied.id], client,
                             runs_dir=runs)
    answer_prompt = [p for p in client.prompts if "QUESTION (" in p][-1]
    assert "Think like an administrator" in answer_prompt
    assert "policy dilemmas, implementation bottlenecks" in answer_prompt


def test_reopening_later_finds_the_answers_and_the_rest_still_answerable(runs: Path):
    """The come-back-tomorrow path, end to end."""
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    monday = [q.id for q in session.questions[:2]]
    service.generate_answers(NOTE_ID, session.dmp_session_id, monday, client,
                             runs_dir=runs)

    # Tuesday: a new process, nothing in memory.
    reopened = service.get_session(NOTE_ID, session.dmp_session_id, runs)
    assert reopened.answered_count == 2
    assert all(reopened.question(qid).answer for qid in monday)

    tuesday = [reopened.questions[5].id]
    client.answer_reply = "Tuesday's answer."
    final = service.generate_answers(NOTE_ID, session.dmp_session_id, tuesday, client,
                                     runs_dir=runs)
    assert final.answered_count == 3
    assert client.answer_calls == 3, "Monday's answers were not regenerated"


def test_the_workflow_key_is_never_mistaken_for_the_subject(tmp_path: Path):
    """On the UPSC workflow `subject` in the metadata is the WORKFLOW ("upsc")
    and the real subject is in `area`. Reading the wrong one puts the word
    "Upsc" at the top of a practice sheet — the exact thing
    subject_display_name refuses to print.
    """
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "language": "en", "subject": "upsc", "area": "society", "workflow": "upsc",
    }), encoding="utf-8")

    context = service.note_context(NOTE_ID, tmp_path)
    assert context["subject"] == "society"
    assert context["subject_line"] == "Indian Society"
    assert "Upsc" not in context["topic"]


def test_a_run_with_no_recognisable_subject_says_nothing_rather_than_guessing(
        tmp_path: Path):
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "language": "en", "subject": "upsc", "area": "auto",
    }), encoding="utf-8")
    context = service.note_context(NOTE_ID, tmp_path)
    assert context["subject_line"] == ""
    assert context["topic"] == "Indian Society"


def test_a_truncated_answer_is_saved_with_a_visible_caution(runs: Path):
    """The detailed-answer change made answers longer, so hitting the output
    cap became possible. What was paid for is kept, but the cut is said out
    loud in the answer itself — which is also what the exports print — rather
    than shipped as if it were finished."""
    class TruncResp(Resp):
        def __init__(self, text):
            super().__init__(text)
            self.truncated = True
            self.finish_reason = "MAX_TOKENS"

    class TruncatingClient(Client):
        def generate(self, prompt, image_paths=None, **kw):
            self.prompts.append(prompt)
            if "Return ONLY valid JSON" in prompt:
                return Resp(self.question_reply)
            return TruncResp("## Introduction\nA long answer that stops mid-")

    client = TruncatingClient()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)
    first = session.questions[0].id
    updated = service.generate_answers(NOTE_ID, session.dmp_session_id, [first],
                                       client, runs_dir=runs)

    answered = next(q for q in updated.questions if q.id == first)
    assert answered.answered
    assert "A long answer that stops mid-" in answered.answer
    assert "may end abruptly" in answered.answer, (
        "a truncated answer shipped with no visible caution")
