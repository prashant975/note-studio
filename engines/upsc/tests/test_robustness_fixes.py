"""Fixes driven by the timing model: network resilience, wired-up concurrency,
single-transcode YouTube audio, per-mode render scale, rebuild consistency,
and standalone PDF export.

Each test names the failure it guards against, because most of these bugs were
invisible in a passing run — they only cost time, or only fired after a crash.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import requests

import pw_access
from src.ai_client import GeminiClient, GeminiError


def _client() -> GeminiClient:
    return GeminiClient("fake-token", model="gemini-3.5-flash",
                        fallback_model="gemini-3.6-flash")


def _ok_response(text="OK"):
    return {"result": {"candidates": [{"content": {"parts": [{"text": text}]}}]}}


# ---------------------------------------------------------------------------
# ai_client: the ladder now defends against "quiet" failures too
# ---------------------------------------------------------------------------
def test_a_network_timeout_is_retried_not_fatal(monkeypatch):
    """A requests.Timeout used to fly straight through the retry ladder: one
    network blip at minute 40 killed the run with no retry and no fallback."""
    calls = []

    def fake_generate(token, **kw):
        calls.append(kw["model"])
        if len(calls) == 1:
            raise requests.Timeout("read timed out")
        return _ok_response()

    monkeypatch.setattr(pw_access, "gemini_generate", fake_generate)
    monkeypatch.setattr("src.ai_client._RETRY_WAITS_SECONDS", (0, 0, 0))
    resp = _client().generate("hello", [], max_images=0)
    assert resp.text == "OK"
    assert len(calls) == 2                      # failed once, retried, served


def test_exhausted_network_errors_still_reach_the_fallback_model(monkeypatch):
    """If the primary model never answers at all, the fallback must be tried —
    'the model never answered' is the same failure whichever layer ate it."""
    calls = []

    def fake_generate(token, **kw):
        calls.append(kw["model"])
        if kw["model"] == "gemini-3.5-flash":
            raise requests.ConnectionError("connection dropped")
        return _ok_response("FALLBACK")

    monkeypatch.setattr(pw_access, "gemini_generate", fake_generate)
    monkeypatch.setattr("src.ai_client._RETRY_WAITS_SECONDS", (0, 0, 0))
    client = _client()
    resp = client.generate("hello", [], max_images=0)
    assert resp.text == "FALLBACK"
    assert calls.count("gemini-3.5-flash") == 3      # full ladder on primary
    assert calls.count("gemini-3.6-flash") == 1
    assert any("unavailable" in n for n in client.fallback_notices)


def test_non_transient_errors_still_fail_immediately(monkeypatch):
    """A 400/auth error must NOT be retried — that behaviour is unchanged."""
    calls = []

    def fake_generate(token, **kw):
        calls.append(kw["model"])
        raise pw_access.PWAccessError("gemini proxy error 400: bad request")

    monkeypatch.setattr(pw_access, "gemini_generate", fake_generate)
    monkeypatch.setattr("src.ai_client._RETRY_WAITS_SECONDS", (0, 0, 0))
    with pytest.raises(GeminiError):
        _client().generate("hello", [], max_images=0)
    # One attempt per candidate model, no ladder.
    assert calls == ["gemini-3.5-flash", "gemini-3.6-flash"]


def test_the_clients_timeout_actually_reaches_the_proxy_call(monkeypatch):
    """GeminiClient(timeout=...) was stored and never read; pw_access used its
    own constant. Now the value must arrive at gemini_generate."""
    seen = {}

    def fake_generate(token, **kw):
        seen["timeout"] = kw.get("timeout")
        return _ok_response()

    monkeypatch.setattr(pw_access, "gemini_generate", fake_generate)
    client = GeminiClient("fake-token", model="gemini-3.5-flash", timeout=123)
    client.generate("hello", [], max_images=0)
    assert seen["timeout"] == 123


# ---------------------------------------------------------------------------
# claim_verifier: max_concurrent_requests is finally load-bearing
# ---------------------------------------------------------------------------
def _Claim(i):
    """A real FactualClaim, so the verifier's prompt builder accepts it."""
    from src.qc.models import FactualClaim

    return FactualClaim(
        claim_id=f"C{i}",
        text=f"Article {i} guarantees something distinct {i}",
        claim_type="constitutional",
        subject="polity",
    )


class _Limits:
    max_claims_verified = 20
    sources_per_claim = 3
    passages_per_source = 3
    request_timeout_seconds = 1
    max_download_bytes = 1024
    max_concurrent_requests = 4


class _Sources:
    official_domains = ["example.gov.in"]
    books = []
    subject_key = "polity"


def test_evidence_gathering_runs_concurrently_and_logs_in_claim_order(monkeypatch):
    from src.qc import claim_verifier

    state = {"in_flight": 0, "max": 0}
    lock = threading.Lock()

    def fake_gather(claim, sources, limits, cache_dir, evidence, log):
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        # Later claims finish first, so completion order is reversed.
        time.sleep(0.05 * (6 - int(claim.claim_id[1:])))
        log.append({"claim_id": claim.claim_id, "query": "q", "outcome": "no evidence"})
        with lock:
            state["in_flight"] -= 1
        return 1, 1

    monkeypatch.setattr(claim_verifier, "_gather_web_evidence", fake_gather)
    claims = [_Claim(i) for i in range(1, 6)]
    verifications, evidence, log = claim_verifier.verify_claims(
        claims, _Sources(), client=object(), limits=_Limits(), retriever=None,
        cache_dir=Path("unused"),
    )
    assert state["max"] > 1, "evidence gathering is still sequential"
    assert state["max"] <= 4, "exceeded max_concurrent_requests"
    # The log reads down the claims, not down the network.
    logged = [e["claim_id"] for e in log if e["claim_id"]]
    assert logged == ["C1", "C2", "C3", "C4", "C5"]
    # Every claim still gets exactly one verification, in input order.
    assert [v.claim_id for v in verifications] == ["C1", "C2", "C3", "C4", "C5"]


def test_verify_batches_run_concurrently(monkeypatch):
    from src.qc import claim_verifier

    monkeypatch.setattr(claim_verifier, "_gather_web_evidence",
                        lambda c, s, l, cd, ev, log: (0, 0))

    # Give every claim one piece of evidence so all of them reach the model.

    def fake_evidence(claim, chunks, books):
        from src.qc.models import Evidence

        return [Evidence(
            evidence_id=f"{claim.claim_id}-E1", claim_id=claim.claim_id,
            source_type="book", source_name="NCERT", passage="Article text")]

    monkeypatch.setattr(claim_verifier, "evidence_from_chunks", fake_evidence)

    state = {"in_flight": 0, "max": 0}
    lock = threading.Lock()

    class SlowClient:
        def generate(self, prompt, images, **kw):
            with lock:
                state["in_flight"] += 1
                state["max"] = max(state["max"], state["in_flight"])
            time.sleep(0.15)
            with lock:
                state["in_flight"] -= 1
            ids = [line.split()[1] for line in prompt.splitlines()
                   if line.startswith("CLAIM ")]
            payload = {"results": [
                {"claim_id": cid, "verdict": "supported", "confidence": 0.9,
                 "evidence_ids": [f"{cid}-E1"], "explanation": "matches"}
                for cid in ids]}
            return types.SimpleNamespace(text=json.dumps(payload))

    class FakeRetriever:
        def available(self):
            return True

        def search(self, *a, **kw):
            return [{"text": "chunk", "book": "NCERT", "locator": "p1", "score": 1.0}]

    claims = [_Claim(i) for i in range(1, 18)]      # 17 claims -> 3 batches
    verifications, _, _ = claim_verifier.verify_claims(
        claims, _Sources(), client=SlowClient(), limits=_Limits(),
        retriever=FakeRetriever(), cache_dir=Path("unused"),
    )
    assert state["max"] > 1, "verify batches are still sequential"
    assert len(verifications) == 17
    assert all(v.claim_id == c.claim_id for v, c in zip(verifications, claims))


# ---------------------------------------------------------------------------
# YouTube: one transcode, not two
# ---------------------------------------------------------------------------
def test_youtube_download_no_longer_re_encodes(tmp_path, monkeypatch):
    """yt-dlp used to convert to Opus, then extract_audio converted AGAIN to
    the STT profile — a full second libopus pass whenever YouTube served AAC."""
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            out = tmp_path / "youtube"
            out.mkdir(parents=True, exist_ok=True)
            (out / "lecture_audio.webm").write_bytes(b"audio-as-served")

    fake_module = types.SimpleNamespace(YoutubeDL=FakeYDL)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)
    monkeypatch.setattr("src.media.audio_extractor.ffmpeg_available", lambda: True)

    from src.transcript import _youtube_media

    media = _youtube_media("https://youtu.be/dQw4w9WgXcQ", tmp_path)
    assert "postprocessors" not in captured, "yt-dlp is re-encoding again"
    assert media.stream_url.endswith("lecture_audio.webm")


# ---------------------------------------------------------------------------
# Render scale: DCN halves the pixel work, legacy keeps print quality
# ---------------------------------------------------------------------------
def test_dcn_mode_renders_pdf_pages_at_reduced_scale(tmp_path):
    import fitz

    from src.extract_pdf import extract_pdf

    pdf = tmp_path / "deck.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), "Article 21")
    doc.save(str(pdf))
    doc.close()

    full = extract_pdf(pdf, tmp_path / "full", render_scale=2.0)
    dcn = extract_pdf(pdf, tmp_path / "dcn", render_scale=1.5)
    from PIL import Image

    w_full = Image.open(full[0].image_path).width
    w_dcn = Image.open(dcn[0].image_path).width
    assert w_dcn == pytest.approx(w_full * 0.75, abs=2)
    # Text extraction is identical — only the raster shrinks.
    assert dcn[0].text == full[0].text


def test_pipeline_source_picks_the_scale_by_mode():
    src = (Path(__file__).resolve().parents[1] / "src" / "pipeline.py").read_text(
        encoding="utf-8")
    assert "render_scale=1.5 if dcn_mode else 2.0" in src


# ---------------------------------------------------------------------------
# rebuild_outputs: same writer as the run it rebuilds
# ---------------------------------------------------------------------------
def _seed_run(tmp_path, *, dcn_mode: bool) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "slides_raw.json").write_text(json.dumps([
        {"slide_no": 1, "heading": "H", "text": "T", "cleaned_text": "T",
         "image_path": None, "source_type": "pdf", "metadata": {}},
    ]), encoding="utf-8")
    (run / "run_metadata.json").write_text(json.dumps({
        "subject": "polity", "dcn_mode": dcn_mode, "detected_language": "en",
        "lecture_title": "L03_DCN03_Probity", "input_file": "lecture.pdf",
    }), encoding="utf-8")
    return run


def test_rebuilding_a_dcn_run_uses_the_dcn_writer(tmp_path, monkeypatch):
    """Rebuilding used the legacy writer regardless: the same note came back as
    a 67-page legacy document instead of the 33-page DCN one it was."""
    from src import pipeline, upsc_dcn_docx_writer

    used = {}

    def fake_dcn_writer(notes_text, output_path, **kw):
        used["writer"] = "dcn"
        used["lecture_title"] = kw.get("lecture_title")
        Path(output_path).write_bytes(b"docx")
        return Path(output_path), []

    monkeypatch.setattr(upsc_dcn_docx_writer, "write_upsc_dcn_docx", fake_dcn_writer)
    monkeypatch.setattr(pipeline, "write_notes_docx",
                        lambda *a, **kw: pytest.fail("legacy writer used for a DCN run"))
    monkeypatch.setattr(pipeline, "export_docx_to_pdf", lambda d, o: (None, ""))

    run = _seed_run(tmp_path, dcn_mode=True)
    docx, pdf, warnings = pipeline.rebuild_outputs(run, "#H1: Edited", stem="lecture")
    assert used["writer"] == "dcn"
    assert used["lecture_title"] == "L03_DCN03_Probity"


def test_rebuilding_a_legacy_run_keeps_the_legacy_writer(tmp_path, monkeypatch):
    from src import pipeline

    used = {}

    def fake_legacy(notes_text, output_path, slides, **kw):
        used["writer"] = "legacy"
        Path(output_path).write_bytes(b"docx")
        return Path(output_path), []

    monkeypatch.setattr(pipeline, "write_notes_docx", fake_legacy)
    monkeypatch.setattr(pipeline, "export_docx_to_pdf", lambda d, o: (None, ""))
    run = _seed_run(tmp_path, dcn_mode=False)
    pipeline.rebuild_outputs(run, "Edited notes", stem="lecture")
    assert used["writer"] == "legacy"


# ---------------------------------------------------------------------------
# PDF conversion: standalone, with actionable failures
# ---------------------------------------------------------------------------
@pytest.mark.real_pdf
def test_pdf_export_does_not_start_an_office_process(tmp_path, monkeypatch):
    from docx import Document
    from src import pdf_exporter
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("PDF export tried to start an external process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    fake_client = types.ModuleType("win32com.client")
    fake_client.DispatchEx = forbidden
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    path = tmp_path / "notes.docx"
    doc = Document()
    doc.add_paragraph("A fresh PDF without installed Office software.")
    doc.save(path)
    pdf, error = pdf_exporter.export_docx_to_pdf(path, tmp_path)
    assert pdf and pdf.read_bytes().startswith(b"%PDF-")
    assert error is None


@pytest.mark.real_pdf
def test_pdf_render_failure_is_reported_and_preserves_the_word_file(tmp_path, monkeypatch):
    from src import pdf_exporter, docx_pdf

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(docx_pdf, "render_docx", fail)
    docx = tmp_path / "notes.docx"
    docx.write_bytes(b"original document")
    pdf, error = pdf_exporter.export_docx_to_pdf(docx, tmp_path)
    assert pdf is None and "disk full" in error
    assert docx.read_bytes() == b"original document"
    assert not list(tmp_path.glob(".pdf-*"))
