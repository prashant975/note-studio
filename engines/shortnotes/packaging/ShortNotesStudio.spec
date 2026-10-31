# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the single-file ShortNotesStudio.exe.

Build with:  python -m PyInstaller packaging/ShortNotesStudio.spec --noconfirm
Output:      dist/ShortNotesStudio.exe
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = Path(SPECPATH).resolve().parent

datas = [
    (str(REPO / "fonts"), "fonts"),
    (str(REPO / "assets"), "assets"),
    (str(REPO / "public"), "public"),
    # Per category/subject note formats, read by notesbot.categories.
    (str(REPO / "notesbot" / "format_rules"), "notesbot/format_rules"),
    # Seeds copied out to ~/.shortnotes_bridge on first launch, or read in place.
    (str(REPO / "client_secret.json"), "."),
    (str(REPO / "sheets.json"), "."),
]
binaries = []
hiddenimports = [
    "api",
    "api.web",
    "pw_access",
    "vercel.oidc",
]

# The app's own packages: the CLI imports most of them lazily inside functions,
# which static analysis alone would not follow.
for package in ("notesbot", "sheets_bridge", "equation_kit", "api"):
    hiddenimports += collect_submodules(package)

# Third-party packages that ship data files or resolve plugins at runtime.
for package in (
    "googleapiclient",      # discovery documents
    "google_auth_oauthlib",
    "docx",                 # default .docx template
    "uharfbuzz",
    "pypdfium2",
    "pypdfium2_raw",
    "fpdf",                 # bundled core font metrics
):
    extra_datas, extra_binaries, extra_hidden = collect_all(package)
    datas += extra_datas
    binaries += extra_binaries
    hiddenimports += extra_hidden

excludes = [
    "tkinter", "_tkinter",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "ipykernel",
    "torch", "torchvision", "torchaudio", "whisper", "openai_whisper",
    "tensorflow", "numba", "llvmlite",
    "pandas", "scipy", "sklearn", "sympy",
    "pytest", "_pytest",
]

a = Analysis(
    [str(REPO / "packaging" / "studio_launcher.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
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
    icon=str(REPO / "packaging" / "studio.ico"),
)
