"""Paper session profile isolation and stopped-preview integration tests."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import paper_engine as e

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.saved = (copy.deepcopy(e.STATE), copy.deepcopy(e.TRADES), dict(e.RUNTIME), dict(e.JOBS),
                      e._PROFILE_NAME, e._PROFILE_UI, e._PROFILE_ERROR, e._PROFILE_DIR, e._PROFILE_ACTIVE_FILE)
        root = Path(self.tmp.name)
        e._PROFILE_DIR = root/'profiles'; e._PROFILE_ACTIVE_FILE = root/'active.json'
        e._PROFILE_NAME = None; e._PROFILE_UI = {}; e._PROFILE_ERROR = ''
        e.STATE.clear(); e.TRADES.clear(); e.JOBS.clear()
        e.RUNTIME.update(running=False, scanning=False, config=dict(e.DEFAULTS, symbols='BANKBEES.NS', market='IN'))
        self.client = app.app.test_client()
    def tearDown(self):
        st, trades, rt, jobs, name, ui, error, directory, active = self.saved
        e.STATE.clear(); e.STATE.update(st); e.TRADES[:] = trades
        e.RUNTIME.clear(); e.RUNTIME.update(rt); e.JOBS.clear(); e.JOBS.update(jobs)
        e._PROFILE_NAME, e._PROFILE_UI, e._PROFILE_ERROR = name, ui, error
        e._PROFILE_DIR, e._PROFILE_ACTIVE_FILE = directory, active
    def action(self, **body):
        return self.client.post('/paper/api/paper-live/profiles', json=body).get_json()
    def test_save_new_load_and_restart_isolation(self):
        e.STATE['BANKBEES.NS'] = {'symbol':'BANKBEES.NS','cash':15,'pos':{'qty':2}}
        e.TRADES.append({'symbol':'BANKBEES.NS','pnl':12})
        ui={'fields':{'market':'IN'},'rows':[{'sym':'BANKBEES.NS'}]}
        d=self.action(action='create',name='nse_20_11_2026',ui=ui)
        self.assertTrue(d['ok'],d); self.assertEqual(d['ui'],ui)
        self.assertFalse(e.RUNTIME['running'])
        e.STATE['BANKBEES.NS']['cash']=42; e._save()
        usui={'fields':{'market':'US'},'rows':[{'sym':'AAPL'}]}
        d=self.action(action='new',name='us_20112026',active='nse_20_11_2026',current_ui=ui,ui=usui,config={'market':'US','symbols':'AAPL'})
        self.assertTrue(d['ok'],d); self.assertEqual(e.STATE,{})
        e.STATE['AAPL']={'symbol':'AAPL','pos':None,'cash':300}; e._save()
        d=self.action(action='load',name='nse_20_11_2026',active='us_20112026',current_ui=usui)
        self.assertTrue(d['ok'],d); self.assertEqual(e.STATE['BANKBEES.NS']['cash'],42)
        self.assertNotIn('AAPL',e.STATE); self.assertEqual(len(e.TRADES),1)
        self.assertFalse(e.RUNTIME['running']); self.assertEqual(d['ui'],ui)
        e.STATE.clear(); e.TRADES.clear(); e._PROFILE_NAME=None
        self.assertTrue(e._profile_restore_startup()); self.assertEqual(e._PROFILE_NAME,'nse_20_11_2026')
        self.assertEqual(e.STATE['BANKBEES.NS']['cash'],42); self.assertFalse(e.RUNTIME['running'])
    def test_reload_same_profile_keeps_latest_memory_and_ui(self):
        self.assertTrue(self.action(action='create',name='nse',ui={'fields':{'market':'IN'}})['ok'])
        e.STATE['A.NS']={'symbol':'A.NS', 'pos':None, 'cash':17}
        ui={'fields':{'market':'US'},'rows':[]}
        d=self.action(action='load',name='nse',active='nse',current_ui=ui)
        self.assertTrue(d['ok'], d)
        self.assertEqual(e.STATE['A.NS']['cash'],17)
        self.assertEqual(d['ui'],ui)

    def test_profile_does_not_read_or_overwrite_shared_allocations(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        d=self.client.get('/paper/api/paper-live/allocations').get_json()
        self.assertFalse(d['exists'])
        with patch.object(e, 'ALLOC_FILE', Path(self.tmp.name)/'shared.json'):
            bad=self.client.post('/paper/api/paper-live/allocations',json={'session_profile':'other','data':{'rows':[]}}).get_json()
            self.assertFalse(bad['ok'])
            good=self.client.post('/paper/api/paper-live/allocations',json={'session_profile':'nse','data':{'rows':[]}}).get_json()
            self.assertTrue(good['ok'])
            self.assertFalse(e.ALLOC_FILE.exists())

    def test_stopped_scan_is_preview_and_stopped_ltp_is_blocked(self):
        self.assertTrue(self.action(action='create', name='nse',ui={})['ok'])
        with patch.object(e,'scan_once',return_value={'ok':True,'symbols':1,'ms':1}) as scan:
            d=self.client.post('/paper/api/paper-live/scan',json={'session_profile':'nse'}).get_json()
        self.assertTrue(d['preview']); self.assertEqual(scan.call_args.kwargs['kind'],'preview')
        self.assertFalse(self.client.post('/paper/api/paper-live/ltp',json={'session_profile':'nse'}).get_json()['ok'])
        self.assertFalse(self.client.post('/paper/api/paper-live/scan',json={'session_profile':'other'}).get_json()['ok'])
    def test_new_preview_runs_after_previous_job_was_stopped(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        e.JOBS['old']={'stop':True}
        with patch.object(e, '_scan_once', return_value={'ok':True}) as scan:
            result=e.scan_once(e.DEFAULTS,kind='preview')
        self.assertTrue(result['ok'])
        scan.assert_called_once()

    def test_switch_blocked_while_running_or_draining(self):
        self.assertTrue(self.action(action='create', name='nse',ui={})['ok'])
        e.RUNTIME['running']=True
        self.assertFalse(self.action(action='new',name='us',active='nse')['ok'])
        e.RUNTIME['running']=False
        e._WORK_GATE.acquire()
        try: self.assertFalse(self.action(action='new',name='us',active='nse')['ok'])
        finally: e._WORK_GATE.release()
        self.assertEqual(e._PROFILE_NAME,'nse')
    def test_trailing_position_change_is_saved_even_without_trade(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        e.STATE['A.NS']={'symbol':'A.NS','pos':{'best':100},'cash':10}
        cfg=dict(e.DEFAULTS,symbols='A.NS')
        def tick(st, *args, **kwargs):
            st['pos']['best']=105
            return False
        with patch.object(e, '_ltp_from_1m', return_value=({'A.NS':105},{'A.NS':0})), \
             patch.object(e, '_ltp_tick', side_effect=tick), patch.object(e, '_save') as save:
            self.assertEqual(e._ltp_only_scan(cfg),1)
            self.assertGreaterEqual(save.call_count,1)
        self.assertEqual(e.STATE['A.NS']['pos']['best'],105)

    def test_stale_ltp_request_cannot_process_after_profile_switch(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        e.RUNTIME['running'] = True
        e._TL.session_profile = 'different_profile'
        try:
            with patch.object(e, '_ltp_only_scan') as scan:
                self.assertEqual(e.ltp_only_scan(e.DEFAULTS), -1)
                scan.assert_not_called()
        finally:
            e._TL.session_profile = None

    def test_save_failure_stops_trading_and_reports_health(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        e.RUNTIME['running']=True
        e.JOBS['job']={'stop':False}
        with patch.object(e._profiles, 'save', side_effect=OSError('disk full')):
            e._save()
        self.assertFalse(e.RUNTIME['running'])
        self.assertTrue(e.JOBS['job']['stop'])
        self.assertIn('disk full', e._PROFILE_ERROR)
        self.assertIn('profile_save', [i['code'] for i in e.global_health(e.DEFAULTS, [])])
        self.assertFalse(self.client.post('/paper/api/paper-live/start',json={'session_profile':'nse'}).get_json()['ok'])

    def test_corrupt_current_profile_fails_closed_on_restart(self):
        self.assertTrue(self.action(action='create',name='nse',ui={})['ok'])
        (e._PROFILE_DIR/'nse.json').write_text('{')
        e.STATE['other']={'pos':None}; e._PROFILE_NAME=None
        self.assertTrue(e._profile_restore_startup()); self.assertTrue(e._PROFILE_ERROR)
        self.assertEqual(e.STATE,{})
        self.assertFalse(self.client.post('/paper/api/paper-live/start',json={}).get_json()['ok'])

if __name__=='__main__': unittest.main()