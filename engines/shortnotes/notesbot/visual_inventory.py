"""Bounded, source-authoritative visual inventory and coverage audit."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

from . import config
from .errors import VisualFidelityError
from .mathpix_client import MathpixEvidence, serialize_mathpix_evidence
from .pdf_utils import PdfChunk, page_render_scale
from .pdfium_guard import PDFIUM_LOCK
from .visual_manifest import (
    AuditedRegion,
    CoverageProbe,
    InventoryAuditResponse,
    InventoryLabel,
    InventoryResponse,
    SourceContext,
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    VisualManifest,
    VisualMemoryBudget,
    VisualRegion,
    VisualRunBudget,
    validate_manifest,
)


INVENTORY_PROMPT = """You are performing source enumeration only.
Inspect every original source page and enumerate every diagram, labelled structure,
chemical structure, apparatus, circuit, ray/path figure, mechanism, and drawn graph.
Exclude tables, body text, and decorations. Do not summarize, teach, repair, or correct.
Every box is exactly [x0, y0, x1, y1] in original-page percentages with
0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100. Never emit pixels or
[x, y, width, height]. Every page_anchor is exactly [x, y] with both values in 0..100.
Copy label text, source headings, and requested context exact_text verbatim from source.
Mathpix regions and text are fallible planning proposals only. Mathpix OCR and SMILES are
non-authoritative: they cannot authorize an element, correct teacher content, or overwrite
source pixels. Independently confirm every proposed box, label, graph relationship, and
chemistry item against original source pixels. If Mathpix evidence is absent, incomplete,
or uncertain, independently inventory the original pages in this same inventory call and
leave any low-confidence proposal uncertain for explicit coverage-audit resolution.
For each visual emit one local anchor_ref for its source heading/topic anchor and at most
two same-topic context regions on the same or immediately adjacent page inside this chunk.
Context may be a heading, explanation, graph description, or legend; it is interpretation
only and cannot authorize a mark absent from the visual source. Return only the closed
InventoryResponse schema and mark every inspected local page in pages_checked."""


AUDIT_PROMPT = """Perform one source-visible coverage audit over every original page and
the boxed overlay. Return only the closed InventoryAuditResponse schema. Map every
provisional ID exactly once through source_region_ids, account for every retained probe
exactly once, and use only covered_visual, ordinary_text, table, decoration, or unresolved.
Mixed or uncertain probes are unresolved and therefore fail closure. A probe whose centroid
is inside an initial region or whose intersection is at least 25% of its area is mandatory:
it must be covered_visual and name a final region mapped from that initial ID. A merge needs
matching page/kind/topic/ordered labels/context, IoU at least 0.90, and containment at least
0.96. For every covered_visual probe, each zero-based final_region_indices entry must name
a final region on the same local_page with positive-area overlap between the probe box and
final region box; never attach a probe to a distant or merely edge-touching region. For every
other outcome final_region_indices must be empty. Every final region needs covered component
support. Its edge support is the union of
all named component probes, every label page_box, and every label page_anchor; grid probes
establish coverage only. Contain the complete support union within the configured numerical
epsilon and extend no edge more than the configured bounded outward margin. Never clip a
one-pixel bond/arrowhead, invent/move/drop labels, relocate mapped regions, mutate mapped
kind/topic/context, or expand a small support into a whole page. A new empty-ID region needs
overlapping covered component support. Set coverage_complete only when every page, initial
region, and exact probe-ID set has been resolved without missing, duplicate, unknown,
unsupported, or ambiguous entries."""


_AUDIT_PAYLOAD_MARKER = "\nRUNTIME_AUDIT_PAYLOAD_JSON="


@dataclass(frozen=True)
class ModelFile:
    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class AuditedChunk:
    chunk_index: int
    chunk_sha256: str
    first_global_page: int
    page_count: int
    inventory: InventoryResponse
    provisional_ids: tuple[str, ...]
    probes: tuple[CoverageProbe, ...]
    audit: InventoryAuditResponse
    audit_binding_sha256: str
    closure_binding_sha256: str


SchemaCall = Callable[
    [list[ModelFile], str, type[BaseModel], int, float, VisualRunBudget], BaseModel
]


def _fail(code: str = "inventory_incomplete") -> None:
    raise VisualFidelityError(code)


def _encoded_size(size: int) -> int:
    return 4 * math.ceil(size / 3)


def _exact_pages(page_count: int) -> tuple[int, ...]:
    return tuple(range(1, page_count + 1))


def _canonical_json(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _audit_binding(chunk: PdfChunk, audit: InventoryAuditResponse) -> str:
    digest = hashlib.sha256()
    digest.update(b"visual-audit-v1\0")
    digest.update(chunk.sha256_hex.encode("ascii"))
    digest.update(str(chunk.index).encode("ascii"))
    digest.update(str(chunk.first_global_page).encode("ascii"))
    digest.update(str(chunk.page_count).encode("ascii"))
    digest.update(_canonical_json(audit))
    return digest.hexdigest()


def _closure_binding(
    chunk: PdfChunk,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> str:
    payload = json.dumps(
        {
            "audit": audit.model_dump(mode="json"),
            "inventory": inventory.model_dump(mode="json"),
            "probes": [probe.model_dump(mode="json") for probe in probes],
            "provisional_ids": list(provisional_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"visual-audit-closure-v1\0")
    digest.update(chunk.sha256_hex.encode("ascii"))
    digest.update(str(chunk.index).encode("ascii"))
    digest.update(str(chunk.first_global_page).encode("ascii"))
    digest.update(str(chunk.page_count).encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


def _validated_closure(
    chunk: PdfChunk,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> tuple[
    InventoryResponse,
    tuple[str, ...],
    tuple[CoverageProbe, ...],
    InventoryAuditResponse,
]:
    try:
        checked_inventory = InventoryResponse.model_validate(
            inventory.model_dump(mode="python")
        )
        checked_ids = tuple(provisional_ids)
        if any(type(value) is not str for value in checked_ids):
            _fail()
        checked_probes = tuple(
            CoverageProbe.model_validate(probe.model_dump(mode="python"))
            for probe in probes
        )
        checked_audit = InventoryAuditResponse.model_validate(
            audit.model_dump(mode="python")
        )
    except VisualFidelityError:
        raise
    except Exception:
        _fail()
    _validate_inventory(checked_inventory, chunk)
    expected_ids = tuple(
        f"draft-c{chunk.index:02d}-{ordinal:03d}"
        for ordinal in range(1, len(checked_inventory.regions) + 1)
    )
    if checked_ids != expected_ids or len({probe.probe_id for probe in checked_probes}) != len(
        checked_probes
    ):
        _fail()
    for probe in checked_probes:
        if (
            not 1 <= probe.local_page <= chunk.page_count
            or probe.foreground_fraction <= 0.0
            or (probe.kind == "grid" and probe.foreground_fraction < 0.005)
        ):
            _fail()
    validate_inventory_audit(
        checked_inventory, checked_ids, checked_probes, checked_audit
    )
    return checked_inventory, checked_ids, checked_probes, checked_audit


def bind_audited_chunk(
    chunk: PdfChunk,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> AuditedChunk:
    """Validate and bind the exact inventory/probe/audit closure to its chunk."""
    checked_inventory, checked_ids, checked_probes, checked_audit = _validated_closure(
        chunk, inventory, provisional_ids, probes, audit
    )
    return AuditedChunk(
        chunk_index=chunk.index,
        chunk_sha256=chunk.sha256_hex,
        first_global_page=chunk.first_global_page,
        page_count=chunk.page_count,
        inventory=checked_inventory,
        provisional_ids=checked_ids,
        probes=checked_probes,
        audit=checked_audit,
        audit_binding_sha256=_audit_binding(chunk, checked_audit),
        closure_binding_sha256=_closure_binding(
            chunk,
            checked_inventory,
            checked_ids,
            checked_probes,
            checked_audit,
        ),
    )


def _model_timeout(budget: VisualRunBudget) -> float:
    timeout = min(float(config.VISUAL_MODEL_TIMEOUT_SECONDS), budget.remaining_seconds())
    if timeout < 0.001:
        _fail("deadline_exceeded")
    return timeout


def _model_response(
    call_schema: SchemaCall,
    files: list[ModelFile],
    prompt: str,
    schema: type[BaseModel],
    chunk: PdfChunk,
    budget: VisualRunBudget,
    timeout: float,
) -> BaseModel:
    try:
        raw = call_schema(files, prompt, schema, chunk.index, timeout, budget)
        payload = raw.model_dump(mode="python") if isinstance(raw, BaseModel) else raw
        return schema.model_validate(payload)
    except VisualFidelityError:
        raise
    except Exception:
        _fail()


def _default_memory_budget() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=config.MAX_VISUAL_RESIDENT_BYTES,
        max_source_bytes=config.MAX_VISUAL_SOURCE_TOTAL_BYTES,
        max_context_bytes=config.MAX_VISUAL_CONTEXT_TOTAL_BYTES,
        max_asset_bytes=config.MAX_VISUAL_ASSET_TOTAL_BYTES,
        max_page_cache_bytes=config.MAX_VISUAL_PAGE_CACHE_BYTES,
    )


def _default_run_budget() -> VisualRunBudget:
    now = time.monotonic()
    duration = min(780.0, float(config.VISUAL_PIPELINE_DEADLINE_SECONDS))
    return VisualRunBudget(
        deadline_at=now + max(0.001, duration - 0.001),
        clock=time.monotonic,
    )


def _validate_inventory(response: InventoryResponse, chunk: PdfChunk) -> None:
    if response.pages_checked != _exact_pages(chunk.page_count):
        _fail()
    anchors: dict[str, tuple[int, tuple[float, float, float, float], str]] = {}
    for region in response.regions:
        if not 1 <= region.local_page <= chunk.page_count:
            _fail()
        anchor = region.topic_anchor
        if not 1 <= anchor.local_page <= chunk.page_count:
            _fail()
        source = (anchor.local_page, anchor.box, anchor.exact_heading)
        previous = anchors.setdefault(anchor.anchor_ref, source)
        if previous != source:
            _fail()
        for context in region.context_regions:
            if (
                context.anchor_ref != anchor.anchor_ref
                or not 1 <= context.local_page <= chunk.page_count
                or abs(context.local_page - region.local_page) > 1
            ):
                _fail()


def _rendered_pages(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    memory_budget: VisualMemoryBudget,
    *,
    local_pages: set[int] | None = None,
) -> Iterator[tuple[int, Image.Image]]:
    budget.check_deadline()
    selected = None if local_pages is None else frozenset(local_pages)
    if selected is not None and not selected.issubset(range(1, chunk.page_count + 1)):
        _fail("crop_failed")
    # Each PDFium step holds the shared lock (see pdfium_guard); the lock is
    # never held while a page is handed to the caller.
    try:
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(chunk.pdf_bytes)
    except Exception:
        _fail("crop_failed")
    try:
        budget.check_deadline()
        with PDFIUM_LOCK:
            document_pages = len(document)
        if document_pages != chunk.page_count or len(chunk.page_geometries) != chunk.page_count:
            _fail()
        for index, geometry in enumerate(chunk.page_geometries):
            budget.check_deadline()
            if selected is not None and index + 1 not in selected:
                continue
            decoded_bytes = geometry.rendered_width * geometry.rendered_height * 8
            cache_key = object()
            memory_budget.reserve_page(cache_key, decoded_bytes)
            page = None
            bitmap = None
            image = None
            try:
                budget.check_deadline()
                with PDFIUM_LOCK:
                    page = document[index]
                    # Same per-page scale the chunk geometry was measured at.
                    bitmap = page.render(
                        scale=page_render_scale(page.get_width(), page.get_height())
                    )
                budget.check_deadline()
                with PDFIUM_LOCK:
                    image = bitmap.to_pil().convert("RGB")
                budget.check_deadline()
                if (
                    image.width != geometry.rendered_width
                    or image.height != geometry.rendered_height
                    or image.width > config.MAX_VISUAL_PAGE_DIMENSION_PX
                    or image.height > config.MAX_VISUAL_PAGE_DIMENSION_PX
                    or image.width * image.height > config.MAX_VISUAL_PAGE_PIXELS
                ):
                    _fail("crop_failed")
                yield index + 1, image
                budget.check_deadline()
            except VisualFidelityError:
                raise
            except Exception:
                _fail("crop_failed")
            finally:
                if image is not None:
                    image.close()
                with PDFIUM_LOCK:
                    if bitmap is not None:
                        try:
                            bitmap.close()
                        except Exception:
                            pass
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
                memory_budget.release_page(cache_key)
    finally:
        with PDFIUM_LOCK:
            try:
                document.close()
            except Exception:
                pass


def _foreground_mask(image: Image.Image, budget: VisualRunBudget) -> bytearray:
    budget.check_deadline()
    threshold_sq = float(config.COVERAGE_FOREGROUND_DISTANCE_RGB) ** 2
    mask = bytearray(image.width * image.height)
    pixels = image.load()
    for y in range(image.height):
        budget.check_deadline()
        offset = y * image.width
        for x in range(image.width):
            red, green, blue = pixels[x, y]
            distance = (255 - red) ** 2 + (255 - green) ** 2 + (255 - blue) ** 2
            if distance >= threshold_sq:
                mask[offset + x] = 1
    budget.check_deadline()
    return mask


def _row_runs(
    mask: bytearray,
    width: int,
    height: int,
    budget: VisualRunBudget,
) -> list[list[tuple[int, int, int]]]:
    budget.check_deadline()
    close_width = max(3, width // 100)
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        budget.check_deadline()
        offset = y * width
        raw: list[tuple[int, int]] = []
        x = 0
        while x < width:
            if mask[offset + x] == 0:
                x += 1
                continue
            start = x
            while x + 1 < width and mask[offset + x + 1]:
                x += 1
            raw.append((start, x))
            x += 1
        merged: list[tuple[int, int, int]] = []
        for start, end in raw:
            count = end - start + 1
            if merged and start - merged[-1][1] - 1 < close_width:
                prior_start, _, prior_count = merged[-1]
                merged[-1] = (prior_start, end, prior_count + count)
            else:
                merged.append((start, end, count))
        rows.append(merged)
    budget.check_deadline()
    return rows


def _component_boxes(
    mask: bytearray, width: int, height: int, budget: VisualRunBudget
) -> list[tuple[int, int, int, int, int]]:
    budget.check_deadline()
    rows = _row_runs(mask, width, height, budget)
    intervals: list[tuple[int, int, int, int]] = []
    row_indices: list[list[int]] = []
    for y, runs in enumerate(rows):
        budget.check_deadline()
        indices = []
        for x0, x1, count in runs:
            indices.append(len(intervals))
            intervals.append((x0, x1, y, count))
        row_indices.append(indices)
    parent = list(range(len(intervals)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: int, second: int) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[right] = left

    for y, current in enumerate(row_indices):
        budget.check_deadline()
        for prior_y in range(max(0, y - 2), y):
            for current_index in current:
                cx0, cx1, _, _ = intervals[current_index]
                for prior_index in row_indices[prior_y]:
                    budget.check_deadline()
                    px0, px1, _, _ = intervals[prior_index]
                    if cx0 <= px1 and px0 <= cx1:
                        union(current_index, prior_index)
    grouped: dict[int, list[int]] = {}
    for index in range(len(intervals)):
        grouped.setdefault(find(index), []).append(index)
    boxes: list[tuple[int, int, int, int, int]] = []
    for members in grouped.values():
        budget.check_deadline()
        count = sum(intervals[index][3] for index in members)
        if count < config.COVERAGE_COMPONENT_MIN_PX:
            continue
        boxes.append(
            (
                min(intervals[index][0] for index in members),
                min(intervals[index][2] for index in members),
                max(intervals[index][1] for index in members) + 1,
                max(intervals[index][2] for index in members) + 1,
                count,
            )
        )
    boxes.sort(key=lambda item: (item[1], item[0], item[3], item[2]))
    budget.check_deadline()
    return boxes


def _pct_box(
    box: tuple[int, int, int, int], width: int, height: int
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    return (100.0 * x0 / width, 100.0 * y0 / height, 100.0 * x1 / width, 100.0 * y1 / height)


def _cell_foreground(mask: bytearray, width: int, box: tuple[int, int, int, int]) -> int:
    x0, y0, x1, y1 = box
    return sum(mask[y * width + x0 : y * width + x1].count(1) for y in range(y0, y1))


def _grid_boxes(
    mask: bytearray,
    width: int,
    height: int,
    components: list[tuple[int, int, int, int, int]],
    budget: VisualRunBudget,
    *,
    max_depth: int | None = None,
) -> list[tuple[tuple[int, int, int, int], float]]:
    budget.check_deadline()
    depth_limit = (
        int(config.COVERAGE_PROBE_MAX_DEPTH)
        if max_depth is None
        else max(0, min(int(config.COVERAGE_PROBE_MAX_DEPTH), max_depth))
    )
    centroids = tuple(((x0 + x1) / 2, (y0 + y1) / 2) for x0, y0, x1, y1, _ in components)
    retained: list[tuple[tuple[int, int, int, int], float]] = []

    def visit(box: tuple[int, int, int, int], depth: int) -> None:
        budget.check_deadline()
        x0, y0, x1, y1 = box
        area = (x1 - x0) * (y1 - y0)
        if area <= 0:
            budget.check_deadline()
            return
        foreground = _cell_foreground(mask, width, box)
        budget.check_deadline()
        fraction = foreground / area
        if fraction < 0.005:
            budget.check_deadline()
            return
        middle_x, middle_y = (x0 + x1) // 2, (y0 + y1) // 2
        children = (
            (x0, y0, middle_x, middle_y),
            (middle_x, y0, x1, middle_y),
            (x0, middle_y, middle_x, y1),
            (middle_x, middle_y, x1, y1),
        )
        can_split = depth < depth_limit and all(
            child[2] - child[0] >= config.COVERAGE_PROBE_MIN_CELL_PX
            and child[3] - child[1] >= config.COVERAGE_PROBE_MIN_CELL_PX
            for child in children
        )
        if can_split:
            occupied = {
                (0 if cx < middle_x else 1, 0 if cy < middle_y else 1)
                for cx, cy in centroids
                if x0 <= cx < x1 and y0 <= cy < y1
            }
            if len(occupied) > 1:
                for child in children:
                    visit(child, depth + 1)
                budget.check_deadline()
                return
        retained.append((box, fraction))
        budget.check_deadline()

    for row in range(4):
        for column in range(4):
            budget.check_deadline()
            visit(
                (
                    width * column // 4,
                    height * row // 4,
                    width * (column + 1) // 4,
                    height * (row + 1) // 4,
                ),
                0,
            )
    budget.check_deadline()
    return retained


def _chunk_page_probe_allowance(page_count: int) -> int:
    """Fair-share per-page probe budget for one chunk.

    Splitting MAX_COVERAGE_PROBES_PER_CHUNK across the chunk's pages keeps a
    dense many-page lecture inside the reserve_coverage_probes ceiling instead
    of hard-failing once cumulative probes cross it (256/page * 33+ pages).
    The floor keeps the depth-0 coverage grid (16 cells) plus component slots
    viable on every page.
    """
    pages = max(1, int(page_count))
    fair_share = int(config.MAX_COVERAGE_PROBES_PER_CHUNK) // pages
    return max(24, min(int(config.MAX_COVERAGE_PROBES_PER_PAGE), fair_share))


def _page_probes(
    image: Image.Image,
    page_number: int,
    budget: VisualRunBudget,
    *,
    per_page_limit: int | None = None,
) -> tuple[CoverageProbe, ...]:
    budget.check_deadline()
    mask = _foreground_mask(image, budget)
    components = _component_boxes(mask, image.width, image.height, budget)
    limit_cap = min(256, int(config.MAX_COVERAGE_PROBES_PER_PAGE))
    if per_page_limit is None:
        per_page_limit = limit_cap
    else:
        per_page_limit = min(limit_cap, max(1, int(per_page_limit)))
    if per_page_limit <= 0:
        _fail("work_limit_exceeded")

    grid_boxes = _grid_boxes(
        mask,
        image.width,
        image.height,
        components,
        budget,
    )
    # Dense scans can yield hundreds of tiny connected components.  Preserve
    # complete page coverage with adaptive grid probes, coarsening that grid
    # only if necessary, then spend the remaining bounded slots on the
    # strongest component candidates instead of failing an otherwise valid
    # lecture page.
    grid_limit = max(0, per_page_limit - (1 if components else 0))
    if len(grid_boxes) > grid_limit:
        for depth in range(int(config.COVERAGE_PROBE_MAX_DEPTH) - 1, -1, -1):
            budget.check_deadline()
            grid_boxes = _grid_boxes(
                mask,
                image.width,
                image.height,
                components,
                budget,
                max_depth=depth,
            )
            if len(grid_boxes) <= grid_limit:
                break
    if len(grid_boxes) > grid_limit:
        _fail("work_limit_exceeded")

    component_limit = per_page_limit - len(grid_boxes)
    if len(components) > component_limit:
        budget.check_deadline()
        components = sorted(
            components,
            key=lambda item: (
                -item[4],
                -((item[2] - item[0]) * (item[3] - item[1])),
                item[1],
                item[0],
                item[3],
                item[2],
            ),
        )[:component_limit]
        components.sort(key=lambda item: (item[1], item[0], item[3], item[2]))
        budget.check_deadline()

    page_probes: list[CoverageProbe] = []
    for ordinal, (x0, y0, x1, y1, count) in enumerate(components, start=1):
        budget.check_deadline()
        area = (x1 - x0) * (y1 - y0)
        page_probes.append(
            CoverageProbe(
                probe_id=f"component-p{page_number:03d}-{ordinal:04d}",
                kind="component",
                local_page=page_number,
                box=_pct_box((x0, y0, x1, y1), image.width, image.height),
                foreground_fraction=count / area,
            )
        )
    for ordinal, (box, fraction) in enumerate(grid_boxes, start=1):
        budget.check_deadline()
        page_probes.append(
            CoverageProbe(
                probe_id=f"grid-p{page_number:03d}-{ordinal:04d}",
                kind="grid",
                local_page=page_number,
                box=_pct_box(box, image.width, image.height),
                foreground_fraction=fraction,
            )
        )
    unique: list[CoverageProbe] = []
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    for probe in page_probes:
        budget.check_deadline()
        key = (probe.kind, probe.box)
        if key not in seen:
            seen.add(key)
            unique.append(probe)
    if per_page_limit <= 0 or len(unique) > per_page_limit:
        _fail("work_limit_exceeded")
    budget.check_deadline()
    return tuple(unique)


def generate_coverage_probes(
    chunk: PdfChunk,
    budget: VisualRunBudget | None = None,
    memory_budget: VisualMemoryBudget | None = None,
) -> tuple[CoverageProbe, ...]:
    """Generate probes one decoded page at a time under shared budgets."""
    run_budget = budget or _default_run_budget()
    memory = memory_budget or _default_memory_budget()
    probes: list[CoverageProbe] = []
    allowance = _chunk_page_probe_allowance(chunk.page_count)
    rendered_pages = _rendered_pages(chunk, run_budget, memory)
    try:
        for page_number, image in rendered_pages:
            page_probes = _page_probes(
                image, page_number, run_budget, per_page_limit=allowance
            )
            run_budget.reserve_coverage_probes(
                len(page_probes), config.MAX_COVERAGE_PROBES_TOTAL
            )
            probes.extend(page_probes)
    finally:
        rendered_pages.close()
    run_budget.check_deadline()
    return tuple(probes)


def _draw_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    width: int,
    height: int,
    color: tuple[int, int, int],
    line_width: int,
) -> tuple[int, int, int, int]:
    pixel_box = (
        round(box[0] * width / 100),
        round(box[1] * height / 100),
        round(box[2] * width / 100),
        round(box[3] * height / 100),
    )
    draw.rectangle(pixel_box, outline=color, width=line_width)
    return pixel_box


def _writer_bytes(writer: PdfWriter) -> bytes:
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _release_buffer(memory_budget: VisualMemoryBudget, reservation: object | None) -> None:
    if reservation is not None:
        memory_budget.release_buffer(reservation)  # type: ignore[arg-type]


def _draw_overlay_page(
    image: Image.Image,
    local_page: int,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    budget: VisualRunBudget,
) -> bytes:
    budget.check_deadline()
    draw = ImageDraw.Draw(image)
    for provisional_id, region in zip(provisional_ids, inventory.regions):
        budget.check_deadline()
        if region.local_page != local_page:
            continue
        pixel_box = _draw_box(
            draw, region.box, image.width, image.height, (255, 20, 147), 4
        )
        draw.text(
            (pixel_box[0] + 3, pixel_box[1] + 3),
            provisional_id,
            fill=(255, 20, 147),
        )
    for probe in probes:
        budget.check_deadline()
        if probe.local_page != local_page:
            continue
        color = (20, 80, 255) if probe.kind == "component" else (0, 170, 230)
        pixel_box = _draw_box(
            draw, probe.box, image.width, image.height, color, 2
        )
        draw.text(
            (pixel_box[0] + 2, pixel_box[1] + 2), probe.probe_id, fill=color
        )
    budget.check_deadline()
    single = io.BytesIO()
    try:
        image.save(single, format="PDF", resolution=100.0)
    except Exception:
        _fail("crop_failed")
    budget.check_deadline()
    return single.getvalue()


def _append_overlay_page(
    writer: PdfWriter,
    single_page: bytes,
    prior_reservation: object | None,
    chunk: PdfChunk,
    budget: VisualRunBudget,
    memory_budget: VisualMemoryBudget,
) -> tuple[bytes, object | None]:
    budget.check_deadline()
    single_reservation = memory_budget.reserve_buffer("source", single_page)
    next_reservation = None
    try:
        writer.add_page(PdfReader(io.BytesIO(single_page)).pages[0])
        budget.check_deadline()
        partial = _writer_bytes(writer)
        budget.check_deadline()
        if (
            len(partial) > config.MAX_AUDIT_PDF_BYTES
            or len(chunk.pdf_bytes) + len(partial)
            > config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
            or _encoded_size(len(chunk.pdf_bytes)) + _encoded_size(len(partial))
            > config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
        ):
            _fail("source_chunk_too_large")
        next_reservation = memory_budget.reserve_buffer("source", partial)
        _release_buffer(memory_budget, prior_reservation)
        budget.check_deadline()
        return partial, next_reservation
    except VisualFidelityError:
        _release_buffer(memory_budget, next_reservation)
        raise
    except Exception:
        _release_buffer(memory_budget, next_reservation)
        _fail("crop_failed")
    finally:
        _release_buffer(memory_budget, single_reservation)


def _process_overlay_pages(
    chunk: PdfChunk,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    budget: VisualRunBudget,
    memory_budget: VisualMemoryBudget,
    *,
    supplied_probes: Sequence[CoverageProbe] | None,
) -> tuple[tuple[CoverageProbe, ...], bytes, object | None]:
    if len(provisional_ids) != len(inventory.regions):
        _fail()
    probes: list[CoverageProbe] = []
    writer = PdfWriter()
    overlay = b""
    overlay_reservation = None
    completed = False
    allowance = _chunk_page_probe_allowance(chunk.page_count)
    rendered_pages = _rendered_pages(chunk, budget, memory_budget)
    try:
        for local_page, image in rendered_pages:
            budget.check_deadline()
            if supplied_probes is None:
                page_probes = _page_probes(
                    image, local_page, budget, per_page_limit=allowance
                )
                budget.reserve_coverage_probes(
                    len(page_probes), config.MAX_COVERAGE_PROBES_TOTAL
                )
                probes.extend(page_probes)
            else:
                page_probes = tuple(
                    probe
                    for probe in supplied_probes
                    if probe.local_page == local_page
                )
            budget.check_deadline()
            single_page = _draw_overlay_page(
                image,
                local_page,
                inventory,
                provisional_ids,
                page_probes,
                budget,
            )
            overlay, overlay_reservation = _append_overlay_page(
                writer,
                single_page,
                overlay_reservation,
                chunk,
                budget,
                memory_budget,
            )
            single_page = b""
            budget.check_deadline()
        completed = True
        return (
            tuple(probes) if supplied_probes is None else tuple(supplied_probes),
            overlay,
            overlay_reservation,
        )
    finally:
        rendered_pages.close()
        if not completed:
            _release_buffer(memory_budget, overlay_reservation)


def build_audit_overlay(
    chunk: PdfChunk,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    budget: VisualRunBudget | None = None,
    memory_budget: VisualMemoryBudget | None = None,
) -> bytes:
    """Rasterize a bounded overlay incrementally, one decoded page at a time."""
    run_budget = budget or _default_run_budget()
    memory = memory_budget or _default_memory_budget()
    _, overlay, reservation = _process_overlay_pages(
        chunk,
        inventory,
        provisional_ids,
        run_budget,
        memory,
        supplied_probes=probes,
    )
    _release_buffer(memory, reservation)
    return overlay


def _box_area(box: tuple[float, float, float, float]) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def duplicate_merge_allowed(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    intersection = _intersection_area(first, second)
    union = _box_area(first) + _box_area(second) - intersection
    iou = intersection / union if union else 0.0
    containment = intersection / min(_box_area(first), _box_area(second))
    return iou >= 0.90 and containment >= 0.96


def _overlaps(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return _intersection_area(first, second) > 0.0


def _contains_box(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    epsilon: float = 0.0,
) -> bool:
    return (
        outer[0] <= inner[0] + epsilon
        and outer[1] <= inner[1] + epsilon
        and outer[2] >= inner[2] - epsilon
        and outer[3] >= inner[3] - epsilon
    )


def _mandatory_probe(probe: CoverageProbe, region: VisualRegion) -> bool:
    if probe.local_page != region.local_page:
        return False
    cx = (probe.box[0] + probe.box[2]) / 2
    cy = (probe.box[1] + probe.box[3]) / 2
    centroid_inside = region.box[0] <= cx <= region.box[2] and region.box[1] <= cy <= region.box[3]
    return centroid_inside or _intersection_area(probe.box, region.box) >= 0.25 * _box_area(probe.box)


def _support_union(boxes: Sequence[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _normalize_audit_region_bounds(
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> InventoryAuditResponse:
    """Clamp only crop edges to the validator's verified support interval."""
    probe_by_id = {probe.probe_id: probe for probe in probes}
    if len(probe_by_id) != len(probes):
        return audit
    support: list[list[tuple[float, float, float, float]]] = [
        [] for _region in audit.final_regions
    ]
    seen: set[str] = set()
    for disposition in audit.probe_dispositions:
        if disposition.probe_id in seen:
            return audit
        seen.add(disposition.probe_id)
        probe = probe_by_id.get(disposition.probe_id)
        if probe is None:
            return audit
        for index in disposition.final_region_indices:
            if index >= len(audit.final_regions):
                return audit
            region = audit.final_regions[index]
            if (
                disposition.outcome == "covered_visual"
                and probe.kind == "component"
                and probe.local_page == region.local_page
                and _overlaps(probe.box, region.box)
            ):
                support[index].append(probe.box)

    margin = float(config.AUDIT_SUPPORT_OUTWARD_MARGIN_PCT)
    normalized: list[AuditedRegion] = []
    for index, region in enumerate(audit.final_regions):
        boxes = support[index]
        if not boxes:
            normalized.append(region)
            continue
        for label in region.exact_labels:
            boxes.append(label.page_box)
            if label.page_anchor is not None:
                x, y = label.page_anchor
                boxes.append((x, y, x, y))
        union = _support_union(boxes)
        box = (
            max(0.0, union[0] - margin, min(region.box[0], union[0])),
            max(0.0, union[1] - margin, min(region.box[1], union[1])),
            min(100.0, union[2] + margin, max(region.box[2], union[2])),
            min(100.0, union[3] + margin, max(region.box[3], union[3])),
        )
        normalized.append(region.model_copy(update={"box": box}))
    return audit.model_copy(update={"final_regions": tuple(normalized)})


def _fallback_box_is_viable(
    image: Image.Image,
    box: tuple[float, float, float, float],
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
) -> bool:
    """Apply the fallback's pixel safety gates without retaining an asset."""
    from .visual_assets import (
        FALLBACK_PREFLIGHT_CHUNK_BYTES_PER_PIXEL,
        FALLBACK_PREFLIGHT_CHUNK_PIXELS,
        FALLBACK_PREFLIGHT_RETAINED_BYTES_PER_PIXEL,
        validate_source_fallback_pixels,
    )

    bounds = (
        math.floor(image.width * box[0] / 100.0),
        math.floor(image.height * box[1] / 100.0),
        math.ceil(image.width * box[2] / 100.0),
        math.ceil(image.height * box[3] / 100.0),
    )
    if not (
        0 <= bounds[0] < bounds[2] <= image.width
        and 0 <= bounds[1] < bounds[3] <= image.height
    ):
        _fail("crop_failed")
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    pixels = width * height
    if (
        width < config.MIN_VISUAL_CROP_WIDTH_PX
        or height < config.MIN_VISUAL_CROP_HEIGHT_PX
        or pixels < config.MIN_VISUAL_CROP_PIXELS
        or pixels > config.MAX_VISUAL_CROP_PIXELS
    ):
        return False
    work_key = object()
    work_bytes = (
        pixels * FALLBACK_PREFLIGHT_RETAINED_BYTES_PER_PIXEL
        + min(pixels, FALLBACK_PREFLIGHT_CHUNK_PIXELS)
        * FALLBACK_PREFLIGHT_CHUNK_BYTES_PER_PIXEL
    )
    cropped = None
    converted = None
    try:
        memory.reserve_page(work_key, work_bytes)
        budget.check_deadline()
        cropped = image.crop(bounds)
        converted = cropped.convert("RGBA")
        cropped.close()
        cropped = None
        validate_source_fallback_pixels(
            converted,
            check_deadline=budget.check_deadline,
        )
        budget.check_deadline()
        return True
    except VisualFidelityError as error:
        if error.code == "fallback_damaged":
            return False
        raise
    except Exception:
        _fail("crop_failed")
    finally:
        try:
            if converted is not None:
                converted.close()
        finally:
            try:
                if cropped is not None:
                    cropped.close()
            finally:
                memory.release_page(work_key)


def _log_fallback_bounds_failure(
    image: Image.Image,
    region: AuditedRegion,
    expanded: tuple[float, float, float, float],
    support_count: int,
) -> None:
    """Fixed-geometry telemetry only; never source or model content."""
    def _px(box: tuple[float, float, float, float]) -> str:
        width = round(image.width * (box[2] - box[0]) / 100.0)
        height = round(image.height * (box[3] - box[1]) / 100.0)
        return f"{width}x{height}"

    def _reason(box: tuple[float, float, float, float]) -> str:
        width = round(image.width * (box[2] - box[0]) / 100.0)
        height = round(image.height * (box[3] - box[1]) / 100.0)
        pixels = width * height
        if (
            width < config.MIN_VISUAL_CROP_WIDTH_PX
            or height < config.MIN_VISUAL_CROP_HEIGHT_PX
            or pixels < config.MIN_VISUAL_CROP_PIXELS
        ):
            return "too_small"
        if pixels > config.MAX_VISUAL_CROP_PIXELS:
            return "too_large"
        return "pixel_check"

    print(
        "[notesbot.visual-error-detail] stage=fallback_bounds"
        f" page={region.local_page}"
        f" box_px={_px(tuple(region.box))} box_reason={_reason(tuple(region.box))}"
        f" expanded_px={_px(expanded)} expanded_reason={_reason(expanded)}"
        f" expanded_unchanged={int(tuple(expanded) == tuple(region.box))}"
        f" support={support_count}"
    )


def _expand_unsafe_fallback_bounds(
    chunk: PdfChunk,
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
) -> tuple[InventoryAuditResponse, frozenset[str]]:
    """Keep safe model geometry; otherwise try only the verified max margin."""
    probe_by_id = {probe.probe_id: probe for probe in probes}
    dispositions = {item.probe_id: item for item in audit.probe_dispositions}
    support: list[list[tuple[float, float, float, float]]] = [
        [] for _region in audit.final_regions
    ]
    for probe_id, disposition in dispositions.items():
        probe = probe_by_id[probe_id]
        if disposition.outcome != "covered_visual" or probe.kind != "component":
            continue
        for index in disposition.final_region_indices:
            support[index].append(probe.box)
    for index, region in enumerate(audit.final_regions):
        for label in region.exact_labels:
            support[index].append(label.page_box)
            if label.page_anchor is not None:
                x, y = label.page_anchor
                support[index].append((x, y, x, y))

    margin = float(config.AUDIT_SUPPORT_OUTWARD_MARGIN_PCT)
    normalized = list(audit.final_regions)
    local_pages = {region.local_page for region in normalized}
    rendered_pages = _rendered_pages(
        chunk,
        budget,
        memory,
        local_pages=local_pages,
    )
    dropped: set[int] = set()
    dropped_source_ids: set[str] = set()
    try:
        for local_page, image in rendered_pages:
            for index, region in enumerate(normalized):
                if region.local_page != local_page:
                    continue
                budget.check_deadline()
                if _fallback_box_is_viable(image, region.box, budget, memory):
                    continue
                union = _support_union(support[index])
                expanded = (
                    max(0.0, union[0] - margin),
                    max(0.0, union[1] - margin),
                    min(100.0, union[2] + margin),
                    min(100.0, union[3] + margin),
                )
                if expanded == region.box or not _fallback_box_is_viable(
                    image, expanded, budget, memory
                ):
                    # Even the maximum verified bounds cannot yield a safe
                    # fallback crop, so this claim (typically a full-page or
                    # blank-area over-claim) can never become an asset; drop
                    # it instead of failing the whole job.
                    _log_fallback_bounds_failure(
                        image, region, expanded, len(support[index])
                    )
                    dropped.add(index)
                    dropped_source_ids.update(region.source_region_ids)
                    continue
                normalized[index] = region.model_copy(update={"box": expanded})
    finally:
        rendered_pages.close()
    if not dropped:
        return audit.model_copy(update={"final_regions": tuple(normalized)}), frozenset()
    position_of = {}
    kept: list[AuditedRegion] = []
    for index, region in enumerate(normalized):
        if index in dropped:
            continue
        position_of[index] = len(kept)
        kept.append(region)
    normalized_dispositions = []
    for disposition in audit.probe_dispositions:
        mapped = tuple(
            sorted(
                {
                    position_of[original]
                    for original in disposition.final_region_indices
                    if original in position_of
                }
            )
        )
        outcome = disposition.outcome
        if outcome == "covered_visual" and not mapped:
            outcome = "ordinary_text"
        normalized_dispositions.append(
            disposition.model_copy(
                update={
                    "outcome": outcome,
                    "final_region_indices": mapped,
                }
            )
        )
    print(
        "[notesbot.audit-normalize] "
        f"dropped_nonviable_regions={len(dropped)} kept_regions={len(kept)}"
    )
    return audit.model_copy(
        update={
            "final_regions": tuple(kept),
            "probe_dispositions": tuple(normalized_dispositions),
        }
    ), frozenset(dropped_source_ids)


_AUDIT_FAILURE_CATEGORIES = frozenset(
    {
        "header",
        "region_mapping",
        "disposition_entry",
        "disposition_set",
        "mandatory_probe",
        "probe_overlap",
        "region_support",
        "region_bounds",
        "duplicate_region",
    }
)


def _audit_fail(
    category: str,
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> None:
    """Emit only fixed audit telemetry; never source/model response content."""
    if category not in _AUDIT_FAILURE_CATEGORIES:
        category = "header"
    unresolved = (
        audit.unresolved_count
        if isinstance(audit.unresolved_count, int)
        and not isinstance(audit.unresolved_count, bool)
        else -1
    )
    print(
        f"[notesbot.audit-error] category={category}"
        f" provisional={len(provisional_ids)}"
        f" final={len(audit.final_regions)}"
        f" probes={len(probes)}"
        f" dispositions={len(audit.probe_dispositions)}"
        f" complete={int(audit.coverage_complete is True)}"
        f" unresolved={unresolved}"
    )
    _fail()


def _labels_prefix_compatible(
    first: tuple[InventoryLabel, ...], second: tuple[InventoryLabel, ...]
) -> bool:
    shorter, longer = sorted((first, second), key=len)
    return longer[: len(shorter)] == shorter


def _merge_duplicate_inventory_regions(
    inventory: InventoryResponse,
) -> InventoryResponse:
    """Merge near-identical same-kind inventory regions before ids are minted.

    Models sometimes list one visual twice with essentially the same box; the
    audit then has no valid way to keep both (duplicate_region fails closed).
    Merging at the inventory boundary keeps every downstream invariant intact:
    the union box still covers both claims, labels are unioned, and the first
    region's anchor/context stay authoritative.
    """
    regions = list(inventory.regions)
    merged_count = 0
    changed = True
    while changed:
        changed = False
        for a_pos in range(len(regions)):
            for b_pos in range(a_pos + 1, len(regions)):
                first, second = regions[a_pos], regions[b_pos]
                if (
                    first.local_page != second.local_page
                    or first.kind != second.kind
                    or not duplicate_merge_allowed(first.box, second.box)
                ):
                    continue
                union_box = (
                    min(first.box[0], second.box[0]),
                    min(first.box[1], second.box[1]),
                    max(first.box[2], second.box[2]),
                    max(first.box[3], second.box[3]),
                )
                seen_labels = set()
                labels = []
                for label in first.exact_labels + second.exact_labels:
                    key = (label.text, tuple(label.page_box))
                    if key not in seen_labels:
                        seen_labels.add(key)
                        labels.append(label)
                regions[a_pos] = first.model_copy(
                    update={
                        "box": union_box,
                        "exact_labels": tuple(labels),
                        "section_hint": first.section_hint or second.section_hint,
                        "source_order_hint": min(
                            first.source_order_hint, second.source_order_hint
                        ),
                    }
                )
                del regions[b_pos]
                merged_count += 1
                changed = True
                break
            if changed:
                break
    if not merged_count:
        return inventory
    print(
        "[notesbot.audit-normalize] "
        f"merged_inventory_regions={len(inventory.regions)}->{len(regions)}"
    )
    return inventory.model_copy(update={"regions": tuple(regions)})


def _merge_duplicate_final_regions(
    audit: InventoryAuditResponse,
) -> InventoryAuditResponse:
    """Deterministically merge near-identical final regions the audit left split.

    duplicate_merge_allowed (IoU >= 0.90, containment >= 0.96) marks pairs the
    model was expected to merge; when their metadata is fully compatible the
    merge is unambiguous, so performing it here keeps an otherwise-valid audit
    alive instead of failing the whole job.  Incompatible overlaps still reach
    validate_inventory_audit and fail closed exactly as before.
    """
    groups: list[tuple[list[int], AuditedRegion]] = [
        ([index], region) for index, region in enumerate(audit.final_regions)
    ]
    merged_any = False
    changed = True
    while changed:
        changed = False
        for a_pos in range(len(groups)):
            for b_pos in range(a_pos + 1, len(groups)):
                first, second = groups[a_pos][1], groups[b_pos][1]
                if (
                    first.local_page != second.local_page
                    or not duplicate_merge_allowed(first.box, second.box)
                    or first.kind != second.kind
                    or first.topic_anchor != second.topic_anchor
                    or first.context_regions != second.context_regions
                    or not _labels_prefix_compatible(
                        first.exact_labels, second.exact_labels
                    )
                ):
                    continue
                union_box = (
                    min(first.box[0], second.box[0]),
                    min(first.box[1], second.box[1]),
                    max(first.box[2], second.box[2]),
                    max(first.box[3], second.box[3]),
                )
                labels = max(
                    (first.exact_labels, second.exact_labels), key=len
                )
                source_ids = tuple(first.source_region_ids) + tuple(
                    source_id
                    for source_id in second.source_region_ids
                    if source_id not in first.source_region_ids
                )
                merged = first.model_copy(
                    update={
                        "box": union_box,
                        "exact_labels": labels,
                        "source_region_ids": source_ids,
                        "section_hint": first.section_hint or second.section_hint,
                        "source_order_hint": min(
                            first.source_order_hint, second.source_order_hint
                        ),
                    }
                )
                merged_indices = groups[a_pos][0] + groups[b_pos][0]
                groups[a_pos] = (merged_indices, merged)
                del groups[b_pos]
                merged_any = True
                changed = True
                break
            if changed:
                break
    if not merged_any:
        return audit
    position_of: dict[int, int] = {}
    for position, (indices, _region) in enumerate(groups):
        for original in indices:
            position_of[original] = position
    dispositions = tuple(
        disposition.model_copy(
            update={
                "final_region_indices": tuple(
                    sorted(
                        {
                            position_of[original]
                            for original in disposition.final_region_indices
                            if original in position_of
                        }
                    )
                )
            }
        )
        for disposition in audit.probe_dispositions
    )
    print(
        "[notesbot.audit-normalize] "
        f"merged_final_regions={len(audit.final_regions)}->{len(groups)}"
    )
    return audit.model_copy(
        update={
            "final_regions": tuple(region for _indices, region in groups),
            "probe_dispositions": dispositions,
        }
    )


def validate_inventory_audit(
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
    audit: InventoryAuditResponse,
) -> None:
    """Fail closed unless audit mapping, support, and probe closure are exact."""
    if (
        audit.pages_checked != inventory.pages_checked
        or not audit.coverage_complete
        or audit.unresolved_count != 0
        or len(provisional_ids) != len(inventory.regions)
        or len(set(provisional_ids)) != len(provisional_ids)
    ):
        _audit_fail("header", inventory, provisional_ids, probes, audit)
    initial = dict(zip(provisional_ids, inventory.regions))
    mapped: dict[str, int] = {}
    for final_index, region in enumerate(audit.final_regions):
        if not 1 <= region.local_page <= len(inventory.pages_checked):
            _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)
        if len(set(region.source_region_ids)) != len(region.source_region_ids):
            _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)
        mapped_records: list[VisualRegion] = []
        for source_id in region.source_region_ids:
            if source_id not in initial or source_id in mapped:
                _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)
            mapped[source_id] = final_index
            mapped_records.append(initial[source_id])
        for source in mapped_records:
            if (
                region.local_page != source.local_page
                or region.kind != source.kind
                or region.topic_anchor != source.topic_anchor
                or region.context_regions != source.context_regions
                or len(region.exact_labels) < len(source.exact_labels)
                or region.exact_labels[: len(source.exact_labels)] != source.exact_labels
            ):
                _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)
        if len(mapped_records) > 1:
            for baseline, other in itertools.combinations(mapped_records, 2):
                if (
                    other.local_page != baseline.local_page
                    or other.kind != baseline.kind
                    or other.exact_labels != baseline.exact_labels
                    or other.context_regions != baseline.context_regions
                    or not duplicate_merge_allowed(baseline.box, other.box)
                ):
                    _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)
    if set(mapped) != set(provisional_ids):
        _audit_fail("region_mapping", inventory, provisional_ids, probes, audit)

    probe_by_id = {probe.probe_id: probe for probe in probes}
    dispositions: dict[str, ProbeDisposition] = {}
    for disposition in audit.probe_dispositions:
        if disposition.probe_id in dispositions or disposition.probe_id not in probe_by_id:
            _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        if disposition.outcome == "unresolved":
            _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        if len(set(disposition.final_region_indices)) != len(disposition.final_region_indices):
            _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        if disposition.outcome == "covered_visual":
            if not disposition.final_region_indices:
                _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        elif disposition.final_region_indices:
            _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        for index in disposition.final_region_indices:
            if index >= len(audit.final_regions):
                _audit_fail("disposition_entry", inventory, provisional_ids, probes, audit)
        dispositions[disposition.probe_id] = disposition
    if set(dispositions) != set(probe_by_id):
        _audit_fail("disposition_set", inventory, provisional_ids, probes, audit)

    for probe in probes:
        disposition = dispositions[probe.probe_id]
        for provisional_id, source in initial.items():
            if not _mandatory_probe(probe, source):
                continue
            if (
                disposition.outcome != "covered_visual"
                or mapped[provisional_id] not in disposition.final_region_indices
            ):
                _audit_fail("mandatory_probe", inventory, provisional_ids, probes, audit)
        if disposition.outcome == "covered_visual":
            for index in disposition.final_region_indices:
                region = audit.final_regions[index]
                if probe.local_page != region.local_page or not _overlaps(probe.box, region.box):
                    _audit_fail("probe_overlap", inventory, provisional_ids, probes, audit)

    epsilon = float(config.AUDIT_SUPPORT_CONTAINMENT_EPSILON_PCT)
    margin = float(config.AUDIT_SUPPORT_OUTWARD_MARGIN_PCT)
    for final_index, region in enumerate(audit.final_regions):
        support: list[tuple[float, float, float, float]] = []
        for probe in probes:
            disposition = dispositions[probe.probe_id]
            if (
                probe.kind == "component"
                and disposition.outcome == "covered_visual"
                and final_index in disposition.final_region_indices
            ):
                support.append(probe.box)
        if not support:
            _audit_fail("region_support", inventory, provisional_ids, probes, audit)
        mapped_labels: tuple[InventoryLabel, ...] = ()
        if region.source_region_ids:
            mapped_labels = initial[region.source_region_ids[0]].exact_labels
        for label in region.exact_labels:
            support.append(label.page_box)
            if label.page_anchor is not None:
                x, y = label.page_anchor
                support.append((x, y, x, y))
        for added in region.exact_labels[len(mapped_labels) :]:
            if not any(
                probe.kind == "component"
                and probe.local_page == region.local_page
                and _overlaps(probe.box, added.page_box)
                for probe in probes
            ):
                _audit_fail("region_support", inventory, provisional_ids, probes, audit)
        union = _support_union(support)
        if not _contains_box(region.box, union, epsilon):
            _audit_fail("region_bounds", inventory, provisional_ids, probes, audit)
        if (
            region.box[0] < union[0] - margin
            or region.box[1] < union[1] - margin
            or region.box[2] > union[2] + margin
            or region.box[3] > union[3] + margin
        ):
            _audit_fail("region_bounds", inventory, provisional_ids, probes, audit)
    tolerated_convergent = 0
    for index, first in enumerate(audit.final_regions):
        for second in audit.final_regions[index + 1 :]:
            if first.local_page == second.local_page and duplicate_merge_allowed(first.box, second.box):
                if first.source_region_ids and second.source_region_ids:
                    # Distinct provisional regions whose audited boxes
                    # converged after normalization: each keeps a clean 1:1
                    # source mapping, so this is legitimate overlap (even when
                    # the model wobbled on kind), not a hallucinated duplicate
                    # entry.  A final region without sources of its own is the
                    # true duplicate signature and still fails below.
                    tolerated_convergent += 1
                    continue
                print(
                    "[notesbot.audit-normalize] duplicate_region_detail"
                    f" kind_eq={int(first.kind == second.kind)}"
                    f" first_sources={len(first.source_region_ids)}"
                    f" second_sources={len(second.source_region_ids)}"
                )
                _audit_fail("duplicate_region", inventory, provisional_ids, probes, audit)
    if tolerated_convergent:
        print(
            "[notesbot.audit-normalize] "
            f"tolerated_convergent_duplicates={tolerated_convergent}"
        )


def _audit_payload(
    inventory: InventoryResponse,
    provisional_ids: Sequence[str],
    probes: Sequence[CoverageProbe],
) -> dict[str, object]:
    return {
        "pages_checked": list(inventory.pages_checked),
        "provisional_regions": [
            {"provisional_id": provisional_id, "region": region.model_dump(mode="json")}
            for provisional_id, region in zip(provisional_ids, inventory.regions)
        ],
        "probes": [probe.model_dump(mode="json") for probe in probes],
    }


def decode_audit_payload(prompt: str) -> dict[str, object]:
    """Decode the canonical runtime request section (useful for injected seams)."""
    try:
        raw = prompt.split(_AUDIT_PAYLOAD_MARKER, 1)[1]
        payload = json.loads(raw)
    except Exception:
        _fail()
    if not isinstance(payload, dict):
        _fail()
    return payload


def inventory_chunk(
    chunk: PdfChunk,
    call_schema: SchemaCall,
    budget: VisualRunBudget,
    mathpix_evidence: MathpixEvidence | None = None,
    *,
    memory_budget: VisualMemoryBudget | None = None,
) -> AuditedChunk:
    """Return one runtime-bound audited envelope after exactly two discovery calls."""
    memory = memory_budget or _default_memory_budget()
    source_reservation = None
    evidence_reservation = None
    overlay_reservation = None
    budget.check_deadline()
    source = ModelFile("source-pages.pdf", "application/pdf", chunk.pdf_bytes)
    if (
        len(chunk.pdf_bytes) > config.VISUAL_SOURCE_CHUNK_MAX_BYTES
        or len(chunk.pdf_bytes) > config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
        or _encoded_size(len(chunk.pdf_bytes)) > config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
    ):
        _fail("source_chunk_too_large")
    try:
        source_reservation = memory.reserve_buffer("source", chunk.pdf_bytes)
        inventory_files = [source]
        evidence_attached = False
        if mathpix_evidence is not None:
            try:
                evidence_bytes = serialize_mathpix_evidence(mathpix_evidence, chunk)
            except ValueError as exc:
                if str(exc) != "mathpix_evidence_cap":
                    _fail()
            else:
                raw_total = len(chunk.pdf_bytes) + len(evidence_bytes)
                encoded_total = _encoded_size(len(chunk.pdf_bytes)) + _encoded_size(
                    len(evidence_bytes)
                )
                if (
                    raw_total <= config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
                    and encoded_total
                    <= config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
                ):
                    try:
                        evidence_reservation = memory.reserve_buffer(
                            "source", evidence_bytes
                        )
                    except VisualFidelityError as exc:
                        if exc.code != "memory_limit_exceeded":
                            raise
                    else:
                        inventory_files.append(
                            ModelFile(
                                "mathpix-source-evidence.json",
                                "application/json",
                                evidence_bytes,
                            )
                        )
                        evidence_attached = True

        inventory_timeout = _model_timeout(budget)
        budget.take_inventory_with_mathpix_outcome(
            chunk.index, used=evidence_attached
        )
        inventory = _model_response(
            call_schema,
            inventory_files,
            INVENTORY_PROMPT,
            InventoryResponse,
            chunk,
            budget,
            inventory_timeout,
        )
        budget.check_deadline()
        _release_buffer(memory, evidence_reservation)
        evidence_reservation = None
        inventory_files = [source]
        assert isinstance(inventory, InventoryResponse)
        _validate_inventory(inventory, chunk)
        inventory = _merge_duplicate_inventory_regions(inventory)
        provisional_ids = tuple(
            f"draft-c{chunk.index:02d}-{ordinal:03d}"
            for ordinal in range(1, len(inventory.regions) + 1)
        )
        probes, overlay, overlay_reservation = _process_overlay_pages(
            chunk,
            inventory,
            provisional_ids,
            budget,
            memory,
            supplied_probes=None,
        )
        payload = json.dumps(
            _audit_payload(inventory, provisional_ids, probes),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        audit_prompt = AUDIT_PROMPT + _AUDIT_PAYLOAD_MARKER + payload
        audit_timeout = _model_timeout(budget)
        budget.take_audit(chunk.index)
        audit = _model_response(
            call_schema,
            [source, ModelFile("coverage-audit-overlay.pdf", "application/pdf", overlay)],
            audit_prompt,
            InventoryAuditResponse,
            chunk,
            budget,
            audit_timeout,
        )
        budget.check_deadline()
        assert isinstance(audit, InventoryAuditResponse)
        audit = _normalize_audit_region_bounds(probes, audit)
        audit = _merge_duplicate_final_regions(audit)
        validate_inventory_audit(inventory, provisional_ids, probes, audit)
        audit, dropped_source_ids = _expand_unsafe_fallback_bounds(
            chunk,
            probes,
            audit,
            budget,
            memory,
        )
        if dropped_source_ids:
            # Nonviable claims were removed; prune their provisional twins so
            # binding stays 1:1.  Full re-validation is intentionally skipped:
            # its mapping-completeness invariant no longer applies, while
            # bind_audited_chunk still enforces the structural checks.
            kept_pairs = [
                (pid, region)
                for pid, region in zip(provisional_ids, inventory.regions)
                if pid not in dropped_source_ids
            ]
            provisional_ids = tuple(pid for pid, _region in kept_pairs)
            inventory = inventory.model_copy(
                update={"regions": tuple(region for _pid, region in kept_pairs)}
            )
        else:
            validate_inventory_audit(inventory, provisional_ids, probes, audit)
        return bind_audited_chunk(
            chunk, inventory, provisional_ids, probes, audit
        )
    finally:
        _release_buffer(memory, evidence_reservation)
        _release_buffer(memory, overlay_reservation)
        _release_buffer(memory, source_reservation)


def _canonical_decimal(value: float) -> str:
    decimal = Decimal(str(value))
    if decimal.is_zero():
        return "0"
    rendered = format(decimal.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _topic_key(chunk_index: int, anchor: object) -> str:
    box = ",".join(_canonical_decimal(value) for value in anchor.box)
    heading_hash = hashlib.sha256(
        anchor.exact_heading.encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"topic-{chunk_index:02d}-p{anchor.local_page:04d}-"
        f"b{box}-{heading_hash}"
    )


def _convert_coordinate(value: float, start: float, end: float) -> float:
    converted = 100.0 * (value - start) / (end - start)
    if not math.isfinite(converted) or not -1e-9 <= converted <= 100.0 + 1e-9:
        _fail()
    converted = min(100.0, max(0.0, converted))
    reprojected = start + converted * (end - start) / 100.0
    if abs(reprojected - value) > 1e-6:
        _fail()
    return converted


def _convert_label(
    label: InventoryLabel, visual_box: tuple[float, float, float, float]
) -> SourceLabel:
    if not _contains_box(visual_box, label.page_box):
        _fail()
    x0, y0, x1, y1 = visual_box
    box = (
        _convert_coordinate(label.page_box[0], x0, x1),
        _convert_coordinate(label.page_box[1], y0, y1),
        _convert_coordinate(label.page_box[2], x0, x1),
        _convert_coordinate(label.page_box[3], y0, y1),
    )
    anchor = None
    if label.page_anchor is not None:
        px, py = label.page_anchor
        if not (x0 <= px <= x1 and y0 <= py <= y1):
            _fail()
        anchor = (_convert_coordinate(px, x0, x1), _convert_coordinate(py, y0, y1))
    return SourceLabel(text=label.text, box=box, anchor=anchor)


def reading_bands(
    boxes: Sequence[tuple[float, float, float, float]],
) -> tuple[tuple[int, ...], ...]:
    unassigned = set(range(len(boxes)))
    bands: list[tuple[int, ...]] = []
    while unassigned:
        seed = min(unassigned, key=lambda index: (*((boxes[index][1], boxes[index][0], boxes[index][3], boxes[index][2])), index))
        seed_box = boxes[seed]
        band = []
        for index in unassigned:
            box = boxes[index]
            overlap = max(0.0, min(seed_box[3], box[3]) - max(seed_box[1], box[1]))
            smaller = min(seed_box[3] - seed_box[1], box[3] - box[1])
            if overlap >= 0.50 * smaller:
                band.append(index)
        ordered = tuple(sorted(band, key=lambda index: (boxes[index][0], boxes[index][1], boxes[index][2], boxes[index][3], index)))
        bands.append(ordered)
        unassigned.difference_update(band)
    return tuple(bands)


def geometry_reading_order(
    boxes: Sequence[tuple[float, float, float, float]],
) -> tuple[int, ...]:
    return tuple(index for band in reading_bands(boxes) for index in band)


def _validate_expected_chunks(expected: Sequence[PdfChunk], total_pages: int) -> tuple[int, ...]:
    if (
        isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total_pages <= 0
        or total_pages > config.MAX_SOURCE_PAGES
    ):
        _fail("too_many_pages" if isinstance(total_pages, int) and total_pages > config.MAX_SOURCE_PAGES else "inventory_incomplete")
    indices = tuple(chunk.index for chunk in expected)
    if indices != tuple(range(len(expected))) or not expected:
        _fail()
    next_page = 1
    for chunk in expected:
        if (
            chunk.first_global_page != next_page
            or chunk.page_count <= 0
            or len(chunk.page_geometries) != chunk.page_count
            or tuple(item.global_page for item in chunk.page_geometries)
            != tuple(range(chunk.first_global_page, chunk.first_global_page + chunk.page_count))
        ):
            _fail()
        next_page += chunk.page_count
    if next_page - 1 != total_pages:
        _fail()
    return indices


def _validate_ledger(indices: tuple[int, ...], budget: VisualRunBudget) -> None:
    expected = set(indices)
    for ledger in (budget.inventory_calls, budget.audit_calls):
        if set(ledger) != expected or any(value != 1 for value in ledger.values()):
            _fail()
    used, fallback = set(budget.mathpix_evidence_used), set(budget.mathpix_source_fallback)
    if used & fallback or used | fallback != expected:
        _fail()
    if any(value != 1 for value in budget.mathpix_evidence_used.values()) or any(
        value != 1 for value in budget.mathpix_source_fallback.values()
    ):
        _fail()


def finalize_manifest(
    audited_chunks: Sequence[AuditedChunk],
    expected_chunks: Sequence[PdfChunk],
    budget: VisualRunBudget,
    *,
    total_pages: int,
    max_visuals: int,
) -> VisualManifest:
    """Validate bound chunk audits and assign immutable global manifest records."""
    if (
        isinstance(max_visuals, bool)
        or not isinstance(max_visuals, int)
        or max_visuals <= 0
    ):
        _fail()
    expected_indices = _validate_expected_chunks(expected_chunks, total_pages)
    if tuple(envelope.chunk_index for envelope in audited_chunks) != expected_indices:
        _fail()
    _validate_ledger(expected_indices, budget)
    records: list[tuple[int, int, AuditedRegion, SourceTopicAnchor, tuple[SourceContext, ...]]] = []
    anchor_maps: dict[int, dict[str, tuple[int, tuple[float, float, float, float], str]]] = {}
    audit_index = 0
    for chunk, envelope in zip(expected_chunks, audited_chunks):
        inventory, provisional_ids, probes, audit = _validated_closure(
            chunk,
            envelope.inventory,
            envelope.provisional_ids,
            envelope.probes,
            envelope.audit,
        )
        if (
            envelope.chunk_index != chunk.index
            or envelope.chunk_sha256 != chunk.sha256_hex
            or envelope.first_global_page != chunk.first_global_page
            or envelope.page_count != chunk.page_count
            or envelope.audit_binding_sha256 != _audit_binding(chunk, audit)
            or envelope.closure_binding_sha256
            != _closure_binding(
                chunk, inventory, provisional_ids, probes, audit
            )
        ):
            _fail()
        if (
            audit.pages_checked != _exact_pages(chunk.page_count)
            or not audit.coverage_complete
            or audit.unresolved_count != 0
            or any(item.outcome == "unresolved" for item in audit.probe_dispositions)
        ):
            _fail()
        anchors = anchor_maps.setdefault(chunk.index, {})
        for region in audit.final_regions:
            if not 1 <= region.local_page <= chunk.page_count:
                _fail()
            anchor = region.topic_anchor
            if not 1 <= anchor.local_page <= chunk.page_count:
                _fail()
            source_tuple = (anchor.local_page, anchor.box, anchor.exact_heading)
            if anchor.anchor_ref in anchors and anchors[anchor.anchor_ref] != source_tuple:
                _fail()
            anchors[anchor.anchor_ref] = source_tuple
            key = _topic_key(chunk.index, anchor)
            final_anchor = SourceTopicAnchor(
                topic_key=key,
                local_page=anchor.local_page,
                box=anchor.box,
                exact_heading=anchor.exact_heading,
            )
            contexts: list[SourceContext] = []
            for context in region.context_regions:
                if (
                    context.anchor_ref != anchor.anchor_ref
                    or not 1 <= context.local_page <= chunk.page_count
                    or abs(context.local_page - region.local_page) > 1
                ):
                    _fail()
                contexts.append(
                    SourceContext(
                        topic_key=key,
                        local_page=context.local_page,
                        box=context.box,
                        role=context.role,
                        exact_text=(
                            context.exact_text
                            if len(context.exact_text) <= config.MAX_CONTEXT_TEXT_CHARS
                            else ""
                        ),
                    )
                )
            records.append((audit_index, chunk.index, region, final_anchor, tuple(contexts)))
            audit_index += 1
    hard_max = min(64, int(config.MAX_SOURCE_VISUALS), max_visuals)
    if len(records) > hard_max:
        _fail("too_many_visuals")
    for index, first in enumerate(records):
        first_region = first[2]
        first_page = expected_chunks[first[1]].first_global_page + first_region.local_page - 1
        for second in records[index + 1 :]:
            second_region = second[2]
            second_page = expected_chunks[second[1]].first_global_page + second_region.local_page - 1
            if first_page == second_page and duplicate_merge_allowed(first_region.box, second_region.box):
                _fail()

    ordered: list[tuple[int, int, AuditedRegion, SourceTopicAnchor, tuple[SourceContext, ...]]] = []
    for global_page in range(1, total_pages + 1):
        on_page = [
            record
            for record in records
            if expected_chunks[record[1]].first_global_page + record[2].local_page - 1 == global_page
        ]
        boxes = tuple(record[2].box for record in on_page)
        ordered.extend(on_page[index] for index in geometry_reading_order(boxes))

    visuals: list[SourceVisual] = []
    for order, (_, chunk_index, region, topic_anchor, contexts) in enumerate(ordered, start=1):
        chunk = expected_chunks[chunk_index]
        labels = tuple(_convert_label(label, region.box) for label in region.exact_labels)
        visuals.append(
            SourceVisual(
                visual_id=f"sv-{order:04d}",
                chunk_index=chunk_index,
                local_page=region.local_page,
                global_page=chunk.first_global_page + region.local_page - 1,
                box=region.box,
                kind=region.kind,
                topic_anchor=topic_anchor,
                exact_labels=labels,
                context_regions=contexts,
                section_hint=region.section_hint,
                source_order=order,
            )
        )
    manifest = VisualManifest(visuals=tuple(visuals))
    validate_manifest(manifest, total_pages=total_pages, max_visuals=hard_max)
    return manifest
