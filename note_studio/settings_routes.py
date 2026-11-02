"""Settings, in five tabs:

  /settings             Sign-in: the old app's sheets and sign-in, Google sign-in
  /settings/ai          AI models: the order per note type and stage
  /settings/categories  Categories: rules (edit, a backup first), preview, check, add, on/off
  /settings/values      Values you can change (the old app's knobs)
  /settings/system      Note engines, note types, storage, about
"""
from __future__ import annotations

import os
import platform

from fastapi import Request
from fastapi.responses import JSONResponse

from . import (brand, categories, category_admin, db, models, note_types, old_app, paths, practice_service,
               requirements, settings_store, setup_check, signin, updater)
from .engines import upsc as upsc_engine
from .version import CHANNEL, VERSION
from .brand import APP_NAME as APP

TABS = (("/settings", "Sign-in"), ("/settings/ai", "AI models"), ("/settings/categories", "Categories"),
        ("/settings/values", "Values"), ("/settings/system", "System"))
CHOICE_WORDS = {
    "yes": ("Using the old app's sheets and sign-in", "ok"),
    "no": ("Kept separate from the old app", "info"),
    "": ("Not chosen yet", "warn"),
}


def _size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _words(size: int) -> str:
    return f"{size / 1e6:.1f} MB" if size >= 1e5 else f"{size / 1e3:.0f} KB"


def storage() -> list[dict]:
    home = paths.home()
    rows = [{"label": label, "path": str(home / name), "size": _words(_size(home / name)), "about": about}
            for name, label, about in (
                ("runs", "Notes made here", "Every run's lecture and its notes."),
                ("queue", "Waiting in the Smart Queue", "Lectures that have not started yet."),
                ("categories", "Categories and their rules", "Kept through updates."),
                ("backups", "Backups", "Copies of the sheet list taken before each change (the last 30)."),
                ("logs", "Problem reports", "Written when a page goes wrong; send the reference on Help."))]
    rows.append({"label": "The old Short Notes app's folder", "path": str(paths.old_app_home()), "size": "",
                 "about": f"Shared, read and written only as you chose. {APP} never deletes anything there."})
    rows.append({"label": "Note engines", "path": str(paths.ENGINES_DIR), "size": "",
                 "about": "Copies of the note apps. The original apps stay where they are, unchanged."})
    return rows


def _packs() -> list[dict]:
    rows = []
    for pack in categories.all_packs():
        rows.append({**pack, "types": categories.offered_types(pack), "rules": category_admin.rule_files(pack),
                     "targets": category_admin.rule_targets(pack),
                     "subject_choices": categories.subject_choices(pack)})
    return rows


def register(app, render, go):
    def page(request: Request, tab: str, **ctx):
        return render(request, "settings.html", tab=tab, tabs=TABS, **ctx)

    # ------------------------------------------------------------ sign-in
    @app.get("/settings")
    def settings(request: Request):
        old = old_app.status()
        return page(request, "/settings", old=old, choice_words=CHOICE_WORDS[old["choice"]], signin=signin.snapshot())

    @app.post("/settings/old-app")
    async def choose_old_app(request: Request):
        form = await request.form()
        decision = str(form.get("choice", ""))
        if decision not in ("yes", "no"):
            return go("/settings#old-app", "Pick one of the two choices.", "warn")
        db.set_setting(old_app.SHARE_KEY, decision)
        for key, value in old_app.environment(decision).items():   # the paths change with the choice
            os.environ[key] = value
        signin.reset()
        message = (f"{APP} now uses the old app's sheets and sign-in." if decision == "yes"
                   else f"{APP} keeps its own sheets and sign-in.")
        return go("/settings#old-app", message, "ok")

    @app.get("/settings/signin-card")
    def signin_card(request: Request):
        return render(request, "_signin_card.html", signin=signin.snapshot())

    @app.post("/settings/signin/start")
    def signin_popup():
        try:
            return {"url": signin.start_popup()}
        except Exception:
            return JSONResponse({"detail":"Google sign-in could not start. Check the registration and close any earlier sign-in."}, status_code=400)

    @app.post("/settings/signin")
    def sign_in():
        try:
            signin.start()
        except signin.SignInNeeded as problem:
            return go("/settings#signin", str(problem), "warn")
        except Exception:
            return go("/settings#signin", "Google sign-in could not be opened: its registration file "
                      "is missing or could not be read. Ask your administrator for the Google Desktop registration JSON, "
                      "save it as ~/.note_studio/google-client.json, and restart NoteCraft.", "danger")
        return go("/settings#signin", "Google sign-in opened in your browser. Finish it there.", "info")

    @app.post("/settings/signout")
    def sign_out():
        signin.sign_out()
        return go("/settings#signin", "Signed out. Notes cannot be made until you sign in again.", "info")

    # ---------------------------------------------------------- AI models
    @app.get("/settings/ai")
    def ai_models(request: Request):
        rows = [{"type": t, "stage": stage, "stage_label": models.STAGES[stage], "order": models.order(t["id"], stage),
                 "default": models.default_order(t["id"], stage), "slots": models.slots(t["id"], stage),
                 "selectable": models.selectable(t["id"], stage), "hints": models.hints(t["id"])}
                for t in [*note_types.all_types(), *note_types.tools()] if note_types.ready(t["id"])
                for stage in models.stages(t["id"])]
        return page(request, "/settings/ai", rows=rows, resting=models.resting())

    @app.post("/settings/ai")
    async def save_models(request: Request):
        form = await request.form()
        type_id, stage = str(form.get("type_id") or ""), str(form.get("stage") or "notes")
        if not note_types.ready(type_id) or stage not in models.stages(type_id):
            return go("/settings/ai", "Choose a note type.", "warn")
        if form.get("reset"):
            chosen = models.default_order(type_id, stage)
        else:
            chosen = [str(form.get(f"model_{i}") or "") for i in range(4)]
        try:
            saved = models.set_order(type_id, stage, chosen)
        except ValueError as problem:
            return go("/settings/ai", str(problem), "warn")
        return go("/settings/ai", f"Saved: {saved[0]} first" + (f", then {', '.join(saved[1:])}" if saved[1:] else "") + ".",
                  "ok")

    # --------------------------------------------------------- categories
    @app.get("/settings/categories")
    def category_page(request: Request, open: str = ""):
        return page(request, "/settings/categories", packs=_packs(), open_id=open,
                    packs_dir=str(categories.packs_dir()), all_types=note_types.all_types(),
                    languages=note_types.LANGUAGE_LABELS)

    @app.post("/settings/categories/{category_id}/rule")
    async def save_rule(request: Request, category_id: str):
        form = await request.form()
        try:
            category_admin.save_rule(category_id, str(form.get("name") or ""), str(form.get("text") or ""))
        except category_admin.CategoryError as problem:
            return go(f"/settings/categories?open={category_id}", str(problem), "warn")
        return go(f"/settings/categories?open={category_id}#cat-{category_id}",
                  "Saved. The next notes follow it; the old text was kept as a backup.", "ok")

    @app.post("/settings/categories/{category_id}/enabled")
    async def switch(request: Request, category_id: str):
        form = await request.form()
        on = form.get("on") == "1"
        try:
            category_admin.set_enabled(category_id, on)
        except category_admin.CategoryError as problem:
            return go("/settings/categories", str(problem), "warn")
        return go(f"/settings/categories#cat-{category_id}", "Switched on." if on else
                  "Switched off: it is no longer offered on New notes.", "ok")

    @app.get("/settings/categories/{category_id}/check")
    def check(request: Request, category_id: str):
        return render(request, "_category_check.html", findings=category_admin.check(category_id))

    @app.get("/settings/categories/preview")
    def preview(request: Request, category: str = "", note_type: str = "", subject: str = "auto",
                language: str = "auto", extra: str = ""):
        resolved = requirements.resolve(category, note_type, subject, language, extra=extra)
        return render(request, "_category_preview.html", resolved=resolved)

    @app.post("/settings/categories/add")
    async def add(request: Request):
        form = await request.form()
        values = {"label": form.get("label"), "subjects": form.get("subjects"), "summary": form.get("summary"),
                  "rules": form.get("rules"), "languages": form.getlist("languages"),
                  "note_types": form.getlist("note_types")}
        try:
            category_id = category_admin.add(values)
        except category_admin.CategoryError as problem:
            return go("/settings/categories#add", str(problem), "warn")
        return go(f"/settings/categories?open={category_id}#cat-{category_id}",
                  "Category added. Write its rules below, then press Check category.", "ok")

    # ------------------------------------------------------------- values
    @app.get("/settings/values")
    def values_page(request: Request):
        return page(request, "/settings/values", values=settings_store.engine_values(),
                    imported=settings_store.import_record())

    @app.post("/settings/values")
    async def save_values(request: Request):
        form = await request.form()
        problems = settings_store.save_values({key: form.get(key) for key in settings_store.KNOBS if key in form})
        if problems:
            return go("/settings/values", " ".join(problems), "warn")
        return go("/settings/values", f"Saved. Close and reopen {APP} for the new values to be used.", "ok")

    # ------------------------------------------------------------- system
    @app.get("/settings/system/check")
    def setup(request: Request):
        return render(request, "_setup_check.html", checks=setup_check.run())

    @app.post("/settings/system/dpp")
    async def dpp_folder(request: Request):
        form = await request.form()
        try:
            practice_service.set_masters_folder(str(form.get("folder") or ""))
        except practice_service.PracticeError as problem:
            return go("/settings/system#dpp", str(problem), "warn")
        return go("/settings/system#dpp", "Saved. The next DPP step reads the workbooks from there.", "ok")

    @app.get("/settings/system")
    def system(request: Request):
        types = [dict(t, available=note_types.available(t["id"])) for t in note_types.all_types()]
        return page(request, "/settings/system", engines=categories.engine_status(), types=types,
                    upsc_available=note_types.available("upsc_dcn"),
                    engine_label=note_types.engine_label, storage=storage(),
                    dpp=practice_service.masters_status(), dpp_files=practice_service.MASTER_FILES,
                    updates=updater.status(),
                    dpp_default=str(upsc_engine.data_dir()),
                    about={"version": VERSION, "channel": CHANNEL, "address": str(request.base_url).rstrip("/"),
                           "python": platform.python_version(), "app": brand.APP_NAME})
