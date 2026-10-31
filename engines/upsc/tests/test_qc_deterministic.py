"""Offline tests for src/qc/deterministic_checks.py.

Every fixture is a small literal string; no network, no config files, no AI.
The clean fixture doubles as a regression net: any new check that fires on
ordinary well-formed notes is a false-positive factory and fails here first.
"""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.deterministic_checks import run_deterministic_checks


def rules(issues):
    return [i.rule_id for i in issues]


def by_rule(issues, rule_id):
    return [i for i in issues if i.rule_id == rule_id]


PAD = (
    "The chapter closes with a short recap of the administrative changes and "
    "their long-term effects on the countryside and the towns alike."
)

CLEAN = """\
# Modern Indian History — Revolt of 1857

## Causes of the Revolt

The revolt of 1857 began as a sepoy mutiny and grew into a wider uprising.
Economic distress in the countryside pushed peasants toward the rebel camps.

- Political causes included the doctrine of lapse.
- Economic causes included heavy land revenue demands.
- Military causes included discontent over service conditions.

## Course of Events

The rising spread from Meerut to Delhi, where Bahadur Shah was proclaimed leader.
Cities such as Kanpur, Lucknow and Jhansi became major centres of resistance.

| Centre | Leader |
| --- | --- |
| Kanpur | Nana Saheb |
| Jhansi | Rani Lakshmibai |
| Lucknow | Begum Hazrat Mahal |

## Consequences

The rule of the East India Company ended and the Crown took direct control.
The army was reorganised and the policy of annexation was quietly dropped.
"""


def test_clean_notes_produce_zero_issues():
    assert run_deterministic_checks(CLEAN, slides=None, enrichments=None) == []


def test_duplicate_headings_flagged_once_per_heading():
    text = (
        "## Consequences\n\nFirst version of the section with enough text to matter.\n\n"
        "## Consequences\n\nSecond version of the section, also with content.\n\n" + PAD
    )
    found = by_rule(run_deterministic_checks(text), "DET-DUP-HEADING")
    assert len(found) == 1
    assert found[0].heading == "Consequences"
    assert found[0].category == "conceptual"


def test_empty_headings_same_level_and_trailing():
    text = (
        "## Section A\n\n## Section B\n\n"
        "Real content sits under this second heading so only the first one and "
        "the trailing one are empty. " + PAD + "\n\n## Trailing\n"
    )
    found = by_rule(run_deterministic_checks(text), "DET-EMPTY-HEADING")
    assert sorted(i.heading for i in found) == ["Section A", "Trailing"]


def test_title_followed_by_subheading_is_not_empty():
    text = "# Chapter Title\n\n## First Section\n\n" + PAD
    assert by_rule(run_deterministic_checks(text), "DET-EMPTY-HEADING") == []


def test_duplicate_paragraphs_flagged_with_index():
    para = (
        "The Regulating Act of 1773 created the office of Governor-General of "
        "Bengal and set up a Supreme Court at Calcutta."
    )
    text = para + "\n\n" + PAD + "\n\n" + para + "\n"
    found = by_rule(run_deterministic_checks(text), "DET-DUP-PARA")
    assert len(found) == 1
    assert found[0].paragraph_index == 2
    assert found[0].source == "deterministic"


def test_near_duplicate_paragraphs_flagged():
    a = (
        "The Charter Act of 1833 centralised legislation and made the Governor-"
        "General of Bengal the Governor-General of India with exclusive "
        "legislative powers over the whole territory."
    )
    b = a.replace("exclusive", "complete")
    issues = run_deterministic_checks(a + "\n\n" + b + "\n")
    found = by_rule(issues, "DET-NEARDUP-PARA")
    assert len(found) == 1
    assert found[0].paragraph_index == 1
    assert by_rule(issues, "DET-DUP-PARA") == []


def test_slide_reference_checks(tmp_path):
    img = tmp_path / "slide1.png"
    img.write_bytes(b"png")
    slides = [
        SimpleNamespace(slide_no=1, image_path=str(img)),
        SimpleNamespace(slide_no=2, image_path=None),
    ]
    text = (
        '(Note to DTP: take the diagram from slide 7 under the heading "Nitrogen Cycle")\n\n'
        '(Note to DTP: take the chart from slide 2 under the heading "Carbon Cycle")\n\n'
        '(Note to DTP: take the map from slide 1 under the heading "Water Cycle")\n\n' + PAD
    )
    issues = run_deterministic_checks(text, slides=slides)
    bad_ref = by_rule(issues, "DET-SLIDE-REF")
    bad_img = by_rule(issues, "DET-SLIDE-IMAGE")
    assert len(bad_ref) == 1 and "slide 7" in bad_ref[0].problem
    assert bad_ref[0].severity == "high"
    assert len(bad_img) == 1 and "slide 2" in bad_img[0].problem


def test_slides_none_skips_slide_checks():
    text = '(Note to DTP: take the diagram from slide 9 under the heading "Alpha")\n\n' + PAD
    issues = run_deterministic_checks(text, slides=None)
    assert by_rule(issues, "DET-SLIDE-REF") == []


def test_markup_leaks_html_json_and_fences():
    text = (
        PAD + "\n\n"
        '<div class="note">leftover html</div>\n\n'
        "```\n\n"
        "{\n"
        '  "key": "value"\n'
        "}\n"
    )
    found = by_rule(run_deterministic_checks(text), "FLOW-FORMAT-004")
    assert len(found) == 3
    problems = " ".join(i.problem for i in found)
    assert "HTML" in problems
    assert "fence" in problems
    assert "JSON" in problems
    assert all(i.category == "formatting" for i in found)


def test_internal_markers_and_repeated_correction_markers():
    text = (
        "NOTES_START\n\n" + PAD + "\n\n"
        "[transcript unavailable 00:10-00:14]\n\n"
        "[Faculty Point Corrected] old figure replaced with the census value.\n"
        "[Faculty Point Corrected] old figure replaced with the census value again.\n"
    )
    issues = run_deterministic_checks(text)
    markers = by_rule(issues, "DET-INTERNAL-MARKER")
    assert len(markers) == 2  # NOTES_START + transcript gap
    assert all(i.severity == "high" for i in markers)
    assert len(by_rule(issues, "DET-REPEAT-MARKER")) == 1


def test_malformed_and_single_row_tables():
    text = (
        PAD + "\n\n"
        "| Article | Provision | Notes |\n"
        "| --- | --- | --- |\n"
        "| 14 |\n\n"
        "| Orphan row |\n"
    )
    found = by_rule(run_deterministic_checks(text), "FLOW-FORMAT-004")
    assert len(found) == 2
    problems = " ".join(i.problem for i in found)
    assert "Malformed table" in problems
    assert "Unclosed table" in problems


def test_long_sentences_capped_at_five():
    sentence = ("filler " * 61).strip() + "."
    text = " ".join(sentence for _ in range(7))
    found = by_rule(run_deterministic_checks(text), "LANG-CLARITY-003")
    assert len(found) == 5
    assert all(i.category == "language" for i in found)


def test_mixed_date_formats_single_summary_issue():
    text = (
        "The Dandi March began on 12 March 1930 and the salt law was broken on "
        "06/04/1930 at the coastal village. " + PAD
    )
    found = by_rule(run_deterministic_checks(text), "DET-DATE-FORMAT")
    assert len(found) == 1
    assert "12 March 1930" in found[0].problem


def test_inconsistent_proper_noun_spellings():
    text = (
        "Ambedkar chaired the drafting committee and Ambedkar presented the "
        "draft in 1948. Later pages spell the name Ambedkhar in two places, and "
        "Ambedkhar appears again in the summary. The heading says INDIA twice: "
        "INDIA, while the body says India and India. " + PAD
    )
    found = by_rule(run_deterministic_checks(text), "FACT-NAMES-005")
    assert len(found) == 2
    pairs = " ".join(i.problem for i in found)
    assert "Ambedkhar" in pairs
    assert "INDIA" in pairs
    assert all(i.category == "language" for i in found)


def test_plural_proper_nouns_not_flagged():
    text = (
        "The Mughal empire expanded steadily and the Mughal court patronised "
        "art. The Mughals taxed the peasantry and the Mughals built new roads. "
        + PAD
    )
    assert by_rule(run_deterministic_checks(text), "FACT-NAMES-005") == []


def test_broken_bullet_nesting():
    text = (
        PAD + "\n\n"
        "- Fundamental rights are enforceable in the courts.\n"
        "              - This bullet is indented far too deep under its parent.\n"
    )
    found = by_rule(run_deterministic_checks(text), "DET-BULLET-NEST")
    assert len(found) == 1


def test_placeholder_text_flagged():
    text = (
        PAD + "\n\n"
        "TODO: add the map of trade routes here.\n"
        "The section on XXX will be completed later.\n"
        "- ...\n"
        "Lorem ipsum dolor sit amet remains from the template.\n"
        "<placeholder for chart>\n"
    )
    found = by_rule(run_deterministic_checks(text), "DET-PLACEHOLDER")
    assert len(found) == 5
    assert all(i.severity == "high" for i in found)


def test_short_notes_high_severity():
    found = by_rule(run_deterministic_checks("Too short."), "DET-LENGTH")
    assert len(found) == 1
    assert found[0].severity == "high"


def test_enrichment_shortfalls_reported():
    enrich = {
        "add_tables": True,
        "add_flowcharts": True,
        "add_prelims_keywords": True,
        "add_mains_value": True,
        "check_facts": True,
    }
    text = (
        "The Directive Principles guide the State in making laws for welfare. "
        "They are non-justiciable yet central to governance debates. " + PAD
    )
    got = rules(run_deterministic_checks(text, enrichments=enrich))
    for rule_id in ("ENRICH-TABLES-001", "ENRICH-FLOW-002",
                    "ENRICH-PRELIMS-004", "ENRICH-MAINS-003"):
        assert rule_id in got


def test_enrichment_satisfied_produces_no_compliance_issues():
    enrich = {
        "add_tables": True,
        "add_flowcharts": True,
        "add_prelims_keywords": True,
        "add_mains_value": True,
    }
    text = (
        PAD + "\n\n"
        "| Stage | Output |\n"
        "| --- | --- |\n"
        "| One | Draft |\n\n"
        "Process: evaporation → condensation as a simple flow.\n\n"
        "Prelims pointer: remember the article numbers.\n\n"
        "Mains angle: evaluate the doctrine critically.\n"
    )
    issues = run_deterministic_checks(text, enrichments=enrich)
    assert [i for i in issues if i.rule_id.startswith("ENRICH-")] == []


def test_cap_appends_truncation_notice_and_ids_unique():
    """Over the cap, the findings are truncated AND the truncation is announced.

    The number is read from the constant: the cap was raised to cover a whole
    note, and what is being tested is that going over it is still declared —
    not what the ceiling happens to be this month.
    """
    from src.qc.deterministic_checks import MAX_TOTAL_ISSUES

    over = MAX_TOTAL_ISSUES + 20
    text = "\n".join(f"TODO: pending item number {i} of the outline" for i in range(over))
    issues = run_deterministic_checks(text)
    assert len(issues) == MAX_TOTAL_ISSUES + 1, "capped findings plus the notice"
    assert issues[-1].rule_id == "DET-TRUNCATED"
    assert "suppressed" in issues[-1].problem
    assert len({i.issue_id for i in issues}) == MAX_TOTAL_ISSUES + 1
    assert all(i.source == "deterministic" for i in issues)


def test_none_input_does_not_crash():
    found = by_rule(run_deterministic_checks(None), "DET-LENGTH")
    assert len(found) == 1 and found[0].severity == "high"
