import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pandas as pd

# =========================
# GOOGLE SHEETS SETUP
# =========================

SERVICE_ACCOUNT_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"   # <-- Replace with your JSON file
SPREADSHEET_NAME = "Stock Price Scraper"       # <-- Your Google Sheet Name

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=scopes
)

client = gspread.authorize(creds)

spreadsheet = client.open(SPREADSHEET_NAME)

# INPUT TAB (contains F&O symbols)
input_sheet = spreadsheet.worksheet("F&O")

# OUTPUT TAB
try:
    news_sheet = spreadsheet.worksheet("news")
except:
    news_sheet = spreadsheet.add_worksheet(title="news", rows=1000, cols=20)

# =========================
# READ SYMBOLS FROM INPUT TAB
# =========================

# Assumes symbols are in Column A
symbols = input_sheet.col_values(1)

# Remove header if exists
symbols = [s.strip() for s in symbols if s.strip()]

# =========================
# CLEAR OLD DATA
# =========================

news_sheet.clear()

headers = [
    "Time",
    "Symbol",
    "Headline",
    "Source",
    "Published Time",
    "URL"
]

news_sheet.append_row(headers)

# =========================
# FETCH NEWS
# =========================

all_rows = []

for symbol in symbols:

    try:
        tv_symbol = f"NSE:{symbol}"

        url = (
            "https://news-headlines.tradingview.com/v2/headlines"
            f"?client=chart&lang=en&symbol={tv_symbol}"
        )

        response = requests.get(url)

        if response.status_code == 200:

            data = response.json()

            headlines = data.get("items", [])

            # Take latest 5 news
            for item in headlines[:5]:

                title = item.get("title", "")
                source = item.get("source", "")
                published = item.get("published", "")
                story_path = item.get("storyPath", "")

                news_url = f"https://www.tradingview.com{story_path}"

                row = [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    symbol,
                    title,
                    source,
                    published,
                    news_url
                ]

                all_rows.append(row)

        else:
            print(f"Failed for {symbol}")

    except Exception as e:
        print(f"Error for {symbol}: {e}")

# =========================
# WRITE TO SHEET
# =========================

if all_rows:
    news_sheet.append_rows(all_rows)

print("News Updated Successfully")