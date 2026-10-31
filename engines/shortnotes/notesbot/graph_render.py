"""Render a qualitative graph (from Gemini's structured spec) to a PNG.

Hand-drawn (xkcd) style using the Patrick Hand font so graphs match the
handwritten-notes theme. Curves come either as a y=f(x) expression in x or as
explicit [x, y] points. Expressions are evaluated in a restricted numpy
namespace (no builtins) so model output cannot execute arbitrary code.
"""
from __future__ import annotations

import io
import os
import re

import numpy as np

from .math_render import _lock, normalize_latex  # shared pyplot lock + LaTeX cleanup

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

_FONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fonts", "PatrickHand-Regular.ttf"
)
try:
    font_manager.fontManager.addfont(_FONT_PATH)
    _FONT_FAMILY = font_manager.FontProperties(fname=_FONT_PATH).get_name()
except Exception:  # noqa: BLE001
    _FONT_FAMILY = "DejaVu Sans"

_GRAPH_DPI = 200
_COLORS = ["#22459c", "#b91d47", "#1a7a3c", "#b86b14"]

_MATH_SPAN_RE = re.compile(r"\$([^$]+)\$")


def _norm_label(text: str, strip_math: bool = False) -> str:
    """Clean a label for matplotlib mathtext.

    Normalizes every $...$ span (word-forms like 'lambda_m^circ' -> proper
    LaTeX). With strip_math=True the $ markers are removed entirely — the
    plain-text fallback used if mathtext still rejects a label.
    """
    text = (text or "").strip()
    if strip_math:
        return _MATH_SPAN_RE.sub(lambda m: m.group(1), text).replace("\\", "")
    return _MATH_SPAN_RE.sub(
        lambda m: "$" + normalize_latex(m.group(1)) + "$", text
    )


_ALLOWED_RE = re.compile(r"^[0-9a-zA-Z_+\-*/().,^ ]*$")
_NS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "exp": np.exp,
    "log": np.log, "log10": np.log10, "sqrt": np.sqrt, "abs": np.abs,
    "pi": np.pi, "e": np.e,
}


def _safe_eval(expr: str, x: "np.ndarray") -> "np.ndarray":
    """Evaluate a y=f(x) maths expression from the model, safely.

    eval() here is deliberate and sandboxed: the string must pass a strict
    character whitelist (digits, operators, parens — no quotes, brackets or
    attribute access), every identifier must be in the fixed numpy whitelist
    (_NS) or be 'x', '__' is rejected, length is capped, and builtins are
    removed. With no attribute access, no subscripts, no strings and no names
    outside the whitelist, the expression cannot reach anything but arithmetic.
    """
    expr = (expr or "").strip().replace("^", "**")
    if not expr or len(expr) > 200 or "__" in expr or not _ALLOWED_RE.match(expr):
        raise ValueError("bad expression")
    for name in re.findall(r"[a-zA-Z_]+", expr):
        if name != "x" and name not in _NS:
            raise ValueError(f"name not allowed: {name}")
    y = eval(expr, {"__builtins__": {}}, {**_NS, "x": x})  # noqa: S307 - see docstring
    return np.broadcast_to(np.asarray(y, dtype=float), x.shape)


def render_graph(graph) -> "tuple[bytes, float, float] | None":
    """Return (png_bytes, width_pt, height_pt), or None if nothing plottable."""
    x_min = float(graph.x_min if graph.x_min is not None else 0.0)
    x_max = float(graph.x_max if graph.x_max is not None else 10.0)
    if not np.isfinite(x_min) or not np.isfinite(x_max) or x_max <= x_min:
        x_min, x_max = 0.0, 10.0
    x = np.linspace(x_min, x_max, 400)

    series = []
    for curve in graph.curves or []:
        label = (curve.label or "").strip()
        try:
            if (curve.expression or "").strip():
                y = _safe_eval(curve.expression, x)
                with np.errstate(all="ignore"):
                    y = np.where(np.isfinite(y), y, np.nan)
                series.append((x, y, label))
            elif curve.points and len(curve.points) >= 2:
                pts = [(float(p[0]), float(p[1])) for p in curve.points
                       if len(p) >= 2]
                pts.sort()
                series.append((np.array([p[0] for p in pts]),
                               np.array([p[1] for p in pts]), label))
        except Exception:  # noqa: BLE001 - skip a bad curve, keep the rest
            continue
    if not series:
        return None

    def _draw(strip_math: bool) -> "io.BytesIO":
        with plt.xkcd(scale=0.5, length=60, randomness=1.2):
            plt.rcParams["font.family"] = [_FONT_FAMILY, "DejaVu Sans"]
            fig, ax = plt.subplots(figsize=(4.4, 3.0), dpi=_GRAPH_DPI)
            try:
                for i, (xs, ys, label) in enumerate(series):
                    ax.plot(xs, ys, color=_COLORS[i % len(_COLORS)],
                            linewidth=2.2,
                            label=_norm_label(label, strip_math) or None)
                ax.set_xlabel(_norm_label(graph.x_label, strip_math),
                              fontsize=12, color="#222")
                ax.set_ylabel(_norm_label(graph.y_label, strip_math),
                              fontsize=12, color="#222")
                title = _norm_label(graph.title, strip_math)
                if title:
                    ax.set_title(title, fontsize=13, color="#14305f")
                ax.spines["top"].set_visible(False)
                ax.spines["right"].set_visible(False)
                ax.tick_params(labelsize=9, colors="#555")
                if getattr(graph, "qualitative", False):
                    ax.set_xticks([])
                    ax.set_yticks([])
                if any(lbl for _, _, lbl in series):
                    # Keep the key outside the plotting area: "best" can still
                    # cover a curve and obscure its sign or slope.
                    ax.legend(fontsize=10, frameon=False, loc="upper center",
                              bbox_to_anchor=(0.5, -0.20))
                out = io.BytesIO()
                fig.savefig(out, dpi=_GRAPH_DPI, format="png",
                            facecolor="white", bbox_inches="tight",
                            pad_inches=0.08)
                return out
            finally:
                plt.close(fig)

    with _lock:
        try:
            buf = _draw(strip_math=False)
        except Exception:  # noqa: BLE001 - bad label LaTeX: retry as plain text
            buf = _draw(strip_math=True)

    from PIL import Image

    with Image.open(io.BytesIO(buf.getvalue())) as img:
        w_px, h_px = img.size
    return buf.getvalue(), w_px / _GRAPH_DPI * 72.0, h_px / _GRAPH_DPI * 72.0
