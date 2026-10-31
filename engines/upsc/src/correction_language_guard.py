"""Ban correction meta-language from the student notes — enforced, not requested.

Corrections must be silent: the notes state the correct fact as settled
knowledge; what changed is recorded in the error report the student never
sees. The prompt already says so, but a prompt is a request. This module is
the enforcement: the pipeline runs :func:`assert_no_correction_metalanguage`
on the final notes text and FAILS THE BUILD on a hit, because a leaked
"as stated in the lecture" in one shipped document costs more than a re-run.

Applies to both scripts, and both need the same nuance: the vocabulary of
*correcting* is also ordinary subject vocabulary, and banning the bare word
fails runs over the syllabus.

In Hindi that is "सुधार" — the ordinary word for *reform* (भूमि सुधार, आर्थिक
सुधार are legitimate exam content), banned only in correction contexts.

In English it is "corrected" and "rectified". A class on DEFECTS OF VISION
cannot be taught without "myopia is corrected using a concave lens"; a class on
power electronics cannot be taught without "AC is rectified to DC". Both were
banned outright until a vision lecture failed four times in a row, after
transcription, generation and QC had all been paid for.

The distinction that replaced the word-ban is what the word points AT. Pointing
at a VALUE — "corrected to 73%", "corrected from 1970" — is the notes narrating
their own edit. Pointing at a REMEDY — "corrected using a concave lens",
"cannot be corrected with spectacles" — is a class describing the world.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Markdown emphasis and list markers, stripped before matching so the patterns
#: see what a reader sees. Without this, "**Correction**: the year is 1983"
#: slipped past the announcement rule — the ``**`` sits between the word and the
#: colon — while the harmless sentence after it was caught instead.
_EMPHASIS_RE = re.compile(r"[*_`]+")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+•●○▪–]|\d{1,2}[.)])[ \t]+")


def _normalise(line: str) -> str:
    """One line as a reader meets it: no bullet marker, no emphasis marks."""
    return _EMPHASIS_RE.sub("", _BULLET_RE.sub("", line)).strip()


#: Physical remedies and the conditions they treat. A line carrying any of these
#: is teaching how a defect is put right — optics, medicine, engineering — not
#: announcing an edit, so "Correction:" on it is a content sub-heading of the
#: kind every textbook prints under a defect.
_REMEDY_RE = re.compile(
    r"\b(?:lens|lenses|spectacles?|eyeglasses|glasses|eyewear|"
    r"bifocals?|trifocals?|convex|concave|cylindrical|spherical|dioptre|diopter|"
    r"surgery|surgical|surgically|operation|implant|transplant|laser|lasik|"
    r"refractive|prescription|treatment|therapy|medication|"
    r"hearing\s+aid|prosthes[ie]s|prosthetic|"
    r"myopia|hypermetropia|hyperopia|presbyopia|astigmatism|cataract|glaucoma)\b",
    re.I)

#: Checked on every line, whatever else it says. None of these has an innocent
#: reading — they all reference the lecture or announce a fact was changed.
_FORBIDDEN_EN: tuple[tuple[str, re.Pattern], ...] = (
    ("CORRECTED FACTS heading",
     re.compile(r"corrected\s+facts", re.I)),
    ("reference to what the lecture said",
     re.compile(r"\bas\s+(?:stated|said|taught|mentioned)\s+(?:here|in\s+the\s+(?:lecture|class|transcript))\b", re.I)),
    ("reference to the transcript's figure",
     re.compile(r"\bthe\s+transcript'?s\s+figure\b", re.I)),
    ("contrast with what was said",
     re.compile(r"\bcontrary\s+to\s+what\s+was\s+(?:said|taught|stated)\b", re.I)),
    # "corrected" pointing at a VALUE, or announcing the act of correcting.
    # The negative lookahead is what lets "is corrected using a concave lens"
    # through while "was corrected to 73%" is still caught.
    ("the word 'corrected'",
     re.compile(
         r"\bcorrected\s+(?:to|from)\b"
         r"|\b(?:has|have|had|is|was|were|been)\s+(?:been\s+)?corrected\b"
         r"(?!\s+(?:using|with|by|through|via|surgically))"
         r"|\bcorrected\s+(?:in|within)\s+the\s+(?:notes?|text|document|above)\b"
         r"|\bcorrected\s+(?:version|figure|value|statement|fact|year|date)\b",
         re.I)),
    # "rectified" only where it is about an error being fixed. Left alone for
    # rectifiers, rectification and rectified spirit, all of which are syllabus.
    ("the word 'rectified'",
     re.compile(
         r"\brectified\s+(?:in|within)\s+the\s+(?:notes?|text|document|above)\b"
         r"|\b(?:error|mistake|typo|figure|value|statement|claim|fact|date|year)\b"
         r"[^.\n]{0,40}\brectified\b"
         r"|\brectified\s+(?:above|earlier|here)\b",
         re.I)),
)

#: Checked only when the line shows no remedy vocabulary. "Correction:" is a
#: leak on a polity line and a sub-heading on an optics line, and the remedy
#: words are what tell the two apart.
_REMEDY_GATED_EN: tuple[tuple[str, re.Pattern], ...] = (
    ("correction announcement",
     re.compile(r"\bcorrection\s*:", re.I)),
)

# Devanagari has no \b in Python's re; the boundaries are hand-rolled as
# "not preceded/followed by another Devanagari letter or matra".
_DEV = "ऀ-ॿ"
_FORBIDDEN_HI: tuple[tuple[str, re.Pattern], ...] = (
    ("तथ्य-सुधार heading",
     re.compile(r"UPSC\s*महत्वपूर्ण\s*तथ्य\s*सुधार")),
    ("संशोधित तथ्य (revised-fact announcement)",
     re.compile(r"संशोधित\s*तथ्य")),
    ("त्रुटि सुधार (error-fix announcement)",
     re.compile(r"त्रुटि\s*सुधार")),
    ("तथ्य सुधार (fact-fix announcement)",
     re.compile(r"तथ्य\s*सुधार")),
    ("सुधार as a correction announcement",
     re.compile(rf"(?<![{_DEV}])सुधार\s*[:：]")),
    ("जैसा कहा गया (as was said)",
     re.compile(r"जैसा\s*कहा\s*गया")),
    ("व्याख्यान के अनुसार गलत (wrong per the lecture)",
     re.compile(r"व्याख्यान\s*के\s*अनुसार\s*गलत")),
)


@dataclass(frozen=True)
class CorrectionLanguageHit:
    """One banned phrase, where it was found."""

    rule: str
    line_no: int
    snippet: str

    def __str__(self) -> str:
        return f"line {self.line_no}: {self.rule} — {self.snippet!r}"


class CorrectionLanguageError(ValueError):
    """Raised when correction meta-language would reach a student document."""

    def __init__(self, hits: list[CorrectionLanguageHit]):
        self.hits = hits
        listing = "\n".join(f"  - {hit}" for hit in hits)
        super().__init__(
            "Correction meta-language found in the notes — corrections must be "
            f"silent (state the fact, never the fix):\n{listing}"
        )


def _snippet(line: str, match: re.Match, radius: int = 40) -> str:
    start = max(0, match.start() - radius)
    end = min(len(line), match.end() + radius)
    return ("…" if start else "") + line[start:end].strip() + ("…" if end < len(line) else "")


#: Structured #CORRECTION blocks (the CORRECTIONS_ENABLED feature) are exempt:
#: their SOURCE:/CORRECTED: field names would otherwise trip the ban, and
#: their content renders inside a marked callout, never as running text. The
#: blocks are blanked line-for-line so every other hit keeps its line number.
_CORRECTION_BLOCK_RE = re.compile(
    r"^[ \t]*#CORRECTION_START[ \t]*$.*?^[ \t]*#CORRECTION_END[ \t]*$",
    re.I | re.M | re.S)


def find_correction_metalanguage(text: str) -> list[CorrectionLanguageHit]:
    """Every banned phrase in `text`, with line numbers. Both scripts, always —
    an English announcement inside a Hindi note is just as much a leak."""
    text = _CORRECTION_BLOCK_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text or "")
    hits: list[CorrectionLanguageHit] = []
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = _normalise(raw)
        if not line:
            continue
        rules = (*_FORBIDDEN_EN, *_FORBIDDEN_HI)
        if not _REMEDY_RE.search(line):
            rules = (*rules, *_REMEDY_GATED_EN)
        for rule, pattern in rules:
            match = pattern.search(line)
            if match:
                hits.append(CorrectionLanguageHit(rule, line_no, _snippet(line, match)))
    return hits


def assert_no_correction_metalanguage(text: str) -> None:
    """Raise :class:`CorrectionLanguageError` if any banned phrase is present.

    Kept for callers that genuinely cannot proceed with a hit. The pipeline is
    NOT one of them any more — see :func:`correction_language_rows`.
    """
    hits = find_correction_metalanguage(text)
    if hits:
        raise CorrectionLanguageError(hits)


#: The Error Report category these findings file under. Named so a reviewer
#: reading the sheet knows this row is about how the note is WRITTEN, not about
#: something the lecturer got wrong.
CORRECTION_LANGUAGE_ERROR_TYPE = "Correction meta-language"


def correction_language_rows(hits: list[CorrectionLanguageHit]) -> list[dict]:
    """Error Report rows for correction meta-language left in the notes.

    One row per hit, in the sheet's own row shape. These describe the NOTES
    rather than the lecture, so they carry no source URL and no timestamp — the
    location is the line number, and it is already in the statement.

    Reporting rather than raising is the whole point: a run that dies here has
    thrown away the transcript, the notes and the QC pass over a phrase a
    reviewer could have fixed in ten seconds.
    """
    return [{
        "original_statement": f"Line {hit.line_no}: {hit.snippet}",
        "error_type": CORRECTION_LANGUAGE_ERROR_TYPE,
        "severity": "High",
        "error_concern": (
            f"The notes announce a correction instead of stating the fact "
            f"({hit.rule}). Corrections must be silent — the student's copy "
            f"should read as settled knowledge, with what changed recorded here."
        ),
        "correct_information_correction": (
            "Rewrite the line to state the correct fact plainly, with no "
            "reference to what the lecture said and no announcement that "
            "anything was changed."
        ),
        "source_urls": "",
    } for hit in hits]
