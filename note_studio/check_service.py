"""Check notes: an error report (Excel) for finished notes (Phase 8).

A check is its own run (source 'check'). It checks one file: a finished run's
notes (its Word file, else its PDF), copied in with shutil.copy2, or a file
uploaded on the Check notes page. It waits in the Smart Queue's Notes lane.

UPSC notes get the QC tool's full check (its AI error report, through PW; web
search allowed). Its prompts are written for UPSC class notes, so other notes
get the checks that need no AI (spelling, names, leftover markers, repeats):
no sign-in and no cost.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from . import db, job_queue, make_service, signin
from .engines import notes_qc as qc_engine

ACCEPTED = (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".png", ".jpg", ".jpeg")
MAX_MB = 200
# Tests replace this to run without signing in: check_token() -> Google token.
check_token = signin.google_token


class CheckError(ValueError):
    """Plain words for the page."""


def checkable_file(run: dict) -> Path | None:
    """The notes file of a finished run: its Word file, else its PDF."""
    if run["source"] not in ("upload",) or run["state"] not in ("complete", "needs_attention"):
        return None
    files = [Path(r["output_path"]) for r in db.query("SELECT output_path FROM run_items WHERE run_id=? ORDER BY position",
                                                      (run["id"],)) if r["output_path"]]
    for suffix in (".docx", ".pdf"):
        found = next((f for f in files if f.suffix.lower() == suffix and f.is_file()
                      and "error_report" not in f.name and "quality_report" not in f.name), None)
        if found is not None:
            return found
    return None


def check_run(run_id: int) -> tuple[int, bool]:
    """Queue a check of a finished run's notes."""
    rows = db.query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        raise CheckError("That run no longer exists.")
    run = rows[0]
    source = checkable_file(run)
    if source is None:
        raise CheckError("This run has no finished notes file to check.")
    full = run["category"] == "upsc"
    payload = {"file": source.name, "full": full, "from_run": run_id, "category": run["category"]}
    return job_queue.submit("check", f"Check: {run['title']}", payload, uploads=[(source.name, source.read_bytes())],
                            key=f"check-{run_id}-{hashlib.sha256(source.read_bytes()).hexdigest()}")


def check_upload(name: str, data: bytes, full: bool) -> tuple[int, bool]:
    """Queue a check of an uploaded file."""
    name = make_service.safe_name(name)
    if not data:
        raise CheckError("Choose the notes file to check.")
    if Path(name).suffix.lower() not in ACCEPTED:
        raise CheckError(f"{name} cannot be checked. Use a PDF, Word, PowerPoint, text or image file.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise CheckError(f"{name} is larger than {MAX_MB} MB.")
    payload = {"file": name, "full": bool(full), "category": "upsc" if full else ""}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + hashlib.sha256(data).digest()).hexdigest()
    return job_queue.submit("check", f"Check: {Path(name).stem}", payload, uploads=[(name, data)], key=key)


def launch(entry: dict) -> tuple[str, str, int | None]:
    """The Notes lane's launcher for a check."""
    payload = entry["payload"]
    source = next((p for p in job_queue.files(entry) if p.name == payload.get("file")), None)
    if source is None:
        return "failed", "The file to check is missing.", None
    full = bool(payload.get("full"))
    run_id = db.create_run(qc_engine.ENGINE, title=entry["title"], note_type=qc_engine.TOOL,
                           category=str(payload.get("category") or ""), source="check",
                           options={"file": source.name, "full": full, "from_run": payload.get("from_run"),
                                    "queue_id": entry["id"]})
    folder = make_service.run_dir(run_id)
    (folder / "input").mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, folder / "input" / source.name)
    db.execute("UPDATE runs SET run_dir=?, queue_id=?, state='running', started_at=?, pct=2 WHERE id=?",
               (str(folder), entry["id"], time.time(), run_id))
    make_service.event(run_id, "step", "Its turn in the Smart Queue came", 1)
    token = ""
    if full:
        try:
            token = check_token()
        except signin.SignInNeeded as problem:
            db.execute("UPDATE runs SET options_json=? WHERE id=?",
                       (json.dumps({"file": source.name, "full": full, "sign_in_problem": True}), run_id))
            make_service.finish(run_id, "failed", str(problem), error=str(problem))
            return "signin", str(problem), run_id
    result = qc_engine.make_notes(
        run_id, folder, qc_engine.build_request([folder / "input" / source.name], full), token=token,
        step=lambda text, pct=None: make_service.event(run_id, "step", text, pct),
        on_cost=lambda total: db.execute("UPDATE runs SET cost_inr=? WHERE id=?", (total, run_id)))
    for text in result.warnings:
        make_service.event(run_id, "warning", text)
    for position, item in enumerate(result.outputs, start=1):
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, position, Path(item["path"]).name, "complete", item["path"],
                                                 0, time.time(), time.time()))
        make_service.event(run_id, "output", Path(item["path"]).name, None, path=item["path"], file_type=item["kind"],
                           label=item["label"])
    error = "" if result.state in ("complete", "needs_attention") else result.message
    make_service.finish(run_id, result.state, result.message, error=error, detail=result.detail, cost=result.cost_inr)
    if result.sign_in:
        return "signin", result.message, run_id
    if result.state in ("complete", "needs_attention"):
        return "done", result.message, run_id
    return ("stopped" if result.state == "stopped" else "failed"), result.message, run_id


def again(run_id: int) -> int | None:
    """The same file checked again (a new queue item)."""
    rows = db.query("SELECT * FROM runs WHERE id=? AND source='check'", (run_id,))
    if not rows or rows[0]["state"] in ("waiting", "running"):
        return None
    options = json.loads(rows[0]["options_json"] or "{}")
    source = Path(rows[0]["run_dir"] or "") / "input" / str(options.get("file") or "")
    if not options.get("file") or not source.is_file():
        return None
    payload = {"file": source.name, "full": bool(options.get("full")), "from_run": options.get("from_run"),
               "category": rows[0]["category"]}
    entry_id, _ = job_queue.submit("check", rows[0]["title"], payload, uploads=[(source.name, source.read_bytes())],
                                   key=f"check-again-{run_id}")
    return entry_id


def recent(limit: int = 30) -> list[dict]:
    return db.query("SELECT * FROM runs WHERE source='check' ORDER BY id DESC LIMIT ?", (limit,))


job_queue.register("check", launch)
