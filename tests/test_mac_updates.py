"""The Mac app updates itself from a zip of NoteCraft.app (updater.py, packaging/build_macos.py).
Synthetic apps in a temporary folder only: nothing in Applications is touched."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from note_studio import db, update_delivery, updater, version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import build_macos  # noqa: E402

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS's ditto and sh")
NEW = "2099.1.1.1"


@pytest.fixture
def mac(monkeypatch):
    """This process as the No UPSC Mac app."""
    db.init()
    setup = version.setup_name("noupsc", mac=True)
    for name, value in {"MAC": True, "SETUP": setup, "KIND": ".zip", "MAGIC": b"PK", "EDITION": "noupsc",
                        "_NAME": updater.name_pattern(setup, ".zip"),
                        "_NOTES": updater.name_pattern(setup, ".txt")}.items():
        monkeypatch.setattr(updater, name, value)


def fake_app(folder: Path, release_version: str, edition: str = "noupsc") -> Path:
    app = folder / "NoteCraft.app"
    launcher = app / "Contents/MacOS/DesktopLauncher"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    python = app / "Contents/Resources/runtime/bin/python3.13"
    python.parent.mkdir(parents=True)
    python.write_text("")
    release = app / "Contents/Resources/source/note_studio/static/release.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"version": release_version, "edition": edition}))
    return app


def app_version(app: Path) -> str:
    return json.loads((app / "Contents/Resources/source/note_studio/static/release.json").read_text())["version"]


def zipped(app: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(target)], check=True)
    return target


class Reply:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        return self.payload


# ------------------------------------------------------------------ names and folders
def test_each_edition_and_the_mac_have_their_own_update_names():
    assert version.setup_name("full") == "NoteCraft_Setup"
    assert version.setup_name("noupsc") == "NoteCraft_Setup_NoUPSC"
    assert version.setup_name("full", mac=True) == "NoteCraft_Mac"
    assert version.setup_name("noupsc", mac=True) == "NoteCraft_Mac_NoUPSC"


def test_a_mac_app_picks_only_its_own_editions_zip(mac):
    rows = updater.parse_files([
        {"id": "w", "name": f"NoteCraft_Setup_NoUPSC_{NEW}.exe", "sha256Checksum": "a"},
        {"id": "f", "name": "NoteCraft_Mac_2099.1.1.2.zip", "sha256Checksum": "b"},
        {"id": "m", "name": f"NoteCraft_Mac_NoUPSC_{NEW}.zip", "sha256Checksum": "c", "size": "5"},
        {"id": "t", "name": f"NoteCraft_Mac_NoUPSC_{NEW}.txt"},
    ])
    assert [r["file_id"] for r in rows] == ["m"] and rows[0]["notes_file"] == "t"


def test_the_mac_app_counts_as_installed_and_the_project_folder_does_not(tmp_path):
    app = tmp_path / "NoteCraft.app"
    assert updater.installed_root(str(app / "Contents/Resources/runtime/bin/python3.13")) == app.resolve()
    assert updater.installed_root(str(tmp_path / "note-studio/.venv/bin/python")) is None


def test_a_release_folder_that_cannot_be_seen_is_said_plainly(mac, monkeypatch):
    monkeypatch.setattr(updater, "source", lambda: {"folder_id": "1MacFolderXyz", "own": False, "built_in": ""})

    class Drive:
        def __init__(self, folder_status):
            self.folder_status = folder_status

        def get(self, url, params=None, **_kw):
            return Reply({"files": []}) if url.endswith("/files") else Reply({}, self.folder_status)

    assert "not shared with your Google account" in updater.check(drive=Drive(404))["error"]
    seen = updater.check(drive=Drive(200))                        # shared, and simply empty
    assert seen["error"] == "" and seen["latest"] is None


def test_a_mac_build_without_upsc_leaves_its_engine_out(tmp_path):
    engines = build_macos.edition_release({"version": "1"}, ["upsc"])["engines"]
    assert "upsc" not in engines and "shortnotes" in engines
    assert build_macos.edition_release({"version": "1"}, ["upsc"])["edition"] == "noupsc"
    assert build_macos.packaged("engines/shortnotes/sheets_bridge/settings.py", engines)
    assert not build_macos.packaged("engines/upsc/main.py", engines)
    assert build_macos.packaged("note_studio/engines/upsc.py", engines)     # the adapter says it is absent
    assert not build_macos.packaged("engines/pw_books/tests/test_x.py", engines)
    assert not build_macos.packaged("note_studio/token.json", engines)
    assert not build_macos.packaged("note_studio/deployment/service_account.json", engines)   # only built in on purpose


# ------------------------------------------------------- one folder for Mac and Windows (as Doubt Desk)
TEAM_FOLDER_FILES = [
    {"id": "wf", "name": f"NoteCraft_Setup_{NEW}.exe", "sha256Checksum": "a"},
    {"id": "wn", "name": f"NoteCraft_Setup_NoUPSC_{NEW}.exe", "sha256Checksum": "b"},
    {"id": "wt", "name": f"NoteCraft_Setup_NoUPSC_{NEW}.txt"},
    {"id": "mn", "name": f"NoteCraft_Mac_NoUPSC_{NEW}.zip", "sha256Checksum": "c"},
    {"id": "mt", "name": f"NoteCraft_Mac_NoUPSC_{NEW}.txt"},
]


def test_mac_and_windows_updates_share_one_folder_each_taking_its_own(mac, monkeypatch):
    mac_rows = updater.parse_files(TEAM_FOLDER_FILES)
    assert [(r["file_id"], r["notes_file"]) for r in mac_rows] == [("mn", "mt")]
    windows = version.setup_name("noupsc")
    monkeypatch.setattr(updater, "_NAME", updater.name_pattern(windows, ".exe"))
    monkeypatch.setattr(updater, "_NOTES", updater.name_pattern(windows, ".txt"))
    assert [(r["file_id"], r["notes_file"]) for r in updater.parse_files(TEAM_FOLDER_FILES)] == [("wn", "wt")]


def test_the_mac_app_reads_the_team_folder_the_windows_installer_reads(tmp_path):
    project = tmp_path / "release.json"
    project.write_text('{"version": "2026.11.27.1", "releases": "1TeamFolderXyz"}')
    bundle = build_macos.edition_release(build_macos.mac_release(project), ["upsc"])
    assert bundle["releases"] == "1TeamFolderXyz" and bundle["edition"] == "noupsc"
    assert not (ROOT / "packaging" / "macos_release.json").exists()      # no separate Mac folder any more


def team_key_file(folder: Path, email: str = "bot@team.iam.gserviceaccount.com") -> Path:
    key = folder / "key.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": email, "private_key": "not-a-real-key"}))
    return key


def test_every_mac_app_carries_the_team_key_showing_only_its_email(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "team_key", lambda: None)
    key = team_key_file(tmp_path)
    email = "bot@team.iam.gserviceaccount.com"
    assert build_macos.team_key_source(key) == (key, email)
    assert build_macos.team_key_source(None, toolkit=key) == (key, email)      # where Doubt Desk's build finds it
    assert build_macos.team_key_source(key, leave_out=True) == (None, "")
    with pytest.raises(SystemExit, match="No team key was found"):
        build_macos.team_key_source(None, toolkit=tmp_path / "missing.json")
    person = tmp_path / "token.json"
    person.write_text('{"refresh_token": "a person sign-in"}')
    with pytest.raises(SystemExit, match="not a valid team key"):
        build_macos.team_key_source(person)
    (tmp_path / "settings").mkdir()
    settings_key = team_key_file(tmp_path / "settings")
    monkeypatch.setattr(updater, "team_key", lambda: settings_key)
    assert build_macos.team_key_source(None, toolkit=key)[0] == settings_key    # the key added in Settings first


def test_a_mac_app_with_the_team_key_reads_the_folder_with_it(mac, tmp_path, monkeypatch):
    key = team_key_file(tmp_path)
    monkeypatch.setattr(updater.paths, "team_key_files", lambda: [tmp_path / "none.json", key])
    assert [who for who, _make in updater._readers()] == [updater.TEAM, updater.PERSON]
    assert updater.team_account() == "bot@team.iam.gserviceaccount.com"


@pytest.mark.parametrize("error,expected", [(updater.NOT_SIGNED_IN, update_delivery.SIGNIN_WAIT),
                                            ("", update_delivery.POLL_SECONDS)])
def test_a_check_before_the_sign_in_is_back_is_tried_again_soon(monkeypatch, error, expected):
    waits = []

    class Stop:
        flag = False

        def is_set(self):
            return self.flag

        def clear(self):
            self.flag = False

        def wait(self, seconds):
            waits.append(seconds)
            self.flag = True

    monkeypatch.setattr(update_delivery, "_stop", Stop())
    monkeypatch.setattr(update_delivery, "_poll_thread", None)
    monkeypatch.setattr(updater, "_offline", lambda: False)
    monkeypatch.setattr(updater, "installed_root", lambda: Path("/Applications/NoteCraft.app"))
    monkeypatch.setattr(updater, "source", lambda: {"folder_id": "1MacFolderXyz"})
    monkeypatch.setattr(updater, "check", lambda: {"error": error})
    update_delivery.start_poller()
    update_delivery._poll_thread.join(5)
    assert waits == [expected]


# ------------------------------------------------------------------ download and install
def test_only_a_zip_with_the_reported_sha256_is_kept(mac, tmp_path):
    good = tmp_path / "u.zip"
    good.write_bytes(b"PK\x03\x04" + b"x" * 100)
    entry = {"version": NEW, "name": f"NoteCraft_Mac_NoUPSC_{NEW}.zip", "size": good.stat().st_size,
             "sha256": hashlib.sha256(good.read_bytes()).hexdigest()}
    assert update_delivery.verified(entry, good)
    assert update_delivery.setup_path(entry).suffix == ".zip"
    program = tmp_path / "u.exe"
    program.write_bytes(b"MZ" + b"x" * 102)
    assert not update_delivery.verified(dict(entry, sha256=hashlib.sha256(program.read_bytes()).hexdigest()), program)


@mac_only
def test_install_unpacks_and_checks_the_new_app_then_hands_over(mac, tmp_path, monkeypatch):
    installed = fake_app(tmp_path / "Applications", "2026.11.25.2")
    package = zipped(fake_app(tmp_path / "build", NEW), tmp_path / "downloads" / "update.zip")
    monkeypatch.setattr(updater, "current_version", lambda: "2026.11.25.2")
    monkeypatch.setattr(updater, "installed_root", lambda: installed)
    monkeypatch.setattr(update_delivery, "prepare", lambda entry, drive=None, progress=None: package)
    monkeypatch.setattr(update_delivery, "installation_problem", lambda: "")
    started = []
    entry = {"version": NEW, "name": f"NoteCraft_Mac_NoUPSC_{NEW}.zip"}
    log = updater.install(entry, launcher=started.append)
    shell, script, root, new_app, backup, log_arg = started[0]
    assert shell == "/bin/sh" and Path(script).read_text() == updater.MAC_HELPER
    assert root == str(installed) and log_arg == str(log)
    assert app_version(Path(new_app)) == NEW and not package.exists()
    assert Path(backup).name == "NoteCraft-2026.11.25.2.app" and Path(backup).parent.is_dir()


@mac_only
@pytest.mark.parametrize("release_version,edition", [("2099.1.1.2", "noupsc"), (NEW, "full")])
def test_an_update_for_another_version_or_edition_is_refused(mac, tmp_path, release_version, edition):
    package = zipped(fake_app(tmp_path / "build", release_version, edition), tmp_path / "downloads" / "u.zip")
    with pytest.raises(ValueError, match="not NoteCraft"):
        updater.unpack_mac({"version": NEW}, package)
    assert not list((tmp_path / "downloads").glob("stage-*"))


@mac_only
def test_the_helper_swaps_the_app_keeps_the_old_one_and_never_leaves_none(tmp_path):
    app = fake_app(tmp_path / "Applications", "2026.11.25.2")
    new = fake_app(tmp_path / "downloads" / f"stage-{NEW}", NEW)
    backup = tmp_path / "backups" / "NoteCraft-2026.11.25.2.app"
    backup.parent.mkdir()
    script = tmp_path / "update.sh"
    script.write_text(updater.MAC_HELPER)
    log = tmp_path / "install.log"
    result = Path(str(log) + ".result")
    run = subprocess.run(["/bin/sh", str(script), str(app), str(new), str(backup), str(log), "true"], timeout=60)
    assert run.returncode == 0 and result.read_text().strip() == "exit 0"
    assert app_version(app) == NEW and app_version(backup) == "2026.11.25.2" and not new.exists()

    # A process that only names the app (a Terminal command, an editor) is neither waited for nor closed.
    bystander = subprocess.Popen(["/bin/sh", "-c", f"sleep 30; : {app}/Contents/MacOS/DesktopLauncher"])
    try:
        new = fake_app(tmp_path / "downloads" / "stage-2099.1.1.3", "2099.1.1.3")
        run = subprocess.run(["/bin/sh", str(script), str(app), str(new), str(backup), str(log), "true"], timeout=20)
        assert run.returncode == 0 and app_version(app) == "2099.1.1.3" and bystander.poll() is None
    finally:
        bystander.kill()

    broken = tmp_path / "downloads" / "stage-2099.1.1.2" / "NoteCraft.app"    # no launcher: not an app
    broken.mkdir(parents=True)
    run = subprocess.run(["/bin/sh", str(script), str(app), str(broken), str(backup), str(log), "true"], timeout=60)
    assert run.returncode == 3 and result.read_text().strip() == "exit 3"
    assert app_version(app) == "2099.1.1.3" and app_version(backup) == NEW
