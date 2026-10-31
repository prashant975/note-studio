"""Where things are. Functions, not constants, so a test can point them at tmp_path.

- NoteCraft's own data lives only in `~/.note_studio` (NOTE_STUDIO_HOME overrides).
- The old Short Notes app's folder `~/.shortnotes_bridge` is shared only when the
  user agrees (old_app.py); NOTE_STUDIO_OLD_APP_HOME overrides it for tests.
- The engine copies live in the project's `engines/` folder.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINES_DIR = PROJECT_ROOT / "engines"
PACKAGE_DIR = Path(__file__).resolve().parent


def home() -> Path:
    """NoteCraft's data folder (created on first use)."""
    override = os.environ.get("NOTE_STUDIO_HOME", "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".note_studio"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return home() / "note_studio.db"


def instance_id() -> str:
    """Which copy is answering on a port: the launcher opens only its own."""
    return hashlib.sha256(os.path.normcase(str(db_path().resolve())).encode("utf-8")).hexdigest()


def logs_dir() -> Path:
    path = home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def old_app_home() -> Path:
    """The old Short Notes app's folder. Never created or written here."""
    override = os.environ.get("NOTE_STUDIO_OLD_APP_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".shortnotes_bridge"


def shortnotes_original() -> Path:
    """The original shortnotes project, read in place (its OAuth client file)."""
    override = os.environ.get("NOTE_STUDIO_SHORTNOTES_ORIGINAL", "").strip()
    return Path(override).expanduser() if override else Path.home() / "Downloads" / "shortnotes-core"


def upsc_original() -> Path:
    """The original UPSC notes app, read in place (its runs/, and its Python until Phase 9)."""
    override = os.environ.get("NOTE_STUDIO_UPSC_ORIGINAL", "").strip()
    return Path(override).expanduser() if override else Path.home() / "Downloads" / "Upsc_Notes_2" / "upsc_Notes"


ORIGINAL_FOLDERS = {
    "upsc": ("Upsc_Notes_2", "upsc_Notes"),
    "pw_books": ("handwritten_notes_automation_complete", "handwritten_notes_automation_complete"),
    "notes_qc": ("upsc_file_QC",),
}


def signin_registration() -> Path:
    """Where an installed NoteCraft keeps the Google sign-in registration (client id, no tokens)."""
    return PROJECT_ROOT / "deployment" / "client_secret.json"


def team_key_files() -> list[Path]:
    """Where the team's service-account key may be, in order: this computer's own
    (added in Settings, in the data folder), then the one built into an install."""
    return [home() / "team_key.json", PROJECT_ROOT / "deployment" / "service_account.json"]


def libs_dir() -> Path | None:
    """An installed NoteCraft's library folders (_internal/libs/<engine>); None in the project folder."""
    folder = PROJECT_ROOT.parent / "libs"
    return folder if (folder / "notecraft").is_dir() and (PROJECT_ROOT / "engine_boot.py").is_file() else None


def original(engine_id: str) -> Path | None:
    """An engine's original project, read in place (NOTE_STUDIO_<ENGINE>_ORIGINAL overrides)."""
    if engine_id == "upsc":
        return upsc_original()
    override = os.environ.get(f"NOTE_STUDIO_{engine_id.upper()}_ORIGINAL", "").strip()
    if override:
        return Path(override).expanduser()
    parts = ORIGINAL_FOLDERS.get(engine_id)
    return Path.home().joinpath("Downloads", *parts) if parts else None
