"""The one exception the whole bridge raises.

Every failure a caller can do something about — Chrome not running, login
expired, an editor that could not be identified, a save the portal rejected —
arrives as a QbgBridgeError with a message written for the person at the
screen, usually naming the diagnostic screenshot that was just saved.
"""
from __future__ import annotations


class QbgBridgeError(Exception):
    """A QBG portal operation failed in a way the operator must resolve."""
