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


def setup_name(edition: str | None = None, mac: bool = False) -> str:
    """The release file's name before _<version>: NoteCraft_Setup[_NoUPSC...] (.exe, Windows)
    or NoteCraft_Mac[_NoUPSC...] (.zip of NoteCraft.app). Each edition has its own."""
    edition = edition or EDITION
    suffixes = {"full": "", "noupsc": "_NoUPSC", "nopwbooks": "_NoPWBooks",
                "noupsc-nopwbooks": "_NoUPSC_NoPWBooks"}
    if edition not in suffixes:
        raise ValueError(f"Unknown NoteCraft edition: {edition}")
    return f"{brand.APP_NAME}_{'Mac' if mac else 'Setup'}" + suffixes[edition]


def mac_app_root(executable: str | None = None) -> Path | None:
    """The NoteCraft.app this Python runs inside (Contents/Resources/runtime, packaging/build_macos.py),
    or None. The Mac app updates itself from a .zip of the app (updater.py)."""
    here = Path(executable or sys.executable).resolve()
    for folder in here.parents:
        if folder.suffix == ".app" and here.is_relative_to(folder / "Contents" / "Resources" / "runtime"):
            return folder
    return None


def mac_app(executable: str | None = None) -> bool:
    return mac_app_root(executable) is not None


# An installed copy runs on its own Python beside the app (packaging/notecraft_setup.iss).
INSTALLED = (Path(sys.executable).resolve().parent.name.lower() == "python"
             and Path(sys.executable).resolve().parent.parent.name.lower() == "_internal")
MAC_APP = mac_app()
CHANNEL = "team install" if INSTALLED else "Mac app" if MAC_APP else "development copy"
