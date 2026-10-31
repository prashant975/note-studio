"""Split PDFs into bounded chunks while preserving original page provenance."""
from __future__ import annotations

import hashlib
import io
import math
from copy import copy
from dataclasses import dataclass
from typing import Protocol

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

from . import config
from .pdfium_guard import serialized
from .errors import UserError, VisualFidelityError


_MAX_PAGE_TREE_DEPTH = 128
_MAX_PAGE_TREE_NODES = 4_096
_MAX_PAGE_TREE_EDGES = 4_096
_MAX_PAGE_TREE_AUX_OBJECTS = 4_096
_MAX_PAGE_VALUE_INDIRECTIONS = 8
_INVALID_PDF_MESSAGE = (
    "PDF padhi nahi ja saki — file corrupt ya protected ho sakti hai."
)


class DeadlineBudget(Protocol):
    def check_deadline(self) -> None: ...


@dataclass(frozen=True)
class PdfPageGeometry:
    global_page: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    user_unit: float
    rendered_width: int
    rendered_height: int


@dataclass(frozen=True)
class PdfChunk:
    index: int
    pdf_bytes: bytes
    first_global_page: int
    page_count: int
    page_geometries: tuple[PdfPageGeometry, ...]

    @property
    def last_global_page(self) -> int:
        return self.first_global_page + self.page_count - 1

    @property
    def sha256_hex(self) -> str:
        return hashlib.sha256(self.pdf_bytes).hexdigest()


def _check_deadline(budget: DeadlineBudget | None) -> None:
    if budget is not None:
        budget.check_deadline()


def _write_pages(reader: PdfReader, start: int, end: int) -> bytes:
    writer = PdfWriter()
    for page_index in range(start, end):
        # The bounded page-tree preflight resolves and validates /Type, but
        # pypdf's writer accepts only a direct /Page value. Normalize that one
        # structural value on a shallow copy so valid indirect encodings work
        # without mutating the source reader.
        page = copy(reader.pages[page_index])
        page[NameObject("/Type")] = NameObject("/Page")
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _box_tuple(box: object) -> tuple[float, float, float, float]:
    values = tuple(float(value) for value in box)  # type: ignore[arg-type]
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise VisualFidelityError("crop_failed")
    x0, y0, x1, y1 = values
    if x1 <= x0 or y1 <= y0:
        raise VisualFidelityError("crop_failed")
    return x0, y0, x1, y1


def page_render_scale(
    width: float,
    height: float,
    *,
    render_scale: float | None = None,
) -> float:
    """The scale one page is rasterised at.

    Normally the configured scale. Some decks are exported with giant pages
    (7200 x 3600 pt slides): at the configured scale their raster is over the
    visual page limits, which used to fail the whole lecture with
    ``crop_failed`` before any model call. Such a page is rendered at the
    largest scale that still fits the limits instead. Every place that
    measures or rasterises a page must take its scale from here, so the
    recorded geometry and the rendered image always agree.
    """
    scale = float(
        config.VISUAL_PAGE_RENDER_SCALE if render_scale is None else render_scale
    )
    if not (
        math.isfinite(scale)
        and math.isfinite(width)
        and math.isfinite(height)
        and scale > 0.0
        and width > 0.0
        and height > 0.0
    ):
        return scale
    max_dimension = config.MAX_VISUAL_PAGE_DIMENSION_PX
    max_pixels = config.MAX_VISUAL_PAGE_PIXELS

    def fits(candidate: float) -> bool:
        rendered_width = math.ceil(width * candidate)
        rendered_height = math.ceil(height * candidate)
        return (
            rendered_width <= max_dimension
            and rendered_height <= max_dimension
            and rendered_width * rendered_height <= max_pixels
        )

    if fits(scale):
        return scale
    fitted = min(
        scale,
        max_dimension / width,
        max_dimension / height,
        math.sqrt(max_pixels / (width * height)),
    )
    # Rounding up to whole pixels can leave a fitted side one pixel over.
    for _ in range(8):
        if fits(fitted):
            break
        fitted *= 0.999
    return fitted


def _page_geometry(
    page: object,
    pdfium_page: object,
    global_page: int,
) -> PdfPageGeometry:
    media_box = _box_tuple(page.mediabox)  # type: ignore[attr-defined]
    crop_box = _box_tuple(page.cropbox)  # type: ignore[attr-defined]
    rotation = int(page.rotation) % 360  # type: ignore[attr-defined]
    user_unit = float(page.user_unit)  # type: ignore[attr-defined]
    if rotation not in (0, 90, 180, 270):
        raise VisualFidelityError("crop_failed")
    if not math.isfinite(user_unit) or user_unit <= 0.0:
        raise VisualFidelityError("crop_failed")

    try:
        width = float(pdfium_page.get_width())  # type: ignore[attr-defined]
        height = float(pdfium_page.get_height())  # type: ignore[attr-defined]
        scale = page_render_scale(width, height)
        rendered_width = math.ceil(width * scale)
        rendered_height = math.ceil(height * scale)
    except VisualFidelityError:
        raise
    except Exception as exc:
        raise VisualFidelityError("crop_failed") from exc

    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or rendered_width <= 0
        or rendered_height <= 0
        or rendered_width > config.MAX_VISUAL_PAGE_DIMENSION_PX
        or rendered_height > config.MAX_VISUAL_PAGE_DIMENSION_PX
        or rendered_width * rendered_height > config.MAX_VISUAL_PAGE_PIXELS
    ):
        raise VisualFidelityError("crop_failed")

    return PdfPageGeometry(
        global_page=global_page,
        media_box=media_box,
        crop_box=crop_box,
        rotation=rotation,
        user_unit=user_unit,
        rendered_width=rendered_width,
        rendered_height=rendered_height,
    )


@serialized
def _read_geometries(
    pdf_bytes: bytes,
    reader: PdfReader,
    *,
    first_global_page: int,
    budget: DeadlineBudget | None,
) -> tuple[PdfPageGeometry, ...]:
    try:
        import pypdfium2 as pdfium

        _check_deadline(budget)
        document = pdfium.PdfDocument(pdf_bytes)
        _check_deadline(budget)
    except VisualFidelityError:
        raise
    except Exception as exc:
        raise VisualFidelityError("crop_failed") from exc

    geometries: list[PdfPageGeometry] = []
    try:
        for local_index in range(len(reader.pages)):
            _check_deadline(budget)
            pdfium_page = document[local_index]
            try:
                geometry = _page_geometry(
                    reader.pages[local_index],
                    pdfium_page,
                    first_global_page + local_index,
                )
            finally:
                pdfium_page.close()
            _check_deadline(budget)
            geometries.append(geometry)
    except VisualFidelityError:
        raise
    except Exception as exc:
        raise VisualFidelityError("crop_failed") from exc
    finally:
        document.close()
    return tuple(geometries)


def _open_reader(
    pdf_bytes: bytes,
    budget: DeadlineBudget | None,
    *,
    source: bool,
) -> PdfReader:
    try:
        _check_deadline(budget)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        _check_deadline(budget)
        return reader
    except VisualFidelityError:
        raise
    except Exception as exc:
        if source:
            raise UserError(
                "PDF padhi nahi ja saki — file corrupt ya protected ho sakti hai."
            ) from exc
        raise VisualFidelityError("crop_failed") from exc


def _indirect_key(reference: object, reader: PdfReader) -> tuple[int, int]:
    if not isinstance(reference, IndirectObject) or reference.pdf is not reader:
        raise ValueError
    return int(reference.idnum), int(reference.generation)


def _bounded_page_count(
    reader: PdfReader,
    budget: DeadlineBudget | None,
) -> int:
    """Count a validated indirect page graph before pypdf can flatten it."""
    try:
        _check_deadline(budget)
        root_reference = reader.trailer.raw_get("/Root")
        _indirect_key(root_reference, reader)
        root = root_reference.get_object()
        if not isinstance(root, DictionaryObject):
            raise ValueError
        pages_reference = root.raw_get("/Pages")
        root_key = _indirect_key(pages_reference, reader)

        # Enter frames carry (reference, depth); finish frames carry the
        # parent key, declared count, and child keys for post-order checking.
        stack: list[tuple[str, object, object, object]] = [
            ("enter", pages_reference, 0, None)
        ]
        seen: set[tuple[int, int]] = set()
        computed: dict[tuple[int, int], int] = {}
        auxiliary_keys: set[tuple[int, int]] = set()
        auxiliary_cache: dict[tuple[int, int], object] = {}
        node_count = 0
        edge_count = 0
        leaf_count = 0

        def resolved_value(value: object) -> object:
            chain: list[tuple[int, int]] = []
            while isinstance(value, IndirectObject):
                _check_deadline(budget)
                key = _indirect_key(value, reader)
                if key in auxiliary_cache:
                    value = auxiliary_cache[key]
                    break
                if key in chain or len(chain) >= _MAX_PAGE_VALUE_INDIRECTIONS:
                    raise ValueError
                chain.append(key)
                auxiliary_keys.add(key)
                if len(auxiliary_keys) > _MAX_PAGE_TREE_AUX_OBJECTS:
                    raise ValueError
                value = value.get_object()
            for key in chain:
                auxiliary_cache[key] = value
            return value

        while stack:
            _check_deadline(budget)
            action, first, second, third = stack.pop()
            if action == "finish":
                key = first
                declared = second
                child_keys = third
                if (
                    not isinstance(key, tuple)
                    or not isinstance(declared, int)
                    or not isinstance(child_keys, tuple)
                ):
                    raise ValueError
                subtotal = sum(computed[child_key] for child_key in child_keys)
                if subtotal != declared:
                    raise ValueError
                computed[key] = subtotal
                continue

            reference = first
            depth = second
            if not isinstance(depth, int) or depth > _MAX_PAGE_TREE_DEPTH:
                raise ValueError
            key = _indirect_key(reference, reader)
            if key in seen:
                # A page tree cannot be cyclic or share a node.
                raise ValueError
            seen.add(key)
            node_count += 1
            if node_count > _MAX_PAGE_TREE_NODES:
                raise ValueError

            node = reference.get_object()
            if not isinstance(node, DictionaryObject):
                raise ValueError
            node_type = resolved_value(node.raw_get("/Type"))
            if node_type == "/Page":
                if "/Kids" in node:
                    raise ValueError
                leaf_count += 1
                if leaf_count > config.MAX_SOURCE_PAGES:
                    raise VisualFidelityError("too_many_pages")
                computed[key] = 1
                continue
            if node_type != "/Pages":
                raise ValueError

            declared_value = resolved_value(node.raw_get("/Count"))
            if (
                isinstance(declared_value, bool)
                or not isinstance(declared_value, int)
                or int(declared_value) < 0
            ):
                raise ValueError
            declared = int(declared_value)
            if declared > config.MAX_SOURCE_PAGES:
                raise VisualFidelityError("too_many_pages")
            kids = resolved_value(node.raw_get("/Kids"))
            if not isinstance(kids, ArrayObject):
                raise ValueError
            edge_count += len(kids)
            if edge_count > _MAX_PAGE_TREE_EDGES:
                raise ValueError
            child_keys = tuple(_indirect_key(child, reader) for child in kids)
            stack.append(("finish", key, declared, child_keys))
            for child in reversed(kids):
                stack.append(("enter", child, depth + 1, None))

        if computed.get(root_key) != leaf_count:
            raise ValueError
        _check_deadline(budget)
        return leaf_count
    except UserError:
        raise
    except Exception:
        raise UserError(_INVALID_PDF_MESSAGE) from None


def _page_count(reader: PdfReader, budget: DeadlineBudget | None) -> int:
    bounded_count = _bounded_page_count(reader, budget)
    try:
        _check_deadline(budget)
        count = len(reader.pages)
        _check_deadline(budget)
        if count != bounded_count:
            raise UserError(_INVALID_PDF_MESSAGE)
        return bounded_count
    except VisualFidelityError:
        raise
    except Exception as exc:
        raise UserError(
            "PDF padhi nahi ja saki — file corrupt ya protected ho sakti hai."
        ) from exc


def extract_chunk_page(
    chunk: PdfChunk,
    local_page: int,
    *,
    budget: DeadlineBudget | None = None,
) -> bytes:
    """Return exactly one validated page from a bounded source chunk."""
    if (
        not isinstance(chunk, PdfChunk)
        or isinstance(local_page, bool)
        or not isinstance(local_page, int)
        or local_page < 1
        or local_page > chunk.page_count
    ):
        raise VisualFidelityError("link_invalid")
    _check_deadline(budget)
    try:
        reader = _open_reader(chunk.pdf_bytes, budget, source=False)
        if _page_count(reader, budget) != chunk.page_count:
            raise VisualFidelityError("link_invalid")
        page_pdf = _write_pages(reader, local_page - 1, local_page)
        verified = _open_reader(page_pdf, budget, source=False)
        if _page_count(verified, budget) != 1:
            raise VisualFidelityError("link_invalid")
    except VisualFidelityError as error:
        if error.code == "deadline_exceeded":
            raise
        raise VisualFidelityError("link_invalid") from None
    except Exception:
        raise VisualFidelityError("link_invalid") from None
    if not page_pdf or len(page_pdf) > config.GEMINI_INLINE_MAX_BYTES:
        raise VisualFidelityError("link_invalid")
    _check_deadline(budget)
    return page_pdf


def split_pdf_chunks(
    pdf_bytes: bytes, *, budget: DeadlineBudget | None = None
) -> list[PdfChunk]:
    """Split with one-based original-page provenance."""
    reader = _open_reader(pdf_bytes, budget, source=True)
    page_count = _page_count(reader, budget)
    if page_count > config.MAX_SOURCE_PAGES:
        raise VisualFidelityError("too_many_pages")
    if page_count <= 0:
        raise UserError("PDF padhi nahi ja saki — file corrupt ya protected ho sakti hai.")

    original_geometries = _read_geometries(
        pdf_bytes,
        reader,
        first_global_page=1,
        budget=budget,
    )
    target_bytes = min(config.CHUNK_TARGET_BYTES, config.VISUAL_SOURCE_CHUNK_MAX_BYTES)
    average_page_bytes = max(len(pdf_bytes) / page_count, 1.0)
    pages_per_chunk = max(1, int(target_bytes // average_page_bytes))

    chunks: list[PdfChunk] = []
    start = 0
    while start < page_count:
        end = min(start + pages_per_chunk, page_count)
        while True:
            try:
                _check_deadline(budget)
                emitted_bytes = _write_pages(reader, start, end)
                _check_deadline(budget)
            except VisualFidelityError:
                raise
            except Exception as exc:
                raise VisualFidelityError("crop_failed") from exc

            if len(chunks) >= config.MAX_CHUNKS:
                del emitted_bytes
                raise VisualFidelityError("too_many_chunks")

            _check_deadline(budget)
            oversized = len(emitted_bytes) > config.VISUAL_SOURCE_CHUNK_MAX_BYTES
            _check_deadline(budget)
            if not oversized:
                break
            if end - start <= 1:
                raise VisualFidelityError("source_chunk_too_large")
            _check_deadline(budget)
            end = start + max(1, (end - start) // 2)
            _check_deadline(budget)

        emitted_reader = _open_reader(emitted_bytes, budget, source=False)
        emitted_count = _page_count(emitted_reader, budget)
        if emitted_count != end - start:
            raise VisualFidelityError("crop_failed")
        emitted_geometries = _read_geometries(
            emitted_bytes,
            emitted_reader,
            first_global_page=start + 1,
            budget=budget,
        )
        expected_geometries = original_geometries[start:end]
        _check_deadline(budget)
        if emitted_geometries != expected_geometries:
            raise VisualFidelityError("crop_failed")
        _check_deadline(budget)

        chunks.append(
            PdfChunk(
                index=len(chunks),
                pdf_bytes=emitted_bytes,
                first_global_page=start + 1,
                page_count=end - start,
                page_geometries=expected_geometries,
            )
        )
        start = end

    return chunks


def split_pdf(pdf_bytes: bytes) -> list[bytes]:
    return [chunk.pdf_bytes for chunk in split_pdf_chunks(pdf_bytes)]
