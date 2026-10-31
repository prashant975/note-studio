"""The QC -> yellow-highlight bridge (src/correction_highlights.py).

What these tests are really defending is the pair of promises the feature makes
to a reviewer:

    a yellow span means QC changed a FACT here
    no yellow span means QC changed nothing here

Both halves matter. A highlight that appears for a grammar fix teaches the
reviewer to ignore the colour; a highlight placed on text nobody verified is
worse than none at all. So there are as many tests below for what must NOT be
highlighted as for what must.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.correction_highlights import (
    Highlight,
    collect_highlights,
    highlight_spans,
    highlights_from_error_rows,
    highlights_from_qc,
    is_highlightable_category,
    narrow_change,
)
from src.qc.correction_applier import apply_corrections
from src.qc.models import AUTO_CORRECT, HUMAN_REVIEW, AppliedCorrection, CorrectionDecision


def _applied(**kw) -> AppliedCorrection:
    base = dict(
        correction_id="C-1", issue_id="QC-001", category="factual",
        location="Heading|para:0", original_text="", replacement_text="",
        action=AUTO_CORRECT, applied=True, highlight=True,
    )
    base.update(kw)
    return AppliedCorrection(**base)


# ------------------------------------------------------------ what counts

def test_only_content_categories_are_highlightable():
    for category in ("factual", "conceptual", "terminology", "numerical", "sequence"):
        assert is_highlightable_category(category), category
    # Spec §11: normal AI rewriting must never become yellow.
    for category in ("language", "formatting", "compliance", "style"):
        assert not is_highlightable_category(category), category


def test_an_unknown_category_fails_closed():
    """A category nobody has ruled on does not start painting the notes."""
    assert not is_highlightable_category("vibes")
    assert not is_highlightable_category("")
    assert not is_highlightable_category(None)


# -------------------------------------------------- only the changed part

def test_narrow_change_keeps_only_the_words_that_moved():
    original = "The Constituent Assembly adopted the Constitution in 1948."
    corrected = "The Constituent Assembly adopted the Constitution in 1949."
    assert narrow_change(original, corrected) == "1949"


def test_narrow_change_expands_a_bare_character_out_to_its_word():
    """1948 -> 1949 differs in one character; shading "9" reads as a printing
    fault, so the span grows to the whole token."""
    span = narrow_change("adopted in 1948", "adopted in 1949")
    assert span == "1949"


def test_narrow_change_keeps_a_multi_word_phrase_whole():
    original = "X is always greater than Y."
    corrected = "X is greater than or equal to Y."
    span = narrow_change(original, corrected)
    assert span in corrected
    assert "greater than or equal to" in span
    # Not the whole sentence: the shared head and tail are trimmed.
    assert span != corrected


def test_a_one_character_numeric_fix_does_not_shade_the_whole_sentence():
    """The commonest numeric correction, and the case that used to defeat the
    narrowing: a single digit next to punctuation sits on a word boundary
    already, so the boundary-expansion pass correctly leaves it alone — and the
    "too short to place" fallback then shaded the entire replacement."""
    corrected = "The rate is 6% today"
    span = narrow_change("The rate is 5% today", corrected)
    assert span in corrected
    assert span != corrected, "a one-digit fix must not shade the whole line"
    assert "6%" in span, span


def test_a_unit_symbol_is_shaded_with_the_number_it_belongs_to():
    """Shading "74.04" and leaving the "%" outside reads as a clipped highlight."""
    assert narrow_change("literacy was 64%", "literacy was 74.04%") == "74.04%"


def test_a_narrowed_span_never_swallows_the_sentence_punctuation():
    """The opposite failure: a highlight that runs on into the full stop looks
    like the correction continued into the next sentence."""
    assert narrow_change("The Constitution was adopted in 1948.",
                         "The Constitution was adopted in 1949.") == "1949"


def test_narrowing_survives_degenerate_input():
    """_expand_to_word_edges is module-level; it must not index past the end."""
    for original, corrected in [("", ""), ("", "x"), ("x", ""), ("a", "b"),
                                ("abc", "abc"), (" ", "  "), ("ab", "ac")]:
        span = narrow_change(original, corrected)
        assert span == "" or span in corrected.strip(), (original, corrected, span)


def test_narrow_change_returns_everything_when_nothing_is_shared():
    assert narrow_change("completely different", "an unrelated sentence entirely") == (
        "an unrelated sentence entirely")


def test_a_pure_deletion_produces_no_span():
    """There is no new wording to point at; the QC report records the removal."""
    assert narrow_change("the wholly unsupported claim here", "the claim here") == ""


def test_an_unchanged_replacement_is_returned_as_is():
    assert narrow_change("same text", "same text") == "same text"


# ------------------------------------------------------- from QC records

def test_a_factual_correction_becomes_a_highlight():
    notes = "#H1: Polity\n\n- The Constitution was adopted in 1949.\n"
    found = highlights_from_qc([_applied(
        original_text="adopted in 1948", replacement_text="adopted in 1949")], notes)
    assert [h.text for h in found] == ["1949"]
    assert found[0].error_id == "QC-001"
    assert found[0].source == "qc"


def test_a_grammar_correction_does_not():
    notes = "- The Constitution was adopted in 1949, and it came into force later.\n"
    assert highlights_from_qc([_applied(
        category="language",
        original_text="adopted in 1949 and it came into force later",
        replacement_text="adopted in 1949, and it came into force later")], notes) == []


def test_a_correction_that_could_not_be_applied_is_not_highlighted():
    """The applier failed to place it, so nothing in the notes was changed."""
    notes = "- The Constitution was adopted in 1949.\n"
    assert highlights_from_qc([_applied(
        applied=False, failure_reason="text not found",
        original_text="adopted in 1948", replacement_text="adopted in 1949")], notes) == []


def test_a_correction_whose_text_is_not_in_the_notes_is_dropped():
    """A later pass reworded it. Approximating the position would put yellow on
    text nobody verified, so the span is dropped instead."""
    notes = "- The Constitution came into effect in 1950.\n"
    assert highlights_from_qc([_applied(
        original_text="adopted in 1948", replacement_text="adopted in 1949")], notes) == []


def test_a_span_that_occurs_twice_is_not_shaded_at_all():
    """The writer shades every occurrence of a span it is given. A span that
    appears twice would paint an occurrence QC never looked at — and "no yellow
    here means QC changed nothing here" is half of what the colour promises."""
    notes = "- Adopted in 1949 here.\n- Also adopted in 1949 there.\n"
    assert collect_highlights(notes, applied=[_applied(
        original_text="in 1948", replacement_text="in 1949")]) == []


def test_an_ambiguous_narrow_span_widens_to_a_unique_one():
    """Widening before dropping: the surrounding sentence is usually unique even
    when the corrected phrase alone is not."""
    notes = ("- The rate was 74.04% at the 2011 Census.\n"
             "- The 2011 Census is the source for the figure above.\n")
    found = collect_highlights(notes, applied=[_applied(
        original_text="The rate was 64% at the 2011 Census",
        replacement_text="The rate was 74.04% at the 2011 Census")])
    assert [h.text for h in found] == ["74.04%"]


def test_the_suppression_flag_on_the_record_is_honoured():
    notes = "- The Constitution was adopted in 1949.\n"
    assert highlights_from_qc([_applied(
        highlight=False,
        original_text="adopted in 1948", replacement_text="adopted in 1949")], notes) == []


# ------------------------------------------- the applier sets the flag itself

def test_the_applier_records_highlight_from_the_category():
    """Spec §14: the correction record carries the flag, so the report and the
    page cannot disagree about which changes are shown in yellow."""
    notes = "The Constitution was adopted in 1948 by the Assembly.\n"
    decisions = [
        CorrectionDecision(
            correction_id="C-1", issue_id="QC-001", category="factual",
            action=AUTO_CORRECT, location="|para:0",
            original_text="adopted in 1948", replacement_text="adopted in 1949",
            reason="", confidence=0.99),
        CorrectionDecision(
            correction_id="C-2", issue_id="QC-002", category="language",
            action=AUTO_CORRECT, location="|para:0",
            original_text="by the Assembly", replacement_text="by the Constituent Assembly",
            reason="", confidence=0.99),
    ]
    text, records = apply_corrections(notes, decisions)
    assert "1949" in text and "Constituent Assembly" in text
    by_id = {r.correction_id: r for r in records}
    assert by_id["C-1"].applied and by_id["C-1"].highlight
    assert by_id["C-2"].applied and not by_id["C-2"].highlight


def test_a_human_review_decision_produces_no_record_at_all():
    """Unchanged behaviour: only AUTO_CORRECT is attempted."""
    notes = "The Constitution was adopted in 1948.\n"
    text, records = apply_corrections(notes, [CorrectionDecision(
        correction_id="C-9", issue_id="QC-009", category="factual",
        action=HUMAN_REVIEW, location="|para:0",
        original_text="1948", replacement_text="1949", reason="", confidence=0.4)])
    assert text == notes and records == []


# ----------------------------------------------------- from the Error Report

def test_an_error_report_row_is_used_only_when_it_is_verbatim_in_the_notes():
    notes = "- India's literacy rate was 74.04% at the 2011 Census.\n"
    rows = [
        {"original_statement": "India's literacy rate was 64% at the 2011 Census",
         "correct_information_correction":
             "India's literacy rate was 74.04% at the 2011 Census",
         "error_type": "Numerical/outdated", "severity": "High"},
        # Prose about the fix, not the fix — must not be matched.
        {"original_statement": "The scheme launched in 2014",
         "correct_information_correction": "Check the ministry's launch-year page.",
         "error_type": "Factual", "severity": "Medium"},
    ]
    found = highlights_from_error_rows(rows, notes)
    assert len(found) == 1
    assert found[0].text in notes
    assert found[0].source == "error_report"


def test_placeholder_error_rows_are_never_highlighted():
    notes = "No reportable factual/conceptual error found.\n"
    rows = [{"original_statement": "No reportable factual/conceptual error found.",
             "correct_information_correction": "No correction required.",
             "error_type": "No issue", "severity": "Low"}]
    assert highlights_from_error_rows(rows, notes) == []


# ------------------------------------------------------------- collection

def test_collect_deduplicates_and_orders_longest_first():
    notes = ("- The Wildlife (Protection) Act, 1972 governs this, "
             "and the Wildlife (Protection) Act, 1972 alone.\n")
    records = [
        _applied(correction_id="C-1", original_text="the 1971 Act",
                 replacement_text="the Wildlife (Protection) Act, 1972"),
        _applied(correction_id="C-2", original_text="the 1971 Act",
                 replacement_text="the Wildlife (Protection) Act, 1972"),
    ]
    found = collect_highlights(notes, applied=records)
    assert len(found) == 1, "the same span must not be listed twice"
    lengths = [len(h.text) for h in collect_highlights(notes, applied=records + [
        _applied(correction_id="C-3", original_text="governs", replacement_text="governs this")])]
    assert lengths == sorted(lengths, reverse=True)


def test_collect_with_nothing_to_say_returns_nothing():
    assert collect_highlights("some notes", applied=[], error_rows=[]) == []
    assert collect_highlights("", applied=None, error_rows=None) == []
    assert highlight_spans(None) == []


def test_rebuild_reuses_the_runs_spans_and_drops_the_ones_that_were_edited(tmp_path):
    """Edit-and-rebuild runs no QC — the corrections were decided once, during
    the run — so the spans are read back from the run folder. A span the editor
    has since reworded must be dropped, not approximated onto new wording."""
    import json

    from src.pipeline import _rebuild_highlight_spans

    (tmp_path / "correction_highlights.json").write_text(json.dumps([
        {"highlighted_text": "1949", "error_id": "QC-001", "highlight": True},
        {"highlighted_text": "74.04%", "error_id": "QC-002", "highlight": True},
    ]), encoding="utf-8")
    edited = "- The Constitution was adopted in 1949.\n- The rate is now different.\n"
    assert _rebuild_highlight_spans(tmp_path, edited) == ["1949"]


def test_rebuild_without_a_highlight_file_is_silent(tmp_path):
    from src.pipeline import _rebuild_highlight_spans

    assert _rebuild_highlight_spans(tmp_path, "some notes") == []
    (tmp_path / "correction_highlights.json").write_text("{not json", encoding="utf-8")
    assert _rebuild_highlight_spans(tmp_path, "some notes") == []


def test_the_audit_record_carries_the_schema_the_spec_asks_for():
    record = Highlight(text="1949", error_id="QC-001", error_type="factual",
                       severity="high", source="qc").as_dict()
    assert record == {
        "error_id": "QC-001", "highlighted_text": "1949", "error_type": "factual",
        "severity": "high", "source": "qc", "highlight": True,
    }
