"""The page, actually rendered.

Every other test of app.py reads it as a string: `assert "max_value=cap" in
panel`. That catches a deleted line and nothing else — a panel that raises on
the first render, a button wired to the wrong key, a widget that never appears
because the branch above it returned early, all pass a substring check.

These tests run the page through Streamlit's own AppTest, so the assertions are
about what a user is actually shown.

The one that matters most is the page-load cost. Streamlit executes every tab
body even while another tab is selected, so the DPP workspace must keep its
55 MB, 26,000-question workbook behind its explicit builder gate. Otherwise a
person opening Notes generation pays ~39 seconds for a workflow they did not
open.
"""
import ast
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

HARNESS = str(Path(__file__).resolve().parent / "app_harness.py")
APP_SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(
    encoding="utf-8")

ROOT_WORKFLOW_TABS = (
    "Notes generation",
    "Daily Mains Practice (DMP)",
    "Daily Prelims Practice (DPP)",
)

#: Generous: this asserts "the page did not do 39 seconds of hidden work", not
#: a performance target. A cold import of pandas/reportlab is inside it.
PAGE_LOAD_BUDGET_SECONDS = 25.0


@pytest.fixture(scope="module")
def page():
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    return at


def _labels(widgets):
    return [w.label for w in widgets]


def _tabs_labels(call: ast.Call) -> tuple[str, ...] | None:
    """Return literal `st.tabs` labels, without depending on tab variable names."""
    if not (isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "st"
            and call.func.attr == "tabs"
            and call.args):
        return None
    values = call.args[0]
    if not isinstance(values, (ast.Tuple, ast.List)):
        return None
    labels: list[str] = []
    for item in values.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        labels.append(item.value)
    return tuple(labels)


def _is_module_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """A root-tab call cannot be hidden in a helper function or class."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        current = parents.get(current)
    return True


def _module_workspace_calls(tree: ast.AST, parents: dict[ast.AST, ast.AST],
                            function_name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
        and _is_module_scope(node, parents)
    ]


# --------------------------------------------------------------- it renders

def test_the_page_renders_without_raising(page):
    assert not page.exception, [str(e.value) for e in page.exception]


def test_the_three_inputs_a_run_needs_are_on_the_page(page):
    """A file, a link and a language. Everything else was moved into
    app_defaults precisely so it could not appear here."""
    assert "Lecture video link" in _labels(page.text_input)
    assert "Select Note Language" in _labels(page.radio)
    assert any("Generate UPSC notes" in b for b in _labels(page.button))


def test_the_three_workflows_are_sibling_root_tabs_in_the_requested_order(page):
    """Notes, DMP, and DPP are peer workflows, not panels inside a note card.

    The source check verifies sibling/root topology; AppTest below verifies the
    labels reach an actual visitor. Looking only at `page.get("tab")` cannot
    prove this because DPP may contain its own nested source tabs.
    """
    tree = ast.parse(APP_SOURCE)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    root_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _tabs_labels(node) == ROOT_WORKFLOW_TABS
        and _is_module_scope(node, parents)
    ]
    assert root_calls, "the three root workflow tabs were not declared together"
    root_call = root_calls[0]

    dmp_calls = _module_workspace_calls(tree, parents, "_render_dmp_workspace")
    dpp_calls = _module_workspace_calls(tree, parents, "_render_dpp_workspace")
    assert dmp_calls and dpp_calls, "each root practice tab must open its own workspace"
    assert root_call.lineno < dmp_calls[0].lineno < dpp_calls[0].lineno

    # A flat listing interleaves each workspace's own nested tabs, so the
    # three root labels are checked for order rather than for being first.
    rendered_tabs = _labels(page.get("tab"))
    positions = [rendered_tabs.index(label) for label in ROOT_WORKFLOW_TABS
                 if label in rendered_tabs]
    assert len(positions) == len(ROOT_WORKFLOW_TABS), rendered_tabs
    assert positions == sorted(positions), rendered_tabs


def test_generate_is_disabled_until_a_link_and_a_language_are_given(page):
    """Both are required and neither has a defensible default, so the button
    must not be pressable — a run that starts without them wastes a full
    transcription before it can fail."""
    generate = [b for b in page.button if b.label == "Generate UPSC notes"]
    assert generate, _labels(page.button)
    assert generate[0].disabled


def test_choosing_only_a_language_does_not_enable_generate():
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    language = [r for r in at.radio if r.label == "Select Note Language"][0]
    language.set_value("English").run()
    generate = [b for b in at.button if b.label == "Generate UPSC notes"][0]
    assert generate.disabled, "a language alone is not a runnable job"
    assert not at.exception


def test_something_that_is_not_a_link_at_all_is_rejected_while_typing():
    """Detection is pure and instant, so the user learns the input is wrong
    before pressing Generate rather than after a wait."""
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    link = [t for t in at.text_input if t.label == "Lecture video link"][0]
    link.set_value("just some words").run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "not a supported" in captions.lower(), captions


def test_an_unrecognised_host_is_not_announced_as_a_confirmed_video():
    """`direct_video` is the catch-all branch, not a recognition: any host that
    is not YouTube or Drive lands there, article and typo alike. Saying
    "Detected: Direct video URL" told the user the link had been checked, and
    they found out otherwise only after the download failed minutes later."""
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    link = [t for t in at.text_input if t.label == "Lecture video link"][0]
    link.set_value("https://example.com/an-article").run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption).lower()
    assert "tried as a" in captions, captions
    assert "detected: **direct video url**" not in captions, (
        "the catch-all branch must not claim the link was recognised")


def test_a_youtube_link_still_says_it_was_recognised():
    """The honesty fix above must not cost the two cases we really do know."""
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    link = [t for t in at.text_input if t.label == "Lecture video link"][0]
    link.set_value("https://www.youtube.com/watch?v=dQw4w9WgXcQ").run()
    captions = " ".join(c.value for c in at.caption)
    assert "Detected: **YouTube**" in captions, captions

    link.set_value("https://drive.google.com/file/d/abc123/view").run()
    captions = " ".join(c.value for c in at.caption)
    assert "Detected: **Google Drive**" in captions, captions


def test_a_supported_link_and_a_language_together_enable_generate():
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ").run()
    [r for r in at.radio if r.label == "Select Note Language"][0].set_value(
        "English").run()
    generate = [b for b in at.button if b.label == "Generate UPSC notes"][0]
    assert not generate.disabled
    assert not at.exception


@pytest.mark.parametrize("enabled", [True, False])
def test_transparent_diagram_option_reaches_notes_generation(monkeypatch, enabled):
    from src import pipeline

    calls = []

    def capture_run(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("Stopped after capturing generation options")

    monkeypatch.setattr(pipeline, "run_pipeline", capture_run)
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    option = at.checkbox(key="notes_transparent_diagrams")
    assert option.value is True
    if not enabled:
        option.set_value(False).run()
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ").run()
    [r for r in at.radio if r.label == "Select Note Language"][0].set_value(
        "English").run()
    [b for b in at.button if b.label == "Generate UPSC notes"][0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(calls) == 1
    assert calls[0]["ai_redraw_diagrams"] is enabled


def test_a_drive_refresh_failure_is_reported_as_a_job_error_not_a_page_crash(monkeypatch):
    from src import drive_auth

    def fail_refresh(*args, **kwargs):
        raise RuntimeError("Drive refresh unavailable")

    monkeypatch.setattr(drive_auth, "access_token", fail_refresh)
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.secrets["auth"] = {"google": {
        "client_id": "test.apps.googleusercontent.com", "client_secret": "test-secret",
    }}
    at.run()
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(
        "https://drive.google.com/file/d/test-lecture-file/view").run()
    [r for r in at.radio if r.label == "Select Note Language"][0].set_value(
        "English").run()
    [b for b in at.button if b.label == "Generate UPSC notes"][0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    results = at.session_state.filtered_state["results"]
    assert len(results) == 1
    assert results[0]["error"]
    assert "Drive refresh unavailable" in results[0]["error_reason"]


def test_existing_drive_text_reaches_generation_without_refreshing_drive_access(monkeypatch):
    from src import drive_auth, pipeline

    calls = []

    def capture_run(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("Stopped after capturing transcript")

    def unexpected_refresh(*args, **kwargs):
        pytest.fail("Pasted text must not depend on Google Drive token renewal")

    monkeypatch.setattr(pipeline, "run_pipeline", capture_run)
    monkeypatch.setattr(drive_auth, "access_token", unexpected_refresh)
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    link = "https://drive.google.com/file/d/test-lecture-file/view"
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(link).run()
    transcript = "Indian polity lecture about fundamental rights and judicial review. " * 10
    [t for t in at.text_area if t.label == "Existing Drive transcript"][0].set_value(transcript).run()
    [r for r in at.radio if r.label == "Select Note Language"][0].set_value("English").run()
    [b for b in at.button if b.label == "Generate UPSC notes"][0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(calls) == 1
    assert calls[0]["transcript_text_override"] == transcript.strip()
    assert calls[0]["video_url"] == link
    assert calls[0]["drive_token"] == ""

    # Text pasted for one recording must not silently carry into another class.
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(
        "https://drive.google.com/file/d/another-lecture-file/view").run()
    assert [t for t in at.text_area if t.label == "Existing Drive transcript"][0].value == ""


def test_short_pasted_drive_text_blocks_generation():
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    [t for t in at.text_input if t.label == "Lecture video link"][0].set_value(
        "https://drive.google.com/file/d/test-lecture-file/view").run()
    [r for r in at.radio if r.label == "Select Note Language"][0].set_value("English").run()
    [t for t in at.text_area if t.label == "Existing Drive transcript"][0].set_value("too short").run()
    assert [b for b in at.button if b.label == "Generate UPSC notes"][0].disabled
    assert not at.exception


# -------------------------------------------- the page-load cost of workspaces

def test_the_page_does_not_parse_the_question_bank_on_load():
    """The regression test for a 39-second frozen page.

    `st.expander` runs its body whether or not it is open, so the DPP library
    panel used to call load_masters() on every page load — including for users
    who never opened it, and before the notes downloads were even drawn.
    """
    from src import dpp_builder

    dpp_builder._load_questions.cache_clear()
    dpp_builder._load_taxonomy.cache_clear()

    at = AppTest.from_file(HARNESS, default_timeout=180)
    started = time.time()
    at.run()
    elapsed = time.time() - started

    assert not at.exception
    assert dpp_builder._load_questions.cache_info().currsize == 0, (
        "the 55 MB question bank was parsed during a plain page load")
    assert elapsed < PAGE_LOAD_BUDGET_SECONDS, (
        f"the page took {elapsed:.1f}s to render; something heavy is running "
        f"inside an unopened workspace")


@pytest.mark.skipif(
    not __import__("src.dpp_builder", fromlist=["x"]).masters_ready(),
    reason="the QBG workbooks are not on this machine")
def test_the_practice_builder_is_offered_but_not_started(page):
    """The gate itself: the panel says what it will cost before spending it."""
    assert any("Open the practice builder" in b for b in _labels(page.button)), (
        _labels(page.button))
    captions = " ".join(c.value for c in page.caption)
    assert "55 MB" in captions or "40 seconds" in captions, (
        "a 40-second wait must be announced, not discovered")


# -------------------------------------------------------------- workspaces

def test_the_dmp_workspace_renders_when_notes_exist(page):
    """The DMP workspace draws from runs already on disk. It must render on a
    fresh page load, before any run — that is the whole point of them.

    Asserted through the PICKER, not through "Generate Practice Questions".
    That button is only shown for a lecture with no practice set yet, and the
    newest lecture changes while this suite runs — other tests generate notes,
    and one seeds a practice set — so keying on it made this test depend on
    which run happened to be newest a moment earlier.
    """
    from src.dmp import service as dmp_service

    if not dmp_service.list_notes():
        pytest.skip("no earlier lectures on this machine")
    pickers = page.session_state.filtered_state
    assert pickers.get("dmp_library_pick"), (
        "the 'practice questions from an earlier lecture' picker did not render")
    assert not page.exception, [str(e.value) for e in page.exception]


def test_dpp_upload_and_lecture_upload_are_both_visible_on_the_page(page):
    """DMP and DPP each have one deliberate no-pipeline upload route; the
    lecture workflow retains its separate optional slide/PDF uploader."""
    uploaders = _labels(page.get("file_uploader"))
    # Root workflows are declared Notes, DMP, DPP, so the lecture uploader is
    # registered before each practice workspace's uploaded-notes route.
    assert uploaders == [
        "Upload lecture file(s)",
        "Upload notes for DMP",
        "Upload notes for DPP",
    ], uploaders


# -------------------------------------------------------- the theme switch

def test_the_theme_can_be_switched_without_breaking_the_page():
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.run()
    appearance = [r for r in at.radio if r.label == "Appearance"]
    assert appearance, _labels(at.radio)
    appearance[0].set_value("Light").run()
    assert not at.exception
    assert at.session_state["ui_theme"] == "Light"


# ------------------------------------------------ the harness cleans up after itself

def test_booting_the_app_does_not_leave_the_authorization_gate_disabled(page):
    """The harness has to stand in for a whitelisted user to reach the page at
    all. AppTest runs it in THIS process, so those replacements are permanent
    unless something puts them back — and the first version of this file left
    them in place, which silently turned three fail-closed tests in
    test_auth_session.py into tests that asserted nothing.

    `page` is requested so the harness has definitely run before this asserts.
    """
    import pw_access
    from src import proxy_session

    for module, attr in (
        (pw_access, "check_allowed"),
        (pw_access, "check_allowed_status"),
        (proxy_session, "attach"),
        (proxy_session, "persist"),
    ):
        function = getattr(module, attr)
        assert getattr(function, "__name__", "") == attr, (
            f"{module.__name__}.{attr} is still a test stub "
            f"({function!r}); the fail-closed gate is disabled for every test "
            f"that runs after this file")


# ---------------------------------------------------------- the results card

def _card(**counts):
    """The page, rendered with one finished run already in session state."""
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.session_state["results"] = [{
        "name": "Your files", "run_dir": None, "notes": "", "error": None,
        "docx": None, "pdf": None, "error_xlsx": None, "error_detailed": None,
        "warnings": [], "tracking": None, "render": {},
        "error_status": counts.pop("status", "ok"),
        "error_counts": counts,
    }]
    at.run()
    return at


def _captions(at):
    return " ".join(c.value for c in at.caption)


def test_the_card_reports_how_many_findings_can_be_checked(page):
    """A count of errors is the least useful of the three numbers a run
    produces. How many a reviewer can jump straight to, and how many survived a
    second opinion, were both computed on every run and shown to nobody — they
    sat in JSON files in the run folder."""
    at = _card(total=5, high=2, medium=2, low=1,
               located=4, locatable_against="recording",
               verified=True, verify_checked=5, verify_demoted=1)
    assert not at.exception
    captions = _captions(at)
    assert "4 of 5" in captions and "timestamp in the recording" in captions
    assert "1 of 5" in captions and "downgraded to Low" in captions


def test_a_report_that_survived_verification_says_so():
    at = _card(total=3, high=1, medium=2, low=0,
               located=3, locatable_against="transcript",
               verified=True, verify_checked=3, verify_demoted=0)
    assert not at.exception
    assert "held up on a second" in _captions(at)


def test_nothing_locatable_is_reported_as_a_missing_source():
    """"0 of 5 traced" reads as "we looked and found nothing". When there was
    no transcript and no deck, nothing was looked at, and the difference
    decides whether the reviewer distrusts the findings or the run."""
    at = _card(total=5, high=1, medium=2, low=2,
               located=0, locatable_against="")
    assert not at.exception
    captions = _captions(at)
    assert "missing source, not a failed search" in captions, captions


def test_a_clean_report_does_not_grow_a_verification_footnote():
    """no_issues is a real result. Appending "all 0 findings held up" to it
    would be noise on the one outcome that needs none."""
    at = _card(total=0, high=0, medium=0, low=0, status="no_issues")
    assert not at.exception
    captions = _captions(at)
    assert "No reportable error was found" in captions
    assert "second, independent check" not in captions


def test_a_failed_report_is_never_shown_as_zero_issues():
    """A reviewer who reads "0 issues" stops looking."""
    at = _card(total=0, status="parse_failed")
    assert not at.exception
    warnings = " ".join(w.value for w in at.warning)
    assert "not** a statement" in warnings or "not a statement" in warnings, warnings
    assert not [m for m in at.get("metric") if m.label == "Total issues"], (
        "a failed report must not render a counts row at all")


# ------------------------------------------ what the document is not, said plainly

def _notice_card(**over):
    """The results card for one finished run, with structured facts set."""
    result = {
        "name": "Your files", "run_dir": None, "notes": "", "error": None,
        "docx": None, "pdf": None, "error_xlsx": None, "error_detailed": None,
        "warnings": [], "tracking": None, "render": {}, "error_status": "ok",
        "error_counts": {"total": 0}, "note_length": {}, "visuals": {},
        "pdf_reason": "",
    }
    result.update(over)
    at = AppTest.from_file(HARNESS, default_timeout=180)
    at.session_state["results"] = [result]
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _notices(at):
    return [m.value for m in at.markdown if m.value.startswith("- **")]


def test_a_run_that_could_not_check_its_own_length_says_so():
    """The page limit is enforced by measuring a rendered PDF. With no
    converter there is no measurement and no enforcement — and the full
    warnings list that said so is developer-only, so the user was told
    nothing."""
    at = _notice_card(note_length={"page_limit": 15, "page_limit_enforced": False})
    text = " ".join(_notices(at))
    assert "could not be checked" in text, _notices(at)
    assert "PDF export error" in text, "the notice must direct the user to the cause"
    assert "LibreOffice" not in text


def test_a_document_over_the_limit_says_how_far_over():
    at = _notice_card(note_length={"page_limit": 15, "final_pages": 19,
                                   "page_limit_enforced": True})
    text = " ".join(_notices(at))
    assert "19 pages" in text and "15-page limit" in text, _notices(at)


def test_diagrams_that_never_reached_the_notes_are_reported():
    """13 found, 13 selected, 0 inserted used to look exactly like 13 inserted."""
    at = _notice_card(visuals={"found": 13, "selected": 13, "inserted": 0})
    text = " ".join(_notices(at))
    assert "None of the 13 slide diagram(s)" in text, _notices(at)


def test_a_partial_diagram_placement_reports_the_fraction():
    at = _notice_card(visuals={"found": 13, "selected": 10, "inserted": 6})
    assert "6 of 10 slide diagram(s)" in " ".join(_notices(at)), _notices(at)


def test_a_run_that_delivered_what_it_promised_says_nothing():
    """The notices exist because hiding a real problem is worse than a little
    text. A run with no problem must add no text at all — that is what keeps
    the notice worth reading when it does appear."""
    at = _notice_card(
        note_length={"page_limit": 15, "final_pages": 12, "page_limit_enforced": True},
        visuals={"found": 5, "selected": 5, "inserted": 5})
    assert _notices(at) == []


def test_a_missing_pdf_names_its_cause_in_the_notices():
    at = _notice_card(pdf_reason="LibreOffice/soffice was not found.")
    assert "LibreOffice/soffice was not found." in " ".join(_notices(at))
