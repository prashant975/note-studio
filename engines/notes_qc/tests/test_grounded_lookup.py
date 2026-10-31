"""Grounded fact-checking, through the proxy the app already uses.

The proxy does not forward GEMINI's ``google_search`` tool — measured, the same
wrong answer with the tool field and without it, in every shape the Gemini REST
API accepts. But Claude's web search is a SERVER tool, so only the declaration
has to travel, and that the proxy does forward: on the five facts the QC run
kept missing — who holds an office, which city hosted a meeting, which
territory named a cyclone, which body published a report, a revised threshold
table — Claude with web search answered 5 of 5 where the Gemini path answered
0 of 5.

So the primary path needs no new key: it is the signed-in session the app
already has. A direct Gemini key stays as an optional fallback.

Every test here runs without a network — both transports are injected. They
pin that the feature is off and harmless when nothing is configured, that an
ungrounded answer is never trusted, and that a grounded one either removes a
row or promotes it with its sources attached.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import grounded_lookup  # noqa: E402
from src.grounded_lookup import GroundedAnswer, look_up  # noqa: E402
from src.qc.verify import run_verification_pass  # noqa: E402


def row(concern="The office-holder named is wrong.", severity="High"):
    return {"original_statement": "Vice President C. P. Radhakrishnan released a book.",
            "error_type": "Factual", "severity": severity, "error_concern": concern,
            "correct_information_correction": "the Governor", "source_urls": ""}


def claude_reply(text, *, searched=True, sources=("https://vicepresidentofindia.nic.in/",),
                 query="current vice president of india"):
    content = []
    if searched:
        content.append({"type": "server_tool_use", "input": {"query": query}})
        content.append({"type": "web_search_tool_result",
                        "content": [{"url": u} for u in sources]})
    content.append({"type": "text", "text": text})
    return {"result": {"content": content}}


# --------------------------------------------------------------- the module

def test_with_no_session_and_no_key_the_feature_is_off_and_silent(monkeypatch):
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")
    assert grounded_lookup.available() is False
    assert look_up("Who is the Vice President of India?") is None
    # The reason names the fix, and the fix is signing in — not buying a key.
    assert "Sign in" in grounded_lookup.why_unavailable()


def test_a_signed_in_session_is_enough_and_no_key_is_required(monkeypatch):
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")
    assert grounded_lookup.available("some-google-token") is True
    assert grounded_lookup.why_unavailable("some-google-token") == ""


def test_claude_web_search_is_asked_through_the_proxy(monkeypatch):
    """The shape that measured 5/5: Claude, with its server-side web search."""
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")
    seen = {}

    def fake_claude(token, *, model, request, **kw):
        seen.update(token=token, model=model, request=request)
        return claude_reply("VERDICT: WRONG_IN_NOTES\nBECAUSE: It is C. P. Radhakrishnan.")

    answer = look_up("Who is the VP?", google_token="tok", claude_send=fake_claude)
    assert answer is not None and answer.grounded and answer.searched
    assert "C. P. Radhakrishnan" in answer.answer
    assert answer.sources == ["https://vicepresidentofindia.nic.in/"]
    assert answer.queries == ["current vice president of india"]
    assert seen["token"] == "tok"
    assert seen["request"]["tools"][0]["name"] == "web_search"
    assert seen["request"]["tools"][0]["type"].startswith("web_search_")


def test_a_model_the_proxy_refuses_falls_through_to_the_next(monkeypatch):
    """Haiku rejects the 20260209 tool variant with a 400; the next pair runs."""
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")
    tried = []

    def fussy(token, *, model, request, **kw):
        tried.append(model)
        if len(tried) < 2:
            raise RuntimeError("claude proxy error 400: unsupported tool")
        return claude_reply("an answer")

    answer = look_up("q", google_token="tok", claude_send=fussy)
    assert answer is not None and answer.answer == "an answer"
    assert len(tried) == 2, "it must try the next model, not give up"


def test_an_ungrounded_claude_reply_is_rejected(monkeypatch):
    """A reply with no search is memory again — exactly what we are escaping."""
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")

    def never_searches(token, *, model, request, **kw):
        return claude_reply("Jagdeep Dhankhar", searched=False, sources=())

    assert look_up("q", google_token="tok", claude_send=never_searches) is None


def test_a_grounded_gemini_reply_is_read_with_its_sources_and_queries():
    parsed = grounded_lookup._read_gemini({"candidates": [{
        "content": {"parts": [{"text": "VERDICT: WRONG_IN_NOTES"}]},
        "groundingMetadata": {
            "webSearchQueries": ["current vice president of india"],
            "groundingChunks": [{"web": {"uri": "https://vicepresidentofindia.nic.in/"}},
                                {"web": {"uri": "https://pib.gov.in/x"}}],
        }}]}, "gemini-2.5-flash")
    assert parsed.grounded is True
    assert parsed.sources == ["https://vicepresidentofindia.nic.in/", "https://pib.gov.in/x"]
    assert parsed.queries == ["current vice president of india"]


def test_an_answer_with_no_grounding_metadata_is_not_evidence():
    parsed = grounded_lookup._read_gemini(
        {"candidates": [{"content": {"parts": [{"text": "Jagdeep Dhankhar"}]}}]}, "m")
    assert parsed.answer == "Jagdeep Dhankhar"
    assert parsed.grounded is False


def test_the_gemini_fallback_asks_for_search_grounding(monkeypatch):
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "test-key")
    seen = {}

    def fake_post(model, key, body):
        seen.update(body=body, key=key)
        return {"candidates": [{"content": {"parts": [{"text": "VERDICT: STILL_UNCLEAR"}]},
                                "groundingMetadata": {"webSearchQueries": ["q"]}}]}

    answer = look_up("Which city hosted the meeting?", gemini_post=fake_post)
    assert answer is not None and answer.grounded
    assert seen["body"]["tools"] == [{"google_search": {}}]
    assert seen["key"] == "test-key"


def test_a_network_failure_never_raises(monkeypatch):
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "test-key")

    def boom(model, key, body):
        raise OSError("no network")

    assert look_up("anything", gemini_post=boom) is None


# ------------------------------------------------- the verification pass

class _Verifier:
    """Answers with one fixed verdict for every claim put to it."""

    def __init__(self, verdict="UNVERIFIABLE", basis=""):
        self.verdict, self.basis = verdict, basis

    def generate(self, prompt, images=None, **kw):
        import re

        ids = [int(m.group(1)) for m in re.finditer(r"--- CLAIM (\d+) ---", prompt)]
        payload = [{"id": i, "verdict": self.verdict, "basis": self.basis,
                    "why": "cannot settle"} for i in ids]

        class R:
            text = json.dumps(payload)
            truncated = False
        return R()


def _lookup(verdict, sources=("https://example.gov.in/page",)):
    def fake(question):
        return GroundedAnswer(
            answer=f"VERDICT: {verdict}\nBECAUSE: the web says so.",
            sources=list(sources), queries=["a search"], searched=True,
            model="claude-haiku-4-5")
    return fake


def test_a_web_search_promotes_an_unverifiable_claim_and_cites_its_sources():
    result = run_verification_pass(_Verifier(), [row()],
                                   lookup=_lookup("WRONG_IN_NOTES"))
    assert len(result.rows) == 1
    kept = result.rows[0]
    assert kept["severity"] == "High", "a confirmed error is not demoted to a lead"
    assert "Checked against the web" in kept["error_concern"]
    assert "example.gov.in" in kept["source_urls"]
    assert result.grounded_confirmed == 1
    assert any("web search settled" in w for w in result.warnings())


def test_a_web_search_removes_a_claim_the_notes_had_right():
    result = run_verification_pass(_Verifier(), [row()],
                                   lookup=_lookup("NOTES_ARE_RIGHT"))
    assert result.rows == []
    assert len(result.dropped) == 1
    assert result.grounded_refuted == 1
    assert "Web search" in result.dropped[0]["_verification_why"]


def test_an_unsettled_search_leaves_the_row_exactly_as_a_lead():
    result = run_verification_pass(_Verifier(), [row()],
                                   lookup=_lookup("STILL_UNCLEAR"))
    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
    assert result.rows[0]["error_concern"].startswith("[LEAD")
    assert result.grounded_confirmed == result.grounded_refuted == 0


def test_a_memory_based_real_verdict_is_looked_up_too():
    result = run_verification_pass(_Verifier("REAL", "memory"), [row()],
                                   lookup=_lookup("WRONG_IN_NOTES"))
    assert result.grounded_confirmed == 1
    assert result.rows[0]["severity"] == "High"
    assert result.memory_only == 0


def test_with_nothing_configured_the_pass_behaves_exactly_as_before(monkeypatch):
    monkeypatch.setattr(grounded_lookup, "_api_key", lambda: "")
    result = run_verification_pass(_Verifier(), [row()])
    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
    assert result.rows[0]["error_concern"].startswith("[LEAD")
    assert result.grounded_confirmed == result.grounded_refuted == 0


def test_an_ungrounded_lookup_answer_is_ignored():
    def ungrounded(question):
        return GroundedAnswer(answer="VERDICT: NOTES_ARE_RIGHT\nBECAUSE: I recall it.",
                              sources=[], queries=[], searched=False)

    result = run_verification_pass(_Verifier(), [row()], lookup=ungrounded)
    assert len(result.rows) == 1, "an unsearched answer must not delete a row"
    assert result.rows[0]["severity"] == "Low"
    assert result.grounded_refuted == 0


def test_a_lookup_that_raises_leaves_the_row_alone():
    def boom(question):
        raise RuntimeError("provider down")

    result = run_verification_pass(_Verifier(), [row()], lookup=boom)
    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
