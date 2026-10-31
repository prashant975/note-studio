"""Package a finished PyInstaller build into the shareable release ZIP.

There was no packaging script: the layout of a release was only discoverable by
opening a previous ZIP, and getting it wrong produces an archive that unzips
into the wrong shape (an EXE with no `_internal` beside it will not start).

The layout, which every release since v2.2 has used:

    README_FIRST.md
    UpscNotesApp/
        UpscNotesApp.exe
        _internal/...

Run after `python -m PyInstaller UpscNotesApp.spec --noconfirm --clean`:

    python scripts/package_release.py

or, for a build made with `--distpath` somewhere else (the usual reason: the
copy in dist/UpscNotesApp is running and Windows will not let it be replaced):

    python scripts/package_release.py --build dist_staging/UpscNotesApp

The ZIP is named from src/config.APP_VERSION in this checkout, and packaging
is refused unless the build's own _internal/src/config.py says the same — so
a stale --build folder, or a build made before a version bump, can never ship
under a number it does not contain.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import APP_VERSION

BUILD = ROOT / "dist" / "UpscNotesApp"
README = ROOT / "dist_README_FIRST.md"

# The DPP builder is dead without these two workbooks, and the v2.9.0 release
# proved it: the ZIP shipped without them, every recipient saw "The QBG tag
# workbook and the question bank are not on this machine", and nobody could
# generate a practice set. They ship in a `data` folder NEXT TO the EXE —
# exactly where streamlit_launcher_stable points QBG_TAGS_PATH and
# QUESTION_BANK_PATH — so the app finds them with no setup step.
DATA = ROOT / "data"
DATA_FILES = (
    "UPSC QBG Tags.xlsx",
    "DPP and Infinite Practice Questions.xlsx",
)


#: Top-level folders the launcher creates NEXT TO the EXE whenever the app is
#: run from the build tree (every smoke test does that): run folders, outputs,
#: and the workbooks copied in for a local DPP check. None of it is the app.
RUNTIME_FOLDERS = frozenset({"data", "runs", "outputs"})


def shippable(path: Path, build: Path) -> bool:
    """Is this file part of the app, or something running it left behind?"""
    rel = path.relative_to(build)
    if rel.parts[0] in RUNTIME_FOLDERS:
        return False
    if "__pycache__" in rel.parts:
        # The checkout's own bytecode: the spec's ('src', 'src') datas entry
        # copies the folder tree wholesale, __pycache__ included. The frozen
        # EXE itself never writes any.
        return False
    if rel == Path(".env"):
        # A user's own configuration beside the EXE; the bundle's copy is _internal/.env.
        return False
    return True


SELFTEST_TIMEOUT = 900


def bundled_version(build: Path) -> str | None:
    """APP_VERSION as the build's own _internal/src/config.py states it.

    Read as text, never imported: importing the bundle's config would run
    its module body against this machine's environment.
    """
    config = build / "_internal" / "src" / "config.py"
    if not config.is_file():
        return None
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_VERSION ") or line.startswith("APP_VERSION="):
            _, _, value = line.partition("=")
            try:
                return str(ast.literal_eval(value.strip()))
            except (ValueError, SyntaxError):
                return None
    return None


def run_selftest(build: Path) -> int:
    """`UpscNotesApp.exe --selftest` on the build about to ship; its exit code.

    This is the gate the whole release exists to enforce: v2.11.0 went out
    with the DPP tab dead because the only check was a human clicking
    through a page. The self-test starts no server and touches no port.
    """
    try:
        completed = subprocess.run(
            [str(build / "UpscNotesApp.exe"), "--selftest"],
            capture_output=True, text=True, errors="backslashreplace",
            timeout=SELFTEST_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        # The partial report is the only clue to what hung; show it.
        for stream in (exc.stdout, exc.stderr):
            if stream:
                text = stream.decode("utf-8", "backslashreplace") if isinstance(stream, bytes) else stream
                print(text.rstrip())
        print(f"The self-test did not finish within {SELFTEST_TIMEOUT} s and was stopped.")
        return 1
    report = (completed.stdout or "").rstrip()
    if report:
        print(report)
    if completed.stderr and completed.stderr.strip():
        print(completed.stderr.rstrip())
    return completed.returncode


def _build_dir(argv) -> Path:
    parser = argparse.ArgumentParser(
        description="Package a finished PyInstaller build into the release ZIP.")
    parser.add_argument(
        "--build", type=Path, default=None,
        help="a finished PyInstaller output folder to package instead of "
             "dist/UpscNotesApp (one built with --distpath while the usual copy "
             "is running). The folder inside the ZIP is always UpscNotesApp/.")
    args = parser.parse_args(argv)
    return args.build.resolve() if args.build else BUILD


def main(argv=()) -> int:
    build = _build_dir(list(argv))
    if not (build / "UpscNotesApp.exe").is_file():
        print(f"No build at {build}. Run PyInstaller first.")
        return 1
    built = bundled_version(build)
    if built != APP_VERSION:
        print(f"Refusing to package: the build at {build} says APP_VERSION = {built!r} "
              f"but src/config.py says {APP_VERSION!r}. Rebuild after bumping the "
              f"version, or point --build at the folder that build went to.")
        return 1
    if not README.is_file():
        print(f"Missing {README}.")
        return 1
    missing_data = [name for name in DATA_FILES if not (DATA / name).is_file()]
    if missing_data:
        print("Refusing to package a release whose DPP builder cannot work.")
        for name in missing_data:
            print(f"  missing: {DATA / name}")
        return 1

    target = ROOT / "dist" / f"UpscNotesApp_v{APP_VERSION}.zip"
    if target.exists():
        print(f"{target.name} already exists — delete it first if you mean to "
              f"replace it. Overwriting a release someone may already have "
              f"downloaded is not something to do by accident.")
        return 1

    print(f"Running the build's own self-test ({build / 'UpscNotesApp.exe'} --selftest) ...")
    if run_selftest(build) != 0:
        print("\nRefusing to package a build that fails its own self-test.")
        return 1

    # The archive's UpscNotesApp/data/ folder is written from DATA_FILES
    # below, so a data folder someone dropped into the build tree (to smoke
    # test the frozen DPP panel) must not be swept up too — zipfile happily
    # writes the same 53 MB workbook twice under one name. runs/, outputs/,
    # __pycache__ and a user's .env are leftovers of running the build, and
    # the self-test above has just created some of them.
    files = sorted(p for p in build.rglob("*") if p.is_file() and shippable(p, build))
    print(f"Packaging {len(files)} files from {build} ...")

    # ZIP_DEFLATED, not stored: the build is ~500 MB and compresses to ~200 MB,
    # which is the difference between a shareable link and one that times out.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(README, "README_FIRST.md")
        for path in files:
            archive.write(path, str(Path("UpscNotesApp") / path.relative_to(build)))
        for name in DATA_FILES:
            archive.write(DATA / name, str(Path("UpscNotesApp") / "data" / name))

    size_mb = target.stat().st_size / 1e6
    print(f"\nWrote {target}")
    print(f"  {len(files) + 1 + len(DATA_FILES)} entries, {size_mb:.0f} MB")
    print(f"  including the {len(DATA_FILES)} DPP workbooks in UpscNotesApp/data/")
    print("\nThis archive embeds .streamlit/secrets.toml — the shared Google")
    print("OAuth client secret. Keep it inside the team.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
