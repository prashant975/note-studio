"""Audio notes from the app: a finished chapter's lecture PDFs, uploaded on New notes, become
one Smart Queue item and one run (the Notes lane makes it with engines/audio.py).

    submit_chapter   the form: every lecture PDF of one chapter (or a ZIP of them), put in lecture order
    unpack           a ZIP among the uploads -> the lecture PDFs inside it (Python's zipfile, no program)
    execute         the run: join, script, check, voice (or the voice alone for a fixed script)
    speak_script     the run page's "Make the audio": the script as the team fixed it, a new run
    retry            Try again / Make again; a run that stopped part way reuses its script and parts
    reuse            what a new run copies from the run it came from (launch calls it)
    script_for       the script a finished run shows on its page

Later the chapter's lectures will come from a sheet; for now they are uploaded here."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import db, job_queue, make_service, models, requirements
from .audio import chapter
from .audio import script as audio_script
from .audio import voice as audio_voice
from .engines import audio as audio_engine

TYPE = "audio_notes"
SOURCE = "audio"
MAX_FILES = 20
MAX_ZIP_MB = 1000                       # one ZIP of a whole chapter's lectures
SCRIPT_FILE = "script.txt"
_KEPT_OUT = ("file", "files", "lectures", "requirements", "queue_id", "sign_in_problem", "voice_only", "from_run")


def _names_and_voices(options: dict) -> None:
    """The friends' names and voices the form sent, or FormError in plain words."""
    first, second = (" ".join(str(options.get(k) or "").split()) for k in ("host_one", "host_two"))
    for label, name in (("first friend", first), ("second friend", second)):
        if not audio_script.valid_name(name):
            raise make_service.FormError(f"Write the {label}'s name in letters only (at most "
                                         f"{audio_script.MAX_NAME} characters), e.g. Raju.")
    if first.casefold() == second.casefold():
        raise make_service.FormError("Give the two friends different names.")
    voices = [str(options.get(k) or "") for k in ("voice_one", "voice_two")]
    if any(v not in audio_voice.VOICES for v in voices):
        raise make_service.FormError("Choose a voice for each friend.")
    if voices[0] == voices[1]:
        raise make_service.FormError("Choose two different voices, so the two friends sound different.")


def is_zip(name: str, data: bytes) -> bool:
    """By its name: a Word or PowerPoint file is a ZIP inside too, and stays "not a PDF"."""
    return str(name).strip().lower().endswith(".zip")


def unpack(files: list[tuple[str, bytes]], limit_mb: int = 100) -> list[tuple[str, bytes]]:
    """The uploads with each ZIP replaced by the PDFs inside it; other files pass unchanged.
    Folders the ZIP's PDFs all share (a zipped "Chapter 5" folder) are left out of the names;
    deeper folders lead the name ("Day 2 - notes.pdf"), so lecture order still follows them.
    Mac and hidden files are skipped. FormError in plain words for a ZIP that cannot be used."""
    out = []
    for name, data in files:
        if not is_zip(name, data):
            out.append((name, data))
            continue
        shown = make_service.safe_name(name)
        if len(data) > MAX_ZIP_MB * 1024 * 1024:
            raise make_service.FormError(f"{shown} is larger than {MAX_ZIP_MB} MB.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            entries = archive.infolist()
        except (zipfile.BadZipFile, OSError, ValueError):
            raise make_service.FormError(f"{shown} is not a ZIP file that opens. Make the ZIP again, "
                                         "or add the lecture PDFs themselves.") from None
        pdfs = []
        for info in entries:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if (info.is_dir() or not parts or not parts[-1].lower().endswith(".pdf")
                    or parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts)):
                continue
            if info.flag_bits & 0x1:
                raise make_service.FormError(f"{shown} is locked with a password. Unzip it and add the "
                                             "lecture PDFs themselves.")
            if info.file_size > limit_mb * 1024 * 1024:
                raise make_service.FormError(f"{parts[-1]} in {shown} is larger than {limit_mb} MB.")
            pdfs.append((parts, info))
        if not pdfs:
            raise make_service.FormError(f"{shown} has no PDF inside. Put the chapter's lecture PDFs in the ZIP.")
        if len(pdfs) > MAX_FILES:
            raise make_service.FormError(f"{shown} holds {len(pdfs)} PDFs; one chapter takes at most {MAX_FILES}.")
        shared = 0                                       # folders every PDF in the ZIP sits in
        while (all(len(p) > shared + 1 for p, _i in pdfs)
               and len({p[shared] for p, _i in pdfs}) == 1):
            shared += 1
        for parts, info in pdfs:
            try:
                with archive.open(info) as member:
                    body = member.read(limit_mb * 1024 * 1024 + 1)
            except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
                raise make_service.FormError(f"{parts[-1]} in {shown} could not be read. Make the ZIP again, "
                                             "or add the lecture PDFs themselves.") from None
            out.append((" - ".join(parts[shared:]), body))
    return out


def submit_chapter(category: str, note_type: str, subject: str, language: str, options: dict,
                   files: list[tuple[str, bytes]]) -> tuple[int, bool, int]:
    """The whole chapter as one Smart Queue item: its PDFs in lecture order (by their names,
    Lecture 2 before Lecture 10), a ZIP's PDFs among them. Returns (id, created, lectures);
    the same chapter pressed twice is added once."""
    found = make_service.check(category, note_type, subject, language, options)
    _names_and_voices(options)
    limit = int(found["type"].get("max_file_mb", 100))
    zips = [name for name, data in files if is_zip(name, data)]
    files = unpack(files, limit)
    if not files:
        raise make_service.FormError("Add the chapter's lecture PDFs: one PDF per lecture, one merged PDF, "
                                     "or one ZIP of them.")
    if len(files) > MAX_FILES:
        raise make_service.FormError(f"Add at most {MAX_FILES} lecture PDFs for one chapter.")
    if len(zips) == 1 and not str(options.get("chapter_title") or "").strip():
        stem = re.sub(r"\.zip$", "", make_service.safe_name(zips[0]), flags=re.I).strip()
        options = {**options, "chapter_title": stem[:120]} if stem else options   # the ZIP names the chapter
    digests = {}
    for name, data in files:
        shown = make_service.safe_name(name)
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise make_service.FormError(f"{shown} is not a PDF file.")
        if len(data) > limit * 1024 * 1024:
            raise make_service.FormError(f"{shown} is larger than {limit} MB.")
        digest = hashlib.sha256(data).hexdigest()
        if digest in digests:
            raise make_service.FormError(f"{digests[digest]} and {shown} are the same file. Add each lecture once.")
        digests[digest] = shown
    ordered = sorted(((make_service.safe_name(n), d) for n, d in files), key=lambda f: chapter.lecture_order(f[0]))
    stored = [f"{n:02d} {name}" for n, (name, _data) in enumerate(ordered, start=1)]
    kept = {**options, "files": stored, "lectures": [name for name, _data in ordered]}
    payload = {"category": category, "note_type": note_type, "subject": subject, "language": language,
               "options": kept, "file": stored[0]}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()
                         + "".join(hashlib.sha256(d).hexdigest() for _n, d in ordered).encode()).hexdigest()
    title = str(options.get("chapter_title") or "").strip() or re.sub(r"\.pdf$", "", ordered[0][0], flags=re.I)
    _id, created = job_queue.submit("new_notes", title[:150], payload,
                                    uploads=[(name, data) for name, (_n, data) in zip(stored, ordered)], key=key)
    return _id, created, len(ordered)


# --------------------------------------------------------------------- the run
def _settings(run: dict, options: dict) -> audio_engine.Settings:
    resolved = requirements.resolve(run["category"], run["note_type"], run["subject"], run["language"],
                                    extra=options.get("instructions", ""))
    order = {stage: models.order(run["note_type"], stage) for stage in models.stages(run["note_type"])}
    return audio_engine.run_settings(options, resolved.text(*requirements.APPLIED[audio_engine.ENGINE]), order)


def execute(run: dict) -> None:
    run_id = run["id"]
    options = json.loads(run["options_json"] or "{}")
    folder = Path(run["run_dir"]) if run["run_dir"] else make_service.run_dir(run_id)
    settings = _settings(run, options)

    def step(text, pct=None):
        make_service.event(run_id, "step", make_service.plain_step(text), pct)

    if options.get("voice_only"):
        source = folder / "input" / str(options.get("file") or "")
        if not source.is_file():
            make_service.finish(run_id, "failed", "The script of this request is missing.",
                                error="The script of this request is missing.")
            return
        step(f"Speaking your script: {settings.hosts[0]} as {settings.voices[0]}, "
             f"{settings.hosts[1]} as {settings.voices[1]}", 3)
        result = audio_engine.make_from_script(source.read_text(encoding="utf-8"), folder, settings,
                                               token_provider=make_service.typeset_token, step=step)
    else:
        stored = [str(n) for n in options.get("files") or []]
        shown = [str(n) for n in options.get("lectures") or []] or stored
        paths = [folder / "input" / name for name in stored]
        if not paths or not all(p.is_file() for p in paths):
            make_service.finish(run_id, "failed", "A lecture PDF of this chapter is missing.",
                                error="A lecture PDF of this chapter is missing.")
            return
        step(f"Making audio notes from {len(paths)} lecture PDF{'s' if len(paths) != 1 else ''} "
             f"(AI model: {(settings.models.get('notes') or ['the default'])[0]})", 3)
        result = audio_engine.make([(name, p.read_bytes()) for name, p in zip(shown, paths)], folder, settings,
                                   token_provider=make_service.typeset_token, step=step)
    _record(run_id, options, folder, result)


def _record(run_id: int, options: dict, folder: Path, result: audio_engine.Result) -> None:
    now = time.time()
    for position, item in enumerate(result.outputs, start=1):
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, position, item["label"], "complete", item["path"],
                                                 0, now, now))
        make_service.event(run_id, "output", item["label"], None, path=item["path"], file_type=item["kind"])
    if result.report:
        report_path = folder / "quality_report.json"
        report_path.write_text(json.dumps(result.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, len(result.outputs) + 1, "Quality report.json", "complete",
                                                 str(report_path), 0, now, now))
        make_service.event(run_id, "output", "Quality report", None, path=str(report_path), file_type="json")
    if result.state != "complete":
        if result.failure == "sign_in":
            db.execute("UPDATE runs SET options_json=? WHERE id=?",
                       (json.dumps({**options, "sign_in_problem": True}, ensure_ascii=False), run_id))
        make_service.finish(run_id, "failed", result.message, error=result.message, detail=result.detail,
                            cost=result.cost_inr)
        return
    if result.script_only:
        state, message = "complete", "The script is ready. Read it below, fix any line, then press Make the audio."
    else:
        state = "complete"
        message = (f"Done: {audio_engine.spoken_length(result.seconds)} of audio, and its script."
                   if result.seconds else "Done: the audio and its script.")
    if result.report.get("verdict") != "passed":
        state, message = "needs_attention", message + " Review the quality report: some checks need attention."
    make_service.finish(run_id, state, message, cost=result.cost_inr)


# --------------------------------------------------------------------- again, and the fixed script
def _run(run_id: int) -> dict | None:
    rows = db.query("SELECT * FROM runs WHERE id=?", (run_id,))
    return rows[0] if rows and rows[0]["engine"] == audio_engine.ENGINE else None


def retry(run_id: int) -> int | None:
    """The same chapter (or the same fixed script) with the same choices, queued again."""
    run = _run(run_id)
    if not run or run["state"] in ("waiting", "running"):
        return None
    options = json.loads(run["options_json"] or "{}")
    stored = [str(n) for n in options.get("files") or []] if not options.get("voice_only") \
        else [str(options.get("file") or "")]
    paths = [Path(run["run_dir"] or "") / "input" / name for name in stored if name]
    if not paths or not all(p.is_file() for p in paths):
        return None
    kept = {k: v for k, v in options.items() if k not in ("file", "requirements", "queue_id", "sign_in_problem")}
    payload = {"category": run["category"], "note_type": run["note_type"], "subject": run["subject"],
               "language": run["language"], "options": kept, "file": stored[0], "again_from": run_id}
    entry_id, _created = job_queue.submit("new_notes", run["title"], payload,
                                          uploads=[(p.name, p.read_bytes()) for p in paths], key=f"again-{run_id}")
    return entry_id


def speak_script(run_id: int, text: str) -> tuple[int, bool]:
    """The script as the team fixed it, spoken in a new run (only the voice is paid for; parts
    whose lines did not change are reused). ScriptError/FormError in plain words."""
    run = _run(run_id)
    if not run:
        raise make_service.FormError("That run no longer exists.")
    if run["state"] in ("waiting", "running"):
        raise make_service.FormError("Wait until this run has finished.")
    options = json.loads(run["options_json"] or "{}")
    settings = audio_engine.run_settings(options, "", {})
    try:
        doc = audio_script.parse(text, settings.hosts)
    except audio_script.ScriptError as problem:
        raise make_service.FormError(str(problem)) from None
    normalized = audio_script.to_text(doc, settings.hosts, settings.teacher)
    kept = {k: v for k, v in options.items() if k not in _KEPT_OUT}
    kept.update(voice_only=True, from_run=run_id)
    payload = {"category": run["category"], "note_type": run["note_type"], "subject": run["subject"],
               "language": run["language"], "options": kept, "file": SCRIPT_FILE, "parts_from": run_id}
    key = "speak-" + hashlib.sha256(json.dumps([run_id, normalized, settings.voices]).encode()).hexdigest()
    title = run["title"] if run["title"].endswith("(audio)") else f"{run['title']} (audio)"
    return job_queue.submit("new_notes", title[:150], payload, uploads=[(SCRIPT_FILE, normalized.encode("utf-8"))],
                            key=key)


def reuse(payload: dict, folder: Path, run_id: int) -> None:
    """A new run starts with what the run it came from paid for: the spoken parts (and, for Try
    again after a run that stopped part way, the script). The engine uses only what still fits."""
    source = payload.get("again_from") or payload.get("parts_from")
    old = _run(int(source)) if source else None
    if not old or not old["run_dir"]:
        return
    prior = Path(old["run_dir"])
    copied = []
    if payload.get("again_from") and old["state"] in ("failed", "needs_attention", "stopped"):
        for name in ("script.json", "script_raw.json"):
            if (prior / name).is_file():
                shutil.copy2(prior / name, folder / name)
                copied.append("the script")
    parts = prior / "audio_parts"
    if parts.is_dir() and (payload.get("parts_from") or old["state"] in ("failed", "needs_attention", "stopped")):
        target = folder / "audio_parts"
        target.mkdir(parents=True, exist_ok=True)
        for item in parts.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
        copied.append("the parts already spoken")
    if copied:
        words = " and ".join(dict.fromkeys(copied))
        make_service.event(run_id, "step", f"Reusing {words} from the earlier run where they still fit", 1)


def script_for(run: dict) -> str:
    """The script text of a finished audio run, for its page ("" when it has none)."""
    if run.get("engine") != audio_engine.ENGINE or run.get("state") in ("waiting", "running"):
        return ""
    for item in db.query("SELECT output_path FROM run_items WHERE run_id=? ORDER BY position", (run["id"],)):
        path = Path(item["output_path"] or "")
        if path.name.endswith("Audio script.txt") and path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""
