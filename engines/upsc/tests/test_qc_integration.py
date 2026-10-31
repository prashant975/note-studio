"""Integration test for the QC engine (spec §34, Polity fixture).

Drives src/qc/engine.run_qc end-to-end with a fake Gemini client and a fake
proxy gate — no network, no tokens. The fixture note contains:
  - a wrong Article attribution (the AI review flags it with a high-confidence
    correction -> auto-applied),
  - a correct constitutional fact (left unchanged),
  - an unverifiable statistic (no evidence -> never auto-corrected),
  - a spelling inconsistency (deterministic check).
"""
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pw_access
from src.qc import engine
from src.qc.models import (
    AUTO_CORRECT,
    HUMAN_REVIEW,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
)

NOTES = """Topics Covered in the Class:
• Fundamental Rights

Fundamental Rights
• Right to Equality is guaranteed under Articles 14 to 18.
• The Right to Constitutional Remedies is under Article 226.
• Dr. Ambedkar called it the heart and soul of the Constitution.

Directive Principles
• DPSPs are contained in Part IV of the Constitution.
• About 62% of citizens know their rights, as per a survey.

Emergency Provisions
• Ambedker chaired the drafting committee.
"""

REVIEW_JSON = json.dumps({
    "summary": {"status": "completed", "total_issues": 1,
                "factual_candidates": 1, "conceptual_issues": 0, "language_issues": 0},
    "issues": [{
        "issue_id": "QC-001", "category": "factual", "rule_id": "FACT-LEGAL-001",
        "severity": "high", "heading": "Fundamental Rights", "paragraph_index": 1,
        "original_text": "The Right to Constitutional Remedies is under Article 226.",
        "problem": "Article 32 is the Right to Constitutional Remedies; Article 226 is the High Courts' writ jurisdiction.",
        "suggested_text": "The Right to Constitutional Remedies is under Article 32.",
        "requires_external_verification": False, "confidence": 0.98,
    }],
    "claims": [{
        "text": "About 62% of citizens know their rights, as per a survey.",
        "claim_type": "economic_statistic", "heading": "Directive Principles",
        "risk_level": "high", "date_sensitive": True, "requires_period": True,
        "entities": ["62%"],
    }],
})


class FakeResp:
    def __init__(self, text):
        self.text = text
        self.model = "fake-model"


class FakeClient:
    """Answers each QC prompt type with canned, well-formed JSON."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, image_paths=None, **kw):
        self.calls.append(prompt[:80])
        if "independent quality reviewer" in prompt:
            return FakeResp(REVIEW_JSON)
        if "rechecking corrections" in prompt:
            return FakeResp(json.dumps({"status": "passed", "problems": []}))
        # Verifier batch: return SOURCE-agnostic NEEDS_HUMAN_REVIEW-ish results
        claim_ids = sorted(set(re.findall(r"CLM-\d+", prompt)))
        return FakeResp(json.dumps({"results": [
            {"claim_id": cid, "status": "SOURCE_NOT_FOUND",
             "explanation": "no approved source found", "suggested_text": None,
             "confidence": 0.0, "evidence_ids": []} for cid in claim_ids
        ]}))


def _fake_verify_claims(claims, sources, **kwargs):
    """Hermetic stand-in for the wave-2 verifier: every claim honestly ends
    SOURCE_NOT_FOUND (its own behaviour is unit-tested offline elsewhere)."""
    from src.qc.models import ClaimVerification

    return ([ClaimVerification(
        claim_id=c.claim_id, original_claim=c.text, status=SOURCE_NOT_FOUND,
        explanation="no approved source found", requires_human_review=True,
    ) for c in claims], [], [{"note": "stubbed in test"}])


def _run(tmp_path, monkeypatch, level="strict", client=None):
    monkeypatch.setattr(pw_access, "check_allowed", lambda _t: True)
    # Engine lazy-imports verify_claims at call time, so patching the module
    # attribute keeps the whole test offline regardless of import style.
    import src.qc.claim_verifier as cv
    monkeypatch.setattr(cv, "verify_claims", _fake_verify_claims)
    return engine.run_qc(
        notes_text=NOTES, run_dir=tmp_path, client=client,
        google_token="tok", slides=[], transcript_text="", transcript_gaps=[],
        subject="polity", language_code="en", note_type="complete",
        quality_level=level, quality_options={"check_facts": True},
        qc_options={"verify_sources": True, "auto_correct": True},
    )


def test_legal_issue_without_evidence_is_not_auto_applied(tmp_path, monkeypatch):
    """A legal/constitutional fix asserted from model memory must not edit notes.

    The fixture's issue is FACT-LEGAL-001, whose rule sets
    external_verification: true. The reviewer is confident (0.98) and even
    reports requires_external_verification=False, but no approved evidence
    backs the change — so policy routes it to a human instead of rewriting
    what the faculty taught.
    """
    client = FakeClient()
    result = _run(tmp_path, monkeypatch, client=client)
    assert "under Article 226" in result.final_notes, "notes must be left as taught"
    assert not result.notes_changed
    assert not [a for a in result.applied if a.applied]

    decision = next(d for d in result.decisions if d.issue_id == "QC-001")
    assert decision.action == HUMAN_REVIEW
    assert decision.requires_human_review
    assert "approved source" in decision.reason
    assert result.human_review_items >= 1


def test_language_issue_is_auto_applied(tmp_path, monkeypatch):
    """The auto-correct path still works where no external source is needed:
    a language-category fix carries its own evidence (the text itself)."""
    review = json.loads(REVIEW_JSON)
    review["issues"] = [{
        "issue_id": "QC-100", "category": "language", "rule_id": "LANG-SPELLING-001",
        "severity": "medium", "heading": "Emergency Provisions", "paragraph_index": 3,
        "original_text": "Ambedker chaired the drafting committee.",
        "problem": "Name misspelt; the note spells it 'Ambedkar' elsewhere.",
        "suggested_text": "Ambedkar chaired the drafting committee.",
        "requires_external_verification": False, "confidence": 0.99,
    }]
    review["claims"] = []
    payload = json.dumps(review)

    class LanguageClient(FakeClient):
        def generate(self, prompt, image_paths=None, **kw):
            self.calls.append(prompt[:80])
            if "independent quality reviewer" in prompt:
                return FakeResp(payload)
            return super().generate(prompt, image_paths=image_paths, **kw)

    result = _run(tmp_path, monkeypatch, client=LanguageClient())
    assert "Ambedkar chaired" in result.final_notes
    assert "Ambedker" not in result.final_notes
    assert result.notes_changed
    applied = [a for a in result.applied if a.applied]
    assert len(applied) == 1 and applied[0].action == AUTO_CORRECT
    # The correct fact and the unverifiable statistic were untouched.
    assert "Articles 14 to 18" in result.final_notes
    assert "62%" in result.final_notes


def test_unverifiable_claim_is_flagged_not_corrected(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, client=FakeClient())
    stat_claims = [v for v in result.verifications
                   if "62%" in v.original_claim]
    assert stat_claims, "the statistic must be extracted as a claim"
    assert all(v.status in (SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE) for v in stat_claims)
    assert all(not v.auto_correct_allowed for v in stat_claims)
    assert result.human_review_items >= 1


def test_artifacts_written(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, client=FakeClient())
    for name in ("deterministic_qc.json", "qc_prompt.txt", "qc_response_raw.txt",
                 "factual_claims.json", "source_verification.json",
                 "correction_decisions.json", "corrections_applied.json",
                 "qc_result.json"):
        assert (tmp_path / name).exists(), f"missing artifact: {name}"


def test_mock_mode_never_calls_ai(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, client=None)
    assert result.stage("ai_review").status == "unavailable"
    # Deterministic checks still ran and the notes are untouched.
    assert result.final_notes == NOTES
    assert not result.notes_changed


def test_denied_gate_fails_closed(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(pw_access, "check_allowed", lambda _t: False)
    result = engine.run_qc(
        notes_text=NOTES, run_dir=tmp_path, client=client,
        google_token="tok", slides=[], transcript_text="", transcript_gaps=[],
        subject="polity", language_code="en", note_type="complete",
        quality_level="strict", quality_options={}, qc_options={},
    )
    assert result.stage("ai_review").status == "unavailable"
    assert client.calls == [], "no paid call may happen after a denied gate"
    assert result.final_notes == NOTES


def test_off_level_reports_disabled(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, level="off", client=FakeClient())
    assert not result.enabled
    assert result.stage("ai_review").status == "disabled"
    assert any("disabled" in w.lower() for w in result.warnings)
