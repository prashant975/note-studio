"""Start NoteCraft and open it in the browser (adapted from QBG One's launch.py).

    python run_studio.py              (or double-click "Start NoteCraft.bat")

1. If this NoteCraft is already running, just open it: two copies would share one
   database and one queue.
2. Port 3100 first, so NoteCraft runs beside the old Short Notes app on 3000,
   QBG One on 8000 and the Streamlit tools on 8501. If 3100 is taken by
   anything else - another program, or a NoteCraft copy with its own data
   folder - use the next free one up to 3110 and say so. Never open another
   app by mistake, and never refuse to start because of one.
3. Open the browser only once the server answers.

Bound to 127.0.0.1 on purpose: it is a desktop app, not reachable from the network.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from . import brand, paths

HOST = "127.0.0.1"
FIRST_PORT = int(os.environ.get("NOTE_STUDIO_PORT", "3100"))
LAST_PORT = FIRST_PORT + 10

# A loopback probe must never go through a system proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) != 0


def answering_instance(port: int, timeout: float = 2.0) -> str:
    """The instance id of a NoteCraft on this port, or '' if it is something else."""
    try:
        with _LOCAL.open(f"http://{HOST}:{port}/api/instance", timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return ""
    return data.get("instance", "") if data.get("app") == brand.APP_NAME else ""


def pick_port(first: int = FIRST_PORT, last: int = LAST_PORT) -> tuple[int, str]:
    """(port, what is there): 'mine' (this NoteCraft, already running) or 'free'.
    A port held by anything else - another program, another NoteCraft copy with
    its own data folder - is skipped. (0, 'none') when the whole range is taken."""
    mine = paths.instance_id()
    for port in range(first, last + 1):          # this NoteCraft first, wherever it ended up
        if not port_free(port) and answering_instance(port) == mine:
            return port, "mine"
    for port in range(first, last + 1):
        if port_free(port):
            return port, "free"
    return 0, "none"


def _open_when_ready(url: str, port: int, limit: float = 60) -> None:
    deadline = time.time() + limit
    while time.time() < deadline:
        if answering_instance(port) == paths.instance_id():
            print("NOTESTUDIO_READY|" + url, flush=True)
            if os.environ.get("NOTE_STUDIO_NO_BROWSER") != "1":
                webbrowser.open(url + "/signin")
            return
        time.sleep(0.5)


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    name = brand.APP_NAME
    port, found = pick_port()
    url = f"http://{HOST}:{port}"
    if found == "none":
        print(f"Ports {FIRST_PORT}-{LAST_PORT} are all in use. Close some programs and try again.")
        return 1
    if found == "mine":
        print(f"{name} is already running at {url} - opening it.")
        if os.environ.get("NOTE_STUDIO_NO_BROWSER") != "1":
            webbrowser.open(url + "/signin")
        return 0
    if port != FIRST_PORT:
        print(f"\n  NOTE: {name} is on {url} this time (port {FIRST_PORT} was busy).\n")
    print(f"  Starting {name} on {url}")
    print("  Your browser will open when it is ready.")
    print("  Leave this window open while you work. Close it to stop.\n")
    threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    import uvicorn
    uvicorn.run("note_studio.app:app", host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
