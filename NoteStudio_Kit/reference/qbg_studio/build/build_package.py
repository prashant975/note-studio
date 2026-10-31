r"""
build_package.py - build the QBG Studio package you can hand to the team.

    python build_package.py            builds dist\QBG_Studio_Team and zips it
    python build_package.py --test     ...and unzips it somewhere else and
                                       starts it there to prove it works
    python build_package.py --no-zip   folder only

What goes IN
  QBG_Studio.exe   double-click launcher
  _internal\app\        this toolkit's code, docs and launchers
  _internal\python\     a private Python 3.13 with every library preinstalled, so the
              team needs no Python install and no pip
  a fresh database seeded with this machine's sheet settings
  _internal\ also holds the database, setup helpers, and team README

What stays OUT (checked, not just hoped)
  credentials.json          live Google service-account key
  sign-in tokens            they live under %LOCALAPPDATA%, never in the tree
  this machine's batches, uploads, caches, run folders, logs, backups,
  screenshots, .pyc files

The build refuses to finish if anything secret-shaped is found in the output.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Both can be pointed elsewhere (QBG_BUILD_CACHE, QBG_BUILD_DIR) so a build
# never has to write inside the toolkit folder.
CACHE = Path(os.getenv("QBG_BUILD_CACHE") or (ROOT / "_build_cache"))
DIST = Path(os.getenv("QBG_BUILD_DIR") or (ROOT / "dist"))
NAME = "QBG_Studio_Team"
OUT = DIST / NAME

PY_VERSION = "3.13.5"
EMBED_URL = ("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
             % (PY_VERSION, PY_VERSION))
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
LOCK = ROOT / "qbg_studio" / "requirements-lock.txt"

# Settings copied from this machine's database into the package's fresh one.
# Nothing here is a secret: the sheet id is an address, and the service
# account still has to be given access to it.
#
# Deliberately NOT copied:
#   sheet_tab    - each install works on its own private queue tab, which
#                  Studio names from a per-install id on first use; copying
#                  this machine's tab would make every teammate share it.
#   pilot_size   - this machine may have the pilot step switched off; a new
#                  operator should start with the default pilot gate.
#   install_id / studio_queue_tab / translation_approved_sig - per install.
SEED_SETTINGS = ("sheet_id", "translation_workers")

# --------------------------------------------------------------- excludes --
EXCLUDE_DIRS = {
    "__pycache__", ".git", "_pre_pw_backups", "_build_cache", "dist",
    "translation_parallel_runs", "translation_quality_reports", "data", "uploads", ".pytest_cache", "tests",
}
EXCLUDE_DIR_PREFIXES = ("output_",)
EXCLUDE_FILES = {
    "credentials.json", "studio.out", "studioport.txt", "studio.db",
    "content_translation_cache.json", "content_translation_cache.json.lock",
    "equation_cache.json", "pw-app-kit.zip", "build_package.py",
    "package_launcher.cs",
    "build_personal_package.py",
    "build_single_exe.py", "single_file_launcher.cs",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".token", ".bak", ".log", ".png", ".jpeg",
                    ".jpg", ".db-wal", ".db-shm")
# "credentials." catches the half-written credentials.json.tmp that the
# Settings upload leaves behind if the machine dies mid-write: it holds a
# live private key, so it must never be copied and must still be scanned.
EXCLUDE_PREFIXES = ("qbg_http_error_diagnostic_", "WhatsApp Image", ".env",
                    "credentials.")

# Anything that looks like a live credential. Names of keys in source code
# (the string "refresh_token" in pw_auth.py) are fine; VALUES are not.
SECRET_PATTERNS = [
    (re.compile(rb"-----BEGIN (RSA |EC )?PRIVATE KEY-----"), "private key block"),
    (re.compile(rb"GOCSPX-[A-Za-z0-9_-]{10,}"), "Google OAuth client secret"),
    (re.compile(rb"AIza[0-9A-Za-z_-]{30,}"), "Google API key"),
    (re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\."), "JWT / bearer token"),
    (re.compile(rb"1//0[A-Za-z0-9_-]{30,}"), "Google refresh token"),
    (re.compile(rb"sk-[A-Za-z0-9]{32,}"), "provider API key"),
]
SCAN_SUFFIXES = (".py", ".json", ".txt", ".md", ".bat", ".html", ".js", ".css",
                 ".cfg", ".ini", ".toml", ".yaml", ".yml", ".csv", ".env",
                 ".tmp", ".bak", ".old", ".orig")


def say(msg):
    print("  " + msg, flush=True)


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 1_000_000:
        say("cached  %s" % dest.name)
        return
    say("fetching %s" % url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    if dest.stat().st_size < 1_000_000:
        raise SystemExit("Download of %s looks truncated (%d bytes)."
                         % (url, dest.stat().st_size))


def run(argv, cwd=None, env=None, check=True, quiet=False):
    full_env = dict(os.environ)
    full_env["PYTHONUTF8"] = "1"
    full_env["PYTHONIOENCODING"] = "utf-8"
    full_env["PYTHONDONTWRITEBYTECODE"] = "0"
    if env:
        full_env.update(env)
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=full_env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if check and proc.returncode != 0:
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
        raise SystemExit("Command failed (%d): %s" % (proc.returncode, " ".join(map(str, argv))))
    if not quiet and proc.stdout.strip():
        for line in proc.stdout.strip().splitlines()[-3:]:
            say("  " + line)
    return proc


# ---------------------------------------------------------------- python --
def build_python(python_dir):
    say("private Python %s" % PY_VERSION)
    embed = CACHE / ("python-%s-embed-amd64.zip" % PY_VERSION)
    get_pip = CACHE / "get-pip.py"
    download(EMBED_URL, embed)
    download(GET_PIP_URL, get_pip)

    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(embed) as zf:
        zf.extractall(python_dir)

    # The embeddable build ships with site-packages switched off. Switch it on
    # so pip and the libraries it installs are importable.
    #
    # A ._pth file also puts the interpreter in safe-path mode: a script's
    # own folder is NOT added to sys.path, so "import db" from app.py, or
    # "import config" from create_questions.py, would fail. A normal Python
    # does add it. List the toolkit folders here (relative to python\) so
    # every first-party module is importable exactly as it is on the
    # operator's machine, whichever script or child process is running.
    pth = next(python_dir.glob("python3*._pth"))
    text = pth.read_text(encoding="utf-8")
    text = text.replace("#import site", "import site")
    app_dirs = "\n".join([
        "Lib\\site-packages",
        "..\\app\\qbg_studio",
        "..\\app\\2_Question_Creation_Pipeline",
        "..\\app\\1_Existing_Bots_Windows_Ready",
        "..\\app",
    ])
    if "..\\app" not in text:
        text = text.replace("import site", app_dirs + "\nimport site")
    pth.write_text(text, encoding="utf-8")

    exe = python_dir / "python.exe"
    say("installing pip")
    run([str(exe), str(get_pip), "--no-warn-script-location", "-q"], quiet=True)
    say("installing libraries from %s" % LOCK.name)
    run([str(exe), "-m", "pip", "install", "-q", "--no-warn-script-location",
         "--disable-pip-version-check", "-r", str(LOCK)], quiet=True)

    say("checking imports in the private Python")
    run([str(exe), "-c",
         "import fastapi, uvicorn, jinja2, multipart, openpyxl, requests, PIL, "
         "playwright, google.oauth2.service_account, google.auth.transport.requests, "
         "sqlite3, ctypes, ssl; import playwright, pathlib; "
         "assert (pathlib.Path(playwright.__file__).parent/'driver'/'node.exe').exists(), 'playwright driver missing'; "
         "print('ok')"], quiet=True)
    return exe


# ------------------------------------------------------------------- app --
def _excluded(rel_parts, name, is_dir):
    if is_dir:
        if name in EXCLUDE_DIRS:
            return True
        if any(name.startswith(p) for p in EXCLUDE_DIR_PREFIXES):
            return True
        return False
    if name in EXCLUDE_FILES:
        return True
    if name.lower().endswith(EXCLUDE_SUFFIXES):
        return True
    if any(name.startswith(p) for p in EXCLUDE_PREFIXES):
        return True
    return False


def copy_app(app_dir):
    say("copying the toolkit into app\\")
    count = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT)
        dirnames[:] = [d for d in dirnames if not _excluded(rel.parts, d, True)]
        for fn in filenames:
            if _excluded(rel.parts, fn, False):
                continue
            src = Path(dirpath) / fn
            dst = app_dir / rel / fn
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
    say("%d files" % count)
    # The bots folder must have a home for the key the admin hands over.
    bots = app_dir / "1_Existing_Bots_Windows_Ready"
    (bots / "PUT_credentials.json_HERE.txt").write_text(
        "Put credentials.json in this folder (next to this file).\n\n"
        "It is the Google service-account key that lets the bots and QBG Studio\n"
        "read and write the team's Google Sheet. Get it from whoever gave you\n"
        "this package, over a private channel. Never email it, never paste it in\n"
        "chat, never put it in a shared drive.\n\n"
        "You can also add it from the sheet card on the Bots screen in QBG Studio\n"
        "instead.\n",
        encoding="utf-8")


def seed_database(exe, app_dir):
    """A fresh database with this machine's non-secret settings copied over."""
    say("seeding a fresh database with this machine's sheet settings")
    sys.path.insert(0, str(ROOT / "qbg_studio"))
    import db as local_db                                     # noqa: E402
    values = {}
    for key in SEED_SETTINGS:
        try:
            value = local_db.get_setting(key, "")
        except Exception:
            value = ""
        if value not in ("", None):
            values[key] = str(value)
    script = (
        "import db, json, sys; db.init(); "
        "v = json.loads(sys.argv[1]); "
        "[db.set_setting(k, x) for k, x in v.items()]; "
        "print('seeded', sorted(v))"
    )
    import json
    run([str(exe), "-c", script, json.dumps(values)], cwd=app_dir / "qbg_studio")
    # init() creates data\ and the .db; make sure nothing else rode along.
    data = app_dir / "qbg_studio" / "data"
    for p in data.glob("*"):
        if p.name not in ("qbg_studio.db",):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    return values


# ------------------------------------------------------------- launchers --
START_BAT = r'''@echo off
setlocal
title QBG Studio
cd /d "%~dp0"

REM UTF-8 everywhere: the pipeline prints Hindi and symbols a default console
REM cannot encode, which would otherwise abort a run partway through.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>&1

set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
    echo.
    echo   The python folder is missing next to this file.
    echo   Unzip the whole package, then run this again.
    echo.
    pause
    exit /b 1
)

echo.
echo   QBG Studio
echo   ==========
echo.

"%PY%" "%~dp0app\qbg_studio\launch.py"

echo.
echo   QBG Studio has stopped.
pause
endlocal
'''

TOOLS_BAT = r'''@echo off
setlocal
title QBG Studio - install tools
echo.
echo   QBG Studio - install the tools it uses
echo   =======================================================
echo.
echo   Pandoc is only needed to read a NEW Word paper.
echo   Everything else works without it. Google Chrome is needed for the
echo   portal browser and the bots.
echo.

set "PY=%~dp0python\python.exe"
"%PY%" -c "import shutil,os,sys; p=shutil.which('pandoc') or (os.path.exists(os.path.expandvars(r'%%LOCALAPPDATA%%\Pandoc\pandoc.exe')) and 'found') or (os.path.exists(os.path.expandvars(r'%%ProgramFiles%%\Pandoc\pandoc.exe')) and 'found'); print('   Pandoc:      ' + ('installed' if p else 'MISSING'))"
"%PY%" -c "import os; p=[x for x in (os.path.expandvars(r'%%ProgramFiles%%\Google\Chrome\Application\chrome.exe'), os.path.expandvars(r'%%ProgramFiles(x86)%%\Google\Chrome\Application\chrome.exe'), os.path.expandvars(r'%%LocalAppData%%\Google\Chrome\Application\chrome.exe')) if os.path.exists(x)]; print('   Chrome:      ' + ('installed' if p else 'MISSING'))"
echo.

where winget >nul 2>&1
if errorlevel 1 (
    echo   winget is not available on this PC, so install by hand:
    echo     Pandoc       https://pandoc.org/installing.html
    echo     Chrome       https://www.google.com/chrome/
    echo.
    pause
    exit /b 0
)

echo   Press any key to install whatever is missing with winget
echo   Or close this window to install by hand from the addresses above.
pause >nul

winget install --id JohnMacFarlane.Pandoc --silent --accept-package-agreements --accept-source-agreements
winget install --id Google.Chrome --silent --accept-package-agreements --accept-source-agreements

echo.
echo   Done. If QBG Studio was open, close its black window and start it again
echo   so it notices the new programs.
echo.
pause
endlocal
'''


def build_launcher(out):
    """Compile a small Windows launcher using the installed .NET Framework."""
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compilers = [windows / "Microsoft.NET" / arch / "v4.0.30319" / "csc.exe"
                 for arch in ("Framework64", "Framework")]
    compiler = next((p for p in compilers if p.exists()), None)
    if compiler is None:
        raise SystemExit("The Windows .NET Framework C# compiler is required to build QBG_Studio.exe.")
    say("compiling QBG_Studio.exe")
    run([str(compiler), "/nologo", "/target:exe", "/platform:anycpu",
         "/optimize+", "/out:" + str(out / "QBG_Studio.exe"),
         str(ROOT / "package_launcher.cs")])


def write_launchers(out, seeded):
    build_launcher(out)
    out = out / "_internal"
    out.mkdir(parents=True, exist_ok=True)
    (out / "START_QBG_STUDIO.bat").write_text(START_BAT, encoding="utf-8")
    (out / "INSTALL_TOOLS.bat").write_text(TOOLS_BAT, encoding="utf-8")
    readme_src = ROOT / "TEAM_README.md"
    if readme_src.exists():
        text = readme_src.read_text(encoding="utf-8")
        text = text.replace("{{SHEET_ID}}", seeded.get("sheet_id", "(not set)"))
        text = text.replace("{{SHEET_TAB}}", seeded.get("sheet_tab", "QBG Studio Queue"))
        text = text.replace("{{BUILT}}", date.today().isoformat())
        (out / "README_TEAM.md").write_text(text, encoding="utf-8")
    (out / "VERSION.txt").write_text(
        "QBG Studio team package\nbuilt %s\nPython %s\n"
        % (time.strftime("%Y-%m-%d %H:%M"), PY_VERSION), encoding="utf-8")


# ---------------------------------------------------------------- checks --
def secret_scan(out, allowed_files=()):
    say("scanning the package for anything secret-shaped")
    hits = []
    allowed_files = {p.resolve() for p in allowed_files}
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() in allowed_files:
            continue
        if path.relative_to(out).parts[:2] == ("_internal", "python"):
            continue                      # the interpreter and libraries
        if path.name == "credentials.json":
            hits.append((path, "credentials.json present"))
            continue
        if not path.name.lower().endswith(SCAN_SUFFIXES):
            continue
        blob = path.read_bytes()
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(blob):
                hits.append((path, label))
    if hits:
        for path, label in hits:
            print("  SECRET: %s  (%s)" % (path.relative_to(out), label))
        raise SystemExit("Refusing to package: secrets found above.")
    say("clean")


def must_not_exist(out):
    out = out / "_internal"
    for rel in ("app/1_Existing_Bots_Windows_Ready/credentials.json",
                "app/qbg_studio/uploads", "app/qbg_studio/data/batches",
                "app/_pre_pw_backups", "app/1_Existing_Bots_Windows_Ready/translation_parallel_runs",
                "app/2_Question_Creation_Pipeline/output_MS01",
                "app/1_Existing_Bots_Windows_Ready/content_translation_cache.json"):
        if (out / rel).exists():
            raise SystemExit("Excluded path made it into the package: %s" % rel)


def make_zip(out):
    stamp = date.today().isoformat()
    zpath = DIST / ("%s_%s.zip" % (NAME, stamp))
    if zpath.exists():
        zpath.unlink()
    say("zipping to %s" % zpath.name)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                zf.write(path, str(Path(NAME) / path.relative_to(out)))
    say("%.0f MB" % (zpath.stat().st_size / 1e6))
    return zpath


# ------------------------------------------------------------------ test --
def smoke_test(zpath, seeded):
    """Unzip somewhere else (a path with a space in it) and start it there."""
    say("smoke test: unzipping to a temporary folder with a space in its name")
    tmp = Path(tempfile.mkdtemp(prefix="qbg pkg test "))
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(tmp)
    pkg = tmp / NAME
    assert {p.name for p in pkg.iterdir()} == {"QBG_Studio.exe", "_internal"}
    run([str(pkg / "QBG_Studio.exe"), "--check"], cwd=tmp)
    exe = pkg / "_internal" / "python" / "python.exe"
    studio = pkg / "_internal" / "app" / "qbg_studio"
    port = 8123
    env = {"QBG_STUDIO_PORT": str(port), "PYTHONUTF8": "1"}
    # The package ships no key, so lend it this machine's for the test only
    # (read-only pages and a bot pass that matches nothing). It never leaves
    # this PC: the env var points at the file where it already is.
    key = ROOT / "1_Existing_Bots_Windows_Ready" / "credentials.json"
    if key.exists():
        env["QBG_SHEET_CREDENTIALS"] = str(key)

    # Same modules the app imports at startup, from the private Python, from
    # the new location: proves nothing points back at the build machine.
    run([str(exe), "-c", "import app, db, jobs, pipeline_bridge; print('imports ok')"],
        cwd=studio, env=env)

    say("starting the packaged server on port %d" % port)
    proc = subprocess.Popen(
        [str(exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(studio), env=dict(os.environ, **env),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/dashboard" % port, timeout=3) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(1)
        if not ready:
            out = proc.stdout.read() if proc.poll() is not None else ""
            raise SystemExit("Packaged server did not answer within 90 s.\n" + out[-3000:])
        bad = []
        for path in ("/", "/dashboard", "/new", "/created", "/translate",
                     "/translation-runs", "/queue", "/settings",
                     "/bots/translation", "/bots/solution", "/batches/1",
                     "/created/report.xlsx", "/created/translation-report.xlsx"):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=60) as r:
                    body = r.read()
                    status = r.status
                    ctype = r.headers.get("content-type", "")
            except urllib.error.HTTPError as exc:
                status, body, ctype = exc.code, exc.read(), ""
            except Exception as exc:
                status, body, ctype = "ERR %s" % exc, b"", ""
            ok = (status == 200) or (path == "/batches/1" and status == 404)
            if ok and "html" in ctype and b"Traceback" in body:
                ok = False
            say("%-36s %s" % (path, status))
            if not ok:
                bad.append((path, status))
        if bad:
            raise SystemExit("Packaged server failed on: %s" % bad)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()

    # The bot must start from the package with every library present. With
    # a pick text that matches nothing it reads the sheet and exits having
    # touched nothing - the same smoke test HANDOVER.md prescribes. Without
    # a debug Chrome open it stops at the browser step instead; either way
    # a missing library or a wrong path would show up before that.
    say("bot smoke: one pass over the sheet with a pick text that matches nothing")
    bots = pkg / "_internal" / "app" / "1_Existing_Bots_Windows_Ready"
    bot_env = {"RUN_ONCE": "1", "PICK_TEXT": "ZZZ_NO_MATCH", "PYTHONUTF8": "1",
               "SHEET_NAME": seeded.get("sheet_tab", "QBG Studio Queue"),
               "SPREADSHEET_ID": seeded.get("sheet_id", "")}
    if key.exists():
        bot_env["CREDENTIALS_PATH"] = str(key)
    proc = run([str(exe), str(bots / "qbg_solution_addition_bot.py")], cwd=bots,
               env=bot_env, check=False, quiet=True)
    text = (proc.stdout + "\n" + proc.stderr)
    low = text.lower()
    if "no module named" in low or "modulenotfound" in low or "importerror" in low:
        print(text[-3000:])
        raise SystemExit("A library is missing from the private Python.")
    if "picked rows: 0" in low:
        say("bot read the sheet from the package and picked nothing (clean)")
    elif "chrome" in low or "cdp" in low or "connect" in low:
        say("bot reached the browser step (no debug Chrome open here); libraries fine")
    elif "credential" in low or "sign-in" in low or "signed in" in low or "not set up" in low:
        say("bot stopped at a credentials / sign-in message (expected without them)")
    else:
        print(text[-3000:])
        raise SystemExit("Unexpected bot output from the package - see above.")
    say("smoke test passed; test folder: %s" % tmp)
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ main --
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if not LOCK.exists():
        raise SystemExit("Missing %s" % LOCK)

    print("\nQBG Studio - team package\n")
    if OUT.exists():
        # Verify the resolved destination before recursively replacing a build.
        if OUT.resolve().parent != DIST.resolve() or OUT.is_symlink() or OUT.is_junction():
            raise SystemExit("Refusing to replace a build outside the distribution folder.")
        say("removing the previous build")
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    internal = OUT / "_internal"
    exe = build_python(internal / "python")
    copy_app(internal / "app")
    seeded = seed_database(exe, internal / "app")
    write_launchers(OUT, seeded)
    must_not_exist(OUT)
    secret_scan(OUT)

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    say("package folder: %s  (%.0f MB)" % (OUT, total / 1e6))

    if args.no_zip:
        return 0
    zpath = make_zip(OUT)
    if args.test:
        smoke_test(zpath, seeded)
    print("\nDone: %s\n" % zpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
