"""How long a set of Daily Class Notes may run, and what to do when it runs longer.

The layout is fixed to the published PW house style, so page count is no longer
something typography can buy back — 11 pt body and an 18/16/14 heading ladder are
what the format is. Length is therefore a content decision, and this module owns
the arithmetic for it:

* how many words the notes are allowed (a page band times a measured, per-language
  words-per-page rate),
* how many words a given draft actually has,
* and the instruction that condenses a draft that came in too long.

It deliberately does NOT truncate. Cutting a set of notes at a word count drops
whatever the faculty happened to teach last, which is invisible to the person
reading them and unrecoverable. Over-long notes are either condensed by the model
— which can drop restatement instead of facts — or handed over intact with a
warning saying how long they are.
"""
from __future__ import annotations

import re

from .config import (
    DCN_MAX_COMPRESSION_PASSES,
    DCN_NOTES_OVERRUN_TOLERANCE,
    DCN_TARGET_PAGES,
    DCN_WORDS_PER_PAGE,
    DCN_WORDS_PER_PAGE_DEFAULT,
)
from .languages import is_devanagari, language_name, normalise

#: Marker lines carry structure, not prose. "#H2: Tribal Structure" is two words
#: of notes and one word of scaffolding, and the bullet dashes are none at all —
#: counting them would inflate every measurement by a few per cent and make the
#: budget mean something slightly different from what the writer sees.
_MARKER_PREFIX_RE = re.compile(
    r"^\s*(?:#(?:LECTURE_TITLE|H1|H2|H3|H4)\s*:|[-*]\s+|\d{1,2}[.)]\s+)")
#: Whole-line markers that are pure structure.
_BARE_MARKER_RE = re.compile(
    r"^\s*#(?:TABLE_START|TABLE_END|FLOW_START|FLOW_END|SEPARATOR"
    r"|TOPICS_COVERED_START|TOPICS_COVERED_END|CORRECTION_START|CORRECTION_END"
    r"|FIGURE)\b")
#: Markdown emphasis and table pipes are punctuation around words, not words.
_NOISE_RE = re.compile(r"[*|`]+")


def _lang(language_code: str | None) -> str:
    """The key DCN_WORDS_PER_PAGE is stored under. "" (unknown) reads as English."""
    return normalise(language_code) or "en"


def words_per_page(language_code: str = "en") -> int:
    """Measured rate for this language — see DCN_WORDS_PER_PAGE in config."""
    return DCN_WORDS_PER_PAGE.get(_lang(language_code), DCN_WORDS_PER_PAGE_DEFAULT)


def count_words(notes_text: str) -> int:
    """Words a reader would count on the page.

    Whitespace-separated tokens, after the markers are stripped. Devanagari
    splits on whitespace the same way Latin does, so one counter serves both
    languages; what it must not do is count "#H2:" or a bullet dash as content.
    """
    total = 0
    for line in (notes_text or "").splitlines():
        if _BARE_MARKER_RE.match(line):
            continue
        line = _MARKER_PREFIX_RE.sub("", line)
        line = _NOISE_RE.sub(" ", line)
        total += len(line.split())
    return total


def estimated_pages(notes_text_or_words: str | int, language_code: str = "en") -> float:
    """Rendered pages a draft of this length comes to, at the house layout."""
    words = (count_words(notes_text_or_words)
             if isinstance(notes_text_or_words, str) else int(notes_text_or_words))
    return words / words_per_page(language_code)


def word_budget(language_code: str = "en") -> tuple[int, int]:
    """(minimum, maximum) words for one lecture's notes, in this language."""
    rate = words_per_page(language_code)
    low, high = DCN_TARGET_PAGES
    return low * rate, high * rate


def condense_threshold(language_code: str = "en") -> int:
    """Above this many words a draft is worth spending a model call on.

    The page band is "10 to 16, a little either way", so a draft a few per cent
    over the maximum is inside what was asked for and is left alone.
    """
    _, high = word_budget(language_code)
    return int(round(high * (1.0 + DCN_NOTES_OVERRUN_TOLERANCE)))


def is_over_budget(notes_text: str, language_code: str = "en") -> bool:
    return count_words(notes_text) > condense_threshold(language_code)


def chunk_word_budget(chunk_total: int, language_code: str = "en") -> int:
    """One part's share of the whole class's budget.

    The long-lecture path writes each stretch of the class separately and then
    joins them, so a budget applied only to the final merge arrives too late —
    the merge would have to throw away work the parts were told to do in full.
    Each part gets its share up front instead.

    A floor keeps a class split into many parts from being asked for notes so
    short that a part cannot state its own topic.
    """
    parts = max(1, int(chunk_total))
    _, high = word_budget(language_code)
    return max(250, high // parts)


def budget_block(language_code: str = "en") -> str:
    """The length instruction shared by the single-call and merge prompts."""
    low_pages, high_pages = DCN_TARGET_PAGES
    low, high = word_budget(language_code)
    if is_devanagari(language_code):
        # One Devanagari block serves Hindi and Marathi. The wording is
        # deliberately register-neutral (numerals and "शब्द" read the same in
        # both) rather than a Hindi paragraph shown to a Marathi run.
        return (
            f"लंबाई की सीमा [अनिवार्य]\n"
            f"- पूरे नोट्स लगभग {low:,} से {high:,} शब्दों के होने चाहिए "
            f"(छपने पर {low_pages}-{high_pages} पृष्ठ)। {high:,} शब्द ऊपरी सीमा है, लक्ष्य नहीं।\n"
            f"- लंबाई तथ्यों से आती है, दोहराव से नहीं। यदि नोट्स लंबे हो रहे हों तो पुनरावृत्ति, "
            f"भूमिका और उदाहरणों का विस्तार काटें — तथ्य, तिथियाँ, नाम, अनुच्छेद, अधिनियम, "
            f"योजनाएँ, समितियाँ या आँकड़े कभी न हटाएँ।\n"
            f"- एक बिंदु एक बार। पहले कही गई बात दोबारा न लिखें।"
        )
    return (
        f"LENGTH BUDGET [HARD]\n"
        f"- The finished notes must be about {low:,}-{high:,} words "
        f"({low_pages}-{high_pages} printed pages). {high:,} words is a ceiling, "
        f"not a target.\n"
        f"- Length comes from facts, never from restating. If the notes run long, cut "
        f"repetition, run-ups and worked-out analogies — never facts, dates, names, "
        f"Articles, Acts, schemes, committees, reports or data.\n"
        f"- Say each point once. Do not restate a heading's content in its first bullet."
    )


def chunk_budget_line(chunk_index: int, chunk_total: int, language_code: str = "en") -> str:
    """The per-part length instruction for the long-lecture path."""
    share = chunk_word_budget(chunk_total, language_code)
    _, high = word_budget(language_code)
    if is_devanagari(language_code):
        return (
            f"इस भाग की लंबाई सीमा [अनिवार्य]\n"
            f"- यह भाग लगभग {share:,} शब्दों का हो (पूरी कक्षा के {high:,} शब्दों "
            f"के बजट में से इस भाग का हिस्सा)। यह सीमा है, लक्ष्य नहीं।\n"
            f"- हर तथ्य रखें, पर एक ही बात दो बार न लिखें और व्याख्या को फैलाएँ नहीं।"
        )
    return (
        f"LENGTH BUDGET FOR THIS PART [HARD]\n"
        f"- Write about {share:,} words for this part — its share of the whole class's "
        f"{high:,}-word budget. That is a ceiling, not a target.\n"
        f"- Keep every fact, but state each once and do not spread an explanation out. "
        f"Density, not length: the parts are joined, so an over-long part costs the "
        f"whole document its page budget."
    )


def pdf_page_count(pdf_path) -> int:
    """Pages in a rendered PDF, or 0 if it cannot be read.

    The word budget is a prediction; this is the measurement. Returns 0 rather
    than raising, because a page count that cannot be taken must not be able to
    fail a run whose documents are already written — the caller treats 0 as
    "unknown" and simply stops trying to compress.
    """
    if not pdf_path:
        return 0
    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            return doc.page_count
    except Exception:      # noqa: BLE001 — an unreadable render is not fatal here
        return 0


def page_limit() -> int:
    """The hard ceiling: a finished document may not exceed this many pages."""
    return DCN_TARGET_PAGES[1]


def max_compression_passes() -> int:
    return DCN_MAX_COMPRESSION_PASSES


def build_page_compress_prompt(notes_text: str, *, language_code: str = "en",
                               actual_pages: int, attempt: int = 1) -> str:
    """Compress notes that RENDERED too long, quoting the real page count.

    Separate from build_condense_prompt because the two know different things.
    That one runs before anything is rendered and can only talk about words;
    this one has the actual page count from the actual document, which is both a
    more concrete instruction and a check the model cannot argue with.

    The proportion is given explicitly. "Make it shorter" produces a token trim
    on a document that is four pages over; "remove about 27% of the length"
    produces a document that fits.
    """
    limit = page_limit()
    over = max(1, actual_pages - limit)
    # Aim slightly inside the limit: a compression that lands exactly on 15
    # pages by prediction lands on 16 as often as not.
    target_pages = max(1, limit - 1)
    cut_fraction = max(0.05, min(0.6, 1.0 - (target_pages / max(1, actual_pages))))
    language = language_name(language_code)
    low, high = DCN_TARGET_PAGES
    pressure = ("This is the FINAL attempt — the document ships at whatever length "
                "you return." if attempt >= DCN_MAX_COMPRESSION_PASSES else "")
    return f"""You are an expert UPSC Daily Class Notes editor.

These notes are finished and correct, but the document they produced is
{actual_pages} pages long. The limit is {limit} pages ({low}-{high} is the target).
That is {over} page(s) too many.

Return the SAME notes, about {round(cut_fraction * 100)}% shorter, so they come to
roughly {target_pages} pages. {pressure}

HOW TO SHORTEN — in this order
1. Delete repetition: the same point made twice, in a section or across sections.
2. Delete run-ups and framing ("Let us now understand...", "In this context...",
   a first bullet that only restates its own heading).
3. Merge closely related bullets into one denser bullet.
4. Cut worked-out analogies and second/third examples of the same idea — keep the
   single clearest example.
5. Turn a repetitive paragraph into two or three compact bullets.
6. Tighten wording without changing meaning.

WHAT MUST SURVIVE — ALL OF IT
- Every concept, definition, classification and analytical argument.
- Every fact, figure, date, percentage, name, place and case study.
- Every Article, Act, Amendment, constitutional provision, scheme, committee,
  report and institution.
- Every comparison and every process/sequence.
- Every table, every #FLOW block and every #FIGURE line, unchanged.
- Every heading, and the heading ladder as it stands.
- Every #LECTURE_TITLE and #TOPICS_COVERED block, unchanged.

Do NOT drop a section to save pages. Shorten across the whole document evenly.
Losing one named fact is worse than being one page over.

MARKERS
Return the notes in the marker format you are given — #LECTURE_TITLE:,
#TOPICS_COVERED_START/END, #H1:/#H2:/#H3:/#H4:, "- " bullets at three indent
levels, #TABLE_START/END, #FLOW_START/END, #SEPARATOR, #FIGURE:. Nothing outside
them, and no commentary about editing.

Language: {language}

------------------------------------------------------------------
NOTES TO COMPRESS
------------------------------------------------------------------

{notes_text}
""".strip()


#: A condensed draft may not lose more of itself than this. Below it, what came
#: back is a summary of the notes rather than a tightened copy of them.
MIN_RETAINED_FRACTION = 0.5


def accept_condensed(draft: str, condensed: str) -> bool:
    """Is ``condensed`` a safe replacement for ``draft``?

    Guards the one way this stage can do real damage. The condense prompt asks
    for the same notes with the padding removed, but a model that misreads it
    returns a summary — and a summary that replaces the notes silently loses the
    facts a student is examined on.

    Two cheap checks catch that: it has to be SHORTER (or there was no point),
    and it has to still be at least half the draft (or it is a different, much
    smaller document). Anything else and the caller keeps the original, which is
    merely too long — the recoverable failure.
    """
    if not (condensed or "").strip():
        return False
    before, after = count_words(draft), count_words(condensed)
    if before == 0:
        return False
    return before > after >= before * MIN_RETAINED_FRACTION


def build_condense_prompt(notes_text: str, *, language_code: str = "en",
                          max_words: int | None = None) -> str:
    """Ask for the same notes, shorter — by removing restatement, not content.

    Given to the model only when a draft came back well over budget. The
    instruction is one-sided on purpose: everything it lists as removable is
    padding, and everything a UPSC student is examined on is named as protected.
    """
    ceiling = max_words or word_budget(language_code)[1]
    current = count_words(notes_text)
    language = language_name(language_code)
    low_pages, high_pages = DCN_TARGET_PAGES
    return f"""You are an expert UPSC Daily Class Notes editor.

The notes below are finished and correct, but they are too long: {current:,} words
against a budget of {ceiling:,} ({low_pages}-{high_pages} printed pages). Return the
SAME notes, tightened to about {ceiling:,} words.

WHAT TO CUT
- Repetition: the same point made twice in different words, in the same section or
  across sections.
- Run-ups and framing: "In this context it is important to understand that ...",
  "Let us now look at ...", sentences that announce the next bullet.
- Worked-out analogies and extended examples where a named example alone carries it.
- Adjectives and hedges that add no information.
- A first bullet that only restates its own heading.

WHAT TO KEEP — ALL OF IT
- Every fact, figure, date, percentage, name, place and case.
- Every Article, Act, Amendment, scheme, committee, report, institution and
  Constitutional provision.
- Every definition, classification and comparison.
- Every heading, and the heading ladder as it stands.
- Every table and every #FLOW / #FIGURE block, unchanged.
- Every #LECTURE_TITLE, #TOPICS_COVERED block and marker line, unchanged.

If a bullet contains only facts, leave it exactly as it is. Shorten prose, not content.
Losing one named fact is worse than being 200 words over.

MARKERS
Return the notes in the same marker format you are given — #LECTURE_TITLE:,
#TOPICS_COVERED_START/END, #H1:/#H2:/#H3:/#H4:, "- " bullets at three indent levels,
#TABLE_START/END, #FLOW_START/END, #SEPARATOR, #FIGURE:. Nothing outside them, and no
commentary about editing.

Language: {language}

------------------------------------------------------------------
NOTES TO TIGHTEN
------------------------------------------------------------------

{notes_text}
""".strip()
