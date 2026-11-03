"""Offline page smoke against a fresh temporary workspace (no model or portal writes).

Set CHROMIUM_PATH to a browser executable, or install Playwright Chromium.
Use --packaged to check the locally built macOS app instead of the source.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ["/signin","/","/new","/runs","/settings","/queue","/automation"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--native", action="store_true", help="Check the native macOS Dock launcher.")
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="desktop-compatibility-") as folder:
        temp = Path(folder)
        env = dict(os.environ, QBG_STUDIO_DB_PATH=str(temp/"qbg.db"),
                   PW_AUTH_STATE_DIR=str(temp/"auth"), QBG_PORTAL_AUTH_PATH=str(temp/"portal.json"),
                   QBG_STUDIO_NO_BROWSER="1", QBG_STUDIO_PORT=str(port),
                   NOTE_STUDIO_HOME=str(temp/"notes"), NOTE_STUDIO_OLD_APP_HOME=str(temp/"old"),
                   NOTE_STUDIO_OFFLINE="1", NOTE_STUDIO_NO_BROWSER="1", NOTE_STUDIO_PORT=str(port))
        command = ([str(ROOT/"dist/NoteCraft.app/Contents/MacOS/launch")] if args.packaged
                   else [sys.executable, str(ROOT/"run_studio.py")])
        if args.native:
            command = [str(ROOT/"dist/NoteCraft.app/Contents/MacOS/DesktopLauncher")]
        with (temp/"server.log").open("w") as log:
            # Only this test owns the file handle. Windows venv redirectors
            # and late probe children must not inherit a handle to server.log.
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            def drain_output():
                with proc.stdout:
                    for line in proc.stdout:
                        log.write(line)
                        log.flush()
            output = threading.Thread(target=drain_output, daemon=True)
            output.start()
            try:
                base = f"http://127.0.0.1:{port}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(240):
                    if proc.poll() is not None:
                        raise RuntimeError((temp/"server.log").read_text())
                    try:
                        with opener.open(base+ROUTES[0], timeout=1):
                            break
                    except OSError:
                        time.sleep(0.25)
                else:
                    raise RuntimeError("Startup timed out: "+(temp/"server.log").read_text())
                with sync_playwright() as tools:
                    executable = os.environ.get("CHROMIUM_PATH")
                    browser = tools.chromium.launch(headless=True, executable_path=executable)
                    page = browser.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base+"/")
                               else route.abort())
                    checked = []
                    for route in ROUTES:
                        response = page.goto(base+route)
                        assert response.status == 200, (route, response.status)
                        assert page.locator("body").inner_text().strip()
                        checked.append(route)
                    assert not errors, errors
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(base+ROUTES[0])
                    browser.close()
                print(json.dumps({"packaged": args.packaged or args.native, "native": args.native,
                                  "pages": checked, "javascript_errors": errors}))
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=20)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                output.join(timeout=20)
                if output.is_alive():
                    raise RuntimeError("The owned test process tree did not close its output pipe.")


if __name__ == "__main__":
    main()
