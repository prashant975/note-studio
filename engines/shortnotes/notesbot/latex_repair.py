"""Repair LaTeX commands mangled by JSON escape parsing.

Gemini returns notes as JSON strings. If it writes a LaTeX command with a single
backslash, JSON parsing eats the backslash pair:  \\rightarrow -> CR+"ightarrow",
\\frac -> FF+"rac", \\triangle -> TAB+"riangle", \\neq -> NL+"eq", \\beta ->
BS+"eta".  This module restores them deterministically.
"""
from __future__ import annotations

import re

# control char produced by JSON  ->  command completions (longest first!)
_RESTORE: dict[str, list[str]] = {
    "\r": ["ightleftharpoons", "ightarrow", "angle", "ho", "ight"],
    "\t": ["herefore", "riangle", "imes", "heta", "ext", "anh", "au", "an", "o"],
    "\f": ["orall", "rac"],
    "\n": ["abla", "eq", "u", "e"],
    "\x08": ["egin", "oxed", "eta", "ar", "ig"],
}

_CTRL_LETTER = {"\r": "r", "\t": "t", "\f": "f", "\n": "n", "\x08": "b"}

_PATTERNS: list[tuple[re.Pattern, str]] = []
for _ctrl, _completions in _RESTORE.items():
    for _c in _completions:
        # Short completions only match when not followed by another letter,
        # so NL+"either" doesn't wrongly become \neither via "e".
        guard = r"(?![A-Za-z])" if len(_c) <= 2 else ""
        # The control char swallowed BOTH the backslash and its letter
        # (\r == "\\" + "r"), so restore "\\" + letter + completion.
        _PATTERNS.append(
            (
                re.compile(re.escape(_ctrl) + re.escape(_c) + guard),
                "\\\\" + _CTRL_LETTER[_ctrl] + _c,
            )
        )

_LEFTOVER_CTRL = re.compile(r"[\r\t\f\x08\n]+")


_TEXT_PREFIX_RE = re.compile(r"(?<![\\$A-Za-z])text(?=[A-Z])")


def repair_text(text: str) -> str:
    """Restore JSON-mangled LaTeX commands; flatten leftover control chars."""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    # "textHeat" = \text{Heat} with backslash and braces both lost.
    text = _TEXT_PREFIX_RE.sub("", text)
    return _LEFTOVER_CTRL.sub(" ", text)


def normalize_math_delimiters(text: str) -> str:
    """Accept display/parenthesized math from old notes without printing markers."""
    text = re.sub(r"\$\$(.*?)\$\$", lambda m: "$" + m[1].strip() + "$", text, flags=re.S)
    text = re.sub(r"\\\[(.*?)\\\]|\\\((.*?)\\\)",
                  lambda m: "$" + (m[1] if m[1] is not None else m[2]).strip() + "$", text, flags=re.S)
    # A sentence inside a text wrapper is prose, especially in Devanagari:
    # mathtext cannot shape it. Keep nearby maths in its own span.
    def prose(match):
        value = match[1]
        out = []
        end = 0
        for found in re.finditer(r"\\text(?:rm|bf|it)?\{([^{}]*)\}", value):
            prefix = value[:found.start()]
            depth = len(re.findall(r"(?<!\\)\{", prefix)) - len(re.findall(r"(?<!\\)\}", prefix))
            is_prose = re.search(r"[\u0900-\u097f]", found[1]) or len(found[1].split()) > 1
            if not is_prose or depth or prefix.rstrip().endswith(("^", "_")):
                continue
            if value[end:found.start()].strip():
                out.append("$" + value[end:found.start()].strip() + "$")
            out.append(found[1])
            end = found.end()
        if not out:
            return match[0]
        if value[end:].strip():
            out.append("$" + value[end:].strip() + "$")
        return " ".join(out)
    return re.sub(r"\$([^$]+)\$", prose, text)


# --- auto-wrap: formulas written OUTSIDE $...$ get wrapped so they render ----
_CMD_RE = re.compile(r"\\[a-zA-Z]{2,}")
_BOUND_START = re.compile(r"[:;]\s*")


def _wrap_segment(seg: str) -> str:
    m = _CMD_RE.search(seg)
    if not m:
        return seg
    # Start after the last ':'/';' before the command (e.g. "Formula: ...").
    starts = [b.end() for b in _BOUND_START.finditer(seg[: m.start()])]
    start = starts[-1] if starts else 0
    # Keep Hindi prose out of mathtext when a model omits the dollar markers.
    hindi_prefix = list(re.finditer(r"[\u0900-\u097f]+\s*", seg[:m.start()]))
    if hindi_prefix:
        start = max(start, hindi_prefix[-1].end())
    # End at the last formula-ish character after the command, so trailing
    # plain words stay outside the wrap.
    tail = seg[m.start():]
    last = max((tail.rfind(c) for c in "}=+-*/)0123456789"), default=-1)
    end = m.start() + last + 1 if last >= 0 else len(seg)
    core = seg[start:end].strip()
    if not core:
        return seg
    return seg[:start] + "$" + core + "$" + seg[end:]


# --- symbols the model sometimes SPELLS OUT in plain prose ------------------
# Only names that are essentially never ordinary English words, so we cannot
# damage normal text ("alpha particle", "beta decay" stay as words).
_SYM_WORDS = ("nu|neu|lambda|mu|theta|sigma|omega|phi|psi|chi|tau|rho|"
              "epsilon|zeta|eta|kappa|xi|Omega|Lambda|Sigma|Phi|Psi|Theta|"
              "uparrow|downarrow|rightarrow|leftarrow|leftrightarrow|"
              "Rightarrow|rightleftharpoons")
_BAR_WORD_RE = re.compile(
    r"(?<![\\$A-Za-z])(" + _SYM_WORDS + r"|[a-zA-Z])\s+bar\b")
_SYM_WORD_RE = re.compile(r"(?<![\\$A-Za-z])(" + _SYM_WORDS + r")\b(?![A-Za-z])")


def _sym_words_to_math(text: str) -> str:
    """Turn spelled-out symbols OUTSIDE $...$ into real maths ("nu bar")."""
    def bar(m):
        base = m.group(1)
        name = "\\nu" if base.lower() in ("nu", "neu") else (
            f"\\{base}" if len(base) > 1 else base)
        return f"$\\bar{{{name}}}$"

    text = _BAR_WORD_RE.sub(bar, text)
    return _SYM_WORD_RE.sub(
        lambda m: "$\\%s$" % ("nu" if m.group(1).lower() in ("nu", "neu")
                              else m.group(1)), text)


def autowrap_math(text: str) -> str:
    """Wrap stray LaTeX / spelled-out symbols (no $...$) in math markers."""
    if not text:
        return text
    text = normalize_math_delimiters(text)
    text = normalize_science_notation(text)
    parts = re.split(r"(\$[^$]*\$)", text)
    out = []
    for p in parts:
        if p.startswith("$") and p.endswith("$"):
            out.append(p)          # already maths — leave untouched
            continue
        if "\\" in p:
            # _wrap_segment may create a new $...$ span that swallows earlier
            # prose. Spelled-out words must then only be converted OUTSIDE the
            # new span — converting inside would nest $...$ and break the
            # renderer's splitter; words inside the span are normalized at
            # render time instead.
            p = "".join(
                sub if sub.startswith("$") else _sym_words_to_math(sub)
                for sub in re.split(r"(\$[^$]*\$)", _wrap_segment(p))
            )
            out.append(p)
            continue
        out.append(_sym_words_to_math(p))
    return "".join(out)


_UNIT_POWER = re.compile(
    r"(?<![\w\\])(?P<length>km|cm|mm|m)\s*(?P<time>s|h)"
    r"\s*(?:\^\s*\{?\s*[-−–]\s*(?P<caret>[12])\s*\}?|"
    r"[-−–]\s*(?P<flat>[12])|⁻(?P<super>[¹²]))(?![\w])"
)
_WORD_FORM_LABELS = (
    "total distance travelled", "total distance traveled", "time interval",
    "average speed", "average velocity", "change in velocity",
    "change in time", "total time", "area of rectangle", "area of triangle",
)
_TEMPERATURE_WORD = re.compile(
    r"(?P<number>[+\-−]?\d+(?:\.\d+)?)\s*(?:deg(?:rees?)?\s*|[°∘]\s*)(?P<unit>[CF])(?![A-Za-z])"
)

# These are command fragments, not ordinary prose such as "textbook" or
# "degree of a polynomial". Check the text actually painted on each page.
_NOTATION_ARTIFACTS = re.compile(
    r"\\(?:text(?:rm|bf|it)?|mathrm|mathbf|mathit|frac|circ|geq?|leq?)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:text(?:rm|bf|it)?|mathrm|mathbf|mathit)(?=[A-Z0-9{])"
    r"|(?:\d|[%^])\s*text(?:\b|(?=[A-Z{]))"
    r"|\bcirc(?:text)?[CF]\b"
    r"|\bdeg\s*[CF]\b"
    r"|\b(?:ge|le)\s+\d+(?=\s*(?:text|circ|deg|°))"
)


def notation_artifacts(text: str) -> list[str]:
    """Recognize the printed LaTeX/temperature debris from real PDF defects."""
    return list(dict.fromkeys(match[0] for match in _NOTATION_ARTIFACTS.finditer(text)))


def normalize_science_notation(text: str) -> str:
    """Repair units, temperatures, word-form labels and known chemistry defects.

    Chemistry changes are restricted to the user-reported bond legend and
    lead displacement; unrelated formulas and physical states stay intact.
    """
    from .chemistry_repair import normalize_chemistry_notation
    text = normalize_chemistry_notation(text)
    def unit(match):
        power = match["caret"] or match["flat"] or {"¹": "1", "²": "2"}[match["super"]]
        return rf"\mathrm{{{match['length']}}}\,\mathrm{{{match['time']}}}^{{-{power}}}"

    parts = re.split(r"(\$[^$]*\$)", text)
    for index, part in enumerate(parts):
        math_span = part.startswith("$") and part.endswith("$")
        if math_span:
            part = _UNIT_POWER.sub(unit, part)
            # Match only a complete brace argument, never arbitrary prose or
            # substrings of symbols. Support old collapsed model output too.
            for label in _WORD_FORM_LABELS:
                pattern = r"(?<!\\mathrm)(?<!\\text)\{" + r"\s*".join(map(re.escape, label.split())) + r"\}"
                part = re.sub(pattern, lambda _m, label=label: r"{\mathrm{" + label + "}}", part, flags=re.I)
        else:
            part = part.replace("℃", "°C").replace("℉", "°F")
            part = _TEMPERATURE_WORD.sub(lambda m: m["number"] + " °" + m["unit"], part)
            part = _UNIT_POWER.sub(lambda m: "$" + unit(m) + "$", part)
        parts[index] = part
    return "".join(parts)


def repair_notes(notes) -> None:
    """Repair every string field of a ShortNotes object in place."""
    def fix(s: str) -> str:
        return autowrap_math(repair_text(s))

    notes.lecture_no = repair_text(notes.lecture_no)
    notes.subject = repair_text(notes.subject)
    notes.title = repair_text(notes.title)
    for section in notes.sections:
        section.heading = fix(section.heading)
        section.points = [fix(p) for p in section.points]
        section.tricks = [fix(t) for t in section.tricks]
        for example in getattr(section, "worked_examples", []) or []:
            example.statement = fix(example.statement)
            example.steps = [fix(step) for step in example.steps]
            example.result = fix(example.result)
        section.flow = [fix(s) for s in section.flow]
        section.notes = [fix(n) for n in getattr(section, "notes", []) or []]
        section.key_terms = [fix(k) for k in getattr(section, "key_terms", []) or []]
        section.diagram = repair_text(getattr(section, "diagram", "") or "")
        for wi in getattr(section, "where", []) or []:
            wi.symbol = fix(wi.symbol)
            wi.meaning = fix(wi.meaning)
        cmpx = getattr(section, "compare", None)
        if cmpx is not None:
            cmpx.left_title = fix(cmpx.left_title)
            cmpx.right_title = fix(cmpx.right_title)
            cmpx.left_points = [fix(p) for p in cmpx.left_points]
            cmpx.right_points = [fix(p) for p in cmpx.right_points]
        if section.table is not None:
            section.table.headers = [fix(h) for h in section.table.headers]
            section.table.rows = [
                [fix(c) for c in row] for row in section.table.rows
            ]
        if section.graph is not None:
            # Labels go to matplotlib mathtext, so also auto-wrap stray LaTeX.
            g = section.graph
            g.title = fix(g.title)
            g.x_label = fix(g.x_label)
            g.y_label = fix(g.y_label)
            for curve in g.curves:
                curve.label = fix(curve.label)
        if getattr(section, "schematic", None):
            section.schematic.caption = fix(section.schematic.caption)
            for label in section.schematic.labels:
                label.text = fix(label.text)
    if getattr(notes, "mindmap", None) is not None:
        mm = notes.mindmap
        mm.center = repair_text(mm.center)
        for branch in mm.branches:
            branch.label = fix(branch.label)
            branch.children = [fix(c) for c in branch.children]
