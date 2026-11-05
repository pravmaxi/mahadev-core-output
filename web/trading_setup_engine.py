import threading
import time
import json
from pathlib import Path
import pandas as pd
import pytz
import yfinance as yf
import re

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

def start_job(job_id, config):
    SETUP_STATE.clear()
    TRIGGER_STATE.clear() 
    if SETUP_STATE_FILE.exists():
        try: SETUP_STATE.update(json.loads(SETUP_STATE_FILE.read_text()))
        except: pass
        
    SETUP_JOBS[job_id] = {"stop": False, "config": config, "is_first_scan": True}
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True)
    t.start()
    return job_id

def stop_job(job_id):
    if job_id in SETUP_JOBS:
        SETUP_JOBS[job_id]["stop"] = True

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
            SETUP_IS_SCANNING = True
            
            cfg = SETUP_JOBS[job_id]["config"]
            tasks = cfg.get("tasks", [])
            
            # Build sym_map FIRST
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

            # Remove symbols from UI state
            current_keys = {f"{t['sym']}_{t['tf']}" for t in tasks}
            for k in list(SETUP_STATE.keys()):
                if k not in current_keys:
                    del SETUP_STATE[k]
            
            # Purge TRIGGER_STATE
            current_yf_syms = {sym_map[k] for k in current_keys if k in sym_map}
            for k in list(TRIGGER_STATE.keys()):
                sym_yf = k.split('|')[0]
                if sym_yf not in current_yf_syms:
                    del TRIGGER_STATE[k]
            
            # Group by TF
            tf_groups = {}
            for t in tasks:
                tf = t["tf"]
                key = f"{t['sym']}_{tf}"
                tf_groups.setdefault(tf, []).append(sym_map[key])

            # F&O FILTER SETUP
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
            
            for tf, syms in tf_groups.items():
                if tf in ["1m", "2m", "5m", "15m", "30m"]: period = "60d"
                elif tf in ["60m", "90m", "1h"]: period = "3mo"
                else: period = "1y"
                
                try:
                    res = download_batch(syms, interval=tf, period=period, force_refresh=True)
                except Exception as e:
                    print(f"Download failed for {tf}: {e}")
                    continue

                # FETCH TRUE LIVE LTP (FORCE OVERRIDE FOR FOREX)
                true_ltps = {}
                try:
                    true_ltps = get_live_ltp(list(set(syms)))
                except Exception:
                    pass
                
                for sym_yf in syms:
                    if "=X" in sym_yf:
                        try:
                            ticker = yf.Ticker(sym_yf)
                            hist = ticker.history(period="1d", interval="1m")
                            if not hist.empty:
                                true_ltps[sym_yf] = float(hist["Close"].iloc[-1])
                        except Exception:
                            pass

                for task in tasks:
                    if task["tf"] != tf: continue
                    
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
                        
                        # FORCE OVERRIDE WITH TRUE LTP
                        if true_ltp is not None:
                            live_px = true_ltp
                            df_real.iloc[-1, df_real.columns.get_loc("Close")] = live_px
                            df_real.iloc[-1, df_real.columns.get_loc("High")] = max(live_px, df_real["High"].iloc[-1])
                            df_real.iloc[-1, df_real.columns.get_loc("Low")] = min(live_px, df_real["Low"].iloc[-1])
                        else:
                            live_px = float(df_real["Close"].iloc[-1])
                            
                        df_ha = heikin_ashi(df_real) if use_ha else df_real
                        
                        def _r(val): return round(val, 5) if abs(val) < 10 else round(val, 2)
                        
                        row = {"symbol": raw_sym, "tf": tf, "ltp": _r(live_px), "yf_symbol": sym_yf}
                        wt_params = {"n1": cfg.get("wt_n1", 15), "n2": cfg.get("wt_n2", 27)}
                        
                        # 1. Generate Scanner WT Action exactly as before
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
                                
                        # 2. Calculate Trigger Level using Autotrader logic
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
                            # Format to 5 decimals for Forex (< 10), else 2
                            row["trigger_lvl"] = f"{tl:.5f}" if abs(tl) < 10 else f"{tl:.2f}"
                            
                            # ✅ FIX: Force the string to say "Market Order" if LTP has already broken out
                            if direction == "sell" and live_px < tl:
                                row["wt_action"] = f"Sell Market Order"
                            elif direction == "buy" and live_px > tl:
                                row["wt_action"] = f"Buy Market Order"
                            else:
                                # If not broken out, keep the Limit Order string but update the price
                                if "wt_action" in row and row["wt_action"]:
                                    try:
                                        row["wt_action"] = re.sub(r'\d+\.\d+', row["trigger_lvl"], row["wt_action"])
                                    except Exception:
                                        pass
                        else:
                            row["trigger_lvl"] = "—"

                        # ✅ NEW: Calculate VALID status for execution safety
                        action_str = str(row.get("wt_action", ""))
                        row["valid"] = "—"
                        max_gap_pct = float(cfg.get("max_gap_pct", 2.0))
                        
                        if "Limit" in action_str:
                            row["valid"] = "YES"
                        elif "Market" in action_str and row.get("trigger_lvl") and row["trigger_lvl"] != "—":
                            try:
                                tl = float(row["trigger_lvl"])
                                if tl > 0:
                                    gap_pct = abs(live_px - tl) / tl * 100
                                    if gap_pct <= max_gap_pct:
                                        row["valid"] = "YES"
                                    else:
                                        row["valid"] = f"NO (Gap {gap_pct:.1f}%)"
                            except Exception:
                                pass

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

        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
        finally:
            SETUP_IS_SCANNING = False
            SETUP_LAST_SCAN_TIME = time.time()
            
            try: SETUP_STATE_FILE.write_text(json.dumps(SETUP_STATE, indent=2))
            except: pass
            
        if SETUP_FORCE_SCAN:
            SETUP_FORCE_SCAN = False
            continue
            
        for _ in range(cfg.get("scan_interval", 1) * 60):
            if SETUP_JOBS[job_id]["stop"] or SETUP_FORCE_SCAN: break
            time.sleep(1)