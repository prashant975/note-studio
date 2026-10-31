"""Build a clean Windows installer from the canonical Studio source.

Usage: python build_team_installer.py
The existing Python runtime is reused, never the installed app or user data.
Output: QBG_Studio_Vibecraft/dist/team/<version>/QBG_Studio_Setup.exe
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

import build_package as base
import build_version

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
LOADER = b"exec(__import__('marshal').loads(__import__('pathlib').Path(__file__).with_suffix('.pyc').read_bytes()[16:]), globals())\n"
ROOT_MODULES = {'pw_access.py', 'pw_auth.py', 'pw_login.py',
                'qbg_concurrency.py', 'setup_tools.py', 'start_installed.py'}
APP_DIRS = {'1_Existing_Bots_Windows_Ready', '2_Question_Creation_Pipeline', 'qbg_studio'}
EXCLUDED = {'data', 'uploads', 'tests', '__pycache__', 'dist', '_build_cache', '.pytest_cache',
            'translation_parallel_runs', 'translation_quality_reports'}


def source_files():
    names = subprocess.check_output(['git', '-C', str(PROJECT), 'ls-files', '-z',
        '--cached', '--others', '--exclude-standard', '--', 'backend/studio'], text=True).split('\0')
    for name in sorted(set(filter(None, names))):
        path = PROJECT / name
        rel = path.relative_to(ROOT)
        if not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            continue
        if any(part in EXCLUDED or part.startswith('output_') for part in rel.parts):
            continue
        if rel.parts[0] not in APP_DIRS and rel.as_posix() not in ROOT_MODULES:
            continue
        if (path.name.startswith(('.env', 'credentials', 'content_translation_cache'))
                or path.name in {'deployment.json', 'studio.out', 'studioport.txt'}
                or path.suffix.lower() in {'.db', '.pyc', '.pyo', '.log', '.token', '.bak'}):
            continue
        yield path, rel


def clean_environment(work):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('QBG_', 'PW_', 'GOOGLE_', 'GEMINI_', 'OPENAI_'))}
    env.update(PW_AUTH_STATE_DIR=str(work / 'empty-auth'), QBG_STUDIO_NO_BROWSER='1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return env


def compile_app(app):
    count = 0
    for path in app.rglob('*.py'):
        data = path.read_bytes()
        code = compile(data, str(path.relative_to(app)), 'exec', dont_inherit=True)
        path.with_suffix('.pyc').write_bytes(importlib.util.MAGIC_NUMBER + struct.pack('<III', 0, 0, len(data)) + marshal.dumps(code))
        loader = LOADER
        if path == app / '2_Question_Creation_Pipeline/config.py':
            for node in ast.parse(data).body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('LANGUAGE_IDS', 'LINK_TEMPLATE') for t in node.targets):
                    loader += (ast.unparse(node) + '\n').encode('utf-8')
        path.write_bytes(loader)
        count += 1
    return count


REGISTRATION = 'deployment.json'
# The bots' service-account key, built in only with --team-key (the owner's
# decision of 28 September 2026): a teammate's Studio then opens the team's
# sheet, sends team data and finds its updates with nothing handed around.
TEAM_KEY = '1_Existing_Bots_Windows_Ready/credentials.json'
TEAM_KEY_LINE = 'Source: "{#PackageDir}\\_internal\\app\\1_Existing_Bots_Windows_Ready\\credentials.json";'


def team_key_only(path):
    """The one key a team package may carry is a Google service-account key;
    anything else in that file, such as a person's sign-in, is refused."""
    try:
        info = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(info, dict) or info.get('type') != 'service_account':
            raise ValueError('it is not a service-account key')
        if not str(info.get('client_email') or '').endswith('.iam.gserviceaccount.com') or not info.get('private_key'):
            raise ValueError('it has no service account or private key')
    except (OSError, ValueError) as exc:
        raise ValueError('The packaged bots key is not valid: ' + str(exc))
    return info['client_email']


def registration_only(path):
    """The one shared value a team package may carry: the app's Google sign-in
    registration. Reject anything else in that file, such as a user's tokens."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        client = data['oauth_client']
        if set(data) != {'oauth_client'} or set(client) != {'client_id', 'client_secret', 'redirect_uri'}:
            raise ValueError('unexpected fields')
        if not all(isinstance(v, str) for v in client.values()):
            raise ValueError('values must be text')
        if not client['client_id'].endswith('.apps.googleusercontent.com') or not client['client_secret']:
            raise ValueError('incomplete registration')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('The packaged sign-in registration is not valid: '+str(exc))
    return client


def write_registration(app):
    """Export this computer's application registration, never a user's sign-in."""
    sys.path.insert(0, str(ROOT))
    import pw_auth
    client_id, client_secret = pw_auth._client_credentials()
    target = app / REGISTRATION
    target.write_text(json.dumps({'oauth_client': {'client_id': client_id, 'client_secret': client_secret,
                                                   'redirect_uri': pw_auth.redirect_uri()}}), encoding='utf-8')
    return registration_only(target)


def verify_clean(package, team_key=False):
    app = package / '_internal/app'
    for path in app.rglob('*'):
        rel = path.relative_to(app)
        if rel.as_posix() == REGISTRATION:
            registration_only(path)
            continue
        if team_key and rel.as_posix() == TEAM_KEY:
            team_key_only(path)
            continue
        if (set(rel.parts) & EXCLUDED or path.name in ('credentials.json', 'deployment.json')
                or path.suffix.lower() in ('.db', '.token', '.log', '.xlsx')):
            raise SystemExit('Private runtime data in package: ' + str(rel))
        if path.is_file() and path.suffix == '.pyc':
            for pattern, label in base.SECRET_PATTERNS:
                if pattern.search(path.read_bytes()):
                    raise SystemExit('Credential-like value in compiled module: ' + str(rel) + ' (' + label + ')')
    base.secret_scan(package, allowed_files=(app/REGISTRATION,) + ((app/TEAM_KEY,) if team_key else ()))


def verify_upgrade_rules(package, script, team_key=False):
    """Fail future team builds if their installer could replace saved work."""
    if 'AppId={{C489EA28-BD98-49D4-B6A3-F11846E07DAD}' not in script:
        raise ValueError('Keep the existing installer identity so updates find the same installation.')
    if any(section in script.lower() for section in ('[installdelete]', '[uninstalldelete]')):
        raise ValueError('Team updates must not delete existing application data.')
    prefix = 'Source: "{#PackageDir}\\_internal\\app\\*";'
    entries = [line for line in script.splitlines() if line.startswith(prefix)]
    if len(entries) != 1 or 'Excludes: ' not in entries[0]:
        raise ValueError('The application update must explicitly exclude saved data.')
    exclusions = entries[0].split('Excludes: ', 1)[1]
    for rel in ('qbg_studio\\data\\*', 'qbg_studio\\uploads\\*',
                '1_Existing_Bots_Windows_Ready\\credentials.json',
                '2_Question_Creation_Pipeline\\config.py'):
        if rel not in exclusions:
            raise ValueError('Missing upgrade exclusion: '+rel)
    app = package / '_internal/app'
    for rel in ('qbg_studio/data', 'qbg_studio/uploads', TEAM_KEY):
        if (app/rel).exists() and not (team_key and rel == TEAM_KEY):
            raise ValueError('Team update must not contain saved user data: '+rel)
    if (app/REGISTRATION).exists():
        registration_only(app/REGISTRATION)
    # A built-in bots key goes only where there is none yet: a key a teammate
    # added themselves is never replaced by an update.
    key_entries = [line for line in script.splitlines() if line.startswith(TEAM_KEY_LINE)]
    if (app/TEAM_KEY).exists():
        team_key_only(app/TEAM_KEY)
        if len(key_entries) != 1 or 'onlyifdoesntexist' not in key_entries[0]:
            raise ValueError("A built-in bots key must never replace the key a teammate already has.")
    elif key_entries:
        raise ValueError('The installer lists a bots key the package does not carry.')
    config_entries = [line for line in script.splitlines() if line.startswith(
        'Source: "{#PackageDir}\\_internal\\app\\2_Question_Creation_Pipeline\\config.py*";')]
    if len(config_entries)!=1 or 'onlyifdoesntexist' not in config_entries[0]:
        raise ValueError('Existing pipeline settings must be preserved on upgrade.')


def smoke_test(package, work, release):
    # Relocate the complete payload and use an empty auth directory: no access
    # to the builder's saved Google sign-in or application database.
    checked = work / 'relocated clean check'
    shutil.copytree(package, checked)
    # A built-in bots key would let this throwaway copy reach the team's real
    # sheet (it makes its own queue tab there when a page looks at the sheet).
    # The check stays off the network, as it always has.
    (checked / '_internal/app' / TEAM_KEY).unlink(missing_ok=True)
    env = clean_environment(work)
    proc = subprocess.run([str(checked/'QBG_Studio.exe'), '--check'], cwd=checked,
                          env=env, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.returncode:
        raise SystemExit('Clean EXE startup failed; see ' + str(checked/'logs/check.log'))
    log = (checked/'logs/check.log').read_text(encoding='utf-8')
    assert 'QBG_CHECK_OK' in log, log[-2000:]
    python = checked/'_internal/python/python.exe'
    app = checked/'_internal/app'
    probe = subprocess.run([str(python), '-c',
        "import db,tagging_service,tagging_model,solution_model,editable_math,latex2mathml; "
        r"assert '<mfrac>' in editable_math.equation(r'\frac{1}{2}'); "
        "tagging_service.initialize(); "
        "assert db.connect().execute('select count(*) from batches').fetchone()[0]==0; "
        "assert db.connect().execute('select count(*) from jobs').fetchone()[0]==0; "
        "assert db.get_setting('tagging_model')==tagging_model.DEFAULT_MODEL; "
        "print('CLEAN_STATE_OK')"], cwd=app, env=env, capture_output=True, text=True, timeout=60)
    if probe.returncode:
        raise SystemExit(probe.stderr[-2000:])
    # Choose an available port, independent of the user's running installation.
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env['QBG_STUDIO_PORT'] = str(port)
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    log_path = work/'clean-server.log'
    with log_path.open('w', encoding='utf-8') as output:
        server = subprocess.Popen([str(python), str(app/'start_installed.py')], cwd=app,
            env=env, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            end = time.monotonic()+45
            while True:
                try:
                    with local.open(f'http://127.0.0.1:{port}/static/release.json', timeout=2) as response:
                        assert json.load(response)['version'] == release['version']
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic()>end:
                        raise RuntimeError('Clean server did not start: '+str(log_path))
                    time.sleep(.5)
            checked_routes = []
            registration = registration_only(app/REGISTRATION)
            for route in ('/settings', '/tagging', '/new/any-format', '/solutions', '/translate/bulk'):
                with local.open(f'http://127.0.0.1:{port}'+route, timeout=15) as response:
                    html = response.read().decode('utf-8')
                    assert response.status==200 and 'Traceback' not in html, route
                    # The packaged registration works silently: never displayed, nothing to type.
                    assert registration['client_id'] not in html and registration['client_secret'] not in html, route
                    if route=='/settings':
                        assert 'Sign in with Google' in html
                        assert 'Google sign-in is not set up' not in html
                        assert 'name="client_secret"' not in html and 'Change client' not in html
                    if route=='/tagging':
                        assert '<option value="direct" selected>' in html
                        assert '<select name="model"' not in html
                        assert '193532 reference rows across 52 categories' in html
                    checked_routes.append(route)
        finally:
            server.terminate()
            try: server.wait(timeout=15)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=10)
    base.say('Clean EXE and five pages passed: sign-in ready, registration never displayed, no saved work.')
    return dict(exe_check=True, clean_database=True, pages=checked_routes)


def third_party_imports():
    """Every module the app imports at module level or inside a function,
    except the standard library, the app's own files, and imports guarded by
    try/except (those are optional by design)."""
    folders=('qbg_studio','2_Question_Creation_Pipeline','.')
    local={p.stem for rel in folders for p in (ROOT/rel).glob('*.py')}|{'qbg_studio','tests'}
    names=set()
    for rel in folders:
        for path in (ROOT/rel).glob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8'),str(path))
            guarded=set()
            for node in ast.walk(tree):
                if isinstance(node,ast.Try):
                    for inner in ast.walk(node):
                        if isinstance(inner,(ast.Import,ast.ImportFrom)):guarded.add(id(inner))
            for node in ast.walk(tree):
                if id(node) in guarded:continue
                if isinstance(node,ast.Import):
                    for alias in node.names:names.add(alias.name.split('.')[0])
                elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module:
                    names.add(node.module.split('.')[0])
    # Imports that only a hosted worker takes (QBG_HOSTED_WORKER=1) are not the team package's business.
    optional={'hosted'}
    return sorted(n for n in names if n not in sys.stdlib_module_names and n not in local and n not in optional)


def verify_runtime_imports(package):
    """The bundled runtime is a copy of the installed one, so a dependency the
    developer's Python has and the runtime lacks reaches every teammate as
    "No module named ..." (lxml, 25 September 2026). Refuse to package that."""
    python=package/'_internal/python/python.exe'
    names=third_party_imports()
    missing=[n for n in names if subprocess.run([str(python),'-c','import '+n],capture_output=True).returncode!=0]
    if missing:
        raise SystemExit('The bundled Python runtime cannot import: '+', '.join(missing)+'. Install into '+str(python.parent)+
                         ' with "python.exe -m pip install <name>" (or drop the dependency), then build again.')
    base.say('Bundled runtime imports every third-party module the code uses (%d checked).'%len(names))


def _prune_old_builds(keep=10):
    """Keep only the newest `keep` builds under dist/team and artifacts/team-installer.

    Every build leaves a few hundred MB (a bundled runtime + the app, twice), and
    these once reached 38 GB. Newest by mtime are kept; the rest are removed. Never
    raises - a cleanup problem must not fail a good build."""
    for folder in (PROJECT/'dist/team', PROJECT/'artifacts/team-installer'):
        try:
            if not folder.is_dir():
                continue
            builds=sorted((d for d in folder.iterdir() if d.is_dir()),
                          key=lambda d:d.stat().st_mtime, reverse=True)
            for old in builds[keep:]:
                shutil.rmtree(old, ignore_errors=True)
                base.say('Pruned old build: %s/%s'%(folder.name, old.name))
        except Exception as exc:
            base.say('Could not prune old builds in %s: %s'%(folder.name, exc))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(os.getenv('LOCALAPPDATA',''))/'Programs/QBGStudio/_internal/python')
    parser.add_argument('--publish', metavar='FOLDER', default='', help='The team release folder on Google Drive (link or id): baked into the build so every Studio looks there, and the installer is uploaded into it when the folder is in a Shared drive.')
    parser.add_argument('--notes', default='', help='One line shown next to the release.')
    parser.add_argument('--team-key', type=Path, help="The bots' service-account key to build in, so a teammate's "
                        'Studio opens the team sheet and finds updates with nothing handed around. It is installed '
                        "only where there is no key yet, never over a teammate's own.")
    parser.add_argument('--team-sheet', default='', help="The team's bots sheet (address or id), built in as the sheet "
                        'of every Studio that has not chosen one.')
    args=parser.parse_args()
    runtime=args.runtime.resolve()
    if not (runtime/'python.exe').is_file():
        raise SystemExit('Supply --runtime with a prepared Windows Python 3.13 runtime.')
    magic=subprocess.check_output([str(runtime/'python.exe'), '-c', 'import importlib.util; print(importlib.util.MAGIC_NUMBER.hex())'], text=True).strip()
    if magic!=importlib.util.MAGIC_NUMBER.hex():
        raise SystemExit('Run this builder using Python 3.13, matching the bundled runtime.')
    release=build_version.next_release()
    if args.publish:
        sys.path.insert(0,str(ROOT/'qbg_studio'))
        import updater
        release['releases']=updater.drive_folder_id(args.publish)
    if args.team_sheet:
        match=re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)',args.team_sheet)
        sheet=match[1] if match else args.team_sheet.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,}',sheet):
            raise SystemExit('--team-sheet must be a Google Sheet address or id.')
        release['team_sheet']=sheet
    team_key=bool(args.team_key)
    if team_key:
        try:
            base.say("Building in the bots' key for the team: "+team_key_only(args.team_key))
        except ValueError as exc:
            raise SystemExit(str(exc))
    work_root=PROJECT/'artifacts/team-installer'
    work_root.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix=release['version']+'-', dir=work_root))
    package=work/'package'
    app=package/'_internal/app'
    app.mkdir(parents=True)
    base.say('Staging current source and a private Python runtime (no saved user data).')
    shutil.copytree(runtime,package/'_internal/python',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    manifest=[]
    for src,rel in source_files():
        target=app/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,target)
        manifest.append(dict(path=rel.as_posix(),sha256=hashlib.sha256(src.read_bytes()).hexdigest()))
    verify_runtime_imports(package)
    if team_key:
        (app/TEAM_KEY).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(args.team_key,app/TEAM_KEY)
    build_version.write_release(app/'qbg_studio/static/release.json',release)
    # The app's Google sign-in registration travels with the installer so a
    # teammate only signs in. No user's sign-in or token is included; the bots'
    # sheet key only with --team-key.
    write_registration(app)
    if team_key:
        sheet_step=("The team's sheet and its key are built in: the sheet card reads it\n"
                    "   by itself. A key you added earlier is kept, never replaced.")
        carries=("It includes the app's Google sign-in registration and the bots' sheet\n"
                 "key, so share this installer only with your team, never outside it.")
    else:
        sheet_step=("Configure the Google Sheet and its access key privately through Settings\n"
                    "   if you use sheet features. These credentials are not in this installer.")
        carries=("It does include the app's Google sign-in registration, so share this\n"
                 "installer only with your team. Ask your app administrator for sheet\n"
                 "setup through a private channel.")
    quick=f'''QBG STUDIO {release['version']} - TEAM SETUP

1. Run QBG_Studio_Setup.exe on Windows 10/11 (64 bit) - or double-click
   START_QBG_STUDIO.bat kept next to it: it installs or repairs QBG One,
   installs Chrome if missing, then starts QBG One. No Python or LibreOffice
   is needed; PDFs use Microsoft Word when present, else QBG One draws them.
2. Open QBG One from the desktop or Start menu (it was called QBG Studio
   before 4 October 2026). Python is included.
3. In Settings, press Sign in with Google and use your own allowed @pw.live
   account. Sign-in is already set up; there is nothing to configure.
   Request access to the QBG Automation app if your account is not allowed.
4. {sheet_step}
5. Open the portal browser in QBG One and sign into your own QBG account.
6. Install Chrome and Pandoc when offered if they are missing.

Autopilot (first item under Papers) takes one paper through reading, upload,
solutions, translation and tagging in a single run and ends with one report.
Tagging uses GPT Sol and offers direct upload without individual approval.
Bulk translation, solution generation, Word / Any format paper upload,
editable previews and run reports are included.

New installations start with empty batches and run history. Updates keep the
existing installation's batches, runs, reports and settings. Do not uninstall
or choose another installation folder when updating.
The package contains no saved papers or account sessions.
{carries}

QBG One runs in the Windows notification area. Right-click its icon to quit.
Existing installed papers, settings and runs are preserved when upgrading.
Keep backups of your installation's _internal/app/qbg_studio/data folder.
'''
    (package/'QUICK_START.txt').write_text(quick,encoding='utf-8')
    verify_clean(package,team_key)
    modules=compile_app(app)
    framework=Path(os.getenv('WINDIR',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319'
    launcher_manifest=base.CACHE/'single_exe/launcher.manifest'
    base.run([str(framework/'csc.exe'),'/nologo','/target:winexe','/platform:x64','/optimize+',
              '/win32icon:'+str(app/'qbg_studio/static/icons/qbg-studio.ico'),
              '/r:System.Windows.Forms.dll','/r:System.Drawing.dll','/win32manifest:'+str(launcher_manifest),
              '/out:'+str(package/'QBG_Studio.exe'),str(ROOT/'desktop_launcher.cs')])
    verify_clean(package,team_key)
    verification=smoke_test(package,work,release)
    # The personal installer script has explicit private-data entries. Omit
    # those in the team build; the application creates its database at startup.
    # With --team-key the bots' key entry stays: it installs only where there
    # is no key yet.
    private=('qbg_studio\\data\\*','qbg_studio\\uploads\\*')+(() if team_key else ('1_Existing_Bots_Windows_Ready\\credentials.json',))
    script=(ROOT/'qbg_setup.iss').read_text(encoding='utf-8')
    script='\n'.join(line for line in script.splitlines() if not any(line.startswith('Source: "{#PackageDir}\\_internal\\app\\'+rel) for rel in private))+'\n'
    verify_upgrade_rules(package, script, team_key)
    iss=work/'team_setup.iss'
    iss.write_text(script,encoding='utf-8')
    destination=PROJECT/'dist/team'/release['version']
    destination.mkdir(parents=True,exist_ok=False)
    compiler=base.CACHE/'installer_tools/InnoSetup/ISCC.exe'
    base.say('Building the single shareable setup EXE.')
    base.run([str(compiler),'/DPackageDir='+str(package),'/DOutputDir='+str(destination),
              '/DAppVersion='+release['version'],str(iss)])
    installer=destination/'QBG_Studio_Setup.exe'
    assert installer.read_bytes()[:2]==b'MZ' and installer.stat().st_size>10_000_000
    shutil.copy2(package/'QUICK_START.txt',destination/'QUICK_START.txt')
    # Kept next to the installer in the release folder: installs or repairs, then starts Studio.
    shutil.copy2(ROOT/'START_QBG_STUDIO.bat',destination/'START_QBG_STUDIO.bat')
    checksum=hashlib.sha256(installer.read_bytes()).hexdigest()
    (destination/'SHA256.txt').write_text(checksum+'  '+installer.name+'\n',encoding='utf-8')
    (work/'verification.json').write_text(json.dumps(dict(release=release,modules=modules,source_files=manifest,
        verification=verification,installer_sha256=checksum),indent=2),encoding='utf-8')
    build_version.write_release(build_version.RELEASE_FILE,release)
    build_version.RELEASE_FILE.write_bytes(build_version.RELEASE_FILE.read_bytes().replace(b'\r\n',b'\n'))
    base.say('Ready: '+str(installer)+' (%.1f MB)'%(installer.stat().st_size/1e6))
    # The versioned copy is what goes into the release folder: dropped in by
    # hand it keeps the name every Studio looks for.
    versioned=destination/('QBG_Studio_Setup_%s.exe'%release['version'])
    shutil.copy2(installer,versioned)
    # Its fixes in brief, one per line: dropped in beside the installer, they
    # are what the update pop-up shows every teammate (updater.read_notes).
    notes_file=release_notes_file(destination,release['version'],args.notes)
    if args.publish:
        base.say('Publishing to the release folder.')
        try:
            published=updater.publish(installer,release['version'],args.publish,notes=args.notes)
            base.say('Published: '+published['link'])
        except PermissionError as exc:
            base.say('Not uploaded: '+str(exc))
            base.say('To publish by hand, drop this file into the release folder: '+str(versioned))
            if notes_file:
                base.say('...and this one beside it, for the update pop-up: '+str(notes_file))
    _prune_old_builds()


def release_notes_file(destination,version,notes):
    """QBG_Studio_Setup_<version>.txt with one fix per line (the --notes split on ';'), or None."""
    if str(ROOT/'qbg_studio') not in sys.path:
        sys.path.insert(0,str(ROOT/'qbg_studio'))
    import updater
    lines=updater.note_lines(notes,limit=20)
    if not lines:
        return None
    path=Path(destination)/('QBG_Studio_Setup_%s.txt'%version)
    path.write_bytes(('\n'.join(lines)+'\n').encode('utf-8'))
    return path


if __name__=='__main__':
    main()
