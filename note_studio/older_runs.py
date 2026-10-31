"""Runs -> From older apps: the original apps' own runs, read in place.

Nothing here writes, moves or creates anything in an original's folder: it
lists run folders, reads their run_metadata.json, and serves the files their
output/ folder already holds.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import note_types, paths

RUN_NAME = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{8}$")
FORMAT_WORDS = {"dcn": "UPSC daily class notes (DCN)", "simple": "UPSC simple notes", "mains": "UPSC Mains-ready notes",
                "prelims": "UPSC Prelims-ready notes"}
OFFERED = (".docx", ".pdf", ".xlsx", ".md")
# The old app's page never offers these; they stay in the run folder (src/config.py DEVELOPER_ONLY_REPORTS).
DEVELOPER_ONLY = ("_quality_report.docx", "_error_report.docx", "_error_fixed_report.docx")


def upsc_runs_dir() -> Path:
    return paths.upsc_original() / "runs"


def _when(name: str, meta: dict) -> float:
    try:
        return datetime.strptime(name[4:19], "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return 0.0


def _files(folder: Path) -> list[dict]:
    output = folder / "output"
    if not output.is_dir():
        return []
    found = [p for p in sorted(output.iterdir()) if p.is_file() and p.suffix.lower() in OFFERED
             and not p.name.endswith(DEVELOPER_ONLY)]
    words = {".pdf": "PDF", ".docx": "Word document", ".xlsx": "Excel workbook", ".md": "Text report"}
    return [{"name": p.name, "kind_words": words[p.suffix.lower()], "size": p.stat().st_size} for p in found]


def _summary(folder: Path) -> dict | None:
    try:
        meta = json.loads((folder / "run_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    failed = (folder / "error.json").is_file()
    if not isinstance(meta, dict) and not failed:
        return None
    meta = meta if isinstance(meta, dict) else {}
    providers = {str(c.get("provider") or "") for c in meta.get("api_calls") or [] if isinstance(c, dict)}
    input_file = Path(str(meta.get("input_file") or "")).stem
    language = str(meta.get("language") or meta.get("detected_language") or "").lower()
    language = {"en": "english", "hi": "hindi", "mr": "marathi"}.get(language, language)
    return {
        "app": "upsc", "app_label": "UPSC Notes", "name": folder.name, "when": _when(folder.name, meta),
        "title": str(meta.get("lecture_title") or "").strip() or input_file or "Class recording",
        "type_label": FORMAT_WORDS.get(str(meta.get("upsc_output_format") or ""), "UPSC notes"),
        "subject": str(meta.get("subject") or ""), "language": note_types.language_label(language) if language else "",
        "state_words": "Did not finish" if failed else "Done", "state_cls": "danger" if failed else "ok",
        "test_run": bool(providers) and providers <= {"mock", "fake"},
        "video_url": str(meta.get("video_url") or ""), "dcn_no": str(meta.get("dcn_no") or ""),
        "warnings": [str(w) for w in meta.get("warnings") or []][:40],
    }


def upsc_runs() -> list[dict]:
    """The original UPSC app's runs, newest first; [] when its folder is not on this computer."""
    root = upsc_runs_dir()
    if not root.is_dir():
        return []
    found = [_summary(p) for p in root.iterdir() if p.is_dir() and RUN_NAME.match(p.name)]
    return sorted((r for r in found if r), key=lambda r: r["when"], reverse=True)


def upsc_run(name: str) -> dict | None:
    if not RUN_NAME.match(str(name or "")):
        return None
    folder = upsc_runs_dir() / name
    run = _summary(folder) if folder.is_dir() else None
    if run:
        run["files"] = _files(folder)
        run["folder"] = str(folder)
    return run


def upsc_file(name: str, filename: str) -> Path | None:
    """One file the old app's page would offer, from a run's output folder; None otherwise."""
    run = upsc_run(name)
    if not run or filename not in {f["name"] for f in run["files"]}:
        return None
    return upsc_runs_dir() / name / "output" / filename
