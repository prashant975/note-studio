"""Put the model's notes into the markup the writers read, whatever dialect it used.

The prompts ask for one markup (``#H2: Heading``, ``#TABLE_START`` blocks,
real → and CO₂ characters), and models drift from it in a few predictable
ways. Each drift reached a student's page as literal text:

    ## H2: How Climate Change Can Manifest      printed with the hashes
    ##H2: संयुक्त राष्ट्र की वित्तीय निर्भरता      the same, in the Hindi notes
    | मिसाइल | रेंज |  /  |---|---:|            a markdown table, printed as pipes
    liquid oxygen ($LOX$) as oxidizer            LaTeX, printed with the dollars
    detonates $\\rightarrow$ generates heat       a LaTeX arrow instead of →

All of them are mechanical, so they are repaired here rather than asked for
again: the meaning is never in doubt, only the spelling of the markup.

Two dialects come out of this. The DCN writer reads ``#H1:``-style markers and
``#TABLE_START`` blocks; the other UPSC formats are plain markdown (``## Heading``,
pipe tables) read by src/docx_writer.py. ``dcn`` picks which one to produce.
Every repair is idempotent, so running it twice changes nothing the second time.
"""
from __future__ import annotations

import re

# ----------------------------------------------------------------- headings
#: "#H2: x", "## H2: x", "##H2 - x", "**#H2: x**". At least one "#" is required:
#: a bare "H2: ..." line can be chemistry ("H2: used as rocket fuel").
#: A closing "**" is consumed only when the line also opened with one, so
#: "#H2: words **bold**" keeps its bold.
_LABELLED_HEADING_RE = re.compile(
    r"^[ \t]*(?P<bold>\*\*)?[ \t]*#{1,6}[ \t]*H(?P<level>[1-4])\b[ \t]*[:：.\-–—]?[ \t]*"
    r"(?P<text>.*?)[ \t]*(?(bold)\*\*)[ \t]*$", re.I)
#: Markers other than headings, which a stray extra "#" or space must not break.
_MARKER_WORDS = (
    "LECTURE_TITLE", "TABLE_START", "TABLE_END", "FLOW_START", "FLOW_END", "FIGURE",
    "SEPARATOR", "TOPICS_COVERED_START", "TOPICS_COVERED_END", "CORRECTION_START",
    "CORRECTION_END")
_MARKER_RE = re.compile(
    r"^[ \t]*(?:#{2,6}[ \t]*|#[ \t]+)(?P<word>" + "|".join(_MARKER_WORDS) + r")\b", re.I)
#: A plain markdown heading: "## Climate Change". Not "#TABLE_START" (no space)
#: and not a heading that already names its level.
_MARKDOWN_HEADING_RE = re.compile(
    r"^[ \t]*(?P<hashes>#{1,6})[ \t]+(?P<text>(?!(?:H[1-4]\b|"
    + "|".join(_MARKER_WORDS) + r"))\S.*?)[ \t]*#*[ \t]*$", re.I)


def _strip_wrapping_bold(text: str) -> str:
    text = text.strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        inner = text[2:-2]
        if "**" not in inner:
            return inner.strip()
    return text


def _heading_line(level: int, text: str, *, dcn: bool) -> str:
    return f"#H{level}: {text}" if dcn else f"{'#' * level} {text}"


def _fix_headings(lines: list[str], *, dcn: bool) -> tuple[list[str], int]:
    fixed = 0
    out: list[str] = []
    for line in lines:
        new = line
        labelled = _LABELLED_HEADING_RE.match(line)
        if labelled and labelled.group("text").strip():
            new = _heading_line(int(labelled.group("level")),
                                _strip_wrapping_bold(labelled.group("text")), dcn=dcn)
        elif dcn:
            marker = _MARKER_RE.match(line)
            if marker:
                new = "#" + marker.group("word").upper() + line[marker.end():]
            else:
                plain = _MARKDOWN_HEADING_RE.match(line)
                if plain:
                    level = min(4, len(plain.group("hashes")))
                    new = _heading_line(level, _strip_wrapping_bold(plain.group("text")),
                                        dcn=True)
        if new != line:
            fixed += 1
        out.append(new)
    return out, fixed


# ------------------------------------------------------------------- tables
#: "|---|---:|:---|" — the alignment row under a markdown table's header.
_RULE_ROW_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$")
_BLOCK_START_RE = re.compile(r"^[ \t]*#(TABLE_START|FLOW_START|CORRECTION_START|TOPICS_COVERED_START)[ \t]*$", re.I)
_BLOCK_END_RE = re.compile(r"^[ \t]*#(TABLE_END|FLOW_END|CORRECTION_END|TOPICS_COVERED_END)[ \t]*$", re.I)


def _is_pipe_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 3


def _wrap_bare_tables(lines: list[str]) -> tuple[list[str], int]:
    """Fence markdown pipe tables in #TABLE_START/#TABLE_END for the DCN writer.

    The DCN writer only reads a table between those markers; a pipe table on its
    own was typeset line by line, pipes and all. Only a run that is plainly a
    table is fenced: two or more pipe rows, with the markdown alignment row
    second, or rows that all carry the same number of cells.
    """
    out: list[str] = []
    wrapped = 0
    inside_block = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BLOCK_START_RE.match(line):
            inside_block = True
        elif _BLOCK_END_RE.match(line):
            inside_block = False
        if inside_block or not _is_pipe_row(line):
            out.append(line)
            i += 1
            continue
        j = i
        while j < len(lines) and (_is_pipe_row(lines[j]) or _RULE_ROW_RE.match(lines[j])):
            j += 1
        run = lines[i:j]
        rows = [r for r in run if not _RULE_ROW_RE.match(r)]
        has_rule = len(run) > 1 and bool(_RULE_ROW_RE.match(run[1]))
        same_width = len({r.strip().strip("|").count("|") for r in rows}) == 1
        if len(rows) >= 2 and (has_rule or same_width):
            out.append("#TABLE_START")
            out.extend(r.strip() for r in rows)
            out.append("#TABLE_END")
            wrapped += 1
        else:
            out.extend(run)
        i = j
    return out, wrapped


# -------------------------------------------------------------------- flows
_FLOW_START_RE = re.compile(r"^[ \t]*#FLOW_START[ \t]*$", re.I)
_FLOW_END_RE = re.compile(r"^[ \t]*#FLOW_END[ \t]*$", re.I)
_LEADING_ARROW_RE = re.compile(r"^[ \t]*(?:→|⟶|➝|➔|⇒|->|-->|=>)[ \t]*")
_TRAILING_ARROW_RE = re.compile(r"[ \t]*(?:→|⟶|➝|➔|⇒|->|-->|=>)[ \t]*$")
_DOWN_ARROW_RE = re.compile(r"^[ \t]*(?:↓|⬇|⇓|→|⟶|➝|➔|⇒|->|-->|=>|\||v|V)[ \t]*$")
_INLINE_ARROW_SPLIT_RE = re.compile(r"[ \t]*(?:→|⟶|➝|➔|⇒|->|-->|=>)[ \t]*")


def _flow_steps(block: list[str]) -> list[str] | None:
    """The steps of a flow not written as "A / ↓ / B", else None.

    The prompt asks for one step per line with ↓ between them. A flow typed with
    sideways arrows at the start of each line was printed exactly so: a centred
    stack of "→ ..." lines that reads as a broken list, not as a flowchart. The
    same goes for "A → B → C" on one line, and for steps with no arrows at all.
    """
    content = [line.strip() for line in block if line.strip()]
    if any(line == "↓" for line in content):
        return None
    if len(content) == 1 and _INLINE_ARROW_SPLIT_RE.search(content[0]):
        steps = [s.strip() for s in _INLINE_ARROW_SPLIT_RE.split(content[0]) if s.strip()]
        return steps if len(steps) >= 2 else None
    steps = []
    for line in content:
        if _DOWN_ARROW_RE.match(line):
            continue
        step = _TRAILING_ARROW_RE.sub("", _LEADING_ARROW_RE.sub("", line)).strip()
        if step:
            steps.append(step)
    return steps if len(steps) >= 2 else None


def _fix_flows(lines: list[str]) -> tuple[list[str], int]:
    out: list[str] = []
    fixed = 0
    i = 0
    while i < len(lines):
        if not _FLOW_START_RE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        end = next((k for k in range(i + 1, len(lines)) if _FLOW_END_RE.match(lines[k])), None)
        if end is None:
            out.extend(lines[i:])
            break
        steps = _flow_steps(lines[i + 1:end])
        if steps is None:
            out.extend(lines[i:end + 1])
        else:
            out.append(lines[i])
            for n, step in enumerate(steps):
                if n:
                    out.append("↓")
                out.append(step)
            out.append(lines[end])
            fixed += 1
        i = end + 1
    return out, fixed


# -------------------------------------------------------------------- LaTeX
_SUPERSCRIPT = dict(zip("0123456789+-=()niax", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃˣ"))
_SUBSCRIPT = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))

#: Longest names first, so \rightarrow is not read as \r + ightarrow.
_COMMANDS = {
    "longrightarrow": "→", "Longrightarrow": "⇒", "rightleftharpoons": "⇌",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "rightarrow": "→", "Rightarrow": "⇒",
    "leftarrow": "←", "Leftarrow": "⇐", "uparrow": "↑", "downarrow": "↓", "implies": "⇒",
    "mapsto": "↦", "to": "→", "gets": "←",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "*",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "simeq": "≃", "sim": "~", "equiv": "≡", "propto": "∝", "infty": "∞", "degree": "°",
    "circ": "°", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "Π", "int": "∫",
    "therefore": "∴", "because": "∵", "in": "∈", "notin": "∉", "subset": "⊂",
    "subseteq": "⊆", "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "angle": "∠",
    "perp": "⊥", "parallel": "∥", "ldots": "…", "cdots": "…", "dots": "…", "hbar": "ħ",
    "ell": "ℓ", "AA": "Å", "checkmark": "✓", "prime": "′", "lt": "<", "gt": ">",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι",
    "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "quad": " ", "qquad": " ", "left": "", "right": "", "displaystyle": "", "limits": "",
}
_COMMAND_RE = re.compile(r"\\(" + "|".join(sorted(_COMMANDS, key=len, reverse=True)) + r")(?![A-Za-z])")
#: Arrows and relations written as LaTeX outside any $...$ — notes never carry a
#: backslash for any other reason.
_BARE_COMMAND_RE = re.compile(
    r"\\(" + "|".join(sorted((c for c, v in _COMMANDS.items() if v.strip() and len(c) > 2),
                             key=len, reverse=True)) + r")(?![A-Za-z])")
_TEXT_WRAPPER_RE = re.compile(
    r"\\(?:text|textrm|textbf|textit|mathrm|mathbf|mathit|mathsf|mathtt|operatorname|"
    r"mbox|ce|pu|unit|mathcal|boldsymbol)\s*\{([^{}]*)\}")
_FRAC_COMMAND_RE = re.compile(r"\\[dt]?frac(?![A-Za-z])")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_DEGREE_RE = re.compile(r"\^\s*\{?\s*\\circ\s*\}?")
_SUP_RE = re.compile(r"\^\s*(?:\{([^{}]*)\}|([^\s{}]))")
_SUB_RE = re.compile(r"_\s*(?:\{([^{}]*)\}|([^\s{}]))")
_SPACING_RE = re.compile(r"\\[,;:! ]")
_ESCAPED_RE = re.compile(r"\\([%$&#_{}])")
#: Inline math: $...$ with no space just inside either dollar and no digit right
#: after the closing one, which is how "$5 billion and $7 billion" stays money.
_INLINE_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\((.+?)\\\)|\\\[(.+?)\\\]|\$(?=\S)([^$\n]{1,160}?)(?<=\S)\$(?!\d)")
_CHEM_DIGITS_RE = re.compile(r"(?<=[A-Za-z)\]])(\d+)")


def _script(text: str, table: dict[str, str]) -> str | None:
    if all(ch in table for ch in text):
        return "".join(table[ch] for ch in text)
    return None


def _math_group(text: str, start: int) -> tuple[str, int] | None:
    """Read one balanced braced operand, including subscripts and fractions."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start == len(text) or text[start] != "{":
        return None
    depth = 1
    for end in range(start + 1, len(text)):
        if text[end] == "{" and text[end - 1] != "\\":
            depth += 1
        elif text[end] == "}" and text[end - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:end], end + 1
    return None


def _fraction_operand(text: str) -> str:
    # A number or named variable can stand alone; sums, products and nested
    # divisions need parentheses to retain the original fraction's grouping.
    if re.fullmatch(r"[+−-]?(?:\d+(?:\.\d+)?|[^\W\d]\w*)", text):
        return text
    return f"({text})"


def _replace_fractions(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _FRAC_COMMAND_RE.finditer(text):
        if match.start() < cursor:
            continue  # the outer fraction already consumed this nested one
        numerator = _math_group(text, match.end())
        denominator = _math_group(text, numerator[1]) if numerator else None
        if denominator is None:
            continue
        out.append(text[cursor:match.start()])
        top = _fraction_operand(_tex_to_text(numerator[0]))
        bottom = _fraction_operand(_tex_to_text(denominator[0]))
        out.append(f"{top}/{bottom}")
        cursor = denominator[1]
    out.append(text[cursor:])
    return "".join(out)


def _tex_to_text(tex: str, *, chemistry: bool = False) -> str:
    text = tex
    if re.search(r"\\ce\s*\{", text):
        chemistry = True
    text = _DEGREE_RE.sub("°", text)
    # Innermost groups first: \text{\frac{a}{b}} needs two passes.
    for _ in range(3):
        text = _TEXT_WRAPPER_RE.sub(r"\1", text)
        text = _replace_fractions(text)
        text = _SQRT_RE.sub(lambda m: "√" + (m.group(1) if len(m.group(1)) == 1 else f"({m.group(1)})"), text)
    if chemistry:
        text = text.replace("<=>", "⇌").replace("->", "→")
        text = _CHEM_DIGITS_RE.sub(lambda m: _script(m.group(1), _SUBSCRIPT) or m.group(1), text)
    text = _COMMAND_RE.sub(lambda m: _COMMANDS[m.group(1)], text)

    def sup(m: re.Match) -> str:
        body = (m.group(1) if m.group(1) is not None else m.group(2)).replace(" ", "")
        return _script(body, _SUPERSCRIPT) or (f"^{body}" if len(body) == 1 else f"^({body})")

    def sub(m: re.Match) -> str:
        body = (m.group(1) if m.group(1) is not None else m.group(2)).replace(" ", "")
        return _script(body, _SUBSCRIPT) or body

    text = _SUP_RE.sub(sup, text)
    text = _SUB_RE.sub(sub, text)
    text = _SPACING_RE.sub(" ", text)
    text = _ESCAPED_RE.sub(r"\1", text)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)      # an unknown command: keep its name
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _looks_like_math(body: str) -> bool:
    """A $...$ span worth converting: not "$5$"-style money, never prose."""
    if any(ch in body for ch in "\\^_{}"):
        return True
    if body[:1].isdigit():
        return False
    return len(body) <= 40 and len(body.split()) <= 4


def _fix_latex(text: str) -> tuple[str, int]:
    count = 0

    def math(m: re.Match) -> str:
        nonlocal count
        body = next(g for g in m.groups() if g is not None)
        if m.group(4) is not None and not _looks_like_math(body):
            return m.group(0)
        count += 1
        return _tex_to_text(body)

    text = _INLINE_MATH_RE.sub(math, text)

    def bare(m: re.Match) -> str:
        nonlocal count
        count += 1
        return _COMMANDS[m.group(1)]

    text = _BARE_COMMAND_RE.sub(bare, text)
    return text, count


# --------------------------------------------------------------------- main
def normalize_notes_markup(notes_text: str, *, dcn: bool = True) -> tuple[str, list[str]]:
    """The notes in the writer's markup, plus one line per kind of repair made.

    The repairs are for the run log: they are spelling fixes to the markup, not
    changes a reader of the notes needs to hear about.
    """
    if not notes_text:
        return notes_text or "", []
    text, latex = _fix_latex(notes_text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")
    lines, headings = _fix_headings(lines, dcn=dcn)
    tables = flows = 0
    if dcn:
        lines, tables = _wrap_bare_tables(lines)
        lines, flows = _fix_flows(lines)
    repairs = []
    if headings:
        repairs.append(f"rewrote {headings} heading line(s) into the "
                       f"{'#H1: marker' if dcn else 'markdown'} heading markup")
    if tables:
        repairs.append(f"fenced {tables} markdown table(s) in #TABLE_START/#TABLE_END")
    if flows:
        repairs.append(f"rewrote {flows} arrow-typed flowchart(s) as steps joined by ↓")
    if latex:
        repairs.append(f"converted {latex} LaTeX math span(s) to plain characters")
    return "\n".join(lines), repairs
