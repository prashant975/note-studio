"""Resolve PDF attachments in catalog cells through the engine's link reader.

Keep the copied engine unchanged. Classic sheets already resolve hyperlinks
and Drive chips; catalog planning needs the same fallback before parsing URLs.
"""
from __future__ import annotations

from dataclasses import replace
import threading

_LOCK = threading.Lock()
_INSTALLED = False


def install() -> None:
    """Add the catalog fallback once, after the engine is on the import path."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        from sheets_bridge import http_source
        from sheets_bridge.catalog_flow import CatalogFlow

        original_plan = CatalogFlow.plan

        def plan(self, row):
            if not http_source.is_web_link(row.drive_link):
                lookup = getattr(self.runner.sheet, "cell_hyperlink", None)
                if lookup is not None:
                    url = lookup(row.number, row.layout.require("drive_link"))
                    if url:
                        values = list(row.values)
                        index = row.layout.require("drive_link")
                        values.extend([""] * max(0, index + 1 - len(values)))
                        values[index] = url
                        row = replace(row, values=tuple(values))
            return original_plan(self, row)

        CatalogFlow.plan = plan
        _INSTALLED = True
