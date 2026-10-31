"""Edition boundaries: no missing-engine choices, work or cross-edition updates."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


from note_studio import categories, db, engine_catalog, guides, make_routes, make_service, note_types
from note_studio import practice_service, setup_check, update_delivery, updater, version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import build_installer  # noqa: E402


def test_a_second_smaller_release_advances_without_changing_full_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(build_installer, "DIST", tmp_path)
    monkeypatch.setattr(build_installer.build_version, "next_release", lambda: {"version": "2026.10.30.1", "updated_at": "fixed"})
    (tmp_path / "2026.10.30.1-noupsc-nopwbooks").mkdir()
    assert build_installer.next_available_release()["version"] == "2026.10.30.2"


@pytest.fixture
def no_upsc(monkeypatch, tmp_path):
    real = engine_catalog.engine_dir
    monkeypatch.setattr(engine_catalog, "engine_dir", lambda e: tmp_path / "absent" if e == "upsc" else real(e))


@pytest.fixture
def no_optional(no_upsc, monkeypatch, tmp_path):
    real = engine_catalog.engine_dir
    monkeypatch.setattr(engine_catalog, "engine_dir", lambda e: tmp_path / "absent" if e == "pw_books" else real(e))


def test_source_still_offers_every_engine(client):
    assert all(engine_catalog.available(e) for e in ("notesbot", "upsc", "pw_books", "notes_qc", "typeset"))
    assert 'href="/new?category=upsc"' in client.get("/new").text
    assert 'value="upsc_dcn"' in client.get("/new?category=upsc").text
    assert note_types.ready("upsc_dcn")


def test_new_notes_filters_engine_copies_without_changing_the_pack(client, no_upsc):
    context = make_routes._form_context("upsc", "upsc_dcn")
    assert "upsc" not in {p["id"] for p in context["packs"]}
    assert {"neet_jee", "foundation", "bsc"} <= {p["id"] for p in context["packs"]}
    assert categories.get("upsc")["enabled"] is True
    assert categories.offered_types(categories.get("upsc")) == []
    assert not note_types.ready("upsc_dcn")
    assert note_types.ready("typeset_notes")
    html = client.get("/new?category=upsc&type=upsc_dcn").text
    assert 'href="/new?category=upsc"' not in html and 'value="upsc_dcn"' not in html
    assert "Typeset notes (B.Sc. style)" in client.get("/new?category=bsc").text


def test_direct_missing_engine_request_cannot_enter_the_queue(client, no_upsc):
    with pytest.raises(make_service.FormError, match="not in this installation"):
        make_service.check("upsc", "upsc_dcn", "polity", "english", {})
    response = client.post("/new", data={"category": "upsc", "note_type": "upsc_dcn",
                                        "subject": "polity", "language": "english"})
    assert "not in this installation" in response.text
    assert db.query("SELECT * FROM job_queue") == []


def test_practice_stays_open_and_does_not_probe_the_original(client, no_upsc, monkeypatch):
    def forbidden():
        pytest.fail("Practice must not scan runs or the question bank for an absent engine")
    monkeypatch.setattr(practice_service, "sources", forbidden)
    monkeypatch.setattr(practice_service, "masters_status", forbidden)
    response = client.get("/practice")
    assert response.status_code == 200
    assert "Mains practice and DPP are not in this installation." in response.text
    assert 'action="/practice/start"' not in response.text
    assert 'href="/practice"' in client.get("/").text
    assert all("Pick a finished UPSC" not in s for s in guides.for_path("/practice")["steps"])


def test_practice_requests_cannot_start_missing_engine_work(client, no_upsc):
    response = client.post("/practice/start", data={"practice": "dmp", "kind": "notecraft", "id": "1"})
    assert "not in this installation" in response.text
    assert practice_service.step(1, "dmp_questions") == (False, "Mains practice and DPP are not in this installation.")
    assert db.query("SELECT * FROM runs") == [] and db.query("SELECT * FROM job_queue") == []


def test_setup_and_models_leave_out_missing_engine(client, no_upsc, monkeypatch):
    monkeypatch.setattr(setup_check, "_missing_modules", lambda: pytest.fail("No UPSC runtime probe"))
    monkeypatch.setattr(setup_check, "ffmpeg", lambda: pytest.fail("No ffmpeg probe"))
    checks = client.get("/settings/system/check").text
    assert "not in this installation" in checks and "ffmpeg" not in checks and "UPSC engine&#39;s Python" not in checks
    ai = client.get("/settings/ai").text
    assert 'value="upsc_dcn"' not in ai and 'value="upsc_dmp"' not in ai
    assert 'value="typeset_notes"' in ai and 'value="pw_books_handwritten"' in ai
    system = client.get("/settings/system").text
    assert "Not in this installation" in system and 'id="dpp"' not in system
    status = next(e for e in engine_catalog.all_status() if e["id"] == "upsc")
    assert status["copied"] is False


def test_pw_books_is_also_absent_when_its_copy_is_left_out(client, no_optional):
    new = client.get("/new?category=neet_jee&type=pw_books_handwritten").text
    assert 'value="pw_books_handwritten"' not in new
    assert 'value="concise_handdrawn"' in new
    ai = client.get("/settings/ai").text
    assert 'value="pw_books_handwritten"' not in ai and 'value="typeset_notes"' in ai
    assert note_types.ready("notes_qc")
    assert not any("PW Books" in s for s in guides.for_path("/new")["steps"])
    with pytest.raises(make_service.FormError, match="not in this installation"):
        make_service.check("neet_jee", "pw_books_handwritten", "physics", "english", {})
    assert client.get("/practice").status_code == 200
    system = client.get("/settings/system").text
    type_rows = system.split('id="types"', 1)[1].split('id="storage"', 1)[0]
    pw_row = next(row for row in type_rows.split("</tr>") if "PW Books handwritten notes" in row)
    assert "Not in this installation" in pw_row and "> Ready<" not in pw_row


def load_edition(monkeypatch, edition):
    monkeypatch.setattr(version, "EDITION", edition)
    spec = importlib.util.spec_from_file_location("note_studio._edition_test", updater.__file__)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize("edition,prefix,other", [
    ("full", "NoteCraft_Setup", "NoteCraft_Setup_NoUPSC"),
    ("noupsc", "NoteCraft_Setup_NoUPSC", "NoteCraft_Setup"),
    ("noupsc-nopwbooks", "NoteCraft_Setup_NoUPSC_NoPWBooks", "NoteCraft_Setup_NoUPSC"),
    ("nopwbooks", "NoteCraft_Setup_NoPWBooks", "NoteCraft_Setup"),
])
def test_shared_drive_folder_only_offers_own_installers_and_notes(monkeypatch, edition, prefix, other):
    module = load_edition(monkeypatch, edition)
    assert module.SETUP == prefix
    files = [{"id": "wrong", "name": f"{other}_2099.1.1.9.exe", "sha256Checksum": "abc"},
             {"id": "own", "name": f"{prefix}_2099.1.1.1.exe", "sha256Checksum": "def"},
             {"id": "wrong-notes", "name": f"{other}_2099.1.1.1.txt"},
             {"id": "own-notes", "name": f"{prefix}_2099.1.1.1.txt"}]
    rows = module.parse_files(files)
    assert len(rows) == 1 and rows[0]["file_id"] == "own" and rows[0]["notes_file"] == "own-notes"


@pytest.mark.parametrize("edition,other", [("noupsc", "NoteCraft_Setup"), ("full", "NoteCraft_Setup_NoUPSC"),
    ("noupsc-nopwbooks", "NoteCraft_Setup"), ("noupsc-nopwbooks", "NoteCraft_Setup_NoUPSC"),
    ("full", "NoteCraft_Setup_NoUPSC_NoPWBooks")])
def test_cached_other_edition_cannot_download_or_install(monkeypatch, edition, other):
    loaded = load_edition(monkeypatch, edition)
    for name in ("SETUP", "_NAME", "_NOTES"):
        monkeypatch.setattr(updater, name, getattr(loaded, name))
    db.init()
    entry = {"version": "2099.1.1.1", "name": f"{other}_2099.1.1.1.exe", "file_id": "wrong", "sha256": "a" * 64}
    db.set_setting(updater.CHECKED, json.dumps({"latest": entry}))
    assert updater.status()["latest"] is None and not updater.status()["available"]
    for action in (updater.start_update, updater.install, update_delivery.prepare):
        with pytest.raises(ValueError, match="different edition"):
            action(entry)
    with pytest.raises(ValueError, match="different edition"):
        updater.download(entry, Path("must-not-be-written.exe"))


def test_unnamed_update_references_are_not_accepted():
    with pytest.raises(ValueError, match="different edition"):
        updater.require_edition({"version": "2099.1.1.1", "file_id": "unknown-edition"})


@pytest.mark.parametrize("without,edition,prefix", [([], "full", "NoteCraft_Setup"),
    (["upsc"], "noupsc", "NoteCraft_Setup_NoUPSC"), (["pw_books"], "nopwbooks", "NoteCraft_Setup_NoPWBooks"),
    (["upsc", "pw_books"], "noupsc-nopwbooks", "NoteCraft_Setup_NoUPSC_NoPWBooks")])
def test_both_build_paths_stage_only_their_engines_and_keep_distinct_outputs(tmp_path, monkeypatch, without, edition, prefix):
    """Exercise the real build orchestration; replace only expensive tools and engine copies."""
    root = tmp_path / "project"
    source_release = root / "note_studio" / "static" / "release.json"
    source_release.parent.mkdir(parents=True)
    source_release.write_text('{"version":"2026.10.26.1","releases":"same-folder"}\n', encoding="utf-8")
    original_bytes = source_release.read_bytes()
    monkeypatch.setattr(build_installer, "ROOT", root)
    monkeypatch.setattr(build_installer, "WORK_ROOT", root / "_build")
    monkeypatch.setattr(build_installer, "DIST", root / "dist" / "team")
    monkeypatch.setattr(build_installer.build_version, "RELEASE_FILE", source_release)
    monkeypatch.setattr(build_installer, "find_compiler", lambda _: "fake-iscc")
    monkeypatch.setattr(build_installer, "copy_runtime", lambda dest: dest.mkdir(parents=True))
    copied = []
    def copy(source, target):
        target.mkdir(parents=True, exist_ok=True)
        copied.append(source.name)
        return 0
    monkeypatch.setattr(build_installer, "copy_tree", copy)
    monkeypatch.setattr(build_installer, "notecraft_code", lambda: ([], set()))
    monkeypatch.setattr(build_installer, "notecraft_distributions", lambda _: {})
    libraries = []
    def copy_libraries(engine, target, source=None):
        libraries.append(engine)
        target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(build_installer, "copy_engine_libraries", copy_libraries)
    monkeypatch.setattr(build_installer, "fill_declared_requirements", lambda *a: [])
    probes = []
    monkeypatch.setattr(build_installer, "verify_runtime_imports", lambda p, w, engines: probes.extend(engines))
    monkeypatch.setattr(build_installer, "verify_clean", lambda *a: None)
    monkeypatch.setattr(build_installer.make_icon, "write", lambda p: p)
    def smoke(package, work, release, client):
        assert json.loads((package / "_internal/app/note_studio/static/release.json").read_text())["edition"] == edition
        return {"mock_runs": {e: {} for e in libraries}}
    monkeypatch.setattr(build_installer, "smoke_test", smoke)
    def run(argv, **kwargs):
        if argv[0] == "fake-iscc":
            dest = Path(next(a.removeprefix("/DOutputDir=") for a in argv if str(a).startswith("/DOutputDir=")))
            script = Path(argv[-1]).read_text(encoding="utf-8")
            assert "OutputBaseFilename=" + prefix in script
            (dest / (prefix + ".exe")).write_bytes(b"MZ" + b"\0" * 10_000_000)
    monkeypatch.setattr(build_installer, "run", run)
    monkeypatch.setattr(build_installer, "prune_old_builds", lambda *a, **kw: None)
    args = ["builder", "--no-signin", "--no-team-key", "--keep-package", "--notes", "Edition check"]
    for engine in without:
        args += ["--without", engine]
    monkeypatch.setattr(sys, "argv", args)
    build_installer.main()
    expected = [e for e in build_installer.ENGINES if e not in without]
    assert copied == ["note_studio", *expected] and probes == expected
    assert libraries == [e for e in build_installer.CHILD_ENGINES if e in expected]
    destination = next(build_installer.DIST.iterdir())
    assert destination.name.endswith("-" + edition) if edition != "full" else destination.name[-1].isdigit()
    assert (destination / (prefix + ".exe")).is_file()
    assert list(destination.glob(prefix + "_*.txt"))
    record = json.loads(next(build_installer.WORK_ROOT.glob("*/verification.json")).read_text())
    assert record["release"]["engines"] == expected
    assert record["release"]["releases"] == "same-folder"
    if without:
        assert source_release.read_bytes() == original_bytes
    else:
        assert json.loads(source_release.read_text())["edition"] == "full"


def test_explicit_preserved_libraries_are_copied_without_using_an_original(tmp_path, monkeypatch):
    source = tmp_path / "preserved" / "pw_books"
    source.mkdir(parents=True)
    (source / "module.py").write_text("VALUE=1\n", encoding="utf-8")
    monkeypatch.setattr(build_installer.paths, "original", lambda _: None)
    target = tmp_path / "libs" / "pw_books"
    build_installer.copy_engine_libraries("pw_books", target, source)
    assert (target / "module.py").read_bytes() == (source / "module.py").read_bytes()
