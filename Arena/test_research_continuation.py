import copy
import unittest
from unittest.mock import patch
import app
import research_backtest
import research_routes
from research_continuation import continuation_view
from datetime import datetime, timedelta, timezone
from math import sin


def candles():
    start = datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    rows = []
    for i in range(240):
        price = 100 + sin(i / 6) * 8
        rows.append({'t': (start + timedelta(hours=i)).isoformat(),
                     'o': price, 'h': price + 1, 'l': price - 1,
                     'c': price + .2, 'v': 1000})
    return rows

class ContinuationTests(unittest.TestCase):
    def test_alternate_cycle_separates_open_leg_from_prior_flips(self):
        original={'to':'today','candles':[{'c':101}], 'trades':[
            {'reason':'target','pnl':20},
            {'reason':'eod','pnl':7,'pnl_pct':1,'side':'long','entry_t':'a','entry_px':100,'exit_t':'b','exit_px':101,'total_qty':1,
             'entries_detail':[{'t':'a','px':100,'sign':1,'qty':1,'leg_pnl':-3}, {'t':'b','px':99,'sign':-1,'qty':2,'leg_pnl':10,'t_out':'c','px_out':94}]}]}
        before=copy.deepcopy(original)
        view=continuation_view(original,'alternate'); c=view['continuation']; t=view['trades'][-1]
        self.assertEqual(original,before)
        self.assertEqual((c['realized_pnl'],c['estimated_open_pnl']),(17,10))
        self.assertEqual((t['side'],t['entry_t'],t['total_qty']),('short','b',2))
        self.assertEqual((t['exit_t'],t['exit_px'],t['pnl'],t['pnl_pct']),('','',10,None))
        self.assertEqual(t['entries_detail'][-1]['t_out'],'')
    def test_simple_and_no_open(self):
        base={'candles':[{'c':50}],'to':'t','trades':[{'reason':'stop','pnl':-10},{'reason':'eod','pnl':5,'pnl_pct':2,'exit_t':'x','exit_px':50}]}
        c=continuation_view(base,'simple')['continuation']
        self.assertEqual((c['realized_pnl'],c['estimated_open_pnl'],c['open_positions']),(-10,5,1))
        base['trades'].pop(); c=continuation_view(base,'simple')['continuation']
        self.assertEqual((c['realized_pnl'],c['estimated_open_pnl'],c['open_positions']),(-10,0,0))
    def test_same_side_open(self):
        b={'trades':[{'reason':'eod','pnl':12,'exit_t':'x','exit_px':50,'entries_detail':[{'qty':1},{'qty':2}]}]}
        v=continuation_view(b,'same_side'); self.assertEqual(v['continuation']['estimated_open_pnl'],12)
        self.assertEqual(v['trades'][0]['exit_t'],'')
    def test_endpoint_does_not_change_backtest_cache_or_engine_result(self):
        client=app.app.test_client(); rows=candles()
        self.assertEqual(client.get('/research/continuation').status_code,200)
        before=copy.deepcopy(research_routes.LAST_BACKTEST_CACHE)
        with patch.object(research_backtest,'download',return_value=rows):
            for mode,side in [('simple','both'),('same_side','long'),('alternate','both')]:
                cfg={'symbol':'TEST.NS','interval':'1h','period':'6mo','indicators':{'wt':True},'fno_mode':'none','strategy_mode':mode,'side_mode':side}
                out=client.post('/research/api/continuation/run',json=cfg).get_json()
                self.assertTrue(out['ok'],out)
                self.assertIn('continuation',out)
                self.assertTrue(all(t['exit_t']=='' and t['exit_px']=='' for t in out['trades'] if t['reason']=='open (estimate)'))
        self.assertEqual(before,research_routes.LAST_BACKTEST_CACHE)
if __name__ == '__main__': unittest.main()