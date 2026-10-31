"""The notes check engine (engines/notes_qc, the UPSC file QC tool): one child process per check.

NoteCraft writes request.json and runs engines/notes_qc/engine_main.py, which
checks the files the way the QC tool's page does. "full" runs its AI error check
(PW proxy only, web search allowed: the user's decisions); otherwise only the
checks that need no AI run (no sign-in, no cost).
"""
from __future__ import annotations

from pathlib import Path

from .. import models
from . import child

ENGINE = "notes_qc"
TOOL = "notes_qc"


def build_request(files: list[Path], full: bool) -> dict:
    return {"files": [str(f) for f in files], "full": bool(full),
            "models": {"notes": models.order(TOOL, "notes")}}


def make_notes(run_id: int, folder: Path, request: dict, *, token: str, step, on_cost=None) -> child.ChildResult:
    result = child.make_notes(ENGINE, run_id, folder, request, token=token, step=step, on_cost=on_cost)
    if result.state == "failed" and result.message == "The engine stopped unexpectedly.":
        result.message = "The notes check stopped unexpectedly."
    return result
