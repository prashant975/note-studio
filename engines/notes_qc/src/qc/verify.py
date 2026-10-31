"""A second opinion on every claimed error, before it reaches the workbook.

The error-report pass is a single generation at temperature 0. Whatever it
asserts becomes a row, and a row in this workbook reads as a finding of fact —
someone will act on it. A model asked to *find* errors is under pressure to
produce some, and the failure that follows is not random: it reports the notes'
correct, current facts as "outdated" because its own training predates them.

So each claimed error is put to the model a second time, in a different posture:
not "find what is wrong" but "here is a claim that this is wrong — try to
refute it". Framing matters. Asked to confirm, a model confirms; asked to
refute, it applies the scrutiny that catches speculation.

Three verdicts:

``REAL``
    The error stands. The row is kept unchanged.
``NOT_AN_ERROR``
    The claim is wrong — the notes were right, or the "error" is a preference.
    The row is dropped, and written to ``verification.json`` so nothing
    disappears without a trace.
``UNVERIFIABLE``
    Cannot be settled either way. The row is kept but dropped to Low severity
    and labelled, because deleting a possibly-real error is the one mistake
    worse than reporting a doubtful one.

**Scope, stated honestly.** The verifier is given the quoted statement and the
claim about it, not the whole document. That is deliberate — it targets the
failure mode that actually occurs (world-knowledge false positives) at one call
per batch rather than per row. The cost is that a claim resting on internal
contradiction ("page 3 says 12, page 9 says 15") cannot be re-checked here; such
claims are provable from the page and are rarely the false ones, so they are
passed through on the finder's authority.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    GROUNDED_LOOKUP_MAX,
    VERIFY_BATCH_SIZE,
    VERIFY_MAX_OUTPUT_TOKENS,
)

__all__ = ["VerificationResult", "run_verification_pass"]

#: Verdicts the model may return. Anything else is treated as UNVERIFIABLE —
#: an unparseable verdict must never silently delete a row.
_REAL = "REAL"
_NOT_AN_ERROR = "NOT_AN_ERROR"
_UNVERIFIABLE = "UNVERIFIABLE"
_VERDICTS = {_REAL, _NOT_AN_ERROR, _UNVERIFIABLE}

#: The two caveats go at the FRONT of the concern, not the end. A reviewer
#: reads this column in a 45-character cell; a caveat appended after two
#: sentences of prose is invisible without expanding the row, so the leads
#: looked exactly like the findings and a reader had no way to tell which
#: rows had been checked.
_CAVEAT = "LEAD — not independently verified; check before acting."
#: For a claim the verifier upheld on its own recollection of a figure or a
#: status. Such a claim is exactly as trustworthy as that recollection, which
#: is the thing this whole pass exists to distrust — so it is kept as a lead.
_MEMORY_CAVEAT = ("LEAD — rests on the checker's memory of a figure or status; "
                  "verify against the cited source before acting.")
_BASES = {"page", "arithmetic", "settled", "memory"}

#: A claim that argues from what year it is. Measured: a row asserted "the
#: UDISE+ report for 2025-26 cannot exist because the current year is 2024",
#: on a run whose prompt had been told today was 26 August 2026. The model
#: reasons from the year it FEELS, not the one it was given, so this claim is
#: settled in Python instead of being argued about.
_YEAR_PREMISE_RE = re.compile(
    r"(?:current|present|today'?s)\s+year\s+is\s+(\d{4})"
    r"|as\s+of\s+(?:today|now)[, ]+(?:the\s+year\s+is\s+)?(\d{4})"
    r"|(?:it\s+is\s+(?:still\s+)?(?:only\s+)?)(\d{4})\s*,?\s*so",
    re.I)


def _false_year_premise(text: str, today_year: int) -> int | None:
    """The year a claim asserts is 'now', when that year is not this year."""
    for match in _YEAR_PREMISE_RE.finditer(text or ""):
        for group in match.groups():
            if not group:
                continue
            claimed = int(group)
            if claimed != today_year:
                return claimed
    return None


#: What a grounded answer adds to a row it confirms.
_GROUNDED_NOTE = "Checked against the web: {answer}"

_LOOKUP_PROMPT = """A quality-control checker has flagged a statement in a set of UPSC class \
notes as an error, but could not confirm it either way. Search the web and settle it.

The statement as printed in the notes:
  {statement}

What the checker says is wrong with it:
  {concern}

The correction it proposes:
  {correction}

Search for the fact in question, then answer in this exact form and nothing else:

VERDICT: WRONG_IN_NOTES | NOTES_ARE_RIGHT | STILL_UNCLEAR
BECAUSE: one sentence, naming the value you found

Use WRONG_IN_NOTES only when what you found contradicts the notes, NOTES_ARE_RIGHT only when \
what you found matches the notes, and STILL_UNCLEAR when the search did not settle it."""


def _grounded_check(row: dict, lookup=None,
                    google_token: str = "") -> tuple[str, str, list[str]] | None:
    """Settle one unverifiable claim from the web.

    Returns ``(verdict, because, sources)`` where verdict is WRONG_IN_NOTES,
    NOTES_ARE_RIGHT or STILL_UNCLEAR — or None when no key is configured, the
    search failed, or the provider did not confirm it actually searched. That
    last case matters: an ungrounded answer is the same guess from memory that
    made the lookup necessary, so it is thrown away rather than trusted.
    """
    try:
        from ..grounded_lookup import look_up
    except Exception:
        return None
    ask = lookup or look_up

    def get(key: str) -> str:
        return " ".join(str(row.get(key, "") or "").split())[:400]

    question = _LOOKUP_PROMPT.format(
        statement=get("original_statement"),
        concern=get("error_concern"),
        correction=get("correct_information_correction"))
    try:
        answer = (ask(question) if lookup is not None
                  else ask(question, google_token=google_token))
    except Exception:
        return None
    if answer is None or not answer.grounded:
        return None

    text = " ".join(str(answer.answer).split())
    upper = text.upper()
    verdict = "STILL_UNCLEAR"
    for candidate in ("WRONG_IN_NOTES", "NOTES_ARE_RIGHT", "STILL_UNCLEAR"):
        if candidate in upper:
            verdict = candidate
            break
    because = text.split("BECAUSE:", 1)[-1].strip() if "BECAUSE:" in upper else text
    return verdict, because[:300], list(answer.sources)


@dataclass
class VerificationResult:
    """What the second opinion changed."""

    rows: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    downgraded: int = 0
    #: Upheld, but only on the verifier's memory — kept at Low as a lead.
    memory_only: int = 0
    #: Settled by a web search instead of being left as a lead.
    grounded_confirmed: int = 0
    grounded_refuted: int = 0
    checked: int = 0
    failed_batches: int = 0
    partial_batches: int = 0

    @property
    def ran(self) -> bool:
        return self.checked > 0

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.dropped:
            notes.append(
                f"{len(self.dropped)} claimed error(s) were removed by the "
                f"verification pass as not real. See verification.json in the "
                f"run folder for each one and why."
            )
        if self.downgraded:
            notes.append(
                f"{self.downgraded} claimed error(s) could not be verified "
                f"either way and were kept at Low severity, labelled "
                f"'{_CAVEAT}'."
            )
        if self.memory_only:
            notes.append(
                f"{self.memory_only} claimed error(s) were upheld only on the "
                f"checker's own memory of a figure or status and are kept at Low "
                f"severity, labelled '{_MEMORY_CAVEAT}'."
            )
        if self.grounded_confirmed or self.grounded_refuted:
            notes.append(
                f"A web search settled {self.grounded_confirmed + self.grounded_refuted} "
                f"claim(s) the checker could not: {self.grounded_confirmed} confirmed as "
                f"real errors (sources are in the row), {self.grounded_refuted} removed."
            )
        if self.failed_batches:
            notes.append(
                f"The verification pass could not run for {self.failed_batches} "
                f"batch(es) of findings; those rows are reported exactly as the "
                f"first pass produced them, unverified."
            )
        if self.partial_batches:
            notes.append(
                f"In {self.partial_batches} batch(es) the verifier answered for "
                f"only some of the claims put to it. The unanswered ones are "
                f"reported as the first pass produced them, unverified."
            )
        return notes


_PROMPT = """You are auditing a quality-control report on a set of UPSC class notes.

TODAY'S DATE IS {today}.

Another model read the notes and claimed the statements below are errors. Your job is the
opposite of its job: for each claim, TRY TO REFUTE IT. Assume the claim is wrong until it
convinces you. You are the check that stops speculation reaching a human reviewer.

CRITICAL — THE NOTES MAY BE NEWER THAN YOUR TRAINING DATA.
The most common false claim you will see is a correct, up-to-date fact being called "outdated",
"superseded" or "non-existent" simply because the first model did not recognise it. Guard
against it:

READ THE PAGE BEFORE YOU REASON. Each claim below may carry "THE PAGE AROUND IT" — the
stretch of notes the disputed sentence sits in. That text is evidence and it outranks your
recollection. If the page states the version, vintage, framework or scope that the claim
disputes — a named scheme version, an "As of <date>" label, a grading ladder whose band names
the claim's alternative does not even contain, a clause repealing the older law, a cohort or
age range that fixes what a percentage is a share OF — then the page governs and the verdict
is NOT_AN_ERROR. Say which words on the page decided it.

VERSIONED THINGS ARE NOT SETTLED THINGS. The grade ladder, band names, threshold table or
classification criteria of a named government scheme or index are revised from time to time.
Where the notes name a version, or use band names your alternative ladder does not have, the
notes are describing a scheme you may not know. That is NOT_AN_ERROR, not REAL.

SCOPE BEFORE FIGURES. Before calling any statistic wrong, check that your comparison figure
has the SAME scope: the same population, the same age range, the same denominator, the same
measure. A share of one cohort is not refuted by a share of the whole population, and a
participation rate is not refuted by a principal-activity share. If the scopes differ, you
have not shown the notes wrong — verdict NOT_AN_ERROR if the page states the narrower scope,
UNVERIFIABLE otherwise.

WHAT EACH VERDICT MEANS — the difference decides whether a row is DELETED or merely demoted,
so read it carefully:

- NOT_AN_ERROR means YOU CAN SHOW THE CLAIM IS WRONG: the quoted text itself refutes it, settled
  knowledge refutes it, or it is a wording/style preference rather than an error. The row is
  deleted, so use this verdict only when you can say positively why the claim fails.
- UNVERIFIABLE means YOU CANNOT SETTLE IT EITHER WAY. The row survives, demoted and labelled as
  a lead for a human to check. Nothing is lost by using it.

"The notes may be citing a newer edition than I know", "that count may have changed since my
data", "this could be a recent scheme I have not seen" are all reasons you CANNOT CHECK the
claim. They are UNVERIFIABLE, never NOT_AN_ERROR. Deleting a row on that ground has repeatedly
thrown away real errors that a reviewer would have caught, because a claim you cannot check is
not a claim you have refuted.

- If the claim is that a figure, statistic, ranking, count or scheme name is outdated or does
  not exist, and the only support is that it differs from what you remember, the first model
  has not shown its case — but neither have you shown it wrong. Verdict UNVERIFIABLE.
- OFFICE-HOLDERS, ABSOLUTELY: a claim that a named person does not hold the office the notes
  give them, or holds a different office, is NEVER REAL. Elections, appointments and transfers
  after your training data are exactly what you cannot see, and "I know this person is X" is
  the false memory this audit exists to catch. Verdict NOT_AN_ERROR — however certain you feel,
  and however many posts you remember the person holding.
- A claim that a figure is IMPOSSIBLE OR IMPLAUSIBLE ON ITS FACE (a percentage over 100, a
  value an order of magnitude off anything plausible, two figures in the notes that cannot both
  be true) is judged on the arithmetic alone. If the arithmetic supports the claim, REAL. If
  you cannot settle the arithmetic, UNVERIFIABLE — never NOT_AN_ERROR, because deleting a
  plausible implausibility hides a lead a human reviewer should have seen. Do not dismiss such
  a claim as "framing" or "interpretation": the claim is about the number, not the words.
- A claim that a cited report or edition CANNOT EXIST YET is judged by date arithmetic against
  today's date and the document's own dates — never by your memory of publication schedules.
  If the arithmetic makes the edition impossible, REAL. If the edition could exist by now but
  the document may have been written earlier, UNVERIFIABLE — the human reviewer can see the
  document's date; you cannot.
- STATUS CLAIMS: a claim that something has NOT been notified, included, launched, enacted,
  passed, released or established — "X is not in the list", "no such scheme/bill exists",
  "this has not happened" — rests on knowing the present, which you do not. Verdict
  UNVERIFIABLE, so it reaches the reviewer as a lead rather than as a finding. Not
  NOT_AN_ERROR: you cannot show the claim is wrong either, and such a row sometimes carries a
  second, provable complaint alongside the unprovable one. Only where the notes settle it
  themselves — the page contradicting the claim — is NOT_AN_ERROR right.
- INTERNAL CONTRADICTION OUTRANKS THE STATUS RULE. When the claim is that the notes contradict
  THEMSELVES — a count that does not match the notes' own list ("expands to 18" above a list of
  14), one quantity given two values, a body called two different names, a total unequal to its
  parts — judge it on the quoted text alone. The world is irrelevant to it. If the quoted text
  bears the claim out, REAL; if you cannot see both sides in what is quoted, UNVERIFIABLE; never
  NOT_AN_ERROR on the ground that the world may have changed.
- TWO NAMES FOR ONE THING IS AN INCONSISTENCY, NOT STYLE. Notes that call one body, scheme,
  case or place by two different names or expansions ("National Tribunals Authority" and
  "National Tribunals Commission" for the same body) are wrong in at least one place. That is
  the QC framework's own proper-noun rule; do not dismiss it as terms "used interchangeably".
- ANACHRONISM claims are the opposite case and must NOT be dismissed as "may be historically
  accurate": if the notes describe a recent event (a 2026 notification, a bill of 2026) as
  following a commission, plan period, guideline cycle or policy whose term had ended years
  earlier, that is judged by the settled sequence (which Finance Commission covered which
  years, which Plan ran when) and date arithmetic. If the cited body's period ended well before
  the notes' own dated context, REAL. If you cannot place the period, UNVERIFIABLE — never
  NOT_AN_ERROR.
- Wording, phrasing, style and depth preferences are NOT errors — verdict NOT_AN_ERROR. But
  grammar is not preference: a subject-verb disagreement, a misspelling, or a wrong tense (a
  currently-operating scheme described with "was") can be REAL.
- If the claim rests on facts that genuinely cannot be settled without a source you do not
  have, the verdict is UNVERIFIABLE. Use it honestly — it is not a way to avoid deciding.

Facts that do NOT expire, and where a well-argued claim can be REAL: constitutional articles
and their content, the text and year of enacted laws, settled historical dates and sequences,
scientific and mathematical facts, formulae, units, symbols, definitions — and settled
slow-moving facts: where a monument, institution or headquarters is located, which river a
monument stands on, who built or founded something and under which dynasty or in which century,
what an established acronym expands to, which body publishes a given report, and which cohort
precedes or succeeds another in a settled ordering.

CLAIMS TO JUDGE:
{claims}

For every verdict also say what it RESTS ON, honestly, as "basis":
- "page"       — the quoted text itself: a contradiction, a count, a misspelling, a grammar slip
- "arithmetic" — a calculation or date arithmetic you can show
- "settled"    — knowledge that does not expire: constitutional text, enacted law, settled
                 history, science, geography, definitions, established acronyms
- "memory"     — your recollection of a figure, ranking, report, scheme, office-holder or
                 status that could have changed after your training data

A REAL verdict whose basis is "memory" is reported to the reviewer as a lead to check, not as
a finding — so choose "memory" whenever that is the truth, and do not dress a recollection up
as "settled".

Reply with ONLY a JSON array, one object per claim, in the same order, no prose around it:

[
  {{"id": 1, "verdict": "REAL" | "NOT_AN_ERROR" | "UNVERIFIABLE",
    "basis": "page" | "arithmetic" | "settled" | "memory", "why": "one sentence"}}
]
"""


#: How much of the page travels with each claim. Enough to carry the labels
#: that settle a version dispute — an "As of <date>" heading, the full band
#: list of a grading ladder, a repealing clause — without crowding the batch.
CONTEXT_CHARS = 1200


def _page_context(statement: str, paged_text: str, width: int = CONTEXT_CHARS) -> str:
    """The stretch of the notes around ``statement``, or "" if not found.

    Located by the longest distinctive prefix of the quoted statement, because
    the row's quote is usually a shortened or lightly reworded version of the
    sentence on the page.
    """
    if not statement or not paged_text:
        return ""
    hay = paged_text
    needle = " ".join(statement.split())
    for length in (120, 80, 50, 30):
        probe = needle[:length]
        if len(probe) < 20:
            continue
        at = hay.find(probe)
        if at < 0:
            at = hay.lower().find(probe.lower())
        if at >= 0:
            start = max(0, at - width // 3)
            return " ".join(hay[start:at + width].split())
    return ""


def _claim_text(index: int, row: dict, paged_text: str = "") -> str:
    def get(key: str) -> str:
        return " ".join(str(row.get(key, "") or "").split())

    context = _page_context(get("original_statement"), paged_text)
    block = (
        f"--- CLAIM {index} ---\n"
        f"Statement quoted from the notes: {get('original_statement')}\n"
        f"Claimed error type: {get('error_type')}\n"
        f"What the first model says is wrong: {get('error_concern')}\n"
        f"Correction it proposes: {get('correct_information_correction')}"
    )
    if context:
        block += f"\nTHE PAGE AROUND IT (this is the evidence): ...{context}..."
    return block


def _parse_verdicts(text: str, expected: int) -> dict[int, tuple[str, str, str]]:
    """{claim id -> (verdict, why, basis)} from the model's reply.

    ``basis`` is one of ``_BASES`` or "" when the reply did not say — an
    absent basis changes nothing, so an older-style reply is handled exactly
    as before.

    Anything missing or unrecognised is simply absent from the mapping, and the
    caller keeps the corresponding row untouched. A verifier that fails to
    answer must never be able to delete a finding by omission.
    """
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, list):
        return {}

    out: dict[int, tuple[str, str, str]] = {}
    contradicted: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            claim_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("verdict", "")).strip().upper().replace(" ", "_")
        if verdict not in _VERDICTS:
            continue
        if not 1 <= claim_id <= expected:
            continue
        basis = str(item.get("basis", "")).strip().lower()
        if basis not in _BASES:
            basis = ""
        if claim_id in out and out[claim_id][0] == verdict and out[claim_id][2] != basis:
            # Same verdict twice with different bases. Last-write-wins would
            # let the ORDER of the reply decide whether the row is a finding
            # or a lead, so the more cautious basis is kept: "memory" never
            # deletes and never promotes.
            if "memory" in (out[claim_id][2], basis):
                out[claim_id] = (verdict, out[claim_id][1], "memory")
                continue
        if claim_id in out and out[claim_id][0] != verdict:
            # The reply judged the same claim twice, differently. Last-write-wins
            # would let a self-contradicting answer delete a row on a coin toss.
            # Neither verdict is trustworthy, so the claim goes unjudged and the
            # row is kept exactly as the first pass produced it.
            contradicted.add(claim_id)
            continue
        out[claim_id] = (verdict, str(item.get("why", "")).strip()[:300], basis)
    for claim_id in contradicted:
        out.pop(claim_id, None)
    return out


def run_verification_pass(client, rows: list[dict], *, run_dir: Path | None = None,
                          today: str = "", paged_text: str = "",
                          lookup=None, google_token: str = "") -> VerificationResult:
    """Put every claimed error to the model a second time and act on the verdict.

    Returns a :class:`VerificationResult`. When ``client`` is None or ``rows`` is
    empty the rows are returned untouched, so a run without AI access behaves
    exactly as it did before this pass existed.
    """
    result = VerificationResult(rows=list(rows))
    if client is None or not rows:
        return result

    if not today:
        try:
            from ..config import now_ist

            today = now_ist().strftime("%d %B %Y")
        except Exception:
            today = "today"

    verdicts: dict[int, tuple[str, str, str]] = {}
    batch_size = max(1, VERIFY_BATCH_SIZE)

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        # Ids are 1-based WITHIN the batch, so the model counts a short list
        # rather than tracking global offsets — fewer places to go wrong.
        claims = "\n\n".join(_claim_text(i, r, paged_text)
                             for i, r in enumerate(batch, start=1))
        prompt = _PROMPT.format(today=today, claims=claims)
        try:
            resp = client.generate(prompt, None,
                                   max_output_tokens=VERIFY_MAX_OUTPUT_TOKENS,
                                   temperature=0.0)
        except Exception:
            result.failed_batches += 1
            continue

        if run_dir is not None:
            try:
                (Path(run_dir) / f"verification_{start // batch_size + 1:02d}.txt"
                 ).write_text(resp.text, encoding="utf-8")
            except Exception:
                pass

        # A cut-off verdict list is a partial one. Whatever did parse is used;
        # rows with no verdict keep their original severity, unverified.
        if getattr(resp, "truncated", False):
            result.failed_batches += 1
            continue
        parsed = _parse_verdicts(resp.text, len(batch))
        if not parsed:
            result.failed_batches += 1
            continue
        if len(parsed) < len(batch):
            # The model judged only some of the claims it was given. The rest
            # are unjudged, not upheld — count the batch as partially failed so
            # the run does not claim every finding was reviewed.
            result.partial_batches += 1
        for local_id, verdict in parsed.items():
            verdicts[start + local_id - 1] = verdict

    try:
        today_year = int(str(today)[-4:])
    except (TypeError, ValueError):
        today_year = 0

    kept: list[dict] = []
    looked_up = 0
    for index, row in enumerate(rows):
        # A claim whose own premise is a wrong year is settled here, not put
        # to the model: it reasons from the year it feels rather than the one
        # the prompt gave it, so asking only invites the same mistake twice.
        if today_year:
            claimed = _false_year_premise(
                " ".join(str(row.get(k, "") or "") for k in (
                    "error_concern", "correct_information_correction")),
                today_year)
            if claimed is not None:
                dropped = dict(row)
                dropped["_verification_why"] = (
                    f"The claim argues from the current year being {claimed}; "
                    f"it is {today_year}. A finding whose own premise is wrong "
                    f"cannot stand.")
                result.dropped.append(dropped)
                result.checked += 1
                continue

        verdict, why, basis = verdicts.get(index, ("", "", ""))
        if not verdict:
            kept.append(row)
            continue
        result.checked += 1
        if verdict == _NOT_AN_ERROR:
            dropped = dict(row)
            dropped["_verification_why"] = why
            result.dropped.append(dropped)
            continue
        # The verifier has just said it cannot check this. That is precisely
        # what a web search is for, so ask before settling for a lead. Only
        # reached when a Gemini API key is configured; the proxy cannot do it.
        if ((verdict == _UNVERIFIABLE or (verdict == _REAL and basis == "memory"))
                and looked_up < max(0, GROUNDED_LOOKUP_MAX)):
            looked_up += 1
            settled = _grounded_check(row, lookup, google_token)
            if settled is not None:
                found, because, sources = settled
                if found == "NOTES_ARE_RIGHT":
                    dropped = dict(row)
                    dropped["_verification_why"] = f"Web search: {because}"
                    dropped["_grounded_sources"] = sources
                    result.dropped.append(dropped)
                    result.grounded_refuted += 1
                    continue
                if found == "WRONG_IN_NOTES":
                    row = dict(row)
                    concern = str(row.get("error_concern", "") or "").rstrip()
                    row["error_concern"] = (
                        f"{concern} [{_GROUNDED_NOTE.format(answer=because)}]").strip()
                    if sources:
                        existing = str(row.get("source_urls", "") or "").strip()
                        joined = "; ".join(sources[:3])
                        row["source_urls"] = f"{existing}; {joined}" if existing else joined
                    result.grounded_confirmed += 1
                    kept.append(row)
                    continue

        if verdict == _UNVERIFIABLE:
            row = dict(row)
            row["severity"] = "Low"
            concern = str(row.get("error_concern", "") or "").rstrip()
            if _CAVEAT not in concern:
                row["error_concern"] = f"[{_CAVEAT}] {concern}".strip()
            result.downgraded += 1
        elif basis == "memory":
            # REAL, says the verifier — but on its own recollection of a
            # figure or a status. That is the one basis this pass exists to
            # distrust, and the prose rules alone did not stop such claims
            # reaching the workbook as High. So the outcome is enforced here:
            # the row stays (a lead is worth having) at Low, and says why.
            row = dict(row)
            row["severity"] = "Low"
            concern = str(row.get("error_concern", "") or "").rstrip()
            if _MEMORY_CAVEAT not in concern:
                row["error_concern"] = f"[{_MEMORY_CAVEAT}] {concern}".strip()
            result.memory_only += 1
        kept.append(row)

    result.rows = kept

    if run_dir is not None:
        try:
            (Path(run_dir) / "verification.json").write_text(
                json.dumps({
                    "checked": result.checked,
                    "kept": len(result.rows),
                    "dropped": result.dropped,
                    "downgraded": result.downgraded,
                    "memory_only": result.memory_only,
                    "failed_batches": result.failed_batches,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return result
