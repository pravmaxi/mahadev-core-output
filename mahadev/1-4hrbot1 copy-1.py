# === Stock Price Analyzer with Multi-Timeframe Support ===
import requests
import numpy as np
import pandas as pd
import yfinance as yf
import time
from datetime import datetime
from collections import defaultdict
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_formatting import CellFormat, Color, format_cell_ranges
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from gspread_formatting import TextFormat
from tenacity import retry, stop_after_attempt, wait_exponential

# === Settings ===
TIMEFRAME = "4h"  # Can be "1h", "4h", "30m", "15m", "5m"
AUTO_PERIOD = True  # Set to False to manually specify period
PERIOD = "60d"     # Only used if AUTO_PERIOD is False
USE_HARDCODED_DATE = True
HARDCODED_DATE = "2026-01-27"
MAX_WORKERS = 8    # Thread pool size for concurrent processing

# Auto period mapping
AUTO_PERIOD_MAP = {
    "5m": "30d",
    "15m": "30d",
    "30m": "60d",
    "1h": "60d",
    "4h": "1y",
    "1d": "1y"
}

# Calculate actual period to use
ACTUAL_PERIOD = AUTO_PERIOD_MAP[TIMEFRAME] if AUTO_PERIOD else PERIOD

# === Telegram Setup ===
TELEGRAM_TOKEN = "8011257570:AAFO8LxI_075Up-B-Znp7pEnFfGlut3V_90"
CHAT_ID = "6187777440"

# === Google Sheet Setup ===
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = f"output_{TIMEFRAME}"
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# === Technical Indicators ===
N1, N2 = 15, 27  # WaveTrend settings

# Rate Limiter Class
class RateLimiter:
    def __init__(self, max_calls=50, period=65):  # Conservative limits
        self.max_calls = max_calls
        self.period = period
        self.timestamps = []
    
    def wait(self):
        now = time.time()
        self.timestamps = [t for t in self.timestamps if t > now - self.period]
        
        if len(self.timestamps) >= self.max_calls:
            sleep_time = self.period - (now - self.timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time + 0.1)  # Small buffer
        
        self.timestamps.append(time.time())
        return self

rate_limiter = RateLimiter()

# Initialize Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME)
input_ws = sheet.worksheet(INPUT_SHEET)

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_or_create_worksheet():
    rate_limiter.wait()
    if OUTPUT_SHEET in [ws.title for ws in sheet.worksheets()]:
        return sheet.worksheet(OUTPUT_SHEET)
    return sheet.add_worksheet(title=OUTPUT_SHEET, rows=1000, cols=100)

output_ws = get_or_create_worksheet()

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def safe_clear_worksheet():
    rate_limiter.wait()
    output_ws.clear()

safe_clear_worksheet()

signals_by_time = defaultdict(list)
date_suffix = HARDCODED_DATE if USE_HARDCODED_DATE else datetime.now().strftime("%Y-%m-%d")
TRIGGER_FILE = f"triggered_signals_{TIMEFRAME}_{date_suffix}.json"

def get_time_slots(timeframe):
    """Generate time slots based on timeframe"""
    if timeframe == "4h":
        return ["09:15", "13:15"]
    elif timeframe == "1h":
        return ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    elif timeframe == "30m":
        return ["09:15", "09:45", "10:15", "10:45", "11:15", "11:45", 
                "12:15", "12:45", "13:15", "13:45", "14:15", "14:45", "15:15"]
    elif timeframe == "15m":
        return ["09:15", "09:30", "09:45", "10:00", "10:15", "10:30", "10:45", 
                "11:00", "11:15", "11:30", "11:45", "12:00", "12:15", "12:30", 
                "12:45", "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", 
                "14:30", "14:45", "15:00", "15:15"]
    elif timeframe == "5m":
        return [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5) 
                if (h < 15 or m <= 15) and (h > 9 or m >= 15)]
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

def load_previous_triggers():
    if os.path.exists(TRIGGER_FILE):
        with open(TRIGGER_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_current_triggers(trigger_set):
    with open(TRIGGER_FILE, "w") as f:
        json.dump(list(trigger_set), f)

def get_column_offset(time_str):
    time_slots = get_time_slots(TIMEFRAME)
    try:
        return time_slots.index(time_str) * 16
    except ValueError:
        time_obj = datetime.strptime(time_str, "%H:%M").time()
        closest = min(time_slots, key=lambda x: abs(
            (datetime.strptime(x, "%H:%M").time().hour * 60 + 
             datetime.strptime(x, "%H:%M").time().minute) - 
            (time_obj.hour * 60 + time_obj.minute)))
        return time_slots.index(closest) * 16

def col_index_to_letter(index):
    letters = ''
    while index >= 0:
        letters = chr(index % 26 + 65) + letters
        index = index // 26 - 1
    return letters

def convert_to_heikin_ashi(df):
    ha_df = df.copy()
    ha_df['HA_Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
    ha_open = [(df['Open'].iloc[0] + df['Close'].iloc[0]) / 2]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i-1] + ha_df['HA_Close'].iloc[i-1]) / 2)
    ha_df['HA_Open'] = ha_open
    ha_df['HA_High'] = ha_df[['HA_Open', 'HA_Close', 'High']].max(axis=1)
    ha_df['HA_Low'] = ha_df[['HA_Open', 'HA_Close', 'Low']].min(axis=1)
    return ha_df[['HA_Open', 'HA_High', 'HA_Low', 'HA_Close']].rename(columns={
        'HA_Open': 'Open', 'HA_High': 'High', 'HA_Low': 'Low', 'HA_Close': 'Close'
    })

def calculate_wavetrend(df):
    hlc3 = (df['High'] + df['Low'] + df['Close']) / 3
    esa = hlc3.ewm(span=N1, adjust=False).mean()
    d = abs(hlc3 - esa).ewm(span=N1, adjust=False).mean()
    ci = (hlc3 - esa) / (0.015 * d)
    tci = ci.ewm(span=N2, adjust=False).mean()
    df['WT1'] = tci
    df['WT2'] = tci.rolling(window=4).mean()
    cross_up = (df['WT1'].shift(1) < df['WT2'].shift(1)) & (df['WT1'] > df['WT2'])
    cross_down = (df['WT1'].shift(1) > df['WT2'].shift(1)) & (df['WT1'] < df['WT2'])
    df['Color'] = np.where(cross_up, 'blue', np.where(cross_down, 'yellow', None))
    df['BY_RSI_Value'] = (df['WT1'] + df['WT2']) / 2
    df['Main_Value'] = df['WT1'] - df['WT2']
    return df

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"Telegram error: {e}")

def analyze_single_stock(stock, df_stock):
    try:
        if df_stock.empty:
            return
            
        df_stock.index = df_stock.index.tz_convert('Asia/Kolkata')
        df_stock = convert_to_heikin_ashi(df_stock)
        df_stock = calculate_wavetrend(df_stock)
        
        target_date = pd.to_datetime(HARDCODED_DATE).date() if USE_HARDCODED_DATE else datetime.now().date()
        signals = df_stock[df_stock['Color'].notna()]
        target_signals = signals[signals.index.date == target_date]
        
        if target_signals.empty:
            return
            
        last_index = df_stock.index[-1]
        for timestamp, row in target_signals.iterrows():
            has_next = "NO" if timestamp == last_index else "YES"
            future_high = future_low = ""
            
            if has_next == "YES":
                future = df_stock[df_stock.index > timestamp]
                if not future.empty:
                    future_high = future['High'].max()
                    future_low = future['Low'].min()
                    
            time_str = timestamp.strftime('%H:%M')
            signal = {
                "date": timestamp.strftime('%Y-%m-%d'),
                "time": time_str,
                "stock": stock,
                "price": row['High'] if row['Color'] == 'blue' else row['Low'],
                "color": row['Color'],
                "by_rsi_value": round(row['BY_RSI_Value'], 2),
                "main_value": round(row['Main_Value'], 2),
                "next_candle": has_next,
                "future_high": round(future_high, 2) if future_high else "",
                "future_low": round(future_low, 2) if future_low else ""
            }
            signals_by_time[time_str].append(signal)
    except Exception as e:
        print(f"{stock} failed: {e}")

def analyze_stocks_multithreaded(stocks):
    tickers = [s if s.startswith('^') else f"{s}.NS" for s in stocks]
    df_all = yf.download(tickers=" ".join(tickers), period=ACTUAL_PERIOD, interval=TIMEFRAME, 
                        group_by='ticker', threads=True)
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for stock in stocks:
            df = df_all[(stock + ".NS")].copy() if (stock + ".NS") in df_all else pd.DataFrame()
            futures.append(executor.submit(analyze_single_stock, stock, df))
        
        for future in as_completed(futures):
            future.result()

def write_to_sheet():
    # Clear existing formatting
    default_format = CellFormat(
        backgroundColor=Color(1, 1, 1),
        textFormat=TextFormat(bold=False)
    )
    
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
    def apply_default_format():
        rate_limiter.wait()
        format_cell_ranges(output_ws, [('A1:EZ1000', default_format)])
    
    apply_default_format()

    def sanitize(val):
        if pd.isna(val) or val is None:
            return ""
        if isinstance(val, np.generic):
            return val.item()
        return val

    all_cells_to_color = []
    previous_triggers = load_previous_triggers()
    current_triggers = set()
    new_trigger_signals_by_stock = defaultdict(list)
    
    # Batch updates
    batch_updates = []
    format_updates = []
    
    headers = ["Date", "Time", "Stock", "Price", "Candle High", "Color", "Candle Low", "Final Status",
               "BY RSI Value", "Main Value", "Next Candle", "New Trigger", "", "", "", ""]
    
    # Write headers for all time slots
    time_slots = get_time_slots(TIMEFRAME)
    for time_str in time_slots:
        col_offset = get_column_offset(time_str)
        col_letter = col_index_to_letter(col_offset)
        batch_updates.append({
            'range': f"{col_letter}1",
            'values': [headers]
        })

    current_row = 2
    for time_str, signals in signals_by_time.items():
        col_offset = get_column_offset(time_str)
        col_letter = col_index_to_letter(col_offset)
        rows = []

        for i, signal in enumerate(signals):
            signal_key = f"{signal['date']}_{signal['time']}_{signal['stock']}_{signal['color']}"
            final_status = new_trigger = ""
            
            if signal['color'] == 'blue' and isinstance(signal['future_high'], (int, float)):
                if signal['price'] < signal['future_high']:
                    final_status = "Buy Triggered"
                    current_triggers.add(signal_key)
            elif signal['color'] == 'yellow' and isinstance(signal['future_low'], (int, float)):
                if signal['price'] > signal['future_low']:
                    final_status = "Sell Triggered"
                    current_triggers.add(signal_key)
                    
            if signal_key in current_triggers and signal_key not in previous_triggers:
                new_trigger = "Triggered NEW"
                new_trigger_signals_by_stock[signal['stock']].append(signal)

            row_data = [signal['date'], signal['time'], signal['stock'], signal['price'],
                       signal['future_high'], signal['color'], signal['future_low'], final_status,
                       signal['by_rsi_value'], signal['main_value'], signal['next_candle'], new_trigger,
                       "", "", "", ""]
            sanitized_row = [sanitize(cell) for cell in row_data]
            rows.append(sanitized_row)

            cell_row = current_row + i
            color_col = col_index_to_letter(col_offset + 5)
            status_col = col_index_to_letter(col_offset + 7)
            trigger_col = col_index_to_letter(col_offset + 11)
            by_rsi_col = col_index_to_letter(col_offset + 8)  # BY RSI Value column
            
            # Original color formatting
            if signal['color'] == 'blue':
                format_updates.append((
                    f"{color_col}{cell_row}",
                    CellFormat(backgroundColor=Color(0.6, 0.9, 0.6))
                ))
            elif signal['color'] == 'yellow':
                format_updates.append((
                    f"{color_col}{cell_row}",
                    CellFormat(backgroundColor=Color(1, 0.6, 0.6))
                ))
                
            if final_status == "Buy Triggered":
                format_updates.append((
                    f"{status_col}{cell_row}",
                    CellFormat(backgroundColor=Color(0.6, 0.9, 0.6))
                ))
            elif final_status == "Sell Triggered":
                format_updates.append((
                    f"{status_col}{cell_row}",
                    CellFormat(backgroundColor=Color(1, 0.6, 0.6))
                ))
                
            if new_trigger:
                format_updates.append((
                    f"{trigger_col}{cell_row}",
                    CellFormat(backgroundColor=Color(1, 1, 0.6))
                ))
            
            # NEW: BY RSI Value color formatting
            if isinstance(signal['by_rsi_value'], (int, float)):
                if signal['by_rsi_value'] > 45:
                    format_updates.append((
                        f"{by_rsi_col}{cell_row}",
                        CellFormat(backgroundColor=Color(1, 0.6, 0.6))  # Red
                    ))
                elif signal['by_rsi_value'] < -45:
                    format_updates.append((
                        f"{by_rsi_col}{cell_row}",
                        CellFormat(backgroundColor=Color(0.6, 0.9, 0.6))  # Green
                    ))

        if rows:
            batch_updates.append({
                'range': f"{col_letter}{current_row}",
                'values': rows
            })

    # Execute batch updates
    if batch_updates:
        @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
        def execute_batch_updates():
            rate_limiter.wait()
            output_ws.batch_update(batch_updates)
        
        execute_batch_updates()
    
    # Execute format updates
    if format_updates:
        @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
        def execute_format_updates():
            rate_limiter.wait()
            format_cell_ranges(output_ws, format_updates)
        
        execute_format_updates()
    
    save_current_triggers(current_triggers)

    for stock, signals in new_trigger_signals_by_stock.items():
        for sig in signals:
            message = (
                f"<b>🚨 New Trigger Detected</b>\n"
                f"<b>Stock:</b> {sig['stock']}\n"
                f"<b>Date:</b> {sig['date']}\n"
                f"<b>Time:</b> {sig['time']}\n"
                f"<b>Trigger:</b> {'Buy' if sig['color'] == 'blue' else 'Sell'}\n"
                f"<b>Price:</b> {sig['price']}\n"
                f"<b>BY RSI Value:</b> {sig['by_rsi_value']}\n"
                f"<b>Main Value:</b> {sig['main_value']}\n"
                f"<b>Next Candle Exists:</b> {sig['next_candle']}"
            )
            send_telegram_message(message)

def main():
    start = time.time()
    
    # Print configuration summary
    print("\n=== Configuration Summary ===")
    print(f"⏰ Timeframe: {TIMEFRAME}")
    print(f"📅 Period: {'Auto (' + AUTO_PERIOD_MAP[TIMEFRAME] + ')' if AUTO_PERIOD else 'Manual (' + PERIOD + ')'}")
    print(f"📅 Using date: {HARDCODED_DATE if USE_HARDCODED_DATE else 'Today'}")
    
    # Fetch stock symbols from input sheet
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_stock_list():
        rate_limiter.wait()
        return [s.strip() for s in input_ws.col_values(1) if s.strip()]
    
    stocks = get_stock_list()
    analyze_stocks_multithreaded(stocks)

    # Write signals if found
    if signals_by_time:
        write_to_sheet()
        send_telegram_message(f"✅ {TIMEFRAME} Signals updated on Google Sheet!")
        print("✅ Signals written to Google Sheet!")
    else:
        send_telegram_message(f"📉 No {TIMEFRAME} signals found today.")
        print("📉 No signals found today.")

    completed_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    duration = time.time() - start
    date_used = HARDCODED_DATE if HARDCODED_DATE else datetime.now().strftime('%Y-%m-%d')

    print("\n=== Execution Summary ===")
    print(f"✅ Code completed at     : {completed_time}")
    print(f"📅 Data date used        : {date_used}")
    print(f"⏰ Timeframe used       : {TIMEFRAME}")
    print(f"📅 Period used          : {ACTUAL_PERIOD}")
    print(f"📄 Input sheet tab name : {INPUT_SHEET}")
    print(f"📄 Output sheet tab name: {OUTPUT_SHEET}")
    print(f"⏱️ Total run time       : {duration:.1f} seconds")

if __name__ == "__main__":
    main()