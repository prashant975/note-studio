"""Practice: Mains practice (DMP) and DPP from finished UPSC notes (Phase 6).

A practice set is its own run (source 'practice'). Starting one copies the
lecture's notes files and run_metadata.json, with shutil.copy2, into
runs/<id>/source_runs/<lecture folder>/: the engine works on that copy, so an
older app's run is only ever read. Each step is one call of the UPSC engine's
child process (engines/upsc/engine_practice.py), the way one button press is
one step on the old app's page:

- paid steps (questions, model answers) wait in the Smart Queue;
- free steps (DPP topics, counts, set, replace, remove, the sheets and PDFs)
  run at once, one at a time per practice set.

What the old app keeps in its page state (the DPP topics, the set, what was
already shown) is kept in runs/<id>/practice.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import db, engine_catalog, engine_host, job_queue, make_service, models, older_runs, signin
from .engines import upsc as upsc_engine

log = logging.getLogger("note_studio.practice")
KINDS = {"dmp": {"note_type": "upsc_mains_practice", "label": "Mains practice"},
         "dpp": {"note_type": "dpp_from_notes", "label": "DPP"}}
NOTE_FILES = ("notes_final.md", "notes_repaired.md", "notes_after_qc.md", "notes_raw.md", "notes_raw.txt")
PAID = {"dmp_questions", "dmp_answers"}
LANGUAGES = {"en": "english", "hi": "hindi", "mr": "marathi", "english": "english", "hindi": "hindi",
             "marathi": "marathi"}
MASTERS_KEY = "dpp_masters_folder"
MASTER_FILES = {"taxonomy": "UPSC QBG Tags.xlsx", "questions": "DPP and Infinite Practice Questions.xlsx"}
# Tests replace these: practice_token() -> Google token; TEST_REQUEST is merged
# into every step's request (a canned-reply client, the PDF stub). Empty in use.
practice_token = signin.google_token
TEST_REQUEST: dict = {}
_busy: set[int] = set()
_lock = threading.Lock()


class PracticeError(ValueError):
    """Plain words for the Practice page."""


# ----------------------------------------------------------------- sources
def _has_notes(folder: Path) -> bool:
    return folder.is_dir() and any((folder / name).is_file() for name in NOTE_FILES)


def _meta(folder: Path) -> dict:
    try:
        value = json.loads((folder / "run_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _notecraft_folder(run: dict) -> Path | None:
    engine_runs = Path(run["run_dir"] or "") / "engine_runs"
    found = sorted(engine_runs.glob("run_*")) if run["run_dir"] and engine_runs.is_dir() else []
    return next((f for f in reversed(found) if _has_notes(f)), None)


def sources() -> list[dict]:
    """Finished UPSC notes a practice set can be made from: this app's, then the old app's."""
    found = []
    for run in db.query("SELECT * FROM runs WHERE engine='upsc' AND source='upload' "
                        "AND state IN ('complete','needs_attention') ORDER BY id DESC LIMIT 100"):
        folder = _notecraft_folder(run)
        if folder is not None:
            found.append({"kind": "notecraft", "id": str(run["id"]), "title": run["title"], "when": run["created_at"],
                          "language": LANGUAGES.get(str(_meta(folder).get("language") or "").lower(), ""),
                          "test_run": False})
    for old in older_runs.upsc_runs():
        if _has_notes(older_runs.upsc_runs_dir() / old["name"]):
            found.append({"kind": "older", "id": old["name"], "title": old["title"], "when": old["when"],
                          "language": old["language"].lower(), "test_run": old["test_run"]})
    return found


def source_folder(kind: str, ident: str) -> Path | None:
    if kind == "notecraft" and str(ident).isdigit():
        rows = db.query("SELECT * FROM runs WHERE id=? AND engine='upsc' AND source='upload'", (int(ident),))
        return _notecraft_folder(rows[0]) if rows else None
    if kind == "older" and older_runs.RUN_NAME.match(str(ident)):
        folder = older_runs.upsc_runs_dir() / ident
        return folder if _has_notes(folder) else None
    return None


# ------------------------------------------------------------------ start
def start(practice: str, kind: str, ident: str) -> int:
    """A new practice set from one lecture's notes. Returns its run id."""
    if not engine_catalog.available("upsc"):
        raise PracticeError("Mains practice and DPP are not in this installation.")
    if practice not in KINDS:
        raise PracticeError("Choose Mains practice or DPP.")
    folder = source_folder(kind, ident)
    if folder is None:
        raise PracticeError("Those notes are no longer on this computer.")
    meta = _meta(folder)
    title = str(meta.get("lecture_title") or "").strip() or (
        db.query("SELECT title FROM runs WHERE id=?", (int(ident),))[0]["title"] if kind == "notecraft"
        else next((r["title"] for r in older_runs.upsc_runs() if r["name"] == ident), folder.name))
    language = LANGUAGES.get(str(meta.get("language") or "").lower(), "")
    run_id = db.create_run("upsc", title=f"{KINDS[practice]['label']}: {title}", note_type=KINDS[practice]["note_type"],
                           category="upsc", subject=str(meta.get("subject") or ""), language=language,
                           source="practice", options={"practice": practice, "note_id": folder.name,
                                                       "from": {"kind": kind, "id": ident, "title": title}})
    run_folder = make_service.run_dir(run_id)
    copy = run_folder / "source_runs" / folder.name
    copy.mkdir(parents=True, exist_ok=True)
    for name in (*NOTE_FILES, "run_metadata.json"):
        if (folder / name).is_file():
            shutil.copy2(folder / name, copy / name)
    db.execute("UPDATE runs SET run_dir=?, state='complete', message=?, pct=100, started_at=?, finished_at=? WHERE id=?",
               (str(run_folder), "Ready.", time.time(), time.time(), run_id))
    _save_state(run_id, {"verified_only": True})
    if practice == "dpp":
        step(run_id, "dpp_topics")
    return run_id


# ------------------------------------------------------------------ state
def get(run_id: int) -> dict | None:
    rows = db.query("SELECT * FROM runs WHERE id=? AND source='practice'", (run_id,))
    if not rows:
        return None
    run = dict(rows[0])
    run["options"] = json.loads(run["options_json"] or "{}")
    return run


def _state_path(run_id: int) -> Path:
    return make_service.run_dir(run_id) / "practice.json"


def state(run_id: int) -> dict:
    try:
        value = json.loads(_state_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(run_id: int, value: dict) -> None:
    path = _state_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def update_state(run_id: int, **changes) -> dict:
    current = state(run_id)
    current.update(changes)
    _save_state(run_id, current)
    return current


def copy_dir(run_id: int) -> Path:
    run = get(run_id)
    return make_service.run_dir(run_id) / "source_runs" / str(run["options"]["note_id"])


def masters_folder() -> str:
    return db.get_setting(MASTERS_KEY, "")


def masters() -> dict:
    """The DPP workbooks chosen in Settings, or {} for the engine's own data folder."""
    folder = masters_folder()
    if not folder:
        return {}
    return {kind: str(Path(folder) / name) for kind, name in MASTER_FILES.items()}


def masters_status() -> dict:
    folder = Path(masters_folder()) if masters_folder() else upsc_engine.data_dir()
    found = {kind: (folder / name).is_file() for kind, name in MASTER_FILES.items()}
    return {"folder": str(folder), "chosen": bool(masters_folder()), "ready": all(found.values()), "found": found}


def set_masters_folder(folder: str) -> None:
    text = str(folder or "").strip().strip('"')
    if not text:
        db.set_setting(MASTERS_KEY, "")
        return
    path = Path(text).expanduser()
    missing = [name for name in MASTER_FILES.values() if not (path / name).is_file()]
    if missing:
        raise PracticeError("That folder does not hold " + " and ".join(missing) + ".")
    db.set_setting(MASTERS_KEY, str(path))


# ------------------------------------------------------------------ steps
def busy(run_id: int) -> bool:
    with _lock:
        if run_id in _busy:
            return True
    return bool(db.query("SELECT id FROM job_queue WHERE kind='practice' AND state IN ('waiting','running') "
                         "AND json_extract(payload_json, '$.run_id')=?", (run_id,)))


def step(run_id: int, action: str, **args) -> tuple[bool, str]:
    """Start one step. Paid steps wait in the Smart Queue; the others run now."""
    if not engine_catalog.available("upsc"):
        return False, "Mains practice and DPP are not in this installation."
    run = get(run_id)
    if run is None:
        return False, "That practice set no longer exists."
    if busy(run_id):
        return False, "This practice set is already busy with a step; wait for it to finish."
    if action in PAID:
        key = hashlib.sha256(json.dumps([run_id, action, args], sort_keys=True).encode()).hexdigest()
        words = "practice questions" if action == "dmp_questions" else "model answers"
        _id, created = job_queue.submit("practice", f"{run['title']}: {words}", {"run_id": run_id, "action": action,
                                                                                "args": args}, key=key)
        if not created:
            return False, "That step is already in the Smart Queue."
        db.execute("UPDATE runs SET message=? WHERE id=?", (f"Waiting in the Smart Queue: {words}.", run_id))
        return True, f"Added to the Smart Queue: {words}. It uses PW AI credit when its turn comes."
    with _lock:
        _busy.add(run_id)
    threading.Thread(target=_run_free, args=(run_id, action, args), name=f"practice-{run_id}", daemon=True).start()
    return True, ""


def _run_free(run_id: int, action: str, args: dict) -> None:
    try:
        execute(run_id, action, args)
    except Exception:
        log.exception("practice step %s %s", run_id, action)
        update_state(run_id, last={"action": action, "status": "failed", "message": "Something went wrong.",
                                   "at": time.time()})
        db.execute("UPDATE runs SET state='complete', message=? WHERE id=?", ("Something went wrong.", run_id))
    finally:
        with _lock:
            _busy.discard(run_id)


def wait_idle(run_id: int, timeout: float = 120) -> bool:
    """For tests: wait until this practice set's free step is done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            if run_id not in _busy:
                return True
        time.sleep(0.05)
    return False


def execute(run_id: int, action: str, args: dict, token: str = "") -> dict:
    """Run one step in the engine's child process; returns engine_host's result."""
    run = get(run_id)
    folder = make_service.run_dir(run_id)
    current = state(run_id)
    request = {"action": action, "runs_dir": str(folder / "source_runs"), "note_id": run["options"]["note_id"],
               "state_dir": str(upsc_engine.state_dir()), "masters": masters(),
               "models": {"notes": models.order(KINDS["dmp"]["note_type"], "notes")},
               "dpp": {k: v for k, v in current.items() if k not in ("eligible", "last")}, **TEST_REQUEST, **args}
    db.execute("UPDATE runs SET state='running', message=?, started_at=? WHERE id=?",
               (STEP_WORDS.get(action, "Working"), time.time(), run_id))
    changes: dict = {}
    spent = {"inr": 0.0}

    def on_event(event: dict) -> None:
        kind = event.get("event")
        if kind == "state":
            changes.update({k: v for k, v in event.items() if k != "event"})
        elif kind == "step":
            make_service.event(run_id, "step", str(event.get("text") or ""), None)
        elif kind == "cost":
            spent["inr"] += float(event.get("inr") or 0)
        elif kind == "warning":
            make_service.event(run_id, "warning", str(event.get("text") or ""))
        elif kind == "done":
            if event.get("session_id"):
                changes["dmp_session"] = event["session_id"]
            code["value"] = str(event.get("code") or "")

    code = {"value": ""}
    result = engine_host.run(upsc_engine.ENGINE, run_id, folder, request, on_event, token=token)
    current = state(run_id)
    current.update(changes)
    if result["status"] == "complete" and action in ("dpp_build", "dpp_pick", "dpp_replace", "dpp_remove"):
        # What was shown changed: the counts and the list to pick from are out of date (app.py:1789-1794).
        current.update(available=None, eligible=[])
        if action == "dpp_pick":
            current["picked"] = []                   # app.py:1925
    current["last"] = {"action": action, "status": result["status"], "message": result["reason"], "at": time.time(),
                       "code": code["value"]}
    _save_state(run_id, current)
    message = result["reason"] or ("Done." if result["status"] in ("complete", "needs_attention") else "It did not finish.")
    db.execute("UPDATE runs SET state='complete', message=?, finished_at=?, cost_inr=COALESCE(cost_inr,0)+? WHERE id=?",
               (message[:300], time.time(), round(spent["inr"], 4), run_id))
    make_service.event(run_id, "done" if result["status"] != "failed" else "warning",
                       f"{STEP_WORDS.get(action, action)}: {message}")
    return result


STEP_WORDS = {"dmp_questions": "Writing the practice questions", "dmp_answers": "Writing the model answers",
              "dmp_export": "Building the sheet", "dpp_topics": "Matching the notes to the question-bank topics",
              "dpp_counts": "Counting the questions available", "dpp_build": "Choosing the questions",
              "dpp_eligible": "Listing the questions to choose from", "dpp_pick": "Building the set from your ticks",
              "dpp_replace": "Replacing a question", "dpp_remove": "Removing a question",
              "dpp_export": "Writing the question paper and solutions"}


def launch(entry: dict) -> tuple[str, str, int | None]:
    """The Notes lane's launcher for a paid practice step."""
    payload = entry["payload"]
    run_id = int(payload["run_id"])
    if get(run_id) is None:
        return "failed", "That practice set no longer exists.", None
    try:
        token = practice_token()
    except signin.SignInNeeded as problem:
        update_state(run_id, last={"action": payload["action"], "status": "failed", "message": str(problem),
                                   "at": time.time(), "code": "sign_in"})
        db.execute("UPDATE runs SET message=? WHERE id=?", (str(problem), run_id))
        return "signin", str(problem), run_id
    with _lock:
        _busy.add(run_id)
    try:
        result = execute(run_id, payload["action"], dict(payload.get("args") or {}), token=token)
    finally:
        with _lock:
            _busy.discard(run_id)
    last = state(run_id).get("last") or {}
    if result["status"] in ("complete", "needs_attention"):
        return "done", last.get("message", ""), run_id
    if result.get("stopped"):
        return "stopped", "Stopped.", run_id
    return ("signin" if last.get("code") == "sign_in" else "failed"), last.get("message", ""), run_id


# ------------------------------------------------------------- DMP reads
def dmp_sessions(run_id: int) -> list[dict]:
    """The engine's own index of practice sets (src/dmp/store.py), read as JSON."""
    try:
        rows = json.loads((copy_dir(run_id) / "dmp" / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def dmp_session(run_id: int, session_id: str) -> dict | None:
    if not session_id or not session_id.startswith("dmp_") or not session_id.replace("_", "").isalnum():
        return None
    try:
        value = json.loads((copy_dir(run_id) / "dmp" / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def dmp_sheets(run_id: int) -> list[Path]:
    folder = copy_dir(run_id) / "dmp" / "sheets"
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".docx", ".pdf")),
                  key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []


def file_path(run_id: int, area: str, name: str) -> Path | None:
    """A file this practice set made, by its area and name; None for anything else."""
    if area == "sheets":
        found = {p.name: p for p in dmp_sheets(run_id)}
    elif area == "dpp":
        found = {Path(p).name: Path(p) for p in (state(run_id).get("exports") or {}).values()}
    else:
        return None
    path = found.get(name)
    root = make_service.run_dir(run_id).resolve()
    return path if path is not None and path.is_file() and root in path.resolve().parents else None


job_queue.register("practice", launch)
