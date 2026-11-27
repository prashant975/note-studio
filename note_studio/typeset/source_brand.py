"""The header name and logo a lecture PDF prints on its pages (27 November 2026).

A category can take its B.Sc.-style notes' header from each lecture instead of fixed words
(category.json "branding": {"from_pdf": true}; Settings -> Categories). The text the PDF repeats at
the top of its pages becomes the notes' header: its left part is the name (also the footer's), its
right part the header's right words. A picture it repeats up there becomes the notes' corner logo and
faded watermark, drawn as the PW logo is (render.Branding.logo).

Nothing is guessed. Text or a picture counts only when it repeats at the same place on most pages
(a one-page PDF: small text in the page's top strip); page numbers never count. A PDF that repeats
nothing up there leaves the category's own words, and no logo means the category's own choice.
The same header lines are then left out of the notes' content (source.extract furniture), since
they are now printed in the notes' own header.
"""
from __future__ import annotations

import math
import re
from collections import Counter

SAMPLE_PAGES = 12           # pages looked at
TOP_TEXT = 0.10             # a running header's text ends within this share of the page height
TOP_LOGO = 0.15             # a header logo ends within this share of the page height
ONE_PAGE_TOP = 0.045        # a one-page PDF: only text this close to the top
SHARE = 0.6                 # a header repeats on at least this share of the pages looked at
MAX_HEADER_SIZE = 13.0      # pt: bigger text up there is a title, not a running header
MAX_NAME = 80               # letters kept of one side's words
LOGO_MAX_W, LOGO_MAX_H = 0.45, 0.14     # of the page: a wider or taller picture is a banner or a scan
LOGO_MIN = 12.0             # pt: a smaller picture is a bullet or a rule
LOGO_PIXELS = 1200          # the logo kept is at most this many pixels wide or tall
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?[-–—(]?\s*\d{1,4}\s*[-–—)]?(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


def normal(text: str) -> str:
    """Whitespace and typographic ligatures only (as source.normal)."""
    return " ".join(str(text or "").translate(_LIGATURES).split())


def page_number(text: str) -> bool:
    return bool(_PAGE_NUMBER.match(normal(text)))


def _side(x0: float, x1: float, width: float) -> str:
    middle = (x0 + x1) / 2
    return "left" if middle < width * 0.4 else "right" if middle > width * 0.6 else "centre"


def _header_lines(page) -> list[tuple[str, str, float, float]]:
    """(side, text, top, left) of every small text line in the page's top strip."""
    width, height = page.rect.width, page.rect.height
    found = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            x0, y0, x1, y1 = line["bbox"]
            text = normal("".join(s.get("text", "") for s in line.get("spans", [])))   # spaces may be own spans
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not text or y1 > height * TOP_TEXT or page_number(text):
                continue
            if max(s.get("size", 0) for s in spans) > MAX_HEADER_SIZE:
                continue
            found.append((_side(x0, x1, width), text, round(y0, 1), round(x0, 1)))
    return found


def _header_pictures(page, top: float = TOP_LOGO) -> list[tuple[tuple, tuple]]:
    """((xref, rounded box), box) of every logo-sized picture in the page's top strip."""
    width, height = page.rect.width, page.rect.height
    found = []
    for info in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        w, h = x1 - x0, y1 - y0
        if not info.get("xref") or y1 > height * top or y0 < -2:
            continue
        if w < LOGO_MIN or h < LOGO_MIN or w > width * LOGO_MAX_W or h > height * LOGO_MAX_H:
            continue
        found.append(((info["xref"], (round(x0 / 6), round(y0 / 6), round(w / 6), round(h / 6))),
                      (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))))
    return found


def _logo_png(pdf, xref: int) -> bytes | None:
    """The picture as a PNG with its own transparency, RGB, at most LOGO_PIXELS."""
    import fitz

    try:
        pix = fitz.Pixmap(pdf, xref)
        smask = pdf.xref_get_key(xref, "SMask")
        if smask[0] == "xref" and not pix.alpha:
            pix = fitz.Pixmap(pix, fitz.Pixmap(pdf, int(smask[1].split()[0])))
        if pix.colorspace and pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        data = pix.tobytes("png")
    except Exception:  # noqa: BLE001 - a picture that cannot be read is no logo
        return None
    from PIL import Image
    import io

    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGBA")
            if max(image.size) > LOGO_PIXELS:
                image.thumbnail((LOGO_PIXELS, LOGO_PIXELS))
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def read(pdf_bytes: bytes) -> dict:
    """{"left", "right", "lines", "logo" (PNG bytes or None), "logo_box" (its place on the page, pt),
    "pages"}: the PDF's running header. Empty words and no logo when it has none (or cannot be opened)."""
    import fitz

    empty = {"left": "", "right": "", "lines": [], "logo": None, "logo_box": None, "pages": 0}
    try:
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 - the notes run reports an unreadable PDF itself
        return empty
    with pdf:
        if pdf.needs_pass or not len(pdf):
            return empty
        pages = [pdf[n] for n in range(min(SAMPLE_PAGES, len(pdf)))]
        need = max(2, math.ceil(SHARE * len(pages))) if len(pages) > 1 else 1
        texts, places, pictures, boxes = Counter(), {}, Counter(), {}
        for page in pages:
            lines = _header_lines(page)
            found = _header_pictures(page, ONE_PAGE_TOP * 2 if len(pages) == 1 else TOP_LOGO)
            if len(pages) == 1:
                lines = [line for line in lines if line[2] < page.rect.height * ONE_PAGE_TOP]
            for side, text, top, left in lines:
                places.setdefault((side, text), (top, left))
            for key in {(side, text) for side, text, _, _ in lines}:       # once per page
                texts[key] += 1
            for key, box in found:
                boxes.setdefault(key, box)
            for key in {key for key, _ in found}:
                pictures[key] += 1
        running = sorted((key for key, count in texts.items() if count >= need), key=lambda k: places[k])
        words = {side: normal(" ".join(text for s, text in running if s == side))[:MAX_NAME]
                 for side in ("left", "centre", "right")}
        left, right = words["left"], words["right"]
        if words["centre"]:
            if not left:
                left = words["centre"]
            elif not right:
                right = words["centre"]
        logo, logo_box = None, None
        repeated = [(count, key) for key, count in pictures.items() if count >= need]
        if repeated:
            _, key = max(repeated, key=lambda item: (item[0], item[1][1][2] * item[1][1][3]))
            logo = _logo_png(pdf, key[0])
            logo_box = boxes[key] if logo else None
        return {"left": left, "right": right, "lines": [text for _, text in running], "logo": logo,
                "logo_box": logo_box, "pages": len(pages)}


def furniture(found: dict) -> dict:
    """What to leave out of the notes' content, now in their own header: "words" (each repeated header
    line, each side, and both sides together, as a reader may join them) and "logo_box" (the logo's place)."""
    words = {normal(t) for t in found.get("lines") or [] if normal(t)}
    left, right = normal(found.get("left")), normal(found.get("right"))
    words |= {w for w in (left, right, normal(f"{left} {right}"), normal(f"{right} {left}")) if w}
    return {"words": words, "logo_box": tuple(found["logo_box"]) if found.get("logo") and found.get("logo_box") else None}


def is_logo(box, furniture_found: dict | None) -> bool:
    """A picture at the header logo's place (within 4 pt)."""
    known = (furniture_found or {}).get("logo_box")
    return bool(known) and all(abs(a - b) < 4 for a, b in zip(tuple(box), known))


def _key(text: str) -> str:
    """Letters and digits only: "CSIR NET – Life Sciences" and "CSIR NET - Life Sciences" are one line."""
    return re.sub(r"\W+", "", normal(text)).casefold()


def is_furniture(text: str, top_share: float, words: set[str]) -> bool:
    """A content line that is the page's running header (now in the notes' own header) or a bare page
    number at the page's top or bottom. top_share: where the line starts, as a share of the page height."""
    text = normal(text)
    if not text or not words:
        return False
    if (top_share < TOP_TEXT + 0.02 or top_share > 0.92) and page_number(text):
        return True
    return top_share < TOP_TEXT + 0.02 and bool(_key(text)) and _key(text) in {_key(w) for w in words}
