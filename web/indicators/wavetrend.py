"""
WaveTrend indicator — full-column version.
Mirrors in_t3_wt_ce.py's WT section byte-for-byte.
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
    "key": "wt",
    "label": "WaveTrend",
    "defaults": {"n1": 15, "n2": 27, "profit_target_pct": 5},
    "columns": [
        {"key": "wt1",                  "label": "WT1"},
        {"key": "wt2",                  "label": "WT2"},
        {"key": "wt_diff",              "label": "WT Diff"},
        {"key": "wt_cross",             "label": "WT Cross"},
        {"key": "wt_trend",             "label": "WT Trend"},
        {"key": "wt_valid_open",        "label": "Valid Open"},
        {"key": "wt_valid_high",        "label": "Valid High"},
        {"key": "wt_valid_low",         "label": "Valid Low"},
        {"key": "wt_valid_close",       "label": "Valid Close"},
        {"key": "wt_valid_dt",          "label": "Valid DT"},
        {"key": "wt_valid_diff",        "label": "Valid Diff"},
        {"key": "wt_valid_wt_value",    "label": "Valid WT Val"},
        {"key": "wt_rsi",               "label": "RSI"},
        {"key": "wt_rsi_dir",           "label": "RSI Dir"},
        {"key": "wt_perfect",           "label": "Perfect?"},
        {"key": "wt_trigger_status",    "label": "Trig Status"},
        {"key": "wt_trigger_dt",        "label": "Trig DT"},
        {"key": "wt_action",            "label": "WT Action"},
        {"key": "wt_trigger_candle_date","label": "Trig Date"},
        {"key": "wt_trigger_candle_time","label": "Trig Time"},
        {"key": "wt_ltp",               "label": "LTP"},
        {"key": "wt_trigger_diff",      "label": "Diff"},
        {"key": "wt_diff_pct",          "label": "Diff %"},
        {"key": "wt_profit",            "label": "P/L ₹"},
        {"key": "wt_booking_status",    "label": "Booking"},
        {"key": "wt_max_profit_pct",    "label": "Max %"},
        {"key": "wt_max_profit_value",  "label": "Max ₹"},
    ],
}


# ============================================================
#  MATH — same as in_t3_wt_ce.py
# ============================================================
def _compute_wavetrend(df, n1=15, n2=27):
    import pandas as pd
    ap  = (df["High"] + df["Low"] + df["Close"]) / 3
    esa = ap.ewm(span=n1, adjust=False).mean()
    d   = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
    ci  = (ap - esa) / (0.015 * d)
    tci = ci.ewm(span=n2, adjust=False).mean()

    wt1     = tci
    wt2     = wt1.rolling(window=4).mean()
    wt_diff = wt1 - wt2

    cross = pd.Series(index=df.index, dtype="object")
    for i in range(1, len(wt1)):
        a0, b0 = wt1.iloc[i - 1], wt2.iloc[i - 1]
        a1, b1 = wt1.iloc[i],     wt2.iloc[i]
        if a0 < b0 and a1 > b1:
            cross.iloc[i] = "Bull Cross"
        elif a0 > b0 and a1 < b1:
            cross.iloc[i] = "Bear Cross"
        else:
            cross.iloc[i] = "No Cross"
    if len(cross) > 0:
        cross.iloc[0] = "No Cross"
    return wt1, wt2, wt_diff, cross


def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_g / avg_l.replace(0, float('nan'))
    return 100 - 100 / (1 + rs)


def compute(df, params):
    n1 = int(params.get("n1", META["defaults"]["n1"]))
    n2 = int(params.get("n2", META["defaults"]["n2"]))
    wt1, wt2, wt_diff, cross = _compute_wavetrend(df, n1=n1, n2=n2)
    rsi = _rsi(df["Close"], 14)
    return {"wt1": wt1, "wt2": wt2, "wt_diff": wt_diff, "cross": cross, "rsi": rsi}


# ============================================================
#  SIGNALS — full column set
# ============================================================
_EMPTY_KEYS = [
    "wt1","wt2","wt_diff","wt_cross","wt_trend",
    "wt_valid_open","wt_valid_high","wt_valid_low","wt_valid_close",
    "wt_valid_dt","wt_valid_diff","wt_valid_wt_value",
    "wt_rsi","wt_rsi_dir",
    "wt_perfect",
    "wt_trigger_status","wt_trigger_dt","wt_action",
    "wt_trigger_candle_date","wt_trigger_candle_time",
    "wt_ltp","wt_trigger_diff","wt_diff_pct","wt_profit",
    "wt_booking_status","wt_max_profit_pct","wt_max_profit_value",
]


def signals(df, params, computed, state=None):
    wt1   = computed["wt1"]
    wt2   = computed["wt2"]
    wt_d  = computed["wt_diff"]
    cross = computed["cross"]

    blank = {k: "" for k in _EMPTY_KEYS}
    blank["wt_triggered"] = False
    blank["_new_state"]   = state

    if len(df) < 6:
        return blank

    i_last = len(df) - 2

    # --- current bar values ---
    wt1_val     = wt1.iloc[i_last]
    wt2_val     = wt2.iloc[i_last]
    wt_diff_val = wt_d.iloc[i_last]
    wt_cross    = cross.iloc[i_last] if cross.iloc[i_last] else "No Cross"

    # --- find last actual cross ---
    valid_idx = None
    direction = ""
    for i in range(len(cross) - 2, 0, -1):
        c = cross.iloc[i]
        if c == "Bull Cross":
            valid_idx = i; direction = "buy";  break
        if c == "Bear Cross":
            valid_idx = i; direction = "sell"; break

    wt_trend = ("Buy Trend" if direction == "buy"
                else "Sell Trend" if direction == "sell"
                else "Inside Trend")

    # --- valid candle values ---
    v_open = v_high = v_low = v_close = ""
    v_dt = v_diff = v_wt = ""
    perfect = ""
    if valid_idx is not None:
        c = df.iloc[valid_idx]
        v_open  = _r(c["Open"])
        v_high  = _r(c["High"])
        v_low   = _r(c["Low"])
        v_close = _r(c["Close"])
        v_dt    = fmt_ist(df.index[valid_idx])
        v_diff  = _r(wt_d.iloc[valid_idx])
        v_wt    = _r(wt1.iloc[valid_idx] if direction == "buy"
                     else wt2.iloc[valid_idx])
        if direction == "buy":
            perfect = "Perfect" if c["Open"] == c["Low"] else "Imperfect"
        else:
            perfect = "Perfect" if c["Open"] == c["High"] else "Imperfect"

    # --- trigger level + direction ---
    trigger_level = None
    if valid_idx is not None:
        trigger_level = float(v_high if direction == "buy" else v_low)

    # --- breakout? ---
    trigger_idx = None
    if trigger_level is not None:
        for i in range(valid_idx + 1, len(df) - 1):
            if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                trigger_idx = i; break
            if direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                trigger_idx = i; break

    # --- LTP: prefer live price passed by server; fall back to HA close ---
    live = params.get("live_ltp")
    ltp = float(live) if live is not None else float(df["Close"].iloc[i_last])

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
        # only flag "New" if direction or valid-candle dt differs from stored
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

    # --- max profit tracking + booking status ---
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

    rsi_val = None; rsi_dir = ""
    if "rsi" in computed and valid_idx is not None:
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
        "wt_rsi":               rsi_val if rsi_val is not None else "",
        "wt_rsi_dir":           rsi_dir,
        "wt1":                  _r(wt1_val),
        "wt2":                  _r(wt2_val),
        "wt_diff":              _r(wt_diff_val),
        "wt_cross":             wt_cross,
        "wt_trend":             wt_trend,
        "wt_valid_open":        v_open,
        "wt_valid_high":        v_high,
        "wt_valid_low":         v_low,
        "wt_valid_close":       v_close,
        "wt_valid_dt":          v_dt,
        "wt_valid_diff":        v_diff,
        "wt_valid_wt_value":    v_wt,
        "wt_perfect":           perfect,
        "wt_trigger_status":    trigger_status,
        "wt_trigger_dt":        trigger_dt,
        "wt_action":            action,
        "wt_trigger_candle_date": t_date,
        "wt_trigger_candle_time": t_time,
        "wt_ltp":               _r(ltp),
        "wt_trigger_diff":      diff,
        "wt_diff_pct":          diff_pct,
        "wt_profit":            profit,
        "wt_booking_status":    booking,
        "wt_max_profit_pct":    max_pct,
        "wt_max_profit_value":  max_val,
        "wt_triggered":         trigger_idx is not None,
        "_new_state":           new_state,
    }


# ============================================================
#  SELF-TEST — python indicators/wavetrend.py
# ============================================================
if __name__ == "__main__":
    import os, sys, math
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import candles_to_df

    print("\n🧪 wavetrend.py self-test\n" + "─" * 78)

    rows = []
    price = 100.0
    for i in range(150):
        drift = math.sin(i / 10) * 1.4 + math.sin(i / 4) * 0.4
        o = price; c = o + drift
        h = max(o, c) + 0.4; l = min(o, c) - 0.4
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
    print(f"\nsecond run  wt_trigger_status : {sig2['wt_trigger_status'] or '(none)'}")
    print("\n✅ wavetrend.py works.\n")