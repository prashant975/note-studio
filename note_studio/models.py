"""AI model order per note type and stage (Settings -> AI models).

Hand-drawn notes: the first model makes the notes. Legacy alternate choices
are kept for existing settings, but the current original's budget-stop rule
applies: an exhausted gateway budget stops the run without switching models.

UPSC: the engine's own rule. Its second model is used when the first is not
available or stays busy after the engine's retries; speech has its own pair,
and slide pictures one model.

Each engine keeps its own default order (note_types.json); Settings can reorder it.
"""
from __future__ import annotations

import json
import time

from . import db, note_types

KEY = "model_order"
STAGES = {"notes": "Writing the notes", "speech": "Listening to the class recording",
          "vision": "Reading the slide pictures", "qc": "Checking the notes", "images": "Looking at slide pictures",
          "figures": "Drawing the figures", "voice": "Speaking the script"}
IMAGE_STAGES = ("images", "figures")       # picture models only: the type's own list
OWN_LIST_STAGES = IMAGE_STAGES + ("voice",)   # speech models too: never PW's text models
# How many models each engine can use per stage, and what the first and the next do.
RULES = {
    "notesbot": {"slots": {"notes": 4}, "first": "Makes the notes", "next": "Saved alternate (budget failures stop)"},
    "upsc": {"slots": {"notes": 2, "speech": 2, "images": 1}, "first": "Used first",
             "next": "If the first is unavailable or stays busy"},
    # The PW Books app's router (src/model_router.py): primary + two fallbacks per task.
    "pw_books": {"slots": {"notes": 3, "vision": 3, "qc": 3, "images": 1}, "first": "Used first",
                 "next": "If the first is unavailable (the app's own routing)"},
    # The QC tool: its model, then its fallback; Claude only when PW says the Gemini budget is spent.
    "notes_qc": {"slots": {"notes": 2}, "first": "Used first", "next": "If the first is unavailable or stays busy"},
    # Typeset notes (note_studio/typeset/ai.py): each stage tries its next model when the first fails.
    "typeset": {"slots": {"notes": 2, "qc": 2, "figures": 2}, "first": "Used first",
                "next": "If the first is unavailable or stays busy"},
    # Audio notes (note_studio/audio): the script, its check and the voices, each with a next model.
    "audio": {"slots": {"notes": 2, "qc": 2, "voice": 2}, "first": "Used first",
              "next": "If the first is unavailable or stays busy"},
}
EXTRA_LABELS = {"gemini-3.1-flash-tts-preview": "Gemini 3.1 Flash voices",
                "gemini-2.5-flash-preview-tts": "Gemini 2.5 Flash voices",
                "gemini-3.5-flash": "Gemini 3.5 Flash", "gemini-2.5-flash-image": "Gemini 2.5 Flash Image",
                "gemini-3.1-flash-image": "Gemini 3.1 Flash Image", "gemini-2.5-pro": "Gemini 2.5 Pro"}
GEMINI_ONLY = ("pw_books", "notes_qc", "typeset", "audio")


def _rules(type_id: str) -> dict:
    return RULES.get((note_types.get(type_id) or {}).get("engine", ""), RULES["notesbot"])


def stages(type_id: str) -> list[str]:
    """The stages a type has a model order for, in STAGES order."""
    have = (note_types.get(type_id) or {}).get("models") or {}
    return [stage for stage in STAGES if stage in have]


def slots(type_id: str, stage: str = "notes") -> int:
    return int(_rules(type_id)["slots"].get(stage, 1))


def hints(type_id: str) -> tuple[str, str]:
    rules = _rules(type_id)
    return rules["first"], rules["next"]


def selectable(type_id: str = "", stage: str = "notes") -> list[dict]:
    """The models one stage may use: PW's list, plus the type's own defaults.
    Speech is Gemini only (the proxy has no GPT speech route), and so are PW
    Books (its kit calls Gemini only) and the typeset notes (Gemini reads the
    lecture PDF); pictures keep the engine's own image models."""
    from sheets_bridge.pw_transport import SELECTABLE_MODELS

    gemini_only = stage == "speech" or (note_types.get(type_id) or {}).get("engine") in GEMINI_ONLY
    listed = [] if stage in OWN_LIST_STAGES else [(m, label) for m, label in SELECTABLE_MODELS
                                               if not (gemini_only and m.startswith("gpt"))]
    for model in default_order(type_id, stage) if type_id else []:
        if model not in {m for m, _ in listed}:
            listed.append((model, EXTRA_LABELS.get(model, model)))
    return [{"id": model, "label": label} for model, label in listed]


def _saved() -> dict:
    try:
        value = json.loads(db.get_setting(KEY, "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def default_order(type_id: str, stage: str = "notes") -> list[str]:
    return list(((note_types.get(type_id) or {}).get("models") or {}).get(stage) or [])


def order(type_id: str, stage: str = "notes") -> list[str]:
    saved = (_saved().get(type_id) or {}).get(stage)
    return list(saved) if saved else default_order(type_id, stage)


def set_order(type_id: str, stage: str, chosen: list[str]) -> list[str]:
    allowed = {m["id"] for m in selectable(type_id, stage)}
    clean: list[str] = []
    for model in chosen:
        model = str(model or "").strip()
        if model in allowed and model not in clean:
            clean.append(model)
    if not clean:
        raise ValueError("Choose at least one model.")
    clean = clean[:slots(type_id, stage)]
    saved = _saved()
    saved.setdefault(type_id, {})[stage] = clean
    db.set_setting(KEY, json.dumps(saved, sort_keys=True))
    apply()
    return clean


def apply() -> None:
    """The sheet automation installs its model through the old app's code, which
    takes no fallbacks: give the engine the hand-drawn order's fallbacks to use."""
    from sheets_bridge import pw_transport

    pw_transport.FALLBACKS = tuple(order("concise_handdrawn")[1:])


def resting() -> list[dict]:
    """Providers whose budget was spent recently, and until when."""
    from sheets_bridge import pw_transport

    now = time.time()
    return [{"provider": "GPT" if name == "gpt" else "Gemini", "until": until}
            for name, until in sorted(pw_transport._SPENT.items()) if until > now]
