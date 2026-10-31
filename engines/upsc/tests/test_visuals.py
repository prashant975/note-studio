"""Tests for the lecture-visuals feature (src/visuals + writer/QC integration).

Everything runs offline: PDFs are synthesised with PyMuPDF, diagrams with PIL,
and the classifier is exercised through a fake client. The spec's scenario
matrix (§27) is covered piecewise — no diagrams, one important diagram,
decorative images, repeated diagrams, tiny icons, Hindi text, multiple diagrams
on one slide — plus the placement, writer, QC and end-to-end mock-pipeline
behaviour.
"""
from pathlib import Path
import json
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    VISUAL_MAX_HEIGHT_IN,
    VISUAL_MAX_PER_LECTURE,
    VISUAL_MAX_PER_SLIDE,
    VISUAL_MAX_WIDTH_IN,
    VISUAL_MIN_WIDTH_IN,
)
from src.models import SlideData
from src.visuals import prepare_visuals, select_visuals
from src.visuals.candidate_detector import filter_candidates
from src.visuals.extractor import extract_candidates, hash_distance
from src.visuals.metadata import VisualCandidate, VisualRegistry
from src.visuals.section_mapper import finalize_figure_markers, strip_figure_markers
from src.visuals.visual_classifier import (
    classify_candidates,
    heuristic_classify,
)
from src.upsc_dcn_docx_writer import figure_display_width_in, write_upsc_dcn_docx
from src.qc.visual_qc import render_visual_qc_md, run_visual_qc

EMU_PER_INCH = 914400


# --------------------------------------------------------------------------
# Synthetic inputs
# --------------------------------------------------------------------------
def _diagram_png(path: Path, w: int = 800, h: int = 600) -> Path:
    """A colourful boxes-and-arrows flowchart — unmistakably 'visual content'."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for i, (x, y) in enumerate([(60, 40), (300, 40), (540, 40), (300, 260), (300, 460)]):
        d.rectangle([x, y, x + 180, y + 90],
                    fill=(200 + 10 * i, 220, 255 - 20 * i), outline="black", width=4)
        d.text((x + 20, y + 35), f"Stage {i + 1}", fill="black")
    d.line([390, 130, 390, 260], fill="black", width=5)
    d.line([390, 350, 390, 460], fill="black", width=5)
    d.ellipse([600, 300, 760, 430], fill=(255, 210, 160), outline="blue", width=4)
    img.save(path)
    return path


def _logo_png(path: Path) -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (200, 200), (30, 30, 60))
    d = ImageDraw.Draw(img)
    d.ellipse([20, 20, 180, 180], outline=(255, 200, 60), width=14)
    d.text((70, 85), "PW", fill="white")
    img.save(path)
    return path


def _build_pdf(path: Path, pages: list[dict]) -> Path:
    import fitz

    doc = fitz.open()
    for spec in pages:
        page = doc.new_page(width=612, height=792)
        if spec.get("heading"):
            page.insert_text((40, 50), spec["heading"], fontsize=18)
        for i, line in enumerate(spec.get("lines", [])):
            page.insert_text((40, 100 + 16 * i), line, fontsize=11)
        if spec.get("image"):
            rect = fitz.Rect(*spec.get("image_rect", (60, 150, 560, 560)))
            page.insert_image(rect, filename=str(spec["image"]))
        if spec.get("logo"):
            page.insert_image(fitz.Rect(430, 20, 580, 170), filename=str(spec["logo"]))
        if spec.get("drawings"):
            for k in range(8):
                page.draw_rect(fitz.Rect(80 + 40 * k, 300 + 20 * k, 200 + 40 * k, 380 + 20 * k),
                               color=(0, 0, 0), fill=(0.8, 0.85, 1), width=2)
            page.draw_line(fitz.Point(100, 300), fitz.Point(420, 540),
                           color=(1, 0, 0), width=3)
    doc.save(str(path))
    doc.close()
    return path


def _slides(headings: list[str]) -> list[SlideData]:
    return [SlideData(slide_no=i, heading=h, text=h, cleaned_text=h, source_type="pdf")
            for i, h in enumerate(headings, start=1)]


# --------------------------------------------------------------------------
# Extraction + local filtering
# --------------------------------------------------------------------------
def test_text_only_pages_yield_no_candidates(tmp_path):
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"heading": "Plain Text", "lines": [f"point {i}" for i in range(12)]},
        {"heading": "More Text", "lines": ["a", "b", "c"]},
    ])
    issues: list[str] = []
    found = extract_candidates(pdf, _slides(["Plain Text", "More Text"]), tmp_path, issues)
    assert found == []
    assert issues == []


def test_embedded_diagram_becomes_a_candidate(tmp_path):
    img = _diagram_png(tmp_path / "flow.png")
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"heading": "Citizen Charter", "image": img},
    ])
    found = extract_candidates(pdf, _slides(["Citizen Charter"]), tmp_path, [])
    assert len(found) == 1
    c = found[0]
    assert c.slide_no == 1
    assert c.origin == "embedded_image"
    assert c.slide_heading == "Citizen Charter"
    assert c.width_px > 400 and c.height_px > 300, "crop must be high resolution"
    assert c.phash


def test_full_page_image_is_recognised_as_the_slide_itself(tmp_path):
    # Portrait image in a page-filling rect (fitz keeps the aspect ratio, so a
    # wide image could never cover enough of a portrait page to qualify).
    img = _diagram_png(tmp_path / "board.png", 1000, 1200)
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"image": img, "image_rect": (6, 36, 606, 756)},
    ])
    found = extract_candidates(pdf, _slides([""]), tmp_path, [])
    assert len(found) == 1
    assert found[0].origin == "full_slide"


def test_shape_drawn_diagram_is_captured_as_a_region(tmp_path):
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"heading": "Process Flow", "drawings": True},
    ])
    found = extract_candidates(pdf, _slides(["Process Flow"]), tmp_path, [])
    assert len(found) == 1
    assert found[0].origin == "region_render"


def test_tiny_icons_are_dropped(tmp_path):
    img = _diagram_png(tmp_path / "icon_src.png", 400, 300)
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        # 30x24pt on a 612x792 page: well under the area floor.
        {"heading": "Iconography", "image": img, "image_rect": (60, 150, 90, 174)},
    ])
    found = extract_candidates(pdf, _slides(["Iconography"]), tmp_path, [])
    assert found == []


def test_recurring_logo_is_filtered_as_page_furniture(tmp_path):
    logo = _logo_png(tmp_path / "logo.png")
    diagram = _diagram_png(tmp_path / "flow.png")
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"heading": "A", "logo": logo},
        {"heading": "B", "logo": logo},
        {"heading": "C", "logo": logo},
        {"heading": "D", "logo": logo, "image": diagram},
    ])
    found = extract_candidates(pdf, _slides(["A", "B", "C", "D"]), tmp_path, [])
    kept, notes = filter_candidates(found)
    # The logo appeared on 4 slides -> furniture; the diagram survives alone.
    assert len(kept) == 1
    assert kept[0].slide_no == 4
    assert any("furniture" in n or "repeats" in n for n in notes)


def test_repeated_diagram_is_kept_once(tmp_path):
    diagram = _diagram_png(tmp_path / "flow.png")
    pdf = _build_pdf(tmp_path / "deck.pdf", [
        {"heading": "First Showing", "image": diagram},
        {"heading": "Second Showing", "image": diagram},
    ])
    found = extract_candidates(pdf, _slides(["First Showing", "Second Showing"]), tmp_path, [])
    kept, notes = filter_candidates(found)
    assert len(kept) == 1
    assert any("duplicate" in n for n in notes)


def test_extraction_failure_is_isolated_not_fatal(tmp_path):
    issues: list[str] = []
    # A file that is not a PDF at all: fitz raises, extract returns nothing.
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(Exception):
        extract_candidates(bad, [], tmp_path, issues)
    # prepare_visuals swallows exactly this and records it instead.
    registry = prepare_visuals(bad, [], tmp_path, client=None)
    assert registry.visuals == []
    assert any("Visual preparation failed" in i for i in registry.issues)
    assert (tmp_path / "visual_registry.json").exists()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, text: str):
        self.text = text


class _FakeClient:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def generate(self, prompt, image_paths, **kwargs):
        self.calls += 1
        return _FakeResp(self.payload)


def _candidate(i: int, slide: int = 1, **kw) -> VisualCandidate:
    defaults = dict(visual_id=f"visual_{slide:03d}_{i:02d}", slide_no=slide,
                    image_path=None, width_px=800, height_px=600,
                    origin="embedded_image", phash=f"{i:016x}")
    defaults.update(kw)
    return VisualCandidate(**defaults)


def test_classifier_applies_json_verdicts():
    cands = [_candidate(1), _candidate(2)]
    payload = json.dumps({"visuals": [
        {"index": 1, "educational": True, "relevance": 0.91, "visual_type": "flowchart",
         "best_as_text_table": False, "topic": "Citizen Charter",
         "caption": "Citizen Charter accountability mechanism",
         "description": "Service standards flow"},
        {"index": 2, "educational": False, "relevance": 0.8, "visual_type": "decorative",
         "best_as_text_table": False, "topic": "", "caption": "", "description": "logo"},
    ]})
    status, issues = classify_candidates(_FakeClient(payload), cands)
    assert status == "completed" and not issues
    assert cands[0].relevance == 0.91 and cands[0].visual_type == "flowchart"
    assert cands[0].caption.startswith("Citizen Charter")
    # "educational: false" caps relevance below every include threshold.
    assert cands[1].relevance <= 0.29


def test_classifier_failure_degrades_without_raising():
    cands = [_candidate(1)]
    status, issues = classify_candidates(_FakeClient("not json at all"), cands)
    assert status == "failed"
    assert issues and not cands[0].classified


def test_unclassified_candidates_are_included():
    """Over-include on purpose: an unclassified candidate already survived the
    local furniture/duplicate filter, and reviewers would rather delete a spare
    image than round-trip to the deck for a missing one."""
    registry = VisualRegistry(visuals=[_candidate(1)])
    select_visuals(registry)
    assert [v.visual_id for v in registry.selected] == ["visual_001_01"]
    assert "included" in registry.visuals[0].reason


def test_selection_thresholds_and_types():
    registry = VisualRegistry(visuals=[
        _candidate(1, classified=True, relevance=0.85, visual_type="map"),
        _candidate(2, classified=True, relevance=0.45, visual_type="diagram"),
        _candidate(3, classified=True, relevance=0.9, visual_type="decorative"),
        _candidate(4, classified=True, relevance=0.9, visual_type="table",
                   prefer_text_table=True),
        _candidate(5, classified=True, relevance=0.30, visual_type="diagram"),
    ])
    select_visuals(registry)
    ids = [v.visual_id for v in registry.selected]
    # 0.45 sits ON the (deliberately low) threshold and stays in — the policy
    # is over-inclusion; only clear noise (0.30) is dropped on relevance.
    assert ids == ["visual_001_01", "visual_001_02"]
    reasons = {v.visual_id: v.reason for v in registry.visuals}
    assert "decorative" in reasons["visual_001_03"]
    assert "document table" in reasons["visual_001_04"]
    assert "below threshold" in reasons["visual_001_05"]


def test_per_slide_and_per_lecture_caps():
    crowded = [_candidate(i, slide=1, classified=True, relevance=0.9 - i * 0.01,
                          visual_type="diagram") for i in range(1, 5)]
    spread = [_candidate(1, slide=s, classified=True, relevance=0.8,
                         visual_type="diagram") for s in range(2, 20)]
    registry = VisualRegistry(visuals=crowded + spread)
    select_visuals(registry)
    per_slide = [v for v in registry.selected if v.slide_no == 1]
    assert len(per_slide) <= VISUAL_MAX_PER_SLIDE
    assert len(registry.selected) <= VISUAL_MAX_PER_LECTURE


def test_heuristic_classifier_is_conservative_about_embedded_images():
    a = _candidate(1, origin="region_render")
    b = _candidate(2, origin="embedded_image")
    heuristic_classify([a, b])
    assert a.relevance > b.relevance


# --------------------------------------------------------------------------
# Placement (section mapper)
# --------------------------------------------------------------------------
_NOTES = """#LECTURE_TITLE: L36_DCN36_Indian Physical Geography:

#H1: Citizen Charter

- Citizen Charters define service standards and accountability.
- The mechanism links citizens to time-bound service delivery.

#FIGURE: visual_007_01

#H1: Social Audit

- Social audit verifies public expenditure with the community.

#H1: Unrelated Topic

- Nothing visual here.
"""


def _registry_for_placement() -> VisualRegistry:
    return VisualRegistry(visuals=[
        _candidate(1, slide=7, classified=True, relevance=0.91, visual_type="flowchart",
                   insert=True, topic="Citizen Charter",
                   caption="Citizen Charter accountability mechanism",
                   slide_heading="Citizen Charter", phash="00ff00ff00ff00ff"),
        _candidate(1, slide=11, classified=True, relevance=0.8, visual_type="diagram",
                   insert=True, topic="Social Audit process",
                   caption="Stages of a social audit",
                   slide_heading="Social Audit", phash="ff00ff00ff00ff00"),
    ])


def test_model_placed_marker_is_validated_and_recorded():
    registry = _registry_for_placement()
    text, report = finalize_figure_markers(_NOTES, registry)
    assert report["placed_by_model"] == 1
    v = registry.by_id("visual_007_01")
    assert v.placed_by == "model" and v.section == "Citizen Charter"


def test_forgotten_figure_is_placed_at_the_end_of_its_matching_section():
    registry = _registry_for_placement()
    text, report = finalize_figure_markers(_NOTES, registry)
    assert report["placed_by_mapping"] == 1
    # The slide-11 figure lands inside the Social Audit section, before the
    # next heading — not at the end of the document.
    social = text.index("#H1: Social Audit")
    unrelated = text.index("#H1: Unrelated Topic")
    marker = text.index("#FIGURE: visual_011_01")
    assert social < marker < unrelated
    assert registry.by_id("visual_011_01").placed_by == "section_match"


def test_unknown_and_duplicate_markers_are_dropped():
    registry = _registry_for_placement()
    noisy = _NOTES + "\n#FIGURE: visual_007_01\n#FIGURE: visual_999_99\n"
    text, report = finalize_figure_markers(noisy, registry)
    assert report["dropped_duplicate"] == ["visual_007_01"]
    assert report["dropped_unknown"] == ["visual_999_99"]
    assert text.count("#FIGURE: visual_007_01") == 1
    assert "visual_999_99" not in text


def test_marker_inside_a_table_is_moved_out():
    registry = _registry_for_placement()
    noisy = _NOTES.replace(
        "#H1: Social Audit\n",
        "#H1: Social Audit\n\n#TABLE_START\nA | B\n#FIGURE: visual_011_01\nC | D\n#TABLE_END\n",
    )
    text, report = finalize_figure_markers(noisy, registry)
    assert "visual_011_01" in report["moved_out_of_blocks"]
    # Re-placed by mapping, outside the table.
    table_end = text.index("#TABLE_END")
    marker = text.index("#FIGURE: visual_011_01")
    assert marker > table_end


def test_no_matching_section_downgrades_instead_of_forcing():
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=3, classified=True, relevance=0.9, visual_type="map",
                   insert=True, topic="Quantum Chromodynamics",
                   slide_heading="Something Entirely Different"),
    ])
    text, report = finalize_figure_markers(_NOTES, registry)
    assert report["not_placed"] == ["visual_003_01"]
    v = registry.by_id("visual_003_01")
    assert not v.insert and "no matching section" in v.reason
    assert "#FIGURE: visual_003_01" not in text


def test_strip_figure_markers_removes_every_marker():
    assert "#FIGURE" not in strip_figure_markers(_NOTES)
    assert "#H1: Citizen Charter" in strip_figure_markers(_NOTES)


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------
def _writer_visuals(tmp_path, w=800, h=600, caption="Citizen Charter accountability flow"):
    img = _diagram_png(tmp_path / "fig.png", w, h)
    return {"visual_007_01": {"path": str(img), "caption": caption,
                              "width_px": w, "height_px": h,
                              "visual_type": "flowchart", "slide_no": 7}}


_WRITER_NOTES = """#LECTURE_TITLE: L07_DCN07_Governance:

#H1: Citizen Charter

- Citizen Charters define service standards and accountability.

#FIGURE: visual_007_01

#H1: Next Topic

- More content follows the figure.
"""


def _docx_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8", "replace")


def test_writer_embeds_the_figure_with_caption(tmp_path):
    out, warnings = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx", visuals=_writer_visuals(tmp_path))
    xml = _docx_xml(out)
    assert "<pic:pic" in xml, "figure image not embedded"
    assert "Figure: Citizen Charter accountability flow" in re.sub(r"<[^>]+>", "", xml)
    assert "#FIGURE" not in re.sub(r"<[^>]+>", "", xml), "marker leaked as text"
    with zipfile.ZipFile(out) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert media, "no media part saved"


def test_figure_keeps_aspect_ratio_and_fits_the_content_column(tmp_path):
    out, _ = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx", visuals=_writer_visuals(tmp_path))
    xml = _docx_xml(out)
    extent = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml)
    assert extent
    cx, cy = int(extent.group(1)), int(extent.group(2))
    assert cx <= int(VISUAL_MAX_WIDTH_IN * EMU_PER_INCH) + 1000
    assert abs((cx / cy) - (800 / 600)) < 0.02, "aspect ratio distorted"


def test_tall_figure_is_height_capped_proportionally(tmp_path):
    out, _ = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx",
        visuals=_writer_visuals(tmp_path, w=600, h=2400))
    xml = _docx_xml(out)
    extent = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml)
    cx, cy = int(extent.group(1)), int(extent.group(2))
    assert cy <= int(VISUAL_MAX_HEIGHT_IN * EMU_PER_INCH) + 1000
    assert abs((cx / cy) - (600 / 2400)) < 0.02, "capping stretched the figure"


def test_unknown_figure_id_warns_and_skips(tmp_path):
    out, warnings = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx", visuals={})
    assert any("visual_007_01" in w for w in warnings)
    xml_text = re.sub(r"<[^>]+>", "", _docx_xml(out))
    assert "#FIGURE" not in xml_text
    assert "Next Topic" in xml_text, "notes after the figure must survive"


def test_missing_image_file_warns_and_skips(tmp_path):
    visuals = {"visual_007_01": {"path": str(tmp_path / "gone.png"),
                                 "caption": "x", "width_px": 800, "height_px": 600}}
    out, warnings = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx", visuals=visuals)
    assert any("missing" in w for w in warnings)
    assert "<pic:pic" not in _docx_xml(out) or "word/media/" not in str(
        zipfile.ZipFile(out).namelist())


def test_hindi_caption_uses_the_hindi_prefix(tmp_path):
    visuals = _writer_visuals(tmp_path, caption="भारत की भू-आकृतिक इकाइयाँ")
    out, _ = write_upsc_dcn_docx(
        _WRITER_NOTES, tmp_path / "notes.docx", visuals=visuals, language_code="hi")
    text = re.sub(r"<[^>]+>", "", _docx_xml(out))
    assert "चित्र:" in text
    assert "भारत की भू-आकृतिक इकाइयाँ" in text


def test_figure_display_width_bounds():
    assert figure_display_width_in(0) <= VISUAL_MAX_WIDTH_IN
    assert figure_display_width_in(200) == VISUAL_MIN_WIDTH_IN
    assert figure_display_width_in(6000) == VISUAL_MAX_WIDTH_IN
    assert VISUAL_MIN_WIDTH_IN <= figure_display_width_in(640) <= VISUAL_MAX_WIDTH_IN


# --------------------------------------------------------------------------
# Visual QC
# --------------------------------------------------------------------------
def test_visual_qc_passes_a_clean_run(tmp_path):
    registry = _registry_for_placement()
    notes, _ = finalize_figure_markers(_NOTES, registry)
    visuals = _writer_visuals(tmp_path)
    visuals["visual_011_01"] = dict(visuals["visual_007_01"])
    out, _ = write_upsc_dcn_docx(notes, tmp_path / "notes.docx", visuals={
        "visual_007_01": visuals["visual_007_01"],
        "visual_011_01": {**visuals["visual_011_01"],
                          "path": str(_diagram_png(tmp_path / "fig2.png", 900, 500))},
    })
    result = run_visual_qc(registry, notes, out)
    assert result["inserted"] == 2 and result["selected"] == 2
    assert result["status"] == "completed"
    assert any(line.startswith("✓") for line in result["checks"])


def test_visual_qc_flags_a_missing_selected_figure(tmp_path):
    registry = _registry_for_placement()
    # Placement never ran: the notes carry only the model's one marker.
    result = run_visual_qc(registry, _NOTES, None)
    assert result["status"] == "completed_with_warnings"
    assert any("NOT in the final notes" in p for p in result["problems"])


def test_visual_qc_flags_duplicates_and_low_resolution():
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=7, classified=True, relevance=0.9, visual_type="flowchart",
                   insert=True, width_px=200, height_px=150, section="Citizen Charter",
                   placed_by="model", caption="ok"),
    ])
    notes = "#H1: Citizen Charter\n\n#FIGURE: visual_007_01\n\n#FIGURE: visual_007_01\n"
    result = run_visual_qc(registry, notes, None)
    assert any("appears 2 times" in p for p in result["problems"])
    assert any("DPI" in p for p in result["problems"])


def test_visual_qc_reports_no_visuals_honestly():
    result = run_visual_qc(VisualRegistry(), "#H1: A\n- b\n", None)
    assert result["status"] == "no_visuals"
    md = render_visual_qc_md(result)
    assert "Visual QC" in md


def test_visual_qc_quotes_exclusion_reasons():
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=11, classified=True, relevance=0.5, visual_type="diagram",
                   insert=False, reason="relevance 0.50 below threshold 0.60"),
    ])
    result = run_visual_qc(registry, "#H1: A\n", None)
    assert any("below threshold" in c for c in result["checks"])


# --------------------------------------------------------------------------
# Prompt integration
# --------------------------------------------------------------------------
def test_dcn_prompt_carries_visuals_and_figure_marker_contract():
    from src.prompt_builder import build_upsc_dcn_prompt

    visuals = [{"visual_id": "visual_007_01", "slide": 7, "visual_type": "flowchart",
                "topic": "Citizen Charter", "description": "Accountability flow"}]
    with_figs = build_upsc_dcn_prompt(subject="polity", slides=_slides(["A"]),
                                      transcript="text", visuals=visuals)
    without = build_upsc_dcn_prompt(subject="polity", slides=_slides(["A"]),
                                    transcript="text")
    assert "LECTURE VISUALS" in with_figs and "visual_007_01" in with_figs
    assert "#FIGURE:" in with_figs
    assert "LECTURE VISUALS" not in without
    assert "#FIGURE:" not in without, "the marker must not be offered without visuals"


def test_chunk_and_merge_prompts_carry_the_figure_rules():
    from src.prompt_builder import build_dcn_chunk_prompt, build_dcn_merge_prompt

    visuals = [{"visual_id": "visual_007_01", "slide": 7, "visual_type": "flowchart",
                "topic": "Citizen Charter", "description": ""}]
    chunk = build_dcn_chunk_prompt(subject="polity", depth="detailed", chunk_index=1,
                                   chunk_total=2, transcript_chunk="text",
                                   slides=_slides(["A"]), visuals=visuals)
    assert "FOR THIS PART" in chunk and "visual_007_01" in chunk
    merge = build_dcn_merge_prompt(subject="polity", depth="detailed", parts=["a", "b"],
                                   lecture_title="T", figures=True)
    assert "#FIGURE" in merge
    merge_plain = build_dcn_merge_prompt(subject="polity", depth="detailed",
                                         parts=["a"], lecture_title="T")
    assert "#FIGURE" not in merge_plain


# --------------------------------------------------------------------------
# Registry round-trip
# --------------------------------------------------------------------------
def test_registry_round_trip(tmp_path):
    registry = _registry_for_placement()
    registry.issues.append("note")
    registry.classifier_status = "completed"
    registry.save(tmp_path)
    loaded = VisualRegistry.load(tmp_path)
    assert loaded is not None
    assert [v.visual_id for v in loaded.visuals] == [v.visual_id for v in registry.visuals]
    assert loaded.by_id("visual_007_01").topic == "Citizen Charter"
    assert loaded.classifier_status == "completed"
    assert loaded.issues == ["note"]


def test_hash_distance_defensive():
    assert hash_distance("", "abc") == 64
    assert hash_distance("f" * 16, "f" * 16) == 0


# --------------------------------------------------------------------------
# End-to-end: mock DCN pipeline run with a diagram-bearing deck
# --------------------------------------------------------------------------
def test_pipeline_inserts_the_diagram_end_to_end(tmp_path):
    """Slides in → figure in the DOCX out, through the real pipeline (mock AI).

    The diagram page's heading matches an #H1 the mock notes are known to
    contain, so the section-mapper fallback places it even though the mock
    model emits no #FIGURE markers itself.
    """
    from src import pipeline
    from src.pipeline import run_pipeline

    diagram = _diagram_png(tmp_path / "board.png", 1000, 1200)
    deck = _build_pdf(tmp_path / "lecture.pdf", [
        {"heading": "Core Concepts and Definitions", "image": diagram,
         "image_rect": (6, 66, 606, 786)},
        {"heading": "Plain Slide", "lines": ["only text here"] * 6},
    ])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "export_docx_to_pdf", lambda docx, out_dir: (None, ""))
        result = run_pipeline(
            deck, subject="environment", language="en", mode="complete",
            google_token="", allow_mock=True, workflow="upsc", area="environment",
            upsc_output_format="dcn", quality_level="off",
            dcn_options={"render_verify": False},
            generate_error_report=False,
            require_transcript=False,      # slides-only fixture, like the CLI's --no-transcript
        )

    meta = result.metadata
    assert meta["visuals_found"] >= 1
    assert meta["visuals_selected"] >= 1
    assert meta["visuals_inserted"] >= 1, "the diagram never reached the document"
    assert (result.run_dir / "visual_registry.json").exists()
    assert (result.run_dir / "visual_qc.json").exists()

    xml = _docx_xml(result.docx_path)
    assert "<pic:pic" in xml, "no figure embedded in the student DOCX"
    text = re.sub(r"<[^>]+>", "", xml)
    assert "#FIGURE" not in text, "figure marker leaked into the student DOCX"

    # The figure sits in its matching section, and the registry says who put
    # it there.
    registry = VisualRegistry.load(result.run_dir)
    inserted = [v for v in registry.selected if v.section]
    assert inserted and inserted[0].section == "Core Concepts and Definitions"


def test_pipeline_without_diagrams_changes_nothing(tmp_path):
    from src import pipeline
    from src.pipeline import run_pipeline

    deck = _build_pdf(tmp_path / "lecture.pdf", [
        {"heading": "Core Concepts and Definitions", "lines": ["text"] * 8},
        {"heading": "Plain Slide", "lines": ["only text here"] * 6},
    ])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "export_docx_to_pdf", lambda docx, out_dir: (None, ""))
        result = run_pipeline(
            deck, subject="environment", language="en", mode="complete",
            google_token="", allow_mock=True, workflow="upsc", area="environment",
            upsc_output_format="dcn", quality_level="off",
            dcn_options={"render_verify": False},
            generate_error_report=False,
            require_transcript=False,
        )
    assert result.metadata["visuals_found"] == 0
    assert result.metadata["visuals_inserted"] == 0
    assert "<pic:pic" not in _docx_xml(result.docx_path)


def test_frontend_title_is_upsc_notes_automation():
    """The visible app heading changed; the proxy identity must NOT change
    (whitelisting depends on it — see README)."""
    app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert '<div class="app-title">UPSC Notes Automation</div>' in app_source
    assert "Handwritten Notes Automation" not in app_source

    import pw_access

    assert pw_access.APP_NAME == "Handwritten Notes Automation"


# ---------------------------------------------------------------------------
# An unplaceable figure says which of the two things went wrong
# ---------------------------------------------------------------------------
# Both outcomes end with the diagram left out of the document, and they have
# opposite fixes. "No matching section" means the lecture never discussed what
# the diagram shows. "Never classified" means nothing was compared at all —
# the visual has no topic, caption or slide heading, so there was nothing to
# match a section AGAINST.
#
# The second was reported as the first, and on a PDF lecture with no slide
# headings it is the case that fires for EVERY visual: 13 candidates found,
# 13 selected, 0 inserted, and a reason that blamed the notes.

#: Notes with NO figure marker in them, so every visual below has to go
#: through the fallback that maps a figure onto a section — which is the code
#: these tests are about. `_NOTES` already carries a model-placed marker for
#: visual_007_01, so reusing it here would test the model-placed branch instead.
_NOTES_WITHOUT_FIGURES = """#LECTURE_TITLE: L36_DCN36_Governance:

#H1: Citizen Charter

- Citizen Charters define service standards and accountability.

#H1: Social Audit

- Social audit verifies public expenditure with the community.
"""


def test_a_figure_whose_topic_matches_nothing_is_left_out_and_says_so():
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=7, classified=True, relevance=0.9, visual_type="diagram",
                   insert=True, topic="Monetary policy transmission",
                   caption="Repo rate to lending rate",
                   slide_heading="Monetary Policy", phash="00ff00ff00ff00ff"),
    ])
    _text, report = finalize_figure_markers(_NOTES_WITHOUT_FIGURES, registry)
    assert report["not_placed"] == ["visual_007_01"]
    assert report["unclassified"] == [], (
        "this visual WAS classified; it simply matched no section")
    visual = registry.by_id("visual_007_01")
    assert visual.insert is False
    assert "no matching section found" in visual.reason


def test_a_figure_with_nothing_to_match_on_blames_the_classifier_not_the_notes():
    """The real case: a PDF lecture whose slides carry no headings, run without
    an AI classifier. topic, caption and slide_heading are all empty, so the
    comparison never happens — and saying "no matching section found" told the
    reader their notes were at fault."""
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=7, classified=True, relevance=0.7, visual_type="diagram",
                   insert=True, topic="", caption="", slide_heading="",
                   phash="00ff00ff00ff00ff"),
    ])
    _text, report = finalize_figure_markers(_NOTES_WITHOUT_FIGURES, registry)
    assert report["not_placed"] == ["visual_007_01"]
    assert report["unclassified"] == ["visual_007_01"]
    reason = registry.by_id("visual_007_01").reason
    assert "never classified" in reason, reason
    assert "no matching section found" not in reason, (
        "the classifier's failure must not be reported as the notes' failure")


def test_an_unmatchable_figure_is_never_forced_into_a_section():
    """A diagram on the wrong topic is worse than a diagram missing."""
    registry = VisualRegistry(visuals=[
        _candidate(1, slide=7, classified=True, relevance=0.99, visual_type="diagram",
                   insert=True, topic="", caption="", slide_heading="",
                   phash="00ff00ff00ff00ff"),
    ])
    text, _report = finalize_figure_markers(_NOTES_WITHOUT_FIGURES, registry)
    assert "#FIGURE: visual_007_01" not in text
    assert registry.by_id("visual_007_01").section == ""


def test_the_run_records_how_many_diagrams_were_chosen_before_placement(tmp_path):
    """`visuals_selected` is measured AFTER the section mapper has deselected
    everything it could not place.

    So a lecture where 13 diagrams were chosen and none could be placed reports
    `visuals_selected: 0` — identical to a lecture that had no diagrams at all.
    The results card needs to tell those apart to say "none of the 13 slide
    diagrams could be placed", which is the whole point of the notice.
    """
    from src import pipeline
    from src.pipeline import run_pipeline

    diagram = _diagram_png(tmp_path / "board.png", 1000, 1200)
    deck = _build_pdf(tmp_path / "lecture.pdf", [
        # A heading that matches nothing the mock notes contain, so every
        # visual survives selection and then fails placement.
        {"heading": "Quantum Chromodynamics Renormalisation", "image": diagram,
         "image_rect": (6, 66, 606, 786)},
    ])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "export_docx_to_pdf", lambda docx, out_dir: (None, ""))
        result = run_pipeline(
            deck, subject="environment", language="en", mode="complete",
            google_token="", allow_mock=True, workflow="upsc", area="environment",
            upsc_output_format="dcn", quality_level="off",
            dcn_options={"render_verify": False},
            generate_error_report=False, require_transcript=False,
        )

    meta = result.metadata
    assert "visuals_selected_for_placement" in meta, (
        "without this the card cannot distinguish 'placed none of 13' from "
        "'the lecture had no diagrams'")
    assert meta["visuals_selected_for_placement"] >= meta["visuals_selected"]
    if meta["visuals_found"]:
        assert meta["visuals_selected_for_placement"] >= 1


def test_the_pre_placement_count_exists_on_a_run_with_no_slides_at_all():
    """It is set inside two conditionals. A name bound only on the branch that
    runs is a NameError on the run that takes the other one — which is exactly
    how the error-report verification pass stayed broken for weeks."""
    from src.pipeline import run_pipeline

    result = run_pipeline(
        None, subject="", language="en", mode="complete", allow_mock=True,
        workflow="upsc", note_type="dcn", upsc_output_format="dcn",
        quality_level="off", generate_error_report=False,
        transcript_text_override=(
            "Namaskar students, aaj hum Indian Polity ke andar Fundamental "
            "Rights aur Article 21 par baat karenge. " * 40),
    )
    assert result.metadata["visuals_selected_for_placement"] == 0
    assert not [w for w in result.warnings if "not defined" in w]
