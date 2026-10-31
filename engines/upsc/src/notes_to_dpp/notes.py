from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from .models import Language, LanguageDetection, NoteDocument, NotePage


class NoteExtractionError(ValueError):
    """Raised when uploaded notes cannot be safely extracted."""


ProgressCallback = Callable[[int, int, str], None]

_LATIN_TOKEN = re.compile(r"[A-Za-z]+")

_BOILERPLATE = re.compile(
    r"^(?:pw\s*)?only\s*ias$|^daily\s+class\s+notes$|^space\s+for\s+notes$",
    flags=re.IGNORECASE,
)


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    cleaned_lines: list[str] = []
    previous_blank = False
    for raw_line in text.splitlines():
        line = re.sub(r"[\t \u00a0]+", " ", raw_line).strip()
        if _BOILERPLATE.fullmatch(line):
            continue
        if not line:
            if cleaned_lines and not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue
        cleaned_lines.append(line)
        previous_blank = False
    return "\n".join(cleaned_lines).strip()


def detect_note_language(document: NoteDocument) -> LanguageDetection:
    """Detect the dominant supported script across every extracted note page.

    The classifier is deliberately script-based: it never translates text and
    ignores all-uppercase Latin acronyms (for example, GDP and ISRO), digits,
    punctuation, and symbols.
    """
    text = document.text
    devanagari_letters = sum(
        1
        for character in text
        if "\u0900" <= character <= "\u097f"
        and unicodedata.category(character).startswith("L")
    )
    latin_letters = sum(
        len(token)
        for token in _LATIN_TOKEN.findall(text)
        if not (len(token) >= 2 and token.isupper())
    )
    total = devanagari_letters + latin_letters
    if total == 0 or devanagari_letters == latin_letters:
        return LanguageDetection(
            language=Language.UNKNOWN,
            confidence=0.0,
            devanagari_letters=devanagari_letters,
            latin_letters=latin_letters,
        )

    if devanagari_letters > latin_letters:
        language = Language.HINDI
        dominant = devanagari_letters
    else:
        language = Language.ENGLISH
        dominant = latin_letters
    return LanguageDetection(
        language=language,
        confidence=dominant / total,
        devanagari_letters=devanagari_letters,
        latin_letters=latin_letters,
    )


@lru_cache(maxsize=1)
def _ocr_engine():
    """The local OCR engine, from whichever RapidOCR package is installed.

    `rapidocr` (v2+) is tried first because it is the maintained one and the
    only one that installs on Python 3.13; `rapidocr_onnxruntime` is the older
    name and is kept so an existing environment or frozen bundle built against
    it still works.
    """
    try:
        from rapidocr import RapidOCR                      # v2, Python 3.13 OK
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR      # v1, Python <3.13
        except ImportError as exc:  # pragma: no cover - a deployment failure
            raise NoteExtractionError(
                "This PDF has no text layer and needs OCR, but no OCR engine is "
                "installed. Install it with `pip install rapidocr` — or upload a "
                "text PDF, which needs no OCR."
            ) from exc
    try:
        return RapidOCR()
    except Exception as exc:  # noqa: BLE001 — model files, onnxruntime DLLs: all "engine unusable"
        raise NoteExtractionError(
            f"The OCR engine is installed but failed to start ({type(exc).__name__}: {exc}). "
            "Text pages are still read; pages without a text layer are skipped."
        ) from exc


def _ocr_lines(result, min_score: float = 0.42) -> list[str]:
    """Recognised lines from either RapidOCR result shape.

    v1 yields an iterable of `(box, text, score)`; v2 yields an object with
    parallel `.txts` and `.scores`. Reading only one of them is how a working
    OCR install produces silently empty pages.
    """
    if result is None:
        return []
    if hasattr(result, "txts"):
        texts = result.txts
        # RapidOCR returns an output object with txts=None on blank pages.
        # It is not an iterable of legacy (box, text, score) rows.
        if texts is None:
            return []
        scores = getattr(result, "scores", None)
        if scores is None or len(scores) == 0:
            scores = [1.0] * len(texts)
        return [str(t).strip()
                for t, sc in zip(texts, scores)
                if str(t).strip() and float(sc) >= min_score]
    lines = []
    for item in result:
        if len(item) >= 3 and float(item[2]) >= min_score:
            text = str(item[1]).strip()
            if text:
                lines.append(text)
    return lines


def _crop_blank_notes_column(image):  # noqa: ANN001, ANN202
    """Remove a mostly blank right notes column without cropping normal documents.

    OpenCV is an OCR-only dependency and the frozen build does not carry it.
    Without it the page is OCR'd uncropped — a little accuracy lost on the
    notes-column layout, nothing else. It used to be imported unconditionally
    here, BEFORE the engine check, so a frozen build failed a text PDF with a
    raw ModuleNotFoundError over its cover page.
    """
    try:
        import cv2
    except ImportError:
        return image
    import numpy as np

    if image.ndim != 3 or image.shape[1] < 600:
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    width = gray.shape[1]
    left = gray[:, : int(width * 0.72)]
    right = gray[:, int(width * 0.78) :]
    left_ink = float(np.mean(left < 235))
    right_ink = float(np.mean(right < 235))
    if left_ink > 0.012 and right_ink < max(0.012, left_ink * 0.28):
        return image[:, : int(width * 0.78)]
    return image


def _ocr_page(page, engine) -> str:  # noqa: ANN001
    """OCR one page with an engine the caller has already resolved."""
    import numpy as np
    import pymupdf

    # Source notes are already page-sized scans. Native-resolution OCR keeps
    # uploads responsive while preserving heading and body text sufficiently.
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0), alpha=False)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height,
        pixmap.width,
        pixmap.n,
    )
    image = _crop_blank_notes_column(image)
    output = engine(image)
    # v1 hands back (result, elapsed); v2 hands back the result itself.
    if isinstance(output, tuple):
        output = output[0] if output else None
    return "\n".join(_ocr_lines(output))


def extract_notes(
    data: bytes,
    filename: str,
    *,
    enable_ocr: bool = True,
    max_pages: int = 200,
    progress: ProgressCallback | None = None,
) -> NoteDocument:
    if not data:
        raise NoteExtractionError("The notes file is empty.")
    suffix = Path(filename).suffix.casefold()
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise NoteExtractionError("Text notes must use UTF-8 encoding.") from exc
        cleaned = _clean_text(text)
        if not cleaned:
            raise NoteExtractionError("No readable text was found in the notes.")
        return NoteDocument(
            filename=filename,
            pages=(NotePage(page_number=1, text=cleaned, extraction_method="text"),),
        )
    if suffix != ".pdf":
        raise NoteExtractionError("Upload a PDF, TXT, or Markdown notes file.")
    if not data.startswith(b"%PDF"):
        raise NoteExtractionError("The uploaded file does not have a valid PDF signature.")

    try:
        import pymupdf

        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise NoteExtractionError(f"Unable to open PDF: {exc}") from exc
    if document.needs_pass:
        document.close()
        raise NoteExtractionError("Password-protected PDFs are not supported.")
    if document.page_count > max_pages:
        count = document.page_count
        document.close()
        raise NoteExtractionError(
            f"This PDF has {count} pages; the current safety limit is {max_pages}."
        )

    pages: list[NotePage] = []
    warnings: list[str] = []
    empty_pages: list[int] = []
    engine = None
    ocr_unavailable = ""
    total = document.page_count
    for index in range(total):
        page_number = index + 1
        if progress:
            progress(page_number, total, f"Reading page {page_number} of {total}")
        page = document[index]
        embedded = _clean_text(page.get_text("text", sort=True))
        method = "text"
        extracted = embedded
        if len(re.sub(r"\s+", "", embedded)) < 60:
            extracted = ""
            method = "empty"
            if enable_ocr and not ocr_unavailable:
                if engine is None:
                    try:
                        engine = _ocr_engine()
                    except NoteExtractionError as exc:
                        # No engine on this machine — the frozen build ships
                        # none. Keep every text page, skip the rest and say
                        # so below: failing a 40-page text PDF over its cover
                        # page is the wrong trade, and only a PDF with no
                        # text at all is refused.
                        ocr_unavailable = str(exc)
                if engine is not None:
                    if progress:
                        progress(page_number, total, f"OCR on page {page_number} of {total}")
                    extracted = _clean_text(_ocr_page(page, engine))
                    method = "ocr"
        if not extracted:
            empty_pages.append(page_number)
        pages.append(
            NotePage(
                page_number=page_number,
                text=extracted,
                extraction_method=method,
            )
        )
    document.close()

    if empty_pages:
        listed = ", ".join(map(str, empty_pages[:20])) + ("…" if len(empty_pages) > 20 else "")
        if ocr_unavailable:
            # Not the engine's own message: that one says "this PDF has no
            # text layer", which is false for a text PDF with a cover page,
            # and tells someone running the packaged app to pip install.
            warnings.append(
                f"{len(empty_pages)} page(s) had no text layer (cover, diagram or blank "
                f"pages) and were skipped because OCR is not available in this build: "
                f"{listed}. Text pages were read normally.")
        else:
            warnings.append(f"No readable text was recovered from page(s): {listed}")
    if not any(page.text for page in pages):
        raise NoteExtractionError(
            ocr_unavailable
            or "No readable text could be extracted. Try a clearer scan or enable OCR."
        )
    return NoteDocument(filename=filename, pages=tuple(pages), warnings=tuple(warnings))
