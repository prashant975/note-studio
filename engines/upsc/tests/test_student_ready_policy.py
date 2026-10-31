from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import SlideData
from src.prompt_builder import build_upsc_prompt
from src.quality_checker import parse_correction_blocks


def test_upsc_prompt_enforces_source_fidelity_and_math_tags():
    prompt = build_upsc_prompt(
        note_type="complete",
        area="auto",
        language_code="en",
        slides=[SlideData(slide_no=1, heading="Waves", text="v = f lambda")],
    )
    assert "The lecture slides and transcript are the only content base" in prompt
    assert "[[MATH_INLINE: latex_here]]" in prompt
    assert "[Correction Note]" in prompt
    assert "Quick Revision (8-15 bullets)" in prompt
    assert "Exclude every student question" in prompt
    assert "Retain every such example that appears in the slides or transcript" in prompt
    assert "SUBJECT-SPECIFIC KEYWORDS" in prompt


def test_new_english_correction_blocks_are_parsed():
    notes = """# Main Notes
[Correction Note]
Original point: "Article 20 creates the Election Commission."
Corrected version: "Article 324 provides for the Election Commission."
Reason: "The original Article number is wrong."
Confidence: High

[Needs Verification]
Point: "This is the latest global ranking."
Issue: "The lecture provides no source or year."
"""
    entries = parse_correction_blocks(notes)
    assert [e.kind for e in entries] == ["corrected", "review"]
    assert entries[0].correction.startswith("Article 324")
    assert entries[1].issue == "The lecture provides no source or year."


def test_new_hindi_correction_block_is_parsed():
    notes = """## मुख्य नोट्स
[सुधार नोट]
मूल बिंदु: "गलत तथ्य"
सही रूप: "सही तथ्य"
कारण: "तथ्यात्मक त्रुटि"
विश्वास स्तर: High
"""
    entries = parse_correction_blocks(notes)
    assert len(entries) == 1
    assert entries[0].kind == "corrected"
    assert entries[0].correction == "सही तथ्य"
