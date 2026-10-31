"""Per-run switches the Sheets automation sets around its model calls.

Everything here is a context variable, so a switch follows the work into the
worker threads that copy the caller's context (both thread pools in the
pipeline do) and never leaks into the web app or another request.

- ``lean_requests()``: the automation's smaller prompt and response schema.
  It generates concise theory notes only, renders no model-drawn visuals and
  builds its own mind map, so the parts of the prompt and schema that serve
  those features are dead weight sent on every call.
- ``inline_source_check(False)``: ``generate_short_notes`` normally reviews
  notes that still contain question text. The automation reviews every page
  group itself, so that inner review only duplicated the outer one, unlogged.
- ``call_purpose()`` / ``call_label()``: names for the per-call cost log.
- ``exam_category()`` / ``exam_subject()``: NEET-JEE / Foundation and the
  lecture's subject; pick the format rules in ``notesbot/format_rules``
  (``notesbot.categories``).
- ``sanskrit_style_as()`` / ``sanskrit_hindi_notes()``: a Sanskrit lecture
  becomes Hindi notes that keep every verse in Sanskrit (the default), or
  strict Sanskrit-only notes (``notesbot.sanskrit``).
- ``fixed_signature()``: the run's signature was typed in the Studio, so the
  teacher named on the slides does not replace it
  (``sheets_bridge.mapping.choose_signature``).
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

_LEAN: contextvars.ContextVar[bool] = contextvars.ContextVar("notesbot_lean_requests", default=False)
_INLINE_CHECK: contextvars.ContextVar[bool] = contextvars.ContextVar("notesbot_inline_source_check", default=True)
_PURPOSE: contextvars.ContextVar[str] = contextvars.ContextVar("notesbot_call_purpose", default="")
_LABEL: contextvars.ContextVar[str] = contextvars.ContextVar("notesbot_call_label", default="")
_CATEGORY: contextvars.ContextVar[str] = contextvars.ContextVar("notesbot_exam_category", default="")
_SUBJECT: contextvars.ContextVar[str] = contextvars.ContextVar("notesbot_exam_subject", default="")
_SANSKRIT_STYLE: contextvars.ContextVar[str] = contextvars.ContextVar("notesbot_sanskrit_style", default="hindi")
_SANSKRIT_HINDI: contextvars.ContextVar[bool] = contextvars.ContextVar("notesbot_sanskrit_hindi", default=False)
_FIXED_SIGNATURE: contextvars.ContextVar[bool] = contextvars.ContextVar("notesbot_fixed_signature", default=False)


@contextlib.contextmanager
def _switch(var: contextvars.ContextVar, value) -> Iterator[None]:
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


def lean() -> bool:
    """True inside ``lean_requests()``."""
    return _LEAN.get()


def lean_requests(enabled: bool = True):
    return _switch(_LEAN, bool(enabled))


def inline_source_check_enabled() -> bool:
    return _INLINE_CHECK.get()


def inline_source_check(enabled: bool):
    return _switch(_INLINE_CHECK, bool(enabled))


def current_purpose() -> str:
    """What the next model call is for, when the call site says so."""
    return _PURPOSE.get()


def call_purpose(purpose: str):
    return _switch(_PURPOSE, str(purpose or ""))


def current_label() -> str:
    """Which part of the lecture the next model call works on."""
    return _LABEL.get()


def call_label(label: str):
    return _switch(_LABEL, str(label or ""))


def current_category() -> str:
    """The exam category (``notesbot.categories``) for this run, or ""."""
    return _CATEGORY.get()


def exam_category(category: str):
    return _switch(_CATEGORY, str(category or ""))


def current_subject() -> str:
    """The lecture's subject for this run (sheet column D or the Studio), or ""."""
    return _SUBJECT.get()


def exam_subject(subject: str):
    return _switch(_SUBJECT, str(subject or ""))


def sanskrit_style() -> str:
    """How a Sanskrit lecture is written: "hindi" (verses verbatim, Hindi
    explanations - the default) or "strict" (everything in Sanskrit)."""
    return _SANSKRIT_STYLE.get()


def sanskrit_style_as(style: str):
    return _switch(_SANSKRIT_STYLE, "strict" if style == "strict" else "hindi")


def sanskrit_hindi() -> bool:
    """True while generating Hindi notes for a Sanskrit lecture."""
    return _SANSKRIT_HINDI.get()


def sanskrit_hindi_notes(enabled: bool = True):
    return _switch(_SANSKRIT_HINDI, bool(enabled))


def signature_fixed() -> bool:
    """True when the run's signature was typed in the Studio, not read from the sheet."""
    return _FIXED_SIGNATURE.get()


def fixed_signature(enabled: bool = True):
    return _switch(_FIXED_SIGNATURE, bool(enabled))
