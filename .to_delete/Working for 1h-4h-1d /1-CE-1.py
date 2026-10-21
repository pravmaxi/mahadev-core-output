import pandas as pd
import numpy as np
import gspread
from datetime import datetime, timedelta
from oauth2client.service_account import ServiceAccountCredentials
import yfinance as yf
import json
from typing import Dict, List, Tuple, Optional
from gspread_formatting import CellFormat, Color, format_cell_ranges, TextFormat

# --- USER CONFIGURATION ---
today = datetime.strptime("2025-12-19", "%Y-%m-%d")  # Set date manually
range_days = 15  # Number of days to look back
INPUT_TAB = "input2"
OUTPUT_TAB = "CE_output"
JSON_FILE = "trigger_history.json"
SHOW_ALL_STOCKS = True  # Set to True to show all stocks, False to show only final status stocks

# TOGGLE: Set to True for Heikin-Ashi candles, False for normal candles
USE_HEIKIN_ASHI = True  # Toggle between Heikin-Ashi and normal candles

# Timeframe configuration - USER SELECTABLE
SELECTED_TIMEFRAME = "1d"  # Options: "5m", "15m", "30m", "1h", "4h", "1d"

TIMEFRAMES = [SELECTED_TIMEFRAME]  # Only analyze the selected timeframe
AUTO_PERIOD_MAP = {
    "5m": "15d",
    "15m": "30d",
    "30m": "60d",
    "1h": "60d",
    "4h": "1y",
    "1d": "1y"
}

# Market hours
MARKET_START_TIME = "09:15"
MARKET_END_TIME = "15:30"

# Chandelier Exit configuration
CE_LENGTH = 22
CE_MULTIPLIER = 3.0
CE_USE_CLOSE = True

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

def get_time_slots(timeframe: str) -> List[str]:
    """Generate time slots for the given timeframe"""
    if timeframe == "1d":
        return ["Daily"]  # Only one time slot for daily data
    
    # Parse market hours
    start_hour, start_minute = map(int, MARKET_START_TIME.split(':'))
    end_hour, end_minute = map(int, MARKET_END_TIME.split(':'))
    
    time_slots = []
    current_time = datetime.now().replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end_time = datetime.now().replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    
    # Convert timeframe to minutes
    if timeframe.endswith('m'):
        interval_minutes = int(timeframe[:-1])
    elif timeframe.endswith('h'):
        interval_minutes = int(timeframe[:-1]) * 60
    else:
        interval_minutes = 60  # Default to 1 hour
    
    while current_time <= end_time:
        time_slots.append(current_time.strftime("%H:%M"))
        current_time += timedelta(minutes=interval_minutes)
    
    return time_slots

def calculate_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Convert regular OHLC data to Heikin-Ashi candles"""
    ha_df = df.copy()
    
    ha_df['HA_Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
    
    # Initialize HA_Open with the first value
    ha_open = [(df['Open'].iloc[0] + df['Close'].iloc[0]) / 2]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i-1] + ha_df['HA_Close'].iloc[i-1]) / 2)
    
    ha_df['HA_Open'] = ha_open
    ha_df['HA_High'] = ha_df[['HA_Open', 'HA_Close', 'High']].max(axis=1)
    ha_df['HA_Low'] = ha_df[['HA_Open', 'HA_Close', 'Low']].min(axis=1)
    
    return ha_df

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range"""
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    
    return true_range.rolling(window=period).mean()

def calculate_chandelier_exit(df: pd.DataFrame, length: int = 22, mult: float = 3.0, 
                             use_close: bool = True) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Chandelier Exit indicator
    
    Returns:
        long_stop: Chandelier Exit for long positions
        short_stop: Chandelier Exit for short positions
        direction: Current direction (1 for long, -1 for short)
    """
    # Calculate ATR
    atr_val = mult * calculate_atr(df, length)
    
    # Calculate highest high and lowest low
    if use_close:
        highest_close = df['Close'].rolling(window=length).max()
        lowest_close = df['Close'].rolling(window=length).min()
    else:
        highest_close = df['High'].rolling(window=length).max()
        lowest_close = df['Low'].rolling(window=length).min()
    
    # Initialize long and short stops
    long_stop = highest_close - atr_val
    short_stop = lowest_close + atr_val
    
    # Convert to pandas Series to preserve index
    long_stop = pd.Series(long_stop, index=df.index)
    short_stop = pd.Series(short_stop, index=df.index)
    
    # Adjust stops based on previous values
    long_stop_prev = long_stop.shift(1)
    short_stop_prev = short_stop.shift(1)
    
    # Adjust long stop
    long_condition = df['Close'].shift(1) > long_stop_prev
    for i in range(1, len(long_stop)):
        if long_condition.iloc[i]:
            long_stop.iloc[i] = max(long_stop.iloc[i], long_stop_prev.iloc[i])
    
    # Adjust short stop
    short_condition = df['Close'].shift(1) < short_stop_prev
    for i in range(1, len(short_stop)):
        if short_condition.iloc[i]:
            short_stop.iloc[i] = min(short_stop.iloc[i], short_stop_prev.iloc[i])
    
    # Calculate direction
    direction = np.ones(len(df))
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > short_stop_prev.iloc[i]:
            direction[i] = 1
        elif df['Close'].iloc[i] < long_stop_prev.iloc[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
    
    return long_stop, short_stop, pd.Series(direction, index=df.index)

def get_stock_data(symbol: str, timeframe: str, period: str) -> Optional[pd.DataFrame]:
    """Fetch stock data for given symbol and timeframe"""
    try:
        # Add .NS for NSE stocks if not already present
        if not symbol.endswith('.NS') and not symbol.endswith('.BO'):
            symbol += '.NS'
        
        # Download data
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=timeframe)
        
        if df.empty:
            print(f"No data found for {symbol} on timeframe {timeframe}")
            return None
        
        # For daily data, don't filter market hours
        if timeframe != "1d":  # Only filter for intraday timeframes
            df = df.between_time('09:15', '15:30')
        
        if df.empty:
            print(f"No data found for {symbol} on timeframe {timeframe}")
            return None
        
        # Convert to Heikin-Ashi if enabled
        if USE_HEIKIN_ASHI:
            df = calculate_heikin_ashi(df)
            # Use Heikin-Ashi values for calculation
            df_ha = df.copy()
            df_ha['Open'] = df['HA_Open']
            df_ha['High'] = df['HA_High']
            df_ha['Low'] = df['HA_Low']
            df_ha['Close'] = df['HA_Close']
            df = df_ha
        
        return df
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None

def analyze_stock(symbol: str, timeframe: str) -> Dict[str, Dict]:
    """Analyze a stock for the selected timeframe"""
    results = {}
    
    period = AUTO_PERIOD_MAP.get(timeframe, "30d")
    
    # Get stock data
    df = get_stock_data(symbol, timeframe, period)
    if df is None or len(df) < CE_LENGTH + 5:
        return {"error": "Insufficient data"}
    
    try:
        # Calculate Chandelier Exit
        long_stop, short_stop, direction = calculate_chandelier_exit(
            df, CE_LENGTH, CE_MULTIPLIER, CE_USE_CLOSE
        )
        
        # Use the hardcoded date
        target_date = today.date()
        
        # Filter for the target date
        target_data = df[df.index.date == target_date]
        
        if target_data.empty:
            print(f"No data found for {symbol} on target date {target_date}")
            return {"error": f"No data for {target_date}"}
        
        # For daily timeframe, we only have one data point per day
        if timeframe == "1d":
            # Get the single daily data point
            timestamp, row = next(iter(target_data.iterrows()))
            
            # Get current values
            current_close = row['Close']
            current_long_stop = long_stop.loc[timestamp] if timestamp in long_stop.index and not pd.isna(long_stop.loc[timestamp]) else 0
            current_short_stop = short_stop.loc[timestamp] if timestamp in short_stop.index and not pd.isna(short_stop.loc[timestamp]) else 0
            current_direction = direction.loc[timestamp] if timestamp in direction.index and not pd.isna(direction.loc[timestamp]) else 0
            
            # Get previous day's direction for change detection
            prev_day_data = df[df.index.date < target_date]
            if not prev_day_data.empty:
                prev_timestamp = prev_day_data.index[-1]
                prev_direction = direction.loc[prev_timestamp] if prev_timestamp in direction.index and not pd.isna(direction.loc[prev_timestamp]) else current_direction
            else:
                prev_direction = current_direction
            
            # Determine signal and change
            if current_direction == 1 and current_close > current_long_stop:
                signal = "BULLISH"
                change = "BULLISH TRIGGER" if prev_direction != 1 else "NO CHANGE"
            elif current_direction == -1 and current_close < current_short_stop:
                signal = "BEARISH"
                change = "BEARISH TRIGGER" if prev_direction != -1 else "NO CHANGE"
            else:
                signal = "NEUTRAL"
                change = "NEUTRAL" if prev_direction == current_direction else f"CHANGED FROM {'BULLISH' if prev_direction == 1 else 'BEARISH'}"
            
            # Store results for daily time slot
            results["Daily"] = {
                "stock_name": symbol.replace('.NS', ''),
                "date": timestamp.strftime("%Y-%m-%d"),
                "time": "15:30",  # Use market close time for daily data
                "close": round(current_close, 2),
                "long_stop": round(current_long_stop, 2),
                "short_stop": round(current_short_stop, 2),
                "direction": int(current_direction),
                "signal": signal,
                "change": change,
                "candle_type": "Heikin-Ashi" if USE_HEIKIN_ASHI else "Normal"
            }
        
        else:
            # For intraday timeframes, analyze each candle
            for idx, (timestamp, row) in enumerate(target_data.iterrows()):
                candle_time = timestamp.strftime("%H:%M")
                
                # Get current values
                current_close = row['Close']
                current_long_stop = long_stop.loc[timestamp] if timestamp in long_stop.index and not pd.isna(long_stop.loc[timestamp]) else 0
                current_short_stop = short_stop.loc[timestamp] if timestamp in short_stop.index and not pd.isna(short_stop.loc[timestamp]) else 0
                current_direction = direction.loc[timestamp] if timestamp in direction.index and not pd.isna(direction.loc[timestamp]) else 0
                
                # Get previous values for change detection
                if idx > 0:
                    prev_timestamp = target_data.index[idx-1]
                    prev_direction = direction.loc[prev_timestamp] if prev_timestamp in direction.index and not pd.isna(direction.loc[prev_timestamp]) else current_direction
                else:
                    # For the first candle of the day, use the last candle from previous day
                    prev_day_data = df[df.index.date < target_date]
                    if not prev_day_data.empty:
                        prev_timestamp = prev_day_data.index[-1]
                        prev_direction = direction.loc[prev_timestamp] if prev_timestamp in direction.index and not pd.isna(direction.loc[prev_timestamp]) else current_direction
                    else:
                        prev_direction = current_direction
                
                # Determine signal and change
                if current_direction == 1 and current_close > current_long_stop:
                    signal = "BULLISH"
                    change = "BULLISH TRIGGER" if prev_direction != 1 else "NO CHANGE"
                elif current_direction == -1 and current_close < current_short_stop:
                    signal = "BEARISH"
                    change = "BEARISH TRIGGER" if prev_direction != -1 else "NO CHANGE"
                else:
                    signal = "NEUTRAL"
                    change = "NEUTRAL" if prev_direction == current_direction else f"CHANGED FROM {'BULLISH' if prev_direction == 1 else 'BEARISH'}"
                
                # Store results for this time slot
                results[candle_time] = {
                    "stock_name": symbol.replace('.NS', ''),
                    "date": timestamp.strftime("%Y-%m-%d"),
                    "time": candle_time,
                    "close": round(current_close, 2),
                    "long_stop": round(current_long_stop, 2),
                    "short_stop": round(current_short_stop, 2),
                    "direction": int(current_direction),
                    "signal": signal,
                    "change": change,
                    "candle_type": "Heikin-Ashi" if USE_HEIKIN_ASHI else "Normal"
                }
        
        return results
        
    except Exception as e:
        print(f"Error calculating CE for {symbol} on {timeframe}: {e}")
        return {"error": f"Calculation error: {str(e)}"}

def load_stock_list() -> List[str]:
    """Load stock list from Google Sheets"""
    try:
        stocks = input_ws.col_values(1)[1:]  # Skip header row
        return [stock.strip() for stock in stocks if stock.strip()]
    except Exception as e:
        print(f"Error loading stock list: {e}")
        return []

def apply_color_formatting(worksheet):
    """Apply color formatting to the worksheet after data is written"""
    try:
        # Define color formats
        green_bg = CellFormat(backgroundColor=Color(0.717, 0.882, 0.717))  # Light green
        red_bg = CellFormat(backgroundColor=Color(1, 0.8, 0.8))  # Light red
        gray_bg = CellFormat(backgroundColor=Color(0.95, 0.95, 0.95))  # Light gray
        
        # Get all data from the worksheet
        data = worksheet.get_all_values()
        fmt_ranges = []
        
        # Find the "Change of CE" columns
        header_row = data[0]
        change_ce_columns = []
        
        for col_idx, header in enumerate(header_row, start=1):
            if "Change of CE" in header:
                change_ce_columns.append(col_idx)
        
        # Apply formatting to each "Change of CE" column
        for col_idx in change_ce_columns:
            for row_idx, row in enumerate(data[1:], start=2):  # Skip header row
                if row_idx - 2 < len(data) and col_idx - 1 < len(row):
                    cell_value = row[col_idx - 1].strip()
                    a1 = gspread.utils.rowcol_to_a1(row_idx, col_idx)
                    
                    if cell_value == "BULLISH TRIGGER":
                        fmt_ranges.append((a1, green_bg))
                    elif cell_value == "BEARISH TRIGGER":
                        fmt_ranges.append((a1, red_bg))
                    elif cell_value == "NO CHANGE" or cell_value == "NEUTRAL":
                        fmt_ranges.append((a1, gray_bg))
        
        # Apply all formatting
        if fmt_ranges:
            format_cell_ranges(worksheet, fmt_ranges)
            print(f"✅ Applied color formatting to {len(fmt_ranges)} cells")
        else:
            print("⚠️ No cells found for color formatting")
            
    except Exception as e:
        print(f"⚠️ Error applying color formatting: {e}")

def get_triggered_stocks_summary(results: Dict[str, Dict], time_slots: List[str]) -> List[List[str]]:
    """Get a summary of triggered stocks time-wise for Google Sheets"""
    triggered_summary = []
    
    # Add header for triggered stocks summary
    triggered_summary.append(["🎯 TRIGGERED STOCKS SUMMARY"])
    triggered_summary.append(["Time", "Stock", "Trigger Type", "Price", "CE Status", "Signal"])
    triggered_summary.append([])  # Empty row
    
    for stock_name, stock_data in results.items():
        for time_slot in time_slots:
            if time_slot in stock_data and "error" not in stock_data[time_slot]:
                data = stock_data[time_slot]
                if data["change"] in ["BULLISH TRIGGER", "BEARISH TRIGGER"]:
                    triggered_summary.append([
                        data['time'],
                        stock_name,
                        data['change'],
                        data['close'],
                        "LONG" if data['direction'] == 1 else "SHORT" if data['direction'] == -1 else "NEUTRAL",
                        data['signal']
                    ])
    
    # Add total count
    if len(triggered_summary) > 3:  # If there are triggered stocks (beyond headers)
        triggered_summary.append([])  # Empty row
        triggered_summary.append(["Total Triggers:", len(triggered_summary) - 3])
    
    return triggered_summary

def save_results_to_sheet(results: Dict[str, Dict], time_slots: List[str]):
    """Save analysis results to Google Sheets with partitioned columns"""
    try:
        # Prepare headers
        headers = []
        for time_slot in time_slots:
            headers.extend([
                f"{time_slot}_Date",
                f"{time_slot}_Time", 
                f"{time_slot}_Stock Name", 
                f"{time_slot}_CE Status", 
                f"{time_slot}_Change of CE",
                "", ""  # Two empty columns for gap
            ])
        
        # Remove the last two empty columns
        headers = headers[:-2]
        
        output_data = [headers]
        
        # Get all stock names
        stock_names = list(results.keys())
        
        # Create rows for each stock
        for stock_name in stock_names:
            row = []
            for time_slot in time_slots:
                if stock_name in results and time_slot in results[stock_name] and "error" not in results[stock_name][time_slot]:
                    data = results[stock_name][time_slot]
                    row.extend([
                        data["date"],
                        data["time"],
                        data["stock_name"],
                        data["signal"],
                        data["change"],
                        "", ""  # Two empty columns for gap
                    ])
                else:
                    # Add empty values for missing data
                    row.extend(["N/A", "N/A", "N/A", "N/A", "N/A", "", ""])
            
            # Remove the last two empty columns
            row = row[:-2]
            output_data.append(row)
        
        # Add summary row with candle type info
        summary_row = ["Candle Type:"]
        for time_slot in time_slots:
            summary_row.extend([f"{'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'}"] + [""] * 6)
        summary_row = summary_row[:-2]
        output_data.append(summary_row)
        
        # Add two empty rows for separation
        output_data.append([])
        output_data.append([])
        
        # Get triggered stocks summary and add to output
        triggered_summary = get_triggered_stocks_summary(results, time_slots)
        output_data.extend(triggered_summary)
        
        # Clear existing data and update with new results
        output_ws.clear()
        output_ws.update(output_data)
        
        # Apply color formatting after data is written
        apply_color_formatting(output_ws)
        
        print(f"Results successfully saved to Google Sheets for timeframe: {SELECTED_TIMEFRAME}")
        print(f"Candle type: {'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'}")
        print(f"Time slots analyzed: {', '.join(time_slots)}")
        print(f"Analysis date: {today.strftime('%Y-%m-%d')}")
        
        # Return triggered stocks count
        return len(triggered_summary) - 3 if len(triggered_summary) > 3 else 0
        
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")
        return 0

def main():
    """Main function to run the analysis"""
    print(f"Starting Chandelier Exit analysis for timeframe: {SELECTED_TIMEFRAME}...")
    print(f"Using {'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'} candles")
    print(f"Analysis date: {today.strftime('%Y-%m-%d')}")
    
    # Generate time slots for the selected timeframe
    time_slots = get_time_slots(SELECTED_TIMEFRAME)
    print(f"Time slots: {time_slots}")
    
    # Load stock list
    stocks = load_stock_list()
    if not stocks:
        print("No stocks found in the input sheet")
        return
    
    print(f"Analyzing {len(stocks)} stocks on {SELECTED_TIMEFRAME} timeframe")
    
    # Analyze each stock
    results = {}
    for i, symbol in enumerate(stocks, 1):
        print(f"Analyzing {symbol} ({i}/{len(stocks)})")
        try:
            stock_results = analyze_stock(symbol, SELECTED_TIMEFRAME)
            results[symbol] = stock_results
        except Exception as e:
            print(f"Error analyzing {symbol}: {e}")
            results[symbol] = {"error": f"Analysis failed: {str(e)}"}
    
    # Save results to Google Sheets and get triggered stocks count
    triggered_count = save_results_to_sheet(results, time_slots)
    
    # Print summary
    if triggered_count > 0:
        print(f"\n🎯 Found {triggered_count} triggered stocks - Summary added to Google Sheets")
    else:
        print("\n📊 No triggers found in this analysis")
    
    print("\nAnalysis completed successfully")

if __name__ == "__main__":
    main()