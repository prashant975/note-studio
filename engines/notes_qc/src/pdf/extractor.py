"""Read an already-generated UPSC notes PDF: text, pages, rendered images.

This is the standalone app's input layer — the piece that replaces "extract the
lecture deck, transcribe the video, generate notes" in the original pipeline.
Everything downstream of here is the note-generation project's own QC code.

The rendering call is the one from ``src/extract_pdf.py`` in that project
(``page.get_pixmap`` at a fixed scale, saved as PNG per page), so the images the
visual check sees are produced the same way.

Nothing here writes to the input PDF. The file is opened read-only and closed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import SlideData
from ..utils import ensure_dir, first_nonempty_line
from .text_integrity import TextIntegrity, check_text_integrity

#: Devanagari block, used to decide the notes' language and to spot mixed script.
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass
class PageText:
    """One page of the PDF as both text and geometry."""

    page_no: int                       # 1-based, the printed page number's page
    text: str = ""
    width_pt: float = 0.0
    height_pt: float = 0.0
    image_path: Path | None = None
    image_count: int = 0
    #: Text spans as (x0, y0, x1, y1, size, color, flags, text) — the layout
    #: checks and the marker reconstruction both read these.
    spans: list[dict] = field(default_factory=list)
    #: Bounding boxes of tables detected by PyMuPDF, in page points.
    table_bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    tables_detected: bool = False      # False = detection unavailable, not "no tables"

    @property
    def is_blank(self) -> bool:
        return len(self.text.strip()) < 5


@dataclass
class PdfDocument:
    """Everything read out of one PDF, in one place."""

    path: Path
    page_count: int = 0
    pages: list[PageText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    encrypted: bool = False
    #: Whether the text layer can be read back faithfully. False on Hindi PDFs
    #: whose embedded font subset returns Devanagari in visual order — see
    #: src/pdf/text_integrity.py. The AI stages read the pages as images when
    #: this is False, because the words on the page are right and only the
    #: copyable text behind them is scrambled.
    integrity: TextIntegrity = field(default_factory=TextIntegrity)

    @property
    def text_layer_reliable(self) -> bool:
        return self.integrity.reliable

    @property
    def text(self) -> str:
        """The whole document as plain text, pages separated by blank lines."""
        return "\n\n".join(p.text for p in self.pages)

    @property
    def paged_text(self) -> str:
        """The same text with a ``[Page n]`` line before each page.

        This is what the AI error-report pass reads: it is the only way a model
        can say *where* a wrong statement sits, which is what the original
        report's slide numbers did.
        """
        return "\n\n".join(f"[Page {p.page_no}]\n{p.text}".rstrip() for p in self.pages)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def language_code(self) -> str:
        """"hi" or "en" for this document.

        Uses the same 15% Devanagari threshold as the note-generation project's
        ``src/language_detect.hindi_ratio``; imported rather than reimplemented
        would pull in the transcript-specific machinery this app has no use for,
        so the threshold constant is honoured here directly.
        """
        from ..language_detect import detect_language

        return detect_language(self.text, default="en")

    @property
    def has_mixed_script(self) -> bool:
        return bool(_DEVANAGARI_RE.search(self.text)) and bool(_LATIN_RE.search(self.text))

    def as_slides(self) -> list[SlideData]:
        """One SlideData per page.

        The deterministic checks and the independent AI review both take a slide
        list. In this app a page *is* the unit of location, so page n becomes
        slide n and every "slide 7" a check reports means "page 7".
        """
        return [
            SlideData(
                slide_no=p.page_no,
                heading=first_nonempty_line(p.text),
                text=p.text,
                cleaned_text=p.text,
                image_path=p.image_path,
                source_type="pdf",
                metadata={"page_index": p.page_no - 1},
            )
            for p in self.pages
        ]


def _span_color_hex(color: int) -> str:
    try:
        return f"{int(color) & 0xFFFFFF:06X}"
    except (TypeError, ValueError):
        return "000000"


def extract_pdf(
    pdf_path: Path,
    run_dir: Path,
    *,
    render_scale: float = 2.0,
    render_pages: bool = True,
    detect_tables: bool = True,
) -> PdfDocument:
    """Open ``pdf_path`` read-only and return everything the QC stages need.

    ``render_pages`` writes one PNG per page under ``run_dir/rendered``. It is
    on by default because the visual check needs them; a caller that only wants
    text can turn it off and skip the slowest part of the read.
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:  # pragma: no cover - pymupdf is a hard requirement
        raise RuntimeError("PyMuPDF is not installed. Run: pip install pymupdf") from e

    pdf_path = Path(pdf_path)
    doc_result = PdfDocument(path=pdf_path)
    render_dir = ensure_dir(Path(run_dir) / "rendered") if render_pages else None
    matrix = fitz.Matrix(render_scale, render_scale)

    doc = fitz.open(str(pdf_path))
    try:
        if doc.needs_pass:
            doc_result.encrypted = True
            doc_result.warnings.append(
                "The PDF is password-protected, so nothing could be read out of it.")
            return doc_result
        doc_result.page_count = doc.page_count
        doc_result.metadata = dict(doc.metadata or {})

        for index, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            rect = page.rect
            entry = PageText(page_no=index, text=text,
                             width_pt=float(rect.width), height_pt=float(rect.height))

            try:
                data = page.get_text("dict")
                for block in data.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            span_text = span.get("text", "")
                            if not span_text.strip():
                                continue
                            bbox = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                            entry.spans.append({
                                "text": span_text,
                                "bbox": [float(v) for v in bbox],
                                "size": float(span.get("size", 0.0)),
                                "color": _span_color_hex(span.get("color", 0)),
                                "font": str(span.get("font", "")),
                                "flags": int(span.get("flags", 0)),
                            })
            except Exception as e:
                doc_result.warnings.append(
                    f"Page {index}: text layout could not be read ({e}); the "
                    "structure checks skip this page.")

            try:
                entry.image_count = len(page.get_images(full=True))
            except Exception:
                entry.image_count = 0

            if detect_tables:
                try:
                    finder = page.find_tables()
                    entry.table_bboxes = [tuple(float(v) for v in t.bbox)
                                          for t in getattr(finder, "tables", [])]
                    entry.tables_detected = True
                except Exception:
                    # Older PyMuPDF, or a page it cannot analyse. Recorded as
                    # "not detected" rather than "no tables" so the checks that
                    # depend on it can say they did not run.
                    entry.tables_detected = False

            if render_dir is not None:
                try:
                    image_path = render_dir / f"page_{index:03d}.png"
                    page.get_pixmap(matrix=matrix, alpha=False).save(str(image_path))
                    entry.image_path = image_path
                except Exception as e:
                    doc_result.warnings.append(
                        f"Page {index} could not be rendered to an image ({e}); "
                        "the visual check skips it.")

            doc_result.pages.append(entry)
    finally:
        doc.close()

    doc_result.integrity = check_text_integrity(doc_result.text)
    if doc_result.page_count and not doc_result.text.strip():
        doc_result.warnings.append(
            "The PDF has no text layer at all — it is probably a scan. Only the "
            "visual checks can see this file; the text-based checks cannot.")
    return doc_result
