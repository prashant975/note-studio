"""Speech-to-text: one router, two interchangeable engines.

Engines expose the same `transcribe(audio_path, ...) -> NormalizedTranscript`,
so the router can swap one for the other without either knowing about it.
"""
from __future__ import annotations

from .stt_router import resolve_engine, transcribe_media

__all__ = ["resolve_engine", "transcribe_media"]
