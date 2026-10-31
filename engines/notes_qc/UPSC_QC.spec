# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the shareable desktop build.

    python -m PyInstaller UPSC_QC.spec --noconfirm

Produces dist/UPSC_QC/ — a folder holding UPSC_QC.exe and everything it needs.
Zip that folder and a teammate unzips it once and double-clicks the exe; it
starts in a few seconds, every time. A one-file build would be a single ~700 MB
executable that re-extracts itself on every launch, which is a worse experience
for the same contents.

Streamlit is the awkward dependency. It loads its own static assets, metadata
and a pile of lazily-imported submodules at runtime, none of which PyInstaller
can see by following imports — hence collect_all, and hence the app's own
files being bundled as DATA rather than as code.
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# Streamlit and the packages it reaches for at runtime rather than at import.
for package in ("streamlit", "altair", "pyarrow", "pandas", "authlib",
                "openpyxl", "fitz", "pymupdf", "spellchecker", "dotenv",
                "requests", "PIL"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Streamlit's CLI entry point and the metadata it reads about itself.
hiddenimports += [
    "streamlit.web.cli",
    "streamlit.runtime.scriptrunner.magic_funcs",
    "streamlit.web.server.starlette.starlette_app",
    "streamlit.web.server.starlette.starlette_auth_routes",
]
try:
    datas += collect_data_files("streamlit", include_py_files=True)
except Exception:
    pass

# The app itself. app.py is DATA, not code: streamlit run needs a real file on
# disk to execute, and the launcher points it at the extracted copy.
datas += [
    ("app.py", "."),
    ("pw_access.py", "."),
    ("src", "src"),
    ("config", "config"),
    ("reference_documents", "reference_documents"),
    # Google sign-in credentials, bundled deliberately so the exe works on a
    # teammate's machine with nothing to configure. The redirect URI is
    # localhost, which is valid on every machine, and the PW proxy still
    # admits only whitelisted @pw.live accounts — the secret alone buys
    # nothing. Treat the build as internal: do not publish it.
    (".streamlit/secrets.toml", ".streamlit"),
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Test and build machinery a user never runs, and the heavy plotting stack
    # Streamlit only needs if a chart is drawn. Dropping them saves ~150 MB.
    excludes=["pytest", "PyInstaller", "matplotlib", "tkinter", "IPython",
              "notebook", "jupyter", "sphinx", "black", "mypy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UPSC_QC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # the window carries the progress log and any error
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="UPSC_QC",
)
