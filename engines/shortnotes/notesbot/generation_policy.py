"""Request-scoped limits for the direct Gemini notes pipeline."""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import VisualFidelityError


@dataclass
class GenerationRun:
    """Enforce one notes call per chunk and at most two image calls."""

    deadline_at: float
    max_images: int
    clock: Callable[[], float] = time.monotonic
    _note_calls: dict[int, int] = field(default_factory=dict, init=False)
    _eligible_keys: set[str] = field(default_factory=set, init=False)
    _image_keys: list[str] = field(default_factory=list, init=False)
    _omitted_keys: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0.0:
            raise VisualFidelityError("deadline_exceeded")

    def remaining_seconds(self) -> float:
        remaining = float(self.deadline_at) - float(self.clock())
        if not math.isfinite(remaining):
            raise VisualFidelityError("deadline_exceeded")
        return remaining

    def take_note(self, chunk_index: int) -> None:
        self.check_deadline()
        with self._lock:
            if chunk_index < 0 or chunk_index in self._note_calls:
                raise VisualFidelityError("work_limit_exceeded")
            self._note_calls[chunk_index] = 1

    def mark_visual_eligible(self, job_key: str) -> None:
        self.check_deadline()
        with self._lock:
            if not job_key:
                raise VisualFidelityError("work_limit_exceeded")
            self._eligible_keys.add(job_key)

    def try_take_image(self, job_key: str) -> bool:
        self.check_deadline()
        with self._lock:
            if not job_key or job_key in self._image_keys:
                raise VisualFidelityError("work_limit_exceeded")
            if len(self._image_keys) >= min(2, max(0, int(self.max_images))):
                return False
            self._image_keys.append(job_key)
            return True

    def mark_image_omitted(self, job_key: str) -> None:
        if not job_key:
            raise VisualFidelityError("work_limit_exceeded")
        with self._lock:
            self._omitted_keys.add(job_key)

    def snapshot(self, chunk_count: int) -> dict[str, int]:
        with self._lock:
            return {
                "chunk_count": int(chunk_count),
                "notes_model_calls": sum(self._note_calls.values()),
                "eligible_visuals": len(self._eligible_keys),
                "image_model_calls": len(self._image_keys),
                "images_omitted": len(self._omitted_keys),
                "mathpix_calls": 0,
                "inventory_calls": 0,
                "audit_calls": 0,
                "verifier_calls": 0,
            }
