import yfinance as yf
import pandas as pd
import gspread
import json
import os
import warnings
import sys
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# ================= SUPPRESS WARNINGS =================
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ================= USER SETTINGS =================
MULTI_TF_MODE = False
SELECTED_TF = "5m"
TIMEFRAMES = ["5m", "15m", "30m", "1h", "1d"]

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "srinput2"
OUTPUT_SHEET = "sroutput_multitf" if MULTI_TF_MODE else f"sroutput_{SELECTED_TF}"
JSON_FILE = f"sup&res_{SELECTED_TF}.json"
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# ================= GLOBAL =================
SKIPPED_SYMBOLS = set()

# ================= CONFIG =================
CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d",  "left": 15, "right": 15},
    "15m": {"interval": "15m", "period": "60d",  "left": 15, "right": 15},
    "30m": {"interval": "30m", "period": "60d",  "left": 15, "right": 15},
    "1h":  {"interval": "1h",  "period": "90d",  "left": 15, "right": 15},
    "1d":  {"interval": "1d",  "period": "10mo","left": 15, "right": 15},
}

# ================= CORE FUNCTIONS =================
def get_luxalgo_sr_and_prev_close(symbol, timeframe):
    cfg = CONFIG[timeframe]

    try:
        df = yf.download(
            symbol,
            interval=cfg["interval"],
            period=cfg["period"],
            progress=False,
            threads=False
        )
    except Exception:
        SKIPPED_SYMBOLS.add(symbol)
        return None, None, None

    if df is None or df.empty:
        SKIPPED_SYMBOLS.add(symbol)
        return None, None, None

    df = df.dropna()
    if len(df) < 3:
        SKIPPED_SYMBOLS.add(symbol)
        return None, None, None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    highs = df["High"].values
    lows = df["Low"].values

    last_r, last_s = None, None
    for i in range(cfg["left"], len(df) - cfg["right"]):
        if highs[i] == highs[i-cfg["left"]: i+cfg["right"]+1].max():
            last_r = round(highs[i], 2)
        if lows[i] == lows[i-cfg["left"]: i+cfg["right"]+1].min():
            last_s = round(lows[i], 2)

    prev_close = round(df["Close"].iloc[-2].item(), 2)
    return last_r, last_s, prev_close


def get_ltp(symbol):
    try:
        df = yf.download(
            symbol,
            interval="1m",
            period="1d",
            progress=False,
            threads=False
        )
        if df is None or df.empty:
            SKIPPED_SYMBOLS.add(symbol)
            return None
        return round(df["Close"].iloc[-1].item(), 2)
    except:
        SKIPPED_SYMBOLS.add(symbol)
        return None

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
    return [
        f"{s.strip().upper()}.NS" if not s.strip().upper().endswith(".NS")
        else s.strip().upper()
        for s in ws.col_values(1) if s.strip()
    ]


def write_output_batch(sheet, rows):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=20)

    ws.clear()

    header = [
        "Timestamp","Symbol","Timeframe","LTP",
        "Support","Resistance","Trend Position","Triggered Status"
    ]

    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")

    format_requests = []
    for i, row in enumerate(rows, start=2):
        color = None
        if row[6] == "Buy Trend":
            color = {"red":0.6,"green":1,"blue":0.6}
        elif row[6] == "Sell Trend":
            color = {"red":1,"green":0.6,"blue":0.6}
        elif row[6] == "Inside Trend":
            color = {"red":1,"green":1,"blue":0.6}

        if color:
            format_requests.append({
                "range": f"G{i}:G{i}",
                "format": {"backgroundColor": color}
            })

    if format_requests:
        ws.batch_format(format_requests)

# ================= MAIN =================
def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    tfs = TIMEFRAMES if MULTI_TF_MODE else [SELECTED_TF]

    output_rows = []
    json_data = []

    old_trend_map = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE,"r") as f:
            old_json = json.load(f)
            old_trend_map = {
                (i["symbol"], i["timeframe"]): i["trend"]
                for i in old_json
            }

    spinner = ["|","/","-","\\"]
    total = len(symbols)

    for idx, symbol in enumerate(symbols, start=1):

        ltp = get_ltp(symbol)
        if symbol in SKIPPED_SYMBOLS:
            print(f"\n⚠️ Skipped (no data / delisted): {symbol}")
            continue

        for tf in tfs:
            r, s, prev_close = get_luxalgo_sr_and_prev_close(symbol, tf)
            if symbol in SKIPPED_SYMBOLS:
                print(f"\n⚠️ Skipped (no data / delisted): {symbol}")
                break

            trend = ""
            if r and prev_close and prev_close > r:
                trend = "Buy Trend"
            elif s and prev_close and prev_close < s:
                trend = "Sell Trend"
            elif r and s and s < prev_close < r:
                trend = "Inside Trend"

            triggered_status = ""
            old_trend = old_trend_map.get((symbol, tf))
            if trend in ("Buy Trend", "Sell Trend") and old_trend != trend:
                triggered_status = "New trigger"

            row = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol, tf, ltp, s, r, trend, triggered_status
            ]

            output_rows.append(row)
            json_data.append({
                "symbol": symbol,
                "timeframe": tf,
                "ltp": ltp,
                "support": s,
                "resistance": r,
                "previous_close": prev_close,
                "trend": trend,
                "timestamp": row[0]
            })

        print(f"\rProcessing {spinner[idx % 4]} {idx}/{total} symbols", end="")
        sys.stdout.flush()

    if output_rows:
        write_output_batch(sheet, output_rows)

    with open(JSON_FILE,"w") as f:
        json.dump(json_data, f, indent=4)

    if SKIPPED_SYMBOLS:
        print("\n\n⚠️ Skipped symbols (no Yahoo data):")
        for s in sorted(SKIPPED_SYMBOLS):
            print(" -", s)

    print("\n\n✅ Sheet + JSON updated successfully!")

if __name__ == "__main__":
    main()
