"""The script: the team's prompt and the chapter's joined PDF go to PW AI, which writes
the two friends' lines; a second call checks every line against the notes.

prompt.txt is the text of the team's "Voice Note.docx", word for word (PROMPT_SHA256;
a test keeps it so). Per run only the two names it gives as examples change: "Rahul
Sir" becomes the run's teacher name and "Raju aur Ram" the two friends' names. It
goes as the user's message, as the team pasted it. The app's own system text asks
only for what a voice needs (two speakers, nothing but spoken words) and the length.

The team's reference episode (a 15 min NotebookLM sample, heard once on 26 Nov 2026:
_build/audio_reference_20261126.*) set how it should sound: a young man who opens and
asks and a young woman who explains most, 2,823 words in 170 short turns, quick
one-word reactions, fillers and laughs, formulas said the classroom way. The system
text asks for that, and reference_excerpt.txt (30 of its turns) shows the manner only.

A script is also plain text, one line per turn ("Raju: ..."); lines starting with #
are notes. That is the file the team downloads, fixes and gives back to be spoken."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

PROMPT_FILE = Path(__file__).with_name("prompt.txt")
PROMPT_SHA256 = "e45783f0386e92d3d2f4c321d4f3e1ff2e6b85c4c2ef84f3721c3cf62566e64e"
PROMPT_TEACHER = "Rahul Sir"
PROMPT_HOSTS = ("Raju", "Ram")
REFERENCE_FILE = Path(__file__).with_name("reference_excerpt.txt")
REFERENCE_TEACHER = "Amit Sir"         # the reference episode's teacher: the run's teacher stands in
REVISION = "audio-script-v2"
WORDS_PER_MINUTE = 185                 # the reference episode: 2,823 words in 15 min 14 s
WORDS_PER_TURN = 16.6                  # ... in 170 turns
MAX_SCRIPT_CHARS = 60000
MAX_NAME = 24
MIN_LINES = 12                         # what PW AI writes; an edited script needs only 2
_NAME = re.compile(r"^[^\W\d_]+(?:[ .'-]{1,2}[^\W\d_]+)*[.]?$")
_MARKUP = re.compile(r"[*_#`~]+")
_DIRECTION = re.compile(r"^\s*[\[(][^\])]{0,40}[\])]\s*")      # a leading "(laughs)" or "[excited]"


class ScriptError(ValueError):
    """A plain-words problem with a script."""


def prompt_text() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8").replace("\r\n", "\n").strip()


def valid_name(name: str) -> bool:
    name = str(name or "").strip()
    return 0 < len(name) <= MAX_NAME and bool(_NAME.match(name))


def prompt(teacher: str, hosts: tuple[str, str]) -> str:
    """The team's prompt with this run's teacher and friends in place of its examples."""
    text = prompt_text()
    teacher = " ".join(str(teacher or "").split())
    if teacher and teacher != PROMPT_TEACHER:
        text = text.replace(PROMPT_TEACHER, teacher)
    if tuple(hosts) != PROMPT_HOSTS:
        text = text.replace(f"{PROMPT_HOSTS[0]} aur {PROMPT_HOSTS[1]}", f"{hosts[0]} aur {hosts[1]}")
    return text


def target_words(minutes: int) -> int:
    return int(minutes) * WORDS_PER_MINUTE


def target_turns(minutes: int) -> int:
    return round(target_words(minutes) / WORDS_PER_TURN)


def _person(name: str, gender: str) -> str:
    """Who a friend is: a Hindi sentence's verbs follow the speaker's gender."""
    if gender == "man":
        return f'{name} is a young man (his Hindi verbs are masculine: "main samajh gaya")'
    if gender == "woman":
        return f'{name} is a young woman (her Hindi verbs are feminine: "main samajh gayi")'
    return f"{name} is a young student"


def reference(hosts: tuple[str, str], teacher: str) -> str:
    """The reference episode's turns, with this run's friends and teacher in place of its own."""
    lines = []
    for line in REFERENCE_FILE.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines():
        label, sep, said = line.partition(": ")
        if sep and label in ("A", "B"):
            lines.append(f"{hosts[0] if label == 'A' else hosts[1]}: {said.replace(REFERENCE_TEACHER, teacher)}")
    return "\n".join(lines)


def system(hosts: tuple[str, str], teacher: str, minutes: int, rules: str = "",
           genders: tuple[str, str] = ("", "")) -> str:
    a, b = hosts
    text = (
        "You write the script of a spoken revision episode for students. The user's message says what the "
        "episode is: follow it exactly for language, tone, vocabulary and flow.\n"
        f"The two friends: {_person(a, genders[0])}; {_person(b, genders[1])}. {a} opens the talk, asks and "
        f"reacts; {b} explains most of the concepts and sums up each lecture; both joke, laugh and add points.\n"
        "Write it so that two voices can read it aloud and it sounds like real friends talking:\n"
        f'- Only two speakers, exactly "{a}" and "{b}". No narrator.\n'
        "- Most turns are short, about 10 to 25 words and never more than about 50. In between, the other "
        'friend often answers in one to three words ("Wahi toh.", "Sahi baat.", "Exactly.", "Accha.", "Mhm.").\n'
        "- Use natural fillers where they fit (haan, accha, yaar, bhai, arre, uh) and write a laugh as haha.\n"
        f'- The teacher is "{teacher}". Call the teacher exactly that.\n'
        "- Write only what is said aloud: no stage directions, sound effects, brackets, headings, lists, "
        "markdown or emojis.\n"
        "- Say formulas in words, the way Indian classrooms say them in English (for example \"W is equal to "
        'F dot s", "F is equal to minus dU by dx", "mu k cos theta"). Numbers may stay as digits.\n'
        "- Every fact, definition, formula and number comes from the PDF; add nothing that is not in it. Go "
        "through every lecture in order and say which lecture each idea came from (lecture 1, lecture 2 ...). "
        "An everyday analogy may explain an idea, as a good teacher would.\n"
        f"- Length: about {minutes} minutes when spoken: about {target_words(minutes)} words in about "
        f"{target_turns(minutes)} turns.\n"
        "- title: a short episode title in Hinglish with the chapter's name.\n\n"
        f"How a reference episode the team likes sounds (another chapter). Copy only its manner: short turns, "
        "quick reactions, fillers, laughs and how formulas are said. Never copy its content, sentences or opening.\n"
        f"{reference(hosts, teacher)}"
    )
    rules = str(rules or "").strip()
    return text + (f"\n\nAlso follow these instructions:\n{rules}" if rules else "")


def schema(hosts: tuple[str, str]) -> dict:
    return {"type": "object", "properties": {
        "title": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "speaker": {"type": "string", "enum": list(hosts)}, "text": {"type": "string"}},
            "required": ["speaker", "text"]}}},
        "required": ["title", "lines"]}


def identity(**parts) -> str:
    """What a saved script was made from: the same identity, the same script."""
    payload = json.dumps({"revision": REVISION, **parts}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- lines
def _host(name: str, hosts: tuple[str, str]) -> str | None:
    wanted = " ".join(str(name or "").split()).casefold()
    return next((h for h in hosts if h.casefold() == wanted), None)


def clean(text: str, hosts: tuple[str, str] = ()) -> str:
    """A line as a voice reads it: no markdown, no leading stage direction or speaker name."""
    text = " ".join(_MARKUP.sub("", str(text or "")).split())
    text = _DIRECTION.sub("", text)
    name, sep, rest = text.partition(":")
    if sep and hosts and _host(name, hosts):
        text = rest.strip()
    return text


def tidy(raw: dict, hosts: tuple[str, str]) -> tuple[dict, int]:
    """(script, how many lines were tidied) from PW AI's answer; ScriptError if it is not a script."""
    lines, tidied = [], 0
    for item in (raw or {}).get("lines") or []:
        if not isinstance(item, dict):
            continue
        said = clean(item.get("text"), hosts)
        if not said:
            continue
        tidied += said != str(item.get("text") or "").strip()
        speaker = _host(item.get("speaker"), hosts)
        if speaker is None:                        # turn-taking decides
            speaker = hosts[1] if lines and lines[-1]["speaker"] == hosts[0] else hosts[0]
            tidied += 1
        lines.append({"speaker": speaker, "text": said})
    if len(lines) < MIN_LINES:
        raise ScriptError("PW AI's script came back too short.")
    if len({line["speaker"] for line in lines}) < 2:
        raise ScriptError("PW AI's script has only one of the two friends speaking.")
    return {"title": clean((raw or {}).get("title")), "lines": lines}, tidied


def words(doc: dict) -> int:
    return sum(len(line["text"].split()) for line in doc.get("lines") or [])


def to_text(doc: dict, hosts: tuple[str, str], teacher: str) -> str:
    head = [f"# {doc.get('title') or 'Audio notes'}",
            f"# Two friends: {hosts[0]} and {hosts[1]}. Teacher: {teacher}."]
    return "\n".join(head + [""] + [f"{line['speaker']}: {line['text']}" for line in doc["lines"]]) + "\n"


def parse(text: str, hosts: tuple[str, str]) -> dict:
    """A script given back as text (one "Name: words" line per turn; a line without a name
    goes on with the line before). ScriptError names the line that is wrong."""
    text = str(text or "")
    if len(text) > MAX_SCRIPT_CHARS:
        raise ScriptError(f"The script is too long (at most {MAX_SCRIPT_CHARS:,} characters).")
    title, lines = "", []
    for number, raw in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not title and not lines:
                title = line.lstrip("#").strip()
            continue
        name, sep, said = line.partition(":")
        speaker = _host(name, hosts) if sep else None
        if speaker:
            said = clean(said, hosts)
            if said:
                lines.append({"speaker": speaker, "text": said})
            continue
        if sep and valid_name(name) and len(name.split()) <= 3:
            raise ScriptError(f'Line {number} is said by "{name.strip()}". Only {hosts[0]} and {hosts[1]} speak: '
                              f'start each line with "{hosts[0]}:" or "{hosts[1]}:".')
        if not lines:
            raise ScriptError(f'Line {number} does not start with "{hosts[0]}:" or "{hosts[1]}:".')
        lines[-1]["text"] = f"{lines[-1]['text']} {clean(line)}".strip()
    if len(lines) < 2:
        raise ScriptError(f'The script needs at least two lines, each starting with "{hosts[0]}:" or "{hosts[1]}:".')
    return {"title": title, "lines": lines}


# --------------------------------------------------------------- the check
def review_system() -> str:
    return (
        "You check the script of a spoken Hinglish revision episode against the lecture notes PDF it was "
        "written from. Report only real problems:\n"
        "1. a fact, definition, formula, number, unit or name that is wrong, or that the notes do not support;\n"
        "2. a scientific or technical term translated into Hindi (it must stay in English);\n"
        "3. something a voice cannot read aloud: symbols, brackets, markdown or stage directions.\n"
        "For each problem give the line number, the line's exact current text (old) and the corrected line "
        "(new): the same speaker's Hinglish in the same tone, as close to the current line as possible.\n"
        "Do not change style, jokes, slang or the praise of the teacher, and do not shorten lines.\n"
        "Also list the important topics of the notes that the script never mentions."
    )


def review_schema() -> dict:
    return {"type": "object", "properties": {
        "corrections": {"type": "array", "items": {"type": "object", "properties": {
            "line": {"type": "integer"}, "old": {"type": "string"}, "new": {"type": "string"},
            "reason": {"type": "string"}}, "required": ["line", "old", "new", "reason"]}},
        "missing": {"type": "array", "items": {"type": "object", "properties": {"topic": {"type": "string"}},
                                               "required": ["topic"]}}},
        "required": ["corrections", "missing"]}


def numbered(doc: dict) -> str:
    return "\n".join(f"{n}\t{line['speaker']}: {line['text']}" for n, line in enumerate(doc["lines"], start=1))


def apply_review(doc: dict, found: dict, hosts: tuple[str, str]) -> dict:
    """Apply the check's corrections in place: a whole line when "old" is the line, else the
    one place "old" stands in it. Anything else is left as it was and listed as skipped."""
    outcome = {"asked": True, "done": True, "applied": [], "skipped": [], "missing": [], "problem": ""}
    for fix in (found or {}).get("corrections") or []:
        if not isinstance(fix, dict):
            continue
        try:
            number = int(fix.get("line"))
        except (TypeError, ValueError):
            number = 0
        entry = {"line": number, "old": clean(fix.get("old"), hosts), "new": clean(fix.get("new"), hosts),
                 "reason": " ".join(str(fix.get("reason") or "").split())}
        if not (1 <= number <= len(doc["lines"])) or not entry["new"] or entry["new"] == entry["old"]:
            outcome["skipped"].append(entry)
            continue
        line = doc["lines"][number - 1]
        if entry["old"] == line["text"]:
            line["text"] = entry["new"]
        elif entry["old"] and line["text"].count(entry["old"]) == 1:
            line["text"] = line["text"].replace(entry["old"], entry["new"])
        else:
            outcome["skipped"].append(entry)
            continue
        outcome["applied"].append(entry)
    outcome["missing"] = [" ".join(str(m.get("topic") or "").split()) for m in (found or {}).get("missing") or []
                          if isinstance(m, dict) and str(m.get("topic") or "").strip()]
    return outcome
