"""Automatic diagram cleanup must survive placement, rebuilds and both exports."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw
import pymupdf

from src.image_ai import clean_visuals_for_notes
from src.image_tools import make_white_transparent
from src.visuals.metadata import VisualCandidate, VisualRegistry


def diagram_bytes():
    image = Image.new("RGB", (800, 450), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 80, 650, 320), outline="navy", width=6)
    draw.text((140, 160), "IR / GS-II / 2026", fill="navy", font_size=30)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ImageClient:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def generate_image(self, prompt, path, **kwargs):
        self.calls.append(Path(path))
        if self.fail:
            raise RuntimeError("Image provider unavailable")
        return diagram_bytes()


def registry_for(tmp_path):
    source = tmp_path / "original.png"
    source.write_bytes(diagram_bytes())
    return VisualRegistry(visuals=[VisualCandidate(
        "figure-1", 1, image_path=source, width_px=800, height_px=450,
        insert=True, visual_type="flowchart", caption="Lecture diagram", topic="Core Concepts")])


def test_white_removal_preserves_existing_transparency_and_coloured_lines(tmp_path):
    image = Image.new("RGBA", (4, 1))
    image.putdata([(255, 255, 255, 255), (255, 255, 0, 255),
                   (0, 0, 128, 0), (0, 0, 128, 100)])
    source = tmp_path / "colours.png"
    image.save(source)
    result = make_white_transparent(source)
    with Image.open(result) as output:
        assert [output.getpixel((x, 0))[3] for x in range(4)] == [0, 255, 0, 100]


def test_only_placed_diagrams_are_edited_and_originals_remain_available(tmp_path):
    registry = registry_for(tmp_path)
    original = registry.selected[0].image_path
    registry.visuals.extend([
        VisualCandidate("unused", 2, image_path=original, insert=False),
        VisualCandidate("photo", 3, image_path=original, insert=True, visual_type="photo"),
    ])
    client, stages = ImageClient(), []
    count, warnings = clean_visuals_for_notes(client, registry, tmp_path / "edited", stage_callback=stages.append)
    assert count == 1 and not warnings
    assert client.calls == [original]
    figure = registry.by_id("figure-1")
    assert figure.original_image_path == original and original.read_bytes() == diagram_bytes()
    assert figure.image_path != original
    with Image.open(figure.image_path) as image:
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((100, 80))[3] == 255
    registry.save(tmp_path)
    loaded = VisualRegistry.load(tmp_path).by_id("figure-1")
    assert loaded.image_edit_status == "transparent"
    assert loaded.original_image_path == original
    assert stages == ["Editing diagrams (0 of 1)", "Editing diagrams (1 of 1)"]


def test_image_provider_failure_keeps_the_original_and_reports_it(tmp_path):
    registry = registry_for(tmp_path)
    original = registry.selected[0].image_path
    count, warnings = clean_visuals_for_notes(ImageClient(fail=True), registry, tmp_path / "edited")
    assert count == 0 and "original image was kept" in warnings[0]
    assert registry.selected[0].image_path == original
    assert registry.selected[0].image_edit_status == "failed"


@pytest.mark.parametrize("colour", ["white", "navy"])
def test_blank_or_opaque_generated_images_are_rejected(tmp_path, colour):
    registry = registry_for(tmp_path)
    original = registry.selected[0].image_path
    payload = BytesIO()
    Image.new("RGB", (800, 450), colour).save(payload, format="PNG")
    client = ImageClient()
    client.generate_image = lambda *a, **k: payload.getvalue()
    count, warnings = clean_visuals_for_notes(client, registry, tmp_path / "edited")
    assert count == 0 and warnings
    assert registry.selected[0].image_path == original


@pytest.mark.real_pdf
def test_generation_edits_diagrams_before_word_and_pdf_exports(tmp_path, monkeypatch):
    from src import pipeline
    from src.ai_client import GeminiResponse
    import src.visuals

    registry = registry_for(tmp_path)
    registry.selected[0].topic = "Core Concepts and Definitions"
    source = tmp_path / "lecture.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Core Concepts and Definitions")
        pdf.save(source)

    class NotesClient(ImageClient):
        fallback_notices = []

        def __init__(self, *args, **kwargs):
            super().__init__()

        def generate(self, *args, **kwargs):
            return GeminiResponse(
                text="#LECTURE_TITLE: Lecture\n#H1: Core Concepts and Definitions\n"
                     "- The lecture explains international relations.\n#FIGURE: figure-1\n",
                provider="test", model="test")

    client = NotesClient()
    monkeypatch.setattr(pipeline.pw_access, "check_allowed", lambda *a, **k: True)
    monkeypatch.setattr(pipeline, "GeminiClient", lambda *a, **k: client)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(src.visuals, "prepare_visuals", lambda *a, **k: registry)
    result = pipeline.run_pipeline(
        source, subject="ir", language="en", mode="complete", google_token="test-token", workflow="upsc",
        upsc_output_format="dcn", quality_level="off", generate_error_report=False,
        require_transcript=False, ai_redraw_diagrams=True,
        dcn_options={"render_verify": False})
    assert client.calls, "the image editor was never called during notes generation"
    assert result.pdf_path is not None
    saved = VisualRegistry.load(result.run_dir).by_id("figure-1")
    assert saved.image_edit_status == "transparent"
    with ZipFile(result.docx_path) as archive:
        images = [archive.read(n) for n in archive.namelist() if n.startswith("word/media/")]
    assert any(Image.open(BytesIO(payload)).mode == "RGBA" for payload in images)
    with pymupdf.open(result.pdf_path) as pdf:
        assert any(image[1] > 0 for page in pdf for image in page.get_images(full=True)), (
            "the PDF export flattened the diagram's transparency")
