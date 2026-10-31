"""Source locations on error report rows — and the refusal to invent one.

An error that says only "this fact is wrong" costs a reviewer the whole lecture
to check. A location makes it seconds. But a location that is confidently WRONG
is worse than none at all: the reviewer jumps to the timestamp, hears something
unrelated, and stops trusting the rest of the report.

So most of what is tested here is the refusal — no timestamp without a real
transcript segment behind it, no slide number without a real slide, and honest
wording when the text simply cannot be found.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.error_locator import (
    LOCATION_UNAVAILABLE,
    MIN_MATCH_SCORE,
    TIMESTAMP_UNAVAILABLE,
    LocatorSources,
    SourceLocation,
    format_timestamp,
    locate,
    locate_row,
    timestamped_url,
)
from src.error_report_detailed import (
    PENDING_VERIFICATION,
    render_detailed_error_report,
    render_error_block,
)

SEGMENTS = [
    {"start": 0.0, "end": 62.0,
     "text": "Namaskar students, aaj hum samvidhan ke basic features padhenge."},
    {"start": 1122.0, "end": 1145.0,
     "text": "the RTI Act came into force in 2005 giving citizens a statutory "
             "right to seek information from public authorities"},
    {"start": 2530.0, "end": 2571.0,
     "text": "Article 342 empowers the President to notify Scheduled Tribes "
             "for a State after consulting the Governor"},
]

SLIDES = [
    {"slide_no": 14, "heading": "Features of Indian Constitution",
     "text": "Federal structure, parliamentary system, independent judiciary, "
             "fundamental rights"},
    {"slide_no": 27, "heading": "Fundamental Rights",
     "text": "Articles 12 to 35 cover the six fundamental rights"},
]


def _sources(**kw):
    base = dict(segments=SEGMENTS, slides=SLIDES,
                video_url="https://www.youtube.com/watch?v=abc123",
                slide_source_type="pptx")
    base.update(kw)
    return LocatorSources(**base)


# ------------------------------------------------------------ finding things

def test_a_claim_is_traced_to_the_minute_of_the_class_that_said_it():
    """The whole point: a reported error becomes checkable in seconds."""
    found = locate("RTI Act was enacted in 2005", _sources())
    assert found.kind == "video"
    assert found.timestamp == "00:18:42 – 00:19:05"
    assert "the RTI Act came into force in 2005" in found.source_text
    assert found.confidence >= MIN_MATCH_SCORE


def test_a_youtube_location_becomes_a_link_that_opens_at_that_point():
    found = locate("RTI Act came into force 2005 statutory right", _sources())
    assert found.url == "https://www.youtube.com/watch?v=abc123&t=1122s"


def test_a_slide_claim_reports_the_slide_number_and_title():
    found = locate("federal structure parliamentary system independent judiciary",
                   _sources())
    assert found.kind == "slide"
    assert found.slide_no == 14
    assert found.describe() == 'Slide 14 — "Features of Indian Constitution"'


def test_a_pdf_source_reports_a_page_and_section_not_a_slide():
    """Same match, different vocabulary: a reviewer opening a PDF is looking for
    a page, and "Slide 27" would send them to the wrong mental place."""
    found = locate("Articles 12 to 35 cover the six fundamental rights",
                   _sources(slide_source_type="pdf"))
    assert found.kind == "pdf_page"
    assert found.page_no == 27
    assert found.slide_no is None
    assert found.describe() == "Page 27, Section: Fundamental Rights"


def test_a_transcript_without_timings_reports_a_segment_number():
    """Spec: when there is no timestamp to be had, say which segment — never
    estimate a clock time from position."""
    untimed = [{"start": None, "end": None, "text": s["text"]} for s in SEGMENTS]
    found = locate("RTI Act came into force in 2005", _sources(segments=untimed))
    assert found.kind == "transcript_segment"
    assert found.segment_index == 2
    assert found.timestamp == TIMESTAMP_UNAVAILABLE
    assert found.describe() == "Transcript segment 2"


# --------------------------------------------------------- refusing to invent

def test_text_that_is_not_in_the_source_gets_no_location_at_all():
    found = locate("Quantum chromodynamics research stations in Antarctica",
                   _sources())
    assert found.kind == "unavailable"
    assert not found.located
    assert found.timestamp == TIMESTAMP_UNAVAILABLE
    assert found.describe() == LOCATION_UNAVAILABLE
    assert found.start_seconds is None


def test_a_weak_overlap_is_not_good_enough():
    """Common words match everything. A location has to be earned by the
    distinctive ones or every row gets pointed at segment 1."""
    found = locate("the and of in this that", _sources())
    assert not found.located


def test_no_sources_at_all_locates_nothing_and_does_not_crash():
    empty = LocatorSources(segments=[], slides=[], video_url="")
    assert not locate("RTI Act 2005", empty).located
    assert not empty.has_timestamps


def test_a_non_youtube_recording_gets_no_fake_seek_link():
    """Drive and direct MP4 have no timestamp convention, so a constructed link
    would simply not work. The timestamp is still reported."""
    found = locate("RTI Act came into force in 2005",
                   _sources(video_url="https://drive.google.com/file/d/xyz/view"))
    assert found.kind == "video"
    assert found.timestamp == "00:18:42 – 00:19:05"
    assert found.url == ""
    assert timestamped_url("https://drive.google.com/file/d/xyz/view", 100) == ""


def test_timestamps_are_formatted_not_guessed():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(1122.0) == "00:18:42"
    assert format_timestamp(4271) == "01:11:11"
    # A missing time is missing, not zero.
    assert format_timestamp(None) == ""
    assert format_timestamp(-5) == ""
    assert format_timestamp("nonsense") == ""


# ------------------------------------------------------------ locating a row

def test_a_row_is_located_from_its_correction_first():
    """The correction quotes what the source really said; the generated
    statement is the version that got it wrong and may share fewer words."""
    row = {
        "original_statement": "RTI Act was enacted in 2006.",
        "correct_information_correction":
            "RTI Act came into force in 2005 giving citizens a statutory right.",
        "error_concern": "Wrong year.",
    }
    found = locate_row(row, _sources())
    assert found.kind == "video"
    assert found.timestamp == "00:18:42 – 00:19:05"


def test_a_row_with_nothing_findable_is_reported_as_unlocated():
    row = {"original_statement": "Entirely unrelated assertion about penguins.",
           "correct_information_correction": "", "error_concern": ""}
    assert not locate_row(row, _sources()).located


# --------------------------------------------------------------- the report

def _row(**kw):
    base = {
        "original_statement": "RTI Act was enacted in 2006.",
        "error_type": "Date/Year Error",
        "severity": "High",
        "error_concern": "The generated note states an incorrect year.",
        "correct_information_correction": "RTI Act came into force in 2005.",
        "source_urls": "",
    }
    base.update(kw)
    return base


def test_the_report_block_carries_every_field_a_reviewer_needs():
    row = _row()
    block = render_error_block(7, row, locate_row(row, _sources()))
    for field in ("ERROR #07", "Type:", "Severity:", "Location:", "Timestamp:",
                  "AI-generated note:", "Source text", "Issue:",
                  "Correct version:", "Confidence:", "Verification:"):
        assert field in block, field
    assert "00:18:42" in block
    assert "Open at this point: https://www.youtube.com/watch?v=abc123&t=1122s" in block
    assert PENDING_VERIFICATION in block


def test_an_unlocated_error_says_so_instead_of_showing_a_time():
    row = _row(original_statement="Penguins drafted Article 21.",
               correct_information_correction="")
    block = render_error_block(1, row, locate_row(row, _sources()))
    assert "Timestamp unavailable" in block
    assert LOCATION_UNAVAILABLE in block
    assert "00:" not in block, "a clock time appeared for an unlocated error"


def test_the_report_orders_high_severity_first():
    rows = [_row(severity="Low", original_statement="low one"),
            _row(severity="High", original_statement="high one"),
            _row(severity="Medium", original_statement="medium one")]
    locations = [locate_row(r, _sources()) for r in rows]
    report = render_detailed_error_report(rows, locations)
    assert report.index("high one") < report.index("medium one") < report.index("low one")


def test_the_report_says_how_many_errors_it_could_actually_locate():
    """A reviewer needs to know the difference between "all traced" and "half of
    these are unverifiable" before they start."""
    rows = [_row(), _row(original_statement="Penguins drafted Article 21.",
                         correct_information_correction="")]
    locations = [locate_row(r, _sources()) for r in rows]
    report = render_detailed_error_report(rows, locations)
    assert "Errors reported: **2**" in report
    assert "With a located source: **1** of 2" in report
    assert LOCATION_UNAVAILABLE in report


def test_a_transcript_without_timings_is_called_out_once_at_the_top():
    rows = [_row()]
    report = render_detailed_error_report(
        rows, [SourceLocation()], transcript_has_timestamps=False)
    assert "no timings" in report
    assert "No timestamp has been estimated." in report


def test_an_empty_report_is_still_a_report():
    report = render_detailed_error_report([], [])
    assert "No errors were reported" in report
    assert "Errors reported: **0**" in report


# ------------------------------------------------- the error-type vocabulary

#: The categories a reviewer asked the report to be able to express.
SPEC_ERROR_TYPES = [
    "Factual Error", "Conceptual Error", "Terminology Error", "Date/Year Error",
    "Number/Data Error", "Constitutional/Legal Error", "Scheme/Policy Error",
    "Committee/Report Error", "Person/Institution Error", "Historical Error",
    "Geography Error", "Mismatch with Source", "Missing Important Information",
    "AI Hallucination", "Incorrect Interpretation", "Language/Translation Error",
    "Diagram/Flow Error",
]


def test_every_named_error_type_survives_the_default_strictness_filter():
    """The filter drops types it does not recognise unless they are High.

    A Medium "Historical Error" was therefore being thrown away silently — the
    row existed, the model reported it, and nothing showed it to anyone. Naming
    the categories is what makes them reportable.
    """
    from src.error_report_writer import normalise_rows

    rows = [{"original_statement": f"statement about {t}", "error_type": t,
             "severity": "Medium", "error_concern": "something is wrong",
             "correct_information_correction": "the corrected form",
             "source_urls": ""} for t in SPEC_ERROR_TYPES]
    kept = {r["error_type"] for r in normalise_rows(rows, strictness="standard")}
    assert not set(SPEC_ERROR_TYPES) - kept, set(SPEC_ERROR_TYPES) - kept


def test_a_fabricated_claim_is_reportable_at_every_strictness():
    """An invented fact is the one thing no setting should be able to hide."""
    from src.error_report_writer import normalise_rows

    row = {"original_statement": "Penguins drafted Article 21.",
           "error_type": "AI Hallucination", "severity": "Medium",
           "error_concern": "Nothing in the class supports this.",
           "correct_information_correction": "Remove the claim.", "source_urls": ""}
    for strictness in ("basic", "standard", "strict"):
        assert normalise_rows([dict(row)], strictness=strictness), strictness


def test_a_translation_edit_is_never_highlighted_as_a_fact_change():
    """Yellow in the notes means "the substance of this changed". Notes can now
    be written in a language the class was not taught in, so translation edits
    exist — and shading them would dilute what the colour means."""
    from src.correction_highlights import NON_HIGHLIGHT_ERROR_TYPES

    assert "language/translation error" in NON_HIGHLIGHT_ERROR_TYPES
