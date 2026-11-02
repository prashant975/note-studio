"""Every test: its own data folder in tmp_path, no network, and the real
~/.note_studio and ~/.shortnotes_bridge never touched."""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before anything imports the app (it prepares its database on import).
_SESSION = Path(tempfile.mkdtemp(prefix="notecraft-tests-"))
os.environ["NOTE_STUDIO_HOME"] = str(_SESSION / "home")
os.environ["NOTE_STUDIO_OLD_APP_HOME"] = str(_SESSION / "old_app")
os.environ["NOTE_STUDIO_SHORTNOTES_ORIGINAL"] = str(_SESSION / "shortnotes_original")
os.environ["NOTE_STUDIO_OFFLINE"] = "1"          # never restore a Google sign-in in tests
os.environ["NOTE_STUDIO_OLD_APP_PORT"] = "1"      # the real old app being open must not change a test
# The UPSC engine child runs on the original app's own Python (read only); its runs/ are never read in tests.
_PYTHON = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
UPSC_PYTHON = ROOT / "engines/upsc/.venv" / _PYTHON
if not UPSC_PYTHON.is_file():
    UPSC_PYTHON = Path.home() / "Downloads/Upsc_Notes_2/upsc_Notes/.venv" / _PYTHON
if UPSC_PYTHON.is_file():
    os.environ["NOTE_STUDIO_UPSC_PYTHON"] = str(UPSC_PYTHON)
os.environ["NOTE_STUDIO_UPSC_ORIGINAL"] = str(_SESSION / "upsc_original")
# The same for the other child-process engines (their originals' own Pythons, read only).
for _engine, _parts in (("PW_BOOKS", ("handwritten_notes_automation_complete", "handwritten_notes_automation_complete")),
                        ("NOTES_QC", ("upsc_file_QC",))):
    _python = ROOT / "engines" / _engine.lower() / ".venv" / _PYTHON
    if not _python.is_file():
        _python = Path.home().joinpath("Downloads", *_parts, ".venv") / _PYTHON
    if _python.is_file():
        os.environ[f"NOTE_STUDIO_{_engine}_PYTHON"] = str(_python)
    os.environ[f"NOTE_STUDIO_{_engine}_ORIGINAL"] = str(_SESSION / f"{_engine.lower()}_original")

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex


def _host(address) -> str:
    return address[0] if isinstance(address, tuple) else str(address)


def _guarded_connect(self, address):
    if _host(address) not in _LOOPBACK:
        raise RuntimeError(f"network is blocked in tests ({_host(address)})")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if _host(address) not in _LOOPBACK:
        raise RuntimeError(f"network is blocked in tests ({_host(address)})")
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fresh data folder and old-app folder per test; os.environ restored after."""
    monkeypatch.setenv("NOTE_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOTE_STUDIO_OLD_APP_HOME", str(tmp_path / "old_app"))
    monkeypatch.setenv("NOTE_STUDIO_SHORTNOTES_ORIGINAL", str(tmp_path / "shortnotes_original"))
    monkeypatch.setenv("NOTE_STUDIO_UPSC_ORIGINAL", str(tmp_path / "upsc_original"))
    saved = dict(os.environ)
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from note_studio import app as app_module
    app_module._prepare()
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def old_app_folder(tmp_path):
    """A stand-in for ~/.shortnotes_bridge with 3 sheets and 1 saved sign-in."""
    folder = tmp_path / "old_app"
    (folder / "tokens").mkdir(parents=True)
    (folder / "pending").mkdir()
    (folder / "sheets.json").write_text(
        '{"profiles": [{"name": "A", "spreadsheetId": "1a"}, {"name": "B", "spreadsheetId": "1b"},'
        ' {"name": "C", "spreadsheetId": "1c", "allTabs": true}]}', encoding="utf-8")
    (folder / "tokens" / "abc.json").write_text('{"token": "SECRET-TOKEN-VALUE"}', encoding="utf-8")
    (folder / "last_account.json").write_text('{"email": "someone@pw.live"}', encoding="utf-8")
    return folder


@pytest.fixture(scope="session")
def lecture_pdf(tmp_path_factory) -> bytes:
    """A small lecture PDF made by the engine copy's own fixture script."""
    out = tmp_path_factory.mktemp("lecture")
    subprocess.run([sys.executable, "-c",
                    "import sys; from pathlib import Path; from scripts.make_sequence_smoke_fixtures import "
                    "build_fixtures; build_fixtures(Path(sys.argv[1]))", str(out)],
                   cwd=ROOT / "engines" / "shortnotes", check=True, timeout=120,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return (out / "text-order.pdf").read_bytes()


def load_fake():
    spec = importlib.util.spec_from_file_location("fake_proxy_inproc", ROOT / "tests" / "parity" / "fake_proxy.py")
    fake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake)
    return fake


@pytest.fixture
def fake_model(monkeypatch):
    """The PW proxy call replaced by tests/parity/fake_proxy.py; signed in as nobody."""
    from note_studio import engine_catalog, job_queue, make_service

    engine_catalog.put_shortnotes_on_path()
    import pw_access
    from sheets_bridge import pw_transport

    fake = load_fake()
    monkeypatch.setattr(pw_access, "gemini_generate", fake.gemini_generate)
    monkeypatch.setattr(make_service, "ensure_proxy", lambda model="", fallbacks=():
                        pw_transport.install(lambda: "test-token", model=model, fallbacks=fallbacks))
    yield fake
    job_queue.wait_idle(120)
