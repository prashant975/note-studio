"""Note Studio's practice steps for this engine: Mains practice (DMP) and DPP.

engine_main.py hands a request here when its "action" is not "notes". Each
call is one step, the way one button press is one step on the old app's page:

    dmp_questions  dmp_answers  dmp_export
    dpp_topics  dpp_counts  dpp_build  dpp_eligible  dpp_pick  dpp_replace  dpp_remove  dpp_export

The engine's own functions are called as app.py calls them (DMP: app.py:1302-1560,
DPP: app.py:1678-2325), with one difference on purpose: they work on a COPY of
the lecture's files in Note Studio's run folder (request["runs_dir"]/<note_id>),
never in the old run, the way app.py itself treats uploaded notes
(app.py:2527-2550). What the old app keeps in its page state (DPP: chosen
topics, the set, what was already shown) Note Studio keeps in its run folder
and sends back in each request. The parsed question bank (about 35 s to read)
is kept as a pickle in Note Studio's own folder, keyed on the workbooks' paths
and dates, so a Replace does not read 55 MB again.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pickle
import re
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def _done(status: str, reason: str = "", **data) -> int:
    _emit("done", status=status, reason=reason, **data)
    return 0


# ------------------------------------------------------------------ DMP
def _dmp_client(token: str, request: dict):
    """app.py's _dmp_client (app.py:1302-1317): a new client and task id per step."""
    if request.get("test_fake_client"):          # tests only: canned replies, no network
        return _FakeDmpClient(request["test_fake_client"])
    if not token:
        return None
    import pw_access
    from src.ai_client import GeminiClient
    from src.config import DEFAULT_MODEL

    return GeminiClient(google_token=token, model=DEFAULT_MODEL,
                        task_id=pw_access.new_task_id("upsc-dmp"), input_unit="question")


class _FakeDmpClient:
    """Tests only (the pattern of the engine's tests/test_dmp_service.py)."""

    model = "fake"

    def __init__(self, replies: dict):
        import itertools

        self.replies = replies
        self._calls = itertools.count(1)             # the three levels call at once

    def generate(self, prompt, image_paths=None, **_kw):
        from types import SimpleNamespace

        call = next(self._calls)
        if "Return ONLY valid JSON" in prompt:       # five distinct questions per call
            text = json.dumps({"questions": [
                {"marks": 10 if i % 2 else 15,
                 "text": f"{self.replies.get('question', 'Discuss the point')} (set {call}, question {i})."}
                for i in range(1, 6)]})
        else:
            text = self.replies.get("answer", "Introduction. Body. Conclusion.")
        return SimpleNamespace(text=text, model="fake", provider="fake", usage={}, truncated=False)


def dmp(action: str, request: dict, token: str) -> int:
    from src.dmp import export as dmp_export
    from src.dmp import service as dmp_service
    from src.dmp import store as dmp_store
    from src.dmp.labels import text

    runs_dir = Path(request["runs_dir"])
    note_id = str(request["note_id"])
    try:
        lang = dmp_service.note_context(note_id, runs_dir)["language"]
    except Exception:  # noqa: BLE001 — the old app falls back to English too (app.py:1572-1576)
        lang = "en"
    if not dmp_service.has_notes(note_id, runs_dir):
        return _done("failed", text("no_notes", lang))

    if action == "dmp_questions":
        client = _dmp_client(token, request)
        if client is None:
            return _done("failed", text("sign_in_questions", lang), code="sign_in")
        _emit("step", text="Writing the practice questions", pct=10)
        try:
            session = dmp_service.generate_questions(note_id, client, runs_dir=runs_dir)
        except dmp_service.DmpError as exc:
            return _done("failed", str(exc), code=_code(str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"{type(exc).__name__}: {exc}", flush=True)
            return _done("failed", text("failed_questions", lang))
        return _done("complete", f"{len(session.questions)} practice questions are ready.",
                     session_id=session.dmp_session_id)

    session_id = str(request.get("session_id") or "")
    if action == "dmp_answers":
        client = _dmp_client(token, request)
        if client is None:
            return _done("failed", text("sign_in_answers", lang), code="sign_in")
        wanted = [str(q) for q in request.get("question_ids") or []]
        _emit("step", text=f"Writing model answers for {len(wanted)} question(s)", pct=10)
        try:
            session = dmp_service.generate_answers(note_id, session_id, wanted, client, runs_dir=runs_dir)
        except dmp_service.DmpError as exc:
            return _done("failed", str(exc), code=_code(str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"{type(exc).__name__}: {exc}", flush=True)
            return _done("failed", text("failed_answers", lang))
        answered = sum(1 for q in session.questions if q.id in set(wanted) and q.answered)
        return _done("complete", f"{answered} of {len(wanted)} model answer(s) written.", session_id=session_id)

    if action == "dmp_export":
        kind = str(request.get("kind") or "")
        session = dmp_service.get_session(note_id, session_id, runs_dir)
        if session is None:
            return _done("failed", "That practice set could not be found.")
        selected = [str(q) for q in request.get("selected_ids") or []]
        # As app.py:1409: the sheets go next to the set (here: in the copy).
        out_dir = dmp_store.dmp_dir(note_id, runs_dir) / "sheets"
        # The page's Subject / Topic / DMP No. boxes. A blank box means what the
        # app prefills its own boxes with (dmp_export.sheet_defaults).
        try:
            labels = dmp_export.sheet_defaults(session, runs_dir)
            for key in ("subject", "topic", "dmp_no"):
                typed = " ".join(str(request.get(key) or "").split())
                if typed:
                    labels[key] = typed
            docx_path, pdf_path, warnings = dmp_export.export(
                session, kind, out_dir, runs_dir=runs_dir,
                selected_ids=selected if kind == "questions" else None, **labels)
        except Exception as exc:  # noqa: BLE001 — app.py:1414-1415
            return _done("failed", f"{text('sheet_failed', lang)} Reason: {exc}")
        for warning in warnings or []:
            _emit("warning", text=str(warning))
        for path, file_kind in ((docx_path, "docx"), (pdf_path, "pdf")):
            if path:
                _emit("output", path=str(path), kind=file_kind, label=f"{kind}:{file_kind}")
        return _done("complete" if pdf_path else "needs_attention",
                     "" if pdf_path else "PDF needs Word or LibreOffice on this computer; the Word file is ready.")
    return _done("failed", f"Unknown step {action}.")


def _code(message: str) -> str:
    """'sign_in' for the engine's sign-in messages, so Note Studio holds its queue."""
    return "sign_in" if re.search(r"\bsign(?:ed)?[- ]in\b", message, re.I) else ""


# ------------------------------------------------------------------ DPP
def _masters(verified_only: bool, cache_dir: Path):
    """dpp_builder.load_masters(verified_only=...), kept between steps as a pickle
    keyed on both workbooks' paths and dates and on the reader's own code."""
    from src import dpp_builder

    paths = [dpp_builder.master_path(kind) for kind in ("taxonomy", "questions")]
    if None in paths:
        return dpp_builder.load_masters(verified_only=verified_only)     # raises the app's own words
    code = [HERE / "src" / "notes_to_dpp" / name for name in ("workbooks.py", "models.py")]
    key_text = "|".join(f"{p.resolve()}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in [*paths, *code])
    key = hashlib.sha256(f"{key_text}|{verified_only}".encode("utf-8")).hexdigest()[:24]
    cache = Path(cache_dir) / f"masters_{key}.pickle"
    if cache.is_file():
        try:
            with cache.open("rb") as handle:
                return pickle.load(handle)
        except Exception:  # noqa: BLE001 — a damaged cache is simply read again
            pass
    _emit("step", text="Reading the question bank (once; about half a minute)", pct=None)
    loaded = dpp_builder.load_masters(verified_only=verified_only)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temp = cache.with_suffix(".tmp")
    with temp.open("wb") as handle:
        pickle.dump(loaded, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(temp, cache)
    return loaded


def default_subject(subject_line: str, rows) -> str:
    """The subject the page prefills (app.py:2023-2026): the lecture's own subject
    line, else the tag-workbook subject of most of the chosen topics."""
    tagged = Counter(row.subject for row in rows)
    return subject_line or (tagged.most_common(1)[0][0] if tagged else "")


def _normalized_number(value) -> str:
    """app.py:1978-1981."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(2) if digits else ""


def _saved_number(note_id: str, runs_dir: Path) -> str:
    """app.py:1984-1999, on the copy."""
    try:
        metadata = json.loads((runs_dir / note_id / "run_metadata.json").read_text(encoding="utf-8"))
        return _normalized_number(metadata.get("dpp_no")) if isinstance(metadata, dict) else ""
    except (AttributeError, OSError, ValueError, TypeError):
        return ""


def _slots_in(state: dict):
    from src.notes_to_dpp.models import Difficulty, SelectionSlot

    return [SelectionSlot(slot_id=s["slot_id"], difficulty=Difficulty(s["difficulty"]), question_id=s["question_id"],
                          preferred_row_id=s["preferred_row_id"]) for s in state.get("slots") or []]


def _slots_out(slots) -> list[dict]:
    return [{"slot_id": s.slot_id, "difficulty": s.difficulty.value, "question_id": s.question_id,
             "preferred_row_id": s.preferred_row_id} for s in slots]


def _preview(question, language) -> dict:
    """What app.py shows per question (app.py:1939-1946)."""
    from src.notes_to_dpp.pdf_export import prompt_preview_markdown

    localized = question.content_for(language)
    prompt = getattr(localized, "prompt", "") or question.prompt
    options = getattr(localized, "options", ()) or question.options
    return {"question_id": question.question_id, "difficulty": question.difficulty.value,
            "prompt": prompt_preview_markdown(prompt, getattr(localized, "prompt_markup", None)),
            "options": [str(o) for o in options]}


def dpp(action: str, request: dict, out: Path) -> int:
    from src import dpp_builder
    from src.dmp import service as dmp_service
    from src.notes_to_dpp.models import Difficulty, Language

    runs_dir = Path(request["runs_dir"])
    note_id = str(request["note_id"])
    state = dict(request.get("dpp") or {})
    cache_dir = Path(request.get("state_dir") or out) / "dpp_cache"
    seen_ids = frozenset(state.get("seen_ids") or ())
    seen_fps = frozenset(state.get("seen_fps") or ())

    if action == "dpp_remove":                       # needs no bank (app.py:1965-1968)
        from src.notes_to_dpp.selection import remove_question

        outcome = remove_question(_slots_in(state), str(request.get("slot_id")), seen_ids=seen_ids,
                                  seen_fingerprints=seen_fps)
        _emit("state", slots=_slots_out(outcome.slots), seen_ids=sorted(outcome.seen_ids),
              seen_fps=sorted(outcome.seen_fingerprints), exports={} if outcome.removed else state.get("exports", {}))
        return _done("complete", "")

    try:
        taxonomy, bank = _masters(bool(state.get("verified_only", True)), cache_dir)
    except dpp_builder.DppError as exc:
        return _done("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — app.py:2253-2256
        return _done("failed", f"The master workbooks could not be read. Reason: {exc}")
    rows_by_id = {r.row_id: r for r in taxonomy.rows}

    if action == "dpp_topics":                       # app.py:2208-2235, 2266-2276, 1678-1781
        try:
            candidates = [c for c in dpp_builder.coverage(note_id, taxonomy.rows, runs_dir=runs_dir)
                          if c.row_id in rows_by_id]
        except dpp_builder.DppError as exc:
            return _done("failed", str(exc))
        try:
            language, code = dpp_builder.note_language(note_id, runs_dir)
        except Exception:  # noqa: BLE001 — app.py:2221-2222
            language, code = Language.ENGLISH, "en"
        context = dmp_service.note_context(note_id, runs_dir)
        _emit("state", note_language_code=code,
              language=state.get("language") or (language.value if language != Language.UNKNOWN else ""),
              subject_line=context.get("subject_line") or "", title=context.get("note_title") or "",
              dpp_no=_saved_number(note_id, runs_dir),
              coverage=[{"row_id": c.row_id, "confidence": c.confidence, "recommended": c.recommended,
                         "evidence": c.evidence, "subject": rows_by_id[c.row_id].subject,
                         "chapter": rows_by_id[c.row_id].chapter, "topic": rows_by_id[c.row_id].topic,
                         "subtopic": rows_by_id[c.row_id].subtopic or ""} for c in candidates])
        return _done("complete", f"{len(candidates)} topic(s) matched.")

    language = Language(state.get("language") or "English")
    rows = [rows_by_id[rid] for rid in state.get("row_ids") or [] if rid in rows_by_id]
    if not rows:
        return _done("failed", "Choose at least one topic first.")
    questions = {q.question_id: q for q in bank.questions}

    if action == "dpp_counts":                       # app.py:1789-1805
        from src.notes_to_dpp.selection import available_counts

        available = available_counts(bank.questions, rows, seen_ids=seen_ids, seen_fingerprints=seen_fps,
                                     language=language)
        _emit("state", available={d.value: int(available[d]) for d in Difficulty},
              subject_default=default_subject(str(state.get("subject_line") or ""), rows))
        return _done("complete", "")

    if action in ("dpp_build", "dpp_pick"):          # app.py:2300-2311 and 1915-1925
        from src.notes_to_dpp.selection import generate_selection, manual_selection

        try:
            if action == "dpp_build":
                requested = {d: max(0, int((request.get("requested") or {}).get(d.value, 0))) for d in Difficulty}
                outcome = generate_selection(bank.questions, rows, requested, seen_ids=seen_ids,
                                             seen_fingerprints=seen_fps, language=language)
            else:
                outcome = manual_selection(bank.questions, rows, [str(q) for q in request.get("picked") or []],
                                           seen_ids=seen_ids, seen_fingerprints=seen_fps, language=language)
        except ValueError as exc:
            message = str(exc) if action == "dpp_pick" else f"Not enough questions for that request. {exc}"
            return _done("failed", message)
        _emit("state", slots=_slots_out(outcome.slots), seen_ids=sorted(outcome.seen_ids),
              seen_fps=sorted(outcome.seen_fingerprints), exports={}, flash="",
              preview={s.question_id: _preview(questions[s.question_id], language) for s in outcome.slots})
        return _done("complete", f"{len(outcome.slots)} questions chosen.")

    if action == "dpp_eligible":                     # app.py:1859-1908
        from src.notes_to_dpp.selection import eligible_questions

        pool = eligible_questions(bank.questions, rows, seen_ids=seen_ids, seen_fingerprints=seen_fps,
                                  language=language)
        listed = []
        for question, row in pool:
            entry = _preview(question, language)
            entry["row_id"] = row.row_id
            entry["topic"] = " | ".join(p for p in (row.subtopic, row.topic) if p)
            listed.append(entry)
        _emit("state", eligible=listed)
        return _done("complete", f"{len(listed)} question(s) to choose from.")

    if action == "dpp_replace":                      # app.py:1951-1961
        from src.notes_to_dpp.selection import replace_question

        slots = _slots_in(state)
        outcome = replace_question(slots, str(request.get("slot_id")), bank.questions, rows, seen_ids=seen_ids,
                                   seen_fingerprints=seen_fps, language=language)
        preview = dict(state.get("preview") or {})
        for slot in outcome.slots:
            preview.setdefault(slot.question_id, _preview(questions[slot.question_id], language))
        _emit("state", slots=_slots_out(outcome.slots), seen_ids=sorted(outcome.seen_ids),
              seen_fps=sorted(outcome.seen_fingerprints), flash=outcome.message, preview=preview,
              exports={} if outcome.replaced else state.get("exports", {}))
        return _done("complete", outcome.message)

    if action == "dpp_export":                       # app.py:2047-2113
        from src.notes_to_dpp.exporting import export_ids_csv, export_ids_text
        from src.notes_to_dpp.pdf_export import export_practice_pdfs
        from src.utils import ensure_dir, safe_name

        slots = _slots_in(state)
        title = str(state.get("title") or "")
        # As typed on the page (app.py:2027-2030); Note Studio prefills it the app's way (default_subject).
        subject = " ".join(str(state.get("subject") or "").split())
        dpp_no = _normalized_number(state.get("dpp_no"))
        named = title if subject and subject.casefold() in (title or "").casefold() else (
            " ".join(p for p in (subject, title) if p))
        stem = safe_name(named or note_id, default="dpp")
        dpp_suffix = f"_DPP_{dpp_no}" if dpp_no else "_dpp"
        try:
            out_dir = ensure_dir(Path(request.get("output_dir") or out / "dpp"))
            export = export_practice_pdfs(slots, questions, language, dpp_no=dpp_no, subject=subject)
            files = {
                "ids_csv": (out_dir / f"{stem}{dpp_suffix}_ids.csv", export_ids_csv(slots)),
                "ids_txt": (out_dir / f"{stem}{dpp_suffix}_ids.txt", export_ids_text(slots)),
                "questions": (out_dir / f"{stem}{dpp_suffix}_questions.pdf", export.question_pdf),
                "solutions": (out_dir / f"{stem}{dpp_suffix}_solutions.pdf", export.solutions_pdf),
                "combined": (out_dir / f"{stem}{dpp_suffix}_questions_with_solutions.pdf", export.combined_pdf),
            }
            for path, payload in files.values():
                path.write_bytes(payload)
            bundle = io.BytesIO()
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, payload in files.values():
                    archive.writestr(path.name, payload)
            bundle_path = out_dir / f"{stem}{dpp_suffix}_all_files.zip"
            bundle_path.write_bytes(bundle.getvalue())
            files["bundle"] = (bundle_path, bundle.getvalue())
        except Exception as exc:  # noqa: BLE001 — app.py:2128-2131
            return _done("failed", f"Those PDFs could not be built. Reason: {exc}")
        exports = {name: str(path) for name, (path, _) in files.items()}
        _emit("state", exports=exports, subject=subject, dpp_no=dpp_no)
        for name, path in exports.items():
            _emit("output", path=path, kind=Path(path).suffix.lstrip("."), label=name)
        return _done("complete", "The practice set files are ready.")
    return _done("failed", f"Unknown step {action}.")


def run(request: dict, out: Path, token: str) -> int:
    action = str(request.get("action") or "")
    if action.startswith("dmp_"):
        return dmp(action, request, token)
    if action.startswith("dpp_"):
        return dpp(action, request, out)
    return _done("failed", f"Unknown step {action}.")
