"""Runs: every run with filters, one run page (live while it works), and its files."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import categories, check_service, db, engine_host, job_queue, make_service, note_types, older_runs

STATE_WORDS = {
    "waiting": ("Waiting", ""), "running": ("Making notes", "info"), "complete": ("Done", "ok"),
    "needs_attention": ("Needs attention", "warn"), "failed": ("Did not finish", "danger"),
    "stopped": ("Stopped", "warn"),
}
FILTERS = {"active": ("waiting", "running"), "done": ("complete",),
           "attention": ("needs_attention", "failed", "stopped")}
FILE_WORDS = {".pdf": "PDF", ".docx": "Word document", ".xlsx": "Excel workbook", ".md": "Text report"}


def _decorate(run: dict) -> dict:
    run = dict(run)
    pack = categories.get(run["category"]) or {}
    entry = note_types.get(run["note_type"]) or {}
    run["category_label"] = pack.get("label", run["category"])
    run["type_label"] = entry.get("label", run["note_type"])
    subjects = {s["id"]: s["label"] for s in categories.subject_choices(pack)} if pack else {}
    run["subject_label"] = subjects.get(run["subject"], run["subject"])
    run["language_label"] = note_types.language_label(run["language"]) if run["language"] else ""
    run["state_words"], run["state_cls"] = STATE_WORDS.get(run["state"], (run["state"], ""))
    run["active"] = run["state"] in ("waiting", "running")
    run["options"] = json.loads(run.get("options_json") or "{}")
    return run


def _run(run_id: int) -> dict:
    rows = db.query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        raise HTTPException(404)
    run = _decorate(rows[0])
    run["events"] = db.query("SELECT * FROM run_events WHERE run_id=? AND kind IN ('step','output','done','warning') "
                             "ORDER BY id", (run_id,))
    run["files"] = db.query("SELECT * FROM run_items WHERE run_id=? ORDER BY position", (run_id,))
    for item in run["files"]:
        item["kind_words"] = FILE_WORDS.get(Path(item["output_path"] or "").suffix.lower(), "File")
    run["sheet"] = run["source"] == "sheet"
    run["stoppable"] = run["state"] == "running" and engine_host.running(run_id)
    run["checkable"] = check_service.checkable_file(run) is not None
    run["log"] = []
    if run["sheet"]:
        from .automation_service import OUTCOME_WORDS

        for item in run["files"]:
            item["words"], item["cls"] = OUTCOME_WORDS.get(item["state"], (item["state"], ""))
        logs = db.query("SELECT data_json FROM run_events WHERE run_id=? AND kind='log' ORDER BY id DESC LIMIT 1",
                        (run_id,))
        run["log"] = json.loads(logs[0]["data_json"]).get("lines", []) if logs else []
    run["position"] = 0
    if run["state"] == "waiting":
        ahead = db.query("SELECT COUNT(*) AS n FROM runs WHERE state IN ('waiting','running') AND id<?", (run_id,))
        run["position"] = ahead[0]["n"]
    return run


def register(app, render, go):
    @app.get("/runs")
    def runs(request: Request, state: str = "", category: str = "", q: str = ""):
        sql, args = "SELECT * FROM runs WHERE 1=1", []
        if state in FILTERS:
            sql += f" AND state IN ({','.join('?' * len(FILTERS[state]))})"
            args += list(FILTERS[state])
        if category:
            sql += " AND category=?"
            args.append(category)
        if q.strip():
            sql += " AND title LIKE ?"
            args.append(f"%{q.strip()}%")
        rows = [_decorate(r) for r in db.query(sql + " ORDER BY id DESC LIMIT 200", tuple(args))]
        counts = {name: db.query(f"SELECT COUNT(*) AS n FROM runs WHERE state IN ({','.join('?' * len(v))})", v)[0]["n"]
                  for name, v in FILTERS.items()}
        return render(request, "runs.html", runs=rows, state=state, category=category, q=q, counts=counts,
                      packs=categories.all_packs(), hold=job_queue.held(),
                      waiting=db.query("SELECT COUNT(*) AS n FROM runs WHERE state='waiting'")[0]["n"])

    # Before /runs/{run_id}: "older" is not a run number.
    @app.get("/runs/older")
    def older(request: Request):
        return render(request, "runs_older.html", runs=older_runs.upsc_runs(), folder=str(older_runs.upsc_runs_dir()))

    @app.get("/runs/older/upsc/{name}")
    def older_run(request: Request, name: str):
        run = older_runs.upsc_run(name)
        if run is None:
            raise HTTPException(404)
        return render(request, "older_run.html", run=run)

    @app.get("/runs/older/upsc/{name}/files/{filename}")
    def older_file(name: str, filename: str):
        path = older_runs.upsc_file(name, filename)
        if path is None or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=path.name)

    @app.get("/runs/{run_id}")
    def run_page(request: Request, run_id: int):
        run = _run(run_id)
        if run["source"] == "practice":
            return RedirectResponse(f"/practice/{run_id}", 303)
        return render(request, "run.html", run=run, hold=job_queue.held())

    @app.get("/runs/{run_id}/card")
    def run_card(request: Request, run_id: int):
        return render(request, "_run_card.html", run=_run(run_id), hold=job_queue.held())

    @app.get("/runs/{run_id}/files/{item_id}")
    def run_file(run_id: int, item_id: int):
        rows = db.query("SELECT * FROM run_items WHERE id=? AND run_id=?", (item_id, run_id))
        if not rows or not rows[0]["output_path"]:
            raise HTTPException(404)
        path = Path(rows[0]["output_path"])
        root = make_service.runs_root().resolve()
        if not path.is_file() or root not in path.resolve().parents:
            raise HTTPException(404)
        return FileResponse(path, filename=rows[0]["label"] or path.name)

    @app.post("/runs/{run_id}/stop")
    def stop(run_id: int):
        if not make_service.stop(run_id):
            return go(f"/runs/{run_id}", "This run is not being made right now.", "info")
        return go(f"/runs/{run_id}", "Stopped. The Smart Queue holds until you press Continue.", "ok")

    @app.post("/runs/{run_id}/again")
    def again(run_id: int):
        rows = db.query("SELECT source FROM runs WHERE id=?", (run_id,))
        if rows and rows[0]["source"] == "check":
            if check_service.again(run_id) is None:
                return go(f"/runs/{run_id}", "This check cannot be run again (its file is missing).", "warn")
            return go("/queue", "Added the check to the Smart Queue again.", "ok")
        entry_id = make_service.retry(run_id)
        if entry_id is None:
            return go(f"/runs/{run_id}", "This run cannot be made again (its lecture file is missing).", "warn")
        return go("/queue", "Added to the Smart Queue again, with the same choices.", "ok")
