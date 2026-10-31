"""Parity: New notes makes the same notes as the old app's Upload page (doc 04).

For each case, tests/parity/upload_driver.py runs once on the ORIGINAL project
(its real notesbot.web_service.run_job with the desktop app's generate/render)
and once on NoteCraft (note_studio.engines.shortnotes on the engine copy, rules
from the category packs), each in its own process, with the same stubbed model
and no network. Compared: every model request byte for byte (SHA-256), the
notes, the page count and text, and the PDF itself (minus its time stamp).
The original is only read; nothing is written into it.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from note_studio import categories

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "parity" / "upload_driver.py"
# The real original (conftest points paths.shortnotes_original() at a temp folder).
ORIGINAL = Path(os.environ.get("NOTE_STUDIO_PARITY_ORIGINAL", Path.home() / "Downloads" / "shortnotes-core"))

pytestmark = pytest.mark.skipif(not (ORIGINAL / "sheets_bridge" / "studio_server.py").is_file(),
                                reason="the original shortnotes-core project is not on this computer")

# sheets_bridge/studio_server.py closures the driver transcribes (SHA-256 of their source).
UPLOAD_CLOSURES = {
    "_ensure_proxy": "f4a9dbb188b2f4cbe8b6cba1b8f1d21a8723688502dc0fe9c1cc38045fc65a68",
    "_local_generate": "72e411aee154666fa3213ff2ab26e5cc290fc05e45e642f2ca7d398efc097cc2",
    "_local_render": "cf003d221f61fb29b4a650ae51e5a26d52a7683a3e432aa2a4b6d893c2f70349",
}

CASES = [
    {"teacher_name": "Saleem Ahmad Sir", "category": "neet_jee", "subject": "auto", "language": "auto"},
    {"teacher_name": "by Priya Ma'am", "category": "foundation", "subject": "mathematics", "language": "english",
     "watermark": True},
    {"teacher_name": "Acharya Ji", "category": "foundation", "subject": "sanskrit", "language": "sanskrit_hindi",
     "logo_watermark": False},
    {"teacher_name": "Saleem Ahmad Sir", "category": "neet_jee", "subject": "physics", "language": "hindi",
     "instructions": "Keep every reaction condition."},
]


def test_the_old_upload_path_is_still_what_the_driver_transcribes():
    source = (ORIGINAL / "sheets_bridge" / "studio_server.py").read_text(encoding="utf-8")
    found = {node.name: hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest()
             for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.FunctionDef) and node.name in UPLOAD_CLOSURES}
    assert found == UPLOAD_CLOSURES, "the old app's Upload path changed: re-check upload_driver.py and the adapter"


@pytest.fixture(scope="module")
def fixture_pdf(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("lecture")
    subprocess.run([sys.executable, "-c",
                    "import sys; from pathlib import Path; from scripts.make_sequence_smoke_fixtures import "
                    "build_fixtures; build_fixtures(Path(sys.argv[1]))", str(out)],
                   cwd=ROOT / "engines" / "shortnotes", check=True, timeout=120,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return out / "text-order.pdf"


def _drive(mode: str, root: Path, pdf: Path, out: Path, settings: dict, packs: Path | None) -> dict:
    settings_file = out / f"{mode}-settings.json"
    out.mkdir(parents=True, exist_ok=True)
    full = {"instructions": "", "watermark": False, "logo_watermark": True, **settings}
    settings_file.write_text(json.dumps(full), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != categories.PACKS_ENV}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    if packs is not None:
        env[categories.PACKS_ENV] = str(packs)
    proc = subprocess.run([sys.executable, str(DRIVER), mode, str(root), str(pdf), str(out / mode), str(settings_file)],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("PARITY-REPORT ")), "")
    assert line, f"{mode} run failed:\n{proc.stderr[-4000:]}"
    return json.loads(line[len("PARITY-REPORT "):])


@pytest.mark.parametrize("case", CASES, ids=[f"{c['category']}-{c['subject']}-{c['language']}" for c in CASES])
def test_new_notes_makes_the_same_notes_as_the_old_upload_page(case, fixture_pdf, tmp_path):
    categories.seed()
    old = _drive("original", ORIGINAL, fixture_pdf, tmp_path, case, None)
    new = _drive("notecraft", ROOT, fixture_pdf, tmp_path, case, categories.packs_dir())
    assert old["state"] == new["state"] == "complete", (old.get("state"), new.get("failure"), new.get("detail"))
    assert new["calls"] == old["calls"]                # every request, byte for byte
    assert new["notes"] == old["notes"]
    assert new["filename"] == old["filename"] == "text-order Concise Notes.pdf"
    assert new["pages"] == old["pages"] and new["page_text"] == old["page_text"]
    assert new["pdf_sha256_normalized"] == old["pdf_sha256_normalized"]
    assert new["cost_inr"] == pytest.approx(1.25 * len(new["calls"]))
