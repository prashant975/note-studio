"""Sheet value -> generator enum mapping, output naming, signature rules."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from notesbot.output_naming import OUTPUT_SUFFIX, output_name

SUBJECTS = ("auto", "chemistry", "physics", "biology", "mathematics", "sanskrit",
            "science", "social_science", "english", "hindi", "other")
LANGUAGES = ("auto", "english", "hindi", "hinglish", "sanskrit")


_BIOLOGY_TOKENS = ("botany", "zoology", "bio", "जीव", "वनस्पति", "प्राणि", "प्राणी")
# Foundation (school) subjects. Social science covers its four textbooks.
_SOCIAL_SCIENCE_TOKENS = ("sst", "social", "history", "geography", "civics", "political",
                          "economics", "सामाजिक", "इतिहास", "भूगोल", "नागरिक", "अर्थशास्त्र")


# -- subject names ---------------------------------------------------------------
# One spelling per subject wherever a subject is shown: the page-1 tag and the
# catalog's Drive folders. The cells vary ("Physics by Saleem Sir", "(AIR)
# Maths", "Zoology AIR", "sanskrit", "Phyiscs", "Mat"); the name drops the
# teacher and the AIR tag and takes the standard spelling. Chemistry and
# Biology keep the branch the cell names.
_SUBJECT_NAMES = {
    # spelling (compared lower-case, punctuation as spaces) -> (name, SUBJECTS id)
    **dict.fromkeys(("physics", "phyiscs", "physic", "phy", "भौतिकी", "भौतिक विज्ञान"),
                    ("Physics", "physics")),
    **dict.fromkeys(("chemistry", "chem", "रसायन", "रसायन विज्ञान"), ("Chemistry", "chemistry")),
    **dict.fromkeys(("physical chemistry", "भौतिक रसायन"), ("Physical Chemistry", "chemistry")),
    **dict.fromkeys(("organic chemistry", "कार्बनिक रसायन"), ("Organic Chemistry", "chemistry")),
    **dict.fromkeys(("inorganic chemistry", "अकार्बनिक रसायन"), ("Inorganic Chemistry", "chemistry")),
    **dict.fromkeys(("biology", "bio", "जीव विज्ञान"), ("Biology", "biology")),
    **dict.fromkeys(("botany", "वनस्पति विज्ञान"), ("Botany", "biology")),
    **dict.fromkeys(("zoology", "प्राणि विज्ञान", "प्राणी विज्ञान"), ("Zoology", "biology")),
    **dict.fromkeys(("maths", "math", "mathematics", "mat", "गणित"), ("Maths", "mathematics")),
    **dict.fromkeys(("science", "विज्ञान"), ("Science", "science")),
    **dict.fromkeys(("sst", "social science", "social studies", "सामाजिक विज्ञान"),
                    ("Social Science", "social_science")),
    **dict.fromkeys(("english", "अंग्रेज़ी", "अंग्रेजी"), ("English", "english")),
    **dict.fromkeys(("hindi", "हिंदी", "हिन्दी"), ("Hindi", "hindi")),
    **dict.fromkeys(("sanskrit", "संस्कृत", "संस्कृतम्"), ("Sanskrit", "sanskrit")),
    **dict.fromkeys(("computer science", "computer"), ("Computer Science", "auto")),
}
_HINDI_NAMES = {
    "Physics": "भौतिक विज्ञान",
    "Chemistry": "रसायन विज्ञान",
    "Physical Chemistry": "भौतिक रसायन",
    "Organic Chemistry": "कार्बनिक रसायन",
    "Inorganic Chemistry": "अकार्बनिक रसायन",
    "Biology": "जीव विज्ञान",
    "Botany": "वनस्पति विज्ञान",
    "Zoology": "प्राणि विज्ञान",
    "Maths": "गणित",
    "Science": "विज्ञान",
    "Social Science": "सामाजिक विज्ञान",
    "Hindi": "हिन्दी",
    "English": "अंग्रेज़ी",
    "Sanskrit": "संस्कृत",
    "Computer Science": "कंप्यूटर विज्ञान",
}
_TEACHER_RE = re.compile(r"\s+by\s+.+$", re.IGNORECASE)
_AIR_RE = re.compile(r"\(\s*air\s*\)|^air\s+|\s+air$", re.IGNORECASE)
_SPELLING_RE = re.compile(r"[\s\-_.,:;/|()]+")


def _without_tags(raw: object) -> str:
    """The cell without the teacher ("... by X Sir") and the AIR tag."""
    text = " ".join(str(raw or "").split())
    text = _TEACHER_RE.sub("", text)
    return " ".join(_AIR_RE.sub(" ", text).split())


def _known_subject(text: str) -> tuple[str, str] | None:
    return _SUBJECT_NAMES.get(" ".join(_SPELLING_RE.sub(" ", text.casefold()).split()))


def subject_name(raw: object) -> str:
    """The subject's standard name: "(AIR) Maths" -> "Maths", "Physics by
    Saleem Sir" -> "Physics", "sanskrit" -> "Sanskrit". A subject not in the
    list keeps its words, title-cased only when typed all in one case."""
    text = _without_tags(raw)
    if not text:
        return ""
    known = _known_subject(text)
    if known:
        return known[0]
    if text.islower() or text.isupper():
        return text.title()
    return text


def map_subject(raw: object) -> str:
    """Column D -> one of SUBJECTS (never "other").

    "chem" is tested BEFORE "physic": "Physical Chemistry" contains "physic".
    Blank/unknown -> "auto" (never guess). Always strip() first - the sheet has
    values like "Physics " with a trailing space.
    """
    if not isinstance(raw, str):
        return "auto"
    text = raw.strip().lower()
    if not text:
        return "auto"
    known = _known_subject(_without_tags(raw))
    if known:
        return known[1]
    if "sanskrit" in text or "संस्कृत" in text:
        return "sanskrit"
    if "chem" in text or "रसायन" in text:
        return "chemistry"
    if "physic" in text or "भौतिक" in text:
        return "physics"
    if any(token in text for token in _BIOLOGY_TOKENS):
        return "biology"
    if "math" in text or "गणित" in text:
        return "mathematics"
    # After physics/chemistry/biology: "Physical Science" is physics.
    if "science" in text or "विज्ञान" in text:
        return "social_science" if any(token in text for token in _SOCIAL_SCIENCE_TOKENS) else "science"
    if any(token in text for token in _SOCIAL_SCIENCE_TOKENS):
        return "social_science"
    if "english" in text or "अंग्रेज" in text:
        return "english"
    if "hindi" in text or "हिंदी" in text or "हिन्दी" in text:
        return "hindi"
    return "auto"


def map_language(batch: object) -> str:
    """Column C (Batch) -> {auto, english, hindi, hinglish}."""
    if not isinstance(batch, str):
        return "auto"
    text = batch.strip().lower()
    if not text:
        return "auto"
    if "sanskrit" in text or "संस्कृत" in text:
        return "sanskrit"
    if "hinglish" in text:
        return "hinglish"
    if "hindi" in text or "हिंदी" in text or "हिन्दी" in text:
        return "hindi"
    if "english" in text:
        return "english"
    return "auto"


def resolve_subject(mode: str, sheet_value: object) -> str:
    """Apply --subject: 'sheet' maps column D, 'auto' forces auto, else fixed."""
    mode = (mode or "sheet").strip().lower()
    if mode == "sheet":
        return map_subject(sheet_value)
    if mode not in SUBJECTS:
        raise ValueError(
            f"unknown subject {mode!r}; expected 'sheet' or one of {SUBJECTS}"
        )
    return mode


def subject_hint(subject: str) -> str:
    """The generator takes '' for auto, otherwise the enum value."""
    return "" if subject == "auto" else subject


def normalize_signature(name: object, fallback: str = "PW Faculty") -> str:
    """Collapse whitespace and prefix 'by ' unless already present."""
    text = " ".join(str(name or "").split())
    if not text:
        text = " ".join(fallback.split()) or "PW Faculty"
    text = text[:120]
    return text if text[:3].lower() == "by " else f"by {text}"


# "By - Venky Anna", "Faculty: ...", "Faculty Name - ...", "Lecture by ...".
_COVER_BY_RE = re.compile(
    r"^(?:[\s:.\-–—]*(?:by|faculty|teacher|educator|mentor|name|presented|taught|lecture)\b)*[\s:.\-–—]*",
    re.IGNORECASE,
)
# Words that are no part of a teacher's name: titles, and the institute's own
# branding a slide footer may carry.
_NOT_A_NAME = frozenset((
    "sir", "maam", "mam", "madam", "miss", "mr", "mrs", "ms", "dr", "prof", "professor", "ji", "shri",
    "sri", "smt", "pw", "faculty", "teacher", "physics", "wallah", "physicswallah", "live", "vidyapeeth",
    "सर", "मैम", "मैडम", "जी", "श्री", "डॉ", "प्रो",
))
_NAME_TOKEN_RE = re.compile(r"[a-z]+|[ऀ-ॿ]+")
# Devanagari consonants as the Latin letters a name is usually spelled with;
# vowels are dropped on both sides, so "Agarwal", "Agrawal" and "अग्रवाल" agree.
_DEVANAGARI_SOUNDS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
    "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
    "ध": "dh", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l", "ं": "n", "ँ": "n",
}


def _sound_key(token: str) -> str:
    """The consonants of one name word, spelled the same in either script."""
    if re.match(r"[ऀ-ॿ]", token):
        token = "".join(_DEVANAGARI_SOUNDS.get(ch, "") for ch in token)
    else:
        token = re.sub(r"c(?!h)", "k", token.replace("ph", "f").replace("w", "v").replace("z", "j")
                       .replace("q", "k").replace("x", "ks"))
        token = re.sub(r"[aeiou]", "", token)
    token = re.sub(r"(.)\1+", r"\1", token)   # "Bhatt" = "Bhat", "Chhabra" = "Chabra"
    return token[:-1] if len(token) > 1 and token.endswith("y") else token


def _name_keys(name: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(name or "")).casefold().replace("'", "").replace("’", "")
    words = [w for w in _NAME_TOKEN_RE.findall(text) if w not in _NOT_A_NAME]
    # A lone initial ("R. Agarwal") says too little to compare.
    return [key for key in (_sound_key(w) for w in words if len(w) > 1) if key]


def _same_word(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def same_teacher(first: str, second: str) -> bool:
    """True when two spellings name the same teacher: every word of the
    shorter one is in the longer ("Rohit Sir" / "Rohit Agarwal Sir"), with
    titles ignored and Hindi and English spellings compared by sound."""
    a, b = _name_keys(first), _name_keys(second)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return all(any(_same_word(word, other) for other in longer) for word in shorter)


def slide_teacher(notes) -> str:
    """The teacher's name the notes model read on the slides, or "" when it
    found none (or only a title or the institute's name)."""
    name = _COVER_BY_RE.sub("", " ".join(str(getattr(notes, "faculty", "") or "").split())).strip()
    if not name or len(name) > 60 or not _name_keys(name):
        return ""
    return name


def choose_signature(signature: str, notes, *, fixed: bool = False) -> tuple[str, str]:
    """(signature to print, why) for one lecture.

    ``signature`` is the sheet's faculty name ("by PW Faculty" when the row
    names nobody). The teacher printed on the slides wins when the two
    differ - the slides show who actually took the class; when they name the
    same teacher, the sheet's spelling is kept. ``fixed``: the signature was
    typed in the Studio for the whole run and is used as is.
    """
    if fixed:
        return signature, "typed in the Studio"
    printed = slide_teacher(notes)
    sheet_names_nobody = signature == normalize_signature("")
    if not printed:
        return signature, ("no teacher on the sheet or the slides" if sheet_names_nobody
                           else "from the sheet (the slides name no teacher)")
    if sheet_names_nobody:
        return normalize_signature(printed), "from the slides (the sheet names no teacher)"
    sheet_name = _COVER_BY_RE.sub("", signature)
    if same_teacher(sheet_name, printed):
        return signature, f"the sheet and the slides ({printed!r}) name the same teacher"
    return normalize_signature(printed), f"the slides name a different teacher than the sheet ({sheet_name!r})"


def signature_for(signature: str, notes, *, fixed: bool = False) -> str:
    """The signature to print; see ``choose_signature``."""
    return choose_signature(signature, notes, fixed=fixed)[0]


# -- page-1 header -----------------------------------------------------------
# The sheet is the authority for what the lecture IS. The model only reads the
# slides, and a cover slide shortens the chapter, carries another batch's year
# or has no lecture number at all - so the header comes from columns D/G/H and
# the model's metadata is only the fallback for a blank cell.

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


@dataclass(frozen=True)
class SheetHeader:
    subject: str = ""   # the "( ... )" tag under the title
    chapter: str = ""   # the title
    lecture: str = ""   # "Lecture - 05"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def display_chapter(raw: object, language: str = "") -> str:
    """Column G as the title. The cell often holds two spellings on separate
    lines ('English name' / 'हिन्दी नाम'): Hindi notes take the Devanagari
    line, every other language the Latin one; a single line is used as is."""
    lines = [_one_line(line) for line in str(raw or "").splitlines() if line.strip()]
    if not lines:
        return ""
    hindi = [line for line in lines if _DEVANAGARI_RE.search(line)]
    latin = [line for line in lines if not _DEVANAGARI_RE.search(line)]
    if language in {"hindi", "sanskrit"} and hindi:
        return hindi[0]
    if language == "sanskrit":
        return ""
    if language != "hindi" and latin:
        return latin[0]
    return lines[0]


def display_subject(raw: object, language: str = "") -> str:
    """Column D by its standard name (see subject_name; "Physical Chemistry"
    stays specific); Hindi notes get the Hindi name of a known subject."""
    text = subject_name(raw)
    if not text:
        return ""
    if language == "sanskrit":
        from notesbot.sanskrit import has_latin
        if text == "Sanskrit":
            return "संस्कृतम्"
        typed = _without_tags(raw)
        return "" if has_latin(typed) else typed
    if language == "hindi":
        return _HINDI_NAMES.get(text, text)
    return text


def display_lecture(raw: object) -> str:
    """Column H -> 'Lecture - 05'. A lecture token wins over a stray number
    ('2027 L05' is lecture 5); a cell with no number shows nothing."""
    from .lecture_match import lecture_numbers, parse_lecture_number

    text = str(raw or "")
    tokens = sorted(lecture_numbers(text))
    number = tokens[0] if tokens else parse_lecture_number(_YEAR_RE.sub(" ", text))
    return f"Lecture - {number:02d}" if number is not None else ""


def sheet_header(*, subject: object, chapter: object, lecture: object, language: str = "") -> SheetHeader:
    lecture_text = display_lecture(lecture)
    if language == "sanskrit":
        from notesbot.sanskrit import devanagari_number
        lecture_text = devanagari_number(lecture_text.replace("Lecture", "पाठः"))
    return SheetHeader(
        subject=display_subject(subject, language),
        chapter=display_chapter(chapter, language),
        lecture=lecture_text,
    )


def apply_sheet_header(notes, header: SheetHeader | None) -> None:
    """Put the sheet's subject, chapter and lecture number on the notes.

    A blank sheet cell keeps what the model read from the slides. The
    exam/batch tag the model copies from a cover slide ("JEE 2026") is shown
    before the subject, so it goes whenever the sheet names the subject, and
    otherwise loses its year - the batch year belongs to the sheet, not the
    slide.
    """
    if header is None:
        return
    if header.chapter:
        notes.title = header.chapter
    if header.lecture:
        notes.lecture_no = header.lecture
    if header.subject:
        notes.subject = header.subject
        notes.audience = ""
    elif getattr(notes, "audience", ""):
        notes.audience = _one_line(_YEAR_RE.sub(" ", notes.audience))
