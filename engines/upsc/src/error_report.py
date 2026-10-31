"""Render the Error Report: factual errors in what the faculty taught.

This is about the *content* of the class, not the machinery. It answers:
  1. Which points the teacher made were wrong, outdated or misleading?
  2. What was changed in the notes because of each one?

The data already exists — the AI is instructed to never silently fix or drop a
faculty point, but to leave a [Faculty Point Corrected] / [Faculty Point
Removed] / [Needs Human Review] block at the exact spot in the notes. Those
blocks are parsed into CorrectionEntry objects by quality_checker; this module
turns them into a standalone report a reviewer can read on its own.

A short transcript-reliability section is appended because the two are linked:
if part of the lecture was never transcribed, the faculty points made in that
stretch were never fact-checked, and the report should not imply otherwise.
"""
from __future__ import annotations

from .models import TranscriptIssue
from .quality_checker import CorrectionEntry

_KIND_ORDER = {"corrected": 0, "removed": 1, "review": 2}


def _cell(value: object) -> str:
    """Flatten a value so it cannot break out of a markdown table cell."""
    text = "" if value is None else str(value)
    text = " ".join(text.replace("|", "\\|").split())
    return text or "-"


def _change_made(e: CorrectionEntry) -> tuple[str, str]:
    """(what the notes now do, the replacement text) for one entry."""
    if e.kind == "corrected":
        return "Corrected in the notes", e.correction
    if e.kind == "removed":
        return "Removed from the notes", e.replacement
    return "Flagged — left in, needs a human check", e.action or "Verify from an official source"


def render_error_report_md(
    corrections: list[CorrectionEntry],
    *,
    filename: str = "",
    chapter_title: str = "",
    youtube_url: str = "",
    note_type: str = "",
    quality_level: str = "",
    fact_check_on: bool = True,
    transcript_issues: list[TranscriptIssue] | None = None,
    transcript_words: int = 0,
) -> str:
    transcript_issues = transcript_issues or []
    corrected = [e for e in corrections if e.kind == "corrected"]
    removed = [e for e in corrections if e.kind == "removed"]
    review = [e for e in corrections if e.kind == "review"]

    out: list[str] = ["# Error Report — faculty points fact-checked", ""]
    if chapter_title:
        out.append(f"**Chapter:** {chapter_title}")
    if filename:
        out.append(f"**File:** {filename}")
    if note_type:
        out.append(f"**Note type:** {note_type}")
    if youtube_url:
        out.append(f"**Class video:** {youtube_url}")
    out.append("")

    # ------------------------------------------------------------- headline
    if not fact_check_on:
        out += [
            "## Fact-checking was OFF for this run",
            "",
            "No faculty point was checked, so this report is empty by configuration — "
            "not because the class was error-free. Re-run with the quality level set to "
            "Basic or Strict to fact-check the lecture.",
            "",
        ]
        return "\n".join(out)

    total = len(corrections)
    if total == 0:
        out += [
            "## No factual errors were flagged",
            "",
            f"The AI fact-checked the class at **{quality_level or 'strict'}** level and did "
            "not find a faculty point it judged wrong, outdated or misleading. Nothing in "
            "the notes was corrected or removed on factual grounds.",
            "",
        ]
    else:
        out += [
            "## Summary",
            "",
            "| What was found | Count |",
            "| --- | --- |",
            f"| Faculty points **corrected** in the notes | {len(corrected)} |",
            f"| Faculty points **removed** from the notes | {len(removed)} |",
            f"| Points **flagged for human review** | {len(review)} |",
            f"| **Total points flagged** | {total} |",
            "",
            f"Fact-check level: **{quality_level or 'strict'}**. Every change below is also "
            "marked at the exact spot in the notes where the point belongs — nothing was "
            "silently altered.",
            "",
        ]

    # ------------------------------------- the main table: said -> changed
    if total:
        out += [
            "## What the teacher said, and what was changed",
            "",
            "| # | Slide | Topic | What the teacher said | What is wrong with it | Change made in the notes | Now reads | Confidence |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        ordered = sorted(corrections, key=lambda e: (_KIND_ORDER.get(e.kind, 9), e.slide_no or 0))
        for n, e in enumerate(ordered, start=1):
            change, now = _change_made(e)
            out.append(
                "| " + " | ".join(_cell(c) for c in [
                    n,
                    e.slide_no if e.slide_no else "-",
                    e.heading,
                    e.original,
                    e.reason or e.issue,
                    change,
                    now,
                    e.confidence,
                ]) + " |"
            )
        out.append("")

    # --------------------------------------------- needs a human, spelled out
    if review:
        out += [
            "## Needs a human check",
            "",
            "The AI was not confident enough to correct these itself. They are still in the "
            "notes as the faculty said them — verify before the notes go out.",
            "",
        ]
        for e in review:
            where = f"slide {e.slide_no}" if e.slide_no else (e.heading or "location not given")
            out.append(f"- **{_cell(e.original)}** ({where}) — {_cell(e.issue or e.reason)}")
        out.append("")

    # ------------------------------------------------ how complete this is
    gaps = [i for i in transcript_issues if i.time_range]
    out += ["## How complete this check is", ""]
    if not transcript_words:
        out += [
            "The lecture was **not transcribed**, so only what is printed on the slides was "
            "fact-checked. Anything the teacher said out loud was never seen by the checker "
            "and is not covered by this report.",
            "",
        ]
    elif gaps:
        out += [
            f"**{len(gaps)} stretch(es) of the lecture audio were not transcribed**, so any "
            "faculty point made in those minutes was never fact-checked. This report covers "
            "the rest of the class:",
            "",
        ]
        for i in gaps:
            out.append(f"- {i.time_range} (chunk {i.chunk or '-'}) — not checked")
        out.append("")
    else:
        out += [
            "The full lecture was transcribed and checked, along with the slides.",
            "",
        ]
    return "\n".join(out)
