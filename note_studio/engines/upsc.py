"""The UPSC notes engine (engines/upsc): one child process per class (doc 07).

NoteCraft writes request.json and runs engines/upsc/engine_main.py, which calls
the UPSC app's own run_pipeline exactly as its page does. Requirement layers
2-7 (note type, category, subject, category x type, this run) go in as one
block at the end of the engine's own prompt, through the copy's hook. The
sign-in token goes only into the child's environment.
"""
from __future__ import annotations

from pathlib import Path

from .. import engine_catalog, models, note_types, paths
from . import child

ENGINE = "upsc"
ClassResult = child.ChildResult


def data_dir() -> Path:
    """Where the engine looks for the DPP workbooks by default (src/dpp_builder.py: ROOT_DIR/data)."""
    return engine_catalog.engine_dir(ENGINE) / "data"


def state_dir() -> Path:
    """The engine's own cache (it would otherwise use the old app's %LOCALAPPDATA% folder)."""
    path = paths.home() / "engines" / ENGINE
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_request(run: dict, options: dict, folder: Path, extra: str, resolved: dict) -> dict:
    entry = note_types.get(run["note_type"]) or {}
    slides = str(options.get("file") or "")
    return {
        "engine_format": entry.get("engine_format", "dcn"),
        "category": run["category"], "note_type": run["note_type"],
        "subject": run["subject"], "language": run["language"],
        "video_url": str(options.get("video_url") or ""),
        "transcript_text_override": str(options.get("transcript_text_override") or "").strip(),
        "slides": str(Path(folder) / "input" / slides) if slides else "",
        "lecture_title": str(options.get("lecture_title") or ""),
        "dcn_no": str(options.get("dcn_no") or ""),
        "dmp_no": str(options.get("dmp_no") or ""),
        "dpp_no": str(options.get("dpp_no") or ""),
        "extra_instructions": extra,
        "requirements": resolved,
        "models": {stage: models.order(run["note_type"], stage) for stage in models.stages(run["note_type"])},
        "state_dir": str(state_dir()),
    }


def make_notes(run_id: int, folder: Path, request: dict, *, token: str, step, on_cost=None) -> ClassResult:
    """Run one class; `step(text, pct)` gets the old app's progress words."""
    result = child.make_notes(ENGINE, run_id, folder, request, token=token, step=step, on_cost=on_cost)
    if result.state == "failed" and result.message == "The engine stopped unexpectedly.":
        result.message = "The UPSC engine stopped unexpectedly."
    elif result.state == "complete":
        pages = f"{result.pages} page{'s' if result.pages != 1 else ''} of notes" if result.pages else "the notes"
        result.message = f"Done: {pages}, with the error report."
    return result
