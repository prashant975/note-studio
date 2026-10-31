"""Decide what kind of link the user pasted, then hand back readable media.

One field on the page, three kinds of link behind it. Classification is by
**host**, never by file extension: a CloudFront or signed CDN URL routinely ends
in a query string rather than ".mp4", and an extension test would send it to the
wrong adapter — or reject a perfectly good lecture.

`detect_video_source` is pure and does no I/O, so it is cheap to call from the UI
on every keystroke. `resolve_media_source` is the one that touches the network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .errors import InvalidVideoURL

log = logging.getLogger(__name__)

SOURCE_YOUTUBE = "youtube"
SOURCE_GOOGLE_DRIVE = "google_drive"
SOURCE_DIRECT_VIDEO = "direct_video"

SOURCE_LABELS = {
    SOURCE_YOUTUBE: "YouTube",
    SOURCE_GOOGLE_DRIVE: "Google Drive",
    SOURCE_DIRECT_VIDEO: "Direct video URL",
}

_DRIVE_HOSTS = frozenset({
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
})

_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
})


@dataclass
class MediaSource:
    """A resolved handle on the lecture's media, ready for FFmpeg.

    `stream_url` is what FFmpeg should open, which is not always what the user
    pasted — a Drive share link has to become a download endpoint first. Keeping
    the original in `url` means logs and the run metadata still say what the
    person actually gave us.
    """

    source_type: str
    url: str
    stream_url: str = ""
    #: Sent to FFmpeg with -headers. Carries the Bearer token for a private Drive
    #: file, so credentials never appear in the URL (and so never reach a log).
    headers: dict[str, str] = field(default_factory=dict)
    file_id: str = ""       # Google Drive
    video_id: str = ""      # YouTube
    content_type: str = ""
    content_length: int = 0
    duration_seconds: float = 0.0
    #: How the media is reachable, for the run log ("public link", "service account").
    access_note: str = ""

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source_type, self.source_type)


def _normalise(url: str) -> str:
    """Add a scheme when the user pasted a bare host, reject anything that is
    not a link at all.

    Returns "" for input that cannot be a URL, which is what separates
    "drive.google.com/file/d/..." (fine, just missing https://) from "abc123"
    (not a link, and must not be quietly turned into https://abc123).
    """
    value = (url or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    # No scheme: only treat it as a URL if the first path-free segment looks
    # like a hostname. Without this test, any typed word becomes a valid host.
    host_part = value.split("/")[0].split("?")[0]
    if "." not in host_part or host_part.startswith(".") or host_part.endswith("."):
        return ""
    return f"https://{value}"


def detect_video_source(url: str) -> str:
    """Classify a lecture link. Returns "" when it is not a usable video link.

    Pure: no network, no filesystem. Host-based, so query strings, signed URLs
    and extensionless CDN paths all classify correctly.
    """
    value = (url or "").strip()
    if not value:
        return ""

    # A bare 11-character YouTube ID is something people do paste. Checked before
    # URL parsing because it has no host to parse.
    if "/" not in value and "." not in value and "://" not in value:
        from ..youtube_transcript import extract_video_id

        return SOURCE_YOUTUBE if extract_video_id(value) else ""

    normalised = _normalise(value)
    if not normalised:
        return ""
    try:
        parsed = urlparse(normalised)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""

    if host in _DRIVE_HOSTS:
        return SOURCE_GOOGLE_DRIVE
    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return SOURCE_YOUTUBE
    return SOURCE_DIRECT_VIDEO


def validate_video_url(url: str) -> str:
    """detect_video_source, but raises the user-facing error instead of "" ."""
    source = detect_video_source(url)
    if not source:
        raise InvalidVideoURL(f"Unrecognised lecture video link: {url!r}")
    return source


def describe_source(url: str) -> str:
    """Human label for a link, for UI hints. "" when unrecognised."""
    return SOURCE_LABELS.get(detect_video_source(url), "")


def resolve_media_source(url: str, *, timeout: int = 30, drive_token: str = "",
                         drive_account: str = "") -> MediaSource:
    """Validate a link against its own service and return FFmpeg-readable media.

    Raises the matching VideoSourceError subclass — the caller turns
    `user_message` into what the page shows. `drive_token` / `drive_account` are
    the signed-in person's own Drive access (src/drive_auth.py), used only for a
    Google Drive link.

    YouTube is deliberately NOT resolved here: its captions path needs no media
    at all, so making it fetch a stream up front would spend minutes on a request
    the fast path never uses. See transcript.acquire_transcript for that order.
    """
    source = validate_video_url(url)
    # debug, not info: acquire_transcript has already announced the source, and
    # it announces it on the captions path too, where this function never runs.
    log.debug("[VIDEO] Source detected: %s", source)

    if source == SOURCE_GOOGLE_DRIVE:
        from .google_drive import resolve_drive_video

        media = resolve_drive_video(url, timeout=timeout, user_token=drive_token,
                                    user_email=drive_account)
    elif source == SOURCE_DIRECT_VIDEO:
        from .direct_video import resolve_direct_video

        media = resolve_direct_video(url, timeout=timeout)
    else:
        from ..youtube_transcript import extract_video_id

        media = MediaSource(
            source_type=SOURCE_YOUTUBE,
            url=url,
            video_id=extract_video_id(url) or "",
        )
    log.info("[VIDEO] Validation passed (%s)", media.access_note or media.label)
    return media
