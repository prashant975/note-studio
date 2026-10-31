"""Prove the DCN page layout holds in an already-rendered notes PDF.

Copied from the note-generation project's ``src/docx_render_check.py``. That
module converted a freshly written DOCX to PDF and then walked the text spans;
this app is handed the PDF directly, so the LibreOffice/Word conversion half is
gone and everything below it — the geometry constants, the tolerances, and
``_scan_page_for_overflow`` — is the original code, unchanged.

WHY it exists: the DCN writer reserves a 2.5 in right margin so the repeating
"Space for Notes" box never sits on top of the body text. Nothing in the file
guarantees that — a wide table, an oversized picture or a long unbreakable token
is laid out by the converter, so a column overflow is invisible until someone
opens the file. This module walks every page looking for body text that crossed
x = 6.0 in (0.5 in margin + 5.5 in column), and for text that landed in the
reserved column outside the notes box's own rectangle.

It is advisory, never authoritative, and it never raises: any failure comes back
as ``ok=False`` plus warnings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import DCN_CONTENT_WIDTH_IN, DCN_MARGIN_LEFT_IN

POINTS_PER_INCH = 72.0

# Body text must stop where the content column stops (0.5 + 5.5 = 6.0 in).
BODY_RIGHT_EDGE_IN = DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN
BODY_RIGHT_EDGE_PT = BODY_RIGHT_EDGE_IN * POINTS_PER_INCH
# Converters round glyph positions, so a span that *starts* a hair past the
# column edge is noise; 2 pt (~0.03 in) keeps that off the warning list.
X_TOLERANCE_PT = 2.0
# A span that *ends* past the edge is the common real failure (a wide table cell
# or an unwrappable line runs from the left margin into the notes column).
#
# The tolerance is set from the reference document rather than from theory: its
# justified lines routinely report a bbox ending at ~6.10 in even though the
# column stops at 6.00 in, because a glyph's bounding box includes side bearings
# and overhang that its advance width does not. Measuring the ground truth and
# flagging it would make the check worse than useless.
#
# What the check actually exists to prevent is text *colliding with the notes
# box*, whose left edge is at 6.26 in. Allowing up to 6.20 in therefore keeps a
# margin of safety while accepting every layout the reference itself produces.
X_END_TOLERANCE_PT = 0.20 * POINTS_PER_INCH

# Header/footer furniture legitimately uses the full page width, so the top and
# bottom 1.0 in bands are excluded from the overlap scan.
HEADER_BAND_IN = 1.0
FOOTER_BAND_IN = 1.0

# The notes-box caption lives inside the reserved column by design. Matched
# whole-span, not as a substring: body text that merely mentions the notes box
# ("...runs under the Space for Notes column") is exactly the overflow we want
# to catch, so a substring test would hide the defect it was written to find.
#
# Text matching alone is not enough, though: PDF extractors split the caption
# into several spans ("Space for" + "Notes"), so the fragments never match. The
# geometry below is the real defence — see _scan_page_for_overflow.
_IGNORED_SPAN_TEXTS = ("space for notes",)

# The reserved column is not empty space: the anchored notes box is drawn there
# on every page. Measured from the reference header — the box is anchored
# 5.76 in from the start of the text column (which itself starts at the 0.5 in
# left margin) and is 2.052 in wide.
NOTES_BOX_LEFT_IN = 0.5 + 5.76
NOTES_BOX_RIGHT_IN = NOTES_BOX_LEFT_IN + 2.052
# Slack for the box's own border and padding. Kept small on purpose: too much
# and the exclusion swallows the whole reserved column, which would silently
# disable the "something was drawn where nothing should be" guard below.
NOTES_BOX_SLACK_IN = 0.12



@dataclass
class RenderCheck:
    """Outcome of a render pass.

    ``ok`` means: a PDF was produced, it had at least one page, and no body span
    crossed the content column. Other warnings (missing logo, converter chatter)
    are informational and do not clear ``ok``.
    """

    ok: bool
    pdf_path: Path | None
    page_images: list[Path]
    page_count: int
    overlap_pages: list[int]
    warnings: list[str] = field(default_factory=list)





def _pages_to_render(page_count: int, max_pages: int) -> list[int]:
    """0-based page indices to rasterise: first and last, then fill forward."""
    if page_count <= 0 or max_pages <= 0:
        return []
    chosen = [0]
    last = page_count - 1
    if last not in chosen and len(chosen) < max_pages:
        chosen.append(last)
    nxt = 1
    while len(chosen) < max_pages and nxt < last:
        chosen.append(nxt)
        nxt += 1
    return sorted(set(chosen))


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def _is_ignored_span(text: str) -> bool:
    flat = _normalise(text).strip(" :.-")
    if not flat:
        return True
    return flat in _IGNORED_SPAN_TEXTS


def _scan_page_for_overflow(page) -> list[tuple[float, str]]:
    """Body spans on one page that cross the content column.

    Returns (worst x in points, text) pairs.

    A span offends when it **crosses** the column edge — it starts inside the
    writing column and runs on into the reserved one. Text that sits wholly
    inside the reserved column is not an overflow: that is where the anchored
    "Space for Notes" box is drawn on every page, and flagging it made this
    check useless (it failed the reference document itself on all 99 pages,
    because PDF extractors split the caption into fragments that never matched
    the ignore-list). Text wholly inside the reserved column is still reported
    when it falls outside the notes box's own rectangle, which is the case that
    would mean something really was pushed over there.

    Spans inside the header/footer bands are skipped because that furniture is
    meant to span the full page width.
    """
    header_limit = HEADER_BAND_IN * POINTS_PER_INCH
    footer_limit = page.rect.height - FOOTER_BAND_IN * POINTS_PER_INCH
    start_limit = BODY_RIGHT_EDGE_PT + X_TOLERANCE_PT
    end_limit = BODY_RIGHT_EDGE_PT + X_END_TOLERANCE_PT
    box_left = (NOTES_BOX_LEFT_IN - NOTES_BOX_SLACK_IN) * POINTS_PER_INCH
    box_right = (NOTES_BOX_RIGHT_IN + NOTES_BOX_SLACK_IN) * POINTS_PER_INCH

    offenders: list[tuple[float, str]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if _is_ignored_span(text):
                    continue
                x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                centre_y = (y0 + y1) / 2.0
                if centre_y < header_limit or centre_y > footer_limit:
                    continue
                if x0 <= start_limit:
                    # Starts in the writing column: an overflow iff it runs past
                    # the edge by more than a trailing-space width.
                    if x1 > end_limit:
                        offenders.append((x1, " ".join(text.split())))
                elif not (box_left <= x0 and x1 <= box_right):
                    # Wholly inside the reserved column but outside the notes
                    # box — nothing legitimate is drawn there.
                    offenders.append((max(x0, x1), " ".join(text.split())))
    return offenders



def verify_pdf_layout(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int = 110,
    max_pages: int = 3,
) -> RenderCheck:
    """Walk every page of ``pdf_path`` and flag content-column overflows.

    Same body as the original ``render_and_verify``, with the DOCX-to-PDF
    conversion removed: the PDF is the input here, so there is nothing to
    convert and nothing that can fail before the scan.
    """
    warnings: list[str] = []
    page_images: list[Path] = []
    try:
        pdf_path = Path(pdf_path)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not pdf_path.exists():
            return RenderCheck(ok=False, pdf_path=None, page_images=[], page_count=0,
                               overlap_pages=[],
                               warnings=[f"PDF not found for layout check: {pdf_path}"])

        try:
            import fitz  # PyMuPDF
        except Exception as exc:
            warnings.append(f"PyMuPDF is not available, skipping layout verification: {exc}")
            return RenderCheck(ok=False, pdf_path=pdf_path, page_images=[], page_count=0,
                               overlap_pages=[], warnings=warnings)

        overlap_pages: list[int] = []
        with fitz.open(str(pdf_path)) as doc:
            page_count = doc.page_count
            if page_count == 0:
                warnings.append("The PDF has no pages — the file may be empty or corrupt.")
                return RenderCheck(ok=False, pdf_path=pdf_path, page_images=[],
                                   page_count=0, overlap_pages=[], warnings=warnings)

            for index in range(page_count):
                page = doc.load_page(index)
                offenders = _scan_page_for_overflow(page)
                if not offenders:
                    continue
                overlap_pages.append(index + 1)
                worst_x, text = max(offenders, key=lambda item: item[0])
                snippet = text[:60] + ("..." if len(text) > 60 else "")
                warnings.append(
                    f"Page {index + 1}: body text runs into the notes-box column "
                    f"(reaches {worst_x / POINTS_PER_INCH:.2f} in, limit {BODY_RIGHT_EDGE_IN:.1f} in): "
                    f'"{snippet}"'
                )

            first_page = doc.load_page(0)
            try:
                has_image = bool(first_page.get_images(full=True))
            except Exception:
                has_image = True  # do not invent a logo warning we cannot substantiate
            if not has_image:
                warnings.append("Page 1 contains no image — the PW logo may be missing from the header.")

            zoom = float(dpi) / POINTS_PER_INCH
            matrix = fitz.Matrix(zoom, zoom)
            for index in _pages_to_render(page_count, max_pages):
                try:
                    pixmap = doc.load_page(index).get_pixmap(matrix=matrix)
                    image_path = out_dir / f"{pdf_path.stem}_p{index + 1}.png"
                    pixmap.save(str(image_path))
                    page_images.append(image_path)
                except Exception as exc:
                    warnings.append(f"Could not rasterise page {index + 1}: {exc}")

        return RenderCheck(ok=not overlap_pages, pdf_path=pdf_path, page_images=page_images,
                           page_count=page_count, overlap_pages=overlap_pages,
                           warnings=warnings)
    except Exception as exc:
        warnings.append(f"Layout check failed unexpectedly: {exc}")
        return RenderCheck(ok=False, pdf_path=pdf_path, page_images=page_images,
                           page_count=0, overlap_pages=[], warnings=warnings)
