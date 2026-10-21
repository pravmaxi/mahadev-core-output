# ============================================================
#  Tillson3_trade – Automated Trading Signal Generator
#  Based on the Tillson T3 indicator (from TradingView)
#  This script scans a list of symbols, computes the T3 line,
#  finds trend changes, detects breakout/trigger candles,
#  and suggests Zerodha orders.
# ============================================================

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
from gspread.utils import rowcol_to_a1

# Suppress pandas deprecation warnings (cleaner output)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================
#  USER CONFIGURATION – change these to suit your needs
# ============================================================

# Timeframe for analysis (e.g., "5m", "15m", "1h", "1d", "1w", "1wk")
SELECTED_TF = "15m"   # changed to test weekly

# Whether to use Heikin‑Ashi candles (smoothed) instead of standard OHLC
USE_HEIKIN_ASHI = True

# Include the current live price (LTP) when deciding orders?
# If False, the last completed close is used.
INCLUDE_LTP = True

# If the trigger has already occurred, should we recommend a Market Order (True)
# or a "gap" order (False)?
CASE_A_MARKET_ORDER = True

# Master toggle: if False, no pending orders are written at all (only shown on sheet)
ENABLE_ZERODHA_ACTION = False

# Sub‑toggles: allow buy orders and/or sell orders to be written to pending_orders.json
ENABLE_BUY_ORDERS = True
ENABLE_SELL_ORDERS = True

# Google Sheets settings
SHEET_NAME = "Stock Price Scraper"               # Name of the spreadsheet
INPUT_SHEET = "input2"                          # Sheet containing symbols (column A)
OUTPUT_SHEET = f"Tillson3_trade__{SELECTED_TF}" # Where results are written
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"  # Service account key

# File names for persistent storage
JSON_FILE = f"Tillson3_trade_{SELECTED_TF}.json"           # Tracks trigger (colour) changes
ORDER_SENT_FILE = f"order_sent_tillson3_{SELECTED_TF}.json" # Prevents duplicate orders
PENDING_ORDERS_FILE = "pending_orders.json"                # Shared with other scripts

# Global set to collect symbols that failed to download
SKIPPED_SYMBOLS = set()

# Timezone (Indian Standard Time)
IST = pytz.timezone("Asia/Kolkata")

# Data download configuration per timeframe
# Includes both "1w" and "1wk" for flexibility
CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "1h":  {"interval": "1h",  "period": "90d"},
    "4h":  {"interval": "4h",  "period": "10mo"},
    "1d":  {"interval": "1d",  "period": "10mo"},
    "1w":  {"interval": "1w",  "period": "10mo"},
    "1wk": {"interval": "1wk", "period": "10mo"},   # alias for weekly
}

# ============================================================
#  TILLSON T3 INDICATOR & HEIKIN‑ASHI FUNCTIONS
# ============================================================

def compute_t3(series, length, vfactor):
    """
    Compute the Tillson T3 moving average for a pandas Series.
    The formula is exactly as in the TradingView code:
      e1 = EMA(series, length)
      e2 = EMA(e1, length)
      ...
      T3 = c1*e6 + c2*e5 + c3*e4 + c4*e3
    where the coefficients c1..c4 depend on vfactor.
    """
    e1 = series.ewm(span=length, adjust=False).mean()
    e2 = e1.ewm(span=length, adjust=False).mean()
    e3 = e2.ewm(span=length, adjust=False).mean()
    e4 = e3.ewm(span=length, adjust=False).mean()
    e5 = e4.ewm(span=length, adjust=False).mean()
    e6 = e5.ewm(span=length, adjust=False).mean()

    c1 = -vfactor ** 3
    c2 = 3 * (vfactor ** 2) + 3 * (vfactor ** 3)
    c3 = -6 * (vfactor ** 2) - 3 * vfactor - 3 * (vfactor ** 3)
    c4 = 1 + 3 * vfactor + (vfactor ** 3) + 3 * (vfactor ** 2)

    return c1 * e6 + c2 * e5 + c3 * e4 + c4 * e3

def heikin_ashi(df):
    """
    Convert a standard OHLC DataFrame to Heikin‑Ashi candles.
    Heikin‑Ashi uses average prices to smooth out noise.
    """
    ha = pd.DataFrame(index=df.index)
    # Heikin‑Ashi Close = (O+H+L+C)/4
    ha['Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4.0
    # First Open = (first Open + first Close)/2
    ha['Open'] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2.0
    # Subsequent Opens = (previous Open + previous Close)/2
    for i in range(1, len(df)):
        ha.loc[ha.index[i], 'Open'] = (ha.loc[ha.index[i-1], 'Open'] + ha.loc[ha.index[i-1], 'Close']) / 2.0
    # High = max(High, Open, Close)
    ha['High'] = pd.concat([df['High'], ha['Open'], ha['Close']], axis=1).max(axis=1)
    # Low = min(Low, Open, Close)
    ha['Low'] = pd.concat([df['Low'], ha['Open'], ha['Close']], axis=1).min(axis=1)
    return ha[['Open', 'High', 'Low', 'Close']]

def get_t3_colour_and_series(df, length=8, vfactor=0.7):
    """
    Compute the T3 series and a colour series for each candle.
    Colour is:
      - green if T3 is rising (T3 > previous T3)
      - red if T3 is falling
      - yellow if unchanged
    Returns (t3_series, colour_series).
    """
    # Typical price = (H+L+2*C)/4 (as used in TradingView code)
    typical = (df['High'] + df['Low'] + 2 * df['Close']) / 4
    t3_series = compute_t3(typical, length, vfactor)

    colours = []
    for i in range(len(t3_series)):
        if i == 0:
            colours.append('yellow')   # no previous value
        else:
            if t3_series.iloc[i] > t3_series.iloc[i-1]:
                colours.append('green')
            elif t3_series.iloc[i] < t3_series.iloc[i-1]:
                colours.append('red')
            else:
                colours.append('yellow')
    return t3_series, pd.Series(colours, index=df.index)

def condition_met(row, colour):
    """
    Check if a candle meets the required shape for the given colour.
    For red (Sell)  : Open == High and Low < High  (bearish candle)
    For green (Buy) : Open == Low  and High > Low  (bullish candle)
    This is used to validate the "breakout" candle.
    """
    if colour == 'red':
        return (row['Open'] == row['High']) and (row['Low'] < row['High'])
    elif colour == 'green':
        return (row['Open'] == row['Low']) and (row['High'] > row['Low'])
    else:
        return False

# ============================================================
#  GOOGLE SHEETS INTEGRATION
# ============================================================

def connect_gsheet():
    """Authenticate with Google Sheets using service account and return the spreadsheet object."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        client.timeout = 30   # avoid hanging
        return client.open(SHEET_NAME)
    except Exception as e:
        print(f"❌ Google Sheets connection failed: {e}")
        sys.exit(1)

def read_symbols(sheet):
    """Read symbols from the input sheet (first column)."""
    ws = sheet.worksheet(INPUT_SHEET)
    raw_symbols = [s.strip().upper() for s in ws.col_values(1) if s.strip()]
    processed = []
    for s in raw_symbols:
        # Keep index symbols (^) and multi‑symbol formats (with ':') as‑is
        if s.startswith("^") or ":" in s:
            processed.append(s)
        else:
            processed.append(f"{s}.NS")   # NSE suffix for yfinance
    return processed

def write_output_batch(sheet, rows, summary=None):
    """
    Write the analysis rows to the output sheet.
    Returns the worksheet object and the row number where the header is placed.
    """
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=20)

    ws.clear()  # remove old data
    start_row = 1
    if summary:
        ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
        start_row = 2

    # Define the header columns
    header = [
        "Timestamp", "Symbol", "Timeframe",
        "Current T3 Value", "Current T3 Colour",
        "Valid Candle Open", "Valid Candle High", "Valid Candle Low", "Valid Candle Close",
        "Valid Candle DateTime", "Valid Candle T3", "Trend",
        "Trigger Status", "Trigger DateTime",
        "Zerodha Action", "Trigger Candle DateTime", "Symbol"
    ]
    ws.update(f"A{start_row}", [header] + rows, value_input_option="USER_ENTERED")
    return ws, start_row

def apply_color_formatting(ws, header_row, zerodha_actions):
    """
    Apply background colours to the 'Zerodha Action' column.
    Uses the provided list of actions (no extra sheet reads).
    This avoids API quota issues.
    """
    # Locate the column index of "Zerodha Action"
    header = ws.row_values(header_row)
    try:
        col_idx = header.index("Zerodha Action") + 1   # 1‑based for gspread
    except ValueError:
        return

    batch = []
    for r, action in enumerate(zerodha_actions, start=header_row + 1):
        if not action:
            continue
        if "Buy" in action:
            color = {"red": 0.72, "green": 0.88, "blue": 0.80}   # light green
        elif "Sell" in action:
            color = {"red": 0.96, "green": 0.80, "blue": 0.80}   # light red
        else:
            continue
        batch.append({
            "range": rowcol_to_a1(r, col_idx),
            "format": {"backgroundColor": color}
        })

    if batch:
        ws.batch_format(batch)

# ============================================================
#  ORDER AND PRICE HELPERS
# ============================================================

def get_current_ltp(symbol):
    """Fetch the current Last Traded Price from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return info.get('regularMarketPrice') or info.get('currentPrice')
    except:
        return None

def load_order_sent():
    """Load the set of already‑sent orders from the order_sent JSON file."""
    if os.path.exists(ORDER_SENT_FILE):
        with open(ORDER_SENT_FILE) as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set):
    """Save the order‑sent set back to JSON."""
    with open(ORDER_SENT_FILE, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=4)

def write_pending_order(symbol, action, level, tf):
    """
    Append a pending order to the shared pending_orders.json file.
    Respects the three toggles: ENABLE_ZERODHA_ACTION, ENABLE_BUY_ORDERS, ENABLE_SELL_ORDERS.
    """
    if not ENABLE_ZERODHA_ACTION:
        return
    if "Buy" in action and not ENABLE_BUY_ORDERS:
        return
    if "Sell" in action and not ENABLE_SELL_ORDERS:
        return

    pending = {
        "symbol": symbol,
        "action": action,
        "level": level,
        "timeframe": tf,
        "timestamp": datetime.now(IST).isoformat()
    }
    try:
        if os.path.exists(PENDING_ORDERS_FILE):
            with open(PENDING_ORDERS_FILE, 'r') as f:
                data = json.load(f)
        else:
            data = []
        data.append(pending)
        with open(PENDING_ORDERS_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ Could not write to {PENDING_ORDERS_FILE}: {e}")

# ============================================================
#  MAIN SCRIPT
# ============================================================

def main():
    print("⏳ Connecting to Google Sheets...")
    sheet = connect_gsheet()
    print("✅ Connected.")

    symbols = read_symbols(sheet)
    total = len(symbols)
    print(f"📊 Found {total} symbols in {INPUT_SHEET}")

    start_time = datetime.now(IST)
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S %Z")

    # ------------------------------------------------------------
    #  Load persistent memory from the JSON file
    # ------------------------------------------------------------
    # triggered_info stores the last known colour for each symbol+timeframe.
    # This is used to detect colour changes (new triggers).
    triggered_info = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            try:
                existing = json.load(f)
                if isinstance(existing, dict):
                    triggered_info = existing.get("triggered_info", {})
            except:
                pass

    # Load the set of already sent orders (prevents duplicate recommendations)
    order_sent_set = load_order_sent()

    # Prepare data structures for the sheet output
    rows = []
    zerodha_actions = []   # store actions separately for colour formatting without extra reads
    new_triggers = 0
    spinner = ["|", "/", "-", "\\"]

    # ------------------------------------------------------------
    #  Process each symbol
    # ------------------------------------------------------------
    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        # Download historical data for the chosen timeframe
        cfg = CONFIG.get(SELECTED_TF)
        if cfg is None:
            print(f"\n❌ Unsupported timeframe: {SELECTED_TF}")
            sys.exit(1)

        try:
            df = yf.download(symbol, interval=cfg["interval"], period=cfg["period"],
                             progress=False, timeout=10)
        except Exception as e:
            print(f"\n⚠️ Download failed for {symbol}: {e}")
            SKIPPED_SYMBOLS.add(symbol)
            continue

        if df is None or df.empty:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        df = df.dropna()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Need at least 4 candles for a valid forward scan
        if len(df) < 4:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        # Apply Heikin‑Ashi if enabled
        if USE_HEIKIN_ASHI:
            df = heikin_ashi(df)

        # Compute T3 series and colours for every candle
        t3_series, colours = get_t3_colour_and_series(df, length=8, vfactor=0.7)

        # The most recent completed candle is at index -2 (the last row is the incomplete one)
        current_colour = colours.iloc[-2]
        current_t3 = t3_series.iloc[-2]

        # Find the most recent colour change (first candle where colour is current)
        change_index = None
        for i in range(len(colours)-2, 0, -1):
            if colours.iloc[i] != colours.iloc[i-1]:
                change_index = i   # this is the first candle of the current colour
                break

        # Determine the trend label based on colour
        if current_colour == 'red':
            trend_label = "Sell Trend"
        elif current_colour == 'green':
            trend_label = "Buy Trend"
        else:
            trend_label = "Inside Trend"

        # ------------------------------------------------------------
        #  Find the 'valid candle' – the first candle (forward from change)
        #  that satisfies the bullish/bearish shape condition.
        # ------------------------------------------------------------
        valid_idx = None
        valid_open = valid_high = valid_low = valid_close = valid_dt = valid_t3 = ""
        if change_index is not None:
            for i in range(change_index, len(df) - 1):   # up to the last completed candle
                if condition_met(df.iloc[i], current_colour):
                    valid_idx = i
                    candle = df.iloc[i]
                    valid_open = round(candle["Open"].item(), 2)
                    valid_high = round(candle["High"].item(), 2)
                    valid_low = round(candle["Low"].item(), 2)
                    valid_close = round(candle["Close"].item(), 2)
                    valid_t3 = round(t3_series.iloc[i].item(), 2)
                    # Convert datetime to IST
                    dt = pd.to_datetime(df.index[i])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    valid_dt = dt.strftime("%Y-%m-%d %H:%M")
                    break

        # ------------------------------------------------------------
        #  Trigger detection (colour change) – uses triggered_info
        # ------------------------------------------------------------
        key = f"{symbol}_{SELECTED_TF}"
        stored = triggered_info.get(key)
        trigger_status = ""
        trigger_datetime = ""

        if current_colour in ('green', 'red'):
            # Only colours that can be traded trigger a status
            if stored is None or stored.get('colour') != current_colour:
                trigger_status = "New Trigger"
                trigger_datetime = valid_dt if valid_idx is not None else ""
                # Update the stored colour and datetime
                triggered_info[key] = {
                    "colour": current_colour,
                    "datetime": trigger_datetime,
                    "t3_value": round(current_t3, 2)
                }
                new_triggers += 1
            else:
                trigger_status = ""
                trigger_datetime = ""

        # ------------------------------------------------------------
        #  Zerodha Action – decide market/limit order
        # ------------------------------------------------------------
        zerodha_action = ""
        trigger_candle_dt = ""   # datetime of the candle that first crossed the trigger level

        if valid_idx is not None and current_colour in ('green', 'red'):
            # The trigger level is the High (Buy) or Low (Sell) of the valid candle
            if current_colour == 'green':
                trigger_level = valid_high
                direction = "buy"
            else:   # red
                trigger_level = valid_low
                direction = "sell"

            # Check if this (symbol, trigger_level, direction) was already sent
            order_key = (symbol, trigger_level, direction)
            if order_key in order_sent_set:
                zerodha_action = "Already buy order sent" if direction == "buy" else "Already sell order sent"
            else:
                # Scan forward from valid_idx+1 to see if any candle has crossed the trigger level
                trigger_happened = False
                trigger_index = None
                for i in range(valid_idx + 1, len(df) - 1):
                    if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                        trigger_happened = True
                        trigger_index = i
                        break
                    elif direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                        trigger_happened = True
                        trigger_index = i
                        break

                # If INCLUDE_LTP and trigger not yet found, check current price
                current_ltp = None
                if INCLUDE_LTP and not trigger_happened:
                    current_ltp = get_current_ltp(symbol)

                if trigger_happened:
                    # Capture the datetime of the trigger candle
                    dt = pd.to_datetime(df.index[trigger_index])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    trigger_candle_dt = dt.strftime("%Y-%m-%d %H:%M")

                    # Case A: trigger already occurred → use setting
                    if CASE_A_MARKET_ORDER:
                        zerodha_action = "Buy Market Order" if direction == "buy" else "Sell Market Order"
                    else:
                        zerodha_action = "Buy Long gap" if direction == "buy" else "Sell gap"
                    # Mark as sent to avoid future duplicates
                    order_sent_set.add(order_key)
                else:
                    # Case B/C: trigger not yet happened → decide based on current price
                    if current_ltp is None:
                        # fallback to the last completed candle's close
                        current_ltp = df.iloc[-2]["Close"].item()

                    if direction == "buy":
                        if current_ltp > trigger_level:
                            zerodha_action = "Buy Market Order"
                        else:
                            zerodha_action = f"Buy Limit Order (BO close {trigger_level})"
                    else:  # sell
                        if current_ltp < trigger_level:
                            zerodha_action = "Sell Market Order"
                        else:
                            zerodha_action = f"Sell Limit Order (BO close {trigger_level})"
                    # Mark as sent (so we don't recommend again for same level)
                    order_sent_set.add(order_key)

                # Write to pending orders if action is not "Already" or "gap"
                if zerodha_action and "Already" not in zerodha_action and "gap" not in zerodha_action:
                    write_pending_order(symbol, zerodha_action, trigger_level, SELECTED_TF)

        # ------------------------------------------------------------
        #  Build the row for the Google Sheet
        # ------------------------------------------------------------
        display_symbol = symbol.replace('.NS', '')
        row = [
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            display_symbol,
            SELECTED_TF,
            round(current_t3, 2),
            current_colour,
            valid_open if valid_idx is not None else "",
            valid_high if valid_idx is not None else "",
            valid_low if valid_idx is not None else "",
            valid_close if valid_idx is not None else "",
            valid_dt if valid_idx is not None else "",
            valid_t3 if valid_idx is not None else "",
            trend_label,
            trigger_status,
            trigger_datetime,
            zerodha_action,
            trigger_candle_dt,   # this is different from valid_dt – shows actual trigger breach
            display_symbol   # duplicate symbol for easier reading when scrolling
        ]
        rows.append(row)
        zerodha_actions.append(zerodha_action)   # store for colour formatting

    # ------------------------------------------------------------
    #  Save persistent state (order_sent and triggered_info)
    # ------------------------------------------------------------
    save_order_sent(order_sent_set)

    out_json = {
        "run_start": start_str,
        "duration": f"{(datetime.now(IST) - start_time).total_seconds():.1f}s",
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

    # ------------------------------------------------------------
    #  Write to Google Sheet and apply colour formatting
    # ------------------------------------------------------------
    summary = (
        f"Timeframe: {SELECTED_TF} | Run start: {start_str} | "
        f"Duration: {out_json['duration']} | Total: {total} | New Triggers: {new_triggers} | "
        f"Skipped: {len(SKIPPED_SYMBOLS)} | Heikin Ashi: {USE_HEIKIN_ASHI} | "
        f"Zerodha: {ENABLE_ZERODHA_ACTION} | Buy orders: {ENABLE_BUY_ORDERS} | Sell orders: {ENABLE_SELL_ORDERS}"
    )
    if rows:
        ws, header_row = write_output_batch(sheet, rows, summary=summary)
        # Colour the Zerodha Action column using the stored actions (no extra reads)
        apply_color_formatting(ws, header_row, zerodha_actions)

    # ------------------------------------------------------------
    #  Print summary to console
    # ------------------------------------------------------------
    print("\n\n📊 RUN SUMMARY")
    print("────────────────────────────")
    print(f"Timeframe          : {SELECTED_TF}")
    print(f"Heikin Ashi        : {USE_HEIKIN_ASHI}")
    print(f"Run start          : {start_str}")
    print(f"Duration           : {out_json['duration']}")
    print(f"Total Symbols      : {total}")
    print(f"New Triggers       : {new_triggers}")
    print(f"Skipped Symbols    : {len(SKIPPED_SYMBOLS)}")
    print(f"Zerodha Action     : {ENABLE_ZERODHA_ACTION}")
    print(f"Buy Orders allowed : {ENABLE_BUY_ORDERS}")
    print(f"Sell Orders allowed: {ENABLE_SELL_ORDERS}")
    print("────────────────────────────")
    print("✅ Sheet, JSON & order files updated successfully")

if __name__ == "__main__":
    main()