"""When Gemini's budget is spent, the run continues on Claude.

Measured on 2026-08-26: every Gemini call returned 429 "Budget has been
exceeded" while Claude answered normally on the same proxy, and the app
produced four rows from its free checks instead of a report. A shared key runs
out; that must degrade the model behind the findings, not the findings.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import pw_access  # noqa: E402
from src import ai_client  # noqa: E402
from src.ai_client import (  # noqa: E402
    GeminiClient,
    _claude_result_as_gemini,
    _gemini_body_as_claude,
    _is_budget_exhausted,
)

BUDGET_ERROR = ('gemini proxy error 429: {"error":"gemini gateway error",'
                '"detail":{"error":{"message":"Budget has been exceeded! '
                'Key=content-creation-key-4 Current cost: 211.10"}}}')


@pytest.fixture(autouse=True)
def _reset_latch():
    GeminiClient._gemini_budget_spent = False
    yield
    GeminiClient._gemini_budget_spent = False


def test_a_spent_budget_is_told_apart_from_a_busy_model():
    assert _is_budget_exhausted(Exception(BUDGET_ERROR))
    # A busy model IS worth retrying, and must not be mistaken for this.
    assert not _is_budget_exhausted(Exception("gemini proxy error 503: overloaded"))
    assert not _is_budget_exhausted(Exception("gemini proxy error 429: rate limited"))


def test_the_request_is_translated_into_claude_shape():
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": "check this page"},
            {"inline_data": {"mime_type": "image/png", "data": "AAAA"}},
        ]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4321},
    }
    request = _gemini_body_as_claude(body)
    assert request["max_tokens"] == 4321
    assert request["temperature"] == 0.0
    content = request["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "check this page"}
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"] == "AAAA"


def test_the_reply_is_translated_back_so_existing_readers_parse_it():
    result = {"result": {"content": [{"type": "text", "text": "# ERROR_REPORT_START"}],
                         "stop_reason": "max_tokens", "model": "claude-haiku-4-5",
                         "usage": {"input_tokens": 10, "output_tokens": 20}}}
    shaped = _claude_result_as_gemini(result)
    assert shaped["candidates"][0]["content"]["parts"][0]["text"] == "# ERROR_REPORT_START"
    # The truncation detector reads finishReason; max_tokens must survive it.
    assert shaped["candidates"][0]["finishReason"] == "MAX_TOKENS"
    assert shaped["_pw_model"] == "claude-haiku-4-5"


def test_a_run_continues_on_claude_when_the_budget_is_gone(monkeypatch):
    asked = {"gemini": 0, "claude": 0}

    def no_budget(*args, **kwargs):
        asked["gemini"] += 1
        raise pw_access.PWAccessError(BUDGET_ERROR)

    def claude(token, *, model, request, **kwargs):
        asked["claude"] += 1
        return {"result": {"content": [{"type": "text", "text": "checked"}],
                           "stop_reason": "end_turn", "model": model}}

    monkeypatch.setattr(pw_access, "gemini_generate", no_budget)
    monkeypatch.setattr(pw_access, "claude_generate", claude)

    client = GeminiClient("token", task_id="t", filename="f")
    answer = client.generate("audit this", None, max_output_tokens=100)
    assert answer.text == "checked"
    assert asked["claude"] == 1
    assert any("out of budget" in note for note in client.fallback_notices)


def test_a_spent_budget_is_discovered_once_not_once_per_call(monkeypatch):
    """The proxy takes ~40s to report a spent budget. Paying that on every
    call cost 84s per call; the latch makes it once per run."""
    asked = {"gemini": 0}

    def no_budget(*args, **kwargs):
        asked["gemini"] += 1
        raise pw_access.PWAccessError(BUDGET_ERROR)

    def claude(token, *, model, request, **kwargs):
        return {"result": {"content": [{"type": "text", "text": "ok"}],
                           "stop_reason": "end_turn", "model": model}}

    monkeypatch.setattr(pw_access, "gemini_generate", no_budget)
    monkeypatch.setattr(pw_access, "claude_generate", claude)

    client = GeminiClient("token", task_id="t", filename="f")
    for _ in range(4):
        assert client.generate("x", None, max_output_tokens=50).text == "ok"
    # Two Gemini attempts on the first call (the model and its fallback), and
    # none after that.
    assert asked["gemini"] <= 2, f"kept asking a spent budget: {asked}"
    # And the user is told once, not four times.
    assert sum("out of budget" in n for n in client.fallback_notices) == 1


def test_a_budget_error_never_goes_through_the_retry_ladder(monkeypatch):
    """Backoff cannot refill a budget; retrying only wastes the run's time."""
    slept = []
    monkeypatch.setattr(ai_client.time, "sleep", lambda s: slept.append(s))

    def no_budget(*args, **kwargs):
        raise pw_access.PWAccessError(BUDGET_ERROR)

    monkeypatch.setattr(pw_access, "gemini_generate", no_budget)
    monkeypatch.setattr(pw_access, "claude_generate",
                        lambda *a, **k: {"result": {"content": [
                            {"type": "text", "text": "ok"}], "model": "claude"}})

    GeminiClient("token", task_id="t", filename="f").generate("x", None)
    assert not [s for s in slept if s], f"backed off on a spent budget: {slept}"


def test_when_claude_is_also_unavailable_the_original_error_is_raised(monkeypatch):
    from src.ai_client import GeminiError

    monkeypatch.setattr(pw_access, "gemini_generate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            pw_access.PWAccessError(BUDGET_ERROR)))
    monkeypatch.setattr(pw_access, "claude_generate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("claude down")))

    with pytest.raises(GeminiError):
        GeminiClient("token", task_id="t", filename="f").generate("x", None)
