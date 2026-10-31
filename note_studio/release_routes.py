"""Updates (the button beside Smart Queue, Settings → System → Updates) and the What's new tour."""
from __future__ import annotations

import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from . import brand, db, update_delivery, updater, whats_new

_checking = threading.Lock()


def check_and_download():
    """One network check at a time, regardless of how many browser tabs are open."""
    if not _checking.acquire(blocking=False):
        return updater.status()
    try:
        result = updater.check()
        if result.get("available"):
            update_delivery.start_download(result["latest"])
        return result
    finally:
        _checking.release()


def _wants_page(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def register(app, render, go):
    @app.get("/update/status")
    def update_status():
        value = updater.status()
        if not value.get("checked_at") or time.time() - float(value["checked_at"]) > update_delivery.POLL_SECONDS:
            threading.Thread(target=check_and_download, daemon=True, name="update-check").start()
        return JSONResponse(value)

    @app.post("/update/check")
    def update_check(request: Request):
        result = check_and_download()
        if _wants_page(request):
            if result.get("error"):
                return go("/settings/system#updates", result["error"], "warn")
            words = (f"{result['latest']['version']} found. It downloads in the background; an install prompt appears when ready." if result.get("available")
                     else "This is the newest version.")
            return go("/settings/system#updates", words, "ok")
        return JSONResponse(result)

    @app.post("/update/install")
    def update_install(request: Request):
        try:
            updater.start_update()
            db.set_setting(updater.ERROR, "")
        except ValueError as exc:
            if _wants_page(request):
                return go("/settings/system#updates", str(exc)[:300], "warn")
            return JSONResponse({"error": str(exc)}, status_code=400)
        if _wants_page(request):
            return go("/settings/system#updates",
                      f"Updating: {brand.APP_NAME} closes and opens again by itself in about a minute.", "ok")
        return JSONResponse({"ok": True})

    @app.post("/settings/updates")
    async def save_update_folder(request: Request):
        form = await request.form()
        try:
            updater.save_folder(str(form.get("folder") or ""))
            db.set_setting(updater.CHECKED, "")
        except ValueError as exc:
            return go("/settings/system#updates", str(exc), "warn")
        return go("/settings/system#updates", "Saved. Press Check for updates.", "ok")

    @app.post("/settings/updates/team-key")
    async def add_team_key(request: Request):
        """The team's service-account key file, as QBG One's Settings takes it. Checked, kept
        in the data folder, never shown again: only its email."""
        form = await request.form()
        upload = form.get("key")
        data = await upload.read() if hasattr(upload, "read") else b""
        if not data:
            return go("/settings/system#updates", "Choose the team key file (.json) first.", "warn")
        if len(data) > 64 * 1024:
            return go("/settings/system#updates", "That file is too large to be a team key.", "warn")
        try:
            email = updater.save_team_key(data)
        except ValueError as exc:
            return go("/settings/system#updates", str(exc), "warn")
        return go("/settings/system#updates", f"Team key added ({email}). Share the release folder with it as "
                  "Viewer, then press Check for updates.", "ok")

    @app.post("/settings/updates/team-key/remove")
    def remove_team_key():
        if not updater.remove_team_key():
            return go("/settings/system#updates", "No team key was added on this computer.", "info")
        return go("/settings/system#updates", "Team key removed from this computer (a backup was kept).", "ok")

    @app.get("/whats-new/state")
    def whats_new_state(again: int = 0):
        value = whats_new.state(mark=not again)
        if again and value.get("steps"):
            value["show"] = True
        return JSONResponse(value)

    @app.post("/whats-new/seen")
    async def whats_new_seen(request: Request):
        form = await request.form()
        tour = str(form.get("tour", "")).strip()
        if tour and any(t["id"] == tour for t in whats_new.TOURS):
            whats_new.finish(tour, done=str(form.get("done", "")).strip() in ("1", "true", "yes"))
        return JSONResponse({"ok": True})
