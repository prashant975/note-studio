"""Where practice sessions live between visits.

One JSON file per session inside the note's own run folder, plus an index the
history list reads without opening every file:

    runs/<note_id>/dmp/<dmp_session_id>.json
    runs/<note_id>/dmp/index.json

The run folder rather than a database because that is where this project already
keeps everything a run produced — the notes, the prompts, the QC trail, the
reports. A session stored anywhere else would be the one artefact that does not
travel with the run when the folder is zipped, copied to a colleague, or
archived.

Writes are atomic (temp file + replace). A practice set represents real money
already spent on model calls, and a half-written JSON file loses all of it —
which is exactly what a crash during `json.dump` into the live path produces.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..config import RUNS_DIR
from .models import DmpSession

#: Run ids this project makes: ``run_20260820_185043_80f94595``. Session ids:
#: ``dmp_1a2b3c4d``. Both are checked before they are used as path segments —
#: the ids come from our own session state today, but a stored id is
#: user-adjacent data and "../.." is a path segment too.
_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SESSION_ID_RE = re.compile(r"^dmp_[A-Za-z0-9]+$")

DMP_DIRNAME = "dmp"
INDEX_NAME = "index.json"


class DmpStoreError(RuntimeError):
    """The session could not be read or written."""


def _check_note_id(note_id: str) -> str:
    note_id = (note_id or "").strip()
    if not note_id or not _NOTE_ID_RE.match(note_id):
        raise DmpStoreError(f"Not a usable note id: {note_id!r}")
    return note_id


def _check_session_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        raise DmpStoreError(f"Not a usable practice-set id: {session_id!r}")
    return session_id


def note_run_dir(note_id: str, runs_dir: Path | None = None) -> Path:
    """The run folder a note lives in. The note id IS the folder name."""
    return Path(runs_dir or RUNS_DIR) / _check_note_id(note_id)


def dmp_dir(note_id: str, runs_dir: Path | None = None) -> Path:
    return note_run_dir(note_id, runs_dir) / DMP_DIRNAME


def session_path(note_id: str, session_id: str, runs_dir: Path | None = None) -> Path:
    return dmp_dir(note_id, runs_dir) / f"{_check_session_id(session_id)}.json"


def index_path(note_id: str, runs_dir: Path | None = None) -> Path:
    return dmp_dir(note_id, runs_dir) / INDEX_NAME


def _write_atomic(path: Path, payload: object) -> None:
    """Write JSON so an interrupted write cannot destroy the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def save(session: DmpSession, runs_dir: Path | None = None) -> Path:
    """Write the session and refresh its row in the index. Returns the path."""
    path = session_path(session.note_id, session.dmp_session_id, runs_dir)
    _write_atomic(path, session.to_dict())
    _update_index(session, runs_dir)
    return path


def _update_index(session: DmpSession, runs_dir: Path | None = None) -> None:
    """Replace this session's row, keeping every other row as it stands.

    A failure here is swallowed: the session file is the record and the index is
    a convenience rebuilt from it (see ``rebuild_index``). Losing the practice
    set because its summary line could not be written would be the wrong trade.
    """
    try:
        rows = [r for r in read_index(session.note_id, runs_dir)
                if r.get("dmp_session_id") != session.dmp_session_id]
        rows.append(session.index_entry())
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        _write_atomic(index_path(session.note_id, runs_dir), rows)
    except Exception:  # noqa: BLE001 — never lose a session over its index row
        pass


def read_index(note_id: str, runs_dir: Path | None = None) -> list[dict]:
    """Rows in the index, newest first. Missing or corrupt index reads as empty."""
    path = index_path(note_id, runs_dir)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def load(note_id: str, session_id: str, runs_dir: Path | None = None) -> DmpSession | None:
    """One session, or None if it is not there. Never raises on a bad file."""
    path = session_path(note_id, session_id, runs_dir)
    if not path.exists():
        return None
    try:
        return DmpSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        return None


def list_sessions(note_id: str, runs_dir: Path | None = None) -> list[dict]:
    """History rows for a note, newest first.

    Falls back to reading the session files when the index is missing — which is
    the normal state for a folder that was copied, restored from a zip, or
    written by a build before the index existed.
    """
    rows = read_index(note_id, runs_dir)
    if rows:
        return rows
    return rebuild_index(note_id, runs_dir)


def rebuild_index(note_id: str, runs_dir: Path | None = None) -> list[dict]:
    """Regenerate the index from the session files that are actually present."""
    folder = dmp_dir(note_id, runs_dir)
    if not folder.exists():
        return []
    rows: list[dict] = []
    for path in sorted(folder.glob("dmp_*.json")):
        try:
            session = DmpSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, AttributeError):
            continue
        rows.append(session.index_entry())
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if rows:
        try:
            _write_atomic(index_path(note_id, runs_dir), rows)
        except Exception:  # noqa: BLE001 — the rows are still returned
            pass
    return rows


def latest_session(note_id: str, runs_dir: Path | None = None) -> DmpSession | None:
    """The most recent practice set for a note — what the panel reopens on."""
    rows = list_sessions(note_id, runs_dir)
    if not rows:
        return None
    return load(note_id, str(rows[0].get("dmp_session_id") or ""), runs_dir)
