"""Settings -> System -> Check setup: what the engines need on this computer, in plain words.

The checks mirror how the UPSC engine itself looks for things (src/pdf_exporter.py
find_soffice, src/docx_layout.py is_font_installed, src/media/audio_extractor.py
shutil.which), so "found" here means the engine will find it too. Nothing is
installed or changed.
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

from . import engine_catalog, engine_host

UPSC_MODULES = {"yt_dlp": "YouTube links", "youtube_transcript_api": "YouTube captions", "docx": "Word documents",
                "fitz": "PDF pages", "pptx": "PowerPoint slides", "openpyxl": "the error report",
                "requests": "PW AI", "PIL": "pictures"}
FONTS = {"Coming Soon": "[Cc]oming*[Ss]oon*.[to]tf", "Kalam": "[Kk]alam*.[to]tf"}


def _font_dirs() -> list[str]:
    return [r"C:\Windows\Fonts", os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
            str(Path.home() / "Library/Fonts"), "/Library/Fonts", "/System/Library/Fonts",
            str(Path.home() / ".local/share/fonts"), "/usr/share/fonts"]


def font_installed(pattern: str) -> bool:
    return any(os.path.isdir(d) and glob.glob(os.path.join(d, "**", pattern), recursive=True) for d in _font_dirs())


def soffice() -> str:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                      "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if Path(candidate).exists():
            return candidate
    return ""


def word_installed() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"):
            return True
    except OSError:
        return False


def office() -> str:
    """'LibreOffice' / 'Word' / '' - what will turn the Word notes into a PDF."""
    return "LibreOffice" if soffice() else "Word" if word_installed() else ""


def ffmpeg() -> tuple[str, str]:
    """(where ffmpeg is, where ffprobe is): PATH first, then the engine copy's own."""
    bundled = engine_catalog.engine_dir("upsc") / "third_party" / "ffmpeg"
    found = []
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name) or (str(bundled / f"{name}.exe")
                                     if os.name == "nt" and (bundled / f"{name}.exe").is_file() else "")
        found.append(path)
    return found[0], found[1]


def _missing_modules() -> list[str] | None:
    """Modules the UPSC engine's Python lacks; None when that Python cannot be run."""
    return engine_host.probe("upsc", UPSC_MODULES)


def run() -> list[dict]:
    """[{'name', 'state': ok|warn|bad, 'words', 'fix'}] for the UPSC engine."""
    if not engine_catalog.available("upsc"):
        return [{"name": "UPSC notes and practice", "state": "ok",
                 "words": "Mains practice and DPP are not in this installation.", "fix": ""}]
    found: list[dict] = []

    def say(name: str, state: str, words: str, fix: str = "") -> None:
        found.append({"name": name, "state": state, "words": words, "fix": fix})

    _python, where = engine_host.runtime("upsc")
    missing = _missing_modules()
    if missing is None:
        say("UPSC engine's Python", "bad", f"It could not be started ({where}).",
            "Reinstall NoteCraft, or on a project-folder copy keep the UPSC Notes app's own folder (with its .venv).")
    elif missing:
        say("UPSC engine's Python", "bad", f"Missing: {', '.join(UPSC_MODULES[m] + ' (' + m + ')' for m in missing)}.",
            f"Install the UPSC Notes app's requirements into {where}.")
    else:
        say("UPSC engine's Python", "ok", f"Ready ({where}).")
    mpeg, probe = ffmpeg()
    if mpeg and probe:
        say("ffmpeg and ffprobe", "ok", "Found: class recordings can be turned into a transcript.")
    else:
        say("ffmpeg and ffprobe", "bad", "Not found: recordings without captions cannot be transcribed.",
            "The engine copy's third_party/ffmpeg folder is missing; copy it again.")
    which = office()
    if which:
        say("PDF from Word notes", "ok", f"{which} makes the PDF.")
    else:
        say("PDF from Word notes", "warn", "Neither LibreOffice nor Microsoft Word was found: the Word notes are made, "
            "but not the PDF.", "Install LibreOffice (free) or Microsoft Word.")
    missing_fonts = [name for name, pattern in FONTS.items() if not font_installed(pattern)]
    if missing_fonts:
        say("Handwriting fonts", "warn", f"Not installed: {', '.join(missing_fonts)}. The Word file carries them, "
            "but the PDF falls back to another face.",
            "Install them from engines/upsc/assets/fonts (right-click the .ttf, Install).")
    else:
        say("Handwriting fonts", "ok", "Coming Soon and Kalam are installed.")
    from . import practice_service

    bank = practice_service.masters_status()
    if bank["ready"]:
        say("DPP question bank", "ok", f"Both workbooks are in {bank['folder']}.")
    else:
        say("DPP question bank", "warn", f"Not found in {bank['folder']}: DPP sets cannot be made.",
            "Choose the folder with both workbooks under DPP question bank, below.")
    return found
