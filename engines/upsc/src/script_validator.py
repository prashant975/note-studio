"""Latin-script detection for Hindi DCN notes — report, never strip.

The Hindi output contract (prompts/upsc_dcn_hi.txt) is Devanagari-only body
text: proper nouns transliterate (स्पेसएक्स, नासा), Roman survives only in
headings and in a bracketed acronym on first use. A model that slips Roman
into a bullet has broken the contract, but silently stripping the run would
corrupt the sentence — so this validator only *flags*, and the pipeline
surfaces the flags as warnings for the reviewer.

What is exempt, and why:

    heading lines            Roman acronyms are allowed there by rule 2
    marker tokens            #H1:, #TABLE_START… are format, not content
    markdown link URLs       a URL has no Devanagari form
    bracketed glosses        "यूएनएफसीसीसी (UNFCCC)", "राजकोषीय घाटा (Fiscal
                             Deficit)": the English after its Devanagari term,
                             which languages.vocabulary_rule asks for
    [Needs Verification:…]   a pipeline tag, not student-facing prose
    math between $…$         formulas have no Devanagari form
    formula/acronym tokens   H₂SO₄, NaCl, NDC — single tokens, capital-led
    capitalised names        Newton, Bohr — scientists and named laws
    bare numbers/units       "73%", "m/s²", "GW" carry no script problem

A run of two or more Latin WORDS is always flagged: that is an English phrase
in a Hindi sentence, which no exemption covers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_LINE_RE = re.compile(r"^\s*#(?:LECTURE_TITLE|H[123]):", re.I)
_MARKER_LINE_RE = re.compile(
    r"^\s*#(?:TABLE_START|TABLE_END|FLOW_START|FLOW_END|SEPARATOR"
    r"|TOPICS_COVERED_START|TOPICS_COVERED_END|FIGURE\b"
    r"|CORRECTION_START|CORRECTION_END)", re.I)
_URL_RE = re.compile(r"\(\s*https?://[^)\s]+\s*\)|https?://\S+")
_NEEDS_VERIFICATION_RE = re.compile(r"\[\s*needs verification\s*:[^\]]*\]", re.I)
#: Inline math: $v = u + at$ — permitted Roman by the language contract.
_MATH_SPAN_RE = re.compile(r"\$[^$\n]+\$")
#: The sanctioned first-use pattern: a Devanagari term (bold or not), then its
#: acronym or English term in brackets.
_BRACKETED_ACRONYM_RE = re.compile(
    r"(?<=[ऀ-ॿ])\**\s*\([A-Za-z][A-Za-z0-9&./'’ ,-]{0,60}\)")
#: A run of two or more Latin words (or one word of 2+ letters). Single
#: stray letters ("क" vs "a") are almost always list markers or units.
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z'’.&-]*(?:\s+[A-Za-z][A-Za-z'’.&-]*)*")
#: A single capital-led token: a chemical formula (NaCl), an acronym (NDC), a
#: scientist or named law (Newton, Bohr). All five permitted-Roman categories
#: surface as single tokens; an English PHRASE is always multi-word.
_CAPITAL_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
#: Unit-ish tokens that legitimately stay Roman next to a figure.
_UNIT_TOKENS = frozenset({
    "gw", "mw", "kw", "tw", "km", "kg", "mg", "cm", "mm", "ha", "ppm", "ppb",
    "co", "ch", "no", "so", "o",  # chemical stubs left when subscripts split
    "pm",  # PM2.5
    "m", "s", "g", "n", "j", "w", "v", "a", "k", "mol", "cd", "hz", "pa",
})


@dataclass(frozen=True)
class LatinRun:
    """One Roman-script run found in Hindi body text."""

    line_no: int
    text: str
    line: str

    def __str__(self) -> str:
        return f"line {self.line_no}: {self.text!r} in {self.line.strip()[:80]!r}"


def find_latin_in_hindi_body(notes_text: str) -> list[LatinRun]:
    """Every Latin-script run in a body (non-heading) line of Hindi notes."""
    runs: list[LatinRun] = []
    for line_no, line in enumerate((notes_text or "").splitlines(), start=1):
        if not line.strip():
            continue
        if _HEADING_LINE_RE.match(line) or _MARKER_LINE_RE.match(line):
            continue
        scrubbed = _URL_RE.sub(" ", line)
        scrubbed = _NEEDS_VERIFICATION_RE.sub(" ", scrubbed)
        scrubbed = _MATH_SPAN_RE.sub(" ", scrubbed)
        scrubbed = _BRACKETED_ACRONYM_RE.sub(" ", scrubbed)
        for match in _LATIN_RUN_RE.finditer(scrubbed):
            token = match.group(0)
            if len(token) < 2:
                continue
            if token.lower() in _UNIT_TOKENS:
                continue
            if " " not in token and _CAPITAL_TOKEN_RE.match(token):
                continue                     # formula / acronym / proper noun
            runs.append(LatinRun(line_no, token, line))
    return runs


def latin_script_warnings(notes_text: str, *, max_items: int = 10) -> list[str]:
    """Reviewer-facing warnings for the pipeline. Empty list = contract held."""
    runs = find_latin_in_hindi_body(notes_text)
    if not runs:
        return []
    shown = [f"Hindi notes carry Roman script in body text ({len(runs)} run(s)); "
             "body text should be Devanagari (transliterate, or move an "
             "exam-facing acronym to a heading):"]
    shown += [f"  {run}" for run in runs[:max_items]]
    if len(runs) > max_items:
        shown.append(f"  … and {len(runs) - max_items} more.")
    return shown
