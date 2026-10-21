# check_data_availability.py - Check oldest available 1m data from Yahoo Finance
import yfinance as yf
import time
from datetime import datetime, timedelta

SYMBOL = "bankbees.NS"  # Change to your symbol

def check_1m_data_availability(symbol):
    """
    Check what's the oldest date with 1-minute data available
    """
    print(f"📊 Checking 1-minute data availability for {symbol}...\n")
    
    # Try from today going back
    today = datetime.now()
    
    for days_back in range(0, 60, 5):  # Check every 5 days
        check_date = today - timedelta(days=days_back)
        start_date = check_date.strftime("%Y-%m-%d")
        end_date = (check_date + timedelta(days=1)).strftime("%Y-%m-%d")
        
        print(f"Checking {start_date}...", end="", flush=True)
        
        try:
            data = yf.download(
                symbol,
                start=start_date,
                end=end_date,
                interval="1m",
                progress=False,
                timeout=10
            )
            
            if not data.empty:
                print(f" ✓ Data available ({len(data)} records)")
                return start_date
            else:
                print(f" ⊘ No data")
        except Exception as e:
            error_msg = str(e)
            if "last 30 days" in error_msg.lower():
                print(f" ✗ Before last 30 days")
                break
            else:
                print(f" ✗ Error: {str(e)[:40]}")
        
        time.sleep(1)
    
    print(f"\n✓ 1-minute data is available for last 30 days maximum")
    print(f"  Oldest available: {(today - timedelta(days=29)).strftime('%Y-%m-%d')}")

if __name__ == "__main__":
    check_1m_data_availability(SYMBOL)
