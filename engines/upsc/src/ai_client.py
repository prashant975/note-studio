from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

import pw_access

from .config import (
    DEFAULT_AUDIO_FALLBACK_MODEL,
    DEFAULT_AUDIO_MODEL,
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
)
from .utils import image_mime_type


class GeminiError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.details = details


@dataclass
class GeminiResponse:
    text: str
    provider: str
    model: str
    raw: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    #: Why the model stopped. "MAX_TOKENS" means the reply is CUT OFF, not
    #: finished — the text still arrives and still looks like prose, which is
    #: why this has to be read rather than inferred from the content.
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason.upper() in _TRUNCATING_REASONS


def _usage_from_mapping(data: dict[str, Any] | None) -> dict[str, int]:
    if not data:
        return {}
    usage = data.get("usageMetadata") or data.get("usage_metadata") or {}
    if not isinstance(usage, dict):
        return {}
    key_map = {
        "promptTokenCount": "tokens_input",
        "candidatesTokenCount": "tokens_output",
        "totalTokenCount": "tokens_total",
        "thoughtsTokenCount": "tokens_thinking",
        "prompt_token_count": "tokens_input",
        "candidates_token_count": "tokens_output",
        "total_token_count": "tokens_total",
        "thoughts_token_count": "tokens_thinking",
    }
    out: dict[str, int] = {}
    for src, dest in key_map.items():
        value = usage.get(src)
        if isinstance(value, int):
            out[dest] = value
    return out


def _usage_from_proxy(resp: dict[str, Any] | None) -> dict[str, int]:
    """Fall back to the proxy's own usage summary ({"tokens_in", "tokens_out"})
    when the raw Gemini response didn't carry usageMetadata."""
    usage = (resp or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    if isinstance(usage.get("tokens_in"), int):
        out["tokens_input"] = usage["tokens_in"]
    if isinstance(usage.get("tokens_out"), int):
        out["tokens_output"] = usage["tokens_out"]
    return out


#: Finish reasons that mean the answer is incomplete. SAFETY and RECITATION
#: are included because they also stop the model mid-answer — the caller's
#: document is short either way, and silence about it is the problem.
_TRUNCATING_REASONS = frozenset({"MAX_TOKENS", "SAFETY", "RECITATION", "OTHER"})


def _finish_reason(data: dict[str, Any]) -> str:
    """Why the model stopped, or "" if it did not say."""
    for cand in data.get("candidates") or []:
        reason = cand.get("finishReason") or cand.get("finish_reason")
        if reason:
            return str(reason)
    return ""


def _extract_text_from_response(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    parts: list[str] = []
    for cand in candidates:
        content = cand.get("content") or {}
        for p in content.get("parts") or []:
            if "text" in p:
                parts.append(p["text"])
    if parts:
        return "\n".join(parts).strip()
    if "output" in data:
        out = data["output"]
        if isinstance(out, str):
            return out.strip()
        if isinstance(out, list):
            return "\n".join(str(x) for x in out).strip()
    return ""


# Pages are rendered as 2x PNGs (often 1-3 MB each), so we downscale and
# re-encode them to JPEG before base64 to keep request bodies sane.
#
# On the size ceiling: pw-app-kit v2 uploads any request over ~3.5 MB through
# the proxy's blob route, so a large body is carried rather than rejected — the
# old hard Vercel ~4.5 MB wall no longer applies. This budget is kept as a
# belt-and-braces cap (it also keeps latency and cost down) but is deliberately
# set ABOVE the kit's blob threshold, so trimming only ever kicks in for bodies
# the blob route would find genuinely unwieldy, not for ordinary multi-image
# calls that v2 can now carry whole.
_PROXY_BODY_BUDGET = 12_000_000
_IMAGE_MAX_DIM = 1600                    # longest edge sent to Gemini
_IMAGE_JPEG_QUALITY = 80

# Transient proxy/provider failures worth retrying rather than surfacing.
# pw-app-kit v1 called Vertex directly and rode out capacity chokes with its own
# ladder (immediate, +8s, other regions, +30s). v2 routes everything through the
# proxy and has no retry of its own, so without this a single 429/503 blip would
# abort a whole notes run — or, worse, silently switch the run to the fallback
# model half way through a document. Regions are the proxy's business now, so
# only the backoff survives.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_RETRY_WAITS_SECONDS = (0, 8, 30)        # before attempt 1, 2, 3
_PROXY_STATUS_RE = re.compile(r"proxy error (\d{3})")


def _transient_proxy_status(err: Exception) -> int | None:
    """Status code from a PWAccessError when it is worth retrying, else None.

    The kit reports failures as `PWAccessError("gemini proxy error 503: ...")`,
    so the code has to be read back out of the message — there is no structured
    field to consult.
    """
    match = _PROXY_STATUS_RE.search(str(err))
    if not match:
        return None
    status = int(match.group(1))
    return status if status in _TRANSIENT_STATUSES else None


def _encode_image_inline(image_path: Path) -> dict[str, Any] | None:
    """Return an inline_data part for one image, downscaled and JPEG-compressed
    so the request stays small. Falls back to the raw bytes if PIL is missing or
    the image can't be processed."""
    p = Path(image_path)
    if not p.exists():
        return None
    raw = p.read_bytes()
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((_IMAGE_MAX_DIM, _IMAGE_MAX_DIM))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
            data = buf.getvalue()
        return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode("ascii")}}
    except Exception:
        return {"inline_data": {"mime_type": image_mime_type(p), "data": base64.b64encode(raw).decode("ascii")}}


# Gemini accepts these audio containers inline. Opus lives in an Ogg container.
_AUDIO_MIME = {
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mp3", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".aiff": "audio/aiff",
}


def _encode_audio_inline(audio_path: Path) -> dict[str, Any] | None:
    """Return an inline_data part for one audio file (already compressed by the caller).

    Audio is NOT re-encoded here: the caller (audio_transcribe) controls the
    bitrate/chunk length, since those are what keep the request under the
    proxy's body limit.
    """
    p = Path(audio_path)
    if not p.exists():
        return None
    mime = _AUDIO_MIME.get(p.suffix.lower(), "audio/ogg")
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(p.read_bytes()).decode("ascii")}}


# ---------------------------------------------------------------------------
# GPT through the proxy.
#
# Every caller builds a Gemini-shaped body and reads a Gemini-shaped reply
# (candidates / finishReason / usageMetadata). Rather than teach each of them a
# second format, a GPT model gets the body translated on the way out and the
# reply translated back, so retries, fallback, truncation warnings and usage
# rows all keep working unchanged whichever provider served the call.
# ---------------------------------------------------------------------------
def is_gpt_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("gpt")


#: GPT-5 models spend part of the completion budget on hidden reasoning. The
#: Gemini-sized limits callers pass would leave long notes cut off, so this much
#: is added on top.
_GPT_REASONING_HEADROOM = 8000

#: OpenAI finish reasons mapped onto the Gemini ones _TRUNCATING_REASONS knows.
_GPT_FINISH_REASONS = {"length": "MAX_TOKENS", "content_filter": "SAFETY", "stop": "STOP", "tool_calls": "STOP"}


def _gemini_body_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate a Gemini generateContent body into a chat-completions body."""
    messages: list[dict[str, Any]] = []
    for content in body.get("contents") or []:
        parts: list[dict[str, Any]] = []
        for part in content.get("parts") or []:
            if "text" in part:
                parts.append({"type": "text", "text": part["text"]})
                continue
            inline = part.get("inline_data") or part.get("inlineData")
            if not inline:
                continue
            mime = inline.get("mime_type") or inline.get("mimeType") or ""
            if not mime.startswith("image/"):
                raise GeminiError(
                    f"GPT models cannot take {mime or 'this'} input through the proxy.",
                    provider="pw_proxy",
                )
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{inline['data']}"}})
        role = "assistant" if content.get("role") == "model" else "user"
        if len(parts) == 1 and parts[0]["type"] == "text":
            messages.append({"role": role, "content": parts[0]["text"]})
        else:
            messages.append({"role": role, "content": parts})

    config = body.get("generationConfig") or {}
    request: dict[str, Any] = {"messages": messages}
    # temperature is left out on purpose: GPT-5 models only accept the default
    # and reject any other value with a 400.
    if isinstance(config.get("maxOutputTokens"), int):
        request["max_completion_tokens"] = config["maxOutputTokens"] + _GPT_REASONING_HEADROOM
    return request


def _openai_text(result: dict[str, Any]) -> tuple[str, str]:
    """(text, finish reason) from a chat-completions or Responses API reply."""
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] or {}
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        return str(content or ""), str(choice.get("finish_reason") or "")
    if isinstance(result.get("output_text"), str):
        text = result["output_text"]
    else:
        text = "".join(
            str(c.get("text", ""))
            for item in result.get("output") or [] if isinstance(item, dict)
            for c in item.get("content") or [] if isinstance(c, dict)
        )
    reason = "length" if result.get("status") == "incomplete" else ("stop" if text else "")
    return text, reason


def _openai_reply_to_gemini(resp: dict[str, Any]) -> dict[str, Any]:
    """Wrap a proxy GPT reply in the Gemini shape the rest of the client reads."""
    result = resp.get("result") if isinstance(resp.get("result"), dict) else resp
    text, reason = _openai_text(result)
    if not text and isinstance(resp.get("text"), str):
        text = resp["text"]
    usage = result.get("usage") or {}
    usage_metadata: dict[str, int] = {}
    for src, dest in (("prompt_tokens", "promptTokenCount"), ("input_tokens", "promptTokenCount"),
                      ("completion_tokens", "candidatesTokenCount"), ("output_tokens", "candidatesTokenCount"),
                      ("total_tokens", "totalTokenCount")):
        if isinstance(usage.get(src), int):
            usage_metadata[dest] = usage[src]
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        usage_metadata["thoughtsTokenCount"] = details["reasoning_tokens"]
    candidate: dict[str, Any] = {"content": {"parts": [{"text": text}] if text else []}}
    if reason:
        candidate["finishReason"] = _GPT_FINISH_REASONS.get(reason, reason.upper())
    gemini: dict[str, Any] = {"candidates": [candidate], "_gpt_raw": result}
    if usage_metadata:
        gemini["usageMetadata"] = usage_metadata
    return {"result": gemini, "usage": resp.get("usage") or {}}


def _build_rest_parts(prompt: str, image_paths: list[Path] | None = None, max_images: int = 8) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for image_path in (image_paths or [])[:max_images]:
        if not image_path:
            continue
        part = _encode_image_inline(Path(image_path))
        if part:
            parts.append(part)
    return parts


def _fit_body_to_budget(body: dict[str, Any], budget: int = _PROXY_BODY_BUDGET) -> int:
    """Safety net: if the serialized request still exceeds the proxy's size
    budget, drop trailing inline images (never the prompt text at index 0) until
    it fits. Returns how many images were dropped."""
    try:
        parts = body["contents"][0]["parts"]
    except (KeyError, IndexError, TypeError):
        return 0
    dropped = 0
    while len(json.dumps(body).encode("utf-8")) > budget:
        for i in range(len(parts) - 1, 0, -1):
            if "inline_data" in parts[i]:
                parts.pop(i)
                dropped += 1
                break
        else:
            break  # nothing left to drop but the prompt
    return dropped


class GeminiClient:
    """Gemini access through the shared PW proxy.

    The app ships no Gemini API key; the proxy holds it. Every call carries the
    signed-in user's Google token, which the proxy verifies (against the app
    whitelist) before calling Gemini with its own key and logging the usage.

    Usage logging is the proxy's job (pw-app-kit v2): every provider call it
    serves writes its own raw row. One run therefore passes a single `task_id`
    to every call so those rows can be grouped afterwards by
    task_id + app + email + model. The kit's older UsageSession/flush() path is
    deliberately not used — in v2 flush() is a no-op, so relying on it would
    silently log nothing.

    `filename`/`input_unit`/`count` describe the source file and ride along on
    each call so the raw rows stay readable in the ledger export.
    """

    def __init__(
        self,
        google_token: str,
        model: str = DEFAULT_MODEL,
        *,
        fallback_model: str = DEFAULT_FALLBACK_MODEL,
        task_id: str = "",
        filename: str = "",
        input_unit: str = "",
        count: int | None = None,
        timeout: int = 300,
    ):
        self.google_token = (google_token or "").strip()
        self.model = model.strip() or DEFAULT_MODEL
        fallback = (fallback_model or "").strip()
        self.fallback_model = fallback if fallback and fallback != self.model else ""
        # One id for the whole run. Generated here only as a safety net: the
        # pipeline creates it at task start so every call in the run shares it.
        self.task_id = (task_id or "").strip() or pw_access.new_task_id("upsc-notes")
        self.filename = filename
        self.input_unit = input_unit
        self.count = count
        self.timeout = timeout
        # Notes appended when the fallback model actually served a call, so the
        # pipeline can surface it in the run's warnings. Written from worker
        # threads (chunked generation runs concurrently), hence the lock.
        self.fallback_notices: list[str] = []
        self._notice_lock = threading.Lock()
        if not self.google_token:
            raise GeminiError(
                "No Google token available. Sign in with your @pw.live Google "
                "account before generating notes (the PW proxy needs it)."
            )

    def _note_fallback(self, message: str) -> None:
        with self._notice_lock:
            if message not in self.fallback_notices:
                self.fallback_notices.append(message)

    def _call_proxy(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        """One proxy call on the provider the model name belongs to.

        A GPT reply comes back already translated into the Gemini shape.
        """
        common = dict(filename=self.filename, input_unit=self.input_unit, count=self.count,
                      task_id=self.task_id)
        if is_gpt_model(model):
            resp = pw_access.gpt_generate(self.google_token, model=model,
                                          request=_gemini_body_to_openai(body), **common)
            return _openai_reply_to_gemini(resp)
        return pw_access.gemini_generate(self.google_token, model=model, request=body, **common)

    def _generate_with_retry(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        """One model, retried through the backoff ladder on transient failures.

        Only capacity/gateway errors are retried; anything else (bad request,
        auth, a model that does not exist) raises immediately so the caller can
        fall back to the other model or surface the real problem. Raises the
        last PWAccessError if every attempt is choked.
        """
        return self._with_retry(model, lambda: self._call_proxy(model, body))

    def _with_retry(self, model: str, call) -> Any:
        last_error: Exception | None = None
        for attempt, wait in enumerate(_RETRY_WAITS_SECONDS, start=1):
            if wait:
                time.sleep(wait)
            try:
                return call()
            except pw_access.PWAccessError as e:
                status = _transient_proxy_status(e)
                if status is None:
                    raise
                last_error = e
                if attempt < len(_RETRY_WAITS_SECONDS):
                    self._note_fallback(
                        f"Model '{model}' was busy (HTTP {status}); retrying "
                        f"(attempt {attempt + 1} of {len(_RETRY_WAITS_SECONDS)})."
                    )
            except requests.RequestException as e:
                # The ladder used to defend only against "busy" (429/503 from
                # the proxy) and not against "quiet": a client-side timeout or a
                # dropped connection raised straight through it, so one network
                # blip at minute 40 killed the whole run with no retry and no
                # model fallback. Same ladder, same budget — a request that hung
                # may have completed server-side, but a possibly-duplicated call
                # costs less than a lost lecture.
                last_error = e
                if attempt < len(_RETRY_WAITS_SECONDS):
                    self._note_fallback(
                        f"Model '{model}': network problem ({type(e).__name__}); "
                        f"retrying (attempt {attempt + 1} of {len(_RETRY_WAITS_SECONDS)})."
                    )
        raise last_error  # type: ignore[misc]

    def _generate_content(self, model: str, body: dict[str, Any], *, fallback: str | None = None) -> dict[str, Any]:
        # The text model falls back to self.fallback_model; other callers
        # (audio, image) name their own fallback or pass none.
        if fallback is None:
            fallback = self.fallback_model if model == self.model else ""
        candidates = [model]
        if fallback and fallback != model:
            candidates.append(fallback)

        for index, candidate in enumerate(candidates):
            try:
                resp = self._generate_with_retry(candidate, body)
            except (pw_access.PWAccessError, requests.RequestException) as e:
                # Network exceptions get the same fallback the proxy errors do:
                # from the run's point of view "the model never answered" is the
                # same failure whichever layer swallowed the reply.
                if index + 1 < len(candidates):
                    self._note_fallback(
                        f"Model '{candidate}' was unavailable, so '{candidates[index + 1]}' "
                        f"was used instead. Reason: {e}"
                    )
                    continue
                raise GeminiError(str(e), provider="pw_proxy") from e
            result = resp.get("result")
            if not isinstance(result, dict):
                raise GeminiError(
                    f"Proxy returned no Gemini result. Response: {json.dumps(resp)[:600]}",
                    provider="pw_proxy",
                    details=resp,
                )
            # Stash the proxy-level usage so callers can fall back to it, and the
            # model that actually served the call so usage/metadata name it.
            result.setdefault("_pw_usage", resp.get("usage") or {})
            result.setdefault("_pw_model", candidate)
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    def generate(self, prompt: str, image_paths: list[Path] | None = None, *, max_output_tokens: int = 12000, temperature: float = 0.15, max_images: int = 8) -> GeminiResponse:
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, image_paths, max_images=max_images)}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        _fit_body_to_budget(body)
        data = self._generate_content(self.model, body)
        text = _extract_text_from_response(data)
        if not text:
            raise GeminiError(
                f"{data.get('_pw_model') or self.model} (via proxy) returned no text. "
                f"Raw response: {json.dumps(data)[:1000]}",
                provider="pw_proxy",
                details=data,
            )
        usage = _usage_from_mapping(data) or _usage_from_proxy({"usage": data.get("_pw_usage")})
        reason = _finish_reason(data)
        if reason.upper() in _TRUNCATING_REASONS:
            # Recorded as a fallback notice so it reaches the run's warnings by
            # the path that already exists, rather than needing every caller to
            # remember to check. Callers that CAN act on it read .truncated.
            self._note_fallback(
                f"The model stopped early (finishReason={reason}) on a call with a "
                f"{max_output_tokens:,}-token limit, so that reply is cut off rather "
                f"than finished. Anything it had not written yet is missing.")
        return GeminiResponse(text=text, provider="pw_proxy",
                              model=data.get("_pw_model") or self.model, raw=data,
                              usage=usage, finish_reason=reason)

    def generate_with_audio(self, prompt: str, audio_path: Path, *, max_output_tokens: int = 32000, temperature: float = 0.0) -> GeminiResponse:
        """Send one (already compressed) audio chunk to Gemini and return its text.

        Used to transcribe lecture audio when a video has no captions. Fails
        loudly if the chunk would exceed the proxy's body limit — silently
        dropping audio (as _fit_body_to_budget does for images) would lose
        minutes of the lecture without anyone noticing.

        The proxy has no GPT speech route, so when the text model is GPT the
        audio goes to the Gemini audio model (AUDIO_MODEL_NAME) instead.
        """
        if is_gpt_model(self.model):
            model, fallback = DEFAULT_AUDIO_MODEL, DEFAULT_AUDIO_FALLBACK_MODEL
        else:
            model, fallback = self.model, self.fallback_model
        part = _encode_audio_inline(Path(audio_path))
        if part is None:
            raise GeminiError(f"Audio chunk not found: {audio_path}", provider="pw_proxy")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}, part]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        size = len(json.dumps(body).encode("utf-8"))
        if size > _PROXY_BODY_BUDGET:
            # The kit blob-uploads big bodies, so this is a sanity ceiling, not
            # a protocol limit: a chunk this large means the chunker misbehaved.
            raise GeminiError(
                f"Audio chunk is implausibly large ({size / 1e6:.1f} MB > "
                f"{_PROXY_BODY_BUDGET / 1e6:.1f} MB) — the audio chunker likely "
                "produced a bad chunk. Use a shorter chunk or a lower bitrate.",
                provider="pw_proxy",
            )
        data = self._generate_content(model, body, fallback=fallback)
        text = _extract_text_from_response(data)
        if not text:
            raise GeminiError(
                f"{data.get('_pw_model') or model} (via proxy) returned no transcript for {Path(audio_path).name}.",
                provider="pw_proxy",
                details=data,
            )
        usage = _usage_from_mapping(data) or _usage_from_proxy({"usage": data.get("_pw_usage")})
        return GeminiResponse(text=text, provider="pw_proxy", model=data.get("_pw_model") or model, raw=data, usage=usage)

    def test_connection(self) -> GeminiResponse:
        return self.generate("Reply with exactly: OK", image_paths=[], max_output_tokens=32, temperature=0.0, max_images=0)

    def generate_image(self, prompt: str, image_path: Path, *, image_model: str = DEFAULT_IMAGE_MODEL) -> bytes:
        """Image-to-image generation through the proxy: send a prompt + source
        image, return PNG bytes. Raises GeminiError if no image was produced."""
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, [image_path], max_images=1)}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        _fit_body_to_budget(body)
        data = self._generate_content(image_model, body, fallback="")
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise GeminiError(
            f"Image generation via proxy returned no image. Raw: {json.dumps(data)[:600]}",
            provider="pw_proxy",
        )


#: Sample rows for the Error Report, appended to every mock response so a mock
#: run exercises the marker split, the JSON parse and the XLSX writer for real
#: instead of falling through to the "no report" placeholder.
MOCK_ERROR_REPORT = """

# ERROR_REPORT_START
[
  {
    "original_statement": "GDP is the market value of all final goods and services produced within domestic territory in a financial year by a normal resident.",
    "error_type": "Conceptual",
    "severity": "High",
    "error_concern": "GDP is not limited to production by normal residents.",
    "correct_information_correction": "GDP covers final goods and services produced within the domestic territory irrespective of whether the producers are residents or non-residents. Residence is relevant for GNP/GNI, not GDP.",
    "source_urls": ["https://mospi.gov.in/"]
  },
  {
    "original_statement": "The installed-capacity figure quoted in the lecture.",
    "error_type": "Numerical/outdated",
    "severity": "Medium",
    "error_concern": "The figure predates the latest CEA release and is no longer current.",
    "correct_information_correction": "Use the latest official figure available at finalisation.",
    "source_urls": []
  }
]
# ERROR_REPORT_END
"""


def generate_mock_notes(subject: str, mode: str, language: str, slide_count: int) -> str:
    if language == "hi":
        return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• यह MOCK output है क्योंकि API call नहीं की गई या mock mode चालू है।\n• कुल {slide_count} slides/pages पढ़े गए।\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".){MOCK_ERROR_REPORT}"""
    return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• This is MOCK output because API call was not used or mock mode is enabled.\n• {slide_count} slides/pages were read.\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".){MOCK_ERROR_REPORT}"""


def generate_mock_dcn_notes(subject: str, slide_count: int) -> str:
    """Marker-format sample for the PW OnlyIAS DCN writer.

    Deliberately exercises every construct the writer parses — all three heading
    levels, the three bullet depths, bold runs, a table, a flowchart, a
    separator, an embedded correction and a verification marker, plus the
    subscript/symbol glyphs that must survive the handwritten font — so a mock
    run is a real end-to-end test of the layout rather than a stub.
    """
    area = (subject or "General Studies").replace("_", " ").title()
    return f"""#LECTURE_TITLE: L01_DCN01_Introduction to {area}:

#H1: Core Concepts and Definitions

- **Ecology** is defined as the scientific study of the relationship of living organisms with each other and with their environment.
- The **environment** is the total of living (biotic) and non-living (abiotic) components and their interactions.
  - All organisms depend on each other and on the environment for food and energy, the Sun being the ultimate source of energy.
    - The environment is classified as **external** (a pond or river for a fish) and **internal** (homeostasis within the body).
- This MOCK output was produced without an AI call; {slide_count} slides/pages were read.

#H2: Measurable Indicators

- Concentrations are reported in **mg/m³**, noise in **dB(A)**, and warming relative to the **1850–1900** baseline in **°C**.
- Key gases include **CO₂**, **CH₄**, **N₂O** and **H₂O**, and thresholds are written as ≥, ≤ or ≠ where relevant.

#TABLE_START
Indicator | Status
Total installed electricity capacity | 513.73 GW
Non-fossil capacity | 266.79 GW
#TABLE_END

#H3: Process Chain

#FLOW_START
Sun
↓
Producers
↓
Consumers
↓
Decomposers
#FLOW_END

#SEPARATOR

#H1: Corrections and Cautions

- Important current update: the older installed-capacity figure quoted in the lecture has been superseded, so the earlier figure should not be used.
- Important distinction: **non-fossil** capacity is not the same as **renewable** capacity, because it also counts nuclear and large hydro.
- UPSC caution: a Least Concern IUCN status does not by itself imply lower legal protection under the **Wildlife (Protection) Act, 1972**.
- [Needs Verification: current installed-capacity figure requires checking against the latest CEA release]
{MOCK_ERROR_REPORT}"""
