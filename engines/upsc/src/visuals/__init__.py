"""Lecture visuals: extraction → local filtering → AI classification → selection.

`prepare_visuals` is the pipeline's single entry point. It returns a
`VisualRegistry` — already saved to the run folder — and never raises: any
stage failing leaves an emptier registry and a recorded issue, and the notes
generate exactly as they did before this feature existed.
"""
from __future__ import annotations

from pathlib import Path

from ..config import (
    VISUAL_MAX_PER_LECTURE,
    VISUAL_MAX_PER_SLIDE,
    VISUAL_RELEVANCE_THRESHOLD,
)
from ..models import SlideData
from .candidate_detector import filter_candidates
from .extractor import extract_candidates
from .metadata import VisualCandidate, VisualRegistry
from .section_mapper import (
    FIGURE_MARKER,
    finalize_figure_markers,
    strip_figure_markers,
)
from .visual_classifier import (
    NON_EDUCATIONAL_TYPES,
    classify_candidates,
    heuristic_classify,
)

__all__ = [
    "FIGURE_MARKER",
    "VisualCandidate",
    "VisualRegistry",
    "finalize_figure_markers",
    "prepare_visuals",
    "select_visuals",
    "strip_figure_markers",
]


def select_visuals(registry: VisualRegistry) -> None:
    """Apply the relevance threshold and the per-slide / per-lecture caps.

    Decides `insert` and writes a human-readable `reason` for every candidate,
    including the excluded ones — the QC report quotes these verbatim.
    """
    for v in registry.visuals:
        if not v.classified:
            # Over-include on purpose: an unclassified candidate has already
            # survived the local furniture/duplicate filter, and the reviewers
            # who edit these documents would rather delete a spare image than
            # go back to the deck for a missing one.
            v.insert = True
            v.reason = "not classified (AI unavailable); included — local filter passed"
        elif v.visual_type in NON_EDUCATIONAL_TYPES:
            v.insert = False
            v.reason = f"excluded — classified as {v.visual_type}"
        elif v.prefer_text_table:
            v.insert = False
            v.reason = "simple table — rendered as an editable document table instead of an image"
        elif v.relevance < VISUAL_RELEVANCE_THRESHOLD:
            v.insert = False
            v.reason = f"relevance {v.relevance:.2f} below threshold {VISUAL_RELEVANCE_THRESHOLD:.2f}"
        else:
            v.insert = True
            v.reason = f"selected — {v.visual_type}, relevance {v.relevance:.2f}"

    # Per-slide cap: keep the most relevant on a crowded slide.
    by_slide: dict[int, list[VisualCandidate]] = {}
    for v in registry.selected:
        by_slide.setdefault(v.slide_no, []).append(v)
    for slide_no, group in by_slide.items():
        if len(group) <= VISUAL_MAX_PER_SLIDE:
            continue
        group.sort(key=lambda v: v.relevance, reverse=True)
        for v in group[VISUAL_MAX_PER_SLIDE:]:
            v.insert = False
            v.reason = (f"relevance {v.relevance:.2f} but slide {slide_no} already "
                        f"contributes {VISUAL_MAX_PER_SLIDE} visual(s)")

    # Per-lecture cap: notes must not become a slide deck again.
    selected = sorted(registry.selected, key=lambda v: (-v.relevance, v.slide_no))
    for v in selected[VISUAL_MAX_PER_LECTURE:]:
        v.insert = False
        v.reason = (f"relevance {v.relevance:.2f} but the lecture already has "
                    f"{VISUAL_MAX_PER_LECTURE} stronger visuals")


def prepare_visuals(
    input_path: Path,
    slides: list[SlideData],
    run_dir: Path,
    *,
    client=None,
    language_code: str = "en",
    stage_callback=None,
) -> VisualRegistry:
    """Extract, filter, classify and select this lecture's visuals.

    Never raises. The returned registry is already saved to
    ``run_dir/visual_registry.json``; re-running in the same run folder reuses
    nothing (each run extracts fresh — the cache boundary is the run, and a
    rebuild reads the saved registry instead of calling this again).
    """
    registry = VisualRegistry()
    try:
        candidates = extract_candidates(Path(input_path), slides, Path(run_dir),
                                        registry.issues)
        candidates, filter_notes = filter_candidates(candidates)
        registry.issues.extend(filter_notes)
        registry.visuals = candidates

        if candidates:
            if client is not None:
                status, classify_issues = classify_candidates(
                    client, candidates, language_code=language_code)
                registry.classifier_status = status
                registry.issues.extend(classify_issues)
            else:
                heuristic_classify(candidates)
                registry.classifier_status = "heuristic"
        else:
            registry.classifier_status = "completed"

        select_visuals(registry)
    except Exception as e:  # noqa: BLE001 — visuals are additive, never fatal
        registry.issues.append(f"Visual preparation failed: {e}")
        for v in registry.visuals:
            v.insert = False
            v.reason = v.reason or "excluded — visual preparation failed"
    try:
        registry.save(Path(run_dir))
    except Exception as e:  # noqa: BLE001
        registry.issues.append(f"Could not save visual_registry.json: {e}")
    return registry
