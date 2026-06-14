import json
import os
import time
import re
import logging
import requests
from kiteconnect import KiteConnect

# ================= CONFIGURATION =================
# These settings control the behaviour of the order executor.
# Change them as needed without touching the core logic.

API_KEY = "ud3fa01mv5n39ra2"
TOKEN_FILE = "access_token.txt"                 # File where the access token is stored

PENDING_FILE = "pending_orders.json"           # Shared queue of orders from the analysis script
ORDER_SENT_BASE = "order_sent"                 # Base name for per-timeframe sent‑order files

# Toggles – enable/disable specific order types
ENABLE_BUY_MARKET = True      # Allow "Buy Market Order" to be placed
ENABLE_SELL_MARKET = False    # Allow "Sell Market Order" (disabled for now)
ENABLE_BUY_LIMIT = True       # Allow "Buy Limit Order"
ENABLE_SELL_LIMIT = False     # Allow "Sell Limit Order"

# Test mode – when True, no real orders are placed, only test actions (print or GTT)
TEST_MODE = False
TEST_ACTION = "gtt_order"     # "print" or "gtt_order"

# GTT safe distance – percentage away from BO close to avoid triggering (0.20 = 20%)
SAFE_DISTANCE = 0.20

# Trading parameters for real orders (used for both market and limit)
PRODUCT = KiteConnect.PRODUCT_CNC          # Delivery order (CNC). Change to MIS for intraday.
VARIETY = KiteConnect.VARIETY_REGULAR
EXCHANGE = KiteConnect.EXCHANGE_NSE
QUANTITY = 1                              # Number of shares per order

# GTT orders also require a delivery product (CNC)
PRODUCT_GTT = KiteConnect.PRODUCT_CNC

# Market protection value (percentage) – mandatory for market orders from 1st April 2026.
# Example: 2.0 means the order will be executed within 2% of the current market price.
MARKET_PROTECTION = 2.0

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================= ZERODHA CONNECTION =================

def get_access_token():
    """Read the access token from the token file."""
    with open(TOKEN_FILE, 'r') as f:
        return f.read().strip()

# Initialise Kite Connect with the API key and access token
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(get_access_token())

# ================= FILE HANDLING =================

def load_pending_orders():
    """Load all pending orders from the shared JSON file."""
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, 'r') as f:
        try:
            return json.load(f)
        except:
            return []

def save_pending_orders(pending_list):
    """Save the pending orders list back to the JSON file."""
    with open(PENDING_FILE, 'w') as f:
        json.dump(pending_list, f, indent=2)

def remove_processed_order(pending_list, index):
    """Remove an order at a specific index and save the updated list."""
    new_list = pending_list[:index] + pending_list[index+1:]
    save_pending_orders(new_list)
    return new_list

def load_order_sent(timeframe):
    """
    Load the set of already‑sent orders for a given timeframe.
    Each entry is a tuple (symbol, S/R value, direction).
    This prevents duplicate orders for the same support/resistance level.
    """
    order_sent_file = f"{ORDER_SENT_BASE}_{timeframe}.json"
    if not os.path.exists(order_sent_file):
        return set()
    with open(order_sent_file, 'r') as f:
        data = json.load(f)
        return set(tuple(item) for item in data)

def save_order_sent(order_set, timeframe):
    """Save the order_sent set back to its JSON file."""
    order_sent_file = f"{ORDER_SENT_BASE}_{timeframe}.json"
    with open(order_sent_file, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=2)

# ================= ORDER PLACEMENT FUNCTIONS =================

def place_market_order_with_protection(symbol, transaction_type):
    """
    Place a market order using a direct API call with market_protection.
    This bypasses the kiteconnect library because the library does not yet support
    the mandatory 'market_protection' parameter required by the new SEBI regulation.
    """
    # Prepare the order data as per Kite Connect API v3
    order_data = {
        "variety": "regular",
        "exchange": EXCHANGE,
        "tradingsymbol": symbol,
        "transaction_type": transaction_type,   # "BUY" or "SELL"
        "quantity": QUANTITY,
        "product": PRODUCT,
        "order_type": "MARKET",
        "market_protection": MARKET_PROTECTION,
        "validity": "DAY"
    }

    # Build authentication headers
    access_token = get_access_token()
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {API_KEY}:{access_token}"
    }

    # API endpoint for regular orders
    url = "https://api.kite.trade/orders/regular"

    try:
        response = requests.post(url, data=order_data, headers=headers)
        response_json = response.json()

        if response.status_code == 200 and response_json.get("status") == "success":
            order_id = response_json.get("data", {}).get("order_id")
            logging.info(f"✅ Market order placed for {symbol} ({transaction_type}) – Order ID: {order_id}")
            return order_id
        else:
            error_msg = response_json.get("message", "Unknown error")
            logging.error(f"❌ Market order failed for {symbol}: {error_msg}")
            return None
    except Exception as e:
        logging.error(f"❌ Market order failed for {symbol}: {e}")
        return None

def place_limit_order(symbol, transaction_type, price):
    """Place a limit order using the kiteconnect library (no market protection needed)."""
    try:
        order_id = kite.place_order(
            variety=VARIETY,
            exchange=EXCHANGE,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=QUANTITY,
            order_type=KiteConnect.ORDER_TYPE_LIMIT,
            price=price,
            product=PRODUCT,
            validity=KiteConnect.VALIDITY_DAY
        )
        logging.info(f"✅ Limit order placed for {symbol} ({transaction_type}) at ₹{price} – Order ID: {order_id}")
        return order_id
    except Exception as e:
        logging.error(f"❌ Limit order failed for {symbol}: {e}")
        return None

def create_safe_gtt_order(symbol, action, bo_close, timeframe):
    """
    Create a GTT (Good Till Triggered) order for testing purposes.
    The trigger price is set 20% away from the BO close so it never executes.
    This verifies that the API call works without risking real money.
    """
    if not bo_close:
        bo_close = 100.0
    tradingsymbol = symbol.replace('.NS', '')
    exchange = "NSE"
    if "Buy" in action:
        trigger_price = round(bo_close * (1 - SAFE_DISTANCE), 2)
        limit_price = round(trigger_price * 1.01, 2)
        transaction_type = KiteConnect.TRANSACTION_TYPE_BUY
    else:
        trigger_price = round(bo_close * (1 + SAFE_DISTANCE), 2)
        limit_price = round(trigger_price * 0.99, 2)
        transaction_type = KiteConnect.TRANSACTION_TYPE_SELL

    logging.info(f"📝 Creating test GTT for {symbol} ({action}) [TF={timeframe}]")
    logging.info(f"   Trigger price: {trigger_price} (safe)")
    logging.info(f"   Limit price  : {limit_price}")
    try:
        response = kite.place_gtt(
            trigger_type=KiteConnect.GTT_TYPE_SINGLE,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=[trigger_price],
            last_price=bo_close,
            orders=[{
                "transaction_type": transaction_type,
                "quantity": QUANTITY,
                "order_type": KiteConnect.ORDER_TYPE_LIMIT,
                "product": PRODUCT_GTT,
                "price": limit_price
            }]
        )
        if 'data' in response:
            trigger_id = response['data']['trigger_id']
            logging.info(f"✅ Test GTT placed for {symbol} – Trigger ID: {trigger_id}")
            return trigger_id
        else:
            logging.error(f"Unexpected GTT response: {response}")
            return None
    except Exception as e:
        logging.error(f"❌ Failed to create test GTT for {symbol}: {e}")
        return None

# ================= MAIN LOOP =================

def main():
    logging.info("Order executor started. Monitoring pending_orders.json...")
    while True:
        try:
            pending = load_pending_orders()
            if not pending:
                time.sleep(2)      # No pending orders, wait a bit
                continue

            # Process pending orders one by one
            i = 0
            while i < len(pending):
                order = pending[i]

                # Extract order details
                symbol_raw = order['symbol']
                symbol = symbol_raw.replace('.NS', '')   # Remove .NS for Kite API
                action = order['action']
                level = order.get('level')
                timeframe = order.get('timeframe')
                if not timeframe:
                    logging.warning(f"Missing timeframe in order: {order}, skipping")
                    i += 1
                    continue

                # Skip orders that are already sent or are non‑tradable gaps
                if "Already" in action or "gap" in action:
                    logging.info(f"Skipping {symbol} – no trade needed ({action})")
                    pending = remove_processed_order(pending, i)
                    continue

                # Respect the user toggles
                if "Buy Market Order" in action and not ENABLE_BUY_MARKET:
                    logging.info(f"Skipping {symbol} – Buy Market Order disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Sell Market Order" in action and not ENABLE_SELL_MARKET:
                    logging.info(f"Skipping {symbol} – Sell Market Order disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Buy Limit Order" in action and not ENABLE_BUY_LIMIT:
                    logging.info(f"Skipping {symbol} – Buy Limit Order disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Sell Limit Order" in action and not ENABLE_SELL_LIMIT:
                    logging.info(f"Skipping {symbol} – Sell Limit Order disabled")
                    pending = remove_processed_order(pending, i)
                    continue

                # (Optional duplicate check – analysis script already prevents duplicates)
                order_sent_set = load_order_sent(timeframe)
                direction = "buy" if "Buy" in action else "sell"

                if TEST_MODE:
                    # Test mode: only simulate or create safe GTT orders
                    if TEST_ACTION == "print":
                        logging.info(f"[TEST MODE] Would place {action} for {symbol} (level={level})")
                    elif TEST_ACTION == "gtt_order":
                        create_safe_gtt_order(symbol, action, level, timeframe)
                    else:
                        logging.warning(f"Unknown TEST_ACTION: {TEST_ACTION}")
                else:
                    # Real order placement
                    if "Market Order" in action:
                        txn_type = "BUY" if "Buy" in action else "SELL"
                        place_market_order_with_protection(symbol, txn_type)
                    elif "Limit Order" in action and level:
                        txn_type = KiteConnect.TRANSACTION_TYPE_BUY if "Buy" in action else KiteConnect.TRANSACTION_TYPE_SELL
                        place_limit_order(symbol, txn_type, level)
                    else:
                        logging.warning(f"Cannot place order for {action} – missing price")

                # After processing (whether test or real), remove the order from pending file
                pending = remove_processed_order(pending, i)
                # Do not increment i because the list has shifted

                # Small delay between orders to avoid rate limits
                time.sleep(0.5)

            time.sleep(2)   # Wait before checking the pending file again
        except KeyboardInterrupt:
            logging.info("Order executor stopped by user.")
            break
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()