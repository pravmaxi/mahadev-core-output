"""
T3 (Tillson) indicator — math and trigger logic.

Math copied verbatim from in_t3_wt_ce.py:
    compute_t3, get_t3_colour_and_series, condition_met

Trigger logic mirrored from in_t3_wt_ce.py:
    colour-change → find valid candle → set trigger level → wait for breakout
"""

try:
    # works when imported as part of the package:  from indicators import t3
    from .common import round_or_blank as _r, fmt_ist
except ImportError:
    # works when run directly:  python indicators/t3.py
    import os, sys
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from common import round_or_blank as _r, fmt_ist


# ============================================================
#  META — describes this indicator to the UI and the runner
# ============================================================
META = {
    "key": "t3",
    "label": "T3 (Tillson)",
    "defaults": {
        "length":  8,
        "vfactor": 0.7,
    },
    "columns": [
        {"key": "t3_trend",  "label": "T3 Trend"},
        {"key": "t3_value",  "label": "T3 Value"},
        {"key": "t3_action", "label": "T3 Action"},
    ],
}


# ============================================================
#  MATH — pure functions, no side effects, no I/O
# ============================================================

def _compute_t3_series(series, length, vfactor):
    e1 = series.ewm(span=length, adjust=False).mean()
    e2 = e1.ewm(span=length, adjust=False).mean()
    e3 = e2.ewm(span=length, adjust=False).mean()
    e4 = e3.ewm(span=length, adjust=False).mean()
    e5 = e4.ewm(span=length, adjust=False).mean()
    e6 = e5.ewm(span=length, adjust=False).mean()
    c1 = -vfactor ** 3
    c2 =  3 * (vfactor ** 2) + 3 * (vfactor ** 3)
    c3 = -6 * (vfactor ** 2) - 3 * vfactor - 3 * (vfactor ** 3)
    c4 =  1 + 3 * vfactor + (vfactor ** 3) + 3 * (vfactor ** 2)
    return c1 * e6 + c2 * e5 + c3 * e4 + c4 * e3


def _colour_series(t3_series):
    """
    Per-bar colour: green / red / yellow, based on whether T3 went up/down.
    Returns a plain list of strings (index-aligned with t3_series).
    """
    colours = ["yellow"]                       # first bar is always yellow
    for i in range(1, len(t3_series)):
        a, b = t3_series.iloc[i], t3_series.iloc[i - 1]
        if a > b:
            colours.append("green")
        elif a < b:
            colours.append("red")
        else:
            colours.append("yellow")
    return colours


def _condition_met(row, colour):
    """Same as condition_met() in in_t3_wt_ce.py."""
    if colour == "red":
        return (row["Open"] == row["High"]) and (row["Low"] < row["High"])
    if colour == "green":
        return (row["Open"] == row["Low"]) and (row["High"] > row["Low"])
    return False


# ============================================================
#  compute() — called once per symbol by the runner
# ============================================================
def compute(df, params):
    """
    Returns a dict of index-aligned objects:
        {
          "t3":     pandas.Series of T3 values,
          "colour": list of 'green' / 'red' / 'yellow'
        }
    """
    length  = int(params.get("length",  META["defaults"]["length"]))
    vfactor = float(params.get("vfactor", META["defaults"]["vfactor"]))

    typical = (df["High"] + df["Low"] + 2 * df["Close"]) / 4
    t3      = _compute_t3_series(typical, length, vfactor)
    colours = _colour_series(t3)

    return {"t3": t3, "colour": colours}


# ============================================================
#  signals() — called once per symbol by the runner
# ============================================================
def signals(df, params, computed):
    """
    Returns the row values for T3:
        {
          "t3_trend":        'Buy Trend' / 'Sell Trend' / 'Inside Trend',
          "t3_value":        latest T3 value (last complete bar),
          "t3_action":       'Buy Market Order' / 'Sell Market Order' / '' / …
          "t3_triggered":    True if this scan detected a breakout,
          "t3_details":      dict with valid-candle + trigger-level info (for debug)
        }
    """
    t3_series = computed["t3"]
    colours   = computed["colour"]

    blank = {
        "t3_trend": "", "t3_value": "", "t3_action": "",
        "t3_triggered": False, "t3_details": {},
    }

    if len(df) < 4 or len(t3_series) < 4:
        return blank

    # Use the last COMPLETE bar (index -2), same convention as in_t3_wt_ce.py
    i_last = len(df) - 2
    current_colour = colours[i_last]
    current_t3     = t3_series.iloc[i_last]

    trend_label = ("Sell Trend" if current_colour == "red"
                   else "Buy Trend" if current_colour == "green"
                   else "Inside Trend")

    # ---- find last colour change ----
    change_index = None
    for i in range(i_last, 0, -1):
        if colours[i] != colours[i - 1]:
            change_index = i
            break

    # ---- find valid candle inside the current colour regime ----
    valid_idx = None
    if change_index is not None and current_colour in ("green", "red"):
        for i in range(change_index, len(df) - 1):
            if _condition_met(df.iloc[i], current_colour):
                valid_idx = i
                break

    # ---- set trigger level + direction ----
    trigger_level = None
    direction     = ""
    if valid_idx is not None:
        candle = df.iloc[valid_idx]
        if current_colour == "green":
            trigger_level = float(candle["High"])
            direction     = "buy"
        else:
            trigger_level = float(candle["Low"])
            direction     = "sell"

    # ---- look for breakout after valid candle ----
    trigger_idx = None
    if trigger_level is not None:
        for i in range(valid_idx + 1, len(df) - 1):
            if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                trigger_idx = i
                break
            if direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                trigger_idx = i
                break

    # ---- decide the action string ----
    action = ""
    if trigger_level is not None:
        if trigger_idx is not None:
            action = "Buy Market Order" if direction == "buy" else "Sell Market Order"
        else:
            last_close = float(df["Close"].iloc[i_last])
            if direction == "buy":
                action = ("Buy Market Order" if last_close > trigger_level
                          else f"Buy Limit Order (BO close {trigger_level})")
            else:
                action = ("Sell Market Order" if last_close < trigger_level
                          else f"Sell Limit Order (BO close {trigger_level})")

    # ---- package details for debugging / sheet writing ----
    details = {}
    if valid_idx is not None:
        vc = df.iloc[valid_idx]
        details["valid_dt"]    = fmt_ist(df.index[valid_idx])
        details["valid_open"]  = _r(vc["Open"])
        details["valid_high"]  = _r(vc["High"])
        details["valid_low"]   = _r(vc["Low"])
        details["valid_close"] = _r(vc["Close"])
        details["valid_t3"]    = _r(t3_series.iloc[valid_idx])
        details["trigger_level"] = _r(trigger_level)
    if trigger_idx is not None:
        details["trigger_dt"] = fmt_ist(df.index[trigger_idx])

    return {
        "t3_trend":     trend_label,
        "t3_value":     _r(current_t3),
        "t3_action":    action,
        "t3_triggered": trigger_idx is not None,
        "t3_details":   details,
    }


# ============================================================
#  SELF-TEST
#  Run:   python indicators/t3.py
#  You should see a small table of T3 values and colours.
# ============================================================

if __name__ == "__main__":
    import os, sys, math
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from indicators.common import candles_to_df

    print("\n🧪 t3.py self-test\n" + "─" * 72)

    # Build 60 fake hourly candles with a sine-wave drift so T3 turns
    # green then red — proves the colour logic works end to end.
    rows = []
    price = 100.0
    for i in range(60):
        drift = math.sin(i / 8) * 1.2
        o = price
        c = o + drift
        h = max(o, c) + 0.4
        l = min(o, c) - 0.4
        hour = i % 24                     # ← valid 0..23
        day  = 1 + (i // 24)              # ← rolls over to next day
        ts   = f"2025-01-{day:02d}T{hour:02d}:00:00"
        rows.append({"t": ts, "o": o, "h": h, "l": l, "c": c, "v": 1000 + i})
        price = c

    df = candles_to_df(rows)
    print(f"input: {len(df)} candles\n")

    # --- compute ---
    comp = compute(df, META["defaults"])
    t3    = comp["t3"]
    cols  = comp["colour"]

    print(f"{'#':>3}  {'Close':>9}  {'T3':>10}  {'Colour':>8}")
    for i in range(0, 20):
        print(f"{i:>3}  {df['Close'].iloc[i]:>9.3f}  "
              f"{t3.iloc[i]:>10.4f}  {cols[i]:>8}")

    print("   …")
    for i in range(50, 60):
        print(f"{i:>3}  {df['Close'].iloc[i]:>9.3f}  "
              f"{t3.iloc[i]:>10.4f}  {cols[i]:>8}")

    # --- signals ---
    sig = signals(df, META["defaults"], comp)
    print("\nsignals():")
    print(f"  trend         : {sig['t3_trend']}")
    print(f"  t3_value      : {sig['t3_value']}")
    print(f"  action        : {sig['t3_action'] or '(none)'}")
    print(f"  triggered now : {sig['t3_triggered']}")
    if sig["t3_details"]:
        print("  details:")
        for k, v in sig["t3_details"].items():
            print(f"    {k:>14} : {v}")

    print("\n✅ t3.py works.\n")