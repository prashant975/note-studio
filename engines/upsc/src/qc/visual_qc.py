"""VISUAL QC — did the important diagrams actually make it, correctly? (spec §20)

Runs after the DOCX exists and checks the registry's promises against reality:

* every selected visual inserted exactly once, in a section that matches it;
* nothing selected went missing, and nothing excluded slipped in;
* each inserted figure is big enough to read (effective DPI at its display
  width) and was never stretched (the writer only scales proportionally, so a
  changed aspect ratio means a real bug);
* no two inserted figures are the same image (average-hash distance);
* a figure whose caption is empty is noted when its type usually needs one.

The output is a check list with ✓/⚠ lines — written to ``visual_qc.json`` and
appended to the reviewer's quality report. It never raises and never withholds
the documents; a failed check is information, not a veto.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from ..config import (
    VISUAL_MIN_READABLE_DPI,
    VISUAL_RELEVANCE_THRESHOLD,
)
from ..visuals.extractor import hash_distance
from ..visuals.metadata import VisualRegistry
from ..visuals.section_mapper import _FIGURE_RE  # same line grammar as placement

#: Types where a caption is expected; a bare photo-style crop can stand alone.
_CAPTION_EXPECTED_TYPES = {"flowchart", "map", "chart", "diagram", "timeline", "framework"}


def _figure_ids_in_notes(notes_text: str) -> list[str]:
    ids: list[str] = []
    for line in (notes_text or "").split("\n"):
        match = _FIGURE_RE.match(line)
        if match:
            ids.append(match.group(1))
    return ids


def _docx_media_count(docx_path: Path | None) -> int | None:
    """Images embedded in the document BODY (header logo etc. excluded)."""
    if not docx_path or not Path(docx_path).exists():
        return None
    try:
        with zipfile.ZipFile(docx_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        return len(re.findall(r"<pic:pic[ >]", xml))
    except Exception:
        return None


def run_visual_qc(
    registry: VisualRegistry | None,
    notes_text: str,
    docx_path: Path | None,
) -> dict:
    """Returns {"status", "inserted", "selected", "checks", "problems"}."""
    if registry is None or not registry.visuals:
        return {"status": "no_visuals", "inserted": 0, "selected": 0,
                "checks": ["No candidate visuals were found in the slides."],
                "problems": []}

    checks: list[str] = []
    problems: list[str] = []
    marker_ids = _figure_ids_in_notes(notes_text)
    marker_counts: dict[str, int] = {}
    for visual_id in marker_ids:
        marker_counts[visual_id] = marker_counts.get(visual_id, 0) + 1

    selected = registry.selected
    inserted = 0
    for v in selected:
        count = marker_counts.get(v.visual_id, 0)
        if count == 0:
            problems.append(
                f"⚠ Slide {v.slide_no} {v.visual_type or 'visual'} ({v.visual_id}) was "
                "selected but is NOT in the final notes.")
            continue
        inserted += 1
        if count > 1:
            problems.append(
                f"⚠ {v.visual_id} appears {count} times — a figure must appear once.")
        line = (f"✓ Slide {v.slide_no} {v.visual_type or 'visual'} inserted"
                + (f" — section: {v.section}" if v.section else "")
                + (f" (placed by {v.placed_by})" if v.placed_by else ""))
        checks.append(line)

        if not v.section:
            problems.append(
                f"⚠ {v.visual_id} has no recorded section — slide-to-topic mapping "
                "could not be confirmed.")
        # Readability: pixels against the width the writer will print it at.
        if v.width_px:
            from ..upsc_dcn_docx_writer import figure_display_width_in

            effective_dpi = v.width_px / figure_display_width_in(v.width_px)
            if effective_dpi < VISUAL_MIN_READABLE_DPI:
                problems.append(
                    f"⚠ {v.visual_id} prints at ~{effective_dpi:.0f} DPI "
                    f"(below {VISUAL_MIN_READABLE_DPI}); small labels may be unreadable.")
        if not (v.caption or "").strip() and (v.visual_type in _CAPTION_EXPECTED_TYPES):
            problems.append(f"⚠ {v.visual_id} ({v.visual_type}) has no caption.")

    # Markers pointing at nothing selected (should have been stripped upstream).
    known_selected = {v.visual_id for v in selected}
    for visual_id in marker_counts:
        if visual_id not in known_selected:
            problems.append(
                f"⚠ Figure marker {visual_id} does not correspond to a selected visual.")

    # Same image inserted twice under different ids.
    inserted_visuals = [v for v in selected if marker_counts.get(v.visual_id)]
    for i, a in enumerate(inserted_visuals):
        for b in inserted_visuals[i + 1:]:
            if a.phash and b.phash and hash_distance(a.phash, b.phash) <= 5:
                problems.append(
                    f"⚠ {a.visual_id} and {b.visual_id} are the same image inserted twice.")

    # The document itself: body image count should cover the inserted figures.
    media = _docx_media_count(docx_path)
    if media is not None and inserted and media < inserted:
        problems.append(
            f"⚠ The DOCX embeds {media} body image(s) but {inserted} figure(s) were "
            "expected — at least one figure failed to embed.")

    # The excluded, with their reasons — the reviewer's answer to "where is the
    # slide 11 diagram?".
    for v in registry.visuals:
        if not v.insert and v.reason:
            near_miss = (VISUAL_RELEVANCE_THRESHOLD - 0.15
                         <= v.relevance < VISUAL_RELEVANCE_THRESHOLD)
            prefix = "⚠" if near_miss else "·"
            checks.append(f"{prefix} Slide {v.slide_no} ({v.visual_id}) not inserted — {v.reason}")

    for issue in registry.issues:
        checks.append(f"· {issue}")

    status = "completed" if not problems else "completed_with_warnings"
    return {"status": status, "inserted": inserted, "selected": len(selected),
            "checks": checks, "problems": problems}


def render_visual_qc_md(result: dict) -> str:
    """The VISUAL QC section for the reviewer's quality_report.md."""
    lines = ["## Visual QC", ""]
    lines.append(f"Selected: {result.get('selected', 0)} · "
                 f"Inserted: {result.get('inserted', 0)} · "
                 f"Status: {result.get('status', 'not run')}")
    lines.append("")
    for problem in result.get("problems", []):
        lines.append(f"- {problem}")
    for check in result.get("checks", []):
        lines.append(f"- {check}")
    if not result.get("problems") and not result.get("checks"):
        lines.append("- No visuals were processed for this run.")
    return "\n".join(lines)
