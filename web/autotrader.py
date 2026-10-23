import threading
import time
import json
from pathlib import Path
import pandas as pd
from datetime import datetime
import pytz

from data import download, _SYMBOL_MAP
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
from indicators import get as get_indicator
import backtest
from backtest import _to_ist
import paper_trades

IST = pytz.timezone("Asia/Kolkata")
STATE_FILE = Path(__file__).resolve().parent / "data" / "autotrade_state.json"

AUTO_JOBS = {}
AUTO_STATE = {}
IS_SCANNING = False
LAST_SCAN_TIME = time.time()

def get_active_pending(df, comp, max_wait):
    """Extract the currently active pending candles, respecting max_wait and same_dir rules."""
    n = len(df)
    pend = {"buy": None, "sell": None}
    for i in range(1, n):
        h = float(df.iloc[i]["High"])
        l = float(df.iloc[i]["Low"])
        
        for direction in ("buy", "sell"):
            p = pend[direction]
            if p is None: continue
            hit = ((direction == "buy"  and h > p["level"]) or
                   (direction == "sell" and l < p["level"]))
            if hit:
                pend[direction] = None
            elif i - p["valid_i"] >= max_wait:
                pend[direction] = None
                
        c = comp["cross"].iloc[i]
        if c == "Bull Cross":
            pend["buy"] = {
                "valid_i": i, 
                "level": float(df.iloc[i]["High"]), 
                "wt_val": float(comp["wt1"].iloc[i]) if not pd.isna(comp["wt1"].iloc[i]) else 0.0
            }
        elif c == "Bear Cross":
            pend["sell"] = {
                "valid_i": i, 
                "level": float(df.iloc[i]["Low"]), 
                "wt_val": float(comp["wt2"].iloc[i]) if not pd.isna(comp["wt2"].iloc[i]) else 0.0
            }
    return pend

def start_job(job_id, config):
    AUTO_JOBS[job_id] = {"stop": False, "config": config}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def stop_job(job_id):
    if job_id in AUTO_JOBS:
        AUTO_JOBS[job_id]["stop"] = True
    AUTO_STATE.clear()

def _worker(job_id):
    cfg = AUTO_JOBS[job_id]["config"]
    tasks = cfg.get("tasks", [])
    
    global IS_SCANNING, LAST_SCAN_TIME
    print(f"🚀 [AUTO] Started Job {job_id} with {len(tasks)} tasks")
    
    while not AUTO_JOBS[job_id]["stop"]:
        IS_SCANNING = True
        LAST_SCAN_TIME = time.time()
        open_positions = paper_trades.positions()
        # Key by symbol + timeframe so XAUUSD_1m and XAUUSD_1h are tracked separately
        pos_map = {f"{p['symbol']}_{p.get('tag', '')}": p for p in open_positions if p["qty"] != 0}

        for task in tasks:
            if AUTO_JOBS[job_id]["stop"]: break
            
            raw_sym = task["sym"]
            tf = task["tf"]
            alloc_amt = task["amt"]
            exch = task["exch"]
            
            # --- FIX: PROPER SYMBOL MAPPING ---
            sym_yf = raw_sym.upper()
            if exch == "NSE" and not sym_yf.endswith(".NS") and "." not in sym_yf and "=" not in sym_yf:
                sym_yf = sym_yf + ".NS"
            elif exch == "BSE" and not sym_yf.endswith(".BO") and "." not in sym_yf and "=" not in sym_yf:
                sym_yf = sym_yf + ".BO"
            elif exch == "FX":
                sym_yf = _SYMBOL_MAP.get(sym_yf, sym_yf)
            
            period = "3mo" if tf in ["1m","5m","15m","30m","1h"] else "1y"
            rows = download(sym_yf, interval=tf, period=period)
            if not rows or len(rows) < 50: continue
            
            df_real = candles_to_df(rows)
            df_real = _to_ist(df_real) # <--- THIS was missing. Converts UTC to IST properly.
            
            if cfg.get("fno_mode") in ("all", "sheet") and tf in ("5m","15m","30m","1h"):
                df_real = apply_fno_filter(df_real, timeframe=tf)
                
            df_ha = heikin_ashi(df_real) if cfg.get("candles") == "ha" else df_real
            
            try:
                mod = get_indicator("wt")
                params = {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)}
                comp = mod.compute(df_ha, params)
                
                # Find the absolute latest cross for UI display (ignores trigger status)
                buy_idx, sell_idx = -1, -1
                for i in range(len(df_ha) - 2, max(0, len(df_ha) - 100), -1): # Look back up to 100 bars
                    c = comp["cross"].iloc[i]
                    if c == "Bull Cross" and buy_idx == -1: buy_idx = i
                    if c == "Bear Cross" and sell_idx == -1: sell_idx = i
                    if buy_idx != -1 and sell_idx != -1: break

                buy_trig, buy_val, buy_time = 0.0, 0.0, ""
                if buy_idx != -1:
                    buy_trig = float(df_ha["High"].iloc[buy_idx])
                    buy_val = float(comp["wt1"].iloc[buy_idx]) if not pd.isna(comp["wt1"].iloc[buy_idx]) else 0.0
                    buy_time = str(df_ha.index[buy_idx]).split("+")[0].replace("T", " ")

                sell_trig, sell_val, sell_time = 0.0, 0.0, ""
                if sell_idx != -1:
                    sell_trig = float(df_ha["Low"].iloc[sell_idx])
                    sell_val = float(comp["wt2"].iloc[sell_idx]) if not pd.isna(comp["wt2"].iloc[sell_idx]) else 0.0
                    sell_time = str(df_ha.index[sell_idx]).split("+")[0].replace("T", " ")

                signal_color = ""
                if buy_idx != -1 and sell_idx != -1:
                    signal_color = "buy" if buy_idx > sell_idx else "sell"
                elif buy_idx != -1:
                    signal_color = "buy"
                elif sell_idx != -1:
                    signal_color = "sell"
                        
                ltp = float(df_real["Close"].iloc[-1])
                
                state_key = f"{raw_sym}_{tf}"
                
                # Check if the trigger happened on the LAST CLOSED candle for execution
                events = backtest.detect_wt_events(
                    df_ha, params, 
                    buffer_pts=cfg.get("buffer_pts", 0), 
                    pending_rule=cfg.get("wt_pending_rule", "same_dir")
                )
                AUTO_STATE[state_key] = {
                    "sym": raw_sym,
                    "tf": tf,
                    "ltp": round(ltp, 2),
                    "buy_trigger": round(buy_trig, 2) if buy_trig > 0 else None,
                    "sell_trigger": round(sell_trig, 2) if sell_trig > 0 else None,
                    "buy_time": buy_time,
                    "sell_time": sell_time,
                    "buy_val": round(buy_val, 2),
                    "sell_val": round(sell_val, 2),
                    "signal_color": signal_color,
                    "status": "In Position" if f"{raw_sym}_{tf}" in pos_map else "Scanning"
                }

                # Check if the trigger happened on the LAST CLOSED candle
                last_closed_idx = len(df_ha) - 2
                triggered_event = None
                for e in events:
                    if e["trigger_i"] == last_closed_idx:
                        triggered_event = e
                        break
                        
                if triggered_event:
                    direction = triggered_event["direction"]
                    price = triggered_event["trigger_level"]
                    
                    # --- ENFORCE SIDE MODE FROM UI SETTINGS ---
                    side_mode = cfg.get("side_mode", "both")
                    if side_mode == "long" and direction == "sell":
                        continue
                    if side_mode == "short" and direction == "buy":
                        continue
                    # -------------------------------------------

                    if f"{raw_sym}_{tf}" not in pos_map:
                        qty = int(alloc_amt / price)
                        if qty > 0:
                            paper_trades.place_order(
                                symbol=raw_sym, exchange=exch, side="BUY" if direction=="buy" else "SELL",
                                qty=qty, order_type="MARKET", price=None, product="MIS",
                                ltp=price, tag=tf
                            )
                            print(f"✅ [AUTO] Placed order for {raw_sym} on {tf}")
            except Exception as e:
                print(f"⚠️ [AUTO] Error on {raw_sym}_{tf}: {e}")

        IS_SCANNING = False
        # Sleep for the user-defined interval, checking for stop flag every second
        interval_mins = cfg.get("scan_interval", 1)
        for _ in range(interval_mins * 60):
            if AUTO_JOBS[job_id]["stop"]: break
            time.sleep(1)