"""The failures that hide inside `except Exception` and turn into a warning.

Every one of these bugs shipped, ran on every lecture, and was invisible —
because the code that broke was wrapped in a broad except whose whole purpose is
to stop a secondary feature from killing a finished run. That is the right
instinct, and it is exactly what makes the bugs undetectable: the run succeeds,
the document is written, and the only evidence is one line in a warnings list
nobody reads.

The real one this file was written for: `date.today()` in the error-report
verification pass, with `date` never imported. It raised NameError on every run
from the day it was added, the except turned it into
"Error-report verification pass skipped: name 'date' is not defined", and the
second-opinion check that demotes unsupported findings never ran once. 1019
tests passed the whole time, because not one of them looked at what the run
said about itself.

So these tests read the warnings. A run is allowed to report an ENVIRONMENTAL
problem — no client, no converter, no network. It is not allowed to report a
Python-level defect, because that is not a condition to survive, it is a bug.
"""
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pipeline
from src.error_locator import LocatorSources, locate_row, segments_from_text
from src.pipeline import run_pipeline

TRANSCRIPT = (
    "Namaskar students, aaj hum Indian Polity ke andar Fundamental Rights, "
    "Article 21, Directive Principles aur judicial review par baat karenge. "
    "Article 32 ko Dr Ambedkar ne heart and soul of the Constitution kaha tha. "
    "Basic structure doctrine Kesavananda Bharati case 1973 mein aaya. "
) * 40

#: The signature of a programming defect that a broad except swallowed. These
#: are the exception types you get from a typo, a bad import, or a signature
#: that drifted — never from a missing file or an unreachable service.
DEFECT = re.compile(
    r"\b(NameError|AttributeError|TypeError|ImportError|ModuleNotFoundError|"
    r"KeyError|IndexError|UnboundLocalError)\b"
    r"|is not defined"
    r"|has no attribute"
    r"|takes \d+ positional argument"
    r"|unexpected keyword argument"
    r"|missing \d+ required",
    re.I,
)


def _run(**kw):
    defaults = dict(
        subject="", language="en", mode="complete", allow_mock=True,
        workflow="upsc", note_type="dcn", upsc_output_format="dcn",
        quality_level="off", generate_error_report=True,
        transcript_text_override=TRANSCRIPT,
    )
    defaults.update(kw)
    return run_pipeline(None, **defaults)


# --------------------------------------------------------- the guard itself

@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_a_run_reports_no_python_defect_in_its_warnings(language):
    """The regression test for the `date` bug, and for every one like it.

    A warning naming a NameError is not a run surviving a bad environment. It
    is a run telling you, in the only channel it has, that a feature you paid
    for did not execute.
    """
    result = _run(language=language)
    defects = [w for w in result.warnings if DEFECT.search(w)]
    assert not defects, (
        "the run swallowed a programming error and carried on:\n  "
        + "\n  ".join(defects))


@pytest.mark.parametrize("quality_level", ["off", "basic"])
def test_no_python_defect_at_any_quality_level(quality_level):
    result = _run(quality_level=quality_level)
    defects = [w for w in result.warnings if DEFECT.search(w)]
    assert not defects, "\n  ".join(defects)


def test_the_error_report_verification_pass_is_actually_reachable():
    """`date` was the only thing standing between this call and running.

    Asserted at the call site rather than through a full run, because in mock
    mode the client is None and `verify_rows` returns before doing anything —
    which is how the NameError stayed invisible: it fired while EVALUATING the
    arguments, before the function it was calling could decline.
    """
    from datetime import date

    from src.error_report_verifier import verify_rows

    # The exact expression the pipeline evaluates. It raised NameError here.
    today = date.today().isoformat()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today)

    rows = [{"original_statement": "x", "error_concern": "y",
             "correct_information_correction": "z", "severity": "High"}]
    returned, summary = verify_rows(rows, None, today=today)
    assert returned == rows, "a declined check must never drop a row"
    assert summary["ran"] is False and summary["reason"] == "no client"


def test_pipeline_imports_every_name_it_uses_at_module_scope():
    """pyflakes over the package, as a test.

    This is the check that would have caught the `date` bug the day it was
    written, in under a second, with no run at all.
    """
    pyflakes = pytest.importorskip("pyflakes.api")
    from pyflakes.reporter import Reporter
    from io import StringIO

    out, err = StringIO(), StringIO()
    root = Path(__file__).resolve().parents[1]
    targets = [root / "app.py", *sorted((root / "src").rglob("*.py"))]
    for target in targets:
        pyflakes.checkPath(str(target), Reporter(out, err))

    undefined = [line for line in out.getvalue().splitlines()
                 if "undefined name" in line]
    assert not undefined, "\n".join(undefined)


# ------------------------------------------------ the locator's honest answer

def test_a_transcript_with_no_timings_is_still_searchable():
    """Captions carry timings; a transcript typed or pasted by a user does not.

    Those runs used to hand the locator an EMPTY haystack, so every row came
    back "unable to verify from provided source" — which reads as "we looked
    and it was not there" when nothing had been looked at.
    """
    segments = segments_from_text(TRANSCRIPT)
    assert segments, "a non-empty transcript must produce something to search"
    assert all(s["start"] is None for s in segments), (
        "untimed text must not acquire timings on the way through")

    sources = LocatorSources(segments=segments)
    assert sources.searchable
    assert not sources.has_timestamps

    row = {"original_statement":
           "Basic structure doctrine came from the Kesavananda Bharati case in 1973"}
    found = locate_row(row, sources)
    assert found.located
    assert found.kind == "transcript_segment", (
        "a segment with no start time must never be reported as a video timestamp")
    assert found.timestamp == "Timestamp unavailable"
    assert found.segment_index and found.segment_index >= 1


def test_text_that_is_not_in_the_lecture_is_still_refused():
    """The fallback adds a haystack; it must not lower the bar for a match."""
    sources = LocatorSources(segments=segments_from_text(TRANSCRIPT))
    row = {"original_statement":
           "GDP is the market value of final goods produced by normal residents"}
    assert not locate_row(row, sources).located


def test_no_sources_reports_itself_as_nothing_to_search():
    """The distinction the warning depends on: we looked and found nothing,
    versus there was nothing to look at."""
    assert not LocatorSources(segments=[], slides=[]).searchable
    assert LocatorSources(segments=[
        {"start": None, "end": None, "text": "Fundamental Rights Article 32"}]).searchable
    # A segment holding nothing a match could ever key on is not searchable
    # either: single letters and stopwords are dropped, so this haystack is
    # empty in every sense that matters.
    assert not LocatorSources(segments=[
        {"start": None, "end": None, "text": "a b c the of and"}]).searchable


def test_the_run_names_the_real_reason_when_nothing_could_be_searched(monkeypatch):
    """A run whose findings cannot be traced must say which of the two it is."""
    monkeypatch.setattr(pipeline, "segments_from_text", lambda text: [])
    result = _run()
    traced = [w for w in result.warnings if w.startswith("Error Report:")]
    assert traced, result.warnings
    assert "missing source, not a failed search" in traced[0], traced[0]


def test_a_normal_run_says_what_it_searched_against():
    result = _run()
    traced = [w for w in result.warnings if w.startswith("Error Report:")]
    assert traced, result.warnings
    assert "transcript text" in traced[0]
    assert "segment numbers rather than clock times" in traced[0], (
        "an untimed search must not imply it produced timestamps")


# --------------------------------------------- the page limit says when it is off

def test_an_unmeasurable_render_says_the_page_limit_was_not_enforced():
    """The limit is enforced by measuring a PDF. With no converter there is no
    measurement and the compression loop never runs — silently, until now."""
    result = _run()
    assert result.pdf_path is None, (
        "conftest stubs the converter; this test depends on that")
    unenforced = [w for w in result.warnings if "could not be enforced" in w]
    assert unenforced, (
        "a run that could not measure its own length must not leave the reader "
        f"believing the limit held: {result.warnings}")
    assert "PDF export error" in unenforced[0], "the warning must point to the export failure"
    assert "LibreOffice" not in unenforced[0]

    length = (result.metadata.get("note_length") or {})
    assert length.get("page_limit_enforced") is False


@pytest.mark.real_pdf
def test_a_measurable_render_records_the_limit_as_enforced():
    from src.pdf_exporter import export_docx_to_pdf  # noqa: F401 — real one restored

    result = _run()
    if result.pdf_path is None:
        pytest.skip("no DOCX->PDF converter on this machine")
    length = (result.metadata.get("note_length") or {})
    assert length.get("page_limit_enforced") is True
    assert not [w for w in result.warnings if "could not be enforced" in w]
