"""Error types and log-redaction helpers shared across the bot."""
from __future__ import annotations

import re


class UserError(Exception):
    """An error whose message is safe and intended to be shown to the user.

    Anything that is NOT a UserError is treated as internal and the user only
    gets a generic message (so we never leak tokens/stack details).
    """


class NotesCoverageError(UserError):
    """Source checking failed; never treat this as an empty lecture."""

    def __init__(self):
        super().__init__("Notes could not be verified against all source pages. Please retry this lecture.")


class ModelBudgetError(UserError):
    """The gateway has exhausted its spending allowance; retries cannot help."""

    code = "model_budget_exceeded"

    def __init__(self):
        super().__init__(
            "The PW model gateway budget is exhausted. Ask the gateway "
            "administrator to renew the budget, then rerun the pending notes."
        )


class NotesQualityError(NotesCoverageError):
    """An unapproved final artifact must never be treated as an empty lecture."""
    code = "not_finalized"

    def __init__(self):
        UserError.__init__(self, "Quality checks could not approve the final PDF. Please retry this lecture.")


class VisualFidelityError(UserError):
    """A fixed, redacted visual-processing error safe for user display."""

    MESSAGES = {
        "inventory_incomplete": "Source visuals ki complete checking confirm nahi ho paayi. Please PDF dobara try karein.",
        "too_many_visuals": "PDF mein supported limit se zyada source visuals hain. Please PDF ko chhote parts mein upload karein.",
        "too_many_pages": "PDF mein supported limit se zyada pages hain. Please PDF ko chhote parts mein upload karein.",
        "too_many_chunks": "PDF mein supported limit se zyada processing chunks hain. Please PDF ko chhote parts mein upload karein.",
        "source_chunk_too_large": "PDF ka ek page safe visual-processing size se bada hai. Please optimized ya chhoti PDF try karein.",
        "crop_failed": "Ek source visual safely extract nahi ho paaya. Incomplete notes dene ke bajay request roki gayi.",
        "link_invalid": "Source visual placement verify nahi ho paayi. Please PDF dobara try karein.",
        "fallback_damaged": "Source visual ka safe sketch version verify nahi ho paaya. Request roki gayi.",
        "render_failed": "Ek verified source visual PDF mein safely render nahi ho paaya. Request roki gayi.",
        "deadline_exceeded": "Visual processing safe time limit tak complete nahi hui. Please chhoti PDF try karein.",
        "work_limit_exceeded": "Visual processing ki safe call limit exceed hui. Request roki gayi.",
        "mathpix_cleanup_failed": "Temporary Mathpix source data safely delete confirm nahi ho paaya. Privacy ke liye request roki gayi.",
        "mathpix_submit_unconfirmed": "Mathpix upload ka temporary-data state confirm nahi ho paaya. Privacy ke liye request roki gayi.",
        "memory_limit_exceeded": "Visual processing safe memory limit se zyada ho rahi thi. Please PDF ko chhote parts mein upload karein.",
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self.MESSAGES[code])


# Telegram bot tokens appear inside request URLs (api.telegram.org/bot<token>/...),
# so network-level exceptions can carry them. Scrub before anything is logged.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]{10,}")
_KEY_RE = re.compile(
    r"(?:AIza[0-9A-Za-z_\-]{10,}|AQ\.[0-9A-Za-z_\-]{10,}|sk-[0-9A-Za-z_\-]{12,})"
)


def redact(text: object) -> str:
    """Return text with bot tokens / API keys masked — safe to print to logs."""
    s = str(text)
    s = _BOT_TOKEN_RE.sub("bot***", s)
    return _KEY_RE.sub("***KEY***", s)
