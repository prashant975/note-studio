"""Build NoteCraft's Windows installer: QBG One's build_team_installer.py, adapted.

    python packaging\\build_installer.py
    python packaging\\build_installer.py --publish <Drive folder link or id> --notes "what changed; and this"

Output, in dist\\team\\<version>\\:
    NoteCraft_Setup.exe                the installer
    NoteCraft_Setup_<version>.exe      the same file under the name every NoteCraft looks for in the
                                       release folder (updater.py)
    NoteCraft_Setup_<version>.txt      its notes, one fix per line: the update pop-up shows them
    SHA256.txt, QUICK_START.txt

As QBG One's build (QBG_Studio_Vibecraft/backend/studio/build_team_installer.py, read in place):
- the version is the date and the build of that day (build_version.py), baked into release.json
  together with the release folder (--publish), so every install looks there for updates;
- the Python runtime is a copy of the one on this computer, and the build refuses to package
  when it cannot import a module the code uses ("No module named ..." reaching the team);
- the app's Google sign-in registration goes in (never anyone's sign-in or token), checked to be
  only that; everything else secret-shaped stops the build;
- a clean start of the packaged copy (NoteCraft.exe --check, then the server and its pages) with
  an empty data folder, before the installer is made;
- --publish uploads the installer into the release folder; when that is refused, the build names
  the file to drop in by hand.

Different, because NoteCraft runs four note apps whose libraries do not agree on versions:
- _internal\\libs\\notecraft holds NoteCraft's own libraries (and the in-process Short Notes
  engine's), copied from this computer's Python; _internal\\libs\\<engine> holds each child
  engine's libraries, copied from its original's own .venv, exactly as that app runs them;
- the engine copies go in unchanged (no tests or run folders), with the UPSC DPP question bank
  (data\\) and ffmpeg (third_party\\) carried, so nothing has to be fetched.
- Notes and settings live in ~/.note_studio, outside the install folder: the installer never
  carries or replaces anyone's work.
"""
import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib import metadata
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
ROOT = PACKAGING.parent
for folder in (str(ROOT), str(PACKAGING)):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import build_version  # noqa: E402
import make_icon  # noqa: E402
from note_studio import brand, paths  # noqa: E402
from note_studio.version import setup_name  # noqa: E402

APP = brand.APP_NAME
CACHE = ROOT / '_build_cache'
WORK_ROOT = ROOT / '_build'
DIST = ROOT / 'dist' / 'team'
ENGINES = ('shortnotes', 'upsc', 'pw_books', 'notes_qc')
CHILD_ENGINES = ('upsc', 'pw_books', 'notes_qc')
APP_ID = 'AppId={{E74CCEA5-9A3A-4D46-8399-962681DC33DA}'
READY = 'NOTESTUDIO_READY|'
REGISTRATION = 'deployment/client_secret.json'
# The team's service-account key (the user's decision, 4 Oct 2026, as QBG's --team-key):
# every install then reads the release folder with nothing handed around.
TEAM_KEY = 'deployment/service_account.json'


def edition_release(release, without=()):
    engines = [e for e in ENGINES if e not in without]
    edition = '-'.join('no' + e.replace('_', '') for e in ('upsc', 'pw_books') if e in without) or 'full'
    return {**release, 'edition': edition, 'engines': engines}


def next_available_release():
    """Smaller editions retain source/full metadata; still advance past builds on disk."""
    release = build_version.next_release()
    prefix, revision = release['version'].rsplit('.', 1)
    revision = int(revision)
    if DIST.is_dir():
        for folder in DIST.iterdir():
            match = re.fullmatch(re.escape(prefix) + r'\.(\d+)(?:-[a-z-]+)?', folder.name)
            if folder.is_dir() and match:
                revision = max(revision, int(match[1])+1)
    return {**release, 'version': prefix + '.' + str(revision)}


def release_destination(release):
    edition = release.get('edition', 'full')
    suffix = '-' + edition if edition != 'full' else ''
    return DIST / (release['version'] + suffix)


def write_source_release(release):
    """A smaller build must never turn the source/full build into the other edition."""
    if release.get('edition', 'full') == 'full':
        build_version.write_release(build_version.RELEASE_FILE, release)
        build_version.RELEASE_FILE.write_bytes(build_version.RELEASE_FILE.read_bytes().replace(b'\r\n', b'\n'))

# Never in the package: tests, run folders, environments, caches, and anything with a sign-in.
EXCLUDED_DIRS = {'__pycache__', '.pytest_cache', 'tests', 'testdata', 'runs', 'out', '.venv', 'venv', '.git',
                 '.streamlit', 'logs', 'tmp', 'scripts', 'docs', 'packaging', 'dist', 'build', '_build',
                 '_build_cache', '.mypy_cache', '.ruff_cache'}
EXCLUDED_NAMES = ('.env', '.env.*', 'credentials*.json', 'token*.json', 'client_secret*.json', 'service_account*.json',
                  'sheets.json', '*.token')
EXCLUDED_SUFFIXES = {'.db', '.pyc', '.pyo', '.log', '.bak', '.sqlite', '.pending', '.spec'}
TEST_ONLY = {'pytest', 'httpx', 'pyflakes'}

# QBG One's build_package.SECRET_PATTERNS.
SECRET_PATTERNS = [
    (re.compile(rb"-----BEGIN (RSA |EC )?PRIVATE KEY-----"), "private key block"),
    (re.compile(rb"GOCSPX-[A-Za-z0-9_-]{10,}"), "Google OAuth client secret"),
    (re.compile(rb"AIza[0-9A-Za-z_-]{30,}"), "Google API key"),
    (re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\."), "JWT / bearer token"),
    (re.compile(rb"1//0[A-Za-z0-9_-]{30,}"), "Google refresh token"),
    (re.compile(rb"ya29\.[A-Za-z0-9_-]{20,}"), "Google access token"),
    (re.compile(rb"sk-[A-Za-z0-9]{32,}"), "provider API key"),
]
SCAN_SUFFIXES = {".py", ".json", ".txt", ".md", ".bat", ".html", ".js", ".css", ".cfg", ".ini", ".toml",
                 ".yaml", ".yml", ".csv", ".env", ".tmp", ".bak", ".old", ".orig"}

# Public Drive playback client identifier, verified against yt-dlp's official
# googledrive.py on 8 October 2026. Only this exact literal in this exact engine
# file is exempt; actual provider/user credentials remain rejected.
PUBLIC_LITERALS = {
    ('engines/upsc/src/video_sources/google_drive.py', 'Google API key'):
        'f5800ea77725791d39d693e48e2e34ea71f6018ea4b4ab86c5e3f387d989e1fe',
}


def say(msg):
    print('  ' + msg, flush=True)


def run(argv, timeout=3600, **kwargs):
    proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, **kwargs)
    if proc.returncode:
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
        raise SystemExit('Failed: ' + ' '.join(str(a) for a in argv[:3]))
    return proc


def _canon(name):
    return re.sub(r'[-_.]+', '-', str(name)).lower()


# ------------------------------------------------------------------- staging
def skipped(rel):
    rel = Path(rel)
    if set(rel.parts[:-1]) & EXCLUDED_DIRS:
        return True
    name = rel.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_NAMES) or Path(name).suffix in EXCLUDED_SUFFIXES


def snapshot(folder):
    return {p for p in Path(folder).rglob('*')}


def remove_new(folder, before):
    """A check that ran inside the staged copy (an engine creating its run folder on import)
    must leave nothing behind in the package."""
    for path in sorted(snapshot(folder) - before, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def copy_tree(source, target):
    """The project's files (an engine copy, the app) without tests, runs or secrets."""
    count = 0
    for path in sorted(Path(source).rglob('*')):
        rel = path.relative_to(source)
        if path.is_dir():
            continue
        if skipped(rel) or rel.parts[0] in EXCLUDED_DIRS:
            continue
        dest = Path(target) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        count += 1
    return count


def copy_runtime(target):
    """This computer's Python 3.13, without its installed packages (QBG copies its runtime too)."""
    base = Path(sys.base_prefix)
    target.mkdir(parents=True)
    for item in base.iterdir():
        if item.is_file() and (item.suffix.lower() in ('.exe', '.dll') or item.name == 'LICENSE.txt'):
            shutil.copy2(item, target / item.name)
    shutil.copytree(base / 'DLLs', target / 'DLLs', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    top_skip = {'site-packages', 'test', 'idlelib', 'turtledemo', 'ensurepip', 'tkinter'}

    def ignore(folder, names):
        top = Path(folder).resolve() == (base / 'Lib').resolve()
        return [n for n in names if n == '__pycache__' or n.endswith('.pyc') or (top and n in top_skip)]

    shutil.copytree(base / 'Lib', target / 'Lib', ignore=ignore)
    (target / 'Lib' / 'site-packages').mkdir()


def third_party_imports(files, local):
    """QBG's third_party_imports: every module imported at module level or in a function,
    except the standard library, the code's own modules, and imports guarded by try/except
    (optional by design) or under `if TYPE_CHECKING:`. Full dotted names (google.auth, not
    google): a namespace is shared by many packages."""
    names = set()
    for path in files:
        if Path(path).name == 'macos_credentials.py' and os.name == 'nt':
            continue
        tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'), str(path))
        guarded = set()
        for node in ast.walk(tree):
            typing_only = isinstance(node, ast.If) and 'TYPE_CHECKING' in ast.unparse(node.test)
            if isinstance(node, ast.Try) or typing_only:
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(inner))
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
    return sorted(n for n in names if n.split('.')[0] not in sys.stdlib_module_names
                  and n.split('.')[0] not in local and n != '__future__')


def owners_of(module, owners):
    """The distributions that provide a module; for a shared namespace (google.*), only the one
    whose files hold that subpackage."""
    top = module.split('.')[0]
    dists = owners.get(top) or []
    if len(dists) <= 1 or '.' not in module:
        return list(dists)
    parts = module.split('.')
    for depth in range(len(parts), 1, -1):
        prefix = '/'.join(parts[:depth])
        found = [d for d in dists if any(str(f).replace('\\', '/').startswith((prefix + '/', prefix + '.'))
                                         for f in metadata.distribution(d).files or [])]
        if found:
            return found
    return list(dists)


def local_modules(folder):
    folder = Path(folder)
    return {p.stem for p in folder.iterdir() if (p.is_file() and p.suffix == '.py') or (p / '__init__.py').exists()}


def code_files(folder, keep=lambda rel: True):
    folder = Path(folder)
    return [p for p in folder.rglob('*.py') if not skipped(p.relative_to(folder)) and keep(p.relative_to(folder))]


def notecraft_code():
    """NoteCraft's own code and the Short Notes engine it runs in process."""
    shortnotes = ROOT / 'engines' / 'shortnotes'
    files = code_files(ROOT / 'note_studio') + code_files(shortnotes)
    local = {'note_studio'} | local_modules(ROOT / 'note_studio') | local_modules(shortnotes)
    return files, local


def engine_code(engine_dir):
    """A child engine's run-time code: what engine_main.py reaches, not its Streamlit app or tools."""
    engine_dir = Path(engine_dir)
    files = [p for p in (engine_dir / 'engine_main.py', engine_dir / 'engine_practice.py', engine_dir / 'pw_access.py')
             if p.is_file()]
    files += code_files(engine_dir / 'src')
    if (engine_dir / 'pw-app-kit').is_dir():
        files += code_files(engine_dir / 'pw-app-kit')
    return files, local_modules(engine_dir) | {'src'}


def src_modules(engine_dir):
    """Every module of the engine's src package, as import names."""
    src = Path(engine_dir) / 'src'
    names = []
    for path in sorted(src.rglob('*.py')):
        rel = path.relative_to(src.parent).with_suffix('')
        if skipped(path.relative_to(src)):
            continue
        parts = list(rel.parts)
        if parts[-1] == '__init__':
            parts = parts[:-1]
        names.append('.'.join(parts))
    return names


def _requirement_lines(path, seen=None):
    """The requirements in a file (following -r) that apply on this computer; test-only ones left out."""
    from pip._vendor.packaging.requirements import Requirement
    seen = seen if seen is not None else set()
    found = []
    path = Path(path)
    if path in seen or not path.is_file():
        return found
    seen.add(path)
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('-r'):
            found += _requirement_lines(path.parent / line[2:].strip(), seen)
            continue
        if line.startswith('-'):
            continue
        req = Requirement(line)
        if req.marker and not req.marker.evaluate():
            continue
        if _canon(req.name) not in TEST_ONLY:
            found.append(req)
    return found


def _requirements(path):
    """Names (with extras) from a requirements file."""
    found = {}
    for req in _requirement_lines(path):
        found.setdefault(_canon(req.name), set()).update(req.extras)
    return found


def fill_declared_requirements(engine, target):
    """An engine's own requirements.txt is what its released app carries. A package it lists
    that the original's development environment lacks (the QC tool's pyspellchecker: its
    released exe bundles it, its .venv does not) is added from this computer's Python when
    the version fits. Returns what was added."""
    from pip._vendor.packaging.requirements import Requirement
    present = {_canon(d.metadata['Name']) for d in metadata.distributions(path=[str(target)])}
    added, queue = [], list(_requirement_lines(ROOT / 'engines' / engine / 'requirements.txt'))
    while queue:
        req = queue.pop()
        name = _canon(req.name)
        if name in present:
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            say(f'  {engine}: its requirements list {req}, which neither its environment nor this computer has.')
            continue
        if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
            say(f'  {engine}: its requirements list {req}; this computer has {dist.version}, which does not fit.')
            continue
        copy_distribution(dist, target)
        present.add(name)
        added.append(f'{dist.metadata["Name"]} {dist.version}')
        for text in dist.requires or []:
            dep = Requirement(text)
            if not dep.marker or dep.marker.evaluate({'extra': ''}):
                queue.append(dep)
    for line in added:
        say(f'  {engine}: added {line} (listed in its requirements.txt, missing from its environment).')
    return added


def notecraft_distributions(imports):
    """The installed distributions NoteCraft needs: its imports and requirements, with every dependency."""
    from pip._vendor.packaging.requirements import Requirement
    owners = metadata.packages_distributions()
    wanted = _requirements(ROOT / 'requirements.txt')
    unknown = []
    for name in imports:
        dists = owners_of(name, owners)
        if not dists:
            unknown.append(name)
            continue
        for dist in dists:
            wanted.setdefault(_canon(dist), set())
    if unknown:
        raise SystemExit('This computer\'s Python has no package for: ' + ', '.join(unknown) +
                         '. Install it (python -m pip install <name>) or drop the import, then build again.')
    found, queue, absent = {}, list(wanted), []
    while queue:
        name = queue.pop()
        extras = wanted.get(name, set())
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            absent.append(name)
            continue
        found[name] = dist
        for text in dist.requires or []:
            req = Requirement(text)
            if req.marker and not any(req.marker.evaluate({'extra': e}) for e in ({''} | extras)):
                continue
            dep = _canon(req.name)
            more = set(req.extras) - wanted.get(dep, set())
            if dep not in wanted or more:
                wanted.setdefault(dep, set()).update(req.extras)
                queue.append(dep)
    if absent:
        raise SystemExit('Required but not installed on this computer: ' + ', '.join(sorted(absent)))
    return found


def copy_distribution(dist, target):
    base = Path(dist.locate_file('')).resolve()
    count = 0
    for item in dist.files or []:
        source = Path(dist.locate_file(item)).resolve()
        try:
            rel = source.relative_to(base)
        except ValueError:
            continue                          # scripts and data outside site-packages
        if '__pycache__' in rel.parts or source.suffix == '.pyc' or not source.is_file():
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        count += 1
    return count


def copy_engine_libraries(engine, target, source=None):
    """The original app's own environment, exactly as it runs the original."""
    original = paths.original(engine)
    local = paths.ENGINES_DIR / engine / '.venv' / 'Lib' / 'site-packages'
    site = (Path(source) if source else local if local.is_dir()
            else original / '.venv' / 'Lib' / 'site-packages' if original else None)
    if not site or not site.is_dir():
        raise SystemExit(f'{engine}: the original app\'s environment is missing ({site}). The build copies '
                         'each engine\'s libraries from its original\'s .venv.')
    for pth in site.glob('*.pth'):
        for line in pth.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if line and not line.startswith(('#', 'import ')) and Path(line).is_absolute():
                raise SystemExit(f'{engine}: {pth.name} points outside the environment ({line}); an editable '
                                 'install cannot travel in a package.')
    shutil.copytree(site, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'pip', 'pip-*.dist-info'))


def probe(package, engine, modules, cwd, env):
    """Import the modules on the packaged runtime with the engine's libraries; [] when all import."""
    python = package / '_internal' / 'python' / 'python.exe'
    boot = package / '_internal' / 'app' / 'engine_boot.py'
    argv = [str(python), str(boot), engine, '--probe', *modules]
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=900, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    last = (proc.stdout or '').strip().splitlines()[-1:] or ['']
    if last[0] == 'PROBE_OK':
        return []
    if last[0].startswith('MISSING|'):
        return last[0][len('MISSING|'):].split('||')
    return [f'the probe did not run: {(proc.stderr or proc.stdout)[-600:]}']


def verify_runtime_imports(package, work, engines=ENGINES):
    """QBG's rule: never ship a runtime that cannot import what the code uses. Here per
    library folder: NoteCraft's own, and each engine's (its third-party imports and every
    module of its src package)."""
    app = package / '_internal' / 'app'
    env = clean_environment(work)
    before = snapshot(app)
    try:
        files, local = notecraft_code()
        names = third_party_imports(files, local)
        missing = probe(package, 'notecraft', names, app / 'engines' / 'shortnotes', env)
        if missing:
            raise SystemExit("NoteCraft's packaged libraries cannot import: " + '; '.join(missing))
        say(f"NoteCraft's libraries import every third-party module its code uses ({len(names)} checked).")
        for engine in CHILD_ENGINES:
            if engine not in engines:
                continue
            engine_dir = app / 'engines' / engine
            files, local = engine_code(engine_dir)
            names = third_party_imports(files, local) + src_modules(engine_dir)
            missing = probe(package, engine, names, engine_dir, env)
            if missing:
                raise SystemExit(f'The {engine} engine cannot import on its packaged libraries: ' + '; '.join(missing))
            say(f'{engine}: every module of the engine and its libraries imports ({len(names)} checked).')
    finally:
        remove_new(app, before)


# ------------------------------------------------------------------- checks
def registration_only(path):
    """The one shared value the package may carry: the app's Google sign-in registration
    (an installed-app OAuth client, as the old Short Notes exe and QBG One carry theirs).
    Anything else in that file, such as someone's tokens, is refused."""
    allowed = {'client_id', 'project_id', 'auth_uri', 'token_uri', 'auth_provider_x509_cert_url',
               'client_secret', 'redirect_uris'}
    try:
        info = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(info, dict) or set(info) != {'installed'}:
            raise ValueError('it is not an installed-app registration')
        client = info['installed']
        if not isinstance(client, dict) or set(client) - allowed:
            raise ValueError('unexpected fields: ' + ', '.join(sorted(set(client) - allowed)))
        if not str(client.get('client_id') or '').endswith('.apps.googleusercontent.com') or not client.get('client_secret'):
            raise ValueError('incomplete registration')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('The packaged sign-in registration is not valid: ' + str(exc))
    return {'installed': {k: v for k, v in client.items() if k in allowed}}


def write_registration(app, source):
    info = registration_only(source)
    target = app / REGISTRATION
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    return info['installed']


def verify_clean(package, registration=True, team_key=False):
    """No tests, run folders, data or secrets in the app; no sign-in files in the libraries.
    The two shared values a team package may carry are checked to be only that."""
    from note_studio import updater
    app = package / '_internal' / 'app'
    hits = []
    for path in app.rglob('*'):
        rel = path.relative_to(app)
        if rel.as_posix() == REGISTRATION:
            if not registration:
                hits.append((rel, 'a sign-in registration in a build without one'))
            else:
                registration_only(path)
            continue
        if rel.as_posix() == TEAM_KEY:
            if not team_key:
                hits.append((rel, 'a team key in a build without one'))
            else:
                updater.team_key_only(path)
            continue
        if path.is_dir():
            if path.name in EXCLUDED_DIRS - {'__pycache__'}:
                hits.append((rel, 'a folder that never travels'))
            continue
        if skipped(rel):
            hits.append((rel, 'a file that never travels'))
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            data = path.read_bytes()
            for pattern, label in SECRET_PATTERNS:
                approved = PUBLIC_LITERALS.get((rel.as_posix(), label))
                if any(hashlib.sha256(match.group()).hexdigest() != approved for match in pattern.finditer(data)):
                    hits.append((rel, label))
    for path in (package / '_internal' / 'libs').rglob('*'):
        if path.is_file() and path.name.lower() in ('credentials.json', 'token.json', 'client_secret.json', '.env',
                                                    'service_account.json'):
            hits.append((path.relative_to(package), 'a sign-in file'))
    if (package / '.note_studio').exists() or list(package.rglob('note_studio.db')):
        hits.append(('note_studio.db', "someone's data"))
    if hits:
        raise SystemExit('Not packaged, private or secret-shaped content found:\n' +
                         '\n'.join(f'    {rel}: {why}' for rel, why in hits[:40]))
    say('Clean: no tests, runs, data, sign-ins or secret-shaped values in the package.')


def verify_upgrade_rules(script):
    """QBG's rules for an installer that updates in place (the script's lines, not its comments)."""
    script = '\n'.join(line for line in script.splitlines() if not line.lstrip().startswith(';'))
    if APP_ID not in script:
        raise ValueError('Keep the installer identity (AppId) so updates find the same installation.')
    if any(section in script.lower() for section in ('[installdelete]', '[uninstalldelete]')):
        raise ValueError('An update must never delete anything.')
    if '.note_studio' in script:
        raise ValueError("The installer must not touch the data folder (~/.note_studio).")


def clean_environment(work):
    """No sign-in, no keys, no originals: the package must work on its own."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('NOTE_STUDIO_', 'SHEETS_BRIDGE_', 'GOOGLE_', 'GEMINI_', 'OPENAI_', 'PW_',
                                        'UPSC_', 'PYTHON'))}
    nowhere = work / 'no_original'
    env.update(NOTE_STUDIO_HOME=str(work / 'clean home'), NOTE_STUDIO_OLD_APP_HOME=str(work / 'clean old app'),
               NOTE_STUDIO_SHORTNOTES_ORIGINAL=str(nowhere), NOTE_STUDIO_UPSC_ORIGINAL=str(nowhere),
               NOTE_STUDIO_PW_BOOKS_ORIGINAL=str(nowhere), NOTE_STUDIO_NOTES_QC_ORIGINAL=str(nowhere),
               NOTE_STUDIO_OFFLINE='1', NOTE_STUDIO_NO_BROWSER='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    return env


def smoke_test(package, work, release, client_id):
    """QBG's clean start: the launcher's --check, then the server and its pages, with an empty
    data folder and none of this computer's sign-ins, keys or original apps."""
    before = snapshot(package / '_internal')
    try:
        # Every attempt starts with fresh verification data, including a
        # resumed build; previous lecture fixtures and mock runs are retained.
        check_work = Path(tempfile.mkdtemp(prefix='verification-', dir=work))
        return _smoke_test(package, check_work, release, client_id)
    finally:
        remove_new(package / '_internal', before)
        shutil.rmtree(package / 'logs', ignore_errors=True)


def _smoke_test(package, work, release, client_id):
    env = clean_environment(work)
    proc = subprocess.run([str(package / f'{APP}.exe'), '--check'], cwd=str(package), env=env, timeout=180,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    log = package / 'logs' / 'check.log'
    text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
    if proc.returncode or 'NOTECRAFT_CHECK_OK' not in text:
        raise SystemExit(f'{APP}.exe --check failed:\n' + text[-3000:])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env['NOTE_STUDIO_PORT'] = str(port)
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    app = package / '_internal' / 'app'
    server_log = work / 'clean-server.log'
    checked = []
    with server_log.open('w', encoding='utf-8') as output:
        server = subprocess.Popen([str(package / '_internal' / 'python' / 'python.exe'), '-u', str(app / 'start_installed.py')],
                                  cwd=str(app), env=env, stdout=output, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            end = time.monotonic() + 120
            while True:
                try:
                    with local.open(f'http://127.0.0.1:{port}/static/release.json', timeout=2) as response:
                        assert json.load(response)['version'] == release['version']
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > end:
                        raise SystemExit('The packaged server did not start: ' + str(server_log))
                    time.sleep(.5)
            for route in ('/', '/new', '/automation', '/queue', '/runs', '/practice', '/check', '/settings',
                          '/settings/ai', '/settings/categories', '/settings/values', '/settings/system',
                          '/settings/system/check'):
                with local.open(f'http://127.0.0.1:{port}{route}', timeout=120) as response:
                    html = response.read().decode('utf-8')
                assert response.status == 200 and 'Traceback' not in html, route
                assert not client_id or client_id not in html, route        # the registration is never shown
                if route == '/settings/system':
                    assert 'team install' in html and release['version'] in html, route
                if route == '/settings/system/check':
                    if 'upsc' in release.get('engines', ENGINES):
                        assert "UPSC engine&#39;s Python" in html or "UPSC engine's Python" in html, route
                    else:
                        assert 'not in this installation' in html and 'ffmpeg' not in html, route
                    assert 'It could not be started' not in html and 'Missing:' not in html, html[-1500:]
                if 'upsc' not in release.get('engines', ENGINES):
                    if route == '/practice':
                        assert 'Mains practice and DPP are not in this installation.' in html, route
                    if route in ('/new', '/settings/ai'):
                        assert 'href="/new?category=upsc"' not in html and 'value="upsc_dcn"' not in html, route
                checked.append(route)
            with local.open(f'http://127.0.0.1:{port}/update/status', timeout=30) as response:
                status = json.load(response)
            assert status['current'] == release['version'] and status['installed'] is True, status
            assert status['source']['built_in'] == release.get('releases', ''), status['source']
            assert status['setup_name'] == setup_name(release.get('edition', 'full')) + '_<version>', status
            with local.open(f'http://127.0.0.1:{port}/', timeout=30) as response:
                home = response.read().decode('utf-8')
            assert f"v{release['version']}" in home and 'data-update-button' in home
        finally:
            subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'], capture_output=True)
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
    say(f'Clean start passed: {APP}.exe --check, {len(checked)} pages, the version, the Update button and '
        'the included engines on their own libraries.')
    mock = mock_runs(package, work, env)
    return {'exe_check': True, 'pages': checked, 'update_status': True, 'mock_runs': mock}


# One mock run per child engine: the status each ends with when only the PDF step is stubbed.
EXPECTED = {'upsc': 'needs_attention', 'pw_books': 'needs_attention', 'notes_qc': 'complete'}


def mock_runs(package, work, env):
    """The plan's "one mock child run per engine", on the packaged copy (smoke_engines.py)."""
    lecture = work / 'lecture'
    lecture.mkdir()
    run([sys.executable, '-c', 'import sys; from pathlib import Path; from scripts.make_sequence_smoke_fixtures '
         'import build_fixtures; build_fixtures(Path(sys.argv[1]))', lecture],
        cwd=str(ROOT / 'engines' / 'shortnotes'), env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=120)
    sample = lecture / 'text-order.pdf'
    proc = subprocess.run([str(package / '_internal' / 'python' / 'python.exe'), str(PACKAGING / 'smoke_engines.py'),
                           str(package / '_internal' / 'app'), str(work / 'mock runs'), str(lecture / 'text-order.pdf'),
                           str(sample)], cwd=str(work), env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=1800, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    line = next((s for s in reversed((proc.stdout or '').splitlines()) if s.startswith('SMOKE|')), '')
    if not line:
        raise SystemExit('The mock engine runs did not finish:\n' + (proc.stdout or '')[-2000:] + (proc.stderr or '')[-2000:])
    found = json.loads(line[len('SMOKE|'):])
    expected = {k: v for k, v in EXPECTED.items() if (package / '_internal' / 'app' / 'engines' / k).is_dir()}
    wrong = {k: v for k, v in found.items() if v['status'] != expected.get(k)}
    if wrong or set(found) != set(expected):
        raise SystemExit('A mock engine run did not end as expected:\n' + json.dumps(wrong or found, indent=2)[:4000])
    say('Mock runs passed on the packaged copy: ' + ', '.join(f'{k} {v["status"]}' for k, v in found.items()) + '.')
    return found


def find_compiler(given=None):
    """Inno Setup's ISCC.exe: given, this project's cache, Program Files, or QBG One's build
    cache (used where it is, never changed)."""
    candidates = [Path(given)] if given else []
    candidates += [CACHE / 'InnoSetup' / 'ISCC.exe',
                   Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Inno Setup 6' / 'ISCC.exe',
                   Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Inno Setup 6' / 'ISCC.exe',
                   Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe',
                   Path.home() / 'Downloads' / 'QBG_Complete_Toolkit' / 'QBG_Studio_Vibecraft' / 'backend' / 'studio'
                   / '_build_cache' / 'installer_tools' / 'InnoSetup' / 'ISCC.exe']
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit('Inno Setup (ISCC.exe) was not found. Install Inno Setup 6, or pass --iscc <path to ISCC.exe>.')


def quick_start(release, registration, team_key=False):
    setup = setup_name(release.get('edition', 'full'))
    included = release.get('engines', ENGINES)
    engines = 'Hand-drawn and B.Sc. typeset notes, Sheet automation and Notes check are included.'
    engines += ('\nPW Books is included.' if 'pw_books' in included else '\nPW Books is not in this installation.')
    engines += ('\nUPSC notes, Mains practice and DPP are included.' if 'upsc' in included else
                '\nUPSC notes, Mains practice and DPP are not in this installation.')
    finished = ('Practice and Check notes start from a finished run.' if 'upsc' in included else
                'Check notes starts from a finished run, or from a notes file.')
    carries = ("It includes the app's Google sign-in registration (no one's sign-in or\n"
               "token)" + (" and the team's service-account key for updates" if team_key else "") +
               ", so share this installer only with your team." if registration else
               "It has no Google sign-in registration: keep the shortnotes-core folder on\n"
               "this computer, or ask for a build that has one.")
    return f'''{APP.upper()} {release['version']} - TEAM SETUP

1. Run {setup}.exe on Windows 10/11 (64 bit). Python is included.
   {engines}
2. Open {APP} from the desktop or the Start menu. Your browser opens it.
3. In Settings, choose how {APP} keeps its sheet list, then press
   Sign in with Google and use your own allowed account.
4. New notes: pick the category and the note type, add the lecture, press
   Make notes. {finished}

Updates: when a newer version is in the team's release folder, a blue
Update button appears beside Smart Queue. Press it: {APP} closes, updates
and opens again by itself in about a minute.

Your notes, runs and settings live in %USERPROFILE%\\.note_studio, never in
the install folder, so updates and reinstalls keep them.
{carries}

{APP} runs in the Windows notification area. Right-click its icon to quit.
PDFs from Word notes need Microsoft Word or LibreOffice on the computer.
'''


def release_notes_file(destination, version, notes, edition='full'):
    """NoteCraft_Setup_<version>.txt with one fix per line (the --notes split on ';'), or None."""
    from note_studio import updater
    lines = updater.note_lines(notes, limit=20)
    if not lines:
        return None
    path = Path(destination) / f'{setup_name(edition)}_{version}.txt'
    path.write_bytes(('\n'.join(lines) + '\n').encode('utf-8'))
    return path


def publish(installer, version, folder, notes, edition='full'):
    """The installer into the release folder as the person signed in to NoteCraft on this computer."""
    import importlib
    importlib.import_module('note_studio.app')          # prepares the database and the sign-in paths
    from note_studio import signin, updater
    current = signin.auth()
    if current is None:
        raise PermissionError(f'Choose in {APP} Settings how it keeps its sign-in, and sign in with Google first.')
    current.status()                       # restores the saved sign-in
    if current.account() is None:
        raise PermissionError(f'Sign in with Google in {APP} Settings first.')
    return updater.publish(installer, version, folder, notes=notes, setup_prefix=setup_name(edition))


# What a build leaves in dist\team and _build: only these are ever pruned. _build also holds
# install backups, sync backups and gate snapshots, which a build never touches.
RELEASE_DIR = re.compile(r'^\d+(?:\.\d+){3}(?:-(?:noupsc|nopwbooks|noupsc-nopwbooks))?$')
STAGING_DIR = re.compile(r'^\d+(?:\.\d+){3}-(?:(?:noupsc|nopwbooks|noupsc-nopwbooks)-)?[a-z0-9_]{8}$')
# The staged runtime + libraries (about 2.2 GB) and the clean-start data of a finished build.
STAGED = ('package', 'verification-*')


def prune_old_builds(keep, current=None):
    """Keep the newest `keep` releases and build records, and the staged package of the
    current build only (and only with --keep-package).

    Each staged package is about 2.2 GB: three kept with --keep-package filled 6.8 GB
    (27 October 2026). A finished build (verification.json written) cannot be resumed,
    so older ones keep just their small records, whatever --keep says. Never fails a
    good build."""
    for folder, pattern in ((DIST, RELEASE_DIR), (WORK_ROOT, STAGING_DIR)):
        try:
            if not folder.is_dir():
                continue
            builds = sorted((d for d in folder.iterdir() if d.is_dir() and pattern.match(d.name)),
                            key=lambda d: d.stat().st_mtime, reverse=True)
            for old in builds[keep:]:
                if current is not None and old.resolve() == Path(current).resolve():
                    continue
                shutil.rmtree(old, ignore_errors=True)
                say(f'Pruned old build: {folder.name}/{old.name}')
            if folder != WORK_ROOT:
                continue
            for done in builds[:keep]:
                if current is not None and done.resolve() == Path(current).resolve():
                    continue
                if not (done / 'verification.json').is_file():
                    continue   # unfinished: --resume-work still needs its package
                for pattern_name in STAGED:
                    for staged in done.glob(pattern_name):
                        if staged.is_dir():
                            shutil.rmtree(staged, ignore_errors=True)
                            say(f'Removed the staged copy of finished build: {done.name}/{staged.name}')
        except Exception as exc:  # noqa: BLE001
            say(f'Could not prune old builds in {folder.name}: {exc}')


def link_or_copy(source, destination):
    """The versioned installer is the same bytes: a hard link costs no space (a copy did, 700 MB)."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--publish', metavar='FOLDER', default='',
                        help='The team release folder on Google Drive (link or id): baked into the build so every '
                             f'{APP} looks there, and the installer is uploaded into it.')
    parser.add_argument('--notes', default='', help='What changed, one fix per "; " - shown in the update pop-up.')
    parser.add_argument('--without', choices=('upsc', 'pw_books'), action='append', default=[],
                        help='Leave out this engine; repeat for both. Each engine selection has its own update names.')
    parser.add_argument('--engine-libs', metavar='ENGINE=FOLDER', action='append', default=[],
                        help="Copy an engine's libraries from a preserved package instead of its original .venv.")
    parser.add_argument('--no-signin', action='store_true',
                        help="Leave out the app's Google sign-in registration.")
    parser.add_argument('--team-key', type=Path, default=None,
                        help='The team\'s service-account key to build in (default: the one added in this '
                             f'computer\'s {APP} Settings -> Updates), so every install reads the release folder.')
    parser.add_argument('--no-team-key', action='store_true', help='Build without the team key.')
    parser.add_argument('--iscc', default='', help='Path to Inno Setup 6 ISCC.exe.')
    parser.add_argument('--keep', type=int, default=3, help='How many builds to keep (default 3).')
    parser.add_argument('--keep-package', action='store_true', help='Keep the staged package folder.')
    parser.add_argument('--resume-work', type=Path,
                        help='Reuse staged runtime/libraries after a failed build; re-copy code and rerun every check.')
    parser.add_argument('--compression', choices=('lzma2/fast', 'zip'), default='lzma2/fast',
                        help='Installer compression. zip builds faster with the same bundled runtimes.')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13):
        raise SystemExit('Build with Python 3.13: the engines\' environments are 3.13.')
    compiler = find_compiler(args.iscc)
    release = edition_release(next_available_release(), args.without)
    library_sources = {}
    for value in args.engine_libs:
        engine, sep, folder = value.partition('=')
        if not sep or engine not in CHILD_ENGINES or engine not in release['engines'] or not Path(folder).is_dir():
            raise SystemExit('--engine-libs must be ENGINE=FOLDER for an included child engine and an existing folder.')
        library_sources[engine] = str(Path(folder).resolve())
    if args.publish:
        from note_studio import updater
        release['releases'] = updater.drive_folder_id(args.publish)
    elif build_version.RELEASE_FILE.is_file():
        try:                                              # a build without --publish keeps the folder it had
            previous = json.loads(build_version.RELEASE_FILE.read_text(encoding='utf-8'))
            if previous.get('releases'):
                release['releases'] = previous['releases']
        except ValueError:
            pass
    registration = None if args.no_signin else paths.shortnotes_original() / 'client_secret.json'
    if registration is not None and not registration.is_file():
        raise SystemExit(f'The Google sign-in registration was not found ({registration}). Pass --no-signin to '
                         'build without one.')
    from note_studio import updater
    key_source = None if args.no_team_key else (args.team_key or updater.team_key())
    team_email = ''
    if key_source is not None:
        try:
            team_email = updater.team_key_only(key_source)
        except ValueError as exc:
            raise SystemExit(str(exc))
        say(f"Building in the team's service-account key: {team_email}")
    else:
        say(f'No team key: installs read the release folder with each person\'s Google sign-in. Add the key in '
            f'{APP} Settings -> Updates (or pass --team-key) to build it in.')
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if args.resume_work:
        work = args.resume_work.resolve()
        if not work.is_relative_to(WORK_ROOT.resolve()) or not (work / 'package' / '_internal' / 'libs').is_dir():
            raise SystemExit('--resume-work must name an existing build inside this project\'s _build folder.')
        staged_release = work / 'package' / '_internal' / 'app' / 'note_studio' / 'static' / 'release.json'
        if not staged_release.is_file():
            raise SystemExit('This staging folder has not reached the verification stage; start a new build.')
        release = json.loads(staged_release.read_text(encoding='utf-8'))
        expected = edition_release(release, args.without)
        if release.get('edition', 'full') != expected['edition'] or release.get('engines', list(ENGINES)) != expected['engines']:
            raise SystemExit('--resume-work must use the same --without choice as the staged build.')
        release = expected
        if release_destination(release).exists():
            folder = release.get('releases')
            release = edition_release(build_version.next_release(staged_release), args.without)
            if folder:
                release['releases'] = folder
    # Smaller editions leave the source release untouched; allocate later builds from dist too.
    while release_destination(release).exists():
        prefix, revision = release['version'].rsplit('.', 1)
        release['version'] = prefix + '.' + str(int(revision) + 1)
    setup = setup_name(release['edition'])
    engines = release['engines']
    if not args.resume_work:
        suffix = '-' + release['edition'] + '-' if release['edition'] != 'full' else '-'
        work = Path(tempfile.mkdtemp(prefix=release['version'] + suffix, dir=WORK_ROOT))
    package = work / 'package'
    app = package / '_internal' / 'app'
    libs = package / '_internal' / 'libs'
    say(f'Building {APP} {release["version"]} in {work}')

    say('Staging the code and a private Python runtime (no saved data, no sign-ins).')
    if not args.resume_work:
        copy_runtime(package / '_internal' / 'python')
    manifest = []
    copy_tree(ROOT / 'note_studio', app / 'note_studio')
    for engine in engines:
        count = copy_tree(ROOT / 'engines' / engine, app / 'engines' / engine)
        say(f'  engine {engine}: {count} files')
    for name in ('start_installed.py', 'engine_boot.py'):
        shutil.copy2(PACKAGING / name, app / name)
    for path in sorted(app.rglob('*')):
        if path.is_file():
            manifest.append({'path': path.relative_to(app).as_posix(),
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    icon = make_icon.write(app / 'note_studio' / 'static' / 'icons' / 'notecraft.ico')

    say("Copying the libraries: NoteCraft's from this computer's Python, each engine's from its original.")
    files, local = notecraft_code()
    dists = notecraft_distributions(third_party_imports(files, local))
    copied = (sum(1 for path in (libs / 'notecraft').rglob('*') if path.is_file()) if args.resume_work
              else sum(copy_distribution(dist, libs / 'notecraft') for dist in dists.values()))
    say(f'  notecraft: {len(dists)} packages, {copied} files')
    filled = {}
    for engine in CHILD_ENGINES:
        if engine not in engines:
            continue
        if not args.resume_work:
            copy_engine_libraries(engine, libs / engine, library_sources.get(engine))
        origin = library_sources.get(engine) or paths.original(engine)
        local_site = paths.ENGINES_DIR / engine / '.venv' / 'Lib' / 'site-packages'
        if engine not in library_sources and local_site.is_dir():
            origin = local_site
        say(f'  {engine}: {"reused staged libraries" if args.resume_work else "copied from " + str(origin)}')
        filled[engine] = fill_declared_requirements(engine, libs / engine)
    verify_runtime_imports(package, work, engines)

    build_version.write_release(app / 'note_studio' / 'static' / 'release.json', release)
    client_id = ''
    if registration is not None:
        client_id = write_registration(app, registration)['client_id']
        say("Built in the app's Google sign-in registration (checked: no tokens).")
    if key_source is not None:
        (app / TEAM_KEY).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(key_source, app / TEAM_KEY)
    (package / 'QUICK_START.txt').write_text(quick_start(release, registration is not None, bool(team_email)),
                                             encoding='utf-8')
    verify_clean(package, registration is not None, bool(team_email))

    framework = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319'
    launcher_source = work / 'desktop_launcher.cs'
    launcher_source.write_text((PACKAGING / 'desktop_launcher.cs').read_text(encoding='utf-8')
                               .replace('__APP_NAME__', APP), encoding='utf-8')
    run([framework / 'csc.exe', '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/win32icon:' + str(icon),
         '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/win32manifest:' + str(PACKAGING / 'launcher.manifest'),
         '/out:' + str(package / f'{APP}.exe'), launcher_source])
    verification = smoke_test(package, work, release, client_id)
    verify_clean(package, registration is not None, bool(team_email))

    script = (PACKAGING / 'notecraft_setup.iss').read_text(encoding='utf-8')
    script = script.replace('OutputBaseFilename={#AppName}_Setup', 'OutputBaseFilename=' + setup)
    script = script.replace('Compression=lzma2/fast', 'Compression=' + args.compression)
    verify_upgrade_rules(script)
    iss = work / 'notecraft_setup.iss'
    iss.write_text(script, encoding='utf-8')
    destination = release_destination(release)
    destination.mkdir(parents=True, exist_ok=False)
    say(f'Making the installer with {compiler} (a few minutes).')
    run([compiler, '/Q', f'/DPackageDir={package}', f'/DOutputDir={destination}', f'/DAppVersion={release["version"]}',
         f'/DAppName={APP}', iss])
    installer = destination / f'{setup}.exe'
    assert installer.read_bytes()[:2] == b'MZ' and installer.stat().st_size > 10_000_000
    shutil.copy2(package / 'QUICK_START.txt', destination / 'QUICK_START.txt')
    digest = hashlib.sha256()
    with open(installer, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    checksum = digest.hexdigest()
    versioned = destination / f'{setup}_{release["version"]}.exe'
    link_or_copy(installer, versioned)
    (destination / 'SHA256.txt').write_text(f'{checksum}  {installer.name}\n{checksum}  {versioned.name}\n',
                                            encoding='utf-8')
    notes_file = release_notes_file(destination, release['version'], args.notes, release['edition'])
    (work / 'verification.json').write_text(json.dumps({
        'release': release, 'source_files': manifest, 'verification': verification, 'installer_sha256': checksum,
        'installer_bytes': installer.stat().st_size, 'notecraft_packages': sorted(dists),
        'added_to_engines': filled, 'engine_library_sources': library_sources,
        'signin_registration': registration is not None,
        'team_key': team_email}, indent=2), encoding='utf-8')
    write_source_release(release)
    say(f'Ready: {installer} ({installer.stat().st_size / 1e6:.1f} MB)')
    say(f'For the release folder: {versioned.name}' + (f' and {notes_file.name}' if notes_file else ''))
    if args.publish:
        say('Publishing to the release folder.')
        try:
            published = publish(versioned, release['version'], args.publish, args.notes, release['edition'])
            say('Published: ' + published['link'])
        except PermissionError as exc:
            say('Not uploaded: ' + str(exc))
            say('To publish by hand, drop this file into the release folder: ' + str(versioned))
            if notes_file:
                say('...and this one beside it, for the update pop-up: ' + str(notes_file))
    if not args.keep_package:
        shutil.rmtree(package, ignore_errors=True)
    prune_old_builds(max(args.keep, 1), current=work)


if __name__ == '__main__':
    main()
