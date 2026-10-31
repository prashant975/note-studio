"""Local, free filtering of extracted candidates — before any AI is spent.

Two jobs (spec §23 stage 2):

* throw out what is obviously not an educational visual — recurring page
  furniture (a logo appears on every slide; a diagram does not), and
* deduplicate — the same diagram shown on consecutive slides is kept once,
  as its sharpest copy.

Everything subtler than that (is this map meaningful? is this table better as
text?) is left to the classifier, because heuristics guessing at meaning is how
important diagrams go missing.
"""
from __future__ import annotations

from .extractor import hash_distance
from .metadata import VisualCandidate

#: Same-hash appearances on this many distinct slides = page furniture (logos,
#: watermarks, template art). A real diagram is taught once, maybe twice.
FURNITURE_SLIDE_COUNT = 3
#: Average-hash Hamming distance at or below which two crops are "the same".
DUPLICATE_DISTANCE = 5


def _pixels(candidate: VisualCandidate) -> int:
    return candidate.width_px * candidate.height_px


def filter_candidates(
    candidates: list[VisualCandidate],
) -> tuple[list[VisualCandidate], list[str]]:
    """(kept, notes). Order is preserved; notes explain every removal."""
    notes: list[str] = []
    if not candidates:
        return [], notes

    # --- recurring furniture -------------------------------------------------
    groups: dict[str, set[int]] = {}
    for c in candidates:
        if c.phash:
            groups.setdefault(c.phash, set()).add(c.slide_no)
    furniture_hashes = {h for h, slide_nos in groups.items()
                        if len(slide_nos) >= FURNITURE_SLIDE_COUNT}
    kept: list[VisualCandidate] = []
    for c in candidates:
        if c.phash and c.phash in furniture_hashes:
            notes.append(
                f"{c.visual_id}: removed — repeats on "
                f"{len(groups[c.phash])} slides (page furniture/logo)")
        else:
            kept.append(c)

    # --- duplicates ----------------------------------------------------------
    # Keep the highest-resolution copy of near-identical crops; the survivors
    # stay in slide order so the prompt's list reads like the lecture.
    unique: list[VisualCandidate] = []
    for c in kept:
        twin = next((u for u in unique
                     if c.phash and u.phash
                     and hash_distance(c.phash, u.phash) <= DUPLICATE_DISTANCE), None)
        if twin is None:
            unique.append(c)
        elif _pixels(c) > _pixels(twin):
            notes.append(f"{twin.visual_id}: removed — duplicate of {c.visual_id} "
                         "(lower resolution copy)")
            unique[unique.index(twin)] = c
        else:
            notes.append(f"{c.visual_id}: removed — duplicate of {twin.visual_id}")
    return unique, notes
