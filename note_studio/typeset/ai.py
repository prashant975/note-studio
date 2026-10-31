"""The PW AI calls of the typeset notes, through the hand-drawn notes engine's own
pw_access (the same proxy, Google account and usage ledger): JSON answers with a
schema, pictures, and what each call cost.

A call is tried twice on its model when PW AI is busy, then once on the next
model of the stage; a schema Gemini refuses (400) is retried with the JSON type
only. A spent budget or a refused account stops at once: no retry clears those."""
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
        if _status(exc) in SIGN_IN_WORDS:
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
