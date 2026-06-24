# =========================================================
# FIRST CANDLE BREAKOUT STRATEGY (WITH BACKTEST MODE)
# =========================================================

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import math
import time

# =========================================================
# USER SETTINGS (EDIT THESE)
# =========================================================

TIMEFRAME = "1h"              # Options: 15m, 30m, 1h
AUTO_DATE = False              # True = use today; False = use MANUAL_DATE
MANUAL_DATE = "2025-3-12"     # Used only if AUTO_DATE = False (YYYY-MM-DD)
USE_HIGH_LOW = True            # True = breakout detection uses High/Low; False = uses Open/Close

BACKTEST_MODE = False           # True = multi-day backtest; False = single date only

# Intraday / Delivery toggle (placeholder – currently does nothing)
INTRADAY_MODE = True           # True = intraday; False = delivery (not implemented yet)

# Google Sheet identifiers
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input5"
OUTPUT_SHEET = "FCB_OUTPUT_BACKTEST"   # Different name to avoid confusion
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# =========================================================
# TIMEFRAME CONFIG
# =========================================================
TIMEFRAME_CONFIG = {
    "15m": {"interval": "15m", "default_period": "5d", "max_days": 60},
    "30m": {"interval": "30m", "default_period": "10d", "max_days": 60},
    "1h":  {"interval": "1h",  "default_period": "30d", "max_days": 60},
}
config = TIMEFRAME_CONFIG[TIMEFRAME]
interval = config["interval"]
default_period = config["default_period"]
MAX_DATA_DAYS = config["max_days"]   # Yahoo intraday limit

# =========================================================
# GOOGLE SHEET SETUP
# =========================================================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME)
input_ws = sheet.worksheet(INPUT_SHEET)

try:
    output_ws = sheet.worksheet(OUTPUT_SHEET)
    output_ws.clear()
except:
    output_ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=5000, cols=100)  # extra columns for backtest

# =========================================================
# READ SYMBOLS
# =========================================================
stocks = input_ws.col_values(1)[1:]
stocks = [s.strip() for s in stocks if s.strip()]
print(f"Total Symbols : {len(stocks)}")

# =========================================================
# TARGET START DATE
# =========================================================
if AUTO_DATE:
    target_date = datetime.now().date()
    print(f"Auto date = True → using today: {target_date}")
else:
    try:
        target_date = datetime.strptime(MANUAL_DATE, "%Y-%m-%d").date()
        print(f"Auto date = False → using manual date: {target_date}")
    except ValueError:
        print("ERROR: MANUAL_DATE must be in YYYY-MM-DD format.")
        exit(1)

today = datetime.now().date()

# =========================================================
# ROUNDING HELPERS (0.05 increments)
# =========================================================
def round_up_to_05(x):
    return math.ceil(x * 20) / 20

def round_down_to_05(x):
    return math.floor(x * 20) / 20

# =========================================================
# FUNCTION: Compute PnL for a specific date from a dataframe
# =========================================================
def compute_pnl_for_date(df, date, use_high_low):
    """Given a dataframe (full data for a symbol) and a target date,
       returns (first_candle_dict, breakout_str, breakout_time, pnl, dated_last_close)
       where pnl is PnL Value (Dated) for that date using first candle high/low."""
    df_target = df[df.index.date == date]
    if df_target.empty:
        return None, None, None, None, None

    # First candle of the day
    first = df_target.iloc[0]
    first_open = float(first["Open"])
    first_high = float(first["High"])
    first_low = float(first["Low"])
    first_close = float(first["Close"])

    # Scan remaining candles for breakout
    remaining = df_target.iloc[1:]
    breakout = ""
    breakout_time = ""
    breakout_type = ""  # "HIGH" or "LOW"

    for idx, row in remaining.iterrows():
        if use_high_low:
            candle_high = float(row["High"])
            candle_low = float(row["Low"])
            high_condition = candle_high > first_high
            low_condition = candle_low < first_low
        else:
            candle_open = float(row["Open"])
            candle_close = float(row["Close"])
            high_condition = candle_close > first_close
            low_condition = candle_open < first_open

        if high_condition:
            breakout = "HIGH BREAKOUT"
            breakout_time = idx.strftime("%H:%M")
            breakout_type = "HIGH"
            break
        elif low_condition:
            breakout = "LOW BREAKOUT"
            breakout_time = idx.strftime("%H:%M")
            breakout_type = "LOW"
            break

    dated_last_close = float(df_target.iloc[-1]["Close"])

    # PnL using FIRST candle levels (rounded)
    pnl = None
    if breakout_type == "HIGH":
        rounded = round_up_to_05(first_high)
        pnl = round(dated_last_close - rounded, 2)
    elif breakout_type == "LOW":
        rounded = round_down_to_05(first_low)
        pnl = round(rounded - dated_last_close, 2)

    first_candle_info = {
        "open": round(first_open, 2),
        "high": round(first_high, 2),
        "low": round(first_low, 2),
        "close": round(first_close, 2),
        "dated_last_close": round(dated_last_close, 2),
        "breakout": breakout,
        "breakout_time": breakout_time
    }
    return first_candle_info, breakout, breakout_time, pnl, dated_last_close

# =========================================================
# MAIN PROCESSING
# =========================================================
if not BACKTEST_MODE:
    # =====================================================
    # SINGLE DATE MODE (original behaviour)
    # =====================================================
    results = []
    # Calculate needed period (but keep original logic)
    days_back = (today - target_date).days + 2
    if days_back < 2:
        days_back = 2
    needed_period = f"{min(max(days_back, int(default_period.rstrip('d'))), MAX_DATA_DAYS)}d"
    print(f"Using data period: {needed_period}")

    for stock in stocks:
        try:
            print(f"Checking : {stock}")
            if stock.startswith("^") or ":" in stock:
                ticker = stock
            else:
                ticker = f"{stock}.NS"

            df = yf.download(ticker, period=needed_period, interval=interval,
                             progress=False, auto_adjust=False)
            if df.empty:
                print(f"  → No data for {ticker}")
                continue

            # Timezone
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")

            # Latest close (LCP)
            latest_close = float(df.iloc[-1]["Close"])
            latest_close_time = df.index[-1].strftime("%Y-%m-%d %H:%M")

            # Compute for target_date
            first_info, breakout, breakout_time, pnl, dated_last_close = compute_pnl_for_date(df, target_date, USE_HIGH_LOW)
            if first_info is None:
                print(f"  → No data for {target_date}")
                continue

            # Complete PnL using LCP
            complete_pnl = None
            if breakout == "HIGH BREAKOUT":
                rounded = round_up_to_05(first_info["high"])
                complete_pnl = round(latest_close - rounded, 2)
            elif breakout == "LOW BREAKOUT":
                rounded = round_down_to_05(first_info["low"])
                complete_pnl = round(rounded - latest_close, 2)

            results.append([
                stock, TIMEFRAME, target_date.strftime("%Y-%m-%d"),
                first_info["open"], first_info["high"], first_info["low"], first_info["close"],
                breakout, breakout_time, None, first_info["dated_last_close"],
                pnl, round(latest_close, 2), latest_close_time, complete_pnl
            ])
            time.sleep(0.1)
        except Exception as e:
            print(f"{stock} Failed : {e}")

    # Write single date output
    if results:
        output_df = pd.DataFrame(results, columns=[
            "Symbol", "Timeframe", "Date", "First Candle Open", "First Candle High",
            "First Candle Low", "First Candle Close", "Breakout", "Breakout Time",
            "Breakout Price", "Dated Last Close", "PnL Value (Dated)",
            "Latest Close (LCP)", "LCP Time", "Complete PnL Value"
        ])
        # Clean NaN/inf
        rows = [output_df.columns.tolist()]
        for row in output_df.values.tolist():
            clean_row = []
            for v in row:
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    clean_row.append(None)
                else:
                    clean_row.append(v)
            rows.append(clean_row)
        output_ws.update(values=rows, range_name="A1")
        print("\n✅ GOOGLE SHEET UPDATED (single date mode)")
    else:
        print("\n⚠️ No results to write.")

else:
    # =====================================================
    # BACKTEST MODE: multiple days from start_date to today
    # =====================================================
    print("\n=== BACKTEST MODE ENABLED ===")
    print(f"Start date: {target_date}, up to today: {today}")
    # Limit to MAX_DATA_DAYS (Yahoo intraday limit)
    max_allowed_start = today - timedelta(days=MAX_DATA_DAYS)
    if target_date < max_allowed_start:
        print(f"Warning: Start date {target_date} is older than {MAX_DATA_DAYS} days. Yahoo intraday data limited to {MAX_DATA_DAYS} days.")
        print(f"Using effective start date: {max_allowed_start}")
        target_date = max_allowed_start

    # For each symbol, download data once covering the whole range
    # Calculate needed period: from target_date-2 to today
    days_range = (today - target_date).days + 5
    needed_period = f"{min(days_range, MAX_DATA_DAYS)}d"
    print(f"Using data period: {needed_period}")

    all_symbol_results = []  # each element: dict with symbol and per-date pnl
    start_date_details = {}  # store first candle info for start_date per symbol

    for stock in stocks:
        try:
            print(f"Processing {stock} ...")
            if stock.startswith("^") or ":" in stock:
                ticker = stock
            else:
                ticker = f"{stock}.NS"

            df = yf.download(ticker, period=needed_period, interval=interval,
                             progress=False, auto_adjust=False)
            if df.empty:
                print(f"  → No data for {ticker}")
                continue

            # Timezone
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")

            # Get all unique dates in the dataframe that are >= target_date and <= today
            available_dates = sorted(set(df.index.date))
            valid_dates = [d for d in available_dates if target_date <= d <= today]
            if not valid_dates:
                print(f"  → No dates in range for {ticker}")
                continue

            # For each valid date, compute PnL
            date_pnl_map = {}
            for d in valid_dates:
                first_info, breakout, breakout_time, pnl, _ = compute_pnl_for_date(df, d, USE_HIGH_LOW)
                if first_info is not None:
                    date_pnl_map[d] = pnl
                    # Save start_date details if this is the start date
                    if d == target_date:
                        start_date_details[stock] = {
                            "first_open": first_info["open"],
                            "first_high": first_info["high"],
                            "first_low": first_info["low"],
                            "first_close": first_info["close"],
                            "breakout": breakout,
                            "breakout_time": breakout_time,
                            "dated_last_close": first_info["dated_last_close"],
                            "pnl_start": pnl
                        }

            # Also get latest close for Complete PnL (using today's data)
            latest_close = float(df.iloc[-1]["Close"])
            latest_close_time = df.index[-1].strftime("%Y-%m-%d %H:%M")
            # Compute Complete PnL using start_date's first candle (if breakout exists)
            complete_pnl = None
            if stock in start_date_details:
                sd = start_date_details[stock]
                if sd["breakout"] == "HIGH BREAKOUT":
                    rounded = round_up_to_05(sd["first_high"])
                    complete_pnl = round(latest_close - rounded, 2)
                elif sd["breakout"] == "LOW BREAKOUT":
                    rounded = round_down_to_05(sd["first_low"])
                    complete_pnl = round(rounded - latest_close, 2)

            all_symbol_results.append({
                "symbol": stock,
                "date_pnl": date_pnl_map,
                "latest_close": round(latest_close, 2),
                "latest_close_time": latest_close_time,
                "complete_pnl": complete_pnl,
                "start_details": start_date_details.get(stock)
            })
            time.sleep(0.1)
        except Exception as e:
            print(f"{stock} Failed : {e}")

    # Build the output DataFrame
    if not all_symbol_results:
        print("No results to write.")
        exit(0)

    # Collect all unique dates across all symbols (for columns)
    all_dates = set()
    for res in all_symbol_results:
        all_dates.update(res["date_pnl"].keys())
    all_dates = sorted([d for d in all_dates if d > target_date])  # exclude start date (already in main columns)

    # Prepare rows
    rows_data = []
    for res in all_symbol_results:
        sym = res["symbol"]
        sd = res["start_details"]
        if sd is None:
            # No data for start date, skip? We'll still add but with blanks
            sd = {
                "first_open": None, "first_high": None, "first_low": None, "first_close": None,
                "breakout": "", "breakout_time": "", "dated_last_close": None, "pnl_start": None
            }
        row = [
            sym,
            TIMEFRAME,
            target_date.strftime("%Y-%m-%d"),
            sd["first_open"], sd["first_high"], sd["first_low"], sd["first_close"],
            sd["breakout"], sd["breakout_time"],
            None,  # Breakout Price (unused)
            sd["dated_last_close"],
            sd["pnl_start"],
            res["latest_close"],
            res["latest_close_time"],
            res["complete_pnl"]
        ]
        # Append PnL for each future date (in order of all_dates)
        for d in all_dates:
            pnl = res["date_pnl"].get(d)
            row.append(pnl)
        rows_data.append(row)

    # Build column names
    base_cols = [
        "Symbol", "Timeframe", "Date", "First Candle Open", "First Candle High",
        "First Candle Low", "First Candle Close", "Breakout", "Breakout Time",
        "Breakout Price", "Dated Last Close", "PnL Value (Dated)",
        "Latest Close (LCP)", "LCP Time", "Complete PnL Value"
    ]
    date_cols = [d.strftime("%Y-%m-%d") for d in all_dates]
    all_cols = base_cols + date_cols

    output_df = pd.DataFrame(rows_data, columns=all_cols)

    # Clean NaN/inf values
    rows_out = [all_cols]
    for row in output_df.values.tolist():
        clean_row = []
        for v in row:
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean_row.append(None)
            else:
                clean_row.append(v)
        rows_out.append(clean_row)

    # Write to Google Sheet
    output_ws.update(values=rows_out, range_name="A1")
    print(f"\n✅ BACKTEST COMPLETE – {len(rows_data)} symbols, {len(date_cols)} future days")
    print(f"Columns added: {date_cols}")