import streamlit as st
import yfinance as yf
import pandas as pd
import json
import os
import warnings
import pytz
from datetime import datetime

warnings.filterwarnings("ignore")

# ========== CONFIG ==========
IST = pytz.timezone("Asia/Kolkata")
CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d", "left": 15, "right": 15},
    "15m": {"interval": "15m", "period": "60d", "left": 15, "right": 15},
    "30m": {"interval": "30m", "period": "60d", "left": 15, "right": 15},
    "1h":  {"interval": "1h",  "period": "90d", "left": 15, "right": 15},
    "4h":  {"interval": "4h",  "period": "10mo","left": 15, "right": 15},
    "1d":  {"interval": "1d",  "period": "10mo","left": 15, "right": 15},
    "1w":  {"interval": "1w",  "period": "10mo","left": 15, "right": 15},
}

# ========== CORE FUNCTIONS (from your script) ==========
def get_luxalgo_sr_and_prev_close(symbol, tf):
    cfg = CONFIG[tf]
    df = yf.download(symbol, interval=cfg["interval"], period=cfg["period"], progress=False)
    if df is None or df.empty:
        return None, None, None, None
    df = df.dropna()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    highs, lows = df["High"].values, df["Low"].values
    last_r, last_s = None, None
    for i in range(cfg["left"], len(df) - cfg["right"]):
        if highs[i] == highs[i-cfg["left"]:i+cfg["right"]+1].max():
            last_r = round(highs[i], 2)
        if lows[i] == lows[i-cfg["left"]:i+cfg["right"]+1].min():
            last_s = round(lows[i], 2)
    prev_close = round(df["Close"].iloc[-2].item(), 2)
    return last_r, last_s, prev_close, df

def get_ltp(df):
    return round(df["Close"].iloc[-1].item(), 2)

def find_breakout_datetime_backward(df, resistance, support, trend):
    closes = df["Close"].values
    times = df.index
    for i in range(len(df)-1, 0, -1):
        prev_close = closes[i-1]
        curr_close = closes[i]
        if trend == "Buy Trend" and resistance and prev_close <= resistance < curr_close:
            ts = pd.to_datetime(times[i])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC").tz_convert(IST)
            else:
                ts = ts.tz_convert(IST)
            return ts.strftime("%Y-%m-%d %H:%M")
        if trend == "Sell Trend" and support and prev_close >= support > curr_close:
            ts = pd.to_datetime(times[i])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC").tz_convert(IST)
            else:
                ts = ts.tz_convert(IST)
            return ts.strftime("%Y-%m-%d %H:%M")
    return ""

# ========== STREAMLIT UI ==========
st.set_page_config(page_title="Stock SR Scanner", layout="wide")
st.title("📈 Support / Resistance Scanner")

# Sidebar controls
st.sidebar.header("⚙️ Settings")
timeframe = st.sidebar.selectbox("Timeframe", list(CONFIG.keys()), index=5)  # default 1d
multi_tf = st.sidebar.checkbox("Multi‑timeframe mode (not fully implemented in this example)")
run_btn = st.sidebar.button("🔄 Run Analysis")

# Manual symbol input (instead of Google Sheets – you can restore Sheets later)
symbols_input = st.sidebar.text_area("Stock symbols (one per line, with or without .NS)", 
                                      "RELIANCE\nTCS\nHDFCBANK\nINFY")
run_btn = st.sidebar.button("🔄 Run Analysis")

if run_btn:
    symbols = [s.strip().upper() + ("" if s.endswith(".NS") else ".NS") 
               for s in symbols_input.splitlines() if s.strip()]
    if not symbols:
        st.warning("Please enter at least one symbol.")
        st.stop()

    progress_bar = st.progress(0)
    status_text = st.empty()
    results = []

    for i, sym in enumerate(symbols):
        status_text.text(f"Processing {i+1}/{len(symbols)}: {sym}")
        r, s, prev_close, df = get_luxalgo_sr_and_prev_close(sym, timeframe)
        if df is None:
            continue
        ltp = get_ltp(df)
        if ltp is None:
            continue

        # Determine trend
        if r and prev_close > r:
            trend = "Buy Trend"
        elif s and prev_close < s:
            trend = "Sell Trend"
        elif r and s and s < prev_close < r:
            trend = "Inside Trend"
        else:
            trend = "No clear trend"

        breakout_dt = ""
        if trend in ("Buy Trend", "Sell Trend"):
            breakout_dt = find_breakout_datetime_backward(df, r, s, trend)

        results.append({
            "Symbol": sym,
            "LTP": ltp,
            "Support": s,
            "Resistance": r,
            "Trend": trend,
            "Breakout DateTime": breakout_dt
        })
        progress_bar.progress((i+1)/len(symbols))

    status_text.text("Analysis complete!")
    if results:
        df_res = pd.DataFrame(results)
        st.subheader(f"📊 Results for {timeframe}")
        st.dataframe(df_res, use_container_width=True)

        # Summary stats
        buy_cnt = sum(1 for x in results if x["Trend"] == "Buy Trend")
        sell_cnt = sum(1 for x in results if x["Trend"] == "Sell Trend")
        inside_cnt = sum(1 for x in results if x["Trend"] == "Inside Trend")
        col1, col2, col3 = st.columns(3)
        col1.metric("Buy Trend", buy_cnt)
        col2.metric("Sell Trend", sell_cnt)
        col3.metric("Inside Trend", inside_cnt)

        # Optional: Save JSON (cannot write to local disk on Streamlit Cloud – use st.session_state or cloud storage)
        # Instead offer download button
        csv = df_res.to_csv(index=False)
        st.download_button("📥 Download as CSV", csv, f"sr_{timeframe}.csv", "text/csv")
    else:
        st.error("No valid data retrieved. Check symbol format or network.")