"""Decide the fate of every QC finding: fix automatically, flag, or leave alone.

This is deliberately the only place that knows when an automatic edit is
permitted. Finding problems (reviewer/verifier) and applying edits
(correction_applier) stay mechanical; every judgement call — the confidence
threshold, the evidence requirement, "never delete faculty content
automatically" — lives here so the whole policy can be audited in one file.

Guiding principles (spec §18):
- A claim whose source could not be found or reached is UNVERIFIED, never
  treated as false.
- Deleting faculty content is never decided by software alone; the strongest
  automatic outcome for a deletion is REMOVE_ONLY_AFTER_APPROVAL.
- Grammar-only fixes ("language" category) need no external evidence, but must
  carry both the original and corrected text so the applier can anchor them.
- Factual fixes need supporting evidence: an unverified AI-review issue that
  demands external verification is queued for humans, not auto-applied.
"""
from __future__ import annotations

from .models import (
    AMBIGUOUS,
    AUTO_CORRECT,
    AUTO_CORRECT_MIN_CONFIDENCE,
    CONTRADICTED,
    ClaimVerification,
    CorrectionDecision,
    FactualClaim,
    HUMAN_REVIEW,
    INCOMPLETE_CONTEXT,
    NEEDS_HUMAN_REVIEW,
    NO_CHANGE,
    OUTDATED,
    QCIssue,
    REMOVE_ONLY_AFTER_APPROVAL,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
    UNVERIFIED,
    VERIFIED,
)
from .qc_config import load_rules


def _location(heading: str | None, paragraph_index: int | None) -> str:
    """Build the "heading|para:N" anchor the applier parses back."""
    para = str(paragraph_index) if paragraph_index is not None else "?"
    return f"{heading or '?'}|para:{para}"


def decide_corrections(
    issues: list[QCIssue],
    verifications: list[ClaimVerification],
    claims: list[FactualClaim],
    *,
    auto_correct_enabled: bool = True,
    rules=None,
) -> list[CorrectionDecision]:
    """One CorrectionDecision per issue and per verification.

    `claims` only supplies the location anchor for verification decisions
    (ClaimVerification carries no heading/paragraph itself). `rules` defaults
    to the production rule set; a malformed config raises QCConfigError here
    on purpose — config errors must fail the stage loudly, not degrade it.
    """
    if rules is None:
        rules = load_rules()
    claim_map = {c.claim_id: c for c in claims}
    decisions: list[CorrectionDecision] = []

    def add(**kwargs) -> None:
        decisions.append(CorrectionDecision(
            correction_id=f"corr-{len(decisions) + 1:04d}", **kwargs,
        ))

    for issue in issues:
        add(**_decide_issue(issue, rules, auto_correct_enabled))
    for verification in verifications:
        claim = claim_map.get(verification.claim_id)
        add(**_decide_verification(verification, claim, auto_correct_enabled))
    return decisions


def _decide_issue(issue: QCIssue, rules, auto_correct_enabled: bool) -> dict:
    original = (issue.original_text or "").strip()
    suggested = (issue.suggested_text or "").strip()
    rule = rules.rules.get(issue.rule_id) if rules is not None else None
    # Unknown rule ids are treated conservatively: no automatic edit.
    rule_allows = bool(rule and rule.auto_correct_allowed)
    is_language = issue.category == "language"
    confident = issue.confidence >= AUTO_CORRECT_MIN_CONFIDENCE

    # The config's external_verification flag is authoritative, not the model's
    # self-report. A reviewer asked to set requires_external_verification=true
    # for legal/statistical claims may simply not do it, and a review issue
    # carries no evidence_ids at all — so honouring only the model's flag let
    # constitutional "fixes" asserted from memory rewrite student notes while
    # the report labelled them evidence-backed. Anything the rule set marks as
    # needing an external source goes to verification or to a human.
    needs_external = bool(rule and rule.external_verification) or issue.requires_external_verification

    base = dict(
        issue_id=issue.issue_id,
        category=issue.category,
        location=_location(issue.heading, issue.paragraph_index),
        original_text=original,
        replacement_text=suggested,
        confidence=issue.confidence,
        evidence_ids=[],
    )

    # A confident fix whose replacement is empty would delete faculty content.
    # Software never decides removals — queue for explicit approval.
    if confident and original and not suggested and (rule_allows or is_language):
        return base | dict(
            action=REMOVE_ONLY_AFTER_APPROVAL,
            requires_human_review=True,
            reason="Proposed fix deletes faculty content; removal requires human approval.",
        )

    if (
        auto_correct_enabled
        and confident
        and original
        and suggested
        and original != suggested
        and (rule_allows or is_language)
        and not needs_external
    ):
        return base | dict(
            action=AUTO_CORRECT,
            requires_human_review=False,
            reason=(
                "Meets auto-correct policy: confidence "
                f"{issue.confidence:.2f} >= {AUTO_CORRECT_MIN_CONFIDENCE}, "
                + ("grammar-only fix preserving meaning."
                   if is_language else f"rule {issue.rule_id} allows auto-correct.")
            ),
        )

    return base | dict(
        action=HUMAN_REVIEW,
        requires_human_review=True,
        reason=_issue_review_reason(
            issue, confident=confident, has_texts=bool(original and suggested),
            gate_open=(rule_allows or is_language),
            auto_correct_enabled=auto_correct_enabled,
            needs_external=needs_external,
        ),
    )


def _issue_review_reason(issue: QCIssue, *, confident: bool, has_texts: bool,
                         gate_open: bool, auto_correct_enabled: bool,
                         needs_external: bool) -> str:
    if not auto_correct_enabled:
        return "Automatic corrections are disabled for this run."
    if needs_external:
        return (f"Rule {issue.rule_id} requires verification against an approved "
                "source; a review finding alone carries no evidence, so this is "
                "not auto-applied.")
    if not confident:
        return (f"Confidence {issue.confidence:.2f} is below the "
                f"{AUTO_CORRECT_MIN_CONFIDENCE} auto-correct threshold.")
    if not has_texts:
        return "Issue does not provide both the original and a replacement text."
    if not gate_open:
        return f"Rule {issue.rule_id} does not permit automatic correction."
    return "Original and replacement text are identical; nothing to apply."


def _decide_verification(v: ClaimVerification, claim: FactualClaim | None,
                         auto_correct_enabled: bool) -> dict:
    original = (v.original_claim or "").strip()
    suggested = (v.suggested_text or "").strip()
    base = dict(
        issue_id=v.claim_id,
        category="factual",
        location=_location(
            claim.heading if claim else None,
            claim.paragraph_index if claim else None,
        ),
        original_text=original,
        replacement_text=suggested,
        confidence=v.confidence,
        evidence_ids=list(v.evidence_ids),
    )

    if v.status == VERIFIED:
        return base | dict(
            action=NO_CHANGE, requires_human_review=False, replacement_text="",
            reason="Verified against approved sources; no change needed.",
        )

    if v.status in (SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE):
        # Absence of a source proves nothing about the claim — never a "false".
        # It does still need a human: the claim ships unverified, and the
        # verifier already flagged it, so agreeing with that flag keeps the
        # detailed entry and the human-check list from contradicting each other.
        return base | dict(
            action=UNVERIFIED, requires_human_review=True, replacement_text="",
            reason=(f"{v.status}: no approved source could confirm or deny this "
                    "claim; left unchanged, not treated as false. Verify manually "
                    "before release."),
        )

    if v.status in (AMBIGUOUS, INCOMPLETE_CONTEXT, NEEDS_HUMAN_REVIEW):
        return base | dict(
            action=HUMAN_REVIEW, requires_human_review=True,
            reason=f"{v.status}: {v.explanation or 'verifier could not decide.'}",
        )

    if v.status in (CONTRADICTED, OUTDATED):
        confident = v.confidence >= AUTO_CORRECT_MIN_CONFIDENCE
        evidenced = bool(v.evidence_ids)

        if confident and evidenced and original and not suggested:
            return base | dict(
                action=REMOVE_ONLY_AFTER_APPROVAL, requires_human_review=True,
                reason=(f"{v.status} with evidence but no replacement text: the "
                        "only fix is removal, which requires human approval."),
            )

        if (
            auto_correct_enabled
            and confident
            and evidenced
            and original
            and suggested
            and original != suggested
            and v.auto_correct_allowed
            and not v.requires_human_review
        ):
            return base | dict(
                action=AUTO_CORRECT, requires_human_review=False,
                reason=(f"{v.status} with confidence {v.confidence:.2f} >= "
                        f"{AUTO_CORRECT_MIN_CONFIDENCE} and "
                        f"{len(v.evidence_ids)} evidence passage(s)."),
            )

        return base | dict(
            action=HUMAN_REVIEW, requires_human_review=True,
            reason=_verification_review_reason(
                v, confident=confident, evidenced=evidenced,
                auto_correct_enabled=auto_correct_enabled,
            ),
        )

    # Unknown status: never crash the pipeline, never guess an edit.
    return base | dict(
        action=HUMAN_REVIEW, requires_human_review=True,
        reason=f"Unrecognised verification status {v.status!r}; queued for review.",
    )


def _verification_review_reason(v: ClaimVerification, *, confident: bool,
                                evidenced: bool, auto_correct_enabled: bool) -> str:
    if not auto_correct_enabled:
        return "Automatic corrections are disabled for this run."
    if v.requires_human_review:
        return "Verifier explicitly requested human review."
    if not evidenced:
        return f"{v.status} without supporting evidence passages."
    if not confident:
        return (f"Confidence {v.confidence:.2f} is below the "
                f"{AUTO_CORRECT_MIN_CONFIDENCE} auto-correct threshold.")
    if not v.auto_correct_allowed:
        return "Verifier did not mark this correction as safe to auto-apply."
    return "Original claim and suggested text do not form an applicable edit."
