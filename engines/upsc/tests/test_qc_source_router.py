"""SourceRouter tests: subject-string routing to approved sources.

Runs entirely offline against the real config/upsc_qc_sources.yaml — the file
is part of the repo, so these tests double as a lint of the shipped alias
table (every alias must land on a real subject).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.qc import qc_config
from src.qc.source_router import HISTORY_UMBRELLA, SourceRouter, SubjectSources


@pytest.fixture()
def router() -> SourceRouter:
    # force_reload so a previous test's cache state can never leak in.
    return SourceRouter(registry=qc_config.load_sources(force_reload=True))


# ---------------------------------------------------------------- normalise


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Science & Tech", "science_technology"),
        ("ir", "international_relations"),
        ("भारतीय राजव्यवस्था", "polity"),
        ("Polity", "polity"),
        ("  POLITY  ", "polity"),
        ("history", HISTORY_UMBRELLA),
    ],
)
def test_normalise_known_aliases(router, raw, expected):
    assert router.normalise(raw) == expected


def test_normalise_exact_registry_key(router):
    assert router.normalise("science_technology") == "science_technology"


def test_normalise_subject_label_fallback(router):
    # "Indian Polity & Governance" is a SUBJECT_LABELS display name that is
    # NOT in the YAML alias table — it must resolve via its app key "polity".
    assert router.normalise("Indian Polity & Governance") == "polity"


def test_normalise_unknown_returns_none(router):
    assert router.normalise("Astrology") is None
    assert router.normalise("") is None
    assert router.normalise("   ") is None
    assert router.normalise(None) is None
    # "Current Affairs" is a real UI label but has no source registry entry:
    # it must NOT silently borrow another subject's sources.
    assert router.normalise("Current Affairs") is None


def test_every_yaml_alias_resolves_to_a_real_subject(router):
    registry = qc_config.load_sources()
    assert registry.subject_aliases, "alias table unexpectedly empty"
    for alias, target in registry.subject_aliases.items():
        assert router.normalise(alias) == target, f"alias {alias!r} did not resolve"
        sources = router.get_sources(alias)
        assert sources.configured, f"alias {alias!r} resolved but is unconfigured"
        if target != HISTORY_UMBRELLA:
            assert target in registry.subjects


# -------------------------------------------------------------- get_sources


def test_get_sources_known_subject(router):
    s = router.get_sources("  POLITY  ")
    assert isinstance(s, SubjectSources)
    assert s.configured
    assert s.subject_key == "polity"
    assert s.display_name == "Indian Polity"
    assert "legislative.gov.in" in s.official_domains
    assert "constitutional_article" in s.claim_types
    assert s.warnings == []


def test_get_sources_unknown_subject(router):
    s = router.get_sources("Astrology")
    assert not s.configured
    assert s.books == []
    assert s.official_domains == []
    assert s.official_publications == []
    assert s.claim_types == []
    assert s.warnings == [
        "no approved-source configuration for subject 'Astrology'; "
        "external factual verification unavailable"
    ]


def test_get_sources_blank_subject(router):
    s = router.get_sources("   ")
    assert not s.configured
    assert "no approved-source configuration for subject ''" in s.warnings[0]


def test_local_index_paths_pass_through_untouched(router):
    s = router.get_sources("Polity")
    by_id = {b["id"]: b for b in s.books}
    assert by_id["laxmikanth"]["local_index"] == "reference_index/polity/laxmikanth"


def test_returned_lists_are_copies_not_registry_views(router):
    first = router.get_sources("Polity")
    first.books.append({"id": "injected"})
    first.books[0]["name"] = "tampered"
    first.official_domains.append("evil.example")
    again = router.get_sources("Polity")
    assert all(b["id"] != "injected" for b in again.books)
    assert again.books[0]["name"] == "M. Laxmikanth - Indian Polity"
    assert "evil.example" not in again.official_domains


# ---------------------------------------------------------- history umbrella


def test_history_umbrella_merges_three_subjects(router):
    s = router.get_sources("History")
    assert s.configured
    assert s.subject_key == HISTORY_UMBRELLA
    assert s.display_name == "History (combined)"

    book_ids = [b["id"] for b in s.books]
    # One representative book from each merged subject.
    assert "rs_sharma" in book_ids          # ancient
    assert "satish_chandra" in book_ids     # medieval
    assert "spectrum" in book_ids           # modern
    # Shared book appears exactly once, first occurrence (ancient) winning.
    assert book_ids.count("poonam_dalal_dahiya") == 1
    poonam = next(b for b in s.books if b["id"] == "poonam_dalal_dahiya")
    assert poonam["local_index"] == "reference_index/history/ancient/poonam_dalal"

    # Claim types from all three, deduped but order-preserving.
    assert "dynasty" in s.claim_types
    assert "movement" in s.claim_types
    assert s.claim_types.count("historical_date") == 1

    assert any("umbrella" in w for w in s.warnings)


def test_history_umbrella_survives_missing_part():
    registry = qc_config.load_sources(force_reload=True)
    trimmed = qc_config.SourceRegistry(
        version=registry.version,
        source_priority=list(registry.source_priority),
        blocked_source_types=list(registry.blocked_source_types),
        subject_aliases=dict(registry.subject_aliases),
        subjects={
            k: v for k, v in registry.subjects.items() if k != "medieval_history"
        },
    )
    s = SourceRouter(registry=trimmed).get_sources("history")
    assert s.configured
    book_ids = [b["id"] for b in s.books]
    assert "rs_sharma" in book_ids and "spectrum" in book_ids
    assert "satish_chandra" not in book_ids
    assert any("medieval_history" in w for w in s.warnings)
