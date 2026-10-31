"""Turn retrieved raw material into Evidence records tied to one claim.

Three inputs, one output shape:

- local reference-index chunks (EvidenceChunk)      -> source_type "book"
- a fetched official HTML page (FetchResult)        -> source_type "official_domain"
- a fetched official PDF (PdfFetchResult)           -> source_type "official_publication"

Passage selection for pages/PDFs is a deliberately simple token-overlap
score: shared lowercase words longer than 3 characters count 1 each, shared
numbers count 3 each — numbers matter most for factual claims (an Article
number or a percentage is the claim). No embeddings, no model calls: the
scorer only decides WHICH passages the verifier model gets to see; the model
does the actual semantic comparison. Passages that share nothing with the
claim are dropped entirely — padding the prompt with irrelevant text invites
"looks similar" false verifications.

Evidence ids are EV-<claim number>-<seq> so an evidence record is traceable
to its claim by eye in the run artifacts. Callers combining several sources
for one claim pass ``start`` to keep the sequence unique across sources.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .models import Evidence, EvidenceChunk

if TYPE_CHECKING:
    from .models import FactualClaim
    from .page_fetcher import FetchResult
    from .pdf_fetcher import PdfFetchResult

NUMBER_WEIGHT = 3
_MIN_WORD_LEN = 4          # "len>3": words of 4+ characters carry signal
_DEFAULT_PASSAGE_CHARS = 900
# Mirrors pdf_fetcher.SCANNED_CHARS_PER_PAGE: a scanned PDF whose pages
# average under this many extractable characters supports no claim.
_NEAR_EMPTY_CHARS_PER_PAGE = 50

_WORD_RE = re.compile(r"[a-z]+")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim_number(claim: "FactualClaim") -> str:
    m = re.search(r"(\d+)", str(getattr(claim, "claim_id", "") or ""))
    return f"{int(m.group(1)):03d}" if m else "000"


def _evidence_id(claim: "FactualClaim", seq: int) -> str:
    return f"EV-{_claim_number(claim)}-{seq}"


# ------------------------------------------------------------------ scoring

def _claim_tokens(text: str) -> tuple[set[str], set[str]]:
    """(content words, numbers) of a claim, both normalised for comparison."""
    lower = str(text or "").lower()
    words = {w for w in _WORD_RE.findall(lower) if len(w) >= _MIN_WORD_LEN}
    numbers = {n.replace(",", "") for n in _NUM_RE.findall(lower)}
    return words, numbers


def _passage_score(words: set[str], numbers: set[str], passage: str) -> float:
    lower = passage.lower()
    p_words = set(_WORD_RE.findall(lower))
    p_numbers = {n.replace(",", "") for n in _NUM_RE.findall(lower)}
    return float(len(words & p_words) + NUMBER_WEIGHT * len(numbers & p_numbers))


def _hard_split(line: str, target: int) -> list[str]:
    """Split one over-long line near word boundaries (wall-of-text pages)."""
    if len(line) <= int(target * 1.5):
        return [line]
    pieces: list[str] = []
    start = 0
    while start < len(line):
        end = min(start + target, len(line))
        if end < len(line):
            space = line.rfind(" ", start + target // 2, end)
            if space > start:
                end = space
        piece = line[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def _split_passages(text: str, passage_chars: int) -> list[str]:
    """Greedy accumulation of lines into ~passage_chars passages.

    page_fetcher's text has one visual line per line and no blank lines, so
    line accumulation (not blank-line paragraphs) is the splitting unit; a
    blank line, where present, always flushes.
    """
    passages: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        joined = "\n".join(buf).strip()
        if joined:
            passages.append(joined)
        buf = []
        size = 0

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        for piece in _hard_split(line, passage_chars):
            if size and size + len(piece) > passage_chars:
                flush()
            buf.append(piece)
            size += len(piece) + 1
    flush()
    return passages


def _best_passages(
    claim: "FactualClaim", text: str, max_passages: int, passage_chars: int
) -> list[tuple[float, str]]:
    """Top-scoring (score, passage) pairs; zero-overlap passages are dropped."""
    if max_passages <= 0:
        return []
    words, numbers = _claim_tokens(getattr(claim, "text", ""))
    scored: list[tuple[float, int, str]] = []
    for order, passage in enumerate(_split_passages(text, passage_chars)):
        score = _passage_score(words, numbers, passage)
        if score > 0:
            scored.append((score, order, passage))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, passage) for score, _, passage in scored[:max_passages]]


# ------------------------------------------------------------- constructors

def evidence_from_chunks(
    claim: "FactualClaim",
    chunks: list[EvidenceChunk],
    source_books: list[dict],
    *,
    start: int = 1,
) -> list[Evidence]:
    """Wrap local reference-index chunks as book Evidence.

    ``source_books`` (SubjectSources.books) backfills edition/publication
    metadata the index row may lack, matched on the book id.
    """
    books = {str(b.get("id") or ""): b for b in (source_books or [])
             if isinstance(b, dict)}
    out: list[Evidence] = []
    seq = start
    for chunk in chunks or []:
        text = str(getattr(chunk, "text", "") or "").strip()
        if not text:
            continue
        book = books.get(str(getattr(chunk, "source_id", "") or ""), {})
        edition = str(getattr(chunk, "edition", "") or book.get("edition") or "")
        pub_year = str(getattr(chunk, "publication_year", "")
                       or book.get("publication_year") or "")
        out.append(Evidence(
            evidence_id=_evidence_id(claim, seq),
            claim_id=claim.claim_id,
            source_type="book",
            source_name=str(getattr(chunk, "source_name", "")
                            or book.get("name") or chunk.source_id),
            passage=text,
            edition=edition or None,
            publication_date=pub_year or None,
            page_number=getattr(chunk, "page_number", None),
            heading=str(getattr(chunk, "chapter", "") or "") or None,
            retrieved_at=_now_iso(),
            relevance_score=float(getattr(chunk, "score", 0.0) or 0.0),
            approved_source=True,
        ))
        seq += 1
    return out


def evidence_from_page(
    claim: "FactualClaim",
    fetch_result: "FetchResult",
    max_passages: int,
    passage_chars: int = _DEFAULT_PASSAGE_CHARS,
    *,
    start: int = 1,
) -> list[Evidence]:
    """Best-overlapping passages of a fetched official page as Evidence."""
    if getattr(fetch_result, "error", None):
        return []
    text = str(getattr(fetch_result, "text", "") or "")
    if not text.strip():
        return []
    final_url = str(getattr(fetch_result, "final_url", "")
                    or getattr(fetch_result, "url", "") or "")
    try:
        domain = urlsplit(final_url).hostname
    except ValueError:
        domain = None
    title = str(getattr(fetch_result, "title", "") or "")
    out: list[Evidence] = []
    for seq, (score, passage) in enumerate(
        _best_passages(claim, text, max_passages, passage_chars), start=start
    ):
        out.append(Evidence(
            evidence_id=_evidence_id(claim, seq),
            claim_id=claim.claim_id,
            source_type="official_domain",
            source_name=title or domain or final_url,
            passage=passage,
            source_domain=domain,
            source_url=final_url or None,
            publication_date=getattr(fetch_result, "publication_date", None),
            retrieved_at=str(getattr(fetch_result, "retrieved_at", "") or "")
            or _now_iso(),
            relevance_score=score,
            approved_source=True,
        ))
    return out


def evidence_from_pdf(
    claim: "FactualClaim",
    pdf_result: "PdfFetchResult",
    max_passages: int,
    *,
    start: int = 1,
) -> list[Evidence]:
    """Best-overlapping passages of a fetched official PDF as Evidence.

    page_number is preserved 1-based. A scanned PDF whose pages are
    near-empty carries no checkable text — it is skipped outright rather
    than letting the verifier "verify" against nothing.
    """
    if getattr(pdf_result, "error", None) or max_passages <= 0:
        return []
    pages = [str(p) for p in (getattr(pdf_result, "text_pages", None) or [])]
    if not pages:
        return []
    if getattr(pdf_result, "scanned", False):
        avg_chars = sum(len(p.strip()) for p in pages) / len(pages)
        if avg_chars < _NEAR_EMPTY_CHARS_PER_PAGE:
            return []

    words, numbers = _claim_tokens(getattr(claim, "text", ""))
    scored: list[tuple[float, int, int, str]] = []
    order = 0
    for page_number, page_text in enumerate(pages, start=1):
        for passage in _split_passages(page_text, _DEFAULT_PASSAGE_CHARS):
            score = _passage_score(words, numbers, passage)
            if score > 0:
                scored.append((score, order, page_number, passage))
            order += 1
    scored.sort(key=lambda item: (-item[0], item[1]))

    final_url = str(getattr(pdf_result, "final_url", "")
                    or getattr(pdf_result, "url", "") or "")
    try:
        domain = urlsplit(final_url).hostname
    except ValueError:
        domain = None
    out: list[Evidence] = []
    for seq, (score, _, page_number, passage) in enumerate(
        scored[:max_passages], start=start
    ):
        out.append(Evidence(
            evidence_id=_evidence_id(claim, seq),
            claim_id=claim.claim_id,
            source_type="official_publication",
            source_name=domain or final_url,
            passage=passage,
            source_domain=domain,
            source_url=final_url or None,
            publication_date=getattr(pdf_result, "publication_date", None),
            page_number=page_number,
            retrieved_at=str(getattr(pdf_result, "retrieved_at", "") or "")
            or _now_iso(),
            relevance_score=score,
            approved_source=True,
        ))
    return out
