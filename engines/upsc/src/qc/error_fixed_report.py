"""Render the Error Fixed Report: what the QC stage found, verified and fixed.

This report is DIFFERENT from src/error_report.py. That one documents faculty
teaching errors parsed out of inline [Faculty Point Corrected] blocks left by
the note-generation AI. This one documents the *independent QC stage* (spec
§23): every issue the deterministic checks and the independent AI review
raised, every high-risk claim verified against approved sources, the evidence
behind each decision, and exactly what was (or could not be) changed in the
notes. A reviewer must be able to audit any single correction from this file
alone — which is why each entry carries its evidence passages verbatim.

Honesty rules baked in here rather than left to callers (spec §24, §32):
- QC switched off never renders as "no errors" — it renders as "not checked".
- Untranscribed minutes are listed as NOT checked, never implied error-free.
- Stage statuses keep "completed", "disabled", "failed" and "unavailable" as
  visibly different strings, because "we found nothing" and "we never looked"
  must never collapse into the same sentence.

All table cell text passes through _cell() so note text containing pipes or
newlines cannot break the markdown tables (same approach as
src/quality_checker.py, reimplemented locally to avoid importing a private
helper across packages).
"""
from __future__ import annotations

import re
from typing import Any

from .models import (
    AUTO_CORRECT,
    HUMAN_REVIEW,
    NO_CHANGE,
    OUTDATED,
    REMOVE_ONLY_AFTER_APPROVAL,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
    UNVERIFIED,
    AppliedCorrection,
    CorrectionDecision,
    Evidence,
    QCRunResult,
)

# Human display strings. Kept distinct on purpose: a stage that was disabled
# by configuration must never read like one that ran and found nothing.
_STAGE_DISPLAY = {
    "completed": "Completed",
    "completed_with_warnings": "Completed with warnings",
    "failed": "Failed",
    "disabled": "Disabled — switched off for this run",
    "unavailable": "Unavailable — required resource missing",
    "not_run": "Not run",
}

_CATEGORY_DISPLAY = {
    "factual": "Factual error",
    "conceptual": "Conceptual issue",
    "language": "Language issue",
    "formatting": "Formatting issue",
    "compliance": "Compliance issue",
}

_ACTION_DISPLAY = {
    AUTO_CORRECT: "Auto-correct (evidence-backed)",
    HUMAN_REVIEW: "Human review required",
    UNVERIFIED: "Unverified — left as taught",
    NO_CHANGE: "No change needed",
    REMOVE_ONLY_AFTER_APPROVAL: "Remove only after human approval",
}

_REVIEW_ACTIONS = {HUMAN_REVIEW, REMOVE_ONLY_AFTER_APPROVAL}
_HUMAN_LIST_ACTIONS = {HUMAN_REVIEW, UNVERIFIED, REMOVE_ONLY_AFTER_APPROVAL}

_RECOMMENDED_ACTION = {
    HUMAN_REVIEW: "Verify against the cited evidence and edit the note manually if needed.",
    UNVERIFIED: "No approved source confirmed this point — verify it manually before release.",
    REMOVE_ONLY_AFTER_APPROVAL: "Confirm the point is wrong, then approve or reject the removal.",
}


def _cell(value: object) -> str:
    """Flatten a value so it cannot break out of a markdown table cell."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = re.sub(r"\s+", " ", text).strip()
    return text or "-"


def _pct(confidence: float) -> str:
    try:
        return f"{round(float(confidence) * 100)}%"
    except (TypeError, ValueError):
        return "-"


def _flatten(d: dict | None) -> str:
    """Compact "key: value; key: value" line for the run summary table."""
    if not d:
        return ""
    parts = []
    for k, v in d.items():
        if isinstance(v, bool):
            v = "on" if v else "off"
        parts.append(f"{k}: {v}")
    return "; ".join(parts)


def _evidence_line(ev: Evidence, with_passage: bool) -> str:
    """One human-readable citation. The passage is included in the detailed
    entries (an auditor must see what the source actually says) but not in
    the human-check one-liners."""
    bits = [ev.source_type or "source"]
    if ev.source_domain:
        bits.append(ev.source_domain)
    elif ev.edition:
        bits.append(f"edition {ev.edition}")
    if ev.publication_date:
        bits.append(f"published {ev.publication_date}")
    if ev.page_number is not None:
        bits.append(f"p. {ev.page_number}")
    line = f"{ev.source_name} ({', '.join(bits)})"
    if with_passage and ev.passage:
        line += f': "{ev.passage}"'
    return line


def _location(heading: str | None, paragraph_index: int | None, fallback: str) -> str:
    if heading and paragraph_index is not None:
        return f"{heading} -> para {paragraph_index}"
    if heading:
        return heading
    if paragraph_index is not None:
        return f"para {paragraph_index}"
    return fallback or "not recorded"


def _stage_table(qc_result: QCRunResult) -> list[str]:
    if not qc_result.stages:
        return ["No stage statuses were recorded for this run.", ""]
    lines = [
        "| Stage | Status | Issues found | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for s in qc_result.stages:
        status = _STAGE_DISPLAY.get(s.status, s.status)
        lines.append(
            "| " + " | ".join(_cell(c) for c in [s.name, status, s.issue_count, s.detail]) + " |"
        )
    lines.append("")
    return lines


class _EntryContext:
    """Pre-indexed lookups so each detailed entry resolves its issue, claim,
    verification, evidence and applied-correction record in O(1)."""

    def __init__(self, qc_result: QCRunResult) -> None:
        self.issues = {i.issue_id: i for i in qc_result.issues}
        self.claims = {c.claim_id: c for c in qc_result.claims}
        self.verifications = {v.claim_id: v for v in qc_result.verifications}
        self.evidence = {e.evidence_id: e for e in qc_result.evidence}
        self.applied = {a.correction_id: a for a in qc_result.applied}

    def evidence_for(self, decision: CorrectionDecision) -> list[str]:
        ids = list(decision.evidence_ids)
        if not ids:
            ver = self.verifications.get(decision.issue_id)
            if ver is not None:
                ids = list(ver.evidence_ids)
        return ids


def _needs_review(d: CorrectionDecision) -> bool:
    return d.requires_human_review or d.action in _REVIEW_ACTIONS


def _applied_display(d: CorrectionDecision, applied: AppliedCorrection | None) -> str:
    if applied is not None:
        if applied.applied:
            return "Yes"
        return f"No — {applied.failure_reason or 'failure reason not recorded'}"
    if d.action == NO_CHANGE:
        return "Not needed"
    if d.action in _REVIEW_ACTIONS:
        return "No — held for human review"
    if d.action == UNVERIFIED:
        return "No — claim could not be verified"
    return "No — not attempted"


def _entry_block(n: int, d: CorrectionDecision, ctx: _EntryContext) -> list[str]:
    issue = ctx.issues.get(d.issue_id)
    claim = ctx.claims.get(d.issue_id)
    ver = ctx.verifications.get(d.issue_id)

    if issue is not None:
        rule = issue.rule_id
        severity = issue.severity
    elif claim is not None:
        rule = claim.claim_type or "factual claim"
        severity = claim.risk_level
    else:
        rule, severity = "-", "-"

    heading = (issue.heading if issue else "") or (claim.heading if claim else "") or ""
    para = issue.paragraph_index if issue and issue.paragraph_index is not None else (
        claim.paragraph_index if claim else None
    )
    location = _location(heading, para, d.location)

    source_bits = []
    if claim is not None and claim.source_slide is not None:
        source_bits.append(f"slide {claim.source_slide}")
    if claim is not None and claim.transcript_timestamp:
        source_bits.append(f"transcript {claim.transcript_timestamp}")
    source_location = ", ".join(source_bits) or "not recorded"

    original = d.original_text or (issue.original_text if issue else "") or (
        claim.text if claim else ""
    )
    problem = (issue.problem if issue else "") or (ver.explanation if ver else "") or d.reason

    decision_display = _ACTION_DISPLAY.get(d.action, d.action)
    if d.reason:
        decision_display = f"{decision_display} — {d.reason}"

    category = _CATEGORY_DISPLAY.get(d.category, d.category.title() or "Uncategorised")
    lines = [
        f"### ERR-{n:03d} — {category}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Rule | {_cell(rule)} |",
        f"| Severity | {_cell(severity)} |",
        f"| Location | {_cell(location)} |",
        f"| Source location | {_cell(source_location)} |",
        f"| Original faculty point | {_cell(original)} |",
        f"| Problem | {_cell(problem)} |",
    ]

    ev_ids = ctx.evidence_for(d)
    if not ev_ids:
        lines.append("| Approved evidence | none |")
    else:
        for i, ev_id in enumerate(ev_ids, start=1):
            ev = ctx.evidence.get(ev_id)
            label = "Approved evidence" if len(ev_ids) == 1 else f"Approved evidence {i}"
            if ev is None:
                lines.append(f"| {label} | {_cell(f'(evidence {ev_id} not recorded)')} |")
            else:
                lines.append(f"| {label} | {_cell(_evidence_line(ev, with_passage=True))} |")

    lines += [
        f"| Correction decision | {_cell(decision_display)} |",
        f"| Correction applied | {_cell(_applied_display(d, ctx.applied.get(d.correction_id)))} |",
        f"| Corrected note text | {_cell(d.replacement_text or '(unchanged)')} |",
        f"| Confidence | {_pct(d.confidence)} |",
        f"| Human review | {'Required' if _needs_review(d) else 'Not required'} |",
        "",
    ]
    return lines


def _human_check_rows(qc_result: QCRunResult, ctx: _EntryContext) -> list[tuple]:
    """(location, original, reason, evidence one-liner, recommended action)
    for every item a human must look at: review-flagged decisions plus any
    auto-correction that failed to apply, deduplicated by correction id."""
    rows: list[tuple] = []
    seen: set[str] = set()

    for d in qc_result.decisions:
        applied = ctx.applied.get(d.correction_id)
        failed_apply = applied is not None and not applied.applied
        in_list = d.action in _HUMAN_LIST_ACTIONS or d.requires_human_review or failed_apply
        if not in_list:
            continue
        seen.add(d.correction_id)
        issue = ctx.issues.get(d.issue_id)
        claim = ctx.claims.get(d.issue_id)
        heading = (issue.heading if issue else "") or (claim.heading if claim else "") or ""
        para = issue.paragraph_index if issue and issue.paragraph_index is not None else (
            claim.paragraph_index if claim else None
        )
        location = _location(heading, para, d.location)
        original = d.original_text or (issue.original_text if issue else "") or (
            claim.text if claim else ""
        )
        if failed_apply:
            reason = f"Approved correction failed to apply: {applied.failure_reason or 'reason not recorded'}"
            action = "Apply the approved correction manually at the stated location."
        else:
            reason = d.reason or (issue.problem if issue else "") or "flagged by QC"
            action = _RECOMMENDED_ACTION.get(
                d.action, "Review the point and the suggested change before release."
            )
        ev_ids = ctx.evidence_for(d)
        ev_line = "; ".join(
            _evidence_line(ctx.evidence[e], with_passage=False)
            for e in ev_ids if e in ctx.evidence
        ) or "none"
        rows.append((location, original, reason, ev_line, action))

    # Failed applies whose decision is not in qc_result.decisions (defensive:
    # the engine should always record both, but a missing decision must not
    # hide a failed edit from the reviewer).
    for a in qc_result.applied:
        if a.applied or a.correction_id in seen:
            continue
        rows.append((
            a.location or "not recorded",
            a.original_text,
            f"Approved correction failed to apply: {a.failure_reason or 'reason not recorded'}",
            "none",
            "Apply the approved correction manually at the stated location.",
        ))
    return rows


def render_error_fixed_report_md(
    qc_result: QCRunResult,
    *,
    run_id: str = "",
    filename: str = "",
    chapter_title: str = "",
    subject: str = "",
    language: str = "",
    note_type: str = "",
    quality_level: str = "",
    model: str = "",
    transcript_source: str = "",
    transcript_gaps: list[str] | None = None,
    enabled_checks: dict | None = None,
    enrichments: dict | None = None,
    source_verification_available: bool = True,
) -> str:
    """Full Error Fixed Report as markdown (spec §23). Never raises on odd
    data — a report with '-' cells is more useful than a crashed pipeline."""
    transcript_gaps = list(transcript_gaps or [])
    out: list[str] = ["# Error Fixed Report", ""]

    # ---------------------------------------------------------- run summary
    summary_rows = [
        ("Run ID", run_id),
        ("File", filename),
        ("Chapter", chapter_title),
        ("Subject", subject),
        ("Language", language),
        ("Note type", note_type),
        ("Quality level", quality_level),
        ("Model", model),
        ("Transcript source", transcript_source),
        ("QC level", qc_result.level),
        ("Enabled checks", _flatten(enabled_checks)),
        ("Enrichments", _flatten(enrichments)),
    ]
    rows = [(k, v) for k, v in summary_rows if str(v or "").strip()]
    if rows:
        out += ["| Field | Value |", "| --- | --- |"]
        out += [f"| {_cell(k)} | {_cell(v)} |" for k, v in rows]
        out.append("")

    # ------------------------------------------------- honest empty: QC off
    if not qc_result.enabled or qc_result.level == "off":
        out += [
            "## Quality checks were OFF for this run",
            "",
            "QC was disabled, so nothing was checked, verified or fixed. This report "
            "is empty by configuration — it does NOT mean the notes are error-free. "
            "Re-run with the quality level set to basic or strict to check them.",
            "",
        ]
        return "\n".join(out)

    if not source_verification_available:
        out += [
            "> **Source verification was unavailable for this run.** Factual claims "
            "were not checked against approved sources; treat every factual point "
            "in the notes as unverified.",
            "",
        ]

    ctx = _EntryContext(qc_result)
    decisions = list(qc_result.decisions)

    # -------------------------------------------- honest empty: nothing found
    if not decisions and not qc_result.issues:
        out += [
            "## Result",
            "",
            "No issues were found by the enabled checks. That statement covers only "
            "the stages listed below as completed — a disabled or unavailable stage "
            "checked nothing.",
            "",
            "## Check stage status",
            "",
        ]
        out += _stage_table(qc_result)
        out += _transcript_section(transcript_gaps, transcript_source)
        return "\n".join(out)

    # -------------------------------------------------- correction summary
    by_category = {c: 0 for c in ("factual", "conceptual", "language", "formatting", "compliance")}
    for d in decisions:
        if d.category in by_category:
            by_category[d.category] += 1
    ver_status = [v.status for v in qc_result.verifications]
    counts = [
        ("Total issues with a correction decision", len(decisions)),
        ("Factual", by_category["factual"]),
        ("Conceptual", by_category["conceptual"]),
        ("Language", by_category["language"]),
        ("Formatting", by_category["formatting"]),
        ("Compliance", by_category["compliance"]),
        ("Auto-corrected (applied to the notes)", sum(1 for a in qc_result.applied if a.applied)),
        ("Needs human review", sum(1 for d in decisions if _needs_review(d))),
        ("Unverified (left as taught)", sum(1 for d in decisions if d.action == UNVERIFIED)),
        ("Outdated data", sum(1 for s in ver_status if s == OUTDATED)),
        ("Source not found", sum(1 for s in ver_status if s in (SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE))),
        ("Failed to apply", sum(1 for a in qc_result.applied if not a.applied)),
    ]
    out += ["## Correction summary", "", "| Measure | Count |", "| --- | --- |"]
    out += [f"| {_cell(k)} | {v} |" for k, v in counts]
    out.append("")

    # ------------------------------------------------------ detailed entries
    out += ["## Detailed entries", ""]
    for n, d in enumerate(decisions, start=1):
        out += _entry_block(n, d, ctx)

    # ------------------------------------------------------ human-check list
    out += ["## Human-check list", ""]
    review_rows = _human_check_rows(qc_result, ctx)
    if not review_rows:
        # "No rows" must never be reported as "nothing to check" when the stage
        # that turns findings into decisions did not run: the findings exist,
        # they were simply never dispatched.
        undispatched = [i for i in qc_result.issues] if not qc_result.decisions else []
        corrections_failed = qc_result.stage("corrections").status in ("failed", "unavailable")
        if undispatched and corrections_failed:
            out += [
                f"**The correction-decision stage did not complete, and {len(undispatched)} "
                "finding(s) from the quality review were never dispatched.** Nothing was "
                "auto-corrected and nothing below has been triaged — every finding needs a "
                "person to look at it.",
                "",
                "| # | Location | Finding | Severity |",
                "| --- | --- | --- | --- |",
            ]
            for n, issue in enumerate(undispatched, start=1):
                out.append("| " + " | ".join(_cell(c) for c in (
                    n,
                    _location(issue.heading or "", issue.paragraph_index, ""),
                    issue.problem or issue.original_text or "flagged by QC",
                    issue.severity or "unknown",
                )) + " |")
            out.append("")
        else:
            out += ["Nothing is pending a human decision for this run.", ""]
    else:
        out += [
            "These items were NOT auto-corrected (or a correction failed to apply). "
            "They must be checked by a person before the notes go out.",
            "",
            "| # | Location | Original text | Reason | Available evidence | Recommended action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for n, row in enumerate(review_rows, start=1):
            out.append("| " + " | ".join(_cell(c) for c in (n, *row)) + " |")
        out.append("")

    out += _transcript_section(transcript_gaps, transcript_source)
    out += ["## Check stage status", ""]
    out += _stage_table(qc_result)
    return "\n".join(out)


def _transcript_section(transcript_gaps: list[str], transcript_source: str) -> list[str]:
    out = ["## Transcript coverage", ""]
    if transcript_gaps:
        out += [
            f"**{len(transcript_gaps)} stretch(es) of the lecture were not transcribed.** "
            "Content taught in those minutes was NOT checked by any QC stage, and this "
            "report makes no claim about it — untranscribed sections must not be "
            "assumed to be error-free.",
            "",
        ]
        out += [f"- {gap} — NOT checked" for gap in transcript_gaps]
        out.append("")
    elif transcript_source:
        out += [
            "The full lecture transcript was available and the enabled checks covered "
            "it together with the slides.",
            "",
        ]
    else:
        out += [
            "No lecture transcript was available: only the slide content was checked. "
            "Anything said aloud by the faculty is not covered by this report.",
            "",
        ]
    return out


def qc_quality_report_meta(
    qc_result: QCRunResult,
    rules_version: str = "",
    sources_version: str = "",
    prompt_version: str = "",
) -> dict:
    """Flat, human-keyed QC counters for the existing quality report's meta
    table. Values are str/int only so the caller can drop them straight into
    its two-column table without type checks. Version args default from the
    engine's summary (run_qc records them there before any stage runs)."""
    rules_version = rules_version or str(qc_result.summary.get("qc_framework_version", ""))
    sources_version = sources_version or str(qc_result.summary.get("source_registry_version", ""))
    prompt_version = prompt_version or str(qc_result.summary.get("qc_prompt_version", ""))
    s = qc_result.to_summary()
    meta: dict[str, Any] = {
        "QC enabled": "yes" if qc_result.enabled else "no",
        "QC level": str(qc_result.level),
        "QC rules version": str(rules_version),
        "QC sources version": str(sources_version),
        "QC prompt version": str(prompt_version),
        "QC issues found": int(s["issues_found"]),
        "Claims extracted": int(s["claims_extracted"]),
        "Claims verified": int(s["claims_verified"]),
        "Claims contradicted": int(s["claims_contradicted"]),
        "Claims outdated": int(s["claims_outdated"]),
        "Claims with source not found": int(s["claims_source_not_found"]),
        "Claims with source unavailable": int(s["claims_source_unavailable"]),
        "Corrections applied": int(s["corrections_applied"]),
        "Corrections failed to apply": int(s["corrections_failed"]),
        "Human review items": int(s["human_review_items"]),
        "Notes changed by QC": "yes" if qc_result.notes_changed else "no",
        "QC warnings": int(s["warnings"]),
    }
    for st in qc_result.stages:
        meta[f"QC stage: {st.name}"] = _STAGE_DISPLAY.get(st.status, str(st.status))
    return meta
