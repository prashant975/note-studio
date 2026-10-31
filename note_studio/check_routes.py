"""Check notes: upload any notes file, or check a finished run's notes from its page."""
from __future__ import annotations

from fastapi import Request

from . import check_service, job_queue


def register(app, render, go):
    @app.get("/check")
    def check_page(request: Request):
        return render(request, "check.html", checks=check_service.recent(), accepted=check_service.ACCEPTED,
                      max_mb=check_service.MAX_MB, queue_counts=job_queue.counts(), hold_words=job_queue.hold_words())

    @app.post("/check")
    async def check_upload(request: Request):
        form = await request.form()
        upload = form.get("file")
        data = await upload.read() if hasattr(upload, "read") else b""
        try:
            _id, created = check_service.check_upload(getattr(upload, "filename", "") or "", data,
                                                      full=form.get("kind") == "upsc")
        except check_service.CheckError as problem:
            return go("/check", str(problem), "warn")
        if not created:
            return go("/queue", "Already in the Smart Queue: it was not added twice.", "info")
        return go("/queue", "Added the check to the Smart Queue.", "ok")

    @app.post("/runs/{run_id}/check")
    def check_run(run_id: int):
        try:
            _id, created = check_service.check_run(run_id)
        except check_service.CheckError as problem:
            return go(f"/runs/{run_id}", str(problem), "warn")
        if not created:
            return go("/queue", "That check is already in the Smart Queue.", "info")
        return go("/queue", "Added the check to the Smart Queue.", "ok")
