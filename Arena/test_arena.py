"""Smoke tests for the single-port Arena integration (no network or broker)."""
import unittest
import app as arena

class ArenaIntegration(unittest.TestCase):
    def setUp(self):
        self.client = arena.app.test_client()
        self.paper_cfg = arena.paper_engine.RUNTIME['config']
        self.live_cfg = arena.live_engine.RUNTIME['config']

    def tearDown(self):
        arena.paper_engine.RUNTIME['config'] = self.paper_cfg
        arena.live_engine.RUNTIME['config'] = self.live_cfg

    def test_pages_and_independent_browser_storage(self):
        for profile, path in [('paper', '/paper/paper-live'), ('live', '/live/live-trade')]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            html = r.get_data(as_text=True)
            self.assertIn('Arena · Trading', html)
            self.assertIn("var PROFILE = '%s'" % profile, html)
            self.assertIn('arena_%s_cfg' % profile, html)
            self.assertIn('/%s/api/paper-live/' % profile, html)
            self.assertIn('/paper/paper-live', html)
            self.assertIn('/live/live-trade', html)

    def test_paper_pending_rule_default_and_overrides(self):
        engine = arena.paper_engine
        self.assertEqual(engine.cfg_of({})['wt_pending_rule'], 'same_dir')
        for rule in ('every', 'same_dir', 'any_dir'):
            self.assertEqual(engine.cfg_of({'wt_pending_rule': rule})['wt_pending_rule'], rule)
        html = self.client.get('/paper/paper-live').get_data(as_text=True)
        self.assertIn('<option value="same_dir" selected>same_dir</option>', html)

    def test_settings_and_state_are_independent(self):
        self.assertIsNot(arena.paper_engine.STATE, arena.live_engine.STATE)
        self.assertIsNot(arena.paper_engine.RUNTIME, arena.live_engine.RUNTIME)
        self.assertNotEqual(arena.paper_engine.STATE_FILE, arena.live_engine.STATE_FILE)
        self.assertIs(arena.paper_engine.DATA_LOCK, arena.live_engine.DATA_LOCK)
        for name, tf in [('paper', '5m'), ('live', '1h')]:
            r = self.client.post('/%s/api/paper-live/update' % name,
                                 json={'interval': tf, 'symbols': 'BPCL.NS'})
            self.assertTrue(r.get_json()['ok'])
        for name, tf in [('paper', '5m'), ('live', '1h')]:
            d = self.client.get('/%s/api/paper-live/status' % name).get_json()
            self.assertEqual(d['profile'], name)
            self.assertEqual(d['config']['interval'], tf)
            self.assertTrue(d['health']['coverage']['reviewed'])

if __name__ == '__main__':
    unittest.main()