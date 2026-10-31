"""PDFium is not thread-safe: every call goes through one shared lock.

Notes are made for up to four page groups at once. Before the lock, two
threads inside PDFium at the same moment broke it for the rest of the process
and every later lecture failed with generate:crop_failed (PdfiumError "Failed
to load document / page") until the app was restarted.
"""
from __future__ import annotations

import threading

import pypdfium2 as pdfium
from fpdf import FPDF

from notesbot import visual_inventory
from notesbot.pdf_utils import split_pdf_chunks
from notesbot.pdfium_guard import PDFIUM_LOCK
from notesbot.visual_manifest import VisualMemoryBudget, VisualRunBudget


def _lecture_pdf(pages: int = 6) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=14)
    for number in range(1, pages + 1):
        pdf.add_page()
        pdf.cell(0, 10, f"Lecture page {number}")
        pdf.rect(20, 40, 120, 60)
    return bytes(pdf.output())


def _render_every_page(pdf_bytes: bytes) -> int:
    rendered = 0
    for chunk in split_pdf_chunks(pdf_bytes):
        memory = VisualMemoryBudget(
            max_total_bytes=256 << 20, max_source_bytes=64 << 20, max_context_bytes=64 << 20,
            max_asset_bytes=64 << 20, max_page_cache_bytes=128 << 20,
        )
        budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)
        for _number, image in visual_inventory._rendered_pages(chunk, budget, memory):
            assert image.width > 0
            rendered += 1
    return rendered


def test_every_pdfium_document_is_opened_while_holding_the_lock(monkeypatch):
    held: list[bool] = []
    real = pdfium.PdfDocument

    class Spy(real):
        def __init__(self, *args, **kwargs):
            held.append(PDFIUM_LOCK._is_owned())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pdfium, "PdfDocument", Spy)

    assert _render_every_page(_lecture_pdf()) == 6
    assert held and all(held), held


def test_page_groups_rendered_from_four_threads_all_succeed():
    data = _lecture_pdf(8)
    outcomes: list[str] = []
    guard = threading.Lock()

    def work():
        for _ in range(5):
            try:
                result = "ok" if _render_every_page(data) == 8 else "short"
            except Exception as exc:  # noqa: BLE001
                result = f"{type(exc).__name__}: {exc.__cause__ or exc}"
            with guard:
                outcomes.append(result)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == ["ok"] * 20, outcomes
