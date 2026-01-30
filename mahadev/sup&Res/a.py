import yfinance as yf
import pandas as pd
import gspread
import json
import os
import warnings
import sys
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

def get_luxalgo_sr_and_prev_close(symbol, tf):
    """Fetch support, resistance, and previous close price from Yahoo Finance."""
    data = yf.download(symbol, period="100d", interval=tf)
    if data.empty:
        return None, None, None
    
    close_price = data['Close'].iloc[-1]
    high_price = data['High'].max()
    low_price = data['Low'].min()
    
    resistance = high_price
    support = low_price
    
    return resistance, support, close_price

def test_one_symbol():
    symbol = "RELIANCE.NS"
    tf = "1d"
    
    # Get Yahoo data
    r_yahoo, s_yahoo, close_yahoo = get_luxalgo_sr_and_prev_close(symbol, tf)
    
    print(f"\nYahoo Finance ({tf}):")
    print(f"Support: {s_yahoo}")
    print(f"Resistance: {r_yahoo}")
    print(f"Prev Close: {close_yahoo}")
    
    # Manually check TradingView for same symbol/timeframe
    print("\nCompare with TradingView manually:")
    print("1. Open TradingView")
    print("2. Add LuxAlgo Support & Resistance indicator")
    print("3. Set left/right = 15")
    print("4. Compare values")


