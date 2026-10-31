"""Verify extracted factual claims against approved sources (spec §12-§15).

Called from engine.run_qc stage 5. For each claim, evidence is gathered in a
fixed order — the local standard-book index first, then (Strict level only,
and only when local evidence is thin) direct known endpoints on the subject's
approved official domains. The Gemini model then compares each claim against
its evidence, one call per batch of up to VERIFY_BATCH_SIZE claims to control
cost.

Honesty rules this module enforces, independent of what the model says:

- A claim with no evidence is SOURCE_NOT_FOUND ("flag for a human"), and a
  claim whose every fetch attempt errored is SOURCE_UNAVAILABLE. Neither is
  ever converted into "the claim is false".
- Statuses outside models.VERIFICATION_STATUSES collapse to
  NEEDS_HUMAN_REVIEW — an inventive model must not mint new outcomes.
- auto_correct_allowed is computed HERE, never trusted from model output:
  only CONTRADICTED/OUTDATED with confidence >= AUTO_CORRECT_MIN_CONFIDENCE,
  a non-empty suggested_text AND cited evidence ids qualify.
- Claims the model forgot to answer for become NEEDS_HUMAN_REVIEW, and an
  unparseable response (after one repair attempt) does the same for its whole
  batch — silence is never treated as verification.

Every retrieval attempt is appended to queries_log so the run artifact
(source_queries.json) documents exactly what was and was not looked up.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from . import page_fetcher, pdf_fetcher
from .evidence_extractor import (
    evidence_from_chunks,
    evidence_from_page,
    evidence_from_pdf,
)
from .models import (
    AMBIGUOUS,
    AUTO_CORRECT_MIN_CONFIDENCE,
    CONTRADICTED,
    INCOMPLETE_CONTEXT,
    NEEDS_HUMAN_REVIEW,
    OUTDATED,
    SOURCE_NOT_FOUND,
    SOURCE_UNAVAILABLE,
    VERIFICATION_STATUSES,
    ClaimVerification,
    Evidence,
)
from .source_search import build_queries, candidate_urls

VERIFY_BATCH_SIZE = 8
# Below this many local passages the verifier also tries the approved web.
MIN_LOCAL_PASSAGES_BEFORE_WEB = 2
_MAX_PASSAGE_CHARS_IN_PROMPT = 900
_MAX_OUTPUT_TOKENS = 8000

_HUMAN_REVIEW_STATUSES = {
    AMBIGUOUS, INCOMPLETE_CONTEXT, NEEDS_HUMAN_REVIEW,
    SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE,
}


# --------------------------------------------------------------- small utils

def _normalise(text: str) -> str:
    """Cache key normalisation (mirrors claim_extractor._normalise)."""
    return " ".join(re.findall(r"[a-z0-9%₹]+", str(text or "").lower()))


def _verification(claim, status: str, explanation: str) -> ClaimVerification:
    """A verification produced by policy, not by the model."""
    return ClaimVerification(
        claim_id=claim.claim_id,
        original_claim=claim.text,
        status=status,
        explanation=explanation,
        auto_correct_allowed=False,
        requires_human_review=status in _HUMAN_REVIEW_STATUSES,
    )


def _strip_fences(raw: str) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Some models prefix prose despite instructions; slice to the outer braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text


def _parse_results(raw: str) -> list[dict]:
    """Strict parse of the verifier response. Raises on anything unusable."""
    data = json.loads(_strip_fences(raw))  # json.JSONDecodeError on bad JSON
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("verifier response is not an object with a 'results' list")
    return [r for r in data["results"] if isinstance(r, dict)]


# ------------------------------------------------------------------- prompt

def _prompt_rules(latest_data_check: bool) -> str:
    if latest_data_check:
        latest_line = (
            "OUTDATED is allowed only when the claim states CURRENT data and the "
            "evidence shows a newer official edition supersedes it."
        )
    else:
        latest_line = (
            "Do NOT flag OUTDATED merely because newer data exists; judge only "
            "what the claim itself states."
        )
    return f"""You are a strict source-verification checker for UPSC study notes.
For every claim below, compare it ONLY against its listed evidence passages.

Rules:
1. EXACT matching is required for quoted legal/constitutional text and for
   Article/Schedule/Amendment/clause numbers, dates, names and judgment
   titles. Paraphrase is acceptable for everything else.
2. A date-sensitive claim must be judged against the period the paragraph
   discusses. NEVER suggest replacing a historical value with the latest one.
3. {latest_line}
4. A statistic that lacks year/period context -> INCOMPLETE_CONTEXT.
5. Conflicting evidence -> AMBIGUOUS or NEEDS_HUMAN_REVIEW.
6. Evidence that merely looks similar is NOT verification. Mark VERIFIED only
   when the evidence actually states the claim's facts; if the evidence does
   not cover the claim, use SOURCE_NOT_FOUND. Never guess.
Allowed status values (use no other): VERIFIED, CONTRADICTED, OUTDATED,
INCOMPLETE_CONTEXT, SOURCE_NOT_FOUND, SOURCE_UNAVAILABLE, AMBIGUOUS,
NEEDS_HUMAN_REVIEW."""


def _build_prompt(batch, evidence_by_claim: dict[str, list[Evidence]],
                  latest_data_check: bool) -> str:
    lines = [_prompt_rules(latest_data_check), ""]
    for claim in batch:
        flags = []
        if getattr(claim, "date_sensitive", False):
            flags.append("date-sensitive")
        if getattr(claim, "requires_period", False):
            flags.append("no year/period stated")
        if getattr(claim, "data_period", ""):
            flags.append(f"period discussed: {claim.data_period}")
        flag_note = f" [{'; '.join(flags)}]" if flags else ""
        lines.append(f"CLAIM {claim.claim_id} (type: {claim.claim_type}){flag_note}")
        lines.append(f"TEXT: {claim.text}")
        for ev in evidence_by_claim.get(claim.claim_id, []):
            where = []
            if ev.publication_date:
                where.append(f"published {ev.publication_date}")
            if ev.edition:
                where.append(f"edition {ev.edition}")
            if ev.page_number is not None:
                where.append(f"page {ev.page_number}")
            where_note = f"; {', '.join(where)}" if where else ""
            passage = ev.passage[:_MAX_PASSAGE_CHARS_IN_PROMPT]
            lines.append(
                f"  EVIDENCE {ev.evidence_id} ({ev.source_type}: "
                f"{ev.source_name}{where_note}): {passage}"
            )
        lines.append("")
    lines.append(
        'Respond with ONLY this JSON — no prose, no code fences:\n'
        '{"results": [{"claim_id": "...", "status": "<one of the allowed '
        'statuses>", "explanation": "...", "suggested_text": "..." or null, '
        '"confidence": 0.0-1.0, "evidence_ids": ["..."], "data_period": "", '
        '"is_latest": true/false/null}]}\n'
        "Include exactly one result for EVERY claim listed above."
    )
    return "\n".join(lines)


def _verification_from_result(claim, raw: dict,
                              valid_evidence_ids: set[str]) -> ClaimVerification:
    """One model result dict -> ClaimVerification, defensively coerced."""
    status = str(raw.get("status") or "").strip().upper()
    explanation = str(raw.get("explanation") or "").strip()
    if status not in VERIFICATION_STATUSES:
        explanation = (f"verifier returned unknown status {status!r}. "
                       + explanation).strip()
        status = NEEDS_HUMAN_REVIEW
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    suggested_raw = raw.get("suggested_text")
    suggested = str(suggested_raw).strip() if suggested_raw not in (None, "") else None
    # Only evidence ids that actually belong to this claim survive — the model
    # cannot cite evidence it was never shown.
    evidence_ids = [str(e) for e in (raw.get("evidence_ids") or [])
                    if str(e) in valid_evidence_ids]
    is_latest = raw.get("is_latest") if isinstance(raw.get("is_latest"), bool) else None
    auto_correct_allowed = (
        status in (CONTRADICTED, OUTDATED)
        and confidence >= AUTO_CORRECT_MIN_CONFIDENCE
        and bool(suggested)
        and bool(evidence_ids)
    )
    return ClaimVerification(
        claim_id=claim.claim_id,
        original_claim=claim.text,
        status=status,
        explanation=explanation or "no explanation given by the verifier",
        suggested_text=suggested,
        evidence_ids=evidence_ids,
        confidence=confidence,
        auto_correct_allowed=auto_correct_allowed,
        requires_human_review=status in _HUMAN_REVIEW_STATUSES,
        data_period=str(raw.get("data_period") or ""),
        is_latest=is_latest,
    )


# ------------------------------------------------------------ evidence phase

def _gather_web_evidence(claim, sources, limits, cache_dir,
                         evidence: list[Evidence],
                         queries_log: list[dict]) -> tuple[int, int]:
    """Fetch direct approved endpoints for one claim. Returns
    (attempts, errors); found passages append to ``evidence`` in place."""
    domains = list(getattr(sources, "official_domains", None) or [])
    attempts = 0
    errors = 0
    for query in build_queries(claim):
        queries_log.append({
            "claim_id": claim.claim_id,
            "query": query,
            "outcome": "recorded for the log only — no web-search API; "
                       "direct official endpoints are fetched instead",
        })
    for url in candidate_urls(claim, sources,
                              max_urls=int(getattr(limits, "sources_per_claim", 3))):
        attempts += 1
        if url.lower().endswith(".pdf"):
            result = pdf_fetcher.fetch_pdf(
                url, domains,
                timeout=int(getattr(limits, "request_timeout_seconds", 20)),
                max_bytes=int(getattr(limits, "max_download_bytes", 15 * 1024 * 1024)),
                cache_dir=cache_dir,
            )
            if result.error:
                errors += 1
                outcome = f"error: {result.error}"
            else:
                found = evidence_from_pdf(
                    claim, result, int(getattr(limits, "passages_per_source", 3)),
                    start=len(evidence) + 1,
                )
                evidence.extend(found)
                outcome = f"fetched pdf: {len(found)} relevant passage(s)"
        else:
            result = page_fetcher.fetch_page(
                url, domains,
                timeout=int(getattr(limits, "request_timeout_seconds", 20)),
                max_bytes=int(getattr(limits, "max_download_bytes", 15 * 1024 * 1024)),
                cache_dir=cache_dir,
            )
            if result.error:
                errors += 1
                outcome = f"error: {result.error}"
            else:
                found = evidence_from_page(
                    claim, result, int(getattr(limits, "passages_per_source", 3)),
                    start=len(evidence) + 1,
                )
                evidence.extend(found)
                outcome = f"fetched page: {len(found)} relevant passage(s)"
        queries_log.append({"claim_id": claim.claim_id, "url": url, "outcome": outcome})
    return attempts, errors


# ---------------------------------------------------------------- public API

def verify_claims(
    claims,
    sources,
    *,
    client,
    limits,
    allow_web: bool = True,
    latest_data_check: bool = True,
    retriever=None,
    cache_dir=None,
) -> tuple[list[ClaimVerification], list[Evidence], list[dict]]:
    """Verify claims against approved sources.

    Returns (verifications, evidence, queries_log) — one verification per
    input claim (post-cap), in input order.
    """
    queries_log: list[dict] = []
    all_evidence: list[Evidence] = []

    claims = [c for c in (claims or []) if c is not None]
    max_claims = int(getattr(limits, "max_claims_verified", 20))
    if len(claims) > max_claims:
        queries_log.append({
            "claim_id": "",
            "query": "claim cap",
            "outcome": (f"{len(claims)} claims requested; verifying the first "
                        f"{max_claims}, {len(claims) - max_claims} skipped "
                        "(limits.max_claims_verified)"),
        })
        claims = claims[:max_claims]
    if not claims:
        return [], [], queries_log

    if client is None:
        return (
            [_verification(c, SOURCE_UNAVAILABLE,
                           "no AI client available for semantic verification")
             for c in claims],
            [],
            queries_log,
        )

    # -------------------------------------------------- in-run duplicate cache
    # Duplicate claims (same normalised text + subject) are verified once; the
    # duplicates reuse that verification (and its evidence ids) afterwards.
    first_by_key: dict[tuple[str, str], str] = {}
    duplicate_of: dict[str, str] = {}
    unique_claims = []
    for c in claims:
        key = (_normalise(c.text), str(getattr(c, "subject", "") or ""))
        original_id = first_by_key.get(key)
        if original_id is not None:
            duplicate_of[c.claim_id] = original_id
            queries_log.append({
                "claim_id": c.claim_id,
                "query": "verification cache",
                "outcome": f"duplicate of {original_id}; reusing its verification",
            })
        else:
            first_by_key[key] = c.claim_id
            unique_claims.append(c)

    # ------------------------------------------------------------- retriever
    if retriever is None:
        try:
            from .reference_index import ReferenceRetriever

            retriever = ReferenceRetriever()
        except Exception:  # noqa: BLE001 — book search is best-effort
            retriever = None
    retriever_ok = False
    if retriever is not None:
        try:
            retriever_ok = bool(retriever.available())
        except Exception:  # noqa: BLE001
            retriever_ok = False

    domains = list(getattr(sources, "official_domains", None) or [])
    passages_per_source = int(getattr(limits, "passages_per_source", 3))

    # ------------------------------------------------------- gather evidence
    # Claims gather their evidence concurrently. Twenty claims each fetching
    # official pages with a 20 s timeout used to run strictly in series, which
    # made this loop — not the model — the slowest part of a Strict run when a
    # source domain was down. `max_concurrent_requests` had sat in the YAML
    # config since the start without anything reading it; it is the cap here.
    #
    # Each worker writes only claim-local state and returns it; the shared
    # dicts and queries_log are filled in afterwards IN CLAIM ORDER, so the log
    # reads down the claims exactly as it did when this was sequential.
    if cache_dir is None and allow_web and domains:
        from .paths import app_data_dir

        cache_dir = app_data_dir("qc_cache")
    # The local book index makes no thread-safety promises, so its calls are
    # serialised; the web fetches (the actual bottleneck) run concurrently.
    retriever_lock = threading.Lock()

    def _gather_for(c) -> tuple[list[Evidence], int, int, list[dict]]:
        local_log: list[dict] = []
        evidence: list[Evidence] = []
        if retriever_ok:
            try:
                with retriever_lock:
                    chunks = retriever.search(
                        c.text, getattr(sources, "subject_key", ""),
                        top_k=passages_per_source,
                    )
            except Exception as e:  # noqa: BLE001 — degrade to no book evidence
                chunks = []
                local_log.append({
                    "claim_id": c.claim_id, "query": c.text[:160],
                    "outcome": f"local index error: {e}",
                })
            else:
                local_log.append({
                    "claim_id": c.claim_id, "query": c.text[:160],
                    "outcome": f"local index: {len(chunks)} passage(s)",
                })
            evidence.extend(evidence_from_chunks(
                c, chunks, getattr(sources, "books", None) or []))

        attempts = errors = 0
        if (allow_web and len(evidence) < MIN_LOCAL_PASSAGES_BEFORE_WEB and domains):
            attempts, errors = _gather_web_evidence(
                c, sources, limits, cache_dir, evidence, local_log)
        return evidence, attempts, errors, local_log

    max_workers = max(1, int(getattr(limits, "max_concurrent_requests", 4)))
    evidence_by_claim: dict[str, list[Evidence]] = {}
    fetch_stats: dict[str, tuple[int, int]] = {}  # claim_id -> (attempts, errors)
    gathered: dict[str, tuple[list[Evidence], int, int, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(unique_claims)))) as ex:
        futures = {ex.submit(_gather_for, c): c for c in unique_claims}
        for future in as_completed(futures):
            c = futures[future]
            try:
                gathered[c.claim_id] = future.result()
            except Exception as e:  # noqa: BLE001 — QC must degrade, not crash
                gathered[c.claim_id] = ([], 1, 1, [{
                    "claim_id": c.claim_id, "query": c.text[:160],
                    "outcome": f"evidence gathering failed: {e}",
                }])
    for c in unique_claims:
        evidence, attempts, errors, local_log = gathered[c.claim_id]
        queries_log.extend(local_log)
        evidence_by_claim[c.claim_id] = evidence
        fetch_stats[c.claim_id] = (attempts, errors)

    # -------------------------------------------- no-evidence claims (policy)
    outcome_by_claim: dict[str, ClaimVerification] = {}
    to_verify = []
    for c in unique_claims:
        evidence = evidence_by_claim[c.claim_id]
        if evidence:
            all_evidence.extend(evidence)
            to_verify.append(c)
            continue
        attempts, errors = fetch_stats[c.claim_id]
        if attempts > 0 and errors >= attempts:
            outcome_by_claim[c.claim_id] = _verification(
                c, SOURCE_UNAVAILABLE,
                "every fetch attempt against approved sources failed; the "
                "claim could not be checked and needs a human review",
            )
        else:
            outcome_by_claim[c.claim_id] = _verification(
                c, SOURCE_NOT_FOUND,
                "no approved source found for this claim; it is flagged for "
                "human review, not marked false",
            )

    # ----------------------------------------------------- batched model calls
    # Batches are independent — each carries its own claims and its own
    # evidence — so they run concurrently under the same cap as the fetches.
    # Each worker returns its verdicts; nothing shared is written from a thread.
    def _verify_batch(batch) -> dict[str, ClaimVerification]:
        verdicts: dict[str, ClaimVerification] = {}
        prompt = _build_prompt(batch, evidence_by_claim, latest_data_check)
        try:
            raw = client.generate(prompt, [], max_images=0,
                                  max_output_tokens=_MAX_OUTPUT_TOKENS,
                                  temperature=0.0).text
        except Exception as e:  # noqa: BLE001 — GeminiError etc.: honest degrade
            for c in batch:
                verdicts[c.claim_id] = _verification(
                    c, SOURCE_UNAVAILABLE,
                    f"verifier call failed: {str(e)[:200]}")
            return verdicts

        results: list[dict] | None
        try:
            results = _parse_results(raw)
        except (json.JSONDecodeError, ValueError) as first_error:
            repair = (
                "Your previous response could not be parsed as JSON "
                f"({first_error}). Return the SAME verification results as ONE "
                "valid JSON object in exactly the schema you were given. "
                f"No prose, no code fences.\n\nPrevious response:\n{raw[:20000]}"
            )
            try:
                raw2 = client.generate(repair, [], max_images=0,
                                       max_output_tokens=_MAX_OUTPUT_TOKENS,
                                       temperature=0.0).text
                results = _parse_results(raw2)
            except Exception:  # noqa: BLE001 — second failure ends the attempt
                results = None
        if results is None:
            for c in batch:
                verdicts[c.claim_id] = _verification(
                    c, NEEDS_HUMAN_REVIEW, "verifier response unparseable")
            return verdicts

        by_id = {str(r.get("claim_id") or "").strip(): r for r in results}
        for c in batch:
            raw_result = by_id.get(c.claim_id)
            if raw_result is None:
                verdicts[c.claim_id] = _verification(
                    c, NEEDS_HUMAN_REVIEW, "verifier omitted this claim")
            else:
                valid_ids = {e.evidence_id for e in evidence_by_claim[c.claim_id]}
                verdicts[c.claim_id] = _verification_from_result(
                    c, raw_result, valid_ids)
        return verdicts

    batches = [to_verify[i:i + VERIFY_BATCH_SIZE]
               for i in range(0, len(to_verify), VERIFY_BATCH_SIZE)]
    if batches:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as ex:
            futures = {ex.submit(_verify_batch, b): b for b in batches}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    outcome_by_claim.update(future.result())
                except Exception as e:  # noqa: BLE001 — degrade, never crash QC
                    for c in batch:
                        outcome_by_claim[c.claim_id] = _verification(
                            c, SOURCE_UNAVAILABLE,
                            f"verifier batch failed: {str(e)[:200]}")

    # ------------------------------------------------ assemble in input order
    verifications: list[ClaimVerification] = []
    for c in claims:
        original_id = duplicate_of.get(c.claim_id)
        if original_id is None:
            verifications.append(outcome_by_claim[c.claim_id])
        else:
            base = outcome_by_claim[original_id]
            # Same verdict, this claim's identity; evidence_ids keep pointing
            # at the original claim's evidence records (documented reuse).
            verifications.append(replace(
                base, claim_id=c.claim_id, original_claim=c.text))
    return verifications, all_evidence, queries_log
