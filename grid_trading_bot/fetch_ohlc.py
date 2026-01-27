# fetch_ohlc.py - Standalone script to fetch OHLC data from Yahoo Finance
import yfinance as yf
import json
import time
from datetime import datetime, timedelta, timezone

# ========== YAHOO FINANCE DATA AVAILABILITY ==========
# 1m  (1-minute):   Last 30 days ONLY
# 5m  (5-minute):   Last 60 days
# 15m (15-minute):  Last 60 days
# 30m (30-minute):  Last 60 days
# 1h  (Hourly):     Last 730 days (2 years)
# 1d  (Daily):      All historical data
# ====================================================

# ========== HARDCODED INPUTS FOR TESTING ==========
SYMBOL = "bankbees.NS"           # Stock symbol
START_DATE = "2025-12-19"        # Start date (YYYY-MM-DD) - Within last 30 days for 1m
END_DATE = "2026-01-18"          # End date (YYYY-MM-DD) - Within last 30 days for 1m
TIMEFRAME = "1m"                 # Timeframe: Use "1m" for 1-minute backtesting
# ===================================================
# ===================================================

RETRY_ATTEMPTS = 3               # Number of retry attempts
RETRY_DELAY = 2                  # Delay in seconds between retries

def fetch_ohlc_data_chunked(symbol, start_date, end_date, interval="1h"):
    """ 
    Fetch OHLC data from Yahoo Finance (with chunking for large requests)
    
    Parameters:
    - symbol: Stock symbol (e.g., "INFY.NS" for NSE, "AAPL" for US)
    - start_date: Start date in format "YYYY-MM-DD"
    - end_date: End date in format "YYYY-MM-DD"
    - interval: Time interval ("1m", "5m", "15m", "30m", "1h", "1d")
    
    Returns:
    - Dictionary with OHLC data saved to JSON file
    """
    try:
        print(f"📊 Fetching {symbol} data from {start_date} to {end_date} ({interval})...")
        
        # For 1-minute intervals, fetch day by day to avoid timeouts
        if interval == "1m":
            print("⏳ Fetching 1-minute data in daily chunks...")
            all_data = []
            
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            current_date = start
            
            while current_date <= end:
                # Skip weekends (Saturday=5, Sunday=6)
                if current_date.weekday() >= 5:
                    print(f"  Skipping {current_date.strftime('%Y-%m-%d')} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][current_date.weekday()]}) - No trading")
                    current_date += timedelta(days=1)
                    continue
                
                day_end = current_date + timedelta(days=1)
                day_start_str = current_date.strftime("%Y-%m-%d")
                day_end_str = day_end.strftime("%Y-%m-%d")
                
                print(f"  Fetching {day_start_str}...", end="", flush=True)
                
                # Retry logic for network issues
                retry_count = 0
                day_data = None
                
                while retry_count < RETRY_ATTEMPTS:
                    try:
                        day_data = yf.download(
                            symbol, 
                            start=day_start_str, 
                            end=day_end_str, 
                            interval=interval,
                            progress=False,
                            timeout=30
                        )
                        
                        if not day_data.empty:
                            all_data.append(day_data)
                            print(f" ✓ ({len(day_data)} records)")
                        else:
                            print(" ⊘ No data")
                        break
                        
                    except Exception as e:
                        retry_count += 1
                        error_msg = str(e)[:40]
                        
                        if retry_count < RETRY_ATTEMPTS:
                            print(f" ✗ Retry {retry_count}/{RETRY_ATTEMPTS}...", end="", flush=True)
                            time.sleep(RETRY_DELAY)
                        else:
                            print(f" ✗ Failed after {RETRY_ATTEMPTS} attempts")
                
                # Add delay between requests to avoid overwhelming the server
                time.sleep(1)
                current_date = day_end
            
            # Combine all data
            if all_data:
                data = pd.concat(all_data)
            else:
                print("✗ No data fetched for the entire range")
                return None
        else:
            # For other intervals, fetch normally
            data = yf.download(symbol, start=start_date, end=end_date, interval=interval, progress=False)
        
        # Convert to dictionary
        ohlc_dict = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "interval": interval,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data": []
        }
        
        # Extract OHLC values
        for index, row in data.iterrows():
            # Convert UTC timestamp to IST (UTC+5:30)
            utc_time = index.replace(tzinfo=timezone.utc) if index.tzinfo is None else index
            ist_time = utc_time.astimezone(timezone(timedelta(hours=5, minutes=30)))
            
            ohlc_dict["data"].append({
                "timestamp": ist_time.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(row['Open']),
                "high": float(row['High']),
                "low": float(row['Low']),
                "close": float(row['Close']),
                "volume": int(row['Volume']) if 'Volume' in row else 0
            })
        
        # Save to JSON file
        json_filename = f"ohlc_{symbol}_{start_date}_{end_date}_{interval}.json"
        with open(json_filename, 'w') as f:
            json.dump(ohlc_dict, f, indent=4)
        
        print(f"\n✓ OHLC data saved to {json_filename}")
        print(f"  Total records: {len(ohlc_dict['data'])}")
        
        return ohlc_dict
    
    except Exception as e:
        print(f"✗ Error fetching OHLC data: {e}")
        return None

# Needed for chunking
import pandas as pd

if __name__ == "__main__":
    # Run the fetch function
    fetch_ohlc_data_chunked(SYMBOL, START_DATE, END_DATE, TIMEFRAME)
    print("✓ Done! You can now use the JSON file in your main grid_bot.py")


print (
"fetch_ohlc.py executed successfully."
)
