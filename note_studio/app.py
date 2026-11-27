"""NoteCraft's web app: wiring only.

Each feature is <feature>_routes.py (+ <feature>_service.py from Phase 2) and its
templates; this file holds the shared helpers (render, go, asset), the
same-site guard, the friendly error pages and the register() calls.
"""
from __future__ import annotations

import logging
import os
import secrets
import traceback
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import brand, categories, db, engine_catalog, guides, old_app, paths, settings_store
from .version import CHANNEL, VERSION

STATIC = paths.PACKAGE_DIR / "static"
log = logging.getLogger("note_studio")


def _prepare() -> None:
    """Idempotent: safe from the server start, a test client or an import.
    Everything the engine reads from the environment is set here, before the
    engine is first imported."""
    db.init()
    engine_catalog.put_shortnotes_on_path()
    settings_store.import_old_env_once()
    categories.seed()
    os.environ.update(categories.engine_environment())
    settings_store.apply_engine_environment()
    old_app.adopt_default()
    old_app.apply_environment()
    from . import automation_languages, automation_links, shortnotes_extensions
    shortnotes_extensions.install()
    automation_links.install()
    automation_languages.install()


@asynccontextmanager
async def _lifespan(_app):
    from . import job_queue, make_service, models, signin, update_delivery

    _prepare()
    db.recover()                  # runs left running are stopped; the queue holds if anything waits
    make_service.recover()        # runs added before the Smart Queue existed join it
    models.apply()                # the sheet automation's fallback models
    signin.warm()
    job_queue.start_pump()
    update_delivery.start_poller()
    try:
        yield
    finally:
        update_delivery.stop_poller()
        from . import engine_host
        engine_host.stop_all()


app = FastAPI(title=brand.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
templates = Jinja2Templates(directory=str(paths.PACKAGE_DIR / "templates"))


def asset(name: str) -> str:
    """A static URL versioned by the file's mtime, so a new look never meets an
    old cached stylesheet."""
    try:
        stamp = int((STATIC / name).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{name}?v={stamp}"


templates.env.globals.update(asset=asset, brand=brand, VERSION=VERSION, CHANNEL=CHANNEL)


def _nav_context() -> dict:
    """What the top bar shows. Only the local database: never a network call."""
    nav = {"running": 0, "queue_waiting": 0, "queue_hold": "", "settings_dot": "", "newer": None}
    try:
        from . import updater

        known = updater.status()                 # the last check, kept in the database
        if known["available"]:
            nav["newer"] = {"version": known["latest"]["version"], "notes": known["notes"],
                            "installed": known["installed"]}
        active = db.active_runs()
        nav["running"] = sum(r["state"] == "running" for r in active)
        queue = db.queue_counts()
        nav["queue_waiting"] = queue["waiting"] + sum(r["state"] == "waiting" for r in active)
        nav["queue_hold"] = queue["hold"]
        if not old_app.choice():
            nav["settings_dot"] = "warn"
    except Exception:
        log.exception("nav context")
    return nav


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("nav", _nav_context())
    ctx.setdefault("guide", guides.for_path(request.url.path))
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    # Every screen shows the CURRENT state; a cached copy would show an old one.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def go(path: str, toast: str = "", kind: str = "info") -> RedirectResponse:
    """Redirect, carrying a one-line message that app.js shows once as a toast."""
    if toast:
        path, mark, fragment = path.partition("#")       # the message goes before any #section
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}toast={urllib.parse.quote(toast)}&kind={urllib.parse.quote(kind)}{mark}{fragment}"
    return RedirectResponse(path, 303)


def _local_host(origin: str) -> bool:
    host = urllib.parse.urlsplit(origin).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


@app.middleware("http")
async def _same_site_only(request: Request, call_next):
    """Refuse a state-changing request sent by a page on another site. NoteCraft
    has no login of its own, so this is its protection against another open tab
    posting to this port. A request with neither header is not from a browser."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        crossed = site is not None and site not in ("same-origin", "none")
        if origin is not None and not _local_host(origin):
            crossed = True
        if crossed:
            return PlainTextResponse(f"Refused: this request came from a page outside {brand.APP_NAME}.",
                                     status_code=403)
    return await call_next(request)


# ------------------------------------------------------------ friendly errors
def _write_log(ref: str, exc: BaseException) -> None:
    try:
        with open(paths.logs_dir() / "studio.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {ref} ---\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return render(request, "error.html", status_code=404, title="This page does not exist",
                      detail="The link may be old. Use the menu at the top to find your way.")
    return render(request, "error.html", status_code=exc.status_code,
                  title="That did not work", detail=str(exc.detail or ""))


@app.exception_handler(Exception)
async def _crash(request: Request, exc: Exception):
    ref = secrets.token_hex(3).upper()
    _write_log(ref, exc)
    try:
        return render(request, "error.html", status_code=500, ref=ref,
                      title="Something went wrong on this page",
                      detail="Nothing you made was lost. Try again; if it happens again, "
                             "send the reference below on WhatsApp Help.")
    except Exception:
        return PlainTextResponse(f"Something went wrong (reference {ref}).", status_code=500)


# ---------------------------------------------------------------- features
from . import (automation_routes, check_routes, coming_routes, home_routes, make_routes,  # noqa: E402
               practice_routes, queue_routes, release_routes, runs_routes, settings_routes)

home_routes.register(app, render, go)
release_routes.register(app, render, go)
check_routes.register(app, render, go)
make_routes.register(app, render, go)
automation_routes.register(app, render, go)
queue_routes.register(app, render, go)
runs_routes.register(app, render, go)
practice_routes.register(app, render, go)
settings_routes.register(app, render, go)
coming_routes.register(app, render, go)
templates.env.globals["SOON"] = set(coming_routes.COMING)

if os.environ.get("NOTE_STUDIO_PREPARE_ON_IMPORT", "1") == "1":
    _prepare()
