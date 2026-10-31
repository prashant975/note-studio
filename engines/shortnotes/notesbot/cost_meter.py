"""Per-job model-usage metering for user-visible cost display in INR.

The active meter travels via a ContextVar so call sites never need job
plumbing; thread pools must copy their context per task for propagation.
Recording never raises: cost display must not be able to break generation.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from . import config

_ACTIVE: ContextVar["UsageMeter | None"] = ContextVar(
    "notesbot_cost_meter", default=None
)


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


@dataclass
class UsageMeter:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    images: int = 0
    calls: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        images: int = 0,
    ) -> None:
        with self._lock:
            self.input_tokens += _safe_count(input_tokens)
            self.output_tokens += _safe_count(output_tokens)
            self.cached_tokens += _safe_count(cached_tokens)
            self.images += _safe_count(images)
            self.calls += 1

    def cost_inr(self) -> float:
        with self._lock:
            tokens_cost = (
                self.input_tokens * config.LLM_COST_INPUT_INR_PER_1M
                + self.output_tokens * config.LLM_COST_OUTPUT_INR_PER_1M
            ) / 1_000_000
            return round(tokens_cost + self.images * config.IMAGE_COST_INR_PER_IMAGE, 2)

    def snapshot(self) -> dict:
        cost = self.cost_inr()
        with self._lock:
            return {
                "inr": cost,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_tokens": self.cached_tokens,
                "images": self.images,
                "calls": self.calls,
            }


@contextmanager
def metered_job() -> Iterator[UsageMeter]:
    meter = UsageMeter()
    token = _ACTIVE.set(meter)
    try:
        yield meter
    finally:
        _ACTIVE.reset(token)


def record_openai_usage(response: object) -> None:
    """Accumulate usage from an OpenAI/LiteLLM chat-completions response."""
    meter = _ACTIVE.get()
    if meter is None:
        return
    try:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        meter.add(
            input_tokens=_safe_count(getattr(usage, "prompt_tokens", 0)),
            output_tokens=_safe_count(getattr(usage, "completion_tokens", 0)),
            cached_tokens=_safe_count(getattr(details, "cached_tokens", 0)),
        )
    except Exception:
        pass


def record_genai_usage(response: object) -> None:
    """Accumulate usage from a google-genai generate_content response."""
    meter = _ACTIVE.get()
    if meter is None:
        return
    try:
        usage = getattr(response, "usage_metadata", None)
        prompt = _safe_count(getattr(usage, "prompt_token_count", 0))
        candidates = _safe_count(getattr(usage, "candidates_token_count", 0))
        thoughts = _safe_count(getattr(usage, "thoughts_token_count", 0))
        cached = _safe_count(getattr(usage, "cached_content_token_count", 0))
        meter.add(
            input_tokens=prompt,
            output_tokens=candidates + thoughts,
            cached_tokens=cached,
        )
    except Exception:
        pass


def record_images(count: int = 1) -> None:
    meter = _ACTIVE.get()
    if meter is None:
        return
    try:
        meter.add(images=_safe_count(count))
    except Exception:
        pass


def format_cost_line(snapshot: dict) -> str:
    """One short user-facing line, e.g. '₹4.86 · input 152,340 tokens · output 8,113 tokens'."""
    try:
        inr = float(snapshot.get("inr", 0.0))
        input_tokens = _safe_count(snapshot.get("input_tokens", 0))
        output_tokens = _safe_count(snapshot.get("output_tokens", 0))
        images = _safe_count(snapshot.get("images", 0))
        parts = [
            f"₹{inr:.2f}",
            f"input {input_tokens:,} tokens",
            f"output {output_tokens:,} tokens",
        ]
        if images:
            parts.append(f"{images} image{'s' if images != 1 else ''}")
        return " · ".join(parts)
    except Exception:
        return ""
