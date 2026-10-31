"""Practice: Mains practice (DMP) and DPP from finished UPSC notes.

The page follows the old app's panels step by step (app.py:1442-1560 for DMP,
app.py:1678-2325 for DPP); each button is one step of the engine.
"""
from __future__ import annotations

import re
from collections import Counter

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from . import categories, db, engine_catalog, job_queue, practice_service as practice

DMP_LEVELS = (("easy", "Easy / Direct"), ("moderate", "Moderate / Multi-layered"), ("applied", "Applied / Analytical"))
DIFFICULTIES = ("Easy", "Medium", "Hard")
PICK_PAGE = 25
DPP_FILES = (("questions", "Question paper (PDF)"), ("solutions", "Answers and solutions (PDF)"),
             ("combined", "Questions with answers (PDF)"), ("bundle", "All files (ZIP)"),
             ("ids_csv", "QBG IDs (CSV)"), ("ids_txt", "QBG IDs (TXT)"))


_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")


def _plain(text: str) -> str:
    """The engine's preview text is Markdown (prompt_preview_markdown escapes "1." and
    the like); this page shows it as text, so the escapes go."""
    return _MD_ESCAPE.sub(r"\1", str(text or ""))


#: Markers a stored model answer carries for the engine's sheet writer
#: (src/dmp/answer_parse.py). The sheet never prints the three part markers or a
#: "Point:" label, so this page does not either.
_DMP_PART_RE = re.compile(
    r"^[ \t]*#{1,6}[ \t]*(?:INTRODUCTION|BODY|CONCLUSION|भूमिका|प्रस्तावना|मुख्य भाग|निष्कर्ष)[ \t]*:?[ \t]*$",
    re.I | re.M)
_DMP_HEAD_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*(APPROACH|ANSWER|दृष्टिकोण|दृष्टिकोन|उत्तर)[ \t]*:?[ \t]*$",
                          re.I | re.M)
_DMP_POINT_RE = re.compile(r"^([ \t]*(?:[-*•][ \t]+)?)(\*\*)?[ \t]*(?:Point|बिंदु|मुद्दा)[ \t]*:[ \t]*(\*\*)?[ \t]*",
                           re.I | re.M)


def _answer_text(answer: str) -> str:
    text = _DMP_PART_RE.sub("", str(answer or ""))
    text = _DMP_HEAD_RE.sub(lambda m: m.group(1).title() if m.group(1).isascii() else m.group(1), text)
    # "**Point: Revenue**" keeps the bold that the label opened.
    text = _DMP_POINT_RE.sub(lambda m: m.group(1) + ("**" if m.group(2) and not m.group(3) else ""), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _shown(question: dict) -> dict:
    return {**question, "prompt": _plain(question.get("prompt", "")),
            "options": [_plain(o) for o in question.get("options") or []]}


def _number(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(2) if digits else ""


def _topics(st: dict, subject: str, chapter: str, weak: bool) -> dict:
    """The DPP topic picker as app.py:1711-1781 builds it."""
    coverage = st.get("coverage") or []
    weight: dict[str, float] = {}
    for cand in coverage:
        weight[cand["subject"]] = weight.get(cand["subject"], 0.0) + cand["confidence"] * (2 if cand["recommended"] else 1)
    subjects = sorted(weight, key=weight.get, reverse=True)
    all_subjects = "All subjects"
    choices = subjects + ([all_subjects] if len(subjects) > 1 else [])
    subject = subject if subject in choices else (choices[0] if choices else "")
    in_subject = [c for c in coverage if subject == all_subjects or c["subject"] == subject]
    chapter_count = Counter(c["chapter"] for c in in_subject)
    chapters = [c for c, _ in chapter_count.most_common()] if subject != all_subjects and len(chapter_count) > 1 else []
    if chapter in chapters:
        in_subject = [c for c in in_subject if c["chapter"] == chapter]
    else:
        chapter = ""
    strong = [c for c in in_subject if c["recommended"]]
    shown = in_subject if weak or not strong else strong

    def label(cand: dict) -> str:
        path = [p for p in (cand["subtopic"], cand["topic"], cand["chapter"]) if p]
        prefix = f"{cand['subject']}: " if subject == all_subjects else ""
        return f"{prefix}{' | '.join(path)}  ·  {cand['confidence']:.0%}"

    shown_ids = [c["row_id"] for c in shown]
    chosen = [rid for rid in st.get("row_ids") or [] if rid in shown_ids]
    ticked = set(chosen or [c["row_id"] for c in shown if c["recommended"]] or shown_ids)
    return {"subjects": [(s, s if s == all_subjects else f"{s}  ({sum(c['subject'] == s for c in coverage)} topics)")
                         for s in choices], "subject": subject,
            "chapters": [(c, f"{c}  ({chapter_count[c]} topics)") for c in chapters], "chapter": chapter, "weak": weak,
            "rows": [{"row_id": c["row_id"], "label": label(c), "ticked": c["row_id"] in ticked} for c in shown],
            "hidden": len(in_subject) - len(shown)}


def _dmp_view(run_id: int, st: dict) -> dict:
    sessions = practice.dmp_sessions(run_id)
    chosen = st.get("dmp_session") if any(r.get("dmp_session_id") == st.get("dmp_session") for r in sessions) else (
        sessions[0]["dmp_session_id"] if sessions else "")
    session = practice.dmp_session(run_id, chosen) if chosen else None
    questions, number = [], 0
    for level, words in DMP_LEVELS:
        for q in ((session or {}).get("levels") or {}).get(level, {}).get("questions") or []:
            number += 1
            questions.append({**q, "number": number, "level_words": words,
                              "answer_text": _answer_text(q.get("answer")),
                              "answered": q.get("status") == "answered" and bool(q.get("answer"))})
    return {"sessions": sessions, "session_id": chosen, "session": session, "questions": questions,
            "answered": sum(1 for q in questions if q["answered"]), "sheets": practice.dmp_sheets(run_id),
            "labels": st.get("dmp_labels") or {}}


def _dpp_view(st: dict, request: Request) -> dict:
    params = request.query_params
    view = {"topics": _topics(st, params.get("subject", ""), params.get("chapter", ""), params.get("weak") == "1")}
    available = st.get("available") or {}
    view["counts"] = [{"name": d, "cap": int(available.get(d, 0)), "value": min(5, int(available.get(d, 0)))}
                      for d in DIFFICULTIES] if available else []
    view["available_total"] = sum(int(v) for v in available.values()) if available else None
    preview = st.get("preview") or {}
    view["slots"] = [_shown({**s, "number": i, **(preview.get(s["question_id"]) or {})})
                     for i, s in enumerate(st.get("slots") or [], start=1)]
    exports = st.get("exports") or {}
    view["files"] = [{"name": name, "label": label, "file": exports[name].replace("\\", "/").split("/")[-1]}
                     for name, label in DPP_FILES if exports.get(name)]
    eligible = st.get("eligible") or []
    level, topic = params.get("level", "All"), params.get("topic", "All")
    topics = {e["row_id"]: e["topic"] for e in eligible}
    listed = [e for e in eligible if (level == "All" or e["difficulty"] == level) and (topic == "All" or e["row_id"] == topic)]
    pages = max(1, -(-len(listed) // PICK_PAGE))
    try:
        page = min(max(1, int(params.get("page", "1"))), pages)
    except ValueError:
        page = 1
    picked = [q for q in st.get("picked") or [] if q in {e["question_id"] for e in eligible}]
    view["pick"] = {"listed": [_shown(e) for e in listed[(page - 1) * PICK_PAGE: page * PICK_PAGE]],
                    "count": len(listed), "page": page,
                    "pages": pages, "level": level, "topic": topic, "topics": topics, "picked": picked,
                    "has_pool": bool(eligible)}
    view["subject_value"] = st.get("subject") if st.get("subject") is not None else st.get("subject_default", "")
    view["dpp_no"] = _number(st.get("dpp_no"))
    return view


def _context(request: Request, run_id: int) -> dict:
    run = practice.get(run_id)
    if run is None:
        raise HTTPException(404)
    st = practice.state(run_id)
    busy = practice.busy(run_id)
    waiting = db.query("SELECT id FROM job_queue WHERE kind='practice' AND state='waiting' "
                       "AND json_extract(payload_json, '$.run_id')=?", (run_id,))
    kind = run["options"].get("practice")
    view = _dmp_view(run_id, st) if kind == "dmp" else _dpp_view(st, request)
    return {"run": run, "st": st, "kind": kind, "busy": busy, "queued": bool(waiting), "view": view,
            "last": st.get("last") or {}, "masters": practice.masters_status() if kind == "dpp" else None,
            "query": str(request.url.query or "")}


def register(app, render, go):
    @app.get("/practice")
    def practice_home(request: Request):
        if not engine_catalog.available("upsc"):
            return render(request, "practice.html", upsc_available=False)
        sets = db.query("SELECT * FROM runs WHERE source='practice' ORDER BY id DESC LIMIT 100")
        upsc = categories.get("upsc") or {}
        return render(request, "practice.html", sets=sets, sources=practice.sources(), masters=practice.masters_status(),
                      upsc_available=True,
                      upsc_on=upsc.get("enabled") is True, queue_counts=job_queue.counts(),
                      hold_words=job_queue.hold_words())

    @app.post("/practice/start")
    async def start(request: Request):
        form = await request.form()
        try:
            run_id = practice.start(str(form.get("practice") or ""), str(form.get("kind") or ""), str(form.get("id") or ""))
        except practice.PracticeError as problem:
            return go("/practice", str(problem), "warn")
        return go(f"/practice/{run_id}")

    @app.get("/practice/{run_id}")
    def practice_page(request: Request, run_id: int):
        return render(request, "practice_set.html", **_context(request, run_id))

    @app.get("/practice/{run_id}/card")
    def practice_card(request: Request, run_id: int):
        return render(request, "_practice_card.html", **_context(request, run_id))

    def _go(run_id: int, ok: bool, words: str, back: str = ""):
        target = f"/practice/{run_id}" + (f"?{back}" if back else "")
        return go(target, words, "ok" if ok else "warn") if words else go(target)

    # ------------------------------------------------------------- DMP
    @app.post("/practice/{run_id}/dmp/questions")
    def dmp_questions(run_id: int):
        ok, words = practice.step(run_id, "dmp_questions")
        return _go(run_id, ok, words)

    async def _ticked(request: Request) -> list[str]:
        form = await request.form()
        return [str(q) for q in form.getlist("q")]

    @app.post("/practice/{run_id}/dmp/answers")
    async def dmp_answers(request: Request, run_id: int):
        st = practice.state(run_id)
        view = _dmp_view(run_id, st)
        ticked = set(await _ticked(request))
        wanted = [q["id"] for q in view["questions"] if q["id"] in ticked and not q["answered"]]
        if not wanted:
            return _go(run_id, False, "Tick at least one question that has no model answer yet.")
        ok, words = practice.step(run_id, "dmp_answers", session_id=view["session_id"], question_ids=wanted)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dmp/sheet/{kind}")
    async def dmp_sheet(request: Request, run_id: int, kind: str):
        if kind not in ("questions", "answers"):
            raise HTTPException(404)
        view = _dmp_view(run_id, practice.state(run_id))
        ticked = await _ticked(request)
        if kind == "questions" and not ticked:
            return _go(run_id, False, "Tick the questions the question paper should have.")
        if kind == "answers" and not view["answered"]:
            return _go(run_id, False, "Make model answers first.")
        form = await request.form()
        # What the sheet's title and topic boxes print; blank means the lecture's own.
        labels = {key: " ".join(str(form.get(f"sheet_{key}") or "").split())[:200]
                  for key in ("subject", "topic", "dmp_no")}
        practice.update_state(run_id, dmp_labels=labels)
        ok, words = practice.step(run_id, "dmp_export", session_id=view["session_id"], kind=kind,
                                  selected_ids=[q for q in (q["id"] for q in view["questions"]) if q in set(ticked)],
                                  **labels)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dmp/set")
    async def dmp_set(request: Request, run_id: int):
        form = await request.form()
        chosen = str(form.get("session") or "")
        if practice.dmp_session(run_id, chosen) is not None:
            practice.update_state(run_id, dmp_session=chosen)
        return go(f"/practice/{run_id}")

    # ------------------------------------------------------------- DPP
    @app.post("/practice/{run_id}/dpp/topics")
    async def dpp_topics(request: Request, run_id: int):
        form = await request.form()
        rows = [str(r) for r in form.getlist("row")]
        st = practice.state(run_id)
        coverage_order = [c["row_id"] for c in st.get("coverage") or []]
        rows = [r for r in coverage_order if r in set(rows)]
        if not rows:
            return _go(run_id, False, "Tick at least one topic.", str(request.url.query or ""))
        language = str(form.get("language") or st.get("language") or "")
        if language not in ("English", "Hindi"):
            return _go(run_id, False, "Choose the question language.", str(request.url.query or ""))
        practice.update_state(run_id, row_ids=rows, language=language, verified_only=form.get("verified") == "on",
                              slots=[], preview={}, exports={}, flash="", available=None, eligible=[], picked=[],
                              mode=str(form.get("mode") or "difficulty"))
        ok, words = practice.step(run_id, "dpp_counts" if form.get("mode") != "pick" else "dpp_eligible")
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/again")
    def dpp_again(run_id: int):
        ok, words = practice.step(run_id, "dpp_topics")
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/build")
    async def dpp_build(request: Request, run_id: int):
        form = await request.form()
        requested = {}
        for name in DIFFICULTIES:
            text = str(form.get(name) or "0").strip()
            requested[name] = int(text) if text.isdigit() else 0
        if not sum(requested.values()):
            return _go(run_id, False, "Ask for at least one question.")
        ok, words = practice.step(run_id, "dpp_build", requested=requested)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/mode/{mode}")
    def dpp_mode(run_id: int, mode: str):
        if mode not in ("difficulty", "pick"):
            raise HTTPException(404)
        st = practice.update_state(run_id, mode=mode)
        if mode == "pick" and not st.get("eligible"):
            ok, words = practice.step(run_id, "dpp_eligible")
            return _go(run_id, ok, words)
        if mode == "difficulty" and not st.get("available"):
            ok, words = practice.step(run_id, "dpp_counts")
            return _go(run_id, ok, words)
        return go(f"/practice/{run_id}")

    @app.post("/practice/{run_id}/dpp/tick/{question_id}")
    async def dpp_tick(request: Request, run_id: int, question_id: str):
        st = practice.state(run_id)
        picked = list(st.get("picked") or [])
        # A list, not a set: the paper follows the order the questions were ticked (app.py:1841-1845).
        picked = [q for q in picked if q != question_id] if question_id in picked else picked + [question_id]
        practice.update_state(run_id, picked=picked)
        return go(f"/practice/{run_id}?{request.url.query}" if request.url.query else f"/practice/{run_id}")

    @app.post("/practice/{run_id}/dpp/pick")
    def dpp_pick(run_id: int):
        picked = practice.state(run_id).get("picked") or []
        if not picked:
            return _go(run_id, False, "Tick at least one question.")
        ok, words = practice.step(run_id, "dpp_pick", picked=picked)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/replace/{slot_id}")
    def dpp_replace(run_id: int, slot_id: str):
        ok, words = practice.step(run_id, "dpp_replace", slot_id=slot_id)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/remove/{slot_id}")
    def dpp_remove(run_id: int, slot_id: str):
        ok, words = practice.step(run_id, "dpp_remove", slot_id=slot_id)
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/new")
    def dpp_new(run_id: int):
        # app.py:1654-1662: drop the set, keep what was already shown.
        practice.update_state(run_id, slots=[], preview={}, exports={}, flash="")
        if practice.state(run_id).get("mode") == "pick":
            ok, words = practice.step(run_id, "dpp_eligible")
        else:
            ok, words = practice.step(run_id, "dpp_counts")
        return _go(run_id, ok, words)

    @app.post("/practice/{run_id}/dpp/files")
    async def dpp_files(request: Request, run_id: int):
        form = await request.form()
        practice.update_state(run_id, dpp_no=_number(form.get("dpp_no")),
                              subject=" ".join(str(form.get("subject") or "").split()))
        ok, words = practice.step(run_id, "dpp_export")
        return _go(run_id, ok, words)

    # ----------------------------------------------------------- files
    @app.get("/practice/{run_id}/files/{area}/{name}")
    def practice_file(run_id: int, area: str, name: str):
        if practice.get(run_id) is None:
            raise HTTPException(404)
        path = practice.file_path(run_id, area, name)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, filename=path.name)
