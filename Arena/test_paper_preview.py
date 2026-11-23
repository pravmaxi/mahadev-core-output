"""Regression tests for the internal preview primitive (no Yahoo network)."""
import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import pandas as pd
import paper_engine as e


class PreviewTest(unittest.TestCase):
    def test_preview_does_not_execute_or_change_position_and_event_cursor(self):
        old_state, old_trades, old_rt, old_jobs = copy.deepcopy(e.STATE), list(e.TRADES), dict(e.RUNTIME), dict(e.JOBS)
        self.addCleanup(lambda: (e.STATE.clear(), e.STATE.update(old_state), e.TRADES.clear(), e.TRADES.extend(old_trades), e.RUNTIME.clear(), e.RUNTIME.update(old_rt), e.JOBS.clear(), e.JOBS.update(old_jobs)))
        pos = {'sign': 1, 'qty': 1, 'entry': 100, 'best': 100, 'stop': 90, 'target': 110}
        e.STATE.clear(); e.STATE['TEST.NS'] = {'symbol':'TEST.NS', 'cash': 0, 'pos': copy.deepcopy(pos), 'events_checked_through':'2026-11-01 10:15'}
        e.TRADES.clear(); e.JOBS.clear(); e.RUNTIME.update(running=False, scanning=False)
        index = pd.date_range('2026-11-01', periods=50, freq='h', tz='Asia/Kolkata')
        df = pd.DataFrame({'Open':100., 'High':101., 'Low':99., 'Close':100.}, index=index)
        cfg = dict(e.DEFAULTS, symbols='TEST.NS', exchange='.NS', heikin_ashi=False, fno_mode='none')
        pend = {'dir':'buy','level':101.,'valid_i':48,'valid_t':'2026-11-03 00:00','age':0}
        def fake_view(st, cs, ltp):
            st['pos']['stop'] = 1
            st['position'] = 'LONG 1'
        with ExitStack() as stack:
            for name, val in {'_data':(lambda: (None, lambda s:{})), '_download_candles':lambda *a,**k:{'TEST.NS':[{}]*50}, '_ltp_from_1m':lambda *a,**k:({'TEST.NS':100.},{'TEST.NS':0}), '_common':lambda:(lambda bars:df, None), '_prep':lambda *a:(df.iloc[:-1], [], {}, None), '_wt_compute':lambda *a: {}, '_pending_book':lambda *a,**k:{'buy':None,'sell':None}, '_pending_signal':lambda *a,**k:pend, '_pos_view':fake_view, '_save':lambda:None}.items():
                stack.enter_context(patch.object(e, name, side_effect=val))
            step = stack.enter_context(patch.object(e, 'live_step'))
            already = stack.enter_context(patch.object(e, '_already_triggered'))
            result = e.scan_once(cfg, kind='preview')
            self.assertTrue(result['ok'])
            self.assertEqual(e.STATE['TEST.NS']['status'], 'OK')
            step.assert_not_called(); already.assert_not_called()
            self.assertEqual(e.STATE['TEST.NS']['pos'], pos)
            self.assertEqual(e.STATE['TEST.NS']['events_checked_through'], '2026-11-01 10:15')
            self.assertEqual(e.STATE['TEST.NS']['latest_note'], 'Preview only — no trades processed')
            self.assertEqual(e.TRADES, [])


if __name__ == '__main__': unittest.main()