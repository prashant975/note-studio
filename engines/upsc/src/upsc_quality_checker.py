"""Quality checks for PW OnlyIAS Daily Class Notes (DCN) marker text.

This is deliberately separate from `src/quality_checker.py` (legacy note types)
and from `src/qc/` (the source-verification engine that calls out to the web).
The DCN format has its own contract — a marker vocabulary, an embedded
correction style and a 5.5 in content column — so it needs its own checks:

* Corrections are SILENT: the model writes the correct fact in place and logs
  what changed to the error report, never to the notes. The correction-phrase
  count here is therefore a leak detector — a clean note counts zero, and the
  hard ban lives in src/correction_language_guard.py.
* The student DOCX must never carry this report. Everything here is rendered to
  `quality_report.md` in the run folder and read by the reviewer, not the
  student (see the DCN contract, §4).
* The writer cannot fix content problems after the fact — a table wider than the
  5.5 in column or a bullet that is a bare fragment has to go back to the model
  — so the checks run on the marker text, before the DOCX is built.

Everything here is deterministic and offline: no API keys, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .table_sanitizer import MAX_ROW_CHARS as _MAX_TABLE_ROW_CHARS
from .title_utils import MAX_UNBREAKABLE_CHARS, is_placeholder, longest_unbreakable_run

# Longest first so "#TABLE_START" is never matched as "#TABLE_S".
_TABLE_START = "#TABLE_START"
_TABLE_END = "#TABLE_END"
_FLOW_START = "#FLOW_START"
_FLOW_END = "#FLOW_END"
_TOPICS_START = "#TOPICS_COVERED_START"
_TOPICS_END = "#TOPICS_COVERED_END"

_TITLE_MARKER = "#LECTURE_TITLE:"
# "Lxx_DCNxx_<Lecture Title>" — the lecture/DCN numbering the PW team files by.
_TITLE_PATTERN_RE = re.compile(r"^L\d{1,3}_DCN\d{1,3}_\S", re.I)
_TOPICS_START_RE = re.compile(r"^\s*#TOPICS_COVERED_START\s*$", re.I | re.M)
_TOPICS_END_RE = re.compile(r"^\s*#TOPICS_COVERED_END\s*$", re.I | re.M)
_HEADING_RE = re.compile(r"^\s*#(H[123]):\s*(.*)$")

_BULLET_RE = re.compile(r"^\s*(?:[-*•◦▪o]\s+|\d+[.)]\s+)(.*)$")

# Correction-ANNOUNCEMENT wording. Corrections are silent now (the guard in
# src/correction_language_guard.py fails the build on the banned phrases), so
# a nonzero count here is a leak indicator for the reviewer, not evidence that
# a review happened. "Important distinction:" / "UPSC caution:" are NOT
# counted — they are legitimate exam warnings, not correction announcements.
_CORRECTION_PHRASES = (
    "therefore, the earlier figure",
    "should not be used",
    "should not be described simply as",
    "should be replaced",
    "corrected",
    "rectified",
)

# Wording that marks a figure the model refreshed to current data. Kept apart
# from the correction phrases because a reviewer treats them differently: a
# correction needs the faculty told, an update only needs the date checked.
_UPDATED_DATA_PHRASES = (
    "important current update:",
    "updated figure",
    "revised figure",
    "current figure",
    "latest available data",
    "most recent data",
    "now stands at",
    "as of ",
)

_NEEDS_VERIFICATION_RE = re.compile(r"\[\s*needs verification\s*:", re.I)

# Claims that rot with time. A bare "currently 220 GW" is unusable a year later.
_DATE_SENSITIVE_RE = re.compile(
    r"\b(currently|at present|presently|as of now|as on date|today|nowadays|"
    r"the latest|latest available|most recent|recently|so far|till date|to date|"
    r"now stands|at the moment|this year|last year)\b",
    re.I,
)
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december"
)
# A year, an FY tag or "as of <month>" all count as an acceptable qualifier.
_DATE_QUALIFIER_RE = re.compile(
    rf"\b(?:19|20)\d{{2}}\b|\bFY\s?\d{{2,4}}\b|\b(?:{_MONTHS})\b", re.I
)

# Rows of "A | B | C" outside a #TABLE block, or a run of "Label: 220 GW" lines,
# both render as ragged paragraphs instead of a table.
# Zero-width so that every separator in "A | B | C" is counted, not just the first.
_PIPE_CELL_RE = re.compile(r"(?<=\S) \| (?=\S)")
_UNIT = r"GW|MW|kW|TW|%|per cent|crore|lakh|billion|million|tonnes|tonne|km|kg|mg"
_LABEL_NUMBER_RE = re.compile(rf"^\s*[^:|]{{1,60}}:\s*[\d][\d,.\s]*\s*(?:{_UNIT})(?![A-Za-z])", re.I)

_MOJIBAKE = ("Â", "â€", "ï¿½")
_SUBSCRIPT_RE = re.compile(r"\b([A-Za-z]{1,3})([₀-₉]+)")
# The structural markers themselves ("#H1:", "#H2:", ...) look like flattened
# subscripts to the symbol check, so they are removed before it runs.
_MARKER_TOKEN_RE = re.compile(r"#(?:LECTURE_TITLE|H[1-6]|TABLE_START|TABLE_END|"
                              r"FLOW_START|FLOW_END|SEPARATOR|FIGURE)\b:?")
_SUBSCRIPT_TO_ASCII = {chr(0x2080 + i): str(i) for i in range(10)}

# Bullet-quality heuristics. A DCN bullet is meant to be a full sentence, so a
# fragment is anything too short, unpunctuated, or with no verb-looking token.
_MIN_BULLET_WORDS = 5
_TERMINAL_PUNCTUATION = (".", "?", "!", "।", ":")
_FRAGMENT_RATIO = 0.30
_MIN_BULLETS_FOR_RATIO = 3
_VERB_HINTS = {
    "is", "are", "was", "were", "be", "been", "being", "am",
    "has", "have", "had", "do", "does", "did", "can", "could", "may", "might",
    "shall", "should", "will", "would", "must", "need", "needs",
    "include", "includes", "provide", "provides", "lead", "leads", "cause",
    "causes", "remain", "remains", "become", "becomes", "make", "makes",
    "give", "gives", "show", "shows", "help", "helps", "refer", "refers",
    "mean", "means", "require", "requires", "increase", "increases",
    "decrease", "decreases", "affect", "affects", "result", "results",
    "occur", "occurs", "contain", "contains", "consist", "consists",
    "form", "forms", "act", "acts", "allow", "allows", "aim", "aims",
    "use", "uses", "set", "sets", "cover", "covers", "apply", "applies",
    "hold", "holds", "carry", "carries", "raise", "raises", "state", "states",
}
# Deliberately permissive: without a POS tagger "-s" cannot be told apart from a
# plural noun, so a plural-only bullet slips through. That is the safe side of the
# trade — length and punctuation already catch real fragments, and a false
# "fragment" warning on a correct sentence ("absorbs", "reaches") is what makes a
# reviewer stop trusting the report.
_VERB_SUFFIX_RE = re.compile(r"^\w{3,}(?:ed|ing|es|s)$")
_TRAILING_MARKER_RE = re.compile(r"\s*\[[^\]]*\]\s*$")

# Layout limits for the measured 5.5 in DCN content column. The row limit is
# imported from table_sanitizer so the check and the repair that acts on it
# can never disagree about what "too wide" means.
_MAX_WORD_CHARS = 40

# How many examples any one warning list may carry before it is summarised.
_MAX_ITEMS = 12


@dataclass
class DcnQualityReport:
    """Reviewer-facing result of :func:`check_dcn_notes`.

    Counts are informational (they say the review happened); the three lists are
    actionable (they say something needs a fix before hand-off).
    """

    corrections: int = 0
    updated_current_data: int = 0
    needs_verification: int = 0
    format_warnings: list[str] = field(default_factory=list)
    factual_risk_points: list[str] = field(default_factory=list)
    layout_warnings: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.format_warnings or self.factual_risk_points or self.layout_warnings)

    def to_markdown(self) -> str:
        """Render the standalone `quality_report.md`.

        Self-contained on purpose: the reviewer opens this file alone, without
        the run metadata or the notes beside it.
        """
        out: list[str] = [
            "# DCN Notes - Quality Report",
            "",
            "_Reviewer copy. This report is generated from the notes text and is "
            "never appended to the student DOCX._",
            "",
            "## 1. Counts",
            "",
            "| Metric | Count |",
            "| --- | --- |",
            f"| Correction announcements found (must be 0 — corrections are silent) | {self.corrections} |",
            f"| Updated current-data statements | {self.updated_current_data} |",
            f"| Points marked [Needs Verification:] | {self.needs_verification} |",
            f"| Format warnings | {len(self.format_warnings)} |",
            f"| Factual risk points | {len(self.factual_risk_points)} |",
            f"| Layout warnings | {len(self.layout_warnings)} |",
            "",
        ]
        out += _section(
            "2. Format warnings",
            self.format_warnings,
            "What to do: fix the marker structure or bullet wording in the notes "
            "text and regenerate the DOCX — the writer cannot repair these.",
            "No format problems found.",
        )
        out += _section(
            "3. Factual risk points",
            self.factual_risk_points,
            "What to do: confirm each figure against an official source and add an "
            "\"as of <month year>\" qualifier, or mark it [Needs Verification: reason].",
            "No unqualified date-sensitive claims found.",
        )
        out += _section(
            "4. Layout warnings",
            self.layout_warnings,
            "What to do: shorten the row or split the column — the DCN content "
            "column is only 5.5 in wide, and anything wider runs into the "
            "\"Space for Notes\" box.",
            "Nothing looks likely to overflow the 5.5 in column.",
        )
        return "\n".join(out)


def _section(heading: str, items: list[str], advice: str, empty: str) -> list[str]:
    lines = [f"## {heading}", "", f"_{advice}_", ""]
    lines += [f"- {i}" for i in items] if items else [empty]
    lines.append("")
    return lines


def check_dcn_notes(notes_text: str, *, min_words: int = 400) -> DcnQualityReport:
    """Run every DCN check over the marker-format notes text.

    Never raises: an empty or badly-formed answer comes back as warnings, so a
    bad model response degrades into a reviewer note rather than a crashed run.
    """
    text = notes_text or ""
    lines = text.splitlines()
    zones = _zone_map(lines)

    report = DcnQualityReport()
    if not text.strip():
        report.format_warnings.append("Notes are empty — nothing was generated.")
        return report

    lowered = text.lower()
    report.corrections = sum(lowered.count(p) for p in _CORRECTION_PHRASES)
    report.updated_current_data = sum(lowered.count(p) for p in _UPDATED_DATA_PHRASES)
    report.needs_verification = len(_NEEDS_VERIFICATION_RE.findall(text))

    report.format_warnings += _check_title(lines)
    report.format_warnings += _check_topics_covered(lines)
    report.format_warnings += _check_closing_sections(text)
    report.format_warnings += _check_marker_balance(lines)
    report.format_warnings += _check_heading_ladder(lines)
    report.format_warnings += _check_bullets(lines, zones)
    report.format_warnings += _check_stray_tables(lines, zones)
    report.format_warnings += _check_symbols(text)
    report.format_warnings += _check_length(lines, min_words)

    report.factual_risk_points += _check_date_sensitive(lines, zones)
    report.layout_warnings += _check_layout(lines, zones)
    return report


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def _zone_map(lines: list[str]) -> list[str]:
    """Label every line "table", "flow" or "body".

    Table/flow bodies legitimately break the prose rules (pipes, bare
    fragments, arrows), so every prose check consults this first.
    """
    zones: list[str] = []
    current = "body"
    for line in lines:
        stripped = line.strip().upper()
        if stripped.startswith(_TABLE_START):
            current = "table"
            zones.append("table")
            continue
        if stripped.startswith(_FLOW_START):
            current = "flow"
            zones.append("flow")
            continue
        if stripped.startswith(_TOPICS_START):
            # The contents list is short phrases by design. Judged as prose it
            # fails the full-sentence rule on every note ever generated.
            current = "topics"
            zones.append("topics")
            continue
        if stripped.startswith((_TABLE_END, _FLOW_END, _TOPICS_END)):
            zones.append(current)
            current = "body"
            continue
        zones.append(current)
    return zones


def _check_marker_balance(lines: list[str]) -> list[str]:
    """Report unclosed or stray #TABLE/#FLOW markers.

    Worth its own check because an unterminated block is not merely a
    formatting slip: _zone_map keeps every following line labelled "table" or
    "flow", which suppresses the prose checks for the rest of the file. Left
    unreported, one missing #TABLE_END would silently blind the whole report.
    """
    warnings: list[str] = []
    open_marker: str | None = None
    open_line = 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip().upper()
        for start, end, label in (
            (_TABLE_START, _TABLE_END, "table"),
            (_FLOW_START, _FLOW_END, "flowchart"),
        ):
            if stripped.startswith(start):
                if open_marker:
                    warnings.append(
                        f"Line {number}: {start} opens while the {open_marker} block from "
                        f"line {open_line} is still open — the earlier block is missing its end marker."
                    )
                open_marker, open_line = label, number
            elif stripped.startswith(end):
                if open_marker != label:
                    warnings.append(
                        f"Line {number}: {end} without a matching {start}."
                    )
                open_marker = None
    if open_marker:
        warnings.append(
            f"The {open_marker} block opened on line {open_line} is never closed — every later "
            "line is treated as part of it, so the prose checks below cover only the text above it."
        )
    return warnings


def _check_title(lines: list[str]) -> list[str]:
    first = next((ln for ln in lines if ln.strip()), "")
    if not first.strip().upper().startswith(_TITLE_MARKER):
        return [f"Notes do not start with '{_TITLE_MARKER}' (first line: {_snippet(first)})."]
    title = first.strip()[len(_TITLE_MARKER):].strip().lstrip("*").strip()
    if not title:
        return [f"'{_TITLE_MARKER}' is present but the title text is empty."]
    warnings: list[str] = []
    # A placeholder is the serious one: it reaches the student's page verbatim.
    if is_placeholder(title):
        warnings.append(
            f"Lecture title is still the placeholder from the prompt: {_snippet(title)}. "
            "Set the Lecture Number, DCN Number and Lecture Title before generating."
        )
    elif not _TITLE_PATTERN_RE.match(title):
        warnings.append(
            f"Lecture title has no L__/DCN__ prefix: {_snippet(title)}. "
            "It will render as 'Lecture: <title>'."
        )
    run = longest_unbreakable_run(title)
    if run > MAX_UNBREAKABLE_CHARS:
        warnings.append(
            f"Lecture title contains an unbreakable run of {run} characters "
            f"({_snippet(title)}); it cannot wrap inside the content column."
        )
    return warnings


def _check_topics_covered(lines: list[str]) -> list[str]:
    """The contents block must NOT appear — the reference has none.

    The repair pass strips it, so a hit here means the notes bypassed the
    repair (a direct writer call) and the block would have been silently
    dropped at render time.
    """
    text = "\n".join(lines)
    if _TOPICS_START_RE.search(text) or _TOPICS_END_RE.search(text):
        return ["The notes carry a #TOPICS_COVERED block; the DCN layout has no "
                "contents section (it is stripped before rendering)."]
    return []


_QUICK_REVISION_RE = re.compile(
    r"^\s*#H1:\s*(?:QUICK REVISION|त्वरित पुनरावृत्ति)\s*$", re.I | re.M)
_KEY_SOURCES_RE = re.compile(
    r"^\s*#H[123]:\s*(?:Key Sources|प्रमुख स्रोत)\s*$", re.I | re.M)


def _check_closing_sections(text: str) -> list[str]:
    """Every DCN note ends with QUICK REVISION and then Key Sources."""
    warnings: list[str] = []
    if not _QUICK_REVISION_RE.search(text):
        warnings.append("Missing the closing 'QUICK REVISION' section (#H1).")
    if not _KEY_SOURCES_RE.search(text):
        warnings.append("Missing the closing 'Key Sources' section (#H3).")
    return warnings


def _check_heading_ladder(lines: list[str]) -> list[str]:
    warnings: list[str] = []
    levels: list[tuple[int, int]] = []  # (line number, 1|2|3)
    for idx, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        levels.append((idx, int(m.group(1)[1])))
        if not m.group(2).strip():
            warnings.append(f"Line {idx}: #{m.group(1)} marker has no heading text.")
    if not levels:
        warnings.append("No #H1/#H2/#H3 headings found — the notes have no section ladder.")
        return warnings
    seen: set[int] = set()
    for idx, level in levels:
        if level > 1 and (level - 1) not in seen:
            warnings.append(
                f"Line {idx}: #H{level} appears before any #H{level - 1} — heading ladder is out of order."
            )
        seen.add(level)
    if 1 not in seen:
        warnings.append("No #H1 heading found — every DCN section starts with a red #H1.")
    return warnings


def _is_fragment(body: str) -> bool:
    """A DCN bullet must read as a sentence: long enough, punctuated, with a verb."""
    clean = body.replace("**", "").strip()
    if not clean:
        return True
    words = clean.split()
    if len(words) < _MIN_BULLET_WORDS:
        return True
    # A trailing "[Needs Verification: ...]" is a marker, not the sentence end,
    # so drop it before asking whether the sentence itself is punctuated.
    tail = _TRAILING_MARKER_RE.sub("", clean).rstrip(")\"'”’").strip()
    if not tail.endswith(_TERMINAL_PUNCTUATION):
        return True
    tokens = {re.sub(r"[^\w]", "", w).lower() for w in words}
    return not any(t in _VERB_HINTS or _VERB_SUFFIX_RE.match(t) for t in tokens if t)


def _check_bullets(lines: list[str], zones: list[str]) -> list[str]:
    bullets: list[tuple[int, str]] = []
    for idx, (line, zone) in enumerate(zip(lines, zones), start=1):
        if zone != "body":
            continue
        m = _BULLET_RE.match(line)
        if m and m.group(1).strip():
            bullets.append((idx, m.group(1).strip()))
    if len(bullets) < _MIN_BULLETS_FOR_RATIO:
        return []
    fragments = [(idx, body) for idx, body in bullets if _is_fragment(body)]
    ratio = len(fragments) / len(bullets)
    if ratio <= _FRAGMENT_RATIO:
        return []
    examples = ", ".join(f"line {idx} ({_snippet(body, 50)})" for idx, body in fragments[:5])
    return [
        f"{len(fragments)} of {len(bullets)} bullets ({ratio:.0%}) are short fragments "
        f"rather than full sentences — e.g. {examples}."
    ]


def _check_stray_tables(lines: list[str], zones: list[str]) -> list[str]:
    """Tabular-looking prose that never got wrapped in #TABLE_START/#TABLE_END."""
    warnings: list[str] = []
    for matcher, label in (
        (lambda s: len(_PIPE_CELL_RE.findall(s)) >= 2, "pipe-separated rows"),
        (lambda s: bool(_LABEL_NUMBER_RE.match(s)), "'Label: <number> <unit>' rows"),
    ):
        run_start = 0
        run = 0
        for idx, (line, zone) in enumerate(zip(lines, zones), start=1):
            hit = zone == "body" and bool(line.strip()) and matcher(line)
            if hit:
                run += 1
                if run == 1:
                    run_start = idx
                continue
            if run >= 3:
                warnings.append(
                    f"Lines {run_start}-{idx - 1}: {run} consecutive {label} outside a "
                    f"{_TABLE_START}/{_TABLE_END} block — wrap them in a table."
                )
            run = 0
        if run >= 3:
            warnings.append(
                f"Lines {run_start}-{len(lines)}: {run} consecutive {label} outside a "
                f"{_TABLE_START}/{_TABLE_END} block — wrap them in a table."
            )
    return warnings


def _check_symbols(text: str) -> list[str]:
    warnings: list[str] = []
    found = [m for m in _MOJIBAKE if m in text]
    if found:
        warnings.append(
            "Mojibake in the notes (" + ", ".join(repr(f) for f in found) +
            ") — the model output was decoded with the wrong encoding."
        )
    # The heading markers are literally "#H1:".."#H3:", which read as flattened
    # H₁..H₃ to the check below. Strip the markers first so a correctly written
    # note is not reported as having lost its subscripts.
    prose = _MARKER_TOKEN_RE.sub(" ", text)
    # A formula written both ways means one occurrence lost its subscripts.
    for base, subs in dict.fromkeys(_SUBSCRIPT_RE.findall(prose)):
        pretty = base + subs
        flat = base + "".join(_SUBSCRIPT_TO_ASCII[c] for c in subs)
        # A trailing \b would miss exactly the formulas that matter most: in
        # "H2O" and "N2O" the character after the flattened "H2"/"N2" is a
        # letter, so there is no word boundary there. Anchor on the left and
        # only forbid a following digit, which is what would make it a
        # different number ("H2" inside "H2020").
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(flat)}(?![0-9])", prose):
            warnings.append(
                f"Subscript lost: '{flat}' appears flattened while '{pretty}' is used "
                "elsewhere — make both the same."
            )
    return warnings


def _check_length(lines: list[str], min_words: int) -> list[str]:
    words = _word_count(lines)
    if words < min_words:
        return [f"Notes too short: {words} words against a minimum of {min_words}."]
    return []


def _word_count(lines: list[str]) -> int:
    total = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.upper().startswith(("#TABLE", "#FLOW", "#SEPARATOR")):
            continue
        body = _HEADING_RE.sub(r"\2", stripped)
        if body.upper().startswith(_TITLE_MARKER):
            body = body[len(_TITLE_MARKER):]
        body = _BULLET_RE.sub(r"\1", body).replace("**", "")
        total += len(body.split())
    return total


def _check_date_sensitive(lines: list[str], zones: list[str]) -> list[str]:
    risks: list[str] = []
    for idx, (line, zone) in enumerate(zip(lines, zones), start=1):
        if zone == "flow" or not line.strip():
            continue
        if not _DATE_SENSITIVE_RE.search(line):
            continue
        if _DATE_QUALIFIER_RE.search(line) or _NEEDS_VERIFICATION_RE.search(line):
            continue
        risks.append(f"Line {idx}: no 'as of <date>' qualifier — {_snippet(line, 110)}")
    return _capped(risks, "unqualified date-sensitive claims")


def _check_layout(lines: list[str], zones: list[str]) -> list[str]:
    wide: list[str] = []
    long_words: list[str] = []
    for idx, (line, zone) in enumerate(zip(lines, zones), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        is_row = zone == "table" or len(_PIPE_CELL_RE.findall(stripped)) >= 1
        if is_row and len(stripped) > _MAX_TABLE_ROW_CHARS:
            wide.append(
                f"Line {idx}: table row is {len(stripped)} characters "
                f"(over {_MAX_TABLE_ROW_CHARS}) and will overflow the 5.5 in column — "
                f"{_snippet(stripped, 60)}"
            )
        for word in stripped.replace("**", "").split():
            if len(word) > _MAX_WORD_CHARS:
                long_words.append(
                    f"Line {idx}: unbreakable word of {len(word)} characters — {_snippet(word, 60)}"
                )
    return _capped(wide, "over-wide table rows") + _capped(long_words, "over-long words")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _snippet(text: str, limit: int = 80) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return f"'{flat}'" if flat else "''"


def _capped(items: list[str], label: str) -> list[str]:
    """Keep reports readable: a model that drifts can produce hundreds of hits."""
    if len(items) <= _MAX_ITEMS:
        return items
    kept = items[:_MAX_ITEMS]
    kept.append(f"... and {len(items) - _MAX_ITEMS} more {label} (list truncated).")
    return kept
