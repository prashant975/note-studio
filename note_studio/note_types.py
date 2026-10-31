"""The note-type registry (note_types.json): what each type makes, its engine,
its form options, and whether it is ready yet."""
from __future__ import annotations

import json
from functools import lru_cache

from . import engine_catalog, paths

REGISTRY = paths.PACKAGE_DIR / "note_types.json"
TYPE_RULES_DIR = paths.PACKAGE_DIR / "note_types"

LANGUAGE_LABELS = {
    "auto": "Same as the lecture",
    "english": "English",
    "hindi": "Hindi",
    "gujarati": "Gujarati (ગુજરાતી)",
    "sanskrit_hindi": "संस्कृत श्लोक + हिन्दी अर्थ",
    "sanskrit": "संस्कृतम् — केवलं संस्कृतम्",
    "hinglish": "Hinglish",
    "marathi": "Marathi",
}


@lru_cache(maxsize=1)
def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def all_types() -> list[dict]:
    return list(registry()["types"])


def tools() -> list[dict]:
    """Things that act on finished notes rather than make them (Check notes)."""
    return list(registry().get("tools") or [])


def get(type_id: str) -> dict | None:
    """A note type or a tool, by id."""
    return next((t for t in [*all_types(), *tools()] if t["id"] == type_id), None)


def ready(type_id: str) -> bool:
    entry = get(type_id)
    return bool(entry and entry.get("status") == "ready" and available(type_id))


def available(type_id: str) -> bool:
    entry = get(type_id)
    return bool(entry and engine_catalog.available(entry["engine"]))


def engine_label(engine_id: str) -> str:
    return registry()["engines"].get(engine_id, {}).get("label", engine_id)


def language_label(code: str) -> str:
    return LANGUAGE_LABELS.get(code, code.replace("_", " ").title())


def option_defaults(type_id: str) -> dict:
    entry = get(type_id) or {}
    return {name: spec.get("default", "" if spec.get("type") != "bool" else False)
            for name, spec in (entry.get("options") or {}).items()}
