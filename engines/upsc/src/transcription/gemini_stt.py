"""Transcribe lecture audio with Gemini, through the PW proxy.

Generalised from the YouTube-only fallback this project already had: the prompt,
the chunk failure policy and the gap markers are the originals, because they were
tuned against real classes. What changed is that it now takes any audio file from
any of the three sources, and returns timed segments instead of one blob.

The chunking exists for one hard reason: the proxy runs on Vercel, which rejects
bodies over ~4.5 MB, and base64 inflates by 4/3. 25 minutes of 12 kbps mono Opus
is ~2.4 MB base64 and fits; the same span as 16 kbps MP3 does not.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import MAX_PARALLEL_AI_CALLS
from ..media.audio_extractor import (
    GEMINI_PROFILE,
    AudioChunk,
    chunk_time_range,
    split_audio,
)
from ..models import NormalizedTranscript, TranscriptSegment
from ..models import record_issue as _record
from ..utils import ensure_dir
from ..video_sources.errors import TranscriptionEngineError

log = logging.getLogger(__name__)

ENGINE_NAME = "gemini"


def _transcribe_prompt(language_hint: str, part: int, total: int) -> str:
    """The original prompt, unchanged: it is tuned for Hinglish UPSC classes."""
    lang = (
        "The lecture is in Hindi (often mixed with English terms). Transcribe in Devanagari, "
        "keeping English technical terms in Latin script."
        if (language_hint or "").lower().startswith("hi")
        else "Transcribe in the language actually spoken (Hindi lectures often mix in English terms)."
    )
    return (
        f"Transcribe this audio from a UPSC class lecture (part {part} of {total}).\n"
        f"{lang}\n"
        "Rules:\n"
        "- Write clean, readable prose with proper punctuation and sentence breaks.\n"
        "- Keep everything the teacher explains, including examples and exam tips.\n"
        "- Skip filler, dead air, and classroom chatter (greetings, 'can you hear me', breaks, promotions).\n"
        "- Do not summarise, do not add commentary, do not invent content.\n"
        "- Output ONLY the transcript text."
    )


def transcribe(
    audio_path: Path,
    *,
    client,
    work_dir: Path,
    language_hint: str = "",
    chunk_seconds: int | None = None,
    progress_callback=None,
    issues: list | None = None,
) -> NormalizedTranscript:
    """Split the audio, transcribe each part in order, return one transcript.

    Chunks run sequentially so ordering is guaranteed without bookkeeping; a
    class is only 2-6 chunks, so the wall-clock cost of not parallelising is
    small next to the risk of interleaving a lecture.

    A chunk that fails does NOT abandon the lecture: the error is recorded, a
    marker is left where the audio is missing, and the rest carries on. Losing 25
    minutes of a class is bad; losing all of it because minute 50 hit a 429 is
    worse. Only a total failure raises.
    """
    if client is None:
        raise TranscriptionEngineError(
            "Gemini transcription needs an AI client, and none was available.")

    work_dir = ensure_dir(work_dir)
    from ..config import STT_CHUNK_SECONDS

    chunks: list[AudioChunk] = split_audio(
        audio_path, work_dir / "chunks",
        chunk_seconds=chunk_seconds or STT_CHUNK_SECONDS,
        profile=GEMINI_PROFILE,
    )
    log.info("[STT] Transcription started (engine=%s, %d chunk(s))", ENGINE_NAME, len(chunks))

    # Chunks are transcribed concurrently. They were sequential because ordering
    # had to be guaranteed, but ordering comes from the chunk index, not from the
    # order the replies arrive in — and on a 2-hour class this is five separate
    # multi-minute audio calls waiting on each other for no reason.
    def _transcribe_chunk(chunk: AudioChunk) -> tuple[AudioChunk, str, str]:
        """Returns (chunk, text, failure_reason). Never raises: a failed chunk is
        a gap in the lecture, not the end of it."""
        try:
            resp = client.generate_with_audio(
                _transcribe_prompt(language_hint, chunk.index, len(chunks)), chunk.path)
        except Exception as e:  # noqa: BLE001 — one chunk must not cost the class
            return chunk, "", str(e)
        text = (resp.text or "").strip()
        return chunk, text, "" if text else "returned no text"

    workers = max(1, min(MAX_PARALLEL_AI_CALLS, len(chunks)))
    results: dict[int, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_transcribe_chunk, c) for c in chunks]
        for done, fut in enumerate(as_completed(futures), start=1):
            chunk, text, failure = fut.result()
            results[chunk.index] = (text, failure)
            if progress_callback:
                try:
                    progress_callback(done, len(chunks))
                except Exception:
                    pass

    # Assembled in lecture order, and the issues are recorded in that order too,
    # so the error report reads down the class rather than down the network.
    segments: list[TranscriptSegment] = []
    parts: list[str] = []
    gaps: list[str] = []
    failed = 0

    for chunk in chunks:
        text, failure = results.get(chunk.index, ("", "was never attempted"))
        label, span = f"{chunk.index}/{len(chunks)}", chunk_time_range(chunk)
        if failure or not text:
            failed += 1
            gaps.append(span)
            _record(issues, "audio-chunk",
                    f"Transcription failed on chunk {label}: {failure or 'no text'}",
                    action=f"Marked {span} as unavailable in the transcript. The notes "
                           "covering that stretch of the class are built from the "
                           "slides only.",
                    chunk=label, time_range=span)
            parts.append(f"[transcript unavailable for approximately {span} of the lecture]")
            continue

        parts.append(text)
        # Gemini returns prose, not timings. The chunk's own span is the honest
        # resolution available here — claiming per-sentence timings we did not
        # measure would be worse than a coarse one that is true.
        segments.append(TranscriptSegment(
            start=chunk.offset_seconds,
            end=chunk.offset_seconds + chunk.duration_seconds,
            text=text,
        ))

    if failed == len(chunks):
        raise TranscriptionEngineError(
            f"Transcription produced no text: all {len(chunks)} audio chunk(s) failed.")
    if failed:
        _record(issues, "audio-coverage",
                f"{failed} of {len(chunks)} audio chunk(s) could not be transcribed.",
                action="Notes were generated from the surviving transcript plus the slides.",
                chunks_total=len(chunks), chunks_failed=failed, recoverable=True)

    full_text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not full_text:
        raise TranscriptionEngineError("Transcription produced no text.")

    total_duration = sum(c.duration_seconds for c in chunks)
    log.info("[STT] Transcription completed: %d words, %d chunk(s), %d gap(s)",
             len(full_text.split()), len(chunks), len(gaps))
    return NormalizedTranscript(
        full_text=full_text,
        source_label="Gemini transcription of lecture audio",
        language=language_hint,
        engine=ENGINE_NAME,
        segments=segments,
        duration_seconds=total_duration,
        gaps=gaps,
    )
