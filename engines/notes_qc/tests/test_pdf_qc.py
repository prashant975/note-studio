"""End-to-end tests for the standalone PDF QC app.

Nothing here calls the AI. Every test runs the pipeline with no Google token,
which puts the three AI stages into "unavailable" and exercises the seven free
stages plus the whole report path — which is where the logic that could silently
go wrong lives.

    python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook  # noqa: E402

from make_fixtures import build  # noqa: E402
from src.error_report_writer import COLUMNS  # noqa: E402
from src.pdf.text_integrity import check_text_integrity  # noqa: E402
from src.pipeline import run_pdf_qc_batch  # noqa: E402
from src.qc_prompts import build_pdf_error_report_prompt, error_report_contract_block  # noqa: E402


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("fixtures")
    return {p.stem: p for p in build(out)}


@pytest.fixture(scope="session")
def batch(fixtures, tmp_path_factory):
    """One run over every fixture, shared by the tests that read its findings."""
    work = tmp_path_factory.mktemp("run")
    return run_pdf_qc_batch(
        sorted(fixtures.values()),
        google_token="",
        output_dir=work / "output",
        runs_dir=work / "runs",
    )


def _result(batch, name: str):
    for r in batch.results:
        if r.file_name == f"{name}.pdf":
            return r
    raise AssertionError(f"{name}.pdf missing from the batch")


def _problems(result) -> str:
    """Everything this app reported about a file.

    Reads the Error Report, which is now the only output. The format/visual
    findings were removed, and the content checks that used to feed that sheet
    (placeholder title, heading ladder, notes too short, proper-noun
    consistency) are Error Report rows.
    """
    return "\n".join(
        f"{r.get('original_statement', '')} {r.get('error_concern', '')}"
        for r in (result.error_rows or []))


# ---------------------------------------------------------------- the checks

def test_clean_pdf_reports_no_real_problem(batch):
    """A well-formed note must come back with nothing a reviewer has to act on.

    The bar is High/Medium: a Low "this check cannot run against a PDF" note is
    information about the checker, not a defect in the file.
    """
    result = _result(batch, "clean")
    actionable = [f for f in result.qc_findings if f["severity"] in ("High", "Medium")]
    assert actionable == [], actionable
    assert result.status == "completed"



def test_a_placeholder_title_is_reported_but_naming_rules_are_not(batch):
    """One checker, two kinds of finding, and only one of them is content.

    A title left as the prompt's placeholder is wrong TEXT printed on the
    student's page, so it belongs in the report. The rest of what the same
    function reports — a missing L__/DCN__ prefix, an unbreakable run, a
    missing #TITLE marker — is naming convention and rendering, and does not.

    Both halves are asserted here because they are produced by the same code:
    a change that restores one is liable to restore the other by accident.
    """
    problems = _problems(_result(batch, "placeholder_title"))
    assert "placeholder" in problems.lower(), (
        "a placeholder title reaches the student's page and must be reported")
    assert "Lxx_DCNxx" in problems
    assert "L__/DCN__ prefix" not in problems, (
        "the naming convention is formatting and must stay out of the report")





def test_a_broken_file_fails_alone(batch):
    """The whole point of the batch: one bad file must not take the others down."""
    broken = _result(batch, "corrupt")
    assert broken.status == "failed"
    assert broken.error
    # ...and the internal copy's path is never shown to the user.
    assert "runs" not in broken.error and "input" not in broken.error
    assert all(r.status != "failed" for r in batch.results if r is not broken)


def test_every_file_gets_a_result(batch, fixtures):
    assert len(batch.results) == len(fixtures)


# ---------------------------------------------------------------- the report

def test_workbook_has_the_expected_sheets(batch):
    wb = load_workbook(batch.workbook_path)
    assert wb.sheetnames[:2] == ["QC Summary", "Error Report"]
    # One sheet per file as well, because this run carried several.
    assert len(wb.sheetnames) == 2 + len(batch.results)


def test_error_report_keeps_the_original_columns(batch):
    """The seven columns, in the original order, in their original positions."""
    wb = load_workbook(batch.workbook_path)
    header = [c.value for c in wb["Error Report"][1]]
    assert tuple(header[:len(COLUMNS)]) == COLUMNS
    # The batch column is appended, never inserted.
    assert header[len(COLUMNS)] == "Source PDF"


def test_per_file_sheets_carry_exactly_the_seven_columns(batch):
    wb = load_workbook(batch.workbook_path)
    sheet = wb[[n for n in wb.sheetnames if n == "clean"][0]]
    assert tuple(c.value for c in sheet[1]) == COLUMNS


def test_summary_lists_every_file_including_the_failure(batch):
    wb = load_workbook(batch.workbook_path)
    ws = wb["QC Summary"]
    names = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
    assert sorted(names) == sorted(r.file_name for r in batch.results)
    statuses = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value
                for r in range(2, ws.max_row + 1)}
    assert statuses["corrupt.pdf"] == "QC FAILED"
    assert statuses["clean.pdf"] == "Clean"


def test_unavailable_ai_is_reported_as_not_run_not_as_clean(batch):
    """An empty content report must never read as "no errors found"."""
    result = _result(batch, "clean")
    assert result.error_report_status == "unavailable"
    assert result.error_rows[0]["error_type"] == "Check not run"
    # ...and it is not counted as an issue.
    assert result.error_report_summary["total"] == 0


def test_individual_reports_are_written_per_file(batch):
    names = {p.name for p in batch.individual_paths}
    assert "clean_error_report.xlsx" in names
    # The failed file has rows too (the "check not run" placeholder), so it is
    # not silently absent from the output folder.
    assert len(batch.individual_paths) >= len(batch.results) - 1


def test_input_pdfs_are_never_modified(batch, fixtures):
    before = {name: path.read_bytes() for name, path in fixtures.items()}
    for name, path in fixtures.items():
        assert path.read_bytes() == before[name]


# ------------------------------------------------------------ the components

def test_text_integrity_passes_clean_text():
    text = "जाति व्यवस्था की उत्पत्ति के सिद्धांत " * 20
    assert check_text_integrity(text).reliable


def test_text_integrity_catches_visual_order_devanagari():
    """The exact corruption a Word-exported Identity-H Hindi PDF produces."""
    text = "जातत व्यिस्था की उत्पत्ति के त्तसद्ांत ािी ेे " * 20
    result = check_text_integrity(text)
    assert not result.reliable
    assert result.corruption_ratio > 0.02
    assert "Devanagari" in result.reason


def test_text_integrity_ignores_english():
    assert check_text_integrity("Probity in governance " * 100).reliable


def test_the_error_report_contract_is_embedded_verbatim():
    """The prompt's contract block must be the note-generation app's, unchanged.

    If this fails, the two apps have started to disagree about what a row means.
    """
    contract = error_report_contract_block("standard", enabled=True)
    for kwargs in ({"notes_text": "notes"}, {"from_images": True}):
        assert contract in build_pdf_error_report_prompt(**kwargs)


def test_strictness_reaches_the_prompt():
    assert "SCOPE FOR THIS RUN: STRICT." in build_pdf_error_report_prompt(
        "notes", strictness="strict")
    assert "SCOPE FOR THIS RUN: BASIC." in build_pdf_error_report_prompt(
        "notes", strictness="basic")


def test_every_module_imports():
    """Import each module in src/ by name.

    Several are imported lazily inside a stage (so a missing optional dependency
    degrades one check instead of killing the run), which means a syntax error in
    one of them can sit there while the whole suite passes and only surface
    mid-run as a caught exception. This closes that gap.
    """
    import importlib

    root = Path(__file__).resolve().parents[1]
    failures = []
    for path in sorted((root / "src").rglob("*.py")):
        name = ".".join(path.relative_to(root).with_suffix("").parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - the point is to report any of them
            failures.append(f"{name}: {type(e).__name__}: {e}")
    assert not failures, "\n".join(failures)


def test_a_non_dcn_template_gets_its_own_type_scale(fixtures, tmp_path):
    """A PDF on another house style must still come back with its structure.

    The fixture's title is 18 pt and its headings 13 and 11 pt. Against the DCN
    scale (title from 21 pt, H1 from 15 pt) every one of them is body text or a
    level too low, the note reads as structureless, and the checks report a
    missing title and a broken heading ladder that are not there.
    """
    from src.pdf.extractor import extract_pdf
    from src.pdf.reconstruct import TypeScale, measure_type_scale, reconstruct_marker_text

    document = extract_pdf(fixtures["other_template"], tmp_path, render_pages=False)

    scale = measure_type_scale(document)
    assert scale.measured, "the DCN palette is absent, so the scale must be measured"
    assert scale.body_pt == 9.0
    assert scale.h1_min < 13.0 <= scale.title_min

    markers = [l for l in reconstruct_marker_text(document).marker_text.splitlines()
               if l.startswith("#")]
    assert markers[0] == "#LECTURE_TITLE: L09_DCN09_Federalism"
    assert "#H1: Constitutional Basis" in markers
    assert "#H2: Division of Powers" in markers


def test_the_dcn_scale_is_kept_when_the_dcn_palette_is_there(fixtures, tmp_path):
    """The measured scale must never override the palette, which is exact."""
    from src.pdf.extractor import extract_pdf
    from src.pdf.reconstruct import measure_type_scale

    document = extract_pdf(fixtures["clean"], tmp_path, render_pages=False)
    assert not measure_type_scale(document).measured


def test_a_non_dcn_template_is_not_reported_as_structureless(batch):
    result = _result(batch, "other_template")
    problems = _problems(result)
    assert "do not start with" not in problems      # the title was found
    assert "heading ladder is out of order" not in problems
    assert "No #H1 heading found" not in problems


def test_parallel_and_serial_runs_agree(fixtures, tmp_path, monkeypatch):
    """Checking several PDFs at once must change the speed, not the answers."""
    from src import pipeline

    paths = sorted(fixtures.values())
    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 1)
    serial = run_pdf_qc_batch(paths, google_token="",
                              output_dir=tmp_path / "s_out", runs_dir=tmp_path / "s_runs")
    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 4)
    concurrent = run_pdf_qc_batch(paths, google_token="",
                                  output_dir=tmp_path / "p_out", runs_dir=tmp_path / "p_runs")

    def shape(batch):
        return [
            (r.file_name, r.status, r.page_count,
             sorted((f["check"], f["severity"], f["page"] or 0)
                    for f in r.qc_findings))
            for r in batch.results
        ]

    # Results also come back in the input order, not in the order they finished.
    assert [r.file_name for r in concurrent.results] == [r.file_name for r in serial.results]
    assert shape(concurrent) == shape(serial)


def test_parallel_run_gives_every_file_its_own_folder(fixtures, tmp_path, monkeypatch):
    """The folder-name claim is shared state; two workers must not collide."""
    from src import pipeline

    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 4)
    batch = run_pdf_qc_batch(sorted(fixtures.values()), google_token="",
                             output_dir=tmp_path / "out", runs_dir=tmp_path / "runs")
    folders = [r.run_dir for r in batch.results]
    assert len(set(folders)) == len(folders), folders
    assert all(d.exists() for d in folders)


def test_parallel_progress_is_reported_on_completion(fixtures, tmp_path, monkeypatch):
    from src import pipeline

    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 4)
    seen: list[tuple[int, str, bool]] = []
    run_pdf_qc_batch(
        sorted(fixtures.values())[:4], google_token="",
        output_dir=tmp_path / "out", runs_dir=tmp_path / "runs",
        progress_callback=lambda n, total, name, finished=False: seen.append(
            (n, name, finished)),
    )
    assert [n for n, _, _ in seen] == [1, 2, 3, 4], seen
    assert all(finished for _, _, finished in seen)


def test_a_three_argument_progress_callback_still_works(fixtures, tmp_path, monkeypatch):
    from src import pipeline

    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 4)
    seen: list[int] = []
    run_pdf_qc_batch(
        sorted(fixtures.values())[:3], google_token="",
        output_dir=tmp_path / "out", runs_dir=tmp_path / "runs",
        progress_callback=lambda n, total, name: seen.append(n),
    )
    assert seen == [1, 2, 3]


def test_ai_calls_are_capped_across_the_whole_run(fixtures, tmp_path, monkeypatch):
    """The two levels of parallelism must not multiply.

    Four files in flight, each allowed four chunk calls, is sixteen concurrent
    requests without a shared limit — and the proxy answers 429 to that.
    """
    import threading
    import time

    from src import pipeline

    monkeypatch.setattr(pipeline, "MAX_PARALLEL_PDFS", 4)
    monkeypatch.setattr(pipeline, "MAX_PARALLEL_AI_CALLS", 2)

    live = 0
    peak = 0
    lock = threading.Lock()

    class CountingClient:
        fallback_notices: list[str] = []

        def generate(self, prompt, image_paths=None, **kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                time.sleep(0.05)
                return type("R", (), {"text": "{}", "model": "stub"})()
            finally:
                with lock:
                    live -= 1

    monkeypatch.setattr(pipeline, "_build_client",
                        lambda *a, **k: CountingClient())
    # The engine re-checks the proxy allowlist before the first paid call. There
    # is no proxy here, and the point of this test is the semaphore.
    import pw_access

    monkeypatch.setattr(pw_access, "check_allowed", lambda *a, **k: True)
    run_pdf_qc_batch(sorted(fixtures.values()), google_token="x",
                     output_dir=tmp_path / "out", runs_dir=tmp_path / "runs")
    assert peak <= 2, f"{peak} AI calls were in flight at once, limit was 2"
    assert peak > 0, "the stub client was never called"


def test_geometry_and_rendering_are_never_reported(batch):
    """The report describes the NOTES, not how the page was drawn.

    Page numbers, table overflow, blank pages, clipped text and the visual
    render check were removed on purpose: they are typesetting matters, and a
    reviewer reading an error report should not have to wade through them to
    find a wrong fact. The detectors still exist; nothing they say reaches the
    report.
    """
    for name in ("table_overflow", "blank_page", "clipped_text", "placeholder_title"):
        try:
            result = _result(batch, name)
        except Exception:
            continue
        assert result.qc_findings == [], f"{name}: no findings may survive"
        blob = _problems(result).lower()
        for banned in ("page number in its footer", "past the", "content column",
                       "clipped by the page edge", "blank page"):
            assert banned not in blob, f"{name}: geometry leaked into the report: {banned!r}"
