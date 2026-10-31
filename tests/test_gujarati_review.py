"""Reviewer corrections: source organisation, retention and real PDF pagination."""
import io

import pytest
from pypdf import PdfReader

from note_studio import engine_catalog, gujarati_editorial, gujarati_layout, shortnotes_extensions


@pytest.fixture
def model():
    engine_catalog.put_shortnotes_on_path()
    shortnotes_extensions.install()
    from notesbot import prompts
    return prompts


def candidate(model):
    return model.ShortNotes(title="સંચાલન", subject="વાણિજ્ય", lecture_no="", output_language="gujarati",
        sections=[model.Section(heading="પ્રકારો", points=["પ્રથમ પ્રકારનું વર્ણન."]),
                  model.Section(heading="અર્થ", points=["અર્થની વ્યાખ્યા.", "ચોક્કસ શરત સાથેનો નિયમ."]),
                  model.Section(heading="અર્થ અને લક્ષણો", points=["અર્થની વ્યાખ્યા.", "એક અનન્ય અપવાદ."])])


def review(groups=None, **kwargs):
    return gujarati_editorial.review_schema().model_validate({"reviewed_pages": [1, 2],
        "section_groups": groups if groups is not None else [
            {"section_indices": [1, 2], "source_page": 1, "source_order": 0, "evidence": "Same source topic."},
            {"section_indices": [0], "source_page": 2, "source_order": 0, "evidence": "Later source topic."}], **kwargs})


def test_move_and_merge_keeps_every_unique_fact_and_original_snapshot(model):
    notes = candidate(model)
    before = notes.model_dump_json()
    result = gujarati_editorial.apply_review(notes, review(), 2)
    assert [s.heading for s in result.sections] == ["અર્થ", "પ્રકારો"]
    assert result.sections[0].points == ["અર્થની વ્યાખ્યા.", "ચોક્કસ શરત સાથેનો નિયમ.", "એક અનન્ય અપવાદ."]
    assert notes.model_dump_json() == before
    assert not result._gujarati_editorial_issue


@pytest.mark.parametrize("indices", [[0, 1], [0, 1, 1], [-1, 0, 1, 2], [0, 1, 99]])
def test_incomplete_or_invalid_reordering_cannot_lose_a_topic(model, indices):
    notes = candidate(model)
    groups = [{"section_indices": indices, "source_page": 1, "source_order": 0, "evidence": "Proposed order."}]
    result = gujarati_editorial.apply_review(notes, review(groups), 2)
    assert result.model_dump() == notes.model_dump()
    assert result._gujarati_editorial_issue


def test_distinct_tables_visuals_and_signed_formulas_survive_grouping(model):
    notes = candidate(model)
    notes.sections[1].table = model.Table(headers=["A"], rows=[["one"]])
    notes.sections[2].table = model.Table(headers=["A"], rows=[["two"]])
    notes.sections[1].points.append("$a-b$")
    notes.sections[2].points.append("$b-a$")
    notes.sections[1].visual_ids = ["diagram-1"]
    notes.sections[2].visual_ids = ["diagram-2"]
    result = gujarati_editorial.apply_review(notes, review(), 2)
    assert len(result.sections) == 3
    assert [s.table.rows for s in result.sections if s.table] == [[["one"]], [["two"]]]
    assert {p for s in result.sections for p in s.points} >= {"$a-b$", "$b-a$"}
    assert [s.visual_ids for s in result.sections if s.visual_ids] == [["diagram-1"], ["diagram-2"]]


def test_exclusions_need_exact_text_valid_evidence_and_cannot_delete_retained_reference(model):
    notes = candidate(model)
    item = {"section_index": 1, "field": "points", "item_index": 0}
    edit = dict(**item, before="અર્થની વ્યાખ્યા.", source_page=1, evidence="Review evidence.")
    repeat = {"remove": {"section_index": 2, "field": "points", "item_index": 0},
              "covered_by": [item], "reason": "Exact same definition."}
    result = gujarati_editorial.apply_review(notes, review(exclusions=[edit], repetitions=[repeat]), 2)
    assert "અર્થની વ્યાખ્યા." in result.sections[0].points
    assert "એક અનન્ય અપવાદ." in result.sections[0].points
    stale = dict(edit, before="Not the actual string")
    result = gujarati_editorial.apply_review(notes, review(exclusions=[stale]), 2)
    assert "અર્થની વ્યાખ્યા." in result.sections[0].points


def test_valid_exclusion_and_source_correction_do_not_shift_each_others_indices(model):
    notes = candidate(model)
    notes.sections[1].points = ["અનાધારિત વધારાની વાત.", "વર્ષ 1912.", "અનન્ય શરત."]
    result = gujarati_editorial.apply_review(notes, review(
        exclusions=[dict(section_index=1, field="points", item_index=0, before=notes.sections[1].points[0],
                         source_page=1, evidence="This is not taught in the source.")],
        corrections=[dict(path="/sections/1/points/1", before="વર્ષ 1912.", after="વર્ષ 1911.",
                          source_page=1, evidence="The source explicitly gives 1911.")]), 2)
    assert result.sections[0].points[:2] == ["વર્ષ 1911.", "અનન્ય શરત."]


def test_source_term_correction_reaches_table_cells_and_preserves_table_structure(model):
    notes = candidate(model)
    notes.sections[1].table = model.Table(headers=["શબ્દ"], rows=[["જૂનો શબ્દ"]])
    result = gujarati_editorial.apply_review(notes, review(corrections=[dict(
        path="/sections/1/table/rows/0/0", before="જૂનો શબ્દ", after="મંજૂર શબ્દ",
        source_page=1, evidence="Exact terminology on the source page.")]), 2)
    assert result.sections[0].table.rows == [["મંજૂર શબ્દ"]]
    assert notes.sections[1].table.rows == [["જૂનો શબ્દ"]]


def test_unreadable_or_partial_source_review_changes_nothing(model):
    notes = candidate(model)
    result = gujarati_editorial.apply_review(notes, review(unreadable_pages=[2]), 2)
    assert result is notes
    assert result._gujarati_editorial_issue


def test_complete_removal_cannot_empty_a_lecture(model):
    notes = candidate(model)
    exclusions = [dict(section_index=i, field="points", item_index=j, before=value,
                       source_page=1, evidence="Proposed exclusion.")
                  for i, s in enumerate(notes.sections) for j, value in enumerate(s.points)]
    assert gujarati_editorial.apply_review(notes, review(exclusions=exclusions), 2) is notes
    assert notes.sections


def test_unfinished_source_review_is_visible_to_the_final_quality_check(model):
    from notesbot.final_quality import local_findings
    from sheets_bridge.generator import render_artifacts
    notes = gujarati_editorial.apply_review(candidate(model), review(unreadable_pages=[2]), 2)
    data = render_artifacts(notes, signature="by Teacher", language="gujarati")
    _, issues = local_findings(notes, data, "gujarati")
    assert any("source organisation review" in issue.detail for issue in issues)


def test_invalid_correction_cannot_change_language_or_unmatched_source_text(model):
    notes = candidate(model)
    changes = [dict(path="/output_language", before="gujarati", after="english", source_page=1, evidence="Invalid target."),
               dict(path="/sections/1/points/0", before="Stale text", after="New text", source_page=1, evidence="Stale target.")]
    result = gujarati_editorial.apply_review(notes, review(corrections=changes), 2)
    assert result.output_language == "gujarati"
    assert result.sections[0].points[0] == "અર્થની વ્યાખ્યા."
    assert result._gujarati_editorial_issue


def test_editorial_review_uses_source_attachment_in_one_existing_review_call(model, monkeypatch, lecture_pdf):
    from notesbot import gemini_client, source_coverage
    notes = candidate(model)
    calls = []

    def generate(files, prompt, schema, *args):
        calls.append((files, prompt, schema))
        count = len(PdfReader(io.BytesIO(lecture_pdf)).pages)
        return {"reviewed_pages": list(range(1, count + 1)), "section_groups": [
            dict(section_indices=[i], source_page=1, source_order=i, evidence="Source order.") for i in range(3)]}

    monkeypatch.setattr(gemini_client, "_generate_pdf_schema", generate)
    result = source_coverage.consolidate_lecture(lecture_pdf, notes, language="gujarati")
    assert len(calls) == 1 and calls[0][0][0].data == lecture_pdf
    assert "GUJARATI CHAPTER ORGANISATION" in calls[0][1]
    assert not result._gujarati_editorial_issue


def make_pdf(model):
    from notesbot.notes_pdf import NotesPDF
    pdf = NotesPDF(candidate(model), "by Teacher", watermark=False)
    pdf.add_page()
    pdf._use_full_width()
    return pdf


def texts(data):
    return [(p.extract_text() or "").replace("\u200b", "") for p in PdfReader(io.BytesIO(data)).pages]


def test_prose_numbers_and_us_are_text_but_formulas_and_scripts_remain_math(model):
    pdf = make_pdf(model)
    atoms = pdf._atoms(r"વર્ષ $1932$, દર $6\%$, પ્રમાણ $1:1$ અને $\mathrm{US}$.", 13, 14.5)
    assert not any(a.get("k") == "m" for a in atoms)
    assert any(a.get("k") == "m" for a in pdf._atoms(r"નિયમ $x^2-y^2$", 13, 14.5))
    assert gujarati_layout.prose_math("$$1932$$") == "$$1932$$"
    assert gujarati_editorial.clean_heading("12. અર્થ") == "અર્થ"
    assert gujarati_editorial.clean_heading("1.2 Topic") == "1.2 Topic"


def test_small_table_moves_with_header_and_first_row(model):
    pdf = make_pdf(model)
    pdf.set_y(pdf.page_break_trigger - 18)
    table = model.Table(headers=["યોજનાનું નામ", "વિગત"], rows=[
        ["યોજના PMJDY", "પ્રથમ પંક્તિની સંપૂર્ણ વિગત."], ["યોજના APY", "બીજી પંક્તિ."]])
    pdf._render_table(table)
    pages = texts(bytes(pdf.output()))
    assert "PMJDY" not in pages[0] and "યોજનાનું નામ" not in pages[0]
    assert "PMJDY" in pages[1] and "APY" in pages[1] and "યોજનાનું નામ" in pages[1]


def test_large_table_never_splits_name_from_other_cells(model):
    pdf = make_pdf(model)
    pdf.set_y(pdf.page_break_trigger - 25)
    table = model.Table(headers=["નામ", "વિગત"], rows=[
        [f"NAME{i:02}", "સંપૂર્ણ માહિતી અને ચોક્કસ શરતો. " * 12 + f" END{i:02}"] for i in range(12)])
    pdf._render_table(table)
    pages = texts(bytes(pdf.output()))
    assert len(pages) > 2
    for i in range(12):
        page = next(page for page in pages if f"NAME{i:02}" in page)
        assert f"END{i:02}" in page


def test_continued_glossary_has_one_heading_and_keeps_all_terms(model):
    pdf = make_pdf(model)
    pdf.set_y(pdf.page_break_trigger - 36)
    terms = [f"TERM{i:02}: " + "સંપૂર્ણ વ્યાખ્યા અને તેની ખાસ શરતો. " * 8 for i in range(14)]
    pdf._key_terms_box(terms)
    pages = texts(bytes(pdf.output()))
    assert len(pages) > 1
    assert sum(page.count("મહત્વપૂર્ણ શબ્દો") for page in pages) == 1
    assert all(sum(page.count(f"TERM{i:02}") for page in pages) == 1 for i in range(14))


def test_flow_is_kept_together_when_it_fits_a_fresh_page(model):
    pdf = make_pdf(model)
    pdf.set_y(pdf.page_break_trigger - 12)
    pdf._render_flow(["સંચાલક મંડળ", "જનરલ મેનેજર", "વિભાગીય અધિકારી", "સુપરવાઇઝર", "કામદારો"])
    pages = texts(bytes(pdf.output()))
    assert "સંચાલક મંડળ" not in pages[0]
    assert all(value in pages[1] for value in ["સંચાલક મંડળ", "કામદારો", "સુપરવાઇઝર"])


def test_short_first_section_and_tall_next_card_do_not_create_a_title_only_page(model):
    from sheets_bridge.generator import render_artifacts
    notes = candidate(model)
    notes.sections = [model.Section(heading="વ્યાખ્યા", points=["પ્રથમ વિભાગનો મુખ્ય મુદ્દો અહીં છે."]),
                      model.Section(heading="વિસ્તૃત લક્ષણો", points=[
                          f"લક્ષણ {i}: " + "અહીં અનન્ય શરતની સંપૂર્ણ વિગત છે. " * 12 for i in range(20)])]
    pages = texts(render_artifacts(notes, signature="by Teacher", language="gujarati"))
    assert "પ્રથમ વિભાગનો મુખ્ય મુદ્દો અહીં છે." in pages[0]
