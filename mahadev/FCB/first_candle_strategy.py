import yfinance as yf
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, date
import pytz
import sys
import time

# ================= USER SETTINGS =================
AUTO_DATE = True                # True = use today; False = ask for a date
MANUAL_DATE = "2025-03-20"      # used only if AUTO_DATE = False (format YYYY-MM-DD)
TIMEFRAME = "15m"               # e.g., "5m", "15m", "1h", "1d"

# Google Sheet configuration (same as your existing setup)
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input5"
OUTPUT_SHEET = f"FirstCandle_{TIMEFRAME}"   # will be created automatically
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

IST = pytz.timezone("Asia/Kolkata")
RATE_LIMIT_DELAY = 0.2          # seconds between symbol requests (avoid Yahoo blocking)

# ================= HELPER FUNCTIONS =================

def connect_gsheet():
    """Authenticate and return Google Sheet object."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)

def read_symbols(sheet):
    """Read symbols from input sheet (first column). Add .NS for NSE stocks."""
    ws = sheet.worksheet(INPUT_SHEET)
    raw_symbols = [s.strip().upper() for s in ws.col_values(1) if s.strip()]
    processed = []
    for s in raw_symbols:
        if s.startswith("^") or ":" in s:   # indices or forex
            processed.append(s)
        else:
            processed.append(f"{s}.NS")
    return processed

def get_first_candle_and_ltp(symbol, target_date, interval):
    """
    Downloads data for the given symbol and interval.
    target_date can be a date or datetime object.
    Returns a dict with candle info and LTP.
    """
    # Normalise target_date to date object
    if isinstance(target_date, datetime):
        target_dt = target_date
        target_date_ist = target_dt.date()
    elif isinstance(target_date, date):
        target_date_ist = target_date
        # Create a datetime for start/end calculations (use noon to avoid DST issues)
        target_dt = datetime.combine(target_date, datetime.min.time())
    else:
        return {"status": f"Invalid date type: {type(target_date)}"}

    # Fetch data covering the target date (add 2 days buffer)
    start_date = target_dt - timedelta(days=1)
    end_date = target_dt + timedelta(days=1)

    try:
        # auto_adjust=False prevents the warning and keeps raw OHLC
        df = yf.download(symbol, start=start_date, end=end_date, interval=interval,
                         progress=False, auto_adjust=False)
    except Exception as e:
        return {"status": f"Download failed: {e}"}

    if df is None or df.empty:
        return {"status": "No data returned from Yahoo"}

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.dropna()

    # Convert index to IST timezone (Yahoo gives UTC)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    # Filter only candles that belong to the target date (IST)
    mask = df.index.date == target_date_ist
    df_day = df[mask]

    if df_day.empty:
        return {"status": f"No candle found for {target_date_ist} with interval {interval}"}

    # First candle of that day
    first = df_day.iloc[0]

    # Get current LTP
    ltp = None
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        ltp = info.get('regularMarketPrice') or info.get('currentPrice')
        if ltp:
            ltp = round(ltp, 2)
    except:
        pass

    return {
        "status": "OK",
        "date_str": target_date_ist.strftime("%Y-%m-%d"),
        "time_str": first.name.strftime("%H:%M"),
        "open": round(first["Open"], 2),
        "high": round(first["High"], 2),
        "low": round(first["Low"], 2),
        "close": round(first["Close"], 2),
        "ltp": ltp
    }

def write_output(sheet, rows, date_str, timeframe):
    """Write results to a new worksheet (or replace existing one)."""
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=1000, cols=20)

    header = [
        "Timestamp", "Symbol", "Date", "Timeframe",
        "Candle Time (IST)", "Open", "High", "Low", "Close", "LTP", "Status"
    ]
    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")
    print(f"✅ Results written to sheet '{OUTPUT_SHEET}'")

# ================= MAIN =================

def main():
    # Determine target date
    if AUTO_DATE:
        target_date = datetime.now(IST).date()
        print(f"Auto date = True → using today: {target_date}")
    else:
        try:
            target_date = datetime.strptime(MANUAL_DATE, "%Y-%m-%d").date()
            print(f"Auto date = False → using manual date: {target_date}")
        except ValueError:
            print("ERROR: MANUAL_DATE must be in YYYY-MM-DD format")
            sys.exit(1)

    print(f"Timeframe: {TIMEFRAME}")
    print("Connecting to Google Sheet...")
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    print(f"Loaded {len(symbols)} symbols from '{INPUT_SHEET}'")

    rows = []
    success = 0
    failed = 0

    for idx, sym in enumerate(symbols, 1):
        display_sym = sym.replace('.NS', '')
        print(f"Processing [{idx}/{len(symbols)}] {display_sym} ...", end=" ")
        sys.stdout.flush()

        result = get_first_candle_and_ltp(sym, target_date, TIMEFRAME)

        if result["status"] == "OK":
            rows.append([
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                display_sym,
                result["date_str"],
                TIMEFRAME,
                result["time_str"],
                result["open"],
                result["high"],
                result["low"],
                result["close"],
                result["ltp"] if result["ltp"] else "N/A",
                "OK"
            ])
            success += 1
            print("✓")
        else:
            rows.append([
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                display_sym,
                str(target_date),
                TIMEFRAME,
                "",
                "",
                "",
                "",
                "",
                "",
                f"ERROR: {result['status']}"
            ])
            failed += 1
            print("✗")

        

    # Write all rows to Google Sheet
    if rows:
        write_output(sheet, rows, str(target_date), TIMEFRAME)
    else:
        print("No data to write.")

    print("\n=== SUMMARY ===")
    print(f"Total symbols      : {len(symbols)}")
    print(f"Successful         : {success}")
    print(f"Failed             : {failed}")
    print(f"Output sheet       : {OUTPUT_SHEET}")
    print(f"Selected date      : {target_date}")
    print(f"Timeframe          : {TIMEFRAME}")

if __name__ == "__main__":
    main()