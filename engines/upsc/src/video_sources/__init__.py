"""Lecture video sources: one pasted link, three services behind it.

`source_router` classifies and resolves; the adapters know one service each.
YouTube's adapter is src/youtube_transcript.py, which predates this package and
already does the job — it is re-exported here so callers have one import site.
"""
from __future__ import annotations

from .errors import (
    DirectVideoAccessError,
    DriveAccessError,
    DriveDownloadDisabled,
    DriveNotShared,
    DriveQuotaExceeded,
    DriveUnsupportedContent,
    EmptyTranscript,
    FFmpegMissing,
    InvalidVideoURL,
    MediaUnreadable,
    TranscriptionEngineError,
    VideoSourceError,
    YouTubeMediaUnavailable,
)
from .source_router import (
    SOURCE_DIRECT_VIDEO,
    SOURCE_GOOGLE_DRIVE,
    SOURCE_LABELS,
    SOURCE_YOUTUBE,
    MediaSource,
    describe_source,
    detect_video_source,
    resolve_media_source,
    validate_video_url,
)

__all__ = [
    "MediaSource",
    "SOURCE_DIRECT_VIDEO",
    "SOURCE_GOOGLE_DRIVE",
    "SOURCE_LABELS",
    "SOURCE_YOUTUBE",
    "describe_source",
    "detect_video_source",
    "resolve_media_source",
    "validate_video_url",
    "VideoSourceError",
    "InvalidVideoURL",
    "DirectVideoAccessError",
    "DriveAccessError",
    "DriveNotShared",
    "DriveQuotaExceeded",
    "DriveDownloadDisabled",
    "DriveUnsupportedContent",
    "YouTubeMediaUnavailable",
    "FFmpegMissing",
    "MediaUnreadable",
    "TranscriptionEngineError",
    "EmptyTranscript",
]
