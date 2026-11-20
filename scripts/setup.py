"""Create independent Python 3.13 environments; never copy a venv between computers."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def install(folder, requirements):
    python = folder / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(folder)
    subprocess.run([str(python), "-m", "pip", "install", *sum(
        (["-r", str(ROOT / req)] for req in requirements), [])], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="Install test dependencies too.")
    parser.add_argument("--skip-engines", action="store_true", help="Only install the main process.")
    parser.add_argument("--desktop-build", action="store_true", help="Include the full installer's local transcription/browser dependencies.")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13):
        parser.error("Use Python 3.13 (python3.13 on macOS; py -3.13 on Windows).")
    install(ROOT / ".venv", ["requirements.txt", "requirements-platform.txt", "engines/shortnotes/sheets_bridge/requirements.txt"] +
            (["requirements.txt"] if args.dev else []))
    if not args.skip_engines:
        for engine in ("upsc", "pw_books", "notes_qc"):
            requirements = [f"engines/{engine}/requirements.txt"]
            if args.desktop_build and engine == "upsc":
                requirements.append("packaging/requirements-upsc-build.txt")
            install(ROOT / "engines" / engine / ".venv", requirements)
    print("Setup complete. Run scripts/start.sh (macOS) or scripts/start.ps1 (Windows).")


if __name__ == "__main__":
    main()
