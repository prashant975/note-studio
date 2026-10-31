"""Formulas as LaTeX draws them: Computer Modern through matplotlib's mathtext
(no TeX needed), as a transparent PNG with its size in points and its depth
below the baseline, so it sits on the line like the text around it."""
from __future__ import annotations

import io
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "mplcache"))

DPI = 600
_LOCK = threading.RLock()
_DISPLAY_FRAC = re.compile(r"\\frac(?=\s*\{)")
# Mathtext sets "a=b" and "a+b" with almost no space; TeX puts a thick space round
# a relation and a medium one round a binary operator, but none inside a script
# or a \mathrm{} word, and none round a sign ("-x", "(-1)").
RELATIONS = {"=", "<", ">", r"\leq", r"\geq", r"\le", r"\ge", r"\neq", r"\ne", r"\approx", r"\equiv", r"\sim",
             r"\simeq", r"\propto", r"\to", r"\rightarrow", r"\leftarrow", r"\Rightarrow", r"\Leftarrow",
             r"\leftrightarrow", r"\Leftrightarrow", r"\rightleftharpoons", r"\longrightarrow", r"\mapsto"}
BINARY = {"+", "-", r"\times", r"\cdot", r"\pm", r"\mp", r"\div", r"\cup", r"\cap"}
_WORDS = {r"\text", r"\textrm", r"\textbf", r"\textit", r"\operatorname", r"\mbox"}      # text, not maths
_OPERAND_END = (")", "]", "}", "|", "!", "'", ".")
_FRACTIONS = {r"\frac", r"\dfrac", r"\tfrac"}
_LATEX_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|.", re.S)


class FormulaError(ValueError):
    """Mathtext cannot draw this LaTeX."""


@dataclass(frozen=True)
class Formula:
    png: bytes
    width: float        # points
    height: float       # points, above and below the baseline
    depth: float        # points below the baseline

    @property
    def ascent(self) -> float:
        return self.height - self.depth


def _matplotlib_lock():
    """matplotlib's fonts are shared by the whole process: draw while holding the
    hand-drawn notes engine's own lock, so a sheet run never draws at the same time."""
    try:
        from notesbot import math_render

        return getattr(math_render, "_lock", _LOCK)
    except Exception:           # the engine is not on the path (a bare render)
        return _LOCK


def _normalized(latex: str) -> str:
    """The hand-drawn engine's clean-up of model-written LaTeX (Unicode Greek,
    unsupported commands); unchanged when that engine is not here."""
    try:
        from notesbot.math_render import normalize_latex

        return normalize_latex(latex)
    except Exception:
        return latex


def spaced(latex: str, display: bool = False) -> str:
    r"""TeX's spacing round relations (\;) and binary operators (\>) at text level."""
    out: list[str] = []
    groups: list[bool] = []            # for each open brace: was spacing allowed outside it?
    allowed = True
    fractions: list[list[int]] = []    # [depth, parts still to come] of each fraction being read
    previous = ""                      # the last token that was not a space
    for token in _LATEX_TOKEN.findall(latex):
        written = token
        if token == "{":
            part = bool(fractions) and fractions[-1][0] == len(groups) and fractions[-1][1] > 0
            groups.append(allowed)
            if previous in ("^", "_") or previous in _WORDS:
                allowed = False
            elif part:
                fractions[-1][1] -= 1
                allowed = allowed and display          # an inline fraction's parts are script size
        elif token == "}":
            allowed = groups.pop() if groups else True
            while fractions and fractions[-1][1] == 0 and fractions[-1][0] == len(groups):
                fractions.pop()
        elif token in _FRACTIONS:
            fractions.append([len(groups), 2])
        elif allowed and token in RELATIONS:
            written = rf"\;{token}\;"
        elif allowed and token in BINARY and _is_operand(previous):
            written = rf"\>{token}\>"
        elif allowed and token == ",":
            written = r",\,"
        out.append(written)
        if not token.isspace():
            previous = token
    return "".join(out)


def _is_operand(token: str) -> bool:
    """Is a binary operator after this token (and not a sign)?"""
    if token.isalnum() or token in _OPERAND_END:
        return True
    return token.startswith("\\") and len(token) > 2 and token not in RELATIONS | BINARY | _FRACTIONS \
        and token not in (r"\left", r"\right", r"\quad", r"\qquad")


def render(latex: str, size: float, *, display: bool = False) -> Formula:
    """One formula, as written or after the clean-up. Raises FormulaError."""
    source = " ".join(str(latex or "").split())
    if not source:
        raise FormulaError("empty formula")
    if display:
        source = _DISPLAY_FRAC.sub(r"\\dfrac", source)
    tried = [spaced(source, display), source]
    if _normalized(source) != source:
        tried.append(spaced(_normalized(source), display))
    for text in tried:
        try:
            return _render(text, round(float(size), 2))
        except Exception:
            continue
    raise FormulaError(source[:80])


@lru_cache(maxsize=4096)
def _render(text: str, size: float) -> Formula:
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import MathTextParser
    from PIL import Image

    with _matplotlib_lock():
        parsed = MathTextParser("agg").parse(f"${text}$", dpi=DPI, prop=FontProperties(size=size, math_fontfamily="cm"))
        alpha = np.array(parsed.image, dtype=np.uint8)
    if not alpha.size or not alpha.any():
        raise FormulaError("nothing drawn")
    rgba = np.zeros((*alpha.shape, 4), dtype=np.uint8)
    rgba[..., 3] = alpha
    out = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(out, format="PNG", optimize=True)
    scale = 72.0 / DPI
    return Formula(out.getvalue(), alpha.shape[1] * scale, alpha.shape[0] * scale, float(parsed.depth) * scale)
