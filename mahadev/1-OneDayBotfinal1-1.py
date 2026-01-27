import numpy as np
import pandas as pd
import yfinance as yf
import gspread
from datetime import datetime, timedelta
from oauth2client.service_account import ServiceAccountCredentials
from urllib.error import HTTPError
from collections import defaultdict
from gspread_formatting import CellFormat, Color, format_cell_ranges
import time
from gspread_formatting import TextFormat
import json
import os

# --- USER CONFIGURATION ---
today = datetime.strptime("2026-01-27", "%Y-%m-%d")  # Set date manually
range_days = 15  # Number of days to look back
INPUT_TAB = "input2"
OUTPUT_TAB = "day_output"
JSON_FILE = "trigger_history.json"

# --- Google Sheets setup ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json", scope
)
client = gspread.authorize(creds)

# Open Google Sheet
sheet = client.open("Stock Price Scraper")
input_ws = sheet.worksheet(INPUT_TAB)
output_ws = sheet.worksheet(OUTPUT_TAB)

start_time = time.time()

# Read stock symbols
symbols = [s.strip().upper() for s in input_ws.col_values(1)[1:] if s.strip()]

# --- Load previous trigger history ---
def load_trigger_history():
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

# --- Save trigger history ---
def save_trigger_history(history):
    with open(JSON_FILE, 'w') as f:
        json.dump(history, f, indent=2)

# Initialize trigger history
trigger_history = load_trigger_history()

# --- Indicator Functions ---
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def sma(series, length):
    return series.rolling(window=length).mean()

def atr(high, low, close, length=14):
    tr = np.maximum(
        high - low,
        np.maximum(
            abs(high - close.shift(1)),
            abs(low - close.shift(1))
        )
    )
    return sma(tr, length)

def chandelier_exit(high, low, close, length=22, mult=3.0, use_close=True):
    # Calculate ATR
    atr_val = mult * atr(high, low, close, length)
    
    # Calculate long stop
    if use_close:
        highest_close = close.rolling(window=length).max()
        long_stop = highest_close - atr_val
    else:
        highest_high = high.rolling(window=length).max()
        long_stop = highest_high - atr_val
    
    # Calculate short stop
    if use_close:
        lowest_close = close.rolling(window=length).min()
        short_stop = lowest_close + atr_val
    else:
        lowest_low = low.rolling(window=length).min()
        short_stop = lowest_low + atr_val
    
    # Initialize direction array
    dir_arr = np.zeros(len(close))
    dir_arr[0] = 1  # Start with bullish direction
    
    # Calculate direction
    for i in range(1, len(close)):
        if close.iloc[i] > short_stop.iloc[i-1] if not pd.isna(short_stop.iloc[i-1]) else False:
            dir_arr[i] = 1
        elif close.iloc[i] < long_stop.iloc[i-1] if not pd.isna(long_stop.iloc[i-1]) else False:
            dir_arr[i] = -1
        else:
            dir_arr[i] = dir_arr[i-1]
    
    # Generate signals
    buy_signal = (dir_arr == 1) & (np.roll(dir_arr, 1) == -1)
    sell_signal = (dir_arr == -1) & (np.roll(dir_arr, 1) == 1)
    
    return dir_arr, buy_signal, sell_signal, long_stop, short_stop

def wave_trend(df, n1=15, n2=27):
    hlc3 = (df['High'] + df['Low'] + df['Close']) / 3
    esa = ema(hlc3, n1)
    d = ema(abs(hlc3 - esa), n1)
    ci = (hlc3 - esa) / (0.015 * d)
    tci = ema(ci, n2)

    wt1 = tci
    wt2 = sma(wt1, 4)

    df['Forca_Alma'] = wt1 - wt2
    df['Avg_Crossover'] = (wt1 + wt2) / 2
    df['Cross'] = ((wt1.shift(1) < wt2.shift(1)) & (wt1 > wt2)) | ((wt1.shift(1) > wt2.shift(1)) & (wt1 < wt2))
    df['Color'] = np.where(df['Cross'], np.where(wt2 - wt1 > 0, 'yellow', 'blue'), None).astype(object)

    return df[['Open', 'High', 'Low', 'Close', 'Forca_Alma', 'Avg_Crossover', 'Color']]

# --- Heikin-Ashi calculation ---
def heikin_ashi(df):
    ha_df = pd.DataFrame(index=df.index)
    ha_df['HA_Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4

    ha_open = [(df['Open'][0] + df['Close'][0]) / 2]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i-1] + ha_df['HA_Close'].iloc[i-1]) / 2)
    ha_df['HA_Open'] = ha_open

    ha_df['HA_High'] = ha_df[['HA_Open', 'HA_Close']].join(df['High']).max(axis=1)
    ha_df['HA_Low'] = ha_df[['HA_Open', 'HA_Close']].join(df['Low']).min(axis=1)

    ha_df.rename(columns={
        'HA_Open': 'Open',
        'HA_High': 'High',
        'HA_Low': 'Low',
        'HA_Close': 'Close'
    }, inplace=True)

    return ha_df

# --- Fetch stock data ---
def fetch_stock_data(symbol, period='90d', interval='1d'):
    try:
        stock = yf.Ticker(symbol + ".NS")
        df = stock.history(period=period, interval=interval)
        if df.empty:
            print(f"No data for {symbol}")
            return None
        return df[['Open', 'High', 'Low', 'Close']]
    except HTTPError as e:
        print(f"HTTPError for {symbol}: {e}")
        return None
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

# --- Target dates (last 15 days) ---
target_dates = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(range_days)]

# --- Store signals by date ---
signals_by_date = defaultdict(list)
new_triggers = []  # Track new triggers for history

for stock in symbols:
    print(f"Processing {stock}...")
    raw_df = fetch_stock_data(stock)
    if raw_df is not None:
        # Calculate Chandelier Exit
        ce_dir, ce_buy, ce_sell, ce_long_stop, ce_short_stop = chandelier_exit(
            raw_df['High'], raw_df['Low'], raw_df['Close']
        )
        
        # Add CE data to dataframe
        raw_df['CE_Direction'] = ce_dir
        raw_df['CE_Buy'] = ce_buy
        raw_df['CE_Sell'] = ce_sell
        
        ha_df = heikin_ashi(raw_df)
        result = wave_trend(ha_df)
        filtered = result[result['Color'].notna()]
        
        for timestamp, row in filtered.iterrows():
            date_str = timestamp.strftime('%Y-%m-%d')
            if date_str in target_dates:
                color = row['Color']
                color_value = row['High'] if color == 'blue' else row['Low'] if color == 'yellow' else np.nan
                avg_crossover = round(row['Avg_Crossover'], 2)
                forca_alma = round(row['Forca_Alma'], 2)

                future_df = ha_df[ha_df.index > timestamp]
                has_next_candle = "Yes" if not future_df.empty else "No"

                final_status = ""
                if has_next_candle == "Yes":
                    if color == "yellow" and any(future_df['Low'] <= color_value):
                        final_status = "Sell Triggered"
                    elif color == "blue" and any(future_df['High'] >= color_value):
                        final_status = "Buy Triggered"
                
                # Check if this is a new trigger
                new_trigger = ""
                trigger_key = f"{date_str}_{stock}"
                
                if final_status in ["Buy Triggered", "Sell Triggered"]:
                    if trigger_key not in trigger_history:
                        new_trigger = "Triggered new"
                        new_triggers.append(trigger_key)
                    elif trigger_history[trigger_key] != final_status:
                        new_trigger = "Triggered new"
                        new_triggers.append(trigger_key)
                
                # Get CE data for this timestamp
                ce_data = raw_df.loc[timestamp]
                ce_direction = ce_data['CE_Direction'] if 'CE_Direction' in ce_data else 0
                ce_status = "Long" if ce_direction == 1 else "Short" if ce_direction == -1 else "Neutral"
                
                # Check if CE direction changed on this day
                prev_idx = raw_df.index.get_indexer([timestamp], method='pad')[0]
                prev_ce_dir = raw_df.iloc[prev_idx-1]['CE_Direction'] if prev_idx > 0 else 0
                ce_change = "Yes" if ce_direction != prev_ce_dir else "No"
                
                signals_by_date[date_str].append([
                    datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%y"),
                    stock,
                    color,
                    round(color_value, 2),
                    avg_crossover,
                    final_status,
                    forca_alma,
                    new_trigger,  # New Trigger column
                    ce_status,    # Chandelier Exit Status
                    ce_change,    # Change of CE direction
                    has_next_candle
                ])

# Update trigger history with new triggers
for trigger_key in new_triggers:
    date_str, stock = trigger_key.split("_", 1)
    # Find the final_status for this trigger
    for signal in signals_by_date.get(date_str, []):
        if signal[1] == stock and signal[5] in ["Buy Triggered", "Sell Triggered"]:
            trigger_history[trigger_key] = signal[5]
            break

# Save updated trigger history
save_trigger_history(trigger_history)

# --- Format output ---
sorted_dates = sorted(signals_by_date.keys(), reverse=True)
date_blocks = [signals_by_date[d] for d in sorted_dates if signals_by_date[d]]
max_rows = max(len(block) for block in date_blocks) if date_blocks else 0

output_data = []
for i in range(max_rows):
    row = []
    for date in sorted_dates:
        signals = signals_by_date[date]
        if i < len(signals):
            row.extend(signals[i])
        else:
            row.extend([""] * 11)  # 11 columns now
        row.extend(["", ""])
    output_data.append(row)

# --- Build header ---
header = []
for _ in sorted_dates:
    header.extend([
        "Date", "Symbol", "Color", "Colour Value",
        "Average Crossover", "Final Status",
        "Forca Alma", "New Trigger", 
        "Chand Exit Status", "Change of CE",  # New columns added
        "Next Candle"
    ])
    header.extend(["", ""])

# --- Write to Google Sheet ---
output_ws.clear()

# === Clear existing formatting ===
default_format = CellFormat(
        backgroundColor=Color(1, 1, 1),  # white
        textFormat=TextFormat(bold=False)
    )
format_cell_ranges(output_ws, [('A1:EZ1000', default_format)])
    

output_ws.update([header] + output_data)

# --- Format cell colors ---
green_bg = CellFormat(backgroundColor=Color(0.6, 0.9, 0.6))
red_bg = CellFormat(backgroundColor=Color(1, 0.6, 0.6))
yellow_bg = CellFormat(backgroundColor=Color(1, 1, 0.6))  # Yellow for new triggers
light_green_bg = CellFormat(backgroundColor=Color(0.8, 1, 0.8))  # Light green for CE Long
light_red_bg = CellFormat(backgroundColor=Color(1, 0.8, 0.8))  # Light red for CE Short

data = output_ws.get_all_values()
fmt_ranges = []

for row_idx, row in enumerate(data[1:], start=2):
    for col_idx, cell in enumerate(row, start=1):
        val = cell.strip().lower()
        a1 = gspread.utils.rowcol_to_a1(row_idx, col_idx)
        
        header_col = data[0][col_idx - 1].strip().lower()
        
        if "new trigger" in header_col and val == "triggered new":
            fmt_ranges.append((a1, yellow_bg))
        elif val == "blue" or val == "buy triggered":
            fmt_ranges.append((a1, green_bg))
        elif val == "yellow" or val == "sell triggered":
            fmt_ranges.append((a1, red_bg))
        elif "chand exit status" in header_col:
            if val == "long":
                fmt_ranges.append((a1, light_green_bg))
            elif val == "short":
                fmt_ranges.append((a1, light_red_bg))
        elif "change of ce" in header_col and val == "yes":
            fmt_ranges.append((a1, yellow_bg))

        try:
            num_val = float(cell)
            if "average crossover" in header_col:
                if num_val > 50:
                    fmt_ranges.append((a1, red_bg))
                elif num_val < -50:
                    fmt_ranges.append((a1, green_bg))
        except:
            continue

if fmt_ranges:
    format_cell_ranges(output_ws, fmt_ranges)

completed_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
duration = time.time() - start_time

print("\n=== Execution Summary ===")
print(f"✅ Code completed at     : {completed_time}")
print(f"📅 Data date used        : {today.strftime('%Y-%m-%d')}")
print(f"📄 Input sheet tab name : {INPUT_TAB}")
print(f"📄 Output sheet tab name: {OUTPUT_TAB}")
print(f"⏱️ Total run time       : {duration:.1f} seconds")
print(f"📊 New triggers detected : {len(new_triggers)}")
print(f"💾 Trigger history saved to: {JSON_FILE}")