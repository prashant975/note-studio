"""Per-update temp workspace on the serverless /tmp disk.

Every update gets its own directory for intermediate files (downloaded PDFs
etc.). The directory is deleted as soon as the update finishes — and on entry
we also sweep any orphaned workspaces older than 30 minutes (crash leftovers),
so temp storage can never keep growing on a warm instance.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid

_BASE = os.path.join(tempfile.gettempdir(), "notesbot-work")
_MAX_AGE_S = 30 * 60


def _sweep() -> None:
    try:
        now = time.time()
        for name in os.listdir(_BASE):
            path = os.path.join(_BASE, name)
            try:
                if now - os.path.getmtime(path) > _MAX_AGE_S:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass  # base dir doesn't exist yet


class Workspace:
    """Context manager: temp dir that is guaranteed-deleted on exit."""

    def __enter__(self) -> str:
        _sweep()
        self.path = os.path.join(_BASE, uuid.uuid4().hex)
        os.makedirs(self.path, exist_ok=True)
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
