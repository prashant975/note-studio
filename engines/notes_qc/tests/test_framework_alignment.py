"""The PW OnlyIAS Notes QC Framework must reach every run.

The framework is the policy this app exists to enforce. It lives in three
places that can drift apart:

  reference_documents/notes_qc_framework.md   the readable original
  config/upsc_qc_rules.yaml                   the checks, machine-readable
  config/upsc_qc_sources.yaml                 the per-subject source registry

and it only does any good if it reaches the prompt that produces the Error
Report. These tests pin all four: every framework check has a rule, every
subject in the framework's source table has an entry, the blocked sources are
actually blocked, and the prompt carries both.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.qc_config import (  # noqa: E402
    framework_checklist,
    load_rules,
    load_sources,
    resolve_subject,
    source_guidance,
)
from src.qc_prompts import build_pdf_error_report_prompt  # noqa: E402

FRAMEWORK_DOC = Path(__file__).resolve().parents[1] / "reference_documents" / "notes_qc_framework.md"


def test_the_framework_document_is_present():
    """config/upsc_qc_rules.yaml names this file as its policy source."""
    assert FRAMEWORK_DOC.exists(), (
        "the framework document the rule file points at is missing")
    text = FRAMEWORK_DOC.read_text(encoding="utf-8")
    for heading in ("Factual Error", "Concept/Flow Error",
                    "English/Grammar Error", "Sources to be used"):
        assert heading in text, f"framework document lost its {heading!r} section"


# --------------------------------------------------------------------- checks

#: The framework's three categories and how many checks each defines.
FRAMEWORK_COUNTS = {"factual": 6, "conceptual": 4, "language": 3}


@pytest.mark.parametrize("category,minimum", sorted(FRAMEWORK_COUNTS.items()))
def test_every_framework_check_has_a_rule(category, minimum):
    rules = load_rules()
    assert len(rules.rules_for(category)) >= minimum, (
        f"the framework defines {minimum} {category} checks; "
        f"config/upsc_qc_rules.yaml has fewer")


def test_subject_specific_source_matching_is_a_rule_of_its_own():
    """Framework Factual #5, which the rule file originally omitted.

    It is not the same as FACT-SOURCE-002: that asks whether a source is
    authentic, this asks whether it is the right one for the subject. A Polity
    claim checked against a Geography textbook passes the first and fails this.
    """
    ids = {r.id for r in load_rules().rules_for("factual")}
    assert "FACT-SUBJECT-007" in ids


# -------------------------------------------------------------------- sources

#: Every subject in the framework's "Sources to be used" table, and one book
#: that must be prescribed for it.
FRAMEWORK_SUBJECTS = {
    "polity": "Laxmikanth",
    "modern_history": "Spectrum",
    "ancient_history": "Sharma",
    "medieval_history": "Satish Chandra",
    "world_history": "NCERT",
    "post_independence": "Guha",
    "indian_geography": "Leong",
    "world_geography": "Leong",
    "economy": "Singh",
    "environment": "Shankar",
    "science_technology": "Agrahari",
    "art_culture": "Singhania",
    "internal_security": "ARC",
    "disaster_management": "NDMA",
    "governance": "ARC",
    "international_relations": "mea",
    "society_social_justice": "Ahuja",
    "agriculture": "Aayog",
    "ethics": "Lexicon",
    "government_schemes": "India Year Book",
    "essay": "Essay",
}


def test_every_framework_subject_is_in_the_registry():
    subjects = load_sources().subjects
    missing = sorted(set(FRAMEWORK_SUBJECTS) - set(subjects))
    assert not missing, f"subjects in the framework but not the registry: {missing}"


@pytest.mark.parametrize("subject,expected", sorted(FRAMEWORK_SUBJECTS.items()))
def test_each_subject_prescribes_its_own_standard_book(subject, expected):
    entry = load_sources().subjects[subject]
    blob = " ".join(str(b.get("name", "")) for b in (entry.get("books") or []))
    blob += " " + " ".join(entry.get("official_domains") or [])
    blob += " " + " ".join(entry.get("official_publications") or [])
    assert expected.lower() in blob.lower(), (
        f"{subject} should prescribe {expected!r}; registry has {blob!r}")


def test_wikipedia_and_coaching_material_are_blocked():
    """Framework Factual #2 forbids them outright."""
    blocked = {b.lower() for b in load_sources().blocked_source_types}
    assert "wikipedia" in blocked
    assert "coaching_website" in blocked


@pytest.mark.parametrize("label,key", [
    ("Polity", "polity"),
    ("indian polity", "polity"),
    ("भारतीय राजव्यवस्था", "polity"),
    ("Economy", "economy"),
    ("Art & Culture", "art_culture"),
    ("something unrecognised", ""),
    ("", ""),
])
def test_subjects_resolve_in_english_and_hindi(label, key):
    assert resolve_subject(label) == key


# --------------------------------------------------------------------- prompt

def test_the_error_report_prompt_carries_the_framework():
    """The framework used to reach only the review stage, not this one.

    The Error Report is the sheet a reviewer actually reads, so the checks and
    the approved sources have to travel with the call that produces it.
    """
    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.", subject="Polity")

    assert "THE CHECKS THIS REPORT MUST APPLY" in prompt
    assert "FACT-LEGAL-001" in prompt
    assert "FACT-SUBJECT-007" in prompt


def test_the_prompt_names_the_right_book_for_the_subject():
    polity = build_pdf_error_report_prompt("notes", subject="Polity")
    assert "Laxmikanth" in polity
    assert "legislative.gov.in" in polity

    ancient = build_pdf_error_report_prompt("notes", subject="Ancient History")
    assert "Sharma" in ancient
    assert "Laxmikanth" not in ancient, "a subject must not carry another's books"


def test_the_prompt_forbids_wikipedia_every_time():
    for subject in ("Polity", "Economy", "", "unrecognised subject"):
        prompt = build_pdf_error_report_prompt("notes", subject=subject)
        assert "wikipedia" in prompt.lower()
        assert "NEVER cite" in prompt


def test_an_unknown_subject_still_gets_source_guidance():
    """Subject detection is a guess, so it must fail useful, not silent."""
    guidance = source_guidance("")
    assert "standard prescribed" in guidance
    assert "wikipedia" in guidance.lower()


def test_the_checklist_renders_from_the_rule_file_not_a_hardcoded_copy():
    """Rendering from the YAML is what stops the prompt drifting from policy."""
    text = framework_checklist()
    for rule in load_rules().rules_for("factual"):
        assert rule.id in text, f"{rule.id} is in the rule file but not the prompt"
