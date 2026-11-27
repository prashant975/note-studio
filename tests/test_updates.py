"""Phase 9: the Update button beside Smart Queue (the user's request, 4 Oct 2026),
the updater copied from QBG One's update workflow (a fake Drive folder: no
network), and the What's new tour."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from note_studio import db, updater, whats_new

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = b"MZ" + b"\0" * 2048


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    db.init()
    # A build writes the release folder into static/release.json; these tests start without one.
    monkeypatch.setattr(updater, "release_info", lambda: {"version": updater.VERSION})


class FakeReply:
    def __init__(self, payload=None, content=b"", status_code=200, headers=None, text=""):
        self.payload, self.content, self.status_code = payload, content, status_code
        self.headers, self.text = headers or {}, text

    def json(self):
        return self.payload

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]


class FakeDrive:
    """A release folder with two installers (the newest maybe not yet checked by Google) and notes."""

    def __init__(self, newest="2099.1.1.1", sha=None, status_code=200):
        self.status_code = status_code
        self.sent = []
        self.files = [
            {"id": "f1", "name": f"NoteCraft_Setup_{newest}.exe", "size": str(len(INSTALLER)),
             "sha256Checksum": sha if sha is not None else hashlib.sha256(INSTALLER).hexdigest(),
             "modifiedTime": "2099-01-01T10:00:00Z", "description": ""},
            {"id": "f0", "name": "NoteCraft_Setup_2026.10.1.1.exe", "size": "10", "sha256Checksum": "ab",
             "modifiedTime": "2026-10-01T10:00:00Z", "description": "old"},
            {"id": "n1", "name": f"NoteCraft_Setup_{newest}.txt"},
            {"id": "x", "name": "something else.pdf"},
        ]

    def get(self, url, params=None, **_kw):
        if self.status_code != 200:
            return FakeReply(status_code=self.status_code)
        if url.endswith("/files"):
            return FakeReply({"files": self.files})
        if url.endswith("/n1"):
            return FakeReply(content=b"New: PW Books notes; Check notes on every run\n- Faster DPP")
        return FakeReply(content=INSTALLER)

    def post(self, url, json=None, headers=None, **_kw):
        self.sent.append(("post", url, json, headers))
        return FakeReply(headers={"Location": "https://upload.example/session"})

    def put(self, url, data=None, headers=None, **_kw):
        self.sent.append(("put", url, data.read(), headers))
        return FakeReply({"id": "new-file"}, status_code=200)


def test_versions_compare_as_numbers():
    assert updater.is_newer("2026.10.10.1", "2026.10.4.9")
    assert not updater.is_newer("2026.10.4.1", "2026.10.4.1")
    assert updater.parse_version("v1") is None and not updater.is_newer("nonsense", "2026.1.1.1")


def test_a_newer_release_puts_the_blue_button_beside_smart_queue(client):
    page = client.get("/").text
    assert re.search(r'data-update-button\s+hidden', page)                       # nothing newer: no button
    assert f"v{updater.current_version()}" in page                               # the version is always shown
    db.set_setting(updater.FOLDER_SETTING, "1AbCdEfGhIjKlMn")
    result = updater.check(FakeDrive())
    assert result["available"] and result["latest"]["version"] == "2099.1.1.1"
    assert result["latest"]["notes"].startswith("New: PW Books notes")
    page = client.get("/runs").text
    nav = page.split('id="globalnav"')[1].split("</nav>")[0]
    smart = nav.index("Smart Queue")
    button = nav.index("data-update-button")
    assert smart < button and 'class="gn-update"' in nav and "Update to 2099.1.1.1" in nav
    assert not re.search(r'data-update-button\s+hidden', nav)
    status = client.get("/update/status").json()
    assert status["available"] and status["notes"] == ["New: PW Books notes", "Check notes on every run", "Faster DPP"]
    css = ROOT.joinpath("note_studio", "static", "style.css").read_text(encoding="utf-8")
    assert "#0a7cff" in css and "animation: updatePulse" in css                 # blue, and always pulsing


def test_the_page_follows_qbgs_update_flow():
    """release.js as QBG One's: the pop-up once per version ("Later" remembered),
    the button starts the update, progress while it runs, a reload when the new
    version answers."""
    script = ROOT.joinpath("note_studio", "static", "release.js").read_text(encoding="utf-8")
    for piece in ('localStorage.setItem(DISMISSED', '"/update/install"', '"/static/release.json"',
                  'location.reload()', '"Downloading "', 'button.addEventListener("click", startUpdate)'):
        assert piece in script, piece


def test_an_installer_google_has_not_checked_is_not_offered():
    db.set_setting(updater.FOLDER_SETTING, "1AbCdEfGhIjKlMn")
    result = updater.check(FakeDrive(sha=""))
    assert result["latest"]["version"] == "2026.10.1.1" and not result["available"]
    assert "not finished checking" in result["error"]


def test_without_a_folder_or_sign_in_the_check_says_so(client):
    assert "No release folder" in updater.check()["error"]
    db.set_setting(updater.FOLDER_SETTING, "1AbCdEfGhIjKlMn")
    assert "Sign in" in updater.check()["error"]
    page = client.get("/settings/system").text
    assert "Updates" in page and "Check for updates" in page and "Sign in" in page
    assert "Running from the project folder" in page


def test_a_folder_not_shared_with_you_is_said_in_plain_words():
    db.set_setting(updater.FOLDER_SETTING, "1AbCdEfGhIjKlMn")
    error = updater.check(FakeDrive(status_code=404))["error"]
    assert "HTTP 404" in error and "share the folder with your Google account" in error


def test_errors_never_show_a_token():
    assert updater.redact("401 for ya29.a0AfB_byCdEf123 and Bearer abcdefghijklmnopqrstuvwxyz0123") == "401 for *** and ***"


def test_the_download_must_match_googles_checksum(tmp_path):
    entry = {"version": "2099.1.1.1", "name": "NoteCraft_Setup_2099.1.1.1.exe", "file_id": "f1",
             "size": len(INSTALLER), "sha256": hashlib.sha256(INSTALLER).hexdigest()}
    assert updater.download(entry, tmp_path / "a.exe", FakeDrive()) == len(INSTALLER)
    assert (tmp_path / "a.exe").read_bytes() == INSTALLER
    with pytest.raises(ValueError, match="does not match"):
        updater.download({**entry, "sha256": "0" * 64}, tmp_path / "b.exe", FakeDrive())
    with pytest.raises(ValueError, match="incomplete"):
        updater.download({**entry, "size": len(INSTALLER) + 1}, tmp_path / "c.exe", FakeDrive())
    assert not (tmp_path / "b.exe").exists() and not (tmp_path / "c.exe").exists()


def test_install_starts_a_helper_that_outlives_notecraft(tmp_path, monkeypatch):
    root = tmp_path / "Programs" / "NoteCraft"
    root.mkdir(parents=True)
    (root / "NoteCraft.exe").write_bytes(b"MZ")
    monkeypatch.setattr(updater, "installed_root", lambda executable=None: root)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    started = []
    entry = {"version": "2099.1.1.1", "name": "NoteCraft_Setup_2099.1.1.1.exe", "file_id": "f1",
             "size": len(INSTALLER), "sha256": hashlib.sha256(INSTALLER).hexdigest()}
    log = updater.install(entry, drive=FakeDrive(), launcher=started.append)
    argv = started[0]
    assert argv[:4] == ["cmd", "/c", "start", ""] and "-Root" in argv and str(root) in argv
    helper = (tmp_path / "temp" / "notecraft_update" / "update.ps1").read_text(encoding="utf-8")
    assert helper.count("/VERYSILENT") == 1 and "'NoteCraft.exe'" in helper
    assert log.name == "install_2099.1.1.1.log"


def test_an_update_needs_a_newer_version_and_an_install(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="no newer version"):
        updater.start_update({"version": "2000.1.1.1"})
    with pytest.raises(ValueError, match="project folder"):
        updater.start_update({"version": "2099.1.1.1", "name": "NoteCraft_Setup_2099.1.1.1.exe"})
    monkeypatch.setattr(updater, "installed_root", lambda executable=None: tmp_path)
    monkeypatch.setattr(db, "active_runs", lambda: [{"state": "running"}])
    with pytest.raises(ValueError, match="Notes are being made"):               # a paid run is never cut off
        updater.start_update({"version": "2099.1.1.1", "name": "NoteCraft_Setup_2099.1.1.1.exe"})


def test_after_the_restart_the_update_says_how_it_went(tmp_path):
    from note_studio import paths

    kept = paths.home() / "update_backups" / "NoteCraft-2026.11.25.2.app" / "Contents"
    kept.mkdir(parents=True)
    other = paths.home() / "update_backups" / "notes.txt"
    other.write_text("not an app", encoding="utf-8")
    db.set_setting(updater.STATE, json.dumps({"state": "failed", "version": "2099.1.1.1", "at": 0}))
    updater.settle_state()
    assert kept.is_dir()                                       # kept while no new version has opened
    db.set_setting(updater.STATE, json.dumps({"state": "installing", "version": updater.current_version(), "at": 0}))
    assert updater.settle_state()["state"] == "done"
    assert not kept.parent.exists() and other.exists()        # the new version opened: the old app goes
    log = tmp_path / "install_2099.1.1.1.log"
    Path(str(log) + ".result").write_text("exit 5", encoding="utf-8")
    db.set_setting(updater.STATE, json.dumps({"state": "installing", "version": "2099.1.1.1", "log": str(log),
                                              "at": time.time()}))
    failed = updater.settle_state()
    assert failed["state"] == "failed" and "exit 5" in failed["error"]
    db.set_setting(updater.STATE, json.dumps({"state": "downloading", "version": "2099.1.1.1", "at": 0}))
    assert updater.settle_state()["state"] == "failed"


def test_publishing_puts_the_installer_in_the_folder_under_the_name_every_copy_looks_for(tmp_path):
    installer = tmp_path / "NoteCraft_Setup.exe"
    installer.write_bytes(INSTALLER)
    drive = FakeDrive()
    done = updater.publish(installer, "2099.1.1.1", "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp",
                           notes="PW Books; Check notes", drive=drive)
    (_, _, meta, _), (_, _, sent, _) = drive.sent
    assert meta == {"name": "NoteCraft_Setup_2099.1.1.1.exe", "parents": ["1AbCdEfGhIjKlMnOp"],
                    "description": "PW Books; Check notes"}
    assert sent == INSTALLER and done["sha256"] == hashlib.sha256(INSTALLER).hexdigest()


def test_a_build_carries_its_release_folder_and_settings_can_override_it(monkeypatch):
    monkeypatch.setattr(updater, "release_info", lambda: {"version": updater.VERSION, "releases": "1TeamFolderIdXyz"})
    assert updater.source() == {"folder_id": "1TeamFolderIdXyz", "own": False, "built_in": "1TeamFolderIdXyz"}
    updater.save_folder("https://drive.google.com/drive/folders/1MyTestFolder99?usp=drive_link")
    assert updater.source()["folder_id"] == "1MyTestFolder99"
    updater.save_folder("")                                                      # empty: back to the built-in one
    assert updater.source()["folder_id"] == "1TeamFolderIdXyz"


def test_the_release_folder_link_is_checked(client):
    bad = client.post("/settings/updates", data={"folder": "not a link"}, follow_redirects=False)
    assert "kind=warn" in bad.headers["location"]
    client.post("/settings/updates", data={"folder": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp"})
    assert updater.source() == {"folder_id": "1AbCdEfGhIjKlMnOp", "own": True, "built_in": ""}


FAKE_KEY = {"type": "service_account", "project_id": "notes", "private_key_id": "0",
            "private_key": "-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n",
            "client_email": "notecraft-updates@notes.iam.gserviceaccount.com", "client_id": "1"}


def test_the_team_key_is_checked_kept_and_never_shown(client):
    """The user's decision (4 Oct 2026): the service account, as QBG One reads its folder."""
    bad = client.post("/settings/updates/team-key", files={"key": ("me.json", b'{"refresh_token": "x"}', "application/json")},
                      follow_redirects=False)
    assert "kind=warn" in bad.headers["location"] and updater.team_key() is None
    good = client.post("/settings/updates/team-key", files={"key": ("key.json", json.dumps(FAKE_KEY).encode(), "application/json")},
                       follow_redirects=False)
    assert "kind=ok" in good.headers["location"]
    assert updater.team_account() == FAKE_KEY["client_email"]
    assert updater.team_key() == updater.paths.home() / "team_key.json"
    page = client.get("/settings/system").text
    assert FAKE_KEY["client_email"] in page and "not a real key" not in page and "Remove team key" in page
    client.post("/settings/updates/team-key/remove")
    assert updater.team_key() is None and (updater.paths.home() / "team_key.json.bak").is_file()


def test_the_folder_is_read_with_the_team_key_first(monkeypatch):
    db.set_setting(updater.FOLDER_SETTING, "1AbCdEfGhIjKlMn")
    updater.save_team_key(json.dumps(FAKE_KEY).encode())
    team, person = FakeDrive(), FakeDrive()
    monkeypatch.setattr(updater, "service_session", lambda scope=updater.READ_SCOPE: team)
    monkeypatch.setattr(updater, "person_session", lambda: person)
    assert updater.check()["available"] and updater._reader is team
    team.status_code = 404                                                       # not shared with the account
    assert updater.check()["available"] and updater._reader is person          # the person's sign-in still works
    monkeypatch.setattr(updater, "person_session", lambda: (_ for _ in ()).throw(PermissionError("Sign in first.")))
    error = updater.check()["error"]
    assert "service account (HTTP 404)" in error and FAKE_KEY["client_email"] in error and "Viewer" in error


def test_publishing_tries_the_team_account_then_the_person(monkeypatch, tmp_path):
    installer = tmp_path / "NoteCraft_Setup.exe"
    installer.write_bytes(INSTALLER)
    updater.save_team_key(json.dumps(FAKE_KEY).encode())

    class NoStorage(FakeDrive):
        def post(self, url, json=None, headers=None, **_kw):
            return FakeReply(status_code=403, text='{"error": {"reason": "storageQuotaExceeded"}}')

    person = FakeDrive()
    monkeypatch.setattr(updater, "service_session", lambda scope=updater.READ_SCOPE: NoStorage())
    monkeypatch.setattr(updater, "person_session", lambda: person)
    done = updater.publish(installer, "2099.1.1.1", "1AbCdEfGhIjKlMnOp")
    assert done["file_id"] == "new-file" and person.sent[0][2]["name"] == "NoteCraft_Setup_2099.1.1.1.exe"
    monkeypatch.setattr(updater, "person_session", lambda: (_ for _ in ()).throw(PermissionError("Sign in first.")))
    with pytest.raises(PermissionError, match="no storage of its own"):
        updater.publish(installer, "2099.1.1.1", "1AbCdEfGhIjKlMnOp")


def test_the_tour_shows_on_two_starts_at_most_and_got_it_ends_it(client, monkeypatch):
    first = client.get("/whats-new/state").json()
    assert first["show"] and first["title"] == whats_new.current()["title"]
    assert first["steps"] == whats_new.current()["steps"] and first["steps"]
    assert client.get("/whats-new/state").json()["show"]                       # same start: still showing
    monkeypatch.setattr(whats_new, "BOOT", "second-start")
    assert client.get("/whats-new/state").json()["last"] is True
    monkeypatch.setattr(whats_new, "BOOT", "third-start")
    assert client.get("/whats-new/state").json()["show"] is False
    assert client.get("/whats-new/state?again=1").json()["show"] is True       # "What's new" opens it again
    client.post("/whats-new/seen", data={"tour": first["id"], "done": "1"})
    client.post("/whats-new/seen", data={"tour": "made-up", "done": "1"})
    assert "made-up" not in db.get_setting(whats_new.STATE, "")
    assert all(step["href"].startswith("/") for step in first["steps"])


def test_every_tour_step_links_to_a_page_that_answers(client):
    for step in whats_new.current()["steps"]:
        assert client.get(step["href"]).status_code == 200, step["href"]
