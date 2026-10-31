from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping

from rapidfuzz import fuzz

from .models import CoverageCandidate, NoteDocument, TaxonomyRow


MATCHER_VERSION = "closed-set-multilingual-v2"

_GENERIC_TARGETS = {
    "general questions",
    "miscellaneous",
    "others",
    "other",
    "introduction",
    "current affairs",
}

_GENERIC_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "concept",
    "concepts",
    "general",
    "important",
    "in",
    "india",
    "international",
    "national",
    "of",
    "other",
    "others",
    "the",
    "to",
    "types",
    "world",
    "और",
    "का",
    "के",
    "की",
    "में",
    "से",
}

_SUBJECT_HINT_ALIASES = {
    "ancient history": ("ancient history", "प्राचीन इतिहास"),
    "art and culture": ("art and culture", "कला एवं संस्कृति", "कला और संस्कृति"),
    "economics": ("economics", "अर्थव्यवस्था", "अर्थशास्त्र"),
    "environment": (
        "environment",
        "environment and ecology",
        "ecology notes",
        "पर्यावरण एवं पारिस्थितिकी",
        "पर्यावरण और पारिस्थितिकी",
        "पारिस्थितिकी",
    ),
    "human and economic geography": (
        "human geography",
        "मानव भूगोल",
        "आर्थिक भूगोल",
    ),
    "indian physical geography": (
        "indian geography",
        "geography of india",
        "भारत का भूगोल",
        "भारतीय भूगोल",
    ),
    "international relations": ("international relations", "अंतर्राष्ट्रीय संबंध"),
    "medieval history": ("medieval history", "मध्यकालीन इतिहास"),
    "modern history": ("modern history", "आधुनिक इतिहास"),
    "physical geography": ("physical geography", "भौतिक भूगोल"),
    "polity": (
        "polity",
        "indian polity",
        "राजव्यवस्था",
        "भारतीय राजव्यवस्था",
        "भारतीय संविधान",
    ),
    "science & technology": (
        "science and technology",
        "science technology",
        "विज्ञान एवं प्रौद्योगिकी",
        "विज्ञान और प्रौद्योगिकी",
    ),
}

# These bridges cover standard syllabus equivalences where the note wording and
# the closed-set QBG label use different, established names. They nominate an
# existing row only; labels and IDs still come solely from the taxonomy workbook.
_SYLLABUS_BRIDGES: dict[str, tuple[str, ...]] = {
    "drainage systems of india": (
        "river system",
        "river systems",
        "river basin",
        "east flowing rivers",
        "west flowing rivers",
        "tributaries",
        "krishna river",
        "cauvery river",
        "godavari river",
        "mahanadi river",
    ),
    "soil types": (
        "indian soils",
        "alluvial soil",
        "black soil",
        "regur soil",
        "red soil",
        "laterite soil",
    ),
    "levels of organisations in ecology": (
        "levels of ecological organisation",
        "individual organism",
        "population community ecosystem biome biosphere",
    ),
    "principles of ecology": (
        "ecology environment and ecosystem",
        "ecological principles",
    ),
    "concept of ecosystem": (
        "an ecosystem is",
        "ecosystem is defined",
    ),
    "biodiversity concepts and types": (
        "levels of biodiversity",
        "genetic diversity",
        "species diversity",
        "ecosystem diversity",
    ),
    "climate change concepts": (
        "global warming",
        "climate change",
        "greenhouse effect",
    ),
    # Hindi notes are matched to these English closed-set labels through explicit
    # syllabus equivalences.  The output still hydrates the untouched workbook row.
    "historical background": (
        "ऐतिहासिक पृष्ठभूमि",
        "संवैधानिक विकास",
        "भारत शासन अधिनियम",
    ),
    "making of the constitution": (
        "संविधान का निर्माण",
        "संविधान निर्माण",
        "संविधान सभा",
        "प्रारूप समिति",
        "उद्देश्य प्रस्ताव",
    ),
    "preamble of the constitution": (
        "संविधान की प्रस्तावना",
        "भारतीय संविधान की प्रस्तावना",
        "प्रस्तावना",
        "उद्देशिका",
    ),
    "union and its territory": (
        "संघ और उसका राज्य क्षेत्र",
        "संघ एवं उसका राज्य क्षेत्र",
        "संघ और उसके राज्य क्षेत्र",
        "संघ तथा उसका राज्य क्षेत्र",
        "राज्यों का पुनर्गठन",
        "राज्य पुनर्गठन",
        "अनुच्छेद 1",
        "अनुच्छेद 2",
        "अनुच्छेद 3",
        "अनुच्छेद 4",
    ),
    "administration of union territories": (
        "संघ राज्य क्षेत्रों का प्रशासन",
        "संघ शासित प्रदेशों का प्रशासन",
        "केंद्र शासित प्रदेशों का प्रशासन",
    ),
    "creation of union territories": (
        "संघ राज्य क्षेत्र का निर्माण",
        "संघ राज्य क्षेत्रों का निर्माण",
        "संघ शासित प्रदेशों का गठन",
        "केंद्र शासित प्रदेशों का गठन",
    ),
    "special provisions for delhi": (
        "दिल्ली के लिए विशेष प्रावधान",
        "राष्ट्रीय राजधानी क्षेत्र दिल्ली",
    ),
}

_DEVANAGARI_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "ळ": "l", "क़": "q",
    "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f",
}
_DEVANAGARI_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}
_DEVANAGARI_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
}
_DEVANAGARI_SIGNS = {"ं": "n", "ँ": "n", "ः": "h", "ऽ": ""}


def _transliterate_devanagari(value: str) -> str:
    """Romanize spellings only; this intentionally performs no translation."""
    characters = list(value)
    output: list[str] = []
    for index, character in enumerate(characters):
        if character in _DEVANAGARI_CONSONANTS:
            output.append(_DEVANAGARI_CONSONANTS[character])
            following = characters[index + 1] if index + 1 < len(characters) else ""
            if following not in _DEVANAGARI_MATRAS and following != "्":
                output.append("a")
        elif character in _DEVANAGARI_VOWELS:
            output.append(_DEVANAGARI_VOWELS[character])
        elif character in _DEVANAGARI_MATRAS:
            output.append(_DEVANAGARI_MATRAS[character])
        elif character in _DEVANAGARI_SIGNS:
            output.append(_DEVANAGARI_SIGNS[character])
        elif character in {"्", "़"}:
            continue
        else:
            output.append(character)
    return normalize_for_search("".join(output))


def _roman_skeleton(value: str) -> str:
    """Collapse common transliteration vowel variants for proper-noun comparison."""
    words = normalize_for_search(value).split()
    return " ".join(re.sub(r"[aeiou]+", "", word) for word in words)


def normalize_for_search(value: str | None) -> str:
    # NFKC keeps Devanagari vowel signs/marks intact.  The previous ASCII-only
    # regex erased an entire Hindi upload before matching.
    text = unicodedata.normalize("NFKC", value or "").casefold()
    searchable = "".join(
        character
        if character.isalnum() or unicodedata.category(character).startswith("M")
        else " "
        for character in text
    )
    return re.sub(r"\s+", " ", searchable).strip()


def _is_matchable(target: str) -> bool:
    if not target or target in _GENERIC_TARGETS or re.fullmatch(r"20\d{2}", target):
        return False
    tokens = target.split()
    return len(target) >= 5 and (len(tokens) > 1 or len(target) >= 7)


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    padded = f" {haystack} "
    if f" {needle} " in padded:
        return True
    # OCR frequently joins colored heading words without spaces.
    compact_needle = needle.replace(" ", "")
    return len(compact_needle) >= 9 and compact_needle in haystack.replace(" ", "")


def _candidate_lines(text: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for line in text.splitlines():
        display = re.sub(r"\s+", " ", line).strip(" •·-–—\t")
        normalized = normalize_for_search(display)
        if not normalized or len(display) > 180:
            continue
        words = normalized.split()
        if len(words) <= 20:
            output.append((display, normalized))
    return output


def _best_line(
    target: str,
    pages: list[tuple[int, str, list[tuple[str, str]]]],
) -> tuple[float, int, str, bool]:
    best_score = 0.0
    best_page = 1
    best_evidence = ""
    for page_number, page_text, lines in pages:
        if _contains(page_text, target):
            exact_line = next(
                (display for display, normalized in lines if _contains(normalized, target)),
                target,
            )
            return 100.0, page_number, exact_line, True
        target_tokens = {
            token for token in target.split() if token not in _GENERIC_WORDS
        } or set(target.split())
        for display, normalized in lines:
            line_tokens = set(normalized.split())
            overlap = len(target_tokens & line_tokens) / max(1, len(target_tokens))
            if overlap < 0.67:
                continue
            score = max(fuzz.ratio(target, normalized), fuzz.token_sort_ratio(target, normalized))
            if score > best_score:
                best_score = score
                best_page = page_number
                best_evidence = display
    return best_score, best_page, best_evidence, False


def _subject_hints(document: NoteDocument, rows: tuple[TaxonomyRow, ...]) -> set[str]:
    title_lines = "\n".join(
        line
        for page in document.pages[:5]
        for line in page.text.splitlines()[:18]
    )
    searchable = normalize_for_search(f"{document.filename}\n{title_lines}")
    subjects = {row.subject for row in rows}
    hints = {
        subject
        for subject in subjects
        if _contains(searchable, normalize_for_search(subject))
    }
    for subject_key, aliases in _SUBJECT_HINT_ALIASES.items():
        if any(_contains(searchable, normalize_for_search(alias)) for alias in aliases):
            hints.update(
                subject for subject in subjects if normalize_for_search(subject) == subject_key
            )
    return hints


def _bridge_match(
    row: TaxonomyRow,
    pages: list[tuple[int, str, list[tuple[str, str]]]],
) -> tuple[int, str] | None:
    keys = {
        normalize_for_search(row.chapter),
        normalize_for_search(row.topic),
        normalize_for_search(row.subtopic),
    }
    phrases = tuple(
        phrase
        for key in keys
        for phrase in _SYLLABUS_BRIDGES.get(key, ())
    )
    if not phrases:
        return None
    for page_number, page_text, lines in pages:
        matched = [phrase for phrase in phrases if _contains(page_text, normalize_for_search(phrase))]
        if matched:
            evidence = next(
                (
                    display
                    for display, normalized in lines
                    if any(_contains(normalized, normalize_for_search(phrase)) for phrase in matched)
                ),
                matched[0],
            )
            return page_number, evidence
    return None


def match_coverage(
    document: NoteDocument,
    taxonomy: tuple[TaxonomyRow, ...] | list[TaxonomyRow],
    *,
    aliases_by_row_id: Mapping[str, tuple[str, ...]] | None = None,
    max_candidates: int = 300,
) -> tuple[CoverageCandidate, ...]:
    rows = tuple(taxonomy)
    pages = [
        (
            page.page_number,
            normalize_for_search(page.text),
            _candidate_lines(page.text),
        )
        for page in document.pages
        if page.text
    ]
    if not pages:
        return ()

    aliases_by_row_id = aliases_by_row_id or {}
    transliterated_pages = [
        (
            page_number,
            _transliterate_devanagari(page_text),
            [
                (display, _transliterate_devanagari(normalized))
                for display, normalized in lines
            ],
        )
        for page_number, page_text, lines in pages
    ]
    transliteration_skeleton_pages = [
        (
            page_number,
            _roman_skeleton(page_text),
            [
                (display, _roman_skeleton(normalized))
                for display, normalized in lines
            ],
        )
        for page_number, page_text, lines in transliterated_pages
    ]
    has_devanagari = any(
        re.search(r"[\u0900-\u097f]", page_text) for _, page_text, _ in pages
    )

    targets = [normalize_for_search(row.subtopic or row.topic) for row in rows]
    target_frequency = Counter(targets)
    subject_hints = _subject_hints(document, rows)
    line_match_cache: dict[str, tuple[float, int, str, bool]] = {}
    candidates: list[CoverageCandidate] = []

    for row, target in zip(rows, targets, strict=True):
        alias_matches: list[tuple[float, int, str, bool]] = []
        for alias in aliases_by_row_id.get(row.row_id, ()):
            normalized_alias = normalize_for_search(alias)
            if _is_matchable(normalized_alias):
                alias_matches.append(_best_line(normalized_alias, pages))

        if not _is_matchable(target) and not alias_matches:
            continue
        bridge = _bridge_match(row, pages)
        if _is_matchable(target) and target not in line_match_cache:
            line_match_cache[target] = _best_line(target, pages)
        fuzzy_score, page_number, evidence, is_exact = line_match_cache.get(
            target,
            (0.0, 1, "", False),
        )
        method = "exact label" if is_exact else "fuzzy heading"
        confidence = (0.98 if len(target.split()) > 1 else 0.94) if is_exact else fuzzy_score / 100.0

        if alias_matches:
            alias_score, alias_page, alias_evidence, alias_exact = max(
                alias_matches,
                key=lambda item: (item[3], item[0]),
            )
            alias_confidence = 0.96 if alias_exact else alias_score / 100.0
            if alias_confidence > confidence:
                confidence = alias_confidence
                page_number = alias_page
                evidence = alias_evidence
                method = "row-scoped alias"

        if bridge and not is_exact:
            page_number, evidence = bridge
            confidence = max(confidence, 0.91)
            method = "syllabus bridge"

        # A conservative proper-noun bridge can connect हिमालय ↔ Himalaya.
        # It cannot connect semantic translations such as मृदा ↔ soil.
        if (
            confidence < 0.82
            and has_devanagari
            and re.fullmatch(r"[a-z0-9 ]+", target)
            and len(target.split()) == 1
            and len(target) >= 6
        ):
            translit_score, translit_page, translit_evidence, translit_exact = _best_line(
                target,
                transliterated_pages,
            )
            skeleton_score, skeleton_page, skeleton_evidence, skeleton_exact = _best_line(
                _roman_skeleton(target),
                transliteration_skeleton_pages,
            )
            if skeleton_score > translit_score or skeleton_exact:
                translit_score = skeleton_score
                translit_page = skeleton_page
                translit_evidence = skeleton_evidence
                translit_exact = skeleton_exact
            translit_confidence = 0.90 if translit_exact else translit_score / 100.0
            if translit_confidence >= 0.85:
                confidence = translit_confidence
                page_number = translit_page
                evidence = translit_evidence
                method = "proper-noun transliteration"
        if confidence < 0.82:
            continue

        page_text = next(text for number, text, _ in pages if number == page_number)
        chapter = normalize_for_search(row.chapter)
        topic = normalize_for_search(row.topic)
        has_parent_context = _contains(page_text, chapter) or (
            row.subtopic is not None and _contains(page_text, topic)
        )
        evidence_words = normalize_for_search(evidence).split()
        target_words = target.split()
        heading_like = len(evidence_words) <= max(6, len(target_words) + 4)
        ambiguous_target = target_frequency[target] > 1 and not has_parent_context
        if ambiguous_target:
            confidence = min(confidence, 0.79)
            method += " (ambiguous path)"

        subject_mismatch = bool(subject_hints) and row.subject not in subject_hints
        if subject_mismatch and not has_parent_context:
            confidence = min(confidence, 0.79)
            method += " (outside detected subject)"

        weak_single_term = len(target_words) == 1 and not heading_like and not has_parent_context

        candidates.append(
            CoverageCandidate(
                row_id=row.row_id,
                confidence=round(confidence, 3),
                evidence=re.sub(r"\s+", " ", evidence).strip()[:220] or target,
                page_number=page_number,
                method=method,
                recommended=(
                    confidence >= 0.88
                    and not ambiguous_target
                    and not subject_mismatch
                    and not weak_single_term
                ),
            )
        )

    candidates.sort(key=lambda item: (-item.confidence, item.page_number, item.row_id))
    return tuple(candidates[:max_candidates])
