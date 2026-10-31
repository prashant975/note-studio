"""A deterministic stand-in for the PW proxy, for offline parity tests.

It replaces ``pw_access.gemini_generate`` - the single point where the hand-drawn
notes engine leaves the computer - so everything above it runs for real: the
prompt, the schema, the proxy transport, repair, the PDF renderer, fonts and
branding. Every request is recorded as a SHA-256 of its exact JSON, so two runs
that send byte-identical prompts produce identical call lists.

No network, no cost. Used by tests/parity/upload_driver.py in a child process.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json

CALLS: list[dict] = []
KEEP_REQUESTS = False          # a test sets True to read the exact prompts sent
REQUESTS: list[dict] = []

FAKE_NOTES = {
    "output_language": "",
    "lecture_no": "",
    "subject": "Physics",
    "title": "Synthetic Release QA",
    "audience": "",
    "faculty": "",
    "quote": "Small steps every day.",
    "sections": [
        {"heading": "First idea", "source_local_page": 1, "source_order": 1,
         "points": ["The **first** idea comes before the second.", "Order is kept as taught."],
         "tricks": []},
        {"heading": "Second idea", "source_local_page": 1, "source_order": 2,
         "points": ["The **second** idea follows the first.", "Each step builds on the last."],
         "tricks": ["Remember: first, then second."]},
    ],
    "mindmap": {"center": "Ideas in order",
                "branches": [{"label": "First", "children": ["comes first"]},
                             {"label": "Second", "children": ["follows"]}]},
}


def _pdf_pages(request: dict) -> int:
    """Pages of the PDF attached to a request (for 'every page reviewed')."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return 1
    for content in request.get("contents") or []:
        for part in content.get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if "pdf" in str(inline.get("mimeType") or inline.get("mime_type") or ""):
                try:
                    return len(PdfReader(io.BytesIO(base64.b64decode(inline["data"]))).pages)
                except Exception:
                    return 1
    return 1


def _schema_keys(request: dict) -> set:
    config = request.get("generationConfig") or {}
    schema = config.get("responseSchema") or config.get("response_schema") or {}
    return set((schema.get("properties") or {}).keys())


def answer(request: dict) -> tuple[str, dict]:
    """(kind, response object) for one request, chosen by its schema."""
    keys = _schema_keys(request)
    pages = list(range(1, _pdf_pages(request) + 1))
    if "sections" in keys:
        return "notes", FAKE_NOTES
    if {"reviewed_pages", "edits"} <= keys:
        return "quality", {"reviewed_pages": pages, "unreadable_pages": [], "issues": [], "edits": []}
    if "section_groups" in keys:
        return "editorial", {"reviewed_pages": pages, "unreadable_pages": [], "repetitions": [],
            "section_groups": [{"section_indices": [i], "source_page": 1, "source_order": i,
                                "evidence": "Offline source fixture."} for i in range(len(FAKE_NOTES["sections"]))],
            "exclusions": [], "corrections": []}
    if "reviewed_pages" in keys:
        return "coverage", {"reviewed_pages": pages, "unreadable_pages": [], "issues": [], "repetitions": []}
    if keys == {"repetitions"}:
        return "repetition", {"repetitions": []}
    text = json.dumps(request, sort_keys=True)
    if '"sections"' in text:
        return "notes", FAKE_NOTES
    return "other", {}


def gemini_generate(token_provider, *, model, request, filename="", input_unit="", count=None, task_id=""):
    body = json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")
    kind, obj = answer(request)
    CALLS.append({"kind": kind, "model": model, "sha256": hashlib.sha256(body).hexdigest(),
                  "bytes": len(body), "input_unit": input_unit, "count": count})
    if KEEP_REQUESTS:
        REQUESTS.append(request)
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps(obj, ensure_ascii=False)}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 400, "totalTokenCount": 1400},
        "modelVersion": model,
        "cost_inr": 1.25,
    }


def install(pw_access_module) -> None:
    """Swap the proxy call, and make the access check a no-op token."""
    pw_access_module.gemini_generate = gemini_generate
