"""The PW AI calls of the typeset notes, through the hand-drawn notes engine's own
pw_access (the same proxy, Google account and usage ledger): JSON answers with a
schema, pictures, and what each call cost.

A call is tried twice on its model when PW AI is busy, then once on the next
model of the stage; a schema Gemini refuses (400) is retried with the JSON type
only. A spent budget or a refused account stops at once: no retry clears those.

The audio notes speak through the same proxy: speech() sends a native Gemini
speech request (two voices in one call), speech_line() one line in one voice."""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

_STATUS = re.compile(r"proxy error (\d{3})")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
RETRY_PAUSE = 2.0                       # seconds, times the attempt number (tests set 0)
BUSY = (408, 429, 500, 502, 503, 504)   # statuses worth asking again
SIGN_IN_WORDS = {401: "The Google sign-in has expired. Sign in again in Settings, then press Try again.",
                 403: "This Google account is not on the PW list for this app. Ask the owner to add it."}
BUDGET_WORDS = "The PW AI budget for this model is spent. Ask the administrator to renew it, then press Try again."


class AIError(RuntimeError):
    """A call that did not give an answer; ``code`` is sign_in, budget or failed."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


def _pw_access():
    import pw_access          # engines/shortnotes, on the path while NoteCraft runs

    return pw_access


def _status(exc: BaseException) -> int:
    found = _STATUS.search(str(exc))
    return int(found.group(1)) if found else 0


def _budget(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "budget" in text and "exceed" in text


def _model_refused(exc: BaseException) -> bool:
    """A 403 that refuses the model for the app's key, not the person's account (PW AI's
    native route answers so for the speech models: "key_model_access_denied", 27 Nov 2026)."""
    text = str(exc).lower()
    return "key_model_access_denied" in text or "not available for this api key" in text


def _body(payload: dict) -> dict:
    """The Gemini answer, whether the proxy returns it bare or wrapped."""
    if not isinstance(payload, dict) or "candidates" in payload:
        return payload if isinstance(payload, dict) else {}
    for key in ("result", "response", "data", "gemini", "body"):
        if isinstance(payload.get(key), dict) and "candidates" in payload[key]:
            return payload[key]
    return payload


def answer_text(payload: dict) -> tuple[str, str]:
    """(text, finish reason) of a Gemini answer."""
    candidates = _body(payload).get("candidates") or []
    if not candidates:
        return "", ""
    first = candidates[0] or {}
    parts = (first.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str)), str(first.get("finishReason") or "")


def audio_of(payload: dict) -> tuple[bytes, str]:
    """(audio bytes, mime type) of a speech answer: a Gemini answer's inline data, or the
    proxy's own {"result": {"audio_base64", "content_type"}} shape; (b"", "") when none came."""
    if not isinstance(payload, dict):
        return b"", ""
    for candidate in _body(payload).get("candidates") or []:
        for part in ((candidate or {}).get("content") or {}).get("parts") or []:
            blob = (part or {}).get("inlineData") or (part or {}).get("inline_data") or {}
            if isinstance(blob.get("data"), str) and blob["data"]:
                return base64.b64decode(blob["data"]), str(blob.get("mimeType") or blob.get("mime_type") or "")
    for found in (payload.get("result"), payload):
        if not isinstance(found, dict):
            continue
        for key in ("audio_base64", "audioContent", "audio"):
            if isinstance(found.get(key), str) and found[key]:
                mime = found.get("content_type") or found.get("mime_type") or found.get("mimeType") or ""
                return base64.b64decode(found[key]), str(mime)
    return b"", ""


def pdf_part(data: bytes) -> dict:
    return {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(data).decode("ascii")}}


def image_part(data: bytes, mime: str = "image/jpeg") -> dict:
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}}


@dataclass
class Session:
    """One lecture's calls: who pays (the token provider), the ledger's task, the log."""
    token_provider: Callable[[], str]
    task_id: str
    filename: str = ""
    pages: int | None = None
    log: Callable[[str], None] = lambda line: None
    cost: float = 0.0
    calls: list = field(default_factory=list)          # [kind, model, INR]

    def _paid(self, payload: dict, kind: str, model: str) -> None:
        value = payload.get("cost_inr") if isinstance(payload, dict) else None
        cost = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0
        self.cost = round(self.cost + cost, 4)
        self.calls.append([kind, model, cost])
        self.log(f"lecture: {kind} call - INR {cost:.2f}")

    def _refused(self, exc: BaseException) -> None:
        """Stop the whole lecture on a spent budget or a refused account."""
        if _budget(exc):
            raise AIError(BUDGET_WORDS, "budget") from None
        if _status(exc) in SIGN_IN_WORDS and not _model_refused(exc):
            raise AIError(SIGN_IN_WORDS[_status(exc)], "sign_in") from None

    def json(self, kind: str, models: list[str], system: str, parts: list[dict], schema: dict, *,
             max_tokens: int = 65000) -> dict:
        """A JSON answer for this request, from the first model that gives one."""
        pw = _pw_access()
        problem = "PW AI did not answer"
        chosen = [m for m in models if m] or ["gemini-3.7-flash"]
        for model in chosen:
            with_schema = True
            for attempt in range(3):
                config = {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}
                if with_schema:
                    config["responseSchema"] = schema
                request = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config,
                           "systemInstruction": {"parts": [{"text": system}]}}
                self.log(f"lecture: {kind} call started")
                try:
                    payload = pw.gemini_generate(self.token_provider, model=model, request=request,
                                                 filename=self.filename, input_unit="No. of pages", count=self.pages,
                                                 task_id=self.task_id)
                except Exception as exc:                  # noqa: BLE001 - every kind of failure is sorted here
                    self._refused(exc)
                    status = _status(exc)
                    problem = f"PW AI failed (status {status or type(exc).__name__})"
                    if status == 400 and with_schema:
                        with_schema = False
                        continue
                    if status and status not in (408, 429, 500, 502, 503, 504):
                        break
                    time.sleep(RETRY_PAUSE * (attempt + 1))
                    continue
                self._paid(payload, kind, model)
                text, finish = answer_text(payload)
                if finish == "MAX_TOKENS":
                    problem = "PW AI's answer was cut off"
                    continue
                try:
                    found = json.loads(_FENCE.sub("", text.strip()))
                except ValueError:
                    problem = "PW AI's answer was cut off" if finish == "MAX_TOKENS" else "PW AI's answer was not readable"
                    continue
                if isinstance(found, dict):
                    return found
                problem = "PW AI's answer was not readable"
            if model != chosen[-1]:
                self.log(f"lecture: {kind} call failed on {model}; {problem}")
        raise AIError(problem)

    def image(self, models: list[str], prompt: str) -> bytes:
        """One picture from the image route (gemini_image), or AIError."""
        pw = _pw_access()
        problem = "no picture came back"
        for model in [m for m in models if m]:
            for attempt in range(3):
                self.log("lecture: figure call started")
                try:
                    payload = pw.gemini_image(self.token_provider, prompt=prompt, model=model,
                                              filename=self.filename, task_id=self.task_id)
                except Exception as exc:                  # noqa: BLE001
                    self._refused(exc)
                    problem = f"the picture route failed (status {_status(exc) or type(exc).__name__})"
                    time.sleep(RETRY_PAUSE * (attempt + 1))
                    continue
                self._paid(payload, "figure", model)
                result = (payload or {}).get("result") or {}
                try:
                    data = base64.b64decode(result.get("image_base64") or "", validate=False)
                except ValueError:
                    data = b""
                if data:
                    return data
                problem = "no picture came back"
        raise AIError(problem)

    def _spoken(self, kind: str, models: list[str], call: Callable[[object, str], dict]) -> tuple[bytes, str]:
        """(audio, mime type) from the first model that speaks. AIError code "unsupported"
        when the last model refused the request itself (a 4xx other than busy)."""
        pw = _pw_access()
        problem, refused = "PW AI sent no audio", False
        for model in [m for m in models if m]:
            for attempt in range(3):
                self.log(f"lecture: {kind} call started")
                try:
                    payload = call(pw, model)
                except Exception as exc:                  # noqa: BLE001 - sorted below
                    self._refused(exc)
                    status = _status(exc)
                    problem = f"PW AI failed (status {status or type(exc).__name__})"
                    refused = 400 <= status < 500 and status not in BUSY
                    if status and status not in BUSY:
                        break
                    time.sleep(RETRY_PAUSE * (attempt + 1))
                    continue
                self._paid(payload, kind, model)
                try:
                    data, mime = audio_of(payload)
                except ValueError:
                    data, mime = b"", ""
                if data:
                    return data, mime
                problem, refused = "PW AI sent no audio", False
        raise AIError(problem, "unsupported" if refused else "failed")

    def speech(self, kind: str, models: list[str], request: dict) -> tuple[bytes, str]:
        """A native Gemini speech request (generationConfig.speechConfig, e.g. two voices)."""
        return self._spoken(kind, models, lambda pw, model: pw.gemini_native(
            self.token_provider, model=model, request=request, filename=self.filename,
            input_unit="No. of pages", count=self.pages, task_id=self.task_id))

    def speech_line(self, kind: str, models: list[str], text: str, voice: str) -> tuple[bytes, str]:
        """One line in one voice, through the proxy's own speech route."""
        return self._spoken(kind, models, lambda pw, model: pw.gemini_tts(
            self.token_provider, text=text, voice=voice, model=model, filename=self.filename,
            count=self.pages, task_id=self.task_id))
