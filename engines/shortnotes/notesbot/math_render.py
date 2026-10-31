"""Render a LaTeX math string to a transparent PNG via matplotlib mathtext.

No system LaTeX needed (mathtext is pure-Python). Returns the PNG bytes plus the
equation's physical size in points so the PDF layout can place it inline.
"""
from __future__ import annotations

import io
import os
import tempfile
import threading

# matplotlib needs a writable config dir and a headless backend.
os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "mplcache"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402  (pulled in by matplotlib)

_DPI = 300
_lock = threading.Lock()  # pyplot's global state is not thread-safe
_cache: "dict[tuple[str, float], tuple[bytes, float, float]]" = {}

import re  # noqa: E402

# Unicode / word-form -> proper LaTeX, applied to every $...$ span before
# rendering. Catches Gemini writing "lambda_m^circ", "->", "Δ" etc. in math.
_UNICODE_MAP = {
    "→": r"\rightarrow ", "⟶": r"\rightarrow ", "⇌": r"\rightleftharpoons ",
    "⇋": r"\rightleftharpoons ", "↔": r"\leftrightarrow ",
    "×": r"\times ", "·": r"\cdot ", "°": r"^\circ ", "∘": r"^\circ ",
    "≠": r"\neq ", "≤": r"\leq ", "≥": r"\geq ", "≈": r"\approx ",
    "∝": r"\propto ", "∞": r"\infty ", "±": r"\pm ",
    "Δ": r"\Delta ", "∆": r"\Delta ", "Λ": r"\Lambda ", "λ": r"\lambda ",
    "μ": r"\mu ", "π": r"\pi ", "σ": r"\sigma ", "ω": r"\omega ",
    "θ": r"\theta ", "α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ",
    "ρ": r"\rho ", "η": r"\eta ", "ε": r"\epsilon ", "Ω": r"\Omega ",
    "ν": r"\nu ", "τ": r"\tau ", "φ": r"\phi ", "ψ": r"\psi ", "χ": r"\chi ",
    "↑": r"\uparrow ", "↓": r"\downarrow ",
    "⇒": r"\Rightarrow ", "⇐": r"\Leftarrow ",
}
_GREEK_WORDS = (
    "Lambda|lambda|Delta|delta|Theta|theta|Sigma|sigma|Omega|omega|alpha|beta|"
    "gamma|mu|nu|pi|rho|tau|phi|Phi|epsilon|eta|infty|propto|approx|circ|"
    "rightleftharpoons|rightarrow|leftrightarrow|uparrow|downarrow|Uparrow|"
    # NOTE: "pm" is deliberately absent — in these notes "pm" is nearly always
    # the picometre unit (Bohr radius, wavelengths); converting it to \pm
    # printed "52.9 n^2/Z ±". A real plus-minus arrives as "±" or "\\pm".
    "Downarrow|Rightarrow|Leftarrow|times|cdot|neq|leq|geq|sqrt"
)
_BARE_WORD_RE = re.compile(r"(?<![\\A-Za-z])(" + _GREEK_WORDS + r")(?![A-Za-z])")


def normalize_latex(latex: str) -> str:
    """Best-effort cleanup of a $...$ span so mathtext parses it."""
    s = latex
    for uni, cmd in _UNICODE_MAP.items():
        s = s.replace(uni, cmd)
    # Commands mathtext does not support -> supported equivalents.
    # \xrightarrow{cond} => arrow with the condition as a superscript.
    s = re.sub(r"\\?xrightarrow\s*\{([^}]*)\}", r"{\\rightarrow}^{\1}", s)
    s = re.sub(r"(?<![\\A-Za-z])xrightarrow(?![A-Za-z])", r"\\rightarrow ", s)
    s = re.sub(r"\\xleftarrow\s*\{([^}]*)\}", r"{\\leftarrow}^{\1}", s)
    s = re.sub(r"\\overset\s*\{([^}]*)\}\s*\{([^}]*)\}", r"{\2}^{\1}", s)
    s = re.sub(r"\\underset\s*\{([^}]*)\}\s*\{([^}]*)\}", r"{\2}_{\1}", s)
    s = re.sub(r"\\?text(?:rm|bf|it)?\s*\{([^}]*)\}",
               lambda m: r"\mathrm{" + m[1].replace("--", "–") + "}" if m[1].strip() else " ", s)
    # Mathtext ignores ordinary spaces even inside \mathrm. Explicit spaces
    # keep word-form fractions legible (total distance travelled / time interval).
    s = re.sub(r"\\mathrm\{([^{}]*)\}",
               lambda m: " " if not m[1].replace(r"\ ", "").strip() else
               r"\mathrm{" + re.sub(r"(?<!\\)\s+", r"\\ ", m[1]) + "}", s)
    # "textHeat" = \text{Heat} with both backslash and braces lost: drop the
    # stray prefix so the word renders as itself.
    s = re.sub(r"(?<![\\A-Za-z])text(?=[A-Z])", "", s)
    # "implies" written as a word inside maths -> a real arrow.
    s = re.sub(r"(?<![\\A-Za-z])implies(?![A-Za-z])", r"\\Rightarrow ", s)
    # A bare percent is a comment marker to mathtext, so unescaping it makes
    # the entire alloy-composition formula fail and enter the prose fallback.
    s = re.sub(r"(?<!\\)%", r"\\%", s).replace(r"\$", "")
    # "nu bar" / "x bar" written as words -> a real overbar.
    s = re.sub(r"\\?\b(?:neu|nu)\s+bar\b", r"\\bar{\\nu}", s, flags=re.I)
    s = re.sub(r"\\?\b([A-Za-z]|\\[A-Za-z]+)\s+bar\b", r"\\bar{\1}", s)
    s = re.sub(r"\\?\b([A-Za-z]|\\[A-Za-z]+)\s+hat\b", r"\\hat{\1}", s)
    s = re.sub(r"\bh\s*cross\b", r"\\hbar", s, flags=re.I)
    # ASCII arrows.
    s = s.replace("<=>", r" \rightleftharpoons ").replace("<->", r" \leftrightarrow ")
    s = s.replace("->", r" \rightarrow ")
    # Word-forms without backslash: lambda_m^circ -> \lambda_m^\circ .
    s = _BARE_WORD_RE.sub(lambda m: "\\" + m.group(1), s)
    # TeX accepts these aliases, but matplotlib mathtext requires geq/leq.
    s = re.sub(r"\\(ge|le)(?![A-Za-z])", lambda m: "\\" + m[1] + "q", s)
    # \triangle G is almost always meant as \Delta G in chemistry.
    s = s.replace(r"\triangle", r"\Delta")
    # ^circ / _circ written with a backslash already are untouched above.
    s = re.sub(r"\^\s*\\?circ", r"^\\circ", s)
    # \sqrt 6 / sqrt(6) -> \sqrt{6}
    s = re.sub(r"\\sqrt\s*\(([^)]*)\)", r"\\sqrt{\1}", s)
    # Collapse doubled backslashes (model over-escaping inside the JSON).
    if r"\begin{" not in s:
        s = s.replace("\\\\", "\\")
    return re.sub(r"\s+", " ", s).strip()


def render_latex(latex: str, fontsize_pt: float = 15.0) -> "tuple[bytes, float, float]":
    """Return (png_bytes, width_pt, height_pt) for a LaTeX expression (no $...$)."""
    latex = normalize_latex(latex)
    key = (latex, fontsize_pt)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    if r"\begin{" in latex:
        from .matrix_render import render_expression
        img = render_expression(latex, fontsize_pt, render_latex, _DPI)
        out = io.BytesIO()
        img.save(out, format="PNG")
        result = (out.getvalue(), img.width / _DPI * 72, img.height / _DPI * 72)
        if len(_cache) < 512:
            _cache[key] = result
        return result

    with _lock:
        fig = plt.figure(figsize=(0.01, 0.01))
        # Generous pad so no glyph (superscripts!) is clipped by the tight bbox.
        fig.text(0, 0, f"${latex}$", fontsize=fontsize_pt, color="#1a2a4a")
        buf = io.BytesIO()
        try:
            fig.savefig(
                buf, dpi=_DPI, transparent=True, format="png",
                bbox_inches="tight", pad_inches=0.06,
            )
        finally:
            plt.close(fig)

    # Crop to the actual glyph bounds + a tiny uniform margin: no clipping, but
    # no large empty gaps around the equation either.
    img = Image.open(io.BytesIO(buf.getvalue())).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        m = 4
        img = img.crop((
            max(0, bbox[0] - m), max(0, bbox[1] - m),
            min(img.width, bbox[2] + m), min(img.height, bbox[3] + m),
        ))
    out = io.BytesIO()
    img.save(out, format="PNG")
    png = out.getvalue()

    w_pt = img.width / _DPI * 72.0
    h_pt = img.height / _DPI * 72.0
    result = (png, w_pt, h_pt)
    if len(_cache) < 512:
        _cache[key] = result
    return result
