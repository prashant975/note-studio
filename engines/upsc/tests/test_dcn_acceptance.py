"""Acceptance checks for the DCN template overhaul.

One test per line of the acceptance checklist: fonts embedded, the type scale,
complex-script twins, the Hindi Kalam build, no rules/underlines, the title
guard, the removed skeleton blocks, the closing sections, image width, and
vertical rhythm.

The measurements are read from src.config, never repeated here. That is the
point of the compact-layout work: config is the single place a margin, a size or
a spacing is decided, and a test that copies the number instead of importing it
turns every future tuning pass into a test-editing pass.

Everything runs offline in programmatic mode (no template committed).
"""
from pathlib import Path
import re
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    DCN_BULLET_GLYPHS,
    DCN_MIN_READABLE_BODY_PT,
    DCN_CONTENT_WIDTH_IN,
    DCN_LINE_SPACING,
    DCN_SIZE_BODY_PT,
    DCN_SIZE_H1_PT,
    DCN_SIZE_H2_PT,
    DCN_COLOR_BODY,
    DCN_SIZE_H3_PT,
    DCN_SIZE_H4_PT,
    DCN_SIZE_SUBJECT_PT,
    DCN_SIZE_TITLE_PT,
)
from src.upsc_dcn_docx_writer import write_upsc_dcn_docx

EN_TITLE = "L01_DCN01_Introduction to Environment and Ecology"

EN_NOTES = f"""#LECTURE_TITLE: {EN_TITLE}

#TOPICS_COVERED_START
- Ecology and ecosystems
- Biodiversity
#TOPICS_COVERED_END

#H1: Core Concepts

- **Ecology** is the study of organisms and their environment (e.g., *Lantana camara* as an invasive).
  - The **Wildlife (Protection) Act, 1972** provides the principal legal framework.
    - Gir hosts the only wild population of Asiatic lions.

#H2: Indicators

- India's installed capacity reached **513.73 GW**, with CO₂ tracked separately.

#H3: Process

#SEPARATOR

- UPSC caution: non-fossil is not the same as renewable.

#FIGURE: visual_001_01

#H1: QUICK REVISION

- [Living Planet Report 2024](https://livingplanet.panda.org) → **73% decline** in monitored wildlife population sizes, 1970–2020.
- [ISFR 2023](https://fsi.nic.in) → forest and tree cover is **25.17%** of India's geographical area.

#H3: Key Sources

- [Convention on Biological Diversity](https://www.cbd.int)
- [MNRE](https://mnre.gov.in)
"""

HI_TITLE = "L01_DCN01_पर्यावरण एवं पारिस्थितिकी का परिचय"

HI_NOTES = f"""#LECTURE_TITLE: {HI_TITLE}

#H1: मूल अवधारणाएँ

- **पारिस्थितिकी** जीवों और उनके पर्यावरण के अध्ययन को कहते हैं।
  - **वन्यजीव (संरक्षण) अधिनियम, 1972** प्रमुख वैधानिक ढाँचा देता है।

#H1: त्वरित पुनरावृत्ति

- [लिविंग प्लैनेट रिपोर्ट 2024](https://livingplanet.panda.org) → वन्यजीव आबादी में **73% गिरावट**, 1970–2020।

#H3: प्रमुख स्रोत

- [जैव विविधता अभिसमय](https://www.cbd.int)
"""

STYLE_IDS = (
    "UPSC_DCN_Subject",
    "UPSC_DCN_Title", "UPSC_DCN_H1_Red", "UPSC_DCN_H2_Blue", "UPSC_DCN_H3_Black",
    "UPSC_DCN_Body", "UPSC_DCN_Bullet_Level_1", "UPSC_DCN_Bullet_Level_2",
    "UPSC_DCN_Bullet_Level_3", "UPSC_DCN_Table", "UPSC_DCN_Flow",
    "UPSC_DCN_Caution", "UPSC_DCN_Topics_Heading",
)


def _parts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()
                if n.endswith(".xml") or n.endswith(".rels")}


def _font_parts(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if n.startswith("word/fonts/")]


def _style_block(styles_xml: str, style_id: str) -> str:
    match = re.search(
        rf'<w:style [^>]*w:styleId="{re.escape(style_id)}".*?</w:style>',
        styles_xml, re.S)
    assert match, f"style {style_id} not found"
    return match.group(0)


@pytest.fixture(scope="module")
def figure(tmp_path_factory):
    from PIL import Image

    path = tmp_path_factory.mktemp("vis") / "wide_diagram.png"
    Image.new("RGB", (3000, 1000), "white").save(path)
    return {"visual_001_01": {"path": str(path), "caption": "Test diagram",
                              "width_px": 3000, "height_px": 1000}}


@pytest.fixture(scope="module")
def written_en(tmp_path_factory, figure):
    out = tmp_path_factory.mktemp("en") / "notes.docx"
    out, warnings = write_upsc_dcn_docx(EN_NOTES, out, lecture_title=EN_TITLE,
                                        visuals=figure)
    return out, warnings, _parts(out)


@pytest.fixture(scope="module")
def written_hi(tmp_path_factory):
    out = tmp_path_factory.mktemp("hi") / "notes_hi.docx"
    out, warnings = write_upsc_dcn_docx(HI_NOTES, out, language_code="hi",
                                        lecture_title=HI_TITLE)
    return out, warnings, _parts(out)


# --------------------------------------------------------------- 1.3 fonts

def test_english_doc_embeds_coming_soon_and_kalam(written_en):
    out, _, parts = written_en
    fonts = _font_parts(out)
    assert any("ComingSoon" in f for f in fonts), fonts
    assert any("Kalam-Regular" in f for f in fonts), fonts
    assert any("Kalam-Bold" in f for f in fonts), fonts
    assert '<w:embedTrueTypeFonts w:val="1"/>' in parts["word/settings.xml"]
    table = parts["word/fontTable.xml"]
    assert re.search(r'w:name="Coming Soon"[^>]*>.*?embedRegular', table, re.S)
    assert re.search(r'w:name="Kalam"[^>]*>.*?embedRegular', table, re.S)
    assert re.search(r'w:name="Kalam"[^>]*>.*?embedBold', table, re.S)
    assert 'ContentType="application/x-font-ttf"' in parts["[Content_Types].xml"]


def test_hindi_doc_embeds_kalam(written_hi):
    out, _, parts = written_hi
    fonts = _font_parts(out)
    assert any("Kalam-Regular" in f for f in fonts), fonts
    assert any("Kalam-Bold" in f for f in fonts), fonts
    assert '<w:embedTrueTypeFonts w:val="1"/>' in parts["word/settings.xml"]


# --------------------------------------------------------- 1.8 type scale

def test_type_scale_is_the_agreed_half_points(written_en):
    """Every style is set at exactly the config size, in half-points.

    The absolute values live in config; what is asserted here is that the
    document actually carries them and that the ladder holds its shape.

    Subject and lecture line are deliberately the SAME size — in the published
    note the two are one title block, not a heading and a subheading — so the
    ordering below is equal-then-strictly-descending, and the four heading
    levels each step down by two points to body.
    """
    assert DCN_SIZE_SUBJECT_PT == DCN_SIZE_TITLE_PT, "the title block split in two"
    assert (DCN_SIZE_TITLE_PT > DCN_SIZE_H1_PT > DCN_SIZE_H2_PT > DCN_SIZE_H3_PT
            > DCN_SIZE_H4_PT > DCN_SIZE_BODY_PT), "type ladder broke"
    assert DCN_SIZE_BODY_PT >= DCN_MIN_READABLE_BODY_PT, "body text below the readable floor"
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    expected = {
        "UPSC_DCN_Subject": DCN_SIZE_SUBJECT_PT,
        "UPSC_DCN_Title": DCN_SIZE_TITLE_PT,
        "UPSC_DCN_H1_Red": DCN_SIZE_H1_PT,
        "UPSC_DCN_H2_Blue": DCN_SIZE_H2_PT,
        "UPSC_DCN_H3_Black": DCN_SIZE_H3_PT,
        "UPSC_DCN_H4_Grey": DCN_SIZE_H4_PT,
        "UPSC_DCN_Body": DCN_SIZE_BODY_PT,
        "UPSC_DCN_Bullet_Level_1": DCN_SIZE_BODY_PT,
    }
    for style_id, size_pt in expected.items():
        block = _style_block(styles, style_id)
        assert f'<w:sz w:val="{int(size_pt * 2)}"/>' in block, (style_id, block)


def test_headings_are_bold(written_en):
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    for style_id in ("UPSC_DCN_Subject", "UPSC_DCN_Title", "UPSC_DCN_H1_Red",
                     "UPSC_DCN_H2_Blue", "UPSC_DCN_H3_Black"):
        assert "<w:b/>" in _style_block(styles, style_id)


# ------------------------------------------------- 1.9 complex-script twins

@pytest.mark.parametrize("fixture_name", ["written_en", "written_hi"])
def test_every_style_carries_szcs_and_bcs_twins(fixture_name, request):
    _, _, parts = request.getfixturevalue(fixture_name)
    styles = parts["word/styles.xml"]
    for style_id in STYLE_IDS:
        block = _style_block(styles, style_id)
        sz = re.search(r'<w:sz w:val="(\d+)"/>', block)
        szcs = re.search(r'<w:szCs w:val="(\d+)"/>', block)
        assert sz and szcs and sz.group(1) == szcs.group(1), (style_id, block)
        if "<w:b/>" in block:
            assert "<w:bCs/>" in block, f"{style_id} bold without bCs"


def test_doc_defaults_carry_szcs(written_en):
    """Both slots, at body size. Devanagari reads ONLY the Cs twin, so a
    docDefaults that carries w:sz without w:szCs renders every Hindi run at
    Word's own 11 pt default instead of the size this document chose."""
    _, _, parts = written_en
    defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>",
                         parts["word/styles.xml"], re.S).group(0)
    half = int(DCN_SIZE_BODY_PT * 2)
    assert f'<w:sz w:val="{half}"/>' in defaults, defaults
    assert f'<w:szCs w:val="{half}"/>' in defaults, defaults


def test_hindi_headings_are_above_body_size_in_the_cs_slot(written_hi):
    """Regression guard for the szCs bug: H1/H2 must outrank 11pt for
    Devanagari, which reads ONLY the complex-script size."""
    _, _, parts = written_hi
    styles = parts["word/styles.xml"]
    assert DCN_SIZE_H1_PT > DCN_SIZE_BODY_PT and DCN_SIZE_H2_PT > DCN_SIZE_BODY_PT
    assert (f'<w:szCs w:val="{int(DCN_SIZE_H1_PT * 2)}"/>'
            in _style_block(styles, "UPSC_DCN_H1_Red"))
    assert (f'<w:szCs w:val="{int(DCN_SIZE_H2_PT * 2)}"/>'
            in _style_block(styles, "UPSC_DCN_H2_Blue"))


# ------------------------------------------------------- 5.0 Hindi = Kalam

def test_hindi_build_names_kalam_in_all_four_slots(written_hi):
    _, _, parts = written_hi
    styles = parts["word/styles.xml"]
    for style_id in STYLE_IDS:
        rfonts = re.search(r"<w:rFonts [^>]*/>", _style_block(styles, style_id))
        assert rfonts, style_id
        for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            assert f'{slot}="Kalam"' in rfonts.group(0), (style_id, rfonts.group(0))
    assert 'w:ascii="Coming Soon"' not in parts["word/document.xml"]
    assert 'w:ascii="Coming Soon"' not in styles


def test_english_build_keeps_coming_soon_with_kalam_cs(written_en):
    _, _, parts = written_en
    block = _style_block(parts["word/styles.xml"], "UPSC_DCN_Body")
    rfonts = re.search(r"<w:rFonts [^>]*/>", block).group(0)
    assert 'w:ascii="Coming Soon"' in rfonts
    assert 'w:cs="Kalam"' in rfonts


# ------------------------------------------- 1.1 / 1.2 rules and underlines

@pytest.mark.parametrize("fixture_name", ["written_en", "written_hi"])
def test_no_single_paragraph_borders_anywhere(fixture_name, request):
    _, _, parts = request.getfixturevalue(fixture_name)
    for name, xml in parts.items():
        for pbdr in re.findall(r"<w:pBdr>.*?</w:pBdr>", xml, re.S):
            assert 'w:val="single"' not in pbdr, (name, pbdr)


def test_no_underlined_heading_styles(written_en):
    _, _, parts = written_en
    styles = parts["word/styles.xml"]
    for style_id in STYLE_IDS:
        block = _style_block(styles, style_id)
        assert '<w:u w:val="single"/>' not in block, style_id


def test_no_underline_runs_on_headings(written_en):
    """Hyperlinks are the one underlined thing left in the body."""
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    for para in re.findall(r"<w:p\b.*?</w:p>", doc, re.S):
        if "UPSC_DCN_Title" in para or "UPSC_DCN_H1_Red" in para:
            underlined = re.findall(r'<w:u w:val="single"/>', para)
            assert not underlined, para[:400]


# --------------------------------------------------------- 2.1 title guard

def test_title_matches_the_reference_shape(written_en):
    """The heading is now the reference IMAGE's two-level shape.

    ``L01_DCN01_`` is still the canonical title everywhere it was before — the
    ``#LECTURE_TITLE:`` marker, run_metadata, the file name — but it is no
    longer what a student reads. The page shows "Lecture 1 — <title>", and the
    machine prefix must not leak onto it.
    """
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    text = re.sub(r"<[^>]+>", "", doc)
    assert "Lecture 1" in text, text[:400]
    assert "Introduction to Environment and Ecology" in text
    assert "L01_DCN01_" not in text, "the machine prefix reached the page"
    assert "b950d402" not in doc


def test_a_uuid_title_fails_loudly(tmp_path):
    notes = "#LECTURE_TITLE: Lecture: b950d402 bc5d 48def60b1910\n\n#H1: Topic\n- A point."
    with pytest.raises(ValueError, match="file identifier"):
        write_upsc_dcn_docx(notes, tmp_path / "bad.docx")


# ------------------------------------------------------- 2.2 / 2.3 skeleton

def test_no_topics_covered_block_is_rendered(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert "Topics Covered" not in doc
    assert "TOPICS_COVERED" not in doc
    assert "Ecology and ecosystems" not in doc     # the block's entries go too


def test_no_mains_practice_question_section(written_en):
    _, _, parts = written_en
    assert "Mains Practice" not in parts["word/document.xml"]


# ------------------------------------------------- 2.4 closing sections

def test_quick_revision_and_key_sources_are_rendered(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert "QUICK REVISION" in doc
    assert "Key Sources" in doc
    revision_at = doc.rfind("QUICK REVISION")
    sources_at = doc.rfind("Key Sources")
    assert revision_at < sources_at, "Key Sources must follow QUICK REVISION"


def test_citations_become_real_hyperlinks(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert doc.count("<w:hyperlink ") >= 4
    rels = parts["word/_rels/document.xml.rels"]
    assert "livingplanet.panda.org" in rels
    assert "cbd.int" in rels
    assert "[" not in re.sub(r"<[^>]+>", "", doc), "markdown link syntax leaked"


# ----------------------------------------------------------- 1.4 spacing

@pytest.mark.parametrize("fixture_name", ["written_en", "written_hi"])
def test_all_styles_run_at_the_configured_line_spacing(fixture_name, request):
    """Every style carries an explicit w:line, and none of them is single.

    Single spacing (240) clips Devanagari matras in LibreOffice, so the compact
    pass has a floor as well as a target. Tables and flow steps are allowed the
    tighter value; everything else runs at DCN_LINE_SPACING.
    """
    _, _, parts = request.getfixturevalue(fixture_name)
    styles = parts["word/styles.xml"]
    expected = int(round(DCN_LINE_SPACING * 240))
    tight_styles = {"UPSC_DCN_Table", "UPSC_DCN_Flow"}
    for style_id in STYLE_IDS:
        block = _style_block(styles, style_id)
        spacing = re.search(r"<w:spacing [^>]*/>", block)
        assert spacing, (style_id, block)
        line = re.search(r'w:line="(\d+)"', spacing.group(0))
        assert line, (style_id, spacing.group(0))
        value = int(line.group(1))
        assert value >= 240, (style_id, "single spacing clips Devanagari", value)
        if style_id not in tight_styles:
            assert value == expected, (style_id, value, expected)


def test_doc_defaults_add_no_space_after(written_en):
    _, _, parts = written_en
    defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>",
                         parts["word/styles.xml"], re.S).group(0)
    spacing = re.search(r"<w:spacing [^>]*/>", defaults)
    assert spacing and 'w:after="0"' in spacing.group(0), defaults


# ------------------------------------------------------- 1.5 / 1.6 details

def test_the_bullet_ladder_uses_the_configured_glyphs(written_en):
    """Three distinct, compact markers — and every level actually defined.

    The old ● ○ ■ set read as decoration at body size; the compact pass swapped
    them for smaller marks and sized the marker below body size. What must hold
    is that the three levels stay visually distinct, so the hierarchy survives
    the compaction.
    """
    _, _, parts = written_en
    numbering = parts["word/numbering.xml"]
    assert len(set(DCN_BULLET_GLYPHS)) == 3, DCN_BULLET_GLYPHS
    for glyph in DCN_BULLET_GLYPHS:
        assert f'<w:lvlText w:val="{glyph}"/>' in numbering, glyph


def test_footer_carries_a_live_page_number(written_en):
    """The published note numbers its pages, centred between the two labels.

    A PAGE field, not typed text: the number differs on every page and nothing
    in the writer knows how many pages the body will come to. The bare word
    "Page" is still not printed — the original shows the digit alone.
    """
    _, _, parts = written_en
    footers = "".join(v for k, v in parts.items() if re.match(r"word/footer\d+\.xml", k))
    assert "PW OnlyIAS" in footers and "Daily Class Notes" in footers
    assert "w:instrText" in footers, "the PAGE field is missing"
    assert "PAGE" in footers
    assert 'w:fldCharType="begin"' in footers and 'w:fldCharType="end"' in footers
    assert 'w:val="center"' in footers, "the number is not centred between the labels"
    assert "Page" not in re.sub(r"<[^>]+>", "", footers), "the word 'Page' was printed"


# --------------------------------------------------------- 1.7 caution

def test_caution_notes_use_the_caution_style(written_en):
    """A caution keeps its own style, loses its label, and is shaded instead.

    The label used to be printed ("UPSC Caution: ..."), which said in words what
    the shading now says in colour. The sentence itself must survive intact —
    dropping the label must never drop the caution.
    """
    from src.config import DCN_HIGHLIGHT_COLOR

    _, _, parts = written_en
    doc = parts["word/document.xml"]
    caution = next(p for p in re.findall(r"<w:p\b.*?</w:p>", doc, re.S)
                   if "non-fossil is not the same as renewable" in p)
    assert 'w:val="UPSC_DCN_Caution"' in caution, caution[:400]
    assert f'w:fill="{DCN_HIGHLIGHT_COLOR}"' in caution, "the caution was not shaded"
    assert "UPSC caution" not in re.sub(r"<[^>]+>", "", doc), "the label reached the page"


# ------------------------------------------------------------- 6 images

def test_every_image_fits_the_content_column(written_en):
    _, _, parts = written_en
    max_emu = int(DCN_CONTENT_WIDTH_IN * 914400)
    for name, xml in parts.items():
        for cx in re.findall(r'<wp:extent cx="(\d+)"', xml):
            assert int(cx) <= max_emu, (name, cx)


def test_a_body_image_is_embedded_inline(written_en):
    _, _, parts = written_en
    doc = parts["word/document.xml"]
    assert "<wp:inline" in doc, "figure was not embedded inline in the body"


# ---------------------------------------------- 4 correction-language ban

def _guard_hits(text):
    from src.correction_language_guard import find_correction_metalanguage

    return find_correction_metalanguage(text)


@pytest.mark.parametrize("leak", [
    "QUICK REVISION: MOST IMPORTANT CORRECTED FACTS",
    'There was no such policy adopted in 1970 as stated here.',
    "The Act was passed in 1972, contrary to what was said.",
    "The transcript's figure of 220 GW is outdated.",
    "Correction: the year is 1983.",
    "The figure was corrected to 73%.",
    "This was rectified in the notes.",
    "UPSC महत्वपूर्ण तथ्य सुधार",
    "संशोधित तथ्य: वर्ष 1983 है।",
    "त्रुटि सुधार लागू किया गया।",
    "जैसा कहा गया, यह गलत है।",
    "व्याख्यान के अनुसार गलत तथ्य।",
    "सुधार: सही वर्ष 1983 है।",
])
def test_correction_metalanguage_is_caught(leak):
    assert _guard_hits(f"#H1: Topic\n- {leak}\n"), leak


@pytest.mark.parametrize("legit", [
    "India adopted the National Wildlife Action Plan in 1983; the Wildlife (Protection) Act, 1972 provides the principal legal framework.",
    "The corrective measures under the Act include penalties.",   # 'corrective' ≠ 'corrected'
    "1991 के आर्थिक सुधार उदारीकरण की शुरुआत थे।",                    # reform, not correction
    "भूमि सुधार स्वतंत्रता के बाद की प्रमुख नीति थी।",
    "Important distinction: non-fossil is not the same as renewable.",
    "[Needs Verification: figure changes every replenishment cycle]",
])
def test_legitimate_content_passes_the_guard(legit):
    assert not _guard_hits(f"#H1: Topic\n- {legit}\n"), legit


def test_the_guard_fails_the_build_loudly():
    from src.correction_language_guard import (
        CorrectionLanguageError,
        assert_no_correction_metalanguage,
    )

    with pytest.raises(CorrectionLanguageError, match="silent"):
        assert_no_correction_metalanguage("- The figure was corrected to 73%.")


# ------------------------------------------------- 5.1 Hindi script rules

def test_latin_runs_in_hindi_body_are_flagged():
    from src.script_validator import find_latin_in_hindi_body

    notes = (
        "#LECTURE_TITLE: L01_DCN01_परिचय\n"
        "#H1: NDC and UNFCCC\n"                                   # heading: exempt
        "- यूएनएफसीसीसी (UNFCCC) की स्थापना 1992 में हुई।\n"       # first-use bracket: exempt
        "- [पेरिस समझौता](https://unfccc.int/paris-agreement) 2015 में हुआ।\n"  # URL: exempt
        "- RTI Act के तहत नागरिकों को अधिकार मिला।\n"              # violation
        "- capacity लगभग 220 GW थी।\n"                             # violation (GW alone is fine)
    )
    runs = find_latin_in_hindi_body(notes)
    flagged = {r.text for r in runs}
    assert "RTI Act" in flagged
    assert "capacity" in flagged
    assert not any("UNFCCC" in t for t in flagged), flagged
    assert not any("https" in t for t in flagged), flagged


def test_clean_hindi_notes_produce_no_script_warnings():
    from src.script_validator import latin_script_warnings

    assert latin_script_warnings(HI_NOTES) == []


# --------------------------------------------------- 3.6 typed arrows

def test_typed_arrows_are_replaced_with_real_ones():
    from src.visual_format_checker import check_and_repair_notes

    notes = (
        "#LECTURE_TITLE: L01_DCN01_Northern Plains\n"
        "#H1: Bhabar\n"
        "- Rivers descend from Shiwaliks -> slope break --> deposition.\n"
    )
    result = check_and_repair_notes(notes, lecture_title="L01_DCN01_Northern Plains")
    assert "->" not in result.notes_text
    assert "→" in result.notes_text
    assert any(a.kind == "ascii_arrows" for a in result.actions)


# ------------------------------------------------- ordered lists (opt-in)

ORDERED_NOTES = """#LECTURE_TITLE: L02_DCN02_Soil Formation

#H1: Weathering Sequence

1. Parent rock fractures under temperature stress.
2. Water enters the cracks and freezes.
3. Fragments break off and accumulate.

#H1: Independent Facts

- Regur soil is black cotton soil.
- Laterite forms under alternating wet and dry seasons.
"""


def test_ordered_steps_render_numerals_and_bullets_render_dots(tmp_path):
    out, warnings = write_upsc_dcn_docx(ORDERED_NOTES, tmp_path / "ordered.docx",
                                        lecture_title="L02_DCN02_Soil Formation")
    doc = _parts(out)["word/document.xml"]
    for numeral in (">1.<", ">2.<", ">3.<"):
        assert numeral in doc, f"step numeral {numeral} missing"
    # The numerals are bold in body ink, not the H1 red: the published note
    # sets a numbered step in the same colour as the sentence it introduces,
    # and a red numeral beside black text reads as a heading that lost its
    # line. Ordinary bullets keep real Word numbering.
    assert f'w:val="{DCN_COLOR_BODY}"' in doc
    assert 'w:val="990000"' not in doc, 'a step numeral is still red'
    assert "<w:numPr>" in doc, "unordered bullets lost their numbering"


# --------------------------------------- 4 corrections (gated, default off)

CORRECTION_NOTES = """#LECTURE_TITLE: L03_DCN03_Motion

#H1: Equations of Motion

- Final velocity is $v = u + at$.

#CORRECTION_START
SOURCE: With u = 0 and a = 2 m/s² for 3 s, the slide concludes v = 5 m/s.
CORRECTED: v = u + at = 0 + 2 × 3 = 6 m/s.
REASON: The slide's own equation v = u + at with its own numbers gives 6, not 5.
#CORRECTION_END
"""


def test_corrections_are_dropped_when_disabled(tmp_path):
    out, warnings = write_upsc_dcn_docx(CORRECTION_NOTES, tmp_path / "off.docx",
                                        lecture_title="L03_DCN03_Motion")
    doc = _parts(out)["word/document.xml"]
    assert "CORRECTION" not in doc and "SOURCE:" not in doc
    assert any("CORRECTIONS_ENABLED is off" in w for w in warnings), warnings


def test_corrections_render_as_red_callout_when_enabled(tmp_path, monkeypatch):
    import src.upsc_dcn_docx_writer as writer

    monkeypatch.setattr(writer, "CORRECTIONS_ENABLED", True)
    out, warnings = write_upsc_dcn_docx(CORRECTION_NOTES, tmp_path / "on.docx",
                                        lecture_title="L03_DCN03_Motion")
    doc = _parts(out)["word/document.xml"]
    assert "Correction" in doc
    assert "6 m/s" in doc
    assert 'w:fill="FDEBEF"' in doc, "callout fill missing"
    assert "<w:strike/>" in doc, "source text is not struck through"
    # Still zero paragraph borders — the callout is a bordered table cell.
    for pbdr in re.findall(r"<w:pBdr>.*?</w:pBdr>", doc, re.S):
        assert 'w:val="single"' not in pbdr


def test_correction_blocks_are_exempt_from_the_language_guard():
    from src.correction_language_guard import find_correction_metalanguage

    assert not find_correction_metalanguage(CORRECTION_NOTES)
    # ...but the ban still bites outside the block, same document.
    leaky = CORRECTION_NOTES + "\n- The figure was corrected to 6 m/s.\n"
    assert find_correction_metalanguage(leaky)


def test_quick_revision_heading_suffix_is_dropped():
    from src.visual_format_checker import check_and_repair_notes

    notes = (
        "#LECTURE_TITLE: L01_DCN01_Ecology\n"
        "#H1: QUICK REVISION: MOST IMPORTANT CORRECTED FACTS\n"
        "- A fact.\n"
    )
    result = check_and_repair_notes(notes, lecture_title="L01_DCN01_Ecology")
    assert "#H1: QUICK REVISION\n" in result.notes_text + "\n"
    assert "CORRECTED FACTS" not in result.notes_text


# ----------------------------------- 4.1 correction words as subject content

#: Lines from real lectures where "corrected" / "rectified" is the syllabus,
#: not a leak. The bare-word ban failed a DEFECTS OF VISION class four times in
#: a row — after transcription, generation and QC had all been paid for.
SUBJECT_VOCABULARY = [
    "**Correction**: It is corrected using a **concave lens** of appropriate focal length.",
    "**Correction**: It is corrected using a **convex lens** of appropriate power.",
    "Corrected using **bifocal lenses**, which combine two powers in one lens.",
    "**Correction**: Cannot be corrected with spectacles or contact lenses; it requires surgery.",
    "**Correction**: It is corrected through surgical intervention, where the lens is replaced.",
    "Myopia is corrected by a concave lens.",
    # The collision waiting behind it: power electronics and chemistry.
    "AC is rectified to DC using a semiconductor diode.",
    "Rectified spirit contains about 95% ethanol.",
]


@pytest.mark.parametrize("line", SUBJECT_VOCABULARY)
def test_correcting_a_defect_is_not_narrating_an_edit(line):
    """What the word points AT is the distinction.

    Pointing at a remedy — a lens, spectacles, surgery, a diode — is a class
    describing the world. Pointing at a value is the notes describing their own
    edit. Only the second is a leak.
    """
    assert not _guard_hits(f"#H1: Topic\n- {line}\n"), line


@pytest.mark.parametrize("leak", [
    # Emphasis used to hide the announcement: the ** sits between the word and
    # the colon, so the pattern never saw them adjacent.
    "**Correction**: the year is 1983.",
    "This has been corrected.",
    "The value was corrected from 1970.",
    "The correct figure is 73%, corrected in the notes.",
])
def test_edit_narration_is_still_caught_however_it_is_dressed(leak):
    assert _guard_hits(f"#H1: Topic\n- {leak}\n"), leak


def test_a_leak_is_reported_rather_than_thrown():
    """The notes must be produced either way.

    Raising here discarded the transcript, the notes and the QC pass — all
    already paid for — over a phrase a reviewer rewords in ten seconds. The
    finding now goes to the Error Report, which the reviewer reads and the
    student never sees.
    """
    from src.correction_language_guard import (
        correction_language_rows,
        find_correction_metalanguage,
    )
    from src.error_report_writer import normalise_rows

    hits = find_correction_metalanguage("#H1: T\n- Correction: the year is 1983.\n")
    rows = correction_language_rows(hits)
    assert len(rows) == len(hits) == 1
    assert rows[0]["severity"] == "High"
    assert rows[0]["original_statement"].startswith("Line 2:")
    # And it survives the sheet's strictness filter at every setting — a note
    # that narrates its own edit is not a matter of taste.
    for strictness in ("basic", "standard", "strict"):
        assert normalise_rows([dict(rows[0])], strictness=strictness), strictness


def test_the_pipeline_no_longer_aborts_on_correction_language():
    """The guard is wired as a report, not a gate."""
    source = (Path(__file__).resolve().parents[1] / "src" / "pipeline.py").read_text(
        encoding="utf-8")
    assert "assert_no_correction_metalanguage(notes_text)" not in source
    assert "correction_language_hits = find_correction_metalanguage(notes_text)" in source
    assert "correction_language_rows" in source


def test_the_prompt_no_longer_bans_the_bare_words():
    """The prompt and the guard have to agree.

    While the prompt said never write "corrected", a vision lecture could only
    obey it by refusing to teach the topic.
    """
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "upsc_dcn_en.txt").read_text(
        encoding="utf-8")
    assert '- "Correction:", "corrected", "rectified"' not in prompt
    # Whitespace-normalised: the sentence wraps, so a raw substring search finds
    # nothing and the test would be reporting on its own formatting.
    assert "corrected using a concave lens" in " ".join(prompt.split())
