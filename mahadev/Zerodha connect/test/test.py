import yfinance as yf
import pandas as pd

symbol = "aartipharm.NS"
target_date = "2026-09-02"   # Your desired date

# Download a larger period (e.g., 5 days) to ensure data is fetched
df = yf.download(symbol, interval="1h", period="5d", progress=False)

# If columns are MultiIndex (when multiple tickers), flatten them
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)

# Convert index to IST
if df.index.tzinfo is None:
    df.index = df.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
else:
    df.index = df.index.tz_convert('Asia/Kolkata')

# Filter to the target date (only that day)
df_day = df[df.index.date == pd.to_datetime(target_date).date()]

print(f"1-hour candles for {symbol} on {target_date}:")
print(df_day)