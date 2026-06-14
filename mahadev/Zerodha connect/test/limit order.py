from kiteconnect import KiteConnect
import os
import time

# ================= CONFIGURATION =================
API_KEY = "ud3fa01mv5n39ra2"
TOKEN_FILE = "access_token.txt"
QUANTITY = 1
TRADING_SYMBOL = "IRFC"
EXCHANGE = "NSE"
TRANSACTION_TYPE = "BUY"
PRODUCT = "CNC"          # Delivery
VARIETY = "regular"
ORDER_TYPE = "LIMIT"     # Use limit order instead of market

# Buffer percentage above current LTP to ensure fill (0.5%)
BUFFER_PERCENT = 0.005   # 0.5%

# ================= READ ACCESS TOKEN =================
def get_access_token():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(f"Token file '{TOKEN_FILE}' not found.")
    with open(TOKEN_FILE, 'r') as f:
        token = f.read().strip()
        if not token:
            raise ValueError("Token file is empty.")
        return token

# ================= PLACE LIMIT ORDER =================
try:
    ACCESS_TOKEN = get_access_token()
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # Fetch current market price (LTP)
    ltp_data = kite.ltp(f"NSE:{TRADING_SYMBOL}")
    ltp = ltp_data[f"NSE:{TRADING_SYMBOL}"]['last_price']
    print(f"Current LTP of {TRADING_SYMBOL}: ₹{ltp}")

    # Calculate limit price (slightly above LTP to ensure fill)
    limit_price = round(ltp * (1 + BUFFER_PERCENT), 2)
    print(f"Placing limit order at ₹{limit_price}")

    # Place limit order
    order_id = kite.place_order(
        variety=VARIETY,
        exchange=EXCHANGE,
        tradingsymbol=TRADING_SYMBOL,
        transaction_type=TRANSACTION_TYPE,
        quantity=QUANTITY,
        order_type=ORDER_TYPE,
        price=limit_price,
        product=PRODUCT,
        validity="DAY"
    )

    print(f"✅ Limit order placed successfully!")
    print(f"   Order ID: {order_id}")
    print(f"   Symbol  : {TRADING_SYMBOL}")
    print(f"   Type    : {TRANSACTION_TYPE}")
    print(f"   Quantity: {QUANTITY}")
    print(f"   Product : {PRODUCT}")
    print(f"   Limit price: ₹{limit_price}")

except FileNotFoundError as e:
    print(f"❌ {e}")
except Exception as e:
    print(f"❌ Order failed: {e}")
    if "No IPs configured" in str(e):
        print("👉 You need to whitelist your static IP in the Kite Connect developer console.")
    elif "instrument" in str(e).lower():
        print("👉 Make sure the trading symbol is correct (IRFC on NSE).")
    else:
        print("👉 Check your access token validity and network connection.")