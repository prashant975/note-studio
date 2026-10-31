"""The web fact-check over standalone claims, and clickable sources.

Graded against the author's marked copy the app found 26 of 30, and all four
misses were the same shape: a lone figure, year or attribution with nothing on
the page to contradict it. Reading the page cannot find those. This stage looks
them up instead, ten claims to a call.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.fact_sweep import _dedupe, _sentences, run_fact_sweep  # noqa: E402

NOTES = """[Page 2]
Material & Size: The sculpture is carved from white marble, measures about
1.285 metres and weighs approximately 500 kg.
[Page 6]
The study examined 12 glaciers between 1982 and 2023 and linked the slowdown
to glacier thickening.
[Page 7]
The name has been contributed by Thailand.
[Page 8]
For each vacancy, the Search-cum-Selection Committee, headed by the National
Tribunals Commission (NTC), recommends primary, secondary and tertiary
candidates.
[Page 9]
This sentence has no checkable value in it at all.
"""


@dataclass
class _Answer:
    answer: str
    sources: list = field(default_factory=list)
    queries: list = field(default_factory=lambda: ["a search"])
    searched: bool = True
    model: str = "claude-haiku-4-5"

    @property
    def grounded(self):
        return bool(self.searched or self.sources or self.queries)


def test_the_four_measured_misses_are_all_extracted():
    """A wrapped PDF line splits a claim in half, and a first-N budget never
    reaches the later pages — both cost one of these four."""
    claims = _dedupe(_sentences(NOTES), 70)
    joined = " | ".join(text.lower() for _page, text in claims)
    for probe in ("500 kg", "1982", "thailand", "tertiary"):
        assert probe in joined, f"{probe!r} was not extracted"


def test_a_sentence_with_nothing_checkable_is_left_alone():
    claims = _dedupe(_sentences(NOTES), 70)
    assert not any("no checkable value" in text.lower() for _p, text in claims)


def test_the_budget_is_spread_across_pages_not_spent_on_the_first():
    many = "".join(
        f"[Page {p}]\nClaim {p}.{i} states that the figure is {1900 + i} exactly here.\n"
        for p in range(1, 9) for i in range(10))
    pages = {page for page, _text in _dedupe(_sentences(many), 16)}
    assert len(pages) >= 6, f"budget collapsed onto {pages}"


def test_only_claims_the_search_calls_wrong_become_rows():
    seen = {}

    def lookup(question):
        seen["question"] = question
        return _Answer(
            'Here you go: [{"n": 1, "wrong_value": "500 kg", '
            '"correct_value": "250 kg", "why": "The museum records 250 kg.", '
            '"sources": ["https://britishmuseum.org/x"]}]')

    result = run_fact_sweep(NOTES, lookup=lookup, max_claims=70)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert "500 kg" in row["original_statement"]
    assert row["correct_information_correction"] == "250 kg"
    assert "britishmuseum.org" in row["source_urls"]
    assert row["severity"] == "High"
    assert result.checked > 0
    assert "page" in seen["question"].lower()


def test_an_empty_array_means_everything_checked_out():
    result = run_fact_sweep(NOTES, lookup=lambda q: _Answer("[]"), max_claims=70)
    assert result.rows == []
    assert result.checked > 0
    assert any("found 0 wrong" in w for w in result.warnings())


def test_an_ungrounded_answer_is_not_trusted():
    """No search means the model answered from memory — which is the whole
    thing this stage exists to get past."""
    def lookup(question):
        return _Answer('[{"n": 1, "correct_value": "250 kg", "why": "I recall it."}]',
                       searched=False, queries=[])

    result = run_fact_sweep(NOTES, lookup=lookup, max_claims=70)
    assert result.rows == []
    assert result.failed_batches >= 1


def test_a_lookup_that_fails_never_raises():
    def boom(question):
        raise RuntimeError("provider down")

    result = run_fact_sweep(NOTES, lookup=boom, max_claims=70)
    assert result.rows == [] and result.failed_batches >= 1


def test_nothing_to_check_costs_no_calls():
    calls = []
    result = run_fact_sweep("[Page 1]\nShort text.", lookup=lambda q: calls.append(q),
                            max_claims=70)
    assert result.rows == [] and calls == []


def test_source_urls_become_clickable_links(tmp_path):
    from openpyxl import load_workbook

    from src.models import PdfQcResult
    from src.report.workbook import write_qc_workbook

    result = PdfQcResult(file_name="demo.pdf")
    result.status = "completed_with_warnings"
    result.error_rows = [
        {"original_statement": "A claim.", "error_type": "Factual", "severity": "High",
         "error_concern": "wrong", "correct_information_correction": "right",
         "source_urls": "https://pib.gov.in/page; https://rbi.org.in/other"},
        {"original_statement": "Another.", "error_type": "Conceptual", "severity": "Low",
         "error_concern": "x", "correct_information_correction": "",
         "source_urls": "Source needed"},
    ]
    result.error_report_summary = {"total": 2, "high": 1, "medium": 0, "low": 1}
    path = write_qc_workbook([result], tmp_path / "wb.xlsx")

    ws = load_workbook(path)["Error Report"]
    linked = ws.cell(row=2, column=7)
    assert linked.hyperlink is not None
    assert linked.hyperlink.target == "https://pib.gov.in/page"
    assert linked.font.underline == "single"
    # A placeholder must not pretend to be a link.
    plain = ws.cell(row=3, column=7)
    assert plain.hyperlink is None and plain.font.underline is None
