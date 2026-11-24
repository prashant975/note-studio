"""New notes: category -> note type -> subject -> language -> lecture PDFs -> options -> Make notes."""
from __future__ import annotations

from fastapi import Request

from . import categories, job_queue, make_service, note_types, requirements, setup_check, signin


def _form_context(category: str = "", note_type: str = "", values: dict | None = None) -> dict:
    packs = [p for p in categories.all_packs(include_disabled=False) if categories.offered_types(p)]
    pack = next((p for p in packs if p["id"] == category), None) or (packs[0] if packs else None)
    types = categories.offered_types(pack) if pack else []
    # Practice types are made from finished notes, on the Practice page.
    ready = [t for t in types if t.get("status") == "ready" and "finished_run" not in (t.get("inputs") or [])]
    chosen = next((t for t in ready if t["id"] == note_type), None) \
        or next((t for t in ready if pack and t["id"] == pack.get("default_note_type")), None) \
        or (ready[0] if ready else None)
    values = dict(values or {})
    defaults = note_types.option_defaults(chosen["id"]) if chosen else {}
    preview = None
    if pack and chosen:
        preview = requirements.resolve(pack["id"], chosen["id"], values.get("subject", "auto"),
                                       values.get("language", "auto"))
    options = [(name,spec) for name,spec in ((chosen or {}).get("options") or {}).items() if not spec.get("hidden")]
    advanced = [(name, spec) for name, spec in options if spec.get("advanced")]
    return {"queue_counts": job_queue.counts(), "hold_words": job_queue.hold_words(),
            "packs": packs, "pack": pack, "types": types, "chosen": chosen,
            "subjects": categories.subject_choices(pack, chosen) if pack else [],
            "languages": categories.language_choices(pack, chosen) if pack and chosen else [],
            "values": {**defaults, **values}, "preview": preview, "signin": signin.snapshot(),
            "basic_options": [(name, spec) for name, spec in options if not spec.get("advanced")],
            "advanced_options": advanced, "advanced_open": any(values.get(name) for name, _spec in advanced),
            "no_pdf_maker": bool(chosen and "pdf" in (chosen.get("outputs") or []) and chosen.get("engine") == "pw_books"
                                 and not setup_check.office())}


def register(app, render, go):
    @app.get("/new")
    def new_notes(request: Request, category: str = "", type: str = ""):
        return render(request, "new.html", **_form_context(category, type))

    @app.get("/new/rules")
    def rules_card(request: Request, category: str = "", note_type: str = "", subject: str = "auto"):
        context = _form_context(category, note_type, {"subject": subject})
        if not context["preview"]:
            return render(request, "_rules_card_empty.html")
        return render(request, "_rules_card.html", preview=context["preview"])

    @app.post("/new")
    async def make_notes(request: Request):
        form = await request.form()
        category = str(form.get("category", ""))
        note_type = str(form.get("note_type", ""))
        entry = note_types.get(note_type) or {}
        options = {}
        for name, spec in (entry.get("options") or {}).items():
            options[name] = form.get(name) == "on" if spec.get("type") == "bool" else str(form.get(name, "")).strip()
        values = {"subject": str(form.get("subject", "auto")), "language": str(form.get("language", "auto")),
                  "video_url": str(form.get("video_url", "")).strip(),
                  "doc_links": str(form.get("doc_links", "")).strip()[:20000], **options}
        try:
            if "gdoc_link" in (entry.get("inputs") or []):
                from . import gdoc_service

                added = gdoc_service.submit_links(category, note_type, values["subject"], values["language"], options,
                                                  values["doc_links"])
                new = sum(1 for _id, created in added if created)
                if not new:
                    return go("/queue", "Already in the Smart Queue: it was not added twice.", "info")
                words = "1 Google Doc" if new == 1 else f"{new} Google Docs"
                return go("/queue", f"Added {words} to the Smart Queue; each is made in turn.", "ok")
            if "video_link" in (entry.get("inputs") or []):
                slides = form.get("slides")
                upload = (slides.filename, await slides.read()) if hasattr(slides, "read") and slides.filename else None
                _id, created = make_service.submit_class(category, note_type, values["subject"], values["language"],
                                                         options, values["video_url"], upload)
                if not created:
                    return go("/queue", "Already in the Smart Queue: it was not added twice.", "info")
                return go("/queue", "Added the class to the Smart Queue; it is made when its turn comes.", "ok")
            uploads = []
            for item in form.getlist("files"):
                if hasattr(item, "read") and getattr(item, "filename", ""):
                    uploads.append((item.filename, await item.read()))
            added = make_service.submit(category, note_type, values["subject"], values["language"], options, uploads)
        except make_service.FormError as problem:
            return render(request, "new.html", status_code=400, problem=str(problem),
                          **_form_context(category, note_type, values))
        new = sum(1 for _id, created in added if created)
        if not new:
            return go("/queue", "Already in the Smart Queue: it was not added twice.", "info")
        words = "1 lecture" if new == 1 else f"{new} lectures"
        return go("/queue", f"Added {words} to the Smart Queue; each is made in turn.", "ok")
