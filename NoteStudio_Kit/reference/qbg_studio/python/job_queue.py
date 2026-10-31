"""One line for papers and tasks: each starts when the one before it has finished.

The operator's request of 3 October 2026: "add multiple papers at once and
then run one time, then one by one all start reading and move ahead to
complete the task" - in New batch, in Autopilot and in the other tasks, and
"each queue is a different batch": nothing that waits here is merged with
anything else.

What waits is the request itself: its settings and its files, kept in a folder
of its own under data/queue. Nothing is created for it - no batch, no run, no
job - until its turn comes, so removing it leaves nothing behind. When its turn
comes its launcher does exactly what the screen's own Start does.

Rules:

- One at a time, in the order added. A task that writes to QBG also waits for
  the portal to be free, as every such task already does.
- A paper that fails, or cannot start, is reported on the card and the line
  moves on.
- Pressing Stop on the task that is running holds the line: the next one does
  not start by itself over the operator's Stop. So does a sign-in that has
  expired (the next papers would only fail the same way), and a restart of
  Studio. "Continue the queue" lets it go on.
"""
import json
import shutil
import threading
import time
import uuid
from collections import namedtuple
from pathlib import Path

import db
import pipeline_bridge as pb

SCHEMA = '''
CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    folder TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'waiting',
    job_ids TEXT NOT NULL DEFAULT '[]',
    link TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
'''
# waiting -> running -> done; 'failed' could not start; 'removed' was taken out by the operator.
OPEN = ('waiting', 'running')
PAUSED, PAUSE_NOTE = 'job_queue_paused', 'job_queue_pause_note'
PUMP_SECONDS = 4
START_GRACE = 120         # a 'running' entry with no job after this long never started (Studio closed mid-start)

# What a launcher hands back: the jobs that mark this entry as still going, the
# page that shows it, and the long work itself (run by the caller, off the request).
# No job ids: the entry was finished by starting it (note says what happened).
Started = namedtuple('Started', 'job_ids link work note', defaults=('',))
# already: the same request was in the line before (a second click), and that one keeps its place.
Result = namedtuple('Result', 'item_id started position error already', defaults=(False,))
LAUNCHERS = {}
_LOCK = threading.RLock()
_READY = set()
_PUMP = []


class Busy(Exception):
    """Not now - the portal was taken a moment ago. The entry keeps its place."""


# Two lines run side by side (the operator's choice, 4 October 2026): reading a
# paper uses AI and never touches QBG, so an Upload does not wait behind a
# reading, nor a reading behind an Upload. Within each line, one at a time, in
# the order added. A paper's own Upload still waits for its own reading: its
# batch is busy until then (waits()).
LANES = {'qbg': 'QBG', 'reading': 'Reading'}


def lane(kind):
    return 'qbg' if (LAUNCHERS.get(kind) or {}).get('portal', True) else 'reading'


def register(kind, launch, *, label, portal=True, ready=None):
    """launch(item) -> Started. portal: it writes to QBG, so it also waits for the
    portal to be free. ready() -> '' or why nothing of this kind can start now."""
    LAUNCHERS[kind] = dict(launch=launch, label=label, portal=portal, ready=ready)


# ------------------------------------------------------------------ storage --
def initialize():
    con = db.connect()
    try:
        con.executescript(SCHEMA)
        if 'key' not in {r['name'] for r in con.execute('PRAGMA table_info(job_queue)')}:
            con.execute("ALTER TABLE job_queue ADD COLUMN key TEXT NOT NULL DEFAULT ''")
    finally:
        con.close()
    _READY.add(str(db.DB_PATH))


def _connect():
    if str(db.DB_PATH) not in _READY:
        initialize()
    return db.connect()


def query(sql, args=(), one=False):
    con = _connect()
    try:
        rows = [_row(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return (rows[0] if rows else None) if one else rows


def _row(row):
    item = dict(row)
    if 'kind' in item:                      # a whole entry, not a one-column look
        try:
            item['params'] = json.loads(item.get('params_json') or '{}')
        except ValueError:
            item['params'] = {}
        item['label'] = (LAUNCHERS.get(item['kind']) or {}).get('label', item['kind'])
        item['lane'] = lane(item['kind'])
    return item


def _update(item_id, **values):
    con = _connect()
    try:
        con.execute('UPDATE job_queue SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                    (*values.values(), item_id))
    finally:
        con.close()


def _folder():
    return db.DB_PATH.parent / 'queue' / ('q-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])


def files(item):
    """The files this entry was added with: [(role, name, bytes)], in the order given."""
    out = []
    for entry in item['params'].get('_files') or []:
        out.append((entry['role'], entry['name'], (Path(item['folder']) / entry['file']).read_bytes()))
    return out


def _drop_files(item):
    if item.get('folder'):
        shutil.rmtree(item['folder'], ignore_errors=True)


# --------------------------------------------------------------- the pause --
def paused():
    return db.get_setting(PAUSED, '') == '1'


def pause(note):
    db.set_setting(PAUSED, '1')
    db.set_setting(PAUSE_NOTE, str(note or '')[:400])


def resume():
    db.set_setting(PAUSED, '')
    db.set_setting(PAUSE_NOTE, '')


# ------------------------------------------------------------------ adding --
def submit(kind, title, params, uploads=(), detail='', actor='', key=''):
    """Add one request to the line, and start it at once if nothing is before it.

    uploads: [(role, name, bytes)]. key: requests with the same key are one
    request - a second click while the first still waits or runs adds nothing.
    Returns Result(item_id, started, position, error, already): started is a
    Started when it began now (the caller runs its work), otherwise position is
    its place in the line; error is why it could not start."""
    if key:
        same = query("SELECT * FROM job_queue WHERE key=? AND state IN ('waiting','running') ORDER BY id LIMIT 1",
                     (key,), True)
        if same:
            if same['state'] == 'running':
                return Result(same['id'], None, 0, 'This is already running from the queue.', True)
            return Result(same['id'], None, position(same['id']), '', True)
    folder, saved = '', []
    if uploads:
        folder = _folder()
        folder.mkdir(parents=True)
        for index, (role, name, data) in enumerate(uploads):
            filename = '%d-%s' % (index, Path(str(name).replace(chr(92), '/')).name)
            (folder / filename).write_bytes(data)
            saved.append(dict(role=role, name=name, file=filename))
    stored = dict(params, _files=saved)
    con = _connect()
    try:
        item_id = con.execute(
            'INSERT INTO job_queue(kind,title,detail,params_json,folder,actor,key,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (kind, str(title)[:160], str(detail)[:300], json.dumps(stored, ensure_ascii=False), str(folder),
             actor or '', key or '', time.time())).lastrowid
    finally:
        con.close()
    began = start_next(only=item_id)
    item = query('SELECT * FROM job_queue WHERE id=?', (item_id,), True)
    if began:
        return Result(item_id, began[1], 0, '')
    if item['state'] == 'failed':
        return Result(item_id, None, 0, item['note'])
    return Result(item_id, None, position(item_id), '')


def position(item_id):
    """Its place in its own line: 1 starts next, counting the one that is running as the line's head."""
    item = query('SELECT * FROM job_queue WHERE id=?', (item_id,), True)
    if not item:
        return 0
    same = query("SELECT kind FROM job_queue WHERE state IN ('waiting','running') AND id<=?", (item_id,))
    return sum(lane(row['kind']) == item['lane'] for row in same)


# ---------------------------------------------------------------- starting --
# What a task's own start says when its portal, its batch or its bot is taken
# right now: then the entry waits for its turn instead of failing.
TAKEN = ('already running', 'already creating', 'already has a job running', 'busy with another step',
         'being created right now')


def waits(exc):
    """Is this refusal only 'not now'?"""
    import jobs
    if isinstance(exc, (Busy, jobs.BatchBusyError)):
        return True
    return isinstance(exc, ValueError) and any(phrase in str(exc) for phrase in TAKEN)


def _finish(item, state, note=''):
    _update(item['id'], state=state, note=str(note or '')[:400], finished_at=time.time())
    _drop_files(item)


def settle():
    """Close the entries whose work has ended."""
    for item in query("SELECT * FROM job_queue WHERE state='running' ORDER BY id"):
        ids = json.loads(item['job_ids'] or '[]')
        if not ids:
            if time.time() - (item['started_at'] or 0) > START_GRACE:
                _finish(item, 'failed', 'Studio closed while this was starting. Add it again.')
            continue
        rows = [db.get_job(job_id) for job_id in ids]
        if any(row is not None and row['state'] in db.ACTIVE_JOB_STATES for row in rows):
            continue
        last = next((row for row in rows if row is not None), None)
        _finish(item, 'done', (last['message'] if last is not None else '') or 'Finished.')


def _heads(con, skip=()):
    """The first waiting entry of every line that is free to start one now, oldest first."""
    busy = {lane(row['kind']) for row in con.execute("SELECT kind FROM job_queue WHERE state='running'")}
    heads, seen = [], set()
    for row in con.execute("SELECT * FROM job_queue WHERE state='waiting' ORDER BY id"):
        which = lane(row['kind'])
        if which in seen:
            continue
        seen.add(which)
        if which in busy or which in skip:
            continue
        if which == 'qbg' and db._exclusive_active(con, None):
            continue                                    # QBG is busy with a job the queue did not start
        heads.append(row)
    return heads


def _claim_head(only, skip=()):
    """Take the entry at the head of a free line, if it may start now."""
    con = _connect()
    try:
        con.execute('BEGIN IMMEDIATE')
        try:
            heads = _heads(con, skip)
            head = next((h for h in heads if only is None or h['id'] == only), None)
            if head is None:
                return None
            con.execute("UPDATE job_queue SET state='running',started_at=? WHERE id=?", (time.time(), head['id']))
            return _row(head)
        finally:
            con.execute('COMMIT')
    finally:
        con.close()


def ready_heads():
    """The entries that would start now, for the readiness check before the pump starts them."""
    con = _connect()
    try:
        return [_row(h) for h in _heads(con)]
    finally:
        con.close()


def start_next(only=None):
    """Start the entry at the head of the line when nothing stands in its way.

    only: start nothing but this entry (the one a person has just added).
    Returns (item, Started) - the caller runs Started.work - or None."""
    with _LOCK:
        skip = set()
        while True:
            settle()
            if paused():
                return None
            item = _claim_head(only, skip)
            if item is None:
                return None
            launcher = LAUNCHERS.get(item['kind'])
            try:
                if launcher is None:
                    raise ValueError('This QBG One cannot start "%s" from the queue.' % item['kind'])
                started = launcher['launch'](item)
            except Exception as exc:
                if waits(exc):
                    # Its batch or the portal is taken: it keeps its place, and says why it waits.
                    _update(item['id'], state='waiting', started_at=None, note='Waiting: ' + str(exc)[:300])
                    if only is not None:
                        return None
                    skip.add(lane(item['kind']))        # the other line may still start
                    continue
                # It cannot start: say why on the card, and the line moves on.
                _finish(item, 'failed', pb.redact_secrets(str(exc)) or exc.__class__.__name__)
                if only is not None:
                    return None
                continue
            _update(item['id'], job_ids=json.dumps([int(j) for j in started.job_ids]), link=started.link or '', note='')
            if started.job_ids:
                _drop_files(item)
            else:
                _finish(item, 'done', started.note or 'Finished.')
            return item, started


# ------------------------------------------------------------------ the card --
def overview():
    """What the Queue card shows."""
    items = query("SELECT * FROM job_queue WHERE state IN ('waiting','running') ORDER BY id")
    recent = query("SELECT * FROM job_queue WHERE state IN ('done','failed') AND finished_at>? ORDER BY id DESC LIMIT 6",
                   (time.time() - 12 * 3600,))
    for place, item in enumerate(items, 1):
        item['place'] = place
    waiting = [i for i in items if i['state'] == 'waiting']
    for item in items:
        item['lane_label'] = LANES[item['lane']]
    return dict(line=items, waiting=waiting, running=[i for i in items if i['state'] == 'running'], recent=recent,
                paused=paused() and bool(waiting), note=db.get_setting(PAUSE_NOTE, ''), active=bool(items))


def outcome(results, form_url, noun='paper'):
    """After adding: where the person goes and what they are told - (url, message, kind).
    The caller runs the work of every result that started."""
    started = [r for r in results if r.started]
    waiting = [r for r in results if not r.started and not r.error]
    errors = [r.error for r in results if r.error]
    plural = lambda n: '%d %s%s' % (n, noun, '' if n == 1 else 's')            # noqa: E731
    said, kind = [], 'info'
    if started and waiting:
        said.append('%s more in the queue; each starts by itself when the one before it has finished.'
                    % plural(len(waiting)))
    elif waiting:
        said.append('%s added to the queue (place %d). %s by itself when the work before it has finished.'
                    % (plural(len(waiting)), waiting[0].position, 'It starts' if len(waiting) == 1 else 'Each starts'))
    if errors:
        said.append(errors[0] if len(results) == 1 else
                    '%s could not be started: %s' % (plural(len(errors)), errors[0]))
        kind = 'danger' if not started and not waiting else 'warn'
    if waiting and all(r.already for r in waiting) and not started:
        said = ['This is already in the queue (place %d). It starts by itself when the work before it has finished.'
                % waiting[0].position]
    return (started[0].started.link if started else form_url), ' '.join(said), kind


def go(path, message='', kind='info'):
    """A redirect that carries a one-line message for the page's toast (as app._go does)."""
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    if message:
        path += ('&' if '?' in path else '?') + 'toast=%s&kind=%s' % (quote(message), quote(kind))
    return RedirectResponse(path, 303)


def reply(results, background, go=None, form_url='/', noun='paper'):
    """The answer to a Start: run what began now, and send the person to it - or back
    to the form with its place in the queue."""
    from fastapi.responses import RedirectResponse
    go = go or globals()['go']
    for result in results:
        if result.started and result.started.work:
            background.add_task(result.started.work)
    url, message, kind = outcome(results, form_url, noun)
    return go(url, message, kind) if message else RedirectResponse(url, 303)


def action(kind, label, launch, *, portal=True, ready=None):
    """Register a task whose start is launch(*args, **kwargs) -> Started: it claims the
    portal itself, exactly as its button always did. A refusal that only means
    "taken right now" (waits) keeps the entry waiting; any other refusal is
    shown on the card as the reason it could not start."""
    def start(item):
        params = item['params']
        return launch(*params.get('args') or [], **dict(params.get('kwargs') or {}))
    register(kind, start, label=label, portal=portal, ready=ready)


def pw_ready():
    """'' when the PW sign-in works; otherwise why the queue holds."""
    try:
        state = pb.pw_state()
    except Exception:
        return ''
    return '' if state.get('usable') else 'The PW sign-in is needed. Sign in on Settings, then continue the queue.'


def qbg_ready():
    """'' when QBG can take a save (the PW sign-in and the QBG session); otherwise why the queue holds."""
    why = pw_ready()
    if why:
        return why
    try:
        auth = pb.token_status()
        if not auth['ok'] and auth.get('source') == 'chrome':
            auth = pb.harvest_token_check()
    except Exception:
        return ''
    return '' if auth.get('ok') else ('The QBG session is needed: open the portal browser and sign in to QBG, '
                                      'then continue the queue.')


def task(kind, label, create, run, page, ready=None):
    """Register a task that starts with create(*args, **kwargs) -> run id and then run(run id):
    the shape of bulk translation, solutions, tagging, video tagging and verifying."""
    def launch(*args, **kwargs):
        run_id = create(*args, **kwargs)
        return Started([run_id], page % run_id, lambda: run(run_id))
    action(kind, label, launch, ready=ready or pw_ready)


def submit_task(kind, title, args, kwargs=None, detail='', actor='', key=''):
    return submit(kind, title, dict(args=list(args), kwargs=kwargs or {}), detail=detail, actor=actor, key=key)


def busy():
    """Is anything in the line? A new request then waits behind it."""
    return bool(query("SELECT id FROM job_queue WHERE state IN ('waiting','running') LIMIT 1"))


def remove(item_id):
    """Take a waiting entry out of the line. One that has started is stopped on its own page."""
    item = query('SELECT * FROM job_queue WHERE id=?', (item_id,), True)
    if not item or item['state'] != 'waiting':
        raise ValueError('Only a paper that is still waiting can be removed from the queue.')
    con = _connect()
    try:
        done = con.execute("UPDATE job_queue SET state='removed',finished_at=? WHERE id=? AND state='waiting'",
                           (time.time(), item_id)).rowcount
    finally:
        con.close()
    if not done:
        raise ValueError('That paper has just started; stop it on its own page.')
    _drop_files(item)
    return item


def clear():
    """Remove everything that is still waiting. Returns how many."""
    count = 0
    for item in query("SELECT * FROM job_queue WHERE state='waiting' ORDER BY id"):
        try:
            remove(item['id'])
            count += 1
        except ValueError:
            pass
    return count


def note_stop(job_id):
    """The operator pressed Stop on a job: if the line started it, the line holds."""
    try:
        for item in query("SELECT * FROM job_queue WHERE state='running'"):
            if int(job_id) in json.loads(item['job_ids'] or '[]') and query(
                    "SELECT id FROM job_queue WHERE state='waiting' LIMIT 1"):
                pause('You stopped "%s", so the next one was not started. Continue the queue when you are ready.'
                      % item['title'])
    except Exception:
        pass                    # a Stop is never held up by the queue


def recover():
    """After a restart: close what ended, and hold what waits until the operator says go on."""
    try:
        for item in query("SELECT * FROM job_queue WHERE state='running'"):
            if not json.loads(item['job_ids'] or '[]'):
                _finish(item, 'failed', 'Studio closed while this was starting. Add it again.')
        settle()
        if query("SELECT id FROM job_queue WHERE state='waiting' LIMIT 1") and not paused():
            pause('Studio was restarted. Check that you are signed in, then continue the queue.')
    except Exception:
        pass


# ------------------------------------------------------------------ the pump --
def _run(started):
    try:
        if started.work:
            started.work()
    except Exception as exc:
        import jobs
        for job_id in started.job_ids:
            row = db.get_job(job_id)
            if row is not None and row['state'] in db.ACTIVE_JOB_STATES:
                jobs._finish(job_id, False, 'Stopped by an error.', pb.redact_secrets(str(exc))[:600])


def tick():
    """One look at the lines: start the next entry of every line whose turn has come.
    Returns the first one started (item, Started), or None."""
    began = []
    with _LOCK:
        settle()
        if paused():
            return None
        heads = ready_heads()
        if not heads:
            return None
        for head in heads:
            ready = (LAUNCHERS.get(head['kind']) or {}).get('ready')
            why = ready() if ready else ''
            if why:
                pause(why)
                return None
        while True:
            one = start_next()
            if not one:
                break
            began.append(one)
    for item, started in began:
        threading.Thread(target=_run, args=(started,), daemon=True, name='queue-%d' % item['id']).start()
    return began[0] if began else None


def start_pump():
    """The background look at the line, every few seconds, for as long as Studio runs."""
    if _PUMP:
        return

    def loop():
        while True:
            time.sleep(PUMP_SECONDS)
            try:
                tick()
            except Exception:
                pass
    thread = threading.Thread(target=loop, daemon=True, name='job-queue')
    _PUMP.append(thread)
    thread.start()
