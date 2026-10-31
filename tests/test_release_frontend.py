"""Behavior check for the ready-only install popup, using a JavaScript DOM double."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_update_popup_waits_for_verified_download_and_requires_a_click():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed on this test machine")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests" / "release_frontend_driver.js"),
                             str(root / "note_studio" / "static" / "release.js")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
