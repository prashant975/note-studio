"""Turning a finished note into practice questions, and ticked questions into answers.

Everything the panel does goes through here. The panel knows about buttons; this
module knows about the model, the note and the store, and it returns either a
session or a `DmpError` whose message is already fit to show a person.

Two rules the whole design hangs on:

* **Answers are never generated up front.** Ten questions cost three calls;
  ten answers cost ten. The user ticks, and only the ticked ones are
  asked for.
* **An answered question is never re-asked.** The answer is on disk; a second
  call would spend money to overwrite it with something slightly different.
  `regenerate=True` is the explicit way to say you meant it.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import (
    APP_VERSION,
    DMP_ANSWER_OVERRUN_FLAG,
    DMP_LEVELS,
    DMP_MARK_WORDS,
    DMP_MAX_GROUNDING_WORDS,
    DMP_MIN_NOTE_WORDS,
    MAX_PARALLEL_AI_CALLS,
    dmp_question_count,
)
from ..config import RUNS_DIR
from ..languages import normalise
from . import store
from .store import DmpStoreError
from .models import (
    STATUS_ANSWERED,
    DmpSession,
    Question,
    new_question_id,
    new_session_id,
    normalise_marks,
    utc_now_iso,
)
from .answer_parse import answer_word_count
from .prompts import build_answer_prompt, build_question_prompt


class DmpError(RuntimeError):
    """Something the user needs told, in words they can act on.

    Raised instead of letting a proxy error or a JSONDecodeError reach the UI:
    "Expecting value: line 1 column 1 (char 0)" is true and useless.
    """


# ---------------------------------------------------------------------------
# Grounding content
# ---------------------------------------------------------------------------

#: Where the finished note text lives, best first. `notes_final.md` is what the
#: DOCX was actually built from; the rest are the older stages, tried in
#: descending order of how finished they are so a run from a previous build
#: still yields practice questions.
_NOTES_CANDIDATES = (
    "notes_final.md",       # exactly what went into the DOCX/PDF
    "notes_repaired.md",    # DCN: after layout repair, before any compression
    "notes_after_qc.md",    # after QC corrections
    "notes_raw.md",
    "notes_raw.txt",
)

#: Structural markers the DCN format uses. Headings become markdown so the model
#: reads the note's shape; the rest are app plumbing and are dropped.
_HEADING_RE = re.compile(r"^#(LECTURE_TITLE|H1|H2|H3|H4):\s*(.*)$")
_DROP_RE = re.compile(r"^#(FIGURE:|SEPARATOR|TABLE_START|TABLE_END|FLOW_START|FLOW_END)")
_HEADING_HASHES = {"LECTURE_TITLE": "#", "H1": "##", "H2": "###", "H3": "####", "H4": "#####"}


def plain_note_text(notes_text: str) -> str:
    """The note as prose a model can read, with the app's markers resolved.

    Headings keep their level — the structure is half of what makes a question
    about "the second body section" possible — and the layout directives that
    mean nothing outside the DOCX writer are removed.
    """
    out: list[str] = []
    for raw in notes_text.splitlines():
        line = raw.rstrip()
        heading = _HEADING_RE.match(line.strip())
        if heading:
            kind, text = heading.group(1), heading.group(2).strip()
            if text:
                out.append(f"{_HEADING_HASHES[kind]} {text}")
            continue
        if _DROP_RE.match(line.strip()):
            continue
        out.append(line)
    # Collapse the blank runs the marker removal leaves behind.
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _trim_to_budget(text: str, max_words: int = DMP_MAX_GROUNDING_WORDS) -> str:
    """Cut a very long note down to what fits in one request.

    By words from the END, not by characters: a character cut lands mid-word,
    and a note that stops mid-sentence invites the model to complete the thought
    itself. The cut is announced in the text so the model knows the note is
    partial rather than assuming the class simply ended there.
    """
    words = text.split()
    if len(words) <= max_words:
        return text
    return (" ".join(words[:max_words])
            + "\n\n[The notes continue beyond this point; this excerpt is what "
              "was sent. Set questions only on what appears above.]")


def grounding_content(note_id: str, runs_dir: Path | None = None) -> str:
    """The finished note for this run, as the model should see it.

    Raises DmpError when the note cannot be found or is too thin to set
    questions on — both of which are states a person can fix, and neither of
    which should produce five confident questions about nothing.
    """
    run_dir = store.note_run_dir(note_id, runs_dir)
    if not run_dir.exists():
        raise DmpError(
            "That set of notes is no longer on this machine, so practice "
            "questions cannot be generated from it. Generate the notes again.")
    for name in _NOTES_CANDIDATES:
        path = run_dir / name
        if not path.exists():
            continue
        text = plain_note_text(path.read_text(encoding="utf-8", errors="replace"))
        if len(text.split()) < DMP_MIN_NOTE_WORDS:
            continue
        return _trim_to_budget(text)
    raise DmpError(
        "These notes do not have enough content to set practice questions on. "
        "Generate the notes again from a fuller lecture.")


def note_context(note_id: str, runs_dir: Path | None = None) -> dict:
    """Title, subject, topic and language for a note, from its run metadata.

    The topic is what a directive prompt would fill `[Topic Name]` with, and is
    built from the subject and the clean title — never from the raw stem, which
    is where `L04_DCN04_...` comes from.
    """
    run_dir = store.note_run_dir(note_id, runs_dir)
    meta = {}
    meta_path = run_dir / "run_metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    from ..title_utils import subject_display_name

    language = normalise(meta.get("language")) or "en"
    # On the UPSC workflow `subject` is the WORKFLOW key ("upsc") and the real
    # subject lives in `area` — the same rule the DCN writer follows when it
    # prints the subject above the lecture title. Reading `subject` alone gives
    # "Upsc", which is the one thing subject_display_name refuses to print.
    area = str(meta.get("area") or "")
    subject = area if area and area != "auto" else str(meta.get("subject") or "")
    title = _clean_note_title(meta, run_dir)
    subject_line = (str(meta.get("subject_line") or "")
                    or subject_display_name(subject, language))
    topic = " — ".join(p for p in (subject_line, title) if p) or title
    return {
        "note_id": note_id,
        "language": language,
        "subject": subject,
        "subject_line": subject_line,
        "note_title": title,
        "topic": topic,
    }


def _clean_note_title(meta: dict, run_dir: Path) -> str:
    """A title fit to print, or "" — never a raw `Lxx_DCNxx` stem.

    `title_utils` already knows what a placeholder looks like and what a clean
    title looks like; this asks it rather than re-deciding, so the practice
    sheet and the notes cover agree about what the lecture is called.
    """
    from ..title_utils import clean_title_text, is_placeholder, parse_marker_title

    for name in _NOTES_CANDIDATES:
        path = run_dir / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            if line.strip().startswith("#LECTURE_TITLE:"):
                parsed = parse_marker_title(line.split(":", 1)[1])
                if parsed.title and not is_placeholder(parsed.title):
                    return parsed.title
                break
        break
    candidate = clean_title_text(str(meta.get("subject_line") or ""))
    return "" if is_placeholder(candidate) else candidate


# ---------------------------------------------------------------------------
# Parsing what the model sent back
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json_object(text: str) -> dict | None:
    """Best effort at the JSON object in a reply, without a model call.

    Models wrap JSON in fences and add a sentence before it more often than they
    return a bare object, and every one of those is recoverable here for free.
    Only a genuinely unparseable reply is worth spending a second call on.
    """
    cleaned = _FENCE_RE.sub("", text or "").strip()
    for candidate in (cleaned, _between_braces(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # Trailing commas are the other common malformation.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"questions": data}
    return None


def _between_braces(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else ""


def parse_questions(reply: str, level: str) -> list[Question]:
    """Questions from one model reply. Returns [] when nothing usable came back.

    Blank and duplicate texts are dropped here rather than stored: a set with
    the same question twice reads as a bug in the app, and it is cheaper to
    notice a short set than to explain a repeated one.
    """
    data = _extract_json_object(reply)
    if not data:
        return []
    raw = data.get("questions")
    if not isinstance(raw, list):
        return []
    questions: list[Question] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("question") or "").strip()
        key = re.sub(r"\W+", "", text.lower())
        if not text or key in seen:
            continue
        seen.add(key)
        questions.append(Question(
            id=new_question_id(), level=level, text=text,
            marks=normalise_marks(item.get("marks")),
        ))
    return questions[:dmp_question_count(level)]


_REPAIR_INSTRUCTION = (
    "\n\nYour previous reply could not be read as JSON. Reply again with the "
    "SAME questions, as nothing but the JSON object described above — no "
    "explanation, no markdown fence, no text before or after it."
)


def _generate_level(client, level: str, notes: str, language_code: str) -> list[Question]:
    """One level's questions: one call, plus at most one repair call.

    The repair is capped at one because the failure it recovers from is a
    formatting slip, and a model that has ignored the format twice is not about
    to comply on the third ask — it is going to keep billing.
    """
    prompt = build_question_prompt(level, notes, language_code=language_code)
    reply = _call(client, prompt)
    questions = parse_questions(reply, level)
    if len(questions) >= dmp_question_count(level):
        return questions
    retry = parse_questions(_call(client, prompt + _REPAIR_INSTRUCTION), level)
    # Whichever attempt gave more. A repair that comes back worse than the first
    # try should not cost the user the questions the first try produced.
    return retry if len(retry) > len(questions) else questions


def _call_raw(client, prompt: str, *, max_output_tokens: int = 8000):
    """One model call, with proxy and network failures translated for a person.

    Returns the full response so callers that care (the answer path, whose
    output got longer in the detailed-answer change) can see ``truncated``.
    """
    from ..ai_client import GeminiError

    try:
        return client.generate(prompt, [], max_images=0,
                               max_output_tokens=max_output_tokens)
    except GeminiError as exc:
        raise DmpError(_friendly(str(exc))) from exc
    except Exception as exc:  # noqa: BLE001 — the UI must never see a traceback
        raise DmpError(_friendly(str(exc))) from exc


def _call(client, prompt: str, *, max_output_tokens: int = 8000) -> str:
    return _call_raw(client, prompt, max_output_tokens=max_output_tokens).text or ""


def _last_model(client, default: str = "") -> str:
    return getattr(client, "model", "") or default


def _friendly(detail: str) -> str:
    """The message a user is shown when a call fails.

    Quota and rate limits get their own wording because the action differs:
    "try again" is right for a busy service and wrong for an exhausted quota.
    """
    low = detail.lower()
    if "429" in low or "quota" in low or "rate limit" in low:
        return ("The AI service is rate-limited or out of quota right now. "
                "Please try again in a few minutes.")
    if "401" in low or "403" in low or "token" in low or "auth" in low:
        return ("Your sign-in is no longer accepted by the PW proxy. "
                "Sign out and sign in again, then retry.")
    if "timeout" in low or "timed out" in low:
        return "The AI service did not answer in time. Please try again."
    return ("The AI service is temporarily unavailable. Please try again. "
            f"({detail[:160]})")


# ---------------------------------------------------------------------------
# The two operations the panel performs
# ---------------------------------------------------------------------------


def generate_questions(note_id: str, client, *, levels=DMP_LEVELS,
                       runs_dir: Path | None = None) -> DmpSession:
    """A new practice set: 4 easy, 3 moderate and 3 applied questions.

    One model call per level, run concurrently — three sequential calls is
    around a minute of somebody watching a spinner, and the client is already
    used this way by the chunked notes path.
    """
    if client is None:
        raise DmpError("Sign in before generating practice questions.")
    notes = grounding_content(note_id, runs_dir)
    context = note_context(note_id, runs_dir)
    wanted = [lv for lv in levels if lv in DMP_LEVELS]
    if not wanted:
        raise DmpError("No difficulty level was selected.")

    results: dict[str, list[Question]] = {}
    errors: list[str] = []
    workers = max(1, min(MAX_PARALLEL_AI_CALLS, len(wanted)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_generate_level, client, level, notes, context["language"]): level
            for level in wanted
        }
        for future in as_completed(futures):
            level = futures[future]
            try:
                results[level] = future.result()
            except DmpError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                errors.append(_friendly(str(exc)))

    if not any(results.values()):
        raise DmpError(errors[0] if errors else
                       "Practice questions could not be generated from these "
                       "notes. Please try again.")

    now = utc_now_iso()
    session = DmpSession(
        dmp_session_id=new_session_id(),
        note_id=note_id,
        note_title=context["note_title"],
        subject=context["subject"],
        subject_line=context["subject_line"],
        topic=context["topic"],
        language=context["language"],
        app_version=APP_VERSION,
        created_at=now,
        updated_at=now,
    )
    for level in wanted:
        if results.get(level):
            session.set_level(level, results[level])
    store.save(session, runs_dir)
    return session


def generate_answers(note_id: str, session_id: str, question_ids, client, *,
                     regenerate: bool = False,
                     runs_dir: Path | None = None) -> DmpSession:
    """Model answers for the selected questions, and nothing else.

    Saves after every answer rather than once at the end. Answers arrive over
    tens of seconds each; a browser closed midway through five of them should
    keep the three that were paid for.
    """
    if client is None:
        raise DmpError("Sign in before generating model answers.")
    session = store.load(note_id, session_id, runs_dir)
    if session is None:
        raise DmpError("That practice set could not be found. It may have been "
                       "deleted along with its notes.")
    wanted = [q for q in session.questions if q.id in set(question_ids or [])]
    if not wanted:
        raise DmpError("Select at least one question first.")

    # The idempotency rule, applied before anything is billed.
    todo = [q for q in wanted if regenerate or not q.answered]
    if not todo:
        return session

    notes = grounding_content(note_id, runs_dir)
    errors: list[str] = []
    for question in todo:
        try:
            resp = _call_raw(
                client,
                build_answer_prompt(question.level, question.text, question.marks,
                                    notes, language_code=session.language),
                # 8000, raised from 4000 alongside the detailed-answer prompt:
                # a ~400-word Hindi answer with headings and bold markup can
                # overrun 4000 output tokens, and a truncated answer is worse
                # than a short one.
                max_output_tokens=8000,
            )
        except DmpError as exc:
            errors.append(str(exc))
            break  # the service is down; the rest will fail identically
        answer = (resp.text or "").strip()
        if not answer:
            errors.append(f"No answer came back for one {question.marks}-mark question.")
            continue
        if getattr(resp, "truncated", False):
            # Keep what was paid for, but say so where the teacher will see
            # it — in the answer itself, which is also what the exports print.
            answer += ("\n\n> NOTE: This answer reached the generation length "
                       "limit and may end abruptly. Regenerate it if the "
                       "ending looks cut off.")
        question.answer = answer
        question.status = STATUS_ANSWERED
        question.answer_generated_at = utc_now_iso()
        question.model_used = _last_model(client)
        session.updated_at = utc_now_iso()
        store.save(session, runs_dir)

    if errors and not any(q.answered for q in wanted):
        raise DmpError(errors[0])
    return session


def answer_overrun(answer: str, marks: int) -> tuple[int, int] | None:
    """(words, limit) when an answer runs more than a fifth past its limit.

    The cheap check after the fact: a word count, no model call. The prompt
    asks for the exam's limit ±10%; this flags only what is clearly past it,
    so a 170-word answer to a 150-word question is not nagged about. It never
    shortens anything — a cut answer stops mid-point, and the teacher is
    better placed to trim than a word counter is.
    """
    if not answer:
        return None
    limit = DMP_MARK_WORDS.get(int(marks), DMP_MARK_WORDS[10])
    words = answer_word_count(answer)
    if words > limit * (1 + DMP_ANSWER_OVERRUN_FLAG):
        return words, limit
    return None


def get_session(note_id: str, session_id: str,
                runs_dir: Path | None = None) -> DmpSession | None:
    return store.load(note_id, session_id, runs_dir)


def list_sessions(note_id: str, runs_dir: Path | None = None) -> list[dict]:
    try:
        return store.list_sessions(note_id, runs_dir)
    except store.DmpStoreError:
        return []


def has_notes(note_id: str, runs_dir: Path | None = None) -> bool:
    """Can a practice set be generated from this note at all?

    Used by the panel to decide whether to offer the button, so a run whose
    folder has been cleaned up shows an explanation instead of a button that
    fails when pressed.
    """
    try:
        grounding_content(note_id, runs_dir)
    except DmpError:
        return False
    return True


def list_notes(runs_dir: Path | None = None, limit: int = 40) -> list[dict]:
    """Past lectures a practice set can be built from, newest first.

    Exists so practice does not require generating notes first. Someone who
    wants only questions — a teacher setting a test, a student revising last
    week's class — should not have to spend a full pipeline run to reach them.

    Folders are read newest-first and the scan stops at `limit`: `runs/` grows
    without bound, and a picker listing four hundred lectures is not a picker.
    Only runs whose notes are still readable are offered, so nothing in the list
    can fail when it is chosen.
    """
    root = Path(runs_dir or RUNS_DIR)
    if not root.exists():
        return []
    notes: list[dict] = []
    # The run id starts with a timestamp, so the name sorts chronologically and
    # no metadata has to be read to order the list.
    for folder in sorted((d for d in root.iterdir() if d.is_dir()),
                         key=lambda d: d.name, reverse=True):
        if len(notes) >= limit:
            break
        try:
            if not has_notes(folder.name, runs_dir):
                continue
            context = note_context(folder.name, runs_dir)
        except (DmpStoreError, OSError):
            continue
        sessions = list_sessions(folder.name, runs_dir)
        notes.append({
            **context,
            "run_dir": str(folder),
            # The timestamp already in the run id — no file read needed.
            "created": folder.name.replace("run_", "")[:15],
            "practice_sets": len(sessions),
        })
    return notes


def note_label(note: dict) -> str:
    """One line for the picker: when, what, and whether practice already exists.

    Timed to the SECOND. Minute precision put eight lectures on identical rows
    in a real runs folder — the user could not tell them apart, and Streamlit
    could not either: picking one and pressing a button landed on a different
    lecture with the same label.
    """
    stamp = str(note.get("created") or "")
    if len(stamp) >= 15:
        when = (f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} "
                f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}")
    elif len(stamp) >= 13:
        when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    else:
        when = stamp
    title = note.get("note_title") or note.get("subject_line") or "Untitled lecture"
    language = (note.get("language") or "en").upper()
    sets = note.get("practice_sets") or 0
    suffix = f" · {sets} practice set(s)" if sets else ""
    return f"{when} · {title} [{language}]{suffix}"


def note_labels(notes: list[dict]) -> dict[str, str]:
    """{note_id: label}, guaranteed distinct.

    `note_label` alone cannot promise this — it sees one note and has no way to
    know another produced the same line. The picker needs the guarantee, so the
    disambiguation happens here, where the whole list is in view, and costs
    nothing on the normal case where the labels already differ.

    Two lectures generated in the same second still collide, so those get the
    tail of their run id appended. It is ugly and it is only ever shown when
    the alternative is two rows a person cannot choose between.
    """
    labels: dict[str, str] = {}
    seen: dict[str, int] = {}
    for note in notes:
        note_id = str(note.get("note_id") or "")
        base = note_label(note)
        seen[base] = seen.get(base, 0) + 1
        labels[note_id] = base
    for note in notes:
        note_id = str(note.get("note_id") or "")
        if seen.get(labels.get(note_id, ""), 0) > 1:
            labels[note_id] = f"{labels[note_id]} · {note_id[-8:]}"
    return labels


def note_id_from_run_dir(run_dir: str | Path | None) -> str:
    """The note id for a run folder path — which is just its name."""
    if not run_dir:
        return ""
    return Path(str(run_dir)).name
