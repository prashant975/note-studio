"""The Daily Prelims Practice workspace's contract with the page.

Source-level, like the DMP workspace tests. The workspace is where the four
steps are wired together, and it is where a stray upload widget, a second
Gemini client, or a quietly dropped exclusion set would appear if nobody
looked.
"""
import ast
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def _panel() -> str:
    start = APP_SOURCE.index("# Daily Prelims Practice (DPP)")
    return APP_SOURCE[start:APP_SOURCE.index("def _render_dcn_render_status")]


def _function_source(name: str) -> str:
    """Return one function exactly, without assuming nearby helper order."""
    tree = ast.parse(APP_SOURCE)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name)
    lines = APP_SOURCE.splitlines(keepends=True)
    return "".join(lines[function.lineno - 1:function.end_lineno])


def test_dpp_is_available_in_its_own_workspace_not_a_note_result_card():
    card = APP_SOURCE[APP_SOURCE.index("results = st.session_state.results"):]
    assert "_render_dpp_panel(r, idx)" not in card
    assert "_render_dpp_body(" not in card

    workspace = _function_source("_render_dpp_workspace")
    assert "_render_dpp_uploaded_notes()" in workspace
    assert "_render_dpp_library()" in workspace
    assert "_render_dpp_panel(" not in workspace


def test_saved_lecture_panel_never_asks_for_a_pdf_upload():
    """The saved-lecture route reads the note already on disk. Upload support
    lives in its distinct temporary DPP workspace, never in this builder."""
    saved_builder = _function_source("_render_dpp_body")
    assert "file_uploader" not in saved_builder
    assert "extract_uploaded_document" not in saved_builder


def test_dpp_upload_workspace_uses_extraction_not_note_generation():
    upload = _function_source("_render_dpp_uploaded_notes")
    assert "Upload notes for DPP" in upload
    assert "extract_uploaded_document" in upload
    assert 'type=["pdf", "txt", "md"]' in upload
    assert "run_pipeline" not in upload


def test_the_panel_generates_no_questions_of_its_own():
    """DPP draws from the bank; DMP writes new questions. Confusing the two
    would mean paying a model for questions that already exist."""
    panel = _panel()
    assert "GeminiClient" not in panel
    assert "generate_questions" not in panel
    assert "run_pipeline" not in panel


def test_marathi_is_offered_a_choice_rather_than_resolved_silently():
    """Marathi and Hindi share a script, so a silent fallback would be
    invisible — and it is the one thing the question bank refuses to do."""
    panel = _panel()
    assert "Language.UNKNOWN" in panel
    assert "st.radio" in panel


def test_the_counts_cannot_exceed_what_the_bank_holds():
    """Asking for 20 when 8 exist raises InsufficientPoolError. Capping the
    input makes that error unreachable rather than merely handled."""
    panel = _panel()
    assert "max_value=cap" in panel
    assert "available_counts" in panel


def test_replace_and_remove_are_wired_to_the_packages_own_rules():
    panel = _panel()
    assert "replace_question(" in panel and "remove_question(" in panel
    # Both return updated exclusion sets. Dropping them would let a question
    # the user just replaced come straight back on the next build.
    assert panel.count("outcome.seen_ids") >= 2
    assert panel.count("outcome.seen_fingerprints") >= 2


def test_preview_uses_the_questions_selected_language():
    """Hindi previews must show Hindi, just like the exported PDF does."""
    panel = _panel()
    assert "question.content_for(language)" in panel
    assert 'getattr(question, "localized"' not in panel


def test_edits_do_not_leave_old_downloads_available():
    panel = _panel()
    assert "def _dpp_invalidate_exports" in panel
    # Topic/language changes discard the built set, while Replace/Remove only
    # discard files. In both cases, no old download can describe new content.
    assert "on_change=_dpp_reset_selection" in panel
    invalidations = re.findall(r"_dpp_invalidate_exports\(\w+\)", panel)
    assert len(invalidations) >= 3, invalidations


def test_dpp_number_is_editable_inside_the_practice_builder():
    """Library notes pre-date metadata fields, so their number belongs here."""
    panel = _panel()
    assert "DPP No. for this practice set" in panel
    assert re.search(r'number_key = f"dpp_export_no_\{\w+\}"', panel)
    assert "_dpp_saved_number(note_id)" in panel
    assert "on_change=_dpp_invalidate_exports" in panel
    assert "dpp_no=dpp_no" in panel


def test_a_new_set_keeps_what_was_already_shown():
    """The builder's rule: a question shown once in a session never reappears.
    Clearing the exclusions on "start a new set" would break it silently."""
    panel = _panel()
    reset = panel[panel.index("def _dpp_reset_selection"):]
    assert "seen_ids" not in reset[:400], "a new set must not clear exclusions"


def test_a_failed_pdf_build_does_not_take_the_page_down():
    panel = _panel()
    assert "Those PDFs could not be built." in panel


def test_a_missing_workbook_is_explained_rather_than_crashed():
    panel = _panel()
    assert "masters_ready()" in panel
    assert "QBG_TAGS_PATH" in panel


def test_the_library_hides_itself_when_the_workbooks_are_absent():
    """Offering the picker on a machine with no question bank is offering a
    button that can only fail."""
    panel = _panel()
    library = panel[panel.index("def _render_dpp_library"):]
    assert "masters_ready()" in library
    assert "return" in library


def test_library_opens_the_selected_note_even_when_it_is_a_current_result():
    """The result card no longer contains DPP, so there is no duplicate builder.

    A lecture just generated in Notes must therefore be immediately usable in
    the standalone DPP tab rather than being sent to a panel that no longer
    exists.
    """
    panel = _panel()
    library = panel[panel.index("def _render_dpp_library"):]
    assert "current_result_note_ids" not in library
    assert "Open its DPP panel there" not in library
    assert "_render_dpp_body(chosen[\"note_id\"]" in library
