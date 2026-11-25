"""Build an unsigned, self-contained macOS app using a relocatable uv CPython runtime.

Run with the project's .venv/bin/python. Use --dmg to also build an installer disk image.
The runtime allowlist excludes local data and credentials.

Each build gets the next version, YYYY.M.D.N, as the Windows builder does (build_version.py):
it is written into the app (shown under the account menu) and its Info.plist, and into the
project's note_studio/static/release.json once the build has passed its check.
"""
import argparse
import json
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile

import build_version
import make_icon

ROOT = Path(__file__).resolve().parents[1]
NAME = "NoteCraft"
ICON = NAME + ".icns"
ENTRY = "run_studio.py"
SOURCE_ROOTS = ["note_studio","engines","run_studio.py"]


def mac_release(source: Path = build_version.RELEASE_FILE) -> dict:
    """The next version after the project's (the same day: N + 1, else .1), keeping its release folder."""
    release = build_version.next_release(source)
    try:
        previous = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if isinstance(previous, dict) and previous.get("releases"):
        release["releases"] = previous["releases"]
    return release


def build_app_icon(target: Path) -> None:
    from PIL import Image

    artwork = make_icon.draw()
    with tempfile.TemporaryDirectory(prefix="notecraft-icon-") as folder:
        iconset = Path(folder) / (NAME + ".iconset")
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            artwork.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            artwork.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "--convert", "icns", "--output", target, iconset], check=True)


def build_disk_image(app: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="notecraft-dmg-", dir=destination.parent) as folder:
        staging = Path(folder) / "payload"
        staging.mkdir()
        subprocess.run(["ditto", app, staging / app.name], check=True)
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)

        disk_image = Path(folder) / destination.name
        subprocess.run(["hdiutil", "create", "-srcfolder", staging, "-volname", NAME,
                        "-format", "UDZO", "-quiet", disk_image], check=True)
        subprocess.run(["xcrun", "swift", "-e", SET_INSTALLER_ICON, disk_image,
                        app / "Contents/Resources" / ICON], check=True)
        disk_image.rename(destination)
    print(f"Built installer: {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", action="store_true", help="Also build dist/NoteCraft.dmg for drag-to-Applications installation.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Build macOS apps on macOS; use the existing Windows builder on Windows.")
    runtime = Path(sys.base_prefix)
    if not (runtime / "BUILD").exists() or "cpython-3.13" not in runtime.name:
        raise SystemExit("Use a relocatable uv Python 3.13 environment (uv python install 3.13; recreate .venv).")
    app = ROOT / "dist" / (NAME + ".app")
    if app.exists():
        raise SystemExit(f"{app} already exists. Move that generated app aside before rebuilding.")
    disk_image = app.with_suffix(".dmg")
    if args.dmg and disk_image.exists():
        raise SystemExit(f"{disk_image} already exists. Move it aside before rebuilding.")
    release = mac_release()
    print(f"Building {NAME} {release['version']}")
    resources = app / "Contents/Resources"
    macos = app / "Contents/MacOS"
    resources.mkdir(parents=True)
    macos.mkdir(parents=True)
    build_app_icon(resources / ICON)
    shutil.copytree(runtime, resources / "runtime", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    libraries = Path(sysconfig.get_paths()["purelib"])
    shutil.copytree(libraries, resources / "libs", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "pip", "pip-*", "pytest", "_pytest"))
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).decode().split("\0")
    for rel in files:
        if not rel or not any(rel == prefix or rel.startswith(prefix + "/") for prefix in SOURCE_ROOTS):
            continue
        path = Path(rel)
        if (set(path.parts) & {"tests", "__pycache__", "node_modules", ".venv", "runs", "output", "runtime"}
                and not rel.startswith("data/examples/")):
            continue
        if (path.name in {"token.json", "tokens.json", "credentials.json", "service_account.json", "client_secret.json"}
                or path.name.startswith(".env") and path.name != ".env.example"
                or path.suffix in {".log", ".db", ".sqlite3", ".pyc"}):
            continue
        source = ROOT / rel
        if not source.is_file():
            continue
        target = resources / "source" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    build_version.write_release(resources / "source/note_studio/static/release.json", release)
    for engine in ("upsc", "pw_books", "notes_qc"):
        site = ROOT / "engines" / engine / ".venv/lib/python3.13/site-packages"
        if not site.is_dir():
            raise SystemExit(f"Missing environment: {site}. Run scripts/setup.py first.")
        shutil.copytree(site, resources / "engine-libs" / engine, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "pip", "pip-*"))
        wrapper = macos / engine
        wrapper.write_text('#!/bin/sh\nset -eu\nBASE="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)"\n'
                           'export PYTHONPATH="$BASE/engine-libs/' + engine + '"\n'
                           'exec "$BASE/runtime/bin/python3.13" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    bootstrap = resources / "bootstrap.py"
    bootstrap.write_text(BOOTSTRAP, encoding="utf-8")
    launcher = macos / "launch"
    launcher.write_text('#!/bin/sh\nset -eu\nBASE="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)"\n'
                        'export PYTHONPATH="$BASE/libs:$BASE/source"\n'
                        'exec "$BASE/runtime/bin/python3.13" "$BASE/bootstrap.py" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    subprocess.run(["xcrun", "swiftc", "-framework", "AppKit",
                    str(ROOT/"packaging/macos_launcher.swift"),
                    "-o", str(macos/"DesktopLauncher")], check=True)
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleName": NAME, "CFBundleDisplayName": NAME,
                      "CFBundleIdentifier": "live.pw.NoteCraft", "CFBundleExecutable": "DesktopLauncher",
                      "CFBundleIconFile": ICON, "CFBundleShortVersionString": release["version"],
                      "CFBundleVersion": release["version"], "CFBundlePackageType": "APPL"}, stream)
    with tempfile.TemporaryDirectory(prefix="mac-package-check-") as folder:
        env = dict(os.environ, STUDIO_ROOT=folder, NOTE_STUDIO_HOME=folder,
                   NOTE_STUDIO_OFFLINE="1", QBG_STUDIO_DB_PATH=str(Path(folder)/"qbg.db"),
                   PW_AUTH_STATE_DIR=str(Path(folder)/"auth"), QBG_PORTAL_AUTH_PATH=str(Path(folder)/"portal.json"))
        subprocess.run([str(launcher), "--check-package"], check=True, env=env)
    print(f"Built and import-checked: {app} ({NAME} {release['version']})")
    if args.dmg:
        build_disk_image(app, disk_image)
    build_version.write_release(build_version.RELEASE_FILE, release)   # the next build counts on from this one
    print(f"Version {release['version']} written to {build_version.RELEASE_FILE.relative_to(ROOT)}")
    print("Unsigned local app. Launch from Terminal or Finder; distribute only after signing/notarizing.")


SET_INSTALLER_ICON = '''import AppKit

guard let icon = NSImage(contentsOfFile: CommandLine.arguments[2]),
      NSWorkspace.shared.setIcon(icon, forFile: CommandLine.arguments[1], options: []) else {
    FileHandle.standardError.write(Data("Could not set the installer icon.\\n".utf8))
    exit(1)
}
'''


BOOTSTRAP = r'''from pathlib import Path
import os
import runpy
import sys

resources = Path(__file__).resolve().parent
source = resources / "source"
os.environ["PATH"] = os.pathsep.join([str(Path.home()/".local/bin"), "/opt/homebrew/bin",
                                     "/usr/local/bin", os.environ.get("PATH", "")])
for engine in ("upsc", "pw_books", "notes_qc"):
    os.environ["NOTE_STUDIO_"+engine.upper()+"_PYTHON"] = str(resources.parent/"MacOS"/engine)
os.chdir(source)
sys.path.insert(0, str(source))
if "--check-package" in sys.argv:
    from note_studio.app import app
    assert app
    import subprocess
    for engine in ("upsc", "pw_books", "notes_qc"):
        subprocess.run([os.environ["NOTE_STUDIO_"+engine.upper()+"_PYTHON"], "-c", "import fitz,requests"], check=True)
    print("PACKAGE_OK")
else:
    sys.argv = [str(source / "run_studio.py"), *sys.argv[1:]]
    runpy.run_path(str(source / "run_studio.py"), run_name="__main__")
'''


if __name__ == "__main__":
    main()
