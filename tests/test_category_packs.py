"""Category packs: seeded byte-for-byte, never overwritten, and with them the
engine's prompt_block() and fingerprint() are byte-identical to before (doc 07)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from note_studio import categories, paths

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engines" / "shortnotes"
RULES = ENGINE / "notesbot" / "format_rules"
SNAPSHOT = ROOT / "tests" / "snapshots" / "category_snapshot.py"
# Current-original baseline after the requested engine sync; the Phase 0
# categories_before.json remains untouched as historical evidence.
BEFORE = json.loads((ROOT / "tests" / "snapshots" / "categories_20261012.json").read_text(encoding="utf-8"))


def _engine_json(code_or_script: list[str], packs: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != categories.PACKS_ENV}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    if packs is not None:
        env[categories.PACKS_ENV] = str(packs)
    out = subprocess.run([sys.executable, *code_or_script], env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout)


def test_seed_writes_the_built_in_packs_byte_for_byte():
    written = categories.seed()
    assert set(written) == {"neet_jee", "foundation", "upsc", "bsc", "csir_net", "_template"}
    root = categories.packs_dir()
    for rule in (RULES / "foundation").glob("*.txt"):
        assert (root / "foundation" / "rules" / rule.name).read_bytes() == rule.read_bytes()
    config = json.loads((RULES / "rules.json").read_text(encoding="utf-8"))
    for cid in ("neet_jee", "foundation"):
        switches = json.loads((root / cid / "switches.json").read_text(encoding="utf-8"))["formats"]
        assert switches == {k: v for k, v in config["formats"].items() if k.startswith(cid + "/")}
    meta = categories.get("foundation")
    assert meta["label"] == "Foundation" and categories.get("neet_jee")["label"] == "NEET-JEE"
    assert [s["id"] for s in meta["subjects"]] == config["category_subjects"]["foundation"]
    assert categories.get("upsc")["enabled"] is True           # switched on in Phase 5
    assert "concise_handdrawn" not in categories.get("upsc")["note_types"]   # so the shortnotes engine never sees it
    assert categories.get("_template") is None            # never offered as a category


def test_seed_never_overwrites_an_edited_pack():
    categories.seed()
    rule = categories.packs_dir() / "neet_jee" / "rules" / "all-subjects.txt"
    rule.write_text("Keep every reaction condition.", encoding="utf-8")
    assert categories.seed() == []
    assert rule.read_text(encoding="utf-8") == "Keep every reaction condition."


def test_the_engine_reads_identical_rules_from_the_packs():
    """The core promise: for every NEET-JEE / Foundation subject, the prompt
    block, the fingerprint and the whole generation prompt are unchanged."""
    categories.seed()
    with_packs = _engine_json([str(SNAPSHOT), str(ENGINE)], categories.packs_dir())
    without = _engine_json([str(SNAPSHOT), str(ENGINE)], None)
    assert with_packs == BEFORE
    assert without == BEFORE
    original = Path.home() / "Downloads" / "shortnotes-core"
    if original.is_dir():
        assert with_packs == _engine_json([str(SNAPSHOT), str(original)], None)


_NEW_CATEGORY = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from notesbot import categories, generation_profile
from notesbot.prompts import build_prompt
with generation_profile.exam_category("test_clat"), generation_profile.exam_subject("legal"):
    prompt = build_prompt("english", "by Sir", subject_hint="legal")
print(json.dumps({"categories": list(categories.CATEGORIES), "label": categories.LABELS.get("test_clat"),
                  "block": categories.prompt_block("test_clat", "legal"), "in_prompt": "Quote every section" in prompt,
                  "fingerprint": categories.fingerprint("test_clat"), "normalize": categories.normalize("Test CLAT"),
                  "neet_block": categories.prompt_block("foundation", "mathematics"),
                  "foundation_fp": categories.fingerprint("foundation")}))
"""


def test_a_new_category_from_a_pack_reaches_the_prompt_and_leaves_the_others_alone():
    categories.seed()
    folder = categories.packs_dir() / "test_clat"
    (folder / "rules").mkdir(parents=True)
    (folder / "category.json").write_text(json.dumps({
        "id": "test_clat", "label": "Test CLAT", "enabled": True, "order": 9,
        "subjects": [{"id": "legal", "label": "Legal Reasoning"}], "languages": ["english"],
        "note_types": ["concise_handdrawn"]}), encoding="utf-8")
    (folder / "rules" / "all-subjects.txt").write_text("# a comment for the operator\nUse plain English.\n",
                                                       encoding="utf-8")
    (folder / "rules" / "legal.txt").write_text("Quote every section number.", encoding="utf-8")
    out = _engine_json(["-c", _NEW_CATEGORY, str(ENGINE)], categories.packs_dir())
    assert out["categories"] == ["neet_jee", "foundation", "test_clat"]
    assert out["label"] == "Test CLAT" and out["normalize"] == "test_clat"
    assert "CATEGORY: Test CLAT notes" in out["block"] and "Use plain English." in out["block"]
    assert "Quote every section number." in out["block"] and "a comment" not in out["block"]
    assert out["in_prompt"] is True and len(out["fingerprint"]) == 16
    assert out["neet_block"] == BEFORE["pairs"]["foundation/mathematics"]["prompt_block"]
    assert out["foundation_fp"] == BEFORE["fingerprints"]["foundation"]


def test_a_switched_off_pack_is_not_a_category():
    categories.seed()
    out = _engine_json(["-c", "import json,sys; sys.path.insert(0, sys.argv[1]); from notesbot import categories as c; "
                        "c._rules(); print(json.dumps(list(c.CATEGORIES)))", str(ENGINE)], categories.packs_dir())
    assert out == ["neet_jee", "foundation"]               # upsc is off until Phase 5


def test_packs_live_in_notecrafts_own_folder():
    assert categories.packs_dir().parent == paths.home()
