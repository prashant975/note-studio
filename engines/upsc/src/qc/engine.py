"""QC engine — the single orchestrator for the quality-control stages.

Called exclusively from src/pipeline.py run_pipeline() between draft-notes
completion and DOCX writing. Implements the Off/Basic/Strict ladder (spec §28):

  off    -> nothing here (pipeline skips the call entirely); reports say so
  basic  -> deterministic checks + independent AI review + safe language/flow
            corrections; external verification only for flagged high-severity
            factual claims, and only against the LOCAL reference index
  strict -> everything: claim extraction, source routing, local + approved-web
            evidence, verification, safe corrections, targeted recheck
            (the visual PDF check runs later, post-export, via format_check)

Design rules enforced here:
- pw_access.check_allowed() is re-checked before the paid review call; denial
  or proxy error skips every paid stage (fail closed) but never destroys the
  already-generated notes (spec §2.2, §32).
- Every stage runs in its own try/except: a failed stage records an honest
  StageStatus and the run continues (spec §32).
- Every artifact from spec §25 is written even when its stage was skipped, so
  a run folder always documents what did and did not happen.
"""
from __future__ import annotations

import json
from pathlib import Path

import pw_access

from .models import (
    AUTO_CORRECT,
    HUMAN_REVIEW,
    NEEDS_HUMAN_REVIEW,
    REMOVE_ONLY_AFTER_APPROVAL,
    SOURCE_UNAVAILABLE,
    UNVERIFIED,
    QCRunResult,
    to_jsonable,
)
from .qc_config import QCConfigError, load_rules, load_sources


def _stage_cb(callback, label: str) -> None:
    if callback is None:
        return
    try:
        callback(label)
    except Exception:
        pass


def _write_json(run_dir: Path, name: str, payload) -> None:
    """Atomic-enough JSON artifact write: temp file then replace. Never raises —
    losing an artifact must not lose the run."""
    try:
        path = Path(run_dir) / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def run_qc(
    *,
    notes_text: str,
    run_dir: Path,
    client,                       # GeminiClient | None (None in mock mode)
    google_token: str,
    slides,
    transcript_text: str,
    transcript_gaps: list[str],
    subject: str,
    language_code: str,
    note_type: str,
    quality_level: str,
    quality_options: dict,
    qc_options: dict | None = None,
    stage_callback=None,
) -> QCRunResult:
    """Run the QC ladder for one file. Always returns a QCRunResult; the
    pipeline uses .final_notes (unchanged text when nothing applied)."""
    level = (quality_level or "strict").strip().lower()
    qc_options = qc_options or {}
    verify_external = bool(qc_options.get("verify_sources", True))
    latest_data_check = bool(qc_options.get("latest_data_check", True))
    auto_correct_enabled = bool(qc_options.get("auto_correct", True))

    result = QCRunResult(enabled=level != "off", level=level, final_notes=notes_text)

    if level == "off":
        for name in ("deterministic", "ai_review", "claim_verification", "corrections", "recheck"):
            s = result.stage(name)
            s.status = "disabled"
            s.detail = "quality level is Off"
        result.warnings.append("Quality verification was disabled for this run (level: Off).")
        _write_artifacts(run_dir, result)
        return result

    # Config is the contract for everything below — a malformed YAML fails the
    # QC stage loudly (spec §7) but not the note run itself (spec §32).
    try:
        rules = load_rules()
        registry_version = load_sources().version
    except QCConfigError as e:
        result.warnings.append(f"QC configuration invalid — QC skipped entirely: {e}")
        s = result.stage("config")
        s.status = "failed"
        s.detail = str(e)[:300]
        _write_artifacts(run_dir, result)
        return result
    result.summary["qc_framework_version"] = rules.version
    result.summary["qc_prompt_version"] = rules.prompt_version
    result.summary["source_registry_version"] = registry_version

    # ---------------------------------------------------------------- 1. deterministic
    stage = result.stage("deterministic")
    try:
        from .deterministic_checks import run_deterministic_checks

        det_issues = run_deterministic_checks(notes_text, slides=slides,
                                              enrichments=quality_options)
        result.issues.extend(det_issues)
        stage.status = "completed" if not det_issues else "completed_with_warnings"
        stage.issue_count = len(det_issues)
    except Exception as e:
        stage.status = "failed"
        stage.detail = str(e)[:300]
        result.warnings.append(f"Deterministic checks failed: {e}")
    _write_json(run_dir, "deterministic_qc.json",
                [i for i in result.issues if i.source == "deterministic"])

    # ------------------------------------------------- 2. gate before paid stages
    ai_available = client is not None
    gate_detail = ""
    if ai_available:
        try:
            if not pw_access.check_allowed(google_token):
                ai_available = False
                gate_detail = "proxy denied access before the QC review (fail closed)"
        except Exception as e:
            ai_available = False
            gate_detail = f"proxy gate errored (fail closed): {e}"
    else:
        gate_detail = "no AI client (mock mode)"
    if not ai_available and gate_detail:
        result.warnings.append(f"AI quality stages skipped: {gate_detail}")

    # ---------------------------------------------------------------- 3. AI review
    _stage_cb(stage_callback, "Reviewing concepts and language")
    stage = result.stage("ai_review")
    ai_claim_dicts: list[dict] = []
    if not ai_available:
        stage.status = "unavailable"
        stage.detail = gate_detail
    else:
        try:
            from .ai_review import run_ai_quality_review

            issues, ai_claim_dicts, _summary, _raw = run_ai_quality_review(
                client,
                notes_text=notes_text, slides=slides,
                transcript_text=transcript_text, transcript_gaps=transcript_gaps,
                subject=subject, language_code=language_code, note_type=note_type,
                quality_level=level, rules=rules, enabled_checks=quality_options,
                run_dir=run_dir,
            )
            result.issues.extend(issues)
            stage.status = "completed" if not issues else "completed_with_warnings"
            stage.issue_count = len(issues)
        except Exception as e:
            stage.status = "failed"
            stage.detail = str(e)[:300]
            result.warnings.append(
                f"AI quality review failed — its corrections were skipped: {e}")

    # ------------------------------------------------------------ 4. claim extraction
    stage = result.stage("claim_extraction")
    try:
        from .claim_extractor import extract_claim_candidates, merge_ai_claims

        candidates = extract_claim_candidates(notes_text, subject)
        result.claims = merge_ai_claims(candidates, ai_claim_dicts, subject)
        stage.status = "completed"
        stage.issue_count = len(result.claims)
    except Exception as e:
        stage.status = "failed"
        stage.detail = str(e)[:300]
        result.warnings.append(f"Claim extraction failed: {e}")
    _write_json(run_dir, "factual_claims.json", result.claims)

    # --------------------------------------------------------- 5. source verification
    _stage_cb(stage_callback, "Verifying important facts")
    stage = result.stage("claim_verification")
    queries_log: list[dict] = []
    if not result.claims:
        stage.status = "completed"
        stage.detail = "no claims to verify"
    elif not verify_external:
        # The setting means the same thing at every level. Honouring it only at
        # Strict meant an unchecked box still ran local-index verification at
        # Basic, and a CONTRADICTED verdict from it could still rewrite the
        # notes — a check the user had explicitly switched off.
        stage.status = "disabled"
        stage.detail = "source verification turned off in settings"
    elif not ai_available:
        # The verifier needs the model for semantic comparison; without it the
        # claims stay honestly unverified rather than pretending.
        for c in result.claims:
            result.verifications.append(_unavailable_verification(c, gate_detail))
        stage.status = "unavailable"
        stage.detail = gate_detail
    else:
        try:
            from .source_router import SourceRouter
            from .claim_verifier import verify_claims

            sources = SourceRouter().get_sources(subject)
            result.warnings.extend(sources.warnings)
            # Basic level: local index only, and only high-risk flagged claims
            # (spec §28 — no broad live-web verification in Basic).
            claims_to_verify = result.claims
            allow_web = level == "strict"
            if level == "basic":
                flagged = {i.original_text for i in result.issues
                           if i.requires_external_verification and i.severity == "high"}
                claims_to_verify = [c for c in result.claims
                                    if c.risk_level == "high" or c.text in flagged]
            verifications, evidence, queries_log = verify_claims(
                claims_to_verify, sources,
                client=client, limits=rules.limits,
                allow_web=allow_web, latest_data_check=latest_data_check,
            )
            result.verifications = verifications
            result.evidence = evidence
            stage.status = "completed"
            stage.issue_count = len(verifications)

            # Coverage, stated rather than assumed. A claim that was extracted
            # and then never verified is the quietest failure in this stage:
            # the sheet shows the verified ones and nothing anywhere says the
            # others were skipped. It used to be recorded only in
            # source_queries.json, which nothing reads.
            unverified = [c for c in claims_to_verify
                          if not any(v.claim_id == c.claim_id
                                     for v in result.verifications)]
            if unverified:
                stage.status = "completed_with_warnings"
                result.warnings.append(
                    f"Source verification covered {len(result.verifications)} of "
                    f"{len(claims_to_verify)} claim(s); {len(unverified)} were not "
                    f"checked against a source. Treat those points as unverified "
                    f"rather than confirmed.")
            if not sources.configured:
                stage.status = "completed_with_warnings"
                stage.detail = "subject has no approved-source configuration"
        except Exception as e:
            stage.status = "failed"
            stage.detail = str(e)[:300]
            result.warnings.append(
                f"Source verification failed — affected claims sent to human review: {e}")
            for c in result.claims:
                if not any(v.claim_id == c.claim_id for v in result.verifications):
                    result.verifications.append(_unavailable_verification(c, str(e)[:200]))
    _write_json(run_dir, "source_queries.json", queries_log)
    _write_json(run_dir, "source_evidence.json", result.evidence)
    _write_json(run_dir, "source_verification.json", result.verifications)

    # ------------------------------------------------------------- 6. decide + apply
    _stage_cb(stage_callback, "Applying safe corrections")
    stage = result.stage("corrections")
    try:
        from .correction_policy import decide_corrections
        from .correction_applier import apply_corrections

        result.decisions = decide_corrections(
            result.issues, result.verifications, result.claims,
            auto_correct_enabled=auto_correct_enabled, rules=rules,
        )
        corrected, applied = apply_corrections(notes_text, result.decisions)
        result.applied = applied
        result.final_notes = corrected
        result.notes_changed = corrected != notes_text
        ok = sum(1 for a in applied if a.applied)
        failed = sum(1 for a in applied if not a.applied)
        stage.status = "completed" if not failed else "completed_with_warnings"
        stage.issue_count = ok
        if failed:
            stage.detail = f"{failed} correction(s) could not be applied and were sent to human review"
    except Exception as e:
        stage.status = "failed"
        stage.detail = str(e)[:300]
        result.warnings.append(f"Correction stage failed — notes left unedited: {e}")
        result.final_notes = notes_text
        result.notes_changed = False
    _write_json(run_dir, "correction_decisions.json", result.decisions)
    _write_json(run_dir, "corrections_applied.json", result.applied)

    # ------------------------------------------------------------------- 7. recheck
    _stage_cb(stage_callback, "Rechecking corrected notes")
    stage = result.stage("recheck")
    if level != "strict":
        stage.status = "disabled"
        stage.detail = "recheck runs at Strict level only"
    elif not result.notes_changed:
        stage.status = "completed"
        stage.detail = "no edits to recheck"
    elif not ai_available:
        stage.status = "unavailable"
        stage.detail = gate_detail
    else:
        try:
            from .ai_review import run_final_recheck

            verdict = run_final_recheck(client, final_notes=result.final_notes,
                                        applied=result.applied, run_dir=run_dir)
            stage.status = ("completed" if verdict.get("status") == "passed"
                            else "completed_with_warnings")
            stage.detail = "; ".join(
                f"{p.get('correction_id')}: {p.get('problem')}"
                for p in verdict.get("problems", []))[:300]
            result.summary["final_recheck_status"] = verdict.get("status")
        except Exception as e:
            stage.status = "failed"
            stage.detail = str(e)[:300]
            result.warnings.append(f"Final recheck failed: {e}")

    result.human_review_items = sum(
        1 for d in result.decisions
        if d.action in (HUMAN_REVIEW, UNVERIFIED, REMOVE_ONLY_AFTER_APPROVAL)
    ) + sum(1 for a in result.applied if not a.applied and a.action == AUTO_CORRECT)

    _write_artifacts(run_dir, result)
    return result


def _unavailable_verification(claim, reason: str):
    from .models import ClaimVerification

    return ClaimVerification(
        claim_id=claim.claim_id, original_claim=claim.text,
        status=SOURCE_UNAVAILABLE,
        explanation=f"Verification unavailable: {reason}",
        requires_human_review=True,
    )


def _write_artifacts(run_dir: Path, result: QCRunResult) -> None:
    # The per-stage artifacts are normally written as each stage finishes. On the
    # early-return paths (level Off, invalid config) no stage runs, so write the
    # empty versions here: a run folder that documents "this stage produced
    # nothing" is auditable, whereas a missing file is indistinguishable from a
    # crash. Never overwrite a file a stage already wrote.
    for name, payload in (
        ("deterministic_qc.json", result.issues),
        ("factual_claims.json", result.claims),
        ("source_queries.json", []),
        ("source_evidence.json", result.evidence),
        ("source_verification.json", result.verifications),
        ("correction_decisions.json", result.decisions),
        ("corrections_applied.json", result.applied),
    ):
        if not (run_dir / name).exists():
            _write_json(run_dir, name, payload)

    result.summary.update(result.to_summary())
    _write_json(run_dir, "qc_result.json", {
        "summary": result.summary,
        "stages": to_jsonable(result.stages),
        "warnings": result.warnings,
        "issues": to_jsonable(result.issues),
        "decisions": to_jsonable(result.decisions),
        "applied": to_jsonable(result.applied),
    })
