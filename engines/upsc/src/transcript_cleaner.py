"""Repair a YouTube auto-caption transcript using the slides as ground truth.

A Hindi-medium UPSC class is code-switched: the teacher says "RTI", "Citizens'
Charter", "Article 19(1)(a)" in English inside Hindi sentences. YouTube's Hindi
recogniser transliterates those into Devanagari — "आरटीआई", "सिटीजन चार्टर" —
and sometimes mis-hears them entirely ("प्रोबेबिलिटी" where the topic is plainly
"probity").

Feed that to the model and the corrupted spelling reaches the student's notes,
because the model has no way to know which of two spellings the slide used.

The slides do know. This module builds a glossary from them — every acronym,
Act, Article, scheme, body and capitalised term the deck contains — and puts the
English form back wherever the transcript transliterated it. What it cannot map
confidently it leaves alone: a wrong guess here would be worse than a caption
artefact, because it would look authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

#: Devanagari, so a transliterated run can be spotted without a language model.
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

#: Terms worth protecting, harvested from slide text.
_ACRONYM_RE = re.compile(r"\b([A-Z]{2,8})(?:\b|(?=[^A-Za-z]))")
_ARTICLE_RE = re.compile(r"\bArticles?\s+\d+[A-Za-z]?(?:\s*\(\s*\d+\s*\)\s*(?:\([a-z]\))?)?", re.I)
_ACT_RE = re.compile(r"\b((?:[A-Z][\w'’\-]*\s+){0,5}Act,?\s*\d{4})")
_TITLECASE_RE = re.compile(r"\b((?:[A-Z][a-z’'\-]{2,}[ \t]+){1,4}[A-Z][a-z’'\-]{2,})\b")
#: A single capitalised word is a term too — "Probity", "Charter", "Governance".
#: Without these the transliteration table has nothing to map onto, because a
#: replacement is only made when the slides contain the English form.
_SINGLE_TERM_RE = re.compile(r"\b([A-Z][a-z’'\-]{4,})\b")
#: Capitalised words that are just sentence starts, not vocabulary.
_SINGLE_TERM_STOPWORDS = frozenset({
    "About", "After", "Again", "Against", "Among", "Because", "Before", "Being",
    "Below", "Between", "Business", "Different", "During", "Every", "First",
    "Following", "Further", "Getting", "具", "Having", "Important", "Latest",
    "Level", "Major", "Making", "Other", "Please", "Point", "Points", "Second",
    "Should", "Since", "Slide", "Their", "There", "These", "Third", "Those",
    "Through", "Today", "Under", "Until", "Using", "Various", "Where", "Which",
    "While", "Within", "Without", "Would", "Class", "Notes", "Lecture", "Topic",
    "Topics", "Example", "Examples", "Introduction", "Conclusion", "Summary",
})

#: Filler a class recording is full of, in both languages.
_FILLER_PATTERNS = (
    r"\b(?:so|ok|okay|right|yes|yeah|hmm+|umm+|uh+|aa+)\b",
    r"\b(?:achha|acha|theek hai|thik hai|samajh (?:me )?aaya|dekho|dekhiye|chalo)\b",
    r"\b(?:haan|han|ji|arre|arey|bilkul|matlab)\b",
    r"(?:ठीक है|समझ (?:में )?आया|देखिए|देखो|अच्छा|हाँ|जी|अरे|बिल्कुल|मतलब)",
)
_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS), re.I)

#: Housekeeping that is never lecture content.
_CHATTER_PATTERNS = (
    r"\b(?:like,? share (?:and|&) subscribe|subscribe (?:to )?(?:the |our )?channel)\b",
    r"\b(?:hit the bell|bell icon|comment (?:section|below)|link in (?:the )?description)\b",
    r"\b(?:can you (?:all )?hear me|is my (?:audio|voice) (?:clear|audible)|audio (?:clear|theek))\b",
    r"\b(?:good (?:morning|evening|afternoon) (?:everyone|students|all))\b",
    r"\b(?:batch|course|coupon|discount|offer|scan the qr|qr code)\b.*\b(?:code|off|link|apply)\b",
    r"(?:सब्सक्राइब|चैनल को सब्सक्राइब|कमेंट (?:बॉक्स|सेक्शन)|लिंक डिस्क्रिप्शन)",
)
_CHATTER_RE = re.compile("|".join(_CHATTER_PATTERNS), re.I)

#: A repeated phrase is only filler once it recurs this often back-to-back.
_MAX_IMMEDIATE_REPEATS = 2

#: Common English->Devanagari transliterations of UPSC vocabulary. Only used to
#: *find* a corrupted span; the replacement always comes from the slides, so a
#: term the deck never mentions is never rewritten.
_TRANSLITERATIONS: dict[str, tuple[str, ...]] = {
    "RTI": ("आरटीआई", "आर टी आई", "आरटीआइ"),
    "PIL": ("पीआईएल", "पी आई एल"),
    "CIC": ("सीआईसी", "सी आई सी"),
    "NGO": ("एनजीओ", "एन जी ओ"),
    "IAS": ("आईएएस", "आई ए एस"),
    "UPSC": ("यूपीएससी", "यू पी एस सी"),
    "CAG": ("सीएजी", "सी ए जी"),
    "CVC": ("सीवीसी", "सी वी सी"),
    "GDP": ("जीडीपी", "जी डी पी"),
    "probity": ("प्रोबिटी", "प्रोबेबिलिटी", "प्रोबिटि"),
    "governance": ("गवर्नेंस", "गवर्नेन्स", "गवर्नंस"),
    "accountability": ("अकाउंटेबिलिटी", "अकाउन्टेबिलिटी"),
    "transparency": ("ट्रांसपेरेंसी", "पारदर्शिता"),
    "integrity": ("इंटीग्रिटी", "इन्टेग्रिटी"),
    "charter": ("चार्टर",),
    "citizen": ("सिटीजन", "सिटिजन"),
    "ethics": ("एथिक्स",),
    "commission": ("कमीशन", "कमिशन"),
    "amendment": ("अमेंडमेंट", "संशोधन"),
    "constitution": ("कॉन्स्टिट्यूशन",),
}


@dataclass
class TranscriptCleanResult:
    """The cleaned transcript plus everything the run log wants to record."""

    text: str
    glossary: list[str] = field(default_factory=list)
    repairs: list[tuple[str, str]] = field(default_factory=list)   # (found, replaced with)
    removed_filler_lines: int = 0
    removed_chatter_lines: int = 0
    original_words: int = 0
    cleaned_words: int = 0
    devanagari_ratio: float = 0.0

    @property
    def repair_count(self) -> int:
        return len(self.repairs)

    def summary(self) -> dict:
        return {
            "glossary_terms": len(self.glossary),
            "term_repairs": self.repair_count,
            "removed_filler_lines": self.removed_filler_lines,
            "removed_chatter_lines": self.removed_chatter_lines,
            "original_words": self.original_words,
            "cleaned_words": self.cleaned_words,
            "devanagari_ratio": round(self.devanagari_ratio, 3),
        }


def _normalise(text: str) -> str:
    """NFC, so a Devanagari match is not defeated by combining-mark order."""
    return unicodedata.normalize("NFC", text or "")


def build_glossary(slide_texts: list[str]) -> list[str]:
    """Terms the slides establish, longest first.

    Longest-first matters at substitution time: "Right to Information Act, 2005"
    must be tried before "Right to Information", or the year is orphaned.
    """
    # Line by line, never on the joined text: a multi-word pattern run across a
    # line break invents terms like "Governance Right" out of two headings.
    lines = [ln for text in slide_texts if text for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    found: set[str] = set()
    for line in lines:
        for pattern in (_ARTICLE_RE, _ACT_RE, _TITLECASE_RE):
            for match in pattern.finditer(line):
                term = " ".join(match.group(0).split()).strip(" .,;:")
                if len(term) > 3:
                    found.add(term)
        for match in _SINGLE_TERM_RE.finditer(line):
            word = match.group(1)
            if word not in _SINGLE_TERM_STOPWORDS:
                found.add(word)
        for match in _ACRONYM_RE.finditer(line):
            acronym = match.group(1)
            # "THE" and "AND" are shouting, not acronyms.
            if acronym not in {"THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "ARE", "NOT"}:
                found.add(acronym)
    return sorted(found, key=lambda t: (-len(t), t.lower()))


def _glossary_lookup(glossary: list[str]) -> dict[str, str]:
    """lowercase term -> the slide's own spelling."""
    return {term.lower(): term for term in glossary}


def restore_terms(
    transcript: str,
    glossary: list[str],
) -> tuple[str, list[tuple[str, str]]]:
    """Put slide spellings back where the captions transliterated them.

    A term is replaced only when the slides actually contain it. That is the
    whole safety property: the transliteration table says *where to look*, the
    slides say *what to write*, and a term the deck never mentions is left as
    the captions had it.
    """
    if not transcript or not glossary:
        return transcript, []
    lookup = _glossary_lookup(glossary)
    text = _normalise(transcript)
    repairs: list[tuple[str, str]] = []

    for english, variants in _TRANSLITERATIONS.items():
        replacement = lookup.get(english.lower())
        if replacement is None:
            continue
        for variant in variants:
            variant_n = _normalise(variant)
            if variant_n not in text:
                continue
            count = text.count(variant_n)
            text = text.replace(variant_n, replacement)
            repairs.append((variant, replacement))
            if count > 1:
                # One entry per distinct term, not per occurrence.
                pass

    # Case repair for English that survived but lost its capitals: the slides
    # say "RTI", the captions often say "rti".
    for term in glossary:
        if not term.isupper() or len(term) < 2:
            continue
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(term.lower())}(?![A-Za-z])")
        if pattern.search(text):
            text = pattern.sub(term, text)
            repairs.append((term.lower(), term))
    return text, repairs


def _collapse_immediate_repeats(lines: list[str]) -> list[str]:
    """Drop a line that just repeated itself, which captions do constantly."""
    out: list[str] = []
    for line in lines:
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if not key:
            out.append(line)
            continue
        recent = [re.sub(r"\W+", " ", p.lower()).strip() for p in out[-_MAX_IMMEDIATE_REPEATS:]]
        if key in recent:
            continue
        out.append(line)
    return out


def devanagari_ratio(text: str) -> float:
    """Share of letters that are Devanagari — how Hindi the transcript is."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _DEVANAGARI_RE.match(c)) / len(letters)


def clean_transcript(
    transcript: str,
    slide_texts: list[str] | None = None,
    *,
    remove_filler: bool = True,
    restore_slide_terms: bool = True,
) -> TranscriptCleanResult:
    """Clean one transcript against its slides.

    Only whole lines that are *nothing but* chatter are dropped. A greeting at
    the start of a sentence that goes on to teach something is left alone —
    losing a real point to tidy up a filler word is the wrong trade.
    """
    original = _normalise(transcript or "")
    result = TranscriptCleanResult(
        text=original,
        original_words=len(original.split()),
        devanagari_ratio=devanagari_ratio(original),
    )
    if not original.strip():
        result.cleaned_words = 0
        return result

    glossary = build_glossary(slide_texts or [])
    result.glossary = glossary

    lines = [line.strip() for line in original.splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line:
            continue
        if _CHATTER_RE.search(line) and len(line.split()) <= 25:
            result.removed_chatter_lines += 1
            continue
        if remove_filler:
            without_filler = _FILLER_RE.sub("", line).strip(" ,.।-")
            if not without_filler or len(without_filler.split()) < 2:
                result.removed_filler_lines += 1
                continue
        kept.append(line)

    kept = _collapse_immediate_repeats(kept)
    text = "\n".join(kept)

    if restore_slide_terms:
        text, repairs = restore_terms(text, glossary)
        result.repairs = repairs

    result.text = text
    result.cleaned_words = len(text.split())
    return result


def glossary_prompt_block(glossary: list[str], *, limit: int = 60) -> str:
    """The slide vocabulary, as an instruction the model can act on.

    Capped because a 300-term list crowds out the lecture itself; the longest
    terms are the ones captions mangle worst, and build_glossary sorts that way.
    """
    # Multi-word terms and acronyms only. The single capitalised words earn
    # their place in the glossary (they drive term repair) but listing them here
    # would spend the model's attention on "Charter" and "Level".
    terms = [t for t in glossary if " " in t or t.isupper()][:limit]
    if not terms:
        return ""
    listed = "; ".join(terms)
    return f"""
SLIDE VOCABULARY [AUTHORITATIVE SPELLINGS]
These terms come from the slides. Spell them EXACTLY like this in the notes, even
if the transcript spells them differently or writes them in Devanagari:
{listed}
"""
