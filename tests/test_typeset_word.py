"""Literal Word input and appearance-only AI through the real upload pipeline."""
import copy
import io
import json
from pathlib import Path

import pytest
from docx import Document
from PIL import Image

from note_studio.engines import typeset
from note_studio.typeset import render, source, word_source


def reference(rows=2):
    doc = Document()
    doc.add_heading("Nursing reference", 0)
    doc.add_paragraph("Keep ALL wording: $5; **literal**; 70–100 square meters.")
    doc.add_paragraph("First source list point", style="List Number")
    doc.add_paragraph("Second source list point", style="List Number")
    image = io.BytesIO()
    Image.new("RGB", (300,200), "navy").save(image, format="PNG")
    doc.add_picture(io.BytesIO(image.getvalue()))
    table = doc.add_table(rows=rows+1, cols=2)
    table.cell(0,0).text, table.cell(0,1).text = "Structure", "Original function"
    for i in range(rows):
        table.cell(i+1,0).text = f"Row {i+1}"
        table.cell(i+1,1).text = f"Keep the full original explanation number {i+1}."
    doc.add_paragraph("This final paragraph must remain after the complete table.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def test_every_word_image_and_table_row_survives_even_across_pages(tmp_path):
    data = reference(70)
    doc, manifest = word_source.extract(data, tmp_path)
    assert manifest["counts"] == {"paragraphs": 5, "tables": 1, "images": 1}
    assert any(b.get("text", "").startswith("1. First") for b in doc["blocks"])
    assert any(b.get("text", "").startswith("2. Second") for b in doc["blocks"])
    pdf, layout = render.render_pdf(doc, render.Branding(), tmp_path)
    assert layout["pages"] > 1
    assert not source.verify(doc, manifest, tmp_path, rendered=pdf, emitted=layout["source_emitted"])
    doc["blocks"].pop()
    assert source.verify(doc, manifest, tmp_path)


def test_ai_only_controls_appearance_and_cached_rendering_never_pays_twice(tmp_path):
    data = reference()
    assets = tmp_path / "source_excerpts"
    doc, manifest = word_source.extract(data, assets)
    calls = []
    class Session:
        def json(self, kind, models, system, parts, schema, **kwargs):
            calls.append(kind)
            assert "text" not in schema["properties"]["styles"]["items"]["properties"]
            return {"styles": [{"id": "w00002", "role": "paragraph", "style": "definition"}]}
    original = copy.deepcopy(manifest["units"])
    word_source.style(doc, manifest, tmp_path, Session(), [], lambda *args: None)
    assert calls == ["Word reference formatting"]
    assert [u["texts"] for u in original] == [u["texts"] for u in manifest["units"]]
    result = typeset.make_notes(data, "Reference.docx", tmp_path, typeset.Settings(full_quality=False),
        token_provider=lambda: pytest.fail("Cached Word formatting must not request another token"))
    assert result.state == "complete", (result.message, result.detail)
    assert result.calls == 0 and result.cost_inr == 0
    assert result.quality_report["verdict"] == "passed"
    assert Path(result.output_path).name == "Reference Notes.pdf"


def test_word_upload_uses_the_nursing_category_and_produces_a_download(client, monkeypatch):
    from note_studio import category_admin, db, job_queue, make_service
    from note_studio.typeset import ai

    cid = category_admin.add({"label": "Nursing", "subjects": "Anatomy", "languages": ["english"],
                              "note_types": ["typeset_notes"]})
    monkeypatch.setattr(make_service, "typeset_token", lambda: lambda: "test-token")
    def answer(self, kind, models, system, parts, schema, **kwargs):
        if kind == "final quality":
            import base64
            import fitz
            with fitz.open(stream=base64.b64decode(parts[0]["inline_data"]["data"]), filetype="pdf") as pdf:
                return {"reviewed_pages": list(range(1,len(pdf)+1)), "unreadable_pages": [], "issues": [], "edits": []}
        return {"styles": []}
    monkeypatch.setattr(ai.Session, "json", answer)
    response = client.post("/new", data={"category": cid, "note_type": "typeset_notes", "subject": "anatomy",
                                        "language": "english"},
                           files=[("files", ("Unit 2.docx", reference(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))])
    assert response.status_code in (200,303)
    assert job_queue.wait_idle(60)
    run = db.query("SELECT * FROM runs ORDER BY id DESC")[0]
    assert run["state"] == "complete", (run["message"], run["error"])
    report = json.loads((Path(run["run_dir"])/"quality_report.json").read_text("utf-8"))
    assert report["source_format"] == "docx" and report["source_counts"]["images"] == 1
    assert "Unit 2 Notes.pdf" in client.get(f"/runs/{run['id']}").text
    assert ",.docx" in client.get("/new?category=nursing").text


def test_unreadable_word_structures_fail_before_a_token_or_charge(tmp_path):
    doc = Document()
    doc.add_paragraph("Complete source")
    doc.sections[0].header.paragraphs[0].text = "Academic header that cannot be dropped"
    buf = io.BytesIO()
    doc.save(buf)
    result = typeset.make_notes(buf.getvalue(), "Reference.docx", tmp_path, typeset.Settings(),
                               token_provider=lambda: pytest.fail("Unsupported input must fail before billing"))
    assert result.state == "failed" and "header or footer" in result.message and result.cost_inr == 0


def _long_doc(paragraphs: int) -> tuple[dict, dict]:
    blocks = [{"kind": "source_text", "source_id": "t0", "role": "title", "text": "Unit 1"}] + [
        {"kind": "source_text", "source_id": f"p{i:04d}", "role": "paragraph", "text": f"Source paragraph {i}."}
        for i in range(paragraphs)]
    units = [{"id": b["source_id"], "texts": [b["text"]]} for b in blocks]
    return {"title": "Unit 1", "preserve_source": True, "blocks": blocks}, {"source_sha256": "abc", "units": units}


class PartSession:
    """Answers each formatting call with a heading choice for the first paragraph it was sent."""

    def __init__(self, fail_at: int = 0, stray: bool = False):
        self.calls, self.fail_at, self.stray = [], fail_at, stray

    def json(self, kind, models, system, parts, schema, **kwargs):
        sent = json.loads(parts[0]["text"])
        self.calls.append((kind, len(sent), kwargs.get("max_tokens")))
        if self.fail_at and len(self.calls) == self.fail_at:
            raise word_source.ai.AIError("PW AI's answer was cut off")
        first = "p9999" if self.stray else sent[0]["id"]
        return {"styles": [{"id": first, "role": "section", "style": "none"}]}


def test_a_short_reference_is_still_formatted_in_one_call(tmp_path):
    doc, manifest = _long_doc(word_source.STYLE_PART)
    session = PartSession()
    word_source.style(doc, manifest, tmp_path, session, [], lambda *a: None)
    assert session.calls == [("Word reference formatting", word_source.STYLE_PART, 12000)]
    assert not list(tmp_path.glob("word_styles_part-*.json"))


def test_a_long_tab_is_formatted_in_parts_and_a_retry_pays_only_for_missing_parts(tmp_path):
    doc, manifest = _long_doc(1203)                       # parts of 500, 500 and 203 paragraphs
    first_try = PartSession(fail_at=2)
    with pytest.raises(word_source.ai.AIError):
        word_source.style(copy.deepcopy(doc), copy.deepcopy(manifest), tmp_path, first_try, [], lambda *a: None)
    assert [c[1] for c in first_try.calls] == [500, 500]
    assert (tmp_path / "word_styles_part-01.json").is_file() and not (tmp_path / "word_styles.json").exists()

    retry = PartSession()
    word_source.style(doc, manifest, tmp_path, retry, [], lambda *a: None)
    assert retry.calls == [("Word reference formatting 2/3", 500, 12000), ("Word reference formatting 3/3", 203, 12000)]
    roles = {b["source_id"]: b.get("role") for b in doc["blocks"]}
    assert [sid for sid, role in roles.items() if role == "section"] == ["p0000", "p0500", "p1000"]
    assert [b.get("text") for b in doc["blocks"]] == [u["texts"][0] for u in manifest["units"]]   # words unchanged
    saved = json.loads((tmp_path / "word_styles.json").read_text("utf-8"))
    assert len(saved["response"]["styles"]) == 3

    cached = PartSession()                                # everything saved: no call at all
    again, again_manifest = _long_doc(1203)
    word_source.style(again, again_manifest, tmp_path, cached, [], lambda *a: None)
    assert cached.calls == []


def test_a_part_may_only_choose_for_its_own_paragraphs(tmp_path):
    doc, manifest = _long_doc(800)
    with pytest.raises(word_source.ai.AIError, match="invalid Word formatting choice"):
        word_source.style(doc, manifest, tmp_path, PartSession(stray=True), [], lambda *a: None)
