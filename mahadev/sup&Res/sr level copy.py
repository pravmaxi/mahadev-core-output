import yfinance as yf
import pandas as pd
import gspread
import json
import os
import warnings
import pytz
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ================= USER SETTINGS =================
MULTI_TF_MODE = False
SELECTED_TF = "5m"

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = f"srtoutput_{SELECTED_TF}"
JSON_FILE = f"sup&res_{SELECTED_TF}.json"
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

IST = pytz.timezone("Asia/Kolkata")
SKIPPED_SYMBOLS = set()

CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d", "left": 15, "right": 15},
    "15m": {"interval": "15m", "period": "60d", "left": 15, "right": 15},
    "30m": {"interval": "30m", "period": "60d", "left": 15, "right": 15},
    "1h":  {"interval": "1h",  "period": "90d", "left": 15, "right": 15},
    "1d":  {"interval": "1d",  "period": "10mo","left": 15, "right": 15},
}

# ================= CORE =================
def get_luxalgo_sr_and_prev_close(symbol):
    cfg = CONFIG[SELECTED_TF]

    df = yf.download(
        symbol,
        interval=cfg["interval"],
        period=cfg["period"],
        progress=False
    )

    if df is None or df.empty:
        SKIPPED_SYMBOLS.add(symbol)
        return None, None, None, None

    df = df.dropna()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    highs, lows = df["High"].values, df["Low"].values
    last_r, last_s = None, None

    for i in range(cfg["left"], len(df) - cfg["right"]):
        if highs[i] == highs[i-cfg["left"]: i+cfg["right"]+1].max():
            last_r = round(float(highs[i]), 2)
        if lows[i] == lows[i-cfg["left"]: i+cfg["right"]+1].min():
            last_s = round(float(lows[i]), 2)

    prev_close = round(float(df["Close"].iloc[-2]), 2)
    return last_r, last_s, prev_close, df


def find_breakout_datetime_backward(df, resistance, support, trend):
    """
    BACKWARD LOGIC:
    Start from latest candle and move backwards.
    First candle where breakout happened is returned.
    """

    closes = df["Close"].values
    times = df.index

    for i in range(len(df) - 1, 0, -1):
        prev_close = float(closes[i - 1])
        curr_close = float(closes[i])

        candle_time = pd.to_datetime(times[i])
        if candle_time.tzinfo is None:
            candle_time = candle_time.tz_localize("UTC").tz_convert(IST)
        else:
            candle_time = candle_time.tz_convert(IST)

        if trend == "Buy Trend" and resistance is not None:
            if prev_close <= resistance and curr_close > resistance:
                return candle_time.strftime("%Y-%m-%d %H:%M")

        if trend == "Sell Trend" and support is not None:
            if prev_close >= support and curr_close < support:
                return candle_time.strftime("%Y-%m-%d %H:%M")

    return ""


def get_trend_strength(prev_close, support, resistance):
    if not support or not resistance:
        return ""

    range_val = resistance - support
    if range_val <= 0:
        return ""

    pos = (prev_close - support) / range_val * 100

    if pos > 80:
        return "Very Strong"
    elif pos > 60:
        return "Strong"
    elif pos > 40:
        return "Moderate"
    else:
        return "Weak"


def get_ltp(symbol):
    df = yf.download(symbol, interval="1m", period="1d", progress=False)
    if df is None or df.empty:
        SKIPPED_SYMBOLS.add(symbol)
        return None

    return round(df["Close"].iloc[-1].item(), 2)


# ================= GOOGLE SHEET =================
def connect_gsheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)


def read_symbols(sheet):
    ws = sheet.worksheet(INPUT_SHEET)
    return [f"{s.strip().upper()}.NS" for s in ws.col_values(1) if s.strip()]


def write_output_batch(sheet, rows):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=20)

    ws.clear()

    header = [
        "Timestamp","Symbol","Timeframe","LTP",
        "Support","Resistance","Trend Position",
        "Trend Strength","Breakout DateTime","Triggered Status"
    ]

    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")

    formats = []
    for i, row in enumerate(rows, start=2):
        color = None
        if row[6] == "Buy Trend":
            color = {"red":0.6,"green":1,"blue":0.6}
        elif row[6] == "Sell Trend":
            color = {"red":1,"green":0.6,"blue":0.6}
        elif row[6] == "Inside Trend":
            color = {"red":1,"green":1,"blue":0.6}

        if color:
            formats.append({
                "range": f"G{i}:G{i}",
                "format": {"backgroundColor": color}
            })

    if formats:
        ws.batch_format(formats)

# ================= MAIN =================
def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)

    old_trend_map = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            for i in json.load(f):
                old_trend_map[(i["symbol"], i["timeframe"])] = i["trend"]

    rows, json_data = [], []

    for symbol in symbols:
        ltp = get_ltp(symbol)
        if symbol in SKIPPED_SYMBOLS:
            continue

        r, s, prev_close, df = get_luxalgo_sr_and_prev_close(symbol)
        if df is None:
            continue

        trend = ""
        if r and prev_close > r:
            trend = "Buy Trend"
        elif s and prev_close < s:
            trend = "Sell Trend"
        elif r and s and s < prev_close < r:
            trend = "Inside Trend"

        breakout_dt = ""
        if trend in ("Buy Trend", "Sell Trend"):
            breakout_dt = find_breakout_datetime_backward(df, r, s, trend)

        trend_strength = get_trend_strength(prev_close, s, r)

        triggered_status = ""
        if trend in ("Buy Trend", "Sell Trend") and old_trend_map.get((symbol, SELECTED_TF)) != trend:
            triggered_status = "New Trigger"

        rows.append([
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            symbol, SELECTED_TF, ltp, s, r,
            trend, trend_strength, breakout_dt, triggered_status
        ])

        json_data.append({
            "symbol": symbol,
            "timeframe": SELECTED_TF,
            "trend": trend
        })

    if rows:
        write_output_batch(sheet, rows)

    with open(JSON_FILE, "w") as f:
        json.dump(json_data, f, indent=4)

    print("✅ Breakout + Trend logic completed (IST enforced)")

if __name__ == "__main__":
    main()
