# fetch_ohlc_alphavantage.py - Fetch OHLC data from Alpha Vantage API
import json
import time
from datetime import datetime
import requests

# ========== ALPHA VANTAGE API CONFIGURATION ==========
API_KEY = " X1D0LOT9JZ4I3LWB"                        # Get free key from https://www.alphavantage.co/
# Free key works, replace with your own for better limits (500 calls/day)
# ====================================================

# ========== HARDCODED INPUTS FOR TESTING ==========
SYMBOL = "BANKBEES"                    # Stock symbol (use just the name for NSE)
MARKET = ""                            # Leave empty for NSE stocks
INTERVAL = "1min"                      # "1min", "5min", "15min", "30min", "60min"
OUTPUT_SIZE = "full"                   # "compact" (100 data points) or "full" (all available)
# ===================================================

def fetch_from_alphavantage(symbol, market, interval, output_size="full"):
    """
    Fetch OHLC data from Alpha Vantage API
    
    Parameters:
    - symbol: Stock symbol (e.g., "BANKBEES" for NSE)
    - market: Market exchange (e.g., "NSE" for Indian stocks)
    - interval: "1min", "5min", "15min", "30min", "60min"
    - output_size: "compact" or "full"
    
    Returns:
    - Dictionary with OHLC data or None
    """
    
    print("=" * 60)
    print("Alpha Vantage - Historical OHLC Data Fetcher")
    print("=" * 60)
    
    # Alpha Vantage API endpoint
    base_url = "https://www.alphavantage.co/query"
    
    # For Alpha Vantage, just use the symbol directly (no market suffix)
    full_symbol = symbol
    
    params = {
        "function": "TIME_SERIES_INTRADAY",
        "symbol": full_symbol,
        "interval": interval,
        "outputsize": output_size,
        "apikey": API_KEY,
        "datatype": "json"
    }
    
    print(f"\n📊 Fetching data...")
    print(f"  Symbol: {full_symbol}")
    print(f"  Interval: {interval}")
    print(f"  Output Size: {output_size}")
    
    try:
        # Make API request
        print(f"\n🔗 Calling Alpha Vantage API...")
        response = requests.get(base_url, params=params, timeout=30)
        
        if response.status_code != 200:
            print(f"✗ API Error: {response.status_code}")
            return None
        
        data = response.json()
        
        # Check for API errors
        if "Error Message" in data:
            print(f"✗ API Error: {data['Error Message']}")
            return None
        
        if "Note" in data:
            print(f"⚠️  API Limit: {data['Note']}")
            print(f"   (Free tier: 5 calls/min, 500/day)")
            print(f"   Get a free key: https://www.alphavantage.co/")
            return None
        
        # Parse time series data
        if "Time Series (1min)" in data:
            time_series = data["Time Series (1min)"]
        elif "Time Series (5min)" in data:
            time_series = data["Time Series (5min)"]
        elif "Time Series (15min)" in data:
            time_series = data["Time Series (15min)"]
        elif "Time Series (30min)" in data:
            time_series = data["Time Series (30min)"]
        elif "Time Series (60min)" in data:
            time_series = data["Time Series (60min)"]
        else:
            print(f"✗ No time series data in response")
            print(f"   Available keys: {list(data.keys())}")
            return None
        
        print(f"  ✓ Fetched {len(time_series)} records")
        
        # Convert to our JSON format
        ohlc_dict = {
            "symbol": full_symbol,
            "interval": interval,
            "source": "Alpha Vantage",
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_records": len(time_series),
            "data": []
        }
        
        # Process OHLC values (Alpha Vantage returns in reverse chronological order)
        for timestamp, values in sorted(time_series.items()):
            ohlc_dict["data"].append({
                "timestamp": timestamp,
                "open": float(values.get("1. open", 0)),
                "high": float(values.get("2. high", 0)),
                "low": float(values.get("3. low", 0)),
                "close": float(values.get("4. close", 0)),
                "volume": int(float(values.get("5. volume", 0)))
            })
        
        # Sort by timestamp (oldest first)
        ohlc_dict["data"].sort(key=lambda x: x["timestamp"])
        
        return ohlc_dict
    
    except requests.exceptions.Timeout:
        print(f"✗ Request timeout")
        return None
    except Exception as e:
        print(f"✗ Error: {e}")
        return None

def save_to_json(ohlc_dict):
    """
    Save OHLC data to JSON file
    """
    if ohlc_dict is None:
        return None
    
    filename = f"ohlc_alphavantage_{ohlc_dict['symbol'].replace('.', '_')}_{ohlc_dict['interval']}.json"
    
    try:
        with open(filename, 'w') as f:
            json.dump(ohlc_dict, f, indent=4)
        
        print(f"\n✓ Data saved to {filename}")
        print(f"  Records: {ohlc_dict['total_records']}")
        return filename
    except Exception as e:
        print(f"✗ Error saving file: {e}")
        return None

if __name__ == "__main__":
    print("\n⚠️  First time? Get free API key:")
    print("   1. Go to https://www.alphavantage.co/")
    print("   2. Click 'GET FREE API KEY'")
    print("   3. Replace API_KEY in this script\n")
    
    if API_KEY == "demo":
        print("📌 Using demo API key (limited)")
        print("   Replace with your free key for better results\n")
    
    # Fetch data
    ohlc_data = fetch_from_alphavantage(SYMBOL, MARKET, INTERVAL, OUTPUT_SIZE)
    
    # Save to JSON
    if ohlc_data:
        json_file = save_to_json(ohlc_data)
        print("\n✓ Done! Ready for backtesting")
    else:
        print("\n✗ Failed to fetch data")
