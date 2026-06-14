from kiteconnect import KiteConnect
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import subprocess
import sys
import os

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
        profit_pct = round(((last_price - avg_price) / avg_price) * 100, 2)

    data.append({
        "Stock": stock["tradingsymbol"],
        "Quantity": stock["quantity"],
        "Avg Price": avg_price,
        "Last Price": last_price,
        "PnL": stock["pnl"],          # absolute profit/loss
        "Profit %": profit_pct
    })

df = pd.DataFrame(data)

# -------------------------------
# Sort by PnL descending (highest positive first)
# -------------------------------
df_sorted = df.sort_values(by="PnL", ascending=False).reset_index(drop=True)

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

# Open sheet and tabs
spreadsheet = client.open("Stock Price Scraper")
worksheet_holdings = spreadsheet.worksheet("zerodha holdings")
worksheet_zsell = spreadsheet.worksheet("Zsellinput")

# -------------------------------
# Update "zerodha holdings" tab with sorted data
# -------------------------------
worksheet_holdings.clear()
worksheet_holdings.update(
    [df_sorted.columns.values.tolist()] + df_sorted.values.tolist()
)

# -------------------------------
# Copy ONLY stock symbols with PnL > 0 to column A of "Zsellinput"
# -------------------------------
positive_pnl_df = df_sorted[df_sorted["PnL"] > 0]   # filter positive PnL
symbols = positive_pnl_df["Stock"].tolist()

# Prepare data for column A (each symbol in its own row)
symbols_column = [[sym] for sym in symbols]

# Clear existing content in column A
worksheet_zsell.batch_clear(["A:A"])

# Write new symbols starting at cell A1
if symbols_column:
    worksheet_zsell.update("A1", symbols_column)

print("Holdings updated (sorted by PnL descending).")
print(f"Copied {len(symbols)} stock symbols (with positive PnL) to Zsellinput column A.")

# -------------------------------
# Trigger the zsell_sr_finder.py script
# -------------------------------

# Determine the full path to zsell_sr_finder.py
# Assuming it's in the same directory as this script:
script_dir = os.path.dirname(os.path.abspath(__file__))
other_script = os.path.join(script_dir, "zsell_sr_finder.py")

# If the script is elsewhere, uncomment and modify the next line:
# other_script = "/Users/apple/Downloads/PK/visual studio/mahadev core output/mahadev/Zerodha connect/zsell_sr_finder.py"

try:
    # Check if the script exists
    if not os.path.isfile(other_script):
        print(f"Error: Could not find {other_script}")
    else:
        # Use sys.executable to get the current Python interpreter
        result = subprocess.run(
            [sys.executable, other_script],
            capture_output=True,
            text=True,
            timeout=60  # optional, in seconds
        )
        print("zsell_sr_finder.py executed successfully.")
        print("STDOUT:", result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
except subprocess.TimeoutExpired:
    print("Script timed out after 60 seconds.")
except Exception as e:
    print(f"Failed to run zsell_sr_finder.py: {e}")