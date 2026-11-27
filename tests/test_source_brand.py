"""CSIR NET, and any category set to "from_pdf": B.Sc.-style notes take their header name and logo
from each lecture PDF (typeset/source_brand.py), and those header lines are not repeated in the
notes' content. No model calls."""
from __future__ import annotations

import io
import json
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageDraw

from note_studio import categories, category_admin
from note_studio.engines import typeset
from note_studio.typeset import render, source, source_brand, transcribe

LEFT, RIGHT = "CSIR NET - Life Sciences", "Unit 1: Biochemistry"
BODY = ["Proteins are heteropolymers of twenty standard L-amino acids.",
        "Glycine is achiral and adopts angles forbidden to other residues.",
        "Proline is an imino acid that breaks alpha helices."]


def no_ai():
    raise AssertionError("Reading the header and preserving the source must never call AI")


def logo_png(width: int = 240, height: int = 120) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((4, 4, width - 4, height - 4), fill=(32, 74, 135, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def lecture(pages: int = 3, *, header: bool = True, logo: bool = True, title_top: bool = False) -> bytes:
    """A LaTeX-notes-like PDF: a running header (left name, right unit), a logo between them, a title on
    page 1, body text and a page number at the bottom of every page."""
    picture = logo_png()
    with fitz.open() as pdf:
        for n in range(pages):
            page = pdf.new_page(width=595.28, height=841.89)
            if header:
                page.insert_text((29, 16), LEFT, fontsize=10)
                page.insert_text((566 - fitz.get_text_length(RIGHT, fontsize=10), 16), RIGHT, fontsize=10)
            else:
                page.insert_text((29, 16), f"Slide topic {n + 1}", fontsize=10)
            if logo:
                page.insert_image(fitz.Rect(270, 4, 310, 24), stream=picture)
            top = 60
            if n == 0:
                page.insert_text((150, 30 if title_top else 60), "Biomolecules Part 1: Proteins", fontname="hebo",
                                 fontsize=22)
                top = 100
            page.insert_text((45, top), BODY[n % len(BODY)], fontsize=11)
            page.insert_text((295, 826), str(n + 1), fontsize=10)
        return pdf.tobytes()


# ------------------------------------------------------------------ reading the PDF
def test_the_running_header_name_right_words_and_logo_are_read():
    found = source_brand.read(lecture())
    assert (found["left"], found["right"]) == (LEFT, RIGHT)
    assert found["pages"] == 3 and "1" not in found["lines"]
    assert not any("Biomolecules" in line for line in found["lines"])        # the title is not a header
    with Image.open(io.BytesIO(found["logo"])) as image:
        assert image.mode == "RGBA" and image.size == (240, 120)
        assert image.getchannel("A").getextrema() == (0, 255)                 # its own transparency kept
    assert found["logo_box"] == pytest.approx((270, 4, 310, 24), abs=0.5)


def test_a_pdf_that_repeats_nothing_at_the_top_gives_nothing():
    found = source_brand.read(lecture(header=False, logo=False))
    assert (found["left"], found["right"], found["logo"], found["lines"]) == ("", "", None, [])
    assert source_brand.read(b"not a pdf")["left"] == ""


def test_a_one_page_pdf_counts_only_small_text_at_the_very_top():
    found = source_brand.read(lecture(pages=1, title_top=True))
    assert (found["left"], found["right"]) == (LEFT, RIGHT)
    assert found["logo"]


def test_header_lines_and_page_numbers_are_furniture_only_at_the_page_edges():
    words = source_brand.furniture(source_brand.read(lecture()))["words"]
    assert source_brand.is_furniture(LEFT, 0.01, words)
    assert source_brand.is_furniture(f"{LEFT} {RIGHT}", 0.01, words)
    assert source_brand.is_furniture("CSIR NET – Life Sciences", 0.02, words)    # another dash, same line
    assert source_brand.is_furniture("7", 0.97, words) and source_brand.is_furniture("Page 2 of 9", 0.01, words)
    assert not source_brand.is_furniture(LEFT, 0.5, words)                     # the same words in the body stay
    assert not source_brand.is_furniture("7", 0.5, words)
    assert not source_brand.is_furniture(LEFT, 0.01, set())


# ------------------------------------------------------------------ the notes
def csir_settings(**more) -> typeset.Settings:
    pack = json.loads((categories.BUILTIN_DIR / "csir_net" / "category.json").read_text(encoding="utf-8"))
    pack["folder"] = "unused"
    settings = typeset.run_settings({}, pack, "life_sciences", "english", "", {})
    for key, value in more.items():
        setattr(settings, key, value)
    return settings


def test_csir_net_notes_carry_the_lectures_header_and_logo_and_not_twice(tmp_path):
    result = typeset.make_notes(lecture(), "Proteins.pdf", tmp_path, csir_settings(content_mode="preserve"),
                                token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    report = result.quality_report
    assert report["checks"][0]["check"] == "Header name and logo"
    assert LEFT in report["checks"][0]["detail"] and "logo" in report["checks"][0]["detail"]
    assert report["header_from_pdf"]["header_left"] == LEFT and report["header_from_pdf"]["header_right"] == RIGHT
    assert (tmp_path / "lecture_logo.png").is_file()
    with fitz.open(result.output_path) as pdf:
        for page in pdf:
            header = page.get_text("text", clip=fitz.Rect(0, 0, page.rect.width, 30))
            assert LEFT in header and RIGHT in header and "CSIR NET Notes" not in header
            assert page.get_text().count(LEFT) == 2                           # header and footer, never the body
            corner = [fitz.Rect(i["bbox"]) for i in page.get_image_info() if i["bbox"][1] < 30]
            assert len(corner) == 1
            assert corner[0].height == pytest.approx(render.LOGO, abs=0.5)    # 11 mm tall, as wide as it is
            assert corner[0].width == pytest.approx(render.LOGO * 232 / 112, abs=0.5)   # (trimmed of its clear edge)
            assert corner[0].x1 == pytest.approx(render.PAGE_W - render.LOGO_MARGIN, abs=0.5)
            assert any(i["bbox"][2] - i["bbox"][0] == pytest.approx(render.WATERMARK, abs=1) for i in page.get_image_info())
        body = source.normal(" ".join(p.get_text() for p in pdf))
        assert all(line in body for line in BODY)
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    assert RIGHT not in json.dumps(doc) and not any(b.get("image") for b in doc["blocks"])
    left_out = json.loads((tmp_path / "source_map.json").read_text("utf-8"))["excluded_furniture"]
    left_words = " | ".join(e["text"] for e in left_out)
    assert left_words.count(LEFT) == 3 and left_words.count(RIGHT) == 3     # one header block per page
    assert {e["text"] for e in left_out} >= {"1", "2", "3"}
    assert sum("header logo" in e["reason"] for e in left_out) == 3


def test_a_pdf_without_a_header_keeps_the_categorys_words(tmp_path):
    result = typeset.make_notes(lecture(header=False, logo=False), "Slides.pdf", tmp_path,
                                csir_settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    assert "category's own header “CSIR NET”" in result.quality_report["checks"][0]["detail"]
    with fitz.open(result.output_path) as pdf:
        header = pdf[0].get_text("text", clip=fitz.Rect(0, 0, pdf[0].rect.width, 30))
        assert "CSIR NET" in header and "CSIR NET Notes" in header
        assert not [i for i in pdf[0].get_image_info() if i["bbox"][1] < 30]       # no logo: CSIR NET has no PW logo


def test_categories_without_from_pdf_are_unchanged(tmp_path):
    result = typeset.make_notes(lecture(), "Proteins.pdf", tmp_path, typeset.Settings(content_mode="preserve"),
                                token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    assert all(c["check"] != "Header name and logo" for c in result.quality_report["checks"])
    assert not (tmp_path / "lecture_logo.png").exists()
    with fitz.open(result.output_path) as pdf:
        assert "B.Sc. Wallah" in pdf[0].get_text("text", clip=fitz.Rect(0, 0, pdf[0].rect.width, 30))


def test_ai_read_pages_leave_out_the_header_lines_and_page_numbers(tmp_path):
    data = lecture(pages=2, logo=False)
    folder = tmp_path / "transcription"
    folder.mkdir()
    (folder / "source.json").write_text(json.dumps(transcribe.cache_identity(data)), encoding="utf-8")
    for n in range(2):
        found = {"items": [{"kind": "paragraph", "text": f"{LEFT} {RIGHT}", "bbox": [40, 8, 960, 22], "style": "none"},
                           {"kind": "paragraph", "text": BODY[n], "bbox": [70, 90, 900, 130], "style": "none"},
                           {"kind": "paragraph", "text": LEFT, "bbox": [70, 400, 900, 420], "style": "none"},
                           {"kind": "paragraph", "text": str(n + 1), "bbox": [490, 975, 510, 985], "style": "none"}],
                 "unreadable": [], "nonacademic": False, "checked": True}
        (folder / f"page-{n + 1:04d}-review.json").write_text(json.dumps(found), encoding="utf-8")
    result = typeset.make_notes(data, "Proteins.pdf", tmp_path, csir_settings(full_quality=False), token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    texts = [b.get("text") or " ".join(b.get("parts") or []) for b in doc["blocks"]]
    assert texts.count(LEFT) == 2                       # the same words in the body are content and stay
    assert not any(RIGHT in t for t in texts) and not any(t in ("1", "2") for t in texts)
    assert BODY[0] in texts and BODY[1] in texts


def test_a_wide_logo_is_capped_and_the_pw_logo_stays_square():
    assert render.logo_box(logo_png(), square=True) == (render.LOGO_X, render.LOGO_MARGIN, render.LOGO, render.LOGO)
    x, y, w, h = render.logo_box(logo_png(1200, 100), square=False)
    assert w == pytest.approx(render.LOGO * render.LOGO_WIDE) and h < render.LOGO and x + w == pytest.approx(
        render.PAGE_W - render.LOGO_MARGIN)
    assert render.logo_box(None) is None


# ------------------------------------------------------------------ the category and Settings
def test_the_csir_net_pack_is_built_in_and_takes_its_header_from_the_pdf():
    categories.seed()
    pack = categories.get("csir_net")
    assert pack["label"] == "CSIR NET" and pack["enabled"] is True and pack["builtin"] is True
    assert [s["label"] for s in pack["subjects"]][:4] == ["Life Sciences", "Physical Sciences", "Chemical Sciences",
                                                          "Mathematical Sciences"]
    assert categories.branding_from_pdf(pack) is True and categories.pw_logo(pack) is False
    offered = [t["id"] for t in categories.offered_types(pack)]
    assert offered[0] == "typeset_notes" and "typeset_gdoc" not in offered
    assert not [c for c in category_admin.check("csir_net") if c["kind"] == "bad"]
    settings = typeset.run_settings({}, pack, "", "", "", {})
    assert settings.branding_from_pdf is True and settings.branding["header_left"] == "CSIR NET"
    assert typeset.run_settings({}, categories.get("bsc"), "", "", "", {}).branding_from_pdf is False


def test_settings_switches_the_header_source_per_category(client):
    page = client.get("/settings/categories").text
    assert 'action="/settings/categories/csir_net/from_pdf"' in page and "Use this category&#39;s own words" in page
    assert 'action="/settings/categories/bsc/from_pdf"' in page and "Take them from each lecture PDF" in page
    assert 'action="/settings/categories/neet_jee/from_pdf"' not in page          # not a B.Sc.-style category
    assert 'name="from_pdf"' in page                                              # Add a category

    on = client.post("/settings/categories/bsc/from_pdf", data={"on": "1"}, follow_redirects=False)
    assert "kind=ok" in on.headers["location"] and categories.branding_from_pdf(categories.get("bsc"))
    folder = Path(categories.get("bsc")["folder"])
    assert "from_pdf" not in json.loads((folder / "category.json.bak").read_text(encoding="utf-8"))["branding"]
    client.post("/settings/categories/bsc/from_pdf", data={"on": "0"})
    assert categories.branding_from_pdf(categories.get("bsc")) is False
    refused = client.post("/settings/categories/neet_jee/from_pdf", data={"on": "1"}, follow_redirects=False)
    assert "kind=warn" in refused.headers["location"] and not categories.branding_from_pdf(categories.get("neet_jee"))

    added = client.post("/settings/categories/add", data={
        "label": "GATE Life Sciences", "subjects": "Biochemistry\n", "languages": ["english"],
        "note_types": ["typeset_notes"], "from_pdf": "1"}, follow_redirects=False)
    assert "kind=ok" in added.headers["location"]
    assert categories.branding_from_pdf(categories.get("gate_life_sciences")) is True
    client.post("/settings/categories/add", data={"label": "Plain Exam", "languages": ["english"],
                                                  "note_types": ["concise_handdrawn"], "from_pdf": "1"})
    assert categories.branding_from_pdf(categories.get("plain_exam")) is False     # not B.Sc.-style: no header
