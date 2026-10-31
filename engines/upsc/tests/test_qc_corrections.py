"""Tests for the correction policy and the anchored correction applier.

Everything runs offline: the policy gets a hand-built rule set instead of
reading config/upsc_qc_rules.yaml, and the applier is pure text manipulation.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.correction_applier import apply_corrections
from src.qc.correction_policy import decide_corrections
from src.qc.models import (
    AMBIGUOUS,
    AUTO_CORRECT,
    CONTRADICTED,
    ClaimVerification,
    CorrectionDecision,
    FactualClaim,
    HUMAN_REVIEW,
    NO_CHANGE,
    QCIssue,
    REMOVE_ONLY_AFTER_APPROVAL,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
    UNVERIFIED,
    VERIFIED,
)
from src.qc.qc_config import QCLimits, QCRule, QCRules


# ---------------------------------------------------------------- helpers
def make_rules() -> QCRules:
    rules = {
        "FACT-LEGAL-001": QCRule(
            id="FACT-LEGAL-001", name="Legal", category="factual",
            severity="high", external_verification=True, auto_correct_allowed=True,
        ),
        "FACT-NAMES-005": QCRule(
            id="FACT-NAMES-005", name="Names", category="factual",
            severity="medium", auto_correct_allowed=True,
        ),
        "LANG-GRAMMAR-002": QCRule(
            id="LANG-GRAMMAR-002", name="Grammar", category="language",
            severity="medium", auto_correct_allowed=False,
        ),
    }
    return QCRules(
        version="test", prompt_version="test", limits=QCLimits(), rules=rules,
        categories={
            "factual": ["FACT-LEGAL-001", "FACT-NAMES-005"],
            "language": ["LANG-GRAMMAR-002"],
        },
    )


def language_issue(confidence: float, **overrides) -> QCIssue:
    fields = dict(
        issue_id="iss-1", category="language", rule_id="LANG-GRAMMAR-002",
        severity="medium", problem="broken sentence", heading="Preamble",
        paragraph_index=1, original_text="Constitution were adopted",
        suggested_text="Constitution was adopted", confidence=confidence,
    )
    fields.update(overrides)
    return QCIssue(**fields)


def verification(status: str, **overrides) -> ClaimVerification:
    fields = dict(
        claim_id="clm-1", original_claim="adopted in 1948", status=status,
        explanation="checked against Laxmikanth", suggested_text="adopted in 1949",
        evidence_ids=["ev-1"], confidence=0.96, auto_correct_allowed=True,
    )
    fields.update(overrides)
    return ClaimVerification(**fields)


def claim() -> FactualClaim:
    return FactualClaim(
        claim_id="clm-1", text="adopted in 1948", claim_type="legal",
        subject="polity", heading="Making of the Constitution", paragraph_index=3,
    )


def decision(original: str, replacement: str, *, location: str = "?|para:?",
             action: str = AUTO_CORRECT, cid: str = "corr-0001") -> CorrectionDecision:
    return CorrectionDecision(
        correction_id=cid, issue_id="iss-1", category="language", action=action,
        location=location, original_text=original, replacement_text=replacement,
        reason="test", confidence=0.99,
    )


RULES = make_rules()


# ------------------------------------------------------------ policy tests
def test_confidence_just_below_threshold_goes_to_human_review():
    [d] = decide_corrections([language_issue(0.94)], [], [], rules=RULES)
    assert d.action == HUMAN_REVIEW
    assert d.requires_human_review is True


def test_confident_language_issue_auto_corrects_without_rule_gate():
    # LANG-GRAMMAR-002 has auto_correct_allowed False; the language category
    # itself opens the gate when both texts are provided.
    [d] = decide_corrections([language_issue(0.96)], [], [], rules=RULES)
    assert d.action == AUTO_CORRECT
    assert d.location == "Preamble|para:1"


def test_language_issue_without_replacement_text_is_not_auto_corrected():
    [d] = decide_corrections(
        [language_issue(0.96, suggested_text="", original_text="")], [], [], rules=RULES,
    )
    assert d.action == HUMAN_REVIEW


def test_contradicted_with_evidence_auto_corrects():
    [d] = decide_corrections([], [verification(CONTRADICTED)], [claim()], rules=RULES)
    assert d.action == AUTO_CORRECT
    assert d.location == "Making of the Constitution|para:3"
    assert d.evidence_ids == ["ev-1"]
    assert d.original_text == "adopted in 1948"
    assert d.replacement_text == "adopted in 1949"


def test_contradicted_without_evidence_needs_human_review():
    [d] = decide_corrections(
        [], [verification(CONTRADICTED, evidence_ids=[])], [claim()], rules=RULES,
    )
    assert d.action == HUMAN_REVIEW


def test_source_not_found_is_unverified_never_false():
    [d] = decide_corrections([], [verification(SOURCE_NOT_FOUND)], [claim()], rules=RULES)
    assert d.action == UNVERIFIED
    assert d.replacement_text == ""


def test_source_unavailable_is_unverified():
    [d] = decide_corrections([], [verification(SOURCE_UNAVAILABLE)], [claim()], rules=RULES)
    assert d.action == UNVERIFIED


def test_verified_means_no_change():
    [d] = decide_corrections([], [verification(VERIFIED)], [claim()], rules=RULES)
    assert d.action == NO_CHANGE


def test_ambiguous_goes_to_human_review():
    [d] = decide_corrections([], [verification(AMBIGUOUS)], [claim()], rules=RULES)
    assert d.action == HUMAN_REVIEW


def test_removal_is_never_automatic():
    v = verification(CONTRADICTED, suggested_text=None, confidence=0.97)
    [d] = decide_corrections([], [v], [claim()], rules=RULES)
    assert d.action == REMOVE_ONLY_AFTER_APPROVAL
    assert d.requires_human_review is True


def test_auto_correct_disabled_downgrades_to_human_review():
    [d1, d2] = decide_corrections(
        [language_issue(0.99)], [verification(CONTRADICTED)], [claim()],
        auto_correct_enabled=False, rules=RULES,
    )
    assert d1.action == HUMAN_REVIEW
    assert d2.action == HUMAN_REVIEW


def test_factual_issue_needing_external_verification_is_not_auto_applied():
    issue = language_issue(
        0.99, category="factual", rule_id="FACT-LEGAL-001",
        original_text="Article 368", suggested_text="Article 356",
        requires_external_verification=True,
    )
    [d] = decide_corrections([issue], [], [], rules=RULES)
    assert d.action == HUMAN_REVIEW


def test_factual_issue_with_permissive_rule_auto_corrects():
    issue = language_issue(
        0.99, category="factual", rule_id="FACT-NAMES-005",
        original_text="Ashoka the great", suggested_text="Ashoka the Great",
    )
    [d] = decide_corrections([issue], [], [], rules=RULES)
    assert d.action == AUTO_CORRECT


def test_verification_without_claim_gets_unknown_location():
    [d] = decide_corrections([], [verification(CONTRADICTED)], [], rules=RULES)
    assert d.location == "?|para:?"


# ----------------------------------------------------------- applier tests
def test_exact_apply_preserves_everything_else():
    notes = "Intro para.\n\nThe Constitution was adopted in 1948.\n\nOutro."
    d = decision("adopted in 1948", "adopted in 1949", location="H|para:1")
    out, records = apply_corrections(notes, [d])
    assert out == "Intro para.\n\nThe Constitution was adopted in 1949.\n\nOutro."
    assert records[0].applied is True
    assert records[0].failure_reason is None


def test_missing_text_fails_without_changing_notes():
    notes = "Only paragraph here."
    d = decision("not present anywhere", "replacement")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "text not found"


def test_two_occurrences_without_anchor_is_ambiguous():
    notes = "The year 1948 matters.\n\nAgain the year 1948 appears."
    d = decision("year 1948", "year 1949")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "ambiguous: 2 occurrences"


def test_reapplying_an_applied_correction_is_idempotent():
    notes = "Heading\n\nThe Constitution was adopted in 1948."
    d = decision("adopted in 1948", "adopted in 1949", location="Heading|para:1")
    once, _ = apply_corrections(notes, [d])
    twice, records = apply_corrections(once, [d])
    assert twice == once
    assert records[0].applied is True
    assert records[0].failure_reason is None


def test_paragraph_anchor_disambiguates_repeated_phrase():
    notes = (
        "The war began in 1856 according to some.\n\n"
        "Unrelated paragraph.\n\n"
        "The war began in 1856 as the faculty said."
    )
    d = decision("began in 1856", "began in 1857", location="History|para:2")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert out == (
        "The war began in 1856 according to some.\n\n"
        "Unrelated paragraph.\n\n"
        "The war began in 1857 as the faculty said."
    )


def test_wrong_anchor_does_not_edit_a_different_paragraph():
    """An anchor that exists but does not contain the text is a failure, not a
    licence to edit elsewhere.

    Falling back to a document-wide search here is how one decision corrupts
    another's anchor: an earlier edit that merges or splits paragraphs shifts
    every later index, and a doc-wide "unique match" then lands in the wrong
    sentence while the audit records a clean success.
    """
    notes = "First paragraph.\n\nThe treaty was signed in 1784."
    d = decision("signed in 1784", "signed in 1783", location="H|para:0")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "text not found in anchored paragraph 0"


def test_unanchored_decision_still_uses_unique_whole_text_match():
    """With no usable anchor there is nothing to scope to, so a single
    unambiguous match in the document is still applied."""
    notes = "First paragraph.\n\nThe treaty was signed in 1784."
    d = decision("signed in 1784", "signed in 1783", location="H|para:?")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert out.endswith("signed in 1783.")


def test_dtp_note_lines_are_never_touched():
    notes = (
        "Heading\n\n"
        "The revolt ended in 1857.\n"
        '(Note to DTP: Insert the image labelled "ended in 1857" from slide 4.)\n\n'
        "Tail."
    )
    d = decision("ended in 1857", "ended in 1858")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert 'labelled "ended in 1857"' in out          # DTP line untouched
    assert "The revolt ended in 1858." in out          # content line fixed


def test_text_only_inside_dtp_note_is_not_found():
    notes = "Heading\n\n(Note to DTP: Insert the map of 1948 borders.)"
    d = decision("1948 borders", "1949 borders")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "text not found"


def test_correction_marker_blocks_are_never_touched():
    notes = (
        "The act passed in 1935.\n\n"
        "[Faculty Point Corrected]\n"
        "Original: the act passed in 1935\n"
        "Correction: the act passed in 1936\n\n"
        "Tail."
    )
    d = decision("passed in 1935", "passed in 1936")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert out.startswith("The act passed in 1936.")
    assert "Original: the act passed in 1935" in out   # audit block untouched


def test_non_auto_decisions_are_not_attempted_or_recorded():
    notes = "The act passed in 1935."
    d = decision("passed in 1935", "passed in 1936", action=HUMAN_REVIEW)
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records == []


def test_empty_replacement_is_refused_as_a_removal():
    notes = "The act passed in 1935."
    d = decision("passed in 1935", "")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert "removal" in (records[0].failure_reason or "")


# ------------------------------------------- regressions from the QC audit
# Each test below reproduces a defect an adversarial review demonstrated
# against the first version of this module. They are behavioural guarantees,
# not implementation details: student notes must never be corrupted by a
# re-run, and an audit record saying "applied" must be true.

def test_rerun_does_not_re_edit_when_replacement_contains_original():
    """'Article 368' -> 'Article 368A' must not become '368AA' on re-run."""
    notes = "Amendments\n\nAmendment is done under Article 368 of the Constitution."
    d = decision("Article 368", "Article 368A", location="Amendments|para:1")
    once, _ = apply_corrections(notes, [d])
    assert "Article 368A of" in once
    twice, records = apply_corrections(once, [d])
    thrice, _ = apply_corrections(twice, [d])
    assert twice == once == thrice
    assert records[0].applied is True
    assert records[0].already_applied is True


def test_replacement_present_elsewhere_is_not_reported_as_applied():
    """A word already in the paragraph must not certify an unlocated fix.

    Reporting this as applied tells faculty a live error was corrected while
    the mistake still stands in the student notes.
    """
    notes = "Exams\n\nStudents receive their marks in April."
    d = decision("recieve", "receive", location="Exams|para:1")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "text not found in anchored paragraph 1"


def test_paragraph_indexes_match_the_claim_extractor():
    """The applier resolves the same paragraph numbers the extractor assigns.

    Consecutive blank lines and whitespace-only lines must not shift the
    index, or an anchor edits a lookalike sentence in a different paragraph.
    """
    from src.qc.claim_extractor import _split_blocks
    from src.qc.correction_applier import paragraph_spans

    for notes in (
        "Intro.\n\n\n\nSentence-X\n\nSentence-X (different meaning)",
        "Intro.\n \nSentence-X\n\t\nSentence-X (different meaning)",
        "Intro.\r\n\r\nSentence-X\r\n\r\nSentence-X (different meaning)",
    ):
        blocks = ["\n".join(b) for b in _split_blocks(notes)]
        spans = [notes[s:e] for s, e in paragraph_spans(notes)]
        assert blocks == [s.replace("\r\n", "\n") for s in spans], notes


def test_anchor_targets_the_right_paragraph_across_blank_runs():
    notes = "Intro.\n\n\n\nSentence-X\n\nSentence-X (different meaning)"
    d = decision("Sentence-X", "Sentence-Y", location="H|para:2")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert out == "Intro.\n\n\n\nSentence-X\n\nSentence-Y (different meaning)"


def test_crlf_notes_are_still_anchored():
    notes = "Rights\r\n\r\nArticle 21 is life.\r\n\r\nArticle 21 also liberty."
    d = decision("Article 21", "Article 21A", location="Rights|para:1")
    out, records = apply_corrections(notes, [d])
    assert records[0].applied is True
    assert out == "Rights\r\n\r\nArticle 21A is life.\r\n\r\nArticle 21 also liberty."


def test_missing_anchor_paragraph_is_a_failure_not_a_doc_wide_edit():
    notes = "Rates\n\nThe rate was 5 percent overall.\n\nThe rate was 5 percent locally."
    d = decision("The rate was 5 percent", "The rate was 6 percent", location="Rates|para:7")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "anchor paragraph 7 does not exist"


def test_self_overlapping_matches_are_ambiguous():
    """'11' occurs twice in '111'; a numeric edit there must not be guessed."""
    notes = "Data\n\nvalue 111 recorded."
    d = decision("11", "12", location="Data|para:1")
    out, records = apply_corrections(notes, [d])
    assert out == notes
    assert records[0].applied is False
    assert records[0].failure_reason == "ambiguous: 2 occurrences"


def test_factual_rule_needing_a_source_is_never_auto_applied_from_review():
    """config's external_verification is authoritative, not the model's flag.

    A reviewer that omits requires_external_verification (or reports it as
    False) must not be able to rewrite a constitutional fact from memory: a
    review issue carries no evidence at all.
    """
    issue = language_issue(
        0.99, issue_id="iss-legal", category="factual", rule_id="FACT-LEGAL-001",
        severity="high", original_text="under Article 226",
        suggested_text="under Article 32", requires_external_verification=False,
    )
    [d] = decide_corrections([issue], [], [], rules=RULES)
    assert d.action == HUMAN_REVIEW
    assert d.requires_human_review is True
    assert "approved source" in d.reason
    assert d.evidence_ids == []


def test_factual_rule_not_needing_a_source_still_auto_corrects():
    """Spelling consistency (external_verification False) stays automatic."""
    issue = language_issue(
        0.99, issue_id="iss-name", category="factual", rule_id="FACT-NAMES-005",
        severity="medium", original_text="Ambedker", suggested_text="Ambedkar",
    )
    [d] = decide_corrections([issue], [], [], rules=RULES)
    assert d.action == AUTO_CORRECT


def test_unverified_claim_is_flagged_for_a_human():
    """SOURCE_NOT_FOUND is not 'false', but it still needs a person to check.

    The detailed report entry and the human-check list read the same flag; if
    they disagree, an auditor is told no review is needed for a fact no
    approved source could confirm.
    """
    [d] = decide_corrections([], [verification(SOURCE_NOT_FOUND)], [claim()], rules=RULES)
    assert d.action == UNVERIFIED
    assert d.requires_human_review is True
