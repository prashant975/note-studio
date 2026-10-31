# -*- mode: python ; coding: utf-8 -*-
"""Rebuild the launch fix using this machine's verified dependency inventory.

The normal ShortNotesStudio.spec remains the full build. This bounded rebuild
adds one module whose imports already exist in the previous dependency graph,
then recompiles all app sources and recreates the standard PyInstaller archive.
"""
import ast
import sys
from pathlib import Path

from PyInstaller.building.build_main import Analysis

REPO = Path(SPECPATH).resolve().parent
cache = REPO / "build/budget-fix-2026-10-08/ShortNotesStudio/Analysis-00.toc"
values = ast.literal_eval(cache.read_text(encoding="utf-8"))
inventory = dict(zip((item[0] for item in Analysis._GUTS), values, strict=True))
assert inventory["_python_version"] == sys.version, "Cached dependencies use a different Python version"
pure = list(inventory["pure"])
names = {name for name, _path, _kind in pure}
assert {"http.client", "json", "webbrowser"}.issubset(names), "New dependencies require the full build"
pure.append(("sheets_bridge.studio_instance", str(REPO / "sheets_bridge/studio_instance.py"), "PYMODULE"))

pyz = PYZ(pure)
exe = EXE(
    pyz,
    inventory["scripts"],
    inventory["binaries"],
    inventory["datas"],
    [],
    name="ShortNotesStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(REPO / "packaging/studio.ico"),
)
