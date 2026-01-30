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

warnings.filterwarnings("ignore")

# ================= USER SETTINGS =================
MULTI_TF_MODE = False
SELECTED_TF = "1d"

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "srinput2"
OUTPUT_SHEET = f"sr3output_{SELECTED_TF}"
JSON_FILE = f"sup_rest_{SELECTED_TF}.json"

CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

IST = pytz.timezone("Asia/Kolkata")
SKIPPED_SYMBOLS = set()

CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "1h":  {"interval": "1h",  "period": "90d"},
    "1d":  {"interval": "1d",  "period": "10mo"},
}

# ================= CORE =================
def get_sr_levels(symbol, tf):
    cfg = CONFIG[tf]
    df = yf.download(symbol, interval=cfg["interval"], period=cfg["period"], progress=False)

    if df is None or df.empty:
        SKIPPED_SYMBOLS.add(symbol)
        return None, None, None

    df.dropna(inplace=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    highs = df["High"].values
    lows = df["Low"].values

    resistance = round(highs[-20:].max(), 2)
    support = round(lows[-20:].min(), 2)

    return resistance, support, df


def find_trend_and_breakout(df, resistance, support):
    base_high = None
    base_low = None

    for i in range(len(df)):
        close = df["Close"].iloc[i]
        high = df["High"].iloc[i]
        low = df["Low"].iloc[i]
        ts = df.index[i]

        # ================= BUY LOGIC =================
        if resistance and close > resistance:
            base_high = high

        if base_high and high > base_high:
            ts = pd.to_datetime(ts)
            ts = ts.tz_localize("UTC").tz_convert(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            return "Buy Trend", ts.strftime("%Y-%m-%d %H:%M")

        # ================= SELL LOGIC =================
        if support and close < support:
            base_low = low

        if base_low and low < base_low:
            ts = pd.to_datetime(ts)
            ts = ts.tz_localize("UTC").tz_convert(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            return "Sell Trend", ts.strftime("%Y-%m-%d %H:%M")

    return "Inside Trend", ""


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


def write_output(sheet, rows):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=20)

    ws.clear()
    header = [
        "Timestamp", "Symbol", "Timeframe", "LTP",
        "Support", "Resistance",
        "Trend Position", "Breakout DateTime", "Triggered Status"
    ]
    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")


def apply_colors(sheet):
    ws = sheet.worksheet(OUTPUT_SHEET)

    rules = [
        ("Buy Trend",  {"red": 0.6, "green": 0.9, "blue": 0.6}),
        ("Sell Trend", {"red": 0.95, "green": 0.6, "blue": 0.6}),
        ("Inside Trend", {"red": 1, "green": 0.9, "blue": 0.6}),
    ]

    requests = []
    for text, color in rules:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 6,
                        "endColumnIndex": 7
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": text}]
                        },
                        "format": {"backgroundColor": color}
                    }
                },
                "index": 0
            }
        })
    sheet.batch_update({"requests": requests})

# ================= MAIN =================
def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)

    old_trend = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            for i in json.load(f):
                old_trend[(i["symbol"], i["timeframe"])] = i["trend"]

    rows = []
    json_out = []

    for symbol in symbols:
        ltp = get_ltp(symbol)
        if symbol in SKIPPED_SYMBOLS:
            continue

        r, s, df = get_sr_levels(symbol, SELECTED_TF)
        if df is None:
            continue

        trend, breakout_dt = find_trend_and_breakout(df, r, s)

        triggered = ""
        if old_trend.get((symbol, SELECTED_TF)) != trend and trend != "Inside Trend":
            triggered = "New trigger"

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
        write_output(sheet, rows)
        apply_colors(sheet)

    with open(JSON_FILE, "w") as f:
        json.dump(json_out, f, indent=4)

    print("✅ Trend calculation completed successfully")

if __name__ == "__main__":
    main()
