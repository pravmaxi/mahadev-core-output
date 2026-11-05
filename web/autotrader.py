import threading
import time
import json
from pathlib import Path
import pandas as pd
import pytz
import paper_trades

from data import download_batch, get_live_ltp, _SYMBOL_MAP
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
from indicators import get as get_indicator
import sheets

IST = pytz.timezone("Asia/Kolkata")
AUTO_JOBS = {}
AUTO_STATE = {}
IS_SCANNING = False
FORCE_SCAN = False
LAST_SCAN_TIME = 0
TRIGGER_STATE = {} 

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ALLOC_FILE = DATA_DIR / "allocations.json"
PAPER_PROCESSED_FILE = DATA_DIR / "paper_processed.json"
PAPER_QUEUE_FILE = DATA_DIR / "paper_orders_queue.json"
AUTO_STATE_FILE = DATA_DIR / "auto_state_cache.json" # ✅ NEW: Cache for instant UI load
ZE_PROCESSED_FILE = DATA_DIR / "zerodha_processed.json"
ZE_QUEUE_FILE = DATA_DIR / "zerodha_orders_queue.json"

def start_job(job_id, config):
    AUTO_STATE.clear()
    # ✅ Load cached state so UI doesn't reset on server restart
    if AUTO_STATE_FILE.exists():
        try: AUTO_STATE.update(json.loads(AUTO_STATE_FILE.read_text()))
        except: pass
        
    AUTO_JOBS[job_id] = {"stop": False, "config": config, "is_first_scan": True}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def stop_job(job_id):
    if job_id in AUTO_JOBS:
        AUTO_JOBS[job_id]["stop"] = True

def get_active_pending_signals(df, params, buffer_pts, max_wait, pending_rule):
    """
    Scans historical closed candles to find:
    1. Currently active pending signals.
    2. The absolute latest signal (whether it triggered, timed out, or is pending) for UI display.
    """
    mod = get_indicator("wt")
    comp = mod.compute(df, params)
    cross = comp["cross"]
    wt1 = comp["wt1"]
    wt2 = comp["wt2"]
    n = len(df)
    pend = {"buy": None, "sell": None}
    latest_signal = {"buy": None, "sell": None} 

    for i in range(1, n):
        h = float(df.iloc[i]["High"])
        l = float(df.iloc[i]["Low"])
        
        for direction in ("buy", "sell"):
            p = pend[direction]
            if p is None: continue
            
            # Check trigger
            hit = ((direction == "buy"  and h > p["level"] + buffer_pts) or
                   (direction == "sell" and l < p["level"] - buffer_pts))
            if hit:
                # ✅ FIX: Only overwrite latest_signal if this triggered signal is newer
                if not latest_signal[direction] or p["valid_i"] >= latest_signal[direction]["valid_i"]:
                    latest_signal[direction] = {
                        "status": "triggered",
                        "level": p["level"],
                        "wt_val": p["wt_val"],
                        "valid_i": p["valid_i"],
                        "valid_time": str(df.index[p["valid_i"]]).split("+")[0].replace("T", " "),
                        "trigger_time": str(df.index[i]).split("+")[0].replace("T", " ")
                    }
                pend[direction] = None # Triggered and consumed
                
            elif i - p["valid_i"] >= max_wait:
                pend[direction] = None # Timed out
        
        # Check new cross
        c = cross.iloc[i]
        if c in ("Bull Cross", "Bear Cross"):
            direction = "buy" if c == "Bull Cross" else "sell"
            vc = df.iloc[i]
            level = float(vc["High"] if direction == "buy" else vc["Low"])
            _v = wt1.iloc[i] if direction == "buy" else wt2.iloc[i]
            wt_val = None if (_v != _v) else round(float(_v), 2)
            
            pend[direction] = {
                "valid_i": i, "level": level, "wt_val": wt_val
            }
            # ✅ FIX: A new cross always becomes the latest signal for the UI
            latest_signal[direction] = {
                "status": "pending",
                "level": level,
                "wt_val": wt_val,
                "valid_i": i,
                "valid_time": str(df.index[i]).split("+")[0].replace("T", " "),
                "trigger_time": "Pending"
            }
            
            if pending_rule == "any_dir":
                opposite = "sell" if direction == "buy" else "buy"
                pend[opposite] = None
                
    return pend, latest_signal

def _process_paper_queue():
    if not PAPER_QUEUE_FILE.exists(): return
    try: queue = json.loads(PAPER_QUEUE_FILE.read_text())
    except: return
    
    if not queue: return
    
    processed = []
    if PAPER_PROCESSED_FILE.exists():
        try: processed = json.loads(PAPER_PROCESSED_FILE.read_text())
        except: pass
        
    for q in queue:
        try:
            o_type = q.get("order_type", "MARKET")
            limit_px = q.get("price") or q.get("signal_level") or q.get("ltp") or 0
                
            o = paper_trades.place_order(
                symbol=q["symbol"], exchange=q["exchange"], side=q["side"], 
                qty=q["qty"], order_type=o_type, price=limit_px if o_type=="LIMIT" else None, product="MIS", ltp=q.get("ltp") or limit_px,
                tag=q.get("tag") # ✅ Pass the exit tag
            )
            if o_type == "MARKET":
                paper_trades.mark_filled(o["id"], q.get("ltp") or limit_px)
            
            processed.append({
                "id": q["id"],
                "symbol": q["symbol"],
                "tf": q["tf"],
                "side": q["side"],
                "signal_level": q.get("signal_level"),
                "signal_time": q.get("signal_time"),
                "executed_time": str(pd.Timestamp.now(tz=IST))
            })
        except Exception as e:
            print(f"Queue execution failed for {q.get('symbol')}: {e}")
            
    PAPER_PROCESSED_FILE.write_text(json.dumps(processed, indent=2))
    PAPER_QUEUE_FILE.write_text("[]")

def _check_pending_limits(downloaded_data):
    try: orders = paper_trades.list_orders()
    except: return
    for o in orders:
        if o["status"] == "OPEN" and o.get("order_type") == "LIMIT":
            live_px = None
            for k, v in downloaded_data.items():
                if k.endswith(o["symbol"]) or k.endswith(o["symbol"] + ".NS") or k.endswith(o["symbol"] + ".BO"):
                    try:
                        df_real = candles_to_df(v)
                        live_px = float(df_real["Close"].iloc[-1])
                    except: pass
                    break
            
            if live_px is not None:
                limit_px = o.get("price")
                if limit_px:
                    filled = False
                    if o["side"] == "BUY" and live_px > limit_px:
                        paper_trades.mark_filled(o["id"], limit_px)
                        print(f"✅ Limit Buy filled for {o['symbol']} at Limit {limit_px}")
                        filled = True
                    elif o["side"] == "SELL" and live_px < limit_px:
                        paper_trades.mark_filled(o["id"], limit_px)
                        print(f"✅ Limit Sell filled for {o['symbol']} at Limit {limit_px}")
                        filled = True
                        
                    # ✅ NEW: Instantly update AUTO_STATE so the UI reflects "Position Open" immediately
                    if filled:
                        for k, v in AUTO_STATE.items():
                            if v.get("symbol") == o["symbol"]:
                                v["overall_status"] = f"Position Open ({o['side'].capitalize()})"
                                v["sent_to"] = ""
                                v["wt_action"] = f"Position In Progress ({o['side'].capitalize()})"

def _check_paper_exits(cfg, downloaded_data):
    try: positions = paper_trades.positions()
    except: return
    if not positions: return
    
    for p in positions:
        if p.get("qty") == 0: continue
        
        live_px = None
        for k, v in downloaded_data.items():
            if k.endswith(p["symbol"]) or k.endswith(p["symbol"] + ".NS") or k.endswith(p["symbol"] + ".BO"):
                try:
                    df_real = candles_to_df(v)
                    live_px = float(df_real["Close"].iloc[-1])
                except: pass
                break
        
        if live_px is None: continue
        
        side = "buy" if p["qty"] > 0 else "sell"
        avg_px = p["avg_price"]
        target_pct = float(cfg.get("target_pct", 0))
        stop_pct = float(cfg.get("stop_pct", 0))
        
        exit_reason = None
        
        if target_pct > 0:
            if side == "buy" and live_px >= avg_px * (1 + target_pct/100): exit_reason = "Target"
            if side == "sell" and live_px <= avg_px * (1 - target_pct/100): exit_reason = "Target"
            
        if not exit_reason and stop_pct > 0:
            if side == "buy" and live_px <= avg_px * (1 - stop_pct/100): exit_reason = "Stop Loss"
            if side == "sell" and live_px >= avg_px * (1 + stop_pct/100): exit_reason = "Stop Loss"
            
        if not exit_reason and cfg.get("exit_opp", True):
            for sym_key, state in AUTO_STATE.items():
                if state.get("symbol") == p["symbol"]:
                    opp_val = state.get("valid_opp_val")
                    if opp_val:
                        if side == "buy" and live_px <= opp_val: exit_reason = "Opp Signal"
                        if side == "sell" and live_px >= opp_val: exit_reason = "Opp Signal"
                    break
        
        if exit_reason:
            close_side = "SELL" if p["qty"] > 0 else "BUY"
            try:
                o = paper_trades.place_order(
                    symbol=p["symbol"], exchange=p["exchange"], side=close_side,
                    qty=abs(p["qty"]), order_type="MARKET", price=None, product=p["product"], ltp=live_px,
                    tag=f"Exit ({exit_reason})" # ✅ Pass the tag to the order ledger
                )
                paper_trades.mark_filled(o["id"], live_px)
                print(f"✅ Exit triggered for {p['symbol']} - {exit_reason}")
            except Exception as e:
                print(f"❌ Exit failed for {p['symbol']}: {e}")

def _worker(job_id):
    tasks = AUTO_JOBS[job_id]["config"].get("tasks", [])
    print(f"🚀 [AUTO] Started Job {job_id} with {len(tasks)} tasks")
    
    while not AUTO_JOBS[job_id]["stop"]:
        try:
            global IS_SCANNING, FORCE_SCAN, LAST_SCAN_TIME
            
            if AUTO_JOBS[job_id]["config"].get("paused", False):
                IS_SCANNING = False
                time.sleep(1)
                continue
                
            IS_SCANNING = True
            
            if ALLOC_FILE.exists():
                try: tasks = json.loads(ALLOC_FILE.read_text())
                except: pass
            
            current_keys = {f"{t['sym']}_{t['tf']}" for t in tasks}
            for k in list(AUTO_STATE.keys()):
                if k not in current_keys:
                    del AUTO_STATE[k]
                
            cfg = AUTO_JOBS[job_id]["config"]
            
            sym_map = {}
            for t in tasks:
                raw_sym = t["sym"]
                exch = t["exch"]
                sym_yf = raw_sym.upper().replace(" ", "")
                if exch == "NSE" and not sym_yf.endswith(".NS") and "." not in sym_yf and "=" not in sym_yf and not sym_yf.startswith("^"):
                    sym_yf = sym_yf + ".NS"
                elif exch == "BSE" and not sym_yf.endswith(".BO") and "." not in sym_yf and "=" not in sym_yf and not sym_yf.startswith("^"):
                    sym_yf = sym_yf + ".BO"
                else:
                    sym_yf = _SYMBOL_MAP.get(sym_yf, sym_yf)
                sym_map[f"{raw_sym}_{t['tf']}"] = sym_yf

            tf_groups = {}
            for task in tasks:
                tf = task["tf"]
                key = f"{task['sym']}_{tf}"
                tf_groups.setdefault(tf, []).append(sym_map[key])
                
            downloaded_data = {}
            for tf, syms in tf_groups.items():
                if tf in ["1m", "2m", "5m", "15m", "30m"]:
                    period = "60d"
                elif tf in ["60m", "90m", "1h"]:
                    period = "3mo"
                else:
                    period = "1y"
                try:
                    res = download_batch(syms, interval=tf, period=period, force_refresh=True)
                    for s_yf, rows in res.items():
                        downloaded_data[f"{tf}_{s_yf}"] = rows
                except Exception as e:
                    print(f"Batch download failed for {tf}: {e}")

            fno_mode = cfg.get("fno_mode", "none")
            fno_list = None
            has_indian_stocks = any(t.get("exch", "").upper() in ["NSE", "BSE"] or str(t.get("sym", "")).upper().endswith((".NS", ".BO")) for t in tasks)
            
            if fno_mode == "sheet" and has_indian_stocks:
                try:
                    raw_fno = sheets.read_symbols(cfg.get("fno_book", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input"))
                    fno_list = {str(s).replace(".NS", "").replace(".BO", "").upper() for s in raw_fno}
                except Exception as e:
                    pass

            use_ha = cfg.get("candles", "ha") == "ha"
            
            for task in tasks:
                if AUTO_JOBS[job_id]["stop"]: break
                
                raw_sym = task["sym"]
                tf = task["tf"]
                key = f"{raw_sym}_{tf}"
                sym_yf = sym_map[key]
                sym_clean = sym_yf.replace(".NS", "").replace(".BO", "").upper()
                
                try:
                    rows = downloaded_data.get(f"{tf}_{sym_yf}", [])
                    if not rows or len(rows) < 40: 
                        AUTO_STATE[key] = {"symbol": raw_sym, "status": "No Data"}
                        continue
                    
                    df_real = candles_to_df(rows)
                    if df_real.index.tz is None:
                        df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
                    else:
                        df_real.index = df_real.index.tz_convert(IST)
                        
                    if fno_mode == "all" or (fno_mode == "sheet" and fno_list and sym_clean in fno_list):
                        df_real = apply_fno_filter(df_real, timeframe=tf)
                    
                    if len(df_real) < 30: continue
                    df_ha = heikin_ashi(df_real) if use_ha else df_real
                    live_px = float(df_real["Close"].iloc[-1])
                    
                    def _r(val): return round(val, 5) if abs(val) < 10 else round(val, 2)
                    
                    row = {"symbol": raw_sym, "tf": tf, "ltp": _r(live_px), "yf_symbol": sym_yf}
                    
                    wt_params = {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)}
                    buffer_pts = float(cfg.get("buffer_pts", 0))
                    wait_bars = int(cfg.get("wait_bars", 100))
                    pending_rule = cfg.get("wt_pending_rule", "same_dir")
                    
                    df_closed = df_ha.iloc[:-1] if len(df_ha) > 1 else df_ha
                    pending, latest_signal = get_active_pending_signals(df_closed, wt_params, buffer_pts, wait_bars, pending_rule)
                    
                    triggered_buy = pending["buy"] and live_px > (pending["buy"]["level"] + buffer_pts)
                    triggered_sell = pending["sell"] and live_px < (pending["sell"]["level"] - buffer_pts)
                    
                    wt_mod = get_indicator("wt")
                    wt_comp = wt_mod.compute(df_ha, wt_params)
                    row["wt_trend"] = "Buy Trend" if wt_comp["wt1"].iloc[-1] > wt_comp["wt2"].iloc[-1] else "Sell Trend"
                    
                    row["valid_time"], row["trigger_lvl"], row["wt_val"], row["trigger_time"] = "", None, "", ""
                    row["wt_action"], row["valid_opp_val"], row["valid_opp_dir"] = "", None, ""
                    
                    # Find the most recent signal to display
                    most_recent = None
                    if latest_signal["buy"] and latest_signal["sell"]:
                        most_recent = ("buy", latest_signal["buy"]) if latest_signal["buy"]["valid_i"] > latest_signal["sell"]["valid_i"] else ("sell", latest_signal["sell"])
                    elif latest_signal["buy"]: most_recent = ("buy", latest_signal["buy"])
                    elif latest_signal["sell"]: most_recent = ("sell", latest_signal["sell"])

                    if triggered_buy:
                        row["valid_time"] = latest_signal["buy"]["valid_time"]
                        row["trigger_lvl"] = _r(latest_signal["buy"]["level"])
                        row["wt_val"] = latest_signal["buy"]["wt_val"]
                        row["wt_action"] = f"Buy Triggered @ {_r(live_px)}"
                        row["trigger_time"] = str(df_real.index[-1]).split("+")[0].replace("T", " ")
                        if pending["sell"]:
                            row["valid_opp_val"] = _r(pending["sell"]["level"])
                            row["valid_opp_dir"] = "sell"
                    elif triggered_sell:
                        row["valid_time"] = latest_signal["sell"]["valid_time"]
                        row["trigger_lvl"] = _r(latest_signal["sell"]["level"])
                        row["wt_val"] = latest_signal["sell"]["wt_val"]
                        row["wt_action"] = f"Sell Triggered @ {_r(live_px)}"
                        row["trigger_time"] = str(df_real.index[-1]).split("+")[0].replace("T", " ")
                        if pending["buy"]:
                            row["valid_opp_val"] = _r(pending["buy"]["level"])
                            row["valid_opp_dir"] = "buy"
                    elif most_recent:
                        direction, sig = most_recent
                        row["valid_time"] = sig["valid_time"]
                        row["trigger_lvl"] = _r(sig["level"])
                        row["wt_val"] = sig["wt_val"]
                        if sig["status"] == "triggered":
                            row["wt_action"] = f"{direction.capitalize()} Triggered (No Active Pending)"
                            row["trigger_time"] = sig["trigger_time"]
                        else:
                            row["wt_action"] = f"{direction.capitalize()} Pending (BO {'>' if direction == 'buy' else '<'} {_r(sig['level'])})"
                            row["trigger_time"] = "Pending"
                            
                        opp_dir = "sell" if direction == "buy" else "buy"
                        if pending[opp_dir]:
                            row["valid_opp_val"] = _r(pending[opp_dir]["level"])
                            row["valid_opp_dir"] = opp_dir

                    # Position Management
                    pos_avg_px = live_px
                    has_open_pos = False
                    pos_side = ""
                    pos_qty = 0
                    try:
                        for p in paper_trades.positions():
                            if p["symbol"] == raw_sym and p["qty"] != 0:
                                pos_avg_px = p["avg_price"]
                                pos_qty = p["qty"]
                                has_open_pos = True
                                pos_side = "Buy" if p["qty"] > 0 else "Sell"
                                break
                    except: pass

                    target_pct = float(cfg.get("target_pct", 0))
                    stop_pct = float(cfg.get("stop_pct", 0))
                    
                    existing_sent = ""
                    try:
                        for o in paper_trades.list_orders():
                            if o["symbol"] == raw_sym and o["status"] == "OPEN":
                                existing_sent = f"pa ({o['order_type']})"
                                break
                    except: pass

                    # ✅ NEW: Position Status & Side Mode Logic
                    pos_status = "—"
                    side_mode = cfg.get("side_mode", "both")
                    is_valid_action = True
                    if side_mode == "long" and "Sell" in row.get("wt_action", ""): is_valid_action = False
                    if side_mode == "short" and "Buy" in row.get("wt_action", ""): is_valid_action = False
                    
                    if not is_valid_action:
                        pos_status = "Ignored (Buy Only)" if side_mode == "long" else "Ignored (Sell Only)"
                        row["sent_to"] = ""
                    else:
                        # ✅ NEW: Averaging Logic
                        is_averaging = cfg.get("strategy_mode") == "same_side"
                        max_levels = int(cfg.get("max_levels", 5))
                        filled_levels = 0
                        try:
                            for ord_hist in paper_trades.list_orders():
                                if ord_hist["symbol"] == raw_sym and ord_hist["status"] == "FILLED" and ord_hist["side"] == ("BUY" if pos_side == "Buy" else "SELL"):
                                    filled_levels += 1
                        except: pass

                        allow_average = False
                        if is_averaging and filled_levels < max_levels and has_open_pos:
                            if (pos_side == "Buy" and "Buy" in row.get("wt_action", "")) or (pos_side == "Sell" and "Sell" in row.get("wt_action", "")):
                                allow_average = True

                        if has_open_pos and not allow_average:
                            if is_averaging and filled_levels >= max_levels:
                                pos_status = f"{pos_side} position in progress (Max Levels)"
                            else:
                                pos_status = f"{pos_side} position in progress"
                            row["sent_to"] = ""
                            row["overall_status"] = f"Position Open ({pos_side})" # ✅ FIX: Force Overall Status to update
                            
                            if target_pct > 0:
                                row["target_val"] = _r(pos_avg_px * (1 + target_pct/100)) if pos_side == "Buy" else _r(pos_avg_px * (1 - target_pct/100))
                            if stop_pct > 0:
                                row["sl_val"] = _r(pos_avg_px * (1 - stop_pct/100)) if pos_side == "Buy" else _r(pos_avg_px * (1 + stop_pct/100))
                            elif row.get("valid_opp_val"):
                                row["sl_val"] = row["valid_opp_val"]
                                
                            exit_reason = None
                            if pos_side == "Buy":
                                if target_pct > 0 and live_px >= pos_avg_px * (1 + target_pct/100): exit_reason = "Target"
                                elif stop_pct > 0 and live_px <= pos_avg_px * (1 - stop_pct/100): exit_reason = "Stop Loss"
                                elif cfg.get("exit_opp") and triggered_sell: exit_reason = "Opp Signal"
                            else:
                                if target_pct > 0 and live_px <= pos_avg_px * (1 - target_pct/100): exit_reason = "Target"
                                elif stop_pct > 0 and live_px >= pos_avg_px * (1 + stop_pct/100): exit_reason = "Stop Loss"
                                elif cfg.get("exit_opp") and triggered_buy: exit_reason = "Opp Signal"
                                
                            if exit_reason:
                                side = "SELL" if pos_side == "Buy" else "BUY"
                                sig_id = f"{raw_sym}_{tf}_{side}_{str(df_real.index[-1])}_exit_{exit_reason}" # ✅ Add reason to ID
                                processed = []
                                if PAPER_PROCESSED_FILE.exists():
                                    try: processed = json.loads(PAPER_PROCESSED_FILE.read_text())
                                    except: pass
                                if not any(p.get("id") == sig_id for p in processed):
                                    queue = []
                                    if PAPER_QUEUE_FILE.exists():
                                        try: queue = json.loads(PAPER_QUEUE_FILE.read_text())
                                        except: pass
                                    queue.append({
                                        "id": sig_id, "symbol": raw_sym, "exchange": task["exch"],
                                        "side": side, "qty": abs(pos_qty), "price": live_px, "tf": tf,
                                        "order_type": "MARKET",
                                        "signal_level": live_px, "signal_time": str(df_real.index[-1]),
                                        "ltp": live_px,
                                        "tag": f"Exit ({exit_reason})" # ✅ Add the tag here
                                    })
                                    PAPER_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
                                    row["sent_to"] = "pa (Exit)"
                                    print(f"✅ Exit Triggered for {raw_sym} - {exit_reason}")
                                    
                                    if exit_reason == "Opp Signal":
                                        has_open_pos = False
                                        allow_average = True # Allow immediate flip

                        if not has_open_pos or allow_average:
                            entry_level = live_px
                            if row.get("trigger_lvl") is not None:
                                entry_level = float(row["trigger_lvl"])

                            if target_pct > 0:
                                if "Buy" in row.get("wt_action", ""):
                                    row["target_val"] = _r(entry_level * (1 + target_pct/100))
                                elif "Sell" in row.get("wt_action", ""):
                                    row["target_val"] = _r(entry_level * (1 - target_pct/100))
                                    
                            if stop_pct > 0:
                                if "Buy" in row.get("wt_action", ""):
                                    row["sl_val"] = _r(entry_level * (1 - stop_pct/100))
                                elif "Sell" in row.get("wt_action", ""):
                                    row["sl_val"] = _r(entry_level * (1 + stop_pct/100))
                            elif row.get("valid_opp_val"):
                                row["sl_val"] = row["valid_opp_val"]

                            is_triggered_history = most_recent and most_recent[1]["status"] == "triggered"
                            
                            # ✅ FIX: Detect bad data gaps (e.g. stock splits)
                            is_bad_data = False
                            if row.get("trigger_lvl") and live_px:
                                tl = float(row["trigger_lvl"])
                                if tl > 0 and (live_px > tl * 1.2 or live_px < tl * 0.8):
                                    is_bad_data = True
                                    pos_status = "Ignored (Data Gap)"
                                    row["sent_to"] = ""
                            
                            # ✅ FIX: Determine the direction of the new signal
                            new_side = None
                            if triggered_buy or pending["buy"]: new_side = "BUY"
                            elif triggered_sell or pending["sell"]: new_side = "SELL"

                            # ✅ FIX: Find the actual open order in the ledger
                            open_order = next((o for o in paper_trades.list_orders() if o["symbol"] == raw_sym and o["status"] == "OPEN"), None)

                            # ✅ FIX: Prioritize the open order ONLY if it matches the new signal direction
                            if is_bad_data:
                                pass # Do nothing, skip order
                            elif open_order and open_order["side"] == new_side:
                                row["sent_to"] = f"pa ({open_order['order_type']})"
                                row["overall_status"] = "Order Placed (Waiting for Breakout)"
                                pos_status = f"{new_side.capitalize()} limit order pending"
                            elif is_triggered_history and not allow_average:
                                row["sent_to"] = ""
                                row["overall_status"] = "Triggered (No Active Pending)"
                                pos_status = "Triggered (No Active Pending)"
                            else:
                                row["overall_status"] = "Signal Pending"
                                row["sent_to"] = ""
                                    
                                if cfg.get("paper_trade"):
                                    side = None
                                    order_type = "LIMIT"
                                    limit_px = None
                                    sig_time = ""
                                    
                                    if triggered_buy:
                                        side = "BUY"
                                        sig_time = str(df_real.index[-1]).split("+")[0].replace("T", " ")
                                        limit_px = live_px
                                        order_type = "MARKET" # ✅ FIX: Live breakouts must be Market orders at LTP
                                    elif triggered_sell:
                                        side = "SELL"
                                        sig_time = str(df_real.index[-1]).split("+")[0].replace("T", " ")
                                        limit_px = live_px
                                        order_type = "MARKET" # ✅ FIX: Live breakouts must be Market orders at LTP
                                    elif pending["buy"]:
                                        side = "BUY"
                                        sig_time = str(df_closed.index[pending["buy"]["valid_i"]]).split("+")[0].replace("T", " ")
                                        limit_px = _r(pending["buy"]["level"] + buffer_pts)
                                    elif pending["sell"]:
                                        side = "SELL"
                                        sig_time = str(df_closed.index[pending["sell"]["valid_i"]]).split("+")[0].replace("T", " ")
                                        limit_px = _r(pending["sell"]["level"] - buffer_pts)

                                    if side:
                                        # ✅ FIX: Limit orders are ALWAYS allowed. Only check checkbox for Market orders.
                                        is_allowed = True
                                        if order_type == "MARKET":
                                            if not cfg.get("allow_market_orders", False):
                                                current_sig_lvl = row.get("trigger_lvl")
                                                last_ignored = AUTO_STATE.get(key, {}).get("last_ignored_sig")
                                                
                                                # Ignore if it's the first scan, OR if the signal level hasn't changed (e.g., still 500)
                                                if AUTO_JOBS[job_id].get("is_first_scan", True) or str(last_ignored) == str(current_sig_lvl):
                                                    pos_status = "Ignored (Market Off)"
                                                    is_allowed = False
                                                    
                                                    # Save the signal we just ignored so we remember it next time
                                                    if key not in AUTO_STATE: AUTO_STATE[key] = {}
                                                    AUTO_STATE[key]["last_ignored_sig"] = current_sig_lvl
                                                else:
                                                    # Signal changed (e.g., from 500 to 530), so we bypass
                                                    pos_status = f"{'Buy' if side == 'BUY' else 'Sell'} position in progress"
                                                    is_allowed = True

                                        try:
                                            for o in paper_trades.list_orders():
                                                if o["symbol"] == raw_sym and o["status"] == "OPEN" and o["side"] != side:
                                                    paper_trades.cancel_order(o["id"])
                                        except Exception:
                                            pass

                                        sig_id = f"{raw_sym}_{tf}_{side}_{sig_time}"
                                        processed = []
                                        if PAPER_PROCESSED_FILE.exists():
                                            try: processed = json.loads(PAPER_PROCESSED_FILE.read_text())
                                            except: pass
                                        
                                        already_processed = any(p.get("id") == sig_id for p in processed)
                                        
                                        if not already_processed and is_allowed:
                                            queue = []
                                            if PAPER_QUEUE_FILE.exists():
                                                try: 
                                                    queue = json.loads(PAPER_QUEUE_FILE.read_text())
                                                    # ✅ FIX: Keep the exit order we just queued, delete other pending orders for this symbol
                                                    queue = [q for q in queue if q.get("symbol") != raw_sym or "_exit" in str(q.get("id", ""))]
                                                except: pass
                                            
                                            existing_q = next((q for q in queue if q.get("id") == sig_id), None)
                                            already_open = any(o["symbol"] == raw_sym and o["status"] == "OPEN" for o in paper_trades.list_orders())
                                            
                                            if existing_q:
                                                row["sent_to"] = f"pa ({existing_q['order_type']})"
                                                pos_status = f"{'Buy' if side == 'BUY' else 'Sell'} limit order pending"
                                            elif already_open and existing_sent:
                                                row["sent_to"] = existing_sent
                                                pos_status = f"{'Buy' if side == 'BUY' else 'Sell'} limit order pending"
                                            else:
                                                qty = max(1, int(float(task.get("amt", 5000)) / limit_px))
                                                queue.append({
                                                    "id": sig_id, "symbol": raw_sym, "exchange": task["exch"],
                                                    "side": side, "qty": qty, "price": limit_px, "tf": tf,
                                                    "order_type": order_type,
                                                    "signal_level": limit_px, "signal_time": sig_time,
                                                    "ltp": live_px
                                                })
                                                PAPER_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
                                                
                                                sent = row.get("sent_to", "")
                                                if sent:
                                                    row["sent_to"] = f"{sent} & pa (Entry)"
                                                else:
                                                    row["sent_to"] = f"pa ({order_type})"
                                                
                                                if order_type == "LIMIT":
                                                    pos_status = f"{'Buy' if side == 'BUY' else 'Sell'} limit order pending"
                                                else:
                                                    pos_status = f"{'Buy' if side == 'BUY' else 'Sell'} position in progress"
                                                
                                                if allow_average:
                                                    pos_status += f" (Level {filled_levels + 1})"
                                                
                                            print(f"✅ Queued {order_type} for {raw_sym} - {side} @ {limit_px}")
                                            
                                            # ✅ NEW: Process the queue immediately so it shows up in the UI instantly
                                            _process_paper_queue()

                    row["pos_status"] = pos_status
                    AUTO_STATE[key] = row

                except Exception as e:
                    print(f"⚠️ [AUTO] Error on {raw_sym}_{tf}: {e}")
                    AUTO_STATE[key] = {"symbol": raw_sym, "status": "Error"}

            if cfg.get("write_to_sheet", False):
                try:
                    headers = ["Symbol", "TF", "LTP", "WT Trend", "Valid Candle", "Trigger Lvl", "WT Val", "WT Action", "Sent To", "Target", "Stop Loss", "Trigger Time"]
                    rows_out = []
                    for r in AUTO_STATE.values():
                        if r.get("status") in ("No Data", "Error"): continue
                        rows_out.append([
                            r.get("symbol"), r.get("tf"), r.get("ltp"), r.get("wt_trend"),
                            r.get("valid_time"), r.get("trigger_lvl"), r.get("wt_val"),
                            r.get("wt_action"), r.get("sent_to"), r.get("target_val"), r.get("sl_val"), r.get("trigger_time")
                        ])
                    
                    sheets.write_results("paper trading", "test_output", headers, rows_out)
                    print("✅ Written to paper trading / test_output")
                except Exception as e:
                    print(f"❌ Failed to write to GSheet: {e}")

            _process_paper_queue()
            _check_pending_limits(downloaded_data)

            # ✅ SET IS_FIRST_SCAN TO FALSE AFTER FIRST FULL LOOP
            AUTO_JOBS[job_id]["is_first_scan"] = False
            
            # ✅ SAVE STATE TO DISK SO UI CAN RESUME INSTANTLY ON SERVER RESTART
            try: AUTO_STATE_FILE.write_text(json.dumps(AUTO_STATE, indent=2))
            except: pass

        except Exception as e:
            print(f"⚠️ [AUTO] Loop Crash: {e}")
        finally:
            IS_SCANNING = False
            LAST_SCAN_TIME = time.time()
            
        if FORCE_SCAN:
            FORCE_SCAN = False
            continue
            
        for _ in range(cfg.get("scan_interval", 1) * 60):
            if AUTO_JOBS[job_id]["stop"] or FORCE_SCAN: break
            time.sleep(1)


           