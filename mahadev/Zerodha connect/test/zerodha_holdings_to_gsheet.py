from kiteconnect import KiteConnect
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd

# -------------------------------
# Zerodha API
# -------------------------------

api_key = "ud3fa01mv5n39ra2"

# Read access token automatically
with open("access_token.txt", "r") as file:
    access_token = file.read().strip()

kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

# Fetch holdings
holdings = kite.holdings()

# -------------------------------
# Convert to DataFrame
# -------------------------------

data = []

for stock in holdings:

    avg_price = stock["average_price"]
    last_price = stock["last_price"]

    profit_pct = 0

    if avg_price > 0:
        profit_pct = round(
            ((last_price - avg_price) / avg_price) * 100,
            2
        )

    data.append({
        "Stock": stock["tradingsymbol"],
        "Quantity": stock["quantity"],
        "Avg Price": avg_price,
        "Last Price": last_price,
        "PnL": stock["pnl"],
        "Profit %": profit_pct
    })

df = pd.DataFrame(data)

# -------------------------------
# Google Sheets Connection
# -------------------------------

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name(
    "Json/automation-project-429417-c51140fdff86.json",
    scope
)

client = gspread.authorize(creds)

# Open sheet
spreadsheet = client.open("Stock Price Scraper")

# Open tab
worksheet = spreadsheet.worksheet("zerodha holdings")

# Clear old data
worksheet.clear()

# Write new data
worksheet.update(
    [df.columns.values.tolist()] + df.values.tolist()
)

print("Holdings updated successfully")