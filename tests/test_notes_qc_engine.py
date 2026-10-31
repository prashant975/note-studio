"""Phase 8: Check notes (the UPSC file QC tool) as a child process. App-path
parity with the original (basic checks: no AI, no network), checks from a run
page and from an upload, the full check's sign-in, and the PW-only rule.

The original is only read: its own Python and code run with bytecode writing
off, and every file they write goes into tmp_path."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from note_studio import check_service, db, engine_catalog, engine_host, job_queue, make_service
from note_studio.engines import notes_qc as qc_engine

ROOT = Path(__file__).resolve().parents[1]
COPY = engine_catalog.engine_dir("notes_qc")
ORIGINAL = Path.home() / "Downloads" / "upsc_file_QC"
SAMPLE = COPY / "testdata" / "probity_governance_english.pdf"
needs_engine = pytest.mark.skipif(not os.environ.get("NOTE_STUDIO_NOTES_QC_PYTHON") or not SAMPLE.is_file(),
                                  reason="the QC tool's own Python or its test PDF is not on this computer")
needs_original = pytest.mark.skipif(not (ORIGINAL / "app.py").is_file(), reason="the original QC tool is not here")


def _env() -> dict:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    env.pop("NOTE_STUDIO_PW_TOKEN", None)
    return env


def _rows(workbook: Path) -> list:
    from openpyxl import load_workbook

    book = load_workbook(workbook, read_only=True)
    return {sheet.title: [tuple(row) for row in sheet.iter_rows(values_only=True)] for sheet in book.worksheets}


def _result(run_dir: Path) -> dict:
    found = next(run_dir.rglob("qc_result.json"))
    value = json.loads(found.read_text(encoding="utf-8"))
    text = json.dumps(value, sort_keys=True).replace(str(run_dir).replace("\\", "\\\\"), "<RUN>")
    value = json.loads(text)
    for key in ("run_dir", "started_at", "finished_at", "elapsed_seconds", "seconds"):
        value.pop(key, None)
    return value


@needs_engine
@needs_original
def test_a_basic_check_is_the_old_tools_check(tmp_path):
    old_out = tmp_path / "old"
    old_out.mkdir()
    ran = subprocess.run([os.environ["NOTE_STUDIO_NOTES_QC_PYTHON"], str(ROOT / "tests" / "parity" / "notes_qc_app_driver.py"),
                          str(ORIGINAL), str(old_out), str(SAMPLE)], capture_output=True, text=True, encoding="utf-8",
                         env=_env(), cwd=str(old_out), timeout=600)
    assert ran.returncode == 0, ran.stderr[-3000:]
    old = json.loads(ran.stdout.strip().splitlines()[-1])
    new_out = tmp_path / "new"
    events = []
    result = engine_host.run("notes_qc", 1, new_out, qc_engine.build_request([SAMPLE], full=False), events.append)
    assert result["status"] == "complete", result
    done = next(e for e in events if e["event"] == "done")
    workbook = next(e["path"] for e in events if e["event"] == "output" and e["label"] == "Error report")
    assert _rows(Path(workbook)) == _rows(Path(old["workbook"]))              # the same report, row for row
    assert _result(Path(done["run_dir"])) == _result(Path(old["run_dir"]))
    assert "basic checks done" in done["reason"]


@needs_engine
def test_the_direct_gemini_key_is_never_used(tmp_path):
    """The user's decision: PW proxy only. engine_main drops the key and the lookup."""
    code = ("import json, sys; from pathlib import Path; import engine_main as m; "
            "m.configure({}, Path(sys.argv[1])); import os; from src import grounded_lookup as g; "
            "before = g._api_key(); g._api_key = lambda: ''; "
            "print(json.dumps({'env': 'GEMINI_API_KEY' in os.environ or 'GOOGLE_API_KEY' in os.environ, 'key': before}))")
    out = subprocess.run([os.environ["NOTE_STUDIO_NOTES_QC_PYTHON"], "-c", code, str(tmp_path)], cwd=str(COPY),
                         capture_output=True, text=True, encoding="utf-8", timeout=120,
                         env={**_env(), "GEMINI_API_KEY": "leak-me", "GOOGLE_API_KEY": "leak-me-too"})
    assert out.returncode == 0, out.stderr[-1500:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {"env": False, "key": ""}
    main = (COPY / "engine_main.py").read_text(encoding="utf-8")
    assert 'grounded_lookup._api_key = lambda: ""' in main                   # also patched for real runs


@needs_engine
def test_check_an_uploaded_file_basic(client):
    response = client.post("/check", data={"kind": "other"},
                           files=[("file", (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf"))], follow_redirects=False)
    assert response.headers["location"].startswith("/queue")
    assert job_queue.wait_idle(300)
    run = db.query("SELECT * FROM runs WHERE source='check'")[0]
    assert run["state"] == "complete" and "basic checks done" in run["message"] and run["cost_inr"] == 0
    page = client.get(f"/runs/{run['id']}").text
    assert "Excel workbook" in page and "Check again" in page
    link = re.search(rf'href="(/runs/{run["id"]}/files/\d+)"', page).group(1)
    assert client.get(link).content.startswith(b"PK")
    assert "Basic" in client.get("/check").text


@needs_engine
def test_check_these_notes_from_a_finished_run(client, monkeypatch, lecture_pdf):
    """A NEET-JEE run (PW Books, mock) gets the basic checks from its own page."""
    from note_studio.engines import pw_books as pw_books_engine

    monkeypatch.setattr(make_service, "class_token", lambda: "")
    real = pw_books_engine.make_notes
    monkeypatch.setattr(pw_books_engine, "make_notes", lambda run_id, folder, request, **kw: real(
        run_id, folder, {**request, "mock": True, "test_stub_pdf": True}, **kw))
    client.post("/new", data={"category": "neet_jee", "note_type": "pw_books_handwritten", "subject": "physics",
                              "language": "english", "mode": "summary", "exam": "", "math_render_mode": "omml",
                              "strict_math": "on"}, files=[("files", ("Physics 05.pdf", lecture_pdf, "application/pdf"))])
    assert job_queue.wait_idle(300)
    made = db.query("SELECT * FROM runs WHERE engine='pw_books'")[0]
    page = client.get(f"/runs/{made['id']}").text
    assert f'action="/runs/{made["id"]}/check"' in page and "No AI, no cost" in page
    client.post(f"/runs/{made['id']}/check")
    assert job_queue.wait_idle(300)
    check = db.query("SELECT * FROM runs WHERE source='check'")[0]
    assert check["state"] == "complete", (check["message"], check["error"])
    assert check["title"].startswith("Check: ")
    copied = Path(check["run_dir"]) / "input"
    assert [p.suffix for p in copied.iterdir()] == [".docx"]                  # the run's Word file, copied in


def test_a_full_check_without_sign_in_holds_the_queue(client):
    client.post("/check", data={"kind": "upsc"}, files=[("file", ("Notes.pdf", b"%PDF-1.4 tiny", "application/pdf"))])
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs WHERE source='check'")[0]
    assert run["state"] == "failed" and "Settings" in run["error"]
    assert job_queue.held() == "signin"
    assert not (Path(run["run_dir"]) / "request.json").exists()


@pytest.mark.parametrize("name, data, words", [
    ("notes.exe", b"MZ", "cannot be checked"),
    ("notes.pdf", b"", "Choose the notes file"),
])
def test_upload_problems_are_said_in_plain_words(client, name, data, words):
    response = client.post("/check", data={"kind": "other"}, files=[("file", (name, data, "application/octet-stream"))],
                           follow_redirects=False)
    assert "kind=warn" in response.headers["location"] and words.replace(" ", "%20") in response.headers["location"]
    assert check_service.recent() == []


def test_settings_offers_the_check_model(client):
    assert "Check notes" in client.get("/settings/ai").text
    assert client.get("/check").status_code == 200
