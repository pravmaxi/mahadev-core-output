import yfinance as yf
import pandas as pd
import gspread
import warnings
import sys
import pytz
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ================= USER SETTINGS =================
SELECTED_TF = "15m"
SYMBOL_TO_DEBUG = "ASHOKLEY"   # change to any symbol
BACKTEST_START_DATE = "2026-05-29"  # inclusive start date (YYYY-MM-DD)

# Google Sheet settings
SHEET_NAME = "Stock Price Scraper"
OUTPUT_SHEET = "S_R_Debug"
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

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

def compute_sr_for_full_df(df, left, right):
    """
    Compute support and resistance for each candle using only past data.
    Returns two lists: supports and resistances (same length as df).
    """
    n = len(df)
    supports = [None] * n
    resistances = [None] * n

    for i in range(n):
        sub_df = df.iloc[:i+1]
        m = len(sub_df)
        if m < 1:
            continue
        if m < left + 1:
            supports[i] = round(sub_df["Low"].min(), 2)
            resistances[i] = round(sub_df["High"].max(), 2)
            continue

        last_r = None
        last_s = None
        for j in range(m):
            high_j = sub_df["High"].iloc[j]
            low_j = sub_df["Low"].iloc[j]
            left_start = max(0, j - left)
            right_end = min(m - 1, j + right)
            if high_j == sub_df["High"].iloc[left_start:right_end+1].max():
                last_r = round(high_j, 2)
            if low_j == sub_df["Low"].iloc[left_start:right_end+1].min():
                last_s = round(low_j, 2)
        supports[i] = last_s
        resistances[i] = last_r

    return supports, resistances

# ================= GOOGLE SHEET FUNCTIONS =================

def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)

def write_debug_data(worksheet, df, supports, resistances, start_date):
    start_ts = pd.Timestamp(start_date).tz_localize('UTC').tz_convert(IST)
    # Filter rows from start_date onward
    mask = df.index >= start_ts
    filtered_indices = [i for i in range(len(df)) if mask.iloc[i]]
    if not filtered_indices:
        # No data after start date; use all data
        filtered_indices = list(range(len(df)))
        print(f"Warning: Start date {start_date} is beyond available data. Showing all data.")

    header = ["Date", "Open", "High", "Low", "Close", "Support", "Resistance"]
    rows = []
    for orig_idx in filtered_indices:
        row = df.iloc[orig_idx]
        dt = row.name.tz_convert(IST).strftime("%Y-%m-%d %H:%M")
        rows.append([
            dt,
            round(row["Open"], 2),
            round(row["High"], 2),
            round(row["Low"], 2),
            round(row["Close"], 2),
            supports[orig_idx] if supports[orig_idx] is not None else "",
            resistances[orig_idx] if resistances[orig_idx] is not None else ""
        ])

    worksheet.clear()
    worksheet.update("A1", [header] + rows, value_input_option="USER_ENTERED")

def main():
    sheet = connect_gsheet()
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=5000, cols=20)

    cfg = CONFIG[SELECTED_TF]
    left = cfg["left"]
    right = cfg["right"]
    period = cfg["period"]

    symbol = f"{SYMBOL_TO_DEBUG}.NS"
    print(f"Debug for {symbol} on {SELECTED_TF}")
    print(f"Downloading data with period={period}...")

    df = yf.download(symbol, interval=cfg["interval"], period=period, progress=False)
    if df is None or df.empty:
        print("No data found. Please check symbol and period.")
        return

    df = df.dropna()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    print(f"Total candles: {len(df)}")
    print("Computing support/resistance for all candles...")

    supports, resistances = compute_sr_for_full_df(df, left, right)

    print(f"Writing data from {BACKTEST_START_DATE} onwards...")
    write_debug_data(ws, df, supports, resistances, BACKTEST_START_DATE)
    print(f"✅ Debug data written to sheet '{OUTPUT_SHEET}'.")

if __name__ == "__main__":
    main()