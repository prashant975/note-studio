"""Media handling: FFmpeg probing, audio extraction and chunking.

Kept apart from both the video-source adapters (which know about services) and
the transcription engines (which know about models), because all three sources
and both engines need exactly the same audio work done.
"""
from __future__ import annotations

from .audio_extractor import (
    CHUNK_SECONDS,
    GEMINI_PROFILE,
    WHISPER_PROFILE,
    AudioChunk,
    AudioExtractionError,
    AudioProfile,
    MediaProbe,
    audio_duration_seconds,
    chunk_time_range,
    compress_audio,
    extract_audio,
    ffmpeg_available,
    ffprobe_available,
    hms,
    probe_media,
    require_ffmpeg,
    split_audio,
)

__all__ = [
    "AudioChunk",
    "AudioExtractionError",
    "AudioProfile",
    "CHUNK_SECONDS",
    "GEMINI_PROFILE",
    "MediaProbe",
    "WHISPER_PROFILE",
    "audio_duration_seconds",
    "chunk_time_range",
    "compress_audio",
    "extract_audio",
    "ffmpeg_available",
    "ffprobe_available",
    "hms",
    "probe_media",
    "require_ffmpeg",
    "split_audio",
]
