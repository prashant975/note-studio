"""Live QBG-portal access for the DPP builder.

The essential machinery of the QBG Solution Addition Bot, carried into this
project so a DPP question — whose ``question_id`` IS the portal's unique_id —
can be opened on qbg-admin.penpencil.co and overwritten from the app, with the
save verified by reopening the question.

Requirements at run time (none at import time):
- ``playwright`` with Chrome available (already in this project's venv);
- a debug Chrome that is logged in to QBG:
  see ``debug_chrome_command()`` for the exact command, or ``QBG_HEADLESS=1``
  with a previously logged-in ``QBG_CHROME_PROFILE``.

Everything raises QbgBridgeError with operator-readable messages; diagnostic
screenshots land in ``runs/_qbg_debug``.
"""
from .client import (
    QbgEdit,
    QbgPushResult,
    edit_question,
    fetch_question,
    open_question,
    read_question_fields,
)
from .errors import QbgBridgeError
from .textops import (
    indexes_to_letters,
    is_qbg_link,
    looks_like_qbg_id,
    normalize_option_letters,
    question_link,
)

__all__ = [
    "QbgBridgeError",
    "QbgEdit",
    "QbgPushResult",
    "debug_chrome_command",
    "edit_question",
    "fetch_question",
    "indexes_to_letters",
    "is_qbg_link",
    "looks_like_qbg_id",
    "normalize_option_letters",
    "open_question",
    "question_link",
    "read_question_fields",
]


def debug_chrome_command() -> str:
    from .browser import debug_chrome_command as _command

    return _command()
