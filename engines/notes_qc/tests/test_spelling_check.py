"""The deterministic typo check: exact on what it claims, silent on the rest.

It is narrow on purpose. Only dropped-letter and transposed-letter typos are
provable from a dictionary without drowning UPSC notes in false alarms —
British spellings, Indian-English words, Latin, transliterated names and
proper nouns all fail a naive lookup. Every test here pins one of those
noise classes as SILENT, and the real typo classes as REPORTED.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.spelling_check import find_misspellings  # noqa: E402


def words(found):
    return {m.word: m.suggestion for m in found}


def test_dropped_letter_typos_are_reported_with_the_right_word():
    text = ("The Bill remves the exsting rule and provid a single register. "
            "The list is removes existing provide.")
    found = words(find_misspellings([(1, text)]))
    assert found == {"remves": "removes", "exsting": "existing",
                     "provid": "provide"}


def test_transposed_letters_are_reported():
    found = words(find_misspellings([(1, "The teh recieve committee. receive")]))
    assert "recieve" in found
    assert found["recieve"] == "receive"


def test_british_indian_and_latin_spellings_are_silent():
    text = ("Labour centres recognised the offences and emphasised the programmes; "
            "weightage per annum, ex parte orders, lakh and crore, panchayat "
            "counselling, scrutinise, symbolises, industrialisation, timelines.")
    assert find_misspellings([(1, text)]) == []


def test_proper_nouns_one_letter_from_a_word_are_silent():
    text = ("Uttar Pradesh, Satya Nagar, Shree Jagannath, Swamy Vivekananda, "
            "Celsus wrote it, the Nagara style, Kartal and Vagdevi of Paramara Dhar.")
    assert find_misspellings([(1, text)]) == []


def test_a_capitalised_typo_of_a_word_the_notes_use_correctly_is_reported():
    text = ("About Perfomance Grading Index for Districts. The Performance Grading "
            "Index measures performance across states. Gender Prity Index and the "
            "gender parity ratio.")
    found = words(find_misspellings([(1, text)]))
    assert found.get("Perfomance") == "Performance"
    assert found.get("Prity") == "Parity"


def test_a_capitalised_typo_with_a_word_suffix_is_reported_even_if_unique():
    found = words(find_misspellings([(1, "Greater Formlisation: registration eases.")]))
    assert found.get("Formlisation") == "Formalisation"


def test_a_term_used_consistently_three_times_is_a_term_not_a_typo():
    text = "The gariyals live here. Gariyals eat fish. Protect gariyals now."
    assert find_misspellings([(1, text)]) == []


def test_hindi_documents_and_devanagari_neighbours_are_skipped():
    assert find_misspellings([(1, "remves the rule")], language_code="hi") == []
    # A Latin token glued to Devanagari is transliteration, not English.
    assert find_misspellings([(1, "योजनाremves का")]) == []


def test_the_row_quotes_the_page_and_the_sentence():
    found = find_misspellings([(3, "Some text. The Bill remves the rule. More.")])
    assert len(found) == 1
    assert found[0].page == 3
    assert "remves the rule" in found[0].context


def test_a_document_with_a_typo_gets_a_language_row_without_any_ai(tmp_path):
    """End to end: the free checks alone put the typo in the Error Report."""
    import fitz

    from src.qc.engine import run_pdf_qc

    pdf = tmp_path / "typo.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "MSME Bill", fontsize=16)
    page.insert_text((72, 140),
                     "The Bill remves the exsting classification for all MSMEs, "
                     "replacing the existing rule.", fontsize=11)
    doc.save(str(pdf))
    doc.close()

    result = run_pdf_qc(pdf, run_dir=tmp_path / "run", client=None,
                        google_token="", file_name="typo.pdf")
    concerns = " | ".join(r["error_concern"] for r in result.error_rows)
    assert "'remves'" in concerns and "'removes'" in concerns
    assert "'exsting'" in concerns
    row = next(r for r in result.error_rows if "remves" in r["error_concern"])
    assert row["error_type"] == "Language clarity"
    assert "remves" in row["original_statement"]


def test_two_different_typos_in_one_sentence_are_two_rows():
    """The dedup merged these: same quoted sentence, concerns that share every
    word except the one that matters. A reviewer was told about one typo of two."""
    from src.qc.engine import dedupe_rows

    ctx = "The Bill remves the exsting classification for all MSMEs"
    rows = [
        {"original_statement": ctx, "error_type": "Language clarity", "severity": "Low",
         "error_concern": "Spelling: 'remves' should be 'removes'.",
         "correct_information_correction": "", "source_urls": ""},
        {"original_statement": ctx, "error_type": "Language clarity", "severity": "Low",
         "error_concern": "Spelling: 'exsting' should be 'existing'.",
         "correct_information_correction": "", "source_urls": ""},
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_the_same_typo_from_the_ai_and_the_dictionary_is_one_row():
    from src.qc.engine import dedupe_rows

    rows = [
        {"original_statement": "Approach: the PGI places multiple states in a helthy band",
         "error_type": "Language clarity", "severity": "Low",
         "error_concern": "Spelling typo: \"helthy\" instead of \"healthy\". [LANG-SPELLING-001]",
         "correct_information_correction": "healthy", "source_urls": ""},
        {"original_statement": "the PGI places multiple states in a helthy band rather than",
         "error_type": "Language clarity", "severity": "Low",
         "error_concern": "Spelling: 'helthy' should be 'healthy'.",
         "correct_information_correction": "", "source_urls": ""},
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_name_consistency_ignores_ordinary_words_and_acronyms_but_not_names():
    from src.qc.deterministic_checks import _check_name_consistency

    # Sentence-initial ordinary words the document also writes in lowercase.
    text = ("Improved access matters. Access improved after 2020. Improves it. "
            "Goddess Saraswati. The goddess is revered. Giddens wrote it. Giddens again.")
    assert _check_name_consistency(text) == []
    # An acronym beside a place that shares its letters.
    assert _check_name_consistency("MANAS academy. MANAS again. Manas river. Manas park.") == []
    # The case the check exists for still fires.
    issues = _check_name_consistency("Mahatma Gandhi led it. Gandhi again. Later Ghandi wrote.")
    assert len(issues) == 1 and "Ghandi" in issues[0].problem


# ---- what the acceptance rules must NOT swallow (found by adversarial review)

def test_a_typo_is_not_accepted_as_a_british_spelling():
    """`british in w` matched anywhere in the token, so "fourm" passed as a
    British "form" and "trems" as "terms" — the transposition typos this
    module exists to catch were read as correct before any search ran."""
    for text, typo, fix in (
        ("The World Economic Fourm met. The forum agreed.", "Fourm", "Forum"),
        ("The Prime Ministre spoke. The Minister replied.", "Ministre", "Minister"),
        ("In real trems of GDP. The terms of trade.", "trems", "terms"),
        ("The rate of intreest fell. The interest rate.", "intreest", "interest"),
    ):
        found = words(find_misspellings([(1, text)]))
        assert found.get(typo) == fix, f"{typo!r} was accepted as a word"


def test_a_typo_is_not_accepted_as_a_plural_of_a_dictionary_fragment():
    """The word list contains "sta", "spec" and "ara", so "staes", "speces"
    and "araes" were read as plurals of them. "from" is a real word but not a
    countable noun, so "froms" is not its plural either."""
    for text, typo, fix in (
        ("The staes of India have powers. Most states agree.", "staes", "states"),
        ("Endangered speces are listed. The species list.", "speces", "species"),
        ("The froms of government. Three forms exist.", "froms", "forms"),
    ):
        found = words(find_misspellings([(1, text)]))
        assert found.get(typo) == fix, f"{typo!r} was accepted as a plural"


def test_real_plurals_and_british_forms_are_still_silent():
    text = ("The states and forms and areas and ideas are listed. Labour "
            "centres recognised the offences; kilometres of optical fibre, "
            "a meagre budget, programmes of high calibre, per annum.")
    assert find_misspellings([(1, text)]) == []


def test_latin_and_sanskrit_vocabulary_is_silent():
    for text in (
        "The Court issues five writs: habeas corpus, mandamus, prohibition, "
        "certiorari and quo warranto.",
        "There is a prima facie case; the Bench recorded obiter dicta. Mens rea "
        "and actus reus must both be proved; the envoy was declared persona non grata.",
        "The four purusharthas are dharma, artha, kama and moksha.",
        "Gandhian values of satya and ahimsa. Jnana yoga and bhakti yoga; the "
        "pinda daan ritual.",
    ):
        assert find_misspellings([(1, text)]) == [], text[:40]


def test_a_word_beside_an_unknown_word_is_domain_vocabulary():
    """Two unfamiliar words side by side are a term, not a typo — and an
    ordinary word between two typos does not protect either of them."""
    assert find_misspellings([(1, "The doctrine of lis pendens applies here.")]) == []
    found = words(find_misspellings([(1, "The Bill remves the exsting rule. "
                                         "It removes the existing rule.")]))
    assert found == {"remves": "removes", "exsting": "existing"}


def test_a_correction_nobody_writes_is_not_offered():
    """"satya" is one letter from "satay". The floor is waived when the notes
    themselves use the corrected word."""
    assert find_misspellings([(1, "The concept of satya in ethics.")]) == []
    found = words(find_misspellings([(1, "The Gender Prity Index and the gender "
                                         "parity ratio are compared.")]))
    assert found.get("Prity") == "Parity"
