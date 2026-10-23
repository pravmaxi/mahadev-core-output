"""
Auto-Test Engine — generates combinations and runs them in the background.
"""
import time
import queue
import threading
import itertools
import backtest

JOBS = {}

# This maps the setting ID to the list of values it will test
OPTION_MAP = {
    "strategy_mode": ["simple", "same_side", "alternate"],
    "side_mode": ["both", "long", "short"],
    "heikin_ashi": [True, False],
    "wt_pending_rule": ["every", "same_dir", "any_dir"],
    "st_entry_mode": ["strict", "loose"],
    "st_exit_mode": ["strict", "loose"],
    "st_reentry_mode": ["standard", "one_per_opposite", "fresh_r"],
    "recovery": ["off", "last", "cumulative"],
    "trailing_enabled": [True, False],
    "exit_on_opposite": [True, False],
    "wt_filter_enabled": [True, False],
    "htf_filter_enabled": [True, False],
    "rsi_filter_enabled": [True, False],
    "vol_filter_enabled": [True, False],
}

def start_job(job_id, base_cfg, vary_keys):
    q = queue.Queue()
    JOBS[job_id] = {"q": q, "stop": False, "results": []}
    
    def worker():
        # 1. Build the options for each varied key
        options = {k: OPTION_MAP[k] for k in vary_keys if k in OPTION_MAP}
                
        # 2. Generate all combinations
        keys = list(options.keys())
        values = list(options.values())
        combos = list(itertools.product(*values))
        total = len(combos)
        
        JOBS[job_id]["total"] = total
        
        # 3. Loop through combinations
        for i, combo in enumerate(combos):
            if JOBS[job_id]["stop"]:
                q.put({"type": "stopped", "processed": i})
                break
                
            # Build specific config for this run
            run_cfg = dict(base_cfg)
            combo_dict = dict(zip(keys, combo))
            
            for k, v in combo_dict.items():
                if k == "recovery":
                    run_cfg["recovery_enabled"] = (v != "off")
                    run_cfg["recovery_cumulative"] = (v == "cumulative")
                else:
                    run_cfg[k] = v
            
            # Run it
            try:
                res = backtest.run(
                    symbol=run_cfg.get("symbol"),
                    interval=run_cfg.get("interval"),
                    period=run_cfg.get("period"),
                    indicators=run_cfg.get("indicators"),
                    params=run_cfg.get("params"),
                    heikin_ashi_flag=run_cfg.get("heikin_ashi"),
                    strategy_mode=run_cfg.get("strategy_mode"),
                    side_mode=run_cfg.get("side_mode"),
                    wt_pending_rule=run_cfg.get("wt_pending_rule", "every"),
                    st_entry_mode=run_cfg.get("st_entry_mode", "strict"),
                    st_exit_mode=run_cfg.get("st_exit_mode", "strict"),
                    st_reentry_mode=run_cfg.get("st_reentry_mode", "standard"),
                    recovery_enabled=run_cfg.get("recovery_enabled", False),
                    recovery_cumulative=run_cfg.get("recovery_cumulative", False),
                    trailing_enabled=run_cfg.get("trailing_enabled", False),
                    exit_on_opposite=run_cfg.get("exit_on_opposite", True),
                    wt_filter_enabled=run_cfg.get("wt_filter_enabled", False),
                    htf_filter_enabled=run_cfg.get("htf_filter_enabled", False),
                    rsi_filter_enabled=run_cfg.get("rsi_filter_enabled", False),
                    vol_filter_enabled=run_cfg.get("vol_filter_enabled", False),
                    # Defaults for everything else
                    buffer_pts=run_cfg.get("buffer_pts", 1),
                    profit_target_pct=run_cfg.get("profit_target_pct", 1),
                    stop_loss_pct=run_cfg.get("stop_loss_pct", 0),
                    wait_bars=run_cfg.get("wait_bars", 20),
                    fno_mode=run_cfg.get("fno_mode", "none"),
                    fno_list=run_cfg.get("fno_list"),
                )
                
                if res.get("ok"):
                    m = res["metrics"]
                    # Make settings readable for the UI
                    readable_settings = {}
                    for k, v in combo_dict.items():
                        if k == "recovery":
                            readable_settings["Recovery"] = v
                        elif k == "strategy_mode":
                            readable_settings["Strategy"] = v
                        elif k == "side_mode":
                            readable_settings["Side"] = v
                        elif k == "heikin_ashi":
                            readable_settings["HA"] = "Yes" if v else "No"
                        elif k == "wt_pending_rule":
                            readable_settings["WT Rule"] = v
                        elif k == "st_entry_mode":
                            readable_settings["ST Entry"] = v
                        elif k == "st_exit_mode":
                            readable_settings["ST Exit"] = v
                        elif k == "st_reentry_mode":
                            readable_settings["ST Re-entry"] = v
                        else:
                            readable_settings[k.replace("_enabled", "").replace("_", " ").title()] = "Yes" if v else "No"

                    row = {
                        "combo_id": i + 1,
                        "settings": readable_settings,
                        "return_pct": m["total_return_pct"],
                        "max_dd_pct": m["max_dd_pct"],
                        "trades": m["trades"],
                        "win_rate": m["win_rate"],
                        "profit_factor": m["profit_factor"],
                        "sharpe": m["sharpe"],
                        "net_pnl": m["net_pnl"],
                    }
                    JOBS[job_id]["results"].append(row)
                    q.put({"type": "row", "row": row, "current": i+1, "total": total})
                else:
                    q.put({"type": "error", "msg": res.get("error", "Unknown"), "current": i+1})
            except Exception as e:
                q.put({"type": "error", "msg": str(e), "current": i+1})

        q.put({"type": "done", "count": len(JOBS[job_id]["results"])})

    threading.Thread(target=worker, daemon=True).start()
    return job_id

def stop_job(job_id):
    if job_id in JOBS:
        JOBS[job_id]["stop"] = True
        return True
    return False