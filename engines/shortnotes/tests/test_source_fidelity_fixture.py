"""Contract checks for the deterministic source-fidelity PDF fixture."""

from __future__ import annotations

import io
import math
from contextlib import contextmanager

import numpy as np
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from notesbot import config
from notesbot.pdf_utils import split_pdf_chunks
from notesbot.snapshot import extract_source_crops
from notesbot.visual_assets import prepare_source_fallback
from notesbot.visual_manifest import (
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    VisualMemoryBudget,
    VisualRunBudget,
)

from tests import visual_fixture


EXPECTED_VISUALS = (
    (
        "sv-0001",
        1,
        (7.0, 9.0, 45.0, 39.0),
        "diagram",
        False,
        "remove_direction_arrow",
    ),
    (
        "sv-0002",
        1,
        (55.0, 9.0, 93.0, 39.0),
        "diagram",
        False,
        "add_unapproved_component",
    ),
    (
        "sv-0003",
        2,
        (16.0, 27.0, 84.0, 61.0),
        "structure",
        False,
        "change_label_case",
    ),
    (
        "sv-0004",
        3,
        (10.0, 12.0, 92.0, 86.0),
        "graph",
        False,
        "remove_curve",
    ),
    (
        "sv-0005",
        4,
        (14.0, 15.0, 86.0, 81.0),
        "diagram",
        True,
        "shift_label_anchor",
    ),
)

EXPECTED_LABELS = (
    ("I=2.50 A", (6.0, 5.0, 56.0, 20.0), (60.0, 25.0)),
    ("R₁", (38.0, 68.0, 60.0, 83.0), (50.0, 60.0)),
    ("α", (73.0, 65.0, 88.0, 81.0), (75.0, 58.0)),
    ("NH₄⁺", (4.0, 5.0, 28.0, 22.0), (37.0, 34.0)),
    ("Cl⁻", (75.0, 5.0, 91.0, 22.0), (67.0, 34.0)),
    ("pH=7.00", (37.0, 79.0, 64.0, 96.0), (50.0, 70.0)),
    ("Anode", (3.0, 45.0, 24.0, 62.0), (29.0, 54.0)),
    ("anode", (76.0, 45.0, 97.0, 62.0), (71.0, 54.0)),
    ("t (s)", (77.0, 91.0, 92.0, 98.0), (91.0, 86.0)),
    ("V (m/s)", (2.0, 3.0, 21.0, 11.0), (13.0, 16.0)),
    ("λ₀", (42.0, 5.0, 58.0, 14.0), (50.0, 25.0)),
)

EXPECTED_COMPONENTS = (
    ("sv1-path", "sv-0001", "path"),
    ("sv1-direction-arrow", "sv-0001", "direction_arrow"),
    ("sv2-lens", "sv-0002", "lens"),
    ("sv2-ray-upper", "sv-0002", "ray"),
    ("sv2-ray-lower", "sv-0002", "ray"),
    ("sv3-bonds", "sv-0003", "bonds"),
    ("sv3-atom-nodes", "sv-0003", "atom_nodes"),
    ("sv4-axes", "sv-0004", "axes"),
    ("sv4-ticks", "sv-0004", "ticks"),
    ("sv4-curve-1", "sv-0004", "curve"),
    ("sv4-curve-2", "sv-0004", "curve"),
    ("sv5-foreground-diagram", "sv-0005", "dark_foreground"),
    ("sv5-anchor", "sv-0005", "anchor"),
)


def _fidelity_memory() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=384 * 1024 * 1024,
        max_source_bytes=128 * 1024 * 1024,
        max_context_bytes=64 * 1024 * 1024,
        max_asset_bytes=64 * 1024 * 1024,
        max_page_cache_bytes=128 * 1024 * 1024,
    )


@contextmanager
def _real_fidelity_crops(fixture=None):
    fixture = fixture or visual_fixture.make_source_fidelity_pdf()
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)
    memory = _fidelity_memory()
    try:
        chunks = tuple(split_pdf_chunks(fixture.pdf_bytes, budget=budget))
        visuals = []
        for source_order, truth in enumerate(fixture.truth):
            chunk = next(
                item
                for item in chunks
                if item.first_global_page
                <= truth.local_page
                <= item.last_global_page
            )
            chunk_page = truth.local_page - chunk.first_global_page + 1
            topic_key = f"fixture-{truth.visual_id}"
            visuals.append(
                SourceVisual(
                    visual_id=truth.visual_id,
                    chunk_index=chunk.index,
                    local_page=chunk_page,
                    global_page=truth.local_page,
                    box=truth.source_box,
                    kind=truth.kind,
                    topic_anchor=SourceTopicAnchor(
                        topic_key=topic_key,
                        local_page=chunk_page,
                        box=(0.0, 0.0, 5.0, 5.0),
                    ),
                    exact_labels=tuple(
                        SourceLabel(
                            text=label.text,
                            box=label.box,
                            anchor=label.anchor,
                        )
                        for label in truth.labels
                    ),
                    source_order=source_order,
                )
            )
        crops = extract_source_crops(
            fixture.pdf_bytes,
            tuple(visuals),
            chunks,
            budget,
            memory,
            render_scale=config.VISUAL_PAGE_RENDER_SCALE,
            max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
            max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
            max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
        )
        yield fixture, tuple(crops), budget, memory
    finally:
        memory.close()


def _crop_pixels(crop) -> np.ndarray:
    with Image.open(io.BytesIO(crop.source_png)) as source:
        return np.asarray(source.convert("RGBA"), dtype=np.int16)


def _border_and_support(array: np.ndarray) -> tuple[float, tuple[int, int, int, int]]:
    border = np.concatenate(
        (
            array[0, :, :3],
            array[-1, :, :3],
            array[1:-1, 0, :3],
            array[1:-1, -1, :3],
        ),
        axis=0,
    )
    median = np.median(border, axis=0)
    border_distance = np.sqrt(np.sum((border - median) ** 2, axis=1))
    confidence = float(np.count_nonzero(border_distance <= 32.0) / len(border))
    distance = np.sqrt(np.sum((array[..., :3] - median) ** 2, axis=2))
    y, x = np.nonzero(distance > 32.0)
    assert len(x) > 0
    support = (int(x.min()), int(y.min()), int(x.max()), int(y.max()))
    return confidence, support


def _component_mismatches(fixture) -> tuple[str, ...]:
    mismatches = []
    with _real_fidelity_crops(fixture) as (_fixture, crops, _budget, _memory):
        crop_by_id = {crop.visual.visual_id: crop for crop in crops}
        for component in fixture.components:
            crop = crop_by_id[component.visual_id]
            observed = visual_fixture.inspect_source_fidelity_component(
                crop.source_png,
                crop.width_px,
                crop.height_px,
                component.box,
            )
            if (
                observed.mask_sha256 != component.mask_sha256
                or observed.foreground_pixels != component.foreground_pixels
                or observed.connected_components != component.connected_components
            ):
                mismatches.append(component.component_id)
    return tuple(mismatches)


def test_real_extracted_components_match_independent_semantic_truth() -> None:
    fixture = visual_fixture.make_source_fidelity_pdf()

    assert tuple(
        (component.component_id, component.visual_id, component.role)
        for component in fixture.components
    ) == EXPECTED_COMPONENTS
    assert _component_mismatches(fixture) == ()


def test_two_page_one_visuals_have_disjoint_validated_component_support() -> None:
    fixture = visual_fixture.make_source_fidelity_pdf()
    first, second = fixture.truth[:2]
    first_components = tuple(
        component
        for component in fixture.components
        if component.visual_id == first.visual_id
    )
    second_components = tuple(
        component
        for component in fixture.components
        if component.visual_id == second.visual_id
    )

    assert first.source_box[2] < second.source_box[0]
    assert {component.role for component in first_components} == {
        "path",
        "direction_arrow",
    }
    assert {component.role for component in second_components} == {"lens", "ray"}
    assert all(component.foreground_pixels > 0 for component in first_components)
    assert all(component.foreground_pixels > 0 for component in second_components)
    assert _component_mismatches(fixture) == ()


def test_component_truth_rejects_erased_sv1_triangle_but_retains_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = visual_fixture._draw_fidelity_page_one

    def draw_without_triangle(image, font) -> None:
        original(image, font)
        draw = ImageDraw.Draw(image)
        draw.rectangle((288, 161, 318, 195), fill=(250, 250, 247))
        draw.line((288, 178, 318, 178), fill=(25, 34, 48), width=5)

    monkeypatch.setattr(
        visual_fixture,
        "_draw_fidelity_page_one",
        draw_without_triangle,
    )
    fixture = visual_fixture.make_source_fidelity_pdf()

    mismatches = _component_mismatches(fixture)

    assert "sv1-direction-arrow" in mismatches
    assert "sv1-path" not in mismatches


def test_component_truth_rejects_stripped_sv3_chemistry_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def draw_without_chemistry(image, font) -> None:
        draw = ImageDraw.Draw(image)
        truth = visual_fixture._SOURCE_FIDELITY_TRUTH[2]
        light = (250, 248, 242)
        visual_fixture._draw_inset_panel(
            draw,
            truth,
            background=light,
            outline=(40, 48, 57),
        )
        visual_fixture._draw_exact_labels(
            draw,
            truth,
            font,
            foreground=(20, 26, 33),
            background=light,
        )

    monkeypatch.setattr(
        visual_fixture,
        "_draw_fidelity_page_two",
        draw_without_chemistry,
    )
    fixture = visual_fixture.make_source_fidelity_pdf()

    mismatches = _component_mismatches(fixture)

    assert "sv3-bonds" in mismatches
    assert "sv3-atom-nodes" in mismatches


def test_every_real_source_fidelity_crop_prepares_a_fallback() -> None:
    with _real_fidelity_crops() as (fixture, crops, budget, memory):
        assert tuple(crop.visual.visual_id for crop in crops) == tuple(
            truth.visual_id for truth in fixture.truth
        )

        prepared = tuple(
            prepare_source_fallback(crop, budget, memory) for crop in crops
        )

        assert tuple(item.visual_id for item in prepared) == tuple(
            truth.visual_id for truth in fixture.truth
        )
        assert tuple(item.background.mode for item in prepared) == (
            "light",
            "light",
            "light",
            "light",
            "dark",
        )


def test_real_crop_borders_are_uniform_and_foreground_support_is_contained() -> None:
    with _real_fidelity_crops() as (_fixture, crops, _budget, _memory):
        for crop in crops:
            pixels = _crop_pixels(crop)
            confidence, (left, top, right, bottom) = _border_and_support(pixels)

            assert confidence >= 0.95, crop.visual.visual_id
            assert left >= 4, crop.visual.visual_id
            assert top >= 4, crop.visual.visual_id
            assert right <= crop.width_px - 5, crop.visual.visual_id
            assert bottom <= crop.height_px - 5, crop.visual.visual_id


def test_source_fidelity_fixture_has_four_pages_and_five_ordered_truths() -> None:
    fixture = visual_fixture.make_source_fidelity_pdf()

    assert (fixture.width, fixture.height) == (800, 1000)
    assert tuple(
        (
            truth.visual_id,
            truth.local_page,
            truth.source_box,
            truth.kind,
            truth.is_dark,
            truth.negative_mutation,
        )
        for truth in fixture.truth
    ) == EXPECTED_VISUALS
    assert fixture.truth[0].local_page == fixture.truth[1].local_page == 1
    assert fixture.truth[0].source_box[0] < fixture.truth[1].source_box[0]

    document = pdfium.PdfDocument(fixture.pdf_bytes)
    try:
        assert len(document) == 4
        for page_index in range(4):
            page = document[page_index]
            try:
                assert page.get_width() == pytest.approx(363.635, abs=0.001)
                assert page.get_height() == pytest.approx(454.54375, abs=0.001)
            finally:
                page.close()
    finally:
        document.close()


def test_source_fidelity_truth_preserves_unicode_case_boxes_and_anchors() -> None:
    fixture = visual_fixture.make_source_fidelity_pdf()
    observed = tuple(
        (label.text, label.box, label.anchor)
        for truth in fixture.truth
        for label in truth.labels
    )

    assert observed == EXPECTED_LABELS
    assert "Anode" in tuple(item[0] for item in observed)
    assert "anode" in tuple(item[0] for item in observed)
    assert "Anode" != "anode"
    for _, box, anchor in observed:
        x0, y0, x1, y1 = box
        assert 0.0 <= x0 < x1 <= 100.0
        assert 0.0 <= y0 < y1 <= 100.0
        assert anchor is not None
        assert all(0.0 <= coordinate <= 100.0 for coordinate in anchor)


def test_source_fidelity_pdf_is_byte_deterministic_and_uses_dejavu_sans() -> None:
    first = visual_fixture.make_source_fidelity_pdf()
    second = visual_fixture.make_source_fidelity_pdf()

    assert first.pdf_bytes == second.pdf_bytes
    assert first.font_family == second.font_family == "DejaVu Sans"


def test_source_fidelity_font_fails_explicitly_for_an_unavailable_glyph() -> None:
    with pytest.raises(RuntimeError, match=r"U\+10FFFF"):
        visual_fixture._load_source_fidelity_font(30, "\U0010ffff")


def test_source_fidelity_dark_panel_is_mostly_opaque_near_black_when_rasterized() -> None:
    fixture = visual_fixture.make_source_fidelity_pdf()
    dark = next(truth for truth in fixture.truth if truth.is_dark)
    document = pdfium.PdfDocument(fixture.pdf_bytes)
    page = document[dark.local_page - 1]
    bitmap = page.render(
        scale=config.VISUAL_PAGE_RENDER_SCALE,
        rev_byteorder=True,
    )
    rendered = bitmap.to_pil().convert("RGBA")
    try:
        assert rendered.size == (fixture.width, fixture.height)
        x0, y0, x1, y1 = dark.source_box
        bounds = (
            math.floor(fixture.width * x0 / 100.0),
            math.floor(fixture.height * y0 / 100.0),
            math.ceil(fixture.width * x1 / 100.0),
            math.ceil(fixture.height * y1 / 100.0),
        )
        crop = rendered.crop(bounds)
        try:
            pixels = tuple(crop.getdata())
            near_black = sum(
                1
                for red, green, blue, alpha in pixels
                if alpha >= 250 and max(red, green, blue) <= 40
            )
            assert near_black / len(pixels) > 0.70
        finally:
            crop.close()
    finally:
        rendered.close()
        bitmap.close()
        page.close()
        document.close()
