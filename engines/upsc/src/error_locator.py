"""Where in the source a reported error came from.

An error report that says "this fact is wrong" and nothing else costs a reviewer
the whole lecture to check. What makes one verifiable in seconds is the place it
came from: a timestamp in the recording, a slide number, a page of the PDF.

This module turns a reported error into a :class:`SourceLocation` by SEARCHING
the material the run actually has — the timed transcript segments, the parsed
slides — for the text the error quotes. It never calculates a plausible-looking
timestamp, and it never asks a model where something was.

That restraint is the point. A fabricated timestamp is worse than no timestamp:
the reviewer jumps to 00:42:18, hears something unrelated, and now distrusts the
whole report. So a location is returned only when the quoted text is genuinely
found, and everything else reports ``kind="unavailable"`` and says so in words.

The matching is deliberately dull — normalised token overlap, with a floor. It
is looking for "which of these 400 segments contains this sentence", which needs
no cleverness, and a fuzzy matcher that tries harder is a fuzzy matcher that
starts returning confident wrong answers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

#: Below this share of shared tokens, a candidate is not a match. Tuned to be
#: unforgiving: an unmatched error still carries its statement and correction,
#: so the cost of refusing is small, and the cost of a wrong location is a
#: reviewer's trust in every other row.
MIN_MATCH_SCORE = 0.45

#: Words that match everything and therefore locate nothing.
_STOPWORDS = frozenset("""
a an the and or of in on at to for from by with is are was were be been being
this that these those it its as not no but if then than so such which who whom
whose what when where how why can could may might shall should will would must
""".split())

_TOKEN_RE = re.compile(r"[0-9A-Za-zऀ-ॿ]+")

#: A location that could not be established. Written out rather than left blank
#: so a reviewer can tell "we looked and could not find it" from "nobody looked".
TIMESTAMP_UNAVAILABLE = "Timestamp unavailable"
LOCATION_UNAVAILABLE = "Unable to verify from provided source."


def _tokens(text: str) -> set[str]:
    return {t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text or ""))
            if t not in _STOPWORDS and len(t) > 1}


def _score(needle: set[str], haystack: set[str]) -> float:
    """Share of the needle's distinctive words present in the haystack.

    Asymmetric on purpose: a two-minute transcript segment is much longer than
    the sentence being located, so scoring by overlap of the union would punish
    every correct match for the segment's own length.
    """
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


def format_timestamp(seconds: float | None) -> str:
    """Seconds to HH:MM:SS. Returns "" for None, never "00:00:00" as a stand-in."""
    if seconds is None:
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def timestamped_url(video_url: str, seconds: float | None) -> str:
    """A link that opens the recording at ``seconds``, when the host allows it.

    YouTube takes ``t=<seconds>s``; Drive and direct files have no such
    convention, so they get no link rather than a broken one.
    """
    if not video_url or seconds is None:
        return ""
    try:
        start = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    parsed = urlparse(video_url)
    host = (parsed.netloc or "").lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return ""
    query = parse_qs(parsed.query)
    query["t"] = [f"{start}s"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


@dataclass
class SourceLocation:
    """Where one reported error came from, or an honest statement that we
    could not tell."""

    #: video | slide | pdf_page | transcript_segment | unavailable
    kind: str = "unavailable"
    start_seconds: float | None = None
    end_seconds: float | None = None
    segment_index: int | None = None
    slide_no: int | None = None
    slide_title: str = ""
    page_no: int | None = None
    section: str = ""
    #: The text actually found in the source — quoted so the reviewer can see
    #: what was matched rather than trusting the match.
    source_text: str = ""
    url: str = ""
    confidence: float = 0.0
    #: Which haystack produced this: "transcript" | "slides" | "" .
    source_label: str = ""

    @property
    def timestamp(self) -> str:
        start = format_timestamp(self.start_seconds)
        if not start:
            return TIMESTAMP_UNAVAILABLE
        end = format_timestamp(self.end_seconds)
        return f"{start} – {end}" if end and end != start else start

    @property
    def located(self) -> bool:
        return self.kind != "unavailable"

    def describe(self) -> str:
        """One line naming the place, for a table cell or a report heading."""
        if self.kind == "video":
            return f"Lecture recording {self.timestamp}"
        if self.kind == "transcript_segment":
            return f"Transcript segment {self.segment_index}"
        if self.kind == "slide":
            title = f" — \"{self.slide_title}\"" if self.slide_title else ""
            return f"Slide {self.slide_no}{title}"
        if self.kind == "pdf_page":
            section = f", Section: {self.section}" if self.section else ""
            return f"Page {self.page_no}{section}"
        return LOCATION_UNAVAILABLE

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "location": self.describe(),
            "timestamp": self.timestamp if self.kind == "video" else "",
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "segment_index": self.segment_index,
            "slide_no": self.slide_no,
            "slide_title": self.slide_title,
            "page_no": self.page_no,
            "section": self.section,
            "source_text": self.source_text,
            "url": self.url,
            "confidence": round(self.confidence, 2),
            "source_label": self.source_label,
        }


@dataclass
class LocatorSources:
    """Everything a run has that an error could be traced back to.

    Built once per run and reused for every row, because the token sets are the
    expensive part and a lecture has hundreds of segments.
    """

    segments: list[dict] = field(default_factory=list)
    slides: list[dict] = field(default_factory=list)
    video_url: str = ""
    #: pptx | pdf | "" — decides whether a slide match is reported as a slide or
    #: as a page, which is the difference between "Slide 14" and "Page 27".
    slide_source_type: str = ""
    _segment_tokens: list[set[str]] = field(default_factory=list, repr=False)
    _slide_tokens: list[set[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._segment_tokens = [_tokens(s.get("text", "")) for s in self.segments]
        self._slide_tokens = [
            _tokens(f"{s.get('heading', '')} {s.get('text', '')}") for s in self.slides]

    @property
    def has_timestamps(self) -> bool:
        return any(s.get("start") is not None for s in self.segments)

    @property
    def searchable(self) -> bool:
        """Is there any text here at all to match a finding against?

        Separates "we looked and found nothing" from "there was nothing to look
        at" — two results that read identically in a report and mean opposite
        things to the reviewer holding it.
        """
        return any(self._segment_tokens) or any(self._slide_tokens)


def _best(needle: set[str], haystacks: list[set[str]]) -> tuple[int, float]:
    best_index, best_score = -1, 0.0
    for index, hay in enumerate(haystacks):
        score = _score(needle, hay)
        if score > best_score:
            best_index, best_score = index, score
    return best_index, best_score


def locate(query: str, sources: LocatorSources) -> SourceLocation:
    """Find where ``query`` came from, or return an unlocated result.

    ``query`` should be the SOURCE statement the error is about — the thing the
    lecture actually said — falling back to the generated sentence when that is
    all the report gives. Both are searched the same way; what matters is that
    the answer is only ever a place the text was really found.
    """
    needle = _tokens(query)
    if not needle:
        return SourceLocation()

    seg_index, seg_score = _best(needle, sources._segment_tokens)
    slide_index, slide_score = _best(needle, sources._slide_tokens)

    # Slides win ties: they are the source of truth for facts and spellings in
    # this pipeline, and "Slide 14" is a more precise instruction to a reviewer
    # than a two-minute window of speech.
    if slide_index >= 0 and slide_score >= MIN_MATCH_SCORE and slide_score >= seg_score:
        slide = sources.slides[slide_index]
        is_pdf = (sources.slide_source_type or "").lower() == "pdf"
        return SourceLocation(
            kind="pdf_page" if is_pdf else "slide",
            slide_no=None if is_pdf else slide.get("slide_no"),
            page_no=slide.get("slide_no") if is_pdf else None,
            slide_title=("" if is_pdf else (slide.get("heading") or "").strip()),
            section=((slide.get("heading") or "").strip() if is_pdf else ""),
            source_text=_snippet(slide.get("text") or slide.get("heading") or ""),
            confidence=round(slide_score, 2),
            source_label="slides",
        )

    if seg_index >= 0 and seg_score >= MIN_MATCH_SCORE:
        segment = sources.segments[seg_index]
        start, end = segment.get("start"), segment.get("end")
        # A transcript with no timings still locates the error — as a segment
        # number, which is what the source actually supports. This is the case
        # the spec calls out: report the segment, never guess a clock time.
        if start is None:
            return SourceLocation(
                kind="transcript_segment",
                segment_index=seg_index + 1,
                source_text=_snippet(segment.get("text", "")),
                confidence=round(seg_score, 2),
                source_label="transcript",
            )
        return SourceLocation(
            kind="video",
            start_seconds=start,
            end_seconds=end,
            segment_index=seg_index + 1,
            source_text=_snippet(segment.get("text", "")),
            url=timestamped_url(sources.video_url, start),
            confidence=round(seg_score, 2),
            source_label="transcript",
        )

    return SourceLocation()


#: Words per pseudo-segment when a transcript arrives as one undivided block.
#: About a minute of speech — small enough that "segment 14 of 96" points a
#: reviewer at a paragraph rather than at the whole class, and large enough that
#: a quoted sentence still lands inside one of them.
TEXT_SEGMENT_WORDS = 150


def segments_from_text(text: str, words_per_segment: int = TEXT_SEGMENT_WORDS) -> list[dict]:
    """Untimed segments cut from a transcript that arrived without any.

    Captions come with timings; a transcript typed by a user, pasted in, or
    returned by an engine that reports none does not. Those runs used to give
    the locator an EMPTY haystack, so every row came back "unable to verify
    from provided source" — a sentence that reads as "we looked and it was not
    there" when nothing had been looked at at all.

    The segments carry ``start=None`` deliberately. :func:`locate` reports those
    as ``transcript_segment``, which names a position without inventing a clock
    time — the one thing this module refuses to do.
    """
    words = (text or "").split()
    if not words:
        return []
    size = max(1, int(words_per_segment))
    return [
        {"start": None, "end": None, "text": " ".join(words[i:i + size])}
        for i in range(0, len(words), size)
    ]


def _snippet(text: str, limit: int = 320) -> str:
    """A quotable extract. Long enough to recognise, short enough for a cell."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0] + "…"


def locate_row(row: dict, sources: LocatorSources) -> SourceLocation:
    """Locate one Error Report row.

    Tries the correction first and the generated statement second. The
    correction usually quotes what the source really said, which is the text
    that will actually be found in a transcript; the generated statement is the
    version that got it wrong and may share fewer words with the source.
    """
    for key in ("correct_information_correction", "original_statement", "error_concern"):
        candidate = (row.get(key) or "").strip()
        if not candidate:
            continue
        found = locate(candidate, sources)
        if found.located:
            return found
    return SourceLocation()
