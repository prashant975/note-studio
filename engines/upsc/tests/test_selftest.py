"""`UpscNotesApp.exe --selftest`: the check a release has to pass before it ships.

It imports every module under src/ and builds one DPP export in each script,
inside the process that will run the app. v2.11.0 shipped without rapidfuzz
and reportlab; the page rendered and the browser smoke test passed, because
the code that needed them sits behind a button behind a login. This would
have failed in seconds.
"""
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import selftest
import streamlit_launcher_stable as launcher

ROOT = Path(__file__).resolve().parents[1]


def test_the_module_walk_covers_the_code_that_went_missing_in_v2_11_0():
    names = selftest.module_names(ROOT)
    assert "src.notes_to_dpp.matching" in names      # rapidfuzz
    assert "src.notes_to_dpp.pdf_export" in names    # reportlab, uharfbuzz
    assert "src.dmp.service" in names
    assert "src.notes_to_dpp" in names, "packages are imported too"
    assert "src" in names
    assert not [n for n in names if "__pycache__" in n or "__init__" in n]


def test_this_checkout_passes_its_own_selftest():
    lines = []
    assert selftest.run(ROOT, out=lines.append) == 0, "\n".join(lines)
    report = "\n".join(lines)
    assert "SELF-TEST PASSED" in report
    assert "English DPP export" in report and "Hindi DPP export" in report
    assert "Devanagari shaping on" in report
    assert "rapidfuzz matcher loaded" in report
    assert "DPP table layout preserved" in report
    assert "notes upload extraction: pages ['text'," in report
    assert "standalone notes PDF (en)" in report
    assert "standalone notes PDF (hi)" in report
    assert "standalone notes PDF (mr)" in report
    assert "standalone Hindi DMP PDF" in report


def test_a_module_that_cannot_import_fails_the_run_and_is_named(tmp_path, monkeypatch):
    """Three missing packages must be reported as three, not as the first."""
    package = f"selftest_probe_{uuid.uuid4().hex[:8]}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "fine.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "broken_a.py").write_text("import no_such_package_alpha\n", encoding="utf-8")
    (root / "broken_b.py").write_text("import no_such_package_beta\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    lines = []
    try:
        code = selftest.run(tmp_path, package=package, probe=None, out=lines.append)
    finally:
        for name in list(sys.modules):
            if name.startswith(package):
                del sys.modules[name]
    report = "\n".join(lines)
    assert code == 1
    assert f"FAIL  {package}.broken_a: ModuleNotFoundError" in report
    assert f"FAIL  {package}.broken_b: ModuleNotFoundError" in report
    assert "no_such_package_alpha" in report and "no_such_package_beta" in report
    assert "SELF-TEST FAILED: 2 problem(s)" in report


def test_a_deferred_package_that_cannot_import_fails_the_run(monkeypatch):
    """Most of src imports its heavy packages inside functions, so the module
    walk alone would pass a build missing all of them."""
    lines = []
    code = selftest.run(ROOT, probe=None, out=lines.append,
                        lazy_imports=("pymupdf", "no_such_deferred_package"))
    report = "\n".join(lines)
    assert code == 1
    assert "FAIL  no_such_deferred_package: ModuleNotFoundError" in report
    assert "SELF-TEST FAILED: 1 problem(s)" in report


def test_the_default_deferred_list_is_the_module_constant_read_at_call_time(monkeypatch):
    monkeypatch.setattr(selftest, "REQUIRED_LAZY_IMPORTS", ("no_such_deferred_package",))
    lines = []
    assert selftest.run(ROOT, probe=None, out=lines.append) == 1
    assert "no_such_deferred_package" in "\n".join(lines)


def test_a_failing_dpp_probe_fails_the_run_even_when_every_import_works():
    lines = []
    code = selftest.run(
        ROOT, out=lines.append,
        probe=lambda out: ["Devanagari shaping is OFF: uharfbuzz is missing"])
    assert code == 1
    assert "Devanagari shaping is OFF" in "\n".join(lines)


def test_an_empty_bundle_is_a_failure_not_a_pass(tmp_path):
    """A wrong bundle_dir must not report 'PASSED: 0 modules import'."""
    lines = []
    assert selftest.run(tmp_path, package="nothing_here", probe=None, out=lines.append) == 1
    assert "no modules found" in "\n".join(lines)


def test_the_probe_reports_shaping_off_when_the_font_is_not_shapable(monkeypatch):
    """reportlab turns shaping off silently when uharfbuzz is missing; the
    probe is the only thing that turns that into a failure."""
    from reportlab.pdfbase import pdfmetrics
    from src.notes_to_dpp import pdf_export

    real_get_font = pdfmetrics.getFont

    class _Unshaped:
        shapable = False

    monkeypatch.setattr(
        pdfmetrics, "getFont",
        lambda name: _Unshaped() if name == pdf_export._FONT_REGULAR else real_get_font(name))
    problems = selftest.dpp_export_probe(lambda line: None)
    assert any("shaping is OFF" in p for p in problems), problems


def test_the_launcher_runs_the_selftest_from_the_bundle_root(monkeypatch):
    """`UpscNotesApp.exe --selftest` must look where main() looks, and must
    never start the server."""
    calls = []
    monkeypatch.setattr(selftest, "run", lambda bundle_dir, **kw: calls.append(bundle_dir) or 7)
    monkeypatch.setattr(launcher, "main", lambda: pytest.fail("the server was started"))
    assert launcher.run_selftest() == 7
    assert calls == [launcher._bundle_dir()]


def test_the_flag_is_handled_before_main_can_touch_the_port():
    source = (ROOT / "streamlit_launcher_stable.py").read_text(encoding="utf-8")
    entry = source[source.index('if __name__ == "__main__":'):]
    assert entry.index("--selftest") < entry.index("main()"), (
        "the self-test must be dispatched before main(), which refuses a busy "
        "port and waits on input()")
