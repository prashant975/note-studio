"""Phase 4 check 1: queue 3 PDFs and 1 sheet run. The two lanes work side by
side, each in order; Stop holds the queue; Continue goes on; a restart stops
what was running and holds what waits. Stubbed model and Google: no cost."""
from __future__ import annotations

import json
import os
import re
import threading
import time

from note_studio import automation_service as auto
from note_studio import db, job_queue, make_service, old_app, signin
from tests.test_automation import SHEETS, FakeAuth, fake_run_targets

NOTES = {"teacher_name": "Saleem Ahmad Sir", "logo_watermark": True}


def _sign_in_fake(tmp_path, monkeypatch):
    folder = tmp_path / "old_app"
    folder.mkdir(exist_ok=True)
    (folder / "sheets.json").write_text(json.dumps(SHEETS), encoding="utf-8")
    old_app.adopt_default()
    for key, value in old_app.environment().items():
        os.environ[key] = value
    monkeypatch.setattr(signin, "_state", {"auth": FakeAuth(), "folder": old_app.state_dir()})
    monkeypatch.setattr(auto, "_state", {"controller": None, "auth": None, "run_id": None, "check": None,
                                         "old_app": (0.0, False)})
    from sheets_bridge.webui import RunController

    monkeypatch.setattr(RunController, "_check_model", lambda self, model: None)


def test_three_pdfs_and_a_sheet_run_side_by_side_then_stop_and_continue(client, fake_model, lecture_pdf,
                                                                         tmp_path, monkeypatch):
    _sign_in_fake(tmp_path, monkeypatch)
    gate = threading.Event()
    sheet_calls: list = []
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets(sheet_calls, wait=gate, rows=(5, 6)))
    job_queue.hold("paused")                                    # add everything first, then let it go
    make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto", NOTES,
                        [("A.pdf", lecture_pdf), ("B.pdf", lecture_pdf), ("C.pdf", lecture_pdf)])
    auto.enqueue({"dryRun": False, "profiles": ["Lakshya JEE 2027"], "subject": "sheet", "category": "neet_jee",
                  "showPageNumbers": True, "limit": None, "row": None, "redo": False, "maxCost": None,
                  "sanskritStyle": "hindi", "model": ""})
    page = client.get("/queue").text
    assert len(re.findall(r'action="/queue/\d+/remove"', page)) == 4 and "You paused the queue" in page

    client.post("/queue/continue")
    # the sheet run waits at its gate while the Notes lane makes A, B and C, in order
    deadline = time.time() + 120
    while time.time() < deadline and len(db.query("SELECT id FROM runs WHERE source='upload' AND state='complete'")) < 3:
        time.sleep(0.1)
    notes = db.query("SELECT title, state FROM runs WHERE source='upload' ORDER BY id")
    assert [(n["title"], n["state"]) for n in notes] == [("A", "complete"), ("B", "complete"), ("C", "complete")]
    assert auto.running()                                        # ... and the sheet run was going all along
    assert "Running" in client.get("/queue/card").text

    client.post("/automation/stop")                              # Stop holds the queue
    assert job_queue.held() == "stop"
    make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto", NOTES, [("D.pdf", lecture_pdf)])
    gate.set()
    job_queue.wait_idle(30)
    assert db.query("SELECT state FROM job_queue WHERE title='D'")[0]["state"] == "waiting"   # not over a Stop
    sheet = db.query("SELECT * FROM runs WHERE source='sheet'")[0]
    assert sheet["state"] == "needs_attention" and "not reached" in sheet["message"]
    start_another = client.get(f"/runs/{sheet['id']}").text.split("Start another")[0].rsplit("<a ", 1)[1]
    assert 'href="/automation"' in start_another                     # back to the sheets, not New notes

    client.post("/queue/continue")
    assert job_queue.wait_idle(120)
    assert db.query("SELECT state FROM runs WHERE title='D'")[0]["state"] == "complete"
    assert [e["state"] for e in job_queue.finished()].count("done") == 5


def test_a_restart_stops_what_ran_and_holds_what_waits(client, lecture_pdf):
    job_queue.hold("paused")
    added = make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto", NOTES,
                                [("A.pdf", lecture_pdf), ("B.pdf", lecture_pdf)])
    first = added[0][0]
    db.execute("UPDATE job_queue SET state='running' WHERE id=?", (first,))      # A was being made ...
    db.recover()                                                                 # ... when NoteCraft closed
    # (the "paused" hold stays until recover() replaces it, so the pump cannot start B in between)
    assert job_queue.get(first)["state"] == "stopped"                            # never paid for twice
    assert job_queue.held() == "restart" and job_queue.position(added[1][0]) == 0
    assert "closed and opened again" in client.get("/queue").text


def test_the_strip_shows_on_the_task_pages(client, lecture_pdf):
    job_queue.hold("paused")
    make_service.submit("neet_jee", "concise_handdrawn", "auto", "auto", NOTES, [("A.pdf", lecture_pdf)])
    for path in ("/new", "/automation"):
        page = client.get(path).text
        assert "Smart Queue: 1 waiting" in page and "holding" in page, path
