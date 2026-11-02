"""Windows CI: fresh install, native startup, reinstall, and synthetic-data preservation.

This operates only under a temporary folder on the CI runner. No person's
credentials or existing installation are used.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name != "nt":
        raise SystemExit("Run Windows installer checks on Windows.")
    installers = list(ROOT.glob("dist/team/*/NoteCraft_Setup_*.exe"))
    if not installers:
        raise SystemExit("Build the Windows installer first.")
    installer = max(installers, key=lambda p: p.stat().st_mtime)
    with tempfile.TemporaryDirectory(prefix="pw-installer-check-") as folder:
        work = Path(folder)
        installed = work/"Installed app with spaces"
        data = work/"user data"
        data.mkdir()
        env = dict(os.environ, STUDIO_ROOT=str(data/"doubt"), NOTE_STUDIO_HOME=str(data/"notes"),
                   NOTE_STUDIO_OFFLINE="1", NOTE_STUDIO_NO_BROWSER="1", QBG_STUDIO_NO_BROWSER="1",
                   PW_AUTH_STATE_DIR=str(data/"auth"), QBG_PORTAL_AUTH_PATH=str(data/"portal.json"),
                   QBG_STUDIO_ADDONS=str(data/"addons"))
        command = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                   "/DIR="+str(installed), "/LOG="+str(work/"setup.log")]
        subprocess.run(command, check=True, timeout=240, env=env)
        executable = installed/"NoteCraft.exe"
        assert executable.is_file()
        subprocess.run([str(executable), "--check"], cwd=installed, env=env, check=True, timeout=180)
        # QBG's historical data location is inside its installation. Other
        # apps deliberately keep user data outside the installation.
        sentinel = data/"compatibility-sentinel.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("synthetic saved work", encoding="utf-8")
        subprocess.run(command, check=True, timeout=240, env=env)
        assert sentinel.read_text(encoding="utf-8") == "synthetic saved work"
        subprocess.run([str(executable), "--check"], cwd=installed, env=env, check=True, timeout=180)
        print("INSTALL_REINSTALL_OK: native startup and synthetic saved work preserved")


if __name__ == "__main__":
    main()
