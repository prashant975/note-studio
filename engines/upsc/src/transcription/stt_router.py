"""Pick a speech-to-text engine and run it.

The one place the engine is decided. Source adapters know how to reach media and
nothing about transcription; engines know how to transcribe a file and nothing
about where it came from. This sits between them, so adding an engine touches
this module and the config, and no adapter at all.

Audio is extracted here rather than in each engine because the extraction is
identical for all three sources — only the encoding profile differs, and that is
a property of the engine, not the link.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import (
    TRANSCRIPTION_ENGINE,
    TRANSCRIPTION_ENGINES,
    TRANSCRIPTION_FALLBACK_ENABLED,
)
from ..media.audio_extractor import (
    GEMINI_PROFILE,
    WHISPER_PROFILE,
    AudioProfile,
    extract_audio,
    extract_timeout_for,
    probe_media,
    require_ffmpeg,
)
from ..models import NormalizedTranscript
from ..models import record_issue as _record
from ..utils import ensure_dir
from ..video_sources.errors import TranscriptionEngineError
from ..video_sources.source_router import MediaSource

log = logging.getLogger(__name__)

#: Encoding each engine wants. Gemini's is dictated by the proxy body limit;
#: Whisper reads local files with no such budget, so it gets lossless PCM.
_PROFILES: dict[str, AudioProfile] = {
    "gemini": GEMINI_PROFILE,
    "faster_whisper": WHISPER_PROFILE,
}


def resolve_engine(preferred: str = "", *, client=None) -> tuple[str, str]:
    """Decide which engine will run. Returns (engine, why_not_the_other).

    Availability is checked before the expensive audio extraction, not after, so
    a missing dependency costs a second rather than the whole download.
    """
    wanted = (preferred or TRANSCRIPTION_ENGINE or "gemini").strip().lower()
    if wanted not in TRANSCRIPTION_ENGINES:
        wanted = "gemini"

    if wanted == "faster_whisper":
        from . import faster_whisper_stt

        if faster_whisper_stt.is_available():
            return "faster_whisper", ""
        reason = faster_whisper_stt.unavailable_reason()
        if not TRANSCRIPTION_FALLBACK_ENABLED:
            raise TranscriptionEngineError(
                f"TRANSCRIPTION_ENGINE=faster_whisper but it cannot run: {reason}")
        log.warning("[STT] faster_whisper unavailable (%s); falling back to gemini", reason)
        return "gemini", reason

    if wanted == "gemini" and client is None:
        from . import faster_whisper_stt

        if TRANSCRIPTION_FALLBACK_ENABLED and faster_whisper_stt.is_available():
            log.warning("[STT] No AI client for gemini; falling back to faster_whisper")
            return "faster_whisper", "no AI client was available for Gemini"
        raise TranscriptionEngineError(
            "Gemini transcription needs an AI client, and none was available.")
    return wanted, ""


def transcribe_media(
    media: MediaSource,
    *,
    work_dir: Path,
    client=None,
    engine: str = "",
    language_hint: str = "",
    progress_callback=None,
    issues: list | None = None,
) -> NormalizedTranscript:
    """Media in, normalized transcript out.

    Extracts audio once and hands it to whichever engine is selected. The audio
    file stays in `work_dir` for the duration of the call; the caller's temp
    manager removes it — see transcript.acquire_transcript's finally block.
    """
    require_ffmpeg()
    work_dir = ensure_dir(work_dir)
    engine_name, fallback_reason = resolve_engine(engine, client=client)
    if fallback_reason:
        _record(issues, "stt-engine",
                f"The preferred transcription engine could not run: {fallback_reason}",
                action=f"Transcribed with '{engine_name}' instead.")

    # Prove the stream carries audio before spending minutes extracting it.
    probe = probe_media(media.stream_url or media.url, media.headers)
    log.info("[MEDIA] Media verified: %.1f min, audio=%s",
             probe.duration_seconds / 60, probe.audio_codec or "unknown")

    profile = _PROFILES.get(engine_name, GEMINI_PROFILE)
    audio_path = work_dir / f"lecture_audio{profile.suffix}"
    # Sized to the bytes this has to pull, not to a flat hour: a 2.39 GB Drive
    # recording on a modest link legitimately takes longer than a 750 MB CDN one.
    extract_audio(
        media.stream_url or media.url, audio_path,
        headers=media.headers, profile=profile,
        timeout=extract_timeout_for(media.content_length),
    )

    log.info("[STT] Engine: %s", engine_name)
    transcript = _run_engine(
        engine_name, audio_path,
        client=client, work_dir=work_dir, language_hint=language_hint,
        progress_callback=progress_callback, issues=issues,
    )
    transcript.source_type = media.source_type
    if not transcript.duration_seconds:
        transcript.duration_seconds = probe.duration_seconds
    return transcript


def _run_engine(
    engine_name: str,
    audio_path: Path,
    *,
    client,
    work_dir: Path,
    language_hint: str,
    progress_callback,
    issues: list | None,
) -> NormalizedTranscript:
    """Run one engine, and try the other if it fails outright.

    The fallback is deliberately limited to a total failure. A partial result —
    a lecture with two unreadable chunks — is kept, because re-running the whole
    class on the other engine to recover 50 minutes would cost more than it
    returns, and the gaps are already reported.
    """
    from . import faster_whisper_stt, gemini_stt

    engines = {"gemini": gemini_stt, "faster_whisper": faster_whisper_stt}
    try:
        return engines[engine_name].transcribe(
            audio_path, client=client, work_dir=work_dir,
            language_hint=language_hint, progress_callback=progress_callback,
            issues=issues,
        )
    except Exception as e:  # noqa: BLE001 — decide on the fallback below
        other = "faster_whisper" if engine_name == "gemini" else "gemini"
        can_fall_back = (
            TRANSCRIPTION_FALLBACK_ENABLED
            and (other != "faster_whisper" or faster_whisper_stt.is_available())
            and (other != "gemini" or client is not None)
        )
        if not can_fall_back:
            raise
        log.warning("[STT] Engine '%s' failed (%s); retrying with '%s'", engine_name, e, other)
        _record(issues, "stt-engine", f"Transcription with '{engine_name}' failed: {e}",
                action=f"Retried with '{other}'.")
        try:
            return engines[other].transcribe(
                audio_path, client=client, work_dir=work_dir,
                language_hint=language_hint, progress_callback=progress_callback,
                issues=issues,
            )
        except Exception as second:  # noqa: BLE001
            raise TranscriptionEngineError(
                f"Both transcription engines failed. {engine_name}: {e} | {other}: {second}"
            ) from second
