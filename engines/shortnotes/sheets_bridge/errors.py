"""Bridge error type carrying a short machine-readable code."""
from __future__ import annotations

import os
import sys
import traceback


class BridgeError(Exception):
    """An expected, classified failure. ``code`` is what lands in column T."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def safe_print(line: str) -> None:
    """Print without dying on a cp1252 console: source names are Devanagari."""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


def describe_exception(exc: BaseException, *, depth: int = 2) -> str:
    """Return ``ClassName @ file.py:line`` - never the message or any content.

    The generator processes real student materials, and model/HTTP exceptions
    can embed request or response text, so the message is deliberately dropped.

    A failure raised in place of another one also names what caused it
    (``NotesCoverageError @ a.py:1 <- APITimeoutError @ b.py:2``). Without that
    chain a generic wrapper hides the reason the lecture actually stopped -
    a timeout and a genuine coverage gap look identical in the log.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" @ {os.path.basename(last.filename)}:{last.lineno}"
    described = f"{type(exc).__name__}{where}"
    cause = exc.__cause__          # only explicit `from`/assigned causes
    if cause is not None and depth > 0:
        return f"{described} <- {describe_exception(cause, depth=depth - 1)}"
    return described


def error_code_for(exc: BaseException, stage: str) -> str:
    """Compress any exception into a short, content-free code for column T."""
    if isinstance(exc, BridgeError):
        return exc.code
    code = getattr(exc, "code", None)  # e.g. notesbot VisualFidelityError
    if isinstance(code, str) and code and " " not in code and len(code) <= 40:
        return f"{stage}:{code}"
    return f"{stage}:{type(exc).__name__}"[:60]
