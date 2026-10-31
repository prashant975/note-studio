# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [
    ('app.py', '.'),
    ('pw_access.py', '.'),
    ('src', 'src'),
    ('assets', 'assets'),
    ('prompts', 'prompts'),
    # QC framework config (read-only; loaded via src/qc/paths.resource_path).
    # Writable QC state (caches, reference indexes) lives in %LOCALAPPDATA%,
    # never inside the bundle.
    ('config', 'config'),
    ('reference_documents', 'reference_documents'),
    ('.streamlit/secrets.toml', '.streamlit'),
]

if Path('.env').exists():
    datas.append(('.env', '.'))

# ffmpeg + ffprobe, bundled so the app runs with no manual install at all.
# They are the one system dependency the team had to fetch by hand, and a
# missing ffmpeg was the most likely first-run failure. The launcher adds this
# folder to PATH only when the machine has no ffmpeg of its own, so an existing
# install always wins. LICENSE/README ride along (GPL build from gyan.dev —
# the licence text must travel with the binaries).
if Path('third_party/ffmpeg/ffmpeg.exe').exists():
    datas.append(('third_party/ffmpeg', 'ffmpeg'))

binaries = []
hiddenimports = [
    'pw_access',
    'streamlit.web.cli',
    'streamlit.runtime.scriptrunner.magic_funcs',
    'yaml',  # QC config loader (src/qc/qc_config.py)
]

tmp_ret = collect_all('streamlit')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('altair')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('docx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fitz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pptx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('dotenv')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Error Report XLSX (src/error_report_writer.py). openpyxl resolves its cell
# writers and style descriptors dynamically, so a static import scan is not
# enough to guarantee the packaged build can save a workbook.
tmp_ret = collect_all('openpyxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Login (Streamlit OAuth) uses Authlib; it is imported lazily so PyInstaller
# misses it without collect_all. Same for httpx (Authlib's async HTTP client).
tmp_ret = collect_all('authlib')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('httpx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Class-video notes: youtube-transcript-api (fast path) and yt-dlp (audio
# fallback) are both imported lazily inside functions. yt-dlp additionally loads
# its extractors dynamically, so it must be collected whole.
tmp_ret = collect_all('youtube_transcript_api')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# DPP practice-set builder (src/notes_to_dpp). src/ travels in the bundle as
# DATA, so PyInstaller never reads one import statement in it: every
# third-party package src uses has to be named here by hand. v2.11.0 shipped
# without rapidfuzz and reportlab: the DPP tab died on its first click with
# "No module named 'rapidfuzz'", and reportlab would have been the next
# failure. tests/test_frozen_build_manifest.py now fails when an import in
# src/ has no entry here, and `UpscNotesApp.exe --selftest` proves the built
# EXE can run it.
#   rapidfuzz — ranks taxonomy candidates (matching.py). C extensions chosen
#               at import time by a dynamic fallback, so the whole package.
#   reportlab — the question / solution / combined PDFs (pdf_export.py), plus
#               its bundled font metrics, without which no text renders.
#   uharfbuzz — reportlab[shaping]. It was in v2.11.0 only because fontTools
#               happens to import it; this makes it a guarantee. Without it
#               reportlab silently turns shaping OFF and Hindi PDFs render
#               Devanagari conjuncts as separate glyphs — nothing raises, so
#               only the self-test's shaping probe would notice.
tmp_ret = collect_all('rapidfuzz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uharfbuzz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

hiddenimports += [
    'fitz',
    'docx',
    'pptx',
    'PIL',
    'PIL.Image',
    'dotenv',
    'pythoncom',
    'win32com',
    'win32com.client',
    'authlib',
    'authlib.integrations.starlette_client',
    'authlib.jose',
    'httpx',
    'youtube_transcript_api',
    'yt_dlp',
    'openpyxl',
    'rapidfuzz',
    'reportlab',
    'uharfbuzz',
    # Imported by src/ under names the collect_all() calls above do not cover,
    # and present in earlier builds only because some other package happened
    # to depend on them. Naming them makes that a guarantee rather than luck.
    'pymupdf',    # src/notes_to_dpp/notes.py imports `pymupdf`, not `fitz`
    'lxml',       # src/upsc_dcn_docx_writer.py (came in via python-docx)
    'requests',   # pw_access.py and half of src/ (came in via streamlit)
    'numpy',      # src/notes_to_dpp/notes.py OCR path (came in via pandas)
    # Usage tracking to a Google Sheet through a service account
    # (src/usage_tracker.py, src/video_sources/google_drive.py). google-auth is
    # a namespace package, so its subpackages must be named one by one.
    'google.auth',
    'google.auth.transport.requests',
    'google.oauth2',
    'google.oauth2.service_account',
    # src/ is bundled as data and imported at runtime, so PyInstaller never
    # scans it — every stdlib module src uses must enter the graph some other
    # way. These are used by the video-ingestion and DPP code (Drive transcript
    # XML, entity unescaping, parallel AI calls, HTML-ish option text, URL
    # handling) and only happen to be present today via other packages'
    # imports; naming them makes that a guarantee.
    'xml.etree.ElementTree',
    'html',
    'html.parser',
    'urllib.parse',
    'urllib.request',
    'concurrent.futures',
]

# Development-only packages that live in the build venv but must NOT ship.
# faster-whisper is the local STT option and is deliberately opt-in: bundling
# it drags in CTranslate2 + ONNX Runtime (~400 MB) that the Gemini default
# never touches. The app degrades cleanly when it is absent (the STT router
# falls back to Gemini). playwright/pytest are test tooling.
excludes = [
    # src/ ships as DATA and is imported from _internal/src at runtime, the
    # way app.py imports it. The launcher's `from src.selftest import run`
    # would otherwise drag src and everything it imports into the PYZ, where
    # a compiled copy silently shadows the data files. One copy of src only.
    # Expected side effect: warn-UpscNotesApp.txt lists src.selftest as a
    # missing module — that is this exclusion working, not a problem.
    'src',
    'faster_whisper',
    'ctranslate2',
    'onnxruntime',
    'av',
    'tokenizers',
    'huggingface_hub',
    'playwright',
    'pytest',
]


a = Analysis(
    ['streamlit_launcher_stable.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='UpscNotesApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    upx=True,
    upx_exclude=[],
    name='UpscNotesApp',
)
