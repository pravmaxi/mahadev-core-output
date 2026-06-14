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
# These settings let you change the behaviour without modifying the core logic.

MULTI_TF_MODE = False               # Not used; kept for legacy.
SELECTED_TF = "15m"                 # Timeframe for analysis (5m, 15m, 1h, etc.)

INCLUDE_LTP = False                 # If True, include current incomplete candle (LTP) for trigger detection.
CASE_A_MARKET_ORDER = True          # When a trigger already happened: True = recommend Market Order, False = "Long buy gap" / "Sell gap".
ONLY_BUY = True                    # If True, process only Buy signals; if False, process both Buy and Sell.

# Google Sheet and file names
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = f"Nsr_trade__{SELECTED_TF}"
JSON_FILE = f"Nsr_trade_{SELECTED_TF}.json"          # Stores full analysis results for history.
ORDER_FILE = f"order_sent_{SELECTED_TF}.json"        # Stores (symbol, S/R value, direction) to prevent duplicate orders.
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

SKIPPED_SYMBOLS = set()             # Symbols that failed to download.
IST = pytz.timezone("Asia/Kolkata") # Indian timezone for timestamps.

# Configuration for each timeframe: how much data to download and how many left/right candles for S/R detection.
CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d", "left": 15, "right": 15},
    "15m": {"interval": "15m", "period": "60d", "left": 15, "right": 15},
    "30m": {"interval": "30m", "period": "60d", "left": 15, "right": 15},
    "1h":  {"interval": "1h",  "period": "90d", "left": 15, "right": 15},
    "4h":  {"interval": "4h",  "period": "10mo","left": 15, "right": 15},
    "1d":  {"interval": "1d",  "period": "10mo","left": 15, "right": 15},
    "1w":  {"interval": "1w",  "period": "10mo","left": 15, "right": 15},
}

# ================= CORE FUNCTIONS =================

def get_luxalgo_sr_and_prev_close(symbol, tf):
    """
    Downloads historical data for a symbol and timeframe.
    Uses the LuxAlgo method to find the most recent support (s) and resistance (r).
    Also returns the close of the second‑last candle (prev_close) and the full dataframe.
    """
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
    # Scan each candle to find swing highs/lows within a window of left and right candles.
    for i in range(cfg["left"], len(df) - cfg["right"]):
        if highs[i] == highs[i-cfg["left"]:i+cfg["right"]+1].max():
            last_r = round(highs[i], 2)
        if lows[i] == lows[i-cfg["left"]:i+cfg["right"]+1].min():
            last_s = round(lows[i], 2)
    prev_close = round(df["Close"].iloc[-2].item(), 2)   # close of the second‑last candle
    return last_r, last_s, prev_close, df

def find_breakout_candle(df, resistance, support, trend):
    """
    Finds the most recent breakout candle:
    - For Buy Trend: the first candle (from the end) that closed above resistance after a reset.
    - For Sell Trend: the first candle that closed below support after a reset.
    Returns its datetime, OHLC, and index in the dataframe.
    """
    closes = df["Close"].values
    times = df.index
    for i in range(len(df) - 1, 0, -1):
        prev_close = closes[i - 1]
        curr_close = closes[i]
        if trend == "Buy Trend" and resistance:
            if prev_close <= resistance and curr_close > resistance:
                candle = df.iloc[i]
                dt = pd.to_datetime(times[i])
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC").tz_convert(IST)
                else:
                    dt = dt.tz_convert(IST)
                return (dt.strftime("%Y-%m-%d %H:%M"), round(candle["Open"].item(), 2),
                        round(candle["High"].item(), 2), round(candle["Low"].item(), 2),
                        round(candle["Close"].item(), 2), i)
        if trend == "Sell Trend" and support:
            if prev_close >= support and curr_close < support:
                candle = df.iloc[i]
                dt = pd.to_datetime(times[i])
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC").tz_convert(IST)
                else:
                    dt = dt.tz_convert(IST)
                return (dt.strftime("%Y-%m-%d %H:%M"), round(candle["Open"].item(), 2),
                        round(candle["High"].item(), 2), round(candle["Low"].item(), 2),
                        round(candle["Close"].item(), 2), i)
    return "", None, None, None, None, None

def get_previous_trend_from_data(df, resistance, support, current_trend):
    """
    Scans the dataframe backwards (starting from the candle before the one used for current trend)
    and returns the first trend (Buy/Sell/Inside) that is different from the current trend.
    Used for the "Previous Trend" column in the sheet.
    """
    if resistance is None and support is None:
        return ""
    closes = df["Close"].values
    for i in range(len(df) - 3, -1, -1):
        close_val = closes[i]
        if resistance is not None and close_val > resistance:
            trend = "Buy Trend"
        elif support is not None and close_val < support:
            trend = "Sell Trend"
        elif resistance is not None and support is not None and support < close_val < resistance:
            trend = "Inside Trend"
        else:
            continue
        if trend != current_trend:
            return trend
    return ""

def find_trigger_candle(df, breakout_index, trigger_level, trend_type, current_ltp=None):
    """
    After a breakout candle, scans forward to find the first candle that “triggers”:
    - For Buy: the candle's high > trigger_level (the breakout close).
    - For Sell: the candle's low < trigger_level.
    If INCLUDE_LTP is True and no historical trigger found, it checks the current live price.
    Returns (datetime_str, trigger_type) or (None, None).
    """
    for i in range(breakout_index + 1, len(df)):
        row = df.iloc[i]
        dt = pd.to_datetime(df.index[i])
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC").tz_convert(IST)
        else:
            dt = dt.tz_convert(IST)
        if trend_type == "Buy":
            if row["High"] > trigger_level:
                return dt.strftime("%Y-%m-%d %H:%M"), "Buy Trigger"
        elif trend_type == "Sell":
            if row["Low"] < trigger_level:
                return dt.strftime("%Y-%m-%d %H:%M"), "Sell Trigger"
    if INCLUDE_LTP and current_ltp is not None:
        if trend_type == "Buy" and current_ltp > trigger_level:
            now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
            return now, "Buy Trigger"
        elif trend_type == "Sell" and current_ltp < trigger_level:
            now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
            return now, "Sell Trigger"
    return None, None

def get_current_ltp(symbol):
    """Fetches the current market price (Last Traded Price) for a symbol."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return info.get('regularMarketPrice') or info.get('currentPrice')
    except:
        return None

def load_order_sent():
    """Loads the set of already‑sent orders from order_sent_{TF}.json.
       Each entry is a tuple (symbol, S/R value, direction)."""
    if os.path.exists(ORDER_FILE):
        with open(ORDER_FILE, 'r') as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set):
    """Saves the order_sent set back to the JSON file."""
    with open(ORDER_FILE, 'w') as f:
        json.dump([list(item) for item in order_set], f)

# ================= GOOGLE SHEET FUNCTIONS =================

def connect_gsheet():
    """Authenticates and returns a connection to the Google Sheet."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)

def read_symbols(sheet):
    """Reads symbols from INPUT_SHEET (first column).
       Leaves ^NSEI, ^NSEBANK, etc. untouched; adds .NS to normal stock symbols.
    """
    ws = sheet.worksheet(INPUT_SHEET)
    raw_symbols = [s.strip().upper() for s in ws.col_values(1) if s.strip()]
    
    processed = []
    for s in raw_symbols:
        # Keep index symbols (starting with ^) and any symbol containing ':' as they are
        if s.startswith("^") or ":" in s:
            processed.append(s)
        else:
            processed.append(f"{s}.NS")
    return processed

def write_output_batch(sheet, rows, summary=None):
    """
    Writes the analysis rows to the OUTPUT_SHEET.
    Also applies colour formatting to Trend, Value Change, Trigger Trend, Trigger Status, and Zerodha Action columns.
    """
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=20)
    ws.clear()
    start_row = 1
    if summary:
        try:
            ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
            start_row = 2
        except Exception:
            pass
    header = [
        "Timestamp", "Symbol", "Timeframe",
        "Open", "High", "Low", "Close",
        "Breakout DateTime",
        "Support", "Resistance", "Trend", "Previous Trend", "Value Change",
        "Symbol", "Trigger Trend", "Trigger Date & Time", "Trigger Status",
        "Zerodha Action"
    ]
    ws.update(f"A{start_row}", [header] + rows, value_input_option="USER_ENTERED")
    # (colour formatting code omitted for brevity – keep your existing implementation)

# ================= MAIN =================

def main():
    # Connect to Google Sheet and get symbols
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    total = len(symbols)

    start_time = datetime.now(IST)
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S %Z")

    # Load previous JSON (Nsr_trade_{TF}.json) for historical data and triggered_info
    old_data = {}
    triggered_info = {}
    prev_run_start = None
    existing_run_history = {}

    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            try:
                existing = json.load(f)
            except Exception:
                existing = None
        if isinstance(existing, list):
            data_list = existing
        elif isinstance(existing, dict):
            data_list = existing.get("data", [])
            existing_run_history = existing.get("run_history", {})
            triggered_info = existing.get("triggered_info", {})
            prev = existing_run_history.get(SELECTED_TF)
            if prev:
                prev_run_start = prev.get("run_start")
        else:
            data_list = []
        for entry in data_list:
            if isinstance(entry, dict) and "symbol" in entry and "timeframe" in entry:
                key = (entry["symbol"], entry["timeframe"])
                old_data[key] = {
                    "trend": entry.get("trend"),
                    "support": entry.get("support"),
                    "resistance": entry.get("resistance")
                }

    # Load the set of already‑sent orders (prevents duplicate recommendations)
    order_sent_set = load_order_sent()

    rows = []          # rows to be written to Google Sheet
    json_out = []      # data to be saved in the JSON file
    buy_cnt = sell_cnt = inside_cnt = 0
    new_buy_triggers = new_sell_triggers = 0
    new_buy_symbols = []
    new_sell_symbols = []
    spinner = ["|", "/", "-", "\\"]

    # Process each symbol one by one
    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        r, s, prev_close, df = get_luxalgo_sr_and_prev_close(symbol, SELECTED_TF)
        if df is None:
            continue

        # --- Determine current trend using the close of the second‑last candle ---
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

        # --- Previous Trend (from historical data) ---
        previous_trend = get_previous_trend_from_data(df, r, s, trend)

        # --- Value Change: compare current support/resistance with previous run ---
        old_entry = old_data.get((symbol, SELECTED_TF))
        old_support = old_entry["support"] if old_entry else None
        old_resistance = old_entry["resistance"] if old_entry else None
        support_changed = (old_support is not None and s != old_support)
        resistance_changed = (old_resistance is not None and r != old_resistance)
        if support_changed and resistance_changed:
            change_text = "Yes - Both changed"
        elif support_changed:
            change_text = "Yes - Support changed"
        elif resistance_changed:
            change_text = "Yes - Resistance changed"
        else:
            change_text = "No - Not changed"
        if old_support is None and old_resistance is None:
            change_text = "No - Not changed"

        # --- Default OHLC (latest completed candle) ---
        last_row = df.iloc[-1]
        default_open = round(last_row["Open"].item(), 2)
        default_high = round(last_row["High"].item(), 2)
        default_low = round(last_row["Low"].item(), 2)
        default_close = round(last_row["Close"].item(), 2)
        breakout_dt_str = ""
        open_val, high_val, low_val, close_val = default_open, default_high, default_low, default_close
        breakout_index = None
        breakout_level = None
        breakout_type = None

        # --- Find breakout candle (only if trend is Buy or Sell) ---
        if trend == "Buy Trend" and not ONLY_BUY:
            dt_str, o, h, l, c, idx_break = find_breakout_candle(df, r, s, trend)
            if dt_str:
                breakout_dt_str = dt_str
                open_val, high_val, low_val, close_val = o, h, l, c
                breakout_index = idx_break
                breakout_level = c
                breakout_type = "Buy"
        elif trend == "Sell Trend" and not ONLY_BUY:
            dt_str, o, h, l, c, idx_break = find_breakout_candle(df, r, s, trend)
            if dt_str:
                breakout_dt_str = dt_str
                open_val, high_val, low_val, close_val = o, h, l, c
                breakout_index = idx_break
                breakout_level = c
                breakout_type = "Sell"
        elif trend == "Buy Trend" and ONLY_BUY:
            dt_str, o, h, l, c, idx_break = find_breakout_candle(df, r, s, trend)
            if dt_str:
                breakout_dt_str = dt_str
                open_val, high_val, low_val, close_val = o, h, l, c
                breakout_index = idx_break
                breakout_level = c
                breakout_type = "Buy"

        # --- Trigger detection (the candle that crosses the breakout close) ---
        trigger_trend = ""
        trigger_datetime = ""
        trigger_status = ""

        if breakout_type is not None and breakout_index is not None:
            trigger_key = f"{symbol}_{SELECTED_TF}"
            stored_trigger = triggered_info.get(trigger_key, {})
            stored_level = stored_trigger.get("level")
            stored_type = stored_trigger.get("type")
            if stored_trigger and stored_level == breakout_level and stored_type == breakout_type:
                # Reuse already stored trigger (so we don't duplicate New Trigger)
                trigger_trend = stored_trigger.get("trend", "")
                trigger_datetime = stored_trigger.get("datetime", "")
                trigger_status = ""
            else:
                trig_dir = breakout_type
                current_ltp = None
                if INCLUDE_LTP:
                    current_ltp = get_current_ltp(symbol)
                trig_dt, trig_type = find_trigger_candle(df, breakout_index, breakout_level, trig_dir, current_ltp)
                if trig_dt:
                    trigger_trend = trig_type
                    trigger_datetime = trig_dt
                    trigger_status = "New Trigger"
                    # Save this trigger in triggered_info so it persists across runs
                    triggered_info[trigger_key] = {
                        "trend": trigger_trend,
                        "datetime": trigger_datetime,
                        "level": breakout_level,
                        "type": breakout_type,
                        "resistance_at_trigger": r if breakout_type == "Buy" else None,
                        "support_at_trigger": s if breakout_type == "Sell" else None
                    }
                    if trig_type == "Buy Trigger":
                        new_buy_triggers += 1
                        new_buy_symbols.append(symbol)
                    elif trig_type == "Sell Trigger":
                        new_sell_triggers += 1
                        new_sell_symbols.append(symbol)

        # --- Zerodha Action: decide what order to suggest (or skip) ---
        zerodha_action = ""
        if breakout_type is not None:
            # Use the resistance (for buy) or support (for sell) as the key to prevent duplicate orders.
            if breakout_type == "Buy":
                s_r_value = r
            else:
                s_r_value = s
            order_key = (symbol, s_r_value, breakout_type.lower())
            if order_key in order_sent_set:
                zerodha_action = "Already buy order sent" if breakout_type == "Buy" else "Already sell order sent"
            else:
                trigger_happened = (trigger_trend != "")
                if trigger_happened:
                    # Case A: trigger already happened (i.e., a later candle already crossed the breakout close)
                    if CASE_A_MARKET_ORDER:
                        zerodha_action = "Buy Market Order" if breakout_type == "Buy" else "Sell Market Order"
                        order_sent_set.add(order_key)   # mark as sent
                    else:
                        zerodha_action = "Buy Long gap" if breakout_type == "Buy" else "Sell gap"
                else:
                    # Case B/C: trigger not yet happened; decide based on current LTP vs breakout level
                    if INCLUDE_LTP:
                        current_ltp = get_current_ltp(symbol)
                        if current_ltp is None:
                            current_ltp = default_close
                    else:
                        current_ltp = default_close
                    if breakout_type == "Buy":
                        if current_ltp > breakout_level:
                            zerodha_action = "Buy Market Order"
                            order_sent_set.add(order_key)
                        else:
                            zerodha_action = f"Buy Limit Order (BO close {breakout_level})"
                            order_sent_set.add(order_key)
                    else:  # Sell
                        if current_ltp < breakout_level:
                            zerodha_action = "Sell Market Order"
                            order_sent_set.add(order_key)
                        else:
                            zerodha_action = f"Sell Limit Order (BO close {breakout_level})"
                            order_sent_set.add(order_key)

        # --- Write pending order to a common file for the order executor (real‑time) ---
        if zerodha_action and "Already" not in zerodha_action and "gap" not in zerodha_action:
            pending_order = {
                "symbol": symbol,
                "action": zerodha_action,
                "level": breakout_level if breakout_level else None,
                "timeframe": SELECTED_TF,
                "timestamp": datetime.now(IST).isoformat()
            }
            pending_file = "pending_orders.json"
            try:
                if os.path.exists(pending_file):
                    with open(pending_file, 'r') as f:
                        pending = json.load(f)
                else:
                    pending = []
                pending.append(pending_order)
                with open(pending_file, 'w') as f:
                    json.dump(pending, f, indent=2)
            except Exception as e:
                print(f"Warning: Could not write to {pending_file}: {e}")

        # --- Legacy: triggered_at (trend change alert) ---
        triggered_at = ""
        old_trend = old_entry["trend"] if old_entry else None
        if trend in ("Buy Trend", "Sell Trend") and old_trend != trend:
            triggered_at = start_str

        # Build the row for the Google Sheet (display symbol without .NS)
        display_symbol = symbol.replace('.NS', '')
        rows.append([
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            display_symbol, SELECTED_TF,
            open_val, high_val, low_val, close_val,
            breakout_dt_str,
            s, r, trend, previous_trend, change_text,
            display_symbol,
            trigger_trend, trigger_datetime, trigger_status,
            zerodha_action
        ])

        # Save data for the JSON file (full history)
        json_out.append({
            "symbol": symbol,
            "timeframe": SELECTED_TF,
            "trend": trend,
            "breakout_dt": breakout_dt_str,
            "triggered_at": triggered_at,
            "support": s,
            "resistance": r
        })

    # Save the updated order_sent set (prevents duplicates in future runs)
    save_order_sent(order_sent_set)

    # Summary statistics
    end_time = datetime.now(IST)
    duration_seconds = (end_time - start_time).total_seconds()
    duration_str = f"{duration_seconds:.1f}s"

    new_triggers_str = f"Buy: {new_buy_triggers}, Sell: {new_sell_triggers}"
    summary = f"Timeframe: {SELECTED_TF} | Run start: {start_str} | Duration: {duration_str} | New Triggers: {new_triggers_str} | Prev run: {prev_run_start or 'N/A'}"

    # Write to Google Sheet
    if rows:
        write_output_batch(sheet, rows, summary=summary)

    # Write to the JSON file (Nsr_trade_{TF}.json)
    run_history = existing_run_history or {}
    run_history[SELECTED_TF] = {"run_start": start_str, "duration_seconds": duration_seconds}
    out_json = {
        "run_history": run_history,
        "data": json_out,
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

    # Console summary
    print("\n\n📊 RUN SUMMARY")
    print("────────────────────────────")
    print(f"Timeframe       : {SELECTED_TF}")
    print(f"Run start       : {start_str}")
    print(f"Duration        : {duration_str}")
    print(f"Previous run    : {prev_run_start or 'N/A'}")
    print(f"Total Symbols   : {total}")
    print(f"Buy Trend       : {buy_cnt}")
    print(f"Sell Trend      : {sell_cnt}")
    print(f"Inside Trend    : {inside_cnt}")
    print(f"New Triggers    : Buy: {new_buy_triggers}, Sell: {new_sell_triggers}")
    print(f"Skipped Symbols : {len(SKIPPED_SYMBOLS)}")
    print("────────────────────────────")
    print("✅ Sheet & JSON updated successfully")

    if new_buy_symbols:
        print("\nNew Buy triggers:")
        for s in new_buy_symbols:
            print(f"- {s.replace('.NS', '')}")
    else:
        print("\nNew Buy triggers: None")

    if new_sell_symbols:
        print("\nNew Sell triggers:")
        for s in new_sell_symbols:
            print(f"- {s.replace('.NS', '')}")
    else:
        print("\nNew Sell triggers: None")

if __name__ == "__main__":
    main()