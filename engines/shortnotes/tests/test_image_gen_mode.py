"""Tests for choosing the diagram renderer that matches the note art direction."""
from __future__ import annotations

import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from notesbot import gemini_client, image_gen
from notesbot.errors import VisualFidelityError
from notesbot.generation_policy import GenerationRun
from notesbot.pdf_utils import PdfChunk
from notesbot.prompts import Section, ShortNotes, Visual
from notesbot.visual_manifest import VisualManifest


class DiagramModeTests(unittest.TestCase):
    SOURCE_PDF = b"%PDF-1.7\nexact bounded source chunk"

    @staticmethod
    def _notes(*, source_pdf: bytes | None = SOURCE_PDF) -> tuple[ShortNotes, Section]:
        section = Section(
            heading="Daniell Cell",
            visual=Visual(kind="core_diagram", prompt="Daniell cell with salt bridge."),
        )
        if source_pdf is not None:
            object.__setattr__(section, "_source_pdf", source_pdf)
        notes = ShortNotes(
            lecture_no="",
            subject="Chemistry",
            title="Electrochemistry",
            sections=[section],
        )
        return notes, section

    @staticmethod
    def _png_bytes() -> bytes:
        from PIL import Image

        image = Image.new("RGB", (3, 2), (20, 40, 60))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def test_auto_mode_prefers_coloured_pencil_raster_before_svg_fallback(self):
        notes, section = self._notes()

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image", return_value=b"raster-sketch") as raster,
            patch("notesbot.svg_gen.try_svg", return_value="<svg></svg>") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        svg.assert_not_called()
        self.assertEqual(getattr(section, "_image", None), b"raster-sketch")
        self.assertIsNone(getattr(section, "_svg", None))

    def test_auto_mode_uses_svg_when_raster_generation_fails(self):
        notes, section = self._notes()

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image", return_value=None) as raster,
            patch("notesbot.svg_gen.try_svg", return_value="<svg></svg>") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        svg.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        self.assertIsNone(getattr(section, "_image", None))
        self.assertEqual(getattr(section, "_svg", None), "<svg></svg>")

    def test_auto_mode_keeps_prompt_as_manual_diagram_when_all_generators_fail(self):
        notes, section = self._notes()
        section.diagram = "old diagram"

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image", return_value=None),
            patch("notesbot.svg_gen.try_svg", return_value=None),
        ):
            image_gen.render_visuals(notes)

        self.assertEqual(section.diagram, "Daniell cell with salt bridge.")

    def test_one_image_reads_documented_dictionary_response_shape(self):
        captured = {}
        png = self._png_bytes()
        response = {
            "choices": [{
                "message": {
                    "images": [{
                        "image_url": {"url": (
                            "data:image/png;base64," + base64.b64encode(png).decode()
                        )},
                    }],
                },
            }],
        }
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: captured.update(kwargs) or response
                )
            )
        )

        with patch.object(gemini_client, "_lite_client", return_value=client):
            image = image_gen._one_image("Daniell cell", self.SOURCE_PDF)

        self.assertIsNotNone(image)
        from PIL import Image
        with Image.open(io.BytesIO(image)) as decoded:
            self.assertEqual(decoded.format, "PNG")
        self.assertEqual(captured["extra_body"], {"modalities": ["image", "text"]})
        sent = captured["messages"][0]["content"]
        self.assertEqual(sent[0]["type"], "file")
        self.assertEqual(sent[0]["file"]["filename"], "source-chunk.pdf")
        self.assertEqual(
            sent[0]["file"]["file_data"],
            "data:application/pdf;base64," + base64.b64encode(self.SOURCE_PDF).decode(),
        )
        self.assertEqual(sent[1]["type"], "text")
        self.assertIn("DRAWING SPECIFICATION:\nDaniell cell", sent[1]["text"])
        self.assertIn("Do not add", sent[1]["text"])

    def test_invalid_raster_bytes_are_rejected(self):
        self.assertIsNone(image_gen._flatten(b"not a decodable image"))

    def test_missing_source_reference_skips_model_renderers_and_uses_manual_fallback(self):
        notes, section = self._notes(source_pdf=None)

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image") as raster,
            patch("notesbot.svg_gen.try_svg", return_value="<svg></svg>") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_not_called()
        svg.assert_not_called()
        self.assertIsNone(getattr(section, "_svg", None))
        self.assertEqual(section.diagram, "Daniell cell with salt bridge.")

    def test_pdf_input_rejection_falls_back_without_text_only_raster_retry(self):
        notes, section = self._notes()

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image", side_effect=RuntimeError("PDF unsupported")) as raster,
            patch("notesbot.svg_gen.try_svg", return_value="<svg></svg>") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        svg.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        self.assertEqual(getattr(section, "_svg", None), "<svg></svg>")

    def test_svg_exception_is_isolated_to_one_visual(self):
        notes, first = self._notes()
        second = Section(
            heading="Second Figure",
            visual=Visual(kind="core_diagram", prompt="Second source figure."),
        )
        object.__setattr__(second, "_source_pdf", self.SOURCE_PDF)
        notes.sections.append(second)

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "auto"),
            patch.object(image_gen, "_one_image", return_value=None),
            patch(
                "notesbot.svg_gen.try_svg",
                side_effect=[RuntimeError("bad first SVG"), "<svg id='second'></svg>"],
            ),
        ):
            image_gen.render_visuals(notes)

        self.assertEqual(first.diagram, "Daniell cell with salt bridge.")
        self.assertEqual(getattr(second, "_svg", None), "<svg id='second'></svg>")

    def test_svg_mode_skips_raster_generation(self):
        notes, section = self._notes()

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "svg"),
            patch.object(image_gen, "_one_image") as raster,
            patch("notesbot.svg_gen.try_svg", return_value="<svg></svg>") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_not_called()
        svg.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        self.assertEqual(getattr(section, "_svg", None), "<svg></svg>")

    def test_image_mode_skips_svg_generation(self):
        notes, section = self._notes()

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "DIAGRAM_MODE", "image"),
            patch.object(image_gen, "_one_image", return_value=b"raster-sketch") as raster,
            patch("notesbot.svg_gen.try_svg") as svg,
        ):
            image_gen.render_visuals(notes)

        raster.assert_called_once_with(
            "Daniell cell with salt bridge.", self.SOURCE_PDF,
        )
        svg.assert_not_called()
        self.assertEqual(getattr(section, "_image", None), b"raster-sketch")

    def test_five_svg_jobs_make_four_sequential_model_calls_then_manual_fallback(self):
        sections = []
        expected_calls = []
        for index in range(1, 6):
            prompt = f"Source figure {index}."
            source_pdf = self.SOURCE_PDF + str(index).encode()
            section = Section(
                heading=f"Figure {index}",
                visual=Visual(kind="core_diagram", prompt=prompt),
            )
            object.__setattr__(section, "_source_pdf", source_pdf)
            sections.append(section)
            expected_calls.append((prompt, source_pdf))
        notes = ShortNotes(
            lecture_no="",
            subject="Physics",
            title="Five Figures",
            sections=sections,
        )
        actual_calls = []

        def grounded_svg(prompt, source_pdf):
            actual_calls.append((prompt, source_pdf))
            return f"<svg id='{len(actual_calls)}'></svg>"

        with (
            patch.object(image_gen, "enabled", return_value=True),
            patch.object(image_gen.config, "MAX_IMAGES", 4),
            patch.object(image_gen.config, "DIAGRAM_MODE", "svg"),
            patch.object(image_gen, "_one_image") as raster,
            patch("notesbot.svg_gen.try_svg", side_effect=grounded_svg),
        ):
            image_gen.render_visuals(notes)

        raster.assert_not_called()
        self.assertEqual(actual_calls, expected_calls[:4])
        self.assertTrue(all(getattr(section, "_svg", None) for section in sections[:4]))
        self.assertIsNone(getattr(sections[4], "_svg", None))
        self.assertEqual(sections[4].diagram, "Source figure 5.")

    def test_raster_429_retry_budget_stops_after_three_attempts(self):
        attempts = []

        def reject(**_kwargs):
            attempts.append(len(attempts) + 1)
            raise RuntimeError("429 capacity unavailable")

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=reject),
            ),
        )

        with (
            patch.object(gemini_client, "_lite_client", return_value=client),
            patch("time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "429"):
                image_gen._one_image("Source figure", self.SOURCE_PDF)

        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(sleep.call_args_list, [call(4.0), call(8.0)])

    def test_one_argument_render_visuals_delegates_to_unchanged_legacy_renderer(self):
        notes, _section = self._notes()

        with patch.object(
            image_gen, "_render_legacy_visuals", return_value=None
        ) as legacy:
            result = image_gen.render_visuals(notes)

        self.assertIsNone(result)
        legacy.assert_called_once_with(notes)

    def test_partial_manifest_arguments_fail_closed_instead_of_using_legacy_mode(self):
        notes, _section = self._notes()

        with self.assertRaises(VisualFidelityError) as caught:
            image_gen.render_visuals(notes, VisualManifest())

        self.assertEqual(caught.exception.code, "link_invalid")


def _sequence_section(plan: Visual | None, *, order: int = 0) -> Section:
    section = Section(
        heading=f"Topic {order}",
        source_local_page=1,
        source_order=order,
        visual=plan,
    )
    object.__setattr__(
        section,
        "_source_chunk",
        PdfChunk(
            index=0,
            pdf_bytes=b"bounded-source-chunk",
            first_global_page=1,
            page_count=3,
            page_geometries=(),
        ),
    )
    return section


def _valid_sequence_plan(*, page: int = 1, suffix: str = "") -> Visual:
    return Visual(
        kind="core_diagram",
        source_local_page=page,
        exact_components=[f"component{suffix}"],
        exact_labels=[f"Label{suffix}"],
        composition_note=f"Component{suffix} points right",
    )


def test_sequence_visual_jobs_select_first_two_valid_plans_in_teacher_order(
    monkeypatch,
) -> None:
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Bounded visuals",
        sections=[
            _sequence_section(
                Visual(
                    kind="core_diagram",
                    source_local_page=1,
                    exact_components=[],
                    composition_note="Invalid empty component list",
                ),
                order=0,
            ),
            _sequence_section(_valid_sequence_plan(page=2, suffix=" A"), order=1),
            _sequence_section(_valid_sequence_plan(page=1, suffix=" B"), order=2),
            _sequence_section(_valid_sequence_plan(page=3, suffix=" C"), order=3),
        ],
    )
    extracted = []

    def extract(chunk, page, *, budget):
        extracted.append((chunk.index, page, budget))
        return f"page-{page}".encode("ascii")

    monkeypatch.setattr(image_gen, "extract_chunk_page", extract, raising=False)
    monkeypatch.setattr(image_gen.config, "MAX_IMAGES", 99)
    run = GenerationRun(deadline_at=10.0, max_images=99, clock=lambda: 1.0)

    jobs = image_gen._select_sequence_visual_jobs(notes, run)

    assert [job.key for job in jobs] == ["c0-p2-s1-i1", "c0-p1-s2-i2"]
    assert [job.source_page_pdf for job in jobs] == [b"page-2", b"page-1"]
    assert [item[:2] for item in extracted] == [(0, 2), (0, 1)]
    assert all(item[2] is run for item in extracted)
    snapshot = run.snapshot(1)
    assert snapshot["eligible_visuals"] == 3
    assert snapshot["images_omitted"] == 2


def test_sequence_visual_jobs_treat_missing_and_non_core_plans_as_text_only(
    monkeypatch,
) -> None:
    notes = ShortNotes(
        lecture_no="",
        subject="Biology",
        title="Text only",
        sections=[
            _sequence_section(None, order=0),
            _sequence_section(Visual(kind="doodle"), order=1),
        ],
    )
    monkeypatch.setattr(
        image_gen,
        "extract_chunk_page",
        lambda *_a, **_k: pytest.fail("text-only plans must not extract a page"),
        raising=False,
    )
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    assert image_gen._select_sequence_visual_jobs(notes, run) == []
    assert run.snapshot(1)["eligible_visuals"] == 0
    assert run.snapshot(1)["images_omitted"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": "legacy free prompt"},
        {"source_local_page": 0},
        {"exact_components": []},
        {"exact_components": ["component"] * 25},
        {"exact_components": [" "]},
        {"exact_components": ["same", " SAME "]},
        {"exact_components": ["x" * 121]},
        {"exact_labels": [f"L{index}" for index in range(25)]},
        {"exact_labels": ["x" * 81]},
        {"composition_note": ""},
        {"composition_note": "x" * 401},
    ],
)
def test_sequence_visual_jobs_reject_invalid_plan_without_consuming_allowance(
    monkeypatch,
    changes,
) -> None:
    payload = _valid_sequence_plan().model_dump()
    payload.update(changes)
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Invalid plan",
        sections=[_sequence_section(Visual.model_validate(payload))],
    )
    monkeypatch.setattr(
        image_gen,
        "extract_chunk_page",
        lambda *_a, **_k: pytest.fail("invalid plans must not extract a page"),
        raising=False,
    )
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    assert image_gen._select_sequence_visual_jobs(notes, run) == []
    snapshot = run.snapshot(1)
    assert snapshot["eligible_visuals"] == 0
    assert snapshot["image_model_calls"] == 0
    assert snapshot["images_omitted"] == 1


def _image_response(data: bytes | str) -> dict:
    encoded = data if isinstance(data, str) else base64.b64encode(data).decode("ascii")
    return {
        "choices": [
            {
                "message": {
                    "images": [
                        {"image_url": {"url": "data:image/png;base64," + encoded}}
                    ]
                }
            }
        ]
    }


def test_sequence_image_uses_one_exact_source_page_call_and_no_legacy_fallbacks(
    monkeypatch,
) -> None:
    source_page = b"exact-one-page-source-pdf"
    section = _sequence_section(_valid_sequence_plan(page=2))
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="One visual",
        sections=[section],
    )
    requests = []
    closed = []

    def create(**kwargs):
        requests.append(kwargs)
        return _image_response(DiagramModeTests._png_bytes())

    raw_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(image_gen, "enabled", lambda: True)
    monkeypatch.setattr(
        image_gen,
        "extract_chunk_page",
        lambda _chunk, _page, *, budget: source_page,
    )
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: raw_client,
    )
    monkeypatch.setattr(image_gen, "_one_redraw", lambda *_a, **_k: pytest.fail("redraw"))
    monkeypatch.setattr(image_gen, "_verify_redraw", lambda *_a, **_k: pytest.fail("verify"))
    monkeypatch.setattr(
        image_gen,
        "prepare_source_fallback",
        lambda *_a, **_k: pytest.fail("source fallback"),
    )
    monkeypatch.setattr(
        "notesbot.svg_gen.try_svg",
        lambda *_a, **_k: pytest.fail("svg fallback"),
    )
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    image_gen.render_sequence_visuals(notes, run)

    assert len(requests) == 1
    content = requests[0]["messages"][0]["content"]
    assert content[0]["file"]["filename"] == "source-page.pdf"
    assert content[0]["file"]["file_data"] == (
        "data:application/pdf;base64," + base64.b64encode(source_page).decode("ascii")
    )
    assert "exact_components" in content[1]["text"]
    assert "exact_labels" in content[1]["text"]
    assert "legacy free prompt" not in content[1]["text"]
    assert getattr(section, "_image", None)
    assert getattr(section, "_svg", None) is None
    assert section.diagram == ""
    assert closed == [True]
    snapshot = run.snapshot(1)
    assert snapshot["image_model_calls"] == 1
    assert snapshot["images_omitted"] == 0


@pytest.mark.parametrize(
    "failure_case",
    ["provider_exception", "invalid_base64", "invalid_png", "oversized"],
)
def test_sequence_image_failure_is_one_attempt_and_falls_back_to_text_only(
    monkeypatch,
    failure_case,
) -> None:
    section = _sequence_section(_valid_sequence_plan())
    section.diagram = "must be cleared"
    notes = ShortNotes(
        lecture_no="",
        subject="Biology",
        title="Best effort image",
        sections=[section],
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if failure_case == "provider_exception":
            raise RuntimeError("429 private provider detail")
        if failure_case == "invalid_base64":
            return _image_response("%%%")
        if failure_case == "invalid_png":
            return _image_response(b"not-a-png")
        return _image_response(DiagramModeTests._png_bytes())

    raw_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: None,
    )
    monkeypatch.setattr(image_gen, "enabled", lambda: True)
    monkeypatch.setattr(
        image_gen,
        "extract_chunk_page",
        lambda *_a, **_k: b"one-page-pdf",
    )
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: raw_client,
    )
    if failure_case == "oversized":
        monkeypatch.setattr(image_gen.config, "MAX_VISUAL_PNG_BYTES", 8)
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    image_gen.render_sequence_visuals(notes, run)

    assert len(calls) == 1
    assert getattr(section, "_image", None) is None
    assert getattr(section, "_svg", None) is None
    assert section.diagram == ""
    snapshot = run.snapshot(1)
    assert snapshot["image_model_calls"] == 1
    assert snapshot["images_omitted"] == 1


def test_sequence_images_disabled_omits_selected_jobs_without_provider_call(
    monkeypatch,
) -> None:
    section = _sequence_section(_valid_sequence_plan())
    notes = ShortNotes(
        lecture_no="",
        subject="Physics",
        title="Images disabled",
        sections=[section],
    )
    monkeypatch.setattr(image_gen, "enabled", lambda: False)
    monkeypatch.setattr(
        image_gen,
        "extract_chunk_page",
        lambda *_a, **_k: b"one-page-pdf",
    )
    monkeypatch.setattr(
        gemini_client,
        "_build_visual_lite_client",
        lambda: pytest.fail("disabled images must not build a provider client"),
    )
    run = GenerationRun(deadline_at=10.0, max_images=2, clock=lambda: 1.0)

    image_gen.render_sequence_visuals(notes, run)

    snapshot = run.snapshot(1)
    assert snapshot["eligible_visuals"] == 1
    assert snapshot["image_model_calls"] == 0
    assert snapshot["images_omitted"] == 1
    assert getattr(section, "_image", None) is None


if __name__ == "__main__":
    unittest.main()
