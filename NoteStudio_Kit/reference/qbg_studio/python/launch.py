"""
launch.py - start QBG Studio and open it in the browser.

    python launch.py

Both launchers (RUN_QBG_STUDIO.bat here, QBG_Studio.exe in the team
package) run this instead of calling uvicorn themselves, so the same three
things happen everywhere:

  1. If QBG Studio is already running on this computer, just open it. A
     second copy would fight the first one for the portal session.
  2. If port 8000 is taken by something else, use the next free port and say
     so, rather than crashing with "address already in use".
  3. Open the browser only once the server actually answers, not after a
     fixed delay.

Bound to 127.0.0.1 on purpose: Studio drives your own logged-in Chrome and
the portal allows one session at a time, so it must not be reachable from
elsewhere on the network.
"""

import os
import hashlib
import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
FIRST_PORT = int(os.getenv("QBG_STUDIO_PORT", "8000"))
PORT_RANGE = 10
# What the Overview page of a running copy says. "QBG Studio" was the name until
# 4 October 2026: an older copy still running is the same app, not a stranger on the port.
MARKS = ("QBG One", "QBG Studio")


# A loopback probe must never go through the system proxy: on a laptop with
# a corporate proxy configured, urlopen() would send 127.0.0.1 to the proxy
# and report "not running" for a Studio that is right there.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _studio_answers(port, timeout=1.5):
    """True when the thing on this port is a QBG Studio, not just 'something'."""
    try:
        with _LOCAL.open("http://%s:%d/dashboard" % (HOST, port),
                         timeout=timeout) as resp:
            body = resp.read(20000).decode("utf-8", "replace")
            return any(mark in body for mark in MARKS)
    except Exception:
        return False


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) != 0


def _same_package(port):
    """Do not silently open a different installation and its database."""
    expected = hashlib.sha256(os.path.normcase(
        str((HERE / 'data' / 'qbg_studio.db').resolve())).encode('utf-8')).hexdigest()
    try:
        with _LOCAL.open('http://%s:%d/api/instance' % (HOST, port), timeout=3) as response:
            return json.load(response).get('instance') == expected
    except Exception:
        return False


def _open_when_ready(url, port, limit=60):
    deadline = time.time() + limit
    while time.time() < deadline:
        if _same_package(port):
            print('QBG_READY|' + url, flush=True)
            if os.getenv('QBG_STUDIO_NO_BROWSER') != '1':
                webbrowser.open(url)
            return
        time.sleep(0.5)


def main():
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    os.environ.setdefault("PYTHONUTF8", "1")

    for port in range(FIRST_PORT, FIRST_PORT + PORT_RANGE):
        url = "http://%s:%d" % (HOST, port)
        if _port_free(port):
            break
        if _studio_answers(port):
            if not _same_package(port):
                print('Another QBG One copy is running at %s.' % url)
                print('Close its black window first, then start this package again to use its database.')
                return 1
            print("QBG One is already running at %s - opening it." % url)
            print("Close the other black window first if you meant to restart it.")
            print('QBG_READY|' + url, flush=True)
            if os.getenv('QBG_STUDIO_NO_BROWSER') != '1':
                webbrowser.open(url)
            return 0
        print("Port %d is in use by another program; trying the next one." % port)
    else:
        print("Ports %d-%d are all in use. Close some programs and try again."
              % (FIRST_PORT, FIRST_PORT + PORT_RANGE - 1))
        return 1

    if port != FIRST_PORT:
        print()
        print("  NOTE: QBG One is on %s this time (port %d was busy)." % (url, port))
        print()

    print("  Starting QBG One on %s" % url)
    print("  Your browser will open when it is ready.")
    print("  Leave this window open while you work. Close it to stop.")
    print()

    threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()

    # Teammate sync runs a fix the owner sent from his laptop against this
    # very server, so it needs to know the port chosen above.
    os.environ["QBG_STUDIO_SERVER_PORT"] = str(port)
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
