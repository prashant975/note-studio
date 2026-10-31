"""Update Studio from inside Studio.

The team's releases live in one Google Drive folder ("QBG Studio releases"):
each installer is a file named QBG_Studio_Setup_<version>.exe, and its notes
are the file's description or, for a file dropped in by hand, the build's
QBG_Studio_Setup_<version>.txt beside it - shown in the update pop-up so a
teammate sees at once whether the fixes matter to them (30 September 2026).
Every install reads that folder with the
service-account key it already has (the folder is shared with the account),
compares the newest version with its own, downloads through the Drive API,
checks the SHA-256 that Drive itself reports for the file, then hands over to
a small detached helper: it waits for Studio to exit, runs the installer
silently and starts Studio again. Studio ends with exit code 0 on purpose -
the desktop launcher quits quietly on 0 and shows "could not start" on
anything else.

Publishing is putting the file in the folder. The build does it through the
API when the folder lives in a Shared drive; a service account owns no
storage of its own, so into a My Drive folder the file is dropped by hand,
under the name the build already gave it. Nothing in the app gains a
credential: the folder id is baked into each build, and the key is the one
every install has for the team sheet.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import db

FOLDER_SETTING = 'update_folder'
CHECKED = 'update_checked'
STATE = 'update_state'
ERROR = 'update_error'
RELEASE_FILE = Path(__file__).resolve().parent / 'static' / 'release.json'
MAX_BYTES = 400 * 1024 * 1024
FILES = 'https://www.googleapis.com/drive/v3/files'
_VERSION = re.compile(r'^\d+(?:\.\d+){3}$')
_NAME = re.compile(r'^QBG_Studio_Setup[_-](\d+(?:\.\d+){3})\.exe$', re.I)
_NOTES = re.compile(r'^QBG_Studio_Setup[_-](\d+(?:\.\d+){3})\.txt$', re.I)
NOTES_BYTES = 4000           # a few lines of fixes; anything longer is cut
_FOLDER = re.compile(r'(?:drive\.google\.com/drive/(?:u/\d+/)?folders/|^)([A-Za-z0-9_-]{10,})')
_lock = threading.Lock()

# The helper: waits for Studio to be gone, installs silently, starts Studio.
HELPER = r'''param([string]$Root,[string]$Setup,[string]$Log)
$deadline = (Get-Date).AddSeconds(90)
do {
  Start-Sleep -Seconds 2
  $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase) })
} while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)
foreach ($p in $left) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$run = Start-Process -FilePath $Setup -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES',('/LOG="' + $Log + '"') -Wait -PassThru
Set-Content -Path ($Log + '.result') -Value ('exit ' + $run.ExitCode)
Start-Process -FilePath (Join-Path $Root 'QBG_Studio.exe') -WorkingDirectory $Root
'''


# ------------------------------------------------------------- versions --
def parse_version(text):
    text = str(text or '').strip()
    return tuple(int(v) for v in text.split('.')) if _VERSION.match(text) else None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def release_info():
    try:
        value = json.loads(RELEASE_FILE.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def current_version():
    return str(release_info().get('version') or '')


def installed_root(executable=None):
    """The folder holding QBG_Studio.exe when this Studio was installed; None from source."""
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if (parent / 'QBG_Studio.exe').is_file():
            return parent
    return None


# --------------------------------------------------------------- source --
def drive_folder_id(link):
    match = _FOLDER.search(str(link or '').strip())
    if not match:
        raise ValueError('Give the release folder as a Google Drive folder link or id.')
    return match.group(1)


def source():
    """The release folder: this Studio's own choice, else the one built in."""
    own = db.get_setting(FOLDER_SETTING, '').strip()
    built_in = str(release_info().get('releases') or '').strip()
    return dict(folder_id=own or built_in, own=bool(own), built_in=built_in)


def save_folder(link):
    """A folder link or id; empty goes back to the folder built into this release."""
    link = (link or '').strip()
    db.set_setting(FOLDER_SETTING, drive_folder_id(link) if link else '')


def _json(key):
    try:
        value = json.loads(db.get_setting(key, '') or '{}')
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def drive_session(scope='https://www.googleapis.com/auth/drive.readonly'):
    """Drive with the key each install already has. Publishing asks for the
    full scope: an upload into a shared folder needs to see that folder."""
    import pipeline_bridge as pb
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(str(pb.credentials_path()), scopes=[scope])
    return AuthorizedSession(creds)


# ------------------------------------------------------------- the list --
def parse_files(files):
    """The folder's installers as release entries, newest version first.
    A file without Drive's SHA-256 yet is left out: nothing is installed
    unverified."""
    rows = []
    notes_files = {}
    for item in files or []:
        found = _NOTES.match(str(item.get('name') or ''))
        if found and item.get('id'):
            notes_files[found.group(1)] = item['id']
    for item in files or []:
        match = _NAME.match(str(item.get('name') or ''))
        if not match or not item.get('id'):
            continue
        stamp = str(item.get('modifiedTime') or '')
        rows.append(dict(version=match.group(1), file_id=item['id'], name=item['name'],
                         size=int(item.get('size') or 0), sha256=str(item.get('sha256Checksum') or '').lower(),
                         released=stamp[:16].replace('T', ' '), notes=str(item.get('description') or '').strip(),
                         notes_file=notes_files.get(match.group(1), '')))
    rows.sort(key=lambda r: parse_version(r['version']), reverse=True)
    return rows


def list_releases(drive=None):
    src = source()
    if not src['folder_id']:
        raise PermissionError('No release folder is set. This build carries none; paste the team folder link under Updates.')
    drive = drive or drive_session()
    reply = drive.get(FILES, params={'q': "'%s' in parents and trashed = false" % src['folder_id'],
                                     'fields': 'files(id,name,size,sha256Checksum,modifiedTime,description)',
                                     'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true', 'pageSize': '100'},
                      timeout=30)
    if reply.status_code != 200:
        raise PermissionError('The release folder could not be read (HTTP %d). Share it with the service account, '
                              'and check that the Drive API is enabled for its project.' % reply.status_code)
    return parse_files(reply.json().get('files') or [])


def read_notes(file_id, drive=None):
    """The text of a release's notes file (QBG_Studio_Setup_<version>.txt), or ''."""
    try:
        drive = drive or drive_session()
        reply = drive.get(FILES + '/' + file_id, params={'alt': 'media', 'supportsAllDrives': 'true'}, timeout=30)
        if reply.status_code != 200:
            return ''
        return reply.content[:NOTES_BYTES].decode('utf-8', errors='replace').strip()
    except Exception:
        return ''          # the update itself never depends on its notes


def note_lines(notes, limit=8):
    """Release notes as a short list for the pop-up: one fix per line or per '; '."""
    lines = []
    for raw in re.split(r'[\r\n]+|;\s+', str(notes or '')):
        line = re.sub(r'^\s*(?:[-*\u2022]|\d+[.)])\s*', '', raw).strip()
        if line and line not in lines:
            lines.append(line[:200])
    return lines[:limit]


def check(drive=None):
    """Read the folder and remember what was found. Never raises to a screen."""
    import pipeline_bridge as pb
    result = dict(checked_at=time.time(), current=current_version(), latest=None, available=False, error='')
    try:
        rows = list_releases(drive)
        verified = [r for r in rows if r['sha256']]
        if rows and not verified:
            result['error'] = 'Google has not finished checking %s yet; try again in a minute.' % rows[0]['name']
        if verified:
            result['latest'] = verified[0]
            result['available'] = is_newer(verified[0]['version'], result['current'])
            if result['available'] and not result['latest']['notes'] and result['latest'].get('notes_file'):
                result['latest']['notes'] = read_notes(result['latest']['notes_file'], drive)
    except Exception as exc:
        result['error'] = pb.redact_secrets(str(exc))[:300]
    db.set_setting(CHECKED, json.dumps(result))
    return result


# ------------------------------------------------------------- download --
def download(entry, target, drive=None, progress=None):
    """Stream the installer through the Drive API, then refuse anything
    whose SHA-256 or size is not what Drive reported for the file."""
    drive = drive or drive_session()
    reply = drive.get('%s/%s?alt=media&supportsAllDrives=true' % (FILES, entry['file_id']), stream=True, timeout=120)
    if reply.status_code != 200:
        raise ValueError('The download failed (HTTP %d). Check that the release folder is shared with the service account.'
                         % reply.status_code)
    total = int(reply.headers.get('content-length') or 0) or int(entry.get('size') or 0)
    done, digest = 0, hashlib.sha256()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'wb') as handle:
        for chunk in reply.iter_content(256 * 1024):
            if not chunk:
                continue
            handle.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if done > MAX_BYTES:
                raise ValueError('The download is larger than any QBG One installer; not installed.')
            if progress:
                progress(done, total)
    if not entry.get('sha256'):
        raise ValueError('Drive has not reported a SHA-256 for this file yet; not installed.')
    if digest.hexdigest() != entry['sha256']:
        raise ValueError('The downloaded file does not match the SHA-256 Drive reports; not installed.')
    if entry.get('size') and done != int(entry['size']):
        raise ValueError('The download is incomplete (%d of %d bytes); not installed.' % (done, int(entry['size'])))
    return done


# -------------------------------------------------------------- install --
def helper_command(script, root, setup, log):
    """Started through cmd's start so the helper is nobody's child: the
    launcher's own taskkill /T on the server tree must not take it down."""
    return ['cmd', '/c', 'start', '', '/min', 'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-WindowStyle', 'Hidden', '-File', str(script), '-Root', str(root), '-Setup', str(setup), '-Log', str(log)]


def install(entry, progress=None, drive=None, launcher=None):
    """Download, verify, hand over to the helper. Returns the install log path."""
    root = installed_root()
    if not root:
        raise ValueError('This QBG One runs from source, so it cannot install over itself. Build and run the installer instead.')
    work = Path(tempfile.gettempdir()) / 'qbg_studio_update'
    work.mkdir(parents=True, exist_ok=True)
    setup = work / ('QBG_Studio_Setup_%s.exe' % entry['version'])
    download(entry, setup, drive=drive, progress=progress)
    script = work / 'update.ps1'
    script.write_text(HELPER, encoding='utf-8')
    log = work / ('install_%s.log' % entry['version'])
    command = helper_command(script, root, setup, log)
    launcher = launcher or (lambda argv: subprocess.Popen(
        argv, cwd=str(work), close_fds=True,
        creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)))
    launcher(command)
    return log


def _state(**fields):
    fields.setdefault('at', time.time())
    db.set_setting(STATE, json.dumps(fields))
    return fields


def run_update(entry, exit_process=None):
    """The whole update in the background: download, hand over, exit 0."""
    version = entry['version']
    _state(state='downloading', version=version, message='Downloading %s...' % version, done=0, total=entry.get('size') or 0)
    try:
        log = install(entry, progress=lambda done, total: _state(state='downloading', version=version,
                                                                 message='Downloading %s...' % version, done=done, total=total))
    except Exception as exc:
        import pipeline_bridge as pb
        _state(state='failed', version=version, error=pb.redact_secrets(str(exc))[:300])
        return False
    _state(state='installing', version=version, log=str(log),
           message='Installing %s. QBG One closes now and starts again by itself in about a minute.' % version)
    time.sleep(1.5)
    (exit_process or (lambda: os._exit(0)))()
    return True


def start_update(entry=None):
    """Begin an update in a thread; refuses a second one while one is going."""
    checked = _json(CHECKED)
    entry = entry or checked.get('latest')
    if not entry or not is_newer(entry.get('version'), current_version()):
        raise ValueError('There is no newer version to install. Press Check for updates first.')
    if not installed_root():
        raise ValueError('This QBG One runs from source, so it cannot install over itself.')
    with _lock:
        state = _json(STATE)
        if state.get('state') in ('downloading', 'installing') and time.time() - float(state.get('at') or 0) < 900:
            raise ValueError('An update is already in progress.')
        _state(state='downloading', version=entry['version'], message='Starting the download...', done=0, total=entry.get('size') or 0)
    thread = threading.Thread(target=run_update, args=(entry,), daemon=True, name='studio-update')
    thread.start()
    return thread


def settle_state():
    """What the last update left behind, read against what is running now."""
    state = _json(STATE)
    if not state:
        return {}
    current = current_version()
    if state.get('state') == 'installing':
        if state.get('version') == current:
            state = _state(state='done', version=current, message='Updated to %s.' % current)
        else:
            result = Path(str(state.get('log') or '') + '.result')
            if result.is_file():
                code = result.read_text(encoding='utf-8', errors='replace').strip()
                if code != 'exit 0':
                    state = _state(state='failed', version=state.get('version'),
                                   error='The installer ended with %s. Its log: %s' % (code, state.get('log')))
            elif time.time() - float(state.get('at') or 0) > 900:
                state = _state(state='failed', version=state.get('version'),
                               error='The update did not finish. Run the installer from the release folder by hand.')
    return state


def status():
    checked = _json(CHECKED)
    current = current_version()
    latest = checked.get('latest')
    return dict(current=current, installed=bool(installed_root()), latest=latest,
                available=bool(latest and is_newer(latest.get('version'), current)),
                notes=note_lines((latest or {}).get('notes')),
                checked_at=checked.get('checked_at'), error=checked.get('error', ''),
                state=settle_state(), source=source(), last_error=db.get_setting(ERROR, ''))


# ------------------------------------------------------------ publishing --
def _upload_problem(reply):
    text = reply.text[:400]
    if 'storageQuotaExceeded' in text or 'storage quota' in text.lower():
        return ('Google gives a service account no storage of its own, so it cannot put a file into a My Drive folder. '
                'Drop the file into the folder by hand, or move the folder into a Shared drive with the service '
                'account as a member and publish again.')
    if reply.status_code == 404:
        return 'Drive cannot see the release folder. Share it with the service account as Editor (HTTP 404).'
    return 'Drive refused the upload (HTTP %d): %s' % (reply.status_code, text[:200])


def upload_installer(installer, version, folder, notes='', drive=None):
    """The installer into the release folder under the name every Studio looks for."""
    installer = Path(installer)
    data = installer.read_bytes()
    drive = drive or drive_session('https://www.googleapis.com/auth/drive')
    meta = {'name': 'QBG_Studio_Setup_%s.exe' % version, 'parents': [drive_folder_id(folder)], 'description': str(notes or '')[:300]}
    start = drive.post(FILES.replace('/drive/v3/', '/upload/drive/v3/') + '?uploadType=resumable&supportsAllDrives=true', json=meta,
                       headers={'X-Upload-Content-Type': 'application/octet-stream', 'X-Upload-Content-Length': str(len(data))},
                       timeout=60)
    if start.status_code != 200:
        raise PermissionError(_upload_problem(start))
    put = drive.put(start.headers['Location'], data=data, headers={'Content-Type': 'application/octet-stream'}, timeout=1800)
    if put.status_code not in (200, 201):
        raise PermissionError(_upload_problem(put))
    file_id = put.json()['id']
    return dict(file_id=file_id, link='https://drive.google.com/file/d/%s/view' % file_id,
                sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def publish(installer, version, folder, notes='', drive=None):
    """What `build_team_installer.py --publish` does after building."""
    return upload_installer(installer, version, folder, notes=notes, drive=drive)
