"""Rewrite the lines of Hindi / Marathi notes that slipped into English.

The prompt forbids Roman script inside a Devanagari sentence, with worked
examples, and the model still writes lines like

    इनमें counter-terrorism और HADR (Humanitarian Assistance and Disaster Relief) शामिल हैं।
    इसके छह प्रमुख अंग हैं — General Assembly, Security Council ... और Secretariat।

src/script_validator.py finds those lines, but only ever listed them as a
warning, so they reached the students as they were. This sends ONLY the flagged
lines back to the model in one short call, and splices each answer in when it
is plainly the same line in better words: the same bullet and indent, the same
numbers and links, and less Roman script than before. A line that fails any
check is kept exactly as it was and stays in the warnings for a reviewer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .languages import is_devanagari, language_name, normalise
from .script_validator import find_latin_in_hindi_body

#: More flagged lines than this is a run that ignored the language altogether;
#: patching it line by line would not make it read as Hindi.
MAX_REPAIR_LINES = 80

_PREFIX_RE = re.compile(r"^[ \t]*(?:[-*•●○◦▪][ \t]+|\d{1,2}[.)][ \t]+)?")
_NUMBER_RE = re.compile(r"[+−-]?\d[\d,]*(?:\.\d+)?")
_URL_RE = re.compile(r"https?://[^\s)]+")
_ANSWER_RE = re.compile(r"^\s*<<\s*(\d+)\s*>>[ \t]?(.*)$")
_BLOCK_START_RE = re.compile(r"^[ \t]*#CORRECTION_START\b", re.I)
_BLOCK_END_RE = re.compile(r"^[ \t]*#CORRECTION_END\b", re.I)

#: Worked examples in the target language, taken from lines reviewers sent back.
_EXAMPLES = {
    "hi": (
        "- counter-terrorism → आतंकवाद-रोधी; General Assembly → महासभा; "
        "Security Council → सुरक्षा परिषद; Secretariat → सचिवालय; "
        "assessed contributions → निर्धारित अंशदान; cost-effectiveness → लागत-प्रभावशीलता\n"
        "- Names in Devanagari: Sagarika → सागरिका; Yudh Abhyas → युद्ध अभ्यास\n"
        "- An abbreviation keeps its letters, with the full form in Hindi: "
        "HADR (Humanitarian Assistance and Disaster Relief) → "
        "मानवीय सहायता एवं आपदा राहत (HADR)"
    ),
    "mr": (
        "- counter-terrorism → दहशतवादविरोधी; General Assembly → महासभा; "
        "Security Council → सुरक्षा परिषद; Secretariat → सचिवालय\n"
        "- Names in Devanagari: Sagarika → सागरिका\n"
        "- An abbreviation keeps its letters, with the full form in Marathi: "
        "HADR (Humanitarian Assistance and Disaster Relief) → "
        "मानवतावादी सहाय्य आणि आपत्ती निवारण (HADR)"
    ),
}


@dataclass
class ScriptRepairResult:
    text: str
    flagged_lines: int = 0
    repaired_lines: int = 0
    rejected: list[str] = field(default_factory=list)
    error: str = ""
    #: The model's response, so the run can count the call's tokens and cost.
    response: object = None

    def summary(self) -> dict:
        """For run_log.json."""
        return {
            "flagged_lines": self.flagged_lines,
            "repaired_lines": self.repaired_lines,
            "kept_as_written": self.rejected[:20],
            "error": self.error or None,
        }


def build_script_repair_prompt(lines: dict[int, str], language_code: str) -> str:
    name = language_name(language_code)
    numbered = "\n".join(f"<<{n}>> {line}" for n, line in lines.items())
    return (
        f"These lines come from {name} class notes for UPSC students. Each one has "
        f"English words in Roman letters inside {name} text, which the notes must not have.\n\n"
        f"Rewrite every line in {name}, written in Devanagari:\n"
        f"- Translate English words and phrases into standard {name} exam vocabulary.\n"
        f"- Write names of people, places, missiles, exercises and schemes in Devanagari.\n"
        f"- An English term may stay only in brackets right after its {name} term, the way "
        f"राजकोषीय घाटा (Fiscal Deficit) is written. Abbreviations (UNSC, SLBM, GST) keep "
        f"their letters.\n"
        f"- Keep everything else exactly: the leading \"- \" and its indentation, the **bold** "
        f"markers, markdown links and their URLs, numbers, dates, units and chemical formulas.\n"
        f"- Do not add, drop or change any fact. Do not merge or split lines.\n\n"
        f"Examples:\n{_EXAMPLES.get(normalise(language_code), _EXAMPLES['hi'])}\n\n"
        f"Answer with the same numbered lines and nothing else, each as \"<<n>> text\".\n\n"
        f"{numbered}\n"
    )


def _numbers(text: str) -> tuple[str, ...]:
    # Preserve signs, repetition and order: a subset check allowed invented
    # dates and changed negative values to pass as a language-only repair.
    return tuple(n.replace(",", "").replace("−", "-") for n in _NUMBER_RE.findall(text))


def _latin_runs(line: str) -> int:
    return len(find_latin_in_hindi_body(line))


def _accept(old: str, new: str) -> tuple[str | None, str]:
    """The line to splice in, or None and why it was refused."""
    prefix = _PREFIX_RE.match(old).group(0)
    body = _PREFIX_RE.sub("", new.strip(), count=1).strip()
    if not body:
        return None, "empty answer"
    candidate = prefix + body
    if _numbers(old) != _numbers(candidate):
        return None, "a number changed"
    if _URL_RE.findall(old) != _URL_RE.findall(candidate):
        return None, "a link changed"
    if candidate.count("**") % 2:
        return None, "unbalanced bold"
    if not 0.5 <= len(candidate) / max(1, len(old)) <= 2.0:
        return None, "length changed too much"
    if _latin_runs(candidate) >= _latin_runs(old):
        return None, "no less English than before"
    return candidate, ""


def repair_latin_in_devanagari(notes_text: str, client, language_code: str, *,
                               max_output_tokens: int = 8000) -> ScriptRepairResult:
    """Ask the model to rewrite the flagged lines; splice in the answers that pass."""
    result = ScriptRepairResult(text=notes_text or "")
    if not is_devanagari(language_code) or client is None or not notes_text:
        return result
    lines = notes_text.split("\n")
    skip: set[int] = set()
    inside = False
    for i, line in enumerate(lines, start=1):
        if _BLOCK_START_RE.match(line):
            inside = True
        if inside:
            skip.add(i)
        if _BLOCK_END_RE.match(line):
            inside = False
    flagged = sorted({run.line_no for run in find_latin_in_hindi_body("\n".join(lines))}
                     - skip)
    flagged = [n for n in flagged if not lines[n - 1].lstrip().startswith("#")]
    result.flagged_lines = len(flagged)
    if not flagged:
        return result
    if len(flagged) > MAX_REPAIR_LINES:
        result.error = (f"{len(flagged)} lines carry English; more than {MAX_REPAIR_LINES} "
                        f"is not repaired line by line")
        return result

    wanted = {n: lines[n - 1] for n in flagged}
    try:
        response = client.generate(build_script_repair_prompt(wanted, language_code), [],
                                   max_images=0, max_output_tokens=max_output_tokens,
                                   temperature=0.1)
    except Exception as exc:  # noqa: BLE001 - a repair is never worth a run
        result.error = f"the repair call failed: {exc}"
        return result
    result.response = response

    answers: dict[int, str] = {}
    for raw in (getattr(response, "text", "") or "").splitlines():
        match = _ANSWER_RE.match(raw)
        if match and int(match.group(1)) in wanted:
            answers.setdefault(int(match.group(1)), match.group(2))
    for n, old in wanted.items():
        if n not in answers:
            result.rejected.append(f"line {n}: no answer")
            continue
        new, why = _accept(old, answers[n])
        if new is None:
            result.rejected.append(f"line {n}: {why}")
            continue
        lines[n - 1] = new
        result.repaired_lines += 1
    result.text = "\n".join(lines)
    return result
