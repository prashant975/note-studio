import io

import pytest
from pypdf import PdfWriter

from notesbot.concise_policy import question_content, require_concise_notes
from notesbot.errors import NotesCoverageError, NotesQualityError
from notesbot.final_quality import QUALITY_VERSION, finalize_notes_pdf
from notesbot.gemini_client import _generation_schema_json
from notesbot.output_naming import output_name
from notesbot.prompts import Section, ShortNotes, WorkedExample, build_prompt
from notesbot.source_coverage import CoverageReview, ensure_source_coverage


def source_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def candidate(**kwargs):
    return ShortNotes(title="Cells", subject="Biology", lecture_no="1",
                      sections=[Section(heading="Cells", source_local_page=1, **kwargs)])


@pytest.mark.parametrize("source, expected", [
    ("L01 Class Notes.pdf", "L01 Concise Notes.pdf"),
    ("पुष्पी पादपों में लैंगिक प्रजनन 02  Class Notes.pdf", "पुष्पी पादपों में लैंगिक प्रजनन 02 Concise Notes.pdf"),
    ("पुष्पी पादपों में लैंगिक प्रजनन 02  Class Notes - Concise Notes.pdf", "पुष्पी पादपों में लैंगिक प्रजनन 02 Concise Notes.pdf"),
    ("Some Basic Concepts of Chemistry 01 Class Notes.pdf", "Some Basic Concepts of Chemistry 01 Concise Notes.pdf"),
    ("L01 Class Notes - Short Notes (1).pdf", "L01 Concise Notes.pdf"),
    ("L01 Class Notes - SHORT NOTES.PDF", "L01 Concise Notes.pdf"),
    ("L01 ClassNotes.pdf", "L01 Concise Notes.pdf"),
    ("भौतिकी - Concise Notes (2).pdf", "भौतिकी Concise Notes.pdf"),
    ("Lecture - Short Notes - Concise Notes.pdf", "Lecture Concise Notes.pdf"),
    ("Circles 06.pdf", "Circles 06 Concise Notes.pdf"),
    ("Thermodynamics 01  Class Notes  Physical Chemistry By Sudhanshu Kumar Sir.pdf",
     "Thermodynamics 01  Concise Notes  Physical Chemistry By Sudhanshu Kumar Sir.pdf"),
    ("Vectors 07  Class Notes  Physics By Abhishek Verma Sir Concise Notes.pdf",
     "Vectors 07  Concise Notes  Physics By Abhishek Verma Sir.pdf"),
    ("", "Lecture Concise Notes.pdf"),
])
def test_saved_names_replace_old_suffix_and_are_idempotent(source, expected):
    assert output_name(source) == expected
    assert output_name(expected) == expected


def test_long_web_name_keeps_concise_suffix():
    from notesbot.web_service import sanitize_pdf_filename
    name = output_name("ग" * 110 + ".pdf", max_stem_length=90)
    assert name.endswith(" Concise Notes.pdf")
    assert sanitize_pdf_filename(name) == name


def test_outputs_named_by_earlier_builds_are_still_recognised():
    from notesbot.output_naming import legacy_output_names
    source = "Thermodynamics 01  Class Notes  Physical Chemistry By Sudhanshu Kumar Sir.pdf"
    assert legacy_output_names(source) == (
        "Thermodynamics 01  Class Notes  Physical Chemistry By Sudhanshu Kumar Sir - Concise Notes.pdf",
        "Thermodynamics 01  Class Notes  Physical Chemistry By Sudhanshu Kumar Sir Concise Notes.pdf",
    )
    # a trailing label gives the same current name as before: only the older form is left
    assert legacy_output_names("Circles 06  Class Notes.pdf") == ("Circles 06  Class Notes - Concise Notes.pdf",)


def test_outputs_named_before_the_rename_are_still_recognised():
    from notesbot.output_naming import legacy_output_name
    assert legacy_output_name("Circles 06  Class Notes.pdf") == "Circles 06  Class Notes - Concise Notes.pdf"
    assert legacy_output_name("Circles 06  Class Notes - Concise Notes.pdf") == "Circles 06  Class Notes - Concise Notes.pdf"


@pytest.mark.parametrize("text", [
    "Question 1: Which tissue carries impulses?",
    "Q.2: Find the force.",
    "प्रश्न २: बल ज्ञात कीजिए।",
    "Answer: Option (C)",
    "Solution:\n1. Substitute the values.",
])
def test_questions_cannot_hide_in_theory_fields(text):
    notes = candidate(points=[text])
    assert question_content(notes)
    with pytest.raises(NotesQualityError):
        require_concise_notes(notes)


def test_scientific_solutions_and_processes_are_allowed():
    notes = candidate(points=["**Solution:** A homogeneous mixture.",
                              "Blood, bone and cartilage are connective tissues."],
                      flow=["Ingestion", "Digestion", "Absorption"])
    require_concise_notes(notes)
    assert not question_content(notes)


def test_review_repairs_qa_and_preserves_unique_theory_even_when_auditor_misses_it():
    before = candidate(points=["Cells are units of life."], worked_examples=[
        WorkedExample(statement="Can cells live outside the human body?",
                      steps=["They can survive under suitable culture conditions."], result="Yes")])
    after = candidate(points=["Cells are units of life.",
                              "Human cells can survive outside the body under suitable culture conditions."])
    passed = CoverageReview(reviewed_pages=[1], unreadable_pages=[], issues=[])
    responses = iter([passed, after])
    prompts = []
    def generate(prompt, schema, max_tokens):
        prompts.append(prompt)
        return next(responses)
    current = ensure_source_coverage(source_pdf(), before, language="english", generate=generate)
    assert not current.sections[0].worked_examples
    assert "culture conditions" in current.sections[0].points[1]
    assert len(prompts) == 2                     # review + repair, no confirmation
    assert "NOT a coverage gap" in prompts[0]
    assert "remove questions and solutions" in prompts[1]


def test_repair_that_keeps_questions_still_ships_after_one_repair():
    """Questions left after the one repair are shipped, not fought over: the
    final quality check records them as an open issue."""
    notes = candidate(worked_examples=[WorkedExample(statement="Find x.", result="2")])
    passed = CoverageReview(reviewed_pages=[1], unreadable_pages=[], issues=[])
    responses = iter([passed, notes])
    calls = []
    def generate(prompt, schema, max_tokens):
        calls.append(schema)
        return next(responses)
    result = ensure_source_coverage(source_pdf(), notes, language="english", generate=generate)
    assert result.sections[0].worked_examples                 # still there, still shipped
    assert calls == [CoverageReview, ShortNotes]


def test_final_pdf_ships_with_structured_questions_noted_even_if_model_approves():
    from test_final_quality import pdf_for, review
    notes = candidate(points=["Cells are units of life."], worked_examples=[WorkedExample(statement="Define a cell.")])
    _, _, receipt = finalize_notes_pdf(pdf_for(), notes, language="english", render=pdf_for, generate=lambda *_: review())
    assert receipt["verdict"] == "shipped_with_issues" and receipt["open_issues"]


def test_old_quality_receipt_cannot_approve_theory_only_notes():
    from notesbot.final_quality import digest, valid_receipt
    raw, pdf = b"{}", b"%PDF"
    receipt = dict(status="passed", version="final-pdf-quality-v1", notes_sha256=digest(raw),
                   pdf_sha256=digest(pdf), reviewed_pages=[1])
    assert not valid_receipt(raw, pdf, receipt)
    assert QUALITY_VERSION != receipt["version"]


def test_concise_map_recovery_preserves_missing_formula_without_repeating_theory():
    from notesbot.gemini_client import _ensure_mindmap
    from notesbot.prompts import Branch, MindMap
    notes = candidate(points=["Source definition."])
    notes.mindmap = MindMap(center="Cells", branches=[Branch(label="Cells", children=["$A=B$"])])
    _ensure_mindmap(notes, include_revision_map=False)
    assert notes.sections[0].points == ["Source definition.", "$A=B$"]
    assert notes.mindmap is None
    first = notes.model_dump()
    _ensure_mindmap(notes, include_revision_map=False)
    assert notes.model_dump() == first


def test_generation_repairs_excluded_qa_before_visuals_or_return(monkeypatch):
    from notesbot import gemini_client, source_coverage
    from notesbot.generation_policy import GenerationRun
    before = candidate(worked_examples=[WorkedExample(statement="Define a cell.")])
    after = candidate(points=["Cells are units of life."])
    bundle = gemini_client.SequenceNoteBundle(notes=before, chunks=(), run=GenerationRun(deadline_at=float("inf"), max_images=0))
    monkeypatch.setattr(gemini_client, "_generate_sequence_note_bundle", lambda *_: bundle)
    observed = []
    def review(pdf, notes, **kwargs):
        observed.append(notes)
        return after
    monkeypatch.setattr(source_coverage, "ensure_source_coverage", review)
    monkeypatch.setattr(gemini_client.image_gen, "render_sequence_visuals", lambda notes, *_: observed.append(notes))
    monkeypatch.setattr(gemini_client, "_emit_generation_ledger", lambda *_a, **_k: None)
    assert gemini_client.generate_short_notes(source_pdf(), language="english") is after
    assert observed == [before, after]


def test_the_automation_switches_the_inner_question_review_off(monkeypatch):
    """The Sheets automation reviews every group itself; the generator's own
    review of question text only duplicated that call, unlogged."""
    from notesbot import gemini_client, source_coverage
    from notesbot.generation_policy import GenerationRun
    from notesbot.generation_profile import inline_source_check
    before = candidate(worked_examples=[WorkedExample(statement="Define a cell.")])
    bundle = gemini_client.SequenceNoteBundle(notes=before, chunks=(), run=GenerationRun(deadline_at=float("inf"), max_images=0))
    monkeypatch.setattr(gemini_client, "_generate_sequence_note_bundle", lambda *_: bundle)
    reviewed = []
    monkeypatch.setattr(source_coverage, "ensure_source_coverage", lambda pdf, notes, **_: reviewed.append(notes) or notes)
    monkeypatch.setattr(gemini_client.image_gen, "render_sequence_visuals", lambda *_: None)
    monkeypatch.setattr(gemini_client, "_emit_generation_ledger", lambda *_a, **_k: None)
    with inline_source_check(False):
        assert gemini_client.generate_short_notes(source_pdf(), language="english") is before
    assert reviewed == []


def test_generate_complete_notes_turns_the_inner_review_off_only_with_its_own_review(monkeypatch):
    from notesbot.generation_profile import inline_source_check_enabled
    from sheets_bridge import coverage
    seen = []
    def fake_group(group, **kwargs):
        seen.append(inline_source_check_enabled())
        return candidate(points=["Cells are units of life."]), None
    monkeypatch.setattr(coverage, "_generate_group", fake_group)
    monkeypatch.setattr("notesbot.source_coverage.consolidate_lecture", lambda _pdf, notes, **_: notes)
    coverage.generate_complete_notes(source_pdf(), language="english", subject_hint="", verify_source=True)
    coverage.generate_complete_notes(source_pdf(), language="english", subject_hint="", verify_source=False)
    assert seen == [False, True]


@pytest.mark.parametrize("language", ["english", "hindi"])
def test_generation_and_schema_exclude_qa_in_both_languages(language):
    prompt = build_prompt(language, "")
    assert "Always leave worked_examples empty" in prompt
    assert "NOT a coverage gap" in prompt
    assert "retain the full question" not in prompt
    schema = _generation_schema_json(ShortNotes)
    assert schema["$defs"]["Section"]["properties"]["worked_examples"]["maxItems"] == 0


def test_english_notes_lose_bracketed_hindi_glosses_without_a_model_call():
    from sheets_bridge.coverage import notes_hindi_share, strip_hindi_glosses
    notes = candidate(points=["Pollination (परागण) moves pollen to the stigma.",
                              "Anther [परागकोष] holds pollen.", "$A=B$ (formula)"])
    notes.title = "Reproduction (जनन)"
    strip_hindi_glosses(notes)
    assert notes.sections[0].points == ["Pollination moves pollen to the stigma.",
                                        "Anther holds pollen.", "$A=B$ (formula)"]
    assert notes.title == "Reproduction"
    assert notes_hindi_share(notes) == 0.0


def test_english_notes_with_hindi_sentences_are_asked_again_once(monkeypatch):
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    answers = iter([
        ShortNotes(lecture_no="", subject="Biology", title="L", sections=[
            Section(heading="परागण", points=["परागकण वर्तिकाग्र तक पहुँचते हैं।"] * 3)]),
        ShortNotes(lecture_no="", subject="Biology", title="L", sections=[
            Section(heading="Pollination", points=["Pollen grains reach the stigma."] * 3)]),
    ])
    prompts = []

    def fake_generate(pdf_bytes, instruction, language, subject_hint):
        prompts.append(instruction)
        return next(answers)

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    lines = []
    group = coverage.split_pages(source_pdf(), 1)[0]
    notes, _ = coverage._generate_group(group, instruction="english", language="english",
                                        subject_hint="", emit=lines.append, allow_split=0)
    assert notes.sections[0].heading == "Pollination"
    assert len(prompts) == 2 and "English only" in prompts[1]
    assert any("asking again in English (1/1)" in line for line in lines)


def test_clean_english_notes_cost_no_extra_call(monkeypatch):
    from notesbot.prompts import Section, ShortNotes
    from sheets_bridge import coverage

    calls = []

    def fake_generate(*_a):
        calls.append(1)
        return ShortNotes(lecture_no="", subject="Biology", title="L", sections=[
            Section(heading="Pollination (परागण)", points=["Pollen grains reach the stigma."] * 3)])

    monkeypatch.setattr("notesbot.gemini_client.generate_short_notes", fake_generate)
    group = coverage.split_pages(source_pdf(), 1)[0]
    notes, _ = coverage._generate_group(group, instruction="", language="english",
                                        subject_hint="", emit=lambda *_: None, allow_split=0)
    assert calls == [1]
    assert notes.sections[0].heading == "Pollination"


def _topic_notes(count):
    return ShortNotes(lecture_no="1", subject="Biology", title="Plant Reproduction", sections=[
        Section(heading=f"Topic {i}", source_local_page=1,
                points=[f"Topic {i} fact one is taught here.", f"Topic {i} fact two is taught here."])
        for i in range(1, count + 1)])


def test_a_lecture_with_several_topics_gets_a_revision_map_on_the_rendered_copy_only():
    from sheets_bridge.generator import with_revision_map
    notes = _topic_notes(4)
    shown = with_revision_map(notes)
    assert shown.mindmap is not None and len(shown.mindmap.branches) >= 3
    assert notes.mindmap is None                              # stored notes untouched
    assert [s.model_dump() for s in shown.sections] == [s.model_dump() for s in notes.sections]


def test_short_or_disabled_lectures_get_no_revision_map(monkeypatch):
    from sheets_bridge.generator import with_revision_map
    assert with_revision_map(_topic_notes(2)).mindmap is None
    monkeypatch.setenv("SHEETS_BRIDGE_MINDMAP", "off")
    assert with_revision_map(_topic_notes(5)).mindmap is None


def test_rendered_pdf_with_a_map_still_passes_the_final_check(monkeypatch):
    from sheets_bridge import pdf_branding, pdf_fonts
    from sheets_bridge.generator import render_artifacts
    # Branding and fonts patch the PDF renderer process-wide; keep this test
    # from leaking them into the layout tests that run after it.
    monkeypatch.setattr(pdf_branding, "install", lambda: True)
    monkeypatch.setattr(pdf_fonts, "install", lambda: None)
    monkeypatch.setattr(pdf_fonts, "set_render_language", lambda _lang: None)
    from test_final_quality import review
    import re as _re
    notes = _topic_notes(4)
    seen = []

    def reviewer(files, prompt, *_):
        pages = int(_re.search(r"FINAL NOTES pages: (\d+)", prompt).group(1))
        seen.append(prompt)
        return review(pages=range(1, pages + 1))

    rendered = []

    def render(current):
        from sheets_bridge.generator import with_revision_map
        rendered.append(with_revision_map(current).mindmap is not None)
        return render_artifacts(current, signature="PW", language="english")

    _, _, receipt = finalize_notes_pdf(source_pdf(), notes, language="english",
                                       render=render, generate=reviewer)
    assert rendered == [True]
    assert notes.mindmap is None
    assert receipt["verdict"] == "passed" and receipt["open_issues"] == {}
    assert "mind map" in seen[0]



def test_lean_requests_use_a_much_smaller_prompt_and_schema():
    import json
    from notesbot import config
    from notesbot.generation_profile import lean_requests
    from sheets_bridge.pw_openai import strict_schema
    args = dict(language="hindi", signature=config.TEACHER_SIGNATURE, subject_hint="biology",
                part_note="Record source_local_page and source_order.", first_chunk=True)
    full_prompt = build_prompt(**args)
    full_schema = json.dumps(strict_schema(_generation_schema_json(ShortNotes)))
    with lean_requests():
        lean_prompt = build_prompt(**args)
        lean_schema = strict_schema(_generation_schema_json(ShortNotes))
    assert len(lean_prompt) < 0.6 * len(full_prompt)
    assert len(json.dumps(lean_schema)) < 0.6 * len(full_schema)
    # the rules that matter are still there
    for rule in ("THEORY ONLY", "source_local_page", "**double asterisks**", "DOUBLE backslash",
                 "Devanagari", "Return ONLY the structured data requested."):
        assert rule in lean_prompt, rule
    assert "<<" not in lean_prompt
    assert "\\frac{h}{2\\pi}" in lean_prompt           # single backslashes once rendered
    # the fields the automation never uses are gone from the schema...
    assert "mindmap" not in lean_schema["properties"]
    assert not {"MindMap", "Branch", "WorkedExample", "Visual"} & set(lean_schema["$defs"])
    section = lean_schema["$defs"]["Section"]["properties"]
    assert "worked_examples" not in section and "visual" not in section
    assert {"graph", "icon", "source_local_page", "source_order"} <= set(section)
    assert "microscope" in json.dumps(section["icon"])        # nullable: nested one level
    # ...and outside lean mode nothing changed
    assert "mindmap" in _generation_schema_json(ShortNotes)["properties"]


def test_a_lean_answer_still_loads_into_the_unchanged_notes_model():
    answer = {"lecture_no": "", "subject": "", "title": "", "audience": "", "quote": "",
              "sections": [{"heading": "Cells", "source_local_page": 1, "source_order": 0,
                            "points": ["Cells are units of life."], "tricks": [], "table": None,
                            "flow": [], "graph": None, "icon": "", "where": [], "notes": [],
                            "compare": None, "key_terms": []}]}
    notes = ShortNotes.model_validate(answer)
    assert notes.mindmap is None
    assert notes.sections[0].worked_examples == [] and notes.sections[0].visual is None


def test_the_automation_prompt_names_the_usual_unsupported_additions():
    from notesbot.prompts import _LEAN_PROMPT_TEMPLATE

    prompt = " ".join(_LEAN_PROMPT_TEMPLATE.split())
    for rule in ("if you cannot point to one, leave it out",
                 "a definition for a term the teacher only named",
                 "never adds a new fact"):
        assert rule in prompt


def test_the_final_check_no_longer_claims_the_lecture_was_checked():
    from notesbot import final_quality

    assert "already checked against these notes" not in final_quality._PROMPT
    assert "Never add or complete" in final_quality._PROMPT
