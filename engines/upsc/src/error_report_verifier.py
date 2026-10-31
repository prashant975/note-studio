"""A second opinion on the Error Report, before it reaches the sheet.

The rows come from the same model that wrote the notes, in the same breath. It
is being asked to grade the lecturer while writing for them, and it produces
some findings it cannot support — a figure it suspects has moved, a scheme it
half-remembers being renamed. Those rows cost a reviewer more than they save,
because checking a wrong finding takes exactly as long as checking a right one.

So every finding is shown to the model once more, cold, with one instruction:
try to REFUTE it. Nothing else about the lecture is supplied — the row has to
stand on its own, which is the same standard the reviewer will apply.

Design notes:

* **One call for all rows, not one per row.** A verification pass that doubles
  the bill would be switched off, and a switched-off check verifies nothing.
* **A refuted row is demoted, never deleted.** The verifier is the same kind of
  thing that produced the finding and is wrong at the same sort of rate;
  silently dropping a real error to save a reviewer thirty seconds is the worse
  trade. Refuted rows survive as Low with the doubt written into the row.
* **Failure is silent and total.** If the call fails, times out, or comes back
  unreadable, every row passes through untouched. A verification pass that can
  block a report is a new way to lose one.
"""
from __future__ import annotations

import json
import re

#: Appended to a row's concern when the second pass could not support it. Written
#: into the sheet rather than kept in a log, because the reviewer deciding
#: whether to act on the row is the person who needs the doubt.
UNSUPPORTED_NOTE = (
    "[Second check: this finding could not be supported on review — treat it as "
    "a prompt to look, not as a confirmed error.]"
)

#: Types whose whole value is currency, and which the model cannot check without
#: live data. These are held to the strictest standard by the verifier.
_CURRENCY_TYPES = frozenset({
    "numerical/outdated", "outdated data", "current-affairs update",
    "number/data error", "date/year error",
})

#: Never second-guessed. These rows are not claims about the world: they are the
#: app reporting on its own output, and the verifier has nothing to add.
_NOT_VERIFIABLE = frozenset({
    "no issue", "report parsing", "report coverage", "correction meta-language",
})

_PROMPT_HEAD = """You are reviewing a list of claimed errors that another reviewer made about a
lecture. Your job is to REFUTE them, not to agree with them.

TODAY'S DATE IS {today}. You have no live web access and your training data ends before
this date, so you cannot confirm the current value of anything that changes over time.

For each numbered finding decide:
  "supported"   — the finding is clearly correct and you could defend it to an examiner.
  "unsupported" — it is wrong, or it depends on a current value neither of us can check,
                  or the "correction" offered is no more reliable than what it replaces.

Be strict. A finding that merely sounds plausible is unsupported. A claim that something is
outdated is unsupported unless the finding itself names the replacement value AND its source.
If you are unsure, answer "unsupported" — the cost of a wrong finding reaching a reviewer is
higher than the cost of one being double-checked.

Return ONLY valid JSON, no prose, no markdown fence:
{{"verdicts":[{{"n":1,"verdict":"supported"}},{{"n":2,"verdict":"unsupported","why":"one short reason"}}]}}
One entry per finding, using the numbers below.

FINDINGS:
"""


def _findings_block(rows: list[dict]) -> str:
    lines = []
    for number, row in enumerate(rows, start=1):
        lines.append(
            f"{number}. [{row.get('error_type', '')} / {row.get('severity', '')}]\n"
            f"   Claimed wrong: {row.get('original_statement', '')}\n"
            f"   Why: {row.get('error_concern', '')}\n"
            f"   Proposed correction: {row.get('correct_information_correction', '')}\n"
            f"   Source given: {row.get('source_urls', '') or 'none'}"
        )
    return "\n".join(lines)


def _verifiable(row: dict) -> bool:
    return (row.get("error_type", "") or "").strip().lower() not in _NOT_VERIFIABLE


def build_prompt(rows: list[dict], today: str) -> str:
    return _PROMPT_HEAD.format(today=today) + _findings_block(rows)


def parse_verdicts(reply: str, expected: int) -> dict[int, tuple[str, str]]:
    """{finding number: (verdict, why)} from the model's reply.

    An unreadable reply yields {} and every row is kept as it was — see the
    module docstring on why this fails open.
    """
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (reply or "").strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return {}
    out: dict[int, tuple[str, str]] = {}
    for item in (data.get("verdicts") or []):
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= expected:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict in {"supported", "unsupported"}:
            out[number] = (verdict, str(item.get("why") or "").strip())
    return out


def verify_rows(rows: list[dict], client, *, today: str,
                max_rows: int = 40) -> tuple[list[dict], dict]:
    """Rows with unsupported findings demoted, plus a summary for the run log.

    Never raises, never drops a row, never blocks the report.
    """
    summary = {"ran": False, "checked": 0, "demoted": 0, "reason": ""}
    if client is None or not rows:
        summary["reason"] = "no client" if client is None else "no rows"
        return rows, summary

    checkable = [r for r in rows if _verifiable(r)][:max_rows]
    if not checkable:
        summary["reason"] = "nothing verifiable"
        return rows, summary

    try:
        reply = client.generate(build_prompt(checkable, today), [], max_images=0,
                                max_output_tokens=4000).text
    except Exception as exc:  # noqa: BLE001 — a second opinion is never worth a run
        summary["reason"] = f"call failed: {type(exc).__name__}"
        return rows, summary

    verdicts = parse_verdicts(reply, len(checkable))
    if not verdicts:
        summary["reason"] = "verdicts unreadable"
        return rows, summary

    summary.update(ran=True, checked=len(verdicts))
    for number, row in enumerate(checkable, start=1):
        verdict, why = verdicts.get(number, ("supported", ""))
        if verdict != "unsupported":
            continue
        summary["demoted"] += 1
        # Demoted, not deleted. The verifier is the same kind of thing that
        # produced the finding; it does not get to be the last word on whether a
        # student sees a correction.
        row["severity"] = "Low"
        note = UNSUPPORTED_NOTE if not why else f"{UNSUPPORTED_NOTE[:-1]} {why}]"
        if note not in row.get("error_concern", ""):
            row["error_concern"] = f"{row.get('error_concern', '').strip()} {note}".strip()
    return rows, summary
