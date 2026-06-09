import json
import os
import time
import re
import logging
from kiteconnect import KiteConnect

# ================= CONFIGURATION =================
API_KEY = "ud3fa01mv5n39ra2"
TOKEN_FILE = "access_token.txt"

PENDING_FILE = "pending_orders.json"
ORDER_SENT_BASE = "order_sent"          # will be suffixed with timeframe

# Toggles (real orders)
ENABLE_BUY_MARKET = True
ENABLE_SELL_MARKET = True
ENABLE_BUY_LIMIT = False
ENABLE_SELL_LIMIT = False

# Test mode – when True, no real orders, only test actions
TEST_MODE = True
TEST_ACTION = "gtt_order"   # "print" or "gtt_order"

# GTT safe distance (percentage away from BO close to avoid triggering)
SAFE_DISTANCE = 0.20   # 20%

# Trading parameters (for real orders)
PRODUCT = KiteConnect.PRODUCT_MIS
VARIETY = KiteConnect.VARIETY_REGULAR
EXCHANGE = KiteConnect.EXCHANGE_NSE
QUANTITY = 1

# GTT orders require CNC or NRML product (not MIS)
PRODUCT_GTT = KiteConnect.PRODUCT_CNC

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================= ZERODHA CONNECTION =================
def get_access_token():
    with open(TOKEN_FILE, 'r') as f:
        return f.read().strip()

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(get_access_token())

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
    """Remove the order at given index and save."""
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

# ================= ZERODHA ACTIONS =================
def place_market_order(symbol, transaction_type):
    try:
        order_id = kite.place_order(
            variety=VARIETY,
            exchange=EXCHANGE,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=QUANTITY,
            order_type=KiteConnect.ORDER_TYPE_MARKET,
            product=PRODUCT,
            validity=KiteConnect.VALIDITY_DAY
        )
        logging.info(f"✅ Market order placed for {symbol} ({transaction_type}) – Order ID: {order_id}")
        return order_id
    except Exception as e:
        logging.error(f"❌ Market order failed for {symbol}: {e}")
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
        logging.error(f"❌ Limit order failed for {symbol}: {e}")
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
                time.sleep(2)
                continue

            # Process each pending order sequentially
            i = 0
            while i < len(pending):
                order = pending[i]
                symbol = order['symbol']
                action = order['action']
                level = order.get('level')
                timeframe = order.get('timeframe')
                if not timeframe:
                    logging.warning(f"Missing timeframe in order: {order}, skipping")
                    i += 1
                    continue

                # Skip already-sent or gap actions
                if "Already" in action or "gap" in action:
                    logging.info(f"Skipping {symbol} – no trade needed ({action})")
                    pending = remove_processed_order(pending, i)
                    continue

                # Check toggles
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

                # Load order_sent for this timeframe
                order_sent_set = load_order_sent(timeframe)
                direction = "buy" if "Buy" in action else "sell"
                # Determine S/R value for duplicate check: we need the original S/R, but the pending order does not store it.
                # However, the duplicate prevention is already handled by the analysis script via order_sent_{TF}.json.
                # The order executor does not need to check again because the analysis script already avoided creating a pending order for an already-sent signal.
                # But to be safe, we can still check using the level? However level is BO close, not S/R.
                # For simplicity, we rely on the analysis script's prevention and just act.
                # If you want a double-check, we could store the S/R in the pending order, but it's optional.

                if TEST_MODE:
                    if TEST_ACTION == "print":
                        logging.info(f"[TEST MODE] Would place {action} for {symbol} (level={level})")
                    elif TEST_ACTION == "gtt_order":
                        create_safe_gtt_order(symbol, action, level, timeframe)
                    else:
                        logging.warning(f"Unknown TEST_ACTION: {TEST_ACTION}")
                else:
                    # Real order placement
                    if "Market Order" in action:
                        txn_type = KiteConnect.TRANSACTION_TYPE_BUY if "Buy" in action else KiteConnect.TRANSACTION_TYPE_SELL
                        place_market_order(symbol, txn_type)
                    elif "Limit Order" in action and level:
                        # Extract price from action or use level as limit price? Limit order uses BO close as limit price.
                        # The action string already contains the price, but level is the BO close. Use level.
                        txn_type = KiteConnect.TRANSACTION_TYPE_BUY if "Buy" in action else KiteConnect.TRANSACTION_TYPE_SELL
                        place_limit_order(symbol, txn_type, level)
                    else:
                        logging.warning(f"Cannot place order for {action} – missing price")

                # After processing (whether test or real), remove this entry
                pending = remove_processed_order(pending, i)
                # do not increment i because the list shifted

                # Optional: small delay between orders
                time.sleep(0.5)

            time.sleep(2)
        except KeyboardInterrupt:
            logging.info("Order executor stopped by user.")
            break
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()