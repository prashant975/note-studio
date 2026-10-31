"""Look a fact up on the web, instead of asking a model to remember it.

The QC passes call Gemini through the PW proxy, and the proxy does not forward
Gemini's ``google_search`` tool. Measured against five facts whose answers
changed after the model's training data, the reply is identical with the tool
field and without it, and wrong both times, in every shape the Gemini REST API
accepts. So the current-affairs misses were never a prompt problem — the
checker had no way to find out.

The proxy also exposes Claude, and Claude's web search is a SERVER tool:
Anthropic runs the search, so only the tool declaration has to travel. That the
proxy does forward. Measured on the same five facts — who holds an office,
which city hosted a meeting, which territory named a cyclone, which body
published a report, a revised threshold table — Claude with web search answered
5 of 5 correctly where the Gemini path answered 0 of 5.

So this module's primary path costs no new key and no new account: it is the
proxy the app already uses, asked through a different model. A direct Gemini
key is kept as an optional fallback for anyone who wants one.

Order of attempts:

  1. Claude via the PW proxy, with web search  (needs the signed-in session)
  2. Gemini direct, with google_search         (needs GEMINI_API_KEY)
  3. give up, and say so

Nothing here can break a run: every failure path returns None and the caller
carries on exactly as it did before this file existed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

__all__ = ["GroundedAnswer", "available", "look_up", "why_unavailable"]

#: (model, web-search tool type) pairs, cheapest first. The 20260209 variant is
#: rejected by Haiku 4.5 with a 400 — it needs Opus 4.6+ or Sonnet 4.6+ — so
#: each model is paired with the variant it actually accepts.
_CLAUDE_ATTEMPTS = (
    ("claude-haiku-4-5", "web_search_20250305"),
    ("claude-sonnet-4-6", "web_search_20260209"),
    ("claude-opus-4-8", "web_search_20260209"),
)
_MAX_SEARCHES = 4
_CLAUDE_MAX_TOKENS = 2048

#: Gemini models that support grounding, for the optional direct fallback.
_GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")
_GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                    "{model}:generateContent?key={key}")
_TIMEOUT = 45


@dataclass
class GroundedAnswer:
    """What the web said, and where it said it."""

    answer: str
    sources: list[str] = field(default_factory=list)
    #: The searches actually run. Empty when the provider does not report them.
    queries: list[str] = field(default_factory=list)
    model: str = ""
    #: True when the provider confirmed it ran a search.
    searched: bool = False

    @property
    def grounded(self) -> bool:
        """True when this answer came from the web rather than from memory.

        An ungrounded answer is NOT evidence: it is the same recollection that
        made the lookup necessary, and callers must discard it.
        """
        return bool(self.searched or self.sources or self.queries)


# --------------------------------------------------------------- Claude path

def _claude_reply(google_token: str, model: str, tool_type: str,
                  question: str, send=None) -> GroundedAnswer | None:
    """Ask Claude, through the proxy, with its server-side web search on."""
    request = {
        "max_tokens": _CLAUDE_MAX_TOKENS,
        "messages": [{"role": "user", "content": question}],
        "tools": [{"type": tool_type, "name": "web_search",
                   "max_uses": _MAX_SEARCHES}],
    }
    if send is None:
        import pw_access

        send = pw_access.claude_generate
    result = send(google_token, model=model, request=request,
                  filename="grounded-lookup", task_id="grounded-lookup")

    body = result.get("result") if isinstance(result.get("result"), dict) else result
    text: list[str] = []
    sources: list[str] = []
    queries: list[str] = []
    searched = False
    for block in body.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            text.append(block.get("text", ""))
        elif kind == "server_tool_use":
            searched = True
            query = (block.get("input") or {}).get("query")
            if query:
                queries.append(query)
        elif kind == "web_search_tool_result":
            searched = True
            # A successful result is a LIST of results; an error is an object.
            for item in block.get("content") or []:
                if isinstance(item, dict) and item.get("url"):
                    sources.append(item["url"])
    answer = " ".join(text).strip()
    if not answer:
        return None
    return GroundedAnswer(answer=answer,
                          sources=list(dict.fromkeys(sources))[:6],
                          queries=list(dict.fromkeys(queries))[:4],
                          model=model, searched=searched)


# --------------------------------------------------------------- Gemini path

def _api_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if key:
        return key
    try:
        import streamlit as st

        for name in ("gemini_api_key", "GEMINI_API_KEY", "google_api_key"):
            value = str(st.secrets.get(name, "") or "").strip()
            if value:
                return value
    except Exception:
        pass
    return ""


def _gemini_post(model: str, key: str, body: dict) -> dict:
    request = urllib.request.Request(
        _GEMINI_ENDPOINT.format(model=model, key=key),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_gemini(data: dict, model: str) -> GroundedAnswer:
    text: list[str] = []
    sources: list[str] = []
    queries: list[str] = []
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if isinstance(part.get("text"), str):
                text.append(part["text"])
        meta = (candidate.get("groundingMetadata")
                or candidate.get("grounding_metadata") or {})
        queries.extend(meta.get("webSearchQueries")
                       or meta.get("web_search_queries") or [])
        for chunk in (meta.get("groundingChunks")
                      or meta.get("grounding_chunks") or []):
            uri = (chunk.get("web") or {}).get("uri")
            if uri:
                sources.append(uri)
    return GroundedAnswer(answer=" ".join(text).strip(),
                          sources=list(dict.fromkeys(sources))[:6],
                          queries=list(dict.fromkeys(queries))[:4],
                          model=model, searched=bool(sources or queries))


# ------------------------------------------------------------------ the API

def available(google_token: str = "") -> bool:
    """True when some grounded path can run."""
    return bool(str(google_token or "").strip() or _api_key())


def why_unavailable(google_token: str = "") -> str:
    """One line a user can act on, or "" when a lookup is possible."""
    if available(google_token):
        return ""
    return ("Grounded fact-checking did not run: no signed-in proxy session and "
            "no Gemini API key, so nothing could look the fact up. Sign in to the "
            "app (or run the CLI with --session) and current-affairs claims are "
            "checked against the web through Claude on the PW proxy.")


def look_up(question: str, *, google_token: str = "",
            claude_send=None, gemini_post=None) -> GroundedAnswer | None:
    """Answer one factual question from the web, or None if that is impossible.

    ``claude_send`` and ``gemini_post`` are the two transports, injectable for
    tests. Never raises: a lookup that cannot happen must leave the check that
    asked for it exactly as it was.
    """
    question = " ".join(str(question or "").split())
    if not question:
        return None

    token = str(google_token or "").strip()
    if token:
        for model, tool_type in _CLAUDE_ATTEMPTS:
            try:
                answer = _claude_reply(token, model, tool_type, question,
                                       send=claude_send)
            except Exception:
                continue      # this model or tool variant is not served; next
            if answer is not None and answer.grounded:
                return answer

    key = _api_key()
    if key:
        body = {
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 800},
        }
        send = gemini_post or _gemini_post
        for model in _GEMINI_MODELS:
            try:
                data = send(model, key, body)
            except urllib.error.HTTPError:
                continue
            except Exception:
                return None
            answer = _read_gemini(data, model)
            if answer.answer:
                return answer
    return None
