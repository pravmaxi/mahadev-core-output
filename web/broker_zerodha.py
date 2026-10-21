"""
Zerodha Kite Connect integration — v2 with proper port cleanup.
"""

import os
import time
import socket
import threading
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    KiteConnect = None


API_KEY       = os.getenv("ZERODHA_API_KEY", "").strip()
API_SECRET    = os.getenv("ZERODHA_API_SECRET", "").strip()
CALLBACK_PORT = int(os.getenv("ZERODHA_CALLBACK_PORT", "5000"))
TOKEN_FILE    = Path(__file__).resolve().parent / "access_token.txt"

# LIVE TRADING switch — must be "1" to allow any order-modifying action
LIVE_TRADING  = os.getenv("ZERODHA_LIVE_TRADING", "0").strip() == "1"

_login_server    = None     # keep a reference so we can close it
_login_thread    = None
_login_status    = {"state": "idle", "message": ""}
_login_url_cur   = ""


# ---- token file ----
def _load_token():
    if not TOKEN_FILE.exists(): return None
    try: return TOKEN_FILE.read_text().strip() or None
    except Exception: return None

def _save_token(t):
    TOKEN_FILE.write_text(t)
    try: os.chmod(TOKEN_FILE, 0o600)
    except Exception: pass


# ---- Kite client ----
def get_kite():
    if not KITE_AVAILABLE or not API_KEY: return None
    token = _load_token()
    if not token: return None
    try:
        k = KiteConnect(api_key=API_KEY); k.set_access_token(token); return k
    except Exception: return None

def _check_token():
    kite = get_kite()
    if kite is None: return False, "no token"
    try: return True, kite.profile()
    except Exception as e: return False, str(e)


# ---- status ----
def status():
    ok, payload = _check_token()
    port_busy = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        port_busy = (s.connect_ex(("127.0.0.1", CALLBACK_PORT)) == 0)
        s.close()
    except Exception:
        pass
    return {
        "kite_installed":    KITE_AVAILABLE,
        "configured":        bool(API_KEY and API_SECRET),
        "logged_in":         ok,
        "login_in_progress": _login_status["state"] == "waiting",
        "state":             _login_status["state"],
        "message":           _login_status["message"],
        "profile":           payload if ok else None,
        "error":             None if ok else (payload if payload != "no token" else None),
        "login_url":         _login_url_cur,
        "callback_port":     CALLBACK_PORT,
        "port_busy":         port_busy,
    }

def profile():
    ok, payload = _check_token()
    if ok: return {"ok": True, "profile": payload}
    return {"ok": False, "error": payload}

def logout():
    try:
        if TOKEN_FILE.exists(): TOKEN_FILE.unlink()
    except Exception: pass
    _login_status.update({"state": "idle", "message": ""})
    return True

def reset():
    """Forcibly stop any pending login server."""
    global _login_server, _login_thread
    if _login_server is not None:
        try:
            threading.Thread(target=_login_server.shutdown, daemon=True).start()
        except Exception: pass
        try:
            _login_server.server_close()
        except Exception: pass
        _login_server = None
    _login_thread = None
    _login_status.update({"state": "idle", "message": ""})
    return True


# ---- login flow ----
class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = q.get("request_token", [None])[0]
        if tok:
            self._handle_token(tok)
        else:
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Waiting for Zerodha login...</h3>")

    def _handle_token(self, req_token):
        global _login_status
        try:
            kite = KiteConnect(api_key=API_KEY)
            data = kite.generate_session(req_token, api_secret=API_SECRET)
            _save_token(data["access_token"])
            _login_status = {"state": "success", "message": "Connected"}
            html = ("<html><body style='font-family:system-ui;padding:40px'>"
                    "<h2 style='color:#3fb950'>Login successful</h2>"
                    "<p>You can close this tab and go back to the Broker page.</p>"
                    "</body></html>")
        except Exception as e:
            _login_status = {"state": "error", "message": str(e)}
            html = f"<h3 style='color:red'>Login failed: {e}</h3>"

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

        # shut down + CLOSE the socket so the next login can reuse the port
        def _stop():
            time.sleep(0.4)
            try: self.server.shutdown()
            except Exception: pass
            try: self.server.server_close()
            except Exception: pass
            print(f"🔓 Zerodha callback server on port {CALLBACK_PORT} closed")
        threading.Thread(target=_stop, daemon=True).start()

    def log_message(self, *a): pass


def start_login():
    global _login_server, _login_thread, _login_status, _login_url_cur

    # if a previous attempt is stuck, forcibly clean it up
    reset()

    if not KITE_AVAILABLE:
        return {"ok": False, "error": "kiteconnect not installed — run: pip install kiteconnect"}
    if not (API_KEY and API_SECRET):
        return {"ok": False, "error": "ZERODHA_API_KEY / ZERODHA_API_SECRET not set in .env"}

    # is port still busy from an old attempt?
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        busy = (s.connect_ex(("127.0.0.1", CALLBACK_PORT)) == 0)
        s.close()
    except Exception:
        busy = False
    if busy:
                return {"ok": False,
                "error": f"port {CALLBACK_PORT} is busy. Run 'lsof -i :{CALLBACK_PORT}' in "
                         f"the terminal to see what's using it. If it's ControlCe (macOS AirPlay), "
                         f"change ZERODHA_CALLBACK_PORT to 5050 in .env AND update your Zerodha "
                         f"app's redirect URL to http://127.0.0.1:5050/callback"}

    _login_status = {"state": "waiting", "message": "Waiting for Zerodha login"}
    _login_url_cur = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"

    def _runner():
        global _login_server
        try:
            _login_server = _ReusableHTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
            print(f"🔓 Zerodha callback server listening on 127.0.0.1:{CALLBACK_PORT}")
            print(f"🔐 Login URL: {_login_url_cur}")
            webbrowser.open(_login_url_cur)
            _login_server.serve_forever()
        except Exception as e:
            _login_status.update({"state": "error", "message": str(e)})
            print(f"❌ Login server error: {e}")

    _login_thread = threading.Thread(target=_runner, daemon=True)
    _login_thread.start()
    return {"ok": True, "login_url": _login_url_cur}


# ---- account data --------------------------------------------------------

def get_positions():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        p = kite.positions()
        return {"ok": True,
                "net": p.get("net", []),
                "day": p.get("day", [])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_holdings():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "holdings": kite.holdings()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_orders():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "orders": kite.orders()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_margins():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        m = kite.margins()
        # Normalise into a simple flat summary the UI can consume
        eq = (m or {}).get("equity", {}) or {}
        net = eq.get("net", 0) or 0
        avail = eq.get("available", {}) or {}
        util = eq.get("utilised", {}) or {}
        return {"ok": True,
                "net":         round(net, 2),
                "available":   round(avail.get("live_balance", 0) or avail.get("cash", 0) or 0, 2),
                "used_debits": round(util.get("debits", 0), 2),
                "used_span":   round(util.get("span", 0), 2),
                "used_option": round(util.get("option_premium", 0), 2),
                "used_total":  round(util.get("debits", 0) + util.get("span", 0) + util.get("option_premium", 0), 2),
                "raw":         eq}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_tradebook():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "trades": kite.trades()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_gtts():
    kite = get_kite()
    if kite is None:
        return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "gtts": kite.get_gtts()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _live_check():
    if not LIVE_TRADING:
        return {"ok": False,
                "error": "LIVE TRADING is OFF. Set ZERODHA_LIVE_TRADING=1 in .env and restart "
                         "the server to enable creating, modifying, or deleting GTT orders."}
    return None


def create_gtt(payload):
    """payload must contain: type, tradingsymbol, exchange, trigger_values (list),
    last_price, orders (list of dicts with transaction_type, quantity, order_type,
    product, price)"""
    blocked = _live_check()
    if blocked: return blocked
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        gtt_id = kite.place_gtt(
            trigger_type   = payload["type"],
            tradingsymbol  = payload["tradingsymbol"],
            exchange       = payload["exchange"],
            trigger_values = payload["trigger_values"],
            last_price     = payload["last_price"],
            orders         = payload["orders"],
        )
        print(f"✅ GTT created: id={gtt_id} for {payload['tradingsymbol']}")
        return {"ok": True, "gtt_id": gtt_id}
    except Exception as e:
        print(f"❌ GTT create failed: {e}")
        return {"ok": False, "error": str(e)}


def modify_gtt(gtt_id, payload):
    blocked = _live_check()
    if blocked: return blocked
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        kite.modify_gtt(
            trigger_id     = int(gtt_id),
            trigger_type   = payload["type"],
            tradingsymbol  = payload["tradingsymbol"],
            exchange       = payload["exchange"],
            trigger_values = payload["trigger_values"],
            last_price     = payload["last_price"],
            orders         = payload["orders"],
        )
        print(f"✅ GTT modified: id={gtt_id}")
        return {"ok": True, "gtt_id": gtt_id}
    except Exception as e:
        print(f"❌ GTT modify failed: {e}")
        return {"ok": False, "error": str(e)}


def delete_gtt(gtt_id):
    blocked = _live_check()
    if blocked: return blocked
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        kite.delete_gtt(int(gtt_id))
        print(f"✅ GTT deleted: id={gtt_id}")
        return {"ok": True, "gtt_id": gtt_id}
    except Exception as e:
        print(f"❌ GTT delete failed: {e}")
        return {"ok": False, "error": str(e)}


def get_ltp(exchange, tradingsymbol):
    """Fetch last price for a symbol (used as `last_price` when creating a GTT)."""
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        key = f"{exchange}:{tradingsymbol}"
        data = kite.ltp([key])
        ltp = (data.get(key) or {}).get("last_price")
        if ltp is None: return {"ok": False, "error": "no LTP for " + key}
        return {"ok": True, "last_price": round(float(ltp), 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def account_summary():
    """One-shot: returns positions + holdings + orders + margins + totals."""
    out = {"ok": True}
    pos = get_positions()
    hol = get_holdings()
    ordr = get_orders()
    marg = get_margins()
    out["positions"] = pos
    out["holdings"]  = hol
    out["orders"]    = ordr
    out["margins"]   = marg
    out["tradebook"] = get_tradebook()
    out["gtts"]      = get_gtts()

    # compute totals
    hold_val = 0; hold_pnl = 0; hold_day = 0
    if hol.get("ok"):
        for h in hol["holdings"]:
            hold_val += (h.get("last_price", 0) or 0) * (h.get("quantity", 0) or 0)
            hold_pnl += h.get("pnl", 0) or 0
            hold_day += (h.get("day_change", 0) or 0) * (h.get("quantity", 0) or 0)

    pos_pnl = 0; pos_day = 0
    if pos.get("ok"):
        for p in pos["net"]:
            pos_pnl += p.get("pnl", 0) or 0
            pos_day += p.get("m2m", 0) or 0

    avail = (marg or {}).get("available", 0) or 0
    out["totals"] = {
        "holdings_value": round(hold_val, 2),
        "holdings_pnl":   round(hold_pnl, 2),
        "holdings_day":   round(hold_day, 2),
        "positions_pnl":  round(pos_pnl, 2),
        "positions_day":  round(pos_day, 2),
        "available_negative": avail < 0,
        "available_abs":     round(abs(avail), 2),
    }
    return out

# ---- live trading --------------------------------------------------------

def place_live_order(exchange, tradingsymbol, transaction_type, quantity,
                     order_type, product, price=None, trigger_price=None,
                     validity="DAY", tag="webapp"):
    """Place a real order on Zerodha. Blocked unless LIVE_TRADING=1."""
    blocked = _live_check()
    if blocked: return blocked
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        params = {
            "variety":          "regular",
            "exchange":         exchange.upper(),
            "tradingsymbol":    tradingsymbol.upper(),
            "transaction_type": transaction_type.upper(),
            "quantity":         int(quantity),
            "product":          product.upper(),
            "order_type":       order_type.upper(),
            "validity":         validity.upper(),
            "tag":              tag,
        }
        ot = order_type.upper()
        if ot in ("LIMIT", "SL"):
            if price is None:
                return {"ok": False, "error": "price required for LIMIT/SL"}
            params["price"] = float(price)
        if ot in ("SL", "SL-M"):
            if trigger_price is None:
                return {"ok": False, "error": "trigger_price required for SL/SL-M"}
            params["trigger_price"] = float(trigger_price)

        order_id = kite.place_order(**params)
        print(f"✅ LIVE order: {order_id}  {transaction_type} {quantity} "
              f"{tradingsymbol} @ {order_type}")
        return {"ok": True, "order_id": order_id}
    except Exception as e:
        print(f"❌ Live order failed: {e}")
        return {"ok": False, "error": str(e)}


def live_orders():
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "orders": kite.orders()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def live_trades():
    kite = get_kite()
    if kite is None: return {"ok": False, "error": "not logged in"}
    try:
        return {"ok": True, "trades": kite.trades()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    print("\n🧪 broker_zerodha.py self-test\n" + "─" * 60)
    print(f"kiteconnect installed : {KITE_AVAILABLE}")
    print(f"API key set           : {bool(API_KEY)}")
    print(f"API secret set        : {bool(API_SECRET)}")
    print(f"Token file present    : {TOKEN_FILE.exists()}")
    print(f"LIVE trading enabled  : {LIVE_TRADING}")
    print(f"Status                : {status()}")
    print()