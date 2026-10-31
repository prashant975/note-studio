"""Build retrieval queries and candidate URLs for claim verification.

There is NO web-search API in this project, and scraping search-engine result
pages is off the table (fragile, against their terms, and an unauditable
evidence trail). So retrieval is deliberately conservative and honest:

- build_queries() produces short keyword queries from a claim. They drive the
  LOCAL reference index and are recorded in the run's queries log so a human
  reviewer can re-run them by hand against official portals.
- candidate_urls() constructs only DIRECT, known official endpoints — each
  approved domain's root page, plus the one well-known listing page we can
  vouch for (PIB's all-releases page). It never invents deep URLs: a guessed
  URL that happens to resolve would put unvetted content into evidence logs.

The consequence is accepted by design: most claims will legitimately end
SOURCE_NOT_FOUND, which the framework treats as "flag for human review",
never as "the claim is false".

Every URL returned has passed url_safety.is_approved_url against the
subject's own official_domains — a config typo can therefore drop a domain
but can never widen the fetch surface.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .url_safety import is_approved_url

if TYPE_CHECKING:  # import only for type checkers: keeps this module light
    from .models import FactualClaim
    from .source_router import SubjectSources

# The one non-root endpoint we construct: PIB's press-release listing page.
# It is a stable, documented entry point — not a guessed deep link.
PIB_DOMAIN = "pib.gov.in"
PIB_ALL_RELEASES_URL = "https://www.pib.gov.in/Allrel.aspx"

MAX_QUERIES = 3
_MAX_QUERY_CHARS = 120

# Type-specific keywords that anchor a query to the right kind of official
# source. Keys mirror claim_extractor._TRIGGERS plus the AI-flagged extras.
_TYPE_KEYWORDS: dict[str, str] = {
    "constitutional_article": "Constitution of India Article",
    "schedule": "Constitution of India Schedule",
    "amendment": "Constitutional Amendment Act",
    "legal_provision": "Act section text",
    "judgment": "Supreme Court judgment",
    "quotation": "original text quotation",
    "attribution": "committee commission report",
    "economic_statistic": "official statistics",
    "scheme": "government scheme guidelines",
    "superlative_claim": "official facts",
    "historical_date": "chronology date",
}

_NUMBER_RE = re.compile(r"₹?\s?\d[\d,]*(?:\.\d+)?%?")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]{3,}")


def _add_query(queries: list[str], candidate: str) -> None:
    cleaned = " ".join(str(candidate).split())[:_MAX_QUERY_CHARS].strip()
    if cleaned and cleaned.lower() not in {q.lower() for q in queries}:
        queries.append(cleaned)


def build_queries(claim: "FactualClaim") -> list[str]:
    """1-3 short keyword queries for one claim.

    Built from the claim's extracted entities and numbers plus a type-specific
    keyword; a plain content-word query is the fallback so even a claim with
    no entities still yields one usable query.
    """
    text = str(getattr(claim, "text", "") or "")
    entities = [str(e).strip() for e in (getattr(claim, "entities", None) or [])
                if str(e).strip()]
    numbers = [n.strip() for n in _NUMBER_RE.findall(text)]
    keywords = _TYPE_KEYWORDS.get(str(getattr(claim, "claim_type", "") or ""), "")

    queries: list[str] = []
    _add_query(queries, " ".join(entities[:2] + numbers[:2]))
    if keywords:
        _add_query(queries, " ".join([keywords] + entities[:1] + numbers[:1]))
    # Fallback: the first content words of the claim itself.
    _add_query(queries, " ".join(_WORD_RE.findall(text)[:6]))
    return queries[:MAX_QUERIES]


def candidate_urls(
    claim: "FactualClaim",
    sources: "SubjectSources",
    max_urls: int | None = None,
) -> list[str]:
    """Direct known endpoints on the subject's approved official domains.

    Only domain roots (https://<domain>/) plus PIB's press-release listing
    page — never invented deep URLs (see module docstring). ``claim`` is part
    of the signature for future targeted endpoints but deliberately unused
    today: constructing per-claim URLs would be guessing.

    Every URL is re-checked with url_safety.is_approved_url against the
    subject's own official_domains before being returned.
    """
    del claim  # deliberately unused — see docstring
    domains = list(getattr(sources, "official_domains", None) or [])
    urls: list[str] = []
    for raw in domains:
        d = str(raw).strip().lower().rstrip(".")
        if not d or "/" in d or " " in d or "@" in d:
            continue  # malformed config entry; skip rather than build junk
        root = f"https://{d}/"
        if is_approved_url(root, domains) and root not in urls:
            urls.append(root)
        if (d == PIB_DOMAIN or d.endswith("." + PIB_DOMAIN)) \
                and PIB_ALL_RELEASES_URL not in urls \
                and is_approved_url(PIB_ALL_RELEASES_URL, domains):
            urls.append(PIB_ALL_RELEASES_URL)
    if max_urls is not None and max_urls >= 0:
        urls = urls[:max_urls]
    return urls
