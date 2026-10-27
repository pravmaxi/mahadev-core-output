import threading
import time
import json
import concurrent.futures
from pathlib import Path
import pandas as pd
import pytz

from data import download_batch, _SYMBOL_MAP
from indicators.common import candles_to_df, heikin_ashi
from indicators import get as get_indicator
from backtest import _to_ist
import paper_trades
import sheets

IST = pytz.timezone("Asia/Kolkata")
AUTO_JOBS = {}
AUTO_STATE = {}
IS_SCANNING = False
FORCE_SCAN = False
LAST_SCAN_TIME = 0

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PAPER_PROCESSED_FILE = DATA_DIR / "paper_processed.json"
ZE_PROCESSED_FILE = DATA_DIR / "zerodha_processed.json"
PAPER_QUEUE_FILE = DATA_DIR / "paper_orders_queue.json"
ZE_QUEUE_FILE = DATA_DIR / "zerodha_orders_queue.json"

def _load_json(filepath):
    if not filepath.exists(): return set() if 'processed' in filepath.name else []
    try:
        data = json.loads(filepath.read_text())
        return set(data) if 'processed' in filepath.name else data
    except: return set() if 'processed' in filepath.name else []

def _save_json(filepath, data):
    try:
        filepath.write_text(json.dumps(list(data) if isinstance(data, set) else data, indent=2))
    except Exception as e:
        print(f"⚠️ Failed to save {filepath.name}: {e}")

def _safe_float(val, default=0.0):
    try:
        if val is None or val == "": return default
        return float(val)
    except:
        return default

def start_job(job_id, config):
    AUTO_STATE.clear()
    AUTO_JOBS[job_id] = {"stop": False, "config": config}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def stop_job(job_id):
    if job_id in AUTO_JOBS:
        AUTO_JOBS[job_id]["stop"] = True
    AUTO_STATE.clear()

def _worker(job_id):
    tasks = AUTO_JOBS[job_id]["config"].get("tasks", [])
    print(f"🚀 [AUTO] Started Job {job_id} with {len(tasks)} tasks")
    
    # Populate UI immediately
    for t in tasks:
        key = f"{t['sym']}_{t['tf']}"
        AUTO_STATE[key] = {
            "sym": t["sym"], "tf": t["tf"], "ltp": 0, 
            "buy_trigger": None, "sell_trigger": None, 
            "buy_time": "", "sell_time": "",
            "buy_val": 0, "sell_val": 0, "signal_color": "mut",
            "status": "Loading...", "target_px": None, "stop_px": None,
            "action": "Waiting", "order_info": "—"
        }

    while not AUTO_JOBS[job_id]["stop"]:
        try:
            global IS_SCANNING, FORCE_SCAN, LAST_SCAN_TIME
            IS_SCANNING = True
            
            cfg = AUTO_JOBS[job_id]["config"]
            
            fno_list = None
            if cfg.get("fno_mode") == "sheet":
                try:
                    fno_list = set(sheets.read_symbols(cfg.get("fno_book", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input")))
                except Exception:
                    pass

            paper_processed = _load_json(PAPER_PROCESSED_FILE)
            ze_processed = _load_json(ZE_PROCESSED_FILE)
            paper_queue = _load_json(PAPER_QUEUE_FILE)
            ze_queue = _load_json(ZE_QUEUE_FILE)

            open_positions = paper_trades.positions()
            pos_map = {f"{p['symbol']}_{p.get('tag', '')}": p for p in open_positions if p["qty"] != 0}
            open_orders = [o for o in paper_trades.list_orders() if o["status"] == "OPEN"]
            open_order_map = {f"{o['symbol']}_{o.get('tag', '')}": o for o in open_orders}

            # --- 1. MAP SYMBOLS ---
            sym_map = {}
            for t in tasks:
                raw_sym = t["sym"]
                exch = t["exch"]
                sym_yf = raw_sym.upper().replace(" ", "")
                if exch == "NSE" and not sym_yf.endswith(".NS") and "." not in sym_yf and "=" not in sym_yf:
                    sym_yf = sym_yf + ".NS"
                elif exch == "BSE" and not sym_yf.endswith(".BO") and "." not in sym_yf and "=" not in sym_yf:
                    sym_yf = sym_yf + ".BO"
                elif exch == "FX":
                    sym_yf = _SYMBOL_MAP.get(sym_yf, sym_yf)
                sym_map[f"{raw_sym}_{t['tf']}"] = sym_yf

            # --- 2. BATCH DOWNLOAD OHLC (WITH TIMEOUT) ---
            from settings import YF_CONFIG
            tf_groups = {}
            for task in tasks:
                tf = task["tf"]
                key = f"{task['sym']}_{tf}"
                tf_groups.setdefault(tf, []).append(sym_map[key])
                
            downloaded_data = {}
            for tf, syms in tf_groups.items():
                # Force the period to match the Backtest (60d for intraday, 6mo for 1h)
                period = "60d" if tf in ["5m", "15m", "30m"] else "6mo"
                try:
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        # force_refresh=True bypasses the 5-minute cache
                        future = executor.submit(download_batch, syms, interval=tf, period=period, force_refresh=True)
                        res = future.result(timeout=15) # Hard 15-sec timeout
                        for s_yf, rows in res.items():
                            downloaded_data[f"{tf}_{s_yf}"] = rows
                except concurrent.futures.TimeoutError:
                    print(f"⏳ Yahoo Finance Timeout for {tf}. Skipping this cycle.")
                except Exception as e:
                    print(f"Batch download failed for {tf}: {e}")

            # --- 3. PROCESS EACH TASK ---
            for task in tasks:
                if AUTO_JOBS[job_id]["stop"]: break
                
                raw_sym = task["sym"]
                tf = task["tf"]
                alloc_amt = task["amt"]
                exch = task["exch"]
                key = f"{raw_sym}_{tf}"
                sym_yf = sym_map[key]
                
                try:
                    rows = downloaded_data.get(f"{tf}_{sym_yf}", [])
                    if not rows or len(rows) < 40: 
                        AUTO_STATE[key]["status"] = "No Data"
                        continue
                    
                    df_real = candles_to_df(rows)
                    df_real = _to_ist(df_real)
                    df_closed = df_real.iloc[:-1].copy() if len(df_real) > 1 else df_real.copy()
                    
                    # Debug: Print the last candle time to the Python console
                    if raw_sym == "APOLLOHOSP":
                        print(f"DEBUG: Latest candle for {raw_sym} is {df_closed.index[-1]}")
                    current_ltp = float(df_real["Close"].iloc[-1])

                    current_pos = pos_map.get(key)
                    current_order = open_order_map.get(key)

                    # --- Calculate Signals ---
                    df_ha_closed = heikin_ashi(df_closed) if cfg.get("candles") == "ha" else df_closed
                    mod = get_indicator("wt")
                    comp = mod.compute(df_ha_closed, {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)})
                    
                    buy_idx, sell_idx = -1, -1
                    for i in range(len(df_ha_closed) - 1, max(0, len(df_ha_closed) - 100), -1):
                        c = comp["cross"].iloc[i]
                        if c == "Bull Cross" and buy_idx == -1: buy_idx = i
                        if c == "Bear Cross" and sell_idx == -1: sell_idx = i
                        if buy_idx != -1 and sell_idx != -1: break

                    buy_trig, buy_val, buy_time, buy_action = None, 0.0, "", "Waiting"
                    buy_triggered = False
                    if buy_idx != -1:
                        raw_trig = float(df_ha_closed["High"].iloc[buy_idx])
                        if not pd.isna(raw_trig) and raw_trig > 0:
                            buy_trig = raw_trig
                            buy_val = float(comp["wt1"].iloc[buy_idx]) if not pd.isna(comp["wt1"].iloc[buy_idx]) else 0.0
                            buy_time = str(df_ha_closed.index[buy_idx]).split("+")[0].replace("T", " ")
                            buy_triggered = any(float(df_ha_closed["High"].iloc[j]) > buy_trig for j in range(buy_idx + 1, len(df_ha_closed)))
                            
                            if current_ltp > buy_trig:
                                buy_action = "Buy Market Order" if cfg.get("allow_market_orders", False) else "Buy Gap"
                            else:
                                buy_action = f"Buy Limit @ {buy_trig:.2f}"

                    sell_trig, sell_val, sell_time, sell_action = None, 0.0, "", "Waiting"
                    sell_triggered = False
                    if sell_idx != -1:
                        raw_trig = float(df_ha_closed["Low"].iloc[sell_idx])
                        if not pd.isna(raw_trig) and raw_trig > 0:
                            sell_trig = raw_trig
                            sell_val = float(comp["wt2"].iloc[sell_idx]) if not pd.isna(comp["wt2"].iloc[sell_idx]) else 0.0
                            sell_time = str(df_ha_closed.index[sell_idx]).split("+")[0].replace("T", " ")
                            sell_triggered = any(float(df_ha_closed["Low"].iloc[j]) < sell_trig for j in range(sell_idx + 1, len(df_ha_closed)))
                            
                            if current_ltp < sell_trig:
                                sell_action = "Sell Market Order" if cfg.get("allow_market_orders", False) else "Sell Gap"
                            else:
                                sell_action = f"Sell Limit @ {sell_trig:.2f}"

                    primary_action = "Waiting"
                    if cfg.get("side_mode") == "long":
                        if buy_idx != -1: primary_action = buy_action
                    elif cfg.get("side_mode") == "short":
                        if sell_idx != -1: primary_action = sell_action
                    else:
                        if buy_idx != -1 and sell_idx != -1:
                            primary_action = buy_action if buy_idx > sell_idx else sell_action
                        elif buy_idx != -1: primary_action = buy_action
                        elif sell_idx != -1: primary_action = sell_action

                    sig_id = None
                    if primary_action.startswith("Buy"): sig_id = f"{raw_sym}_{tf}_BUY_{buy_time}"
                    elif primary_action.startswith("Sell"): sig_id = f"{raw_sym}_{tf}_SELL_{sell_time}"
                    original_action = primary_action

                    if sig_id and not current_pos and not current_order:
                        if cfg.get("paper_trade", True) and sig_id not in paper_processed and primary_action != "Executed (Wait)":
                            if primary_action != "Buy Gap" and primary_action != "Sell Gap":
                                paper_queue.append({"symbol": raw_sym, "exchange": exch, "tf": tf, "action": primary_action, "alloc_amt": alloc_amt, "ltp": current_ltp})
                                paper_processed.add(sig_id)
                                _save_json(PAPER_PROCESSED_FILE, paper_processed)
                                _save_json(PAPER_QUEUE_FILE, paper_queue)
                        
                        if cfg.get("live_trade", False) and sig_id not in ze_processed and primary_action != "Executed (Wait)":
                            if primary_action != "Buy Gap" and primary_action != "Sell Gap":
                                ze_queue.append({"symbol": raw_sym, "exchange": exch, "tf": tf, "action": primary_action, "alloc_amt": alloc_amt, "ltp": current_ltp})
                                ze_processed.add(sig_id)
                                _save_json(ZE_PROCESSED_FILE, ze_processed)
                                _save_json(ZE_QUEUE_FILE, ze_queue)

                    if sig_id and sig_id in paper_processed:
                        primary_action = "Executed (Wait)"

                    # --- UI Formatting ---
                    suffix = ""
                    p_trade = cfg.get("paper_trade", True)
                    l_trade = cfg.get("live_trade", False)
                    if p_trade and l_trade: suffix = " to PA & ZE"
                    elif p_trade: suffix = " to PA"
                    elif l_trade: suffix = " to ZE"
                    else: suffix = " (Disabled)"
                    
                    order_info = "—"
                    if current_order:
                        order_info = f"{current_order['side']} {current_order['order_type']} @ {current_order['price']:.2f} (Qty: {current_order['qty']})"
                    elif current_pos:
                        filled_orders = [o for o in paper_trades.list_orders() if o["symbol"] == raw_sym and o.get("tag") == tf and o["status"] == "FILLED"]
                        if filled_orders:
                            last_ord = filled_orders[-1]
                            order_info = f"{last_ord['side']} {last_ord['order_type']} @ {last_ord['fill_price']:.2f} (Qty: {last_ord['qty']})"
                        else:
                            side_label = "BUY" if current_pos['qty'] > 0 else "SELL"
                            order_info = f"Filled {side_label} @ {current_pos['avg_price']:.2f} (Qty: {abs(current_pos['qty'])})"
                    else:
                        if original_action.startswith("Buy Limit @"):
                            limit_px = float(original_action.split("@")[1].strip())
                            if limit_px > 0: order_info = f"BUY LIMIT @ {limit_px:.2f} (Qty: {int(alloc_amt / limit_px)})"
                        elif original_action.startswith("Sell Limit @"):
                            limit_px = float(original_action.split("@")[1].strip())
                            if limit_px > 0: order_info = f"SELL LIMIT @ {limit_px:.2f} (Qty: {int(alloc_amt / limit_px)})"
                        elif original_action.startswith("Buy Market"):
                            order_info = f"BUY MARKET @ {current_ltp:.2f} (Qty: {int(alloc_amt / current_ltp)})"
                        elif original_action.startswith("Sell Market"):
                            order_info = f"SELL MARKET @ {current_ltp:.2f} (Qty: {int(alloc_amt / current_ltp)})"

                    if order_info != "—" and not current_pos:
                        order_info += suffix

                    # --- Calculate Target/Stop ---
                    target_px = 0.0
                    stop_px = 0.0
                    opp_signal_px = None
                    t_pct = _safe_float(cfg.get("target_pct"), 2.0)
                    s_pct = _safe_float(cfg.get("stop_pct"), 0.0) # Default to 0 (Off) if blank

                    if current_pos:
                        side_sign = 1 if current_pos['qty'] > 0 else -1
                        entry_px = current_pos['avg_price']
                        
                        target_px = entry_px * (1 + side_sign * t_pct / 100)
                        if s_pct > 0:
                            stop_px = entry_px * (1 - side_sign * s_pct / 100)
                            
                        if side_sign > 0 and sell_trig and not sell_triggered:
                            opp_signal_px = sell_trig
                        elif side_sign < 0 and buy_trig and not buy_triggered:
                            opp_signal_px = buy_trig
                        
                        close_pos = False
                        reason = ""
                        
                        if side_sign > 0 and current_ltp >= target_px:
                            close_pos = True; reason = "Target Hit"
                        elif side_sign < 0 and current_ltp <= target_px:
                            close_pos = True; reason = "Target Hit"
                        elif s_pct > 0:
                            if side_sign > 0 and current_ltp <= stop_px:
                                close_pos = True; reason = "Stop Loss Hit"
                            elif side_sign < 0 and current_ltp >= stop_px:
                                close_pos = True; reason = "Stop Loss Hit"
                        elif cfg.get("exit_opp", True) and opp_signal_px:
                            if side_sign > 0 and current_ltp <= opp_signal_px:
                                close_pos = True; reason = "Opposite Signal"
                            elif side_sign < 0 and current_ltp >= opp_signal_px:
                                close_pos = True; reason = "Opposite Signal"
                                
                        if close_pos:
                            close_side = "SELL" if current_pos["qty"] > 0 else "BUY"
                            qty = abs(current_pos["qty"])
                            paper_trades.place_order(symbol=raw_sym, exchange=exch, side=close_side, qty=qty, order_type="MARKET", price=None, product="MIS", ltp=current_ltp, tag=tf)
                            del pos_map[key]
                            current_pos = None
                            print(f"✅ [AUTO] Closed position for {raw_sym}_{tf} at {current_ltp} ({reason})")
                            display_action = "Waiting"

                    else:
                        if original_action.startswith("Buy"):
                            side_sign = 1
                            entry_px = current_ltp if "Market" in original_action else buy_trig
                            if entry_px:
                                target_px = entry_px * (1 + side_sign * t_pct / 100)
                                if s_pct > 0: stop_px = entry_px * (1 - side_sign * s_pct / 100)
                                if sell_trig and not sell_triggered: opp_signal_px = sell_trig
                                
                        elif original_action.startswith("Sell"):
                            side_sign = -1
                            entry_px = current_ltp if "Market" in original_action else sell_trig
                            if entry_px:
                                target_px = entry_px * (1 + side_sign * t_pct / 100)
                                if s_pct > 0: stop_px = entry_px * (1 - side_sign * s_pct / 100)
                                if buy_trig and not buy_triggered: opp_signal_px = buy_trig

                    display_action = "Hold Position" if current_pos else ("Executed (Wait)" if current_order or (sig_id and sig_id in paper_processed) else primary_action)
                    if not current_pos and not current_order and primary_action not in ("Executed (Wait)", "Waiting"):
                        display_action += suffix

                    AUTO_STATE[key] = {
                        "sym": raw_sym, "tf": tf, "ltp": round(current_ltp, 2),
                        "buy_trigger": round(buy_trig, 2) if (buy_trig and not buy_triggered) else None,
                        "sell_trigger": round(sell_trig, 2) if (sell_trig and not sell_triggered) else None,
                        "buy_time": buy_time, "sell_time": sell_time,
                        "buy_val": round(buy_val, 2) if buy_val else 0.0, 
                        "sell_val": round(sell_val, 2) if sell_val else 0.0,
                        "signal_color": "buy" if buy_idx > sell_idx else "sell",
                        "status": "In Position" if key in pos_map else "Scanning",
                        "target_px": round(target_px, 2) if target_px > 0 else None,
                        "stop_px": round(stop_px, 2) if stop_px > 0 else None,
                        "opp_signal_px": round(opp_signal_px, 2) if opp_signal_px else None,
                        "action": display_action,
                        "order_info": order_info
                    }

                    time.sleep(0.2)

                except Exception as e:
                    print(f"⚠️ [AUTO] Error on {raw_sym}_{tf}: {e}")
                    AUTO_STATE[key]["status"] = "Error"

            # --- 4. Process Queues ---
            if paper_queue:
                for order in paper_queue:
                    try:
                        action = order["action"]
                        sym = order["symbol"]
                        exch = order["exchange"]
                        tf = order["tf"]
                        amt = order["alloc_amt"]
                        ltp = order.get("ltp", 0)
                        
                        if action.startswith("Buy Market"):
                            qty = int(amt / ltp)
                            paper_trades.place_order(symbol=sym, exchange=exch, side="BUY", qty=qty, order_type="MARKET", price=None, product="MIS", ltp=ltp, tag=tf)
                        elif action.startswith("Buy Limit @"):
                            limit_px = float(action.split("@")[1].strip())
                            qty = int(amt / limit_px)
                            paper_trades.place_order(symbol=sym, exchange=exch, side="BUY", qty=qty, order_type="LIMIT", price=limit_px, product="MIS", ltp=None, tag=tf)
                        elif action.startswith("Sell Market"):
                            qty = int(amt / ltp)
                            paper_trades.place_order(symbol=sym, exchange=exch, side="SELL", qty=qty, order_type="MARKET", price=None, product="MIS", ltp=ltp, tag=tf)
                        elif action.startswith("Sell Limit @"):
                            limit_px = float(action.split("@")[1].strip())
                            qty = int(amt / limit_px)
                            paper_trades.place_order(symbol=sym, exchange=exch, side="SELL", qty=qty, order_type="LIMIT", price=limit_px, product="MIS", ltp=None, tag=tf)
                    except Exception as e:
                        print(f"❌ Failed to execute paper order: {e}")
                _save_json(PAPER_QUEUE_FILE, [])
                paper_queue = []

            if ze_queue:
                _save_json(ZE_QUEUE_FILE, [])
                ze_queue = []

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