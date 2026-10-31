"""A short "What's new" tour after an update (the operator's request, 3 October 2026).

"When we give a new update, the first time give them a short tour of the
update and tell them about the new feature, for one or two times, so they are
aware of it" - Smart Queue first, then the rest of the release.

A tour is shown on the first page Studio opens after it starts, at most
SHOWINGS times in all (one Studio start each): "Got it" ends it for good,
closing it any other way lets it come back once more on the next start. It can
be opened again at any time from "What's new" under the version in the side
menu. Each tour has its own id, so the next release's tour is shown even to
someone who finished this one.
"""
import json
import time
import uuid

import db

SHOWINGS = 2
STATE = 'whats_new_state'            # {tour id: {shown, boots, done, at}}
BOOT = uuid.uuid4().hex              # this Studio start: a tour is shown once per start

# The newest tour first. Each step: title, a short text, and where "Show me" goes.
TOURS = [
    dict(id='2026-10-04-qbg-one', title="What's new", steps=[
        dict(title='QBG Studio is now QBG One', href='/dashboard', icon='sparkles',
             text='Same app, same work, a new name and a calmer look: fewer words on every screen, one clear button '
                  'to press, and a menu grouped by what you came to do.'),
        dict(title='Search questions', href='/search', icon='search',
             text='Find questions on QBG by their words or by their IDs, just as on QBG itself, then send them '
                  'straight to Tag, Translate, Solutions or Verify.'),
        dict(title='How this works', href='/new/any-format', icon='help',
             text='Every main screen has a small "How this works" button at the top. It opens the first time you '
                  'visit a screen, then stays out of your way.'),
        dict(title='Smart Queue', href='/work-queue', icon='clock',
             text='Still here: start as many papers and tasks as you like. Whatever cannot start yet waits in '
                  'Smart Queue and starts by itself.'),
    ]),
    dict(id='2026-10-03-smart-queue', title="What's new in QBG One", steps=[
        dict(title='Smart Queue', href='/work-queue', icon='layers',
             text='Start as many papers and tasks as you like. Whatever cannot start yet waits in Smart Queue and '
                  'starts by itself, in the order you added it. Papers being read and work on QBG run side by '
                  'side, each one at a time. Find it in the side menu.'),
        dict(title='Many papers in one go', href='/new/any-format', icon='plus',
             text='On New batch or Autopilot, choose all the files and tick "Each file is a separate paper". '
                  'Every file becomes its own batch, and they are read one after another.'),
        dict(title='No more "portal is busy"', href='/work-queue', icon='check-circle',
             text='Question creation, translation, tagging, solutions, video tagging, Verify, the bots and every '
                  'Upload or Retry button join the queue instead of being refused. Every task page has a '
                  '"Start another" button.'),
        dict(title='English and Hindi under one QBG ID', href='/new/any-format', icon='languages',
             text='On New batch, tick "Add the same paper in another language" to upload a paper and its Hindi '
                  'version as one question each. Questions already on QBG can take the Hindi paper\'s text too.'),
        dict(title='Hindi content in the English language', href='/new/any-format', icon='languages',
             text='Choose English for a paper written in Hindi and it is read as it is. Before upload you are asked '
                  'to confirm, or to switch the language to Hindi.'),
        dict(title='Faster, and it does not stop', href='/video-tagging', icon='sparkles',
             text='Video tagging and tagging from an Excel sheet work on 5 IDs at a time. Automatic translation '
                  'uploads every draft and lists anything worth a second look on the run page afterwards.'),
    ]),
]


def _load():
    try:
        value = json.loads(db.get_setting(STATE, '') or '{}')
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save(value):
    db.set_setting(STATE, json.dumps(value))


def current():
    return TOURS[0] if TOURS else None


def state(mark=True):
    """What the page needs on load: the tour to show now, or none.

    mark: count this as one showing (once per Studio start)."""
    tour = current()
    if not tour:
        return dict(show=False)
    saved = _load()
    entry = saved.get(tour['id']) or {}
    shown = int(entry.get('shown') or 0)
    boots = list(entry.get('boots') or [])
    show = not entry.get('done') and (BOOT in boots or shown < SHOWINGS)
    if show and mark and BOOT not in boots:
        entry.update(shown=shown + 1, boots=(boots + [BOOT])[-SHOWINGS:], at=time.time())
        saved[tour['id']] = entry
        _save(saved)
    return dict(show=show, id=tour['id'], title=tour['title'], steps=tour['steps'],
                last=bool(show and int(entry.get('shown') or 0) >= SHOWINGS))


def finish(tour_id, done=True):
    """'Got it' (done) ends the tour for good; closing it lets it come back once more."""
    saved = _load()
    entry = saved.get(tour_id) or {}
    if done:
        entry.update(done=True, at=time.time())
    else:
        entry['closed'] = time.time()
    saved[tour_id] = entry
    _save(saved)
