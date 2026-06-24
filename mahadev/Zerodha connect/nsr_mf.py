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
from string import ascii_uppercase

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ================= USER SETTINGS =================
# Enable multi‑timeframe mode (set to True for MF, False for single)
MF_ENABLED = True

# List of timeframes to analyse when MF_ENABLED = True
# You can specify any subset from: "5m","15m","30m","1h","4h","1d","1w"
TIMEFRAMES = ["15m", "1h", "4h"]   # example – change as needed

# Single‑timeframe fallback (used only when MF_ENABLED = False)
SELECTED_TF = "15m"

# Common settings for all timeframes
INCLUDE_LTP = False
CASE_A_MARKET_ORDER = True
ONLY_BUY = True

# Google Sheet and file names (same for all TFs)
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input5"
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

# Base names for JSON files (timeframe will be appended)
JSON_BASE = "Nsr_trade_{}.json"
ORDER_BASE = "order_sent_{}.json"

# Column layout in Google Sheet:
# First timeframe starts at column A (index 0)
# Leave one empty column (S) as separator
# Next timeframe starts at column T (index 19)
# Column mapping: A=0, B=1, ... Z=25, AA=26, etc.
# We'll use a function to convert column index to letter.

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

# ================= CORE FUNCTIONS (identical to original) =================

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

def find_breakout_candle(df, resistance, support, trend):
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
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return info.get('regularMarketPrice') or info.get('currentPrice')
    except:
        return None

def load_order_sent(order_file):
    if os.path.exists(order_file):
        with open(order_file, 'r') as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set, order_file):
    with open(order_file, 'w') as f:
        json.dump([list(item) for item in order_set], f)

# ================= GOOGLE SHEET FUNCTIONS =================

def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)

def read_symbols(sheet):
    ws = sheet.worksheet(INPUT_SHEET)
    raw_symbols = [s.strip().upper() for s in ws.col_values(1) if s.strip()]
    processed = []
    for s in raw_symbols:
        if s.startswith("^") or ":" in s:
            processed.append(s)
        else:
            processed.append(f"{s}.NS")
    return processed

def col_letter(col_num):
    """Convert zero‑based column index to Excel column letters (e.g., 0->A, 25->Z, 26->AA)."""
    result = ""
    while col_num >= 0:
        result = chr(col_num % 26 + 65) + result
        col_num = col_num // 26 - 1
    return result

def write_tf_output(worksheet, rows, start_col, summary_line=None):
    """
    Write a timeframe's data rows starting at the given column.
    start_col: zero‑based column index (e.g., 0 for column A)
    summary_line: optional text to put at row 1, column start_col.
    """
    start_row = 1
    if summary_line:
        # Write summary at the first row of this block
        cell = f"{col_letter(start_col)}{start_row}"
        worksheet.update(cell, [[summary_line]], value_input_option="USER_ENTERED")
        start_row = 2

    if not rows:
        return

    # Prepare full block of cells: header row + data rows
    header = [
        "Timestamp", "Symbol", "Timeframe",
        "Open", "High", "Low", "Close",
        "Breakout DateTime",
        "Support", "Resistance", "Trend", "Previous Trend", "Value Change",
        "Symbol", "Trigger Trend", "Trigger Date & Time", "Trigger Status",
        "Zerodha Action"
    ]
    block = [header] + rows
    # Convert to list of lists for update
    start_cell = f"{col_letter(start_col)}{start_row}"
    worksheet.update(start_cell, block, value_input_option="USER_ENTERED")
    # (Colour formatting could be applied but is omitted for brevity – you can copy from original if needed)

# ================= MAIN =================

def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    total_symbols = len(symbols)
    if total_symbols == 0:
        print("No symbols found.")
        return

    start_time = datetime.now(IST)
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S %Z")

    # Determine which timeframes to run
    if MF_ENABLED:
        tfs_to_run = TIMEFRAMES
    else:
        tfs_to_run = [SELECTED_TF]

    # Get the worksheet (do not clear; we will write each TF to its own column block)
    try:
        ws = sheet.worksheet("Nsr_MF")  # use a separate sheet for multi‑TF results
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Nsr_MF", rows=2000, cols=50)

    # Clear only the area we will write (optional – we'll overwrite)
    # Alternatively, clear the whole sheet once:
    ws.clear()

    # Column offsets: start at A (0), leave one empty column after each TF
    next_col = 0

    for tf in tfs_to_run:
        print(f"\n{'='*60}")
        print(f"Processing timeframe: {tf}")
        print(f"{'='*60}")

        # Prepare file names for this timeframe
        json_file = JSON_BASE.format(tf)
        order_file = ORDER_BASE.format(tf)

        # Load previous data for this timeframe
        old_data = {}
        triggered_info = {}
        prev_run_start = None
        existing_run_history = {}
        if os.path.exists(json_file):
            with open(json_file) as f:
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
                prev = existing_run_history.get(tf)
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

        order_sent_set = load_order_sent(order_file)

        rows = []
        json_out = []
        buy_cnt = sell_cnt = inside_cnt = 0
        new_buy_triggers = 0
        new_sell_triggers = 0
        new_buy_symbols = []
        new_sell_symbols = []
        spinner = ["|", "/", "-", "\\"]

        # Process each symbol
        for idx, symbol in enumerate(symbols, start=1):
            print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total_symbols} : {symbol} [{tf}]", end="")
            sys.stdout.flush()

            r, s, prev_close, df = get_luxalgo_sr_and_prev_close(symbol, tf)
            if df is None:
                continue

            # --- Determine current trend ---
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

            previous_trend = get_previous_trend_from_data(df, r, s, trend)

            # Value Change
            old_entry = old_data.get((symbol, tf))
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

            # Default OHLC
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

            # Find breakout candle
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

            # Trigger detection
            trigger_trend = ""
            trigger_datetime = ""
            trigger_status = ""

            if breakout_type is not None and breakout_index is not None:
                trigger_key = f"{symbol}_{tf}"
                stored_trigger = triggered_info.get(trigger_key, {})
                stored_level = stored_trigger.get("level")
                stored_type = stored_trigger.get("type")
                if stored_trigger and stored_level == breakout_level and stored_type == breakout_type:
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

            # Zerodha Action
            zerodha_action = ""
            if breakout_type is not None:
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
                        if CASE_A_MARKET_ORDER:
                            zerodha_action = "Buy Market Order" if breakout_type == "Buy" else "Sell Market Order"
                            order_sent_set.add(order_key)
                        else:
                            zerodha_action = "Buy Long gap" if breakout_type == "Buy" else "Sell gap"
                    else:
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
                        else:
                            if current_ltp < breakout_level:
                                zerodha_action = "Sell Market Order"
                                order_sent_set.add(order_key)
                            else:
                                zerodha_action = f"Sell Limit Order (BO close {breakout_level})"
                                order_sent_set.add(order_key)

            # Write pending order (common file for executor)
            if zerodha_action and "Already" not in zerodha_action and "gap" not in zerodha_action:
                pending_order = {
                    "symbol": symbol,
                    "action": zerodha_action,
                    "level": breakout_level if breakout_level else None,
                    "timeframe": tf,
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

            # Build row
            triggered_at = ""
            old_trend = old_entry["trend"] if old_entry else None
            if trend in ("Buy Trend", "Sell Trend") and old_trend != trend:
                triggered_at = start_str

            display_symbol = symbol.replace('.NS', '')
            rows.append([
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                display_symbol, tf,
                open_val, high_val, low_val, close_val,
                breakout_dt_str,
                s, r, trend, previous_trend, change_text,
                display_symbol,
                trigger_trend, trigger_datetime, trigger_status,
                zerodha_action
            ])

            json_out.append({
                "symbol": symbol,
                "timeframe": tf,
                "trend": trend,
                "breakout_dt": breakout_dt_str,
                "triggered_at": triggered_at,
                "support": s,
                "resistance": r
            })

        # Save order_sent for this timeframe
        save_order_sent(order_sent_set, order_file)

        # Summary for this timeframe
        end_time = datetime.now(IST)
        duration_seconds = (end_time - start_time).total_seconds()
        duration_str = f"{duration_seconds:.1f}s"

        new_triggers_str = f"Buy: {new_buy_triggers}, Sell: {new_sell_triggers}"
        summary_line = f"{tf} | Run start: {start_str} | Duration: {duration_str} | New Triggers: {new_triggers_str} | Prev run: {prev_run_start or 'N/A'}"

        # Write to Google Sheet at the computed column offset
        write_tf_output(ws, rows, next_col, summary_line)

        # After writing, set next column offset: current block width (header length) + 1 empty column
        # Header has 18 columns
        next_col += 19   # 18 data columns + 1 empty spacer

        # Write JSON for this timeframe
        run_history = existing_run_history or {}
        run_history[tf] = {"run_start": start_str, "duration_seconds": duration_seconds}
        out_json = {
            "run_history": run_history,
            "data": json_out,
            "triggered_info": triggered_info
        }
        with open(json_file, "w") as f:
            json.dump(out_json, f, indent=4)

        # Console output for this TF
        print(f"\n\n📊 {tf} RUN SUMMARY")
        print("────────────────────────────")
        print(f"Timeframe       : {tf}")
        print(f"Run start       : {start_str}")
        print(f"Duration        : {duration_str}")
        print(f"Previous run    : {prev_run_start or 'N/A'}")
        print(f"Total Symbols   : {total_symbols}")
        print(f"Buy Trend       : {buy_cnt}")
        print(f"Sell Trend      : {sell_cnt}")
        print(f"Inside Trend    : {inside_cnt}")
        print(f"New Triggers    : Buy: {new_buy_triggers}, Sell: {new_sell_triggers}")
        print(f"Skipped Symbols : {len(SKIPPED_SYMBOLS)}")
        print("────────────────────────────")
        print("✅ Sheet & JSON updated successfully")

        if new_buy_symbols:
            print(f"\nNew Buy triggers ({tf}):")
            for s in new_buy_symbols:
                print(f"- {s.replace('.NS', '')}")
        else:
            print(f"\nNew Buy triggers ({tf}): None")

        if new_sell_symbols:
            print(f"\nNew Sell triggers ({tf}):")
            for s in new_sell_symbols:
                print(f"- {s.replace('.NS', '')}")
        else:
            print(f"\nNew Sell triggers ({tf}): None")

        # Reset skipped symbols for next timeframe (optional)
        SKIPPED_SYMBOLS.clear()

    print("\n✅ All timeframes processed.")

if __name__ == "__main__":
    main()