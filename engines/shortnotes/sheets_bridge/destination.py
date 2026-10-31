"""The Drive tree a catalog sheet's notes are filed in.

``FolderTree`` finds or creates ``exam/batch/subject/chapter`` under the
profile's output folder. Names are compared case-insensitively with
whitespace tidied, so a folder someone made by hand ("jee ", "physics") is
used rather than doubled. Drive allows two folders with one name; the oldest
is used and the log says so. Each folder is listed at most once per run, and a
folder this run created is known to be empty, so it is never listed at all.
"""
from __future__ import annotations

import unicodedata

from .errors import BridgeError


def folder_key(name: object) -> str:
    """Comparison form of a folder name."""
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    return " ".join(text.split())


class FolderTree:
    def __init__(self, drive, root) -> None:
        self.drive = drive
        self.root = root
        self._listings: dict[str, list] = {}
        self.created: list[tuple[str, ...]] = []      # paths of folders made this run
        self.doubled: list[str] = []                  # names that exist more than once

    @property
    def drive_id(self) -> str:
        return str(getattr(self.root, "drive_id", "") or "")

    @classmethod
    def open(cls, drive, folder_id: str) -> "FolderTree":
        """The output folder itself; refuses a file id or an unreachable folder."""
        try:
            root = drive.get_file(folder_id)
        except BridgeError as exc:
            raise BridgeError("output_folder_unavailable", exc.code) from None
        if not getattr(root, "is_folder", False):
            raise BridgeError("output_folder_not_a_folder", "the output folder link must point at a folder")
        return cls(drive, root)

    # -- listings -------------------------------------------------------------
    def children(self, folder_id: str) -> list:
        if folder_id not in self._listings:
            self._listings[folder_id] = list(self.drive.list_folder(folder_id, self.drive_id))
        return self._listings[folder_id]

    def remember(self, folder_id: str, entry) -> None:
        """Add an entry this run created, so later rows see it without a new listing."""
        if folder_id in self._listings:
            self._listings[folder_id].append(entry)

    # -- folders ----------------------------------------------------------------
    def find_folder(self, parent_id: str, names):
        """The child folder called ``names`` - one name, or a tuple of
        acceptable names tried in order (the exam level: "JEE", "IIT-JEE")."""
        options = (names,) if isinstance(names, str) else tuple(names)
        folders = [f for f in self.children(parent_id) if getattr(f, "is_folder", False)]
        for name in options:
            key = folder_key(name)
            matches = [f for f in folders if folder_key(f.name) == key]
            if not matches:
                continue
            if len(matches) > 1 and name not in self.doubled:
                self.doubled.append(name)
            exact = [f for f in matches if f.name == name]
            pool = exact or matches
            # Oldest first (RFC 3339 times sort as text); no time sorts last.
            return sorted(pool, key=lambda f: (str(getattr(f, "modified_time", "") or "") or "~", f.name, f.id))[0]
        return None

    def find_path(self, names):
        """The folder at the end of ``names`` when every level exists (read-only)."""
        folder = self.root
        for name in names:
            found = self.find_folder(folder.id, name)
            if found is None:
                return None
            folder = found
        return folder

    def ensure_path(self, names):
        """The folder at the end of ``names``, creating the missing levels."""
        folder = self.root
        walked: list[str] = []
        for names_here in names:
            name = names_here if isinstance(names_here, str) else names_here[0]
            walked.append(name)
            found = self.find_folder(folder.id, names_here)
            if found is None:
                found = self.drive.create_folder(folder.id, name, self.drive_id)
                self.remember(folder.id, found)
                self._listings.setdefault(found.id, [])     # just made: empty, no listing needed
                self.created.append(tuple(walked))
            folder = found
        return folder

    # -- files ------------------------------------------------------------------
    def find_file(self, folder_id: str, name: str):
        for entry in self.children(folder_id):
            if not getattr(entry, "is_folder", False) and entry.name == name:
                return entry
        return None
