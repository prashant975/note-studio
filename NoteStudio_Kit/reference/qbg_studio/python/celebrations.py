"""Small, kind moments: a welcome, a greeting, and a quiet cheer when work lands.

The numbers are real. Uploaded = questions actually created on QBG
(create_events, outcome 'created'); translated = bulk-translation items that
reached the portal; the streak = distinct days this Studio ran a job; time
saved = a plain estimate from those counts. Nothing here writes to QBG or
sends anything; it only reads counts and remembers, in the settings table,
which cheers have already been shown so none repeats.

The browser decides WHEN to show a cheer (never over a running job or a form
being typed in); this module only decides WHAT is worth celebrating.
"""
import json
import time

import db

# Estimated manual time, the operator's baseline: ~1.5 h per 45 questions to
# upload, and ~1.5 h per 45 to translate.
HOURS_PER_QUESTION_UPLOAD = 1.5 / 45
HOURS_PER_QUESTION_TRANSLATE = 1.5 / 45
UPLOAD_MILESTONES = (100, 500, 1000, 5000)
HOURS_MILESTONES = (10, 50, 100)
STREAK_MILESTONES = (5, 10, 30)
BIG = {'uploaded_1000', 'uploaded_5000', 'hours_100', 'streak_30'}

ON = 'celebrations_on'
LANGUAGE = 'celebrations_language'
WELCOMED = 'celebrations_welcomed'
SHOWN = 'celebrations_shown'              # JSON list of milestone ids already shown
JOB_MARK = 'celebrations_job_mark'        # highest finished job id already celebrated
SEEDED = 'celebrations_seeded'

# Which finished job kinds become a completion cheer, and the cheer's kind.
JOB_CHEERS = {
    'create': 'batch_uploaded',
    'bulk-translation': 'translation_done',
    'bulk-translation-write': 'translation_done',
    'translation-run': 'translation_done',
    'tagging': 'tagging_done',
    'tagging-write': 'tagging_done',
    'sheet-tagging': 'tagging_done',
}


def _get(key, default=''):
    return db.get_setting(key, default)


def _set(key, value):
    db.set_setting(key, value)


def enabled():
    return _get(ON, '1') != '0'


def language():
    return _get(LANGUAGE, 'english') or 'english'


def _shown():
    try:
        return set(json.loads(_get(SHOWN, '[]')) or [])
    except (ValueError, TypeError):
        return set()


def _mark_shown(ids):
    if ids:
        _set(SHOWN, json.dumps(sorted(_shown() | set(ids))))


def _count(con, sql):
    """A count, or 0 when the table does not exist yet (a module that creates
    it has not initialized). Never lets a missing table break a greeting."""
    import sqlite3
    try:
        row = con.execute(sql).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0) if row else 0


def totals(con):
    """The live counts the cheers are built from."""
    uploaded = _count(con, "SELECT COUNT(*) FROM create_events WHERE outcome='created'")
    translated = _count(con, "SELECT COUNT(*) FROM bulk_translation_items WHERE state='uploaded'")
    hours = uploaded * HOURS_PER_QUESTION_UPLOAD + translated * HOURS_PER_QUESTION_TRANSLATE
    days = _count(con, "SELECT COUNT(DISTINCT date(started_at, 'unixepoch', '+05:30')) "
                       "FROM jobs WHERE started_at IS NOT NULL")
    return {'uploaded': uploaded, 'translated': translated, 'hours': hours, 'days': days}


def _earned(counts):
    """Every milestone id the counts have reached, id -> tokens for the message."""
    earned = {}
    for n in UPLOAD_MILESTONES:
        if counts['uploaded'] >= n:
            earned['uploaded_%d' % n] = {'count': n}
    for h in HOURS_MILESTONES:
        if counts['hours'] >= h:
            earned['hours_%d' % h] = {'hours': h}
    for d in STREAK_MILESTONES:
        if counts['days'] >= d:
            earned['streak_%d' % d] = {'days': d}
    return earned


def initialize():
    """Seed the "already shown" set once, so an existing Studio does not fire a
    flood of old milestones, and start the job mark at the newest finished job
    so history is not replayed."""
    con = db.connect()
    try:
        if _get(SEEDED, '') != '1':
            con_counts = totals(con)
            _mark_shown(list(_earned(con_counts)))
            top = con.execute("SELECT COALESCE(MAX(id), 0) FROM jobs WHERE state='done'").fetchone()[0]
            _set(JOB_MARK, str(int(top or 0)))
            if con_counts['uploaded'] > 0:
                _set(WELCOMED, '1')      # not a first-ever launch
            _set(SEEDED, '1')
    finally:
        con.close()


def _completion_cheers(con):
    """Cheers for jobs that finished since the last check, marking the point so
    each finish is celebrated once. Real count and time for a batch upload."""
    try:
        mark = int(_get(JOB_MARK, '0') or 0)
    except ValueError:
        mark = 0
    rows = con.execute(
        "SELECT id, kind, state, done_n, started_at, finished_at FROM jobs "
        "WHERE id > ? AND state='done' ORDER BY id", (mark,)).fetchall()
    cheers, highest = [], mark
    uploaded_before = _count(con, "SELECT COUNT(*) FROM create_events WHERE outcome='created'")
    for row in rows:
        highest = max(highest, row['id'])
        kind = JOB_CHEERS.get(row['kind'])
        if not kind:
            continue
        tokens = {}
        if kind == 'batch_uploaded':
            count = int(row['done_n'] or 0)
            if count <= 0:
                continue                 # nothing created; not a cheer
            tokens['count'] = count
            if row['started_at'] and row['finished_at']:
                tokens['seconds'] = max(1, int(row['finished_at'] - row['started_at']))
            # The very first upload ever gets its own, warmer message.
            if uploaded_before <= count and 'first_upload' not in _shown():
                cheers.append({'id': 'first_upload', 'kind': 'first_upload', 'size': 'big', 'tokens': tokens})
                _mark_shown(['first_upload'])
        cheers.append({'id': 'job_%d' % row['id'], 'kind': kind, 'size': 'small', 'tokens': tokens})
    if highest != mark:
        _set(JOB_MARK, str(highest))
    return cheers


def pending(con=None):
    """Everything worth celebrating right now that has not been shown before:
    newly-earned milestones plus completions since the last check. Milestones
    are marked shown here (never twice); completions are marked by the job
    mark. Returns a list of {id, kind, size, tokens}."""
    own = con is None
    con = con or db.connect()
    try:
        counts = totals(con)
        events = _completion_cheers(con)
        shown = _shown()
        fresh = []
        for mid, tokens in sorted(_earned(counts).items()):
            if mid not in shown:
                kind = mid.rsplit('_', 1)[0]                       # uploaded / hours / streak
                events.append({'id': mid, 'kind': 'milestone_' + kind,
                               'size': 'big' if mid in BIG else 'small', 'tokens': tokens})
                fresh.append(mid)
        _mark_shown(fresh)
        return events
    finally:
        if own:
            con.close()


def operator_name():
    """A first name for the greeting, or '' when there is nothing name-like."""
    import pipeline_bridge as pb
    email = ''
    try:
        email = pb.pw_auth.current_email()
    except Exception:
        email = ''
    local = str(email or '').split('@')[0]
    token = local.replace('_', '.').split('.')[0]
    token = ''.join(ch for ch in token if ch.isalpha())
    if len(token) < 2:
        return ''
    return token[:1].upper() + token[1:].lower()


def state():
    """What the browser needs on load: the switches, the operator's name,
    whether this is a first-ever launch, and any cheers waiting."""
    if not enabled():
        return {'enabled': False, 'language': language(), 'welcomed': True, 'name': '', 'events': []}
    con = db.connect()
    try:
        first = _get(WELCOMED, '') != '1'
        events = pending(con)
        return {'enabled': True, 'language': language(), 'welcomed': not first,
                'name': operator_name(), 'events': events, 'totals': totals(con)}
    finally:
        con.close()


def mark_welcomed():
    _set(WELCOMED, '1')


def save_settings(on, lang):
    _set(ON, '1' if on else '0')
    _set(LANGUAGE, 'hinglish' if str(lang).lower() == 'hinglish' else 'english')
