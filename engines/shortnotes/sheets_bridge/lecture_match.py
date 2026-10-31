"""Pick the class-notes PDF for one lecture inside a Drive folder.

A chapter folder holds files for several chapters *and* subjects, named

    <Chapter name> <NN>  Class Notes.pdf        e.g. "Circles 06  Class Notes.pdf"

so the lecture number alone is not unique: three files can carry "06". The
row's Chapter (column G, often "English\\nहिन्दी") is what disambiguates them.

The file whose name best matches the row's chapter - and carries its lecture
number when the folder numbers its files - is used, even when weaker names
also resemble the chapter. Copies of one lecture (a duplicate upload, "(1)",
"copy", "v2", identical content, the same title before the number) never fail:
one is used and the log says so. Our own and hand-made "Concise Notes" /
"Short Notes" files are never a source.

What still fails is only what cannot be decided without guessing a different
lecture - the lecture or chapter is not in the folder, or two different
lectures fit equally ("Ionic Equilibrium 01" / "Chemical Equilibrium 01" for
chapter "Equilibrium") - because a wrong pick would silently produce notes for
the wrong lecture and mark the row done.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .errors import BridgeError

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Tokens that introduce a *lecture* number. "class"/"कक्षा" are excluded on
# purpose (they denote the grade: "Class 12"), as are part/भाग ("L05 Part 2").
_LECTURE_TOKEN = r"(?:lecture|lect|lec|व्याख्यान|लेक्चर|l)"
_TOKEN_NUMBER_RE = re.compile(
    rf"(?<![a-z0-9]){_LECTURE_TOKEN}[\s\-_.#:]*0*(\d{{1,3}})(?!\d)",
    re.IGNORECASE,
)
# A number only counts when it stands on its own: "Circles 06", "SO 1", "7. Cell".
# Digits glued to Latin letters are part of an ID or a word ("04d30aa5-...3bd",
# "Notes12", "11th"), not a lecture number.
_ANY_NUMBER_RE = re.compile(r"(?<![0-9A-Za-z])0*(\d{1,3})(?![0-9A-Za-z])")
_FIRST_INT_RE = re.compile(r"\d{1,3}")
_NOTES_HINT_RE = re.compile(r"class\s*notes|notes|नोट्स|नोट", re.IGNORECASE)
# Our outputs and the team's own hand-made ones ("Chemical Bonding-03_Concise_Notes.pdf"):
# never a lecture source, whatever separates the two words.
_GENERATED_RE = re.compile(r"short[\s_\-]*notes|concise[\s_\-]*notes", re.IGNORECASE)
# Numbers in a file name that are not lecture numbers: a download copy "(1)",
# a batch version "NEET 3.0", a grade "Class 7th" / "Class 12".
_NOT_LECTURE_NUMBER_RE = re.compile(
    r"\(\s*\d+\s*\)|\d+\.\d+|class\s*\d{1,2}(?:st|nd|rd|th)?", re.IGNORECASE)
_NOTES_SUFFIX_RE = re.compile(r"[\s\-_]*class\s*notes\s*$|[\s\-_]*नोट्स\s*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\wऀ-ॿ]+")
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def _ascii_digits(text: str) -> str:
    return (text or "").translate(_DEVANAGARI_DIGITS)


def parse_lecture_number(text: object) -> int | None:
    """Column H -> int: 'L01' -> 1, 'Lecture 12' -> 12, '7' -> 7, '' -> None."""
    if not isinstance(text, str):
        return None
    match = _FIRST_INT_RE.search(_ascii_digits(text))
    return int(match.group(0)) if match else None


def lecture_numbers(name: str) -> set[int]:
    """Numbers introduced by a lecture token: 'L05 ...' -> {5}."""
    return {int(m.group(1)) for m in _TOKEN_NUMBER_RE.finditer(_ascii_digits(name))}


def any_numbers(name: str) -> set[int]:
    """Every number that could be a lecture number: '(1)', '3.0' and 'Class 12' excluded."""
    text = _NOT_LECTURE_NUMBER_RE.sub(" ", _ascii_digits(name))
    return {int(m.group(1)) for m in _ANY_NUMBER_RE.finditer(text)}


def is_generated_output(name: str) -> bool:
    return bool(_GENERATED_RE.search(name or ""))


def is_pdf_candidate(file) -> bool:
    """A source lecture PDF: PDF by type or extension, not Google-native, not
    one of our own outputs."""
    name = str(getattr(file, "name", "") or "")
    mime = str(getattr(file, "mime_type", "") or "")
    if mime.startswith("application/vnd.google-apps."):
        return False
    if is_generated_output(name):
        return False
    return mime == "application/pdf" or name.lower().endswith(".pdf")


# --------------------------------------------------------------------------- #
# Chapter matching
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    """Casefolded, punctuation- and digit-free comparison form."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _ascii_digits(text)
    text = re.sub(r"\d+", " ", text)
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.casefold().split())


def chapter_variants(chapter: object) -> list[str]:
    """Column G may hold several spellings: 'English name\\nहिन्दी नाम'."""
    if not isinstance(chapter, str):
        return []
    parts = re.split(r"[\n\r|/;]+", chapter)
    seen: list[str] = []
    for part in parts:
        normalized = _normalize(part)
        if normalized and normalized not in seen:
            seen.append(normalized)
    # A long title the cell wrapped onto two lines ("Earth as a System:
    # Energy, Matter,\nand Life") is also one spelling, not only its two
    # halves ("and life" alone matched every "... Life" lecture). Lines in two
    # scripts are two spellings (English / Hindi), never joined.
    lines = [line for line in re.split(r"[\n\r]+", chapter) if line.strip()]
    if len(lines) > 1 and len({bool(_DEVANAGARI_RE.search(line)) for line in lines}) == 1:
        whole = _normalize(" ".join(lines))
        if whole and whole not in seen:
            seen.append(whole)
    return seen


def file_stem(name: str) -> str:
    """Comparable chapter part of a file name: drop .pdf, 'Class Notes', digits."""
    text = str(name or "")
    if text.lower().endswith(".pdf"):
        text = text[:-4]
    text = _NOTES_SUFFIX_RE.sub("", text)
    return _normalize(text)


# Words that say nothing about WHICH chapter a file is. Two unrelated titles
# sharing "and", "the", "Centuries" leftovers such as "th" (from "11th" once
# digits are dropped) used to reach the match bar together.
_FILLER_WORDS = frozenset({
    "a", "an", "the", "and", "of", "to", "in", "on", "for", "with", "by", "from", "at", "as", "or", "its",
    "th", "st", "nd", "rd",
    "class", "notes", "note", "lecture", "lec", "chapter", "ch", "part",
    "sir", "maam", "mam", "ma", "am",
    "और", "का", "के", "की", "तथा", "एवं", "में", "व",
})
# The share of the chapter's own words a file name must contain to count, before
# the containment and exact-name rules; kept below containment (0.9).
_OVERLAP_WEIGHT = 0.85


def _content_words(text: str) -> set[str]:
    return {word for word in text.split() if word not in _FILLER_WORDS and len(word) > 1}


def _same_word(left: str, right: str) -> bool:
    """Equal, or one typo apart in a long word ("neighbors" / "neighbours")."""
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    from difflib import SequenceMatcher

    return SequenceMatcher(None, left, right).ratio() >= 0.85


def _token_overlap(stem: str, variant: str) -> float:
    """How much of the chapter's own wording the file name carries (0..0.85)."""
    chapter_words = _content_words(variant)
    name_words = _content_words(stem)
    if not chapter_words or not name_words:
        return 0.0
    covered = sum(1 for word in chapter_words if any(_same_word(word, other) for other in name_words))
    return _OVERLAP_WEIGHT * covered / len(chapter_words)


def _initials_match(stem: str, variant: str) -> bool:
    """``'so'`` names ``'structural organization in animals'``.

    Production folders abbreviate: the files inside "Structural Organization
    in Animals" are "SO 01" ... "SO 10". A stem that is one short alphabetic
    token equal to the initials of the chapter's leading words is that chapter.
    Kept narrow (2-4 letters, leading words only) so it cannot fire on prose.
    """
    if not stem or " " in stem or not stem.isalpha() or not 2 <= len(stem) <= 4:
        return False
    words = variant.split()
    if len(words) < len(stem):
        return False
    return "".join(word[0] for word in words[: len(stem)]) == stem


def chapter_score(name: str, variants: list[str]) -> float:
    """0.0 = unrelated, 1.0 = the file names exactly this chapter."""
    stem = file_stem(name)
    if not stem or not variants:
        return 0.0
    best = 0.0
    for variant in variants:
        if not variant:
            continue
        if stem == variant:
            return 1.0
        # Whole words only: a Chapter cell holding just "d" is not inside
        # "tales by dots and lines", while "motion" is inside "laws of motion".
        if f" {variant} " in f" {stem} " or f" {stem} " in f" {variant} ":
            best = max(best, 0.9)
        elif _initials_match(stem, variant):
            best = max(best, 0.7)
        else:
            best = max(best, _token_overlap(stem, variant))
    return best


CHAPTER_MATCH_THRESHOLD = 0.6


@dataclass(frozen=True)
class Match:
    file: object
    how: str      # chapter_number | lecture_token | loose_number | single_file
    note: str = ""  # set when one of several copies of the lecture was used


# Marks that make a copy of a lecture file, not a different lecture:
# "(1)", "copy", "copy of", "old", "new", "final", "updated", "revised",
# "latest", "v2", "version 3". Anything else in the name ("(Hindi)",
# "Part 2") still tells files apart.
_COPY_MARK_RE = re.compile(
    r"\(\s*\d+\s*\)"
    r"|\b(?:copy\s+of|copy|old|new|final|updated|revised|latest)\b"
    r"|\bv(?:ersion)?\s*\d+\b",
    re.IGNORECASE,
)
_STALE_MARK_RE = re.compile(r"\(\s*\d+\s*\)|\bcopy\b|\bold\b", re.IGNORECASE)
_FRESH_MARK_RE = re.compile(r"\b(?:final|updated|revised|latest|new)\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"\bv(?:ersion)?\s*(\d+)\b", re.IGNORECASE)


def lecture_identity(name: object) -> str:
    """A file name with copy/version marks removed, digits kept.

    "Circles 06 Class Notes (1).pdf" and "Circles 06 Class Notes.pdf" give the
    same identity; "Circles 06 Part 2.pdf" and "Circles 06 Part 1.pdf" do not.
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    if text.lower().endswith(".pdf"):
        text = text[:-4]
    text = _ascii_digits(text)
    text = _COPY_MARK_RE.sub(" ", text)
    text = re.sub(r"\d+", lambda m: str(int(m.group())), text)   # 06 == 6
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.casefold().split())


def _copies_of_one_lecture(files: list) -> bool:
    """True when every file is a copy of the same lecture: identical content
    (same checksum) or the same name once copy/version marks are ignored."""
    if len(files) < 2:
        return False
    parent = list(range(len(files)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    identities = [lecture_identity(getattr(f, "name", "")) for f in files]
    checksums = [str(getattr(f, "md5", "") or "") for f in files]
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            same_content = checksums[i] and checksums[i] == checksums[j]
            same_name = identities[i] and identities[i] == identities[j]
            if same_content or same_name:
                parent[root(i)] = root(j)
    return len({root(i) for i in range(len(files))}) == 1


def _modified_epoch(file) -> float:
    from datetime import datetime

    text = str(getattr(file, "modified_time", "") or "")
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _pick_copy(files: list, variants: list[str]) -> object:
    """The copy to use: best chapter match, not marked old/copy, highest
    version, marked final/updated, most recently changed, largest; then name
    and id so the choice is stable from run to run."""
    def key(file):
        name = str(getattr(file, "name", "") or "")
        versions = [int(v) for v in _VERSION_RE.findall(name)]
        return (
            -(chapter_score(name, variants) if variants else 0.0),
            1 if _STALE_MARK_RE.search(name) else 0,
            -(max(versions) if versions else 0),
            0 if _FRESH_MARK_RE.search(name) else 1,
            -_modified_epoch(file),
            -int(getattr(file, "size", 0) or 0),
            name,
            str(getattr(file, "id", "") or ""),
        )

    return sorted(files, key=key)[0]


def _one_of_copies(files: list, how: str, variants: list[str]) -> "Match | None":
    if not _copies_of_one_lecture(files):
        return None
    identical = len({str(getattr(f, "md5", "") or "") for f in files}) == 1 and all(getattr(f, "md5", "") for f in files)
    kind = "identical copies" if identical else "copies"
    return Match(_pick_copy(files, variants), how, note=f"{len(files)} {kind} of this lecture in the folder - using one")


def _names(files: list, limit: int = 3) -> str:
    shown = ", ".join(repr(str(getattr(f, "name", ""))) for f in files[:limit])
    return shown + (f" and {len(files) - limit} more" if len(files) > limit else "")


def _prefer_notes(files: list) -> list:
    """Tie-break: prefer names that look like class notes over DPP/PPT etc."""
    noted = [f for f in files if _NOTES_HINT_RE.search(str(f.name))]
    return noted if 0 < len(noted) < len(files) else files


def _by_number(candidates: list, lecture_number: int, chapter: str = "") -> tuple[list, str]:
    """Files carrying that lecture number, and how they were matched."""
    tokened = [f for f in candidates if lecture_number in lecture_numbers(str(f.name))]
    if tokened:
        return tokened, "lecture_token"
    # Loose numbering only when the folder does not use lecture tokens at all,
    # otherwise a date/chapter number could masquerade as a lecture number.
    # Numbers that are part of the chapter title ("11th and 12th Centuries")
    # are not lecture numbers either.
    if not any(lecture_numbers(str(f.name)) for f in candidates):
        loose = [f for f in candidates if lecture_number in _file_lecture_numbers(str(f.name), chapter)]
        if loose:
            return loose, "loose_number"
    return [], ""


def folder_is_chapter(folder_name: str, chapter: str) -> bool:
    """True when the folder itself is named after the row's chapter.

    Then every file inside belongs to that chapter and the file names need not
    repeat it - they may be as terse as "SO 07" - so the chapter must not be
    demanded of them again.
    """
    variants = chapter_variants(chapter)
    stem = file_stem(folder_name)
    if not variants or not stem:
        return False
    # Deliberately stricter than chapter_score: a subject folder ("Chemistry")
    # is a *word inside* "Chemistry in Everyday Life", not that chapter, and
    # treating it as the chapter folder would hand back another chapter's
    # lecture. The folder name must cover the chapter title (or be its
    # initials), so a spelling variant still matches and a subject does not.
    words = set(stem.split())
    for variant in variants:
        if not variant:
            continue
        if stem == variant or _initials_match(stem, variant):
            return True
        variant_words = set(variant.split())
        if variant_words and len(words & variant_words) / max(len(words), len(variant_words)) >= CHAPTER_MATCH_THRESHOLD:
            return True
    return False


# A file that scores this much less than the best chapter match is not a rival:
# "Turning Tides ..." at 1.00 beats "Empires and Kingdoms ..." at 0.60.
MATCH_MARGIN = 0.15


def _file_lecture_numbers(name: str, chapter: str) -> set[int]:
    """Lecture numbers in a file name, ignoring numbers that belong to the
    chapter title itself ("Empires and Kingdoms: 6th to 10th Centuries")."""
    title = any_numbers(_ascii_digits(str(chapter or "")))
    return (lecture_numbers(name) | any_numbers(name)) - title


def _lecture_title(name: str, chapter: str) -> str:
    """The words of a file name before its lecture number, without filler:
    'Ionic Equilibrium 01 - Class Notes.pdf' -> 'ionic equilibrium'. A name
    with no lecture number gives all its words."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    if text.lower().endswith(".pdf"):
        text = text[:-4]
    text = _NOT_LECTURE_NUMBER_RE.sub(" ", _COPY_MARK_RE.sub(" ", _ascii_digits(text)))
    title_numbers = any_numbers(_ascii_digits(str(chapter or "")))
    for found in _ANY_NUMBER_RE.finditer(text):
        if int(found.group(1)) not in title_numbers:
            text = text[:found.start()]
            break
    words = _PUNCT_RE.sub(" ", re.sub(r"\d+", " ", text)).casefold().split()
    return " ".join(word for word in words if word not in _FILLER_WORDS)


def _chapter_leaders(files: list, variants: list[str]) -> tuple[list, list]:
    """(files level with the best chapter match, every (score, file) ranked).

    Leaders are the files at or over the match bar and within MATCH_MARGIN of
    the best score; empty when nothing reaches the bar.
    """
    ranked = sorted(((chapter_score(str(f.name), variants), f) for f in files),
                    key=lambda pair: -pair[0])
    if not ranked or ranked[0][0] < CHAPTER_MATCH_THRESHOLD:
        return [], ranked
    top = ranked[0][0]
    leaders = [f for score, f in ranked if score >= CHAPTER_MATCH_THRESHOLD and top - score < MATCH_MARGIN]
    return leaders, ranked


def _closest(ranked: list) -> str:
    if not ranked:
        return ""
    score, file = ranked[0]
    return f"; closest: {str(getattr(file, 'name', ''))!r} ({score:.0%})"


def _decide(leaders: list, ranked: list, how: str, variants: list[str], *,
            chapter: str, lecture_number: int | None) -> "Match | None":
    """One file out of the chapter leaders, or None when they are different lectures.

    A clear winner is used. Leaders that are copies of one lecture, or all the
    same chapter and lecture (same lecture number, or none in their names), are
    equally right: the newest is used and the log says so.
    """
    beaten = sum(1 for score, _file in ranked if score >= CHAPTER_MATCH_THRESHOLD) - len(leaders)
    all_leaders = list(leaders)
    if len(leaders) == 1:
        note = f"closest chapter name, ahead of {beaten} weaker match(es)" if beaten else ""
        return Match(leaders[0], how, note=note)
    leaders = _prefer_notes(leaders)
    if len(leaders) == 1:
        return Match(leaders[0], how, note="class notes preferred over other files of this chapter")
    copies = _one_of_copies(leaders, how, variants)
    if copies is not None:
        return copies
    numbers = {frozenset(_file_lecture_numbers(str(f.name), chapter)) for f in leaders}
    titles = {_lecture_title(str(f.name), chapter) for f in leaders}
    # Versions share the words before the lecture number ("Solutions 15 Class
    # Notes" / "Solutions 15 Class Notes ... (1)"); "Ionic Equilibrium 01" and
    # "Chemical Equilibrium 01" do not, and are two different lectures.
    same_lecture = len(titles) == 1 and len(numbers) == 1 and (
        lecture_number is None or not next(iter(numbers)) or lecture_number in next(iter(numbers)))
    if same_lecture:
        # Versions of one lecture: an extra word such as "Hindi" lowers the name
        # score without making the file less right, so recency decides, not score.
        return Match(_pick_copy(leaders, []), how,
                     note=f"{len(leaders)} files of this chapter and lecture - using the newest")
    # Different lectures, but exactly one is named as the chapter itself:
    # "Circles" is CIRCLES.pdf, not AREAS RELATED TO CIRCLES.pdf, which only
    # contains the word. Versions of one lecture were settled above by recency.
    exact = [f for score, f in ranked if score >= 1.0 and f in all_leaders]
    if len(exact) == 1:
        return Match(exact[0], how, note="the file named exactly as the chapter")
    return None


def choose_lecture_pdf(
    files: Iterable,
    lecture_number: int | None,
    *,
    chapter: str = "",
    folder_name: str = "",
) -> Match:
    """Return the PDF for this row or raise a ``BridgeError``.

    The file whose name best matches the row's chapter (and carries its
    lecture number, when the folder numbers its files) is used. Copies of one
    lecture never fail. What still fails is only what cannot be decided
    without guessing another lecture:

    ``no_pdf_in_folder``       - the folder holds no source PDF
    ``chapter_not_found``      - no file in the folder is this chapter
    ``lecture_not_found``      - this chapter is there, but not this lecture
    ``lecture_number_missing`` - several different lectures of the chapter and
                                 column H has no number to pick one
    ``lecture_ambiguous``      - no chapter to go by and several different
                                 files carry the number
    """
    candidates = [f for f in files if is_pdf_candidate(f)]
    if not candidates:
        raise BridgeError("no_pdf_in_folder")

    variants = chapter_variants(chapter)
    if variants and folder_is_chapter(folder_name, chapter):
        # A chapter-named folder: use the chapter only to break a tie, never
        # to reject a file that simply does not repeat the folder's name.
        numbered_here, _how = _by_number(candidates, lecture_number, chapter) if lecture_number is not None else ([], "")
        if numbered_here and not any(
            chapter_score(str(f.name), variants) >= CHAPTER_MATCH_THRESHOLD for f in numbered_here
        ):
            variants = []

    if lecture_number is None:
        if len(candidates) == 1:
            return Match(candidates[0], "single_file")
        copies = _one_of_copies(candidates, "single_file", variants)
        if copies is not None:
            return copies
        if variants:
            leaders, ranked = _chapter_leaders(candidates, variants)
            if not leaders:
                raise BridgeError("chapter_not_found", f"no file in the folder matches this chapter{_closest(ranked)}")
            chosen = _decide(leaders, ranked, "chapter_number", variants, chapter=chapter, lecture_number=None)
            if chosen is not None:
                return chosen
            raise BridgeError(
                "lecture_number_missing",
                f"{len(leaders)} different lectures of this chapter: {_names(leaders)} - put the lecture number in column H",
            )
        raise BridgeError("lecture_number_missing", f"{len(candidates)} PDFs in folder and no chapter to choose by")

    numbered, how = _by_number(candidates, lecture_number, chapter)

    if variants:
        # The chapter is the disambiguator when a folder mixes chapters.
        if numbered:
            leaders, ranked = _chapter_leaders(numbered, variants)
            if leaders:
                chosen = _decide(leaders, ranked, "chapter_number", variants,
                                 chapter=chapter, lecture_number=lecture_number)
                if chosen is not None:
                    return chosen
                raise BridgeError(
                    "lecture_ambiguous",
                    f"{len(leaders)} different files match chapter and lecture {lecture_number}: {_names(leaders)}",
                )
        # No numbered file is this chapter (or none carries the number). A file
        # named after the chapter with NO lecture number in its name is that
        # chapter's only lecture file ("The Story of Indian Farming.pdf"); a file
        # carrying a different number is another lecture and is never used.
        unnumbered = [f for f in candidates if not _file_lecture_numbers(str(f.name), chapter)]
        leaders, ranked = _chapter_leaders(unnumbered, variants)
        if leaders:
            chosen = _decide(leaders, ranked, "chapter_number", variants,
                             chapter=chapter, lecture_number=lecture_number)
            if chosen is not None:
                note = "matched by chapter name; the file name has no lecture number"
                return Match(chosen.file, chosen.how, note="; ".join(filter(None, (chosen.note, note))))
        chapter_files, all_ranked = _chapter_leaders(candidates, variants)
        if chapter_files:
            raise BridgeError(
                "lecture_not_found",
                f"this chapter is in the folder ({_names(chapter_files)}) but not lecture {lecture_number}",
            )
        if numbered:
            # Number matched but no file belongs to this chapter: refuse rather
            # than hand back another chapter's lecture with the same number.
            raise BridgeError(
                "chapter_not_found",
                f"{len(numbered)} file(s) carry lecture {lecture_number}, none match the chapter{_closest(all_ranked)}",
            )
        raise BridgeError("chapter_not_found", f"no file in the folder matches this chapter{_closest(all_ranked)}")

    if not numbered:
        if len(candidates) == 1 and not any_numbers(str(candidates[0].name)):
            return Match(candidates[0], "single_file")
        raise BridgeError("lecture_not_found", f"no PDF for lecture {lecture_number} among {len(candidates)}")

    numbered = _prefer_notes(numbered)
    if len(numbered) == 1:
        return Match(numbered[0], how)
    copies = _one_of_copies(numbered, how, variants)
    if copies is not None:
        return copies
    raise BridgeError(
        "lecture_ambiguous",
        f"{len(numbered)} different files carry lecture {lecture_number}: {_names(numbered)}",
    )
