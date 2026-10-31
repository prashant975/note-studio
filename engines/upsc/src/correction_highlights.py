"""Turn QC corrections into the yellow spans the DCN writer shades.

This module deliberately detects NOTHING. The pipeline already has one error
detector — the QC engine (src/qc) plus the model's Error Report — and a second
opinion here would produce highlights the Error Report cannot explain, which is
the opposite of what the highlight is for. Everything below is a translation:

    QC decided a fact was wrong
        -> the correction applier spliced the fix into the notes
            -> AppliedCorrection records the exact original/replacement pair
                -> this module narrows that pair to the words that changed
                    -> the writer shades those words light yellow

So a reviewer reading the notes can see WHERE a correction landed, and the
Error Report next to it says WHAT was wrong. Neither replaces the other.

Three rules the rest of the file exists to enforce:

* **Only meaningful corrections.** Grammar, formatting, compliance and
  house-style edits are not highlighted (spec §11) — the QC category decides,
  and an unknown category fails closed.
* **Only the changed portion.** ``narrow_change`` trims the words the two
  versions share, so a one-word fix in a three-line paragraph highlights one
  word (spec §12).
* **Only text that is really there.** Every candidate is checked against the
  FINAL notes before it is returned. A correction that a later pass reworded is
  dropped rather than approximated — a highlight in the wrong place is worse
  than a missing one.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

#: QC categories (src/qc/models.QCIssue.category) whose corrections earn a
#: highlight. Deliberately a small allow-list, not a deny-list: an unrecognised
#: category is not highlighted, so a category added later cannot start painting
#: the notes yellow before anyone has decided it should.
HIGHLIGHT_CATEGORIES = frozenset({
    "factual", "conceptual", "terminology", "numerical", "legal", "sequence",
})

#: The ones that exist today and explicitly do NOT earn a highlight. Kept as its
#: own name so the intent is greppable — "language" is Gemini fixing grammar,
#: which spec §11 says must never become yellow.
NON_HIGHLIGHT_CATEGORIES = frozenset({"language", "formatting", "compliance", "style"})

#: Error Report "Error type" values that are not content errors. Used only by
#: the (secondary) error-report path below.
NON_HIGHLIGHT_ERROR_TYPES = frozenset({
    "no issue", "report parsing", "language clarity",
    # Rewording a sentence is not correcting a fact. Yellow means "the substance
    # of this changed"; a translation or phrasing fix would dilute that.
    "language/translation error",
})

#: A span shorter than this is not highlighted on its own. Two or three
#: characters carry no context: shading them looks like a rendering fault, and
#: matching them against the notes finds the wrong occurrence about as often as
#: the right one. A short span is padded with neighbouring words rather than
#: discarded — see _pad_with_context.
MIN_SPAN_CHARS = 4

#: Beyond this a "narrowed" span is not narrow — the correction rewrote the
#: sentence rather than fixing a phrase in it, and the whole thing is the change.
MAX_SPAN_CHARS = 400

_WORD_EDGE_RE = re.compile(r"[\wऀ-ॿ]", re.UNICODE)

#: Symbols that belong to the number they touch rather than to the sentence.
#: Shading "74.04" and leaving the "%" outside it reads as a printing fault, so
#: these are absorbed however long the span already is. Sentence punctuation
#: (. , ; : ! ?) deliberately is NOT here: a highlight that swallows the full
#: stop after it looks like the correction ran on into the next sentence.
_ATTACHED_SUFFIX = "%‰°′″"
_ATTACHED_PREFIX = "₹$€£"


@dataclass(frozen=True)
class Highlight:
    """One stretch of the final notes to shade, and why.

    ``text`` is a literal substring of the notes — not a pattern, not a
    paraphrase. ``error_id`` ties the shading back to the row in the QC report
    that explains it.
    """

    text: str
    error_id: str = ""
    error_type: str = ""
    severity: str = ""
    source: str = "qc"          # qc | error_report

    def as_dict(self) -> dict:
        """The audit shape spec §14 asks for, written to the run folder."""
        return {
            "error_id": self.error_id,
            "highlighted_text": self.text,
            "error_type": self.error_type,
            "severity": self.severity,
            "source": self.source,
            "highlight": True,
        }


def is_highlightable_category(category: str) -> bool:
    """True when a QC category means "a fact/concept was wrong", not "prose was clumsy"."""
    key = (category or "").strip().lower()
    if not key or key in NON_HIGHLIGHT_CATEGORIES:
        return False
    return key in HIGHLIGHT_CATEGORIES


def _expand_to_word_edges(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow [start, end) outwards until both ends sit on a word boundary.

    Without this a "1948 -> 1949" fix highlights the bare digit ``9``, which
    reads as a printing fault. With it, the whole year is shaded.

    Only splits *inside* a word are repaired: a boundary that already sits next
    to a space or a punctuation mark is where a word ends, so it stays put. The
    bounds tests are on both sides on purpose — an empty range at the very end
    of the string would otherwise index past it.
    """
    while (start > 0 and start < len(text)
           and _WORD_EDGE_RE.match(text[start - 1]) and _WORD_EDGE_RE.match(text[start])):
        start -= 1
    while (0 < end < len(text)
           and _WORD_EDGE_RE.match(text[end - 1]) and _WORD_EDGE_RE.match(text[end])):
        end += 1
    return start, end


def _absorb_attached_symbols(text: str, start: int, end: int) -> tuple[int, int]:
    """Pull a unit or currency symbol into the span it belongs to.

    "64%" -> "74.04%" narrows to "74.04", because '%' is not a word character
    and the word-edge pass stops there — correctly, since that IS where the word
    ends. But a reader sees one figure, and shading the digits without the sign
    looks like the highlight was clipped.
    """
    while end < len(text) and text[end] in _ATTACHED_SUFFIX:
        end += 1
    while start > 0 and text[start - 1] in _ATTACHED_PREFIX:
        start -= 1
    return start, end


def _snap_to_token_edges(text: str, start: int, end: int) -> tuple[int, int]:
    """Extend [start, end) to the nearest whitespace on each side.

    _expand_to_word_edges only repairs a split *inside* a word, so it correctly
    leaves "6" alone in "the rate is 6% today" — the '%' is a word boundary. But
    "6%" is one thing a reader sees, and shading the digit without its unit is
    the same kind of printing fault. This snaps to what the eye reads as a token.

    Used only when the narrowed span is too short to place; a span that is
    already long enough keeps its tighter word-level bounds, so "adopted in
    1948" -> "1949" still shades the year and not the full stop after it.
    """
    while start > 0 and text[start - 1] != " ":
        start -= 1
    while end < len(text) and text[end] != " ":
        end += 1
    return start, end


def _pad_with_context(text: str, start: int, end: int, minimum: int) -> tuple[int, int]:
    """Widen [start, end) by whole neighbouring words until it is long enough.

    Why this exists: a one-character numeric fix ("the rate is 5% today" ->
    "6%") produces a span far too short to place, because a boundary already
    touching a space is where a word ends. Without padding, such a span falls
    below MIN_SPAN_CHARS and the fallback shades the WHOLE replacement — turning
    the commonest numeric correction into a yellow sentence, which is exactly
    what spec §12 exists to prevent.

    Words are taken whole, alternating left and right so the change stays near
    the middle of what is shaded, and only until the span is long enough to be
    placed. Each step crosses the gap and then the word beyond it, so a side
    that can move always does — the loop cannot spin.
    """
    take_left = True
    while end - start < minimum:
        can_left, can_right = start > 0, end < len(text)
        if not (can_left or can_right):
            break
        if can_left and (take_left or not can_right):
            cursor = start
            while cursor > 0 and text[cursor - 1] == " ":
                cursor -= 1
            while cursor > 0 and text[cursor - 1] != " ":
                cursor -= 1
            start = cursor
        else:
            cursor = end
            while cursor < len(text) and text[cursor] == " ":
                cursor += 1
            while cursor < len(text) and text[cursor] != " ":
                cursor += 1
            end = cursor
        take_left = not take_left
    return start, end


def narrow_change(original: str, corrected: str) -> str:
    """The part of ``corrected`` that ``original`` does not already say.

    Trims the shared head and the shared tail, then grows the remainder back out
    to whole words. Spec §12: highlighting the whole paragraph when one phrase
    changed tells a reviewer nothing.

    Returns ``corrected`` unchanged when the two share nothing worth trimming,
    and ``""`` when the correction only DELETED text (there is no new wording to
    point at, and the QC report already records the removal).
    """
    original = original or ""
    corrected = (corrected or "").strip()
    if not corrected:
        return ""
    if not original or original == corrected:
        return corrected

    head = 0
    limit = min(len(original), len(corrected))
    while head < limit and original[head] == corrected[head]:
        head += 1
    tail = 0
    while (tail < limit - head
           and original[len(original) - 1 - tail] == corrected[len(corrected) - 1 - tail]):
        tail += 1

    start, end = head, len(corrected) - tail
    if start >= end:
        return ""                     # pure deletion: nothing new to shade
    start, end = _expand_to_word_edges(corrected, start, end)
    start, end = _absorb_attached_symbols(corrected, start, end)
    span = corrected[start:end].strip()
    if len(span) < MIN_SPAN_CHARS:
        # Too little context to place safely. Complete the token the change sits
        # in, then take neighbouring WORDS — rather than giving up and shading
        # the whole replacement.
        start, end = _snap_to_token_edges(corrected, start, end)
        if len(corrected[start:end].strip()) < MIN_SPAN_CHARS:
            start, end = _pad_with_context(corrected, start, end, MIN_SPAN_CHARS)
        span = corrected[start:end].strip()
    if len(span) < MIN_SPAN_CHARS:
        # The replacement itself is barely longer than the change. It is still a
        # truthful highlight — it is just a wider one.
        return corrected
    return span


def _locatable(span: str, notes_text: str) -> bool:
    """True when ``span`` occurs in the notes EXACTLY once.

    Uniqueness, not mere presence. The writer shades every occurrence of a span
    it is given, so a span that appears twice would paint an occurrence QC never
    looked at — and "no yellow here means QC changed nothing here" is half of
    what the colour promises a reviewer. A common token ("1949", "India") can
    easily occur twice; when it does, the caller widens the span or drops it.
    """
    if not span or len(span) < MIN_SPAN_CHARS or len(span) > MAX_SPAN_CHARS:
        return False
    return notes_text.count(span) == 1


def _placeable(original: str, corrected: str, notes_text: str) -> str:
    """The narrowest span for this edit that sits in exactly one place, or "".

    Tries the narrowed change first, then the whole replacement — a phrase that
    repeats is often unique once its surrounding sentence is included. If
    neither is unique the correction gets no highlight at all: the QC report
    still records it, and a reviewer losing a colour cue costs less than a
    reviewer being pointed at text nobody changed.
    """
    narrowed = narrow_change(original, corrected)
    if _locatable(narrowed, notes_text):
        return narrowed
    whole = (corrected or "").strip()
    if narrowed and whole != narrowed and _locatable(whole, notes_text):
        return whole
    return ""


def highlights_from_qc(applied, notes_text: str) -> list[Highlight]:
    """Highlights for the corrections QC actually spliced into the notes.

    ``applied`` is the ``AppliedCorrection`` list from QCRunResult. Only records
    that report ``applied`` are considered — a correction the applier could not
    place is in the report as a failure and must not be painted anywhere.
    """
    out: list[Highlight] = []
    for record in applied or []:
        if not getattr(record, "applied", False):
            continue
        if not getattr(record, "highlight", True):
            continue                  # explicitly suppressed upstream
        if not is_highlightable_category(getattr(record, "category", "")):
            continue
        span = _placeable(getattr(record, "original_text", ""),
                          getattr(record, "replacement_text", ""), notes_text)
        if not span:
            continue
        out.append(Highlight(
            text=span,
            error_id=getattr(record, "issue_id", "") or getattr(record, "correction_id", ""),
            error_type=getattr(record, "category", ""),
            severity="",
            source="qc",
        ))
    return out


def highlights_from_error_rows(rows, notes_text: str) -> list[Highlight]:
    """Highlights for Error Report rows whose correction reached the notes verbatim.

    Secondary on purpose. The Error Report's "Correct information" column is
    written for a human — "Use the 2011 Census figure, not the 2001 one" — and
    is usually a description of the fix rather than the fixed sentence. When the
    model happens to write the corrected wording itself, and that wording is in
    the notes, shading it costs nothing and gains a reviewer a lookup. When it
    does not, the row is skipped in silence: this path never approximates.
    """
    out: list[Highlight] = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        error_type = str(row.get("error_type") or "").strip()
        if error_type.lower() in NON_HIGHLIGHT_ERROR_TYPES:
            continue
        span = _placeable(str(row.get("original_statement") or ""),
                          str(row.get("correct_information_correction") or ""), notes_text)
        if not span:
            continue
        out.append(Highlight(
            text=span,
            # The sheet numbers its rows by position, so the index is the id.
            error_id=f"ER-{index + 1:03d}",
            error_type=error_type,
            severity=str(row.get("severity") or ""),
            source="error_report",
        ))
    return out


def collect_highlights(notes_text: str, *, applied=None, error_rows=None) -> list[Highlight]:
    """Every span to shade in ``notes_text``, deduplicated, longest first.

    Longest first is what the writer's matcher needs: with a short correction
    nested inside a longer one, matching the long span first keeps the shading
    as one block instead of three.
    """
    if not notes_text:
        return []
    found: list[Highlight] = []
    seen: set[str] = set()
    for highlight in (highlights_from_qc(applied, notes_text)
                      + highlights_from_error_rows(error_rows, notes_text)):
        if highlight.text in seen:
            continue
        seen.add(highlight.text)
        found.append(highlight)
    found.sort(key=lambda h: len(h.text), reverse=True)
    return found


def highlight_spans(highlights) -> list[str]:
    """Just the strings, for ``write_upsc_dcn_docx(highlight_spans=...)``."""
    return [h.text for h in highlights or []]
