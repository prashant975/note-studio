"""Old builds must not fill the disk: three staged packages kept with --keep-package were
6.8 GB on 27 October 2026. Nothing is built here."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import build_installer  # noqa: E402


def make(folder, name, age, finished=True, staged=True):
    work = folder / name
    work.mkdir(parents=True)
    if staged:
        for part in ("package", "verification-abc12345"):
            (work / part).mkdir()
            (work / part / "python.exe").write_bytes(b"MZ")
    if finished:
        (work / "verification.json").write_text("{}", encoding="utf-8")
    os.utime(work, (1_000_000 - age, 1_000_000 - age))
    return work


def prune(tmp_path, monkeypatch, keep, current=None):
    monkeypatch.setattr(build_installer, "DIST", tmp_path / "dist" / "team")
    monkeypatch.setattr(build_installer, "WORK_ROOT", tmp_path / "_build")
    monkeypatch.setattr(build_installer, "say", lambda *a: None)
    build_installer.prune_old_builds(keep, current=current)


def test_prune_keeps_the_newest_builds_and_only_the_current_package(tmp_path, monkeypatch):
    dist = tmp_path / "dist" / "team"
    releases = [make(dist, "2026.10.%d.1" % n, 30 - n, staged=False) for n in range(20, 25)]
    work = tmp_path / "_build"
    current = make(work, "2026.10.27.1-hv0lxkzl", 0)
    finished = make(work, "2026.10.26.1-eij0_77i", 1)
    unfinished = make(work, "2026.10.25.1-3pfmrofv", 2, finished=False)
    oldest = make(work, "2026.10.24.1-i334gkyb", 3)

    prune(tmp_path, monkeypatch, 3, current=current)

    assert [d.exists() for d in releases] == [False, False, True, True, True]
    assert (current / "package").is_dir() and (current / "verification-abc12345").is_dir()
    assert not (finished / "package").exists() and not (finished / "verification-abc12345").exists()
    assert (finished / "verification.json").is_file()
    assert (unfinished / "package").is_dir()          # --resume-work still needs it
    assert not oldest.exists()


def test_keep_1000_still_removes_finished_packages(tmp_path, monkeypatch):
    work = tmp_path / "_build"
    current = make(work, "2026.10.27.1-hv0lxkzl", 0)
    older = [make(work, "2026.10.%d.1-abcdefgh" % n, 30 - n) for n in range(20, 26)]

    prune(tmp_path, monkeypatch, 1000, current=current)

    assert (current / "package").is_dir()
    for build in older:
        assert (build / "verification.json").is_file()
        assert not (build / "package").exists()


def test_prune_never_touches_other_build_folders(tmp_path, monkeypatch):
    """_build also holds install backups (databases), sync backups and gate snapshots."""
    work = tmp_path / "_build"
    current = make(work, "2026.10.27.1-hv0lxkzl", 0)
    others = []
    for age, name in enumerate(("engine_sync_backups", "install_update_20261026", "upsc-sync-test-temp"), 5):
        other = work / name
        other.mkdir(parents=True)
        (other / "installed_before.db").write_bytes(b"SQLite format 3\x00")
        os.utime(other, (1_000_000 - age, 1_000_000 - age))
        others.append(other)

    prune(tmp_path, monkeypatch, 1, current=current)

    assert all((other / "installed_before.db").is_file() for other in others)


def test_versioned_installer_is_a_link_not_a_second_copy(tmp_path):
    installer = tmp_path / "NoteCraft_Setup.exe"
    installer.write_bytes(b"MZ" + b"\x00" * 1000)
    versioned = tmp_path / "NoteCraft_Setup_2026.10.27.1.exe"

    build_installer.link_or_copy(installer, versioned)

    assert versioned.read_bytes() == installer.read_bytes()
    assert os.stat(versioned).st_nlink == 2
