# ============================================================
#  Tillson3_trade – Optimized for speed
#  Batch downloads & batch LTP (or GOOGLEFINANCE formulas)
#  Corrected Diff formula: IF(Buy, LTP - High, Low - LTP)
#  Trigger Candle Date and Time are separate columns.
#  Booking Profit Status with max profit tracking.
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
import time

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================
#  USER CONFIGURATION – TOGGLES (with clear comments)
# ============================================================

SELECTED_TF = "1d"            # Timeframe: 5m, 15m, 1h, 4h, 1d, 1w, 1wk
USE_HEIKIN_ASHI = True         # Use Heikin‑Ashi candles (smoothed)
INCLUDE_LTP = True             # Use real‑time LTP for order decisions (if False, uses last close)
CASE_A_MARKET_ORDER = True     # If trigger already happened: True = Market Order, False = "gap"
ENABLE_ZERODHA_ACTION = False  # Master toggle – if False, no pending orders are written at all
ENABLE_BUY_ORDERS = True       # Allow buy orders to be written (only if ENABLE_ZERODHA_ACTION is True)
ENABLE_SELL_ORDERS = True      # Allow sell orders to be written (only if ENABLE_ZERODHA_ACTION is True)
SHOW_LTP_INFO = True           # Add LTP, Diff, Diff% columns to the sheet
USE_GOOGLEFINANCE = True       # True → write GOOGLEFINANCE formulas; False → use yfinance batch LTP
INVESTMENT_AMOUNT = 5000       # Amount (₹) to use for the "Profit/Loss (₹)" column calculation
PROFIT_TARGET_PERCENT = 5      # Target profit % to book (e.g., 5%)

# ============================================================
#  GOOGLE SHEETS & FILE SETTINGS
# ============================================================

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input5"
OUTPUT_SHEET = f"Tillson3_trade__{SELECTED_TF}"
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

JSON_FILE = f"Tillson3_trade_{SELECTED_TF}.json"           # Tracks trigger (colour) changes
ORDER_SENT_FILE = f"order_sent_tillson3_{SELECTED_TF}.json" # Prevents duplicate orders
PENDING_ORDERS_FILE = "pending_orders.json"                # Shared with other scripts

SKIPPED_SYMBOLS = set()
IST = pytz.timezone("Asia/Kolkata")

CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "1h":  {"interval": "1h",  "period": "90d"},
    "4h":  {"interval": "4h",  "period": "10mo"},
    "1d":  {"interval": "1d",  "period": "10mo"},
    "1w":  {"interval": "1w",  "period": "10mo"},
    "1wk": {"interval": "1wk", "period": "10mo"},
}

# ============================================================
#  TILLSON3 & HELPERS
# ============================================================

def compute_t3(series, length, vfactor):
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
    ha = pd.DataFrame(index=df.index)
    ha['Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4.0
    ha['Open'] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha.loc[ha.index[i], 'Open'] = (ha.loc[ha.index[i-1], 'Open'] + ha.loc[ha.index[i-1], 'Close']) / 2.0
    ha['High'] = pd.concat([df['High'], ha['Open'], ha['Close']], axis=1).max(axis=1)
    ha['Low'] = pd.concat([df['Low'], ha['Open'], ha['Close']], axis=1).min(axis=1)
    return ha[['Open', 'High', 'Low', 'Close']]

def get_t3_colour_and_series(df, length=8, vfactor=0.7):
    typical = (df['High'] + df['Low'] + 2 * df['Close']) / 4
    t3_series = compute_t3(typical, length, vfactor)
    colours = []
    for i in range(len(t3_series)):
        if i == 0:
            colours.append('yellow')
        else:
            if t3_series.iloc[i] > t3_series.iloc[i-1]:
                colours.append('green')
            elif t3_series.iloc[i] < t3_series.iloc[i-1]:
                colours.append('red')
            else:
                colours.append('yellow')
    return t3_series, pd.Series(colours, index=df.index)

def condition_met(row, colour):
    if colour == 'red':
        return (row['Open'] == row['High']) and (row['Low'] < row['High'])
    elif colour == 'green':
        return (row['Open'] == row['Low']) and (row['High'] > row['Low'])
    else:
        return False

def load_order_sent():
    if os.path.exists(ORDER_SENT_FILE):
        with open(ORDER_SENT_FILE) as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set):
    with open(ORDER_SENT_FILE, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=4)

def write_pending_order(symbol, action, level, tf):
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
#  GOOGLE SHEETS FUNCTIONS
# ============================================================

def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        client.timeout = 30
        return client.open(SHEET_NAME)
    except Exception as e:
        print(f"❌ Google Sheets connection failed: {e}")
        sys.exit(1)

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

def write_output_batch(sheet, rows, summary=None, show_ltp_info=False,
                       use_googlefinance=False, investment_amount=5000,
                       target_pct=5):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=30)

    ws.clear()
    start_row = 1
    if summary:
        ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
        start_row = 2

    base_header = [
        "Timestamp", "Symbol", "Timeframe",
        "Current T3 Value", "Current T3 Colour",
        "Valid Candle Open", "Valid Candle High", "Valid Candle Low", "Valid Candle Close",
        "Valid Candle DateTime", "Valid Candle T3", "Trend",
        "Trigger Status", "Trigger DateTime",
        "Zerodha Action", "Trigger Candle Date", "Trigger Candle Time"
    ]
    if show_ltp_info:
        ltp_header = ["LTP", "Diff from Trigger", "Diff %"]
    else:
        ltp_header = []
    profit_columns = [
        "Profit/Loss (₹)",
        "Booking Profit Status",
        "Max Profit % Achieved",
        "Max Profit Value"
    ]
    header = base_header + ltp_header + ["Symbol"] + profit_columns
    ws.update(f"A{start_row}", [header] + rows, value_input_option="USER_ENTERED")

    # If we need to add formulas for LTP info
    if show_ltp_info and use_googlefinance:
        # Get column letters for required columns
        try:
            symbol_col = header.index("Symbol") + 1                    # column B (2)
            trend_col = header.index("Trend") + 1                     # column L (12)
            high_col = header.index("Valid Candle High") + 1          # column G (7)
            low_col = header.index("Valid Candle Low") + 1            # column H (8)
            ltp_col = header.index("LTP") + 1                         # column Q (17) or whatever
            diff_col = header.index("Diff from Trigger") + 1          # column R
            diff_pct_col = header.index("Diff %") + 1                 # column S
        except ValueError:
            return ws, start_row

        # Build batch updates for formulas
        batch_data = []
        for i in range(start_row + 1, start_row + 1 + len(rows)):
            symbol_letter = rowcol_to_a1(1, symbol_col)[0]
            trend_letter = rowcol_to_a1(1, trend_col)[0]
            high_letter = rowcol_to_a1(1, high_col)[0]
            low_letter = rowcol_to_a1(1, low_col)[0]
            ltp_letter = rowcol_to_a1(1, ltp_col)[0]
            diff_letter = rowcol_to_a1(1, diff_col)[0]
            diff_pct_letter = rowcol_to_a1(1, diff_pct_col)[0]

            # LTP formula: =GOOGLEFINANCE("NSE:" & B{row})
            ltp_formula = f'=GOOGLEFINANCE("NSE:" & {symbol_letter}{i})'
            # CORRECTED DIFF: IF(Trend="Buy Trend", LTP - High, Low - LTP)
            diff_formula = f'=IF({trend_letter}{i}="Buy Trend", {ltp_letter}{i} - {high_letter}{i}, {low_letter}{i} - {ltp_letter}{i})'
            # Diff%: IF(Buy, Diff/High, Diff/Low)*100
            diff_pct_formula = f'=IF({trend_letter}{i}="Buy Trend", {diff_letter}{i} / {high_letter}{i}, {diff_letter}{i} / {low_letter}{i}) * 100'

            batch_data.append({"range": f"{ltp_letter}{i}", "values": [[ltp_formula]]})
            batch_data.append({"range": f"{diff_letter}{i}", "values": [[diff_formula]]})
            batch_data.append({"range": f"{diff_pct_letter}{i}", "values": [[diff_pct_formula]]})

        if batch_data:
            ws.batch_update(batch_data, value_input_option='USER_ENTERED')

    # Add Profit/Loss formula (uses Diff column)
    if show_ltp_info:
        try:
            trend_col = header.index("Trend") + 1
            high_col = header.index("Valid Candle High") + 1
            low_col = header.index("Valid Candle Low") + 1
            diff_col = header.index("Diff from Trigger") + 1
            profit_col = header.index("Profit/Loss (₹)") + 1
        except ValueError:
            return ws, start_row

        batch_data = []
        for i in range(start_row + 1, start_row + 1 + len(rows)):
            trend_letter = rowcol_to_a1(1, trend_col)[0]
            high_letter = rowcol_to_a1(1, high_col)[0]
            low_letter = rowcol_to_a1(1, low_col)[0]
            diff_letter = rowcol_to_a1(1, diff_col)[0]
            profit_letter = rowcol_to_a1(1, profit_col)[0]
            formula = f'=IF({trend_letter}{i}="Sell Trend", TRUNC({investment_amount}/{low_letter}{i}), TRUNC({investment_amount}/{high_letter}{i})) * {diff_letter}{i}'
            batch_data.append({"range": f"{profit_letter}{i}", "values": [[formula]]})
        if batch_data:
            ws.batch_update(batch_data, value_input_option='USER_ENTERED')

    return ws, start_row

def apply_color_formatting(ws, header_row, zerodha_actions):
    header = ws.row_values(header_row)
    try:
        col_idx = header.index("Zerodha Action") + 1
    except ValueError:
        return

    batch = []
    for r, action in enumerate(zerodha_actions, start=header_row + 1):
        if action and "Buy" in action:
            color = {"red": 0.72, "green": 0.88, "blue": 0.80}
        elif action and "Sell" in action:
            color = {"red": 0.96, "green": 0.80, "blue": 0.80}
        else:
            color = {"red": 1.0, "green": 1.0, "blue": 1.0}
        batch.append({
            "range": rowcol_to_a1(r, col_idx),
            "format": {"backgroundColor": color}
        })
    if batch:
        ws.batch_format(batch)

# ============================================================
#  BATCH LTP FETCHER (used only when USE_GOOGLEFINANCE is False)
# ============================================================

def get_batch_ltp(symbols):
    """Fetch LTP for multiple symbols in one API call using yf.Tickers."""
    ltp_dict = {}
    try:
        normal_symbols = [s for s in symbols if not s.startswith("^")]
        if normal_symbols:
            tickers = yf.Tickers(" ".join(normal_symbols))
            for sym in normal_symbols:
                try:
                    info = tickers.tickers[sym].info
                    ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                    if ltp:
                        ltp_dict[sym] = round(ltp, 2)
                except:
                    pass
        index_symbols = [s for s in symbols if s.startswith("^")]
        for sym in index_symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                if ltp:
                    ltp_dict[sym] = round(ltp, 2)
            except:
                pass
    except Exception as e:
        print(f"⚠️ Batch LTP fetch failed: {e}")
    return ltp_dict

# ============================================================
#  BATCH DATA DOWNLOADER with retry and auto_adjust=False
# ============================================================

def download_symbol_with_retry(symbol, interval, period, max_retries=2):
    """Download a single symbol with retries and auto_adjust=False."""
    for attempt in range(max_retries):
        try:
            df = yf.download(symbol, interval=interval, period=period,
                             progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"⚠️ Attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(1)
    return None

def download_batch_data(symbols, interval, period):
    normal = [s for s in symbols if not s.startswith("^")]
    index = [s for s in symbols if s.startswith("^")]
    data_dict = {}

    if normal:
        try:
            df = yf.download(normal, interval=interval, period=period,
                             group_by='ticker', progress=False, auto_adjust=False)
            for sym in normal:
                if sym in df.columns.levels[0]:
                    data_dict[sym] = df[sym].dropna()
                else:
                    data_dict[sym] = download_symbol_with_retry(sym, interval, period)
        except Exception as e:
            print(f"⚠️ Batch download failed: {e}")
            for sym in normal:
                data_dict[sym] = download_symbol_with_retry(sym, interval, period)

    for sym in index:
        data_dict[sym] = download_symbol_with_retry(sym, interval, period)

    return data_dict

# ============================================================
#  MAIN
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

    # Load persistent memory
    triggered_info = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            try:
                existing = json.load(f)
                if isinstance(existing, dict):
                    triggered_info = existing.get("triggered_info", {})
            except:
                pass

    order_sent_set = load_order_sent()

    cfg = CONFIG.get(SELECTED_TF)
    if cfg is None:
        print(f"❌ Unsupported timeframe: {SELECTED_TF}")
        sys.exit(1)

    print(f"⏳ Downloading data for {len(symbols)} symbols (batch)...")
    data_dict = download_batch_data(symbols, cfg["interval"], cfg["period"])
    print("✅ Data download complete.")

    # Fetch LTP only if we are NOT using GoogleFinance formulas
    ltp_dict = {}
    if SHOW_LTP_INFO and not USE_GOOGLEFINANCE:
        print("⏳ Fetching LTP for all symbols (batch)...")
        ltp_dict = get_batch_ltp(symbols)
        print("✅ LTP fetch complete.")

    rows = []
    zerodha_actions = []
    new_triggers = 0
    spinner = ["|", "/", "-", "\\"]

    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        df = data_dict.get(symbol)
        if df is None or df.empty:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        df = df.dropna()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if len(df) < 4:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        if USE_HEIKIN_ASHI:
            df = heikin_ashi(df)

        t3_series, colours = get_t3_colour_and_series(df, length=8, vfactor=0.7)

        current_colour = colours.iloc[-2]
        current_t3 = t3_series.iloc[-2]

        change_index = None
        for i in range(len(colours)-2, 0, -1):
            if colours.iloc[i] != colours.iloc[i-1]:
                change_index = i
                break

        trend_label = "Sell Trend" if current_colour == 'red' else "Buy Trend" if current_colour == 'green' else "Inside Trend"

        # Find valid candle
        valid_idx = None
        valid_open = valid_high = valid_low = valid_close = valid_dt = valid_t3 = ""
        if change_index is not None:
            for i in range(change_index, len(df) - 1):
                if condition_met(df.iloc[i], current_colour):
                    valid_idx = i
                    candle = df.iloc[i]
                    valid_open = round(candle["Open"].item(), 2)
                    valid_high = round(candle["High"].item(), 2)
                    valid_low = round(candle["Low"].item(), 2)
                    valid_close = round(candle["Close"].item(), 2)
                    valid_t3 = round(t3_series.iloc[i].item(), 2)
                    dt = pd.to_datetime(df.index[i])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    valid_dt = dt.strftime("%Y-%m-%d %H:%M")
                    break

        # Determine trigger and trigger candle datetime
        trigger_level = None
        trigger_candle_date = ""
        trigger_candle_time = ""
        direction = ""
        trigger_index = None
        if valid_idx is not None and current_colour in ('green', 'red'):
            if current_colour == 'green':
                trigger_level = valid_high
                direction = "buy"
            else:
                trigger_level = valid_low
                direction = "sell"

            for i in range(valid_idx + 1, len(df) - 1):
                if direction == "buy" and df.iloc[i]["High"] > trigger_level:
                    trigger_index = i
                    break
                elif direction == "sell" and df.iloc[i]["Low"] < trigger_level:
                    trigger_index = i
                    break

            if trigger_index is not None:
                dt = pd.to_datetime(df.index[trigger_index])
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC").tz_convert(IST)
                else:
                    dt = dt.tz_convert(IST)
                trigger_candle_date = dt.strftime("%Y-%m-%d")
                trigger_candle_time = dt.strftime("%H:%M")

        # Trigger status & datetime
        key = f"{symbol}_{SELECTED_TF}"
        stored = triggered_info.get(key)
        trigger_status = ""
        trigger_datetime = ""

        if current_colour in ('green', 'red'):
            if stored is None or stored.get('colour') != current_colour:
                trigger_status = "New Trigger"
                trigger_datetime = valid_dt if valid_idx is not None else ""
                triggered_info[key] = {
                    "colour": current_colour,
                    "datetime": trigger_datetime,
                    "t3_value": round(current_t3, 2)
                }
                new_triggers += 1
            else:
                trigger_status = ""
                trigger_datetime = stored.get('datetime', '') if stored else ""

        # Zerodha Action
        zerodha_action = ""
        if valid_idx is not None and current_colour in ('green', 'red') and trigger_level is not None:
            order_key = (symbol, trigger_level, direction)
            if order_key in order_sent_set:
                zerodha_action = "Already buy order sent" if direction == "buy" else "Already sell order sent"
            else:
                trigger_happened = trigger_index is not None
                current_ltp = None
                if not trigger_happened:
                    if USE_GOOGLEFINANCE:
                        current_ltp = df.iloc[-2]["Close"].item()
                    else:
                        current_ltp = ltp_dict.get(symbol)

                if trigger_happened:
                    if CASE_A_MARKET_ORDER:
                        zerodha_action = "Buy Market Order" if direction == "buy" else "Sell Market Order"
                    else:
                        zerodha_action = "Buy Long gap" if direction == "buy" else "Sell gap"
                    order_sent_set.add(order_key)
                else:
                    if current_ltp is None:
                        current_ltp = df.iloc[-2]["Close"].item()
                    if direction == "buy":
                        zerodha_action = "Buy Market Order" if current_ltp > trigger_level else f"Buy Limit Order (BO close {trigger_level})"
                    else:
                        zerodha_action = "Sell Market Order" if current_ltp < trigger_level else f"Sell Limit Order (BO close {trigger_level})"
                    order_sent_set.add(order_key)

                if zerodha_action and "Already" not in zerodha_action and "gap" not in zerodha_action:
                    write_pending_order(symbol, zerodha_action, trigger_level, SELECTED_TF)

        # ----- Booking Profit Status -----
        booking_status = ""
        max_profit_pct = 0.0
        max_profit_value = 0.0

        if trigger_index is not None and direction in ('buy', 'sell'):
            entry_price = trigger_level
            # Get the prices from trigger_index to last completed candle (index -2)
            # For Buy: use High; for Sell: use Low
            if direction == 'buy':
                prices = df.iloc[trigger_index : len(df)-1]['High']
                # current price: use last close
                current_price = df.iloc[-2]['Close']
            else:  # sell
                prices = df.iloc[trigger_index : len(df)-1]['Low']
                current_price = df.iloc[-2]['Close']

            # Compute max profit percentage
            if direction == 'buy':
                max_price = prices.max()
                max_profit_pct = round((max_price - entry_price) / entry_price * 100, 2)
                current_profit_pct = round((current_price - entry_price) / entry_price * 100, 2)
                max_profit_value = round(max_price - entry_price, 2)
            else:  # sell
                min_price = prices.min()
                max_profit_pct = round((entry_price - min_price) / entry_price * 100, 2)
                current_profit_pct = round((entry_price - current_price) / entry_price * 100, 2)
                max_profit_value = round(entry_price - min_price, 2)

            # Determine status
            if max_profit_pct >= PROFIT_TARGET_PERCENT:
                booking_status = f"Profit booked at {PROFIT_TARGET_PERCENT}%"
            elif max_profit_pct > 0 and current_profit_pct > 0:
                booking_status = f"Profit in progress of {max_profit_pct:.1f}%"
            elif max_profit_pct > 0 and current_profit_pct <= 0:
                booking_status = f"Booked maximum profit of {max_profit_pct:.1f}%"
            else:
                booking_status = "No profit yet"

            # For display, we want max_profit_pct as numeric and max_profit_value
            # Already computed.
        else:
            # No trade opened
            booking_status = ""
            max_profit_pct = ""
            max_profit_value = ""

        # LTP info – static values only if not using GoogleFinance
        ltp = ""
        diff = ""
        diff_pct = ""
        if SHOW_LTP_INFO and not USE_GOOGLEFINANCE:
            if valid_idx is not None and current_colour in ('green', 'red') and trigger_level is not None:
                ltp_val = ltp_dict.get(symbol)
                if ltp_val:
                    ltp = ltp_val
                    if current_colour == 'green':
                        diff = round(ltp - trigger_level, 2)
                        diff_pct = round((diff / trigger_level) * 100, 2)
                    else:
                        diff = round(trigger_level - ltp, 2)
                        diff_pct = round((diff / trigger_level) * 100, 2)

        display_symbol = symbol.replace('.NS', '')
        base_row = [
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
            trigger_candle_date,
            trigger_candle_time
        ]
        if SHOW_LTP_INFO:
            base_row.extend([ltp, diff, diff_pct])
        base_row.append(display_symbol)
        # Append the profit-related columns: Profit/Loss (₹) placeholder, status, max_pct, max_value
        # We'll add placeholder for Profit/Loss formula; status and max values.
        base_row.append("")  # Profit/Loss (₹) will be formula
        base_row.append(booking_status)
        base_row.append(max_profit_pct)
        base_row.append(max_profit_value)

        rows.append(base_row)
        zerodha_actions.append(zerodha_action)

    save_order_sent(order_sent_set)

    out_json = {
        "run_start": start_str,
        "duration": f"{(datetime.now(IST) - start_time).total_seconds():.1f}s",
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

    summary = (
        f"Timeframe: {SELECTED_TF} | Run start: {start_str} | "
        f"Duration: {out_json['duration']} | Total: {total} | New Triggers: {new_triggers} | "
        f"Skipped: {len(SKIPPED_SYMBOLS)} | Heikin Ashi: {USE_HEIKIN_ASHI} | "
        f"Zerodha: {ENABLE_ZERODHA_ACTION} | Buy orders: {ENABLE_BUY_ORDERS} | Sell orders: {ENABLE_SELL_ORDERS} | "
        f"LTP info: {SHOW_LTP_INFO} | GoogleFinance: {USE_GOOGLEFINANCE} | Investment: ₹{INVESTMENT_AMOUNT} | Target Profit: {PROFIT_TARGET_PERCENT}%"
    )
    if rows:
        ws, header_row = write_output_batch(sheet, rows, summary=summary,
                                            show_ltp_info=SHOW_LTP_INFO,
                                            use_googlefinance=USE_GOOGLEFINANCE,
                                            investment_amount=INVESTMENT_AMOUNT,
                                            target_pct=PROFIT_TARGET_PERCENT)
        apply_color_formatting(ws, header_row, zerodha_actions)

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
    print(f"Show LTP info      : {SHOW_LTP_INFO}")
    print(f"Use GoogleFinance  : {USE_GOOGLEFINANCE}")
    print(f"Investment Amount  : ₹{INVESTMENT_AMOUNT}")
    print(f"Target Profit %    : {PROFIT_TARGET_PERCENT}%")
    print("────────────────────────────")
    print("✅ Sheet, JSON & order files updated successfully")

if __name__ == "__main__":
    main()