"""Lecture video ingestion: link -> media -> transcript.

Network is mocked throughout. The live checks against the real CloudFront and
Drive URLs were run during development and are not repeated here: they are slow,
they depend on someone else's CDN staying up, and a red test that means "the
internet is down" trains people to ignore red tests.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.models import NormalizedTranscript, TranscriptSegment
from src.video_sources import (
    SOURCE_DIRECT_VIDEO,
    SOURCE_GOOGLE_DRIVE,
    SOURCE_YOUTUBE,
    detect_video_source,
    validate_video_url,
)
from src.video_sources.errors import (
    DirectVideoAccessError,
    DriveAccessError,
    DriveUnsupportedContent,
    EmptyTranscript,
    InvalidVideoURL,
)
from src.video_sources.google_drive import extract_file_id


# --------------------------------------------------------------------------
# Source detection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://dysg3c97clm9i.cloudfront.net/a/mp4_muxing/b_720p.mp4",
    # No extension and a signed query — the shape that defeats extension tests.
    "https://cdn.example.net/stream/abc123?Signature=xyz&Expires=1",
    "https://example.com/video",
    "http://10.0.0.5:8080/lecture",
])
def test_direct_video_detected(url):
    assert detect_video_source(url) == SOURCE_DIRECT_VIDEO


@pytest.mark.parametrize("url", [
    "https://drive.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/view",
    "https://drive.google.com/open?id=1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89",
    "https://drive.google.com/uc?id=1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89&export=download",
    "https://docs.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/edit",
    "https://drive.usercontent.google.com/download?id=1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89",
    "drive.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/view",
])
def test_google_drive_detected(url):
    assert detect_video_source(url) == SOURCE_GOOGLE_DRIVE


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
])
def test_youtube_detected(url):
    assert detect_video_source(url) == SOURCE_YOUTUBE


@pytest.mark.parametrize("value", [
    "", "   ", "abc123", "not a url at all",
    "ftp://example.com/v.mp4",       # wrong protocol
    "file:///C:/lecture.mp4",        # local path, not a lecture link
])
def test_unrecognised_input_is_rejected(value):
    assert detect_video_source(value) == ""


def test_validate_raises_with_a_user_message():
    with pytest.raises(InvalidVideoURL) as exc:
        validate_video_url("abc123")
    assert exc.value.user_message == (
        "The provided lecture video link is invalid or unsupported.")


# --------------------------------------------------------------------------
# Google Drive file IDs
# --------------------------------------------------------------------------
def test_file_id_from_every_common_drive_shape():
    wanted = "1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89"
    for url in (
        f"https://drive.google.com/file/d/{wanted}/view",
        f"https://drive.google.com/file/d/{wanted}/view?usp=sharing",
        f"https://drive.google.com/open?id={wanted}",
        f"https://drive.google.com/uc?id={wanted}&export=download",
        f"https://drive.usercontent.google.com/download?id={wanted}&export=download",
        f"https://docs.google.com/file/d/{wanted}/edit",
    ):
        assert extract_file_id(url) == wanted, url


def test_folder_link_is_not_a_file():
    assert extract_file_id(
        "https://drive.google.com/drive/folders/1AAAAAAAAAAAAAAAAAAAA") is None


def test_folder_link_reports_that_it_is_a_folder():
    from src.video_sources.google_drive import resolve_drive_video

    with pytest.raises(DriveUnsupportedContent) as exc:
        resolve_drive_video("https://drive.google.com/drive/folders/1AAAAAAAAAAAAAAAAAAAA")
    assert "folder" in exc.value.user_message.lower()


# --------------------------------------------------------------------------
# Direct video access
# --------------------------------------------------------------------------
def _response(status=200, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    return r


def test_direct_video_accepts_a_cdn_mp4():
    from src.video_sources.direct_video import resolve_direct_video

    with patch("src.video_sources.direct_video.requests.head") as head:
        head.return_value = _response(
            200, {"Content-Type": "video/mp4", "Content-Length": "750656091"})
        media = resolve_direct_video("https://cdn.example.net/lecture_720p.mp4")

    assert media.source_type == SOURCE_DIRECT_VIDEO
    assert media.content_length == 750656091
    # Streamed, not downloaded: FFmpeg is pointed at the original URL.
    assert media.stream_url == "https://cdn.example.net/lecture_720p.mp4"


def test_direct_video_reports_a_refused_link():
    from src.video_sources.direct_video import resolve_direct_video

    with patch("src.video_sources.direct_video.requests.head") as head, \
         patch("src.video_sources.direct_video.requests.get") as get:
        head.return_value = _response(403)
        get.return_value = _response(403)
        with pytest.raises(DirectVideoAccessError) as exc:
            resolve_direct_video("https://cdn.example.net/private.mp4")
    assert "signed" in exc.value.user_message or "refused" in exc.value.user_message


def test_direct_video_rejects_a_web_page():
    from src.video_sources.direct_video import resolve_direct_video

    with patch("src.video_sources.direct_video.requests.head") as head:
        head.return_value = _response(200, {"Content-Type": "text/html; charset=utf-8"})
        with pytest.raises(DirectVideoAccessError) as exc:
            resolve_direct_video("https://example.com/watch-page")
    assert "does not point at a video" in exc.value.user_message


def test_true_size_is_read_from_a_ranged_reply():
    """A ranged probe answers Content-Length: 2. The lecture is not 2 bytes."""
    from src.video_sources.direct_video import total_length

    assert total_length({"Content-Length": "2",
                         "Content-Range": "bytes 0-1/2390594088"}) == 2390594088
    assert total_length({"Content-Length": "750656091"}) == 750656091
    assert total_length({}) == 0


def test_drive_html_reply_means_not_shared():
    from src.video_sources.google_drive import resolve_drive_video

    with patch("src.video_sources.google_drive._access_token", return_value=""), \
         patch("src.video_sources.google_drive.requests.get") as get:
        get.return_value = _response(200, {"Content-Type": "text/html; charset=utf-8"})
        with pytest.raises(DriveAccessError) as exc:
            resolve_drive_video(
                "https://drive.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/view")
    assert "shared" in exc.value.user_message.lower()


def test_drive_public_file_resolves_to_a_download_endpoint():
    from src.video_sources.google_drive import resolve_drive_video

    with patch("src.video_sources.google_drive._access_token", return_value=""), \
         patch("src.video_sources.google_drive.requests.get") as get:
        get.return_value = _response(200, {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="lecture.ts"',
            "Content-Range": "bytes 0-1/2390594088",
        })
        media = resolve_drive_video(
            "https://drive.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/view")

    assert media.file_id == "1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89"
    assert media.content_length == 2390594088
    assert "drive.usercontent.google.com" in media.stream_url
    assert media.access_note == "public link"


# --------------------------------------------------------------------------
# Credentials never leak
# --------------------------------------------------------------------------
def test_bearer_token_goes_to_ffmpeg_as_an_argument_not_in_the_url():
    from src.media.audio_extractor import _header_args

    args = _header_args({"Authorization": "Bearer secret-token",
                         "User-Agent": "UPSCNotes/1.0"})
    assert "-user_agent" in args
    assert "-headers" in args
    joined = " ".join(args)
    assert "Authorization: Bearer secret-token" in joined


def test_redacted_command_hides_tokens_and_signed_queries():
    from src.media.audio_extractor import _redact

    line = _redact([
        "ffmpeg", "-headers", "Authorization: Bearer secret-token\r\n",
        "-user_agent", "UPSCNotes/1.0",
        "-i", "https://cdn.example.net/lecture.mp4?Signature=abc123&Key=xyz",
    ])
    assert "secret-token" not in line
    assert "Signature" not in line
    assert "abc123" not in line
    assert "https://cdn.example.net/lecture.mp4" in line


def test_signed_query_is_stripped_from_log_lines():
    from src.video_sources.direct_video import _safe_url

    assert _safe_url("https://cdn.net/v.mp4?Signature=abc&X-Amz-Credential=k") == (
        "https://cdn.net/v.mp4")


# --------------------------------------------------------------------------
# Chunk timing
# --------------------------------------------------------------------------
def test_chunk_offsets_are_measured_not_assumed():
    """The segment muxer drifts from the nominal length; assuming it accumulates
    error down a long lecture."""
    from src.media.audio_extractor import AudioChunk, chunk_time_range

    chunk = AudioChunk(path=Path("chunk_002.ogg"), index=2,
                       offset_seconds=1499.2, duration_seconds=1500.4)
    # 1499.2 -> 24:59, and 2999.6 -> 49:59. Both truncate rather than round: a
    # gap report should not claim a second of audio it did not cover.
    assert chunk_time_range(chunk) == "24:59-49:59"

    # Third chunk of a long lecture: the hour rolls over correctly.
    later = AudioChunk(path=Path("chunk_003.ogg"), index=3,
                       offset_seconds=2999.6, duration_seconds=1500.0)
    assert chunk_time_range(later) == "49:59-1:14:59"


def test_split_resets_timestamps_on_every_chunk():
    """Regression: without -reset_timestamps each chunk keeps the ORIGINAL
    timeline, so ffprobe reports chunk 3 of a 2-hour class as 75 minutes long.
    Offsets are derived from those durations, so they run away — a real run
    reported its last chunk at "4:10:00-6:11:04" of a 121-minute lecture."""
    from unittest.mock import patch as _patch

    from src.media import audio_extractor

    captured: list[list[str]] = []

    def _fake_run(cmd, what, **kw):
        captured.append(cmd)
        return ""

    with _patch.object(audio_extractor, "_run", _fake_run), \
         _patch.object(audio_extractor, "require_ffmpeg", lambda: None), \
         _patch.object(audio_extractor, "audio_duration_seconds", lambda p: 1500.0), \
         _patch.object(Path, "glob", lambda self, pat: iter([Path("chunk_001.ogg")])):
        audio_extractor.split_audio(Path("in.ogg"), Path("out"), chunk_seconds=1500)

    cmd = captured[0]
    assert "-reset_timestamps" in cmd
    assert cmd[cmd.index("-reset_timestamps") + 1] == "1"


def test_offsets_accumulate_across_a_two_hour_lecture():
    """Five 25-minute chunks must land at 0, 25, 50, 75 and 100 minutes."""
    from src.media.audio_extractor import AudioChunk, chunk_time_range

    chunks = []
    offset = 0.0
    for i, duration in enumerate([1500.0, 1500.0, 1500.0, 1500.0, 1264.1], start=1):
        chunks.append(AudioChunk(Path(f"chunk_{i:03d}.ogg"), i, offset, duration))
        offset += duration

    assert [chunk_time_range(c) for c in chunks] == [
        "0:00-25:00", "25:00-50:00", "50:00-1:15:00",
        "1:15:00-1:40:00", "1:40:00-2:01:04",
    ]
    assert offset == pytest.approx(7264.1)      # the real lecture's length


def test_segment_shift_places_a_chunk_in_the_whole_lecture():
    seg = TranscriptSegment(start=10.0, end=20.0, text="Article 21")
    moved = seg.shifted(1500.0)
    assert (moved.start, moved.end) == (1510.0, 1520.0)
    assert moved.text == "Article 21"
    assert seg.start == 10.0        # the original is untouched


# --------------------------------------------------------------------------
# Normalized transcript
# --------------------------------------------------------------------------
def test_every_source_produces_the_same_shape():
    made = [
        NormalizedTranscript(full_text="a b c", source_type=SOURCE_YOUTUBE,
                             engine="captions"),
        NormalizedTranscript(full_text="a b c", source_type=SOURCE_GOOGLE_DRIVE,
                             engine="gemini"),
        NormalizedTranscript(full_text="a b c", source_type=SOURCE_DIRECT_VIDEO,
                             engine="faster_whisper"),
    ]
    for t in made:
        assert set(t.to_dict()) == {
            "source_type", "source_label", "language", "engine", "duration_seconds",
            "word_count", "gaps", "segments", "full_text",
        }
        assert t.word_count == 3
        assert bool(t) is True


def test_an_empty_transcript_is_falsey():
    assert not NormalizedTranscript(full_text="   ")


def test_validation_rejects_an_empty_transcript():
    from src.transcript import validate_transcript

    with pytest.raises(EmptyTranscript):
        validate_transcript(NormalizedTranscript(full_text=""))


def test_validation_rejects_a_transcript_too_short_to_be_a_lecture():
    from src.transcript import validate_transcript

    with pytest.raises(EmptyTranscript) as exc:
        validate_transcript(NormalizedTranscript(full_text="thank you for watching"))
    assert "4 word" in exc.value.user_message


def test_validation_accepts_a_real_lecture():
    from src.transcript import validate_transcript

    validate_transcript(NormalizedTranscript(full_text=" ".join(["word"] * 5000)))


# --------------------------------------------------------------------------
# Transcript cache — a 2-hour class must not be transcribed twice
# --------------------------------------------------------------------------
def test_cache_round_trips_a_transcript(tmp_path, monkeypatch):
    import src.config as config
    from src.transcript import load_cached, save_cached

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    url = "https://cdn.example.net/lecture_720p.mp4"
    original = NormalizedTranscript(
        full_text=" ".join(["word"] * 100), source_type=SOURCE_DIRECT_VIDEO,
        source_label="Gemini transcription of lecture audio", engine="gemini",
        language="hi", duration_seconds=7264.0, gaps=["50:00-1:15:00"],
        segments=[TranscriptSegment(0.0, 8.4, "hello")],
    )
    save_cached(url, original)

    got = load_cached(url, "gemini")
    assert got is not None
    assert got.full_text == original.full_text
    assert got.gaps == ["50:00-1:15:00"]
    assert got.segments[0].end == 8.4
    # A different engine is a different transcript and must not be served.
    assert load_cached(url, "faster_whisper") is None
    # A different lecture must not be served either.
    assert load_cached("https://cdn.example.net/other.mp4", "gemini") is None


def test_unreadable_cache_entry_is_ignored_not_fatal(tmp_path, monkeypatch):
    import src.config as config
    from src.transcript import _cache_path, load_cached

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    url = "https://cdn.example.net/lecture.mp4"
    path = _cache_path(url, "gemini")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert load_cached(url, "gemini") is None


# --------------------------------------------------------------------------
# Temporary media
# --------------------------------------------------------------------------
def test_media_workspace_is_removed_even_when_the_run_fails(tmp_path):
    from src.transcript import media_workspace

    seen = None
    with pytest.raises(RuntimeError):
        with media_workspace(tmp_path) as workspace:
            seen = workspace
            (workspace / "lecture_audio.wav").write_bytes(b"x" * 1024)
            raise RuntimeError("transcription blew up")
    assert seen is not None and not seen.exists()


def test_media_workspace_is_removed_on_success(tmp_path):
    from src.transcript import media_workspace

    with media_workspace(tmp_path) as workspace:
        (workspace / "chunk_001.ogg").write_bytes(b"x")
        seen = workspace
    assert not seen.exists()


def test_keeping_debug_audio_does_not_suppress_a_transcription_failure(tmp_path):
    from src.transcript import media_workspace

    with pytest.raises(RuntimeError, match="transcription blew up"):
        with media_workspace(tmp_path, keep=True) as workspace:
            (workspace / "lecture_audio.wav").write_bytes(b"audio")
            raise RuntimeError("transcription blew up")
    assert (workspace / "lecture_audio.wav").exists()


# --------------------------------------------------------------------------
# Engine routing
# --------------------------------------------------------------------------
def test_missing_faster_whisper_falls_back_to_gemini():
    from src.transcription.stt_router import resolve_engine

    # Both are patched together: on a machine where faster-whisper really is
    # installed, patching only is_available leaves unavailable_reason saying
    # everything is fine, and the test asserts against a state that cannot occur.
    with patch("src.transcription.faster_whisper_stt.is_available", return_value=False), \
         patch("src.transcription.faster_whisper_stt.unavailable_reason",
               return_value="faster-whisper is not installed."):
        engine, reason = resolve_engine("faster_whisper", client=object())
    assert engine == "gemini"
    assert "not installed" in reason


def test_faster_whisper_is_used_when_it_is_installed():
    from src.transcription.stt_router import resolve_engine

    with patch("src.transcription.faster_whisper_stt.is_available", return_value=True):
        engine, reason = resolve_engine("faster_whisper", client=None)
    assert engine == "faster_whisper"
    assert reason == ""


def test_fallback_can_be_switched_off():
    import src.transcription.stt_router as router
    from src.video_sources.errors import TranscriptionEngineError

    with patch("src.transcription.faster_whisper_stt.is_available", return_value=False), \
         patch.object(router, "TRANSCRIPTION_FALLBACK_ENABLED", False):
        with pytest.raises(TranscriptionEngineError):
            router.resolve_engine("faster_whisper", client=object())


def test_gemini_without_a_client_is_refused_when_nothing_else_can_run():
    from src.transcription.stt_router import resolve_engine
    from src.video_sources.errors import TranscriptionEngineError

    with patch("src.transcription.faster_whisper_stt.is_available", return_value=False):
        with pytest.raises(TranscriptionEngineError):
            resolve_engine("gemini", client=None)


# --------------------------------------------------------------------------
# Backwards compatibility
# --------------------------------------------------------------------------
def test_old_audio_transcribe_names_still_import():
    """Existing code imported these from src.audio_transcribe; it still can."""
    from src.audio_transcribe import (  # noqa: F401
        AudioTranscriptionError,
        audio_duration_seconds,
        compress_audio,
        download_audio,
        ffmpeg_available,
        split_audio,
        transcribe_youtube_audio,
    )


def test_pipeline_still_accepts_the_old_youtube_url_argument():
    import inspect

    from src.pipeline import run_pipeline

    params = inspect.signature(run_pipeline).parameters
    assert "video_url" in params
    assert "youtube_url" in params          # alias kept for existing callers
    assert "require_transcript" in params
