"""Page-level checks that only a finished PDF can answer.

Scope discipline
----------------
Everything in this module exists because the standalone app has *only* the PDF.
The note-generation pipeline answers these same questions earlier and by other
means — it knows the DOCX has a footer part, it knows the notes were not empty,
it knows how wide a figure was told to print — and none of that knowledge
survives into a file someone hands you. Each function below therefore states
which original check it stands in for; none of them invents a new QC rule.

  page numbers / footer   ← run_local_output_checks: "DOCX has no footer part
                             (page numbers/footer missing)"
  blank pages             ← run_local_output_checks: "Every PDF page is blank",
                             narrowed to per-page so one blank page in ninety is
                             still reported
  text clipping           ← the visual check's "text cut off at page edges",
                             measured geometrically instead of asked of a model
  table clipping          ← the visual check's "table columns overflowing", and
                             src/table_sanitizer's 5.5 in content-column limit
  figure readability      ← src/qc/visual_qc: effective DPI below
                             VISUAL_MIN_READABLE_DPI

Findings come back as plain dicts in the shape the report writer consumes:
``{"check", "severity", "page", "location", "problem"}``.
"""
from __future__ import annotations

import re

from ..config import (
    DCN_CONTENT_WIDTH_IN,
    DCN_MARGIN_LEFT_IN,
    VISUAL_MIN_READABLE_DPI,
)
from ..pdf_layout_check import FOOTER_BAND_IN, HEADER_BAND_IN, POINTS_PER_INCH
from .extractor import PdfDocument, PageText

#: A span whose box starts before the page's left edge or ends past its right
#: edge is clipped by the page itself. 2 pt of slack for glyph side bearings —
#: the same reasoning (and the same order of magnitude) as X_TOLERANCE_PT in the
#: layout check.
_EDGE_TOLERANCE_PT = 2.0

#: Below this many characters a page carries no notes. The whole-document
#: version of this test in run_local_output_checks uses 5.
_NEAR_BLANK_CHARS = 40

#: A page whose text stops this far above the bottom margin has a large hole in
#: it — the symptom of a bad page break, which the visual check calls
#: "excessive blank space".
_BLANK_TAIL_IN = 3.5

#: Page numbers as they appear in a footer.
_PAGE_NUMBER_RE = re.compile(r"(?:^|\s)(?:page\s*)?(\d{1,4})(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)

_CONTENT_RIGHT_EDGE_PT = (DCN_MARGIN_LEFT_IN + DCN_CONTENT_WIDTH_IN) * POINTS_PER_INCH


def _finding(check: str, severity: str, page: int | None, problem: str,
             location: str = "") -> dict:
    return {
        "check": check,
        "severity": severity,
        "page": page,
        "location": location or (f"Page {page}" if page else ""),
        "problem": problem,
    }


def _footer_text(page: PageText) -> str:
    """Everything drawn in the bottom band of one page."""
    limit = page.height_pt - FOOTER_BAND_IN * POINTS_PER_INCH
    parts = [s["text"] for s in page.spans if (s["bbox"][1] + s["bbox"][3]) / 2.0 > limit]
    return " ".join(" ".join(parts).split())


def _header_text(page: PageText) -> str:
    limit = HEADER_BAND_IN * POINTS_PER_INCH
    parts = [s["text"] for s in page.spans if (s["bbox"][1] + s["bbox"][3]) / 2.0 < limit]
    return " ".join(" ".join(parts).split())


def check_page_numbers(document: PdfDocument) -> list[dict]:
    """Every page must carry a footer with its page number.

    Stands in for run_local_output_checks' DOCX footer-part test. A page with no
    footer at all is reported as missing its page number; a document where the
    numbers exist but do not run 1..n in order is reported once, because that is
    a single defect however many pages show it.
    """
    findings: list[dict] = []
    if not document.pages:
        return findings

    numbered: dict[int, int] = {}
    #: page_no -> why it has no number: no footer at all, or a footer with no
    #: number in it. Both are "the page number is missing" to a reviewer, and
    #: both were invisible to the original DOCX footer-part test, which could
    #: only tell whether a footer *existed*.
    unnumbered: dict[int, str] = {}
    for page in document.pages:
        footer = _footer_text(page)
        match = _PAGE_NUMBER_RE.search(footer) if footer else None
        if match:
            numbered[page.page_no] = int(match.group(1))
        elif not footer:
            unnumbered[page.page_no] = "The page has no footer, so its page number is missing."
        else:
            unnumbered[page.page_no] = (
                f"The page's footer carries no page number ({footer[:60]!r}).")

    if not numbered:
        # Nothing anywhere: one finding for the document, not one per page.
        findings.append(_finding(
            "Page number", "Medium", None,
            "No page in this PDF carries a page number in its footer."))
    elif unnumbered:
        # Some pages have one and some do not, which is the defect a reviewer
        # has to go and fix page by page — so one row per page, with its number.
        for page_no in sorted(unnumbered)[:40]:
            findings.append(_finding(
                "Page number", "Medium", page_no, unnumbered[page_no]))
        if len(unnumbered) > 40:
            findings.append(_finding(
                "Page number", "Medium", None,
                f"{len(unnumbered) - 40} further page(s) are also missing their "
                "page number (list truncated)."))

    if numbered and len(numbered) > 1:
        out_of_order = [p for p, printed in sorted(numbered.items())
                        if printed != p and printed != p - 1]
        # printed == p - 1 is the ordinary cover-page offset and is not a defect.
        if len(out_of_order) > len(numbered) * 0.2:
            findings.append(_finding(
                "Page number", "Low", None,
                f"Printed page numbers do not follow the page order "
                f"({len(out_of_order)} of {len(numbered)} pages disagree)."))
    return findings


def check_blank_pages(document: PdfDocument) -> list[dict]:
    """Pages with no readable content, and pages that stop far too early."""
    findings: list[dict] = []
    for page in document.pages:
        stripped = page.text.strip()
        if len(stripped) < 5 and not page.image_count:
            findings.append(_finding(
                "Blank page", "High", page.page_no,
                "The page is completely blank."))
            continue
        if len(stripped) < _NEAR_BLANK_CHARS and not page.image_count:
            findings.append(_finding(
                "Blank page", "Medium", page.page_no,
                f"The page carries almost nothing ({len(stripped)} characters): "
                f"{' '.join(stripped.split())[:60]!r}."))
            continue
        if not page.spans:
            continue
        lowest = max((s["bbox"][3] for s in page.spans), default=0.0)
        gap_in = (page.height_pt - FOOTER_BAND_IN * POINTS_PER_INCH - lowest) / POINTS_PER_INCH
        if gap_in > _BLANK_TAIL_IN and page.page_no != document.page_count:
            findings.append(_finding(
                "Page break", "Low", page.page_no,
                f"The page stops {gap_in:.1f} in above the bottom margin — a large "
                "blank gap, usually a bad page break."))
    return findings


def check_text_clipping(document: PdfDocument) -> list[dict]:
    """Text whose box runs off the edge of the page — cut off when printed."""
    findings: list[dict] = []
    for page in document.pages:
        worst: tuple[float, str] | None = None
        for span in page.spans:
            x0, _y0, x1, _y1 = span["bbox"]
            over = max(x1 - (page.width_pt - _EDGE_TOLERANCE_PT), _EDGE_TOLERANCE_PT - x0)
            if over > 0 and (worst is None or over > worst[0]):
                worst = (over, " ".join(span["text"].split()))
        if worst:
            over, text = worst
            findings.append(_finding(
                "Text clipping", "High", page.page_no,
                f"Text runs {over / POINTS_PER_INCH:.2f} in off the edge of the page "
                f"and is cut off: \"{text[:60]}\"."))
    return findings


def check_table_clipping(document: PdfDocument) -> list[dict]:
    """Tables wider than the 5.5 in content column, or off the page.

    The note-generation project rewrites such a table as bullets before the DOCX
    is written (src/table_sanitizer.py). By the time it is a PDF the row is
    already clipped, so here it can only be reported.
    """
    findings: list[dict] = []
    for page in document.pages:
        if not page.tables_detected:
            continue
        for x0, _y0, x1, _y1 in page.table_bboxes:
            if x1 > page.width_pt - _EDGE_TOLERANCE_PT:
                findings.append(_finding(
                    "Table clipping", "High", page.page_no,
                    f"A table reaches {x1 / POINTS_PER_INCH:.2f} in, past the page "
                    "edge — its right-hand column is cut off."))
            elif x1 > _CONTENT_RIGHT_EDGE_PT + _EDGE_TOLERANCE_PT:
                findings.append(_finding(
                    "Table clipping", "Medium", page.page_no,
                    f"A table reaches {x1 / POINTS_PER_INCH:.2f} in, past the "
                    f"{(_CONTENT_RIGHT_EDGE_PT / POINTS_PER_INCH):.1f} in content "
                    "column, and runs into the notes-box column."))
    return findings


def check_figures(document: PdfDocument) -> list[dict]:
    """Figures too low-resolution to read at the size they print.

    Same rule and same threshold as src/qc/visual_qc.py in the note-generation
    project, measured here from the PDF's own image placement instead of from
    the visual registry it no longer has.
    """
    findings: list[dict] = []
    try:
        import fitz
    except Exception:
        return findings
    try:
        doc = fitz.open(str(document.path))
    except Exception:
        return findings
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            try:
                infos = page.get_image_info(hashes=False)
            except Exception:
                continue
            for info in infos:
                bbox = info.get("bbox") or (0, 0, 0, 0)
                display_width_in = (bbox[2] - bbox[0]) / POINTS_PER_INCH
                width_px = int(info.get("width") or 0)
                if display_width_in <= 0.3 or width_px <= 0:
                    continue
                effective_dpi = width_px / display_width_in
                if effective_dpi < VISUAL_MIN_READABLE_DPI:
                    findings.append(_finding(
                        "Figure quality", "Medium", index + 1,
                        f"A figure prints at ~{effective_dpi:.0f} DPI (below "
                        f"{VISUAL_MIN_READABLE_DPI}); small labels may be unreadable."))
    finally:
        doc.close()
    return findings


def check_page_geometry(document: PdfDocument) -> list[dict]:
    """Pages that are not all the same size — a sign of a merged or spliced file."""
    findings: list[dict] = []
    sizes = {(round(p.width_pt), round(p.height_pt)) for p in document.pages}
    if len(sizes) > 1:
        described = ", ".join(
            f"{w / POINTS_PER_INCH:.1f}x{h / POINTS_PER_INCH:.1f} in"
            for w, h in sorted(sizes)[:4])
        findings.append(_finding(
            "Page geometry", "Low", None,
            f"The PDF mixes {len(sizes)} page sizes ({described}) — pages from "
            "more than one source may have been merged."))
    return findings


def run_page_analysis(document: PdfDocument) -> list[dict]:
    """Every page-level check, in the order a reviewer cares about them.

    Each check is isolated: one that trips on a pathological file contributes no
    findings and the rest still run, exactly as run_deterministic_checks does.
    """
    findings: list[dict] = []
    for check in (check_text_clipping, check_table_clipping, check_blank_pages,
                  check_page_numbers, check_figures, check_page_geometry):
        try:
            findings.extend(check(document))
        except Exception:
            continue
    return findings
