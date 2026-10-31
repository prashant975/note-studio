"""Update NoteCraft from inside NoteCraft: QBG One's update workflow, copied.

Source: QBG_Complete_Toolkit/QBG_Studio_Vibecraft/backend/studio/qbg_studio/updater.py
(4 Oct 2026), read in place and never changed. What is the same:

- The team's releases live in one Google Drive folder. Each installer is a file
  named NoteCraft_Setup_<version>.exe; its notes are the file's Drive
  description or, for a file dropped in by hand, the build's
  NoteCraft_Setup_<version>.txt beside it, shown in the update pop-up.
- Every install compares the newest version there with its own, downloads it
  through the Drive API, and refuses a file whose SHA-256 (the one Drive itself
  reports) or size does not match. A file Google has not finished checking is
  not offered.
- A small detached helper waits for NoteCraft to exit, runs the installer
  silently and starts NoteCraft again. NoteCraft ends with exit code 0 on
  purpose: the desktop launcher quits quietly on 0.
- The folder id is baked into each build (release.json, "releases");
  Settings -> System -> Updates can point one computer at another folder.
- `build_installer.py --publish <folder>` uploads the installer into the folder.

- The folder is read with the team's service-account key, as QBG reads its
  own (the user's decision, 4 Oct 2026: "you can copy service account too").
  Whoever releases NoteCraft adds the key once in Settings -> System -> Updates;
  the build carries it into the team installer, so a teammate's NoteCraft finds
  updates with nothing handed around. The folder is shared with the service
  account's email (Viewer). The key file is never shown; only its email is.

What is different, on purpose:

- Without a team key, or when the service account cannot see the folder,
  NoteCraft reads it as the signed-in person (the Google sign-in the sheets
  already use, which has Drive access), so a folder shared with the team's
  Google accounts works too. Publishing tries the service account first (it
  owns no storage, so only into a Shared drive), then the person.
- An update never starts while notes are being made: the restart would stop a
  paid run halfway.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import brand, db, paths
from .version import RELEASE_FILE, VERSION, setup_name

log = logging.getLogger("note_studio.updater")
FOLDER_SETTING = "update_folder"
CHECKED = "update_checked"
STATE = "update_state"
ERROR = "update_error"
LAUNCHER = f"{brand.APP_NAME}.exe"
SETUP = setup_name()                         # each edition has its own .exe and .txt names
MAX_BYTES = 2000 * 1024 * 1024               # the engines' libraries make NoteCraft larger than QBG
FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FULL_SCOPE = "https://www.googleapis.com/auth/drive"       # publishing: an upload needs to see the folder
TEAM = "the team's service account"
PERSON = "your Google sign-in"
_VERSION = re.compile(r"^\d+(?:\.\d+){3}$")
_NAME = re.compile(rf"^{re.escape(SETUP)}[_-](\d+(?:\.\d+){{3}})\.exe$", re.I)
_NOTES = re.compile(rf"^{re.escape(SETUP)}[_-](\d+(?:\.\d+){{3}})\.txt$", re.I)
NOTES_BYTES = 4000                           # a few lines of fixes; anything longer is cut
_FOLDER = re.compile(r"(?:drive\.google\.com/drive/(?:u/\d+/)?folders/|^)([A-Za-z0-9_-]{10,})")
_SECRET = re.compile(r"(?:ya29\.[0-9A-Za-z_\-.]+|1//0[0-9A-Za-z_\-]{20,}|AIza[0-9A-Za-z_\-]{30,}|GOCSPX-[0-9A-Za-z_\-]+"
                     r"|(?i:bearer)\s+[0-9A-Za-z_\-.]{20,})")
_lock = threading.Lock()
_reader = None                              # the session that last read the folder (download uses it)

# The helper: waits for NoteCraft to be gone, installs silently, starts NoteCraft.
HELPER = r"""param([string]$Root,[string]$Setup,[string]$Log)
$deadline = (Get-Date).AddSeconds(90)
do {
  Start-Sleep -Seconds 2
  $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase) })
} while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)
foreach ($p in $left) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$run = Start-Process -FilePath $Setup -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES',('/LOG="' + $Log + '"') -Wait -PassThru
Set-Content -Path ($Log + '.result') -Value ('exit ' + $run.ExitCode)
Start-Process -FilePath (Join-Path $Root '__LAUNCHER__') -WorkingDirectory $Root
""".replace("__LAUNCHER__", LAUNCHER)


def redact(text) -> str:
    """Error text without anything token-shaped: it is shown on a page and kept."""
    return _SECRET.sub("***", str(text or ""))


# ------------------------------------------------------------------ versions
def parse_version(text) -> tuple | None:
    text = str(text or "").strip()
    return tuple(int(p) for p in text.split(".")) if _VERSION.match(text) else None


def is_newer(candidate, current) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def release_info() -> dict:
    try:
        value = json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def current_version() -> str:
    return str(release_info().get("version") or VERSION)


def same_edition(entry: dict) -> bool:
    """Also guard saved update references when the two editions share a data folder."""
    name = str(entry.get("name") or "")
    found = _NAME.fullmatch(name)
    return bool(found and found.group(1) == entry.get("version"))


def require_edition(entry: dict) -> None:
    if not same_edition(entry):
        raise ValueError("That update is for a different edition. Press Check for updates again.")


def installed_root(executable: str | None = None) -> Path | None:
    """The folder holding NoteCraft.exe when this NoteCraft was installed; None from the project folder."""
    here = Path(executable or sys.executable).resolve()
    for folder in here.parents:
        if (folder / LAUNCHER).is_file():
            return folder
    return None


# -------------------------------------------------------------------- source
def drive_folder_id(link: str) -> str:
    found = _FOLDER.search(str(link or "").strip())
    if not found:
        raise ValueError("Paste the release folder as a Google Drive folder link (drive.google.com/drive/folders/...).")
    return found.group(1)


def source() -> dict:
    """The release folder: this computer's own choice, else the one built in."""
    own = db.get_setting(FOLDER_SETTING, "").strip()
    built_in = str(release_info().get("releases") or "").strip()
    return {"folder_id": own or built_in, "own": bool(own), "built_in": built_in}


def save_folder(link: str) -> None:
    """A folder link or id; empty goes back to the folder built into this release."""
    link = str(link or "").strip()
    db.set_setting(FOLDER_SETTING, drive_folder_id(link) if link else "")


def _json(key: str) -> dict:
    try:
        value = json.loads(db.get_setting(key, "") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- the team key
def team_key_only(path) -> str:
    """QBG's check: the one key NoteCraft keeps is a Google service-account key.
    Anything else in that file, such as a person's sign-in, is refused. Returns its email."""
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise ValueError("it is not a service-account key")
        if not str(info.get("client_email") or "").endswith(".iam.gserviceaccount.com") or not info.get("private_key"):
            raise ValueError("it has no service account or private key")
    except (OSError, ValueError) as exc:
        raise ValueError("That is not a valid team key: " + str(exc) + ".") from None
    return info["client_email"]


def team_key() -> Path | None:
    """This computer's team key (added in Settings), else the one built into this install."""
    for path in paths.team_key_files():
        if path.is_file():
            return path
    return None


def team_account() -> str:
    key = team_key()
    if key is None:
        return ""
    try:
        return team_key_only(key)
    except ValueError:
        return ""


def save_team_key(data: bytes) -> str:
    """The key file from Settings, checked, into ~/.note_studio (the old one kept as .bak)."""
    target = paths.home() / "team_key.json"
    pending = target.with_suffix(".json.pending")
    pending.write_bytes(data)
    try:
        email = team_key_only(pending)
    except ValueError:
        pending.unlink(missing_ok=True)
        raise
    if target.is_file():
        target.replace(target.with_suffix(".json.bak"))
    pending.replace(target)
    db.set_setting(CHECKED, "")
    return email


def remove_team_key() -> bool:
    target = paths.home() / "team_key.json"
    if not target.is_file():
        return False
    target.replace(target.with_suffix(".json.bak"))
    db.set_setting(CHECKED, "")
    return True


def _offline() -> bool:
    return os.environ.get("NOTE_STUDIO_OFFLINE") == "1"


def service_session(scope: str = READ_SCOPE):
    """Drive as the team's service account, as QBG reads its release folder."""
    key = team_key()
    if key is None:
        raise PermissionError("No team key is added. Add it under Settings -> System -> Updates.")
    if _offline():
        raise PermissionError("Updates are not checked while NoteCraft is offline.")
    team_key_only(key)
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    return AuthorizedSession(Credentials.from_service_account_file(str(key), scopes=[scope]))


def person_session():
    """Drive as the signed-in person. Their sign-in has the Drive scope already
    (sheets_bridge.settings.SCOPES), so nothing new is asked for or stored."""
    from google.auth.transport.requests import AuthorizedSession

    from . import signin

    current = signin.auth()
    account = current.account() if current is not None else None
    if account is None:
        raise PermissionError("Sign in with Google in Settings to check for updates.")
    return AuthorizedSession(account.creds)


def drive_session(scope: str = READ_SCOPE):
    """The team's service account when its key is here (QBG's way), else the signed-in person."""
    return service_session(scope) if team_key() is not None else person_session()


def _readers():
    if team_key() is not None:
        yield TEAM, service_session
    yield PERSON, person_session


# ------------------------------------------------------------------ the list
def parse_files(files) -> list[dict]:
    """The folder's installers as release entries, newest version first. A file
    without Drive's SHA-256 yet keeps an empty sha256: nothing is installed unverified."""
    notes_files = {}
    for item in files or []:
        found = _NOTES.match(str(item.get("name") or ""))
        if found and item.get("id"):
            notes_files[found.group(1)] = item["id"]
    rows = []
    for item in files or []:
        found = _NAME.match(str(item.get("name") or ""))
        if not found or not item.get("id"):
            continue
        stamp = str(item.get("modifiedTime") or "")
        rows.append({"version": found.group(1), "file_id": item["id"], "name": item["name"],
                     "size": int(item.get("size") or 0), "sha256": str(item.get("sha256Checksum") or "").lower(),
                     "released": stamp[:16].replace("T", " "), "notes": str(item.get("description") or "").strip(),
                     "notes_file": notes_files.get(found.group(1), "")})
    rows.sort(key=lambda r: parse_version(r["version"]) or (), reverse=True)
    return rows


def list_releases(drive=None) -> list[dict]:
    src = source()
    if not src["folder_id"]:
        raise PermissionError("No release folder is set. This version carries none; paste the team's folder "
                              "link under Updates.")
    global _reader
    attempts = [(PERSON, lambda: drive)] if drive is not None else list(_readers())
    problems = []
    for who, make in attempts:
        try:
            session = make()
        except PermissionError as exc:
            problems.append(str(exc))
            continue
        reply = session.get(FILES, params={"q": "'%s' in parents and trashed = false" % src["folder_id"],
                                           "fields": "files(id,name,size,sha256Checksum,modifiedTime,description)",
                                           "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                                           "pageSize": "100"}, timeout=30)
        if reply.status_code == 200:
            _reader = session
            return parse_files(reply.json().get("files") or [])
        if who == TEAM:
            problems.append("The release folder could not be read with the team's service account (HTTP %d). Share "
                            "it with %s as Viewer, and check that the Drive API is on for its project."
                            % (reply.status_code, team_account()))
        else:
            problems.append("The release folder could not be read (HTTP %d). Ask whoever releases %s to share "
                            "the folder with your Google account." % (reply.status_code, brand.APP_NAME))
    raise PermissionError(problems[0] if problems else "The release folder could not be read.")


def read_notes(file_id: str, drive=None) -> str:
    """The text of a release's notes file (NoteCraft_Setup_<version>.txt), or ''."""
    try:
        drive = drive or _reader or drive_session()
        reply = drive.get(f"{FILES}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, timeout=30)
        if reply.status_code != 200:
            return ""
        return reply.content[:NOTES_BYTES].decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - the update itself never depends on its notes
        return ""


def note_lines(notes, limit: int = 8) -> list[str]:
    """Release notes as a short list for the pop-up: one fix per line or per '; '."""
    lines = []
    for raw in re.split(r"[\r\n]+|;\s+", str(notes or "")):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
        if line and line not in lines:
            lines.append(line[:200])
    return lines[:limit]


def check(drive=None) -> dict:
    """Read the folder and remember what was found. Never raises to a screen."""
    result = {"checked_at": time.time(), "current": current_version(), "latest": None, "available": False,
              "error": ""}
    try:
        rows = list_releases(drive)
        verified = [r for r in rows if r["sha256"]]
        if rows and (not verified or rows[0] is not verified[0]):
            result["error"] = "Google has not finished checking %s yet; try again in a minute." % rows[0]["name"]
        if verified:
            result["latest"] = verified[0]
            result["available"] = is_newer(verified[0]["version"], result["current"])
            if result["available"] and not result["latest"]["notes"] and result["latest"].get("notes_file"):
                result["latest"]["notes"] = read_notes(result["latest"]["notes_file"], drive)
    except Exception as exc:  # noqa: BLE001
        result["error"] = redact(exc)[:300]
    db.set_setting(CHECKED, json.dumps(result))
    return result


# ------------------------------------------------------------------ download
def download(entry: dict, target, drive=None, progress=None) -> int:
    """Stream the installer through the Drive API, then refuse anything whose
    SHA-256 or size is not what Drive reported for the file."""
    require_edition(entry)
    drive = drive or _reader or drive_session()
    reply = drive.get(f"{FILES}/{entry['file_id']}", params={"alt": "media", "supportsAllDrives": "true"},
                      stream=True, timeout=120)
    if reply.status_code != 200:
        raise ValueError("The download failed (HTTP %d). Press Check for updates, then try again." % reply.status_code)
    total = int(reply.headers.get("content-length") or 0) or int(entry.get("size") or 0)
    done, digest = 0, hashlib.sha256()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "wb") as handle:
            for chunk in reply.iter_content(256 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if done > MAX_BYTES:
                    raise ValueError(f"The download is larger than any {brand.APP_NAME} installer; not installed.")
                if progress:
                    progress(done, total)
        if not entry.get("sha256"):
            raise ValueError("Drive has not reported a SHA-256 for this file yet; not installed.")
        if digest.hexdigest() != str(entry["sha256"]).lower():
            raise ValueError("The downloaded file does not match the SHA-256 Drive reports; not installed.")
        if entry.get("size") and done != int(entry["size"]):
            raise ValueError("The download is incomplete (%d of %d bytes); not installed." % (done, int(entry["size"])))
    except Exception:
        target.unlink(missing_ok=True)       # a refused installer is never left lying around
        raise
    return done


# ------------------------------------------------------------------- install
def helper_command(script, root, setup, log_path) -> list[str]:
    """Started through cmd's start so the helper is nobody's child: the
    launcher's own taskkill /T on the server tree must not take it down."""
    return ["cmd", "/c", "start", "", "/min", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script), "-Root", str(root), "-Setup", str(setup),
            "-Log", str(log_path)]


def install(entry: dict, progress=None, drive=None, launcher=None) -> Path:
    """Download, verify, hand over to the helper. Returns the install log path."""
    require_edition(entry)
    root = installed_root()
    if not root:
        raise ValueError(f"This {brand.APP_NAME} runs from the project folder, so it cannot install over itself. "
                         "Build and run the installer instead.")
    work = Path(tempfile.gettempdir()) / "notecraft_update"
    work.mkdir(parents=True, exist_ok=True)
    from . import update_delivery

    problem = update_delivery.installation_problem()
    if problem:
        raise ValueError(problem)
    setup = update_delivery.prepare(entry, drive=drive, progress=progress)
    # Work may have started during a manual download. The normal UI path also
    # holds the queue before entering this function.
    problem = update_delivery.installation_problem()
    if problem:
        raise ValueError(problem)
    script = work / "update.ps1"
    script.write_text(HELPER, encoding="utf-8")
    log_path = work / f"install_{entry['version']}.log"
    for stale in (log_path, Path(str(log_path) + ".result")):
        stale.unlink(missing_ok=True)
    command = helper_command(script, root, setup, log_path)
    launcher = launcher or (lambda argv: subprocess.Popen(
        argv, cwd=str(work), close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)))
    launcher(command)
    return log_path


def _state(**fields) -> dict:
    fields.setdefault("at", time.time())
    db.set_setting(STATE, json.dumps(fields))
    return fields


def making_notes() -> int:
    """Runs being made right now: an update waits for them."""
    try:
        return sum(r["state"] == "running" for r in db.active_runs())
    except Exception:  # noqa: BLE001
        return 0


def run_update(entry: dict, exit_process=None) -> bool:
    """Install only after the user's click; reuse and recheck the cached download."""
    version = entry["version"]
    _state(state="downloading", version=version, message=f"Downloading {version}...", done=0,
           total=int(entry.get("size") or 0))
    try:
        log_path = install(entry, progress=lambda done, total: _state(
            state="downloading", version=version, message=f"Downloading {version}...", done=done, total=total))
    except Exception as exc:  # noqa: BLE001
        log.error("update failed: %s", redact(exc)[:300])
        _state(state="failed", version=version, error=redact(exc)[:300])
        from . import job_queue

        if job_queue.held() == "update":
            job_queue.resume()
        return False
    _state(state="installing", version=version, log=str(log_path),
           message=f"Installing {version}. {brand.APP_NAME} closes now and starts again by itself in about a minute.")
    time.sleep(1.5)
    (exit_process or (lambda: os._exit(0)))()
    return True


def start_update(entry: dict | None = None):
    """Begin an update in a thread; refuses a second one while one is going."""
    entry = entry or _json(CHECKED).get("latest")
    if not entry or not is_newer(entry.get("version"), current_version()):
        raise ValueError("There is no newer version to install. Press Check for updates first.")
    require_edition(entry)
    if not installed_root():
        raise ValueError(f"This {brand.APP_NAME} runs from the project folder, so it cannot install over itself.")
    from . import job_queue, update_delivery

    with _lock, job_queue._lock:
        problem = update_delivery.installation_problem()
        if problem:
            raise ValueError(problem)
        state = _json(STATE)
        if state.get("state") in ("downloading", "installing") and time.time() - float(state.get("at") or 0) < 900:
            raise ValueError("An update is already in progress.")
        # Share the claim lock so a queued run cannot start between this check
        # and the restart. Downloads themselves never hold the queue.
        if not job_queue.held():
            job_queue.hold("update")
        _state(state="downloading", version=entry["version"], message="Starting the download...", done=0,
               total=int(entry.get("size") or 0))
    thread = threading.Thread(target=run_update, args=(entry,), daemon=True, name="notecraft-update")
    thread.start()
    return thread


def settle_state() -> dict:
    """What the last update left behind, read against what is running now."""
    state = _json(STATE)
    if not state:
        return {}
    current = current_version()
    if state.get("state") == "installing":
        if state.get("version") == current:
            state = _state(state="done", version=current, message=f"Updated to {current}.")
        else:
            result = Path(str(state.get("log") or "") + ".result")
            if result.is_file():
                code = result.read_text(encoding="utf-8", errors="replace").strip()
                if code != "exit 0":
                    state = _state(state="failed", version=state.get("version"),
                                   error=f"The installer ended with {code}. Its log: {state.get('log')}")
            elif time.time() - float(state.get("at") or 0) > 900:
                state = _state(state="failed", version=state.get("version"),
                               error="The update did not finish. Run the installer from the release folder by hand.")
    elif state.get("state") == "downloading" and time.time() - float(state.get("at") or 0) > 900:
        # NoteCraft was closed during the download: nothing was installed.
        state = _state(state="failed", version=state.get("version"),
                       error="The download stopped before it finished. Press Update to try again.")
    return state


def status() -> dict:
    checked = _json(CHECKED)
    current = current_version()
    latest = checked.get("latest")
    if latest and not same_edition(latest):
        latest = None
    state = settle_state()
    ready = bool(latest and state.get("state") == "ready" and state.get("version") == latest.get("version")
                 and state.get("sha256") == latest.get("sha256")
                 and Path(str(state.get("setup") or "")).is_file())
    return {"current": current, "installed": bool(installed_root()), "latest": latest,
            "available": bool(latest and is_newer(latest.get("version"), current)),
            "notes": note_lines((latest or {}).get("notes")),
            "checked_at": checked.get("checked_at"), "error": checked.get("error", ""),
            "state": state, "download_ready": ready, "automatic": True,
            "source": source(), "last_error": db.get_setting(ERROR, ""),
            "updated_at": str(release_info().get("updated_at") or ""), "setup_name": f"{SETUP}_<version>",
            "team_account": team_account(), "team_key_here": (paths.home() / "team_key.json").is_file()}


# ---------------------------------------------------------------- publishing
def _upload_problem(reply, who: str = PERSON) -> str:
    text = reply.text[:400]
    if "storageQuotaExceeded" in text or "storage quota" in text.lower():
        if who == TEAM:
            return ("Google gives a service account no storage of its own, so it cannot put a file into a My Drive "
                    "folder. Move the folder into a Shared drive with the service account as a member, or publish "
                    "while signed in to NoteCraft, or drop the file in by hand.")
        return "Your Google Drive is full, so the installer could not be put into the folder. Drop it in by hand."
    if reply.status_code in (403, 404):
        whose = f"the service account ({team_account()})" if who == TEAM else "your Google account"
        return ("Drive cannot put a file into the release folder (HTTP %d). Give %s edit access to it, "
                "or drop the file in by hand." % (reply.status_code, whose))
    return "Drive refused the upload (HTTP %d): %s" % (reply.status_code, redact(text)[:200])


def upload_installer(installer, version: str, folder: str, notes: str = "", drive=None, who: str = PERSON,
                     setup_prefix: str | None = None) -> dict:
    """The installer into the release folder under the name every NoteCraft looks for."""
    installer = Path(installer)
    size = installer.stat().st_size
    drive = drive or drive_session(FULL_SCOPE)
    meta = {"name": f"{setup_prefix or SETUP}_{version}.exe", "parents": [drive_folder_id(folder)],
            "description": str(notes or "")[:300]}
    start = drive.post(UPLOAD + "?uploadType=resumable&supportsAllDrives=true", json=meta,
                       headers={"X-Upload-Content-Type": "application/octet-stream",
                                "X-Upload-Content-Length": str(size)}, timeout=60)
    if start.status_code != 200:
        raise PermissionError(_upload_problem(start, who))
    digest = hashlib.sha256()
    with open(installer, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    with open(installer, "rb") as handle:            # streamed: the installer is large
        put = drive.put(start.headers["Location"], data=handle,
                        headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                        timeout=3600)
    if put.status_code not in (200, 201):
        raise PermissionError(_upload_problem(put, who))
    file_id = put.json()["id"]
    return {"file_id": file_id, "link": f"https://drive.google.com/file/d/{file_id}/view",
            "sha256": digest.hexdigest(), "size": size}


def publish(installer, version: str, folder: str, notes: str = "", drive=None,
            setup_prefix: str | None = None) -> dict:
    """What `build_installer.py --publish` does after building: as the team's service
    account first (QBG's way; it goes through into a Shared drive), then as the person
    signed in to NoteCraft on this computer (a person's Drive has storage)."""
    if drive is not None:
        return upload_installer(installer, version, folder, notes=notes, drive=drive, setup_prefix=setup_prefix)
    problems = []
    for who, make in ((TEAM, lambda: service_session(FULL_SCOPE)), (PERSON, person_session)):
        if who == TEAM and team_key() is None:
            continue
        try:
            return upload_installer(installer, version, folder, notes=notes, drive=make(), who=who,
                                    setup_prefix=setup_prefix)
        except PermissionError as exc:
            problems.append(str(exc))
    raise PermissionError(" ".join(problems) or "Nobody to publish as: add the team key or sign in.")
