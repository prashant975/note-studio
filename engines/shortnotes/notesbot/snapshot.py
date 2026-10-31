"""Resolve immutable source crops, with a legacy snapshot compatibility path.

Gemini supplies (page, box%) for each important diagram (see prompts.Snapshot).
We rasterise that page with pypdfium2, crop the box, then normalise the
background so the diagram sits naturally on the white notes page:

  * dark slide  -> inverted, autocontrast, ink tinted dark-blue on white
  * light slide -> per-channel white balance (original colours preserved)
"""
from __future__ import annotations

import io
import math
import traceback
from collections import OrderedDict
from collections.abc import Sequence
from types import MappingProxyType
from typing import NoReturn

import numpy as np
from PIL import Image, ImageOps
from pypdf import PdfReader

from . import config
from .errors import VisualFidelityError, redact
from .pdf_utils import PdfChunk, PdfPageGeometry, page_render_scale
from .pdfium_guard import serialized
from .visual_manifest import (
    BufferReservation,
    SourceContext,
    SourceContextCrop,
    SourceCrop,
    SourceVisual,
    VisualMemoryBudget,
    VisualRunBudget,
)

_RENDER_SCALE = 2.2          # ~158 dpi
_MIN_CROP_PX = 40            # skip absurdly tiny boxes
_MAX_AREA_FRACTION = 0.88    # skip "whole slide" boxes
_INK = np.array([30.0, 40.0, 70.0])  # dark slate-blue, matches the notes theme


def _crop_failure(exc: Exception | None = None) -> NoReturn:
    if exc is None:
        raise VisualFidelityError("crop_failed")
    raise VisualFidelityError("crop_failed") from exc


def _validated_limits(
    render_scale: float,
    max_page_dimension_px: int,
    max_crop_pixels: int,
    max_png_bytes: int,
) -> tuple[float, int, int, int]:
    if (
        isinstance(render_scale, bool)
        or not isinstance(render_scale, (int, float))
        or not math.isfinite(render_scale)
        or render_scale <= 0.0
        or isinstance(max_page_dimension_px, bool)
        or not isinstance(max_page_dimension_px, int)
        or max_page_dimension_px <= 0
        or isinstance(max_crop_pixels, bool)
        or not isinstance(max_crop_pixels, int)
        or max_crop_pixels <= 0
        or isinstance(max_png_bytes, bool)
        or not isinstance(max_png_bytes, int)
        or max_png_bytes <= 0
    ):
        _crop_failure()
    return (
        float(render_scale),
        min(max_page_dimension_px, config.MAX_VISUAL_PAGE_DIMENSION_PX),
        min(max_crop_pixels, config.MAX_VISUAL_CROP_PIXELS),
        min(max_png_bytes, config.MAX_VISUAL_PNG_BYTES),
    )


def _box_tuple(box: object) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(value) for value in box)  # type: ignore[arg-type]
    except Exception as exc:
        _crop_failure(exc)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        _crop_failure()
    x0, y0, x1, y1 = values
    if x1 <= x0 or y1 <= y0:
        _crop_failure()
    return x0, y0, x1, y1


def _chunk_map(
    chunks: Sequence[PdfChunk], total_pages: int
) -> "MappingProxyType[int, PdfChunk]":
    try:
        supplied = tuple(chunks)
    except Exception as exc:
        _crop_failure(exc)
    if not supplied:
        _crop_failure()

    by_index: dict[int, PdfChunk] = {}
    expected_global_page = 1
    for expected_index, chunk in enumerate(supplied):
        if (
            not isinstance(chunk, PdfChunk)
            or isinstance(chunk.index, bool)
            or chunk.index != expected_index
            or chunk.index in by_index
            or isinstance(chunk.page_count, bool)
            or chunk.page_count <= 0
            or chunk.first_global_page != expected_global_page
            or len(chunk.page_geometries) != chunk.page_count
        ):
            _crop_failure()
        for offset, geometry in enumerate(chunk.page_geometries):
            if (
                not isinstance(geometry, PdfPageGeometry)
                or geometry.global_page != chunk.first_global_page + offset
            ):
                _crop_failure()
        by_index[chunk.index] = chunk
        expected_global_page = chunk.last_global_page + 1
    if expected_global_page != total_pages + 1:
        _crop_failure()
    return MappingProxyType(by_index)


def _original_geometry(
    reader_page: object,
    pdfium_page: object,
    global_page: int,
    render_scale: float,
) -> PdfPageGeometry:
    try:
        media_box = _box_tuple(reader_page.mediabox)  # type: ignore[attr-defined]
        crop_box = _box_tuple(reader_page.cropbox)  # type: ignore[attr-defined]
        rotation = int(reader_page.rotation) % 360  # type: ignore[attr-defined]
        user_unit = float(reader_page.user_unit)  # type: ignore[attr-defined]
        width = float(pdfium_page.get_width())
        height = float(pdfium_page.get_height())
    except VisualFidelityError:
        raise
    except Exception as exc:
        _crop_failure(exc)
    if (
        rotation not in (0, 90, 180, 270)
        or not math.isfinite(user_unit)
        or user_unit <= 0.0
        or not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0.0
        or height <= 0.0
    ):
        _crop_failure()
    # An oversized page is measured at its fitted scale (see page_render_scale).
    scale = page_render_scale(width, height, render_scale=render_scale)
    return PdfPageGeometry(
        global_page=global_page,
        media_box=media_box,
        crop_box=crop_box,
        rotation=rotation,
        user_unit=user_unit,
        rendered_width=math.ceil(width * scale),
        rendered_height=math.ceil(height * scale),
    )


def _pixel_bounds(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    if (
        len(box) != 4
        or not all(math.isfinite(value) for value in box)
        or not (0.0 <= box[0] < box[2] <= 100.0)
        or not (0.0 <= box[1] < box[3] <= 100.0)
    ):
        _crop_failure()
    bounds = (
        math.floor(width * box[0] / 100.0),
        math.floor(height * box[1] / 100.0),
        math.ceil(width * box[2] / 100.0),
        math.ceil(height * box[3] / 100.0),
    )
    if not (
        0 <= bounds[0] < bounds[2] <= width
        and 0 <= bounds[1] < bounds[3] <= height
    ):
        _crop_failure()
    return bounds


def _validate_visual_crop_size(width: int, height: int, max_pixels: int) -> None:
    if (
        width < config.MIN_VISUAL_CROP_WIDTH_PX
        or height < config.MIN_VISUAL_CROP_HEIGHT_PX
        or width * height < config.MIN_VISUAL_CROP_PIXELS
        or width * height > max_pixels
    ):
        _crop_failure()


def _validate_context_crop_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > config.MAX_CONTEXT_PIXELS:
        _crop_failure()


def _validate_page_size(
    geometry: PdfPageGeometry, max_page_dimension_px: int
) -> None:
    if (
        geometry.rendered_width <= 0
        or geometry.rendered_height <= 0
        or geometry.rendered_width > max_page_dimension_px
        or geometry.rendered_height > max_page_dimension_px
        or geometry.rendered_width * geometry.rendered_height
        > config.MAX_VISUAL_PAGE_PIXELS
    ):
        _crop_failure()


def _encode_png(
    image: Image.Image,
    budget: VisualRunBudget,
    *,
    byte_limit: int,
) -> bytes:
    output = io.BytesIO()
    try:
        budget.check_deadline()
        image.save(output, format="PNG", optimize=False, compress_level=9)
        budget.check_deadline()
        encoded = output.getvalue()
    except VisualFidelityError:
        raise
    except Exception as exc:
        _crop_failure(exc)
    finally:
        output.close()
    if type(encoded) is not bytes or len(encoded) > byte_limit:
        _crop_failure()
    return encoded


def _release_cached_page(
    memory: VisualMemoryBudget,
    cache_key: int,
    cached: tuple[Image.Image, object],
) -> None:
    image, bitmap = cached
    first_error: Exception | None = None
    try:
        image.close()
    except Exception as exc:
        first_error = exc
    try:
        close = getattr(bitmap, "close", None)
        if close is not None:
            close()
    except Exception as exc:
        if first_error is None:
            first_error = exc
    try:
        memory.release_page(cache_key)
    except Exception as exc:
        if first_error is None:
            first_error = exc
    if first_error is not None:
        raise first_error


def _close_unretained_decode(image: object | None, bitmap: object | None) -> None:
    """Attempt both alias closes; report an error only after both were attempted."""
    first_error: Exception | None = None
    if image is not None:
        try:
            close = getattr(image, "close", None)
            if close is not None:
                close()
        except Exception as exc:
            first_error = exc
    if bitmap is not None:
        try:
            close = getattr(bitmap, "close", None)
            if close is not None:
                close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _construct_source_crop(
    *,
    visual: SourceVisual,
    source_png: bytes,
    width_px: int,
    height_px: int,
    context_crops: Sequence[SourceContextCrop],
) -> SourceCrop:
    """Close the immutable record only after every retained crop is available."""
    return SourceCrop(
        visual=visual,
        source_png=source_png,
        width_px=width_px,
        height_px=height_px,
        context_crops=tuple(context_crops),
    )


@serialized
def extract_source_crops(
    pdf_bytes: bytes,
    visuals: Sequence[SourceVisual],
    chunks: Sequence[PdfChunk],
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
    *,
    render_scale: float,
    max_page_dimension_px: int,
    max_crop_pixels: int,
    max_png_bytes: int,
) -> list[SourceCrop]:
    """Return one immutable crop per input visual or raise VisualFidelityError."""
    if type(pdf_bytes) is not bytes or not isinstance(budget, VisualRunBudget) or not isinstance(memory, VisualMemoryBudget):
        _crop_failure()
    render_scale, max_page_dimension_px, max_crop_pixels, max_png_bytes = _validated_limits(
        render_scale, max_page_dimension_px, max_crop_pixels, max_png_bytes
    )
    try:
        requested_visuals = tuple(visuals)
    except Exception as exc:
        _crop_failure(exc)
    if any(not isinstance(visual, SourceVisual) for visual in requested_visuals):
        _crop_failure()

    reader: PdfReader | None = None
    document = None
    cache: "OrderedDict[int, tuple[Image.Image, object]]" = OrderedDict()
    reservations: list[BufferReservation] = []
    body_succeeded = False
    try:
        try:
            budget.check_deadline()
            reader = PdfReader(io.BytesIO(pdf_bytes))
            total_pages = len(reader.pages)
            budget.check_deadline()
            if total_pages <= 0:
                _crop_failure()

            import pypdfium2 as pdfium

            budget.check_deadline()
            document = pdfium.PdfDocument(pdf_bytes)
            pdfium_pages = len(document)
            budget.check_deadline()
            if pdfium_pages != total_pages:
                _crop_failure()
        except VisualFidelityError:
            raise
        except Exception as exc:
            _crop_failure(exc)

        chunk_by_index = _chunk_map(chunks, total_pages)

        # Complete preflight: no page raster or retained buffer exists until every
        # visual, context, mapping, box, and original-page frame is known valid.
        plans: list[
            tuple[
                SourceVisual,
                int,
                tuple[int, int, int, int],
                tuple[tuple[SourceContext, int, tuple[int, int, int, int]], ...],
            ]
        ] = []
        checked_geometries: dict[int, PdfPageGeometry] = {}

        def geometry_for(global_page: int, expected: PdfPageGeometry) -> PdfPageGeometry:
            if not (1 <= global_page <= total_pages):
                _crop_failure()
            prior = checked_geometries.get(global_page)
            if prior is not None:
                if prior != expected:
                    _crop_failure()
                return prior
            page = None
            try:
                budget.check_deadline()
                page = document[global_page - 1]
                actual = _original_geometry(
                    reader.pages[global_page - 1], page, global_page, render_scale
                )
                budget.check_deadline()
            except VisualFidelityError:
                raise
            except Exception as exc:
                _crop_failure(exc)
            finally:
                if page is not None:
                    page.close()
            if actual != expected:
                _crop_failure()
            _validate_page_size(actual, max_page_dimension_px)
            checked_geometries[global_page] = actual
            return actual

        ids: set[str] = set()
        for visual in requested_visuals:
            if visual.visual_id in ids:
                _crop_failure()
            ids.add(visual.visual_id)
            chunk = chunk_by_index.get(visual.chunk_index)
            if (
                chunk is None
                or not (1 <= visual.local_page <= chunk.page_count)
                or visual.global_page
                != chunk.first_global_page + visual.local_page - 1
            ):
                _crop_failure()
            visual_geometry = geometry_for(
                visual.global_page, chunk.page_geometries[visual.local_page - 1]
            )
            visual_bounds = _pixel_bounds(
                visual.box,
                visual_geometry.rendered_width,
                visual_geometry.rendered_height,
            )
            _validate_visual_crop_size(
                visual_bounds[2] - visual_bounds[0],
                visual_bounds[3] - visual_bounds[1],
                max_crop_pixels,
            )
            if len(visual.context_regions) > 2:
                _crop_failure()
            context_plans: list[
                tuple[SourceContext, int, tuple[int, int, int, int]]
            ] = []
            for context in visual.context_regions:
                if (
                    context.topic_key != visual.topic_anchor.topic_key
                    or abs(context.local_page - visual.local_page) > 1
                    or len(context.exact_text) > config.MAX_CONTEXT_TEXT_CHARS
                    or not (1 <= context.local_page <= chunk.page_count)
                    or (context.box[2] - context.box[0])
                    * (context.box[3] - context.box[1])
                    / 10_000.0
                    > config.MAX_CONTEXT_AREA_FRACTION
                ):
                    _crop_failure()
                context_global_page = visual.global_page + (
                    context.local_page - visual.local_page
                )
                if not (
                    chunk.first_global_page
                    <= context_global_page
                    <= chunk.last_global_page
                    and 1 <= context_global_page <= total_pages
                ):
                    _crop_failure()
                expected_context_geometry = chunk.page_geometries[
                    context.local_page - 1
                ]
                context_geometry = geometry_for(
                    context_global_page, expected_context_geometry
                )
                context_bounds = _pixel_bounds(
                    context.box,
                    context_geometry.rendered_width,
                    context_geometry.rendered_height,
                )
                _validate_context_crop_size(
                    context_bounds[2] - context_bounds[0],
                    context_bounds[3] - context_bounds[1],
                )
                context_plans.append(
                    (context, context_global_page, context_bounds)
                )
            plans.append(
                (visual, visual.global_page, visual_bounds, tuple(context_plans))
            )

        def decoded_page(global_page: int) -> Image.Image:
            cached = cache.pop(global_page, None)
            if cached is not None:
                cache[global_page] = cached
                return cached[0]

            geometry = checked_geometries[global_page]
            decoded_bytes = (
                geometry.rendered_width * geometry.rendered_height * 3
            )
            while not memory.page_would_fit(decoded_bytes) and cache:
                old_key, old_value = cache.popitem(last=False)
                _release_cached_page(memory, old_key, old_value)
            if not memory.page_would_fit(decoded_bytes):
                raise VisualFidelityError("memory_limit_exceeded")

            page = None
            bitmap = None
            image = None
            operation_error: Exception | None = None
            try:
                budget.check_deadline()
                page = document[global_page - 1]
                bitmap = page.render(
                    scale=page_render_scale(
                        float(page.get_width()),
                        float(page.get_height()),
                        render_scale=render_scale,
                    ),
                    rev_byteorder=True,
                )
                image = bitmap.to_pil()
                image.load()
                budget.check_deadline()
            except Exception as exc:
                operation_error = exc
            page_close_error: Exception | None = None
            if page is not None:
                try:
                    page.close()
                except Exception as exc:
                    page_close_error = exc
            if operation_error is not None or page_close_error is not None:
                try:
                    _close_unretained_decode(image, bitmap)
                except Exception:
                    pass
                if isinstance(operation_error, VisualFidelityError):
                    raise operation_error
                _crop_failure(operation_error or page_close_error)
            if (
                image.size
                != (geometry.rendered_width, geometry.rendered_height)
                or image.mode not in ("RGB", "RGBA")
            ):
                try:
                    _close_unretained_decode(image, bitmap)
                except Exception:
                    pass
                _crop_failure()
            actual_decoded_bytes = image.width * image.height * len(image.getbands())
            if actual_decoded_bytes != decoded_bytes:
                try:
                    _close_unretained_decode(image, bitmap)
                except Exception:
                    pass
                _crop_failure()
            try:
                memory.reserve_page(global_page, decoded_bytes)
            except Exception as exc:
                try:
                    _close_unretained_decode(image, bitmap)
                except Exception:
                    pass
                if isinstance(exc, VisualFidelityError):
                    raise
                _crop_failure(exc)
            cache[global_page] = (image, bitmap)
            return image

        def crop_and_encode(
            global_page: int,
            bounds: tuple[int, int, int, int],
            *,
            context: bool,
        ) -> tuple[bytes, int, int]:
            page_image = decoded_page(global_page)
            cropped = None
            try:
                budget.check_deadline()
                cropped = page_image.crop(bounds)
                budget.check_deadline()
                width, height = cropped.size
                if context:
                    _validate_context_crop_size(width, height)
                    byte_limit = config.MAX_CONTEXT_PNG_BYTES
                else:
                    _validate_visual_crop_size(width, height, max_crop_pixels)
                    byte_limit = max_png_bytes
                return _encode_png(cropped, budget, byte_limit=byte_limit), width, height
            except VisualFidelityError:
                raise
            except Exception as exc:
                _crop_failure(exc)
            finally:
                if cropped is not None:
                    cropped.close()

        results: list[SourceCrop] = []
        for visual, global_page, bounds, context_plans in plans:
            source_png, width, height = crop_and_encode(
                global_page, bounds, context=False
            )
            source_reservation = memory.reserve_buffer("source", source_png)
            if source_reservation is None:
                raise VisualFidelityError("memory_limit_exceeded")
            reservations.append(source_reservation)

            context_crops: list[SourceContextCrop] = []
            context_total_bytes = 0
            for context, context_global_page, context_bounds in context_plans:
                context_png, context_width, context_height = crop_and_encode(
                    context_global_page, context_bounds, context=True
                )
                context_total_bytes += len(context_png)
                if context_total_bytes > config.MAX_CONTEXT_TOTAL_BYTES:
                    _crop_failure()
                context_reservation = memory.reserve_buffer("context", context_png)
                if context_reservation is None:
                    raise VisualFidelityError("memory_limit_exceeded")
                reservations.append(context_reservation)
                context_crops.append(
                    SourceContextCrop(
                        context=context,
                        source_png=context_png,
                        width_px=context_width,
                        height_px=context_height,
                    )
                )

            source_crop = _construct_source_crop(
                visual=visual,
                source_png=source_png,
                width_px=width,
                height_px=height,
                context_crops=context_crops,
            )
            if tuple(item.context for item in source_crop.context_crops) != visual.context_regions:
                _crop_failure()
            results.append(source_crop)

        if tuple(crop.visual.visual_id for crop in results) != tuple(
            visual.visual_id for visual in requested_visuals
        ):
            _crop_failure()
        body_succeeded = True
    except VisualFidelityError:
        raise
    except Exception as exc:
        _crop_failure(exc)
    finally:
        cleanup_error: Exception | None = None
        while cache:
            cache_key, cached = cache.popitem(last=False)
            try:
                _release_cached_page(memory, cache_key, cached)
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if document is not None:
            try:
                document.close()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if not body_succeeded or cleanup_error is not None:
            for reservation in reversed(reservations):
                try:
                    memory.release_buffer(reservation)
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
        if body_succeeded and cleanup_error is not None:
            _crop_failure(cleanup_error)
    return results


# Alternate explicit name used by callers during the pipeline migration.
resolve_manifest_crops = extract_source_crops


def _border_stats(arr: "np.ndarray") -> "tuple[np.ndarray, float]":
    """Median border colour and its luminance (background estimate)."""
    h, w = arr.shape[:2]
    t = max(2, min(h, w) // 50)
    ring = np.concatenate([
        arr[:t].reshape(-1, 3), arr[-t:].reshape(-1, 3),
        arr[:, :t].reshape(-1, 3), arr[:, -t:].reshape(-1, 3),
    ])
    med = np.median(ring, axis=0)
    lum = float(0.299 * med[0] + 0.587 * med[1] + 0.114 * med[2])
    return med, lum


def _whiten(img: "Image.Image") -> "Image.Image":
    img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.float64)
    bg, lum = _border_stats(arr)

    if lum < 120:
        # Dark slide: grayscale -> invert -> stretch -> ink-on-white tint.
        g = ImageOps.autocontrast(ImageOps.invert(ImageOps.grayscale(img)), cutoff=2)
        v = np.asarray(g, dtype=np.float64)[..., None] / 255.0  # 1 = background
        out = v * 255.0 + (1.0 - v) * _INK  # white bg, dark-blue ink
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))

    # Light slide: scale each channel so the background becomes pure white.
    scale = 255.0 / np.maximum(bg, 80.0)
    out = np.clip(arr * scale, 0, 255).astype(np.uint8)
    return ImageOps.autocontrast(Image.fromarray(out), cutoff=1)


@serialized
def extract_snapshots(pdf_bytes: bytes, snapshots) -> "list[tuple[bytes, str]]":
    """Legacy compatibility wrapper; the source-visual pipeline never calls it."""
    if not snapshots:
        return []
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception:  # noqa: BLE001 - never let snapshots break the notes
        print(redact(traceback.format_exc()))
        return []

    results: list[tuple[bytes, str]] = []
    try:
        n_pages = len(doc)
        for snap in snapshots[:4]:
            try:
                idx = int(snap.page) - 1
                if not (0 <= idx < n_pages) or len(snap.box or []) != 4:
                    continue
                x0, y0, x1, y1 = [float(v) for v in snap.box]
                x0, x1 = sorted((max(0.0, min(100.0, x0)),
                                 max(0.0, min(100.0, x1))))
                y0, y1 = sorted((max(0.0, min(100.0, y0)),
                                 max(0.0, min(100.0, y1))))
                if (x1 - x0) * (y1 - y0) / 10000.0 > _MAX_AREA_FRACTION:
                    continue
                # A touch of breathing room around the crop.
                pad_x, pad_y = (x1 - x0) * 0.02 + 0.4, (y1 - y0) * 0.02 + 0.4
                x0, y0 = max(0.0, x0 - pad_x), max(0.0, y0 - pad_y)
                x1, y1 = min(100.0, x1 + pad_x), min(100.0, y1 + pad_y)

                page = doc[idx]
                pil = page.render(
                    scale=page_render_scale(
                        float(page.get_width()),
                        float(page.get_height()),
                        render_scale=_RENDER_SCALE,
                    )
                ).to_pil()
                W, H = pil.size
                crop = pil.crop((
                    int(W * x0 / 100), int(H * y0 / 100),
                    int(W * x1 / 100), int(H * y1 / 100),
                ))
                if crop.width < _MIN_CROP_PX or crop.height < _MIN_CROP_PX:
                    continue
                cleaned = _whiten(crop)
                buf = io.BytesIO()
                cleaned.save(buf, format="PNG")
                results.append((buf.getvalue(), (snap.caption or "").strip()))
            except Exception:  # noqa: BLE001 - skip one bad snapshot, keep rest
                print(redact(traceback.format_exc()))
                continue
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return results
