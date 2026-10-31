"""Every third-party package the app imports must be named in the PyInstaller spec.

The frozen build carries ``src/`` (and ``app.py``) as DATA files, not as
analysed modules. PyInstaller therefore never sees an import statement in
them, and a package they use ends up in the EXE only if the spec names it —
or if some other bundled package happens to depend on it. v2.11.0 shipped
with ``rapidfuzz`` and ``reportlab`` missing: the app started, login worked,
the page rendered, and the DPP tab crashed on its first click with
``ModuleNotFoundError``. (``uharfbuzz`` was present only because fontTools
imports it — luck, which the spec now replaces with a guarantee.)

These tests read the imports and the spec, so that regression fails at test
time rather than on a teammate's machine. The last one reads PyInstaller's
own record of the previous build, when there is one, and checks the same
list against what actually went into the bundle.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "UpscNotesApp.spec"
BUILD_TOC = ROOT / "build" / "UpscNotesApp" / "Analysis-00.toc"
#: The stdlib core PyInstaller zips separately. A build made with --distpath
#: (the usual move while dist/UpscNotesApp is running) lands in dist_staging,
#: so the newest copy of either is the one that goes with the TOC.
BASE_LIBRARIES = (
    ROOT / "dist" / "UpscNotesApp" / "_internal" / "base_library.zip",
    ROOT / "dist_staging" / "UpscNotesApp" / "_internal" / "base_library.zip",
)

#: The files the bundle runs but PyInstaller never analyses.
SCANNED = (
    ROOT / "app.py",
    ROOT / "pw_access.py",
    ROOT / "streamlit_launcher_stable.py",
    *sorted((ROOT / "src").rglob("*.py")),
)
LOCAL_PACKAGES = {"src", "app", "pw_access", "streamlit_launcher_stable"}
STDLIB = set(sys.stdlib_module_names)

#: Packages the build leaves out on purpose, and why. Each must be imported
#: only inside a function — a module-level import of any of these would make
#: the frozen module fail to import at all, message or no message.
OPTIONAL_BY_DESIGN = {
    "faster_whisper": "local STT, opt-in; the spec excludes it (CTranslate2 + ONNX, ~400 MB)",
    "ctranslate2": "faster_whisper's runtime, excluded with it",
    "rapidocr": "OCR for scanned PDFs; a text PDF needs none and a scan is refused with a message",
    "rapidocr_onnxruntime": "the pre-Python-3.13 name of the same OCR engine",
    "cv2": "used only on the OCR path (crops a scanned page's blank notes column)",
    "playwright": "QBG portal bridge (src/qbg_bridge), not wired into the app",
}


# ------------------------------------------------------------------ readers

def _import_nodes(tree):
    """(node, inside_a_function) for every import statement in the tree."""
    def walk(node, in_function=False):
        for child in ast.iter_child_nodes(node):
            inside = in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, inside
            yield from walk(child, inside)
    return walk(tree)


def _names(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return [node.module]
    return []


def _imports():
    """(module, file, at_module_level) for every non-local, non-stdlib import."""
    found = []
    for path in SCANNED:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, in_function in _import_nodes(tree):
            for name in _names(node):
                top = name.split(".")[0]
                if top in STDLIB or top in LOCAL_PACKAGES:
                    continue
                found.append((name, path.relative_to(ROOT).as_posix(), not in_function))
    return found


def _submodules_pulled_in_by_from_imports():
    """`from google.oauth2 import service_account` needs the MODULE
    google.oauth2.service_account, not just the package; `from docx.shared
    import Pt` needs nothing extra. Only the names that resolve to a module in
    this environment are required, so an attribute never becomes a demand."""
    found = set()
    for path in SCANNED:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, _ in _import_nodes(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module and not node.level):
                continue
            top = node.module.split(".")[0]
            if top in STDLIB or top in LOCAL_PACKAGES or top in OPTIONAL_BY_DESIGN:
                continue
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                try:
                    if importlib.util.find_spec(candidate) is not None:
                        found.add(candidate)
                except (ImportError, ValueError, AttributeError):
                    continue
    return found


def _stdlib_imports():
    """Every stdlib module the scanned files import, dotted names included."""
    found = set()
    for path in SCANNED:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, _ in _import_nodes(tree):
            found.update(n for n in _names(node) if n.split(".")[0] in STDLIB)
    return found


def _spec_manifest():
    """What the spec pulls in: collect_all() targets, hiddenimports, excludes."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))
    collected, hidden, excludes = set(), set(), set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "collect_all" and node.args
                and isinstance(node.args[0], ast.Constant)):
            collected.add(node.args[0].value)
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AugAssign):
            target = node.target
        if isinstance(target, ast.Name) and isinstance(node.value, ast.List):
            bucket = {"hiddenimports": hidden, "excludes": excludes}.get(target.id)
            if bucket is not None:
                bucket.update(e.value for e in node.value.elts
                              if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return collected, hidden, excludes


def _covered(module: str, names: set[str]) -> bool:
    """`docx.enum.text` is covered by `docx`. `google.oauth2` is not covered
    by `google` unless `google` itself is named — it is a namespace package,
    so it never is."""
    parts = module.split(".")
    return any(".".join(parts[:i]) in names for i in range(1, len(parts) + 1))


# -------------------------------------------------------------------- tests

def test_every_shipped_import_is_named_in_the_spec():
    """The regression: a package src/ needs that the spec never mentions."""
    collected, hidden, _ = _spec_manifest()
    named = collected | hidden
    missing = sorted({
        f"{module}  ({file})"
        for module, file, _ in _imports()
        if module.split(".")[0] not in OPTIONAL_BY_DESIGN
        and not _covered(module, named)
    })
    assert not missing, (
        "src/ is bundled as data, so PyInstaller cannot see these imports; "
        "add a collect_all() or hiddenimports entry to UpscNotesApp.spec for:\n  "
        + "\n  ".join(missing))


def test_the_dpp_packages_that_went_missing_are_collected_whole():
    """rapidfuzz picks its C extension with a dynamic import and uharfbuzz is
    one; reportlab needs its font data files. A bare hiddenimports name would
    bring the package's __init__ and not the parts that make it work."""
    collected, _, _ = _spec_manifest()
    for package in ("rapidfuzz", "reportlab", "uharfbuzz"):
        assert package in collected, f"{package} must be collect_all()'d, not just named"


def test_the_stdlib_modules_src_reaches_for_by_dotted_name_are_named():
    """The spec's own rule: `xml.etree.ElementTree` and friends are in the
    build today only because streamlit imports them; the spec names them so
    that stays true. Any new dotted stdlib import needs the same treatment,
    unless it is part of a package the interpreter always ships whole."""
    _, hidden, _ = _spec_manifest()
    dotted = {m for m in _stdlib_imports() if "." in m}
    # collections/__init__ aliases collections.abc to _collections_abc, so it
    # is never a separate module in any build. Everything else is named.
    always_whole = {"collections.abc"}
    unexplained = sorted(m for m in dotted - always_whole if not _covered(m, hidden))
    assert not unexplained, (
        "name these in the spec's hiddenimports (src/ is data, so nothing "
        f"else guarantees them): {unexplained}")


def test_optional_engines_are_only_ever_imported_inside_functions():
    """An optional package imported at module level is not optional: the
    module fails to import in the build that omits it, and the message the
    code meant to show never gets a chance."""
    at_module_level = sorted({
        f"{module}  ({file})"
        for module, file, top_level in _imports()
        if top_level and module.split(".")[0] in OPTIONAL_BY_DESIGN
    })
    assert not at_module_level, at_module_level


def test_the_optional_list_names_only_packages_the_code_still_imports():
    """A stale entry here would let a real dependency hide under 'optional'."""
    imported = {module.split(".")[0] for module, _, _ in _imports()}
    stale = sorted(set(OPTIONAL_BY_DESIGN) - imported)
    assert not stale, f"no longer imported anywhere; remove from OPTIONAL_BY_DESIGN: {stale}"


def test_src_is_excluded_from_the_analysis_so_only_the_data_copy_ships():
    """The launcher imports src.selftest, and PyInstaller follows imports in
    the entry script. Without the exclusion src and everything it reaches are
    compiled into the PYZ, where they silently shadow _internal/src/*.py."""
    _, _, excludes = _spec_manifest()
    assert "src" in excludes


def test_the_selftest_imports_every_package_src_only_uses_inside_functions():
    """The module walk executes module-level imports only. A package that src
    imports only inside functions (most of them) is invisible to it unless
    REQUIRED_LAZY_IMPORTS names it — so that list is derived here from the
    source and compared, instead of being trusted."""
    from src.selftest import REQUIRED_LAZY_IMPORTS

    walked = {module.split(".")[0] for module, file, top_level in _imports()
              if top_level and (file.startswith("src/") or file == "pw_access.py")}
    lazy = set(REQUIRED_LAZY_IMPORTS)
    unexercised = sorted({
        f"{module}  ({file})" for module, file, _ in _imports()
        if module.split(".")[0] not in OPTIONAL_BY_DESIGN
        and module.split(".")[0] not in walked
        and not _covered(module, lazy)
    })
    assert not unexercised, (
        "the self-test never imports these; add them to "
        "src.selftest.REQUIRED_LAZY_IMPORTS:\n  " + "\n  ".join(unexercised))


def test_the_optional_engines_the_spec_excludes_stay_excluded():
    """The exclusions are what keep ~400 MB of STT runtime out of the ZIP.
    Anyone re-adding one should have to remove it from here first."""
    _, _, excludes = _spec_manifest()
    for name in ("faster_whisper", "ctranslate2", "onnxruntime", "playwright"):
        assert name in excludes, f"{name} left the spec's excludes list"


# ---------------------------------------------------- the last real build

def _toc_entries(node):
    """Every (name, source, typecode) triple anywhere in a PyInstaller .toc.

    The file is a pprint'ed Python literal (PyInstaller reads it back with
    eval), so it is read the same way here: a regex over quotes broke on any
    path containing an apostrophe."""
    if (isinstance(node, tuple) and len(node) == 3
            and isinstance(node[0], str) and isinstance(node[2], str)):
        yield node
        return
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _toc_entries(child)


def _built_modules() -> set[str]:
    """Module names PyInstaller recorded for the last build on this machine."""
    toc = ast.literal_eval(BUILD_TOC.read_text(encoding="utf-8", errors="replace"))
    names = set()
    for name, _source, kind in _toc_entries(toc):
        if kind not in {"PYMODULE", "PYSOURCE", "EXTENSION"}:
            continue
        if kind == "EXTENSION" and ("\\" in name or "/" in name or name.endswith(".pyd")):
            # Binaries are recorded by destination path, e.g.
            # PIL\_imaging.cp313-win_amd64.pyd -> PIL._imaging
            stem = name.replace("\\", "/").split(".")[0]
            name = ".".join(part for part in stem.split("/") if part)
        names.add(name)
    libraries = [path for path in BASE_LIBRARIES if path.is_file()]
    if libraries:
        newest = max(libraries, key=lambda path: path.stat().st_mtime)
        try:
            with zipfile.ZipFile(newest) as archive:
                members = archive.namelist()
        except zipfile.BadZipFile:
            pytest.skip(f"{newest} is still being written (build in progress)")
        for member in members:
            if member.endswith(".pyc"):
                names.add(member[:-4].replace("/", ".").removesuffix(".__init__"))
    return names


def test_the_last_build_carries_src_only_as_data():
    """No src.* module may be compiled into the PYZ (see the excludes test)."""
    if not BUILD_TOC.is_file():
        pytest.skip("no PyInstaller build on this machine")
    leaked = sorted(n for n in _built_modules() if n == "src" or n.startswith("src."))
    assert not leaked, f"src modules were analysed into the bundle: {leaked}"


def test_the_last_build_on_this_machine_contains_every_module_the_app_imports():
    """Read PyInstaller's own record of what went into the bundle. This is
    the check that would have failed v2.11.0 before it was zipped; it skips
    on a machine that has never built, because there is nothing to read."""
    if not BUILD_TOC.is_file():
        pytest.skip("no PyInstaller build on this machine (build/UpscNotesApp/Analysis-00.toc)")
    built = _built_modules()
    required = {module for module, _, _ in _imports()
                if module.split(".")[0] not in OPTIONAL_BY_DESIGN}
    # Modules compiled into the interpreter (sys, math, time, itertools ...)
    # are not files and never appear in PyInstaller's record.
    required |= {m for m in _stdlib_imports()
                 if m.split(".")[0] not in sys.builtin_module_names}
    required |= _submodules_pulled_in_by_from_imports()
    # collections.abc is a one-line shim; the bundle carries _collections_abc.
    aliases = {"collections.abc": "_collections_abc"}
    # Exact names, never prefixes: PyInstaller records a namespace package such
    # as `google` even when none of its subpackages made it in, so a prefix
    # match would have passed the v2.11.0 build for google.auth as well.
    missing = sorted(m for m in required
                     if m not in built and aliases.get(m) not in built)
    assert not missing, (
        "the last PyInstaller build does not contain these modules the app "
        f"imports; rebuild after fixing UpscNotesApp.spec: {missing}")
