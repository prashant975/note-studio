"""Gujarati is automatic while existing language mappings stay unchanged."""
import pytest

@pytest.mark.parametrize("batch", ["Gujarat Board", "Gujarati Board", "Gujarati", "Gujarat-2027", "ગુજરાતી માધ્યમ"])
def test_gujarati_batches_work_in_classic_and_catalog_sheets(client, batch):
    from sheets_bridge import catalog, mapping, runner

    assert catalog.language_for_batch(batch) == "gujarati"
    assert mapping.map_language(batch) == runner.map_language(batch) == "gujarati"


@pytest.mark.parametrize("batch,classic,catalog_language", [
    ("Hindi batch", "hindi", "hindi"), ("English batch", "english", "english"),
    ("Hinglish", "hinglish", "hinglish"), ("Telugu", "auto", "english"),
    ("Lakshya JEE 2027", "auto", "auto"),
])
def test_other_languages_keep_the_original_batch_mapping(client, batch, classic, catalog_language):
    from sheets_bridge import catalog, mapping

    assert mapping.map_language(batch) == classic
    assert catalog.language_for_batch(batch) == catalog_language


def test_gujarati_headers_keep_sheet_text_and_do_not_overwrite_translated_metadata(client):
    from sheets_bridge import mapping

    header = mapping.sheet_header(subject="Accountancy", chapter="Partnership\nભાગીદારી",
                                  lecture="01", language="gujarati")
    assert header.subject == "હિસાબશાસ્ત્ર" and header.chapter == "ભાગીદારી"
    assert header.lecture == "વ્યાખ્યાન - 01"
    unknown = mapping.sheet_header(subject="Unlisted subject", chapter="English only title", lecture="",
                                   language="gujarati")
    assert unknown.subject == unknown.chapter == ""


def test_the_page_explains_automatic_gujarati_without_a_language_selection(client):
    page = client.get("/automation").text
    assert "Gujarati is detected from Gujarat/Gujarati batch names" in page
    assert 'name="language"' not in page
