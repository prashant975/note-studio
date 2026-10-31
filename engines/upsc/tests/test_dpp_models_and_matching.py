from __future__ import annotations

from src.notes_to_dpp.matching import match_coverage
from src.notes_to_dpp.models import NoteDocument, NotePage, QuestionTag, TaxonomyRow


def test_composite_keys_include_every_hierarchy_identifier() -> None:
    row = TaxonomyRow(
        row_id="ROW-17",
        subject="Indian Physical Geography",
        subject_id="S-IND",
        chapter="Drainage Systems of India",
        chapter_id="C-DRAINAGE",
        topic="River Systems",
        topic_id="T-RIVERS",
        subtopic="Himalayan Rivers",
        subtopic_id="ST-HIMALAYAN",
    )
    tag = QuestionTag(
        subject_id="S-IND",
        chapter_id="C-DRAINAGE",
        topic_id="T-RIVERS",
        subtopic_id="ST-HIMALAYAN",
    )

    assert row.composite_key == (
        "S-IND",
        "C-DRAINAGE",
        "T-RIVERS",
        "ST-HIMALAYAN",
    )
    assert tag.composite_key == row.composite_key


def test_match_results_hydrate_authoritative_rows_and_keep_blank_subtopic() -> None:
    topic_row = TaxonomyRow(
        row_id="ROW-TOPIC",
        subject="Indian Physical Geography",
        subject_id="S-IND",
        chapter="Drainage Systems of India",
        chapter_id="C-DRAINAGE",
        topic="River Systems",
        topic_id="T-RIVERS",
        subtopic=None,
        subtopic_id=None,
    )
    subtopic_row = TaxonomyRow(
        row_id="ROW-SUBTOPIC",
        subject="Indian Physical Geography",
        subject_id="S-IND",
        chapter="Drainage Systems of India",
        chapter_id="C-DRAINAGE",
        topic="River Systems",
        topic_id="T-RIVERS",
        subtopic="Himalayan Rivers",
        subtopic_id="ST-HIMALAYAN",
    )
    taxonomy = (topic_row, subtopic_row)
    document = NoteDocument(
        filename="Indian Physical Geography notes.pdf",
        pages=(
            NotePage(
                page_number=1,
                text=(
                    "Indian Physical Geography\n"
                    "Drainage Systems of India\n"
                    "River Systems\n"
                    "Himalayan Rivers"
                ),
                extraction_method="text",
            ),
        ),
    )

    candidates = match_coverage(document, taxonomy)
    taxonomy_by_id = {row.row_id: row for row in taxonomy}
    hydrated = tuple(taxonomy_by_id[candidate.row_id] for candidate in candidates)

    assert {candidate.row_id for candidate in candidates} == {
        "ROW-TOPIC",
        "ROW-SUBTOPIC",
    }
    assert all(candidate.row_id in taxonomy_by_id for candidate in candidates)
    assert set(hydrated) == set(taxonomy)
    hydrated_topic = taxonomy_by_id["ROW-TOPIC"]
    assert hydrated_topic.subtopic is None
    assert hydrated_topic.subtopic_id is None
    assert all(candidate.recommended for candidate in candidates)
