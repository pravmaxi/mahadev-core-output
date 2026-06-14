import json
import requests
from kiteconnect import KiteConnect
import os

API_KEY = "ud3fa01mv5n39ra2"
TOKEN_FILE = "access_token.txt"

def get_access_token():
    with open(TOKEN_FILE, 'r') as f:
        return f.read().strip()

try:
    ACCESS_TOKEN = get_access_token()
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # --- Order details ---
    order_data = {
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "IRFC",
        "transaction_type": "BUY",
        "quantity": 1,
        "product": "CNC",
        "order_type": "MARKET",
        "market_protection": 2.0,  # Provide a numeric value > 0
        "validity": "DAY"
    }

    # API endpoint and headers
    url = "https://api.kite.trade/orders/regular"
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {API_KEY}:{ACCESS_TOKEN}"
    }

    # Make the HTTP POST request
    response = requests.post(url, data=order_data, headers=headers)
    response_json = response.json()

    if response.status_code == 200 and response_json.get("status") == "success":
        order_id = response_json.get("data", {}).get("order_id")
        print(f"✅ Market order placed successfully!")
        print(f"   Order ID: {order_id}")
    else:
        print(f"❌ Order failed: {response_json.get('message')}")
        print(f"   Details: {response_json}")

except Exception as e:
    print(f"❌ Script encountered an error: {e}")