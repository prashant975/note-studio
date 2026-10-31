"""Immutable source-visual contracts and bounded visual-run accounting."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import VisualFidelityError


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


VisualKind = Literal["diagram", "structure", "graph"]
AssetState = Literal["verified_redraw", "sketch_source_fallback"]
ContextRole = Literal["heading", "explanation", "graph_description", "legend"]

VISUAL_PIPELINE_DEADLINE_SECONDS = 780.0
MATHPIX_MAX_POLL_ATTEMPTS = 12
MAX_COVERAGE_PROBES = 131_072

PercentageCoordinate = Annotated[float, Field(ge=0.0, le=100.0)]
# Variadic tuples with exact length bounds keep the runtime type immutable
# while emitting minItems/maxItems instead of prefixItems, which the
# google-genai schema transformer rejects.
PagePercentageBox = Annotated[
    tuple[PercentageCoordinate, ...],
    Field(
        min_length=4,
        max_length=4,
        description=(
            "Original-page percentage box [x0, y0, x1, y1]; "
            "require 0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100. "
            "Never use pixels or [x, y, width, height]."
        )
    ),
]
PagePercentagePoint = Annotated[
    tuple[PercentageCoordinate, ...],
    Field(
        min_length=2,
        max_length=2,
        description=(
            "Original-page percentage point [x, y]; each coordinate must be "
            "between 0 and 100. Never use pixels."
        )
    ),
]


def _validate_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if not all(math.isfinite(value) for value in box):
        raise ValueError("coordinates must be finite")
    x0, y0, x1, y1 = box
    if not (0.0 <= x0 < x1 <= 100.0 and 0.0 <= y0 < y1 <= 100.0):
        raise ValueError("box must be an ordered percentage rectangle")
    return box


def _validate_anchor(anchor: tuple[float, float] | None) -> tuple[float, float] | None:
    if anchor is None:
        return None
    if not all(math.isfinite(value) and 0.0 <= value <= 100.0 for value in anchor):
        raise ValueError("anchor must contain finite percentage coordinates")
    return anchor


def _non_empty(value: str) -> str:
    if value == "":
        raise ValueError("value must not be empty")
    return value


class SourceLabel(FrozenModel):
    text: str
    box: tuple[float, float, float, float]
    anchor: tuple[float, float] | None = None

    _text_not_empty = field_validator("text")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)
    _anchor_is_valid = field_validator("anchor")(_validate_anchor)


class InventoryLabel(FrozenModel):
    text: str
    page_box: PagePercentageBox
    page_anchor: PagePercentagePoint | None = None

    _text_not_empty = field_validator("text")(_non_empty)
    _box_is_valid = field_validator("page_box")(_validate_box)
    _anchor_is_valid = field_validator("page_anchor")(_validate_anchor)


class InventoryTopicAnchor(FrozenModel):
    anchor_ref: str
    local_page: int = Field(ge=1)
    box: PagePercentageBox
    exact_heading: str = ""

    _anchor_ref_not_empty = field_validator("anchor_ref")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)


class InventoryContext(FrozenModel):
    anchor_ref: str
    local_page: int = Field(ge=1)
    box: PagePercentageBox
    role: ContextRole
    exact_text: str = ""

    _anchor_ref_not_empty = field_validator("anchor_ref")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)


class SourceTopicAnchor(FrozenModel):
    topic_key: str
    local_page: int = Field(ge=1)
    box: tuple[float, float, float, float]
    exact_heading: str = ""

    _topic_key_not_empty = field_validator("topic_key")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)


class SourceContext(FrozenModel):
    topic_key: str
    local_page: int = Field(ge=1)
    box: tuple[float, float, float, float]
    role: ContextRole
    exact_text: str = ""

    _topic_key_not_empty = field_validator("topic_key")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)


class VisualRegion(FrozenModel):
    local_page: int = Field(ge=1)
    box: PagePercentageBox
    kind: VisualKind
    topic_anchor: InventoryTopicAnchor
    exact_labels: tuple[InventoryLabel, ...] = Field(default_factory=tuple)
    context_regions: tuple[InventoryContext, ...] = Field(default_factory=tuple, max_length=2)
    section_hint: str = ""
    source_order_hint: int = Field(ge=0)

    _box_is_valid = field_validator("box")(_validate_box)

    @model_validator(mode="after")
    def _validate_contexts(self) -> "VisualRegion":
        for context in self.context_regions:
            if context.anchor_ref != self.topic_anchor.anchor_ref:
                raise ValueError("context anchor_ref must match its region anchor")
            if abs(context.local_page - self.local_page) > 1:
                raise ValueError("context must be on the same or an adjacent page")
        return self


class InventoryResponse(FrozenModel):
    pages_checked: tuple[int, ...]
    regions: tuple[VisualRegion, ...]

    @field_validator("pages_checked")
    @classmethod
    def _pages_are_positive(cls, pages: tuple[int, ...]) -> tuple[int, ...]:
        if any(page <= 0 for page in pages):
            raise ValueError("pages_checked must contain positive pages")
        return pages


class AuditedRegion(VisualRegion):
    source_region_ids: tuple[str, ...] = Field(default_factory=tuple)


class CoverageProbe(FrozenModel):
    probe_id: str
    kind: Literal["component", "grid"]
    local_page: int = Field(ge=1)
    box: tuple[float, float, float, float]
    foreground_fraction: float

    _probe_id_not_empty = field_validator("probe_id")(_non_empty)
    _box_is_valid = field_validator("box")(_validate_box)

    @field_validator("foreground_fraction")
    @classmethod
    def _fraction_is_valid(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("foreground_fraction must be finite and in range")
        return value


class ProbeDisposition(FrozenModel):
    probe_id: str = Field(
        description="Exact probe_id from the runtime audit payload."
    )
    outcome: Literal[
        "covered_visual", "ordinary_text", "table", "decoration", "unresolved"
    ] = Field(
        description=(
            "Use covered_visual only when every listed final region is on the probe's "
            "same local_page and has positive-area overlap with the probe box."
        )
    )
    final_region_indices: tuple[int, ...] = Field(
        default_factory=tuple,
        description=(
            "Zero-based indices into final_regions. For covered_visual, every index must "
            "identify a region on the probe's same local_page with positive-area overlap; "
            "for all other outcomes this list must be empty."
        ),
    )

    _probe_id_not_empty = field_validator("probe_id")(_non_empty)

    @field_validator("final_region_indices")
    @classmethod
    def _indices_are_nonnegative(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 0 for value in values):
            raise ValueError("final region indices must be nonnegative")
        return values


class InventoryAuditResponse(FrozenModel):
    pages_checked: tuple[int, ...]
    coverage_complete: bool
    unresolved_count: int = Field(ge=0)
    final_regions: tuple[AuditedRegion, ...]
    probe_dispositions: tuple[ProbeDisposition, ...]

    @field_validator("pages_checked")
    @classmethod
    def _pages_are_positive(cls, pages: tuple[int, ...]) -> tuple[int, ...]:
        if any(page <= 0 for page in pages):
            raise ValueError("pages_checked must contain positive pages")
        return pages


class SourceVisual(FrozenModel):
    visual_id: str = Field(pattern=r"^sv-\d{4}$")
    chunk_index: int = Field(ge=0)
    local_page: int = Field(ge=1)
    global_page: int = Field(ge=1)
    box: tuple[float, float, float, float]
    kind: VisualKind
    topic_anchor: SourceTopicAnchor
    exact_labels: tuple[SourceLabel, ...] = Field(default_factory=tuple)
    context_regions: tuple[SourceContext, ...] = Field(default_factory=tuple, max_length=2)
    section_hint: str = ""
    source_order: int = Field(ge=0)

    _box_is_valid = field_validator("box")(_validate_box)

    @model_validator(mode="after")
    def _validate_contexts(self) -> "SourceVisual":
        for context in self.context_regions:
            if context.topic_key != self.topic_anchor.topic_key:
                raise ValueError("context topic_key must match its visual topic")
            if abs(context.local_page - self.local_page) > 1:
                raise ValueError("context must be on the same or an adjacent page")
        return self


class VisualManifest(FrozenModel):
    visuals: tuple[SourceVisual, ...] = Field(default_factory=tuple)


class RedrawVerdict(FrozenModel):
    faithful: bool
    missing_elements: tuple[str, ...] = Field(default_factory=tuple)
    extra_elements: tuple[str, ...] = Field(default_factory=tuple)
    unapproved_text: tuple[str, ...] = Field(default_factory=tuple)
    candidate_text_free: bool
    label_regions_clear: bool
    anchors_aligned: bool
    geometry_aligned: bool


@dataclass(frozen=True)
class SourceContextCrop:
    context: SourceContext
    source_png: bytes
    width_px: int
    height_px: int


@dataclass(frozen=True)
class SourceCrop:
    visual: SourceVisual
    source_png: bytes
    width_px: int
    height_px: int
    context_crops: tuple[SourceContextCrop, ...] = ()


@dataclass(frozen=True)
class VisualAsset:
    visual_id: str
    state: AssetState
    primary_png: bytes
    fallback_png: bytes
    width_px: int
    height_px: int


@dataclass(frozen=True)
class RenderedVisual:
    visual_id: str
    state: AssetState
    page_number: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number <= 0
        ):
            raise ValueError("page_number must be a positive integer")
        for name in ("x_mm", "y_mm", "width_mm", "height_mm"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite")
        if self.x_mm < 0.0 or self.y_mm < 0.0:
            raise ValueError("render position must be nonnegative")
        if self.width_mm <= 0.0 or self.height_mm <= 0.0:
            raise ValueError("render dimensions must be positive")


@dataclass
class VisualRunBudget:
    deadline_at: float
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    mathpix_submit_calls: dict[int, int] = field(default_factory=dict)
    mathpix_poll_calls: dict[int, int] = field(default_factory=dict)
    mathpix_lines_calls: dict[int, int] = field(default_factory=dict)
    mathpix_mmd_calls: dict[int, int] = field(default_factory=dict)
    mathpix_delete_calls: dict[int, int] = field(default_factory=dict)
    mathpix_evidence_used: dict[int, int] = field(default_factory=dict)
    mathpix_source_fallback: dict[int, int] = field(default_factory=dict)
    inventory_calls: dict[int, int] = field(default_factory=dict)
    audit_calls: dict[int, int] = field(default_factory=dict)
    note_calls: dict[int, int] = field(default_factory=dict)
    redraw_calls: dict[str, int] = field(default_factory=dict)
    verifier_calls: dict[str, int] = field(default_factory=dict)
    coverage_probes_total: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._bounded_remaining_seconds() is None:
            raise VisualFidelityError("deadline_exceeded")

    def remaining_seconds(self) -> float:
        remaining = self._bounded_remaining_seconds()
        return 0.0 if remaining is None else remaining

    def redacted_counts(self) -> tuple[tuple[str, int], ...]:
        with self._lock:
            return (
                ("mathpix_submit", sum(self.mathpix_submit_calls.values())),
                ("mathpix_poll", sum(self.mathpix_poll_calls.values())),
                ("mathpix_lines", sum(self.mathpix_lines_calls.values())),
                ("mathpix_mmd", sum(self.mathpix_mmd_calls.values())),
                ("mathpix_delete", sum(self.mathpix_delete_calls.values())),
                ("mathpix_evidence_used_chunks", sum(self.mathpix_evidence_used.values())),
                ("mathpix_source_fallback_chunks", sum(self.mathpix_source_fallback.values())),
                ("inventory", sum(self.inventory_calls.values())),
                ("audit", sum(self.audit_calls.values())),
                ("note", sum(self.note_calls.values())),
                ("redraw", sum(self.redraw_calls.values())),
                ("verifier", sum(self.verifier_calls.values())),
                ("coverage_probes", self.coverage_probes_total),
            )

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0.0:
            raise VisualFidelityError("deadline_exceeded")

    def take_inventory(self, chunk_index: int) -> None:
        self._take_once(self.inventory_calls, chunk_index)

    def take_inventory_with_mathpix_outcome(
        self, chunk_index: int, *, used: bool
    ) -> None:
        """Atomically reserve one inventory dispatch and its evidence outcome."""
        with self._lock:
            self._check_deadline_locked()
            if (
                type(used) is not bool
                or self.inventory_calls.get(chunk_index, 0) != 0
                or chunk_index in self.mathpix_evidence_used
                or chunk_index in self.mathpix_source_fallback
            ):
                raise VisualFidelityError("work_limit_exceeded")
            self.inventory_calls[chunk_index] = 1
            target = self.mathpix_evidence_used if used else self.mathpix_source_fallback
            target[chunk_index] = 1

    def take_audit(self, chunk_index: int) -> None:
        self._take_once(self.audit_calls, chunk_index)

    def take_note(self, chunk_index: int) -> None:
        self._take_once(self.note_calls, chunk_index)

    def take_redraw(self, visual_id: str) -> None:
        self._take_at_most(self.redraw_calls, visual_id, 2)

    def take_verifier(self, visual_id: str) -> None:
        self._take_at_most(self.verifier_calls, visual_id, 2)

    def take_mathpix_submit(self, chunk_index: int) -> None:
        self._take_once(self.mathpix_submit_calls, chunk_index)

    def take_mathpix_poll(self, chunk_index: int, limit: int) -> None:
        self._take_at_most(
            self.mathpix_poll_calls,
            chunk_index,
            limit,
            hard_cap=MATHPIX_MAX_POLL_ATTEMPTS,
        )

    def take_mathpix_lines(self, chunk_index: int) -> None:
        self._take_once(self.mathpix_lines_calls, chunk_index)

    def take_mathpix_mmd(self, chunk_index: int) -> None:
        self._take_once(self.mathpix_mmd_calls, chunk_index)

    def take_mathpix_delete(self, chunk_index: int) -> None:
        with self._lock:
            next_count = self.mathpix_delete_calls.get(chunk_index, 0) + 1
            if next_count > 1:
                raise VisualFidelityError("work_limit_exceeded")
            self.mathpix_delete_calls[chunk_index] = next_count

    def record_mathpix_outcome(self, chunk_index: int, *, used: bool) -> None:
        with self._lock:
            self._check_deadline_locked()
            if chunk_index in self.mathpix_evidence_used or chunk_index in self.mathpix_source_fallback:
                raise VisualFidelityError("work_limit_exceeded")
            target = self.mathpix_evidence_used if used else self.mathpix_source_fallback
            target[chunk_index] = 1

    def reserve_coverage_probes(self, count: int, limit: int) -> None:
        with self._lock:
            self._check_deadline_locked()
            self._validate_limit(limit, MAX_COVERAGE_PROBES)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise VisualFidelityError("work_limit_exceeded")
            next_total = self.coverage_probes_total + count
            if next_total > limit:
                raise VisualFidelityError("work_limit_exceeded")
            self.coverage_probes_total = next_total

    def _check_deadline_locked(self) -> None:
        if self._bounded_remaining_seconds() is None:
            raise VisualFidelityError("deadline_exceeded")

    def _bounded_remaining_seconds(self) -> float | None:
        if (
            isinstance(self.deadline_at, bool)
            or not isinstance(self.deadline_at, (int, float))
            or not math.isfinite(self.deadline_at)
            or self.deadline_at <= 0.0
        ):
            return None
        try:
            now = self.clock()
        except Exception:
            return None
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
        ):
            return None
        remaining = self.deadline_at - now
        if (
            not math.isfinite(remaining)
            or remaining <= 0.0
            or remaining > VISUAL_PIPELINE_DEADLINE_SECONDS
        ):
            return None
        return remaining

    @staticmethod
    def _validate_limit(limit: int, hard_cap: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > hard_cap
        ):
            raise VisualFidelityError("work_limit_exceeded")

    def _take_once(self, counters: dict, key: object) -> None:
        self._take_at_most(counters, key, 1)

    def _take_at_most(
        self,
        counters: dict,
        key: object,
        limit: int,
        *,
        hard_cap: int | None = None,
    ) -> None:
        with self._lock:
            self._check_deadline_locked()
            self._validate_limit(limit, limit if hard_cap is None else hard_cap)
            next_count = counters.get(key, 0) + 1
            if next_count > limit:
                raise VisualFidelityError("work_limit_exceeded")
            counters[key] = next_count


@dataclass(frozen=True)
class BufferReservation:
    token: int
    kind: Literal["source", "context", "asset"]
    object_id: int
    size: int
    data: bytes = field(repr=False, compare=False)


@dataclass
class VisualMemoryBudget:
    max_total_bytes: int
    max_source_bytes: int
    max_context_bytes: int
    max_asset_bytes: int
    max_page_cache_bytes: int
    source_bytes: int = 0
    context_bytes: int = 0
    asset_bytes: int = 0
    page_cache_bytes: int = 0
    _next_buffer_token: int = 1
    _buffer_entries: dict[int, BufferReservation] = field(default_factory=dict, repr=False)
    _buffer_token_by_object_id: dict[int, int] = field(default_factory=dict, repr=False)
    _page_entries: dict[object, int] = field(default_factory=dict, repr=False)
    _closed: bool = field(default=False, repr=False, compare=False)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def reserve_buffer(
        self, kind: Literal["source", "context", "asset"], data: bytes
    ) -> BufferReservation | None:
        with self._lock:
            if (
                self._closed
                or kind not in ("source", "context", "asset")
                or type(data) is not bytes
            ):
                raise VisualFidelityError("memory_limit_exceeded")
            object_id = id(data)
            existing_token = self._buffer_token_by_object_id.get(object_id)
            if existing_token is not None:
                existing = self._buffer_entries.get(existing_token)
                if existing is None or existing.data is not data or existing.kind != kind:
                    raise VisualFidelityError("memory_limit_exceeded")
                return None

            size = len(data)
            current = self._category_bytes(kind)
            if current + size > self._category_cap(kind) or self._resident_bytes_locked() + size > self.max_total_bytes:
                raise VisualFidelityError("memory_limit_exceeded")

            token = self._next_buffer_token
            reservation = BufferReservation(token, kind, object_id, size, data)
            self._next_buffer_token += 1
            self._buffer_entries[token] = reservation
            self._buffer_token_by_object_id[object_id] = token
            self._set_category_bytes(kind, current + size)
            return reservation

    def release_buffer(self, reservation: BufferReservation) -> None:
        with self._lock:
            if self._closed or not isinstance(reservation, BufferReservation):
                raise VisualFidelityError("memory_limit_exceeded")
            active = self._buffer_entries.get(reservation.token)
            if (
                active is not reservation
                or active.kind != reservation.kind
                or active.object_id != reservation.object_id
                or active.size != reservation.size
                or active.data is not reservation.data
                or id(active.data) != active.object_id
                or self._buffer_token_by_object_id.get(active.object_id) != active.token
            ):
                raise VisualFidelityError("memory_limit_exceeded")
            current = self._category_bytes(active.kind)
            if active.size > current:
                raise VisualFidelityError("memory_limit_exceeded")
            del self._buffer_entries[active.token]
            del self._buffer_token_by_object_id[active.object_id]
            self._set_category_bytes(active.kind, current - active.size)

    def _reservation_is_active(self, reservation: BufferReservation) -> bool:
        """Return whether this exact original handle remains budget-owned."""
        with self._lock:
            if not isinstance(reservation, BufferReservation):
                raise VisualFidelityError("memory_limit_exceeded")
            category_sizes = {"source": 0, "context": 0, "asset": 0}
            for token, entry in self._buffer_entries.items():
                if (
                    entry.token != token
                    or entry.kind not in category_sizes
                    or entry.object_id != id(entry.data)
                    or entry.size != len(entry.data)
                    or self._buffer_token_by_object_id.get(entry.object_id) != token
                ):
                    raise VisualFidelityError("memory_limit_exceeded")
                category_sizes[entry.kind] += entry.size
            if (
                len(self._buffer_token_by_object_id) != len(self._buffer_entries)
                or category_sizes["source"] != self.source_bytes
                or category_sizes["context"] != self.context_bytes
                or category_sizes["asset"] != self.asset_bytes
            ):
                raise VisualFidelityError("memory_limit_exceeded")
            active = self._buffer_entries.get(reservation.token)
            indexed_token = self._buffer_token_by_object_id.get(reservation.object_id)
            if active is None:
                if indexed_token is not None:
                    raise VisualFidelityError("memory_limit_exceeded")
                return False
            if (
                active is not reservation
                or active.kind != reservation.kind
                or active.object_id != reservation.object_id
                or active.size != reservation.size
                or active.data is not reservation.data
                or id(active.data) != active.object_id
                or len(active.data) != active.size
                or indexed_token != active.token
            ):
                raise VisualFidelityError("memory_limit_exceeded")
            return True

    def reserve_page(self, cache_key: object, decoded_bytes: int) -> None:
        with self._lock:
            if self._closed:
                raise VisualFidelityError("memory_limit_exceeded")
            try:
                duplicate = cache_key in self._page_entries
            except (TypeError, ValueError):
                raise VisualFidelityError("memory_limit_exceeded") from None
            if (
                duplicate
                or isinstance(decoded_bytes, bool)
                or not isinstance(decoded_bytes, int)
                or decoded_bytes <= 0
                or self.page_cache_bytes + decoded_bytes > self.max_page_cache_bytes
                or self._resident_bytes_locked() + decoded_bytes > self.max_total_bytes
            ):
                raise VisualFidelityError("memory_limit_exceeded")
            self._page_entries[cache_key] = decoded_bytes
            self.page_cache_bytes += decoded_bytes

    def page_would_fit(self, decoded_bytes: int) -> bool:
        with self._lock:
            if self._closed:
                raise VisualFidelityError("memory_limit_exceeded")
            return (
                not isinstance(decoded_bytes, bool)
                and isinstance(decoded_bytes, int)
                and decoded_bytes > 0
                and self.page_cache_bytes + decoded_bytes <= self.max_page_cache_bytes
                and self._resident_bytes_locked() + decoded_bytes <= self.max_total_bytes
            )

    def release_page(self, cache_key: object) -> None:
        with self._lock:
            try:
                decoded_bytes = self._page_entries.pop(cache_key, None)
            except (TypeError, ValueError):
                return
            if decoded_bytes is not None:
                self.page_cache_bytes = max(0, self.page_cache_bytes - decoded_bytes)

    def close(self) -> None:
        """Terminally and idempotently release all encoded job buffers."""
        with self._lock:
            if self._closed:
                return
            if self._page_entries or self.page_cache_bytes != 0:
                raise VisualFidelityError("memory_limit_exceeded")

            category_sizes = {"source": 0, "context": 0, "asset": 0}
            for token, reservation in self._buffer_entries.items():
                if (
                    reservation.token != token
                    or reservation.kind not in category_sizes
                    or reservation.object_id != id(reservation.data)
                    or reservation.size != len(reservation.data)
                    or self._buffer_token_by_object_id.get(reservation.object_id) != token
                ):
                    raise VisualFidelityError("memory_limit_exceeded")
                category_sizes[reservation.kind] += reservation.size
            if (
                len(self._buffer_token_by_object_id) != len(self._buffer_entries)
                or category_sizes["source"] != self.source_bytes
                or category_sizes["context"] != self.context_bytes
                or category_sizes["asset"] != self.asset_bytes
            ):
                raise VisualFidelityError("memory_limit_exceeded")

            for reservation in tuple(self._buffer_entries.values()):
                current = self._category_bytes(reservation.kind)
                self._set_category_bytes(reservation.kind, current - reservation.size)
            self._buffer_entries.clear()
            self._buffer_token_by_object_id.clear()
            self._closed = True

    def resident_bytes(self) -> int:
        with self._lock:
            return self._resident_bytes_locked()

    def _resident_bytes_locked(self) -> int:
        return self.source_bytes + self.context_bytes + self.asset_bytes + self.page_cache_bytes

    def _category_bytes(self, kind: Literal["source", "context", "asset"]) -> int:
        return getattr(self, f"{kind}_bytes")

    def _set_category_bytes(self, kind: Literal["source", "context", "asset"], value: int) -> None:
        setattr(self, f"{kind}_bytes", value)

    def _category_cap(self, kind: Literal["source", "context", "asset"]) -> int:
        return getattr(self, f"max_{kind}_bytes")


def validate_manifest(
    manifest: VisualManifest, *, total_pages: int, max_visuals: int = 64
) -> None:
    if total_pages > 300:
        raise VisualFidelityError("too_many_pages")
    if total_pages <= 0:
        raise VisualFidelityError("link_invalid")
    if len(manifest.visuals) > max_visuals:
        raise VisualFidelityError("too_many_visuals")

    ids: set[str] = set()
    orders: set[int] = set()
    prior_order: int | None = None
    for visual in manifest.visuals:
        if visual.visual_id in ids or visual.source_order in orders:
            raise VisualFidelityError("link_invalid")
        if prior_order is not None and visual.source_order <= prior_order:
            raise VisualFidelityError("link_invalid")
        if visual.global_page > total_pages:
            raise VisualFidelityError("link_invalid")
        ids.add(visual.visual_id)
        orders.add(visual.source_order)
        prior_order = visual.source_order


def manifest_ids(manifest: VisualManifest) -> tuple[str, ...]:
    return tuple(visual.visual_id for visual in manifest.visuals)


@dataclass(frozen=True)
class VisualReferenceCheck:
    placed_ids: tuple[str, ...]
    unlinked_ids: tuple[str, ...]


def validate_visual_references(
    manifest: VisualManifest,
    section_id_lists: Sequence[Sequence[str]],
) -> VisualReferenceCheck:
    """Reject invalid proposed links and return missing IDs in source order."""
    expected = manifest_ids(manifest)
    positions = {visual_id: index for index, visual_id in enumerate(expected)}
    if len(positions) != len(expected):
        raise VisualFidelityError("link_invalid")

    placed: list[str] = []
    placed_with_sections: list[tuple[str, int]] = []
    seen: set[str] = set()
    for section_index, section_ids in enumerate(section_id_lists):
        for visual_id in section_ids:
            if (
                not isinstance(visual_id, str)
                or visual_id not in positions
                or visual_id in seen
            ):
                raise VisualFidelityError("link_invalid")
            seen.add(visual_id)
            placed.append(visual_id)
            placed_with_sections.append((visual_id, section_index))

    prior_manifest_position = -1
    prior_section_index = -1
    for visual_id, section_index in placed_with_sections:
        manifest_position = positions[visual_id]
        if (
            manifest_position < prior_manifest_position
            or section_index < prior_section_index
        ):
            raise VisualFidelityError("link_invalid")
        prior_manifest_position = manifest_position
        prior_section_index = section_index

    return VisualReferenceCheck(
        placed_ids=tuple(placed),
        unlinked_ids=tuple(
            visual_id for visual_id in expected if visual_id not in seen
        ),
    )


def assert_ordered_exact_once(
    manifest: VisualManifest, rendered_ids: Sequence[str]
) -> None:
    expected = manifest_ids(manifest)
    actual = tuple(rendered_ids)
    if actual != expected or len(actual) != len(set(actual)):
        raise VisualFidelityError("link_invalid")
