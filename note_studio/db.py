"""SQLite (WAL) at ~/.note_studio/note_studio.db: settings, runs, the Smart Queue.

Every function opens its own short connection, so threads never share one.
Nothing here makes a network call; every page reads it.

Run states (one list for every engine):
    waiting -> running -> complete | needs_attention | failed | stopped
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import paths
from .brand import APP_NAME as APP

RUN_STATES = ("waiting", "running", "complete", "needs_attention", "failed", "stopped")
ACTIVE_STATES = ("waiting", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    engine       TEXT NOT NULL,
    note_type    TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    language     TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'upload',
    state        TEXT NOT NULL DEFAULT 'waiting',
    pct          INTEGER NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    cost_inr     REAL NOT NULL DEFAULT 0,
    run_dir      TEXT NOT NULL DEFAULT '',
    pid          INTEGER,
    queue_id     INTEGER,
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);

CREATE TABLE IF NOT EXISTS run_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    position    INTEGER NOT NULL DEFAULT 0,
    label       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'waiting',
    message     TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    cost_inr    REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS run_items_run ON run_items(run_id);

CREATE TABLE IF NOT EXISTS run_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    at        REAL NOT NULL,
    kind      TEXT NOT NULL,              -- step | cost | warning | output | done | log
    text      TEXT NOT NULL DEFAULT '',
    pct       INTEGER,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id);

CREATE TABLE IF NOT EXISTS job_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lane         TEXT NOT NULL DEFAULT 'notes',   -- notes | sheets
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    key          TEXT NOT NULL DEFAULT '',        -- de-duplicates a double click
    state        TEXT NOT NULL DEFAULT 'waiting', -- waiting | running | done | failed | removed
    payload_json TEXT NOT NULL DEFAULT '{}',
    run_id       INTEGER,
    hold_reason  TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS job_queue_state ON job_queue(state);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(paths.db_path(), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    # Two connections turning a brand-new file to WAL at the same moment: one
    # gets "locked" at once (the busy timeout does not cover it). The other has
    # just done it, so a short wait is all that is needed.
    for attempt in range(60):
        try:
            con.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 59:
                con.close()
                raise
            time.sleep(0.05)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        # Columns added after the first release; ADD COLUMN is idempotent only by hand.
        queue_columns = {r["name"] for r in con.execute("PRAGMA table_info(job_queue)")}
        if "note" not in queue_columns:
            con.execute("ALTER TABLE job_queue ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    finally:
        con.close()


def query(sql: str, args: tuple = ()) -> list[dict]:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def execute(sql: str, args: tuple = ()) -> int:
    """Run one write; returns the new row id (or the row count for an UPDATE)."""
    con = connect()
    try:
        cur = con.execute(sql, args)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
    finally:
        con.close()


# ------------------------------------------------------------------ settings
def get_setting(key: str, default: str = "") -> str:
    try:
        rows = query("SELECT value FROM settings WHERE key=?", (key,))
    except sqlite3.OperationalError:
        return default            # table not made yet: nothing was ever saved
    return rows[0]["value"] if rows and rows[0]["value"] else default


def set_setting(key: str, value: object) -> None:
    execute("INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value or "")))


# ---------------------------------------------------------------------- runs
def create_run(engine: str, *, title: str = "", note_type: str = "", category: str = "",
               subject: str = "", language: str = "", source: str = "upload",
               options: dict | None = None) -> int:
    return execute(
        "INSERT INTO runs (engine, note_type, category, subject, language, title, source,"
        " options_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (engine, note_type, category, subject, language, title, source,
         json.dumps(options or {}, ensure_ascii=False), time.time()))


def active_runs() -> list[dict]:
    return query("SELECT * FROM runs WHERE state IN ('waiting','running') ORDER BY id")


def recent_runs(limit: int = 8) -> list[dict]:
    return query("SELECT * FROM runs WHERE state NOT IN ('waiting','running') "
                 "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT ?", (limit,))


def attention_runs(limit: int = 6) -> list[dict]:
    return query("SELECT * FROM runs WHERE state IN ('needs_attention','failed','stopped') "
                 "AND COALESCE(finished_at, created_at) > ? ORDER BY id DESC LIMIT ?",
                 (time.time() - 7 * 86400, limit))


def queue_counts() -> dict:
    rows = query("SELECT state, COUNT(*) AS n FROM job_queue "
                 "WHERE state IN ('waiting','running') GROUP BY state")
    counts = {"waiting": 0, "running": 0}
    counts.update({r["state"]: r["n"] for r in rows})
    counts["hold"] = get_setting("queue_hold")
    return counts


# ------------------------------------------------------------------ recovery
STOPPED_BY_RESTART = (f"{APP} was closed while this was running. Nothing more was done; "
                      "start it again when you are ready.")


def recover() -> dict:
    """After a restart: a run that was running is stopped, and so is its queue
    item (never silently resumed: that could pay for a lecture twice). Items
    still waiting hold until the operator presses Continue."""
    now = time.time()
    con = connect()
    try:
        # One transaction: a queue pump already running must never see the running
        # item stopped before the hold is set, or it would start the next one.
        con.execute("BEGIN IMMEDIATE")
        stopped = con.execute(
            "UPDATE runs SET state='stopped', error=?, finished_at=?, pid=NULL "
            "WHERE state='running'", (STOPPED_BY_RESTART, now)).rowcount
        interrupted = con.execute(
            "UPDATE job_queue SET state='stopped', note=?, finished_at=? WHERE state='running'",
            (STOPPED_BY_RESTART, now)).rowcount
        waiting = con.execute("SELECT COUNT(*) FROM job_queue WHERE state='waiting'").fetchone()[0]
        if waiting:
            con.execute("INSERT INTO settings (key, value) VALUES ('queue_hold', 'restart') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        else:
            # The install hold protects the old process while it exits. Once
            # this process has restarted with no pending work, it must not
            # leave future submissions paused forever. Other holds are kept.
            con.execute("UPDATE settings SET value='' WHERE key='queue_hold' AND value='update'")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"stopped_runs": stopped, "interrupted": interrupted, "waiting": waiting}
