"""Offline, deterministic tests for the QC Error Fixed Report renderer."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.error_fixed_report import qc_quality_report_meta, render_error_fixed_report_md
from src.qc.models import (
    AUTO_CORRECT,
    HUMAN_REVIEW,
    AppliedCorrection,
    ClaimVerification,
    CorrectionDecision,
    Evidence,
    FactualClaim,
    QCIssue,
    QCRunResult,
    StageStatus,
)

PASSAGE = "Article 21A provides free and compulsory education to children aged 6 to 14."
CLAIM_TEXT = "Article 21 provides free education | for all children"
LANG_ORIGINAL = "The constitution have many provisions regarding rights."


def make_result() -> QCRunResult:
    """2 issues + 1 evidence-backed claim verification + 1 applied correction."""
    issues = [
        QCIssue(
            issue_id="ISS-1", category="factual", rule_id="FACT-LEGAL-001",
            severity="high", problem="Wrong article number for RTE",
            heading="Fundamental Rights", paragraph_index=3,
            original_text=CLAIM_TEXT, requires_external_verification=True,
        ),
        QCIssue(
            issue_id="ISS-2", category="language", rule_id="LANG-GRAM-002",
            severity="low", problem="Subject-verb agreement error",
            heading="Preamble", paragraph_index=1, original_text=LANG_ORIGINAL,
        ),
    ]
    claim = FactualClaim(
        claim_id="CLM-1", text=CLAIM_TEXT, claim_type="legal_article",
        subject="polity", heading="Fundamental Rights", paragraph_index=3,
        source_slide=12, transcript_timestamp="00:14:32",
    )
    evidence = Evidence(
        evidence_id="EV-1", claim_id="CLM-1", source_type="book",
        source_name="Indian Polity (M. Laxmikanth)", passage=PASSAGE,
        publication_date="2023", edition="7th", page_number=145,
    )
    verification = ClaimVerification(
        claim_id="CLM-1", original_claim=CLAIM_TEXT, status="CONTRADICTED",
        explanation="The source attributes the RTE provision to Article 21A, not 21.",
        suggested_text="Article 21A provides free education for children aged 6-14.",
        evidence_ids=["EV-1"], confidence=0.97, auto_correct_allowed=True,
    )
    decisions = [
        CorrectionDecision(
            correction_id="COR-1", issue_id="CLM-1", category="factual",
            action=AUTO_CORRECT, location="Fundamental Rights|3",
            original_text=CLAIM_TEXT,
            replacement_text="Article 21A provides free education for children aged 6-14.",
            reason="Contradicted by an approved source", confidence=0.97,
            evidence_ids=["EV-1"],
        ),
        CorrectionDecision(
            correction_id="COR-2", issue_id="ISS-2", category="language",
            action=HUMAN_REVIEW, location="Preamble|1",
            original_text=LANG_ORIGINAL, replacement_text="",
            reason="Grammar fix would rephrase a faculty sentence", confidence=0.6,
            requires_human_review=True,
        ),
    ]
    applied = [
        AppliedCorrection(
            correction_id="COR-1", issue_id="CLM-1", category="factual",
            location="Fundamental Rights|3", original_text=CLAIM_TEXT,
            replacement_text="Article 21A provides free education for children aged 6-14.",
            action=AUTO_CORRECT, evidence_ids=["EV-1"], confidence=0.97, applied=True,
        ),
    ]
    return QCRunResult(
        enabled=True, level="strict", final_notes="notes", notes_changed=True,
        issues=issues, claims=[claim], evidence=[evidence],
        verifications=[verification], decisions=decisions, applied=applied,
        stages=[
            StageStatus(name="ai_review", status="completed", issue_count=2),
            StageStatus(name="source_verification", status="completed", issue_count=1),
        ],
        human_review_items=1,
    )


def render_full() -> str:
    return render_error_fixed_report_md(
        make_result(),
        run_id="RUN-42", filename="lecture.pdf", chapter_title="Fundamental Rights",
        subject="Polity", language="English", note_type="detailed",
        quality_level="strict", model="test-model", transcript_source="whisper",
        transcript_gaps=[], enabled_checks={"factual": True, "language": True},
        enrichments={"pyqs": 4},
    )


def table_columns(line: str) -> int:
    """Column count of a markdown table row, honouring escaped pipes."""
    return len(re.split(r"(?<!\\)\|", line)) - 2


def test_full_render_has_all_sections():
    report = render_full()
    for section in (
        "# Error Fixed Report",
        "## Correction summary",
        "## Detailed entries",
        "### ERR-001 — Factual error",
        "### ERR-002 — Language issue",
        "## Human-check list",
        "## Transcript coverage",
        "## Check stage status",
    ):
        assert section in report, section


def test_run_summary_omits_empty_and_flattens_dicts():
    report = render_full()
    assert "| Run ID | RUN-42 |" in report
    assert "factual: on; language: on" in report
    assert "pyqs: 4" in report
    # youtube/other empty kwargs never rendered as empty rows
    assert "|  |" not in report


def test_evidence_passage_and_metadata_appear():
    report = render_full()
    assert "Article 21A provides free and compulsory education" in report
    assert "Indian Polity (M. Laxmikanth)" in report
    assert "published 2023" in report
    assert "p. 145" in report
    # source location comes from the claim
    assert "slide 12, transcript 00:14:32" in report
    # the language issue has no evidence
    assert "| Approved evidence | none |" in report


def test_applied_correction_and_confidence_rendered():
    report = render_full()
    err1 = report.split("### ERR-001", 1)[1].split("### ERR-002", 1)[0]
    assert "| Correction applied | Yes |" in err1
    assert "| Confidence | 97% |" in err1
    assert "| Human review | Not required |" in err1
    err2 = report.split("### ERR-002", 1)[1].split("## Human-check list", 1)[0]
    assert "| Human review | Required |" in err2
    assert "| Correction applied | No — held for human review |" in err2


def test_human_check_lists_only_review_items():
    report = render_full()
    section = report.split("## Human-check list", 1)[1].split("## Transcript coverage", 1)[0]
    assert "Subject-verb agreement" in section or "Grammar fix" in section
    # the auto-corrected claim must NOT be listed for human review
    assert "Article 21A provides free and compulsory education" not in section
    assert "Preamble -> para 1" in section


def test_failed_apply_lands_in_summary_and_human_check():
    result = make_result()
    result.applied[0].applied = False
    result.applied[0].failure_reason = "anchor text not found"
    report = render_error_fixed_report_md(result)
    assert "| Failed to apply | 1 |" in report
    assert "| Auto-corrected (applied to the notes) | 0 |" in report
    section = report.split("## Human-check list", 1)[1].split("## Transcript coverage", 1)[0]
    assert "anchor text not found" in section
    assert "Apply the approved correction manually" in section
    err1 = report.split("### ERR-001", 1)[1].split("### ERR-002", 1)[0]
    assert "| Correction applied | No — anchor text not found |" in err1


def test_pipes_do_not_break_tables():
    report = render_full()
    # the raw claim text contains " | " which must be escaped everywhere
    assert "free education \\| for all children" in report
    err1 = report.split("### ERR-001", 1)[1].split("### ERR-002", 1)[0]
    for line in err1.splitlines():
        if line.startswith("|"):
            assert table_columns(line) == 2, line
    section = report.split("## Human-check list", 1)[1].split("## Transcript coverage", 1)[0]
    for line in section.splitlines():
        if line.startswith("|"):
            assert table_columns(line) == 6, line


def test_disabled_qc_is_honest_not_error_free():
    result = QCRunResult(enabled=False, level="off", final_notes="notes")
    report = render_error_fixed_report_md(result, run_id="RUN-1")
    assert "OFF" in report
    assert "error-free" in report
    assert "## Correction summary" not in report
    assert "No issues were found" not in report


def test_no_issues_distinguishes_stage_statuses():
    result = QCRunResult(
        enabled=True, level="basic", final_notes="notes",
        stages=[
            StageStatus(name="ai_review", status="completed"),
            StageStatus(name="source_verification", status="disabled"),
            StageStatus(name="visual_check", status="unavailable"),
            StageStatus(name="apply_corrections", status="failed"),
        ],
    )
    report = render_error_fixed_report_md(result, transcript_source="whisper")
    assert "No issues were found by the enabled checks" in report

    def status_cell(stage: str) -> str:
        row = next(l for l in report.splitlines() if l.startswith(f"| {stage} |"))
        return row.split(" | ")[1]

    cells = {s: status_cell(s) for s in (
        "ai_review", "source_verification", "visual_check", "apply_corrections",
    )}
    assert cells["ai_review"] == "Completed"
    assert "Disabled" in cells["source_verification"]
    assert "Unavailable" in cells["visual_check"]
    assert cells["apply_corrections"] == "Failed"
    # the four outcomes must be visibly different strings
    assert len(set(cells.values())) == 4


def test_transcript_gaps_marked_not_checked():
    report = render_error_fixed_report_md(
        make_result(), transcript_source="whisper",
        transcript_gaps=["00:10:00-00:12:30", "00:41:05-00:44:00"],
    )
    section = report.split("## Transcript coverage", 1)[1]
    assert "00:10:00-00:12:30 — NOT checked" in section
    assert "00:41:05-00:44:00 — NOT checked" in section
    assert "must not be" in section and "error-free" in section
    assert "full lecture transcript was available" not in section


def test_transcript_full_coverage_vs_slides_only():
    covered = render_error_fixed_report_md(make_result(), transcript_source="whisper")
    assert "full lecture transcript was available" in covered
    slides_only = render_error_fixed_report_md(make_result(), transcript_source="")
    assert "No lecture transcript was available" in slides_only
    assert "not covered by this report" in slides_only


def test_source_verification_unavailable_warning():
    report = render_error_fixed_report_md(make_result(), source_verification_available=False)
    assert "Source verification was unavailable" in report


def test_quality_report_meta_flat_and_typed():
    meta = qc_quality_report_meta(make_result(), "1.2", "3.4", "5.6")
    assert meta["QC enabled"] == "yes"
    assert meta["QC rules version"] == "1.2"
    assert meta["QC sources version"] == "3.4"
    assert meta["QC prompt version"] == "5.6"
    assert meta["QC issues found"] == 2
    assert meta["Claims extracted"] == 1
    assert meta["Claims contradicted"] == 1
    assert meta["Corrections applied"] == 1
    assert meta["Notes changed by QC"] == "yes"
    assert meta["QC stage: ai_review"] == "Completed"
    for key, value in meta.items():
        assert isinstance(key, str)
        assert isinstance(value, (str, int)), (key, value)
