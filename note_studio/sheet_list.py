"""The sheet list (sheets.json), shared with the old Short Notes app.

The old app's own save (sheets_bridge.profiles.save_profiles) rewrites the file
with the fields it knows and keeps no backup. NoteCraft edits it more carefully:

- every save re-reads the file first, so a change the old app just made is kept;
- every key NoteCraft does not know is kept, on each sheet and at the top;
- the file is checked with the old app's own reader before it is written, so
  the old app can always open it;
- a backup is written first: sheets.json.bak beside it, plus a dated copy in
  ~/.note_studio/backups/sheets/ (the last 30).

What only NoteCraft uses (each sheet's category and note type) is kept in
NoteCraft's own database, keyed by sheet name, never in the shared file: the
old app's save would drop it.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import db, paths

EXTRAS_KEY = "sheet_extras"
KEEP_BACKUPS = 30
FIELD_ORDER = ("name", "spreadsheetId", "gid", "allTabs", "signature", "maxCostInr", "alsoPdf",
               "enabled", "outputFolder", "skipBatches")


class SheetListError(ValueError):
    """Plain words for the page."""


def path() -> Path:
    """The same file the copied automation reads (SHEETS_BRIDGE_PROFILES, set
    from the Settings choice). Never the engine copy's own folder."""
    from . import old_app

    folder = old_app.state_dir()
    if folder is None:
        raise SheetListError("Choose in Settings whether NoteCraft uses the old app's sheets first.")
    return Path(folder) / "sheets.json"


def read_raw() -> dict:
    """The file as it is ({"profiles": []} when there is none yet)."""
    target = path()
    if not target.is_file():
        return {"profiles": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SheetListError("The sheet list file could not be read. Nothing was changed.") from None
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise SheetListError("The sheet list file is not in the expected shape. Nothing was changed.")
    return data


def sheets() -> list[dict]:
    """Every sheet: the old app's parsed view, the raw entry, and NoteCraft's extras."""
    from sheets_bridge import profiles

    raw = read_raw()
    extras = all_extras()
    out = []
    for index, entry in enumerate(raw["profiles"]):
        try:
            target = profiles._target_from_json(entry, where=f"sheet #{index + 1}")
            problem = ""
        except Exception as exc:          # one broken entry must not hide the others
            target, problem = None, getattr(exc, "detail", "") or "this entry cannot be read"
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        out.append({"index": index, "name": name, "target": target, "raw": entry, "problem": problem,
                    "extras": extras.get(_key(name), {})})
    return out


def _key(name: str) -> str:
    return str(name or "").strip().casefold()


# ------------------------------------------------------------------ extras
def all_extras() -> dict:
    try:
        value = json.loads(db.get_setting(EXTRAS_KEY, "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def extras_for(name: str) -> dict:
    return all_extras().get(_key(name), {})


def set_extras(name: str, values: dict) -> None:
    extras = all_extras()
    extras[_key(name)] = {**extras.get(_key(name), {}), **values}
    db.set_setting(EXTRAS_KEY, json.dumps(extras, ensure_ascii=False, sort_keys=True))


def _move_extras(old: str, new: str) -> None:
    extras = all_extras()
    if _key(old) in extras and _key(old) != _key(new):
        extras[_key(new)] = extras.pop(_key(old))
        db.set_setting(EXTRAS_KEY, json.dumps(extras, ensure_ascii=False, sort_keys=True))


# -------------------------------------------------------------------- save
def _ordered(entry: dict) -> dict:
    """Known fields in the old app's order, then every other key as it was."""
    known = {k: entry[k] for k in FIELD_ORDER if k in entry}
    return {**known, **{k: v for k, v in entry.items() if k not in known}}


def _validate(data: dict) -> None:
    """The old app's own reader must accept every entry, and names stay unique."""
    from sheets_bridge import profiles

    seen = set()
    for index, entry in enumerate(data["profiles"]):
        try:
            target = profiles._target_from_json(entry, where=f"sheet #{index + 1}")
        except Exception as exc:
            raise SheetListError(f"Not saved: {getattr(exc, 'detail', '') or 'a sheet entry is not valid'}.") from None
        if target.name.casefold() in seen:
            raise SheetListError(f"Not saved: there is already a sheet called {target.name!r}.")
        seen.add(target.name.casefold())


def _backup(target: Path) -> None:
    if not target.is_file():
        return
    shutil.copy2(target, target.with_name(target.name + ".bak"))
    folder = paths.home() / "backups" / "sheets"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, folder / f"sheets-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}.json")
    for old in sorted(folder.glob("sheets-*.json"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def save(data: dict) -> None:
    _validate(data)
    target = path()
    data = {**data, "profiles": [_ordered(e) for e in data["profiles"]]}
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    _backup(target)
    tmp = target.with_name(target.name + ".notecraft.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)


def _find(data: dict, name: str) -> int:
    for index, entry in enumerate(data["profiles"]):
        if isinstance(entry, dict) and _key(entry.get("name")) == _key(name):
            return index
    raise SheetListError("That sheet is no longer in the list (it may have been removed in the other app).")


def add(entry: dict, extras: dict | None = None) -> None:
    data = read_raw()
    if any(isinstance(e, dict) and _key(e.get("name")) == _key(entry.get("name")) for e in data["profiles"]):
        raise SheetListError(f"There is already a sheet called {entry.get('name')!r}. Choose another name.")
    data["profiles"].append(entry)
    save(data)
    if extras:
        set_extras(entry["name"], extras)


def update(name: str, changes: dict, *, remove_keys: tuple = ()) -> str:
    """Change some fields of one sheet; every other key stays. Returns the (new) name."""
    data = read_raw()
    index = _find(data, name)
    entry = dict(data["profiles"][index])
    entry.update(changes)
    for key in remove_keys:
        entry.pop(key, None)
    data["profiles"][index] = entry
    save(data)
    new_name = entry.get("name", name)
    _move_extras(name, new_name)
    return new_name


def remove(name: str) -> None:
    data = read_raw()
    index = _find(data, name)
    del data["profiles"][index]
    save(data)
