"""Make every ``#FIGURE:`` marker honest, and place the figures the model forgot.

The DCN model is told which visuals exist and asked to drop a ``#FIGURE: <id>``
line where each supports the notes. Model output being model output, this pass
enforces the contract before the writer sees it:

* an id that does not exist, or was not selected, is removed;
* a second occurrence of the same id is removed (a figure appears once);
* a marker inside a #TABLE/#FLOW block is pulled out (the writer would have
  typeset it as literal text there);
* a selected figure the model never placed is inserted at the end of the
  section whose heading best matches its topic — after the explanation, which
  is where the reference notes put their diagrams;
* a figure no section matches is NOT forced in. It is downgraded with a
  recorded reason, and the visual QC reports it (spec §20) — a diagram on the
  wrong topic is worse than a diagram missing.

Everything decided here is written back into the registry (section, placed_by),
which is what the QC checks against later.
"""
from __future__ import annotations

import re

from .metadata import VisualRegistry

FIGURE_MARKER = "#FIGURE:"
_FIGURE_RE = re.compile(r"^\s*#FIGURE:\s*(\S+)\s*$", re.I)
_HEADING_RE = re.compile(r"^\s*#(H1|H2|H3):\s*(.+?)\s*$", re.I)
_TABLE_FLOW_RE = re.compile(r"^\s*#(TABLE|FLOW)_(START|END)\s*$", re.I)

#: Minimum topic/heading token overlap for the fallback placement to trust.
MATCH_THRESHOLD = 0.34

_STOPWORDS = {
    "the", "of", "and", "in", "to", "a", "an", "for", "on", "with", "its",
    "class", "notes", "lecture", "india", "indian", "introduction",
    "का", "की", "के", "और", "में", "से", "एक", "पर", "है", "हैं",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[\wऀ-ॿ]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _match_score(heading: str, visual_terms: set[str]) -> float:
    heading_tokens = _tokens(heading)
    if not heading_tokens or not visual_terms:
        return 0.0
    overlap = len(heading_tokens & visual_terms)
    return overlap / min(len(heading_tokens), len(visual_terms))


def finalize_figure_markers(
    notes_text: str,
    registry: VisualRegistry | None,
) -> tuple[str, dict]:
    """Validate model-placed markers, place the rest, update the registry.

    Returns ``(notes_text, placement_report)``. With no registry (or none
    selected) every marker is stripped — text without figures beats markers
    leaking into the student's document.
    """
    report = {
        "placed_by_model": 0,
        "placed_by_mapping": 0,
        "dropped_unknown": [],
        "dropped_duplicate": [],
        "moved_out_of_blocks": [],
        "not_placed": [],
        #: The subset of not_placed that had nothing to match on at all. Kept
        #: separate because the two have different fixes: one is a classifier
        #: that did not run, the other is a diagram the lecture never discusses.
        "unclassified": [],
    }
    lines = (notes_text or "").replace("\r\n", "\n").split("\n")

    placed: set[str] = set()
    kept_lines: list[str] = []
    last_heading = ""
    in_block = False
    for line in lines:
        block_marker = _TABLE_FLOW_RE.match(line)
        if block_marker:
            in_block = block_marker.group(2).upper() == "START"
            kept_lines.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            last_heading = heading.group(2)
            kept_lines.append(line)
            continue
        figure = _FIGURE_RE.match(line)
        if not figure:
            kept_lines.append(line)
            continue

        visual_id = figure.group(1)
        visual = registry.by_id(visual_id) if registry else None
        if visual is None or not visual.insert:
            report["dropped_unknown"].append(visual_id)
            continue
        if visual_id in placed:
            report["dropped_duplicate"].append(visual_id)
            continue
        if in_block:
            # Re-placed by the fallback below, outside the table/flow.
            report["moved_out_of_blocks"].append(visual_id)
            continue
        placed.add(visual_id)
        visual.placed_by = "model"
        visual.section = last_heading
        report["placed_by_model"] += 1
        kept_lines.append(f"{FIGURE_MARKER} {visual_id}")

    lines = kept_lines
    if registry is None:
        return "\n".join(lines), report

    # ---- fallback: map still-unplaced figures onto their best heading -------
    unplaced = [v for v in registry.selected if v.visual_id not in placed]
    if unplaced:
        headings = [(index, match.group(2))
                    for index, line in enumerate(lines)
                    if (match := _HEADING_RE.match(line))]
        # Later insertions shift indexes, so collect first and apply bottom-up.
        insertions: list[tuple[int, str]] = []
        for visual in sorted(unplaced, key=lambda v: v.slide_no):
            terms = _tokens(f"{visual.topic} {visual.slide_heading} {visual.caption}")
            if not terms:
                # Nothing to match ON — the classifier did not run (or failed),
                # so this visual has no topic, caption or slide heading. The
                # old message here was "no matching section found", which reads
                # as "we compared it against your notes and it fitted nowhere".
                # Every visual in the lecture then failed for a reason that
                # named the notes instead of the classifier that produced none.
                visual.insert = False
                visual.reason = ("selected but not inserted — the visual has no "
                                 "topic, caption or slide heading to match a "
                                 "section against (it was never classified)")
                report["not_placed"].append(visual.visual_id)
                report["unclassified"].append(visual.visual_id)
                continue
            best_index, best_score, best_heading = -1, 0.0, ""
            for index, heading in headings:
                score = _match_score(heading, terms)
                if score > best_score:
                    best_index, best_score, best_heading = index, score, heading
            if best_index < 0 or best_score < MATCH_THRESHOLD:
                visual.insert = False
                visual.reason = "selected but not inserted — no matching section found"
                report["not_placed"].append(visual.visual_id)
                continue
            # End of the matched section: just before the next heading (or EOF),
            # skipping back over trailing blank lines so the figure sits with
            # the section's text rather than floating between sections.
            end = len(lines)
            for index, _ in headings:
                if index > best_index:
                    end = index
                    break
            while end > best_index + 1 and not lines[end - 1].strip():
                end -= 1
            insertions.append((end, visual.visual_id))
            visual.placed_by = "section_match"
            visual.section = best_heading
            report["placed_by_mapping"] += 1
        for position, visual_id in sorted(insertions, reverse=True):
            lines.insert(position, f"{FIGURE_MARKER} {visual_id}")
            lines.insert(position, "")

    return "\n".join(lines), report


def strip_figure_markers(notes_text: str) -> str:
    """Remove every #FIGURE line — the escape hatch when no visuals exist."""
    lines = [l for l in (notes_text or "").split("\n") if not _FIGURE_RE.match(l)]
    return "\n".join(lines)
