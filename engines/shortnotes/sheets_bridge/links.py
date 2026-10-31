"""Google Drive link parsing.

``parse_drive_link`` accepts both *file* links (``/file/d/{id}``, ``?id={id}``)
and *folder* links (``/drive/folders/{id}``) and tells the caller which one it
got. Folder links are resolved to a lecture PDF by ``lecture_match`` using the
row's Lecture Number - never by guessing.

``parse_drive_file_link`` is the strict file-only variant (refuses folders).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .errors import BridgeError

_FILE_ID = r"[A-Za-z0-9_-]{10,}"
_FILE_D_RE = re.compile(rf"/file/(?:u/\d+/)?d/({_FILE_ID})(?:[/?#]|$)")
_FOLDER_RE = re.compile(rf"/folders/({_FILE_ID})(?:[/?#]|$)")
_ID_ONLY_RE = re.compile(rf"^{_FILE_ID}$")

KIND_FILE = "file"
KIND_FOLDER = "folder"


@dataclass(frozen=True)
class DriveLink:
    kind: str   # KIND_FILE | KIND_FOLDER
    id: str


def parse_drive_link(raw: object) -> DriveLink:
    """Return the Drive id and whether it is a file or a folder link.

    Raises ``BridgeError`` with code ``blank_link`` or ``invalid_link``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise BridgeError("blank_link")
    text = raw.strip()

    match = _FOLDER_RE.search(text)
    if match:
        return DriveLink(KIND_FOLDER, match.group(1))

    match = _FILE_D_RE.search(text)
    if match:
        return DriveLink(KIND_FILE, match.group(1))

    try:
        parts = urlsplit(text)
    except ValueError:
        raise BridgeError("invalid_link") from None
    if parts.query:
        ids = parse_qs(parts.query).get("id") or []
        if len(ids) == 1 and _ID_ONLY_RE.match(ids[0]):
            # open?id= / uc?id= point at files; a folder id here would fail
            # later at files.get with a clear mimeType check.
            return DriveLink(KIND_FILE, ids[0])

    raise BridgeError("invalid_link")


def parse_drive_file_link(raw: object) -> str:
    """Strict variant: return a *file* id; folder links raise
    ``folder_link_needs_resolver``."""
    link = parse_drive_link(raw)
    if link.kind == KIND_FOLDER:
        raise BridgeError("folder_link_needs_resolver")
    return link.id
