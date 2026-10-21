"""
Support/Resistance breakout indicator (LuxAlgo S/R style).
Same column set as WaveTrend, st_ prefix.

Valid candle logic:
  1. R = rolling latest confirmed pivot high (left/right window)
  2. Valid BUY candle = first bar with High > R AND Close > R after R sets
  3. Trigger level = valid candle's High
  4. Trigger = first bar after valid with High > trigger level
  5. Nullified if R value changes or a valid SELL candle appears later
Mirror for SELL with Support.

Mode toggle (params["mode"]):
  "strict" (default) — valid = High>R AND Close>R; trigger_level = valid.High
  "loose"            — valid = High>R (any cross);    trigger_level = R itself
"""
try:
    from .common import round_or_blank as _r, fmt_ist
except ImportError:
    import os, sys
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from common import round_or_blank as _r, fmt_ist

import pandas as pd


META = {
    "key": "st",
    "label": "Support/Resistance",
    "defaults": {"left": 15, "right": 15, "profit_target_pct": 5},
    "columns": [
        {"key": "st_resistance",         "label": "Resistance"},
        {"key": "st_support",            "label": "Support"},
        {"key": "st_trend",              "label": "ST Trend"},
        {"key": "st_valid_open",         "label": "Valid Open"},
        {"key": "st_valid_high",         "label": "Valid High"},
        {"key": "st_valid_low",          "label": "Valid Low"},
        {"key": "st_valid_close",        "label": "Valid Close"},
        {"key": "st_valid_dt",           "label": "Valid DT"},
        {"key": "st_valid_resistance",   "label": "Valid R/S"},
        {"key": "st_prev_close",         "label": "Prev Close"},
        {"key": "st_r_change_dt",        "label": "R Since"},
        {"key": "st_s_change_dt",        "label": "S Since"},
        {"key": "st_dist_to_res_pct",    "label": "To R %"},
        {"key": "st_dist_to_sup_pct",    "label": "To S %"},
        {"key": "st_rsi",                "label": "RSI"},
        {"key": "st_rsi_dir",            "label": "RSI Dir"},
        {"key": "st_perfect",            "label": "Perfect?"},
        {"key": "st_trigger_status",     "label": "Trig Status"},
        {"key": "st_trigger_dt",         "label": "Trig DT"},
        {"key": "st_action",             "label": "ST Action"},
        {"key": "st_trigger_candle_date","label": "Trig Date"},
        {"key": "st_trigger_candle_time","label": "Trig Time"},
        {"key": "st_ltp",                "label": "LTP"},
        {"key": "st_trigger_diff",       "label": "Diff"},
        {"key": "st_diff_pct",           "label": "Diff %"},
        {"key": "st_profit",             "label": "P/L ₹"},
        {"key": "st_booking_status",     "label": "Booking"},
        {"key": "st_max_profit_pct",     "label": "Max %"},
        {"key": "st_max_profit_value",   "label": "Max ₹"},
    ],
}


def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_g / avg_l.replace(0, float('nan'))
    return 100 - 100 / (1 + rs)


def _compute_sr(df, left=15, right=15):
    """Non-repainting rolling R/S.
    At bar t, R(t) = high of the most recent confirmed pivot high whose value
    became available at or before bar t. Availability is pivot_bar + right + 1
    (matches Pine's pivothigh(...)[1]).

    Pivot rule: strictly greater than ALL left bars AND strictly greater
    than ALL right bars. This matches Pine's pivothigh — ties are ignored.
    """
    highs = df["High"].values
    lows  = df["Low"].values
    n     = len(df)

    res = [None] * n
    sup = [None] * n

    pivot_highs_at = {}   # confirm_bar -> pivot value
    pivot_lows_at  = {}
    for j in range(left, n - right):
        left_h  = highs[j-left:j].max()
        right_h = highs[j+1:j+right+1].max()
        if highs[j] > left_h and highs[j] > right_h:
            cb = j + right + 1
            if cb < n:
                pivot_highs_at[cb] = float(highs[j])

        left_l  = lows[j-left:j].min()
        right_l = lows[j+1:j+right+1].min()
        if lows[j] < left_l and lows[j] < right_l:
            cb = j + right + 1
            if cb < n:
                pivot_lows_at[cb] = float(lows[j])

    last_r = None
    last_s = None
    for i in range(n):
        if i in pivot_highs_at:
            last_r = pivot_highs_at[i]
        if i in pivot_lows_at:
            last_s = pivot_lows_at[i]
        res[i] = last_r
        sup[i] = last_s

    return (pd.Series(res, index=df.index),
            pd.Series(sup, index=df.index))


def compute(df, params):
    left  = int(params.get("left",  META["defaults"]["left"]))
    right = int(params.get("right", META["defaults"]["right"]))
    res, sup = _compute_sr(df, left=left, right=right)
    rsi = _rsi(df["Close"], 14)
    return {"resistance": res, "support": sup, "rsi": rsi}


_EMPTY_KEYS = [
    "st_resistance","st_support","st_trend",
    "st_valid_open","st_valid_high","st_valid_low","st_valid_close",
    "st_valid_dt","st_valid_resistance",
    "st_prev_close","st_r_change_dt","st_s_change_dt",
    "st_dist_to_res_pct","st_dist_to_sup_pct",
    "st_rsi","st_rsi_dir","st_perfect",
    "st_trigger_status","st_trigger_dt","st_action",
    "st_trigger_candle_date","st_trigger_candle_time",
    "st_ltp","st_trigger_diff","st_diff_pct","st_profit",
    "st_booking_status","st_max_profit_pct","st_max_profit_value",
]


def _find_latest_valid(df, series, i_last, direction, mode="strict"):
    """Find the most recent valid candle whose R/S value is still current.
    strict : valid = high>R AND close>R; trigger_level = valid.high
    loose  : valid = high>R (any cross); trigger_level = R itself (immediate)
    Returns (valid_idx, rs_value, trigger_level) or (None, None, None)."""
    if i_last < 1:
        return None, None, None
    current = series.iloc[i_last]
    if current is None or pd.isna(current):
        return None, None, None

    # Walk backward to find the bar where R/S last changed value
    start = 0
    for i in range(i_last, 0, -1):
        if series.iloc[i] != series.iloc[i-1]:
            start = i
            break
    else:
        start = 0

    # Forward scan from start: first bar that satisfies the valid-candle rule
    for i in range(start, i_last + 1):
        rv = series.iloc[i]
        if rv is None or pd.isna(rv):
            continue
        bar = df.iloc[i]
        if mode == "loose":
            # Any candle that touches/crosses R is valid + trigger, level = R
            if direction == "buy" and bar["High"] > rv:
                return i, float(rv), float(rv)
            if direction == "sell" and bar["Low"] < rv:
                return i, float(rv), float(rv)
        else:
            if direction == "buy":
                if bar["High"] > rv and bar["Close"] > rv:
                    return i, float(rv), float(bar["High"])
            else:
                if bar["Low"] < rv and bar["Close"] < rv:
                    return i, float(rv), float(bar["Low"])
    return None, None, None


def _sr_change_dt(series, i_last):
    """Return the IST timestamp when this S/R series last changed value."""
    cur = series.iloc[i_last]
    if cur is None or pd.isna(cur):
        return ""
    for i in range(i_last, 0, -1):
        prev = series.iloc[i - 1]
        if prev is None or pd.isna(prev) or prev != cur:
            return fmt_ist(series.index[i])
    return ""


def _dist_pct(level, ltp, side):
    if level is None or pd.isna(level) or not ltp:
        return ""
    try:
        if side == "up":
            return round((float(level) - float(ltp)) / float(ltp) * 100, 2)
        return round((float(ltp) - float(level)) / float(ltp) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def signals(df, params, computed, state=None):
    res = computed["resistance"]
    sup = computed["support"]

    blank = {k: "" for k in _EMPTY_KEYS}
    blank["st_triggered"] = False
    blank["_new_state"]   = state
    if len(df) < 6:
        return blank

    i_last = len(df) - 2
    r_now  = res.iloc[i_last]
    s_now  = sup.iloc[i_last]
    mode   = params.get("mode", "strict")

    # Find latest valid candle for each direction
    v_buy,  r_used_buy,  trig_buy  = _find_latest_valid(df, res, i_last, "buy",  mode)
    v_sell, s_used_sell, trig_sell = _find_latest_valid(df, sup, i_last, "sell", mode)

    # Opposite-direction nullification: latest wins
    if v_buy is not None and v_sell is not None:
        if v_buy >= v_sell:
            v_sell, s_used_sell, trig_sell = None, None, None
        else:
            v_buy, r_used_buy, trig_buy = None, None, None

    valid_idx     = None
    direction     = ""
    valid_rs      = None
    trigger_level = None
    if v_buy is not None:
        valid_idx, direction, valid_rs, trigger_level = v_buy, "buy", r_used_buy, trig_buy
    elif v_sell is not None:
        valid_idx, direction, valid_rs, trigger_level = v_sell, "sell", s_used_sell, trig_sell

    # Trend label from current close vs current R/S
    last_close = float(df["Close"].iloc[i_last])
    if r_now is not None and not pd.isna(r_now) and last_close > float(r_now):
        trend = "Buy Trend"
    elif s_now is not None and not pd.isna(s_now) and last_close < float(s_now):
        trend = "Sell Trend"
    else:
        trend = "Inside Trend"

    # Valid candle bar values
    v_open = v_high = v_low = v_close = v_dt = ""
    perfect = ""
    if valid_idx is not None:
        c = df.iloc[valid_idx]
        v_open  = _r(c["Open"])
        v_high  = _r(c["High"])
        v_low   = _r(c["Low"])
        v_close = _r(c["Close"])
        v_dt    = fmt_ist(df.index[valid_idx])
        if direction == "buy":
            perfect = "Perfect" if c["Open"] == c["Low"] else "Imperfect"
        else:
            perfect = "Perfect" if c["Open"] == c["High"] else "Imperfect"

    # Trigger search
    trigger_idx = None
    if valid_idx is not None and trigger_level is not None:
        if mode == "loose":
            # In loose mode the valid candle IS the trigger candle
            trigger_idx = valid_idx
        else:
            for i in range(valid_idx + 1, i_last + 1):
                if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                    trigger_idx = i; break
                if direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                    trigger_idx = i; break

    live = params.get("live_ltp")
    ltp = float(live) if live is not None else float(df["Close"].iloc[i_last])

    action = ""
    if trigger_level is not None:
        if trigger_idx is not None:
            action = "Buy Market Order" if direction == "buy" else "Sell Market Order"
        elif direction == "buy":
            action = ("Buy Market Order" if ltp > trigger_level
                      else f"Buy Limit Order (BO close {trigger_level})")
        else:
            action = ("Sell Market Order" if ltp < trigger_level
                      else f"Sell Limit Order (BO close {trigger_level})")

    trigger_status = ""
    trigger_dt     = ""
    new_state      = state
    if valid_idx is not None:
        if (state is None
            or state.get("direction") != direction
            or state.get("datetime")  != v_dt):
            trigger_status = "New Trigger"
            trigger_dt     = v_dt
            new_state      = {"direction": direction, "datetime": v_dt,
                              "level": trigger_level}
        else:
            trigger_dt = state.get("datetime", "")

    t_date = t_time = ""
    if trigger_idx is not None:
        dt_str = fmt_ist(df.index[trigger_idx])
        if " " in dt_str:
            t_date, t_time = dt_str.split(" ", 1)

    diff = diff_pct = profit = ""
    invest = float(params.get("investment", 5000))
    if trigger_level is not None and direction in ("buy", "sell"):
        diff = round(ltp - trigger_level, 2) if direction == "buy" \
               else round(trigger_level - ltp, 2)
        if trigger_level != 0:
            diff_pct = round(diff / trigger_level * 100, 2)
        if trigger_level:
            profit = round((invest // trigger_level) * diff, 2)

    max_pct = max_val = 0.0
    booking = ""
    if trigger_idx is not None and direction in ("buy", "sell"):
        window = df.iloc[trigger_idx : i_last + 1]
        current_close = float(df["Close"].iloc[i_last])
        entry = trigger_level
        if direction == "buy":
            max_price = float(window["High"].max())
            max_pct   = round((max_price - entry) / entry * 100, 2)
            current_pct = round((current_close - entry) / entry * 100, 2)
            max_val   = round(max_price - entry, 2)
        else:
            min_price = float(window["Low"].min())
            max_pct   = round((entry - min_price) / entry * 100, 2)
            current_pct = round((entry - current_close) / entry * 100, 2)
            max_val   = round(entry - min_price, 2)
        target = float(params.get("profit_target_pct", 5))
        if max_pct >= target:
            booking = f"Profit booked at {target}%"
        elif max_pct > 0 and current_pct > 0:
            booking = f"Profit in progress of {max_pct:.1f}%"
        elif max_pct > 0 and current_pct <= 0:
            booking = f"Booked maximum profit of {max_pct:.1f}%"
        else:
            booking = "No profit yet"

    rsi_val = None; rsi_dir = ""
    if valid_idx is not None and "rsi" in computed:
        r = computed["rsi"].iloc[valid_idx]
        if r == r:
            rsi_val = round(float(r), 2)
            if valid_idx >= 1:
                rp = computed["rsi"].iloc[valid_idx - 1]
                if rp == rp:
                    if r > rp:   rsi_dir = "rising"
                    elif r < rp: rsi_dir = "falling"
                    else:        rsi_dir = "flat"

    return {
        "st_resistance":         _r(r_now) if r_now is not None and not pd.isna(r_now) else "",
        "st_support":            _r(s_now) if s_now is not None and not pd.isna(s_now) else "",
        "st_trend":              trend,
        "st_valid_open":         v_open,
        "st_valid_high":         v_high,
        "st_valid_low":          v_low,
        "st_valid_close":        v_close,
        "st_valid_dt":           v_dt,
        "st_valid_resistance":   _r(valid_rs) if valid_rs is not None else "",
        "st_prev_close":         _r(df["Close"].iloc[i_last - 1]) if i_last >= 1 else "",
        "st_r_change_dt":        _sr_change_dt(res, i_last),
        "st_s_change_dt":        _sr_change_dt(sup, i_last),
        "st_dist_to_res_pct":    _dist_pct(r_now, ltp, "up"),
        "st_dist_to_sup_pct":    _dist_pct(s_now, ltp, "down"),
        "st_rsi":                rsi_val if rsi_val is not None else "",
        "st_rsi_dir":            rsi_dir,
        "st_perfect":            perfect,
        "st_trigger_status":     trigger_status,
        "st_trigger_dt":         trigger_dt,
        "st_action":             action,
        "st_trigger_candle_date": t_date,
        "st_trigger_candle_time": t_time,
        "st_ltp":                _r(ltp),
        "st_trigger_diff":       diff,
        "st_diff_pct":           diff_pct,
        "st_profit":             profit,
        "st_booking_status":     booking,
        "st_max_profit_pct":     max_pct,
        "st_max_profit_value":   max_val,
        "st_triggered":          trigger_idx is not None,
        "_new_state":            new_state,
    }


if __name__ == "__main__":
    import os, sys, math
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import candles_to_df
    print("\n🧪 support_resistance.py self-test\n" + "─" * 78)
    rows = []
    price = 100.0
    for i in range(300):
        drift = math.sin(i / 12) * 1.6 + math.sin(i / 5) * 0.5
        o = price; c = o + drift
        h = max(o, c) + 0.4; l = min(o, c) - 0.4
        ts = f"2025-01-{1 + (i // 24):02d}T{i % 24:02d}:00:00"
        rows.append({"t": ts, "o": o, "h": h, "l": l, "c": c, "v": 1000 + i})
        price = c
    df = candles_to_df(rows)
    comp = compute(df, META["defaults"])
    sig  = signals(df, META["defaults"], comp, state=None)
    print("signals() — first run:\n")
    for k, v in sig.items():
        if k.startswith("_"): continue
        print(f"  {k:>26} : {v}")
    print("\n✅ support_resistance.py works.\n")