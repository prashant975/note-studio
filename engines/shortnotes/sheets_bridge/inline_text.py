"""Turn the model's inline markup into reviewable, editable document content.

Notes come back with two kinds of inline markup:

* ``**emphasis**`` — markdown bold,
* ``$ ... $`` — LaTeX maths (the studygram PDF renders these with matplotlib).

Maths is handed to :mod:`equation_kit`, which converts it to a **native Word
equation** (OMML) that survives Drive's conversion into a Google Doc and stays
editable there — real fractions, vector arrows and true sub/superscripts
instead of an approximation. ``$...$`` is first rewritten into the kit's
protected tags, and the kit's repair pass rescues formulas the model wrote as
plain text (``H2O`` → ``H_2O``, ``m/s2`` → ``m s⁻²``).

``plain_text`` gives the Unicode rendering for the places that need a plain
string (headings in the file name, joined flows, key-term lists).
"""
from __future__ import annotations

import re

from equation_kit import latex_to_unicode, repair_equations, split_math_segments

_DOLLAR_RE = re.compile(r"\$(.+?)\$", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_TIDY = (("^°", "°"), ("^{}", ""), ("  ", " "))


def prepare(text: object) -> str:
    """``$...$`` → protected maths tags, then rescue untagged formulas."""
    from notesbot.latex_repair import normalize_math_delimiters
    source = normalize_math_delimiters(str(text or ""))
    if not source:
        return ""
    tagged = _DOLLAR_RE.sub(lambda match: f"[[MATH_INLINE: {match.group(1).strip()}]]", source)
    try:
        repaired, _repairs = repair_equations(tagged)
        return repaired
    except Exception:  # noqa: BLE001 - a repair must never break a document
        return tagged


def segments(text: object) -> list[tuple[str, str]]:
    """``[(kind, payload)]`` where kind is ``text`` | ``inline`` | ``block``."""
    prepared = prepare(text)
    if not prepared:
        return []
    try:
        return list(split_math_segments(prepared))
    except Exception:  # noqa: BLE001
        return [("text", prepared)]


def bold_runs(text: object) -> list[tuple[str, bool]]:
    """Split a plain segment into ``(chunk, is_bold)`` on ``**...**``."""
    source = str(text or "")
    if not source:
        return []
    runs: list[tuple[str, bool]] = []
    position = 0
    for match in _BOLD_RE.finditer(source):
        if match.start() > position:
            runs.append((source[position:match.start()], False))
        runs.append((match.group(1), True))
        position = match.end()
    if position < len(source):
        runs.append((source[position:], False))
    return [run for run in runs if run[0]]


def latex_to_text(latex: str) -> str:
    """Readable Unicode for one LaTeX span (vectors, chemistry, units)."""
    try:
        rendered = latex_to_unicode(str(latex or ""))
    except Exception:  # noqa: BLE001
        rendered = str(latex or "")
    for bad, good in _TIDY:
        rendered = rendered.replace(bad, good)
    return rendered.strip()


def inline_runs(text: object) -> list[tuple[str, bool, bool]]:
    """``(text, bold, is_math)`` runs — used where OMML cannot be embedded."""
    runs: list[tuple[str, bool, bool]] = []
    for kind, payload in segments(text):
        if kind == "text":
            runs.extend((chunk, bold, False) for chunk, bold in bold_runs(payload))
        else:
            rendered = latex_to_text(payload)
            if rendered:
                runs.append((rendered, False, True))
    return [run for run in runs if run[0]]


def plain_text(text: object) -> str:
    """The same content as one string, markup resolved."""
    return "".join(chunk for chunk, _bold, _math in inline_runs(text))
