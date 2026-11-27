"""Category packs: one folder per exam in ~/.note_studio/categories/<id>/ (doc 07).

    category.json   id, label, enabled, subjects, languages, note types, branding
    rules/*.txt     plain commands: all-subjects.txt, <subject>.txt
    rules/types/<note type>.txt   category x note type commands (optional)
    switches.json   {"formats": {"<id>/<subject>": {...}}} switches the engine enforces

Built-in packs are written on first start, only if absent, so an operator's
edits and new categories survive updates (a built-in file never edited since an
earlier version shipped it is brought up to date, with a .bak). NEET-JEE and Foundation are seeded
byte-for-byte from the engine copy's notesbot/format_rules/, which itself is
never edited; the copy's categories.py reads the packs (an allowed change).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import engine_catalog, note_types, paths

PACKS_ENV = "NOTE_STUDIO_CATEGORY_PACKS"
BUILTIN_DIR = paths.PACKAGE_DIR / "packs"

# The two categories the hand-drawn notes engine has today. Labels must stay
# exactly as the engine prints them into the prompt ("CATEGORY: NEET-JEE notes").
HANDDRAWN_LANGUAGES = ["auto", "english", "hindi", "gujarati", "sanskrit_hindi", "sanskrit", "hinglish"]
BUILTIN = {
    "neet_jee": {
        "label": "NEET-JEE", "order": 1, "legacy_ids": ["neet", "jee"],
        # The old Upload page's NEET-JEE subject menu (public/index.html).
        "subject_ids": ["chemistry", "physics", "biology", "mathematics", "sanskrit"],
        "summary": "Hand-drawn concise notes for NEET and JEE lectures.",
        "note_types": ["concise_handdrawn", "pw_books_handwritten"],          # PW Books since Phase 7
    },
    "foundation": {
        "label": "Foundation", "order": 2, "legacy_ids": [],
        "subject_ids": None,            # rules.json category_subjects.foundation
        "summary": "Class 6-10 notes, with Maths solved examples and Sanskrit verses kept.",
    },
}


def packs_dir() -> Path:
    path = paths.home() / "categories"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rules_source() -> Path:
    return engine_catalog.engine_dir("shortnotes") / "notesbot" / "format_rules"


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed() -> list[str]:
    """Write the built-in packs that are missing. Returns the ids written."""
    root = packs_dir()
    written = []
    source = _rules_source()
    try:
        config = json.loads((source / "rules.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    labels = config.get("subject_labels", {})
    for cid, spec in BUILTIN.items():
        folder = root / cid
        if folder.exists():
            continue
        (folder / "rules").mkdir(parents=True)
        for rule in sorted((source / cid).glob("*.txt")) if (source / cid).is_dir() else []:
            shutil.copy2(rule, folder / "rules" / rule.name)          # byte for byte
        formats = {k: v for k, v in config.get("formats", {}).items() if k.startswith(f"{cid}/")}
        _write_json(folder / "switches.json", {"formats": formats})
        ids = spec["subject_ids"] or config.get("category_subjects", {}).get(cid, [])
        _write_json(folder / "category.json", {
            "id": cid, "label": spec["label"], "enabled": True, "builtin": True, "order": spec["order"],
            "summary": spec["summary"], "legacy_ids": spec["legacy_ids"],
            "subjects": [{"id": s, "label": labels.get(s, s.replace("_", " ").title())} for s in ids],
            "subject_auto": True,
            "languages": HANDDRAWN_LANGUAGES, "language_required": False,
            "note_types": list(spec.get("note_types") or ["concise_handdrawn"]), "default_note_type": "concise_handdrawn",
            "inputs": ["pdf_upload"],
            "branding": {"signature_label": "Teacher name", "logo": "PW logo (the engine's own)"},
            "rules_dir": "rules", "seeded_from": "engines/shortnotes/notesbot/format_rules/" + cid,
        })
        written.append(cid)
    for name in ("upsc", "bsc", "csir_net", "_template"):
        if not (root / name).exists() and (BUILTIN_DIR / name).is_dir():
            shutil.copytree(BUILTIN_DIR / name, root / name, copy_function=shutil.copy2)
            written.append(name)
    return written + _upgrade_builtin(root) + _offer_new_types(root) + _offer_gujarati(root)


def _offer_gujarati(root: Path) -> list[str]:
    """Extend untouched built-in language lists, retaining all other pack data."""
    upgraded = []
    previous = [code for code in HANDDRAWN_LANGUAGES if code != "gujarati"]
    for cid in BUILTIN:
        path = root / cid / "category.json"
        if not path.is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("builtin") is True and meta.get("languages") == previous:
            shutil.copy2(path, path.with_name("category.json.before-gujarati.bak"))
            meta["languages"] = list(HANDDRAWN_LANGUAGES)
            _write_json(path, meta)
            upgraded.append(cid)
    return upgraded


def _offer_new_types(root: Path) -> list[str]:
    """A built-in pack seeded before a note type was added to it gets that type,
    but only while its list is still exactly what was seeded (an operator's
    change is left as it is). A .bak is kept."""
    offered = []
    for cid, spec in BUILTIN.items():
        wanted = list(spec.get("note_types") or [])
        path = root / cid / "category.json"
        if not wanted or not path.is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("builtin") is True and meta.get("note_types") == ["concise_handdrawn"] and wanted != meta["note_types"]:
            shutil.copy2(path, path.with_name("category.json.bak"))
            meta["note_types"] = wanted
            _write_json(path, meta)
            offered.append(cid)
    return offered


# Built-in pack files as an earlier version shipped them (SHA-256). One that is
# still exactly that, never edited, is replaced by the current version, with a
# .bak kept; an edited one is left as the operator made it.
SHIPPED_BEFORE = {
    "upsc/category.json": {"d3280953390d424e8e16f9e6392203233bda66b0b3fc90ce2498697e7f0b7060"},   # Phase 2, off
}


def _sha256(path: Path) -> str:
    # Git may check the same shipped JSON out as CRLF on Windows. Newline
    # conversion is not an operator edit; all other bytes must still match.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _upgrade_builtin(root: Path) -> list[str]:
    upgraded = []
    for relative, old_hashes in SHIPPED_BEFORE.items():
        target, source = root / relative, BUILTIN_DIR / relative
        if not (target.is_file() and source.is_file()):
            continue
        current = _sha256(target)
        if current in old_hashes and current != _sha256(source):
            shutil.copy2(target, target.with_name(target.name + ".bak"))
            shutil.copy2(source, target)
            upgraded.append(relative.split("/")[0])
    return upgraded


def _load(folder: Path) -> dict | None:
    try:
        meta = json.loads((folder / "category.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("id") != folder.name:
        return None
    meta["folder"] = str(folder)
    return meta


def all_packs(include_disabled: bool = True) -> list[dict]:
    packs = [_load(f) for f in sorted(packs_dir().iterdir()) if f.is_dir() and not f.name.startswith(("_", "."))]
    packs = [p for p in packs if p and (include_disabled or p.get("enabled") is True)]
    return sorted(packs, key=lambda p: (p.get("order", 1000), p["id"]))


def get(category_id: str) -> dict | None:
    folder = packs_dir() / str(category_id or "")
    if not category_id or folder.name.startswith(("_", ".")) or folder.parent != packs_dir():
        return None
    return _load(folder) if folder.is_dir() else None


def pw_logo(pack: dict | None) -> bool:
    """Whether the category's typeset (B.Sc.-style) notes carry the PW logo in the
    corner and as the watermark, as the NEET-JEE notes do. The pack's own
    branding.pw_logo (Settings -> Categories) wins. Otherwise a category added
    from Settings (Nursing...) has it and a built-in one keeps its samples' look."""
    if not pack:
        return False
    value = (pack.get("branding") or {}).get("pw_logo")
    return value if isinstance(value, bool) else pack.get("builtin") is not True


def branding_from_pdf(pack: dict | None) -> bool:
    """Whether the category's B.Sc.-style notes take their header name and logo from each lecture PDF
    (branding.from_pdf; Settings -> Categories) instead of the category's fixed words (CSIR NET)."""
    return bool(pack) and (pack.get("branding") or {}).get("from_pdf") is True


GDOC_TYPE = "typeset_gdoc"


def gdoc_offered(pack: dict | None) -> bool:
    """Whether the category offers Google Docs with tabs (B.Sc. style). Listed in the pack, it does. Otherwise
    a category whose B.Sc.-style notes carry the PW logo (Nursing, any category added from Settings) offers
    it by itself, since the Google Doc template carries the PW watermark, unless Settings switched it off
    ("offer_gdoc": false). The built-in B.Sc. Wallah pack keeps its own look until switched on."""
    if not pack:
        return False
    listed = pack.get("note_types") or []
    if GDOC_TYPE in listed:
        return True
    return "typeset_notes" in listed and pack.get("offer_gdoc") is not False and pw_logo(pack)


AUDIO_TYPE = "audio_notes"


def audio_offered(pack: dict | None) -> bool:
    """Whether the category offers Audio notes (a chapter's revision podcast). Every category does by
    itself, since it is made from any lecture notes PDFs, unless Settings switched it off ("offer_audio": false)."""
    if not pack:
        return False
    return AUDIO_TYPE in (pack.get("note_types") or []) or pack.get("offer_audio") is not False


def type_ids(pack: dict) -> list[str]:
    """The pack's note type ids, with Google Docs right after the B.Sc.-style notes when it is offered,
    and Audio notes last when it is offered."""
    ids = list(pack.get("note_types") or [])
    if GDOC_TYPE not in ids and gdoc_offered(pack):
        ids.insert(ids.index("typeset_notes") + 1, GDOC_TYPE)
    if AUDIO_TYPE not in ids and audio_offered(pack):
        ids.append(AUDIO_TYPE)
    return ids


def offered_types(pack: dict) -> list[dict]:
    """The pack's note types that exist in the registry, in the pack's order. Audio notes come with a
    category's own notes: a category none of whose own types is in this installation (UPSC in the
    smaller installer) offers nothing, unless the category itself lists Audio notes."""
    found = [note_types.get(t) for t in type_ids(pack)]
    found = [t for t in found if t and note_types.available(t["id"])]
    if all(t["id"] == AUDIO_TYPE for t in found) and AUDIO_TYPE not in (pack.get("note_types") or []):
        return []
    return found


def subject_choices(pack: dict, type_entry: dict | None = None) -> list[dict]:
    """The subject menu: Auto Detect, the pack's subjects, Other (as the old page).
    A note type that names its own subjects (an engine that needs one of them,
    e.g. PW Books) offers only those, and no Auto Detect."""
    subjects = [{"id": s["id"], "label": s.get("label") or s["id"]}
                for s in pack.get("subjects") or [] if isinstance(s, dict) and s.get("id")]
    only = (type_entry or {}).get("subjects")
    if only:
        return [s for s in subjects if s["id"] in only]
    if pack.get("subject_auto", True):
        return [{"id": "auto", "label": "Auto Detect"}, *subjects, {"id": "other", "label": "Other"}]
    return subjects


def language_choices(pack: dict, type_entry: dict) -> list[dict]:
    """The languages both the category and the note type allow, in the category's order. A type
    whose language is part of what it is ("own_languages", e.g. the Hinglish audio notes) keeps its own."""
    if type_entry.get("own_languages"):
        return [{"id": code, "label": note_types.language_label(code)} for code in type_entry.get("languages") or []]
    allowed = set(type_entry.get("languages") or [])
    return [{"id": code, "label": note_types.language_label(code)}
            for code in pack.get("languages") or [] if code in allowed]


def engine_status() -> list[dict]:
    """The engines (Overview, Settings -> System), each "for" the switched-on
    categories that use it; the fixed words while none does yet."""
    packs = all_packs(include_disabled=False)
    rows = []
    for engine in engine_catalog.all_status():
        uses = engine.get("types_engine", engine["id"])
        users = [p["label"] for p in packs if any(t.get("engine") == uses for t in offered_types(p))]
        rows.append(dict(engine, **({"for": ", ".join(users)} if users else {})))
    return rows


def engine_environment() -> dict:
    """Points the engine copy's categories.py at the packs (read at use)."""
    return {PACKS_ENV: str(packs_dir())}
