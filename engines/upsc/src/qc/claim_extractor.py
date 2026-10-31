"""Deterministic extraction of high-risk factual claim candidates.

The AI review also nominates claims, but a model can miss the exact patterns
UPSC reviewers care most about (article numbers, percentages, superlatives).
This module is the deterministic safety net: pure regex/heuristics over the
notes text, so the same notes always yield the same candidates in the same
order — which keeps run artifacts diffable and the verification stage
reproducible. merge_ai_claims() then folds the AI's nominations into that
stable list, deduping near-identical claims so a fact is never verified twice.

Sentences inside [Faculty Point Corrected] / [Faculty Point Removed] /
[Needs Human Review] pointer blocks are skipped on purpose: those points were
already fact-checked by the generation pass and are parsed separately by
src/quality_checker.parse_correction_blocks(); re-extracting them would make
the verifier re-litigate settled corrections.

Never raises on malformed input — bad text or bad AI dicts degrade to fewer
candidates, not a crashed pipeline.
"""
from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher

from .models import FactualClaim

# Hard cap on candidates.
#
# Was 60, chosen when the verifier only checked the first 20 anyway. Both are
# now sized to cover a whole note: at 15 pages there is no realistic way to
# write more than this many distinct checkable claims, so the cap is a guard
# against a malformed note rather than a sampling rate.
MAX_CANDIDATES = 250

# difflib ratio above which two claim texts are treated as the same claim.
SIMILARITY_THRESHOLD = 0.85

# claim_type given to AI claims that arrive without one; also treated as
# "unclassified" when deciding which of two duplicate records is richer.
_AI_DEFAULT_TYPE = "ai_flagged"

_RISK_LEVELS = ("high", "medium", "low")

# --------------------------------------------------------------------------
# Correction-pointer blocks. Mirrors src/quality_checker._BLOCK_RE — kept as a
# local copy so this module stays importable without the main pipeline models.
# --------------------------------------------------------------------------
_MARKER_RE = re.compile(
    r"^[\s\-\*•>]*\**\s*\[\s*(?:Faculty Point Corrected|Faculty Point Removed|"
    r"Needs Human Review|Correction Note|Needs Verification|सुधार नोट|सत्यापन आवश्यक)"
    r"\s*\]\s*\**\s*:?\s*$",
    re.I,
)
# 'Label: value' lines inside a pointer block. Only consulted while a block is
# open, so an ordinary sentence starting with e.g. 'Reason' is unaffected.
_FIELD_RE = re.compile(
    r"^[\s\-\*•>]*\**\s*(?:original|faculty|removed|corrected|correction|point|"
    r"suggested|replacement|issue|action|reason|confidence|"
    r"मूल|बिंदु|सही|समस्या|कारण|विश्वास)[^:\n]{0,40}:",
    re.I,
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•◦▪‣>]+|\d{1,2}[.)]|\(?[a-z]\)|\(?[ivx]{1,4}\))\s+", re.I)
_DTP_RE = re.compile(r"\bdtp\b|\[\s*dtp\s*\]", re.I)
_SLIDE_RE = re.compile(r"(?:slide|स्लाइड)\s*(?:no\.?|#)?\s*[:\-]?\s*(\d{1,3})\b", re.I)

# --------------------------------------------------------------------------
# Triggers, most specific first. A sentence gets exactly ONE claim: the first
# matching type wins, so a bare year never outranks the article/amendment/
# statistic it belongs to.
# --------------------------------------------------------------------------
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
)
_YEAR_RE = re.compile(r"\b1[5-9]\d{2}\b|\b20\d{2}\b")
_FULL_DATE_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    re.I,
)
# 'FY24', 'FY 2023-24', '2023-24', or a plain year — anything that anchors an
# economic figure to a period.
_PERIOD_RE = re.compile(r"\bFY\s*'?\d{2,4}\b|\b(?:19|20)\d{2}\b|\b\d{4}\s*[-–]\s*\d{2,4}\b", re.I)

_ORDINAL_WORDS = (
    r"(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|"
    r"Eleventh|Twelfth)"
)
_TRIGGERS: list[tuple[str, re.Pattern[str]]] = [
    ("constitutional_article",
     re.compile(r"\b(?:Articles?|Art\.?|Anuchchhed|अनुच्छेद)\s*-?\s*\d+[A-Z]?\b", re.I)),
    ("schedule",
     re.compile(
         rf"\b(?:\d+(?:st|nd|rd|th)|{_ORDINAL_WORDS})\s+Schedule\b"
         r"|\bSchedule\s+(?:[IVXL]+|\d+)\b",
         re.I)),
    ("amendment",
     re.compile(r"\b\d+(?:st|nd|rd|th)\s+(?:Constitutional?\s+)?Amendment\b", re.I)),
    # Scoped (?i:...) keeps keyword matching case-insensitive while the 'X v. Y'
    # and 'Xxx Act' patterns stay case-sensitive — capitalisation IS their signal.
    ("legal_provision",
     re.compile(r"(?i:\bsections?\s+\d+[a-z]?\b)|\b(?:[A-Z][\w’'()&,.-]*\s+){1,7}Act\b(?:,?\s*\d{4})?")),
    ("judgment",
     re.compile(
         r"\b[A-Z][\w.’']*\s+(?:v\.|vs\.?|versus)\s+[A-Z]"
         r"|(?i:\bjudge?ments?\b|\bcase\b)",
         )),
    ("quotation",
     re.compile(r"[\"“][^\"“”]{16,}[\"”]")),
    ("attribution",
     re.compile(
         r"\b(?:[A-Z][\w’'.-]+\s+){1,5}(?:Committee|Commission|Report)\b"
         r"|\b(?:Committee|Commission|Report)\s+(?:on|of)\s+[A-Z]")),
    ("economic_statistic",
     re.compile(
         r"\b\d[\d,]*(?:\.\d+)?\s*(?:%|per\s?cents?|percent)"
         r"|₹\s*\d|\bRs\.?\s*\d"
         r"|\b\d[\d,]*(?:\.\d+)?\s*(?:crore|lakh|billion|million|trillion)s?\b"
         r"|\brank(?:ed|ing|s)?\b",
         re.I)),
    ("superlative_claim",
     re.compile(r"\b(?:largest|first|only|highest|lowest|biggest|oldest)\b", re.I)),
    # Weakest signal last: a bare year/date only matters when nothing more
    # specific claimed the sentence first.
    ("historical_date", re.compile(rf"{_YEAR_RE.pattern}|{_FULL_DATE_RE.pattern}", re.I)),
]

# Entities: runs of 2+ capitalised tokens, allowing one lowercase connective.
_ENTITY_RE = re.compile(
    r"\b[A-Z][A-Za-z’'&.\-]+(?:(?:\s+(?:of|and|for|the|to|in))?\s+[A-Z][A-Za-z’'&.\-]+)+"
)
_NUMBER_RE = re.compile(
    r"₹\s*\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:%|per\s?cents?|crore|lakh|billion|million|trillion)"
    r"|\b\d[\d,]*(?:\.\d+)?(?:st|nd|rd|th)?\b",
    re.I,
)
_MAX_ENTITIES = 6

# Abbreviations whose trailing period must not end a sentence ('v.' matters
# most: case names would otherwise split in half).
_ABBREV_RE = re.compile(
    r"\b(v|vs|Rs|Arts?|Nos?|Dr|Mr|Mrs|Ms|St|Smt|Sh|Jr|Sr|Govt|Ltd|Co|etc)\.",
    re.I,
)
_IE_EG_RE = re.compile(r"\b([ie])\.([eg])\.", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")
_PLACEHOLDER = "\x00"


# --------------------------------------------------------------------------
# Text walking helpers
# --------------------------------------------------------------------------

def _split_blocks(text: str) -> list[list[str]]:
    """Paragraphs = runs of non-blank lines; blank lines separate them.

    correction_applier.paragraph_spans() implements the identical convention
    over character offsets: the paragraph_index recorded on a claim here is the
    same index the applier resolves back to a span, so the two must never drift.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _is_heading(line: str) -> bool:
    """Spec rule: markdown '#', or a non-bullet line < 80 chars with no
    terminal period. Field/table lines are excluded so a stray pointer field
    never becomes the heading context (mirrors quality_checker)."""
    s = line.strip()
    if not s or _MARKER_RE.match(line) or _FIELD_RE.match(line):
        return False
    if s.startswith("#"):
        return True
    if len(s) >= 80 or "|" in s or _BULLET_RE.match(line):
        return False
    return not s.endswith((".", "।", "?", "!"))


def _clean_heading(line: str) -> str:
    return line.strip().lstrip("#").strip().replace("**", "").strip(": ").strip()


def _split_sentences(text: str) -> list[str]:
    """Split on sentence enders, protecting abbreviation periods first so
    'Kesavananda Bharati v. State of Kerala' stays one sentence."""
    protected = _ABBREV_RE.sub(lambda m: m.group(1) + _PLACEHOLDER, text)
    protected = _IE_EG_RE.sub(
        lambda m: m.group(1) + _PLACEHOLDER + m.group(2) + _PLACEHOLDER, protected
    )
    parts = _SENTENCE_SPLIT_RE.split(protected)
    return [p.replace(_PLACEHOLDER, ".").strip() for p in parts if p.strip()]


def _extract_entities(sentence: str) -> list[str]:
    """Capitalised multi-word names first, then numbers; ordered, deduped,
    capped so the claims artifact never balloons."""
    seen: list[str] = []
    for regex in (_ENTITY_RE, _NUMBER_RE):
        for m in regex.finditer(sentence):
            value = m.group().strip()
            if value and value not in seen:
                seen.append(value)
            if len(seen) >= _MAX_ENTITIES:
                return seen
    return seen


def _slide_for_block(index: int, slides: list[int | None], dtp_flags: list[bool]) -> int | None:
    """Own slide mention wins; otherwise a neighbouring DTP note's mention —
    DTP notes name the slide the surrounding content came from."""
    if slides[index] is not None:
        return slides[index]
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(slides) and dtp_flags[neighbour] and slides[neighbour] is not None:
            return slides[neighbour]
    return None


def _classify(sentence: str) -> str | None:
    """First matching trigger wins — see _TRIGGERS ordering rationale."""
    for claim_type, regex in _TRIGGERS:
        if regex.search(sentence):
            return claim_type
    return None


def _build_claim(
    sentence: str,
    claim_type: str,
    subject: str,
    heading: str | None,
    paragraph_index: int,
    source_slide: int | None,
    sequence: int,
) -> FactualClaim:
    date_sensitive = False
    requires_period = False
    if claim_type == "economic_statistic":
        # A statistic without a stated period is the single most common
        # source of 'outdated notes' complaints — flag it for the verifier.
        date_sensitive = True
        requires_period = _PERIOD_RE.search(sentence) is None
    return FactualClaim(
        claim_id=f"CLM-{sequence:03d}",
        text=sentence,
        claim_type=claim_type,
        subject=subject,
        risk_level="high",
        heading=heading,
        paragraph_index=paragraph_index,
        source_slide=source_slide,
        date_sensitive=date_sensitive,
        requires_period=requires_period,
        entities=_extract_entities(sentence),
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def extract_claim_candidates(notes_text: str, subject: str) -> list[FactualClaim]:
    """Walk the notes paragraph by paragraph and emit at most MAX_CANDIDATES
    high-risk claims in document order, ids CLM-001..."""
    if not isinstance(notes_text, str) or not notes_text.strip():
        return []
    subject = str(subject or "").strip()

    blocks = _split_blocks(notes_text)
    block_texts = ["\n".join(b) for b in blocks]
    slides: list[int | None] = []
    for text in block_texts:
        m = _SLIDE_RE.search(text)
        slides.append(int(m.group(1)) if m else None)
    dtp_flags = [bool(_DTP_RE.search(t)) for t in block_texts]

    claims: list[FactualClaim] = []
    heading: str | None = None
    in_marker = False
    for p_idx, block in enumerate(blocks):
        source_slide = _slide_for_block(p_idx, slides, dtp_flags)
        for line in block:
            if in_marker:
                if _FIELD_RE.match(line):
                    continue
                in_marker = False  # block over; this line is ordinary text
            if _MARKER_RE.match(line):
                in_marker = True
                continue
            if _DTP_RE.search(line):
                # Production instructions to the DTP team, not lecture facts.
                continue
            if _is_heading(line):
                heading = _clean_heading(line)
                continue
            for sentence in _split_sentences(_BULLET_RE.sub("", line, count=1)):
                if len(sentence) < 8:
                    continue
                claim_type = _classify(sentence)
                if claim_type is None:
                    continue
                claims.append(_build_claim(
                    sentence, claim_type, subject, heading, p_idx,
                    source_slide, len(claims) + 1,
                ))
                if len(claims) >= MAX_CANDIDATES:
                    return claims
    return claims


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9%₹]+", text.lower()))


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio() > SIMILARITY_THRESHOLD


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _claim_from_ai(raw: dict, subject: str) -> FactualClaim | None:
    """One AI review dict -> FactualClaim; None when there is nothing usable.
    Everything is coerced defensively — the review JSON is model output."""
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    risk = str(raw.get("risk_level") or "high").strip().lower()
    if risk not in _RISK_LEVELS:
        risk = "high"
    entities_raw = raw.get("entities")
    entities = []
    if isinstance(entities_raw, list):
        for e in entities_raw:
            value = str(e).strip()
            if value and value not in entities:
                entities.append(value)
            if len(entities) >= _MAX_ENTITIES:
                break
    return FactualClaim(
        claim_id="CLM-000",  # renumbered by merge_ai_claims
        text=text,
        claim_type=str(raw.get("claim_type") or _AI_DEFAULT_TYPE).strip() or _AI_DEFAULT_TYPE,
        subject=subject,
        risk_level=risk,
        heading=str(raw.get("heading") or "").strip() or None,
        paragraph_index=_to_int(raw.get("paragraph_index")),
        source_slide=_to_int(raw.get("source_slide")),
        date_sensitive=bool(raw.get("date_sensitive", False)),
        requires_period=bool(raw.get("requires_period", False)),
        entities=entities,
    )


def _richness(c: FactualClaim) -> int:
    """How much verification-useful context a record carries. Used only to
    pick the base record of a duplicate pair — the loser still backfills."""
    return (
        int(bool(c.heading))
        + int(c.paragraph_index is not None)
        + int(c.source_slide is not None)
        + int(bool(c.entities))
        + int(bool(c.transcript_timestamp))
        + int(bool(c.data_period))
        + int(bool(c.claim_type) and c.claim_type != _AI_DEFAULT_TYPE)
        + int(c.date_sensitive)
        + int(c.requires_period)
    )


def _merge_pair(kept: FactualClaim, other: FactualClaim) -> FactualClaim:
    """Union two records describing the same claim. The richer one's text and
    type win; anchors it lacks are backfilled; date flags OR together because
    missing a time-sensitivity flag is worse than a spurious period check."""
    if _richness(other) > _richness(kept):
        kept, other = other, kept
    entities = list(kept.entities)
    for e in other.entities:
        if e not in entities and len(entities) < _MAX_ENTITIES:
            entities.append(e)
    risk_order = {level: i for i, level in enumerate(_RISK_LEVELS)}
    risk = min(kept.risk_level, other.risk_level, key=lambda r: risk_order.get(r, 0))
    return replace(
        kept,
        claim_type=kept.claim_type if kept.claim_type != _AI_DEFAULT_TYPE else other.claim_type,
        risk_level=risk,
        heading=kept.heading or other.heading,
        paragraph_index=kept.paragraph_index if kept.paragraph_index is not None else other.paragraph_index,
        source_slide=kept.source_slide if kept.source_slide is not None else other.source_slide,
        transcript_timestamp=kept.transcript_timestamp or other.transcript_timestamp,
        date_sensitive=kept.date_sensitive or other.date_sensitive,
        requires_period=kept.requires_period or other.requires_period,
        entities=entities,
        data_period=kept.data_period or other.data_period,
    )


def merge_ai_claims(
    candidates: list[FactualClaim], ai_claims: list[dict], subject: str
) -> list[FactualClaim]:
    """Fold AI-nominated claims into the deterministic candidates.

    Order is stable — deterministic candidates keep their document order, new
    AI claims append in review order — and ids are renumbered CLM-001... so
    downstream evidence/verification records always reference a dense, ordered
    sequence. Duplicate texts (difflib ratio > SIMILARITY_THRESHOLD after
    normalisation) collapse into one record; the richer one wins.
    """
    subject = str(subject or "").strip()
    merged: list[FactualClaim] = [
        replace(c) for c in (candidates or []) if isinstance(c, FactualClaim)
    ]
    for raw in ai_claims or []:
        if not isinstance(raw, dict):
            continue
        ai = _claim_from_ai(raw, subject)
        if ai is None:
            continue
        for i, existing in enumerate(merged):
            if _similar(existing.text, ai.text):
                merged[i] = _merge_pair(existing, ai)
                break
        else:
            merged.append(ai)
    merged = merged[:MAX_CANDIDATES]
    for i, claim in enumerate(merged, start=1):
        claim.claim_id = f"CLM-{i:03d}"
    return merged
