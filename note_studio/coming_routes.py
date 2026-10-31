"""Pages that arrive in later phases: each says, in plain words, what it will do.

When a page is built, its entry here is removed and its own <feature>_routes.py
takes the URL (tests check every menu link answers).
"""
from __future__ import annotations

from fastapi import Request

# Every planned page is built (Phase 8); a future one goes here until it is.
COMING: dict = {}


def register(app, render, go):
    def page(path: str):
        def view(request: Request):
            return render(request, "coming.html", page=COMING[path], path=path)
        view.__name__ = "coming_" + path.strip("/").replace("-", "_")
        return view

    for path in COMING:
        app.add_api_route(path, page(path), methods=["GET"])
