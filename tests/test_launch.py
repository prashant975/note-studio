"""The launcher: port 3100 first, the next free one if taken, and it reopens a
NoteCraft that is already running instead of starting a second copy."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from note_studio import brand, launch, paths


def _serve(body: dict | None, port: int = 0):
    """A server on a loopback port (a free one by default); body=None answers 404 like a stranger."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if body is None or self.path != "/api/instance":
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_default_port_is_3100_up_to_3110():
    assert launch.FIRST_PORT == 3100 and launch.LAST_PORT == 3110


def test_a_free_port_is_used():
    port = _free_port()
    assert launch.pick_port(port, port) == (port, "free")


def test_a_port_held_by_another_program_is_skipped():
    stranger = _serve(None)
    try:
        port = stranger.server_address[1]
        assert launch.pick_port(port, port) == (0, "none")
    finally:
        stranger.shutdown()


def test_this_notecraft_already_running_is_reopened():
    mine = _serve({"app": brand.APP_NAME, "instance": paths.instance_id()})
    try:
        port = mine.server_address[1]
        assert launch.pick_port(port, port) == (port, "mine")
    finally:
        mine.shutdown()


def _bindable(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _two_ports_one_held(body):
    """A server on port N with N-1 and N+1 free (as 3100 then 3101). Fixed ports below
    Windows' temporary range: a connect to a temporary port can meet itself."""
    for port in range(23101, 23999, 3):
        if not (_bindable(port - 1) and _bindable(port) and _bindable(port + 1)):
            continue
        return _serve(body, port), port
    raise RuntimeError("no free ports")


def test_another_notecraft_copy_is_not_opened_by_mistake_and_does_not_block():
    """A NoteCraft with its own data folder on the port: this one starts on the next."""
    other, port = _two_ports_one_held({"app": brand.APP_NAME, "instance": "a-different-database"})
    try:
        assert launch.pick_port(port, port) == (0, "none")
        assert launch.pick_port(port, port + 1) == (port + 1, "free")
    finally:
        other.shutdown()


def test_another_app_on_the_port_is_never_opened(monkeypatch):
    """QBG One (or any tool) on the first port: NoteCraft starts on the next, never opens it."""
    qbg, port = _two_ports_one_held({"app": "QBG One", "instance": "x"})
    try:
        assert launch.pick_port(port, port + 1) == (port + 1, "free")
    finally:
        qbg.shutdown()


def test_this_notecraft_is_found_on_a_later_port_first():
    """Started on 3101 because 3100 was busy; 3100 free again: reopen 3101, never a second copy."""
    mine, port = _two_ports_one_held({"app": brand.APP_NAME, "instance": paths.instance_id()})
    try:
        assert launch.pick_port(port - 1, port) == (port, "mine")
    finally:
        mine.shutdown()


def test_notecrafts_ports_stay_clear_of_the_teams_other_tools():
    taken = {3000, *range(8000, 8010), *range(8501, 8511)}            # old Short Notes, QBG One, Streamlit tools
    assert not taken & set(range(launch.FIRST_PORT, launch.LAST_PORT + 1))
