"""Google Docs with tabs (B.Sc. style), from the app: New notes (Google Doc links) and a sheet's rows.

The note type "typeset_gdoc" turns a final-notes Google Doc into an editable Google Doc in the B.Sc. look,
with every word kept (typeset/gdoc_make.py). Two ways in:

- New notes: paste Google Doc links, one per line; each becomes its own Smart Queue item in the Notes lane.
- Sheet automation: a tab whose rows link a Google Doc each (downloadable_link). Check rows reads the tab
  (free); Make Google Docs adds ONE item to the Sheets lane that makes the chosen rows still without a Doc,
  a few at a time, and writes each Doc's link into the row's "Google Doc (B.Sc. style)" column only after
  the Doc passed the word-for-word, watermark and logo checks.

Docs are made by the signed-in Google account (NoteCraft's sign-in), and shared so anyone at that
account's domain can edit them with the link (switch "share" off to keep them private).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import categories, db, gdoc_sheet, job_queue, make_service, models, signin
from .engines import typeset as typeset_engine
from .typeset import ai, gdoc_make, render, source

log = logging.getLogger("note_studio.gdoc")
TYPE = "typeset_gdoc"
SHEET_KIND = "gdoc_sheet"
PARALLEL = 3                     # notes made at the same time in a sheet run (as the 16-24 Nov batches)
MAX_LINKS = 50
_stops: set[int] = set()
_stop_lock = threading.Lock()

# Tests replace these: the signed-in account's Google credentials and email, and the maker itself.
make_doc = gdoc_make.make


def account():
    current = signin.auth()
    found = current.account() if current is not None else None
    if found is None:
        raise signin.SignInNeeded("Sign in with Google in Settings, then press Try again.")
    return found


def google_creds():
    return account().creds


def account_domain() -> str:
    email = str(getattr(account(), "email", "") or "")
    return email.rsplit("@", 1)[1].lower() if "@" in email else ""


def sheets_client():
    return gdoc_make.Google(google_creds()).sheets


def is_gdoc(note_type: str) -> bool:
    return note_type == TYPE


def branding(pack: dict | None) -> render.Branding:
    """The category's header and footer words and PW logo (as its B.Sc.-style PDFs)."""
    words = typeset_engine.run_settings({}, pack or {}, "", "", "", {}).branding
    return render.Branding(**words)


def model_order(note_type: str = TYPE) -> tuple[list[str], list[str]]:
    style = models.order(note_type, "notes") or ["gemini-3.7-flash"]
    quality = models.order(note_type, "qc") or style
    return style, quality


# ------------------------------------------------------------------ New notes
def link_ids(text: str) -> list[tuple[str, str]]:
    """[(doc id, link)] from pasted lines, in order, each once."""
    found, seen = [], set()
    for line in str(text or "").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        document_id = gdoc_make.doc_id(line)
        if not document_id:
            raise make_service.FormError(f"This is not a Google Doc link: {line[:80]}")
        if document_id not in seen:
            seen.add(document_id)
            found.append((document_id, gdoc_make.doc_url(document_id)))
    if not found:
        raise make_service.FormError("Paste at least one Google Doc link.")
    if len(found) > MAX_LINKS:
        raise make_service.FormError(f"Paste at most {MAX_LINKS} Google Doc links at a time, or use Sheet automation.")
    return found


def submit_links(category: str, note_type: str, subject: str, language: str, options: dict,
                 text: str) -> list[tuple[int, bool]]:
    """One Notes-lane item per Google Doc link. [(item id, newly added)]."""
    make_service.check(category, note_type, subject, language, options)
    added = []
    for document_id, url in link_ids(text):
        payload = {"category": category, "note_type": note_type, "subject": subject, "language": language,
                   "options": {**options, "source_id": document_id, "source_url": url}, "file": ""}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        added.append(job_queue.submit("new_notes", f"Google Doc {document_id[:10]}…", payload, key=key))
    return added


def _report(folder: Path, result: dict) -> Path:
    path = folder / "quality_report.json"
    keep = ("title", "url", "source_url", "source_title", "verification", "watermark", "header_logo",
            "blank_last_page", "pages", "quality", "total_cost_inr", "sharing", "sheet")
    path.write_text(json.dumps({k: result[k] for k in keep if k in result}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _verdict(result: dict) -> tuple[str, str]:
    """(run state, a plain sentence) for one made Doc."""
    if not result.get("ok"):
        return "failed", " ".join(gdoc_make.problems(result)) or "The new Doc did not pass its checks."
    tabs = len(result.get("tab_ids") or [])
    words = f"Google Doc with {tabs} tab{'s' if tabs != 1 else ''}, {result.get('pages', 0)} pages"
    attention = [q["title"] for q in result.get("quality") or [] if q["verdict"] != "passed"]
    if attention:
        return "needs_attention", f"Done: {words}. Review the quality report for: {', '.join(attention)[:200]}."
    return "complete", f"Done: {words}."


_PLAIN = (gdoc_make.GdocError, source.SourceError, ai.AIError, signin.SignInNeeded)


def _failure_words(exc: BaseException) -> str:
    if isinstance(exc, _PLAIN):
        return str(exc)
    return "Something went wrong while making the Google Doc."


def _detail(exc: BaseException) -> str:
    """A short reason after an unexpected failure (e.g. Google's own answer), for whoever looks into it.
    The whole error goes to ~/.note_studio/logs/gdoc.log."""
    if isinstance(exc, _PLAIN):
        return ""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = ""
    try:
        reason = str(exc._get_reason()) if hasattr(exc, "_get_reason") else ""
    except Exception:  # noqa: BLE001
        reason = ""
    words = f"Google answered {status}" if status else type(exc).__name__
    return f" ({words}{': ' + reason[:140] if reason else ''})"


def _log_failure(context: str, exc: BaseException) -> None:
    import traceback

    from . import paths

    try:
        with open(paths.logs_dir() / "gdoc.log", "a", encoding="utf-8") as handle:
            handle.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {context} ---\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def execute(run: dict) -> None:
    """A New notes run: one Google Doc link -> one B.Sc.-style Google Doc."""
    run_id = run["id"]
    options = json.loads(run["options_json"] or "{}")
    folder = Path(run["run_dir"]) if run["run_dir"] else make_service.run_dir(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    source_id = str(options.get("source_id") or "")
    if not source_id:
        make_service.finish(run_id, "failed", "The Google Doc link of this run is missing.",
                            error="The Google Doc link of this run is missing.")
        return
    pack = categories.get(run["category"]) or {}
    style_models, quality_models = model_order(run["note_type"])
    try:
        creds, token = google_creds(), make_service.typeset_token()
        domain = account_domain() if options.get("share_domain", True) else ""
        result = make_doc(source_id, folder, creds=creds, token_provider=token, branding=branding(pack),
                          style_models=style_models, quality_models=quality_models, share_domain=domain,
                          say=lambda text, pct=None: make_service.event(run_id, "step", text, pct))
    except signin.SignInNeeded as exc:
        make_service._sign_in_problem(run_id, options)
        make_service.finish(run_id, "failed", str(exc), error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("gdoc run %s", run_id)
        _log_failure(f"run {run_id}", exc)
        words = _failure_words(exc) + _detail(exc)
        make_service.finish(run_id, "failed", words, error=words, detail=type(exc).__name__,
                            cost=gdoc_make.spent_before(folder))
        return
    if result.get("source_title"):
        db.execute("UPDATE runs SET title=? WHERE id=?", (result["source_title"][:200], run_id))
    state, message = _verdict(result)
    if state != "failed":
        db.execute("INSERT INTO run_items (run_id, position, label, state, output_path, cost_inr, created_at, finished_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (run_id, 1, result["title"], "complete", result["url"],
                                                 result["total_cost_inr"], time.time(), time.time()))
        make_service.event(run_id, "output", result["title"], None, path=result["url"], file_type="gdoc",
                           pages=result.get("pages", 0))
    report = _report(folder, result)
    make_service.event(run_id, "output", "Quality check report", None, path=str(report), file_type="json")
    make_service.finish(run_id, state, message, error=message if state == "failed" else "",
                        cost=result.get("total_cost_inr", 0.0))


# ------------------------------------------------------------------ sheet
def sheet_rows(link: str, rows_spec: str = "") -> dict:
    """Read a sheet tab (free): which rows link a Google Doc, which already have one made."""
    spreadsheet_id, gid = gdoc_sheet.parse_link(link)
    wanted = gdoc_sheet.parse_rows(rows_spec)
    sheets = sheets_client()
    try:
        tab, gid = gdoc_sheet.tab_title(sheets, spreadsheet_id, gid)
        found = gdoc_sheet.read(sheets, spreadsheet_id, tab)
    except gdoc_sheet.SheetError:
        raise
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            raise gdoc_sheet.SheetError("NoteCraft cannot open that sheet. Make sure the signed-in account can edit it.") from exc
        raise gdoc_sheet.SheetError("Google did not answer. Check the internet connection and try again.") from exc
    rows = [r for r in found["rows"] if wanted is None or r["row"] in wanted]
    ready = [r for r in rows if not r["link"]]
    return {"spreadsheet_id": spreadsheet_id, "gid": gid, "tab": tab, "rows": rows, "ready": ready,
            "done": [r for r in rows if r["link"]], "column": gdoc_sheet.letter(found["column"])
            if found["column"] is not None else "", "header": gdoc_sheet.HEADER}


def enqueue_sheet(category: str, link: str, rows_spec: str, share: bool = True) -> tuple[int, bool, int]:
    """One Sheets-lane item for the chosen rows still without a Doc: (item id, newly added, rows)."""
    pack = categories.get(category)
    if not pack or pack.get("enabled") is not True or not categories.gdoc_offered(pack):
        raise gdoc_sheet.SheetError("Choose a category that makes Google Docs (B.Sc. style).")
    found = sheet_rows(link, rows_spec)
    if not found["ready"]:
        raise gdoc_sheet.SheetError("No row needs a Google Doc: every chosen row already has one, or has no Doc link.")
    payload = {"category": category, "spreadsheet_id": found["spreadsheet_id"], "gid": found["gid"],
               "tab": found["tab"], "rows": [r["row"] for r in found["ready"]], "share": bool(share)}
    count = len(payload["rows"])
    title = f"Google Docs from {found['tab']}: {count} row{'s' if count != 1 else ''}"
    entry_id, created = job_queue.submit(SHEET_KIND, title, payload, key="gdoc-" + json.dumps(payload, sort_keys=True))
    return entry_id, created, count


def retry_sheet(run_id: int) -> tuple[int, int] | None:
    """Try again for a finished sheet run: its rows that did not get a Doc, reusing what each one saved.
    (item id, rows), or None when nothing is left to make."""
    runs = db.query("SELECT * FROM runs WHERE id=? AND source='gdoc_sheet'", (run_id,))
    if not runs or runs[0]["state"] in ("waiting", "running"):
        return None
    options = json.loads(runs[0]["options_json"] or "{}")
    left = []
    for found in db.query("SELECT label, state FROM run_items WHERE run_id=? ORDER BY position", (run_id,)):
        number = re.match(r"Row (\d+) ·", found["label"] or "")
        if number and found["state"] not in ("done", "skipped"):
            left.append(int(number.group(1)))
    if not left:
        return None
    payload = {k: options[k] for k in ("category", "spreadsheet_id", "gid", "tab", "share") if k in options}
    payload.update(rows=left, again_from=int(run_id))
    title = f"Google Docs from {options.get('tab', 'the sheet')}: {len(left)} row{'s' if len(left) != 1 else ''} again"
    entry_id, _ = job_queue.submit(SHEET_KIND, title, payload, key=f"gdoc-again-{run_id}")
    return entry_id, len(left)


def stop_sheet(run_id: int) -> bool:
    rows = db.query("SELECT state, source FROM runs WHERE id=?", (run_id,))
    if not rows or rows[0]["source"] != "gdoc_sheet" or rows[0]["state"] != "running":
        return False
    with _stop_lock:
        _stops.add(int(run_id))
    make_service.event(run_id, "step", "Stopping after the rows being made now")
    return True


def _stopped(run_id: int) -> bool:
    with _stop_lock:
        return run_id in _stops


def _row_label(row: dict) -> str:
    return f"Row {row['row']} · {row.get('topic') or row['source_id'][:10]}"[:200]


def launch_sheet(entry: dict) -> tuple[str, str, int | None]:
    """The Sheets lane's launcher for a Google Docs sheet run."""
    payload = entry["payload"]
    pack = categories.get(payload["category"]) or {}
    run_id = db.create_run(typeset_engine.ENGINE, title=entry["title"], note_type=TYPE, category=payload["category"],
                           subject="auto", language="english", source="gdoc_sheet",
                           options={**payload, "queue_id": entry["id"]})
    folder = make_service.run_dir(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    db.execute("UPDATE runs SET run_dir=?, queue_id=?, state='running', started_at=?, pct=2 WHERE id=?",
               (str(folder), entry["id"], time.time(), run_id))
    make_service.event(run_id, "step", "Its turn in the Smart Queue came", 1)
    try:
        return _run_sheet(run_id, payload, pack, folder)
    except signin.SignInNeeded as exc:
        make_service.finish(run_id, "failed", str(exc), error=str(exc))
        return "signin", str(exc), run_id
    except Exception as exc:  # noqa: BLE001
        log.exception("gdoc sheet run %s", run_id)
        words = str(exc) if isinstance(exc, gdoc_sheet.SheetError) else "Something went wrong during the sheet run."
        make_service.finish(run_id, "failed", words, error=words, detail=type(exc).__name__)
        return "failed", words, run_id
    finally:
        with _stop_lock:
            _stops.discard(run_id)


def _run_sheet(run_id: int, payload: dict, pack: dict, folder: Path) -> tuple[str, str, int | None]:
    creds, token = google_creds(), make_service.typeset_token()
    domain = account_domain() if payload.get("share", True) else ""
    look = branding(pack)
    style_models, quality_models = model_order()
    google = gdoc_make.Google(creds)
    found = gdoc_sheet.read(google.sheets, payload["spreadsheet_id"], payload["tab"])
    by_row = {r["row"]: r for r in found["rows"]}
    rows = [by_row[n] for n in payload["rows"] if n in by_row]
    items = {}
    for position, row in enumerate(rows, start=1):
        items[row["row"]] = db.execute(
            "INSERT INTO run_items (run_id, position, label, state, created_at) VALUES (?,?,?,?,?)",
            (run_id, position, _row_label(row), "planned", time.time()))
    total, finished, lock = len(rows), [0], threading.Lock()
    make_service.event(run_id, "step", f"Making {total} Google Doc{'s' if total != 1 else ''}, "
                                       f"{min(PARALLEL, max(total, 1))} at a time", 3)

    def item(row, state, message="", url="", cost=0.0):
        db.execute("UPDATE run_items SET state=?, message=?, output_path=?, cost_inr=?, finished_at=? WHERE id=?",
                   (state, message[:300], url, cost, time.time(), items[row["row"]]))

    def one(row) -> dict:
        if _stopped(run_id):
            item(row, "stopped", "Not reached: the run was stopped.")
            return {"state": "stopped", "cost": 0.0}
        if row["link"]:
            item(row, "skipped", "Already has a Google Doc.")
            return {"state": "skipped", "cost": 0.0}
        db.execute("UPDATE run_items SET state='running' WHERE id=?", (items[row["row"]],))
        row_folder = folder / "rows" / f"r{row['row']:04d}"
        earlier = make_service.run_dir(int(payload["again_from"])) / "rows" / row_folder.name \
            if payload.get("again_from") else None
        if earlier is not None and earlier.is_dir() and not row_folder.exists():
            # Try again: the earlier attempt's saved formatting, quality checks and checked Doc are reused.
            shutil.copytree(earlier, row_folder, copy_function=shutil.copy2)
        try:
            result = make_doc(row["source_id"], row_folder, creds=creds, token_provider=token, branding=look,
                              style_models=style_models, quality_models=quality_models, share_domain=domain,
                              label=row.get("topic") or "")
        except signin.SignInNeeded:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("gdoc sheet run %s row %s", run_id, row["row"])
            _log_failure(f"sheet run {run_id} row {row['row']}", exc)
            paid = gdoc_make.spent_before(row_folder)         # its formatting is saved for Try again
            item(row, "failed", _failure_words(exc) + _detail(exc), "", paid)
            return {"state": "failed", "cost": paid}
        state, message = _verdict(result)
        cost = float(result.get("total_cost_inr") or 0.0)
        if state == "failed":
            item(row, "failed", message, result.get("url", ""), cost)
            return {"state": "failed", "cost": cost}
        result["sheet"] = gdoc_sheet.write_link(google.sheets, payload["spreadsheet_id"], payload["tab"], row["row"],
                                                row["source_id"], result["url"])
        _report(row_folder, result)
        note = message if state == "needs_attention" else ""
        if not result["sheet"].startswith(("written", "already")):
            note = f"Made, but its link was {result['sheet']}."
            state = "needs_attention"
        item(row, "done", note, result["url"], cost)
        with lock:
            finished[0] += 1
            make_service.event(run_id, "step", f"{_row_label(row)}: {result['sheet']}",
                               3 + int(95 * finished[0] / max(total, 1)))
        return {"state": state, "cost": cost}

    # Each thread has its own Google clients (gdoc_make.Google); a row's link is written only after the
    # row is read again, so rows made side by side never write over each other.
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        outcomes = list(pool.map(one, rows))
    cost = round(sum(o["cost"] for o in outcomes), 4)
    made = sum(1 for o in outcomes if o["state"] in ("complete", "needs_attention"))
    failed = sum(1 for o in outcomes if o["state"] == "failed")
    attention = sum(1 for o in outcomes if o["state"] == "needs_attention")
    stopped = sum(1 for o in outcomes if o["state"] == "stopped")
    words = f"Made {made} Google Doc{'s' if made != 1 else ''}"
    if attention:
        words += f"; {attention} need{'s' if attention == 1 else ''} attention"
    if failed:
        words += f"; {failed} did not finish"
    if stopped:
        words += f"; {stopped} not reached (stopped)"
    state = "stopped" if stopped and not made else ("needs_attention" if failed or attention or stopped else "complete")
    make_service.finish(run_id, state, words + ".", error=words + "." if state == "stopped" else "", cost=cost)
    return ("stopped" if state == "stopped" else "done"), words + ".", run_id


job_queue.register(SHEET_KIND, launch_sheet)
