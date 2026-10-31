"""The copied engines: what each makes, how it runs, and whether its copy is here.

Facts about a copy come from its COPIED_FROM.md (written by
NoteStudio_Kit/tools/copy_engine.py). Nothing here imports an engine.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import paths
from .brand import APP_NAME as APP

ENGINES = (
    {"id": "shortnotes", "label": "Hand-drawn notes", "icon": "pen", "types_engine": "notesbot",
     "makes": "Concise hand-drawn notes from a lecture PDF, and the Google Sheet automation",
     "runs": f"Inside {APP}", "for": "NEET-JEE, Foundation"},
    {"id": "upsc", "label": "UPSC notes", "icon": "video",
     "makes": "Daily class notes, simple, Mains and Prelims notes from a class recording; Mains practice; DPP",
     "runs": "In its own window, one per run", "for": "UPSC"},
    {"id": "pw_books", "label": "PW Books handwritten", "icon": "book",
     "makes": "Handwritten-style Word and PDF notes from a lecture deck",
     "runs": "In its own window, one per run", "for": "NEET-JEE"},
    {"id": "notes_qc", "label": "Notes check", "icon": "shield",
     "makes": "A check report for any finished notes file",
     "runs": "In its own window, one per run", "for": "Every category"},
    # Not a copy of an older app: NoteCraft's own engine (note_studio/typeset).
    {"id": "typeset", "label": "Typeset notes", "icon": "file-text", "builtin": True,
     "makes": "Typeset notes with boxes, tables and labelled figures from a lecture PDF",
     "runs": f"Inside {APP}", "for": "B.Sc."},
)


def engine_dir(engine_id: str) -> Path:
    return paths.ENGINES_DIR / engine_id


def available(engine_id: str) -> bool:
    """Only copies present in this installation are offered; typeset is built in."""
    if engine_id == "typeset":
        return (paths.PACKAGE_DIR / "typeset").is_dir()
    return engine_dir("shortnotes" if engine_id == "notesbot" else engine_id).is_dir()


def status(engine: dict) -> dict:
    """One engine plus whether its copy is present, and when it was copied
    (an engine built into NoteCraft is always on board)."""
    if engine.get("builtin"):
        code = paths.PACKAGE_DIR / engine["id"]
        return dict(engine, copied=True, copied_on="", size="",
                    files=str(sum(1 for _ in code.glob("*.py"))) if code.is_dir() else "")
    folder = engine_dir(engine["id"])
    note = folder / "COPIED_FROM.md"
    info = dict(engine, copied=available(engine["id"]), copied_on="", files="", size="")
    if not note.is_file():
        return info
    text = note.read_text(encoding="utf-8", errors="replace")
    info["copied"] = True
    when = re.search(r"- Copied: (\d{4}-\d{2}-\d{2})", text)
    info["copied_on"] = when.group(1) if when else ""
    files = re.search(r"- Files: (\d+) \(([\d.]+ MB)\)", text)
    if files:
        info["files"], info["size"] = files.group(1), files.group(2)
    added = re.findall(r"- Files: (\d+) \(([\d.]+) MB\)", text)[1:]
    if added and info["files"]:
        info["files"] = str(int(info["files"]) + sum(int(n) for n, _ in added))
    return info


def all_status() -> list[dict]:
    return [status(e) for e in ENGINES]


def put_shortnotes_on_path() -> Path:
    """The hand-drawn notes engine runs in this process: its folder goes on
    sys.path so `notesbot` and `sheets_bridge` import unchanged. Appended, so it
    can never shadow an installed package of the same name."""
    folder = engine_dir("shortnotes")
    if folder.is_dir() and str(folder) not in sys.path:
        sys.path.append(str(folder))
    return folder
