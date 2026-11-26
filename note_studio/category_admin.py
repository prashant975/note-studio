"""Settings -> Categories: edit rule files (a backup first), preview what a run
would follow, check a category, add one from a form, switch one on or off.

A new exam needs no code when an existing note type fits it (doc 07): its pack
is a folder of plain files, and the hand-drawn notes engine reads it the next
time it makes notes.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import categories, note_types

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
RESERVED = {"new", "all", "auto", "other", "sheet", "template"}


class CategoryError(ValueError):
    """Plain words for the Settings page."""


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return value if value[:1].isalpha() else f"c_{value}" if value else ""


def _rules_dir(pack: dict) -> Path:
    return Path(pack["folder"]) / str(pack.get("rules_dir") or "rules")


def rule_targets(pack: dict) -> list[dict]:
    """Every rule file a pack may have: for every subject, for one subject, for one note type."""
    targets = [{"name": "all-subjects", "label": "Every subject"}]
    targets += [{"name": s["id"], "label": f"Subject: {s.get('label') or s['id']}"} for s in pack.get("subjects") or []
                if isinstance(s, dict) and s.get("id")]
    targets += [{"name": f"types/{t['id']}", "label": f"Note type: {t['label']}"} for t in categories.offered_types(pack)]
    return targets


def rule_files(pack: dict) -> list[dict]:
    folder = _rules_dir(pack)
    files = []
    for target in rule_targets(pack):
        path = folder / f"{target['name']}.txt"
        if path.is_file():
            files.append({**target, "text": path.read_text(encoding="utf-8", errors="replace"),
                          "backup": path.with_name(path.name + ".bak").is_file()})
    return files


def save_rule(category_id: str, name: str, text: str) -> None:
    pack = categories.get(category_id)
    if not pack:
        raise CategoryError("That category no longer exists.")
    if name not in {t["name"] for t in rule_targets(pack)}:
        raise CategoryError("That rule file does not belong to this category.")
    path = _rules_dir(pack) / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    body = str(text or "").replace("\r\n", "\n").strip()
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def set_enabled(category_id: str, on: bool) -> None:
    pack = categories.get(category_id)
    if not pack:
        raise CategoryError("That category no longer exists.")
    meta_path = Path(pack["folder"]) / "category.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    shutil.copy2(meta_path, meta_path.with_name("category.json.bak"))
    meta["enabled"] = bool(on)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_pw_logo(category_id: str, on: bool) -> None:
    """The PW logo and watermark on this category's typeset notes (a backup first)."""
    pack = categories.get(category_id)
    if not pack:
        raise CategoryError("That category no longer exists.")
    meta_path = Path(pack["folder"]) / "category.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    shutil.copy2(meta_path, meta_path.with_name("category.json.bak"))
    branding = meta.get("branding") if isinstance(meta.get("branding"), dict) else {}
    branding["pw_logo"] = bool(on)
    meta["branding"] = branding
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_from_pdf(category_id: str, on: bool) -> None:
    """B.Sc.-style notes take their header name and logo from each lecture PDF, or the category's own
    words (a backup first). Only a category that makes B.Sc.-style notes has that header."""
    pack = categories.get(category_id)
    if not pack:
        raise CategoryError("That category no longer exists.")
    meta_path = Path(pack["folder"]) / "category.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if on and "typeset_notes" not in (meta.get("note_types") or []):
        raise CategoryError("Only a category that makes B.Sc.-style notes can take its header from the lecture PDF.")
    shutil.copy2(meta_path, meta_path.with_name("category.json.bak"))
    branding = meta.get("branding") if isinstance(meta.get("branding"), dict) else {}
    branding["from_pdf"] = bool(on)
    meta["branding"] = branding
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


GDOC_TYPE = categories.GDOC_TYPE


def set_gdoc(category_id: str, on: bool) -> None:
    """Offer (or stop offering) Google Docs with tabs (B.Sc. style) in this category (a backup first).
    Only a category that makes B.Sc.-style notes may offer it: it uses the same header and footer words.
    Off is remembered ("offer_gdoc": false), because a category with the PW logo offers it by itself."""
    pack = categories.get(category_id)
    if not pack:
        raise CategoryError("That category no longer exists.")
    meta_path = Path(pack["folder"]) / "category.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    offered = [t for t in meta.get("note_types") or [] if t != GDOC_TYPE]
    if on:
        if "typeset_notes" not in offered:
            raise CategoryError("Only a category that makes B.Sc.-style notes can offer Google Docs.")
        offered.insert(offered.index("typeset_notes") + 1, GDOC_TYPE)
        meta.pop("offer_gdoc", None)
    elif not offered:
        raise CategoryError("A category needs at least one note type.")
    else:
        meta["offer_gdoc"] = False
    shutil.copy2(meta_path, meta_path.with_name("category.json.bak"))
    meta["note_types"] = offered
    if meta.get("default_note_type") not in offered:
        meta["default_note_type"] = offered[0]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(category_id: str) -> list[dict]:
    """What is right and wrong with one category, in plain words."""
    found: list[dict] = []

    def say(kind: str, text: str) -> None:
        found.append({"kind": kind, "text": text})

    pack = categories.get(category_id)
    if not pack:
        say("bad", "The category file cannot be read (category.json).")
        return found
    say("ok" if pack.get("label") else "bad", f"Name: {pack.get('label') or 'missing'}")
    subjects = [s for s in pack.get("subjects") or [] if isinstance(s, dict)]
    ids = [s.get("id", "") for s in subjects]
    bad_ids = [i for i in ids if not re.match(r"^[a-z0-9_]+$", str(i))]
    if not subjects:
        say("warn", "No subjects: only Auto Detect is offered.")
    elif bad_ids or len(set(ids)) != len(ids):
        say("bad", "Subject ids must be unique and use only small letters, digits and _.")
    else:
        say("ok", f"{len(subjects)} subject{'s' if len(subjects) != 1 else ''}")
    languages = pack.get("languages") or []
    unknown = [code for code in languages if code not in note_types.LANGUAGE_LABELS]
    say("bad" if not languages or unknown else "ok",
        "Languages: " + (", ".join(note_types.language_label(c) for c in languages) or "none") +
        (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    offered = categories.offered_types(pack)
    missing = [t for t in pack.get("note_types") or [] if not note_types.get(t)]
    if missing:
        say("bad", "Unknown note types: " + ", ".join(missing))
    for entry in offered:
        if not categories.language_choices(pack, entry):
            say("bad", f"{entry['label']}: none of this category's languages can be made.")
    ready = [t for t in offered if t.get("status") == "ready"]
    say("ok" if ready else "warn", "Ready to make: " + (", ".join(t["label"] for t in ready) or "nothing yet"))
    if pack.get("default_note_type") and pack["default_note_type"] not in (pack.get("note_types") or []):
        say("bad", "The default note type is not one this category offers.")
    folder = _rules_dir(pack)
    known = {f"{t['name']}.txt" for t in rule_targets(pack)}
    for path in sorted(folder.rglob("*.txt")) if folder.is_dir() else []:
        relative = path.relative_to(folder).as_posix()
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            say("bad", f"Rule file {relative} is not plain text (UTF-8).")
            continue
        if relative not in known:
            say("warn", f"Rule file {relative} is not used (no subject or note type has that name).")
    rules = rule_files(pack)
    say("ok", f"{len(rules)} rule file{'s' if len(rules) != 1 else ''}" if rules else "No rule files: the standard notes")
    if pack.get("enabled") is True and "concise_handdrawn" in (pack.get("note_types") or []):
        try:
            from notesbot import categories as engine_categories

            engine_categories.normalize(category_id)
            say("ok", "The hand-drawn notes engine sees this category.")
        except Exception:
            say("bad", "The hand-drawn notes engine does not see this category yet.")
    elif pack.get("enabled") is not True:
        say("warn", "Switched off: it is not offered on New notes.")
    return found


def add(form: dict) -> str:
    """A new category from the Settings form. Returns its id."""
    label = " ".join(str(form.get("label") or "").split())
    if not label or len(label) > 40:
        raise CategoryError("Give the category a name (up to 40 letters).")
    category_id = slug(label)
    if not ID_RE.match(category_id) or category_id in RESERVED:
        raise CategoryError("Choose a name that starts with a letter.")
    root = categories.packs_dir()
    base, n = category_id, 2
    while (root / category_id).exists():
        category_id, n = f"{base}_{n}", n + 1
    subjects, seen = [], set()
    for line in str(form.get("subjects") or "").splitlines():
        name = " ".join(line.split())
        sid = slug(name)
        if name and sid and sid not in seen:
            seen.add(sid)
            subjects.append({"id": sid, "label": name})
    languages = [code for code in form.get("languages") or [] if code in note_types.LANGUAGE_LABELS]
    chosen = [t for t in form.get("note_types") or [] if note_types.get(t)]
    if not languages:
        raise CategoryError("Choose at least one language.")
    if not chosen:
        raise CategoryError("Choose at least one note type.")
    template = root / "_template"
    if not template.is_dir():
        categories.seed()
    shutil.copytree(template, root / category_id, copy_function=shutil.copy2)
    meta_path = root / category_id / "category.json"
    meta = {k: v for k, v in json.loads(meta_path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    order = 100 + len(categories.all_packs())
    meta.update({"id": category_id, "label": label, "enabled": True, "builtin": False, "order": order,
                 "summary": " ".join(str(form.get("summary") or "").split())[:160], "subjects": subjects,
                 "subject_auto": True, "languages": languages, "language_required": False, "note_types": chosen,
                 "default_note_type": chosen[0], "inputs": ["pdf_upload"]})
    meta.setdefault("branding", {})["signature_label"] = "Teacher name"
    if form.get("from_pdf") and "typeset_notes" in chosen:      # B.Sc.-style: header name and logo from each PDF
        meta["branding"]["from_pdf"] = True
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    first_rules = str(form.get("rules") or "").strip()
    if first_rules:
        rule = root / category_id / "rules" / "all-subjects.txt"
        rule.write_text(rule.read_text(encoding="utf-8").rstrip() + "\n\n" + first_rules.replace("\r\n", "\n") + "\n",
                        encoding="utf-8")
    return category_id
