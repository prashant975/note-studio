"""Regressions for the six defects behind the trial run's recall and precision.

The trial that prompted this file reported 14 of 40 seeded errors (35% recall)
and, on a clean document, 9 findings of which 9 were false. Each test below
pins one of the causes so it cannot come back silently:

  1. a cut-off reply used to discard every row in that part      -> _salvage_rows
  2. a failed or truncated part used to report as "completed"    -> coverage
  3. the prompt never said what today was, so current facts
     were reported as "outdated"                                 -> recency guard
  4. nothing ever challenged a finding before it was printed     -> verification
  5. two errors in one sentence collapsed to one                 -> _row_key
  6. the visual check's page sample was never disclosed          -> coverage note

What these cannot prove is that the model answers well; that needs a real run.
They prove the machinery around it stops losing and stops inventing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qc.engine import (  # noqa: E402
    _ErrorReportPass,
    _merge_error_blocks,
    _row_key,
    _salvage_rows,
)
from src.qc.verify import run_verification_pass  # noqa: E402


def row(statement="A statement", error_type="Factual", concern="is wrong",
        severity="High"):
    return {"original_statement": statement, "error_type": error_type,
            "severity": severity, "error_concern": concern,
            "correct_information_correction": "the fix", "source_urls": []}


# ---------------------------------------------------------------- 1. salvage

def test_a_cut_off_reply_keeps_the_rows_it_managed_to_write():
    """The defect: json.loads failed, and the whole part was dropped."""
    full = [row(f"Statement {i}") for i in range(1, 6)]
    body = json.dumps(full, indent=2)
    cut = body[:body.index('"Statement 4"') + 30]      # cut mid-object

    recovered = _salvage_rows(cut)

    assert len(recovered) == 3, "complete objects before the cut must survive"
    assert [r["original_statement"] for r in recovered] == [
        "Statement 1", "Statement 2", "Statement 3"]


def test_salvage_is_not_fooled_by_braces_inside_quoted_text():
    """A brace in a value must not be read as structure."""
    rows = [row('He wrote {"a": 1} on the board'), row('An escaped quote \\" and }')]
    recovered = _salvage_rows(json.dumps(rows))
    assert len(recovered) == 2


def test_an_unreadable_reply_is_never_reported_as_a_clean_document():
    """Salvaging nothing must stay distinguishable from finding nothing.

    This is the trap the fix itself introduced: salvage turns a garbled reply
    into a valid empty array, which reads as "no issues" unless the count of
    unusable blocks is carried out separately.
    """
    _merged, salvaged, unusable = _merge_error_blocks(["{not json at all"])
    assert salvaged == 0
    assert unusable == 1, "an unreadable block must be counted, not silently emptied"

    _merged, salvaged, unusable = _merge_error_blocks(["[]"])
    assert (salvaged, unusable) == (0, 0), "a genuinely empty report is not a failure"


# --------------------------------------------------------------- 2. coverage

def test_a_failed_part_makes_the_run_incomplete():
    p = _ErrorReportPass(total_parts=5, failed_parts=[3],
                         errors=["part 3: proxy error 429"])
    assert p.complete is False
    assert any("INCOMPLETE COVERAGE" in w for w in p.coverage_warnings())


def test_a_truncated_part_makes_the_run_incomplete():
    p = _ErrorReportPass(total_parts=5, truncated_parts=[2])
    assert p.complete is False
    text = " ".join(p.coverage_warnings())
    assert "INCOMPLETE COVERAGE" in text and "cut off" in text


def test_a_run_with_every_part_intact_is_complete():
    assert _ErrorReportPass(total_parts=4).complete is True
    assert _ErrorReportPass(total_parts=4).coverage_warnings() == []


def test_a_partial_scan_never_wears_a_verdict_status():
    """"Clean" and "Issues found" both imply the whole file was checked."""
    from src.models import PdfQcResult
    from src.report.workbook import _status_label

    r = PdfQcResult(file_name="x.pdf")
    r.status = "completed_with_warnings"
    r.error_report_complete = False
    assert _status_label(r) == "PARTIAL SCAN"

    r.error_report_complete = True
    assert _status_label(r) == "Issues found"


# ---------------------------------------------------------- 3. recency guard

def test_the_prompt_tells_the_model_todays_date_and_its_own_blind_spot():
    from src.qc_prompts import build_pdf_error_report_prompt

    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.")
    assert "TODAY'S DATE IS" in prompt
    assert "YOUR OWN KNOWLEDGE IS OLDER" in prompt
    # Internal contradiction stays reportable — it needs no outside knowledge.
    assert "CONTRADICT THEMSELVES" in prompt


def test_the_prompt_no_longer_asks_for_unverifiable_figures():
    """It used to say: when you cannot verify a figure, "still report it"."""
    from src.qc_prompts import build_pdf_error_report_prompt

    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.")
    assert "still report it" not in prompt
    assert "DO NOT report it" in prompt


# ----------------------------------------------------------- 4. verification

class _Verifier:
    """Refutes recency claims, upholds durable ones — like a real second pass."""

    def generate(self, prompt, image_paths=None, **kwargs):
        import re

        verdicts = []
        for match in re.finditer(r"--- CLAIM (\d+) ---(.*?)(?=--- CLAIM |\Z)",
                                 prompt, re.S):
            claim_id, body = int(match.group(1)), match.group(2)
            if "Outdated data" in body or "Current-affairs" in body:
                verdict = "NOT_AN_ERROR"
            elif "cannot tell" in body:
                verdict = "UNVERIFIABLE"
            else:
                verdict = "REAL"
            verdicts.append({"id": claim_id, "verdict": verdict, "why": "because"})

        class R:
            text = json.dumps(verdicts)

        return R()


def test_verification_removes_the_recency_false_positives():
    rows = [
        row("Scheme covers 12 crore families", "Outdated data", "does not match my memory"),
        row("Article 21 protects property", "Constitutional/legal mismatch", "wrong article"),
    ]
    result = run_verification_pass(_Verifier(), rows)

    assert len(result.rows) == 1
    assert result.rows[0]["error_type"] == "Constitutional/legal mismatch"
    assert len(result.dropped) == 1
    assert any("removed by the verification pass" in w for w in result.warnings())


def test_an_unverifiable_claim_is_kept_but_demoted_not_deleted():
    """Deleting a possibly-real error is worse than flagging a doubtful one."""
    rows = [row("Something", "Unsupported claim", "cannot tell either way")]
    result = run_verification_pass(_Verifier(), rows)

    assert len(result.rows) == 1
    assert result.rows[0]["severity"] == "Low"
    # The caveat leads the cell, so a reviewer sees it without expanding the row.
    assert result.rows[0]["error_concern"].startswith("[LEAD")
    assert result.downgraded == 1


def test_a_broken_verifier_can_never_delete_a_finding():
    """Fail-safe: if the second opinion cannot be had, the rows stand."""
    class Garbage:
        def generate(self, *a, **k):
            class R:
                text = "I was unable to complete this request."
            return R()

    class Exploding:
        def generate(self, *a, **k):
            raise RuntimeError("proxy error 429")

    rows = [row("A"), row("B"), row("C")]
    for client in (Garbage(), Exploding(), None):
        result = run_verification_pass(client, rows)
        assert len(result.rows) == 3, f"{client!r} must not lose rows"
        assert result.dropped == []


def test_a_verifier_that_answers_for_only_some_claims_leaves_the_rest_alone():
    class Partial:
        def generate(self, *a, **k):
            class R:
                text = json.dumps([{"id": 1, "verdict": "NOT_AN_ERROR", "why": "x"}])
            return R()

    rows = [row("A"), row("B"), row("C")]
    result = run_verification_pass(Partial(), rows)
    assert len(result.rows) == 2, "only the judged claim may be removed"
    assert [r["original_statement"] for r in result.rows] == ["B", "C"]


# ------------------------------------------------------------------ 5. dedup

def test_two_different_errors_in_one_sentence_both_survive():
    """The defect: the key was the quoted sentence, so the second was dropped."""
    sentence = "The Act was passed in 1992 under Article 370."
    blocks = [json.dumps([
        row(sentence, "Numerical/outdated", "the year is wrong"),
        row(sentence, "Constitutional/legal mismatch", "the article is wrong"),
    ])]

    merged, _salvaged, _unusable = _merge_error_blocks(blocks)
    kept = json.loads(merged)

    assert len(kept) == 2, "one sentence can carry more than one error"
    assert {r["error_type"] for r in kept} == {
        "Numerical/outdated", "Constitutional/legal mismatch"}


def test_the_same_error_arriving_twice_from_overlapping_chunks_collapses():
    """Chunks overlap on purpose, so real duplicates must still be removed."""
    duplicate = row("Overlapping statement", "Factual", "same concern")
    merged, _salvaged, _unusable = _merge_error_blocks(
        [json.dumps([duplicate]), json.dumps([duplicate])])

    assert len(json.loads(merged)) == 1


def test_the_dedup_key_uses_type_and_concern_not_just_the_quote():
    quote = "Identical quoted sentence"
    assert _row_key(row(quote, "Factual", "one")) != _row_key(row(quote, "Factual", "two"))
    assert _row_key(row(quote, "Factual", "one")) == _row_key(row(quote, "Factual", "one"))


# ============================================================================
# Second wave: defects found by the adversarial review of the first six fixes.
# ============================================================================

def test_every_page_reaches_a_chunk_whatever_the_page_sizes():
    """A page big enough to flush alone used to strand the page after it.

    The old tail test asked how BIG the leftover was, not whether it was NEW,
    so a single un-flushed page at the end was never sent to the model at all.
    """
    import random
    import re as _re
    from dataclasses import dataclass, field as dfield

    from src.qc import engine

    @dataclass
    class _P:
        page_no: int
        text: str
        image_path: object = None
        spans: list = dfield(default_factory=list)

    @dataclass
    class _Doc:
        pages: list
        language_code: str = "en"

        @property
        def paged_text(self):
            return "\n\n".join(f"[Page {p.page_no}]\n{p.text}" for p in self.pages)

    def covered(doc):
        chunks = engine._chunk_pages(doc, paged_text=doc.paged_text,
                                     text_is_readable=True)
        if len(chunks) == 1:
            return {p.page_no for p in doc.pages}
        seen = set()
        for c in chunks:
            seen |= {int(m.group(1)) for m in _re.finditer(r"\[Page (\d+)\]", c.text)}
        return seen

    # The exact shape that used to lose its last page.
    doc = _Doc([_P(1, "w " * 3200), _P(2, "w " * 3200), _P(3, "t " * 50)])
    assert covered(doc) == {1, 2, 3}, "the trailing short page must be checked"

    random.seed(11)
    for _ in range(120):
        n = random.randint(1, 45)
        pages = [_P(i + 1, "w " * random.choice([20, 200, 400, 900, 3200, 6500]))
                 for i in range(n)]
        assert covered(_Doc(pages)) == set(range(1, n + 1))


def test_a_fenced_or_wrapped_reply_is_read_not_salvaged():
    """A complete reply in ```json fences used to be mistaken for a truncated one.

    It failed json.loads, fell through to salvage, and — because needing salvage
    is treated as proof of truncation — got the whole file marked PARTIAL SCAN.
    A false incompleteness warning teaches people to ignore the real ones.
    """
    from src.qc.engine import _merge_error_blocks

    rows = [row(f"S{i}") for i in range(1, 4)]
    body = json.dumps(rows)

    for name, block in {
        "fenced": f"```json\n{body}\n```",
        "prose + fenced": f"Here you go:\n```json\n{body}\n```\nHope that helps.",
        "wrapper object": json.dumps({"errors": rows}),
        "fenced wrapper": "```json\n" + json.dumps({"errors": rows}) + "\n```",
    }.items():
        merged, salvaged, unusable = _merge_error_blocks([block])
        assert len(json.loads(merged)) == 3, name
        assert salvaged == 0, f"{name} must parse cleanly, not be salvaged"
        assert unusable == 0, name


def test_a_truncated_wrapper_still_yields_its_complete_rows():
    """Rows nested one level down were invisible to a depth-0 scan."""
    from src.qc.engine import _merge_error_blocks

    rows = [row(f"S{i}") for i in range(1, 4)]
    body = json.dumps({"errors": rows})
    cut = body[:body.index('"S3"') + 8]

    merged, salvaged, _unusable = _merge_error_blocks([cut])
    assert len(json.loads(merged)) == 2
    assert salvaged == 2


def test_an_unreadable_part_is_reported_even_when_other_parts_worked():
    """`unusable` used to be consulted only when EVERY part was unreadable."""
    from src.qc.engine import _merge_error_blocks

    good = json.dumps([row("A real error")])
    merged, _salvaged, unusable = _merge_error_blocks([good, "I could not read that."])
    assert len(json.loads(merged)) == 1
    assert unusable == 1, "the unreadable part must still be counted"


# ------------------------------------------------- strictness filter (recall)

def test_a_paraphrased_error_type_is_not_deleted_by_strictness():
    """The filter contradicted its own documented contract.

    ERROR_TYPES says an invented type "is kept verbatim but does not participate
    in strictness filtering". The code dropped any non-High row whose type was
    outside the allowed set — so "Factual error" instead of "Factual" at Medium
    severity silently lost a real finding.
    """
    from src.error_report_writer import normalise_rows

    for error_type in ("Factual", "Factual error", "Factual inaccuracy",
                       "Numerical error", "Something Novel"):
        kept = normalise_rows([row("S", error_type, "c", "Medium")],
                              strictness="standard")
        assert kept, f"{error_type!r} at Medium must not be deleted"


def test_a_report_emptied_by_strictness_never_reads_as_clean():
    from src.error_report_writer import build_report_rows

    dropped: list[dict] = []
    rows, status = build_report_rows(
        json.dumps([row("S1", "Unsupported claim", "c", "Medium"),
                    row("S2", "Terminology issue", "c", "Low")]),
        marker_found=True, strictness="standard", dropped=dropped)

    assert status == "filtered_out", "not the same as 'no_issues'"
    assert len(dropped) == 2
    assert "removed by the 'standard' strictness level" in rows[0]["original_statement"]


def test_strictness_still_filters_what_it_is_meant_to():
    """The fix must not turn strictness into a no-op for known types."""
    from src.error_report_writer import normalise_rows

    dropped: list[dict] = []
    kept = normalise_rows([row("S", "Terminology issue", "c", "Low")],
                          strictness="basic", dropped=dropped)
    assert kept == [] and len(dropped) == 1
    # High severity is never filtered, whatever the type.
    assert normalise_rows([row("S", "Terminology issue", "c", "High")],
                          strictness="basic")


# --------------------------------------------------------- verifier hardening

def test_a_self_contradicting_verdict_leaves_the_row_alone():
    class Contradicts:
        def generate(self, *a, **k):
            class R:
                text = json.dumps([{"id": 1, "verdict": "NOT_AN_ERROR", "why": "a"},
                                   {"id": 1, "verdict": "REAL", "why": "b"}])
            return R()

    result = run_verification_pass(Contradicts(), [row("A"), row("B")])
    assert len(result.rows) == 2
    assert result.dropped == []


def test_a_truncated_verification_reply_is_not_trusted():
    class Truncated:
        def generate(self, *a, **k):
            class R:
                text = json.dumps([{"id": 1, "verdict": "NOT_AN_ERROR", "why": "x"}])
                truncated = True
            return R()

    result = run_verification_pass(Truncated(), [row("A"), row("B")])
    assert len(result.rows) == 2, "a cut-off audit may not delete anything"
    assert result.failed_batches == 1


def test_a_partly_answered_batch_is_flagged_not_counted_as_reviewed():
    class Partial:
        def generate(self, *a, **k):
            class R:
                text = json.dumps([{"id": 1, "verdict": "REAL", "why": "x"}])
            return R()

    result = run_verification_pass(Partial(), [row(f"S{i}") for i in range(5)])
    assert len(result.rows) == 5
    assert result.partial_batches == 1
    assert any("only some of the claims" in w for w in result.warnings())


# ============================================================================
# Round 2 (graded): 21/40 found, 35 reported, 10 of them repeats.
# Reviewer: "There are pages and pages not considered by the system."
# ============================================================================

def _fake_doc(page_count: int, words_per_page: int = 250):
    from dataclasses import dataclass, field as dfield

    @dataclass
    class _P:
        page_no: int
        text: str
        image_path: object = None
        spans: list = dfield(default_factory=list)

    @dataclass
    class _Doc:
        pages: list
        page_count: int
        language_code: str = "en"

        @property
        def paged_text(self):
            return "\n\n".join(f"[Page {p.page_no}]\n{p.text}" for p in self.pages)

    pages = [_P(i + 1, " ".join(f"w{i}_{j}" for j in range(words_per_page)))
             for i in range(page_count)]
    return _Doc(pages, page_count)


def test_a_whole_document_is_never_sent_as_one_call():
    """The defect behind "pages and pages not considered".

    The old rule sent the document in ONE request whenever it fitted under a
    6,000-word ceiling. A 19-page set of notes is ~4,700 words, so it always
    fitted — and a model asked to audit nineteen pages in one answer skims.
    The graded run that scored 21/40 made exactly one call.
    """
    from src.qc import engine

    doc = _fake_doc(19)
    assert len(doc.paged_text.split()) < 6000, "the fixture must be the size that used to slip through"

    chunks = engine._chunk_pages(doc, paged_text=doc.paged_text, text_is_readable=True)
    assert len(chunks) > 1, "a 19-page document must never go in a single call"
    for c in chunks:
        pages = {int(m) for m in __import__("re").findall(r"\[Page (\d+)\]", c.text)}
        assert len(pages) <= 4, "a window must stay small enough to be read closely"


def test_every_page_reaches_a_window_at_any_document_length():
    import re as _re

    from src.qc import engine

    for page_count in (1, 2, 3, 7, 19, 40, 97):
        doc = _fake_doc(page_count)
        chunks = engine._chunk_pages(doc, paged_text=doc.paged_text,
                                     text_is_readable=True)
        seen = set()
        for c in chunks:
            seen |= {int(m) for m in _re.findall(r"\[Page (\d+)\]", c.text)}
        assert seen == set(range(1, page_count + 1)), (
            f"{page_count}-page document missed {sorted(set(range(1, page_count + 1)) - seen)}")


# ------------------------------------------------------------ 10 repetitions

def test_one_error_quoted_three_ways_collapses_to_one_row():
    """Smaller windows mean more repeats, so dedup has to catch rephrasings.

    Exact-key dedup only sees a verbatim repeat, and the model does not repeat
    itself verbatim — it quotes a different span of the same sentence and
    rewords the complaint.
    """
    from src.qc.engine import dedupe_rows

    rows = [
        row("It is the largest Hindu temple complex in Malaysia", "Factual",
            "Prambanan is in Indonesia, not Malaysia", "High"),
        row("largest Hindu temple complex in Malaysia and one of the finest",
            "Factual", "The temple is located in Indonesia rather than Malaysia", "High"),
        row("the largest Hindu temple complex in Malaysia", "Factual",
            "Wrong country: it is Indonesia", "High"),
    ]
    for r in rows:
        r["correct_information_correction"] = "Indonesia"

    kept, removed = dedupe_rows(rows)
    assert len(kept) == 1 and removed == 2


def test_two_different_errors_in_one_sentence_still_both_survive():
    """The guard against dedup going too far."""
    from src.qc.engine import dedupe_rows

    sentence = "Constructed during the 12th century CE under the Angkor Dynasty"
    rows = [
        {**row(sentence, "Factual", "Prambanan was built in the 9th century CE"),
         "correct_information_correction": "9th century CE"},
        {**row(sentence, "Factual", "It was the Sanjaya Dynasty, not Angkor"),
         "correct_information_correction": "Sanjaya Dynasty"},
    ]
    kept, removed = dedupe_rows(rows)
    assert len(kept) == 2 and removed == 0


def test_dedup_keeps_the_row_that_carries_a_correction():
    """A reviewer acts on the correction, not on the prose about the problem."""
    from src.qc.engine import dedupe_rows

    a = {**row("Mount Etna's eruptions", "Factual", "Wrong volcano"),
         "correct_information_correction": "Mount Merapi"}
    b = {**row("Mount Etna's eruptions damaged it", "Factual",
               "The volcano named here is not the one that damaged the complex"),
         "correct_information_correction": ""}
    kept, _removed = dedupe_rows([a, b])
    assert len(kept) == 1
    assert kept[0]["correct_information_correction"] == "Mount Merapi"


# ------------------------------------------------------ 7. the second lens

def test_the_second_pass_reads_the_same_pages_through_the_entity_lens():
    """The graded misses clustered in quiet classes — a wrong city, a wrong
    acronym expansion, a wrong tense — that an open "what is wrong?" read walks
    past. The second pass exists to read the same pages against exactly that
    checklist, and it must still carry the recency guard and the contract."""
    from src.qc_prompts import build_pdf_error_report_prompt

    open_pass = build_pdf_error_report_prompt("[Page 1]\nSome notes.")
    lens_pass = build_pdf_error_report_prompt("[Page 1]\nSome notes.",
                                              lens="entities")
    assert "SECOND PASS" not in open_pass
    assert "SECOND PASS" in lens_pass
    for probe in ("PROPER NOUNS", "ACRONYMS", "GRAMMAR AND TENSE",
                  "FIGURES THAT CANNOT BE TRUE"):
        assert probe in lens_pass, f"the lens checklist must cover {probe}"
    assert "TODAY'S DATE IS" in lens_pass
    assert "# ERROR_REPORT_START" in lens_pass


def _five_page_doc():
    from dataclasses import dataclass, field as dfield

    @dataclass
    class _P:
        page_no: int
        text: str
        image_path: object = None
        spans: list = dfield(default_factory=list)

    @dataclass
    class _Doc:
        pages: list
        language_code: str = "en"

        @property
        def paged_text(self):
            return "\n\n".join(f"[Page {p.page_no}]\n{p.text}"
                               for p in self.pages)

    return _Doc([_P(i + 1, f"Words of page {i + 1}. " * 30) for i in range(5)])


class _CountingClient:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, images=None, **kwargs):
        self.prompts.append(prompt)

        class R:
            text = "# ERROR_REPORT_START\n[]\n# ERROR_REPORT_END"
            truncated = False

        return R()


def test_every_text_window_is_read_by_both_passes(tmp_path):
    from src.qc import engine

    doc = _five_page_doc()
    client = _CountingClient()
    report = engine._run_error_report_pass(
        client, doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")

    windows = len(engine._chunk_pages(doc, paged_text=doc.paged_text,
                                      text_is_readable=True))
    assert windows > 1
    consistency = [p for p in client.prompts if "WHOLE-DOCUMENT PASS" in p]
    assert len(consistency) == 1, (
        "the internal-consistency pass reads the whole document exactly once")
    assert "[Page 5]" in consistency[0], "it must actually carry every page"
    assert len(client.prompts) == windows * 2 + 1, (
        "each text window must be read by the open pass AND the lens pass, "
        "plus one whole-document consistency pass")
    lensed = [p for p in client.prompts if "SECOND PASS" in p]
    assert len(lensed) == windows
    # The lens pass covers the same pages, not different ones: every window's
    # page range appears in exactly one open prompt and one lens prompt.
    import re as _re

    def ranges(prompts):
        return sorted(m.group(1) for p in prompts
                      if (m := _re.search(r"Pages: (\S+)", p)))

    assert ranges(lensed) == ranges(
        [p for p in client.prompts
         if "SECOND PASS" not in p and "WHOLE-DOCUMENT PASS" not in p])
    assert report.complete


def test_a_single_pass_run_stays_single_pass(tmp_path, monkeypatch):
    from src.qc import engine

    monkeypatch.setattr(engine, "ERROR_REPORT_PASSES", 1)
    doc = _five_page_doc()
    client = _CountingClient()
    engine._run_error_report_pass(
        client, doc, tmp_path, paged_text=doc.paged_text,
        text_is_readable=True, file_name="x.pdf", lecture_title="",
        subject="", strictness="strict")
    assert not any("SECOND PASS" in p for p in client.prompts)
    assert not any("WHOLE-DOCUMENT PASS" in p for p in client.prompts)


# --------------------------------------- 8. the verifier's deletion policy

def test_the_verifier_may_not_delete_office_holder_or_implausibility_leads():
    """Measured on a real run: the verifier deleted two seeded errors (an
    implausible figure, a future-dated report) and kept the one guaranteed
    false positive (a person's current office contradicted from memory). The
    prompt now pins all three classes."""
    from src.qc import verify

    assert "OFFICE-HOLDERS, ABSOLUTELY" in verify._PROMPT
    assert "NEVER REAL" in verify._PROMPT
    assert "never NOT_AN_ERROR" in verify._PROMPT
    assert "CANNOT EXIST YET" in verify._PROMPT


def test_the_finder_prompt_carries_the_settled_facts_and_office_holder_rules():
    from src.qc_prompts import build_pdf_error_report_prompt

    prompt = build_pdf_error_report_prompt("[Page 1]\nSome notes.")
    assert "OFFICE-HOLDERS, ABSOLUTELY" in prompt
    assert "Equally safe to report" in prompt          # settled slow-moving facts
    assert "IMPLAUSIBLE figure" in prompt.replace("WILDLY ", "")
    assert "FUTURE-DATED SOURCE" in prompt


def test_two_pass_repeats_collapse_even_when_no_other_stage_contributed():
    """The fuzzy dedup used to run only when the review or the structural
    checks added rows. With two finder passes, the same mistake arrives twice
    in different words on every document — the collapse must be unconditional."""
    from src.models import PdfQcResult
    from src.qc.engine import _merge_extra_rows

    r = PdfQcResult(file_name="x.pdf")
    r.error_rows = [
        row("It is the largest Hindu temple complex in Malaysia",
            concern="Prambanan is located in Indonesia, not Malaysia"),
        row("the largest Hindu temple complex in Malaysia and one of the finest",
            concern="The temple complex is in Indonesia (Central Java), not in Malaysia"),
    ]
    _merge_extra_rows(r, [])
    assert len(r.error_rows) == 1


def test_an_empty_merge_never_wipes_the_no_issue_placeholder():
    from src.error_report_writer import no_issue_rows
    from src.models import PdfQcResult
    from src.qc.engine import _merge_extra_rows

    r = PdfQcResult(file_name="x.pdf")
    r.error_rows = no_issue_rows()
    before = list(r.error_rows)
    _merge_extra_rows(r, [])
    assert r.error_rows == before
