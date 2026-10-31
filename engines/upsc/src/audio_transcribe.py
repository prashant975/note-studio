"""Backwards-compatible shim. The implementation moved.

This module used to hold both the FFmpeg work and the Gemini transcription for
YouTube's no-captions fallback. Three video sources and two engines later, those
are two jobs with different reasons to change, so they live apart now:

    FFmpeg / audio  -> src/media/audio_extractor.py
    Gemini STT      -> src/transcription/gemini_stt.py
    engine choice   -> src/transcription/stt_router.py

The names below are re-exported so anything still importing from here keeps
working. New code should import from the modules above.
"""
from __future__ import annotations

from pathlib import Path

from .media.audio_extractor import (
    CHUNK_SECONDS,
    OPUS_BITRATE,
    SAMPLE_RATE,
    AudioExtractionError,
    audio_duration_seconds,
    compress_audio,
    ffmpeg_available,
    split_audio,
)
from .video_sources.errors import TranscriptionEngineError

#: Was raised by this module for every failure in the audio path. Kept as an
#: alias so existing `except AudioTranscriptionError` blocks still catch both
#: the media and the transcription failures they used to.
AudioTranscriptionError = AudioExtractionError

__all__ = [
    "AudioTranscriptionError",
    "AudioExtractionError",
    "CHUNK_SECONDS",
    "OPUS_BITRATE",
    "SAMPLE_RATE",
    "audio_duration_seconds",
    "compress_audio",
    "download_audio",
    "ffmpeg_available",
    "split_audio",
    "transcribe_youtube_audio",
]


def download_audio(youtube_url: str, out_dir: Path) -> Path:
    """Download a YouTube lecture's audio track. See transcript._youtube_media.

    The file arrives in whatever container YouTube served (webm/m4a/…); it is
    no longer re-encoded to Opus here — the transcription layer converts it
    once, straight to the profile its engine needs.
    """
    from .transcript import _youtube_media

    media = _youtube_media(youtube_url, Path(out_dir))
    return Path(media.stream_url)


def transcribe_youtube_audio(
    client,
    youtube_url: str,
    work_dir: Path,
    *,
    language_hint: str = "",
    progress_callback=None,
    issues: list | None = None,
) -> str:
    """Download, transcribe and return a YouTube lecture's audio as plain text.

    Preserved for callers that predate the normalized transcript. Returns the
    text only; use src.transcript.acquire_transcript to get timings and gaps.
    """
    from .transcript import _youtube_media
    from .transcription.stt_router import transcribe_media

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    media = _youtube_media(youtube_url, work_dir)
    try:
        transcript = transcribe_media(
            media, work_dir=work_dir, client=client, engine="gemini",
            language_hint=language_hint, progress_callback=progress_callback,
            issues=issues,
        )
    except TranscriptionEngineError as e:
        raise AudioTranscriptionError(str(e)) from e
    return transcript.full_text
