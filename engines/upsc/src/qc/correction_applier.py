"""Apply AUTO_CORRECT decisions to the notes text, safely and audibly.

Safety model: an edit happens only when the original text can be located
unambiguously *inside the anchored paragraph* — exactly one occurrence. Zero or
several matches abort that single edit (never the run) with an audit record,
because replacing the wrong spot in student notes is worse than replacing
nothing. A document-wide search is used only when the decision carries no usable
anchor at all; an anchor that exists but does not contain the text is a recorded
failure, not a licence to edit elsewhere. There is deliberately no global
search-and-replace.

"Already applied" is decided positionally, never by asking whether the
replacement string appears somewhere in the document: a note that merely happens
to contain the corrected wording elsewhere must not make an unlocated correction
look successful. An audit record saying "applied" has to mean this exact span
now reads as the replacement.

Two kinds of lines are invisible to the applier and survive verbatim:
"(Note to DTP: ...)" instructions and correction-marker blocks
([Faculty Point Corrected] etc.) — both quote the faculty's original wording
on purpose, so "fixing" them would destroy the audit trail.

Paragraph indexes follow the claim_extractor convention exactly (see
paragraph_spans): paragraphs are runs of non-blank lines, whitespace-only lines
separate them, consecutive blanks do not create empty paragraphs, and \\r\\n is
handled. Everything outside the single replaced span is preserved byte-for-byte.
"""
from __future__ import annotations

import re

from ..correction_highlights import is_highlightable_category
from .models import AUTO_CORRECT, AppliedCorrection, CorrectionDecision

_DTP_NOTE_RE = re.compile(r"\(\s*Note\s+to\s+DTP\s*:", re.I)

# Same marker family quality_checker parses; a marker line starts a protected
# block that runs to the next blank line (blocks never span paragraphs).
_CORRECTION_MARKER_RE = re.compile(
    r"^[\s\-\*•>]*\**\s*\[\s*(?:Faculty\s+Point\s+Corrected|Faculty\s+Point\s+Removed|"
    r"Needs\s+Human\s+Review|Correction\s+Note|Needs\s+Verification|"
    r"सुधार नोट|सत्यापन आवश्यक)\s*\]",
    re.I,
)


def apply_corrections(
    notes_text: str, decisions: list[CorrectionDecision]
) -> tuple[str, list[AppliedCorrection]]:
    """Apply every AUTO_CORRECT decision; return (new text, audit records).

    Non-AUTO decisions are not attempted and produce no record — they belong
    to the human-review report, not the edit log. Decisions are applied in
    order, each against the text as already edited by the previous ones; because
    an edit can shift later anchors, paragraph spans are recomputed per decision.
    """
    text = notes_text
    records: list[AppliedCorrection] = []
    for decision in decisions:
        if decision.action != AUTO_CORRECT:
            continue
        record = AppliedCorrection(
            correction_id=decision.correction_id,
            issue_id=decision.issue_id,
            category=decision.category,
            location=decision.location,
            original_text=decision.original_text,
            replacement_text=decision.replacement_text,
            action=decision.action,
            evidence_ids=list(decision.evidence_ids),
            confidence=decision.confidence,
            # Decided here, once, from the category the policy already assigned:
            # a factual/conceptual fix is shown to the reader in yellow, a
            # grammar or formatting fix is not (spec §11). Recorded on the audit
            # record rather than re-derived at render time so the report and the
            # page can never disagree about which is which.
            highlight=is_highlightable_category(decision.category),
        )
        text = _apply_one(text, decision, record)
        records.append(record)
    return text, records


def _apply_one(text: str, decision: CorrectionDecision, record: AppliedCorrection) -> str:
    original = decision.original_text
    replacement = decision.replacement_text

    # The policy should never emit these; guard anyway so a hand-built
    # decision cannot blank out or mangle notes.
    if not original:
        record.failure_reason = "empty original_text"
        return text
    if not replacement:
        record.failure_reason = "empty replacement_text (removal requires approval)"
        return text
    if original == replacement:
        record.failure_reason = "replacement identical to original"
        return text

    protected = _protected_spans(text)
    anchor = _anchor_paragraph_index(decision.location)
    if anchor is not None:
        bounds = _paragraph_bounds(text, anchor)
        if bounds is None:
            # A stale or foreign anchor (the notes have fewer paragraphs than
            # the decision expects). Searching the whole document instead would
            # be exactly the blind edit this module exists to prevent.
            record.failure_reason = f"anchor paragraph {anchor} does not exist"
            return text
    else:
        bounds = (0, len(text))

    occurrences = _visible_occurrences(text, original, protected, bounds)

    # Idempotency, decided positionally: when a previous run already spliced the
    # replacement in, the only "original" still visible is the substring of that
    # replacement — e.g. "Article 368" inside the corrected "Article 368A".
    # Re-splicing would corrupt the text, so report success without editing.
    # Requires an actual overlap: a replacement string that merely occurs
    # elsewhere in the paragraph must never make an unlocated correction look
    # applied, or the report tells faculty a live error was fixed.
    replacement_hits = _visible_occurrences(text, replacement, protected, bounds)
    if replacement_hits and occurrences and all(
        _covered_by(pos, len(original), replacement_hits, len(replacement))
        for pos in occurrences
    ):
        record.applied = True
        record.already_applied = True
        return text
    if replacement_hits and not occurrences and anchor is not None and _is_edit_of(
        original, replacement
    ):
        # The original is gone and the replacement is here, in the anchored
        # paragraph, and the two texts are near-identical — this is the earlier
        # pass's own edit ("adopted in 1948" -> "adopted in 1949"). The
        # similarity test is what keeps this honest: a replacement that shares
        # no context with the original ("recieve" -> "receive", where the
        # paragraph happened to contain "receive" all along) must NOT certify an
        # unlocated correction as applied, or the report tells faculty a live
        # error was fixed while it still stands in the notes.
        record.applied = True
        record.already_applied = True
        return text

    if len(occurrences) == 1:
        record.applied = True
        return _splice(text, occurrences[0], len(original), replacement)
    if len(occurrences) > 1:
        record.failure_reason = f"ambiguous: {len(occurrences)} occurrences"
        return text

    record.failure_reason = (
        f"text not found in anchored paragraph {anchor}"
        if anchor is not None else "text not found"
    )
    return text


def _covered_by(
    pos: int, length: int, container_starts: list[int], container_length: int
) -> bool:
    """True when [pos, pos+length) sits wholly inside one container span."""
    return any(
        start <= pos and pos + length <= start + container_length
        for start in container_starts
    )


# An "already applied" verdict is only trustworthy when finding the replacement
# text is strong evidence that THIS edit produced it. That needs the replacement
# to carry enough surrounding context to be specific to the edit: a
# multi-word/long span ("adopted in 1948" -> "adopted in 1949") could hardly
# appear by chance, whereas a bare token ("recieve" -> "receive") can easily
# have been in the paragraph all along. Below this length, fail closed and
# report the correction as not found.
_IDEMPOTENCY_MIN_CONTEXT = 12


def _is_edit_of(original: str, replacement: str) -> bool:
    """True when `replacement` is specific enough that finding it in the
    anchored paragraph means this correction already ran."""
    if len(replacement) < _IDEMPOTENCY_MIN_CONTEXT:
        return False
    # Shares a real prefix or suffix with the original, i.e. the edit changed
    # part of a phrase rather than swapping an unrelated string.
    shared = 0
    for a, b in zip(original, replacement):
        if a != b:
            break
        shared += 1
    if shared >= _IDEMPOTENCY_MIN_CONTEXT:
        return True
    tail = 0
    for a, b in zip(reversed(original), reversed(replacement)):
        if a != b:
            break
        tail += 1
    return tail >= _IDEMPOTENCY_MIN_CONTEXT


def _splice(text: str, index: int, length: int, replacement: str) -> str:
    return text[:index] + replacement + text[index + length:]


def _anchor_paragraph_index(location: str) -> int | None:
    """Parse "heading|para:N" back into N; "?" or malformed anchors mean none."""
    marker = "|para:"
    if marker not in (location or ""):
        return None
    tail = location.rsplit(marker, 1)[1].strip()
    return int(tail) if tail.isdigit() else None


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of each paragraph, claim_extractor's convention.

    A paragraph is a run of non-blank lines; whitespace-only lines separate
    paragraphs and never form one, so consecutive blank lines (and \\r\\n line
    endings) do not shift the index. This is the offset-aware twin of
    claim_extractor._split_blocks — the two must agree or an anchor points at
    the wrong paragraph.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    start: int | None = None
    end = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            if start is None:
                start = offset
            # End at the last non-blank character so the span excludes the
            # trailing newline, matching "\n".join(block) content bounds.
            end = offset + len(line.rstrip("\r\n"))
        elif start is not None:
            spans.append((start, end))
            start = None
        offset += len(line)
    if start is not None:
        spans.append((start, end))
    return spans


def _paragraph_bounds(text: str, index: int) -> tuple[int, int] | None:
    """(start, end) offsets of paragraph `index`, or None when out of range."""
    spans = paragraph_spans(text)
    if index < 0 or index >= len(spans):
        return None
    return spans[index]


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of lines the applier must never touch."""
    spans: list[tuple[int, int]] = []
    offset = 0
    in_marker_block = False
    for line in text.splitlines(keepends=True):
        if not line.strip():
            in_marker_block = False
        elif _CORRECTION_MARKER_RE.match(line):
            in_marker_block = True
        if line.strip() and (in_marker_block or _DTP_NOTE_RE.search(line)):
            spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _visible_occurrences(
    text: str,
    needle: str,
    protected: list[tuple[int, int]],
    bounds: tuple[int, int],
) -> list[int]:
    """Start offsets of `needle` fully inside `bounds`, excluding any occurrence
    that touches a protected line.

    Overlapping matches are counted separately (step 1, not len(needle)): "11"
    occurs twice in "111", and that genuine ambiguity must reach the caller
    rather than be collapsed into a single confident edit.
    """
    start, end = bounds
    out: list[int] = []
    i = text.find(needle, start)
    while i != -1 and i + len(needle) <= end:
        touches_protected = any(
            i < p_end and i + len(needle) > p_start for p_start, p_end in protected
        )
        if not touches_protected:
            out.append(i)
        i = text.find(needle, i + 1)
    return out
