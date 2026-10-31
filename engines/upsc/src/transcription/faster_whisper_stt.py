"""Transcribe lecture audio locally with faster-whisper.

Optional: the dependency is not in requirements.txt because on CPU it runs at
roughly realtime, which makes a two-hour class a two-hour wait, and because
bundling CTranslate2 plus a model would add hundreds of megabytes to the team's
packaged build. `is_available()` reports whether it can run, and the STT router
falls back to Gemini when it cannot.

    pip install faster-whisper
    TRANSCRIPTION_ENGINE=faster_whisper

Whisper decodes long audio natively, so unlike the Gemini path this does not
split the file — there is no request-size budget to respect, and not splitting
means no seams to merge and no boundary text to lose.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

from ..models import NormalizedTranscript, TranscriptSegment
from ..video_sources.errors import TranscriptionEngineError

log = logging.getLogger(__name__)

ENGINE_NAME = "faster_whisper"

#: Nudges Whisper towards the vocabulary a UPSC class actually uses. Whisper
#: conditions on this text, so it biases spelling of terms it would otherwise
#: mangle ("Article 21", "Rajya Sabha") without inventing content — it is a
#: prompt, not a substitution table.
_UPSC_PROMPT = (
    "UPSC civil services lecture. Terms: Article 21, Article 370, Fundamental Rights, "
    "Directive Principles, Rajya Sabha, Lok Sabha, Supreme Court, Parliament, "
    "Constitution, Amendment, Judiciary, Federalism, Preamble, Governor, President, "
    "GDP, Fiscal Deficit, Monetary Policy, RBI, NITI Aayog, Monsoon, Plateau, "
    "Biodiversity, Ecosystem, Mughal, Maurya, Gupta, Harappan, Freedom Struggle, "
    "Non-Cooperation Movement, Panchayati Raj, RTI, PIL, CAG, Election Commission."
)


def is_available() -> bool:
    """True when faster-whisper can actually be imported and used."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def unavailable_reason() -> str:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return ("faster-whisper is not installed. Run: pip install faster-whisper")
    except Exception as e:  # noqa: BLE001
        return f"faster-whisper could not be loaded: {e}"
    return ""


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    """Pick device and precision, preferring CUDA when it is genuinely present.

    "auto" is resolved here rather than handed to the library so the run log can
    record what was actually used — "auto" in a log answers no question later.
    """
    resolved_device = device
    if device == "auto":
        resolved_device = "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                resolved_device = "cuda"
        except Exception:
            resolved_device = "cpu"
    if compute_type and compute_type != "auto":
        return resolved_device, compute_type
    # int8 is the CPU default because float32 buys accuracy that is not audible
    # on a lecture recording at several times the runtime.
    return resolved_device, ("float16" if resolved_device == "cuda" else "int8")


@lru_cache(maxsize=2)
def _load_model(model_size: str, device: str, compute_type: str):
    """Load (and cache) a Whisper model.

    Cached because loading costs seconds and a re-run in the same process would
    otherwise pay it again. Keyed on all three settings so changing one in the
    environment still takes effect.
    """
    from faster_whisper import WhisperModel

    log.info("[STT] Loading faster-whisper model=%s device=%s compute=%s",
             model_size, device, compute_type)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe(
    audio_path: Path,
    *,
    language_hint: str = "",
    progress_callback=None,
    issues: list | None = None,
    **_ignored,
) -> NormalizedTranscript:
    """Transcribe a whole lecture locally, preserving segment timings.

    `**_ignored` keeps the signature interchangeable with the Gemini engine, so
    the router can call either without knowing which it has.
    """
    if not is_available():
        raise TranscriptionEngineError(unavailable_reason())

    from ..config import (
        WHISPER_BEAM_SIZE,
        WHISPER_COMPUTE_TYPE,
        WHISPER_DEVICE,
        WHISPER_LANGUAGE,
        WHISPER_MODEL,
        WHISPER_VAD_FILTER,
    )

    device, compute_type = _resolve_device(WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
    try:
        model = _load_model(WHISPER_MODEL, device, compute_type)
    except Exception as e:  # noqa: BLE001
        raise TranscriptionEngineError(f"Could not load the Whisper model: {e}") from e

    # An explicit setting wins; otherwise the slide-derived hint; otherwise let
    # Whisper detect. Forcing "hi" on a code-switched class makes it transliterate
    # English terms into Devanagari, which is the corruption transcript_cleaner
    # then has to undo — so the hint is only applied when it is unambiguous.
    language = WHISPER_LANGUAGE or (language_hint if language_hint in {"hi", "en"} else "") or None

    log.info("[STT] Transcription started (engine=%s, model=%s, device=%s, lang=%s)",
             ENGINE_NAME, WHISPER_MODEL, device, language or "auto")
    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=WHISPER_BEAM_SIZE,
            vad_filter=WHISPER_VAD_FILTER,
            initial_prompt=_UPSC_PROMPT,
            # Whisper's own condition-on-previous-text loop is what produces the
            # repeated-sentence failure on long lectures; turning it off costs a
            # little cross-sentence context and removes that failure entirely.
            condition_on_previous_text=False,
            word_timestamps=False,
        )
    except Exception as e:  # noqa: BLE001
        raise TranscriptionEngineError(f"faster-whisper failed to start: {e}") from e

    total = float(getattr(info, "duration", 0.0) or 0.0)
    segments: list[TranscriptSegment] = []
    parts: list[str] = []
    last_reported = -1
    try:
        # Lazily evaluated: transcription actually happens as this is consumed.
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            segments.append(TranscriptSegment(
                start=float(seg.start or 0.0), end=float(seg.end or 0.0), text=text))
            parts.append(text)
            if progress_callback and total > 0:
                # Reported as (percent, 100): Whisper decodes the whole file in
                # one pass, so there is no chunk number to count the way the
                # Gemini engine does. Callers read total == 100 as "percent".
                done = int((float(seg.end or 0.0) / total) * 100)
                if done >= last_reported + 5:      # every ~5%, not every segment
                    last_reported = done
                    try:
                        progress_callback(min(done, 100), 100)
                    except Exception:
                        pass
    except Exception as e:  # noqa: BLE001
        if not parts:
            raise TranscriptionEngineError(f"faster-whisper failed: {e}") from e
        # Partial audio already transcribed is worth keeping, exactly as a failed
        # Gemini chunk is: the caller records the gap and carries on.
        from ..models import record_issue as _record

        _record(issues, "audio-chunk",
                f"Local transcription stopped early: {e}",
                action="Notes were generated from the part of the lecture that was "
                       "transcribed, plus the slides.")
        log.warning("[STT] faster-whisper stopped early after %d segment(s): %s",
                    len(parts), e)

    full_text = re.sub(r"[ \t]+", " ", " ".join(parts)).strip()
    if not full_text:
        raise TranscriptionEngineError("faster-whisper produced no text.")

    detected = str(getattr(info, "language", "") or language or "")
    log.info("[STT] Transcription completed: %d words, %d segment(s), language=%s",
             len(full_text.split()), len(segments), detected)
    return NormalizedTranscript(
        full_text=full_text,
        source_label=f"Local Whisper transcription ({WHISPER_MODEL})",
        language=detected,
        engine=ENGINE_NAME,
        segments=segments,
        duration_seconds=total or (segments[-1].end if segments else 0.0),
    )
