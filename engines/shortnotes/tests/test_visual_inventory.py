from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
from dataclasses import replace

import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter

from notesbot import config, gemini_client
from notesbot.errors import VisualFidelityError
from notesbot.mathpix_client import MathpixEvidence, MathpixLine, MathpixPage
from notesbot.pdf_utils import PdfChunk, PdfPageGeometry, split_pdf_chunks
from notesbot.snapshot import extract_source_crops
from notesbot.visual_assets import (
    FALLBACK_PREFLIGHT_CHUNK_BYTES_PER_PIXEL,
    FALLBACK_PREFLIGHT_CHUNK_PIXELS,
    FALLBACK_PREFLIGHT_RETAINED_BYTES_PER_PIXEL,
    prepare_source_fallback,
)
from notesbot.visual_manifest import (
    AuditedRegion,
    CoverageProbe,
    InventoryAuditResponse,
    InventoryContext,
    InventoryLabel,
    InventoryResponse,
    InventoryTopicAnchor,
    ProbeDisposition,
    VisualRegion,
    VisualMemoryBudget,
    VisualRunBudget,
)
from tests.visual_fixture import (
    make_adaptive_grid_pdf,
    make_four_page_visual_pdf,
    make_many_component_pdf,
    make_probe_pdf,
)


def _vi():
    return importlib.import_module("notesbot.visual_inventory")


def _budget() -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)


def test_inventory_schema_adapter_preserves_legacy_call_shape(monkeypatch):
    files = [_vi().ModelFile("source.pdf", "application/pdf", b"pdf")]
    budget = _budget()
    expected = InventoryResponse(pages_checked=(1,), regions=())
    captured = {}

    def generate(supplied_files, prompt, schema, max_tokens, temperature, supplied_budget):
        captured["args"] = (
            supplied_files, prompt, schema, max_tokens, temperature, supplied_budget
        )
        return expected

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", generate, raising=False)

    result = gemini_client._inventory_schema_call(
        files, "prompt", InventoryResponse, 7, 12.5, budget
    )

    assert result is expected
    assert captured["args"] == (files, "prompt", InventoryResponse, 65536, 0.1, budget)


def test_inventory_submillisecond_deadline_fails_before_schema_dispatch():
    calls = []
    budget = VisualRunBudget(deadline_at=10.0019, clock=lambda: 10.001)

    def schema_call(*args):
        calls.append(args)
        raise AssertionError("schema dispatch must not begin")

    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(_chunk(), schema_call, budget)

    assert caught.value.code == "deadline_exceeded"
    assert calls == []


def _blank_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=400, height=400)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _chunk(
    pdf_bytes: bytes | None = None,
    *,
    index: int = 0,
    first_page: int = 1,
    pages: int = 1,
    width: int = 400,
    height: int = 400,
) -> PdfChunk:
    data = _blank_pdf(pages) if pdf_bytes is None else pdf_bytes
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(data)
    assert len(document) == pages
    geometries = tuple(
        PdfPageGeometry(
            global_page=first_page + offset,
            media_box=(0.0, 0.0, float(width), float(height)),
            crop_box=(0.0, 0.0, float(width), float(height)),
            rotation=0,
            user_unit=1.0,
            rendered_width=math.ceil(
                document[offset].get_width() * config.VISUAL_PAGE_RENDER_SCALE
            ),
            rendered_height=math.ceil(
                document[offset].get_height() * config.VISUAL_PAGE_RENDER_SCALE
            ),
        )
        for offset in range(pages)
    )
    return PdfChunk(index, data, first_page, pages, geometries)


def _memory_budget(*, page_bytes: int, source_bytes: int = 20 * 1024 * 1024) -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=page_bytes + source_bytes,
        max_source_bytes=source_bytes,
        max_context_bytes=1,
        max_asset_bytes=1,
        max_page_cache_bytes=page_bytes,
    )


def _pipeline_memory() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=384 * 1024 * 1024,
        max_source_bytes=128 * 1024 * 1024,
        max_context_bytes=64 * 1024 * 1024,
        max_asset_bytes=64 * 1024 * 1024,
        max_page_cache_bytes=128 * 1024 * 1024,
    )


def _anchor(ref: str = "a", *, page: int = 1, heading: str = "Topic") -> InventoryTopicAnchor:
    return InventoryTopicAnchor(
        anchor_ref=ref,
        local_page=page,
        box=(5.0, 2.0, 40.0, 8.0),
        exact_heading=heading,
    )


def _region(
    *,
    box: tuple[float, float, float, float] = (20.0, 20.0, 60.0, 60.0),
    page: int = 1,
    kind: str = "diagram",
    ref: str = "a",
    heading: str = "Topic",
    labels: tuple[InventoryLabel, ...] = (),
    contexts: tuple[InventoryContext, ...] = (),
    hint: int = 0,
) -> VisualRegion:
    return VisualRegion(
        local_page=page,
        box=box,
        kind=kind,
        topic_anchor=_anchor(ref, page=page, heading=heading),
        exact_labels=labels,
        context_regions=contexts,
        section_hint="",
        source_order_hint=hint,
    )


def _empty_call_log(inventory: InventoryResponse | None = None):
    calls: list[tuple[list[object], str, type[object]]] = []
    response = inventory or InventoryResponse(pages_checked=(1,), regions=())

    def call(files, prompt, schema, *_args):
        calls.append((files, prompt, schema))
        if schema is InventoryResponse:
            return response
        return InventoryAuditResponse(
            pages_checked=response.pages_checked,
            coverage_complete=True,
            unresolved_count=0,
            final_regions=(),
            probe_dispositions=(),
        )

    return calls, call


def _manual_case(
    *,
    box: tuple[float, float, float, float] = (20.0, 20.0, 60.0, 60.0),
    final_box: tuple[float, float, float, float] = (25.0, 25.0, 55.0, 55.0),
    labels: tuple[InventoryLabel, ...] = (),
    chunk_index: int = 0,
):
    provisional_id = f"draft-c{chunk_index:02d}-001"
    inventory = InventoryResponse(pages_checked=(1,), regions=(_region(box=box, labels=labels),))
    probe = CoverageProbe(
        probe_id="component-p001-0001",
        kind="component",
        local_page=1,
        box=final_box,
        foreground_fraction=1.0,
    )
    audit = InventoryAuditResponse(
        pages_checked=(1,),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=(
            AuditedRegion(
                **{**inventory.regions[0].model_dump(), "box": final_box},
                source_region_ids=(provisional_id,),
            ),
        ),
        probe_dispositions=(
            ProbeDisposition(
                probe_id=probe.probe_id,
                outcome="covered_visual",
                final_region_indices=(0,),
            ),
        ),
    )
    return inventory, (provisional_id,), (probe,), audit


def _bind_manual(vi, chunk: PdfChunk, case=None):
    inventory, ids, probes, audit = case or _manual_case(chunk_index=chunk.index)
    return vi.bind_audited_chunk(chunk, inventory, ids, probes, audit)


def _audit_call_for(inventory: InventoryResponse, mutate=None):
    vi = _vi()
    calls = []

    def call(files, prompt, schema, *_args):
        calls.append((files, prompt, schema))
        if schema is InventoryResponse:
            return inventory
        payload = vi.decode_audit_payload(prompt)
        audit = _complete_audit(payload)
        return mutate(audit, payload) if mutate else audit

    return calls, call


def _intersection_area(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def _complete_audit(payload: dict[str, object]) -> InventoryAuditResponse:
    provisional = payload["provisional_regions"]
    raw_probes = payload["probes"]
    assert isinstance(provisional, list) and isinstance(raw_probes, list)
    regions = [
        VisualRegion.model_validate_json(json.dumps(item["region"]))
        for item in provisional
    ]
    probes = [
        CoverageProbe.model_validate_json(json.dumps(item)) for item in raw_probes
    ]
    assignments: list[set[int]] = [set() for _ in probes]
    supports: list[list[tuple[float, float, float, float]]] = [[] for _ in regions]
    for probe_index, probe in enumerate(probes):
        area = (probe.box[2] - probe.box[0]) * (probe.box[3] - probe.box[1])
        cx = (probe.box[0] + probe.box[2]) / 2
        cy = (probe.box[1] + probe.box[3]) / 2
        for region_index, region in enumerate(regions):
            if probe.local_page != region.local_page:
                continue
            inside = region.box[0] <= cx <= region.box[2] and region.box[1] <= cy <= region.box[3]
            if inside or _intersection_area(probe.box, region.box) >= 0.25 * area:
                assignments[probe_index].add(region_index)
                if probe.kind == "component":
                    supports[region_index].append(probe.box)
    final = []
    for index, region in enumerate(regions):
        assert supports[index], "fixture inventory regions must have component support"
        boxes = supports[index]
        support_box = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        final.append(
            AuditedRegion(
                **{**region.model_dump(), "box": support_box},
                source_region_ids=(provisional[index]["provisional_id"],),
            )
        )
    dispositions = tuple(
        ProbeDisposition(
            probe_id=probe.probe_id,
            outcome="covered_visual" if assignments[index] else "ordinary_text",
            final_region_indices=tuple(sorted(assignments[index])),
        )
        for index, probe in enumerate(probes)
    )
    return InventoryAuditResponse(
        pages_checked=tuple(payload["pages_checked"]),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=tuple(final),
        probe_dispositions=dispositions,
    )


def _mathpix(chunk: PdfChunk, *, uncertain: bool = False) -> MathpixEvidence:
    pages = tuple(
        MathpixPage(
            local_page=page,
            page_width=400,
            page_height=400,
            lines=(
                MathpixLine(
                    local_page=page,
                    box=(20.0, 20.0, 60.0, 60.0),
                    polygon=(),
                    line_type="diagram",
                    role="visual_candidate",
                    text="A",
                    confidence=0.2 if uncertain else 0.95,
                    uncertain=uncertain,
                ),
            ),
        )
        for page in range(1, chunk.page_count + 1)
    )
    return MathpixEvidence(
        chunk_index=chunk.index,
        chunk_sha256=chunk.sha256_hex,
        first_global_page=chunk.first_global_page,
        page_count=chunk.page_count,
        pages=pages,
        mmd_page_count=chunk.page_count,
    )


def test_inventory_chunk_makes_exactly_one_inventory_and_one_audit_call() -> None:
    calls, call = _empty_call_log()
    budget = _budget()
    _vi().inventory_chunk(_chunk(), call, budget)
    assert [item[2] for item in calls] == [InventoryResponse, InventoryAuditResponse]
    assert calls[0][0][0].data == calls[1][0][0].data == _chunk().pdf_bytes
    assert budget.inventory_calls == {0: 1}
    assert budget.audit_calls == {0: 1}


def test_inventory_with_mathpix_attaches_one_normalized_evidence_file_and_original_source() -> None:
    chunk = _chunk()
    calls, call = _empty_call_log()
    _vi().inventory_chunk(chunk, call, _budget(), _mathpix(chunk))
    first_files = calls[0][0]
    assert [(f.filename, f.mime_type) for f in first_files] == [
        ("source-pages.pdf", "application/pdf"),
        ("mathpix-source-evidence.json", "application/json"),
    ]
    assert first_files[0].data == chunk.pdf_bytes
    assert json.loads(first_files[1].data)["pages"][0]["lines"][0]["uncertain"] is False


def test_inventory_rejects_mathpix_evidence_swapped_between_equal_length_chunks() -> None:
    source = _blank_pdf()
    first = _chunk(pdf_bytes=source + b"A", index=0)
    second = _chunk(pdf_bytes=source + b"B", index=1)
    calls, call = _empty_call_log()
    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(second, call, _budget(), _mathpix(first))
    assert caught.value.code == "inventory_incomplete"
    assert calls == []


def test_inventory_without_mathpix_independently_extracts_from_original_source_in_same_call() -> None:
    calls, call = _empty_call_log()
    chunk = _chunk()
    _vi().inventory_chunk(chunk, call, _budget())
    assert len(calls[0][0]) == 1 and calls[0][0][0].data == chunk.pdf_bytes
    prompt = calls[0][1].lower()
    assert "independently" in prompt and "original" in prompt and "same inventory call" in prompt


@pytest.mark.parametrize("with_evidence", (False, True))
def test_mathpix_success_or_fallback_keeps_exactly_two_gemini_calls_per_chunk(with_evidence: bool) -> None:
    chunk = _chunk()
    calls, call = _empty_call_log()
    budget = _budget()
    _vi().inventory_chunk(chunk, call, budget, _mathpix(chunk) if with_evidence else None)
    assert len(calls) == 2
    assert budget.mathpix_evidence_used == ({0: 1} if with_evidence else {})
    assert budget.mathpix_source_fallback == ({} if with_evidence else {0: 1})


def test_duplicate_inventory_authorization_cannot_orphan_mathpix_outcome_or_dispatch() -> None:
    budget = _budget()
    budget.inventory_calls[0] = 1
    calls, call = _empty_call_log()

    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(_chunk(), call, budget)

    assert caught.value.code == "work_limit_exceeded"
    assert calls == []
    assert budget.inventory_calls == {0: 1}
    assert budget.mathpix_evidence_used == {}
    assert budget.mathpix_source_fallback == {}


def test_deadline_during_atomic_inventory_authorization_leaves_no_orphan_ledgers() -> None:
    values = iter((1.0, 1.0, 1.0, 101.0))
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: next(values))
    calls, call = _empty_call_log()

    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(_chunk(), call, budget)

    assert caught.value.code == "deadline_exceeded"
    assert calls == []
    assert budget.inventory_calls == {}
    assert budget.mathpix_evidence_used == {}
    assert budget.mathpix_source_fallback == {}


@pytest.mark.parametrize("malformed_stage", ("inventory", "audit"))
def test_malformed_discovery_response_has_exact_ledgers_and_no_model_loop(
    malformed_stage: str,
) -> None:
    calls: list[type[object]] = []

    def call(_files, _prompt, schema, *_args):
        calls.append(schema)
        if malformed_stage == "inventory" or schema is InventoryAuditResponse:
            return {"malformed": True}
        return InventoryResponse(pages_checked=(1,), regions=())

    budget = _budget()
    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(_chunk(), call, budget)

    assert caught.value.code == "inventory_incomplete"
    expected = (
        [InventoryResponse]
        if malformed_stage == "inventory"
        else [InventoryResponse, InventoryAuditResponse]
    )
    assert calls == expected
    assert budget.inventory_calls == {0: 1}
    assert budget.mathpix_source_fallback == {0: 1}
    assert budget.audit_calls == (
        {} if malformed_stage == "inventory" else {0: 1}
    )


def test_low_confidence_mathpix_proposal_remains_uncertain_for_audit_resolution() -> None:
    chunk = _chunk()
    calls, call = _empty_call_log()
    _vi().inventory_chunk(chunk, call, _budget(), _mathpix(chunk, uncertain=True))
    evidence = json.loads(calls[0][0][1].data)
    assert evidence["pages"][0]["lines"][0]["uncertain"] is True
    assert "uncertain" in calls[0][1].lower() and "audit" in calls[0][1].lower()


def test_mathpix_proposal_cannot_authorize_element_absent_from_source_pixels_and_probes() -> None:
    chunk = _chunk()
    calls, call = _empty_call_log()
    _vi().inventory_chunk(chunk, call, _budget(), _mathpix(chunk))
    prompt = calls[0][1].lower()
    assert "cannot authorize" in prompt and "source pixels" in prompt


def test_inventory_prompt_marks_mathpix_ocr_and_smiles_as_non_authoritative() -> None:
    prompt = _vi().INVENTORY_PROMPT.lower()
    assert "ocr" in prompt and "smiles" in prompt and "non-authoritative" in prompt


def test_inventory_prompt_forbids_summary_and_requires_every_visual_type() -> None:
    prompt = _vi().INVENTORY_PROMPT.lower()
    assert "do not summarize" in prompt
    for word in ("diagram", "labelled structure", "chemical structure", "apparatus", "circuit", "ray", "mechanism", "drawn graph"):
        assert word in prompt


def test_audit_prompt_states_the_probe_region_overlap_contract() -> None:
    prompt = _vi().AUDIT_PROMPT.lower()
    assert "positive-area overlap" in prompt
    assert "same local_page" in prompt
    assert "final_region_indices" in prompt


def test_context_rejects_same_page_different_topic() -> None:
    with pytest.raises(ValidationError):
        _region(
            contexts=(
                InventoryContext(anchor_ref="other", local_page=1, box=(1, 10, 20, 15), role="explanation"),
            )
        )


def test_context_accepts_adjacent_page_same_topic_within_chunk() -> None:
    context = InventoryContext(anchor_ref="a", local_page=2, box=(1, 10, 20, 15), role="explanation")
    region = _region(contexts=(context,))
    assert region.context_regions == (context,)


def test_context_topic_key_survives_inventory_audit_and_finalize() -> None:
    vi = _vi()
    context = InventoryContext(anchor_ref="a", local_page=1, box=(5, 10, 30, 15), role="legend", exact_text="legend")
    inventory, ids, probes, audit = _manual_case()
    region = inventory.regions[0].model_copy(update={"context_regions": (context,)})
    inventory = inventory.model_copy(update={"regions": (region,)})
    audit_region = audit.final_regions[0].model_copy(update={"context_regions": (context,)})
    audit = audit.model_copy(update={"final_regions": (audit_region,)})
    vi.validate_inventory_audit(inventory, ids, probes, audit)
    chunk = _chunk()
    envelope = _bind_manual(vi, chunk, (inventory, ids, probes, audit))
    budget = _budget(); budget.inventory_calls[0] = 1; budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    visual = vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64).visuals[0]
    assert visual.context_regions[0].topic_key == visual.topic_anchor.topic_key


def test_inventory_schema_does_not_require_runtime_topic_key_and_finalize_assigns_matching_immutable_keys() -> None:
    vi = _vi()
    assert "topic_key" not in InventoryResponse.model_json_schema()["$defs"]["InventoryTopicAnchor"]["properties"]
    inventory, ids, probes, audit = _manual_case()
    vi.validate_inventory_audit(inventory, ids, probes, audit)
    chunk = _chunk(); envelope = _bind_manual(vi, chunk, (inventory, ids, probes, audit))
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    topic = vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64).visuals[0].topic_anchor
    assert topic.topic_key.startswith("topic-") and not hasattr(topic, "anchor_ref")


def test_runtime_topic_key_uses_canonical_decimal_geometry_and_exact_heading_hash() -> None:
    vi = _vi()
    negative_zero = _anchor().model_copy(
        update={"box": (-0.0, 1.0, 2.0, 3.0)}
    )
    positive_zero = _anchor().model_copy(
        update={"box": (0.0, 1.00, 2e0, 3.000)}
    )
    expected = "topic-00-p0001-b0,1,2,3-7e61847d61d6"
    assert vi._topic_key(0, negative_zero) == expected
    assert vi._topic_key(0, positive_zero) == expected

    exact_whitespace = positive_zero.model_copy(
        update={"exact_heading": " Topic "}
    )
    assert vi._topic_key(0, exact_whitespace) == (
        "topic-00-p0001-b0,1,2,3-895cdbc0324d"
    )
    assert vi._topic_key(1, positive_zero) != expected
    assert vi._topic_key(
        0,
        positive_zero.model_copy(update={"local_page": 2}),
    ) != expected


def test_reused_anchor_ref_requires_identical_page_box_and_exact_heading() -> None:
    vi = _vi(); chunk = _chunk()
    probes = (
        CoverageProbe(probe_id="p1", kind="component", local_page=1, box=(20, 20, 30, 30), foreground_fraction=1),
        CoverageProbe(probe_id="p2", kind="component", local_page=1, box=(60, 60, 70, 70), foreground_fraction=1),
    )
    first = AuditedRegion(**_region(box=probes[0].box, ref="a").model_dump(), source_region_ids=())
    second = AuditedRegion(**_region(box=probes[1].box, ref="a", heading="Other").model_dump(), source_region_ids=())
    audit = InventoryAuditResponse(
        pages_checked=(1,), coverage_complete=True, unresolved_count=0,
        final_regions=(first, second),
        probe_dispositions=(
            ProbeDisposition(probe_id="p1", outcome="covered_visual", final_region_indices=(0,)),
            ProbeDisposition(probe_id="p2", outcome="covered_visual", final_region_indices=(1,)),
        ),
    )
    envelope = vi.bind_audited_chunk(
        chunk, InventoryResponse(pages_checked=(1,), regions=()), (), probes, audit
    )
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64)


def test_finalize_omits_oversized_context_text_once_without_truncation(monkeypatch) -> None:
    vi = _vi(); monkeypatch.setattr(config, "MAX_CONTEXT_TEXT_CHARS", 4)
    context = InventoryContext(anchor_ref="a", local_page=1, box=(5, 10, 30, 15), role="legend", exact_text="12345")
    inventory, ids, probes, audit = _manual_case()
    inventory = inventory.model_copy(update={"regions": (inventory.regions[0].model_copy(update={"context_regions": (context,)}),)})
    audit = audit.model_copy(update={"final_regions": (audit.final_regions[0].model_copy(update={"context_regions": (context,)}),)})
    chunk = _chunk(); envelope = _bind_manual(vi, chunk, (inventory, ids, probes, audit))
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    manifest = vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64)
    assert manifest.visuals[0].context_regions[0].exact_text == ""
    assert "12345" not in repr(manifest)


def test_audit_overlay_contains_every_provisional_box_and_id() -> None:
    vi = _vi()
    boxes = ((20.0, 20.0, 60.0, 60.0), (65.0, 20.0, 90.0, 50.0))
    inventory = InventoryResponse(
        pages_checked=(1,),
        regions=(_region(box=boxes[0]), _region(box=boxes[1], ref="b")),
    )
    overlay = vi.build_audit_overlay(_chunk(make_probe_pdf()), inventory, ("draft-c00-001", "draft-c00-002"), ())
    assert overlay.startswith(b"%PDF")
    import pypdfium2 as pdfium
    image = pdfium.PdfDocument(overlay)[0].render(scale=1).to_pil().convert("RGB")
    pixels = image.load()

    def is_pink(x: int, y: int) -> bool:
        red, green, blue = pixels[x, y]
        return red > 180 and green < 150 and blue > 100

    for box in boxes:
        x0, y0, x1, y1 = (
            round(box[0] * image.width / 100),
            round(box[1] * image.height / 100),
            round(box[2] * image.width / 100),
            round(box[3] * image.height / 100),
        )
        left = sum(is_pink(x, y) for x in range(x0, x0 + 3) for y in range(y0, y1 + 1))
        right = sum(is_pink(x, y) for x in range(x1 - 2, x1 + 1) for y in range(y0, y1 + 1))
        top = sum(is_pink(x, y) for x in range(x0, x1 + 1) for y in range(y0, y0 + 3))
        bottom = sum(is_pink(x, y) for x in range(x0, x1 + 1) for y in range(y1 - 2, y1 + 1))
        identifier_ink = sum(
            is_pink(x, y)
            for x in range(x0 + 4, min(x1, x0 + 100))
            for y in range(y0 + 4, min(y1, y0 + 18))
        )
        assert min(left, right) > (y1 - y0) // 2
        assert min(top, bottom) > (x1 - x0) // 2
        assert identifier_ink > 20


def test_audit_accounts_for_every_nonblank_foreground_probe() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    vi.validate_inventory_audit(inventory, ids, probes, audit)
    duplicate = audit.model_copy(update={"probe_dispositions": audit.probe_dispositions * 2})
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, duplicate)
    missing = audit.model_copy(update={"probe_dispositions": ()})
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, missing)
    unknown = audit.model_copy(
        update={
            "probe_dispositions": audit.probe_dispositions
            + (ProbeDisposition(probe_id="unknown", outcome="ordinary_text"),)
        }
    )
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, unknown)


def test_audit_failure_logs_only_fixed_phase_and_integer_counts(capsys) -> None:
    vi = _vi()
    inventory, ids, probes, audit = _manual_case()
    missing = audit.model_copy(update={"probe_dispositions": ()})

    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, missing)

    assert capsys.readouterr().out.strip() == (
        "[notesbot.audit-error] category=disposition_set"
        " provisional=1 final=1 probes=1 dispositions=0"
        " complete=1 unresolved=0"
    )


def test_bind_and_finalize_require_exact_inventory_probe_support_closure() -> None:
    vi = _vi()
    chunk = _chunk()
    inventory, ids, probes, audit = _manual_case()
    unsupported = audit.model_copy(update={"probe_dispositions": ()})

    with pytest.raises(VisualFidelityError):
        vi.bind_audited_chunk(chunk, inventory, ids, probes, unsupported)

    envelope = vi.bind_audited_chunk(chunk, inventory, ids, probes, audit)
    forged = replace(envelope, probes=())
    budget = _budget()
    budget.inventory_calls[0] = budget.audit_calls[0] = 1
    budget.mathpix_source_fallback[0] = 1
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest(
            (forged,), (chunk,), budget, total_pages=1, max_visuals=64
        )


def test_finalize_rejects_probe_evidence_changed_after_closure_binding() -> None:
    vi = _vi()
    chunk = _chunk()
    envelope = _bind_manual(vi, chunk)
    changed_probe = envelope.probes[0].model_copy(
        update={"foreground_fraction": 0.5}
    )
    forged = replace(envelope, probes=(changed_probe,))
    budget = _budget()
    budget.inventory_calls[0] = budget.audit_calls[0] = 1
    budget.mathpix_source_fallback[0] = 1

    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest(
            (forged,), (chunk,), budget, total_pages=1, max_visuals=64
        )


def test_lecture_probe_cap_fails_before_overlay_or_audit_call_without_truncation(monkeypatch) -> None:
    vi = _vi()
    chunk = _chunk(make_probe_pdf())
    calls, call = _empty_call_log()
    monkeypatch.setattr(config, "MAX_COVERAGE_PROBES_TOTAL", 1)
    budget = _budget()
    with pytest.raises(VisualFidelityError):
        vi.inventory_chunk(chunk, call, budget)
    assert len(calls) == 1
    assert budget.coverage_probes_total == 0
    assert budget.audit_calls == {}


def test_incremental_pages_share_memory_budget_and_release_each_page() -> None:
    fixture = make_four_page_visual_pdf()
    chunk = _chunk(fixture.pdf_bytes, pages=4, width=800, height=1000)
    regions = tuple(
        _region(
            box=truth.box,
            page=truth.local_page,
            kind=truth.kind,
            ref=f"a{index}",
            hint=index,
        )
        for index, truth in enumerate(fixture.truth)
    )
    inventory = InventoryResponse(pages_checked=(1, 2, 3, 4), regions=regions)
    calls, call = _audit_call_for(inventory)
    one_page_bytes = max(
        geometry.rendered_width * geometry.rendered_height * 8
        for geometry in chunk.page_geometries
    )
    max_work_bytes = 0
    for region in regions:
        geometry = chunk.page_geometries[region.local_page - 1]
        width = math.ceil(geometry.rendered_width * region.box[2] / 100) - math.floor(
            geometry.rendered_width * region.box[0] / 100
        )
        height = math.ceil(geometry.rendered_height * region.box[3] / 100) - math.floor(
            geometry.rendered_height * region.box[1] / 100
        )
        pixels = width * height
        max_work_bytes = max(
            max_work_bytes,
            pixels * FALLBACK_PREFLIGHT_RETAINED_BYTES_PER_PIXEL
            + min(pixels, FALLBACK_PREFLIGHT_CHUNK_PIXELS)
            * FALLBACK_PREFLIGHT_CHUNK_BYTES_PER_PIXEL,
        )
    memory = _memory_budget(page_bytes=one_page_bytes + max_work_bytes)

    envelope = _vi().inventory_chunk(
        chunk, call, _budget(), memory_budget=memory
    )

    assert len(calls) == 2
    assert envelope.probes
    assert memory.page_cache_bytes == 0
    assert memory.resident_bytes() == 0


def test_deadline_after_first_page_render_stops_before_second_and_releases_memory(monkeypatch) -> None:
    import pypdfium2 as pdfium

    now = [1.0]
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: now[0])
    chunk = _chunk(_blank_pdf(4), pages=4)
    memory = _memory_budget(
        page_bytes=max(
            geometry.rendered_width * geometry.rendered_height * 8
            for geometry in chunk.page_geometries
        )
    )
    calls, call = _empty_call_log(
        InventoryResponse(pages_checked=(1, 2, 3, 4), regions=())
    )
    real_render = pdfium.PdfPage.render
    rendered_pages: list[int] = []

    def expiring_render(page, *args, **kwargs):
        result = real_render(page, *args, **kwargs)
        rendered_pages.append(len(rendered_pages) + 1)
        now[0] = 101.0
        return result

    monkeypatch.setattr(pdfium.PdfPage, "render", expiring_render)

    with pytest.raises(VisualFidelityError) as caught:
        _vi().inventory_chunk(chunk, call, budget, memory_budget=memory)

    assert caught.value.code == "deadline_exceeded"
    assert rendered_pages == [1]
    assert len(calls) == 1
    assert budget.audit_calls == {}
    assert memory.resident_bytes() == 0


def test_fallback_preflight_rasterizes_only_pages_with_final_regions(monkeypatch) -> None:
    import pypdfium2 as pdfium

    vi = _vi()
    chunk = _chunk(_blank_pdf(3), pages=3)
    region = _region(page=2, box=(20.0, 20.0, 60.0, 60.0))
    probe = CoverageProbe(
        probe_id="component-p002-0001",
        kind="component",
        local_page=2,
        box=(25.0, 25.0, 55.0, 55.0),
        foreground_fraction=1.0,
    )
    audit = InventoryAuditResponse(
        pages_checked=(1, 2, 3),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=(
            AuditedRegion(
                **region.model_dump(mode="python"),
                source_region_ids=("draft-c00-001",),
            ),
        ),
        probe_dispositions=(
            ProbeDisposition(
                probe_id=probe.probe_id,
                outcome="covered_visual",
                final_region_indices=(0,),
            ),
        ),
    )
    page_bytes = max(
        item.rendered_width * item.rendered_height * 8
        for item in chunk.page_geometries
    )
    memory = _memory_budget(page_bytes=page_bytes)
    rendered: list[int] = []
    real_render = pdfium.PdfPage.render

    def tracking_render(page, *args, **kwargs):
        rendered.append(1)
        return real_render(page, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "render", tracking_render)
    monkeypatch.setattr(vi, "_fallback_box_is_viable", lambda *_args: True)

    result, dropped_ids = vi._expand_unsafe_fallback_bounds(
        chunk,
        (probe,),
        audit,
        _budget(),
        memory,
    )

    assert result.final_regions == audit.final_regions
    assert dropped_ids == frozenset()
    assert rendered == [1]
    assert memory.resident_bytes() == 0


def test_dense_page_compacts_probes_to_cap_without_losing_grid_coverage() -> None:
    chunk = _chunk(make_many_component_pdf())
    budget = _budget()
    memory = _memory_budget(
        page_bytes=chunk.page_geometries[0].rendered_width
        * chunk.page_geometries[0].rendered_height
        * 8
    )

    probes = _vi().generate_coverage_probes(
        chunk,
        budget=budget,
        memory_budget=memory,
    )

    assert len(probes) == 256
    assert len({probe.probe_id for probe in probes}) == 256
    assert {probe.kind for probe in probes} == {"component", "grid"}
    assert budget.coverage_probes_total == 256
    assert memory.resident_bytes() == 0


@pytest.mark.parametrize("cap", ("raw", "encoded"))
def test_audit_overlay_raw_and_encoded_payload_caps_fail_before_audit_call(monkeypatch, cap: str) -> None:
    import pypdfium2 as pdfium

    vi = _vi()
    chunk = _chunk(_blank_pdf(4), pages=4)
    calls, call = _empty_call_log(
        InventoryResponse(pages_checked=(1, 2, 3, 4), regions=())
    )
    if cap == "raw":
        monkeypatch.setattr(
            config, "VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES", len(chunk.pdf_bytes) + 1
        )
    else:
        source_encoded = 4 * ((len(chunk.pdf_bytes) + 2) // 3)
        monkeypatch.setattr(
            config, "VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES", source_encoded + 1
        )
    real_render = pdfium.PdfPage.render
    rendered_pages: list[int] = []

    def tracking_render(page, *args, **kwargs):
        rendered_pages.append(len(rendered_pages) + 1)
        return real_render(page, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "render", tracking_render)
    with pytest.raises(VisualFidelityError):
        vi.inventory_chunk(chunk, call, _budget())
    assert len(calls) == 1
    assert rendered_pages == [1]


def test_small_visual_inside_text_heavy_coarse_cell_gets_adaptive_probe() -> None:
    probes = _vi().generate_coverage_probes(
        _chunk(make_probe_pdf(dark_box=(170, 170, 188, 188), text_heavy=True))
    )
    components = [p for p in probes if p.kind == "component"]
    assert any((p.box[2] - p.box[0]) < 10 and (p.box[3] - p.box[1]) < 10 for p in components)


def test_adaptive_grid_discards_parent_but_never_emits_subminimum_children() -> None:
    import pypdfium2 as pdfium

    pdf_bytes = make_adaptive_grid_pdf(size=1600)
    chunk = _chunk(pdf_bytes)
    probes = _vi().generate_coverage_probes(chunk)
    grid = [probe for probe in probes if probe.kind == "grid"]
    document = pdfium.PdfDocument(pdf_bytes)
    rendered = document[0].render(scale=config.VISUAL_PAGE_RENDER_SCALE).to_pil()

    assert grid
    assert not any(probe.box == (0.0, 0.0, 25.0, 25.0) for probe in grid)
    assert any((probe.box[2] - probe.box[0]) < 12.5 for probe in grid)
    assert all(
        (probe.box[2] - probe.box[0]) * rendered.width / 100
        >= config.COVERAGE_PROBE_MIN_CELL_PX
        and (probe.box[3] - probe.box[1]) * rendered.height / 100
        >= config.COVERAGE_PROBE_MIN_CELL_PX
        for probe in grid
    )
    assert len({probe.box for probe in grid}) == len(grid)


def test_mixed_or_uncertain_probe_fails_instead_of_becoming_ordinary_text() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    bad = audit.model_copy(update={"unresolved_count": 1, "probe_dispositions": (audit.probe_dispositions[0].model_copy(update={"outcome": "unresolved", "final_region_indices": ()}),)})
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, bad)


def test_audit_may_add_a_missed_region_without_losing_initial_regions() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    new_probe = CoverageProbe(probe_id="new", kind="component", local_page=1, box=(70, 70, 80, 80), foreground_fraction=1)
    new_region = AuditedRegion(**_region(box=(70, 70, 80, 80), ref="b").model_dump(), source_region_ids=())
    expanded = audit.model_copy(update={"final_regions": audit.final_regions + (new_region,), "probe_dispositions": audit.probe_dispositions + (ProbeDisposition(probe_id="new", outcome="covered_visual", final_region_indices=(1,)),)})
    vi.validate_inventory_audit(inventory, ids, probes + (new_probe,), expanded)


def test_audit_merge_requires_proven_duplicate_mapping() -> None:
    vi = _vi(); first = _region(); second = _region(box=(21, 21, 61, 61), ref="b")
    inventory = InventoryResponse(pages_checked=(1,), regions=(first, second))
    probe = CoverageProbe(probe_id="p", kind="component", local_page=1, box=(25, 25, 55, 55), foreground_fraction=1)
    merged = AuditedRegion(**{**first.model_dump(), "box": probe.box}, source_region_ids=("draft-c00-001", "draft-c00-002"))
    audit = InventoryAuditResponse(pages_checked=(1,), coverage_complete=True, unresolved_count=0, final_regions=(merged,), probe_dispositions=(ProbeDisposition(probe_id="p", outcome="covered_visual", final_region_indices=(0,)),))
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ("draft-c00-001", "draft-c00-002"), (probe,), audit)


def test_three_way_merge_requires_every_pair_to_be_proven_duplicates() -> None:
    vi = _vi()
    boxes = (
        (10.0, 20.0, 90.0, 60.0),
        (6.8, 20.0, 86.8, 60.0),
        (13.2, 20.0, 93.2, 60.0),
    )
    regions = tuple(_region(box=box) for box in boxes)
    inventory = InventoryResponse(pages_checked=(1,), regions=regions)
    probe = CoverageProbe(
        probe_id="component-p001-0001",
        kind="component",
        local_page=1,
        box=(20.0, 25.0, 80.0, 55.0),
        foreground_fraction=1.0,
    )
    merged = AuditedRegion(
        **{**regions[0].model_dump(), "box": probe.box},
        source_region_ids=("draft-c00-001", "draft-c00-002", "draft-c00-003"),
    )
    audit = InventoryAuditResponse(
        pages_checked=(1,),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=(merged,),
        probe_dispositions=(
            ProbeDisposition(
                probe_id=probe.probe_id,
                outcome="covered_visual",
                final_region_indices=(0,),
            ),
        ),
    )

    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(
            inventory,
            ("draft-c00-001", "draft-c00-002", "draft-c00-003"),
            (probe,),
            audit,
        )


def test_duplicate_merge_iou_boundary_is_explicit() -> None:
    vi = _vi()
    assert vi.duplicate_merge_allowed((0, 0, 100, 100), (0, 0, 90, 100)) is True
    assert vi.duplicate_merge_allowed((0, 0, 100, 100), (0, 0, 89.999, 100)) is False


def test_audit_mapped_region_cannot_change_page_relocate_or_expand_unbounded() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    for update in ({"local_page": 2}, {"box": (70, 70, 90, 90)}, {"box": (1, 1, 99, 99)}):
        bad_region = audit.final_regions[0].model_copy(update=update)
        with pytest.raises(VisualFidelityError):
            vi.validate_inventory_audit(inventory, ids, probes, audit.model_copy(update={"final_regions": (bad_region,)}))


@pytest.mark.parametrize(
    ("model_box", "expected_box"),
    (
        ((30.0, 30.0, 50.0, 50.0), (25.0, 25.0, 55.0, 55.0)),
        ((1.0, 1.0, 99.0, 99.0), (23.0, 23.0, 57.0, 57.0)),
    ),
)
def test_runtime_clamps_model_crop_to_verified_support_interval(
    model_box: tuple[float, float, float, float],
    expected_box: tuple[float, float, float, float],
) -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    bad_region = audit.final_regions[0].model_copy(update={"box": model_box})
    repaired = vi._normalize_audit_region_bounds(
        probes,
        audit.model_copy(update={"final_regions": (bad_region,)}),
    )

    assert repaired.final_regions[0].box == expected_box
    vi.validate_inventory_audit(inventory, ids, probes, repaired)


def test_inventory_adds_verified_margin_only_when_tight_crop_cannot_form_fallback() -> None:
    vi = _vi()
    pdf_bytes = make_probe_pdf(dark_box=(120, 120, 280, 240))
    budget = _budget()
    chunks = tuple(split_pdf_chunks(pdf_bytes, budget=budget))
    assert len(chunks) == 1
    chunk = chunks[0]
    inventory = InventoryResponse(
        pages_checked=(1,),
        regions=(_region(box=(20.0, 20.0, 80.0, 70.0)),),
    )
    _calls, call = _audit_call_for(inventory)
    memory = _pipeline_memory()
    try:
        envelope = vi.inventory_chunk(
            chunk,
            call,
            budget,
            memory_budget=memory,
        )

        dispositions = {
            item.probe_id: item for item in envelope.audit.probe_dispositions
        }
        support = tuple(
            probe.box
            for probe in envelope.probes
            if probe.kind == "component"
            and dispositions[probe.probe_id].final_region_indices
        )
        support_box = vi._support_union(support)
        margin = float(config.AUDIT_SUPPORT_OUTWARD_MARGIN_PCT)
        expected_box = (
            max(0.0, support_box[0] - margin),
            max(0.0, support_box[1] - margin),
            min(100.0, support_box[2] + margin),
            min(100.0, support_box[3] + margin),
        )
        assert envelope.audit.final_regions[0].box == expected_box

        manifest = vi.finalize_manifest(
            (envelope,),
            chunks,
            budget,
            total_pages=1,
            max_visuals=8,
        )
        crops = extract_source_crops(
            pdf_bytes,
            manifest.visuals,
            chunks,
            budget,
            memory,
            render_scale=config.VISUAL_PAGE_RENDER_SCALE,
            max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
            max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
            max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
        )
        assert len(crops) == 1
        prepared = prepare_source_fallback(crops[0], budget, memory)
        assert prepared.foreground_recall == 1.0
        assert crops[0].visual.box == expected_box
    finally:
        memory.close()
    assert memory.resident_bytes() == 0


def test_inventory_preserves_safe_model_selected_padding() -> None:
    vi = _vi()
    pdf_bytes = make_probe_pdf(dark_box=(120, 120, 280, 240))
    budget = _budget()
    chunk = tuple(split_pdf_chunks(pdf_bytes, budget=budget))[0]
    inventory = InventoryResponse(
        pages_checked=(1,),
        regions=(_region(box=(20.0, 20.0, 80.0, 70.0)),),
    )
    selected: list[tuple[float, float, float, float]] = []

    def padded(audit: InventoryAuditResponse, _payload: object):
        region = audit.final_regions[0]
        margin = float(config.AUDIT_SUPPORT_OUTWARD_MARGIN_PCT)
        box = (
            max(0.0, region.box[0] - margin),
            max(0.0, region.box[1] - margin),
            min(100.0, region.box[2] + margin),
            min(100.0, region.box[3] + margin),
        )
        selected.append(box)
        return audit.model_copy(
            update={
                "final_regions": (
                    region.model_copy(update={"box": box}),
                )
            }
        )

    _calls, call = _audit_call_for(inventory, padded)
    envelope = vi.inventory_chunk(chunk, call, budget)

    assert selected and envelope.audit.final_regions[0].box == selected[0]


def test_inventory_drops_region_when_verified_margin_still_has_no_foreground(
    capsys,
) -> None:
    vi = _vi()
    pdf_bytes = make_probe_pdf(dark_box=(0, 0, 399, 399))
    budget = _budget()
    chunk = tuple(split_pdf_chunks(pdf_bytes, budget=budget))[0]
    inventory = InventoryResponse(
        pages_checked=(1,),
        regions=(_region(box=(0.0, 0.0, 100.0, 100.0)),),
    )
    _calls, call = _audit_call_for(inventory)

    envelope = vi.inventory_chunk(chunk, call, budget)

    # A claim that can never yield a safe fallback crop (even at the maximum
    # verified margin) is dropped with fixed telemetry instead of failing the
    # whole job.
    assert envelope.audit.final_regions == ()
    assert all(
        disposition.outcome != "covered_visual"
        or disposition.final_region_indices
        for disposition in envelope.audit.probe_dispositions
    )
    assert any(
        disposition.outcome == "ordinary_text"
        and disposition.final_region_indices == ()
        for disposition in envelope.audit.probe_dispositions
    )
    output = capsys.readouterr().out
    assert "dropped_nonviable_regions=1" in output
    assert "stage=fallback_bounds" in output


def test_fallback_drop_remaps_only_dispositions_for_nonviable_regions() -> None:
    page = Image.new("RGB", (400, 400), "white")
    page.paste("black", (0, 0, 400, 200))
    page.paste("black", (200, 260, 230, 290))
    output = io.BytesIO()
    page.save(output, format="PDF", resolution=100.0)
    chunk = tuple(split_pdf_chunks(output.getvalue(), budget=_budget()))[0]

    regions = (
        AuditedRegion(
            **_region(box=(0.0, 0.0, 100.0, 50.0), ref="drop").model_dump(),
            source_region_ids=("draft-c00-001",),
        ),
        AuditedRegion(
            **_region(box=(45.0, 60.0, 62.0, 80.0), ref="keep").model_dump(),
            source_region_ids=("draft-c00-002",),
        ),
    )
    probes = (
        CoverageProbe(
            probe_id="drop-probe",
            kind="component",
            local_page=1,
            box=(10.0, 10.0, 90.0, 40.0),
            foreground_fraction=1.0,
        ),
        CoverageProbe(
            probe_id="keep-probe",
            kind="component",
            local_page=1,
            box=(50.0, 65.0, 58.0, 75.0),
            foreground_fraction=1.0,
        ),
    )
    audit = InventoryAuditResponse(
        pages_checked=(1,),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=regions,
        probe_dispositions=(
            ProbeDisposition(
                probe_id="drop-probe",
                outcome="covered_visual",
                final_region_indices=(0,),
            ),
            ProbeDisposition(
                probe_id="keep-probe",
                outcome="covered_visual",
                final_region_indices=(1,),
            ),
        ),
    )

    normalized, dropped_ids = _vi()._expand_unsafe_fallback_bounds(
        chunk,
        probes,
        audit,
        _budget(),
        _pipeline_memory(),
    )

    assert len(normalized.final_regions) == 1
    dropped, kept = normalized.probe_dispositions
    assert dropped.outcome == "ordinary_text"
    assert dropped.final_region_indices == ()
    assert kept.outcome == "covered_visual"
    assert kept.final_region_indices == (0,)
    assert dropped_ids == frozenset({"draft-c00-001"})


def test_inventory_chunk_applies_verified_crop_clamp_before_validation() -> None:
    vi = _vi()
    chunk = _chunk(make_probe_pdf(dark_box=(120, 120, 280, 240)))
    inventory = InventoryResponse(
        pages_checked=(1,),
        regions=(_region(box=(20.0, 20.0, 80.0, 70.0)),),
    )

    def overexpand(audit: InventoryAuditResponse, _payload: object) -> InventoryAuditResponse:
        region = audit.final_regions[0].model_copy(update={"box": (1.0, 1.0, 99.0, 99.0)})
        return audit.model_copy(update={"final_regions": (region,)})

    _calls, call = _audit_call_for(inventory, overexpand)
    envelope = vi.inventory_chunk(chunk, call, _budget())

    assert envelope.audit.final_regions[0].box != (1.0, 1.0, 99.0, 99.0)
    vi.validate_inventory_audit(
        envelope.inventory,
        envelope.provisional_ids,
        envelope.probes,
        envelope.audit,
    )


def test_new_audit_region_requires_overlapping_foreground_visual_probe() -> None:
    vi = _vi(); inventory = InventoryResponse(pages_checked=(1,), regions=())
    probe = CoverageProbe(probe_id="p", kind="component", local_page=1, box=(10, 10, 20, 20), foreground_fraction=1)
    region = AuditedRegion(**_region(box=(50, 50, 60, 60)).model_dump(), source_region_ids=())
    audit = InventoryAuditResponse(pages_checked=(1,), coverage_complete=True, unresolved_count=0, final_regions=(region,), probe_dispositions=(ProbeDisposition(probe_id="p", outcome="covered_visual", final_region_indices=(0,)),))
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, (), (probe,), audit)


def test_audit_rejects_final_box_that_clips_rightmost_twenty_percent_arrow_or_label() -> None:
    vi = _vi(); label = InventoryLabel(text="A", page_box=(50, 30, 60, 40), page_anchor=(60, 35))
    inventory, ids, probes, audit = _manual_case(final_box=(25, 25, 55, 55), labels=(label,))
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, audit)


def test_audit_rejects_whole_page_new_region_supported_by_one_small_probe() -> None:
    vi = _vi(); inventory = InventoryResponse(pages_checked=(1,), regions=())
    probe = CoverageProbe(probe_id="p", kind="component", local_page=1, box=(40, 40, 41, 41), foreground_fraction=1)
    region = AuditedRegion(**_region(box=(1, 1, 99, 99)).model_dump(), source_region_ids=())
    audit = InventoryAuditResponse(pages_checked=(1,), coverage_complete=True, unresolved_count=0, final_regions=(region,), probe_dispositions=(ProbeDisposition(probe_id="p", outcome="covered_visual", final_region_indices=(0,)),))
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, (), (probe,), audit)


def test_audit_preserves_every_mandatory_provisional_visual_probe() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    bad = audit.model_copy(update={"probe_dispositions": (audit.probe_dispositions[0].model_copy(update={"outcome": "ordinary_text", "final_region_indices": ()}),), "final_regions": ()})
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, bad)


def test_audit_box_resize_or_merge_preserves_label_page_coordinates() -> None:
    vi = _vi(); label = InventoryLabel(text="A", page_box=(30, 30, 35, 35), page_anchor=(34, 34))
    inventory, ids, probes, audit = _manual_case(labels=(label,))
    changed = audit.final_regions[0].model_copy(update={"exact_labels": (label.model_copy(update={"page_box": (31, 30, 36, 35)}),)})
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ids, probes, audit.model_copy(update={"final_regions": (changed,)}))


def test_audit_rejects_mapped_kind_topic_anchor_or_context_mutation() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    changed_context = InventoryContext(anchor_ref="a", local_page=1, box=(1, 10, 20, 15), role="legend")
    for update in ({"kind": "graph"}, {"topic_anchor": _anchor("b")}, {"context_regions": (changed_context,)}):
        bad = audit.final_regions[0].model_copy(update=update)
        with pytest.raises(VisualFidelityError):
            vi.validate_inventory_audit(inventory, ids, probes, audit.model_copy(update={"final_regions": (bad,)}))


def test_component_probe_keeps_small_diagram_tight_inside_large_grid_cell() -> None:
    probes = _vi().generate_coverage_probes(_chunk(make_probe_pdf(dark_box=(100, 100, 112, 112))))
    tight = [p for p in probes if p.kind == "component" and p.box[0] < 30 and p.box[2] < 35]
    assert tight and max(p.box[2] - p.box[0] for p in tight) < 8


def test_one_pixel_thin_arrow_at_support_boundary_cannot_be_clipped() -> None:
    vi = _vi(); probes = vi.generate_coverage_probes(_chunk(make_probe_pdf(dark_box=(100, 190, 115, 205), thin_arrow=True)))
    arrow = max((p for p in probes if p.kind == "component"), key=lambda p: p.box[2])
    inventory = InventoryResponse(pages_checked=(1,), regions=(_region(box=(20, 40, 50, 60)),))
    final = AuditedRegion(**{**inventory.regions[0].model_dump(), "box": (arrow.box[0], arrow.box[1], arrow.box[2] - 0.01, arrow.box[3])}, source_region_ids=("draft-c00-001",))
    audit = InventoryAuditResponse(pages_checked=(1,), coverage_complete=True, unresolved_count=0, final_regions=(final,), probe_dispositions=tuple(ProbeDisposition(probe_id=p.probe_id, outcome="covered_visual" if p.probe_id == arrow.probe_id else "ordinary_text", final_region_indices=(0,) if p.probe_id == arrow.probe_id else ()) for p in probes))
    with pytest.raises(VisualFidelityError):
        vi.validate_inventory_audit(inventory, ("draft-c00-001",), probes, audit)


def test_finalize_converts_page_relative_labels_to_crop_relative_once_and_round_trips() -> None:
    vi = _vi(); label = InventoryLabel(text="A", page_box=(30, 35, 40, 45), page_anchor=(35, 40))
    case = _manual_case(final_box=(20, 20, 60, 60), labels=(label,))
    chunk = _chunk(); envelope = _bind_manual(vi, chunk, case)
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    source = vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64).visuals[0].exact_labels[0]
    assert source.box == pytest.approx((25, 37.5, 50, 62.5))
    assert source.anchor == pytest.approx((37.5, 50))


def test_finalize_rejects_missing_inventory_or_audit_for_any_chunk() -> None:
    vi = _vi(); chunk = _chunk(); envelope = _bind_manual(vi, chunk)
    budget = _budget(); budget.inventory_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64)


def test_finalize_rejects_swapped_chunk_bytes_offsets_counts_or_noncontiguous_ranges() -> None:
    vi = _vi(); c0 = _chunk(index=0); c1 = _chunk(pdf_bytes=_blank_pdf() + b" ", index=1, first_page=2)
    case0 = _manual_case(chunk_index=0); case1 = _manual_case(chunk_index=1)
    envelopes = (_bind_manual(vi, c1, case1), _bind_manual(vi, c0, case0))
    budget = _budget(); budget.inventory_calls.update({0: 1, 1: 1}); budget.audit_calls.update({0: 1, 1: 1}); budget.mathpix_source_fallback.update({0: 1, 1: 1})
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest(envelopes, (c0, c1), budget, total_pages=2, max_visuals=64)
    bad_offset = replace(c1, first_global_page=3)
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest(
            (_bind_manual(vi, c0, case0), _bind_manual(vi, bad_offset, case1)),
            (c0, bad_offset),
            budget,
            total_pages=3,
            max_visuals=64,
        )
    bad_count = replace(c1, page_count=2)
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest(
            (_bind_manual(vi, c0, case0), _bind_manual(vi, bad_count, case1)),
            (c0, bad_count),
            budget,
            total_pages=3,
            max_visuals=64,
        )


def test_finalize_rejects_swapped_audit_payload_inside_canonical_chunk_wrappers() -> None:
    vi = _vi(); c0 = _chunk(index=0); c1 = _chunk(pdf_bytes=_blank_pdf() + b" ", index=1, first_page=2)
    case0 = _manual_case(chunk_index=0)
    inventory, ids, probes, audit = _manual_case(chunk_index=1)
    other_anchor = _anchor("other", heading="Other")
    inventory = inventory.model_copy(update={"regions": (inventory.regions[0].model_copy(update={"topic_anchor": other_anchor}),)})
    other = audit.model_copy(
        update={
            "final_regions": (
                audit.final_regions[0].model_copy(
                    update={"topic_anchor": other_anchor}
                ),
            )
        }
    )
    e0 = _bind_manual(vi, c0, case0)
    e1 = _bind_manual(vi, c1, (inventory, ids, probes, other))
    forged = replace(e0, audit=e1.audit)
    budget = _budget(); budget.inventory_calls.update({0: 1, 1: 1}); budget.audit_calls.update({0: 1, 1: 1}); budget.mathpix_source_fallback.update({0: 1, 1: 1})
    with pytest.raises(VisualFidelityError):
        vi.finalize_manifest((forged, e1), (c0, c1), budget, total_pages=2, max_visuals=64)


def test_empty_visual_chunk_still_receives_inventory_and_audit_once() -> None:
    calls, call = _empty_call_log(); _vi().inventory_chunk(_chunk(), call, _budget())
    assert len(calls) == 2 and calls[1][2] is InventoryAuditResponse


def test_known_fixture_truth_set_cannot_be_omitted_while_claiming_complete() -> None:
    vi = _vi()
    fixture = make_four_page_visual_pdf()
    chunk = _chunk(fixture.pdf_bytes, pages=4, width=800, height=1000)
    regions = tuple(
        _region(box=t.box, page=t.local_page, kind=t.kind, ref=f"a{n}", hint=n)
        for n, t in enumerate(fixture.truth[:-1])
    )
    inventory = InventoryResponse(pages_checked=(1, 2, 3, 4), regions=regions)
    calls: list[tuple[object, ...]] = []

    def call(files, prompt, schema, *_args):
        calls.append((files, prompt, schema))
        if schema is InventoryResponse:
            return inventory
        payload = vi.decode_audit_payload(prompt)
        base = _complete_audit(payload)
        probes = tuple(
            CoverageProbe.model_validate_json(json.dumps(item))
            for item in payload["probes"]
        )
        truth = fixture.truth[-1]
        support = tuple(
            probe
            for probe in probes
            if probe.kind == "component"
            and probe.local_page == truth.local_page
            and _intersection_area(probe.box, truth.box) > 0
        )
        assert support
        support_box = (
            min(probe.box[0] for probe in support),
            min(probe.box[1] for probe in support),
            max(probe.box[2] for probe in support),
            max(probe.box[3] for probe in support),
        )
        labels = (
            InventoryLabel(text="N", page_box=(40.0, 28.0, 44.0, 33.0)),
            InventoryLabel(text="S", page_box=(55.0, 60.0, 59.0, 65.0)),
        )
        recovered = AuditedRegion(
            **_region(
                box=support_box,
                page=truth.local_page,
                kind=truth.kind,
                ref="recovered",
                labels=labels,
            ).model_dump(),
            source_region_ids=(),
        )
        recovered_index = len(base.final_regions)
        support_ids = {probe.probe_id for probe in support}
        dispositions = tuple(
            disposition.model_copy(
                update={
                    "outcome": "covered_visual",
                    "final_region_indices": (recovered_index,),
                }
            )
            if disposition.probe_id in support_ids
            else disposition
            for disposition in base.probe_dispositions
        )
        return base.model_copy(
            update={
                "final_regions": base.final_regions + (recovered,),
                "probe_dispositions": dispositions,
            }
        )

    budget = _budget()
    envelope = vi.inventory_chunk(chunk, call, budget)
    manifest = vi.finalize_manifest(
        (envelope,), (chunk,), budget, total_pages=4, max_visuals=64
    )

    assert len(calls) == 2
    assert len(manifest.visuals) == len(fixture.truth)
    recovered = next(visual for visual in manifest.visuals if visual.global_page == 4)
    assert tuple(label.text for label in recovered.exact_labels) == fixture.truth[-1].labels


def test_same_page_final_order_comes_from_geometry_not_model_order_hint() -> None:
    vi = _vi()
    left_source = _region(box=(10, 20, 20, 30), ref="left", hint=99)
    right_source = _region(box=(60, 20, 70, 30), ref="right", hint=0)
    inventory = InventoryResponse(pages_checked=(1,), regions=(left_source, right_source))
    probes = (
        CoverageProbe(probe_id="left", kind="component", local_page=1, box=left_source.box, foreground_fraction=1),
        CoverageProbe(probe_id="right", kind="component", local_page=1, box=right_source.box, foreground_fraction=1),
    )
    left = AuditedRegion(**left_source.model_dump(), source_region_ids=("draft-c00-001",))
    right = AuditedRegion(**right_source.model_dump(), source_region_ids=("draft-c00-002",))
    audit = InventoryAuditResponse(
        pages_checked=(1,), coverage_complete=True, unresolved_count=0,
        final_regions=(right, left),
        probe_dispositions=(
            ProbeDisposition(probe_id="left", outcome="covered_visual", final_region_indices=(1,)),
            ProbeDisposition(probe_id="right", outcome="covered_visual", final_region_indices=(0,)),
        ),
    )
    chunk = _chunk()
    envelope = vi.bind_audited_chunk(
        chunk, inventory, ("draft-c00-001", "draft-c00-002"), probes, audit
    )
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    manifest = vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=64)
    assert [v.box for v in manifest.visuals] == [(10, 20, 20, 30), (60, 20, 70, 30)]


def test_reading_band_seed_rule_does_not_chain_nontransitive_overlaps() -> None:
    boxes = ((10, 0, 20, 10), (30, 5, 40, 15), (50, 10, 60, 20))
    assert _vi().geometry_reading_order(boxes) == (0, 1, 2)
    assert _vi().reading_bands(boxes) == ((0, 1), (2,))


def test_incomplete_pages_ambiguity_or_65th_visual_fails_closed() -> None:
    vi = _vi(); inventory, ids, probes, audit = _manual_case()
    for bad in (
        audit.model_copy(update={"pages_checked": ()}),
        audit.model_copy(update={"coverage_complete": False}),
        audit.model_copy(update={"unresolved_count": 1}),
    ):
        with pytest.raises(VisualFidelityError):
            vi.validate_inventory_audit(inventory, ids, probes, bad)
    chunk = _chunk()
    many_probes = tuple(
        CoverageProbe(
            probe_id=f"p{i}", kind="component", local_page=1,
            box=(float((i % 5) * 20 + 1), float((i // 5) * 7 + 1), float((i % 5) * 20 + 11), float((i // 5) * 7 + 4)),
            foreground_fraction=1,
        )
        for i in range(65)
    )
    many = tuple(
        AuditedRegion(
            **_region(box=probe.box, ref=f"a{i}").model_dump(),
            source_region_ids=(),
        )
        for i, probe in enumerate(many_probes)
    )
    many_audit = InventoryAuditResponse(
        pages_checked=(1,), coverage_complete=True, unresolved_count=0,
        final_regions=many,
        probe_dispositions=tuple(
            ProbeDisposition(probe_id=probe.probe_id, outcome="covered_visual", final_region_indices=(i,))
            for i, probe in enumerate(many_probes)
        ),
    )
    envelope = vi.bind_audited_chunk(
        chunk, InventoryResponse(pages_checked=(1,), regions=()), (), many_probes, many_audit
    )
    budget = _budget(); budget.inventory_calls[0] = budget.audit_calls[0] = 1; budget.mathpix_source_fallback[0] = 1
    with pytest.raises(VisualFidelityError) as caught:
        vi.finalize_manifest((envelope,), (chunk,), budget, total_pages=1, max_visuals=999)
    assert caught.value.code == "too_many_visuals"
