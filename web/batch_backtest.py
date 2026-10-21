"""
Batch backtest — runs N symbols in a background thread, streams results
one-by-one via a queue. Server exposes it through SSE.
"""
import threading
import queue
import time
from backtest import run as run_one


JOBS = {}   # job_id -> {"q": Queue, "stop": {"stop": bool}, "results": []}


def _extract_summary(symbol, res):
    """Compress a full backtest result into a one-row summary."""
    if not res.get("ok"):
        return {
            "symbol": symbol, "ok": False,
            "error": res.get("error", "unknown"),
            "return_pct": 0, "trades": 0, "win_rate": 0,
            "profit_factor": 0, "max_dd_pct": 0, "net_pnl": 0,
            "max_win": 0, "max_loss": 0, "sharpe": 0,
            "buy_hold_pct": 0, "capital": 0, "final_equity": 0,
            "best_trade": None, "worst_trade": None,
        }
    m = res["metrics"]
    trades = res["trades"] or []
    best  = max(trades, key=lambda t: t["pnl"], default=None)
    worst = min(trades, key=lambda t: t["pnl"], default=None)
    return {
        "symbol": symbol, "ok": True,
        "error": "",
        "return_pct": m["total_return_pct"],
        "buy_hold_pct": m["buy_hold_pct"],
        "trades": m["trades"],
        "win_rate": m["win_rate"],
        "profit_factor": m["profit_factor"],
        "max_dd_pct": m["max_dd_pct"],
        "net_pnl": m["net_pnl"],
        "max_win": m["max_win"],
        "max_loss": m["max_loss"],
        "sharpe": m["sharpe"],
        "capital": m["capital"],
        "final_equity": m["final_equity"],
        "best_trade":  {"side": best["side"],  "pnl": best["pnl"]}  if best  else None,
        "worst_trade": {"side": worst["side"], "pnl": worst["pnl"]} if worst else None,
    }


def start_job(job_id, symbols, settings):
    q = queue.Queue()
    JOBS[job_id] = {"q": q, "stop": {"stop": False}, "results": []}
    t = threading.Thread(target=_worker,
                         args=(job_id, symbols, settings),
                         daemon=True)
    t.start()
    return JOBS[job_id]


def stop_job(job_id):
    if job_id in JOBS:
        JOBS[job_id]["stop"]["stop"] = True
        return True
    return False


def _worker(job_id, symbols, settings):
    j = JOBS[job_id]
    q = j["q"]

    for i, sym in enumerate(symbols):
        if j["stop"]["stop"]:
            q.put({"type": "stopped", "processed": i, "total": len(symbols)})
            return

        t0 = time.time()
        try:
            res = run_one(symbol=sym, **settings)
            summary = _extract_summary(sym, res)
        except Exception as e:
            summary = _extract_summary(sym, {"ok": False, "error": str(e)})

        summary["i"]   = i
        summary["ms"]  = int((time.time() - t0) * 1000)
        j["results"].append(summary)
        q.put({"type": "row", "row": summary, "i": i, "total": len(symbols)})

    q.put({"type": "done", "count": len(j["results"]), "total": len(symbols)})