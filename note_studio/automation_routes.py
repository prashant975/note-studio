"""Sheet automation: run the Google Sheets, manage the sheet list, and the checks."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import Request

from . import automation_service as auto
from . import categories, job_queue, models, note_types, sheet_list, signin

SUBJECT_WORDS = {"sheet": "From the sheet (column D)", "auto": "Auto Detect", "chemistry": "Chemistry",
                 "physics": "Physics", "biology": "Biology", "mathematics": "Maths", "sanskrit": "Sanskrit",
                 "science": "Science", "social_science": "Social Science (SST)", "english": "English",
                 "hindi": "Hindi", "other": "Other"}


def _int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _options(form) -> dict:
    """The run form, in the shape the old app's run API checks (webui._clean_options)."""
    chosen = str(form.get("sheet") or "")
    enabled = [s["name"] for s in sheet_list.sheets() if s["target"] is not None and s["target"].enabled]
    spend = str(form.get("max_cost") or "").strip()
    try:
        max_cost = float(spend) if spend else None
    except ValueError:
        max_cost = None
    return {
        "dryRun": form.get("mode") == "dry",
        "limit": _int(form.get("limit")),
        "row": _int(form.get("row")),
        "redo": form.get("redo") == "on",
        "showPageNumbers": form.get("page_numbers") == "on",
        "maxCost": max_cost,
        "subject": str(form.get("subject") or "sheet"),
        "category": str(form.get("category") or "neet_jee"),
        "sanskritStyle": str(form.get("sanskrit_style") or "hindi"),
        "model": str(form.get("model") or ""),
        "profiles": enabled if chosen == "__all__" else ([chosen] if chosen else []),
    }


def _sheet_rows() -> list[dict]:
    rows = []
    packs = {p["id"]: p["label"] for p in categories.all_packs()}
    for s in sheet_list.sheets():
        target, raw, extras = s["target"], s["raw"], s["extras"]
        rows.append({
            **s, "enabled": bool(target and target.enabled), "all_tabs": bool(target and target.all_tabs),
            "gid": target.gid if target else "", "spreadsheet_id": target.spreadsheet_id if target else "",
            "category": extras.get("category", "neet_jee"), "category_set": "category" in extras,
            "category_label": packs.get(extras.get("category", "neet_jee"), extras.get("category", "neet_jee")),
            "note_type": extras.get("note_type", "concise_handdrawn"),
            "type_label": (note_types.get(extras.get("note_type", "concise_handdrawn")) or {}).get("label", ""),
            "signature": raw.get("signature", "") if isinstance(raw, dict) else "",
            "max_cost": raw.get("maxCostInr", "") if isinstance(raw, dict) else "",
            "also_pdf": {True: "yes", False: "no"}.get(raw.get("alsoPdf") if isinstance(raw, dict) else None, "inherit"),
            "output_folder": raw.get("outputFolder", "") if isinstance(raw, dict) else "",
            "skip_batches": "\n".join(raw.get("skipBatches") or []) if isinstance(raw, dict) else "",
            "extra_keys": sorted(k for k in raw if k not in sheet_list.FIELD_ORDER) if isinstance(raw, dict) else [],
        })
    return rows


def _model_choices() -> list[dict]:
    """The run form's models, the first of Settings -> AI models chosen (the
    engine's own list otherwise preselects its environment value)."""
    from sheets_bridge import pw_transport

    listed = pw_transport.selectable_models()
    first = (models.order("concise_handdrawn") or [""])[0]
    if any(m["id"] == first for m in listed):
        listed = [dict(m, current=m["id"] == first) for m in listed]
    return listed


def _page_context(problem: str = "") -> dict:
    try:
        rows, list_problem, sheet_file = _sheet_rows(), "", str(sheet_list.path())
    except sheet_list.SheetListError as exc:
        rows, list_problem, sheet_file = [], str(exc), ""
    handdrawn = [p for p in categories.all_packs(include_disabled=False)
                 if "concise_handdrawn" in (p.get("note_types") or [])]
    return {"queue_counts": job_queue.counts(), "hold_words": job_queue.hold_words(),
            "sheets": rows, "list_problem": list_problem, "problem": problem, "signin": signin.snapshot(),
            "live": auto.status(), "running": auto.running(), "blocked": auto.ready_to_run(),
            "old_app_open": auto.old_app_open(), "check": auto.check_state(),
            "categories": handdrawn, "types": [t for t in note_types.all_types() if t["engine"] == "notesbot"],
            "subjects": SUBJECT_WORDS, "models": _model_choices(),
            "outcome_words": auto.OUTCOME_WORDS, "recent": auto.recent_runs(), "run_id": auto.current_run_id(),
            "sheet_file": sheet_file}


def register(app, render, go):
    @app.get("/automation")
    def automation(request: Request):
        return render(request, "automation.html", **_page_context())

    @app.get("/automation/live")
    def live(request: Request):
        return render(request, "_automation_live.html", live=auto.status(), running=auto.running(),
                      outcome_words=auto.OUTCOME_WORDS, run_id=auto.current_run_id())

    @app.post("/automation/confirm")
    async def confirm(request: Request):
        form = await request.form()
        options = _options(form)
        ok, problem, items = auto.preview(options["profiles"])
        return render(request, "_automation_confirm.html", ok=ok, problem=problem, items=items,
                      options=options, form=dict(form),
                      ready=sum(int(i.get("ready") or 0) for i in items))

    @app.post("/automation/run")
    async def run(request: Request):
        form = await request.form()
        options = _options(form)
        if signin.snapshot()["state"] != "ready":
            return go("/automation", "Sign in with Google in Settings first.", "warn")
        from sheets_bridge.webui import _clean_options

        try:                                   # the old app's own checks, before it waits in the queue
            options = _clean_options(options, auto.controller().settings)
        except ValueError as problem:
            return go("/automation", auto.words(str(problem)), "warn")
        _entry, created = auto.enqueue(options)
        if not created:
            return go("/automation#live", "That run is already in the Smart Queue.", "info")
        dry = form.get("mode") == "dry"
        return go("/automation#live", ("Dry run added: nothing is made or written." if dry
                                       else "Added to the Smart Queue: it starts when the Sheets lane is free."), "ok")

    @app.post("/automation/stop")
    def stop():
        auto.stop()
        return go("/automation#live", "Stopping after the current row.", "info")

    @app.post("/automation/tabs")
    async def tabs(request: Request):
        form = await request.form()
        ok, problem, payload = auto.tabs(str(form.get("link") or ""))
        return render(request, "_automation_tabs.html", ok=ok, problem=problem, payload=payload)

    @app.post("/automation/sheets/add")
    async def add_sheet(request: Request):
        form = await request.form()
        ok, problem = auto.add_sheet({k: form.get(k) for k in ("name", "link", "gid", "output_folder", "category",
                                                              "note_type")} | {"all_tabs": form.get("all_tabs") == "on"})
        if not ok:
            return go("/automation#sheets", problem, "warn")
        return go("/automation#sheets", "Sheet added. The old app sees it too.", "ok")

    @app.post("/automation/sheets/edit")
    async def edit_sheet(request: Request):
        form = await request.form()
        name = str(form.get("original_name") or "")
        values = {k: form.get(k) for k in ("name", "signature", "max_cost", "also_pdf", "output_folder",
                                           "skip_batches", "category", "note_type")}
        values["enabled"] = form.get("enabled") == "on"
        ok, problem, _final = auto.edit_sheet(name, values)
        if not ok:
            return go("/automation#sheets", problem, "warn")
        return go("/automation#sheets", "Saved. A backup of the previous list was kept.", "ok")

    @app.post("/automation/sheets/remove")
    async def remove_sheet(request: Request):
        form = await request.form()
        try:
            sheet_list.remove(str(form.get("name") or ""))
        except sheet_list.SheetListError as exc:
            return go("/automation#sheets", str(exc), "warn")
        return go("/automation#sheets", "Removed from the list. A backup was kept; the sheet itself is untouched.", "ok")

    @app.post("/automation/checks/{kind}")
    async def run_check(request: Request, kind: str):
        pdf_path = ""
        if kind == "probe":
            form = await request.form()
            upload = form.get("file")
            data = await upload.read() if hasattr(upload, "read") else b""
            if not data.lstrip()[:5].startswith(b"%PDF"):
                return go("/automation#checks", "Choose a lecture PDF to try.", "warn")
            handle = tempfile.NamedTemporaryFile(prefix="notecraft-probe-", suffix=".pdf", delete=False)
            handle.write(data)
            handle.close()
            pdf_path = handle.name
        ok, problem = auto.start_check(kind, pdf_path)
        if not ok:
            if pdf_path:
                Path(pdf_path).unlink(missing_ok=True)
            return go("/automation#checks", problem, "warn")
        return go("/automation#checks", f"{auto.CHECKS[kind]}: started.", "info")

    @app.get("/automation/checks/card")
    def check_card(request: Request):
        return render(request, "_automation_check.html", check=auto.check_state())
