"""English left inside Hindi notes is sent back once and spliced in only when safe."""
from __future__ import annotations

import re

import pytest

from src.script_repair import build_script_repair_prompt, repair_latin_in_devanagari
from src.script_validator import find_latin_in_hindi_body

NOTES = "\n".join([
    "#LECTURE_TITLE: L01 संयुक्त राष्ट्र",
    "#H1: संयुक्त राष्ट्र की व्यवस्था",
    "- **संयुक्त राष्ट्र (United Nations)** की स्थापना 1945 में हुई।",         # allowed gloss
    "- इनमें counter-terrorism और HADR (Humanitarian Assistance and Disaster Relief) शामिल हैं।",
    "- इसके छह प्रमुख अंग हैं — General Assembly, Security Council और Secretariat।",
    "  - K-15 को Sagarika नाम से 750 किमी तक जाना जाता है।",
    "- [पेरिस समझौता](https://unfccc.int/paris) 2015 में हुआ।",
])


class FakeClient:
    def __init__(self, answer: str):
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, prompt, images, **kwargs):
        self.prompts.append(prompt)

        class Response:
            text = self.answer
            model = "fake"
        return Response()


def _flagged_numbers(text: str) -> list[int]:
    return sorted({r.line_no for r in find_latin_in_hindi_body(text)})


def test_a_hindi_gloss_in_brackets_is_not_flagged():
    assert 3 not in _flagged_numbers(NOTES)
    # Line 6 for "K-15", which the prompt wants as के-15 (reviewers marked it too).
    assert _flagged_numbers(NOTES) == [4, 5, 6]


def test_only_the_flagged_lines_are_sent():
    client = FakeClient("")
    repair_latin_in_devanagari(NOTES, client, "hi")
    prompt = client.prompts[0]
    assert "<<4>>" in prompt and "<<5>>" in prompt
    assert "<<3>>" not in prompt and "<<1>>" not in prompt
    assert "Hindi" in prompt


def test_good_answers_are_spliced_in_and_bad_ones_refused():
    client = FakeClient("\n".join([
        "<<4>> - इनमें आतंकवाद-रोधी अभियान और मानवीय सहायता एवं आपदा राहत (HADR) शामिल हैं।",
        "<<5>> इसके छह प्रमुख अंग हैं — महासभा, सुरक्षा परिषद और सचिवालय।",   # bullet dropped
    ]))
    result = repair_latin_in_devanagari(NOTES, client, "hi")
    lines = result.text.split("\n")
    assert lines[3] == "- इनमें आतंकवाद-रोधी अभियान और मानवीय सहायता एवं आपदा राहत (HADR) शामिल हैं।"
    assert lines[4] == "- इसके छह प्रमुख अंग हैं — महासभा, सुरक्षा परिषद और सचिवालय।"  # bullet restored
    assert result.repaired_lines == 2 and result.flagged_lines == 3
    assert result.rejected == ["line 6: no answer"]
    assert lines[5] == NOTES.split("\n")[5]             # unanswered: kept as written
    assert lines[2] == NOTES.split("\n")[2]


def test_an_answer_that_changes_a_number_is_kept_as_written():
    notes = "#H1: x\n- इसकी स्थापना 1945 में General Assembly ने की।"
    client = FakeClient("<<2>> - इसकी स्थापना 1946 में महासभा ने की।")
    result = repair_latin_in_devanagari(notes, client, "hi")
    assert result.text == notes
    assert result.repaired_lines == 0 and "number" in result.rejected[0]


@pytest.mark.parametrize("original, rewritten", [
    ("1945", "1945 और 1946"),
    ("1945", "1945 और 1945"),
    ("1945 और 1950", "1950 और 1945"),
    ("10", "-10"),
    ("-10", "10"),
    ("10", "−10"),
])
def test_a_repair_cannot_add_repeat_reorder_or_change_signs(original, rewritten):
    notes = f"#H1: x\n- General Assembly के आँकड़े {original} हैं।"
    client = FakeClient(f"<<2>> - महासभा के आँकड़े {rewritten} हैं।")
    result = repair_latin_in_devanagari(notes, client, "hi")
    assert result.text == notes
    assert result.repaired_lines == 0
    assert "number" in result.rejected[0]


def test_a_repair_accepts_thousands_separators_without_changing_the_number():
    notes = "#H1: x\n- General Assembly का आँकड़ा 1,945 है।"
    result = repair_latin_in_devanagari(
        notes, FakeClient("<<2>> - महासभा का आँकड़ा 1945 है।"), "hi")
    assert result.repaired_lines == 1


def test_a_repair_cannot_add_a_new_source_link():
    notes = "#H1: x\n- General Assembly की जानकारी [स्रोत](https://un.org/a) में है।"
    result = repair_latin_in_devanagari(notes, FakeClient(
        "<<2>> - महासभा की जानकारी [स्रोत](https://un.org/a) और "
        "[स्रोत](https://example.com/b) में है।"), "hi")
    assert result.text == notes
    assert "link" in result.rejected[0]


def test_an_answer_with_as_much_english_is_refused():
    notes = "#H1: x\n- यह cost-effectiveness वाला पक्ष है।"
    client = FakeClient("<<2>> - यह cost effectiveness वाला पक्ष है।")
    result = repair_latin_in_devanagari(notes, client, "hi")
    assert result.text == notes


def test_a_failed_call_never_breaks_the_run():
    class Broken:
        def generate(self, *a, **k):
            raise RuntimeError("proxy down")

    result = repair_latin_in_devanagari(NOTES, Broken(), "hi")
    assert result.text == NOTES and "proxy down" in result.error


def test_english_notes_and_clean_notes_make_no_call():
    client = FakeClient("")
    repair_latin_in_devanagari(NOTES, client, "en")
    repair_latin_in_devanagari("#H1: x\n- संसद सर्वोच्च है।", client, "hi")
    assert client.prompts == []


def test_marathi_gets_marathi_examples():
    prompt = build_script_repair_prompt({2: "- याचे General Assembly आहे."}, "mr")
    assert "Marathi" in prompt and "दहशतवादविरोधी" in prompt
    assert re.search(r"<<2>> - याचे", prompt)
