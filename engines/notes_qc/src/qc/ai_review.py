"""Independent AI quality review of generated notes (spec §16, §21).

The generation model must not be the only reviewer of its own output: this
module makes a SEPARATE Gemini call over the finished draft, framed as a
reviewer who did not write it, returning structured issues only — never a
rewrite. High-risk claim candidates are extracted in the same call, so the QC
review costs one request, not two.

All calls go through the pipeline's existing GeminiClient (primary
gemini-3.5-flash, fallback handled inside the client), which tags them with the
run's shared task_id so the proxy's raw usage rows group back into one task.
Strict JSON parsing with exactly one repair attempt; an
unparseable response is preserved raw and reported as a QC failure — unparsed
corrections are never applied (spec §16).
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import AppliedCorrection, QCIssue
from .qc_config import QCRules

_VALID_CATEGORIES = {"factual", "conceptual", "language", "compliance", "formatting"}

# Issues and claims are capped defensively; a model that emits hundreds of
# issues is degenerating, and unbounded lists would swamp the reports.
_MAX_ISSUES = 60
_MAX_CLAIMS = 40


def _slide_digest(slides, limit: int = 12000) -> str:
    parts = []
    for s in slides or []:
        text = (getattr(s, "prompt_text", "") or getattr(s, "text", "") or "").strip()
        parts.append(f"[Slide {getattr(s, 'slide_no', '?')}] {getattr(s, 'heading', '')}: {text[:400]}")
    digest = "\n".join(parts)
    return digest[:limit]


def _rules_block(rules: QCRules, enabled_checks: dict | None) -> str:
    """Render the enabled QC rules for the prompt. Enrichment-compliance rules
    are included only for enrichments the user actually enabled (spec §6)."""
    enabled_checks = enabled_checks or {}
    enrich_map = {
        "ENRICH-TABLES-001": "add_tables",
        "ENRICH-FLOW-002": "add_flowcharts",
        "ENRICH-MAINS-003": "add_mains_value",
        "ENRICH-PRELIMS-004": "add_prelims_keywords",
    }
    out = []
    for cat, rule_ids in rules.categories.items():
        for rid in rule_ids:
            rule = rules.rules[rid]
            if cat == "compliance":
                toggle = enrich_map.get(rid)
                if toggle and not enabled_checks.get(toggle, False):
                    continue
            out.append(f"- {rule.id} [{cat}/{rule.severity}] {rule.name}: {rule.description}")
    return "\n".join(out)


def build_review_prompt(
    *,
    notes_text: str,
    slides,
    transcript_text: str,
    transcript_gaps: list[str],
    subject: str,
    language_code: str,
    note_type: str,
    quality_level: str,
    rules: QCRules,
    enabled_checks: dict | None,
) -> str:
    gaps = "\n".join(f"- {g}" for g in transcript_gaps) if transcript_gaps else "none"
    return f"""You are an independent quality reviewer for UPSC class notes. You did NOT write these notes. Your only job is to find errors — do not rewrite the document, do not add content, do not praise it.

CONTEXT
Subject: {subject} | Language: {language_code} | Note type: {note_type} | QC level: {quality_level}
Transcript gaps (lecture minutes that were NEVER transcribed — content from them cannot be checked and must not be assumed correct): {gaps}

QC RULES TO CHECK (only these):
{_rules_block(rules, enabled_checks)}

IMPORTANT DISTINCTIONS
- Enrichment shortfalls (missing tables/flowcharts/keywords) are category "compliance", never "factual".
- A student-friendly paraphrase is NOT an error; only verbatim-quoted legal/constitutional text must match exactly.
- If you are not sure a fact is wrong, mark it as a claim to verify (below) instead of an issue.
- You have NO web access. Do not assert corrections from memory for statistics, current data, or legal text — flag them with requires_external_verification true.

SOURCE MATERIAL (ground truth for what was taught):
--- SLIDES ---
{_slide_digest(slides)}
--- TRANSCRIPT (may be partial) ---
{(transcript_text or "")[:40000]}
--- END SOURCES ---

NOTES UNDER REVIEW:
--- NOTES ---
{notes_text}
--- END NOTES ---

Respond with ONLY a JSON object, no prose, no code fences:
{{
  "summary": {{"status": "completed", "total_issues": <int>, "factual_candidates": <int>, "conceptual_issues": <int>, "language_issues": <int>}},
  "issues": [
    {{"issue_id": "QC-001", "category": "factual|conceptual|language|compliance|formatting", "rule_id": "<rule id from the list>", "severity": "high|medium|low", "heading": "<nearest heading in the NOTES>", "paragraph_index": <0-based index counting the NOTES' blank-line-separated paragraphs, or null>, "original_text": "<EXACT text copied from the notes>", "problem": "<what is wrong>", "suggested_text": "<corrected text, or empty if unsure>", "requires_external_verification": true|false, "confidence": <0.0-1.0>}}
  ],
  "claims": [
    {{"text": "<exact sentence from the notes making a checkable factual claim>", "claim_type": "<constitutional_article|amendment|schedule|legal_provision|judgment|historical_date|economic_statistic|scheme|superlative_claim|attribution|quotation|other>", "heading": "<nearest heading>", "risk_level": "high|medium", "date_sensitive": true|false, "requires_period": true|false, "entities": ["..."]}}
  ]
}}
original_text MUST be copied exactly from the notes (it is used to locate the edit). Issues you cannot anchor to exact text belong in "claims" or must carry empty original_text."""


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Some models prefix prose despite instructions; slice to the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text


def parse_review_response(raw: str) -> tuple[list[QCIssue], list[dict], dict]:
    """Validate the review JSON into typed issues + raw claim dicts.

    Raises ValueError on unparseable/misshapen JSON — the caller decides
    whether to attempt the single repair pass.
    """
    data = json.loads(_strip_fences(raw))
    if not isinstance(data, dict) or not isinstance(data.get("issues", []), list):
        raise ValueError("review response is not an object with an 'issues' list")

    issues: list[QCIssue] = []
    for i, item in enumerate(data.get("issues", [])[:_MAX_ISSUES], start=1):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "conceptual"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        pidx = item.get("paragraph_index")
        issues.append(QCIssue(
            issue_id=str(item.get("issue_id") or f"QC-{i:03d}"),
            category=category,
            rule_id=str(item.get("rule_id", "")).strip(),
            severity=str(item.get("severity", "medium")).strip().lower() or "medium",
            problem=str(item.get("problem", "")).strip(),
            heading=str(item.get("heading", "")).strip(),
            paragraph_index=int(pidx) if isinstance(pidx, (int, float)) else None,
            original_text=str(item.get("original_text", "")),
            suggested_text=str(item.get("suggested_text", "")),
            requires_external_verification=bool(item.get("requires_external_verification", False)),
            confidence=confidence,
            source="ai_review",
        ))

    claims = [c for c in data.get("claims", [])[:_MAX_CLAIMS] if isinstance(c, dict) and c.get("text")]
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return issues, claims, summary


def run_ai_quality_review(
    client,
    *,
    notes_text: str,
    slides,
    transcript_text: str,
    transcript_gaps: list[str],
    subject: str,
    language_code: str,
    note_type: str,
    quality_level: str,
    rules: QCRules,
    enabled_checks: dict | None,
    run_dir=None,
) -> tuple[list[QCIssue], list[dict], dict, str]:
    """One review call (+ at most one JSON-repair retry).

    Returns (issues, ai_claim_dicts, summary, raw_response). Raises ValueError
    when both attempts produced unusable JSON — the raw text is still written
    to run_dir first so nothing is lost.
    """
    prompt = build_review_prompt(
        notes_text=notes_text, slides=slides, transcript_text=transcript_text,
        transcript_gaps=transcript_gaps, subject=subject, language_code=language_code,
        note_type=note_type, quality_level=quality_level, rules=rules,
        enabled_checks=enabled_checks,
    )
    if run_dir is not None:
        (run_dir / "qc_prompt.txt").write_text(prompt, encoding="utf-8")

    resp = client.generate(prompt, [], max_images=0, max_output_tokens=16000, temperature=0.0)
    raw = resp.text
    if run_dir is not None:
        (run_dir / "qc_response_raw.txt").write_text(raw, encoding="utf-8")
    try:
        issues, claims, summary = parse_review_response(raw)
        return issues, claims, summary, raw
    except (ValueError, json.JSONDecodeError) as first_error:
        repair = (
            "Your previous response could not be parsed as JSON "
            f"({first_error}). Return the SAME review as ONE valid JSON object "
            "in exactly the schema you were given. No prose, no code fences.\n\n"
            f"Previous response:\n{raw[:20000]}"
        )
        resp2 = client.generate(repair, [], max_images=0, max_output_tokens=16000, temperature=0.0)
        raw2 = resp2.text
        if run_dir is not None:
            (run_dir / "qc_response_raw.txt").write_text(
                raw + "\n\n===== REPAIR ATTEMPT =====\n\n" + raw2, encoding="utf-8")
        issues, claims, summary = parse_review_response(raw2)  # raises on 2nd failure
        return issues, claims, summary, raw2


# ---------------------------------------------------------------------------
# Targeted final recheck (spec §21): only edited regions, reusing no external
# verification — cached evidence already backed the corrections.
# ---------------------------------------------------------------------------

def run_final_recheck(
    client,
    *,
    final_notes: str,
    applied: list[AppliedCorrection],
    run_dir=None,
) -> dict:
    """Verify the applied edits landed cleanly. Returns a JSON-able verdict:
    {"status": "passed"|"issues_found"|"failed", "problems": [...]}."""
    applied_ok = [a for a in applied if a.applied]
    if not applied_ok:
        return {"status": "passed", "problems": [], "note": "no corrections were applied"}

    # Send each edited paragraph with context, not the whole document.
    paragraphs = final_notes.split("\n\n")
    excerpts = []
    for a in applied_ok:
        hits = [i for i, p in enumerate(paragraphs) if a.replacement_text[:60] in p]
        idx = hits[0] if hits else None
        if idx is None:
            excerpts.append(f"[edit {a.correction_id}] EXPECTED TEXT NOT FOUND: {a.replacement_text[:200]}")
            continue
        lo, hi = max(0, idx - 1), min(len(paragraphs), idx + 2)
        excerpts.append(
            f"[edit {a.correction_id}] replaced: {a.original_text[:200]!r}\n"
            f"with: {a.replacement_text[:200]!r}\n"
            f"resulting section:\n" + "\n\n".join(paragraphs[lo:hi])
        )

    prompt = f"""You are rechecking corrections that were just applied to UPSC notes. For each edit below verify:
1. the replacement text is present, grammatically complete, and not duplicated;
2. the surrounding section does not now contradict itself;
3. no internal markers ([Faculty Point, NOTES_START, transcript unavailable, Note to DTP visible outside its own line) leaked into the text;
4. the heading of the section still matches its content.

{chr(10).join(excerpts)}

Respond with ONLY JSON: {{"status": "passed" or "issues_found", "problems": [{{"correction_id": "...", "problem": "..."}}]}}"""
    try:
        resp = client.generate(prompt, [], max_images=0, max_output_tokens=4000, temperature=0.0)
        data = json.loads(_strip_fences(resp.text))
        status = data.get("status") if data.get("status") in ("passed", "issues_found") else "issues_found"
        problems = [p for p in data.get("problems", []) if isinstance(p, dict)]
        result: dict[str, Any] = {"status": status, "problems": problems}
    except Exception as e:
        result = {"status": "failed", "problems": [], "error": str(e)[:300]}
    if run_dir is not None:
        (run_dir / "final_recheck.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
