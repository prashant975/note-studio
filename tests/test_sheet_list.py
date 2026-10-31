"""The shared sheet list: every unknown key kept, a backup before each save, a
fresh read before each change, and always readable by the old app."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from note_studio import db, old_app, paths, sheet_list

ORIGINAL = Path(os.environ.get("NOTE_STUDIO_PARITY_ORIGINAL", Path.home() / "Downloads" / "shortnotes-core"))
START = {
    "version": 2,
    "profiles": [
        {"name": "Lakshya JEE 2027", "spreadsheetId": "18ZcAOO_Y0j-abcdefghij", "gid": 0, "futureKey": {"x": 1}},
        {"name": "Catalog", "spreadsheetId": "1dPYUsfG9orAjUWvwcX8", "gid": 0, "outputFolder": "1T6H3Iir9amxkTkY",
         "skipBatches": []},
    ],
}


@pytest.fixture
def shared(tmp_path):
    folder = tmp_path / "old_app"
    folder.mkdir()
    (folder / "sheets.json").write_text(json.dumps(START, indent=2), encoding="utf-8")
    db.init()
    from note_studio import engine_catalog

    engine_catalog.put_shortnotes_on_path()
    assert old_app.adopt_default() == "yes"           # the user's choice where the folder exists
    for key, value in old_app.environment().items():
        os.environ[key] = value
    return folder / "sheets.json"


def _load_with_old_app(file: Path) -> list[str]:
    from sheets_bridge import profiles

    return [t.name for t in profiles.load_profiles(file)]


def test_it_reads_the_shared_file(shared):
    rows = sheet_list.sheets()
    assert [r["name"] for r in rows] == ["Lakshya JEE 2027", "Catalog"]
    assert rows[0]["target"].gid == 0 and rows[1]["target"].output_folder == "1T6H3Iir9amxkTkY"
    assert sheet_list.path() == shared


def test_an_edit_keeps_unknown_keys_and_backs_up_first(shared):
    before = shared.read_bytes()
    sheet_list.update("Lakshya JEE 2027", {"signature": "Saleem Ahmad Sir", "maxCostInr": 500})
    data = json.loads(shared.read_text(encoding="utf-8"))
    assert data["version"] == 2
    entry = data["profiles"][0]
    assert entry["futureKey"] == {"x": 1} and entry["signature"] == "Saleem Ahmad Sir" and entry["maxCostInr"] == 500
    assert data["profiles"][1]["skipBatches"] == []                   # an explicit empty list stays
    assert (shared.parent / "sheets.json.bak").read_bytes() == before
    assert list((paths.home() / "backups" / "sheets").glob("sheets-*.json"))
    assert _load_with_old_app(shared) == ["Lakshya JEE 2027", "Catalog"]


def test_a_change_made_by_the_old_app_meanwhile_is_kept(shared):
    sheet_list.sheets()                                  # NoteCraft read the list ...
    data = json.loads(shared.read_text(encoding="utf-8"))
    data["profiles"].append({"name": "Added In Old App", "spreadsheetId": "1abcdefghijKLMN", "gid": 5})
    shared.write_text(json.dumps(data), encoding="utf-8")  # ... the old app added a sheet ...
    sheet_list.update("Catalog", {"enabled": False})      # ... then NoteCraft saved
    names = _load_with_old_app(shared)
    assert names == ["Lakshya JEE 2027", "Catalog", "Added In Old App"]


def test_a_save_the_old_app_could_not_read_is_refused(shared):
    before = shared.read_bytes()
    with pytest.raises(sheet_list.SheetListError):
        sheet_list.update("Catalog", {"maxCostInr": -5})
    with pytest.raises(sheet_list.SheetListError):
        sheet_list.update("Catalog", {"name": "Lakshya JEE 2027"})
    with pytest.raises(sheet_list.SheetListError):
        sheet_list.add({"name": "catalog", "spreadsheetId": "1zzzzzzzzzzzz", "gid": 1})
    assert shared.read_bytes() == before


def test_add_rename_and_remove_carry_notecrafts_extras(shared):
    sheet_list.add({"name": "Foundation 8", "spreadsheetId": "17NLSXUu4smmS33ok", "gid": 7},
                   extras={"category": "foundation", "note_type": "concise_handdrawn"})
    assert sheet_list.extras_for("foundation 8") == {"category": "foundation", "note_type": "concise_handdrawn"}
    assert "category" not in json.loads(shared.read_text(encoding="utf-8"))["profiles"][-1]   # never in the shared file
    sheet_list.update("Foundation 8", {"name": "Pre Foundation 8"})
    assert sheet_list.extras_for("Pre Foundation 8")["category"] == "foundation"
    sheet_list.remove("Pre Foundation 8")
    assert _load_with_old_app(shared) == ["Lakshya JEE 2027", "Catalog"]


def test_without_a_choice_nothing_is_read_or_written(tmp_path):
    db.init()
    assert old_app.choice() == ""
    with pytest.raises(sheet_list.SheetListError):
        sheet_list.sheets()


@pytest.mark.skipif(not (ORIGINAL / "sheets_bridge" / "profiles.py").is_file(), reason="original not on this computer")
def test_the_original_old_app_reads_what_notecraft_saved(shared):
    sheet_list.update("Lakshya JEE 2027", {"signature": "by Sir", "alsoPdf": True})
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from sheets_bridge import profiles; "
            "print(json.dumps([t.to_json() for t in profiles.load_profiles(__import__('pathlib').Path(sys.argv[2]))]))")
    out = subprocess.run([sys.executable, "-c", code, str(ORIGINAL), str(shared)], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = json.loads(out.stdout)
    assert loaded[0]["signature"] == "by Sir" and loaded[0]["alsoPdf"] is True and len(loaded) == 2
