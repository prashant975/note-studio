from __future__ import annotations

from src.notes_to_dpp import matching, models, notes


def _document(*page_texts: str) -> models.NoteDocument:
    return models.NoteDocument(
        filename="notes.pdf",
        pages=tuple(
            models.NotePage(
                page_number=index,
                text=text,
                extraction_method="text",
            )
            for index, text in enumerate(page_texts, start=1)
        ),
    )


def test_language_and_localized_question_model_contract() -> None:
    assert models.Language.ENGLISH.value == "English"
    assert models.Language.HINDI.value == "Hindi"
    assert models.Language.UNKNOWN.value == "Unknown"

    localized = models.LocalizedQuestion(
        language=models.Language.HINDI,
        prompt="भारत की सबसे लंबी नदी कौन-सी है?",
        options=("गंगा", "गोदावरी"),
        correct_option_indexes=(0,),
        solution="सही उत्तर गंगा है।",
    )

    assert localized.language is models.Language.HINDI
    assert localized.options == ("गंगा", "गोदावरी")
    assert localized.correct_option_indexes == (0,)


def test_detect_note_language_uses_all_pages_and_ignores_numbers_and_acronyms() -> None:
    document = _document(
        "Indian Geography 2026",
        (
            "भारत में हिमालयी नदियों का अपवाह तंत्र समझाया गया है। "
            "GDP, ISRO और NITI Aayog जैसे संक्षिप्त नाम भाषा नहीं बदलते।"
        ),
    )

    detection = notes.detect_note_language(document)

    assert isinstance(detection, models.LanguageDetection)
    assert detection.language is models.Language.HINDI
    assert detection.devanagari_letters > detection.latin_letters
    assert 0.5 < detection.confidence <= 1.0


def test_detect_note_language_reports_english_and_unknown() -> None:
    english = notes.detect_note_language(
        _document("Indian rivers originate in mountains and drain into the sea.")
    )
    unknown = notes.detect_note_language(_document("2024 / 2025 -- 100%"))

    assert english.language is models.Language.ENGLISH
    assert english.latin_letters > 0
    assert unknown.language is models.Language.UNKNOWN
    assert unknown.devanagari_letters == 0
    assert unknown.latin_letters == 0


def test_exact_hindi_taxonomy_label_is_matchable() -> None:
    row = models.TaxonomyRow(
        row_id="ROW-HINDI",
        subject="भारतीय भूगोल",
        subject_id="S-1",
        chapter="अपवाह तंत्र",
        chapter_id="C-1",
        topic="हिमालयी नदियाँ",
        topic_id="T-1",
    )

    candidates = matching.match_coverage(
        _document("भारतीय भूगोल\nअपवाह तंत्र\nहिमालयी नदियाँ"),
        (row,),
    )

    assert tuple(candidate.row_id for candidate in candidates) == ("ROW-HINDI",)
    assert candidates[0].recommended


def test_hindi_alias_is_row_scoped_and_transliteration_is_not_translation() -> None:
    soil_row = models.TaxonomyRow(
        row_id="ROW-SOIL",
        subject="Indian Physical Geography",
        subject_id="S-1",
        chapter="Soils of India",
        chapter_id="C-1",
        topic="Soil Types",
        topic_id="T-1",
    )
    document = _document("भारतीय भूगोल\nमृदा के प्रकार\nकाली मिट्टी और जलोढ़ मिट्टी")

    # Transliteration maps scripts; it must not invent the semantic translation
    # from "mritika" to "soil" without an explicit, row-scoped alias.
    assert matching.match_coverage(document, (soil_row,)) == ()

    candidates = matching.match_coverage(
        document,
        (soil_row,),
        aliases_by_row_id={"ROW-SOIL": ("मृदा के प्रकार",)},
    )

    assert tuple(candidate.row_id for candidate in candidates) == ("ROW-SOIL",)
    assert candidates[0].recommended
    assert soil_row.topic == "Soil Types"
    assert soil_row.topic_id == "T-1"


def test_close_hindi_proper_noun_can_match_by_transliteration() -> None:
    row = models.TaxonomyRow(
        row_id="ROW-HIMALAYA",
        subject="Physical Geography",
        subject_id="S-1",
        chapter="Mountains",
        chapter_id="C-1",
        topic="Himalaya",
        topic_id="T-1",
    )

    candidates = matching.match_coverage(_document("हिमालय"), (row,))

    assert tuple(candidate.row_id for candidate in candidates) == ("ROW-HIMALAYA",)
