"""Draw the planned diagrams as SVG (via the text model) — pure vector.

Asking the *text* model for SVG beats an image model for scientific figures:
labels are real text (never garbled or misspelt), lines stay crisp at any
zoom, the background is genuinely transparent, and the payload is a few KB
instead of a megabyte. fpdf2 draws the SVG straight into the page — no
rasteriser and no system libraries needed.
"""
from __future__ import annotations

import base64
import re

from . import config

_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S | re.I)
# Anything that could make the SVG do more than draw.
_UNSAFE_RE = re.compile(
    r"<\s*(script|foreignObject|iframe|image)\b|href\s*=\s*[\"']\s*http", re.I)
_FONT_RE = re.compile(r'font-family\s*=\s*"[^"]*"', re.I)
_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*"([\d.\-\s]+)"', re.I)

PROMPT = """SOURCE LOCK: use the attached teacher PDF as the sole factual and visual
authority. Draw ONLY components, relationships, arrows, values, and labels
explicitly present or taught there and named in the diagram specification.
Never infer, complete, correct, or add a standard part. Omit uncertain detail.

Draw this as ONE self-contained SVG diagram for handwritten-style student notes.

DIAGRAM: {desc}

STRICT RULES
- Output ONLY the SVG markup: a single <svg> element, no markdown fences, no
  explanation before or after.
- Start with: <svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">
  (you may choose another viewBox, keep it roughly 4:3).
- NO background rectangle — the notes paper must show through.
- Hand-drawn feel: stroke="#23283c", stroke-width="2.5",
  stroke-linecap="round", slightly irregular coordinates.
- Soft coloured-pencil fills, muted: #cfe3f5 blue, #f7d9e3 pink, #d8ecd9
  green, #f6e2c0 sand, #d9d9de grey, with fill-opacity="0.55".
- LABEL every source-named part requested in DIAGRAM with
  <text ... font-family="Patrick" font-size="13" fill="#23283c">, and join
  each label to its part with a thin leader line. Copy supplied label spelling
  exactly, even when unusual. Expand the viewBox or spacing rather than omit a
  source label; keep labels outside shapes and avoid overlaps.
- Use ONLY plain shapes (rect, circle, ellipse, line, polyline, polygon, path)
  and text. No scripts, no external images, no CSS classes, no filters, no
  gradients, no transforms.
"""


def enabled() -> bool:
    return bool(config.NOTES_IMAGES and config.litellm_key())


def clean(svg: str) -> "str | None":
    """Extract, sanitise and normalise model-written SVG."""
    m = _SVG_RE.search(svg or "")
    if not m:
        return None
    body = m.group(0)
    if _UNSAFE_RE.search(body):
        return None
    if "xmlns" not in body[:200]:
        body = body.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    # Labels must use the font actually embedded in the PDF.
    body = _FONT_RE.sub('font-family="Patrick"', body)
    if "viewBox" not in body:
        body = body.replace("<svg", '<svg viewBox="0 0 400 300"', 1)
    return body


def aspect(svg: str) -> float:
    """height / width of the SVG's viewBox (defaults to 3:4)."""
    m = _VIEWBOX_RE.search(svg or "")
    if m:
        try:
            _, _, vw, vh = [float(v) for v in m.group(1).split()]
            if vw > 0 and vh > 0:
                return vh / vw
        except ValueError:
            pass
    return 0.75


def ask(desc: str, source_pdf: bytes) -> "str | None":
    """Ask the model for one SVG while attaching its exact source chunk."""
    if (
        not isinstance(source_pdf, bytes)
        or not source_pdf
        or len(source_pdf) > config.GEMINI_INLINE_MAX_BYTES
    ):
        return None
    from .gemini_client import _lite_client, _lite_model

    pdf_data = "data:application/pdf;base64," + base64.b64encode(source_pdf).decode()
    resp = _lite_client().chat.completions.create(
        model=_lite_model(),
        messages=[{"role": "user", "content": [
            {"type": "file", "file": {
                "filename": "source-chunk.pdf",
                "file_data": pdf_data,
            }},
            {"type": "text", "text": PROMPT.format(desc=desc.strip())},
        ]}],
        temperature=0.4,
        max_tokens=16384,
    )
    if not resp.choices:
        return None
    return clean(resp.choices[0].message.content or "")


def try_svg(desc: str, source_pdf: bytes) -> "str | None":
    """SVG markup for `desc`, or None so the caller can fall back."""
    if (
        not isinstance(source_pdf, bytes)
        or not source_pdf
        or len(source_pdf) > config.GEMINI_INLINE_MAX_BYTES
        or not enabled()
    ):
        return None
    try:
        return ask(desc, source_pdf)
    except Exception as exc:  # noqa: BLE001 - never break notes over a diagram
        print(
            "[notesbot] Grounded SVG unavailable "
            f"({type(exc).__name__}); using a manual fallback."
        )
        return None
