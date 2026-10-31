"""Contract tests for request-scoped generation settings and AI budgets."""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import threading
import time
from collections import UserDict
from dataclasses import replace
from types import SimpleNamespace

import openai
import pytest
from fpdf import FPDF
from pydantic import BaseModel
from pypdf import PdfReader

from notesbot import config, gemini_client, svg_gen, visual_inventory
from notesbot.errors import UserError
from notesbot.prompts import (
    Branch,
    Curve,
    Graph,
    MindMap,
    Section,
    ShortNotes,
    Table,
    VISUAL_STYLE,
    Visual,
    WhereItem,
    build_prompt,
)
from notesbot.errors import VisualFidelityError
from notesbot.generation_policy import GenerationRun
from notesbot.pdf_utils import PdfChunk
from notesbot.visual_manifest import (
    AuditedRegion,
    CoverageProbe,
    InventoryAuditResponse,
    InventoryResponse,
    InventoryTopicAnchor,
    ProbeDisposition,
    SourceContext,
    SourceCrop,
    SourceLabel,
    SourceTopicAnchor,
    SourceVisual,
    RedrawVerdict,
    VisualAsset,
    VisualMemoryBudget,
    VisualManifest,
    VisualRegion,
    VisualRunBudget,
    manifest_ids,
)
from notesbot.visual_inventory import ModelFile


def _pdf_with_text(text: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 8, text)
    return bytes(pdf.output())


def _source_notes(*, quote: str = "", subject: str = "Physics") -> ShortNotes:
    return ShortNotes(
        lecture_no="Lecture 1",
        subject=subject,
        title="Resistance",
        quote=quote,
        sections=[Section(
            heading="Resistance",
            points=["Resistance opposes current."],
            icon="book",
        )],
        mindmap=MindMap(
            center="Resistance",
            branches=[Branch(label="Resistance", children=["Opposes current"])],
        ),
    )


def _sequence_chunk(
    index: int,
    *,
    first_global_page: int = 1,
    page_count: int = 1,
) -> PdfChunk:
    return PdfChunk(
        index=index,
        pdf_bytes=f"chunk-{index}".encode("ascii"),
        first_global_page=first_global_page,
        page_count=page_count,
        page_geometries=(),
    )


def _manifest(
    *kinds: str,
    hints: tuple[str, ...] = (),
    chunk_index: int = 0,
    first_id: int = 1,
) -> VisualManifest:
    visuals = []
    for offset, kind in enumerate(kinds):
        index = first_id + offset
        page = offset + 1
        visuals.append(SourceVisual(
            visual_id=f"sv-{index:04d}",
            chunk_index=chunk_index,
            local_page=page,
            global_page=page,
            box=(10.0, 20.0, 80.0, 90.0),
            kind=kind,
            topic_anchor=SourceTopicAnchor(
                topic_key=f"topic-{index}",
                local_page=page,
                box=(1.0, 2.0, 30.0, 12.0),
                exact_heading=f"Source topic {index}",
            ),
            section_hint=(hints[offset] if offset < len(hints) else ""),
            source_order=index,
        ))
    return VisualManifest(visuals=tuple(visuals))


def test_prompt_applies_request_language_and_subject_without_global_mutation():
    before = config.NOTES_LANGUAGE

    prompt = build_prompt("hinglish", "by A", subject_hint="Physics")

    assert "Hinglish" in prompt
    assert "Physics" in prompt
    assert config.NOTES_LANGUAGE == before


def test_faculty_rule_reads_the_whole_deck_not_only_the_front_page():
    first = build_prompt("english", "by A", first_chunk=True)
    later = build_prompt("english", "by A", first_chunk=False)

    # PW lecture PPTs often print the teacher's name on an intro slide, a
    # slide footer or beside the teacher's photo rather than the front page.
    assert "slide footers" in first
    assert '"Faculty: ..." -> the name' in first
    assert "Never guess or" in first

    # Later chunks copy a printed name too (the merge keeps the earliest
    # part's name), but still never invent one.
    assert "faculty: empty unless a slide in THIS part prints the teacher's name" in later
    assert "never guess or recall one" in later


def test_prompt_retires_legacy_core_diagram_generation_cap():
    prompt = " ".join(build_prompt("english", "by A").split())

    assert "At most 4 source-backed core diagrams per lecture" not in prompt
    assert "source-faithful `diagram` instruction" not in prompt


def test_section_accepts_ordered_visual_ids_and_legacy_visual_remains_readable():
    section = Section(
        heading="Cell",
        visual_ids=["sv-0002", "sv-0003"],
    )
    legacy = Section.model_validate({
        "heading": "Legacy cell",
        "diagram": "Draw the source cell.",
        "visual": {"kind": "core_diagram", "prompt": "Source cell"},
    })

    assert section.visual_ids == ["sv-0002", "sv-0003"]
    assert legacy.diagram == "Draw the source cell."
    assert legacy.visual == Visual(kind="core_diagram", prompt="Source cell")
    assert legacy.visual_ids == []


def test_sequence_provenance_is_in_schema_but_not_public_dump():
    section = Section(
        heading="Teacher topic",
        source_local_page=2,
        source_order=1,
    )

    schema = Section.model_json_schema()

    assert "source_local_page" in schema["properties"]
    assert "source_order" in schema["properties"]
    assert "source_local_page" not in section.model_dump()
    assert "source_order" not in section.model_dump()


def test_bounded_visual_plan_is_structured_and_keeps_legacy_prompt_empty():
    plan = Visual(
        kind="core_diagram",
        source_local_page=3,
        exact_components=["cell body", "axon"],
        exact_labels=["Cell body", "Axon"],
        composition_note="Axon extends right from the cell body",
    )

    assert plan.source_local_page == 3
    assert plan.exact_components == ["cell body", "axon"]
    assert plan.exact_labels == ["Cell body", "Axon"]
    assert plan.composition_note == "Axon extends right from the cell body"
    assert plan.prompt == ""


def test_prompt_separates_sequence_and_manifest_visual_contracts():
    sequence = build_prompt("english", "by A")
    manifest = build_prompt("english", "by A", visual_mode="manifest")

    assert "Zero visuals is correct" in sequence
    assert "Add no customary component" in sequence
    assert "same order the teacher taught" in sequence.lower()
    assert "textbook sequence" in sequence.lower()
    assert "teacher-emphasized" in sequence.lower()
    assert "general knowledge" in sequence.lower()
    assert "LEGACY MANIFEST COMPATIBILITY" not in sequence

    assert "LEGACY MANIFEST COMPATIBILITY" in manifest
    assert "immutable visual_id" in manifest
    assert "Zero visuals is correct" not in manifest


def test_note_prompt_contains_only_current_chunk_manifest_ids():
    current = _manifest("diagram", "structure")
    foreign = _manifest("diagram", chunk_index=1, first_id=9999)

    prompt = build_prompt(
        "english",
        "by A",
        visual_manifest_json=current.model_dump_json(),
    )

    assert "IMMUTABLE SOURCE-VISUAL MANIFEST" in prompt
    assert "sv-0001" in prompt
    assert "sv-0002" in prompt
    assert "sv-9999" not in prompt
    assert "reference each supplied ID at most once" in prompt
    assert "leave that ID unlinked" in prompt
    assert "source_pdf" not in prompt
    assert "source_png" not in prompt
    assert "crop_bytes" not in prompt

    mixed_chunks = VisualManifest(visuals=current.visuals + foreign.visuals)
    with pytest.raises(ValueError, match="one chunk"):
        build_prompt(
            "english",
            "by A",
            visual_manifest_json=mixed_chunks.model_dump_json(),
        )


@pytest.mark.parametrize(
    ("private_key", "private_value"),
    [
        ("source_pdf", "%PDF-private"),
        ("source_png", "data:image/png;base64,AA=="),
        ("crop_bytes", "private-crop-bytes"),
    ],
)
def test_note_prompt_rejects_private_source_payload_fields(
    private_key,
    private_value,
):
    valid_payload = _manifest("diagram").model_dump(mode="json")
    valid_payload["visuals"][0][private_key] = private_value
    private_payload = json.dumps(valid_payload)

    with pytest.raises(ValueError, match="private payload"):
        build_prompt(
            "english",
            "by A",
            visual_manifest_json=private_payload,
        )


def test_unlinked_manifest_visual_gets_one_source_order_headingless_continuation_placement():
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[Section(heading="Cells", points=["Source point."])],
    )
    manifest = _manifest("diagram", "structure")

    bound = gemini_client._bind_visual_ids(notes, manifest)

    assert len(bound.sections) == 2
    assert bound.sections[-1].heading == ""
    assert bound.sections[-1].visual_ids == ["sv-0001", "sv-0002"]
    assert bound.sections[-1].points == []
    assert bound.sections[-1].tricks == []
    assert bound.sections[-1].diagram == ""
    assert bound.sections[-1].visual is None
    assert bound.sections[-1].graph is None


def test_unlinked_visual_before_linked_visual_is_inserted_before_it():
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[
            Section(heading="First", points=["A."]),
            Section(heading="Second", visual_ids=["sv-0002"]),
        ],
    )
    manifest = _manifest("diagram", "structure", "diagram")

    bound = gemini_client._bind_visual_ids(notes, manifest)

    assert bound.sections[0].visual_ids == []
    assert bound.sections[1].visual_ids == ["sv-0001", "sv-0002", "sv-0003"]


def test_flattened_bound_visual_ids_equal_manifest_order_not_only_set_equality():
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[
            Section(heading="First", visual_ids=["sv-0001"]),
            Section(heading="Second", visual_ids=["sv-0003"]),
        ],
    )
    manifest = _manifest("diagram", "structure", "diagram")

    bound = gemini_client._bind_visual_ids(notes, manifest)
    flattened = tuple(
        visual_id
        for section in bound.sections
        for visual_id in section.visual_ids
    )

    assert flattened == manifest_ids(manifest)
    assert flattened == ("sv-0001", "sv-0002", "sv-0003")


@pytest.mark.parametrize(
    "legacy_fields",
    [
        {"diagram": "Draw a source diagram."},
        {"visual": Visual(kind="core_diagram", prompt="Generate an image.")},
        {"visual": Visual(prompt='<svg><image href="data:image/png;base64,AA=="/></svg>')},
    ],
)
def test_new_manifest_notes_reject_legacy_visual_svg_image_or_diagram_content(
    legacy_fields,
):
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[Section(heading="Cell", **legacy_fields)],
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._bind_visual_ids(notes, _manifest("diagram"))

    assert caught.value.code == "link_invalid"


def test_qc_and_design_cannot_change_visual_ids(monkeypatch):
    source = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[Section(heading="Cell", visual_ids=["sv-0001"])],
    )
    bound = gemini_client._bind_visual_ids(source, _manifest("diagram"))
    candidate = bound.model_copy(deep=True)
    candidate.sections[0].visual_ids = ["sv-9999"]
    candidate.sections[0].icon = "bolt"

    polished = gemini_client._source_locked_polish(
        bound, candidate, allow_icons=True
    )
    merged = gemini_client._merge([polished])
    gemini_client._tidy(merged)

    assert merged.sections[0].visual_ids == ["sv-0001"]


def test_manifest_note_sections_never_retain_legacy_source_pdf_bytes():
    source = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Cells",
        sections=[Section(heading="Cell", visual_ids=["sv-0001"])],
    )
    object.__setattr__(source.sections[0], "_source_pdf", b"private-source-pdf")

    bound = gemini_client._bind_visual_ids(source, _manifest("diagram"))
    polished = gemini_client._source_locked_polish(
        bound, source.model_copy(deep=True)
    )
    dumped = polished.model_dump_json()

    assert getattr(bound.sections[0], "_source_pdf", None) is None
    assert getattr(polished.sections[0], "_source_pdf", None) is None
    assert "private-source-pdf" not in dumped
    assert '"visual_ids":["sv-0001"]' in dumped


def test_drawn_source_graph_cannot_also_be_semantically_regenerated(monkeypatch):
    source_pdf = _pdf_with_text("The page contains a drawn velocity-time graph.")
    structured_payload = json.dumps({
        "lecture_no": "",
        "subject": "Physics",
        "title": "Motion",
        "sections": [{
            "heading": "Graph",
            "visual_ids": ["sv-0001"],
            "graph": {"title": "Velocity", "curves": []},
        }],
    })
    captured = {}
    response = SimpleNamespace(parsed=None, text=structured_payload, candidates=[])
    fake_client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs) or response
    ))
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
    monkeypatch.setattr(gemini_client, "_client", lambda: fake_client)
    manifest = _manifest("graph")

    generated = gemini_client._generate_one(
        source_pdf,
        visual_manifest_json=manifest.model_dump_json(),
    )
    prompt = " ".join(captured["contents"][0].split())

    assert "lecture draws or describes" not in prompt
    assert "leave section.graph empty in every section" in prompt
    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._bind_visual_ids(generated, manifest)

    assert caught.value.code == "link_invalid"


def test_chunk_with_drawn_manifest_graph_rejects_semantic_graph_in_any_section():
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Motion",
        sections=[
            Section(heading="Source graph", visual_ids=["sv-0001"]),
            Section(heading="Elsewhere", graph=Graph(title="Invented duplicate")),
        ],
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._bind_visual_ids(notes, _manifest("graph"))

    assert caught.value.code == "link_invalid"


def test_chunk_without_drawn_graph_can_keep_source_text_described_semantic_graph(
    monkeypatch,
):
    source_sentence = (
        "Graph title: Velocity-time. x-axis label: time (s). "
        "x-axis bounds: 0.0 s to 1.0 s. "
        "y-axis label: velocity (m/s). Curve label: motion. "
        "Exact points: (0.0, 0.0) and (1.0, 1.0)."
    )
    source_pdf = _pdf_with_text(source_sentence)
    structured_payload = json.dumps({
        "lecture_no": "",
        "subject": "Physics",
        "title": "Motion",
        "sections": [{
            "heading": "Text-described graph",
            "visual_ids": ["sv-0001"],
            "points": [source_sentence],
            "graph": {
                "title": "Velocity-time",
                "x_label": "time (s)",
                "y_label": "velocity (m/s)",
                "x_min": 0.0,
                "x_max": 1.0,
                "curves": [{
                    "label": "motion",
                    "points": [[0.0, 0.0], [1.0, 1.0]],
                }],
            },
        }],
    })
    captured = {}
    response = SimpleNamespace(parsed=None, text=structured_payload, candidates=[])
    fake_client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs) or response
    ))
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
    monkeypatch.setattr(gemini_client, "_client", lambda: fake_client)
    manifest = _manifest("diagram")

    generated = gemini_client._generate_one(
        source_pdf,
        visual_manifest_json=manifest.model_dump_json(),
    )
    prompt = " ".join(captured["contents"][0].split())
    bound = gemini_client._bind_visual_ids(generated, manifest)
    attached_source_text = " ".join(
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(io.BytesIO(
            captured["contents"][1].inline_data.data
        )).pages
    )
    bound_graph = bound.sections[0].graph

    assert captured["contents"][1].inline_data.data == source_pdf
    assert source_sentence not in prompt
    assert base64.b64encode(source_pdf).decode() not in prompt
    assert "sv-0001" in prompt
    assert "source text explicitly describes" in prompt
    assert bound_graph is not None
    assert bound_graph.x_label == "time (s)"
    assert (bound_graph.x_min, bound_graph.x_max) == (0.0, 1.0)
    assert "x-axis bounds: 0.0 s to 1.0 s" in attached_source_text
    assert bound_graph.curves[0].points == [[0.0, 0.0], [1.0, 1.0]]
    for source_stated_label in (
        bound_graph.title,
        bound_graph.x_label,
        bound_graph.y_label,
        bound_graph.curves[0].label,
    ):
        assert source_stated_label in attached_source_text
    for x_value, y_value in bound_graph.curves[0].points:
        assert f"({x_value:.1f}, {y_value:.1f})" in attached_source_text
    assert getattr(bound.sections[0], "_source_pdf", None) is None


def test_invented_section_hint_is_never_rendered_as_continuation_heading():
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Motion",
        sections=[Section(heading="Source section", points=["Source point."])],
    )
    hint = "MODEL INVENTED CONTINUATION TITLE"

    bound = gemini_client._bind_visual_ids(
        notes,
        _manifest("diagram", hints=(hint,)),
    )

    assert bound.sections[-1].heading == ""
    assert hint not in [section.heading for section in bound.sections]


def test_prompt_makes_pdf_the_only_factual_authority_for_revision_requests():
    prompt = " ".join(build_prompt(
        "english",
        "by A",
        edit_instruction="Add the Nernst equation and a new mnemonic.",
        subject_hint="Chemistry",
    ).split())

    assert "lecture PDF is the sole factual authority" in prompt
    assert "selection or presentation only" in prompt
    assert "cannot authorize new facts" in prompt
    assert "When uncertain, omit" in prompt
    assert "Preserve every unique theory point" in prompt
    assert "Extract a unique general fact taught during a question" in prompt
    assert "Never drop eligible theory to meet a page, section, bullet, or visual target" in prompt
    assert "leave `quote` empty" in prompt


def test_schema_and_image_instructions_forbid_unsourced_content():
    quote_rule = ShortNotes.model_fields["quote"].description
    visual_rule = Section.model_fields["visual"].description
    graph_rule = Section.model_fields["graph"].description

    assert "explicitly" in quote_rule and "PDF" in quote_rule and "empty" in quote_rule
    assert "explicitly" in visual_rule and "PDF" in visual_rule
    assert "core_diagram" in visual_rule and "exact_labels" in visual_rule
    assert "explicitly" in graph_rule and "faithfully" in graph_rule
    assert "Never invent" in graph_rule
    assert "only the supplied source components and labels" in VISUAL_STYLE
    assert "Never infer" in VISUAL_STYLE


def test_generation_schema_prunes_legacy_fields_but_model_keeps_them():
    from notesbot.gemini_client import _generation_schema_json

    schema_json = _generation_schema_json(ShortNotes)
    section_props = schema_json["$defs"]["Section"]["properties"]
    visual_props = schema_json["$defs"]["Visual"]["properties"]
    assert "diagram" not in section_props
    assert "visual_ids" not in section_props
    assert "prompt" not in visual_props
    # Pydantic model keeps them so previously stored notes still parse.
    assert "diagram" in Section.model_fields
    assert "visual_ids" in Section.model_fields
    assert "prompt" in Visual.model_fields


def test_schema_content_bounds_are_layout_preferences_not_drop_caps():
    table_headers = Table.model_fields["headers"].description
    table_rows = Table.model_fields["rows"].description
    graph_curves = Graph.model_fields["curves"].description
    points = Section.model_fields["points"].description
    tricks = Section.model_fields["tricks"].description
    branches = MindMap.model_fields["branches"].description.casefold()
    children = Branch.model_fields["children"].description
    curve_points = Curve.model_fields["points"].description
    flow = Section.model_fields["flow"].description

    assert "Every PDF-taught column" in table_headers
    assert "Every taught row and cell" in table_rows
    assert "Every PDF-taught curve" in graph_curves
    assert "every explicitly taught" in points
    assert "every" in tricks and "never invent or omit" in tricks
    assert "every unique source-taught topic" in branches
    assert "never omit" in branches
    assert "up to 8" not in branches
    assert "prefer 1-4" in children and "never add adjacent" in children
    assert "never invent" in curve_points
    assert "Every source-taught process" in flow

    # The per-field "never omit / overflow" boilerplate moved to one global
    # template rule; the guarantee must still ship in every prompt.
    prompt = " ".join(build_prompt("english", "by A").split())
    assert "layout preferences only" in prompt
    assert "never omit taught content" in prompt


def test_prompt_uses_overflow_and_pagination_instead_of_content_dropping_caps():
    prompt = " ".join(build_prompt("english", "by A").split()).lower()

    assert "complete source-taught theory" in prompt
    assert "retain only their unique general theory and final formulas" in prompt
    assert "additional sections or pages" in prompt
    assert "preserve every taught table column, row, cell, value, and condition" in prompt
    assert "include only axes, values, curves, expressions, points, and labels stated by that source" in prompt
    assert "include every source-taught trick" in prompt
    assert "sections: up to 8" not in prompt
    assert "tricks: 0-3" not in prompt
    assert "2-4 columns, cells 1-5 words" not in prompt
    assert "and 1-3 curves each" not in prompt
    assert "long derivations" not in prompt
    assert "everything important, nothing extra" not in prompt
    assert "terse, high-yield only" not in prompt
    assert "always leave `mindmap` null" in prompt
    assert "mind map (mandatory" not in prompt
    assert "complete detail belongs in the main theory" in prompt
    assert "prefer up to 8 source topics" not in prompt


def test_qc_and_design_prompts_are_content_locked():
    assert "may not change any content-bearing field" in gemini_client._QC_PROMPT
    assert "ONLY `icon`" in gemini_client._DESIGN_PROMPT
    assert "Never create key terms, comparisons, visuals, labels, or facts" in gemini_client._DESIGN_PROMPT


def test_generative_rewrite_passes_default_off_but_images_stay_enabled(monkeypatch):
    monkeypatch.delenv("NOTES_QC", raising=False)
    monkeypatch.delenv("NOTES_DESIGN", raising=False)
    monkeypatch.delenv("NOTES_IMAGES", raising=False)
    importlib.reload(config)
    try:
        assert config.NOTES_QC is False
        assert config.NOTES_DESIGN is False
        assert config.NOTES_IMAGES is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_sequence_provider_defaults_disable_mathpix_and_cap_images_at_two(monkeypatch):
    # Empty real-environment values prevent a developer .env from masking the
    # production defaults during module reload.
    monkeypatch.setenv("MATHPIX_ENABLED", "")
    monkeypatch.setenv("MAX_IMAGES", "")
    importlib.reload(config)
    try:
        assert config.MATHPIX_ENABLED is False
        assert config.MAX_IMAGES == 2
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_litellm_text_calls_request_configured_high_reasoning(monkeypatch):
    payload = '{"lecture_no":"","subject":"Physics","title":"Motion","sections":[]}'
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=payload), finish_reason="stop"
        )]
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: setattr(fake_client, "kwargs", kwargs) or response)
        )
    )
    monkeypatch.setattr(gemini_client, "_lite_client", lambda: fake_client)
    monkeypatch.setattr(config, "LITELLM_REASONING_EFFORT", "high", raising=False)

    gemini_client._lite_generate([{"type": "text", "text": "x"}], 100, 0.1)

    assert fake_client.kwargs["extra_body"] == {"reasoning_effort": "high"}


def test_direct_svg_call_attaches_exact_source_pdf_and_configured_reasoning(monkeypatch):
    captured = {}
    source_pdf = b"%PDF-1.7\nexact bounded SVG source chunk"
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content='<svg viewBox="0 0 400 300"></svg>'
            )
        )]
    )
    raw_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: captured.update(kwargs) or response
            )
        )
    )
    monkeypatch.setattr(gemini_client, "_local", threading.local())
    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: raw_client)
    monkeypatch.setattr(config, "LITELLM_REASONING_EFFORT", "medium")

    svg = svg_gen.ask("Daniell cell", source_pdf)

    assert svg is not None
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert captured["extra_body"] == {"reasoning_effort": "medium"}
    content = captured["messages"][0]["content"]
    assert content[0] == {
        "type": "file",
        "file": {
            "filename": "source-chunk.pdf",
            "file_data": (
                "data:application/pdf;base64,"
                + base64.b64encode(source_pdf).decode()
            ),
        },
    }
    assert content[1]["type"] == "text"
    svg_instruction = " ".join(content[1]["text"].split())
    assert "Daniell cell" in svg_instruction
    assert "attached teacher PDF as the sole factual and visual authority" in svg_instruction
    assert "Never infer, complete, correct, or add" in svg_instruction
    assert "At most 12 labels" not in svg_instruction


def test_svg_generator_rejects_missing_or_oversized_source_before_model_call(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_INLINE_MAX_BYTES", 4)
    monkeypatch.setattr(svg_gen, "enabled", lambda: True)
    called = []
    monkeypatch.setattr(svg_gen, "ask", lambda *_args: called.append(True))

    assert svg_gen.try_svg("Source figure", b"") is None
    assert svg_gen.try_svg("Source figure", b"12345") is None
    assert called == []


def test_litellm_client_preserves_model_listing_interface(monkeypatch):
    listing = SimpleNamespace(
        data=[SimpleNamespace(id="vertex_ai/gemini-3.6-flash")]
    )
    raw_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: None)
        ),
        models=SimpleNamespace(list=lambda: listing),
    )
    monkeypatch.setattr(gemini_client, "_local", threading.local())
    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: raw_client)

    client = gemini_client._lite_client()

    assert hasattr(client, "models")
    assert [model.id for model in client.models.list().data] == [
        "vertex_ai/gemini-3.6-flash"
    ]


def test_request_options_are_forwarded_once_to_sequence_bundle_without_global_mutation(
    monkeypatch,
):
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Sequence",
        sections=[Section(heading="Topic", source_local_page=1, source_order=0)],
    )
    bundle = gemini_client.SequenceNoteBundle(
        notes=notes,
        chunks=(_sequence_chunk(0),),
        run=GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0),
    )
    calls = []

    def generate_bundle(*args, **kwargs):
        calls.append(("bundle", args, kwargs))
        return bundle

    monkeypatch.setattr(gemini_client, "_generate_sequence_note_bundle", generate_bundle)
    monkeypatch.setattr(
        gemini_client.image_gen,
        "render_sequence_visuals",
        lambda supplied, supplied_run: calls.append(
            ("render", supplied, supplied_run)
        ),
    )
    before = config.NOTES_LANGUAGE

    gemini_client.generate_short_notes(b"pdf", language="hindi", subject_hint="Physics")

    assert calls[0] == (
        "bundle",
        (b"pdf", "", "hindi", "Physics"),
        {},
    )
    assert calls[1] == ("render", notes, bundle.run)
    assert config.NOTES_LANGUAGE == before


def test_generate_one_attaches_exact_source_chunk_to_every_section(monkeypatch):
    chunk = b"%PDF-1.7\nexact bounded lecture chunk"
    generated = ShortNotes(
        lecture_no="Lecture 1",
        subject="Physics",
        title="Resistance",
        sections=[
            Section(heading="Definition", points=["Resistance opposes current."]),
            Section(
                heading="Source Figure",
                visual=Visual(
                    kind="core_diagram",
                    prompt="Redraw the source circuit labelled R.",
                ),
            ),
        ],
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_lite_generate", lambda **_kwargs: generated)

    result = gemini_client._generate_one(chunk)

    assert all(getattr(section, "_source_pdf", None) is chunk for section in result.sections)
    assert "_source_pdf" not in result.model_dump()
    assert "_source_pdf" not in result.model_dump_json()


def test_source_chunk_provenance_survives_legacy_direct_merge_and_polish():
    chunk = b"%PDF-1.7\nsource provenance"
    source = _source_notes()
    object.__setattr__(source.sections[0], "_source_pdf", chunk)
    candidate = source.model_copy(deep=True)
    candidate.sections[0].icon = "bolt"

    polished = gemini_client._source_locked_polish(
        source,
        candidate,
        allow_icons=True,
    )
    result = gemini_client._merge([polished])

    assert getattr(result.sections[0], "_source_pdf", None) == chunk
    assert result.sections[0].icon == "bolt"
    assert "_source_pdf" not in result.model_dump_json()


def test_sequence_validator_orders_sections_by_teacher_source_position():
    chunk = _sequence_chunk(0, page_count=2)
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    notes = ShortNotes(
        lecture_no="",
        subject="Chemistry",
        title="Teacher order",
        sections=[
            Section(heading="Later", source_local_page=2, source_order=0),
            Section(heading="First", source_local_page=1, source_order=1),
            Section(heading="Opening", source_local_page=1, source_order=0),
        ],
    )

    ordered = gemini_client._validate_and_order_chunk_notes(notes, chunk, run)

    assert [section.heading for section in ordered.sections] == [
        "Opening",
        "First",
        "Later",
    ]
    assert all(
        getattr(section, "_source_chunk", None) is chunk
        for section in ordered.sections
    )
    assert "_source_chunk" not in ordered.model_dump_json()


@pytest.mark.parametrize(
    ("page", "order"),
    [(0, 0), (3, 0)],
)
def test_sequence_validator_repairs_unmappable_source_pages(page, order, capsys):
    chunk = _sequence_chunk(0, page_count=2)
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Invalid provenance",
        sections=[
            Section(
                heading="Topic",
                source_local_page=page,
                source_order=order,
            )
        ],
    )

    ordered = gemini_client._validate_and_order_chunk_notes(notes, chunk, run)

    # Real content with broken provenance inherits its neighbour's position
    # (page 1 / order 0 when it is first) instead of being lost or failing.
    assert len(ordered.sections) == 1
    assert ordered.sections[0].source_local_page == 1
    assert ordered.sections[0].source_order == 0
    assert "repaired_pages=1" in capsys.readouterr().out


def test_sequence_validator_repairs_invalid_order_instead_of_failing(capsys):
    chunk = _sequence_chunk(0, page_count=2)
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Repairable provenance",
        sections=[
            Section(heading="Topic", source_local_page=1, source_order=-1),
        ],
    )

    ordered = gemini_client._validate_and_order_chunk_notes(notes, chunk, run)

    assert len(ordered.sections) == 1
    assert ordered.sections[0].source_order == 10_000
    assert "repaired_orders=1" in capsys.readouterr().out


def test_sequence_validator_preserves_relative_order_for_duplicate_positions():
    chunk = _sequence_chunk(0)
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Stable order",
        sections=[
            Section(heading="A", source_local_page=1, source_order=0),
            Section(heading="B", source_local_page=1, source_order=0),
        ],
    )

    ordered = gemini_client._validate_and_order_chunk_notes(notes, chunk, run)

    assert [section.heading for section in ordered.sections] == ["A", "B"]


def test_active_sequence_boundary_dispatches_once_per_chunk_without_legacy_visual_flow(
    monkeypatch,
):
    chunks = (
        _sequence_chunk(0, first_global_page=1),
        _sequence_chunk(1, first_global_page=2),
    )
    calls = []
    rendered = []

    def retired(*_args, **_kwargs):
        raise AssertionError("retired visual boundary called")

    for name in (
        "collect_mathpix_evidence",
        "inventory_chunk",
        "finalize_manifest",
        "extract_source_crops",
        "_generate_manifest_note_bundle",
    ):
        monkeypatch.setattr(gemini_client, name, retired)

    monkeypatch.setattr(gemini_client, "split_pdf_chunks", lambda *_a, **_k: chunks)
    monkeypatch.setattr(
        gemini_client,
        "_source_verified_quote",
        lambda _pdf, quote, **_kwargs: quote,
    )
    monkeypatch.setattr(gemini_client, "_tidy", lambda *_a, **_k: None)
    monkeypatch.setattr(gemini_client, "_ensure_mindmap", lambda *_a, **_k: None)

    def generate(files, prompt, schema, max_tokens, temperature, run):
        calls.append((files, prompt, schema, max_tokens, temperature, run))
        chunk_index = int(files[0].data.decode("ascii").rsplit("-", 1)[1])
        return ShortNotes(
            lecture_no="Lecture 1" if chunk_index == 0 else "",
            subject="Physics",
            title="Sequence",
            sections=[
                Section(
                    heading=f"Chunk {chunk_index}",
                    source_local_page=1,
                    source_order=0,
                )
            ],
        )

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", generate)
    monkeypatch.setattr(
        gemini_client.image_gen,
        "render_sequence_visuals",
        lambda notes, run: rendered.append((notes, run)),
    )

    result = gemini_client.generate_short_notes(b"source")

    assert [section.heading for section in result.sections] == ["Chunk 0", "Chunk 1"]
    assert len(calls) == 2
    assert all(call[2] is ShortNotes for call in calls)
    assert all("Zero visuals is correct" in call[1] for call in calls)
    run = calls[0][5]
    assert all(call[5] is run for call in calls)
    assert run.snapshot(2)["notes_model_calls"] == 2
    assert rendered == [(result, run)]


def test_generation_ledger_is_numeric_and_redacts_source_content(
    monkeypatch,
    capsys,
) -> None:
    hostile = "HOSTILE-HEADING-LABEL-URL-KEY"
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title=hostile,
        sections=[
            Section(
                heading=hostile,
                source_local_page=1,
                source_order=0,
                visual=Visual(
                    kind="core_diagram",
                    source_local_page=1,
                    exact_components=[hostile],
                    exact_labels=[hostile],
                    composition_note=hostile,
                ),
            )
        ],
    )
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)
    run.take_note(0)
    run.take_note(1)
    for key in ("c0-p1-s0-i0", "c0-p2-s0-i1", "c1-p1-s0-i2"):
        run.mark_visual_eligible(key)
    assert run.try_take_image("c0-p1-s0-i0") is True
    assert run.try_take_image("c0-p2-s0-i1") is True
    run.mark_image_omitted("c1-p1-s0-i2")
    bundle = gemini_client.SequenceNoteBundle(
        notes=notes,
        chunks=(_sequence_chunk(0), _sequence_chunk(1)),
        run=run,
    )
    monkeypatch.setattr(
        gemini_client,
        "_generate_sequence_note_bundle",
        lambda *_a, **_k: bundle,
    )
    monkeypatch.setattr(
        gemini_client.image_gen,
        "render_sequence_visuals",
        lambda *_a, **_k: None,
    )

    result = gemini_client.generate_short_notes(b"pdf")

    assert result is notes
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("[notesbot.generation] ")
    ]
    assert len(lines) == 1
    event = json.loads(lines[0].split(" ", 1)[1])
    assert event == {
        "success": 1,
        "chunk_count": 2,
        "notes_model_calls": 2,
        "eligible_visuals": 3,
        "image_model_calls": 2,
        "images_omitted": 1,
        "mathpix_calls": 0,
        "inventory_calls": 0,
        "audit_calls": 0,
        "verifier_calls": 0,
    }
    assert all(type(value) is int for value in event.values())
    assert hostile not in lines[0]


def test_manifest_bundle_one_failed_chunk_aborts_instead_of_returning_partial_notes(
    monkeypatch,
):
    source = _source_notes()
    _install_task8_pipeline(monkeypatch)

    def generate(_chunk, *_args, chunk_index=None, **_kwargs):
        if chunk_index == 1:
            raise VisualFidelityError("link_invalid")
        return source

    monkeypatch.setattr(gemini_client, "_generate_one", generate)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_manifest_note_bundle(b"pdf")

    assert caught.value.code == "link_invalid"


def test_optional_polish_can_only_copy_a_validated_decorative_icon():
    source = _source_notes()
    invented = source.model_copy(deep=True)
    invented.title = "Nernst Equation"
    invented.quote = "A newly composed motivational quote."
    invented.sections[0].points.append("The Nernst equation predicts cell potential.")
    invented.sections[0].tricks.append("Mnemonic: RED CAT gains electrons.")
    invented.sections[0].visual = Visual(
        kind="core_diagram",
        prompt="Add a salt bridge and label both half-cells.",
    )
    invented.sections[0].icon = "bolt"

    result = gemini_client._source_locked_polish(
        source,
        invented,
        allow_icons=True,
    )

    expected = source.model_copy(deep=True)
    expected.sections[0].icon = "bolt"
    assert result.model_dump() == expected.model_dump()


def test_legacy_direct_merge_preserves_every_teacher_field_and_source_visual():
    source = ShortNotes(
        lecture_no="Lecture 1",
        subject="Physics",
        title="Ohm's Law",
        sections=[
            Section(
                heading="Equation",
                points=["$V=IR$", "Resistance opposes current."],
                where=[WhereItem(symbol="$R$", meaning="Resistance")],
            ),
            Section(
                heading="Teacher Diagram",
                key_terms=["R : resistance taught here"],
                visual=Visual(
                    kind="core_diagram",
                    prompt="Faithfully redraw the source circuit labelled R only.",
                ),
            ),
        ],
        mindmap=MindMap(
            center="Ohm's Law",
            branches=[
                Branch(label="Equation", children=["$V=IR$"]),
                Branch(label="Teacher Diagram", children=["Circuit labelled R"]),
            ],
        ),
    )
    expected = source.model_copy(deep=True)

    result = gemini_client._merge([source.model_copy(deep=True)])

    assert result.model_dump() == expected.model_dump()


def test_quote_is_kept_only_when_its_full_text_is_extractable_from_pdf():
    quote = "Revise the mechanism, not the mnemonic."
    result = gemini_client._source_verified_quote(
        _pdf_with_text("The teacher says: Revise the mechanism, not the mnemonic."),
        quote,
    )

    assert result == quote


def test_quote_is_cleared_when_pdf_does_not_contain_it():
    result = gemini_client._source_verified_quote(
        _pdf_with_text("This lecture teaches that resistance opposes current."),
        "Energy rewards the curious mind.",
    )

    assert result == ""


def test_quote_is_not_verified_inside_a_larger_source_word():
    result = gemini_client._source_verified_quote(
        _pdf_with_text("A particle moves through the apparatus."),
        "art",
    )

    assert result == ""


def test_quote_is_verified_as_a_complete_source_token():
    result = gemini_client._source_verified_quote(
        _pdf_with_text("The teacher calls the labelled sketch art."),
        "art",
    )

    assert result == "art"


def test_missing_subject_stays_empty_instead_of_using_an_unsourced_default():
    source = _source_notes(subject="")

    gemini_client._tidy(source)

    assert source.subject == ""


def test_missing_title_and_source_content_stay_empty_without_generic_fallback():
    source = ShortNotes(
        lecture_no="",
        subject="",
        title="",
        sections=[],
        mindmap=None,
    )

    gemini_client._tidy(source)
    gemini_client._ensure_mindmap(source)

    assert source.title == ""
    assert source.mindmap is None


def test_mindmap_facts_recover_hollow_theory_and_collapse_semantic_topic_duplicates():
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[
            Section(
                heading="Variable River Velocity Drift",
                where=[
                    WhereItem(symbol="$V_s$", meaning="swimmer speed"),
                    WhereItem(symbol="$V_r$", meaning="river velocity"),
                ],
            ),
            Section(
                heading="Vector Analysis of Momentum",
                where=[
                    WhereItem(symbol="$p$", meaning="linear momentum"),
                    WhereItem(symbol="$\\Delta p$", meaning="momentum change"),
                ],
            ),
            Section(heading="Newton First Law and Inertia"),
            Section(
                heading="Newton Second Law of Motion",
                where=[
                    WhereItem(symbol="$F_{net}$", meaning="net force"),
                    WhereItem(symbol="$dm/dt$", meaning="mass change rate"),
                ],
            ),
            Section(heading="Graph Analysis and Impulse"),
        ],
        mindmap=MindMap(
            center="Laws of Motion",
            branches=[
                Branch(
                    label="Momentum Vectors",
                    children=["$p=mv$", "$\\Delta p=\\vec p_f-\\vec p_i$"],
                ),
                Branch(
                    label="Vector Analysis of Momentum",
                    children=["Linear momentum is a vector quantity"],
                ),
                Branch(
                    label="Graph Analysis",
                    children=["Slope of $P-t$ gives force", "Area gives impulse"],
                ),
                Branch(
                    label="First Law & Inertia",
                    children=["$F_{ext,net}=0$ preserves state", "Inertia is proportional to mass"],
                ),
                Branch(
                    label="Second Law",
                    children=["$F_{net}=\\frac{dP}{dt}$", "For constant mass, $F=ma$"],
                ),
                Branch(
                    label="Variable River Velocity Drift",
                    children=["$S_x=\\int V_r\\,dt$"],
                ),
                Branch(
                    label="Newton First Law and Inertia",
                    children=["Newton First Law and Inertia"],
                ),
                Branch(
                    label="Graph Analysis and Impulse",
                    children=["Graph Analysis and Impulse"],
                ),
            ],
        ),
    )

    gemini_client._ensure_mindmap(notes, strict_theory=True)

    assert all(section.points for section in notes.sections)
    assert "$S_x=\\int V_r\\,dt$" in notes.sections[0].points
    assert "$p=mv$" in notes.sections[1].points
    assert "$F_{ext,net}=0$ preserves state" in notes.sections[2].points
    assert "$F_{net}=\\frac{dP}{dt}$" in notes.sections[3].points
    assert "Area gives impulse" in notes.sections[4].points
    assert [branch.label for branch in notes.mindmap.branches] == [
        section.heading for section in notes.sections
    ]
    assert all(
        child.casefold() != branch.label.casefold()
        for branch in notes.mindmap.branches
        for child in branch.children
    )


def test_mindmap_derives_multiple_revision_hooks_from_complete_main_theory():
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[
            Section(
                heading="Newton Second Law",
                points=[
                    "Net force equals momentum change rate.",
                    "$F_{net}=\\frac{dP}{dt}$",
                    "For constant mass, $F=ma$.",
                ],
                tricks=["Use variable-mass form when $dm/dt\\ne0$."],
            )
        ],
        mindmap=None,
    )

    gemini_client._ensure_mindmap(notes, strict_theory=True)

    assert notes.mindmap is not None
    assert [branch.label for branch in notes.mindmap.branches] == [
        "Newton Second Law"
    ]
    assert notes.mindmap.branches[0].children == [
        "Net force equals momentum change rate.",
        "$F_{net}=\\frac{dP}{dt}$",
        "For constant mass, $F=ma$.",
        "Use variable-mass form when $dm/dt\\ne0$.",
    ]


def test_unmatched_optional_mindmap_branch_does_not_reject_complete_theory():
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[
            Section(
                heading="Newton Second Law",
                points=[
                    "Net force equals momentum change rate.",
                    "$F_{net}=\\frac{dP}{dt}$",
                ],
            )
        ],
        mindmap=MindMap(
            center="Laws of Motion",
            branches=[
                Branch(
                    label="Core Formula",
                    children=["$F_{net}=\\frac{dP}{dt}$"],
                )
            ],
        ),
    )

    gemini_client._ensure_mindmap(notes, strict_theory=True)

    assert notes.mindmap is not None
    assert [branch.label for branch in notes.mindmap.branches] == [
        "Newton Second Law"
    ]
    assert "$F_{net}=\\frac{dP}{dt}$" in notes.mindmap.branches[0].children


def test_unrecoverable_heading_only_theory_is_rejected_before_rendering():
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[Section(heading="Newton First Law")],
        mindmap=MindMap(
            center="Laws of Motion",
            branches=[
                Branch(label="Newton First Law", children=["Newton First Law"])
            ],
        ),
    )

    with pytest.raises(UserError, match="complete theory"):
        gemini_client._ensure_mindmap(notes, strict_theory=True)


def test_blank_section_heading_is_rejected_before_rendering():
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[Section(heading="   ", points=["Inertia preserves state."])],
        mindmap=None,
    )

    with pytest.raises(UserError, match="complete theory"):
        gemini_client._ensure_mindmap(notes, strict_theory=True)


def test_where_legend_without_its_formula_is_cleared_before_rendering(capsys):
    notes = ShortNotes(
        lecture_no="Lecture 2",
        subject="Physics",
        title="Laws of Motion",
        sections=[
            Section(
                heading="Momentum",
                points=["Momentum changes when force acts."],
                where=[
                    WhereItem(symbol="$p$", meaning="momentum"),
                    WhereItem(symbol="$m$", meaning="mass"),
                ],
            )
        ],
        mindmap=None,
    )

    gemini_client._ensure_mindmap(notes, strict_theory=True)

    # The orphaned "where" legend is auxiliary: it is cleared with fixed
    # telemetry instead of rejecting the whole generation.
    assert len(notes.sections) == 1
    assert notes.sections[0].where in (None, [])
    assert "cleared_where=1" in capsys.readouterr().out


def test_config_clamps_chunk_and_image_limits_to_web_budget(monkeypatch):
    monkeypatch.setenv("MAX_CHUNKS", "99")
    monkeypatch.setenv("MAX_IMAGES", "99")
    importlib.reload(config)
    try:
        assert config.MAX_CHUNKS == 16
        assert config.MAX_IMAGES == 2
    finally:
        monkeypatch.undo()
        importlib.reload(config)


@pytest.mark.parametrize("raw_limit", ("0", "-7"))
def test_config_clamps_non_positive_chunk_limit_to_one(monkeypatch, raw_limit):
    monkeypatch.setenv("MAX_CHUNKS", raw_limit)
    importlib.reload(config)
    try:
        assert config.MAX_CHUNKS == 1
    finally:
        monkeypatch.undo()
        importlib.reload(config)


class _GatewaySchema(BaseModel):
    value: str


def _visual_budget(*, remaining: float = 30.0) -> VisualRunBudget:
    now = 10.0
    return VisualRunBudget(deadline_at=now + remaining, clock=lambda: now)


def _memory_budget() -> VisualMemoryBudget:
    return VisualMemoryBudget(
        max_total_bytes=config.MAX_VISUAL_RESIDENT_BYTES,
        max_source_bytes=config.MAX_VISUAL_SOURCE_TOTAL_BYTES,
        max_context_bytes=config.MAX_VISUAL_CONTEXT_TOTAL_BYTES,
        max_asset_bytes=config.MAX_VISUAL_ASSET_TOTAL_BYTES,
        max_page_cache_bytes=config.MAX_VISUAL_PAGE_CACHE_BYTES,
    )


_VISUAL_RUNTIME_ATTRS = (
    "_visual_manifest",
    "_source_crops",
    "_visual_assets",
    "_visual_run_budget",
    "_visual_memory_budget",
)


def _activation_bundle(
    *,
    notes: ShortNotes | None = None,
    title: str = "Activation notes",
    manifest: VisualManifest | None = None,
) -> tuple[gemini_client.ManifestNoteBundle, dict[str, VisualAsset]]:
    """Build a production-shaped, memory-owned public activation bundle."""
    manifest = manifest or _manifest("diagram", "structure")
    if notes is None:
        notes = ShortNotes(
            lecture_no="",
            subject="Physics",
            title=title,
            sections=[Section(
                heading="Source visuals",
                visual_ids=list(manifest_ids(manifest)),
            )],
        )
    budget = _visual_budget()
    memory = _memory_budget()
    crops: list[SourceCrop] = []
    assets: dict[str, VisualAsset] = {}
    for ordinal, visual in enumerate(manifest.visuals):
        source_png = bytes(bytearray(f"source-{visual.visual_id}".encode()))
        fallback_png = bytes(bytearray(f"fallback-{visual.visual_id}".encode()))
        primary_png = (
            bytes(bytearray(f"primary-{visual.visual_id}".encode()))
            if ordinal == 0
            else fallback_png
        )
        memory.reserve_buffer("source", source_png)
        memory.reserve_buffer("asset", fallback_png)
        if primary_png is not fallback_png:
            memory.reserve_buffer("asset", primary_png)
        crops.append(SourceCrop(
            visual=visual,
            source_png=source_png,
            width_px=120 + ordinal,
            height_px=80 + ordinal,
        ))
        assets[visual.visual_id] = VisualAsset(
            visual_id=visual.visual_id,
            state=("verified_redraw" if ordinal == 0 else "sketch_source_fallback"),
            primary_png=primary_png,
            fallback_png=fallback_png,
            width_px=120 + ordinal,
            height_px=80 + ordinal,
        )
    return (
        gemini_client.ManifestNoteBundle(
            notes=notes,
            chunk_count=2,
            manifest=manifest,
            crops=tuple(crops),
            budget=budget,
            memory=memory,
        ),
        assets,
    )


def _install_public_activation(
    monkeypatch,
    bundle: gemini_client.ManifestNoteBundle,
    assets: dict[str, VisualAsset],
    *,
    calls: list | None = None,
) -> list:
    calls = calls if calls is not None else []

    def generate_bundle(*args, **kwargs):
        calls.append(("bundle", args, kwargs))
        return bundle

    def render(*args):
        calls.append(("render", args))
        return assets

    monkeypatch.setattr(gemini_client, "_generate_manifest_note_bundle", generate_bundle)
    monkeypatch.setattr(gemini_client.image_gen, "render_visuals", render)
    monkeypatch.setattr(
        gemini_client,
        "split_pdf",
        lambda *_a, **_k: pytest.fail("legacy split_pdf path was called"),
    )
    return calls


def _retired_public_generation_activates_manifest_crops_assets_and_shared_budgets_atomically(
    monkeypatch,
):
    bundle, assets = _activation_bundle()
    calls = _install_public_activation(monkeypatch, bundle, assets)
    for legacy_name in (
        "_generate_one",
        "_qc_pass",
        "_design_pass",
        "_source_verified_quote",
        "_tidy",
        "_ensure_mindmap",
    ):
        monkeypatch.setattr(
            gemini_client,
            legacy_name,
            lambda *_a, _name=legacy_name, **_k: pytest.fail(
                f"legacy {_name} path was called"
            ),
        )

    result = gemini_client.generate_short_notes(
        b"exact-pdf",
        "tighten wording",
        language="hindi",
        subject_hint="Physics",
    )

    assert result is bundle.notes
    assert calls[0] == (
        "bundle",
        (b"exact-pdf", "tighten wording", "hindi", "Physics"),
        {},
    )
    assert calls[1][0] == "render"
    render_args = calls[1][1]
    assert len(render_args) == 5
    assert render_args[0] is bundle.notes
    assert render_args[1] is bundle.manifest
    assert tuple(render_args[2]) == manifest_ids(bundle.manifest)
    assert tuple(render_args[2].values()) == bundle.crops
    assert render_args[3] is bundle.budget
    assert render_args[4] is bundle.memory
    assert result._visual_manifest is bundle.manifest
    assert tuple(result._source_crops) == manifest_ids(bundle.manifest)
    assert tuple(result._source_crops.values()) == bundle.crops
    assert tuple(result._visual_assets) == manifest_ids(bundle.manifest)
    assert tuple(result._visual_assets.values()) == tuple(assets.values())
    assert result._visual_run_budget is bundle.budget
    assert result._visual_memory_budget is bundle.memory
    assets.clear()
    assert tuple(result._visual_assets) == manifest_ids(bundle.manifest)
    dumped = result.model_dump()
    dumped_json = result.model_dump_json()
    assert all(name not in dumped and name not in dumped_json for name in _VISUAL_RUNTIME_ATTRS)
    assert all(
        "_source_pdf" not in section.__dict__
        and "_image" not in section.__dict__
        and "_svg" not in section.__dict__
        for section in result.sections
    )
    assert bundle.memory.resident_bytes() > 0
    bundle.memory.close()


def _retired_empty_manifest_still_activates_one_manifest_job_without_legacy_fallback(
    monkeypatch,
):
    manifest = VisualManifest()
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="No source visuals",
        sections=[Section(heading="Text only", points=["Source point."])],
    )
    bundle, assets = _activation_bundle(notes=notes, manifest=manifest)
    calls = _install_public_activation(monkeypatch, bundle, assets)
    monkeypatch.setattr(
        gemini_client,
        "_generate_one",
        lambda *_a, **_k: pytest.fail("legacy note path was called"),
    )

    result = gemini_client.generate_short_notes(b"pdf")

    assert result is notes
    assert [call[0] for call in calls] == ["bundle", "render"]
    assert len(calls[1][1]) == 5
    assert calls[1][1][2] == {}
    assert result._visual_manifest is manifest
    assert result._source_crops == {}
    assert result._visual_assets == {}
    assert result._visual_run_budget is bundle.budget
    assert result._visual_memory_budget is bundle.memory
    bundle.memory.close()


@pytest.mark.parametrize(
    "failure_case",
    [
        "crop_container_list",
        "crop_duck_type",
        "crop_missing",
        "crop_extra",
        "crop_swapped",
        "crop_embedded_id",
        "asset_non_dict_mapping",
        "asset_missing",
        "asset_extra",
        "asset_unknown",
        "asset_swapped",
        "asset_embedded_id",
        "invalid_state",
        "primary_not_bytes",
        "fallback_not_bytes",
        "width_bool",
        "width_zero",
        "width_negative",
        "width_float",
        "width_string",
        "height_bool",
        "height_zero",
        "height_negative",
        "height_float",
        "height_string",
        "non_asset",
        "none",
        "renderer_exception",
        "event_exception",
        "setter_1",
        "setter_2",
        "setter_3",
        "setter_4",
        "setter_5",
    ],
)
def _retired_failed_asset_activation_exposes_no_partial_public_manifest_state(
    monkeypatch,
    capsys,
    failure_case,
):
    bundle, valid_assets = _activation_bundle()
    ids = manifest_ids(bundle.manifest)
    if failure_case == "crop_container_list":
        bundle = replace(bundle, crops=list(bundle.crops))
    elif failure_case == "crop_duck_type":
        duck_crop = SimpleNamespace(
            visual=bundle.crops[0].visual,
            source_png=bundle.crops[0].source_png,
            width_px=bundle.crops[0].width_px,
            height_px=bundle.crops[0].height_px,
            context_crops=bundle.crops[0].context_crops,
        )
        bundle = replace(bundle, crops=(duck_crop, bundle.crops[1]))
    elif failure_case == "crop_missing":
        bundle = replace(bundle, crops=bundle.crops[:-1])
    elif failure_case == "crop_extra":
        bundle = replace(bundle, crops=bundle.crops + (bundle.crops[-1],))
    elif failure_case == "crop_swapped":
        bundle = replace(bundle, crops=tuple(reversed(bundle.crops)))
    elif failure_case == "crop_embedded_id":
        bundle = replace(
            bundle,
            crops=(replace(bundle.crops[0], visual=bundle.manifest.visuals[1]), bundle.crops[1]),
        )

    def render(*_args):
        if failure_case == "renderer_exception":
            raise RuntimeError("PRIVATE RENDERER FAILURE")
        if failure_case == "none":
            return None
        assets = dict(valid_assets)
        if failure_case == "asset_non_dict_mapping":
            return UserDict(assets)
        if failure_case == "asset_missing":
            assets.pop(ids[1])
            return assets
        if failure_case == "asset_extra":
            assets["sv-9999"] = replace(
                assets[ids[1]],
                visual_id="sv-9999",
            )
            return assets
        if failure_case == "asset_unknown":
            assets.pop(ids[1])
            assets["sv-9999"] = replace(
                valid_assets[ids[1]],
                visual_id="sv-9999",
            )
            return assets
        if failure_case == "asset_swapped":
            return {ids[1]: assets[ids[1]], ids[0]: assets[ids[0]]}
        if failure_case == "asset_embedded_id":
            assets[ids[0]] = replace(assets[ids[0]], visual_id=ids[1])
        elif failure_case == "invalid_state":
            assets[ids[0]] = replace(assets[ids[0]], state="pending")
        elif failure_case == "primary_not_bytes":
            assets[ids[0]] = replace(assets[ids[0]], primary_png="PRIVATE")
        elif failure_case == "fallback_not_bytes":
            assets[ids[0]] = replace(assets[ids[0]], fallback_png=bytearray(b"png"))
        elif failure_case == "width_bool":
            assets[ids[0]] = replace(assets[ids[0]], width_px=True)
        elif failure_case == "width_zero":
            assets[ids[0]] = replace(assets[ids[0]], width_px=0)
        elif failure_case == "width_negative":
            assets[ids[0]] = replace(assets[ids[0]], width_px=-1)
        elif failure_case == "width_float":
            assets[ids[0]] = replace(assets[ids[0]], width_px=120.5)
        elif failure_case == "width_string":
            assets[ids[0]] = replace(assets[ids[0]], width_px="120")
        elif failure_case == "height_bool":
            assets[ids[0]] = replace(assets[ids[0]], height_px=True)
        elif failure_case == "height_zero":
            assets[ids[0]] = replace(assets[ids[0]], height_px=0)
        elif failure_case == "height_negative":
            assets[ids[0]] = replace(assets[ids[0]], height_px=-1)
        elif failure_case == "height_float":
            assets[ids[0]] = replace(assets[ids[0]], height_px=80.5)
        elif failure_case == "height_string":
            assets[ids[0]] = replace(assets[ids[0]], height_px="80")
        elif failure_case == "non_asset":
            assets[ids[0]] = {"visual_id": ids[0]}
        return assets

    monkeypatch.setattr(
        gemini_client,
        "_generate_manifest_note_bundle",
        lambda *_a, **_k: bundle,
    )
    monkeypatch.setattr(gemini_client.image_gen, "render_visuals", render)
    monkeypatch.setattr(
        gemini_client,
        "split_pdf",
        lambda *_a, **_k: pytest.fail("legacy split_pdf path was called"),
    )
    if failure_case.startswith("setter_"):
        target = _VISUAL_RUNTIME_ATTRS[int(failure_case.rsplit("_", 1)[1]) - 1]

        def mutating_setter(obj, name, value):
            object.__setattr__(obj, name, value)
            if name == target:
                raise RuntimeError("PRIVATE SETTER FAILURE")

        monkeypatch.setattr(
            gemini_client,
            "_set_visual_runtime_attr",
            mutating_setter,
            raising=False,
        )
    if failure_case == "event_exception":
        monkeypatch.setattr(
            gemini_client,
            "_emit_visual_activation_event",
            lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("PRIVATE EVENT FAILURE")
            ),
            raising=False,
        )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client.generate_short_notes(b"pdf")

    assert caught.value.code in {
        "link_invalid",
        "crop_failed",
        "render_failed",
        "memory_limit_exceeded",
    }
    assert "PRIVATE" not in str(caught.value)
    assert all(name not in bundle.notes.__dict__ for name in _VISUAL_RUNTIME_ATTRS)
    assert bundle.memory.resident_bytes() == 0
    assert bundle.memory.source_bytes == 0
    assert bundle.memory.context_bytes == 0
    assert bundle.memory.asset_bytes == 0
    with pytest.raises(VisualFidelityError) as closed:
        bundle.memory.reserve_buffer("source", b"after-close")
    assert closed.value.code == "memory_limit_exceeded"
    assert "[notesbot.visual]" not in capsys.readouterr().out


def _retired_failed_activation_restores_all_five_preexisting_private_values(monkeypatch):
    bundle, assets = _activation_bundle()
    prior = {name: object() for name in _VISUAL_RUNTIME_ATTRS}
    for name, value in prior.items():
        object.__setattr__(bundle.notes, name, value)

    def setter(obj, name, value):
        object.__setattr__(obj, name, value)
        if name == "_visual_assets":
            raise RuntimeError("PRIVATE MUTATE THEN FAIL")

    monkeypatch.setattr(gemini_client, "_generate_manifest_note_bundle", lambda *_a, **_k: bundle)
    monkeypatch.setattr(gemini_client.image_gen, "render_visuals", lambda *_a: assets)
    monkeypatch.setattr(gemini_client, "_set_visual_runtime_attr", setter, raising=False)
    monkeypatch.setattr(gemini_client, "split_pdf", lambda *_a: pytest.fail("legacy path"))

    with pytest.raises(VisualFidelityError):
        gemini_client.generate_short_notes(b"pdf")

    assert all(bundle.notes.__dict__[name] is value for name, value in prior.items())
    assert bundle.memory.resident_bytes() == 0


def _retired_generation_emits_one_redacted_aggregate_visual_event_after_activation(monkeypatch):
    hostile = "HOSTILE-LABEL-CONTEXT-PROVIDER"
    bundle, assets = _activation_bundle(title=hostile)
    budget = bundle.budget
    budget.take_mathpix_submit(0)
    budget.take_mathpix_poll(0, 2)
    budget.take_mathpix_poll(0, 2)
    budget.take_mathpix_lines(0)
    budget.take_mathpix_mmd(0)
    budget.take_mathpix_delete(0)
    budget.take_inventory_with_mathpix_outcome(0, used=True)
    budget.take_inventory_with_mathpix_outcome(1, used=False)
    budget.take_audit(0)
    budget.take_audit(1)
    budget.take_note(0)
    budget.take_note(1)
    budget.take_redraw("HOSTILE-VISUAL-ID")
    budget.take_verifier("HOSTILE-VISUAL-ID")
    budget.reserve_coverage_probes(3, 10)
    lines = []

    def capture_print(line):
        assert all(name in bundle.notes.__dict__ for name in _VISUAL_RUNTIME_ATTRS)
        lines.append(line)

    _install_public_activation(monkeypatch, bundle, assets)
    monkeypatch.setattr("builtins.print", capture_print)

    result = gemini_client.generate_short_notes(b"pdf")

    assert result is bundle.notes
    assert len(lines) == 1
    prefix = "[notesbot.visual] "
    assert lines[0].startswith(prefix)
    payload = json.loads(lines[0][len(prefix):])
    assert payload == {
        "audit": 2,
        "chunks": 2,
        "coverage_probes": 3,
        "inventory": 2,
        "manifest": 2,
        "mathpix_delete": 1,
        "mathpix_evidence_used_chunks": 1,
        "mathpix_lines": 1,
        "mathpix_mmd": 1,
        "mathpix_poll": 2,
        "mathpix_source_fallback_chunks": 1,
        "mathpix_submit": 1,
        "note": 2,
        "redraw": 1,
        "verifier": 1,
    }
    assert all(type(value) is int for value in payload.values())
    assert hostile not in lines[0]
    assert "sv-0001" not in lines[0]
    assert "HOSTILE-VISUAL-ID" not in lines[0]
    bundle.memory.close()


def _retired_successful_public_activation_leaves_memory_live_for_pdf_owner(monkeypatch):
    bundle, assets = _activation_bundle()
    resident_before = bundle.memory.resident_bytes()
    _install_public_activation(monkeypatch, bundle, assets)

    result = gemini_client.generate_short_notes(b"pdf")

    assert result._visual_memory_budget is bundle.memory
    assert resident_before > 0
    assert bundle.memory.resident_bytes() == resident_before
    extra = bytes(bytearray(b"pdf-still-owns-live-memory"))
    assert bundle.memory.reserve_buffer("asset", extra) is not None
    bundle.memory.close()


def _task8_manifest() -> VisualManifest:
    return VisualManifest(visuals=(
        SourceVisual(
            visual_id="sv-0001",
            chunk_index=0,
            local_page=1,
            global_page=1,
            box=(10.0, 20.0, 80.0, 90.0),
            kind="diagram",
            topic_anchor=SourceTopicAnchor(
                topic_key="topic-1",
                local_page=1,
                box=(1.0, 2.0, 30.0, 12.0),
                exact_heading="Source heading one",
            ),
            exact_labels=(SourceLabel(
                text="L1",
                box=(12.0, 22.0, 18.0, 28.0),
                anchor=(20.0, 30.0),
            ),),
            context_regions=(SourceContext(
                topic_key="topic-1",
                local_page=1,
                box=(2.0, 3.0, 40.0, 15.0),
                role="explanation",
                exact_text="Bounded source context one",
            ),),
            section_hint="PRIVATE MODEL HINT ONE",
            source_order=0,
        ),
        SourceVisual(
            visual_id="sv-0002",
            chunk_index=1,
            local_page=1,
            global_page=2,
            box=(15.0, 25.0, 85.0, 95.0),
            kind="structure",
            topic_anchor=SourceTopicAnchor(
                topic_key="topic-2",
                local_page=1,
                box=(2.0, 3.0, 31.0, 13.0),
                exact_heading="Source heading two",
            ),
            exact_labels=(),
            context_regions=(),
            section_hint="PRIVATE MODEL HINT TWO",
            source_order=1,
        ),
    ))


def _task8_chunks() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(index=0, pdf_bytes=b"%PDF-chunk-zero", page_count=1),
        SimpleNamespace(index=1, pdf_bytes=b"%PDF-chunk-one", page_count=1),
    ]


def _install_task8_pipeline(monkeypatch, *, events=None, evidence=None):
    events = events if events is not None else []
    chunks = _task8_chunks()
    manifest = _task8_manifest()
    crops = tuple(
        SourceCrop(visual=visual, source_png=b"png-" + visual.visual_id.encode(), width_px=10, height_px=10)
        for visual in manifest.visuals
    )
    monkeypatch.setattr(
        gemini_client,
        "split_pdf_chunks",
        lambda _pdf, *, budget: events.append(("split", budget)) or chunks,
        raising=False,
    )
    monkeypatch.setattr(
        gemini_client,
        "collect_mathpix_evidence",
        lambda chunk, budget: events.append(("mathpix", chunk.index, budget))
        or (evidence(chunk) if callable(evidence) else evidence),
        raising=False,
    )
    monkeypatch.setattr(
        gemini_client,
        "inventory_chunk",
        lambda chunk, call, budget, found, *, memory_budget: events.append(
            ("inventory", chunk.index, found, budget, memory_budget)
        ) or SimpleNamespace(chunk_index=chunk.index),
        raising=False,
    )
    monkeypatch.setattr(
        gemini_client,
        "finalize_manifest",
        lambda audited, expected, budget, **kwargs: events.append(
            ("finalize", tuple(item.chunk_index for item in audited), budget, kwargs)
        ) or manifest,
        raising=False,
    )
    monkeypatch.setattr(
        gemini_client,
        "extract_source_crops",
        lambda pdf, visuals, supplied_chunks, budget, memory, **kwargs: events.append(
            ("crops", tuple(v.visual_id for v in visuals), budget, memory)
        ) or list(crops),
        raising=False,
    )
    monkeypatch.setattr(
        gemini_client,
        "_source_verified_quote",
        lambda _pdf, quote, budget=None: quote,
    )
    monkeypatch.setattr(gemini_client, "_tidy", lambda _notes, budget=None: None)
    monkeypatch.setattr(gemini_client, "_ensure_mindmap", lambda _notes, budget=None: None)
    return chunks, manifest, crops, events


def test_chunk_limit_rejects_before_any_provider_or_raster_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = FPDF(unit="pt", format=(200, 300))
    for _ in range(9):
        pdf.add_page()
    source = bytes(pdf.output())
    monkeypatch.setattr(config, "MAX_CHUNKS", 8)
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1)
    dispatches: list[str] = []

    def forbidden(name: str):
        def call(*_args: object, **_kwargs: object) -> object:
            dispatches.append(name)
            raise AssertionError(f"{name} must not run past the chunk cap")

        return call

    for name in (
        "collect_mathpix_evidence",
        "inventory_chunk",
        "finalize_manifest",
        "extract_source_crops",
        "_generate_one",
    ):
        monkeypatch.setattr(gemini_client, name, forbidden(name))
    monkeypatch.setattr(
        gemini_client.image_gen,
        "render_visuals",
        forbidden("render_visuals"),
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_manifest_note_bundle(source)

    assert caught.value.code == "too_many_chunks"
    assert dispatches == []


def test_structured_pdf_schema_call_supports_source_and_overlay_on_litellm(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))]
    )
    def create(**kwargs):
        captured["request"] = kwargs
        return response

    def with_options(**kwargs):
        captured["options"] = kwargs
        return call_client

    def close():
        captured["closed"] = captured.get("closed", 0) + 1

    call_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    raw_client = SimpleNamespace(
        with_options=with_options,
        close=close,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client, raising=False)

    result = gemini_client._generate_pdf_schema(
        [
            ModelFile("source.pdf", "application/pdf", b"source"),
            ModelFile("overlay.pdf", "application/pdf", b"overlay"),
        ],
        "audit prompt",
        _GatewaySchema,
        123,
        0.2,
        _visual_budget(),
    )

    content = captured["request"]["messages"][0]["content"]
    assert result == _GatewaySchema(value="ok")
    assert content[0] == {"type": "text", "text": "audit prompt"}
    assert [item["file"]["filename"] for item in content[1:]] == ["source.pdf", "overlay.pdf"]
    assert all(item["type"] == "file" for item in content[1:])
    assert captured["request"]["response_format"]["json_schema"]["schema"] == _GatewaySchema.model_json_schema()
    assert captured["request"]["response_format"]["json_schema"]["strict"] is True
    assert captured["closed"] == 1


@pytest.mark.parametrize(
    ("choices", "expected_category", "expected_suffix"),
    [
        pytest.param([], "empty_choices", "", id="choices"),
        pytest.param(
            [SimpleNamespace(message=SimpleNamespace(content=None))],
            "empty_content",
            " finish=None content_chars=0 reasoning_chars=0"
            " completion_tokens=None reasoning_tokens=None fenced=False",
            id="content",
        ),
        pytest.param(
            [SimpleNamespace(message=SimpleNamespace(content='{"private":"detail"}'))],
            "schema_invalid",
            " finish=None content_chars=20 reasoning_chars=0"
            " completion_tokens=None reasoning_tokens=None fenced=False"
            " validation_errors=1 error_types=missing:1 error_locs=value",
            id="schema",
        ),
    ],
)
def test_litellm_pdf_schema_failure_logs_only_fixed_response_category(
    monkeypatch,
    capsys,
    choices,
    expected_category,
    expected_suffix,
):
    response = SimpleNamespace(choices=choices)
    call_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )
    raw_client = SimpleNamespace(
        with_options=lambda **_kwargs: call_client,
        close=lambda: None,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: raw_client,
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"source")],
            "inventory prompt",
            _GatewaySchema,
            123,
            0.1,
            _visual_budget(),
        )

    assert caught.value.code == "inventory_incomplete"
    output_line = capsys.readouterr().out.strip()
    assert output_line == (
        "[notesbot.model-error] stage=schema category="
        + expected_category
        + expected_suffix
    )
    assert "private" not in output_line and "detail" not in output_line


def test_structured_pdf_schema_call_supports_source_and_overlay_on_google(monkeypatch):
    captured = {}
    response = SimpleNamespace(parsed=_GatewaySchema(value="ok"), text="provider-private")
    def generate_content(**kwargs):
        captured["request"] = kwargs
        return response

    def build_visual_client(timeout_ms):
        captured["timeout_ms"] = timeout_ms
        return fake_client

    def close():
        captured["closed"] = captured.get("closed", 0) + 1

    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content),
        close=close,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_client",
        build_visual_client,
        raising=False,
    )

    result = gemini_client._generate_pdf_schema(
        [
            ModelFile("source.pdf", "application/pdf", b"source"),
            ModelFile("overlay.pdf", "application/pdf", b"overlay"),
        ],
        "audit prompt",
        _GatewaySchema,
        123,
        0.2,
        _visual_budget(),
    )

    contents = captured["request"]["contents"]
    assert result == _GatewaySchema(value="ok")
    assert contents[0] == "audit prompt"
    assert [part.inline_data.data for part in contents[1:]] == [b"source", b"overlay"]
    assert captured["request"]["config"].response_schema is _GatewaySchema
    assert captured["closed"] == 1


def test_generation_inventories_and_crops_before_note_summarization(monkeypatch):
    _, _, _, events = _install_task8_pipeline(monkeypatch)

    def generate(_pdf, *_args, chunk_index=None, **_kwargs):
        events.append(("note", chunk_index))
        return _source_notes()

    monkeypatch.setattr(gemini_client, "_generate_one", generate)
    gemini_client._generate_manifest_note_bundle(b"source")

    names = [event[0] for event in events]
    assert names.index("crops") > max(i for i, name in enumerate(names) if name == "inventory")
    assert names.index("crops") < min(i for i, name in enumerate(names) if name == "note")


def test_generation_collects_mathpix_then_inventory_for_each_chunk_in_order(monkeypatch):
    marker = object()
    _, _, _, events = _install_task8_pipeline(monkeypatch, evidence=marker)
    monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: _source_notes())

    gemini_client._generate_manifest_note_bundle(b"source")

    discovery = [(event[0], event[1]) for event in events if event[0] in {"mathpix", "inventory"}]
    assert discovery == [("mathpix", 0), ("inventory", 0), ("mathpix", 1), ("inventory", 1)]
    assert [event[2] for event in events if event[0] == "inventory"] == [marker, marker]


def test_mathpix_none_uses_same_single_source_only_inventory_call(monkeypatch):
    _, _, _, events = _install_task8_pipeline(monkeypatch, evidence=None)
    monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: _source_notes())

    gemini_client._generate_manifest_note_bundle(b"source")

    inventory = [event for event in events if event[0] == "inventory"]
    assert [(event[1], event[2]) for event in inventory] == [(0, None), (1, None)]


def test_mathpix_cleanup_error_stops_before_inventory_notes_or_render(monkeypatch):
    events = []
    _install_task8_pipeline(monkeypatch, events=events)
    monkeypatch.setattr(
        gemini_client,
        "collect_mathpix_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(VisualFidelityError("mathpix_cleanup_failed")),
        raising=False,
    )
    monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: events.append(("note",)))
    monkeypatch.setattr(gemini_client.image_gen, "render_visuals", lambda *_a, **_k: events.append(("render",)))

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_manifest_note_bundle(b"source")

    assert caught.value.code == "mathpix_cleanup_failed"
    assert not any(event[0] in {"inventory", "note", "render"} for event in events)


def test_mathpix_dto_and_raw_outputs_never_enter_notes_or_runtime_attachments(monkeypatch):
    evidence = SimpleNamespace(raw="MATHPIX-PRIVATE-RAW", dto="MATHPIX-PRIVATE-DTO")
    _, _, _, events = _install_task8_pipeline(monkeypatch, evidence=evidence)
    captured = []
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=_source_notes().model_dump_json())
    )])

    def create(**kwargs):
        captured.append(kwargs)
        return response

    per_call = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    raw_client = SimpleNamespace(
        with_options=lambda **_kwargs: per_call,
        close=lambda: None,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)
    bundle = gemini_client._generate_manifest_note_bundle(b"source")

    dumped = bundle.notes.model_dump_json()
    prompts = [request["messages"][0]["content"][0]["text"] for request in captured]
    assert "MATHPIX-PRIVATE" not in dumped
    assert all("MATHPIX-PRIVATE" not in repr(request) for request in captured)
    assert all("section_hint" not in prompt for prompt in prompts)
    assert all("PRIVATE MODEL HINT" not in prompt for prompt in prompts)
    assert all("topic_key" not in prompt for prompt in prompts)
    assert sorted(tuple(visual_id for visual_id in ("sv-0001", "sv-0002") if visual_id in prompt) for prompt in prompts) == [
        ("sv-0001",), ("sv-0002",)
    ]
    assert all(getattr(section, "_source_pdf", None) is None for section in bundle.notes.sections)


def test_manifest_note_bundle_keeps_manifest_crops_budget_and_memory_private_after_polish(monkeypatch):
    _, manifest, crops, _ = _install_task8_pipeline(monkeypatch)
    monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: _source_notes())

    bundle = gemini_client._generate_manifest_note_bundle(b"source")

    assert bundle.manifest is manifest
    assert bundle.crops == crops
    assert isinstance(bundle.budget, VisualRunBudget)
    assert isinstance(bundle.memory, VisualMemoryBudget)
    assert bundle.chunk_count == 2
    dumped = bundle.notes.model_dump_json()
    assert "max_total_bytes" not in dumped
    assert not any(
        name.startswith("_visual") or name == "_source_crops"
        for name in bundle.notes.__dict__
    )
    assert all("_source_pdf" not in section.__dict__ for section in bundle.notes.sections)


@pytest.mark.parametrize("failure_stage", ["crop", "note_base_exception"])
def test_manifest_note_bundle_closes_owned_memory_on_every_pre_return_baseexception(
    monkeypatch,
    failure_stage,
):
    _, manifest, _, _ = _install_task8_pipeline(monkeypatch)
    captured = {}

    def extract(_pdf, visuals, _chunks, _budget, memory, **_kwargs):
        captured["memory"] = memory
        crops = []
        for visual in visuals:
            source_png = bytes(bytearray(f"owned-{visual.visual_id}".encode()))
            memory.reserve_buffer("source", source_png)
            crops.append(SourceCrop(
                visual=visual,
                source_png=source_png,
                width_px=10,
                height_px=10,
            ))
            if failure_stage == "crop":
                raise VisualFidelityError("crop_failed")
        return crops

    monkeypatch.setattr(gemini_client, "extract_source_crops", extract)

    if failure_stage == "note_base_exception":
        monkeypatch.setattr(
            gemini_client,
            "_generate_one",
            lambda *_a, **_k: (_ for _ in ()).throw(
                asyncio.CancelledError("PRIVATE NOTE CANCELLATION")
            ),
        )
        expected_error = asyncio.CancelledError
    else:
        monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: _source_notes())
        expected_error = VisualFidelityError

    with pytest.raises(expected_error):
        gemini_client._generate_manifest_note_bundle(b"source")

    memory = captured["memory"]
    assert memory.resident_bytes() == 0
    assert memory.source_bytes == 0
    assert memory.context_bytes == 0
    assert memory.asset_bytes == 0
    with pytest.raises(VisualFidelityError) as closed:
        memory.reserve_buffer("source", b"after-close")
    assert closed.value.code == "memory_limit_exceeded"


def test_successful_manifest_note_bundle_keeps_owned_memory_live(monkeypatch):
    _install_task8_pipeline(monkeypatch)

    def extract(_pdf, visuals, _chunks, _budget, memory, **_kwargs):
        crops = []
        for visual in visuals:
            source_png = bytes(bytearray(f"owned-{visual.visual_id}".encode()))
            memory.reserve_buffer("source", source_png)
            crops.append(SourceCrop(
                visual=visual,
                source_png=source_png,
                width_px=10,
                height_px=10,
            ))
        return crops

    monkeypatch.setattr(gemini_client, "extract_source_crops", extract)
    monkeypatch.setattr(gemini_client, "_generate_one", lambda *_a, **_k: _source_notes())

    bundle = gemini_client._generate_manifest_note_bundle(b"source")

    assert bundle.memory.resident_bytes() > 0
    assert bundle.memory.source_bytes > 0
    extra = bytes(bytearray(b"still-open-after-bundle-return"))
    assert bundle.memory.reserve_buffer("asset", extra) is not None
    bundle.memory.close()


def test_multichunk_generation_passes_only_local_ids_and_preserves_global_order(monkeypatch):
    _, manifest, _, _ = _install_task8_pipeline(monkeypatch)
    seen = []

    def generate(_pdf, *_args, visual_manifest=None, chunk_index=None, **_kwargs):
        seen.append((chunk_index, manifest_ids(visual_manifest)))
        visual_id = visual_manifest.visuals[0].visual_id
        return ShortNotes(
            lecture_no="",
            subject="Physics",
            title=f"Part {chunk_index}",
            sections=[Section(heading=f"Part {chunk_index}", visual_ids=[visual_id])],
        )

    monkeypatch.setattr(gemini_client, "_generate_one", generate)
    bundle = gemini_client._generate_manifest_note_bundle(b"source")

    assert sorted(seen) == [(0, ("sv-0001",)), (1, ("sv-0002",))]
    flattened = tuple(visual_id for section in bundle.notes.sections for visual_id in section.visual_ids)
    assert flattened == manifest_ids(manifest)
    assert [section.heading for section in bundle.notes.sections] == ["Part 0", "Part 1"]


@pytest.mark.parametrize(
    "failing_stage",
    [
        "inventory_malformed",
        "audit_malformed",
        "provider_timeout",
        "provider_cancel",
        "provider_error",
        "finalization",
        "crops",
    ],
)
def test_any_inventory_or_crop_failure_prevents_partial_note_generation(monkeypatch, failing_stage):
    pdf_bytes, calls = _install_real_task8_gateway(
        monkeypatch, failure=failing_stage
    )
    if failing_stage == "finalization":
        monkeypatch.setattr(
            gemini_client,
            "finalize_manifest",
            lambda *_a, **_k: (_ for _ in ()).throw(
                VisualFidelityError("inventory_incomplete")
            ),
        )

    with pytest.raises(VisualFidelityError):
        gemini_client._generate_manifest_note_bundle(pdf_bytes)

    assert "ShortNotes" not in [call["schema"] for call in calls]


def test_pipeline_deadline_stops_before_starting_another_model_call(monkeypatch):
    calls = []
    budget = VisualRunBudget(deadline_at=10.0014, clock=lambda: 10.001)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: calls.append("dispatch") or SimpleNamespace(),
        raising=False,
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt", _GatewaySchema, 10, 0.0, budget,
        )

    assert caught.value.code == "deadline_exceeded"
    assert calls == []


def _retry_capable_budget():
    # remaining must exceed the 60s retry reserve or the loop never retries,
    # and must stay <= VISUAL_PIPELINE_DEADLINE_SECONDS (780) to be valid.
    return VisualRunBudget(deadline_at=710.0, clock=lambda: 10.0)


def test_invalid_schema_response_gets_one_bounded_retry(monkeypatch):
    calls = []
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))]
        ),
    ]

    def create(**kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    call_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    raw_client = SimpleNamespace(with_options=lambda **_k: call_client, close=lambda: None)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client, "_build_visual_lite_client", lambda: raw_client, raising=False
    )

    result = gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, _retry_capable_budget(),
    )

    assert result == _GatewaySchema(value="ok")
    assert len(calls) == 2


def test_invalid_schema_response_fails_after_single_content_retry(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]
        )

    call_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    raw_client = SimpleNamespace(with_options=lambda **_k: call_client, close=lambda: None)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client, "_build_visual_lite_client", lambda: raw_client, raising=False
    )

    with pytest.raises(VisualFidelityError):
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt", _GatewaySchema, 10, 0.0, _retry_capable_budget(),
        )

    assert len(calls) == 2


def test_transport_timeout_gets_bounded_retry_then_succeeds(monkeypatch):
    import openai as openai_module

    calls = []
    sleeps = []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise openai_module.APITimeoutError(request=SimpleNamespace())
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))]
        )

    call_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    raw_client = SimpleNamespace(with_options=lambda **_k: call_client, close=lambda: None)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client, "_build_visual_lite_client", lambda: raw_client, raising=False
    )

    result = gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, _retry_capable_budget(),
    )

    assert result == _GatewaySchema(value="ok")
    assert len(calls) == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize("error_kind,expected_calls", [("timeout", 2), ("connection", 2), ("rate_limit", 4)])
def test_schema_transport_retries_do_not_multiply_slow_requests(monkeypatch, error_kind, expected_calls):
    import httpx
    import openai as openai_module

    calls = []
    sleeps = []
    request = httpx.Request("POST", "https://example.test/generate")
    def create(**kwargs):
        calls.append(kwargs)
        if error_kind == "timeout":
            raise openai_module.APITimeoutError(request=request)
        if error_kind == "connection":
            raise openai_module.APIConnectionError(request=request)
        raise openai_module.RateLimitError(
            "rate limited", response=httpx.Response(429, request=request), body=None)

    call_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    raw_client = SimpleNamespace(with_options=lambda **_k: call_client, close=lambda: None)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)
    monkeypatch.setattr(gemini_client.time, "sleep", sleeps.append)
    with pytest.raises(VisualFidelityError):
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt", _GatewaySchema, 10, 0.0, _retry_capable_budget())
    assert len(calls) == expected_calls
    assert len(sleeps) == expected_calls - 1


def test_manifest_note_generation_dispatches_exactly_once_per_chunk(monkeypatch):
    pdf_bytes, calls = _install_real_task8_gateway(monkeypatch)

    bundle = gemini_client._generate_manifest_note_bundle(pdf_bytes)

    assert bundle.budget.inventory_calls == {0: 1, 1: 1}
    assert bundle.budget.audit_calls == {0: 1, 1: 1}
    assert bundle.budget.note_calls == {0: 1, 1: 1}
    assert bundle.budget.mathpix_source_fallback == {0: 1, 1: 1}
    assert manifest_ids(bundle.manifest) == ("sv-0001", "sv-0002")
    assert tuple(
        visual_id
        for section in bundle.notes.sections
        for visual_id in section.visual_ids
    ) == ("sv-0001", "sv-0002")
    assert [call["schema"] for call in calls[:4]] == [
        "InventoryResponse",
        "InventoryAuditResponse",
        "InventoryResponse",
        "InventoryAuditResponse",
    ]
    assert [call["schema"] for call in calls[4:]] == ["ShortNotes", "ShortNotes"]
    note_prompts = [call["prompt"] for call in calls if call["schema"] == "ShortNotes"]
    assert sorted(
        tuple(visual_id for visual_id in ("sv-0001", "sv-0002") if visual_id in prompt)
        for prompt in note_prompts
    ) == [("sv-0001",), ("sv-0002",)]


@pytest.mark.parametrize(
    ("remaining", "cap", "expected_seconds", "expected_ms"),
    [(3.25, 5.0, 3.25, 3250), (9.0, 2.75, 2.75, 2750), (0.0019, 1.0, 0.0019, 1)],
)
def test_provider_timeout_is_minimum_of_cap_and_remaining_deadline(
    remaining, cap, expected_seconds, expected_ms
):
    seconds, milliseconds = gemini_client._bounded_model_timeout(
        _visual_budget(remaining=remaining), cap
    )

    assert seconds == pytest.approx(expected_seconds)
    assert milliseconds == expected_ms


def test_sub_millisecond_remaining_time_fails_before_all_visual_model_dispatches(monkeypatch):
    calls = []
    budget = _visual_budget(remaining=0.0009)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: calls.append("schema") or SimpleNamespace(),
        raising=False,
    )
    manifest = VisualManifest(visuals=(_task8_manifest().visuals[0],))

    with pytest.raises(VisualFidelityError):
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt", _GatewaySchema, 10, 0.0, budget,
        )
    with pytest.raises(VisualFidelityError):
        gemini_client._generate_one(
            b"pdf", visual_manifest=manifest, budget=budget, chunk_index=0
        )

    assert calls == []


@pytest.mark.parametrize("encoded_only", [False, True])
def test_structured_attachment_raw_and_base64_totals_are_enforced_before_dispatch(
    monkeypatch, encoded_only
):
    calls = []
    raw_cap = 5 if not encoded_only else 10
    encoded_cap = 100 if not encoded_only else 7
    monkeypatch.setattr(config, "VISUAL_MODEL_ATTACHMENT_TOTAL_BYTES", raw_cap)
    monkeypatch.setattr(config, "VISUAL_MODEL_ENCODED_ATTACHMENT_TOTAL_BYTES", encoded_cap)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: calls.append("dispatch") or SimpleNamespace(),
        raising=False,
    )

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("one.pdf", "application/pdf", b"1234"), ModelFile("two.pdf", "application/pdf", b"56")],
            "prompt", _GatewaySchema, 10, 0.0, _visual_budget(),
        )

    assert caught.value.code == "inventory_incomplete"
    assert calls == []


def test_visual_provider_clients_disable_sdk_retries(monkeypatch):
    captured = {}
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))])
    per_call = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_k: response)))

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["constructor"] = kwargs

        def with_options(self, **kwargs):
            captured["per_call"] = kwargs
            return per_call

        def close(self):
            captured["closed"] = captured.get("closed", 0) + 1

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, _visual_budget(),
    )
    assert captured["constructor"]["max_retries"] == 0
    assert captured["per_call"]["max_retries"] == 0
    assert captured["closed"] == 1

    direct = {}
    fake_direct = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_k: SimpleNamespace(parsed=_GatewaySchema(value="ok"), text="")),
        close=lambda: None,
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
    def direct_client(**kwargs):
        direct["kwargs"] = kwargs
        return fake_direct

    monkeypatch.setattr(gemini_client.genai, "Client", direct_client)
    gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, _visual_budget(),
    )
    retry = direct["kwargs"]["http_options"].retry_options
    assert retry.attempts == 1


def test_structured_verifier_invalid_output_maps_to_fixed_render_error(monkeypatch):
    private_response = "PROVIDER-SECRET malformed verifier output"
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=private_response)
    )])
    per_call = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: response
    )))
    closed = []
    raw_client = SimpleNamespace(
        with_options=lambda **_kwargs: per_call,
        close=lambda: closed.append("closed"),
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("source.png", "image/png", b"png")],
            "verify", RedrawVerdict, 100, 0.0, _visual_budget(),
        )

    assert caught.value.code == "render_failed"
    assert "PROVIDER-SECRET" not in str(caught.value)
    assert closed == ["closed"]


def test_manifest_generation_skips_legacy_qc_and_design_provider_passes_even_when_enabled(monkeypatch):
    _, _, _, events = _install_task8_pipeline(monkeypatch)

    def generate(_pdf, *_args, budget=None, chunk_index=None, **_kwargs):
        budget.take_note(chunk_index)
        return _source_notes()

    monkeypatch.setattr(gemini_client, "_generate_one", generate)
    monkeypatch.setattr(gemini_client, "_qc_pass", lambda *_a: events.append(("qc",)))
    monkeypatch.setattr(gemini_client, "_design_pass", lambda *_a: events.append(("design",)))
    monkeypatch.setattr(config, "NOTES_QC", True)
    monkeypatch.setattr(config, "NOTES_DESIGN", True)

    gemini_client._generate_manifest_note_bundle(b"source")

    assert not any(event[0] in {"qc", "design"} for event in events)


def test_quote_tidy_and_mindmap_local_loops_check_shared_deadline_per_page_or_section():
    class CountingBudget:
        def __init__(self):
            self.calls = 0

        def check_deadline(self):
            self.calls += 1

    budget = CountingBudget()
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Motion",
        sections=[
            Section(heading="One", points=["first"]),
            Section(heading="Two", points=["second"]),
        ],
        mindmap=MindMap(center="Motion", branches=[
            Branch(label="One", children=["first"]),
            Branch(label="Two", children=["second"]),
        ]),
    )
    source = _pdf_with_text("first\nsecond")

    gemini_client._source_verified_quote(source, "first", budget=budget)
    after_quote = budget.calls
    gemini_client._tidy(notes, budget=budget)
    after_tidy = budget.calls
    gemini_client._ensure_mindmap(notes, budget=budget)

    assert after_quote >= 4
    assert after_tidy - after_quote >= 2 * len(notes.sections)
    assert budget.calls - after_tidy >= 2 * (len(notes.sections) + len(notes.mindmap.branches))


class _AdvancingClock:
    def __init__(self, now: float = 10.0):
        self.now = now
        self.events: list[str] = []

    def __call__(self) -> float:
        self.events.append("clock")
        return self.now

    def advance(self, seconds: float, event: str) -> None:
        self.now += seconds
        self.events.append(event)


def _record_deadline_checks(budget: VisualRunBudget, clock: _AdvancingClock) -> None:
    check_deadline = budget.check_deadline

    def checked() -> None:
        check_deadline()
        clock.events.append("check_deadline")

    budget.check_deadline = checked  # type: ignore[method-assign]


def test_litellm_schema_dispatch_refreshes_timeout_after_encoding_and_client_setup(monkeypatch):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    _record_deadline_checks(budget, clock)
    captured = {}
    real_encode = base64.b64encode

    def encode(data):
        clock.advance(1.0, "encode")
        return real_encode(data)

    def create(**kwargs):
        captured["prior_event"] = clock.events[-1]
        clock.events.append("create")
        captured["request"] = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"value":"ok"}')
        )])

    def with_options(**kwargs):
        clock.advance(1.0, "with_options")
        captured["options"] = kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def close():
        captured["closed"] = captured.get("closed", 0) + 1

    def build_client():
        clock.advance(3.0, "client")
        return SimpleNamespace(
            with_options=with_options,
            close=close,
        )

    monkeypatch.setattr(config, "VISUAL_MODEL_TIMEOUT_SECONDS", 8)
    monkeypatch.setattr(gemini_client.base64, "b64encode", encode)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)

    result = gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, budget,
    )

    assert result == _GatewaySchema(value="ok")
    assert captured["request"]["timeout"] == pytest.approx(5.0)
    assert captured["prior_event"] == "check_deadline"
    assert captured["closed"] == 1


def test_google_schema_dispatch_refreshes_request_timeout_after_client_setup(monkeypatch):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    _record_deadline_checks(budget, clock)
    captured = {}

    def generate_content(**kwargs):
        captured["prior_event"] = clock.events[-1]
        clock.events.append("generate_content")
        captured["request"] = kwargs
        return SimpleNamespace(parsed=_GatewaySchema(value="ok"), text="")

    def close():
        captured["closed"] = captured.get("closed", 0) + 1

    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content),
        close=close,
    )

    def build_client(timeout_ms):
        captured["client_timeout_ms"] = timeout_ms
        clock.advance(4.0, "client")
        return fake_client

    monkeypatch.setattr(config, "VISUAL_MODEL_TIMEOUT_SECONDS", 8)
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
    monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    result = gemini_client._generate_pdf_schema(
        [ModelFile("source.pdf", "application/pdf", b"pdf")],
        "prompt", _GatewaySchema, 10, 0.0, budget,
    )

    request_http = captured["request"]["config"].http_options
    assert result == _GatewaySchema(value="ok")
    assert request_http.timeout == 6000
    assert request_http.retry_options.attempts == 1
    assert captured["prior_event"] == "check_deadline"
    assert captured["closed"] == 1


@pytest.mark.parametrize("gateway", ["litellm", "google"])
def test_manifest_note_dispatch_refreshes_timeout_after_client_setup(monkeypatch, gateway):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    _record_deadline_checks(budget, clock)
    captured = {}
    manifest = VisualManifest(visuals=(_task8_manifest().visuals[0],))
    notes = _source_notes()
    monkeypatch.setattr(config, "VISUAL_MODEL_TIMEOUT_SECONDS", 8)

    if gateway == "litellm":
        real_encode = base64.b64encode

        def encode(data):
            clock.advance(1.0, "encode")
            return real_encode(data)

        def create(**kwargs):
            captured["prior_event"] = clock.events[-1]
            clock.events.append("create")
            captured["request"] = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=notes.model_dump_json())
            )])

        def with_options(**kwargs):
            clock.advance(1.0, "with_options")
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        def close():
            captured["closed"] = captured.get("closed", 0) + 1

        def build_client():
            clock.advance(3.0, "client")
            return SimpleNamespace(
                with_options=with_options,
                close=close,
            )

        monkeypatch.setattr(gemini_client.base64, "b64encode", encode)
        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def generate_content(**kwargs):
            captured["prior_event"] = clock.events[-1]
            clock.events.append("generate_content")
            captured["request"] = kwargs
            return SimpleNamespace(parsed=notes, text="")

        def close():
            captured["closed"] = captured.get("closed", 0) + 1

        fake_client = SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content),
            close=close,
        )

        def build_client(_timeout_ms):
            clock.advance(4.0, "client")
            return fake_client

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    result = gemini_client._generate_one(
        b"pdf", visual_manifest=manifest, budget=budget, chunk_index=0
    )

    if gateway == "litellm":
        assert captured["request"]["timeout"] == pytest.approx(5.0)
        assert captured["prior_event"] == "check_deadline"
        assert captured["closed"] == 1
    else:
        request_http = captured["request"]["config"].http_options
        assert request_http.timeout == 6000
        assert request_http.retry_options.attempts == 1
        assert captured["prior_event"] == "check_deadline"
        assert captured["closed"] == 1
    assert result.sections[-1].visual_ids == ["sv-0001"]


@pytest.mark.parametrize("gateway", ["litellm", "google"])
def test_manifest_note_setup_deadline_closes_created_client_without_dispatch(
    monkeypatch,
    gateway,
):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    dispatched = []
    closed = []

    if gateway == "litellm":
        def build_client():
            clock.advance(11.0, "client")
            return SimpleNamespace(
                with_options=lambda **_kwargs: dispatched.append("with_options"),
                close=lambda: closed.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def build_client(_timeout_ms):
            clock.advance(11.0, "client")
            return SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **_kwargs: dispatched.append("google")
                ),
                close=lambda: closed.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_one(
            b"pdf",
            visual_manifest=VisualManifest(
                visuals=(_task8_manifest().visuals[0],)
            ),
            budget=budget,
            chunk_index=0,
        )

    assert caught.value.code == "deadline_exceeded"
    assert dispatched == []
    assert closed == ["closed"]


def test_manifest_note_litellm_with_options_setup_deadline_closes_raw_client(
    monkeypatch,
):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    dispatched = []
    closed = []

    def with_options(**_kwargs):
        clock.advance(11.0, "with_options")
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_request: dispatched.append("provider")
            )
        ))

    raw_client = SimpleNamespace(
        with_options=with_options,
        close=lambda: closed.append("closed"),
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_one(
            b"pdf",
            visual_manifest=VisualManifest(
                visuals=(_task8_manifest().visuals[0],)
            ),
            budget=budget,
            chunk_index=0,
        )

    assert caught.value.code == "deadline_exceeded"
    assert dispatched == []
    assert closed == ["closed"]


@pytest.mark.parametrize("gateway", ["litellm", "google"])
def test_manifest_note_client_close_failure_preserves_primary_deadline(
    monkeypatch,
    gateway,
):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    close_attempts = []

    def fail_close():
        close_attempts.append("closed")
        raise RuntimeError("PRIVATE CLOSE FAILURE")

    if gateway == "litellm":
        def build_client():
            clock.advance(11.0, "client")
            return SimpleNamespace(
                with_options=lambda **_kwargs: pytest.fail(
                    "expired setup reached with_options"
                ),
                close=fail_close,
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def build_client(_timeout_ms):
            clock.advance(11.0, "client")
            return SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **_kwargs: pytest.fail(
                        "expired setup reached provider dispatch"
                    )
                ),
                close=fail_close,
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_one(
            b"pdf",
            visual_manifest=VisualManifest(
                visuals=(_task8_manifest().visuals[0],)
            ),
            budget=budget,
            chunk_index=0,
        )

    assert caught.value.code == "deadline_exceeded"
    assert "PRIVATE" not in str(caught.value)
    assert close_attempts == ["closed"]


@pytest.mark.parametrize("gateway", ["litellm", "google"])
def test_client_setup_consuming_deadline_prevents_schema_dispatch(monkeypatch, gateway):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    dispatched = []
    closed = []

    if gateway == "litellm":
        def build_client():
            clock.advance(11.0, "client")
            return SimpleNamespace(
                with_options=lambda **_kwargs: SimpleNamespace(
                    chat=SimpleNamespace(completions=SimpleNamespace(
                        create=lambda **_kwargs: dispatched.append("litellm")
                    ))
                ),
                close=lambda: closed.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def build_client(_timeout_ms):
            clock.advance(11.0, "client")
            return SimpleNamespace(
                models=SimpleNamespace(generate_content=lambda **_kwargs: dispatched.append("google")),
                close=lambda: closed.append("closed"),
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt", _GatewaySchema, 10, 0.0, budget,
        )

    assert caught.value.code == "deadline_exceeded"
    assert dispatched == []
    assert closed == ["closed"]


@pytest.mark.parametrize("gateway", ["litellm", "google"])
def test_schema_client_close_failure_does_not_mask_primary_deadline(monkeypatch, gateway):
    clock = _AdvancingClock()
    budget = VisualRunBudget(deadline_at=20.0, clock=clock)
    close_attempts = []

    def fail_close():
        close_attempts.append("closed")
        raise RuntimeError("private close failure")

    if gateway == "litellm":
        def build_client():
            clock.advance(11.0, "client")
            return SimpleNamespace(
                with_options=lambda **_kwargs: pytest.fail("deadline was not refreshed"),
                close=fail_close,
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def build_client(_timeout_ms):
            clock.advance(11.0, "client")
            return SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **_kwargs: pytest.fail(
                        "deadline was not refreshed"
                    )
                ),
                close=fail_close,
            )

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    with pytest.raises(VisualFidelityError) as caught:
        gemini_client._generate_pdf_schema(
            [ModelFile("source.pdf", "application/pdf", b"pdf")],
            "prompt",
            _GatewaySchema,
            10,
            0.0,
            budget,
        )

    assert caught.value.code == "deadline_exceeded"
    assert close_attempts == ["closed"]


@pytest.mark.parametrize("operation", ["schema", "note"])
@pytest.mark.parametrize("gateway", ["litellm", "google"])
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "malformed"])
def test_visual_gateway_failure_matrix_is_fixed_and_closes_schema_clients(
    monkeypatch, operation, gateway, outcome
):
    closed = []
    private_detail = f"PRIVATE-{gateway}-{operation}-{outcome}"

    def provider_response():
        if outcome == "timeout":
            raise TimeoutError(private_detail)
        if outcome == "cancel":
            raise asyncio.CancelledError(private_detail)
        if gateway == "litellm":
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=private_detail)
            )])
        return SimpleNamespace(parsed=None, text=private_detail)

    if gateway == "litellm":
        per_call = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: provider_response()
        )))
        raw_client = SimpleNamespace(
            with_options=lambda **_kwargs: per_call,
            close=lambda: closed.append("closed"),
        )
        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", lambda: raw_client)
    else:
        direct_client = SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **_kwargs: provider_response()),
            close=lambda: closed.append("closed"),
        )
        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(
            gemini_client, "_build_visual_client", lambda _timeout_ms: direct_client
        )

    budget = _visual_budget()
    with pytest.raises(VisualFidelityError) as caught:
        if operation == "schema":
            gemini_client._generate_pdf_schema(
                [ModelFile("source.pdf", "application/pdf", b"pdf")],
                "prompt", _GatewaySchema, 10, 0.0, budget,
            )
        else:
            gemini_client._generate_one(
                b"pdf",
                visual_manifest=VisualManifest(
                    visuals=(_task8_manifest().visuals[0],)
                ),
                budget=budget,
                chunk_index=0,
            )

    assert caught.value.code == (
        "inventory_incomplete" if operation == "schema" else "link_invalid"
    )
    assert private_detail not in str(caught.value)
    assert closed == ["closed"]


@pytest.mark.parametrize("gateway", ["litellm", "google"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_two_chunk_manifest_notes_use_distinct_clients_closed_once_per_worker(
    monkeypatch,
    gateway,
    outcome,
):
    _install_task8_pipeline(monkeypatch)
    created = []
    close_counts = {}
    private_provider = f"PRIVATE {gateway} SECOND WORKER FAILURE"
    private_close = f"PRIVATE {gateway} SECOND WORKER CLOSE FAILURE"

    def new_owner():
        token = object()
        close_counts[id(token)] = 0
        state = {"primary_failed": False}

        def close():
            close_counts[id(token)] += 1
            if state["primary_failed"]:
                raise RuntimeError(private_close)

        return token, state, close

    def maybe_fail(prompt, state):
        if outcome == "failure" and "sv-0002" in prompt:
            state["primary_failed"] = True
            raise RuntimeError(private_provider)

    if gateway == "litellm":
        def build_client():
            token, state, close = new_owner()

            def create(**kwargs):
                prompt = kwargs["messages"][0]["content"][0]["text"]
                maybe_fail(prompt, state)
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=_source_notes().model_dump_json())
                )])

            raw_client = SimpleNamespace(
                token=token,
                with_options=lambda **_kwargs: SimpleNamespace(
                    chat=SimpleNamespace(
                        completions=SimpleNamespace(create=create)
                    )
                ),
                close=close,
            )
            created.append(raw_client)
            return raw_client

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
        monkeypatch.setattr(gemini_client, "_build_visual_lite_client", build_client)
    else:
        def build_client(_timeout_ms):
            token, state, close = new_owner()

            def generate_content(**kwargs):
                prompt = kwargs["contents"][0]
                maybe_fail(prompt, state)
                return SimpleNamespace(parsed=_source_notes(), text="")

            raw_client = SimpleNamespace(
                token=token,
                models=SimpleNamespace(generate_content=generate_content),
                close=close,
            )
            created.append(raw_client)
            return raw_client

        monkeypatch.setattr(gemini_client, "_use_litellm", lambda: False)
        monkeypatch.setattr(gemini_client, "_build_visual_client", build_client)

    if outcome == "failure":
        with pytest.raises(VisualFidelityError) as caught:
            gemini_client._generate_manifest_note_bundle(b"source")
        assert caught.value.code == "link_invalid"
        assert private_provider not in str(caught.value)
        assert private_close not in str(caught.value)
    else:
        bundle = gemini_client._generate_manifest_note_bundle(b"source")
        assert manifest_ids(bundle.manifest) == ("sv-0001", "sv-0002")
        bundle.memory.close()

    assert len(created) == 2
    assert len({id(client) for client in created}) == 2
    assert sorted(close_counts.values()) == [1, 1]


def _two_page_task8_pdf() -> bytes:
    pdf = FPDF()
    for page_number in (1, 2):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 8, f"Source topic {page_number}")
    return bytes(pdf.output())


def _install_real_task8_gateway(monkeypatch, *, failure: str = ""):
    """Keep ledger owners real; replace only transport/raster-heavy boundaries."""
    calls: list[dict[str, str]] = []
    inventories: dict[int, InventoryResponse] = {}
    probes: dict[int, CoverageProbe] = {}

    def process_overlay_pages(
        chunk,
        inventory,
        provisional_ids,
        budget,
        memory,
        supplied_probes=None,
    ):
        del inventory, provisional_ids, memory, supplied_probes
        budget.check_deadline()
        probe = CoverageProbe(
            probe_id=f"component-c{chunk.index:02d}",
            kind="component",
            local_page=1,
            box=(20.0, 20.0, 60.0, 60.0),
            foreground_fraction=1.0,
        )
        probes[chunk.index] = probe
        return (probe,), chunk.pdf_bytes, None

    inventory_index = 0
    audit_index = 0

    def create(**kwargs):
        nonlocal inventory_index, audit_index
        schema_name = kwargs["response_format"]["json_schema"]["name"]
        prompt = kwargs["messages"][0]["content"][0]["text"]
        calls.append({"schema": schema_name, "prompt": prompt})

        if len(calls) == 1:
            if failure == "provider_timeout":
                raise TimeoutError("PRIVATE provider timeout")
            if failure == "provider_cancel":
                raise asyncio.CancelledError("PRIVATE provider cancellation")
            if failure == "provider_error":
                raise RuntimeError("PRIVATE provider failure")

        if schema_name == "InventoryResponse":
            if failure == "inventory_malformed" and inventory_index == 0:
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="PRIVATE malformed inventory")
                )])
            chunk_index = inventory_index
            inventory_index += 1
            inventory = InventoryResponse(
                pages_checked=(1,),
                regions=(VisualRegion(
                    local_page=1,
                    box=(20.0, 20.0, 60.0, 60.0),
                    kind="diagram",
                    topic_anchor=InventoryTopicAnchor(
                        anchor_ref=f"anchor-{chunk_index}",
                        local_page=1,
                        box=(5.0, 2.0, 40.0, 8.0),
                        exact_heading=f"Source topic {chunk_index + 1}",
                    ),
                    exact_labels=(),
                    context_regions=(),
                    section_hint="",
                    source_order_hint=0,
                ),),
            )
            inventories[chunk_index] = inventory
            payload = inventory.model_dump_json()
        elif schema_name == "InventoryAuditResponse":
            if failure == "audit_malformed" and audit_index == 0:
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="PRIVATE malformed audit")
                )])
            chunk_index = audit_index
            audit_index += 1
            inventory = inventories[chunk_index]
            provisional_id = f"draft-c{chunk_index:02d}-001"
            audit = InventoryAuditResponse(
                pages_checked=(1,),
                coverage_complete=True,
                unresolved_count=0,
                final_regions=(AuditedRegion(
                    **inventory.regions[0].model_dump(mode="python"),
                    source_region_ids=(provisional_id,),
                ),),
                probe_dispositions=(ProbeDisposition(
                    probe_id=probes[chunk_index].probe_id,
                    outcome="covered_visual",
                    final_region_indices=(0,),
                ),),
            )
            payload = audit.model_dump_json()
        elif schema_name == "ShortNotes":
            visual_id = "sv-0001" if "sv-0001" in prompt else "sv-0002"
            payload = ShortNotes(
                lecture_no="",
                subject="Physics",
                title=f"Part {visual_id}",
                sections=[Section(
                    heading=f"Source section {visual_id}",
                    points=[f"Source point {visual_id}"],
                )],
            ).model_dump_json()
        else:
            raise AssertionError(f"unexpected schema {schema_name}")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=payload)
        )])

    per_call = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: SimpleNamespace(
            with_options=lambda **_kwargs: per_call,
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(gemini_client, "_use_litellm", lambda: True)
    monkeypatch.setattr(visual_inventory, "_process_overlay_pages", process_overlay_pages)

    def expand_fallback_bounds(_chunk, _probes, audit, budget, _memory):
        budget.check_deadline()
        return audit, frozenset()

    monkeypatch.setattr(
        visual_inventory,
        "_expand_unsafe_fallback_bounds",
        expand_fallback_bounds,
    )
    monkeypatch.setattr(config, "MATHPIX_ENABLED", False)
    monkeypatch.setattr(config, "CHUNK_TARGET_BYTES", 1)

    def extract_crops(_pdf, visuals, _chunks, budget, _memory, **_kwargs):
        budget.check_deadline()
        if failure == "crops":
            raise VisualFidelityError("crop_failed")
        return [
            SourceCrop(
                visual=visual,
                source_png=f"png-{visual.visual_id}".encode(),
                width_px=10,
                height_px=10,
            )
            for visual in visuals
        ]

    monkeypatch.setattr(gemini_client, "extract_source_crops", extract_crops)
    return _two_page_task8_pdf(), calls


def test_project_runtime_matches_supported_vercel_python():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert (root / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


def test_visual_stage_failure_event_is_allowlisted_and_redacted(capsys):
    def fail():
        raise RuntimeError("provider-key=super-secret source-label=private")

    with pytest.raises(RuntimeError):
        gemini_client._run_visual_stage("inventory", fail)

    assert capsys.readouterr().out == (
        "[notesbot.visual-error-detail] stage=inventory category=internal_error\n"
    )
    with pytest.raises(ValueError):
        gemini_client._run_visual_stage("hostile-private-stage", fail)
    assert capsys.readouterr().out == ""
