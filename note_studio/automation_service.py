"""Sheet automation: the old app's own run code, driven from NoteCraft.

Everything that runs a sheet is the copied sheets_bridge, unchanged:
sheets_bridge.webui.RunController (one run at a time, Stop after the current
row, live progress) and its api_response() for run / stop / preview / tabs,
with its own option checks. NoteCraft adds:

- a record of every run in its own history (runs, one item per sheet row with
  its Drive link), which survives a restart; the remote run-log tab is still
  written by the run itself, exactly as before;
- careful editing of the shared sheet list (sheet_list.py);
- the setup check, model test and PDF probe as buttons;
- a warning when the old app is open.

Sign-in is the old app's: the signed-in, whitelisted Google account (signin.py).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import capture, db, job_queue, signin, sheet_list

log = logging.getLogger("note_studio.automation")
_lock = threading.Lock()
_state: dict = {"controller": None, "auth": None, "run_id": None, "check": None, "old_app": (0.0, False)}

OUTCOME_WORDS = {"done": ("Done", "ok"), "failed": ("Failed", "danger"), "planned": ("Would be made", "info"),
                 "stopped": ("Not reached", "warn"), "skipped": ("Skipped", "")}
CODE_WORDS = {
    "sign_in_required": "Sign in with Google in Settings first.",
    "already_running": "A sheet run is already going. Stop it or wait for it to finish.",
    "not_authorized": "This Google account is not on PW's list for this app. Ask the owner to add it.",
    "allowlist_undecided": "PW could not confirm your access just now. Try again in a minute.",
    "model_unavailable": "That AI model is not answering right now. Choose another model, or try again later.",
    "blank_link": "Paste the link of the Google Sheet.",
    "invalid_link": "That is not a Google Sheets link. Copy the link from the sheet's address bar.",
    "no_gid": "That link does not say which tab. Load the tabs and pick one, or copy the link again from the tab you want.",
    "duplicate_profile": "There is already a sheet with that name. Choose another name.",
    "profile_not_found": "That sheet is no longer in the list.",
    "sheets_unavailable": "Google did not answer. Check the internet connection and try again.",
    "no_sheet": "Add a sheet first.",
    "bad_profiles": "The sheet list has an entry that cannot be read.",
    "unknown model": "Choose a model from the list.",
    "bad category": "Choose a category.",
    "bad subject": "Choose a subject.",
    "limit must be a positive integer": "The number of rows must be 1 or more.",
    "row must be >= 2": "The row number must be 2 or more (row 1 is the header).",
}


def words(payload: dict | str) -> str:
    """A plain sentence for an error from the old app's API or a BridgeError."""
    if isinstance(payload, str):
        return CODE_WORDS.get(payload, payload)
    code = str(payload.get("error") or "")
    detail = str(payload.get("detail") or "")
    if code in ("bad_profile", "bad_output_folder") and detail:
        return detail[:1].upper() + detail[1:] + "."
    return CODE_WORDS.get(code) or (f"That did not work ({code})." if code else "That did not work.")


# ------------------------------------------------------------- controller
def controller():
    """The one RunController, for the chosen sign-in folder."""
    auth = signin.auth()
    with _lock:
        current = _state["controller"]
        if current is None or (_state["auth"] is not auth and not current.running()):
            from sheets_bridge.settings import Settings
            from sheets_bridge.webui import RunController

            current = RunController(Settings.from_env(), auth=auth)
            _state.update(controller=current, auth=auth)
        return current


def current_run_id() -> int | None:
    """The history record of the sheet run going now, if any."""
    return _state["run_id"]


def running() -> bool:
    current = _state["controller"]
    return bool(current is not None and current.running())


def status() -> dict:
    current = _state["controller"]
    return current.status() if current is not None else {"state": "idle", "log": [], "summary": None, "progress": {}}


def api(method: str, route: str, body: dict | None = None) -> tuple[int, dict]:
    from sheets_bridge.webui import safe_api_response

    return safe_api_response(controller(), method, route, body)


def ready_to_run() -> str:
    """'' when a run may start, else why not, in plain words."""
    if signin.snapshot()["state"] != "ready":
        return "Sign in with Google in Settings first."
    if running():
        return CODE_WORDS["already_running"]
    return ""


def enqueue(options: dict) -> tuple[int, bool]:
    """Add a sheet run to the Smart Queue's Sheets lane: (item id, newly added)."""
    names = options.get("profiles") or []
    what = ", ".join(names) if 0 < len(names) <= 2 else (f"{len(names)} sheets" if names else "the sheets")
    title = ("Dry run: " if options.get("dryRun") else "Sheet run: ") + what
    key = "sheet-" + json.dumps(options, sort_keys=True)
    return job_queue.submit("sheet_run", title, options, key=key)


def launch(entry: dict) -> tuple[str, str, int | None]:
    """The Sheets lane's launcher: start the run, then wait for it to end."""
    ok, problem, run_id = start(entry["payload"])
    if not ok:
        return ("signin" if "Sign in" in problem else "failed"), problem, None
    while running() or current_run_id() == run_id:
        time.sleep(0.5)
    run = db.query("SELECT state, message, error FROM runs WHERE id=?", (run_id,))[0]
    state = {"complete": "done", "needs_attention": "done"}.get(run["state"], "failed")
    if run["error"] and run["error"] == CODE_WORDS.get("not_authorized"):
        state = "signin"
    return state, run["message"] or run["error"], run_id


# ------------------------------------------------------------------ runs
def start(options: dict) -> tuple[bool, str, int | None]:
    """Start a sheet run through the old app's API. (ok, problem, run id)."""
    problem = ready_to_run()
    if problem:
        return False, problem, None
    code, payload = api("POST", "run", options)
    if code != 202:
        return False, words(payload), None
    run_id = _record_start(options)
    threading.Thread(target=_watch, args=(run_id, bool(options.get("dryRun"))), name="sheet-run-watch",
                     daemon=True).start()
    return True, "", run_id


def stop() -> None:
    """Stop after the current row, and hold the Smart Queue: nothing new starts
    by itself over the operator's Stop."""
    if running():
        controller().stop()
        job_queue.hold("stop")
        run_id = _state["run_id"]
        if run_id:
            from . import make_service

            make_service.event(run_id, "step", "Stop asked: finishing the current row, then stopping")


def _record_start(options: dict) -> int:
    names = options.get("profiles") or [s["name"] for s in sheet_list.sheets()
                                        if s["target"] is not None and s["target"].enabled]
    dry = bool(options.get("dryRun"))
    first = sheet_list.extras_for(names[0]) if len(names) == 1 else {}
    title = ("Dry run: " if dry else "Sheet run: ") + (", ".join(names) if len(names) <= 2 else f"{len(names)} sheets")
    run_id = db.create_run("notesbot", title=title[:200], note_type=first.get("note_type") or "concise_handdrawn",
                           category=options.get("category") or "neet_jee", subject=options.get("subject") or "sheet",
                           source="sheet", options={**options, "sheets": names})
    db.execute("UPDATE runs SET state='running', started_at=?, pct=1, message=? WHERE id=?",
               (time.time(), "Signing in and reading the sheet", run_id))
    _state["run_id"] = run_id
    from . import make_service

    make_service.event(run_id, "step", "Started" + (" (dry run: nothing is made or written)" if dry else ""), 1)
    return run_id


def _progress_words(progress: dict) -> tuple[str, int | None]:
    if not progress:
        return "", None
    position, total = progress.get("position") or 0, progress.get("total") or 0
    pct = int(position * 100 / total) if total else None
    parts = [f"Row {progress.get('row')}" + (f" ({position} of {total})" if total else "")]
    parts.append(f"{progress.get('done', 0)} done")
    if progress.get("failed"):
        parts.append(f"{progress['failed']} failed")
    if progress.get("costInr"):
        parts.append(f"₹{progress['costInr']:.2f}")
    if progress.get("sheet"):
        parts.append(progress["sheet"])
    return " · ".join(parts), pct


def _watch(run_id: int, dry: bool) -> None:
    """Copy the controller's progress into the run record until it ends."""
    from . import make_service

    current = controller()
    seen_row = None
    while True:
        alive = current.running()
        st = current.status()
        progress = st.get("progress") or {}
        if progress and progress.get("row") != seen_row:
            seen_row = progress.get("row")
            text, pct = _progress_words(progress)
            outcome = OUTCOME_WORDS.get(progress.get("outcome"), (progress.get("outcome") or "", ""))[0]
            make_service.event(run_id, "step", f"Row {seen_row}: {outcome}", None)
            db.execute("UPDATE runs SET message=?, pct=COALESCE(?, pct), cost_inr=? WHERE id=?",
                       (text, pct, float(progress.get("costInr") or 0), run_id))
        elif st.get("state") == "running" and not progress:
            db.execute("UPDATE runs SET message=? WHERE id=?", ("Reading the sheet and finding the lectures", run_id))
        if not alive:
            break
        time.sleep(1)
    _finish(run_id, current.status(), dry)


def _finish(run_id: int, st: dict, dry: bool) -> None:
    from . import make_service

    summary = st.get("summary") or {}
    counts = summary.get("counts") or {}
    outcomes = summary.get("outcomes") or []
    for position, outcome in enumerate(outcomes, 1):
        db.execute("INSERT INTO run_items (run_id, position, label, state, message, output_path, cost_inr, created_at,"
                   " finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, position, f"Row {outcome.get('row')}", outcome.get("outcome", ""),
                    " ".join(x for x in (outcome.get("code"), outcome.get("note")) if x),
                    outcome.get("link") or "", float(outcome.get("cost_inr") or 0), time.time(), time.time()))
    lines = list(st.get("log") or [])
    db.execute("INSERT INTO run_events (run_id, at, kind, text, data_json) VALUES (?,?,?,?,?)",
               (run_id, time.time(), "log", f"{len(lines)} log lines", json.dumps({"lines": lines}, ensure_ascii=False)))
    cost = float(summary.get("totalCostInr") or 0)
    if st.get("state") == "error":
        problem = str(st.get("error") or "")
        code = problem.split(":", 1)[0].strip()
        message = CODE_WORDS.get(code) or "The sheet run stopped with a problem. The full log is below."
        make_service.finish(run_id, "failed", message, error=message, detail=problem[:300], cost=cost)
    else:
        done, failed = counts.get("done", 0), counts.get("failed", 0)
        planned, stopped = counts.get("planned", 0), counts.get("stopped", 0)
        if dry:
            message = f"Dry run: {planned} row{'s' if planned != 1 else ''} would be made. Nothing was made or written."
        else:
            message = f"{done} lecture{'s' if done != 1 else ''} made" + (f", {failed} failed" if failed else "") + \
                      (f", {stopped} not reached" if stopped else "") + (f" · ₹{cost:.2f}" if cost else "")
        state = "needs_attention" if (failed or summary.get("interrupted") or stopped) else "complete"
        make_service.finish(run_id, state, message, cost=cost)
    _state["run_id"] = None


def preview(names: list[str]) -> tuple[bool, str, list]:
    if signin.snapshot()["state"] != "ready":
        return False, "Sign in with Google in Settings first.", []
    code, payload = api("POST", "preview", {"profiles": names})
    if code != 200:
        return False, words(payload), []
    return True, "", payload.get("items", [])


def tabs(link: str) -> tuple[bool, str, dict]:
    if signin.snapshot()["state"] != "ready":
        return False, "Sign in with Google in Settings first.", {}
    code, payload = api("POST", "tabs", {"link": link})
    if code != 200:
        return False, words(payload), {}
    return True, "", payload


# ---------------------------------------------------------------- sheets
def add_sheet(form: dict) -> tuple[bool, str]:
    """Check the sheet with Google as the signed-in person, then add it (the
    old app's own checks, NoteCraft's careful save)."""
    from sheets_bridge import profiles
    from sheets_bridge.errors import BridgeError
    from sheets_bridge.webui import chosen_gid

    if signin.snapshot()["state"] != "ready":
        return False, "Sign in with Google in Settings first; the sheet is checked with your account."
    try:
        name = profiles.clean_name(form.get("name"))
        spreadsheet_id, link_gid = profiles.parse_sheet_link(form.get("link"))
        folder = profiles.parse_folder_link(form["output_folder"]) if form.get("output_folder") else ""
        all_tabs = bool(form.get("all_tabs"))
        if all_tabs:
            target = profiles.SheetTarget(name=name, spreadsheet_id=spreadsheet_id, gid=0, all_tabs=True,
                                          output_folder=folder)
        else:
            gid = chosen_gid(form.get("gid"), link_gid)
            if gid is None:
                return False, CODE_WORDS["no_gid"]
            target = profiles.SheetTarget(name=name, spreadsheet_id=spreadsheet_id, gid=gid, output_folder=folder)
        account = signin.auth().require()
        try:
            sheets_service, drive_service = account.fresh_services()
            if folder:
                profiles.verify_output_folder(drive_service, folder)
            if all_tabs:
                profiles.verify_workbook(sheets_service, target)
            else:
                profiles.verify_target(sheets_service, target)
        except BridgeError:
            raise
        except Exception:
            return False, CODE_WORDS["sheets_unavailable"]
        sheet_list.add(target.to_json(), extras={"category": form.get("category") or "neet_jee",
                                                 "note_type": form.get("note_type") or "concise_handdrawn"})
    except BridgeError as exc:
        return False, words({"error": exc.code, "detail": exc.detail})
    except sheet_list.SheetListError as exc:
        return False, str(exc)
    return True, ""


def edit_sheet(name: str, form: dict) -> tuple[bool, str, str]:
    """Change one sheet's settings; every key NoteCraft does not know stays. (ok, problem, name)."""
    from sheets_bridge import profiles
    from sheets_bridge.errors import BridgeError

    try:
        current = next((s for s in sheet_list.sheets() if s["name"].casefold() == name.casefold()), None)
        if current is None:
            return False, CODE_WORDS["profile_not_found"], name
        raw = current["raw"]
        changes: dict = {"name": profiles.clean_name(form.get("name") or name)}
        remove: list = []

        def put(key: str, value, keep: bool) -> None:
            """Set the key, or take it out of the entry (the old app's default)."""
            if keep:
                changes[key] = value
            else:
                remove.append(key)

        signature = str(form.get("signature") or "").strip()
        put("signature", signature, bool(signature))
        spend = str(form.get("max_cost") or "").strip()
        if spend:
            try:
                value = float(spend)
            except ValueError:
                return False, "Max spend must be a number of rupees, or empty for no limit.", name
            if value <= 0:
                return False, "Max spend must be more than 0, or empty for no limit.", name
            changes["maxCostInr"] = int(value) if value.is_integer() else value
        else:
            remove.append("maxCostInr")
        also_pdf = form.get("also_pdf", "inherit")
        put("alsoPdf", also_pdf == "yes", also_pdf in ("yes", "no"))
        put("enabled", False, not form.get("enabled"))
        folder_text = str(form.get("output_folder") or "").strip()
        if folder_text:
            folder = profiles.parse_folder_link(folder_text)
            if folder != raw.get("outputFolder"):
                if signin.snapshot()["state"] != "ready":
                    return False, "Sign in first: a new notes folder is checked with your account.", name
                try:
                    _sheets, drive_service = signin.auth().require().fresh_services()
                    profiles.verify_output_folder(drive_service, folder)
                except BridgeError:
                    raise
                except Exception:
                    return False, CODE_WORDS["sheets_unavailable"], name
            changes["outputFolder"] = folder
        else:
            remove.append("outputFolder")
        # Batches that are not lectures (catalog sheets). Empty = the old app's
        # standard list; an explicit empty list already in the file is kept.
        batches = [line.strip() for line in str(form.get("skip_batches") or "").splitlines() if line.strip()]
        if batches:
            changes["skipBatches"] = batches
        elif raw.get("skipBatches") != []:
            remove.append("skipBatches")
        final = sheet_list.update(name, changes, remove_keys=tuple(remove))
        sheet_list.set_extras(final, {"category": form.get("category") or "neet_jee",
                                      "note_type": form.get("note_type") or "concise_handdrawn"})
        return True, "", final
    except BridgeError as exc:
        return False, words({"error": exc.code, "detail": exc.detail}), name
    except sheet_list.SheetListError as exc:
        return False, str(exc), name


# ---------------------------------------------------------------- checks
CHECKS = {
    "setup": "Check setup",
    "model": "Test the AI model",
    "probe": "Try one PDF",
}


def check_state() -> dict | None:
    return _state["check"]


def start_check(kind: str, pdf_path: str = "") -> tuple[bool, str]:
    current = _state["check"]
    if current and current["state"] == "running":
        return False, "A check is already running."
    if kind not in CHECKS:
        return False, "Unknown check."
    if kind in ("model", "probe") and signin.snapshot()["state"] != "ready":
        return False, "Sign in with Google in Settings first."
    _state["check"] = {"kind": kind, "label": CHECKS[kind], "state": "running", "lines": [], "ok": None,
                       "started": time.time()}
    threading.Thread(target=_run_check, args=(kind, pdf_path), name=f"check-{kind}", daemon=True).start()
    return True, ""


def _run_check(kind: str, pdf_path: str) -> None:
    from sheets_bridge.settings import Settings

    result = 1
    with capture.captured() as out:
        try:
            settings = Settings.from_env()
            if kind == "setup":
                from sheets_bridge.setup_check import run_check

                result = run_check(settings)
            elif kind == "model":
                from sheets_bridge.model_check import test_model

                result = test_model(settings, interactive=False)
            else:
                from sheets_bridge.model_check import probe_pdf_file

                result = probe_pdf_file(settings, pdf_path, group_index=1, language="auto", interactive=False)
        except Exception as exc:
            print(f"[!!]  the check could not finish ({type(exc).__name__})")
            result = 1
    lines = [line.rstrip() for line in out.getvalue().splitlines() if line.strip()]
    check = _state["check"] or {}
    check.update(state="done", ok=(result == 0), lines=lines[-200:], finished=time.time())
    if pdf_path:
        Path(pdf_path).unlink(missing_ok=True)


# --------------------------------------------------------------- old app
def old_app_open() -> bool:
    """Is the old Short Notes app answering on its port (3000)? Checked at most every 15 s."""
    checked_at, value = _state["old_app"]
    if time.time() - checked_at < 15:
        return value
    port = int(os.environ.get("NOTE_STUDIO_OLD_APP_PORT", "3000"))
    value = False
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.4)
        connection.request("HEAD", "/")
        value = connection.getresponse().getheader("Server", "").startswith("shortnotes-local")
        connection.close()
    except Exception:
        value = False
    _state["old_app"] = (time.time(), value)
    return value


def recent_runs(limit: int = 6) -> list[dict]:
    return db.query("SELECT * FROM runs WHERE source='sheet' ORDER BY id DESC LIMIT ?", (limit,))


job_queue.register("sheet_run", launch)
