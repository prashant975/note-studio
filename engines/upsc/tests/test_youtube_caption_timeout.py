"""A silent caption server must not hold notes generation indefinitely."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace
import time

import pytest
import requests
import youtube_transcript_api

from src import youtube_transcript as captions


@pytest.fixture
def stalled_caption_server():
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            release.wait(2)
            try:
                self.send_response(200)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/captions"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.parametrize("method", ("get", "post"))
def test_caption_requests_leave_a_server_that_never_responds(
    method, stalled_caption_server, monkeypatch,
):
    monkeypatch.setattr(captions, "_CAPTION_TIMEOUT", (0.1, 0.1))
    started = time.monotonic()
    with captions._CaptionSession() as client:
        with pytest.raises(requests.ReadTimeout):
            getattr(client, method)(stalled_caption_server)
    assert time.monotonic() - started < 1


def test_slow_lookup_becomes_a_caption_failure_the_pipeline_can_recover_from(
    stalled_caption_server, monkeypatch,
):
    monkeypatch.setattr(captions, "_CAPTION_TIMEOUT", (0.1, 0.1))

    class SlowApi:
        def __init__(self, *, http_client):
            self.client = http_client

        def list(self, video_id):
            self.client.get(stalled_caption_server)

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", SlowApi)
    with pytest.raises(captions.TranscriptUnavailable, match="caption timeout") as error:
        captions.fetch_transcript("dQw4w9WgXcQ")
    assert isinstance(error.value.__cause__, requests.ReadTimeout)


def test_caption_selection_and_diagnostics_still_return_the_manual_track(monkeypatch):
    track = SimpleNamespace(
        language_code="hi", language="Hindi", is_generated=False,
        fetch=lambda: [SimpleNamespace(text="First line\nSecond line", start=0, duration=5)],
    )

    class Api:
        def __init__(self, *, http_client):
            assert isinstance(http_client, requests.Session)

        def list(self, video_id):
            return [track]

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", Api)
    result = captions.fetch_transcript("dQw4w9WgXcQ")
    assert result.text == "First line Second line"
    assert result.duration_seconds == 5
    assert not result.is_generated
    assert captions.list_available_tracks("dQw4w9WgXcQ") == [
        {"language_code": "hi", "language": "Hindi", "is_generated": False},
    ]
