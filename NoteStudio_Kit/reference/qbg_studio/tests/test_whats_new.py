"""The What's new tour after an update (3 October 2026): shown on one or two
starts, the newest change first (QBG One since 4 October, Smart Queue before
it), "Got it" ends it, and the side menu opens it again at any time."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'qbg_studio'), str(ROOT), str(ROOT / 'tests'), str(ROOT / '2_Question_Creation_Pipeline')]

import db  # noqa: E402
import whats_new  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(db, 'DB_PATH', Path(temp.name) / 'test.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def restart(self, boot):
        patcher = patch.object(whats_new, 'BOOT', boot)
        patcher.start()
        self.addCleanup(patcher.stop)


class TwoStartsAtMost(Base):
    def test_the_newest_change_comes_first(self):
        tour = whats_new.current()
        self.assertEqual(tour['id'], '2026-10-04-qbg-one')
        self.assertEqual((tour['steps'][0]['title'], tour['steps'][0]['href']), ('QBG Studio is now QBG One', '/dashboard'))
        self.assertIn('/search', [step['href'] for step in tour['steps']])
        self.assertTrue(all(step['text'] and step['href'].startswith('/') for step in tour['steps']))
        earlier = whats_new.TOURS[1]                                     # the Smart Queue tour stays, behind it
        self.assertEqual((earlier['steps'][0]['title'], earlier['steps'][0]['href']), ('Smart Queue', '/work-queue'))

    def test_shown_on_two_starts_then_never_unless_asked(self):
        self.restart('first')
        first = whats_new.state()
        self.assertTrue(first['show'])
        self.assertFalse(first['last'])
        self.assertTrue(whats_new.state()['show'])          # every page of the same start: still the same showing
        whats_new.finish(first['id'], done=False)           # closed, not finished
        self.restart('second')
        second = whats_new.state()
        self.assertEqual((second['show'], second['last']), (True, True))
        self.restart('third')
        self.assertFalse(whats_new.state()['show'])
        # The side menu's "What's new" still has the steps.
        self.assertTrue(whats_new.state(mark=False)['steps'])

    def test_got_it_ends_it_on_the_first_start(self):
        self.restart('first')
        whats_new.finish(whats_new.state()['id'], done=True)
        self.assertFalse(whats_new.state()['show'])
        self.restart('second')
        self.assertFalse(whats_new.state()['show'])

    def test_the_next_releases_tour_is_shown_to_someone_who_finished_this_one(self):
        self.restart('first')
        whats_new.finish(whats_new.state()['id'], done=True)
        newer = dict(id='next-release', title="What's new", steps=[dict(title='Next', text='Something new.', href='/')])
        with patch.object(whats_new, 'TOURS', [newer] + whats_new.TOURS):
            self.assertEqual((whats_new.state()['show'], whats_new.state()['id']), (True, 'next-release'))


class TheRoutes(Base):
    def client(self):
        import app
        import pipeline_bridge as pb
        import pw_auth
        from fastapi.testclient import TestClient
        for target, name, value in ((pb, 'health', dict(checks=[], bad=0, warn=0, ready=True)),
                                    (pb, 'portal_session', dict(ok=False, source='none', hours=None, headline='',
                                                                detail='', needs_fix=False)),
                                    (pw_auth, 'status', dict(configured=False, signed_in=False, usable=False, detail='',
                                                             email='', store_path='', redirect_uri='', live=False, html='',
                                                             encrypted_at_rest=False))):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pb._PW_CACHE['value'] = None
        app._prepare()
        return TestClient(app.app)

    def test_the_page_asks_on_load_and_the_answer_is_kept(self):
        self.restart('first')
        client = self.client()
        page = client.get('/work-queue').text
        self.assertIn('whats_new.js', page)
        self.assertIn('data-whats-new>What&#39;s new</a>', page.replace("What's new", 'What&#39;s new'))
        value = client.get('/whats-new/state').json()
        self.assertEqual((value['show'], value['steps'][0]['title']), (True, 'QBG Studio is now QBG One'))
        client.post('/whats-new/seen', data={'tour': value['id'], 'done': '1'})
        self.assertFalse(client.get('/whats-new/state').json()['show'])
        again = client.get('/whats-new/state?again=1').json()           # the side menu's link
        self.assertTrue(again['show'])
        client.post('/whats-new/seen', data={'tour': 'not-a-tour', 'done': '1'})
        self.assertNotIn('not-a-tour', db.get_setting(whats_new.STATE, ''))
        script = (ROOT / 'qbg_studio/static/whats_new.js').read_text(encoding='utf-8')
        self.assertIn('/whats-new/state', script)
        self.assertIn('Show me', script)


class ThePageGuides(TheRoutes):
    """4 October 2026: "add instructions or a short guide on top so that they can easily
    understand the app and the task"."""

    def test_every_main_screen_starts_with_its_guide(self):
        client = self.client()
        for path, title in (('/autopilot', 'Autopilot: a whole paper to QBG in one go'),
                            ('/new/any-format', 'New batch: read a paper, check it, upload it'),
                            ('/work-queue', 'Smart Queue: one task after another, by itself'),
                            ('/translate/bulk', 'Bulk translation: another language on the same QBG IDs'),
                            ('/tagging', 'Tag questions: subject, chapter, topic and difficulty'),
                            ('/video-tagging', 'Video tagging: a solution video on each question'),
                            ('/verify', 'Verify questions: the Verified mark on QBG'),
                            ('/solutions', 'Solutions: worked solutions on QBG questions')):
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn('data-page-guide="%s"' % path, page.text, path)
            self.assertIn(title, page.text, path)
            self.assertIn('How this works', page.text, path)
            # On top: before the screen's own heading.
            self.assertLess(page.text.index('data-page-guide'), page.text.index('<h1'), path)

    def test_every_guide_is_short_and_a_run_page_has_none(self):
        import guides
        for path, guide in guides.GUIDES.items():
            self.assertTrue(guide['title'] and guide['about'], path)
            self.assertTrue(1 <= len(guide['steps']) <= 4, path)
        self.assertIsNone(guides.for_path('/translate/bulk/runs/12'))
        self.assertEqual(guides.for_path('/tagging/')['key'], '/tagging/')
        script = (ROOT / 'qbg_studio/static/app.js').read_text(encoding='utf-8')
        self.assertIn('qbg-guide-seen:', script)            # open on the first visit, folded after


if __name__ == '__main__':
    unittest.main()
