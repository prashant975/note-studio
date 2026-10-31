"""The PW Books handwritten notes engine (engines/pw_books): one child process per deck.

NoteCraft writes request.json and runs engines/pw_books/engine_main.py, which
runs the deck the way the PW Books app's page does (its model routing and its
run_pipeline call). The note type's own layers (note type, category x type,
this run) go in as one block at the end of the engine's own prompt, through the
copy's hook. The NEET-JEE category's general rule files are the hand-drawn
notes engine's own format rules, so they are not sent here.
"""
from __future__ import annotations

from pathlib import Path

from .. import models
from ..requirements import APPLIED
from . import child

ENGINE = "pw_books"
EXTRA_LAYERS = APPLIED[ENGINE]


def build_request(run: dict, options: dict, folder: Path, extra: str, resolved: dict) -> dict:
    return {
        "category": run["category"], "note_type": run["note_type"],
        "subject": run["subject"], "language": run["language"],
        "file": str(Path(folder) / "input" / str(options.get("file") or "")),
        "mode": str(options.get("mode") or "summary"),
        "exam": str(options.get("exam") or ""),
        "math_render_mode": str(options.get("math_render_mode") or "omml"),
        "strict_math": bool(options.get("strict_math", True)),
        "extra_instructions": extra,
        "requirements": resolved,
        "models": {stage: models.order(run["note_type"], stage) for stage in models.stages(run["note_type"])},
    }


def make_notes(run_id: int, folder: Path, request: dict, *, token: str, step, on_cost=None) -> child.ChildResult:
    result = child.make_notes(ENGINE, run_id, folder, request, token=token, step=step, on_cost=on_cost)
    if result.state == "failed" and result.message == "The engine stopped unexpectedly.":
        result.message = "The PW Books engine stopped unexpectedly."
    elif result.state == "complete":
        result.message = "Done: the handwritten notes are ready."
    return result
