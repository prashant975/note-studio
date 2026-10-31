"""The two chunked stages run their calls concurrently — and still in order.

Transcribing a long lecture's audio and writing the notes for each stretch of it
are both independent per chunk, and both used to run one call after another. On a
2-hour class that is ten multi-minute calls in series.

Parallelising them puts a specific bug within reach: replies arrive in whatever
order the network returns them, and a lecture assembled in *that* order is
silently scrambled — every sentence present, the argument destroyed. So the
tests below deliberately make the replies arrive backwards.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.media.audio_extractor import AudioChunk
from src.models import record_issue  # noqa: F401 - imported for symmetry with src


class RecordingClient:
    """Fake Gemini client that finishes chunks in REVERSE order and counts overlap.

    The reverse ordering is the point: if any stage keeps results in completion
    order rather than chunk order, these tests fail loudly instead of producing a
    plausible-looking, wrongly-ordered lecture.
    """

    def __init__(self, *, delay_unit=0.05, fail_on=(), empty_on=()):
        self.delay_unit = delay_unit
        self.fail_on = set(fail_on)
        self.empty_on = set(empty_on)
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def _enter(self, index: int):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls.append(index)

    def _exit(self):
        with self._lock:
            self.in_flight -= 1

    def _work(self, index: int, total: int) -> str:
        self._enter(index)
        try:
            # Later chunks finish FIRST.
            time.sleep(self.delay_unit * (total - index + 1))
            if index in self.fail_on:
                raise RuntimeError(f"chunk {index} exploded")
            if index in self.empty_on:
                return ""
            return f"PART{index}"
        finally:
            self._exit()

    # --- notes generation ---------------------------------------------------
    def generate(self, prompt, images=None, *, max_images=0, max_output_tokens=0,
                 temperature=0.0):
        index = int(str(prompt).split("INDEX=")[1].split()[0])
        total = int(str(prompt).split("TOTAL=")[1].split()[0])
        return _Resp(self._work(index, total))

    # --- audio transcription ------------------------------------------------
    def generate_with_audio(self, prompt, audio_path, **kw):
        index = int(Path(audio_path).stem.split("_")[-1])
        total = int(str(prompt).split(" of ")[1].split(")")[0])
        return _Resp(self._work(index, total))


class _Resp:
    def __init__(self, text):
        self.text = text
        self.provider = "fake"
        self.model = "fake"
        self.usage = {}


# ---------------------------------------------------------------------------
# Notes generation
# ---------------------------------------------------------------------------
def _run_notes(tmp_path, client, transcript_words=12000, monkeypatch=None):
    from src import pipeline, prompt_builder

    monkeypatch.setattr(
        prompt_builder, "build_dcn_chunk_prompt",
        lambda **kw: f"INDEX={kw['chunk_index']} TOTAL={kw['chunk_total']} PROMPT")
    monkeypatch.setattr(
        prompt_builder, "build_dcn_merge_prompt",
        lambda **kw: "MERGE " + " ".join(kw["parts"]))

    api_metadata, warnings = [], []
    merged, count = pipeline._generate_dcn_in_chunks(
        client=client, run_dir=tmp_path, subject="polity", depth="detailed",
        transcript=" ".join(["word"] * transcript_words), slides=[],
        language_code="en", glossary=[], quality_level="off", quality_options={},
        error_report=False, error_report_strictness="standard",
        lecture_title="Test", api_metadata=api_metadata, warnings=warnings,
    )
    return merged, count, api_metadata, warnings


def test_notes_parts_are_assembled_in_lecture_order(tmp_path, monkeypatch):
    """Replies arrive backwards; the lecture must not."""
    client = RecordingClient()
    merged, count, _, _ = _run_notes(tmp_path, client, monkeypatch=monkeypatch)

    assert count >= 4, "expected a long transcript to be chunked"
    # The merge prompt (and so the merged text) must list the parts in order.
    positions = [merged.index(f"PART{i}") for i in range(1, count + 1)]
    assert positions == sorted(positions), f"parts out of order: {merged[:200]}"


def test_notes_parts_actually_run_concurrently(tmp_path, monkeypatch):
    client = RecordingClient()
    _, count, _, _ = _run_notes(tmp_path, client, monkeypatch=monkeypatch)
    assert client.max_in_flight > 1, "notes chunks were still serialised"
    from src.config import MAX_PARALLEL_AI_CALLS
    assert client.max_in_flight <= MAX_PARALLEL_AI_CALLS, "exceeded the configured call cap"


def test_part_files_are_written_under_their_own_index(tmp_path, monkeypatch):
    """dcn_part_03.txt must hold part 3, whenever part 3 happened to arrive."""
    client = RecordingClient()
    _, count, meta, _ = _run_notes(tmp_path, client, monkeypatch=monkeypatch)
    for i in range(1, count + 1):
        assert (tmp_path / f"prompt_dcn_part_{i:02d}.txt").exists()
        assert (tmp_path / f"dcn_part_{i:02d}.txt").read_text(encoding="utf-8") == f"PART{i}"
    # Usage rows are recorded in lecture order too, so the ledger reads sensibly.
    assert [m["chunk"] for m in meta if str(m.get("chunk", "")).startswith("dcn_part")] == [
        f"dcn_part_{i}" for i in range(1, count + 1)]


def test_a_failed_notes_part_still_fails_the_run(tmp_path, monkeypatch):
    """Notes are not tolerant of a missing stretch — that behaviour is unchanged."""
    from src.ai_client import GeminiError

    client = RecordingClient(fail_on={2})
    with pytest.raises(GeminiError):
        _run_notes(tmp_path, client, monkeypatch=monkeypatch)


def test_setting_the_cap_to_one_restores_sequential_behaviour(tmp_path, monkeypatch):
    from src import pipeline

    monkeypatch.setattr(pipeline, "MAX_PARALLEL_AI_CALLS", 1)
    client = RecordingClient(delay_unit=0.01)
    merged, count, _, _ = _run_notes(tmp_path, client, monkeypatch=monkeypatch)
    assert client.max_in_flight == 1
    positions = [merged.index(f"PART{i}") for i in range(1, count + 1)]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# Audio transcription
# ---------------------------------------------------------------------------
def _chunks(tmp_path, n=5):
    made = []
    for i in range(1, n + 1):
        p = tmp_path / f"chunk_{i:03d}.ogg"
        p.write_bytes(b"audio")
        made.append(AudioChunk(path=p, index=i, offset_seconds=(i - 1) * 1500.0,
                               duration_seconds=1500.0))
    return made


def _run_stt(tmp_path, client, monkeypatch, chunks=None, issues=None):
    from src.transcription import gemini_stt

    chunks = chunks or _chunks(tmp_path)
    monkeypatch.setattr(gemini_stt, "split_audio", lambda *a, **kw: chunks)
    return gemini_stt.transcribe(
        tmp_path / "audio.ogg", client=client, work_dir=tmp_path,
        language_hint="hi", issues=issues,
    )


def test_transcript_is_assembled_in_lecture_order(tmp_path, monkeypatch):
    client = RecordingClient()
    result = _run_stt(tmp_path, client, monkeypatch)
    positions = [result.full_text.index(f"PART{i}") for i in range(1, 6)]
    assert positions == sorted(positions), result.full_text


def test_transcription_chunks_run_concurrently(tmp_path, monkeypatch):
    client = RecordingClient()
    _run_stt(tmp_path, client, monkeypatch)
    assert client.max_in_flight > 1, "audio chunks were still serialised"


def test_segment_timings_follow_the_chunk_not_the_reply_order(tmp_path, monkeypatch):
    client = RecordingClient()
    result = _run_stt(tmp_path, client, monkeypatch)
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts) == [0.0, 1500.0, 3000.0, 4500.0, 6000.0]
    assert result.segments[2].text == "PART3"


def test_one_failed_chunk_leaves_a_gap_and_keeps_the_rest(tmp_path, monkeypatch):
    """The per-chunk failure tolerance must survive the concurrency change."""
    issues = []
    client = RecordingClient(fail_on={3})
    result = _run_stt(tmp_path, client, monkeypatch, issues=issues)

    assert "PART2" in result.full_text and "PART4" in result.full_text
    assert "transcript unavailable for approximately 50:00-1:15:00" in result.full_text
    assert result.gaps == ["50:00-1:15:00"]
    # Issues are recorded in lecture order, not completion order.
    chunk_issues = [i for i in issues if i.stage == "audio-chunk"]
    assert [i.chunk for i in chunk_issues] == ["3/5"]
    assert chunk_issues[0].time_range == "50:00-1:15:00"


def test_an_empty_chunk_is_treated_as_a_gap(tmp_path, monkeypatch):
    issues = []
    client = RecordingClient(empty_on={1})
    result = _run_stt(tmp_path, client, monkeypatch, issues=issues)
    assert result.gaps == ["0:00-25:00"]
    assert "PART5" in result.full_text


def test_every_chunk_failing_raises(tmp_path, monkeypatch):
    from src.video_sources.errors import TranscriptionEngineError

    client = RecordingClient(fail_on={1, 2, 3, 4, 5})
    with pytest.raises(TranscriptionEngineError):
        _run_stt(tmp_path, client, monkeypatch)


# ---------------------------------------------------------------------------
# Layout verification reuses the PDF the run already produced
# ---------------------------------------------------------------------------
def test_layout_check_reuses_an_already_rendered_pdf(tmp_path, monkeypatch):
    """The pipeline exports the student's PDF, then verifies layout. Converting
    the same unchanged DOCX again means a second trip through Word over COM."""
    from src import docx_render_check

    converted = []
    monkeypatch.setattr(
        docx_render_check, "_convert_to_pdf",
        lambda docx, out: (converted.append(docx) or (None, ["converted"])))

    docx = tmp_path / "notes.docx"
    docx.write_bytes(b"docx")
    pdf = tmp_path / "notes.pdf"
    # A real one-page PDF, so the fitz scan below has something to open.
    import fitz
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()

    result = docx_render_check.render_and_verify(
        docx, tmp_path / "check", existing_pdf=pdf)

    assert converted == [], "Word was driven a second time for the same DOCX"
    assert result.pdf_path == pdf
    assert result.page_count == 1


def test_layout_check_still_converts_when_the_pdf_is_missing(tmp_path, monkeypatch):
    """A stale or absent path must fall back to converting, not silently skip."""
    from src import docx_render_check

    converted = []
    monkeypatch.setattr(
        docx_render_check, "_convert_to_pdf",
        lambda docx, out: (converted.append(docx) or (None, ["convert failed"])))

    docx = tmp_path / "notes.docx"
    docx.write_bytes(b"docx")
    docx_render_check.render_and_verify(
        docx, tmp_path / "check", existing_pdf=tmp_path / "does_not_exist.pdf")
    assert converted == [docx]


# ---------------------------------------------------------------------------
# Wasted work removed after the timing model exposed it
# ---------------------------------------------------------------------------
def test_zip_stores_already_compressed_files_and_deflates_text(tmp_path):
    """A run folder is mostly slide PNGs. DEFLATE-ing them took 8.5 s on a
    92-page deck to shrink the archive by 0.4%."""
    import zipfile

    from src.utils import zip_dir

    run = tmp_path / "run"
    run.mkdir()
    (run / "slide_001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40)
    (run / "notes_raw.txt").write_text("Article 21 " * 2000, encoding="utf-8")

    out = zip_dir(run, tmp_path / "run.zip")
    with zipfile.ZipFile(out) as zf:
        methods = {i.filename: i.compress_type for i in zf.infolist()}
    assert methods["slide_001.png"] == zipfile.ZIP_STORED
    assert methods["notes_raw.txt"] == zipfile.ZIP_DEFLATED
    # And the archive is still readable.
    with zipfile.ZipFile(out) as zf:
        assert zf.read("notes_raw.txt").decode().startswith("Article 21")


def test_extract_timeout_scales_with_the_bytes_to_download(tmp_path):
    """A flat hour hard-failed a 2.39 GB Drive lecture on a modest link."""
    from src.media.audio_extractor import EXTRACT_TIMEOUT, extract_timeout_for

    assert extract_timeout_for(0) == EXTRACT_TIMEOUT            # unknown size
    assert extract_timeout_for(750_656_091) == EXTRACT_TIMEOUT  # CDN file: an hour is plenty
    # The 2.39 GB Drive recording needs more than an hour at 2 Mbps.
    drive = extract_timeout_for(2_390_594_088)
    assert drive > EXTRACT_TIMEOUT
    assert drive <= 6 * 3600


def test_a_failed_page_fetch_is_remembered_briefly(tmp_path, monkeypatch):
    """A dead official domain was re-fetched once per claim, 20 s each."""
    from src.qc import page_fetcher

    calls = []

    class _Raw:
        def __init__(self):
            self.error = "connection refused"
            self.final_url = "https://example.gov.in/x"
            self.status = 0
            self.content_type = ""
            self.body = b""

    def _fake_fetch_raw(url, domains, **kw):
        calls.append(url)
        return _Raw()

    monkeypatch.setattr(page_fetcher, "fetch_raw", _fake_fetch_raw)
    url = "https://example.gov.in/x"

    first = page_fetcher.fetch_page(url, ["example.gov.in"], cache_dir=tmp_path)
    second = page_fetcher.fetch_page(url, ["example.gov.in"], cache_dir=tmp_path)

    assert first.error and second.error
    assert second.from_cache is True
    assert calls == [url], "the dead host was fetched twice"


def test_a_successful_fetch_is_still_cached_as_a_success(tmp_path, monkeypatch):
    from src.qc import page_fetcher

    class _Raw:
        error = ""
        final_url = "https://example.gov.in/ok"
        status = 200
        content_type = "text/html"
        body = b"<html><head><title>T</title></head><body>Article 21 text</body></html>"

    monkeypatch.setattr(page_fetcher, "fetch_raw", lambda *a, **kw: _Raw())
    url = "https://example.gov.in/ok"
    page_fetcher.fetch_page(url, ["example.gov.in"], cache_dir=tmp_path)

    monkeypatch.setattr(
        page_fetcher, "fetch_raw",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("refetched a cached success")))
    again = page_fetcher.fetch_page(url, ["example.gov.in"], cache_dir=tmp_path)
    assert again.from_cache is True and not again.error
    assert again.status == 200


def test_a_failed_notes_part_does_not_wait_for_the_others(tmp_path, monkeypatch):
    """Once a part fails the run is lost; waiting out the rest adds their full
    HTTP timeout to a run that is already going to raise."""
    import time as _time

    from src.ai_client import GeminiError

    # Replies arrive in reverse index order, so with 5 parts the sleeps are
    # p1=4.5s, p2=3.6s, p3=2.7s, p4=1.8s. Failing p4 means the error surfaces at
    # ~1.8 s while p1-p3 are still in flight — which is exactly the case where
    # shutdown(wait=True) used to sit and wait for all of them.
    client = RecordingClient(delay_unit=0.9, fail_on={4})
    started = _time.time()
    with pytest.raises(GeminiError):
        _run_notes(tmp_path, client, monkeypatch=monkeypatch)
    elapsed = _time.time() - started
    assert elapsed < 3.0, (
        f"waited {elapsed:.1f}s — the run should have given up at ~1.8s instead "
        "of waiting out the slowest part (4.5s)")


def test_progress_is_reported_once_per_completed_chunk(tmp_path, monkeypatch):
    from src.transcription import gemini_stt

    seen = []
    chunks = _chunks(tmp_path)
    monkeypatch.setattr(gemini_stt, "split_audio", lambda *a, **kw: chunks)
    gemini_stt.transcribe(
        tmp_path / "audio.ogg", client=RecordingClient(), work_dir=tmp_path,
        progress_callback=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
