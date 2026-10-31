"""Contract tests for the bounded source-visual redraw state machine."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import struct
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFilter, UnidentifiedImageError
from pypdf import PdfReader

from notesbot import (
    config,
    gemini_client,
    image_gen,
    mathpix_client,
    notes_pdf as notes_pdf_module,
    visual_inventory,
)
from notesbot.errors import VisualFidelityError
from notesbot.pdf_utils import split_pdf_chunks
from notesbot.prompts import Section, ShortNotes
from notesbot.snapshot import extract_source_crops
from notesbot.visual_assets import prepare_source_fallback
from notesbot.visual_manifest import (
    AuditedRegion,
    CoverageProbe,
    InventoryAuditResponse,
    InventoryContext,
    InventoryLabel,
    InventoryResponse,
    InventoryTopicAnchor,
    ProbeDisposition,
    RedrawVerdict,
    SourceContext,
    SourceContextCrop,
    SourceCrop,
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    RenderedVisual,
    VisualManifest,
    VisualMemoryBudget,
    VisualRunBudget,
    VisualRegion,
    manifest_ids,
)
from notesbot.notes_pdf import NotesPDF, build_notes_pdf
from tests.visual_fixture import (
    SourceFidelityComponentObservation,
    SourceFidelityComponentTruth,
    SourceFidelityPdfFixture,
    SourceFidelityTruth,
    inspect_source_fidelity_component,
    make_source_fidelity_pdf,
)


NOTE_PROSE_SENTINEL = "GENERATED NOTE PROSE MUST NEVER REACH A VISUAL MODEL"

FIDELITY_COLOURS = {
    "component-left": (20, 90, 210, 255),
    "component-right": (210, 70, 50, 255),
    "arrow": (25, 150, 75, 255),
    "curve": (135, 60, 190, 255),
    "anchor": (235, 135, 25, 255),
    "extra-component": (245, 205, 35, 255),
    "outside-slot-text": (10, 15, 25, 255),
}


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _source_png(width: int = 80, height: int = 60, *, marker: int = 0) -> bytes:
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    # Exact source-label glyph evidence inside the declared label box.
    draw.line((5, 5, 15, 5), fill=(20, 30, 60, 255), width=2)
    draw.line((5, 5, 5, 10), fill=(20, 30, 60, 255), width=2)
    # Instructional geometry outside the label box.
    draw.ellipse((28, 24, 51, 45), outline=(30, 40, 70, 255), width=2)
    draw.line((51, 34, 69, 34 + marker % 3), fill=(70, 90, 130, 255), width=2)
    result = _png(image)
    image.close()
    return result


def _context_png(width: int = 32, height: int = 24, *, marker: int = 0) -> bytes:
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.line((3, 4 + marker, width - 4, height - 5), fill=(40, 60, 90, 255), width=2)
    result = _png(image)
    image.close()
    return result


def _candidate_png(
    width: int = 80,
    height: int = 60,
    *,
    background: tuple[int, int, int, int] = (0, 0, 0, 0),
    label_collision: bool = False,
) -> bytes:
    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((28, 24, 51, 45), outline=(30, 40, 70, 255), width=2)
    draw.line((51, 34, 69, 34), fill=(70, 90, 130, 255), width=2)
    if label_collision:
        # Shifted within the approved box so exact-source-mask overlap alone
        # cannot catch generated replacement text.
        draw.line((8, 9, 16, 9), fill=(120, 20, 20, 255), width=2)
    result = _png(image)
    image.close()
    return result


def _fidelity_source_png(width: int = 80, height: int = 60) -> bytes:
    """Deterministic authority with independently identifiable visual marks."""
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    # Immutable source glyphs occupy only the approved label slot.
    draw.line((5, 5, 15, 5), fill=(20, 30, 60, 255), width=2)
    draw.line((5, 5, 5, 10), fill=(20, 30, 60, 255), width=2)
    _draw_fidelity_geometry(draw)
    result = _png(image)
    image.close()
    return result


def _draw_fidelity_geometry(
    draw: ImageDraw.ImageDraw,
    *,
    component_left: bool = True,
    component_right: bool = True,
    arrow: bool = True,
    curve: bool = True,
    anchor: bool = True,
    shifted_anchor: bool = False,
    extra_component: bool = False,
    outside_text: bool = False,
) -> None:
    if component_left:
        draw.rectangle((25, 20, 33, 29), fill=FIDELITY_COLOURS["component-left"])
    if component_right:
        draw.ellipse((45, 19, 55, 29), fill=FIDELITY_COLOURS["component-right"])
    if arrow:
        colour = FIDELITY_COLOURS["arrow"]
        draw.line((34, 24, 43, 24), fill=colour, width=2)
        draw.polygon(((43, 24), (39, 21), (39, 27)), fill=colour)
    if curve:
        draw.arc((42, 34, 68, 54), 190, 350, fill=FIDELITY_COLOURS["curve"], width=3)
    if anchor:
        box = (22, 14, 24, 16) if shifted_anchor else (18, 12, 20, 14)
        draw.rectangle(box, fill=FIDELITY_COLOURS["anchor"])
    if extra_component:
        draw.rectangle((69, 18, 74, 24), fill=FIDELITY_COLOURS["extra-component"])
    if outside_text:
        colour = FIDELITY_COLOURS["outside-slot-text"]
        # Three deterministic glyph-like marks well outside the approved slot.
        draw.line((20, 49, 20, 55), fill=colour, width=2)
        draw.line((24, 49, 28, 49), fill=colour, width=2)
        draw.line((26, 49, 26, 55), fill=colour, width=2)
        draw.line((31, 49, 35, 55), fill=colour, width=2)
        draw.line((35, 49, 31, 55), fill=colour, width=2)


def _fidelity_candidate_png(**features: bool) -> bytes:
    image = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_fidelity_geometry(draw, **features)
    result = _png(image)
    image.close()
    return result


def _contains_colour(
    image: Image.Image,
    colour: tuple[int, int, int, int],
    box: tuple[int, int, int, int] | None = None,
) -> bool:
    rgba = image.convert("RGBA")
    try:
        region = rgba if box is None else rgba.crop(box)
        try:
            return colour in tuple(region.getdata())
        finally:
            if region is not rgba:
                region.close()
    finally:
        rgba.close()


def _inspect_fidelity_candidate(candidate_png: bytes) -> RedrawVerdict:
    with Image.open(io.BytesIO(candidate_png)) as raw:
        candidate = raw.convert("RGBA")
    try:
        ordered_required = (
            "component-left",
            "component-right",
            "arrow",
            "curve",
        )
        missing = tuple(
            name
            for name in ordered_required
            if not _contains_colour(candidate, FIDELITY_COLOURS[name])
        )
        extra = (
            ("extra-component",)
            if _contains_colour(candidate, FIDELITY_COLOURS["extra-component"])
            else ()
        )
        unapproved = (
            ("outside-slot-text",)
            if _contains_colour(
                candidate,
                FIDELITY_COLOURS["outside-slot-text"],
                (18, 47, 38, 58),
            )
            else ()
        )
        expected_anchor = _contains_colour(
            candidate, FIDELITY_COLOURS["anchor"], (18, 12, 21, 15)
        )
        shifted_anchor = _contains_colour(
            candidate, FIDELITY_COLOURS["anchor"], (22, 14, 25, 17)
        )
        label_region = candidate.crop((1, 1, 18, 12))
        try:
            label_clear = all(pixel[3] == 0 for pixel in label_region.getdata())
        finally:
            label_region.close()
        anchors_aligned = expected_anchor and not shifted_anchor
        geometry_aligned = not missing
        candidate_text_free = not unapproved
        faithful = not (missing or extra or unapproved) and anchors_aligned and label_clear
        return RedrawVerdict(
            faithful=faithful,
            missing_elements=missing,
            extra_elements=extra,
            unapproved_text=unapproved,
            candidate_text_free=candidate_text_free,
            label_regions_clear=label_clear,
            anchors_aligned=anchors_aligned,
            geometry_aligned=geometry_aligned,
        )
    finally:
        candidate.close()


def _checkerboard_png(width: int = 80, height: int = 60) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cell = 5
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            colour = (210, 210, 210, 255) if (x // cell + y // cell) % 2 else (255, 255, 255, 255)
            draw.rectangle((x, y, min(width - 1, x + cell - 1), min(height - 1, y + cell - 1)), fill=colour)
    result = _png(image)
    image.close()
    return result


def _inner_checkerboard_png(width: int = 80, height: int = 60) -> bytes:
    """Checker pattern whose complete outer pixel ring remains transparent."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colours = ((250, 220, 180, 255), (130, 180, 245, 255))
    cell = 2
    for y in range(1, height - 1, cell):
        for x in range(1, width - 1, cell):
            colour = colours[((x - 1) // cell + (y - 1) // cell) % 2]
            draw.rectangle(
                (x, y, min(width - 2, x + cell - 1), min(height - 2, y + cell - 1)),
                fill=colour,
            )
    # Keep the complete approved source-label slot blank.
    draw.rectangle((1, 1, 17, 11), fill=(0, 0, 0, 0))
    result = _png(image)
    image.close()
    return result


def _inner_neutral_panel_png(
    width: int = 80,
    height: int = 60,
    *,
    colour: tuple[int, int, int, int] = (250, 250, 250, 255),
) -> bytes:
    """Large neutral panel with a transparent border and blank label slot."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((1, 1, width - 2, height - 2), fill=colour)
    draw.rectangle((1, 1, 17, 11), fill=(0, 0, 0, 0))
    draw.ellipse((28, 24, 51, 45), outline=(30, 90, 190, 255), width=2)
    result = _png(image)
    image.close()
    return result


def _candidate_with_opaque_border_pixels(count: int) -> bytes:
    with Image.open(io.BytesIO(_candidate_png())) as source:
        image = source.convert("RGBA")
    try:
        width, height = image.size
        border = [
            *((x, 0) for x in range(width)),
            *((x, height - 1) for x in range(width)),
            *((0, y) for y in range(1, height - 1)),
            *((width - 1, y) for y in range(1, height - 1)),
        ]
        assert len(border) == 276
        for pixel in border[:count]:
            image.putpixel(pixel, (25, 120, 220, 255))
        return _png(image)
    finally:
        image.close()


def _transparent_png(width: int = 80, height: int = 60) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    result = _png(image)
    image.close()
    return result


def _pass_verdict() -> RedrawVerdict:
    return RedrawVerdict(
        faithful=True,
        missing_elements=(),
        extra_elements=(),
        unapproved_text=(),
        candidate_text_free=True,
        label_regions_clear=True,
        anchors_aligned=True,
        geometry_aligned=True,
    )


def _failed_verdict(
    *,
    missing: tuple[str, ...] = ("teacher arrow",),
    extra: tuple[str, ...] = (),
    unapproved_text: tuple[str, ...] = (),
) -> RedrawVerdict:
    return RedrawVerdict(
        faithful=False,
        missing_elements=missing,
        extra_elements=extra,
        unapproved_text=unapproved_text,
        candidate_text_free=not unapproved_text,
        label_regions_clear=True,
        anchors_aligned=False,
        geometry_aligned=False,
    )


def _memory(*, asset: int = 64 * 1024 * 1024, total: int = 128 * 1024 * 1024) -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=total,
        max_source_bytes=32 * 1024 * 1024,
        max_context_bytes=16 * 1024 * 1024,
        max_asset_bytes=asset,
        max_page_cache_bytes=16 * 1024 * 1024,
    )


def _budget() -> VisualRunBudget:
    return VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)


def _case(
    count: int = 1,
    *,
    context_count: int = 0,
    kind: str = "diagram",
    memory: VisualMemoryBudget | None = None,
    fidelity: bool = False,
) -> tuple[ShortNotes, VisualManifest, OrderedDict[str, SourceCrop], VisualRunBudget, VisualMemoryBudget]:
    actual_memory = memory or _memory()
    visuals: list[SourceVisual] = []
    crops: OrderedDict[str, SourceCrop] = OrderedDict()
    for index in range(count):
        visual_id = f"sv-{index + 1:04d}"
        contexts = tuple(
            SourceContext(
                topic_key=f"topic-{index}",
                local_page=1,
                box=(1.0 + offset, 30.0, 31.0 + offset, 55.0),
                role="graph_description" if kind == "graph" else "explanation",
                exact_text=f"teacher-only context {index}-{offset}",
            )
            for offset in range(context_count)
        )
        visual = SourceVisual(
            visual_id=visual_id,
            chunk_index=0,
            local_page=1,
            global_page=1,
            box=(10.0, 10.0, 90.0, 90.0),
            kind=kind,
            topic_anchor=SourceTopicAnchor(
                topic_key=f"topic-{index}",
                local_page=1,
                box=(0.0, 0.0, 50.0, 8.0),
                exact_heading=f"Exact source heading {index}",
            ),
            exact_labels=(
                SourceLabel(
                    text=f"SOURCE-LABEL-{index}",
                    box=(2.0, 2.0, 22.0, 20.0),
                    anchor=(22.0, 20.0),
                ),
            ),
            context_regions=contexts,
            section_hint="model-authored section hint must stay out",
            source_order=index + 1,
        )
        source_png = bytes(
            bytearray(
                _fidelity_source_png() if fidelity else _source_png(marker=index)
            )
        )
        assert actual_memory.reserve_buffer("source", source_png) is not None
        context_crops: list[SourceContextCrop] = []
        for offset, context in enumerate(contexts):
            context_png = bytes(bytearray(_context_png(marker=offset)))
            assert actual_memory.reserve_buffer("context", context_png) is not None
            context_crops.append(
                SourceContextCrop(
                    context=context,
                    source_png=context_png,
                    width_px=32,
                    height_px=24,
                )
            )
        crop = SourceCrop(
            visual=visual,
            source_png=source_png,
            width_px=80,
            height_px=60,
            context_crops=tuple(context_crops),
        )
        visuals.append(visual)
        crops[visual_id] = crop

    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Source visuals",
        sections=[
            Section(
                heading="Generated notes",
                visual_ids=[visual.visual_id for visual in visuals],
                points=[NOTE_PROSE_SENTINEL],
            )
        ],
    )
    return notes, VisualManifest(visuals=tuple(visuals)), crops, _budget(), actual_memory


def _render(
    notes: ShortNotes,
    manifest: VisualManifest,
    crops: OrderedDict[str, SourceCrop],
    budget: VisualRunBudget,
    memory: VisualMemoryBudget,
    *,
    redraw,
    verify,
):
    return image_gen.render_visuals(
        notes,
        manifest,
        crops,
        budget,
        memory,
        redraw=redraw,
        verify=verify,
    )


def _install_litellm_image_provider(monkeypatch, candidate: bytes, calls: list[dict]) -> None:
    encoded = base64.b64encode(candidate).decode("ascii")
    response = {
        "choices": [
            {
                "message": {
                    "images": [
                        {"image_url": {"url": f"data:image/png;base64,{encoded}"}}
                    ]
                }
            }
        ]
    }

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    raw_client = SimpleNamespace(
        with_options=lambda **_kwargs: client,
        close=lambda: None,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)


def test_redraw_receives_only_exact_crop_kind_and_approved_label_regions(monkeypatch):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    calls: list[dict] = []
    candidate = _candidate_png()
    _install_litellm_image_provider(monkeypatch, candidate, calls)

    try:
        assert image_gen._one_redraw(crop, fallback, budget) == candidate
        assert len(calls) == 1
        content = calls[0]["messages"][0]["content"]
        images = [item for item in content if item["type"] == "image_url"]
        prompt = next(item["text"] for item in content if item["type"] == "text")
        assert len(images) == 1
        assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) is not None
        assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == crop.source_png
        assert '"visual_kind":"diagram"' in prompt
        assert '"box":[2.0,2.0,22.0,20.0]' in prompt
        assert '"anchor":[22.0,20.0]' in prompt
        assert "SOURCE-LABEL-0" not in prompt
        assert "section hint" not in prompt
        assert NOTE_PROSE_SENTINEL not in prompt
    finally:
        memory.close()


def test_redraw_receives_bounded_same_topic_context_but_no_generated_note_prose(monkeypatch):
    notes, manifest, crops, budget, memory = _case(context_count=2)
    candidate = _candidate_png()
    calls: list[dict] = []
    _install_litellm_image_provider(monkeypatch, candidate, calls)

    try:
        assets = image_gen.render_visuals(
            notes,
            manifest,
            crops,
            budget,
            memory,
            verify=lambda *_args: _pass_verdict(),
        )
        assert assets["sv-0001"].state == "verified_redraw"
        content = calls[0]["messages"][0]["content"]
        images = [item for item in content if item["type"] == "image_url"]
        prompt = next(item["text"] for item in content if item["type"] == "text")
        assert len(images) == 3  # one exact visual plus two audited contexts
        decoded = [base64.b64decode(item["image_url"]["url"].split(",", 1)[1]) for item in images]
        assert decoded == [
            crops["sv-0001"].source_png,
            *(item.source_png for item in crops["sv-0001"].context_crops),
        ]
        assert NOTE_PROSE_SENTINEL not in prompt
        assert "teacher-only context" not in prompt
        assert "cannot authorize" in prompt.lower()
    finally:
        memory.close()


def test_redraw_and_verifier_receive_at_most_two_context_files_within_all_budgets(monkeypatch):
    _notes, _manifest, crops, budget, memory = _case(context_count=2)
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    candidate = _candidate_png()
    image_calls: list[dict] = []
    schema_calls: list[tuple] = []
    _install_litellm_image_provider(monkeypatch, candidate, image_calls)

    def schema_call(files, prompt, schema, max_tokens, temperature, actual_budget):
        schema_calls.append((files, prompt, schema, max_tokens, temperature, actual_budget))
        return _pass_verdict()

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", schema_call)
    try:
        assert image_gen._one_redraw(crop, fallback, budget) == candidate
        assert image_gen._verify_redraw(crop, candidate, fallback, budget) == _pass_verdict()
        image_content = image_calls[0]["messages"][0]["content"]
        assert sum(item["type"] == "image_url" for item in image_content) == 3
        files = schema_calls[0][0]
        assert [item.data for item in files] == [
            crop.source_png,
            *(item.source_png for item in crop.context_crops),
            candidate,
        ]
        assert len([item for item in files if item.filename.startswith("context-")]) == 2
        assert sum(len(item.data) for item in files) <= config.VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES
        assert sum(4 * ((len(item.data) + 2) // 3) for item in files) <= config.VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES
        assert budget.redraw_calls == {"sv-0001": 1}
        assert budget.verifier_calls == {"sv-0001": 1}
    finally:
        memory.close()


def _direct_image_response(candidate: bytes):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            inline_data=SimpleNamespace(
                                mime_type="image/png",
                                data=candidate,
                            )
                        )
                    ]
                )
            )
        ]
    )


@pytest.mark.parametrize("gateway", ("litellm", "direct"))
def test_default_redraw_transport_allows_only_initial_and_correction_and_closes_every_client(
    monkeypatch,
    gateway,
):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    candidate = _candidate_png()
    dispatches = []
    closes = []
    setup = []
    correction = _failed_verdict(
        missing=("arrow", "curve"),
        extra=("extra-component",),
        unapproved_text=("outside-slot-text",),
    )

    if gateway == "litellm":
        encoded = base64.b64encode(candidate).decode("ascii")
        response = {
            "choices": [
                {
                    "message": {
                        "images": [
                            {"image_url": {"url": f"data:image/png;base64,{encoded}"}}
                        ]
                    }
                }
            ]
        }

        def build_client():
            client_number = len(setup) + 1

            def create(**kwargs):
                dispatches.append(kwargs)
                return response

            client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )

            def with_options(**kwargs):
                setup.append((client_number, kwargs))
                return client

            return SimpleNamespace(
                with_options=with_options,
                close=lambda: closes.append(client_number),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        build_timeouts = []

        def build_client(timeout_ms):
            client_number = len(build_timeouts) + 1
            build_timeouts.append(timeout_ms)

            def generate_content(**kwargs):
                dispatches.append(kwargs)
                return _direct_image_response(candidate)

            return SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content),
                close=lambda: closes.append(client_number),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    try:
        assert image_gen._one_redraw(crop, fallback, budget, None) == candidate
        assert image_gen._one_redraw(crop, fallback, budget, correction) == candidate
        # A third default call may build a fresh client, but take_redraw() must
        # reject it before a third network dispatch and that client must close.
        assert image_gen._one_redraw(crop, fallback, budget, correction) is None
        assert len(dispatches) == 2
        assert budget.redraw_calls == {"sv-0001": 2}
        assert closes == [1, 2, 3]
        if gateway == "litellm":
            assert len(setup) == 3
            assert all(options["max_retries"] == 0 for _, options in setup)
            assert all(call["extra_body"] == {"modalities": ["image", "text"]} for call in dispatches)
            assert "ONE_CORRECTION_ONLY" not in dispatches[0]["messages"][0]["content"][-1]["text"]
            assert "ONE_CORRECTION_ONLY" in dispatches[1]["messages"][0]["content"][-1]["text"]
        else:
            assert len(build_timeouts) == 3
            assert all(
                call["config"].http_options.retry_options.attempts == 1
                for call in dispatches
            )
            assert "ONE_CORRECTION_ONLY" not in dispatches[0]["contents"][-1]
            assert "ONE_CORRECTION_ONLY" in dispatches[1]["contents"][-1]
    finally:
        memory.close()


@pytest.mark.parametrize("gateway", ("litellm", "direct"))
def test_default_redraw_refreshes_deadline_after_setup_and_closes_on_provider_failure(
    monkeypatch,
    gateway,
):
    now = [1.0]
    budget = VisualRunBudget(deadline_at=10.0, clock=lambda: now[0])
    memory = _memory()
    notes, manifest, crops, _unused_budget, memory = _case(memory=memory)
    del notes, manifest, _unused_budget
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    candidate = _candidate_png()
    closes = []
    dispatches = []
    monkeypatch.setattr(image_gen.config, "VISUAL_MODEL_TIMEOUT_SECONDS", 8)

    if gateway == "litellm":
        option_calls = []

        def build_client():
            now[0] = 4.0

            def with_options(**kwargs):
                option_calls.append(kwargs)
                now[0] = 6.0
                return SimpleNamespace(
                    chat=SimpleNamespace(
                        completions=SimpleNamespace(
                            create=lambda **call: dispatches.append(call)
                            or (_ for _ in ()).throw(RuntimeError("provider failure"))
                        )
                    )
                )

            return SimpleNamespace(
                with_options=with_options,
                close=lambda: closes.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        build_timeouts = []

        def build_client(timeout_ms):
            build_timeouts.append(timeout_ms)
            now[0] = 4.0
            return SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **call: dispatches.append(call)
                    or (_ for _ in ()).throw(RuntimeError("provider failure"))
                ),
                close=lambda: closes.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    try:
        assert image_gen._one_redraw(crop, fallback, budget) is None
        assert len(dispatches) == 1
        assert budget.redraw_calls == {"sv-0001": 1}
        assert closes == ["closed"]
        if gateway == "litellm":
            assert option_calls == [{"timeout": 6.0, "max_retries": 0}]
            assert dispatches[0]["timeout"] == 4.0
        else:
            assert build_timeouts == [8000]
            request_options = dispatches[0]["config"].http_options
            assert request_options.timeout == 6000
            assert request_options.retry_options.attempts == 1
    finally:
        memory.close()


@pytest.mark.parametrize("gateway", ("litellm", "direct"))
def test_default_redraw_close_failure_does_not_mask_cancellation(monkeypatch, gateway):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    close_attempts = []

    def cancel(**_kwargs):
        raise asyncio.CancelledError("cancel redraw")

    def fail_close():
        close_attempts.append("closed")
        raise RuntimeError("private close failure")

    if gateway == "litellm":
        per_call = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=cancel))
        )
        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(
            gemini_client,
            "_build_visual_lite_client",
            lambda: SimpleNamespace(
                with_options=lambda **_kwargs: per_call,
                close=fail_close,
            ),
        )
    else:
        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(
            gemini_client,
            "_build_visual_client",
            lambda _timeout_ms: SimpleNamespace(
                models=SimpleNamespace(generate_content=cancel),
                close=fail_close,
            ),
        )

    try:
        with pytest.raises(asyncio.CancelledError):
            image_gen._one_redraw(crop, fallback, budget)
        assert close_attempts == ["closed"]
        assert budget.redraw_calls == {"sv-0001": 1}
    finally:
        memory.close()


def test_default_verifier_ledger_stops_before_a_third_schema_dispatch(monkeypatch):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    candidate = _candidate_png()
    schema_calls = []

    def schema_call(*args):
        schema_calls.append(args)
        return _pass_verdict()

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", schema_call)
    try:
        assert image_gen._verify_redraw(crop, candidate, fallback, budget) == _pass_verdict()
        assert image_gen._verify_redraw(crop, candidate, fallback, budget) == _pass_verdict()
        with pytest.raises(VisualFidelityError) as error:
            image_gen._verify_redraw(crop, candidate, fallback, budget)
        assert error.value.code == "work_limit_exceeded"
        assert len(schema_calls) == 2
        assert budget.verifier_calls == {"sv-0001": 2}
    finally:
        memory.close()


@pytest.mark.parametrize("gateway", ("litellm", "direct"))
def test_default_provider_state_machine_dispatches_exactly_two_redraws_and_two_verifiers(
    monkeypatch,
    gateway,
):
    notes, manifest, crops, _unused_budget, memory = _case()
    now = [10.0]
    budget = VisualRunBudget(deadline_at=50.0, clock=lambda: now[0])
    monkeypatch.setattr(image_gen.config, "VISUAL_MODEL_TIMEOUT_SECONDS", 60)
    candidate = _candidate_png()
    failed = _failed_verdict(missing=("arrow",), extra=("extra-component",))
    dispatches: list[tuple[str, object, float]] = []
    closes: list[int] = []
    setups: list[tuple[int, object]] = []

    if gateway == "litellm":
        encoded = base64.b64encode(candidate).decode("ascii")
        image_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        images=[
                            SimpleNamespace(
                                image_url=SimpleNamespace(
                                    url=f"data:image/png;base64,{encoded}"
                                )
                            )
                        ]
                    )
                )
            ]
        )
        verdict_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=failed.model_dump_json())
                )
            ]
        )

        def build_client():
            client_number = len(setups) + 1
            now[0] += 1.0

            def create(**kwargs):
                role = (
                    "redraw"
                    if kwargs.get("extra_body") == {"modalities": ["image", "text"]}
                    else "verify"
                )
                dispatches.append((role, kwargs, budget.remaining_seconds()))
                return image_response if role == "redraw" else verdict_response

            client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )

            def with_options(**kwargs):
                setups.append((client_number, kwargs))
                now[0] += 1.0
                return client

            return SimpleNamespace(
                with_options=with_options,
                close=lambda: closes.append(client_number),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        build_timeouts: list[int] = []

        def build_client(timeout_ms):
            client_number = len(build_timeouts) + 1
            build_timeouts.append(timeout_ms)
            now[0] += 2.0

            def generate_content(**kwargs):
                request_config = kwargs["config"]
                role = "redraw" if request_config.response_modalities else "verify"
                dispatches.append((role, kwargs, budget.remaining_seconds()))
                if role == "redraw":
                    return _direct_image_response(candidate)
                return SimpleNamespace(parsed=failed, text="")

            return SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content),
                close=lambda: closes.append(client_number),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    try:
        assets = image_gen.render_visuals(notes, manifest, crops, budget, memory)
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert [role for role, _, _ in dispatches] == [
            "redraw",
            "verify",
            "redraw",
            "verify",
        ]
        assert budget.redraw_calls == {"sv-0001": 2}
        assert budget.verifier_calls == {"sv-0001": 2}
        assert closes == [1, 2, 3, 4]
        if gateway == "litellm":
            assert len(setups) == 4
            assert all(options["max_retries"] == 0 for _, options in setups)
            assert [options["timeout"] for _, options in setups] == [
                39.0,
                37.0,
                35.0,
                33.0,
            ]
            assert [call["timeout"] for _, call, _ in dispatches] == [
                38.0,
                36.0,
                34.0,
                32.0,
            ]
            assert [remaining for _, _, remaining in dispatches] == [
                38.0,
                36.0,
                34.0,
                32.0,
            ]
            redraw_prompts = [
                call["messages"][0]["content"][-1]["text"]
                for role, call, _ in dispatches
                if role == "redraw"
            ]
        else:
            assert build_timeouts == [40000, 38000, 36000, 34000]
            assert all(
                call["config"].http_options.retry_options.attempts == 1
                for _, call, _ in dispatches
            )
            assert [
                call["config"].http_options.timeout for _, call, _ in dispatches
            ] == [38000, 36000, 34000, 32000]
            assert [remaining for _, _, remaining in dispatches] == [
                38.0,
                36.0,
                34.0,
                32.0,
            ]
            redraw_prompts = [
                call["contents"][-1]
                for role, call, _ in dispatches
                if role == "redraw"
            ]
        assert "ONE_CORRECTION_ONLY" not in redraw_prompts[0]
        assert "ONE_CORRECTION_ONLY" in redraw_prompts[1]
        correction_only = redraw_prompts[1].split("ONE_CORRECTION_ONLY", 1)[1]
        assert correction_only.index("arrow") < correction_only.index("extra-component")
    finally:
        memory.close()


def test_context_can_disambiguate_but_cannot_authorize_an_absent_component():
    notes, manifest, crops, budget, memory = _case(context_count=1)
    redraw_calls = []
    verify_calls = []

    def redraw(crop, fallback, actual_budget, correction=None):
        redraw_calls.append((crop, fallback, actual_budget, correction))
        return _candidate_png()

    def verify(*args):
        verify_calls.append(args)
        return _failed_verdict(extra=("context-only compass",))

    try:
        assets = _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert len(redraw_calls) == 2
        assert len(verify_calls) == 2
        assert redraw_calls[1][3].extra_elements == ("context-only compass",)
    finally:
        memory.close()


@pytest.mark.parametrize(
    ("field", "isolated_value"),
    (
        ("faithful", False),
        ("candidate_text_free", False),
        ("label_regions_clear", False),
        ("anchors_aligned", False),
        ("geometry_aligned", False),
        ("missing_elements", ("arrow",)),
        ("extra_elements", ("extra-component",)),
        ("unapproved_text", ("outside-slot-text",)),
    ),
)
def test_each_isolated_verdict_field_blocks_acceptance_and_allows_only_one_correction(
    field,
    isolated_value,
):
    notes, manifest, crops, budget, memory = _case(fidelity=True)
    candidate = _fidelity_candidate_png()
    values = _pass_verdict().model_dump(mode="python")
    values[field] = isolated_value
    isolated = RedrawVerdict.model_validate(values)
    corrections = []
    verifier_calls = []

    def redraw(_crop, _fallback, _budget, correction=None):
        corrections.append(correction)
        return candidate

    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=lambda *_args: verifier_calls.append(isolated) or isolated,
        )
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert corrections == [None, isolated]
        assert verifier_calls == [isolated, isolated]
    finally:
        memory.close()


@pytest.mark.parametrize(
    ("features", "expected_missing", "expected_extra", "expected_text", "anchor_ok"),
    (
        ({"component_left": False}, ("component-left",), (), (), True),
        ({"component_right": False}, ("component-right",), (), (), True),
        ({"arrow": False}, ("arrow",), (), (), True),
        ({"curve": False}, ("curve",), (), (), True),
        ({"extra_component": True}, (), ("extra-component",), (), True),
        ({"outside_text": True}, (), (), ("outside-slot-text",), True),
        ({"shifted_anchor": True}, (), (), (), False),
    ),
)
def test_candidate_inspecting_verifier_rejects_each_real_visual_mutation(
    features,
    expected_missing,
    expected_extra,
    expected_text,
    anchor_ok,
):
    notes, manifest, crops, budget, memory = _case(fidelity=True)
    candidate = _fidelity_candidate_png(**features)
    seen = []

    def verify(_crop, candidate_png, _fallback, _budget):
        verdict = _inspect_fidelity_candidate(candidate_png)
        seen.append(verdict)
        return verdict

    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: candidate,
            verify=verify,
        )
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert len(seen) == 2
        assert all(verdict.missing_elements == expected_missing for verdict in seen)
        assert all(verdict.extra_elements == expected_extra for verdict in seen)
        assert all(verdict.unapproved_text == expected_text for verdict in seen)
        assert all(verdict.anchors_aligned is anchor_ok for verdict in seen)
    finally:
        memory.close()


def test_candidate_inspecting_verdict_preserves_ordered_deltas_then_second_failure_falls_back():
    notes, manifest, crops, budget, memory = _case(fidelity=True)
    candidate = _fidelity_candidate_png(
        component_left=False,
        arrow=False,
        curve=False,
        shifted_anchor=True,
        extra_component=True,
        outside_text=True,
    )
    corrections = []
    verdicts = []

    def redraw(_crop, _fallback, _budget, correction=None):
        corrections.append(correction)
        return candidate

    def verify(_crop, candidate_png, _fallback, _budget):
        verdict = _inspect_fidelity_candidate(candidate_png)
        verdicts.append(verdict)
        return verdict

    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=verify,
        )
        expected = verdicts[0]
        assert expected.missing_elements == ("component-left", "arrow", "curve")
        assert expected.extra_elements == ("extra-component",)
        assert expected.unapproved_text == ("outside-slot-text",)
        assert expected.anchors_aligned is False
        assert corrections == [None, expected]
        assert verdicts == [expected, expected]
        correction_prompt = image_gen._redraw_prompt(crops["sv-0001"], expected)
        ordered_deltas = (
            "component-left",
            "arrow",
            "curve",
            "extra-component",
            "outside-slot-text",
            "restore approved label-anchor alignment",
            "restore source geometry alignment",
        )
        correction_only = correction_prompt.split("ONE_CORRECTION_ONLY:", 1)[1]
        positions = [correction_only.index(value) for value in ordered_deltas]
        assert positions == sorted(positions)
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert assets["sv-0001"].primary_png == assets["sv-0001"].fallback_png
    finally:
        memory.close()


def test_same_aspect_different_resolution_candidate_is_rejected_before_labels(monkeypatch):
    notes, manifest, crops, budget, memory = _case()
    redraw_calls = []
    verify_calls = []
    composite_calls = []

    def redraw(*args):
        redraw_calls.append(args)
        return _candidate_png(160, 120)

    def verify(*args):
        verify_calls.append(args)
        return _pass_verdict()

    monkeypatch.setattr(image_gen, "composite_source_labels", lambda *args: composite_calls.append(args))
    try:
        assets = _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert len(redraw_calls) == 2
        assert verify_calls == []
        assert composite_calls == []
    finally:
        memory.close()


@pytest.mark.parametrize("colour", [(255, 255, 255, 255), (12, 12, 15, 255)])
def test_opaque_white_or_dark_candidate_background_cannot_reach_pdf_as_rectangle(colour):
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png(background=colour)
    verify_calls = []
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: candidate,
            verify=lambda *args: verify_calls.append(args) or _pass_verdict(),
        )
        asset = assets["sv-0001"]
        assert asset.state == "sketch_source_fallback"
        assert asset.primary_png == asset.fallback_png
        assert verify_calls == []
    finally:
        memory.close()


def test_text_free_candidate_passes_geometry_verifier_then_exact_labels_are_checked_after_composite(monkeypatch):
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    events: list[str] = []
    actual_composite = image_gen.composite_source_labels
    actual_validate = image_gen.validate_composited_labels

    def redraw(*_args):
        events.append("redraw")
        return candidate

    def verify(*_args):
        events.append("verify")
        return _pass_verdict()

    def composite(*args):
        events.append("composite")
        return actual_composite(*args)

    def validate(*args):
        events.append("label-validation")
        return actual_validate(*args)

    monkeypatch.setattr(image_gen, "composite_source_labels", composite)
    monkeypatch.setattr(image_gen, "validate_composited_labels", validate)
    try:
        assets = _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
        assert assets["sv-0001"].state == "verified_redraw"
        assert events == ["redraw", "verify", "composite", "label-validation"]
    finally:
        memory.close()


def test_faithful_first_candidate_becomes_verified_redraw():
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    redraw_calls = []
    verify_calls = []
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *args: redraw_calls.append(args) or candidate,
            verify=lambda *args: verify_calls.append(args) or _pass_verdict(),
        )
        asset = assets["sv-0001"]
        assert asset.state == "verified_redraw"
        assert asset.primary_png != candidate  # immutable source labels were added
        assert asset.primary_png != asset.fallback_png
        assert asset.width_px == 80 and asset.height_px == 60
        assert len(redraw_calls) == len(verify_calls) == 1
    finally:
        memory.close()


def test_failed_first_candidate_gets_exactly_one_correction_and_second_verification():
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    failure = _failed_verdict(
        missing=("first missing", "second missing"),
        extra=("first extra",),
    )
    corrections = []
    verdicts = iter((failure, _pass_verdict()))

    def redraw(_crop, _fallback, _budget, correction=None):
        corrections.append(correction)
        return candidate

    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=lambda *_args: next(verdicts),
        )
        assert assets["sv-0001"].state == "verified_redraw"
        assert corrections == [None, failure]
        correction_prompt = image_gen._redraw_prompt(crops["sv-0001"], failure)
        assert correction_prompt.index("first missing") < correction_prompt.index("second missing")
        assert correction_prompt.index("second missing") < correction_prompt.index("first extra")
        assert '"alignment":["restore approved label-anchor alignment","restore source geometry alignment"]' in correction_prompt
        assert "SOURCE-LABEL-0" not in correction_prompt
        assert "teacher-only context" not in correction_prompt
    finally:
        memory.close()


def test_second_failure_timeout_quota_or_malformed_verdict_uses_prepared_fallback():
    scenarios = ("timeout", "quota", "malformed")
    for scenario in scenarios:
        notes, manifest, crops, budget, memory = _case()
        calls = []

        def redraw(*args):
            calls.append("redraw")
            if scenario == "timeout":
                raise TimeoutError("provider timed out")
            if scenario == "quota":
                raise RuntimeError("429 quota")
            return _candidate_png()

        def verify(*args):
            calls.append("verify")
            return {"faithful": True}

        try:
            assets = _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
            assert assets["sv-0001"].state == "sketch_source_fallback"
            assert calls.count("redraw") == 2
            assert calls.count("verify") == (2 if scenario == "malformed" else 0)
        finally:
            memory.close()


def test_each_local_candidate_failure_class_gets_at_most_one_correction():
    oversized = _candidate_png() + b"\0" * (config.MAX_VISUAL_PNG_BYTES + 1)
    failures = {
        "undecodable": b"not-png",
        "encoded-cap": oversized,
        "wrong-dimensions": _candidate_png(40, 30),
        "empty-alpha": _transparent_png(),
        "checkerboard": _checkerboard_png(),
        "label-collision": _candidate_png(label_collision=True),
    }
    for name, candidate in failures.items():
        notes, manifest, crops, budget, memory = _case()
        redraw_calls = []
        verify_calls = []
        try:
            assets = _render(
                notes,
                manifest,
                crops,
                budget,
                memory,
                redraw=lambda *args: redraw_calls.append(args) or candidate,
                verify=lambda *args: verify_calls.append(args) or _pass_verdict(),
            )
            assert assets["sv-0001"].state == "sketch_source_fallback", name
            assert len(redraw_calls) == 2, name
            assert verify_calls == [], name
        finally:
            memory.close()


@pytest.mark.parametrize("defect", ("checkerboard", "light-panel", "dark-panel"))
def test_transparent_border_background_fixture_reaches_only_its_specialized_guard(
    monkeypatch,
    defect,
):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    if defect == "checkerboard":
        candidate = _inner_checkerboard_png()
    else:
        colour = (250, 250, 250, 255) if defect == "light-panel" else (25, 25, 25, 255)
        candidate = _inner_neutral_panel_png(colour=colour)

    try:
        rgba = image_gen._decode_exact_rgba(
            candidate,
            crop.width_px,
            crop.height_px,
            max_bytes=config.MAX_VISUAL_PNG_BYTES,
            max_pixels=config.MAX_VISUAL_CROP_PIXELS,
        )
        alpha = rgba[..., 3]
        border = [
            *alpha[0, :],
            *alpha[-1, :],
            *alpha[1:-1, 0],
            *alpha[1:-1, -1],
        ]
        assert all(value <= 16 for value in border)

        with Image.open(io.BytesIO(candidate)) as raw:
            checker = image_gen._looks_checkered(raw)
        neutral_panel = image_gen._contains_opaque_neutral_rectangle(rgba)
        with pytest.raises(VisualFidelityError) as error:
            image_gen._validate_candidate_png(
                candidate,
                crop,
                fallback,
                labels_composited=False,
            )
        assert error.value.code == "render_failed"
        if defect == "checkerboard":
            assert checker is True
            assert neutral_panel is False
            monkeypatch.setattr(image_gen, "_looks_checkered", lambda _image: False)
        else:
            assert checker is False
            assert neutral_panel is True
            monkeypatch.setattr(
                image_gen,
                "_contains_opaque_neutral_rectangle",
                lambda _rgba: False,
            )

        # Disabling just the intended detector makes the otherwise valid
        # transparent-border candidate pass every generic local guard.
        image_gen._validate_candidate_png(
            candidate,
            crop,
            fallback,
            labels_composited=False,
        )
    finally:
        memory.close()


def test_transparent_border_threshold_accepts_exact_limit_and_rejects_next_pixel():
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    exact = _candidate_with_opaque_border_pixels(41)
    beyond = _candidate_with_opaque_border_pixels(42)
    try:
        image_gen._validate_candidate_png(
            exact,
            crop,
            fallback,
            labels_composited=False,
        )
        with pytest.raises(VisualFidelityError) as error:
            image_gen._validate_candidate_png(
                beyond,
                crop,
                fallback,
                labels_composited=False,
            )
        assert error.value.code == "render_failed"
    finally:
        memory.close()


def test_decoded_candidate_pixel_cap_accepts_exact_limit_and_rejects_one_beyond(
    monkeypatch,
):
    _notes, _manifest, crops, budget, memory = _case()
    crop = crops["sv-0001"]
    fallback = prepare_source_fallback(crop, budget, memory)
    candidate = _candidate_png()
    try:
        monkeypatch.setattr(image_gen.config, "MAX_VISUAL_CROP_PIXELS", 4800)
        image_gen._validate_candidate_png(
            candidate,
            crop,
            fallback,
            labels_composited=False,
        )
        monkeypatch.setattr(image_gen.config, "MAX_VISUAL_CROP_PIXELS", 4799)
        with pytest.raises(VisualFidelityError) as error:
            image_gen._validate_candidate_png(
                candidate,
                crop,
                fallback,
                labels_composited=False,
            )
        assert error.value.code == "render_failed"
    finally:
        memory.close()


def test_redraw_budget_overflow_uses_fallback_without_dropping_visuals(monkeypatch):
    notes, manifest, crops, budget, memory = _case(count=5)
    redraw_ids = []
    verify_ids = []
    monkeypatch.setattr(image_gen.config, "MAX_IMAGES", 4)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda crop, *_args: redraw_ids.append(crop.visual.visual_id) or _candidate_png(),
            verify=lambda crop, *_args: verify_ids.append(crop.visual.visual_id) or _pass_verdict(),
        )
        assert tuple(assets) == tuple(f"sv-{index:04d}" for index in range(1, 6))
        assert redraw_ids == verify_ids == list(assets)[:4]
        assert all(assets[key].state == "verified_redraw" for key in list(assets)[:4])
        assert assets["sv-0005"].state == "sketch_source_fallback"
    finally:
        memory.close()


def test_source_labels_are_composited_once_after_candidate_verification(monkeypatch):
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    calls = []
    actual = image_gen.composite_source_labels

    def composite(*args):
        calls.append(args)
        return actual(*args)

    monkeypatch.setattr(image_gen, "composite_source_labels", composite)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: candidate,
            verify=lambda *_args: _pass_verdict(),
        )
        assert len(calls) == 1
        asset = assets["sv-0001"]
        with Image.open(io.BytesIO(asset.primary_png)) as image:
            assert image.convert("RGBA").getpixel((5, 5))[3] > 0
    finally:
        memory.close()


def test_no_state_machine_path_exceeds_two_provider_redraws_or_two_verifiers():
    scenarios = (
        (lambda *_args: None, lambda *_args: _pass_verdict()),
        (lambda *_args: _candidate_png(), lambda *_args: _failed_verdict()),
        (lambda *_args: _candidate_png(40, 30), lambda *_args: _pass_verdict()),
    )
    for redraw_impl, verify_impl in scenarios:
        notes, manifest, crops, budget, memory = _case()
        redraw_count = 0
        verify_count = 0

        def redraw(*args):
            nonlocal redraw_count
            redraw_count += 1
            return redraw_impl(*args)

        def verify(*args):
            nonlocal verify_count
            verify_count += 1
            return verify_impl(*args)

        try:
            _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
            assert 0 <= redraw_count <= 2
            assert 0 <= verify_count <= 2
        finally:
            memory.close()


def test_manifest_graph_invalid_candidate_uses_complete_source_fallback_not_partial_semantic_graph():
    notes, manifest, crops, budget, memory = _case(kind="graph")
    partial = _transparent_png()
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: partial,
            verify=lambda *_args: _pass_verdict(),
        )
        asset = assets["sv-0001"]
        assert asset.state == "sketch_source_fallback"
        assert asset.primary_png == asset.fallback_png
        with Image.open(io.BytesIO(asset.primary_png)) as fallback:
            rgba = fallback.convert("RGBA")
            assert rgba.getpixel((5, 5))[3] > 0  # exact source label survives
            assert rgba.getpixel((28, 34))[3] > 0  # complete source graph survives
        assert notes.sections[0].diagram == ""
    finally:
        memory.close()


def test_render_visuals_rejects_swapped_crop_key_visual_id_or_order_before_work(monkeypatch):
    for corruption in ("order", "swapped-values", "context"):
        notes, manifest, crops, budget, memory = _case(
            count=2,
            context_count=1 if corruption == "context" else 0,
        )
        if corruption == "order":
            invalid = OrderedDict(reversed(tuple(crops.items())))
        elif corruption == "swapped-values":
            invalid = OrderedDict(
                (("sv-0001", crops["sv-0002"]), ("sv-0002", crops["sv-0001"]))
            )
        else:
            invalid = OrderedDict(crops)
            invalid["sv-0001"] = replace(invalid["sv-0001"], context_crops=())

        calls = []
        monkeypatch.setattr(
            image_gen,
            "prepare_source_fallback",
            lambda *_args: calls.append("fallback") or pytest.fail("work started"),
        )
        with pytest.raises(VisualFidelityError) as error:
            image_gen.render_visuals(notes, manifest, invalid, budget, memory)
        assert error.value.code == "link_invalid"
        assert calls == []
        assert memory.source_bytes == memory.context_bytes == memory.asset_bytes == 0


def test_canonical_empty_context_reaches_redraw_and_verifier_unchanged():
    notes, manifest, crops, budget, memory = _case(context_count=0)
    crop = crops["sv-0001"]
    seen = []

    def redraw(actual_crop, *_args):
        seen.append(("redraw", actual_crop.context_crops))
        return _candidate_png()

    def verify(actual_crop, *_args):
        seen.append(("verify", actual_crop.context_crops))
        return _pass_verdict()

    try:
        _render(notes, manifest, crops, budget, memory, redraw=redraw, verify=verify)
        assert seen == [("redraw", crop.context_crops), ("verify", crop.context_crops)]
        assert crop.context_crops == ()
    finally:
        memory.close()


def test_candidates_composites_and_terminal_assets_obey_aggregate_memory_cap():
    # Measure only deterministic preparation so the actual run can put its cap
    # one byte below the optional candidate reservation.
    _notes, _manifest, probe_crops, probe_budget, probe_memory = _case()
    prepare_source_fallback(probe_crops["sv-0001"], probe_budget, probe_memory)
    prepared_bytes = probe_memory.asset_bytes
    probe_memory.close()

    candidate = _candidate_png()
    capped_memory = _memory(asset=prepared_bytes + len(candidate) - 1)
    notes, manifest, crops, budget, memory = _case(memory=capped_memory)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: candidate,
            verify=lambda *_args: _pass_verdict(),
        )
        assert assets["sv-0001"].state == "sketch_source_fallback"
        assert memory.asset_bytes <= memory.max_asset_bytes
        assert memory.source_bytes + memory.context_bytes + memory.asset_bytes <= memory.max_total_bytes
    finally:
        memory.close()


def test_terminal_asset_releases_dead_label_layers_and_rejected_composite_reservations(monkeypatch):
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    base_composite = image_gen.composite_source_labels
    composites = []

    def composite(*args):
        result = base_composite(*args)
        composites.append(result)
        return result

    monkeypatch.setattr(image_gen, "composite_source_labels", composite)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda *_args: candidate,
            verify=lambda *_args: _pass_verdict(),
        )
        asset = assets["sv-0001"]
        active_asset_data = [
            entry.data for entry in memory._buffer_entries.values() if entry.kind == "asset"
        ]
        assert {id(data) for data in active_asset_data} == {
            id(asset.fallback_png),
            id(asset.primary_png),
        }
        assert composites == [asset.primary_png]
        assert memory.asset_bytes == sum(len(data) for data in active_asset_data)
    finally:
        memory.close()

    rejected_notes, rejected_manifest, rejected_crops, rejected_budget, rejected_memory = _case()
    rejected_composites = []

    def rejected_composite(*args):
        result = base_composite(*args)
        rejected_composites.append(result)
        return result

    monkeypatch.setattr(image_gen, "composite_source_labels", rejected_composite)
    monkeypatch.setattr(
        image_gen,
        "validate_composited_labels",
        lambda *_args: (_ for _ in ()).throw(VisualFidelityError("fallback_damaged")),
    )
    try:
        rejected_assets = _render(
            rejected_notes,
            rejected_manifest,
            rejected_crops,
            rejected_budget,
            rejected_memory,
            redraw=lambda *_args: candidate,
            verify=lambda *_args: _pass_verdict(),
        )
        rejected_asset = rejected_assets["sv-0001"]
        assert rejected_asset.state == "sketch_source_fallback"
        assert len(rejected_composites) == 2
        active_data = [entry.data for entry in rejected_memory._buffer_entries.values()]
        assert all(
            all(data is not composite for data in active_data)
            for composite in rejected_composites
        )
        assert sum(
            entry.kind == "asset" and entry.data is rejected_asset.fallback_png
            for entry in rejected_memory._buffer_entries.values()
        ) == 1
    finally:
        rejected_memory.close()


@pytest.mark.parametrize(
    "defect",
    ("wrong-dimensions", "encoded-cap", "decoded-pixel-cap", "neutral-background"),
)
def test_each_locally_invalid_composite_is_owned_then_released_before_fallback(
    monkeypatch,
    defect,
):
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    invalid_composites: list[bytes] = []
    acquired = []
    released = []
    downstream_label_calls = []
    actual_reserve = memory.reserve_buffer
    actual_release = memory.release_buffer
    redraw_calls = []
    verify_calls = []

    def make_invalid_composite(*_args):
        if defect == "wrong-dimensions":
            result = _candidate_png(79, 60)
        elif defect == "encoded-cap":
            base = bytes(bytearray(candidate))
            result = base + b"\0" * (config.MAX_VISUAL_PNG_BYTES + 1 - len(base))
        elif defect == "decoded-pixel-cap":
            monkeypatch.setattr(image_gen.config, "MAX_VISUAL_CROP_PIXELS", 4799)
            result = bytes(bytearray(candidate))
        else:
            result = _inner_neutral_panel_png()
        invalid_composites.append(result)
        return result

    def redraw(*args):
        redraw_calls.append(args)
        # Let the next candidate pass before the fake composite lowers the cap.
        if defect == "decoded-pixel-cap":
            monkeypatch.setattr(image_gen.config, "MAX_VISUAL_CROP_PIXELS", 4800)
        return candidate

    def verify(*args):
        verify_calls.append(args)
        return _pass_verdict()

    def reserve(kind, data):
        reservation = actual_reserve(kind, data)
        if any(data is composite for composite in invalid_composites):
            acquired.append(reservation)
        return reservation

    def release(reservation):
        if any(reservation.data is composite for composite in invalid_composites):
            released.append(reservation)
        return actual_release(reservation)

    def accept_composited_labels(composite, *_args):
        # Isolate the dedicated local composite guard: downstream exact-label
        # validation is deliberately forced to accept every malformed image.
        downstream_label_calls.append(composite)

    monkeypatch.setattr(image_gen, "composite_source_labels", make_invalid_composite)
    monkeypatch.setattr(
        image_gen,
        "validate_composited_labels",
        accept_composited_labels,
    )
    monkeypatch.setattr(memory, "reserve_buffer", reserve)
    monkeypatch.setattr(memory, "release_buffer", release)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=verify,
        )
        asset = assets["sv-0001"]
        assert asset.state == "sketch_source_fallback"
        assert len(redraw_calls) == len(verify_calls) == 2
        assert len(invalid_composites) == 2
        assert downstream_label_calls == []
        assert len(acquired) == len(released) == 2
        assert all(owner is not None for owner in acquired)
        assert all(
            sum(owner.data is composite for owner in acquired) == 1
            for composite in invalid_composites
        )
        assert all(
            sum(owner.data is composite for owner in released) == 1
            for composite in invalid_composites
        )
        active = tuple(entry.data for entry in memory._buffer_entries.values())
        assert all(
            all(data is not composite for data in active)
            for composite in invalid_composites
        )
        assert [
            entry.data
            for entry in memory._buffer_entries.values()
            if entry.kind == "asset"
        ] == [asset.fallback_png]
    finally:
        memory.close()


def test_candidate_alias_of_retained_fallback_is_rejected_without_releasing_owner(monkeypatch):
    notes, manifest, crops, budget, memory = _case()
    released = []
    actual_release = memory.release_buffer

    def release(reservation):
        released.append(reservation)
        return actual_release(reservation)

    monkeypatch.setattr(memory, "release_buffer", release)
    fallback_objects = []

    def redraw(_crop, fallback, _budget, correction=None):
        fallback_objects.append(fallback.png)
        return fallback.png

    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=lambda *_args: pytest.fail("alias reached verifier"),
        )
        asset = assets["sv-0001"]
        assert asset.state == "sketch_source_fallback"
        assert all(item is asset.fallback_png for item in fallback_objects)
        assert all(reservation.data is not asset.fallback_png for reservation in released)
        assert sum(
            entry.data is asset.fallback_png for entry in memory._buffer_entries.values()
        ) == 1
    finally:
        memory.close()


def test_manifest_path_exception_closes_all_prior_and_current_reservations(monkeypatch):
    notes, manifest, crops, budget, memory = _case(count=2)
    actual_prepare = image_gen.prepare_source_fallback
    prepared_ids = []

    def prepare(crop, actual_budget, actual_memory):
        prepared_ids.append(crop.visual.visual_id)
        if crop.visual.visual_id == "sv-0002":
            raise VisualFidelityError("fallback_damaged")
        return actual_prepare(crop, actual_budget, actual_memory)

    monkeypatch.setattr(image_gen, "prepare_source_fallback", prepare)
    with pytest.raises(VisualFidelityError):
        image_gen.render_visuals(notes, manifest, crops, budget, memory)
    assert prepared_ids == ["sv-0001", "sv-0002"]
    assert memory.source_bytes == 0
    assert memory.context_bytes == 0
    assert memory.asset_bytes == 0


def test_later_candidate_cleanup_failure_closes_prior_current_and_remainder_owners(
    monkeypatch,
):
    notes, manifest, crops, budget, memory = _case(count=3, context_count=1)
    candidates = {
        visual_id: bytes(bytearray(_candidate_png())) for visual_id in crops
    }
    actual_prepare = image_gen.prepare_source_fallback
    actual_composite = image_gen.composite_source_labels
    actual_release = memory.release_buffer
    prepared = []
    composites = []
    redraw_ids = []
    failure_snapshot = []
    failed_once = False

    def prepare(crop, actual_budget, actual_memory):
        result = actual_prepare(crop, actual_budget, actual_memory)
        prepared.append(result)
        return result

    def composite(*args):
        result = actual_composite(*args)
        composites.append(result)
        return result

    def redraw(crop, *_args):
        redraw_ids.append(crop.visual.visual_id)
        # Preparation is global, and visual 1 has already become terminal by
        # the time processing for visual 2 begins.
        assert len(prepared) == 3
        if crop.visual.visual_id == "sv-0002":
            active = tuple(entry.data for entry in memory._buffer_entries.values())
            assert len(composites) == 1
            assert any(data is composites[0] for data in active)
            assert any(data is prepared[0].png for data in active)
            assert all(
                all(data is not handle.data for data in active)
                for handle in prepared[0].temporary_label_reservations
            )
        return candidates[crop.visual.visual_id]

    def release(reservation):
        nonlocal failed_once
        if reservation.data is candidates["sv-0002"] and not failed_once:
            failed_once = True
            failure_snapshot.extend(
                entry.data for entry in memory._buffer_entries.values()
            )
            # Fail before changing ownership: the terminal outer close must
            # drain this current candidate together with every other owner.
            raise RuntimeError("fatal exact cleanup failure")
        return actual_release(reservation)

    monkeypatch.setattr(image_gen, "prepare_source_fallback", prepare)
    monkeypatch.setattr(image_gen, "composite_source_labels", composite)
    monkeypatch.setattr(memory, "release_buffer", release)

    with pytest.raises(VisualFidelityError) as error:
        _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=redraw,
            verify=lambda *_args: _pass_verdict(),
        )
    assert error.value.code == "memory_limit_exceeded"
    assert redraw_ids == ["sv-0001", "sv-0002"]
    assert len(prepared) == 3
    assert len(composites) == 2
    assert failed_once
    assert any(data is composites[0] for data in failure_snapshot)
    assert any(data is composites[1] for data in failure_snapshot)
    assert any(data is candidates["sv-0002"] for data in failure_snapshot)
    assert all(any(data is item.png for data in failure_snapshot) for item in prepared)
    assert all(
        any(data is handle.data for data in failure_snapshot)
        for handle in prepared[1].temporary_label_reservations
    )
    assert all(
        any(data is handle.data for data in failure_snapshot)
        for handle in prepared[2].temporary_label_reservations
    )
    assert memory.source_bytes == 0
    assert memory.context_bytes == 0
    assert memory.asset_bytes == 0
    assert memory.page_cache_bytes == 0
    assert memory._buffer_entries == {}
    assert memory._buffer_token_by_object_id == {}
    assert memory._page_entries == {}
    assert memory._closed is True


def test_matching_success_retains_only_sources_contexts_fallbacks_and_terminal_composites(
    monkeypatch,
):
    monkeypatch.setattr(config, "MAX_IMAGES", 3)
    notes, manifest, crops, budget, memory = _case(count=3, context_count=1)
    candidates = {
        visual_id: bytes(bytearray(_candidate_png())) for visual_id in crops
    }
    actual_prepare = image_gen.prepare_source_fallback
    actual_composite = image_gen.composite_source_labels
    prepared = []
    composites = []

    def prepare(crop, actual_budget, actual_memory):
        result = actual_prepare(crop, actual_budget, actual_memory)
        prepared.append(result)
        return result

    def composite(*args):
        result = actual_composite(*args)
        composites.append(result)
        return result

    monkeypatch.setattr(image_gen, "prepare_source_fallback", prepare)
    monkeypatch.setattr(image_gen, "composite_source_labels", composite)
    try:
        assets = _render(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=lambda crop, *_args: candidates[crop.visual.visual_id],
            verify=lambda *_args: _pass_verdict(),
        )
        assert [asset.state for asset in assets.values()] == ["verified_redraw"] * 3
        assert memory._closed is False
        assert memory.source_bytes > 0
        assert memory.context_bytes > 0
        assert memory.asset_bytes > 0
        assert memory.page_cache_bytes == 0
        assert memory._page_entries == {}

        active = tuple(entry.data for entry in memory._buffer_entries.values())
        expected = [
            *(crop.source_png for crop in crops.values()),
            *(
                context.source_png
                for crop in crops.values()
                for context in crop.context_crops
            ),
            *(fallback.png for fallback in prepared),
            *composites,
        ]
        assert {id(data) for data in active} == {id(data) for data in expected}
        assert len(memory._buffer_token_by_object_id) == len(memory._buffer_entries)
        assert all(
            all(data is not candidate for data in active)
            for candidate in candidates.values()
        )
        assert all(
            all(
                all(data is not handle.data for data in active)
                for handle in fallback.temporary_label_reservations
            )
            for fallback in prepared
        )
    finally:
        memory.close()


def _activated_fallback_case():
    notes, manifest, crops, budget, memory = _case()
    assets = _render(
        notes,
        manifest,
        crops,
        budget,
        memory,
        redraw=lambda *_args: None,
        verify=lambda *_args: pytest.fail("fallback-only case reached verifier"),
    )
    object.__setattr__(notes, "_visual_manifest", manifest)
    object.__setattr__(notes, "_source_crops", OrderedDict(crops))
    object.__setattr__(notes, "_visual_assets", OrderedDict(assets))
    object.__setattr__(notes, "_visual_run_budget", budget)
    object.__setattr__(notes, "_visual_memory_budget", memory)
    return notes, manifest, assets, budget, memory


def _activated_verified_case():
    notes, manifest, crops, budget, memory = _case()
    candidate = _candidate_png()
    assets = _render(
        notes,
        manifest,
        crops,
        budget,
        memory,
        redraw=lambda *_args, **_kwargs: candidate,
        verify=lambda *_args, **_kwargs: _pass_verdict(),
    )
    assert assets["sv-0001"].state == "verified_redraw"
    object.__setattr__(notes, "_visual_manifest", manifest)
    object.__setattr__(notes, "_source_crops", OrderedDict(crops))
    object.__setattr__(notes, "_visual_assets", OrderedDict(assets))
    object.__setattr__(notes, "_visual_run_budget", budget)
    object.__setattr__(notes, "_visual_memory_budget", memory)
    return notes, manifest, assets, budget, memory


def test_build_notes_pdf_closes_job_memory_only_after_successful_output(monkeypatch):
    notes, manifest, assets, _budget, memory = _activated_fallback_case()
    output_observations = []
    integrity_observations = []
    actual_output = NotesPDF.output
    actual_integrity = NotesPDF._assert_visual_render_integrity

    def record_integrity(self):
        integrity_observations.append((
            memory.resident_bytes(),
            tuple((item.visual_id, item.state) for item in self._rendered_visuals),
        ))
        return actual_integrity(self)

    def record_output(self, *args, **kwargs):
        output_observations.append(memory.resident_bytes())
        return actual_output(self, *args, **kwargs)

    monkeypatch.setattr(NotesPDF, "_assert_visual_render_integrity", record_integrity)
    monkeypatch.setattr(NotesPDF, "output", record_output)

    result = build_notes_pdf(notes, "by QA", watermark=False)

    assert result.startswith(b"%PDF")
    assert output_observations and output_observations[0] > 0
    assert integrity_observations == [(
        output_observations[0],
        ((manifest.visuals[0].visual_id, assets["sv-0001"].state),),
    )]
    assert memory.resident_bytes() == 0
    assert memory._closed is True
    assert notes._visual_manifest is manifest
    assert notes._visual_assets["sv-0001"] is assets["sv-0001"]
    assert notes._visual_memory_budget is memory


@pytest.mark.parametrize(
    "failure_stage",
    [
        "constructor",
        "fallback-preflight",
        "real-write",
        "integrity",
        "output",
        "post-output-deadline",
    ],
)
def test_build_notes_pdf_terminally_closes_job_memory_on_each_failure(
    monkeypatch,
    capsys,
    failure_stage,
):
    notes, _manifest, _assets, budget, memory = _activated_fallback_case()
    output_completed = {"value": False}

    if failure_stage == "constructor":
        object.__setattr__(notes, "_visual_assets", OrderedDict())
    elif failure_stage == "fallback-preflight":
        monkeypatch.setattr(
            NotesPDF,
            "_preflight_visual_png",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("PRIVATE fallback preflight failure")
            ),
            raising=False,
        )
    elif failure_stage == "real-write":
        monkeypatch.setattr(
            NotesPDF,
            "image",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("PRIVATE final image failure")
            ),
        )
    elif failure_stage == "integrity":
        monkeypatch.setattr(
            NotesPDF,
            "_assert_visual_render_integrity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                VisualFidelityError("render_failed")
            ),
            raising=False,
        )
    elif failure_stage == "output":
        monkeypatch.setattr(
            NotesPDF,
            "output",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("PRIVATE output failure")
            ),
        )
    else:
        actual_output = NotesPDF.output
        actual_check = budget.check_deadline

        def record_output(self, *args, **kwargs):
            result = actual_output(self, *args, **kwargs)
            output_completed["value"] = True
            return result

        def fail_after_output():
            if output_completed["value"]:
                raise VisualFidelityError("deadline_exceeded")
            actual_check()

        monkeypatch.setattr(NotesPDF, "output", record_output)
        budget.check_deadline = fail_after_output  # type: ignore[method-assign]

    with pytest.raises((VisualFidelityError, RuntimeError)):
        build_notes_pdf(notes, "by QA", watermark=False)

    assert memory.resident_bytes() == 0
    assert memory._closed is True
    assert "[notesbot.visual-render]" not in capsys.readouterr().out


def test_terminal_memory_close_failure_emits_no_render_success_event(
    monkeypatch,
    capsys,
):
    notes, _manifest, _assets, _budget, memory = _activated_fallback_case()
    close_attempts = []

    def fail_close():
        close_attempts.append("close")
        raise VisualFidelityError("memory_limit_exceeded")

    monkeypatch.setattr(memory, "close", fail_close)

    with pytest.raises(VisualFidelityError) as caught:
        build_notes_pdf(notes, "by QA", watermark=False)

    assert caught.value.code == "memory_limit_exceeded"
    assert close_attempts
    assert "[notesbot.visual-render]" not in capsys.readouterr().out


def test_invalid_memory_runtime_preserves_fixed_link_error_during_cleanup(capsys):
    notes, _manifest, _assets, _budget, original_memory = _activated_fallback_case()
    object.__setattr__(notes, "_visual_memory_budget", object())
    original_memory.close()

    with pytest.raises(VisualFidelityError) as caught:
        build_notes_pdf(notes, "by QA", watermark=False)

    assert caught.value.code == "link_invalid"
    assert "[notesbot.visual-render]" not in capsys.readouterr().out


@pytest.mark.parametrize("asset_state", ["verified_redraw", "fallback"])
@pytest.mark.parametrize("failure_point", ["image", "output"])
def test_scratch_failure_checks_expired_deadline_before_fallback_or_normalization(
    monkeypatch,
    capsys,
    asset_state,
    failure_point,
):
    if asset_state == "verified_redraw":
        notes, _manifest, assets, budget, memory = _activated_verified_case()
    else:
        notes, _manifest, assets, budget, memory = _activated_fallback_case()
    asset = assets["sv-0001"]
    now = [1.0]
    budget.deadline_at = 10.0
    budget.clock = lambda: now[0]
    events = []
    real_writes = []
    instances = []

    def candidate_name(png):
        if asset_state == "verified_redraw" and png == asset.primary_png:
            return "primary"
        if png == asset.fallback_png:
            return "fallback"
        return "unknown"

    actual_check = budget.check_deadline

    def record_deadline():
        events.append(("deadline", now[0]))
        return actual_check()

    budget.check_deadline = record_deadline  # type: ignore[method-assign]

    actual_decode = NotesPDF._decode_visual_png

    def record_decode(png, width_px, height_px, error_code):
        events.append(("decode", candidate_name(png)))
        return actual_decode(png, width_px, height_px, error_code)

    monkeypatch.setattr(
        NotesPDF,
        "_decode_visual_png",
        staticmethod(record_decode),
    )

    class ScratchFPDF:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.candidate = "unknown"

        def set_auto_page_break(self, *args, **kwargs):
            del args, kwargs

        def add_page(self):
            return None

        def image(self, name, *args, **kwargs):
            del args, kwargs
            self.candidate = candidate_name(name.getvalue())
            events.append(("scratch-image", self.candidate))
            if failure_point == "image":
                now[0] = 11.0
                events.append(("scratch-failure", self.candidate, "image"))
                raise RuntimeError("PRIVATE scratch image failure")

        def output(self):
            events.append(("scratch-output", self.candidate))
            if failure_point == "output":
                now[0] = 11.0
                events.append(("scratch-failure", self.candidate, "output"))
                raise RuntimeError("PRIVATE scratch output failure")
            return b"%PDF-scratch"

    class CapturingNotesPDF(NotesPDF):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def image(self, name, *args, **kwargs):
            real_writes.append((name, args, kwargs))

    monkeypatch.setattr(notes_pdf_module, "FPDF", ScratchFPDF)
    monkeypatch.setattr(notes_pdf_module, "NotesPDF", CapturingNotesPDF)

    result = None
    with pytest.raises(VisualFidelityError) as caught:
        result = build_notes_pdf(notes, "by QA", watermark=False)

    assert caught.value.code == "deadline_exceeded"
    failure_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "scratch-failure"
    )
    assert events[failure_index + 1] == ("deadline", 11.0)
    if asset_state == "verified_redraw":
        assert not any(
            event == ("decode", "fallback")
            for event in events[failure_index + 1:]
        )
    assert result is None
    assert real_writes == []
    assert len(instances) == 1
    assert instances[0]._rendered_visuals == []
    assert memory.resident_bytes() == 0
    assert memory._closed is True
    assert "[notesbot.visual-render]" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "corruption",
    ["missing", "duplicate", "invalid-state", "non-record"],
)
def test_visual_render_integrity_rejects_missing_duplicate_and_invalid_final_state(
    corruption,
):
    notes, manifest, _assets, _budget, memory = _activated_fallback_case()
    pdf = NotesPDF(notes, "by QA", watermark=False)
    valid = RenderedVisual(
        visual_id=manifest.visuals[0].visual_id,
        state="sketch_source_fallback",
        page_number=1,
        x_mm=10.0,
        y_mm=20.0,
        width_mm=100.0,
        height_mm=60.0,
    )
    if corruption == "missing":
        pdf._rendered_visuals = []
    elif corruption == "duplicate":
        pdf._rendered_visuals = [valid, valid]
    elif corruption == "non-record":
        pdf._rendered_visuals = [object()]
    else:
        pdf._rendered_visuals = [replace(valid, state="pending")]

    try:
        with pytest.raises(VisualFidelityError) as caught:
            pdf._assert_visual_render_integrity()
        assert caught.value.code == "render_failed"
    finally:
        memory.close()


# ---------------------------------------------------------------------------
# Task 11: deterministic source-to-final-PDF fidelity closure.
# ---------------------------------------------------------------------------

SOURCE_FIDELITY_IDS = (
    "sv-0001",
    "sv-0002",
    "sv-0003",
    "sv-0004",
    "sv-0005",
)
MANUAL_PLACEHOLDER_HEADING = "DIAGRAM \u2014 draw here"


def _pct_page_box(
    source_box: tuple[float, float, float, float],
    relative_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    sx0, sy0, sx1, sy1 = source_box
    width, height = sx1 - sx0, sy1 - sy0
    return (
        sx0 + width * relative_box[0] / 100.0,
        sy0 + height * relative_box[1] / 100.0,
        sx0 + width * relative_box[2] / 100.0,
        sy0 + height * relative_box[3] / 100.0,
    )


def _pct_page_anchor(
    source_box: tuple[float, float, float, float],
    anchor: tuple[float, float] | None,
) -> tuple[float, float] | None:
    if anchor is None:
        return None
    sx0, sy0, sx1, sy1 = source_box
    return (
        sx0 + (sx1 - sx0) * anchor[0] / 100.0,
        sy0 + (sy1 - sy0) * anchor[1] / 100.0,
    )


def _fidelity_inventory(fixture: SourceFidelityPdfFixture) -> InventoryResponse:
    regions: list[VisualRegion] = []
    for source_order, truth in enumerate(fixture.truth, start=1):
        anchor_ref = f"fixture-anchor-{source_order}"
        x0, y0, x1, _y1 = truth.source_box
        anchor = InventoryTopicAnchor(
            anchor_ref=anchor_ref,
            local_page=truth.local_page,
            box=(x0, max(0.0, y0 - 5.0), min(x1, x0 + 30.0), y0),
            exact_heading=f"Source topic {source_order}",
        )
        contexts: tuple[InventoryContext, ...] = ()
        if truth.visual_id == "sv-0001":
            contexts = (
                InventoryContext(
                    anchor_ref=anchor_ref,
                    local_page=truth.local_page,
                    box=(7.0, 9.0, 34.0, 17.0),
                    role="explanation",
                    exact_text="The source arrow fixes the intended direction.",
                ),
            )
        labels = tuple(
            InventoryLabel(
                text=label.text,
                page_box=_pct_page_box(truth.source_box, label.box),
                page_anchor=_pct_page_anchor(truth.source_box, label.anchor),
            )
            for label in truth.labels
        )
        regions.append(
            VisualRegion(
                local_page=truth.local_page,
                box=truth.source_box,
                kind=truth.kind,
                topic_anchor=anchor,
                exact_labels=labels,
                context_regions=contexts,
                section_hint="",
                source_order_hint=source_order,
            )
        )
    return InventoryResponse(
        pages_checked=(1, 2, 3, 4),
        regions=tuple(regions),
    )


def _box_intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def _fidelity_audit_from_prompt(prompt: str) -> InventoryAuditResponse:
    payload = visual_inventory.decode_audit_payload(prompt)
    raw_regions = payload["provisional_regions"]
    raw_probes = payload["probes"]
    assert isinstance(raw_regions, list)
    assert isinstance(raw_probes, list)
    regions = tuple(
        VisualRegion.model_validate_json(json.dumps(item["region"]))
        for item in raw_regions
    )
    probes = tuple(
        CoverageProbe.model_validate_json(json.dumps(item)) for item in raw_probes
    )

    assignments: list[tuple[int, ...]] = []
    for probe in probes:
        matches: list[int] = []
        probe_area = (probe.box[2] - probe.box[0]) * (probe.box[3] - probe.box[1])
        center_x = (probe.box[0] + probe.box[2]) / 2.0
        center_y = (probe.box[1] + probe.box[3]) / 2.0
        for index, region in enumerate(regions):
            if probe.local_page != region.local_page:
                continue
            center_inside = (
                region.box[0] <= center_x <= region.box[2]
                and region.box[1] <= center_y <= region.box[3]
            )
            substantially_overlaps = (
                _box_intersection_area(probe.box, region.box) >= 0.25 * probe_area
            )
            if center_inside or substantially_overlaps:
                matches.append(index)
        assignments.append(tuple(matches))

    final_regions = tuple(
        AuditedRegion(
            **region.model_dump(mode="python"),
            source_region_ids=(raw_regions[index]["provisional_id"],),
        )
        for index, region in enumerate(regions)
    )
    dispositions = tuple(
        ProbeDisposition(
            probe_id=probe.probe_id,
            outcome="covered_visual" if assignments[index] else "ordinary_text",
            final_region_indices=assignments[index],
        )
        for index, probe in enumerate(probes)
    )
    return InventoryAuditResponse(
        pages_checked=tuple(payload["pages_checked"]),
        coverage_complete=True,
        unresolved_count=0,
        final_regions=final_regions,
        probe_dispositions=dispositions,
    )


class _FidelityFakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._body


class _FidelityFakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        self.content = _FidelityFakeContent(body)


class _FidelityRequestContext:
    def __init__(self, response: _FidelityFakeResponse):
        self.response = response
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FidelityFakeResponse:
        self.entered = True
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True


@dataclass
class _FidelityFakeSession:
    responses: list[_FidelityRequestContext]
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    entered: int = 0
    exited: int = 0
    all_contexts: tuple[_FidelityRequestContext, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.all_contexts = tuple(self.responses)

    async def __aenter__(self) -> "_FidelityFakeSession":
        self.entered += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1

    def request(self, method: str, url: str, **kwargs: Any) -> _FidelityRequestContext:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected Mathpix HTTP request")
        if method == "DELETE":
            return self.responses.pop()
        return self.responses.pop(0)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _mathpix_line(
    line_number: int,
    line_type: str,
    text: str,
    box: tuple[float, float, float, float],
) -> dict[str, object]:
    x0, y0, x1, y1 = box
    return {
        "id": f"fixture-line-{line_number}",
        "line": line_number,
        "type": line_type,
        "text": text,
        "confidence": 0.99,
        "region": {
            "top_left_x": round(800 * x0 / 100.0),
            "top_left_y": round(1000 * y0 / 100.0),
            "width": round(800 * (x1 - x0) / 100.0),
            "height": round(1000 * (y1 - y0) / 100.0),
        },
    }


def _mathpix_success_session(
    fixture: SourceFidelityPdfFixture,
) -> _FidelityFakeSession:
    page_lines: dict[int, list[dict[str, object]]] = {page: [] for page in range(1, 5)}
    line_number = 1
    for truth in fixture.truth:
        line_type = {
            "diagram": "diagram",
            "structure": "chemistry",
            "graph": "chart",
        }[truth.kind]
        page_lines[truth.local_page].append(
            _mathpix_line(line_number, line_type, truth.visual_id, truth.source_box)
        )
        line_number += 1
    page_lines[1].append(
        _mathpix_line(line_number, "figure_label", "I=2.50 A", (9.0, 10.0, 29.0, 15.0))
    )
    line_number += 1
    page_lines[2].append(
        _mathpix_line(line_number, "model_label", "NH\u2084\u207a Cl\u207b", (18.0, 28.0, 36.0, 34.0))
    )
    line_number += 1
    page_lines[3].extend(
        (
            _mathpix_line(line_number, "x_axis_label", "t (s)", (73.0, 78.0, 88.0, 84.0)),
            _mathpix_line(line_number + 1, "y_axis_label", "V (m/s)", (11.0, 14.0, 25.0, 20.0)),
        )
    )
    line_number += 2
    page_lines[4].append(
        _mathpix_line(line_number, "figure_label", "\u03bb\u2080", (45.0, 17.0, 56.0, 23.0))
    )
    lines_body = _json_bytes(
        {
            "pages": [
                {
                    "page": page,
                    "page_width": 800,
                    "page_height": 1000,
                    "lines": page_lines[page],
                }
                for page in range(1, 5)
            ]
        }
    )
    page_mmd = (
        "sv-0001 sv-0002 I=2.50 A",
        "sv-0003 NH\u2084\u207a Cl\u207b",
        "sv-0004 t (s) V (m/s)",
        "sv-0005 \u03bb\u2080",
    )
    mmd_body = "\n\\pagebreak\n".join(page_mmd).encode("utf-8")
    pdf_id = "fixture-private-id"
    contexts = [
        _FidelityRequestContext(_FidelityFakeResponse(200, _json_bytes({"pdf_id": pdf_id}))),
        _FidelityRequestContext(
            _FidelityFakeResponse(200, _json_bytes({"pdf_id": pdf_id, "status": "completed"}))
        ),
        _FidelityRequestContext(_FidelityFakeResponse(200, lines_body)),
        _FidelityRequestContext(_FidelityFakeResponse(200, mmd_body)),
        _FidelityRequestContext(
            _FidelityFakeResponse(200, _json_bytes({"pdf_id": pdf_id, "status": "completed"}))
        ),
    ]
    return _FidelityFakeSession(contexts)


def _mathpix_failure_session() -> _FidelityFakeSession:
    return _FidelityFakeSession(
        [_FidelityRequestContext(_FidelityFakeResponse(422, b"{}"))]
    )


@dataclass(frozen=True)
class _ProviderCall:
    schema: str
    filenames: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    prompt: str


class _FidelityProvider:
    def __init__(self, fixture: SourceFidelityPdfFixture):
        self.inventory = _fidelity_inventory(fixture)
        self.calls: list[_ProviderCall] = []
        self.close_counts: dict[int, int] = {}
        self._next_client_id = 0

    @staticmethod
    def _decode_files(contents: list[dict[str, object]]) -> tuple[tuple[str, bytes], ...]:
        decoded: list[tuple[str, bytes]] = []
        for item in contents:
            if item.get("type") != "file":
                continue
            raw_file = item["file"]
            assert isinstance(raw_file, dict)
            filename = raw_file["filename"]
            data_url = raw_file["file_data"]
            assert isinstance(filename, str) and isinstance(data_url, str)
            encoded = data_url.split(",", 1)[1]
            decoded.append((filename, base64.b64decode(encoded, validate=True)))
        return tuple(decoded)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        schema = kwargs["response_format"]["json_schema"]["name"]
        contents = kwargs["messages"][0]["content"]
        assert isinstance(contents, list)
        prompt_item = contents[0]
        assert prompt_item["type"] == "text"
        prompt = prompt_item["text"]
        assert isinstance(prompt, str)
        files = self._decode_files(contents)
        self.calls.append(
            _ProviderCall(
                schema=schema,
                filenames=tuple(filename for filename, _data in files),
                files=files,
                prompt=prompt,
            )
        )
        if schema == "InventoryResponse":
            payload = self.inventory.model_dump_json()
        elif schema == "InventoryAuditResponse":
            payload = _fidelity_audit_from_prompt(prompt).model_dump_json()
        elif schema == "ShortNotes":
            payload = ShortNotes(
                lecture_no="",
                subject="Physics",
                title="Source visual fidelity",
                sections=[
                    Section(
                        heading="Source-grounded overview",
                        points=["Each source visual remains bound to its source order."],
                    )
                ],
            ).model_dump_json()
        else:
            raise AssertionError(f"unexpected provider schema {schema}")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )

    def build_client(self) -> SimpleNamespace:
        client_id = self._next_client_id
        self._next_client_id += 1
        self.close_counts[client_id] = 0
        per_call = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )
        )

        def close() -> None:
            self.close_counts[client_id] += 1

        return SimpleNamespace(
            with_options=lambda **_kwargs: per_call,
            close=close,
        )


def _pixel_box(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    return (
        math.floor(width * box[0] / 100.0),
        math.floor(height * box[1] / 100.0),
        math.ceil(width * box[2] / 100.0),
        math.ceil(height * box[3] / 100.0),
    )


def _clear_source_labels(image: Image.Image, crop: SourceCrop) -> None:
    draw = ImageDraw.Draw(image)
    for label in crop.visual.exact_labels:
        x0, y0, x1, y1 = _pixel_box(label.box, crop.width_px, crop.height_px)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(0, 0, 0, 0))


def _mutate_candidate(
    image: Image.Image,
    crop: SourceCrop,
    mutation: str,
) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    if mutation == "remove_direction_arrow":
        x0, y0, x1, y1 = _pixel_box((75.0, 22.0, 87.0, 36.0), width, height)
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0, 0))
    elif mutation == "remove_curve":
        pixels = image.load()
        for y in range(height):
            for x in range(width):
                red, green, blue, alpha = pixels[x, y]
                if alpha and red >= 120 and red > green * 1.35 and red > blue * 1.15:
                    pixels[x, y] = (0, 0, 0, 0)
    elif mutation == "add_unapproved_component":
        draw.rounded_rectangle(
            (
                round(width * 0.08),
                round(height * 0.72),
                round(width * 0.24),
                round(height * 0.90),
            ),
            radius=max(2, min(width, height) // 40),
            fill=(237, 177, 44, 255),
            outline=(35, 64, 112, 255),
            width=max(2, min(width, height) // 80),
        )
    elif mutation in ("change_label_case", "add_text_outside_approved_regions"):
        text = "anode" if mutation == "change_label_case" else "EXTRA"
        draw.text(
            (round(width * 0.40), round(height * 0.38)),
            text,
            fill=(18, 29, 45, 255),
        )
    elif mutation == "remove_chemistry_structure":
        for component_box in (
            (54.0, 35.0, 61.0, 43.0),
            (46.0, 18.0, 54.0, 30.0),
        ):
            x0, y0, x1, y1 = _pixel_box(component_box, width, height)
            draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0, 0))
    elif mutation == "shift_label_anchor":
        x0, y0, x1, y1 = _pixel_box((46.0, 20.0, 54.0, 29.0), width, height)
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0, 0))
        shifted_x = round(width * 0.62)
        shifted_y = round(height * 0.30)
        draw.line(
            (shifted_x, shifted_y, shifted_x, shifted_y + max(12, height // 10)),
            fill=(33, 101, 172, 255),
            width=max(3, min(width, height) // 70),
        )
        draw.polygon(
            (
                (shifted_x, shifted_y),
                (shifted_x - max(4, width // 80), shifted_y + max(8, height // 80)),
                (shifted_x + max(4, width // 80), shifted_y + max(8, height // 80)),
            ),
            fill=(33, 101, 172, 255),
        )
    else:
        raise AssertionError(f"unknown fidelity mutation {mutation}")
    _clear_source_labels(image, crop)


def _candidate_from_prepared(
    crop: SourceCrop,
    fallback: object,
    mutation: str | None = None,
) -> bytes:
    fallback_png = getattr(fallback, "png")
    with Image.open(io.BytesIO(fallback_png)) as source:
        image = source.convert("RGBA")
    try:
        _clear_source_labels(image, crop)
        if mutation is not None:
            _mutate_candidate(image, crop, mutation)
        return _png(image)
    finally:
        image.close()


def _mutation_verdict(mutation: str) -> RedrawVerdict:
    values: dict[str, object] = {
        "faithful": False,
        "missing_elements": (),
        "extra_elements": (),
        "unapproved_text": (),
        "candidate_text_free": True,
        "label_regions_clear": True,
        "anchors_aligned": True,
        "geometry_aligned": True,
    }
    if mutation == "remove_direction_arrow":
        values["missing_elements"] = ("direction arrow",)
        values["geometry_aligned"] = False
    elif mutation == "remove_curve":
        values["missing_elements"] = ("second curve",)
        values["geometry_aligned"] = False
    elif mutation == "add_unapproved_component":
        values["extra_elements"] = ("unapproved customary component",)
    elif mutation == "change_label_case":
        values["unapproved_text"] = ("Anode changed to anode",)
        values["candidate_text_free"] = False
    elif mutation == "add_text_outside_approved_regions":
        values["unapproved_text"] = ("EXTRA outside approved regions",)
        values["candidate_text_free"] = False
    elif mutation == "shift_label_anchor":
        values["anchors_aligned"] = False
    else:
        raise AssertionError(f"unknown fidelity mutation {mutation}")
    return RedrawVerdict.model_validate(values)


_COMPONENT_FAILURE_NAMES = {
    "sv1-path": "source path",
    "sv1-direction-arrow": "direction arrow",
    "sv2-lens": "lens",
    "sv2-ray-upper": "upper ray",
    "sv2-ray-lower": "lower ray",
    "sv3-bonds": "chemical bonds",
    "sv3-atom-nodes": "atom nodes",
    "sv4-axes": "graph axes",
    "sv4-ticks": "graph ticks",
    "sv4-curve-1": "first curve",
    "sv4-curve-2": "second curve",
    "sv5-foreground-diagram": "dark foreground diagram",
    "sv5-anchor": "source label anchor",
}


def _eight_connected_mask_count(mask: bytes, width: int, height: int) -> int:
    visited = bytearray(len(mask))
    count = 0
    for start, foreground in enumerate(mask):
        if not foreground or visited[start]:
            continue
        count += 1
        visited[start] = 1
        pending = deque((start,))
        while pending:
            index = pending.pop()
            y, x = divmod(index, width)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < width and 0 <= ny < height):
                        continue
                    neighbour = ny * width + nx
                    if mask[neighbour] and not visited[neighbour]:
                        visited[neighbour] = 1
                        pending.append(neighbour)
    return count


def _inspect_transparent_component(
    png: bytes,
    width_px: int,
    height_px: int,
    box: tuple[float, float, float, float],
) -> SourceFidelityComponentObservation:
    with Image.open(io.BytesIO(png)) as opened:
        image = opened.convert("RGBA")
    try:
        assert image.size == (width_px, height_px)
        x0, y0, x1, y1 = _pixel_box(box, width_px, height_px)
        region = image.crop((x0, y0, x1, y1))
        try:
            mask = bytes(
                1 if alpha > 16 else 0
                for _red, _green, _blue, alpha in region.getdata()
            )
        finally:
            region.close()
        support = tuple(index for index, value in enumerate(mask) if value)
        region_width, region_height = x1 - x0, y1 - y0
        if support:
            support_x = tuple(index % region_width + x0 for index in support)
            support_y = tuple(index // region_width + y0 for index in support)
            support_box = (
                min(support_x),
                min(support_y),
                max(support_x),
                max(support_y),
            )
        else:
            support_box = None
        return SourceFidelityComponentObservation(
            mask_sha256=hashlib.sha256(
                struct.pack(">II", region_width, region_height) + mask
            ).hexdigest(),
            foreground_pixels=sum(mask),
            connected_components=_eight_connected_mask_count(
                mask,
                region_width,
                region_height,
            ),
            support_box=support_box,
        )
    finally:
        image.close()


def _visible_extra_pixels(candidate_png: bytes, baseline_png: bytes) -> tuple[tuple[int, int, int, int], ...]:
    with Image.open(io.BytesIO(candidate_png)) as opened_candidate:
        candidate = opened_candidate.convert("RGBA")
    with Image.open(io.BytesIO(baseline_png)) as opened_baseline:
        baseline = opened_baseline.convert("RGBA")
    try:
        assert candidate.size == baseline.size
        return tuple(
            candidate_pixel
            for candidate_pixel, baseline_pixel in zip(
                candidate.getdata(),
                baseline.getdata(),
            )
            if candidate_pixel[3] > 16 and baseline_pixel[3] <= 16
        )
    finally:
        candidate.close()
        baseline.close()


def _component_pixel_verdict(
    crop: SourceCrop,
    candidate_png: bytes,
    baseline_png: bytes,
    components: tuple[SourceFidelityComponentTruth, ...],
) -> tuple[RedrawVerdict, tuple[tuple[str, SourceFidelityComponentObservation], ...]]:
    observations: list[tuple[str, SourceFidelityComponentObservation]] = []
    inspected: list[
        tuple[SourceFidelityComponentTruth, SourceFidelityComponentObservation, bool]
    ] = []
    missing: list[str] = []
    for component in components:
        if component.visual_id != crop.visual.visual_id:
            continue
        observed = _inspect_transparent_component(
            candidate_png,
            crop.width_px,
            crop.height_px,
            component.box,
        )
        observations.append((component.component_id, observed))
        exact = (
            observed.mask_sha256 == component.mask_sha256
            and observed.foreground_pixels == component.foreground_pixels
            and observed.connected_components == component.connected_components
        )
        inspected.append((component, observed, exact))
    anchors_aligned = not any(
        component.role == "anchor" and not exact
        for component, _observed, exact in inspected
    )
    for component, observed, exact in inspected:
        if exact or component.role == "anchor":
            continue
        if component.role == "dark_foreground" and not anchors_aligned:
            # The independently measured anchor is intentionally nested inside
            # the broad dark-diagram support; its mismatch owns this delta.
            continue
        if observed.foreground_pixels < component.foreground_pixels:
            missing.append(_COMPONENT_FAILURE_NAMES[component.component_id])

    extra_pixels = _visible_extra_pixels(candidate_png, baseline_png)
    warm_pixels = sum(
        red >= 150 and green >= 80 and blue <= 110 and red - blue >= 55
        for red, green, blue, _alpha in extra_pixels
    )
    extra_elements: tuple[str, ...] = ()
    unapproved_text: tuple[str, ...] = ()
    if anchors_aligned:
        if warm_pixels >= 10 or len(extra_pixels) >= 300:
            extra_elements = ("unapproved customary component",)
        elif extra_pixels:
            unapproved_text = ("text outside immutable source-label layers",)

    values = {
        "faithful": not missing
        and not extra_elements
        and not unapproved_text
        and anchors_aligned,
        "missing_elements": tuple(missing),
        "extra_elements": extra_elements,
        "unapproved_text": unapproved_text,
        "candidate_text_free": not unapproved_text,
        "label_regions_clear": True,
        "anchors_aligned": anchors_aligned,
        "geometry_aligned": not missing,
    }
    return RedrawVerdict.model_validate(values), tuple(observations)


@dataclass(frozen=True)
class _ExpectedLabelMask:
    text: str
    box: tuple[float, float, float, float]
    source_mask_sha256: str
    width: int
    height: int
    mask: bytes


@dataclass
class _FidelityVisualAdapters:
    components: tuple[SourceFidelityComponentTruth, ...] = ()
    mutation: str | None = None
    failure_mode: str = ""
    redraw_inputs: list[dict[str, object]] = field(default_factory=list)
    attempted_candidates: list[bytes] = field(default_factory=list)
    candidates: list[bytes] = field(default_factory=list)
    corrections: list[RedrawVerdict | None] = field(default_factory=list)
    verdicts: list[RedrawVerdict] = field(default_factory=list)
    budgets: list[VisualRunBudget] = field(default_factory=list)
    labels: OrderedDict[
        str,
        tuple[tuple[str, tuple[float, float, float, float], str], ...],
    ] = field(default_factory=OrderedDict)
    label_masks: OrderedDict[str, tuple[_ExpectedLabelMask, ...]] = field(
        default_factory=OrderedDict
    )
    baseline_candidates: OrderedDict[str, bytes] = field(default_factory=OrderedDict)
    component_observations: list[
        tuple[tuple[str, SourceFidelityComponentObservation], ...]
    ] = field(default_factory=list)

    def observe_fallback(self, crop: SourceCrop, fallback: object) -> None:
        baseline = _candidate_from_prepared(crop, fallback)
        previous_baseline = self.baseline_candidates.setdefault(
            crop.visual.visual_id,
            baseline,
        )
        assert previous_baseline == baseline
        observed_labels = tuple(
            (
                layer.text,
                tuple(round(value, 6) for value in layer.box),
                layer.source_mask_sha256,
            )
            for layer in getattr(fallback, "label_layers")
        )
        previous = self.labels.setdefault(crop.visual.visual_id, observed_labels)
        assert previous == observed_labels
        observed_masks: list[_ExpectedLabelMask] = []
        for layer in getattr(fallback, "label_layers"):
            with Image.open(io.BytesIO(layer.png)) as label_source:
                label_rgba = label_source.convert("RGBA")
            try:
                mask = bytes(
                    1 if alpha > 0 else 0
                    for _red, _green, _blue, alpha in label_rgba.getdata()
                )
                assert hashlib.sha256(mask).hexdigest() == layer.source_mask_sha256
                observed_masks.append(
                    _ExpectedLabelMask(
                        text=layer.text,
                        box=tuple(round(value, 6) for value in layer.box),
                        source_mask_sha256=layer.source_mask_sha256,
                        width=label_rgba.width,
                        height=label_rgba.height,
                        mask=mask,
                    )
                )
            finally:
                label_rgba.close()
        masks = tuple(observed_masks)
        previous_masks = self.label_masks.setdefault(crop.visual.visual_id, masks)
        assert previous_masks == masks

    def redraw(
        self,
        crop: SourceCrop,
        fallback: object,
        budget: VisualRunBudget,
        correction: RedrawVerdict | None,
    ) -> bytes | None:
        budget.take_redraw(crop.visual.visual_id)
        self.budgets.append(budget)
        self.observe_fallback(crop, fallback)
        prompt = image_gen._redraw_prompt(crop, correction)
        model_files = image_gen._crop_model_files(crop)
        self.redraw_inputs.append(
            {
                "visual_id": crop.visual.visual_id,
                "source_png": crop.source_png,
                "context_pngs": tuple(item.source_png for item in crop.context_crops),
                "kind": crop.visual.kind,
                "labels": tuple(
                    (label.box, label.anchor) for label in crop.visual.exact_labels
                ),
                "prompt": prompt,
                "filenames": tuple(item.filename for item in model_files),
            }
        )
        self.corrections.append(correction)

        if self.failure_mode in ("redraw_exception", "bounded_provider_timeout"):
            error = TimeoutError if self.failure_mode == "bounded_provider_timeout" else RuntimeError
            raise error("injected bounded provider failure")
        if self.failure_mode == "malformed_png":
            malformed = b"not-a-decodable-png"
            self.attempted_candidates.append(malformed)
            return malformed
        if self.failure_mode == "valid_opaque_candidate":
            opaque = Image.new(
                "RGBA",
                (crop.width_px, crop.height_px),
                (255, 255, 255, 255),
            )
            try:
                opaque_draw = ImageDraw.Draw(opaque)
                opaque_draw.ellipse(
                    (
                        crop.width_px // 4,
                        crop.height_px // 4,
                        crop.width_px * 3 // 4,
                        crop.height_px * 3 // 4,
                    ),
                    outline=(30, 80, 160, 255),
                    width=max(2, min(crop.width_px, crop.height_px) // 80),
                )
                opaque_png = _png(opaque)
                self.attempted_candidates.append(opaque_png)
                return opaque_png
            finally:
                opaque.close()
        if self.failure_mode == "first_verifier_rejection" and correction is not None:
            raise RuntimeError("injected correction provider failure")

        mutation = self.mutation
        if self.failure_mode == "context_correction":
            assert 0 < len(crop.context_crops) <= 2
            mutation = "remove_direction_arrow" if correction is None else None
        candidate = _candidate_from_prepared(crop, fallback, mutation)
        self.attempted_candidates.append(candidate)
        self.candidates.append(candidate)
        return candidate

    def verify(
        self,
        crop: SourceCrop,
        candidate_png: bytes,
        _fallback: object,
        budget: VisualRunBudget,
    ) -> RedrawVerdict:
        budget.take_verifier(crop.visual.visual_id)
        self.budgets.append(budget)
        if self.failure_mode in (
            "first_verifier_rejection",
            "second_verifier_rejection",
        ):
            verdict = _mutation_verdict("remove_direction_arrow")
            observations: tuple[tuple[str, SourceFidelityComponentObservation], ...] = ()
        else:
            verdict, observations = _component_pixel_verdict(
                crop,
                candidate_png,
                self.baseline_candidates[crop.visual.visual_id],
                self.components,
            )
        self.component_observations.append(observations)
        self.verdicts.append(verdict)
        assert candidate_png in self.candidates
        return verdict


@dataclass(frozen=True)
class _Task11Run:
    fixture: SourceFidelityPdfFixture
    notes: ShortNotes
    manifest: VisualManifest
    crops: OrderedDict[str, SourceCrop]
    assets: dict[str, object]
    budget: VisualRunBudget
    memory: VisualMemoryBudget
    adapters: _FidelityVisualAdapters
    pdf_bytes: bytes
    ledger: tuple[RenderedVisual, ...]
    provider: _FidelityProvider | None = None
    mathpix_session: _FidelityFakeSession | None = None
    final_label_corruptions: tuple[tuple[str, str], ...] = ()


def _capture_built_pdf(
    monkeypatch: pytest.MonkeyPatch,
    notes: ShortNotes,
    *,
    image_transform: Callable[[bytes], bytes] | None = None,
) -> tuple[bytes, tuple[RenderedVisual, ...]]:
    instances: list[NotesPDF] = []
    real_notes_pdf = notes_pdf_module.NotesPDF

    class CapturingNotesPDF(real_notes_pdf):
        def __init__(self, *args: object, **kwargs: object):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def image(self, name: Any, *args: object, **kwargs: object) -> Any:
            if image_transform is not None and isinstance(name, io.BytesIO):
                name = io.BytesIO(image_transform(name.getvalue()))
            return super().image(name, *args, **kwargs)

    monkeypatch.setattr(notes_pdf_module, "NotesPDF", CapturingNotesPDF)
    output = notes_pdf_module.build_notes_pdf(notes, "by QA", watermark=False)
    assert len(instances) == 1
    return output, tuple(instances[0]._rendered_visuals)


def _final_label_dot_corruptor(
    notes: ShortNotes,
    observed: list[tuple[str, str]],
) -> Callable[[bytes], bytes]:
    assets = dict(getattr(notes, "_visual_assets"))
    crops = dict(getattr(notes, "_source_crops"))
    targets: dict[str, tuple[str, bytes, SourceCrop]] = {}
    for visual_id, asset in assets.items():
        payload = asset.primary_png or asset.fallback_png
        targets[hashlib.sha256(payload).hexdigest()] = (visual_id, payload, crops[visual_id])

    def corrupt(payload: bytes) -> bytes:
        target = targets.get(hashlib.sha256(payload).hexdigest())
        if target is None or payload != target[1]:
            return payload
        visual_id, _expected, crop = target
        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert("RGBA")
        try:
            draw = ImageDraw.Draw(image)
            for label in crop.visual.exact_labels:
                x0, y0, x1, y1 = _pixel_box(
                    label.box,
                    crop.width_px,
                    crop.height_px,
                )
                draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(0, 0, 0, 0))
                center_x = (x0 + x1 - 1) // 2
                center_y = (y0 + y1 - 1) // 2
                draw.rectangle(
                    (
                        center_x - 1,
                        center_y - 1,
                        center_x + 1,
                        center_y + 1,
                    ),
                    fill=(15, 20, 35, 255),
                )
                observed.append((visual_id, label.text))
            return _png(image)
        finally:
            image.close()

    return corrupt


def _install_fidelity_mathpix(
    monkeypatch: pytest.MonkeyPatch,
    fixture: SourceFidelityPdfFixture,
    *,
    success: bool,
) -> _FidelityFakeSession:
    session = _mathpix_success_session(fixture) if success else _mathpix_failure_session()
    monkeypatch.setattr(config, "MATHPIX_ENABLED", True)
    monkeypatch.setattr(config, "MATHPIX_APP_ID", "fixture-app-id")
    monkeypatch.setattr(config, "MATHPIX_APP_KEY", "fixture-app-key")
    monkeypatch.setattr(config, "MATHPIX_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(mathpix_client, "_default_session", lambda: session)
    return session


def _run_public_fidelity_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mathpix_success: bool = True,
    max_images: int = 4,
    mutation: str | None = None,
    failure_mode: str = "",
    corrupt_final_labels: bool = False,
) -> _Task11Run:
    fixture = make_source_fidelity_pdf()
    session = _install_fidelity_mathpix(
        monkeypatch,
        fixture,
        success=mathpix_success,
    )
    provider = _FidelityProvider(fixture)
    monkeypatch.setattr(config, "MAX_IMAGES", max_images)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", provider.build_client)

    adapters = _FidelityVisualAdapters(
        components=fixture.components,
        mutation=mutation,
        failure_mode=failure_mode,
    )
    real_render_visuals = image_gen.render_visuals
    real_prepare_fallback = image_gen.prepare_source_fallback

    def prepare_observed_fallback(
        crop: SourceCrop,
        budget: VisualRunBudget,
        memory: VisualMemoryBudget,
    ) -> object:
        fallback = real_prepare_fallback(crop, budget, memory)
        adapters.observe_fallback(crop, fallback)
        return fallback

    def render_with_adapters(
        notes: ShortNotes,
        manifest: VisualManifest,
        crops: OrderedDict[str, SourceCrop],
        budget: VisualRunBudget,
        memory: VisualMemoryBudget,
    ) -> dict[str, object] | None:
        return real_render_visuals(
            notes,
            manifest,
            crops,
            budget,
            memory,
            redraw=adapters.redraw,
            verify=adapters.verify,
        )

    monkeypatch.setattr(image_gen, "render_visuals", render_with_adapters)
    monkeypatch.setattr(image_gen, "prepare_source_fallback", prepare_observed_fallback)
    bundle = gemini_client._generate_manifest_note_bundle(
        fixture.pdf_bytes,
        language="english",
        subject_hint="Physics",
    )
    notes = bundle.notes
    manifest = bundle.manifest
    crops = OrderedDict(gemini_client._ordered_source_crop_map(bundle))
    rendered = image_gen.render_visuals(
        notes,
        manifest,
        crops,
        bundle.budget,
        bundle.memory,
    )
    assets = gemini_client._validated_visual_asset_map(manifest, rendered)
    object.__setattr__(notes, "_visual_manifest", manifest)
    object.__setattr__(notes, "_source_crops", crops)
    object.__setattr__(notes, "_visual_assets", assets)
    object.__setattr__(notes, "_visual_run_budget", bundle.budget)
    object.__setattr__(notes, "_visual_memory_budget", bundle.memory)
    budget = bundle.budget
    memory = bundle.memory
    final_label_corruptions: list[tuple[str, str]] = []
    image_transform = (
        _final_label_dot_corruptor(notes, final_label_corruptions)
        if corrupt_final_labels
        else None
    )
    pdf_bytes, ledger = _capture_built_pdf(
        monkeypatch,
        notes,
        image_transform=image_transform,
    )
    return _Task11Run(
        fixture=fixture,
        notes=notes,
        manifest=manifest,
        crops=crops,
        assets=assets,
        budget=budget,
        memory=memory,
        adapters=adapters,
        pdf_bytes=pdf_bytes,
        ledger=ledger,
        provider=provider,
        mathpix_session=session,
        final_label_corruptions=tuple(final_label_corruptions),
    )


def _task11_memory() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=192 * 1024 * 1024,
        max_source_bytes=64 * 1024 * 1024,
        max_context_bytes=32 * 1024 * 1024,
        max_asset_bytes=96 * 1024 * 1024,
        max_page_cache_bytes=32 * 1024 * 1024,
    )


def _selected_source_visual(
    truth: SourceFidelityTruth,
    *,
    source_order: int,
) -> SourceVisual:
    topic_key = f"fixture-selected-{truth.visual_id}"
    x0, y0, x1, _y1 = truth.source_box
    contexts: tuple[SourceContext, ...] = ()
    if truth.visual_id == "sv-0001":
        contexts = (
            SourceContext(
                topic_key=topic_key,
                local_page=truth.local_page,
                box=(7.0, 9.0, 34.0, 17.0),
                role="explanation",
                exact_text="The source arrow fixes the intended direction.",
            ),
        )
    return SourceVisual(
        visual_id=truth.visual_id,
        chunk_index=0,
        local_page=truth.local_page,
        global_page=truth.local_page,
        box=truth.source_box,
        kind=truth.kind,
        topic_anchor=SourceTopicAnchor(
            topic_key=topic_key,
            local_page=truth.local_page,
            box=(x0, max(0.0, y0 - 5.0), min(x1, x0 + 30.0), y0),
            exact_heading=f"Selected source {truth.visual_id}",
        ),
        exact_labels=tuple(
            SourceLabel(text=label.text, box=label.box, anchor=label.anchor)
            for label in truth.labels
        ),
        context_regions=contexts,
        section_hint="",
        source_order=source_order,
    )


def _assert_fixture_source_truth_before_model_pipeline(
    fixture: SourceFidelityPdfFixture,
) -> None:
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)
    memory = _task11_memory()
    try:
        chunks = tuple(split_pdf_chunks(fixture.pdf_bytes, budget=budget))
        visuals = tuple(
            _selected_source_visual(truth, source_order=index)
            for index, truth in enumerate(fixture.truth, start=1)
        )
        crops = extract_source_crops(
            fixture.pdf_bytes,
            visuals,
            chunks,
            budget,
            memory,
            render_scale=config.VISUAL_PAGE_RENDER_SCALE,
            max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
            max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
            max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
        )
        crop_by_id = {crop.visual.visual_id: crop for crop in crops}
        assert tuple(crop_by_id) == SOURCE_FIDELITY_IDS
        for component in fixture.components:
            crop = crop_by_id[component.visual_id]
            observed = inspect_source_fidelity_component(
                crop.source_png,
                crop.width_px,
                crop.height_px,
                component.box,
            )
            assert observed.mask_sha256 == component.mask_sha256
            assert observed.foreground_pixels == component.foreground_pixels
            assert observed.connected_components == component.connected_components
            assert observed.support_box is not None
    finally:
        memory.close()
    assert memory.resident_bytes() == 0


def _run_selected_fidelity_visual(
    monkeypatch: pytest.MonkeyPatch,
    visual_id: str,
    *,
    mutation: str | None = None,
    failure_mode: str = "",
    max_images: int = 1,
) -> _Task11Run:
    fixture = make_source_fidelity_pdf()
    truth_index = next(
        index for index, item in enumerate(fixture.truth) if item.visual_id == visual_id
    )
    truth = fixture.truth[truth_index]
    budget = VisualRunBudget(deadline_at=100.0, clock=lambda: 1.0)
    memory = _task11_memory()
    chunks = tuple(split_pdf_chunks(fixture.pdf_bytes, budget=budget))
    assert len(chunks) == 1
    visual = _selected_source_visual(truth, source_order=truth_index + 1)
    manifest = VisualManifest(visuals=(visual,))
    extracted = extract_source_crops(
        fixture.pdf_bytes,
        manifest.visuals,
        chunks,
        budget,
        memory,
        render_scale=config.VISUAL_PAGE_RENDER_SCALE,
        max_page_dimension_px=config.MAX_VISUAL_PAGE_DIMENSION_PX,
        max_crop_pixels=config.MAX_VISUAL_CROP_PIXELS,
        max_png_bytes=config.MAX_VISUAL_PNG_BYTES,
    )
    crops = OrderedDict((crop.visual.visual_id, crop) for crop in extracted)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title=f"Selected fidelity {visual_id}",
        sections=[
            Section(
                heading="",
                points=["Source-grounded selected visual."],
                visual_ids=[visual_id],
            )
        ],
    )
    adapters = _FidelityVisualAdapters(
        components=fixture.components,
        mutation=mutation,
        failure_mode=failure_mode,
    )
    real_prepare_fallback = image_gen.prepare_source_fallback

    def prepare_observed_fallback(
        crop: SourceCrop,
        budget: VisualRunBudget,
        memory: VisualMemoryBudget,
    ) -> object:
        fallback = real_prepare_fallback(crop, budget, memory)
        adapters.observe_fallback(crop, fallback)
        return fallback

    monkeypatch.setattr(image_gen, "prepare_source_fallback", prepare_observed_fallback)
    monkeypatch.setattr(config, "MAX_IMAGES", max_images)
    assets = image_gen.render_visuals(
        notes,
        manifest,
        crops,
        budget,
        memory,
        redraw=adapters.redraw,
        verify=adapters.verify,
    )
    assert isinstance(assets, dict)
    object.__setattr__(notes, "_visual_manifest", manifest)
    object.__setattr__(notes, "_source_crops", dict(crops))
    object.__setattr__(notes, "_visual_assets", dict(assets))
    object.__setattr__(notes, "_visual_run_budget", budget)
    object.__setattr__(notes, "_visual_memory_budget", memory)
    pdf_bytes, ledger = _capture_built_pdf(monkeypatch, notes)
    return _Task11Run(
        fixture=fixture,
        notes=notes,
        manifest=manifest,
        crops=crops,
        assets=assets,
        budget=budget,
        memory=memory,
        adapters=adapters,
        pdf_bytes=pdf_bytes,
        ledger=ledger,
    )


def _pdf_text(pdf_bytes: bytes) -> str:
    return "\n".join(
        page.extract_text() or ""
        for page in PdfReader(io.BytesIO(pdf_bytes)).pages
    )


def _rasterized_pages(pdf_bytes: bytes) -> tuple[Image.Image, ...]:
    document = pdfium.PdfDocument(pdf_bytes)
    pages: list[Image.Image] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap = None
            try:
                bitmap = page.render(scale=2.0, rev_byteorder=True)
                pages.append(bitmap.to_pil().convert("RGBA"))
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return tuple(pages)


def _slot_pixel_box(
    slot: RenderedVisual,
    page: Image.Image,
    *,
    expand: int = 2,
) -> tuple[int, int, int, int]:
    scale = 2.0
    x0 = math.floor(slot.x_mm / 25.4 * 72.0 * scale)
    y0 = math.floor(slot.y_mm / 25.4 * 72.0 * scale)
    x1 = x0 + math.ceil(slot.width_mm / 25.4 * 72.0 * scale)
    y1 = y0 + math.ceil(slot.height_mm / 25.4 * 72.0 * scale)
    return (
        max(0, x0 - expand),
        max(0, y0 - expand),
        min(page.width, x1 + expand),
        min(page.height, y1 + expand),
    )


def _foreground_pixel(red: int, green: int, blue: int, alpha: int) -> bool:
    return alpha >= 128 and (
        min(red, green, blue) <= 210
        or max(red, green, blue) - min(red, green, blue) >= 28
    )


def _foreground_fraction(image: Image.Image) -> float:
    pixels = tuple(image.getdata())
    return sum(_foreground_pixel(*pixel) for pixel in pixels) / max(1, len(pixels))


def _assert_final_slots_have_foreground(run: _Task11Run) -> None:
    pages = _rasterized_pages(run.pdf_bytes)
    try:
        assert len(run.ledger) == len(run.manifest.visuals)
        for slot in run.ledger:
            assert slot.x_mm > 0.0
            assert slot.y_mm > 0.0
            assert slot.width_mm > 0.0
            assert slot.height_mm > 0.0
            page = pages[slot.page_number - 1]
            slot_image = page.crop(_slot_pixel_box(slot, page))
            try:
                assert _foreground_fraction(slot_image) > 0.005
            finally:
                slot_image.close()
    finally:
        for page in pages:
            page.close()


@dataclass(frozen=True)
class _LabelSpatialMetric:
    visual_id: str
    text: str
    recall: float
    precision: float
    width_extent: float
    height_extent: float
    expected_pixels: int
    actual_pixels: int

    @property
    def faithful(self) -> bool:
        return (
            self.expected_pixels > 0
            and self.actual_pixels > 0
            and self.recall >= 0.70
            and self.precision >= 0.50
            and self.width_extent >= 0.55
            and self.height_extent >= 0.55
        )


def _mask_support_extent(mask: Image.Image) -> tuple[int, int]:
    box = mask.getbbox()
    if box is None:
        return 0, 0
    return box[2] - box[0], box[3] - box[1]


def _balanced_extent(actual: int, expected: int) -> float:
    if actual <= 0 or expected <= 0:
        return 0.0
    return min(actual / expected, expected / actual)


def _label_spatial_metrics(run: _Task11Run) -> tuple[_LabelSpatialMetric, ...]:
    pages = _rasterized_pages(run.pdf_bytes)
    metrics: list[_LabelSpatialMetric] = []
    try:
        for slot in run.ledger:
            page = pages[slot.page_number - 1]
            sx0, sy0, sx1, sy1 = _slot_pixel_box(slot, page, expand=0)
            width, height = sx1 - sx0, sy1 - sy0
            expected_labels = run.adapters.label_masks[slot.visual_id]
            for expected in expected_labels:
                lx0, ly0, lx1, ly1 = _pixel_box(expected.box, width, height)
                target_width = max(1, lx1 - lx0)
                target_height = max(1, ly1 - ly0)
                label_image = page.crop((sx0 + lx0, sy0 + ly0, sx0 + lx1, sy0 + ly1))
                source_mask = Image.new("L", (expected.width, expected.height))
                source_mask.putdata(tuple(255 if value else 0 for value in expected.mask))
                expected_mask = source_mask.resize(
                    (target_width, target_height),
                    Image.Resampling.NEAREST,
                )
                actual_mask = Image.new("L", label_image.size)
                actual_mask.putdata(
                    tuple(
                        255 if _foreground_pixel(*pixel) else 0
                        for pixel in label_image.getdata()
                    )
                )
                try:
                    dilated_actual = actual_mask.filter(ImageFilter.MaxFilter(7))
                    dilated_expected = expected_mask.filter(ImageFilter.MaxFilter(7))
                    expected_values = tuple(value > 0 for value in expected_mask.getdata())
                    actual_values = tuple(value > 0 for value in actual_mask.getdata())
                    dilated_actual_values = tuple(
                        value > 0 for value in dilated_actual.getdata()
                    )
                    dilated_expected_values = tuple(
                        value > 0 for value in dilated_expected.getdata()
                    )
                    expected_pixels = sum(expected_values)
                    actual_pixels = sum(actual_values)
                    recall = sum(
                        expected_pixel and actual_pixel
                        for expected_pixel, actual_pixel in zip(
                            expected_values,
                            dilated_actual_values,
                        )
                    ) / max(1, expected_pixels)
                    precision = sum(
                        actual_pixel and expected_pixel
                        for actual_pixel, expected_pixel in zip(
                            actual_values,
                            dilated_expected_values,
                        )
                    ) / max(1, actual_pixels)
                    actual_width, actual_height = _mask_support_extent(actual_mask)
                    expected_width, expected_height = _mask_support_extent(expected_mask)
                    metrics.append(
                        _LabelSpatialMetric(
                            visual_id=slot.visual_id,
                            text=expected.text,
                            recall=recall,
                            precision=precision,
                            width_extent=_balanced_extent(actual_width, expected_width),
                            height_extent=_balanced_extent(actual_height, expected_height),
                            expected_pixels=expected_pixels,
                            actual_pixels=actual_pixels,
                        )
                    )
                finally:
                    source_mask.close()
                    expected_mask.close()
                    actual_mask.close()
                    dilated_actual.close()
                    dilated_expected.close()
                    label_image.close()
    finally:
        for page in pages:
            page.close()
    return tuple(metrics)


def _assert_final_label_masks_survive_pdf_render(run: _Task11Run) -> None:
    metrics = _label_spatial_metrics(run)
    expected_count = sum(
        len(visual.exact_labels) for visual in run.manifest.visuals
    )
    assert len(metrics) == expected_count
    if expected_count == 0:
        return
    assert metrics
    failures = tuple(
        (
            metric.visual_id,
            metric.text,
            round(metric.recall, 4),
            round(metric.precision, 4),
            round(metric.width_extent, 4),
            round(metric.height_extent, 4),
        )
        for metric in metrics
        if not metric.faithful
    )
    assert failures == ()


def _near_black_fraction(image: Image.Image) -> float:
    rgba = image.convert("RGBA")
    try:
        pixels = tuple(rgba.getdata())
        near_black = sum(
            alpha >= 250 and max(red, green, blue) <= 40
            for red, green, blue, alpha in pixels
        )
        return near_black / max(1, len(pixels))
    finally:
        rgba.close()


def _assert_dark_source_and_final_slot(run: _Task11Run, visual_id: str) -> None:
    crop = run.crops[visual_id]
    with Image.open(io.BytesIO(crop.source_png)) as source:
        source_rgba = source.convert("RGBA")
    try:
        assert _near_black_fraction(source_rgba) > 0.70
    finally:
        source_rgba.close()
    pages = _rasterized_pages(run.pdf_bytes)
    try:
        slot = next(item for item in run.ledger if item.visual_id == visual_id)
        page = pages[slot.page_number - 1]
        final_slot = page.crop(_slot_pixel_box(slot, page, expand=0))
        try:
            assert _near_black_fraction(final_slot) < 0.05
        finally:
            final_slot.close()
    finally:
        for page in pages:
            page.close()


def _expected_label_geometry(
    fixture: SourceFidelityPdfFixture,
) -> OrderedDict[str, tuple[tuple[str, tuple[float, float, float, float]], ...]]:
    return OrderedDict(
        (
            truth.visual_id,
            tuple(
                (label.text, tuple(round(value, 6) for value in label.box))
                for label in truth.labels
            ),
        )
        for truth in fixture.truth
    )


def _assert_label_text_and_boxes(run: _Task11Run) -> None:
    all_expected = _expected_label_geometry(run.fixture)
    observed = OrderedDict(
        (
            visual_id,
            tuple((text, box) for text, box, _hash in labels),
        )
        for visual_id, labels in run.adapters.labels.items()
    )
    assert tuple(observed) == tuple(
        visual_id for visual_id in all_expected if visual_id in observed
    )
    expected = OrderedDict(
        (visual_id, all_expected[visual_id]) for visual_id in observed
    )
    assert observed == expected


def _schema_calls(provider: _FidelityProvider, schema: str) -> tuple[_ProviderCall, ...]:
    return tuple(call for call in provider.calls if call.schema == schema)


EXPECTED_FIDELITY_GLYPH_HASHES = (
    # Hand-recorded from the deterministic source fixture, independent of the
    # tuple assembled by the assertion below. Each item is source-order ID plus
    # ordered SHA-256 masks for its immutable exact glyph layers.
    (
        "sv-0001",
        (
            "1c20341af67131515ec179667f5e94bb1663edd08e19cb74ad3a5bd687571db5",
            "570288d486e4b1a56b343b69119954e560818cd4d1628f6e6a789273358aea75",
            "0444d5fdcc1a64d607f53f45fcae96a998a07bf532d7611f7f48fb3215e13a1d",
        ),
    ),
    ("sv-0002", ()),
    (
        "sv-0003",
        (
            "6dfcc5c00e4cd212b1e4b384511db8a725968268e72bc10b8ee29d481db66c0d",
            "3734f3efa4c94802090076c6404ed7b06d73fbd7a30ac87a23bd45ab2ad50e98",
            "507bb426a08b63474e6150f74ef23394e1a695584a0169e0bf77ecccc9e6b149",
            "1d0b9068e999d10f03b11deb0773cb0a9c6eda41c5ce79b22216f6a4c8c4b10f",
            "cc72f83342c3c6a1a1d954d5838489e2d24996dfe65f0a48f6e6e3d0a88a53b5",
        ),
    ),
    (
        "sv-0004",
        (
            "7c6eff4508e59c3e1cd27e30724e6fb5462fbdcd86dac5294cc49b86d0112e92",
            "1fe1c7a7b2ea94ec26216cf19c9534976cbcb93369cf8a448ecd6213972e48e0",
        ),
    ),
    (
        "sv-0005",
        (
            "b6b85ce88678154463841f67747ffabdc4a17aae5ee4c2314b99de5098c1f6d4",
        ),
    ),
)

EXPECTED_FIDELITY_COMPONENTS = (
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


def _assert_source_component_truth(run: _Task11Run) -> None:
    selected = set(manifest_ids(run.manifest))
    expected = tuple(
        item for item in EXPECTED_FIDELITY_COMPONENTS if item[1] in selected
    )
    components = tuple(
        component
        for component in run.fixture.components
        if component.visual_id in selected
    )
    assert tuple(
        (component.component_id, component.visual_id, component.role)
        for component in components
    ) == expected
    assert tuple(run.adapters.baseline_candidates) == tuple(manifest_ids(run.manifest))
    for component in components:
        crop = run.crops[component.visual_id]
        source_observed = inspect_source_fidelity_component(
            crop.source_png,
            crop.width_px,
            crop.height_px,
            component.box,
        )
        prepared_observed = _inspect_transparent_component(
            run.adapters.baseline_candidates[component.visual_id],
            crop.width_px,
            crop.height_px,
            component.box,
        )
        for observed in (source_observed, prepared_observed):
            assert observed.mask_sha256 == component.mask_sha256
            assert observed.foreground_pixels == component.foreground_pixels
            assert observed.connected_components == component.connected_components
            assert observed.support_box is not None


def test_generate_to_pdf_closes_every_source_visual_exactly_once(monkeypatch):
    _assert_fixture_source_truth_before_model_pipeline(make_source_fidelity_pdf())
    run = _run_public_fidelity_pipeline(monkeypatch, max_images=4)
    _assert_source_component_truth(run)

    expected_states = (
        ("sv-0001", "verified_redraw"),
        ("sv-0002", "verified_redraw"),
        ("sv-0003", "verified_redraw"),
        ("sv-0004", "verified_redraw"),
        ("sv-0005", "sketch_source_fallback"),
    )
    assert manifest_ids(run.manifest) == SOURCE_FIDELITY_IDS
    assert tuple(run.assets) == SOURCE_FIDELITY_IDS
    assert tuple((key, asset.state) for key, asset in run.assets.items()) == expected_states
    assert tuple((slot.visual_id, slot.state) for slot in run.ledger) == expected_states
    assert tuple(
        (visual.local_page, visual.global_page) for visual in run.manifest.visuals
    ) == ((1, 1), (1, 1), (2, 2), (3, 3), (4, 4))
    assert tuple(
        visual_id
        for section in run.notes.sections
        for visual_id in section.visual_ids
    ) == SOURCE_FIDELITY_IDS
    assert run.notes.sections[-1].heading == ""
    assert getattr(run.notes, "_visual_run_budget") is run.budget
    assert getattr(run.notes, "_visual_memory_budget") is run.memory
    assert run.adapters.budgets
    assert all(item is run.budget for item in run.adapters.budgets)
    assert run.budget.coverage_probes_total == 100
    assert run.memory._closed is True
    assert run.memory.resident_bytes() == 0
    _assert_final_slots_have_foreground(run)
    _assert_final_label_masks_survive_pdf_render(run)
    assert MANUAL_PLACEHOLDER_HEADING not in _pdf_text(run.pdf_bytes)
    assert len(hashlib.sha256(run.pdf_bytes).hexdigest()) == 64


@pytest.mark.parametrize(
    ("visual_id", "mutation", "failed_field"),
    (
        ("sv-0001", "remove_direction_arrow", "missing_elements"),
        ("sv-0004", "remove_curve", "missing_elements"),
        ("sv-0002", "add_unapproved_component", "extra_elements"),
        ("sv-0003", "remove_chemistry_structure", "missing_elements"),
        ("sv-0003", "change_label_case", "unapproved_text"),
        ("sv-0003", "add_text_outside_approved_regions", "unapproved_text"),
        ("sv-0005", "shift_label_anchor", "anchors_aligned"),
    ),
)
def test_rejected_added_or_missing_component_never_reaches_final_pdf(
    monkeypatch,
    visual_id,
    mutation,
    failed_field,
):
    run = _run_selected_fidelity_visual(
        monkeypatch,
        visual_id,
        mutation=mutation,
    )
    _assert_source_component_truth(run)

    asset = run.assets[visual_id]
    assert asset.state == "sketch_source_fallback"
    assert asset.primary_png == asset.fallback_png
    assert tuple((item.visual_id, item.state) for item in run.ledger) == (
        (visual_id, "sketch_source_fallback"),
    )
    assert len(run.adapters.candidates) == 2
    assert len(run.adapters.verdicts) == 2
    expected_verdict = run.adapters.verdicts[0]
    assert run.adapters.verdicts == [expected_verdict, expected_verdict]
    if failed_field == "anchors_aligned":
        assert getattr(expected_verdict, failed_field) is False
    else:
        assert getattr(expected_verdict, failed_field)
    assert run.adapters.corrections == [None, expected_verdict]
    expected_component_ids = tuple(
        component.component_id
        for component in run.fixture.components
        if component.visual_id == visual_id
    )
    assert len(run.adapters.component_observations) == 2
    assert all(
        tuple(component_id for component_id, _observation in observations)
        == expected_component_ids
        for observations in run.adapters.component_observations
    )
    designated_components = {
        "remove_direction_arrow": ("sv1-direction-arrow",),
        "remove_curve": ("sv4-curve-2",),
        "remove_chemistry_structure": ("sv3-bonds", "sv3-atom-nodes"),
        "shift_label_anchor": ("sv5-anchor",),
    }.get(mutation, ())
    truth_by_id = {
        component.component_id: component for component in run.fixture.components
    }
    for observations in run.adapters.component_observations:
        observed_by_id = dict(observations)
        for component_id in designated_components:
            observed_component = observed_by_id[component_id]
            component_truth = truth_by_id[component_id]
            assert observed_component.mask_sha256 != component_truth.mask_sha256
            assert (
                observed_component.foreground_pixels
                < component_truth.foreground_pixels
            )
    if mutation in (
        "add_unapproved_component",
        "change_label_case",
        "add_text_outside_approved_regions",
    ):
        extra_pixel_sets = tuple(
            _visible_extra_pixels(candidate, run.adapters.baseline_candidates[visual_id])
            for candidate in run.adapters.candidates
        )
        assert all(extra_pixels for extra_pixels in extra_pixel_sets)
        warm_counts = tuple(
            sum(
                red >= 150
                and green >= 80
                and blue <= 110
                and red - blue >= 55
                for red, green, blue, _alpha in extra_pixels
            )
            for extra_pixels in extra_pixel_sets
        )
        if mutation == "add_unapproved_component":
            assert all(count >= 10 for count in warm_counts)
        else:
            assert warm_counts == (0, 0)
    assert all(candidate != asset.primary_png for candidate in run.adapters.candidates)
    baseline = _candidate_from_prepared(
        run.crops[visual_id],
        SimpleNamespace(png=asset.fallback_png),
    )
    assert all(candidate != baseline for candidate in run.adapters.candidates)
    _assert_label_text_and_boxes(run)
    _assert_final_slots_have_foreground(run)
    _assert_final_label_masks_survive_pdf_render(run)
    assert MANUAL_PLACEHOLDER_HEADING not in _pdf_text(run.pdf_bytes)


def test_dark_source_visual_shows_page_background_without_black_rectangle(monkeypatch):
    run = _run_selected_fidelity_visual(
        monkeypatch,
        "sv-0005",
        max_images=0,
    )

    assert run.assets["sv-0005"].state == "sketch_source_fallback"
    assert tuple((slot.visual_id, slot.state) for slot in run.ledger) == (
        ("sv-0005", "sketch_source_fallback"),
    )
    _assert_dark_source_and_final_slot(run, "sv-0005")
    _assert_final_slots_have_foreground(run)
    _assert_final_label_masks_survive_pdf_render(run)


def test_exact_source_label_layers_survive_generation_and_pdf_render(monkeypatch):
    run = _run_public_fidelity_pipeline(monkeypatch, max_images=5)

    assert tuple(run.adapters.labels) == SOURCE_FIDELITY_IDS
    _assert_label_text_and_boxes(run)
    observed_hashes = tuple(
        (
            visual_id,
            tuple(mask_hash for _text, _box, mask_hash in labels),
        )
        for visual_id, labels in run.adapters.labels.items()
    )
    assert observed_hashes == EXPECTED_FIDELITY_GLYPH_HASHES
    flattened_text = tuple(
        text
        for labels in run.adapters.labels.values()
        for text, _box, _hash in labels
    )
    assert flattened_text == (
        "I=2.50 A",
        "R\u2081",
        "\u03b1",
        "NH\u2084\u207a",
        "Cl\u207b",
        "pH=7.00",
        "Anode",
        "anode",
        "t (s)",
        "V (m/s)",
        "\u03bb\u2080",
    )
    assert "Anode" != "anode"
    _assert_final_label_masks_survive_pdf_render(run)


def test_real_final_write_rejects_centered_dot_in_every_exact_label_region(
    monkeypatch,
):
    run = _run_public_fidelity_pipeline(
        monkeypatch,
        max_images=5,
        corrupt_final_labels=True,
    )

    expected = tuple(
        (truth.visual_id, label.text)
        for truth in run.fixture.truth
        for label in truth.labels
    )
    assert len(expected) == 11
    assert run.final_label_corruptions == expected
    metrics = _label_spatial_metrics(run)
    assert tuple((metric.visual_id, metric.text) for metric in metrics) == expected
    # Every corrupt region still contains dark foreground, so the former
    # any-foreground oracle would accept all eleven centered dots.
    assert all(metric.actual_pixels > 0 for metric in metrics)
    assert tuple(
        (metric.visual_id, metric.text)
        for metric in metrics
        if not metric.faithful
    ) == expected


def test_inventory_over_redraw_budget_still_renders_all_visuals(monkeypatch):
    run = _run_public_fidelity_pipeline(monkeypatch, max_images=2)

    expected_states = (
        ("sv-0001", "verified_redraw"),
        ("sv-0002", "verified_redraw"),
        ("sv-0003", "sketch_source_fallback"),
        ("sv-0004", "sketch_source_fallback"),
        ("sv-0005", "sketch_source_fallback"),
    )
    assert tuple((key, asset.state) for key, asset in run.assets.items()) == expected_states
    assert tuple((slot.visual_id, slot.state) for slot in run.ledger) == expected_states
    assert run.budget.redraw_calls == {"sv-0001": 1, "sv-0002": 1}
    assert run.budget.verifier_calls == {"sv-0001": 1, "sv-0002": 1}
    assert len(run.assets) == len(run.ledger) == 5
    _assert_final_slots_have_foreground(run)


@pytest.mark.parametrize(
    "failure_mode",
    (
        "redraw_exception",
        "malformed_png",
        "valid_opaque_candidate",
        "first_verifier_rejection",
        "second_verifier_rejection",
        "bounded_provider_timeout",
    ),
)
def test_every_visual_failure_path_renders_one_sketch_source_fallback_and_no_manual_placeholder(
    monkeypatch,
    failure_mode,
):
    run = _run_selected_fidelity_visual(
        monkeypatch,
        "sv-0001",
        failure_mode=failure_mode,
    )

    asset = run.assets["sv-0001"]
    assert asset.state == "sketch_source_fallback"
    assert asset.primary_png == asset.fallback_png
    assert tuple((slot.visual_id, slot.state) for slot in run.ledger) == (
        ("sv-0001", "sketch_source_fallback"),
    )
    assert run.budget.redraw_calls == {"sv-0001": 2}
    assert len(run.adapters.redraw_inputs) == 2
    expected_attempted_candidates = {
        "redraw_exception": 0,
        "malformed_png": 2,
        "valid_opaque_candidate": 2,
        "first_verifier_rejection": 1,
        "second_verifier_rejection": 2,
        "bounded_provider_timeout": 0,
    }
    assert len(run.adapters.attempted_candidates) == expected_attempted_candidates[
        failure_mode
    ]
    if failure_mode == "malformed_png":
        for attempted in run.adapters.attempted_candidates:
            with pytest.raises(UnidentifiedImageError):
                with Image.open(io.BytesIO(attempted)) as malformed:
                    malformed.load()
    elif failure_mode == "valid_opaque_candidate":
        crop = run.crops["sv-0001"]
        for attempted in run.adapters.attempted_candidates:
            with Image.open(io.BytesIO(attempted)) as opened:
                assert opened.format == "PNG"
                rgba = opened.convert("RGBA")
            try:
                assert rgba.size == (crop.width_px, crop.height_px)
                pixels = tuple(rgba.getdata())
                assert all(alpha == 255 for _red, _green, _blue, alpha in pixels)
                neutral_light = sum(
                    max(red, green, blue) - min(red, green, blue) <= 18
                    and red * 0.2126 + green * 0.7152 + blue * 0.0722 >= 180.0
                    for red, green, blue, _alpha in pixels
                ) / len(pixels)
                assert neutral_light >= 0.92
            finally:
                rgba.close()
            decoded = image_gen._decode_exact_rgba(
                attempted,
                crop.width_px,
                crop.height_px,
                max_bytes=config.MAX_VISUAL_PNG_BYTES,
                max_pixels=config.MAX_VISUAL_CROP_PIXELS,
            )
            assert image_gen._contains_opaque_neutral_rectangle(decoded) is True
    expected_verifiers = {
        "redraw_exception": 0,
        "malformed_png": 0,
        "valid_opaque_candidate": 0,
        "first_verifier_rejection": 1,
        "second_verifier_rejection": 2,
        "bounded_provider_timeout": 0,
    }
    verifier_count = expected_verifiers[failure_mode]
    assert run.budget.verifier_calls == (
        {"sv-0001": verifier_count} if verifier_count else {}
    )
    if failure_mode == "bounded_provider_timeout":
        assert run.budget.remaining_seconds() == pytest.approx(99.0)
    for section in run.notes.sections:
        assert section.diagram == ""
        assert "_image" not in section.__dict__
        assert "_svg" not in section.__dict__
    assert MANUAL_PLACEHOLDER_HEADING not in _pdf_text(run.pdf_bytes)
    _assert_final_slots_have_foreground(run)


def test_source_topic_context_improves_disambiguation_without_authorizing_extra_content(
    monkeypatch,
):
    run = _run_selected_fidelity_visual(
        monkeypatch,
        "sv-0001",
        failure_mode="context_correction",
    )

    assert run.assets["sv-0001"].state == "verified_redraw"
    assert len(run.adapters.redraw_inputs) == 2
    assert len(run.adapters.verdicts) == 2
    assert run.adapters.verdicts[0].missing_elements == ("direction arrow",)
    assert run.adapters.verdicts[1] == _pass_verdict()
    assert run.adapters.corrections == [None, run.adapters.verdicts[0]]
    for call in run.adapters.redraw_inputs:
        assert set(call) == {
            "visual_id",
            "source_png",
            "context_pngs",
            "kind",
            "labels",
            "prompt",
            "filenames",
        }
        assert call["source_png"] is run.crops["sv-0001"].source_png
        assert 0 < len(call["context_pngs"]) <= 2
        assert call["kind"] == "diagram"
        assert call["labels"] == tuple(
            (label.box, label.anchor)
            for label in run.manifest.visuals[0].exact_labels
        )
        assert call["filenames"] == (
            "sv-0001-source.png",
            "context-01.png",
        )
        prompt = call["prompt"]
        assert NOTE_PROSE_SENTINEL not in prompt
        assert run.manifest.visuals[0].context_regions[0].exact_text not in prompt
        assert "genuinely transparent RGBA background" in prompt
        assert "APPROVED_NON_TEXT_METADATA=" in prompt
    assert run.adapters.candidates[0] != run.adapters.candidates[-1]
    with Image.open(io.BytesIO(run.adapters.candidates[-1])) as corrected:
        corrected_rgba = corrected.convert("RGBA")
    try:
        assert (237, 177, 44, 255) not in tuple(corrected_rgba.getdata())
    finally:
        corrected_rgba.close()
    assert run.adapters.verdicts[-1].extra_elements == ()
    assert run.adapters.verdicts[-1].unapproved_text == ()
    _assert_final_slots_have_foreground(run)


def test_mocked_mathpix_evidence_flows_into_single_gemini_inventory_then_exact_source_assets(
    monkeypatch,
):
    run = _run_public_fidelity_pipeline(monkeypatch, mathpix_success=True, max_images=4)
    assert run.provider is not None
    assert run.mathpix_session is not None

    assert [method for method, _url, _kwargs in run.mathpix_session.calls] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "DELETE",
    ]
    assert run.mathpix_session.entered == run.mathpix_session.exited == 2
    assert all(context.entered and context.exited for context in run.mathpix_session.all_contexts)
    inventory_calls = _schema_calls(run.provider, "InventoryResponse")
    audit_calls = _schema_calls(run.provider, "InventoryAuditResponse")
    note_calls = _schema_calls(run.provider, "ShortNotes")
    assert len(inventory_calls) == len(audit_calls) == len(note_calls) == 1
    assert inventory_calls[0].filenames == (
        "source-pages.pdf",
        "mathpix-source-evidence.json",
    )
    evidence_bytes = dict(inventory_calls[0].files)["mathpix-source-evidence.json"]
    evidence_payload = json.loads(evidence_bytes)
    assert tuple(page["local_page"] for page in evidence_payload["pages"]) == (1, 2, 3, 4)
    evidence_types = tuple(
        line["type"]
        for page in evidence_payload["pages"]
        for line in page["lines"]
    )
    assert {"diagram", "chemistry", "chart", "figure_label", "x_axis_label", "y_axis_label"} <= set(evidence_types)
    assert audit_calls[0].filenames == (
        "source-pages.pdf",
        "coverage-audit-overlay.pdf",
    )
    assert note_calls[0].filenames == ("lecture.pdf",)
    assert all(visual_id in note_calls[0].prompt for visual_id in SOURCE_FIDELITY_IDS)
    assert "source_png" not in note_calls[0].prompt
    serialized_notes = run.notes.model_dump_json()
    assert "fixture-private-id" not in serialized_notes
    assert "mathpix-source-evidence" not in serialized_notes
    assert "The source arrow fixes the intended direction." not in serialized_notes
    assert all(name not in run.notes.model_dump() for name in (
        "_visual_manifest",
        "_source_crops",
        "_visual_assets",
        "_visual_run_budget",
        "_visual_memory_budget",
    ))
    assert tuple(run.provider.close_counts.values()) == (1, 1, 1)
    assert run.budget.inventory_calls == {0: 1}
    assert run.budget.audit_calls == {0: 1}
    assert run.budget.note_calls == {0: 1}
    assert run.budget.mathpix_evidence_used == {0: 1}
    assert run.budget.mathpix_source_fallback == {}
    assert run.budget.coverage_probes_total == 100
    assert tuple(run.assets) == tuple(slot.visual_id for slot in run.ledger) == SOURCE_FIDELITY_IDS
    _assert_final_slots_have_foreground(run)


def test_mocked_mathpix_failure_uses_source_only_inventory_without_extra_model_call(
    monkeypatch,
):
    run = _run_public_fidelity_pipeline(monkeypatch, mathpix_success=False, max_images=4)
    assert run.provider is not None
    assert run.mathpix_session is not None

    assert [method for method, _url, _kwargs in run.mathpix_session.calls] == ["POST"]
    assert run.mathpix_session.entered == run.mathpix_session.exited == 1
    assert all(context.entered and context.exited for context in run.mathpix_session.all_contexts)
    inventory_calls = _schema_calls(run.provider, "InventoryResponse")
    audit_calls = _schema_calls(run.provider, "InventoryAuditResponse")
    note_calls = _schema_calls(run.provider, "ShortNotes")
    assert len(run.provider.calls) == 3
    assert len(inventory_calls) == len(audit_calls) == len(note_calls) == 1
    assert inventory_calls[0].filenames == ("source-pages.pdf",)
    assert run.budget.inventory_calls == {0: 1}
    assert run.budget.audit_calls == {0: 1}
    assert run.budget.note_calls == {0: 1}
    assert run.budget.mathpix_evidence_used == {}
    assert run.budget.mathpix_source_fallback == {0: 1}
    assert run.budget.mathpix_submit_calls == {0: 1}
    assert run.budget.mathpix_delete_calls == {}
    assert run.budget.coverage_probes_total == 100
    assert tuple(run.provider.close_counts.values()) == (1, 1, 1)
    assert tuple(run.assets) == tuple(slot.visual_id for slot in run.ledger) == SOURCE_FIDELITY_IDS
    assert MANUAL_PLACEHOLDER_HEADING not in _pdf_text(run.pdf_bytes)
    _assert_final_slots_have_foreground(run)
