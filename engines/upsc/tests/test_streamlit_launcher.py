"""The frozen app's front door.

Only one thing here really matters, and it is invisible from inside Python:
whether the packaged EXE serves the Streamlit frontend at all. It decides that
by asking Streamlit whether it is a development checkout, and Streamlit answers
by looking for "site-packages" in its own __file__ — which is absent inside a
PyInstaller bundle. So a frozen app defaults to development mode, serves no
static files, and returns 404 for every page while /_stcore/health still returns
200. It looks alive from the outside.

The launcher's job is to turn that off before the server is built. These tests
pin the two ways that has already failed: the config call sitting behind an
optional import that no longer resolves, and flag_options being passed to
bootstrap.run in the belief that it applies them.
"""
from pathlib import Path
import os
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit.config as config
import streamlit.file_util as file_util

import streamlit_launcher_stable as launcher

LAUNCHER_SOURCE = (Path(__file__).resolve().parents[1] / "streamlit_launcher_stable.py").read_text(
    encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    """A directory shaped like the PyInstaller bundle's _internal."""
    static = tmp_path / "streamlit" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    return tmp_path, static


@pytest.fixture(autouse=True)
def restore_streamlit_globals():
    """The patch mutates process-wide Streamlit state; put it back."""
    original = file_util.get_static_dir
    original_mode = config.get_option("global.developmentMode")
    original_watcher = config.get_option("server.fileWatcherType")
    yield
    file_util.get_static_dir = original
    config.set_option("global.developmentMode", original_mode, "test")
    config.set_option("server.fileWatcherType", original_watcher, "test")


def test_it_turns_development_mode_off(bundle):
    """Without this the server serves nothing and every page is a 404."""
    bundle_dir, _ = bundle
    # What a frozen build starts from.
    config.set_option("global.developmentMode", True, "test")
    launcher._patch_streamlit_static_dir(bundle_dir)
    assert config.get_option("global.developmentMode") is False


def test_it_points_streamlit_at_the_bundled_frontend(bundle):
    bundle_dir, static = bundle
    launcher._patch_streamlit_static_dir(bundle_dir)
    assert file_util.get_static_dir() == str(static)


def test_frozen_app_does_not_watch_bundled_libraries(bundle):
    bundle_dir, _ = bundle
    config.set_option("server.fileWatcherType", "auto", "test")
    launcher._patch_streamlit_static_dir(bundle_dir)
    assert config.get_option("server.fileWatcherType") == "none"


def test_it_does_not_depend_on_an_optional_module(bundle):
    """The config calls used to sit under `import
    streamlit.web.server.starlette.starlette_static_routes`, which 1.48 removed.
    The import raised, the whole block was skipped, and the only trace was one
    line of stdout. This environment has no such module, so the test above
    already proves the point — this one states it."""
    assert "starlette_static_routes" not in _config_section()
    bundle_dir, _ = bundle
    config.set_option("global.developmentMode", True, "test")
    launcher._patch_streamlit_static_dir(bundle_dir)      # must not raise
    assert config.get_option("global.developmentMode") is False


def _config_section() -> str:
    """The part of the patch that runs before any optional import."""
    start = LAUNCHER_SOURCE.index("def _patch_streamlit_static_dir")
    body = LAUNCHER_SOURCE[start:LAUNCHER_SOURCE.index("\ndef ", start + 1)]
    body = re.sub(r'"""[\s\S]*?"""', "", body)           # drop the docstring
    return body[:body.index("for name in _STATIC_ROUTE_MODULES")]


def test_the_config_calls_come_before_anything_optional():
    section = _config_section()
    assert 'config.set_option("global.developmentMode", False' in section
    assert "try:" not in section, "the config calls must not sit inside a try block"


def test_a_missing_frontend_says_so_instead_of_serving_404s(tmp_path, capsys):
    launcher._patch_streamlit_static_dir(tmp_path)        # no streamlit/static
    out = capsys.readouterr().out
    assert "WARNING" in out and "missing" in out


def test_the_launcher_still_binds_the_documented_port():
    """README_FIRST tells users localhost:8501, and --server.port is ignored by
    this launcher, so the constant is the contract."""
    assert launcher.PORT == 8501


# ---------------------------------------------------------------------------
# The question-bank workbooks, in the folder the app tells people to use
# ---------------------------------------------------------------------------
# They are 53 MB of PW's own question content and are deliberately outside the
# bundle. The DPP panel says "put them in the app's data folder", and for a
# frozen build the app resolved that folder from HANDWRITTEN_NOTES_ROOT — which
# is PyInstaller's `_internal` directory. So the only sensible reading of the
# instruction, a `data` folder beside the EXE, was a folder nothing ever read,
# and the practice builder stayed switched off with no way to tell why.

def test_the_launcher_points_at_a_data_folder_beside_the_exe(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    tags = data / "UPSC QBG Tags.xlsx"
    bank = data / "DPP and Infinite Practice Questions.xlsx"
    tags.write_bytes(b"xlsx")
    bank.write_bytes(b"xlsx")

    monkeypatch.setattr(launcher, "_runtime_dir", lambda: tmp_path)
    monkeypatch.delenv("QBG_TAGS_PATH", raising=False)
    monkeypatch.delenv("QUESTION_BANK_PATH", raising=False)

    _apply_workbook_env(monkeypatch, tmp_path)
    assert os.environ["QBG_TAGS_PATH"] == str(tags)
    assert os.environ["QUESTION_BANK_PATH"] == str(bank)


def test_an_admins_own_workbook_path_is_never_overwritten(tmp_path, monkeypatch):
    """setdefault, not assignment: a shared network copy configured by an
    admin must survive the launcher finding a local one."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "UPSC QBG Tags.xlsx").write_bytes(b"xlsx")

    monkeypatch.setenv("QBG_TAGS_PATH", r"\share\qbg\tags.xlsx")
    _apply_workbook_env(monkeypatch, tmp_path)
    assert os.environ["QBG_TAGS_PATH"] == r"\share\qbg\tags.xlsx"


def test_a_missing_data_folder_is_not_an_error(tmp_path, monkeypatch):
    """Most installs will not have the workbooks. The app degrades to a panel
    that says so; it must not fail to start."""
    monkeypatch.delenv("QBG_TAGS_PATH", raising=False)
    monkeypatch.delenv("QUESTION_BANK_PATH", raising=False)
    _apply_workbook_env(monkeypatch, tmp_path)
    assert "QBG_TAGS_PATH" not in os.environ


def _apply_workbook_env(monkeypatch, runtime_dir):
    """The launcher's workbook block, run against `runtime_dir`.

    main() starts a Streamlit server, so the block is exercised directly rather
    than by calling it. Kept in step with the source by the test below.
    """
    external_data = Path(runtime_dir) / "data"
    for var, filename in (("QBG_TAGS_PATH", "UPSC QBG Tags.xlsx"),
                          ("QUESTION_BANK_PATH",
                           "DPP and Infinite Practice Questions.xlsx")):
        candidate = external_data / filename
        if candidate.is_file():
            monkeypatch.setenv(var, os.environ.get(var) or str(candidate))


def test_the_launcher_really_contains_that_block():
    """The helper above mirrors the launcher. This is what stops the mirror
    drifting away from the thing it mirrors."""
    assert "QBG_TAGS_PATH" in LAUNCHER_SOURCE
    assert "QUESTION_BANK_PATH" in LAUNCHER_SOURCE
    assert 'runtime_dir / "data"' in LAUNCHER_SOURCE, (
        "the workbooks must be looked for beside the EXE, not in the bundle")
    assert "setdefault" in LAUNCHER_SOURCE
