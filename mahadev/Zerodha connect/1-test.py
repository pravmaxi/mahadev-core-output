from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException


api_key = "ud3fa01mv5n39ra2"

access_token = "S9DPOKf628vxRUqCd1c1wNh0EU0pQex7"

kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)


     
def get_profile():
    profile1 = kite.profile()
    print("Login Successful:", profile1["user_name"])


def fetch_gtt_orders():
    try:
        gtts = kite.get_gtts()
        print("Total GTTs:", len(gtts))

        for gtt in gtts:
            print("----")
            print("GTT ID:", gtt["id"])
            print("Symbol:", gtt["condition"]["tradingsymbol"])
            print("Trigger Type:", gtt["type"])
            print("Status:", gtt["status"])
            print("Trigger Values:", gtt["condition"]["trigger_values"])

        return gtts

    except KiteException as e:
        print("Failed to fetch GTTs:", e)
        return []


def place_delivery_order():
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol="TCS",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=1,
            product=kite.PRODUCT_CNC,   # Delivery
            order_type=kite.ORDER_TYPE_MARKET
        )

        print("Order placed successfully. Order ID:", order_id)
        return order_id

    except KiteException as e:
        print("Order failed:", e)
        return None


# ---- function call ----
# place_delivery_order()
get_profile()
fetch_gtt_orders()
