"""
Chandelier Exit indicator — full-column version.
Mirrors in_t3_wt_ce.py's CE section byte-for-byte.
"""

try:
    from .common import round_or_blank as _r, fmt_ist
except ImportError:
    import os, sys
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from common import round_or_blank as _r, fmt_ist


META = {
    "key": "ce",
    "label": "Chandelier Exit",
    "defaults": {"length": 22, "mult": 3.0, "use_close": True,
                 "profit_target_pct": 5},
    "columns": [
        {"key": "ce_dir",                    "label": "CE Dir"},
        {"key": "ce_valid_open",             "label": "Valid Open"},
        {"key": "ce_valid_high",             "label": "Valid High"},
        {"key": "ce_valid_low",              "label": "Valid Low"},
        {"key": "ce_valid_close",            "label": "Valid Close"},
        {"key": "ce_valid_dt",               "label": "Valid DT"},
        {"key": "ce_valid_stop",             "label": "Valid Stop"},
        {"key": "ce_perfect",                "label": "Perfect?"},
        {"key": "ce_trigger_status",         "label": "Trig Status"},
        {"key": "ce_trigger_dt",             "label": "Trig DT"},
        {"key": "ce_action",                 "label": "CE Action"},
        {"key": "ce_trigger_candle_date",    "label": "Trig Date"},
        {"key": "ce_trigger_candle_time",    "label": "Trig Time"},
        {"key": "ce_ltp",                    "label": "LTP"},
        {"key": "ce_trigger_diff",           "label": "Diff"},
        {"key": "ce_diff_pct",               "label": "Diff %"},
        {"key": "ce_profit",                 "label": "P/L ₹"},
        {"key": "ce_booking_status",         "label": "Booking"},
        {"key": "ce_max_profit_pct",         "label": "Max %"},
        {"key": "ce_max_profit_value",       "label": "Max ₹"},
    ],
}


# ============================================================
#  MATH — same as in_t3_wt_ce.py
# ============================================================
def _compute_ce(df, length=22, mult=3.0, use_close=True):
    import pandas as pd

    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    alpha = 1.0 / length
    atr = tr.ewm(alpha=alpha, adjust=False).mean()

    if use_close:
        highest = close.rolling(window=length).max()
        lowest  = close.rolling(window=length).min()
    else:
        highest = high.rolling(window=length).max()
        lowest  = low.rolling(window=length).min()

    long_stop  = highest - mult * atr
    short_stop = lowest  + mult * atr

    long_prev = short_prev = None
    long_s, short_s, dir_s, buy_s, sell_s = [], [], [], [], []
    direction = 1

    for i in range(len(df)):
        if i == 0:
            ls_val, ss_val = long_stop.iloc[i], short_stop.iloc[i]
        else:
            ls_val = long_stop.iloc[i]
            if close.iloc[i - 1] > long_prev:
                ls_val = max(ls_val, long_prev)
            ss_val = short_stop.iloc[i]
            if close.iloc[i - 1] < short_prev:
                ss_val = min(ss_val, short_prev)

        long_prev, short_prev = ls_val, ss_val
        long_s.append(ls_val); short_s.append(ss_val)

        if i == 0:
            dir_val = 1
        else:
            if close.iloc[i] > short_prev:
                dir_val = 1
            elif close.iloc[i] < long_prev:
                dir_val = -1
            else:
                dir_val = direction
        direction = dir_val
        dir_s.append(dir_val)

        if i > 0:
            buy_s.append(dir_val == 1  and dir_s[i - 1] == -1)
            sell_s.append(dir_val == -1 and dir_s[i - 1] == 1)
        else:
            buy_s.append(False); sell_s.append(False)

    return {
        "long_stop":   pd.Series(long_s,  index=df.index),
        "short_stop":  pd.Series(short_s, index=df.index),
        "direction":   pd.Series(dir_s,   index=df.index),
        "buy_signal":  pd.Series(buy_s,   index=df.index),
        "sell_signal": pd.Series(sell_s,  index=df.index),
    }


def compute(df, params):
    length    = int(params.get("length",    META["defaults"]["length"]))
    mult      = float(params.get("mult",    META["defaults"]["mult"]))
    use_close = bool(params.get("use_close", META["defaults"]["use_close"]))
    return _compute_ce(df, length=length, mult=mult, use_close=use_close)


# ============================================================
#  SIGNALS — full column set
# ============================================================
_EMPTY_KEYS = [
    "ce_dir","ce_valid_open","ce_valid_high","ce_valid_low","ce_valid_close",
    "ce_valid_dt","ce_valid_stop","ce_perfect",
    "ce_trigger_status","ce_trigger_dt","ce_action",
    "ce_trigger_candle_date","ce_trigger_candle_time",
    "ce_ltp","ce_trigger_diff","ce_diff_pct","ce_profit",
    "ce_booking_status","ce_max_profit_pct","ce_max_profit_value",
]


def signals(df, params, computed, state=None):
    dir_series = computed["direction"]
    long_stop  = computed["long_stop"]
    short_stop = computed["short_stop"]

    blank = {k: "" for k in _EMPTY_KEYS}
    blank["ce_triggered"] = False
    blank["_new_state"]   = state

    if len(df) < 4 or len(dir_series) < 4:
        return blank

    i_last = len(df) - 2
    ce_dir = int(dir_series.iloc[i_last])
    ce_label = "Long" if ce_dir == 1 else "Short" if ce_dir == -1 else ""

    # --- find last direction flip ---
    valid_idx = None
    if ce_dir in (1, -1):
        for i in range(len(dir_series) - 2, 0, -1):
            if dir_series.iloc[i] != dir_series.iloc[i - 1]:
                valid_idx = i
                break

    # --- valid candle values ---
    v_open = v_high = v_low = v_close = v_dt = v_stop = ""
    perfect = ""
    if valid_idx is not None:
        c = df.iloc[valid_idx]
        v_open  = _r(c["Open"])
        v_high  = _r(c["High"])
        v_low   = _r(c["Low"])
        v_close = _r(c["Close"])
        v_dt    = fmt_ist(df.index[valid_idx])
        v_stop  = _r(long_stop.iloc[valid_idx] if ce_dir == 1
                     else short_stop.iloc[valid_idx])
        if ce_dir == 1:
            perfect = "Perfect" if c["Open"] == c["Low"] else "Imperfect"
        else:
            perfect = "Perfect" if c["Open"] == c["High"] else "Imperfect"

    # --- trigger level + direction ---
    trigger_level = None
    direction = ""
    if valid_idx is not None:
        direction = "buy" if ce_dir == 1 else "sell"
        trigger_level = float(v_high if direction == "buy" else v_low)

    # --- breakout? ---
    trigger_idx = None
    if trigger_level is not None:
        for i in range(valid_idx + 1, len(df) - 1):
            if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                trigger_idx = i; break
            if direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                trigger_idx = i; break

    # --- LTP (live price from server, fallback to HA close) ---
    live = params.get("live_ltp")
    ltp  = float(live) if live is not None else float(df["Close"].iloc[i_last])

    # --- action string ---
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

    # --- trigger status via persistent state ---
    trigger_status = ""
    trigger_dt     = ""
    new_state      = state
    if valid_idx is not None and direction in ("buy", "sell"):
        if (state is None
            or state.get("direction") != direction
            or state.get("datetime")  != v_dt):
            trigger_status = "New Trigger"
            trigger_dt     = v_dt
            new_state      = {"direction": direction, "datetime": v_dt,
                              "level": trigger_level}
        else:
            trigger_dt = state.get("datetime", "")

    # --- trigger candle date/time ---
    t_date = t_time = ""
    if trigger_idx is not None:
        dt_str = fmt_ist(df.index[trigger_idx])
        if " " in dt_str:
            t_date, t_time = dt_str.split(" ", 1)

    # --- diff + profit ---
    diff = diff_pct = profit = ""
    invest = float(params.get("investment", 5000))
    if trigger_level is not None and direction in ("buy", "sell"):
        diff = round(ltp - trigger_level, 2) if direction == "buy" \
               else round(trigger_level - ltp, 2)
        if trigger_level != 0:
            diff_pct = round(diff / trigger_level * 100, 2)
        if trigger_level:
            profit = round((invest // trigger_level) * diff, 2)

    # --- max profit + booking status ---
    max_pct = max_val = 0.0
    booking = ""
    if trigger_idx is not None and direction in ("buy", "sell"):
        window = df.iloc[trigger_idx : len(df) - 1]
        current_close = float(df["Close"].iloc[i_last])
        entry = trigger_level
        if direction == "buy":
            max_price = float(window["High"].max())
            max_pct = round((max_price - entry) / entry * 100, 2)
            current_pct = round((current_close - entry) / entry * 100, 2)
            max_val = round(max_price - entry, 2)
        else:
            min_price = float(window["Low"].min())
            max_pct = round((entry - min_price) / entry * 100, 2)
            current_pct = round((entry - current_close) / entry * 100, 2)
            max_val = round(entry - min_price, 2)

        target = float(params.get("profit_target_pct", 5))
        if max_pct >= target:
            booking = f"Profit booked at {target}%"
        elif max_pct > 0 and current_pct > 0:
            booking = f"Profit in progress of {max_pct:.1f}%"
        elif max_pct > 0 and current_pct <= 0:
            booking = f"Booked maximum profit of {max_pct:.1f}%"
        else:
            booking = "No profit yet"

    return {
        "ce_dir":                 ce_label,
        "ce_valid_open":          v_open,
        "ce_valid_high":          v_high,
        "ce_valid_low":           v_low,
        "ce_valid_close":         v_close,
        "ce_valid_dt":            v_dt,
        "ce_valid_stop":          v_stop,
        "ce_perfect":             perfect,
        "ce_trigger_status":      trigger_status,
        "ce_trigger_dt":          trigger_dt,
        "ce_action":              action,
        "ce_trigger_candle_date": t_date,
        "ce_trigger_candle_time": t_time,
        "ce_ltp":                 _r(ltp),
        "ce_trigger_diff":        diff,
        "ce_diff_pct":            diff_pct,
        "ce_profit":              profit,
        "ce_booking_status":      booking,
        "ce_max_profit_pct":      max_pct,
        "ce_max_profit_value":    max_val,
        "ce_triggered":           trigger_idx is not None,
        "_new_state":             new_state,
    }


# ============================================================
#  SELF-TEST — python indicators/chandelier.py
# ============================================================
if __name__ == "__main__":
    import os, sys, math
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import candles_to_df

    print("\n🧪 chandelier.py self-test\n" + "─" * 78)

    rows = []
    price = 100.0
    for i in range(150):
        drift = math.sin(i / 12) * 1.6 + math.sin(i / 5) * 0.5
        o = price; c = o + drift
        h = max(o, c) + 0.5; l = min(o, c) - 0.5
        ts = f"2025-01-{1 + (i // 24):02d}T{i % 24:02d}:00:00"
        rows.append({"t": ts, "o": o, "h": h, "l": l, "c": c, "v": 1000 + i})
        price = c
    df = candles_to_df(rows)

    comp = compute(df, META["defaults"])
    sig  = signals(df, META["defaults"], comp, state=None)

    print("signals() — first run (state=None):\n")
    for k, v in sig.items():
        if k.startswith("_"): continue
        print(f"  {k:>24} : {v}")

    sig2 = signals(df, META["defaults"], comp, state=sig["_new_state"])
    print(f"\nsecond run  ce_trigger_status : {sig2['ce_trigger_status'] or '(none)'}")
    print("\n✅ chandelier.py works.\n")