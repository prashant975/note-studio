"""Sheet automation through the old app's own RunController and run API, with
Google and the AI replaced by fakes: options, live progress, history that
survives a restart, Stop, problems in plain words, the checks, and one notes
maker at a time."""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from note_studio import automation_service as auto
from note_studio import db, job_queue, old_app, sheet_list, signin

SHEETS = {"profiles": [
    {"name": "Lakshya JEE 2027", "spreadsheetId": "18ZcAOO_Y0j-abcdefghij", "gid": 0},
    {"name": "Pre Foundation", "spreadsheetId": "17NLSXUu4smmS33okDzz", "gid": 4},
    {"name": "Old Sheet", "spreadsheetId": "1N6juvFsmHgLd8NfzWSU", "gid": 9, "enabled": False},
]}


class FakeAccess:
    email = "operator@pw.live"

    def install_transport(self, model=""):
        return model or "gemini-3.7-flash"


class FakeAccount:
    email = "operator@pw.live"
    access = FakeAccess()
    sheets_service = object()
    drive_service = object()

    def fresh_services(self):
        return object(), object()


class FakeAuth:
    """Stands in for sheets_bridge.auth_session.AutomationAuth: signed in, allowed."""
    signed_out = False

    def __init__(self):
        self._lock = threading.Lock()
        self._account, self._state, self._error, self._restored = FakeAccount(), "ready", "", True

    def account(self, owner=""):
        return self._account

    def require(self, owner=""):
        return self._account

    def status(self, owner=""):
        return {"state": "ready", "signedIn": True, "email": self._account.email}


@pytest.fixture
def setup(client, tmp_path, monkeypatch):
    """The shared folder with 3 sheets, signed in (fake), and no real model check."""
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
    yield folder
    current = auto._state["controller"]
    if current is not None:
        current.stop()
        current.wait(30)
    for _ in range(100):
        if auto.current_run_id() is None:
            break
        time.sleep(0.05)


def fake_run_targets(calls: list, *, rows=(5, 6, 7), wait: threading.Event | None = None, fail_row=None,
                     raise_code: str = ""):
    """A run_targets that reports like the real one (progress per row, then a BatchSummary)."""
    def run_targets(targets, sheets_service, drive_service, options, *, access=None, out=None, stop_check=None,
                    operator="", batch_id=None, on_progress=None):
        from sheets_bridge.batch import BatchSummary, TargetResult
        from sheets_bridge.errors import BridgeError
        from sheets_bridge.runner import RowOutcome, RunSummary

        calls.append({"targets": [t.name for t in targets], "options": options, "operator": operator})
        if raise_code:
            raise BridgeError(raise_code, "fake")
        summary = RunSummary(run_id="run-1")
        for position, row in enumerate(rows, 1):
            if wait is not None:
                wait.wait(10)
            if stop_check and stop_check():
                summary.outcomes.append(RowOutcome(row, "stopped", "stop_requested"))
                summary.interrupted = True
                continue
            if options.dry_run:
                outcome = RowOutcome(row, "planned", note="would generate")
            elif row == fail_row:
                outcome = RowOutcome(row, "failed", "generate_failed")
            else:
                outcome = RowOutcome(row, "done", link=f"https://drive.google.com/file/d/notes-{row}", cost_inr=4.2)
                summary.total_cost_inr += 4.2
            summary.outcomes.append(outcome)
            out(f"row {row}: {outcome.outcome}")
            on_progress({"row": row, "outcome": outcome.outcome, "position": position, "total": len(rows),
                         "done": sum(o.outcome == "done" for o in summary.outcomes),
                         "failed": sum(o.outcome == "failed" for o in summary.outcomes),
                         "costInr": round(summary.total_cost_inr, 2), "sheet": targets[0].name})
        return BatchSummary("batch-1", [TargetResult(targets[0].name, targets[0].spreadsheet_id, targets[0].gid,
                                                     summary=summary)])
    return run_targets


def _wait_for_end(timeout=30):
    job_queue.wait_idle(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not auto.running() and auto.current_run_id() is None:
            return True
        time.sleep(0.05)
    return False


FORM = {"sheet": "Lakshya JEE 2027", "category": "neet_jee", "subject": "sheet", "model": "gemini-3.7-flash",
        "page_numbers": "on"}


def test_the_page_lists_the_shared_sheets(client, setup):
    page = client.get("/automation").text
    for words in ("Lakshya JEE 2027", "Pre Foundation", "Old Sheet", "Try it first", "Run now", "Check setup",
                  "Shared with the old Short Notes app"):
        assert words in page, words
    assert '<option value="__all__">All sheets (2)</option>' in page          # the switched-off sheet is not run


def test_a_run_goes_through_the_old_apps_controller_and_is_kept(client, setup, monkeypatch):
    calls = []
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets(calls, fail_row=6))
    response = client.post("/automation/run", data={**FORM, "mode": "run", "limit": "3", "max_cost": "250"},
                           follow_redirects=False)
    assert response.status_code == 303 and "kind=ok" in response.headers["location"]
    assert _wait_for_end()
    options = calls[0]["options"]
    assert calls[0]["targets"] == ["Lakshya JEE 2027"] and calls[0]["operator"] == "operator@pw.live"
    assert options.limit == 3 and options.max_cost_inr == 250 and options.category == "neet_jee"
    assert options.dry_run is False and options.show_page_numbers is True and options.subject_mode == "sheet"
    run = db.query("SELECT * FROM runs WHERE source='sheet'")[0]
    assert run["state"] == "needs_attention" and run["cost_inr"] == pytest.approx(8.4)
    assert run["message"].startswith("2 lectures made, 1 failed")
    items = db.query("SELECT * FROM run_items WHERE run_id=? ORDER BY position", (run["id"],))
    assert [(i["label"], i["state"]) for i in items] == [("Row 5", "done"), ("Row 6", "failed"), ("Row 7", "done")]
    assert items[0]["output_path"] == "https://drive.google.com/file/d/notes-5"
    page = client.get(f"/runs/{run['id']}").text
    assert "Open in Drive" in page and "Full log" in page and "row 6: failed" in page


def test_a_dry_run_changes_nothing_and_says_so(client, setup, monkeypatch):
    calls = []
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets(calls))
    client.post("/automation/run", data={**FORM, "mode": "dry"})
    assert _wait_for_end()
    assert calls[0]["options"].dry_run is True
    run = db.query("SELECT * FROM runs WHERE source='sheet'")[0]
    assert run["state"] == "complete" and run["title"].startswith("Dry run:")
    assert "3 rows would be made" in run["message"] and run["cost_inr"] == 0


def test_gujarati_batch_detection_works_in_the_real_controller_worker(client, setup, monkeypatch):
    from sheets_bridge import catalog, mapping

    calls, observed = [], []
    fake = fake_run_targets(calls)

    def run_targets(*args, **kwargs):
        observed.append((catalog.language_for_batch("Gujarat Board"), mapping.map_language("Gujarati batch")))
        return fake(*args, **kwargs)

    monkeypatch.setattr("sheets_bridge.batch.run_targets", run_targets)
    response = client.post("/automation/run", data={**FORM, "mode": "dry"}, follow_redirects=False)
    assert response.status_code == 303 and "kind=ok" in response.headers["location"]
    assert _wait_for_end()
    assert observed == [("gujarati", "gujarati")]
    assert catalog.language_for_batch("English batch") == "english"


def test_all_sheets_runs_every_switched_on_sheet(client, setup, monkeypatch):
    calls = []
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets(calls))
    client.post("/automation/run", data={**FORM, "sheet": "__all__", "mode": "dry"})
    assert _wait_for_end()
    assert calls[0]["targets"] == ["Lakshya JEE 2027", "Pre Foundation"]


def test_stop_after_this_row(client, setup, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets([], wait=gate))
    client.post("/automation/run", data={**FORM, "mode": "run"})
    for _ in range(200):
        if auto.current_run_id():
            break
        time.sleep(0.02)
    assert "Stop after this row" in client.get("/automation/live").text
    client.post("/automation/stop")
    assert job_queue.held() == "stop"                 # nothing new starts over the operator's Stop
    gate.set()
    assert _wait_for_end()
    run = db.query("SELECT * FROM runs WHERE source='sheet'")[0]
    assert run["state"] == "needs_attention" and "not reached" in run["message"]
    texts = [e["text"] for e in db.query("SELECT text FROM run_events WHERE run_id=?", (run["id"],))]
    assert any(t.startswith("Stop asked") for t in texts)


def test_a_problem_is_said_in_plain_words(client, setup, monkeypatch):
    monkeypatch.setattr("sheets_bridge.batch.run_targets", fake_run_targets([], raise_code="model_unavailable"))
    client.post("/automation/run", data={**FORM, "mode": "run"})
    assert _wait_for_end()
    run = db.query("SELECT * FROM runs WHERE source='sheet'")[0]
    assert run["state"] == "failed" and run["error"] == auto.CODE_WORDS["model_unavailable"]
    assert "Traceback" not in client.get(f"/runs/{run['id']}").text


def test_bad_options_are_refused_by_the_old_apps_own_checks(client, setup):
    response = client.post("/automation/run", data={**FORM, "mode": "run", "model": "gpt-made-up"},
                           follow_redirects=False)
    assert "kind=warn" in response.headers["location"] and "Choose%20a%20model" in response.headers["location"]
    assert db.query("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 0


def test_without_sign_in_nothing_starts(client, setup, monkeypatch):
    monkeypatch.setattr(signin, "_state", {"auth": None, "folder": None})
    ok, problem, _ = auto.start({"dryRun": True, "profiles": ["Lakshya JEE 2027"]})
    assert not ok and "Sign in" in problem


def test_the_same_sheet_run_twice_is_queued_once(client, setup, monkeypatch):
    job_queue.hold("paused")
    client.post("/automation/run", data={**FORM, "mode": "dry"})
    second = client.post("/automation/run", data={**FORM, "mode": "dry"}, follow_redirects=False)
    assert "already%20in%20the%20Smart%20Queue" in second.headers["location"]
    assert [e["lane"] for e in job_queue.entries()] == ["sheets"]


def test_a_sheet_run_left_running_is_stopped_after_a_restart(setup):
    run_id = db.create_run("notesbot", title="Sheet run: X", source="sheet")
    db.execute("UPDATE runs SET state='running' WHERE id=?", (run_id,))
    db.recover()
    assert db.query("SELECT state FROM runs WHERE id=?", (run_id,))[0]["state"] == "stopped"


def test_adding_a_sheet_checks_it_with_google_then_saves_carefully(client, setup, monkeypatch):
    checked = []
    monkeypatch.setattr("sheets_bridge.profiles.verify_target", lambda service, target: checked.append(target.gid) or {})
    response = client.post("/automation/sheets/add", data={
        "name": "Arjuna JEE 2027", "link": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrSt/edit#gid=77",
        "category": "neet_jee", "note_type": "concise_handdrawn"}, follow_redirects=False)
    assert "kind=ok" in response.headers["location"] and checked == [77]
    names = [s["name"] for s in sheet_list.sheets()]
    assert names[-1] == "Arjuna JEE 2027" and (setup / "sheets.json.bak").is_file()
    bad = client.post("/automation/sheets/add", data={"name": "X", "link": "not a link"}, follow_redirects=False)
    assert "kind=warn" in bad.headers["location"]


def test_editing_a_sheet_from_the_page(client, setup):
    client.post("/automation/sheets/edit", data={"original_name": "Pre Foundation", "name": "Pre Foundation",
                                                 "signature": "Priya Ma'am", "max_cost": "300", "also_pdf": "no",
                                                 "category": "foundation", "note_type": "concise_handdrawn",
                                                 "enabled": "on"})
    entry = json.loads((setup / "sheets.json").read_text(encoding="utf-8"))["profiles"][1]
    assert entry == {"name": "Pre Foundation", "spreadsheetId": "17NLSXUu4smmS33okDzz", "gid": 4,
                     "signature": "Priya Ma'am", "maxCostInr": 300, "alsoPdf": False}
    assert sheet_list.extras_for("Pre Foundation")["category"] == "foundation"
    assert "Foundation" in client.get("/automation").text


def test_the_setup_check_runs_in_the_background_and_reads_as_a_list(client, setup, monkeypatch):
    def fake_check(settings):
        print("[ok]  OAuth client JSON: fine")
        print("[!!]  PW proxy app: not registered")
        return 1

    monkeypatch.setattr("sheets_bridge.setup_check.run_check", fake_check)
    noise = threading.Thread(target=lambda: [print("other thread line") for _ in range(50)])
    client.post("/automation/checks/setup")
    noise.start()
    noise.join()
    for _ in range(100):
        if auto.check_state()["state"] == "done":
            break
        time.sleep(0.05)
    check = auto.check_state()
    assert check["ok"] is False and check["lines"] == ["[ok]  OAuth client JSON: fine", "[!!]  PW proxy app: not registered"]
    card = client.get("/automation/checks/card").text
    assert "needs attention" in card and "OAuth client JSON: fine" in card


def test_the_old_app_being_open_is_noticed(monkeypatch):
    class Old(BaseHTTPRequestHandler):
        server_version = "shortnotes-local/0.2"

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("NOTE_STUDIO_OLD_APP_PORT", str(server.server_address[1]))
        monkeypatch.setitem(auto._state, "old_app", (0.0, False))
        assert auto.old_app_open() is True
    finally:
        server.shutdown()
    monkeypatch.setitem(auto._state, "old_app", (0.0, False))
    monkeypatch.setenv("NOTE_STUDIO_OLD_APP_PORT", "1")
    assert auto.old_app_open() is False
