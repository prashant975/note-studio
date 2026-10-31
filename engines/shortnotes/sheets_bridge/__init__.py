"""Google Sheet -> Drive -> notes generator -> Drive -> Sheet bridge.

This package only *imports* from ``notesbot``; it never modifies it and it does
not route through the HTTP/web layer (sessions, CSRF, Blob storage).
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
