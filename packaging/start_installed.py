"""Start an installed NoteCraft (copied into _internal/app by the build).

Adapted from QBG One's start_installed.py. The desktop launcher runs

    _internal/python/python.exe -u _internal/app/start_installed.py [--check]

NoteCraft's own libraries live in _internal/libs/notecraft; each child-process
engine has its own folder there too (engine_boot.py), because the engines'
libraries do not agree on versions. Notes, runs and settings live in
~/.note_studio, never in the install folder, so an update cannot touch them.
"""
import os
import site
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
LIBS = APP.parent / 'libs'


def use_libraries(name):
    """One library folder in front of everything else (its .pth files included)."""
    folder = LIBS / name
    before = list(sys.path)
    site.addsitedir(str(folder))
    added = [p for p in sys.path if p not in before]
    sys.path[:] = added + before


def prepare():
    use_libraries('notecraft')
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    os.chdir(APP)
    from note_studio import app as studio     # creates the database and seeds the categories
    return studio


if __name__ == '__main__':
    studio = prepare()
    if '--check' in sys.argv:
        from note_studio import db, engine_catalog, paths, version
        with db.connect() as con:
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        engines = version._release().get('engines', ('shortnotes', 'upsc', 'pw_books', 'notes_qc'))
        assert engines and 'shortnotes' in engines, engines
        for engine in engines:
            assert engine_catalog.engine_dir(engine).is_dir(), engine
            assert engine == 'shortnotes' or (LIBS / engine).is_dir(), engine
        assert paths.libs_dir() == LIBS, paths.libs_dir()
        print('NOTECRAFT_CHECK_OK', flush=True)
    else:
        from note_studio import launch
        sys.exit(launch.main())
