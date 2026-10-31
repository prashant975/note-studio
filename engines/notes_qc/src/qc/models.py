"""Data models for the QC and source-verification stages.

These are the contracts between every qc module. Kept separate from the
top-level src/models.py so the main pipeline models stay small; PipelineResult
carries only summary data and report paths, never these internals.

All models serialise via dataclasses.asdict() into the run's JSON artifacts,
so every field must be JSON-representable (str/int/float/bool/None/list/dict).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Verification statuses (spec §14). String constants, not an Enum, so the
# JSON artifacts read naturally and comparisons survive serialisation.
# --------------------------------------------------------------------------
VERIFIED = "VERIFIED"
CONTRADICTED = "CONTRADICTED"
OUTDATED = "OUTDATED"
INCOMPLETE_CONTEXT = "INCOMPLETE_CONTEXT"
SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
AMBIGUOUS = "AMBIGUOUS"
NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"

VERIFICATION_STATUSES = {
    VERIFIED, CONTRADICTED, OUTDATED, INCOMPLETE_CONTEXT,
    SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE, AMBIGUOUS, NEEDS_HUMAN_REVIEW,
}

# Correction actions (spec §18).
AUTO_CORRECT = "AUTO_CORRECT"
HUMAN_REVIEW = "HUMAN_REVIEW"
UNVERIFIED = "UNVERIFIED"
NO_CHANGE = "NO_CHANGE"
REMOVE_ONLY_AFTER_APPROVAL = "REMOVE_ONLY_AFTER_APPROVAL"

CORRECTION_ACTIONS = {
    AUTO_CORRECT, HUMAN_REVIEW, UNVERIFIED, NO_CHANGE, REMOVE_ONLY_AFTER_APPROVAL,
}

# Confidence at or above which a supported correction may be applied
# automatically (spec §18). Never lower this at a call site.
AUTO_CORRECT_MIN_CONFIDENCE = 0.95


@dataclass
class QCIssue:
    """One issue found by the deterministic checks or the independent AI review."""

    issue_id: str
    category: str               # factual | conceptual | language | compliance | formatting
    rule_id: str                # e.g. FACT-LEGAL-001 (see config/upsc_qc_rules.yaml)
    severity: str               # high | medium | low
    problem: str
    heading: str = ""
    paragraph_index: int | None = None
    original_text: str = ""
    suggested_text: str = ""
    requires_external_verification: bool = False
    confidence: float = 0.0
    source: str = "ai_review"   # ai_review | deterministic


@dataclass
class FactualClaim:
    """A high-risk factual claim extracted from the notes (spec §10)."""

    claim_id: str
    text: str
    claim_type: str
    subject: str
    risk_level: str = "high"
    heading: str | None = None
    paragraph_index: int | None = None
    source_slide: int | None = None
    transcript_timestamp: str | None = None
    date_sensitive: bool = False
    requires_period: bool = False
    entities: list[str] = field(default_factory=list)
    # Time-sensitivity context (spec §15); filled by the verifier when known.
    data_period: str = ""


@dataclass
class EvidenceChunk:
    """One passage returned by the local reference index."""

    source_id: str
    source_name: str
    edition: str = ""
    publication_year: str = ""
    page_number: int | None = None
    chapter: str = ""
    text: str = ""
    score: float = 0.0


@dataclass
class Evidence:
    """One piece of retrieved evidence tied to a claim (spec §13).

    Never contains tokens, session passes, or auth headers — evidence logs are
    written verbatim into the run folder.
    """

    evidence_id: str
    claim_id: str
    source_type: str            # book | official_domain | official_publication | cache
    source_name: str
    passage: str
    source_domain: str | None = None
    source_url: str | None = None
    publication_date: str | None = None
    edition: str | None = None
    page_number: int | None = None
    heading: str | None = None
    retrieved_at: str = ""
    relevance_score: float = 0.0
    approved_source: bool = True


@dataclass
class ClaimVerification:
    """Outcome of checking one claim against its evidence (spec §14)."""

    claim_id: str
    original_claim: str
    status: str                 # one of VERIFICATION_STATUSES
    explanation: str
    suggested_text: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    auto_correct_allowed: bool = False
    requires_human_review: bool = False
    # Time-sensitive context (spec §15).
    data_period: str = ""
    publication_date: str = ""
    is_latest: bool | None = None
    is_provisional: bool | None = None
    is_revised: bool | None = None


@dataclass
class CorrectionDecision:
    """What the correction policy decided for one issue/claim (spec §18)."""

    correction_id: str
    issue_id: str               # QCIssue.issue_id or FactualClaim.claim_id
    category: str
    action: str                 # one of CORRECTION_ACTIONS
    location: str               # "heading|paragraph_index" anchor description
    original_text: str
    replacement_text: str
    reason: str
    confidence: float
    evidence_ids: list[str] = field(default_factory=list)
    requires_human_review: bool = False


@dataclass
class AppliedCorrection:
    """Audit record of one attempted edit (spec §19)."""

    correction_id: str
    issue_id: str
    category: str
    location: str
    original_text: str
    replacement_text: str
    action: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    applied: bool = False
    # True when the text already read as the replacement (a re-run over notes
    # corrected earlier), so nothing was spliced this time. Still applied=True,
    # because the correction *is* present in the notes.
    already_applied: bool = False
    failure_reason: str | None = None


@dataclass
class StageStatus:
    """Honest per-stage outcome so reports can distinguish 'ran clean' from
    'disabled' from 'failed' from 'unavailable' (spec §24, §32)."""

    name: str
    status: str = "not_run"     # completed | completed_with_warnings | failed | disabled | unavailable | not_run
    detail: str = ""
    issue_count: int = 0


@dataclass
class QCRunResult:
    """Everything the QC engine hands back to the pipeline."""

    enabled: bool
    level: str                                  # off | basic | strict
    final_notes: str                            # corrected text (== input when nothing applied)
    notes_changed: bool = False
    issues: list[QCIssue] = field(default_factory=list)
    claims: list[FactualClaim] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    verifications: list[ClaimVerification] = field(default_factory=list)
    decisions: list[CorrectionDecision] = field(default_factory=list)
    applied: list[AppliedCorrection] = field(default_factory=list)
    stages: list[StageStatus] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    human_review_items: int = 0
    summary: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str) -> StageStatus:
        for s in self.stages:
            if s.name == name:
                return s
        s = StageStatus(name=name)
        self.stages.append(s)
        return s

    def to_summary(self) -> dict[str, Any]:
        """Compact dict for run_metadata.json and the UI summary cards."""
        by_status: dict[str, int] = {}
        for v in self.verifications:
            by_status[v.status] = by_status.get(v.status, 0) + 1
        return {
            "qc_enabled": self.enabled,
            "qc_level": self.level,
            "issues_found": len(self.issues),
            "claims_extracted": len(self.claims),
            "claims_verified": by_status.get(VERIFIED, 0),
            "claims_contradicted": by_status.get(CONTRADICTED, 0),
            "claims_outdated": by_status.get(OUTDATED, 0),
            "claims_source_not_found": by_status.get(SOURCE_NOT_FOUND, 0),
            "claims_source_unavailable": by_status.get(SOURCE_UNAVAILABLE, 0),
            "corrections_applied": sum(1 for a in self.applied if a.applied),
            "corrections_failed": sum(1 for a in self.applied if not a.applied and a.action == AUTO_CORRECT),
            "human_review_items": self.human_review_items,
            "notes_changed": self.notes_changed,
            "stages": {s.name: s.status for s in self.stages},
            "warnings": len(self.warnings),
        }


def to_jsonable(obj: Any) -> Any:
    """Dataclass (or list of them) -> plain JSON-ready structures."""
    if isinstance(obj, list):
        return [to_jsonable(o) for o in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj
