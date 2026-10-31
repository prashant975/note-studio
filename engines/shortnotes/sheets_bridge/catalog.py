"""Rows of a *catalog* sheet: exam / batch / subject / chapter / lecture topic / link.

The content team's catalog lists every lecture with a direct PDF link instead
of a Drive folder, and carries no status columns. Its notes are filed in a
Drive tree the automation builds under the profile's output folder:

    <exam category>/<batch>/<subject>/<chapter>/<lecture title> Concise Notes.pdf

Everything that reads those cells lives here: the category behind an exam
name, the folder names, the file name, the lecture number, the teacher named
in the subject cell, the language of regional batches, and the batches that
are not lectures at all. The sheet is the authority for names: a folder keeps
the cell's text as typed, with only whitespace tidied - except the subject,
which takes its standard name (mapping.subject_name) so one subject is one
folder whichever teacher or AIR tag the cell carries.
"""
from __future__ import annotations

import re

from notesbot import categories
from notesbot.output_naming import OUTPUT_SUFFIX

from .mapping import map_language, subject_name

# Exam cell (comparison form) -> exam. IIT-JEE and JEE are one exam: the
# catalog uses both spellings for the same batches. NEET and JEE share the
# NEET-JEE note format (categories.LEGACY) but keep their own Drive folders.
_EXAMS = {
    "neet": "neet",
    "neet ug": "neet",
    "jee": "jee",
    "iit jee": "jee",
    "iitjee": "jee",
    "jee main": "jee",
    "jee mains": "jee",
    "jee advanced": "jee",
    "jee main advanced": "jee",
    "neet jee": "neet_jee",
    "foundation": "foundation",
}
# Spellings an operator may already have given an exam folder by hand; the
# first is the name a new folder gets.
_EXAM_FOLDERS = {
    "neet": ("NEET", "NEET UG"),
    "jee": ("JEE", "IIT-JEE", "IIT JEE", "IITJEE"),
    "neet_jee": ("NEET-JEE",),
    "foundation": ("Foundation",),
}

# Batches that are not lectures: marketing streams (Pi Lens: current affairs,
# UGC NET, editorials), QA/test batches and DPP feeds. Matched as substrings,
# case-insensitively. A profile's ``skipBatches`` replaces this list.
DEFAULT_SKIP_BATCHES = ("Pi Lens", "UXCC", "VP LIVE", "Smart DPP")

# Batches taught in a script the PDF renderer has no font for (it draws Latin
# and Devanagari). Their notes are written in English.
_REGIONAL = ("gujarati", "telugu", "tinglish", "tamil", "kannada", "bangla", "bengali",
             "marathi", "odia", "oriya", "malayalam", "assamese", "punjabi")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_SEGMENT = re.compile(r"\s*\|\|\s*")
# Scheduling noise that can trail a topic inside its first segment.
_ADMIN_TAIL = re.compile(
    r"(?:[\s\-:|]*(?:\(\s*no\s*dpp\s*\)|no\s*dpp|\(\s*recorded\s*\)|\brecorded|extra\s+recorded(?:\s+lecture)?"
    r"|reschedul\w*.*|\(\s*one\s*shot\s*\)|extra\s+class))+\s*$",
    re.IGNORECASE,
)
# "Capacitance 3: Work done", "Parabola 10 : Introduction". Not a clock time
# ("@08:00") and not part of a word or a version ("2.0").
_NUMBER_BEFORE_COLON = re.compile(r"(?<![\w.])(\d{1,3})\s*:(?!\d)")
_BY = re.compile(r"\s+by\s+(.+?)\s*$", re.IGNORECASE)
_CH_PREFIX = re.compile(r"^ch(?:apter)?[\s\-_.]*\d+\s*[:\-\u2013.]?\s*", re.IGNORECASE)
_OTHER_INDIC = re.compile(r"[\u0980-\u0dff]")        # Bengali .. Malayalam (incl. Gurmukhi, Gujarati)
_LATIN_PAREN = re.compile(r"\(([^()]*[A-Za-z]{3,}[^()]*)\)")
_LATIN_TO_DEVA = re.compile(r"(?<=[A-Za-z0-9)])\s*(?=[\u0900-\u097f])")
_DEVA_TO_LATIN = re.compile(r"(?<=[\u0900-\u097f])\s*(?=[A-Za-z(])")
_DEVANAGARI_DIGITS = str.maketrans("\u0966\u0967\u0968\u0969\u096a\u096b\u096c\u096d\u096e\u096f", "0123456789")

MAX_STEM_LENGTH = 150


def clean_name(text: object) -> str:
    """The cell as typed, with control characters dropped and whitespace tidied."""
    return " ".join(_CONTROL.sub(" ", str(text or "")).split())


def _comparison(text: object) -> str:
    return " ".join(_NON_WORD.sub(" ", str(text or "").casefold()).split())


# -- exam ---------------------------------------------------------------------
def category_for_exam(exam: object) -> tuple[str, str]:
    """(category id or "", folder name).

    A known exam ("NEET", "IIT-JEE", "JEE") gives its category, whose format
    rules the notes follow, and its own folder name ("NEET", "JEE"). An
    unknown exam keeps its own text as the folder name, and the run's
    category decides the format rules.
    """
    known = _EXAMS.get(_comparison(exam), "")
    if known:
        return categories.normalize(known), _EXAM_FOLDERS[known][0]
    return "", clean_name(exam) or "Unsorted"


def exam_folder_names(exam: object) -> tuple[str, ...]:
    """Acceptable names for the exam folder, the one to create first.

    An existing "IIT-JEE" folder is the JEE folder: it is used rather than a
    second "JEE" beside it.
    """
    known = _EXAMS.get(_comparison(exam), "")
    return _EXAM_FOLDERS[known] if known else (clean_name(exam) or "Unsorted",)


def folder_path(exam: object, batch: object, subject: object, chapter: object) -> tuple:
    """Folder levels under the output folder: the exam level is a tuple of
    acceptable names (see exam_folder_names), then batch, subject (its
    standard name), chapter."""
    return (exam_folder_names(exam), clean_name(batch) or "Unnamed",
            subject_name(subject) or "Unnamed", clean_name(chapter) or "Unnamed")


def display_path(path) -> str:
    """'JEE / Lakshya JEE 2027 / Physics / Capacitance' for logs."""
    return " / ".join(level[0] if isinstance(level, tuple) else str(level) for level in path)


# -- lecture topic --------------------------------------------------------------
def lecture_title(topic: object) -> str:
    """The lecture's own name: the first ``||`` segment without scheduling noise.

    "Alcohol, Phenols and Ether 06 : Grignard Reagents || NO DPP || Reschedule
    @08:00 AM" -> "Alcohol, Phenols and Ether 06 : Grignard Reagents".
    """
    text = clean_name(topic)
    if not text:
        return ""
    first = _SEGMENT.split(text)[0]
    title = _ADMIN_TAIL.sub("", first).strip().rstrip(" :-\u2013|").strip()
    # A topic that *starts* with a scheduling note keeps its words.
    return title or first.strip().rstrip(" :-\u2013|").strip()


def _shorten(title: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a word, no dangling bracket."""
    if len(title) <= limit:
        return title
    cut = title[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    cut = cut.rstrip(" ,:;-\u2013|(")
    if cut.count("(") > cut.count(")"):
        cut = cut[:cut.rfind("(")].rstrip(" ,:;-\u2013|")
    return cut or title[:limit]


def lecture_number(topic: object) -> int | None:
    """The number written right before the colon ("Capacitance 3: Work done"
    -> 3), else a lecture token ("L05"), else None. Never simply the first
    integer: "Class 11th Revision ... 1: Type of Fission" is lecture 1."""
    text = str(topic or "").translate(_DEVANAGARI_DIGITS)
    first = _SEGMENT.split(text)[0]
    match = _NUMBER_BEFORE_COLON.search(first)
    if match:
        return int(match.group(1))
    from .lecture_match import lecture_numbers

    tokens = sorted(lecture_numbers(first))
    return tokens[0] if tokens else None


def output_name(topic: object, ordinal: int = 1) -> str:
    """The file name: lecture title + " Concise Notes.pdf". ``ordinal`` > 1
    adds " (2)" for a second, different lecture that would share the name."""
    title = _shorten(lecture_title(topic) or "Lecture", MAX_STEM_LENGTH)
    if ordinal > 1:
        title = f"{title} ({ordinal})"
    return f"{title}{OUTPUT_SUFFIX}"


# -- subject, chapter, batch ---------------------------------------------------
def faculty(subject: object) -> str:
    """The teacher named in the subject cell: "Physics By Saleem Ahmad Sir"
    -> "Saleem Ahmad Sir"; "" when the cell names none."""
    match = _BY.search(clean_name(subject))
    return match.group(1).strip() if match else ""


def header_subject(subject: object) -> str:
    """The subject for page 1: its standard name, without the teacher and the AIR tag."""
    return subject_name(subject)


def header_chapter(chapter: object) -> str:
    """The chapter for page 1.

    A "Ch-09 :" prefix goes. A chapter written in a script the renderer
    cannot draw (Gujarati, Bengali ...) uses the English name the cell carries
    in parentheses, or nothing (the slides' own title is then used). English
    and Hindi written together ("Biomolecules" directly followed by the Hindi
    name) are split onto two lines, so page 1 takes the line of the notes'
    language, exactly as it does for a classic sheet's two-line chapter cell.
    """
    text = _CH_PREFIX.sub("", clean_name(chapter))
    if _OTHER_INDIC.search(text):
        match = _LATIN_PAREN.search(text)
        return match.group(1).strip() if match else ""
    text = _LATIN_TO_DEVA.sub("\n", text)
    text = _DEVA_TO_LATIN.sub("\n", text)
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 2 and line[0] == "(" and line[-1] == ")":
            line = line[1:-1].strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def is_regional(batch: object) -> str:
    """The regional language a batch is taught in ("gujarati"), or ""."""
    words = set(_comparison(batch).split())
    for name in _REGIONAL:
        if name in words:
            return name
    return ""


def language_for_batch(batch: object) -> str:
    """Notes language for a row: regional batches get English notes (the
    renderer cannot draw their script); otherwise the classic Batch rule
    (a Hindi batch -> Hindi, else detected from the slides)."""
    if is_regional(batch):
        return "english"
    return map_language(batch)


# -- rows to leave alone --------------------------------------------------------
def skip_reason(batch: object, skip_batches=None) -> str:
    """Why this row is not a lecture, or "" when it is one."""
    entries = DEFAULT_SKIP_BATCHES if skip_batches is None else tuple(skip_batches)
    haystack = clean_name(batch).casefold()
    for entry in entries:
        needle = clean_name(entry).casefold()
        if needle and needle in haystack:
            return f"batch matches the skip list entry {clean_name(entry)!r}"
    return ""
