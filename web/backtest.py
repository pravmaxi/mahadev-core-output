"""
Backtest engine — valid-candle → trigger-breakout backtest.
Three strategy modes: simple, same_side, alternate.
Integer quantity. Trailing stop loss. Signal confirmation filters:
  - WT extreme (buy max / sell min)
  - HTF trend (higher timeframe EMA50 > EMA200 for buy, reverse for sell)
  - RSI rising/falling
  - Volume spike vs rolling average
"""

import time
import statistics
import math
import pandas as pd
from data import download
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
from indicators import get as get_indicator
from settings import IST


# =========================================================================
# HELPERS
# =========================================================================

def _to_ist(df):
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df


def _multiplier(kind, k):
    if kind == "martingale":  return 2 ** k
    if kind == "incremental": return k + 1
    return 1


def _qty_for(C, fill, cash):
    """Integer quantity only."""
    if fill <= 0 or cash <= 0: return 0
    if C["sizing_mode"] == "fixed":
        q = max(1, math.floor(C["position_amount"] / fill))
        return q if q * fill <= cash else 0
    elif C["sizing_mode"] == "qty":
        q = max(1, int(C["fixed_qty"]))
        return q if q * fill <= cash else 0
    else:
        q = int((cash * C["position_pct"] / 100) / fill)
        return q if q >= 1 else 0


def _target_px(C, entry_price, qty_abs, side_sign):
    if qty_abs <= 0: return None
    mode = C.get("profit_target_mode", "pct")
    val  = float(C.get("profit_target_value", 0) or 0)
    if val <= 0: return None
    if mode == "amount":
        return entry_price + side_sign * (val / qty_abs)
    return entry_price * (1 + side_sign * val / 100)


def _update_pending(C, pending, net):
    """After a trade closes, update the pending recovery amount.
       cumulative : sum losses, subtract wins (floor at 0)
       last       : a loss REPLACES pending, any win clears it
    """
    if not C.get("recovery_enabled"):
        return 0.0
    if net > 0:
        if C.get("recovery_cumulative"):
            return max(0.0, pending - net)
        return 0.0
    if C.get("recovery_cumulative"):
        return pending + abs(net)
    return abs(net)


def _recovery_target_px(C, entry_price, qty_abs, side_sign, pending):
    """When recovery is enabled and there is a pending amount, override the
    target to (pending + buffer) in ₹. Else fall back to the normal target."""
    if not C.get("recovery_enabled") or pending <= 0 or qty_abs <= 0:
        return _target_px(C, entry_price, qty_abs, side_sign)
    amt = pending + float(C.get("recovery_buffer_amount", 0.0) or 0.0)
    return entry_price + side_sign * (amt / qty_abs)


def _trailing_sl_price(C, entry, best_price, side_sign):
    if not C.get("trailing_enabled"): return None
    if entry <= 0: return None
    if side_sign > 0:
        max_pct = (best_price - entry) / entry * 100
    else:
        max_pct = (entry - best_price) / entry * 100
    if max_pct < C["trailing_start_pct"]: return None
    step = C["trailing_step_pct"]
    if C.get("trailing_tighten_enabled") and max_pct >= C["trailing_tighten_at"]:
        step = C["trailing_tighten_step"]
    sl_pct = max_pct - step
    if sl_pct <= 0: return None
    return entry * (1 + side_sign * sl_pct / 100)


# =========================================================================
# INDICATOR HELPERS — for confirmation filters
# =========================================================================

def _rsi_series(series, period=14):
    """Wilder's RSI, index-aligned."""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_g / avg_l.replace(0, float('nan'))
    return 100 - 100 / (1 + rs)


_HTF_MAP = {
    "5m":"1h", "15m":"1h", "30m":"1h",
    "1h":"1d", "1d":"1wk", "1wk":"1mo",
    "4h":"1d", "60m":"1d", "90m":"1d",
}
_HTF_PERIOD = {
    "1h":"60d", "1d":"2y", "1wk":"10y", "1mo":"10y",
}


def _load_htf_trend(symbol, base_interval):
    """Return a DataFrame with ema50, ema200 on the higher timeframe,
    or None if we can't get data."""
    htf = _HTF_MAP.get(base_interval)
    if not htf: return None
    period = _HTF_PERIOD.get(htf, "2y")
    try:
        rows = download(symbol, htf, period)
    except Exception:
        return None
    if not rows or len(rows) < 60: return None
    df = candles_to_df(rows)
    df = _to_ist(df)
    df["ema50"]  = df["Close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["Close"].ewm(span=200, adjust=False).mean()
    return df


def _htf_uptrend_at(hdf, ts):
    """Return True/False/None (unknown) at the given timestamp."""
    if hdf is None or hdf.empty: return None
    try:
        idx = hdf.index.searchsorted(ts, side="right") - 1
    except Exception:
        return None
    if idx < 0 or idx >= len(hdf): return None
    r = hdf.iloc[idx]
    if pd.isna(r["ema50"]) or pd.isna(r["ema200"]): return None
    return bool(r["ema50"] > r["ema200"])


# =========================================================================
# EVENT DETECTION
# =========================================================================

def detect_wt_events(df, params, buffer_pts, max_wait=20, pending_rule="every"):
    """WaveTrend event detector.
    
    pending_rule:
      "every"    → Every cross checked against its own wait window.
      "same_dir" → Only newest cross of each direction is alive.
      "any_dir"  → Only newest cross total is alive (buy kills sell, sell kills buy).
    """
    mod = get_indicator("wt")
    comp = mod.compute(df, params)
    cross = comp["cross"]
    wt1 = comp["wt1"]
    wt2 = comp["wt2"]
    events = []
    n = len(df)

    def _mk_cross_meta(i, direction):
        vc = df.iloc[i]
        level = float(vc["High"] if direction == "buy" else vc["Low"])
        perfect = ("Perfect" if ((direction == "buy" and vc["Open"] == vc["Low"]) or
                                 (direction == "sell" and vc["Open"] == vc["High"]))
                   else "Imperfect")
        _v = wt1.iloc[i] if direction == "buy" else wt2.iloc[i]
        wt_val = None if (_v != _v) else round(float(_v), 2)
        return level, perfect, wt_val

    if pending_rule in ("same_dir", "any_dir"):
        pend = {"buy": None, "sell": None}
        for i in range(1, n):
            h = float(df.iloc[i]["High"])
            l = float(df.iloc[i]["Low"])
            # Trigger check
            for direction in ("buy", "sell"):
                p = pend[direction]
                if p is None: continue
                hit = ((direction == "buy"  and h > p["level"] + buffer_pts) or
                       (direction == "sell" and l < p["level"] - buffer_pts))
                if hit:
                    events.append({
                        "valid_i": p["valid_i"], "trigger_i": i,
                        "direction": direction,
                        "trigger_level": round(p["level"], 4),
                        "buffer": buffer_pts,
                        "perfect": p["perfect"], "source": "wt",
                        "cross": p["cross"], "wt_val": p["wt_val"],
                    })
                    pend[direction] = None
                elif i - p["valid_i"] >= max_wait:
                    pend[direction] = None
            
            # Fresh cross
            c = cross.iloc[i]
            if c in ("Bull Cross", "Bear Cross"):
                direction = "buy" if c == "Bull Cross" else "sell"
                level, perfect, wt_val = _mk_cross_meta(i, direction)
                pend[direction] = {
                    "valid_i": i, "level": level,
                    "perfect": perfect, "cross": c, "wt_val": wt_val,
                }
                if pending_rule == "any_dir":
                    # Wipe out the opposite direction
                    opposite = "sell" if direction == "buy" else "buy"
                    pend[opposite] = None
        return events

    # --- Default: every cross has its own wait window ---
    for i in range(1, n):
        c = cross.iloc[i]
        if c not in ("Bull Cross", "Bear Cross"): continue
        direction = "buy" if c == "Bull Cross" else "sell"
        level, perfect, wt_val = _mk_cross_meta(i, direction)
        for k in range(i + 1, min(n, i + 1 + max_wait)):
            hit = ((direction == "buy"  and float(df.iloc[k]["High"]) > level + buffer_pts) or
                   (direction == "sell" and float(df.iloc[k]["Low"])  < level - buffer_pts))
            if hit:
                events.append({
                    "valid_i": i, "trigger_i": k,
                    "direction": direction,
                    "trigger_level": round(level, 4),
                    "buffer": buffer_pts,
                    "perfect": perfect, "source": "wt", "cross": c,
                    "wt_val": wt_val,
                })
                break
    return events

def detect_ce_events(df, params, buffer_pts, max_wait=20):
    mod = get_indicator("ce")
    comp = mod.compute(df, params)
    dir_series = comp["direction"]
    events = []
    n = len(df)
    for i in range(1, n):
        if dir_series.iloc[i] == dir_series.iloc[i-1]: continue
        direction = "buy" if int(dir_series.iloc[i]) == 1 else "sell"
        vc = df.iloc[i]
        trigger_level = float(vc["High"] if direction == "buy" else vc["Low"])
        perfect = ("Perfect" if ((direction == "buy" and vc["Open"] == vc["Low"]) or
                                 (direction == "sell" and vc["Open"] == vc["High"]))
                   else "Imperfect")
        for k in range(i + 1, min(n, i + 1 + max_wait)):
            hit = ((direction == "buy"  and float(df.iloc[k]["High"]) > trigger_level + buffer_pts) or
                   (direction == "sell" and float(df.iloc[k]["Low"])  < trigger_level - buffer_pts))
            if hit:
                events.append({
                    "valid_i": i, "trigger_i": k,
                    "direction": direction,
                    "trigger_level": round(trigger_level, 4),
                    "buffer": buffer_pts,
                    "perfect": perfect, "source": "ce", "cross": "CE flip",
                    "wt_val": None,
                })
                break
    return events


def _detect_st_stream(df, res, sup, buffer_pts, mode, role):
    """Single-pass ST detection. Emits one list of events.

    mode = "strict": valid candle → pending → trigger on later bar
    mode = "loose" : any cross → signal on the same bar
    role = "entry" or "exit" — tagged onto every emitted event.

    Rules (spec):
      - Valid buy  = H > R AND C > R. Trigger level = bar's High.
      - Valid sell = L < S AND C < S. Trigger level = bar's Low.
      - Buy and sell pendings are independent.
      - Pending buy  nullified when R value changes.
      - Pending sell nullified when S value changes.
      - Trigger fires on later bar that breaks the trigger level ± buffer.
    """
    n = len(df)
    events = []

    pend_buy  = None
    pend_sell = None

    def _emit(direction, valid_i, level, trigger_i, rs, buf=None):
        vc = df.iloc[valid_i]
        perfect = ("Perfect" if ((direction == "buy"  and vc["Open"] == vc["Low"]) or
                                 (direction == "sell" and vc["Open"] == vc["High"]))
                   else "Imperfect")
        events.append({
            "rs": round(rs, 4) if rs is not None else None,
            "valid_i": valid_i, "trigger_i": trigger_i,
            "direction": direction,
            "trigger_level": round(level, 4),
            "buffer": (buffer_pts if buf is None else buf),
            "perfect": perfect, "source": "st",
            "cross": "ST Break", "wt_val": None, "role": role,
        })

    for i in range(1, n):
        rv = res.iloc[i]; sv = sup.iloc[i]
        rv = None if (rv is None or pd.isna(rv)) else float(rv)
        sv = None if (sv is None or pd.isna(sv)) else float(sv)
        h = float(df.iloc[i]["High"])
        l = float(df.iloc[i]["Low"])
        c = float(df.iloc[i]["Close"])

        # --- 1. Nullify on level change ---
        if pend_buy is not None and (rv is None or rv != pend_buy["rs"]):
            pend_buy = None
        if pend_sell is not None and (sv is None or sv != pend_sell["rs"]):
            pend_sell = None

        # --- 2. Trigger check (strict only) ---
        if mode == "strict":
            if pend_buy is not None and i > pend_buy["i"] and h > pend_buy["level"] + buffer_pts:
                _emit("buy", pend_buy["i"], pend_buy["level"], i, pend_buy["rs"])
                pend_buy = None
            if pend_sell is not None and i > pend_sell["i"] and l < pend_sell["level"] - buffer_pts:
                _emit("sell", pend_sell["i"], pend_sell["level"], i, pend_sell["rs"])
                pend_sell = None

        # --- 3. Fresh valid candle ---
        if mode == "loose":
            # Loose: fire on every bar where price is beyond R/S.
            # Entry price = bar's Close (so each fill differs), and rapid
            # re-entry after a close is preserved.
            if rv is not None and h > rv + buffer_pts:
                _emit("buy", i, c, i, rv, buf=0.0)
            if sv is not None and l < sv - buffer_pts:
                _emit("sell", i, c, i, sv, buf=0.0)
        else:
            # Set the pending ONLY if empty. Do NOT replace an existing
            # pending — the FIRST valid candle wins until it triggers or
            # gets nullified by an R/S change.
            if pend_buy is None and rv is not None and h > rv and c > rv:
                pend_buy = {"i": i, "level": h, "rs": rv}
            if pend_sell is None and sv is not None and l < sv and c < sv:
                pend_sell = {"i": i, "level": l, "rs": sv}

    return events


def detect_st_events(df_real, params, buffer_pts, max_wait=20):
    """Two streams: entry-mode and exit-mode. Both use identical rules above.
    Duplicates (same valid_i, trigger_i, direction) merge into role='both'."""
    mod = get_indicator("st")
    comp = mod.compute(df_real, params)
    res = comp["resistance"]
    sup = comp["support"]

    entry_mode = params.get("entry_mode", params.get("mode", "strict"))
    exit_mode  = params.get("exit_mode",  params.get("mode", "strict"))

    entry_events = _detect_st_stream(df_real, res, sup, buffer_pts, entry_mode, "entry")
    exit_events  = _detect_st_stream(df_real, res, sup, buffer_pts, exit_mode,  "exit")

    key_of = lambda e: (e["valid_i"], e["trigger_i"], e["direction"])
    combined = {}
    for e in entry_events:
        combined[key_of(e)] = e
    for e in exit_events:
        k = key_of(e)
        if k in combined:
            combined[k]["role"] = "both"
        else:
            combined[k] = e
    return list(combined.values())


def combine_events(wt_events, ce_events, st_events, logic, match_window):
    sources_map = {"wt": wt_events, "ce": ce_events, "st": st_events}
    active = [lst for lst in sources_map.values() if lst]

    all_events = wt_events + ce_events + st_events
    all_events.sort(key=lambda e: e["trigger_i"])

    if logic == "any" or len(active) <= 1:
        return all_events

    kept = []
    for e in all_events:
        for src_key, src_list in sources_map.items():
            if src_key == e["source"]:
                continue
            for p in src_list:
                if p["direction"] == e["direction"] and abs(p["trigger_i"] - e["trigger_i"]) <= match_window:
                    kept.append(e)
                    break
            else:
                continue
            break
    return kept


# =========================================================================
# FILTER APPLICATION
# =========================================================================

def _apply_filters(events, df, htf_df, C, volume_series=None):
    """
    For each event, decide whether it passes all enabled filters.
    Tag: filtered (bool), filter_reasons (list of strings).
    volume_series: Volume from BEFORE heikin_ashi (HA strips it).
    """
    rsi = None
    vol_avg = None
    if C["rsi_filter_enabled"]:
        rsi = _rsi_series(df["Close"], C["rsi_period"])
    if C["vol_filter_enabled"] and volume_series is not None:
        vol_avg = volume_series.rolling(C["vol_lookback"]).mean()

    for e in events:
        reasons = []

        if C["wt_filter_enabled"] and e["source"] == "wt":
            v = e.get("wt_val")
            if v is None:
                reasons.append("WT none")
            elif e["direction"] == "buy" and v > C["wt_buy_max"]:
                reasons.append("WT>max")
            elif e["direction"] == "sell" and v < C["wt_sell_min"]:
                reasons.append("WT<min")

        if C["htf_filter_enabled"]:
            ts = df.index[e["valid_i"]]
            up = _htf_uptrend_at(htf_df, ts)
            if up is None:
                reasons.append("HTF none")
            elif e["direction"] == "buy" and not up:
                reasons.append("HTF down")
            elif e["direction"] == "sell" and up:
                reasons.append("HTF up")

        if C["rsi_filter_enabled"] and rsi is not None:
            i = e["valid_i"]
            if i < 1:
                reasons.append("RSI warmup")
            else:
                cur, prev = rsi.iloc[i], rsi.iloc[i - 1]
                if pd.isna(cur) or pd.isna(prev):
                    reasons.append("RSI warmup")
                elif e["direction"] == "buy" and cur <= prev:
                    reasons.append("RSI falling")
                elif e["direction"] == "sell" and cur >= prev:
                    reasons.append("RSI rising")

        if C["vol_filter_enabled"] and vol_avg is not None and volume_series is not None:
            v = volume_series.iloc[e["trigger_i"]]
            a = vol_avg.iloc[e["trigger_i"]]
            if pd.isna(v) or pd.isna(a) or a <= 0:
                reasons.append("Vol n/a")
            elif v < C["vol_mult"] * a:
                reasons.append("Vol low")

        e["filtered"] = len(reasons) > 0
        e["filter_reasons"] = reasons
    return events


# =========================================================================
# SIMULATORS
# =========================================================================

def _mk_trade(df, entry_i, exit_i, entry_px, exit_px, side_sign,
              source, perfect, pnl, notional, reason, extra=None):
    t = {
        "entry_i": entry_i, "exit_i": exit_i,
        "entry_t": str(df.index[entry_i]),
        "exit_t":  str(df.index[exit_i]),
        "entry_px": round(entry_px, 2),
        "exit_px":  round(exit_px, 2),
        "side": "long" if side_sign > 0 else "short",
        "key": source,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / abs(notional) * 100, 2) if notional else 0,
        "reason": reason,
        "bars": exit_i - entry_i,
        "perfect": perfect,
        "capital_used": round(abs(notional), 2),
    }
    if extra: t.update(extra)
    return t


def _simulate_simple(df, events_by_bar, n, C, res_series=None, sup_series=None):
    cash = float(C["capital"])
    units = 0.0; side = 0; entry = None
    equity = [cash]; trades = []
    pending = 0.0

    # ---- ST re-entry tracking ----
    st_reentry = C.get("st_reentry_mode", "standard")
    st_blocked_dir = None              # for "one_per_opposite"
    last_close_rs  = {"buy": None, "sell": None}   # for "fresh_r"

    def _maybe_block_on_close(closed_side, closed_source, closed_bar_i):
        nonlocal st_blocked_dir
        if closed_source != "st":
            return
        if st_reentry == "one_per_opposite":
            st_blocked_dir = "buy" if closed_side > 0 else "sell"
        elif st_reentry == "fresh_r":
            if closed_side > 0 and res_series is not None:
                try:
                    v = res_series.iloc[closed_bar_i]
                    last_close_rs["buy"] = None if pd.isna(v) else float(v)
                except Exception:
                    last_close_rs["buy"] = None
            elif closed_side < 0 and sup_series is not None:
                try:
                    v = sup_series.iloc[closed_bar_i]
                    last_close_rs["sell"] = None if pd.isna(v) else float(v)
                except Exception:
                    last_close_rs["sell"] = None

    for i in range(1, n):
        bar_h = float(df.iloc[i]["High"])
        bar_l = float(df.iloc[i]["Low"])
        bar_c = float(df.iloc[i]["Close"])

        if side != 0 and entry is not None:
            if side == 1 and bar_h > entry.get("best_price", entry["price"]):
                entry["best_price"] = bar_h
            if side == -1 and bar_l < entry.get("best_price", entry["price"]):
                entry["best_price"] = bar_l

            exit_px = None; reason = None; ep = entry["price"]
            qty_abs = abs(units)

            if C["stop_loss_pct"] > 0:
                stop = ep * (1 - side * C["stop_loss_pct"] / 100)
                if (side == 1 and bar_l <= stop) or (side == -1 and bar_h >= stop):
                    exit_px = stop; reason = "stop"
            if exit_px is None:
                tsl = _trailing_sl_price(C, ep, entry["best_price"], side)
                if tsl is not None:
                    if (side == 1 and bar_l <= tsl) or (side == -1 and bar_h >= tsl):
                        exit_px = tsl; reason = "trail_stop"
            if exit_px is None:
                tgt = _recovery_target_px(C, ep, qty_abs, side, pending)
                if tgt is not None:
                    if (side == 1 and bar_h >= tgt) or (side == -1 and bar_l <= tgt):
                        exit_px = tgt; reason = "target"

            if exit_px is not None:
                fill = exit_px * (1 - side * C["slippage_pct"] / 100)
                comm = abs(units * fill) * C["commission_pct"] / 100
                cash += units * fill - comm
                net = (fill - ep) * units - comm - entry["commission"]
                notional = abs(units) * ep
                _q = int(abs(units))
                pending = _update_pending(C, pending, net)
                trades.append(_mk_trade(df, entry["bar_i"], i, ep, fill, side,
                                        entry["source"], entry["perfect"],
                                        net, notional, reason,
                                        extra={"total_qty": _q,
                                               "pending_after": round(pending, 2),
                                               "entries_detail": [
                                                   {"t": str(df.index[entry["bar_i"]]),
                                                    "px": round(entry["price"], 2),
                                                    "qty": _q}]}))
                _maybe_block_on_close(side, entry["source"], i)
                units = 0.0; side = 0; entry = None

        # Pass 1 — exits
        for ev in events_by_bar.get(i, []):
            if ev.get("role", "both") not in ("exit", "both"): continue
            ev_sign = 1 if ev["direction"] == "buy" else -1
            ev_px   = ev["trigger_level"] + ev_sign * ev["buffer"]
            if side != 0 and side != ev_sign and C["exit_on_opposite"]:
                fill = ev_px * (1 - side * C["slippage_pct"] / 100)
                comm = abs(units * fill) * C["commission_pct"] / 100
                cash += units * fill - comm
                net = (fill - entry["price"]) * units - comm - entry["commission"]
                notional = abs(units) * entry["price"]
                _q = int(abs(units))
                pending = _update_pending(C, pending, net)
                trades.append(_mk_trade(df, entry["bar_i"], i, entry["price"], fill, side,
                                        entry["source"], entry["perfect"],
                                        net, notional, "opp_signal",
                                        extra={"total_qty": _q,
                                               "pending_after": round(pending, 2),
                                               "entries_detail": [
                                                   {"t": str(df.index[entry["bar_i"]]),
                                                    "px": round(entry["price"], 2),
                                                    "qty": _q}]}))
                _maybe_block_on_close(side, entry["source"], i)
                units = 0.0; side = 0; entry = None
                break

        # Unblock "one_per_opposite" when opposite ST signal fires
        if st_reentry == "one_per_opposite" and st_blocked_dir is not None:
            for e2 in events_by_bar.get(i, []):
                if e2["source"] != "st": continue
                if e2["direction"] != st_blocked_dir:
                    st_blocked_dir = None
                    break

        # Pass 2 — entries
        if side == 0:
            for ev in events_by_bar.get(i, []):
                if ev.get("role", "both") not in ("entry", "both"): continue
                if C["side_mode"] == "long"  and ev["direction"] == "sell": continue
                if C["side_mode"] == "short" and ev["direction"] == "buy":  continue
                if (st_reentry == "one_per_opposite"
                        and ev["source"] == "st"
                        and st_blocked_dir == ev["direction"]):
                    continue
                if st_reentry == "fresh_r" and ev["source"] == "st":
                    baseline = last_close_rs.get(ev["direction"])
                    if baseline is not None and ev.get("rs") is not None:
                        if float(ev["rs"]) == baseline:
                            continue
                ev_sign = 1 if ev["direction"] == "buy" else -1
                ev_px   = ev["trigger_level"] + ev_sign * ev["buffer"]
                fill = ev_px * (1 + ev_sign * C["slippage_pct"] / 100)
                qty = _qty_for(C, fill, cash)
                if qty <= 0: continue
                notional = qty * fill
                comm = notional * C["commission_pct"] / 100
                if ev_sign > 0:
                    cash -= notional + comm
                else:
                    cash += notional - comm
                units = ev_sign * qty
                side = ev_sign
                entry = {"bar_i": i, "price": fill, "source": ev["source"],
                         "perfect": ev["perfect"], "commission": comm,
                         "best_price": fill}
                break

        equity.append(cash + units * bar_c)

    if side != 0 and entry is not None:
        fill = float(df["Close"].iloc[-1])
        comm = abs(units * fill) * C["commission_pct"] / 100
        cash += units * fill - comm
        net = (fill - entry["price"]) * units - comm - entry["commission"]
        notional = abs(units) * entry["price"]
        _q = int(abs(units))
        pending = _update_pending(C, pending, net)
        _maybe_block_on_close(side, entry["source"], n - 1)
        trades.append(_mk_trade(df, entry["bar_i"], n - 1, entry["price"], fill, side,
                                entry["source"], entry["perfect"],
                                net, notional, "eod",
                                extra={"total_qty": _q,
                                       "pending_after": round(pending, 2),
                                       "entries_detail": [
                                           {"t": str(df.index[entry["bar_i"]]),
                                            "px": round(entry["price"], 2),
                                            "qty": _q}]}))
        equity[-1] = cash

    return trades, equity


def _simulate_same_side(df, events_by_bar, n, C):
    cash = float(C["capital"])
    equity = [cash]; trades = []

    allowed_dir = "buy" if C["side_mode"] == "long" else "sell"
    sign_total  = 1 if allowed_dir == "buy" else -1

    entries = []; total_qty = 0.0; total_cost = 0.0; level = 0
    best_price = None; base_qty_locked = None
    pending = 0.0

    def avg_entry():
        return total_cost / abs(total_qty) if total_qty != 0 else 0.0

    def close_all(i, fill_price, reason):
        nonlocal cash, total_qty, total_cost, level, entries, best_price, base_qty_locked, pending
        qty = int(abs(total_qty))
        fill = fill_price * (1 - sign_total * C["slippage_pct"] / 100)
        comm_exit = qty * fill * C["commission_pct"] / 100
        ep = avg_entry()
        if sign_total > 0:
            cash += qty * fill - comm_exit
            gross = (fill - ep) * qty
        else:
            cash -= qty * fill + comm_exit
            gross = (ep - fill) * qty
        total_comm = comm_exit + sum(e["comm"] for e in entries)
        net = gross - total_comm
        pending = _update_pending(C, pending, net)
        trades.append(_mk_trade(
            df, entries[0]["bar_i"], i, ep, fill, sign_total,
            entries[0]["source"], entries[0]["perfect"],
            net, total_cost, reason,
            extra={"levels": len(entries), "avg_entry": round(ep, 2),
                   "total_qty": qty,
                   "pending_after": round(pending, 2),
                   "entries_detail": [{"t": str(df.index[e["bar_i"]]),
                                       "px": round(e["price"], 2),
                                       "qty": e["qty"]} for e in entries]},
        ))
        entries = []; total_qty = 0.0; total_cost = 0.0; level = 0
        best_price = None; base_qty_locked = None

    for i in range(1, n):
        bar_h = float(df.iloc[i]["High"])
        bar_l = float(df.iloc[i]["Low"])
        bar_c = float(df.iloc[i]["Close"])

        if total_qty != 0:
            if sign_total > 0 and (best_price is None or bar_h > best_price):
                best_price = bar_h
            if sign_total < 0 and (best_price is None or bar_l < best_price):
                best_price = bar_l
            avg = avg_entry(); qty_abs = abs(total_qty); closed = False
            if C["stop_loss_pct"] > 0:
                stp = avg * (1 - sign_total * C["stop_loss_pct"] / 100)
                if (sign_total > 0 and bar_l <= stp) or (sign_total < 0 and bar_h >= stp):
                    close_all(i, stp, "stop"); closed = True
            if not closed and best_price is not None:
                tsl = _trailing_sl_price(C, avg, best_price, sign_total)
                if tsl is not None:
                    if (sign_total > 0 and bar_l <= tsl) or (sign_total < 0 and bar_h >= tsl):
                        close_all(i, tsl, "trail_stop"); closed = True
            if not closed:
                tgt = _recovery_target_px(C, avg, qty_abs, sign_total, pending)
                if tgt is not None:
                    if (sign_total > 0 and bar_h >= tgt) or (sign_total < 0 and bar_l <= tgt):
                        close_all(i, tgt, "target"); closed = True

        for ev in events_by_bar.get(i, []):
            if ev.get("role", "both") not in ("entry", "both"): continue
            if ev["direction"] != allowed_dir: continue
            if level >= C["max_levels"]:       continue
            ev_sign = 1 if ev["direction"] == "buy" else -1
            ev_px = ev["trigger_level"] + ev_sign * ev["buffer"]
            fill = ev_px * (1 + ev_sign * C["slippage_pct"] / 100)
            if level == 0:
                qty = _qty_for(C, fill, cash) if C["sizing_mode"] != "qty" \
                      else max(1, int(C["fixed_qty"]))
                if qty <= 0: continue
                base_qty_locked = qty
            else:
                qty = int(base_qty_locked * _multiplier(C["multiplier_type"], level))
                if qty <= 0: continue
            notional = qty * fill
            comm = notional * C["commission_pct"] / 100
            if ev_sign > 0:
                if notional + comm > cash: continue
                cash -= notional + comm
            else:
                cash += notional - comm
            total_qty += ev_sign * qty
            total_cost += notional
            entries.append({"bar_i": i, "price": fill, "qty": qty,
                            "comm": comm, "source": ev["source"],
                            "perfect": ev["perfect"]})
            level += 1

        equity.append(cash + total_qty * bar_c)

    if total_qty != 0:
        close_all(n - 1, float(df["Close"].iloc[-1]), "eod")
        equity[-1] = cash

    return trades, equity


def _simulate_alternate(df, events_by_bar, n, C):
    cash = float(C["capital"])
    equity = [cash]; trades = []

    in_cycle = False; position = None
    cycle_legs = []; cycle_realized = 0.0
    cycle_source = None; cycle_perfect = None; base_qty_locked = None
    pending = 0.0

    def _close_leg(i, raw_price, reason):
        nonlocal cash, position, cycle_realized
        p = position; qty = int(abs(p["qty"]))
        fill = raw_price * (1 - p["sign"] * C["slippage_pct"] / 100)
        comm = qty * fill * C["commission_pct"] / 100
        if p["sign"] > 0:
            cash += qty * fill - comm
            gross = (fill - p["price"]) * qty
        else:
            cash -= qty * fill + comm
            gross = (p["price"] - fill) * qty
        leg_pnl = gross - comm - p["comm"]
        cycle_realized += leg_pnl
        cycle_legs.append({
            "bar_in": p["bar_i"], "bar_out": i,
            "t_in": str(df.index[p["bar_i"]]), "t_out": str(df.index[i]),
            "px_in": round(p["price"], 2), "px_out": round(fill, 2),
            "qty": qty, "sign": p["sign"],
            "leg_pnl": round(leg_pnl, 2), "level": p["level"] + 1,
            "reason": reason,
            "source": p.get("source", ""), "perfect": p.get("perfect", ""),
        })
        position = None

    def _end_cycle(i, reason):
        nonlocal in_cycle, cycle_realized, cycle_legs, cycle_source, cycle_perfect, base_qty_locked, pending
        if not cycle_legs:
            in_cycle = False; cycle_realized = 0.0; cycle_legs = []
            cycle_source = None; cycle_perfect = None; base_qty_locked = None
            return
        first = cycle_legs[0]; last = cycle_legs[-1]
        total_pnl = sum(l["leg_pnl"] for l in cycle_legs)
        total_notional = sum(l["qty"] * l["px_in"] for l in cycle_legs)
        pending = _update_pending(C, pending, total_pnl)
        trades.append({
            "entry_i":  first["bar_in"], "exit_i": last["bar_out"],
            "entry_t":  first["t_in"], "exit_t": last["t_out"],
            "entry_px": first["px_in"], "exit_px": last["px_out"],
            "side":     "long" if first["sign"] > 0 else "short",
            "key":      cycle_source or "wt",
            "pnl":      round(total_pnl, 2),
            "pnl_pct":  round(total_pnl / total_notional * 100, 2) if total_notional else 0,
            "reason":   reason, "bars": last["bar_out"] - first["bar_in"],
            "perfect":  cycle_perfect or "",
            "levels":   len(cycle_legs), "total_qty": last["qty"],
            "capital_used": round(total_notional, 2),
            "pending_after": round(pending, 2),
            "entries_detail": [
                {"t": l["t_in"], "px": l["px_in"], "qty": l["qty"],
                 "sign": l["sign"], "leg_pnl": l["leg_pnl"],
                 "t_out": l["t_out"], "px_out": l["px_out"],
                 "reason": l["reason"], "perfect": l["perfect"]}
                for l in cycle_legs
            ],
        })
        in_cycle = False; cycle_realized = 0.0; cycle_legs = []
        cycle_source = None; cycle_perfect = None; base_qty_locked = None

    def _open_leg(i, raw_price, direction, level, source, perfect):
        nonlocal cash, position, base_qty_locked
        sign = 1 if direction == "buy" else -1
        if level == 0:
            qty = _qty_for(C, raw_price, cash) if C["sizing_mode"] != "qty" \
                  else max(1, int(C["fixed_qty"]))
            if qty <= 0: return False
            base_qty_locked = qty
        else:
            qty = int(base_qty_locked * _multiplier(C["multiplier_type"], level))
            if qty <= 0: return False
        fill = raw_price * (1 + sign * C["slippage_pct"] / 100)
        notional = qty * fill
        comm = notional * C["commission_pct"] / 100
        if sign > 0:
            if notional + comm > cash: return False
            cash -= notional + comm
        else:
            cash += notional - comm
        position = {"bar_i": i, "price": fill, "qty": qty, "sign": sign,
                    "comm": comm, "source": source, "perfect": perfect,
                    "level": level, "best_price": fill}
        return True

    def _target_amount():
        if C.get("recovery_enabled") and pending > 0:
            return pending + float(C.get("recovery_buffer_amount", 0.0) or 0.0)
        mode = C.get("profit_target_mode", "pct")
        val  = float(C.get("profit_target_value", 0) or 0)
        if val <= 0: return None
        if mode == "amount": return val
        first_notional = 0.0
        if cycle_legs:
            first_notional = cycle_legs[0]["qty"] * cycle_legs[0]["px_in"]
        elif position is not None:
            first_notional = abs(position["qty"]) * position["price"]
        return first_notional * val / 100

    for i in range(1, n):
        bar_h = float(df.iloc[i]["High"])
        bar_l = float(df.iloc[i]["Low"])
        bar_c = float(df.iloc[i]["Close"])

        if in_cycle and position is not None:
            if position["sign"] > 0 and bar_h > position["best_price"]:
                position["best_price"] = bar_h
            if position["sign"] < 0 and bar_l < position["best_price"]:
                position["best_price"] = bar_l

        if in_cycle and position is not None:
            target_amt = _target_amount()
            if target_amt is not None:
                qty_abs = abs(position["qty"])
                need = target_amt - cycle_realized
                if qty_abs > 0:
                    sign = position["sign"]
                    exit_target = position["price"] + sign * (need / qty_abs)
                    hit = ((sign > 0 and bar_h >= exit_target) or
                           (sign < 0 and bar_l <= exit_target))
                    if hit:
                        _close_leg(i, exit_target, "target")
                        _end_cycle(i, "target")

        if in_cycle and position is not None and C["stop_loss_pct"] > 0:
            ep = position["price"]; sign = position["sign"]
            stp = ep * (1 - sign * C["stop_loss_pct"] / 100)
            if (sign > 0 and bar_l <= stp) or (sign < 0 and bar_h >= stp):
                _close_leg(i, stp, "stop")
                _end_cycle(i, "stop")

        if in_cycle and position is not None:
            ep = position["price"]; sign = position["sign"]
            tsl = _trailing_sl_price(C, ep, position["best_price"], sign)
            if tsl is not None:
                if (sign > 0 and bar_l <= tsl) or (sign < 0 and bar_h >= tsl):
                    _close_leg(i, tsl, "trail_stop")
                    _end_cycle(i, "trail_stop")

        for ev in events_by_bar.get(i, []):
            role = ev.get("role", "both")
            ev_sign = 1 if ev["direction"] == "buy" else -1
            ev_px = ev["trigger_level"] + ev_sign * ev["buffer"]
            if not in_cycle:
                if role not in ("entry", "both"): continue
                if _open_leg(i, ev_px, ev["direction"], 0,
                             ev["source"], ev["perfect"]):
                    in_cycle = True; cycle_realized = 0.0; cycle_legs = []
                    cycle_source = ev["source"]; cycle_perfect = ev["perfect"]
            else:
                if role not in ("exit", "both"): continue
                if position is None: continue
                if ev_sign == position["sign"]: continue
                next_level = position["level"] + 1
                if next_level >= C["max_levels"]:
                    _close_leg(i, ev_px, "max_levels"); _end_cycle(i, "max_levels")
                    continue
                _close_leg(i, ev_px, "flip")
                if not _open_leg(i, ev_px, ev["direction"], next_level,
                                 ev["source"], ev["perfect"]):
                    _end_cycle(i, "flip_no_cash")

        qty_now = position["qty"] if position else 0
        equity.append(cash + qty_now * bar_c)

    if in_cycle and position is not None:
        _close_leg(n - 1, float(df["Close"].iloc[-1]), "eod")
        _end_cycle(n - 1, "eod")
        equity[-1] = cash

    return trades, equity


# =========================================================================
# MAIN
# =========================================================================

def run(symbol, interval="1h", period="6mo",
        indicators=None, params=None,
        capital=100000, position_pct=100,
        position_amount=5000, fixed_qty=1, sizing_mode="fixed",
        commission_pct=0.05, slippage_pct=0.02,
        buffer_pts=0.01,
        profit_target_pct=5, stop_loss_pct=2,
        profit_target_mode="pct", profit_target_value=None,
        exit_on_opposite=True,
        side_mode="both",
        logic="any", match_window=5,
        heikin_ashi_flag=True, fno_mode="none", fno_sheet="F&O_input", fno_list=None,
        st_entry_mode="strict",
        st_exit_mode="strict",
        st_reentry_mode="standard",
        wt_pending_rule="every",
        wait_bars=20,
        strategy_mode="simple",
        multiplier_type="martingale", base_qty=1, max_levels=5,
        recovery_enabled=False,
        recovery_cumulative=False,
        recovery_buffer_amount=20.0,
        recovery_trail_enabled=False,
        recovery_trail_premium=40.0,
        recovery_trail_step=5.0,
        trailing_enabled=False, trailing_start_pct=3.0, trailing_step_pct=2.0,
        trailing_tighten_enabled=False, trailing_tighten_at=9.0, trailing_tighten_step=1.0,
        wt_filter_enabled=False, wt_buy_max=-40.0, wt_sell_min=40.0,
        htf_filter_enabled=False,
        rsi_filter_enabled=False, rsi_period=14,
        vol_filter_enabled=False, vol_mult=1.5, vol_lookback=20,
        **_ignored):

    t0 = time.time()
    indicators = indicators or {"wt": True, "ce": False}
    params = params or {}

    rows = download(symbol, interval, period)
    if not rows or len(rows) < 40:
        return {"ok": False, "error": f"not enough data for {symbol}"}

    df = candles_to_df(rows)
    df = _to_ist(df)

    if fno_mode == "all" or (fno_mode == "sheet" and fno_list and symbol in fno_list):
        if interval in ("5m", "15m", "30m", "1h"):
            df = apply_fno_filter(df, interval)

    volume_series = df["Volume"] if "Volume" in df.columns else None
    df_real = df.copy()

    if heikin_ashi_flag:
        df = heikin_ashi(df)
    n = len(df)
    if n < 40:
        return {"ok": False, "error": "not enough bars after filtering"}

    wt_evts, ce_evts, st_evts = [], [], []
    if indicators.get("wt"):
        try:    wt_evts = detect_wt_events(df, params.get("wt", {}), buffer_pts,
                                            max_wait=wait_bars,
                                            pending_rule=wt_pending_rule)
        except Exception as e: print(f"⚠️ WT detect: {e}")
    if indicators.get("ce"):
        try:    ce_evts = detect_ce_events(df, params.get("ce", {}), buffer_pts, max_wait=wait_bars)
        except Exception as e: print(f"⚠️ CE detect: {e}")
    if indicators.get("st"):
        try:
            st_params = dict(params.get("st", {}))
            st_params.setdefault("entry_mode", st_entry_mode)
            st_params.setdefault("exit_mode",  st_exit_mode)
            st_evts = detect_st_events(df_real, st_params, buffer_pts, max_wait=wait_bars)
        except Exception as e: print(f"⚠️ ST detect: {e}")

    if strategy_mode in ("same_side", "alternate"):
        active_srcs = sum(1 for k in ("wt", "ce", "st") if indicators.get(k))
        if active_srcs > 1:
            return {"ok": False, "error": "Same-side / Alternate modes support only ONE signal source"}

    events = combine_events(wt_evts, ce_evts, st_evts, logic, match_window)
    if not events:
        return {"ok": False, "error": "no triggers found — lower buffer or change params"}

    # ST series (for the "fresh_r" re-entry mode)
    res_series = sup_series = None
    if indicators.get("st"):
        try:
            _st_mod = get_indicator("st")
            _comp = _st_mod.compute(df_real, params.get("st", {}))
            res_series = _comp["resistance"]
            sup_series = _comp["support"]
        except Exception as e:
            print(f"⚠️ ST series unavailable: {e}")

    htf_df = None
    if htf_filter_enabled:
        htf_df = _load_htf_trend(symbol, interval)
        if htf_df is None:
            print(f"⚠️ HTF data unavailable — filter disabled for {symbol}")

    C = {
        "capital": capital, "position_pct": position_pct,
        "position_amount": position_amount, "fixed_qty": fixed_qty,
        "sizing_mode": sizing_mode,
        "commission_pct": commission_pct, "slippage_pct": slippage_pct,
        "profit_target_pct": profit_target_pct,
        "profit_target_mode": profit_target_mode,
        "profit_target_value": (profit_target_value if profit_target_value is not None
                                else profit_target_pct),
        "stop_loss_pct": stop_loss_pct,
        "exit_on_opposite": exit_on_opposite, "side_mode": side_mode,
        "multiplier_type": multiplier_type, "base_qty": int(base_qty),
        "max_levels": max_levels,
        "st_reentry_mode": st_reentry_mode,
        "recovery_enabled": bool(recovery_enabled),
        "recovery_cumulative": bool(recovery_cumulative),
        "recovery_buffer_amount": float(recovery_buffer_amount),
        "recovery_trail_enabled": bool(recovery_trail_enabled),
        "recovery_trail_premium": float(recovery_trail_premium),
        "recovery_trail_step":    float(recovery_trail_step),
        "trailing_enabled": trailing_enabled,
        "trailing_start_pct": trailing_start_pct,
        "trailing_step_pct": trailing_step_pct,
        "trailing_tighten_enabled": trailing_tighten_enabled,
        "trailing_tighten_at": trailing_tighten_at,
        "trailing_tighten_step": trailing_tighten_step,
        "wt_filter_enabled": wt_filter_enabled,
        "wt_buy_max": wt_buy_max, "wt_sell_min": wt_sell_min,
        "htf_filter_enabled": htf_filter_enabled,
        "rsi_filter_enabled": rsi_filter_enabled, "rsi_period": rsi_period,
        "vol_filter_enabled": vol_filter_enabled,
        "vol_mult": vol_mult, "vol_lookback": vol_lookback,
    }

    events = _apply_filters(events, df, htf_df, C, volume_series)

    events_by_bar = {}; tradable_by_bar = {}
    for e in events:
        events_by_bar.setdefault(e["trigger_i"], []).append(e)
        if not e["filtered"]:
            tradable_by_bar.setdefault(e["trigger_i"], []).append(e)

    if not tradable_by_bar:
        return {"ok": False,
                "error": "all signals filtered out — loosen filters or disable them",
                "events": [{"valid_i": e["valid_i"], "trigger_i": e["trigger_i"],
                            "direction": e["direction"], "level": e["trigger_level"],
                            "perfect": e["perfect"], "source": e["source"],
                            "valid_t": str(df.index[e["valid_i"]]),
                            "trigger_t": str(df.index[e["trigger_i"]]),
                            "wt_val": e.get("wt_val"),
                            "filtered": True,
                            "filter_reasons": e.get("filter_reasons", [])}
                           for e in events]}

    if strategy_mode == "same_side":
        trades, equity = _simulate_same_side(df, tradable_by_bar, n, C)
    elif strategy_mode == "alternate":
        trades, equity = _simulate_alternate(df, tradable_by_bar, n, C)
    else:
        trades, equity = _simulate_simple(df, tradable_by_bar, n, C,
                                          res_series=res_series,
                                          sup_series=sup_series)

    final_eq = equity[-1]
    total_ret = (final_eq / capital - 1) * 100
    bh_ret = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[0]) - 1) * 100
    peak = equity[0]; max_dd = 0.0
    for e in equity:
        if e > peak: peak = e
        dd = (e - peak) / peak * 100
        if dd < max_dd: max_dd = dd
    bpy = {"5m": 75*252, "15m": 25*252, "30m": 13*252,
           "1h": 6*252, "1d": 252, "1wk": 52}.get(interval, 252)
    rets = [equity[k]/equity[k-1]-1 for k in range(1, len(equity)) if equity[k-1] > 0]
    sharpe = 0
    if len(rets) > 1 and statistics.stdev(rets) > 0:
        sharpe = (statistics.mean(rets) / statistics.stdev(rets)) * (bpy ** 0.5)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in losses)
    pf = gw / gl if gl > 0 else (999 if gw > 0 else 0)
    avg_w = gw / len(wins) if wins else 0
    avg_l = -gl / len(losses) if losses else 0
    exp_  = sum(t["pnl"] for t in trades) / len(trades) if trades else 0
    max_win  = max((t["pnl"] for t in trades), default=0)
    max_loss = min((t["pnl"] for t in trades), default=0)

    total_events = len(events)
    passed_events = sum(1 for e in events if not e["filtered"])
    filter_stats = {
        "total":   total_events,
        "passed":  passed_events,
        "wt":      sum(1 for e in events if any("WT" in r for r in e["filter_reasons"])),
        "htf":     sum(1 for e in events if any("HTF" in r for r in e["filter_reasons"])),
        "rsi":     sum(1 for e in events if any("RSI" in r for r in e["filter_reasons"])),
        "vol":     sum(1 for e in events if any("Vol" in r for r in e["filter_reasons"])),
    }

    candles_out = [{
        "t": str(df.index[i]),
        "o": round(float(df["Open"].iloc[i]), 2),
        "h": round(float(df["High"].iloc[i]), 2),
        "l": round(float(df["Low"].iloc[i]), 2),
        "c": round(float(df["Close"].iloc[i]), 2),
    } for i in range(n)]

    events_out = [{
        "valid_i": e["valid_i"], "trigger_i": e["trigger_i"],
        "direction": e["direction"], "level": e["trigger_level"],
        "perfect": e["perfect"], "source": e["source"],
        "valid_t": str(df.index[e["valid_i"]]),
        "trigger_t": str(df.index[e["trigger_i"]]),
        "wt_val": e.get("wt_val"),
        "filtered": e.get("filtered", False),
        "filter_reasons": e.get("filter_reasons", []),
    } for e in events]

    return {
        "ok": True, "symbol": symbol, "interval": interval, "period": period,
        "bars": n, "from": str(df.index[0]), "to": str(df.index[-1]),
        "candles": candles_out, "events": events_out,
        "trades": trades, "equity": [round(e, 2) for e in equity],
        "filter_stats": filter_stats,
        "metrics": {
            "capital": capital, "final_equity": round(final_eq, 2),
            "net_pnl": round(final_eq - capital, 2),
            "total_return_pct": round(total_ret, 2),
            "buy_hold_pct": round(bh_ret, 2),
            "max_dd_pct": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
            "trades": len(trades),
            "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2),
            "avg_win": round(avg_w, 2),
            "avg_loss": round(avg_l, 2),
            "expectancy": round(exp_, 2),
            "events": total_events,
            "events_passed": passed_events,
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
        },
        "ms": int((time.time() - t0) * 1000),
    }


# =========================================================================
# S/R DIAGNOSTIC — no order simulation, just segments
# =========================================================================

def extract_sr_segments(df, left=15, right=15, min_bars=2):
    """Report R/S segments the way TradingView draws them.

    Pine:  plot(fixnan(pivothigh(L,R)[1]), offset=-(R+1))
      - pivot_bar = p, value appears in series at p + R + 1 (due to [1])
      - offset shifts display back by R+1 → value is DRAWN at p

    So visually the segment starts at the pivot bar itself. That's what we
    report. The `usable_from_t` field tells you when the value actually
    became available for signal logic (pivot_bar + right + 1).

    Pivot rule: strictly greater than left bars AND strictly greater than
    right bars (matches Pine's pivothigh).
    """
    n = len(df)
    highs = df["High"].values
    lows  = df["Low"].values

    r_pivots = []   # (pivot_bar_index, level)
    s_pivots = []

    for j in range(left, n - right):
        left_h  = highs[j-left:j].max()
        right_h = highs[j+1:j+right+1].max()
        if highs[j] > left_h and highs[j] > right_h:
            r_pivots.append((j, float(highs[j])))

        left_l  = lows[j-left:j].min()
        right_l = lows[j+1:j+right+1].min()
        if lows[j] < left_l and lows[j] < right_l:
            s_pivots.append((j, float(lows[j])))

    def _build(pivots, kind):
        segs = []
        for k, (p, lvl) in enumerate(pivots):
            end_p = (pivots[k+1][0] - 1) if (k + 1 < len(pivots)) else (n - 1)
            bars  = end_p - p + 1
            if bars < min_bars:
                continue
            usable_i = min(p + right + 1, n - 1)
            segs.append({
                "type":    kind,
                "level":   round(lvl, 2),
                "start_i": p,
                "end_i":   end_p,
                "start_t": str(df.index[p]),
                "end_t":   str(df.index[end_p]),
                "bars":    bars,
                "usable_from_i": usable_i,
                "usable_from_t": str(df.index[usable_i]),
            })
        return segs

    return _build(r_pivots, "R") + _build(s_pivots, "S")


def run_sr_diagnostic(symbol, interval="1h", period="6mo",
                      left=15, right=15):
    """Fetch data, compute S/R, return segments + raw series for chart overlay."""
    t0 = time.time()
    rows = download(symbol, interval, period)
    if not rows or len(rows) < 40:
        return {"ok": False, "error": f"not enough data for {symbol}"}

    df = candles_to_df(rows)
    df = _to_ist(df)

    segments = extract_sr_segments(df, left=left, right=right, min_bars=2)

    candles = [{
        "t": str(df.index[i]),
        "o": round(float(df["Open"].iloc[i]), 2),
        "h": round(float(df["High"].iloc[i]), 2),
        "l": round(float(df["Low"].iloc[i]), 2),
        "c": round(float(df["Close"].iloc[i]), 2),
    } for i in range(len(df))]

    return {
        "ok": True,
        "symbol": symbol, "interval": interval, "period": period,
        "left": left, "right": right,
        "bars": len(df),
        "from": str(df.index[0]), "to": str(df.index[-1]),
        "candles": candles,
        "segments": segments,
        "ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    print("\n🧪 backtest.py self-test\n" + "─" * 60)
    res = run("HAL.NS", interval="1h", period="6mo",
              indicators={"wt": True, "ce": False},
              buffer_pts=1, profit_target_pct=1, stop_loss_pct=3,
              side_mode="both", strategy_mode="simple",
              htf_filter_enabled=True, rsi_filter_enabled=True,
              vol_filter_enabled=True)
    if res.get("ok"):
        m = res["metrics"]; fs = res["filter_stats"]
        print(f"bars: {res['bars']}  events: {fs['total']}  passed: {fs['passed']}")
        print(f"rejected: WT={fs['wt']}  HTF={fs['htf']}  RSI={fs['rsi']}  VOL={fs['vol']}")
        print(f"trades: {m['trades']}  win%: {m['win_rate']}  ret: {m['total_return_pct']}%  maxDD: {m['max_dd_pct']}%")
    else:
        print(f"❌ {res.get('error')}")
    print()