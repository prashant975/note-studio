"""Build an unsigned, self-contained macOS app using a relocatable uv CPython runtime.

Run with the project's .venv/bin/python. Windows keeps its existing installer.
Only Git source files under the runtime allowlist are bundled; local data and
credentials are excluded. Rebuild after pulling source updates.
"""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "NoteCraft"
ENTRY = "run_studio.py"
SOURCE_ROOTS = ["note_studio","engines","run_studio.py"]


def main():
    if sys.platform != "darwin":
        raise SystemExit("Build macOS apps on macOS; use the existing Windows builder on Windows.")
    runtime = Path(sys.base_prefix)
    if not (runtime / "BUILD").exists() or "cpython-3.13" not in runtime.name:
        raise SystemExit("Use a relocatable uv Python 3.13 environment (uv python install 3.13; recreate .venv).")
    app = ROOT / "dist" / (NAME + ".app")
    if app.exists():
        raise SystemExit(f"{app} already exists. Move that generated app aside before rebuilding.")
    resources = app / "Contents/Resources"
    macos = app / "Contents/MacOS"
    resources.mkdir(parents=True)
    macos.mkdir(parents=True)
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
                      "CFBundleVersion": "1", "CFBundlePackageType": "APPL"}, stream)
    with tempfile.TemporaryDirectory(prefix="mac-package-check-") as folder:
        env = dict(os.environ, STUDIO_ROOT=folder, NOTE_STUDIO_HOME=folder,
                   NOTE_STUDIO_OFFLINE="1", QBG_STUDIO_DB_PATH=str(Path(folder)/"qbg.db"),
                   PW_AUTH_STATE_DIR=str(Path(folder)/"auth"), QBG_PORTAL_AUTH_PATH=str(Path(folder)/"portal.json"))
        subprocess.run([str(launcher), "--check-package"], check=True, env=env)
    print(f"Built and import-checked: {app}")
    print("Unsigned local app. Launch from Terminal or Finder; distribute only after signing/notarizing.")


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
