"""Route every model call of the (unmodified) generator through the PW proxy.

`notesbot` talks to its gateway with an OpenAI-compatible client. This module
provides a drop-in stand-in for that client which speaks to
``pw_access.gemini_generate`` instead, so the app ships no provider keys and
every call is logged by the proxy as a raw usage row.

  OpenAI chat.completions.create(...)   -->  Gemini generateContent body
  Gemini candidates/usageMetadata       -->  OpenAI-shaped response object

`install()` swaps the client into `notesbot.gemini_client` at runtime; the
generator package itself is never modified.
"""
from __future__ import annotations

import base64
import contextvars
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pw_access
from notesbot.errors import ModelBudgetError

# Per-task metadata for the proxy's usage ledger (one task = one sheet row).
_TASK: contextvars.ContextVar[dict] = contextvars.ContextVar("pw_task", default={})
# The proxy reports what each call actually cost; that beats any local estimate.
_COST: contextvars.ContextVar[list] = contextvars.ContextVar("pw_cost", default=[])
# (kind, cost) per call of the current task, for the per-call cost log.
_CALLS: contextvars.ContextVar[list] = contextvars.ContextVar("pw_calls", default=[])
# Where a line per call goes (the row log), or None for no per-call lines.
_CALL_LOG: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar("pw_call_log", default=None)

# What each response schema is for, in the words the run log uses.
_CALL_KINDS = {
    "ShortNotes": "notes",
    "CoverageReview": "source review",
    "RepetitionReview": "duplicate pass",
    "QualityReview": "quality check",
}

# Gemini model used through the proxy. The kit documents plain Gemini ids
# (e.g. "gemini-2.5-flash"); the vertex_ai/ prefix is a LiteLLM-ism.
DEFAULT_MODEL = "gemini-3.7-flash"

# What the Studio's model dropdown offers, newest-proven first. Text-generation
# models only: the proxy's GPT catalog (GET /api/gpt/models) also lists image
# and TTS ids, which this pipeline cannot use. Anything not on this list is
# refused before a run starts, so a typo never reaches the proxy.
SELECTABLE_MODELS = (
    ("gemini-3.7-flash", "Gemini 3.7 Flash - proven default"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gpt-5.6-terra-ard-content", "GPT-5.6 Terra - balanced"),
    ("gpt-5.6-sol-ard-content", "GPT-5.6 Sol - strongest"),
    ("gpt-5.6-luna-ard-content", "GPT-5.6 Luna - cheapest"),
)


def selectable_models() -> list:
    """[{id, label, current}] for the dropdown."""
    import os

    current = (os.environ.get("PW_PROXY_MODEL", "") or DEFAULT_MODEL).strip().split("/")[-1]
    listed = [{"id": model, "label": label, "current": model == current}
              for model, label in SELECTABLE_MODELS]
    if not any(entry["current"] for entry in listed):
        # A model configured by hand in .env stays selectable and selected.
        listed.insert(0, {"id": current, "label": f"{current} (from .env)", "current": True})
    return listed


def is_selectable(model: str) -> bool:
    return any(entry["id"] == (model or "").strip() for entry in selectable_models())

_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)

# JSON-Schema keywords Gemini's responseSchema accepts.
_SCHEMA_KEEP = {"type", "description", "enum", "items", "properties", "required", "nullable", "format"}
_MAX_SCHEMA_DEPTH = 4  # recursive models (mind-map branches) must be bounded


class ProxyRequestError(RuntimeError):
    """A proxy/provider failure, already stripped of response content."""


def set_task(*, task_id: str, filename: str = "", input_unit: str = "", count: Any = None) -> None:
    """Set the ledger metadata used by every model call of the current task."""
    _TASK.set({"task_id": task_id, "filename": filename, "input_unit": input_unit, "count": count})
    _COST.set([])
    _CALLS.set([])
    _CALL_LOG.set(None)


def set_call_log(log: Callable[[str], None] | None) -> None:
    """Send one line per model call of the current task to ``log``."""
    _CALL_LOG.set(log)


def task_cost_inr() -> float:
    """Sum of the proxy-reported cost of this task's calls so far."""
    return round(sum(_COST.get() or []), 4)


def schema_name(kwargs: dict) -> str:
    """The response schema's name from an OpenAI-style request, or ""."""
    response_format = kwargs.get("response_format") or {}
    if not isinstance(response_format, dict):
        return ""
    return str((response_format.get("json_schema") or {}).get("name") or "")


def call_kind(schema: str = "") -> str:
    """'notes', 'Hindi re-ask', 'repair', 'source review', ... for one call."""
    from notesbot.generation_profile import current_purpose

    return current_purpose() or _CALL_KINDS.get(schema, schema or "call")


def log_call_event(schema: str, event: str) -> None:
    """Expose request timing and safe failure categories in the row's log."""
    log = _CALL_LOG.get()
    if log is None:
        return
    from notesbot.generation_profile import current_label

    try:
        log(f"{current_label() or 'lecture'}: {call_kind(schema)} call {event}")
    except Exception:
        pass


def _record_cost(payload: dict, schema: str = "") -> None:
    value = payload.get("cost_inr") if isinstance(payload, dict) else None
    if not (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0):
        return
    cost = float(value)
    _COST.get().append(cost)
    kind = call_kind(schema)
    _CALLS.get().append((kind, cost))
    log = _CALL_LOG.get()
    if log is None:
        return
    from notesbot.generation_profile import current_label

    try:
        log(f"{current_label() or 'lecture'}: {kind} call - INR {cost:.2f}")
    except Exception:  # noqa: BLE001 - a log line must never cost the call
        pass


def call_breakdown() -> str:
    """'cost by call type: notes 4 x INR 18.20, source review 4 x INR 6.10'."""
    totals: dict[str, list[float]] = {}
    for kind, cost in list(_CALLS.get() or []):
        entry = totals.setdefault(kind, [0, 0.0])
        entry[0] += 1
        entry[1] += cost
    if not totals:
        return ""
    ordered = sorted(totals.items(), key=lambda item: -item[1][1])
    parts = [f"{kind} {int(count)} x INR {total:.2f}" for kind, (count, total) in ordered]
    calls = sum(int(count) for count, _total in totals.values())
    return f"cost by call type ({calls} call(s)): " + ", ".join(parts)


def current_task() -> dict:
    return dict(_TASK.get() or {})


# --------------------------------------------------------------------------- #
# JSON Schema -> Gemini responseSchema
# --------------------------------------------------------------------------- #
def gemini_schema(schema: dict, defs: dict | None = None, depth: int = 0) -> dict | None:
    """Inline $refs and drop keywords Gemini rejects. None = unrepresentable."""
    if not isinstance(schema, dict):
        return None
    defs = defs if defs is not None else schema.get("$defs", {}) or {}

    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(name)
        if target is None or depth >= _MAX_SCHEMA_DEPTH:
            return None
        return gemini_schema(target, defs, depth + 1)

    if "anyOf" in schema:  # pydantic Optional[X] -> anyOf [X, null]
        options = [o for o in schema["anyOf"] if isinstance(o, dict)]
        non_null = [o for o in options if o.get("type") != "null"]
        nullable = len(non_null) != len(options)
        if len(non_null) != 1:
            return None
        converted = gemini_schema(non_null[0], defs, depth)
        if converted is None:
            return None
        if nullable:
            converted["nullable"] = True
        if schema.get("description"):
            converted.setdefault("description", schema["description"])
        return converted

    out: dict = {key: value for key, value in schema.items() if key in _SCHEMA_KEEP}
    node_type = out.get("type")

    if node_type == "object" or "properties" in schema:
        properties = {}
        for name, child in (schema.get("properties") or {}).items():
            converted = gemini_schema(child, defs, depth)
            if converted is not None:
                properties[name] = converted
        if not properties:
            return None
        out["type"] = "object"
        out["properties"] = properties
        required = [r for r in (schema.get("required") or []) if r in properties]
        if required:
            out["required"] = required
        else:
            out.pop("required", None)
        return out

    if node_type == "array" or "items" in schema:
        items = gemini_schema(schema.get("items") or {}, defs, depth)
        if items is None:
            return None
        out["type"] = "array"
        out["items"] = items
        return out

    if not node_type:
        return None
    return out


# --------------------------------------------------------------------------- #
# OpenAI request -> Gemini request
# --------------------------------------------------------------------------- #
def _parts_from_content(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict] = []
    for item in content or ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append({"text": item.get("text", "")})
        elif kind == "file":
            data_url = (item.get("file") or {}).get("file_data", "")
            match = _DATA_URL.match(data_url or "")
            if match:
                parts.append({"inline_data": {"mime_type": match.group(1), "data": match.group(2)}})
        elif kind == "image_url":
            data_url = (item.get("image_url") or {}).get("url", "")
            match = _DATA_URL.match(data_url or "")
            if match:
                parts.append({"inline_data": {"mime_type": match.group(1), "data": match.group(2)}})
    return parts


def build_gemini_request(kwargs: dict, *, with_schema: bool = True) -> dict:
    """Translate chat.completions.create(**kwargs) into a generateContent body."""
    contents = []
    system_text: list[str] = []
    for message in kwargs.get("messages") or ():
        role = message.get("role", "user")
        if role == "system":
            content = message.get("content")
            system_text.append(content if isinstance(content, str) else " ".join(p.get("text", "") for p in _parts_from_content(content)))
            continue
        parts = _parts_from_content(message.get("content"))
        if parts:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})

    generation_config: dict = {}
    if kwargs.get("temperature") is not None:
        generation_config["temperature"] = kwargs["temperature"]
    if kwargs.get("max_tokens") is not None:
        generation_config["maxOutputTokens"] = kwargs["max_tokens"]

    response_format = kwargs.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        generation_config["responseMimeType"] = "application/json"
        if with_schema:
            raw = (response_format.get("json_schema") or {}).get("schema") or {}
            converted = gemini_schema(raw)
            if converted:
                generation_config["responseSchema"] = converted

    body: dict = {"contents": contents}
    if generation_config:
        body["generationConfig"] = generation_config
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(system_text)}]}
    return body


# --------------------------------------------------------------------------- #
# Gemini response -> OpenAI-shaped response
# --------------------------------------------------------------------------- #
def _unwrap(payload: dict) -> dict:
    """The proxy may return the Gemini body directly or inside a wrapper."""
    if not isinstance(payload, dict):
        return {}
    if "candidates" in payload or "usageMetadata" in payload:
        return payload
    for key in ("result", "response", "data", "gemini", "body"):
        inner = payload.get(key)
        if isinstance(inner, dict) and ("candidates" in inner or "usageMetadata" in inner):
            return inner
    return payload


_FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter", "RECITATION": "content_filter"}


def openai_shaped(payload: dict) -> SimpleNamespace:
    body = _unwrap(payload)
    candidates = body.get("candidates") or []
    text = ""
    finish = "stop"
    if candidates:
        first = candidates[0] or {}
        finish = _FINISH.get(str(first.get("finishReason") or "STOP").upper(), "stop")
        for part in ((first.get("content") or {}).get("parts") or []):
            piece = part.get("text")
            if isinstance(piece, str):
                text += piece
    meta = body.get("usageMetadata") or {}

    def _count(name: str) -> int:
        value = meta.get(name)
        return value if isinstance(value, int) and value >= 0 else 0

    usage = SimpleNamespace(
        prompt_tokens=_count("promptTokenCount"),
        completion_tokens=_count("candidatesTokenCount") + _count("thoughtsTokenCount"),
        total_tokens=_count("totalTokenCount"),
        prompt_tokens_details=SimpleNamespace(cached_tokens=_count("cachedContentTokenCount")),
    )
    choice = SimpleNamespace(index=0, finish_reason=finish, message=SimpleNamespace(role="assistant", content=text))
    return SimpleNamespace(id="pw-proxy", choices=[choice], usage=usage, model=str(body.get("modelVersion") or ""))


# --------------------------------------------------------------------------- #
# The stand-in client
# --------------------------------------------------------------------------- #
_STATUS_RE = re.compile(r"proxy error (\d{3})")


def _translate_error(exc: BaseException) -> BaseException:
    """Map proxy failures onto the openai errors the generator retries on."""
    import httpx
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    request = httpx.Request("POST", f"{pw_access.PROXY_BASE_URL}/api/gemini/generate")
    import requests as _requests

    if isinstance(exc, (_requests.exceptions.Timeout,)):
        return APITimeoutError(request=request)
    if isinstance(exc, (_requests.exceptions.ConnectionError,)):
        return APIConnectionError(request=request)
    match = _STATUS_RE.search(str(exc))
    status = int(match.group(1)) if match else 0
    # Read only the known category, exactly as the current original does.
    detail = str(exc).casefold()
    if status and ("budget_exceeded" in detail or "budget has been exceeded" in detail):
        return ModelBudgetError()
    if status == 429:
        return RateLimitError("proxy rate limit", response=httpx.Response(429, request=request), body=None)
    if status in (502, 503, 504):
        return APIConnectionError(request=request)
    # Content is deliberately not carried over: proxy bodies can echo request text.
    return ProxyRequestError(f"proxy call failed (status {status or 'unknown'})")


@dataclass
class _Completions:
    client: "ProxyClient"

    def create(self, **kwargs) -> SimpleNamespace:
        return self.client._create(**kwargs)


@dataclass
class ProxyClient:
    """Minimal stand-in for the OpenAI client used by notesbot."""

    token_provider: Callable[[], str]
    model: str = DEFAULT_MODEL
    timeout: float | None = None
    chat: Any = field(init=False)

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions(self))

    # notesbot calls these two on the raw client
    def with_options(self, **kwargs) -> "ProxyClient":
        clone = ProxyClient(self.token_provider, self.model, kwargs.get("timeout", self.timeout))
        return clone

    def close(self) -> None:
        return None

    def _create(self, **kwargs) -> SimpleNamespace:
        task = current_task()
        model = self.model
        attempt_without_schema = False
        for attempt in (0, 1):
            request = build_gemini_request(kwargs, with_schema=not attempt_without_schema)
            if model.startswith("gemini-3."):
                # Use provider defaults, matching the measured model comparison.
                # New Gemini 3 releases deprecate these sampling overrides.
                request.get("generationConfig", {}).pop("temperature", None)
            try:
                payload = pw_access.gemini_generate(
                    self.token_provider,
                    model=model,
                    request=request,
                    filename=str(task.get("filename") or ""),
                    input_unit=str(task.get("input_unit") or ""),
                    count=task.get("count"),
                    task_id=str(task.get("task_id") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                translated = _translate_error(exc)
                if isinstance(translated, ModelBudgetError):
                    log_call_event(schema_name(kwargs), "failed: model_budget_exceeded")
                # A schema Gemini refuses (400) is worth one retry with the
                # JSON mime type only; the prompt already states the shape and
                # the caller validates with pydantic.
                if (
                    attempt == 0
                    and not attempt_without_schema
                    and isinstance(translated, ProxyRequestError)
                    and "400" in str(exc)
                    and (kwargs.get("response_format") or {}).get("type") == "json_schema"
                ):
                    attempt_without_schema = True
                    continue
                raise translated from None
            _record_cost(payload, schema_name(kwargs))
            return openai_shaped(payload)
        raise ProxyRequestError("proxy call failed")


# --------------------------------------------------------------------------- #
# Installation into the unmodified generator
# --------------------------------------------------------------------------- #
def install(token_provider: Callable[[], str], *, model: str = "", fallbacks=None) -> str:
    """Point `notesbot.gemini_client` at the proxy. Returns the model in use.

    A ``gpt-`` model id selects the proxy's GPT route instead of Gemini's; the
    generator itself is unchanged either way.

    Note Studio: the account and model are kept per run in a context variable, so two runs
    in one process can use different models. A call from a context that never
    called install() uses the last one installed, as before. Legacy alternate
    model settings are accepted, but a gateway budget failure stops the run,
    exactly as it does in the current original.
    """
    import os

    from notesbot import gemini_client

    global _LAST_RUN
    chosen = (model or os.environ.get("PW_PROXY_MODEL", "") or DEFAULT_MODEL).strip()
    chosen = chosen.split("/")[-1]  # a vertex_ai/ prefix is not a proxy model id
    order = [chosen]
    for extra in (FALLBACKS if fallbacks is None else fallbacks):
        extra = str(extra or "").strip().split("/")[-1]
        if extra and extra not in order:
            order.append(extra)
    run = {"token_provider": token_provider, "order": order, "index": 0}
    _RUN.set(run)
    _LAST_RUN = run

    gemini_client._use_litellm = lambda: True
    gemini_client._lite_model = _current_model
    gemini_client._lite_client = _current_client
    gemini_client._build_visual_lite_client = _current_client
    return _current_model()


# --------------------------------------------------------------------------- #
# Note Studio: one run's model order (a context variable)
# --------------------------------------------------------------------------- #
# Models to fall back to when a run's model has spent its budget; Note Studio
# sets this from its Settings. Empty: no fallback (the original behaviour).
FALLBACKS: tuple = ()
BUDGET_PAUSE_SECONDS = 30 * 60
_RUN: contextvars.ContextVar[dict | None] = contextvars.ContextVar("pw_run", default=None)
_LAST_RUN: dict = {}
_SPENT: dict = {}                             # provider -> time its budget may be asked again


def _provider(model: str) -> str:
    from . import pw_openai

    return "gpt" if pw_openai.is_gpt_model(model) else "gemini"


def _paused(model: str) -> bool:
    import time

    return _SPENT.get(_provider(model), 0) > time.time()


def _run() -> dict:
    return _RUN.get() or _LAST_RUN


def _current_model() -> str:
    run = _run()
    return run["order"][run["index"]] if run else DEFAULT_MODEL


def _current_client():
    from . import pw_openai

    run, model = _run(), _current_model()
    if pw_openai.is_gpt_model(model):
        # GPT already speaks the generator's request shape: no translation.
        return pw_openai.OpenAIProxyClient(run["token_provider"], model)
    return ProxyClient(run["token_provider"], model)


def budget_spent(exc: BaseException) -> bool:
    """The proxy's "Budget has been exceeded": a quota no retry clears."""
    text = str(exc).lower()
    return "budget" in text and "exceed" in text


def _move_past(model: str) -> bool:
    """Pause ``model``'s provider and move the run to its next model of another
    provider. False when there is none (the error then stands, as before)."""
    import time

    run = _run()
    if not run:
        return False
    _SPENT[_provider(model)] = time.time() + BUDGET_PAUSE_SECONDS
    for index in range(run["index"] + 1, len(run["order"])):
        if _provider(run["order"][index]) != _provider(model):
            run["index"] = index
            return True
    return False


def json_size(request: dict) -> int:
    return len(json.dumps(request).encode("utf-8"))


def encode_pdf(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
