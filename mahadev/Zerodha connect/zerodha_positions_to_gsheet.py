from kiteconnect import KiteConnect
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd

# -------------------------------
# Zerodha API
# -------------------------------

api_key = "ud3fa01mv5n39ra2"

# Read access token automatically
with open("token.txt", "r") as file:
    access_token = file.read().strip()

kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

# Fetch positions
positions_data = kite.positions()

# Get net positions
positions = positions_data["net"]

# -------------------------------
# Convert to DataFrame
# -------------------------------

data = []

for pos in positions:
    data.append({
        "Stock": pos["tradingsymbol"],
        "Exchange": pos["exchange"],
        "Product": pos["product"],
        "Quantity": pos["quantity"],
        "Buy Qty": pos["buy_quantity"],
        "Sell Qty": pos["sell_quantity"],
        "Avg Price": pos["average_price"],
        "Last Price": pos["last_price"],
        "PnL": pos["pnl"]
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
worksheet = spreadsheet.worksheet("zerodha positions")

# Clear old data
worksheet.clear()

# Write new data
worksheet.update(
    [df.columns.values.tolist()] + df.values.tolist()
)

print("Positions updated successfully")