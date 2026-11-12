import threading
import time
import json
from pathlib import Path
import pandas as pd
import pytz
import yfinance as yf
import re
import paper_trades

from data import download_batch, get_live_ltp, _SYMBOL_MAP
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
from indicators import get as get_indicator
import sheets

IST = pytz.timezone("Asia/Kolkata")
SETUP_JOBS = {}
SETUP_STATE = {}
SETUP_IS_SCANNING = False
SETUP_FORCE_SCAN = False
SETUP_LAST_SCAN_TIME = 0
TRIGGER_STATE = {}

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SETUP_ALLOC_FILE = DATA_DIR / "setup_allocations.json"
SETUP_STATE_FILE = DATA_DIR / "setup_state_cache.json"
SETUP_PROCESSED_FILE = DATA_DIR / "setup_paper_processed.json"
SETUP_QUEUE_FILE = DATA_DIR / "order_queue.json"
SETUP_HISTORY_FILE = DATA_DIR / "setup_trade_history.json" # ✅ NEW: Trade History

# ✅ NEW: Live Queue and Processed files
LIVE_QUEUE_FILE = DATA_DIR / "live_order_queue.json"
LIVE_PROCESSED_FILE = DATA_DIR / "live_processed.json"

# ✅ STEP 2: Setup Trigger Persistence
SETUP_TRIGGER_FILE = DATA_DIR / "setup_trigger_state.json"
if SETUP_TRIGGER_FILE.exists():
    try:
        TRIGGER_STATE.update(json.loads(SETUP_TRIGGER_FILE.read_text()))
    except Exception as e:
        print(f"⚠️ Failed to load setup_trigger_state.json: {e}")

def start_job(job_id, config):
    SETUP_STATE.clear()
    # TRIGGER_STATE.clear()  <-- REMOVED to keep trigger memory across restarts
    if SETUP_STATE_FILE.exists():
        try: SETUP_STATE.update(json.loads(SETUP_STATE_FILE.read_text()))
        except: pass
        
    SETUP_JOBS[job_id] = {"stop": False, "config": config, "is_first_scan": True}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def save_history_entry(entry):
    history = []
    if SETUP_HISTORY_FILE.exists():
        try: history = json.loads(SETUP_HISTORY_FILE.read_text())
        except: pass
    history.append(entry)
    if len(history) > 200: history = history[-200:]
    SETUP_HISTORY_FILE.write_text(json.dumps(history, indent=2))

# ✅ FIX: Added missing stop_job function
def stop_job(job_id):
    if job_id in SETUP_JOBS:
        SETUP_JOBS[job_id]["stop"] = True

def sync_state_to_ledger():
    """Pushes the latest Target/SL from SETUP_STATE into paper_trades.json"""
    import paper_trades
    import json
    
    try:
        ledger_file = paper_trades.DATA_DIR / "paper_trades.json"
        if not ledger_file.exists(): return
        data = json.loads(ledger_file.read_text())
        
        updated_count = 0
        # 1. Sync Open Orders
        for o in data.get("orders", []):
            if o.get("status") == "OPEN":
                for state in SETUP_STATE.values():
                    if state.get("symbol") == o["symbol"]:
                        if state.get("target_val") and state["target_val"] != "—":
                            o["target"] = state["target_val"]
                        if state.get("sl_val") and state["sl_val"] != "—":
                            o["sl"] = state["sl_val"]
                        updated_count += 1
                        break
                        
        # 2. Sync Open Positions
        for p in data.get("positions", []):
            for state in SETUP_STATE.values():
                if state.get("symbol") == p["symbol"]:
                    if state.get("target_val") and state["target_val"] != "—":
                        p["target"] = state["target_val"]
                    if state.get("sl_val") and state["sl_val"] != "—":
                        p["sl"] = state["sl_val"]
                    updated_count += 1
                    break
                    
        if updated_count > 0:
            ledger_file.write_text(json.dumps(data, indent=2))
            print(f"🔄 Synced {updated_count} orders/positions with new Target/SL")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

def _process_queue_file(queue_file, processed_file, mode="PAPER"):
    if not queue_file.exists(): return
    try: queue = json.loads(queue_file.read_text())
    except: return
    if not queue: return
    
    processed = []
    if processed_file.exists():
        try: processed = json.loads(processed_file.read_text())
        except: pass
        
    for q in queue:
        try:
            action = q.get("action", "NEW_ORDER")
            
            if action == "UPDATE_RISK":
                # ✅ Queue Action: Update Target/SL for existing orders/positions
                if mode == "PAPER":
                    ledger_file = paper_trades.DATA_DIR / "paper_trades.json"
                    if ledger_file.exists():
                        data = json.loads(ledger_file.read_text())
                        for o in data.get("orders", []):
                            if o.get("symbol") == q["symbol"] and o.get("status") == "OPEN":
                                if q.get("target_val"): o["target"] = q["target_val"]
                                if q.get("sl_val"): o["sl"] = q["sl_val"]
                        for p in data.get("positions", []):
                            if p.get("symbol") == q["symbol"]:
                                if q.get("target_val"): p["target"] = q["target_val"]
                                if q.get("sl_val"): p["sl"] = q["sl_val"]
                        ledger_file.write_text(json.dumps(data, indent=2))
                        print(f"✅ [QUEUE] Updated Target/SL for {q['symbol']} via Paper Queue")
                else:
                    print(f"🚀 [LIVE] Would have updated Target/SL for {q['symbol']} on Zerodha")
                
                # Skip the normal order placement logic
                continue

            # --- Standard NEW_ORDER Logic ---
            o_type = q.get("order_type", "MARKET")
            limit_px = q.get("price") or q.get("signal_level") or q.get("ltp") or 0
            
            if mode == "PAPER":
                o = paper_trades.place_order(
                    symbol=q["symbol"], exchange=q["exchange"], side=q["side"], 
                    qty=q["qty"], order_type=o_type, price=limit_px if o_type=="LIMIT" else None, product="MIS", ltp=q.get("ltp") or limit_px,
                    tag=q.get("tag")
                )
                if o_type == "MARKET":
                    paper_trades.mark_filled(o["id"], q.get("ltp") or limit_px)
            else:
                # ✅ FUTURE: Live Zerodha execution logic will go here
                print(f"🚀 [LIVE] Would have placed {o_type} for {q['symbol']} - {q['side']} @ {limit_px}")
            
            processed.append({
                "id": q["id"], "symbol": q["symbol"], "tf": q["tf"], "side": q["side"],
                "signal_level": q.get("signal_level"), "signal_time": q.get("signal_time"),
                "target_val": q.get("target_val"), "sl_val": q.get("sl_val"), "opp_sig": q.get("opp_sig"),
                "executed_time": str(pd.Timestamp.now(tz=IST))
            })
        except Exception as e:
            print(f"Queue execution failed for {q.get('symbol')}: {e}")
            
    processed_file.write_text(json.dumps(processed, indent=2))
    queue_file.write_text("[]")
    print(f"✅ Executed {mode} Queue successfully.")

def execute_queue():
    _process_queue_file(SETUP_QUEUE_FILE, SETUP_PROCESSED_FILE, mode="PAPER")
    _process_queue_file(LIVE_QUEUE_FILE, LIVE_PROCESSED_FILE, mode="LIVE")

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
                    if o["side"] == "BUY" and live_px > limit_px:
                        paper_trades.mark_filled(o["id"], limit_px)
                    elif o["side"] == "SELL" and live_px < limit_px:
                        paper_trades.mark_filled(o["id"], limit_px)

def get_active_pending_signals(df, params, buffer_pts, max_wait, pending_rule):
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
            
            hit = ((direction == "buy"  and h > p["level"] + buffer_pts) or
                   (direction == "sell" and l < p["level"] - buffer_pts))
            if hit:
                if not latest_signal[direction] or p["valid_i"] >= latest_signal[direction]["valid_i"]:
                    latest_signal[direction] = {
                        "status": "triggered",
                        "level": p["level"],
                        "wt_val": p["wt_val"],
                        "valid_i": p["valid_i"],
                        "valid_time": str(df.index[p["valid_i"]]).split("+")[0].replace("T", " "),
                        "trigger_time": str(df.index[i]).split("+")[0].replace("T", " ")
                    }
                pend[direction] = None
                
            elif i - p["valid_i"] >= max_wait:
                pend[direction] = None
        
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

def _worker(job_id):
    print(f"🚀 [SETUP] Started Job {job_id}")
    
    while not SETUP_JOBS[job_id]["stop"]:
        try:
            global SETUP_IS_SCANNING, SETUP_FORCE_SCAN, SETUP_LAST_SCAN_TIME
            
            cfg = SETUP_JOBS[job_id]["config"]
            
            # ✅ FIX: Allow the FIRST scan to run even if paused.
            is_first = SETUP_JOBS[job_id].get("is_first_scan", False)
            if cfg.get("paused", False) and not SETUP_FORCE_SCAN and not is_first:
                time.sleep(1)
                continue
                
            SETUP_IS_SCANNING = True
            tasks = cfg.get("tasks", [])
            
            sym_map = {}
            for t in tasks:
                raw_sym = t["sym"]
                exch = t.get("exch", "NSE")
                sym_yf = raw_sym.upper().replace(" ", "")
                if exch == "NSE" and not sym_yf.endswith(".NS") and "." not in sym_yf and "=" not in sym_yf and not sym_yf.startswith("^"):
                    sym_yf = sym_yf + ".NS"
                elif exch == "BSE" and not sym_yf.endswith(".BO") and "." not in sym_yf and "=" not in sym_yf and not sym_yf.startswith("^"):
                    sym_yf = sym_yf + ".BO"
                else:
                    sym_yf = _SYMBOL_MAP.get(sym_yf, sym_yf)
                sym_map[f"{raw_sym}_{t['tf']}"] = sym_yf

            current_keys = {f"{t['sym']}_{t['tf']}" for t in tasks}
            for k in list(SETUP_STATE.keys()):
                if k not in current_keys:
                    del SETUP_STATE[k]
            
            current_yf_syms = {sym_map[k] for k in current_keys if k in sym_map}
            for k in list(TRIGGER_STATE.keys()):
                sym_yf = k.split('|')[0]
                if sym_yf not in current_yf_syms:
                    del TRIGGER_STATE[k]
            
            tf_groups = {}
            for t in tasks:
                tf = t["tf"]
                key = f"{t['sym']}_{tf}"
                tf_groups.setdefault(tf, []).append(sym_map[key])

            fno_mode = cfg.get("fno_mode", "none")
            fno_list = None
            has_indian_stocks = any(t.get("exch", "").upper() in ["NSE", "BSE"] or str(t.get("sym", "")).upper().endswith((".NS", ".BO")) for t in tasks)
            
            if fno_mode == "sheet" and has_indian_stocks:
                try:
                    raw_fno = sheets.read_symbols(cfg.get("fno_book", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input"))
                    fno_list = {str(s).replace(".NS", "").replace(".BO", "").upper() for s in raw_fno}
                except Exception:
                    pass

            use_ha = cfg.get("candles", "ha") == "ha"
            downloaded_data = {}
            
            for tf, syms in tf_groups.items():
                if tf in ["1m", "2m", "5m", "15m", "30m"]: period = "60d"
                elif tf in ["60m", "90m", "1h"]: period = "3mo"
                else: period = "1y"
                
                try:
                    # ✅ OPTIMIZATION: Disabled force_refresh for speed. We override with live LTP anyway.
                    res = download_batch(syms, interval=tf, period=period, force_refresh=False)
                    for s_yf, rows in res.items():
                        downloaded_data[f"{tf}_{s_yf}"] = rows
                except Exception as e:
                    print(f"Download failed for {tf}: {e}")
                    continue

                true_ltps = {}
                try:
                    true_ltps = get_live_ltp(list(set(syms)))
                except Exception:
                    pass
                
                # ✅ OPTIMIZATION: Batch fetch all Forex symbols in ONE network call
                forex_syms = [s for s in syms if "=X" in s]
                if forex_syms:
                    try:
                        import pandas as pd
                        hist = yf.download(forex_syms, period="1d", interval="1m", progress=False, auto_adjust=False, threads=True)
                        if not hist.empty:
                            if len(forex_syms) == 1:
                                if "Close" in hist.columns:
                                    true_ltps[forex_syms[0]] = float(hist["Close"].dropna().iloc[-1])
                            else:
                                if isinstance(hist.columns, pd.MultiIndex):
                                    close_df = hist["Close"]
                                    for sym in forex_syms:
                                        if sym in close_df.columns:
                                            valid = close_df[sym].dropna()
                                            if not valid.empty:
                                                true_ltps[sym] = float(valid.iloc[-1])
                    except Exception as e:
                        print(f"Forex batch fetch failed: {e}")

                for task in tasks:
                    if task["tf"] != tf: continue
                    
                    # ✅ FIX: Instantly kill the worker loop if Stop is clicked
                    if job_id not in SETUP_JOBS or SETUP_JOBS[job_id].get("stop", False):
                        print(f"🛑 [SETUP] Stop requested. Halting scan immediately...")
                        return
                    
                    raw_sym = task["sym"]
                    key = f"{raw_sym}_{tf}"
                    sym_yf = sym_map[key]
                    sym_clean = sym_yf.replace(".NS", "").replace(".BO", "").upper()
                    
                    try:
                        rows = res.get(sym_yf, [])
                        if not rows or len(rows) < 40: 
                            SETUP_STATE[key] = {"symbol": raw_sym, "status": "No Data"}
                            continue
                        
                        true_ltp = true_ltps.get(sym_yf)
                        
                        df_real = candles_to_df(rows)
                        if df_real.index.tz is None: df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
                        else: df_real.index = df_real.index.tz_convert(IST)
                        
                        if fno_mode == "all" or (fno_mode == "sheet" and fno_list and sym_clean in fno_list):
                            df_real = apply_fno_filter(df_real, timeframe=tf)
                        
                        if len(df_real) < 30: continue
                        
                        if true_ltp is not None:
                            live_px = true_ltp
                            df_real.iloc[-1, df_real.columns.get_loc("Close")] = live_px
                            df_real.iloc[-1, df_real.columns.get_loc("High")] = max(live_px, df_real["High"].iloc[-1])
                            df_real.iloc[-1, df_real.columns.get_loc("Low")] = min(live_px, df_real["Low"].iloc[-1])
                        else:
                            live_px = float(df_real["Close"].iloc[-1])
                            
                        df_ha = heikin_ashi(df_real) if use_ha else df_real
                        
                        def _r(val): return round(val, 5) if abs(val) < 10 else round(val, 2)
                        
                        # ✅ Restore previous position tracking data
                        prev_row = SETUP_STATE.get(key, {})
                        row = {"symbol": raw_sym, "tf": tf, "ltp": _r(live_px), "yf_symbol": sym_yf}
                        row["alloted_amt"] = task.get("amt", 0)
                        row["pos_qty"] = prev_row.get("pos_qty", 0)
                        row["pos_entry"] = prev_row.get("pos_entry", 0)
                        row["pos_side"] = prev_row.get("pos_side", "")
                        row["order_pnl"] = prev_row.get("order_pnl", 0)
                        row["total_pnl"] = prev_row.get("total_pnl", 0)
                        row["order_col"] = prev_row.get("order_col", "—")
                        row["position_col"] = prev_row.get("position_col", "—")
                        row["order_trig_price"] = prev_row.get("order_trig_price", "—")
                        row["pos_in_progress"] = prev_row.get("pos_in_progress", "NO")
                        row["order_queue"] = prev_row.get("order_queue", "—") # ✅ NEW
                        row["queue_trig_price"] = prev_row.get("queue_trig_price", 0) # ✅ NEW
                        wt_params = {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)}
                        
                        wt_mod = get_indicator("wt")
                        wt_comp = wt_mod.compute(df_ha, wt_params)
                        wt_params["live_ltp"] = live_px
                        
                        mem_key = f"{sym_yf}|wt"
                        last_state = TRIGGER_STATE.get(mem_key)
                        
                        wt_sig = wt_mod.signals(df_ha, wt_params, wt_comp, state=last_state)
                        
                        new_state = wt_sig.pop("_new_state", None)
                        if new_state is not None:
                            TRIGGER_STATE[mem_key] = new_state
                            
                        for k, v in wt_sig.items():
                            if not k.endswith("_details") and k != "_new_state":
                                row[k] = v
                                
                        buffer_pts = float(cfg.get("buffer_pts", 0))
                        wait_bars = int(cfg.get("wait_bars", 100))
                        pending_rule = cfg.get("wt_pending_rule", "same_dir")
                        df_closed = df_ha.iloc[:-1] if len(df_ha) > 1 else df_ha
                        
                        pending, latest_signal = get_active_pending_signals(df_closed, wt_params, buffer_pts, wait_bars, pending_rule)
                        
                        most_recent = None
                        if latest_signal["buy"] and latest_signal["sell"]:
                            most_recent = ("buy", latest_signal["buy"]) if latest_signal["buy"]["valid_i"] > latest_signal["sell"]["valid_i"] else ("sell", latest_signal["sell"])
                        elif latest_signal["buy"]: most_recent = ("buy", latest_signal["buy"])
                        elif latest_signal["sell"]: most_recent = ("sell", latest_signal["sell"])

                        if most_recent:
                            direction, sig = most_recent
                            tl = float(sig["level"])
                            gap_pct = abs(live_px - tl) / tl * 100 if tl > 0 else 0
                            
                            # ✅ 20% GAP FILTER: Prevent data glitches / stock split errors
                            if gap_pct > 20:
                                row["valid_time"] = "—"
                                row["trigger_lvl"] = "—"
                                row["wt_action"] = "Data Glitch (Gap > 20%)"
                                row["opp_sig"] = "—"
                            else:
                                row["valid_time"] = sig.get("valid_time")
                                row["trigger_lvl"] = f"{tl:.5f}" if abs(tl) < 10 else f"{tl:.2f}"
                                
                                if "wt_action" in row and row["wt_action"]:
                                    try:
                                        row["wt_action"] = re.sub(r'\d+\.\d+', row["trigger_lvl"], row["wt_action"])
                                    except Exception:
                                        pass
                            
                            # ✅ FIX: If a position is open, the opposite signal is the opposite of the position.
                            if row.get("pos_qty", 0) != 0:
                                opp_dir = "sell" if row["pos_side"] == "BUY" else "buy"
                            else:
                                opp_dir = "sell" if direction == "buy" else "buy"
                                
                            # ✅ NEW: Check if there is a pending active order in the opposite direction
                            has_pending_order = False
                            if row.get("order_col", "—") != "—" and row.get("position_col") == "Pending":
                                order_side = "BUY" if "BUY" in row["order_col"] else "SELL"
                                sig_side = "BUY" if direction == "buy" else "SELL"
                                
                                # If the pending order is opposite to the new signal
                                if order_side != sig_side:
                                    row["opp_sig"] = row.get("order_trig_price", "—")
                                    has_pending_order = True

                            # If no pending opposite order, use the WT indicator's pending signal
                            if not has_pending_order:
                                if pending[opp_dir]:
                                    opp_lvl = _r(pending[opp_dir]["level"])
                                    row["opp_sig"] = f"{opp_lvl:.5f}" if abs(opp_lvl) < 10 else f"{opp_lvl:.2f}"
                                else:
                                    row["opp_sig"] = "—"
                        else:
                            row["trigger_lvl"] = "—"
                            row["opp_sig"] = "—"

                        action_str = str(row.get("wt_action", ""))
                        row["valid"] = "—"
                        max_gap_pct = float(cfg.get("max_gap_pct", 2.0))
                        
                        # ✅ FIX: Calculate gap and validity robustly
                        gap_pct = 0
                        is_valid = False
                        
                        if "Limit" in action_str:
                            is_valid = True
                            tl = float(row["trigger_lvl"]) if row.get("trigger_lvl") and row["trigger_lvl"] != "—" else 0
                            if tl > 0:
                                gap_pct = abs(live_px - tl) / tl * 100
                        elif "Market" in action_str and row.get("trigger_lvl") and row["trigger_lvl"] != "—":
                            try:
                                tl = float(row["trigger_lvl"])
                                if tl > 0:
                                    gap_pct = abs(live_px - tl) / tl * 100
                                    is_valid = (gap_pct <= max_gap_pct)
                            except Exception:
                                pass
                        else:
                            is_valid = True # Fallback for things like indicators without levels
                            
                        if is_valid:
                            row["valid"] = f"YES (Gap {gap_pct:.1f}%)"
                        else:
                            row["valid"] = f"NO (Gap {gap_pct:.1f}%)"
                                
                        if is_valid and row.get("trigger_lvl") and row["trigger_lvl"] != "—":
                            # ✅ Fix: Use pos_entry if position is open, else trigger_lvl
                            if row.get("pos_qty", 0) != 0:
                                entry_level = float(row["pos_entry"])
                                is_buy_dir = (row["pos_side"] == "BUY")
                            else:
                                entry_level = float(row["trigger_lvl"])
                                is_buy_dir = "Buy" in action_str
                                
                            current_target_pct = float(cfg.get("target_pct", 0))
                            current_stop_pct = float(cfg.get("stop_pct", 0))
                            
                            if current_target_pct > 0:
                                t_val = _r(entry_level * (1 + current_target_pct/100)) if is_buy_dir else _r(entry_level * (1 - current_target_pct/100))
                                row["target_val"] = f"{t_val:.5f}" if abs(t_val) < 10 else f"{t_val:.2f}"
                            else:
                                row["target_val"] = "—"
                                
                            if current_stop_pct > 0:
                                s_val = _r(entry_level * (1 - current_stop_pct/100)) if is_buy_dir else _r(entry_level * (1 + current_stop_pct/100))
                                row["sl_val"] = f"{s_val:.5f}" if abs(s_val) < 10 else f"{s_val:.2f}"
                            else:
                                row["sl_val"] = row["opp_sig"]
                        else:
                            row["target_val"] = "—"
                            row["sl_val"] = "—"

                        # ✅ FIX: Use the robust is_valid calculated above, and strip whitespace from trigger status
                        is_new_trigger = str(row.get("wt_trigger_status", "")).strip() == "New Trigger"
                        is_trade_action = "Buy" in action_str or "Sell" in action_str

                        # ✅ NEW: QUEUE-BASED STATE MACHINE
                        if "order_queue" not in row:
                            row["order_queue"] = "—"
                            
                        # 1. Update Order Queue on New Signal
                        if is_new_trigger and is_valid and is_trade_action:
                            side = "BUY" if "Buy" in action_str else "SELL"
                            order_type = "MARKET" if "Market" in action_str else "LIMIT"
                            row["order_queue"] = f"{side} {order_type}"
                            row["queue_trig_price"] = _r(live_px) if order_type == "MARKET" else float(row["trigger_lvl"])

                        # ✅ 2. THE PROMOTION RULE: If flat and no active order, promote from queue
                        if row["pos_qty"] == 0 and row["order_col"] == "—" and row.get("order_queue", "—") != "—":
                            row["order_col"] = row["order_queue"]
                            row["order_trig_price"] = row.get("queue_trig_price", float(row["trigger_lvl"]))
                            row["order_queue"] = "—"

                        # 2. Manage Active Order (if no position is open yet)
                        if row["pos_qty"] == 0 and row["order_col"] != "—":
                            row["position_col"] = "Pending"
                            side = "BUY" if "BUY" in row["order_col"] else "SELL"
                            order_type = "MARKET" if "MARKET" in row["order_col"] else "LIMIT"
                            
                            row["pos_in_progress"] = "YES" if order_type == "MARKET" else "NO"
                            
                            filled = False
                            if order_type == "MARKET": filled = True
                            elif order_type == "LIMIT":
                                # Buy Limit triggers when price breaks ABOVE the level
                                # Sell Limit triggers when price breaks BELOW the level
                                if side == "BUY" and live_px >= float(row["order_trig_price"]): filled = True
                                elif side == "SELL" and live_px <= float(row["order_trig_price"]): filled = True
                                    
                            if filled:
                                row["pos_qty"] = max(1, int(float(task.get("amt", 5000)) / float(row["order_trig_price"])))
                                row["pos_entry"] = float(row["order_trig_price"])
                                row["pos_entry_time"] = str(pd.Timestamp.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S'))
                                row["pos_side"] = side
                                row["pos_in_progress"] = "YES"
                                row["position_col"] = f"{'LONG' if side == 'BUY' else 'SHORT'} {row['pos_qty']}"
                                row["order_pnl"] = 0

                                # ✅ FIX: Recalculate Target/SL based on the actual fill price (pos_entry)
                                current_target_pct = float(cfg.get("target_pct", 0))
                                current_stop_pct = float(cfg.get("stop_pct", 0))
                                is_buy_dir = (row["pos_side"] == "BUY")
                                
                                if current_target_pct > 0:
                                    t_val = _r(row["pos_entry"] * (1 + current_target_pct/100)) if is_buy_dir else _r(row["pos_entry"] * (1 - current_target_pct/100))
                                    row["target_val"] = f"{t_val:.5f}" if abs(t_val) < 10 else f"{t_val:.2f}"
                                else:
                                    row["target_val"] = "—"
                                    
                                if current_stop_pct > 0:
                                    s_val = _r(row["pos_entry"] * (1 - current_stop_pct/100)) if is_buy_dir else _r(row["pos_entry"] * (1 + current_stop_pct/100))
                                    row["sl_val"] = f"{s_val:.5f}" if abs(s_val) < 10 else f"{s_val:.2f}"
                                else:
                                    row["sl_val"] = row["opp_sig"]

                        # 3. Manage Open Position
                        if row["pos_qty"] != 0:
                            # Calculate Live P&L
                            if row["pos_side"] == "BUY":
                                row["order_pnl"] = round((live_px - row["pos_entry"]) * row["pos_qty"], 2)
                            else:
                                row["order_pnl"] = round((row["pos_entry"] - live_px) * row["pos_qty"], 2)
                            
                            # Check for Exit (Strictly price-based now)
                            exit_reason = None
                            t_val = float(row["target_val"]) if row.get("target_val") != "—" else (9999999 if row["pos_side"] == "BUY" else 0)
                            s_val = float(row["sl_val"]) if row.get("sl_val") != "—" else (0 if row["pos_side"] == "BUY" else 9999999)
                            opp_val = float(row["opp_sig"]) if row.get("opp_sig") != "—" else (0 if row["pos_side"] == "BUY" else 9999999)
                            
                            if row["pos_side"] == "BUY":
                                if live_px >= t_val: exit_reason = "Target"
                                elif live_px <= opp_val: exit_reason = "Opposite Signal"
                                elif live_px <= s_val: exit_reason = "StopLoss"
                            else:
                                if live_px <= t_val: exit_reason = "Target"
                                elif live_px >= opp_val: exit_reason = "Opposite Signal"
                                elif live_px >= s_val: exit_reason = "StopLoss"

                            if exit_reason:
                                row["total_pnl"] = round(row.get("total_pnl", 0) + row["order_pnl"], 2)
                                save_history_entry({
                                    "symbol": raw_sym, "tf": tf, "side": row["pos_side"],
                                    "qty": row["pos_qty"], "entry": row["pos_entry"], "entry_time": row.get("pos_entry_time", "—"),
                                    "exit": live_px, "pnl": row["order_pnl"],
                                    "reason": exit_reason, "time": str(pd.Timestamp.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S'))
                                })
                                row["pos_qty"] = 0
                                row["pos_entry"] = 0
                                row["pos_side"] = ""
                                row["order_pnl"] = 0
                                row["position_col"] = "—"
                                row["order_col"] = "—" # ✅ Clears active order
                                row["order_trig_price"] = "—"
                                row["pos_in_progress"] = "NO"
                                
                                # ✅ FIX: Clear old risk values so they don't trigger an instant exit on the new trade
                                row["target_val"] = "—"
                                row["sl_val"] = "—"
                                row["opp_sig"] = "—"

                                # ✅ Instantly promote queue item if one was waiting
                                if row.get("order_queue", "—") != "—":
                                    row["order_col"] = row["order_queue"]
                                    row["order_trig_price"] = row.get("queue_trig_price", float(row["trigger_lvl"]))
                                    row["order_queue"] = "—"

                        if is_new_trigger and is_valid and is_trade_action:
                            side = "BUY" if "Buy" in action_str else "SELL"
                            order_type = "MARKET" if "Market" in action_str else "LIMIT"
                            limit_px = live_px if order_type == "MARKET" else float(row["trigger_lvl"])
                            
                            sig_time = row.get("valid_time")
                            if not sig_time:
                                print(f"⚠️ Skipping queue for {raw_sym}: missing valid_time")
                                continue # ✅ STEP 2: Skip if missing
                            sig_id = f"{raw_sym}_{tf}_{side}_{sig_time}"
                            
                            processed = []
                            if SETUP_PROCESSED_FILE.exists():
                                try: processed = json.loads(SETUP_PROCESSED_FILE.read_text())
                                except: pass
                                
                            already_processed = any(p.get("id") == sig_id for p in processed)
                            
                            if not already_processed:
                                queue = []
                                if SETUP_QUEUE_FILE.exists():
                                    try: queue = json.loads(SETUP_QUEUE_FILE.read_text())
                                    except: pass
                                
                                # ✅ FIX: Define qty before using it
                                qty = max(1, int(float(task.get("amt", 5000)) / limit_px))

                                q_entry = {
                                    "id": sig_id, "action": "NEW_ORDER", "symbol": raw_sym, "exchange": task["exch"],
                                    "side": side, "qty": qty, "price": limit_px, "tf": tf,
                                    "order_type": order_type,
                                    "signal_level": limit_px, "signal_time": sig_time,
                                    "ltp": live_px,
                                    "target_val": row.get("target_val"),
                                    "sl_val": row.get("sl_val"),
                                    "opp_sig": row.get("opp_sig")
                                }
                                
                                paper_enabled = cfg.get("paper_trade", False)
                                live_enabled = cfg.get("live_trade", False)
                                
                                # ✅ MASTER SWITCH: Only write to JSON queues if Send to Queue is enabled
                                if cfg.get("send_to_queue", True):
                                    # WRITE TO PAPER QUEUE
                                    if not any(q.get("id") == sig_id for q in queue):
                                        queue.append(q_entry)
                                        SETUP_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
                                        print(f"✅ Queued PAPER {order_type} for {raw_sym} - {side} @ {limit_px}")
                                    
                                    # WRITE TO LIVE QUEUE
                                    live_q = []
                                    if LIVE_QUEUE_FILE.exists():
                                        try: live_q = json.loads(LIVE_QUEUE_FILE.read_text())
                                        except: pass
                                    if not any(q.get("id") == sig_id for q in live_q):
                                        live_q.append(q_entry)
                                        LIVE_QUEUE_FILE.write_text(json.dumps(live_q, indent=2))
                                        print(f"✅ Queued LIVE {order_type} for {raw_sym} - {side} @ {limit_px}")

                        # CALCULATE RSI
                        rsi_period = 14
                        delta = df_real["Close"].diff()
                        gain = delta.clip(lower=0)
                        loss = -delta.clip(upper=0)
                        avg_g = gain.ewm(alpha=1/rsi_period, adjust=False).mean()
                        avg_l = loss.ewm(alpha=1/rsi_period, adjust=False).mean()
                        rs = avg_g / avg_l.replace(0, float('nan'))
                        rsi_series = 100 - 100 / (1 + rs)
                        if pd.notna(rsi_series.iloc[-1]):
                            row["wt_rsi"] = round(float(rsi_series.iloc[-1]), 2)
                            row["wt_rsi_dir"] = "rising" if rsi_series.iloc[-1] > rsi_series.iloc[-2] else "falling"
                        else:
                            row["wt_rsi"], row["wt_rsi_dir"] = None, ""

                        SETUP_STATE[key] = row
                    except Exception as e:
                        print(f"Error on {raw_sym}: {e}")
                        SETUP_STATE[key] = {"symbol": raw_sym, "tf": tf, "status": "Error"}

            # Process queues ONLY if Send to Queue is enabled
            if cfg.get("send_to_queue", True):
                if cfg.get("paper_trade"):
                    _process_queue_file(SETUP_QUEUE_FILE, SETUP_PROCESSED_FILE, mode="PAPER")
                    
                if cfg.get("live_trade"):
                    _process_queue_file(LIVE_QUEUE_FILE, LIVE_PROCESSED_FILE, mode="LIVE")

                _check_pending_limits(downloaded_data)

        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
        finally:
            SETUP_IS_SCANNING = False
            SETUP_LAST_SCAN_TIME = time.time()
            SETUP_JOBS[job_id]["is_first_scan"] = False  # ✅ Mark first scan as done
            
            try: SETUP_STATE_FILE.write_text(json.dumps(SETUP_STATE, indent=2))
            except: pass
            
            # ✅ STEP 2: Atomic write for TRIGGER_STATE
            try:
                tmp_file = SETUP_TRIGGER_FILE.with_suffix('.tmp')
                tmp_file.write_text(json.dumps(TRIGGER_STATE, indent=2))
                tmp_file.replace(SETUP_TRIGGER_FILE)
            except Exception as e:
                print(f"⚠️ Failed to save trigger state: {e}")
            
        if SETUP_FORCE_SCAN:
            SETUP_FORCE_SCAN = False
            continue
            
        # ✅ Continuous background check every 1 second
        for _ in range(cfg.get("scan_interval", 1) * 60):
            if SETUP_JOBS[job_id]["stop"] or SETUP_FORCE_SCAN or cfg.get("paused", False): break
            
            # ✅ Instantly promote queue if ORDER column is empty
            queue_updated = False
            for k, r in SETUP_STATE.items():
                if r.get("pos_qty", 0) == 0 and r.get("order_col") == "—" and r.get("order_queue", "—") != "—":
                    r["order_col"] = r["order_queue"]
                    r["order_trig_price"] = r.get("queue_trig_price", r.get("trigger_lvl", 0))
                    r["order_queue"] = "—"
                    queue_updated = True
                    
            if queue_updated:
                try: SETUP_STATE_FILE.write_text(json.dumps(SETUP_STATE, indent=2))
                except: pass
                
            time.sleep(1)