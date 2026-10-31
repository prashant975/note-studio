"""Desktop entry point — what the packaged .exe actually runs.

Streamlit is a server: normally you type `streamlit run app.py` and open a
browser yourself. A teammate double-clicking an icon will not do either, so
this starts the server in-process, waits for it to answer, and opens the
browser at it. When the window is closed the console stays alive, because
closing the browser tab must not kill a QC run that is still going.

Two things are different inside a PyInstaller bundle and both bite:

* ``__file__`` is inside a temporary extraction directory that changes every
  launch, so app.py, src/ and config/ are located through ``sys._MEIPASS``.
* the working directory is wherever the user's shortcut points, so anything
  written relative to "." would land somewhere unpredictable. Runs and reports
  go beside the executable instead, in a folder the user can actually find.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_PORT = int(os.environ.get("UPSC_QC_PORT", "8501"))


def _bundle_root() -> Path:
    """Where app.py and its data live — the bundle when frozen, the repo when not."""
    frozen = getattr(sys, "_MEIPASS", "")
    return Path(frozen) if frozen else Path(__file__).resolve().parent


def _install_root() -> Path:
    """Where the user's files go: beside the .exe, not inside the bundle.

    A PyInstaller one-file bundle is extracted to a temporary directory and
    deleted on exit, so a report written there would vanish the moment the app
    closed.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _is_our_app(port: int) -> bool:
    """True when what is already listening on ``port`` is this app."""
    try:
        import urllib.request

        with urllib.request.urlopen(
                f"http://localhost:{port}/_stcore/health", timeout=3) as answer:
            return answer.status == 200
    except Exception:
        return False


def _open_when_ready(port: int, timeout: float = 90.0) -> None:
    """Open the browser once the server answers, not before."""
    url = f"http://localhost:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _free(port):                     # something is listening now
            time.sleep(1.0)                     # let the first render finish
            try:
                webbrowser.open(url)
            except Exception:
                print(f"  Open this in your browser: {url}")
            return
        time.sleep(0.4)
    print(f"  The app is taking longer than usual. Try opening {url} yourself.")


def main() -> int:
    root = _bundle_root()
    install = _install_root()

    # Writable locations, beside the executable, created before anything runs.
    os.environ.setdefault("RUNS_DIR", str(install / "runs"))
    os.environ.setdefault("OUTPUT_DIR", str(install / "reports"))
    for name in ("runs", "reports"):
        (install / name).mkdir(parents=True, exist_ok=True)

    app_file = root / "app.py"
    if not app_file.exists():
        print(f"  Could not find app.py inside the bundle ({root}).")
        return 2

    # Streamlit reads secrets relative to the working directory, so the bundle
    # root has to be it — that is where .streamlit/secrets.toml was packed.
    os.chdir(root)
    sys.path.insert(0, str(root))

    # The port is NOT negotiable. Google sign-in only accepts the redirect URI
    # registered for this app — http://localhost:8501/oauth2callback — so a
    # second-choice port produces an app that loads and then cannot sign in,
    # with only an "origin mismatch" line in the console to explain why.
    port = APP_PORT
    if not _free(port):
        if _is_our_app(port):
            print("  UPSC PDF QC is already running. Opening it.")
            try:
                webbrowser.open(f"http://localhost:{port}")
            except Exception:
                print(f"  Open this in your browser: http://localhost:{port}")
            return 0
        print(f"  Port {port} is in use by another program, and this app cannot")
        print(f"  use a different one — Google sign-in is registered for {port}.")
        print("  Close whatever is using it and start the app again.")
        try:
            input("\n  Press Enter to close...")
        except EOFError:
            pass
        return 2

    print("  UPSC PDF QC")
    print(f"  Starting on http://localhost:{port} — this window must stay open.")
    print(f"  Reports are saved to: {install / 'reports'}")
    print()

    threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()

    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit", "run", str(app_file),
        f"--server.port={port}",
        "--server.headless=true",        # we open the browser ourselves
        "--server.address=localhost",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except Exception as exc:                     # pragma: no cover - desktop path
        # A frozen app that closes instantly tells the user nothing, so the
        # error is printed and the window held open until they read it.
        print(f"\n  The app could not start: {exc}\n")
        import traceback

        traceback.print_exc()
        try:
            input("  Press Enter to close...")
        except EOFError:
            pass
