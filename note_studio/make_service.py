"""New notes: one Smart Queue item per lecture PDF, or per class (recording link
+ optional slides); its run is made when its turn comes.

The request (choices + its files) waits in ~/.note_studio/queue/<id>/. When the
Notes lane reaches it, a run is created in ~/.note_studio/runs/<id>/ (input/
holds the lecture or slides) and its engine makes it with this run's own AI
model order: the hand-drawn notes engine in this process, the UPSC engine as a
child process. The run, its steps and its files are kept in the database, so
Runs survives a restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path

from . import categories, db, engine_host, job_queue, models, note_types, paths, requirements, signin
from .engines import pw_books as pw_books_engine
from .engines import shortnotes as shortnotes_engine
from .engines import typeset as typeset_engine
from .engines import upsc as upsc_engine

# Engines that run as a child process: (adapter, requirement layers sent as the
# extra block, the engine's name in plain words).
CHILD_ENGINES = {
    upsc_engine.ENGINE: (upsc_engine, ("type", "category", "subject", "category_type", "profile", "run"), "UPSC"),
    pw_books_engine.ENGINE: (pw_books_engine, pw_books_engine.EXTRA_LAYERS, "PW Books"),
}

log = logging.getLogger("note_studio.make")
# Tests replace these to run without signing in or paying: ensure_proxy(model,
# fallbacks) for the hand-drawn notes; class_token() for the UPSC engine's child;
# typeset_token() for the typeset notes (the account's PW token provider).
ensure_proxy = signin.ensure_proxy
class_token = signin.google_token
typeset_token = signin.proxy_token_provider

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_LINK = re.compile(r"^(https?://\S+|[A-Za-z0-9_-]{11})$")
SLIDE_KINDS = {".pdf": (b"%PDF",), ".pptx": (b"PK\x03\x04",), ".ppt": (b"\xd0\xcf\x11\xe0",)}


class FormError(ValueError):
    """A plain-words problem with what was submitted."""


def runs_root() -> Path:
    path = paths.home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(run_id: int) -> Path:
    return runs_root() / str(int(run_id))


def safe_name(name: str) -> str:
    cleaned = " ".join(_UNSAFE.sub("", str(name or "")).split()).strip(". ")
    return (cleaned or "lecture.pdf")[:150]


# ------------------------------------------------------------------ submit
def check(category: str, note_type: str, subject: str, language: str, options: dict) -> dict:
    """Validate a New notes request; returns the pack and type, or raises FormError."""
    pack = categories.get(category)
    if not pack or pack.get("enabled") is not True:
        raise FormError("Choose a category.")
    entry = note_types.get(note_type)
    if not entry or note_type not in (pack.get("note_types") or []):
        raise FormError("Choose a note type this category offers.")
    if not note_types.available(note_type):
        raise FormError(f"{entry['label']} is not in this installation.")
    if entry.get("status") != "ready":
        raise FormError(f"{entry['label']} is not ready yet in this version.")
    if "finished_run" in (entry.get("inputs") or []):
        raise FormError(f"{entry['label']} is made from finished notes: open Practice.")
    if subject not in {s["id"] for s in categories.subject_choices(pack, entry)}:
        raise FormError("Choose a subject.")
    if language not in {l["id"] for l in categories.language_choices(pack, entry)}:
        raise FormError("Choose a language.")
    for name, spec in (entry.get("options") or {}).items():
        value = options.get(name)
        if spec.get("required") and not str(value or "").strip():
            raise FormError(f"Fill in {spec.get('label', name).lower()}.")
        if spec.get("max") and isinstance(value, str) and len(value) > int(spec["max"]):
            raise FormError(f"{spec.get('label', name)} is too long (at most {spec['max']} characters).")
        if spec.get("min_words") and isinstance(value, str) and value.strip() and len(value.split()) < int(spec["min_words"]):
            raise FormError(f"{spec.get('label', name)} is too short. Paste the full lecture transcript "
                            f"(at least {spec['min_words']} words).")
    return {"pack": pack, "type": entry}


def submit(category: str, note_type: str, subject: str, language: str, options: dict,
           files: list[tuple[str, bytes]]) -> list[tuple[int, bool]]:
    """One Smart Queue item per PDF. Returns [(item id, newly added)]; the same
    request pressed twice is added once."""
    found = check(category, note_type, subject, language, options)
    decks = "pptx_upload" in (found["type"].get("inputs") or [])
    word = "docx_upload" in (found["type"].get("inputs") or [])
    if not files:
        raise FormError("Add at least one lecture PDF" + (" or PowerPoint." if decks else " or Word reference." if word else "."))
    limit = int(found["type"].get("max_file_mb", 100)) * 1024 * 1024
    for name, data in files:
        suffix = Path(safe_name(name)).suffix.lower()
        if decks:
            if suffix not in SLIDE_KINDS or not data.lstrip()[:8].startswith(SLIDE_KINDS[suffix]):
                raise FormError(f"{safe_name(name)} is not a PDF or PowerPoint file.")
        elif word and suffix == ".docx":
            # Validate the package before queueing; it must be an actual Word file.
            import io
            from zipfile import BadZipFile, ZipFile

            try:
                with ZipFile(io.BytesIO(data)) as archive:
                    if "word/document.xml" not in archive.namelist():
                        raise BadZipFile()
            except BadZipFile:
                raise FormError(f"{safe_name(name)} is not a valid Word reference.") from None
        elif not data.lstrip()[:5].startswith(b"%PDF"):
            raise FormError(f"{safe_name(name)} is not a PDF file.")
        if len(data) > limit:
            raise FormError(f"{safe_name(name)} is larger than {found['type'].get('max_file_mb', 100)} MB.")
    added = []
    for name, data in files:
        name = safe_name(name)
        payload = {"category": category, "note_type": note_type, "subject": subject, "language": language,
                   "options": options, "file": name}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + hashlib.sha256(data).digest()).hexdigest()
        title = re.sub(r"\.(pdf|pptx?|docx)$", "", name, flags=re.I)
        added.append(job_queue.submit("new_notes", title, payload, uploads=[(name, data)], key=key))
    return added


def submit_class(category: str, note_type: str, subject: str, language: str, options: dict,
                 video_url: str, slides: tuple[str, bytes] | None) -> tuple[int, bool]:
    """One class (UPSC): the recording link, and the slides if there are any.
    As in the old app, the link is required and the slides are optional."""
    found = check(category, note_type, subject, language, options)
    video_url = str(video_url or "").strip()
    if not video_url:
        raise FormError("Paste the class recording link (Google Drive, YouTube or a direct video link).")
    if not _LINK.match(video_url) or len(video_url) > 2000:
        raise FormError("That is not a link. Paste the class recording's Google Drive, YouTube or video address.")
    uploads, slide_name = [], ""
    if slides and slides[1]:
        slide_name, data = safe_name(slides[0]), slides[1]
        suffix = Path(slide_name).suffix.lower()
        if suffix not in SLIDE_KINDS or not data.startswith(SLIDE_KINDS[suffix]):
            raise FormError(f"{slide_name} is not a PDF or PowerPoint file.")
        limit = int(found["type"].get("max_file_mb", 200))
        if len(data) > limit * 1024 * 1024:
            raise FormError(f"{slide_name} is larger than {limit} MB.")
        uploads = [(slide_name, data)]
    payload = {"category": category, "note_type": note_type, "subject": subject, "language": language,
               "options": {**options, "video_url": video_url}, "file": slide_name}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
    for _name, data in uploads:
        digest.update(hashlib.sha256(data).digest())
    title = str(options.get("lecture_title") or "").strip() or (Path(slide_name).stem if slide_name else "Class recording")
    return job_queue.submit("new_notes", title[:150], payload, uploads=uploads, key=digest.hexdigest())


# ----------------------------------------------------------------- events
_CALL_COST = re.compile(r"^.*?: (?P<kind>[\w ,'-]+?) call - INR (?P<cost>[\d.]+)$")
_CALL_EVENT = re.compile(r"^.*?: (?P<kind>[\w ,'-]+?) call (?P<what>.+)$")


def plain_step(text: str) -> str:
    """The engine's per-call log lines ("lecture: notes call - INR 4.20") in plain words."""
    found = _CALL_COST.match(text or "")
    if found:
        return f"{found['kind'].capitalize()} done (₹{float(found['cost']):.2f})"
    found = _CALL_EVENT.match(text or "")
    if found:
        what = found["what"]
        if what.startswith("start"):
            return f"Asking PW AI: {found['kind']}"
        if what.startswith("budget spent"):
            return "That AI model's budget is spent; " + what.split(";", 1)[-1].strip().replace("using", "switched to")
        if what.startswith(("fail", "error")):
            return f"{found['kind'].capitalize()}: PW AI did not answer; trying again"
        return f"{found['kind'].capitalize()}: {what}"
    return text


def event(run_id: int, kind: str, text: str, pct: int | None = None, **data) -> None:
    db.execute("INSERT INTO run_events (run_id, at, kind, text, pct, data_json) VALUES (?,?,?,?,?,?)",
               (run_id, time.time(), kind, str(text)[:500], pct, json.dumps(data, ensure_ascii=False)))
    if kind == "step":
        if pct is None:
            db.execute("UPDATE runs SET message=? WHERE id=?", (str(text)[:300], run_id))
        else:
            db.execute("UPDATE runs SET message=?, pct=MAX(pct, ?) WHERE id=?", (str(text)[:300], int(pct), run_id))


def finish(run_id: int, state: str, message: str, *, error: str = "", detail: str = "", cost: float = 0.0) -> None:
    db.execute("UPDATE runs SET state=?, message=?, error=?, finished_at=?, cost_inr=?, "
               "pct=CASE WHEN ?='complete' THEN 100 ELSE pct END WHERE id=?",
               (state, message, error, time.time(), cost, state, run_id))
    event(run_id, "done", message, None, status=state, detail=detail)


# ------------------------------------------------------------- the lane
def launch(entry: dict) -> tuple[str, str, int | None]:
    """The Notes lane's launcher: create the run from the queued request and make it.
    Returns (queue state, note, run id); "signin" holds the queue."""
    payload = entry["payload"]
    if payload.get("run_id"):                           # a run added before the Smart Queue existed
        run_id = int(payload["run_id"])
    else:
        name = str(payload.get("file") or "")
        source = next((p for p in job_queue.files(entry) if p.name == name), None) if name else None
        if name and source is None:
            return "failed", "The file of this request is missing.", None
        engine = (note_types.get(payload["note_type"]) or {}).get("engine", "notesbot")
        resolved = requirements.resolve(payload["category"], payload["note_type"], payload["subject"],
                                        payload["language"], extra=payload["options"].get("instructions", ""))
        linked = bool(payload["options"].get("source_id")) and not name      # a Google Doc link (typeset_gdoc)
        run_id = db.create_run(engine, title=entry["title"], note_type=payload["note_type"],
                               category=payload["category"], subject=payload["subject"], language=payload["language"],
                               source="gdoc" if linked else "upload", options={**payload["options"], "file": name,
                                                         "requirements": resolved.to_dict(), "queue_id": entry["id"]})
        folder = run_dir(run_id)
        (folder / "input").mkdir(parents=True, exist_ok=True)
        if source is not None:
            shutil.copy2(source, folder / "input" / source.name)
        if engine == typeset_engine.ENGINE and payload.get("again_from") and source is not None:
            # A failed rendering/transcription retry keeps checked pages and paid
            # proposals, but only for the identical PDF and transcription prompt.
            from .typeset import transcribe

            old_runs = db.query("SELECT * FROM runs WHERE id=?", (int(payload["again_from"]),))
            if old_runs and old_runs[0]["state"] in {"failed", "needs_attention"}:
                prior = Path(old_runs[0]["run_dir"]) / "transcription"
                marker = prior / "source.json"
                if marker.is_file() and json.loads(marker.read_text("utf-8")) == transcribe.cache_identity(source.read_bytes()):
                    destination = folder / "transcription"
                    destination.mkdir(exist_ok=True)
                    for cached in prior.glob("*.json"):
                        if cached.name == "source.json" or re.fullmatch(r"page-\d{4}(?:-review)?\.json", cached.name):
                            shutil.copy2(cached, destination / cached.name)
                    event(run_id, "step", "Reusing saved AI pages from the previous attempt", 1)
                if source.suffix.lower() == ".docx":
                    from .typeset import word_source

                    saved_style = Path(old_runs[0]["run_dir"]) / "word_styles.json"
                    if saved_style.is_file() and json.loads(saved_style.read_text("utf-8")).get("identity") == word_source.style_identity(source.read_bytes()):
                        shutil.copy2(saved_style, folder / "word_styles.json")
                        event(run_id, "step", "Reusing saved AI formatting from the previous attempt", 1)
                saved_review = Path(old_runs[0]["run_dir"]) / "final_quality_review.json"
                if saved_review.is_file() and json.loads(saved_review.read_text("utf-8")).get("identity", {}).get("source_sha256") == hashlib.sha256(source.read_bytes()).hexdigest():
                    shutil.copy2(saved_review, folder / "final_quality_review.json")
        db.execute("UPDATE runs SET run_dir=?, queue_id=? WHERE id=?", (str(folder), entry["id"], run_id))
        event(run_id, "step", "Its turn in the Smart Queue came", 1)
    db.execute("UPDATE runs SET state='running', started_at=?, pct=2 WHERE id=?", (time.time(), run_id))
    try:
        execute(db.query("SELECT * FROM runs WHERE id=?", (run_id,))[0])
    except Exception as exc:
        log.exception("run %s", run_id)
        finish(run_id, "failed", "Something went wrong while making these notes.", detail=type(exc).__name__)
    run = db.query("SELECT state, message, error, options_json FROM runs WHERE id=?", (run_id,))[0]
    if run["state"] in ("complete", "needs_attention"):
        return "done", run["message"], run_id
    if run["state"] == "stopped":
        return "stopped", run["message"], run_id
    if json.loads(run["options_json"] or "{}").get("sign_in_problem"):
        return "signin", run["error"], run_id
    return "failed", run["error"] or run["message"], run_id


def execute(run: dict) -> None:
    """Make one run's notes with its engine."""
    if run["engine"] in CHILD_ENGINES:
        execute_child(run)
        return
    if run["note_type"] == "typeset_gdoc":
        from . import gdoc_service

        gdoc_service.execute(run)
        return
    if run["engine"] == typeset_engine.ENGINE:
        execute_typeset(run)
        return
    run_id = run["id"]
    options = json.loads(run["options_json"] or "{}")
    folder = Path(run["run_dir"]) if run["run_dir"] else run_dir(run_id)
    source = folder / "input" / options.get("file", "")
    if not source.is_file():
        finish(run_id, "failed", "The uploaded lecture file is missing.", error="The uploaded lecture file is missing.")
        return
    resolved = requirements.resolve(run["category"], run["note_type"], run["subject"], run["language"],
                                    extra=options.get("instructions", ""))
    # Layers 3-4 (category, subject) the engine applies itself from the packs.
    instructions = resolved.text("type", "category_type", "profile", "run")
    settings = shortnotes_engine.run_settings(options, run["category"], run["subject"], run["language"], instructions)
    order = models.order(run["note_type"]) or [""]
    event(run_id, "step", f"Reading the lecture PDF (AI model: {order[0] or 'the default'})", 5)
    result = shortnotes_engine.make_notes(
        source.read_bytes(), source.name, folder, settings,
        ensure_proxy=lambda: ensure_proxy(order[0], order[1:]),
        step=lambda text, pct=None: event(run_id, "step", plain_step(text), pct))
    if result.state == "complete":
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, 1, result.filename, "complete", result.output_path,
                                                 result.cost_inr, time.time(), time.time()))
        event(run_id, "output", result.filename, None, path=result.output_path, file_type="pdf", pages=result.pages)
        state = "complete"
        message = f"Done: {result.pages} page{'s' if result.pages != 1 else ''} of notes."
        if result.quality_report:
            report_path = folder / "quality_report.json"
            report_path.write_text(json.dumps(result.quality_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            event(run_id, "output", "Quality check report", None, path=str(report_path), file_type="json")
            if result.quality_report.get("verdict") != "passed":
                state = "needs_attention"
                message += " Review the quality report: some checks need attention."
        finish(run_id, state, message,
               cost=result.cost_inr)
        return
    if result.failure == "sign_in":
        db.execute("UPDATE runs SET options_json=? WHERE id=?",
                   (json.dumps({**options, "sign_in_problem": True}, ensure_ascii=False), run_id))
    finish(run_id, "failed", result.message, error=result.message, detail=result.detail, cost=result.cost_inr)


def execute_typeset(run: dict) -> None:
    """One uploaded lecture through the B.Sc. engine. Source preservation is
    local; only optional AI rewriting applies generation rules and model calls."""
    run_id = run["id"]
    options = json.loads(run["options_json"] or "{}")
    folder = Path(run["run_dir"]) if run["run_dir"] else run_dir(run_id)
    source = folder / "input" / options.get("file", "")
    if not source.is_file():
        finish(run_id, "failed", "The uploaded lecture file is missing.", error="The uploaded lecture file is missing.")
        return
    resolved = requirements.resolve(run["category"], run["note_type"], run["subject"], run["language"],
                                    extra=options.get("instructions", ""))
    order = {stage: models.order(run["note_type"], stage) for stage in models.stages(run["note_type"])}
    settings = typeset_engine.run_settings(options, categories.get(run["category"]) or {}, run["subject"],
                                           run["language"], resolved.text(*requirements.LAYERS[1:]), order)
    event(run_id, "step", "Reading the lecture PDF and preserving its wording" if settings.content_mode == "preserve"
          else f"Reading the lecture PDF (AI model: {(order.get('notes') or ['the default'])[0]})", 5)
    result = typeset_engine.make_notes(source.read_bytes(), source.name, folder, settings,
                                       token_provider=typeset_token,
                                       step=lambda text, pct=None: event(run_id, "step", plain_step(text), pct))
    if result.state != "complete":
        if result.failure == "sign_in":
            _sign_in_problem(run_id, options)
        finish(run_id, "failed", result.message, error=result.message, detail=result.detail, cost=result.cost_inr)
        return
    db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
               " VALUES (?,?,?,?,?,?,?,?)", (run_id, 1, result.filename, "complete", result.output_path,
                                             result.cost_inr, time.time(), time.time()))
    event(run_id, "output", result.filename, None, path=result.output_path, file_type="pdf", pages=result.pages)
    report_path = folder / "quality_report.json"
    report_path.write_text(json.dumps(result.quality_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    event(run_id, "output", "Quality check report", None, path=str(report_path), file_type="json")
    if settings.content_mode != "rewrite":
        event(run_id, "output", "Source content and order record", None,
              path=str(folder / "source_map.json"), file_type="json")
    state, message = "complete", f"Done: {result.pages} page{'s' if result.pages != 1 else ''} of notes."
    if result.quality_report.get("verdict") != "passed":
        state, message = "needs_attention", message + " Review the quality report: some checks need attention."
    finish(run_id, state, message, cost=result.cost_inr)


def _sign_in_problem(run_id: int, options: dict) -> None:
    db.execute("UPDATE runs SET options_json=? WHERE id=?",
               (json.dumps({**options, "sign_in_problem": True}, ensure_ascii=False), run_id))


def execute_child(run: dict) -> None:
    """One lecture (UPSC class or PW Books deck) through its engine, in its own process."""
    run_id = run["id"]
    adapter, layers, words = CHILD_ENGINES[run["engine"]]
    options = json.loads(run["options_json"] or "{}")
    folder = Path(run["run_dir"]) if run["run_dir"] else run_dir(run_id)
    if options.get("file") and not (folder / "input" / options["file"]).is_file():
        finish(run_id, "failed", "The uploaded file is missing.", error="The uploaded file is missing.")
        return
    resolved = requirements.resolve(run["category"], run["note_type"], run["subject"], run["language"],
                                    extra=options.get("instructions", ""))
    extra = resolved.text(*layers)
    try:
        token = class_token()
    except signin.SignInNeeded as problem:
        _sign_in_problem(run_id, options)
        finish(run_id, "failed", str(problem), error=str(problem))
        return
    request = adapter.build_request(run, options, folder, extra, resolved.to_dict())
    order = request["models"].get("notes") or [""]
    event(run_id, "step", f"Starting the {words} engine (AI model: {order[0] or 'the default'})", 2)
    result = adapter.make_notes(
        run_id, folder, request, token=token,
        step=lambda text, pct=None: event(run_id, "step", text, pct),
        on_cost=lambda total: db.execute("UPDATE runs SET cost_inr=? WHERE id=?", (total, run_id)))
    for text in result.warnings:
        event(run_id, "warning", text)
    for position, item in enumerate(result.outputs, start=1):
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, position, Path(item["path"]).name, "complete", item["path"],
                                                 0, time.time(), time.time()))
        event(run_id, "output", Path(item["path"]).name, None, path=item["path"], file_type=item["kind"],
              label=item["label"])
    if result.sign_in:
        _sign_in_problem(run_id, options)
    error = "" if result.state in ("complete", "needs_attention") else result.message
    finish(run_id, result.state, result.message, error=error, detail=result.detail, cost=result.cost_inr)


# --------------------------------------------------------------- controls
def stop(run_id: int) -> bool:
    """Stop a lecture while a child-process engine makes it (its whole process
    tree), and hold the Smart Queue so nothing new starts until Continue."""
    if not engine_host.running(run_id):
        return False
    job_queue.hold("stop")
    return engine_host.stop(run_id)

def retry(run_id: int) -> int | None:
    """The same lecture with the same choices, added to the Smart Queue again."""
    rows = db.query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows or rows[0]["state"] in ("waiting", "running") or rows[0]["source"] not in ("upload", "gdoc"):
        return None
    run = rows[0]
    options = json.loads(run["options_json"] or "{}")
    if run["source"] == "gdoc":
        # A Google Doc link: the same link and choices, made again (a new Doc).
        if not options.get("source_id"):
            return None
        kept = {k: v for k, v in options.items() if k not in ("file", "requirements", "queue_id", "sign_in_problem")}
        payload = {"category": run["category"], "note_type": run["note_type"], "subject": run["subject"],
                   "language": run["language"], "options": kept, "file": "", "again_from": run_id}
        entry_id, _ = job_queue.submit("new_notes", run["title"], payload, key=f"again-{run_id}")
        return entry_id
    name = str(options.get("file") or "")
    source = Path(run["run_dir"]) / "input" / name if name else None
    if (source is not None and not source.is_file()) or (source is None and run["engine"] != upsc_engine.ENGINE):
        # (only a UPSC class may have no file: its recording link is the input)
        return None
    kept = {k: v for k, v in options.items() if k not in ("file", "requirements", "queue_id", "sign_in_problem")}
    payload = {"category": run["category"], "note_type": run["note_type"], "subject": run["subject"],
               "language": run["language"], "options": kept, "file": name, "again_from": run_id}
    uploads = [(source.name, source.read_bytes())] if source is not None else []
    entry_id, _ = job_queue.submit("new_notes", run["title"], payload, uploads=uploads, key=f"again-{run_id}")
    return entry_id


def recover() -> None:
    """Runs added before the Smart Queue existed (still waiting) join it, once;
    like everything waiting after a restart, they wait for Continue."""
    joined = 0
    for run in db.query("SELECT id, title FROM runs WHERE state='waiting' AND source='upload'"):
        _entry, created = job_queue.submit("new_notes", run["title"], {"run_id": run["id"]}, key=f"run-{run['id']}")
        joined += created
    if joined:
        job_queue.hold("restart")


job_queue.register("new_notes", launch)
