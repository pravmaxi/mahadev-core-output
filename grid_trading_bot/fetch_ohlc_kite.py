# fetch_ohlc_kite.py - Fetch historical OHLC data from Zerodha Kite API
import json
from datetime import datetime, timedelta, timezone
from kiteconnect import KiteConnect

# ========== KITE API CONFIGURATION ==========
# Get these from your Zerodha settings
API_KEY = "ud3fa01mv5n39ra2"                    # Your API key
API_SECRET = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"        # Get from Zerodha Dashboard → Settings → API Tokens
# ==========================================

# ========== HARDCODED INPUTS FOR TESTING ==========
SYMBOL = "BANKBEES"                    # Trading symbol (NSE only, without .NS)
EXCHANGE = "NSE"                       # Exchange: NSE or BSE
START_DATE = "2025-12-01"              # Start date (YYYY-MM-DD)
END_DATE = "2026-01-18"                # End date (YYYY-MM-DD)
INTERVAL = "minute"                    # Interval: "minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"
# ===================================================

def login_kite():
    """
    Login to Kite API
    Returns KiteConnect object
    """
    if API_SECRET == "PUT_YOUR_API_SECRET_HERE":
        print("❌ ERROR: API_SECRET is not set!")
        print("\n📋 Steps to fix:")
        print("  1. Go to https://kite.zerodha.com")
        print("  2. Settings → API Tokens (left sidebar)")
        print("  3. Copy your API Secret")
        print("  4. Update API_SECRET in this script\n")
        return None, None
    
    kite = KiteConnect(api_key=API_KEY)
    
    # Get login URL
    login_url = kite.login_url()
    print(f"🔗 Login URL: {login_url}")
    print("\n⚠️  Please visit the URL above and complete login")
    print("📋 After login, you'll be redirected. Copy the 'request_token' from the URL\n")
    
    # Get request token from user
    request_token = input("Enter the request_token from redirect URL: ").strip()
    
    # Get access token
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        print(f"✓ Login successful!")
        print(f"  Access Token: {session['access_token']}")
        return kite, session['access_token']
    except Exception as e:
        print(f"✗ Login failed: {e}")
        return None, None

def search_instrument(kite, symbol, exchange):
    """
    Search for instrument and get its token
    """
    try:
        print(f"🔍 Searching for {exchange}:{symbol}...")
        instruments = kite.instruments(exchange)
        
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                print(f"  ✓ Found: {inst['name']} (Token: {inst['instrument_token']})")
                return inst['instrument_token']
        
        print(f"  ✗ Symbol not found: {symbol}")
        return None
    except Exception as e:
        print(f"  ✗ Error searching: {e}")
        return None

def fetch_historical_data(kite, instrument_token, from_date, to_date, interval):
    """
    Fetch historical OHLC data from Kite API
    
    Parameters:
    - kite: KiteConnect object
    - instrument_token: Instrument token
    - from_date: Start date (YYYY-MM-DD)
    - to_date: End date (YYYY-MM-DD)
    - interval: "minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"
    """
    try:
        print(f"\n📊 Fetching historical data...")
        print(f"  Period: {from_date} to {to_date}")
        print(f"  Interval: {interval}")
        
        # Fetch data
        data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )
        
        print(f"  ✓ Fetched {len(data)} records")
        
        # Convert to JSON format
        ohlc_dict = {
            "symbol": SYMBOL,
            "exchange": EXCHANGE,
            "from_date": from_date,
            "to_date": to_date,
            "interval": interval,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_records": len(data),
            "data": []
        }
        
        # Process OHLC values
        for candle in data:
            ohlc_dict["data"].append({
                "timestamp": candle['date'].strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(candle['open']),
                "high": float(candle['high']),
                "low": float(candle['low']),
                "close": float(candle['close']),
                "volume": int(candle['volume']) if candle['volume'] else 0,
                "oi": int(candle['oi']) if candle.get('oi') else 0
            })
        
        return ohlc_dict
    
    except Exception as e:
        print(f"  ✗ Error fetching data: {e}")
        return None

def save_to_json(ohlc_dict, filename=None):
    """
    Save OHLC data to JSON file
    """
    if filename is None:
        filename = f"ohlc_kite_{ohlc_dict['symbol']}_{ohlc_dict['from_date']}_{ohlc_dict['to_date']}_{ohlc_dict['interval']}.json"
    
    try:
        with open(filename, 'w') as f:
            json.dump(ohlc_dict, f, indent=4)
        print(f"\n✓ Data saved to {filename}")
        return filename
    except Exception as e:
        print(f"✗ Error saving file: {e}")
        return None

if __name__ == "__main__":
    print("=" * 60)
    print("Zerodha Kite API - Historical OHLC Data Fetcher")
    print("=" * 60)
    
    # Login
    kite, access_token = login_kite()
    if not kite:
        exit(1)
    
    # Search for instrument
    instrument_token = search_instrument(kite, SYMBOL, EXCHANGE)
    if not instrument_token:
        exit(1)
    
    # Fetch historical data
    ohlc_data = fetch_historical_data(kite, instrument_token, START_DATE, END_DATE, INTERVAL)
    if not ohlc_data:
        exit(1)
    
    # Save to JSON
    json_file = save_to_json(ohlc_data)
    
    print("\n✓ Done! Historical data is ready for backtesting")
