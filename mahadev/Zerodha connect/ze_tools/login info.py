
from kiteconnect import KiteConnect
import os

# ===================== CONFIGURATION =====================
API_KEY = "ud3fa01mv5n39ra2"          # Your API key (same as in token generator)
TOKEN_FILE = "access_token.txt"

# ===================== READ TOKEN FROM FILE =====================
def get_access_token():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(f"Token file '{TOKEN_FILE}' not found. Run the token generation script first.")
    with open(TOKEN_FILE, 'r') as f:
        token = f.read().strip()
        if not token:
            raise ValueError("Token file is empty.")
        return token

# ===================== FETCH ACCOUNT DETAILS =====================
try:
    ACCESS_TOKEN = get_access_token()
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # 1. Profile (user ID, email, name)
    profile = kite.profile()
    print("✅ Authentication successful!\n")
    print("👤 Profile Details:")
    print(f"   User ID       : {profile['user_id']}")
    print(f"   Email         : {profile['email']}")
    print(f"   Name          : {profile['user_name']}")
    print(f"   Broker        : {profile['broker']}")
    print(f"   Exchanges     : {', '.join(profile['exchanges'])}")

    # 2. Margins (available funds)
    margins = kite.margins()
    print("\n💰 Margins (Equity):")
    print(f"   Available Cash    : ₹{margins['equity']['available']['cash']}")
    print(f"   Available Intraday: ₹{margins['equity']['available']['intraday_payin']}")
    print(f"   Used               : ₹{margins['equity']['utilised']['debits']}")

    # 3. Positions (optional)
    positions = kite.positions()
    net_positions = positions['net']
    print(f"\n📊 Current Positions: {len(net_positions)}")
    for pos in net_positions[:3]:   # show first 3
        print(f"   {pos['tradingsymbol']} : {pos['quantity']} shares @ ₹{pos['average_price']}")

    print("\n✅ Token is valid and active.")

except FileNotFoundError as e:
    print(f"❌ {e}")
    print("Please run your token generation script (Flask) to obtain a new access token.")
except Exception as e:
    print(f"❌ Error: {e}")
    print("Possible reasons: invalid/expired token, network issue, or API key mismatch.")