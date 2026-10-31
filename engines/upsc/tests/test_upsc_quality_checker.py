from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.upsc_quality_checker import DcnQualityReport, check_dcn_notes


# Padding keeps the "good" note above the 400-word default without hand-writing
# 400 words of prose; every padding line is a full sentence so it must not be
# counted as a fragment.
_PADDING_TOPICS = [
    "Montreal Protocol", "Kigali Amendment", "Ramsar Convention", "Ozone layer",
    "Carbon sink", "Biosphere reserve", "Eutrophication", "Bioaccumulation",
    "Wetland ecosystem", "Coral bleaching", "Mangrove cover", "Nitrogen cycle",
    "Ecological pyramid", "Keystone species", "Invasive species",
    "Habitat fragmentation", "Climate finance", "Green hydrogen mission",
    "Carbon market", "National Clean Air Programme", "Bharat Stage norms",
    "Compensatory afforestation", "Ecological succession", "Trophic level",
    "Biodiversity hotspot", "Wildlife corridor", "Environmental clearance",
    "Coastal regulation zone", "Solid waste rules", "Plastic waste rules",
]


def _padding_bullets() -> str:
    return "\n".join(
        f"- The {topic} was explained in the class with worked examples for revision."
        for topic in _PADDING_TOPICS
    )


GOOD_NOTES = f"""#LECTURE_TITLE: L01_DCN01_Environment and Ecology - Basics:

#H1: Ecosystem and Its Components

#H2: Definition and Structure

- An **ecosystem** is a functional unit in which living organisms interact with their physical surroundings.
- The biotic component includes producers, consumers and decomposers that transfer energy through the food chain.
- The abiotic component covers sunlight, temperature, water and soil nutrients that control productivity.

#H3: Energy Flow

- Energy flow in an ecosystem is unidirectional, and roughly ten per cent of it passes to the next trophic level.
- Decomposers return nutrients to the soil, so the nutrient cycle remains closed while energy keeps moving.

#TABLE_START
Component | Example | Role
Producers | Green plants | Fix solar energy
Consumers | Herbivores | Transfer energy upward
#TABLE_END

#H2: Greenhouse Gases and Warming

- Important current update: India reported an installed renewable capacity of 220 GW as of December 2024.
- CO₂ and CH₄ trap outgoing infrared radiation, and this raises the mean surface temperature of the planet.
- Methane (CH₄) has a global warming potential of about 84 over a 20-year horizon, far higher than CO₂ in the short term.

#H1: QUICK REVISION

{_padding_bullets()}
- The [Living Planet Report 2024](https://livingplanet.panda.org) recorded a 73% average decline in monitored wildlife populations between 1970 and 2020.

#H3: Key Sources

- The forest figures above come from the [India State of Forest Report 2023](https://fsi.nic.in), published by the Forest Survey of India.
"""


FRAGMENT_NOTES = """#LECTURE_TITLE: L02_DCN02_Biodiversity:

#H1: Key Terms

#H2: Definitions

- Ozone layer
- Kyoto Protocol
- Ramsar sites
- Hotspots
- Carbon sink
- Wetlands
- Corals
- Endemic species
"""


def test_good_dcn_note_scores_clean():
    report = check_dcn_notes(GOOD_NOTES)
    assert report.format_warnings == []
    assert report.factual_risk_points == []
    assert report.layout_warnings == []
    assert report.is_clean
    # Corrections are silent now: a clean note announces no fixes at all.
    assert report.corrections == 0
    assert report.updated_current_data >= 2  # "Important current update:" + "as of "


def test_fragment_heavy_note_is_flagged():
    report = check_dcn_notes(FRAGMENT_NOTES, min_words=10)
    joined = " ".join(report.format_warnings)
    assert "fragments" in joined
    assert "8 of 8 bullets" in joined


def test_needs_verification_markers_are_counted():
    notes = (
        "#LECTURE_TITLE: L03_DCN03_Climate Finance:\n"
        "#H1: Funding\n"
        "- The Green Climate Fund pledge figure quoted in class could not be confirmed. "
        "[Needs Verification: no official source was cited in the lecture]\n"
        "- The adaptation fund share was also quoted without a source. "
        "[needs verification: figure changes every replenishment cycle]\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    assert report.needs_verification == 2


def test_tabular_content_outside_table_block_is_flagged():
    notes = (
        "#LECTURE_TITLE: L04_DCN04_Renewables:\n"
        "#H1: Installed Capacity\n"
        "Source | Capacity | Share\n"
        "Solar | 89 GW | 40 per cent\n"
        "Wind | 45 GW | 20 per cent\n"
        "Hydro | 47 GW | 21 per cent\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    stray = [w for w in report.format_warnings if "#TABLE_START" in w]
    assert stray, report.format_warnings
    assert "pipe-separated rows" in stray[0]


def test_label_number_runs_outside_table_block_are_flagged():
    notes = (
        "#LECTURE_TITLE: L05_DCN05_Energy Mix:\n"
        "#H1: Capacity Split\n"
        "- Solar: 89 GW\n"
        "- Wind: 45 GW\n"
        "- Hydro: 47 GW\n"
        "- Nuclear: 8 GW\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    assert any("Label" in w for w in report.format_warnings), report.format_warnings


def test_unqualified_date_sensitive_claim_is_a_factual_risk():
    notes = (
        "#LECTURE_TITLE: L06_DCN06_Air Quality:\n"
        "#H1: Status\n"
        "- India currently has the largest installed solar capacity in South Asia.\n"
        "- India had 89 GW of solar capacity as of March 2025, according to the ministry.\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    assert len(report.factual_risk_points) == 1
    assert "currently" in report.factual_risk_points[0]


def test_title_and_heading_ladder_problems_are_flagged():
    notes = "#H2: Sub-heading first\n- A body sentence that is long enough to pass.\n"
    report = check_dcn_notes(notes, min_words=5)
    joined = " ".join(report.format_warnings)
    assert "#LECTURE_TITLE:" in joined
    assert "#H2 appears before any #H1" in joined


def test_mojibake_and_flattened_subscript_are_flagged():
    notes = (
        "#LECTURE_TITLE: L07_DCN07_Gases:\n"
        "#H1: Greenhouse Gases\n"
        "- CO₂ is the reference gas for global warming potential calculations.\n"
        "- CO2 concentration was measured at Mauna Loa by the observatory team.\n"
        "- The Â encoding artefact shows the text was decoded incorrectly here.\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    joined = " ".join(report.format_warnings)
    assert "Mojibake" in joined
    assert "Subscript lost" in joined


def test_layout_warnings_for_wide_rows_and_long_words():
    long_word = "x" * 45
    notes = (
        "#LECTURE_TITLE: L08_DCN08_Layout:\n"
        "#H1: Data\n"
        "#TABLE_START\n"
        "Parameter with a very long descriptive label | Observed value in the year under review | Remarks column\n"
        "#TABLE_END\n"
        f"- The term {long_word} was written without a break in the lecture slide.\n"
    )
    report = check_dcn_notes(notes, min_words=10)
    joined = " ".join(report.layout_warnings)
    assert "will overflow the 5.5 in column" in joined
    assert "unbreakable word of 45 characters" in joined


def test_short_notes_are_flagged():
    notes = "#LECTURE_TITLE: L09_DCN09_Tiny:\n#H1: One\n- A single short body sentence here.\n"
    report = check_dcn_notes(notes)
    assert any("Notes too short" in w for w in report.format_warnings)


def test_empty_notes_do_not_raise():
    report = check_dcn_notes("")
    assert report.corrections == 0
    assert report.format_warnings == ["Notes are empty — nothing was generated."]


def test_to_markdown_contains_every_count():
    report = DcnQualityReport(
        corrections=3,
        updated_current_data=2,
        needs_verification=1,
        format_warnings=["Format problem A"],
        factual_risk_points=["Risk B", "Risk C"],
        layout_warnings=[],
    )
    md = report.to_markdown()
    assert "| Correction announcements found (must be 0 — corrections are silent) | 3 |" in md
    assert "| Updated current-data statements | 2 |" in md
    assert "| Points marked [Needs Verification:] | 1 |" in md
    assert "| Format warnings | 1 |" in md
    assert "| Factual risk points | 2 |" in md
    assert "| Layout warnings | 0 |" in md
    assert "Format problem A" in md
    assert "Risk C" in md
    assert "Nothing looks likely to overflow" in md
    assert "What to do" in md
    assert "never appended to the student DOCX" in md


def test_to_markdown_is_self_contained_for_a_clean_run():
    md = check_dcn_notes(GOOD_NOTES).to_markdown()
    assert md.startswith("# DCN Notes - Quality Report")
    assert "No format problems found." in md
    assert "No unqualified date-sensitive claims found." in md


# --- Topics Covered: must NOT appear (the reference has no contents block) --

def test_a_topics_covered_block_is_reported():
    """The DCN layout goes straight from the title to the first H1; a contents
    block means the notes bypassed the repair pass that strips it."""
    with_block = GOOD_NOTES.replace(
        "#H1: Ecosystem and Its Components",
        "#TOPICS_COVERED_START\n- Ecosystem and its components\n#TOPICS_COVERED_END\n\n"
        "#H1: Ecosystem and Its Components")
    warnings = check_dcn_notes(with_block).format_warnings
    assert any("contents section" in w for w in warnings)


# --- Closing sections: every note ends with QUICK REVISION + Key Sources ----

def test_missing_quick_revision_is_reported():
    without = GOOD_NOTES.replace("#H1: QUICK REVISION", "#H1: Revision Points")
    warnings = check_dcn_notes(without).format_warnings
    assert any("QUICK REVISION" in w for w in warnings)


def test_missing_key_sources_is_reported():
    without = GOOD_NOTES.replace("#H3: Key Sources", "#H3: Sources of Note")
    warnings = check_dcn_notes(without).format_warnings
    assert any("Key Sources" in w for w in warnings)


# --- Lecture title ----------------------------------------------------------

def test_placeholder_title_is_reported_as_a_placeholder():
    """The exact failure from the reported run: it reached the student's page."""
    notes = GOOD_NOTES.replace(
        "#LECTURE_TITLE: L01_DCN01_Environment and Ecology - Basics:",
        "#LECTURE_TITLE: Lxx_DCNxx_Probity_in_Governance_RTI_and_Citizens_Charter")
    warnings = check_dcn_notes(notes).format_warnings
    assert any("placeholder" in w.lower() for w in warnings)
    assert any("unbreakable run of 56" in w for w in warnings)
