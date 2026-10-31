"""Default Vercel Python entrypoint; `/api/*` is handled by the web boundary."""

from api.web import handler as WebHandler


class handler(WebHandler):
    """Vercel-detected handler retaining the tested implementation."""


__all__ = ["handler"]
