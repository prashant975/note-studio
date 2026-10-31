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
