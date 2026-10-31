from __future__ import annotations

import importlib
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


PORT = 8501


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _open_app_when_ready(url: str) -> None:
    """Open the local app page only after Streamlit reports healthy.

    This does not initiate Google sign-in; OAuth still starts exclusively from
    the user's Continue/Renew button inside the app.
    """
    def _open() -> None:
        health_url = f"{url}/_stcore/health"
        for _ in range(120):
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    if response.status == 200 and response.read().strip() == b"ok":
                        webbrowser.open(url)
                        return
            except Exception:
                time.sleep(1)
        print(f"Server is running, but the browser did not open. Open {url}")

    threading.Thread(target=_open, daemon=True).start()


#: Modules that hold their own reference to get_static_dir, by Streamlit
#: version. Missing ones are skipped: which of these exists depends on the
#: release, and the app must not care.
_STATIC_ROUTE_MODULES = (
    "streamlit.web.server.starlette.starlette_static_routes",
    "streamlit.web.server.routes",
    "streamlit.web.server.server",
)


def _patch_streamlit_static_dir(bundle_dir: Path) -> None:
    """Make the frozen app serve its own copy of the Streamlit frontend.

    Streamlit decides whether it is a development checkout by looking for
    "site-packages" in streamlit/config.py's own path:

        def _global_development_mode() -> bool:
            return (not env_util.is_pex()
                    and "site-packages" not in __file__ ...)

    Inside a PyInstaller bundle that path is _internal/streamlit/config.py, so
    the answer is "yes, development" and the server serves no static files at
    all -- every page request returns 404 while /_stcore/health still returns
    200, which is why this looks like a working server until you open it.

    Neither of the obvious overrides reaches it. bootstrap.run() takes
    flag_options but only installs config *watchers* with them; applying them is
    the CLI's job, and the launcher calls bootstrap directly. So the option has
    to be set here, and it has to be set before Server() is constructed.

    Order matters below: the config calls come first and depend on nothing
    optional, because they used to sit under an import of a module that no
    longer exists (starlette_static_routes, gone in 1.48), which meant the whole
    block was skipped and the failure was one line of stdout nobody reads.
    """
    static_dir = bundle_dir / "streamlit" / "static"
    print(f"Streamlit static folder: {static_dir}")
    print(f"Streamlit static folder exists: {static_dir.exists()}")
    if not static_dir.exists():
        print("WARNING: the bundled Streamlit frontend is missing; the app will not load.")
        return

    import streamlit.config as config
    import streamlit.file_util as file_util

    config.set_option("global.developmentMode", False, "launcher")
    config.set_option("server.headless", True, "launcher")
    config.set_option("logger.hideWelcomeMessage", True, "launcher")
    config.set_option("server.port", PORT, "launcher")
    # Frozen sources are read-only. Watching every bundled library created
    # thousands of watch events and delayed the first local page response.
    config.set_option("server.fileWatcherType", "none", "launcher")

    def _static_dir() -> str:
        return str(static_dir)

    file_util.get_static_dir = _static_dir
    patched = ["streamlit.file_util"]
    for name in _STATIC_ROUTE_MODULES:
        module = sys.modules.get(name)
        if module is None:
            try:
                module = importlib.import_module(name)
            except Exception:
                continue
        if getattr(module, "file_util", None) is not None:
            module.file_util.get_static_dir = _static_dir
            patched.append(name)

    print(f"Serving the bundled frontend (development mode off). Patched: {', '.join(patched)}")


def main() -> None:
    url = f"http://127.0.0.1:{PORT}"
    if not _port_is_free(PORT):
        print("")
        print(f"Port {PORT} is already in use.")
        print("Close any old UpscNotesApp.exe window/process, then run this app again.")
        print("")
        input("Press Enter to exit...")
        return

    bundle_dir = _bundle_dir()
    runtime_dir = _runtime_dir()
    external_env = runtime_dir / ".env"

    # The bundle carries its own ffmpeg/ffprobe so a fresh machine needs no
    # manual install; a machine that already has ffmpeg on PATH keeps using its
    # own (an existing install always wins — the bundled copy only fills a gap).
    bundled_ffmpeg = bundle_dir / "ffmpeg"
    if shutil.which("ffmpeg") is None and (bundled_ffmpeg / "ffmpeg.exe").exists():
        os.environ["PATH"] = str(bundled_ffmpeg) + os.pathsep + os.environ.get("PATH", "")
        print(f"Using the bundled ffmpeg ({bundled_ffmpeg})")

    # The DPP question-bank workbooks, from a `data` folder NEXT TO THE EXE.
    #
    # They are 53 MB of PW's own question content and are deliberately not in
    # the bundle. Without this the app looked for them under HANDWRITTEN_NOTES_ROOT
    # — which for a frozen build is PyInstaller's `_internal` folder — so the
    # panel's own instruction, "put them in the app's data folder", named a
    # place the app never looked, and dropping the files beside the exe (the
    # only sensible reading) silently did nothing.
    #
    # setdefault throughout: an admin who has already set QBG_TAGS_PATH to a
    # shared network copy keeps it.
    external_data = runtime_dir / "data"
    for var, filename in (("QBG_TAGS_PATH", "UPSC QBG Tags.xlsx"),
                          ("QUESTION_BANK_PATH",
                           "DPP and Infinite Practice Questions.xlsx")):
        candidate = external_data / filename
        if candidate.is_file():
            os.environ.setdefault(var, str(candidate))
            print(f"Using the question bank workbook at {candidate}")

    os.environ.setdefault("HANDWRITTEN_NOTES_ROOT", str(bundle_dir))
    os.environ.setdefault("HANDWRITTEN_NOTES_ENV", str(external_env if external_env.exists() else bundle_dir / ".env"))
    os.environ.setdefault("RUNS_DIR", str(runtime_dir / "runs"))
    os.environ.setdefault("OUTPUT_DIR", str(runtime_dir / "outputs"))
    os.environ["STREAMLIT_SERVER_PORT"] = str(PORT)
    os.environ["STREAMLIT_BROWSER_SERVER_ADDRESS"] = "localhost"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_SERVER_ENABLE_CORS"] = "true"
    os.environ["STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION"] = "true"
    os.environ["STREAMLIT_LOGGER_HIDE_WELCOME_MESSAGE"] = "true"
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"

    (runtime_dir / "runs").mkdir(exist_ok=True)
    (runtime_dir / "outputs").mkdir(exist_ok=True)

    if getattr(sys, "frozen", False):
        os.chdir(str(bundle_dir))
        _patch_streamlit_static_dir(bundle_dir)

    app_path = bundle_dir / "app.py"
    flag_options = {
        "global.developmentMode": False,
        "server.port": PORT,
        "server.headless": True,
        "server.enableCORS": True,
        "server.enableXsrfProtection": True,
        "logger.hideWelcomeMessage": True,
        "browser.serverAddress": "localhost",
    }
    print("")
    print(f"Opening UPSC_Notes at {url}")
    print("Keep this window open while using the app.")
    print("")
    _open_app_when_ready(url)
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(PORT),
        "--server.headless",
        "true",
        "--logger.hideWelcomeMessage",
        "true",
        "--global.developmentMode",
        "false",
    ]
    from streamlit.web import bootstrap

    bootstrap.run(str(app_path), False, [], flag_options)


def run_selftest() -> int:
    """`UpscNotesApp.exe --selftest`: import every module under src/ and the
    packages they defer to, build one DPP export in each script, print a
    report and exit -- no server, no browser.

    A release is not finished until this passes on the built EXE. The browser
    smoke test cannot stand in for it: src/ ships as data, so a package that
    PyInstaller left out fails only when the code that imports it runs, and
    that code sits behind a button on a page behind a login (v2.11.0 shipped
    that way). See src/selftest.py for what the report covers.
    """
    bundle_dir = _bundle_dir()
    if str(bundle_dir) not in sys.path:
        sys.path.insert(0, str(bundle_dir))
    # The same root main() gives the app, so fonts and config resolve the
    # way they will at runtime.
    os.environ.setdefault("HANDWRITTEN_NOTES_ROOT", str(bundle_dir))
    # The report is usually redirected to a file, and a redirected stream on
    # Windows is cp1252: one Devanagari character in an exception message
    # would turn the report into a UnicodeEncodeError of its own.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    from src.selftest import run

    return run(bundle_dir)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(run_selftest())
    main()
