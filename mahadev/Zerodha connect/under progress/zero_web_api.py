#!/usr/bin/env python3
"""
Place GTT (Good Till Triggered) orders from a Google Sheet.
Reads configuration from nsr_trade.json.
"""

import os
import json
import time
import sys
from urllib.parse import urlencode
from datetime import datetime, timedelta

import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ============================================================
#  CONFIG LOADING
# ============================================================
CONFIG_FILE = "nsr_trade.json"

def load_config():
    """Load configuration from nsr_trade.json."""
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file '{CONFIG_FILE}' not found.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

# ============================================================
#  GOOGLE SHEETS CONNECTION
# ============================================================
def connect_gsheet(creds_file, spreadsheet_id):
    """Authenticate and open the Google Sheet."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_file, scope)
        client = gspread.authorize(creds)
        client.timeout = 30
        return client.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"❌ Google Sheets connection failed: {e}")
        sys.exit(1)

def read_gtt_input(sheet, sheet_name):
    """
    Read the GTT input sheet.
    Expects columns: A = Symbol, B = Trigger, C = Price, D = Quantity.
    Returns a list of dicts.
    """
    try:
        ws = sheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ Sheet '{sheet_name}' not found.")
        sys.exit(1)

    # Get all values (including header)
    all_vals = ws.get_all_values()
    if len(all_vals) < 2:
        print("⚠️ No data rows found.")
        return []

    # Assume first row is header; we'll read by index anyway
    # We'll just take columns A-D from each row (0-based)
    rows = []
    for i, row in enumerate(all_vals[1:], start=2):  # start row number for logging
        if len(row) < 4:
            continue
        symbol = row[0].strip()
        trigger_str = row[1].strip()
        price_str = row[2].strip()
        qty_str = row[3].strip()
        if not symbol or not trigger_str or not price_str or not qty_str:
            continue
        try:
            trigger = float(trigger_str)
            price = float(price_str)
            qty = int(qty_str)
            if qty <= 0:
                print(f"⚠️ Row {i}: Quantity must be positive, skipping.")
                continue
        except ValueError:
            print(f"⚠️ Row {i}: Invalid numeric value, skipping.")
            continue
        rows.append({
            'symbol': symbol.upper(),
            'trigger': trigger,
            'price': price,
            'quantity': qty,
            'row_num': i
        })
    return rows

# ============================================================
#  ZERODHA GTT ORDER PLACEMENT
# ============================================================
def place_gtt_order(symbol, trigger_value, price, quantity, config):
    """
    Send the GTT order using the internal Zerodha web API.
    config contains zerodha credentials and gtt defaults.
    """
    # Extract credentials from config
    zd = config['zerodha']
    enctoken = zd['enctoken']
    user_id = zd['user_id']
    app_uuid = zd['app_uuid']
    cookie_string = zd['cookie_string']

    # GTT defaults
    gtt_defaults = config.get('gtt', {})
    exchange = gtt_defaults.get('exchange', 'NSE')
    product = gtt_defaults.get('product', 'CNC')
    expiry_days = gtt_defaults.get('expiry_days', 30)

    # Build expiry date
    expiry_date = (datetime.now() + timedelta(days=expiry_days)).strftime("%Y-%m-%d 00:00:00")

    # For last_price, we can use trigger - 1 (approximate) – you may change this
    # if you want to fetch real LTP from another endpoint
    last_price = trigger_value - 1.0  # placeholder; adjust as needed

    # Build payload
    condition = {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "trigger_values": [trigger_value],
        "last_price": last_price
    }
    order = {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "transaction_type": "BUY",
        "quantity": quantity,
        "price": price,
        "order_type": "LIMIT",
        "product": product
    }
    # For sell, you would change transaction_type, but we assume BUY.
    # You can extend to support both buy/sell by adding a column in sheet.

    data_payload = {
        "condition": json.dumps(condition),
        "orders": json.dumps([order]),
        "type": "single",
        "expires_at": expiry_date
    }

    body = urlencode(data_payload)

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9,ta;q=0.8",
        "authorization": f"enctoken {enctoken}",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://kite.zerodha.com",
        "referer": "https://kite.zerodha.com/orders/gtt",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "x-kite-app-uuid": app_uuid,
        "x-kite-userid": user_id,
        "x-kite-version": "3.0.0",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
    }

    session = requests.Session()
    # Set cookies
    for cookie in cookie_string.split("; "):
        if "=" in cookie:
            key, value = cookie.split("=", 1)
            session.cookies.set(key, value)

    response = session.post(
        "https://kite.zerodha.com/oms/gtt/triggers",
        headers=headers,
        data=body,
        timeout=15
    )
    return response

# ============================================================
#  MAIN
# ============================================================
def main():
    print("⏳ Loading configuration...")
    config = load_config()
    print("✅ Config loaded.")

    # Extract sheet details
    spreadsheet_id = config.get('spreadsheet_id')
    creds_file = config.get('credentials_file')
    sheet_name = config.get('sheet_name', 'gtt input')
    if not spreadsheet_id or not creds_file:
        print("❌ Missing 'spreadsheet_id' or 'credentials_file' in config.")
        sys.exit(1)

    # Connect to Google Sheets
    print("⏳ Connecting to Google Sheets...")
    sheet = connect_gsheet(creds_file, spreadsheet_id)
    print("✅ Connected.")

    # Read GTT input rows
    rows = read_gtt_input(sheet, sheet_name)
    if not rows:
        print("No rows to process. Exiting.")
        return

    print(f"📊 Found {len(rows)} GTT orders to place.")

    # Place each order
    success_count = 0
    fail_count = 0
    for idx, entry in enumerate(rows, start=1):
        symbol = entry['symbol']
        trigger = entry['trigger']
        price = entry['price']
        qty = entry['quantity']
        row_num = entry['row_num']

        print(f"\n🔹 Order {idx}/{len(rows)} (Row {row_num}): {symbol} Trigger={trigger} Price={price} Qty={qty}")
        try:
            response = place_gtt_order(symbol, trigger, price, qty, config)
            print(f"   Status: {response.status_code}")
            # Try to parse JSON
            try:
                resp_json = response.json()
                if response.status_code == 200 and resp_json.get('status') == 'success':
                    print(f"   ✅ Success! Response: {json.dumps(resp_json, indent=2)}")
                    success_count += 1
                else:
                    print(f"   ❌ Failed: {resp_json}")
                    fail_count += 1
            except:
                print(f"   ❌ Unexpected response: {response.text}")
                fail_count += 1
        except Exception as e:
            print(f"   ❌ Error: {e}")
            fail_count += 1

        # Delay to avoid rate limits
        time.sleep(1)

    # Final summary
    print("\n" + "="*50)
    print(f"📊 Summary: {success_count} successful, {fail_count} failed.")
    print("="*50)

if __name__ == "__main__":
    main()