# =========================================================
# FIRST CANDLE BREAKOUT STRATEGY
# =========================================================

import yfinance as yf
import pandas as pd
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from tenacity import retry, stop_after_attempt, wait_exponential

# =========================================================
# SETTINGS
# =========================================================

TIMEFRAME = "1h"      # 15m / 30m / 1h
CHECK_INTERVAL = "5m"

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "F&O"
OUTPUT_SHEET = "FCB_OUTPUT"

CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# =========================================================
# GOOGLE SHEET SETUP
# =========================================================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name(
    CREDENTIALS_FILE,
    scope
)

client = gspread.authorize(creds)

sheet = client.open(SHEET_NAME)

input_ws = sheet.worksheet(INPUT_SHEET)

# =========================================================
# OUTPUT SHEET
# =========================================================

try:
    output_ws = sheet.worksheet(OUTPUT_SHEET)

except:
    output_ws = sheet.add_worksheet(
        title=OUTPUT_SHEET,
        rows=5000,
        cols=20
    )

# =========================================================
# GET STOCK LIST
# =========================================================

stocks = input_ws.col_values(1)[1:]

stocks = [s.strip() for s in stocks if s.strip()]

print(f"Total Stocks : {len(stocks)}")

# =========================================================
# TIMEFRAME SETTINGS
# =========================================================

TIMEFRAME_CONFIG = {
    "15m": {
        "period": "5d",
        "interval": "15m",
        "first_candle_end": "09:30"
    },
    "30m": {
        "period": "10d",
        "interval": "30m",
        "first_candle_end": "09:45"
    },
    "1h": {
        "period": "30d",
        "interval": "1h",
        "first_candle_end": "10:15"
    }
}

config = TIMEFRAME_CONFIG[TIMEFRAME]

# =========================================================
# MAIN STRATEGY
# =========================================================

results = []

for stock in stocks:

    try:

        print(f"Checking : {stock}")

        ticker = f"{stock}.NS"

        df = yf.download(
            ticker,
            period=config["period"],
            interval=config["interval"],
            progress=False,
            auto_adjust=False
        )

        if df.empty:
            continue

        # =================================================
        # TIMEZONE
        # =================================================

        df.index = df.index.tz_convert("Asia/Kolkata")

        today = datetime.now().date()

        df_today = df[df.index.date == today]

        if df_today.empty:
            continue

        # =================================================
        # FIRST CANDLE
        # =================================================

        first_candle = df_today.iloc[0]

        first_high = float(first_candle["High"])
        first_low = float(first_candle["Low"])

        first_time = df_today.index[0]

        # =================================================
        # REMAINING CANDLES
        # =================================================

        remaining = df_today.iloc[1:]

        breakout = ""
        breakout_time = ""
        breakout_price = ""

        # =================================================
        # CHECK ALL COMPLETED CANDLES
        # =================================================

        for idx, row in remaining.iterrows():

            candle_high = float(row["High"])
            candle_low = float(row["Low"])

            # HIGH BREAKOUT
            if candle_high > first_high:

                breakout = "HIGH BREAKOUT"

                breakout_time = idx.strftime("%H:%M")

                breakout_price = candle_high

                break

            # LOW BREAKOUT
            elif candle_low < first_low:

                breakout = "LOW BREAKOUT"

                breakout_time = idx.strftime("%H:%M")

                breakout_price = candle_low

                break

        # =================================================
        # CURRENT MARKET PRICE
        # =================================================

        cmp_price = float(df_today.iloc[-1]["Close"])

        # =================================================
        # STORE RESULT
        # =================================================

        results.append([
            stock,
            TIMEFRAME,
            round(first_high, 2),
            round(first_low, 2),
            breakout,
            breakout_time,
            breakout_price,
            round(cmp_price, 2)
        ])

    except Exception as e:

        print(f"{stock} Failed : {e}")

# =========================================================
# CREATE DATAFRAME
# =========================================================

output_df = pd.DataFrame(results, columns=[
    "Symbol",
    "Timeframe",
    "First Candle High",
    "First Candle Low",
    "Breakout",
    "Breakout Time",
    "Breakout Price",
    "CMP"
])

# =========================================================
# PRINT CONSOLE
# =========================================================

print("\n==============================")
print(output_df.head(20))
print("==============================")

# =========================================================
# UPDATE GOOGLE SHEET
# =========================================================

output_ws.clear()

rows = [output_df.columns.tolist()] + output_df.astype(str).values.tolist()

output_ws.update(
    values=rows,
    range_name="A1"
)

print("\n✅ GOOGLE SHEET UPDATED")