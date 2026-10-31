"""Gemini decides which candidates are worth a page in the student's notes.

One call per small batch of crops (they travel inline through the ~4.5 MB proxy
body budget, and ai_client compresses each image). The model returns strict
JSON per image: educational or not, a relevance score on the shared 0-1 scale,
the visual's type, the topic it belongs to, a short caption, and whether it is
really a simple table that reads better as text (spec §12).

Failures degrade, never raise: a batch whose call or JSON fails leaves its
candidates unclassified, and unclassified candidates are simply not inserted —
the notes still generate (spec §24).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import MAX_PARALLEL_AI_CALLS, VISUAL_CLASSIFY_BATCH
from .metadata import VisualCandidate

_VALID_TYPES = {"flowchart", "map", "chart", "diagram", "table", "timeline",
                "framework", "illustration", "photo", "decorative", "text"}
#: Types that are content-free for a notes document even at high confidence.
NON_EDUCATIONAL_TYPES = {"decorative", "photo", "text"}


def _classification_prompt(batch: list[VisualCandidate], language_code: str) -> str:
    from ..languages import language_name

    language = language_name(language_code)
    lines = []
    for index, c in enumerate(batch, start=1):
        heading = (c.slide_heading or "").strip().replace("\n", " ")[:120]
        lines.append(f"Image {index}: from slide {c.slide_no}"
                     + (f' (slide heading: "{heading}")' if heading else ""))
    listing = "\n".join(lines)
    return f"""You are reviewing visuals cropped from UPSC lecture slides. For EACH attached image, judge whether it belongs in the student's typed class notes.

{listing}

Judge each image on its own:
- EDUCATIONAL means it explains a concept, shows a process, relationships, a map, a chart, a labelled diagram, a timeline, or important structured facts — removing it would reduce understanding.
- NOT educational: logos, branding, decorative art, backgrounds, icons, photos of people/studio, QR codes, promotional panels, and slides that are ONLY plain text (bullets/headings with no diagrammatic content).
- relevance is 0.00-1.00: 0.00-0.29 ignore, 0.30-0.59 weak, 0.60-0.79 useful, 0.80-1.00 essential.
- visual_type is one of: flowchart, map, chart, diagram, table, timeline, framework, illustration, photo, decorative, text.
- best_as_text_table is true ONLY for a simple grid of short text cells that loses nothing when retyped as a document table. A table with drawings, arrows or maps inside is false.
- topic: 2-6 words naming the concept the visual teaches, in {language}.
- caption: one short academically useful sentence fragment in {language} (no "Figure:" prefix, no trailing full stop needed), e.g. "Stages of Himalayan orogeny and the resulting fault lines".
- description: one sentence saying what the visual shows, in English (internal use).

Respond ONLY with JSON, no code fences:
{{"visuals": [{{"index": 1, "educational": true, "relevance": 0.85, "visual_type": "flowchart", "best_as_text_table": false, "topic": "...", "caption": "...", "description": "..."}}]}}
The array must have exactly one entry per attached image, in order."""


def _parse_response(text: str) -> list[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the classifier response")
    data = json.loads(cleaned[start:end + 1])
    visuals = data.get("visuals")
    if not isinstance(visuals, list):
        raise ValueError("classifier JSON carries no 'visuals' array")
    return [v for v in visuals if isinstance(v, dict)]


def _apply(batch: list[VisualCandidate], rows: list[dict]) -> None:
    by_index = {int(r.get("index", 0)): r for r in rows if str(r.get("index", "")).isdigit()
                or isinstance(r.get("index"), int)}
    for position, candidate in enumerate(batch, start=1):
        row = by_index.get(position)
        if row is None and position <= len(rows):
            row = rows[position - 1]       # index missing/wrong: trust order
        if row is None:
            continue
        try:
            relevance = float(row.get("relevance", 0.0))
        except (TypeError, ValueError):
            relevance = 0.0
        visual_type = str(row.get("visual_type", "")).strip().lower()
        candidate.classified = True
        candidate.relevance = max(0.0, min(1.0, relevance))
        candidate.visual_type = visual_type if visual_type in _VALID_TYPES else "diagram"
        candidate.topic = str(row.get("topic", "")).strip()[:120]
        candidate.caption = str(row.get("caption", "")).strip()[:200]
        candidate.description = str(row.get("description", "")).strip()[:300]
        candidate.prefer_text_table = bool(row.get("best_as_text_table", False))
        if not bool(row.get("educational", False)):
            candidate.relevance = min(candidate.relevance, 0.29)


def classify_candidates(
    client,
    candidates: list[VisualCandidate],
    *,
    language_code: str = "en",
    batch_size: int = VISUAL_CLASSIFY_BATCH,
) -> tuple[str, list[str]]:
    """Classify in place. Returns (status, issues).

    status: "completed" | "partial" | "failed" | "skipped".
    """
    if not candidates:
        return "completed", []
    if client is None:
        return "skipped", []

    batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    issues: list[str] = []

    def _classify_batch(batch: list[VisualCandidate]) -> str | None:
        prompt = _classification_prompt(batch, language_code)
        images = [c.image_path for c in batch if c.image_path]
        resp = client.generate(prompt, images, max_images=len(images),
                               max_output_tokens=2000, temperature=0.0)
        _apply(batch, _parse_response(resp.text))
        return None

    failures = 0
    workers = max(1, min(MAX_PARALLEL_AI_CALLS, len(batches)))
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(_classify_batch, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 — the batch degrades, the run continues
                failures += 1
                ids = ", ".join(c.visual_id for c in batch)
                issues.append(f"Visual classification failed for {ids}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if failures == 0:
        return "completed", issues
    if failures == len(batches):
        return "failed", issues
    return "partial", issues


def heuristic_classify(candidates: list[VisualCandidate]) -> None:
    """No-AI stand-in (mock runs, tests): big drawn regions count as diagrams.

    Deliberately conservative about embedded images — without semantics a
    pasted photo and a pasted map look the same, so only region renders and
    full-slide visuals (which the extractor already vetted for drawing content)
    get an includable score.
    """
    for c in candidates:
        if c.classified:
            continue
        c.classified = True
        c.visual_type = "diagram"
        c.topic = (c.slide_heading or "").strip()[:120]
        c.caption = c.topic
        c.description = f"Visual region from slide {c.slide_no}"
        c.relevance = 0.7 if c.origin in ("region_render", "full_slide") else 0.5
