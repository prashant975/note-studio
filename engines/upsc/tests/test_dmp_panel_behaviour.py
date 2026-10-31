"""The DMP panel actually run, against a stubbed Streamlit.

The sibling file asserts what the panel's source is allowed to contain. This one
executes it. Both are needed: a panel can satisfy every source rule and still
raise NameError on the first click, and a Streamlit page's exceptions are shown
to the user rather than only logged.

The panel's functions are exec'd out of app.py because importing app.py runs the
whole page — auth, uploaders, the lot. Lifting the block is what makes it
testable without a browser.
"""
from pathlib import Path
import json
import sys
import time
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dmp import service, store
from src.utils import format_duration

NOTE_ID = "run_20260821_101500_abc12345"
NOTES = "\n".join(
    ["#LECTURE_TITLE: L04_DCN04_Indian Society:", "", "#H1: Tribal Communities", ""]
    + [f"- Point {i} about the social structure discussed in this class in detail."
       for i in range(60)])


class Rerun(Exception):
    """What st.rerun does: abandons the render and starts again."""


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeStreamlit:
    """Enough of Streamlit to run the panel and see what it did."""

    def __init__(self, clicked=(), ticked=()):
        self.clicked = set(clicked)
        self.session_state = {f"dmp_pick_{k}": True for k in ticked}
        self.errors: list[str] = []
        self.captions: list[str] = []
        self.markdown_calls: list[str] = []
        self.buttons: list[tuple[str, bool]] = []
        self.checkboxes: list[str] = []
        self.downloads: list[str] = []
        self.text_inputs: list[str] = []
        self.warnings: list[str] = []

    # -- widgets that return something -------------------------------------
    def button(self, label, key=None, **kw):
        pressed = key in self.clicked and not kw.get("disabled")
        self.buttons.append((label, bool(kw.get("disabled"))))
        return pressed

    def checkbox(self, label, key=None, **kw):
        self.checkboxes.append(label)
        return bool(self.session_state.get(key))

    def selectbox(self, label, options, **kw):
        options = list(options)
        return options[kw.get("index", 0)] if options else None

    def text_input(self, label, key=None, **kw):
        self.text_inputs.append(label)
        return self.session_state.get(key, kw.get("value", ""))

    # -- output ------------------------------------------------------------
    def error(self, message, **kw):
        self.errors.append(str(message))

    def warning(self, message, **kw):
        self.warnings.append(str(message))

    def caption(self, message, **kw):
        self.captions.append(str(message))

    def markdown(self, message, **kw):
        self.markdown_calls.append(str(message))

    def divider(self, **kw):
        pass

    def download_button(self, label, *a, **kw):
        self.downloads.append(label)

    # -- layout ------------------------------------------------------------
    def expander(self, label, **kw):
        return _Ctx()

    def spinner(self, label, **kw):
        return _Ctx()

    def columns(self, spec, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Ctx() for _ in range(n)]

    def rerun(self):
        raise Rerun


def _panel_namespace(fake_st, client):
    """The panel's functions, with the page's collaborators stubbed."""
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    block = source[source.index("def _dmp_state_key"):
                   source.index("def _render_dcn_render_status")]

    def _download_from_path(label, path, key, missing, reason=""):
        fake_st.downloads.append(label if path and Path(path).exists() else f"MISSING:{label}")

    namespace = {
        "st": fake_st,
        "pw_access": types.SimpleNamespace(new_task_id=lambda prefix="task": f"{prefix}_1"),
        "DEFAULT_MODEL": "gemini-3.5-flash",
        "_google_proxy_token": lambda: "token" if client is not None else "",
        "_download_from_path": _download_from_path,
        "Path": Path,
        # The panel reports how long a set took to write, so it needs the
        # clock and the formatter the page hands it.
        "time": time,
        "format_duration": format_duration,
    }
    exec(compile(block, "app_dmp_panel", "exec"), namespace)
    # The client factory is the one collaborator that must not really call out.
    namespace["_dmp_client"] = lambda: client
    return namespace


class Resp:
    def __init__(self, text):
        self.text, self.model = text, "gemini-3.5-flash"


class Client:
    def __init__(self):
        self.model = "gemini-3.5-flash"
        self.prompts: list[str] = []

    def generate(self, prompt, image_paths=None, **kw):
        self.prompts.append(prompt)
        if "Return ONLY valid JSON" in prompt:
            items = ", ".join(f'{{"marks": 10, "text": "Question {i}?"}}' for i in range(5))
            return Resp('{"questions": [' + items + ']}')
        return Resp("## Introduction\nAn answer.")

    @property
    def answer_calls(self):
        return sum(1 for p in self.prompts if "QUESTION (" in p)


@pytest.fixture
def runs(tmp_path: Path, monkeypatch) -> Path:
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(
        json.dumps({"language": "en", "subject": "society"}), encoding="utf-8")
    # The panel calls the service with no runs_dir, so the default has to move.
    # Both modules need it now: the store resolves every path, and list_notes
    # scans the runs root itself to build the standalone picker.
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(service, "RUNS_DIR", tmp_path)
    return tmp_path


def _result(runs: Path) -> dict:
    return {"run_dir": str(runs / NOTE_ID)}


# --------------------------------------------------------------- first visit

def test_a_fresh_note_offers_one_button_and_calls_nothing(runs: Path):
    fake = FakeStreamlit()
    client = Client()
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert [label for label, _ in fake.buttons] == ["Generate Practice Questions"]
    assert client.prompts == [], "merely opening the panel must not call the model"


def test_pressing_generate_creates_a_set_and_reruns(runs: Path):
    fake = FakeStreamlit(clicked={"dmp_gen_0"})
    client = Client()
    ns = _panel_namespace(fake, client)
    with pytest.raises(Rerun):
        ns["_render_dmp_panel"](_result(runs), 0)

    rows = service.list_sessions(NOTE_ID, runs)
    assert len(rows) == 1 and rows[0]["total"] == 10
    assert client.answer_calls == 0
    assert fake.session_state["dmp_open_session_0"] == rows[0]["dmp_session_id"]


def test_a_run_folder_that_is_gone_explains_itself(runs: Path):
    fake = FakeStreamlit()
    ns = _panel_namespace(fake, Client())
    ns["_render_dmp_panel"]({"run_dir": str(runs / "run_20200101_000000_deadbeef")}, 0)

    assert fake.buttons == [], "no button should be offered"
    assert any("not on this machine" in c for c in fake.captions)


def test_a_signed_out_user_is_told_rather_than_failing(runs: Path):
    fake = FakeStreamlit(clicked={"dmp_gen_0"})
    ns = _panel_namespace(fake, None)
    ns["_render_dmp_panel"](_result(runs), 0)
    assert any("Sign in again" in e for e in fake.errors)


def test_a_dead_service_shows_a_sentence_not_a_traceback(runs: Path):
    from src.ai_client import GeminiError

    class Dead(Client):
        def generate(self, prompt, image_paths=None, **kw):
            raise GeminiError("gemini proxy error 429: rate limited")

    fake = FakeStreamlit(clicked={"dmp_gen_0"})
    ns = _panel_namespace(fake, Dead())
    ns["_render_dmp_panel"](_result(runs), 0)

    assert fake.errors, "the failure must be reported"
    assert "429" not in fake.errors[0]
    assert "rate-limited" in fake.errors[0] or "quota" in fake.errors[0]


# ------------------------------------------------------------ the second visit

def _existing_set(runs: Path, client: Client):
    return service.generate_questions(NOTE_ID, client, runs_dir=runs)


def test_an_existing_set_renders_its_questions_as_checkboxes(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert len(fake.checkboxes) == 10
    labels = [label for label, _ in fake.buttons]
    assert any(label.startswith("Generate Model Answers") for label in labels)


def test_the_answer_button_is_disabled_with_nothing_ticked(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    disabled = {label: state for label, state in fake.buttons}
    answer_label = next(l for l in disabled if l.startswith("Generate Model Answers"))
    assert disabled[answer_label] is True
    assert "(0 selected)" in answer_label


def test_only_the_ticked_questions_are_answered_through_the_panel(runs: Path):
    """The whole cost story, exercised through the UI rather than the service."""
    client = Client()
    session = _existing_set(runs, client)
    chosen = [session.questions[0].id, session.questions[9].id]

    fake = FakeStreamlit(clicked={"dmp_answer_0"})
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    for qid in chosen:
        fake.session_state[f"dmp_pick_0_{session.dmp_session_id}_{qid}"] = True

    ns = _panel_namespace(fake, client)
    with pytest.raises(Rerun):
        ns["_render_dmp_panel"](_result(runs), 0)

    assert client.answer_calls == 2
    saved = service.get_session(NOTE_ID, session.dmp_session_id, runs)
    assert [q.id for q in saved.questions if q.answered] == chosen


def test_the_ticks_remain_available_for_the_selected_question_download(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    qid = session.questions[0].id
    key = f"dmp_pick_0_{session.dmp_session_id}_{qid}"

    fake = FakeStreamlit(clicked={"dmp_answer_0"})
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    fake.session_state[key] = True

    ns = _panel_namespace(fake, client)
    with pytest.raises(Rerun):
        ns["_render_dmp_panel"](_result(runs), 0)
    assert fake.session_state[key] is True

    # On the next render it remains selected for the question paper, but it
    # cannot trigger a duplicate paid answer-generation request.
    ns["_render_dmp_panel"](_result(runs), 0)
    disabled = {label: state for label, state in fake.buttons}
    answer_label = next(label for label in disabled
                        if label.startswith("Generate Model Answers") and "(0 selected)" in label)
    assert disabled[answer_label] is True


def test_an_answered_question_remains_selectable_for_question_download(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    service.generate_answers(NOTE_ID, session.dmp_session_id,
                             [session.questions[0].id], client, runs_dir=runs)

    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert len(fake.checkboxes) == 10, "all questions can be selected for the PDF"
    assert any("Answered" in label for label in fake.checkboxes)
    assert any("An answer." in m for m in fake.markdown_calls)


def test_new_practice_set_clears_the_selection_without_generating(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    before = len(client.prompts)

    fake = FakeStreamlit(clicked={"dmp_new_0"})
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    with pytest.raises(Rerun):
        ns["_render_dmp_panel"](_result(runs), 0)

    assert "dmp_open_session_0" not in fake.session_state
    assert len(client.prompts) == before, "no model call for a navigation button"
    assert len(service.list_sessions(NOTE_ID, runs)) == 1, "and no second set"


def test_reopening_shows_the_saved_progress(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    service.generate_answers(NOTE_ID, session.dmp_session_id,
                             [q.id for q in session.questions[:3]], client, runs_dir=runs)

    # A fresh page load: nothing in session state but the id.
    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert any("3 answered" in m and "7 to go" in m for m in fake.markdown_calls)


def test_with_no_stored_id_the_panel_reopens_the_latest_set(runs: Path):
    """The come-back-tomorrow path: a new browser session has no state at all."""
    client = Client()
    session = _existing_set(runs, client)
    fake = FakeStreamlit()
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert fake.session_state.get("dmp_open_session_0") == session.dmp_session_id
    assert len(fake.checkboxes) == 10


# ------------------------------------------------ the panel speaks the notes

def _hindi_runs(tmp_path: Path, monkeypatch) -> Path:
    run_dir = tmp_path / NOTE_ID
    run_dir.mkdir(parents=True)
    (run_dir / "notes_final.md").write_text(NOTES, encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(
        json.dumps({"language": "hi", "subject": "polity"}), encoding="utf-8")
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(service, "RUNS_DIR", tmp_path)
    return tmp_path


def test_hindi_notes_get_a_hindi_panel(tmp_path: Path, monkeypatch):
    """The questions always followed the note's language. The scaffolding round
    them did not, so a Hindi set arrived inside "Generate Practice Questions"."""
    runs = _hindi_runs(tmp_path, monkeypatch)
    fake = FakeStreamlit()
    ns = _panel_namespace(fake, Client())
    ns["_render_dmp_panel"]({"run_dir": str(runs / NOTE_ID)}, 0)

    labels = [label for label, _ in fake.buttons]
    assert labels == ["अभ्यास प्रश्न बनाएँ"], labels
    assert any("अभ्यास प्रश्न" in c for c in fake.captions)


def test_the_level_headings_follow_the_notes_language(tmp_path: Path, monkeypatch):
    runs = _hindi_runs(tmp_path, monkeypatch)
    client = Client()
    session = service.generate_questions(NOTE_ID, client, runs_dir=runs)

    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"]({"run_dir": str(runs / NOTE_ID)}, 0)

    assert any("उत्तरित" in m for m in fake.markdown_calls), fake.markdown_calls
    answer_button = [l for l, _ in fake.buttons if "मॉडल उत्तर" in l]
    assert answer_button, [l for l, _ in fake.buttons]


def test_an_english_note_still_reads_in_english(runs: Path):
    """The fallback path, which is every note generated before this existed."""
    fake = FakeStreamlit()
    ns = _panel_namespace(fake, Client())
    ns["_render_dmp_panel"](_result(runs), 0)
    assert [l for l, _ in fake.buttons] == ["Generate Practice Questions"]


# ------------------------------------------- practice without generating notes

def test_the_library_lists_notes_that_are_already_on_disk(runs: Path):
    notes = service.list_notes(runs)
    assert [n["note_id"] for n in notes] == [NOTE_ID]
    label = service.note_label(notes[0])
    assert "Indian Society" in label
    assert "[EN]" in label


def test_the_library_offers_practice_without_touching_the_pipeline(runs: Path):
    """The point of the feature: questions from a lecture generated last week,
    with no notes run and nothing regenerated."""
    fake = FakeStreamlit(clicked={f"dmp_gen_lib_{NOTE_ID}"})
    client = Client()
    ns = _panel_namespace(fake, client)
    with pytest.raises(Rerun):
        ns["_render_dmp_library"]()

    rows = service.list_sessions(NOTE_ID, runs)
    assert len(rows) == 1 and rows[0]["total"] == 10
    assert client.answer_calls == 0


def test_the_library_hides_itself_when_there_is_nothing_to_practise(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(service, "RUNS_DIR", tmp_path)
    fake = FakeStreamlit()
    ns = _panel_namespace(fake, Client())
    ns["_render_dmp_library"]()
    assert fake.buttons == [] and fake.checkboxes == []


def test_a_run_with_no_usable_notes_is_not_offered(tmp_path: Path, monkeypatch):
    """A folder that exists but holds nothing to examine would give a picker
    entry that fails the moment it is chosen."""
    (tmp_path / "run_20260101_000000_deadbeef").mkdir(parents=True)
    thin = tmp_path / "run_20260102_000000_cafe0000"
    thin.mkdir(parents=True)
    (thin / "notes_final.md").write_text("#H1: Too short\n- One line.", encoding="utf-8")
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    assert service.list_notes(tmp_path) == []


def test_the_library_keys_its_widgets_by_note_not_by_position(runs: Path):
    """The list reorders as new notes are generated. A position key would move
    somebody's practice set out from under them on the next run."""
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    library = source[source.index("def _render_dmp_library"):]
    assert 'f"lib_{note_id}"' in library


# ------------------------------------------ what the downloaded sheets print

def _runs_with_identifiers(runs: Path) -> None:
    (runs / NOTE_ID / "run_metadata.json").write_text(json.dumps({
        "language": "en", "subject": "society",
        "subject_line": "Society, Social Issues, Social Justice", "dmp_no": "19",
    }), encoding="utf-8")


def test_the_sheet_boxes_are_prefilled_from_the_lecture(runs: Path):
    """Subject the way the DPP builder finds it, the lecture title as the
    topic, and the lecture's saved DMP No."""
    _runs_with_identifiers(runs)
    client = Client()
    session = _existing_set(runs, client)
    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    _panel_namespace(fake, client)["_render_dmp_panel"](_result(runs), 0)

    assert fake.text_inputs == ["Subject printed on the sheets",
                                "Topic printed under it", "DMP No."]
    assert fake.session_state["dmp_print_subject_0"] == "Society, Social Issues, Social Justice"
    assert fake.session_state["dmp_print_topic_0"] == session.note_title
    assert fake.session_state["dmp_print_dmp_no_0"] == "19"


def test_a_built_sheet_prints_what_the_boxes_say(runs: Path, monkeypatch):
    """An edited box reaches the export, and a sheet built before the edit is
    not offered as if it said the new thing."""
    from src.dmp import export as dmp_export

    client = Client()
    session = _existing_set(runs, client)
    qid = session.questions[0].id
    calls = []

    def fake_export(session, kind, out_dir, **kw):
        calls.append(kw)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        docx = out / f"{kind}.docx"
        docx.write_bytes(b"docx")
        return docx, None, []

    monkeypatch.setattr(dmp_export, "export", fake_export)
    fake = FakeStreamlit(clicked={"dmp_build_questions_0"})
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    fake.session_state[f"dmp_pick_0_{session.dmp_session_id}_{qid}"] = True
    fake.session_state["dmp_print_subject_0"] = "Geography"
    fake.session_state["dmp_print_topic_0"] = ""
    fake.session_state["dmp_print_dmp_no_0"] = "5"
    ns = _panel_namespace(fake, client)
    ns["_render_dmp_panel"](_result(runs), 0)

    assert calls and calls[0]["subject"] == "Geography"
    assert calls[0]["topic"] == "" and calls[0]["dmp_no"] == "5"
    assert calls[0]["selected_ids"] == [qid]
    assert "Questions DOCX" in fake.downloads

    # The teacher changes the subject: the old file is withdrawn, not served.
    fake.clicked.clear()
    fake.downloads.clear()
    fake.session_state["dmp_print_subject_0"] = "Polity"
    ns["_render_dmp_panel"](_result(runs), 0)
    assert "Questions DOCX" not in fake.downloads


def test_a_long_answer_is_flagged_beside_it_and_kept_whole(runs: Path):
    """The post-check: a word count, shown in the app's warning style. The
    answer itself is stored and shown in full."""
    long_answer = "# ANSWER\n" + " ".join(["word"] * 260)

    class LongClient(Client):
        def generate(self, prompt, image_paths=None, **kw):
            if "Return ONLY valid JSON" in prompt:
                return super().generate(prompt, image_paths, **kw)
            self.prompts.append(prompt)
            return Resp(long_answer)

    client = LongClient()
    session = _existing_set(runs, client)
    ten_mark = session.questions[0]
    service.generate_answers(NOTE_ID, session.dmp_session_id, [ten_mark.id],
                             client, runs_dir=runs)

    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    _panel_namespace(fake, client)["_render_dmp_panel"](_result(runs), 0)

    assert len(fake.warnings) == 1, fake.warnings
    assert "260 words" in fake.warnings[0] and "150-word limit" in fake.warnings[0]
    # Shown in full (as the sheet prints it, without the section markers).
    assert any(m.split().count("word") == 260 for m in fake.markdown_calls)


def test_an_answer_within_its_limit_is_not_flagged(runs: Path):
    client = Client()
    session = _existing_set(runs, client)
    service.generate_answers(NOTE_ID, session.dmp_session_id,
                             [session.questions[0].id], client, runs_dir=runs)
    fake = FakeStreamlit()
    fake.session_state["dmp_open_session_0"] = session.dmp_session_id
    _panel_namespace(fake, client)["_render_dmp_panel"](_result(runs), 0)
    assert fake.warnings == []
