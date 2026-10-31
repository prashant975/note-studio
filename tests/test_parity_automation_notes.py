"""The default manual quality mode uses the exact original Sheets pipeline."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from note_studio import categories

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = Path.home() / "Downloads/shortnotes-core"
DRIVER = ROOT / "tests/parity/automation_driver.py"


@pytest.mark.skipif(not ORIGINAL.is_dir(), reason="The original engine is unavailable")
@pytest.mark.parametrize("category,subject", [("neet_jee", "physics"), ("foundation", "mathematics")])
def test_manual_automation_mode_matches_original_requests_notes_pdf_and_quality(category, subject, lecture_pdf, tmp_path):
    categories.seed()
    source = tmp_path / "lecture.pdf"
    source.write_bytes(lecture_pdf)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"category": category, "subject": subject, "language": "english",
                                   "teacher_name": "Teacher"}), encoding="utf-8")
    reports = []
    for mode, root in (("original", ORIGINAL), ("notecraft", ROOT)):
        env = {key: value for key, value in os.environ.items() if key != categories.PACKS_ENV}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        if mode == "notecraft":
            env[categories.PACKS_ENV] = str(categories.packs_dir())
        proc = subprocess.run([sys.executable, str(DRIVER), mode, str(root), str(source),
                               str(tmp_path / mode), str(settings)], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=240)
        line = next((line for line in proc.stdout.splitlines() if line.startswith("AUTOMATION-PARITY ")), "")
        assert proc.returncode == 0 and line, proc.stderr[-3500:]
        reports.append(json.loads(line[len("AUTOMATION-PARITY "):]))
    old, new = reports
    for field in ("calls", "notes", "pages", "page_text", "pdf_sha256_normalized", "quality_report", "cost_inr"):
        assert old[field] == new[field], field
    assert new["quality_report"]["verdict"] == "passed"
    assert "quality" in {call["kind"] for call in new["calls"]}
