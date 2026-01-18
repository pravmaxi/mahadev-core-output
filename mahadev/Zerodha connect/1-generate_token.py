from kiteconnect import KiteConnect

api_key = "ud3fa01mv5n39ra2"
api_secret = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"
request_token = "tUlrvu57F5d0bqLpDsvdTg0qrZRBGQhB"

kite = KiteConnect(api_key=api_key)

data = kite.generate_session(request_token, api_secret=api_secret)
access_token = data["access_token"]

kite.set_access_token(access_token)

print("ACCESS TOKEN:", access_token)
