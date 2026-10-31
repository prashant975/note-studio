"""Find the layout faults a DCN note can have, and fix the ones that are fixable.

This runs on the marker text *before* the DOCX is built, which is the only point
where most of these are still repairable. Once the document exists, a 320-character
table row is a clipped table — nothing downstream can widen a 5.5 in column.

Every repair here is silent by design. A normal user is not the person who can act
on "Line 11: table row is 320 characters"; they uploaded a lecture and want notes.
The repair happens, the reason goes to run_log.json, and the developer expander is
where anyone who cares can read it.

What is checked, and what happens:

    placeholder title       rebuilt from the run's metadata or the real half
    unbreakable title       zero-width breaks inserted so it wraps
    Topics Covered block    stripped — the DCN layout has no contents section
    over-wide table         rewritten as a heading plus bullets
    unclosed table/flow     closed, and its rows rewritten
    font substitution       reported as information, never as a failure
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .table_sanitizer import sanitize_tables
from .title_utils import (
    MAX_UNBREAKABLE_CHARS,
    LectureTitle,
    is_placeholder,
    longest_unbreakable_run,
    parse_marker_title,
)
from .upsc_dcn_docx_writer import resolve_handwritten_font

_TITLE_LINE_RE = re.compile(r"^[ \t]*#LECTURE_TITLE:[ \t]*(.*)$", re.I | re.M)
_TOPICS_BLOCK_RE = re.compile(
    r"^[ \t]*#TOPICS_COVERED_START[ \t]*$.*?^[ \t]*#TOPICS_COVERED_END[ \t]*$\n?",
    re.I | re.M | re.S)
#: Typed ASCII arrows ("->", "-->", "=>") — the reference always uses →.
_ASCII_ARROW_RE = re.compile(r"[ \t]*(?:-{1,2}>|={1,2}>)[ \t]*")
#: The closing section heading with any suffix ("QUICK REVISION: MOST IMPORTANT
#: CORRECTED FACTS"...). The suffix is the single most likely way correction
#: meta-language leaks, and it is repairable — the guard that runs after this
#: pass would otherwise fail the whole build over a heading.
_QUICK_REVISION_H1_RE = re.compile(
    r"^([ \t]*#H1:[ \t]*)(QUICK[ \t]+REVISION|त्वरित[ \t]*पुनरावृत्ति)\b[^\n]+$",
    re.I | re.M)


@dataclass
class RepairAction:
    """One thing that was wrong and what was done about it."""

    kind: str          # placeholder_title | long_title | topics_covered | wide_table | ...
    detail: str
    repaired: bool = True

    def __str__(self) -> str:
        verb = "repaired" if self.repaired else "detected"
        return f"{self.kind} ({verb}): {self.detail}"


@dataclass
class FormatCheckResult:
    """The repaired notes plus the trail of what was changed."""

    notes_text: str
    actions: list[RepairAction] = field(default_factory=list)
    font: str = ""
    font_substituted: bool = False
    font_note: str = ""

    @property
    def repaired(self) -> bool:
        return any(a.repaired for a in self.actions)

    @property
    def unrepaired(self) -> list[RepairAction]:
        return [a for a in self.actions if not a.repaired]

    def summary(self) -> dict:
        """For run_log.json — the frontend never renders this."""
        return {
            "repairs": [str(a) for a in self.actions],
            "repair_count": sum(1 for a in self.actions if a.repaired),
            "unrepaired_count": len(self.unrepaired),
            "font_used": self.font,
            "font_substituted": self.font_substituted,
            "font_note": self.font_note,
        }


def check_and_repair_notes(
    notes_text: str,
    *,
    lecture_title: str = "",
    language_code: str = "en",
    convert_wide_tables: bool = True,
) -> FormatCheckResult:
    """Repair the marker text so the DOCX cannot come out broken.

    Order matters: the title is settled first so every later repair sees the
    real title line; tables are rewritten before anything counts headings.
    """
    text = notes_text or ""
    actions: list[RepairAction] = []

    # --- title -------------------------------------------------------------
    match = _TITLE_LINE_RE.search(text)
    existing = match.group(1).strip() if match else ""
    resolved = (lecture_title or "").strip()

    if existing and is_placeholder(existing):
        replacement = resolved or parse_marker_title(existing).display
        text = _TITLE_LINE_RE.sub(
            lambda m: f"#LECTURE_TITLE: {replacement}", text, count=1)
        actions.append(RepairAction(
            "placeholder_title",
            f"{existing!r} was the prompt's example; replaced with {replacement!r}"))
        existing = replacement
    elif resolved and existing and existing != resolved:
        text = _TITLE_LINE_RE.sub(lambda m: f"#LECTURE_TITLE: {resolved}", text, count=1)
        actions.append(RepairAction(
            "title_override", f"{existing!r} replaced with the run's title {resolved!r}"))
        existing = resolved
    elif not match and resolved:
        text = f"#LECTURE_TITLE: {resolved}\n\n{text}".strip()
        actions.append(RepairAction("missing_title", f"inserted {resolved!r}"))
        existing = resolved
    elif not match:
        actions.append(RepairAction(
            "missing_title", "the response had no #LECTURE_TITLE: and no title was supplied",
            repaired=False))

    if existing:
        run = longest_unbreakable_run(existing)
        if run > MAX_UNBREAKABLE_CHARS:
            # The writer inserts the zero-width breaks at render time; recording
            # it here is what puts it in the run log.
            actions.append(RepairAction(
                "long_title",
                f"unbreakable run of {run} characters; break opportunities inserted"))

    # --- tables ------------------------------------------------------------
    sanitized = sanitize_tables(text, convert=convert_wide_tables)
    text = sanitized.text
    for action in sanitized.actions:
        if action.converted:
            actions.append(RepairAction(
                "wide_table",
                f"line {action.line_no}: {action.reason}; rewritten as bullets"))
        elif action.needs_conversion:
            actions.append(RepairAction(
                "wide_table", f"line {action.line_no}: {action.reason}", repaired=False))
    for warning in sanitized.warnings:
        actions.append(RepairAction("table_structure", warning))

    # --- topics covered ------------------------------------------------------
    # The reference goes straight from the title to the first H1 — it has no
    # contents section in any of its lectures. Strip any block a model that
    # remembers the old contract still emits.
    text, removed = _TOPICS_BLOCK_RE.subn("", text)
    if removed:
        actions.append(RepairAction(
            "topics_covered",
            f"removed {removed} #TOPICS_COVERED block(s); the DCN layout has no contents section"))

    # --- typography -----------------------------------------------------------
    # Typed "->" renders as two stray characters; the reference uses a real →.
    text, arrows = _ASCII_ARROW_RE.subn(" → ", text)
    if arrows:
        actions.append(RepairAction(
            "ascii_arrows", f"replaced {arrows} typed arrow(s) (->, =>) with →"))

    # --- closing-section heading ----------------------------------------------
    # "QUICK REVISION: MOST IMPORTANT CORRECTED FACTS" → "QUICK REVISION".
    # Repaired here so the correction-language guard downstream does not fail
    # the build over a heading suffix; body-text leaks still fail it.
    text, retitled = _QUICK_REVISION_H1_RE.subn(lambda m: m.group(1) + m.group(2), text)
    if retitled:
        actions.append(RepairAction(
            "quick_revision_heading",
            "dropped the suffix from the QUICK REVISION heading (plain title only)"))

    font, substituted, note = resolve_handwritten_font()
    return FormatCheckResult(
        notes_text=text,
        actions=actions,
        font=font,
        font_substituted=substituted,
        font_note=note,
    )


def inspect_rendered_pages(render_check) -> list[RepairAction]:
    """Turn a RenderCheck into actions, for the run log.

    Overlap is reported as unrepaired: by the time a page has been rendered the
    only real fix is to regenerate, which the pipeline decides, not this module.
    """
    actions: list[RepairAction] = []
    if render_check is None:
        return actions
    for page in getattr(render_check, "overlap_pages", []) or []:
        actions.append(RepairAction(
            "content_overlap",
            f"page {page}: text reaches into the 'Space for Notes' column",
            repaired=False))
    for warning in getattr(render_check, "warnings", []) or []:
        actions.append(RepairAction("render", warning, repaired=False))
    return actions


def needs_second_pass(render_check) -> bool:
    """True when re-rendering with tables forced to bullets is worth a try.

    Only overlap justifies it. A missing logo or an absent PDF converter will be
    exactly as missing on the second attempt.
    """
    return bool(getattr(render_check, "overlap_pages", None))
