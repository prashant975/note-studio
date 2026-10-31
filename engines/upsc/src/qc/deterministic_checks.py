"""Deterministic, no-AI sanity checks on generated notes.

These run independently of the AI review: pure string analysis of the
student-facing notes plus the slide list and the requested quality options.
They exist because a whole class of production defects — leaked pipeline
markers, broken tables, duplicated blocks, DTP notes pointing at slides that
do not exist — is cheaper and more reliable to catch with exact rules than
with a model, and must still be caught when the AI stage is disabled or
unavailable.

Design rules honoured here:
- Never crash the pipeline: every sub-check runs isolated, so an unexpected
  parsing failure in one check yields no findings from that check instead of
  an exception (the remaining checks and the report stages still run).
- No silent caps: the issue list is bounded (MAX_TOTAL_ISSUES) and a capped
  run appends one extra QCIssue saying how many findings were suppressed.
- rule_id reuses config/upsc_qc_rules.yaml ids where a rule genuinely fits
  (FLOW-FORMAT-004 for raw-syntax leaks and broken tables, LANG-CLARITY-003
  for over-long sentences, FACT-NAMES-005 for proper-noun consistency,
  ENRICH-* for enrichment shortfalls); purely mechanical checks with no YAML
  counterpart use DET-<NAME> ids. Severities of reused rules match the YAML.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from .models import QCIssue

try:
    from ..dtp_parser import find_dtp_notes
except ImportError:  # pragma: no cover - dtp_parser ships with the app
    find_dtp_notes = None

# Bounds. The per-kind caps exist because one systemic problem (e.g. a model
# that never ends a sentence) should not drown out every other finding.
# Raised from 60 with the rest of the coverage caps. The PER-KIND caps below
# are the ones doing the useful work — they stop one systemic fault drowning out
# everything else — and they are unchanged. This one only existed to bound the
# total, and at 60 it was truncating the findings for a dense note.
MAX_TOTAL_ISSUES = 250
MAX_LONG_SENTENCE_ISSUES = 5
MAX_NAME_ISSUES = 5
MIN_NOTES_CHARS = 200
MAX_NOTES_CHARS = 400_000
LONG_SENTENCE_WORDS = 60
NEAR_DUP_MIN_CHARS = 80
NEAR_DUP_RATIO = 0.9
EXACT_DUP_MIN_CHARS = 40          # repeated tiny bullets ("Yes.") are not dup content
NAME_RATIO = 0.85
BULLET_NEST_MAX_JUMP = 8
_MAX_NEAR_DUP_CANDIDATES = 150    # bounds the O(n^2) paragraph comparison
_MAX_NAME_FORMS = 400             # bounds the O(n^2) spelling comparison

_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(\S.*?)\s*$")
# A line that is entirely bold acts as a (deepest-level) heading in generated notes.
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*].*?)\*\*[:.]?\s*$")
_BULLET_RE = re.compile(r"^(\s*)(?:[-*•‣▪]|\d{1,3}[.)])\s+\S")
_PLACEHOLDER_BULLET_RE = re.compile(r"^\s*(?:[-*•‣▪]|\d{1,3}[.)])\s*(?:\.\.\.|…)\s*$")
_HTML_LEAK_RE = re.compile(r"</|<div\b|<span\b", re.I)
_FENCE = "```"
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CAP_WORD_RE = re.compile(r"\b[A-Z][A-Za-z]{4,}\b")
_TEXT_DATE_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July"
    r"|August|September|October|November|December)\s+\d{4}\b",
    re.I,
)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_INTERNAL_MARKERS = ("NOTES_START", "NOTES_END", "REPORT_START", "REPORT_END")
_TRANSCRIPT_GAP = "[transcript unavailable"
_CORRECTION_MARKER_RE = re.compile(
    r"\[(?:Faculty Point Corrected|Faculty Point Removed|Needs Human Review)\]", re.I
)
_PLACEHOLDER_LINE_RES = (
    re.compile(r"\bTODO\b"),
    re.compile(r"\bXXX\b"),
    re.compile(r"lorem\s+ipsum", re.I),
    re.compile(r"<placeholder", re.I),
)


def _clip(text: object, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _issue(
    rule_id: str,
    category: str,
    severity: str,
    problem: str,
    *,
    heading: str = "",
    paragraph_index: int | None = None,
    original_text: str = "",
) -> QCIssue:
    """Deterministic findings are certain by construction, hence confidence 1.0.

    issue_id is assigned once, sequentially, in run_deterministic_checks so
    ids stay stable and unique regardless of which checks fired.
    """
    return QCIssue(
        issue_id="",
        category=category,
        rule_id=rule_id,
        severity=severity,
        problem=problem,
        heading=heading,
        paragraph_index=paragraph_index,
        original_text=_clip(original_text),
        confidence=1.0,
        source="deterministic",
    )


def _heading_of(line: str) -> tuple[int, str] | None:
    """(level, text) when the line is a heading, else None."""
    m = _MD_HEADING_RE.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    m = _BOLD_HEADING_RE.match(line)
    if m:
        return 7, m.group(1).strip()
    return None


def _blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Blank-line separated blocks as (start_line_index, lines)."""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip():
            if not current:
                start = i
            current.append(line)
        elif current:
            blocks.append((start, current))
            current = []
    if current:
        blocks.append((start, current))
    return blocks


# ---------------------------------------------------------------- checks


def _check_length(text: str) -> list[QCIssue]:
    stripped = len(text.strip())
    if stripped < MIN_NOTES_CHARS:
        return [_issue(
            "DET-LENGTH", "conceptual", "high",
            f"Notes are only {stripped} characters — empty or near-empty output; "
            "the generation stage almost certainly failed.",
            original_text=text,
        )]
    if len(text) > MAX_NOTES_CHARS:
        return [_issue(
            "DET-LENGTH", "conceptual", "medium",
            f"Notes are {len(text)} characters, beyond the {MAX_NOTES_CHARS} sanity "
            "limit — likely runaway or duplicated generation.",
        )]
    return []


def _check_internal_markers(lines: list[str]) -> list[QCIssue]:
    """Pipeline-internal strings that must never reach a student."""
    issues: list[QCIssue] = []
    for marker in _INTERNAL_MARKERS:
        hits = [i for i, ln in enumerate(lines) if marker in ln]
        if hits:
            issues.append(_issue(
                "DET-INTERNAL-MARKER", "formatting", "high",
                f"Internal pipeline marker {marker!r} is visible in the notes "
                f"({len(hits)} line(s), first at line {hits[0] + 1}).",
                original_text=lines[hits[0]],
            ))
    gap_hits = [i for i, ln in enumerate(lines) if _TRANSCRIPT_GAP in ln.lower()]
    if gap_hits:
        issues.append(_issue(
            "DET-INTERNAL-MARKER", "formatting", "high",
            f"Transcript-gap marker left visible to students ({len(gap_hits)} "
            f"line(s), first at line {gap_hits[0] + 1}).",
            original_text=lines[gap_hits[0]],
        ))
    # Two correction markers on consecutive content lines means a block was
    # duplicated during merge — a single marker is legitimate by design.
    run = 0
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if _CORRECTION_MARKER_RE.search(ln):
            run += 1
            if run == 2:
                issues.append(_issue(
                    "DET-REPEAT-MARKER", "formatting", "medium",
                    f"Correction markers appear on consecutive lines (line {i + 1}) "
                    "— likely a duplicated correction block.",
                    original_text=ln,
                ))
        else:
            run = 0
    return issues


def _check_markup_leaks(lines: list[str]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    html_hits = [i for i, ln in enumerate(lines) if _HTML_LEAK_RE.search(ln)]
    if html_hits:
        issues.append(_issue(
            "FLOW-FORMAT-004", "formatting", "medium",
            f"Raw HTML markup leaked into the notes on {len(html_hits)} line(s) "
            f"(first at line {html_hits[0] + 1}).",
            original_text=lines[html_hits[0]],
        ))
    fence_hits = [i for i, ln in enumerate(lines) if _FENCE in ln]
    if fence_hits:
        issues.append(_issue(
            "FLOW-FORMAT-004", "formatting", "medium",
            f"Markdown fence backticks (```) left in the text on {len(fence_hits)} "
            f"line(s) (first at line {fence_hits[0] + 1}).",
            original_text=lines[fence_hits[0]],
        ))
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("{"):
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "}":
                    issues.append(_issue(
                        "FLOW-FORMAT-004", "formatting", "medium",
                        f"Raw JSON-style brace block leaked into the notes "
                        f"(lines {i + 1}-{j + 1}).",
                        original_text=lines[i],
                    ))
                    i = j
                    break
        i += 1
    return issues


def _cell_count(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def _check_tables(lines: list[str]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        start = i
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            i += 1
        rows = lines[start:i]
        if len(rows) == 1:
            issues.append(_issue(
                "FLOW-FORMAT-004", "formatting", "medium",
                f"Unclosed table: a lone table row at line {start + 1} has no "
                "header or body.",
                original_text=rows[0],
            ))
            continue
        header_cells = _cell_count(rows[0])
        for offset, row in enumerate(rows[1:], start=1):
            cells = _cell_count(row)
            if abs(cells - header_cells) > 1:
                issues.append(_issue(
                    "FLOW-FORMAT-004", "formatting", "medium",
                    f"Malformed table at line {start + 1}: row at line "
                    f"{start + offset + 1} has {cells} cell(s) but the header "
                    f"has {header_cells}.",
                    original_text=row,
                ))
                break  # one finding per table block; fixing it re-runs QC anyway
    return issues


def _check_slide_references(text: str, slides: list | None) -> list[QCIssue]:
    """DTP notes must point at slides that exist and have an exported image.

    slides=None means the caller has no slide information at all, so nothing
    can be validated; an empty list means "this deck has no slides" and every
    slide reference is therefore invalid.
    """
    if slides is None or find_dtp_notes is None:
        return []
    by_no: dict[int, object] = {}
    for slide in slides:
        no = getattr(slide, "slide_no", None)
        if no is None:
            continue
        try:
            by_no[int(no)] = slide
        except (TypeError, ValueError):
            continue
    issues: list[QCIssue] = []
    flagged_unknown: set[int] = set()
    flagged_image: set[int] = set()
    for note in find_dtp_notes(text):
        if note.slide_no is None:
            continue
        if note.slide_no not in by_no:
            if note.slide_no not in flagged_unknown:
                flagged_unknown.add(note.slide_no)
                issues.append(_issue(
                    "DET-SLIDE-REF", "formatting", "high",
                    f"DTP note references slide {note.slide_no}, which does not "
                    "exist in this deck — the image can never be placed.",
                    heading=note.heading or "",
                    original_text=note.raw,
                ))
            continue
        image = getattr(by_no[note.slide_no], "image_path", None)
        missing = image is None
        if not missing:
            try:
                missing = not Path(str(image)).exists()
            except OSError:
                missing = True
        if missing and note.slide_no not in flagged_image:
            flagged_image.add(note.slide_no)
            issues.append(_issue(
                "DET-SLIDE-IMAGE", "formatting", "medium",
                f"DTP note references slide {note.slide_no}, but that slide has "
                "no exported image file — DTP cannot place it.",
                heading=note.heading or "",
                original_text=note.raw,
            ))
    return issues


def _check_headings(lines: list[str]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    headings: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        h = _heading_of(line)
        if h:
            headings.append((i, h[0], h[1]))

    occurrences: dict[str, list[str]] = {}
    for _, _, text in headings:
        norm = " ".join(text.split()).casefold().rstrip(" :.-")
        if norm:
            occurrences.setdefault(norm, []).append(text)
    for texts in occurrences.values():
        if len(texts) >= 2:
            issues.append(_issue(
                "DET-DUP-HEADING", "conceptual", "medium",
                f"Heading {texts[0]!r} appears {len(texts)} times — sections were "
                "likely duplicated or split incorrectly.",
                heading=texts[0],
            ))

    # A heading is empty when nothing but blank lines separates it from the
    # next heading at the same or a shallower level, or from the end of the
    # document. A deeper heading counts as content: a chapter title followed
    # by its section headings is normal structure, not an empty section.
    for line_no, level, text in headings:
        empty = True
        for j in range(line_no + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip():
                continue
            h = _heading_of(nxt)
            empty = h is not None and h[0] <= level
            break
        if empty:
            issues.append(_issue(
                "DET-EMPTY-HEADING", "conceptual", "medium",
                f"Heading {text!r} (line {line_no + 1}) has no content under it.",
                heading=text,
            ))
    return issues


def _check_paragraphs(blocks: list[tuple[int, list[str]]]) -> list[QCIssue]:
    """Exact and near-duplicate paragraph detection.

    Comparison happens on whitespace/case-normalised block text so wrapping
    differences do not hide duplication. Near-duplicates only apply to blocks
    over NEAR_DUP_MIN_CHARS — short bullets legitimately resemble each other.
    """
    paras: list[tuple[int, str, list[str]]] = []
    for b_idx, (_start, blines) in enumerate(blocks):
        first = blines[0]
        if first.lstrip().startswith("|"):
            continue  # tables are checked structurally, not as prose
        if len(blines) == 1 and _heading_of(first) is not None:
            continue
        norm = " ".join(" ".join(blines).split()).casefold()
        paras.append((b_idx, norm, blines))

    issues: list[QCIssue] = []
    first_seen: dict[str, int] = {}
    for b_idx, norm, blines in paras:
        if len(norm) < EXACT_DUP_MIN_CHARS:
            continue
        if norm in first_seen:
            issues.append(_issue(
                "DET-DUP-PARA", "conceptual", "medium",
                f"Paragraph {b_idx} repeats paragraph {first_seen[norm]} verbatim.",
                paragraph_index=b_idx,
                original_text=blines[0],
            ))
        else:
            first_seen[norm] = b_idx

    candidates = [(b, n) for b, n, _ in paras if len(n) > NEAR_DUP_MIN_CHARS]
    candidates = candidates[:_MAX_NEAR_DUP_CANDIDATES]
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            ia, a = candidates[i]
            ib, b = candidates[j]
            if a == b:
                continue  # exact duplicate, already reported above
            la, lb = len(a), len(b)
            if 2 * min(la, lb) / (la + lb) <= NEAR_DUP_RATIO:
                continue  # lengths alone rule out the similarity threshold
            sm = SequenceMatcher(None, a, b)
            if sm.real_quick_ratio() <= NEAR_DUP_RATIO or sm.quick_ratio() <= NEAR_DUP_RATIO:
                continue
            if sm.ratio() > NEAR_DUP_RATIO:
                issues.append(_issue(
                    "DET-NEARDUP-PARA", "conceptual", "low",
                    f"Paragraph {ib} is a near-duplicate of paragraph {ia} "
                    f"(over {int(NEAR_DUP_RATIO * 100)}% similar).",
                    paragraph_index=ib,
                    original_text=a,
                ))
    return issues


def _check_placeholders(lines: list[str]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    for i, line in enumerate(lines):
        if _PLACEHOLDER_BULLET_RE.match(line):
            issues.append(_issue(
                "DET-PLACEHOLDER", "formatting", "high",
                f"Bullet at line {i + 1} is only an ellipsis — unfinished content.",
                original_text=line,
            ))
            continue
        for rx in _PLACEHOLDER_LINE_RES:
            if rx.search(line):
                issues.append(_issue(
                    "DET-PLACEHOLDER", "formatting", "high",
                    f"Placeholder text left in the notes at line {i + 1}.",
                    original_text=line,
                ))
                break
    return issues


def _check_bullet_nesting(lines: list[str]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    prev_indent: int | None = None
    for i, line in enumerate(lines):
        if not line.strip():
            continue  # blank lines inside a list do not reset nesting context
        m = _BULLET_RE.match(line.expandtabs(4))
        if not m:
            prev_indent = None
            continue
        indent = len(m.group(1))
        if prev_indent is not None and indent - prev_indent > BULLET_NEST_MAX_JUMP:
            issues.append(_issue(
                "DET-BULLET-NEST", "formatting", "low",
                f"Bullet at line {i + 1} jumps {indent - prev_indent} spaces deeper "
                "than the previous bullet — broken nesting will render wrongly.",
                original_text=line,
            ))
        prev_indent = indent
    return issues


def _check_long_sentences(blocks: list[tuple[int, list[str]]]) -> list[QCIssue]:
    issues: list[QCIssue] = []
    for b_idx, (_start, blines) in enumerate(blocks):
        first = blines[0]
        if first.lstrip().startswith("|"):
            continue
        if len(blines) == 1 and _heading_of(first) is not None:
            continue
        # Bullet lines are independent units: joining an entire bullet list
        # into one string would fake a giant "sentence" out of short items.
        if _BULLET_RE.match(first.expandtabs(4)):
            pieces = [ln.strip() for ln in blines]
        else:
            pieces = [" ".join(ln.strip() for ln in blines)]
        for piece in pieces:
            for sentence in _SENTENCE_SPLIT_RE.split(piece):
                words = sentence.split()
                if len(words) > LONG_SENTENCE_WORDS:
                    issues.append(_issue(
                        "LANG-CLARITY-003", "language", "medium",
                        f"Sentence with {len(words)} words (limit "
                        f"{LONG_SENTENCE_WORDS}) — split it for readability.",
                        paragraph_index=b_idx,
                        original_text=sentence,
                    ))
                    if len(issues) >= MAX_LONG_SENTENCE_ISSUES:
                        return issues
    return issues


def _check_date_formats(text: str) -> list[QCIssue]:
    textual = _TEXT_DATE_RE.search(text)
    numeric = _NUMERIC_DATE_RE.search(text)
    if textual and numeric:
        return [_issue(
            "DET-DATE-FORMAT", "language", "low",
            f"Mixed date styles in the same notes: {textual.group(0)!r} and "
            f"{numeric.group(0)!r} — standardise on one format.",
            original_text=f"{textual.group(0)} / {numeric.group(0)}",
        )]
    return []


def _check_name_consistency(text: str) -> list[QCIssue]:
    """Same proper noun spelled two ways (case variants or one-edit typos).

    Simple plural pairs (Mughal/Mughals) are skipped: adding -s/-es is normal
    English, not a spelling inconsistency, and would dominate the findings.
    """
    counts = Counter(_CAP_WORD_RE.findall(text))
    forms = sorted(w for w, c in counts.items() if c >= 2)[:_MAX_NAME_FORMS]
    issues: list[QCIssue] = []
    for i in range(len(forms)):
        for j in range(i + 1, len(forms)):
            a, b = forms[i], forms[j]
            al, bl = a.casefold(), b.casefold()
            if al != bl:
                if abs(len(al) - len(bl)) > 2:
                    continue
                shorter, longer = sorted((al, bl), key=len)
                if longer in (shorter + "s", shorter + "es"):
                    continue
                if SequenceMatcher(None, al, bl).ratio() <= NAME_RATIO:
                    continue
            issues.append(_issue(
                "FACT-NAMES-005", "language", "medium",
                f"Likely inconsistent spelling of the same name: {a!r} "
                f"(x{counts[a]}) vs {b!r} (x{counts[b]}).",
                original_text=f"{a} / {b}",
            ))
            if len(issues) >= MAX_NAME_ISSUES:
                return issues
    return issues


def _check_enrichment(text: str, enrichments: dict | None) -> list[QCIssue]:
    """Requested enrichments that visibly did not happen.

    Spec §6: these are requirement-compliance findings, never factual errors.
    Only the four options with an ENRICH-* rule in the YAML are checked; the
    remaining add_* options have no reliable textual signature to test for.
    """
    if not enrichments:
        return []
    issues: list[QCIssue] = []
    lower = text.casefold()
    if enrichments.get("add_tables") and not re.search(r"^\s*\|.+\|\s*$", text, re.M):
        issues.append(_issue(
            "ENRICH-TABLES-001", "compliance", "low",
            "Tables were requested (add_tables) but the notes contain no table.",
        ))
    has_flow = ("→" in text or re.search(r"\S\s*-{1,2}>\s*\S", text)
                or "flowchart" in lower)
    if enrichments.get("add_flowcharts") and not has_flow:
        issues.append(_issue(
            "ENRICH-FLOW-002", "compliance", "low",
            "Flowcharts were requested (add_flowcharts) but no flow or arrow "
            "structure appears in the notes.",
        ))
    if enrichments.get("add_prelims_keywords") and not re.search(r"\bprelims\b", lower):
        issues.append(_issue(
            "ENRICH-PRELIMS-004", "compliance", "low",
            "Prelims keywords were requested (add_prelims_keywords) but no "
            "Prelims marker appears in the notes.",
        ))
    if enrichments.get("add_mains_value") and not re.search(r"\bmains\b", lower):
        issues.append(_issue(
            "ENRICH-MAINS-003", "compliance", "low",
            "Mains value-adds were requested (add_mains_value) but no Mains "
            "marker appears in the notes.",
        ))
    return issues


# ---------------------------------------------------------------- entry point


def run_deterministic_checks(
    notes_text: str,
    slides: list | None = None,
    enrichments: dict | None = None,
) -> list[QCIssue]:
    """Run every local check and return the (bounded) list of findings.

    Checks run in severity-of-consequence order so that when the cap bites,
    the surviving findings are the ones a reviewer must see first (leaked
    markers and broken structure before style nits).
    """
    if isinstance(notes_text, str):
        text = notes_text
    else:
        text = "" if notes_text is None else str(notes_text)
    lines = text.splitlines()
    try:
        blocks = _blocks(lines)
    except Exception:
        blocks = []

    checks = [
        lambda: _check_length(text),
        lambda: _check_internal_markers(lines),
        lambda: _check_markup_leaks(lines),
        lambda: _check_tables(lines),
        lambda: _check_slide_references(text, slides),
        lambda: _check_headings(lines),
        lambda: _check_paragraphs(blocks),
        lambda: _check_placeholders(lines),
        lambda: _check_bullet_nesting(lines),
        lambda: _check_long_sentences(blocks),
        lambda: _check_date_formats(text),
        lambda: _check_name_consistency(text),
        lambda: _check_enrichment(text, enrichments),
    ]
    issues: list[QCIssue] = []
    for check in checks:
        try:
            issues.extend(check())
        except Exception:
            # A deterministic check must never take the pipeline down; a check
            # that trips on pathological input contributes no findings and the
            # remaining checks still run.
            continue

    if len(issues) > MAX_TOTAL_ISSUES:
        suppressed = len(issues) - MAX_TOTAL_ISSUES
        issues = issues[:MAX_TOTAL_ISSUES]
        issues.append(_issue(
            "DET-TRUNCATED", "compliance", "low",
            f"Deterministic checks found more problems than reported: "
            f"{suppressed} additional issue(s) were suppressed after the first "
            f"{MAX_TOTAL_ISSUES}. Fix the reported issues and re-run QC.",
        ))
    for n, issue in enumerate(issues, start=1):
        issue.issue_id = f"det-{n:03d}"
    return issues
