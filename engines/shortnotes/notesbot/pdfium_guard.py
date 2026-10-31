"""One lock for every PDFium call.

PDFium is not thread-safe, and the notes are made for up to four page groups
at once. Two threads inside PDFium at the same moment corrupted its state for
the rest of the process: every later lecture then failed with
``generate:crop_failed`` (PdfiumError "Failed to load page") until the app was
restarted. Every pypdfium2 call - opening a document, loading, rendering and
closing a page - is therefore made while holding this lock. The model calls,
the slow part, still run in parallel.

The lock is re-entrant, so a guarded function may call another one.
"""
from __future__ import annotations

import functools
import threading

PDFIUM_LOCK = threading.RLock()


def serialized(func):
    """Run ``func`` while holding the PDFium lock (for functions that open,
    use and close their PDFium objects without yielding in between)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with PDFIUM_LOCK:
            return func(*args, **kwargs)

    return wrapper
