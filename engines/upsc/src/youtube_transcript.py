"""Fetch a class lecture's transcript from a YouTube link.

This is the fast path for video-based notes: YouTube captions are free and
arrive in ~2 seconds, and because a transcript is *text* it sidesteps the
proxy's ~4 MB body limit entirely (audio would not — see audio_transcribe.py,
the fallback used only when a video has no captions).

No API key is required.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from requests import Session

# Accepts: watch?v=ID, youtu.be/ID, /embed/ID, /live/ID, /shorts/ID, or a bare ID.
_VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|/embed/|/live/|/shorts/|/v/|[?&]v=)([A-Za-z0-9_-]{11})"
)
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hindi first: PW lectures are usually Hindi-medium, and a Hindi track better
# matches the audio than an auto-translated English one.
DEFAULT_LANGUAGE_PREFERENCE = ("hi", "en")

# The caption library leaves requests unbounded by default. A stalled lookup
# must release the notes pipeline so it can use the audio transcription route.
_CAPTION_TIMEOUT = (5, 20)  # connect, read (seconds)


class _CaptionSession(Session):
    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _CAPTION_TIMEOUT
        return super().request(method, url, **kwargs)


class TranscriptUnavailable(RuntimeError):
    """No usable caption track (disabled, none published, or video blocked)."""


# Maps the library's exception names to a short, human reason. Its own messages
# are multi-paragraph dumps that would swamp the app's warning list.
_REASONS = {
    "TranscriptsDisabled": "captions are turned off for this video",
    "NoTranscriptFound": "no caption track in a usable language",
    "VideoUnavailable": "the video is unavailable or private",
    "VideoUnplayable": "the video cannot be played (region/age restricted?)",
    "AgeRestricted": "the video is age-restricted",
    "IpBlocked": "YouTube is blocking this network",
    "RequestBlocked": "YouTube blocked the request (try again later)",
    "PoTokenRequired": "YouTube requires additional verification for this video",
    "InvalidVideoId": "not a valid video ID",
    "ConnectTimeout": "YouTube did not connect before the caption timeout",
    "ReadTimeout": "YouTube did not respond before the caption timeout",
}


def _short_reason(exc: Exception) -> str:
    """One-line reason for a caption failure, for the UI warning list."""
    name = type(exc).__name__
    if name in _REASONS:
        return _REASONS[name]
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else name
    return (first_line[:120] + "…") if len(first_line) > 120 else (first_line or name)


@dataclass
class TranscriptResult:
    text: str
    language_code: str
    is_generated: bool  # True = YouTube ASR (noisier), False = human-published
    video_id: str
    segment_count: int
    duration_seconds: float

    @property
    def source_label(self) -> str:
        kind = "auto-generated" if self.is_generated else "manual"
        return f"YouTube captions ({self.language_code}, {kind})"


def extract_video_id(url_or_id: str) -> str | None:
    """Pull the 11-character video ID out of any common YouTube URL form."""
    value = (url_or_id or "").strip()
    if not value:
        return None
    if _BARE_ID_RE.match(value):
        return value
    m = _VIDEO_ID_RE.search(value)
    return m.group(1) if m else None


def is_youtube_link(value: str) -> bool:
    return extract_video_id(value) is not None


def _pick_transcript(transcript_list, preference: tuple[str, ...]):
    """Prefer a human-published track, then ASR, in the caller's language order.

    Manual captions are markedly cleaner than YouTube's ASR (which drops
    punctuation and mangles terms like "Article 21"), so they win even if the
    ASR track is in a more preferred language.
    """
    available = list(transcript_list)
    for want_generated in (False, True):
        for lang in preference:
            for t in available:
                if t.language_code.startswith(lang) and t.is_generated == want_generated:
                    return t
    # Nothing matched the preferred languages — fall back to any track at all.
    for want_generated in (False, True):
        for t in available:
            if t.is_generated == want_generated:
                return t
    return None


def fetch_transcript(
    url_or_id: str,
    *,
    preference: tuple[str, ...] = DEFAULT_LANGUAGE_PREFERENCE,
) -> TranscriptResult:
    """Return the lecture transcript for a YouTube link.

    Raises TranscriptUnavailable when the video has no usable captions, so the
    caller can fall back to audio transcription.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as e:  # pragma: no cover - dependency missing
        raise TranscriptUnavailable(
            "youtube-transcript-api is not installed. Run: pip install -r requirements.txt"
        ) from e

    video_id = extract_video_id(url_or_id)
    if not video_id:
        raise TranscriptUnavailable(f"Not a recognisable YouTube link or video ID: {url_or_id!r}")

    with _CaptionSession() as http_client:
        try:
            transcript_list = YouTubeTranscriptApi(http_client=http_client).list(video_id)
        except Exception as e:
            raise TranscriptUnavailable(f"No captions for {video_id} — {_short_reason(e)}.") from e

        chosen = _pick_transcript(transcript_list, preference)
        if chosen is None:
            raise TranscriptUnavailable(f"No caption tracks published for {video_id}.")

        try:
            fetched = chosen.fetch()
        except Exception as e:
            raise TranscriptUnavailable(f"Could not fetch captions for {video_id} — {_short_reason(e)}.") from e

    snippets = list(fetched)
    text = " ".join((s.text or "").replace("\n", " ").strip() for s in snippets)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise TranscriptUnavailable(f"Caption track for {video_id} was empty.")

    duration = 0.0
    if snippets:
        last = snippets[-1]
        duration = float(getattr(last, "start", 0) or 0) + float(getattr(last, "duration", 0) or 0)

    return TranscriptResult(
        text=text,
        language_code=chosen.language_code,
        is_generated=bool(chosen.is_generated),
        video_id=video_id,
        segment_count=len(snippets),
        duration_seconds=duration,
    )


def list_available_tracks(url_or_id: str) -> list[dict]:
    """Report the caption tracks a video has (used for diagnostics/UI)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    video_id = extract_video_id(url_or_id)
    if not video_id:
        return []
    with _CaptionSession() as http_client:
        try:
            return [
                {"language_code": t.language_code, "language": t.language, "is_generated": t.is_generated}
                for t in YouTubeTranscriptApi(http_client=http_client).list(video_id)
            ]
        except Exception:
            return []
