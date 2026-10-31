"""Start one child-process engine on its own libraries (installed NoteCraft only).

    python.exe engine_boot.py <engine> <script.py> [args...]     run the engine's script
    python.exe engine_boot.py <engine> --probe <module> [...]    import modules, print what is missing

On a computer with the original apps, each engine runs on the original's own
virtual environment. An installed NoteCraft has none: it carries every engine's
libraries, exactly as the original's environment has them, in
_internal/libs/<engine>, and this puts that folder first on the path before the
engine's own engine_main.py runs, unchanged.
"""
import os
import runpy
import site
import sys
from pathlib import Path

LIBS = Path(__file__).resolve().parent.parent / 'libs'


def use_libraries(name):
    folder = LIBS / name
    if not folder.is_dir():
        raise SystemExit(f'The libraries for {name} are missing from this install ({folder}). Reinstall NoteCraft.')
    before = list(sys.path)
    site.addsitedir(str(folder))
    added = [p for p in sys.path if p not in before]
    sys.path[:] = added + before


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    engine, rest = argv[0], argv[1:]
    use_libraries(engine)
    if rest[0] == '--probe':
        import importlib
        sys.path.insert(0, os.getcwd())          # the engine's own modules (src.*), run from its folder
        missing = []
        for name in rest[1:]:
            try:
                importlib.import_module(name)
            except Exception as exc:      # noqa: BLE001 - every failure is reported
                missing.append(f'{name} ({type(exc).__name__}: {exc})'[:300])
        print('MISSING|' + '||'.join(missing) if missing else 'PROBE_OK', flush=True)
        return 1 if missing else 0
    script = Path(rest[0]).resolve()
    sys.argv = [str(script), *rest[1:]]
    sys.path.insert(0, str(script.parent))
    os.chdir(script.parent)
    runpy.run_path(str(script), run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
