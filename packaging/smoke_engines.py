"""One mock run per child engine on a packaged NoteCraft (the build's check; doc 06, Phase 9).

    <package>/_internal/python/python.exe smoke_engines.py <package>/_internal/app <work> <lecture.pdf> <qc_sample.pdf>

Runs with NoteCraft's packaged libraries, through NoteCraft's own adapters and
engine_host, so each engine starts the way it does for a teammate: the bundled
Python, engine_boot.py and the engine's own library folder. Mock mode: no
sign-in, no network, no cost (the same requests the tests make).
"""
import hashlib
import json
import os
import shutil
import site
import sys
from pathlib import Path

APP, WORK, LECTURE, SAMPLE = (Path(a) for a in sys.argv[1:5])
LIBS = APP.parent / 'libs'
URL = 'https://www.youtube.com/watch?v=abcdefghijk'
TRANSCRIPT = ('Namaskar students, aaj hum Indian Polity ke andar Fundamental Rights, Article 21 aur judicial review '
              'par baat karenge. Article 32 ko Dr Ambedkar ne heart and soul of the Constitution kaha tha. Writs paanch '
              'types ke hote hain: habeas corpus, mandamus, prohibition, certiorari aur quo warranto. ') * 4

before = list(sys.path)
site.addsitedir(str(LIBS / 'notecraft'))
sys.path[:] = [p for p in sys.path if p not in before] + before
sys.path.insert(0, str(APP))
os.environ['TRANSCRIPTION_FALLBACK'] = 'false'        # the tests' setting: no local transcriber probe in mock mode

from note_studio import db, engine_catalog, engine_host, paths  # noqa: E402
from note_studio.engines import notes_qc, pw_books, upsc  # noqa: E402

assert paths.libs_dir() == LIBS, paths.libs_dir()
db.init()
found = {}


def run(engine, folder, request):
    events = []
    result = engine_host.run(engine, 1, folder, request, events.append)
    done = next((e for e in events if e.get('event') == 'done'), {})
    found[engine] = {'status': result['status'], 'reason': result['reason'][:300],
                     'outputs': [e.get('label') for e in events if e.get('event') == 'output'],
                     'tail': result['tail'][-8:] if result['status'] == 'failed' else [],
                     'run_dir': done.get('run_dir', '')}


def inputs(name, file_name):
    folder = WORK / f'mock {name}'
    (folder / 'input').mkdir(parents=True)
    shutil.copy2(LECTURE, folder / 'input' / file_name)
    return folder


# UPSC: a class link (its transcript already in the engine's own cache) and its slides.
if engine_catalog.available('upsc'):
    folder = inputs('upsc', 'Polity 07 Writs.pdf')
    cache = folder / 'engine_runs' / '_transcript_cache'
    cache.mkdir(parents=True)
    key = hashlib.sha256(f'{URL}|captions'.encode('utf-8')).hexdigest()[:32]
    (cache / f'{key}.json').write_text(json.dumps({
        'full_text': TRANSCRIPT, 'source_type': 'youtube', 'source_label': 'YouTube captions', 'language': 'hi',
        'engine': 'captions', 'duration_seconds': 600, 'gaps': [], 'segments': []}), encoding='utf-8')
    built = upsc.build_request({'note_type': 'upsc_dcn', 'category': 'upsc', 'subject': 'polity', 'language': 'english'},
                               {'video_url': URL, 'file': 'Polity 07 Writs.pdf', 'lecture_title': 'Writs', 'dcn_no': '07'},
                               folder, '', {})
    run('upsc', folder, {**built, 'mock': True, 'test_stub_pdf': True})

# PW Books: a deck.
if engine_catalog.available('pw_books'):
    folder = inputs('pw_books', 'Physics Lecture 05.pdf')
    built = pw_books.build_request({'category': 'neet_jee', 'note_type': 'pw_books_handwritten', 'subject': 'physics',
                                    'language': 'english'},
                                   {'file': 'Physics Lecture 05.pdf', 'mode': 'summary', 'exam': 'jee',
                                    'math_render_mode': 'omml', 'strict_math': True}, folder, '', {})
    run('pw_books', folder, {**built, 'mock': True, 'test_stub_pdf': True})

# Check notes: the basic check (no AI) of the QC tool's own sample.
if engine_catalog.available('notes_qc'):
    folder = WORK / 'mock notes_qc'
    folder.mkdir(parents=True)
    run('notes_qc', folder, notes_qc.build_request([SAMPLE], full=False))

print('SMOKE|' + json.dumps(found), flush=True)
