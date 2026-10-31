"""Deterministic, geometry-preserving source-visual fallback preparation."""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np
from PIL import Image

from . import config
from .errors import VisualFidelityError
from .visual_manifest import (
    BufferReservation,
    SourceCrop,
    VisualMemoryBudget,
    VisualRunBudget,
)


BACKGROUND_DISTANCE_RGB = 18.0
BACKGROUND_MIN_CONFIDENCE = 0.85
DARK_LUMINANCE_MAX = 120.0
LIGHT_LUMINANCE_MIN = 180.0
SATURATED_PIXEL_MIN = 0.25
SATURATED_HUE_MAX_DELTA = 0.04
MEANINGFUL_COMPONENT_MIN_PX = 6
FOREGROUND_REQUIRED_RECALL = 1.0
NOTES_INK_RGB = (30, 40, 70)
NOTES_PAGE_RGB = (253, 251, 245)
LABEL_READABLE_CONTRAST_MIN = 1.5
LABEL_READABLE_FRACTION_MIN = 1.0
FALLBACK_PREFLIGHT_CHUNK_PIXELS = 65_536
FALLBACK_PREFLIGHT_RETAINED_BYTES_PER_PIXEL = 7
FALLBACK_PREFLIGHT_CHUNK_BYTES_PER_PIXEL = 128


@dataclass(frozen=True)
class BackgroundProfile:
    rgb: tuple[int, int, int]
    luminance: float
    confidence: float
    mode: Literal["light", "dark"]


@dataclass(frozen=True)
class LabelLayer:
    label_index: int
    text: str
    png: bytes
    box: tuple[float, float, float, float]
    anchor: tuple[float, float] | None
    source_mask_sha256: str
    reservation: BufferReservation


@dataclass(frozen=True)
class PreparedFallback:
    visual_id: str
    png: bytes
    label_layers: tuple[LabelLayer, ...]
    label_union_png: bytes
    temporary_label_reservations: tuple[BufferReservation, ...]
    foreground_recall: float
    background: BackgroundProfile


def _failure() -> None:
    raise VisualFidelityError("fallback_damaged")


def _decode_rgba(png: bytes, width: int, height: int) -> np.ndarray:
    if (
        type(png) is not bytes
        or type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
    ):
        _failure()
    if width * height > config.MAX_VISUAL_CROP_PIXELS:
        _failure()
    try:
        with Image.open(io.BytesIO(png)) as source:
            source.load()
            if source.size != (width, height):
                _failure()
            return np.array(source.convert("RGBA"), dtype=np.uint8, copy=True)
    except VisualFidelityError:
        raise
    except Exception:
        _failure()


def _luminance(rgb: np.ndarray) -> np.ndarray:
    values = rgb.astype(np.float64)
    return values[..., 0] * 0.2126 + values[..., 1] * 0.7152 + values[..., 2] * 0.0722


def _saturation(rgb: np.ndarray) -> np.ndarray:
    values = rgb.astype(np.float64)
    maximum = values.max(axis=-1)
    minimum = values.min(axis=-1)
    return np.divide(
        maximum - minimum,
        maximum,
        out=np.zeros_like(maximum),
        where=maximum > 0.0,
    )


def _hue(rgb: np.ndarray) -> np.ndarray:
    values = rgb.astype(np.float64) / 255.0
    maximum = values.max(axis=-1)
    minimum = values.min(axis=-1)
    delta = maximum - minimum
    hue = np.zeros_like(maximum)
    nonzero = delta > 0.0
    red = nonzero & (maximum == values[..., 0])
    green = nonzero & ~red & (maximum == values[..., 1])
    blue = nonzero & ~red & ~green
    hue[red] = ((values[..., 1][red] - values[..., 2][red]) / delta[red]) % 6.0
    hue[green] = (values[..., 2][green] - values[..., 0][green]) / delta[green] + 2.0
    hue[blue] = (values[..., 0][blue] - values[..., 1][blue]) / delta[blue] + 4.0
    return (hue / 6.0) % 1.0


def _validate_saturated_colour_metrics(
    source_hue: np.ndarray,
    output_hue: np.ndarray,
    source_saturation: np.ndarray,
    output_saturation: np.ndarray,
) -> None:
    if not (
        source_hue.shape
        == output_hue.shape
        == source_saturation.shape
        == output_saturation.shape
    ) or not all(
        np.all(np.isfinite(values))
        for values in (
            source_hue,
            output_hue,
            source_saturation,
            output_saturation,
        )
    ):
        _failure()
    if (
        np.any((source_hue < 0.0) | (source_hue >= 1.0))
        or np.any((output_hue < 0.0) | (output_hue >= 1.0))
        or np.any((source_saturation < 0.0) | (source_saturation > 1.0))
        or np.any((output_saturation < 0.0) | (output_saturation > 1.0))
    ):
        _failure()
    forward_delta = np.mod(output_hue - source_hue, 1.0)
    backward_delta = np.mod(source_hue - output_hue, 1.0)
    forward_limit = np.mod(
        np.mod(source_hue + SATURATED_HUE_MAX_DELTA, 1.0) - source_hue,
        1.0,
    )
    backward_limit = np.mod(
        source_hue - np.mod(source_hue - SATURATED_HUE_MAX_DELTA, 1.0),
        1.0,
    )
    hue_outside_limit = np.where(
        forward_delta <= backward_delta,
        forward_delta > forward_limit,
        backward_delta > backward_limit,
    )
    if (
        np.any(hue_outside_limit)
        or np.any(output_saturation < source_saturation * 0.8)
    ):
        _failure()


def _colour_distance(rgb: np.ndarray, background_rgb: tuple[int, int, int]) -> np.ndarray:
    delta = rgb.astype(np.float64) - np.asarray(background_rgb, dtype=np.float64)
    return np.sqrt(np.sum(delta * delta, axis=-1))


def _estimate_background_from_border(border: np.ndarray) -> BackgroundProfile:
    if (
        not isinstance(border, np.ndarray)
        or border.ndim != 2
        or border.shape[0] < 4
        or border.shape[1] != 4
    ):
        _failure()
    median = np.median(border[:, :3].astype(np.float64), axis=0)
    distances = np.sqrt(np.sum((border[:, :3].astype(np.float64) - median) ** 2, axis=1))
    confidence = float(np.count_nonzero(distances <= BACKGROUND_DISTANCE_RGB) / len(border))
    rgb = tuple(int(round(float(value))) for value in median)
    luminance = float(_luminance(np.asarray(rgb, dtype=np.uint8)))
    if confidence < BACKGROUND_MIN_CONFIDENCE:
        _failure()
    if luminance >= LIGHT_LUMINANCE_MIN:
        mode: Literal["light", "dark"] = "light"
    elif luminance <= DARK_LUMINANCE_MAX:
        mode = "dark"
    else:
        _failure()
    return BackgroundProfile(rgb=rgb, luminance=luminance, confidence=confidence, mode=mode)


def _estimate_background(rgba: np.ndarray) -> BackgroundProfile:
    height, width = rgba.shape[:2]
    if width < 2 or height < 2:
        _failure()
    border = np.concatenate(
        (
            rgba[0, :, :],
            rgba[-1, :, :],
            rgba[1:-1, 0, :],
            rgba[1:-1, -1, :],
        ),
        axis=0,
    )
    return _estimate_background_from_border(border)


def validate_source_fallback_pixels(
    image: Image.Image,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> BackgroundProfile:
    """Validate fallback background/foreground separation with bounded rows."""
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _failure()
    width, height = image.size
    if width < 2 or height < 2:
        _failure()
    check = check_deadline or (lambda: None)

    strips: list[np.ndarray] = []
    try:
        for box in (
            (0, 0, width, 1),
            (0, height - 1, width, height),
            (0, 1, 1, height - 1),
            (width - 1, 1, width, height - 1),
        ):
            check()
            strip = image.crop(box)
            try:
                strips.append(
                    np.array(strip, dtype=np.uint8, copy=True).reshape(-1, 4)
                )
            finally:
                strip.close()
        profile = _estimate_background_from_border(np.concatenate(strips, axis=0))
    except VisualFidelityError:
        raise
    except Exception:
        _failure()
    finally:
        strips.clear()

    background_saturation = float(
        _saturation(np.asarray(profile.rgb, dtype=np.uint8))
    )
    foreground_total = 0
    retained_total = 0
    rows_per_chunk = max(1, FALLBACK_PREFLIGHT_CHUNK_PIXELS // width)
    for y0 in range(0, height, rows_per_chunk):
        check()
        y1 = min(height, y0 + rows_per_chunk)
        region = image.crop((0, y0, width, y1))
        try:
            rgba = np.array(region, dtype=np.uint8, copy=True)
        finally:
            region.close()
        rgb = rgba[..., :3]
        distance = _colour_distance(rgb, profile.rgb)
        luminance_delta = np.abs(_luminance(rgb) - profile.luminance)
        saturation_delta = np.abs(_saturation(rgb) - background_saturation)
        visible = rgba[..., 3] > 0
        source_mask = visible & (
            (distance > BACKGROUND_DISTANCE_RGB)
            | (luminance_delta > BACKGROUND_DISTANCE_RGB)
            | (saturation_delta > 0.12)
        )
        transformed_mask = visible & (distance > BACKGROUND_DISTANCE_RGB)
        foreground_total += int(np.count_nonzero(source_mask))
        retained_total += int(np.count_nonzero(source_mask & transformed_mask))
        if np.any(source_mask != transformed_mask):
            _failure()
        check()
    if foreground_total == 0 or retained_total != foreground_total:
        _failure()
    return profile


def _meaningful_foreground_mask(
    rgba: np.ndarray, profile: BackgroundProfile
) -> np.ndarray:
    rgb = rgba[..., :3]
    distance = _colour_distance(rgb, profile.rgb)
    luminance_delta = np.abs(_luminance(rgb) - profile.luminance)
    background_saturation = float(_saturation(np.asarray(profile.rgb, dtype=np.uint8)))
    saturation_delta = np.abs(_saturation(rgb) - background_saturation)
    visible = rgba[..., 3] > 0
    return visible & (
        (distance > BACKGROUND_DISTANCE_RGB)
        | (luminance_delta > BACKGROUND_DISTANCE_RGB)
        | (saturation_delta > 0.12)
    )


def _build_transparent_rgba(
    source: np.ndarray, profile: BackgroundProfile
) -> tuple[np.ndarray, np.ndarray]:
    output = source.copy()
    rgb = source[..., :3]
    distance = _colour_distance(rgb, profile.rgb)
    source_mask = _meaningful_foreground_mask(source, profile)
    output[..., 3] = np.where(
        distance > BACKGROUND_DISTANCE_RGB,
        source[..., 3],
        0,
    ).astype(np.uint8)

    if profile.mode == "dark":
        saturation = _saturation(rgb)
        light_ink = (
            source_mask
            & (saturation < SATURATED_PIXEL_MIN)
            & (_luminance(rgb) >= LIGHT_LUMINANCE_MIN)
        )
        output[light_ink, :3] = NOTES_INK_RGB

        saturated = source_mask & (saturation >= SATURATED_PIXEL_MIN)
        if np.any(saturated):
            # Saturated instructional pixels remain byte-identical, which is
            # stricter than the permitted hue and 80%-saturation tolerances.
            output[saturated, :3] = source[saturated, :3]
            output_saturation = _saturation(output[..., :3])
            _validate_saturated_colour_metrics(
                _hue(source[..., :3])[saturated],
                _hue(output[..., :3])[saturated],
                saturation[saturated],
                output_saturation[saturated],
            )
    return output, source_mask


def _validate_foreground_preservation(
    source_mask: np.ndarray, transformed: np.ndarray
) -> float:
    total = int(np.count_nonzero(source_mask))
    if total == 0:
        _failure()
    transformed_mask = transformed[..., 3] > 0
    retained = int(np.count_nonzero(source_mask & transformed_mask))
    recall = retained / total
    if recall != FOREGROUND_REQUIRED_RECALL:
        _failure()
    # Exact mask equality is a stronger topology proof than walking only the
    # components at the meaningful-size boundary: every component of at least
    # the required size (and every smaller one) has identical membership.
    if total >= MEANINGFUL_COMPONENT_MIN_PX and not np.array_equal(
        source_mask, transformed_mask
    ):
        _failure()
    return float(recall)


def _pixel_bounds(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    x0 = math.floor(width * box[0] / 100.0)
    y0 = math.floor(height * box[1] / 100.0)
    x1 = math.ceil(width * box[2] / 100.0)
    y1 = math.ceil(height * box[3] / 100.0)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        _failure()
    return x0, y0, x1, y1


def _encode_png(rgba: np.ndarray) -> bytes:
    output = io.BytesIO()
    try:
        image = Image.fromarray(rgba, mode="RGBA")
        try:
            image.save(output, format="PNG")
        finally:
            image.close()
        return output.getvalue()
    except VisualFidelityError:
        raise
    except Exception:
        _failure()


def _check_visual_png(data: bytes, width: int, height: int) -> None:
    if (
        type(data) is not bytes
        or len(data) > config.MAX_VISUAL_PNG_BYTES
        or width * height > config.MAX_VISUAL_CROP_PIXELS
    ):
        _failure()
    _decode_rgba(data, width, height)


def _reserve_owned(memory: VisualMemoryBudget, data: bytes) -> BufferReservation:
    reservation = memory.reserve_buffer("asset", data)
    if reservation is None:
        raise VisualFidelityError("memory_limit_exceeded")
    return reservation


def _release_reverse(
    memory: VisualMemoryBudget, reservations: list[BufferReservation]
) -> None:
    first_incomplete_error: BaseException | None = None
    for reservation in reversed(reservations):
        release_error: BaseException | None = None
        try:
            memory.release_buffer(reservation)
        except BaseException as error:  # cleanup must continue for exact owners
            release_error = error
        try:
            still_active = memory._reservation_is_active(reservation)
        except BaseException as probe_error:
            still_active = True
            if first_incomplete_error is None:
                first_incomplete_error = release_error or probe_error
        if still_active and first_incomplete_error is None:
            first_incomplete_error = release_error or VisualFidelityError(
                "memory_limit_exceeded"
            )
    if first_incomplete_error is not None:
        raise VisualFidelityError("memory_limit_exceeded") from first_incomplete_error


def _mask_sha256(mask: np.ndarray) -> str:
    return hashlib.sha256(mask.astype(np.uint8, copy=False).tobytes()).hexdigest()


def prepare_source_fallback(
    crop: SourceCrop,
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
) -> PreparedFallback:
    """Create an exact-geometry transparent derivative or fail closed."""
    owned: list[BufferReservation] = []
    try:
        source = _decode_rgba(crop.source_png, crop.width_px, crop.height_px)

        budget.check_deadline()
        profile = _estimate_background(source)
        budget.check_deadline()

        budget.check_deadline()
        transformed, source_mask = _build_transparent_rgba(source, profile)
        budget.check_deadline()

        budget.check_deadline()
        fallback_png = _encode_png(transformed)
        _check_visual_png(fallback_png, crop.width_px, crop.height_px)
        budget.check_deadline()
        fallback_owner = _reserve_owned(memory, fallback_png)
        owned.append(fallback_owner)

        label_layers: list[LabelLayer] = []
        union = np.zeros_like(transformed)
        copied = np.zeros((crop.height_px, crop.width_px), dtype=bool)
        temporary: list[BufferReservation] = []
        for label_index, label in enumerate(crop.visual.exact_labels):
            budget.check_deadline()
            x0, y0, x1, y1 = _pixel_bounds(label.box, crop.width_px, crop.height_px)
            source_box = source[y0:y1, x0:x1]
            transformed_box = transformed[y0:y1, x0:x1].copy()
            source_label_mask = _meaningful_foreground_mask(source_box, profile)
            transformed_label_mask = transformed_box[..., 3] > 0
            if (
                not np.any(source_label_mask)
                or not np.array_equal(
                    source_label_mask,
                    source_label_mask & transformed_label_mask,
                )
            ):
                _failure()
            layer_png = _encode_png(transformed_box)
            layer_width, layer_height = x1 - x0, y1 - y0
            if (
                layer_width * layer_height > config.MAX_LABEL_LAYER_PIXELS
                or len(layer_png) > config.MAX_LABEL_LAYER_PNG_BYTES
            ):
                _failure()
            decoded_layer = _decode_rgba(layer_png, layer_width, layer_height)
            if not np.array_equal(
                source_label_mask,
                _meaningful_foreground_mask(decoded_layer, profile),
            ):
                _failure()
            layer_owner = _reserve_owned(memory, layer_png)
            owned.append(layer_owner)
            temporary.append(layer_owner)
            label_layers.append(
                LabelLayer(
                    label_index=label_index,
                    text=label.text,
                    png=layer_png,
                    box=label.box,
                    anchor=label.anchor,
                    source_mask_sha256=_mask_sha256(source_label_mask),
                    reservation=layer_owner,
                )
            )

            copied_box = copied[y0:y1, x0:x1]
            new_coverage = ~copied_box
            union_box = union[y0:y1, x0:x1]
            transformed_region = transformed[y0:y1, x0:x1]
            union_box[new_coverage] = transformed_region[new_coverage]
            copied_box[:] = True
            budget.check_deadline()

        budget.check_deadline()
        label_union_png = _encode_png(union)
        _check_visual_png(label_union_png, crop.width_px, crop.height_px)
        budget.check_deadline()
        union_owner = _reserve_owned(memory, label_union_png)
        owned.append(union_owner)
        temporary.append(union_owner)

        prepared = PreparedFallback(
            visual_id=crop.visual.visual_id,
            png=fallback_png,
            label_layers=tuple(label_layers),
            label_union_png=label_union_png,
            temporary_label_reservations=tuple(temporary),
            foreground_recall=FOREGROUND_REQUIRED_RECALL,
            background=profile,
        )

        budget.check_deadline()
        foreground_recall = _validate_foreground_preservation(source_mask, transformed)
        if len(label_layers) != len(crop.visual.exact_labels):
            _failure()
        validate_composited_labels(fallback_png, crop, prepared)
        budget.check_deadline()

        return PreparedFallback(
            visual_id=prepared.visual_id,
            png=prepared.png,
            label_layers=prepared.label_layers,
            label_union_png=prepared.label_union_png,
            temporary_label_reservations=prepared.temporary_label_reservations,
            foreground_recall=foreground_recall,
            background=prepared.background,
        )
    except BaseException as error:
        cleanup_error: BaseException | None = None
        try:
            _release_reverse(memory, owned)
        except BaseException as caught_cleanup_error:
            cleanup_error = caught_cleanup_error
        if cleanup_error is not None:
            if isinstance(cleanup_error, VisualFidelityError):
                raise cleanup_error from error
            raise VisualFidelityError("memory_limit_exceeded") from error
        if isinstance(error, VisualFidelityError) or not isinstance(error, Exception):
            raise
        raise VisualFidelityError("fallback_damaged") from None


def _validated_union(crop: SourceCrop, fallback: PreparedFallback) -> np.ndarray:
    if fallback.visual_id != crop.visual.visual_id:
        _failure()
    _check_visual_png(fallback.label_union_png, crop.width_px, crop.height_px)
    return _decode_rgba(fallback.label_union_png, crop.width_px, crop.height_px)


def composite_source_labels(
    candidate_png: bytes,
    crop: SourceCrop,
    fallback: PreparedFallback,
) -> bytes:
    """Composite the prepared full-canvas label union exactly once."""
    _check_visual_png(candidate_png, crop.width_px, crop.height_px)
    candidate = _decode_rgba(candidate_png, crop.width_px, crop.height_px)
    union = _validated_union(crop, fallback)
    try:
        candidate_image = Image.fromarray(candidate, mode="RGBA")
        union_image = Image.fromarray(union, mode="RGBA")
        try:
            composite = Image.alpha_composite(candidate_image, union_image)
            try:
                result = _encode_png(np.array(composite, dtype=np.uint8, copy=True))
            finally:
                composite.close()
        finally:
            candidate_image.close()
            union_image.close()
    except VisualFidelityError:
        raise
    except Exception:
        _failure()
    _check_visual_png(result, crop.width_px, crop.height_px)
    validate_composited_labels(result, crop, fallback)
    return result


def validate_composited_labels(
    composite_png: bytes,
    crop: SourceCrop,
    fallback: PreparedFallback,
) -> None:
    """Validate every ordered label-record identity at its exact target box."""
    _check_visual_png(composite_png, crop.width_px, crop.height_px)
    composite = _decode_rgba(composite_png, crop.width_px, crop.height_px)
    source = _decode_rgba(crop.source_png, crop.width_px, crop.height_px)
    if _estimate_background(source) != fallback.background:
        _failure()
    union = _validated_union(crop, fallback)
    labels = crop.visual.exact_labels
    temporary = fallback.temporary_label_reservations
    if (
        len(fallback.label_layers) != len(labels)
        or len(temporary) != len(labels) + 1
        or any(
            temporary[index] is not layer.reservation
            for index, layer in enumerate(fallback.label_layers)
        )
        or temporary[-1].kind != "asset"
        or temporary[-1].data is not fallback.label_union_png
        or temporary[-1].object_id != id(fallback.label_union_png)
        or temporary[-1].size != len(fallback.label_union_png)
    ):
        _failure()

    transformed_source, _ = _build_transparent_rgba(source, fallback.background)
    expected_union = np.zeros_like(transformed_source)
    covered = np.zeros((crop.height_px, crop.width_px), dtype=bool)
    for label in labels:
        x0, y0, x1, y1 = _pixel_bounds(label.box, crop.width_px, crop.height_px)
        covered_box = covered[y0:y1, x0:x1]
        new_coverage = ~covered_box
        expected_box = expected_union[y0:y1, x0:x1]
        transformed_box = transformed_source[y0:y1, x0:x1]
        expected_box[new_coverage] = transformed_box[new_coverage]
        covered_box[:] = True
    if not np.array_equal(union, expected_union):
        _failure()

    for index, (label, layer) in enumerate(zip(labels, fallback.label_layers)):
        if (
            layer.label_index != index
            or layer.text != label.text
            or layer.box != label.box
            or layer.anchor != label.anchor
        ):
            _failure()
        x0, y0, x1, y1 = _pixel_bounds(label.box, crop.width_px, crop.height_px)
        source_mask = _meaningful_foreground_mask(source[y0:y1, x0:x1], fallback.background)
        if not np.any(source_mask) or _mask_sha256(source_mask) != layer.source_mask_sha256:
            _failure()
        if (
            len(layer.png) > config.MAX_LABEL_LAYER_PNG_BYTES
            or (x1 - x0) * (y1 - y0) > config.MAX_LABEL_LAYER_PIXELS
            or layer.reservation.data is not layer.png
            or layer.reservation.kind != "asset"
            or layer.reservation.object_id != id(layer.png)
            or layer.reservation.size != len(layer.png)
        ):
            _failure()
        layer_rgba = _decode_rgba(layer.png, x1 - x0, y1 - y0)
        expected_layer, expected_mask = _build_transparent_rgba(
            source[y0:y1, x0:x1], fallback.background
        )
        if (
            not np.array_equal(source_mask, expected_mask)
            or not np.array_equal(layer_rgba, expected_layer)
        ):
            _failure()

        composite_box = composite[y0:y1, x0:x1]
        union_box = union[y0:y1, x0:x1]
        if not np.array_equal(union_box, expected_layer):
            _failure()
        if not np.array_equal(
            composite_box[source_mask], expected_layer[source_mask]
        ):
            # Exact equality defines the partial-alpha contract: verified
            # redraws must leave source-glyph pixels transparent before the
            # one union composite, so candidate pixels cannot mask a stroke.
            _failure()
        glyph_pixels = composite_box[source_mask].astype(np.float64)
        alpha = glyph_pixels[:, 3:4] / 255.0
        notes_page = np.asarray(NOTES_PAGE_RGB, dtype=np.float64)
        displayed_rgb = glyph_pixels[:, :3] * alpha + notes_page * (1.0 - alpha)
        displayed_luminance = _luminance(displayed_rgb)
        page_luminance = float(_luminance(notes_page))
        contrast_with_notes_page = (
            np.maximum(page_luminance, displayed_luminance) + 5.0
        ) / (np.minimum(page_luminance, displayed_luminance) + 5.0)
        readable_fraction = float(
            np.count_nonzero(
                contrast_with_notes_page >= LABEL_READABLE_CONTRAST_MIN
            )
            / len(contrast_with_notes_page)
        )
        if readable_fraction < LABEL_READABLE_FRACTION_MIN:
            _failure()
