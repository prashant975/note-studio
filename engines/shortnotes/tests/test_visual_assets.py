from __future__ import annotations

import hashlib
import io
import math
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    FloatObject,
    NameObject,
    NumberObject,
    RectangleObject,
)

from notesbot import config
from notesbot import snapshot as snapshot_module
from notesbot.errors import VisualFidelityError
from notesbot.pdf_utils import PdfChunk, split_pdf_chunks
from notesbot.snapshot import extract_source_crops
from notesbot.visual_manifest import (
    SourceContext,
    SourceCrop,
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    VisualMemoryBudget,
    VisualRunBudget,
)
from notesbot.visual_assets import (
    NOTES_INK_RGB,
    composite_source_labels,
    prepare_source_fallback,
    validate_composited_labels,
)


def _pdf(*, pages: int = 1, width: float = 400, height: float = 400) -> bytes:
    writer = PdfWriter()
    for page_index in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        # Distinct, saturated source pixels expose whitening, inversion, and page swaps.
        stream = DecodedStreamObject()
        stream.set_data(
            (
                "q\n"
                f"{0.1 + page_index * 0.2:.2f} 0.20 0.80 rg\n"
                f"0 0 {width / 2:g} {height:g} re f\n"
                "0.90 0.10 0.20 rg\n"
                f"{width / 2:g} 0 {width / 2:g} {height:g} re f\n"
                "Q\n"
            ).encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _transformed_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=420, height=300)
    page.cropbox = RectangleObject((35, 25, 385, 275))
    page[NameObject("/Rotate")] = NumberObject(90)
    page[NameObject("/UserUnit")] = FloatObject(1.75)
    stream = DecodedStreamObject()
    # Asymmetric marks make accidental unrotated or MediaBox-based crops visible.
    stream.set_data(
        b"q\n"
        b"0.05 0.75 0.15 rg 45 35 80 55 re f\n"
        b"0.85 0.10 0.65 rg 245 155 125 105 re f\n"
        b"0.10 0.25 0.95 rg 135 80 65 150 re f\n"
        b"Q\n"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _single_page_pdf(pdf_bytes: bytes, page_index: int) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _budget(*, clock=None) -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=clock or (lambda: 1.0))


def _memory(
    *,
    total: int = 384 * 1024 * 1024,
    source: int = 128 * 1024 * 1024,
    context: int = 64 * 1024 * 1024,
    pages: int = 128 * 1024 * 1024,
) -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=total,
        max_source_bytes=source,
        max_context_bytes=context,
        max_asset_bytes=64 * 1024 * 1024,
        max_page_cache_bytes=pages,
    )


def _context(
    *,
    page: int = 1,
    box: tuple[float, float, float, float] = (0.0, 0.0, 30.0, 30.0),
    role: str = "explanation",
    text: str = "source context",
) -> SourceContext:
    return SourceContext(
        topic_key="topic",
        local_page=page,
        box=box,
        role=role,
        exact_text=text,
    )


def _visual(
    visual_id: str,
    *,
    chunk_index: int = 0,
    local_page: int = 1,
    global_page: int = 1,
    box: tuple[float, float, float, float] = (10.0, 10.0, 90.0, 90.0),
    contexts: tuple[SourceContext, ...] = (),
) -> SourceVisual:
    return SourceVisual(
        visual_id=visual_id,
        chunk_index=chunk_index,
        local_page=local_page,
        global_page=global_page,
        box=box,
        kind="diagram",
        topic_anchor=SourceTopicAnchor(
            topic_key="topic",
            local_page=local_page,
            box=(0.0, 0.0, 40.0, 8.0),
            exact_heading="Exact heading",
        ),
        exact_labels=(
            SourceLabel(text="Na+ / Cl-", box=(2.0, 3.0, 20.0, 12.0)),
        ),
        context_regions=contexts,
        section_hint="section",
        source_order=int(visual_id[-4:]),
    )


def _extract(
    pdf_bytes: bytes,
    visuals: tuple[SourceVisual, ...],
    chunks: tuple[PdfChunk, ...] | None = None,
    *,
    memory: VisualMemoryBudget | None = None,
    budget: VisualRunBudget | None = None,
    max_page_dimension_px: int = 5_000,
    max_crop_pixels: int = 8_000_000,
    max_png_bytes: int = 10 * 1024 * 1024,
):
    actual_chunks = tuple(split_pdf_chunks(pdf_bytes)) if chunks is None else chunks
    return extract_source_crops(
        pdf_bytes,
        visuals,
        actual_chunks,
        budget or _budget(),
        memory or _memory(),
        render_scale=config.VISUAL_PAGE_RENDER_SCALE,
        max_page_dimension_px=max_page_dimension_px,
        max_crop_pixels=max_crop_pixels,
        max_png_bytes=max_png_bytes,
    )


def _decoded(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def _memory_state(memory: VisualMemoryBudget) -> tuple[int, int, int, int, int]:
    return (
        memory.source_bytes,
        memory.context_bytes,
        memory.asset_bytes,
        memory.page_cache_bytes,
        memory.resident_bytes(),
    )


class _TrackedImage:
    def __init__(
        self,
        size: tuple[int, int],
        *,
        fail_load: bool = False,
        fail_close: bool = False,
    ) -> None:
        self._image = Image.new("RGB", size, (31, 63, 127))
        self.fail_load = fail_load
        self.fail_close = fail_close
        self.closed = False
        self.close_calls = 0

    @property
    def size(self):
        return self._image.size

    @property
    def mode(self):
        return self._image.mode

    @property
    def width(self):
        return self._image.width

    @property
    def height(self):
        return self._image.height

    def getbands(self):
        return self._image.getbands()

    def load(self):
        if self.fail_load:
            raise RuntimeError("injected decode failure")
        return self._image.load()

    def crop(self, bounds):
        return self._image.crop(bounds)

    def close(self):
        self.close_calls += 1
        self.closed = True
        self._image.close()
        if self.fail_close:
            raise RuntimeError("injected image close failure")


class _TrackedBitmap:
    def __init__(
        self,
        image: _TrackedImage | None,
        *,
        fail_to_pil: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.image = image
        self.fail_to_pil = fail_to_pil
        self.fail_close = fail_close
        self.close_calls = 0

    def to_pil(self):
        if self.fail_to_pil:
            raise RuntimeError("injected to_pil failure")
        return self.image

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("injected bitmap close failure")


def _spy_buffer_ownership(monkeypatch, memory: VisualMemoryBudget):
    real_reserve = memory.reserve_buffer
    real_release = memory.release_buffer
    reserved = []
    released = []

    def recording_reserve(kind, data):
        reservation = real_reserve(kind, data)
        if reservation is not None:
            reserved.append(reservation)
        return reservation

    def recording_release(reservation):
        released.append(reservation)
        return real_release(reservation)

    monkeypatch.setattr(memory, "reserve_buffer", recording_reserve)
    monkeypatch.setattr(memory, "release_buffer", recording_release)
    return reserved, released


def test_extract_source_crops_returns_one_crop_per_manifest_id_in_order() -> None:
    pdf = _pdf(pages=2)
    visuals = (
        _visual("sv-0002", local_page=2, global_page=2),
        _visual("sv-0001"),
    )

    crops = _extract(pdf, visuals)

    assert [crop.visual.visual_id for crop in crops] == ["sv-0002", "sv-0001"]
    assert len(crops) == len(visuals)


def test_crop_uses_translated_global_page_and_exact_percentage_box() -> None:
    pdf = _pdf(pages=3, width=401, height=301)
    chunk = tuple(split_pdf_chunks(pdf))[0]
    visual = _visual(
        "sv-0001",
        local_page=2,
        global_page=chunk.first_global_page + 1,
        box=(10.1, 20.2, 70.3, 80.4),
    )
    geometry = chunk.page_geometries[1]
    expected = (
        math.floor(geometry.rendered_width * 10.1 / 100.0),
        math.floor(geometry.rendered_height * 20.2 / 100.0),
        math.ceil(geometry.rendered_width * 70.3 / 100.0),
        math.ceil(geometry.rendered_height * 80.4 / 100.0),
    )

    crop = _extract(pdf, (visual,), (chunk,))[0]

    assert (crop.width_px, crop.height_px) == (
        expected[2] - expected[0],
        expected[3] - expected[1],
    )
    # The second page has a different left-half colour, proving global translation.
    assert _decoded(crop.source_png).getpixel((0, 0))[0] > 50


def test_crop_revalidates_rotated_cropped_original_and_chunk_coordinate_frames() -> None:
    pdf = _transformed_pdf()
    chunk = tuple(split_pdf_chunks(pdf))[0]
    geometry = chunk.page_geometries[0]
    assert geometry.rotation == 90
    assert geometry.crop_box == (35.0, 25.0, 385.0, 275.0)
    assert geometry.user_unit == 1.75

    visual = _visual("sv-0001", box=(11.3, 17.1, 78.7, 83.2))
    crop = _extract(pdf, (visual,), (chunk,))[0]

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    page = document[0]
    bitmap = page.render(
        scale=config.VISUAL_PAGE_RENDER_SCALE,
        rev_byteorder=True,
    )
    rendered = bitmap.to_pil()
    expected_bounds = (
        math.floor(rendered.width * 11.3 / 100.0),
        math.floor(rendered.height * 17.1 / 100.0),
        math.ceil(rendered.width * 78.7 / 100.0),
        math.ceil(rendered.height * 83.2 / 100.0),
    )
    expected = rendered.crop(expected_bounds)
    actual = _decoded(crop.source_png)
    try:
        assert (crop.width_px, crop.height_px) == expected.size
        assert ImageChops.difference(actual, expected).getbbox() is None
    finally:
        actual.close()
        expected.close()
        rendered.close()
        bitmap.close()
        page.close()
        document.close()

    mismatches = (
        replace(geometry, rotation=0),
        replace(geometry, crop_box=(36.0, 25.0, 385.0, 275.0)),
        replace(geometry, media_box=(0.0, 0.0, 419.0, 300.0)),
        replace(geometry, user_unit=1.5),
        replace(geometry, rendered_width=geometry.rendered_width + 1),
        replace(geometry, rendered_height=geometry.rendered_height + 1),
    )

    for bad_geometry in mismatches:
        bad_chunk = replace(chunk, page_geometries=(bad_geometry,))
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (_visual("sv-0001"),), (bad_chunk,))
        assert exc.value.code == "crop_failed"


def test_context_crops_use_only_same_or_adjacent_source_pages_and_are_not_render_assets() -> None:
    pdf = _pdf(pages=3)
    contexts = (
        _context(page=1, role="heading", text="same"),
        _context(page=3, role="legend", text="adjacent"),
    )
    visual = _visual(
        "sv-0001", local_page=2, global_page=2, contexts=contexts
    )

    crop = _extract(pdf, (visual,))[0]

    assert tuple(item.context for item in crop.context_crops) == contexts
    assert all(not hasattr(item, "visual_id") for item in crop.context_crops)
    assert crop.source_png not in tuple(item.source_png for item in crop.context_crops)


def test_source_crop_rejects_reordered_or_swapped_same_size_context_records(
    monkeypatch,
) -> None:
    pdf = _pdf(pages=2)
    contexts = (
        _context(page=1, role="heading", text="first"),
        _context(page=1, role="legend", text="second"),
    )
    visual = _visual("sv-0001", contexts=contexts)
    swap_reached = False

    def swapped_source_crop(*, visual, source_png, width_px, height_px, context_crops):
        nonlocal swap_reached
        swap_reached = True
        assert len(context_crops) == 2
        assert context_crops[0].source_png == context_crops[1].source_png
        assert context_crops[0].width_px == context_crops[1].width_px
        assert context_crops[0].height_px == context_crops[1].height_px
        return snapshot_module.SourceCrop(
            visual=visual,
            source_png=source_png,
            width_px=width_px,
            height_px=height_px,
            context_crops=tuple(reversed(context_crops)),
        )

    monkeypatch.setattr(
        snapshot_module,
        "_construct_source_crop",
        swapped_source_crop,
        raising=False,
    )

    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (visual,))
    assert exc.value.code == "crop_failed"
    assert swap_reached


def test_context_crop_rejects_adjacent_global_page_outside_originating_chunk() -> None:
    pdf = _pdf(pages=2)
    original = tuple(split_pdf_chunks(pdf))[0]
    first_chunk = PdfChunk(
        index=0,
        pdf_bytes=_single_page_pdf(pdf, 0),
        first_global_page=1,
        page_count=1,
        page_geometries=(original.page_geometries[0],),
    )
    second_chunk = PdfChunk(
        index=1,
        pdf_bytes=_single_page_pdf(pdf, 1),
        first_global_page=2,
        page_count=1,
        page_geometries=(original.page_geometries[1],),
    )
    visual = _visual("sv-0001", contexts=(_context(page=2),))
    # Control proves the supplied two-chunk partition is complete and valid.
    control = _visual(
        "sv-0002",
        chunk_index=1,
        local_page=1,
        global_page=2,
    )
    assert _extract(pdf, (control,), (first_chunk, second_chunk))[0].visual == control

    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (visual,), (first_chunk, second_chunk))

    assert exc.value.code == "crop_failed"


@pytest.mark.parametrize("limit_kind", ("area", "pixels", "png"))
def test_context_rejects_over_area_pixel_or_png_budget(monkeypatch, limit_kind: str) -> None:
    pdf = _pdf()
    box = (0.0, 0.0, 80.0, 80.0) if limit_kind == "area" else (0.0, 0.0, 40.0, 40.0)
    if limit_kind == "pixels":
        monkeypatch.setattr(config, "MAX_CONTEXT_PIXELS", 10)
    if limit_kind == "png":
        monkeypatch.setattr(config, "MAX_CONTEXT_PNG_BYTES", 10)
    visual = _visual("sv-0001", contexts=(_context(box=box),))
    memory = _memory()

    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (visual,), memory=memory)

    assert exc.value.code == "crop_failed"
    assert _memory_state(memory) == (0, 0, 0, 0, 0)


def test_two_context_payloads_obey_total_byte_budget(monkeypatch) -> None:
    pdf = _pdf(pages=2)
    contexts = (_context(page=1), _context(page=2, role="legend"))
    visual = _visual("sv-0001", contexts=contexts)
    first = _extract(pdf, (visual,))[0]
    total = sum(len(item.source_png) for item in first.context_crops)
    monkeypatch.setattr(config, "MAX_CONTEXT_TOTAL_BYTES", total - 1)

    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (visual,))

    assert exc.value.code == "crop_failed"


def test_oversized_optional_context_text_is_omitted_not_truncated_or_rendered() -> None:
    pdf = _pdf()
    canonical_omission = _context(text="")
    crop = _extract(pdf, (_visual("sv-0001", contexts=(canonical_omission,)),))[0]
    assert crop.context_crops[0].context.exact_text == ""

    oversized = _context(text="x" * (config.MAX_CONTEXT_TEXT_CHARS + 1))
    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (_visual("sv-0001", contexts=(oversized,)),))
    assert exc.value.code == "crop_failed"


def test_crop_aggregate_source_context_and_resident_memory_caps_fail_before_retention() -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    visual = _visual("sv-0001", contexts=(_context(),))
    baseline = _extract(pdf, (visual,), chunks)[0]
    source_size = len(baseline.source_png)
    context_size = len(baseline.context_crops[0].source_png)
    geometry = chunks[0].page_geometries[0]
    page_bytes = geometry.rendered_width * geometry.rendered_height * 3

    for memory in (
        _memory(source=source_size - 1),
        _memory(context=context_size - 1),
    ):
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (visual,), chunks, memory=memory)
        assert exc.value.code == "memory_limit_exceeded"
        assert memory.source_bytes == 0
        assert memory.context_bytes == 0
        assert memory.page_cache_bytes == 0

    # The page and source both fit; only the later distinct context crosses total.
    context_aggregate = _memory(
        total=page_bytes + source_size + context_size - 1,
        pages=page_bytes,
    )
    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (visual,), chunks, memory=context_aggregate)
    assert exc.value.code == "memory_limit_exceeded"
    assert _memory_state(context_aggregate) == (0, 0, 0, 0, 0)

    # The page and first source fit; only the second distinct source crosses total.
    source_aggregate = _memory(
        total=page_bytes + source_size * 2 - 1,
        pages=page_bytes,
    )
    with pytest.raises(VisualFidelityError) as exc:
        _extract(
            pdf,
            (_visual("sv-0001"), _visual("sv-0002")),
            chunks,
            memory=source_aggregate,
        )
    assert exc.value.code == "memory_limit_exceeded"
    assert _memory_state(source_aggregate) == (0, 0, 0, 0, 0)


def test_decoded_page_cache_uses_bounded_lru_eviction_and_releases_reservations(monkeypatch) -> None:
    pdf = _pdf(pages=2)
    import pypdfium2

    render_calls: list[bool] = []
    original_render = pypdfium2.PdfPage.render

    def recording_render(self, *args, **kwargs):
        render_calls.append(True)
        return original_render(self, *args, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", recording_render)
    chunks = tuple(split_pdf_chunks(pdf))
    page_bytes = chunks[0].page_geometries[0].rendered_width * chunks[0].page_geometries[0].rendered_height * 3
    memory = _memory(total=page_bytes + 20_000_000, pages=page_bytes)
    visuals = (
        _visual("sv-0001", local_page=1, global_page=1),
        _visual("sv-0002", local_page=2, global_page=2),
        _visual("sv-0003", local_page=1, global_page=1),
    )

    _extract(pdf, visuals, chunks, memory=memory)

    assert len(render_calls) == 3
    assert memory.page_cache_bytes == 0


@pytest.mark.parametrize("failure_stage", ("render", "to_pil", "load", "deadline"))
def test_partial_page_decode_failures_close_every_acquired_resource(
    monkeypatch,
    failure_stage: str,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    geometry = chunks[0].page_geometries[0]
    image = _TrackedImage(
        (geometry.rendered_width, geometry.rendered_height),
        fail_load=failure_stage == "load",
    )
    bitmap = _TrackedBitmap(image, fail_to_pil=failure_stage == "to_pil")
    expired = False
    import pypdfium2

    original_page_close = pypdfium2.PdfPage.close
    page_close_calls = 0

    def recording_page_close(self):
        nonlocal page_close_calls
        page_close_calls += 1
        return original_page_close(self)

    def injected_render(self, *args, **kwargs):
        nonlocal expired
        if failure_stage == "render":
            raise RuntimeError("injected render failure")
        if failure_stage == "deadline":
            expired = True
        return bitmap

    monkeypatch.setattr(pypdfium2.PdfPage, "close", recording_page_close)
    monkeypatch.setattr(pypdfium2.PdfPage, "render", injected_render)
    clock = lambda: 101.0 if expired else 1.0
    memory = _memory()

    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, (_visual("sv-0001"),), chunks, memory=memory, budget=_budget(clock=clock))

    assert exc.value.code == (
        "deadline_exceeded" if failure_stage == "deadline" else "crop_failed"
    )
    assert page_close_calls >= 2  # preflight handle and render handle
    if failure_stage != "render":
        assert bitmap.close_calls == 1
    if failure_stage in ("load", "deadline"):
        assert image.close_calls == 1
    else:
        assert image.close_calls == 0
    assert _memory_state(memory) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("cleanup_stage", ("image", "bitmap", "document"))
def test_final_cleanup_failure_rolls_back_all_new_buffer_and_page_ownership(
    monkeypatch,
    cleanup_stage: str,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    geometry = chunks[0].page_geometries[0]
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-owned-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    import pypdfium2

    image = _TrackedImage(
        (geometry.rendered_width, geometry.rendered_height),
        fail_close=cleanup_stage == "image",
    )
    bitmap = _TrackedBitmap(image, fail_close=cleanup_stage == "bitmap")
    monkeypatch.setattr(
        pypdfium2.PdfPage,
        "render",
        lambda self, *args, **kwargs: bitmap,
    )
    original_document_close = pypdfium2.PdfDocument.close
    document_close_calls = 0

    def recording_document_close(self):
        nonlocal document_close_calls
        document_close_calls += 1
        original_document_close(self)
        if cleanup_stage == "document":
            raise RuntimeError("injected document close failure")

    monkeypatch.setattr(pypdfium2.PdfDocument, "close", recording_document_close)
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)

    visual = _visual("sv-0001", contexts=(_context(),))
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (visual,), chunks, memory=memory)
        assert exc.value.code == "crop_failed"
        assert _memory_state(memory) == before
        assert image.close_calls == 1
        assert bitmap.close_calls == 1
        assert document_close_calls == 1
        assert [item.kind for item in reserved] == ["source", "context"]
        assert released == list(reversed(reserved))
    finally:
        # Pre-call ownership is intentionally outside resolver rollback.
        memory.release_buffer(preexisting)


@pytest.mark.parametrize(
    "deadline_phase",
    ("reader_parse", "pdfium_parse", "render", "crop", "encode"),
)
def test_deadline_is_checked_after_each_parse_render_crop_and_encode_boundary(
    monkeypatch,
    deadline_phase: str,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    expired = False
    boundary_reached = False

    def arm_deadline() -> None:
        nonlocal expired, boundary_reached
        boundary_reached = True
        expired = True

    if deadline_phase == "reader_parse":
        original_reader = snapshot_module.PdfReader

        def expiring_reader(*args, **kwargs):
            reader = original_reader(*args, **kwargs)
            arm_deadline()
            return reader

        monkeypatch.setattr(snapshot_module, "PdfReader", expiring_reader)
    elif deadline_phase == "pdfium_parse":
        import pypdfium2

        original_document = pypdfium2.PdfDocument

        def expiring_document(*args, **kwargs):
            document = original_document(*args, **kwargs)
            arm_deadline()
            return document

        monkeypatch.setattr(pypdfium2, "PdfDocument", expiring_document)
    elif deadline_phase == "render":
        import pypdfium2

        original_render = pypdfium2.PdfPage.render

        def expiring_render(self, *args, **kwargs):
            bitmap = original_render(self, *args, **kwargs)
            arm_deadline()
            return bitmap

        monkeypatch.setattr(pypdfium2.PdfPage, "render", expiring_render)
    elif deadline_phase == "crop":
        original_crop = Image.Image.crop

        def expiring_crop(self, *args, **kwargs):
            crop = original_crop(self, *args, **kwargs)
            arm_deadline()
            return crop

        monkeypatch.setattr(Image.Image, "crop", expiring_crop)
    else:
        original_save = Image.Image.save

        def expiring_save(self, *args, **kwargs):
            result = original_save(self, *args, **kwargs)
            arm_deadline()
            return result

        monkeypatch.setattr(Image.Image, "save", expiring_save)

    memory = _memory()
    clock = lambda: 100.0 if expired else 1.0
    with pytest.raises(VisualFidelityError) as exc:
        _extract(
            pdf,
            (_visual("sv-0001"),),
            chunks,
            memory=memory,
            budget=_budget(clock=clock),
        )

    assert boundary_reached
    assert exc.value.code == "deadline_exceeded"
    assert _memory_state(memory) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("expire_on_check", "expected_operations"),
    (
        (7, (0, 0, 0)),
        (8, (1, 0, 0)),
        (9, (1, 0, 0)),
        (10, (1, 1, 0)),
        (11, (1, 1, 0)),
        (12, (1, 1, 1)),
        (13, (1, 1, 1)),
        (14, (1, 2, 1)),
        (15, (1, 2, 1)),
        (16, (1, 2, 2)),
    ),
)
def test_deadline_before_and_after_source_and_context_boundaries_is_exact(
    monkeypatch,
    expire_on_check: int,
    expected_operations: tuple[int, int, int],
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-deadline-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)
    import pypdfium2

    operation_counts = {"render": 0, "crop": 0, "save": 0}
    original_render = pypdfium2.PdfPage.render
    original_crop = Image.Image.crop
    original_save = Image.Image.save

    def recording_render(self, *args, **kwargs):
        operation_counts["render"] += 1
        return original_render(self, *args, **kwargs)

    def recording_crop(self, *args, **kwargs):
        operation_counts["crop"] += 1
        return original_crop(self, *args, **kwargs)

    def recording_save(self, *args, **kwargs):
        operation_counts["save"] += 1
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", recording_render)
    monkeypatch.setattr(Image.Image, "crop", recording_crop)
    monkeypatch.setattr(Image.Image, "save", recording_save)

    class ExpireOnCheck:
        def __init__(self, threshold: int) -> None:
            self.threshold = threshold
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 100.0 if self.calls >= self.threshold else 1.0

    clock = ExpireOnCheck(expire_on_check)
    budget = _budget()
    budget.clock = clock
    visual = _visual("sv-0001", contexts=(_context(),))
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (visual,), chunks, memory=memory, budget=budget)
        assert exc.value.code == "deadline_exceeded"
        assert tuple(operation_counts.values()) == expected_operations
        expected_kinds = ["source"] if expire_on_check >= 13 else []
        assert [item.kind for item in reserved] == expected_kinds
        assert released == list(reversed(reserved))
        assert _memory_state(memory) == before
    finally:
        memory.release_buffer(preexisting)


@pytest.mark.parametrize("encode_stage", ("source", "context"))
def test_injected_source_or_context_encode_failure_rolls_back_to_precall_state(
    monkeypatch,
    encode_stage: str,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-encode-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)
    original_encode = snapshot_module._encode_png
    calls = 0
    fail_call = 1 if encode_stage == "source" else 2

    def injected_encode(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise RuntimeError(f"injected {encode_stage} encode failure")
        return original_encode(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_encode_png", injected_encode)
    visual = _visual("sv-0001", contexts=(_context(),))
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (visual,), chunks, memory=memory)
        assert exc.value.code == "crop_failed"
        assert calls == fail_call
        assert _memory_state(memory) == before
        expected_kinds = [] if encode_stage == "source" else ["source"]
        assert [item.kind for item in reserved] == expected_kinds
        assert released == list(reversed(reserved))
    finally:
        memory.release_buffer(preexisting)


def test_multi_visual_late_encode_failure_rolls_back_all_prior_crops(
    monkeypatch,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-late-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)
    original_encode = snapshot_module._encode_png
    calls = 0

    def fail_second_visual_source(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # first source, first context, then second source
            raise RuntimeError("injected late source encode failure")
        return original_encode(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_encode_png", fail_second_visual_source)
    visuals = (
        _visual("sv-0001", contexts=(_context(),)),
        _visual("sv-0002"),
    )
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, visuals, chunks, memory=memory)
        assert exc.value.code == "crop_failed"
        assert calls == 3
        assert _memory_state(memory) == before
        assert [item.kind for item in reserved] == ["source", "context"]
        assert released == list(reversed(reserved))
    finally:
        memory.release_buffer(preexisting)


@pytest.mark.parametrize("save_stage", ("source", "context"))
def test_pillow_source_or_context_png_encoder_failure_rolls_back_owned_buffers(
    monkeypatch,
    save_stage: str,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-pillow-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)
    original_save = Image.Image.save
    calls = 0
    fail_call = 1 if save_stage == "source" else 2

    def raising_save(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise OSError(f"injected Pillow {save_stage} failure")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", raising_save)
    visual = _visual("sv-0001", contexts=(_context(),))
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, (visual,), chunks, memory=memory)
        assert exc.value.code == "crop_failed"
        assert calls == fail_call
        assert _memory_state(memory) == before
        expected_kinds = [] if save_stage == "source" else ["source"]
        assert [item.kind for item in reserved] == expected_kinds
        assert released == list(reversed(reserved))
    finally:
        memory.release_buffer(preexisting)


def test_rollback_continues_after_one_release_hook_raises_post_release(
    monkeypatch,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    memory = _memory()
    preexisting = memory.reserve_buffer("asset", b"preexisting-release-asset")
    assert preexisting is not None
    before = _memory_state(memory)
    real_reserve = memory.reserve_buffer
    real_release = memory.release_buffer
    reserved = []
    release_attempts = []

    def recording_reserve(kind, data):
        reservation = real_reserve(kind, data)
        if reservation is not None:
            reserved.append(reservation)
        return reservation

    def release_then_raise_once(reservation):
        release_attempts.append(reservation)
        real_release(reservation)
        if len(release_attempts) == 1:
            raise RuntimeError("injected post-release hook failure")

    monkeypatch.setattr(memory, "reserve_buffer", recording_reserve)
    monkeypatch.setattr(memory, "release_buffer", release_then_raise_once)
    original_encode = snapshot_module._encode_png
    calls = 0

    def fail_late(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected late failure")
        return original_encode(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_encode_png", fail_late)
    visuals = (
        _visual("sv-0001", contexts=(_context(),)),
        _visual("sv-0002"),
    )
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, visuals, chunks, memory=memory)
        assert exc.value.code == "crop_failed"
        assert [item.kind for item in reserved] == ["source", "context"]
        assert release_attempts == list(reversed(reserved))
        assert _memory_state(memory) == before
    finally:
        real_release(preexisting)


def test_source_png_byte_cap_fails_closed_and_releases_page_state() -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    visual = _visual("sv-0001")
    baseline = _extract(pdf, (visual,), chunks)[0]
    memory = _memory()

    with pytest.raises(VisualFidelityError) as exc:
        _extract(
            pdf,
            (visual,),
            chunks,
            memory=memory,
            max_png_bytes=len(baseline.source_png) - 1,
        )

    assert exc.value.code == "crop_failed"
    assert _memory_state(memory) == (0, 0, 0, 0, 0)


def test_crop_keeps_source_pixels_and_label_regions_unchanged() -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    visual = _visual("sv-0001", box=(0.0, 0.0, 50.0, 100.0))
    crop = _extract(pdf, (visual,), chunks)[0]

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    page = document[0]
    rendered = page.render(scale=config.VISUAL_PAGE_RENDER_SCALE).to_pil()
    expected = rendered.crop((0, 0, math.ceil(rendered.width / 2), rendered.height))
    actual = _decoded(crop.source_png)
    try:
        assert ImageChops.difference(actual, expected).getbbox() is None
        assert crop.visual.exact_labels == visual.exact_labels
        # No-Git checkpoint: immutable original crop hash retained in test output.
        print("original-crop-sha256", hashlib.sha256(crop.source_png).hexdigest())
    finally:
        expected.close()
        rendered.close()
        page.close()
        document.close()


def test_invalid_page_box_tiny_crop_decode_page_dimension_or_pixel_limit_fails_whole_job() -> None:
    pdf = _pdf()
    chunk = tuple(split_pdf_chunks(pdf))[0]
    malformed = replace(
        chunk,
        page_geometries=(
            replace(chunk.page_geometries[0], crop_box=(0.0, 0.0, 0.0, 400.0)),
        ),
    )
    cases = (
        ((malformed,), _visual("sv-0001"), 5_000, 8_000_000),
        ((chunk,), _visual("sv-0001", box=(0.0, 0.0, 1.0, 1.0)), 5_000, 8_000_000),
        ((chunk,), _visual("sv-0001"), 100, 8_000_000),
        ((chunk,), _visual("sv-0001"), 5_000, 100),
    )

    for chunks, visual, dimension_limit, pixel_limit in cases:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(
                pdf,
                (visual,),
                chunks,
                max_page_dimension_px=dimension_limit,
                max_crop_pixels=pixel_limit,
            )
        assert exc.value.code == "crop_failed"


def test_crop_exact_minimum_pixel_boundaries_pass_and_one_pixel_below_fails_before_raster(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_WIDTH_PX", 96)
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_HEIGHT_PX", 64)
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_PIXELS", 8_192)
    pdf = _pdf(width=100, height=100)
    chunk = tuple(split_pdf_chunks(pdf))[0]
    width = chunk.page_geometries[0].rendered_width
    height = chunk.page_geometries[0].rendered_height
    import pypdfium2

    renders = 0
    original_render = pypdfium2.PdfPage.render

    def recording_render(self, *args, **kwargs):
        nonlocal renders
        renders += 1
        return original_render(self, *args, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", recording_render)
    cases = (
        (96, 100, True),
        (95, 100, False),
        (140, 64, True),
        (140, 63, False),
        (128, 64, True),  # exactly 8,192 pixels
    )
    for index, (target_width, target_height, passes) in enumerate(cases, 1):
        visual = _visual(
            f"sv-{index:04d}",
            box=(
                0.0,
                0.0,
                target_width * 100.0 / width,
                target_height * 100.0 / height,
            ),
        )
        before_renders = renders
        if passes:
            crop = _extract(pdf, (visual,), (chunk,))[0]
            assert (crop.width_px, crop.height_px) == (
                target_width,
                target_height,
            )
            assert renders == before_renders + 1
        else:
            with pytest.raises(VisualFidelityError) as exc:
                _extract(pdf, (visual,), (chunk,))
            assert exc.value.code == "crop_failed"
            assert renders == before_renders


def test_crop_exact_8192_and_8191_pixel_boundaries_are_isolated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_WIDTH_PX", 1)
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_HEIGHT_PX", 1)
    monkeypatch.setattr(config, "MIN_VISUAL_CROP_PIXELS", 8_192)
    monkeypatch.setattr(config, "MAX_VISUAL_PAGE_DIMENSION_PX", 9_000)
    pdf = _pdf(
        width=(8_192 - 0.25) / config.VISUAL_PAGE_RENDER_SCALE,
        height=0.25 / config.VISUAL_PAGE_RENDER_SCALE,
    )
    chunk = tuple(split_pdf_chunks(pdf))[0]
    assert (
        chunk.page_geometries[0].rendered_width,
        chunk.page_geometries[0].rendered_height,
    ) == (8_192, 1)

    passing = _visual("sv-0001", box=(0.0, 0.0, 100.0, 100.0))
    crop = _extract(
        pdf,
        (passing,),
        (chunk,),
        max_page_dimension_px=9_000,
    )[0]
    assert crop.width_px * crop.height_px == 8_192

    import pypdfium2

    original_render = pypdfium2.PdfPage.render
    renders = 0

    def recording_render(self, *args, **kwargs):
        nonlocal renders
        renders += 1
        return original_render(self, *args, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", recording_render)
    failing = _visual(
        "sv-0002",
        box=(0.0, 0.0, 8_191 * 100.0 / 8_192, 100.0),
    )
    with pytest.raises(VisualFidelityError) as exc:
        _extract(
            pdf,
            (failing,),
            (chunk,),
            max_page_dimension_px=9_000,
        )
    assert exc.value.code == "crop_failed"
    assert renders == 0


def test_retained_source_buffers_have_unique_ownership_and_alias_failure_rolls_back(
    monkeypatch,
) -> None:
    pdf = _pdf()
    chunks = tuple(split_pdf_chunks(pdf))
    visuals = (_visual("sv-0001"), _visual("sv-0002"))
    memory = _memory()
    reserved, released = _spy_buffer_ownership(monkeypatch, memory)
    crops = _extract(pdf, visuals, chunks, memory=memory)
    try:
        assert crops[0].source_png == crops[1].source_png
        assert crops[0].source_png is not crops[1].source_png
        assert [item.kind for item in reserved] == ["source", "source"]
        assert released == []
        assert {id(item.data) for item in reserved} == {
            id(crop.source_png) for crop in crops
        }
        assert memory.source_bytes == sum(len(crop.source_png) for crop in crops)
    finally:
        for reservation in reversed(reserved):
            memory.release_buffer(reservation)
    assert _memory_state(memory) == (0, 0, 0, 0, 0)

    shared_png = crops[0].source_png
    alias_memory = _memory()
    preexisting = alias_memory.reserve_buffer("asset", b"preexisting-alias-asset")
    assert preexisting is not None
    before = _memory_state(alias_memory)
    monkeypatch.setattr(
        snapshot_module,
        "_encode_png",
        lambda *args, **kwargs: shared_png,
    )
    try:
        with pytest.raises(VisualFidelityError) as exc:
            _extract(pdf, visuals, chunks, memory=alias_memory)
        assert exc.value.code == "memory_limit_exceeded"
        assert _memory_state(alias_memory) == before
    finally:
        alias_memory.release_buffer(preexisting)


def test_crop_resolver_has_no_four_visual_slice_or_silent_continue() -> None:
    pdf = _pdf()
    visuals = tuple(_visual(f"sv-{index:04d}") for index in range(1, 7))
    crops = _extract(pdf, visuals)
    assert [crop.visual.visual_id for crop in crops] == [
        f"sv-{index:04d}" for index in range(1, 7)
    ]

    invalid = visuals[:3] + (
        _visual("sv-0004", box=(0.0, 0.0, 1.0, 1.0)),
    ) + visuals[4:]
    memory = _memory()
    with pytest.raises(VisualFidelityError) as exc:
        _extract(pdf, invalid, memory=memory)
    assert exc.value.code == "crop_failed"
    assert memory.source_bytes == 0
    assert memory.page_cache_bytes == 0


def _png_image(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _asset_crop(
    *,
    background: tuple[int, int, int] = (248, 248, 246),
    labels: tuple[SourceLabel, ...] | None = None,
    saturated: bool = False,
) -> SourceCrop:
    width, height = 40, 30
    image = Image.new("RGBA", (width, height), (*background, 255))
    draw = ImageDraw.Draw(image)
    if max(background) <= 120:
        draw.line((4, 4, 35, 25), fill=(242, 242, 242, 255), width=2)
        draw.rectangle((5, 7, 9, 12), fill=(224, 224, 224, 255))
    else:
        draw.line((4, 4, 35, 25), fill=(25, 35, 62, 255), width=2)
        draw.rectangle((5, 7, 9, 12), fill=(20, 28, 48, 255))
    if saturated:
        draw.rectangle((26, 4, 33, 10), fill=(230, 35, 55, 255))
        draw.line((26, 18, 34, 18), fill=(20, 95, 235, 255), width=2)
    actual_labels = (
        (
            SourceLabel(
                text="Na+ / Cl-",
                box=(10.0, 16.0, 30.0, 46.0),
                anchor=(35.0, 50.0),
            ),
        )
        if labels is None
        else labels
    )
    visual = _visual("sv-0001").model_copy(update={"exact_labels": actual_labels})
    try:
        return SourceCrop(
            visual=visual,
            source_png=_png_image(image),
            width_px=width,
            height_px=height,
        )
    finally:
        image.close()


def _release_prepared(memory: VisualMemoryBudget, prepared) -> None:
    for reservation in reversed(prepared.temporary_label_reservations):
        memory.release_buffer(reservation)
    memory.close()


def test_light_uniform_background_becomes_transparent_without_moving_foreground() -> None:
    crop = _asset_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    source = _decoded(crop.source_png).convert("RGBA")
    result = _decoded(prepared.png).convert("RGBA")
    try:
        assert result.size == source.size == (crop.width_px, crop.height_px)
        assert result.getpixel((0, 0))[3] == 0
        source_foreground = {
            (x, y)
            for y in range(source.height)
            for x in range(source.width)
            if source.getpixel((x, y))[:3] != (248, 248, 246)
        }
        result_foreground = {
            (x, y)
            for y in range(result.height)
            for x in range(result.width)
            if result.getpixel((x, y))[3] > 0
        }
        assert source_foreground <= result_foreground
        assert prepared.foreground_recall == 1.0
        print(
            "task6-alpha-background-ratio",
            sum(result.getpixel((x, y))[3] == 0 for y in range(result.height) for x in range(result.width))
            / (result.width * result.height),
        )
        print("task6-foreground-recall", prepared.foreground_recall)
    finally:
        source.close()
        result.close()
        _release_prepared(memory, prepared)


def test_dark_background_becomes_transparent_and_light_ink_becomes_notes_ink() -> None:
    crop = _asset_crop(background=(18, 20, 24))
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    result = _decoded(prepared.png).convert("RGBA")
    try:
        assert prepared.background.mode == "dark"
        assert result.getpixel((0, 0))[3] == 0
        assert result.getpixel((5, 5))[:3] == NOTES_INK_RGB
        assert result.getpixel((5, 5))[3] == 255
    finally:
        result.close()
        _release_prepared(memory, prepared)


def test_saturated_instructional_colours_survive_dark_conversion() -> None:
    crop = _asset_crop(background=(18, 20, 24), saturated=True)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    source = _decoded(crop.source_png).convert("RGBA")
    result = _decoded(prepared.png).convert("RGBA")
    try:
        for point in ((28, 6), (30, 18)):
            assert result.getpixel(point) == source.getpixel(point)
        print(
            "task6-saturated-max-channel-delta",
            max(
                abs(result.getpixel(point)[channel] - source.getpixel(point)[channel])
                for point in ((28, 6), (30, 18))
                for channel in range(3)
            ),
        )
    finally:
        source.close()
        result.close()
        _release_prepared(memory, prepared)


def test_low_confidence_background_or_lost_foreground_fails_closed(monkeypatch) -> None:
    image = Image.new("RGBA", (40, 30), (248, 248, 246, 255))
    draw = ImageDraw.Draw(image)
    for x in range(40):
        draw.point((x, 0), fill=((20, 20, 20, 255) if x % 2 else (248, 248, 246, 255)))
        draw.point((x, 29), fill=((248, 248, 246, 255) if x % 2 else (20, 20, 20, 255)))
    crop = _asset_crop()
    crop = replace(crop, source_png=_png_image(image))
    image.close()
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"

    import notesbot.visual_assets as assets_module

    good = _asset_crop()
    original = assets_module._build_transparent_rgba

    def lose_foreground(*args, **kwargs):
        rgba, source_mask = original(*args, **kwargs)
        rgba[source_mask] = (0, 0, 0, 0)
        return rgba, source_mask

    monkeypatch.setattr(assets_module, "_build_transparent_rgba", lose_foreground)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(good, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"


def test_prepare_fallback_never_mutates_source_crop_bytes_or_hash() -> None:
    crop = _asset_crop()
    original_bytes = crop.source_png
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        assert crop.source_png is original_bytes
        assert hashlib.sha256(crop.source_png).hexdigest() == original_hash
        assert prepared.png is not crop.source_png
    finally:
        _release_prepared(memory, prepared)


def test_label_layers_preserve_exact_glyph_masks_box_order_and_anchor() -> None:
    labels = (
        SourceLabel(text=" left ", box=(10.0, 16.0, 30.0, 46.0), anchor=(35.0, 50.0)),
        SourceLabel(text="RIGHT", box=(60.0, 10.0, 88.0, 40.0), anchor=None),
    )
    crop = _asset_crop(labels=labels, saturated=True)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        assert [(layer.label_index, layer.text, layer.box, layer.anchor) for layer in prepared.label_layers] == [
            (0, labels[0].text, labels[0].box, labels[0].anchor),
            (1, labels[1].text, labels[1].box, labels[1].anchor),
        ]
        print(
            "task6-ordered-label-metadata",
            [(layer.label_index, layer.text, layer.box, layer.anchor) for layer in prepared.label_layers],
        )
        validate_composited_labels(
            composite_source_labels(_transparent_png(40, 30), crop, prepared),
            crop,
            prepared,
        )
    finally:
        _release_prepared(memory, prepared)


def _transparent_png(width: int, height: int) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    try:
        return _png_image(image)
    finally:
        image.close()


def test_duplicate_identical_glyph_hashes_use_record_index_box_and_anchor_identity() -> None:
    labels = (
        SourceLabel(text="X", box=(10.0, 16.0, 30.0, 46.0), anchor=(35.0, 50.0)),
        SourceLabel(text="X", box=(10.0, 16.0, 30.0, 46.0), anchor=(70.0, 80.0)),
    )
    crop = _asset_crop(labels=labels)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        assert len(prepared.label_layers) == 2
        assert prepared.label_layers[0].source_mask_sha256 == prepared.label_layers[1].source_mask_sha256
        assert prepared.label_layers[0].label_index == 0
        assert prepared.label_layers[1].label_index == 1
        assert prepared.label_layers[0].anchor != prepared.label_layers[1].anchor
    finally:
        _release_prepared(memory, prepared)


def test_overlapping_label_boxes_composite_one_union_canvas_without_double_alpha() -> None:
    labels = (
        SourceLabel(text="A", box=(10.0, 10.0, 60.0, 60.0)),
        SourceLabel(text="B", box=(20.0, 20.0, 70.0, 70.0)),
    )
    crop = _asset_crop(labels=labels)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    once = composite_source_labels(_transparent_png(40, 30), crop, prepared)
    composite = _decoded(once).convert("RGBA")
    union = _decoded(prepared.label_union_png).convert("RGBA")
    try:
        assert ImageChops.difference(composite, union).getbbox() is None
        assert composite.getpixel((8, 7))[3] == union.getpixel((8, 7))[3]
    finally:
        composite.close()
        union.close()
        _release_prepared(memory, prepared)


def test_composite_labels_uses_original_normalized_positions_and_canvas_size() -> None:
    crop = _asset_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    composite_png = composite_source_labels(_transparent_png(40, 30), crop, prepared)
    composite = _decoded(composite_png).convert("RGBA")
    try:
        assert composite.size == (40, 30)
        assert composite.getpixel((5, 8))[3] > 0
        assert composite.getpixel((2, 8))[3] == 0
        validate_composited_labels(composite_png, crop, prepared)
    finally:
        composite.close()
        _release_prepared(memory, prepared)


def test_fallback_and_label_layers_respect_aggregate_asset_and_resident_caps() -> None:
    crop = _asset_crop()
    baseline_memory = _memory()
    baseline = prepare_source_fallback(crop, _budget(), baseline_memory)
    needed = baseline_memory.asset_bytes
    _release_prepared(baseline_memory, baseline)

    for memory in (
        VisualMemoryBudget(needed - 1, 1, 1, needed * 2, 1),
        VisualMemoryBudget(needed * 2, 1, 1, needed - 1, 1),
    ):
        with pytest.raises(VisualFidelityError) as exc:
            prepare_source_fallback(crop, _budget(), memory)
        assert exc.value.code == "memory_limit_exceeded"
        assert memory.asset_bytes == 0


def test_fallback_union_label_layer_and_composite_inflation_hit_per_item_caps(monkeypatch) -> None:
    import notesbot.visual_assets as assets_module

    crop = _asset_crop()
    baseline_memory = _memory()
    baseline = prepare_source_fallback(crop, _budget(), baseline_memory)
    try:
        layer_size = len(baseline.label_layers[0].png)
        fallback_size = len(baseline.png)
        union_size = len(baseline.label_union_png)
        composite_size = len(composite_source_labels(_transparent_png(40, 30), crop, baseline))
    finally:
        _release_prepared(baseline_memory, baseline)

    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PNG_BYTES", layer_size - 1)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"

    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PNG_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", fallback_size - 1)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"

    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", max(fallback_size, union_size) + 8)
    real_encode = assets_module._encode_png
    encode_calls = 0

    def inflate_union(rgba):
        nonlocal encode_calls
        encode_calls += 1
        encoded = real_encode(rgba)
        if encode_calls == 3:
            return encoded + b"x" * (config.MAX_VISUAL_PNG_BYTES - len(encoded) + 1)
        return encoded

    monkeypatch.setattr(assets_module, "_encode_png", inflate_union)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"
    monkeypatch.setattr(assets_module, "_encode_png", real_encode)

    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PIXELS", 8 * 9 - 1)
    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", 10 * 1024 * 1024)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PIXELS", 2_000_000)

    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        candidate_cap = max(fallback_size, union_size, composite_size) + 1
        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", candidate_cap)
        padded_candidate = _transparent_png(40, 30) + b"x" * candidate_cap
        with pytest.raises(VisualFidelityError) as exc:
            composite_source_labels(padded_candidate, crop, prepared)
        assert exc.value.code == "fallback_damaged"

        valid_composite = composite_source_labels(_transparent_png(40, 30), crop, prepared)
        padded_composite = valid_composite + b"x" * candidate_cap
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(padded_composite, crop, prepared)
        assert exc.value.code == "fallback_damaged"

        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", composite_size - 1)
        with pytest.raises(VisualFidelityError) as exc:
            composite_source_labels(_transparent_png(40, 30), crop, prepared)
        assert exc.value.code == "fallback_damaged"
    finally:
        _release_prepared(memory, prepared)


def test_light_and_dark_luminance_thresholds_are_closed_boundaries() -> None:
    for background, mode in (((180, 180, 180), "light"), ((120, 120, 120), "dark")):
        crop = _asset_crop(background=background)
        memory = _memory()
        prepared = prepare_source_fallback(crop, _budget(), memory)
        try:
            assert prepared.background.mode == mode
        finally:
            _release_prepared(memory, prepared)

    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(
            _asset_crop(background=(179, 179, 179)),
            _budget(),
            _memory(),
        )
    assert exc.value.code == "fallback_damaged"


def test_fallback_and_label_work_checks_shared_deadline_before_and_after_each_image_operation() -> None:
    crop = _asset_crop()

    class RecordingBudget:
        def __init__(self, expire_at: int | None = None) -> None:
            self.calls = 0
            self.expire_at = expire_at

        def check_deadline(self) -> None:
            self.calls += 1
            if self.expire_at is not None and self.calls >= self.expire_at:
                raise VisualFidelityError("deadline_exceeded")

    recording = RecordingBudget()
    memory = _memory()
    prepared = prepare_source_fallback(crop, recording, memory)
    try:
        assert recording.calls == 12
    finally:
        _release_prepared(memory, prepared)

    for expire_at in range(1, 13):
        memory = _memory()
        with pytest.raises(VisualFidelityError) as exc:
            prepare_source_fallback(crop, RecordingBudget(expire_at), memory)
        assert exc.value.code == "deadline_exceeded"
        assert memory.asset_bytes == 0


@pytest.mark.parametrize("asset_reservations_before_failure", (1, 2))
def test_prepare_failure_unwinds_owned_asset_handles_and_preserves_prior_source_context(
    monkeypatch,
    asset_reservations_before_failure: int,
) -> None:
    import notesbot.visual_assets as assets_module

    crop = _asset_crop(labels=(
        SourceLabel(text="A", box=(10.0, 16.0, 30.0, 46.0)),
        SourceLabel(text="B", box=(12.5, 16.0, 32.5, 46.0)),
    ))
    memory = _memory()
    source_owner = memory.reserve_buffer("source", crop.source_png)
    context_owner = memory.reserve_buffer("context", b"context-owner")
    assert source_owner is not None and context_owner is not None
    real_reserve = memory.reserve_buffer
    real_release = memory.release_buffer
    owned_assets = []
    released_assets = []

    def fail_after_two_assets(kind, data):
        if kind == "asset" and len(owned_assets) == asset_reservations_before_failure:
            raise VisualFidelityError("memory_limit_exceeded")
        reservation = real_reserve(kind, data)
        if kind == "asset" and reservation is not None:
            owned_assets.append(reservation)
        return reservation

    def recording_release(reservation):
        released_assets.append(reservation)
        real_release(reservation)

    monkeypatch.setattr(memory, "reserve_buffer", fail_after_two_assets)
    monkeypatch.setattr(memory, "release_buffer", recording_release)
    with pytest.raises(VisualFidelityError) as exc:
        assets_module.prepare_source_fallback(crop, _budget(), memory)
    assert exc.value.code == "memory_limit_exceeded"
    assert memory.asset_bytes == 0
    assert released_assets == list(reversed(owned_assets))
    assert memory.source_bytes == len(crop.source_png)
    assert memory.context_bytes == len(b"context-owner")
    memory.close()


def test_successful_prepared_fallback_keeps_fallback_until_terminal_close() -> None:
    crop = _asset_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    all_asset_bytes = memory.asset_bytes
    temporary_bytes = sum(item.size for item in prepared.temporary_label_reservations)
    assert all_asset_bytes == len(prepared.png) + temporary_bytes
    assert prepared.temporary_label_reservations[:-1] == tuple(
        layer.reservation for layer in prepared.label_layers
    )
    assert prepared.temporary_label_reservations[-1].data is prepared.label_union_png
    for reservation in reversed(prepared.temporary_label_reservations):
        memory.release_buffer(reservation)
    assert memory.asset_bytes == len(prepared.png)
    memory.close()
    assert memory.asset_bytes == 0


def test_memory_close_is_terminal_idempotent_and_fails_with_live_page_cache() -> None:
    memory = _memory()
    source = memory.reserve_buffer("source", b"source")
    context = memory.reserve_buffer("context", b"context")
    asset = memory.reserve_buffer("asset", b"asset")
    assert source is not None and context is not None and asset is not None
    memory.reserve_page("page", 16)
    with pytest.raises(VisualFidelityError) as exc:
        memory.close()
    assert exc.value.code == "memory_limit_exceeded"
    assert memory.resident_bytes() > 0
    memory.release_page("page")
    memory.close()
    memory.close()
    assert _memory_state(memory) == (0, 0, 0, 0, 0)
    assert memory._buffer_entries == {}
    assert memory._buffer_token_by_object_id == {}
    for operation in (
        lambda: memory.reserve_buffer("asset", b"later"),
        lambda: memory.reserve_page("later-page", 1),
        lambda: memory.page_would_fit(1),
        lambda: memory.release_buffer(asset),
    ):
        with pytest.raises(VisualFidelityError) as exc:
            operation()
        assert exc.value.code == "memory_limit_exceeded"


_BITMAP_GLYPHS = {
    "α": (
        "01110",
        "10001",
        "10001",
        "11111",
        "10001",
        "10011",
        "01101",
    ),
    "Δ": (
        "00100",
        "01010",
        "01010",
        "10001",
        "10001",
        "11111",
        "11111",
    ),
    "Na⁺": (
        "100010111000100",
        "110010001000100",
        "101010111001110",
        "100110001000100",
        "100010111000100",
        "100010000000000",
        "100010000000000",
    ),
    "Cl⁻": (
        "011100100000000",
        "100000100000000",
        "100000100001110",
        "100000100000000",
        "100000100000000",
        "100000100000000",
        "011100111000000",
    ),
    "H₂O": (
        "100010000001110",
        "100010000010001",
        "111110000010001",
        "100010110010001",
        "100010001001110",
        "100010010000000",
        "100010111000000",
    ),
    "photosythesis": (
        "1110011101010101010111010111010101011101010101110",
        "1000010101010101010101010101010101010101010101000",
        "1110011101110111010101010111010101011101110101110",
        "1000010101010101010101010001010101010101010100010",
        "1000010101010101110111010111011101011101010101110",
        "0000000000000000000000000000000100000000000000000",
        "0000000000000000000000000000001100000000000000000",
    ),
    "X": (
        "10001",
        "01010",
        "00100",
        "00100",
        "01010",
        "10001",
        "10001",
    ),
}


def _draw_bitmap_glyph(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill: tuple[int, int, int, int],
    scale: int = 2,
) -> None:
    pattern = _BITMAP_GLYPHS[text]
    x0, y0 = xy
    for row, pixels in enumerate(pattern):
        for column, value in enumerate(pixels):
            if value == "1":
                draw.rectangle(
                    (
                        x0 + column * scale,
                        y0 + row * scale,
                        x0 + (column + 1) * scale - 1,
                        y0 + (row + 1) * scale - 1,
                    ),
                    fill=fill,
                )


def _glyph_crop() -> SourceCrop:
    width, height = 360, 180
    background = (250, 250, 248, 255)
    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    records = (
        ("α", 8, 8),
        ("Δ", 128, 8),
        ("Na⁺", 248, 8),
        ("Cl⁻", 8, 98),
        ("H₂O", 128, 98),
        ("photosythesis", 248, 98),
    )
    labels = []
    for index, (text, x, y) in enumerate(records):
        _draw_bitmap_glyph(
            draw,
            (x + 5, y + 10),
            text,
            fill=(24, 34, 62, 255),
            scale=1 if text == "photosythesis" else 2,
        )
        labels.append(
            SourceLabel(
                text=text,
                box=(
                    x * 100.0 / width,
                    y * 100.0 / height,
                    (x + 104) * 100.0 / width,
                    (y + 64) * 100.0 / height,
                ),
                anchor=(min(x + 100 + index, width - 1) * 100.0 / width, (y + 70) * 100.0 / height),
            )
        )
    visual = _visual("sv-0001").model_copy(update={"exact_labels": tuple(labels)})
    try:
        return SourceCrop(
            visual=visual,
            source_png=_png_image(image),
            width_px=width,
            height_px=height,
        )
    finally:
        image.close()


def _oracle_bounds(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    return (
        math.floor(width * box[0] / 100.0),
        math.floor(height * box[1] / 100.0),
        math.ceil(width * box[2] / 100.0),
        math.ceil(height * box[3] / 100.0),
    )


def _independent_label_mask(
    crop: SourceCrop, label: SourceLabel
) -> tuple[tuple[int, int, int, int], bytes, list[tuple[int, int]]]:
    source = _decoded(crop.source_png).convert("RGBA")
    bounds = _oracle_bounds(label.box, crop.width_px, crop.height_px)
    x0, y0, x1, y1 = bounds
    mask = bytearray()
    points = []
    try:
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixel = source.getpixel((x, y))
                distance = math.sqrt(
                    (pixel[0] - 250) ** 2
                    + (pixel[1] - 250) ** 2
                    + (pixel[2] - 248) ** 2
                )
                foreground = pixel[3] > 0 and distance > 18.0
                mask.append(1 if foreground else 0)
                if foreground:
                    points.append((x, y))
    finally:
        source.close()
    return bounds, bytes(mask), points


def test_utf8_source_glyphs_have_independent_exact_masks_and_ordered_identity() -> None:
    crop = _glyph_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        assert [layer.text for layer in prepared.label_layers] == [
            "α",
            "Δ",
            "Na⁺",
            "Cl⁻",
            "H₂O",
            "photosythesis",
        ]
        for index, (label, layer) in enumerate(
            zip(crop.visual.exact_labels, prepared.label_layers)
        ):
            bounds, oracle_mask, points = _independent_label_mask(crop, label)
            assert points, f"fixture did not render glyph pixels for record {index}"
            assert layer.label_index == index
            assert layer.text == label.text
            assert layer.box == label.box
            assert layer.anchor == label.anchor
            assert layer.source_mask_sha256 == hashlib.sha256(oracle_mask).hexdigest()
            decoded = _decoded(layer.png).convert("RGBA")
            try:
                observed = bytes(
                    1 if decoded.getpixel((x, y))[3] > 0 else 0
                    for y in range(decoded.height)
                    for x in range(decoded.width)
                )
            finally:
                decoded.close()
            assert observed == oracle_mask
            assert decoded.size == (bounds[2] - bounds[0], bounds[3] - bounds[1])
    finally:
        _release_prepared(memory, prepared)


def _partial_alpha_overlap_crop() -> SourceCrop:
    width, height = 72, 54
    image = Image.new("RGBA", (width, height), (248, 248, 246, 255))
    draw = ImageDraw.Draw(image)
    _draw_bitmap_glyph(draw, (24, 16), "X", fill=(25, 35, 62, 128), scale=3)
    labels = (
        SourceLabel(text="X-left", box=(20.0, 10.0, 70.0, 85.0), anchor=(10.0, 50.0)),
        SourceLabel(text="X-right", box=(25.0, 15.0, 75.0, 90.0), anchor=(90.0, 50.0)),
    )
    visual = _visual("sv-0001").model_copy(update={"exact_labels": labels})
    try:
        return SourceCrop(visual, _png_image(image), width, height)
    finally:
        image.close()


def test_partial_alpha_overlap_is_applied_once_and_double_application_is_rejected() -> None:
    crop = _partial_alpha_overlap_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    union = _decoded(prepared.label_union_png).convert("RGBA")
    try:
        partial_points = [
            (x, y)
            for y in range(union.height)
            for x in range(union.width)
            if 0 < union.getpixel((x, y))[3] < 255
        ]
        assert partial_points
        point = partial_points[len(partial_points) // 2]
        source_alpha = union.getpixel(point)[3]
        once_png = composite_source_labels(
            _transparent_png(crop.width_px, crop.height_px), crop, prepared
        )
        once = _decoded(once_png).convert("RGBA")
        try:
            assert once.getpixel(point)[3] == source_alpha
        finally:
            once.close()

        transparent = Image.new("RGBA", union.size, (0, 0, 0, 0))
        try:
            double = Image.alpha_composite(
                Image.alpha_composite(transparent, union),
                union,
            )
            double_png = _png_image(double)
            assert double.getpixel(point)[3] > source_alpha
        finally:
            transparent.close()
            double.close()
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(double_png, crop, prepared)
        assert exc.value.code == "fallback_damaged"
    finally:
        union.close()
        _release_prepared(memory, prepared)


def _mutated_png(png: bytes, mutate) -> bytes:
    image = _decoded(png).convert("RGBA")
    try:
        mutate(image)
        return _png_image(image)
    finally:
        image.close()


def test_removed_union_stroke_cannot_be_hidden_by_opaque_candidate() -> None:
    crop = _glyph_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    _, _, points = _independent_label_mask(crop, crop.visual.exact_labels[0])
    point = points[len(points) // 2]
    source = _decoded(crop.source_png).convert("RGBA")
    try:
        expected = source.getpixel(point)
    finally:
        source.close()
    damaged_union = _mutated_png(
        prepared.label_union_png,
        lambda image: image.putpixel(point, (expected[0], expected[1], expected[2], 0)),
    )
    damaged = replace(prepared, label_union_png=damaged_union)
    candidate = Image.new("RGBA", (crop.width_px, crop.height_px), (0, 0, 0, 0))
    try:
        candidate.putpixel(point, (expected[0], expected[1], expected[2], 255))
        candidate_png = _png_image(candidate)
    finally:
        candidate.close()
    try:
        with pytest.raises(VisualFidelityError) as exc:
            composite_source_labels(candidate_png, crop, damaged)
        assert exc.value.code == "fallback_damaged"
    finally:
        _release_prepared(memory, prepared)


@pytest.mark.parametrize(
    "mutation",
    (
        "stroke",
        "hash",
        "box",
        "anchor",
        "profile",
        "handles",
        "placement",
        "extra",
        "outside",
    ),
)
def test_each_label_stroke_hash_box_anchor_and_position_mutation_fails_closed(
    mutation: str,
) -> None:
    crop = _glyph_crop()
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    _, _, points = _independent_label_mask(crop, crop.visual.exact_labels[0])
    point = points[len(points) // 2]
    mutated_owner = None
    candidate = prepared
    if mutation == "stroke":
        layer = prepared.label_layers[0]
        x0, y0, _, _ = _oracle_bounds(layer.box, crop.width_px, crop.height_px)
        changed_png = _mutated_png(
            layer.png,
            lambda image: image.putpixel((point[0] - x0, point[1] - y0), (0, 0, 0, 0)),
        )
        mutated_owner = memory.reserve_buffer("asset", changed_png)
        assert mutated_owner is not None
        changed_layer = replace(layer, png=changed_png, reservation=mutated_owner)
        candidate = replace(
            prepared,
            label_layers=(changed_layer,) + prepared.label_layers[1:],
        )
    elif mutation == "hash":
        changed_layer = replace(prepared.label_layers[0], source_mask_sha256="0" * 64)
        candidate = replace(prepared, label_layers=(changed_layer,) + prepared.label_layers[1:])
    elif mutation == "box":
        changed_layer = replace(prepared.label_layers[0], box=(1.0, 1.0, 20.0, 20.0))
        candidate = replace(prepared, label_layers=(changed_layer,) + prepared.label_layers[1:])
    elif mutation == "anchor":
        changed_layer = replace(prepared.label_layers[0], anchor=(99.0, 99.0))
        candidate = replace(prepared, label_layers=(changed_layer,) + prepared.label_layers[1:])
    elif mutation == "profile":
        candidate = replace(
            prepared,
            background=replace(
                prepared.background,
                confidence=prepared.background.confidence - 0.01,
            ),
        )
    elif mutation == "handles":
        candidate = replace(
            prepared,
            temporary_label_reservations=tuple(
                reversed(prepared.temporary_label_reservations)
            ),
        )
    else:
        x0, y0, x1, y1 = _oracle_bounds(
            prepared.label_layers[0].box, crop.width_px, crop.height_px
        )
        foreground_points = set(points)
        empty_point = next(
            (x, y)
            for y in range(y0, y1)
            for x in range(x0, x1)
            if (x, y) not in foreground_points
        )
        source = _decoded(prepared.label_union_png).convert("RGBA")
        try:
            pixel = source.getpixel(point)
            if mutation == "placement":
                source.putpixel(point, (pixel[0], pixel[1], pixel[2], 0))
                source.putpixel(empty_point, pixel)
            elif mutation == "extra":
                source.putpixel(empty_point, pixel)
            else:
                source.putpixel((crop.width_px - 1, crop.height_px - 1), pixel)
            changed_union = _png_image(source)
        finally:
            source.close()
        candidate = replace(prepared, label_union_png=changed_union)
    try:
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(prepared.png, crop, candidate)
        assert exc.value.code == "fallback_damaged"
    finally:
        if mutated_owner is not None:
            memory.release_buffer(mutated_owner)
        _release_prepared(memory, prepared)


def test_mostly_low_contrast_multistroke_glyph_fails_whole_record() -> None:
    width, height = 48, 36
    image = Image.new("RGBA", (width, height), (250, 250, 248, 255))
    draw = ImageDraw.Draw(image)
    low = (215, 215, 215, 255)
    draw.line((12, 9, 12, 26), fill=low, width=2)
    draw.line((26, 9, 26, 26), fill=low, width=2)
    draw.line((12, 18, 26, 18), fill=low, width=2)
    draw.point((12, 9), fill=(20, 30, 55, 255))
    label = SourceLabel(text="H₂O", box=(20.0, 15.0, 65.0, 80.0), anchor=(75.0, 50.0))
    visual = _visual("sv-0001").model_copy(update={"exact_labels": (label,)})
    try:
        crop = SourceCrop(visual, _png_image(image), width, height)
    finally:
        image.close()
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"


def test_zero_label_union_is_exactly_empty_full_canvas() -> None:
    crop = _asset_crop(labels=())
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    union = _decoded(prepared.label_union_png).convert("RGBA")
    try:
        assert union.getbbox() is None
        changed = union.copy()
        try:
            changed.putpixel((0, 0), (20, 30, 55, 128))
            damaged = replace(prepared, label_union_png=_png_image(changed))
        finally:
            changed.close()
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(prepared.png, crop, damaged)
        assert exc.value.code == "fallback_damaged"
    finally:
        union.close()
        _release_prepared(memory, prepared)


@pytest.mark.parametrize(
    "release_timing", ("before-removal", "after-release", "no-op")
)
def test_prepare_unwind_surfaces_only_incomplete_owned_release(
    monkeypatch,
    release_timing: str,
) -> None:
    crop = _asset_crop(
        labels=(
            SourceLabel(text="A", box=(10.0, 16.0, 30.0, 46.0)),
            SourceLabel(text="B", box=(12.5, 16.0, 32.5, 46.0)),
        )
    )
    memory = _memory()
    source_owner = memory.reserve_buffer("source", crop.source_png)
    assert source_owner is not None
    real_reserve = memory.reserve_buffer
    real_release = memory.release_buffer
    assets = []
    injected_release = False

    def failing_reserve(kind, data):
        if kind == "asset" and len(assets) == 2:
            raise VisualFidelityError("fallback_damaged")
        reservation = real_reserve(kind, data)
        if kind == "asset" and reservation is not None:
            assets.append(reservation)
        return reservation

    def failing_release(reservation):
        nonlocal injected_release
        if not injected_release:
            injected_release = True
            if release_timing == "before-removal":
                raise RuntimeError("release blocked before ownership removal")
            if release_timing == "no-op":
                return
            real_release(reservation)
            raise RuntimeError("hook failed after exact release")
        real_release(reservation)

    monkeypatch.setattr(memory, "reserve_buffer", failing_reserve)
    monkeypatch.setattr(memory, "release_buffer", failing_release)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), memory)
    if release_timing in ("before-removal", "no-op"):
        assert exc.value.code == "memory_limit_exceeded"
        assert memory.asset_bytes == assets[-1].size
    else:
        assert exc.value.code == "fallback_damaged"
        assert memory.asset_bytes == 0
    assert memory.source_bytes == len(crop.source_png)
    memory.close()
    assert _memory_state(memory) == (0, 0, 0, 0, 0)


def _probe_crop(
    probe_rgb: tuple[int, int, int],
    *,
    background: tuple[int, int, int] = (240, 240, 240),
) -> tuple[SourceCrop, tuple[int, int]]:
    width, height = 30, 20
    image = Image.new("RGBA", (width, height), (*background, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 9, 10), fill=(25, 35, 62, 255))
    probe = (24, 15)
    draw.point(probe, fill=(*probe_rgb, 255))
    label = SourceLabel(text="probe", box=(10.0, 15.0, 40.0, 60.0), anchor=(45.0, 50.0))
    visual = _visual("sv-0001").model_copy(update={"exact_labels": (label,)})
    try:
        return SourceCrop(visual, _png_image(image), width, height), probe
    finally:
        image.close()


def test_background_distance_exact_boundary_is_removed_and_one_beyond_survives() -> None:
    cases = (((222, 240, 240), 0), ((221, 240, 240), 255))
    for rgb, expected_alpha in cases:
        crop, probe = _probe_crop(rgb)
        memory = _memory()
        prepared = prepare_source_fallback(crop, _budget(), memory)
        result = _decoded(prepared.png).convert("RGBA")
        try:
            assert result.getpixel(probe)[3] == expected_alpha
        finally:
            result.close()
            _release_prepared(memory, prepared)


def _confidence_boundary_crop(outlier_count: int) -> SourceCrop:
    width = height = 6
    image = Image.new("RGBA", (width, height), (240, 240, 240, 255))
    draw = ImageDraw.Draw(image)
    border = (
        [(x, 0) for x in range(width)]
        + [(x, height - 1) for x in range(width)]
        + [(0, y) for y in range(1, height - 1)]
        + [(width - 1, y) for y in range(1, height - 1)]
    )
    for point in border[:outlier_count]:
        draw.point(point, fill=(30, 30, 30, 255))
    draw.point((2, 2), fill=(20, 30, 55, 255))
    label = SourceLabel(text="x", box=(20.0, 20.0, 60.0, 60.0), anchor=None)
    visual = _visual("sv-0001").model_copy(update={"exact_labels": (label,)})
    try:
        return SourceCrop(visual, _png_image(image), width, height)
    finally:
        image.close()


def test_background_confidence_exact_point_85_passes_and_one_border_pixel_beyond_fails() -> None:
    memory = _memory()
    prepared = prepare_source_fallback(_confidence_boundary_crop(3), _budget(), memory)
    try:
        assert prepared.background.confidence == 0.85
    finally:
        _release_prepared(memory, prepared)

    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(_confidence_boundary_crop(4), _budget(), _memory())
    assert exc.value.code == "fallback_damaged"


def _dark_saturation_crop(rgb: tuple[int, int, int]) -> tuple[SourceCrop, tuple[int, int]]:
    width, height = 30, 20
    image = Image.new("RGBA", (width, height), (20, 20, 20, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 5, 11, 11), fill=(235, 235, 235, 255))
    probe = (22, 15)
    draw.rectangle((20, 13, 24, 17), fill=(*rgb, 255))
    label = SourceLabel(text="sat", box=(15.0, 20.0, 45.0, 65.0), anchor=None)
    visual = _visual("sv-0001").model_copy(update={"exact_labels": (label,)})
    try:
        return SourceCrop(visual, _png_image(image), width, height), probe
    finally:
        image.close()


def test_saturation_point_25_is_preserved_and_one_channel_below_becomes_notes_ink() -> None:
    exact = (240, 180, 180)
    below = (240, 181, 181)
    assert (max(exact) - min(exact)) / max(exact) == 0.25
    assert (max(below) - min(below)) / max(below) < 0.25
    for rgb, expected in ((exact, exact), (below, NOTES_INK_RGB)):
        crop, probe = _dark_saturation_crop(rgb)
        memory = _memory()
        prepared = prepare_source_fallback(crop, _budget(), memory)
        result = _decoded(prepared.png).convert("RGBA")
        try:
            assert result.getpixel(probe)[:3] == expected
        finally:
            result.close()
            _release_prepared(memory, prepared)


def test_hue_delta_and_eighty_percent_saturation_have_exact_closed_boundaries() -> None:
    import notesbot.visual_assets as assets_module

    source_hue = np.asarray([0.20])
    source_saturation = np.asarray([0.75])
    boundary_hue = source_hue + assets_module.SATURATED_HUE_MAX_DELTA
    assets_module._validate_saturated_colour_metrics(
        source_hue,
        boundary_hue,
        source_saturation,
        source_saturation * 0.8,
    )
    with pytest.raises(VisualFidelityError) as exc:
        assets_module._validate_saturated_colour_metrics(
            source_hue,
            np.nextafter(boundary_hue, math.inf),
            source_saturation,
            source_saturation * 0.8,
        )
    assert exc.value.code == "fallback_damaged"
    zero_hue = np.asarray([0.0])
    assets_module._validate_saturated_colour_metrics(
        zero_hue,
        np.asarray([assets_module.SATURATED_HUE_MAX_DELTA]),
        source_saturation,
        source_saturation,
    )
    with pytest.raises(VisualFidelityError) as exc:
        assets_module._validate_saturated_colour_metrics(
            zero_hue,
            np.asarray(
                [np.nextafter(assets_module.SATURATED_HUE_MAX_DELTA, math.inf)]
            ),
            source_saturation,
            source_saturation,
        )
    assert exc.value.code == "fallback_damaged"
    with pytest.raises(VisualFidelityError) as exc:
        assets_module._validate_saturated_colour_metrics(
            source_hue,
            boundary_hue,
            source_saturation,
            np.nextafter(source_saturation * 0.8, 0.0),
        )
    assert exc.value.code == "fallback_damaged"


@pytest.mark.parametrize("component_size", (5, 6))
def test_exact_mask_recall_rejects_loss_below_and_at_meaningful_component_boundary(
    component_size: int,
) -> None:
    import notesbot.visual_assets as assets_module

    source_mask = np.ones((1, component_size), dtype=bool)
    intact = np.zeros((1, component_size, 4), dtype=np.uint8)
    intact[..., 3] = 255
    assert assets_module._validate_foreground_preservation(source_mask, intact) == 1.0
    damaged = intact.copy()
    damaged[0, -1, 3] = 0
    with pytest.raises(VisualFidelityError) as exc:
        assets_module._validate_foreground_preservation(source_mask, damaged)
    assert exc.value.code == "fallback_damaged"


def test_direct_visual_and_label_pixel_caps_pass_exactly_and_fail_one_beyond(
    monkeypatch,
) -> None:
    crop = _asset_crop()
    visual_pixels = crop.width_px * crop.height_px
    monkeypatch.setattr(config, "MAX_VISUAL_CROP_PIXELS", visual_pixels)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    _release_prepared(memory, prepared)
    monkeypatch.setattr(config, "MAX_VISUAL_CROP_PIXELS", visual_pixels - 1)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"

    monkeypatch.setattr(config, "MAX_VISUAL_CROP_PIXELS", visual_pixels)
    label_bounds = _oracle_bounds(
        crop.visual.exact_labels[0].box, crop.width_px, crop.height_px
    )
    label_pixels = (label_bounds[2] - label_bounds[0]) * (
        label_bounds[3] - label_bounds[1]
    )
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PIXELS", label_pixels)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    _release_prepared(memory, prepared)
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PIXELS", label_pixels - 1)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"


def test_direct_fallback_union_label_and_composite_png_caps_are_closed_boundaries(
    monkeypatch,
) -> None:
    crop = _asset_crop()
    baseline_memory = _memory()
    baseline = prepare_source_fallback(crop, _budget(), baseline_memory)
    transparent = _transparent_png(crop.width_px, crop.height_px)
    composite = composite_source_labels(transparent, crop, baseline)
    label_bounds = _oracle_bounds(
        crop.visual.exact_labels[0].box, crop.width_px, crop.height_px
    )
    noisy_image = Image.new(
        "RGBA", (crop.width_px, crop.height_px), (0, 0, 0, 0)
    )
    for y in range(crop.height_px):
        for x in range(crop.width_px):
            if not (
                label_bounds[0] <= x < label_bounds[2]
                and label_bounds[1] <= y < label_bounds[3]
            ):
                noisy_image.putpixel(
                    (x, y),
                    (
                        (x * 37 + y * 11) % 256,
                        (x * 19 + y * 43) % 256,
                        (x * 53 + y * 7) % 256,
                        255,
                    ),
                )
    try:
        noisy_composite = composite_source_labels(
            _png_image(noisy_image), crop, baseline
        )
    finally:
        noisy_image.close()
    fallback_limit = max(len(baseline.png), len(baseline.label_union_png))
    label_limit = len(baseline.label_layers[0].png)
    union_limit = len(baseline.label_union_png)
    composite_limit = len(noisy_composite)
    assert composite_limit > union_limit
    _release_prepared(baseline_memory, baseline)

    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", fallback_limit)
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PNG_BYTES", label_limit)
    memory = _memory()
    prepared = prepare_source_fallback(crop, _budget(), memory)
    try:
        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", union_limit)
        validate_composited_labels(composite, crop, prepared)
        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", union_limit - 1)
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(composite, crop, prepared)
        assert exc.value.code == "fallback_damaged"

        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", composite_limit)
        validate_composited_labels(noisy_composite, crop, prepared)
        monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", composite_limit - 1)
        with pytest.raises(VisualFidelityError) as exc:
            validate_composited_labels(noisy_composite, crop, prepared)
        assert exc.value.code == "fallback_damaged"
    finally:
        _release_prepared(memory, prepared)

    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", fallback_limit - 1)
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PNG_BYTES", label_limit)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"

    monkeypatch.setattr(config, "MAX_VISUAL_PNG_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(config, "MAX_LABEL_LAYER_PNG_BYTES", label_limit - 1)
    with pytest.raises(VisualFidelityError) as exc:
        prepare_source_fallback(crop, _budget(), _memory())
    assert exc.value.code == "fallback_damaged"
