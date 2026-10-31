"""Smart Queue: everything waits here and runs by itself (adapted from QBG One).

What waits is the request: its settings in the database and its files in
~/.note_studio/queue/<id>/. Nothing else exists for it until its turn, so
removing it leaves nothing behind. When its turn comes, its launcher does what
the page's own button does, and the run appears in Runs.

- Two lanes, Notes (New notes) and Sheets (sheet automation). Each runs one
  item at a time, in the order added; the two lanes run side by side (each
  run has its own AI model, so they do not get in each other's way).
- Stop on a sheet run, a lost sign-in, and a NoteCraft restart hold the queue:
  nothing new starts until the operator presses Continue. A failed item does
  not hold it; the line moves on.
- The same request twice in a row (a double click) is added once.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import db, paths

log = logging.getLogger("note_studio.queue")
LANES = {"notes": "Notes", "sheets": "Sheets"}
KIND_LANE = {"new_notes": "notes", "sheet_run": "sheets", "practice": "notes", "check": "notes"}
HOLD_KEY = "queue_hold"
HOLD_WORDS = {
    "update": "The queue is waiting while NoteCraft installs its update.",
    "restart": "NoteCraft was closed and opened again. Nothing starts by itself after that.",
    "stop": "A run was stopped. The next item waits until you continue.",
    "signin": "The Google sign-in is missing or no longer works. Sign in, then continue.",
    "paused": "You paused the queue. What was running finishes; nothing new starts.",
}
PUMP_SECONDS = 2.0
OPEN = ("waiting", "running")

LAUNCHERS: dict = {}          # kind -> launch(entry) -> (state, note, run_id); set by the services
_lock = threading.RLock()
_state: dict = {"pump": None, "lane_threads": {}, "wake": threading.Event()}


def register(kind: str, launch) -> None:
    LAUNCHERS[kind] = launch


# ---------------------------------------------------------------- folders
def folder(entry_id: int) -> Path:
    path = paths.home() / "queue" / str(int(entry_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def files(entry: dict) -> list[Path]:
    path = paths.home() / "queue" / str(int(entry["id"]))
    return sorted(p for p in path.glob("*") if p.is_file()) if path.is_dir() else []


def _drop_files(entry_id: int) -> None:
    shutil.rmtree(paths.home() / "queue" / str(int(entry_id)), ignore_errors=True)


# ---------------------------------------------------------------- reading
def get(entry_id: int) -> dict | None:
    rows = db.query("SELECT * FROM job_queue WHERE id=?", (entry_id,))
    return _decode(rows[0]) if rows else None


def _decode(row: dict) -> dict:
    row = dict(row)
    row["payload"] = json.loads(row.get("payload_json") or "{}")
    return row


def entries(states=OPEN, lane: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT * FROM job_queue WHERE state IN ({','.join('?' * len(states))})"
    args = list(states)
    if lane:
        sql += " AND lane=?"
        args.append(lane)
    return [_decode(r) for r in db.query(sql + " ORDER BY id LIMIT ?", tuple(args) + (limit,))]


def finished(limit: int = 20) -> list[dict]:
    return [_decode(r) for r in db.query(
        "SELECT * FROM job_queue WHERE state NOT IN ('waiting','running') ORDER BY COALESCE(finished_at, created_at) "
        "DESC LIMIT ?", (limit,))]


def position(entry_id: int) -> int:
    """How many items are ahead of this one in its lane (0 = next)."""
    entry = get(entry_id)
    if not entry or entry["state"] != "waiting":
        return 0
    rows = db.query("SELECT COUNT(*) AS n FROM job_queue WHERE lane=? AND state IN ('waiting','running') AND id<?",
                    (entry["lane"], entry_id))
    return rows[0]["n"]


def counts() -> dict:
    rows = db.query("SELECT lane, state, COUNT(*) AS n FROM job_queue WHERE state IN ('waiting','running') "
                    "GROUP BY lane, state")
    out = {"waiting": 0, "running": 0, "lanes": {lane: {"waiting": 0, "running": 0} for lane in LANES}}
    for row in rows:
        out[row["state"]] += row["n"]
        out["lanes"].setdefault(row["lane"], {"waiting": 0, "running": 0})[row["state"]] += row["n"]
    out["hold"] = held()
    return out


# ------------------------------------------------------------------ holds
def held() -> str:
    return db.get_setting(HOLD_KEY)


def hold(reason: str) -> None:
    db.set_setting(HOLD_KEY, reason)


def resume() -> None:
    db.set_setting(HOLD_KEY, "")
    wake()


def hold_words(reason: str | None = None) -> str:
    reason = held() if reason is None else reason
    return HOLD_WORDS.get(reason, "The queue is holding." if reason else "")


# ---------------------------------------------------------------- changes
def submit(kind: str, title: str, payload: dict, uploads=(), key: str = "") -> tuple[int, bool]:
    """Add a request. Returns (entry id, created); a same-key request already
    waiting or running is returned instead of adding a second one."""
    lane = KIND_LANE[kind]
    with _lock:
        if key:
            same = db.query("SELECT id FROM job_queue WHERE key=? AND state IN ('waiting','running') LIMIT 1", (key,))
            if same:
                return same[0]["id"], False
        entry_id = db.execute(
            "INSERT INTO job_queue (lane, kind, title, key, state, payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (lane, kind, title[:200], key, "waiting", json.dumps(payload, ensure_ascii=False), time.time()))
        where = folder(entry_id) if uploads else None
        for name, data in uploads:
            (where / name).write_bytes(data)
    wake()
    return entry_id, True


def remove(entry_id: int) -> bool:
    """Take a waiting item out. Nothing was started for it, so nothing is left."""
    changed = db.execute("UPDATE job_queue SET state='removed', finished_at=?, note=? WHERE id=? AND state='waiting'",
                         (time.time(), "Removed before it started", entry_id))
    if changed:
        _drop_files(entry_id)
    return bool(changed)


def _finish(entry_id: int, state: str, note: str = "", run_id: int | None = None) -> None:
    db.execute("UPDATE job_queue SET state=?, note=?, run_id=COALESCE(?, run_id), finished_at=? WHERE id=?",
               (state, note[:300], run_id, time.time(), entry_id))
    _drop_files(entry_id)


# ------------------------------------------------------------------- pump
def _claim(lane: str) -> dict | None:
    """The lane's next item, marked running; None when the lane is busy, empty or held."""
    with _lock:
        if held():
            return None
        if db.query("SELECT id FROM job_queue WHERE lane=? AND state='running' LIMIT 1", (lane,)):
            return None
        rows = db.query("SELECT * FROM job_queue WHERE lane=? AND state='waiting' ORDER BY id LIMIT 1", (lane,))
        if not rows:
            return None
        db.execute("UPDATE job_queue SET state='running', started_at=? WHERE id=?", (time.time(), rows[0]["id"]))
        return get(rows[0]["id"])


def _run(entry: dict) -> None:
    launch = LAUNCHERS.get(entry["kind"])
    try:
        if launch is None:
            raise RuntimeError(f"no launcher for {entry['kind']}")
        state, note, run_id = launch(entry)
    except Exception as exc:
        log.exception("queue item %s", entry["id"])
        state, note, run_id = "failed", f"Could not start ({type(exc).__name__}).", None
    # A launcher answers "signin" when the sign-in is the problem: the item did
    # not finish, and the next ones would only fail the same way.
    lost_sign_in = state == "signin"
    _finish(entry["id"], "failed" if lost_sign_in else state, note, run_id)
    if lost_sign_in:
        hold("signin")
    wake()


def tick() -> None:
    """Start the head of every free lane."""
    for lane in LANES:
        thread = _state["lane_threads"].get(lane)
        if thread is not None and thread.is_alive():
            continue
        entry = _claim(lane)
        if entry is None:
            continue
        thread = threading.Thread(target=_run, args=(entry,), name=f"queue-{lane}-{entry['id']}", daemon=True)
        _state["lane_threads"][lane] = thread
        thread.start()


def wake() -> None:
    """Let the pump look now (started on first use)."""
    start_pump()
    _state["wake"].set()


def start_pump() -> None:
    with _lock:
        pump = _state["pump"]
        if pump is not None and pump.is_alive():
            return

        def loop():
            while True:
                _state["wake"].wait(PUMP_SECONDS)
                _state["wake"].clear()
                try:
                    tick()
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):         # a data folder not prepared yet: next time
                        log.exception("queue pump")
                except Exception:
                    log.exception("queue pump")

        pump = threading.Thread(target=loop, name="queue-pump", daemon=True)
        _state["pump"] = pump
        pump.start()


def wait_idle(timeout: float = 120) -> bool:
    """For tests: wait until nothing is running and nothing that may start is waiting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = any(t is not None and t.is_alive() for t in _state["lane_threads"].values())
        startable = not held() and db.query("SELECT id FROM job_queue WHERE state IN ('waiting','running') LIMIT 1")
        if not busy and not startable:
            return True
        wake()
        time.sleep(0.05)
    return False
