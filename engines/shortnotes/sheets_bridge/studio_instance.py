"""Recognize the local Studio and open its existing browser interface."""
from __future__ import annotations

import http.client
import json
import webbrowser


def _get(port: int, path: str, limit: int) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(limit)
    finally:
        connection.close()


def activate_existing_studio(port: int, *, open_browser: bool = True) -> bool:
    """Reuse a healthy Studio on this port, without starting another server.

    Check both its health response and its page title so another local app
    occupying the port still gets the normal port-conflict error.
    """
    try:
        status, body = _get(port, "/api/web?action=health", 1024)
        if status != 200 or json.loads(body).get("ok") is not True:
            return False
        status, body = _get(port, "/", 65536)
        if status != 200 or b"<title>Short Notes Studio</title>" not in body:
            return False
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        return False

    url = f"http://localhost:{port}/"
    print(f"Short Notes Studio is already running: {url}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # opening the browser must not stop the existing app
            pass
    return True
