"""The DMP panel's contract with the page it sits on.

Read from the source rather than by driving Streamlit, which is how the rest of
this project tests its UI: the assertions are about what the page is allowed to
contain, and that is a property of the code, not of a rendered run.

The panel is the place where an "advanced options" section grows if nobody is
watching. These tests are the thing watching.
"""
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP = Path(__file__).resolve().parents[1] / "app.py"
APP_SOURCE = APP.read_text(encoding="utf-8")


#: The DMP block ends where the next panel begins. It used to run to
#: _render_dcn_render_status, which was true only while DMP was the last thing
#: on the page — once the DPP panel landed between them, every "the panel must
#: not contain X" assertion was really reading two panels.
_DMP_END = "# Daily Prelims Practice (DPP)"


def _panel_source() -> str:
    """The DMP panel and its helpers, exactly as written."""
    start = APP_SOURCE.index("# Daily Mains Practice")
    end = (APP_SOURCE.index(_DMP_END) if _DMP_END in APP_SOURCE
           else APP_SOURCE.index("def _render_dcn_render_status"))
    return APP_SOURCE[start:end]


def _function_source(name: str) -> str:
    """Return one function exactly, without relying on its neighbours' order."""
    import ast

    tree = ast.parse(APP_SOURCE)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name)
    lines = APP_SOURCE.splitlines(keepends=True)
    return "".join(lines[function.lineno - 1:function.end_lineno])


def test_the_standalone_picker_practises_on_notes_that_already_exist():
    """Practice without generating: someone who wants only questions should not
    have to spend a full pipeline run to reach them."""
    panel = _panel_source()
    assert "def _render_dmp_library" in panel
    assert "list_notes()" in panel
    workspace = _function_source("_render_dmp_workspace")
    assert "_render_dmp_library()" in workspace
    # It shares the panel body rather than duplicating it — two copies of a
    # ten-question checkbox grid is two places for the tick logic to drift.
    assert panel.count("def _render_dmp_body") == 1
    library = panel[panel.index("def _render_dmp_library"):]
    assert "_render_dmp_body(" in library
    # And it must never reach for the pipeline.
    assert "run_pipeline" not in library


def test_the_expanded_header_is_readable():
    """An open expander keeps Streamlit's light summary background while the app
    paints the label near-white: measured 1.06:1, which is white on white. Every
    expanded title on the page rendered blank."""
    assert '[data-testid="stExpander"] details[open] > summary' in APP_SOURCE
    css = APP_SOURCE[APP_SOURCE.index('[data-testid="stExpander"] details > summary'):]
    assert "background: var(--surface-muted) !important;" in css[:400]
    assert "color: var(--text) !important;" in css[:400]


def _panel_code() -> str:
    """The panel with its comments removed.

    Needed because a comment explaining that the panel must never call
    run_pipeline contains the string "run_pipeline". Searching the raw source
    for forbidden calls finds the sentence promising not to make them.
    """
    import io
    import tokenize

    source = _panel_source()
    lines = source.splitlines(keepends=True)
    try:
        comments = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source
    # Blank the comments IN PLACE rather than re-joining tokens: a rejoined
    # stream turns `has_notes(note_id)` into `has_notes ( note_id )`, and every
    # assertion about a real call would then have to be written twice.
    for token in comments:
        if token.type != tokenize.COMMENT:
            continue
        row, start, end = token.start[0] - 1, token.start[1], token.end[1]
        lines[row] = lines[row][:start] + " " * (end - start) + lines[row][end:]
    return "".join(lines)


def _panel_visible_text() -> str:
    """Every string literal on the panel, whitespace-normalised.

    This is the approximation of "what a user could be shown". Identifiers are
    excluded on purpose: `GeminiClient` is the class the panel is REQUIRED to
    reuse, and it must not be confused with naming the model on screen.
    """
    import ast

    module = _panel_body_module()
    # Docstrings are strings too, and they explain the code rather than being
    # shown by it. Counting them makes this the raw-source search it exists to
    # replace: the docstring for the client factory names the model.
    docstrings = {id(n.body[0].value)
                  for n in ast.walk(module)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)
                  and isinstance(n.body[0].value.value, str)}
    literals: list[str] = []
    for node in ast.walk(module):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            literals.extend(v.value for v in node.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return " ".join(" ".join(text.split()) for text in literals)


def _panel_body_module():
    """The panel parsed as a module, with the leading comment block dropped."""
    import ast

    source = _panel_source()
    first_def = source.index("def _dmp_state_key")
    return ast.parse(source[first_def:])


def _results_card_source() -> str:
    return APP_SOURCE[APP_SOURCE.index("results = st.session_state.results"):]


# ------------------------------------------------------ its own root workflow

def test_dmp_is_not_embedded_inside_finished_note_cards():
    """Generating/reviewing notes must not also render a second DMP workflow."""
    card = _results_card_source()
    assert "_render_dmp_panel(r, idx)" not in card
    assert "_render_dmp_body(" not in card


def test_the_dmp_workspace_only_hosts_the_standalone_library():
    """The DMP tab is a separate destination and has one source of truth."""
    workspace = _function_source("_render_dmp_workspace")
    assert "_render_dmp_library()" in workspace
    assert "_render_dmp_panel(" not in workspace


# --------------------------------------------------------------- two buttons

def test_there_are_exactly_two_generating_buttons():
    """One to ask, one to answer. Everything else on the panel is a download or
    a piece of navigation."""
    panel = _panel_source()
    keys = re.findall(r'st\.button\(\s*\n?\s*text\("(\w+)"', panel)
    generating = [k for k in keys if k.startswith("generate_")]
    assert generating == ["generate_questions", "generate_answers"], generating


def test_the_answer_button_is_dead_until_something_is_ticked():
    """Spec: no question selected is a gentle hint, not a request. A disabled
    button cannot send one."""
    panel = _panel_source()
    assert "disabled=not unanswered_selected" in panel
    assert 'text("tick_hint", lang)' in panel


def test_a_new_practice_set_is_never_generated_by_a_mis_click():
    """The "New practice set" button clears the selection so the generate button
    reappears — it does not itself spend three model calls."""
    panel = _panel_source()
    new_block = panel[panel.index("if new_clicked:"):]
    assert "generate_questions" not in new_block[:600]
    assert "st.session_state.pop(state_key, None)" in new_block


# ------------------------------------------------ nothing internal is exposed

@pytest.mark.parametrize("banned", [
    "temperature", "max_output_tokens", "top_p", "retry", "fallback_model",
    "quality_level", "strictness",
])
def test_no_model_internals_reach_the_panel(banned):
    """Spec §11. The user picks questions; the app picks everything else."""
    assert banned not in _panel_code().lower()


def test_the_model_is_not_named_on_the_panel():
    """The name is in the code — the panel reuses GeminiClient and must. It is
    what the USER is shown that has to be free of it."""
    assert "gemini" not in _panel_visible_text().lower()
    # The model is still recorded per answer for traceability, just not shown.
    assert "model_used" not in _panel_code()


def test_the_panel_adds_no_second_version_string():
    """The header already shows one. Two version strings on a page is how they
    start disagreeing."""
    code = _panel_code()
    assert "APP_VERSION" not in code
    assert "version_label" not in code


def test_the_panel_can_never_re_enter_the_notes_pipeline():
    """A practice button that regenerates the lecture would be catastrophic and
    is one careless import away."""
    assert "run_pipeline" not in _panel_code()


# ------------------------------------------------------------- failing safely

def test_a_missing_run_folder_is_explained_rather_than_offered():
    assert "has_notes(note_id)" in _panel_code()
    assert 'text("no_notes", lang)' in _panel_source()


def test_every_generate_path_handles_a_dead_service():
    """Two try blocks, each catching the friendly error and the unexpected one.
    A traceback in a Streamlit page is shown to the user, not just to a log."""
    panel = _panel_source()
    assert panel.count("except dmp_service.DmpError as exc:") == 2
    assert panel.count("st.error(str(exc))") == 2
    # And a bare failure still says something useful, in the note's language.
    assert 'text("failed_questions", lang)' in panel
    assert 'text("failed_answers", lang)' in panel


def test_a_signed_out_user_is_told_to_sign_in_before_anything_is_attempted():
    panel = _panel_source()
    assert panel.count("if client is None:") == 2
    assert 'text("sign_in_questions", lang)' in panel
    assert 'text("sign_in_answers", lang)' in panel


def test_answering_keeps_the_download_selection_but_filters_answered_questions():
    """An answered question that keeps its tick is answered again — and paid
    for again — on the next press of the button."""
    panel = _panel_source()
    assert "def _dmp_unanswered_selected_ids" in panel
    assert "_dmp_clear_picks" not in panel
    answer_block = panel[panel.index("if answer_clicked and unanswered_selected:"):]
    assert ("note_id, session.dmp_session_id, unanswered_selected, client,"
            in answer_block)


def test_answers_are_requested_by_id_and_only_for_the_ticked_ones():
    panel = _panel_source()
    assert "note_id, session.dmp_session_id, unanswered_selected, client," in panel
    assert "_dmp_selected_ids" in panel


def test_question_downloads_receive_only_the_current_ticked_questions():
    panel = _panel_source()
    build_block = panel[panel.index("def _render_dmp_downloads"):]
    assert "selected_ids=selected_ids if kind == \"questions\" else None" in build_block
    assert "disabled=(kind == \"questions\" and not selected_ids)" in build_block
    assert 'built.get("selected_ids") != list(selected_ids)' in build_block


def test_the_panel_reloads_from_disk_rather_than_holding_the_session():
    """Holding the object in session state would make the history, the restart
    survival and the browser refresh three separate features. Reloading makes
    them one."""
    panel = _panel_source()
    assert "dmp_service.get_session(" in panel
    assert "note_id, st.session_state[state_key], runs_dir)" in panel
    # What IS held is an id.
    assert 'return f"dmp_open_session_{idx}"' in panel


def test_the_download_uses_the_shared_helper_like_every_other_download():
    panel = _panel_source()
    assert "_download_from_path(" in panel
    assert "st.download_button(" not in panel, (
        "downloads go through the shared helper, which handles the missing-file case")


def test_a_failed_sheet_build_does_not_take_the_page_down():
    panel = _panel_source()
    build_block = panel[panel.index("def _render_dmp_downloads"):]
    assert "except Exception as exc:" in build_block
    assert 'text("sheet_failed", lang)' in build_block


# ----------------------------------------------------------------- traceability

def test_practice_calls_are_tagged_separately_in_the_usage_ledger():
    """Same client, same proxy, different task id — so the bill can be read as
    "notes" and "practice" rather than as one number."""
    panel = _panel_source()
    assert 'pw_access.new_task_id("upsc-dmp")' in panel
    assert "GeminiClient(" in panel


def test_the_panel_builds_no_second_client_class():
    """Spec §11: no second Gemini client. It imports the one that exists."""
    panel = _panel_source()
    assert "from src.ai_client import GeminiClient" in panel
    assert "\nclass " not in _panel_source()
    assert "requests.post" not in _panel_code()


# ------------------------------------------------- what the sheets print

def test_the_sheets_print_the_panel_boxes_not_a_guess():
    """Issue 8: the title read "Daily Mains Practice DMP" with no subject or
    number. The panel now asks, prefilled from the lecture, and hands all
    three to the export."""
    downloads = _function_source("_render_dmp_downloads")
    assert "_render_dmp_sheet_labels(session, idx, lang, runs_dir)" in downloads
    assert 'subject=printed["subject"], topic=printed["topic"],' in downloads
    assert 'dmp_no=printed["dmp_no"]' in downloads
    labels = _function_source("_render_dmp_sheet_labels")
    assert "dmp_export.sheet_defaults(session, runs_dir)" in labels
    assert "st.text_input(" in labels


def test_a_long_answer_is_flagged_where_it_is_shown():
    """Issue 10: the length check is shown beside the answer, in the page's
    warning style, and nothing in the panel shortens the answer."""
    questions = _function_source("_render_dmp_questions")
    assert "dmp_service.answer_overrun(" in questions
    assert 'st.warning(text("over_limit", lang' in questions
