"""QC engine — the single orchestrator for one already-generated notes PDF.

This is the standalone counterpart of ``src/qc/engine.py`` in the
note-generation project. The stage ladder, the fail-closed proxy gate, the
per-stage try/except, and the "write every artifact even when its stage was
skipped" rule are that engine's design, kept. What changed is only what feeds
the stages: instead of marker text, slides and a transcript arriving from a
generation run, everything is read out of the finished PDF.

Stages, in order, and where each one comes from:

  1. integrity      src/qc/format_check.run_local_output_checks (PDF half)
  2. layout         src/pdf_layout_check.verify_pdf_layout
                    (= the original src/docx_render_check overflow scan)
  3. page_analysis  src/pdf/analyzer  — the page-level checks only a finished
                    PDF can answer; each stands in for an original check
  4. deterministic  src/qc/deterministic_checks.run_deterministic_checks
  5. structure      src/upsc_quality_checker.check_dcn_notes, over marker text
                    rebuilt from the PDF by src/pdf/reconstruct
  6. legacy_checks  src/quality_checker.quality_check
  7. visual         src/qc/format_check.run_visual_format_check  (AI, Strict)
  8. ai_review      src/qc/ai_review.run_ai_quality_review        (AI, Strict)
  9. error_report   the ERROR_REPORT contract prompt + the original
                    src/error_report_writer parsing and normalisation  (AI)

Nothing here writes to, moves or renames the input PDF.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from pathlib import Path

import pw_access

from ..config import (
    CHUNK_OVERLAP_WORDS,
    FACT_SWEEP_BATCH,
    FACT_SWEEP_MAX_CLAIMS,
    CHUNK_TRANSCRIPT_WORDS,
    ERROR_REPORT_MAX_OUTPUT_TOKENS,
    ERROR_REPORT_PASSES,
    LAYOUT_RENDER_PAGES,
    DCN_COLOR_H1,
    DCN_COLOR_TITLE,
    DEFAULT_ERROR_REPORT_STRICTNESS,
    MAX_PARALLEL_AI_CALLS,
    MAX_SINGLE_CALL_TRANSCRIPT_WORDS,
    MAX_VISUAL_CHECK_PAGES,
    PAGES_PER_ERROR_REPORT_CALL,
    MIN_NOTES_WORDS,
    VERIFY_FINDINGS,
)
from ..error_report_writer import (
    all_refuted_rows,
    build_report_rows,
    extract_error_report_block,
    no_issue_rows,
    parse_failure_rows,
    summarise,
    unavailable_rows,
)
from ..models import PdfQcResult
from ..pdf.analyzer import run_page_analysis
from ..pdf.extractor import PdfDocument, extract_pdf
from ..pdf.reconstruct import locate, reconstruct_marker_text
from ..pdf_layout_check import verify_pdf_layout
from ..qc_prompts import build_pdf_error_report_prompt
from ..quality_checker import quality_check
from ..upsc_quality_checker import check_dcn_notes
from ..utils import write_json
from .format_check import run_local_output_checks, run_visual_format_check
from .qc_config import QCConfigError, load_rules
from .verify import run_verification_pass

#: Problems run_local_output_checks reports about a DOCX. This app is given a
#: PDF and no DOCX, so they describe the input it was never handed, not a defect
#: in the file under review. The PDF half of that function is used unchanged.
_DOCX_ONLY_PREFIXES = (
    "DOCX file was not created.",
    "DOCX file is empty.",
    "DOCX package is missing",
    "DOCX has no footer part",
    "DOCX text content is nearly empty.",
    "Internal marker visible in the student DOCX",
    "Raw markdown table syntax visible in the DOCX.",
    "DOCX does not open as a ZIP package",
    "DOCX inspection failed:",
)

#: The same, for src/quality_checker.quality_check.
_DOCX_ONLY_WARNINGS = (
    "DOCX was not created.",
    "No extractable text found.",
    "No text layer found in the source;",
)

#: Severity for each stage's findings when the check itself does not say. The
#: values follow the original rules: structure/layout problems that reach a
#: student are High, things a reviewer should look at are Medium, style is Low.
_SEVERITY_BY_QC_SEVERITY = {"high": "High", "medium": "Medium", "low": "Low"}

#: Rendered pages per AI call when the text layer cannot be read and the pages
#: have to travel as images. Matches VISUAL_CLASSIFY_BATCH / the visual check's
#: page budget in the note-generation project — images go inline through the
#: proxy and small batches are what that path is tuned for.
PAGES_PER_IMAGE_CALL = 6

#: Checks that read the notes as text and therefore cannot be trusted when the
#: text layer is scrambled. Their findings are still produced (they can catch
#: real structural problems) but are dropped to Low and labelled, so a reviewer
#: is never sent chasing a "spelling inconsistency" that is a font artefact.
_TEXT_DEPENDENT_STAGES = {"deterministic", "structure", "legacy_checks"}


def _stage_cb(callback, label: str) -> None:
    if callback is None:
        return
    try:
        callback(label)
    except Exception:
        pass


def _finding(stage: str, check: str, severity: str, problem: str,
             page: int | None = None, location: str = "", caveat: str = "") -> dict:
    """One row for the report.

    ``problem`` is the check's own message, verbatim — never edited, never
    appended to. A reader comparing this report against the note-generation
    app's must find the same sentence. Anything this app needs to add about
    how far the finding can be trusted goes in ``caveat``, which the report
    writer renders in its own column.
    """
    return {
        "stage": stage,
        "check": check,
        "severity": severity,
        "page": page,
        "location": location or (f"Page {page}" if page else ""),
        "problem": problem,
        "caveat": caveat,
    }


#: Stages that only ever describe how the page is drawn — geometry, rendering,
#: pagination. The report no longer carries any of it: this app is asked what is
#: WRONG IN THE NOTES, and a table overflowing its column is a typesetting
#: matter for a different tool.
_GEOMETRY_STAGES = {"integrity", "layout", "page_analysis", "visual"}

#: The remaining stages are mixed: the same checker reports both a missing
#: heading prefix (geometry) and an inconsistent proper noun (Factual #6 of the
#: framework). These check names are the geometry half and are dropped; anything
#: else the stage reports is content and becomes an Error Report row.
#: Note what is NOT here: the structure checker's "Format" findings. Those are
#: the heading ladder being out of order and a missing QUICK REVISION section —
#: the framework's own Concept/Flow checks wearing a misleading label. Dropping
#: everything called "Format" would have thrown them away.
_GEOMETRY_CHECKS = {
    "layout", "visual", "content overflow", "page number", "page break",
    "table clipping", "text layer", "figure resolution", "page size",
    "blank page", "mixed page sizes",
    # The structure checker's whole "Format" bucket: the heading ladder, the
    # #H1/#H2/#H3 marker scaffolding, missing QUICK REVISION / Key Sources
    # sections, the lecture-title prefix, and the share of bullets that are
    # fragments. Every one of those describes how the document is ASSEMBLED.
    # This report answers a different question — what is wrong in what the
    # notes say — so none of them belong in it.
    "format", "content completeness",
    # "no 'as of <date>' qualifier" fires on any date-sensitive sentence
    # whether or not it is wrong, so it is a prompt to go and check rather than
    # a finding. The AI review reports a genuinely missing qualifier under
    # FACT-DATA-003 with the actual claim attached; that one stays.
    "factual risk",
}

#: Message fragments that mark a finding as being about writing STYLE rather
#: than about a mistake. A sentence being long is not an error in the notes.
_STYLE_MESSAGES = (
    "split it for readability",
    "short fragments rather than full sentences",
    "standardise on one format",
)

#: Findings whose CHECK is labelled formatting but whose SUBJECT is content.
#:
#: The title checker is the case that matters. Most of what it reports is naming
#: convention — a missing L__/DCN__ prefix, an unbreakable run — and none of that
#: belongs here. But one of its messages is different in kind: a title left as
#: the prompt's placeholder is wrong TEXT, printed on the student's page. That is
#: a defect in what the notes say, and it is caught by the same function as the
#: naming rules purely by accident of where the code lives.
_CONTENT_DESPITE_LABEL = (
    "is still the placeholder from the prompt",
)


def _is_geometry(finding: dict) -> bool:
    """True when a finding is about the document rather than about the content.

    The report carries content errors only — things the notes get WRONG. Layout,
    pagination, the heading scaffolding, missing boilerplate sections and
    writing style are all statements about the artefact, and a reviewer looking
    for factual mistakes has to read past every one of them to find the rows
    that matter.
    """
    if finding.get("stage") in _GEOMETRY_STAGES:
        return True
    problem = str(finding.get("problem", "")).lower()
    # Checked before the label, because these are the findings the label gets
    # wrong: content reported by a checker that mostly reports formatting.
    if any(marker in problem for marker in _CONTENT_DESPITE_LABEL):
        return False
    check = str(finding.get("check", "")).strip().lower()
    if check in _GEOMETRY_CHECKS or check.startswith("not applicable"):
        return True
    return any(marker in problem for marker in _STYLE_MESSAGES)


#: Check-name head word -> the report's error type, for findings that carry no
#: framework rule id.
_CHECK_HEAD_TO_TYPE = {
    "factual": "Factual",
    "factual risk": "Missing qualifier",
    "conceptual": "Conceptual",
    "content completeness": "Conceptual",
    "format": "Conceptual",          # structure: heading ladder, missing sections
    "language": "Language clarity",
}

_RULE_ID_RE = re.compile(r"\(([A-Z]{3,}-[A-Z]+(?:-\d+)?)\)")


def _error_type_from_check(check: str) -> str:
    """The report's error type for a checker's finding.

    The framework's own rule id decides it where there is one — FACT-* is a
    factual error whatever the check called itself, and the deterministic
    proper-noun check labels itself "Language" while carrying FACT-NAMES-005.
    Trusting the label over the rule id would file it under the wrong category.
    """
    text = str(check or "")
    match = _RULE_ID_RE.search(text)
    if match:
        rule = match.group(1)
        if rule.startswith("FACT-"):
            return "Factual"
        if rule.startswith("FLOW-"):
            return "Conceptual"
        if rule.startswith("LANG-"):
            return "Language clarity"
    head = text.split("(")[0].strip().lower()
    return _CHECK_HEAD_TO_TYPE.get(head, "Conceptual")


def _row_from_finding(finding: dict) -> dict:
    """One Error Report row from a non-AI check's finding.

    These come from the deterministic and structural checkers, which implement
    the framework's proper-noun consistency and sentence-clarity rules. They
    have no quoted statement of their own, so the location does that job — a
    reviewer needs to be able to find the thing on the page.
    """
    location = finding.get("location") or (
        f"Page {finding['page']}" if finding.get("page") else "")
    concern = str(finding.get("problem", "")).strip()
    caveat = str(finding.get("caveat", "")).strip()
    if caveat:
        concern = f"{concern} [{caveat}]"
    return {
        "original_statement": location or "(see the notes)",
        "error_type": _error_type_from_check(finding.get("check", "")),
        "severity": finding.get("severity", "Medium"),
        "error_concern": concern,
        "correct_information_correction": "",
        "source_urls": "",
    }


def _row_from_issue(issue) -> dict:
    """One Error Report row from an independent-AI-review issue.

    This is the mapping that was missing. The review runs the whole framework
    rule set and produces exactly what the report needs — the quoted text, the
    rule that fired, and a suggested correction — and every one of those rows
    used to be filed under "Format & Visual QC", where a reviewer reading the
    Error Report would never see it.
    """
    category = str(getattr(issue, "category", "") or "").strip().title()
    rule_id = str(getattr(issue, "rule_id", "") or "").strip()
    problem = str(getattr(issue, "problem", "") or "").strip()
    heading = str(getattr(issue, "heading", "") or "").strip()

    concern = problem
    if rule_id:
        concern = f"{concern} [{rule_id}]"
    if heading:
        concern = f"{concern} (under: {heading})"

    return {
        "original_statement": (str(getattr(issue, "original_text", "") or "").strip()
                               or heading or "(see the notes)"),
        "error_type": category or "Conceptual",
        "severity": _SEVERITY_BY_QC_SEVERITY.get(
            str(getattr(issue, "severity", "")).lower(), "Medium"),
        "error_concern": concern,
        "correct_information_correction": str(
            getattr(issue, "suggested_text", "") or "").strip(),
        "source_urls": "",
    }


def _merge_extra_rows(result: PdfQcResult, extra: list[dict]) -> None:
    """Add rows found by other stages to the report, without duplicating.

    Placeholder rows ("No reportable error found", "the check did not run") are
    statements ABOUT the report, so as soon as there is a real finding they are
    false and must go. Real rows are deduplicated against what is already there
    with the same key the chunk merge uses, because the review and the error
    report can legitimately notice the same mistake.
    """
    from ..error_report_writer import _PLACEHOLDER_TYPES

    existing = [r for r in (result.error_rows or [])
                if r.get("error_type") not in _PLACEHOLDER_TYPES]
    # Called with NO extra rows, this still dedupes what the report already
    # holds — the two finder passes quote the same mistake in different words,
    # and their repeats must collapse whether or not any other stage
    # contributed. Only an empty report is left alone, so a placeholder row
    # ("no issues", "check not run") is never wiped by a no-op merge.
    if not extra and not existing:
        return
    seen = {_row_key(r) for r in existing}
    for row in extra:
        key = _row_key(row)
        if key.strip("|") and key in seen:
            continue
        if key.strip("|"):
            seen.add(key)
        existing.append(row)

    # ONE dedup, the measured one. There used to be two passes here, and the
    # first — _collapse_same_mistake — quietly cost real findings. It grouped
    # rows by quoted statement and called two of them the same mistake when
    # their concerns shared any number at all, then kept whichever was more
    # severe, ties going to whichever arrived first. Measured on a real run:
    # "The report was released by NITI Aayog, not the Ministry of Finance, and
    # the target year is 2047" was collapsed into "The target year is 2047"
    # because both said 2047, and the wrong ministry left the report.
    #
    # Everything it was for — one mistake filed under two error types — is
    # already handled by _same_finding, which ignores the type, weighs the
    # complaint and the correction, and keeps rows apart when each names an
    # entity the other does not. That one is pinned by thirteen measured cases;
    # this one was pinned by none.
    deduped, removed = dedupe_rows(existing)
    if removed:
        result.duplicate_rows_removed = (
            getattr(result, "duplicate_rows_removed", 0) + removed)
    # Renumber is the writer's job; order is not, so the report reads in the
    # order things were found rather than by whichever stage produced them.
    result.error_rows = deduped


#: Statements that identify no particular sentence. Rows carrying one of these
#: must never be collapsed against each other — the text is a placeholder, not
#: evidence that two findings are about the same thing.
_GENERIC_STATEMENTS = frozenset({"(see the notes)", "", "see the notes"})

#: Numbers, years and article/section references. Two corrections that cite the
#: same one are almost always making the same point.
_ANCHOR_RE = re.compile(r"\b\d{2,4}\b")

#: Words too common in this domain to identify anything on their own.
_STOPWORDS = frozenset({
    "the", "and", "that", "this", "with", "from", "under", "which", "their",
    "there", "these", "those", "should", "would", "notes", "note", "state",
    "states", "india", "indian", "constitution", "article", "act", "report",
    "reports", "government", "national", "however", "instead", "according",
})






def _page_from(message: str) -> int | None:
    match = re.search(r"\bPage\s+(\d+)", message, re.I)
    return int(match.group(1)) if match else None


def _looks_like_dcn(document: PdfDocument) -> bool:
    """True when the PDF is a PW OnlyIAS Daily Class Note.

    Decided from the DCN palette (dark-green title, dark-red H1) and the
    reserved notes column's caption — the two things the DCN writer always
    draws. It matters because the two structural checkers disagree about one
    thing on purpose: the legacy checker wants a "Topics Covered in the Class"
    section, and the DCN checker requires its absence (the DCN layout has no
    contents page). Whichever format the file actually is, only that format's
    rule is applied.
    """
    palette = {DCN_COLOR_TITLE.upper(), DCN_COLOR_H1.upper()}
    for page in document.pages[:6]:
        for span in page.spans:
            if str(span.get("color", "")).upper() in palette:
                return True
        if "space for notes" in " ".join(page.text.lower().split()):
            return True
    return False


@dataclass
class _ErrorReportPass:
    """What the error-report call(s) produced, and what did not survive."""

    blocks: list[str] = field(default_factory=list)
    marker_found: bool = False
    total_parts: int = 0
    failed_parts: list[int] = field(default_factory=list)
    truncated_parts: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Pages that could not be rendered in image mode, so were never checked.
    unrendered_pages: list[int] = field(default_factory=list)
    #: Parts whose reply carried no ERROR_REPORT marker — the model answered,
    #: but not with a report. Neither a failure nor a result.
    unmarked_parts: list[int] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every part of the document was checked end to end."""
        return not (self.failed_parts or self.truncated_parts
                    or self.unrendered_pages or self.unmarked_parts)

    def coverage_warnings(self) -> list[str]:
        """Plain-language notes about any stretch of the document not checked.

        These exist so a partial report can never be mistaken for a clean one.
        """
        notes: list[str] = []
        if self.failed_parts:
            parts = ", ".join(str(p) for p in self.failed_parts)
            notes.append(
                f"INCOMPLETE COVERAGE: {len(self.failed_parts)} of "
                f"{self.total_parts} parts of this document could not be checked "
                f"(part {parts}). The errors listed are only those found in the "
                f"parts that succeeded — this is not a complete error report. "
                f"Reason: {'; '.join(self.errors[:2])}"
            )
        if self.unmarked_parts:
            parts = ", ".join(str(p) for p in self.unmarked_parts)
            notes.append(
                f"INCOMPLETE COVERAGE: part {parts} of {self.total_parts} came "
                f"back without an error-report block, so that stretch of the "
                f"document produced no findings at all. It was not checked — "
                f"this is not the same as it being clean."
            )
        if self.unrendered_pages:
            pages = ", ".join(str(p) for p in self.unrendered_pages[:12])
            more = "…" if len(self.unrendered_pages) > 12 else ""
            notes.append(
                f"INCOMPLETE COVERAGE: {len(self.unrendered_pages)} page(s) "
                f"({pages}{more}) could not be rendered, and this file's text "
                f"layer is unreadable, so those pages were never checked by any "
                f"content stage."
            )
        if self.truncated_parts:
            parts = ", ".join(str(p) for p in self.truncated_parts)
            notes.append(
                f"INCOMPLETE COVERAGE: the AI's reply for part {parts} of "
                f"{self.total_parts} hit the output limit and was cut off. Rows "
                f"written before the cut have been kept, but any error the model "
                f"had not yet written is missing. Re-run this file, or raise "
                f"ERROR_REPORT_MAX_OUTPUT_TOKENS."
            )
        return notes


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _strip_fences(block: str) -> str:
    """Remove ```json fences and any prose around the JSON.

    A fenced but otherwise perfect reply used to fail json.loads, fall through
    to salvage, and — because salvage having been needed is treated as proof of
    truncation — get the whole file labelled PARTIAL SCAN. A false incompleteness
    warning trains people to ignore the real ones.
    """
    text = _FENCE_RE.sub("", (block or "").strip())
    # Trim anything before the first bracket/brace and after the last, which is
    # where "Here is the report:" and trailing commentary live.
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    ends = [i for i in (text.rfind("]"), text.rfind("}")) if i >= 0]
    if starts and ends and max(ends) > min(starts):
        text = text[min(starts):max(ends) + 1]
    return text.strip()


def _salvage_rows(block: str, rejected: list[str] | None = None) -> list[dict]:
    """Every complete object in ``block``, even when the array never closed.

    A reply cut off at the output cap ends mid-array — sometimes mid-object.
    ``json.loads`` rejects the whole thing, and the obvious reading of that
    ("bad block, skip it") throws away every error the model *had* finished
    writing. For a document whose findings ran long, that is most of them.

    So the array is scanned object by object, tracking string state so a brace
    inside a quoted value is not mistaken for structure. Complete objects are
    kept; the trailing partial one is the only thing lost.
    """
    rows: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    # A wrapped reply — {"errors": [ {...}, {...} — nests the rows one level
    # down, where a depth-0 scan never sees them. Start at the array instead, so
    # a truncated wrapper yields its complete rows like a truncated array does.
    first_bracket = block.find("[")
    first_brace = block.find("{")
    if first_brace != -1 and first_bracket != -1 and first_brace < first_bracket:
        block = block[first_bracket:]

    for i, ch in enumerate(block):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(block[start:i + 1])
                    except (ValueError, TypeError):
                        # A complete-looking object json still rejects. Counted
                        # rather than dropped in silence, so the caller can say
                        # a row was lost instead of implying none existed.
                        if rejected is not None:
                            rejected.append(block[start:i + 1][:400])
                    else:
                        if isinstance(obj, dict):
                            rows.append(obj)
                    start = -1
    return rows


def _looks_like_row(value: object) -> bool:
    return isinstance(value, dict) and (
        "original_statement" in value or "error_concern" in value)


def _block_without_markers(text: str) -> tuple[str, bool]:
    """``(block, True)`` when a marker-less reply is nonetheless a report.

    The contract asks for markers around the array, and most replies carry
    them. Some carry a fenced JSON array and nothing else — the same rows,
    minus two lines of scaffolding. Treating that as "no report" lost a whole
    page window per run. Accepted only when the content is unmistakably the
    report: an array (or a wrapper around one) whose every element is a row,
    or a bare ``[]``. Prose, a stray object, a list of strings: still not a
    report.
    """
    candidate = _strip_fences(text)
    if not candidate or candidate[0] not in "[{":
        return "", False
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        # Cut off before the array closed — or bounded by prose that happens
        # to contain a bracket, since _strip_fences trims from the first "["
        # to the last "]". Either way, salvage what parses and hand back a
        # clean array. Returning the prose-bounded text instead made the
        # caller's json.loads fail a second time, and "salvage was needed" is
        # read as "the reply was cut off": a complete report was labelled a
        # PARTIAL SCAN.
        rows = _salvage_rows(candidate)
        if rows and all(_looks_like_row(r) for r in rows):
            return json.dumps(rows, ensure_ascii=False), True
        return "", False
    if isinstance(data, dict):
        for key in ("errors", "rows", "error_report", "problems", "findings"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if isinstance(data, list) and all(_looks_like_row(r) for r in data):
        # Re-serialised, not echoed: the caller parses this again, and the
        # unwrapped list is what it expects.
        return json.dumps(data, ensure_ascii=False), True
    return "", False


#: Statements that name no particular sentence. The structural checks use these
#: instead of a quote, so they are exempt from the quote check below.
_NO_QUOTE = ("(see the notes)", "see the notes", "")


def _words(text: object) -> list[str]:
    return [w for w in re.sub(r"[^\w\s]", " ", str(text or "").lower()).split() if w]


def _longest_run_on_page(quote: str, notes: str, cap: int = 12) -> int:
    """How many consecutive words of ``quote`` appear in ``notes``."""
    words = _words(quote)
    if not words:
        return 0
    for size in range(min(len(words), cap), 0, -1):
        for start in range(0, len(words) - size + 1):
            if " ".join(words[start:start + size]) in notes:
                return size
    return 0


def _quote_is_on_the_page(statement: str, notes: str) -> bool:
    """True when the row's quoted sentence can actually be found in the notes.

    A reviewer works by locating the quote on the page. A row quoting a
    sentence that is not there cannot be acted on at all, and it costs more
    trust than an ordinary wrong answer: the reader concludes the tool invents
    things. Measured on a real run, the independent review produced two such
    rows out of thirty-five — "The study was conducted by the Indian Institute
    of Science (IISc) in Mumbai" and "The Bill seek to streamline the
    functioning of tribunals". Neither sentence exists in that document; IISc,
    Mumbai and Bengaluru appear nowhere in it, and the notes say "which SEEKS
    to reform the administration of tribunals".

    The rule is a located run of at least six consecutive words, or the whole
    quote when it is shorter. The margin is wide: on that run the real rows
    matched 4 of 4, 5 of 5 and 9-to-12 of the rest, while the two invented ones
    matched 2 of 13 and 2 of 9.
    """
    text = " ".join(str(statement or "").split())
    if text.lower().strip() in _NO_QUOTE:
        return True
    words = _words(text)
    if len(words) < 4:
        return True          # too short to judge; a label, not a quotation
    return _longest_run_on_page(text, notes) >= min(6, len(words))


def _drop_invented_quotes(rows: list[dict], paged_text: str) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) — rows quoting a sentence that is not in the notes."""
    notes = " ".join(re.sub(r"[^\w\s]", " ", str(paged_text or "").lower()).split())
    if len(notes.split()) < 50:
        return list(rows), []        # nothing trustworthy to check against
    kept, dropped = [], []
    for row in rows:
        if _quote_is_on_the_page(row.get("original_statement", ""), notes):
            kept.append(row)
        else:
            dropped.append(row)
    return kept, dropped


def _row_key(row: dict) -> str:
    """What makes two error rows the same finding.

    Chunks overlap by CHUNK_OVERLAP_WORDS, so the same error genuinely does
    arrive twice and has to be collapsed. But the quoted sentence alone is too
    coarse a key: one sentence can carry several distinct errors — a wrong date
    *and* a wrong article number — and keying on the quote drops all but the
    first. The error type and the description of what is wrong are what
    separate them, so they are part of the key.
    """
    statement = " ".join(str(row.get("original_statement", "")).split()).lower()[:160]
    error_type = " ".join(str(row.get("error_type", "")).split()).lower()[:60]
    concern = " ".join(str(row.get("error_concern", "")).split()).lower()[:120]
    return f"{statement}|{error_type}|{concern}"


def _norm(text: object) -> str:
    """Lowercase, punctuation-free, single-spaced — for comparing two claims."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").lower()).split())


#: A single word inside straight or curly quotes — what a spelling or
#: terminology complaint is about.
_QUOTED_RE = re.compile(r"[\"'‘’“”](\w[\w-]*)[\"'‘’“”]")
#: A short quoted phrase, starting and ending on a word character so the text
#: BETWEEN two quoted phrases is never itself read as a phrase — the wording a
#: contradiction complaint points at.
_QUOTED_PHRASE_RE = re.compile(r"[\"‘’“”'](\w[^\"‘’“”']{1,58}\w)[\"‘’“”']")


def _shared_typo(a: dict, b: dict, a_stmt: str, b_stmt: str,
                 shared: set[str]) -> bool:
    """True when two LANGUAGE rows are about one misspelled word.

    The AI pass and the dictionary check both report a typo, quoting different
    spans around it: "The Gender Prity Index (GPI) is calculated..." against
    "...1.57 crore. The Gender Prity Index...". Half a sentence apart, one
    'Prity'.

    The shared word must be a word no dictionary knows — that is what makes it
    the TYPO rather than the subject. Without that test a tense complaint
    quoting 'was' would merge with any other language row whose sentence
    happens to contain "was".
    """
    if not shared:
        return False
    if not (str(a.get("error_type", "")).lower().startswith("lang")
            and str(b.get("error_type", "")).lower().startswith("lang")):
        return False
    try:
        from .spelling_check import _dictionary, _known

        sp = _dictionary()
        misspelled = {w for w in shared if not _known(sp, w)}
    except Exception:
        # No dictionary: fall back to "long and not a common word", which is
        # the property that made the test worth making in the first place.
        misspelled = {w for w in shared if len(w) >= 5 and w not in _STOPWORDS}
    return any(w in a_stmt and w in b_stmt for w in misspelled)


def same_sentence(a_stmt: str, b_stmt: str, threshold: float = 0.82) -> bool:
    """Whether two quoted spans are quoting the same sentence.

    Token overlap, not character similarity: two quotes of one sentence
    usually differ by a clause at one end. Not below 0.75 — at 0.6,
    Prambanan's "Inscribed as a UNESCO World Heritage Site in 1998" and the
    Taj Mahal's "Declared a UNESCO World Heritage Site (1999)" passed as one
    sentence, and two seeded errors became one row.
    """
    return (a_stmt in b_stmt or b_stmt in a_stmt
            or _overlap(a_stmt, b_stmt) >= 0.75
            or SequenceMatcher(None, a_stmt, b_stmt).ratio() >= threshold)


def _contradiction_from_both_ends(a_con_raw: str, b_con_raw: str,
                                  a_stmt: str, b_stmt: str) -> bool:
    """One contradiction reported twice, once from each side of it.

    Row A quotes the sentence saying "red-marble" and complains that the notes
    later say "carved from white marble"; row B quotes the white-marble
    sentence and complains about the earlier "red-marble". Different
    sentences, one finding.

    Two conditions keep this from swallowing unrelated rows. Each quoted
    phrase must be the OTHER location's wording — present in the other row's
    sentence and absent from this row's own — so a grammar row that quotes its
    own correction ("'launches' should be 'launched'") is not mistaken for a
    contradiction. And two DISTINCT phrases must cross, so two rows that both
    complain about one term ("Uttam-3" in a table and again in a ranking) stay
    two rows: that term is wrong in two places, and each place is its own fix.
    """
    a_phrases = {_norm(q) for q in _QUOTED_PHRASE_RE.findall(a_con_raw)}
    b_phrases = {_norm(q) for q in _QUOTED_PHRASE_RE.findall(b_con_raw)}
    a_hits = {p for p in a_phrases if len(p) >= 5 and p in b_stmt and p not in a_stmt}
    b_hits = {p for p in b_phrases if len(p) >= 5 and p in a_stmt and p not in b_stmt}
    return bool(a_hits) and bool(b_hits) and len(a_hits | b_hits) >= 2


def _same_finding(a: dict, b: dict, threshold: float = 0.82) -> bool:
    """True when two rows are the same error described in different words.

    Exact-key dedup only catches a verbatim repeat, and the model does not
    repeat itself verbatim: asked about overlapping windows and through
    different lenses, it quotes a different span and rephrases the complaint.

    The rules below were fixed by measuring thirteen row pairs taken from real
    runs — six that had to merge, seven that had to stay apart — and finding
    what actually separates them. Where no signal separates two cases, this
    function keeps them apart: a duplicate row costs a reviewer a few seconds,
    and a swallowed row costs them the error.

    In order:

    1. Two complaints that quote words and share none are two findings. This
       is the strongest signal there is — "'remves' should be 'removes'" and
       "'exsting' should be 'existing'" sit in one sentence and share every
       other word.
    2. Two LANGUAGE rows about one misspelled word are one finding, whatever
       spans they quote (see :func:`_shared_typo`).
    3. Rows about different sentences are separate, unless they are the two
       ends of one contradiction (see :func:`_contradiction_from_both_ends`).
    4. Rows about the same sentence are the same finding when their complaints
       agree — measured, that is containment or 0.65 overlap. Below that they
       are two faults in one sentence, which is common: a wrong century AND a
       wrong dynasty in the same line.
    5. Failing that, the same sentence and the same proposed correction is one
       finding phrased two ways.
    """
    a_stmt, b_stmt = _norm(a.get("original_statement")), _norm(b.get("original_statement"))
    if not a_stmt or not b_stmt:
        return False

    a_con_raw = str(a.get("error_concern", "") or "")
    b_con_raw = str(b.get("error_concern", "") or "")
    a_quoted = {q.lower() for q in _QUOTED_RE.findall(a_con_raw)}
    b_quoted = {q.lower() for q in _QUOTED_RE.findall(b_con_raw)}
    a_con, b_con = _norm(a_con_raw), _norm(b_con_raw)
    concerns_agree = bool(a_con) and bool(b_con) and (
        a_con in b_con or b_con in a_con or _overlap(a_con, b_con) >= 0.65)

    # 1. Different quoted words, different findings — unless the two
    # complaints plainly say the same thing about the same sentence. Measured:
    # two rows calling "MSMEs Enterprises" redundant quote different
    # incidental words ('E' and 'Enterprises' against 'MSMEs') yet agree 0.75,
    # while two spelling rows quoting different typos agree only 0.60.
    if a_quoted and b_quoted and not (a_quoted & b_quoted):
        return same_sentence(a_stmt, b_stmt, threshold) and concerns_agree

    # 2. One typo, two spans.
    if _shared_typo(a, b, a_stmt, b_stmt, a_quoted & b_quoted):
        return True

    # 3. Same sentence? Token overlap, not character similarity: two quotes of
    # one sentence usually differ by a clause at one end. Not below 0.75 — at
    # 0.6, Prambanan's "Inscribed as a UNESCO World Heritage Site in 1998" and
    # the Taj Mahal's "Declared a UNESCO World Heritage Site (1999)" passed as
    # one sentence, and two seeded errors became one row.
    if not same_sentence(a_stmt, b_stmt, threshold):
        # One finding quoted from two different spans. The error-report pass
        # and the independent review read the same page and pick different
        # sentences to quote — "Indra Sawhney Case (1990): Introduced the
        # Creamy Layer doctrine" against "The Indra Sawhney (1990) judgment
        # held that…" — and the report then said the same thing three times.
        #
        # What identifies the finding is the COMPLAINT, not the span: both say
        # the judgment was 1992 and not 1990, agreeing 0.80 and citing the same
        # two years. Both halves are required. The years alone would merge
        # unrelated rows, and the wording alone would merge Prambanan's UNESCO
        # year with the Taj Mahal's, which read almost identically and agree
        # 0.62 — below the line this rule needs.
        if concerns_agree and _share_anchors(a_con, b_con):
            return True
        return _contradiction_from_both_ends(a_con_raw, b_con_raw, a_stmt, b_stmt)

    if not a_con or not b_con:
        # Nothing to tell them apart by. They quote one sentence; treat them
        # as one finding rather than printing the same quote twice.
        return True

    # 4. Complaints that plainly agree are one finding, whatever corrections
    # they carry: two rows may both say "Prambanan is in Indonesia, not
    # Malaysia" and answer it with "Indonesia" and "Central Java, near
    # Yogyakarta". Measured, plain agreement is containment or 0.65 overlap.
    if concerns_agree:
        return True

    # 4b. The same sentence, and both complaints dispute the SAME value in it.
    # Two checkers correcting "70x" — one to "7.2 to 9.7 times", the other to
    # "eight times" — have found one error and disagree about the right
    # number, which is not the same thing as finding two errors. Their
    # corrections share almost nothing, so the rule below would call them two
    # faults; what settles it is that both quarrel with the same figure.
    #
    # The disputed value has to come from the QUOTED SENTENCE, which is what
    # keeps the wrong century and the wrong dynasty in one Prambanan line
    # apart: only one of those complaints cites a number off the page.
    if a_stmt == b_stmt:
        in_statement = set(_ANCHOR_RE.findall(a_stmt))
        disputed = (in_statement & set(_ANCHOR_RE.findall(a_con))
                    & set(_ANCHOR_RE.findall(b_con)))
        if disputed:
            return True

    a_fix = _norm(a.get("correct_information_correction"))
    b_fix = _norm(b.get("correct_information_correction"))
    fixes_overlap = _overlap(a_fix, b_fix) if (a_fix and b_fix) else None

    # 5. Below that, the corrections decide. Two corrections that share
    # NOTHING are two faults in one sentence: "9th century" against "Sanjaya
    # Dynasty", "Kushana dynasty" against "78 CE". Each is its own row.
    #
    # Sharing is judged on substance, not on wording. "Recently, NITI Aayog
    # has released the report…" and "The report was released by NITI Aayog"
    # overlap 0.41 as token sets — mostly filler — and were split into two
    # rows although both name the same body. What they share is the answer:
    # NITI Aayog.
    if fixes_overlap is not None and fixes_overlap < 0.5:
        if not _share_substance(a_fix, b_fix):
            return False

    # 6. Complaints that partly agree, corrections that do not disagree — one
    # of them is usually absent. "Wrong volcano" and "the volcano named here
    # is not the one that damaged the complex" quote one sentence, and only
    # the first says it should be Merapi.
    #
    # This is the weakest route to a merge, so it carries the one guard that
    # matters: if each row names an organisation, scheme or place the other
    # does not, they are about different things. Measured, a row saying the
    # caregiving report "was released by NITI Aayog, not the Ministry of
    # Finance" merged here with one about the report's target YEAR, and the
    # ministry error left the report with it.
    if _overlap(a_con, b_con) >= 0.4 and not _each_names_its_own_entity(a, b):
        return True

    # 7. Same sentence and the same proposed fix. Two reviewers who disagree
    # about how to phrase the complaint but agree the answer is "Indonesia"
    # have found one error, not two.
    #
    # The entity guard applies here too, and this is where it earns its keep:
    # when both corrections rewrite the WHOLE sentence they overlap almost
    # completely — 0.88 for the pair above — while differing in the one word
    # that matters. Textual agreement is not agreement about the fix.
    return (fixes_overlap is not None and fixes_overlap >= 0.6
            and not _each_names_its_own_entity(a, b))


def _overlap(a: str, b: str) -> float:
    """Shared words as a share of the shorter side. 1.0 = fully contained."""
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / min(len(aw), len(bw))


#: A named thing: a multi-word proper noun ("NITI Aayog", "Government of
#: India"), an acronym, or a year. These are what a row is ABOUT, as distinct
#: from the prose around them.
_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z]{2,}(?:\s+(?:of\s+|and\s+)?[A-Z][A-Za-z]{2,})+"
    r"|[A-Z]{2,}[A-Za-z]*"
    # Any figure, not just a year: "70x", "8.4", "2,000", "26.8". A complaint
    # about a wrong number is ABOUT that number, and _richer must be able to
    # see that the row quoting both the wrong figure and the right one says
    # more than the row quoting only the right one.
    r"|\d[\d,.]*\s*(?:x|%|times)?)\b")

#: Capitalised words that start sentences or label the report rather than
#: naming anything.
_SCAFFOLD = frozenset({
    "page", "context", "the", "this", "that", "recently", "note", "notes",
    "about", "key", "more", "under", "both", "however", "wrong", "text",
    "spelling", "however", "instead", "according", "source", "correct",
})


def _entities(row: dict) -> set[str]:
    """The named things a row mentions.

    Read field by field, never from the two fields joined: concatenating
    "…is in Indonesia, not Malaysia" with the correction "Indonesia" put those
    last two words next to each other and invented the entity "Malaysia
    Indonesia", which then differed from every other row's invention and blocked
    merges that should have happened.
    """
    out: set[str] = set()
    for key in ("error_concern", "correct_information_correction"):
        for match in _ENTITY_RE.findall(str(row.get(key, "") or "")):
            name = " ".join(match.split()).lower()
            if name and not all(word in _SCAFFOLD for word in name.split()):
                out.add(name)
    return out


def _share_substance(a: str, b: str) -> bool:
    """True when two corrections propose the same answer in different words.

    Token overlap alone cannot tell "Recently, NITI Aayog has released the
    report…" from "The report was released by NITI Aayog": both are mostly
    filler, and they score 0.41. What matters is whether the same ANSWER is in
    both — a shared figure, or a shared substantive word.
    """
    if not a or not b:
        return False
    if set(_ANCHOR_RE.findall(a)) & set(_ANCHOR_RE.findall(b)):
        return True
    def substance(text: str) -> set[str]:
        return {w for w in text.split() if len(w) >= 4 and w not in _STOPWORDS}
    return bool(substance(a) & substance(b))


def _share_anchors(a_con: str, b_con: str) -> bool:
    """True when two complaints cite at least one number in common.

    Both must cite something: two complaints with no numbers between them are
    not evidence of anything, and treating them as agreement would merge on
    wording alone.
    """
    a_anchors = set(_ANCHOR_RE.findall(a_con))
    b_anchors = set(_ANCHOR_RE.findall(b_con))
    return bool(a_anchors and b_anchors and (a_anchors & b_anchors))


def _each_names_its_own_entity(a: dict, b: dict) -> bool:
    """True when each row names something the other never mentions.

    Two complaints about one sentence that name different organisations,
    schemes or places are about different things, however much prose they
    share. Measured: {NITI Aayog} against {Government of India} — two faults;
    the two Tibet paraphrases name nothing exclusive to either — one fault.
    """
    a_names, b_names = _entities(a), _entities(b)
    return bool(a_names - b_names) and bool(b_names - a_names)


def _facts(row: dict) -> set[str]:
    """Everything a row names, for deciding which of two duplicates to keep."""
    return _entities(row)


def _richer(a: dict, b: dict) -> dict:
    """Whichever of two duplicate rows tells a reviewer more.

    Length is a poor measure of that, and it cost a real finding: one row said
    the caregiving report "was released by NITI Aayog, not the Ministry of
    Finance, and the target year is 2047", another said only that the year
    should be 2047. Both quoted the same sentence and both ran past the length
    cap, so the tie fell to whichever arrived first — and the row naming NITI
    Aayog lost, taking the ministry error out of the report with it.

    So the distinct FACTS a row names — the proper nouns, years and figures in
    its complaint and its correction — are counted first. A row that names
    everything the other names, and more, always wins.
    """
    a_facts, b_facts = _facts(a), _facts(b)
    if a_facts > b_facts:        # strict superset: it says everything b does
        return a
    if b_facts > a_facts:
        return b

    def score(r: dict, facts: set[str]) -> int:
        fix = str(r.get("correct_information_correction", "")).strip()
        # Having a correction at all outweighs any amount of prose about the
        # problem: "it is Indonesia" is what a reviewer acts on, and a row
        # without it sends them to look the answer up themselves.
        return ((200 if fix else 0)
                + 10 * len(facts)
                + min(len(fix), 120)
                + min(len(str(r.get("error_concern", ""))), 120)
                + (40 if str(r.get("severity")) == "High" else 0)
                + (30 if str(r.get("source_urls") or "").strip() else 0))
    return a if score(a, a_facts) >= score(b, b_facts) else b


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse near-duplicate findings. Returns (kept, removed_count).

    Quadratic, deliberately: a document produces tens of rows, not thousands,
    and an index that could miss a match is worse here than the time saved.
    """
    kept: list[dict] = []
    removed = 0
    for row in rows:
        for i, existing in enumerate(kept):
            if _same_finding(existing, row):
                kept[i] = _richer(existing, row)
                removed += 1
                break
        else:
            kept.append(row)
    return kept, removed


def _merge_error_blocks(blocks: list[str]) -> tuple[str, int, int]:
    """One JSON array from the per-part arrays, duplicates removed.

    Returns ``(json, salvaged_count, unusable_blocks)``.

    ``salvaged_count`` is how many rows were recovered from a block that did not
    parse — it means a part was cut off and the rows after the cut do not exist.

    ``unusable_blocks`` counts blocks that neither parsed NOR yielded a single
    salvageable row. That number is what separates "the model found nothing" from
    "the model's answer was unreadable", and the caller must keep them apart:
    reporting a garbled response as a clean document is the exact failure this
    whole pass exists to prevent.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    salvaged = 0
    unusable = 0

    for block in blocks:
        block = _strip_fences(block)
        data = None
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            data = None

        # Models routinely wrap the array in an object: {"errors": [...]}.
        # Reaching in for the array is the difference between reading the rows
        # and salvaging one contentless row out of the wrapper.
        if isinstance(data, dict):
            for key in ("errors", "rows", "error_report", "problems", "findings"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break

        if isinstance(data, list):
            candidates = [r for r in data if isinstance(r, dict)]
        else:
            # Either not JSON at all or a truncated array. Recover what is
            # there rather than discarding the part wholesale.
            candidates = _salvage_rows(block)
            if candidates:
                salvaged += len(candidates)
            elif block.strip():
                unusable += 1

        for row in candidates:
            key = _row_key(row)
            if key.strip("|") and key in seen:
                continue
            if key.strip("|"):
                seen.add(key)
            rows.append(row)

    return json.dumps(rows, ensure_ascii=False, indent=2), salvaged, unusable


@dataclass
class _Chunk:
    """One error-report call's worth of the document."""

    text: str = ""
    page_range: str = ""
    images: list[Path] = field(default_factory=list)

    @property
    def from_images(self) -> bool:
        return bool(self.images)


_PAGE_MARKER_RE = re.compile(r"(?=\[Page \d+\])")


def _split_chunk(chunk: "_Chunk") -> list["_Chunk"]:
    """One chunk per page, or [] when the chunk is a single page already.

    Used only to recover a reply that hit the output cap. The cap is per CALL,
    so halving the content a call has to cover doubles the room its answer has
    — which is the whole reason a smaller window finds MORE, not less.
    """
    if chunk.from_images or not chunk.text.strip():
        return []
    parts = [part for part in _PAGE_MARKER_RE.split(chunk.text) if part.strip()]
    if len(parts) < 2:
        return []
    out: list[_Chunk] = []
    for part in parts:
        match = re.match(r"\[Page (\d+)\]", part.lstrip())
        out.append(_Chunk(text=part.strip(),
                          page_range=match.group(1) if match else chunk.page_range))
    return out


def _chunk_pages(document: PdfDocument, *, paged_text: str = "",
                 text_is_readable: bool = True,
                 unrendered: list[int] | None = None) -> list[_Chunk]:
    """The document split into error-report calls.

    Text mode (the normal one) uses the note-generation pipeline's budgets: one
    call below MAX_SINGLE_CALL_TRANSCRIPT_WORDS, otherwise CHUNK_TRANSCRIPT_WORDS
    per part with CHUNK_OVERLAP_WORDS of overlap. The split lands on a page
    boundary rather than mid-sentence, because a page is the unit a reviewer
    locates an error by, and half a page is a statement without its subject.

    Image mode is used when src/pdf/text_integrity found the text layer
    unreadable. Pages then travel as rendered images, PAGES_PER_IMAGE_CALL at a
    time — the batch size the visual check already uses, and one the proxy's
    body budget carries comfortably.
    """
    paged_text = paged_text or document.paged_text
    if not text_is_readable:
        chunks: list[_Chunk] = []
        with_images = [p for p in document.pages if p.image_path]
        # Pages whose render failed cannot travel as images, and in this mode
        # there is no readable text to fall back on — so they are genuinely
        # unchecked. The caller is told, rather than the run quietly covering
        # fewer pages than the document has.
        if unrendered is not None:
            unrendered.extend(p.page_no for p in document.pages if not p.image_path)
        for start in range(0, len(with_images), PAGES_PER_IMAGE_CALL):
            group = with_images[start:start + PAGES_PER_IMAGE_CALL]
            chunks.append(_Chunk(
                page_range=f"{group[0].page_no}-{group[-1].page_no}",
                images=[p.image_path for p in group if p.image_path],
            ))
        if chunks:
            return chunks
        # Nothing rendered: fall through and send the text, corrupt as it is,
        # rather than producing no report at all.

    # A SMALL, FIXED NUMBER OF PAGES PER CALL. This is the single biggest lever
    # on recall, and getting it wrong is what made the tool miss most of a
    # document.
    #
    # The old rule was "if it fits in one call, send it in one call", with a
    # 6,000-word ceiling. A 19-page set of notes is about 4,700 words, so the
    # whole thing went to the model as ONE request — and a model asked to audit
    # nineteen pages in a single answer does not audit nineteen pages. It reads
    # the beginning closely, skims, and returns a plausible-length list. The
    # graded trial that produced 21 of 40 errors ran exactly this way, and the
    # reviewer's verdict was "there are pages and pages not considered".
    #
    # Word budgets are the wrong unit for the same reason: they were designed to
    # keep requests small enough to succeed, not to keep attention high enough
    # to be thorough. Pages are the unit a reviewer checks and the unit the model
    # has to be held to, so the split is by pages and the word budget survives
    # only as a safety valve for unusually dense pages.
    pages_all = list(document.pages)
    if not pages_all:
        return [_Chunk(text=paged_text, page_range="")]

    per_call = max(1, PAGES_PER_ERROR_REPORT_CALL)
    groups: list[list] = []
    current: list = []
    words = 0
    for page in pages_all:
        current.append(page)
        words += len(page.text.split())
        # Close the group on the page count, or early if these pages are dense
        # enough to crowd the model's attention on their own.
        if len(current) >= per_call or words >= CHUNK_TRANSCRIPT_WORDS:
            groups.append(current)
            current, words = [], 0
    if current:
        groups.append(current)

    out: list[_Chunk] = []
    for group in groups:
        body = "\n\n".join(f"[Page {p.page_no}]\n{p.text}".rstrip() for p in group)
        out.append(_Chunk(text=body,
                          page_range=f"{group[0].page_no}-{group[-1].page_no}"))

    # Full coverage is the contract, so it is checked rather than assumed.
    emitted = {p.page_no for g in groups for p in g}
    missed = [p for p in pages_all if p.page_no not in emitted]
    if missed:
        body = "\n\n".join(f"[Page {p.page_no}]\n{p.text}".rstrip() for p in missed)
        out.append(_Chunk(text=body,
                          page_range=f"{missed[0].page_no}-{missed[-1].page_no}"))
    return out


def _chunk_pages_legacy(document: PdfDocument, *, paged_text: str = "",
                        text_is_readable: bool = True) -> list[_Chunk]:
    """The previous word-budget split. Kept only so the old behaviour is
    documented next to the new one; nothing calls it."""
    groups: list[list] = []
    current: list = []
    words = 0
    overlap_pages = max(1, CHUNK_OVERLAP_WORDS // 200)  # ~200 words to a notes page

    emitted: set[int] = set()
    for page in document.pages:
        current.append(page)
        words += len(page.text.split())
        if words >= CHUNK_TRANSCRIPT_WORDS:
            groups.append(current)
            emitted.update(p.page_no for p in current)
            current = current[-overlap_pages:] if len(current) > overlap_pages else []
            words = sum(len(p.text.split()) for p in current)
    # Emit the tail whenever it holds a page no group has carried yet. The old
    # test was `len(current) > overlap_pages`, which asked how BIG the tail was
    # instead of whether it was NEW: a single page left over after a large page
    # flushed on its own was silently never sent to the model at all.
    if current and any(p.page_no not in emitted for p in current):
        groups.append(current)
        emitted.update(p.page_no for p in current)

    # Full coverage is the contract, so it is checked rather than assumed.
    missed = [p.page_no for p in document.pages if p.page_no not in emitted]
    if missed:
        groups.append([p for p in document.pages if p.page_no in set(missed)])

    out: list[_Chunk] = []
    for group in groups:
        body = "\n\n".join(f"[Page {p.page_no}]\n{p.text}".rstrip() for p in group)
        out.append(_Chunk(text=body,
                          page_range=f"{group[0].page_no}-{group[-1].page_no}"))
    return out


def run_pdf_qc(
    pdf_path: Path,
    *,
    run_dir: Path,
    client,                      # GeminiClient | None (None = no AI stages)
    google_token: str = "",
    file_name: str = "",
    strictness: str = DEFAULT_ERROR_REPORT_STRICTNESS,
    stage_callback=None,
    origin=None,
) -> PdfQcResult:
    """Run every QC stage over one PDF and return its result.

    Always returns a PdfQcResult, even when a stage or the whole file fails: the
    caller is processing a batch and a file that could not be checked has to be
    reportable beside the ones that could.

    ``origin`` is the :class:`~src.input_normalize.NormalizedInput` describing
    what the user actually uploaded. When that was not a PDF, this PDF is a
    conversion of it, and the stages that measure the PW notes template are
    skipped rather than run against geometry the template never produced.
    """
    pdf_path = Path(pdf_path)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    result = PdfQcResult(file_name=file_name or pdf_path.name, run_dir=run_dir)

    # ------------------------------------------------------------------ read
    _stage_cb(stage_callback, "Reading the PDF")
    try:
        document = extract_pdf(pdf_path, run_dir)
    except Exception as e:
        result.status = "failed"
        # The message names the file the user uploaded, never the temporary copy
        # this app made of it — an internal run path in a report reads as a bug.
        # PyMuPDF escapes the backslashes in its message, so both spellings of
        # the path are substituted out.
        detail = str(e)
        for spelling in (str(pdf_path), str(pdf_path).replace("\\", "\\\\")):
            detail = detail.replace(spelling, result.file_name)
        result.error = f"The PDF could not be opened: {detail}"
        result.stages["read"] = "failed"
        _write_artifacts(run_dir, result)
        return result

    # Error Report rows contributed by stages other than the error-report call
    # itself: the independent AI review, and the deterministic/structural checks
    # that implement the framework's language and consistency rules. They are
    # merged into the report before verification, so every claim — whatever
    # found it — is refuted before a reviewer sees it.
    review_rows: list[dict] = []

    result.page_count = document.page_count
    result.word_count = document.word_count
    result.language_code = document.language_code
    result.warnings.extend(document.warnings)
    result.stages["read"] = "completed"

    # Same detector the note-generation app uses to pick a subject when the user
    # did not name one. It only ever labels the run — the QC rules are the same
    # for every subject — so a wrong guess costs nothing but a line in the log.
    try:
        from ..subject_detect import detect_subject

        result.subject = detect_subject(filename=result.file_name,
                                        slide_text=document.text[:20000])
    except Exception:
        result.subject = ""

    if document.encrypted or document.page_count == 0:
        result.status = "failed"
        result.error = (document.warnings[0] if document.warnings
                        else "The PDF has no pages.")
        _write_artifacts(run_dir, result)
        return result

    is_dcn = _looks_like_dcn(document)
    reconstruction = reconstruct_marker_text(document)
    result.lecture_title = reconstruction.lecture_title
    (run_dir / "reconstructed_notes.txt").write_text(
        reconstruction.marker_text, encoding="utf-8")

    # ------------------------------------------ gate before every paid stage
    # Same fail-closed gate as the note-generation engine: the proxy is
    # re-checked before the first paid call, and a denial or proxy error skips
    # every AI stage without destroying what the free stages already found. It
    # runs here, before anything else, because the very first stage may need the
    # model: a PDF whose text layer cannot be read has to be read back from its
    # pages before the text checks can run at all.
    ai_available = client is not None
    gate_detail = ""
    from ..proxy_session import is_restored_pass

    # A restored 7-day pass was authorised when it was minted, and there is no
    # Google token behind it to re-check. Re-checking the sentinel is what made
    # every --session run skip its AI stages.
    if ai_available and google_token and not is_restored_pass(google_token):
        try:
            if not pw_access.check_allowed(google_token):
                ai_available = False
                gate_detail = "proxy denied access before the QC review (fail closed)"
        except Exception as e:
            ai_available = False
            gate_detail = f"proxy gate errored (fail closed): {e}"
    elif not ai_available:
        gate_detail = "no AI client"
    if not ai_available and gate_detail:
        result.warnings.append(f"AI quality stages skipped: {gate_detail}")

    # ------------------------------------------------------ 0. text recovery
    # Five of the nine stages read the notes as text. When the PDF's own text
    # layer is scrambled (see src/pdf/text_integrity.py) they would all have to
    # be discounted — so the pages are read back by the model instead, and the
    # result is validated before anything is allowed to use it. A recovery that
    # works restores every stage to full strength; one that does not leaves the
    # app exactly where it was before this stage existed.
    text_ok = document.text_layer_reliable
    notes_text = document.text
    paged_text = document.paged_text
    marker_text = reconstruction.marker_text
    recovery = None
    stage = "text_recovery"

    if text_ok:
        result.stages[stage] = "not_needed"
    elif not ai_available:
        result.stages[stage] = "unavailable"
    else:
        _stage_cb(stage_callback, "Reading the pages")
        try:
            from ..pdf.text_recovery import recover_text

            recovery = recover_text(client, document, run_dir,
                                    max_workers=MAX_PARALLEL_AI_CALLS,
                                    stage_callback=stage_callback)
            result.warnings.extend(recovery.warnings)
            if recovery.ok:
                notes_text = recovery.plain_text
                paged_text = recovery.paged_text
                marker_text = recovery.marker_text
                result.word_count = len(notes_text.split())
                text_ok = True
                result.stages[stage] = "completed"
                (run_dir / "recovered_marker_notes.txt").write_text(
                    marker_text, encoding="utf-8")
            else:
                result.stages[stage] = "failed"
                result.warnings.append(
                    "The pages could not be read back reliably "
                    f"({recovery.reason}), so the text checks below are marked "
                    "as unreliable and the factual check reads the pages as images.")
        except Exception as e:
            result.stages[stage] = "failed"
            result.warnings.append(f"Reading the pages back failed: {e}")

    if not document.text_layer_reliable:
        recovered_ok = recovery is not None and recovery.ok
        result.warnings.append(document.integrity.reason)
        result.qc_findings.append(_finding(
            "integrity", "Text layer", "Medium",
            "This PDF's copyable text is scrambled: " + document.integrity.reason +
            (" The pages were read back from their images for this check, so the "
             "report below is unaffected — but copy-paste out of this PDF, and any "
             "search inside it, will not work for a student."
             if recovered_ok else
             " The pages themselves are checked from their images instead, so the "
             "content report is unaffected — but copy-paste out of this PDF, and any "
             "search inside it, will not work for a student.")))

    # ------------------------------------------------------ 1. file integrity
    stage = "integrity"
    try:
        local = run_local_output_checks(None, pdf_path)
        problems = [p for p in local.get("problems", [])
                    if not p.startswith(_DOCX_ONLY_PREFIXES)]
        for problem in problems:
            result.qc_findings.append(_finding(
                stage, "PDF integrity", "High", problem))
        result.stages[stage] = "completed" if not problems else "completed_with_warnings"
        write_json(run_dir / "local_output_check.json",
                   {**local, "problems": problems})
    except Exception as e:
        result.stages[stage] = "failed"
        result.warnings.append(f"PDF integrity check failed: {e}")

    # --------------------------------------------------------- 2. page layout
    # Only meaningful for a PDF laid out from the PW notes template. A converted
    # Word file or an image has no 5.5in content column and no header logo, so
    # measuring for them would report the source format as a defect.
    stage = "layout"
    if origin is not None and not getattr(origin, "template_checks_apply", True):
        result.stages[stage] = "not_applicable"
        result.warnings.append(
            f"Page-layout check not run: the file was supplied as "
            f"{origin.origin_label} ({origin.origin_ext}), so the notes-template "
            f"geometry does not apply to it."
        )
    else:
        _stage_cb(stage_callback, "Checking page layout")
        try:
            check = verify_pdf_layout(pdf_path, run_dir / "layout_check",
                                      max_pages=LAYOUT_RENDER_PAGES)
            for warning in check.warnings:
                page = _page_from(warning)
                severity = "High" if page and "runs into the notes-box" in warning else "Medium"
                result.qc_findings.append(_finding(
                    stage, "Content overflow", severity, warning, page=page))
            result.stages[stage] = "completed" if check.ok else "completed_with_warnings"
            write_json(run_dir / "layout_check.json", {
                "ok": check.ok, "page_count": check.page_count,
                "overlap_pages": check.overlap_pages, "warnings": check.warnings,
            })
        except Exception as e:
            result.stages[stage] = "failed"
            result.warnings.append(f"Layout check failed: {e}")

    # ------------------------------------------------------- 3. page analysis
    stage = "page_analysis"
    try:
        page_findings = run_page_analysis(document)
        for item in page_findings:
            result.qc_findings.append(_finding(
                stage, item["check"], item["severity"], item["problem"],
                page=item.get("page"), location=item.get("location", "")))
        result.stages[stage] = ("completed" if not page_findings
                                else "completed_with_warnings")
        write_json(run_dir / "page_analysis.json", page_findings)
    except Exception as e:
        result.stages[stage] = "failed"
        result.warnings.append(f"Page analysis failed: {e}")

    # ------------------------------------------- 4. deterministic notes checks
    _stage_cb(stage_callback, "Running structural checks")
    stage = "deterministic"
    try:
        from .deterministic_checks import run_deterministic_checks

        slides = document.as_slides()
        issues = run_deterministic_checks(notes_text, slides=slides, enrichments=None)
        for issue in issues:
            result.qc_findings.append(_finding(
                stage, f"{issue.category.title()} ({issue.rule_id})",
                _SEVERITY_BY_QC_SEVERITY.get(issue.severity, "Medium"),
                issue.problem,
                location=issue.heading or ""))
        # Typos a dictionary can prove — dropped or transposed letters in a
        # word the notes otherwise use correctly. The AI passes report these
        # too, and so far always have; this makes "always" a guarantee rather
        # than a streak, at no cost. Skipped over a scrambled text layer,
        # where every word would look misspelled.
        misspellings = []
        if document.text_layer_reliable:
            from .spelling_check import find_misspellings

            misspellings = find_misspellings(
                [(p.page_no, p.text) for p in document.pages],
                language_code=document.language_code)
            for miss in misspellings:
                result.qc_findings.append(_finding(
                    stage, "Language (LANG-SPELLING-001)", "Low",
                    f"Spelling: '{miss.word}' should be '{miss.suggestion}'.",
                    page=miss.page, location=miss.context))
        result.stages[stage] = ("completed" if not (issues or misspellings)
                                else "completed_with_warnings")
        write_json(run_dir / "deterministic_qc.json",
                   [i.__dict__ for i in issues]
                   + [{"rule_id": "LANG-SPELLING-001", "page": m.page,
                       "word": m.word, "suggestion": m.suggestion,
                       "context": m.context} for m in misspellings])
    except Exception as e:
        result.stages[stage] = "failed"
        result.warnings.append(f"Deterministic checks failed: {e}")

    # -------------------------------------------------- 5. DCN structure check
    stage = "structure"
    try:
        report = check_dcn_notes(marker_text, min_words=MIN_NOTES_WORDS)
        buckets = (
            ("Format", "High", report.format_warnings),
            ("Factual risk", "Medium", report.factual_risk_points),
            ("Layout", "Medium", report.layout_warnings),
        )
        found = False
        for check_name, severity, items in buckets:
            for message in items:
                found = True
                where = locate(message, recovery if (recovery and recovery.ok)
                               else reconstruction)
                result.qc_findings.append(_finding(
                    stage, check_name, severity, message,
                    page=int(where.split()[-1].split("-")[0]) if where.startswith("Page ") else None,
                    location=where))
        if not (recovery and recovery.ok):
            # These say what the *geometric* reconstruction could not recover —
            # flowchart zones above all, since arrows in body text are
            # indistinguishable from prose to anything that cannot read the
            # page. When the pages were read back instead, they were recovered,
            # so the note would be false.
            for note in reconstruction.not_applicable:
                result.qc_findings.append(_finding(
                    stage, "Not applicable to a PDF", "Low", note))
        result.stages[stage] = "completed" if not found else "completed_with_warnings"
        (run_dir / "structure_report.md").write_text(
            report.to_markdown(), encoding="utf-8")
    except Exception as e:
        result.stages[stage] = "failed"
        result.warnings.append(f"Structure check failed: {e}")

    # ------------------------------------------------------ 6. legacy checks
    stage = "legacy_checks"
    try:
        warnings = quality_check(notes_text, document.as_slides(),
                                 None, pdf_path, send_images_to_ai=True)
        kept = [w for w in warnings if not w.startswith(_DOCX_ONLY_WARNINGS)]
        if is_dcn:
            # The DCN layout has no contents section by design, and the DCN
            # checker above enforces its absence. Applying the legacy rule too
            # would report every correct DCN note as missing a section it must
            # not have.
            kept = [w for w in kept if "Topics Covered" not in w]
        for warning in kept:
            result.qc_findings.append(_finding(
                stage, "Content completeness", "Medium", warning))
        result.stages[stage] = "completed" if not kept else "completed_with_warnings"
    except Exception as e:
        result.stages[stage] = "failed"
        result.warnings.append(f"Content completeness check failed: {e}")

    # -------------------------------------------------- 7. visual check (off)
    # Removed deliberately. This app reports what is WRONG IN THE NOTES; how the
    # page is drawn is a typesetting matter for a different tool. Cutting it also
    # removes an AI call per batch of pages, which pays for the deeper content
    # checking that replaced it.
    result.stages["visual"] = "not_applicable"

    # ------------------------------------------------- 8. independent review
    _stage_cb(stage_callback, "Reviewing concepts and language")
    stage = "ai_review"
    if not ai_available:
        result.stages[stage] = "unavailable"
    elif not text_ok:
        # The review reads the notes as text and reports language and clarity
        # problems against exact quotes. Over a scrambled text layer it would
        # report the font's artefacts as the writer's mistakes, in volume. This
        # only happens when the recovery stage above could not read the pages
        # back either; the factual check still runs, from the images.
        result.stages[stage] = "unavailable"
        result.warnings.append(
            "The independent language/concept review was skipped for this file: it "
            "reads the notes as text, this PDF's text layer is scrambled, and the "
            "pages could not be read back reliably. The factual error report still "
            "ran, from the page images.")
    else:
        try:
            from .ai_review import run_ai_quality_review

            rules = load_rules()
            issues, _claims, _summary, _raw = run_ai_quality_review(
                client,
                notes_text=paged_text,
                slides=[],
                transcript_text="",
                # Stated, not hidden: this app is handed the finished notes and
                # nothing else, and the prompt already knows what to do with a
                # gap it cannot see into.
                transcript_gaps=[
                    "The class recording and the lecture slides are NOT available to "
                    "this check. Only the finished notes PDF can be read, so nothing "
                    "can be compared against what was taught — judge the notes on "
                    "their own internal correctness and consistency."
                ],
                subject=result.subject or "upsc",
                language_code=result.language_code,
                note_type="dcn" if is_dcn else "upsc",
                quality_level="strict",
                rules=rules,
                enabled_checks={},
                run_dir=run_dir,
            )
            # These are Error Report rows, not a separate sheet. The review
            # applies the whole framework rule set and is the app's second set
            # of eyes on the content; filing its findings anywhere other than
            # the report a reviewer actually reads is how they went missing.
            review_rows.extend(_row_from_issue(issue) for issue in issues)
            result.stages[stage] = "completed" if not issues else "completed_with_warnings"
            write_json(run_dir / "ai_review.json", [i.__dict__ for i in issues])
        except QCConfigError as e:
            result.stages[stage] = "failed"
            result.warnings.append(f"QC configuration invalid — review skipped: {e}")
        except Exception as e:
            result.stages[stage] = "failed"
            result.warnings.append(f"Independent AI review failed: {e}")

    # -------------------------------------------------------- 9. Error Report
    _stage_cb(stage_callback, "Building the error report")
    stage = "error_report"
    if not ai_available:
        result.stages[stage] = "unavailable"
        result.error_report_status = "unavailable"
        result.error_rows = unavailable_rows(
            gate_detail and f"The AI content check was skipped: {gate_detail}.")
    else:
        try:
            report_pass = _run_error_report_pass(
                client, document, run_dir,
                paged_text=paged_text,
                text_is_readable=text_ok,
                file_name=result.file_name,
                lecture_title=result.lecture_title,
                subject=result.subject,
                strictness=strictness,
                stage_callback=stage_callback,
            )
            blocks, marker_found = report_pass.blocks, report_pass.marker_found
            # Always merge, even for a single part: merging is what salvages
            # rows from a reply that was cut off, and a one-part document is
            # exactly as capable of overrunning the output cap as a ten-part one.
            combined, salvaged, unusable = _merge_error_blocks(blocks)
            (run_dir / "error_report_raw.json").write_text(
                combined or "[]", encoding="utf-8")
            filtered: list[dict] = []
            result.error_rows, result.error_report_status = build_report_rows(
                combined, marker_found=marker_found, strictness=strictness,
                dropped=filtered)
            if filtered:
                write_json(run_dir / "filtered_out_rows.json", filtered)
                result.strictness_dropped = len(filtered)
                result.warnings.append(
                    f"{len(filtered)} finding(s) the AI reported are out of "
                    f"scope at the '{strictness}' strictness level and are not "
                    f"listed. They are kept in filtered_out_rows.json in the run "
                    f"folder. Re-run at 'strict' to include them."
                )
            # Salvage turns an unreadable reply into a valid empty array, which
            # build_report_rows would then read as "no issues". If nothing at
            # all could be read out of the model's answer, that is a parse
            # failure and has to keep saying so — an unreadable response must
            # never be reported as a clean document.
            if unusable and not json.loads(combined or "[]"):
                result.error_rows, result.error_report_status = (
                    parse_failure_rows(), "parse_failed")
            elif unusable:
                # Some parts were readable and some were not. The report is
                # real but partial, and the count below is a floor.
                result.warnings.append(
                    f"INCOMPLETE COVERAGE: {unusable} part(s) of this document "
                    f"returned an answer that could not be read at all, so those "
                    f"stretches contributed no findings. The errors listed come "
                    f"only from the parts that were readable."
                )

            # ---------------------------------------- web fact sweep
            # The claims nothing on the page can settle: a lone figure, a lone
            # year, an attribution to a body or a place. Every error the app
            # missed on a graded document was this shape, and no amount of
            # reading the page finds them — the checker either happens to know
            # the true value or it does not. So they are looked up, ten to a
            # call. Rows arrive with their sources and then face verification
            # like every other claim.
            if FACT_SWEEP_MAX_CLAIMS > 0 and text_ok:
                _stage_cb(stage_callback, "Fact-checking figures and names on the web")
                try:
                    from .fact_sweep import run_fact_sweep

                    sweep = run_fact_sweep(
                        paged_text,
                        google_token=(getattr(client, "google_token", "")
                                      or google_token),
                        max_claims=FACT_SWEEP_MAX_CLAIMS,
                        batch_size=FACT_SWEEP_BATCH)
                    review_rows.extend(sweep.rows)
                    result.stages["fact_sweep"] = (
                        "completed" if not sweep.failed_batches
                        else "completed_with_warnings")
                    for note in sweep.warnings():
                        result.warnings.append(note)
                except Exception as e:
                    result.stages["fact_sweep"] = "failed"
                    result.warnings.append(
                        f"The web fact-check could not run ({str(e)[:160]}). The "
                        f"report below is unaffected apart from those checks.")
            else:
                result.stages["fact_sweep"] = (
                    "not_applicable" if not text_ok else "disabled")

            # The independent review's findings join the report HERE, before
            # verification, because they are AI claims like any other and must
            # face the same refutation. Merging them afterwards would let the
            # exact false positives this app exists to stop walk straight in.
            # The call is unconditional: even with nothing to add it collapses
            # the repeats the two finder passes produce between themselves.
            _merge_extra_rows(result, review_rows)
            if review_rows and result.error_report_status in (
                    "no_issues", "filtered_out"):
                result.error_report_status = "ok"

            # A second opinion, before any of this reaches a human. Only real
            # findings are put to it: "no issues" and "parse failed" produce a
            # single placeholder row, and asking the model to refute a
            # placeholder is meaningless.
            if VERIFY_FINDINGS and result.error_report_status == "ok":
                _stage_cb(stage_callback, "Verifying the findings")
                try:
                    # The notes travel with the claims: measured, the page
                    # itself already refuted five of the nine false positives
                    # a panel of judges found — it printed the grading ladder,
                    # labelled the table "As of August 2026", named the law it
                    # repealed — and the verifier could not see any of it.
                    verified = run_verification_pass(
                        client, result.error_rows, run_dir=run_dir,
                        paged_text=paged_text,
                        google_token=getattr(client, "google_token", "") or google_token)
                    result.error_rows = verified.rows
                    result.verification_dropped = len(verified.dropped)
                    # Both kinds of demotion count: a claim the verifier could
                    # not settle, and one it upheld only from memory. Both
                    # leave the row at Low with a caveat, so a run folder that
                    # counted only the first understated what had changed.
                    result.verification_downgraded = (
                        verified.downgraded + verified.memory_only)
                    for note in verified.warnings():
                        result.warnings.append(note)
                    # Say so when a web search WOULD have settled something.
                    # These leads are the app's remaining gap, and the fix is
                    # one environment variable — but only worth mentioning on
                    # a run that actually produced leads.
                    unsettled = verified.downgraded + verified.memory_only
                    if unsettled:
                        try:
                            from ..grounded_lookup import why_unavailable

                            hint = why_unavailable(
                                getattr(client, "google_token", "") or google_token)
                        except Exception:
                            hint = ""
                        if hint:
                            result.warnings.append(
                                f"{unsettled} claim(s) above are leads only because "
                                f"nothing could look the fact up. {hint}")
                    # Every claim removed and every claim left standing: if the
                    # pass itself could not run, that is reported too rather
                    # than leaving the rows looking checked.
                    result.stages["verification"] = (
                        "completed" if not verified.failed_batches
                        else "completed_with_warnings")
                    if not verified.rows:
                        # Every finding was refuted. That is a real outcome, but
                        # it is NOT "the AI found nothing" — it is "the AI found
                        # things and the audit rejected all of them", which a
                        # reviewer may well want to look at themselves.
                        result.error_rows = all_refuted_rows(
                            len(verified.dropped))
                        result.error_report_status = "all_refuted"
                except Exception as e:
                    result.stages["verification"] = "failed"
                    result.warnings.append(
                        f"The verification pass failed ({str(e)[:200]}). The "
                        f"findings below are the first pass's, unverified.")

            # Anything that did not get checked is said out loud. A partial
            # report that looks complete is worse than an obvious failure.
            for note in report_pass.coverage_warnings():
                result.warnings.append(note)
            if salvaged:
                result.warnings.append(
                    f"INCOMPLETE COVERAGE: {salvaged} error row(s) had to be "
                    f"recovered from an AI reply that was cut off before it "
                    f"finished. Those rows are kept, but any error the model had "
                    f"not yet written is missing, so this is not a complete "
                    f"error report. See error_report_raw.json and the "
                    f"error_report_response_*.txt files in the run folder."
                )
            # Salvage having been needed is itself proof the reply was cut off:
            # a model that finished writing emits a closing bracket. Treat it as
            # incomplete even when the provider did not send a finishReason, so
            # detection does not rest solely on a field the proxy may not pass
            # through.
            result.error_report_complete = report_pass.complete and not salvaged
            result.stages[stage] = (
                "completed" if result.error_report_complete
                else "completed_with_warnings")
            if result.error_report_status == "missing":
                result.warnings.append(
                    "The AI response carried no # ERROR_REPORT_START block, so the "
                    "error report could not be filled in. The report records that "
                    "rather than claiming the notes were error-free.")
            elif result.error_report_status == "parse_failed":
                result.warnings.append(
                    "The AI's error report was not valid JSON. The report records "
                    "that; see error_report_raw.json in the run folder.")
        except Exception as e:
            result.stages[stage] = "failed"
            result.error_report_status = "failed"
            result.warnings.append(f"Error report could not be built: {e}")
            result.error_rows = unavailable_rows(
                f"The AI content check failed: {str(e)[:200]}.")

    if not text_ok:
        # Reached only when the text layer was scrambled AND the pages could not
        # be read back. Every text-reading check still ran, and its findings are
        # kept — some are real — but none can be relied on, so they drop to Low
        # and carry a caveat rather than sending a reviewer to hunt a spelling
        # mistake that is a font artefact. The check's own wording is untouched.
        for finding in result.qc_findings:
            if finding["stage"] in _TEXT_DEPENDENT_STAGES:
                finding["severity"] = "Low"
                finding["caveat"] = (
                    "Read from a scrambled text layer — confirm against the page "
                    "before acting on it.")

    # Geometry and rendering findings are not reported at all any more. What the
    # remaining checkers found about the CONTENT — an inconsistent proper noun,
    # a sentence no student could parse, a missing section — belongs in the one
    # report this app produces, not in a second sheet beside it.
    # These are mechanical facts about the text — a name spelled two ways, a
    # 240-word sentence — so unlike the AI's claims they are not put to the
    # verifier: there is nothing for it to refute, and asking would only give it
    # a chance to be wrong.
    content_findings = [f for f in result.qc_findings if not _is_geometry(f)]
    result.qc_findings = []
    if content_findings:
        _merge_extra_rows(result, [_row_from_finding(f) for f in content_findings])

    # Last gate before the workbook: every row must quote something a reviewer
    # can find on the page. Placed here so it catches rows from EVERY stage —
    # the two measured cases came from the independent review, not from the
    # error-report pass.
    if text_ok and result.error_rows:
        from ..error_report_writer import _PLACEHOLDER_TYPES

        real = [r for r in result.error_rows
                if r.get("error_type") not in _PLACEHOLDER_TYPES]
        if real:
            kept, invented = _drop_invented_quotes(real, paged_text)
            if invented:
                placeholders = [r for r in result.error_rows
                                if r.get("error_type") in _PLACEHOLDER_TYPES]
                result.error_rows = kept + placeholders
                write_json(run_dir / "invented_quote_rows.json", invented)
                result.warnings.append(
                    f"{len(invented)} claimed error(s) quoted a sentence that is "
                    f"not in this document and were removed. A row a reviewer "
                    f"cannot find on the page cannot be acted on. They are kept "
                    f"in invented_quote_rows.json in the run folder."
                )

    result.error_report_summary = summarise(result.error_rows)
    problems = [f for f in result.qc_findings if f["severity"] in ("High", "Medium")]
    if any(s == "failed" for s in result.stages.values()):
        result.status = "completed_with_warnings"
    elif result.error_report_summary.get("total") or problems:
        result.status = "completed_with_warnings"
    else:
        result.status = "completed"
    _write_artifacts(run_dir, result)
    return result


def _run_error_report_pass(
    client,
    document: PdfDocument,
    run_dir: Path,
    *,
    paged_text: str,
    text_is_readable: bool,
    file_name: str,
    lecture_title: str,
    subject: str,
    strictness: str,
    stage_callback=None,
) -> _ErrorReportPass:
    """The AI error-report call(s) for one PDF.

    Long documents are chunked and the parts run concurrently, at the same
    MAX_PARALLEL_AI_CALLS ceiling the note-generation pipeline uses. A part that
    fails does not lose the others: its exception is recorded and the remaining
    parts still contribute their rows.

    Every part's fate is carried back in the returned :class:`_ErrorReportPass`.
    A part that failed, or that the model was cut off part-way through, means a
    stretch of the document went unchecked — and a report that does not say so
    reads as "these are all the errors" when it is really "these are the errors
    in the part that survived".
    """
    unrendered: list[int] = []
    chunks = _chunk_pages(document, paged_text=paged_text,
                          text_is_readable=text_is_readable,
                          unrendered=unrendered)

    # Each text window is read more than once, through different lenses: the
    # open "what is wrong here?" pass, then the entity/acronym/definition/
    # grammar checklist pass. Each (window, lens) pair is a part of its own, so
    # the coverage bookkeeping below — failed, truncated, unmarked — needs no
    # special cases. Image windows stay single-pass: they are the rare
    # scrambled-text path and already cost several pages per call.
    lenses = [""]
    if max(1, ERROR_REPORT_PASSES) > 1:
        lenses.append("entities")
    tasks: list[tuple[_Chunk, str]] = []
    for chunk in chunks:
        for lens in (lenses if not chunk.from_images else [""]):
            tasks.append((chunk, lens))

    # One more part, in multi-pass text mode: the whole document at once, asked
    # only for internal contradictions. The page windows force close reading but
    # blind every call to the rest of the document — a figure that disagrees
    # with its own repeat twelve pages later is invisible to all of them. This
    # is the one question that requires the whole document, and the one class of
    # whole-document claim the recency trap cannot touch.
    if (len(lenses) > 1 and paged_text.strip()
            and not any(c.from_images for c in chunks)):
        pages = [p.page_no for p in document.pages]
        whole_range = (f"{pages[0]}-{pages[-1]}" if pages else "")
        tasks.append((_Chunk(text=paged_text, page_range=whole_range),
                      "consistency"))

    total = len(tasks)
    blocks: list[str] = []
    marker_found = False

    def one(index: int, chunk: _Chunk, lens: str) -> tuple[int, str, bool, str, bool]:
        prompt = build_pdf_error_report_prompt(
            chunk.text,
            subject=subject,
            language_code=document.language_code,
            lecture_title=lecture_title,
            file_name=file_name,
            page_range=chunk.page_range,
            strictness=strictness,
            part=index + 1 if total > 1 else 0,
            total_parts=total,
            from_images=chunk.from_images,
            lens=lens,
        )
        (run_dir / f"error_report_prompt_{index + 1:02d}.txt").write_text(
            prompt, encoding="utf-8")
        # One retry, for the two ways a part comes back empty-handed: the
        # call raised, or the model answered with something that is not a
        # report. Both happen at a rate of about one part in twenty, and
        # without the retry each one is a stretch of the document the user
        # is told was never checked. A second, identical call is the cheapest
        # coverage there is.
        error = ""
        # The reply that was USED always lands in error_report_response_NN.txt,
        # whichever attempt produced it — that is the file the README and the
        # coverage warnings send a reviewer to. A discarded first answer is
        # kept beside it under _rejected, so nothing is thrown away silently.
        used = run_dir / f"error_report_response_{index + 1:02d}.txt"
        rejected = run_dir / f"error_report_response_{index + 1:02d}_rejected.txt"
        last_was_cut = False
        for attempt in range(2):
            try:
                resp = client.generate(prompt, chunk.images,
                                       max_images=len(chunk.images),
                                       max_output_tokens=ERROR_REPORT_MAX_OUTPUT_TOKENS,
                                       temperature=0.0)
            except Exception as e:
                error = str(e)[:300]
                continue
            # A cut-off reply is not a short answer, it is an incomplete one:
            # the rows the model had not written yet do not exist anywhere.
            # Salvage keeps what was written; this flag is what tells the
            # user the rest is missing. It is carried out of the loop as well,
            # so a part cut off on BOTH attempts is reported as truncated —
            # which names the remedy — rather than as "no report block".
            last_was_cut = bool(getattr(resp, "truncated", False))
            _body, block, found = extract_error_report_block(resp.text)
            if not found:
                # The model sometimes returns the array itself, fenced, with
                # no markers around it. That is a report, and was being
                # thrown away as "this part was not checked".
                block, found = _block_without_markers(resp.text)
            if found:
                used.write_text(resp.text, encoding="utf-8")
                return index, block, True, "", last_was_cut
            (rejected if attempt == 0 else used).write_text(
                resp.text, encoding="utf-8")
            error = ""
        return index, "", False, error, last_was_cut

    if total == 1:
        chunk, lens = tasks[0]
        results = [one(0, chunk, lens)]
    else:
        _stage_cb(stage_callback, f"Building the error report (0 of {total})")
        with ThreadPoolExecutor(max_workers=max(1, MAX_PARALLEL_AI_CALLS)) as pool:
            futures = [pool.submit(one, i, c, lens)
                       for i, (c, lens) in enumerate(tasks)]
            results = []
            for done, future in enumerate(futures, start=1):
                results.append(future.result())
                _stage_cb(stage_callback,
                          f"Building the error report ({done} of {total})")

    # A reply that hit the output cap is not a short answer: the rows the model
    # had not written yet do not exist anywhere, and raising the cap only moves
    # the ceiling. So each cut-off window is asked again ONE PAGE AT A TIME —
    # the same budget over half the content, which is the same reason a small
    # window finds more than a large one. What the retry returns is added to
    # what survived the cut, and the part only counts as truncated if the
    # smaller calls were cut off too.
    salvage_tasks: list[tuple[int, _Chunk, str]] = []
    for index, block, found, error, was_cut in results:
        if not was_cut:
            continue
        chunk, lens = tasks[index]
        for piece in _split_chunk(chunk):
            salvage_tasks.append((index, piece, lens))

    recovered: dict[int, list[str]] = {}
    if salvage_tasks:
        _stage_cb(stage_callback,
                  f"Re-reading {len({i for i, _, _ in salvage_tasks})} cut-off "
                  f"part(s) one page at a time")
        with ThreadPoolExecutor(max_workers=max(1, MAX_PARALLEL_AI_CALLS)) as pool:
            futures = {
                pool.submit(one, 10_000 + n, piece, lens): parent
                for n, (parent, piece, lens) in enumerate(salvage_tasks)
            }
            for future, parent in futures.items():
                try:
                    _i, block, found, _err, piece_cut = future.result()
                except Exception:
                    continue
                if found and block.strip() and not piece_cut:
                    recovered.setdefault(parent, []).append(block)

    if recovered:
        rebuilt = []
        for index, block, found, error, was_cut in results:
            blocks_back = recovered.get(index)
            if blocks_back:
                # The page-sized answers replace the cut-off one: they cover the
                # same pages and are complete, where it was not.
                rebuilt.append((index, blocks_back[0], True, error, False))
                for extra in blocks_back[1:]:
                    rebuilt.append((10_000 + len(rebuilt), extra, True, "", False))
            else:
                rebuilt.append((index, block, found, error, was_cut))
        results = rebuilt

    errors: list[str] = []
    failed_parts: list[int] = []
    truncated_parts: list[int] = []
    unmarked_parts: list[int] = []
    for index, block, found, error, was_cut in sorted(results, key=lambda r: r[0]):
        marker_found = marker_found or found
        if block.strip():
            blocks.append(block)
        if error:
            errors.append(f"part {index + 1}: {error}")
            failed_parts.append(index + 1)
        elif not found:
            # The call succeeded but the reply carried no error-report block.
            # That part of the document produced nothing, and "nothing" here
            # means "unchecked", not "clean".
            unmarked_parts.append(index + 1)
        if was_cut:
            truncated_parts.append(index + 1)
    if errors and not blocks:
        raise RuntimeError("; ".join(errors[:3]))
    return _ErrorReportPass(
        blocks=blocks, marker_found=marker_found, total_parts=total,
        failed_parts=failed_parts, truncated_parts=truncated_parts,
        errors=errors, unrendered_pages=unrendered,
        unmarked_parts=unmarked_parts,
    )


def _write_artifacts(run_dir: Path, result: PdfQcResult) -> None:
    """The run folder always documents what did and did not happen.

    Same rule as the note-generation engine: a missing artifact is
    indistinguishable from a crash, so the summary is written on every path
    including the early returns.
    """
    try:
        write_json(run_dir / "qc_result.json", {
            "file_name": result.file_name,
            "status": result.status,
            "error": result.error,
            "page_count": result.page_count,
            "word_count": result.word_count,
            "language_code": result.language_code,
            "lecture_title": result.lecture_title,
            "stages": result.stages,
            "warnings": result.warnings,
            "error_report_status": result.error_report_status,
            # Whether the whole document was actually checked, and what the
            # second opinion changed. Without these the run folder cannot
            # answer "was this report complete?" after the fact.
            "error_report_complete": result.error_report_complete,
            "verification_dropped": result.verification_dropped,
            "verification_downgraded": result.verification_downgraded,
            "error_report_summary": result.error_report_summary,
            "error_rows": result.error_rows,
            "qc_findings": result.qc_findings,
        })
    except Exception:
        pass
