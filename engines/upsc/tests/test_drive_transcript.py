"""Google Drive is transcript-first, exactly like YouTube.

Drive's viewer shows a transcript for most uploaded lectures; reading it costs
three small HTTP requests. The old flow streamed the whole multi-gigabyte file
into an STT pass for the identical words. These tests pin the new order — native
transcript, then STT — and every way the native path must FALL BACK rather than
fail the run.

All network is mocked. The live check against the real 2-hour Drive lecture ran
during development: 11,781 words in 5.5 s, engine=drive_native, no audio stage.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from src.models import NormalizedTranscript, TranscriptSegment
from src.video_sources.google_drive import (
    DriveTranscriptUnavailable,
    _parse_track_xml,
    _pick_track,
    fetch_drive_transcript,
)

DRIVE_URL = "https://drive.google.com/file/d/1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89/view"

#: Shapes taken from the real endpoints, not invented.
GVI_OK = ("status=ok&docid=1_8w4kFL0SoQNhYNu9C78Fb9QHQn1vW89&length_seconds=6897"
          "&ttsurl=https%3A%2F%2Fdrive.google.com%2Ftimedtext%3Fid%3Dabc%26vid%3Dxyz")
GVI_NO_TTS = "status=ok&docid=abc&length_seconds=6897"
GVI_FAIL = "status=fail&reason=This+video+is+unavailable"

TRACK_LIST = ('<?xml version="1.0" encoding="utf-8" ?><transcript_list docid="159">'
              '<track id="0" kind="asr" name="" lang_code="hi" lang_original="hindi"/>'
              '</transcript_list>')
TRACK_LIST_EMPTY = '<?xml version="1.0" encoding="utf-8" ?><transcript_list docid="159"></transcript_list>'

TRACK_XML = ('<?xml version="1.0" encoding="utf-8" ?><transcript>'
             '<text start="59.6" dur="5.4">कनेक्टेड है।</text>'
             '<text start="62" dur="3">ओके</text>'
             '<text start="68.56" dur="7.12">सॉरी थोड़ा गैप हो गया आप लोगों का लेकिन</text>'
             '</transcript>')
TRACK_XML_EMPTY = '<?xml version="1.0" encoding="utf-8" ?><transcript></transcript>'


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.content = text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


def _drive_http(gvi=GVI_OK, track_list=TRACK_LIST, track=TRACK_XML):
    """A fake requests.get that answers the three Drive endpoints."""

    def fake_get(url, **kw):
        if "/playback" in url:
            fields = {k: v[0] for k, v in parse_qs(gvi).items() if v}
            if fields.get("status") != "ok":
                return _Resp("{}", status=403)
            return _Resp(json.dumps({
                "mediaMetadata": {"duration": fields.get("length_seconds", "0") + "s"},
                "timedTextDetails": {"timedTextBaseUrl": fields.get("ttsurl", "")},
            }))
        if "get_video_info" in url:
            return _Resp(gvi)
        if "type=list" in url:
            return _Resp(track_list)
        if "type=track" in url:
            return _Resp(track)
        raise AssertionError(f"unexpected URL fetched: {url.split('?')[0]}")

    return fake_get


# ---------------------------------------------------------------------------
# The adapter itself
# ---------------------------------------------------------------------------
def test_native_transcript_is_fetched_and_normalized():
    with patch("src.video_sources.google_drive.requests.get", _drive_http()), \
         patch("src.video_sources.google_drive._access_token", return_value=""):
        tr = fetch_drive_transcript(DRIVE_URL)

    assert isinstance(tr, NormalizedTranscript)
    assert tr.engine == "drive_native"
    assert tr.source_type == "google_drive"
    assert tr.language == "hi"
    assert "auto-generated" in tr.source_label
    assert len(tr.segments) == 3
    # Timestamps preserved, chronological, in seconds.
    assert tr.segments[0].start == pytest.approx(59.6)
    assert tr.segments[0].end == pytest.approx(65.0)
    assert tr.segments[1].text == "ओके"
    assert [s.start for s in tr.segments] == sorted(s.start for s in tr.segments)
    assert tr.duration_seconds == pytest.approx(6897.0)
    assert "कनेक्टेड" in tr.full_text and "सॉरी" in tr.full_text


def test_current_player_skips_the_removed_video_info_endpoint_and_encodes_track_names():
    calls = []
    name = "Hindi & class 1"
    tracks = '<transcript_list><track lang_code="hi" kind="asr" name="Hindi &amp; class 1"/></transcript_list>'

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "/playback" in url:
            return _Resp(json.dumps({
                "mediaMetadata": {"duration": "11200.466s"},
                "timedTextDetails": {"timedTextBaseUrl": "https://drive.google.com/timedtext?id=signed"},
            }))
        if "type=list" in url:
            return _Resp(tracks)
        assert parse_qs(url.split("?", 1)[1])["name"] == [name]
        return _Resp(TRACK_XML)

    with patch("src.video_sources.google_drive.requests.get", get), \
         patch("src.video_sources.google_drive._access_token", return_value=""):
        transcript = fetch_drive_transcript(DRIVE_URL)
    assert transcript.duration_seconds == pytest.approx(11200.466)
    assert len(calls) == 3
    assert not any("get_video_info" in url for url, _ in calls)


def test_private_player_lookup_uses_the_current_users_drive_token():
    calls = []
    normal_get = _drive_http()

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "/playback" in url and not kwargs["headers"].get("Authorization"):
            return _Resp("{}", status=403)
        return normal_get(url, **kwargs)

    with patch("src.video_sources.google_drive.requests.get", get):
        transcript = fetch_drive_transcript(DRIVE_URL, user_token="current-user-token")
    assert transcript.engine == "drive_native"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer current-user-token"
    assert calls[0][1]["params"]  # Public playback uses Google's viewer key.
    assert calls[1][1]["params"] == {}  # OAuth must not carry another project's key.
    assert not any("get_video_info" in url for url, _ in calls)


def test_private_service_denial_does_not_claim_the_transcript_is_missing():
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "/playback" in url:
            reason = ("SERVICE_DISABLED" if kwargs["headers"].get("Authorization")
                      else "USER_PERMISSION_DENIED")
            return _Resp(json.dumps({"error": {"details": [{"reason": reason}]}}), status=403)
        return _Resp("removed", status=404)

    with patch("src.video_sources.google_drive.requests.get", get):
        with pytest.raises(DriveTranscriptUnavailable) as exc:
            fetch_drive_transcript(DRIVE_URL, user_token="current-user-token")
    assert "private transcript service" in str(exc.value)
    assert "may still be visible in the browser" in str(exc.value)
    assert len(calls) == 3, "the retired endpoint must not be retried with another token"


def test_legacy_player_remains_usable_if_the_new_endpoint_is_unavailable():
    normal_get = _drive_http()

    def get(url, **kwargs):
        if "/playback" in url:
            return _Resp("{}", status=503)
        return normal_get(url, **kwargs)

    with patch("src.video_sources.google_drive.requests.get", get), \
         patch("src.video_sources.google_drive._access_token", return_value=""):
        transcript = fetch_drive_transcript(DRIVE_URL)
    assert transcript.engine == "drive_native"
    assert len(transcript.segments) == 3


def test_player_cannot_send_caption_requests_to_an_unrelated_host():
    with patch("src.video_sources.google_drive.requests.get", return_value=_Resp(json.dumps({
        "mediaMetadata": {"duration": "30s"},
        "timedTextDetails": {"timedTextBaseUrl": "https://unrelated.example/timedtext?id=signed"},
    }))) as get, patch("src.video_sources.google_drive._access_token", return_value=""):
        with pytest.raises(DriveTranscriptUnavailable, match="invalid transcript service URL"):
            fetch_drive_transcript(DRIVE_URL)
    assert get.call_count == 1


@pytest.mark.parametrize("gvi,track_list,track,expect", [
    (GVI_FAIL, TRACK_LIST, TRACK_XML, "refused"),          # player says no
    (GVI_NO_TTS, TRACK_LIST, TRACK_XML, "no transcript"),  # no ttsurl at all
    (GVI_OK, TRACK_LIST_EMPTY, TRACK_XML, "no caption tracks"),
    (GVI_OK, TRACK_LIST, TRACK_XML_EMPTY, "no text"),      # empty track
    (GVI_OK, TRACK_LIST, "not xml at all", "not valid XML"),
])
def test_every_unavailable_shape_raises_the_fallback_signal(gvi, track_list, track, expect):
    with patch("src.video_sources.google_drive.requests.get",
               _drive_http(gvi, track_list, track)), \
         patch("src.video_sources.google_drive._access_token", return_value=""):
        with pytest.raises(DriveTranscriptUnavailable) as err:
            fetch_drive_transcript(DRIVE_URL)
    assert expect in str(err.value)


def test_track_preference_mirrors_youtube_manual_over_asr():
    tracks = [
        {"kind": "asr", "lang_code": "hi"},
        {"lang_code": "en"},                # manual English
    ]
    # A manual track beats ASR even in a less-preferred language.
    assert _pick_track(tracks, ("hi", "en")) == {"lang_code": "en"}
    # With only ASR available, language preference decides.
    only_asr = [{"kind": "asr", "lang_code": "en"}, {"kind": "asr", "lang_code": "hi"}]
    assert _pick_track(only_asr, ("hi", "en"))["lang_code"] == "hi"


def test_track_xml_entities_and_blank_segments():
    xml = ('<transcript><text start="1" dur="2">A &amp;amp; B</text>'
           '<text start="5" dur="1"></text>'
           '<text start="3" dur="1">out of order</text></transcript>')
    segments = _parse_track_xml(xml)
    assert len(segments) == 2                      # blank segment dropped
    assert segments[0].start == 1.0                # re-sorted chronologically
    assert segments[1].start == 3.0
    assert "&" in segments[0].text                 # entities unescaped


# ---------------------------------------------------------------------------
# acquire_transcript routing: native first, STT only when needed
# ---------------------------------------------------------------------------
def _native(words=100):
    return NormalizedTranscript(
        full_text=" ".join(["शब्द"] * words), source_type="google_drive",
        source_label="Google Drive transcript (hi, auto-generated)",
        language="hi", engine="drive_native",
        segments=[TranscriptSegment(59.6, 65.0, "कनेक्टेड है।")],
        duration_seconds=6897.0,
    )


def _stt_result():
    return NormalizedTranscript(
        full_text=" ".join(["word"] * 5000), source_type="google_drive",
        source_label="Gemini transcription of lecture audio", engine="gemini",
        duration_seconds=6897.0,
    )


def _acquire(tmp_path, *, native, stages=None, warnings=None):
    """Run acquire_transcript with the Drive fetch and the STT path both mocked."""
    from src import transcript as transcript_mod

    stt_calls = []

    def fake_transcribe_media(media, **kw):
        stt_calls.append(media)
        return _stt_result()

    fetch_patch = (
        patch("src.video_sources.google_drive.fetch_drive_transcript",
              side_effect=native if isinstance(native, Exception) else None,
              return_value=None if isinstance(native, Exception) else native)
    )
    with fetch_patch, \
         patch("src.transcription.stt_router.transcribe_media", fake_transcribe_media), \
         patch.object(transcript_mod, "resolve_media_source") as fake_resolve:
        from src.video_sources import MediaSource

        fake_resolve.return_value = MediaSource(
            source_type="google_drive", url=DRIVE_URL, stream_url="https://x/m")
        result = transcript_mod.acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, warnings=warnings if warnings is not None else [],
            use_cache=False, stage_callback=(stages.append if stages is not None else None),
        )
    return result, stt_calls


def test_native_success_skips_stt_and_audio_stages(tmp_path):
    """TEST 1 (mocked): transcript found -> no media resolve, no STT."""
    stages, warnings = [], []
    result, stt_calls = _acquire(tmp_path, native=_native(), stages=stages,
                                 warnings=warnings)
    assert result.engine == "drive_native"
    assert stt_calls == [], "STT ran despite a native transcript"
    assert "Retrieving existing transcript" in stages
    assert "Extracting audio" not in stages and "Accessing video" not in stages
    # The ASR caveat is surfaced, same as YouTube auto-captions.
    assert any("auto-generated" in w for w in warnings)


def test_pipeline_passes_the_signed_in_drive_access_to_native_transcript_lookup(tmp_path):
    from src.transcript import acquire_transcript

    with patch("src.video_sources.google_drive.fetch_drive_transcript", return_value=_native()) as native, \
         patch("src.transcription.stt_router.transcribe_media") as stt:
        result = acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, use_cache=False, drive_token="current-user-token")
    native.assert_called_once_with(DRIVE_URL, user_token="current-user-token")
    stt.assert_not_called()
    assert result.engine == "drive_native"


def test_native_unavailable_falls_back_to_stt(tmp_path):
    """TEST 2: no native transcript -> the existing media/STT path runs."""
    stages, warnings = [], []
    result, stt_calls = _acquire(
        tmp_path, native=DriveTranscriptUnavailable("no caption tracks"),
        stages=stages, warnings=warnings)
    assert result.engine == "gemini"
    assert len(stt_calls) == 1, "fallback STT did not run"
    assert "Retrieving existing transcript" in stages     # it was tried first
    assert "Accessing video" in stages                    # then the media path
    assert any("Could not read the existing Drive transcript" in w for w in warnings)


def test_empty_native_transcript_is_rejected_then_stt_runs(tmp_path):
    """TEST 4: a transcript that exists but is unusable must not be accepted."""
    result, stt_calls = _acquire(tmp_path, native=_native(words=4))
    assert result.engine == "gemini"
    assert len(stt_calls) == 1


def test_native_lookup_crash_still_reaches_stt(tmp_path):
    """An unexpected error in the native path must degrade, not kill the run."""
    result, stt_calls = _acquire(tmp_path, native=RuntimeError("parser exploded"))
    assert result.engine == "gemini"
    assert len(stt_calls) == 1


def test_invalid_drive_link_errors_before_any_stt(tmp_path):
    """TEST 3: a bad link is a validation error, never a transcription attempt."""
    from src.transcript import acquire_transcript
    from src.video_sources.errors import InvalidVideoURL

    with patch("src.transcription.stt_router.transcribe_media") as stt:
        with pytest.raises(InvalidVideoURL):
            acquire_transcript("abc123", work_dir=tmp_path, use_cache=False)
    stt.assert_not_called()


def test_force_audio_bypasses_the_native_transcript(tmp_path):
    """force_audio exists because auto-captions corrupt Hinglish terms; it must
    keep skipping the native text for Drive exactly as it does for YouTube."""
    from src import transcript as transcript_mod
    from src.video_sources import MediaSource

    with patch("src.video_sources.google_drive.fetch_drive_transcript") as native, \
         patch("src.transcription.stt_router.transcribe_media",
               return_value=_stt_result()), \
         patch.object(transcript_mod, "resolve_media_source",
                      return_value=MediaSource(source_type="google_drive",
                                               url=DRIVE_URL, stream_url="https://x/m")):
        result = transcript_mod.acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, use_cache=False, force_audio=True)
    native.assert_not_called()
    assert result.engine == "gemini"


# ---------------------------------------------------------------------------
# Cache: retries recheck access without transcribing the lecture again
# ---------------------------------------------------------------------------
def test_native_transcript_is_cached_and_reused(tmp_path, monkeypatch):
    import src.config as config
    from src import transcript as transcript_mod

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, _native())

    with patch("src.video_sources.google_drive.fetch_drive_transcript") as native, \
         patch("src.transcription.stt_router.transcribe_media") as stt, \
         patch.object(transcript_mod, "resolve_media_source") as access:
        result = transcript_mod.acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, use_cache=True)
    access.assert_called_once_with(DRIVE_URL, drive_token="", drive_account="")
    native.assert_not_called()
    stt.assert_not_called()
    assert result.engine == "drive_native"


def test_a_drive_video_transcribed_by_stt_last_time_hits_that_cache(tmp_path, monkeypatch):
    """Native was unavailable on the first run and gemini transcribed it; the
    second run must find THAT cache entry instead of transcribing again."""
    import src.config as config
    from src import transcript as transcript_mod

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, _stt_result())

    with patch("src.video_sources.google_drive.fetch_drive_transcript") as native, \
         patch("src.transcription.stt_router.transcribe_media") as stt, \
         patch("src.transcription.stt_router.resolve_engine",
               return_value=("gemini", "")), \
         patch.object(transcript_mod, "resolve_media_source") as access:
        result = transcript_mod.acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, use_cache=True)
    access.assert_called_once_with(DRIVE_URL, drive_token="", drive_account="")
    stt.assert_not_called()
    assert result.engine == "gemini"
    native.assert_not_called()


@pytest.mark.parametrize("cached", [_native(), _stt_result()])
def test_a_private_drive_cache_is_scoped_to_the_signed_in_account(tmp_path, monkeypatch, cached):
    import src.config as config
    from src import transcript as transcript_mod
    from src.video_sources.errors import DriveNotShared

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, cached, account="teacher@pw.live")
    assert transcript_mod.load_cached(
        DRIVE_URL, cached.engine, account=" TEACHER@PW.LIVE ") is not None
    assert transcript_mod.load_cached(DRIVE_URL, cached.engine, account="other@pw.live") is None
    assert transcript_mod.load_cached(DRIVE_URL, cached.engine) is None

    with patch("src.video_sources.google_drive.fetch_drive_transcript",
               side_effect=DriveTranscriptUnavailable("private")), \
         patch("src.transcription.stt_router.resolve_engine", return_value=("gemini", "")), \
         patch("src.transcription.stt_router.transcribe_media") as stt, \
         patch.object(transcript_mod, "resolve_media_source",
                      side_effect=DriveNotShared("not shared with other account")) as access:
        with pytest.raises(DriveNotShared):
            transcript_mod.acquire_transcript(
                DRIVE_URL, work_dir=tmp_path, drive_token="other-token",
                drive_account="other@pw.live")
    access.assert_called_once_with(
        DRIVE_URL, drive_token="other-token", drive_account="other@pw.live")
    stt.assert_not_called()


@pytest.mark.parametrize("cached", [_native(), _stt_result()])
def test_the_same_account_must_still_have_drive_access_before_cache_reuse(
    tmp_path, monkeypatch, cached,
):
    import src.config as config
    from src import transcript as transcript_mod
    from src.video_sources.errors import DriveNotShared

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, cached, account="teacher@pw.live")
    with patch("src.video_sources.google_drive.fetch_drive_transcript") as native, \
         patch("src.transcription.stt_router.resolve_engine", return_value=("gemini", "")), \
         patch("src.transcription.stt_router.transcribe_media") as stt, \
         patch.object(transcript_mod, "resolve_media_source",
                      side_effect=DriveNotShared("access revoked")) as access:
        with pytest.raises(DriveNotShared):
            transcript_mod.acquire_transcript(
                DRIVE_URL, work_dir=tmp_path, drive_token="user-token",
                drive_account="teacher@pw.live")
    access.assert_called_once_with(
        DRIVE_URL, drive_token="user-token", drive_account="teacher@pw.live")
    native.assert_not_called()
    stt.assert_not_called()


@pytest.mark.parametrize("cached", [_native(), _stt_result()])
def test_an_authorized_account_reuses_its_cache_without_stt(tmp_path, monkeypatch, cached):
    import src.config as config
    from src import transcript as transcript_mod

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, cached, account="teacher@pw.live")
    with patch("src.video_sources.google_drive.fetch_drive_transcript") as native, \
         patch("src.transcription.stt_router.resolve_engine", return_value=("gemini", "")), \
         patch("src.transcription.stt_router.transcribe_media") as stt, \
         patch.object(transcript_mod, "resolve_media_source") as access:
        result = transcript_mod.acquire_transcript(
            DRIVE_URL, work_dir=tmp_path, drive_token="user-token",
            drive_account="teacher@pw.live")
    assert result.full_text == cached.full_text
    access.assert_called_once_with(
        DRIVE_URL, drive_token="user-token", drive_account="teacher@pw.live")
    native.assert_not_called()
    stt.assert_not_called()


def test_readable_native_captions_are_refetched_when_downloads_are_disabled(tmp_path, monkeypatch):
    import src.config as config
    from src import transcript as transcript_mod
    from src.video_sources.errors import DriveDownloadDisabled

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, _native())
    with patch.object(transcript_mod, "resolve_media_source",
                      side_effect=DriveDownloadDisabled("downloads disabled")), \
         patch("src.video_sources.google_drive.fetch_drive_transcript",
               return_value=_native(words=200)) as native:
        result = transcript_mod.acquire_transcript(DRIVE_URL, work_dir=tmp_path)
    assert result.word_count == 200
    native.assert_called_once()


def test_an_old_unowned_drive_cache_is_not_served_to_another_account(tmp_path, monkeypatch):
    import json
    import src.config as config
    from src import transcript as transcript_mod

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    path = transcript_mod._cache_path(DRIVE_URL, "gemini")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_stt_result().to_dict()), encoding="utf-8")
    assert transcript_mod.load_cached(DRIVE_URL, "gemini") is None
    assert transcript_mod.load_cached(DRIVE_URL, "gemini", account="other@pw.live") is None


def test_a_short_cached_transcript_is_retrieved_again_instead_of_failing_the_run(
    tmp_path, monkeypatch,
):
    import src.config as config
    from src import transcript as transcript_mod

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    transcript_mod.save_cached(DRIVE_URL, _native(words=2))
    with patch("src.video_sources.google_drive.fetch_drive_transcript",
               return_value=_native()) as native:
        result = transcript_mod.acquire_transcript(DRIVE_URL, work_dir=tmp_path)
    assert result.word_count == 100
    native.assert_called_once()


def test_youtube_captions_first_still_works(tmp_path):
    """TEST 5: the YouTube flow is untouched by the Drive change."""
    from src import transcript as transcript_mod
    from src.youtube_transcript import TranscriptResult

    fake = TranscriptResult(
        text=" ".join(["word"] * 500), language_code="hi", is_generated=False,
        video_id="dQw4w9WgXcQ", segment_count=100, duration_seconds=600.0)
    with patch.object(transcript_mod, "fetch_transcript", return_value=fake), \
         patch("src.transcription.stt_router.transcribe_media") as stt:
        result = transcript_mod.acquire_transcript(
            "https://youtu.be/dQw4w9WgXcQ", work_dir=tmp_path, use_cache=False)
    stt.assert_not_called()
    assert result.engine == "captions"
    assert result.source_type == "youtube"
