"""Direct/CDN video links — CloudFront, S3, signed URLs, plain HTTP files.

Nothing is downloaded. The lecture this was built against is a 750 MB, two-hour
MP4; FFmpeg reads the audio off it remotely at ~56x realtime, so pulling the
whole file first would cost minutes and 750 MB of disk to end up at the same
place. The adapter's whole job is to establish that the URL really serves media
before the audio stage commits to it.
"""
from __future__ import annotations

import logging

import requests

from .errors import DirectVideoAccessError
from .source_router import SOURCE_DIRECT_VIDEO, MediaSource

log = logging.getLogger(__name__)

#: Servers that reject a HEAD or a bare GET from an unknown client are common
#: enough on video CDNs that a browser UA is the difference between working and
#: a 403 the user cannot act on.
_UA = "Mozilla/5.0 (compatible; UPSCNotes/1.0; +https://pw.live)"

#: Content types that are, or can carry, video/audio. `application/octet-stream`
#: is included because plenty of CDNs serve MP4 under it, and a manifest is a
#: text type that FFmpeg nonetheless opens as media.
_MEDIA_PREFIXES = ("video/", "audio/")
_MEDIA_EXACT = frozenset({
    "application/octet-stream",
    "application/mp4",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "application/dash+xml",
    "binary/octet-stream",
})


def _looks_like_media(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        # No content-type at all is not disqualifying — ffprobe decides later.
        return True
    return ct.startswith(_MEDIA_PREFIXES) or ct in _MEDIA_EXACT


def _safe_url(url: str) -> str:
    """A URL fit for a log line: query string dropped.

    Signed CDN links carry their credentials in the query (Signature=,
    X-Amz-Credential=, token=). Logging the full URL would log those.
    """
    return (url or "").split("?")[0]


def total_length(headers) -> int:
    """The file's full size, whether the reply was whole or a range.

    A ranged probe answers Content-Length: 2, which is the size of the two bytes
    asked for, not of the lecture. The real total is the tail of Content-Range.
    """
    content_range = headers.get("Content-Range") or ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    try:
        return int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def check_accessible(url: str, *, timeout: int = 30) -> tuple[str, int]:
    """Confirm the URL serves something. Returns (content_type, content_length).

    HEAD first because it costs one round trip and no body. A fair number of
    origins answer HEAD with 403/405 while serving GET perfectly, so a failed
    HEAD falls through to a one-byte ranged GET rather than being treated as the
    answer.
    """
    headers = {"User-Agent": _UA}
    response = None
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            response = None
    except requests.RequestException as e:
        log.info("[VIDEO] HEAD failed for %s (%s); retrying with a ranged GET",
                 _safe_url(url), e)
        response = None

    if response is None:
        try:
            response = requests.get(
                url, headers={**headers, "Range": "bytes=0-1"},
                timeout=timeout, allow_redirects=True, stream=True,
            )
        except requests.RequestException as e:
            raise DirectVideoAccessError(
                f"Could not reach {_safe_url(url)}: {e}") from e
        finally:
            if response is not None:
                response.close()

    status = response.status_code
    if status in {401, 403}:
        raise DirectVideoAccessError(
            f"{_safe_url(url)} returned HTTP {status} — the link is private or its "
            "signature has expired.",
            user_message=(
                "Unable to access the direct video URL: the server refused the "
                "request. If the link is time-limited or signed, please generate a "
                "fresh one."
            ),
        )
    if status == 404:
        raise DirectVideoAccessError(
            f"{_safe_url(url)} returned HTTP 404.",
            user_message="Unable to access the direct video URL: the file was not found.",
        )
    if status >= 400:
        raise DirectVideoAccessError(f"{_safe_url(url)} returned HTTP {status}.")

    content_type = response.headers.get("Content-Type", "")
    length = total_length(response.headers)

    if not _looks_like_media(content_type):
        raise DirectVideoAccessError(
            f"{_safe_url(url)} served '{content_type}', which is not a media type.",
            user_message=(
                "The lecture video link does not point at a video file. If this is a "
                "sharing or player page, please paste the direct media link instead."
            ),
        )
    return content_type, length


def resolve_direct_video(url: str, *, timeout: int = 30) -> MediaSource:
    """Validate a direct video URL and hand back a streamable MediaSource."""
    content_type, length = check_accessible(url, timeout=timeout)
    log.info("[VIDEO] Direct video reachable: %s (%s, %s bytes)",
             _safe_url(url), content_type or "no content-type", length or "unknown")
    return MediaSource(
        source_type=SOURCE_DIRECT_VIDEO,
        url=url,
        stream_url=url,
        headers={"User-Agent": _UA},
        content_type=content_type,
        content_length=length,
        access_note="direct URL, streamed",
    )
