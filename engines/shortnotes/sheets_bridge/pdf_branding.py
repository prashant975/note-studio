"""PW branding for the studygram PDF: the logo on the page, the logo as the
watermark.

The renderer already ships both ingredients but neither is used: the centre
logo watermark is switched off by default, and when switched on it is drawn at
5.5% opacity, which is invisible in practice. The teacher's name is what gets
tiled across the page instead.

This module, applied at runtime so ``notesbot`` stays unmodified:

* replaces the tiled teacher name with the **PW logo watermark**, at an opacity
  you can actually see (``SHEETS_BRIDGE_WATERMARK_OPACITY``, default 0.10);
* stamps a small **PW logo** in the top-right corner of every page.

The teacher's signature still appears in the header and footer as before.
"""
from __future__ import annotations

import io
import os
import threading

_LOCK = threading.Lock()
_INSTALLED = False

DEFAULT_OPACITY = 0.10
LOGO_CORNER_MM = 11.0
LOGO_MARGIN_MM = 5.0


def watermark_opacity() -> float:
    raw = os.environ.get("SHEETS_BRIDGE_WATERMARK_OPACITY", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_OPACITY
    except ValueError:
        value = DEFAULT_OPACITY
    return max(0.02, min(0.5, value))


def _faded_png(path: str, opacity: float) -> bytes | None:
    """The logo with its alpha scaled down, trimmed of transparent padding."""
    try:
        from PIL import Image

        with Image.open(path) as source:
            logo = source.convert("RGBA")
        bounds = logo.getchannel("A").getbbox()
        if not bounds:
            return None
        logo = logo.crop(bounds)
        alpha = logo.getchannel("A").point(lambda value: max(1, round(value * opacity)) if value else 0)
        logo.putalpha(alpha)
        buffer = io.BytesIO()
        logo.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - branding must never break a render
        return None


def _corner_png(path: str) -> bytes | None:
    """The logo at full strength for the page corner."""
    try:
        from PIL import Image

        with Image.open(path) as source:
            logo = source.convert("RGBA")
        bounds = logo.getchannel("A").getbbox()
        if bounds:
            logo = logo.crop(bounds)
        buffer = io.BytesIO()
        logo.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return None


def install() -> bool:
    """Swap in the PW watermark and corner logo. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        from notesbot import notes_pdf

        path = str(notes_pdf.LOGO_WATERMARK_PATH)
        if not os.path.isfile(path):
            return False

        opacity = watermark_opacity()

        # 1) a watermark you can actually see
        def _load_logo_watermark() -> "bytes | None":
            return _faded_png(path, opacity)

        notes_pdf.NotesPDF._load_logo_watermark = staticmethod(_load_logo_watermark)

        # 2) the PW logo in the corner of every page
        corner = _corner_png(path)
        original_header = notes_pdf.NotesPDF.header

        def header(self) -> None:
            original_header(self)
            if not corner or getattr(self, "language", "") == "sanskrit":
                return
            try:
                self.image(
                    io.BytesIO(corner),
                    x=self.w - LOGO_CORNER_MM - LOGO_MARGIN_MM,
                    y=LOGO_MARGIN_MM,
                    w=LOGO_CORNER_MM,
                    h=LOGO_CORNER_MM,
                    keep_aspect_ratio=True,
                )
            except Exception:  # noqa: BLE001
                pass

        notes_pdf.NotesPDF.header = header
        _INSTALLED = True
        return True
