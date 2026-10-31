"""Capture what one thread prints, without touching other threads' output.

The engine's check tools (setup check, model test, PDF probe) report by
printing. NoteCraft runs them in a background thread and shows their lines;
a notes run printing at the same time must not end up in that report.
"""
from __future__ import annotations

import contextlib
import io
import sys
import threading

_lock = threading.Lock()


class _Router(io.TextIOBase):
    def __init__(self, fallback):
        self.fallback = fallback
        self.buffers: dict[int, io.StringIO] = {}

    def write(self, text):
        target = self.buffers.get(threading.get_ident())
        if target is not None:
            target.write(text)
        elif self.fallback is not None:
            self.fallback.write(text)
        return len(text)

    def flush(self):
        if self.fallback is not None:
            try:
                self.fallback.flush()
            except Exception:
                pass

    @property
    def encoding(self):
        return getattr(self.fallback, "encoding", "utf-8") or "utf-8"


def _router(name: str) -> _Router:
    with _lock:
        stream = getattr(sys, name)
        if not isinstance(stream, _Router):
            stream = _Router(stream)
            setattr(sys, name, stream)
        return stream


@contextlib.contextmanager
def captured():
    """Everything this thread prints (stdout and stderr) goes into the returned buffer."""
    buffer = io.StringIO()
    routers = [_router("stdout"), _router("stderr")]
    ident = threading.get_ident()
    for router in routers:
        router.buffers[ident] = buffer
    try:
        yield buffer
    finally:
        for router in routers:
            router.buffers.pop(ident, None)
