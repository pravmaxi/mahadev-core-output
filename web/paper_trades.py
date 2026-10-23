"""
Paper trading — simulated orders recorded to a JSON file. Never touches Zerodha.
"""
import json
import uuid
from pathlib import Path
from datetime import datetime
from settings import IST

PAPER_FILE = Path(__file__).resolve().parent / "data" / "paper_trades.json"
PAPER_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load():
    if not PAPER_FILE.exists():
        return {"orders": []}
    try:
        return json.loads(PAPER_FILE.read_text())
    except Exception:
        return {"orders": []}


def _save(data):
    PAPER_FILE.write_text(json.dumps(data, indent=2))


def _now():
    return datetime.now(IST).isoformat()


def place_order(symbol, exchange, side, qty, order_type, price, product,
                trigger_price=None, ltp=None, tag=""):
    """Record a paper order. MARKET fills instantly at ltp (if given)."""
    data = _load()
    ot = order_type.upper()
    filled = ot == "MARKET" and ltp is not None
    o = {
        "id":           "P" + uuid.uuid4().hex[:8].upper(),
        "ts":           _now(),
        "symbol":       symbol.upper().strip(),
        "exchange":     exchange.upper(),
        "side":         side.upper(),
        "qty":          int(qty),
        "order_type":   ot,
        "price":        float(price) if price not in (None, "", 0) else None,
        "trigger_price": float(trigger_price) if trigger_price not in (None, "", 0) else None,
        "product":      product.upper(),
        "status":       "FILLED" if filled else "OPEN",
        "fill_price":   float(ltp) if filled else None,
        "fill_ts":      _now() if filled else None,
        "tag":          tag,
    }
    data["orders"].append(o)
    _save(data)
    return o


def cancel_order(order_id):
    data = _load()
    for o in data["orders"]:
        if o["id"] == order_id and o["status"] == "OPEN":
            o["status"] = "CANCELLED"
            _save(data)
            return o
    return None


def mark_filled(order_id, fill_price):
    data = _load()
    for o in data["orders"]:
        if o["id"] == order_id and o["status"] == "OPEN":
            o["status"] = "FILLED"
            o["fill_price"] = float(fill_price)
            o["fill_ts"] = _now()
            _save(data)
            return o
    return None


def list_orders():
    return _load().get("orders", [])


def positions():
    """Net positions from filled orders — weight-avg entry per symbol."""
    from collections import defaultdict
    acc = defaultdict(lambda: {
        "symbol": "", "exchange": "", "product": "", "tag": "",
        "net_qty": 0, "buy_qty": 0, "sell_qty": 0,
        "buy_value": 0.0, "sell_value": 0.0,
    })
    for o in _load().get("orders", []):
        if o["status"] != "FILLED":
            continue
        tag = o.get("tag", "")
        key = (o["symbol"], o["exchange"], o["product"], tag)
        a = acc[key]
        a["symbol"]   = o["symbol"]
        a["exchange"] = o["exchange"]
        a["product"]  = o["product"]
        a["tag"]      = tag
        fp = o.get("fill_price") or o.get("price") or 0
        if o["side"] == "BUY":
            a["net_qty"]    += o["qty"]
            a["buy_qty"]    += o["qty"]
            a["buy_value"]  += fp * o["qty"]
        else:
            a["net_qty"]    -= o["qty"]
            a["sell_qty"]   += o["qty"]
            a["sell_value"] += fp * o["qty"]
    out = []
    for (sym, exch, prod, tag), a in acc.items():
        if a["net_qty"] == 0:
            continue
        if a["net_qty"] > 0 and a["buy_qty"]:
            avg = a["buy_value"] / a["buy_qty"]
        elif a["net_qty"] < 0 and a["sell_qty"]:
            avg = a["sell_value"] / a["sell_qty"]
        else:
            avg = 0
        out.append({
            "symbol":   sym, "exchange": exch,
            "product":  prod, "tag": tag, "qty": a["net_qty"],
            "buy_qty":  a["buy_qty"], "sell_qty": a["sell_qty"],
            "avg_price": round(avg, 2),
            "buy_value":  round(a["buy_value"], 2),
            "sell_value": round(a["sell_value"], 2),
        })
    return out


def clear_all():
    _save({"orders": []})
    return True