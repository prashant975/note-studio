"""Regression tests for the academic-relevance image gate.

The notes used to take a picture whenever a slide had one. These lock the new
behaviour: the default is NO image, question/decorative/duplicate/low-value
pictures are rejected, and rejection happens BEFORE anything expensive runs.

Test images are drawn here rather than shipped, so the suite stays standalone.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.docx_writer import write_notes_docx
from src.image_filter import ImageGate, average_hash, fit_image_size, hamming
from src.models import SlideData
from src.style_config import IMAGE_MAX_WIDTH_IN


# --------------------------------------------------------------------------
# Fixtures: synthetic slide pictures
# --------------------------------------------------------------------------
def _graph(path: Path, size=(800, 600)) -> Path:
    """A plotted curve with axes — the kind of figure that must be KEPT."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle([20, 20, w - 20, h - 20], outline="black", width=5)
    d.line([60, h - 60, w - 40, h - 60], fill="black", width=7)     # x axis
    d.line([60, 40, 60, h - 60], fill="black", width=7)             # y axis
    prev = None
    for i in range(0, w - 120):
        x = 60 + i
        import math
        y = int((h - 60) - (h * 0.35) * (0.5 + 0.5 * math.sin(i / 60.0)))
        if prev:
            d.line([prev, (x, y)], fill="blue", width=6)
        prev = (x, y)
    for t in range(80, w - 60, 90):                                  # ticks
        d.line([t, h - 70, t, h - 50], fill="black", width=4)
    img.save(path)
    return path


def _ray_diagram(path: Path, size=(700, 500)) -> Path:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    w, h = size
    d.line([40, h // 2, w - 40, h // 2], fill="black", width=6)      # principal axis
    d.line([w // 2, 40, w // 2, h - 40], fill="black", width=8)      # mirror
    d.line([60, 120, w // 2, h // 2], fill="red", width=6)           # incident ray
    d.line([w // 2, h // 2, w - 60, 120], fill="red", width=6)       # reflected ray
    d.ellipse([100, 200, 180, 280], outline="black", width=5)
    img.save(path)
    return path


def _text_screenshot(path: Path, size=(800, 600)) -> Path:
    """Evenly spaced thin lines of 'text' and no strokes — must be REJECTED."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    w, _ = size
    widths = [0.46, 0.38, 0.44, 0.30, 0.42, 0.36, 0.48, 0.33,
              0.45, 0.39, 0.41, 0.35, 0.47, 0.32, 0.43, 0.37,
              0.44, 0.31, 0.46, 0.34, 0.40, 0.36, 0.45, 0.38]
    y = 20
    for frac in widths:
        d.rectangle([40, y, 40 + int(w * frac), y + 8], fill="black")
        y += 24
    img.save(path)
    return path


def _icon(path: Path) -> Path:
    img = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(img).ellipse([8, 8, 56, 56], outline="black", width=4)
    img.save(path)
    return path


def _banner(path: Path) -> Path:
    img = Image.new("RGB", (1400, 90), "white")
    ImageDraw.Draw(img).rectangle([10, 30, 1390, 60], fill="black")
    img.save(path)
    return path


def _blank(path: Path) -> Path:
    Image.new("RGB", (600, 400), "white").save(path)
    return path


def _slide(no: int, text: str, image: Path | None, heading: str = "") -> SlideData:
    return SlideData(slide_no=no, heading=heading, text=text, image_path=image)


# --------------------------------------------------------------------------
# Slide-text screening (stage 1 — runs before any pixel or API work)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Q.5 A body moves with velocity v. Find the value of acceleration.\n(a) 2 m/s (b) 4 m/s (c) 6 m/s (d) 8 m/s",
    "MCQ-12: choose the correct option for the given ray diagram",
    "ASQ-51 then the value of sin 2A is:",
    "DPP 03 — practice questions on refraction",
    "Illustration 4: A convex lens forms an image. Sol. Using the lens formula",
    "Try yourself: draw the free body diagram",
    "Home work: exercise 7.2",
])
def test_question_slides_are_rejected_even_when_they_contain_a_diagram(tmp_path, text):
    gate = ImageGate()
    slide = _slide(1, text, _ray_diagram(tmp_path / "q.png"))
    decision = gate.screen_slide(slide)
    assert not decision.accept
    assert "question" in decision.reason.lower() or "mcq" in decision.reason.lower()


@pytest.mark.parametrize("text", [
    "Thank you for joining the class!",
    "Scan the QR code to download the PW app",
    "Follow me on Instagram @physicswallah",
    "Topics to be covered in this lecture",
])
def test_decorative_and_promotional_slides_are_rejected(tmp_path, text):
    gate = ImageGate()
    slide = _slide(2, text, _graph(tmp_path / "g.png"))
    assert not gate.screen_slide(slide).accept


@pytest.mark.parametrize("text", [
    # Bare "exercise" is biology prose; only "Exercise 7.1" is a problem set.
    "Effect of exercise on heart rate\nGraph: heart rate vs exercise intensity",
    # A lettered CASE ANALYSIS line is long; an answer option ("(a) 10 cm") is short.
    "Images formed by concave mirror\n(a) Object beyond C - real, inverted, diminished image formed",
    # The word "question" in teaching prose is not a question slide.
    "This raises an important question about the stability of orbits",
    # "solution" mid-prose is chemistry, not a worked answer.
    "Add dilute HCl to the solution. Observe the gas evolved with a diagram",
])
def test_teaching_slides_that_merely_mention_risky_words_pass(tmp_path, text):
    gate = ImageGate()
    slide = _slide(4, text, _graph(tmp_path / "g.png"))
    assert gate.screen_slide(slide).accept


def test_a_real_concept_slide_is_allowed_through(tmp_path):
    gate = ImageGate()
    slide = _slide(3, "Ray diagram for image formation by a plane mirror. "
                      "The incident ray and the normal are shown.",
                   _ray_diagram(tmp_path / "ray.png"), heading="Plane Mirror")
    decision = gate.screen_slide(slide)
    assert decision.accept
    assert decision.category == "ray_diagram"


def test_screening_is_cached_so_a_slide_is_classified_once(tmp_path):
    gate = ImageGate()
    slide = _slide(4, "Graph of sin theta against theta.", _graph(tmp_path / "g2.png"))
    first = gate.screen_slide(slide)
    assert gate.screen_slide(slide) is first


# --------------------------------------------------------------------------
# Pixel screening (stage 2)
# --------------------------------------------------------------------------
def test_a_graph_and_a_ray_diagram_survive_the_pixel_screen(tmp_path):
    gate = ImageGate()
    assert gate.screen_pixels(_graph(tmp_path / "g.png")).accept
    assert gate.screen_pixels(_ray_diagram(tmp_path / "r.png")).accept


def test_a_screenshot_of_text_is_rejected(tmp_path):
    gate = ImageGate()
    decision = gate.screen_pixels(_text_screenshot(tmp_path / "t.png"))
    assert not decision.accept
    assert "text" in decision.reason


def test_a_formula_slide_that_also_carries_a_figure_is_kept(tmp_path):
    """The lecture case: handwritten working with one real diagram beside it.

    Rejecting the whole slide would throw away the 3-4-5 triangle the notes ask
    for, so an irregular ink band (something that is not a line of type) has to
    override the text-screenshot verdict.
    """
    path = tmp_path / "mixed.png"
    img = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(img)
    y = 20
    for frac in (0.40, 0.34, 0.44, 0.31, 0.42, 0.36, 0.45, 0.33):
        d.rectangle([40, y, 40 + int(900 * frac), y + 8], fill="black")
        y += 24
    d.polygon([(520, 480), (820, 480), (820, 250)], outline="green", width=6)
    img.save(path)
    assert ImageGate().screen_pixels(path).accept


def test_icons_banners_and_blank_images_are_rejected(tmp_path):
    gate = ImageGate()
    assert not gate.screen_pixels(_icon(tmp_path / "i.png")).accept
    assert not gate.screen_pixels(_banner(tmp_path / "b.png")).accept
    assert not gate.screen_pixels(_blank(tmp_path / "e.png")).accept


def test_an_unreadable_image_fails_closed(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    gate = ImageGate()
    assert not gate.screen_pixels(broken).accept


# --------------------------------------------------------------------------
# Duplicates (stage 3)
# --------------------------------------------------------------------------
def test_the_same_diagram_is_never_inserted_twice(tmp_path):
    gate = ImageGate()
    a = _graph(tmp_path / "a.png")
    b = tmp_path / "b.png"
    b.write_bytes(a.read_bytes())
    assert gate.accept_image(a, label="slide 3").accept
    second = gate.accept_image(b, label="slide 9")
    assert not second.accept
    assert "already inserted" in second.reason


def test_a_near_duplicate_diagram_is_rejected_by_perceptual_hash(tmp_path):
    gate = ImageGate()
    a = _graph(tmp_path / "a.png")
    near = Image.open(a).copy()
    ImageDraw.Draw(near).point((5, 5), fill="black")      # 1 px difference
    b = tmp_path / "near.png"
    near.save(b)
    assert hamming(average_hash(a), average_hash(b)) <= 6
    assert gate.accept_image(a, label="slide 3").accept
    assert not gate.accept_image(b, label="slide 7").accept


def test_two_genuinely_different_diagrams_both_pass(tmp_path):
    gate = ImageGate()
    assert gate.accept_image(_graph(tmp_path / "g.png"), section="Graph").accept
    assert gate.accept_image(_ray_diagram(tmp_path / "r.png"), section="Mirror").accept
    assert gate.accepted == 2


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------
def test_a_section_never_collects_more_than_one_diagram(tmp_path):
    gate = ImageGate()
    assert gate.accept_image(_graph(tmp_path / "g.png"), section="Graph").accept
    second = gate.accept_image(_ray_diagram(tmp_path / "r.png"), section="Graph")
    assert not second.accept
    assert "already has" in second.reason


def test_commit_false_does_not_consume_the_budget_until_commit(tmp_path):
    """The writer decides, Word inserts, THEN the gate registers — a failed
    insertion must not spend the section cap or poison the dup registry."""
    gate = ImageGate()
    d = gate.accept_image(_graph(tmp_path / "a.png"), section="S", commit=False)
    assert d.accept
    assert gate.accepted == 0                      # nothing registered yet
    d2 = gate.accept_image(_ray_diagram(tmp_path / "b.png"), section="S", commit=False)
    assert d2.accept                               # cap not consumed by the un-committed accept
    gate.commit_image(d2, section="S", label="slide 2")
    assert gate.accepted == 1
    third = gate.accept_image(_graph(tmp_path / "c.png"), section="S")
    assert not third.accept
    assert third.code == "section_cap"


def test_a_paid_ai_redraw_is_not_rejected_by_the_pixel_heuristics(tmp_path):
    """A redraw the run already paid for was validated as a real image when it
    was saved; the text-screenshot/ink screens are tuned for raw slide crops
    and misread handwritten flowcharts. Dedupe and the caps still apply."""
    shot = _text_screenshot(tmp_path / "redraw.png")   # worst case: reads as text
    slide = _slide(9, "Flow chart of classification of matter.", shot)
    slide.metadata["ai_redrawn"] = True
    gate = ImageGate()
    assert gate.accept_image(shot, slide=slide, section="Matter").accept


def test_zero_images_is_a_valid_outcome(tmp_path):
    """No rigid minimum: a document with nothing worth drawing gets nothing."""
    gate = ImageGate()
    for name, maker in (("i.png", _icon), ("b.png", _banner), ("e.png", _blank)):
        gate.accept_image(maker(tmp_path / name))
    assert gate.accepted == 0
    assert gate.report()["rejected"] == 3


def test_an_optional_hard_cap_can_be_set(tmp_path):
    gate = ImageGate(max_total=1)
    assert gate.accept_image(_graph(tmp_path / "g.png"), section="A").accept
    assert not gate.accept_image(_ray_diagram(tmp_path / "r.png"), section="B").accept


# --------------------------------------------------------------------------
# AI generation gate + early screening
# --------------------------------------------------------------------------
def test_ai_generation_is_refused_for_question_slides(tmp_path):
    gate = ImageGate()
    slide = _slide(1, "Q.3 Find the value of x. (a) 1 (b) 2 (c) 3 (d) 4",
                   _ray_diagram(tmp_path / "q.png"))
    assert not gate.should_generate_ai_image(slide, source_available=True).accept


def test_ai_generation_is_refused_when_there_is_no_source_image(tmp_path):
    gate = ImageGate()
    slide = _slide(1, "Ray diagram of a plane mirror.", None)
    assert not gate.should_generate_ai_image(slide, source_available=False).accept


def test_approve_slides_filters_the_redraw_worklist_before_any_billing(tmp_path):
    gate = ImageGate()
    slides = [
        _slide(1, "Ray diagram for a plane mirror with the normal shown.",
               _ray_diagram(tmp_path / "ok.png")),
        _slide(2, "Q.7 Choose the correct option. (a) x (b) y (c) z (d) w",
               _graph(tmp_path / "q.png")),
        _slide(3, "Thank you! Scan the QR code.", _graph(tmp_path / "qr.png")),
    ]
    approved, warnings = gate.approve_slides(slides, {1, 2, 3})
    assert approved == {1}
    assert len(warnings) == 2
    assert all("not used in the notes" in w for w in warnings)


# --------------------------------------------------------------------------
# Sizing / overflow
# --------------------------------------------------------------------------
def test_image_width_never_crosses_the_page_margins(tmp_path):
    huge = tmp_path / "huge.png"
    Image.new("RGB", (4000, 1200), "white").save(huge)
    width, height = fit_image_size(huge)
    assert width <= IMAGE_MAX_WIDTH_IN + 1e-6
    assert height > 0


def test_image_width_varies_with_the_source_resolution(tmp_path):
    small = tmp_path / "small.png"
    Image.new("RGB", (300, 240), "white").save(small)
    big = tmp_path / "big.png"
    Image.new("RGB", (1600, 1200), "white").save(big)
    assert fit_image_size(small)[0] < fit_image_size(big)[0]


def test_a_tall_diagram_is_fitted_by_height_not_stretched(tmp_path):
    tall = tmp_path / "tall.png"
    Image.new("RGB", (900, 3000), "white").save(tall)
    width, height = fit_image_size(tall)
    assert height <= 6.5 + 1e-6
    assert abs((height / width) - (3000 / 900)) < 0.01     # aspect preserved


def test_a_low_resolution_crop_is_not_blown_up(tmp_path):
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (200, 150), "white").save(tiny)
    width, _ = fit_image_size(tiny)
    assert width <= (200 / 96) * 1.6 + 1e-6


# --------------------------------------------------------------------------
# End to end through the DOCX writer
# --------------------------------------------------------------------------
_DTP = ('(Note to DTP: Insert the image with "mirror" and "normal" given on '
        'slide no. {n} under the heading "Plane Mirror".)')


def test_question_image_is_not_inserted_into_the_document(tmp_path):
    slide = _slide(5, "Q.9 Find the image distance. (a) 2 (b) 4 (c) 6 (d) 8",
                   _ray_diagram(tmp_path / "q.png"))
    notes = f"Plane Mirror\n• Image is virtual and erect.\n{_DTP.format(n=5)}\n"
    _, warnings = write_notes_docx(notes, tmp_path / "o.docx", [slide], run_dir=tmp_path)
    assert any("Inserted 0 image(s)" in w for w in warnings)
    assert any("Skipped image from slide 5" in w for w in warnings)


def test_a_relevant_diagram_is_inserted_once_and_not_repeated(tmp_path):
    slide = _slide(6, "Ray diagram: the incident ray, the normal and the "
                      "reflected ray at a plane mirror.",
                   _ray_diagram(tmp_path / "ray.png"), heading="Plane Mirror")
    notes = (
        "Plane Mirror\n• Image is virtual and erect.\n"
        f"{_DTP.format(n=6)}\n\n"
        "Spherical Mirror\n• Focal length is half the radius.\n"
        f"{_DTP.format(n=6)}\n"
    )
    out, warnings = write_notes_docx(notes, tmp_path / "o.docx", [slide], run_dir=tmp_path)
    assert any("Inserted 1 image(s)" in w for w in warnings)
    from docx import Document
    assert len(Document(str(out)).inline_shapes) == 1


def test_inserted_images_stay_inside_the_text_column(tmp_path):
    slide = _slide(7, "Graph of velocity against time with both axes labelled.",
                   _graph(tmp_path / "g.png", size=(2400, 900)), heading="Graph")
    notes = f"Graph\n• Slope gives acceleration.\n{_DTP.format(n=7)}\n"
    out, _ = write_notes_docx(notes, tmp_path / "o.docx", [slide], run_dir=tmp_path)
    from docx import Document
    from docx.shared import Inches
    for shape in Document(str(out)).inline_shapes:
        assert int(shape.width) <= int(Inches(IMAGE_MAX_WIDTH_IN)) + 9144


def test_a_section_cap_rejection_does_not_eat_the_slides_only_region(tmp_path):
    """A slide capped out of one section must still land in a later section.

    The cap is external to the slide: rejecting slide 2 under a full heading
    used to consume its only region, so the SAME slide referenced again under
    the next heading got "no unused diagram region left" and the figure was
    lost from the whole document.
    """
    s1 = _slide(1, "Ray diagram for a plane mirror with the normal shown.",
                _ray_diagram(tmp_path / "r.png"), heading="Optics")
    s2 = _slide(2, "Graph of velocity against time with both axes labelled.",
                _graph(tmp_path / "g.png"), heading="Optics")
    notes = (
        "Optics\n• Light travels in straight lines.\n"
        f"{_DTP.format(n=1)}\n{_DTP.format(n=2)}\n\n"
        "Refraction\n• Bending of light at a boundary.\n"
        f"{_DTP.format(n=2)}\n"
    )
    out, warnings = write_notes_docx(notes, tmp_path / "o.docx", [s1, s2],
                                     run_dir=tmp_path)
    from docx import Document
    assert any("Inserted 2 image(s)" in w for w in warnings), warnings
    assert len(Document(str(out)).inline_shapes) == 2


def test_the_writer_reuses_the_pipelines_gate(tmp_path):
    """A slide already rejected before the AI stage cannot re-enter at insert."""
    slide = _slide(8, "Ray diagram of a plane mirror.",
                   _ray_diagram(tmp_path / "ray.png"))
    gate = ImageGate()
    gate.approve_slides([slide], {8})
    gate.accept_image(_ray_diagram(tmp_path / "same.png"), label="slide 8")  # registered
    notes = f"Plane Mirror\n• Virtual image.\n{_DTP.format(n=8)}\n"
    _, warnings = write_notes_docx(notes, tmp_path / "o.docx", [slide],
                                   run_dir=tmp_path, image_gate=gate)
    assert any("Inserted 0 image(s)" in w for w in warnings)
