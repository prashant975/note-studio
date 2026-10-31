"""Path helpers that work identically from source and inside the PyInstaller EXE.

Two distinct roots, never to be confused:

- resource_path(): READ-ONLY bundled files (config YAMLs, prompts). Inside the
  EXE these live under sys._MEIPASS, which is wiped on exit — nothing may be
  written there.
- app_data_dir(): WRITABLE per-user state (source cache, downloaded official
  PDFs, local reference indexes). Same %LOCALAPPDATA%\\UPSC_Notes root the
  proxy session pass already uses (src/proxy_session.py), so all app state
  lives in one place. Never store credentials here in plain text.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_path(relative: str) -> Path:
    """Resolve a bundled read-only resource in source and frozen modes."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS"))
    else:
        base = Path(__file__).resolve().parents[2]
    return base / relative


def app_data_dir(*parts: str) -> Path:
    """Writable per-user directory, created on first use."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) / "UPSC_Notes" if base else Path.home() / ".upsc_notes"
    p = root.joinpath(*parts) if parts else root
    p.mkdir(parents=True, exist_ok=True)
    return p
