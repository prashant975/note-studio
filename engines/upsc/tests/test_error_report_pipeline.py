"""The Error Report as the pipeline and the app actually deliver it.

Two things are checked here that the unit tests cannot see: that a real mock run
writes the sheet and records it in run_log.json, and that the Streamlit results
card offers three downloads and no more. The second is asserted against app.py's
source because importing app.py outside a Streamlit runtime raises on the theme
lookup — but the download contract is exactly the thing most likely to regress
when someone adds "just one more" report.
"""
from pathlib import Path
import json
import re
import shutil
import sys

from openpyxl import load_workbook
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pipeline
from src.config import DEVELOPER_ONLY_REPORTS
from src.error_report_writer import COLUMNS
from src.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PIPELINE_SOURCE = (ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")

#: A lecture PDF from a previous run, if this machine has one. Building a PDF
#: here would test the fixture, not the pipeline.
SAMPLE = next((p for p in sorted((ROOT / "runs").glob("*/input/*.pdf"), reverse=True)), None)
needs_sample = pytest.mark.skipif(SAMPLE is None, reason="no sample lecture on this machine")


#: DOCX->PDF drives Word over COM on this machine, which is slow and fails with
#: RPC_S_CALL_FAILED when two runs overlap. The PDF is not what these tests are
#: about, so it is stubbed out along with the render check that also converts.
NO_CONVERSION = {"render_verify": False}


def _run(lecture: Path, **kwargs):
    language = kwargs.pop("language", "en")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "export_docx_to_pdf", lambda docx, out_dir: (None, ""))
        return run_pipeline(
            lecture, subject="environment", language=language, mode="complete",
            google_token="", allow_mock=True, workflow="upsc", area="environment",
            upsc_output_format="dcn", quality_level="off",
            dcn_options=NO_CONVERSION,
            # These fixtures exercise the error-report machinery on slides
            # alone; a lecture video is deliberately not part of them. This is
            # the same slides-only escape hatch the CLI's --no-transcript uses.
            require_transcript=False,
            **kwargs,
        )


@pytest.fixture(scope="module")
def mock_run(tmp_path_factory):
    """One mock DCN run, reused by every test below."""
    if SAMPLE is None:
        pytest.skip("no sample lecture on this machine")
    lecture = tmp_path_factory.mktemp("run") / "lecture.pdf"
    shutil.copy(SAMPLE, lecture)
    return _run(lecture, generate_error_report=True, error_report_strictness="standard")


# ------------------------------------------------------------- the artefacts

@needs_sample
def test_a_run_produces_notes_and_one_error_report(mock_run):
    assert mock_run.docx_path and mock_run.docx_path.exists()
    assert mock_run.error_report_xlsx_path and mock_run.error_report_xlsx_path.exists()
    assert mock_run.error_report_xlsx_path.suffix == ".xlsx"
    assert mock_run.error_report_xlsx_path.parent.name == "output"


@needs_sample
def test_the_sheet_has_the_agreed_columns(mock_run):
    wb = load_workbook(mock_run.error_report_xlsx_path)
    ws = wb[wb.sheetnames[0]]
    assert [ws.cell(1, c).value for c in range(1, 9)] == list(COLUMNS)
    assert ws.cell(2, 1).value == 1


@needs_sample
def test_the_raw_json_is_kept_for_developers(mock_run):
    raw = mock_run.run_dir / "error_report_raw.json"
    assert raw.exists()
    assert isinstance(json.loads(raw.read_text(encoding="utf-8")), list)


@needs_sample
def test_run_log_records_the_path_and_the_counts(mock_run):
    log = json.loads((mock_run.run_dir / "run_log.json").read_text(encoding="utf-8"))
    assert log["generate_error_report"] is True
    assert log["error_report_status"] == "ok"
    assert log["error_report_xlsx_path"].endswith(".xlsx")
    counts = log["error_report_counts"]
    # The severity counts are the contract: four keys that add up. The summary
    # also carries how many findings were traced to a place in the lecture and
    # how many survived the verification pass — both are shown on the results
    # card, so they belong in the log next to the counts they qualify.
    assert {"total", "high", "medium", "low"} <= set(counts)
    assert counts["total"] == counts["high"] + counts["medium"] + counts["low"]
    assert counts == mock_run.error_report_summary


@needs_sample
def test_the_counts_say_how_far_each_finding_can_be_trusted(mock_run):
    """A count of errors is the least useful of the three numbers a run makes.

    `located` is how many a reviewer can jump straight to; `verified` is
    whether the second-opinion pass ran at all. Both were computed on every run
    and reported to nobody — and `verified` was, for weeks, always False
    because the call raised NameError on `date` and a broad except turned it
    into a warning.
    """
    counts = mock_run.error_report_summary
    assert set(counts) >= {"located", "locatable_against",
                           "verified", "verify_checked", "verify_demoted",
                           "verify_reason"}
    assert isinstance(counts["located"], int)
    assert 0 <= counts["located"] <= counts["total"]
    assert counts["verify_demoted"] <= counts["verify_checked"]
    # Mock runs have no client, so the pass declines — and must say why rather
    # than reporting a Python defect.
    assert counts["verified"] is False
    assert counts["verify_reason"] == "no client", counts["verify_reason"]


@needs_sample
def test_the_json_array_never_reaches_the_notes(mock_run):
    """The single failure that would be visible to a student: the report
    typeset into their document as a bullet."""
    notes = (mock_run.run_dir / "notes_raw.txt").read_text(encoding="utf-8")
    assert "ERROR_REPORT" not in notes
    assert "original_statement" not in notes
    assert "correct_information_correction" not in notes


@needs_sample
def test_switching_the_report_off_writes_no_sheet(tmp_path):
    lecture = tmp_path / "lecture.pdf"
    shutil.copy(SAMPLE, lecture)
    result = _run(lecture, generate_error_report=False)
    assert result.error_report_xlsx_path is None
    assert result.error_report_status == "disabled"
    assert not list((result.run_dir / "output").glob("*.xlsx"))
    # The notes are still produced — the report is optional, they are not.
    assert result.docx_path.exists()


# ------------------------------------------------------------ output language

@needs_sample
def test_choosing_english_overrules_a_hindi_class(tmp_path):
    """The reported failure, end to end: the class is taught in Hindi, the user
    asked for English, and the run must honour the user."""
    from src.language_detect import detect_notes_language

    lecture = tmp_path / "lecture.pdf"
    shutil.copy(SAMPLE, lecture)
    hindi = ("तो देखिए बच्चों, probity का मतलब होता है ईमानदारी। "
             "RTI Act 2005 के तहत नागरिक सूचना माँग सकते हैं। ") * 60
    # Auto really does read this class as Hindi - that is not the bug.
    assert detect_notes_language("Probity RTI Act Citizens Charter", hindi)[0] == "hi"

    result = _run(lecture, language="English", transcript_text_override=hindi,
                  generate_error_report=False)
    log = json.loads((result.run_dir / "run_log.json").read_text(encoding="utf-8"))
    assert log["detected_language"] == "en", "the explicit choice was overruled"
    prompt = (result.run_dir / "prompt_upsc.txt").read_text(encoding="utf-8")
    assert "in ENGLISH" in prompt
    assert "LANGUAGE [READ THIS FIRST]" in prompt
    # And nothing re-detected it afterwards: no "auto-detected" note in the log.
    assert not [w for w in result.warnings if "Language auto-detected" in w]


@needs_sample
def test_auto_still_follows_the_class_when_it_is_asked_to(tmp_path):
    """Auto is still on offer, and still does its job — the fix is that it is no
    longer what happens to you without choosing it."""
    lecture = tmp_path / "lecture.pdf"
    shutil.copy(SAMPLE, lecture)
    hindi = ("तो देखिए बच्चों, probity का मतलब होता है ईमानदारी। "
             "RTI Act 2005 के तहत नागरिक सूचना माँग सकते हैं। ") * 60
    result = _run(lecture, language="auto", transcript_text_override=hindi,
                  generate_error_report=False)
    log = json.loads((result.run_dir / "run_log.json").read_text(encoding="utf-8"))
    assert log["detected_language"] == "hi"
    assert [w for w in result.warnings if "Language auto-detected" in w]


# ------------------------------------------------- what the app actually shows

def _results_card_source() -> str:
    """The block that renders one finished file's card."""
    start = APP_SOURCE.index("results = st.session_state.results")
    return APP_SOURCE[start:]


def test_the_results_card_offers_the_outputs_and_nothing_else():
    """Notes, PDF, error report — plus the located version of that same report.

    The fourth entry is not a fourth kind of thing: it is the error report
    written for a human to check against the lecture, with each finding's
    timestamp / slide / page. It appears only when it exists, so a run without
    one still shows three.
    """
    card = _results_card_source()
    labels = re.findall(r'_download_from_path\(\s*\n?\s*"([^"]+)"', card)
    assert labels == [
        "Download Notes DOCX",
        "Download Notes PDF",
        "Download Error Report XLSX",
        "Download Error Report (with locations)",
    ], labels
    # Still no pipeline internals on the page.
    for banned in ("Download transcript", "Download QC", "Download run log"):
        assert banned not in card


def test_no_report_is_served_outside_the_shared_helper():
    """Every download reads a path stored when the run finished, so a click
    re-serves a file and can never re-enter run_pipeline."""
    direct = re.findall(r"st\.download_button\(", APP_SOURCE)
    assert len(direct) == 1, "downloads must all go through _download_from_path"
    helper_start = APP_SOURCE.index("def _download_from_path")
    helper_end = APP_SOURCE.index("\ndef ", helper_start + 1)
    assert "st.download_button(" in APP_SOURCE[helper_start:helper_end]


@pytest.mark.parametrize("key", DEVELOPER_ONLY_REPORTS)
def test_the_old_reports_are_only_reachable_in_developer_mode(key):
    card = _results_card_source()
    dev_start = APP_SOURCE.index("_DEVELOPER_DOWNLOADS = (")
    dev_end = APP_SOURCE.index(")", APP_SOURCE.index('"Full Run ZIP"', dev_start))
    assert f'"{key}"' in APP_SOURCE[dev_start:dev_end], f"{key} is not listed as developer-only"
    # ...and the card itself never reaches for it directly.
    assert f'r.get("{key}")' not in card
    assert f'r["{key}"]' not in card


def test_developer_downloads_are_gated_on_the_flag():
    """The gate is at the call site, which resolves the env var *and* ?dev=1.
    Gating inside the function on the env var alone would kill the URL switch."""
    calls = re.findall(r"if DEV_MODE:\n(?:.|\n)*?_render_developer_downloads", APP_SOURCE)
    assert calls, "developer downloads must be rendered under an `if DEV_MODE:` guard"
    assert "_render_developer_downloads(r, idx)" in APP_SOURCE


def test_the_summary_shows_counts_not_the_report_body():
    fn_start = APP_SOURCE.index("def _render_error_report_summary")
    fn_end = APP_SOURCE.index("\ndef ", fn_start + 1)
    body = APP_SOURCE[fn_start:fn_end]
    for label in ("Total issues", "High", "Medium", "Low"):
        assert f'"{label}"' in body
    # A failed or missing report must not be presented as "0 issues".
    assert "not** a statement" in body or "not a statement" in body


def test_the_error_report_settings_reach_the_pipeline():
    """They are no longer controls on the page — they are defaults resolved in
    src/app_defaults.py — but they must still be passed through."""
    assert "generate_error_report=GENERATE_ERROR_REPORT" in APP_SOURCE
    assert "error_report_strictness=ERROR_REPORT_STRICTNESS_VALUE" in APP_SOURCE
    assert "error_report_template_path=ERROR_REPORT_TEMPLATE_PATH" in APP_SOURCE
    assert "GENERATE_ERROR_REPORT = defaults.GENERATE_ERROR_REPORT" in APP_SOURCE


def _normal_page_source() -> str:
    """app.py's page, minus the Developer Options block and minus comments.

    Comments explain the code; they are not on the page. Stripping them matters
    because several of them name the very controls being banned, in explaining
    why they are hidden.
    """
    page_start = APP_SOURCE.index("DEV_MODE = developer_mode_enabled")
    page = APP_SOURCE[page_start:]
    dev_start = page.index('with st.expander("Developer Options"')
    dev_end = page.index("run_button = st.button", dev_start)
    normal = page[:dev_start] + page[dev_end:]
    return "\n".join(
        line for line in normal.split("\n") if not line.lstrip().startswith("#"))


def test_the_normal_page_has_no_settings_controls():
    """The whole point of the simplified UI: a normal user is not asked to have
    an opinion about machinery they cannot see the effects of.

    Notes language is the deliberate exception — see the test below.
    """
    normal = _normal_page_source()
    for banned in ("Quality Check", "Note depth", "UPSC area",
                   "Model routing", "Strictness", "st.selectbox"):
        assert banned not in normal, f"{banned!r} is visible on the normal page"


def test_notes_language_is_the_only_control_on_the_page():
    """It earns its place: unlike depth or strictness, there is no answer that
    is right for every run, the user cannot see the outcome until the notes
    exist, and being wrong wastes the entire run."""
    normal = _normal_page_source()
    radios = re.findall(r'st\.radio\(\s*\n?\s*"([^"]+)"', normal)
    assert radios == ["Select Note Language"], radios
    # .get, not [] — the placeholder row is a real option and maps to no
    # language, which is what makes "nothing chosen" a state the page can be in.
    assert 'LANGUAGE = LANGUAGE_CHOICES.get(language_label, "")' in normal


def test_the_language_choice_is_not_overridden_by_developer_options():
    """The Developer Options block runs after the page controls, so a second
    language widget there would quietly win."""
    dev_start = APP_SOURCE.index('with st.expander("Developer Options"')
    dev_end = APP_SOURCE.index("run_button = st.button", dev_start)
    dev = "\n".join(
        line for line in APP_SOURCE[dev_start:dev_end].split("\n")
        if not line.lstrip().startswith("#"))
    # Reassignment is the failure, not mention: the readiness gate just below
    # legitimately READS the choice to decide whether the run may start.
    assert "LANGUAGE =" not in dev, "Developer Options reassigns LANGUAGE"
    assert "LANGUAGE_CHOICES" not in dev, "Developer Options offers a second picker"


def test_the_language_must_be_chosen_and_has_no_default():
    """There is no default note language, and no way to start a run without one.

    Every candidate default is wrong for most runs, and the mistake is invisible
    until the notes already exist — so the choice is required rather than
    guessed. "Auto (match the class)" is gone for a related reason: the output
    language is now independent of the language the class was taught in, which
    is what lets a Hindi class produce Marathi notes.
    """
    assert 'LANGUAGE = ""' in APP_SOURCE, "a default language crept back in"
    assert "LANGUAGE_CHOSEN = False" in APP_SOURCE
    # The option DEFINITION, not the words: the comment above LANGUAGE_CHOICES
    # explains why the option was removed and would match a looser check.
    assert '"Auto (match the class)": "auto"' not in APP_SOURCE
    assert '"auto"' not in APP_SOURCE.split("LANGUAGE_CHOICES = {", 1)[1].split("}", 1)[0]
    assert "defaults.DEFAULT_LANGUAGE" not in APP_SOURCE

    # All three languages are offered, and the picker opens on none of them.
    for label in ('"English": "English"', '"हिंदी / Hindi": "Hindi"',
                  '"मराठी / Marathi": "Marathi"'):
        assert label in APP_SOURCE, label
    assert 'LANGUAGE_PROMPT = "— Select —"' in APP_SOURCE
    assert "[LANGUAGE_PROMPT, *LANGUAGE_CHOICES]" in APP_SOURCE
    assert "index=0" in APP_SOURCE

    # Blocked in two places: the button is disabled, and the run branch refuses
    # even if a stale session state gets past it.
    assert "READY = bool(VIDEO_SOURCE) and LANGUAGE_CHOSEN" in APP_SOURCE
    assert "if run_button and not LANGUAGE_CHOSEN:" in APP_SOURCE
    assert APP_SOURCE.count(
        "Please select the note language before generating notes.") == 2


def test_subject_and_practice_identity_fields_reach_the_pipeline():
    for label in ("Subject name", "Lecture title", "DCN No.", "DMP No.", "DPP No."):
        assert label in APP_SOURCE, f"{label} is missing from the notes form"
    assert "Lecture title (optional)" not in APP_SOURCE
    for wiring in ("subject=SUBJECT", "lecture_title=LECTURE_TITLE", "dcn_no=DCN_NO",
                   "dmp_no=DMP_NO", "dpp_no=DPP_NO",
                   "source_options=SOURCE_OPTIONS", "video_url=video_url"):
        assert wiring in APP_SOURCE, f"{wiring} is not passed to run_pipeline"
    for removed in ("lecture_no=LECTURE_NO",):
        assert removed not in APP_SOURCE, f"legacy lecture identity field remains: {removed}"


def test_source_locations_are_resolved_before_the_timestamped_xlsx_is_written():
    locations = PIPELINE_SOURCE.index("error_locations = [locate_row")
    workbook = PIPELINE_SOURCE.index("error_report_xlsx_path = write_error_report_xlsx")
    assert locations < workbook
    assert 'row["timestamp"]' in PIPELINE_SOURCE[locations:workbook]


def test_video_links_remain_the_default_with_an_optional_existing_drive_transcript():
    """Private Drive captions can be visible in-browser but unreadable via OAuth.

    Pasting existing text is an optional escape from redundant STT; the normal
    recording flow still does not ask for transcript files or audio uploads.
    """
    assert "detect_video_source" in APP_SOURCE
    assert "Lecture video link" in APP_SOURCE
    assert "Use existing Drive transcript (optional)" in APP_SOURCE
    assert "transcript_text_override=supplied_drive_transcript" in APP_SOURCE
    for gone in ("_read_transcript_upload",
                 "Or paste the transcript", "Transcript file",
                 "Upload Transcript", "Upload Audio", "Audio URL"):
        assert gone not in APP_SOURCE, f"{gone} is still on the page"


# ------------------------------------------------------ saying why it failed
# A friendly message with no cause is worse than a raw traceback: it costs the
# user the one moment they were looking at the screen and tells them nothing to
# do next. The headline stays; the reason goes next to it.

def _error_branch_source() -> str:
    card = _results_card_source()
    start = card.index('if r.get("error"):')
    return card[start:card.index("continue", start)]


def test_a_failed_run_shows_the_reason_the_run_folder_and_the_trace():
    branch = _error_branch_source()
    for needed in ("error_reason", "run_dir", "error_trace", "Technical details"):
        assert needed in branch, f"{needed} is not shown when a run fails"


def test_the_reason_is_not_developer_only():
    """The person who can act on "ffmpeg is not on PATH" is the person at the
    machine, not whoever set DEVELOPER_MODE."""
    assert "DEV_MODE" not in _error_branch_source()


def test_both_handlers_capture_the_cause():
    """GeminiError used to be caught bare — `except GeminiError:` with no name —
    so the API's own explanation was discarded at the point of failure."""
    run_start = APP_SOURCE.index("result = run_pipeline(")
    run_end = APP_SOURCE.index("progress.progress(i / total)", run_start)
    handlers = APP_SOURCE[run_start:run_end]
    assert "except GeminiError as e:" in handlers
    assert handlers.count("_error_details(e)") == 2, "a handler drops the cause"


def test_every_download_that_can_go_missing_explains_why():
    """A greyed-out button with no reason is the worst version of a failure.

    The located error report is exempt because it is rendered only when its file
    exists — there is no state in which it can be absent AND shown.
    """
    card = _results_card_source()
    calls = re.findall(r"_download_from_path\(\s*\n(.*?)\n\s*\)", card, re.S)
    assert len(calls) == 4, calls
    for call in calls[:3]:
        assert "_missing_file_reason" in call, call
    assert 'if r.get("error_detailed"):' in card, \
        "the located report must be gated on its file existing"


def test_the_reason_comes_from_a_field_not_from_searching_warnings():
    """The first attempt searched the run's `warnings` prose for a keyword, and
    matching a PDF failure on "word" picked out "...(14,663 words)". A confident
    wrong reason is worse than the silence it replaced, so the pipeline hands
    the cause over as its own field."""
    fn_start = APP_SOURCE.index("def _missing_file_reason")
    body = APP_SOURCE[fn_start:APP_SOURCE.index("\ndef ", fn_start + 1)]
    assert "warnings" not in body.split('"""')[-1], "still reading the warnings prose"
    assert '"pdf_reason"' in APP_SOURCE and '"error_report_reason"' in APP_SOURCE
    # ...and the pipeline must actually set them.
    assert "pdf_error=pdf_error" in PIPELINE_SOURCE
    assert "error_report_error=error_report_error" in PIPELINE_SOURCE


def test_the_banner_does_not_claim_success_over_a_failure():
    card = _results_card_source()
    banner = card[:card.index("for idx, r in enumerate(results):")]
    assert "st.warning(" in banner, "a partly failed run still shows a green banner"
    assert 'r.get("error")' in banner and 'r.get("docx")' in banner


@needs_sample
def test_the_pipeline_records_why_the_pdf_is_missing(tmp_path):
    """The end the app reads from. export_docx_to_pdf returns a specific cause;
    the pipeline must keep it, and keep it worded so the app can find it."""
    lecture = tmp_path / "lecture.pdf"
    shutil.copy(SAMPLE, lecture)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "export_docx_to_pdf",
                   lambda docx, out_dir: (None, "Microsoft Word PDF export failed: COM error"))
        result = run_pipeline(
            lecture, subject="environment", language="en", mode="complete",
            google_token="", allow_mock=True, workflow="upsc", area="environment",
            upsc_output_format="dcn", quality_level="off",
            dcn_options=NO_CONVERSION, generate_error_report=False,
            require_transcript=False,      # slides-only fixture, like _run above
        )
    assert result.pdf_path is None
    reason = next((w for w in result.warnings if "export failed" in w.lower()), "")
    assert reason, f"the cause was dropped: {result.warnings}"
    # The app finds it by these needles; the two wordings have to agree.
    assert any(n in reason.lower() for n in ("pdf", "word", "pywin32")), reason


def test_the_xlsx_status_reasons_cover_every_status_the_pipeline_sets():
    """A status with no entry would show a blank reason, which is the bug this
    whole change is about."""
    statuses = set(re.findall(r'error_report_status = "(\w+)"', PIPELINE_SOURCE))
    statuses |= set(re.findall(r'error_report_status == "(\w+)"', PIPELINE_SOURCE))
    covered = set(re.findall(r'^\s*"(\w+)":', _xlsx_reasons_source(), re.M))
    # "ok" and "no_issues" are the two SUCCESS states: the sheet was written and
    # downloads. _XLSX_STATUS_REASONS explains why a sheet is ABSENT, so neither
    # belongs in it — no_issues has its own caption in the summary card instead.
    # (It escaped this check until the pipeline had reason to name it in code.)
    missing = statuses - covered - {"ok", "no_issues"}
    assert not missing, f"no reason text for status(es): {sorted(missing)}"


def _xlsx_reasons_source() -> str:
    start = APP_SOURCE.index("_XLSX_STATUS_REASONS = {")
    return APP_SOURCE[start:APP_SOURCE.index("}", start)]


def test_the_app_does_not_hardcode_a_header_logo_file():
    """The app passes logo_path to the pipeline explicitly, and an explicit path
    outranks the writer's own fallback. Naming a file here therefore overrides
    LOGO_CANDIDATES silently — which is how the bare PW mark stayed in the
    header after the full OnlyIAS lockup was installed."""
    fn_start = APP_SOURCE.index("def _default_dcn_logo_path")
    fn_end = APP_SOURCE.index("\ndef ", fn_start + 1)
    body = APP_SOURCE[fn_start:fn_end]
    assert "default_logo_path()" in body, "the app must ask the writer, not name a file"
    # The docstring explains which file used to be hardcoded, so strip it (and
    # the comments) before looking for a filename in the code itself.
    code = re.sub(r'"""[\s\S]*?"""', "", body)
    code = "\n".join(
        line for line in code.split("\n") if not line.lstrip().startswith("#"))
    assert ".png" not in code, f"a logo filename is hardcoded:\n{code}"


def test_the_header_shows_version_date_and_ist_time():
    assert "Version: v{APP_VERSION}" in APP_SOURCE
    assert '_STAMP["date"]' in APP_SOURCE and '_STAMP["time"]' in APP_SOURCE
    assert "_STAMP = stamp_ist()" in APP_SOURCE
