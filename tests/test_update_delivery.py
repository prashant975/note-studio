"""Background downloads, verified cache reuse and protection of active work."""
from __future__ import annotations

import hashlib
import json
import threading

import pytest

from note_studio import db, job_queue, update_delivery as delivery, updater
from test_updates import FakeDrive, INSTALLER


@pytest.fixture(autouse=True)
def tables():
    db.init()


@pytest.fixture
def entry():
    return {"version": "2099.1.1.1", "name": "NoteCraft_Setup_2099.1.1.1.exe", "file_id": "f1", "size": len(INSTALLER),
            "sha256": hashlib.sha256(INSTALLER).hexdigest()}


def test_background_download_finishes_without_installing_and_is_reused(entry, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "install", lambda *a, **kw: calls.append("installed"))
    assert delivery.run_download(entry, drive=FakeDrive())
    db.set_setting(updater.CHECKED, json.dumps({"latest": entry}))
    assert updater.status()["download_ready"]
    assert not calls

    class NoNetwork:
        def get(self, *a, **kw):
            pytest.fail("A completed, verified download must be reused.")

    assert delivery.prepare(entry, drive=NoNetwork()).read_bytes() == INSTALLER
    # Tampering cannot pass the install-time recheck; it is replaced with a
    # fresh download and verified again before any helper can be launched.
    delivery.setup_path(entry).write_bytes(b"MZ" + b"x" * (len(INSTALLER) - 2))
    assert delivery.prepare(entry, drive=FakeDrive()).read_bytes() == INSTALLER


def test_partial_and_non_executable_downloads_never_become_ready(entry, monkeypatch):
    def broken(entry, target, **kwargs):
        target.write_bytes(b"partial")
        raise OSError("connection lost")

    monkeypatch.setattr(updater, "download", broken)
    assert not delivery.run_download(entry)
    assert updater.status()["state"]["state"] == "failed"
    assert not delivery.setup_path(entry).exists()
    assert not delivery.setup_path(entry).with_suffix(".part").exists()

    data = b"not an executable"
    bad_entry = {**entry, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(updater, "download", lambda entry, target, **kw: target.write_bytes(data))
    with pytest.raises(ValueError, match="Windows installer"):
        delivery.prepare(bad_entry)


def test_download_can_run_during_work_and_duplicate_start_is_ignored(entry, monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(updater, "_offline", lambda: False)
    monkeypatch.setattr(updater, "installed_root", lambda: tmp_path)
    monkeypatch.setattr(db, "active_runs", lambda: [{"state": "running"}])

    def download(entry, target, progress=None, **kw):
        started.set()
        assert release.wait(5)
        target.write_bytes(INSTALLER)
        progress(len(INSTALLER), len(INSTALLER))

    monkeypatch.setattr(updater, "download", download)
    try:
        assert delivery.start_download(entry)
        assert started.wait(3)
        assert not delivery.start_download(entry)
        assert not job_queue.held()
        with pytest.raises(ValueError, match="Notes are being made"):
            updater.start_update(entry)
    finally:
        release.set()
        delivery._thread.join(5)
    assert updater.status()["state"]["state"] == "ready"


def test_install_rechecks_for_work_that_started_during_download(entry, monkeypatch, tmp_path):
    running, launched = [], []
    monkeypatch.setattr(updater, "installed_root", lambda: tmp_path)
    monkeypatch.setattr(db, "active_runs", lambda: running)
    original = updater.download

    def download(*args, **kw):
        result = original(*args, **kw)
        running.append({"state": "running"})
        return result

    monkeypatch.setattr(updater, "download", download)
    with pytest.raises(ValueError, match="Notes are being made"):
        updater.install(entry, drive=FakeDrive(), launcher=launched.append)
    assert not launched
    assert delivery.verified(entry, delivery.setup_path(entry))


def test_queue_is_held_before_install_and_released_on_failure(entry, monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(updater, "installed_root", lambda: tmp_path)

    def fail(entry, **kw):
        observed.append(job_queue.held())
        assert job_queue._claim("notes") is None
        raise ValueError("installer refused")

    monkeypatch.setattr(updater, "install", fail)
    thread = updater.start_update(entry)
    thread.join(5)
    assert observed == ["update"]
    assert not job_queue.held()
    assert updater.status()["state"]["state"] == "failed"


def test_poller_downloads_without_a_browser_and_stops(entry, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_offline", lambda: False)
    monkeypatch.setattr(updater, "installed_root", lambda: tmp_path)
    monkeypatch.setattr(updater, "source", lambda: {"folder_id": "team-folder"})
    monkeypatch.setattr(updater, "check", lambda: {"available": True, "latest": entry})
    monkeypatch.setattr(updater, "_reader", FakeDrive())
    ready = threading.Event()
    original = delivery.run_download

    def run(entry):
        try:
            return original(entry)
        finally:
            ready.set()

    monkeypatch.setattr(delivery, "run_download", run)
    try:
        delivery.start_poller()
        assert ready.wait(5)
        assert updater.status()["state"]["state"] == "ready"
    finally:
        delivery.stop_poller()
        delivery._poll_thread.join(5)
        if delivery._thread:
            delivery._thread.join(5)
    assert not delivery._poll_thread.is_alive()


def test_a_running_queue_item_also_blocks_install(entry, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "installed_root", lambda: tmp_path)
    db.execute("INSERT INTO job_queue (lane,kind,title,state,payload_json,created_at) "
               "VALUES ('notes','practice','Preparing','running','{}',0)")
    with pytest.raises(ValueError, match="Smart Queue"):
        updater.start_update(entry)


def test_cache_path_rejects_unverified_or_unsafe_release_references(entry):
    for bad in ({**entry, "version": "../../evil"}, {**entry, "sha256": "no-checksum"}):
        with pytest.raises(ValueError, match="verified release"):
            delivery.setup_path(bad)


def test_install_hold_clears_after_restart_with_an_empty_queue():
    job_queue.hold('update')
    db.recover()
    assert not job_queue.held()
    job_queue.hold('stop')
    db.recover()
    assert job_queue.held() == 'stop'


def test_install_hold_becomes_restart_hold_when_work_is_waiting():
    job_queue.hold('update')
    db.execute("INSERT INTO job_queue (lane,kind,title,state,payload_json,created_at) "
               "VALUES ('notes','practice','Waiting','waiting','{}',0)")
    db.recover()
    assert job_queue.held() == 'restart'
