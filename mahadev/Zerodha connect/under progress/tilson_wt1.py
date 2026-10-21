# ============================================================
#  Tillson3 + WaveTrend Trade Script
#  FIX: WT direction_wt set regardless of trigger detection
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
#  USER CONFIGURATION – TOGGLES
# ============================================================

SELECTED_TF = "1h"
USE_HEIKIN_ASHI = True
INCLUDE_LTP = True          # Fetch LTP via yfinance
CASE_A_MARKET_ORDER = True
ENABLE_ZERODHA_ACTION = False
ENABLE_BUY_ORDERS = True
ENABLE_SELL_ORDERS = True
INVESTMENT_AMOUNT = 5000
PROFIT_TARGET_PERCENT = 5

# ============================================================
#  GOOGLE SHEETS & FILE SETTINGS
# ============================================================

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = f"Tillson3_trade__{SELECTED_TF}"
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

JSON_FILE = f"Tillson3_trade_{SELECTED_TF}.json"
ORDER_SENT_FILE_T3 = f"order_sent_tillson3_{SELECTED_TF}.json"
ORDER_SENT_FILE_WT = f"order_sent_wavetrend_{SELECTED_TF}.json"
PENDING_ORDERS_FILE = "pending_orders.json"

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
#  INDICATOR FUNCTIONS (unchanged)
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

def compute_wavetrend(df, n1=15, n2=27):
    ap = (df['High'] + df['Low'] + df['Close']) / 3
    esa = ap.ewm(span=n1, adjust=False).mean()
    d = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
    ci = (ap - esa) / (0.015 * d)
    tci = ci.ewm(span=n2, adjust=False).mean()
    wt1 = tci
    wt2 = wt1.rolling(window=4).mean()
    wt_diff = wt1 - wt2
    cross_signal = pd.Series(index=df.index, dtype='object')
    for i in range(1, len(wt1)):
        if wt1.iloc[i-1] < wt2.iloc[i-1] and wt1.iloc[i] > wt2.iloc[i]:
            cross_signal.iloc[i] = "Bull Cross"
        elif wt1.iloc[i-1] > wt2.iloc[i-1] and wt1.iloc[i] < wt2.iloc[i]:
            cross_signal.iloc[i] = "Bear Cross"
        else:
            cross_signal.iloc[i] = "No Cross"
    cross_signal.iloc[0] = "No Cross"
    return wt1, wt2, wt_diff, cross_signal

# ============================================================
#  ORDER HELPERS
# ============================================================

def load_order_sent(filepath):
    if os.path.exists(filepath):
        with open(filepath) as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(filepath, order_set):
    with open(filepath, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=4)

def write_pending_order(symbol, action, level, tf, prefix=""):
    if not ENABLE_ZERODHA_ACTION:
        return
    if "Buy" in action and not ENABLE_BUY_ORDERS:
        return
    if "Sell" in action and not ENABLE_SELL_ORDERS:
        return
    pending = {
        "symbol": symbol,
        "action": f"{prefix}{action}",
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

def write_output_batch(sheet, rows, summary=None, investment_amount=5000):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=60)

    ws.clear()
    start_row = 1
    if summary:
        ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
        start_row = 2

    header = [
        "Timestamp", "Symbol", "Timeframe",
        "Current T3 Value", "Current T3 Colour",
        "Valid Candle Open", "Valid Candle High", "Valid Candle Low", "Valid Candle Close",
        "Valid Candle DateTime", "Valid Candle T3", "Trend",
        "Trigger Status", "Trigger DateTime",
        "Zerodha Action", "Trigger Candle Date", "Trigger Candle Time",
        "LTP", "Diff from Trigger", "Diff %",
        "Symbol", "Profit/Loss (₹)",
        "T3 Booking Profit Status", "T3 Max Profit % Achieved", "T3 Max Profit Value", "T3 Empty",
        "WT1", "WT2", "WT Diff (WT1-WT2)", "WT Cross", "WT Trend",
        "WT Valid Candle Open", "WT Valid Candle High", "WT Valid Candle Low",
        "WT Valid Candle Close", "WT Valid Candle DateTime", "WT Valid Candle Diff",
        "WT Valid Candle WT Value",
        "WT Perfect/Imperfect", "WT Trigger Status", "WT Trigger DateTime",
        "WT Zerodha Action", "WT Trigger Candle Date", "WT Trigger Candle Time",
        "WT LTP", "WT Diff from Trigger", "WT Diff %",
        "Symbol", "WT Profit/Loss (₹)",
        "WT Booking Profit Status", "WT Max Profit % Achieved", "WT Max Profit Value", "WT Empty"
    ]

    ws.update(f"A{start_row}", [header] + rows, value_input_option="USER_ENTERED")
    return ws, start_row

def apply_color_formatting(ws, header_row, zerodha_actions_t3, zerodha_actions_wt):
    header = ws.row_values(header_row)
    try:
        col_t3 = header.index("Zerodha Action") + 1
    except ValueError:
        col_t3 = None
    try:
        col_wt = header.index("WT Zerodha Action") + 1
    except ValueError:
        col_wt = None

    batch = []
    if col_t3 is not None:
        for r, action in enumerate(zerodha_actions_t3, start=header_row + 1):
            if action and "Buy" in action:
                color = {"red": 0.72, "green": 0.88, "blue": 0.80}
            elif action and "Sell" in action:
                color = {"red": 0.96, "green": 0.80, "blue": 0.80}
            else:
                color = {"red": 1.0, "green": 1.0, "blue": 1.0}
            batch.append({"range": rowcol_to_a1(r, col_t3), "format": {"backgroundColor": color}})
    if col_wt is not None:
        for r, action in enumerate(zerodha_actions_wt, start=header_row + 1):
            if action and "Buy" in action:
                color = {"red": 0.72, "green": 0.88, "blue": 0.80}
            elif action and "Sell" in action:
                color = {"red": 0.96, "green": 0.80, "blue": 0.80}
            else:
                color = {"red": 1.0, "green": 1.0, "blue": 1.0}
            batch.append({"range": rowcol_to_a1(r, col_wt), "format": {"backgroundColor": color}})
    if batch:
        ws.batch_format(batch)

# ============================================================
#  DATA FETCHERS
# ============================================================

def get_batch_ltp(symbols):
    ltp_dict = {}
    try:
        normal = [s for s in symbols if not s.startswith("^")]
        if normal:
            tickers = yf.Tickers(" ".join(normal))
            for sym in normal:
                try:
                    info = tickers.tickers[sym].info
                    ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                    if ltp:
                        ltp_dict[sym] = round(ltp, 2)
                except:
                    pass
        for sym in symbols:
            if sym.startswith("^"):
                try:
                    info = yf.Ticker(sym).info
                    ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                    if ltp:
                        ltp_dict[sym] = round(ltp, 2)
                except:
                    pass
    except Exception as e:
        print(f"⚠️ Batch LTP fetch failed: {e}")
    return ltp_dict

def download_symbol_with_retry(symbol, interval, period, max_retries=2):
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

    triggered_info = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            try:
                existing = json.load(f)
                if isinstance(existing, dict):
                    triggered_info = existing.get("triggered_info", {})
            except:
                pass

    order_sent_t3 = load_order_sent(ORDER_SENT_FILE_T3)
    order_sent_wt = load_order_sent(ORDER_SENT_FILE_WT)

    cfg = CONFIG.get(SELECTED_TF)
    if cfg is None:
        print(f"❌ Unsupported timeframe: {SELECTED_TF}")
        sys.exit(1)

    print(f"⏳ Downloading data for {len(symbols)} symbols (batch)...")
    data_dict = download_batch_data(symbols, cfg["interval"], cfg["period"])
    print("✅ Data download complete.")

    ltp_dict = {}
    if INCLUDE_LTP:
        print("⏳ Fetching LTP for all symbols (batch)...")
        ltp_dict = get_batch_ltp(symbols)
        print("✅ LTP fetch complete.")
    else:
        print("ℹ️ LTP fetch skipped (INCLUDE_LTP=False). Using last close for LTP columns.")

    rows = []
    zerodha_actions_t3 = []
    zerodha_actions_wt = []
    new_triggers_t3 = 0
    new_triggers_wt = 0
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

        # =====================================================
        #  T3 INDICATOR (unchanged)
        # =====================================================
        t3_series, colours = get_t3_colour_and_series(df, length=8, vfactor=0.7)
        current_colour = colours.iloc[-2]
        current_t3 = t3_series.iloc[-2]

        change_index = None
        for i in range(len(colours)-2, 0, -1):
            if colours.iloc[i] != colours.iloc[i-1]:
                change_index = i
                break

        trend_label = "Sell Trend" if current_colour == 'red' else "Buy Trend" if current_colour == 'green' else "Inside Trend"

        valid_idx_t3 = None
        valid_open_t3 = valid_high_t3 = valid_low_t3 = valid_close_t3 = valid_dt_t3 = valid_t3_val = ""
        if change_index is not None:
            for i in range(change_index, len(df) - 1):
                if condition_met(df.iloc[i], current_colour):
                    valid_idx_t3 = i
                    candle = df.iloc[i]
                    valid_open_t3 = round(candle["Open"].item(), 2)
                    valid_high_t3 = round(candle["High"].item(), 2)
                    valid_low_t3 = round(candle["Low"].item(), 2)
                    valid_close_t3 = round(candle["Close"].item(), 2)
                    valid_t3_val = round(t3_series.iloc[i].item(), 2)
                    dt = pd.to_datetime(df.index[i])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    valid_dt_t3 = dt.strftime("%Y-%m-%d %H:%M")
                    break

        trigger_level_t3 = None
        trigger_candle_date_t3 = ""
        trigger_candle_time_t3 = ""
        direction_t3 = ""
        trigger_index_t3 = None
        if valid_idx_t3 is not None and current_colour in ('green', 'red'):
            if current_colour == 'green':
                trigger_level_t3 = valid_high_t3
                direction_t3 = "buy"
            else:
                trigger_level_t3 = valid_low_t3
                direction_t3 = "sell"

            for i in range(valid_idx_t3 + 1, len(df) - 1):
                if direction_t3 == "buy" and df.iloc[i]["High"] > trigger_level_t3:
                    trigger_index_t3 = i
                    break
                elif direction_t3 == "sell" and df.iloc[i]["Low"] < trigger_level_t3:
                    trigger_index_t3 = i
                    break

            if trigger_index_t3 is not None:
                dt = pd.to_datetime(df.index[trigger_index_t3])
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC").tz_convert(IST)
                else:
                    dt = dt.tz_convert(IST)
                trigger_candle_date_t3 = dt.strftime("%Y-%m-%d")
                trigger_candle_time_t3 = dt.strftime("%H:%M")

        key_t3 = f"{symbol}_{SELECTED_TF}"
        stored_t3 = triggered_info.get(key_t3)
        trigger_status_t3 = ""
        trigger_datetime_t3 = ""
        if current_colour in ('green', 'red'):
            if stored_t3 is None or stored_t3.get('colour') != current_colour:
                trigger_status_t3 = "New Trigger"
                trigger_datetime_t3 = valid_dt_t3 if valid_idx_t3 is not None else ""
                triggered_info[key_t3] = {
                    "colour": current_colour,
                    "datetime": trigger_datetime_t3,
                    "t3_value": round(current_t3, 2)
                }
                new_triggers_t3 += 1
            else:
                trigger_status_t3 = ""
                trigger_datetime_t3 = stored_t3.get('datetime', '') if stored_t3 else ""

        zerodha_action_t3 = ""
        if valid_idx_t3 is not None and current_colour in ('green', 'red') and trigger_level_t3 is not None:
            order_key = (symbol, trigger_level_t3, direction_t3)
            if order_key in order_sent_t3:
                zerodha_action_t3 = "Already buy order sent" if direction_t3 == "buy" else "Already sell order sent"
            else:
                trigger_happened = trigger_index_t3 is not None
                current_ltp = None
                if not trigger_happened:
                    if INCLUDE_LTP:
                        current_ltp = ltp_dict.get(symbol)
                    if current_ltp is None:
                        current_ltp = df.iloc[-2]["Close"].item()
                if trigger_happened:
                    if CASE_A_MARKET_ORDER:
                        zerodha_action_t3 = "Buy Market Order" if direction_t3 == "buy" else "Sell Market Order"
                    else:
                        zerodha_action_t3 = "Buy Long gap" if direction_t3 == "buy" else "Sell gap"
                    order_sent_t3.add(order_key)
                else:
                    if current_ltp is None:
                        current_ltp = df.iloc[-2]["Close"].item()
                    if direction_t3 == "buy":
                        if current_ltp > trigger_level_t3:
                            zerodha_action_t3 = "Buy Market Order"
                        else:
                            zerodha_action_t3 = f"Buy Limit Order (BO close {trigger_level_t3})"
                    else:
                        if current_ltp < trigger_level_t3:
                            zerodha_action_t3 = "Sell Market Order"
                        else:
                            zerodha_action_t3 = f"Sell Limit Order (BO close {trigger_level_t3})"
                    order_sent_t3.add(order_key)
                if zerodha_action_t3 and "Already" not in zerodha_action_t3 and "gap" not in zerodha_action_t3:
                    write_pending_order(symbol, zerodha_action_t3, trigger_level_t3, SELECTED_TF, "T3 ")

        # ---- T3 static LTP columns (unchanged) ----
        t3_ltp_val = ""
        t3_diff = ""
        t3_diff_pct = ""
        t3_profit = ""
        current_ltp_t3 = ltp_dict.get(symbol) if INCLUDE_LTP else df.iloc[-2]["Close"].item()
        if current_ltp_t3 is not None and trigger_level_t3 is not None and direction_t3 in ('buy', 'sell'):
            t3_ltp_val = round(current_ltp_t3, 2)
            if direction_t3 == 'buy':
                t3_diff = round(current_ltp_t3 - trigger_level_t3, 2)
                if trigger_level_t3 != 0:
                    t3_diff_pct = round((t3_diff / trigger_level_t3) * 100, 2)
            else:
                t3_diff = round(trigger_level_t3 - current_ltp_t3, 2)
                if trigger_level_t3 != 0:
                    t3_diff_pct = round((t3_diff / trigger_level_t3) * 100, 2)
            if t3_diff and trigger_level_t3:
                t3_profit = round((INVESTMENT_AMOUNT // trigger_level_t3) * t3_diff, 2)

        # ---- T3 Booking Profit (unchanged) ----
        booking_status_t3 = ""
        max_profit_pct_t3 = 0.0
        max_profit_value_t3 = 0.0
        if trigger_index_t3 is not None and direction_t3 in ('buy', 'sell'):
            entry_price = trigger_level_t3
            if direction_t3 == 'buy':
                prices = df.iloc[trigger_index_t3 : len(df)-1]['High']
                current_price = df.iloc[-2]['Close']
            else:
                prices = df.iloc[trigger_index_t3 : len(df)-1]['Low']
                current_price = df.iloc[-2]['Close']
            if direction_t3 == 'buy':
                max_price = prices.max()
                max_profit_pct_t3 = round((max_price - entry_price) / entry_price * 100, 2)
                current_profit_pct = round((current_price - entry_price) / entry_price * 100, 2)
                max_profit_value_t3 = round(max_price - entry_price, 2)
            else:
                min_price = prices.min()
                max_profit_pct_t3 = round((entry_price - min_price) / entry_price * 100, 2)
                current_profit_pct = round((entry_price - current_price) / entry_price * 100, 2)
                max_profit_value_t3 = round(entry_price - min_price, 2)
            if max_profit_pct_t3 >= PROFIT_TARGET_PERCENT:
                booking_status_t3 = f"Profit booked at {PROFIT_TARGET_PERCENT}%"
            elif max_profit_pct_t3 > 0 and current_profit_pct > 0:
                booking_status_t3 = f"Profit in progress of {max_profit_pct_t3:.1f}%"
            elif max_profit_pct_t3 > 0 and current_profit_pct <= 0:
                booking_status_t3 = f"Booked maximum profit of {max_profit_pct_t3:.1f}%"
            else:
                booking_status_t3 = "No profit yet"

        # =====================================================
        #  WAVETREND INDICATOR (FIXED: direction_wt set before trigger loop)
        # =====================================================
        wt1_series, wt2_series, wt_diff_series, cross_series = compute_wavetrend(df, n1=15, n2=27)

        wt1_val = ""
        wt2_val = ""
        wt_diff_val = ""
        wt_cross = ""
        wt_trend = ""
        valid_idx_wt = None
        valid_open_wt = valid_high_wt = valid_low_wt = valid_close_wt = valid_dt_wt = valid_diff_wt = ""
        wt_valid_candle_value = ""
        perfect_status_wt = ""
        trigger_level_wt = None
        trigger_candle_date_wt = ""
        trigger_candle_time_wt = ""
        direction_wt = ""   # will be set when valid candle exists
        trigger_index_wt = None
        trigger_status_wt = ""
        trigger_datetime_wt = ""
        zerodha_action_wt = ""
        booking_status_wt = ""
        max_profit_pct_wt = 0.0
        max_profit_value_wt = 0.0

        if len(wt1_series) >= 4:
            wt1_val = round(wt1_series.iloc[-2], 2) if not pd.isna(wt1_series.iloc[-2]) else ""
            wt2_val = round(wt2_series.iloc[-2], 2) if not pd.isna(wt2_series.iloc[-2]) else ""
            wt_diff_val = round(wt_diff_series.iloc[-2], 2) if not pd.isna(wt_diff_series.iloc[-2]) else ""
            wt_cross = cross_series.iloc[-2] if not pd.isna(cross_series.iloc[-2]) else "No Cross"

            found_cross = False
            for i in range(len(cross_series)-2, 0, -1):
                if cross_series.iloc[i] in ("Bull Cross", "Bear Cross"):
                    valid_idx_wt = i
                    wt_direction = "buy" if cross_series.iloc[i] == "Bull Cross" else "sell"
                    wt_trend = "Buy Trend" if cross_series.iloc[i] == "Bull Cross" else "Sell Trend"
                    found_cross = True
                    candle = df.iloc[i]
                    valid_open_wt = round(candle["Open"].item(), 2)
                    valid_high_wt = round(candle["High"].item(), 2)
                    valid_low_wt = round(candle["Low"].item(), 2)
                    valid_close_wt = round(candle["Close"].item(), 2)
                    valid_diff_wt = round(wt_diff_series.iloc[i].item(), 2) if not pd.isna(wt_diff_series.iloc[i]) else ""
                    if wt_direction == "buy":
                        wt_valid_candle_value = round(wt1_series.iloc[i].item(), 2) if not pd.isna(wt1_series.iloc[i]) else ""
                    else:
                        wt_valid_candle_value = round(wt2_series.iloc[i].item(), 2) if not pd.isna(wt2_series.iloc[i]) else ""
                    dt = pd.to_datetime(df.index[i])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    valid_dt_wt = dt.strftime("%Y-%m-%d %H:%M")
                    if wt_direction == "buy":
                        perfect_status_wt = "Perfect" if candle["Open"] == candle["Low"] else "Imperfect"
                    else:
                        perfect_status_wt = "Perfect" if candle["Open"] == candle["High"] else "Imperfect"
                    break
            if not found_cross:
                wt_trend = "Inside Trend"

            # ---- WT Trigger detection (FIXED: direction_wt set here) ----
            if valid_idx_wt is not None and wt_direction in ("buy", "sell"):
                if wt_direction == "buy":
                    trigger_level_wt = valid_high_wt
                    direction_wt = "buy"      # <-- set direction regardless of trigger
                else:
                    trigger_level_wt = valid_low_wt
                    direction_wt = "sell"     # <-- set direction regardless of trigger

                for i in range(valid_idx_wt + 1, len(df) - 1):
                    if wt_direction == "buy" and df.iloc[i]["High"] > trigger_level_wt:
                        trigger_index_wt = i
                        break
                    elif wt_direction == "sell" and df.iloc[i]["Low"] < trigger_level_wt:
                        trigger_index_wt = i
                        break

                if trigger_index_wt is not None:
                    dt = pd.to_datetime(df.index[trigger_index_wt])
                    if dt.tzinfo is None:
                        dt = dt.tz_localize("UTC").tz_convert(IST)
                    else:
                        dt = dt.tz_convert(IST)
                    trigger_candle_date_wt = dt.strftime("%Y-%m-%d")
                    trigger_candle_time_wt = dt.strftime("%H:%M")

            # ---- WT Trigger Status (separate memory) ----
            key_wt = f"wt_{symbol}_{SELECTED_TF}"
            stored_wt = triggered_info.get(key_wt)
            if valid_idx_wt is not None and direction_wt in ("buy", "sell"):
                if stored_wt is None or stored_wt.get('datetime') != valid_dt_wt or stored_wt.get('direction') != direction_wt:
                    trigger_status_wt = "New Trigger"
                    trigger_datetime_wt = valid_dt_wt
                    triggered_info[key_wt] = {
                        "datetime": valid_dt_wt,
                        "direction": direction_wt,
                        "level": trigger_level_wt
                    }
                    new_triggers_wt += 1
                else:
                    trigger_status_wt = ""
                    trigger_datetime_wt = stored_wt.get('datetime', '') if stored_wt else ""

            # ---- WT Zerodha Action (now direction_wt is always set) ----
            if valid_idx_wt is not None and direction_wt in ("buy", "sell") and trigger_level_wt is not None:
                order_key_wt = (symbol, trigger_level_wt, direction_wt)
                if order_key_wt in order_sent_wt:
                    zerodha_action_wt = "Already buy order sent" if direction_wt == "buy" else "Already sell order sent"
                else:
                    trigger_happened_wt = trigger_index_wt is not None
                    current_ltp_wt = None
                    if not trigger_happened_wt:
                        if INCLUDE_LTP:
                            current_ltp_wt = ltp_dict.get(symbol)
                        if current_ltp_wt is None:
                            current_ltp_wt = df.iloc[-2]["Close"].item()
                    if trigger_happened_wt:
                        if CASE_A_MARKET_ORDER:
                            zerodha_action_wt = "Buy Market Order" if direction_wt == "buy" else "Sell Market Order"
                        else:
                            zerodha_action_wt = "Buy Long gap" if direction_wt == "buy" else "Sell gap"
                        order_sent_wt.add(order_key_wt)
                    else:
                        if current_ltp_wt is None:
                            current_ltp_wt = df.iloc[-2]["Close"].item()
                        if direction_wt == "buy":
                            if current_ltp_wt > trigger_level_wt:
                                zerodha_action_wt = "Buy Market Order"
                            else:
                                zerodha_action_wt = f"Buy Limit Order (BO close {trigger_level_wt})"
                        else:
                            if current_ltp_wt < trigger_level_wt:
                                zerodha_action_wt = "Sell Market Order"
                            else:
                                zerodha_action_wt = f"Sell Limit Order (BO close {trigger_level_wt})"
                        order_sent_wt.add(order_key_wt)
                    if zerodha_action_wt and "Already" not in zerodha_action_wt and "gap" not in zerodha_action_wt:
                        write_pending_order(symbol, zerodha_action_wt, trigger_level_wt, SELECTED_TF, "WT ")

            # ---- WT static LTP columns ----
            wt_ltp_val = ""
            wt_diff = ""
            wt_diff_pct = ""
            wt_profit = ""
            current_ltp_wt = ltp_dict.get(symbol) if INCLUDE_LTP else df.iloc[-2]["Close"].item()
            if current_ltp_wt is not None and trigger_level_wt is not None and direction_wt in ('buy', 'sell'):
                wt_ltp_val = round(current_ltp_wt, 2)
                if direction_wt == 'buy':
                    wt_diff = round(current_ltp_wt - trigger_level_wt, 2)
                    if trigger_level_wt != 0:
                        wt_diff_pct = round((wt_diff / trigger_level_wt) * 100, 2)
                else:
                    wt_diff = round(trigger_level_wt - current_ltp_wt, 2)
                    if trigger_level_wt != 0:
                        wt_diff_pct = round((wt_diff / trigger_level_wt) * 100, 2)
                if wt_diff and trigger_level_wt:
                    wt_profit = round((INVESTMENT_AMOUNT // trigger_level_wt) * wt_diff, 2)

            # ---- WT Booking Profit ----
            if trigger_index_wt is not None and direction_wt in ("buy", "sell"):
                entry_price_wt = trigger_level_wt
                if direction_wt == "buy":
                    prices_wt = df.iloc[trigger_index_wt : len(df)-1]['High']
                    current_price_wt = df.iloc[-2]['Close']
                else:
                    prices_wt = df.iloc[trigger_index_wt : len(df)-1]['Low']
                    current_price_wt = df.iloc[-2]['Close']
                if direction_wt == "buy":
                    max_price_wt = prices_wt.max()
                    max_profit_pct_wt = round((max_price_wt - entry_price_wt) / entry_price_wt * 100, 2)
                    current_profit_pct_wt = round((current_price_wt - entry_price_wt) / entry_price_wt * 100, 2)
                    max_profit_value_wt = round(max_price_wt - entry_price_wt, 2)
                else:
                    min_price_wt = prices_wt.min()
                    max_profit_pct_wt = round((entry_price_wt - min_price_wt) / entry_price_wt * 100, 2)
                    current_profit_pct_wt = round((entry_price_wt - current_price_wt) / entry_price_wt * 100, 2)
                    max_profit_value_wt = round(entry_price_wt - min_price_wt, 2)
                if max_profit_pct_wt >= PROFIT_TARGET_PERCENT:
                    booking_status_wt = f"Profit booked at {PROFIT_TARGET_PERCENT}%"
                elif max_profit_pct_wt > 0 and current_profit_pct_wt > 0:
                    booking_status_wt = f"Profit in progress of {max_profit_pct_wt:.1f}%"
                elif max_profit_pct_wt > 0 and current_profit_pct_wt <= 0:
                    booking_status_wt = f"Booked maximum profit of {max_profit_pct_wt:.1f}%"
                else:
                    booking_status_wt = "No profit yet"

        # ---- Build row ----
        display_symbol = symbol.replace('.NS', '')
        row = [
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            display_symbol,
            SELECTED_TF,
            round(current_t3, 2) if current_t3 is not None else "",
            current_colour,
            valid_open_t3 if valid_idx_t3 is not None else "",
            valid_high_t3 if valid_idx_t3 is not None else "",
            valid_low_t3 if valid_idx_t3 is not None else "",
            valid_close_t3 if valid_idx_t3 is not None else "",
            valid_dt_t3 if valid_idx_t3 is not None else "",
            valid_t3_val if valid_idx_t3 is not None else "",
            trend_label,
            trigger_status_t3,
            trigger_datetime_t3,
            zerodha_action_t3,
            trigger_candle_date_t3,
            trigger_candle_time_t3,
            t3_ltp_val,
            t3_diff,
            t3_diff_pct,
            display_symbol,
            t3_profit,
            booking_status_t3,
            max_profit_pct_t3,
            max_profit_value_t3,
            ""
        ]
        row.extend([
            wt1_val,
            wt2_val,
            wt_diff_val,
            wt_cross,
            wt_trend,
            valid_open_wt if valid_idx_wt is not None else "",
            valid_high_wt if valid_idx_wt is not None else "",
            valid_low_wt if valid_idx_wt is not None else "",
            valid_close_wt if valid_idx_wt is not None else "",
            valid_dt_wt if valid_idx_wt is not None else "",
            valid_diff_wt,
            wt_valid_candle_value,
            perfect_status_wt,
            trigger_status_wt,
            trigger_datetime_wt,
            zerodha_action_wt,
            trigger_candle_date_wt,
            trigger_candle_time_wt,
            wt_ltp_val,
            wt_diff,
            wt_diff_pct,
            display_symbol,
            wt_profit,
            booking_status_wt,
            max_profit_pct_wt,
            max_profit_value_wt,
            ""
        ])

        rows.append(row)
        zerodha_actions_t3.append(zerodha_action_t3)
        zerodha_actions_wt.append(zerodha_action_wt)

    # Save order memories
    save_order_sent(ORDER_SENT_FILE_T3, order_sent_t3)
    save_order_sent(ORDER_SENT_FILE_WT, order_sent_wt)

    out_json = {
        "run_start": start_str,
        "duration": f"{(datetime.now(IST) - start_time).total_seconds():.1f}s",
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

    summary = (
        f"Timeframe: {SELECTED_TF} | Run start: {start_str} | "
        f"Duration: {out_json['duration']} | Total: {total} | New Triggers (T3): {new_triggers_t3} | "
        f"New Triggers (WT): {new_triggers_wt} | Skipped: {len(SKIPPED_SYMBOLS)} | Heikin Ashi: {USE_HEIKIN_ASHI} | "
        f"Zerodha: {ENABLE_ZERODHA_ACTION} | Buy orders: {ENABLE_BUY_ORDERS} | Sell orders: {ENABLE_SELL_ORDERS} | "
        f"Investment: ₹{INVESTMENT_AMOUNT} | Target Profit: {PROFIT_TARGET_PERCENT}%"
    )
    if rows:
        ws, header_row = write_output_batch(sheet, rows, summary=summary,
                                            investment_amount=INVESTMENT_AMOUNT)
        apply_color_formatting(ws, header_row, zerodha_actions_t3, zerodha_actions_wt)

    print("\n\n📊 RUN SUMMARY")
    print("────────────────────────────")
    print(f"Timeframe          : {SELECTED_TF}")
    print(f"Heikin Ashi        : {USE_HEIKIN_ASHI}")
    print(f"Run start          : {start_str}")
    print(f"Duration           : {out_json['duration']}")
    print(f"Total Symbols      : {total}")
    print(f"New Triggers (T3)  : {new_triggers_t3}")
    print(f"New Triggers (WT)  : {new_triggers_wt}")
    print(f"Skipped Symbols    : {len(SKIPPED_SYMBOLS)}")
    print(f"Zerodha Action     : {ENABLE_ZERODHA_ACTION}")
    print(f"Buy Orders allowed : {ENABLE_BUY_ORDERS}")
    print(f"Sell Orders allowed: {ENABLE_SELL_ORDERS}")
    print(f"Investment Amount  : ₹{INVESTMENT_AMOUNT}")
    print(f"Target Profit %    : {PROFIT_TARGET_PERCENT}%")
    print("────────────────────────────")
    print("✅ Sheet, JSON & order files updated successfully")

if __name__ == "__main__":
    main()