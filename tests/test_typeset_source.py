"""Real PDF -> locked source -> actual renderer, without a fake AI document."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import fitz
import pytest

from note_studio.engines import typeset
from note_studio.typeset import quality, render, source


@pytest.fixture
def source_pdf():
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((45, 65), "Frames of Reference", fontname="hebo", fontsize=20)
        page.insert_text((45, 104), "1. Fundamental concepts", fontname="hebo", fontsize=14)
        page.insert_text((45, 133), "An event occurs at a definite position and time. Cost: $5; **literal**.", fontsize=11)
        page.draw_rect((33, 155, 562, 222), color=(32/255, 74/255, 135/255), fill=(32/255, 74/255, 135/255))
        page.draw_rect((34, 173, 561, 221), color=None, fill=(.97, .98, .99))
        page.insert_text((45, 168), "Definition 1: Frame of Reference", color=(1, 1, 1), fontsize=11)
        page.insert_text((45, 195), "A coordinate system relative to which position is specified.", fontsize=11)
        page.insert_text((45, 253), "Keep this numerical example: 12 kg, 3 m/s, and 18 J.", fontsize=11)
        second = pdf.new_page()
        second.insert_text((45, 65), "2. The final topic", fontname="hebo", fontsize=14)
        second.insert_text((45, 95), "This last paragraph must not disappear or move before the example.", fontsize=11)
        return pdf.tobytes()


def no_ai():
    raise AssertionError("Source preservation must never request a token or call AI")


def test_real_pdf_content_survives_the_whole_renderer(source_pdf, tmp_path):
    result = typeset.make_notes(source_pdf, "Lecture.pdf", tmp_path, typeset.Settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    assert result.quality_report["verdict"] == "passed"
    assert result.calls == 0 and result.cost_inr == 0
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    manifest = json.loads((tmp_path / "source_map.json").read_text("utf-8"))
    assert manifest["source_pages"] == 2
    assert any(b["kind"] == "source_box" for b in doc["blocks"])
    with fitz.open(result.output_path) as pdf:
        text = source.normal(" ".join(p.get_text() for p in pdf))
        assert "Cost: $5; **literal**." in text
        assert text.index("12 kg, 3 m/s, and 18 J") < text.index("The final topic")
        assert "Definition 1: Frame of Reference" in text and "Definition 2:" not in text
    assert source.verify(doc, manifest, tmp_path / "source_excerpts", rendered=Path(result.output_path).read_bytes(),
                         emitted=result.quality_report["layout"]["source_emitted"]) == []


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "reorder", "rewrite"])
def test_changed_or_missing_content_is_rejected(source_pdf, tmp_path, mutation):
    doc, manifest = source.extract(source_pdf, tmp_path)
    if mutation == "omit":
        doc["blocks"].pop()
    elif mutation == "duplicate":
        doc["blocks"].append(copy.deepcopy(doc["blocks"][-1]))
    elif mutation == "reorder":
        doc["blocks"].reverse()
    else:
        doc["blocks"][0]["text"] = "An invented replacement title"
    assert source.verify(doc, manifest, tmp_path)


def test_missing_output_content_cannot_pass_the_source_audit(source_pdf, tmp_path):
    doc, manifest = source.extract(source_pdf, tmp_path)
    with fitz.open() as blank:
        blank.new_page()
        problems = source.verify(doc, manifest, tmp_path, rendered=blank.tobytes(), emitted=[])
    assert any("wording" in p for p in problems) and any("draw every" in p for p in problems)


def test_scanned_content_needs_explicit_transcription_instead_of_copying_the_old_page(tmp_path):
    from PIL import Image, ImageDraw
    import io

    image = Image.new("RGB", (800, 400), "white")
    ImageDraw.Draw(image).text((30, 30), "Source diagram and handwritten lecture", fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    with fitz.open() as pdf:
        page = pdf.new_page(width=800, height=400)
        page.insert_image(page.rect, stream=buf.getvalue())
        data = pdf.tobytes()
    result = typeset.make_notes(data, "Scan.pdf", tmp_path, typeset.Settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "failed" and "default B.Sc." in result.message
    assert result.cost_inr == 0 and result.calls == 0
    assert not (tmp_path / "Scan Notes.pdf").exists()


def test_latex_printed_as_pdf_gets_a_clear_error_without_ai(tmp_path):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 60), r"\documentclass[11pt]{article}")
        data = pdf.tobytes()
    result = typeset.make_notes(data, "code.pdf", tmp_path, typeset.Settings(), token_provider=no_ai)
    assert result.state == "failed" and "Compile it first" in result.message
    assert not (tmp_path / "code Notes.pdf").exists()


def test_the_app_defaults_to_ai_for_every_page_and_the_fixed_template(client, source_pdf, monkeypatch):
    from note_studio import db, job_queue, make_service
    from note_studio.typeset import transcribe

    requested = []
    def read_pages(data, numbers, folder, session, models, say):
        requested.extend(numbers)
        return {n: {"items": [{"kind": "paragraph", "text": f"Lecture page {n+1} content.",
                              "bbox": [10,10,900,300], "style": "concept"}],
                    "unreadable": [], "nonacademic": False, "checked": True} for n in numbers}
    monkeypatch.setattr(make_service, "typeset_token", lambda: lambda: "test-token")
    monkeypatch.setattr(transcribe, "pages", read_pages)
    from note_studio.typeset import source_quality
    monkeypatch.setattr(source_quality, "review", lambda *args: {"verdict": "passed", "reviewed_pages": [1],
        "problem": "", "blocking_issues": [], "advisory_issues": []})

    response = client.post("/new", data={"category": "bsc", "note_type": "typeset_notes", "subject": "physics",
                                        "language": "english"},
                           files=[("files", ("Lecture.pdf", source_pdf, "application/pdf"))])
    assert response.status_code in (200, 303)
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs ORDER BY id DESC")[0]
    assert run["state"] == "needs_attention" and requested == [0, 1]
    assert job_queue.held() != "signin"
    run_page = client.get(f"/runs/{run['id']}").text
    assert "Lecture Notes.pdf" in run_page and "Source content and order record" in run_page
    downloads = re.findall(rf'href="(/runs/{run["id"]}/files/\d+)"', run_page)
    assert any(client.get(link).content.startswith(b"%PDF") for link in downloads)
    page = client.get("/new?category=bsc").text
    assert "AI notes in the B.Sc. template" in page
    assert 'name="content_mode"' not in page and 'name="transcribe_scans"' not in page
    assert "Transcribe scanned or complex pages with PW AI" not in page
    assert typeset.run_settings({}, {}, "", "", "", {}).content_mode == "reference_ai"


def test_ai_mode_cannot_pass_after_content_was_dropped():
    doc = {"blocks": [{"kind": "paragraph", "text": "A short fragment"}]}
    report = quality.report(doc=doc, tidy=["block 3 (box): left out (empty or not a known kind)"],
                            review={"asked": False}, figures=[], layout={"pages": 1}, language="english")
    assert report["verdict"] == "needs_attention"
    assert any(c["check"] == "Content removed during processing" for c in report["checks"])


def test_source_assets_do_not_float_after_later_text(source_pdf, tmp_path):
    doc, manifest = source.extract(source_pdf, tmp_path)
    data, layout = render.render_pdf(doc, render.Branding(), tmp_path)
    assert source.verify(doc, manifest, tmp_path, rendered=data, emitted=layout["source_emitted"]) == []


def test_list_items_keep_separate_lines_and_exact_wording(tmp_path):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), "1. First source item\n2. Second source item\n3. Third source item", fontsize=11)
        data = pdf.tobytes()
    result = typeset.make_notes(data, "List.pdf", tmp_path, typeset.Settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "complete", result.message
    with fitz.open(result.output_path) as pdf:
        words = pdf[0].get_text("words")
        item_ys = [round(w[1]) for w in words if w[4] in ("First", "Second", "Third")]
    assert len(set(item_ys)) == 3


def test_highlighted_text_is_rebuilt_into_reference_boxes(tmp_path):
    with fitz.open() as pdf:
        p = pdf.new_page()
        p.draw_rect((39, 79, 360, 95), color=None, fill=(252/255,237/255,223/255))
        p.insert_text((40, 92), "A reference frame is a coordinate system used to describe motion.", fontsize=11)
        data = pdf.tobytes()
    result = typeset.make_notes(data, "Highlighted.pdf", tmp_path, typeset.Settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "complete" and result.quality_report["verdict"] == "passed", result.message
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    assert any(b["kind"] == "source_box" for b in doc["blocks"])
    assert not any(b["kind"] == "source_panel" for b in doc["blocks"])


def test_inline_formula_images_do_not_force_a_whole_page_screenshot(tmp_path):
    import io
    from PIL import Image

    picture = io.BytesIO()
    Image.new("RGB", (20,20), "black").save(picture, format="PNG")
    with fitz.open() as pdf:
        p = pdf.new_page()
        p.insert_text((40, 90), "The coordinates of the event are shown by these source symbols.", fontsize=11)
        p.insert_image((130,82,137,90), stream=picture.getvalue())
        p.insert_image((160,82,167,90), stream=picture.getvalue())
        data = pdf.tobytes()
    result = typeset.make_notes(data, "Inline.pdf", tmp_path, typeset.Settings(content_mode="preserve"), token_provider=no_ai)
    assert result.state == "complete", result.message
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    assert all(b.get("height_pt", 0) < 100 for b in doc["blocks"])


@pytest.mark.parametrize("bad", [{"items": [], "unreadable": [], "nonacademic": False},
    {"items": [{"kind": "paragraph", "text": "", "bbox": [0,0,100,100], "style": "none"}], "unreadable": [], "nonacademic": False},
    {"items": [{"kind": "diagram", "text": "", "bbox": [10,10,10,10], "style": "none"}], "unreadable": [], "nonacademic": False}])
def test_incomplete_page_transcription_is_rejected_without_dropping_content(bad):
    from note_studio.typeset import ai, transcribe

    with pytest.raises(ai.AIError):
        transcribe.validate(bad)


def test_reversed_bbox_corners_retain_the_same_source_region():
    from note_studio.typeset import transcribe

    found = {"items": [{"kind": "paragraph", "text": "Keep this sentence.",
                        "bbox": [58,477,992,362], "style": "none"}], "unreadable": [], "nonacademic": False}
    assert transcribe.validate(found)[0]["bbox"] == [58,362,992,477]
    assert found["layout_warnings"]


def test_failed_page_cache_reuses_checked_pages_and_only_reviews_the_unfinished_page(source_pdf, tmp_path):
    from note_studio.typeset import transcribe

    folder = tmp_path / "transcription"
    folder.mkdir()
    (folder / "source.json").write_text(json.dumps(transcribe.cache_identity(source_pdf)), encoding="utf-8")
    item = {"kind": "paragraph", "text": "Original wording.", "bbox": [10,10,900,300], "style": "none"}
    found = {"items": [item], "unreadable": [], "nonacademic": False}
    (folder / "page-0001-review.json").write_text(json.dumps({**found, "checked": True}), encoding="utf-8")
    (folder / "page-0002.json").write_text(json.dumps(found), encoding="utf-8")
    calls = []
    class Session:
        pages = 2
        def json(self, kind, *args, **kwargs):
            calls.append(kind)
            return {**copy.deepcopy(found), "checked": True}
    pages = transcribe.pages(source_pdf, [0,1], folder, Session(), [], lambda *args: None)
    assert sorted(pages) == [0,1] and calls == ["transcription review page 2"]
    assert transcribe.load_cached(source_pdf + b"changed", [0,1], folder) == {}
    identity = transcribe.cache_identity(source_pdf)
    identity["prompt_sha256"] = "different"
    (folder / "source.json").write_text(json.dumps(identity), encoding="utf-8")
    assert transcribe.load_cached(source_pdf, [0,1], folder) == {}


def test_cached_default_ai_needs_no_new_token_and_preserves_hindi_and_inline_math(source_pdf, tmp_path):
    from note_studio.typeset import transcribe

    folder = tmp_path / "transcription"
    folder.mkdir()
    (folder / "source.json").write_text(json.dumps(transcribe.cache_identity(source_pdf)), encoding="utf-8")
    for n in range(2):
        text = f"{n+1}. निर्देश फ्रेम $\\rightarrow$ coordinate system. Space \\rightarrow (x,y,z)."
        found = {"items": [{"kind": "paragraph", "text": text, "bbox": [10,10,900,300], "style": "definition"}],
                 "unreadable": [], "nonacademic": False, "checked": True}
        (folder / f"page-{n+1:04d}-review.json").write_text(json.dumps(found), encoding="utf-8")
    result = typeset.make_notes(source_pdf, "Lecture.pdf", tmp_path, typeset.Settings(full_quality=False), token_provider=no_ai)
    assert result.state == "complete", (result.message, result.detail)
    assert result.cost_inr == 0 and result.calls == 0
    assert result.quality_report["reused_transcription_pages"] == [1,2]
    assert result.quality_report["verdict"] == "needs_attention"
    assert "reviewed transcription" in result.quality_report["checks"][0]["detail"]
    canvas = render.Canvas(render.Branding(), "")
    fragments = [f for word in canvas.words(r"Space \rightarrow (x,y,z)", literal=True, inline_math=True) for f in word]
    assert any(f.text == "→" and f.logical == r"\rightarrow" for f in fragments)
    assert any(f.formula for word in canvas.words(r"Time $\rightarrow$ t", literal=True, inline_math=True) for f in word)


def test_scanned_page_is_transcribed_reviewed_and_rendered_by_default(tmp_path, monkeypatch):
    import base64
    import io
    from PIL import Image
    from note_studio import engine_catalog

    image = io.BytesIO()
    Image.new("RGB", (640,360), "white").save(image, format="PNG")
    with fitz.open() as pdf:
        page = pdf.new_page(width=640, height=360)
        page.insert_image(page.rect, stream=image.getvalue())
        data = pdf.tobytes()
    engine_catalog.put_shortnotes_on_path()
    import pw_access

    calls = []
    transcription = {"items": [
        {"kind": "heading", "text": "Frame of Reference", "bbox": [40,20,600,100], "style": "none"},
        {"kind": "paragraph", "text": "A frame is a coordinate system.", "bbox": [40,120,900,280], "style": "definition"},
        {"kind": "diagram", "text": "", "bbox": [400,350,800,800], "style": "none"}],
        "unreadable": [], "nonacademic": False}

    def answer(provider, *, request, count, **kwargs):
        assert provider() == "test-token" and count == 1
        parts = request["contents"][0]["parts"]
        single = fitz.open(stream=base64.b64decode(parts[0]["inline_data"]["data"]), filetype="pdf")
        assert len(single) == 1
        review = "checked" in request["generationConfig"]["responseSchema"]["properties"]
        found = {**copy.deepcopy(transcription), **({"checked": True} if review else {})}
        calls.append(review)
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(found)}]}, "finishReason": "STOP"}], "cost_inr": .25}

    monkeypatch.setattr(pw_access, "gemini_generate", answer)
    result = typeset.make_notes(data, "Scan.pdf", tmp_path, typeset.Settings(full_quality=False),
                                token_provider=lambda: lambda: "test-token")
    assert result.state == "complete", (result.message, result.detail)
    assert calls == [False, True] and result.calls == 2 and result.cost_inr == .5
    assert result.quality_report["verdict"] == "needs_attention"
    assert result.quality_report["transcription_pages"] == [1]
    doc = json.loads((tmp_path / "notes.json").read_text("utf-8"))
    assert any(b["kind"] == "source_box" for b in doc["blocks"])
    assert not any(b.get("height_pt",0) >= 360 for b in doc["blocks"])
    manifest = json.loads((tmp_path / "source_map.json").read_text("utf-8"))
    visual = next(u for u in manifest["units"] if u.get("image"))
    asset = tmp_path / "source_excerpts" / visual["image"]
    asset.write_bytes(b"changed")
    assert source.verify(doc, manifest, asset.parent)
