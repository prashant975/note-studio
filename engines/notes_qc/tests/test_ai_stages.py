"""The three AI stages, driven by a stub model.

The real stages call Gemini through the PW proxy, which needs a signed-in
@pw.live Google account and a browser. That makes them untestable in CI and,
worse, untested at development time — so this file drives them with a stub that
speaks the same interface GeminiClient does and returns responses shaped like
the real ones.

What this proves: the prompt is built, the response is parsed by the ORIGINAL
error_report_writer functions, the rows land in the XLSX with the original
columns and severities, chunking splits and merges correctly, image mode fires
when the text layer is unreadable, and a model that fails on one part does not
cost the whole file its report.

What it cannot prove: that Gemini answers well. That needs a real run.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from make_fixtures import clean  # noqa: E402
from src.error_report_writer import COLUMNS  # noqa: E402
from src.qc.engine import run_pdf_qc  # noqa: E402

ROWS = [
    {
        # Quoted from the fixture verbatim: since the engine now drops rows
        # whose sentence is not in the notes, a stub that invents its quote is
        # testing an input the app is designed to reject.
        "original_statement": ("The Right to Information Act, 2005 gives every citizen "
                               "a legal route to hold a public authority to account"),
        "error_type": "Factual",
        "severity": "High",
        "error_concern": "The Right to Information Act received assent in 2005, not 2004.",
        "correct_information_correction": "The RTI Act was passed in 2005.",
        "source_urls": ["https://rti.gov.in/"],
    },
    {
        "original_statement": ("Probity in governance means adherence to moral "
                               "principles by a public servant"),
        "error_type": "Outdated data",
        "severity": "Medium",
        "error_concern": "Correct as of 2026, but the count should carry its date.",
        "correct_information_correction": "India has 28 states as of 2026.",
        "source_urls": [],
    },
]


@dataclass
class StubResponse:
    text: str
    provider: str = "stub"
    model: str = "stub-model"
    raw: dict | None = None
    usage: dict | None = None


@dataclass
class StubClient:
    """Speaks GeminiClient's interface and records what it was asked."""

    rows: list[dict] = field(default_factory=lambda: list(ROWS))
    calls: list[dict] = field(default_factory=list)
    fail_on: set[int] = field(default_factory=set)
    #: Error-report PARTS (by their "This is part N of" label) that fail on
    #: every attempt — the engine retries a failed part once, so failing a
    #: single call index no longer loses a part.
    fail_parts: set[int] = field(default_factory=set)
    fallback_notices: list[str] = field(default_factory=list)

    def generate(self, prompt, image_paths=None, *, max_output_tokens=12000,
                 temperature=0.15, max_images=8):
        index = len(self.calls)
        self.calls.append({
            "prompt": prompt,
            "images": [str(p) for p in (image_paths or [])],
            "max_images": max_images,
        })
        if index in self.fail_on:
            raise RuntimeError("stub model refused this call")
        part = re.search(r"This is part (\d+) of", prompt)
        if part and int(part.group(1)) in self.fail_parts:
            raise RuntimeError("stub model refused this part")

        if "ERROR_REPORT_START" in prompt:
            body = json.dumps(self.rows, ensure_ascii=False)
            return StubResponse(
                "# ERROR_REPORT_START\n" + body + "\n# ERROR_REPORT_END")
        if "independent quality reviewer" in prompt:
            return StubResponse(json.dumps({
                "summary": {"status": "completed", "total_issues": 1},
                "issues": [{
                    "issue_id": "QC-001", "category": "language",
                    "rule_id": "LANG-CLARITY-003", "severity": "low",
                    "heading": "Core Concepts", "paragraph_index": 1,
                    "original_text": "Probity in governance",
                    "problem": "One sentence runs past the readable length.",
                    "suggested_text": "", "requires_external_verification": False,
                    "confidence": 0.8,
                }],
                "claims": [],
            }))
        # The visual check.
        return StubResponse(json.dumps({
            "problems": [{"page": 2, "problem": "A table column is cut off at the right."}]
        }))


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "clean.pdf"
    clean(path)
    return path


def _run(pdf: Path, tmp_path: Path, client, **kwargs):
    return run_pdf_qc(pdf, run_dir=tmp_path / "run", client=client,
                      google_token="", file_name="clean.pdf", **kwargs)


def test_all_three_ai_stages_run_and_land_in_the_result(pdf, tmp_path):
    client = StubClient()
    result = _run(pdf, tmp_path, client)

    assert result.stages["error_report"] == "completed"
    # The visual check was removed: this app reports what is wrong in the notes,
    # not how the page is drawn.
    assert result.stages["visual"] == "not_applicable"
    assert result.stages["ai_review"].startswith("completed")

    # The rows came back through the ORIGINAL normalisation, so the vocabulary
    # and the severities are the original's.
    assert result.error_report_status == "ok"
    types = [r["error_type"] for r in result.error_rows]
    assert types[:2] == ["Factual", "Outdated data"]
    assert [r["severity"] for r in result.error_rows][:2] == ["High", "Medium"]
    assert result.error_rows[0]["source_urls"] == "https://rti.gov.in/"
    # An empty source list becomes the original's placeholder, not an empty cell.
    assert result.error_rows[1]["source_urls"] == "Source needed"

    # The independent review's findings are Error Report ROWS now, not a
    # separate sheet. This is the fix for "many errors are missing": the review
    # applies the whole framework rule set and its findings used to be filed
    # where nobody reading the Error Report would ever see them.
    concerns = " ".join(r["error_concern"] for r in result.error_rows)
    assert "LANG-CLARITY-003" in concerns

    # No format/visual findings survive anywhere.
    assert result.qc_findings == []


def test_the_error_report_call_carries_the_contract(pdf, tmp_path):
    client = StubClient()
    _run(pdf, tmp_path, client)
    prompts = [c["prompt"] for c in client.calls if "ERROR_REPORT_START" in c["prompt"]]
    # The document is split into small page windows so the model has to look at
    # every page, so there is one call PER WINDOW rather than one per document.
    # Every one of them must carry the full contract — a window that arrives
    # without the rules is a window checked to a different standard.
    assert prompts, "the error report must be requested at least once"
    for prompt in prompts:
        assert "SCOPE FOR THIS RUN: STRICT." in prompt      # the default strictness
        assert '"severity" is exactly High, Medium or Low.' in prompt
    assert any("[Page 1]" in p for p in prompts)             # page-located notes


def test_strictness_is_passed_through(pdf, tmp_path):
    client = StubClient()
    _run(pdf, tmp_path, client, strictness="strict")
    prompt = next(c["prompt"] for c in client.calls if "ERROR_REPORT_START" in c["prompt"])
    assert "SCOPE FOR THIS RUN: STRICT." in prompt


def test_a_failed_error_report_call_does_not_lose_the_free_checks(pdf, tmp_path):
    """Every AI call fails. The file still gets a report, and says what happened."""
    client = StubClient(fail_on=set(range(50)))
    result = _run(pdf, tmp_path, client)
    assert result.stages["error_report"] == "failed"
    assert result.error_rows[0]["error_type"] == "Check not run"
    assert result.error_report_summary["total"] == 0
    # The free stages still ran.
    assert result.stages["deterministic"].startswith("completed")
    assert result.stages["layout"].startswith("completed")


def test_a_response_with_no_markers_is_not_read_as_clean(pdf, tmp_path):
    class NoMarkers(StubClient):
        def generate(self, prompt, image_paths=None, **kwargs):
            self.calls.append({"prompt": prompt, "images": [], "max_images": 0})
            if "ERROR_REPORT_START" in prompt:
                return StubResponse("I could not find any problems.")
            return StubResponse("{}")

    result = _run(pdf, tmp_path, NoMarkers())
    assert result.error_report_status == "missing"
    assert result.error_rows[0]["error_type"] == "Report parsing"
    assert result.error_report_summary["total"] == 0


def test_unparseable_json_is_reported_as_such(pdf, tmp_path):
    class BadJson(StubClient):
        def generate(self, prompt, image_paths=None, **kwargs):
            self.calls.append({"prompt": prompt, "images": [], "max_images": 0})
            if "ERROR_REPORT_START" in prompt:
                return StubResponse(
                    "# ERROR_REPORT_START\n{not json at all\n# ERROR_REPORT_END")
            return StubResponse("{}")

    result = _run(pdf, tmp_path, BadJson())
    assert result.error_report_status == "parse_failed"
    assert result.error_report_summary["total"] == 0


def test_pages_go_as_images_when_the_text_layer_is_unreadable(tmp_path, monkeypatch):
    """A Hindi PDF whose font subset scrambles the text must be read visually."""
    from src.pdf import extractor
    from src.pdf.text_integrity import TextIntegrity

    path = tmp_path / "hindi.pdf"
    clean(path)

    real = extractor.check_text_integrity
    monkeypatch.setattr(extractor, "check_text_integrity",
                        lambda text: TextIntegrity(
                            devanagari_words=500, leading_matra=100,
                            reliable=False, reason="stubbed as scrambled"))

    client = StubClient()
    result = run_pdf_qc(path, run_dir=tmp_path / "run", client=client,
                        google_token="", file_name="hindi.pdf")

    report_calls = [c for c in client.calls if "ERROR_REPORT_START" in c["prompt"]]
    assert report_calls, "the error report still has to run"
    assert all(c["images"] for c in report_calls), "pages must travel as images"
    assert "attached as images" in report_calls[0]["prompt"]
    # The text-reading review is skipped rather than run over scrambled text.
    assert result.stages["ai_review"] == "unavailable"
    # And the reader is still told the text layer is broken — as a warning on
    # the summary row now, since the format/visual findings were removed.
    assert any("scrambled" in w or "text layer" in w.lower()
               for w in result.warnings)
    assert real  # keeps the import meaningful


def test_rows_reach_the_workbook_in_the_original_columns(pdf, tmp_path):
    from openpyxl import load_workbook

    from src.report.workbook import write_individual_report, write_qc_workbook

    result = _run(pdf, tmp_path, StubClient())
    single = write_individual_report(result, tmp_path / "out")
    wb = load_workbook(single)
    ws = wb.active
    assert ws.title == "Error Report"
    assert tuple(c.value for c in ws[1]) == COLUMNS
    assert ws.cell(row=2, column=3).value == "Factual"
    assert ws.cell(row=2, column=4).value == "High"

    combined = write_qc_workbook([result], tmp_path / "out" / "combined.xlsx")
    wb2 = load_workbook(combined)
    # One file: no "Source PDF" column and no per-file sheets.
    assert wb2.sheetnames == ["QC Summary", "Error Report"]
    assert tuple(c.value for c in wb2["Error Report"][1]) == COLUMNS


def test_a_correction_starting_with_equals_stays_text(pdf, tmp_path):
    """openpyxl turns a leading "=" into a formula; the original writer guards
    against it, and that guard has to survive the copy."""
    from openpyxl import load_workbook

    from src.report.workbook import write_individual_report

    # "Conceptual" so the standard strictness scope keeps the row: the original
    # writer drops a Medium "Conceptual/formula precision" at that level, which
    # is correct behaviour and would make this test about the wrong thing.
    client = StubClient(rows=[{
        # Quoted from the fixture, so the invented-quote gate keeps the row —
        # this test is about the "=" guard, not about quote grounding.
        "original_statement": ("A citizen's charter states what service a department "
                               "will provide and within what time"),
        "error_type": "Conceptual",
        "severity": "Medium",
        "error_concern": "The identity should be stated explicitly.",
        "correct_information_correction": "= GVA at basic prices + product taxes",
        "source_urls": [],
    }])
    result = _run(pdf, tmp_path, client)
    path = write_individual_report(result, tmp_path / "out")
    cell = load_workbook(path).active.cell(row=2, column=6)
    assert cell.data_type == "s"
    assert cell.value.startswith("= GVA")


def test_a_long_pdf_is_chunked_and_the_parts_are_merged(tmp_path, monkeypatch):
    """Above the single-call word budget the document is split, and the parts'
    JSON arrays are merged with duplicates dropped — the same merge the
    note-generation pipeline does for a chunked lecture."""
    from src.qc import engine

    path = tmp_path / "long.pdf"
    clean(path)
    # Force chunking without building a 6,000-word PDF: the budgets are the
    # knob the pipeline itself turns, so turning them down is the honest way to
    # exercise the split.
    monkeypatch.setattr(engine, "MAX_SINGLE_CALL_TRANSCRIPT_WORDS", 50)
    monkeypatch.setattr(engine, "CHUNK_TRANSCRIPT_WORDS", 60)

    client = StubClient()
    result = run_pdf_qc(path, run_dir=tmp_path / "run", client=client,
                        google_token="", file_name="long.pdf")

    report_calls = [c for c in client.calls if "ERROR_REPORT_START" in c["prompt"]]
    assert len(report_calls) > 1, "the document should have been split"
    # The parts run concurrently, so which prompt was recorded first is a race.
    # Assert every part is labelled instead of assuming an order.
    labels = sorted(
        int(m.group(1))
        for c in report_calls
        if (m := re.search(r"This is part (\d+) of", c["prompt"])))
    assert labels == list(range(1, len(report_calls) + 1)), (
        f"every part must carry its own label; got {labels}")
    # Every part returned the same two rows; the merge keeps one of each,
    # plus the row the independent review contributed.
    assert result.error_report_summary["total"] == 3
    assert len(result.error_rows) == 3
    # The overlapping parts returned the same rows; the merge kept one of each
    # rather than repeating them once per part.
    quoted = [r["original_statement"] for r in result.error_rows]
    assert len(quoted) == len(set(quoted))


def test_one_failed_part_does_not_lose_the_others(tmp_path, monkeypatch):
    from src.qc import engine

    path = tmp_path / "long.pdf"
    clean(path)
    monkeypatch.setattr(engine, "MAX_SINGLE_CALL_TRANSCRIPT_WORDS", 50)
    monkeypatch.setattr(engine, "CHUNK_TRANSCRIPT_WORDS", 60)

    # A part that fails on BOTH attempts. Failing one call index is no longer
    # enough: the engine retries a failed part once, and a single refusal is
    # exactly what the retry exists to absorb.
    client = StubClient(fail_parts={2})
    result = run_pdf_qc(path, run_dir=tmp_path / "run", client=client,
                        google_token="", file_name="long.pdf")
    # The surviving parts still contribute their rows — that is the point of
    # The surviving parts still contribute their rows — that is the point of
    # the per-part isolation — plus the independent review's own row.
    assert result.error_report_summary["total"] == 3
    # But the run must NOT look complete. A part failed, so a stretch of the
    # document was never checked, and a status of "completed" would let a
    # partial report pass for a whole one.
    assert result.stages["error_report"] == "completed_with_warnings"
    assert result.error_report_complete is False
    assert any("INCOMPLETE COVERAGE" in w for w in result.warnings), (
        "a part that failed must be reported to the user, not swallowed")
