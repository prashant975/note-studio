"""The database lives in NoteCraft's data folder, survives restarts, and a
restart never silently resumes work."""
from __future__ import annotations

import time

from note_studio import db, paths


def test_init_makes_every_table_in_wal_mode():
    db.init()
    assert paths.db_path().parent == paths.home()
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "runs", "run_items", "run_events", "job_queue"} <= names
    con = db.connect()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_settings_round_trip_and_default():
    db.init()
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("teacher", "Saleem Ahmad Sir")
    db.set_setting("teacher", "Another Name")
    assert db.get_setting("teacher") == "Another Name"


def test_a_setting_read_before_init_is_the_default():
    assert db.get_setting("anything", "x") == "x"


def test_recover_stops_running_work_and_holds_the_queue():
    db.init()
    running = db.create_run("shortnotes", title="Lecture 1")
    finished = db.create_run("shortnotes", title="Lecture 0")
    db.execute("UPDATE runs SET state='running', pid=1234, started_at=? WHERE id=?", (time.time(), running))
    db.execute("UPDATE runs SET state='complete', finished_at=? WHERE id=?", (time.time(), finished))
    db.execute("INSERT INTO job_queue (kind, state, created_at) VALUES ('notes', 'running', ?)", (time.time(),))
    db.execute("INSERT INTO job_queue (kind, state, created_at) VALUES ('notes', 'waiting', ?)", (time.time(),))

    result = db.recover()

    # The item that was running is stopped, never started again by itself (it
    # could pay for a lecture twice); the waiting one holds for Continue.
    assert result == {"stopped_runs": 1, "interrupted": 1, "waiting": 1}
    rows = {r["id"]: r for r in db.query("SELECT * FROM runs")}
    assert rows[running]["state"] == "stopped" and rows[running]["pid"] is None
    assert "closed while this was running" in rows[running]["error"]
    assert rows[finished]["state"] == "complete"
    assert [r["state"] for r in db.query("SELECT state FROM job_queue ORDER BY id")] == ["stopped", "waiting"]
    assert db.queue_counts() == {"waiting": 1, "running": 0, "hold": "restart"}
    assert db.attention_runs()[0]["id"] == running


def test_recover_with_nothing_to_do_does_not_hold():
    db.init()
    assert db.recover() == {"stopped_runs": 0, "interrupted": 0, "waiting": 0}
    assert db.get_setting("queue_hold") == ""
