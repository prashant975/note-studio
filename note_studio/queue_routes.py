"""Smart Queue: what waits and what runs, in two lanes; Remove, Pause, Continue."""
from __future__ import annotations

from fastapi import Request

from . import db, job_queue

STATE_WORDS = {"waiting": ("Waiting", ""), "running": ("Running", "info"), "done": ("Done", "ok"),
               "failed": ("Did not finish", "danger"), "removed": ("Removed", ""), "stopped": ("Stopped", "warn")}


def _with_runs(items: list[dict]) -> list[dict]:
    """Each item with the run made for it (once its turn came)."""
    for item in items:
        run_id = item.get("run_id")
        if not run_id:
            rows = db.query("SELECT id FROM runs WHERE queue_id=? ORDER BY id DESC LIMIT 1", (item["id"],))
            run_id = rows[0]["id"] if rows else None
        run = db.query("SELECT id, state, pct, message FROM runs WHERE id=?", (run_id,))[0] if run_id else None
        item["run"] = run
        item["words"], item["cls"] = STATE_WORDS.get(item["state"], (item["state"], ""))
    return items


def lanes() -> list[dict]:
    out = []
    for lane, label in job_queue.LANES.items():
        items = _with_runs(job_queue.entries(lane=lane))
        out.append({"id": lane, "label": label, "running": [i for i in items if i["state"] == "running"],
                    "waiting": [i for i in items if i["state"] == "waiting"]})
    return out


def register(app, render, go):
    def context() -> dict:
        return {"lanes": lanes(), "hold": job_queue.held(), "hold_words": job_queue.hold_words(),
                "finished": _with_runs(job_queue.finished()), "counts": job_queue.counts()}

    @app.get("/queue")
    def queue_page(request: Request):
        return render(request, "queue.html", **context())

    @app.get("/queue/card")
    def queue_card(request: Request):
        return render(request, "_queue_lanes.html", **context())

    @app.get("/queue/strip")
    def queue_strip(request: Request):
        return render(request, "_queue_strip.html", queue_counts=job_queue.counts(), hold_words=job_queue.hold_words())

    @app.post("/queue/{entry_id}/remove")
    def remove(entry_id: int):
        if job_queue.remove(entry_id):
            return go("/queue", "Removed. Nothing was made or paid for.", "ok")
        return go("/queue", "That one has already started (or finished), so it stays.", "warn")

    @app.post("/queue/continue")
    def resume():
        job_queue.resume()
        return go("/queue", "Continuing: the next items start by themselves.", "ok")

    @app.post("/queue/pause")
    def pause():
        job_queue.hold("paused")
        return go("/queue", "Paused: what is running finishes; nothing new starts until you continue.", "info")
