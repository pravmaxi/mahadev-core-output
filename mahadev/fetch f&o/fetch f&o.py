# === NSE F&O SYMBOL FETCHER TO GOOGLE SHEET ===

import requests
import pandas as pd
import time
import gspread

from oauth2client.service_account import ServiceAccountCredentials
from tenacity import retry, stop_after_attempt, wait_exponential

# =====================================================
# GOOGLE SHEET SETTINGS
# =====================================================

SHEET_NAME = "Stock Price Scraper"
FO_SHEET = "F&O"

CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# =====================================================
# GOOGLE SHEET CONNECTION
# =====================================================

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

# =====================================================
# GET / CREATE SHEET
# =====================================================

try:
    fo_ws = sheet.worksheet(FO_SHEET)
    print(f"Using existing sheet: {FO_SHEET}")

except:

    fo_ws = sheet.add_worksheet(
        title=FO_SHEET,
        rows=5000,
        cols=10
    )

    print(f"Created new sheet: {FO_SHEET}")

# =====================================================
# FETCH NSE DATA
# =====================================================

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=15)
)
def fetch_nse_fo_data():

    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    # GET NSE COOKIES
    session.get(
        "https://www.nseindia.com",
        headers=headers,
        timeout=10
    )

    time.sleep(2)

    # API CALL
    api_url = "https://www.nseindia.com/api/underlying-information"

    response = session.get(
        api_url,
        headers=headers,
        timeout=15
    )

    response.raise_for_status()

    return response.json()

# =====================================================
# UPDATE GOOGLE SHEET
# =====================================================

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=15)
)
def update_fo_sheet():

    print("Fetching NSE F&O data...")

    data = fetch_nse_fo_data()

    # =================================================
    # GET UNDERLYING LIST
    # =================================================

    records = data["data"]["UnderlyingList"]

    if not records:

        print("No records found")
        return

    # =================================================
    # CREATE DATAFRAME
    # =================================================

    df = pd.DataFrame(records)

    # =================================================
    # KEEP ONLY REQUIRED COLUMNS
    # =================================================

    df = df[["symbol", "underlying"]]

    # =================================================
    # CLEAN DATA
    # =================================================

    df = df.fillna("")

    df = df.drop_duplicates()

    df = df.sort_values(by="symbol")

    print("\nPreview Data:")
    print(df.head())

    print(f"\nTotal Symbols : {len(df)}")

    # =================================================
    # CLEAR EXISTING SHEET
    # =================================================

    fo_ws.clear()

    # =================================================
    # UPDATE GOOGLE SHEET
    # =================================================

    rows = [df.columns.tolist()] + df.astype(str).values.tolist()

    fo_ws.update(
        values=rows,
        range_name="A1"
    )

    print("\n✅ Google Sheet Updated Successfully")

# =====================================================
# MAIN
# =====================================================

def main():

    start_time = time.time()

    print("\n==============================")
    print("NSE F&O FETCH STARTED")
    print("==============================\n")

    update_fo_sheet()

    duration = round(time.time() - start_time, 2)

    print("\n==============================")
    print("PROCESS COMPLETED")
    print("==============================")

    print(f"Execution Time : {duration} sec")

# =====================================================
# RUN
# =====================================================

if __name__ == "__main__":
    main()