"""Findings must not be lost between the model and the sheet.

Every test here is a silent failure — output that looked complete and was not.
That is the shape worth testing hardest, because nothing about the artefact
invites anyone to check it: a sheet listing three errors from the first third of
a lecture looks exactly like a sheet listing all the errors in it.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.error_report_verifier import (
    UNSUPPORTED_NOTE,
    build_prompt,
    parse_verdicts,
    verify_rows,
)
from src.error_report_writer import (
    build_report_rows,
    normalise_rows,
    parse_error_report,
)


def _json_row(stmt, kind="Factual", correction="the right version", source=""):
    return ('{"original_statement": "%s", "error_type": "%s", "severity": "High", '
            '"error_concern": "it is wrong", '
            '"correct_information_correction": "%s", "source_urls": "%s"}'
            % (stmt, kind, correction, source))


def _row(stmt, kind="Factual", severity="High", concern="it is wrong"):
    return {"original_statement": stmt, "error_type": kind, "severity": severity,
            "error_concern": concern,
            "correct_information_correction": "the right version", "source_urls": ""}


# ------------------------------------------------- a long lecture's chunks

def test_every_chunk_of_a_long_lecture_reaches_the_sheet():
    """A long lecture is generated in parts and each part returns its OWN array.
    The parser read the first one and stopped, so a three-part lecture reported
    the errors from its first third and marked itself complete."""
    block = "\n".join("[" + _json_row(f"error from part {i}") + "]" for i in (1, 2, 3))
    parsed = parse_error_report(block)

    assert len(parsed.rows) == 3, [r["original_statement"] for r in parsed.rows]
    assert parsed.complete
    statements = " ".join(r["original_statement"] for r in parsed.rows)
    for part in (1, 2, 3):
        assert f"part {part}" in statements


def test_a_single_clean_array_behaves_exactly_as_before():
    parsed = parse_error_report("[" + _json_row("the only error") + "]")
    assert len(parsed.rows) == 1 and parsed.ok and parsed.complete


def test_an_empty_report_is_still_a_valid_report():
    """The prompt asks for [] when there is nothing to say."""
    for block in ("", "   ", "[]"):
        parsed = parse_error_report(block)
        assert parsed.rows == [] and parsed.ok and parsed.complete, block


def test_a_wrapped_report_is_not_mistaken_for_a_partial_one():
    parsed = parse_error_report('{"errors": [' + _json_row("wrapped") + "]}")
    assert len(parsed.rows) == 1 and parsed.complete


# ------------------------------------------------------------- truncation

def test_complete_findings_survive_a_reply_that_was_cut_off():
    """Two whole findings sat in front of the truncation point and all three
    were discarded, because one of them was half-written."""
    block = ("[" + _json_row("complete one") + ", " + _json_row("complete two")
             + ', {"original_statement": "cut off mid')
    parsed = parse_error_report(block)

    assert len(parsed.rows) == 2
    assert parsed.salvaged and not parsed.complete


def test_a_bracket_inside_a_sentence_does_not_end_the_report():
    """`the Act [sic] applies` is prose, not structure. Treating it as structure
    truncates the report at that row."""
    block = ("[" + _json_row("the Act [sic] applies here") + ", "
             + _json_row("second finding") + "]")
    parsed = parse_error_report(block)
    assert len(parsed.rows) == 2 and parsed.complete


def test_a_malformed_bracket_cannot_swallow_the_rows_after_it():
    """One unclosed "[" makes everything after it part of an array that never
    ends. The rows are still readable and must still be read."""
    block = ("[" + _json_row("good one") + "]\n[ broken \n["
             + _json_row("good two") + "]")
    parsed = parse_error_report(block)
    assert len(parsed.rows) == 2
    assert not parsed.complete, "and it must not claim to be complete"


def test_nothing_readable_is_still_reported_as_a_failure():
    parsed = parse_error_report("I'm sorry, I can't help with that.")
    assert parsed.rows == [] and not parsed.ok


# --------------------------------------------------- saying what is missing

def test_a_partial_report_says_so_in_the_sheet():
    """Not in a log the reviewer never opens. A reviewer who reads "2 issues"
    and stops looking has been misled by a file that never mentioned it was
    missing part of the lecture."""
    block = ("[" + _json_row("complete one") + ", " + _json_row("complete two")
             + ', {"original_statement": "cut')
    rows, status = build_report_rows(block, marker_found=True)

    assert status == "partial"
    assert rows[0]["error_type"] == "Report coverage"
    assert rows[0]["severity"] == "High"
    assert "cut off" in rows[0]["error_concern"]
    # ...and the findings themselves are still there, below it.
    assert len([r for r in rows if r["error_type"] != "Report coverage"]) == 2


def test_a_complete_report_carries_no_coverage_row():
    rows, status = build_report_rows("[" + _json_row("only") + "]", marker_found=True)
    assert status == "ok"
    assert all(r["error_type"] != "Report coverage" for r in rows)


def test_the_coverage_row_survives_every_strictness():
    """It is not a finding about the lecture; it is the report describing its own
    limits, and no setting should be able to filter that away."""
    from src.error_report_writer import coverage_row

    for strictness in ("basic", "standard", "strict"):
        kept = normalise_rows([coverage_row("half of it was unreadable")],
                              strictness=strictness)
        assert kept, strictness


# ------------------------------------------------------------------ dedup

def test_the_same_finding_twice_is_reported_once():
    """Merging every chunk's array makes duplicates possible for the first time:
    chunk boundaries overlap."""
    rows = normalise_rows([_row("The Act was passed in 1972."),
                           _row("The Act was passed in 1972.")])
    assert len(rows) == 1


def test_two_different_faults_in_one_sentence_are_both_kept():
    """The key includes the ERROR TYPE. One sentence can carry a wrong year AND
    a wrong attribution, and keying on the statement alone drops the second."""
    rows = normalise_rows([
        _row("The Act was passed in 1972.", "Date/Year Error"),
        _row("The Act was passed in 1972.", "Person/Institution Error"),
    ])
    assert len(rows) == 2
    assert {r["error_type"] for r in rows} == {"Date/Year Error",
                                               "Person/Institution Error"}


def test_a_duplicate_contributes_the_fields_the_first_one_lacked():
    """The later row is redundant; the fields it filled in are not."""
    bare = _row("The Act was passed in 1972.")
    bare["correct_information_correction"] = ""
    rich = _row("The Act was passed in 1972.")
    rich["correct_information_correction"] = "It was 1972, under the MoEFCC."
    rich["source_urls"] = "https://example.gov.in/act"

    rows = normalise_rows([bare, rich])
    assert len(rows) == 1
    assert "MoEFCC" in rows[0]["correct_information_correction"]
    assert "example.gov.in" in rows[0]["source_urls"]


# ------------------------------------------------------ the second opinion

class _Client:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def generate(self, prompt, image_paths=None, **kw):
        self.calls += 1
        self.prompt = prompt

        class R:
            text = self.reply
        return R()


def test_an_unsupported_finding_is_demoted_not_deleted():
    """The verifier is the same kind of thing that produced the finding. It does
    not get to be the last word on whether a student sees a correction."""
    rows = [_row("India's forest cover is 21%.", "Numerical/outdated")]
    client = _Client('{"verdicts":[{"n":1,"verdict":"unsupported","why":"no source given"}]}')

    out, summary = verify_rows(rows, client, today="2026-08-21")
    assert len(out) == 1, "the row must survive"
    assert out[0]["severity"] == "Low"
    assert UNSUPPORTED_NOTE[:30] in out[0]["error_concern"]
    assert "no source given" in out[0]["error_concern"]
    assert summary["demoted"] == 1


def test_a_supported_finding_is_left_exactly_as_it_was():
    rows = [_row("Article 342 concerns Scheduled Castes.")]
    before = dict(rows[0])
    out, summary = verify_rows(rows, _Client('{"verdicts":[{"n":1,"verdict":"supported"}]}'),
                               today="2026-08-21")
    assert out[0] == before
    assert summary["demoted"] == 0


def test_the_whole_list_costs_one_call():
    """A verification pass that doubled the bill would be switched off, and a
    switched-off check verifies nothing."""
    rows = [_row(f"finding {i}") for i in range(12)]
    client = _Client('{"verdicts":[' + ",".join(
        '{"n":%d,"verdict":"supported"}' % i for i in range(1, 13)) + "]}")
    verify_rows(rows, client, today="2026-08-21")
    assert client.calls == 1


@pytest.mark.parametrize("reply", ["", "not json at all", '{"verdicts": "nonsense"}'])
def test_an_unreadable_verdict_leaves_every_row_untouched(reply):
    """Failing open is the only safe direction: a check that can block a report
    is a new way to lose one."""
    rows = [_row("a finding")]
    out, summary = verify_rows(rows, _Client(reply), today="2026-08-21")
    assert out[0]["severity"] == "High"
    assert not summary["ran"]


def test_a_failing_call_does_not_take_the_report_with_it():
    class Dead:
        def generate(self, *a, **kw):
            raise RuntimeError("proxy down")

    rows = [_row("a finding")]
    out, summary = verify_rows(rows, Dead(), today="2026-08-21")
    assert out == rows and not summary["ran"]
    assert "call failed" in summary["reason"]


def test_the_apps_own_rows_are_never_second_guessed():
    """"No issue", "Report coverage" and the correction-meta rows are the app
    reporting on its own output. The verifier has nothing to add to them."""
    rows = [_row("Coverage of this report is incomplete.", "Report coverage"),
            _row("No reportable error found.", "No issue")]
    out, summary = verify_rows(rows, _Client('{"verdicts":[{"n":1,"verdict":"unsupported"}]}'),
                               today="2026-08-21")
    assert all(r["severity"] == "High" for r in out)
    assert summary["reason"] == "nothing verifiable"


def test_the_verifier_is_told_the_date_and_asked_to_refute():
    rows = [_row("a finding")]
    client = _Client('{"verdicts":[{"n":1,"verdict":"supported"}]}')
    verify_rows(rows, client, today="2026-08-21")
    assert "TODAY'S DATE IS 2026-08-21" in client.prompt
    assert "REFUTE" in client.prompt
    assert "no live web access" in client.prompt.lower()


def test_verdict_numbers_outside_the_list_are_ignored():
    """A model that invents a finding 9 out of three must not index into rows."""
    assert parse_verdicts('{"verdicts":[{"n":9,"verdict":"unsupported"}]}', 3) == {}


def test_the_prompt_lists_every_finding_it_is_asked_about():
    rows = [_row("first thing"), _row("second thing")]
    prompt = build_prompt(rows, "2026-08-21")
    assert "1." in prompt and "2." in prompt
    assert "first thing" in prompt and "second thing" in prompt


# ------------------------------------------------- truncation at the source

def test_a_cut_off_reply_is_detected_rather_than_inferred():
    """finishReason is the only reliable signal. Truncated text still reads as
    prose, which is why it was written into documents as though the class had
    simply ended there."""
    from src.ai_client import GeminiResponse

    assert GeminiResponse("x", "p", "m", finish_reason="MAX_TOKENS").truncated
    assert GeminiResponse("x", "p", "m", finish_reason="SAFETY").truncated
    assert not GeminiResponse("x", "p", "m", finish_reason="STOP").truncated
    # A provider that says nothing is not assumed to have truncated.
    assert not GeminiResponse("x", "p", "m").truncated


def test_every_call_that_emits_a_whole_document_raises_the_token_cap():
    """Four calls each return a COMPLETE set of notes and ran on the 12,000
    default — the merge that assembles the whole lecture among them. Devanagari
    tokenises heavier, so the notes most likely to overrun were the ones nobody
    on the team reads back."""
    import re

    source = (Path(__file__).resolve().parents[1] / "src" / "pipeline.py").read_text(
        encoding="utf-8")
    # Every client.generate(...) call, with its arguments — anchored on the CALL,
    # not on the name, which also appears in the import block at the top.
    calls = re.findall(r"client\.generate\((.*?)\)\s*$", source, re.S | re.M)
    document_calls = [
        c for c in calls
        if any(k in c for k in ("merge_prompt", "build_condense_prompt",
                                "build_page_compress_prompt"))
    ]
    # Not an exact count: the regex spans nested calls, so one call can appear in
    # more than one match. What matters is that none of them lacks the cap.
    assert len(document_calls) >= 3, [c[:60] for c in document_calls]
    for call in document_calls:
        assert "TRANSCRIPT_NOTES_MAX_OUTPUT_TOKENS" in call, call[:120]


def test_a_lost_part_of_a_lecture_is_announced():
    """Empty parts were filtered out silently, producing notes covering four
    fifths of a class with nothing anywhere saying which fifth was missing."""
    source = (Path(__file__).resolve().parents[1] / "src" / "pipeline.py").read_text(
        encoding="utf-8")
    assert "came back empty and are" in source
    assert "empty = sum(1 for p in parts if not p)" in source


# ------------------------------------------------------- currency claims

def test_the_model_is_told_the_date_and_its_own_limits():
    from src.prompt_builder import error_report_contract_block

    block = error_report_contract_block("standard", today="2026-08-21")
    assert "TODAY'S DATE IS 2026-08-21" in block
    assert "NO live web access" in block


def test_an_outdated_claim_needs_a_value_a_source_and_a_date():
    """The block used to say: when you cannot verify a figure, report it anyway.
    That manufactures High-severity findings about numbers nothing checked."""
    from src.prompt_builder import error_report_contract_block

    block = error_report_contract_block("standard", today="2026-08-21")
    assert "the replacement value, stated exactly" in block
    assert "If you cannot supply all three, it is NOT an error" in block
    # And the sentence that produced the empty findings is gone as an instruction.
    assert "still report it, and write" not in block


# ------------------------------------------------------------ layout cover

def test_the_layout_check_covers_the_whole_document():
    """It rasterised three pages of a fifteen-page note and reported "layout
    verified" — and the pages it skipped were the middle ones, which is where a
    table pushes body text into the notes column."""
    from src.config import DCN_RENDER_CHECK_MAX_PAGES, DCN_TARGET_PAGES
    from src.docx_render_check import _pages_to_render

    assert DCN_RENDER_CHECK_MAX_PAGES > DCN_TARGET_PAGES[1]
    for pages in (3, 12, DCN_TARGET_PAGES[1]):
        assert len(_pages_to_render(pages, DCN_RENDER_CHECK_MAX_PAGES)) == pages


def test_a_partial_layout_check_says_how_much_it_saw():
    """"No overlaps found" is a different claim depending on how much was looked
    at, and the two read identically unless one of them says so."""
    from src.docx_render_check import RenderCheck

    partial = RenderCheck(ok=True, pdf_path=None, page_images=[], page_count=20,
                          overlap_pages=[], pages_checked=16)
    assert not partial.fully_checked
    full = RenderCheck(ok=True, pdf_path=None, page_images=[], page_count=12,
                       overlap_pages=[], pages_checked=12)
    assert full.fully_checked


# ============================================================ QC coverage
# The notes text always reached the reviewer whole. What did not was the
# CHECKING: six caps sized for a document with no length limit left parts of a
# 15-page note unexamined, and the largest of them was invisible.

def _full_note_words() -> int:
    from src.config import DCN_TARGET_PAGES, DCN_WORDS_PER_PAGE

    return DCN_TARGET_PAGES[1] * max(DCN_WORDS_PER_PAGE.values())


def test_every_claim_in_a_full_note_can_be_extracted_and_verified():
    """The gap that mattered: 60 claims could be extracted and only 20 verified,
    so two thirds of the checkable facts in a note were never checked against a
    source — and the only record was a line in source_queries.json."""
    from src.qc.claim_extractor import MAX_CANDIDATES
    from src.qc.qc_config import QCLimits

    verified = QCLimits().max_claims_verified
    assert verified >= MAX_CANDIDATES or verified >= _full_note_words() / 40, (
        f"only {verified} of up to {MAX_CANDIDATES} extracted claims get verified")


def test_the_reviewers_own_output_is_not_truncated_for_a_full_note():
    from src.qc.ai_review import _MAX_CLAIMS, _MAX_ISSUES

    floor = _full_note_words() / 40
    assert _MAX_ISSUES > floor, f"reviewer issues capped at {_MAX_ISSUES}"
    assert _MAX_CLAIMS > floor, f"reviewer claims capped at {_MAX_CLAIMS}"


def test_the_deterministic_pass_reports_a_full_notes_worth_of_findings():
    from src.qc.deterministic_checks import MAX_TOTAL_ISSUES

    assert MAX_TOTAL_ISSUES > _full_note_words() / 40


def test_the_whole_lecture_is_available_as_ground_truth():
    """The transcript is what "does this note match what was taught?" is answered
    against. Cut at 40,000 characters — about half a 90-minute class — that
    question quietly became "does it match the first half?"."""
    from src.qc.ai_review import _MAX_TRANSCRIPT_CHARS, _transcript_for_review

    # A 90-minute lecture is roughly 80,000 characters.
    assert _MAX_TRANSCRIPT_CHARS >= 160_000

    whole = "x" * 50_000
    assert _transcript_for_review(whole) == whole, "a normal lecture is sent entire"


def test_a_transcript_that_still_overruns_says_so_in_the_prompt():
    """Otherwise the reviewer reports the second half of the notes as unsupported
    — findings that are wrong because of a cap the model was never told about."""
    from src.qc.ai_review import _MAX_TRANSCRIPT_CHARS, _transcript_for_review

    out = _transcript_for_review("y" * (_MAX_TRANSCRIPT_CHARS + 1000))
    assert "TRANSCRIPT TRUNCATED HERE" in out
    assert "Do NOT report a note as unsupported" in out


def test_the_visual_check_covers_the_pages_a_note_can_have():
    from src.config import DCN_TARGET_PAGES
    from src.qc.qc_config import QCLimits

    assert QCLimits().max_visual_check_pages > DCN_TARGET_PAGES[1]


def test_an_unverified_claim_is_reported_to_the_user_not_just_logged():
    """A claim extracted and then never verified is the quietest failure in the
    stage: the report shows the verified ones and nothing says the rest were
    skipped."""
    source = (Path(__file__).resolve().parents[1] / "src" / "qc" / "engine.py").read_text(
        encoding="utf-8")
    assert "were not " in source and "Source verification covered" in source
    assert "result.warnings.append(" in source


def test_the_policy_file_and_the_code_defaults_agree():
    """The YAML is the policy; the dataclass defaults apply only when a key is
    missing. Two different numbers means the answer depends on which file loaded."""
    import yaml

    from src.qc.qc_config import QCLimits

    path = Path(__file__).resolve().parents[1] / "config" / "upsc_qc_rules.yaml"
    limits = yaml.safe_load(path.read_text(encoding="utf-8"))["limits"]
    defaults = QCLimits()
    for key in ("max_claims_verified", "max_visual_check_pages"):
        assert limits[key] == getattr(defaults, key), (
            f"{key}: yaml={limits[key]} default={getattr(defaults, key)}")
