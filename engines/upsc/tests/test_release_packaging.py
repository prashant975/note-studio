"""The release ZIP must contain everything the app needs to actually work.

v2.9.0 shipped without the two DPP workbooks, so every recipient saw "The QBG
tag workbook and the question bank are not on this machine" and could not
build a practice set. These tests run the real packaging script against a
fake build tree and fail the moment a release would go out hollow again.
"""
from __future__ import annotations

import importlib
import zipfile
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def packaging(tmp_path, monkeypatch):
    """The real script, pointed at a miniature build in tmp_path."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("package_release")

    build = tmp_path / "dist" / "UpscNotesApp"
    (build / "_internal" / "src").mkdir(parents=True)
    (build / "UpscNotesApp.exe").write_bytes(b"exe")
    (build / "_internal" / "base.dll").write_bytes(b"dll")
    # The build's own config, as the spec's src datas entry ships it.
    (build / "_internal" / "src" / "config.py").write_text(
        f'APP_VERSION = "{module.APP_VERSION}"' + chr(10) + 'APP_VERSION_DATE = "2026-01-01"' + chr(10),
        encoding="utf-8")
    readme = tmp_path / "dist_README_FIRST.md"
    readme.write_text("read me first", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    for name in module.DATA_FILES:
        (data / name).write_bytes(b"workbook bytes")

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "BUILD", build)
    monkeypatch.setattr(module, "README", readme)
    monkeypatch.setattr(module, "DATA", data)
    # The fake EXE is three bytes; the gate is exercised by its own tests.
    monkeypatch.setattr(module, "run_selftest", lambda build: 0)
    return module, tmp_path


def test_the_zip_ships_the_dpp_workbooks_beside_the_exe(packaging) -> None:
    module, root = packaging
    assert module.main() == 0

    zips = list((root / "dist").glob("UpscNotesApp_v*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as archive:
        names = set(archive.namelist())

    assert "README_FIRST.md" in names
    assert "UpscNotesApp/UpscNotesApp.exe" in names
    assert "UpscNotesApp/_internal/base.dll" in names
    # The regression this file exists for: the workbooks, in the data folder
    # the launcher reads, next to the EXE.
    for name in module.DATA_FILES:
        assert f"UpscNotesApp/data/{name}" in names, sorted(names)


def test_a_release_without_the_workbooks_is_refused(packaging) -> None:
    module, root = packaging
    (module.DATA / module.DATA_FILES[1]).unlink()

    assert module.main() == 1, "a hollow release was allowed to package"
    assert not list((root / "dist").glob("*.zip")), "it still wrote a ZIP"


def test_an_existing_release_zip_is_never_overwritten(packaging) -> None:
    module, root = packaging
    assert module.main() == 0
    existing = next((root / "dist").glob("UpscNotesApp_v*.zip"))
    before = existing.read_bytes()

    assert module.main() == 1
    assert existing.read_bytes() == before


def test_a_data_folder_already_in_the_build_tree_is_not_archived_twice(
    packaging,
) -> None:
    """Smoke-testing the frozen DPP panel means copying the workbooks into
    dist/UpscNotesApp/data first. Packaging after that must not write each
    53 MB workbook twice under the same archive name."""
    module, root = packaging
    build_data = module.BUILD / "data"
    build_data.mkdir()
    for name in module.DATA_FILES:
        (build_data / name).write_bytes(b"stale copy from a smoke test")

    assert module.main() == 0
    with zipfile.ZipFile(next((root / "dist").glob("*.zip"))) as archive:
        names = archive.namelist()
        for name in module.DATA_FILES:
            arcname = f"UpscNotesApp/data/{name}"
            assert names.count(arcname) == 1, names
            # And the canonical copy wins, not the smoke-test leftover.
            assert archive.read(arcname) == b"workbook bytes"


def test_a_build_made_elsewhere_can_be_packaged_with_the_build_flag(packaging) -> None:
    """A copy of the app running from dist/UpscNotesApp holds Windows file locks,
    so a rebuild has to go to another folder (PyInstaller --distpath). The
    ZIP's inner folder must still be UpscNotesApp/ — README_FIRST and the
    launcher's paths depend on that name, whatever the staging folder is."""
    module, root = packaging
    staging = root / "dist_staging" / "UpscNotesApp"
    staging.parent.mkdir()
    module.BUILD.rename(staging)
    assert not module.BUILD.exists()

    assert module.main(["--build", str(staging)]) == 0
    with zipfile.ZipFile(next((root / "dist").glob("UpscNotesApp_v*.zip"))) as archive:
        names = set(archive.namelist())
    assert "UpscNotesApp/UpscNotesApp.exe" in names
    assert "UpscNotesApp/_internal/base.dll" in names
    assert not [n for n in names if "dist_staging" in n]
    for name in module.DATA_FILES:
        assert f"UpscNotesApp/data/{name}" in names


def test_without_the_flag_the_default_build_folder_is_still_required(packaging) -> None:
    module, root = packaging
    module.BUILD.rename(root / "elsewhere")
    assert module.main() == 1
    assert not list((root / "dist").glob("*.zip"))


def test_a_build_that_fails_its_self_test_is_not_packaged(packaging, monkeypatch, capsys) -> None:
    """The gate the release exists for. v2.11.0 shipped because the only check
    was a person clicking through the page."""
    module, root = packaging
    monkeypatch.setattr(module, "run_selftest", lambda build: 1)
    assert module.main() == 1
    assert not list((root / "dist").glob("*.zip"))
    assert "fails its own self-test" in capsys.readouterr().out


def test_the_self_test_runs_against_the_build_being_packaged(packaging, monkeypatch) -> None:
    module, root = packaging
    staging = root / "dist_staging" / "UpscNotesApp"
    staging.parent.mkdir()
    module.BUILD.rename(staging)
    seen = []
    monkeypatch.setattr(module, "run_selftest", lambda build: seen.append(build) or 0)
    assert module.main(["--build", str(staging)]) == 0
    assert seen == [staging.resolve()]


def test_run_selftest_invokes_the_exe_flag_and_returns_its_exit_code(
        monkeypatch, tmp_path, capsys) -> None:
    """No `packaging` fixture here: it stubs run_selftest, and this is the test
    of the real one."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("package_release")
    calls = []

    class _Done:
        returncode = 3
        stdout = "SELF-TEST FAILED: 1 problem(s) - rapidfuzz: ModuleNotFoundError"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    build = tmp_path / "UpscNotesApp"
    assert module.run_selftest(build) == 3
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [str(build / "UpscNotesApp.exe"), "--selftest"]
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] >= 600
    assert "rapidfuzz: ModuleNotFoundError" in capsys.readouterr().out, "the report is shown"


def test_what_running_the_app_leaves_beside_the_exe_is_not_shipped(packaging) -> None:
    """Every smoke test starts the EXE from the build tree, and the spec's src
    datas entry copies the checkout's __pycache__ folders in. runs/, outputs/,
    __pycache__ and a user's .env are the machine's, not the release's;
    _internal/.env is the bundle's own and stays."""
    module, root = packaging
    build = module.BUILD
    for folder in ("runs", "outputs"):
        (build / folder / "run_x").mkdir(parents=True)
        (build / folder / "run_x" / "notes.docx").write_bytes(b"left over")
    (build / ".env").write_text("MODEL_NAME=local", encoding="utf-8")
    (build / "_internal" / ".env").write_text("bundled", encoding="utf-8")
    cache = build / "_internal" / "src" / "__pycache__"
    cache.mkdir()
    (cache / "config.cpython-313.pyc").write_bytes(b"pyc")

    assert module.main() == 0
    with zipfile.ZipFile(next((root / "dist").glob("*.zip"))) as archive:
        names = set(archive.namelist())
    assert "UpscNotesApp/_internal/src/config.py" in names
    assert "UpscNotesApp/_internal/.env" in names
    assert "UpscNotesApp/UpscNotesApp.exe" in names
    assert not [n for n in names if "/runs/" in n or "/outputs/" in n or "__pycache__" in n]
    assert "UpscNotesApp/.env" not in names


def test_a_build_whose_bundled_version_differs_from_the_checkout_is_refused(packaging, capsys) -> None:
    """--build makes it easy to zip a stale folder under a fresh number, and
    the self-test cannot notice (it checks imports, not versions)."""
    module, root = packaging
    (module.BUILD / "_internal" / "src" / "config.py").write_text(
        'APP_VERSION = "0.0.0"', encoding="utf-8")
    assert module.main() == 1
    assert not list((root / "dist").glob("*.zip"))
    out = capsys.readouterr().out
    assert "0.0.0" in out and module.APP_VERSION in out


def test_a_build_with_no_bundled_config_is_refused(packaging) -> None:
    module, root = packaging
    (module.BUILD / "_internal" / "src" / "config.py").unlink()
    assert module.main() == 1
    assert not list((root / "dist").glob("*.zip"))


def test_bundled_version_reads_the_file_without_importing_it(packaging) -> None:
    module, _ = packaging
    config = module.BUILD / "_internal" / "src" / "config.py"
    config.write_text("import os" + chr(10) + "os.environ['SELFTEST_IMPORTED'] = '1'" + chr(10)
                      + "APP_VERSION = '9.9.9'" + chr(10) + "APP_VERSION_DATE = '2026-01-01'" + chr(10),
                      encoding="utf-8")
    import os
    assert module.bundled_version(module.BUILD) == "9.9.9"
    assert "SELFTEST_IMPORTED" not in os.environ


def test_a_self_test_that_hangs_is_reported_and_blocks_packaging(monkeypatch, tmp_path, capsys) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("package_release")

    def fake_run(cmd, **kwargs):
        raise module.subprocess.TimeoutExpired(cmd, kwargs["timeout"], output="Importing 110 modules ...")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.run_selftest(tmp_path / "UpscNotesApp") == 1
    out = capsys.readouterr().out
    assert "Importing 110 modules" in out, "the partial report is the only clue to what hung"
    assert "did not finish" in out


def test_the_real_data_folder_holds_what_the_script_will_ship() -> None:
    """On the release machine the workbook names in the script must match
    real files. The workbooks are gitignored (PW's own content), so a fresh
    clone legitimately lacks them — that is a skip, not a failure; the
    packaging script itself still refuses to build a hollow release there."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("package_release")
    missing = [name for name in module.DATA_FILES
               if not (module.DATA / name).is_file()]
    if len(missing) == len(module.DATA_FILES):
        pytest.skip(
            "the gitignored QBG workbooks are not on this machine; "
            "package_release.py will refuse to build here")
    assert not missing, (
        f"the packaging script names workbooks that are not in {module.DATA}: "
        f"{missing}")
