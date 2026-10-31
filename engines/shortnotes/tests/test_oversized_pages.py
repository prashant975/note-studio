"""Decks exported with giant pages still make notes.

On 3 October 2026 a Physics lecture whose slides were 7200 x 3600 pt (100 x 50
inches) failed in the Studio with generate:crop_failed, in a second and at no
AI cost: at the fixed 2.2x render scale such a page is 15840 x 7920 px, over
the 5000 px / 25 MP page limits, and the size check rejected the whole
lecture. An oversized page is now rendered at the largest scale that fits the
limits (pdf_utils.page_render_scale); ordinary pages are unchanged.
"""
from __future__ import annotations

import math

import pytest
from fpdf import FPDF

from notesbot import config, visual_inventory
from notesbot.pdf_utils import page_render_scale, split_pdf_chunks
from notesbot.snapshot import extract_source_crops
from notesbot.visual_manifest import VisualMemoryBudget, VisualRunBudget

from tests.test_visual_assets import _visual


def _deck(width: float, height: float, pages: int = 2) -> bytes:
    pdf = FPDF(unit="pt", format=(width, height))
    pdf.set_font("Helvetica", size=height / 12)
    for number in range(1, pages + 1):
        pdf.add_page()
        pdf.cell(0, height / 6, f"Circular motion - slide {number}")
        pdf.rect(width * 0.1, height * 0.4, width * 0.5, height * 0.4)
    return bytes(pdf.output())


def _memory() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=config.MAX_VISUAL_RESIDENT_BYTES,
        max_source_bytes=64 << 20,
        max_context_bytes=64 << 20,
        max_asset_bytes=64 << 20,
        max_page_cache_bytes=config.MAX_VISUAL_PAGE_CACHE_BYTES,
    )


def _budget() -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)


def test_ordinary_pages_keep_the_configured_scale():
    for width, height in ((960.0, 540.0), (595.0, 842.0), (1920.0, 1080.0)):
        assert page_render_scale(width, height) == config.VISUAL_PAGE_RENDER_SCALE


@pytest.mark.parametrize(
    ("width", "height"),
    ((7200.0, 3600.0), (3600.0, 7200.0), (4000.0, 4000.0), (14400.0, 8100.0), (2272.73, 2272.73)),
)
def test_an_oversized_page_gets_the_largest_scale_inside_the_limits(width, height):
    scale = page_render_scale(width, height)

    assert 0.0 < scale < config.VISUAL_PAGE_RENDER_SCALE
    rendered_width, rendered_height = math.ceil(width * scale), math.ceil(height * scale)
    assert rendered_width <= config.MAX_VISUAL_PAGE_DIMENSION_PX
    assert rendered_height <= config.MAX_VISUAL_PAGE_DIMENSION_PX
    assert rendered_width * rendered_height <= config.MAX_VISUAL_PAGE_PIXELS
    assert (
        max(rendered_width, rendered_height) >= config.MAX_VISUAL_PAGE_DIMENSION_PX - 8
        or rendered_width * rendered_height >= 0.99 * config.MAX_VISUAL_PAGE_PIXELS
    )


def test_a_deck_of_7200x3600pt_slides_splits_instead_of_failing_with_crop_failed():
    chunks = split_pdf_chunks(_deck(7200, 3600, pages=3))

    assert [(chunk.first_global_page, chunk.page_count) for chunk in chunks] == [(1, 3)]
    for geometry in chunks[0].page_geometries:
        assert geometry.media_box == (0.0, 0.0, 7200.0, 3600.0)
        assert (geometry.rendered_width, geometry.rendered_height) == (5000, 2500)


def test_oversized_pages_render_at_the_size_their_geometry_records():
    source = _deck(7200, 3600, pages=1)
    chunk = split_pdf_chunks(source)[0]
    geometry = chunk.page_geometries[0]

    pages = list(visual_inventory._rendered_pages(chunk, _budget(), _memory()))
    assert [number for number, _image in pages] == [1]

    crop = extract_source_crops(
        source,
        (_visual("sv-0001", box=(5.0, 30.0, 65.0, 90.0)),),
        (chunk,),
        _budget(),
        _memory(),
        render_scale=config.VISUAL_PAGE_RENDER_SCALE,
        max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
        max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
        max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
    )[0]
    assert crop.width_px == math.ceil(geometry.rendered_width * 0.65) - math.floor(
        geometry.rendered_width * 0.05
    )
    assert crop.height_px == math.ceil(geometry.rendered_height * 0.90) - math.floor(
        geometry.rendered_height * 0.30
    )
