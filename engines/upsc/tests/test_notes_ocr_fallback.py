"""Uploading notes to a build that has no OCR engine.

The frozen app ships neither OpenCV nor RapidOCR — OCR is an opt-in extra for
scanned notes. The extractor used to import cv2 BEFORE it checked for an
engine, so on a frozen build any PDF with one sparse page (a cover, a
diagram, a "space for notes" page) died with a raw ModuleNotFoundError as
soon as OCR was ticked — and OCR is ticked by default. Worse, a 40-page text
PDF was refused outright over its cover page. These tests run the extractor
with those packages made un-importable, the way the EXE sees the world.
"""
from pathlib import Path
import sys

import numpy as np
import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.notes_to_dpp import notes

TEXT = ("Fundamental Rights are enshrined in Part III of the Constitution. "
        "Article 32 empowers the Supreme Court to issue writs. ") * 3


def _pdf(*pages_text: str | None) -> bytes:
    """A PDF whose pages carry the given text; None makes a blank page."""
    document = pymupdf.open()
    for text in pages_text:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=10)
    try:
        return document.tobytes()
    finally:
        document.close()


@pytest.fixture
def no_ocr_stack(monkeypatch):
    """What the frozen EXE has: no cv2, no RapidOCR under either name."""
    for name in ("cv2", "rapidocr", "rapidocr_onnxruntime"):
        monkeypatch.setitem(sys.modules, name, None)
    # Hold the real (lru_cached) function: two tests replace notes._ocr_engine
    # with a plain function, and that replacement is still in place when this
    # fixture tears down.
    real_engine = notes._ocr_engine
    real_engine.cache_clear()
    yield
    real_engine.cache_clear()


def test_a_text_pdf_with_a_sparse_page_still_extracts_and_says_what_was_skipped(no_ocr_stack):
    document = notes.extract_notes(_pdf(TEXT, None, TEXT), "notes.pdf", enable_ocr=True)

    assert [p.extraction_method for p in document.pages] == ["text", "empty", "text"]
    assert TEXT.split()[0] in document.pages[0].text
    assert document.pages[1].text == ""
    assert len(document.warnings) == 1
    warning = document.warnings[0]
    assert "1 page(s) had no text layer" in warning and "skipped" in warning
    assert warning.endswith("2. Text pages were read normally.")
    # Not the engine's own message: this PDF HAS a text layer, and nobody
    # running the packaged app can pip install anything.
    assert not warning.startswith("This PDF has no text layer")
    assert "pip install" not in warning


def test_a_fully_scanned_pdf_is_refused_with_the_engine_message_not_a_traceback(no_ocr_stack):
    with pytest.raises(notes.NoteExtractionError, match="no OCR engine is installed"):
        notes.extract_notes(_pdf(None, None), "scan.pdf", enable_ocr=True)


def test_with_ocr_off_a_sparse_page_is_simply_empty(no_ocr_stack):
    document = notes.extract_notes(_pdf(TEXT, None), "notes.pdf", enable_ocr=False)
    assert [p.extraction_method for p in document.pages] == ["text", "empty"]
    assert "No readable text was recovered from page(s): 2" in document.warnings[0]
    assert not any("OCR engine" in w for w in document.warnings)


def test_the_engine_is_asked_for_before_anything_needs_opencv(no_ocr_stack, monkeypatch):
    """The regression: cv2 was imported on the way to the engine check."""
    calls = []

    def fake_engine():
        calls.append("engine")
        raise notes.NoteExtractionError("no OCR engine is installed (fake)")

    monkeypatch.setattr(notes, "_ocr_engine", fake_engine)
    document = notes.extract_notes(_pdf(TEXT, None, None), "notes.pdf", enable_ocr=True)
    assert calls == ["engine"], "the engine is resolved once, not once per page"
    assert [p.extraction_method for p in document.pages] == ["text", "empty", "empty"]


def test_when_an_engine_exists_pages_are_ocrd_even_without_opencv(no_ocr_stack, monkeypatch):
    """OpenCV only crops a blank notes column; losing it must not lose OCR."""
    class _Result:
        txts = ("Recovered by OCR",)
        scores = (0.9,)

    monkeypatch.setattr(notes, "_ocr_engine", lambda: (lambda image: _Result()))
    document = notes.extract_notes(_pdf(TEXT, None), "notes.pdf", enable_ocr=True)
    assert document.pages[1].extraction_method == "ocr"
    assert document.pages[1].text == "Recovered by OCR"
    assert not document.warnings


def test_the_column_crop_is_skipped_when_opencv_is_missing(no_ocr_stack):
    image = np.full((900, 1200, 3), 255, dtype=np.uint8)
    assert notes._crop_blank_notes_column(image) is image


def test_blank_rapidocr_output_is_an_empty_page(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(notes, "_ocr_engine", lambda: (
        lambda image: SimpleNamespace(txts=None, scores=None)))
    document = notes.extract_notes(_pdf(TEXT, None), "notes.pdf", enable_ocr=True)
    assert [p.extraction_method for p in document.pages] == ["text", "ocr"]
    assert document.pages[1].text == ""


def test_rapidocr_numpy_scores_are_filtered_without_boolean_coercion():
    from types import SimpleNamespace

    result = SimpleNamespace(txts=("good", "uncertain"), scores=np.array([0.9, 0.1]))
    assert notes._ocr_lines(result) == ["good"]


def test_an_engine_that_installs_but_will_not_start_is_a_skip_not_a_traceback(monkeypatch):
    """rapidocr imports fine and then fails in its constructor (model files
    missing, onnxruntime DLL not loadable). That is the same situation as no
    engine, and must read as one."""
    import types

    class _BrokenEngine:
        def __init__(self):
            raise OSError("onnxruntime DLL load failed")

    monkeypatch.setitem(sys.modules, "rapidocr", types.SimpleNamespace(RapidOCR=_BrokenEngine))
    monkeypatch.setitem(sys.modules, "cv2", None)
    notes._ocr_engine.cache_clear()
    try:
        document = notes.extract_notes(_pdf(TEXT, None), "notes.pdf", enable_ocr=True)
    finally:
        notes._ocr_engine.cache_clear()
    assert [p.extraction_method for p in document.pages] == ["text", "empty"]
    assert "skipped" in document.warnings[0]
    with pytest.raises(notes.NoteExtractionError, match="failed to start"):
        notes.extract_notes(_pdf(None), "scan.pdf", enable_ocr=True)
