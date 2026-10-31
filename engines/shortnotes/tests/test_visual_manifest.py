from __future__ import annotations

import gc
import hashlib
import importlib
import io
import math
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, FloatObject, NameObject, NumberObject, RectangleObject

from notesbot import config, pdf_utils, visual_manifest
from notesbot.visual_manifest import (
    AuditedRegion,
    BufferReservation,
    CoverageProbe,
    InventoryAuditResponse,
    InventoryContext,
    InventoryLabel,
    InventoryResponse,
    InventoryTopicAnchor,
    ProbeDisposition,
    RedrawVerdict,
    RenderedVisual,
    SourceContext,
    SourceCrop,
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    VisualManifest,
    VisualMemoryBudget,
    VisualRegion,
    VisualRunBudget,
    assert_ordered_exact_once,
    manifest_ids,
    validate_manifest,
)
from notesbot.errors import VisualFidelityError


EXACT_LABELS = ["Na⁺ / Cl⁻", "λₘ°", "pH=7.00", "Anode", "anode"]


def _topic(*, key: str = "topic-1", page: int = 1) -> SourceTopicAnchor:
    return SourceTopicAnchor(
        topic_key=key,
        local_page=page,
        box=(1.0, 2.0, 30.0, 12.0),
        exact_heading=" Electrochemistry ",
    )


def _visual(
    *,
    visual_id: str = "sv-0001",
    source_order: int = 1,
    local_page: int = 1,
    global_page: int = 1,
    context_regions: tuple[SourceContext, ...] = (),
    labels: tuple[SourceLabel, ...] = (),
) -> SourceVisual:
    return SourceVisual(
        visual_id=visual_id,
        chunk_index=0,
        local_page=local_page,
        global_page=global_page,
        box=(10.0, 20.0, 80.0, 90.0),
        kind="diagram",
        topic_anchor=_topic(page=local_page),
        exact_labels=labels,
        context_regions=context_regions,
        section_hint="section",
        source_order=source_order,
    )


def _memory_budget(**overrides: int) -> VisualMemoryBudget:
    limits = {
        "max_total_bytes": 20,
        "max_source_bytes": 10,
        "max_context_bytes": 10,
        "max_asset_bytes": 10,
        "max_page_cache_bytes": 10,
    }
    limits.update(overrides)
    return VisualMemoryBudget(**limits)


def _source_pdf(
    pages: tuple[
        tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            int,
            float,
        ],
        ...,
    ],
    *,
    content_bytes: bytes | None = None,
) -> bytes:
    writer = PdfWriter()
    for media_box, crop_box, rotation, user_unit in pages:
        page = writer.add_blank_page(
            width=media_box[2] - media_box[0],
            height=media_box[3] - media_box[1],
        )
        page.mediabox = RectangleObject(media_box)
        page.cropbox = RectangleObject(crop_box)
        page[NameObject("/Rotate")] = NumberObject(rotation)
        page[NameObject("/UserUnit")] = FloatObject(user_unit)
        if content_bytes is not None:
            stream = DecodedStreamObject()
            stream.set_data(content_bytes)
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _visual_budget() -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)


def test_source_label_preserves_exact_text_and_order() -> None:
    labels = tuple(
        SourceLabel(text=text, box=(0.0, float(i), 100.0, float(i + 1)))
        for i, text in enumerate(EXACT_LABELS)
    )
    manifest = VisualManifest(visuals=(_visual(labels=labels),))

    assert tuple(label.text for label in manifest.visuals[0].exact_labels) == tuple(
        EXACT_LABELS
    )


def test_inventory_label_uses_page_coordinates_while_source_label_uses_crop_coordinates() -> None:
    inventory = InventoryLabel(
        text="Na⁺ / Cl⁻",
        page_box=(40.0, 50.0, 60.0, 70.0),
        page_anchor=(55.0, 65.0),
    )
    source = SourceLabel(
        text=inventory.text,
        box=(0.0, 0.0, 100.0, 100.0),
        anchor=(75.0, 80.0),
    )

    assert inventory.page_box == (40.0, 50.0, 60.0, 70.0)
    assert source.box == (0.0, 0.0, 100.0, 100.0)
    assert not hasattr(inventory, "box")
    assert not hasattr(source, "page_box")


def test_manifest_nested_records_are_immutable() -> None:
    label = SourceLabel(text="Anode", box=(1.0, 2.0, 3.0, 4.0))
    manifest = VisualManifest(visuals=(_visual(labels=(label,)),))

    with pytest.raises(ValidationError):
        manifest.visuals[0].exact_labels[0].text = "anode"
    with pytest.raises(ValidationError):
        manifest.visuals[0].topic_anchor.topic_key = "changed"

    crop = SourceCrop(
        visual=manifest.visuals[0], source_png=b"png", width_px=10, height_px=10
    )
    with pytest.raises(FrozenInstanceError):
        crop.width_px = 20


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("page_number", 0),
        ("page_number", -1),
        ("page_number", 1.5),
        ("x_mm", -0.01),
        ("x_mm", float("nan")),
        ("y_mm", -0.01),
        ("y_mm", float("inf")),
        ("width_mm", 0.0),
        ("width_mm", -1.0),
        ("width_mm", float("nan")),
        ("height_mm", 0.0),
        ("height_mm", -1.0),
        ("height_mm", float("-inf")),
    ),
)
def test_rendered_visual_rejects_invalid_page_position_and_dimensions(
    field_name: str, invalid_value: float
) -> None:
    values: dict[str, object] = {
        "visual_id": "sv-0001",
        "state": "verified_redraw",
        "page_number": 1,
        "x_mm": 0.0,
        "y_mm": 0.0,
        "width_mm": 10.0,
        "height_mm": 20.0,
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError):
        RenderedVisual(**values)  # type: ignore[arg-type]


def test_rendered_visual_accepts_finite_zero_origin_and_positive_rectangle() -> None:
    rendered = RenderedVisual(
        visual_id="sv-0001",
        state="sketch_source_fallback",
        page_number=1,
        x_mm=0.0,
        y_mm=0.0,
        width_mm=0.01,
        height_mm=0.01,
    )

    assert (rendered.page_number, rendered.x_mm, rendered.y_mm) == (1, 0.0, 0.0)


def test_manifest_rejects_invalid_pages_boxes_ids_and_order() -> None:
    with pytest.raises(ValidationError):
        SourceLabel(text="x", box=(0.0, 0.0, float("inf"), 2.0))
    with pytest.raises(ValidationError):
        SourceLabel(text="x", box=(50.0, 0.0, 40.0, 2.0))
    with pytest.raises(ValidationError):
        _visual(visual_id="visual-1")
    with pytest.raises(ValidationError):
        _visual(local_page=0)

    outside = VisualManifest(visuals=(_visual(global_page=3),))
    with pytest.raises(VisualFidelityError, match="placement verify"):
        validate_manifest(outside, total_pages=2)

    reordered = VisualManifest(
        visuals=(
            _visual(visual_id="sv-0002", source_order=2),
            _visual(visual_id="sv-0001", source_order=1),
        )
    )
    with pytest.raises(VisualFidelityError, match="placement verify"):
        validate_manifest(reordered, total_pages=2)
    with pytest.raises(VisualFidelityError, match="placement verify"):
        assert_ordered_exact_once(
            VisualManifest(
                visuals=(
                    _visual(visual_id="sv-0001", source_order=1),
                    _visual(visual_id="sv-0002", source_order=2),
                )
            ),
            ("sv-0002", "sv-0001"),
        )


def test_manifest_rejects_context_topic_key_mismatch() -> None:
    context = SourceContext(
        topic_key="different",
        local_page=1,
        box=(1.0, 1.0, 2.0, 2.0),
        role="legend",
        exact_text=" Anode ",
    )

    with pytest.raises(ValidationError):
        _visual(context_regions=(context,))


def test_manifest_rejects_more_than_64_visuals() -> None:
    visuals = tuple(
        _visual(
            visual_id=f"sv-{index:04d}",
            source_order=index,
            global_page=index,
        )
        for index in range(1, 66)
    )
    with pytest.raises(VisualFidelityError, match="supported limit"):
        validate_manifest(VisualManifest(visuals=visuals), total_pages=65)


def test_visual_run_budget_enforces_fixed_counts_and_deadline() -> None:
    now = [10.0]
    budget = VisualRunBudget(deadline_at=20.0, clock=lambda: now[0])

    budget.take_inventory(0)
    with pytest.raises(VisualFidelityError, match="safe call limit"):
        budget.take_inventory(0)
    budget.take_redraw("sv-0001")
    budget.take_redraw("sv-0001")
    with pytest.raises(VisualFidelityError, match="safe call limit"):
        budget.take_redraw("sv-0001")
    budget.take_mathpix_delete(0)
    now[0] = 21.0
    with pytest.raises(VisualFidelityError, match="safe time limit"):
        budget.take_audit(0)
    budget.take_mathpix_delete(1)


@pytest.mark.parametrize(
    ("deadline_at", "now"),
    (
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (float("-inf"), 1.0),
        (0.0, 0.0),
        (-1.0, 0.0),
        (-1.0, -2.0),
        (1.0, 1.0),
        (780.000001, 0.0),
        (10.0, float("nan")),
        (10.0, float("inf")),
    ),
)
def test_visual_run_budget_rejects_invalid_or_over_cap_deadline(
    deadline_at: float, now: float
) -> None:
    with pytest.raises(VisualFidelityError, match="safe time limit"):
        VisualRunBudget(deadline_at=deadline_at, clock=lambda: now)


def test_visual_run_budget_accepts_exact_780_second_deadline_cap() -> None:
    budget = VisualRunBudget(deadline_at=790.0, clock=lambda: 10.0)

    assert budget.remaining_seconds() == 780.0


@pytest.mark.parametrize(
    "invalid_limit",
    (0, -1, True, 1.5, float("nan"), float("inf"), 13, 10**9),
)
def test_visual_run_budget_rejects_invalid_or_over_cap_poll_limits(
    invalid_limit: object,
) -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)

    with pytest.raises(VisualFidelityError, match="safe call limit"):
        budget.take_mathpix_poll(7, invalid_limit)  # type: ignore[arg-type]

    assert budget.mathpix_poll_calls == {}


@pytest.mark.parametrize(
    "invalid_limit",
    (0, -1, True, 1.5, float("nan"), float("inf"), 131_073, 10**9),
)
def test_visual_run_budget_rejects_invalid_or_over_cap_probe_limits(
    invalid_limit: object,
) -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)

    with pytest.raises(VisualFidelityError, match="safe call limit"):
        budget.reserve_coverage_probes(1, invalid_limit)  # type: ignore[arg-type]

    assert budget.coverage_probes_total == 0


def test_visual_run_budget_accepts_exact_poll_and_probe_hard_caps() -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)

    for _ in range(12):
        budget.take_mathpix_poll(2, 12)
    budget.reserve_coverage_probes(131_072, 131_072)

    assert budget.mathpix_poll_calls == {2: 12}
    assert budget.coverage_probes_total == 131_072


def test_visual_run_budget_reservations_are_atomic_under_parallel_note_calls() -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)

    def take() -> str:
        try:
            budget.take_note(3)
        except VisualFidelityError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: take(), range(64)))

    assert results.count("accepted") == 1
    assert results.count("rejected") == 63
    assert budget.note_calls == {3: 1}


def test_visual_run_budget_atomically_authorizes_inventory_and_mathpix_outcome() -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)

    budget.take_inventory_with_mathpix_outcome(4, used=True)

    assert budget.inventory_calls == {4: 1}
    assert budget.mathpix_evidence_used == {4: 1}
    assert budget.mathpix_source_fallback == {}
    with pytest.raises(VisualFidelityError, match="safe call limit"):
        budget.take_inventory_with_mathpix_outcome(4, used=False)
    assert budget.inventory_calls == {4: 1}
    assert budget.mathpix_evidence_used == {4: 1}
    assert budget.mathpix_source_fallback == {}


def test_visual_run_budget_redacted_counts_are_aggregate_only() -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)
    budget.take_inventory(41)
    budget.take_note(41)
    budget.take_redraw("sv-0042")

    counts = budget.redacted_counts()

    assert ("inventory", 1) in counts
    assert ("note", 1) in counts
    assert ("redraw", 1) in counts
    assert "41" not in repr(counts)
    assert "sv-0042" not in repr(counts)


def test_visual_memory_budget_counts_unique_buffers_and_page_cache_atomically() -> None:
    budget = _memory_budget(max_total_bytes=12)
    shared = bytes(bytearray(b"1234"))
    equal_but_distinct = bytes(bytearray(b"1234"))

    first = budget.reserve_buffer("source", shared)
    assert first is not None
    assert budget.reserve_buffer("source", shared) is None
    second = budget.reserve_buffer("source", equal_but_distinct)
    assert second is not None
    budget.reserve_page("page-1", 4)
    assert budget.resident_bytes() == 12
    assert not budget.page_would_fit(1)
    with pytest.raises(VisualFidelityError, match="safe memory limit"):
        budget.reserve_page("page-2", 1)
    assert budget.resident_bytes() == 12
    assert budget.page_cache_bytes == 4


def test_visual_memory_budget_releases_only_matching_owned_reservation() -> None:
    budget = _memory_budget()
    data = bytes(bytearray(b"1234"))
    owned = budget.reserve_buffer("source", data)
    assert owned is not None
    forged = replace(owned)

    with pytest.raises(VisualFidelityError, match="safe memory limit"):
        budget.release_buffer(forged)
    assert budget.source_bytes == 4
    budget.release_buffer(owned)
    assert budget.resident_bytes() == 0
    with pytest.raises(VisualFidelityError, match="safe memory limit"):
        budget.release_buffer(owned)


def test_visual_memory_budget_borrowed_alias_cannot_release_retained_buffer() -> None:
    budget = _memory_budget()
    data = bytes(bytearray(b"1234"))
    owned = budget.reserve_buffer("asset", data)
    borrowed = budget.reserve_buffer("asset", data)

    assert owned is not None
    assert borrowed is None
    with pytest.raises(VisualFidelityError, match="safe memory limit"):
        budget.release_buffer(borrowed)  # type: ignore[arg-type]
    with pytest.raises(VisualFidelityError, match="safe memory limit"):
        budget.reserve_buffer("source", data)
    assert budget.asset_bytes == 4


def test_inventory_transport_schema_does_not_require_runtime_topic_key() -> None:
    anchor = InventoryTopicAnchor(
        anchor_ref="heading-1",
        local_page=1,
        box=(1.0, 2.0, 30.0, 12.0),
        exact_heading=" Electrochemistry ",
    )
    region = VisualRegion(
        local_page=1,
        box=(10.0, 20.0, 80.0, 90.0),
        kind="diagram",
        topic_anchor=anchor,
        context_regions=(
            InventoryContext(
                anchor_ref="heading-1",
                local_page=2,
                box=(2.0, 3.0, 40.0, 20.0),
                role="explanation",
                exact_text=" verbatim ",
            ),
        ),
        source_order_hint=1,
    )

    response = InventoryResponse(pages_checked=(1, 2), regions=(region,))

    assert response.regions[0].topic_anchor.anchor_ref == "heading-1"
    assert "topic_key" not in InventoryResponse.model_json_schema()["$defs"][
        "InventoryTopicAnchor"
    ]["properties"]
    with pytest.raises(ValidationError):
        InventoryTopicAnchor.model_validate(
            {
                "anchor_ref": "heading-1",
                "local_page": 1,
                "box": (1.0, 2.0, 30.0, 12.0),
                "topic_key": "model-must-not-author-this",
            }
        )


def test_inventory_audit_verdict_and_provider_models_forbid_unknown_or_misspelled_fields() -> None:
    label = InventoryLabel(text="Anode", page_box=(1.0, 1.0, 2.0, 2.0))
    anchor = InventoryTopicAnchor(
        anchor_ref="a", local_page=1, box=(1.0, 1.0, 2.0, 2.0)
    )
    audited = AuditedRegion(
        local_page=1,
        box=(1.0, 1.0, 20.0, 20.0),
        kind="graph",
        topic_anchor=anchor,
        exact_labels=(label,),
        source_order_hint=1,
        source_region_ids=("raw-1",),
    )
    audit = InventoryAuditResponse(
        pages_checked=(1,),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=(audited,),
        probe_dispositions=(
            ProbeDisposition(
                probe_id="p1", outcome="covered_visual", final_region_indices=(0,)
            ),
        ),
    )
    verdict = RedrawVerdict(
        faithful=True,
        candidate_text_free=True,
        label_regions_clear=True,
        anchors_aligned=True,
        geometry_aligned=True,
    )
    CoverageProbe(
        probe_id="p1",
        kind="grid",
        local_page=1,
        box=(1.0, 1.0, 2.0, 2.0),
        foreground_fraction=0.5,
    )

    for model in (InventoryResponse, InventoryAuditResponse, RedrawVerdict):
        schema = model.model_json_schema()
        objects = [schema, *schema.get("$defs", {}).values()]
        assert all(
            item.get("additionalProperties") is False
            for item in objects
            if item.get("type") == "object"
        )

    with pytest.raises(ValidationError):
        InventoryLabel.model_validate(
            {"text": "Anode", "page_box": (1.0, 1.0, 2.0, 2.0), "page_ancher": None}
        )
    with pytest.raises(ValidationError):
        InventoryAuditResponse.model_validate(
            {**audit.model_dump(), "coverage_complate": True}
        )
    with pytest.raises(ValidationError):
        RedrawVerdict.model_validate({**verdict.model_dump(), "provider_detail": "raw"})


def test_inventory_schema_exposes_percentage_corner_contract_to_model() -> None:
    schema = InventoryResponse.model_json_schema()
    expected_description = (
        "Original-page percentage box [x0, y0, x1, y1]; "
        "require 0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100. "
        "Never use pixels or [x, y, width, height]."
    )

    for definition, field in (
        ("VisualRegion", "box"),
        ("InventoryTopicAnchor", "box"),
        ("InventoryLabel", "page_box"),
        ("InventoryContext", "box"),
    ):
        box_schema = schema["$defs"][definition]["properties"][field]
        assert box_schema["description"] == expected_description
        assert box_schema["minItems"] == box_schema["maxItems"] == 4
        # items (not prefixItems): the google-genai schema transformer
        # rejects prefixItems, so the box contract rides on a uniform
        # bounded-coordinate item schema plus the exact-length bounds.
        assert "prefixItems" not in box_schema
        items = box_schema["items"]
        assert (items["minimum"], items["maximum"]) == (0.0, 100.0)


def test_audit_schema_exposes_probe_region_overlap_contract_to_model() -> None:
    schema = InventoryAuditResponse.model_json_schema()
    disposition = schema["$defs"]["ProbeDisposition"]["properties"]
    descriptions = " ".join(
        str(disposition[field].get("description", ""))
        for field in ("outcome", "final_region_indices")
    ).lower()
    assert "positive-area overlap" in descriptions
    assert "same local_page" in descriptions


def test_visual_error_never_includes_source_detail() -> None:
    for code, fixed_message in VisualFidelityError.MESSAGES.items():
        error = VisualFidelityError(code)
        assert str(error) == fixed_message
        assert error.code == code
        for source_detail in EXACT_LABELS:
            assert source_detail not in str(error)

    manifest = VisualManifest(visuals=(_visual(),))
    assert manifest_ids(manifest) == ("sv-0001",)


def test_unknown_or_duplicate_visual_reference_fails_closed() -> None:
    manifest = VisualManifest(
        visuals=(
            _visual(visual_id="sv-0001", source_order=1),
            _visual(visual_id="sv-0002", source_order=2),
        )
    )

    with pytest.raises(VisualFidelityError) as unknown:
        visual_manifest.validate_visual_references(manifest, [["sv-9999"]])
    with pytest.raises(VisualFidelityError) as duplicate:
        visual_manifest.validate_visual_references(
            manifest, [["sv-0001"], ["sv-0001"]]
        )

    assert unknown.value.code == "link_invalid"
    assert duplicate.value.code == "link_invalid"


def test_reversed_linked_section_targets_fail_closed() -> None:
    manifest = VisualManifest(
        visuals=(
            _visual(visual_id="sv-0001", source_order=1),
            _visual(visual_id="sv-0002", source_order=2),
            _visual(visual_id="sv-0003", source_order=3),
        )
    )

    with pytest.raises(VisualFidelityError) as caught:
        visual_manifest.validate_visual_references(
            manifest,
            [["sv-0002"], ["sv-0001", "sv-0003"]],
        )

    assert caught.value.code == "link_invalid"


def test_visual_reference_check_reports_unlinked_ids_in_source_order() -> None:
    manifest = VisualManifest(
        visuals=(
            _visual(visual_id="sv-0001", source_order=1),
            _visual(visual_id="sv-0002", source_order=2),
            _visual(visual_id="sv-0003", source_order=3),
        )
    )

    checked = visual_manifest.validate_visual_references(
        manifest, [[], ["sv-0002"]]
    )

    assert checked.placed_ids == ("sv-0002",)
    assert checked.unlinked_ids == ("sv-0001", "sv-0003")


def test_split_pdf_chunks_preserves_page_offsets_digest_and_original_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_pdf(
        tuple(
            ((0.0, 0.0, 100.0 + page, 200.0), (0.0, 0.0, 100.0 + page, 200.0), 0, 1.0)
            for page in range(1, 5)
        )
    )
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1)

    chunks = pdf_utils.split_pdf_chunks(source, budget=_visual_budget())

    assert [chunk.index for chunk in chunks] == [0, 1, 2, 3]
    assert [chunk.first_global_page for chunk in chunks] == [1, 2, 3, 4]
    assert [chunk.last_global_page for chunk in chunks] == [1, 2, 3, 4]
    assert [chunk.page_count for chunk in chunks] == [1, 1, 1, 1]
    assert [chunk.page_geometries[0].global_page for chunk in chunks] == [1, 2, 3, 4]
    assert [chunk.page_geometries[0].media_box[2] for chunk in chunks] == [
        101.0,
        102.0,
        103.0,
        104.0,
    ]
    assert all(
        chunk.sha256_hex == hashlib.sha256(chunk.pdf_bytes).hexdigest()
        for chunk in chunks
    )
    with pytest.raises(FrozenInstanceError):
        chunks[0].first_global_page = 99
    with pytest.raises(FrozenInstanceError):
        chunks[0].page_geometries[0].global_page = 99


def test_split_pdf_chunks_accepts_exact_configured_chunk_limit_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_pdf(
        tuple(
            (
                (0.0, 0.0, 100.0 + page, 200.0),
                (0.0, 0.0, 100.0 + page, 200.0),
                0,
                1.0,
            )
            for page in range(1, 9)
        )
    )
    monkeypatch.setattr(config, "MAX_CHUNKS", 8)
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1)

    chunks = pdf_utils.split_pdf_chunks(source, budget=_visual_budget())

    assert [chunk.index for chunk in chunks] == list(range(8))
    assert [chunk.first_global_page for chunk in chunks] == list(range(1, 9))
    assert [chunk.last_global_page for chunk in chunks] == list(range(1, 9))
    assert [chunk.page_geometries[0].global_page for chunk in chunks] == list(
        range(1, 9)
    )


def test_split_pdf_chunks_rejects_first_chunk_beyond_limit_without_retaining_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackedBytes(bytearray):
        __slots__ = ("__weakref__",)

    source = _source_pdf(
        tuple(
            ((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0)
            for _ in range(9)
        )
    )
    monkeypatch.setattr(config, "MAX_CHUNKS", 8)
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1)
    monkeypatch.setattr(config, "VISUAL_SOURCE_CHUNK_MAX_BYTES", 10_000)
    real_write_pages = pdf_utils._write_pages
    write_count = 0
    rejected_ref: weakref.ReferenceType[TrackedBytes] | None = None

    def tracked_ninth_write(*args: object, **kwargs: object) -> bytes:
        nonlocal write_count, rejected_ref
        write_count += 1
        emitted = real_write_pages(*args, **kwargs)
        if write_count == 9:
            rejected = TrackedBytes(emitted + (b"x" * 10_001))
            rejected_ref = weakref.ref(rejected)
            return rejected  # type: ignore[return-value]
        return emitted

    monkeypatch.setattr(pdf_utils, "_write_pages", tracked_ninth_write)

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.split_pdf_chunks(source, budget=_visual_budget())

    gc.collect()
    assert caught.value.code == "too_many_chunks"
    assert str(caught.value) == (
        "PDF mein supported limit se zyada processing chunks hain. "
        "Please PDF ko chhote parts mein upload karein."
    )
    assert write_count == 9
    assert rejected_ref is not None and rejected_ref() is None


def test_split_rotated_and_cropped_pages_preserves_exact_coordinate_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_pdf(
        (
            ((10.0, 20.0, 410.0, 620.0), (30.0, 40.0, 330.0, 240.0), 450, 2.0),
        )
    )
    monkeypatch.setattr(config, "VISUAL_PAGE_RENDER_SCALE", 2.0)

    chunk = pdf_utils.split_pdf_chunks(source, budget=_visual_budget())[0]
    geometry = chunk.page_geometries[0]

    assert geometry.global_page == 1
    assert geometry.media_box == (10.0, 20.0, 410.0, 620.0)
    assert geometry.crop_box == (30.0, 40.0, 330.0, 240.0)
    assert geometry.rotation == 90
    assert geometry.user_unit == 2.0
    assert (geometry.rendered_width, geometry.rendered_height) == (400, 600)
    emitted_page = PdfReader(io.BytesIO(chunk.pdf_bytes)).pages[0]
    assert tuple(float(value) for value in emitted_page.mediabox) == geometry.media_box
    assert tuple(float(value) for value in emitted_page.cropbox) == geometry.crop_box
    assert emitted_page.rotation % 360 == geometry.rotation
    assert float(emitted_page.user_unit) == geometry.user_unit


@pytest.mark.parametrize(
    ("field_name", "mismatched_value"),
    (
        ("media_box", (0.0, 0.0, 201.0, 300.0)),
        ("crop_box", (0.0, 0.0, 199.0, 300.0)),
        ("rotation", 90),
        ("user_unit", 2.0),
        ("rendered_width", 442),
    ),
)
def test_reopened_chunk_geometry_mismatch_fails_closed_before_provider_work(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    mismatched_value: object,
) -> None:
    source = _source_pdf(
        (((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),)
    )
    real_read_geometries = pdf_utils._read_geometries
    reads = [0]

    def mismatched_emitted_geometry(*args: object, **kwargs: object) -> tuple[object, ...]:
        geometries = real_read_geometries(*args, **kwargs)
        reads[0] += 1
        if reads[0] == 2:
            return (
                replace(geometries[0], **{field_name: mismatched_value}),
                *geometries[1:],
            )
        return geometries

    monkeypatch.setattr(pdf_utils, "_read_geometries", mismatched_emitted_geometry)
    budget = _visual_budget()

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.split_pdf_chunks(source, budget=budget)

    assert caught.value.code == "crop_failed"
    assert all(count == 0 for _, count in budget.redacted_counts())


@pytest.mark.parametrize(
    ("page_points", "dimension_cap", "pixel_cap"),
    (
        pytest.param(4_000.0, 5_000, 100_000_000, id="dimension"),
        pytest.param(900.0, 2_000, 3_000_000, id="pixel-product"),
    ),
)
def test_giant_mediabox_is_fitted_to_dimension_and_pixel_caps_without_a_preflight_raster(
    monkeypatch: pytest.MonkeyPatch,
    page_points: float,
    dimension_cap: int,
    pixel_cap: int,
) -> None:
    """A giant page used to fail the lecture (crop_failed); it is now measured
    at the largest scale that fits the caps, still without rasterising it."""
    import pypdfium2 as pdfium

    source = _source_pdf(
        (
            (
                (0.0, 0.0, page_points, page_points),
                (0.0, 0.0, page_points, page_points),
                0,
                1.0,
            ),
        )
    )
    monkeypatch.setattr(config, "VISUAL_PAGE_RENDER_SCALE", 2.0)
    monkeypatch.setattr(config, "MAX_VISUAL_PAGE_DIMENSION_PX", dimension_cap)
    monkeypatch.setattr(config, "MAX_VISUAL_PAGE_PIXELS", pixel_cap)
    renders: list[bool] = []

    def forbidden_render(*_args: object, **_kwargs: object) -> object:
        renders.append(True)
        raise AssertionError("page raster allocation must not occur during preflight")

    monkeypatch.setattr(pdfium.PdfPage, "render", forbidden_render)
    budget = _visual_budget()

    geometry = pdf_utils.split_pdf_chunks(source, budget=budget)[0].page_geometries[0]

    width, height = geometry.rendered_width, geometry.rendered_height
    assert width == height
    assert width <= dimension_cap and width * height <= pixel_cap
    # As much detail as the caps allow, not an arbitrary small raster.
    assert width == dimension_cap or width * height >= 0.99 * pixel_cap
    assert renders == []
    assert all(count == 0 for _, count in budget.redacted_counts())


@pytest.mark.parametrize("expiry_stage", ("parse", "geometry", "reopen", "halving"))
def test_manifest_split_deadline_expires_during_parse_geometry_reopen_or_halving_before_provider(
    monkeypatch: pytest.MonkeyPatch, expiry_stage: str
) -> None:
    import pypdfium2 as pdfium

    source = _source_pdf(
        (
            ((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),
            ((0.0, 0.0, 210.0, 300.0), (0.0, 0.0, 210.0, 300.0), 0, 1.0),
        )
    )
    phase = ["initial"]
    real_reader = pdf_utils.PdfReader
    reader_calls = [0]

    def tracking_reader(*args: object, **kwargs: object) -> PdfReader:
        reader_calls[0] += 1
        phase[0] = "parse" if reader_calls[0] == 1 else "reopen"
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(pdf_utils, "PdfReader", tracking_reader)
    real_width = pdfium.PdfPage.get_width

    def tracking_width(page: object) -> float:
        phase[0] = "geometry"
        return real_width(page)  # type: ignore[arg-type]

    monkeypatch.setattr(pdfium.PdfPage, "get_width", tracking_width)
    if expiry_stage == "halving":
        monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1_000_000)
        monkeypatch.setattr(config, "VISUAL_SOURCE_CHUNK_MAX_BYTES", 1_000_000)
        real_write = pdf_utils._write_pages

        def oversize_first_write(*args: object, **kwargs: object) -> bytes:
            data = real_write(*args, **kwargs)
            phase[0] = "halving"
            return data + (b"x" * 1_000_001)

        monkeypatch.setattr(pdf_utils, "_write_pages", oversize_first_write)

    budget = _visual_budget()
    real_check = budget.check_deadline

    def expiring_check() -> None:
        real_check()
        if phase[0] == expiry_stage:
            raise VisualFidelityError("deadline_exceeded")

    budget.check_deadline = expiring_check

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.split_pdf_chunks(source, budget=budget)

    assert caught.value.code == "deadline_exceeded"
    assert all(count == 0 for _, count in budget.redacted_counts())


def test_split_pdf_compatibility_still_returns_bytes() -> None:
    source = _source_pdf(
        (
            ((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),
            ((0.0, 0.0, 210.0, 300.0), (0.0, 0.0, 210.0, 300.0), 0, 1.0),
        )
    )

    chunks = pdf_utils.split_pdf(source)

    assert chunks and all(type(chunk) is bytes for chunk in chunks)
    assert sum(len(PdfReader(io.BytesIO(chunk)).pages) for chunk in chunks) == 2


def test_source_page_limit_fails_before_provider_or_raster_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdfium2 as pdfium

    source = _source_pdf(
        (
            ((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),
            ((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),
        )
    )
    monkeypatch.setattr(config, "MAX_SOURCE_PAGES", 1)
    pdfium_opens: list[bool] = []

    def forbidden_pdfium(*_args: object, **_kwargs: object) -> object:
        pdfium_opens.append(True)
        raise AssertionError("geometry/raster work must not start past the page cap")

    monkeypatch.setattr(pdfium, "PdfDocument", forbidden_pdfium)
    budget = _visual_budget()

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.split_pdf_chunks(source, budget=budget)

    assert caught.value.code == "too_many_pages"
    assert pdfium_opens == []
    assert all(count == 0 for _, count in budget.redacted_counts())


def test_visual_chunk_near_limit_reserves_mandatory_audit_overlay_raw_and_base64_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nine_mib = 9 * 1024 * 1024
    source_content = b"%" + (b"s" * (nine_mib - 4_098)) + b"\n"
    source = _source_pdf(
        (((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),),
        content_bytes=source_content,
    )
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", nine_mib)
    monkeypatch.setattr(config, "VISUAL_SOURCE_CHUNK_MAX_BYTES", nine_mib)
    monkeypatch.setattr(config, "MAX_AUDIT_PDF_BYTES", nine_mib)
    monkeypatch.setattr(config, "VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES", 18 * 1024 * 1024)
    monkeypatch.setattr(
        config, "VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES", 24 * 1024 * 1024
    )

    chunks = pdf_utils.split_pdf_chunks(source, budget=_visual_budget())

    assert len(chunks) == 1
    emitted = chunks[0].pdf_bytes
    assert nine_mib - 8_192 <= len(emitted) <= nine_mib
    emitted_page = PdfReader(io.BytesIO(emitted)).pages[0]
    emitted_content = emitted_page["/Contents"].get_object().get_data()
    assert emitted_content == source_content
    raw_total = len(emitted) + config.MAX_AUDIT_PDF_BYTES
    encoded_total = (
        4 * math.ceil(len(emitted) / 3)
        + 4 * math.ceil(config.MAX_AUDIT_PDF_BYTES / 3)
    )

    assert raw_total <= config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
    assert encoded_total <= config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES


def test_single_page_over_visual_chunk_cap_fails_before_any_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_pdf(
        (((0.0, 0.0, 200.0, 300.0), (0.0, 0.0, 200.0, 300.0), 0, 1.0),),
        content_bytes=b"%" + (b"source" * 2_000),
    )
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1_000)
    monkeypatch.setattr(config, "VISUAL_SOURCE_CHUNK_MAX_BYTES", 1_000)
    budget = _visual_budget()

    with pytest.raises(VisualFidelityError) as caught:
        pdf_utils.split_pdf_chunks(source, budget=budget)

    assert caught.value.code == "source_chunk_too_large"
    assert all(count == 0 for _, count in budget.redacted_counts())


def test_visual_limits_cannot_be_raised_past_hard_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "MAX_SOURCE_VISUALS",
        "MAX_SOURCE_PAGES",
        "MAX_VISUAL_CROP_PIXELS",
        "MIN_VISUAL_CROP_WIDTH_PX",
        "MIN_VISUAL_CROP_HEIGHT_PX",
        "MIN_VISUAL_CROP_PIXELS",
        "MAX_VISUAL_PNG_BYTES",
        "MAX_LABEL_LAYER_PNG_BYTES",
        "MAX_LABEL_LAYER_PIXELS",
        "MAX_VISUAL_PAGE_DIMENSION_PX",
        "MAX_VISUAL_PAGE_PIXELS",
        "VISUAL_SOURCE_CHUNK_MAX_BYTES",
        "MAX_AUDIT_PDF_BYTES",
        "VISUAL_PIPELINE_DEADLINE_SECONDS",
        "VISUAL_MODEL_TIMEOUT_SECONDS",
    )
    for name in names:
        monkeypatch.setenv(name, "999999999")
    monkeypatch.setenv("VISUAL_PAGE_RENDER_SCALE", "99")
    importlib.reload(config)
    try:
        assert config.MAX_SOURCE_VISUALS == 64
        assert config.MAX_SOURCE_PAGES == 300
        assert config.MAX_VISUAL_CROP_PIXELS == 8_000_000
        assert config.MIN_VISUAL_CROP_WIDTH_PX == 512
        assert config.MIN_VISUAL_CROP_HEIGHT_PX == 512
        assert config.MIN_VISUAL_CROP_PIXELS == 262_144
        assert config.MAX_VISUAL_PNG_BYTES == 10 * 1024 * 1024
        assert config.MAX_LABEL_LAYER_PNG_BYTES == 2 * 1024 * 1024
        assert config.MAX_LABEL_LAYER_PIXELS == 2_000_000
        assert config.MAX_VISUAL_PAGE_DIMENSION_PX == 5_000
        assert config.MAX_VISUAL_PAGE_PIXELS == 25_000_000
        assert config.VISUAL_PAGE_RENDER_SCALE == 2.5
        assert config.VISUAL_SOURCE_CHUNK_MAX_BYTES == 9 * 1024 * 1024
        assert config.MAX_AUDIT_PDF_BYTES == 9 * 1024 * 1024
        assert config.VISUAL_PIPELINE_DEADLINE_SECONDS == 780
        assert config.VISUAL_MODEL_TIMEOUT_SECONDS == 180
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_mathpix_limits_cannot_be_raised_past_hard_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "MATHPIX_REQUEST_TIMEOUT_SECONDS",
        "MATHPIX_MAX_POLL_ATTEMPTS",
        "MATHPIX_MAX_RESPONSE_BYTES",
        "MATHPIX_MAX_MMD_CHARS",
        "MATHPIX_MAX_EVIDENCE_LINES",
        "MATHPIX_MAX_EVIDENCE_TEXT_CHARS",
        "MATHPIX_MAX_POLYGON_POINTS_PER_LINE",
        "MATHPIX_MAX_EVIDENCE_JSON_BYTES",
    )
    for name in names:
        monkeypatch.setenv(name, "999999999")
    monkeypatch.setenv("MATHPIX_POLL_INTERVAL_SECONDS", "99")
    importlib.reload(config)
    try:
        assert config.MATHPIX_REQUEST_TIMEOUT_SECONDS == 30
        assert config.MATHPIX_MAX_POLL_ATTEMPTS == 12
        assert config.MATHPIX_POLL_INTERVAL_SECONDS == 5.0
        assert config.MATHPIX_MAX_RESPONSE_BYTES == 8 * 1024 * 1024
        assert config.MATHPIX_MAX_MMD_CHARS == 200_000
        assert config.MATHPIX_MAX_EVIDENCE_LINES == 2_048
        assert config.MATHPIX_MAX_EVIDENCE_TEXT_CHARS == 60_000
        assert config.MATHPIX_MAX_POLYGON_POINTS_PER_LINE == 64
        assert config.MATHPIX_MAX_EVIDENCE_JSON_BYTES == 1024 * 1024
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_mathpix_cleanup_reserve_exceeds_delete_timeout() -> None:
    assert config.MATHPIX_CLEANUP_RESERVE_SECONDS > config.MATHPIX_CLEANUP_TIMEOUT_SECONDS


def test_mathpix_credentials_remain_server_optional_for_gemini_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "configured")
    monkeypatch.setenv("LITELLM_API_KEY", "configured")
    monkeypatch.setenv("MATHPIX_ENABLED", "true")
    # Empty strings (not delenv): config's .env loader uses setdefault, so a
    # provisioned local .env would silently refill deleted variables.
    monkeypatch.setenv("MATHPIX_APP_ID", "")
    monkeypatch.setenv("MATHPIX_APP_KEY", "")
    importlib.reload(config)
    try:
        assert config.MATHPIX_ENABLED is True
        assert config.MATHPIX_APP_ID == ""
        assert config.MATHPIX_APP_KEY == ""
        assert config.missing_required() == []
        monkeypatch.setenv("MATHPIX_APP_ID", "partial-only")
        importlib.reload(config)
        assert config.MATHPIX_APP_ID == "partial-only"
        assert config.MATHPIX_APP_KEY == ""
        assert config.missing_required() == []
    finally:
        monkeypatch.undo()
        importlib.reload(config)
