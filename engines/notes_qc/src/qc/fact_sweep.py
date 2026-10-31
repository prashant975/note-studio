"""Check the standalone facts — the ones nothing on the page can contradict.

Graded against the author's own marked copy, the app found 26 of 30 seeded
errors. All four misses were the same shape:

    "weighs approximately 500 kg"          (it is 250)
    "examined 12 glaciers between 1982"    (it is 1992)
    "the name was contributed by Thailand" (it is Hong Kong)
    "primary, secondary and tertiary candidates"  (one primary, one waiting-list)

Every other class the app catches has something to check against: a figure that
disagrees with another figure, a total that does not add up, a word the notes
spell two ways. These four have nothing. A lone "500 kg" is consistent with
every other word on the page, so no amount of reading the page can find it —
the checker either happens to know the true weight or it does not.

That is a retrieval problem, and the app can retrieve now: the PW proxy
forwards Claude's server-side web search. So this stage takes the notes, pulls
out the claims that are checkable ON THEIR OWN, and looks them up in batches.

Batched deliberately. One claim per call would be forty calls on a long
document; ten claims per call is four, and a search-capable model handles a
numbered list without losing track. Only claims it reports as WRONG become
rows, each carrying the sources that settled it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

__all__ = ["FactSweepResult", "run_fact_sweep"]

#: Sentences worth checking are short, specific and carry a value: a number, a
#: year, a measurement, or an attribution to a named body or place.
_HAS_VALUE = re.compile(
    r"\b\d{2,4}\b"                       # a year, a count, a figure
    r"|\b\d+(?:\.\d+)?\s*(?:kg|km|kilometres|kilometers|metres|meters|crore|lakh|"
    r"million|billion|percent|%|years?|centuries|century)\b",
    re.I)
#: An attribution — "contributed by X", "built by X", "located in X".
_ATTRIBUTION = re.compile(
    r"\b(?:contributed|developed|built|founded|established|launched|released|"
    r"published|located|situated|headquartered|discovered|written|composed)\s+"
    r"(?:by|in|at)\b", re.I)

#: A provision of a named instrument — "the Committee recommends X", "the Bill
#: provides Y". These carry no figure and no attribution verb, so the two tests
#: above miss them, and one of the four measured misses was exactly this shape:
#: "the Search-cum-Selection Committee ... recommends primary, secondary and
#: tertiary candidates". A search settles it; nothing on the page can.
_PROVISION = re.compile(
    r"\b(?:Bill|Act|Commission|Committee|Authority|Board|Council|Ministry|Policy|"
    r"Scheme|Mission|Rules?|Regulations?)\b[^.]{0,90}?"
    r"\b(?:recommends?|provides?|establishes?|requires?|mandates?|prescribes?|"
    r"stipulates?|empowers?|specifies?)\b", re.I)

#: Sentences to leave alone: the ones whose truth is internal to the document,
#: or that name nothing a search could settle.
_SKIP = re.compile(r"^\s*(?:page \d+|#|\d+[.)]\s*$)", re.I)

_MIN_WORDS = 6
_MAX_WORDS = 60


@dataclass
class FactSweepResult:
    """What the sweep checked and what it found."""

    rows: list[dict] = field(default_factory=list)
    checked: int = 0
    batches: int = 0
    failed_batches: int = 0

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.checked:
            notes.append(
                f"A web fact-check read {self.checked} standalone claim(s) from "
                f"this document — figures, years and attributions that nothing "
                f"on the page could confirm — and found {len(self.rows)} wrong."
            )
        if self.failed_batches:
            notes.append(
                f"The web fact-check could not run for {self.failed_batches} "
                f"batch(es) of claims; those claims were not checked."
            )
        return notes


def _pages(paged_text: str) -> list[tuple[int, str]]:
    """[(page, running text)] with the PDF's hard line-wraps joined up.

    A notes PDF wraps mid-sentence, so reading line by line cuts claims in
    half: "measures about 1.285 metres and weighs" on one line, "approximately
    500 kg" on the next. Splitting THAT into sentences loses the value the
    claim turns on, which is the whole point of the sweep.
    """
    out: list[tuple[int, str]] = []
    page, buffer = 0, []
    for line in str(paged_text or "").splitlines():
        marker = re.match(r"\s*\[Page (\d+)\]", line)
        if marker:
            if buffer:
                out.append((page, " ".join(buffer)))
            page, buffer = int(marker.group(1)), []
            line = line[marker.end():]
        text = " ".join(line.split())
        if text:
            buffer.append(text)
    if buffer:
        out.append((page, " ".join(buffer)))
    return out


def _sentences(paged_text: str) -> list[tuple[int, str]]:
    """[(page, sentence)] — sentences worth putting to a search."""
    out: list[tuple[int, str]] = []
    for page, running in _pages(paged_text):
        # Bullets and sentence ends both start a new claim; the marker
        # scaffolding the reconstruction leaves behind is stripped.
        running = re.sub(r"#H[123]:|#TABLE_(?:START|END)", " ", running)
        for piece in re.split(r"(?<=[.?!])\s+|\s*[•●]\s*|\s+[?]{2}\s+", running):
            piece = " ".join(piece.split()).strip(" -–—:;?")
            words = piece.split()
            if not (_MIN_WORDS <= len(words) <= _MAX_WORDS):
                continue
            if _SKIP.match(piece):
                continue
            if (_HAS_VALUE.search(piece) or _ATTRIBUTION.search(piece)
                    or _PROVISION.search(piece)):
                out.append((page, piece))
    return out


def _dedupe(claims: list[tuple[int, str]], limit: int) -> list[tuple[int, str]]:
    """At most ``limit`` distinct claims, spread ACROSS the document.

    Taking the first N in order would spend the whole budget on the opening
    pages — measured, a 40-claim cap never reached page 8, where one of the
    four misses lives. So the budget is shared out page by page, a round at a
    time, and every page contributes before any page contributes twice.
    """
    seen: set[str] = set()
    by_page: dict[int, list[str]] = {}
    order: list[int] = []
    for page, text in claims:
        key = " ".join(text.lower().split())[:120]
        if key in seen:
            continue
        seen.add(key)
        if page not in by_page:
            by_page[page] = []
            order.append(page)
        by_page[page].append(text)

    out: list[tuple[int, str]] = []
    depth = 0
    while len(out) < limit and any(len(by_page[p]) > depth for p in order):
        for page in order:
            if len(out) >= limit:
                break
            if len(by_page[page]) > depth:
                out.append((page, by_page[page][depth]))
        depth += 1
    out.sort(key=lambda pair: pair[0])
    return out


_PROMPT = """You are fact-checking specific claims taken from a set of Indian competitive-exam
class notes. Search the web for each one and report ONLY the claims that are WRONG.

TODAY'S DATE IS {today}.

For each numbered claim: search for the specific value it asserts — the figure, the year, the
place, the body it credits — and compare. A claim is WRONG only when what you find plainly
contradicts it. If your search does not settle it, leave it out; silence costs nothing here and
a wrong correction costs a reviewer their trust.

Do NOT report:
- a claim that is merely imprecise, rounded, or phrased differently from your source;
- a figure that could be a newer value than the one you found, unless your source is clearly
  about the same edition or period;
- anything you did not actually search for.

CLAIMS:
{claims}

Reply with ONLY a JSON array, no prose around it. One object per WRONG claim:

[
  {{"n": 3,
    "wrong_value": "the exact words in the claim that are wrong",
    "correct_value": "what it should say",
    "why": "one sentence, naming what you found",
    "sources": ["https://..."]}}
]

An empty array [] is the right answer when everything checked out."""


def run_fact_sweep(paged_text: str, *, google_token: str = "", today: str = "",
                   max_claims: int = 70, batch_size: int = 10,
                   lookup=None) -> FactSweepResult:
    """Check the document's standalone claims against the web.

    Returns rows in Error Report shape. Needs a signed-in proxy session (or a
    Gemini key); without one it returns an empty result and the caller carries
    on, because a check that cannot run must change nothing.
    """
    result = FactSweepResult()
    claims = _dedupe(_sentences(paged_text), max_claims)
    if not claims:
        return result

    if lookup is None:
        try:
            from ..grounded_lookup import available, look_up
        except Exception:
            return result
        if not available(google_token):
            return result

        def lookup(question):                       # noqa: E306
            return look_up(question, google_token=google_token)

    if not today:
        try:
            from ..config import now_ist

            today = now_ist().strftime("%d %B %Y")
        except Exception:
            today = "today"

    for start in range(0, len(claims), max(1, batch_size)):
        batch = claims[start:start + max(1, batch_size)]
        numbered = "\n".join(f"{i}. (page {page}) {text}"
                             for i, (page, text) in enumerate(batch, start=1))
        result.batches += 1
        try:
            answer = lookup(_PROMPT.format(today=today, claims=numbered))
        except Exception:
            result.failed_batches += 1
            continue
        if answer is None or not getattr(answer, "grounded", False):
            # Not searched — that is memory again, and memory is what this
            # stage exists to get past.
            result.failed_batches += 1
            continue
        result.checked += len(batch)

        text = str(getattr(answer, "answer", "") or "")
        open_at, close_at = text.find("["), text.rfind("]")
        if open_at < 0 or close_at <= open_at:
            continue
        try:
            found = json.loads(text[open_at:close_at + 1])
        except (ValueError, TypeError):
            continue
        if not isinstance(found, list):
            continue

        for item in found:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if not 1 <= index <= len(batch):
                continue
            page, claim = batch[index - 1]
            why = " ".join(str(item.get("why", "") or "").split())[:400]
            correct = " ".join(str(item.get("correct_value", "") or "").split())[:300]
            wrong = " ".join(str(item.get("wrong_value", "") or "").split())[:160]
            if not why and not correct:
                continue
            sources = [s for s in (item.get("sources") or [])
                       if isinstance(s, str) and s.startswith("http")][:3]
            concern = why or f"'{wrong}' does not match what the sources say."
            if wrong:
                concern = f"{concern} (the wrong part: '{wrong}')"
            result.rows.append({
                "original_statement": claim,
                "error_type": "Factual",
                "severity": "High",
                "error_concern": f"{concern} [checked on the web, page {page}]",
                "correct_information_correction": correct,
                "source_urls": "; ".join(sources),
            })
    return result
