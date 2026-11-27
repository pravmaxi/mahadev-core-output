"""Presentation of a recalculated backtest, never a persistent trading ledger.

The engine's eod close is hypothetical. Keep its valuation (including its
assumed closing costs) but do not report it as a completed trade.
"""
import copy


def continuation_view(result, strategy_mode):
    result = copy.deepcopy(result)
    trades = result.get('trades', [])
    closed = [t for t in trades if t.get('reason') != 'eod']
    opened = [t for t in trades if t.get('reason') == 'eod']
    closed_pnl = sum(float(t.get('pnl') or 0) for t in closed)
    open_leg_realized = 0.0
    estimated_open = 0.0
    for trade in opened:
        details = trade.get('entries_detail') or []
        if strategy_mode == 'alternate' and details:
            # Earlier flips are real simulated closes; only the final eod leg
            # is hypothetical. Its pnl includes simulated exit costs.
            open_leg_realized += sum(float(leg.get('leg_pnl') or 0) for leg in details[:-1])
        estimated_open += float(trade.get('pnl') or 0)
        if strategy_mode == 'alternate' and details:
            last = details[-1]
            trade['side'] = 'long' if last.get('sign') == 1 else 'short'
            trade['entry_t'] = last.get('t', trade['entry_t'])
            trade['entry_px'] = last.get('px', trade['entry_px'])
            trade['total_qty'] = last.get('qty', trade.get('total_qty'))
            last['t_out'] = ''
            last['px_out'] = ''
            last['leg_pnl'] = None
            last['reason'] = 'open (estimated)'
        trade['exit_t'] = ''
        trade['exit_px'] = ''
        trade['reason'] = 'open (estimate)'
        # For an alternate cycle the engine pnl includes realised earlier legs.
        trade['pnl'] = round(float(trade.get('pnl') or 0) - (sum(float(x.get('leg_pnl') or 0) for x in details[:-1]) if strategy_mode == 'alternate' else 0), 2)
        # Cycle return is not the current leg return; suppress it.
        if strategy_mode == 'alternate':
            trade['pnl_pct'] = None
    result['continuation'] = {
        'completed_trades': len(closed),
        'open_positions': len(opened),
        'realized_pnl': round(closed_pnl + open_leg_realized, 2),
        'estimated_open_pnl': round(estimated_open - open_leg_realized, 2),
        'valuation_time': result.get('to'),
        'valuation_price': result.get('candles', [{}])[-1].get('c') if result.get('candles') else None,
    }
    return result