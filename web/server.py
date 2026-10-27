"""
Scanner app — real yfinance + Google Sheet source + sheet write-back.
Run from web/:   python server.py
"""
import os
import time
import queue
import threading
import pandas as pd
import pytz
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, jsonify, request

from data import download, download_batch, get_live_ltp
from indicators import list_all, get as get_indicator
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
import sheets
import backtest
import batch_backtest

load_dotenv()

app = Flask(__name__)
PORT = int(os.getenv("PORT", "8000"))
IST = pytz.timezone("Asia/Kolkata")
CANCEL_FLAGS  = {}

# ✅ FIX: Filter out the continuous 5-second polling logs from the terminal
import logging
class PollingFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Ignore these specific background routes
        noisy_routes = [
            "/api/trade/auto/status",
            "/api/trade/paper/positions",
            "/api/trade/paper/orders"
        ]
        return not any(route in msg for route in noisy_routes)

log = logging.getLogger('werkzeug')
log.addFilter(PollingFilter())

# ---- last-result caches (survive page navigation) ------------------------
LAST_SCAN_CACHE     = {"rows": [], "meta": None, "ts": 0}
LAST_BACKTEST_CACHE = {"data": None, "ts": 0}
SCANNER_JOBS = {}


# ---- persistent trigger state --------------------------------------------
import json
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "data" / "trigger_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_trigger_state():
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        print(f"📂 Loaded {len(data)} trigger-state entries from {STATE_FILE.name}")
        return data
    except Exception as e:
        print(f"⚠️ Could not load trigger state ({e}); starting fresh")
        return {}


def _save_trigger_state(state):
    """Atomic write: temp file then rename, so a crash mid-write can't corrupt."""
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_FILE)
        return True
    except Exception as e:
        print(f"⚠️ Could not save trigger state: {e}")
        return False


TRIGGER_STATE = _load_trigger_state()


# ---- Pages ----------------------------------------------------------------

@app.route("/")
def home():
    return redirect("/scanner")

@app.route("/scanner")
def scanner_page():
    return render_template("scanner.html", active="scanner")

@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html", active="backtest")

@app.route("/trading")
def trading_page():
    return render_template("trading.html", active="trading")

@app.route("/settings")
def settings_page():
    return render_template("settings.html", active="settings")


# ---- Settings API ---------------------------------------------------------

import settings_store

# Fields allowed to be written via the Settings UI
_ALLOWED_KEYS = {
    "PORT", "APP_PASSWORD",
    "ZERODHA_API_KEY", "ZERODHA_API_SECRET",
    "ZERODHA_CALLBACK_PORT", "ZERODHA_LIVE_TRADING",
}

@app.route("/api/settings")
def api_settings_get():
    env = settings_store.read_env()
    from settings import CREDENTIALS_FILE
    return jsonify({
        "ok": True,
        "env_file":          str(settings_store.ENV_FILE),
        "port":              env.get("PORT", "8000"),
        "app_password_set":  bool(env.get("APP_PASSWORD")),
        "zerodha_api_key":   env.get("ZERODHA_API_KEY", ""),
        "zerodha_secret_masked":  settings_store.mask(env.get("ZERODHA_API_SECRET", "")),
        "zerodha_secret_present": bool(env.get("ZERODHA_API_SECRET")),
        "zerodha_callback_port":  env.get("ZERODHA_CALLBACK_PORT", "5050"),
        "zerodha_live_trading":   env.get("ZERODHA_LIVE_TRADING", "0") == "1",
        "service_account_email":  sheets.service_account_email(),
        "credentials_file":       str(CREDENTIALS_FILE),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    incoming = body.get("values") or {}

    updates = {}
    restart_needed = False

    for k, v in incoming.items():
        if k not in _ALLOWED_KEYS:
            continue
        if k == "ZERODHA_API_SECRET":
            if not v or (isinstance(v, str) and v.startswith("*")):
                continue
        if k == "ZERODHA_LIVE_TRADING":
            vs = str(v).strip().lower()
            updates[k] = "1" if vs in ("1", "true", "yes", "on") else "0"
            continue
        updates[k] = str(v)

    if not updates:
        return jsonify({"ok": True, "applied": [], "restart_required": False})

    try:
        settings_store.write_env(updates)
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to write .env: {e}"})

    if broker_zerodha is not None:
        if "ZERODHA_API_KEY" in updates:
            broker_zerodha.API_KEY = updates["ZERODHA_API_KEY"]
        if "ZERODHA_API_SECRET" in updates:
            broker_zerodha.API_SECRET = updates["ZERODHA_API_SECRET"]
        if "ZERODHA_LIVE_TRADING" in updates:
            broker_zerodha.LIVE_TRADING = (updates["ZERODHA_LIVE_TRADING"] == "1")
        if "ZERODHA_CALLBACK_PORT" in updates:
            restart_needed = True

    if "PORT" in updates:
        restart_needed = True

    return jsonify({
        "ok": True,
        "applied": list(updates.keys()),
        "restart_required": restart_needed,
    })


@app.route("/broker")
def broker_page():
    return render_template("broker.html", active="broker")


# ---- Sheets ---------------------------------------------------------------

@app.route("/api/sheets/tabs")
def api_sheets_tabs():
    book = request.args.get("book") or ""
    if not book:
        return jsonify({"ok": False, "tabs": []})
    return jsonify({"ok": True, "book": book, "tabs": sheets.list_tabs(book)})

@app.route("/api/sheets/service_account")
def api_service_account():
    return jsonify({"email": sheets.service_account_email()})

@app.route("/api/sheets/symbols")
def api_sheets_symbols():
    book = request.args.get("book") or ""
    tab = request.args.get("tab") or ""
    if not book or not tab:
        return jsonify({"ok": False, "symbols": []})
    syms = sheets.read_symbols(book, tab)
    return jsonify({"ok": True, "symbols": syms})


# ---- Zerodha broker -------------------------------------------------------

try:
    import broker_zerodha
except Exception as e:
    print(f"⚠️ broker_zerodha import failed: {e}")
    broker_zerodha = None


@app.route("/api/broker/zerodha/status")
def api_zerodha_status():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify({"ok": True, **broker_zerodha.status()})


@app.route("/api/broker/zerodha/login", methods=["POST"])
def api_zerodha_login():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.start_login())


@app.route("/api/broker/zerodha/profile")
def api_zerodha_profile():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.profile())


@app.route("/api/broker/zerodha/logout", methods=["POST"])
def api_zerodha_logout():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    broker_zerodha.logout()
    return jsonify({"ok": True})


@app.route("/api/broker/zerodha/positions")
def api_zerodha_positions():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_positions())


@app.route("/api/broker/zerodha/holdings")
def api_zerodha_holdings():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_holdings())


@app.route("/api/broker/zerodha/orders")
def api_zerodha_orders():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_orders())


@app.route("/api/broker/zerodha/margins")
def api_zerodha_margins():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_margins())


@app.route("/api/broker/zerodha/tradebook")
def api_zerodha_tradebook():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_tradebook())


@app.route("/api/broker/zerodha/gtts")
def api_zerodha_gtts():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.get_gtts())


@app.route("/api/broker/zerodha/gtt/create", methods=["POST"])
def api_zerodha_gtt_create():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    body = request.get_json(silent=True) or {}
    return jsonify(broker_zerodha.create_gtt(body))


@app.route("/api/broker/zerodha/gtt/modify", methods=["POST"])
def api_zerodha_gtt_modify():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    body = request.get_json(silent=True) or {}
    gtt_id = body.pop("gtt_id", None)
    if not gtt_id:
        return jsonify({"ok": False, "error": "missing gtt_id"})
    return jsonify(broker_zerodha.modify_gtt(gtt_id, body))


@app.route("/api/broker/zerodha/gtt/delete", methods=["POST"])
def api_zerodha_gtt_delete():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    body = request.get_json(silent=True) or {}
    gid = body.get("gtt_id")
    if not gid:
        return jsonify({"ok": False, "error": "missing gtt_id"})
    return jsonify(broker_zerodha.delete_gtt(gid))


@app.route("/api/broker/zerodha/ltp")
def api_zerodha_ltp():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    ex  = request.args.get("exchange", "NSE")
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"ok": False, "error": "missing symbol"})

    res = broker_zerodha.get_ltp(ex, sym)
    if res.get("ok"):
        return jsonify(res)

    try:
        import yfinance as yf
        if ex == "NSE":
            yf_sym = sym + ".NS"
        elif ex == "BSE":
            yf_sym = sym + ".BO"
        else:
            yf_sym = sym

        df = yf.download(yf_sym, period="5d", interval="1d",
                         progress=False, auto_adjust=False)
        if df is not None and not df.empty:
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            close = df["Close"].dropna()
            if len(close):
                ltp = float(close.iloc[-1])
                print(f"💡 LTP fallback (yfinance daily) {yf_sym} = {ltp}")
                return jsonify({"ok": True, "last_price": round(ltp, 2),
                                "source": "yfinance_daily"})

        return jsonify({"ok": False,
                        "error": f"LTP unavailable for {sym} (kite: {res.get('error','?')}; yfinance returned no data)"})
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"LTP unavailable. Kite: {res.get('error','?')}. yfinance: {e}"})


@app.route("/api/broker/zerodha/live_trading")
def api_zerodha_live_trading():
    return jsonify({"ok": True, "enabled": bool(broker_zerodha and broker_zerodha.LIVE_TRADING)})


@app.route("/api/broker/zerodha/account")
def api_zerodha_account():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    return jsonify(broker_zerodha.account_summary())


@app.route("/api/broker/zerodha/reset", methods=["POST"])
def api_zerodha_reset():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    broker_zerodha.reset()
    return jsonify({"ok": True})


# ---- Trading API (paper + live) ------------------------------------------

import paper_trades

@app.route("/api/trade/mode")
def api_trade_mode():
    return jsonify({
        "ok": True,
        "live_enabled": bool(broker_zerodha and broker_zerodha.LIVE_TRADING),
    })


def _resolve_ltp(exchange, symbol):
    """Try Zerodha → yfinance daily fallback. Returns (price, source)."""
    if broker_zerodha is not None:
        r = broker_zerodha.get_ltp(exchange, symbol)
        if r.get("ok"): return r["last_price"], "kite"
    try:
        import yfinance as yf
        from data import _SYMBOL_MAP
        
        sym_clean = symbol.strip().upper()
        
        # Strip .NS if it's a known forex pair
        if sym_clean.endswith(".NS") and sym_clean[:-3] in _SYMBOL_MAP:
            sym_clean = sym_clean[:-3]
            
        sym_clean = _SYMBOL_MAP.get(sym_clean, sym_clean)

        # Don't append suffixes if it's an index (^), forex (=X), mapped (=F), or already has a suffix (.)
        if sym_clean.startswith("^") or "." in sym_clean or "=" in sym_clean:
            yf_sym = sym_clean
        elif exchange == "NSE":
            yf_sym = sym_clean + ".NS"
        elif exchange == "BSE":
            yf_sym = sym_clean + ".BO"
        else:
            yf_sym = sym_clean
        df = yf.download(yf_sym, period="5d", interval="1d",
                         progress=False, auto_adjust=False)
        if df is not None and not df.empty:
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            c = df["Close"].dropna()
            if len(c):
                px = float(c.iloc[-1])
                return (round(px, 5) if abs(px) < 10 else round(px, 2)), "yfinance"
    except Exception as e:
        print(f"⚠️ LTP resolve failed: {e}")
    return None, None


@app.route("/api/trade/ltp")
def api_trade_ltp():
    ex = request.args.get("exchange", "NSE")
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"ok": False, "error": "missing symbol"})
    px, src = _resolve_ltp(ex, sym)
    if px is None:
        return jsonify({"ok": False, "error": "no LTP available"})
    return jsonify({"ok": True, "last_price": px, "source": src})

@app.route("/api/trade/ltp_batch", methods=["POST"])
def api_trade_ltp_batch():
    body = request.get_json(silent=True) or {}
    reqs = body.get("symbols", [])
    if not reqs:
        return jsonify({"ok": False, "error": "no symbols"})
    
    from data import get_live_ltp, _SYMBOL_MAP
    yf_syms = []
    for r in reqs:
        sym = r.get("symbol", "").strip().upper()
        exch = r.get("exchange", "NSE").strip().upper()
        if not sym: continue
        
        yf_sym = _SYMBOL_MAP.get(sym, sym)
        if yf_sym.startswith("^") or "." in yf_sym or "=" in yf_sym:
            pass
        elif exch in ("NASDAQ", "NYSE", "AMEX"):
            pass
        elif exch == "NSE":
            yf_sym = yf_sym + ".NS"
        elif exch == "BSE":
            yf_sym = yf_sym + ".BO"
        
        yf_syms.append(yf_sym)
        
    live_prices = get_live_ltp(list(set(yf_syms)))
    out = {}
    for r in reqs:
        sym = r.get("symbol", "").strip().upper()
        exch = r.get("exchange", "NSE").strip().upper()
        yf_sym = _SYMBOL_MAP.get(sym, sym)
        if yf_sym.startswith("^") or "." in yf_sym or "=" in yf_sym:
            pass
        elif exch in ("NASDAQ", "NYSE", "AMEX"):
            pass
        elif exch == "NSE":
            yf_sym = yf_sym + ".NS"
        elif exch == "BSE":
            yf_sym = yf_sym + ".BO"
            
        out[sym] = live_prices.get(yf_sym)
        
    return jsonify({"ok": True, "prices": out})

@app.route("/api/trade/paper/place", methods=["POST"])
def api_trade_paper_place():
    body = request.get_json(silent=True) or {}
    try:
        ltp = None
        if body.get("order_type", "").upper() == "MARKET":
            ltp, _ = _resolve_ltp(body["exchange"], body["symbol"])
        o = paper_trades.place_order(
            symbol       = body["symbol"],
            exchange     = body["exchange"],
            side         = body["side"],
            qty          = body["qty"],
            order_type   = body["order_type"],
            price        = body.get("price"),
            product      = body["product"],
            trigger_price= body.get("trigger_price"),
            ltp          = ltp,
        )
        return jsonify({"ok": True, "order": o})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/trade/paper/orders")
def api_trade_paper_orders():
    orders = paper_trades.list_orders()
    import autotrader
    for o in orders:
        o["ltp"] = None
        for state in autotrader.AUTO_STATE.values():
            if state.get("symbol") == o["symbol"]:
                o["ltp"] = state.get("ltp")
                break
    return jsonify({"ok": True, "orders": orders})


@app.route("/api/trade/paper/positions")
def api_trade_paper_positions():
    pos = paper_trades.positions()
    import autotrader
    for p in pos:
        p["ltp"] = None
        p["target"] = None
        p["sl"] = None
        for state in autotrader.AUTO_STATE.values():
            if state.get("symbol") == p["symbol"]:
                p["ltp"] = state.get("ltp")
                p["target"] = state.get("target_val")
                p["sl"] = state.get("sl_val")
                break
        
        # Recalculate P&L based on the synced Scanner LTP
        if p["ltp"] is not None and p["qty"] != 0:
            if p["qty"] > 0:
                p["unrealized_pnl"] = (p["ltp"] - p["avg_price"]) * p["qty"]
            else:
                p["unrealized_pnl"] = (p["avg_price"] - p["ltp"]) * abs(p["qty"])
            
            if p["avg_price"] > 0:
                p["unrealized_pct"] = (p["unrealized_pnl"] / (p["avg_price"] * abs(p["qty"]))) * 100
                
    return jsonify({"ok": True, "positions": pos})


@app.route("/api/trade/paper/cancel", methods=["POST"])
def api_trade_paper_cancel():
    body = request.get_json(silent=True) or {}
    o = paper_trades.cancel_order(body.get("order_id"))
    if o is None:
        return jsonify({"ok": False, "error": "not found or not open"})
    return jsonify({"ok": True, "order": o})


@app.route("/api/trade/paper/fill", methods=["POST"])
def api_trade_paper_fill():
    body = request.get_json(silent=True) or {}
    o = paper_trades.mark_filled(body.get("order_id"), body.get("fill_price"))
    if o is None:
        return jsonify({"ok": False, "error": "not found or already filled"})
    return jsonify({"ok": True, "order": o})


@app.route("/api/trade/paper/clear", methods=["POST"])
def api_trade_paper_clear():
    paper_trades.clear_all()
    return jsonify({"ok": True})

import autotrader

@app.route("/api/trade/auto/start", methods=["POST"])
def api_trade_auto_start():
    body = request.get_json(silent=True) or {}
    
    # KILL any existing jobs to prevent race conditions (data mixing)
    for jid in list(autotrader.AUTO_JOBS.keys()):
        autotrader.stop_job(jid)
        
    job_id = f"auto_{int(time.time()*1000)}"
    autotrader.start_job(job_id, body)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/trade/auto/stop", methods=["POST"])
def api_trade_auto_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if jid:
        autotrader.stop_job(jid)
    return jsonify({"ok": True})

@app.route("/api/trade/auto/json_files")
def api_trade_auto_json_files():
    import autotrader
    import json
    
    def read_file(path):
        if not path.exists(): return []
        try: 
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except: 
            return []

    alloc_file = autotrader.DATA_DIR / "allocations.json"

    return jsonify({
        "ok": True,
        "paper_processed": read_file(autotrader.PAPER_PROCESSED_FILE),
        "ze_processed": read_file(autotrader.ZE_PROCESSED_FILE),
        "paper_queue": read_file(autotrader.PAPER_QUEUE_FILE),
        "ze_queue": read_file(autotrader.ZE_QUEUE_FILE),
        "allocations": read_file(alloc_file)
    })

@app.route("/api/trade/auto/reset_signals", methods=["POST"])
def api_trade_auto_reset_signals():
    import autotrader
    import paper_trades
    
    # Wipe all 4 files to fully reset the state
    autotrader.PAPER_PROCESSED_FILE.unlink(missing_ok=True)
    autotrader.ZE_PROCESSED_FILE.unlink(missing_ok=True)
    autotrader.PAPER_QUEUE_FILE.unlink(missing_ok=True)
    autotrader.ZE_QUEUE_FILE.unlink(missing_ok=True)
    
    # NEW: Cancel all open paper orders so the bot can re-evaluate immediately
    for o in paper_trades.list_orders():
        if o["status"] == "OPEN":
            paper_trades.cancel_order(o["id"])
    
    return jsonify({"ok": True})


@app.route("/api/trade/auto/update_config", methods=["POST"])
def api_trade_auto_update_config():
    body = request.get_json(silent=True) or {}
    job_id = body.get("job_id")
    new_cfg = body.get("config")
    import autotrader
    if job_id in autotrader.AUTO_JOBS and new_cfg:
        autotrader.AUTO_JOBS[job_id]["config"].update(new_cfg)
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/api/trade/auto/trigger_scan", methods=["POST"])
def api_trade_auto_trigger_scan():
    import autotrader
    autotrader.FORCE_SCAN = True
    return jsonify({"ok": True})

@app.route("/api/trade/auto/fetch_ltp", methods=["POST"])
def api_trade_auto_fetch_ltp():
    import autotrader
    from data import get_live_ltp, _SYMBOL_MAP
    
    # ✅ FIX: Abort if auto-scan is currently running to prevent yfinance clash
    if getattr(autotrader, "IS_SCANNING", False):
        return jsonify({"ok": False, "error": "Auto-Scan in progress"})

    if not autotrader.AUTO_STATE: return jsonify({"ok": True})
    
    yf_syms = []
    # Map symbols correctly for Yahoo Finance based on what's in AUTO_STATE
    for r in autotrader.AUTO_STATE.values():
        sym = r.get("yf_symbol") or r.get("symbol")
        if sym:
            sym_upper = sym.upper()
            # Strip .NS if it's actually a Forex pair
            if sym_upper.endswith(".NS") and sym_upper[:-3] in _SYMBOL_MAP:
                sym_upper = sym_upper[:-3]
            
            mapped_sym = _SYMBOL_MAP.get(sym_upper, sym_upper)
            yf_syms.append(mapped_sym)
            
    if not yf_syms: return jsonify({"ok": True})
    
    try:
        live_ltps = get_live_ltp(list(set(yf_syms)))
    except Exception as e:
        print(f"⚠️ Manual LTP fetch failed: {e}")
        return jsonify({"ok": False, "error": str(e)})
    
    # Get config to recalculate Target/SL based on new LTP
    cfg = {}
    if autotrader.AUTO_JOBS:
        jid = list(autotrader.AUTO_JOBS.keys())[0]
        cfg = autotrader.AUTO_JOBS[jid].get("config", {})
    target_pct = float(cfg.get("target_pct", 2))
    stop_pct = float(cfg.get("stop_pct", 3))

    updated = 0
    for key, r in autotrader.AUTO_STATE.items():
        sym = r.get("yf_symbol") or r.get("symbol")
        if sym:
            sym_upper = sym.upper()
            if sym_upper.endswith(".NS") and sym_upper[:-3] in _SYMBOL_MAP:
                sym_upper = sym_upper[:-3]
            
            mapped_sym = _SYMBOL_MAP.get(sym_upper, sym_upper)
            
            if live_ltps.get(mapped_sym):
                px = live_ltps[mapped_sym]
                new_ltp = round(px, 5) if abs(px) < 10 else round(px, 2)
                r["ltp"] = new_ltp
                updated += 1
                
                action = r.get("wt_action", "")
                if "Position in progress" not in action:
                    if target_pct > 0:
                        r["target_val"] = round(new_ltp * (1 + target_pct/100), 5) if "Buy" in action else round(new_ltp * (1 - target_pct/100), 5)
                    if stop_pct > 0:
                        r["sl_val"] = round(new_ltp * (1 - stop_pct/100), 5) if "Buy" in action else round(new_ltp * (1 + stop_pct/100), 5)
                    elif r.get("valid_opp_val"):
                        r["sl_val"] = r["valid_opp_val"]

    print(f"💡 Manually updated {updated} LTPs via API")
    return jsonify({"ok": True, "updated": updated})

@app.route("/api/trade/auto/save_allocations", methods=["POST"])
def api_trade_auto_save_allocations():
    body = request.get_json(silent=True) or {}
    tasks = body.get("tasks", [])
    
    from pathlib import Path
    file_path = Path(__file__).resolve().parent / "data" / "allocations.json"
    file_path.parent.mkdir(exist_ok=True)
    
    import json
    with open(file_path, "w") as f:
        json.dump(tasks, f, indent=2)
        
    # UPDATE RUNNING JOBS IMMEDIATELY
    import autotrader
    for jid in autotrader.AUTO_JOBS:
        autotrader.AUTO_JOBS[jid]["config"]["tasks"] = tasks
        
    return jsonify({"ok": True, "count": len(tasks)})

@app.route("/api/trade/auto/status")
def api_trade_auto_status():
    import autotrader
    return jsonify({
        "ok": True, 
        "state": autotrader.AUTO_STATE,
        "is_scanning": autotrader.IS_SCANNING,
        "last_scan_time": autotrader.LAST_SCAN_TIME
    })


@app.route("/api/trade/live/place", methods=["POST"])
def api_trade_live_place():
    if broker_zerodha is None:
        return jsonify({"ok": False, "error": "broker module not loaded"})
    body = request.get_json(silent=True) or {}
    return jsonify(broker_zerodha.place_live_order(
        exchange         = body["exchange"],
        tradingsymbol    = body["symbol"],
        transaction_type = body["side"],
        quantity         = body["qty"],
        order_type       = body["order_type"],
        product          = body["product"],
        price            = body.get("price"),
        trigger_price    = body.get("trigger_price"),
        validity         = body.get("validity", "DAY"),
    ))


# ---- Indicators -----------------------------------------------------------

@app.route("/api/indicators")
def api_indicators():
    return jsonify(list_all())


# ---- Symbol resolution ----------------------------------------------------

FNO_SYMBOLS_FALLBACK = ["HAL.NS", "WIPRO.NS", "RELIANCE.NS", "TCS.NS", "INFY.NS",
                        "SBIN.NS", "TATAMOTORS.NS", "VOLTAS.NS",
                        "HDFCBANK.NS", "ICICIBANK.NS"]


def _from_watchlist():
    path = os.path.join(os.path.dirname(__file__), "data", "watchlist.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            arr = json.load(f)
        return [str(s).strip().upper() for s in arr if str(s).strip()]
    except Exception as e:
        print(f"⚠️ watchlist read failed: {e}")
        return []


def _resolve_symbols(body):
    source = (body.get("source") or "").lower()

    if source == "manual":
        raw = (body.get("manual_symbols") or "").strip()
        if not raw:
            return []
        manual_is_us = bool(body.get("manual_us"))
        seen, out = set(), []
        for token in raw.replace(";", ",").replace("\n", ",").split(","):
            s = token.strip().upper()
            if not s:
                continue
            if s.startswith("^") or "." in s or "=" in s:
                sym = s
            elif manual_is_us:
                sym = s
            else:
                sym = s + ".NS"
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
        print(f"📋 Manual symbols: {out}")
        return out

    book = body.get("sheet_name") or "Stock Price Scraper"
    tab  = body.get("input_sheet") or "F&O"

    from_sheet = []
    if source in ("sheet", "both"):
        from_sheet = sheets.read_symbols(book, tab)
        if not from_sheet:
            print("⚠️ Sheet read returned 0 — using hardcoded fallback")
            from_sheet = FNO_SYMBOLS_FALLBACK

    from_watch = _from_watchlist() if source in ("watchlist", "both") else []

    merged, seen = [], set()
    for s in (from_sheet + from_watch):
        if s not in seen:
            seen.add(s)
            merged.append(s)
    return merged


def _params_for(ind_key, body):
    if ind_key == "t3":
        return {"length":  int(body.get("t3_len", 8)),
                "vfactor": float(body.get("t3_vfactor", 0.7))}
    if ind_key == "wt":
        return {"n1": int(body.get("wt_n1", 15)),
                "n2": int(body.get("wt_n2", 27))}
    if ind_key == "st":
        return {"left":  int(body.get("st_left",  15)),
                "right": int(body.get("st_right", 15)),
                "mode":  body.get("st_mode", "strict")}
    if ind_key == "ce":
        return {"length":    int(body.get("ce_len", 22)),
                "mult":      float(body.get("ce_mult", 3.0)),
                "use_close": bool(body.get("ce_close", True))}
    return {}


# ---- Real scan (background thread + SSE) ----------------------------------

@app.route("/api/scanner/run", methods=["POST"])
def api_scanner_run():
    body = request.get_json(silent=True) or {}
    job_id = body.get("job_id") or f"scan_{int(time.time()*1000)}"

    tf          = body.get("tf", "1h")
    indicators  = body.get("indicators", {"t3": True, "wt": True, "ce": False})
    use_ha      = bool(body.get("heikin_ashi", True))
    fno_mode    = body.get("fno_mode", "none")
    fno_list    = set(sheets.read_symbols(body.get("sheet_name", "Stock Price Scraper"), body.get("fno_sheet", "F&O_input"))) if fno_mode == "sheet" else None

    picked = [k for k, v in indicators.items() if v]
    if not picked:
        return jsonify({"ok": False, "error": "no indicators selected"})

    symbols = _resolve_symbols(body)
    if not symbols:
        return jsonify({"ok": False, "error": "no symbols to scan"})

    q = queue.Queue()
    SCANNER_JOBS[job_id] = {
        "q": q, "stop": False, "results": [],
        "total": len(symbols), "done": False
    }

    def worker():
        from settings import YF_CONFIG
        period = YF_CONFIG.get(tf, {}).get("default_period", "60d")
        t0 = time.time()
        print(f"⏳ [job {job_id}] Downloading {len(symbols)} symbols @ {tf} / {period} for {picked}…")

        try:
            force = bool(body.get("force_refresh", False))
            data = download_batch(symbols, interval=tf, period=period, force_refresh=force)
        except Exception as e:
            q.put({"type": "error", "msg": f"download failed: {e}"})
            SCANNER_JOBS[job_id]["done"] = True
            return

        try:
            live_ltp = get_live_ltp(symbols)
        except Exception:
            live_ltp = {}

        stopped_early = False
        for idx, sym in enumerate(symbols):
            if SCANNER_JOBS[job_id]["stop"]:
                stopped_early = True
                break

            sym_rows = data.get(sym, [])
            if not sym_rows or len(sym_rows) < 40:
                continue

            df_real = candles_to_df(sym_rows)
            if fno_mode == "all" or (fno_mode == "sheet" and fno_list and sym in fno_list):
                df_real = apply_fno_filter(df_real, timeframe=tf)
            if len(df_real) < 30:
                continue
            df_ha = heikin_ashi(df_real) if use_ha else df_real

            s0 = time.time()
            live_px = live_ltp.get(sym)
            display_ltp = live_px if live_px is not None else round(float(df_real["Close"].iloc[-2]), 2)

            row = {
                "symbol": sym.replace(".NS", ""),
                "ltp":    round(display_ltp, 2),
                "is_new": False,
                "ms":     0,
            }
            for k in ("t3_trend","t3_value","t3_action","t3_colour",
                      "t3_valid_open","t3_valid_high","t3_valid_low","t3_valid_close",
                      "t3_valid_dt","t3_valid_t3","t3_trigger_status","t3_trigger_dt",
                      "t3_trigger_candle_date","t3_trigger_candle_time",
                      "t3_ltp","t3_diff","t3_diff_pct","t3_profit",
                      "t3_booking_status","t3_max_profit_pct","t3_max_profit_value",
                      "wt1","wt2","wt_diff","wt_cross","wt_trend",
                      "wt_valid_open","wt_valid_high","wt_valid_low","wt_valid_close",
                      "wt_valid_dt","wt_valid_diff","wt_valid_wt_value","wt_perfect",
                      "wt_trigger_status","wt_trigger_dt",
                      "wt_trigger_candle_date","wt_trigger_candle_time",
                      "wt_ltp","wt_trigger_diff","wt_diff_pct","wt_profit",
                      "wt_booking_status","wt_max_profit_pct","wt_max_profit_value",
                      "ce_dir","ce_valid_open","ce_valid_high","ce_valid_low","ce_valid_close",
                      "ce_valid_dt","ce_valid_stop","ce_perfect",
                      "ce_trigger_status","ce_trigger_dt",
                      "ce_trigger_candle_date","ce_trigger_candle_time",
                      "ce_ltp","ce_trigger_diff","ce_diff_pct","ce_profit",
                      "ce_booking_status","ce_max_profit_pct","ce_max_profit_value",
                      "st_resistance","st_support","st_trend","st_valid_open","st_valid_high",
                      "st_valid_low","st_valid_close","st_valid_dt","st_valid_resistance",
                      "st_prev_close","st_r_change_dt","st_s_change_dt","st_dist_to_res_pct",
                      "st_dist_to_sup_pct","st_rsi","st_rsi_dir","st_perfect",
                      "st_trigger_status","st_trigger_dt","st_action","st_trigger_candle_date",
                      "st_trigger_candle_time","st_ltp","st_trigger_diff","st_diff_pct",
                      "st_profit","st_booking_status","st_max_profit_pct","st_max_profit_value"):
                row.setdefault(k, "")

            for key in picked:
                mod = get_indicator(key)
                if mod is None: continue
                try:
                    params = _params_for(key, body)
                    params["investment"]        = float(body.get("invest", 5000))
                    params["profit_target_pct"] = float(body.get("target_pct", 5))
                    params["live_ltp"]          = live_px

                    mem_key    = f"{sym}|{key}"
                    last_state = TRIGGER_STATE.get(mem_key)
                    df_for_ind = df_real if key == "st" else df_ha

                    computed = mod.compute(df_for_ind, params)
                    try:
                        sig = mod.signals(df_for_ind, params, computed, state=last_state)
                    except TypeError:
                        sig = mod.signals(df_for_ind, params, computed)

                    new_state = sig.pop("_new_state", None)
                    if new_state is not None:
                        TRIGGER_STATE[mem_key] = new_state
                    sig.pop(f"{key}_details", None)
                    row.update(sig)
                    if sig.get(f"{key}_triggered"):
                        row["is_new"] = True
                except Exception as e:
                    print(f"⚠️ {key} on {sym}: {e}")

            row["ms"] = int((time.time() - s0) * 1000)

            SCANNER_JOBS[job_id]["results"].append(row)
            q.put({"type": "row", "row": row, "current": idx + 1, "total": len(symbols)})

        total_ms = int((time.time() - t0) * 1000)

        # Optional: write to sheet
        sheet_written = False
        output_sheet = body.get("output_sheet") or ""
        write_back = bool(body.get("write_to_sheet", False))
        book_name = body.get("sheet_name") or "Stock Price Scraper"

        if write_back and output_sheet and SCANNER_JOBS[job_id]["results"]:
            headers = ["Symbol", "LTP"]
            for key in picked:
                mod = get_indicator(key)
                if mod and "columns" in mod.META:
                    for col in mod.META["columns"]:
                        headers.append(col["label"])
            headers += ["New", "Time(ms)"]

            data_rows = []
            for r in SCANNER_JOBS[job_id]["results"]:
                line = [r.get("symbol", ""), r.get("ltp", "")]
                for key in picked:
                    mod = get_indicator(key)
                    if mod and "columns" in mod.META:
                        for col in mod.META["columns"]:
                            line.append(r.get(col["key"], ""))
                line.append("Y" if r.get("is_new") else "")
                line.append(r.get("ms", ""))
                data_rows.append(line)

            try:
                sheet_written = sheets.write_results(book_name, output_sheet, headers, data_rows)
            except Exception as e:
                print(f"❌ Sheet write-back failed: {e}")

        _save_trigger_state(TRIGGER_STATE)

        SCANNER_JOBS[job_id]["done"] = True
        q.put({
            "type": "done",
            "count": len(SCANNER_JOBS[job_id]["results"]),
            "stopped_early": stopped_early,
            "total_ms": total_ms,
            "sheet_written": sheet_written,
            "output_sheet": output_sheet
        })

    threading.Thread(target=worker, daemon=True).start()

    return jsonify({"ok": True, "job_id": job_id, "count": len(symbols)})


@app.route("/api/scanner/stream")
def api_scanner_stream():
    import json as _json
    from flask import Response
    job_id = request.args.get("job")
    if not job_id or job_id not in SCANNER_JOBS:
        return jsonify({"ok": False, "error": "unknown job"})

    def gen():
        job = SCANNER_JOBS[job_id]
        q = job["q"]

        # 1. Replay history
        for i, row in enumerate(job["results"]):
            item = {"type": "row", "row": row, "current": i+1, "total": job["total"]}
            yield f"data: {_json.dumps(item)}\n\n"

        if job["done"]:
            yield f"data: {_json.dumps({'type': 'done', 'count': len(job['results'])})}\n\n"
            return

        # 2. Stream new items
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {_json.dumps(item)}\n\n"
            if item.get("type") in ("done", "stopped"):
                break

    return Response(gen(), mimetype="text/event-stream")


# ---- Last-result restore endpoints ---------------------------------------

@app.route("/api/scanner/last")
def api_scanner_last():
    age = time.time() - LAST_SCAN_CACHE["ts"]
    if not LAST_SCAN_CACHE["rows"] or age > 1800:
        return jsonify({"ok": False, "error": "no recent scan"})
    return jsonify({"ok": True, "rows": LAST_SCAN_CACHE["rows"],
                    "meta": LAST_SCAN_CACHE["meta"], "age": int(age)})


@app.route("/api/scanner/load_saved")
def api_scanner_load_saved():
    """Read a previously-written scanner output tab from Google Sheets and
    return rows in the same shape the scanner table expects (dict keyed by
    column key like wt1, wt2, t3_trend, etc.)."""
    book = request.args.get("book") or ""
    tab  = (request.args.get("tab") or "").strip()
    if not book:
        return jsonify({"ok": False, "error": "no book specified"})
    if not tab:
        return jsonify({"ok": False, "error": "no tab specified"})

    raw = sheets.read_results(book, tab)
    if not raw:
        return jsonify({"ok": False,
                        "error": f"no rows found in {book}::{tab}"})

    label_to_key = {}
    for ind_key in list_all().keys():
        mod = get_indicator(ind_key)
        if mod and "columns" in mod.META:
            for col in mod.META["columns"]:
                label_to_key[col["label"].strip().lower()] = col["key"]

    fixed_map = {
        "symbol":   "symbol",
        "ltp":      "ltp",
        "new":      "is_new",
        "time(ms)": "ms",
    }

    rows = []
    for r in raw:
        out = {}
        for header, value in r.items():
            h = str(header).strip().lower()
            if h in fixed_map:
                k = fixed_map[h]
            elif h in label_to_key:
                k = label_to_key[h]
            else:
                k = h
            if k == "is_new":
                out["is_new"] = str(value).strip().upper() in ("Y", "YES", "TRUE", "1")
            elif k == "ms":
                try:
                    out["ms"] = int(float(str(value).replace(",", "")))
                except (ValueError, TypeError):
                    out["ms"] = 0
            else:
                out[k] = value
        if out.get("symbol"):
            rows.append(out)

    print(f"📥 Loaded {len(rows)} scanner rows from {book}::{tab}")
    return jsonify({"ok": True, "rows": rows, "count": len(rows),
                    "book": book, "tab": tab})


@app.route("/api/backtest/last")
def api_backtest_last():
    age = time.time() - LAST_BACKTEST_CACHE["ts"]
    if not LAST_BACKTEST_CACHE["data"] or age > 1800:
        return jsonify({"ok": False, "error": "no recent backtest"})
    d = dict(LAST_BACKTEST_CACHE["data"])
    d["age"] = int(age)
    return jsonify(d)


# ---- Trigger state endpoints ----------------------------------------------

@app.route("/api/trigger_state")
def api_trigger_state():
    return jsonify({
        "ok": True,
        "file": str(STATE_FILE),
        "count": len(TRIGGER_STATE),
        "state": TRIGGER_STATE,
    })


@app.route("/api/trigger_state/clear", methods=["POST"])
def api_trigger_state_clear():
    global TRIGGER_STATE
    TRIGGER_STATE = {}
    _save_trigger_state(TRIGGER_STATE)
    print("🧹 Trigger state cleared")
    return jsonify({"ok": True})


# ---- Backtest -------------------------------------------------------------

@app.route("/api/backtest/sr_segments")
def api_backtest_sr_segments():
    """Diagnostic: return S/R segments (which bars a level held for) with no
    order simulation. Used to verify pivot detection against TradingView."""
    import backtest as bt
    symbol   = request.args.get("symbol", "HAL.NS")
    interval = request.args.get("interval", "1h")
    period   = request.args.get("period", "6mo")
    left     = int(request.args.get("left",  "15"))
    right    = int(request.args.get("right", "15"))
    try:
        res = bt.run_sr_diagnostic(symbol, interval=interval, period=period,
                                    left=left, right=right)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})
    return jsonify(res)


@app.route("/api/backtest/events")
def api_backtest_events():
    import backtest as bt
    from data import download as dl
    from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
    from settings import IST

    symbol     = request.args.get("symbol", "HAL.NS")
    interval   = request.args.get("interval", "1h")
    period     = request.args.get("period", "6mo")
    buffer_pts = float(request.args.get("buffer", "1"))
    use_ha     = request.args.get("ha", "1") == "1"
    use_fno    = request.args.get("fno", "1") == "1"

    rows = dl(symbol, interval, period)
    if not rows:
        return jsonify({"ok": False, "error": f"no data for {symbol}"})
    df = candles_to_df(rows)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    if use_fno and interval in ("5m","15m","30m","1h"):
        df = apply_fno_filter(df, interval)
    if use_ha:
        df = heikin_ashi(df)

    try:
        events = bt.detect_wt_events(df, {}, buffer_pts)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    out = []
    for e in events:
        vc = df.iloc[e["valid_i"]]
        out.append({
            "cross_time":    str(df.index[e["valid_i"]]),
            "cross_type":    e["cross"],
            "direction":     e["direction"],
            "perfect":       e["perfect"],
            "valid_open":    round(float(vc["Open"]), 2),
            "valid_high":    round(float(vc["High"]), 2),
            "valid_low":     round(float(vc["Low"]), 2),
            "valid_close":   round(float(vc["Close"]), 2),
            "trigger_time":  str(df.index[e["trigger_i"]]),
            "trigger_level": e["trigger_level"],
            "entry_px":      round(e["trigger_level"] + (buffer_pts if e["direction"]=="buy" else -buffer_pts), 2),
        })
    return jsonify({
        "ok": True,
        "symbol": symbol, "interval": interval, "period": period,
        "buffer": buffer_pts, "heikin_ashi": use_ha, "fno_filter": use_fno,
        "bars": len(df), "events_count": len(out), "events": out,
    })


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    body = request.get_json(silent=True) or {}
    try:
        res = backtest.run(
            symbol            = body.get("symbol", "HAL.NS"),
            interval          = body.get("interval", "1h"),
            period            = body.get("period", "6mo"),
            indicators        = body.get("indicators", {"wt": True}),
            params            = body.get("params", {}),
            capital           = float(body.get("capital", 100000)),
            position_pct      = float(body.get("position_pct", 100)),
            position_amount   = float(body.get("position_amount", 5000)),
            fixed_qty         = int(body.get("fixed_qty", 1)),
            sizing_mode       = body.get("sizing_mode", "fixed"),
            commission_pct    = float(body.get("commission_pct", 0.05)),
            slippage_pct      = float(body.get("slippage_pct", 0.02)),
            buffer_pts        = float(body.get("buffer_pts", 0.01)),
            wait_bars         = int(body.get("wait_bars", 20)),
            profit_target_pct = float(body.get("profit_target_pct", 5)),
            profit_target_mode = body.get("profit_target_mode", "pct"),
            profit_target_value = float(body.get("profit_target_value",
                                                  body.get("profit_target_pct", 1))),
            stop_loss_pct     = float(body.get("stop_loss_pct", 2)),
            exit_on_opposite  = bool(body.get("exit_on_opposite", True)),
            side_mode         = body.get("side_mode", "both"),
            logic             = body.get("logic", "any"),
            match_window      = int(body.get("match_window", 5)),
            st_entry_mode     = body.get("st_entry_mode", "strict"),
            st_exit_mode      = body.get("st_exit_mode",  "strict"),
            st_reentry_mode   = body.get("st_reentry_mode", "standard"),
            wt_pending_rule   = body.get("wt_pending_rule", "every"),
            fno_mode          = body.get("fno_mode", "none"),
            fno_list          = set(sheets.read_symbols(body.get("sheet_name", "Stock Price Scraper"), body.get("fno_sheet", "F&O_input"))) if body.get("fno_mode") == "sheet" else None,
            strategy_mode     = body.get("strategy_mode", "simple"),
            multiplier_type   = body.get("multiplier_type", "martingale"),
            base_qty          = int(body.get("base_qty", 1)),
            max_levels        = int(body.get("max_levels", 5)),
            recovery_enabled         = bool(body.get("recovery_enabled", False)),
            recovery_cumulative      = bool(body.get("recovery_cumulative", False)),
            recovery_buffer_amount   = float(body.get("recovery_buffer_amount", 20.0)),
            recovery_trail_enabled   = bool(body.get("recovery_trail_enabled", False)),
            recovery_trail_premium   = float(body.get("recovery_trail_premium", 40.0)),
            recovery_trail_step      = float(body.get("recovery_trail_step", 5.0)),
            trailing_enabled         = bool(body.get("trailing_enabled", False)),
            trailing_start_pct       = float(body.get("trailing_start_pct", 3.0)),
            trailing_step_pct        = float(body.get("trailing_step_pct", 2.0)),
            trailing_tighten_enabled = bool(body.get("trailing_tighten_enabled", False)),
            trailing_tighten_at      = float(body.get("trailing_tighten_at", 9.0)),
            trailing_tighten_step    = float(body.get("trailing_tighten_step", 1.0)),
            wt_filter_enabled = bool(body.get("wt_filter_enabled", False)),
            wt_buy_max        = float(body.get("wt_buy_max", -40.0)),
            wt_sell_min       = float(body.get("wt_sell_min", 40.0)),
            htf_filter_enabled = bool(body.get("htf_filter_enabled", False)),
            rsi_filter_enabled = bool(body.get("rsi_filter_enabled", False)),
            rsi_period         = int(body.get("rsi_period", 14)),
            vol_filter_enabled = bool(body.get("vol_filter_enabled", False)),
            vol_mult           = float(body.get("vol_mult", 1.5)),
            vol_lookback       = int(body.get("vol_lookback", 20)),
        )
        if res.get("ok"):
            LAST_BACKTEST_CACHE.update({"data": res, "ts": time.time()})
        return jsonify(res)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})

# ---- Batch backtest -------------------------------------------------------

import autotest

@app.route("/api/backtest/autotest/start", methods=["POST"])
def api_autotest_start():
    body = request.get_json(silent=True) or {}
    cfg = body.get("config", {})
    vary = body.get("vary", [])

    job_id = f"at_{int(time.time()*1000)}_{os.urandom(3).hex()}"

    if cfg.get("fno_mode") == "sheet":
        cfg["fno_list"] = set(sheets.read_symbols(cfg.get("sheet_name", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input")))

    autotest.start_job(job_id, cfg, vary)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/backtest/autotest/stream")
def api_autotest_stream():
    import json as _json
    from flask import Response
    job_id = request.args.get("job")
    if not job_id or job_id not in autotest.JOBS:
        return jsonify({"ok": False, "error": "unknown job"})

    def gen():
        job = autotest.JOBS[job_id]
        q = job["q"]

        history = job.get("results", [])
        total = job.get("total", 1)
        for i, row in enumerate(history):
            item = {"type": "row", "row": row, "current": i+1, "total": total}
            yield f"data: {_json.dumps(item)}\n\n"

        if job.get("done"):
            yield f"data: {_json.dumps({'type': 'done', 'count': len(history)})}\n\n"
            return

        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {_json.dumps(item)}\n\n"
            if item.get("type") in ("done", "stopped"):
                job["done"] = True
                break

    return Response(gen(), mimetype="text/event-stream")

@app.route("/api/backtest/autotest/stop", methods=["POST"])
def api_autotest_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    autotest.stop_job(jid)
    return jsonify({"ok": True})

@app.route("/api/backtest/batch/start", methods=["POST"])
def api_backtest_batch_start():
    body = request.get_json(silent=True) or {}
    source       = body.get("source", "sheet")
    book         = body.get("sheet_name") or "Backtest scanner"
    tab          = body.get("input_sheet") or "zero_input"
    manual       = body.get("manual_symbols") or ""
    settings     = body.get("settings") or {}

    symbols = []
    if source == "manual":
        for token in manual.replace(";", ",").replace("\n", ",").split(","):
            s = token.strip().upper()
            if not s: continue
            if not s.startswith("^") and "." not in s and not s.endswith("=X") and "=" not in s:
                s += ".NS"
            symbols.append(s)
    else:
        raw = sheets.read_symbols(book, tab)
        symbols = raw or []

    if not symbols:
        return jsonify({"ok": False, "error": "no symbols resolved"})

    job_id = f"bt_{int(time.time()*1000)}_{os.urandom(3).hex()}"

    kwargs = {
        "interval":          settings.get("interval", "1h"),
        "period":            settings.get("period", "6mo"),
        "indicators":        settings.get("indicators", {"wt": True}),
        "params":            settings.get("params", {}),
        "capital":           float(settings.get("capital", 100000)),
        "position_pct":      float(settings.get("position_pct", 100)),
        "position_amount":   float(settings.get("position_amount", 5000)),
        "fixed_qty":         int(settings.get("fixed_qty", 1)),
        "sizing_mode":       settings.get("sizing_mode", "fixed"),
        "commission_pct":    float(settings.get("commission_pct", 0.05)),
        "slippage_pct":      float(settings.get("slippage_pct", 0.02)),
        "buffer_pts":        float(settings.get("buffer_pts", 1)),
        "wait_bars":         int(settings.get("wait_bars", 20)),
        "profit_target_mode":  settings.get("profit_target_mode", "pct"),
        "profit_target_value": float(settings.get("profit_target_value", 1)),
        "profit_target_pct":   float(settings.get("profit_target_pct", 1)),
        "stop_loss_pct":       float(settings.get("stop_loss_pct", 3)),
        "exit_on_opposite":    bool(settings.get("exit_on_opposite", True)),
        "side_mode":           settings.get("side_mode", "both"),
        "st_entry_mode":       settings.get("st_entry_mode", "strict"),
        "st_exit_mode":        settings.get("st_exit_mode",  "strict"),
        "st_reentry_mode":     settings.get("st_reentry_mode", "standard"),
        "wt_pending_rule":     settings.get("wt_pending_rule", "every"),
        "fno_mode":            settings.get("fno_mode", "none"),
        "fno_list":            set(sheets.read_symbols(settings.get("sheet_name", "Stock Price Scraper"), settings.get("fno_sheet", "F&O_input"))) if settings.get("fno_mode") == "sheet" else None,
        "strategy_mode":       settings.get("strategy_mode", "simple"),
        "multiplier_type":     settings.get("multiplier_type", "martingale"),
        "base_qty":            int(settings.get("base_qty", 1)),
        "max_levels":          int(settings.get("max_levels", 5)),
        "recovery_enabled":       bool(settings.get("recovery_enabled", False)),
        "recovery_cumulative":    bool(settings.get("recovery_cumulative", False)),
        "recovery_buffer_amount": float(settings.get("recovery_buffer_amount", 20.0)),
        "recovery_trail_enabled": bool(settings.get("recovery_trail_enabled", False)),
        "recovery_trail_premium": float(settings.get("recovery_trail_premium", 40.0)),
        "recovery_trail_step":    float(settings.get("recovery_trail_step", 5.0)),
        "trailing_enabled":         bool(settings.get("trailing_enabled", False)),
        "trailing_start_pct":       float(settings.get("trailing_start_pct", 3)),
        "trailing_step_pct":        float(settings.get("trailing_step_pct", 2)),
        "trailing_tighten_enabled": bool(settings.get("trailing_tighten_enabled", False)),
        "trailing_tighten_at":      float(settings.get("trailing_tighten_at", 9)),
        "trailing_tighten_step":    float(settings.get("trailing_tighten_step", 1)),
        "wt_filter_enabled":  bool(settings.get("wt_filter_enabled", False)),
        "wt_buy_max":         float(settings.get("wt_buy_max", -40.0)),
        "wt_sell_min":        float(settings.get("wt_sell_min", 40.0)),
        "htf_filter_enabled": bool(settings.get("htf_filter_enabled", False)),
        "rsi_filter_enabled": bool(settings.get("rsi_filter_enabled", False)),
        "rsi_period":         int(settings.get("rsi_period", 14)),
        "vol_filter_enabled": bool(settings.get("vol_filter_enabled", False)),
        "vol_mult":           float(settings.get("vol_mult", 1.5)),
        "vol_lookback":       int(settings.get("vol_lookback", 20)),
    }

    batch_backtest.start_job(job_id, symbols, kwargs)
    batch_backtest.JOBS[job_id]["total"] = len(symbols)
    print(f"🚀 Batch {job_id}: {len(symbols)} symbols from {book}::{tab}")
    return jsonify({"ok": True, "job_id": job_id, "count": len(symbols)})


@app.route("/api/backtest/batch/stream")
def api_backtest_batch_stream():
    import json as _json
    from flask import Response
    job_id = request.args.get("job")
    if not job_id or job_id not in batch_backtest.JOBS:
        return jsonify({"ok": False, "error": "unknown job"})

    def gen():
        job = batch_backtest.JOBS[job_id]
        q = job["q"]

        history = job.get("results", [])
        total = job.get("total", len(history))
        for i, row in enumerate(history):
            item = {"type": "row", "row": row, "current": i+1, "total": total}
            yield f"data: {_json.dumps(item)}\n\n"

        if job.get("done"):
            yield f"data: {_json.dumps({'type': 'done', 'count': len(history)})}\n\n"
            return

        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {_json.dumps(item)}\n\n"
            if item.get("type") in ("done", "stopped"):
                job["done"] = True
                break

    return Response(gen(), mimetype="text/event-stream")


@app.route("/api/backtest/batch/stop", methods=["POST"])
def api_backtest_batch_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    ok = batch_backtest.stop_job(jid)
    return jsonify({"ok": ok})


@app.route("/api/backtest/batch/write", methods=["POST"])
def api_backtest_batch_write():
    body      = request.get_json(silent=True) or {}
    book      = body.get("sheet_name") or "Backtest scanner"
    rows      = body.get("rows") or []
    in_tab    = body.get("input_sheet") or "zero_input"
    tf        = body.get("tf") or "1h"
    picked    = body.get("indicators") or {"wt": True}
    if not rows:
        return jsonify({"ok": False, "error": "no rows"})

    custom = (body.get("output_tab") or "").strip()
    if custom:
        out_tab = custom
    else:
        prefix = (in_tab.split("_")[0] or "ind").replace("&", "").replace(" ", "")
        mid = "_".join(k.upper() for k, v in picked.items() if v) or "scan"
        out_tab = f"{prefix}_{mid}_{tf}"

    headers = [
        "Symbol", "Return %", "Buy&Hold %", "Trades", "Win %", "PF",
        "Max DD %", "Net ₹", "Max win ₹", "Max loss ₹", "Sharpe",
        "Final equity", "Capital", "Best trade side", "Best trade ₹",
        "Worst trade side", "Worst trade ₹", "Time (ms)",
    ]
    data_rows = []
    for r in rows:
        best  = r.get("best_trade")  or {}
        worst = r.get("worst_trade") or {}
        data_rows.append([
            r.get("symbol", ""),
            r.get("return_pct", 0),
            r.get("buy_hold_pct", 0),
            r.get("trades", 0),
            r.get("win_rate", 0),
            r.get("profit_factor", 0),
            r.get("max_dd_pct", 0),
            r.get("net_pnl", 0),
            r.get("max_win", 0),
            r.get("max_loss", 0),
            r.get("sharpe", 0),
            r.get("final_equity", 0),
            r.get("capital", 0),
            best.get("side", ""),
            best.get("pnl", ""),
            worst.get("side", ""),
            worst.get("pnl", ""),
            r.get("ms", 0),
        ])

    try:
        ok = sheets.write_results(book, out_tab, headers, data_rows,
                                  summary=f"Batch backtest · {len(data_rows)} symbols")
        print(f"📄 Batch wrote {len(data_rows)} rows to {book}::{out_tab}")
        return jsonify({"ok": ok, "output_sheet": out_tab, "count": len(data_rows)})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/backtest/batch/results")
def api_backtest_batch_results():
    jid = request.args.get("job")
    if not jid or jid not in batch_backtest.JOBS:
        return jsonify({"ok": False, "error": "unknown job"})
    return jsonify({"ok": True, "results": batch_backtest.JOBS[jid]["results"]})


@app.route("/api/backtest/batch/load")
def api_backtest_batch_load():
    book = request.args.get("book") or "Backtest scanner"
    tab  = (request.args.get("tab") or "").strip()
    if not tab:
        return jsonify({"ok": False, "error": "no tab specified"})

    raw = sheets.read_results(book, tab)
    if not raw:
        return jsonify({"ok": False,
                        "error": f"no rows found in {book}::{tab}"})

    def _f(v, default=0.0):
        try:
            if v == "" or v is None:
                return default
            s = str(v).replace(",", "").replace("₹", "").replace("$", "").strip()
            return float(s)
        except (TypeError, ValueError):
            return default

    def _i(v, default=0):
        try:
            if v == "" or v is None:
                return default
            s = str(v).replace(",", "").strip()
            return int(float(s))
        except (TypeError, ValueError):
            return default

    rows = []
    for r in raw:
        symbol = (r.get("Symbol") or "").strip()
        if not symbol:
            continue
        rows.append({
            "ok":            True,
            "symbol":        symbol,
            "return_pct":    _f(r.get("Return %")),
            "buy_hold_pct":  _f(r.get("Buy&Hold %")),
            "trades":        _i(r.get("Trades")),
            "win_rate":      _f(r.get("Win %")),
            "profit_factor": _f(r.get("PF")),
            "max_dd_pct":    _f(r.get("Max DD %")),
            "net_pnl":       _f(r.get("Net ₹")),
            "max_win":       _f(r.get("Max win ₹")),
            "max_loss":      _f(r.get("Max loss ₹")),
            "sharpe":        _f(r.get("Sharpe")),
            "ms":            _i(r.get("Time (ms)")),
        })

    print(f"📥 Loaded {len(rows)} rows from {book}::{tab}")
    return jsonify({"ok": True, "rows": rows,
                    "count": len(rows), "book": book, "tab": tab})


# ---- Cancel ---------------------------------------------------------------

@app.route("/api/scanner/stop", methods=["POST"])
def api_scanner_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    if jid in SCANNER_JOBS:
        SCANNER_JOBS[jid]["stop"] = True
    return jsonify({"ok": True})


# ---- Debug ---------------------------------------------------------------

@app.route("/api/test/indicators")
def api_test_indicators():
    symbol   = request.args.get("symbol", "HAL.NS")
    interval = request.args.get("interval", "1h")
    period   = request.args.get("period", "1mo")
    rows = download(symbol, interval, period)
    if not rows:
        return jsonify({"ok": False, "error": f"no data for {symbol}"})
    df = candles_to_df(rows)
    df = heikin_ashi(df)
    out = {}
    for key in list_all().keys():
        mod = get_indicator(key)
        try:
            comp = mod.compute(df, {})
            sig  = mod.signals(df, {}, comp)
            out[key] = {k: v for k, v in sig.items() if not k.endswith("_details")}
        except Exception as e:
            out[key] = {"error": str(e)}
    return jsonify({"ok": True, "symbol": symbol, "bars": len(df), "indicators": out})


if __name__ == "__main__":
    print(f"\n🚀 Scanner running at  http://localhost:{PORT}")
    print(f"   Service account: {sheets.service_account_email() or '(none)'}")
    if broker_zerodha is not None:
        print(f"   Zerodha API key : {broker_zerodha.API_KEY[:6] + '...' if broker_zerodha.API_KEY else '(not set)'}")
        print(f"   Zerodha secret  : {'set' if broker_zerodha.API_SECRET else '(not set)'}")
        print(f"   Zerodha port    : {broker_zerodha.CALLBACK_PORT}   ← this is the value in .env")
        print(f"   Zerodha token   : {'present' if broker_zerodha.TOKEN_FILE.exists() else '(none)'}")
    else:
        print(f"   Zerodha module  : NOT LOADED")
    print("   Press Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=PORT, debug=True)