"""The queue (3 October 2026): "add multiple papers at once and then run one
time, then one by one all start reading and move ahead", in Autopilot and in
New batch, and "each queue is a different batch"."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote_plus as unquote

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'qbg_studio'), str(ROOT), str(ROOT / 'tests'), str(ROOT / '2_Question_Creation_Pipeline')]

import db  # noqa: E402
import jobs  # noqa: E402
import job_queue as queue  # noqa: E402
import job_queue_routes  # noqa: E402
import paper_ai as ai  # noqa: E402
import paper_routes  # noqa: E402
import autopilot_routes  # noqa: E402
import autopilot_runner as runner  # noqa: E402
import autopilot_service as service  # noqa: E402


def png(colour='white'):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (60, 40), colour).save(buffer, format='PNG')
    return buffer.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        patcher = patch.object(db, 'DB_PATH', self.tmp / 'data' / 'test.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / 'data').mkdir()
        db.init()
        ai.initialize()
        service.initialize()
        import tagging_service
        tagging_service.initialize()
        queue.initialize()
        self.addCleanup(jobs._STOP_REQUESTED.clear)
        self.launched = []

    def kind(self, name='reading', portal=False, ready=None, fail=None):
        """A stand-in task: starting it claims a job and remembers what it was given."""
        def launch(item):
            if fail and item['title'] in fail:
                raise ValueError(fail[item['title']])
            job, why = (db.claim_global_job('video-tagging', item['title']) if portal
                        else db.claim_job('paper-check', None, item['title']))
            if not job:
                raise queue.Busy(why)
            self.launched.append((item['title'], job, [(role, name, len(data)) for role, name, data in queue.files(item)]))
            return queue.Started([job], '/shown/%d' % job, lambda: None)
        queue.register(name, launch, label='Stand-in', portal=portal, ready=ready)
        self.addCleanup(queue.LAUNCHERS.pop, name, None)        # the real tasks' own entries stay
        return name

    def states(self):
        return [(i['title'], i['state']) for i in queue.query('SELECT * FROM job_queue ORDER BY id')]


class TheLine(Base):
    def test_the_first_starts_now_and_the_rest_start_one_by_one_in_order(self):
        kind = self.kind()
        first = queue.submit(kind, 'Paper 1', dict(n=1), [('mixed', 'one.pdf', b'11')])
        second = queue.submit(kind, 'Paper 2', dict(n=2), [('mixed', 'two.pdf', b'2222')])
        third = queue.submit(kind, 'Paper 3', dict(n=3))
        self.assertTrue(first.started and first.started.link.startswith('/shown/'))
        self.assertEqual((second.started, second.position, third.position), (None, 2, 3))
        self.assertEqual([title for title, _job, _files in self.launched], ['Paper 1'])
        self.assertTrue(queue.busy())
        # Nothing starts while Paper 1 is still going.
        self.assertIsNone(queue.start_next())
        jobs._finish(self.launched[0][1], True, 'Read 45 questions.')
        item, started = queue.start_next()
        self.assertEqual((item['title'], self.launched[1][2]), ('Paper 2', [('mixed', 'two.pdf', 4)]))
        self.assertEqual(self.states(), [('Paper 1', 'done'), ('Paper 2', 'running'), ('Paper 3', 'waiting')])
        self.assertEqual(queue.query('SELECT * FROM job_queue WHERE title=?', ('Paper 1',), True)['note'], 'Read 45 questions.')
        jobs._finish(self.launched[1][1], False, 'Could not read it.')          # a failed paper does not stop the line
        self.assertEqual(queue.start_next()[0]['title'], 'Paper 3')
        jobs._finish(self.launched[2][1], True, 'done')
        self.assertIsNone(queue.start_next())
        self.assertFalse(queue.busy())
        # The files of a paper are kept only until it has started.
        self.assertEqual(list((self.tmp / 'data' / 'queue').iterdir()), [])

    def test_a_task_that_writes_to_qbg_also_waits_for_the_portal(self):
        kind = self.kind('saving', portal=True)
        other, why = db.claim_global_job('bulk-translation', 'someone else')
        self.assertIsNotNone(other, why)
        waiting = queue.submit(kind, 'Run 1', {})
        self.assertEqual((waiting.started, waiting.position, self.launched), (None, 1, []))
        jobs._finish(other, True, 'done')
        self.assertEqual(queue.start_next()[0]['title'], 'Run 1')
        # A reading does not need the portal: it starts beside a portal job.
        jobs._finish(self.launched[0][1], True, 'done')
        other, _ = db.claim_global_job('bulk-translation', 'someone else')
        self.assertTrue(queue.submit(self.kind('reading'), 'Paper', {}).started)

    def test_one_that_cannot_start_is_reported_and_the_line_moves_on(self):
        kind = self.kind(fail={'Bad paper': 'The file could not be opened.'})
        going = queue.submit(kind, 'Paper 1', {})
        queue.submit(kind, 'Bad paper', {})
        queue.submit(kind, 'Paper 3', {})
        jobs._finish(self.launched[0][1], True, 'done')
        self.assertEqual(queue.start_next()[0]['title'], 'Paper 3')
        bad = queue.query('SELECT * FROM job_queue WHERE title=?', ('Bad paper',), True)
        self.assertEqual((bad['state'], bad['note']), ('failed', 'The file could not be opened.'))
        self.assertTrue(going.started)
        # Added when nothing is before it, the reason goes straight back to the person.
        jobs._finish(self.launched[1][1], True, 'done')
        result = queue.submit(kind, 'Bad paper', {})
        self.assertEqual((result.started, result.error), (None, 'The file could not be opened.'))

    def test_a_waiting_paper_can_be_removed_and_leaves_nothing_behind(self):
        kind = self.kind()
        queue.submit(kind, 'Paper 1', {})
        second = queue.submit(kind, 'Paper 2', {}, [('mixed', 'two.pdf', b'22')])
        third = queue.submit(kind, 'Paper 3', {}, [('mixed', 'three.pdf', b'33')])
        folder = Path(queue.query('SELECT * FROM job_queue WHERE id=?', (second.item_id,), True)['folder'])
        self.assertTrue((folder / '0-two.pdf').exists())
        queue.remove(second.item_id)
        self.assertFalse(folder.exists())
        self.assertEqual(queue.position(third.item_id), 2)
        with self.assertRaisesRegex(ValueError, 'still waiting'):
            queue.remove(queue.query("SELECT id FROM job_queue WHERE state='running'", one=True)['id'])
        self.assertEqual(queue.clear(), 1)
        self.assertEqual(len(self.launched), 1)                       # nothing was ever created for the removed ones
        self.assertEqual(db.active_jobs()[0]['id'], self.launched[0][1])

    def test_stop_an_expired_sign_in_and_a_restart_hold_the_line(self):
        signed_in = [True]
        kind = self.kind(ready=lambda: '' if signed_in[0] else 'The PW sign-in is needed. Sign in, then continue the queue.')
        queue.submit(kind, 'Paper 1', {})
        queue.submit(kind, 'Paper 2', {})
        queue.submit(kind, 'Paper 3', {})
        # Stop on the paper that is running: the next one is not started over it.
        jobs.request_stop(self.launched[0][1])
        jobs._finish(self.launched[0][1], False, 'Stopped by you.')
        self.assertTrue(queue.paused())
        self.assertIn('You stopped "Paper 1"', queue.overview()['note'])
        self.assertIsNone(queue.tick())
        queue.resume()
        began = queue.tick()
        self.assertEqual(began[0]['title'], 'Paper 2')
        # The sign-in has gone by the time the next one is due: hold, with the reason.
        jobs._finish(self.launched[1][1], True, 'done')
        signed_in[0] = False
        self.assertIsNone(queue.tick())
        self.assertEqual((queue.paused(), queue.overview()['note']),
                         (True, 'The PW sign-in is needed. Sign in, then continue the queue.'))
        self.assertEqual(len(self.launched), 2)
        # Studio is closed and opened again: what waits stays, on hold.
        queue.resume()
        queue.recover()
        self.assertTrue(queue.paused())
        self.assertIn('Studio was restarted', queue.overview()['note'])
        signed_in[0] = True
        queue.resume()
        self.assertEqual(queue.tick()[0]['title'], 'Paper 3')
        # A Stop on a job the queue did not start, or with nothing waiting, changes nothing.
        jobs.request_stop(self.launched[2][1])
        self.assertFalse(queue.paused())

    def test_the_portal_taken_a_moment_before_keeps_the_place_and_a_closed_studio_is_noticed(self):
        kind = self.kind('saving', portal=True)
        launch = queue.LAUNCHERS[kind]['launch']
        taken = [True]                       # the look said free; the claim, a moment later, says no

        def claim(item):
            if taken[0]:
                raise queue.Busy('Another QBG portal job is already running.')
            return launch(item)
        queue.LAUNCHERS[kind]['launch'] = claim
        result = queue.submit(kind, 'Run 1', {})
        self.assertEqual((result.started, result.error, self.states()), (None, '', [('Run 1', 'waiting')]))
        taken[0] = False
        self.assertEqual(queue.start_next()[0]['title'], 'Run 1')
        # An entry left "running" with no job is a start Studio never finished.
        jobs._finish(self.launched[0][1], True, 'done')
        queue.submit(self.kind('reading'), 'Paper', {})
        queue._update(queue.query("SELECT id FROM job_queue WHERE title='Paper'", one=True)['id'], job_ids='[]')
        queue.recover()
        self.assertEqual(self.states()[-1], ('Paper', 'failed'))


class TwoLines(Base):
    """4 October 2026: reading papers and work on QBG run side by side, each line one at a time."""

    def test_reading_and_qbg_work_run_side_by_side_each_in_order(self):
        reading, saving = self.kind('reading'), self.kind('saving', portal=True)
        a = queue.submit(reading, 'Read A', {})
        b = queue.submit(saving, 'Upload B', {})
        c = queue.submit(reading, 'Read C', {})
        d = queue.submit(saving, 'Upload D', {})
        self.assertTrue(a.started and b.started)                      # one in each line, at once
        self.assertEqual((c.started, c.position, d.started, d.position), (None, 2, None, 2))
        self.assertEqual([title for title, _job, _files in self.launched], ['Read A', 'Upload B'])
        overview = queue.overview()
        self.assertEqual([(i['title'], i['lane_label']) for i in overview['running']], [('Read A', 'Reading'), ('Upload B', 'QBG')])
        # The QBG line's job ends first: only Upload D starts; the reading line is still busy with A.
        jobs._finish(self.launched[1][1], True, 'done')
        with patch.object(queue.threading, 'Thread'):
            began = queue.tick()
        self.assertEqual(began[0]['title'], 'Upload D')
        self.assertEqual(self.states()[2], ('Read C', 'waiting'))
        jobs._finish(self.launched[0][1], True, 'done')
        with patch.object(queue.threading, 'Thread'):
            self.assertEqual(queue.tick()[0]['title'], 'Read C')

    def test_a_papers_own_upload_waits_for_its_reading_and_the_other_line_goes_on(self):
        reading = self.kind('reading')
        read_a = queue.submit(reading, 'Read A', {})
        self.assertTrue(read_a.started)

        def upload(item):
            raise jobs.BatchBusyError('This paper is busy with another step. Wait for it to finish before changing the review sheet.')
        queue.register('upload-a', upload, label='Upload to QBG', portal=True)
        self.addCleanup(queue.LAUNCHERS.pop, 'upload-a', None)
        waiting = queue.submit('upload-a', 'Upload A', {})
        self.assertEqual((waiting.started, waiting.error), (None, ''))
        entry = queue.query('SELECT * FROM job_queue WHERE id=?', (waiting.item_id,), True)
        self.assertEqual(entry['state'], 'waiting')
        self.assertIn('busy with another step', entry['note'])
        # A reading added now still starts once A's line is free; the waiting upload does not block it.
        queue.submit(reading, 'Read B', {})
        jobs._finish(self.launched[0][1], True, 'done')
        with patch.object(queue.threading, 'Thread'):
            began = queue.tick()
        self.assertEqual(began[0]['title'], 'Read B')
        self.assertEqual(queue.query('SELECT state FROM job_queue WHERE id=?', (waiting.item_id,), True)['state'], 'waiting')


class Screens(Base):
    def client(self):
        env = Environment(loader=FileSystemLoader(ROOT / 'qbg_studio/templates'), autoescape=True)
        env.globals.update(asset=lambda p: '/static/' + p, run_time=lambda t: None, ago=lambda t: '',
                           queue_overview=queue.overview, exam_categories=['NEET-JEE', 'NEET', 'JEE'])

        def render(request, name, **ctx):
            return HTMLResponse(env.get_template(name).render(request=request, nav={}, preflight={},
                                                              here=request.url.path, **ctx))

        def go(path, message='', kind=''):
            return RedirectResponse(path + ('?toast=' + message if message else ''), status_code=303)
        app = FastAPI()
        guard = lambda request, action: None                                  # noqa: E731
        autopilot_routes.register(app, render, go, guard)
        paper_routes.register(app, render, guard)
        job_queue_routes.register(app, render, go)
        return TestClient(app)

    def finish(self, job_id, *args, **kwargs):
        jobs._finish(job_id, True, 'Read 3 question records.')


class NewBatch(Screens):
    def post(self, client, names, **data):
        files = [('question_files', (name, png(colour), 'image/png')) for name, colour in names]
        return client.post('/batches/ai', data=dict({'exam_category': 'JEE'}, **data), files=files, follow_redirects=False)

    def test_each_file_is_its_own_batch_read_one_after_another(self):
        client = self.client()
        page = client.get('/new/any-format').text
        self.assertIn('name="separate_papers"', page)
        self.assertIn('Each file is a separate paper', page)
        read = []
        with patch.object(paper_routes.ai, 'run_import', side_effect=lambda job, bid, **kw: read.append(bid)) as reading:
            reply = self.post(client, [('Physics.png', 'white'), ('Chemistry.png', 'ivory'), ('Maths.png', 'beige')],
                              separate_papers='true')
        self.assertEqual(reply.status_code, 303, reply.text[:300])
        self.assertEqual(reading.call_count, 1)                           # only the first is being read
        batches = db.list_batches()
        self.assertEqual([b['name'] for b in batches], ['Physics'])       # no batch yet for the papers that wait
        self.assertIn('/batches/%d/paper-preview' % batches[0]['id'], reply.headers['location'])
        self.assertIn('2 papers more in the queue', unquote(reply.headers['location']))
        self.assertEqual(self.states(), [('Physics', 'running'), ('Chemistry', 'waiting'), ('Maths', 'waiting')])
        card = client.get('/new/any-format').text
        self.assertIn('Queue · 3', card)
        self.assertIn('Running now', card)
        self.assertIn('hx-get="/work-queue/card"', card)
        # Physics finishes: Chemistry starts by itself, as a batch of its own with only its own file.
        with patch.object(paper_routes.ai, 'run_import', side_effect=lambda job, bid, **kw: read.append(bid)):
            jobs._finish(db.running_job(batches[0]['id'])['id'], True, 'Read 30 question records.')
            item, started = queue.start_next()
            started.work()
            self.assertEqual(item['title'], 'Chemistry')
            jobs._finish(db.running_job(read[-1])['id'], True, 'Read 30 question records.')
            queue.start_next()[1].work()
        names = {b['id']: b['name'] for b in db.list_batches()}
        self.assertEqual(sorted(names.values()), ['Chemistry', 'Maths', 'Physics'])
        self.assertEqual([names[bid] for bid in read], ['Physics', 'Chemistry', 'Maths'])
        for bid in read:
            manifest = ai.info(bid)['manifest']
            self.assertEqual([f['name'] for f in manifest], ['0-%s.png' % names[bid]])
            self.assertTrue(Path(manifest[0]['path']).exists())
        self.assertEqual(len({db.get_batch(bid)['out_dir'] for bid in read}), 3)

    def test_without_the_tick_the_files_are_one_paper_and_a_second_paper_waits_its_turn(self):
        client = self.client()
        with patch.object(paper_routes.ai, 'run_import'):
            self.post(client, [('Page 1.png', 'white'), ('Page 2.png', 'ivory')])
            first = db.list_batches()
            self.assertEqual(len(first), 1)
            self.assertEqual(len(ai.info(first[0]['id'])['manifest']), 2)
            # Another paper added while that one is being read joins the queue instead of reading beside it.
            reply = self.post(client, [('Second paper.png', 'beige')])
        self.assertEqual(len(db.list_batches()), 1)
        self.assertIn('/new/any-format', reply.headers['location'])
        self.assertIn('1 paper added to the queue (place 2)', unquote(reply.headers['location']))

    def test_what_cannot_be_split_into_papers_is_said_before_anything_is_added(self):
        client = self.client()
        both = [('question_files', ('A.png', png(), 'image/png')), ('question_files', ('B.png', png('ivory'), 'image/png')),
                ('solution_files', ('Key.png', png('beige'), 'image/png'))]
        reply = client.post('/batches/ai', data={'exam_category': 'JEE', 'separate_papers': 'true'}, files=both,
                            follow_redirects=False)
        self.assertEqual(reply.status_code, 200)
        self.assertIn('a separate answer file cannot be matched to its paper', reply.text)
        self.assertIn('data-separate-papers checked', reply.text)
        self.assertEqual((db.list_batches(), self.states()), ([], []))


class Autopilot(Screens):
    def start(self, client, names, **data):
        values = dict(exam_category='JEE', do_tagging='false', do_solutions='false', answer_source='paper', solution_language='English')
        values.update(data)
        files = [('files', (name, png(colour), 'image/png')) for name, colour in names]
        return client.post('/autopilot/runs', data=values, files=files, follow_redirects=False)

    def test_several_papers_become_several_runs_one_after_another(self):
        client = self.client()
        ready = dict(checks=[dict(key='pw', ok=True, label='PW sign-in', detail='', href='/settings'),
                             dict(key='session', ok=True, label='QBG session', detail='', href='/')], ready=True, queueing=False)
        with patch.object(autopilot_routes, 'readiness', return_value=ready), patch.object(runner, 'run') as run:
            reply = self.start(client, [('Mock 1.png', 'white'), ('Mock 2.png', 'ivory'), ('Mock 3.png', 'beige')],
                               separate_papers='true')
            self.assertEqual(reply.status_code, 303, reply.text[:300])
            runs = service.history()
            self.assertEqual([r['label'] for r in runs], ['Mock 1'])
            first = runs[0]['job_id']
            run.assert_called_once_with(first)
            self.assertIn('/autopilot/runs/%d' % first, reply.headers['location'])
            self.assertIn('2 runs more in the queue', unquote(reply.headers['location']))
            self.assertEqual(self.states(), [('Mock 1', 'running'), ('Mock 2', 'waiting'), ('Mock 3', 'waiting')])
            # While it runs, a paper added from the form joins the end of the line - no refusal.
            reply = self.start(client, [('Extra.png', 'azure')])
            self.assertIn('1 run added to the queue (place 4)', unquote(reply.headers['location']))
            # The run ends: the queue starts Mock 2 by itself, in its own batch.
            jobs._finish(first, False, '12 question(s) on QBG; 0 never uploaded; 1 need attention.')
            began = queue.tick()
            self.assertEqual(began[0]['title'], 'Mock 2')
        runs = {r['label']: r for r in service.history()}
        self.assertEqual(sorted(runs), ['Mock 1', 'Mock 2'])
        config = service.details(runs['Mock 2']['job_id'])['config']
        self.assertEqual((config['exam_category'], config['label'], config['do_tagging']), ('JEE', 'Mock 2', False))
        batch = db.get_batch(config['batch_id'])
        self.assertEqual(batch['name'], 'Autopilot - Mock 2')
        self.assertEqual([f['name'] for f in ai.info(batch['id'])['manifest']], ['0-Mock 2.png'])
        self.assertNotEqual(batch['out_dir'], db.get_batch(service.details(first)['config']['batch_id'])['out_dir'])
        self.assertEqual(self.states()[:2], [('Mock 1', 'done'), ('Mock 2', 'running')])

    def test_without_the_tick_all_files_are_one_run_and_the_page_says_when_a_run_will_wait(self):
        client = self.client()
        with patch.object(runner, 'run'):
            self.start(client, [('Paper.png', 'white'), ('Answer key.png', 'ivory')])
        runs = service.history()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(ai.info(service.details(runs[0]['job_id'])['config']['batch_id'])['manifest']), 2)
        signed = patch.object(autopilot_routes.pb, 'pw_state', return_value=dict(usable=True, email='me@pw.live'))
        connected = patch.object(autopilot_routes.pb, 'portal_session', return_value=dict(ok=True, headline='Connected'))
        with signed, connected:
            state = autopilot_routes.readiness()
        self.assertTrue(state['ready'])                                 # a busy portal no longer blocks Start
        self.assertTrue(state['queueing'])
        job = next(c for c in state['checks'] if c['key'] == 'job')
        self.assertEqual((job['ok'], job['label']), (True, 'Queue'))
        self.assertIn('waits in the queue and starts by itself', job['detail'])

    def test_the_queue_checks_the_sign_ins_before_it_starts_a_run(self):
        missing = dict(checks=[dict(key='pw', ok=True, label='PW sign-in', detail='', href=''),
                               dict(key='session', ok=False, label='QBG session', detail='Sign in to QBG.', href='')])
        with patch.object(autopilot_routes, 'readiness', return_value=missing):
            self.assertEqual(autopilot_routes.queue_ready(), 'QBG session is not ready: Sign in to QBG. Then continue the queue.')


class OtherTasks(Screens):
    def test_a_sheet_added_while_another_task_runs_waits_and_then_starts_by_itself(self):
        import video_tagging as vt
        import video_tagging_routes
        from test_video_tagging import FakeQBG, LINK, QID, question, workbook
        vt.initialize()
        app = FastAPI()
        go = lambda path, message='', kind='': RedirectResponse(path + ('?toast=' + message if message else ''), status_code=303)  # noqa: E731
        video_tagging_routes.register(app, lambda *a, **k: HTMLResponse(''), go, lambda request, action: None)
        client = TestClient(app)
        other, why = db.claim_global_job('bulk-translation', 'a translation is running')
        self.assertIsNotNone(other, why)
        sheet = {'sheet': ('videos.xlsx', workbook([(QID, 'english', 'update', '', LINK)]),
                           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
        reply = client.post('/video-tagging/runs', files=sheet, follow_redirects=False)
        # Not refused as it used to be: it waits, and nothing has been created for it.
        self.assertIn('1 sheet added to the queue (place 1)', unquote(reply.headers['location']))
        self.assertEqual((vt.history(), self.states()), ([], [('videos.xlsx', 'waiting')]))
        qbg = FakeQBG(**{QID: question()})
        with patch.object(vt.portal, 'Portal', return_value=qbg), patch.object(vt.pb, 'require_pw'):
            self.assertIsNone(queue.start_next())                       # the portal is still taken
            jobs._finish(other, True, 'done')
            item, started = queue.start_next()
            started.work()
        run = vt.details(started.job_ids[0])
        self.assertEqual((item['label'], started.link), ('Video tagging', '/video-tagging/runs/%d' % run['job_id']))
        self.assertEqual(([i['state'] for i in run['items']], run['source_name'], len(qbg.writes)), (['saved'], 'videos.xlsx', 1))
        # With nothing before it, a start is what it always was: straight to its run page.
        self.assertIsNone(queue.start_next())
        with patch.object(vt.portal, 'Portal', return_value=qbg), patch.object(vt.pb, 'require_pw'):
            reply = client.post('/video-tagging/runs', files=sheet, follow_redirects=False)
        self.assertRegex(reply.headers['location'], '^/video-tagging/runs/[0-9]+$')

    def test_every_task_that_writes_to_qbg_can_wait_in_the_queue(self):
        for module in ('bulk_translation_routes', 'sheet_tagging_routes', 'solution_routes', 'tagging_routes',
                       'verification_routes', 'video_tagging_routes'):
            __import__(module)                                    # each registers its task as it is loaded
        for kind in ('autopilot', 'bulk-translation', 'solution-run', 'tagging', 'video-tagging', 'sheet-tagging', 'verification'):
            self.assertTrue(queue.LAUNCHERS[kind]['portal'], kind)
        self.assertFalse(queue.LAUNCHERS['paper-read']['portal'])        # reading a paper does not write to QBG
        # A task's start that finds the portal taken at the last moment keeps its place.
        created = []

        def create(ids, actor=''):
            if not created:
                created.append('refused')
                raise ValueError('Another QBG portal job is already running. Wait for it to finish before starting this bot.')
            created.append((ids, actor))
            return db.claim_global_job('verification', 'x')[0]
        queue.task('stand-in', 'Stand-in', create, lambda run_id: None, '/runs/%d')
        for name in ('stand-in', 'strict'):
            self.addCleanup(queue.LAUNCHERS.pop, name, None)
        waiting = queue.submit_task('stand-in', 'Ids', [['a', 'b']], dict(actor='me'))
        self.assertEqual((waiting.started, waiting.error, self.states()), (None, '', [('Ids', 'waiting')]))
        item, started = queue.start_next()
        self.assertEqual((created[1], started.link), ((['a', 'b'], 'me'), '/runs/%d' % started.job_ids[0]))
        # Input the task refuses is an error for the person, not a place in the queue.
        queue.task('strict', 'Strict', lambda ids: (_ for _ in ()).throw(ValueError('Enter between 1 and 200 ids.')),
                   lambda run_id: None, '/runs/%d')
        jobs._finish(started.job_ids[0], True, 'done')
        refused = queue.submit_task('strict', 'Too many', [[]])
        self.assertEqual(queue.outcome([refused], '/form', 'run'), ('/form', 'Enter between 1 and 200 ids.', 'danger'))


class EveryQbgButton(Screens):
    """3 October 2026: "if I run any task - question creation, translation, tagging -
    even if the portal is busy it goes to the queue and then one by one it starts"."""

    def test_every_button_that_works_on_qbg_has_a_place_in_the_queue(self):
        __import__('app')                                   # registers Create and the bots
        for module in ('bulk_translation_routes', 'sheet_tagging_routes', 'solution_routes', 'tagging_routes',
                       'verification_routes', 'video_tagging_routes', 'autopilot_routes', 'paper_routes'):
            __import__(module)
        for kind in ('create', 'paper-upload', 'hindi-existing', 'bot', 'bulk-translation', 'bulk-translation-op',
                     'bulk-translation-checked', 'solution-run', 'solution-check', 'solution-write', 'tagging',
                     'tagging-write', 'tagging-sync', 'sheet-tagging', 'sheet-tagging-retry', 'video-tagging',
                     'video-tagging-retry', 'verification', 'verification-retry', 'autopilot', 'autopilot-resume',
                     'autopilot-leftovers'):
            self.assertIn(kind, queue.LAUNCHERS)
            self.assertTrue(queue.LAUNCHERS[kind]['portal'], kind)

    def test_taken_right_now_waits_and_anything_else_is_said(self):
        self.assertTrue(queue.waits(ValueError('Another QBG portal job is already running. Wait for it to finish.')))
        self.assertTrue(queue.waits(ValueError('This batch already has a job running.')))
        self.assertTrue(queue.waits(jobs.BatchBusyError('This paper is busy with another step.')))
        self.assertTrue(queue.waits(ValueError('That bot is already running.')))
        self.assertFalse(queue.waits(ValueError('No approved translation to upload.')))
        self.assertFalse(queue.waits(jobs.GateError('Preview first.')))

    def test_a_retry_while_qbg_is_busy_waits_says_why_and_starts_by_itself(self):
        import video_tagging as vt
        import video_tagging_routes
        from test_video_tagging import FakeQBG, LINK, QID, question
        vt.initialize()
        qbg = FakeQBG()                                     # the first pass cannot find the question
        with patch.object(vt.portal, 'Portal', return_value=qbg), patch.object(vt.pb, 'require_pw'):
            run_id = vt.create_run([('r1', QID, 'english', LINK, '')], 'videos.xlsx')
            vt.run(run_id)
        self.assertEqual(vt.details(run_id)['items'][0]['state'], 'failed')
        app = FastAPI()
        go = lambda path, message='', kind='': RedirectResponse(path + ('?toast=' + message if message else ''), status_code=303)  # noqa: E731
        video_tagging_routes.register(app, lambda *a, **k: HTMLResponse(''), go, lambda request, action: None)
        client = TestClient(app)
        other, why = db.claim_global_job('bulk-translation-write', 'Uploading approved translations...')
        self.assertIsNotNone(other, why)
        reply = client.post('/video-tagging/runs/%d/retry' % run_id, follow_redirects=False)
        self.assertIn('1 retry added to the queue (place 1)', unquote(reply.headers['location']))
        again = client.post('/video-tagging/runs/%d/retry' % run_id, follow_redirects=False)
        self.assertIn('already in the queue (place 1)', unquote(again.headers['location']))
        self.assertEqual(self.states(), [('Retry of run %d' % run_id, 'waiting')])
        # Its turn: the portal is free again, QBG now has the question.
        qbg.questions[QID] = question()
        jobs._finish(other, True, 'done')
        with patch.object(vt.portal, 'Portal', return_value=qbg), patch.object(vt.pb, 'require_pw'):
            item, started = queue.start_next()
            started.work()
        self.assertEqual((item['kind'], started.link), ('video-tagging-retry', '/video-tagging/runs/%d' % run_id))
        self.assertEqual(vt.details(run_id)['items'][0]['state'], 'saved')

    def test_a_start_that_finds_its_batch_busy_keeps_its_place_with_the_reason(self):
        def launch(item):
            raise ValueError('This batch already has a job running.')
        queue.register('busy-batch', launch, label='Stand-in', portal=False)
        self.addCleanup(queue.LAUNCHERS.pop, 'busy-batch', None)
        result = queue.submit('busy-batch', 'Upload', {}, key='busy-batch:1')
        self.assertEqual((result.started, result.error), (None, ''))
        entry = queue.query('SELECT * FROM job_queue WHERE id=?', (result.item_id,), True)
        self.assertEqual((entry['state'], entry['note']), ('waiting', 'Waiting: This batch already has a job running.'))
        self.assertIn('Waiting: This batch already has a job running.', self.client().get('/work-queue/card').text)


class TheCard(Screens):
    def test_the_buttons_hold_continue_remove_and_clear(self):
        kind = self.kind()
        queue.submit(kind, 'Paper 1', {})
        second = queue.submit(kind, 'Paper 2', {})
        queue.submit(kind, 'Paper 3', {})
        client = self.client()
        page = client.get('/work-queue').text
        self.assertIn('Queue · 3', page)
        self.assertIn('Hold the queue', page)
        self.assertIn('action="/work-queue/%d/remove"' % second.item_id, page)
        client.post('/work-queue/pause', follow_redirects=False)
        held = client.get('/work-queue/card').text
        self.assertIn('The queue is on hold', held)
        self.assertIn('Continue the queue', held)
        jobs._finish(self.launched[0][1], True, 'done')
        self.assertIsNone(queue.tick())
        reply = client.post('/work-queue/%d/remove' % second.item_id, headers={'referer': 'http://testserver/autopilot'},
                            follow_redirects=False)
        self.assertTrue(unquote(reply.headers['location']).startswith('/autopilot?toast="Paper 2" was removed'))
        client.post('/work-queue/continue', follow_redirects=False)                 # continues, and starts the next at once
        self.assertEqual([title for title, _job, _files in self.launched], ['Paper 1', 'Paper 3'])
        self.assertIn('Finished from the queue (1)', client.get('/work-queue/card').text)
        queue.submit(kind, 'Paper 4', {})
        reply = client.post('/work-queue/clear', headers={'referer': 'https://elsewhere.example//x'}, follow_redirects=False)
        self.assertTrue(reply.headers['location'].startswith('/'))
        self.assertIn('1 waiting item removed', unquote(reply.headers['location']))

    def test_an_empty_queue_says_how_to_fill_it(self):
        page = self.client().get('/work-queue').text
        self.assertIn('Nothing is waiting', page)
        self.assertIn('Each file is a separate paper', page)
        self.assertNotIn('hx-get="/work-queue/card"', page)
        self.assertEqual(json.loads(json.dumps(queue.overview()['line'])), [])


class TheRealApp(Base):
    """The screens as Studio itself serves them, side menu and all."""
    def client(self):
        import app
        import pipeline_bridge as pb
        import pw_auth
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
        app._prepare()                                  # every screen's tables, in this test's database
        return TestClient(app.app), app

    def test_the_queue_has_its_own_page_and_the_card_is_on_both_forms(self):
        client, app = self.client()
        kind = self.kind()
        queue.submit(kind, 'Paper 1', {})
        queue.submit(kind, 'Paper 2', {})
        for path in ('/work-queue', '/autopilot', '/new/any-format'):
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn('href="/work-queue"', page.text, path)                # the side menu
            self.assertIn('Queue · 2', page.text, path)
            self.assertIn('Paper 2', page.text, path)
        self.assertIn('Each file is a separate paper', client.get('/autopilot').text)
        menu = client.get('/work-queue').text
        self.assertIn('Smart Queue', menu)
        self.assertIn('Add many papers &amp; tasks at once; they run one by one', menu)
        self.assertIn('1 waiting', menu)                     # the highlighted tab's badge: Paper 1 runs, Paper 2 waits
        self.assertIn('What is new', menu)
        for path, button in (('/translate/bulk', 'Smart Queue:'), ('/solutions', 'Smart Queue:')):
            self.assertIn(button, client.get(path).text, path)
        # "Send to bots" keeps its address and its screen.
        paths = {route.path for route in app.app.routes}
        self.assertTrue({'/queue', '/queue/translation', '/work-queue', '/work-queue/card'} <= paths)
        self.assertIn('Send to bots', (ROOT / 'qbg_studio/templates/queue.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
