"""Get a class lecture's transcript from whatever kind of link the user pasted.

Three sources, one normalized transcript. The routing is cheapest-first within
each source rather than a flat order:

  YouTube       captions (free, ~2 s, and text, so the proxy's 4 MB body limit
                never applies) -> audio + STT only if the video has none.
  Google Drive  existing captions first -> audio + STT if they cannot be read.
  Direct video  validate the URL, then the same.

Everything converges on `NormalizedTranscript`, whose `full_text` is the plain
string every downstream stage — cleaning, prompt building, QC — already expects.

Unlike the previous version this raises rather than returning an empty string.
Notes built from a missing transcript are indistinguishable from notes built
from a lecture, which is the one failure a reader cannot catch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from .models import NormalizedTranscript, TranscriptSegment
from .models import record_issue as _record
from .video_sources import (
    SOURCE_GOOGLE_DRIVE,
    SOURCE_YOUTUBE,
    MediaSource,
    resolve_media_source,
    validate_video_url,
)
from .video_sources.errors import (
    DriveDownloadDisabled,
    EmptyTranscript,
    VideoSourceError,
    YouTubeMediaUnavailable,
)
from .youtube_transcript import TranscriptUnavailable, fetch_transcript

log = logging.getLogger(__name__)

#: A transcript below this many words is not a lecture. A 30-minute class is
#: several thousand; single digits mean the recording was silent, the wrong file,
#: or an engine that returned a refusal string.
MIN_TRANSCRIPT_WORDS = 25


# ---------------------------------------------------------------------------
# Temporary media
# ---------------------------------------------------------------------------
@contextmanager
def media_workspace(run_dir: Path, *, keep: bool = False):
    """A scratch directory for audio, removed however the block exits.

    Outside the run folder's zipped contents on purpose: the extracted audio for
    a two-hour lecture is 230 MB as PCM, and leaving it in the run folder would
    put it in the run's ZIP and in every backup of it.
    """
    workspace = Path(run_dir) / "_media_tmp"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        yield workspace
    finally:
        if keep:
            log.info("[MEDIA] Keeping temporary media at %s", workspace)
        else:
            try:
                shutil.rmtree(workspace, ignore_errors=True)
                log.info("[MEDIA] Temporary media cleaned up")
            except Exception as e:  # noqa: BLE001 — cleanup must not fail a good run
                log.warning("[MEDIA] Could not clean up temporary media: %s", e)


# ---------------------------------------------------------------------------
# Transcript cache
# ---------------------------------------------------------------------------
def _cache_key(url: str, engine: str, *, account: str = "") -> str:
    identity = account.strip().lower()
    key = f"{url}|{engine}"
    if identity:
        key = json.dumps([url, engine, identity])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _cache_path(url: str, engine: str, *, account: str = "") -> Path:
    from .config import RUNS_DIR

    return Path(RUNS_DIR) / "_transcript_cache" / f"{_cache_key(url, engine, account=account)}.json"


def load_cached(url: str, engine: str, *, account: str = "") -> NormalizedTranscript | None:
    """A transcript for this exact link and engine from an earlier run.

    Transcribing a two-hour class is the single most expensive step in the
    pipeline. Re-running the same lecture — to change the notes language, to
    retry a failed render — should not pay for it twice. Drive entries are
    scoped to the signed-in account; acquire_transcript checks current access
    before using the cached text.
    """
    path = _cache_path(url, engine, account=account)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("source_type") == SOURCE_GOOGLE_DRIVE and data.get("_cache_version") != 2:
            # Old Drive entries mixed private and public transcripts without
            # ownership. Refetch them rather than migrate unknown permissions.
            return None
        transcript = NormalizedTranscript(
            full_text=data.get("full_text", ""),
            source_type=data.get("source_type", ""),
            source_label=data.get("source_label", ""),
            language=data.get("language", ""),
            engine=data.get("engine", ""),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            gaps=list(data.get("gaps") or []),
            segments=[
                TranscriptSegment(float(s["start"]), float(s["end"]), s["text"])
                for s in data.get("segments") or []
            ],
        )
        if transcript.engine != engine:
            return None
        validate_transcript(transcript)
    except Exception as e:  # noqa: BLE001 — a bad cache entry is not an error
        log.warning("[TRANSCRIPT] Ignoring unreadable cache entry %s: %s", path.name, e)
        return None
    return transcript


def save_cached(url: str, transcript: NormalizedTranscript, *, account: str = "") -> None:
    path = _cache_path(url, transcript.engine, account=account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({**transcript.to_dict(), "_cache_version": 2}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — caching is an optimisation
        log.warning("[TRANSCRIPT] Could not cache the transcript: %s", e)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_transcript(transcript: NormalizedTranscript) -> None:
    """Stop before note generation when transcription produced nothing usable."""
    words = transcript.word_count
    if not transcript.full_text.strip():
        raise EmptyTranscript("Transcription returned no text at all.")
    if words < MIN_TRANSCRIPT_WORDS:
        raise EmptyTranscript(
            f"Transcription returned only {words} word(s), which is not a lecture.",
            user_message=(
                f"Only {words} word(s) could be transcribed from the lecture video, "
                "which is too little to build notes from. Please check that the link "
                "points at the correct recording and that it has audio."
            ),
        )
    log.info("[TRANSCRIPT] Validation passed (%d words, %d segment(s), %d gap(s))",
             words, len(transcript.segments), len(transcript.gaps))


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
def _captions_transcript(url: str, warnings: list, issues: list | None) -> NormalizedTranscript:
    """YouTube's published captions, as a normalized transcript."""
    result = fetch_transcript(url)
    if result.is_generated:
        msg = ("Video captions are auto-generated: they may transliterate English into "
               "Devanagari and corrupt technical terms.")
        warnings.append(
            msg + " The slides are used as the source of truth for facts and spellings.")
        _record(issues, "captions", msg,
                action="Used the captions for the lecture's content, but treated the "
                       "slides as the source of truth for spellings and facts.")
    return NormalizedTranscript(
        full_text=result.text,
        source_type=SOURCE_YOUTUBE,
        source_label=result.source_label,
        language=result.language_code,
        engine="captions",
        duration_seconds=result.duration_seconds,
    )


def _youtube_media(url: str, work_dir: Path) -> MediaSource:
    """Download the lecture's audio track with yt-dlp so STT can read it.

    yt-dlp is the project's existing, supported route to a YouTube video's audio
    and is used only for videos the app can already play. Nothing here attempts
    to defeat an access control: an age-gated, private or DRM-protected video
    fails, and the failure is reported.
    """
    from .media.audio_extractor import require_ffmpeg
    from .video_sources.source_router import SOURCE_YOUTUBE as _YT

    require_ffmpeg()
    try:
        import yt_dlp
    except ImportError as e:
        raise YouTubeMediaUnavailable(
            "yt-dlp is not installed. Run: pip install -r requirements.txt") from e

    out_dir = work_dir / "youtube"
    out_dir.mkdir(parents=True, exist_ok=True)
    # No FFmpegExtractAudio postprocessor, on purpose. The download used to be
    # re-encoded to Opus by yt-dlp and then re-encoded AGAIN to the STT
    # profile by extract_audio — a full second libopus pass (~60 s per lecture
    # hour) whenever YouTube served AAC. The file is downloaded as-is here and
    # converted exactly once, by the same extract_audio every other source uses.
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "lecture_audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        raise YouTubeMediaUnavailable(f"Could not download the lecture audio: {e}") from e

    for candidate in sorted(out_dir.glob("lecture_audio.*")):
        if candidate.suffix.lower() in {".ogg", ".opus", ".m4a", ".webm", ".mp3", ".wav",
                                        ".aac", ".mka", ".mp4"}:
            return MediaSource(
                source_type=_YT, url=url, stream_url=str(candidate),
                access_note="yt-dlp audio download",
            )
    raise YouTubeMediaUnavailable("The audio download produced no usable file.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def acquire_transcript(
    video_url: str,
    *,
    client=None,
    work_dir: Path,
    language_hint: str = "",
    warnings: list[str] | None = None,
    issues: list | None = None,
    engine: str = "",
    force_audio: bool = False,
    use_cache: bool = True,
    progress_callback=None,
    stage_callback=None,
    drive_token: str = "",
    drive_account: str = "",
) -> NormalizedTranscript:
    """Turn a lecture video link into a validated, normalized transcript.

    Raises VideoSourceError (with a `user_message` fit for the page) when the
    link is unusable, the media cannot be reached, or transcription produces
    nothing. Recoverable problems — auto-generated captions, a failed audio chunk
    — are appended to `warnings`/`issues` and do not stop the run.

    `force_audio` skips YouTube captions and transcribes the audio instead:
    auto-captions of a Hinglish class transliterate English into Devanagari and
    corrupt technical terms, so it is worth the time when fidelity matters.

    `drive_token` / `drive_account` are the signed-in person's own Drive access,
    which opens a Google Drive video shared only inside PW (src/drive_auth.py).
    """
    warnings = warnings if warnings is not None else []
    work_dir = Path(work_dir)

    def _stage(label: str) -> None:
        if stage_callback is None:
            return
        try:
            stage_callback(label)
        except Exception:
            pass

    _stage("Detecting video source")
    source_type = validate_video_url(video_url)
    log.info("[VIDEO] Source detected: %s", source_type)

    from .config import TRANSCRIPTION_ENGINE
    from .transcription.stt_router import resolve_engine

    # Engines name the cache entries: the same link read from its native
    # transcript and transcribed by Gemini are different transcripts and must
    # not share one. A source is checked under EVERY engine that could have
    # served it, in the order the live run would try them — otherwise a Drive
    # video whose native transcript was unavailable last time (cached under
    # gemini) would be re-transcribed just because the first key missed.
    cache_engines: list[str] = []
    if source_type == SOURCE_YOUTUBE and not force_audio:
        cache_engines.append("captions")
    if source_type == SOURCE_GOOGLE_DRIVE and not force_audio:
        cache_engines.append("drive_native")
    try:
        stt_engine, _ = resolve_engine(engine or TRANSCRIPTION_ENGINE, client=client)
    except VideoSourceError:
        # Deciding the engine is only needed to name the cache entry here; if
        # it cannot be decided the real attempt below will say why.
        stt_engine = engine or TRANSCRIPTION_ENGINE
    cache_engines.append(stt_engine)
    cache_account = drive_account if source_type == SOURCE_GOOGLE_DRIVE else ""

    if use_cache:
        for cache_engine in cache_engines:
            cached = load_cached(video_url, cache_engine, account=cache_account)
            if cached is not None:
                if cached.source_type != source_type:
                    continue
                if source_type == SOURCE_GOOGLE_DRIVE:
                    # A cache hit saves transcription, not authorization.
                    # Check the current account/public access on every reuse.
                    _stage("Accessing video")
                    try:
                        resolve_media_source(video_url, drive_token=drive_token,
                                             drive_account=drive_account)
                    except DriveDownloadDisabled:
                        if cached.engine == "drive_native":
                            # Reading a currently available caption track can
                            # still be allowed when downloading is disabled.
                            # Fetch it afresh rather than expose cached text.
                            continue
                        raise
                log.info("[TRANSCRIPT] Reusing cached transcript (%d words, engine=%s)",
                         cached.word_count, cached.engine)
                warnings.append(
                    f"Reused the transcript already made for this video "
                    f"({cached.word_count:,} words) instead of transcribing it again.")
                validate_transcript(cached)
                return cached

    # --- YouTube: captions first --------------------------------------------
    if source_type == SOURCE_YOUTUBE and not force_audio:
        _stage("Fetching captions")
        try:
            transcript = _captions_transcript(video_url, warnings, issues)
            if transcript:
                validate_transcript(transcript)
                if use_cache:
                    save_cached(video_url, transcript, account=cache_account)
                log.info("[TRANSCRIPT] YouTube captions used (%d words)", transcript.word_count)
                return transcript
        except TranscriptUnavailable as e:
            detail = f"No YouTube captions: {e}"
            warnings.append(detail)
            _record(issues, "captions", detail,
                    action="Fell back to transcribing the lecture audio.")
        except Exception as e:  # noqa: BLE001 — must not block the audio route
            detail = f"Caption lookup failed unexpectedly: {e}"
            warnings.append(detail)
            _record(issues, "captions", detail,
                    action="Fell back to transcribing the lecture audio.")

    # --- Google Drive: the transcript Drive already made ---------------------
    # Same principle as YouTube captions, for the same reason: the transcript
    # panel in Drive's viewer is real, timed text that costs three small HTTP
    # requests — against streaming a multi-gigabyte file into an STT pass for
    # the identical words. Every failure here falls through to the media route;
    # the native check may never cost a run, only save one.
    if source_type == SOURCE_GOOGLE_DRIVE and not force_audio:
        from .video_sources.google_drive import (
            DriveTranscriptUnavailable,
            fetch_drive_transcript,
        )

        _stage("Retrieving existing transcript")
        drive_fallback = "Falling back to transcribing the lecture audio."
        try:
            transcript = fetch_drive_transcript(video_url, user_token=drive_token)
            # An empty or few-word track is treated as unavailable, not as a
            # fatal EmptyTranscript: the audio may still yield a real
            # transcript, and only after STT also fails should the run stop.
            validate_transcript(transcript)
        except DriveTranscriptUnavailable as e:
            log.info("[DRIVE] Native transcript unavailable (%s)", e)
            log.info("[DRIVE] Falling back to media transcription")
            detail = f"Could not read the existing Drive transcript: {e}"
            warnings.append(detail)
            _record(issues, "drive-transcript", detail, action=drive_fallback)
        except EmptyTranscript as e:
            log.info("[DRIVE] Native transcript rejected as unusable (%s)", e)
            detail = f"The Drive transcript was unusable: {e}"
            warnings.append(detail)
            _record(issues, "drive-transcript", detail, action=drive_fallback)
        except Exception as e:  # noqa: BLE001 — must not block the audio route
            detail = f"Drive transcript lookup failed unexpectedly: {e}"
            warnings.append(detail)
            _record(issues, "drive-transcript", detail, action=drive_fallback)
        else:
            if "auto-generated" in transcript.source_label:
                msg = ("The Drive transcript is auto-generated: it may transliterate "
                       "English into Devanagari and corrupt technical terms.")
                warnings.append(
                    msg + " The slides are used as the source of truth for facts "
                          "and spellings.")
                _record(issues, "drive-transcript", msg,
                        action="Used the transcript for the lecture's content, but "
                               "treated the slides as the source of truth for "
                               "spellings and facts.")
            if use_cache:
                save_cached(video_url, transcript, account=cache_account)
            log.info("[PIPELINE] Passing normalized transcript to note engine "
                     "(%d words, drive_native)", transcript.word_count)
            return transcript

    # --- Everything else: media -> audio -> STT ------------------------------
    from .transcription.stt_router import transcribe_media

    with media_workspace(work_dir) as workspace:
        _stage("Accessing video")
        if source_type == SOURCE_YOUTUBE:
            media = _youtube_media(video_url, workspace)
        else:
            media = resolve_media_source(video_url, drive_token=drive_token,
                                         drive_account=drive_account)

        _stage("Extracting audio")
        transcript = transcribe_media(
            media,
            work_dir=workspace,
            client=client,
            engine=engine,
            language_hint=language_hint,
            progress_callback=progress_callback,
            issues=issues,
        )

    transcript.source_type = source_type
    validate_transcript(transcript)
    if use_cache:
        save_cached(video_url, transcript, account=cache_account)
    log.info("[PIPELINE] Passing normalized transcript to note engine (%d words)",
             transcript.word_count)
    return transcript


def slice_transcript(text: str, n_parts: int, *, overlap: float = 0.1) -> list[str]:
    """Split a transcript into `n_parts` slices aligned to consecutive slide chunks.

    Slides are sent to the AI in small image chunks (the 4 MB limit), but the
    transcript is one long stream, so it is divided proportionally on the
    assumption the class moves through the slides roughly linearly. That is
    approximate, so neighbouring slices overlap by `overlap` to avoid dropping an
    explanation that straddles a boundary.

    Slicing (rather than resending the whole transcript per chunk) means the
    transcript is paid for once, not once per chunk.
    """
    text = (text or "").strip()
    if not text:
        return [""] * max(1, n_parts)
    if n_parts <= 1:
        return [text]
    words = text.split()
    if len(words) < n_parts:
        return [text] + [""] * (n_parts - 1)
    per = len(words) / n_parts
    pad = int(per * max(0.0, overlap))
    slices: list[str] = []
    for i in range(n_parts):
        start = max(0, int(i * per) - pad)
        end = min(len(words), int((i + 1) * per) + pad)
        slices.append(" ".join(words[start:end]))
    return slices
