"""Overview: Start, Running now, Needs you, the note engines, recent runs.

Nothing on this page starts work; every action links to the page that does it.
"""
from __future__ import annotations

from fastapi import Request

from . import brand, categories, coming_routes, db, job_queue, old_app, paths, signin
from .version import VERSION
from .brand import APP_NAME as APP

# The things people come to do, most used first. A tile whose page is still in
# coming_routes.COMING shows "Soon".
START = (
    {"href": "/new", "icon": "plus", "title": "New notes", "lead": True,
     "about": "Lecture PDFs or a class recording, for any category"},
    {"href": "/automation", "icon": "sheet", "title": "Sheet automation",
     "about": "Notes for every new row of a Google Sheet"},
    {"href": "/runs", "icon": "file-text", "title": "Runs",
     "about": "Everything made, with files, cost and log"},
    {"href": "/practice", "icon": "target", "title": "Practice",
     "about": "Mains practice and DPP from finished notes"},
    {"href": "/check", "icon": "shield", "title": "Check notes",
     "about": "A check report for any finished notes"},
    {"href": "/queue", "icon": "layers", "title": "Smart Queue",
     "about": "Add many at once; they run one by one"},
)


def start_tiles() -> list[dict]:
    return [{**tile, "ready": tile["href"] not in coming_routes.COMING} for tile in START]

RUN_STATE_WORDS = {
    "waiting": ("Waiting", ""), "running": ("Running", "info"), "complete": ("Done", "ok"),
    "needs_attention": ("Needs attention", "warn"), "failed": ("Did not finish", "danger"),
    "stopped": ("Stopped", "warn"),
}


def needs_you(queue: dict) -> list[dict]:
    """What waits for a person, in plain words, each with the one place to fix it."""
    items = []
    if not old_app.choice():
        items.append({"kind": "warn", "icon": "key", "href": "/settings#old-app", "action": "Choose",
                      "title": f"Choose whether {APP} uses the old app's sheets and sign-in",
                      "about": "A one-time choice. Recommended: use them, so both apps share one "
                               "sheet list and one Google sign-in."})
    elif signin.snapshot()["state"] in ("signed_out", "error"):
        items.append({"kind": "warn", "icon": "key", "href": "/settings#signin", "action": "Sign in",
                      "title": "Sign in with Google to make notes",
                      "about": "Notes are made through PW with your Google account."})
    if queue.get("hold"):
        waiting = queue.get("waiting", 0)
        items.append({"kind": "warn", "icon": "pause", "href": "/queue", "action": "Continue",
                      "title": f"Smart Queue is holding ({waiting} waiting)",
                      "about": job_queue.hold_words(queue["hold"]) + " Press Continue in the Smart Queue."})
    for run in db.attention_runs():
        words, _ = RUN_STATE_WORDS.get(run["state"], (run["state"], ""))
        items.append({"kind": "danger" if run["state"] == "failed" else "warn", "icon": "alert-circle",
                      "href": f"/runs/{run['id']}", "action": "Open",
                      "title": f"{run['title'] or 'A run'}: {words.lower()}",
                      "about": run["error"] or run["message"]})
    return items


def register(app, render, go):
    @app.get("/")
    def overview(request: Request):
        queue = db.queue_counts()
        return render(request, "home.html", start=start_tiles(), queue=queue,
                      active=db.active_runs(), recent=db.recent_runs(),
                      needs=needs_you(queue), engines=categories.engine_status(),
                      states=RUN_STATE_WORDS)

    @app.get("/api/instance")
    def instance():
        """The launcher asks this to know the copy on a port is this one."""
        return {"app": brand.APP_NAME, "instance": paths.instance_id(), "version": VERSION}
