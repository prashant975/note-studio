"""Exam categories (NEET-JEE / Foundation) and their per-subject note formats.

The formats live as plain files in ``notesbot/format_rules/`` (see the README
there): ``<category>/all-subjects.txt`` and ``<category>/<subject>.txt`` hold
prompt commands, ``rules.json`` holds the switches the code enforces (solved
examples, mind map, one word table) and the subject lists the Studio offers.

A category/subject with no files makes exactly the default concise notes, so
adding a rule never changes any other pair. The commands go to the end of the
generation prompt, after the language and subject lines, which keeps the long
shared prefix cache-friendly.

The category and subject travel as context variables (``generation_profile``),
so they reach every prompt a run builds - page groups, coverage repairs, the
final quality check - without threading new arguments through each call.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from . import generation_profile

DEFAULT = "neet_jee"
CATEGORIES = ("neet_jee", "foundation")
LABELS = {"neet_jee": "NEET-JEE", "foundation": "Foundation"}
# NEET and JEE were separate categories until 2026-09-30. Saved settings,
# stored jobs and scripts may still name them; both mean NEET-JEE now.
LEGACY = {"neet": "neet_jee", "jee": "neet_jee"}

RULES_DIR = Path(__file__).with_name("format_rules")
ALL_SUBJECTS_FILE = "all-subjects"
_SUBJECT_ALIASES = {"maths": "mathematics", "math": "mathematics", "sst": "social_science",
                    "social science": "social_science", "social-science": "social_science"}

_lock = threading.Lock()
_cache: dict = {"signature": None, "rules": None}

# --- Note Studio (only in the engines/shortnotes copy) ----------------------
# When NOTE_STUDIO_CATEGORY_PACKS names a folder of category packs
# (<id>/category.json, <id>/rules/*.txt, <id>/switches.json), the category list,
# the labels and the rule texts come from the packs instead of RULES_DIR. The
# built-in packs are seeded byte-for-byte from RULES_DIR, so prompt_block() and
# fingerprint() come out unchanged for them. Unset, nothing here is used.
PACKS_ENV = "NOTE_STUDIO_CATEGORY_PACKS"
PACK_NOTE_TYPE = "concise_handdrawn"
_BUILTIN = (CATEGORIES, LABELS, LEGACY)


def _packs_dir() -> Path | None:
    value = os.environ.get(PACKS_ENV, "").strip()
    return Path(value) if value and Path(value).is_dir() else None


def _uncommented(text: str) -> str:
    """A pack's rule file may carry '#' comment lines; they never reach a prompt."""
    lines = text.split("\n")
    kept = [line for line in lines if not line.lstrip().startswith("#")]
    return text if len(kept) == len(lines) else "\n".join(kept)


def _pack_rules(packs: Path) -> tuple[dict, tuple]:
    """(rules shaped like the RULES_DIR ones, (categories, labels, legacy))."""
    found = []
    for meta_path in sorted(packs.glob("*/category.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(meta, dict) and meta.get("enabled") is True and meta.get("id") == meta_path.parent.name
                and PACK_NOTE_TYPE in (meta.get("note_types") or [])):
            found.append((meta_path.parent, meta))
    found.sort(key=lambda item: (item[1].get("order", 1000), item[1]["id"]))
    texts: dict[str, str] = {}
    config: dict = {"formats": {}, "category_subjects": {}, "subject_labels": {}}
    ids, labels, legacy = [], {}, {}
    for folder, meta in found:
        category = meta["id"]
        ids.append(category)
        labels[category] = str(meta.get("label") or category)
        for old in meta.get("legacy_ids") or []:
            legacy[str(old)] = category
        for path in sorted((folder / str(meta.get("rules_dir") or "rules")).glob("*.txt")):
            text = _uncommented(path.read_text(encoding="utf-8")).strip()
            if text:
                texts[f"{category}/{path.stem}"] = text
        switches_file = folder / "switches.json"
        if switches_file.is_file():
            formats = json.loads(switches_file.read_text(encoding="utf-8")).get("formats") or {}
            config["formats"].update({k: v for k, v in formats.items() if k.startswith(f"{category}/")})
        subjects = [s for s in meta.get("subjects") or [] if isinstance(s, dict) and s.get("id")]
        if subjects:
            config["category_subjects"][category] = [s["id"] for s in subjects]
        for subject in subjects:
            config["subject_labels"].setdefault(subject["id"], str(subject.get("label") or subject["id"]))
    return {"texts": texts, "config": config}, (tuple(ids), labels, legacy)


def _use_categories(ids: tuple, labels: dict, legacy: dict) -> None:
    global CATEGORIES, LABELS, LEGACY
    if not ids:
        ids, labels, legacy = _BUILTIN
    CATEGORIES, LABELS, LEGACY = ids, labels, legacy


def _signature() -> tuple:
    """(path, size, mtime) of every rule file: cheap, and changes on any edit."""
    packs = _packs_dir()
    if packs is not None:
        entries = [("<packs>", str(packs), 0)]
        for path in sorted(packs.rglob("*")):
            if path.suffix in (".txt", ".json") and path.is_file():
                stat = path.stat()
                entries.append((path.relative_to(packs).as_posix(), stat.st_size, stat.st_mtime_ns))
        return tuple(entries)
    if not RULES_DIR.is_dir():
        return ()
    entries = []
    for path in sorted(RULES_DIR.rglob("*")):
        if path.suffix in (".txt", ".json") and path.is_file():
            stat = path.stat()
            entries.append((path.relative_to(RULES_DIR).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


def _rules() -> dict:
    """{"texts": {"cat/subject": text}, "config": rules.json} - re-read only
    when a file changed, so edits apply to the next run without a restart."""
    signature = _signature()
    with _lock:
        if _cache["signature"] == signature and _cache["rules"] is not None:
            return _cache["rules"]
        packs = _packs_dir()
        if packs is not None:
            rules, chosen = _pack_rules(packs)
            _use_categories(*chosen)
            _cache.update(signature=signature, rules=rules)
            return rules
        if CATEGORIES is not _BUILTIN[0]:
            _use_categories(*_BUILTIN)
        texts: dict[str, str] = {}
        config: dict = {}
        for relative, _size, _mtime in signature:
            path = RULES_DIR / relative
            if relative == "rules.json":
                config = json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix == ".txt" and len(Path(relative).parts) == 2:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    texts[Path(relative).with_suffix("").as_posix()] = text
        rules = {"texts": texts, "config": config}
        _cache.update(signature=signature, rules=rules)
        return rules


def normalize(value: object) -> str:
    """A known category id ("NEET-JEE", "neet" and "jee" all give "neet_jee");
    anything else is refused."""
    if _packs_dir() is not None:
        _rules()                      # Note Studio: the list comes from the packs
    category = "_".join(str(value or "").strip().lower().replace("-", " ").split())
    category = LEGACY.get(category, category)
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    return category


def normalize_subject(value: object) -> str:
    """Subject id used by the rule files; "" for auto/other/unknown."""
    subject = str(value or "").strip().lower()
    subject = _SUBJECT_ALIASES.get(subject, subject)
    return "" if subject in ("", "auto", "other", "sheet") else subject


def current() -> str:
    category = generation_profile.current_category()
    return LEGACY.get(category, category) or DEFAULT


def _resolve(category: str | None) -> str:
    """The given category (an old id mapped to today's), else the current one."""
    return LEGACY.get(category, category) if category else current()


def current_subject(subject_hint: str = "", *, sanskrit_hindi: bool | None = None) -> str:
    """The subject whose rules apply now.

    A Sanskrit lecture written as Hindi notes is "sanskrit" even when the
    sheet named no subject (it was detected from the PDF). Sanskrit rule files
    are written for those Hindi notes, so strict Sanskrit-only notes get none.
    """
    if sanskrit_hindi is None:
        sanskrit_hindi = generation_profile.sanskrit_hindi()
    if sanskrit_hindi:
        return "sanskrit"
    subject = normalize_subject(generation_profile.current_subject() or subject_hint)
    return "" if subject == "sanskrit" else subject


def commands(category: str | None = None, subject: str | None = None) -> str:
    """The category's all-subjects commands, then the subject's own."""
    category = _resolve(category)
    subject = current_subject() if subject is None else normalize_subject(subject)
    texts = _rules()["texts"]
    parts = [texts.get(f"{category}/{ALL_SUBJECTS_FILE}", "")]
    if subject:
        parts.append(texts.get(f"{category}/{subject}", ""))
    return "\n\n".join(part for part in parts if part)


def switches(category: str | None = None, subject: str | None = None) -> dict:
    category = _resolve(category)
    subject = current_subject() if subject is None else normalize_subject(subject)
    formats = _rules()["config"].get("formats", {})
    merged = dict(formats.get(f"{category}/{ALL_SUBJECTS_FILE}", {}))
    if subject:
        merged.update(formats.get(f"{category}/{subject}", {}))
    return merged


def switch(name: str, default=None, *, category: str | None = None, subject: str | None = None):
    return switches(category, subject).get(name, default)


def solved_examples(category: str | None = None, subject: str | None = None) -> bool:
    """True where the notes keep the teacher's solved examples."""
    return bool(switch("solved_examples", False, category=category, subject=subject))


def prompt_block(category: str | None = None, subject: str | None = None) -> str:
    """The category/subject commands as a prompt block, or "" when there are none."""
    category = _resolve(category)
    text = commands(category, subject)
    if not text:
        return ""
    return (
        f"\n\nCATEGORY: {LABELS[category]} notes. Follow these format commands "
        "for selection and presentation only; they never add facts beyond the "
        f"lecture PDF:\n{text}"
    )


def fingerprint(category: str) -> str:
    """Digest of every rule for ``category``; "" when it has none.

    Goes into the automation's cache key, so editing any of the category's
    files regenerates its notes instead of reusing older ones.
    """
    category = _resolve(category)
    rules = _rules()
    texts = {key: value for key, value in rules["texts"].items() if key.startswith(f"{category}/")}
    formats = {key: value for key, value in rules["config"].get("formats", {}).items()
               if key.startswith(f"{category}/")}
    if not texts and not formats:
        return ""
    payload = json.dumps({"texts": texts, "formats": formats}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def category_subjects(category: str) -> list[str]:
    return list(_rules()["config"].get("category_subjects", {}).get(category, []))


def subject_label(subject: str) -> str:
    return _rules()["config"].get("subject_labels", {}).get(subject, subject.replace("_", " ").title())


def studio_choices() -> list[dict]:
    """Categories with their subject menus, for the Studio's dropdowns."""
    formats = _rules()["config"].get("formats", {})
    return [
        {
            "id": category,
            "label": LABELS[category],
            "default": category == DEFAULT,
            "subjects": [
                {"id": subject, "label": subject_label(subject),
                 "summary": formats.get(f"{category}/{subject}", {}).get("summary", "")}
                for subject in category_subjects(category)
            ],
        }
        for category in CATEGORIES
    ]
