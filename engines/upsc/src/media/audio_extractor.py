"""FFmpeg work: prove a stream carries audio, pull that audio out, split it.

These primitives used to live in src/audio_transcribe.py, which was written when
the only video was a YouTube one and the only transcriber was Gemini. Neither is
true now — three sources feed two engines — so the media handling sits here and
the engines import it. audio_transcribe.py re-exports the old names so existing
callers keep working.

Nothing here *stores* a video: FFmpeg streams the remote URL and writes only the
audio, so a 750 MB / 2-hour lecture becomes a 10 MB file in about two and a half
minutes and no video ever touches the disk.

It does, however, read the whole container over the network. `-vn` discards the
video after decode — it does not stop it being fetched — because audio and video
packets are interleaved. Measured: 44.9 MB pulled where 41.6 MB of container was
expected, and the same 300 s of media took 27.6 s remotely against 5.1 s from
local disk. So extraction time tracks FILE SIZE, not lecture length, which is
why a 2.39 GB Drive recording costs about three times a 750 MB CDN one of the
same duration.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..utils import ensure_dir
from ..video_sources.errors import FFmpegMissing, MediaUnreadable

log = logging.getLogger(__name__)

SAMPLE_RATE = "16000"   # speech; ASR models resample to this anyway
CHANNELS = "1"          # mono: a lecture is one voice, and it halves the bytes

#: 25 min/chunk at 12 kbps keeps each Gemini request ~2.4 MB base64 (budget 4 MB).
CHUNK_SECONDS = 1500
OPUS_BITRATE = "12k"

#: Extraction of a 2-hour lecture runs at 15-56x realtime depending on the CDN,
#: so this is a hang guard, not an expected duration.
EXTRACT_TIMEOUT = 3600
PROBE_TIMEOUT = 180

#: FFmpeg reads the WHOLE container to reach the audio — `-vn` saves CPU, not
#: bandwidth — so extraction time tracks file size, not lecture length. A flat
#: hour was fine for a 750 MB CDN file and not for a 2.39 GB Drive recording:
#: beating it needed a sustained 5.3 Mbps, and a 3-hour one needs ~8 Mbps. So
#: the budget is derived from the actual byte count instead.
MIN_ASSUMED_THROUGHPUT_BPS = 250_000       # 2 Mbps — a slow but working link
MAX_EXTRACT_TIMEOUT = 6 * 3600


def extract_timeout_for(content_length: int = 0) -> int:
    """A hang guard sized to the download this actually has to do."""
    if content_length <= 0:
        return EXTRACT_TIMEOUT
    needed = int(content_length / MIN_ASSUMED_THROUGHPUT_BPS) + 300   # + startup
    return max(EXTRACT_TIMEOUT, min(needed, MAX_EXTRACT_TIMEOUT))


@dataclass
class AudioProfile:
    """How to encode extracted audio, which depends on who will read it.

    Gemini gets aggressively compressed Opus because the PW proxy rejects bodies
    over ~4.5 MB and base64 inflates by 4/3. Whisper runs locally with no such
    budget, so it gets lossless PCM — spending disk to avoid throwing away the
    consonants a 12 kbps encoder discards.
    """

    name: str
    suffix: str
    codec_args: list[str]

    @property
    def glob(self) -> str:
        return f"chunk_*{self.suffix}"


GEMINI_PROFILE = AudioProfile(
    name="opus-12k",
    suffix=".ogg",
    codec_args=["-c:a", "libopus", "-b:a", OPUS_BITRATE],
)
WHISPER_PROFILE = AudioProfile(
    name="pcm-s16le",
    suffix=".wav",
    codec_args=["-c:a", "pcm_s16le"],
)


@dataclass
class MediaProbe:
    duration_seconds: float = 0.0
    has_audio: bool = False
    audio_codec: str = ""
    container: str = ""


class AudioExtractionError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def require_ffmpeg() -> None:
    """Fail with the user-facing message before anything expensive starts."""
    if not ffmpeg_available():
        raise FFmpegMissing("ffmpeg was not found on PATH.")


def _header_args(headers: dict[str, str] | None) -> list[str]:
    """Turn a MediaSource's headers into FFmpeg flags.

    User-Agent has its own flag; everything else goes through -headers. Both are
    passed as arguments, never interpolated into the URL, so a Drive bearer token
    cannot end up in a log line or a run_metadata field.
    """
    if not headers:
        return []
    args: list[str] = []
    extra: list[str] = []
    for key, value in headers.items():
        if key.lower() == "user-agent":
            args += ["-user_agent", str(value)]
        else:
            extra.append(f"{key}: {value}")
    if extra:
        args += ["-headers", "".join(f"{line}\r\n" for line in extra)]
    return args


def _reconnect_args(target: str) -> list[str]:
    """Survive the network blips a two-hour remote read will meet.

    Only valid for FFmpeg's http/https protocols; passing them for a local file
    is an error, so a local path gets none.
    """
    if not str(target).lower().startswith(("http://", "https://")):
        return []
    return [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10",
    ]


def _redact(cmd: list[str]) -> str:
    """A command line safe to log: header values and query strings removed."""
    out: list[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            out.append("<redacted>")
            skip_next = False
            continue
        if item in {"-headers", "-user_agent"}:
            out.append(item)
            skip_next = True
            continue
        out.append(item.split("?")[0] if item.lower().startswith("http") else item)
    return " ".join(out)


def _run(cmd: list[str], what: str, *, timeout: int = EXTRACT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise FFmpegMissing(f"{what}: {cmd[0]} not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioExtractionError(f"{what}: timed out after {timeout}s.") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise AudioExtractionError(f"{what} failed: {tail}")
    return proc.stdout or ""


def probe_media(
    target: str | Path,
    headers: dict[str, str] | None = None,
    *,
    timeout: int = PROBE_TIMEOUT,
) -> MediaProbe:
    """Confirm the stream really carries decodable audio, and for how long.

    This is the authoritative accessibility check. A content-type header can lie
    or be absent on a CDN; a stream ffprobe can open and find an audio track in
    cannot. Running it before the expensive extraction turns "two minutes then a
    cryptic failure" into an immediate, accurate error.
    """
    if not ffprobe_available():
        # Not fatal on its own — extraction will fail with a clearer message.
        log.warning("[MEDIA] ffprobe not on PATH; skipping the media probe")
        return MediaProbe()

    cmd = [
        "ffprobe", "-v", "error",
        *_header_args(headers),
        *_reconnect_args(str(target)),
        "-show_entries", "format=duration,format_name:stream=codec_type,codec_name",
        "-of", "default=noprint_wrappers=1",
        str(target),
    ]
    log.info("[MEDIA] Probing media: %s", _redact(cmd))
    try:
        output = _run(cmd, "Media probe", timeout=timeout)
    except (AudioExtractionError, FFmpegMissing) as e:
        raise MediaUnreadable(f"Could not read the media stream: {e}") from e

    probe = MediaProbe()
    codec_types: list[str] = []
    codec_names: list[str] = []
    for line in output.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "duration":
            try:
                probe.duration_seconds = float(value)
            except ValueError:
                pass
        elif key == "format_name":
            probe.container = value
        elif key == "codec_type":
            codec_types.append(value)
        elif key == "codec_name":
            codec_names.append(value)

    probe.has_audio = "audio" in codec_types
    if probe.has_audio:
        for kind, name in zip(codec_types, codec_names):
            if kind == "audio":
                probe.audio_codec = name
                break
    if not probe.has_audio:
        raise MediaUnreadable(
            "The media stream has no audio track "
            f"(streams found: {', '.join(codec_types) or 'none'})."
        )
    return probe


def extract_audio(
    target: str | Path,
    out_path: Path,
    *,
    headers: dict[str, str] | None = None,
    profile: AudioProfile = GEMINI_PROFILE,
    timeout: int = EXTRACT_TIMEOUT,
) -> Path:
    """Pull the audio track out of a local or remote video into `out_path`.

    `-vn` is what keeps this cheap over the network: the video packets are never
    decoded, so a 720p two-hour lecture costs about as much as its audio.
    """
    require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        *_header_args(headers),
        *_reconnect_args(str(target)),
        "-i", str(target),
        "-vn", "-sn", "-dn",              # no video, subtitle or data streams
        "-ac", CHANNELS, "-ar", SAMPLE_RATE,
        *profile.codec_args,
        str(out_path),
    ]
    log.info("[MEDIA] Audio extraction started (%s)", profile.name)
    log.debug("[MEDIA] %s", _redact(cmd))
    _run(cmd, "Audio extraction", timeout=timeout)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise AudioExtractionError("Audio extraction produced an empty file.")
    log.info("[MEDIA] Audio extraction completed: %s (%.1f MB)",
             out_path.name, out_path.stat().st_size / 1e6)
    return out_path


def compress_audio(src: Path, out_path: Path, *, bitrate: str = OPUS_BITRATE) -> Path:
    """Re-encode to mono Opus at a low bitrate so chunks fit the proxy budget."""
    require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", CHANNELS, "-ar", SAMPLE_RATE,
         "-c:a", "libopus", "-b:a", bitrate, str(out_path)],
        "Audio compression",
    )
    return out_path


def audio_duration_seconds(path: Path) -> float:
    """Duration of a local audio file, 0.0 if it cannot be read."""
    if not ffprobe_available():
        return 0.0
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return float((proc.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


@dataclass
class AudioChunk:
    """One piece of a split lecture, and where it sits in the whole.

    `offset_seconds` is measured, not assumed: the segment muxer cuts on packet
    boundaries, so chunks drift from the nominal length. Assuming the nominal
    value puts every timestamp after the first chunk slightly wrong, and the
    error accumulates down a three-hour lecture.
    """

    path: Path
    index: int
    offset_seconds: float
    duration_seconds: float


def split_audio(
    path: Path,
    out_dir: Path,
    *,
    chunk_seconds: int = CHUNK_SECONDS,
    profile: AudioProfile = GEMINI_PROFILE,
) -> list[AudioChunk]:
    """Split audio into fixed-length chunks, each tagged with its true offset."""
    require_ffmpeg()
    out_dir = ensure_dir(out_dir)
    pattern = str(out_dir / f"chunk_%03d{profile.suffix}")
    _run(
        ["ffmpeg", "-y", "-i", str(path), "-f", "segment",
         "-segment_time", str(chunk_seconds),
         # Without this each chunk keeps the ORIGINAL timeline, so chunk 3 of a
         # 2-hour class reports a duration of 75 minutes rather than 25 — and
         # the measured offsets below then run away from the real lecture. The
         # symptom is a gap reported at "4:10:00" of a two-hour recording.
         "-reset_timestamps", "1",
         "-c", "copy", pattern],
        "Audio split",
    )
    paths = sorted(out_dir.glob(profile.glob))
    if not paths:
        raise AudioExtractionError("Splitting produced no audio chunks.")

    chunks: list[AudioChunk] = []
    offset = 0.0
    for index, chunk_path in enumerate(paths, start=1):
        duration = audio_duration_seconds(chunk_path) or float(chunk_seconds)
        chunks.append(AudioChunk(
            path=chunk_path, index=index,
            offset_seconds=offset, duration_seconds=duration,
        ))
        offset += duration
    log.info("[MEDIA] Audio split into %d chunk(s), %.1f min total",
             len(chunks), offset / 60)
    return chunks


def hms(seconds: float) -> str:
    s = int(max(0.0, seconds))
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chunk_time_range(chunk: AudioChunk) -> str:
    """"50:00-1:15:00" — where in the lecture a chunk sits, for gap reports."""
    return f"{hms(chunk.offset_seconds)}-{hms(chunk.offset_seconds + chunk.duration_seconds)}"
