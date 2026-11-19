"""Route the generator's model calls to a GPT model through the PW proxy.

`notesbot` already talks to its gateway with an OpenAI-compatible client, so
unlike the Gemini adapter in :mod:`pw_transport` this one barely translates
anything: the request body *is* an OpenAI chat-completions body.

  OpenAI chat.completions.create(...)  -->  POST /api/gpt/generate
  OpenAI chat completion               -->  the response object notesbot expects

What it still has to do:

* make the JSON schema strict-mode clean (OpenAI structured outputs require
  every property to be required and no additional properties),
* send ``max_completion_tokens`` instead of ``max_tokens`` (reasoning models
  reject the old name),
* carry the same ledger metadata (task id, filename, page count) as every
  other proxy call, so one lecture stays one task in the usage sheet.

The app ships no OpenAI key: the call is authorised by the signed-in user's
Google token, exactly like the Gemini route.

Calls go through the kit's own `pw_access.gpt_generate` (pw-app-kit v2, the
2026-09-16 update). The direct post below remains only for an older kit copy
without that helper, or for a `PW_PROXY_GPT_PATH` override.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pw_access

from .pw_transport import ProxyRequestError, _record_cost, _translate_error, current_task, schema_name, log_call_event

# The proxy's GPT route. `/api/openai/generate` does not exist (404); the live
# route is `/api/gpt/generate`, alongside `/api/gemini/generate`.
DEFAULT_ROUTE = "/api/gpt/generate"
# Balances capability and cost in OpenAI's own line-up, and supports PDF/image
# input plus structured outputs, which this pipeline needs on every call.
DEFAULT_MODEL = "gpt-5.6-terra-ard-content"

# Structured-output keywords OpenAI's strict mode accepts. Everything else
# (title, default, minimum, maxItems, format...) is dropped: pydantic still
# validates the answer on arrival, so the constraints lose nothing.
_STRICT_KEEP = {"type", "description", "enum", "items", "properties", "required",
                "anyOf", "$ref", "$defs", "additionalProperties"}


def route() -> str:
    return (os.environ.get("PW_PROXY_GPT_PATH", "") or DEFAULT_ROUTE).strip()


def is_gpt_model(model: str) -> bool:
    """True for ids this transport handles (``gpt-5.6-terra-ard-content``, ``openai/gpt-...``)."""
    name = (model or "").strip().lower()
    return name.split("/")[-1].startswith("gpt")


# --------------------------------------------------------------------------- #
# JSON Schema -> OpenAI strict schema
# --------------------------------------------------------------------------- #
def _nullable(node: dict) -> dict:
    """Strict mode requires every property; optional ones become nullable."""
    options = node.get("anyOf")
    if isinstance(options, list):
        if any(isinstance(option, dict) and option.get("type") == "null" for option in options):
            return node
        return {**node, "anyOf": [*options, {"type": "null"}]}
    if node.get("type") == "null":
        return node
    return {"anyOf": [node, {"type": "null"}]}


def strict_schema(schema: dict) -> dict:
    """Convert a pydantic JSON schema into one OpenAI strict mode accepts.

    ``$ref``/``$defs`` are kept rather than inlined, so recursive models (the
    mind map's branches) stay finite.
    """
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        # A $ref may not carry siblings in strict mode.
        return {"$ref": schema["$ref"]}

    out: dict = {key: value for key, value in schema.items() if key in _STRICT_KEEP}

    defs = schema.get("$defs")
    if isinstance(defs, dict):
        out["$defs"] = {name: strict_schema(child) for name, child in defs.items()
                        if isinstance(child, dict)}

    options = schema.get("anyOf")
    if isinstance(options, list):
        out["anyOf"] = [strict_schema(option) for option in options if isinstance(option, dict)]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        required = set(schema.get("required") or ())
        kept: dict = {}
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            if child.get("maxItems") == 0:
                # The app forbids this field outright (worked examples). Strict
                # mode would force the model to emit it, so drop it instead and
                # let the pydantic default stand.
                continue
            converted = strict_schema(child)
            if name not in required or "default" in child:
                converted = _nullable(converted)
            kept[name] = converted
        out["type"] = "object"
        out["properties"] = kept
        out["required"] = list(kept)
        out["additionalProperties"] = False
        return out

    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = strict_schema(items)
    return out


def _drop_nulls(value):
    """Remove ``"key": null`` pairs, recursively; list items are left alone.

    Strict mode made every defaulted field nullable so the model can decline it
    (see `strict_schema`). The app's models give those fields defaults but do
    not all accept None (``points: list[str] = []``), so a null would fail
    validation. Every field that could come back null has a default, so
    dropping the pair restores exactly the value the app expects.
    """
    if isinstance(value, dict):
        return {key: _drop_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_nulls(item) for item in value]
    return value


def _clean_json_text(text: str) -> str:
    import json

    try:
        return json.dumps(_drop_nulls(json.loads(text)), ensure_ascii=False)
    except (TypeError, ValueError):
        return text          # not JSON: the generator's own checks report it


# --------------------------------------------------------------------------- #
# notesbot request -> OpenAI request
# --------------------------------------------------------------------------- #
def build_openai_request(kwargs: dict, *, model: str) -> dict:
    """Translate chat.completions.create(**kwargs) into a GPT request body."""
    request: dict = {"model": model, "messages": list(kwargs.get("messages") or ())}

    if kwargs.get("temperature") is not None:
        request["temperature"] = kwargs["temperature"]
    if kwargs.get("max_tokens") is not None:
        # Reasoning models reject `max_tokens`.
        request["max_completion_tokens"] = kwargs["max_tokens"]

    response_format = kwargs.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        json_schema = response_format.get("json_schema") or {}
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": json_schema.get("name") or "Response",
                "schema": strict_schema(json_schema.get("schema") or {}),
                "strict": True,
            },
        }

    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        request.update(extra_body)
    effort = (os.environ.get("PW_PROXY_REASONING_EFFORT", "") or "").strip()
    if effort:
        request["reasoning_effort"] = effort
    if "temperature" in request and request.get("reasoning_effort") != "none":
        # The live route refuses it (HTTP 400): "azure/gpt-5.6-terra doesn't
        # support temperature=0.2 while reasoning is active. Only temperature=1
        # is supported." Reasoning is on unless explicitly disabled, and the
        # generator asks for 0.1-0.3 on every call, so use the provider default.
        request.pop("temperature")
    return request


# --------------------------------------------------------------------------- #
# OpenAI response -> the object notesbot reads
# --------------------------------------------------------------------------- #
def _unwrap(payload: dict) -> dict:
    """The chat completion inside the proxy's reply.

    The live route answers ``{"result": <chat.completion>, "usage":
    {"tokens_in", "tokens_out", ...}, "cost_inr", ...}``: the envelope carries
    a ``usage`` block of its own. Only ``choices`` identifies the completion -
    treating the envelope's ``usage`` as one read every answer as empty.
    """
    if not isinstance(payload, dict):
        return {}
    if "choices" in payload:
        return payload
    for key in ("result", "response", "data", "openai", "gpt", "body"):
        inner = payload.get(key)
        if isinstance(inner, dict) and "choices" in inner:
            return inner
    return payload


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    text = ""
    for part in content or ():
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text += part["text"]
    return text


def openai_shaped(payload: dict) -> SimpleNamespace:
    body = _unwrap(payload)
    choices = body.get("choices") or []
    text = ""
    finish = "stop"
    if choices:
        first = choices[0] or {}
        finish = str(first.get("finish_reason") or "stop")
        text = _text_of(first.get("message") or {})
    meta = body.get("usage") or {}
    # The proxy's own counters, for a completion that arrives without usage.
    envelope = payload.get("usage") if isinstance(payload, dict) and body is not payload else {}
    envelope = envelope if isinstance(envelope, dict) else {}

    def _count(*names: str) -> int:
        for source in (meta, envelope):
            for name in names:
                value = source.get(name)
                if isinstance(value, int) and value >= 0:
                    return value
        return 0

    details = meta.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(cached, int):
        cached = envelope.get("cache_read_tokens", 0)
    prompt_tokens = _count("prompt_tokens", "input_tokens", "tokens_in")
    completion_tokens = _count("completion_tokens", "output_tokens", "tokens_out")
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        # OpenAI already counts reasoning tokens inside completion_tokens.
        completion_tokens=completion_tokens,
        total_tokens=_count("total_tokens") or prompt_tokens + completion_tokens,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=cached if isinstance(cached, int) and cached >= 0 else 0
        ),
    )
    choice = SimpleNamespace(index=0, finish_reason=finish,
                             message=SimpleNamespace(role="assistant", content=text))
    # The untouched proxy reply, kept in memory only: --test-model prints its
    # *shape* when no text was found, which is how a format change is spotted.
    return SimpleNamespace(id="pw-proxy-gpt", choices=[choice], usage=usage,
                           model=str(body.get("model") or ""), _raw=payload)


# --------------------------------------------------------------------------- #
# The stand-in client
# --------------------------------------------------------------------------- #
_STATUS_RE = re.compile(r"proxy error (\d{3})")

# What an operator should read into each gateway status.
STATUS_MEANING = {
    500: "the proxy failed while handling the GPT call",
    502: "bad gateway - the proxy's GPT backend returned an invalid answer or failed",
    503: "service unavailable - the GPT backend is down or overloaded",
    504: "gateway timeout - the GPT backend did not answer in time",
}


def _status_of(exc: BaseException) -> int:
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else 0


def _detail_of(exc: BaseException) -> str:
    """The proxy's own words after `gpt proxy error 504:`, one line, capped.

    Only the preflight and --test-model print this, and both send a fixed
    "reply OK" prompt, so the text cannot echo lecture material. Real runs keep
    it on the exception and never log it.
    """
    text = str(exc)
    match = _STATUS_RE.search(text)
    if match is None:
        return ""
    return " ".join(text[match.end():].lstrip(": ").split())[:200]


def _route_error_from(exc: BaseException) -> ProxyRequestError | None:
    """Rewrite the kit's `gpt proxy error 404: <body>` as actionable advice.

    The kit's message carries the proxy's response body, which can echo request
    text, so it is never passed on - only the status is read from it.
    """
    match = _STATUS_RE.search(str(exc))
    if match is None:
        return None
    status = int(match.group(1))
    return _route_error(status) if status in (401, 403, 404) else None


def _route_error(status: int) -> ProxyRequestError:
    """Say which side needs fixing, without echoing the proxy's body."""
    if status == 404:
        return ProxyRequestError(
            f"the PW proxy has no GPT route at {route()} (HTTP 404). Set "
            "PW_PROXY_GPT_PATH to the route PW published, or switch "
            "PW_PROXY_MODEL back to a gemini- model."
        )
    if status in (401, 403):
        return ProxyRequestError(
            f"the PW proxy refused the GPT route (HTTP {status}). Ask the PW "
            f"proxy team to enable GPT for '{pw_access.APP_NAME}'; the Gemini "
            "route is unaffected."
        )
    return ProxyRequestError(f"GPT proxy call failed (status {status or 'unknown'})")


def gpt_generate(token_provider, *, model: str, request: dict, filename: str = "",
                 input_unit: str = "", count: Any = None, task_id: str = "",
                 app: str = pw_access.APP_NAME, timeout: float | None = None) -> dict:
    """Call GPT through the proxy, the way the kit calls Gemini and Claude.

    The kit's helper is the normal path. The direct post below stays as the
    fallback for an older kit copy without it, and for a route override.
    """
    shipped = getattr(pw_access, "gpt_generate", None) or getattr(pw_access, "openai_generate", None)
    if callable(shipped) and route() == DEFAULT_ROUTE:
        return shipped(token_provider, model=model, request=request, filename=filename,
                       input_unit=input_unit, count=count, task_id=task_id, app=app)

    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "task_id": task_id,
    }
    blob_url = pw_access._upload_large_json(token_provider, app, request, "gpt")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    response = pw_access._post(route(), token_provider, payload,
                               pw_access._AI_TIMEOUT if timeout is None else timeout)
    if response.status_code != 200:
        raise _route_error(response.status_code)
    return response.json()


@dataclass
class _Completions:
    client: "OpenAIProxyClient"

    def create(self, **kwargs) -> SimpleNamespace:
        return self.client._create(**kwargs)


@dataclass
class OpenAIProxyClient:
    """Minimal stand-in for the OpenAI client used by notesbot."""

    token_provider: Callable[[], str]
    model: str = DEFAULT_MODEL
    timeout: float | None = None
    chat: Any = field(init=False)

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions(self))

    def with_options(self, **kwargs) -> "OpenAIProxyClient":
        return OpenAIProxyClient(self.token_provider, self.model,
                                 kwargs.get("timeout", self.timeout))

    def close(self) -> None:
        return None

    def _create(self, **kwargs) -> SimpleNamespace:
        task = current_task()
        request = build_openai_request(kwargs, model=self.model)
        timeout = kwargs.get("timeout", self.timeout)
        started = time.monotonic()
        schema = schema_name(kwargs)
        log_call_event(schema, f"started (request timeout {timeout if timeout is not None else pw_access._AI_TIMEOUT:g}s)")
        try:
            payload = gpt_generate(
                self.token_provider,
                model=self.model,
                request=request,
                filename=str(task.get("filename") or ""),
                input_unit=str(task.get("input_unit") or ""),
                count=task.get("count"),
                task_id=str(task.get("task_id") or ""),
                timeout=timeout,
            )
        except ProxyRequestError:
            log_call_event(schema, f"failed after {time.monotonic() - started:.1f}s (proxy request rejected)")
            raise
        except Exception as exc:  # noqa: BLE001 - never carry proxy text across
            translated = _route_error_from(exc) or _translate_error(exc)
            # 502/503/504 all become APIConnectionError so the generator retries
            # them - which also erases which one it was. Keep the status (and,
            # for the fixed-prompt diagnostics only, the proxy's text) on the
            # exception; neither is ever part of a log line on a real run.
            translated.proxy_status = _status_of(exc)
            translated.proxy_detail = _detail_of(exc)
            status = f", HTTP {translated.proxy_status}" if translated.proxy_status else ""
            log_call_event(schema, f"failed after {time.monotonic() - started:.1f}s ({type(translated).__name__}{status})")
            raise translated from None
        _record_cost(payload, schema_name(kwargs))
        log_call_event(schema, f"completed in {time.monotonic() - started:.1f}s")
        shaped = openai_shaped(payload)
        if (kwargs.get("response_format") or {}).get("type") == "json_schema":
            message = shaped.choices[0].message
            message.content = _clean_json_text(message.content)
        return shaped
