"""The child-process host (engine_host.py), with a small fake engine: events,
the token only in the child's environment, its own TEMP, a crash, and Stop
ending the whole process tree (grandchildren too)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from note_studio import engine_catalog, engine_host

FAKE_MAIN = r'''
import hashlib, json, os, subprocess, sys, time
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
request = json.load(open(args["--request"], encoding="utf-8"))
def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)
emit("step", text="Started", pct=5)
token = os.environ.get("NOTE_STUDIO_PW_TOKEN", "")
emit("step", text="token=" + ("yes" if hashlib.sha256(token.encode()).hexdigest() == request.get("token_sha") else "no"))
emit("step", text="private=" + ("leaked" if os.environ.get("NOTE_STUDIO_SECRET_SETTING") else "kept"))
emit("step", text="temp=" + os.environ["TEMP"])
print("a plain line", flush=True)
mode = request.get("mode")
if mode == "crash":
    raise SystemExit(3)
if mode == "wait":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    emit("step", text="grandchild=%d" % child.pid)
    time.sleep(120)
emit("output", path="x.docx", kind="docx", label="Notes")
emit("done", status="complete", reason="")
'''


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    folder = tmp_path / "engines" / "fake"
    folder.mkdir(parents=True)
    (folder / "engine_main.py").write_text(FAKE_MAIN, encoding="utf-8")
    real = engine_catalog.engine_dir
    monkeypatch.setattr(engine_catalog, "engine_dir", lambda engine_id: folder if engine_id == "fake" else real(engine_id))
    monkeypatch.setenv("NOTE_STUDIO_FAKE_PYTHON", sys.executable)
    monkeypatch.setenv("NOTE_STUDIO_SECRET_SETTING", "do-not-pass")
    return folder


def test_events_token_and_own_temp(fake_engine, tmp_path, monkeypatch):
    from note_studio import signin

    monkeypatch.setattr(signin, "snapshot", lambda: {"email": "tester@pw.live"})
    assert engine_host.child_env(tmp_path / "check", "tok")["NOTE_STUDIO_PW_ACCOUNT"] == "tester@pw.live"
    events = []
    run_dir = tmp_path / "run"
    request = {"token_sha": hashlib.sha256(b"tok-123").hexdigest()}
    result = engine_host.run("fake", 1, run_dir, request, events.append, token="tok-123")
    texts = [e.get("text") for e in events if e["event"] == "step"]
    assert result["status"] == "complete" and result["returncode"] == 0
    assert "token=yes" in texts and "private=kept" in texts
    assert f"temp={run_dir / 'tmp'}" in texts
    assert {"event": "log", "text": "a plain line"} in events
    assert [e for e in events if e["event"] == "output"][0]["kind"] == "docx"
    assert json.loads((run_dir / "request.json").read_text(encoding="utf-8")) == request
    assert "tok-123" not in (run_dir / "request.json").read_text(encoding="utf-8")       # never on disk


def test_a_crash_is_a_failed_run_with_its_last_lines(fake_engine, tmp_path):
    result = engine_host.run("fake", 2, tmp_path / "run", {"mode": "crash"}, lambda e: None)
    assert result["status"] == "failed" and result["returncode"] == 3
    assert "a plain line" in result["tail"]


def test_stop_ends_the_child_and_what_it_started(fake_engine, tmp_path):
    events, box = [], {}
    worker = threading.Thread(target=lambda: box.update(result=engine_host.run(
        "fake", 3, tmp_path / "run", {"mode": "wait"}, events.append)))
    worker.start()
    deadline = time.time() + 30
    grandchild = None
    while time.time() < deadline and grandchild is None:
        grandchild = next((int(e["text"].split("=")[1]) for e in list(events)
                           if str(e.get("text", "")).startswith("grandchild=")), None)
        time.sleep(0.1)
    assert grandchild and engine_host.running(3)
    assert engine_host.stop(3)
    worker.join(30)
    assert box["result"]["status"] == "stopped" and box["result"]["stopped"]
    time.sleep(1)
    assert not _alive(grandchild)
    assert not engine_host.running(3) and engine_host.stop(3) is False


def test_the_upsc_engine_runs_on_a_python_that_has_its_packages():
    python, where = engine_host.runtime("upsc")
    assert os.path.isfile(python) and where
