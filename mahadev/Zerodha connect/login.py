from kiteconnect import KiteConnect

api_key = "ud3fa01mv5n39ra2"

kite = KiteConnect(api_key=api_key)

print(kite.login_url())