# fetch_ohlc_kite_simple.py - Fetch OHLC data using Kite API
import json
from datetime import datetime
from kiteconnect import KiteConnect

# ========== KITE API CONFIGURATION ==========
API_KEY = "ud3fa01mv5n39ra2"        # Your API key
API_SECRET = ""                    # Your API secret (ask Zerodha support)
# ==========================================

# ========== HARDCODED INPUTS ==========
SYMBOL_TOKEN = 3045633              # Bankbees token (find from kite.instruments())
START_DATE = "2025-12-01"           # Start date (YYYY-MM-DD)
END_DATE = "2026-01-18"             # End date (YYYY-MM-DD)
INTERVAL = "minute"                 # Interval: "minute", "3minute", "5minute", etc
# =====================================

def get_instruments_list():
    """
    Helper to find your symbol's token
    Run this once to find BANKBEES token
    """
    try:
        kite = KiteConnect(api_key=API_KEY)
        
        # Get list of all NSE instruments
        instruments = kite.instruments("NSE")
        
        print("Searching for BANKBEES...")
        for inst in instruments:
            if "BANKBEES" in inst['name'] or inst['tradingsymbol'] == "BANKBEES":
                print(f"Found: {inst['name']}")
                print(f"  Symbol: {inst['tradingsymbol']}")
                print(f"  Token: {inst['instrument_token']}")
                return inst['instrument_token']
        
        print("Not found. Showing first 10 NSE symbols:")
        for i, inst in enumerate(instruments[:10]):
            print(f"  {inst['tradingsymbol']} (Token: {inst['instrument_token']})")
        
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def fetch_historical_data(instrument_token, from_date, to_date, interval):
    """
    Fetch historical data - requires valid login with access_token
    """
    try:
        kite = KiteConnect(api_key=API_KEY)
        
        # For proper login, you need to:
        # 1. Get the login URL
        # 2. Complete login in browser
        # 3. Use the request_token to generate session
        
        # For now, show login instructions
        print("\n" + "="*60)
        print("⚠️  To fetch data, you need to complete login:")
        print("="*60)
        
        login_url = kite.login_url()
        print(f"\n1️⃣  Visit this URL: {login_url}")
        print(f"\n2️⃣  Complete login")
        print(f"\n3️⃣  Copy the request_token from redirect URL")
        print(f"\n4️⃣  Use code like this:")
        print(f"""
session = kite.generate_session(
    request_token='your_request_token',
    api_secret='your_api_secret'
)
access_token = session['access_token']

# Then fetch data
data = kite.historical_data(
    instrument_token={instrument_token},
    from_date='{from_date}',
    to_date='{to_date}',
    interval='{interval}'
)
""")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    print("Zerodha Kite API - OHLC Data Fetcher")
    print("=" * 60)
    
    # First, find the instrument token
    print("\n1. Finding instrument token...\n")
    token = get_instruments_list()
    
    if token:
        print(f"\n✓ Use token {token} in SYMBOL_TOKEN")
        
        # Now show how to fetch data
        print(f"\n2. To fetch historical data:\n")
        fetch_historical_data(token, START_DATE, END_DATE, INTERVAL)
    else:
        print("\n✗ Could not find instrument")
