"""Make the notes cover the whole lecture.

The generator sizes its own chunks by bytes, with a 1 MB floor: a 21-page
lecture went to the model as four calls of six pages each, and one of those
came back with a fraction of the content — the autoimmune-disorder slides
never reached the notes at all.

This module drives the generator a page-group at a time instead:

* the source PDF is split into fixed page groups (``PAGES_PER_CALL``), so each
  model call sees a handful of slides and has room to write them out fully;
* a group whose answer comes back suspiciously thin is asked once more, and the
  fuller of the two answers is kept;
* the parts are merged with the generator's own ``_merge``, which concatenates
  sections in lecture order and pools the mind-map branches.

``notesbot`` is imported, never modified.
"""
from __future__ import annotations

import io
import os
import re
import time

from notesbot.errors import ModelBudgetError, UserError, VisualFidelityError, NotesCoverageError

from .errors import describe_exception


def _content_verdict(exc: BaseException | None) -> bool:
    """True when the generator itself judged the pages to hold no study
    content - the only failure that may drop an edge group. Transport errors
    (proxy, timeout, rate limit) are not verdicts about the pages."""
    return isinstance(exc, UserError) and not isinstance(exc, (VisualFidelityError, NotesCoverageError))
from dataclasses import dataclass

from notesbot.generation_profile import call_label, call_purpose, inline_source_check

# Pages sent per generation call. The fixed prompt, schema and reasoning are
# paid per call, not per page, so 8 pages per call roughly halves the calls
# (and the reviews that follow each one) against 4. A dense group that times
# out is still halved once and retried.
DEFAULT_PAGES_PER_CALL = 8
# A last page group this short gets no source review of its own: it is
# usually a summary or closing slide, and the review costs a full call.
SHORT_TAIL_PAGES = 3
MIN_POINTS_PER_PAGE = 1.5
# Calls fired back-to-back made the gateway drop three groups in a row, while
# the same pages succeeded on their own. Stagger the starts a little.
GROUP_PAUSE_SECONDS = 1.0
# Transport wobbles (timeout, gateway) get one retry; every extra call is paid.
GROUP_ATTEMPTS = 2
# ...unless the model LOOKED and said there is no study content (an empty
# answer is a UserError, see _content_verdict). That is a verdict, not a
# wobble: the pages are skipped on the first look - a second look at a
# cover, an index or a question-only page only repeats the bill.
VERDICT_ATTEMPTS = 1
# How many page groups may be in flight at once. Sequential generation costs
# (call + pause) per group, so a 44-page lecture took twelve calls back to back
# - minutes of wall clock for one row. Dispatch is still staggered by
# GROUP_PAUSE_SECONDS, so the gateway sees a steady trickle rather than a
# burst; 1 restores the exact old behaviour.
DEFAULT_GROUP_WORKERS = 4
# The Hindi share a Hindi lecture's notes aim for, by word count. Maths
# symbols (A, kA, sin, aij) are not counted as English, so a matrices lecture
# is judged on its prose, not its formulas. A group is asked again - up to
# HINDI_REASKS times, with a blunt Hindi-only instruction - only when it falls
# below HINDI_REASK_BELOW (the shipping floor further down): re-asking a 74-87%
# answer bought 2 points for a full generation call, or nothing at all.
MIN_HINDI_SHARE = 0.90
HINDI_REASKS = 1
# ...and this much is the hard floor the finished notes must clear. The two
# numbers must differ: generation KEEPS the most-Hindi answer it got even when
# the re-asks fall short (79%, 81% are normal for biology and chemistry, whose
# Hindi notes carry English exam terms), so a final gate set at the 0.90 target
# rejected lectures that generation had already accepted - after every call had
# been paid for. Below the floor the notes really are half English.
MIN_HINDI_SHARE_FINAL = 0.70
HINDI_REASK_BELOW = MIN_HINDI_SHARE_FINAL
# The mirror image for an English lecture: some Hindi is fine, half is not.
MAX_HINDI_SHARE_IN_ENGLISH = 0.10
# The gate needs real English to react to: an answer made only of symbols and
# single letters has nothing to translate and is left alone.
MIN_ENGLISH_WORDS_TO_JUDGE = 3
# A source is treated as a Hindi lecture when this much of its wording is
# Devanagari. Slides are usually bilingual, so the bar is deliberately low.
SOURCE_HINDI_SHARE = 0.15
RETRY_BACKOFF_SECONDS = 5.0


def group_workers() -> int:
    """``SHEETS_BRIDGE_GROUP_WORKERS`` page groups generated concurrently."""
    raw = os.environ.get("SHEETS_BRIDGE_GROUP_WORKERS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_GROUP_WORKERS
    except ValueError:
        value = DEFAULT_GROUP_WORKERS
    return max(1, min(value, 6))


def source_review_enabled() -> bool:
    """``SHEETS_BRIDGE_SOURCE_REVIEW=on`` brings back the per-group source review.

    Off by default since 2026-09-17. With 8-page groups the reviewer found an
    issue in nearly every group, and each issue bought a full repair call:
    review plus repair was 55% of a lecture's cost (INR 72.57 of 131.81 over
    two lectures). The lean prompt, the lecture-wide duplicate pass and the
    final quality check still run.
    """
    raw = os.environ.get("SHEETS_BRIDGE_SOURCE_REVIEW", "").strip().lower()
    return raw in ("1", "on", "true", "yes")


def pages_per_call() -> int:
    raw = os.environ.get("SHEETS_BRIDGE_PAGES_PER_CALL", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PAGES_PER_CALL
    except ValueError:
        value = DEFAULT_PAGES_PER_CALL
    return max(1, min(20, value))


@dataclass(frozen=True)
class PageGroup:
    index: int
    first_page: int          # 1-based, inclusive
    last_page: int           # 1-based, inclusive
    pdf_bytes: bytes

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


def split_pages(pdf_bytes: bytes, group_size: int) -> list[PageGroup]:
    """Split into fixed page groups, preserving page order and content."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    if total <= group_size:
        return [PageGroup(0, 1, total, pdf_bytes)]

    groups: list[PageGroup] = []
    for start in range(0, total, group_size):
        end = min(start + group_size, total)
        writer = PdfWriter()
        for index in range(start, end):
            writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
        groups.append(PageGroup(len(groups), start + 1, end, buffer.getvalue()))
    return groups


# The generator's prompt accepts a revision request that may affect
# "selection or presentation only" - exactly the right channel for keeping a
# Hindi lecture's headings in Hindi instead of drifting into English.
_HINDI_ESCALATION = (
    "DHYAN DEIN: pichhli baar aapne bahut saara content English mein likha tha. "
    "Is baar har heading, har point, har key_term ki definition, har note, har "
    "trick aur mindmap ke sabhi nodes Hindi (Devanagari) mein likhein. English "
    "sirf formula, symbol, unit aur chemical formula ke liye."
)

LANGUAGE_INSTRUCTIONS = {
    # Maximum Hindi. The previous wording told the model to keep technical
    # terms, disease names and units in English, so a Hindi lecture came back
    # with English-heavy bullets and every heading glossed twice. English is
    # now reserved for what genuinely cannot be translated, plus a *first
    # mention* gloss for the exam term students must recognise.
    "hindi": (
        "Kam se kam 90% content Hindi (Devanagari) mein likhein - headings, "
        "points, key_terms ki definition, notes, tricks, table, compare, flow, "
        "aur mindmap ka center, branch titles va children bhi. English sirf "
        "wahan jahan Hindi mein likhna sambhav nahi: formula, symbol, unit, "
        "chemical formula, aur kisi pramukh term ka standard exam naam - wo bhi "
        "sirf pehli baar, chhote bracket mein Hindi ke baad (jaise "
        "'स्वप्रतिरक्षा रोग (Autoimmune Disorder)'). Kisi Hindi vaakya ko English "
        "mein dobara mat likhein, ek hi gloss baar-baar mat dohraayein, aur koi "
        "heading, definition ya mindmap node sirf English mein na ho."
    ),
    "hinglish": (
        "Sabhi headings aur points Hinglish (Roman script) mein likhein, jaise "
        "Indian coaching notes hote hain. Formulas aur technical terms English mein rakhein."
    ),
    "english": (
        "Write every section heading and point in English only. No Devanagari, no "
        "Hinglish words, no Hindi gloss in brackets - translate the lecture's "
        "Hindi terms into standard English terminology."
    ),
}

# English notes that still carry Hindi script after the free cleanup are asked
# again at most this many times (each re-ask is a paid call).
ENGLISH_REASKS = 1
# Devanagari share above which English notes count as mixed. A stray proper
# noun must not buy a second call.
MAX_HINDI_SHARE_BEFORE_REASK = 0.02
_ENGLISH_ESCALATION = (
    "IMPORTANT: your previous answer contained Hindi. Rewrite EVERYTHING in "
    "English only - no Devanagari script, no Hinglish words, no Hindi term in "
    "brackets. Translate the lecture's Hindi terms into standard English."
)
_HINDI_GLOSS_RE = re.compile(r"\s*[\(\[]([^()\[\]]*[\u0900-\u097F][^()\[\]]*)[\)\]]")


_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{4,}")   # 4+: sin, cos, log, aij are symbols


def hindi_share(text: str) -> float:
    """Hindi words / (Hindi + English words), 0.0 when there are no words.

    Words, not characters: a matrix lecture is full of ``A``, ``kA``, ``i``,
    ``j``, ``sin`` and ``aij``, which are symbols rather than English, so only
    Latin runs of four or more letters count against the Hindi share.
    """
    from notesbot.latex_repair import normalize_math_delimiters
    body = normalize_math_delimiters(str(text or ""))
    body = re.sub(r"\$[^$]*\$", "", body)
    hindi = len(_DEVANAGARI_RE.findall(body))
    english = sum(len(word) for word in _LATIN_WORD_RE.findall(body))
    total = hindi + english
    return (hindi / total) if total else 0.0


def english_word_count(text: str) -> int:
    """How many real English words (4+ letters) the text carries."""
    from notesbot.latex_repair import normalize_math_delimiters
    body = re.sub(r"\$[^$]*\$", "", normalize_math_delimiters(str(text or "")))
    return len(_LATIN_WORD_RE.findall(body))


def _notes_text(notes) -> str:
    """Every human-visible string of a notes object, including the mind map."""
    out: list[str] = []

    def add(value):
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)
        elif hasattr(value, "__dict__"):
            for key, item in vars(value).items():
                if not key.startswith("_"):
                    add(item)
        elif isinstance(value, dict):
            for item in value.values():
                add(item)

    for name in ("title", "subject", "audience", "sections", "mindmap"):
        add(getattr(notes, name, None))
    return " ".join(out)


def _map_strings(value, fn):
    """Apply ``fn`` to every string inside a notes object, in place."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = fn(item)
            else:
                _map_strings(item, fn)
        return
    fields = getattr(type(value), "model_fields", None)
    if not fields:
        return
    for name in fields:
        item = getattr(value, name, None)
        if isinstance(item, str):
            setattr(value, name, fn(item))
        elif item is not None:
            _map_strings(item, fn)


def strip_hindi_glosses(notes) -> None:
    """English notes: drop bracketed Hindi glosses such as 'Pollination (परागण)'.

    Free and deterministic - no model call. Hindi written inside the sentence
    itself is left for the one re-ask, since cutting it would break the text.
    """
    def clean(text: str) -> str:
        if not _DEVANAGARI_RE.search(text):
            return text
        return re.sub(r"[ 	]{2,}", " ", _HINDI_GLOSS_RE.sub("", text)).strip()

    _map_strings(notes, clean)


def notes_hindi_share(notes) -> float:
    """How Hindi the generated notes actually are, mind map included."""
    return hindi_share(_notes_text(notes))


def detect_source_language(pdf_bytes: bytes, pages: int = 6) -> str:
    """'hindi' when the lecture itself is written in Devanagari, else ''.

    The sheet's Batch column is the intended signal, but it is often blank or
    named something the mapping does not recognise - and then the notes were
    generated as "auto", which drifts into English on a Hindi lecture. The
    source is the authority nobody can forget to fill in.
    """
    from notesbot.sanskrit import resolve_language
    if resolve_language("auto", pdf_bytes=pdf_bytes) == "sanskrit":
        return "sanskrit"
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(pdf_bytes))
        sample = " ".join(
            (page.extract_text() or "") for page in reader.pages[:pages]
        )
    except Exception:  # noqa: BLE001 - detection is best effort
        return ""
    return "hindi" if hindi_share(sample) >= SOURCE_HINDI_SHARE else ""


def language_instruction(language: str) -> str:
    """Presentation-only request that keeps the notes in one language."""
    return LANGUAGE_INSTRUCTIONS.get((language or "").strip().lower(), "")


# --------------------------------------------------------------------------- #
# Duplicate topics
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\wऀ-ॿ ]+")
_HEADING_STOP = {"ka", "ke", "ki", "aur", "and", "of", "the", "एवं", "और", "का", "के", "की", "व"}
DUPLICATE_THRESHOLD = 0.6


def heading_key(text: str) -> frozenset:
    """Comparable token set for a heading.

    Bilingual headings carry the English gloss in brackets
    ("एलर्जी एवं एलर्जन (Allergy and Allergens)"), and that gloss is often the
    only part that matches when the Hindi spelling varies between groups, so
    it is kept rather than stripped.
    """
    cleaned = _PUNCT_RE.sub(" ", str(text or "")).casefold()
    return frozenset(word for word in cleaned.split() if word not in _HEADING_STOP and len(word) > 1)


def _similar(left: frozenset, right: frozenset) -> bool:
    if not left or not right:
        return False
    overlap = len(left & right) / min(len(left), len(right))
    return overlap >= DUPLICATE_THRESHOLD


def _point_key(text: str) -> str:
    from notesbot.note_quality import text_identity
    return text_identity(text)


def _compatible_sections(left, right) -> bool:
    # Two distinct tables/flows/diagrams cannot occupy one schema slot. Keep
    # separate cards instead of silently dropping the later structure.
    for attr in ("table", "compare", "graph", "schematic", "flow", "diagram", "visual", "visual_ids", "_image", "_svg"):
        first, second = getattr(left, attr, None), getattr(right, attr, None)
        if first and second and first != second:
            return False
    return True


def dedupe_sections(sections: list) -> list:
    """Fold sections that cover the same topic into one, keeping every point.

    Adjacent page groups overlap: the same topic can come back twice (often
    once per group). Merging keeps the notes readable without losing content.
    """
    kept: list = []
    keys: list[frozenset] = []
    for section in sections or []:
        key = heading_key(getattr(section, "heading", ""))
        # Subset heading tokens may indicate a bilingual gloss. Partial overlap
        # alone must not merge "symmetric matrix" with "skew symmetric matrix".
        def same_topic(existing, prior):
            if not existing or not key:
                return False
            if existing == key:
                return True
            first_base = heading_key(_PAREN_RE.sub("", prior.heading))
            second_base = heading_key(_PAREN_RE.sub("", section.heading))
            if first_base and first_base == second_base:
                return True
            first_gloss = heading_key(" ".join(_PAREN_RE.findall(prior.heading)))
            second_gloss = heading_key(" ".join(_PAREN_RE.findall(section.heading)))
            return bool(first_gloss and first_gloss == second_gloss)
        match = next((index for index, existing in enumerate(keys)
                      if same_topic(existing, kept[index]) and _compatible_sections(kept[index], section)), None)
        if match is None:
            for attr in ("points", "tricks", "notes", "key_terms"):
                current = getattr(section, attr, None)
                if current:
                    seen = set()
                    unique = []
                    for item in current:
                        identity = _point_key(item)
                        if identity not in seen:
                            unique.append(item)
                            seen.add(identity)
                    setattr(section, attr, unique)
            kept.append(section)
            keys.append(key)
            continue
        target = kept[match]
        seen = {_point_key(point) for point in (getattr(target, "points", None) or [])}
        for point in getattr(section, "points", None) or []:
            if _point_key(point) not in seen:
                target.points.append(point)
                seen.add(_point_key(point))
        for attribute in ("table", "compare", "graph", "schematic", "flow", "diagram", "visual", "visual_ids", "_image", "_svg"):
            if not getattr(target, attribute, None) and getattr(section, attribute, None):
                setattr(target, attribute, getattr(section, attribute))
        for attribute in ("tricks", "notes", "key_terms"):
            extra = getattr(section, attribute, None) or []
            current = getattr(target, attribute, None)
            if current is not None:
                existing = {_point_key(item) for item in current}
                for item in extra:
                    if _point_key(item) not in existing:
                        current.append(item)
                        existing.add(_point_key(item))
        for attribute in ("worked_examples", "where"):
            for item in getattr(section, attribute, None) or []:
                current = getattr(target, attribute, None)
                if current is None:
                    setattr(target, attribute, [])
                    current = getattr(target, attribute)
                if item not in current:
                    current.append(item)
        keys[match] = keys[match] | key
    return kept


def content_size(notes) -> int:
    """How much study material a part carries (sections, points, tables…)."""
    total = 0
    for section in getattr(notes, "sections", None) or []:
        total += len(str(getattr(section, "heading", "") or ""))
        total += sum(len(str(point)) for point in (getattr(section, "points", None) or []))
        total += sum(len(str(trick)) for trick in (getattr(section, "tricks", None) or []))
        total += sum(len(str(note)) for note in (getattr(section, "notes", None) or []))
        for example in getattr(section, "worked_examples", []) or []:
            total += len(example.statement) + len(example.result) + sum(map(len, example.steps))
        table = getattr(section, "table", None)
        if table is not None:
            total += sum(len(str(cell)) for row in (getattr(table, "rows", None) or []) for cell in row)
    return total


def is_thin(notes, page_count: int) -> bool:
    """A part that plainly under-reports its pages deserves one more attempt."""
    sections = getattr(notes, "sections", None) or []
    if not sections:
        return True
    points = sum(len(getattr(section, "points", None) or []) +
                 sum(1 + len(example.steps) for example in getattr(section, "worked_examples", []) or [])
                 for section in sections)
    return points < max(2, int(page_count * MIN_POINTS_PER_PAGE))


def _generate_group(group, *, label=None, **kwargs):
    """One page group: generate, retry a transport wobble, and keep what came back.

    Returns ``(notes | None, last_error)``. ``None`` means these pages carry no
    study content (a cover, an index, a question-only stretch) or never
    answered. The caller skips them: a lecture is never failed over one group.
    Every model call made for the group is logged under its label.
    """
    label = label or f"pages {group.first_page}-{group.last_page}"
    with call_label(label):
        return _generate_group_calls(group, label=label, **kwargs)


def _generate_group_calls(group, *, instruction, language, subject_hint, emit, label, allow_split=1,
                          verify_source=False, edge=False, review=True):
    from notesbot.gemini_client import generate_short_notes

    notes = None
    last_error: BaseException | None = None
    for attempt in range(GROUP_ATTEMPTS):
        try:
            notes = generate_short_notes(group.pdf_bytes, instruction, language, subject_hint)
            break
        except (KeyboardInterrupt, ModelBudgetError):
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            verdict = _content_verdict(exc)
            if attempt == GROUP_ATTEMPTS - 1 or (verdict and attempt >= VERDICT_ATTEMPTS - 1):
                if verdict:
                    emit(f"{label}: no study content found ({attempt + 1} look(s))")
                break
            if verdict:
                # "Nothing here" is answered at once; only a transport wobble
                # deserves a pause before the next try.
                continue
            delay = RETRY_BACKOFF_SECONDS * (attempt + 1)
            emit(f"{label}: attempt {attempt + 1} failed ({describe_exception(exc)}); retrying in {delay:.0f}s")
            time.sleep(delay)

    if notes is None and _content_verdict(last_error):
        if edge:
            # First/last pages with "no study content" are a cover or a closing
            # slide. Slicing them smaller only repeats the bill.
            allow_split = 0
        else:
            # A dense middle group may answer as halves; one halving is enough
            # to tell dense pages from empty ones.
            allow_split = min(allow_split, 1)
    if notes is None and allow_split > 0 and group.page_count > 1:
        # A transport failure (timeout, gateway) on a big group often clears
        # when the pages are sent in smaller slices. One halving only - going
        # down to single pages tripled the bill. Keep every slice that
        # answers: partial notes beat no notes.
        halves = split_pages(group.pdf_bytes, (group.page_count + 1) // 2)
        if len(halves) > 1:
            emit(f"{label}: failed as one group - retrying as {len(halves)} smaller call(s)")
            recovered = []
            for index, half in enumerate(halves, start=1):
                part, _err = _generate_group(
                    half, instruction=instruction, language=language,
                    subject_hint=subject_hint, emit=emit,
                    label=f"{label} part {index}", allow_split=allow_split - 1,
                )
                if part is not None:
                    recovered.append(part)
            if recovered:
                from notesbot.gemini_client import _merge

                notes = _merge(recovered)
                if len(recovered) == len(halves):
                    emit(f"{label}: recovered as {len(halves)} smaller call(s)")
                else:
                    emit(f"{label}: recovered {len(recovered)} of {len(halves)} smaller call(s) - "
                         "the rest had no study content")

    if notes is not None and language == "hindi":
        from notesbot.gemini_client import generate_short_notes as _gen

        for round_no in range(1, HINDI_REASKS + 1):
            text = _notes_text(notes)
            share = hindi_share(text)
            if share >= HINDI_REASK_BELOW or english_word_count(text) < MIN_ENGLISH_WORDS_TO_JUDGE:
                break
            emit(f"{label}: only {share * 100:.0f}% Hindi - asking again in Hindi ({round_no}/{HINDI_REASKS})")
            time.sleep(GROUP_PAUSE_SECONDS)
            try:
                harder = instruction + " " + _HINDI_ESCALATION
                with call_purpose("Hindi re-ask"):
                    retry = _gen(group.pdf_bytes, harder, language, subject_hint)
            except (KeyboardInterrupt, ModelBudgetError):
                raise
            except Exception:  # noqa: BLE001 - keep what we have
                break
            better = notes_hindi_share(retry)
            if better > share:
                notes = retry
                emit(f"{label}: now {better * 100:.0f}% Hindi")
            else:
                break

    if notes is not None and language == "english":
        from notesbot.gemini_client import generate_short_notes as _gen

        strip_hindi_glosses(notes)
        share = notes_hindi_share(notes)
        for round_no in range(1, ENGLISH_REASKS + 1):
            if share <= MAX_HINDI_SHARE_BEFORE_REASK:
                break
            emit(f"{label}: {share * 100:.0f}% Hindi in English notes - asking again in English "
                 f"({round_no}/{ENGLISH_REASKS})")
            try:
                with call_purpose("English re-ask"):
                    retry = _gen(group.pdf_bytes, instruction + " " + _ENGLISH_ESCALATION, language, subject_hint)
            except (KeyboardInterrupt, ModelBudgetError):
                raise
            except Exception:  # noqa: BLE001 - keep what we have
                break
            strip_hindi_glosses(retry)
            better = notes_hindi_share(retry)
            if better < share:
                notes, share = retry, better
                emit(f"{label}: now {share * 100:.0f}% Hindi")
            else:
                break

    if notes is not None and not verify_source and is_thin(notes, group.page_count) and group.page_count > 1:
        emit(f"{label}: thin answer, asking once more")
        time.sleep(GROUP_PAUSE_SECONDS)
        try:
            retry = generate_short_notes(group.pdf_bytes, instruction, language, subject_hint)
            if content_size(retry) > content_size(notes):
                notes = retry
        except (KeyboardInterrupt, ModelBudgetError):
            raise
        except Exception:  # noqa: BLE001 - keep the first answer
            pass
    checked = verify_source and source_review_enabled()
    if notes is not None and checked and not review:
        emit(f"{label}: short last group - no separate source review")
    if notes is not None and checked and review:
        from notesbot.source_coverage import ensure_source_coverage
        from notesbot.concise_policy import solved_examples_allowed
        emit(f"{label}: checking theory and formulas; "
             + ("keeping solved examples" if solved_examples_allowed() else "excluding questions and solutions"))
        try:
            notes = ensure_source_coverage(
                group.pdf_bytes, notes, language=language, subject_hint=subject_hint,
                log=lambda message: emit(f"{label}: {message}"),
                report=lambda attempt, review: emit(
                    f"{label}: review {attempt + 1}: {len(review.issues)} issue(s)"
                    f"{_kinds(review.issues)}, {len(review.repetitions)} repeat(s)"),
            )
        except (KeyboardInterrupt, ModelBudgetError):
            raise
        except Exception as exc:  # noqa: BLE001
            # The check is a safeguard, not a gate: when it cannot run (timeout,
            # gateway error, malformed answer) the notes as generated still ship.
            emit(f"{label}: source check could not run ({describe_exception(exc)}) - keeping the notes as generated")
    return notes, last_error


def _kinds(issues) -> str:
    """` (missing_content x2, layout x1)` - kinds and counts, never content."""
    counts: dict[str, int] = {}
    for issue in issues:
        kind = str(getattr(issue, "kind", "") or "issue")
        counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return ""
    listed = ", ".join(f"{kind} x{count}" for kind, count in sorted(counts.items()))
    return f" ({listed})"


def _generate_groups(groups, *, instruction, language, subject_hint, emit, workers, verify_source=False):
    """Every group's result, in lecture order."""
    common = {"instruction": instruction, "language": language,
              "subject_hint": subject_hint, "emit": emit, "verify_source": verify_source}
    edges = (0, len(groups) - 1)

    def review_for(position, group) -> bool:
        short_tail = (len(groups) > 1 and position == len(groups) - 1
                      and group.page_count <= SHORT_TAIL_PAGES)
        return not short_tail

    if workers <= 1 or len(groups) <= 1:
        results = []
        for position, group in enumerate(groups):
            if position:
                time.sleep(GROUP_PAUSE_SECONDS)   # do not machine-gun the gateway
            results.append(_generate_group(group, edge=position in edges,
                                           review=review_for(position, group), **common))
        return results

    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for position, group in enumerate(groups):
            if position:
                # Stagger the *starts*: several calls may be in flight, but
                # they never leave together.
                time.sleep(GROUP_PAUSE_SECONDS)
            # Carry task identity and its shared cost list into generation and
            # source-review calls made by each worker.
            futures.append(pool.submit(copy_context().run, _generate_group, group,
                                       edge=position in edges,
                                       review=review_for(position, group), **common))
        return [future.result() for future in futures]     # lecture order


def generate_complete_notes(
    pdf_bytes: bytes,
    *,
    language: str,
    subject_hint: str,
    group_size: int | None = None,
    log=None,
    verify_source: bool = False,
):
    """Generate notes for the whole lecture, group by group, and merge them."""
    from notesbot.gemini_client import _merge, generate_short_notes

    size = group_size or pages_per_call()
    groups = split_pages(pdf_bytes, size)
    from notesbot.sanskrit import resolve_language
    language = resolve_language(language or "auto", subject_hint, pdf_bytes)
    if (language or "auto").strip().lower() in ("", "auto"):
        detected = detect_source_language(pdf_bytes)
        if detected:
            language = detected
            (log or (lambda _m: None))(
                "source looks like a Hindi lecture - generating in Hindi" if detected == "hindi"
                else f"source language detected: {detected}"
            )
    instruction = language_instruction(language)
    emit = log or (lambda _message: None)
    emit(f"splitting the lecture into {len(groups)} page group(s) of up to {size} page(s)")

    workers = group_workers()
    if workers > 1 and len(groups) > 1:
        emit(f"generating up to {workers} page group(s) at a time")
    if verify_source and not source_review_enabled():
        emit("per-group source review is off - notes are not re-checked against the slides")
    # With verify_source this module owns the checking (its own source review
    # when that is on, the duplicate pass and the final quality check), so the
    # generator's inner, unlogged review of question text stays switched off.
    with inline_source_check(not verify_source):
        results = _generate_groups(
            groups, instruction=instruction, language=language,
            subject_hint=subject_hint, emit=emit, workers=workers, verify_source=verify_source,
        )

    parts = []
    for group, (notes, last_error) in zip(groups, results):
        label = f"pages {group.first_page}-{group.last_page}"
        if notes is None:
            # Nothing came back for these pages. A cover, an index or a
            # question-only stretch has nothing for concise notes; a group
            # that never answered is logged and skipped too. The lecture
            # ships from whatever the other pages produced.
            if _content_verdict(last_error):
                emit(f"{label}: no study content - skipped")
            else:
                reason = describe_exception(last_error) if last_error is not None else "no answer"
                emit(f"{label}: skipped after {GROUP_ATTEMPTS} attempts ({reason})")
            continue

        sections = len(getattr(notes, "sections", None) or [])
        points = sum(len(getattr(s, "points", None) or []) for s in (getattr(notes, "sections", None) or []))
        emit(f"{label}: {sections} section(s), {points} point(s)")
        parts.append(notes)

    if not any(getattr(part, "sections", None) for part in parts):
        # Nothing anywhere: there is no note to render, so say so honestly.
        errors = [error for _notes, error in results if error is not None]
        if errors:
            raise errors[-1]
        from notesbot.errors import UserError

        raise UserError("Lecture se notes nahi ban paaye. Please dobara try karein.")

    merged = _merge(parts)
    merged.output_language = language
    if language == "english":
        # Repairs and the source check may reintroduce a gloss; strip again.
        strip_hindi_glosses(merged)
    before = len(getattr(merged, "sections", None) or [])
    merged.sections = dedupe_sections(merged.sections)
    after = len(merged.sections)
    if after != before:
        emit(f"merged {before - after} duplicate topic(s) from overlapping page groups")
    if verify_source:
        from notesbot.source_coverage import consolidate_lecture
        emit("checking repeated information across the whole lecture")
        try:
            merged = consolidate_lecture(pdf_bytes, merged, language=language, subject_hint=subject_hint, log=emit)
        except (KeyboardInterrupt, ModelBudgetError):
            raise
        except Exception as exc:  # noqa: BLE001 - a tidy-up must not cost the lecture
            emit(f"whole-lecture cleanup could not run ({describe_exception(exc)}) - keeping the merged notes")
        after = len(merged.sections)
    if language == "english":
        strip_hindi_glosses(merged)
    emit(f"merged: {after} section(s) across {len(groups)} call(s)")
    return merged
