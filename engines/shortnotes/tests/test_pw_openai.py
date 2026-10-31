"""The GPT route through the PW proxy: schema, request, response, errors.

The proxy publishes `/api/gpt/generate` (`/api/openai/generate` is a 404), so
these tests pin the contract this app sends there and the failure messages the
operator sees when PW has not enabled GPT for the app yet.
"""
from __future__ import annotations

import pytest

from sheets_bridge import pw_openai as G


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or "proxy body that must never reach a log"

    def json(self) -> dict:
        return self._payload


def _nodes(schema):
    """Every schema node. The property *name* map is walked by value, not by
    key, so a field called "title" is never mistaken for the title keyword."""
    if not isinstance(schema, dict):
        return
    yield schema
    for key in ("properties", "$defs"):
        for child in (schema.get(key) or {}).values():
            yield from _nodes(child)
    for child in schema.get("anyOf") or ():
        yield from _nodes(child)
    yield from _nodes(schema.get("items"))


_REJECTED = {"title", "default", "minimum", "minLength", "maxItems"}


# --------------------------------------------------------------------------- #
# model routing
# --------------------------------------------------------------------------- #
def test_only_gpt_ids_take_the_gpt_route():
    assert G.is_gpt_model("gpt-5.6-terra-ard-content")
    assert G.is_gpt_model("openai/gpt-5.6-luna")
    assert not G.is_gpt_model("gemini-3.7-flash")
    assert not G.is_gpt_model("vertex_ai/gemini-3.7-flash")
    assert not G.is_gpt_model("")


def test_install_picks_the_transport_that_matches_the_model():
    from notesbot import gemini_client

    from sheets_bridge import pw_transport as T

    original = (gemini_client._use_litellm, gemini_client._lite_model,
                gemini_client._lite_client, gemini_client._build_visual_lite_client)
    try:
        model = T.install(lambda: "tok", model="openai/gpt-5.6-terra-ard-content")
        assert model == "gpt-5.6-terra-ard-content"
        assert isinstance(gemini_client._lite_client(), G.OpenAIProxyClient)
        assert isinstance(gemini_client._build_visual_lite_client(), G.OpenAIProxyClient)

        T.install(lambda: "tok", model="gemini-3.7-flash")
        assert isinstance(gemini_client._lite_client(), T.ProxyClient)
    finally:
        (gemini_client._use_litellm, gemini_client._lite_model,
         gemini_client._lite_client, gemini_client._build_visual_lite_client) = original


# --------------------------------------------------------------------------- #
# JSON schema -> OpenAI strict mode
# --------------------------------------------------------------------------- #
def test_strict_schema_meets_openai_rules():
    converted = G.strict_schema({
        "$defs": {"Leaf": {"type": "object", "title": "Leaf",
                           "properties": {"v": {"type": "integer", "minimum": 1}},
                           "required": ["v"]}},
        "type": "object",
        "title": "Root",
        "properties": {
            "leaf": {"$ref": "#/$defs/Leaf", "description": "sibling dropped"},
            "needed": {"type": "string", "minLength": 1},
            "optional": {"type": "string", "default": ""},
            "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "forbidden": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
        },
        "required": ["leaf", "needed", "forbidden"],
    })

    # every property required, nothing extra, no rejected keywords anywhere
    assert converted["required"] == list(converted["properties"])
    assert converted["additionalProperties"] is False
    for node in _nodes(converted):
        assert not set(node) & _REJECTED, f"strict mode rejects {sorted(set(node) & _REJECTED)}"

    # a field the app forbids is removed rather than forced on the model
    assert "forbidden" not in converted["properties"]
    # a $ref may not carry siblings
    assert converted["properties"]["leaf"] == {"$ref": "#/$defs/Leaf"}
    # optional/defaulted fields stay answerable by becoming nullable
    assert converted["properties"]["optional"]["anyOf"][-1] == {"type": "null"}
    assert converted["properties"]["maybe"]["anyOf"].count({"type": "null"}) == 1
    assert converted["properties"]["needed"] == {"type": "string"}
    assert converted["$defs"]["Leaf"]["additionalProperties"] is False


def test_real_notes_schema_converts_without_losing_the_shape():
    from notesbot.gemini_client import _generation_schema_json
    from notesbot.prompts import ShortNotes

    converted = G.strict_schema(_generation_schema_json(ShortNotes))

    problems: list[str] = []
    for node in _nodes(converted):
        if set(node) & _REJECTED:
            problems.append(f"rejected keyword {sorted(set(node) & _REJECTED)}")
        if "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append("object without additionalProperties: false")
            if list(node.get("required") or ()) != list(node["properties"]):
                problems.append("object whose required list is not every property")
        if "$ref" in node and len(node) > 1:
            problems.append("$ref with siblings")
    assert not problems, problems[:5]
    assert "sections" in converted["properties"]
    # recursive models keep their $ref instead of being inlined forever
    assert "$defs" in converted and converted["$defs"]


# --------------------------------------------------------------------------- #
# request / response translation
# --------------------------------------------------------------------------- #
def test_request_keeps_the_pdf_and_renames_the_token_limit(monkeypatch):
    monkeypatch.delenv("PW_PROXY_REASONING_EFFORT", raising=False)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "PROMPT"},
        {"type": "file", "file": {"filename": "l.pdf", "file_data": "data:application/pdf;base64,QUJD"}},
    ]}]

    request = G.build_openai_request({
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 24576,
        "extra_body": {"reasoning_effort": "high"},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "S", "schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}}},
    }, model="gpt-5.6-terra-ard-content")

    assert request["model"] == "gpt-5.6-terra-ard-content"
    assert request["messages"][0]["content"][1]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert request["max_completion_tokens"] == 24576 and "max_tokens" not in request
    # refused live while reasoning is on: "Only temperature=1 is supported"
    assert "temperature" not in request
    assert request["reasoning_effort"] == "high"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False

    monkeypatch.setenv("PW_PROXY_REASONING_EFFORT", "medium")
    assert G.build_openai_request({"messages": messages, "extra_body": {"reasoning_effort": "high"}},
                                  model="gpt-5.6-terra-ard-content")["reasoning_effort"] == "medium"


def test_response_is_shaped_like_the_generator_expects():
    shaped = G.openai_shaped({
        "choices": [{"finish_reason": "length", "message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "prompt_tokens_details": {"cached_tokens": 4}},
        "model": "gpt-5.6-terra-ard-content",
    })
    assert shaped.choices[0].message.content == "hello"
    assert shaped.choices[0].finish_reason == "length"
    assert (shaped.usage.prompt_tokens, shaped.usage.completion_tokens) == (10, 5)
    assert shaped.usage.prompt_tokens_details.cached_tokens == 4

    # wrapped bodies, part lists and Responses-style token names all survive
    wrapped = G.openai_shaped({"result": {
        "choices": [{"message": {"content": [{"type": "text", "text": "he"}, {"type": "text", "text": "llo"}]}}],
        "usage": {"input_tokens": 3, "output_tokens": 2}}})
    assert wrapped.choices[0].message.content == "hello"
    assert (wrapped.usage.prompt_tokens, wrapped.usage.completion_tokens) == (3, 2)
    assert G.openai_shaped({}).choices[0].message.content == ""


# --------------------------------------------------------------------------- #
# proxy call: metadata, cost, failure messages
# --------------------------------------------------------------------------- #
def test_call_sends_ledger_metadata_and_no_provider_key(monkeypatch):
    from sheets_bridge import pw_transport as T

    seen: dict = {}

    def fake_post(path, token_provider, payload, timeout):
        seen["path"] = path
        seen["timeout"] = timeout
        seen["payload"] = payload
        seen["token"] = token_provider() if callable(token_provider) else token_provider
        return FakeResponse(200, {"choices": [{"message": {"content": "OK"}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                                  "cost_inr": 2.5})

    monkeypatch.setattr(G.pw_access, "_post", fake_post)
    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")

    T.set_task(task_id="task-9", filename="lecture.pdf", input_unit="No. of pages", count=12)
    client = G.OpenAIProxyClient(lambda: "google-token", "gpt-5.6-terra-ard-content")
    response = client.with_options(timeout=30).chat.completions.create(
        model="ignored", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}], max_tokens=8,
    )

    assert response.choices[0].message.content == "OK"
    assert seen["path"] == "/api/gpt/generate"
    assert seen["timeout"] == 30
    assert seen["payload"]["app"] == "Handwritten Notes Automation"
    assert seen["payload"]["model"] == "gpt-5.6-terra-ard-content"
    assert seen["payload"]["task_id"] == "task-9" and seen["payload"]["filename"] == "lecture.pdf"
    assert seen["payload"]["input_unit"] == "No. of pages" and seen["payload"]["count"] == 12
    assert seen["token"] == "google-token"      # the user's Google token, never a provider key
    assert T.task_cost_inr() == 2.5             # proxy-reported cost reaches the run total


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("client_timeout,call_timeout,expected", [
    (None, None, 300), (30, None, 30), (30, 0.125, 0.125), (None, 180, 180),
])
def test_gpt_timeout_reaches_http_on_both_routes(monkeypatch, override, client_timeout,
                                               call_timeout, expected):
    monkeypatch.setenv("PW_PROXY_GPT_PATH", "/api/gpt/v2/generate" if override else "")
    seen = []
    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(G.pw_access, "_post", lambda path, tok, payload, timeout: (
        seen.append(timeout) or FakeResponse(200, {"choices": []})))
    client = G.OpenAIProxyClient(lambda: "tok").with_options(timeout=client_timeout)
    options = {"timeout": call_timeout} if call_timeout is not None else {}
    client.chat.completions.create(messages=[], **options)
    assert seen == [expected]


def test_gpt_timeout_is_visible_in_row_log_without_response_content(monkeypatch):
    import requests
    from openai import APITimeoutError
    from notesbot.generation_profile import call_label
    from sheets_bridge import pw_transport as T

    lines = []
    T.set_task(task_id="latency-test")
    T.set_call_log(lines.append)
    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    def fail(*a, **k):
        raise requests.exceptions.Timeout("private request content")
    monkeypatch.setattr(G.pw_access, "_post", fail)
    try:
        with call_label("pages 9-15"), pytest.raises(APITimeoutError):
            G.OpenAIProxyClient(lambda: "tok", timeout=180).chat.completions.create(
                messages=[], response_format={"type": "json_schema", "json_schema": {
                    "name": "ShortNotes", "schema": {"type": "object"}}})
    finally:
        T.set_call_log(None)
    assert "pages 9-15: notes call started (request timeout 180s)" in lines
    assert any("failed after" in line and "APITimeoutError" in line for line in lines)
    assert "private request content" not in " ".join(lines)


def test_large_requests_go_through_the_blob_route(monkeypatch):
    posted: dict = {}

    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "https://blob/req.json")
    monkeypatch.setattr(G.pw_access, "_post", lambda path, tok, payload, timeout: (
        posted.update(payload) or FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]})))

    G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])

    assert posted["request_blob_url"] == "https://blob/req.json" and "request" not in posted


@pytest.mark.parametrize("status,expected", [
    (404, "no GPT route"),
    (403, "enable GPT"),
    (401, "enable GPT"),
    (500, "status 500"),
])
def test_failures_say_which_side_to_fix_without_echoing_the_body(monkeypatch, status, expected):
    from sheets_bridge.pw_transport import ProxyRequestError

    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(G.pw_access, "_post",
                        lambda *a, **k: FakeResponse(status, text="secret proxy body"))

    with pytest.raises(ProxyRequestError) as caught:
        G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])

    assert expected in str(caught.value)
    assert "secret proxy body" not in str(caught.value)


def test_preflight_stops_a_batch_before_it_claims_rows(monkeypatch):
    from notesbot import gemini_client

    from sheets_bridge.model_check import preflight

    # a gemini run is never charged for a preflight call
    calls: list = []
    monkeypatch.setattr(gemini_client, "_lite_client", lambda: calls.append("called"))
    assert preflight("gemini-3.7-flash") == "" and not calls

    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(G.pw_access, "_post", lambda *a, **k: FakeResponse(404))
    monkeypatch.setattr(gemini_client, "_lite_client",
                        lambda: G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content"))
    assert "no GPT route" in preflight("gpt-5.6-terra-ard-content")

    monkeypatch.setattr(G.pw_access, "_post", _by_kind(FakeResponse(
        200, {"choices": [{"message": {"content": "OK"}}]}), FakeResponse(200, NOTES_REPLY)))
    assert preflight("gpt-5.6-terra-ard-content") == ""

    monkeypatch.setattr(G.pw_access, "_post", lambda *a, **k: FakeResponse(
        200, {"choices": [{"message": {"content": "  "}}]}))
    assert "returned no text" in preflight("gpt-5.6-terra-ard-content")


def test_transport_errors_map_onto_the_retries_the_generator_knows(monkeypatch):
    import requests
    from openai import APITimeoutError

    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")

    def timeout(*_a, **_k):
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(G.pw_access, "_post", timeout)
    with pytest.raises(APITimeoutError):
        G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])


# --------------------------------------------------------------------------- #
# The kit helper is the path of record; the direct post is only a fallback
# --------------------------------------------------------------------------- #
def test_kit_helper_is_used_and_mirrors_the_gemini_payload(monkeypatch):
    """pw_access.gpt_generate is a local addition until PW ships one. It must
    send exactly what gemini_generate sends, so the usage ledger matches."""
    import pw_access

    from sheets_bridge import pw_transport as T

    seen = {}
    monkeypatch.setattr(pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(pw_access, "_post", lambda path, tok, payload, timeout: (
        seen.update(path=path, payload=payload) or FakeResponse(
            200, {"choices": [{"message": {"content": "OK"}}]})))

    T.set_task(task_id="task-7", filename="lecture.pdf", input_unit="No. of pages", count=9)
    G.OpenAIProxyClient(lambda: "google-token", "gpt-5.6-terra-ard-content").chat.completions.create(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}], max_tokens=8)

    assert seen["path"] == "/api/gpt/generate"
    assert set(seen["payload"]) == {"app", "model", "request", "filename",
                                    "input_unit", "count", "video_duration", "task_id"}
    assert seen["payload"]["task_id"] == "task-7"
    assert seen["payload"]["app"] == "Handwritten Notes Automation"


def test_task_id_is_omitted_when_there_is_no_task(monkeypatch):
    """The kit only sets task_id when it has one; an empty string would make a
    ledger row that groups under nothing."""
    import pw_access

    seen = {}
    monkeypatch.setattr(pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(pw_access, "_post", lambda path, tok, payload, timeout: (
        seen.update(payload=payload) or FakeResponse(200, {"choices": []})))

    pw_access.gpt_generate(lambda: "tok", model="gpt-5.6-terra-ard-content", request={"messages": []})
    assert "task_id" not in seen["payload"]


def test_route_override_bypasses_the_helper(monkeypatch):
    """PW may publish the route under another path; the override must win."""
    import pw_access

    monkeypatch.setenv("PW_PROXY_GPT_PATH", "/api/gpt/v2/generate")
    called = []
    monkeypatch.setattr(pw_access, "gpt_generate",
                        lambda *a, **k: called.append("helper") or {"choices": []})
    monkeypatch.setattr(pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(pw_access, "_post", lambda path, tok, payload, timeout: (
        called.append(path) or FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]})))

    G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert called == ["/api/gpt/v2/generate"]


# --------------------------------------------------------------------------- #
# A failed preflight must say what the proxy answered
# --------------------------------------------------------------------------- #
def _gpt_client(monkeypatch):
    from notesbot import gemini_client

    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(gemini_client, "_lite_client",
                        lambda: G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content"))


def _answers(monkeypatch, *responses):
    seen = []
    queue = list(responses)

    def post(path, tok, payload, timeout):
        seen.append(payload)
        return queue.pop(0)

    monkeypatch.setattr(G.pw_access, "_post", post)
    return seen


def test_gateway_timeout_is_named_with_the_proxys_own_words(monkeypatch):
    """The first live run said only `APIConnectionError`, which covers 502, 503,
    504 and a dropped connection alike."""
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    _answers(monkeypatch,
             FakeResponse(504, text="upstream request timed out\n  after 20s"),
             FakeResponse(504, text="upstream request timed out\n  after 20s"))
    slept = []
    problem = preflight("gpt-5.6-terra-ard-content", sleep=slept.append)

    assert "HTTP 504" in problem and "gateway timeout" in problem
    assert "2 attempts" in problem and slept == [5.0]
    assert "Proxy said: upstream request timed out after 20s" in problem   # one line
    assert "Gemini model in the dropdown" in problem


def test_one_gateway_blip_does_not_cancel_the_batch(monkeypatch):
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    _answers(monkeypatch,
             FakeResponse(502, text="bad gateway"),
             FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
             FakeResponse(200, NOTES_REPLY))
    assert preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None) == ""


def test_permission_refusal_is_not_retried(monkeypatch):
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    seen = _answers(monkeypatch, FakeResponse(403, text="app not allowed"))
    problem = preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: pytest.fail("retried"))
    assert "enable GPT" in problem and len(seen) == 1
    assert "app not allowed" not in problem          # app-written advice only


def test_dropped_connection_is_reported_as_no_answer(monkeypatch):
    import requests

    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)

    def drop(*_a, **_k):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(G.pw_access, "_post", drop)
    problem = preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None)
    assert "no answer from the proxy (APIConnectionError" in problem and "Proxy said" not in problem


def test_probe_leaves_room_for_reasoning_and_explains_an_empty_answer(monkeypatch):
    from sheets_bridge.model_check import PROBE_MAX_TOKENS, preflight

    _gpt_client(monkeypatch)
    seen = _answers(monkeypatch, FakeResponse(200, {"choices": [
        {"finish_reason": "length", "message": {"content": ""}}]}))
    problem = preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None)

    assert seen[0]["request"]["max_completion_tokens"] == PROBE_MAX_TOKENS >= 256
    assert "whole output budget" in problem


def test_real_runs_keep_the_status_but_never_log_the_proxy_text(monkeypatch):
    from openai import APIConnectionError

    from sheets_bridge.errors import describe_exception

    _gpt_client(monkeypatch)
    _answers(monkeypatch, FakeResponse(503, text="lecture text echoed back"))
    with pytest.raises(APIConnectionError) as caught:
        G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
            messages=[{"role": "user", "content": [{"type": "text", "text": "lecture text"}]}])

    assert caught.value.proxy_status == 503
    assert "lecture text" not in describe_exception(caught.value)
    assert "lecture text" not in str(caught.value)


# The exact envelope the live /api/gpt/generate returned on 16 Sep 2026 for the
# probe prompt (trimmed). Its top-level "usage" is the proxy's own block; the
# completion sits under "result". Reading the envelope as the completion made
# every GPT answer look empty.
LIVE_REPLY = {
    "result": {
        "id": "chatcmpl-EOlYqfk1lsAU9GSzipYyecLgGAj6P",
        "object": "chat.completion",
        "model": "gpt-5.6-terra-ard-content",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "OK", "annotations": []}}],
        "usage": {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17,
                  "completion_tokens_details": {"reasoning_tokens": 0},
                  "prompt_tokens_details": {"cache_creation_tokens": 0}},
        "service_tier": "default",
    },
    "model": "gpt-5.6-terra-ard-content",
    "usage": {"tokens_in": 13, "tokens_out": 4, "tokens_in_uncached": 13,
              "cache_creation_tokens": 0, "cache_read_tokens": 0},
    "cost_inr": 0.007,
    "cost_source": "gateway",
}


def test_live_proxy_envelope_yields_the_answer_usage_and_cost(monkeypatch):
    from sheets_bridge import pw_transport as T

    shaped = G.openai_shaped(LIVE_REPLY)
    assert shaped.choices[0].message.content == "OK"
    assert shaped.choices[0].finish_reason == "stop"
    assert (shaped.usage.prompt_tokens, shaped.usage.completion_tokens, shaped.usage.total_tokens) == (13, 4, 17)
    assert shaped.usage.prompt_tokens_details.cached_tokens == 0

    # the cost the proxy reports reaches the run total through the real client
    monkeypatch.setattr(G.pw_access, "_upload_large_json", lambda *a, **k: "")
    monkeypatch.setattr(G.pw_access, "_post", lambda *a, **k: FakeResponse(200, LIVE_REPLY))
    T.set_task(task_id="t", filename="f", input_unit="No. of pages", count=1)
    reply = G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content").chat.completions.create(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert reply.choices[0].message.content == "OK"
    assert T.task_cost_inr() == 0.007


def test_envelope_counters_fill_in_when_the_completion_has_no_usage():
    reply = {"result": {"choices": [{"message": {"content": "x"}}]},
             "usage": {"tokens_in": 20, "tokens_out": 7, "cache_read_tokens": 5}}
    shaped = G.openai_shaped(reply)
    assert (shaped.usage.prompt_tokens, shaped.usage.completion_tokens, shaped.usage.total_tokens) == (20, 7, 27)
    assert shaped.usage.prompt_tokens_details.cached_tokens == 5


def test_preflight_passes_on_the_live_reply(monkeypatch):
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    _answers(monkeypatch, FakeResponse(200, LIVE_REPLY), FakeResponse(200, NOTES_REPLY))
    assert preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None) == ""


@pytest.mark.parametrize("extra,env,kept", [
    ({"reasoning_effort": "high"}, "", False),   # the generator's normal call
    (None, "", False),                           # model default: reasoning is on
    ({"reasoning_effort": "high"}, "none", True),  # operator turned reasoning off
    ({"reasoning_effort": "none"}, "", True),
])
def test_temperature_is_only_sent_when_reasoning_is_off(monkeypatch, extra, env, kept):
    if env:
        monkeypatch.setenv("PW_PROXY_REASONING_EFFORT", env)
    else:
        monkeypatch.delenv("PW_PROXY_REASONING_EFFORT", raising=False)
    kwargs = {"messages": [], "temperature": 0.3}
    if extra is not None:
        kwargs["extra_body"] = extra
    request = G.build_openai_request(kwargs, model="gpt-5.6-terra-ard-content")
    assert ("temperature" in request) is kept


def test_null_fields_fall_back_to_the_apps_defaults():
    """Strict mode made defaulted fields nullable; the app's models reject
    null for fields like `points: list[str] = []`, so nulls are dropped."""
    from notesbot.prompts import ShortNotes

    raw = ('{"lecture_no": "1", "subject": "Physics", "title": "Motion", "mindmap": null,'
           ' "sections": [{"heading": "Inertia", "points": ["A body stays at rest."],'
           ' "tricks": null, "table": null, "source_local_page": 1, "source_order": 1}]}')
    with pytest.raises(Exception):
        ShortNotes.model_validate_json(raw)            # what would have failed every row

    cleaned = G._clean_json_text(raw)
    notes = ShortNotes.model_validate_json(cleaned)
    assert notes.sections[0].tricks == [] and notes.mindmap is None
    assert G._clean_json_text("not json") == "not json"
    assert G._drop_nulls({"a": [None, 1]}) == {"a": [None, 1]}   # list items untouched


def test_json_replies_are_cleaned_but_plain_text_is_not(monkeypatch):
    _gpt_client(monkeypatch)
    body = dict(LIVE_REPLY, result=dict(LIVE_REPLY["result"], choices=[
        {"finish_reason": "stop", "message": {"content": '{"a": 1, "b": null}'}}]))
    _answers(monkeypatch, FakeResponse(200, body), FakeResponse(200, body))
    client = G.OpenAIProxyClient(lambda: "tok", "gpt-5.6-terra-ard-content")

    as_json = client.chat.completions.create(messages=[], response_format={
        "type": "json_schema", "json_schema": {"name": "S", "schema": {"type": "object"}}})
    assert as_json.choices[0].message.content == '{"a": 1}'
    as_text = client.chat.completions.create(messages=[])
    assert as_text.choices[0].message.content == '{"a": 1, "b": null}'


def test_pdf_probe_checks_the_notes_actually_read_the_page():
    from types import SimpleNamespace

    from sheets_bridge.model_check import check_pdf_probe

    def reply(content):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    good = ('{"lecture_no": "1", "subject": "Physics", "title": "Laws of motion", "sections": ['
            '{"heading": "First law", "points": ["A body stays at rest unless a net force acts."],'
            ' "source_local_page": 1, "source_order": 1}]}')
    assert check_pdf_probe(reply(good)) == ""
    assert "no notes JSON" in check_pdf_probe(reply(""))
    assert "did not match" in check_pdf_probe(reply('{"sections": "nope"}'))
    unread = (good.replace("A body stays at rest unless a net force acts.", "Nothing here.")
              .replace("First law", "Intro").replace("Laws of motion", "Lecture"))
    problem = check_pdf_probe(reply(unread))
    assert "attachment may not have been read" in problem and "nothing here" in problem

    # a live run put the page's teaching in key terms, not bullets: still read
    in_key_terms = ('{"lecture_no": "1", "subject": "Physics", "title": "Lecture", "sections": ['
                    '{"heading": "Intro", "points": [], "key_terms": ["Inertia: a body keeps its state"],'
                    ' "source_local_page": 1, "source_order": 1}]}')
    assert check_pdf_probe(reply(in_key_terms)) == ""
    assert "no sections" in check_pdf_probe(reply(
        '{"lecture_no": "1", "subject": "Physics", "title": "Laws of motion", "sections": []}'))


NOTES_JSON = ('{"lecture_no": "1", "subject": "Physics", "title": "Laws of motion", "sections": ['
              '{"heading": "First law", "points": ["A body stays at rest unless a net force acts."],'
              ' "source_local_page": 1, "source_order": 1}]}')
NOTES_REPLY = dict(LIVE_REPLY, result=dict(LIVE_REPLY["result"], choices=[
    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": NOTES_JSON}}]))


def _by_kind(text_reply, notes_reply):
    """Answer the text probe and the notes-shaped probe differently."""
    def post(path, tok, payload, timeout):
        return notes_reply if "response_format" in payload["request"] else text_reply
    return post


def test_preflight_catches_a_refused_notes_call_the_text_probe_misses(monkeypatch):
    """The first live GPT run: 'OK' to plain text, HTTP 400 to every real call."""
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    refusal = FakeResponse(400, text=(
        '{"error":"gpt gateway error","detail":{"error":{"message":"litellm.UnsupportedParamsError: '
        "azure/gpt-5.6-terra does not support temperature=0.2 while reasoning is active.\"}}}"))
    monkeypatch.setattr(G.pw_access, "_post", _by_kind(FakeResponse(200, LIVE_REPLY), refusal))

    problem = preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None)
    assert "refused a notes-shaped call" in problem
    assert "temperature=0.2 while reasoning is active" in problem


def test_notes_probe_sends_what_the_generator_sends(monkeypatch):
    """Same schema, a real PDF part, and reasoning on - so the probe fails
    wherever a real call would."""
    from sheets_bridge.model_check import preflight

    _gpt_client(monkeypatch)
    seen = _answers(monkeypatch, FakeResponse(200, LIVE_REPLY), FakeResponse(200, NOTES_REPLY))
    assert preflight("gpt-5.6-terra-ard-content", sleep=lambda _s: None) == ""

    notes_call = seen[1]["request"]
    parts = notes_call["messages"][0]["content"]
    assert any(part.get("type") == "file" and part["file"]["file_data"].startswith("data:application/pdf;base64,")
               for part in parts)
    assert notes_call["response_format"]["json_schema"]["name"] == "ShortNotes"
    assert notes_call["response_format"]["json_schema"]["strict"] is True
    assert "reasoning_effort" in notes_call and "temperature" not in notes_call
