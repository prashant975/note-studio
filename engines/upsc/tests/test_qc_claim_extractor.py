"""Offline, deterministic tests for src/qc/claim_extractor.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.claim_extractor import (
    MAX_CANDIDATES,
    extract_claim_candidates,
    merge_ai_claims,
)
from src.qc.models import FactualClaim


NOTES = """# Constitutional Framework

Article 21 of the Constitution guarantees the Right to Life and Personal Liberty.

Indian Economy Today

- India's services sector contributes 54% to Gross Value Added.
- Agriculture contributed 18% to GDP in FY 2023-24.
- India is the largest producer of milk in the world.

Judiciary and Landmark Cases

- In Kesavananda Bharati v. State of Kerala, the court laid down the basic structure doctrine.
- Nehru said, "At the stroke of the midnight hour, India will awake to life and freedom."

Committees and Reports

- The Sarkaria Commission examined Centre-State relations.
- The Revolt of 1857 began in Meerut.

[Faculty Point Corrected]
Original faculty point: Article 370 was repealed in 2020.
Corrected version: Article 370 was effectively abrogated on 5 August 2019.
Reason: The Presidential Order was issued in 2019.
Confidence: High

Note to DTP: recreate the GDP chart from slide 7.

- Manufacturing contributes 17% of GDP.
"""


def _claims():
    return extract_claim_candidates(NOTES, "polity")


def _by_type(claims, claim_type):
    return [c for c in claims if c.claim_type == claim_type]


# ---------------------------------------------------------------- extraction

def test_empty_and_bad_input_yield_no_claims():
    assert extract_claim_candidates("", "polity") == []
    assert extract_claim_candidates("   \n\n ", "polity") == []
    assert extract_claim_candidates(None, "polity") == []


def test_article_claim_with_heading_and_paragraph_anchor():
    claims = _claims()
    articles = _by_type(claims, "constitutional_article")
    assert len(articles) == 1
    c = articles[0]
    assert "Article 21" in c.text
    assert c.heading == "Constitutional Framework"
    assert isinstance(c.paragraph_index, int)
    assert c.subject == "polity"
    assert c.risk_level == "high"
    assert not c.date_sensitive


def test_percentage_without_year_requires_period():
    claims = _claims()
    c = next(c for c in claims if "54%" in c.text)
    assert c.claim_type == "economic_statistic"
    assert c.date_sensitive is True
    assert c.requires_period is True
    assert c.heading == "Indian Economy Today"


def test_percentage_with_fy_does_not_require_period():
    claims = _claims()
    c = next(c for c in claims if "18%" in c.text)
    assert c.claim_type == "economic_statistic"
    assert c.date_sensitive is True
    assert c.requires_period is False


def test_superlative_claim_is_high_risk():
    claims = _claims()
    c = next(c for c in claims if "largest producer" in c.text)
    assert c.claim_type == "superlative_claim"
    assert c.risk_level == "high"


def test_judgment_case_name_stays_one_sentence():
    claims = _claims()
    judgments = _by_type(claims, "judgment")
    assert len(judgments) == 1
    # 'v.' must not have split the case name in half.
    assert "Kesavananda Bharati v. State of Kerala" in judgments[0].text


def test_quotation_claim():
    claims = _claims()
    quotes = _by_type(claims, "quotation")
    assert len(quotes) == 1
    assert "stroke of the midnight hour" in quotes[0].text


def test_committee_attribution_claim():
    claims = _claims()
    c = next(c for c in claims if "Sarkaria" in c.text)
    assert c.claim_type == "attribution"
    assert c.heading == "Committees and Reports"


def test_bare_year_falls_back_to_historical_date():
    claims = _claims()
    c = next(c for c in claims if "1857" in c.text)
    assert c.claim_type == "historical_date"
    assert c.date_sensitive is False


def test_correction_marker_block_is_skipped():
    claims = _claims()
    joined = " | ".join(c.text for c in claims)
    assert "Article 370" not in joined
    assert "Presidential Order" not in joined


def test_slide_anchor_from_adjacent_dtp_note():
    claims = _claims()
    c = next(c for c in claims if "Manufacturing" in c.text)
    assert c.source_slide == 7
    # The DTP instruction line itself must never become a claim.
    assert not any("recreate the GDP chart" in c.text for c in claims)


def test_ids_are_sequential_in_document_order():
    claims = _claims()
    assert [c.claim_id for c in claims] == [
        f"CLM-{i:03d}" for i in range(1, len(claims) + 1)
    ]
    order = [c.claim_id for c in claims]
    assert order == sorted(order)


def test_entities_extracted_and_capped():
    claims = _claims()
    article = _by_type(claims, "constitutional_article")[0]
    assert "21" in article.entities
    assert all(len(c.entities) <= 6 for c in claims)


def test_extraction_stops_at_the_candidate_cap():
    """The cap is a guard against a malformed note, not a sampling rate — it is
    set above what a length-limited note can hold. Read from the constant so
    raising the policy is not a test failure."""
    over = MAX_CANDIDATES + 40
    lines = "\n".join(f"- Item number {i} grew by {i}%." for i in range(1, over + 1))
    claims = extract_claim_candidates(lines, "economy")
    assert len(claims) == MAX_CANDIDATES
    assert claims[0].claim_id == "CLM-001"
    assert claims[-1].claim_id == f"CLM-{MAX_CANDIDATES:03d}"


def test_a_whole_note_fits_inside_the_candidate_cap():
    """The point of raising it: every claim in a full-length note is extracted,
    so nothing is dropped before verification is even considered."""
    from src.config import DCN_TARGET_PAGES, DCN_WORDS_PER_PAGE

    words = DCN_TARGET_PAGES[1] * DCN_WORDS_PER_PAGE["en"]
    # A dense note carries a checkable claim roughly every 40 words.
    assert MAX_CANDIDATES > words / 40, (
        f"{MAX_CANDIDATES} candidates cannot cover a {words}-word note")


# ------------------------------------------------------------------- merging

def _candidate():
    return FactualClaim(
        claim_id="CLM-001",
        text="India's services sector contributes 54% to Gross Value Added.",
        claim_type="economic_statistic",
        subject="economy",
        heading="Indian Economy Today",
        paragraph_index=3,
        date_sensitive=True,
        requires_period=True,
        entities=["54%"],
    )


def test_merge_dedupes_similar_text_and_appends_new_claims():
    ai = [
        {
            "text": "India's services sector contributes 54% to Gross Value Added",
            "claim_type": "economic_statistic",
            "heading": "Economy",
            "risk_level": "medium",
            "date_sensitive": True,
            "requires_period": True,
            "entities": ["Gross Value Added"],
        },
        {
            "text": "The Chola dynasty built the Brihadeeswara Temple at Thanjavur.",
            "claim_type": "historical_date",
            "heading": "Art and Culture",
            "risk_level": "high",
            "date_sensitive": False,
            "requires_period": False,
            "entities": ["Brihadeeswara Temple"],
        },
    ]
    merged = merge_ai_claims([_candidate()], ai, "economy")
    assert len(merged) == 2
    # Deduped: the richer deterministic record wins but is enriched.
    kept = merged[0]
    assert kept.heading == "Indian Economy Today"
    assert kept.paragraph_index == 3
    assert "54%" in kept.entities and "Gross Value Added" in kept.entities
    assert kept.risk_level == "high"          # highest severity of the pair
    # New AI claim appended after the candidates, in review order.
    new = merged[1]
    assert "Chola" in new.text
    assert new.subject == "economy"
    assert new.claim_type == "historical_date"
    assert new.heading == "Art and Culture"


def test_merge_keeps_richer_ai_record_when_candidate_is_bare():
    bare = FactualClaim(
        claim_id="CLM-001",
        text="The Finance Commission devolves 41% of taxes to states",
        claim_type="economic_statistic",
        subject="polity",
    )
    ai = [{
        "text": "The Finance Commission devolves 41% of taxes to states.",
        "claim_type": "economic_statistic",
        "heading": "Fiscal Federalism",
        "risk_level": "high",
        "date_sensitive": True,
        "requires_period": True,
        "entities": ["Finance Commission", "41%"],
    }]
    merged = merge_ai_claims([bare], ai, "polity")
    assert len(merged) == 1
    c = merged[0]
    assert c.heading == "Fiscal Federalism"
    assert c.date_sensitive is True and c.requires_period is True
    assert "Finance Commission" in c.entities


def test_merge_renumbers_ids_densely_and_in_stable_order():
    candidates = [_candidate()]
    candidates[0].claim_id = "CLM-017"      # stale id from a previous pass
    merged = merge_ai_claims(candidates, [], "economy")
    assert [c.claim_id for c in merged] == ["CLM-001"]
    # Caller's objects are not mutated.
    assert candidates[0].claim_id == "CLM-017"


def test_merge_skips_junk_ai_entries_without_crashing():
    junk = [
        {"claim_type": "economic_statistic"},     # no text
        {"text": "   "},                          # blank text
        "not a dict",
        None,
        42,
    ]
    merged = merge_ai_claims([_candidate()], junk, "economy")
    assert len(merged) == 1
    assert merged[0].claim_id == "CLM-001"


def test_merge_caps_the_total_at_the_same_ceiling():
    candidates = [
        FactualClaim(
            claim_id=f"CLM-{i:03d}",
            text=f"Fact {i}",
            claim_type="historical_date",
            subject="history",
        )
        for i in range(1, MAX_CANDIDATES + 11)
    ]
    merged = merge_ai_claims(candidates, [], "history")
    assert len(merged) == MAX_CANDIDATES
    assert merged[-1].claim_id == f"CLM-{MAX_CANDIDATES:03d}"


def test_merge_collapses_ai_claims_that_repeat_each_other():
    """Two AI nominations of the same fact must yield one claim, so the
    verifier never spends a source lookup twice on one sentence."""
    ai = [
        {"text": "The 73rd Amendment gave constitutional status to panchayats.",
         "claim_type": "amendment"},
        {"text": "The 73rd Amendment gave constitutional status to panchayats",
         "claim_type": "amendment", "heading": "Local Government"},
    ]
    merged = merge_ai_claims([], ai, "polity")
    assert len(merged) == 1
    assert merged[0].heading == "Local Government"   # backfilled from the richer twin
