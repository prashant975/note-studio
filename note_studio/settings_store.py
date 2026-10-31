"""Values NoteCraft keeps for the engines, and the one-time import of the old
app's settings.

The old Short Notes app kept its knobs (model, chunk size, images, cache...) in
`.env` and in its launcher's defaults. On first start NoteCraft copies the
NON-SECRET knobs from the original project's `.env` into its own settings, once;
the file itself is never changed. Secrets (keys, passwords, tokens, client
files, sheet ids) are never read into NoteCraft.

The engine reads these from the environment once, when it is first imported, so
`apply_engine_environment()` runs at start-up, before any engine import:
    a value already in the environment  >  imported value  >  engine default
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import db, old_app, paths

IMPORT_KEY = "imported_old_env"
VALUES_KEY = "engine_values_shortnotes"

# Knobs the hand-drawn notes engine and its sheet automation read (names from
# engines/shortnotes/notesbot/config.py and sheets_bridge/settings.py). Only
# these are ever imported.
KNOBS = (
    "PW_PROXY_MODEL", "PW_PROXY_REASONING_EFFORT", "IMAGE_MODEL", "GENERATION_MAX_OUTPUT_TOKENS",
    "VISUAL_SOURCE_CHUNK_MAX_BYTES", "MAX_CHUNKS", "MAX_IMAGES", "MAX_PDF_MB", "NOTES_IMAGES",
    "NOTES_DESIGN", "NOTES_QC", "NOTES_LANGUAGE", "DIAGRAM_MODE", "DEFAULT_SUBJECT", "MATHPIX_ENABLED",
    "VISUAL_PAGE_RENDER_SCALE", "VISUAL_MODEL_TIMEOUT_SECONDS", "VISUAL_PIPELINE_DEADLINE_SECONDS",
    "LLM_COST_INPUT_INR_PER_1M", "LLM_COST_OUTPUT_INR_PER_1M", "IMAGE_COST_INR_PER_IMAGE",
    "SHEETS_BRIDGE_CACHE", "SHEETS_BRIDGE_GROUP_WORKERS", "SHEETS_BRIDGE_PAGES_PER_CALL",
    "SHEETS_BRIDGE_SOURCE_REVIEW", "SHEETS_BRIDGE_MINDMAP", "SHEETS_BRIDGE_NAME_WATERMARK",
    "SHEETS_BRIDGE_WATERMARK_OPACITY", "SHEETS_BRIDGE_LOG_SHEET", "SHEETS_BRIDGE_LOG_TAB",
    "SHEETS_BRIDGE_SIGNATURE", "SHEETS_BRIDGE_MAX_COST_INR",
)


# Plain words for the values people change most (Settings -> Values you can change).
KNOB_INFO = {
    "MAX_CHUNKS": ("Most parts one lecture is split into", "number"),
    "MAX_IMAGES": ("Most drawn pictures in one set of notes", "number"),
    "NOTES_IMAGES": ("Draw pictures in the notes (true / false)", "bool"),
    "MAX_PDF_MB": ("Largest lecture PDF, in MB", "number"),
    "NOTES_QC": ("Extra AI quality pass (true / false)", "bool"),
    "SHEETS_BRIDGE_CACHE": ("Reuse notes already made for a sheet row (on / off)", "onoff"),
    "SHEETS_BRIDGE_SIGNATURE": ("Signature on every sheet row's notes (empty: from the sheet)", "text"),
    "SHEETS_BRIDGE_MAX_COST_INR": ("Spend limit for one sheet run, in rupees (empty: none)", "number"),
    "SHEETS_BRIDGE_PAGES_PER_CALL": ("Lecture pages sent in one AI call (sheets)", "number"),
    "SHEETS_BRIDGE_GROUP_WORKERS": ("Parts of a lecture made at the same time (sheets)", "number"),
    "SHEETS_BRIDGE_SOURCE_REVIEW": ("Check the notes against the lecture (sheets)", "text"),
    "SHEETS_BRIDGE_MINDMAP": ("Mind map at the end of the notes (sheets)", "text"),
    "SHEETS_BRIDGE_NAME_WATERMARK": ("Teacher-name watermark (sheets)", "text"),
    "SHEETS_BRIDGE_WATERMARK_OPACITY": ("Watermark strength (sheets)", "number"),
    "SHEETS_BRIDGE_LOG_SHEET": ("Run-log sheet id", "text"),
    "SHEETS_BRIDGE_LOG_TAB": ("Run-log tab name", "text"),
    "PW_PROXY_REASONING_EFFORT": ("How hard GPT models think (low / medium / high)", "text"),
}


def save_values(values: dict) -> list[str]:
    """Save changed values (only known knobs). Returns plain-words problems; nothing
    is saved when there is one. Applied the next time NoteCraft opens."""
    current = imported_values()
    defaults = old_app.engine_defaults()
    problems = []
    for key, raw in values.items():
        if key not in KNOBS or key == "PW_PROXY_MODEL":
            continue
        value = str(raw or "").strip()
        kind = KNOB_INFO.get(key, ("", "text"))[1]
        if value and kind == "number":
            try:
                float(value)
            except ValueError:
                problems.append(f"{KNOB_INFO[key][0]}: write a number.")
                continue
        if value and kind == "bool" and value.lower() not in ("true", "false"):
            problems.append(f"{KNOB_INFO[key][0]}: write true or false.")
            continue
        if value and kind == "onoff" and value.lower() not in ("on", "off"):
            problems.append(f"{KNOB_INFO[key][0]}: write on or off.")
            continue
        if value and value != defaults.get(key, ""):
            current[key] = value
        else:
            current.pop(key, None)            # empty or the default: back to the old app's value
    if not problems:
        db.set_setting(VALUES_KEY, json.dumps(current, sort_keys=True))
    return problems


def _read_env_file(path: Path) -> dict:
    """KEY=value lines, the same way the engine's own loader reads them."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def import_old_env_once() -> dict:
    """Copy the old project's non-secret knobs once. Returns what was imported."""
    if db.get_setting(IMPORT_KEY):
        return imported_values()
    source = paths.shortnotes_original() / ".env"
    found = _read_env_file(source)
    values = {key: found[key] for key in KNOBS if key in found and found[key] != ""}
    db.set_setting(VALUES_KEY, json.dumps(values, sort_keys=True))
    db.set_setting(IMPORT_KEY, json.dumps({"from": str(source), "at": int(time.time()),
                                           "found": source.is_file(), "count": len(values)}))
    return values


def imported_values() -> dict:
    try:
        value = json.loads(db.get_setting(VALUES_KEY, "{}"))
    except ValueError:
        return {}
    return {k: str(v) for k, v in value.items() if k in KNOBS} if isinstance(value, dict) else {}


def import_record() -> dict:
    try:
        return json.loads(db.get_setting(IMPORT_KEY, "{}")) or {}
    except ValueError:
        return {}


def engine_values() -> list[dict]:
    """Every value the engine starts with, and where it came from (for Settings)."""
    imported = imported_values()
    defaults = old_app.engine_defaults()
    rows = []
    for key in sorted(set(imported) | set(defaults) | set(KNOB_INFO)):
        if key == "PW_PROXY_MODEL":
            continue                          # chosen in Settings -> AI models
        source = ("set in NoteCraft" if key in imported else
                  "the old app's built-in default" if key in defaults else "not set: the engine's own default")
        label, kind = KNOB_INFO.get(key, (key, "text"))
        rows.append({"key": key, "label": label, "kind": kind, "value": imported.get(key, defaults.get(key, "")),
                     "source": source, "in_use": os.environ.get(key, ""), "main": key in KNOB_INFO})
    return rows


def apply_engine_environment() -> None:
    """Set the engine's knobs for this process; a value set outside always wins."""
    for key, value in imported_values().items():
        os.environ.setdefault(key, value)
    for key, value in old_app.engine_defaults().items():
        os.environ.setdefault(key, value)
