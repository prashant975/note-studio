"""Regressions from the two-document round of 2026-08-25.

Every test here reproduces something measured on a real run — a row that was
lost, a row printed twice, a page window thrown away — with the exact shapes
that occurred, so it cannot come back quietly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.engine import _block_without_markers, dedupe_rows  # noqa: E402


def row(stmt, concern, fix="", error_type="Factual", severity="High"):
    return {"original_statement": stmt, "error_type": error_type,
            "severity": severity, "error_concern": concern,
            "correct_information_correction": fix, "source_urls": ""}


# ------------------------------------------------ a report without markers

def test_a_fenced_array_without_markers_is_still_a_report():
    text = ('```json\n[\n  {"original_statement": "x", "error_type": "Factual", '
            '"severity": "High", "error_concern": "y", '
            '"correct_information_correction": "z", "source_urls": []}\n]\n```')
    block, found = _block_without_markers(text)
    assert found
    assert json.loads(block)[0]["original_statement"] == "x"


def test_a_bare_empty_array_is_an_explicit_empty_report():
    assert _block_without_markers("[]") == ("[]", True)


def test_prose_and_stray_json_are_not_reports():
    assert _block_without_markers("I could not find any errors in this part.") == ("", False)
    assert _block_without_markers('{"summary": "fine"}') == ("", False)
    assert _block_without_markers('["a", "b"]') == ("", False)


def test_a_cut_off_marker_less_array_keeps_its_complete_rows():
    text = ('[{"original_statement": "a", "error_concern": "b"}, '
            '{"original_statement": "c", "error_conc')
    block, found = _block_without_markers(text)
    assert found and '"a"' in block


def test_a_part_that_answers_with_prose_is_retried_once(tmp_path):
    from src.qc import engine

    class _P:
        def __init__(self, n):
            self.page_no, self.text, self.image_path, self.spans = n, "w " * 40, None, []

    class _Doc:
        pages = [_P(1), _P(2)]
        language_code = "en"

        @property
        def paged_text(self):
            return "\n\n".join(f"[Page {p.page_no}]\n{p.text}" for p in self.pages)

    calls = []

    class _Client:
        def generate(self, prompt, images=None, **kw):
            calls.append(prompt)

            class R:
                truncated = False
                # First answer to any prompt: prose. Second: a real report.
                text = ("Sorry, here is my analysis in words."
                        if calls.count(prompt) == 1
                        else "# ERROR_REPORT_START\n[]\n# ERROR_REPORT_END")
            return R()

    doc = _Doc()
    report = engine._run_error_report_pass(
        _Client(), doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")
    assert report.complete, "one retry must rescue a part that answered with prose"
    assert report.unmarked_parts == []
    # Every part was asked twice: once for the prose, once for the report.
    assert len(calls) == 2 * len(set(calls))


# ---------------------------------------------------------------- dedup

def test_the_same_quote_with_paraphrased_concerns_is_one_row():
    """Measured: both passes quoted the identical Tibet sentence, one said
    'has no coastline, making it geographically impossible', the other
    'a typhoon cannot make landfall there', one had no correction — two rows."""
    stmt = "Typhoon Dolphin made landfall near Tibet Province of eastern China."
    rows = [
        row(stmt, "Tibet is a landlocked, mountainous region in southwestern China "
                  "and has no coastline, making it geographically impossible for a "
                  "typhoon to make landfall there.",
            fix="Typhoons make landfall along the coastal provinces of eastern China."),
        row(stmt, "Tibet is a landlocked, mountainous autonomous region in "
                  "western/south-western China. A typhoon cannot make landfall in Tibet."),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_two_faults_in_one_sentence_are_still_two_rows():
    stmt = "Constructed during the 12th century CE under the Angkor Dynasty."
    rows = [
        row(stmt, "Prambanan was built in the 9th century, not the 12th.",
            fix="9th century"),
        row(stmt, "Prambanan was built by the Sanjaya dynasty, not the Angkor dynasty.",
            fix="Sanjaya Dynasty"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_a_typo_reported_by_the_ai_and_by_the_dictionary_is_one_row():
    rows = [
        row("replacing the exsting provision requiring",
            "The word 'exsting' is a typographical error for 'existing'.",
            fix="replacing the existing provision requiring",
            error_type="Language clarity", severity="Low"),
        row("a digital platform will be voluntary for all MSMEs, replacing the "
            "exsting provision requiring certain enterprises",
            "Spelling: 'exsting' should be 'existing'.",
            error_type="Language clarity", severity="Low"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_one_contradiction_quoted_from_both_ends_is_one_row():
    rows = [
        row("The Vagdevi idol is an 11th-century red-marble sculpture from the "
            "Paramara period in Dhar, Madhya Pradesh.",
            "The text describes the idol as a 'red-marble sculpture' here, but "
            "later describes it as 'carved from white marble'. Both cannot be true.",
            fix="grey sandstone"),
        row("Material & Size: The sculpture is carved from white marble, measures "
            "about 1.285 metres and weighs approximately 500 kg.",
            "This contradicts the earlier description of 'red-marble'.",
            fix="grey sandstone"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_two_different_sentences_with_unrelated_quotes_stay_apart():
    rows = [
        row("Chandigarh emerged as the best-performing UT with 'Uttam-3'.",
            "There is no 'Uttam-3' grade in PGI 2.0.", fix="Daksh"),
        row("No district attained the highest grade 'Utkarsh'.",
            "The highest grade in PGI-D is 'Daksh', not 'Utkarsh'.", fix="Daksh"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2


# ------------------------------------------------------------- verifier

def _verify(rows, reply):
    from src.qc.verify import run_verification_pass

    class _C:
        def generate(self, prompt, images=None, **kw):
            class R:
                text = json.dumps(reply)
                truncated = False
            return R()

    return run_verification_pass(_C(), rows)


def test_a_real_verdict_resting_on_memory_is_kept_as_a_low_lead():
    rows = [row("only 14.8% of men are in regular salaried employment",
                "PLFS 2023-24 gives 21.5% for males.", severity="High")]
    result = _verify(rows, [{"id": 1, "verdict": "REAL", "basis": "memory",
                             "why": "I recall the PLFS figure."}])
    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
    assert "memory" in result.rows[0]["error_concern"]
    assert result.memory_only == 1
    assert any("memory" in w for w in result.warnings())


def test_a_real_verdict_on_a_settled_basis_is_untouched():
    rows = [row("Prambanan is in Malaysia", "It is in Indonesia.", severity="High")]
    result = _verify(rows, [{"id": 1, "verdict": "REAL", "basis": "settled",
                             "why": "Geography."}])
    assert result.rows[0]["severity"] == "High"
    assert result.memory_only == 0


def test_a_reply_without_a_basis_behaves_as_before():
    rows = [row("x", "y", severity="High")]
    result = _verify(rows, [{"id": 1, "verdict": "REAL", "why": "sure"}])
    assert result.rows[0]["severity"] == "High"


def test_the_verifier_prompt_ranks_contradiction_above_status_and_names_two_names():
    from src.qc import verify

    assert "INTERNAL CONTRADICTION OUTRANKS THE STATUS RULE" in verify._PROMPT
    assert "TWO NAMES FOR ONE THING" in verify._PROMPT
    assert '"basis"' in verify._PROMPT


# -------------------------------------------------------------- spelling

def test_a_name_that_becomes_a_word_with_a_letter_in_front_is_not_a_typo():
    from src.qc.spelling_check import find_misspellings

    text = ("Start-ups such as Razorpay, Zepto, Ather and Ola grew. Sushruta is the "
            "Father of Plastic Surgery; the father figure.")
    assert find_misspellings([(1, text)]) == []


def test_two_monuments_with_lookalike_unesco_rows_stay_apart():
    """Measured: at a 0.6 sentence gate these two seeded errors became one row."""
    rows = [
        row("UNESCO Status: Inscribed as a UNESCO World Heritage Site in 1998 for its value.",
            "Prambanan was inscribed as a UNESCO World Heritage Site in 1991, not 1998.",
            fix="Inscribed as a UNESCO World Heritage Site in 1991"),
        row("UNESCO Recognition: Declared a UNESCO World Heritage Site (1999)",
            "The Taj Mahal was declared a UNESCO World Heritage Site in 1983, not 1999.",
            fix="Declared a UNESCO World Heritage Site (1983)"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_a_grammar_row_is_not_swallowed_by_a_conceptual_row_on_a_nearby_sentence():
    """Measured: the tense complaint quotes 'launched' as its correction; that
    word sits in the other row's sentence, and the two merged — losing the
    seeded tense error."""
    rows = [
        row("Recently, the Union Minister of Cooperation launched 'Bank in a Box', a "
            "Security Operations Centre for Urban Cooperative Banks.",
            "'Bank in a Box' and the 'Security Operations Centre' (SOC) are two distinct "
            "services offered by the NUCFDC.", fix="two distinct services"),
        row("Bank in a Box was an integrated technology platform launches to help small "
            "Urban Cooperative Banks.",
            "The sentence incorrectly uses the past tense 'was' for an active platform, "
            "and 'launches' should be 'launched'.",
            fix="Bank in a Box is an integrated technology platform launched to help",
            error_type="Language clarity", severity="Low"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_a_rule_tag_and_an_incidental_figure_are_not_anchors():
    rows = [
        row("The Gender Prity Index (GPI) is calculated as the ratio of female GER",
            'Spelling typo: "Prity" instead of "Parity". [LANG-SPELLING-001] (under: Survey 2021-22)',
            fix="Parity", error_type="Language clarity", severity="Low"),
        row("Female enrolment increased from 1.57 crore. The Gender Prity Index (GPI) is",
            "Spelling: 'Prity' should be 'Parity'.",
            error_type="Language clarity", severity="Low"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


# ------------------------------ what the adversarial review found (round 4b)

def test_a_marker_less_report_wrapped_in_prose_is_returned_as_clean_json():
    """_strip_fences trims from the first '[' to the last ']', so a bracket in
    the surrounding prose used to be carried into the block. The caller then
    failed to parse it, salvaged the rows, and "salvage was needed" is read as
    "the reply was cut off" — a complete report reported as a PARTIAL SCAN."""
    r = ('{"original_statement": "a", "error_type": "Factual", "severity": "High", '
         '"error_concern": "b", "correct_information_correction": "c", "source_urls": []}')
    for text in (f'Error report [pages 3-4]:\n```json\n[{r}]\n```',
                 f'```json\n[{r}]\n```\n\nNote: I flagged [1] item.'):
        block, found = _block_without_markers(text)
        assert found
        parsed = json.loads(block)          # must parse first time, no salvage
        assert parsed[0]["original_statement"] == "a"


def test_a_part_cut_off_on_both_attempts_is_reported_as_truncated(tmp_path):
    """The exhausted-retry path hard-coded was_cut=False, so the user was told
    the part "came back without an error-report block" and never saw the one
    actionable remedy — raise the output limit."""
    from src.qc import engine

    class _P:
        def __init__(self, n):
            self.page_no, self.text, self.image_path, self.spans = n, "w " * 40, None, []

    class _Doc:
        pages = [_P(1)]
        language_code = "en"

        @property
        def paged_text(self):
            return "[Page 1]\nw w w"

    class _Client:
        def generate(self, prompt, images=None, **kw):
            class R:
                truncated = True
                text = '```json\n[{"original_statement": "long long'
            return R()

    doc = _Doc()
    report = engine._run_error_report_pass(
        _Client(), doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")
    assert report.truncated_parts, "a cut-off reply must be reported as truncated"
    assert any("output limit" in w for w in report.coverage_warnings())


def test_the_reply_that_was_used_is_the_numbered_response_file(tmp_path):
    """README points a reviewer at error_report_response_NN.txt. After a retry
    that file held the discarded answer, or did not exist at all."""
    from src.qc import engine

    class _P:
        def __init__(self, n):
            self.page_no, self.text, self.image_path, self.spans = n, "w " * 40, None, []

    class _Doc:
        pages = [_P(1)]
        language_code = "en"

        @property
        def paged_text(self):
            return "[Page 1]\nw w w"

    # Counted PER PROMPT, not globally: the passes run concurrently, so a
    # shared counter makes which attempt gets the prose a race.
    import threading

    seen: dict[str, int] = {}
    lock = threading.Lock()

    class _Client:
        def generate(self, prompt, images=None, **kw):
            with lock:
                seen[prompt] = seen.get(prompt, 0) + 1
                attempt = seen[prompt]

            class R:
                truncated = False
                text = ("Sorry, prose." if attempt == 1
                        else "# ERROR_REPORT_START\n[]\n# ERROR_REPORT_END")
            return R()

    doc = _Doc()
    engine._run_error_report_pass(
        _Client(), doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")
    used = (tmp_path / "error_report_response_01.txt").read_text(encoding="utf-8")
    rejected = (tmp_path / "error_report_response_01_rejected.txt").read_text(encoding="utf-8")
    assert "ERROR_REPORT_START" in used
    assert "Sorry, prose." in rejected


def test_a_repeated_verdict_with_two_bases_keeps_the_cautious_one():
    """Same verdict twice with different bases: last-write-wins let the ORDER
    of the reply decide whether a row is a High finding or a Low lead."""
    rows = [row("x", "y", severity="High")]
    for order in ([{"id": 1, "verdict": "REAL", "basis": "settled", "why": "a"},
                   {"id": 1, "verdict": "REAL", "basis": "memory", "why": "b"}],
                  [{"id": 1, "verdict": "REAL", "basis": "memory", "why": "b"},
                   {"id": 1, "verdict": "REAL", "basis": "settled", "why": "a"}]):
        result = _verify(rows, order)
        assert result.rows[0]["severity"] == "Low", order
        assert result.memory_only == 1


def test_the_name_check_still_sees_names_late_in_the_alphabet(tmp_path):
    """The 400-form cap took forms alphabetically, so on a real document every
    name after roughly 'Pl' was never compared."""
    from src.qc.deterministic_checks import _check_name_consistency

    filler = " ".join(f"Aaaa{i:04d} Aaaa{i:04d}" for i in range(450))
    text = (filler + " The Prambanan complex is famous. Prambanan again. "
            "The Prambanam temple was built later.")
    issues = _check_name_consistency(text)
    assert any("Prambanam" in i.problem for i in issues), (
        "a name late in the alphabet must still be compared")


# --------------------------------------------------------------------------
# The dedup specification: every pair measured from a real run, with the
# verdict each one must get. _same_finding was written FROM this table, so it
# is the closest thing the function has to a contract.
# --------------------------------------------------------------------------

DEDUP_CASES = [
    (False, "two faults in one sentence, both complaints naming the subject",
     row("Kanishka of the Gupta dynasty ascended the throne in 78 BCE.",
         "'Kanishka' belonged to the Kushana dynasty, not the Gupta dynasty.", "Kushana dynasty"),
     row("Kanishka of the Gupta dynasty ascended the throne in 78 BCE.",
         "'Kanishka' ascended the throne in 78 CE, not 78 BCE.", "78 CE")),
    (False, "two typos on different pages, each correction a word in the other's sentence",
     row("a digital platform will be voluntary for all MSMEs, replacing the exsting provision requiring certain enterprises",
         "Spelling: 'exsting' should be 'existing'.", "", "Language clarity", "Low"),
     row("the existing provison for MSMEs under the Act will lapse once the platform is live",
         "Spelling: 'provison' should be 'provision'.", "", "Language clarity", "Low")),
    (False, "two faults in one sentence, one row carrying no correction",
     row("Borobudur was built in the 9th century by the Sanjaya dynasty.",
         "Wrong dynasty: Borobudur is a Shailendra-era monument, built c. 800 CE.", "Shailendra dynasty"),
     row("Borobudur was built in the 9th century by the Sanjaya dynasty.",
         "Wrong century: Borobudur construction began in the 8th century.", "")),
    (True, "one complaint, two corrections worded differently",
     row("It is the largest Hindu temple complex in Malaysia.",
         "Prambanan is in Indonesia, not Malaysia.", "Indonesia"),
     row("It is the largest Hindu temple complex in Malaysia.",
         "Prambanan is in Indonesia, not Malaysia.", "Central Java, near Yogyakarta")),
    (True, "one fault, complaint paraphrased, one row carrying no correction",
     row("Typhoon Dolphin made landfall near Tibet Province of eastern China.",
         "Tibet is a landlocked, mountainous region in southwestern China and has no coastline, "
         "making it geographically impossible for a typhoon to make landfall there.",
         "Typhoons make landfall along the coastal provinces of eastern China."),
     row("Typhoon Dolphin made landfall near Tibet Province of eastern China.",
         "Tibet is a landlocked, mountainous autonomous region in western/south-western China. "
         "A typhoon cannot make landfall in Tibet.", "")),
    (True, "one typo, quoted by the AI and by the dictionary from different spans",
     row("The Gender Prity Index (GPI) is calculated as the ratio of female GER",
         'Spelling typo: "Prity" instead of "Parity". [LANG-SPELLING-001]', "Parity",
         "Language clarity", "Low"),
     row("Female enrolment increased from 1.57 crore. The Gender Prity Index (GPI) is",
         "Spelling: 'Prity' should be 'Parity'.", "", "Language clarity", "Low")),
    (False, "an ambiguous pair — the safe direction is to keep both",
     row("The Simon Commission arrived in India in 1927 and was boycotted by all parties.",
         "The commission arrived in February 1928, not 1927.", "1928"),
     row("The Simon Commission arrived in India and was boycotted by all parties.",
         "The commission (appointed under the Act of 1919) reached Bombay only in February of the following year.",
         "February of the following year")),
]


def test_the_dedup_specification():
    from src.qc.engine import _same_finding

    wrong = [name for want, name, a, b in DEDUP_CASES
             if _same_finding(a, b) is not want]
    assert not wrong, f"dedup verdict wrong for: {wrong}"


def test_two_rows_that_agree_merge_even_when_they_quote_different_words():
    """Measured: two rows calling "MSMEs Enterprises" redundant quoted
    different incidental words ('E' and 'Enterprises' against 'MSMEs'), so the
    quoted-word veto kept both and the report printed the point twice. The
    veto is right for two typos, whose complaints agree only 0.60; these agree
    0.75 and are one finding."""
    rows = [
        row("About MSMEs Enterprises:",
            "The term 'MSMEs Enterprises' is redundant because the 'E' in MSMEs "
            "already stands for 'Enterprises'.", "About MSMEs:",
            "Terminology issue", "Low"),
        row("About MSMEs Enterprises:",
            "The phrase 'MSMEs Enterprises' is redundant because 'MSMEs' already "
            "stands for 'Micro, Small and Medium Enterprises'.", "About MSMEs:",
            "Language clarity", "Low"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_a_status_claim_is_kept_as_a_lead_not_deleted():
    """Measured: a row saying heatwaves "are not notified under the 2012-17
    guidelines" ALSO carried the seeded anachronism about those guidelines.
    Deleting it as a status claim lost the seeded error; the verifier cannot
    show such a claim is wrong either, so it is a lead."""
    from src.qc import verify

    assert "Verdict\n  UNVERIFIABLE" in verify._PROMPT or "UNVERIFIABLE, so it reaches" in verify._PROMPT
    assert "Not\n  NOT_AN_ERROR" in verify._PROMPT or "Not NOT_AN_ERROR" in verify._PROMPT
    # An UNVERIFIABLE verdict keeps the row, demoted and labelled.
    rows = [row("The Centre has included heatwaves as notified calamities under "
                "the 2012-2017 guidelines.",
                "Those guidelines did not include heatwaves.", severity="High")]
    result = _verify(rows, [{"id": 1, "verdict": "UNVERIFIABLE", "basis": "memory",
                             "why": "cannot settle"}])
    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
    assert result.dropped == []


def test_the_recency_guard_asks_whether_the_model_can_name_the_right_value():
    """Measured with a direct question: the model KNOWS six of the eight
    errors it kept missing (Melbourne not Perth, 1997-2012 not 1996-2013,
    digital not travel, Hong Kong not Thailand, thinning not thickening, G-7
    wrong). The guard was muting knowledge it has, because "do I recognise
    this?" is the wrong test. The right one is "can I say what is correct?"."""
    from src.qc_prompts import build_pdf_error_report_prompt

    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.")
    assert "THE TEST IS NOT WHETHER YOU RECOGNISE" in prompt
    assert "CAN YOU NAME THE CORRECT VALUE?" in prompt
    assert "IS ALL YOU HAVE A DOUBT?" in prompt
    # The classes that genuinely move after a cutoff stay silent.
    for probe in ("current holder of an office", "EXISTS YET"):
        assert probe in prompt, probe
    # But existence is ALL that rule covers: a body's NAME is checkable, and a
    # wrong one is among the commonest errors in notes like these.
    assert "you are being asked what it" in prompt
    # And the closing line no longer reads as "prefer silence to reporting".
    assert "Prefer silence to SPECULATION — not to knowledge." in prompt


def test_a_lead_says_so_at_the_start_of_the_cell():
    """The caveat used to be appended after two sentences of prose, where a
    45-character Excel column hides it: leads and findings looked identical."""
    rows = [row("x", "This figure is wrong and here is a long explanation of why "
                     "it matters to a student reading these notes.", severity="High")]
    for basis, marker in (("memory", "memory"), ("", "not independently verified")):
        reply = [{"id": 1, "verdict": "REAL" if basis else "UNVERIFIABLE",
                  "basis": basis, "why": "w"}]
        result = _verify(rows, reply)
        concern = result.rows[0]["error_concern"]
        assert concern.startswith("[LEAD"), concern[:60]
        assert marker in concern.lower()
        assert result.rows[0]["severity"] == "Low"


def test_one_finding_quoted_from_two_spans_collapses():
    """The error-report pass and the review quote different sentences for the
    same fault. What identifies it is the complaint plus the years it cites."""
    rows = [
        row("Indra Sawhney Case (1990): Introduced the Creamy Layer doctrine",
            "The landmark Indra Sawhney v. Union of India judgment was delivered "
            "in 1992, not 1990.", "1992"),
        row("The Indra Sawhney (1990) judgment held that the creamy layer applies",
            "The Indra Sawhney judgment was delivered in 1992, not 1990.", "1992"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 1


def test_lookalike_wording_with_different_years_still_stays_apart():
    """The same rule must not merge Prambanan's UNESCO year with the Taj
    Mahal's: near-identical wording, different years, different monuments."""
    rows = [
        row("UNESCO Status: Inscribed as a UNESCO World Heritage Site in 1998.",
            "Prambanan was inscribed as a UNESCO World Heritage Site in 1991, not 1998.",
            "1991"),
        row("UNESCO Recognition: Declared a UNESCO World Heritage Site (1999)",
            "The Taj Mahal was declared a UNESCO World Heritage Site in 1983, not 1999.",
            "1983"),
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_two_faults_sharing_a_year_are_not_collapsed_by_the_year_alone():
    """There used to be a second dedup pass ahead of the measured one. It
    grouped rows by quoted statement, called two of them one mistake whenever
    their concerns shared any number, and kept the more severe — ties going to
    whichever arrived first. Measured: the wrong ministry was collapsed into a
    row about the target year because both said 2047, and left the report."""
    stmt = ("Context: Recently, the Ministry of Finance has released the report "
            "\"Reimagining Care: Strategies for Empowering Caregivers in Viksit "
            "Bharat@2057\".")
    rows = [
        row(stmt, "The target year for the 'Viksit Bharat' initiative is 2047 "
                  "(marking 100 years of independence), not 2057.",
            "Viksit Bharat@2047"),
        row(stmt, "The report was released by NITI Aayog, not the Ministry of "
                  "Finance, and the target year for Viksit Bharat is 2047.",
            "Recently, NITI Aayog has released the report ... Viksit Bharat@2047"),
    ]
    kept, _removed = dedupe_rows(rows)
    assert any("NITI" in r["error_concern"] for r in kept), (
        "the row naming the wrong ministry must survive")


def test_the_entity_lens_forces_enumeration_before_judgement():
    """Measured: pages holding a wrong city, a wrong weight and a wrong number
    of arms produced six confident findings, none of them those three. The
    model had read the pages; it never asked about those items. So the lens now
    tells it to enumerate every checkable item first and judge the list."""
    from src.qc_prompts import build_pdf_error_report_prompt

    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.", lens="entities")
    assert "WORK BY ENUMERATION, NOT BY IMPRESSION" in prompt
    assert "ITEM BY ITEM" in prompt
    for probe in ("every number, year, date, percentage",
                  "every acronym and its expansion"):
        assert probe in prompt, probe
    # The open pass is untouched — enumeration is the second lens's job.
    assert "WORK BY ENUMERATION" not in build_pdf_error_report_prompt("[Page 1]\nx.")


def test_the_row_that_quotes_the_wrong_figure_survives_the_merge():
    """Measured: one row said "women spend about 8.4 times (not 70 times) more
    time"; another gave only the correct figure. They merged, and the survivor
    no longer quoted the notes' own wrong number — which is the thing a
    reviewer searches the page for."""
    stmt = "Women in India spend 70x more time than men on unpaid domestic work."
    rows = [
        row(stmt, "According to the National Time Use Survey (2019) published by "
                  "MoSPI, women spend far more time on unpaid work than men.",
            "about 8.4 times"),
        row(stmt, "According to the Time Use Survey 2019, women spend about 8.4 "
                  "times (not 70 times) more time than men on unpaid work.",
            "8.4 times"),
    ]
    kept, _removed = dedupe_rows(rows)
    assert len(kept) == 1
    assert "70" in kept[0]["error_concern"], (
        "the surviving row must still quote the wrong figure")


# ------------- what 33 independent judges found in the unmatched rows

def test_the_verifier_is_given_the_page_the_claim_is_about():
    """A panel judging every unmatched row found that five of nine false
    positives were refuted by the page itself — it printed the grading ladder,
    labelled the table "As of August 2026", named the law it repealed — and the
    verifier was structurally unable to see any of it."""
    from src.qc.verify import _claim_text, _page_context

    notes = ("[Page 16] Performance Grading Index 2.0. The PGI 2.0 grading framework "
             "comprises Utkarsh (above 90%), Uttam-1 (81-90%), Uttam-2 (71-80%), "
             "Uttam-3 (61-70%), Prachesta-1 and Akanshi-3. Chandigarh emerged as the "
             "best-performing State/Union Territory with the Uttam-3 grade in PGI 2.0.")
    r = row("Chandigarh emerged as the best-performing State/Union Territory with the "
            "Uttam-3 grade in PGI 2.0.",
            "The highest grade in the PGI framework is 'Daksh', not 'Utkarsh'.", "Daksh")
    text = _claim_text(1, r, notes)
    assert "THE PAGE AROUND IT" in text
    assert "Uttam-1 (81-90%)" in text, "the ladder that settles it must travel"
    # A statement that is not in the notes simply carries no context.
    assert _page_context("Something never written on any page here at all", notes) == ""
    assert "THE PAGE AROUND IT" not in _claim_text(1, r, "")


def test_the_verifier_prompt_ranks_the_page_above_recollection():
    from src.qc import verify

    for probe in ("READ THE PAGE BEFORE YOU REASON",
                  "VERSIONED THINGS ARE NOT SETTLED THINGS",
                  "SCOPE BEFORE FIGURES"):
        assert probe in verify._PROMPT, probe


def test_a_claim_whose_premise_is_the_wrong_year_is_settled_in_code():
    """Measured: a row argued the UDISE+ 2025-26 report "cannot exist yet as the
    current year is 2024" — on a run whose prompt had been told today was
    26 August 2026. The model reasons from the year it feels, so asking it
    again only repeats the mistake."""
    from src.qc.verify import _false_year_premise, run_verification_pass

    assert _false_year_premise("the current year is 2024", 2026) == 2024
    assert _false_year_premise("It is still 2024, so this cannot be right.", 2026) == 2024
    assert _false_year_premise("The report covers 2024 and 2025 figures.", 2026) is None
    assert _false_year_premise("the current year is 2026", 2026) is None

    class _C:
        def generate(self, prompt, images=None, **kw):
            raise AssertionError("a false-premise row must never reach the model")

    rows = [row("UDISE+ Report 2025-26", "This cannot exist yet as the current "
                                         "year is 2024.", severity="High")]
    result = run_verification_pass(_C(), rows, today="26 August 2026")
    assert result.rows == []
    assert len(result.dropped) == 1
    assert "2024" in result.dropped[0]["_verification_why"]


# ------------- a row must quote something a reviewer can find on the page

NOTES = ("[Page 3] 3.1 Ladakh's Glaciers Are Slowing as the Himalaya Warms. Context: "
         "A recent study published in The Cryosphere has found that glaciers in the "
         "region are slowing. The study examined 12 glaciers between 1982 and 2023. "
         "[Page 8] 4.1 Tribunal Reforms Bill, 2026. Context: Recently, Parliament "
         "passed the Tribunal Reforms Bill, 2026, which seeks to reform the "
         "administration of tribunals and establish the National Tribunals Authority "
         "as a central institutional mechanism. Greater Formlisation: Digital "
         "registration, replacing the exsting provision requiring certain enterprises.")


def test_a_row_quoting_a_sentence_that_is_not_there_is_removed():
    """Measured on a real run: the independent review produced two rows out of
    thirty-five quoting sentences the document does not contain. IISc, Mumbai
    and Bengaluru appear nowhere in it, and the notes say "which SEEKS to
    reform", not "The Bill seek". A reviewer looking for either sentence finds
    nothing and stops trusting the whole report."""
    from src.qc.engine import _drop_invented_quotes

    rows = [
        row("The study examined 12 glaciers between 1982 and 2023.",
            "The study began in 1992, not 1982.", "1992"),
        row("The study was conducted by the Indian Institute of Science (IISc) in Mumbai.",
            "IISc is located in Bengaluru, not Mumbai.", "Bengaluru"),
        row("The Bill seek to streamline the functioning of tribunals",
            "Subject-verb agreement: 'Bill seek' should be 'Bill seeks'.", "seeks"),
    ]
    kept, dropped = _drop_invented_quotes(rows, NOTES)
    assert len(kept) == 1 and len(dropped) == 2
    assert "12 glaciers" in kept[0]["original_statement"]
    assert all("IISc" in d["original_statement"] or "Bill seek" in d["original_statement"]
               for d in dropped)


def test_short_real_quotes_and_placeholder_statements_survive():
    """The margin measured on that run: real short quotes matched 4 of 4 and
    5 of 5 words, the invented ones 2 of 13 and 2 of 9."""
    from src.qc.engine import _drop_invented_quotes

    rows = [
        row("Greater Formlisation: Digital registration", "Spelling.", "Formalisation"),
        row("replacing the exsting provision requiring", "Spelling.", "existing"),
        row("(see the notes)", "A structural finding with no quote.", ""),
        row("Page 8", "A location, not a quotation.", ""),
    ]
    kept, dropped = _drop_invented_quotes(rows, NOTES)
    assert dropped == [], f"dropped a real row: {dropped}"
    assert len(kept) == 4


def test_the_check_stands_down_when_there_is_no_readable_text():
    """A scrambled text layer is not evidence that a quote was invented."""
    from src.qc.engine import _drop_invented_quotes

    rows = [row("Any statement at all here", "something", "x")]
    kept, dropped = _drop_invented_quotes(rows, "")
    assert dropped == [] and len(kept) == 1
