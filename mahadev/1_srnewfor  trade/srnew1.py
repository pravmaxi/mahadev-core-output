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
SELECTED_TF = "15m"

# Set to True if you want to include the current incomplete candle (LTP) for trigger detection
INCLUDE_LTP = False

# Toggle for Case A (trigger already happened)
# True  -> recommend "Market Order"
# False -> recommend "Long buy gap" (or "Sell gap" for sell)
CASE_A_MARKET_ORDER = True

# Set to True to process only Buy signals; False to process both Buy and Sell
ONLY_BUY = False

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input1"
OUTPUT_SHEET = "sroutput_multitf" if MULTI_TF_MODE else f"Nsr_trade__{SELECTED_TF}"
JSON_FILE = f"Nsr_trade_{SELECTED_TF}.json"
ORDER_FILE = f"order_sent_{SELECTED_TF}.json"   # stores (symbol, level, 'buy'/'sell')
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

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


def find_breakout_candle(df, resistance, support, trend):
    """
    Returns (datetime_str, open, high, low, close, index) of the breakout candle.
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
                return (
                    dt.strftime("%Y-%m-%d %H:%M"),
                    round(candle["Open"].item(), 2),
                    round(candle["High"].item(), 2),
                    round(candle["Low"].item(), 2),
                    round(candle["Close"].item(), 2),
                    i
                )

        if trend == "Sell Trend" and support:
            if prev_close >= support and curr_close < support:
                candle = df.iloc[i]
                dt = pd.to_datetime(times[i])
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC").tz_convert(IST)
                else:
                    dt = dt.tz_convert(IST)
                return (
                    dt.strftime("%Y-%m-%d %H:%M"),
                    round(candle["Open"].item(), 2),
                    round(candle["High"].item(), 2),
                    round(candle["Low"].item(), 2),
                    round(candle["Close"].item(), 2),
                    i
                )

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
    """
    Scan forward from breakout_index+1 to find the first candle that crosses trigger_level.
    For Buy: high > trigger_level. For Sell: low < trigger_level.
    If INCLUDE_LTP is True and current_ltp provided, also check current incomplete candle.
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
    # No historical trigger, check LTP if enabled
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


def load_order_sent():
    """Load the set of (symbol, level, order_type) for which an order has already been sent."""
    if os.path.exists(ORDER_FILE):
        with open(ORDER_FILE, 'r') as f:
            data = json.load(f)
            # Convert list of lists to set of tuples
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set):
    """Save the order sent set to JSON."""
    with open(ORDER_FILE, 'w') as f:
        json.dump([list(item) for item in order_set], f)

# ================= GOOGLE SHEET =================
def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SHEET_NAME)


def read_symbols(sheet):
    ws = sheet.worksheet(INPUT_SHEET)
    return [f"{s.strip().upper()}.NS" for s in ws.col_values(1) if s.strip()]


def write_output_batch(sheet, rows, summary=None):
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
        "Symbol",   # duplicate column
        "Trigger Trend", "Trigger Date & Time", "Trigger Status",
        "Zerodha Action"
    ]

    ws.update(f"A{start_row}", [header] + rows, value_input_option="USER_ENTERED")

    format_requests = []
    data_start = start_row + 1
    for i, row in enumerate(rows, start=data_start):
        trend = row[10] if len(row) > 10 else ""
        change_text = row[12] if len(row) > 12 else ""
        trigger_trend = row[14] if len(row) > 14 else ""
        trigger_status = row[16] if len(row) > 16 else ""
        zerodha_action = row[17] if len(row) > 17 else ""

        # Trend colors (K)
        if trend == "Buy Trend":
            format_requests.append({"range": f"K{i}:K{i}", "format": {"backgroundColor": {"red": 0.6, "green": 1.0, "blue": 0.6}, "textFormat": {"bold": True}}})
        elif trend == "Sell Trend":
            format_requests.append({"range": f"K{i}:K{i}", "format": {"backgroundColor": {"red": 1.0, "green": 0.6, "blue": 0.6}, "textFormat": {"bold": True}}})
        elif trend == "Inside Trend":
            format_requests.append({"range": f"K{i}:K{i}", "format": {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 0.6}, "textFormat": {"bold": True}}})

        # Value Change colors (M)
        if "Yes" in change_text:
            format_requests.append({"range": f"M{i}:M{i}", "format": {"backgroundColor": {"red": 0.56, "green": 0.93, "blue": 0.56}}})
        elif "No" in change_text:
            format_requests.append({"range": f"M{i}:M{i}", "format": {"backgroundColor": {"red": 0.83, "green": 0.83, "blue": 0.83}}})

        # Trigger Trend colors (O)
        if trigger_trend == "Buy Trigger":
            format_requests.append({"range": f"O{i}:O{i}", "format": {"backgroundColor": {"red": 0.68, "green": 0.85, "blue": 0.90}}})
        elif trigger_trend == "Sell Trigger":
            format_requests.append({"range": f"O{i}:O{i}", "format": {"backgroundColor": {"red": 1.0, "green": 0.6, "blue": 0.6}}})

        # Trigger Status (Q)
        if trigger_status == "New Trigger":
            format_requests.append({"range": f"Q{i}:Q{i}", "format": {"backgroundColor": {"red": 0.6, "green": 1.0, "blue": 0.6}}})

        # Zerodha Action column (R)
        if "Market Order" in zerodha_action:
            format_requests.append({"range": f"R{i}:R{i}", "format": {"backgroundColor": {"red": 0.56, "green": 0.93, "blue": 0.56}}})
        elif "Limit Order" in zerodha_action:
            format_requests.append({"range": f"R{i}:R{i}", "format": {"backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.6}}})
        elif "Long gap" in zerodha_action or "Sell gap" in zerodha_action:
            format_requests.append({"range": f"R{i}:R{i}", "format": {"backgroundColor": {"red": 0.68, "green": 0.85, "blue": 0.90}}})
        elif "Already" in zerodha_action:
            format_requests.append({"range": f"R{i}:R{i}", "format": {"backgroundColor": {"red": 0.83, "green": 0.83, "blue": 0.83}}})

    if format_requests:
        ws.batch_format(format_requests)


# ================= MAIN =================
def main():
    sheet = connect_gsheet()
    symbols = read_symbols(sheet)
    total = len(symbols)

    start_time = datetime.now(IST)
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S %Z")

    # Load previous JSON
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

    # Load order sent records
    order_sent_set = load_order_sent()

    rows, json_out = [], []
    buy_cnt = sell_cnt = inside_cnt = 0
    new_buy_triggers = new_sell_triggers = 0
    new_buy_symbols = []
    new_sell_symbols = []
    spinner = ["|", "/", "-", "\\"]

    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        r, s, prev_close, df = get_luxalgo_sr_and_prev_close(symbol, SELECTED_TF)
        if df is None:
            continue

        # Current trend
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

        # Default OHLC (latest)
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

        # ----- Trigger logic -----
        trigger_trend = ""
        trigger_datetime = ""
        trigger_status = ""

        if breakout_type is not None and breakout_index is not None:
            trigger_key = f"{symbol}_{SELECTED_TF}"
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

        # ---------------- Zerodha Action column logic (FIXED) ----------------
        zerodha_action = ""
        if breakout_type is not None:
            level = breakout_level
            order_key = (symbol, level, breakout_type.lower())
            if order_key in order_sent_set:
                if breakout_type == "Buy":
                    zerodha_action = "Already buy order sent"
                else:
                    zerodha_action = "Already sell order sent"
            else:
                trigger_happened = (trigger_trend != "")
                if trigger_happened:
                    # Case A: trigger already happened
                    if CASE_A_MARKET_ORDER:
                        if breakout_type == "Buy":
                            zerodha_action = "Buy Market Order"
                        else:
                            zerodha_action = "Sell Market Order"
                        order_sent_set.add(order_key)
                    else:
                        if breakout_type == "Buy":
                            zerodha_action = "Buy Long gap"
                        else:
                            zerodha_action = "Sell gap"
                        # No order sent for gap, do not mark
                else:
                    # Case B/C: trigger not yet happened
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
                            # FIX: Mark order as sent for limit order too
                            order_sent_set.add(order_key)
                    else:  # Sell
                        if current_ltp < breakout_level:
                            zerodha_action = "Sell Market Order"
                            order_sent_set.add(order_key)
                        else:
                            zerodha_action = f"Sell Limit Order (BO close {breakout_level})"
                            # FIX: Mark order as sent for limit order too
                            order_sent_set.add(order_key)
        # -----------------------------------------------------------

        # Old trigger alert based on trend change
        triggered_at = ""
        old_trend = old_entry["trend"] if old_entry else None
        if trend in ("Buy Trend", "Sell Trend") and old_trend != trend:
            triggered_at = start_str

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

        json_out.append({
            "symbol": symbol,
            "timeframe": SELECTED_TF,
            "trend": trend,
            "breakout_dt": breakout_dt_str,
            "triggered_at": triggered_at,
            "support": s,
            "resistance": r
        })

    # Save order sent set
    save_order_sent(order_sent_set)

    # Summary stats
    end_time = datetime.now(IST)
    duration_seconds = (end_time - start_time).total_seconds()
    duration_str = f"{duration_seconds:.1f}s"

    new_triggers_str = f"Buy: {new_buy_triggers}, Sell: {new_sell_triggers}"
    summary = f"Timeframe: {SELECTED_TF} | Run start: {start_str} | Duration: {duration_str} | New Triggers: {new_triggers_str} | Prev run: {prev_run_start or 'N/A'}"

    if rows:
        write_output_batch(sheet, rows, summary=summary)

    run_history = existing_run_history or {}
    run_history[SELECTED_TF] = {"run_start": start_str, "duration_seconds": duration_seconds}
    out_json = {
        "run_history": run_history,
        "data": json_out,
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

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