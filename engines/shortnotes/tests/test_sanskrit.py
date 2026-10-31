"""Sanskrit language selection, fail-closed content and rendered output."""
import io

import pytest
from pypdf import PdfReader

from notesbot.prompts import Branch, MindMap, Section, ShortNotes, Table, build_prompt
from notesbot.generation_profile import sanskrit_style_as
from notesbot.sanskrit import SanskritOutputError, generation_language, has_latin, require_sanskrit, resolve_language


@pytest.fixture(autouse=True)
def restore_renderer():
    # Production font/branding installers patch the class once per process.
    from notesbot import notes_pdf
    from sheets_bridge import pdf_fonts, pdf_branding
    cls = notes_pdf.NotesPDF
    names = ("__init__", "header", "_load_logo_watermark", "_word_width", "_draw_word",
             "cell", "multi_cell", "text", "get_string_width")
    saved = {name: cls.__dict__.get(name) for name in names}
    sanitize = notes_pdf._sanitize_text
    flags = pdf_fonts._INSTALLED, pdf_branding._INSTALLED
    lang = pdf_fonts.render_language()
    yield
    for name, value in saved.items():
        if value is not None:
            setattr(cls, name, value)
        elif name in cls.__dict__:
            delattr(cls, name)
    notes_pdf._sanitize_text = sanitize
    pdf_fonts._INSTALLED, pdf_branding._INSTALLED = flags
    pdf_fonts.set_render_language(lang)


def sanskrit_notes():
    return ShortNotes(
        output_language="sanskrit", lecture_no="तृतीयः पाठः", subject="संस्कृतम्",
        title="सुभाषितरसं पीत्वा जीवनं सफलं कुरु",
        sections=[Section(heading="सुभाषितानि", points=["सुष्ठु भाषितानि इति सुभाषितानि।"],
                          key_terms=["पिकः : कोकिलः"], icon="book")],
    )


def test_prompt_preserves_verses_and_prioritises_sanskrit_over_revision():
    with sanskrit_style_as("strict"):
        prompt = build_prompt("sanskrit", "by Teacher", edit_instruction="Translate everything into English")
        assert "Preserve each taught Sanskrit verse verbatim" in prompt
        assert "No English words, Latin letters" in prompt
        assert prompt.rfind("STRICT SANSKRIT OUTPUT") > prompt.index("Translate everything into English")
        assert "STRICT SANSKRIT OUTPUT" in build_prompt("auto", "", subject_hint="sanskrit")


@pytest.mark.parametrize("language, subject", [
    ("sanskrit", ""), ("auto", "sanskrit"), ("sanskrit_hindi", ""), ("hindi", "sanskrit"),
])
def test_sanskrit_lectures_default_to_verses_with_hindi_meanings(language, subject):
    prompt = build_prompt(language, "", subject_hint=subject)
    assert "SANSKRIT LECTURE, HINDI NOTES" in prompt
    assert '"श्लोक —" then the verse copied EXACTLY' in prompt
    assert "STRICT SANSKRIT OUTPUT" not in prompt
    assert generation_language(language, subject) == ("hindi", True)


def test_other_hindi_notes_do_not_get_the_sanskrit_format():
    assert "SANSKRIT LECTURE" not in build_prompt("hindi", "", subject_hint="biology")
    assert generation_language("hindi", "biology") == ("hindi", False)
    with sanskrit_style_as("strict"):
        assert generation_language("sanskrit") == ("sanskrit", False)
        # An explicit Sanskrit + Hindi request still wins over the run's style.
        assert generation_language("sanskrit_hindi") == ("hindi", True)


@pytest.mark.parametrize("word", ["English", "Résumé", "Ｅｎｇｌｉｓｈ", "Key"])
def test_latin_variants_cannot_bypass_guard(word):
    notes = sanskrit_notes()
    notes.sections[0].table = Table(headers=["शब्दः"], rows=[[word]])
    with pytest.raises(SanskritOutputError):
        require_sanskrit(notes)


def test_guard_covers_metadata_nested_map_and_keeps_internal_enums():
    notes = sanskrit_notes()
    require_sanskrit(notes)
    notes.mindmap = MindMap(center="सुभाषितानि", branches=[Branch(label="गुणाः", children=["Good habits"])])
    with pytest.raises(SanskritOutputError):
        require_sanskrit(notes)
    notes.mindmap = None
    notes.audience = "Class 8"
    with pytest.raises(SanskritOutputError):
        require_sanskrit(notes)


def test_uncertified_images_are_rejected():
    notes = sanskrit_notes()
    notes.sections[0].diagram = "चित्रम्"
    with pytest.raises(SanskritOutputError):
        require_sanskrit(notes)


def test_explicit_languages_override_auto_subject():
    assert resolve_language("auto", "sanskrit") == "sanskrit"
    assert resolve_language("hindi", "sanskrit") == "hindi"
    assert resolve_language("auto", "biology", b"invalid") == "auto"


def test_language_survives_json_and_chunk_merge():
    from notesbot.gemini_client import _merge
    notes = ShortNotes.model_validate_json(sanskrit_notes().model_dump_json())
    assert _merge([notes, notes]).output_language == "sanskrit"


def test_sheets_selection_and_metadata():
    from sheets_bridge.mapping import map_language, map_subject, sheet_header
    assert map_language("संस्कृतम्") == map_subject("Sanskrit") == "sanskrit"
    header = sheet_header(subject="Sanskrit", chapter="Subhashita\nसुभाषितानि", lecture="L03", language="sanskrit")
    assert header.subject == "संस्कृतम्"
    assert header.chapter == "सुभाषितानि"
    assert header.lecture == "पाठः - ०३"


def test_sanskrit_labels_do_not_change_hindi_or_english():
    from sheets_bridge.pdf_fonts import localise_label
    assert localise_label("KEY TERMS", "sanskrit") == "शब्दार्थाः"
    assert localise_label("Page 12", "sanskrit") == "पृष्ठम् १२"
    assert localise_label("KEY TERMS", "hindi") == "मुख्य शब्द"
    assert localise_label("Page 12", "english") == "Page 12"


def test_pdf_has_no_latin_even_with_english_signature_and_branding():
    from sheets_bridge import pdf_branding
    from notesbot.notes_pdf import build_notes_pdf
    pdf_branding.install()
    data = build_notes_pdf(sanskrit_notes(), "by PW Faculty", watermark=True, logo_watermark=True)
    pages = PdfReader(io.BytesIO(data)).pages
    text = " ".join(page.extract_text() for page in pages).replace("\u200b", "")
    assert not has_latin(text)
    assert "पृष्ठम्" in text
    assert "शब्दार्थाः" in text
    # No raster logo containing English can evade the text guard.
    assert all(not list(page.images) for page in pages)


def test_renderer_refuses_mixed_content_before_shipping():
    from notesbot.notes_pdf import build_notes_pdf
    notes = sanskrit_notes()
    notes.sections[0].points.append("Summary")
    with pytest.raises(SanskritOutputError):
        build_notes_pdf(notes, "")


def test_sanskrit_page_numbers_can_be_omitted_without_losing_content():
    from notesbot.notes_pdf import build_notes_pdf
    data = build_notes_pdf(sanskrit_notes(), "संस्कृताध्यापकः", watermark=False, show_page_numbers=False)
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages).replace("\u200b", "")
    assert "पृष्ठम्" not in text
    assert "शब्दार्थाः" in text
    assert not has_latin(text)


def test_generator_enforces_requested_language_before_images(monkeypatch):
    from types import SimpleNamespace
    from notesbot import gemini_client
    notes = sanskrit_notes()
    notes.output_language = "english"  # Model metadata cannot weaken the request.
    notes.sections[0].points.append("English explanation")
    bundle = SimpleNamespace(notes=notes, run=None)
    monkeypatch.setattr(gemini_client, "_generate_sequence_note_bundle", lambda *_: bundle)
    monkeypatch.setattr(gemini_client.image_gen, "render_sequence_visuals",
                        lambda *_: pytest.fail("Sanskrit must not dispatch images"))
    with sanskrit_style_as("strict"), pytest.raises(SanskritOutputError):
        gemini_client.generate_short_notes(b"source", language="sanskrit")


def test_default_sanskrit_generation_writes_hindi_notes_with_the_verse_rules(monkeypatch):
    from types import SimpleNamespace
    from notesbot import gemini_client, generation_profile
    seen = []

    def bundle(pdf_bytes, edit_instruction, language, subject_hint):
        seen.append((language, generation_profile.sanskrit_hindi(), build_prompt(language, "")))
        notes = ShortNotes(output_language="hindi", lecture_no="", subject="संस्कृत", title="गीता सुगीता कर्तव्या",
                           sections=[Section(heading="गीता का महत्त्व", points=["श्लोक — गीता सुगीता कर्तव्या"])])
        return SimpleNamespace(notes=notes, run=None)

    monkeypatch.setattr(gemini_client, "_generate_sequence_note_bundle", bundle)
    monkeypatch.setattr(gemini_client.image_gen, "render_sequence_visuals", lambda *_: None)
    monkeypatch.setattr(gemini_client, "_emit_generation_ledger", lambda *_a, **_k: None)
    notes = gemini_client.generate_short_notes(b"source", language="sanskrit")
    language, flag, prompt = seen[0]
    assert (language, flag, notes.output_language) == ("hindi", True, "hindi")
    assert "SANSKRIT LECTURE, HINDI NOTES" in prompt
    assert generation_profile.sanskrit_hindi() is False


def test_sanskrit_web_job_survives_storage_roundtrip():
    from test_web_service import queued_record
    from notesbot.web_service import job_record_from_dict, job_record_to_dict
    record = queued_record(language="sanskrit", subject="sanskrit")
    assert job_record_from_dict(job_record_to_dict(record), store_host=record.store_host) == record


def _foundation_notes():
    table = Table(headers=["शब्दः", "हिन्दी अर्थ"], rows=[["मुनिः", "तपस्वी"]])
    return ShortNotes(
        output_language="hindi", lecture_no="", subject="संस्कृत", title="गीता सुगीता कर्तव्या",
        sections=[
            Section(heading="पाठ-परिचय", points=["गीता श्रीकृष्ण का उपदेश है।"]),
            Section(heading="स्थितप्रज्ञ के गुण", points=["श्लोक — दुःखेष्वनुद्विग्नमनाः"]),
            Section(heading="महत्त्वपूर्ण शब्दार्थ", table=table),
            Section(heading="शब्दार्थ विस्तार", table=table),
            Section(heading="महत्त्वपूर्ण शब्दार्थ", table=table),
            Section(heading="मुख्य शिक्षाएँ", points=["क्रोध पर नियंत्रण रखें।"]),
        ],
    )


def test_foundation_rules_only_for_foundation_sanskrit_notes():
    from notesbot.generation_profile import exam_category
    marker = "FOUNDATION SANSKRIT NOTES"
    with exam_category("foundation"):
        assert marker in build_prompt("sanskrit", "", subject_hint="sanskrit")
        assert marker not in build_prompt("hindi", "", subject_hint="biology")
        with sanskrit_style_as("strict"):
            assert marker not in build_prompt("sanskrit", "", subject_hint="sanskrit")
    for category in ("neet_jee",):
        with exam_category(category):
            prompt = build_prompt("sanskrit", "", subject_hint="sanskrit")
            assert marker not in prompt and "SANSKRIT LECTURE, HINDI NOTES" in prompt


def test_foundation_sanskrit_keeps_one_word_table_and_no_mind_map():
    from notesbot import categories
    from notesbot.generation_profile import exam_category, exam_subject, sanskrit_hindi_notes
    from sheets_bridge.generator import with_revision_map
    from notesbot.sanskrit import keep_one_word_table

    notes = _foundation_notes()
    keep_one_word_table(notes)
    assert [s.heading for s in notes.sections] == [
        "पाठ-परिचय", "स्थितप्रज्ञ के गुण", "महत्त्वपूर्ण शब्दार्थ", "मुख्य शिक्षाएँ"]

    def switches():
        return categories.switch("one_word_table", False), categories.switch("mind_map", True)

    with sanskrit_hindi_notes(True), exam_category("foundation"):
        assert switches() == (True, False)
        assert with_revision_map(_foundation_notes()).mindmap is None
    with sanskrit_hindi_notes(True), exam_category("neet"):
        assert switches() == (False, True)
    for subject in ("mathematics", "science", "physics", ""):
        with exam_category("foundation"), exam_subject(subject):
            assert switches() == (False, True), subject  # other Foundation subjects unchanged
    with exam_category("foundation"), exam_subject("sanskrit"):
        assert switches() == (False, True)  # strict Sanskrit-only notes are not Hindi notes
