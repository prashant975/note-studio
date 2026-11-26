"""QBG One's background update delivery, adapted for NoteCraft's data and queue.

Source: QBG_Complete_Toolkit/QBG_Studio_Vibecraft/backend/studio/
qbg_studio/update_delivery.py, read only, 8 October 2026.
"""
import hashlib
from pathlib import Path
import threading
import time

from . import job_queue, paths, updater

_lock = threading.Lock()
_thread = None
_poll_thread = None
POLL_SECONDS = 300
SIGNIN_WAIT = 30
_stop = threading.Event()


def setup_path(entry):
    version, digest = str(entry.get('version','')), str(entry.get('sha256',''))
    if not updater.parse_version(version) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('The update does not have a verified release reference yet.')
    updater.require_edition(entry)
    return paths.home()/'update_downloads'/(updater.SETUP+'_'+version+'_'+digest[:16]+updater.KIND)


def verified(entry, path):
    path = Path(path)
    if not path.is_file() or not 2 <= path.stat().st_size <= updater.MAX_BYTES:
        return False
    if entry.get('size') and path.stat().st_size != int(entry['size']):
        return False
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        if handle.read(2)!=updater.MAGIC:   # a Windows installer, or the Mac app's zip
            return False
        handle.seek(0)
        for chunk in iter(lambda:handle.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()==entry.get('sha256')


def prepare(entry, drive=None, progress=None):
    """A partial or changed file is never handed to an installer."""
    target = setup_path(entry)
    if verified(entry,target):
        return target
    target.parent.mkdir(parents=True,exist_ok=True)
    partial = target.with_suffix('.part')
    try:
        updater.download(entry,partial,drive=drive,progress=progress)
        if not verified(entry,partial):
            raise ValueError('This file is not a verified ' + ('Mac update.' if updater.MAC else 'Windows installer.'))
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def run_download(entry, drive=None):
    version = entry['version']
    try:
        setup = prepare(entry,drive=drive,progress=lambda done,total:updater._state(
            state='downloading',version=version,automatic=True,done=done,total=total,
            message='Downloading the update in the background. Your work can continue.'))
        updater._state(state='ready',version=version,automatic=True,setup=str(setup),
                       sha256=entry['sha256'],message='Update downloaded and checked. Click Install update when your work is finished.')
        return True
    except Exception as exc:
        updater._state(state='failed',version=version,automatic=True,error=updater.redact(exc)[:300])
        return False


def start_download(entry):
    global _thread
    if updater._offline() or not entry or not updater.is_newer(entry.get('version'),updater.current_version()) or not updater.installed_root():
        return False
    setup = setup_path(entry)
    with _lock, updater._lock:
        if _thread and _thread.is_alive():
            return False
        state = updater.settle_state()
        if state.get('state')=='installing':
            return False
        if state.get('state')=='downloading' and not state.get('automatic') and time.time()-float(state.get('at',0))<900:
            return False
        if state.get('state')=='ready' and state.get('version')==entry['version'] and state.get('sha256')==entry['sha256'] and setup.is_file():
            return True
        if state.get('state')=='failed' and state.get('version')==entry['version'] and time.time()-float(state.get('at',0))<POLL_SECONDS:
            return False
        updater._state(state='downloading',version=entry['version'],automatic=True,done=0,total=entry.get('size',0),
                       message='Downloading the update in the background. Your work can continue.')
        _thread = threading.Thread(target=run_download,args=(dict(entry),),name='update-download',daemon=True)
        _thread.start()
        return True


def installation_problem():
    """Hold a ready update while a worker or a queued task is starting."""
    if updater.making_notes():
        return 'Notes are being made right now. The update is downloaded and will wait until they finish.'
    if job_queue.counts()['running']:
        return 'Finish the Smart Queue task before installing. The update will wait.'
    return ''


def start_poller():
    global _poll_thread
    if updater._offline() or not updater.installed_root():
        return
    if _poll_thread and _poll_thread.is_alive():
        return
    _stop.clear()
    def loop():
        while not _stop.is_set():
            wait = POLL_SECONDS
            try:
                if updater.installed_root() and updater.source()['folder_id']:
                    result = updater.check()
                    if result.get('available'):
                        start_download(result['latest'])
                    if result.get('error') == updater.NOT_SIGNED_IN:
                        wait = SIGNIN_WAIT   # at start-up the sign-in comes back a few seconds later
            except Exception:
                pass  # The status screen shows the check/download error and the next check retries.
            _stop.wait(wait)
    _poll_thread = threading.Thread(target=loop,name='automatic-updates',daemon=True)
    _poll_thread.start()


def stop_poller():
    _stop.set()
