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
ZE_PROCESSED_FILE = DATA_DIR / "zerodha_processed.json"
ZE_QUEUE_FILE = DATA_DIR / "zerodha_orders_queue.json"

def start_job(job_id, config):
    AUTO_STATE.clear()
    AUTO_JOBS[job_id] = {"stop": False, "config": config}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def stop_job(job_id):
    if job_id in AUTO_JOBS:
        AUTO_JOBS[job_id]["stop"] = True
    # ✅ FIX: Do not clear AUTO_STATE here to prevent race conditions with the UI.
    # The worker thread will naturally exit, and start_job() will clear it on the next run.

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
            
            # ✅ FIX: Grab the price directly from signal_level if it's missing
            limit_px = q.get("price")
            if not limit_px:
                limit_px = q.get("signal_level")
            if not limit_px:
                limit_px = q.get("ltp") or 0
                
            o = paper_trades.place_order(
                symbol=q["symbol"], exchange=q["exchange"], side=q["side"], 
                qty=q["qty"], order_type=o_type, price=limit_px if o_type=="LIMIT" else None, product="MIS", ltp=q.get("ltp") or limit_px
            )
            # Only fill MARKET orders immediately. LIMIT orders stay OPEN until hit.
            if q.get("order_type", "MARKET") == "MARKET":
                paper_trades.mark_filled(o["id"], q.get("ltp") or limit_px)
            
            # Store details as requested
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
    PAPER_QUEUE_FILE.write_text("[]") # Clear queue

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
                    # User requested logic: Buy fills if LTP > Limit, Sell fills if LTP < Limit
                    # User rule: Buy fills if LTP > Limit, Sell fills if LTP < Limit
                    if o["side"] == "BUY" and live_px > limit_px:
                        paper_trades.mark_filled(o["id"], limit_px) # Fills exactly at Limit
                        print(f"✅ Limit Buy filled for {o['symbol']} at Limit {limit_px}")
                    elif o["side"] == "SELL" and live_px < limit_px:
                        paper_trades.mark_filled(o["id"], limit_px) # Fills exactly at Limit
                        print(f"✅ Limit Sell filled for {o['symbol']} at Limit {limit_px}")

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
        
        # 1. Target Check
        if target_pct > 0:
            if side == "buy" and live_px >= avg_px * (1 + target_pct/100): exit_reason = "Target"
            if side == "sell" and live_px <= avg_px * (1 - target_pct/100): exit_reason = "Target"
            
        # 2. Stop Loss Check
        if not exit_reason and stop_pct > 0:
            if side == "buy" and live_px <= avg_px * (1 - stop_pct/100): exit_reason = "Stop Loss"
            if side == "sell" and live_px >= avg_px * (1 + stop_pct/100): exit_reason = "Stop Loss"
            
        # 3. Opposite Signal Check
        if not exit_reason and cfg.get("exit_on_opposite", True):
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
                    qty=abs(p["qty"]), order_type="MARKET", price=None, product=p["product"], ltp=live_px
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
            
            # Pause the loop if the UI unchecked Auto-Scan
            if AUTO_JOBS[job_id]["config"].get("paused", False):
                IS_SCANNING = False
                time.sleep(1)
                continue
                
            IS_SCANNING = True
            
            # RE-READ ALLOCATIONS ON EVERY SCAN
            if ALLOC_FILE.exists():
                try:
                    tasks = json.loads(ALLOC_FILE.read_text())
                except: pass
            
            # Remove stocks that were deleted from allocations.json
            current_keys = {f"{t['sym']}_{t['tf']}" for t in tasks}
            for k in list(AUTO_STATE.keys()):
                if k not in current_keys:
                    del AUTO_STATE[k]
                
            cfg = AUTO_JOBS[job_id]["config"]
            
            # Map symbols
            sym_map = {}
            for t in tasks:
                raw_sym = t["sym"]
                exch = t["exch"]
                sym_yf = raw_sym.upper().replace(" ", "")
                if exch == "NSE" and not sym_yf.endswith(".NS") and "." not in sym_yf and "=" not in sym_yf:
                    sym_yf = sym_yf + ".NS"
                elif exch == "BSE" and not sym_yf.endswith(".BO") and "." not in sym_yf and "=" not in sym_yf:
                    sym_yf = sym_yf + ".BO"
                else:
                    # ✅ FIX: Translate Forex to EURUSD=X / Gold to GC=F
                    sym_yf = _SYMBOL_MAP.get(sym_yf, sym_yf)
                sym_map[f"{raw_sym}_{t['tf']}"] = sym_yf

            # Group by TF
            tf_groups = {}
            for task in tasks:
                tf = task["tf"]
                key = f"{task['sym']}_{tf}"
                tf_groups.setdefault(tf, []).append(sym_map[key])
                
            downloaded_data = {}
            for tf, syms in tf_groups.items():
                # ✅ FIX: 5m/15m/30m strictly limited to 60d by Yahoo Finance. 1h allows 3mo.
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

            # EXACT SCANNER LOGIC
            fno_mode = cfg.get("fno_mode", "none")
            fno_list = None
            
            # ✅ FIX: Check if allocations actually contain any Indian stocks
            has_indian_stocks = any(
                t.get("exch", "").upper() in ["NSE", "BSE"] or 
                str(t.get("sym", "")).upper().endswith((".NS", ".BO")) 
                for t in tasks
            )
            
            if fno_mode == "sheet" and has_indian_stocks:
                try:
                    raw_fno = sheets.read_symbols(cfg.get("fno_book", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input"))
                    fno_list = {str(s).replace(".NS", "").replace(".BO", "").upper() for s in raw_fno}
                    print(f"📄 F&O Filter enabled. Read {len(fno_list)} symbols from {cfg.get('fno_book')}::{cfg.get('fno_sheet')}")
                except Exception as e:
                    print(f"⚠️ Failed to read F&O sheet: {e}")
            elif fno_mode == "sheet" and not has_indian_stocks:
                print("⏭️ [AUTO] Skipping F&O Sheet read: No NSE/BSE stocks in allocations.")
                fno_mode = "none"  # Automatically disable the filter for this run

            use_ha = cfg.get("candles", "ha") == "ha"
            picked = ["wt"]

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
                    
                    # Force Timezone to IST immediately
                    if df_real.index.tz is None:
                        df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
                    else:
                        df_real.index = df_real.index.tz_convert(IST)
                        
                    if fno_mode == "all" or (fno_mode == "sheet" and fno_list and sym_clean in fno_list):
                        df_real = apply_fno_filter(df_real, timeframe=tf)
                    
                    if len(df_real) < 30: continue
                    df_ha = heikin_ashi(df_real) if use_ha else df_real

                    live_px = float(df_real["Close"].iloc[-1])
                    
                    # Dynamic rounding: 5 decimals for Forex (< 10), 2 for stocks/gold
                    def _r(val):
                        return round(val, 5) if abs(val) < 10 else round(val, 2)

                    row = {
                        "symbol": raw_sym, "tf": tf, "ltp": _r(live_px), "yf_symbol": sym_yf
                    }
                    
                    for k in picked:
                        mod = get_indicator(k)
                        if mod is None: continue
                        
                        params = {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)}
                        params["investment"] = float(task.get("amt", 5000))
                        params["profit_target_pct"] = float(cfg.get("target_pct", 5))
                        params["live_ltp"] = live_px

                        mem_key = f"{sym_yf}|{k}"
                        last_state = TRIGGER_STATE.get(mem_key)
                        df_for_ind = df_real if k == "st" else df_ha

                        computed = mod.compute(df_for_ind, params)
                        
                        # --- Recent Buy/Sell Signals ---
                        buy_idx, sell_idx = -1, -1
                        for i in range(len(df_ha) - 1, max(0, len(df_ha) - 100), -1):
                            c = computed["cross"].iloc[i]
                            if c == "Bull Cross" and buy_idx == -1: buy_idx = i
                            if c == "Bear Cross" and sell_idx == -1: sell_idx = i
                            if buy_idx != -1 and sell_idx != -1: break
                            
                        buy_sig_val, buy_sig_time, buy_sig_wt = None, "", ""
                        if buy_idx != -1:
                            buy_sig_val = _r(float(df_ha["High"].iloc[buy_idx]))
                            buy_sig_time = str(df_ha.index[buy_idx]).split("+")[0].replace("T", " ")
                            _v = computed["wt1"].iloc[buy_idx] if "wt1" in computed else None
                            buy_sig_wt = round(float(_v), 2) if _v is not None and _v == _v else ""
                            
                        sell_sig_val, sell_sig_time, sell_sig_wt = None, "", ""
                        if sell_idx != -1:
                            sell_sig_val = _r(float(df_ha["Low"].iloc[sell_idx]))
                            sell_sig_time = str(df_ha.index[sell_idx]).split("+")[0].replace("T", " ")
                            _v = computed["wt2"].iloc[sell_idx] if "wt2" in computed else None
                            sell_sig_wt = round(float(_v), 2) if _v is not None and _v == _v else ""
                            
                        row["recent_buy_val"] = buy_sig_val
                        row["recent_buy_time"] = buy_sig_time
                        row["recent_buy_wt"] = buy_sig_wt
                        row["recent_sell_val"] = sell_sig_val
                        row["recent_sell_time"] = sell_sig_time
                        row["recent_sell_wt"] = sell_sig_wt
                        # -------------------------------
                        
                        try:
                            sig = mod.signals(df_for_ind, params, computed, state=last_state)
                        except TypeError:
                            sig = mod.signals(df_for_ind, params, computed)

                        new_state = sig.pop("_new_state", None)
                        if new_state is not None:
                            TRIGGER_STATE[mem_key] = new_state
                        sig.pop(f"{k}_details", None)
                        row.update(sig)
                        
                        # ✅ FIX: Format WT Action string to show 5 decimals for Forex
                        if row.get("wt_action") and "close" in str(row["wt_action"]).lower():
                            import re
                            def _fix_px(m):
                                return f"close {_r(float(m.group(1)))}"
                            row["wt_action"] = re.sub(r'close\s+([\d\.]+)', _fix_px, str(row["wt_action"]))
                        
                        # --- Side Mode & Valid Opposite Signal ---
                        action = row.get("wt_action", "")
                        # ✅ FIX: Re-format the text to show 5 decimals for Forex
                        if action and "close" in action.lower():
                            import re
                            def _fix_px(m):
                                return f"close {_r(float(m.group(1)))}"
                            action = re.sub(r'close\s+([\d\.]+)', _fix_px, action)
                            row["wt_action"] = action
                        side_mode = cfg.get("side_mode", "both")
                        
                        # Filter action by side mode
                        if side_mode == "long" and "Sell" in action:
                            row["wt_action"] = ""
                            action = ""
                        elif side_mode == "short" and "Buy" in action:
                            row["wt_action"] = ""
                            action = ""
                            
                        row["valid_opp_val"] = None
                        row["valid_opp_time"] = ""
                        row["valid_opp_dir"] = ""
                        
                        # If Exit on Opposite is enabled, find the valid pending opposite cross
                        if cfg.get("exit_on_opposite", True) and action:
                            opp_dir = "sell" if "Buy" in action else "buy" if "Sell" in action else ""
                            if opp_dir:
                                opp_cross = "Bear Cross" if opp_dir == "sell" else "Bull Cross"
                                for i in range(len(df_ha) - 2, -1, -1):
                                    if computed["cross"].iloc[i] == opp_cross:
                                        t_level = float(df_ha["Low"].iloc[i]) if opp_dir == "sell" else float(df_ha["High"].iloc[i])
                                        triggered = False
                                        for j in range(i+1, len(df_ha)):
                                            if opp_dir == "sell" and float(df_ha["Low"].iloc[j]) < t_level:
                                                triggered = True; break
                                            if opp_dir == "buy" and float(df_ha["High"].iloc[j]) > t_level:
                                                triggered = True; break
                                        if not triggered:
                                            row["valid_opp_val"] = round(t_level, 2)
                                            row["valid_opp_time"] = str(df_ha.index[i]).split("+")[0].replace("T", " ")
                                            row["valid_opp_dir"] = opp_dir
                                        break
                        # -------------------------------

                        # Check for existing position
                        pos_avg_px = live_px
                        has_open_pos = False
                        pos_side = ""
                        try:
                            for p in paper_trades.positions():
                                if p["symbol"] == raw_sym and p["qty"] != 0:
                                    pos_avg_px = p["avg_price"]
                                    has_open_pos = True
                                    pos_side = "Buy" if p["qty"] > 0 else "Sell"
                                    break
                        except: pass

                        if has_open_pos:
                            row["wt_action"] = "Position in progress"
                            row["sent_to"] = ""
                            
                            # Calculate Target/SL based on actual position
                            target_pct = float(cfg.get("target_pct", 0))
                            stop_pct = float(cfg.get("stop_pct", 0))
                            if target_pct > 0:
                                row["target_val"] = _r(pos_avg_px * (1 + target_pct/100)) if pos_side == "Buy" else _r(pos_avg_px * (1 - target_pct/100))
                            else:
                                row["target_val"] = None
                            if stop_pct > 0:
                                row["sl_val"] = _r(pos_avg_px * (1 - stop_pct/100)) if pos_side == "Buy" else _r(pos_avg_px * (1 + stop_pct/100))
                            elif row.get("valid_opp_val"):
                                row["sl_val"] = row["valid_opp_val"]
                            else:
                                row["sl_val"] = None
                        else:
                            # --- WRITE TO PAPER QUEUE ---
                            if cfg.get("paper_trade") and row.get("wt_action"):
                                if "Market Order" in action or "Limit Order" in action:
                                    side = "BUY" if "Buy" in action else "SELL"
                                    sig_time = row.get("recent_buy_time") if side == "BUY" else row.get("recent_sell_time")
                                    sig_val = row.get("recent_buy_val") if side == "BUY" else row.get("recent_sell_val")
                                    sig_id = f"{raw_sym}_{tf}_{side}_{sig_time}"
                                    
                                    order_type = "MARKET" if "Market Order" in action else "LIMIT"
                                    limit_price = live_px
                                    if order_type == "LIMIT":
                                        if sig_val is not None:
                                            limit_price = float(sig_val)
                                            action = f"{'Buy' if side == 'BUY' else 'Sell'} Limit Order (BO close {_r(limit_price)})"
                                            row["wt_action"] = action
                                        else:
                                            import re
                                            match = re.search(r'close\s+([\d\.]+)', action)
                                            if match: limit_price = float(match.group(1))
                                    
                                    processed = []
                                    if PAPER_PROCESSED_FILE.exists():
                                        try: processed = json.loads(PAPER_PROCESSED_FILE.read_text())
                                        except: pass
                                    
                                    is_allowed = (order_type == "LIMIT") or cfg.get("allow_market_orders", False)
                                    already_processed = any(p.get("id") == sig_id for p in processed)
                                    
                                    if not already_processed and is_allowed:
                                        queue = []
                                        if PAPER_QUEUE_FILE.exists():
                                            try: queue = json.loads(PAPER_QUEUE_FILE.read_text())
                                            except: pass
                                        
                                        existing_q = next((q for q in queue if q.get("symbol") == raw_sym), None)
                                        if existing_q:
                                            row["wt_action"] = f"Pending {existing_q.get('order_type')} ({existing_q.get('price')})"
                                        else:
                                            qty = max(1, int(float(task.get("amt", 5000)) / live_px))
                                            queue.append({
                                                "id": sig_id, "symbol": raw_sym, "exchange": task["exch"],
                                                "side": side, "qty": qty, "price": limit_price, "tf": tf,
                                                "order_type": order_type,
                                                "signal_level": sig_val, "signal_time": sig_time
                                            })
                                            PAPER_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
                                            
                                sent_to = []
                                if cfg.get("paper_trade") and (is_allowed or already_processed): sent_to.append("pa")
                                if cfg.get("live_trade") and (is_allowed or already_processed): sent_to.append("ze")
                                row["sent_to"] = " & ".join(sent_to) if sent_to else ""
                            else:
                                row["sent_to"] = ""
                                
                            # Calculate Target and SL for new signals
                            target_pct = float(cfg.get("target_pct", 0))
                            stop_pct = float(cfg.get("stop_pct", 0))
                            
                            if target_pct > 0:
                                row["target_val"] = _r(pos_avg_px * (1 + target_pct/100)) if "Buy" in action else _r(pos_avg_px * (1 - target_pct/100))
                            else:
                                row["target_val"] = None
                                
                            if stop_pct > 0:
                                row["sl_val"] = _r(pos_avg_px * (1 - stop_pct/100)) if "Buy" in action else _r(pos_avg_px * (1 + stop_pct/100))
                            elif row.get("valid_opp_val"):
                                row["sl_val"] = row["valid_opp_val"]
                            else:
                                row["sl_val"] = None
                        # -------------------------------

                    AUTO_STATE[key] = row

                except Exception as e:
                    print(f"⚠️ [AUTO] Error on {raw_sym}_{tf}: {e}")
                    AUTO_STATE[key] = {"symbol": raw_sym, "status": "Error"}

            # --- WRITE TO GOOGLE SHEET ---
            if cfg.get("write_to_sheet", False):
                try:
                    headers = ["Symbol", "TF", "LTP", "WT1", "WT2", "WT Cross", "WT Trend", "Trig Status", "Recent Buy", "Recent Sell", "Valid Opp Signal", "WT Action", "Sent To", "Target", "Stop Loss"]
                    rows_out = []
                    for r in AUTO_STATE.values():
                        if r.get("status") in ("No Data", "Error"): continue
                        
                        rb = f"{r.get('recent_buy_val')} ({r.get('recent_buy_time')})" if r.get('recent_buy_val') else ""
                        rs = f"{r.get('recent_sell_val')} ({r.get('recent_sell_time')})" if r.get('recent_sell_val') else ""
                        vo = f"{r.get('valid_opp_val')} ({r.get('valid_opp_time')})" if r.get('valid_opp_val') else ""
                        
                        rows_out.append([
                            r.get("symbol"), r.get("tf"), r.get("ltp"), r.get("wt1"), r.get("wt2"),
                            r.get("wt_cross"), r.get("wt_trend"), r.get("wt_trigger_status"),
                            rb, rs, vo, r.get("wt_action"), r.get("sent_to"), r.get("target_val"), r.get("sl_val")
                        ])
                    
                    sheets.write_results("paper trading", "test_output", headers, rows_out)
                    print("✅ Written to paper trading / test_output")
                except Exception as e:
                    print(f"❌ Failed to write to GSheet: {e}")

            # Process any pending orders in the queue
            _process_paper_queue()
            _check_pending_limits(downloaded_data)
            _check_paper_exits(cfg, downloaded_data)

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