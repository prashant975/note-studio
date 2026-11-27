"""Requirements are layered text, resolved in one place (doc 07).

    1 engine     the engine's own prompt (referenced, never edited, never copied in)
    2 type       note_studio/note_types/<type>/rules.txt
    3 category   categories/<cat>/rules/all-subjects.txt
    4 subject    categories/<cat>/rules/<subject>.txt
    5 cat x type categories/<cat>/rules/types/<type>.txt
    6 profile    instructions saved on a sheet profile
    7 run        this run's "Extra instructions" box

Lines starting with '#' are comments and are dropped; an empty layer adds
nothing. Switches merge in the same order, later wins. The fingerprint hashes
every layer 2-7 text and the switches: a changed rule never reuses stale notes.

Each engine adapter takes what it needs: the hand-drawn notes engine applies
layers 3-4 itself (the copy's categories.py reads the packs), so its adapter
sends layers 2, 5, 6 and 7 as the engine's existing "instructions" input.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import categories, note_types, paths

LAYERS = ("engine", "type", "category", "subject", "category_type", "profile", "run")
# Engines that receive only some layers. PW Books: the NEET-JEE category's general
# rule files are the hand-drawn notes engine's own format rules, so they are not
# sent to it; its own rules go in rules/types/pw_books_handwritten.txt.
# Audio notes: a category's notes rules are about written notes, not a spoken episode; its own
# words for the episode go in rules/types/audio_notes.txt.
APPLIED = {"pw_books": ("type", "category_type", "profile", "run"),
           "audio": ("type", "category_type", "profile", "run")}
ENGINE_PROMPTS = {"notesbot": "engines/shortnotes/notesbot/prompts.py", "upsc": "engines/upsc/prompts/",
                  "pw_books": "engines/pw_books/prompts/", "notes_qc": "engines/notes_qc/src/qc_prompts.py",
                  "typeset": "note_studio/typeset/prompts.py", "audio": "note_studio/audio/prompt.txt"}
_ALIASES = {"maths": "mathematics", "math": "mathematics", "sst": "social_science",
            "social science": "social_science", "social-science": "social_science"}


def without_comments(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#")).strip()


def normalize_subject(subject: str) -> str:
    value = str(subject or "").strip().lower()
    value = _ALIASES.get(value, value)
    return "" if value in ("", "auto", "other", "sheet") else value


@dataclass
class Layer:
    layer: str
    path: str           # where it came from, for Preview requirements
    text: str


@dataclass
class Resolved:
    category: str
    note_type: str
    subject: str
    language: str
    layers: list[Layer] = field(default_factory=list)
    switches: dict = field(default_factory=dict)
    fingerprint: str = ""
    engine: str = ""

    def applied(self) -> list[Layer]:
        """The layers with text that this note type's engine actually receives."""
        names = APPLIED.get(self.engine)
        return [l for l in self.layers if l.text and l.layer != "engine" and (names is None or l.layer in names)]

    def text(self, *names: str) -> str:
        """The texts of the named layers, in layer order, one blank line apart."""
        return "\n\n".join(l.text for l in self.layers if l.layer in names and l.text)

    def to_dict(self) -> dict:
        return {"category": self.category, "note_type": self.note_type, "subject": self.subject,
                "language": self.language, "fingerprint": self.fingerprint, "switches": self.switches,
                "layers": [{"layer": l.layer, "path": l.path, "text": l.text} for l in self.layers],
                "applied": [l.layer for l in self.applied()]}


def _file_layer(name: str, path: Path, shown: str) -> Layer | None:
    try:
        text = without_comments(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return Layer(name, shown, text) if text else None


def resolve(category: str, note_type: str, subject: str = "", language: str = "",
            profile: dict | None = None, extra: str = "") -> Resolved:
    pack = categories.get(category) or {}
    entry = note_types.get(note_type) or {}
    subject_id = normalize_subject(subject)
    engine = entry.get("engine", "")
    result = Resolved(category, note_type, subject_id, language, engine=engine)
    if engine:
        result.layers.append(Layer("engine", ENGINE_PROMPTS.get(engine, engine), ""))

    folder = Path(pack["folder"]) / str(pack.get("rules_dir") or "rules") if pack else None
    candidates = [("type", paths.PACKAGE_DIR / "note_types" / note_type / "rules.txt",
                   f"note_types/{note_type}/rules.txt")]
    if folder is not None:
        candidates.append(("category", folder / "all-subjects.txt", f"categories/{category}/rules/all-subjects.txt"))
        if subject_id:
            candidates.append(("subject", folder / f"{subject_id}.txt", f"categories/{category}/rules/{subject_id}.txt"))
        candidates.append(("category_type", folder / "types" / f"{note_type}.txt",
                           f"categories/{category}/rules/types/{note_type}.txt"))
    for name, path, shown in candidates:
        layer = _file_layer(name, path, shown)
        if layer:
            result.layers.append(layer)
    profile_text = without_comments(str((profile or {}).get("instructions") or ""))
    if profile_text:
        result.layers.append(Layer("profile", "sheet profile", profile_text))
    run_text = str(extra or "").strip()
    if run_text:
        result.layers.append(Layer("run", "this run", run_text))

    switches = dict(entry.get("switches") or {})
    if pack:
        try:
            formats = json.loads((Path(pack["folder"]) / "switches.json").read_text(encoding="utf-8")).get("formats") or {}
        except (OSError, ValueError):
            formats = {}
        switches.update(formats.get(f"{category}/all-subjects", {}))
        if subject_id:
            switches.update(formats.get(f"{category}/{subject_id}", {}))
    switches.update((profile or {}).get("switches") or {})
    result.switches = switches

    hashed = [[l.layer, l.path, l.text] for l in result.layers if l.layer != "engine"]
    if hashed or switches:
        payload = json.dumps({"layers": hashed, "switches": switches}, sort_keys=True, ensure_ascii=False)
        result.fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return result
