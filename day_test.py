# Import required libraries
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# Function to calculate EMA
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

# Function to calculate SMA
def sma(series, length):
    return series.rolling(window=length).mean()

# Function to calculate Wave Trend
def wave_trend(df, n1=15, n2=27):
    hlc3 = (df['High'] + df['Low'] + df['Close']) / 3
    
    esa = ema(hlc3, n1)
    d = ema(abs(hlc3 - esa), n1)
    ci = (hlc3 - esa) / (0.015 * d)
    tci = ema(ci, n2)
    
    wt1 = tci
    wt2 = sma(wt1, 4)
    
    df['Forca_Alma'] = wt1 - wt2
    df['Cross'] = (wt1.shift(1) < wt2.shift(1)) & (wt1 > wt2) | (wt1.shift(1) > wt2.shift(1)) & (wt1 < wt2)
    df['Color'] = np.where(df['Cross'], np.where(wt2 - wt1 > 0, 'yellow', 'blue'), None).astype(object)
    
    return df[['Forca_Alma', 'Color']]

# Function to fetch stock data
def fetch_stock_data(symbol, period='90d', interval='1d'):
    stock = yf.Ticker(symbol + ".NS")
    df = stock.history(period=period, interval=interval)
    if df.empty:
        print(f"No data found for {symbol}")
        return None
    return df[['Open', 'High', 'Low', 'Close']]

# List of NSE stocks
nse_stocks = [
        "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ABFRL", "AIAENG", "AJANTPHARM",
        "APLLTD", "ALKEM", "AMBUJACEM", "APOLLOHOSP", "APOLLOTYRE", "ASHOKLEY", "ASIANPAINT",
        "ASTRAL", "AUROPHARMA", "DMART", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
        "BAJAJHLDNG", "BALKRISIND", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "BATAINDIA", "BEL",
        "BHARATFORG", "BHEL", "BPCL", "BHARTIARTL", "BIOCON", "BSOFT", "BOSCHLTD", "BRIGADE",
        "BRITANNIA", "CANBK", "CASTROLIND", "CENTRALBK", "CESC", "CHAMBLFERT", "CIPLA",
        "COALINDIA", "COLPAL", "CONCOR", "COROMANDEL", "CROMPTON", "CUMMINSIND", "DALBHARAT",
        "DCBBANK", "DEEPAKNTR", "DELTACORP", "DLF", "LALPATHLAB", "DRREDDY", "EICHERMOT",
        "ESCORTS", "EXIDEIND", "FEDERALBNK", "GAIL", "GLENMARK", "GODREJCP", "GODREJPROP",
        "GRANULES", "GRASIM", "GUJGASLTD", "GNFC", "GSFC", "HCLTECH", "HDFCAMC", "HDFCBANK",
        "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HAL", "HINDCOPPER", "HINDPETRO", "HINDUNILVR",
        "HUDCO", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDFCFIRSTB", "INDIANB", "IEX",
        "INDHOTEL", "IOC", "IGL", "INDUSINDBK", "INFY", "INDIGO", "ITC", "JINDALSTEL",
        "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "KOTAKBANK", "LT", "LICHSGFIN", "LUPIN", "M&MFIN",
        "M&M", "MANAPPURAM", "MARICO", "MARUTI", "MPHASIS", "MAXHEALTH", "METROPOLIS", "MSUMI",
        "MCX", "NATIONALUM", "NAVINFLUOR", "NHPC", "NMDC", "NTPC", "ONGC", "OFSS", "PAGEIND",
        "PIDILITIND", "PIIND", "PFC", "POWERGRID", "PNB", "RVNL", "RECLTD", "RELIANCE", "SBICARD",
        "SBILIFE", "SHREECEM", "SIEMENS", "SRF", "SBIN", "SAIL", "SUNPHARMA", "TATACHEM", "TCS",
        "TATACONSUM", "TATAELXSI", "TATAMOTORS", "TATAPOWER", "TATASTEEL", "TECHM", "TITAN",
        "TORNTPHARM", "TORNTPOWER", "TRENT", "UPL", "ULTRACEMCO", "UBL", "VBL", "VEDL", "VOLTAS",
        "WIPRO", "ZEEL"
    ]
# Get today's date and last 5 days
today = datetime.today()
last_5_days = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(5)]

data_points = []

# Process each stock
for stock in nse_stocks:
    df = fetch_stock_data(stock)
    if df is not None:
        result = wave_trend(df)
        filtered_result = result[result['Color'].notna()]
        
        for timestamp, row in filtered_result.iterrows():
            date_str = timestamp.strftime('%Y-%m-%d')
            if date_str in last_5_days:
                data_points.append((timestamp, stock, row['Color']))

# Visualization using Matplotlib
if data_points:
    df_visual = pd.DataFrame(data_points, columns=['Timestamp', 'Stock', 'Color'])
    df_visual['Timestamp'] = pd.to_datetime(df_visual['Timestamp']).dt.date

    colors = {'yellow': 'gold', 'blue': 'dodgerblue'}
    plt.figure(figsize=(12, 8))

    for color in df_visual['Color'].unique():
        subset = df_visual[df_visual['Color'] == color]
        plt.scatter(subset['Timestamp'], subset['Stock'], label=color, color=colors.get(color, 'gray'), alpha=0.7)

    plt.title('Stock Cross Signals on Wave Trend (Last 5 Days)')
    plt.xlabel('Date')
    plt.ylabel('Stock')
    plt.xticks(rotation=45)
    plt.legend(title='Signal Color')
    plt.tight_layout()
    plt.show()
else:
    print("No cross signals found for the last 5 days.")
