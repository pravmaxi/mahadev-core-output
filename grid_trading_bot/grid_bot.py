# strategy_setup.py
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
from datetime import datetime

# ========== HARDCODED INPUTS FOR TESTING ==========
JSON_FILE = "ohlc_bankbees.NS_2026-01-08_2026-01-09_5m.json"  # OHLC data JSON file
# ===================================================

# Telegram Setup
TELEGRAM_TOKEN = "8011257570:AAFO8LxI_075Up-B-Znp7pEnFfGlut3V_90"
CHAT_ID = "6187777440"

# Google Sheets Setup
SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "GridInputs"
OUTPUT_SHEET = "strategy_output"
CREDENTIALS_FILE = "/Users/apple/Downloads/learning/automation-project-429417-c51140fdff86.json"

# Initialize Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME)
input_ws = sheet.worksheet(INPUT_SHEET)

# Get or create output sheet
try:
    output_ws = sheet.worksheet(OUTPUT_SHEET)
except:
    output_ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=1000, cols=20)

# Clear and setup output sheet
output_ws.clear()
headers = [ "Symbol", "CurrentPrice", "Signal", "Reason", "Telegram_Sent", "Trend"]
output_ws.append_row(headers)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    requests.post(url, data=payload)
    return True

def log_to_sheet(data):
    output_ws.append_row(data)

def load_ohlc_from_json(json_filename):
    """
    Load OHLC data from JSON file
    
    Parameters:
    - json_filename: Path to the JSON file
    
    Returns:
    - Dictionary with OHLC data or None if file not found
    """
    try:
        with open(json_filename, 'r') as f:
            data = json.load(f)
        print(f"✓ Loaded {len(data['data'])} records from {json_filename}")
        return data
    except FileNotFoundError:
        print(f"✗ JSON file not found: {json_filename}")
        print(f"  Run fetch_ohlc.py first to generate the JSON file")
        return None
    except Exception as e:
        print(f"✗ Error loading JSON file: {e}")
        return None

print("finish")

# Load OHLC data from JSON file
ohlc_data = load_ohlc_from_json(JSON_FILE)