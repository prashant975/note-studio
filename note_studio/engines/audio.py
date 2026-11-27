"""Audio notes for a finished chapter (note_studio/audio), in this process:

    1 join     the lecture PDFs, in lecture order -> "<chapter> merged notes.pdf"  (free)
    2 script   the team's prompt + the joined PDF -> the two friends' lines       (stage "notes")
    3 check    every line against the notes; its corrections applied              (stage "qc")
    4 voice    the script spoken in two voices -> "<chapter> Audio notes.wav"     (stage "voice")

A "script first" run stops after 3: the team reads the script on the run page, fixes
any line and has it spoken (make_from_script, a new run that does only 4). A run that
stops part way keeps what it paid for: Try again reuses the script and every part
already spoken."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..audio import chapter, script, voice

ENGINE = "audio"
REVISION = "audio-notes-v1"
LENGTHS = (10, 15, 25)                 # minutes offered on the form
DEFAULT_VOICES = ("Puck", "Kore")          # a young man and a young woman, as in the reference episode
DEFAULT_HOSTS = ("Raju", "Riya")
FAILURE_WORDS = {
    "source": "The lecture PDFs could not be joined.",
    "script": "The script could not be written for this chapter.",
    "voice": "The script is ready (download it below), but the audio could not be made.",
}
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


@dataclass
class Settings:
    chapter: str = ""
    teacher: str = script.PROMPT_TEACHER
    hosts: tuple = DEFAULT_HOSTS
    voices: tuple = DEFAULT_VOICES
    minutes: int = 15
    audio_now: bool = True
    rules: str = ""
    models: dict = field(default_factory=dict)


@dataclass
class Result:
    state: str                          # complete | failed
    outputs: list = field(default_factory=list)       # [{"path", "label", "kind"}], also when it failed
    seconds: float | None = None
    script_only: bool = False
    cost_inr: float = 0.0
    calls: int = 0
    failure: str = ""                   # source | script | voice | sign_in | model_budget_exceeded
    message: str = ""
    detail: str = ""
    report: dict = field(default_factory=dict)


def _name(value, default: str) -> str:
    value = " ".join(str(value or "").split())
    return value if script.valid_name(value) else default


def run_settings(options: dict, rules: str, models: dict) -> Settings:
    """Settings from a run's saved options (the form checked them; old or odd values fall back)."""
    try:
        minutes = int(str(options.get("length") or 15))
    except ValueError:
        minutes = 15
    hosts = (_name(options.get("host_one"), DEFAULT_HOSTS[0]), _name(options.get("host_two"), DEFAULT_HOSTS[1]))
    if hosts[0].casefold() == hosts[1].casefold():
        hosts = DEFAULT_HOSTS
    voices = tuple(str(options.get(key) or "") if str(options.get(key) or "") in voice.VOICES else default
                   for key, default in (("voice_one", DEFAULT_VOICES[0]), ("voice_two", DEFAULT_VOICES[1])))
    return Settings(chapter=" ".join(str(options.get("chapter_title") or "").split())[:120],
                    teacher=" ".join(str(options.get("teacher_name") or "").split())[:60] or script.PROMPT_TEACHER,
                    hosts=hosts, voices=voices, minutes=minutes if minutes in LENGTHS else 15,
                    audio_now=options.get("audio_step", "now") != "after_check", rules=rules, models=models)


def file_stem(settings: Settings, first_file: str = "") -> str:
    stem = settings.chapter or re.sub(r"\.pdf$", "", str(first_file or ""), flags=re.I)
    stem = " ".join(_UNSAFE.sub("", stem).split()).strip(". ")
    return (stem or "Chapter")[:90]


class _Paid:
    """The PW AI session, made only when something must be paid for."""

    def __init__(self, token_provider, filename: str, say):
        self.token_provider, self.filename, self.say = token_provider, filename, say
        self.pages = 1
        self.session = None

    def get(self):
        if self.session is None:
            from .. import engine_catalog
            from ..typeset import ai

            engine_catalog.put_shortnotes_on_path()                 # pw_access, as for the typeset notes
            import pw_access

            self.session = ai.Session(self.token_provider(), pw_access.new_task_id("studio-audio"), self.filename[:120],
                                      self.pages, log=self._log)
        return self.session

    def _log(self, line: str) -> None:
        if "voice line call" not in line:                          # lines spoken one by one: not listed singly
            self.say(line, None)

    def cost(self) -> tuple[float, int, list]:
        if self.session is None:
            return 0.0, 0, []
        return self.session.cost, len(self.session.calls), self.session.calls


def _review(paid: _Paid, settings: Settings, pdf: bytes, doc: dict) -> dict:
    from ..typeset import ai

    try:
        found = paid.get().json("script check", settings.models.get("qc") or settings.models.get("notes") or [],
                                script.review_system(), [ai.pdf_part(pdf), {"text": script.numbered(doc)}],
                                script.review_schema(), max_tokens=20000)
    except ai.AIError as problem:
        if problem.code in ("budget", "sign_in"):
            raise
        return {"asked": True, "done": False, "applied": [], "skipped": [], "missing": [],
                "problem": f"The check against the notes could not be made: {problem}"}
    return script.apply_review(doc, found, settings.hosts)


def _sources_key(sources: list[tuple[str, bytes]]) -> str:
    """The lecture files themselves (names and bytes, in order): the joined PDF differs on every join."""
    digest = hashlib.sha256()
    for name, data in sources:
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def _script(paid: _Paid, settings: Settings, merged: bytes, sources_key: str, lectures: list[dict], folder: Path,
            say) -> tuple[dict, dict, bool]:
    """(script, its check, reused) - a script saved by an earlier attempt from the same lecture files,
    prompt and choices is reused; its check is made again only if it never finished."""
    from ..typeset import ai

    prompt = script.prompt(settings.teacher, settings.hosts)
    system = script.system(settings.hosts, settings.teacher, settings.minutes, settings.rules,
                           tuple(voice.gender(v) for v in settings.voices))
    mapping = chapter.lecture_map(lectures)
    key = script.identity(prompt=prompt, system=system, lectures=mapping, sources=sources_key)
    saved = folder / "script.json"
    try:
        found = json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = {}
    pdf = None
    reused = found.get("identity") == key and bool((found.get("doc") or {}).get("lines"))
    if reused:
        doc, review = found["doc"], found.get("review") or {}
        say("Reusing the script from the previous attempt", 25)
    else:
        say("Writing the script: PW AI reads the whole chapter", 12)
        pdf = chapter.ai_copy(merged)
        raw = paid.get().json("script", settings.models.get("notes") or [], system,
                              [ai.pdf_part(pdf), {"text": prompt}, {"text": mapping}], script.schema(settings.hosts))
        (folder / "script_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        doc, tidied = script.tidy(raw, settings.hosts)
        review = {"done": False, "tidied": tidied}
        saved.write_text(json.dumps({"identity": key, "doc": doc, "review": review}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    if not review.get("done"):
        say("Checking every line against the notes", 30)
        tidied = review.get("tidied", 0)
        review = _review(paid, settings, pdf or chapter.ai_copy(merged), doc)
        review["tidied"] = tidied
        saved.write_text(json.dumps({"identity": key, "doc": doc, "review": review}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return doc, review, reused


def _voice(paid: _Paid, settings: Settings, doc: dict, folder: Path, stem: str, say, result: Result) -> dict:
    spoken = voice.speak(paid.get, doc["lines"], settings.hosts, settings.voices,
                         settings.models.get("voice") or [], folder, say)
    target = folder / f"{stem} Audio notes.{spoken['ext']}"
    target.write_bytes(spoken.pop("audio"))
    result.outputs.insert(0, {"path": str(target), "label": target.name, "kind": spoken["ext"]})
    result.seconds = spoken["seconds"]
    return {**spoken, "voices": {settings.hosts[0]: settings.voices[0], settings.hosts[1]: settings.voices[1]}}


def spoken_length(seconds: float | None) -> str:
    if not seconds:
        return ""
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest:02d} s"


def _report(settings: Settings, doc: dict, review: dict, lectures: list[dict], audio: dict, paid: _Paid,
            reused_script: bool) -> dict:
    checks = []
    if lectures:
        checks.append({"check": "Lectures joined", "result": "ok", "detail":
                       f"{len(lectures)} lecture PDF{'s' if len(lectures) != 1 else ''}, "
                       f"{lectures[-1]['last_page']} pages, in this order: " + "; ".join(l["file"] for l in lectures)})
    count = script.words(doc)
    checks.append({"check": "Script", "result": "ok", "detail":
                   f"{len(doc['lines'])} lines, {count} words (about {max(1, round(count / script.WORDS_PER_MINUTE))} "
                   f"minutes); {settings.hosts[0]} and {settings.hosts[1]} speak; teacher: {settings.teacher}."})
    if review:
        if review.get("done"):
            skipped = len(review.get("skipped") or [])
            checks.append({"check": "Checked against the notes", "result": "attention" if skipped else "ok", "detail":
                           f"{len(review.get('applied') or [])} line(s) corrected"
                           + (f"; {skipped} suggested correction(s) could not be placed: read them in this report "
                              "and fix those lines by hand" if skipped else "") + "."})
            if review.get("missing"):
                checks.append({"check": "Topics the episode does not mention", "result": "advice",
                               "detail": "; ".join(review["missing"])})
        else:
            checks.append({"check": "Checked against the notes", "result": "attention",
                           "detail": review.get("problem") or "The check was not made."})
    if audio:
        form = (f"MP3 ({voice.MP3_KBPS} kbps)" if audio["ext"] == "mp3"
                else "WAV: the MP3 library is not in this installation")
        checks.append({"check": "Audio", "result": "ok", "detail":
                       (f"{spoken_length(audio.get('seconds'))} of audio, {form}; " if audio.get("seconds") else f"{form}; ")
                       + f"{audio['parts']} part(s) spoken, {audio['reused_parts']} reused from an earlier attempt; "
                       f"voices: {settings.hosts[0]} {settings.voices[0]}, {settings.hosts[1]} {settings.voices[1]}."})
    cost, _count, calls = paid.cost()
    return {"verdict": "needs_attention" if any(c["result"] == "attention" for c in checks) else "passed",
            "engine": ENGINE, "revision": REVISION, "prompt_sha256": script.PROMPT_SHA256,
            "teacher": settings.teacher, "hosts": list(settings.hosts), "voices": list(settings.voices),
            "minutes_asked": settings.minutes, "lectures": lectures, "checks": checks, "review": review,
            "reused_script": reused_script, "audio": audio, "cost_inr": cost, "calls": calls}


def _failed(result: Result, failure: BaseException, paid: _Paid) -> Result:
    from ..typeset import ai

    code = getattr(failure, "code", "")
    if isinstance(failure, chapter.ChapterError):
        result.message = str(failure)
    elif type(failure).__name__ == "SignInNeeded" or code == "sign_in":
        result.failure, result.message = "sign_in", str(failure)
    elif code == "budget":
        result.failure, result.message = "model_budget_exceeded", str(failure)
    elif isinstance(failure, (ai.AIError, script.ScriptError, voice.VoiceError)):
        result.message = f"{result.message} {failure}".strip()
    if result.failure in ("voice", "sign_in", "model_budget_exceeded") and any(o["kind"] == "txt" for o in result.outputs):
        result.message += " Press Try again: the script and the parts already spoken are reused."
    result.cost_inr, result.calls, _ = paid.cost()
    result.detail = f"{type(failure).__module__}.{type(failure).__qualname__}"
    return result


def make(sources: list[tuple[str, bytes]], out_dir: Path, settings: Settings, *,
         token_provider: Callable[[], Callable[[], str]],
         step: Callable[[str, int | None], None] | None = None) -> Result:
    """A chapter's lecture PDFs (in lecture order) -> merged PDF, script and audio in ``out_dir``."""
    say = step or (lambda text, pct=None: None)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = file_stem(settings, sources[0][0] if sources else "")
    result = Result(state="failed", failure="source", message=FAILURE_WORDS["source"])
    paid = _Paid(token_provider, stem, say)
    try:
        say(f"Joining the {len(sources)} lecture PDF{'s' if len(sources) != 1 else ''} in lecture order", 5)
        merged, lectures = chapter.merge(sources)
        merged_path = folder / f"{stem} merged notes.pdf"
        merged_path.write_bytes(merged)
        result.outputs.append({"path": str(merged_path), "label": merged_path.name, "kind": "pdf"})
        paid.pages = lectures[-1]["last_page"]
        result.failure, result.message = "script", FAILURE_WORDS["script"]
        doc, review, reused = _script(paid, settings, merged, _sources_key(sources), lectures, folder, say)
        script_path = folder / f"{stem} Audio script.txt"
        script_path.write_text(script.to_text(doc, settings.hosts, settings.teacher), encoding="utf-8")
        result.outputs.insert(0, {"path": str(script_path), "label": script_path.name, "kind": "txt"})
        audio = {}
        if settings.audio_now:
            result.failure, result.message = "voice", FAILURE_WORDS["voice"]
            audio = _voice(paid, settings, doc, folder, stem, say, result)
        result.report = _report(settings, doc, review, lectures, audio, paid, reused)
        result.state, result.failure, result.message = "complete", "", ""
        result.script_only = not settings.audio_now
        result.cost_inr, result.calls, _ = paid.cost()
        return result
    except Exception as failure:                                     # noqa: BLE001 - plain words for every failure
        return _failed(result, failure, paid)


def make_from_script(text: str, out_dir: Path, settings: Settings, *,
                     token_provider: Callable[[], Callable[[], str]],
                     step: Callable[[str, int | None], None] | None = None) -> Result:
    """A script the team checked (text, "Name: words" lines) -> its audio, in ``out_dir``."""
    say = step or (lambda text, pct=None: None)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = file_stem(settings, "Audio notes")
    result = Result(state="failed", failure="voice", message="The audio could not be made from this script.")
    paid = _Paid(token_provider, stem, say)
    try:
        doc = script.parse(text, settings.hosts)
        script_path = folder / f"{stem} Audio script.txt"
        script_path.write_text(script.to_text(doc, settings.hosts, settings.teacher), encoding="utf-8")
        result.outputs.append({"path": str(script_path), "label": script_path.name, "kind": "txt"})
        audio = _voice(paid, settings, doc, folder, stem, say, result)
        result.report = _report(settings, doc, {}, [], audio, paid, True)
        result.state, result.failure, result.message = "complete", "", ""
        result.cost_inr, result.calls, _ = paid.cost()
        return result
    except Exception as failure:                                     # noqa: BLE001
        return _failed(result, failure, paid)
