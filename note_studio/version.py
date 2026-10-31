"""The version shown in the app: YYYY.M.D.N (month and day without padding, N =
the build of that day). The installer build writes it into static/release.json
(packaging/build_installer.py); that file is the one source of truth."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import brand

RELEASE_FILE = Path(__file__).resolve().parent / "static" / "release.json"


def _release() -> dict:
    try:
        value = json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


VERSION = str(_release().get("version") or "2026.10.4.1")
EDITION = str(_release().get("edition") or "full")


def setup_name(edition: str | None = None) -> str:
    edition = edition or EDITION
    suffixes = {"full": "", "noupsc": "_NoUPSC", "nopwbooks": "_NoPWBooks",
                "noupsc-nopwbooks": "_NoUPSC_NoPWBooks"}
    if edition not in suffixes:
        raise ValueError(f"Unknown NoteCraft edition: {edition}")
    return f"{brand.APP_NAME}_Setup" + suffixes[edition]


# An installed copy runs on its own Python beside the app (packaging/notecraft_setup.iss).
INSTALLED = (Path(sys.executable).resolve().parent.name.lower() == "python"
             and Path(sys.executable).resolve().parent.parent.name.lower() == "_internal")
CHANNEL = "team install" if INSTALLED else "development copy"
