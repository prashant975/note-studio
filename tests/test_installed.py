"""Phase 9: an installed NoteCraft (QBG One's layout, plus one library folder per engine)
and the build's own checks. Nothing is built or installed here."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from note_studio import engine_host, old_app, paths

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import build_installer  # noqa: E402
import build_version  # noqa: E402

REGISTRATION = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "project_id": "notes",
                              "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                              "token_uri": "https://oauth2.googleapis.com/token",
                              "client_secret": "not-a-real-secret", "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """_internal/app (the project) beside _internal/libs/<engine>."""
    app = tmp_path / "_internal" / "app"
    app.mkdir(parents=True)
    (app / "engine_boot.py").write_bytes((ROOT / "packaging" / "engine_boot.py").read_bytes())
    for name in ("notecraft", "upsc"):
        (tmp_path / "_internal" / "libs" / name).mkdir(parents=True)
    monkeypatch.setattr(paths, "PROJECT_ROOT", app)
    monkeypatch.delenv("NOTE_STUDIO_UPSC_PYTHON", raising=False)
    return tmp_path


def test_an_installed_engine_starts_on_its_own_libraries(installed):
    assert paths.libs_dir() == installed / "_internal" / "libs"
    argv = engine_host.command("upsc", Path("engines/upsc/engine_main.py"), "--request", "r.json")
    assert argv[1].endswith("engine_boot.py") and argv[2] == "upsc" and argv[-2:] == ["--request", "r.json"]
    assert "own libraries" in engine_host.runtime("upsc")[1]


def test_a_project_folder_copy_runs_engines_as_before(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTE_STUDIO_UPSC_PYTHON", sys.executable)
    assert paths.libs_dir() is None
    assert engine_host.command("upsc", Path("main.py"), "-x") == [sys.executable, "main.py", "-x"]


def test_engine_boot_puts_the_engine_libraries_first(tmp_path):
    libs = tmp_path / "_internal" / "libs" / "upsc"
    libs.mkdir(parents=True)
    (libs / "only_in_upsc.py").write_text("VALUE = 7\n", encoding="utf-8")
    boot = tmp_path / "_internal" / "app" / "engine_boot.py"
    boot.parent.mkdir(parents=True)
    boot.write_bytes((ROOT / "packaging" / "engine_boot.py").read_bytes())
    script = tmp_path / "engine_main.py"
    script.write_text("import only_in_upsc, sys; print(only_in_upsc.VALUE, sys.argv[1:])\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(boot), "upsc", str(script), "--request", "r.json"],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "7 ['--request', 'r.json']", out.stderr
    probe = subprocess.run([sys.executable, str(boot), "upsc", "--probe", "only_in_upsc", "not_there_at_all"],
                           capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert probe.stdout.strip().startswith("MISSING|not_there_at_all (")


def test_the_check_setup_probe_reads_both_ways(monkeypatch):
    monkeypatch.setenv("NOTE_STUDIO_UPSC_PYTHON", sys.executable)
    assert engine_host.probe("upsc", ["json", "certainly_not_a_module"]) == ["certainly_not_a_module"]
    assert engine_host.probe("upsc", ["json"]) == []


def test_an_installed_copy_signs_in_with_the_registration_it_carries(installed, monkeypatch, tmp_path):
    monkeypatch.setenv("NOTE_STUDIO_SHORTNOTES_ORIGINAL", str(tmp_path / "nowhere"))
    assert "SHEETS_BRIDGE_CLIENT_SECRET" not in old_app.environment("no")
    paths.signin_registration().parent.mkdir(parents=True)
    paths.signin_registration().write_text(json.dumps(REGISTRATION), encoding="utf-8")
    assert old_app.environment("no")["SHEETS_BRIDGE_CLIENT_SECRET"] == str(paths.signin_registration())


def test_the_build_carries_a_registration_and_never_a_token(tmp_path):
    good = tmp_path / "client_secret.json"
    good.write_text(json.dumps(REGISTRATION), encoding="utf-8")
    assert build_installer.registration_only(good) == REGISTRATION
    for bad in ({"installed": {**REGISTRATION["installed"], "refresh_token": "1//0abc"}},
                {"web": REGISTRATION["installed"]}, {"type": "service_account", "private_key": "x"},
                {"installed": {**REGISTRATION["installed"], "client_id": "someone@example.com"}}):
        good.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            build_installer.registration_only(good)


def test_a_package_carries_only_the_shared_values_it_was_built_with(tmp_path):
    from note_studio import updater

    app = tmp_path / "_internal" / "app"
    (app / "note_studio").mkdir(parents=True)
    (tmp_path / "_internal" / "libs").mkdir()
    (app / "note_studio" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (app / "deployment").mkdir()
    (app / build_installer.TEAM_KEY).write_text(json.dumps({"type": "service_account", "private_key": "-----BEGIN "
                                                "PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
                                                "client_email": "a@b.iam.gserviceaccount.com"}), encoding="utf-8")
    build_installer.verify_clean(tmp_path, registration=False, team_key=True)
    with pytest.raises(SystemExit, match="a team key in a build without one"):
        build_installer.verify_clean(tmp_path, registration=False, team_key=False)
    (app / build_installer.TEAM_KEY).write_text(json.dumps({"refresh_token": "1//0abc"}), encoding="utf-8")
    with pytest.raises(ValueError):
        build_installer.verify_clean(tmp_path, registration=False, team_key=True)
    (app / build_installer.TEAM_KEY).unlink()
    (app / "note_studio" / "leak.py").write_text("KEY = 'AIza" + "x" * 35 + "'\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Google API key"):
        build_installer.verify_clean(tmp_path, registration=False)
    assert updater.paths.team_key_files()[1].name == "service_account.json"     # where an install keeps it


def test_the_build_leaves_out_tests_runs_and_sign_ins():
    for rel in ("tests/test_x.py", "engines/upsc/runs/run_1/notes.docx", "engines/upsc/.env", "token.json",
                "credentials.json", "client_secret.json", "sheets.json", "note_studio.db", "x/__pycache__/a.pyc",
                ".venv/Lib/site.py", "studio.log"):
        assert build_installer.skipped(rel), rel
    for rel in ("note_studio/app.py", "engines/upsc/src/tokenizer.py", "engines/upsc/data/DPP_Master.xlsx",
                "engines/upsc/third_party/ffmpeg/ffmpeg.exe", "engines/pw_books/src/pw_auth.py",
                "engines/upsc/reference_documents/notes_qc_framework.md"):
        assert not build_installer.skipped(rel), rel


def test_public_playback_identifier_exception_does_not_admit_private_keys(tmp_path):
    app = tmp_path / '_internal' / 'app'
    (tmp_path / '_internal' / 'libs').mkdir(parents=True)
    target = app / 'engines/upsc/src/video_sources/google_drive.py'
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / 'engines/upsc/src/video_sources/google_drive.py').read_bytes())
    build_installer.verify_clean(tmp_path, registration=False)
    with target.open('a', encoding='utf-8') as handle:
        handle.write("\nPRIVATE_PROVIDER_KEY = 'AIza" + "x" * 35 + "'\n")
    with pytest.raises(SystemExit, match='Google API key'):
        build_installer.verify_clean(tmp_path, registration=False)
    other = app / 'engines/upsc/src/another.py'
    target.rename(other)
    with pytest.raises(SystemExit, match='Google API key'):
        build_installer.verify_clean(tmp_path, registration=False)


def test_the_build_finds_imports_that_are_not_optional(tmp_path):
    code = tmp_path / "mod.py"
    code.write_text("import json\nimport fastapi\nfrom google.auth import x\nfrom . import sibling\n"
                    "try:\n    import maybe_there\nexcept ImportError:\n    pass\n"
                    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import only_for_types\n"
                    "def f():\n    import docx\n", encoding="utf-8")
    assert build_installer.third_party_imports([code], {"mod"}) == ["docx", "fastapi", "google.auth"]


def test_the_installer_keeps_qbgs_upgrade_rules():
    script = (ROOT / "packaging" / "notecraft_setup.iss").read_text(encoding="utf-8")
    build_installer.verify_upgrade_rules(script)
    assert "{localappdata}\\Programs\\{#AppName}" in script and "PrivilegesRequired=lowest" in script
    with pytest.raises(ValueError):
        build_installer.verify_upgrade_rules(script.replace(build_installer.APP_ID, "AppId={{0000}"))
    with pytest.raises(ValueError):
        build_installer.verify_upgrade_rules(script + "\n[InstallDelete]\nType: files; Name: \"{app}\\x\"\n")


def test_the_launcher_waits_for_the_ready_line_the_server_prints():
    launcher = (ROOT / "packaging" / "desktop_launcher.cs").read_text(encoding="utf-8")
    launch = (ROOT / "note_studio" / "launch.py").read_text(encoding="utf-8")
    assert f'ReadyPrefix = "{build_installer.READY}"' in launcher and f'"{build_installer.READY}" + url' in launch
    assert "start_installed.py" in launcher and "__APP_NAME__" in launcher


def test_versions_count_the_builds_of_a_day(tmp_path):
    release = tmp_path / "release.json"
    first = build_version.next_release(release)
    build_version.write_release(release, first)
    second = build_version.next_release(release)
    assert first["version"].rsplit(".", 1)[1] == "1" and second["version"].rsplit(".", 1)[1] == "2"
    assert json.loads(release.read_text(encoding="utf-8"))["version"] == first["version"]


def test_a_mac_build_counts_on_from_the_project_version_and_keeps_its_release_folder(tmp_path):
    import build_macos

    release = tmp_path / "release.json"
    prefix = build_version.next_release(release)["version"].rsplit(".", 1)[0]
    build_version.write_release(release, {"version": prefix + ".3", "updated_at": "x", "releases": "1TeamFolder"})
    assert build_macos.mac_release(release) | {"updated_at": "x"} == {
        "version": prefix + ".4", "updated_at": "x", "releases": "1TeamFolder"}
    build_version.write_release(release, {"version": "2000.1.1.9", "updated_at": "x"})
    assert build_macos.mac_release(release)["version"] == prefix + ".1"
    assert "releases" not in build_macos.mac_release(release)


def test_the_mac_app_is_told_apart_by_its_own_runtime(tmp_path):
    from note_studio import version

    resources = tmp_path / "NoteCraft.app" / "Contents" / "Resources"
    assert version.mac_app(str(resources / "runtime" / "bin" / "python3.13"))
    assert not version.mac_app(str(resources / "engine-libs" / "upsc" / "python"))
    assert not version.mac_app(str(tmp_path / "note-studio" / ".venv" / "bin" / "python"))
