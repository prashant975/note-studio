"""GPT models through the PW proxy.

Callers speak Gemini's request/response shape; a GPT model must be translated
both ways so retries, fallback, truncation warnings and usage keep working.
"""
from __future__ import annotations

import pytest

import pw_access
from src import ai_client
from src.ai_client import GeminiClient, GeminiError

GPT = "gpt-5.6-luna-ard-content"


def _chat_reply(text="OK", finish="stop"):
    return {
        "result": {
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                      "completion_tokens_details": {"reasoning_tokens": 3}},
        },
        "usage": {"tokens_in": 11, "tokens_out": 7},
    }


def _no_gemini(token, **kw):
    raise AssertionError(f"Gemini called for {kw['model']}")


def test_gpt_model_goes_to_the_gpt_route_with_a_chat_body(monkeypatch, tmp_path):
    seen = {}

    def fake_gpt(token, **kw):
        seen.update(kw)
        return _chat_reply("Notes here")

    monkeypatch.setattr(pw_access, "gpt_generate", fake_gpt)
    monkeypatch.setattr(pw_access, "gemini_generate", _no_gemini)
    img = tmp_path / "slide.png"
    from PIL import Image
    Image.new("RGB", (10, 10), "white").save(img)

    resp = GeminiClient("t", model=GPT, fallback_model="").generate("Write notes", [img], max_output_tokens=12000)

    assert resp.text == "Notes here"
    assert resp.model == GPT
    assert resp.usage == {"tokens_input": 11, "tokens_output": 7, "tokens_total": 18, "tokens_thinking": 3}
    assert not resp.truncated
    req = seen["request"]
    assert "temperature" not in req
    assert req["max_completion_tokens"] == 12000 + ai_client._GPT_REASONING_HEADROOM
    content = req["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Write notes"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_gpt_length_stop_is_reported_as_truncation(monkeypatch):
    monkeypatch.setattr(pw_access, "gpt_generate", lambda token, **kw: _chat_reply("half", "length"))
    client = GeminiClient("t", model=GPT, fallback_model="")
    resp = client.generate("x", [], max_images=0)
    assert resp.truncated
    assert any("stopped early" in n for n in client.fallback_notices)


def test_gpt_failure_falls_back_to_gemini(monkeypatch):
    monkeypatch.setattr(ai_client, "_RETRY_WAITS_SECONDS", (0, 0, 0))

    def down(token, **kw):
        raise pw_access.PWAccessError("gpt proxy error 503: overloaded")

    monkeypatch.setattr(pw_access, "gpt_generate", down)
    monkeypatch.setattr(pw_access, "gemini_generate", lambda token, **kw: {
        "result": {"candidates": [{"content": {"parts": [{"text": "GEMINI"}]}}]}})
    client = GeminiClient("t", model=GPT, fallback_model="gemini-3.5-flash")
    resp = client.generate("x", [], max_images=0)
    assert resp.text == "GEMINI"
    assert resp.model == "gemini-3.5-flash"
    assert any("unavailable" in n for n in client.fallback_notices)


def test_audio_stays_on_gemini_when_the_text_model_is_gpt(monkeypatch, tmp_path):
    models = []

    def fake_gemini(token, **kw):
        models.append(kw["model"])
        return {"result": {"candidates": [{"content": {"parts": [{"text": "transcript"}]}}]}}

    monkeypatch.setattr(pw_access, "gemini_generate", fake_gemini)
    monkeypatch.setattr(pw_access, "gpt_generate", lambda *a, **k: pytest.fail("audio sent to GPT"))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"OggS")
    resp = GeminiClient("t", model=GPT).generate_with_audio("transcribe", audio)
    assert resp.text == "transcript"
    assert models == [ai_client.DEFAULT_AUDIO_MODEL]


def test_responses_api_shape_is_also_read():
    wrapped = ai_client._openai_reply_to_gemini(
        {"result": {"output": [{"content": [{"type": "output_text", "text": "hi"}]}], "status": "completed"}})
    assert ai_client._extract_text_from_response(wrapped["result"]) == "hi"


def test_non_image_input_is_refused_for_gpt():
    body = {"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": "audio/ogg", "data": "x"}}]}]}
    with pytest.raises(GeminiError):
        ai_client._gemini_body_to_openai(body)
