import json
import os
import time
import logging
import requests
import subprocess
import sys
from kiteconnect import KiteConnect

# ================= CONFIGURATION =================
API_KEY = "ud3fa01mv5n39ra2"
API_SECRET = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"
TOKEN_FILE = "access_token.txt"
TOKEN_SCRIPT_PATH = "mahadev/Zerodha connect/auto token.py"   # adjust if needed

PENDING_FILE = "pending_orders.json"
ORDER_SENT_BASE = "order_sent"

ENABLE_BUY_MARKET = True
ENABLE_SELL_MARKET = False
ENABLE_BUY_LIMIT = True
ENABLE_SELL_LIMIT = False

TEST_MODE = False
TEST_ACTION = "gtt_order"
SAFE_DISTANCE = 0.20
PRODUCT = KiteConnect.PRODUCT_CNC
VARIETY = KiteConnect.VARIETY_REGULAR
EXCHANGE = KiteConnect.EXCHANGE_NSE
QUANTITY = 1
PRODUCT_GTT = KiteConnect.PRODUCT_CNC
MARKET_PROTECTION = 2.0
RETRY_INTERVAL = 60   # seconds to wait before retrying a failed order

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================= ZERODHA CONNECTION =================
kite = KiteConnect(api_key=API_KEY)

def get_access_token():
    with open(TOKEN_FILE, 'r') as f:
        return f.read().strip()

def is_token_valid():
    try:
        kite.profile()
        return True
    except Exception as e:
        logging.warning(f"Token validation failed: {e}")
        return False

def run_token_generator():
    logging.info("Starting token generation script...")
    try:
        # Timeout after 60 seconds – if the script hangs, we force-continue
        result = subprocess.run(
            [sys.executable, TOKEN_SCRIPT_PATH],
            check=False,
            capture_output=False,
            timeout=60
        )
        if result.returncode != 0:
            logging.error(f"Token script exited with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        logging.warning("Token script timed out (60s). Assuming it may have succeeded but didn't exit cleanly.")
        # Check if token file was created anyway
        if os.path.exists(TOKEN_FILE):
            logging.info("Token file exists – will try to use it.")
        else:
            logging.error("Token file not found after timeout.")
            return False
    except FileNotFoundError:
        logging.error(f"Token script not found at: {TOKEN_SCRIPT_PATH}")
        return False
    except Exception as e:
        logging.error(f"Failed to run token script: {e}")
        return False

    if not os.path.exists(TOKEN_FILE):
        logging.error("Token file still missing after generator.")
        return False

    try:
        new_token = get_access_token()
        kite.set_access_token(new_token)
        if is_token_valid():
            logging.info("New token is valid.")
            return True
        else:
            logging.error("New token is still invalid.")
            return False
    except Exception as e:
        logging.error(f"Error loading new token: {e}")
        return False

def ensure_valid_token():
    if not os.path.exists(TOKEN_FILE):
        logging.warning("Token file missing. Generating...")
        return run_token_generator()

    try:
        token = get_access_token()
        kite.set_access_token(token)
    except Exception as e:
        logging.error(f"Error reading token: {e}")
        return run_token_generator()

    if is_token_valid():
        logging.info("Access token is valid.")
        return True
    else:
        logging.warning("Access token invalid. Refreshing...")
        return run_token_generator()

# ================= FILE HANDLING =================
def load_pending_orders():
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, 'r') as f:
        try:
            return json.load(f)
        except:
            return []

def save_pending_orders(pending_list):
    with open(PENDING_FILE, 'w') as f:
        json.dump(pending_list, f, indent=2)

def remove_processed_order(pending_list, index):
    new_list = pending_list[:index] + pending_list[index+1:]
    save_pending_orders(new_list)
    return new_list

def load_order_sent(timeframe):
    order_sent_file = f"{ORDER_SENT_BASE}_{timeframe}.json"
    if not os.path.exists(order_sent_file):
        return set()
    with open(order_sent_file, 'r') as f:
        data = json.load(f)
        return set(tuple(item) for item in data)

def save_order_sent(order_set, timeframe):
    order_sent_file = f"{ORDER_SENT_BASE}_{timeframe}.json"
    with open(order_sent_file, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=2)

# ================= ORDER PLACEMENT =================
def place_market_order_with_protection(symbol, transaction_type):
    order_data = {
        "variety": "regular",
        "exchange": EXCHANGE,
        "tradingsymbol": symbol,
        "transaction_type": transaction_type,
        "quantity": QUANTITY,
        "product": PRODUCT,
        "order_type": "MARKET",
        "market_protection": MARKET_PROTECTION,
        "validity": "DAY"
    }
    access_token = get_access_token()
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {API_KEY}:{access_token}"
    }
    url = "https://api.kite.trade/orders/regular"
    try:
        response = requests.post(url, data=order_data, headers=headers)
        response_json = response.json()
        if response.status_code == 200 and response_json.get("status") == "success":
            order_id = response_json.get("data", {}).get("order_id")
            logging.info(f"✅ Market order placed for {symbol} ({transaction_type}) – Order ID: {order_id}")
            return order_id
        else:
            logging.error(f"❌ Market order failed: {response_json.get('message')}")
            return None
    except Exception as e:
        logging.error(f"❌ Market order error: {e}")
        return None

def place_limit_order(symbol, transaction_type, price):
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
        logging.error(f"❌ Limit order failed: {e}")
        return None

def create_safe_gtt_order(symbol, action, bo_close, timeframe):
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
    logging.info(f"   Trigger: {trigger_price}, Limit: {limit_price}")
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
            logging.info(f"✅ Test GTT placed – Trigger ID: {trigger_id}")
            return trigger_id
        else:
            logging.error(f"Unexpected GTT response: {response}")
            return None
    except Exception as e:
        logging.error(f"❌ GTT failed: {e}")
        return None

# ================= MAIN LOOP (UPDATED WITH RETRY LOGIC) =================
def main():
    logging.info("Order executor started.")

    if not ensure_valid_token():
        logging.critical("Unable to obtain valid token. Exiting.")
        sys.exit(1)

    logging.info("Token validated. Monitoring pending_orders.json...")

    while True:
        try:
            pending = load_pending_orders()
            if not pending:
                time.sleep(2)
                continue

            i = 0
            while i < len(pending):
                order = pending[i]
                symbol_raw = order['symbol']
                symbol = symbol_raw.replace('.NS', '')
                action = order['action']
                level = order.get('level')
                timeframe = order.get('timeframe')

                if not timeframe:
                    logging.warning(f"Missing timeframe: {order}, skipping")
                    # Remove malformed order (it won't work anyway)
                    pending = remove_processed_order(pending, i)
                    continue

                # Skip orders that are already marked as "Already" or "gap"
                if "Already" in action or "gap" in action:
                    logging.info(f"Skipping {symbol} – {action}")
                    pending = remove_processed_order(pending, i)
                    continue

                # Check toggles – remove if disabled
                if "Buy Market Order" in action and not ENABLE_BUY_MARKET:
                    logging.info(f"Skipping {symbol} – Buy Market disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Sell Market Order" in action and not ENABLE_SELL_MARKET:
                    logging.info(f"Skipping {symbol} – Sell Market disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Buy Limit Order" in action and not ENABLE_BUY_LIMIT:
                    logging.info(f"Skipping {symbol} – Buy Limit disabled")
                    pending = remove_processed_order(pending, i)
                    continue
                if "Sell Limit Order" in action and not ENABLE_SELL_LIMIT:
                    logging.info(f"Skipping {symbol} – Sell Limit disabled")
                    pending = remove_processed_order(pending, i)
                    continue

                # ========== RETRY LOGIC ==========
                # If this order failed recently, skip it until RETRY_INTERVAL has passed
                last_attempt = order.get('last_attempt')
                if last_attempt:
                    elapsed = time.time() - last_attempt
                    if elapsed < RETRY_INTERVAL:
                        logging.info(f"⏳ Skipping {symbol} – retry in {int(RETRY_INTERVAL - elapsed)}s")
                        i += 1
                        continue

                # ========== PLACE ORDER ==========
                success = False

                if TEST_MODE:
                    if TEST_ACTION == "print":
                        logging.info(f"[TEST] Would place {action} for {symbol} (level={level})")
                        success = True   # treat test as success so it's removed
                    elif TEST_ACTION == "gtt_order":
                        trigger_id = create_safe_gtt_order(symbol, action, level, timeframe)
                        if trigger_id:
                            success = True
                        else:
                            success = False
                    else:
                        logging.warning(f"Unknown TEST_ACTION: {TEST_ACTION}")
                        success = True   # remove to avoid infinite loop
                else:
                    if "Market Order" in action:
                        txn_type = "BUY" if "Buy" in action else "SELL"
                        order_id = place_market_order_with_protection(symbol, txn_type)
                        if order_id:
                            success = True
                    elif "Limit Order" in action and level:
                        txn_type = KiteConnect.TRANSACTION_TYPE_BUY if "Buy" in action else KiteConnect.TRANSACTION_TYPE_SELL
                        order_id = place_limit_order(symbol, txn_type, level)
                        if order_id:
                            success = True
                    else:
                        logging.warning(f"Cannot place order for {action} – missing price. Removing.")
                        success = True   # malformed order – remove it

                # ========== HANDLE RESULT ==========
                if success:
                    # Order placed successfully – remove from pending
                    pending = remove_processed_order(pending, i)
                    logging.info(f"✅ {symbol} processed and removed from pending.")
                else:
                    # Order failed – keep it in pending, update last_attempt timestamp
                    order['last_attempt'] = time.time()
                    # Update the list (the order object is already in 'pending')
                    save_pending_orders(pending)
                    logging.error(f"❌ {symbol} failed – will retry after {RETRY_INTERVAL}s")
                    i += 1   # move to next order

                time.sleep(0.5)

            # All pending orders processed (or skipped). Wait a bit before next scan.
            time.sleep(2)

        except KeyboardInterrupt:
            logging.info("Order executor stopped by user.")
            break
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()