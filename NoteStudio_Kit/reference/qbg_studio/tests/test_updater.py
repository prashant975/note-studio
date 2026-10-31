"""The Update button: the release folder on Drive, a verified download
through the Drive API, a helper that reinstalls and restarts, and publishing
from the build."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'qbg_studio'), str(ROOT / '2_Question_Creation_Pipeline'), str(ROOT)]
import db
import updater


class Reply:
    def __init__(self, status=200, body=None, headers=None, text='', content=b''):
        self.status_code, self._body, self.headers, self.text, self._content = status, body, headers or {}, text, content

    def json(self):
        return self._body

    def iter_content(self, size):
        for i in range(0, len(self._content), size):
            yield self._content[i:i + size]


FILES = [dict(id='F5', name='QBG_Studio_Setup_2026.9.24.5.exe', size='97484076', sha256Checksum='A' * 64,
              modifiedTime='2026-09-24T23:41:00.000Z', description='today'),
         dict(id='F1', name='QBG_Studio_Setup_2026.9.25.1.exe', size='97484100', sha256Checksum='b' * 64,
              modifiedTime='2026-09-25T10:00:00.000Z', description='update button'),
         dict(id='X', name='notes.txt', size='10'),
         dict(id='F9', name='QBG_Studio_Setup_2026.9.25.9.exe', size='5', modifiedTime='2026-09-25T11:00:00.000Z')]


class Drive:
    """The Drive calls the updater makes, recorded."""

    def __init__(self, files=None, content=b'', list_status=200, media_status=200):
        self.files, self.content, self.list_status, self.media_status, self.calls = files or [], content, list_status, media_status, []

    def get(self, url, params=None, **kw):
        self.calls.append(('get', url, params))
        if 'alt=media' in url:
            return Reply(status=self.media_status, headers={'content-type': 'application/octet-stream',
                                                           'content-length': str(len(self.content))}, content=self.content)
        return Reply(status=self.list_status, body={'files': self.files})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch.object(db, 'DB_PATH', Path(self.dir.name) / 'studio.sqlite3')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()
        release = Path(self.dir.name) / 'release.json'
        release.write_text(json.dumps(dict(version='2026.9.24.5', updated_at='2026-09-24T23:41:12+05:30', releases='FOLDER_builtin1')), encoding='utf-8')
        patcher = patch.object(updater, 'RELEASE_FILE', release)
        patcher.start()
        self.addCleanup(patcher.stop)


class TheReleaseFolder(Base):
    def test_versions_compare_as_numbers(self):
        self.assertEqual(updater.parse_version('2026.9.25.1'), (2026, 9, 25, 1))
        self.assertIsNone(updater.parse_version('v1.2'))
        self.assertTrue(updater.is_newer('2026.10.1.1', '2026.9.30.9'))
        self.assertFalse(updater.is_newer('2026.9.24.5', '2026.9.24.5'))

    def test_the_folder_comes_from_the_build_unless_this_studio_chose_one(self):
        self.assertEqual(updater.source(), dict(folder_id='FOLDER_builtin1', own=False, built_in='FOLDER_builtin1'))
        updater.save_folder('https://drive.google.com/drive/folders/1268W3CYy9dsKVteeIIY0e2diRuwVYJOu?usp=drive_link')
        self.assertEqual(updater.source()['folder_id'], '1268W3CYy9dsKVteeIIY0e2diRuwVYJOu')
        self.assertTrue(updater.source()['own'])
        updater.save_folder('')
        self.assertFalse(updater.source()['own'])
        with self.assertRaises(ValueError):
            updater.save_folder('not a folder')

    def test_installers_are_read_by_name_and_only_verified_ones_count(self):
        rows = updater.parse_files(FILES)
        self.assertEqual([r['version'] for r in rows], ['2026.9.25.9', '2026.9.25.1', '2026.9.24.5'])
        self.assertEqual(rows[1]['sha256'], 'b' * 64)
        self.assertEqual(rows[2]['sha256'], 'a' * 64)
        self.assertEqual((rows[1]['size'], rows[1]['released'], rows[1]['notes']), (97484100, '2026-09-25 10:00', 'update button'))
        drive = Drive(FILES)
        result = updater.check(drive=drive)
        self.assertEqual((result['latest']['version'], result['available'], result['error']), ('2026.9.25.1', True, ''))
        self.assertIn("'FOLDER_builtin1' in parents", drive.calls[0][2]['q'])
        self.assertEqual(drive.calls[0][2]['supportsAllDrives'], 'true')
        status = updater.status()
        self.assertTrue(status['available'])
        self.assertEqual(status['latest']['file_id'], 'F1')

    def test_problems_reading_the_folder_are_told_plainly(self):
        self.assertIn('HTTP 403', updater.check(drive=Drive(list_status=403))['error'])
        only_unchecked = updater.check(drive=Drive([FILES[3]]))
        self.assertIsNone(only_unchecked['latest'])
        self.assertIn('not finished checking', only_unchecked['error'])
        db.set_setting(updater.FOLDER_SETTING, '')
        with patch.object(updater, 'release_info', return_value={'version': '2026.9.24.5'}):
            self.assertIn('No release folder', updater.check(drive=Drive(FILES))['error'])


class TheReleaseNotes(Base):
    """The update pop-up shows the release's fixes in brief (30 September 2026):
    the file's Drive description, or the build's .txt dropped in beside it."""

    class NotesDrive(Drive):
        def __init__(self, files, notes=b'', notes_status=200):
            super().__init__(files)
            self.notes, self.notes_status = notes, notes_status

        def get(self, url, params=None, **kw):
            if (params or {}).get('alt') == 'media':
                self.calls.append(('media', url, params))
                reply = Reply(status=self.notes_status)
                reply.content = self.notes
                return reply
            return super().get(url, params, **kw)

    def test_a_notes_file_beside_the_installer_is_read_when_the_installer_has_no_description(self):
        files = [dict(id='F2', name='QBG_Studio_Setup_2026.9.30.2.exe', size='97484100', sha256Checksum='c' * 64,
                      modifiedTime='2026-09-30T10:00:00.000Z'),
                 dict(id='N2', name='QBG_Studio_Setup_2026.9.30.2.txt', size='80')]
        self.assertEqual(updater.parse_files(files)[0]['notes_file'], 'N2')
        drive = self.NotesDrive(files, notes='Autopilot checks cost less\n- PDFs without Word\n'.encode('utf-8'))
        result = updater.check(drive=drive)
        self.assertEqual(result['latest']['notes'], 'Autopilot checks cost less\n- PDFs without Word')
        self.assertEqual([c for c in drive.calls if c[0] == 'media'][0][1], updater.FILES + '/N2')
        self.assertEqual(updater.status()['notes'], ['Autopilot checks cost less', 'PDFs without Word'])

    def test_a_description_wins_and_the_notes_file_is_not_fetched(self):
        files = [dict(id='F2', name='QBG_Studio_Setup_2026.9.30.2.exe', size='1', sha256Checksum='c' * 64,
                      description='One; Two'),
                 dict(id='N2', name='QBG_Studio_Setup_2026.9.30.2.txt')]
        drive = self.NotesDrive(files, notes=b'ignored')
        updater.check(drive=drive)
        self.assertFalse([c for c in drive.calls if c[0] == 'media'])
        self.assertEqual(updater.status()['notes'], ['One', 'Two'])

    def test_notes_that_cannot_be_read_never_stop_the_update(self):
        files = [dict(id='F2', name='QBG_Studio_Setup_2026.9.30.2.exe', size='1', sha256Checksum='c' * 64),
                 dict(id='N2', name='QBG_Studio_Setup_2026.9.30.2.txt')]
        result = updater.check(drive=self.NotesDrive(files, notes_status=403))
        self.assertTrue(result['available'])
        self.assertEqual(result['latest']['notes'], '')
        self.assertEqual(updater.status()['notes'], [])
        self.assertEqual(updater.read_notes('N2', drive=self.NotesDrive(files, notes=b'x' * 9000)), 'x' * updater.NOTES_BYTES)

    def test_note_lines_are_short_unique_and_without_bullets(self):
        self.assertEqual(updater.note_lines('- One\r\n* Two\n• Three; 4) Four;  One'), ['One', 'Two', 'Three', 'Four'])
        self.assertEqual(len(updater.note_lines(';\n'.join('fix %d' % i for i in range(20)))), 8)
        self.assertEqual(updater.note_lines(None), [])
        self.assertEqual(len(updater.note_lines('x' * 500)[0]), 200)

    def test_the_build_writes_the_notes_file_beside_the_versioned_installer(self):
        import build_team_installer
        path = build_team_installer.release_notes_file(self.dir.name, '2026.9.30.2', 'Autopilot checks cost less; PDFs without Word')
        self.assertEqual(path.name, 'QBG_Studio_Setup_2026.9.30.2.txt')
        self.assertEqual(path.read_bytes(), b'Autopilot checks cost less\nPDFs without Word\n')
        self.assertIsNone(build_team_installer.release_notes_file(self.dir.name, '2026.9.30.3', ''))

    def test_every_page_carries_the_pop_up(self):
        script = (ROOT / 'qbg_studio' / 'static' / 'release.js').read_text(encoding='utf-8')
        self.assertIn('showModal', script)
        self.assertIn('qbg-update-dismissed', script)
        self.assertIn('textContent = line', script)        # notes are text, never HTML
        self.assertNotIn('innerHTML = line', script)
        css = (ROOT / 'qbg_studio' / 'static' / 'style.css').read_text(encoding='utf-8')
        self.assertIn('.update-dialog', css)


class TheDownload(Base):
    def test_the_file_comes_through_the_api_and_must_match_drives_hash(self):
        payload = b'MZ' + bytes(range(256)) * 10
        entry = dict(version='2026.9.25.1', file_id='F1', sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
        drive = Drive(content=payload)
        target = Path(self.dir.name) / 'setup.exe'
        steps = []
        self.assertEqual(updater.download(entry, target, drive=drive, progress=lambda d, t: steps.append((d, t))), len(payload))
        self.assertEqual(target.read_bytes(), payload)
        self.assertIn('/files/F1?alt=media&supportsAllDrives=true', drive.calls[0][1])
        self.assertEqual(steps[-1], (len(payload), len(payload)))
        with self.assertRaises(ValueError):
            updater.download(dict(entry, sha256='0' * 64), target, drive=Drive(content=payload))
        with self.assertRaises(ValueError):
            updater.download(dict(entry, sha256=''), target, drive=Drive(content=payload))
        with self.assertRaises(ValueError):
            updater.download(dict(entry, size=len(payload) + 1), target, drive=Drive(content=payload))
        with self.assertRaises(ValueError):
            updater.download(entry, target, drive=Drive(content=payload, media_status=403))


class TheHandOver(Base):
    def root(self):
        root = Path(self.dir.name) / 'Programs' / 'QBGStudio'
        (root / '_internal' / 'python').mkdir(parents=True)
        (root / 'QBG_Studio.exe').write_bytes(b'MZ')
        return root

    def test_installed_root_is_found_from_the_bundled_python_and_not_from_source(self):
        root = self.root()
        self.assertEqual(updater.installed_root(root / '_internal' / 'python' / 'python.exe'), root)
        self.assertIsNone(updater.installed_root(Path(self.dir.name) / 'python.exe'))

    def test_install_downloads_writes_the_helper_and_starts_it_detached(self):
        root = self.root()
        payload = b'MZ setup'
        entry = dict(version='2026.9.25.1', file_id='F1', sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
        started = []
        with patch.object(updater, 'installed_root', return_value=root), \
             patch.object(updater.tempfile, 'gettempdir', return_value=self.dir.name):
            log = updater.install(entry, drive=Drive(content=payload), launcher=started.append)
        argv = started[0]
        self.assertEqual(argv[:5], ['cmd', '/c', 'start', '', '/min'])
        self.assertIn('powershell', argv)
        self.assertEqual(argv[argv.index('-Root') + 1], str(root))
        script = Path(argv[argv.index('-File') + 1]).read_text(encoding='utf-8')
        self.assertIn("'/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES'", script)
        self.assertIn('/LOG="', script)
        self.assertIn("Join-Path $Root 'QBG_Studio.exe'", script)
        self.assertEqual(Path(argv[argv.index('-Setup') + 1]).read_bytes(), payload)
        self.assertTrue(str(log).endswith('install_2026.9.25.1.log'))
        with patch.object(updater, 'installed_root', return_value=None), self.assertRaises(ValueError):
            updater.install(entry, drive=Drive(content=payload), launcher=started.append)

    def test_run_update_ends_with_exit_zero_and_a_failure_is_reported(self):
        exits = []
        with patch.object(updater, 'install', return_value=Path(self.dir.name) / 'i.log'), \
             patch.object(updater.time, 'sleep', lambda s: None):
            self.assertTrue(updater.run_update(dict(version='2026.9.25.1', size=5), exit_process=lambda: exits.append(0)))
        self.assertEqual(exits, [0])
        self.assertEqual(updater.settle_state()['state'], 'installing')
        with patch.object(updater, 'install', side_effect=ValueError('The download failed (HTTP 403).')):
            self.assertFalse(updater.run_update(dict(version='2026.9.25.1'), exit_process=lambda: exits.append(1)))
        self.assertEqual(exits, [0])
        self.assertIn('HTTP 403', updater.settle_state()['error'])

    def test_start_update_refuses_what_must_not_run(self):
        with self.assertRaises(ValueError):
            updater.start_update()                                  # nothing checked yet
        db.set_setting(updater.CHECKED, json.dumps(dict(latest=dict(version='2026.9.25.1', file_id='F1', sha256='b' * 64, size=0))))
        with self.assertRaises(ValueError):
            updater.start_update()                                  # running from source
        with patch.object(updater, 'installed_root', return_value=Path(self.dir.name)), \
             patch.object(updater, 'run_update', lambda entry: None):
            updater.start_update().join()
            with self.assertRaises(ValueError):
                updater.start_update()                              # one is already going

    def test_settle_state_reads_what_the_helper_left_behind(self):
        log = Path(self.dir.name) / 'install_2026.9.25.1.log'
        updater._state(state='installing', version='2026.9.25.1', log=str(log))
        Path(str(log) + '.result').write_text('exit 5', encoding='utf-8')
        self.assertIn('exit 5', updater.settle_state()['error'])
        updater._state(state='installing', version='2026.9.24.5', log=str(log))
        self.assertEqual(updater.settle_state()['state'], 'done')


class Publishing(Base):
    def installer(self):
        path = Path(self.dir.name) / 'QBG_Studio_Setup.exe'
        path.write_bytes(b'MZ' * 100)
        return path

    def test_publish_uploads_under_the_name_every_studio_looks_for(self):
        calls = []

        class Uploads:
            def post(self, url, json=None, headers=None, timeout=None):
                calls.append(('post', url, json))
                return Reply(headers={'Location': 'https://upload/session'})

            def put(self, url, data=None, headers=None, timeout=None):
                calls.append(('put', url, len(data)))
                return Reply(body={'id': 'FILE1'})
        result = updater.publish(self.installer(), '2026.9.25.1', 'https://drive.google.com/drive/folders/FOLDER_abc123', notes='n', drive=Uploads())
        self.assertEqual(result['link'], 'https://drive.google.com/file/d/FILE1/view')
        self.assertEqual(result['sha256'], hashlib.sha256(b'MZ' * 100).hexdigest())
        self.assertIn('uploadType=resumable&supportsAllDrives=true', calls[0][1])
        self.assertEqual(calls[0][2], {'name': 'QBG_Studio_Setup_2026.9.25.1.exe', 'parents': ['FOLDER_abc123'], 'description': 'n'})
        self.assertEqual(calls[1][2], 200)

    def test_a_service_accounts_empty_quota_is_explained(self):
        class Refuses:
            def post(self, url, json=None, headers=None, timeout=None):
                return Reply(headers={'Location': 'https://upload/session'})

            def put(self, url, data=None, headers=None, timeout=None):
                return Reply(status=403, text='{"error": {"message": "Service Accounts do not have storage quota.", '
                                              '"errors": [{"reason": "storageQuotaExceeded"}]}}')
        with self.assertRaises(PermissionError) as caught:
            updater.publish(self.installer(), '2026.9.25.1', 'FOLDER_abc123', drive=Refuses())
        self.assertIn('Drop the file into the folder by hand', str(caught.exception))


class TheScreens(Base):
    def client(self):
        import app
        import pipeline_bridge as pb
        import pw_auth
        from fastapi.testclient import TestClient
        for target, name, value in ((pb, 'health', dict(checks=[], bad=0, warn=0, ready=True)),
                                    (pb, 'portal_session', dict(ok=False, source='none', hours=None, headline='No portal session.',
                                                                detail='', needs_fix=False)),
                                    (pw_auth, 'status', dict(configured=False, signed_in=False, usable=False, detail='Not signed in.',
                                                             email='', store_path='', redirect_uri='', live=False, html='',
                                                             encrypted_at_rest=False))):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pb._PW_CACHE['value'] = None
        return TestClient(app.app)

    def test_status_answers_and_install_refuses_without_a_newer_version(self):
        db.set_setting(updater.CHECKED, json.dumps(dict(checked_at=1e12, latest=None)))
        client = self.client()
        with patch('app._refresh_updates_soon') as refresh:
            status = client.get('/update/status').json()
        self.assertEqual(status['current'], '2026.9.24.5')
        self.assertFalse(status['available'])
        self.assertEqual(status['source']['folder_id'], 'FOLDER_builtin1')
        refresh.assert_not_called()
        reply = client.post('/update/install', headers={'accept': 'application/json'})
        self.assertEqual(reply.status_code, 400)
        self.assertIn('no newer version', reply.json()['error'])
        page = client.get('/settings').text
        self.assertIn('Check for updates', page)
        self.assertIn('QBG_Studio_Setup_&lt;version&gt;.exe', page)
        reply = client.post('/settings/updates', data={'folder': 'https://drive.google.com/drive/folders/FOLDER_own_1234'}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertEqual(updater.source()['folder_id'], 'FOLDER_own_1234')

    def test_the_header_has_the_slot_and_the_script_polls(self):
        self.assertIn('data-update-slot', (ROOT / 'qbg_studio/templates/base.html').read_text(encoding='utf-8'))
        script = (ROOT / 'qbg_studio/static/release.js').read_text(encoding='utf-8')
        self.assertIn('/update/status', script)
        self.assertIn('/update/install', script)
        self.assertIn('location.reload()', script)


if __name__ == '__main__':
    unittest.main()
