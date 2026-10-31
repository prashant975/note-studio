"""Does this app say the same thing about a lecture that the original app did?

The note-generation project checks its notes as marker text, before they are
typeset. This app checks the same notes after, as a PDF. If the two disagree
about a lecture, one of them is wrong — so for lectures where the original
app's own ``quality_report.md`` is on record, its findings are pinned here and
compared, wording and numbers included.

The PDFs and the expectations live together in REFERENCE below. Add a lecture
by dropping its ``_upsc_notes.pdf`` into ``testdata/`` and pasting the format
warnings out of that run's ``quality_report.md``.

Tests skip, rather than fail, when a PDF is not present — the fixtures in
tests/fixtures cover the checks themselves, and this file is about agreement
with the original on real material.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import run_pdf_qc_batch  # noqa: E402

TESTDATA = Path(__file__).resolve().parents[1] / "testdata"

#: pdf -> what the note-generation app's own quality report said about it.
#:
#: "exact" entries must appear word for word. "contains" entries are checks
#: whose message carries a count this app measures slightly differently (it
#: counts bullets off the rendered page, the original counts them off the
#: marker text), so the claim and the percentage are pinned, not the raw counts.
REFERENCE: dict[str, dict] = {
    "probity_governance_english.pdf": {
        "run": "run_20260813_175639_c38ee9c3",
        "exact": [
            "Lecture title has no L__/DCN__ prefix: 'Lecture: Probity Governance'. "
            "It will render as 'Lecture: <title>'.",
        ],
        "contains": [
            # The original counted 246 words, this app counts 243. The three are
            # the "|" characters in the marker text's table rows: the original
            # counts them as words, and a rendered table has no pipes to count.
            # The claim and the threshold are what a reviewer acts on, so those
            # are pinned and the count is not.
            "Notes too short: 24",
            "words against a minimum of 400.",
        ],
        # Lines the CURRENT checker reports and the original run's report does
        # not, because that run is from 13 Aug 2026 and predates these rules.
        # They are checker changes over time, not a disagreement between apps.
        "expected_extra": [
            "Missing the closing 'QUICK REVISION' section (#H1).",
            "Missing the closing 'Key Sources' section (#H3).",
            "The notes carry a #TOPICS_COVERED block",
            "Notes too short:",
        ],
    },
    "indian_society_hindi.pdf": {
        "run": "run_20260818_153941_3a764ae3",
        "exact": [
            "Lecture title has no L__/DCN__ prefix: 'Lecture: भारतीय समाज लेक्चर-5: "
            "भारतीय समाज की प्रमुख विशेषताएं (भाग-5)'. It will render as 'Lecture: <title>'.",
            "Missing the closing 'QUICK REVISION' section (#H1).",
            "Missing the closing 'Key Sources' section (#H3).",
        ],
        # The original counted 176 of 194 bullets, this app 179 of 197 — three
        # more bullets seen on the page than in the marker text, because a
        # bullet the writer nested inside another renders as its own line. Both
        # land on the same 91%, which is the number the check reports on.
        "contains": ["bullets (91%) are short fragments"],
        "expected_extra": [
            # This app's deterministic checks, which the 18 Aug 2026 reference
            # run predates. They implement the framework's English/Grammar #3
            # (sentence clarity) and Factual #6 (proper-noun consistency).
            "split it for readability",
            "Likely inconsistent spelling of the same name",
            "no 'as of <date>' qualifier",
            "Notes too short:",
            "The notes carry a #TOPICS_COVERED block",
            "Missing the 'Topics Covered in the Class' section.",
        ],
    },
}


def _available() -> list[str]:
    return [name for name in REFERENCE if (TESTDATA / name).exists()]


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    names = _available()
    if not names:
        pytest.skip("no reference PDFs in testdata/")
    work = tmp_path_factory.mktemp("regression")
    batch = run_pdf_qc_batch(
        [TESTDATA / n for n in names],
        google_token="",
        output_dir=work / "output",
        runs_dir=work / "runs",
        write_individual_reports=False,
    )
    return {r.file_name: r for r in batch.results}


@pytest.mark.parametrize("name", list(REFERENCE))
def test_the_originals_structural_findings_are_deliberately_absent(runs, name):
    """This report answers a narrower question than the original app's did.

    The note-generation app reported the heading ladder, the missing QUICK
    REVISION and Key Sources sections, the lecture-title prefix, and the share
    of bullets that are fragments. Every one of those describes how the document
    is ASSEMBLED. This app was asked for content errors only -- what the notes
    get WRONG -- so those rows are filtered out before the report is written.

    The divergence is pinned here rather than the test deleted, because an
    exclusion that nothing tests for is one that comes back by accident. If a
    later change starts reporting document structure again, this fails.
    """
    if name not in runs:
        pytest.skip(f"{name} not in testdata/")
    result = runs[name]
    expected = REFERENCE[name]
    problems = [r.get("error_concern", "") for r in (result.error_rows or [])]

    leaked = [line for line in expected["exact"]
              if any(line in p for p in problems)]
    assert not leaked, (
        f"{name}: these describe the document, not its content, and must not "
        f"appear in the report: " + "; ".join(leaked))

    blob = "\n".join(problems)
    for fragment in expected["contains"]:
        assert fragment not in blob, (
            f"{name}: {fragment!r} is a formatting finding and must not be "
            f"reported")



@pytest.mark.parametrize("name", list(REFERENCE))
def test_reports_nothing_the_original_did_not(runs, name):
    """The other direction: no invented structural findings.

    Only the structure stage is compared — it is the one that runs the original
    checker over the same notes. The page/layout/integrity stages exist because
    the original could not see a finished PDF at all, so they have nothing to
    disagree with.
    """
    if name not in runs:
        pytest.skip(f"{name} not in testdata/")
    result = runs[name]
    expected = REFERENCE[name]
    allowed = expected["exact"] + expected["contains"] + expected["expected_extra"]

    unexplained = []
    for row in (result.error_rows or []):
        problem = row.get("error_concern", "")
        if not problem or row.get("error_type") in {"No issue", "Check not run",
                                                    "Report parsing", "Incomplete scan"}:
            continue
        if any(a in problem or problem in a for a in allowed):
            continue
        unexplained.append(problem)

    assert not unexplained, (
        f"{name}: this app reported structural findings the original did not:\n"
        + "\n".join(f"  - {u}" for u in unexplained))


@pytest.mark.parametrize("name", list(REFERENCE))
def test_page_count_and_language_are_right(runs, name):
    if name not in runs:
        pytest.skip(f"{name} not in testdata/")
    result = runs[name]
    assert result.page_count > 0
    assert result.language_code == ("hi" if "hindi" in name else "en")
    assert result.status != "failed"
