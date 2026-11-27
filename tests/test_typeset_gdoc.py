"""Google Doc (tabs) source and writer: literal content, order, and exact request ranges. Offline."""
import io

import pytest
from PIL import Image

from note_studio.typeset import gdoc_source, gdoc_writer, source


def png(colour="navy", size=(40, 30)):
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def run(text, **style):
    return {"textRun": {"content": text, "textStyle": style}}


def para(*elements, named="NORMAL_TEXT", bullet=None, positioned=None):
    paragraph = {"elements": list(elements), "paragraphStyle": {"namedStyleType": named}}
    if bullet:
        paragraph["bullet"] = bullet
    if positioned:
        paragraph["positionedObjectIds"] = positioned
    return {"paragraph": paragraph}


def cell(*paragraphs, **style):
    return {"content": list(paragraphs), "tableCellStyle": style}


def image_object(uri="https://img/1", w=200, h=100):
    return {"inlineObjectProperties": {"embeddedObject": {
        "imageProperties": {"contentUri": uri},
        "size": {"width": {"magnitude": w, "unit": "PT"}, "height": {"magnitude": h, "unit": "PT"}}}}}


def nursing_tab():
    content = [
        {"sectionBreak": {}},
        para(run("Anatomical Position\n", bold=True, fontSize={"magnitude": 20, "unit": "PT"}), named="HEADING_1"),
        para(run("Definition:", bold=True), run(" The body stands erect, facing forward.\n")),
        para(run("Anatomical & Clinical Examples\n", bold=True)),
        para(run("Superior and inferior\n"), bullet={"listId": "L1", "nestingLevel": 0}),
        para(run("Cranial means toward the head\n"), bullet={"listId": "L1", "nestingLevel": 1}),
        para(run("Anterior and posterior\n"), bullet={"listId": "L1", "nestingLevel": 0}),
        para({"inlineObjectElement": {"inlineObjectId": "img1"}}, run("\n")),
        {"table": {"tableRows": [
            {"tableCells": [cell(para(run("Term\n"))), cell(para(run("Meaning\n")))]},
            {"tableCells": [cell(para(run("Dorsal\n"))), cell(para(run("Back side\n")),
                                                             para(run("of the body\n")))]}]}},
        {"table": {"tableRows": [{"tableCells": [cell(para(run("Remember: palms face forward.\n")))]}]}},
        para(run("Posterior chest auscultation: lower lobes of the lungs.\n")),
    ]
    return {"tabProperties": {"tabId": "t.0", "title": "Anatomical Position"}, "documentTab": {
        "body": {"content": content}, "inlineObjects": {"img1": image_object()},
        "lists": {"L1": {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}, {"glyphSymbol": "○"}]}}}}}


def document(*tabs):
    return {"title": "UNIT -1 A&P", "tabs": list(tabs)}


def test_tabs_in_reading_order_with_children():
    child = {"tabProperties": {"tabId": "t.c", "title": "Child"}, "documentTab": {"body": {"content": []}}}
    parent = {"tabProperties": {"tabId": "t.p", "title": "Parent"}, "documentTab": {}, "childTabs": [child]}
    last = {"tabProperties": {"tabId": "t.l", "title": "Last"}, "documentTab": {}}
    found = gdoc_source.all_tabs(document(parent, last))
    assert [(t["tab_id"], t["depth"], t["parent"]) for t in found] == [("t.p", 0, ""), ("t.c", 1, "t.p"), ("t.l", 0, "")]


def test_every_paragraph_list_item_cell_and_picture_is_kept_in_order(tmp_path):
    tab = gdoc_source.all_tabs(document(nursing_tab()))[0]
    doc, manifest = gdoc_source.extract(tab, tmp_path, lambda uri: png())
    kinds = [(b["kind"], b.get("role")) for b in doc["blocks"]]
    assert kinds == [("source_text", "title"), ("source_text", "paragraph"), ("source_text", "subsection"),
                     ("source_text", "paragraph"), ("source_text", "paragraph"), ("source_text", "paragraph"),
                     ("source_panel", None), ("source_table", None), ("source_box", None), ("source_text", "paragraph")]
    assert manifest["counts"] == {"paragraphs": 4, "list_items": 3, "tables": 1, "boxes": 1, "images": 1}
    definition = doc["blocks"][1]
    assert definition["text"] == "Definition: The body stands erect, facing forward."
    assert definition["spans"] == [[0, 11, {"bold": True}]]
    assert [b["list"]["level"] for b in doc["blocks"][3:6]] == [0, 1, 0]
    table = doc["blocks"][7]
    assert table["columns"] == ["Term", "Meaning"] and table["rows"] == [["Dorsal", "Back side\nof the body"]]
    assert doc["blocks"][8]["parts"] == ["Remember: palms face forward."]
    picture = doc["blocks"][6]
    assert (tmp_path / picture["image"]).read_bytes() == png() and picture["object_id"] == "img1"
    texts = [t for u in manifest["units"] for t in u["texts"]]
    assert texts[0] == "Anatomical Position" and "of the body" in texts and texts[-1].startswith("Posterior chest")


def test_a_code_blocks_hidden_markers_are_not_text_but_its_words_stay(tmp_path):
    # Google Docs marks a code block's start and end with U+E907, its own run, which Docs will not insert
    # (row 411 of the Nursing sheet). Leading spaces then go as for any paragraph.
    tab = nursing_tab()
    tab["documentTab"]["body"]["content"].append(
        para(run("", bold=True), run("      [ Standard Precautions ] (ALL patients)\n")))
    tab["documentTab"]["body"]["content"].append(para(run("|\n")))
    doc, manifest = gdoc_source.extract(gdoc_source.all_tabs(document(tab))[0], tmp_path, lambda uri: png())
    texts = [b["text"] for b in doc["blocks"] if b["kind"] == "source_text"]
    assert texts[-2:] == ["[ Standard Precautions ] (ALL patients)", "|"]
    assert not any("" in t for u in manifest["units"] for t in u["texts"])


@pytest.mark.parametrize("element, words", [
    ({"equation": {}}, "an equation"),
    ({"footnoteReference": {"footnoteNumber": "1"}}, "a footnote")])
def test_unreadable_content_fails_before_any_ai_call(tmp_path, element, words):
    tab = nursing_tab()
    tab["documentTab"]["body"]["content"].append(para(element))
    with pytest.raises(source.SourceError, match=words):
        gdoc_source.extract(gdoc_source.all_tabs(document(tab))[0], tmp_path, lambda uri: png())


def test_a_drawing_fails_instead_of_being_dropped(tmp_path):
    tab = nursing_tab()
    tab["documentTab"]["inlineObjects"]["img1"] = {"inlineObjectProperties": {"embeddedObject": {
        "embeddedDrawingProperties": {}}}}
    with pytest.raises(source.SourceError, match="Google Drawing"):
        gdoc_source.extract(gdoc_source.all_tabs(document(tab))[0], tmp_path, lambda uri: png())


def test_layout_table_with_picture_keeps_its_content_in_reading_order(tmp_path):
    tab = nursing_tab()
    tab["documentTab"]["body"]["content"] = [{"table": {"tableRows": [{"tableCells": [
        cell(para({"inlineObjectElement": {"inlineObjectId": "img1"}}, run("\n"))),
        cell(para(run("Musculoskeletal:\n", bold=True)), para(run("The spine is posterior to the heart.\n"),
                                                               bullet={"listId": "L1", "nestingLevel": 0}))]}]}}]
    doc, manifest = gdoc_source.extract(gdoc_source.all_tabs(document(tab))[0], tmp_path, lambda uri: png())
    assert [b["kind"] for b in doc["blocks"]] == ["source_panel", "source_text", "source_text"]
    assert manifest["attention"] and "layout table" in manifest["attention"][0]


def test_utf16_lengths_count_like_google_docs():
    assert gdoc_source.u16("abc") == 3
    assert gdoc_source.u16("µ→") == 2
    assert gdoc_source.u16("🫀") == 2


# ----------------------------------------------------------------- writer
class FakeDocs:
    """Google Docs' body index model for text, lists and inline pictures: one tab,
    index 0 the section break, the body starting with one empty paragraph."""

    def __init__(self):
        self.text = "\x00\n"
        self.styled = []

    def apply(self, request):
        (kind, body), = request.items()
        if kind == "insertText":
            at = body["location"]["index"]
            assert 1 <= at < len(self.text) and self.text[at - 1] in "\x00\n", "insert at a paragraph start"
            self.text = self.text[:at] + body["text"] + self.text[at:]
        elif kind == "insertInlineImage":
            at = body["location"]["index"]
            self.text = self.text[:at] + "￼" + self.text[at:]
        elif kind == "updateTextStyle":
            r = body["range"]
            assert r["endIndex"] > r["startIndex"]
            self.styled.append((self.text[r["startIndex"]:r["endIndex"]], body["fields"]))
        elif kind in ("updateParagraphStyle", "deleteParagraphBullets", "createParagraphBullets"):
            r = body["range"]
            assert self.text[r["endIndex"] - 1] == "\n", f"{kind} must end on a paragraph's newline"
            if kind == "createParagraphBullets":
                start, end = r["startIndex"], r["endIndex"]
                chunk = self.text[start:end]
                cleaned = "\n".join(line.lstrip("\t") for line in chunk.split("\n"))
                self.text = self.text[:start] + cleaned + self.text[end:]
        else:
            raise AssertionError(f"unexpected request {kind}")


def written_blocks():
    return [
        {"kind": "source_text", "role": "title", "text": "Anatomical Position", "spans": [], "source_id": "g1"},
        {"kind": "source_text", "role": "paragraph", "text": "Definition: The body stands erect.",
         "spans": [[0, 11, {"bold": True}]], "source_id": "g2"},
        {"kind": "source_text", "role": "paragraph", "text": "Superior and inferior", "spans": [],
         "list": {"id": "L1", "level": 0, "numbered": False}, "source_id": "g3"},
        {"kind": "source_text", "role": "paragraph", "text": "Cranial → toward the head",
         "spans": [[0, 7, {"italic": True}]], "list": {"id": "L1", "level": 1, "numbered": False}, "source_id": "g4"},
        {"kind": "source_panel", "image": "a.png", "width_pt": 900, "height_pt": 450, "object_id": "img1",
         "source_id": "g5"},
        {"kind": "source_text", "role": "subsection", "text": "Assessment Sites:", "spans": [], "source_id": "g6"},
    ]


def test_reverse_insertion_puts_every_block_and_style_in_the_right_place():
    fake = FakeDocs()
    requests = gdoc_writer.tab_requests(written_blocks(), "t.0", lambda block: "https://img/" + block["object_id"])
    for request in requests:
        fake.apply(request)
    assert fake.text == ("\x00Anatomical Position\nDefinition: The body stands erect.\nSuperior and inferior\n"
                         "Cranial → toward the head\n￼\nAssessment Sites:\n\n")
    assert ("Definition:", "bold") in fake.styled
    assert ("Cranial", "italic") in fake.styled
    picture = next(r for r in requests if "insertInlineImage" in r)["insertInlineImage"]
    assert picture["objectSize"]["width"]["magnitude"] == pytest.approx(gdoc_source.TEXT_WIDTH_PT)
    assert picture["uri"] == "https://img/img1"


def test_table_cells_are_filled_last_first_at_their_own_paragraphs():
    block = {"kind": "source_table", "columns": ["Term", "Meaning"], "rows": [["Dorsal", "Back"]], "merges": [],
             "source_id": "g1"}
    requests = gdoc_writer.tab_requests([block], "t.0", lambda b: "")
    inserts = [r["insertText"] for r in requests if "insertText" in r and r["insertText"]["text"] != "\n"]
    assert [i["text"] for i in inserts] == ["Back", "Dorsal", "Meaning", "Term"]
    assert [i["location"]["index"] for i in inserts] == [
        gdoc_writer.cell_index(2, 1, 1, 2), gdoc_writer.cell_index(2, 1, 0, 2),
        gdoc_writer.cell_index(2, 0, 1, 2), gdoc_writer.cell_index(2, 0, 0, 2)]
    assert gdoc_writer.cell_index(2, 0, 0, 2) == 5 and gdoc_writer.cell_index(2, 1, 0, 2) == 10


def test_adjacent_boxes_of_one_style_share_one_frame_and_list_items_keep_a_bullet():
    blocks = [{"kind": "source_box", "style": "note", "parts": ["One"], "source_id": "g1"},
              {"kind": "source_box", "style": "note", "parts": ["Two"], "list": {"id": "L", "level": 0},
               "source_id": "g2"},
              {"kind": "source_box", "style": "warning", "parts": ["Three"], "source_id": "g3"}]
    found, labels = gdoc_writer.plan(blocks)
    assert [(g["kind"], len(g["blocks"])) for g in found] == [("box", 2), ("box", 1)]
    entries = gdoc_writer.box_entries(found[0], labels)
    assert [(e["text"], e["bullet"]) for e in entries] == [("One", None), ("Two", 0)]
    # Without a source label a box has no title bar: the template adds no words.
    assert gdoc_writer.expected(blocks) == [("One", ""), ("Two", ""), ("Three", "")]
    requests = gdoc_writer.tab_requests(blocks, "t.0", lambda b: "")
    assert not any("shading" in r.get("updateParagraphStyle", {}).get("fields", "") for r in requests)


def test_the_read_back_check_rejects_any_added_word():
    blocks = [text("g1", "Mixed glands are heterocrine.")]
    units = [{"texts": ["Mixed glands are heterocrine."]}]
    added = {"paragraphs": ["Note", "Mixed glands are heterocrine."], "pictures": 0, "tables": 0}
    assert gdoc_writer.verify(blocks, units, added)
    original = gdoc_writer.expected
    try:
        gdoc_writer.expected = lambda b, l=None: [("Note: Mixed glands are heterocrine.", "Note: ")]
        problems = gdoc_writer.verify(blocks, units, {"paragraphs": ["Note: Mixed glands are heterocrine."],
                                                      "pictures": 0, "tables": 0})
    finally:
        gdoc_writer.expected = original
    assert any("added" in p for p in problems)


def text(sid, value, role="paragraph", **extra):
    return {"kind": "source_text", "role": role, "text": value, "spans": [], "source_id": sid, **extra}


def test_first_heading_is_the_title_and_the_course_line_a_kicker():
    blocks = [text("g1", "BSc Nursing - 1st year / 1st sem - A&P"), text("g2", "Anatomical Position", "section"),
              text("g3", "The body stands erect.")]
    found, _ = gdoc_writer.plan(blocks)
    assert [g["blocks"][0]["role"] for g in found] == ["kicker", "title", "paragraph"]


def test_a_label_before_a_box_moves_into_its_title_bar():
    blocks = [text("g1", "Definition:"),
              {"kind": "source_box", "style": "definition", "title": "", "parts": ["The anatomical position is ..."],
               "source_id": "g2"}]
    found, _ = gdoc_writer.plan(blocks)
    assert len(found) == 1 and found[0]["title_block"]["text"] == "Definition:"
    assert gdoc_writer.expected(blocks) == [("Definition:", ""), ("The anatomical position is ...", "")]
    requests = gdoc_writer.tab_requests(blocks, "t.0", lambda b: "")
    inserts = [r["insertText"] for r in requests if "insertText" in r and r["insertText"]["text"] != "\n"]
    # One cell: the title bar is its first paragraph (kept with the text), the body follows it.
    first = gdoc_writer.cell_index(2, 0, 0, 1)
    assert [(i["text"], i["location"]["index"]) for i in inserts] == [
        ("Definition:\n", first), ("The anatomical position is ...", first + len("Definition:") + 1)]
    bar = next(r["updateParagraphStyle"] for r in requests if "updateParagraphStyle" in r
               and r["updateParagraphStyle"]["range"]["startIndex"] == first)
    assert bar["paragraphStyle"]["keepWithNext"] is True and "shading" in bar["fields"]


def test_a_short_subheading_right_before_a_box_becomes_its_title_but_a_section_heading_stays():
    box = {"kind": "source_box", "style": "note", "title": "", "parts": ["Dislocation occurs in mobile joints."],
           "source_id": "g2"}
    found, _ = gdoc_writer.plan([text("g0", "Joints", "section"), text("g1", "Clinical Importance", "subsection"), box])
    assert [g["kind"] for g in found] == ["source_text", "box"] and found[1]["title_block"]["source_id"] == "g1"
    found, _ = gdoc_writer.plan([text("g0", "Intro"), text("g9", "Overview"), text("g10", "More"),
                                 text("g1", "Classification of Joints", "section"), dict(box, source_id="g3")])
    assert found[-2]["kind"] == "source_text" and not found[-1].get("title_block")


def test_an_important_points_section_becomes_a_titled_box_with_real_bullets():
    bullet = {"id": "L", "level": 0, "numbered": False}
    blocks = [text("g1", "Important Points", "subsection"), text("g2", "Mixed glands are heterocrine."),
              text("g3", "Pancreas is the example.", list=bullet), text("g4", "Insulin and glucagon.", list=bullet),
              text("g5", "Functions", "section")]
    found, labels = gdoc_writer.plan(blocks)
    assert [g["kind"] for g in found] == ["box", "source_text"]
    assert found[0]["style"] == "note" and found[0]["title_block"]["source_id"] == "g1"
    requests = gdoc_writer.tab_requests(blocks, "t.0", lambda b: "")
    assert any("createParagraphBullets" in r for r in requests)
    units = [{"texts": [b["text"]]} for b in blocks]
    written = {"paragraphs": ["Important Points", "Mixed glands are heterocrine.", "Pancreas is the example.",
                              "Insulin and glucagon.", "Functions"], "pictures": 0, "tables": 0}
    assert gdoc_writer.verify(blocks, units, written) == []


def test_a_long_important_section_stays_running_text():
    blocks = [text("g1", "Important Points", "subsection")] + \
             [text(f"g{n}", "A long source paragraph. " * 20) for n in range(2, 6)]
    found, _ = gdoc_writer.plan(blocks)
    assert found[0]["kind"] == "source_text"


def test_simple_dollar_notation_is_shown_with_real_scripts_and_spans_follow():
    out, scripts, _ = gdoc_writer.inline_math("calcium ions ($Ca^{2+}$), which")
    assert out == "calcium ions (Ca2+), which" and scripts == [(16, 18, "SUPERSCRIPT")]
    out, scripts, _ = gdoc_writer.inline_math("$T_3, T_4$, Calcitonin")
    assert out == "T3, T4, Calcitonin" and scripts == [(1, 2, "SUBSCRIPT"), (5, 6, "SUBSCRIPT")]
    assert gdoc_writer.inline_math("gastric acid ($HCl$)")[0] == "gastric acid (HCl)"
    assert gdoc_writer.inline_math("costs $5 and $\\frac{a}{b}$")[0] == "costs $5 and $\\frac{a}{b}$"
    block = text("g1", "Stores $Ca^{2+}$ in bone", spans=[[17, 21, {"bold": True}]])
    shown = gdoc_writer._with_math(block)
    assert shown["text"] == "Stores Ca2+ in bone" and shown["spans"] == [[12, 16, {"bold": True}]]
    units = [{"texts": ["Stores $Ca^{2+}$ in bone"]}]
    assert gdoc_writer.verify([block], units, {"paragraphs": ["Stores Ca2+ in bone"], "pictures": 0, "tables": 0}) == []


def test_bold_inside_a_sentence_is_a_navy_key_term_but_a_lead_label_stays_black():
    sentence = "Epimysium: the sheath around the muscle, with fascicles inside."
    styles = gdoc_writer.span_styles(1, [[0, 10, {"bold": True}], [46, 55, {"bold": True}]], "t.0", sentence)
    assert "foregroundColor" not in styles[0]["updateTextStyle"]["fields"]
    assert "foregroundColor" in styles[1]["updateTextStyle"]["fields"]


def test_the_empty_paragraph_after_a_last_table_takes_no_room():
    table = {"startIndex": 10, "endIndex": 60, "table": {}}
    empty = {"startIndex": 60, "endIndex": 61, "paragraph": {"elements": [{"textRun": {"content": "\n"}}]}}
    requests = gdoc_writer.end_requests([table, empty], "t.0")
    assert requests[1]["updateTextStyle"]["textStyle"]["fontSize"]["magnitude"] == 1
    assert requests[0]["updateParagraphStyle"]["range"] == {"startIndex": 60, "endIndex": 61, "tabId": "t.0"}
    words = {"startIndex": 60, "endIndex": 70, "paragraph": {"elements": [{"textRun": {"content": "Last words\n"}}]}}
    assert gdoc_writer.end_requests([table, words], "t.0") == []
    # Our spacer after the last table plus Google's own last paragraph: the extra one is removed.
    spacer = dict(empty, startIndex=60, endIndex=61)
    final = dict(empty, startIndex=61, endIndex=62)
    requests = gdoc_writer.end_requests([table, spacer, final], "t.0")
    assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 60, "endIndex": 61, "tabId": "t.0"}}}
    assert requests[1]["updateParagraphStyle"]["range"] == {"startIndex": 60, "endIndex": 61, "tabId": "t.0"}


def test_a_tab_ending_in_text_has_no_empty_paragraph_left_to_start_a_blank_page():
    words = {"startIndex": 40, "endIndex": 60, "paragraph": {"elements": [{"textRun": {"content": "Last words\n"}}]}}
    empty = {"startIndex": 60, "endIndex": 61, "paragraph": {"elements": [{"textRun": {"content": "\n"}}]}}
    final = dict(empty, startIndex=61, endIndex=62)
    requests = gdoc_writer.end_requests([{"sectionBreak": {}}, words, empty, final], "t.0")
    # Google's last paragraph is made 1 pt, then joined to the last words (their own style is kept).
    assert requests[0]["updateTextStyle"]["range"] == {"startIndex": 61, "endIndex": 62, "tabId": "t.0"}
    assert requests[0]["updateTextStyle"]["textStyle"]["fontSize"]["magnitude"] == 1
    assert requests[1] == {"deleteContentRange": {"range": {"startIndex": 59, "endIndex": 61, "tabId": "t.0"}}}
    assert len(requests) == 2


def test_a_last_table_that_fills_its_page_gets_room_for_googles_last_paragraph():
    def paragraph(start, text="\n"):
        return {"startIndex": start, "endIndex": start + len(text),
                "paragraph": {"elements": [{"textRun": {"content": text}}]}}
    first = {"startIndex": 10, "endIndex": 50, "table": {}}
    box = {"startIndex": 52, "endIndex": 90, "table": {}}
    doubled = [first, paragraph(50), paragraph(51), box, paragraph(90)]
    assert gdoc_writer.full_page_requests(doubled, "t.0") == [
        {"deleteContentRange": {"range": {"startIndex": 50, "endIndex": 51, "tabId": "t.0"}}}]
    single = [paragraph(1, "Words before the box\n"), paragraph(22), dict(box, startIndex=23), paragraph(90)]
    shrink = gdoc_writer.full_page_requests(single, "t.0")
    assert shrink[0]["updateParagraphStyle"]["range"] == {"startIndex": 22, "endIndex": 23, "tabId": "t.0"}
    assert shrink[1]["updateTextStyle"]["textStyle"]["fontSize"]["magnitude"] == 1
    assert gdoc_writer.full_page_requests([paragraph(1, "Words\n"), paragraph(7)], "t.0") == []


def test_a_last_page_with_only_the_header_and_footer_counts_as_blank():
    import pymupdf

    def pdf(last_words):
        document = pymupdf.open()
        for words in ("Body text on page one", last_words):
            page = document.new_page()
            page.insert_text((40, 30), "Nursing Notes")            # header, same place on every page
            page.insert_text((40, 820), "Concept Understand Apply")  # footer
            if words:
                page.insert_text((40, 120), words)
        return document.tobytes()
    assert gdoc_writer.ends_on_blank_page(pdf("")) is True
    assert gdoc_writer.ends_on_blank_page(pdf("Location Heart")) is False


def test_a_last_table_whose_last_row_spilled_starts_a_new_page_with_its_heading():
    def paragraph(start, text="\n", named="NORMAL_TEXT"):
        return {"startIndex": start, "endIndex": start + len(text), "paragraph": {
            "elements": [{"textRun": {"content": text}}], "paragraphStyle": {"namedStyleType": named}}}
    heading = paragraph(10, "Comparison of Muscle Types\n", "HEADING_1")
    table = {"startIndex": 38, "endIndex": 90, "table": {"rows": 8}}
    requests = gdoc_writer.last_table_break_requests([{"sectionBreak": {}}, heading, paragraph(37), table,
                                                      paragraph(90)], "t.0")
    assert requests == [{"updateParagraphStyle": {"range": {"startIndex": 10, "endIndex": 37, "tabId": "t.0"},
                                                  "paragraphStyle": {"pageBreakBefore": True},
                                                  "fields": "pageBreakBefore"}}]
    # No heading above (a box after a list): the spacer paragraph above the box takes the break.
    words = paragraph(10, "Last list item\n")
    box = dict(table, startIndex=26)
    found = gdoc_writer.last_table_break_requests([{"sectionBreak": {}}, words, paragraph(25), box, paragraph(90)],
                                                  "t.0", on=False)
    assert found[0]["updateParagraphStyle"]["range"]["startIndex"] == 25
    assert found[0]["updateParagraphStyle"]["paragraphStyle"] == {"pageBreakBefore": False}
    assert gdoc_writer.last_table_break_requests([{"sectionBreak": {}}, words, paragraph(25)], "t.0") == []


def test_characters_lato_cannot_draw_are_set_in_the_fallback_font():
    styles = gdoc_writer.glyph_styles(10, "CₙH₂ₙOₙ and Cₘ", "t.0")
    ranges = [(s["updateTextStyle"]["range"]["startIndex"], s["updateTextStyle"]["range"]["endIndex"]) for s in styles]
    assert ranges == [(11, 12), (14, 15), (16, 17), (23, 24)]      # ₙ ₙ ₙ ₘ only; ₂ is in Lato
    assert all(s["updateTextStyle"]["textStyle"]["weightedFontFamily"]["fontFamily"] == "Noto Sans" for s in styles)
    assert gdoc_writer.glyph_styles(1, "Plain words (HCl) → μ", "t.0") == []


def _pdf(with_picture: bool) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), "Body text")
    if with_picture:
        page.insert_image(pymupdf.Rect(122, 245, 474, 597), stream=png("lightgrey", (200, 200)))
    return doc.tobytes()


def test_watermark_check_needs_the_big_picture_on_every_page():
    assert gdoc_writer.has_watermark(_pdf(True))
    assert not gdoc_writer.has_watermark(_pdf(False))


def test_big_content_pictures_in_different_places_are_not_a_watermark():
    import pymupdf

    doc = pymupdf.open()
    for top in (100, 400):                      # each page has a big centred picture, but not at one place
        page = doc.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(122, top, 474, top + 200), stream=png("navy", (300, 170)))
    assert not gdoc_writer.has_watermark(doc.tobytes())
    same = pymupdf.open()
    for _ in range(3):
        page = same.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(122, 245, 474, 597), stream=png("lightgrey", (200, 200)))
    assert gdoc_writer.has_watermark(same.tobytes())


class FakeTemplateApi:
    """Drive copy + Docs get/batchUpdate, enough for create_from_template."""

    def __init__(self, tabs):
        self.docs_tabs = {"tpl": list(tabs)}
        self.requests = []

    # Drive
    def files(self):
        return self

    def copy(self, fileId, body, fields, supportsAllDrives):
        self.docs_tabs["new"] = list(self.docs_tabs[fileId])
        self.copied = body["name"]
        self.answer = {"id": "new"}
        return self

    # Docs
    def documents(self):
        return self

    def get(self, documentId, includeTabsContent, fields):
        self.answer = {"tabs": [{"tabProperties": {"tabId": t, "title": t}} for t in self.docs_tabs[documentId]]}
        return self

    def batchUpdate(self, documentId, body):
        self.requests += body["requests"]
        self.answer = {"replies": [{} for _ in body["requests"]]}
        return self

    def execute(self):
        return getattr(self, "answer", None) or {"id": "new"}


def test_a_note_is_a_copy_of_the_watermark_template_with_only_the_tabs_it_needs():
    api = FakeTemplateApi(["t.0", "t.a", "t.b", "t.c"])
    api.answer = None
    document_id, used = gdoc_writer.create_from_template(api, api, "tpl", "UNIT -1 A&P",
                                                         [{"title": "Cell"}, {"title": "Tissue"}])
    assert document_id == "new" and used == ["t.0", "t.a"] and api.copied == "UNIT -1 A&P"
    assert [r["deleteTab"]["tabId"] for r in api.requests if "deleteTab" in r] == ["t.c", "t.b"]
    assert [r["updateDocumentTabProperties"]["tabProperties"]["title"] for r in api.requests
            if "updateDocumentTabProperties" in r] == ["Cell", "Tissue"]
    with pytest.raises(source.SourceError, match="duplicate its first tab 3 more"):
        gdoc_writer.create_from_template(api, api, "tpl", "Big unit", [{"title": str(n)} for n in range(7)])


class FakeHeaderApi:
    """One template tab: a header (where its watermark lives) and no footer yet."""

    def __init__(self):
        self.requests = []
        self.footer = ""

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent):
        style = {"defaultHeaderId": "kix.hf0", **({"defaultFooterId": self.footer} if self.footer else {})}
        empty = {"content": [{"endIndex": 1, "paragraph": {"elements": [{"textRun": {"content": "\n"}}]}}]}
        cell = lambda end: {"content": [{"endIndex": end, "paragraph": {"elements": []}}]}  # noqa: E731
        bar = {"content": [{"endIndex": 1, "paragraph": {"elements": []}},
                           {"endIndex": 30, "table": {"tableRows": [{"tableCells": [cell(12), cell(28)]}]}},
                           {"endIndex": 31, "paragraph": {"elements": []}}]}
        header = bar if any("insertTable" in r for r in self.requests) else empty
        self.answer = {"tabs": [{"tabProperties": {"tabId": "t.0", "title": "A"}, "documentTab": {
            "documentStyle": style, "headers": {"kix.hf0": header},
            "footers": {self.footer: empty} if self.footer else {}}}]}
        return self

    def batchUpdate(self, documentId, body):
        self.requests += body["requests"]
        replies = []
        for request in body["requests"]:
            if "createFooter" in request:
                self.footer = "kix.ft1"
                replies.append({"createFooter": {"footerId": "kix.ft1"}})
            else:
                replies.append({})
        self.answer = {"replies": replies}
        return self

    def execute(self):
        return self.answer


def test_the_logo_goes_into_the_header_bar_in_its_own_step_and_the_header_comes_before_the_footer():
    from note_studio.typeset import render

    api = FakeHeaderApi()
    gdoc_writer.header_footer(api, "doc", "t.0", render.Branding("Nursing", "Nursing Notes", "Nursing", "Apply"),
                              "https://logo", 1.0, watermarked=True)
    kinds = [next(iter(r)) for r in api.requests]
    table, picture, footer = kinds.index("insertTable"), kinds.index("insertInlineImage"), kinds.index("createFooter")
    # A picture inserted together with the bar drops the watermark; in its own step after the bar it is kept.
    assert table < picture < footer
    image = next(r["insertInlineImage"] for r in api.requests if "insertInlineImage" in r)
    assert image["location"] == {"index": 27 + 2, "tabId": "t.0", "segmentId": "kix.hf0"}   # end of the right cell
    assert "deleteHeader" not in kinds and "createHeader" not in kinds


def test_short_table_columns_are_centred_and_the_header_repeats():
    block = {"kind": "source_table", "columns": ["Circulation", "Function"],
             "rows": [["Pulmonary", "Gas exchange"], ["Bronchial", "Nutrition"]], "merges": [], "source_id": "g1"}
    requests = gdoc_writer.tab_requests([block], "t.0", lambda b: "")
    aligns = [r["updateParagraphStyle"]["paragraphStyle"]["alignment"] for r in requests
              if "updateParagraphStyle" in r and r["updateParagraphStyle"]["range"]["startIndex"] > 2]
    assert aligns.count("CENTER") == 3 and aligns.count("START") == 3
    assert any("pinTableHeaderRows" in r for r in requests)


def test_a_numbered_list_split_by_paragraphs_keeps_the_source_numbers():
    numbered = {"id": "N", "level": 0, "numbered": True}
    blocks = [
        {"kind": "source_text", "role": "paragraph", "text": "Cardiac Evaluation:", "spans": [[0, 7, {"bold": True}]],
         "list": numbered, "source_id": "g1"},
        {"kind": "source_text", "role": "paragraph", "text": "Assessment of anterior chest pain.", "spans": [],
         "source_id": "g2"},
        {"kind": "source_text", "role": "paragraph", "text": "Procedural:", "spans": [], "list": numbered,
         "source_id": "g3"},
        {"kind": "source_text", "role": "paragraph", "text": "Sub point", "spans": [],
         "list": {"id": "N", "level": 1, "numbered": True}, "source_id": "g4"},
    ]
    lists = {"N": [{"glyphType": "DECIMAL", "glyphFormat": "%0."}, {"glyphType": "ALPHA", "glyphFormat": "(%1)"}]}
    assert gdoc_writer.list_labels(blocks, lists) == {"g1": "1.", "g3": "2.", "g4": "(a)"}
    assert all(g.get("literal") for g in gdoc_writer.groups(blocks) if g["kind"] == "list")
    fake = FakeDocs()
    for request in gdoc_writer.tab_requests(blocks, "t.0", lambda b: "", lists):
        fake.apply(request)
    assert fake.text == ("\x001. Cardiac Evaluation:\nAssessment of anterior chest pain.\n2. Procedural:\n"
                         "(a) Sub point\n\n")
    assert ("Cardiac", "bold,foregroundColor") in fake.styled         # a bold term, not a "label:" -> navy
    units = [{"texts": [b["text"]]} for b in blocks]
    written = {"paragraphs": ["1. Cardiac Evaluation:", "Assessment of anterior chest pain.", "2. Procedural:",
                              "(a) Sub point"], "pictures": 0, "tables": 0}
    assert gdoc_writer.verify(blocks, units, written, lists) == []


def test_unsplit_lists_keep_googles_own_numbering():
    numbered = {"id": "N", "level": 0, "numbered": True}
    blocks = [{"kind": "source_text", "role": "paragraph", "text": t, "spans": [], "list": numbered, "source_id": s}
              for t, s in (("One", "g1"), ("Two", "g2"))]
    requests = gdoc_writer.tab_requests(blocks, "t.0", lambda b: "")
    assert any("createParagraphBullets" in r for r in requests)
    assert [t for t, _ in gdoc_writer.expected(blocks)] == ["One", "Two"]


def test_read_back_check_accepts_exact_content_and_names_a_change():
    blocks = written_blocks()
    units = [{"texts": [source.normal(b["text"])]} for b in blocks if b.get("text")]
    good = {"paragraphs": [b["text"] for b in blocks if b.get("text")], "pictures": 1, "tables": 0}
    assert gdoc_writer.verify(blocks, units, good) == []
    changed = dict(good, paragraphs=good["paragraphs"][:1] + ["Definition: The body stands."] + good["paragraphs"][2:])
    problems = gdoc_writer.verify(blocks, units, changed)
    assert problems and "Paragraph 2 differs" in problems[0]
    assert "pictures" in " ".join(gdoc_writer.verify(blocks, units, dict(good, pictures=0)))


class _Refused(Exception):
    def __init__(self, status, content):
        super().__init__(content)
        self.resp, self.content = type("Resp", (), {"status": status})(), content


class _Call:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def execute(self):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_drives_user_rate_limit_waits_and_tries_again_but_missing_access_does_not():
    limit = _Refused(403, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}], '
                          b'"message": "User rate limit exceeded."}}')
    call = _Call(limit, {"id": "copy"})
    assert gdoc_writer._execute(call, pause=0, outages=False) == {"id": "copy"} and call.calls == 2
    denied = _Refused(403, b'{"error": {"errors": [{"reason": "forbidden"}], "message": "The caller does not have '
                           b'permission"}}')
    with pytest.raises(_Refused):
        gdoc_writer._execute(_Call(denied, {"id": "never"}), pause=0)
    with pytest.raises(_Refused):                       # a copy after an outage may exist: not made twice
        gdoc_writer._execute(_Call(_Refused(503, b"backend error"), {"id": "twice"}), pause=0, outages=False)
    assert gdoc_writer._execute(_Call(_Refused(503, b"backend error"), {"id": "batch"}), pause=0) == {"id": "batch"}
