"""Every DMP string, in every language the app can write notes in.

The failure this guards is not a crash. It is a Hindi practice set arriving
inside "Easy / Direct", "Generate Model Answers", "Download Question Paper" —
questions the student can read, wrapped in scaffolding they cannot. Half a
translation looks like a bug in the product, and the half that is missing is
always the half someone needed.

So the tests are about completeness and about the placeholders surviving
translation, which is where hand-written locale tables actually break.
"""
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DMP_LEVELS
from src.dmp.labels import LEVELS, UI, level_label, text, ui
from src.languages import SUPPORTED

#: Placeholders are named, not positional, so a translator can reorder them —
#: "4 of 5 answered" does not survive a literal rendering into Devanagari.
_FIELD_RE = re.compile(r"\{(\w+)\}")


def test_every_supported_language_has_a_table():
    """A language the app will happily write notes in, with no labels, falls
    back to English silently — which is the bug, not the safety net."""
    assert set(UI) == set(SUPPORTED), set(SUPPORTED) - set(UI)
    assert set(LEVELS) == set(SUPPORTED), set(SUPPORTED) - set(LEVELS)


@pytest.mark.parametrize("language", sorted(SUPPORTED))
def test_no_language_is_missing_a_string(language):
    missing = set(UI["en"]) - set(UI[language])
    assert not missing, f"{language} is missing: {sorted(missing)}"


@pytest.mark.parametrize("language", sorted(SUPPORTED))
def test_no_language_has_an_extra_string_nothing_reads(language):
    """An extra key is a translation of something that was renamed or deleted.
    It never appears, and it hides the fact that the real key is untranslated."""
    extra = set(UI[language]) - set(UI["en"])
    assert not extra, f"{language} defines unused: {sorted(extra)}"


@pytest.mark.parametrize("language", sorted(SUPPORTED))
def test_every_level_is_named(language):
    assert set(LEVELS[language]) == set(DMP_LEVELS)
    for level in DMP_LEVELS:
        assert LEVELS[language][level].strip()


@pytest.mark.parametrize("language", sorted(SUPPORTED))
def test_placeholders_survive_translation(language):
    """A dropped {n} loses the count; an invented one raises at render time.
    Both are only visible on the page, in the language nobody on the team reads.
    """
    for key, english in UI["en"].items():
        expected = set(_FIELD_RE.findall(english))
        actual = set(_FIELD_RE.findall(UI[language][key]))
        assert actual == expected, (
            f"{language}.{key}: expected {sorted(expected)}, got {sorted(actual)}")


@pytest.mark.parametrize("language", ("hi", "mr"))
def test_the_devanagari_tables_are_actually_devanagari(language):
    """A table copied from English and never translated passes every other test
    here. This is the one that notices."""
    devanagari = re.compile(r"[ऀ-ॿ]")
    untranslated = [
        key for key, value in UI[language].items()
        if not devanagari.search(value) and value != UI["en"][key]
    ]
    assert not untranslated, untranslated
    # The strings that carry real prose must all be in the script.
    for key in ("panel_title", "intro", "generate_questions", "generate_answers",
                "new_set", "answered_badge", "model_answer", "tick_hint",
                "download_questions", "download_answers"):
        assert devanagari.search(UI[language][key]), f"{language}.{key} is not translated"
    for level in DMP_LEVELS:
        assert devanagari.search(LEVELS[language][level]), f"{language}.{level}"


def test_hindi_and_marathi_are_not_the_same_table():
    """Marathi is not Hindi in Devanagari — the whole reason src/languages.py
    separated the two."""
    differing = [k for k in UI["en"] if UI["hi"][k] != UI["mr"][k]]
    assert differing, "the Marathi table is a copy of the Hindi one"


# ------------------------------------------------------------------ lookups

def test_a_string_is_filled_in_for_the_language_asked_for():
    assert text("generate_answers", "en", n=3) == "Generate Model Answers (3 selected)"
    assert "3" in text("generate_answers", "hi", n=3)
    assert "निवडलेले" in text("generate_answers", "mr", n=3)


def test_the_levels_read_differently_per_language():
    assert level_label("easy", "en") == "Easy / Direct"
    assert level_label("easy", "hi") != level_label("easy", "en")
    assert level_label("easy", "mr") != level_label("easy", "hi")


@pytest.mark.parametrize("unknown", ["ta", "", None, "klingon", "auto"])
def test_an_unknown_language_reads_english_rather_than_failing(unknown):
    assert ui(unknown) is UI["en"]
    assert level_label("easy", unknown) == "Easy / Direct"


def test_a_missing_field_degrades_to_the_template_not_a_crash():
    """A KeyError here surfaces as a Streamlit exception on a rendered page,
    which is worse than a label with a visible {placeholder} in it."""
    assert text("generate_answers", "en") == UI["en"]["generate_answers"]


def test_an_unknown_key_returns_something_printable():
    assert text("no_such_key_anywhere", "en") == "no_such_key_anywhere"
