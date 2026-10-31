"""Literal, page-by-page visual transcription for scanned B.Sc. lectures.

B.Sc. uses this path by default with the normal PW AI credit disclosure. It never
summarizes a whole lecture in one answer. All pages and raw responses are recorded; unreadable material and
all transcribed wording require review against the source.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from . import ai

REVISION = "literal-pages-v1"

SYSTEM = """Transcribe the academic content of this ONE lecture page, in its original reading order.
Keep the exact source wording, names, numbers, units, equations, examples and handwritten additions.
Do not paraphrase, explain, complete a derivation, correct facts, summarize, add definitions or invent
a Key Takeaways section. Ignore only repeating logos, teacher portraits and purely promotional text.
Return each heading, paragraph, list point, equation, table and diagram separately. A heading is its
exact source heading. A list point includes its source number or bullet. Join words split only by a
physical line wrap. Use ordinary text for prose. For equations, tables and diagrams, retain a precise
source bounding box including all its labels and source caption so the original material can be
preserved without re-drawing or guessing symbols.
Coordinates are integers from 0 to 1000 relative to the whole page, [left, top, right, bottom].
For paragraph/list/heading items, text is a literal transcription. For a diagram/table/equation, text
is only its exact source caption, or empty if none. style may suggest definition, concept, formula,
warning, note, summary or none; this changes appearance only. Mark unreadable handwriting explicitly
in unreadable; never silently omit it or guess it. A title-only academic page still has its title.
nonacademic is true only for a page containing no academic content. If unsure, preserve it as a diagram.
Answer one JSON object with items, unreadable, and nonacademic. Every item has kind, text, bbox, style."""


def schema() -> dict:
    return {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["heading", "paragraph", "list", "equation", "table", "diagram"]},
            "text": {"type": "string"},
            "bbox": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
            "style": {"type": "string", "enum": ["none", "definition", "concept", "formula", "warning", "note", "summary"]}},
            "required": ["kind", "text", "bbox", "style"]}},
        "unreadable": {"type": "array", "items": {"type": "string"}},
        "nonacademic": {"type": "boolean"}}, "required": ["items", "unreadable", "nonacademic"]}


def validate(found: dict) -> list[dict]:
    if not isinstance(found.get("items"), list) or not isinstance(found.get("unreadable"), list) or \
            not isinstance(found.get("nonacademic"), bool):
        raise ai.AIError("A scanned page returned an incomplete transcription.")
    if not all(isinstance(t, str) for t in found["unreadable"]) or \
            (found["nonacademic"] and (found["items"] or found["unreadable"])):
        raise ai.AIError("A scanned page returned contradictory content or an invalid unreadable-text report.")
    if not found["items"] and not found["nonacademic"]:
        raise ai.AIError("A lecture page came back empty. It was not skipped.")
    for item in found["items"]:
        if not isinstance(item, dict) or item.get("kind") not in {"heading", "paragraph", "list", "equation", "table", "diagram"}:
            raise ai.AIError("A scanned page returned an invalid content item.")
        box = item.get("bbox")
        if not isinstance(box, list) or len(box) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool)
                                                                and 0 <= n <= 1000 for n in box) or \
                box[0] == box[2] or box[1] == box[3]:
            raise ai.AIError("A source equation or diagram has an invalid page position.")
        ordered = [min(box[0],box[2]), min(box[1],box[3]), max(box[0],box[2]), max(box[1],box[3])]
        if ordered != box:
            item["bbox"] = ordered
            found.setdefault("layout_warnings", []).append("A source bounding-box corner order was normalized; content was retained.")
        if not isinstance(item.get("text"), str) or (item["kind"] in {"heading", "paragraph", "list"} and not item["text"].strip()):
            raise ai.AIError("A source text item came back empty. It was not dropped.")
        if item.get("style") not in {"none", "definition", "concept", "formula", "warning", "note", "summary"}:
            raise ai.AIError("A scanned page returned an invalid layout style.")
    return found["items"]


def cache_identity(pdf_bytes: bytes) -> dict:
    return {"source_sha256": hashlib.sha256(pdf_bytes).hexdigest(), "revision": REVISION,
            "prompt_sha256": hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest()}


def load_cached(pdf_bytes: bytes, numbers: list[int], folder: Path) -> dict[int, dict]:
    marker = folder / "source.json"
    if not marker.is_file() or json.loads(marker.read_text("utf-8")) != cache_identity(pdf_bytes):
        return {}
    result = {}
    for number in numbers:
        path = folder / f"page-{number+1:04d}-review.json"
        if path.is_file():
            found = json.loads(path.read_text("utf-8"))
            validate(found)
            if found.get("checked") is True:
                result[number] = found
    return result


def pages(pdf_bytes: bytes, numbers: list[int], folder: Path, session, models: list[str], say) -> dict[int, dict]:
    import fitz

    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "source.json"
    identity = cache_identity(pdf_bytes)
    if marker.is_file():
        if json.loads(marker.read_text("utf-8")) != identity:
            raise ai.AIError("Saved page transcriptions belong to a different source or prompt.")
    elif any(folder.glob("page-*.json")):
        raise ai.AIError("Existing page responses need source confirmation before reuse.")
    else:
        marker.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    result = load_cached(pdf_bytes, numbers, folder)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        for number in numbers:
            if number in result:
                say(f"Reusing checked source page {number+1} without another AI charge", None)
                continue
            say(f"Transcribing source page {number+1} of {len(pdf)} without rewriting", None)
            existing = folder / f"page-{number+1:04d}.json"
            if existing.is_file():
                found = json.loads(existing.read_text("utf-8"))
            else:
                found = None
            with fitz.open() as single:
                single.insert_pdf(pdf, from_page=number, to_page=number)
                previous = session.pages
                session.pages = 1
                try:
                    if found is None:
                        found = session.json(f"transcription page {number+1}", models, SYSTEM,
                                         [ai.pdf_part(single.tobytes()), {"text": f"Source page {number+1}. Transcribe it literally."}],
                                         schema(), max_tokens=12000)
                finally:
                    session.pages = previous
            (folder / f"page-{number+1:04d}.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
            validate(found)
            review_schema = schema()
            review_schema["properties"].update(checked={"type": "boolean"})
            review_schema["required"].append("checked")
            previous = session.pages
            session.pages = 1
            try:
                checked = session.json(f"transcription review page {number+1}", models,
                    SYSTEM + "\nNow VERIFY the proposed transcription against the entire page, including handwriting. "
                    "Return the complete corrected items, all omitted readable content, unreadable, nonacademic, "
                    "and checked=true only after checking the whole page. Correct transcription errors only, not source facts.",
                    [ai.pdf_part(single_bytes(pdf, number)), {"text": "Proposed literal transcription:\n" + json.dumps(found, ensure_ascii=False)}],
                    review_schema, max_tokens=12000)
            finally:
                session.pages = previous
            (folder / f"page-{number+1:04d}-review.json").write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
            validate(checked)
            if checked.get("checked") is not True:
                raise ai.AIError("A scanned lecture page could not be verified against its source.")
            result[number] = checked
    return result


def single_bytes(pdf, number: int) -> bytes:
    import fitz

    with fitz.open() as single:
        single.insert_pdf(pdf, from_page=number, to_page=number)
        return single.tobytes()
