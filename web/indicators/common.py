"""
Shared helpers used by every indicator.

Nothing here knows about T3 or WaveTrend or Chandelier — it's the small
stuff every indicator needs: date conversion, candle-shape conversion,
the pandas-DataFrame factory, Heikin Ashi, F&O filter, and a couple of
number formatters.

If you find yourself wanting to add math that only one indicator uses,
put it in that indicator's file, not here.
"""
import sys
from pathlib import Path
import pandas as pd

# make `settings.py` importable when this file is run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import IST, FNO_INTRADAY_TFS, FNO_CUTOFF_HOUR, FNO_CUTOFF_MIN


# ---- DataFrame factory ---------------------------------------------------

def candles_to_df(rows):
    """
    Convert our [{t,o,h,l,c,v}, …] list into a pandas DataFrame
    with a DatetimeIndex and columns Open/High/Low/Close/Volume.
    """
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"])
    df = df.set_index("t")
    df = df.rename(columns={"o": "Open", "h": "High",
                            "l": "Low",  "c": "Close", "v": "Volume"})
    return df


def heikin_ashi(df):
    """
    Heikin-Ashi candle transformation.
    Copied verbatim from in_t3_wt_ce.py — same math, same seeding.
    """
    ha = pd.DataFrame(index=df.index)
    ha["Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4.0
    ha["Open"] = (df["Open"].iloc[0] + df["Close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha.loc[ha.index[i], "Open"] = (
            ha.loc[ha.index[i - 1], "Open"] + ha.loc[ha.index[i - 1], "Close"]
        ) / 2.0
    ha["High"] = pd.concat([df["High"], ha["Open"], ha["Close"]], axis=1).max(axis=1)
    ha["Low"]  = pd.concat([df["Low"],  ha["Open"], ha["Close"]], axis=1).min(axis=1)
    return ha[["Open", "High", "Low", "Close"]]


# ---- F&O session filter --------------------------------------------------

def apply_fno_filter(df, timeframe):
    """
    Drop candles at/after 15:15 IST for intraday timeframes.
    Same rule as filter_fno_market_candles() in in_t3_wt_ce.py.
    Returns a new DataFrame (original is untouched).
    """
    if df is None or df.empty:
        return df
    if timeframe not in FNO_INTRADAY_TFS:
        return df

    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    mask = ~((df.index.hour == FNO_CUTOFF_HOUR) &
             (df.index.minute >= FNO_CUTOFF_MIN))
    return df[mask]


# ---- Formatting helpers --------------------------------------------------

def fmt_ist(ts):
    """Format a timestamp as 'YYYY-MM-DD HH:MM' in IST."""
    dt = pd.to_datetime(ts)
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC").tz_convert(IST)
    else:
        dt = dt.tz_convert(IST)
    return dt.strftime("%Y-%m-%d %H:%M")


def round_or_blank(x, digits=2):
    """Round a number, but return '' for None/NaN/unusable values."""
    try:
        v = float(x)
        if pd.isna(v):
            return ""
        return round(v, digits)
    except (TypeError, ValueError):
        return ""


# ---- Self-test -----------------------------------------------------------
# Run:   python indicators/common.py
# Should print a small table of fake OHLC and their HA values.

if __name__ == "__main__":
    print("\n🧪 common.py self-test\n" + "─" * 60)

    # 20 fake candles
    rows = []
    price = 100.0
    for i in range(20):
        o = price
        c = price + (0.5 if i % 2 == 0 else -0.4)
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        rows.append({"t": f"2025-01-01T{i:02d}:00:00", "o": o, "h": h,
                     "l": l, "c": c, "v": 1000 + i})
        price = c

    df = candles_to_df(rows)
    ha = heikin_ashi(df)

    print(f"candles_to_df → {len(df)} rows, columns: {list(df.columns)}")
    print(f"heikin_ashi   → {len(ha)} rows\n")
    print(f"{'#':>3}  {'Close':>8}  {'HA Open':>9}  {'HA High':>9}  {'HA Low':>9}  {'HA Close':>9}")
    for i in range(6):
        print(f"{i:>3}  {df['Close'].iloc[i]:>8.3f}  "
              f"{ha['Open'].iloc[i]:>9.3f}  {ha['High'].iloc[i]:>9.3f}  "
              f"{ha['Low'].iloc[i]:>9.3f}  {ha['Close'].iloc[i]:>9.3f}")

    print(f"\nfmt_ist       → {fmt_ist(df.index[0])}")
    print(f"round_or_blank→ {round_or_blank(3.14159)}, "
          f"{round_or_blank(float('nan'))!r}, {round_or_blank(None)!r}")
    print("\n✅ common.py works.\n")