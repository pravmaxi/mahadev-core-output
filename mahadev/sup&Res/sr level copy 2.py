import yfinance as yf
import pandas as pd
import gspread
import json
import os
import warnings
import sys
import pytz
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ================= USER SETTINGS =================
MULTI_TF_MODE = False
SELECTED_TF = "1h"

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = "sroutput_multitf" if MULTI_TF_MODE else f"sroutput_{SELECTED_TF}"
JSON_FILE = f"sup&res_{SELECTED_TF}.json"
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

SKIPPED_SYMBOLS = set()
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

# ================= CORE =================
def get_luxalgo_sr_and_prev_close(symbol, tf):
    cfg = CONFIG[tf]
    df = yf.download(symbol, interval=cfg["interval"], period=cfg["period"], progress=False)

    if df is None or df.empty:
        SKIPPED_SYMBOLS.add(symbol)
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


def find_breakout_datetime_backward(df, resistance, support, trend):
    closes = df["Close"].values
    times = df.index

    for i in range(len(df) - 1, 0, -1):
        prev_close = closes[i - 1]
        curr_close = closes[i]

        if trend == "Buy Trend" and resistance:
            if prev_close <= resistance and curr_close > resistance:
                ts = pd.to_datetime(times[i])
                ts = ts.tz_localize("UTC").tz_convert(IST) if ts.tzinfo is None else ts.tz_convert(IST)
                return ts.strftime("%Y-%m-%d %H:%M")

        if trend == "Sell Trend" and support:
            if prev_close >= support and curr_close < support:
                ts = pd.to_datetime(times[i])
                ts = ts.tz_localize("UTC").tz_convert(IST) if ts.tzinfo is None else ts.tz_convert(IST)
                return ts.strftime("%Y-%m-%d %H:%M")

    return ""


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
        "Breakout DateTime","Triggered Status"
    ]

    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")

    # ================= COLOR FORMATTING =================
    format_requests = []

    for i, row in enumerate(rows, start=2):
        trend = row[6]
        color = None

        if trend == "Buy Trend":
            color = {"red": 0.6, "green": 1.0, "blue": 0.6}
        elif trend == "Sell Trend":
            color = {"red": 1.0, "green": 0.6, "blue": 0.6}
        elif trend == "Inside Trend":
            color = {"red": 1.0, "green": 1.0, "blue": 0.6}

        if color:
            format_requests.append({
                "range": f"G{i}:G{i}",
                "format": {
                    "backgroundColor": color,
                    "textFormat": {"bold": True}
                }
            })

    if format_requests:
        ws.batch_format(format_requests)

# ================= MAIN =================
def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    total = len(symbols)

    old_trend = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            for i in json.load(f):
                old_trend[(i["symbol"], i["timeframe"])] = i["trend"]

    rows, json_out = [], []
    buy_cnt = sell_cnt = inside_cnt = new_triggers = 0
    spinner = ["|", "/", "-", "\\"]

    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        ltp = get_ltp(symbol)
        if symbol in SKIPPED_SYMBOLS:
            continue

        r, s, prev_close, df = get_luxalgo_sr_and_prev_close(symbol, SELECTED_TF)
        if df is None:
            continue

        trend = ""
        if r and prev_close > r:
            trend = "Buy Trend"
            buy_cnt += 1
        elif s and prev_close < s:
            trend = "Sell Trend"
            sell_cnt += 1
        elif r and s and s < prev_close < r:
            trend = "Inside Trend"
            inside_cnt += 1

        breakout_dt = ""
        if trend in ("Buy Trend", "Sell Trend"):
            breakout_dt = find_breakout_datetime_backward(df, r, s, trend)

        triggered = ""
        if trend in ("Buy Trend", "Sell Trend") and old_trend.get((symbol, SELECTED_TF)) != trend:
            triggered = "New trigger"
            new_triggers += 1

        rows.append([
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            symbol, SELECTED_TF, ltp, s, r, trend, breakout_dt, triggered
        ])

        json_out.append({
            "symbol": symbol,
            "timeframe": SELECTED_TF,
            "trend": trend
        })

    if rows:
        write_output_batch(sheet, rows)

    with open(JSON_FILE, "w") as f:
        json.dump(json_out, f, indent=4)

    print("\n\n📊 RUN SUMMARY")
    print("────────────────────────────")
    print(f"Total Symbols   : {total}")
    print(f"Buy Trend       : {buy_cnt}")
    print(f"Sell Trend      : {sell_cnt}")
    print(f"Inside Trend    : {inside_cnt}")
    print(f"New Triggers    : {new_triggers}")
    print(f"Skipped Symbols : {len(SKIPPED_SYMBOLS)}")
    print("────────────────────────────")
    print("✅ Sheet & JSON updated successfully")

if __name__ == "__main__":
    main()
