"""Deterministic spelling check — the typo classes a dictionary can prove.

The AI passes do report misspellings, and on graded documents they found them
every time so far. But "so far" is a probability, and a reviewer who finds one
missed typo stops trusting the whole report. A typo is one of the few defects a
machine can settle without a model, so this check runs on every English
document, costs nothing, and does not depend on the model's mood.

It is deliberately narrow. A general spell-checker over UPSC notes is useless:
British spellings ("labour", "recognised"), Indian-English words ("weightage",
"crore"), Latin ("per annum", "ex parte"), transliterated names ("Vagdevi",
"Paramara") and hundreds of proper nouns all fail a dictionary lookup, and a
report full of those would bury the real ones. Measured on two seeded trial
documents (eight planted typos) and their clean originals, the noise fell into
three shapes and the real typos into one:

  real typos  — a DROPPED letter ("remves", "exsting", "Perfomance") or a
                transposition. Every one of the eight.
  noise       — a substitution to a rarer word ("kartal" -> "hartal"), an extra
                letter that is really a plural ("timelines"), a distance-2
                guess ("deepfakes" -> "keepsakes"), or a capitalised proper noun
                one letter from a common word ("Satya" -> "satay").

So only dropped-letter and transposed-letter candidates count, British variants
and plurals are accepted as known, and a Capitalised token is flagged only when
the corrected word appears elsewhere in the same document (the notes spell it
right somewhere) or the token carries an English derivational suffix that a
name would not ("-isation", "-ance", "-ity"). On the trial material that is
exact: every planted typo found, nothing else reported, zero flags on the
clean originals.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

__all__ = ["Misspelling", "find_misspellings"]


@dataclass
class Misspelling:
    page: int
    word: str
    suggestion: str
    #: A dozen words around the first occurrence, so the row can quote the
    #: sentence a reviewer has to find — and so the same typo reported by the
    #: AI pass, which quotes the sentence too, collapses into one row.
    context: str


#: Words a dictionary rejects that are correct in this domain. Latin legal
#: terms and Sanskrit/Hindi terms are the two vocabularies that recur in every
#: UPSC document, so they are named rather than left to the heuristics.
_ALLOW = frozenset({
    # Indian administration and measures.
    "annum", "lakh", "lakhs", "crore", "crores", "weightage", "anganwadi",
    "anganwadis", "panchayat", "panchayats", "sabha", "sabhas", "taluka",
    "talukas", "tehsil", "tehsils", "mandal", "mandals", "zilla", "gram",
    "nyaya", "seva", "yojana", "yojanas", "abhiyan", "swaraj", "sarpanch",
    "sarpanches", "prelims", "mains", "upsc", "reskilling", "upskilling",
    "deskilling", "hartal", "kartal", "swadeshi", "satyagraha", "khadi",
    # Latin, legal and diplomatic.
    "parte", "facto", "jure", "motu", "facie", "fide", "vires", "prima",
    "obiter", "dicta", "dictum", "actus", "reus", "mens", "habeas", "corpus",
    "mandamus", "certiorari", "warranto", "juris", "judicata", "decidendi",
    "decisis", "alteram", "partem", "cogens", "erga", "omnes", "causa",
    "nolle", "prosequi", "damno", "opinio", "sine", "curiae", "amicus",
    "grata", "persona", "quo", "res", "ratio", "locus", "standi", "suo",
    # Sanskrit and Indian philosophy, as notes write them (often lowercase).
    "dharma", "artha", "kama", "moksha", "satya", "ahimsa", "jnana", "bhakti",
    "karma", "pinda", "daan", "purushartha", "purusharthas", "varna",
    "ashrama", "shastra", "shastras", "shastrarth", "veda", "vedas", "vedic",
    "upanishad", "upanishads", "puranas", "smriti", "shruti", "yajna",
    "murti", "stupa", "stupas", "vihara", "mandapa", "shikhara", "garbhagriha",
    # British spellings the frequency list happens to lack.
    "fibre", "fibres", "calibre", "meagre", "sombre", "sabre", "spectre",
    "sceptre", "lustre", "mitre", "ochre", "theatre", "litre", "litres",
    "metre", "metres", "centre", "centres", "manoeuvre", "programme",
    "programmes", "storey", "storeys", "kerb", "gaol", "plough", "moustache",
})

#: British -> American respellings. A word is "known" if the American form is.
_BRITISH = (
    ("isation", "ization"), ("ising", "izing"), ("ised", "ized"),
    ("ises", "izes"), ("ise", "ize"), ("yse", "yze"), ("ysed", "yzed"),
    ("ysing", "yzing"), ("our", "or"), ("ours", "ors"), ("ence", "ense"),
    ("ences", "enses"), ("mme", "m"),
    ("mmes", "ms"), ("ogue", "og"), ("lling", "ling"), ("lled", "led"),
    ("ller", "ler"), ("llor", "lor"),
)

#: The British -re words, as endings so compounds work ("kilometres",
#: "millimetre"). Deliberately a list rather than a "-tre → -ter" rule: that
#: rule accepted "ministre" as a British "minister", which is a transposition
#: typo, and French rather than British at that.
_BRITISH_RE_WORDS = (
    "metre", "metres", "centre", "centres", "litre", "litres", "theatre",
    "theatres", "fibre", "fibres", "calibre", "calibres", "spectre",
    "spectres", "sceptre", "lustre", "mitre", "ochre", "sombre", "sabre",
    "meagre", "manoeuvre", "manoeuvres", "macabre",
)

#: Function words. A token is not a plural just because stripping its "s"
#: leaves one: "froms" is a typo for "forms", not the plural of "from".
_FUNCTION_WORDS = frozenset({
    "from", "with", "that", "this", "than", "then", "when", "what", "which",
    "were", "been", "have", "into", "over", "upon", "also", "such", "more",
    "most", "some", "only", "very", "must", "shall", "will", "they", "them",
    "their", "there", "where", "while", "about", "after", "before", "under",
    "above", "below", "between", "would", "could", "should", "does", "done",
    "here", "each", "both", "same", "other", "these", "those", "being",
})

#: Derivational suffixes a proper noun almost never carries. A Capitalised
#: token ending in one of these is a word, not a name.
_WORD_SUFFIXES = (
    "isation", "ization", "ising", "izing", "ised", "ized", "ment", "ments",
    "ance", "ances", "ence", "ences", "ity", "ities", "ness", "tion", "tions",
    "sion", "sions", "ship", "ships", "able", "ible", "ical", "ically",
    "fully", "lessly", "ously", "ively",
)

#: A word of five or more letters, first letter any case, the rest lowercase.
#: ALL-CAPS tokens (acronyms), hyphenated and slashed compounds, handles,
#: e-mail-like runs and anything glued to a digit are left alone.
_TOKEN = re.compile(r"(?<![\w'’\-@/.])([A-Za-z][a-z]{4,})(?![\w'’\-@/])")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
#: Any run of letters, for reading a token's neighbours.
_ANY_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
#: Below this usage frequency a suggested correction is a word
#: nobody writes. Measured: the corrections of real typos score
#: 1.3e-07 ("parity") and up, while "satay", "pindar" and
#: "warrantor" — the suggestions that made noise — score 4e-08.
_SUGGESTION_FLOOR = 1e-07

_checker = None


def _dictionary():
    global _checker
    if _checker is None:
        from spellchecker import SpellChecker

        _checker = SpellChecker(language="en", distance=1)
    return _checker


#: A plural stem shorter than this, or rarer than _STEM_FLOOR, is not evidence
#: that the token is a plural. pyspellchecker's list contains "sta", "spec",
#: "ara" and "ida", so the old rule read "staes", "speces", "araes" and
#: "idaes" as plurals of them and never looked for the typo.
_MIN_STEM = 4
_STEM_FLOOR = 1e-06


def _known(sp, w: str) -> bool:
    """True when ``w`` is a word — dictionary, allow-list, British form, plural.

    Every rule here is anchored to the END of the token. The first version
    tested ``british in w`` anywhere in the word, so "fourm" was accepted as a
    British "form", "ministre" as "minister" and "trems" as "terms": the exact
    transposition typos this module exists to catch were being read as correct
    spellings before the search for a correction ever ran.
    """
    if w in sp or w in _ALLOW:
        return True
    if w.endswith(_BRITISH_RE_WORDS):
        return True
    for british, american in _BRITISH:
        if w.endswith(british) and (w[:-len(british)] + american) in sp:
            return True
    # Plurals and third-person forms the frequency list does not carry. The
    # stem has to look like a real word on its own, not a three-letter
    # fragment that happens to be in the list.
    def _stem_ok(stem: str) -> bool:
        return (len(stem) >= _MIN_STEM and stem in sp
                and stem not in _FUNCTION_WORDS
                and _frequency(sp, stem) >= _STEM_FLOOR)

    if w.endswith("ies") and _stem_ok(w[:-3] + "y"):
        return True
    if w.endswith("es") and _stem_ok(w[:-2]):
        return True
    return bool(w.endswith("s") and _stem_ok(w[:-1]))


def _candidates(sp, w: str, *, capitalised: bool = False) -> set[str]:
    """Words one DROPPED letter or one TRANSPOSITION away from ``w``.

    For a Capitalised token the dropped letter may not be the FIRST one: a
    name that becomes a word when a letter is put in front of it — "Ather"
    (an EV company) and "father" — is a name, and was reported as a typo.
    Fingers drop letters inside words; they do not drop capitals.
    """
    out: set[str] = set()
    for i in range(1 if capitalised else 0, len(w) + 1):
        for ch in "abcdefghijklmnopqrstuvwxyz":
            c = w[:i] + ch + w[i:]
            if _known(sp, c):
                out.add(c)
    for i in range(len(w) - 1):
        c = w[:i] + w[i + 1] + w[i] + w[i + 2:]
        if c != w and _known(sp, c):
            out.add(c)
    return out


def _frequency(sp, w: str) -> float:
    try:
        return float(sp.word_usage_frequency(w))
    except Exception:
        return 0.0


#: Characters that, touching a token, mean it is part of something larger: a
#: possessive or contraction ("boys'"), a hyphenated compound ("Co-operative"),
#: a handle, a path, a filename, or a word glued to a digit ("policy2020").
#: Asymmetric on the full stop, deliberately: a dot BEFORE a word makes it part
#: of a filename or a domain ("qc.report"), while a dot after it is how most
#: sentences end — counting that as glue silently dropped "refrms." and every
#: other typo that happened to fall at the end of a sentence.
_GLUE_BEFORE = "'’-@/."
_GLUE_AFTER = "'’-@/"


def _is_candidate(word: str, text: str, start: int, end: int) -> bool:
    """True when a token may be judged on its own spelling.

    The neighbour scan reads maximal runs of letters, apostrophes and hyphens,
    which is right for finding what sits BESIDE a word but wrong for deciding
    what to check: it let "boys'" through as a token, and the dictionary duly
    proposed "boy's".
    """
    if len(word) < 5 or not word.isalpha() or not word[1:].islower():
        return False
    before = text[start - 1:start] if start else ""
    after = text[end:end + 1]
    return not (before.isalnum() or (before and before in _GLUE_BEFORE)
                or after.isalnum() or (after and after in _GLUE_AFTER))


_DOMAIN_CACHE: dict[str, bool] = {}


def _is_domain_term(sp, w: str) -> bool:
    """True when ``w`` is an unfamiliar TERM rather than a misspelling.

    A term is a word the dictionary does not know and for which it has no
    plausible correction either — no candidate at all, or only one so rare
    that nobody writes it. "warranto", "dicta", "reus" and "artha" are terms;
    "teh" is not, because "the" is right there.
    """
    if len(w) < 3:
        return False
    cached = _DOMAIN_CACHE.get(w)
    if cached is not None:
        return cached
    verdict = False
    if not _known(sp, w):
        cands = _candidates(sp, w)
        verdict = not cands or max(
            _frequency(sp, c) for c in cands) < _SUGGESTION_FLOOR
    _DOMAIN_CACHE[w] = verdict
    return verdict


def _context(text: str, start: int, end: int, width: int = 70) -> str:
    lo, hi = max(0, start - width), min(len(text), end + width)
    snippet = text[lo:hi]
    # Trim to word boundaries so the quote does not start mid-word.
    if lo > 0:
        snippet = snippet.split(" ", 1)[-1]
    if hi < len(text):
        snippet = snippet.rsplit(" ", 1)[0]
    return " ".join(snippet.split())


def find_misspellings(pages: list[tuple[int, str]], *,
                      language_code: str = "en") -> list[Misspelling]:
    """Every provable typo in ``pages`` ([(page_no, text), ...]), first occurrence.

    Returns [] for a non-English document and — deliberately — when the
    dictionary cannot be loaded: a missing optional package must never turn
    into a crash of the free checks.
    """
    if (language_code or "en") != "en":
        return []
    try:
        sp = _dictionary()
    except Exception:
        return []

    # One pass to learn the document's own vocabulary: how often each token
    # occurs, the set of words it spells, and which tokens sit immediately
    # beside another word no dictionary knows.
    counts: Counter = Counter()
    first: dict[str, tuple[int, str, str]] = {}   # lower -> (page, token, context)
    beside_unknown: set[str] = set()
    for page_no, text in pages:
        run: list[tuple[str, int, int]] = []      # (word, start, end)
        for m in _ANY_WORD.finditer(text):
            run.append((m.group(0), m.start(), m.end()))
        for index, (word, start, end) in enumerate(run):
            w = word.lower()
            if _is_candidate(word, text, start, end):
                counts[w] += 1
                if w not in first:
                    around = text[max(0, start - 2):end + 2]
                    if not _DEVANAGARI.search(around):
                        first[w] = (page_no, word, _context(text, start, end))
            # A word standing immediately beside a TERM the dictionary does
            # not know is domain vocabulary, not a slip: "quo warranto",
            # "obiter dicta", "actus reus", "dharma, artha, kama". Only
            # punctuation and spaces may separate them, so "remves the
            # exsting" — an ordinary word between two typos — is unaffected.
            #
            # The neighbour has to be a term rather than another typo, or two
            # typos in a row would shield each other ("teh recieve"). What
            # separates them is whether the neighbour has a plausible
            # correction: "teh" is one letter from "the", while nobody means
            # "warrantor" when they write "warranto".
            for other in (index - 1, index + 1):
                if not 0 <= other < len(run):
                    continue
                gap = (text[run[other][2]:start] if other < index
                       else text[end:run[other][1]])
                if any(ch.isalpha() for ch in gap):
                    continue
                if _is_domain_term(sp, run[other][0].lower()):
                    beside_unknown.add(w)
                    break
    vocabulary = set(counts)

    out: list[Misspelling] = []
    for w, (page_no, tok, context) in first.items():
        if _known(sp, w):
            continue
        if counts[w] >= 3:
            # Used consistently three or more times: a domain term or a name
            # the dictionary lacks, not a slip of the finger.
            continue
        capitalised = tok[0].isupper()
        if w in beside_unknown:
            continue
        cands = _candidates(sp, w, capitalised=capitalised)
        if not cands:
            continue
        if capitalised:
            in_document = any(c in vocabulary for c in cands)
            worded = w.endswith(_WORD_SUFFIXES)
            if not (in_document or worded):
                continue   # a name one letter from a word, e.g. "Satya"
            # Prefer the form the document itself uses.
            preferred = [c for c in cands if c in vocabulary]
            if preferred:
                cands = set(preferred)
        best = max(cands, key=lambda c: (c in vocabulary, _frequency(sp, c)))
        # A correction nobody writes is not a correction. "satya" is one
        # letter from "satay" and "pinda" from "pindar", and neither is what
        # the notes meant. The floor is waived when the document itself uses
        # the corrected word, and when the token carries an English
        # derivational suffix (a British form such as "formalisation" can be
        # absent from the frequency list altogether).
        if (best not in vocabulary and not w.endswith(_WORD_SUFFIXES)
                and _frequency(sp, best) < _SUGGESTION_FLOOR):
            continue
        if capitalised:
            best = best[0].upper() + best[1:]
        out.append(Misspelling(page=page_no, word=tok, suggestion=best,
                               context=context))
    out.sort(key=lambda m: (m.page, m.word.lower()))
    return out
