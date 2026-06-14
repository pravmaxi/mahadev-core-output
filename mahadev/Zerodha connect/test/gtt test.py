from kiteconnect import KiteConnect

api_key = "ud3fa01mv5n39ra2"
# Read token automatically
with open("access_token.txt", "r") as file: 
    access_token = file.read().strip()

kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

# Create GTT Order
response = kite.place_gtt(
    trigger_type=kite.GTT_TYPE_SINGLE,
    tradingsymbol="IRFC",
    exchange="NSE",
    trigger_values=[101],
    last_price=100,
    orders=[
        {
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "product": "CNC",
            "price": 102
        }
    ]
)

print(response)