"""Gujarati generation instructions, preserved packs and real PDF glyph rendering."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextvars
import io
import json

from pypdf import PdfReader

from note_studio import categories, engine_catalog, note_types, shortnotes_extensions
from note_studio.engines import shortnotes


def gujarati_notes():
    engine_catalog.put_shortnotes_on_path()
    from notesbot.prompts import ShortNotes
    return ShortNotes.model_validate({
        "output_language": "gujarati", "subject": "વિજ્ઞાન", "title": "પ્રકાશ અને પરાવર્તન",
        "lecture_no": "૧", "audience": "ધોરણ ૧૦", "faculty": "", "quote": "",
        "sections": [
            {"heading": "પ્રકાશનું પરાવર્તન", "points": [
                "પ્રકાશ સપાટી પરથી પાછો ફરે તેને **પરાવર્તન** કહે છે.",
                "આપાત કિરણ, પરાવર્તિત કિરણ અને અભિલંબ એક જ સમતલમાં હોય છે."],
             "key_terms": ["અભિલંબ: સપાટીને લંબ રેખા"], "icon": "bulb"},
            {"heading": "પરાવર્તનનો નિયમ", "points": ["આપાત કોણ અને પરાવર્તન કોણ સમાન હોય છે: $i = r$."],
             "where": [{"symbol": "$i$", "meaning": "આપાત કોણ"}, {"symbol": "$r$", "meaning": "પરાવર્તન કોણ"}]},
            {"heading": "સપાટ અરીસામાં પ્રતિબિંબ", "table": {
                "headers": ["ગુણધર્મ", "પ્રતિબિંબ"],
                "rows": [["કદ", "વસ્તુ જેટલું"], ["પ્રકાર", "આભાસી"], ["અભિમુખતા", "સીધું"]]},
             "notes": ["અરીસાની પાછળનું અંતર વસ્તુના આગળના અંતર જેટલું હોય છે."]},
        ],
    })


def test_gujarati_is_offered_for_both_shortnotes_categories(client):
    for category in ("neet_jee", "foundation"):
        page = client.get(f"/new?category={category}&type=concise_handdrawn").text
        assert 'value="gujarati"' in page and "Gujarati (ગુજરાતી)" in page
        assert 'name="automation_quality"' in page
    assert "gujarati" not in note_types.get("upsc_dcn")["languages"]
    assert shortnotes.run_settings({}, "foundation", "science", "gujarati", "").automation_quality


def test_language_upgrade_preserves_other_pack_data_and_keeps_a_backup():
    categories.seed()
    path = categories.packs_dir() / "foundation/category.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["languages"].remove("gujarati")
    meta["custom_metadata"] = {"keep": "operator's value"}
    path.write_text(json.dumps(meta), encoding="utf-8")
    before = path.read_bytes()
    assert "foundation" in categories.seed()
    assert path.with_name("category.json.before-gujarati.bak").read_bytes() == before
    assert categories.get("foundation")["custom_metadata"] == meta["custom_metadata"]
    assert "gujarati" in categories.get("foundation")["languages"]
    assert categories.seed() == []


def test_language_upgrade_respects_an_operators_custom_language_list():
    categories.seed()
    path = categories.packs_dir() / "neet_jee/category.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["languages"] = ["english"]
    path.write_text(json.dumps(meta), encoding="utf-8")
    categories.seed()
    assert categories.get("neet_jee")["languages"] == ["english"]


def test_prompt_extension_changes_only_gujarati_and_current_run_instructions():
    engine_catalog.put_shortnotes_on_path()
    from notesbot import prompts
    baseline = prompts.build_prompt("english", "by Teacher")
    shortnotes_extensions.install()
    assert prompts.build_prompt("english", "by Teacher") == baseline
    prompt = prompts.build_prompt("gujarati", "by Teacher")
    assert "GUJARATI OUTPUT" in prompt and "Gujarati script" in prompt
    assert "SAME language the lecture" not in prompt
    with shortnotes_extensions.instructions("Keep every source condition."):
        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            modified = pool.submit(context.run, prompts.build_prompt, "english", "by Teacher").result()
        assert "Keep every source condition." in modified
    assert prompts.build_prompt("english", "by Teacher") == baseline


def test_gujarati_pdf_contains_body_headings_table_labels_and_mixed_latin(caplog):
    notes = gujarati_notes()
    shortnotes_extensions.install()
    from sheets_bridge.generator import render_artifacts
    from sheets_bridge.pdf_fonts import localise_label
    rendered = render_artifacts(notes, signature="by Teacher", language="gujarati")
    reader = PdfReader(io.BytesIO(rendered))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    text = text.replace("\u200b", "")
    for expected in ("પ્રકાશ", "પરાવર્તન", "અભિલંબ", "ગુણધર્મ", "આભાસી", "પૃષ્ઠ", "Teacher"):
        assert expected in text, (expected, text)
    assert "KEY TERMS" not in text and "Where," not in text
    assert not any(ord(character) < 32 and character not in "\n\r\t" for character in text)
    assert localise_label("Where,", "gujarati") == "જ્યાં,"
    assert localise_label("KEY TERMS", "english") == "KEY TERMS"
    assert not any("missing glyph" in record.message.lower() for record in caplog.records)


def test_gujarati_pdf_text_is_detected_automatically(client, lecture_pdf):
    from sheets_bridge.coverage import detect_source_language
    from sheets_bridge.generator import render_artifacts

    source = render_artifacts(gujarati_notes(), signature="by Teacher", language="gujarati")
    assert detect_source_language(source) == "gujarati"
    assert detect_source_language(lecture_pdf) == ""
    assert detect_source_language(b"not a PDF") == ""


def test_auto_gujarati_source_reaches_generation_fonts_and_final_quality(client, fake_model):
    from sheets_bridge import generator, mapping, pw_transport

    source = generator.render_artifacts(gujarati_notes(), signature="by Teacher", language="gujarati")
    fake_model.FAKE_NOTES = gujarati_notes().model_dump()
    fake_model.KEEP_REQUESTS = True
    pw_transport.install(lambda: "offline-token")
    messages = []
    result = generator.generate_notes_pdf(source, language="auto", subject="science", signature="by Teacher",
        header=mapping.SheetHeader(subject="Science", chapter="Reflection", lecture="Lecture - 01"), log=messages.append)
    notes = json.loads(result.notes_json)
    assert notes["output_language"] == "gujarati" and notes["subject"] == "વિજ્ઞાન"
    assert notes["title"] == "પ્રકાશ અને પરાવર્તન" and notes["lecture_no"] == "વ્યાખ્યાન - 01"
    assert result.quality_report["status"] == "passed"
    assert "source looks like a Gujarati lecture - generating in Gujarati" in messages
    assert any("GUJARATI OUTPUT" in json.dumps(request) for request in fake_model.REQUESTS)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(result.pdf)).pages)
    text = text.replace("\u200b", "")
    assert "વિજ્ઞાન" in text and "પરાવર્તન" in text


def test_manual_automation_mode_makes_a_pdf_and_final_quality_report(client, fake_model, lecture_pdf, tmp_path):
    from sheets_bridge import pw_transport
    fake_model.KEEP_REQUESTS = True
    result = shortnotes.make_notes(
        lecture_pdf, "Lecture.pdf", tmp_path / "run",
        shortnotes.UploadSettings(teacher_name="Teacher", language="english", automation_quality=True,
                                  instructions="Keep every source condition."),
        ensure_proxy=lambda: pw_transport.install(lambda: "offline-token"),
    )
    assert result.state == "complete", (result.message, result.detail)
    assert result.quality_report["verdict"] == "passed"
    assert result.quality_report["reviewed_pages"] == list(range(1, result.pages + 1))
    assert any(call["kind"] == "quality" for call in fake_model.CALLS)
    assert any("Keep every source condition." in json.dumps(request) for request in fake_model.REQUESTS)
    assert (tmp_path / "run/notes.json").is_file()


def test_gujarati_queued_notes_use_automation_and_offer_the_quality_report(client, fake_model, lecture_pdf):
    from note_studio import db, job_queue
    fake_model.FAKE_NOTES = gujarati_notes().model_dump()
    response = client.post("/new", data={"category": "foundation", "note_type": "concise_handdrawn",
        "subject": "science", "language": "gujarati", "teacher_name": "Teacher",
        "automation_quality": "on", "logo_watermark": "on"},
        files=[("files", ("Gujarati lecture.pdf", lecture_pdf, "application/pdf"))], follow_redirects=False)
    assert response.status_code == 303
    assert job_queue.wait_idle(120)
    run = db.query("SELECT * FROM runs")[0]
    assert run["state"] == "complete", run["error"]
    page = client.get(f"/runs/{run['id']}").text
    assert "Gujarati (ગુજરાતી)" in page and "Quality check report" in page


def test_an_unresolved_quality_issue_is_shown_for_review(client, fake_model, lecture_pdf, monkeypatch):
    from note_studio import db, job_queue
    original_answer = fake_model.answer
    def answer(request):
        kind, body = original_answer(request)
        if kind == "quality":
            body["issues"] = [{"page": 1, "kind": "layout", "detail": "Synthetic review issue for this test."}]
        return kind, body
    monkeypatch.setattr(fake_model, "answer", answer)
    response = client.post("/new", data={"category": "neet_jee", "note_type": "concise_handdrawn",
        "subject": "physics", "language": "english", "teacher_name": "Teacher",
        "automation_quality": "on", "logo_watermark": "on"},
        files=[("files", ("Lecture.pdf", lecture_pdf, "application/pdf"))], follow_redirects=False)
    assert response.status_code == 303 and job_queue.wait_idle(120)
    run = db.query("SELECT * FROM runs")[0]
    assert run["state"] == "needs_attention"
    assert "Review the quality report" in run["message"]
    assert "Quality check report" in client.get(f"/runs/{run['id']}").text


def test_long_gujarati_with_inline_math_stays_within_the_pdf_page():
    notes = gujarati_notes()
    notes.sections[1].points.extend([
        "**આપાતકોણ ($i$)** આપાત કિરણ અને લંબ વચ્ચે માપવામાં આવે છે.",
        "**પરાવર્તનકોણ ($r$)** પરાવર્તિત કિરણ અને લંબ વચ્ચે માપવામાં આવે છે."])
    shortnotes_extensions.install()
    from sheets_bridge.generator import render_artifacts
    import pypdfium2 as pdfium
    from notesbot.pdfium_guard import PDFIUM_LOCK
    data = render_artifacts(notes, signature="by Teacher", language="gujarati")
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(data)
        try:
            for index in range(len(document)):
                page = document[index]
                try:
                    text = page.get_textpage()
                    try:
                        for position in range(text.count_chars()):
                            left, _bottom, right, _top = text.get_charbox(position)
                            assert -1 <= left <= right <= page.get_width() + 1
                    finally:
                        text.close()
                finally:
                    page.close()
        finally:
            document.close()


def test_english_metadata_in_gujarati_notes_is_reported_by_the_local_quality_check():
    notes = gujarati_notes()
    notes.title = "Reflection of light"
    notes.audience = "Class 10 Science"
    notes.sections[0].notes = ["Important! પ્રકાશનું પરાવર્તન"]
    shortnotes_extensions.install()
    from sheets_bridge.generator import render_artifacts
    from notesbot.final_quality import local_findings
    data = render_artifacts(notes, signature="by Teacher", language="gujarati")
    _pages, issues = local_findings(notes, data, "gujarati")
    assert len([issue for issue in issues if issue.kind == "language"]) == 3
