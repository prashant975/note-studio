"""Bounded, non-flattening PDF page-tree preflight regressions."""
from __future__ import annotations

import io

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from notesbot import pdf_utils
from notesbot.errors import UserError, VisualFidelityError


def _marked_two_page_pdf() -> bytes:
    pdf = FPDF()
    for marker in ("FIRST_PAGE_MARKER", "SECOND_PAGE_MARKER"):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, marker)
    return bytes(pdf.output())


def test_extract_chunk_page_returns_only_the_requested_source_page() -> None:
    chunk = pdf_utils.split_pdf_chunks(_marked_two_page_pdf())[0]

    extracted = pdf_utils.extract_chunk_page(chunk, 2)

    reader = PdfReader(io.BytesIO(extracted))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text() or ""
    assert "SECOND_PAGE_MARKER" in text
    assert "FIRST_PAGE_MARKER" not in text


@pytest.mark.parametrize("local_page", [0, -1, 3, True, "2"])
def test_extract_chunk_page_rejects_invalid_local_page(local_page) -> None:
    chunk = pdf_utils.split_pdf_chunks(_marked_two_page_pdf())[0]

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.extract_chunk_page(chunk, local_page)

    assert caught.value.code == "link_invalid"


def _raw_pdf(objects: list[bytes]) -> bytes:
    parts = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    size = len(parts[0])
    for number, body in enumerate(objects, start=1):
        offsets.append(size)
        item = f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        parts.append(item)
        size += len(item)
    xref_at = size
    xref = [f"xref\n0 {len(objects) + 1}\n".encode(), b"0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return b"".join([*parts, *xref, trailer])


def _flat_page_tree(page_count: int, *, declared_count: int | None = None) -> bytes:
    kids = b" ".join(f"{number} 0 R".encode() for number in range(3, page_count + 3))
    declared = page_count if declared_count is None else declared_count
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count "
        + str(declared).encode()
        + b" /Kids ["
        + kids
        + b"] >>",
    ]
    objects.extend(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"
        for _ in range(page_count)
    )
    return _raw_pdf(objects)


def _repeated_page_reference_pdf() -> bytes:
    return _raw_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 2 /Kids [3 0 R 3 0 R] >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        ]
    )


def _cyclic_page_tree_pdf() -> bytes:
    return _raw_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
            b"<< /Type /Pages /Count 1 /Kids [2 0 R] >>",
        ]
    )


def _indirect_page_tree_values_pdf() -> bytes:
    return _raw_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type 3 0 R /Count 4 0 R /Kids 5 0 R >>",
            b"/Pages",
            b"1",
            b"[6 0 R]",
            b"<< /Type 7 0 R /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            b"/Page",
        ]
    )


def _deep_page_tree_pdf(depth: int) -> bytes:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    leaf_number = depth + 2
    for offset in range(depth):
        number = offset + 2
        child = number + 1 if offset + 1 < depth else leaf_number
        objects.append(
            b"<< /Type /Pages /Count 1 /Kids ["
            + f"{child} 0 R".encode()
            + b"] >>"
        )
    parent = leaf_number - 1
    objects.append(
        b"<< /Type /Page /Parent "
        + f"{parent} 0 R".encode()
        + b" /MediaBox [0 0 100 100] >>"
    )
    return _raw_pdf(objects)


def test_repeated_page_reference_is_rejected_before_pypdf_flatten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = PdfReader(io.BytesIO(_repeated_page_reference_pdf()))
    page_property = PdfReader.pages
    page_accesses = 0

    def observed_pages(instance):
        nonlocal page_accesses
        page_accesses += 1
        return page_property.fget(instance)

    monkeypatch.setattr(PdfReader, "pages", property(observed_pages))

    with pytest.raises(UserError):
        pdf_utils._bounded_page_count(reader, None)

    assert page_accesses == 0


@pytest.mark.parametrize(
    "pdf_bytes",
    [_cyclic_page_tree_pdf(), _deep_page_tree_pdf(129)],
    ids=["cycle", "depth"],
)
def test_invalid_page_graph_is_rejected_with_bounded_nonflattening_work(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))

    with pytest.raises(UserError):
        pdf_utils._bounded_page_count(reader, None)


def test_page_tree_exact_300_boundary_passes_without_flattening():
    reader = PdfReader(io.BytesIO(_flat_page_tree(300)))

    assert pdf_utils._bounded_page_count(reader, None) == 300
    assert reader.flattened_pages is None


def test_valid_indirect_type_count_and_kids_values_are_bounded_and_accepted():
    pdf_bytes = _indirect_page_tree_values_pdf()
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)

    assert pdf_utils._bounded_page_count(reader, None) == 1
    assert reader.flattened_pages is None

    chunks = pdf_utils.split_pdf_chunks(pdf_bytes)
    assert len(chunks) == 1
    assert chunks[0].page_count == 1
    assert len(PdfReader(io.BytesIO(chunks[0].pdf_bytes)).pages) == 1


def test_page_tree_301_stops_at_the_fixed_source_page_cap():
    # The declared count deliberately lies below the cap.  The traversal must
    # still observe and stop at the 301st unique leaf rather than trust it.
    reader = PdfReader(io.BytesIO(_flat_page_tree(301, declared_count=1)))

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils._bounded_page_count(reader, None)

    assert caught.value.code == "too_many_pages"
    assert reader.flattened_pages is None


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [("_MAX_PAGE_TREE_NODES", 1), ("_MAX_PAGE_TREE_EDGES", 0)],
)
def test_page_tree_node_and_edge_caps_are_operational(
    monkeypatch: pytest.MonkeyPatch, limit_name: str, limit: int
) -> None:
    reader = PdfReader(io.BytesIO(_flat_page_tree(1)))
    monkeypatch.setattr(pdf_utils, limit_name, limit)

    with pytest.raises(UserError):
        pdf_utils._bounded_page_count(reader, None)

    assert reader.flattened_pages is None


def test_page_tree_deadline_stops_cooperative_traversal_before_flattening():
    reader = PdfReader(io.BytesIO(_deep_page_tree_pdf(32)))

    class ExpiringBudget:
        def __init__(self):
            self.checks = 0

        def check_deadline(self):
            self.checks += 1
            if self.checks == 6:
                raise VisualFidelityError("deadline_exceeded")

    budget = ExpiringBudget()
    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils._bounded_page_count(reader, budget)  # type: ignore[arg-type]

    assert caught.value.code == "deadline_exceeded"
    assert budget.checks == 6
    assert reader.flattened_pages is None


def test_split_rejects_repeated_page_reference_before_flatten_or_raster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_accesses = 0

    def forbidden_pages(_instance):
        nonlocal page_accesses
        page_accesses += 1
        raise AssertionError("untrusted page tree was flattened")

    monkeypatch.setattr(PdfReader, "pages", property(forbidden_pages))

    with pytest.raises(UserError):
        pdf_utils.split_pdf_chunks(_repeated_page_reference_pdf())

    assert page_accesses == 0
