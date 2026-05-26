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
today = datetime.strptime("2026-04-23", "%Y-%m-%d")  # Set date manually
range_days = 15  # Number of days to look back

# Timeframe configuration - USER SELECTABLE
SELECTED_TIMEFRAME = "1d"  # Options: "5m", "15m", "30m", "1h", "4h", "1d"
INPUT_TAB = "input2"
OUTPUT_TAB = f"CE_output_{SELECTED_TIMEFRAME}"
JSON_FILE = "trigger_history.json"
SHOW_ALL_STOCKS = True  # Set to True to show all stocks, False to show only final status stocks

# TOGGLE: Set to True for Heikin-Ashi candles, False for normal candles
USE_HEIKIN_ASHI = True  # Toggle between Heikin-Ashi and normal candles

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

# Ensure output worksheet exists
try:
    output_ws = sheet.worksheet(OUTPUT_TAB)
except gspread.exceptions.WorksheetNotFound:
    print(f"Worksheet '{OUTPUT_TAB}' not found. Creating a new worksheet.")
    output_ws = sheet.add_worksheet(title=OUTPUT_TAB, rows="100", cols="20")

def get_date_range(start_date: datetime, days_back: int) -> List[datetime]:
    """Generate list of dates to analyze"""
    dates = []
    for i in range(days_back):
        current_date = start_date - timedelta(days=i)
        # Skip weekends (Saturday=5, Sunday=6)
        if current_date.weekday() < 5:
            dates.append(current_date)
    return sorted(dates)  # Return in chronological order

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

def analyze_stock_for_date(symbol: str, timeframe: str, target_date: datetime) -> Dict[str, Dict]:
    """Analyze a stock for a specific date"""
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
        
        # Filter for the target date
        target_data = df[df.index.date == target_date.date()]
        
        if target_data.empty:
            return {"error": f"No data for {target_date.date()}"}
        
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
            prev_day_data = df[df.index.date < target_date.date()]
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
                    prev_day_data = df[df.index.date < target_date.date()]
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
        print(f"Error calculating CE for {symbol} on {timeframe} for date {target_date}: {e}")
        return {"error": f"Calculation error: {str(e)}"}

def analyze_stock_multiple_days(symbol: str, timeframe: str, dates: List[datetime]) -> Dict[str, Dict]:
    """Analyze a stock for multiple dates"""
    results = {}
    
    for date in dates:
        date_str = date.strftime("%Y-%m-%d")
        print(f"  Analyzing {date_str}")
        
        date_results = analyze_stock_for_date(symbol, timeframe, date)
        results[date_str] = date_results
    
    return results

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
        for row_idx, row in enumerate(data):
            for col_idx, cell_value in enumerate(row):
                if "Change of CE" in str(cell_value):
                    # This is a header, format the entire column below
                    for data_row_idx in range(row_idx + 1, len(data)):
                        if col_idx < len(data[data_row_idx]):
                            cell_val = data[data_row_idx][col_idx].strip()
                            a1 = gspread.utils.rowcol_to_a1(data_row_idx + 1, col_idx + 1)
                            
                            if cell_val == "BULLISH TRIGGER":
                                fmt_ranges.append((a1, green_bg))
                            elif cell_val == "BEARISH TRIGGER":
                                fmt_ranges.append((a1, red_bg))
                            elif cell_val == "NO CHANGE" or cell_val == "NEUTRAL":
                                fmt_ranges.append((a1, gray_bg))
        
        # Apply all formatting
        if fmt_ranges:
            format_cell_ranges(worksheet, fmt_ranges)
            print(f"✅ Applied color formatting to {len(fmt_ranges)} cells")
        else:
            print("⚠️ No cells found for color formatting")
            
    except Exception as e:
        print(f"⚠️ Error applying color formatting: {e}")

def save_results_to_sheet(results: Dict[str, Dict], dates: List[datetime], time_slots: List[str]):
    """Save analysis results to Google Sheets with date-wise partitioning"""
    try:
        # Clear existing data
        output_ws.clear()
        
        # Prepare headers - Date-wise partitioning
        headers = ["Stock Name"]
        
        for date in dates:
            date_str = date.strftime("%Y-%m-%d")
            for time_slot in time_slots:
                headers.extend([
                    f"{date_str} {time_slot} - CE Status",
                    f"{date_str} {time_slot} - Change of CE",
                    f"{date_str} {time_slot} - Price",
                    ""  # Empty column for gap
                ])
            # Remove the last empty column and add a bigger gap between dates
            headers = headers[:-1]
            headers.extend(["", ""])  # Two empty columns between dates
        
        # Remove the last two empty columns
        headers = headers[:-2]
        
        output_data = [headers]
        
        # Get all stock names
        stock_names = list(results.keys())
        
        # Create rows for each stock
        for stock_name in stock_names:
            row = [stock_name]
            
            for date in dates:
                date_str = date.strftime("%Y-%m-%d")
                
                for time_slot in time_slots:
                    if (date_str in results[stock_name] and 
                        time_slot in results[stock_name][date_str] and 
                        "error" not in results[stock_name][date_str][time_slot]):
                        
                        data = results[stock_name][date_str][time_slot]
                        row.extend([
                            data["signal"],
                            data["change"],
                            data["close"],
                            ""  # Empty column for gap
                        ])
                    else:
                        # Add empty values for missing data
                        row.extend(["N/A", "N/A", "N/A", ""])
                
                # Remove the last empty column and add bigger gap between dates
                row = row[:-1]
                row.extend(["", ""])
            
            # Remove the last two empty columns
            row = row[:-2]
            output_data.append(row)
        
        # Add summary information
        output_data.append([])
        output_data.append(["Analysis Summary"])
        output_data.append([f"Timeframe: {SELECTED_TIMEFRAME}"])
        output_data.append([f"Candle Type: {'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'}"])
        output_data.append([f"Analysis Period: {dates[0].strftime('%Y-%m-%d')} to {dates[-1].strftime('%Y-%m-%d')}"])
        output_data.append([f"Total Stocks Analyzed: {len(stock_names)}"])
        output_data.append([f"Total Days Analyzed: {len(dates)}"])
        
        # Update the sheet with all data
        output_ws.update(output_data)
        
        # Apply color formatting after data is written
        apply_color_formatting(output_ws)
        
        print(f"Results successfully saved to Google Sheets")
        print(f"Timeframe: {SELECTED_TIMEFRAME}")
        print(f"Candle type: {'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'}")
        print(f"Analysis period: {dates[0].strftime('%Y-%m-%d')} to {dates[-1].strftime('%Y-%m-%d')}")
        print(f"Total stocks: {len(stock_names)}, Total days: {len(dates)}")
        
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")

def get_triggered_stocks_summary(results: Dict[str, Dict], dates: List[datetime], time_slots: List[str]) -> List[List[str]]:
    """Get a summary of triggered stocks for all dates"""
    triggered_summary = []
    
    # Add header for triggered stocks summary
    triggered_summary.append(["🎯 TRIGGERED STOCKS SUMMARY"])
    triggered_summary.append(["Date", "Time", "Stock", "Trigger Type", "Price", "Signal"])
    triggered_summary.append([])  # Empty row
    
    trigger_count = 0
    
    for stock_name, stock_data in results.items():
        for date in dates:
            date_str = date.strftime("%Y-%m-%d")
            if date_str in stock_data:
                for time_slot in time_slots:
                    if (time_slot in stock_data[date_str] and 
                        "error" not in stock_data[date_str][time_slot]):
                        
                        data = stock_data[date_str][time_slot]
                        if data["change"] in ["BULLISH TRIGGER", "BEARISH TRIGGER"]:
                            triggered_summary.append([
                                date_str,
                                data['time'],
                                stock_name,
                                data['change'],
                                data['close'],
                                data['signal']
                            ])
                            trigger_count += 1
    
    # Add total count
    if trigger_count > 0:
        triggered_summary.append([])  # Empty row
        triggered_summary.append(["Total Triggers:", trigger_count])
    
    return triggered_summary, trigger_count

def main():
    """Main function to run the analysis"""
    print(f"Starting Chandelier Exit analysis for {range_days} days...")
    print(f"Timeframe: {SELECTED_TIMEFRAME}")
    print(f"Using {'Heikin-Ashi' if USE_HEIKIN_ASHI else 'Normal'} candles")
    print(f"Base date: {today.strftime('%Y-%m-%d')}")
    
    # Generate dates to analyze
    dates = get_date_range(today, range_days)
    if not dates:
        print("No valid trading days found in the specified range")
        return
    
    print(f"Analyzing {len(dates)} trading days: {[d.strftime('%Y-%m-%d') for d in dates]}")
    
    # Generate time slots for the selected timeframe
    time_slots = get_time_slots(SELECTED_TIMEFRAME)
    print(f"Time slots: {time_slots}")
    
    # Load stock list
    stocks = load_stock_list()
    if not stocks:
        print("No stocks found in the input sheet")
        return
    
    print(f"Analyzing {len(stocks)} stocks over {len(dates)} days")
    
    # Analyze each stock for all dates
    results = {}
    total_analysis = len(stocks) * len(dates)
    current_analysis = 0
    
    for i, symbol in enumerate(stocks, 1):
        print(f"Analyzing {symbol} ({i}/{len(stocks)})")
        try:
            stock_results = analyze_stock_multiple_days(symbol, SELECTED_TIMEFRAME, dates)
            results[symbol] = stock_results
            current_analysis += len(dates)
            print(f"Progress: {current_analysis}/{total_analysis} analyses completed")
        except Exception as e:
            print(f"Error analyzing {symbol}: {e}")
            # Create error entries for all dates
            error_results = {}
            for date in dates:
                error_results[date.strftime("%Y-%m-%d")] = {"error": f"Analysis failed: {str(e)}"}
            results[symbol] = error_results
            current_analysis += len(dates)
    
    # Save results to Google Sheets
    save_results_to_sheet(results, dates, time_slots)
    
    # Get triggered stocks summary
    triggered_summary, trigger_count = get_triggered_stocks_summary(results, dates, time_slots)
    
    # Add triggered summary to the sheet (starting from row after the main data)
    if trigger_count > 0:
        try:
            # Find the next empty row after main data
            main_data_rows = len(results) + 10  # Approximate main data rows + summary
            output_ws.update(f'A{main_data_rows}', triggered_summary)
            print(f"🎯 Triggered stocks summary added to the sheet")
        except Exception as e:
            print(f"Warning: Could not add triggered summary to sheet: {e}")
    
    # Print final summary
    print(f"\n📊 ANALYSIS COMPLETED SUCCESSFULLY")
    print(f"📈 Stocks analyzed: {len(stocks)}")
    print(f"📅 Days analyzed: {len(dates)}")
    print(f"🎯 Total triggers found: {trigger_count}")
    print(f"💾 Results saved to Google Sheets tab: {OUTPUT_TAB}")

if __name__ == "__main__":
    main()