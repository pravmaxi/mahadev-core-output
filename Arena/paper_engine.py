"""
Live Paper-Trading Engine — runs `backtest.py`'s OWN logic on live candles.

WHAT THIS IS
------------
Backtest page:  download OHLC -> WaveTrend -> entry/exit rules -> trades.
This module:    poll OHLC  -> SAME WaveTrend -> SAME entry/exit rules -> paper trades.

It does not re-implement the strategy. It imports and calls backtest's own
private helpers, so the rules cannot drift:

    backtest.detect_wt_events(...)   -> signal + trigger level (buffer, wait_bars, pending rule)
    backtest._apply_filters(...)     -> WT-extreme / HTF / RSI / volume gates
    backtest._qty_for(...)           -> integer quantity from Alloted Amount
    backtest._target_px(...)         -> profit target (% or amount)
    backtest._trailing_sl_price(...) -> trailing stop

Exits are evaluated in the same order as `_simulate_simple`:
    stop  ->  trailing stop  ->  target  ->  opposite signal

Only two things differ from a backtest, and both are unavoidable live:
  * exits are checked on the live LTP, not on the bar's High/Low (no ticks);
  * the entry is taken on the tick AFTER the breakout bar closed.
Both are labelled in the UI.

NO SHARED STATE
---------------
Writes only its own files:
    data/paper_live_state.json    (open positions)
    data/paper_live_trades.json   (closed trades)
It never touches paper_trades.json, setup_state_cache.json, order_queue.json,
SETUP_STATE or any Auto-Trader global.

HOW TO RUN
----------
    A) standalone:      cd web && python paper_live_engine.py
                        -> http://localhost:8020/paper-live
    B) inside server.py (2 additive lines):
           from paper_live_engine import paper_live_bp
           app.register_blueprint(paper_live_bp)
                        -> http://localhost:8000/paper-live

ROUTES
------
    GET  /paper-live                     the page
    POST /api/paper-live/start           {config} start polling
    POST /api/paper-live/stop            stop polling
    GET  /api/paper-live/status          live rows + trades + job info
    POST /api/paper-live/scan            force one scan now
    POST /api/paper-live/reset           clear positions + trade log
    POST /api/paper-live/replay          historical replay of THIS engine
    POST /api/paper-live/verify          replay vs backtest.run()  <- the proof
"""

import copy
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import pytz
from flask import Blueprint, jsonify, redirect, render_template, request

paper_live_bp = Blueprint("paper_live", __name__)

IST = pytz.timezone("Asia/Kolkata")
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
# Paper Live (default) or Live Trade (`python paper_live_engine.py --live`): the same engine,
# run as a second server with its own settings / symbols / positions / trades files.
PROFILE = "paper"
_FP = "live_trade" if PROFILE == "live" else "paper_live"
STATE_FILE = DATA_DIR / ("%s_state.json" % _FP)       # open positions + cash
TRADE_FILE = DATA_DIR / ("%s_trades.json" % _FP)      # closed trades
PRESET_FILE = DATA_DIR / "paper_live_presets.json"   # named symbol lists (shared by both pages)
ALLOC_FILE = DATA_DIR / ("%s_allocations.json" % _FP)  # the CURRENT working set (like Trade Setup)
SHEET_FILE = DATA_DIR / ("%s_sheet_out.json" % _FP)   # Google Sheet output: settings + queue
ORDERS_CSV = DATA_DIR / ("%s_orders.csv" % _FP)       # local copy of every row sent to the sheet

STATE = {}          # symbol -> row (state + display)
TRADES = []         # closed trades, newest last
JOBS = {}           # job_id -> {"stop": bool, ...}
RUN_LOCK = threading.Lock()
_FULL_SCAN_LOCK = threading.Lock()   # one full scan per profile
_WORK_GATE = threading.Lock()        # scan and LTP never process simultaneously
_STOP_EPOCH = 0                     # invalidates in-flight work, including manual requests
DATA_LOCK = threading.Lock()          # yfinance is not thread-safe: one download at a time
_LTP_WAITING = threading.Event()      # an LTP poll is queued for DATA_LOCK -> the scan yields
_LTP_RUN = threading.Lock()           # one LTP poll at a time (no pile-up behind a slow scan)
_LTP_WAITS = []                       # [(epoch, seconds an LTP poll waited)] — last 10 min
LAST_SCAN = 0.0
LAST_ERR = ""
RUNTIME = {"running": False, "job_id": None, "config": {}, "next_scan": 0, "cycles": 0,
           "last_error": "", "started": 0, "stopping": False, "aborted": False,
           "scanning": False, "scan_started": 0.0, "scan_ms": 0, "scan_n": 0,
           "last_scan_at": "", "last_scan_kind": ""}


# ── defaults ───────────────────────────────────────────────────────────────
DEFAULTS = {
    "symbols": "HAL, RELIANCE",
    "verify_symbol": "",
    "exchange": ".NS",
    "symbol_exch": {},
    # IN | US | FX | CRYPTO — decides the ticker suffix AND which clock says "open"
    "market": "IN",
    "interval": "15m",
    "period": "1mo",
    "wt_n1": 15.0,
    "wt_n2": 27.0,
    "wt_pending_rule": "same_dir",
    "buffer_pts": 0.0,
    "wait_bars": 20,
    "profit_target_pct": 1.0,
    "profit_target_mode": "pct",   # pct | amount
    "stop_loss_pct": 0.5,
    "trailing_enabled": False,
    "trailing_start_pct": 1.0,
    "trailing_step_pct": 0.5,
    "trailing_tighten_enabled": False,
    "trailing_tighten_at": 9.0,
    "trailing_tighten_step": 1.0,
    "side_mode": "both",
    "heikin_ashi": True,
    # live-only: "level" = fill at the breakout level (a resting stop order — what
    # the backtest assumes) | "market" = if the price is already past the level when
    # the scan sees it, fill at the live price instead (manual "buy now" behaviour)
    "entry_exec": "level",
    "allow_already_triggered": False,
    "already_triggered_max_pct": 1.0,
    "alloc": [],                   # [{symbol, amount, capital}] — per-symbol override from the table
    "sizing_mode": "fixed",        # fixed = ₹ per trade | qty = fixed quantity | pct = % of capital
    "position_amount": 5000.0,
    "fixed_qty": 1,
    "position_pct": 100.0,
    "capital": 100000.0,
    "commission_pct": 0.05,
    "slippage_pct": 0.02,
    "exit_on_opposite": True,
    "fno_mode": "all",              # none | all | sheet  (same as the Backtest page)
    "fno_book": "Backtest scanner",
    "fno_tab": "F&O_input",
    "wt_filter_enabled": False,
    "wt_buy_max": -40.0,
    "wt_sell_min": 40.0,
    "htf_filter_enabled": False,
    "rsi_filter_enabled": False,
    "rsi_period": 14,
    "vol_filter_enabled": False,
    "vol_mult": 1.5,
    "vol_lookback": 20,
    "poll_seconds": 0,          # 0 = auto from timeframe
    # keep scanning after the exchange closes (paper only: nothing can fill on stale
    # bars unless allow_stale_entries is also ticked, but signals stay visible)
    "poll_when_market_closed": True,
    "auto_poll": True,          # False = scan once, then wait for "Scan now"/Apply
    "allow_stale_entries": False,   # refuse entries on a stale last bar
}

# poll cadence per timeframe (seconds) when poll_seconds == 0
POLL_FOR_TF = {"1m": 30, "2m": 45, "5m": 60, "15m": 90, "30m": 150,
               "60m": 240, "1h": 240, "1d": 900, "1wk": 3600}

# bar length in minutes, used to detect stale prices before entering
TF_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
              "60m": 60, "1h": 60, "4h": 240, "1d": 1440, "5d": 7200, "1wk": 10080}


# ── HTF trend cache (per symbol, 30 min) ───────────────────────────────────
_HTF_CACHE = {"ts": {}, "df": {}}


def htf_for(sym, tf, ttl=1800):
    """Higher-timeframe EMA frame for one symbol (cached, never raises)."""
    key = (sym, tf)
    now = time.time()
    if key in _HTF_CACHE["df"] and (now - _HTF_CACHE["ts"].get(key, 0)) < ttl:
        return _HTF_CACHE["df"][key]
    try:
        df = _bt()._load_htf_trend(sym, tf)
    except Exception as e:
        print("⚠️ HTF failed for %s: %s" % (sym, e))
        df = None
    _HTF_CACHE["df"][key] = df
    _HTF_CACHE["ts"][key] = now
    return df


# ── F&O 15:15 filter (identical rule to backtest.run) ──────────────────────
FNO_TFS = ("5m", "15m", "30m", "1h")
_FNO_CACHE = {"key": None, "ts": 0.0, "set": set(), "err": ""}


def _fno_list(cfg, ttl=600):
    """Symbols listed in the F&O sheet tab (cached 10 min)."""
    key = (cfg.get("fno_book"), cfg.get("fno_tab"))
    now = time.time()
    if _FNO_CACHE["key"] == key and (now - _FNO_CACHE["ts"]) < ttl:
        return _FNO_CACHE["set"]
    try:
        import sheets
        raw = sheets.read_symbols(str(cfg.get("fno_book") or "Backtest scanner"),
                                 str(cfg.get("fno_tab") or "F&O_input")) or []
        vals = set(str(x).strip().upper() for x in raw if str(x).strip())
        _FNO_CACHE.update({"key": key, "ts": now, "set": vals,
                           "err": "" if vals else "F&O sheet is empty or not readable"})
        return vals
    except Exception as e:
        _FNO_CACHE.update({"key": key, "ts": now, "set": set(), "err": str(e)})
        return set()


def fno_should_apply(cfg, sym):
    """Same decision the backtest makes, including its timeframe restriction."""
    mode = str(cfg.get("fno_mode") or "none").lower()
    tf = str(cfg.get("interval") or "")
    if tf not in FNO_TFS or mode == "none":
        return False
    if mode == "all":
        return True
    if mode == "sheet":
        lst = _fno_list(cfg)
        if not lst:
            return False
        s = (sym or "").upper()
        return s in lst or s.replace(".NS", "").replace(".BO", "") in lst
    return False


def apply_fno(df, cfg, sym):
    """Drop 15:15+ candles for intraday F&O symbols (no-op when not selected)."""
    if df is None or len(df) == 0:
        return df
    if not fno_should_apply(cfg, sym):
        return df
    try:
        from indicators.common import apply_fno_filter
        return apply_fno_filter(df, str(cfg.get("interval") or "15m"))
    except Exception as e:
        print("⚠️ F&O filter failed:", e)
        return df


# ── lazy imports (so the page renders even if a dep is missing) ────────────
def _bt():
    import backtest
    return backtest


def _common():
    from indicators.common import candles_to_df, heikin_ashi
    return candles_to_df, heikin_ashi


def _wt():
    from indicators import get as get_indicator
    return get_indicator("wt")


def _data():
    from data import download_batch, get_live_ltp
    return download_batch, get_live_ltp


# ── fast, bit-identical copies of two slow indicator loops ─────────────────
# indicators.common.heikin_ashi writes one pandas cell per candle, and
# wavetrend._compute_wavetrend reads / writes one cell per candle for its cross
# column. On a month of 5m candles that was ~1 s per symbol (~4 s for 24h FX)
# — most of a full scan. The copies below do the SAME float arithmetic in the
# same order on plain arrays, so every number is bit-identical. _fast_math_ok()
# proves it on a sample before first use and falls back to the originals if
# they ever differ (e.g. after someone edits the indicator files).
FAST_MATH = {"checked": False, "ha": False, "wt": False, "why": ""}
_FAST_MATH_LOCK = threading.Lock()
_OHLC = ("Open", "High", "Low", "Close")


def _ha_arrays(df):
    """indicators.common.heikin_ashi, with the HA-Open recursion on a list."""
    import pandas as pd
    ha = pd.DataFrame(index=df.index)
    ha["Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4.0
    close = ha["Close"].tolist()
    op = [0.0] * len(close)
    op[0] = float((df["Open"].iloc[0] + df["Close"].iloc[0]) / 2.0)
    for i in range(1, len(close)):
        op[i] = (op[i - 1] + close[i - 1]) / 2.0
    ha["Open"] = op
    ha["High"] = pd.concat([df["High"], ha["Open"], ha["Close"]], axis=1).max(axis=1)
    ha["Low"] = pd.concat([df["Low"], ha["Open"], ha["Close"]], axis=1).min(axis=1)
    return ha[["Open", "High", "Low", "Close"]]


def _wt_arrays(df, params):
    """wavetrend.compute(df, params), with the cross loop done on whole arrays."""
    import numpy as np
    import pandas as pd
    mod = _wt()
    n1 = int(params.get("n1", mod.META["defaults"]["n1"]))
    n2 = int(params.get("n2", mod.META["defaults"]["n2"]))
    ap = (df["High"] + df["Low"] + df["Close"]) / 3
    esa = ap.ewm(span=n1, adjust=False).mean()
    d = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
    ci = (ap - esa) / (0.015 * d)
    tci = ci.ewm(span=n2, adjust=False).mean()
    wt1 = tci
    wt2 = wt1.rolling(window=4).mean()
    wt_diff = wt1 - wt2
    a = wt1.to_numpy(dtype="float64")
    b = wt2.to_numpy(dtype="float64")
    lab = np.full(len(a), "No Cross", dtype=object)
    if len(a) > 1:
        with np.errstate(invalid="ignore"):
            bull = (a[:-1] < b[:-1]) & (a[1:] > b[1:])
            bear = (a[:-1] > b[:-1]) & (a[1:] < b[1:])
        body = lab[1:]                    # a view: writing it writes lab
        body[bear] = "Bear Cross"
        body[bull] = "Bull Cross"
    cross = pd.Series(lab, index=df.index, dtype="object")
    rsi = mod._rsi(df["Close"], 14)
    return {"wt1": wt1, "wt2": wt2, "wt_diff": wt_diff, "cross": cross, "rsi": rsi}


def _fast_math_ok():
    """Prove once per process that the fast copies equal the originals exactly."""
    if FAST_MATH["checked"]:
        return FAST_MATH
    with _FAST_MATH_LOCK:
        if FAST_MATH["checked"]:
            return FAST_MATH
        why = []
        try:
            import numpy as np
            import pandas as pd
            rng = np.random.default_rng(7)
            n = 400
            close = 100 + np.cumsum(rng.normal(0, 0.6, n))
            open_ = close + rng.normal(0, 0.3, n)
            high = np.maximum(open_, close) + rng.random(n) * 0.5
            low = np.minimum(open_, close) - rng.random(n) * 0.5
            idx = pd.date_range("2026-01-05 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
            df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                               "Volume": 1000.0}, index=idx)
            slow_ha = _common()[1](df)
            FAST_MATH["ha"] = bool(_ha_arrays(df).equals(slow_ha))
            if not FAST_MATH["ha"]:
                why.append("Heikin-Ashi")
            mod = _wt()
            same = hasattr(mod, "_rsi")
            for frame in (df, slow_ha):
                if not same:
                    break
                slow = mod.compute(frame, {"n1": 15.0, "n2": 27.0})
                fast = _wt_arrays(frame, {"n1": 15.0, "n2": 27.0})
                same = all(fast[k].equals(slow[k]) for k in ("wt1", "wt2", "wt_diff",
                                                             "cross", "rsi"))
            FAST_MATH["wt"] = bool(same)
            if not same:
                why.append("WaveTrend")
        except Exception as e:                       # never break a scan over this
            FAST_MATH["ha"] = FAST_MATH["wt"] = False
            why.append("self-test crashed: %s: %s" % (type(e).__name__, e))
        FAST_MATH["why"] = ", ".join(why)
        FAST_MATH["checked"] = True
        return FAST_MATH


def _heikin_ashi(df):
    """indicators.common.heikin_ashi — the fast copy when it is proven identical."""
    if (len(df) and not df.index.has_duplicates and _fast_math_ok()["ha"]
            and all(str(df[c].dtype) == "float64" for c in _OHLC)):
        return _ha_arrays(df)
    return _common()[1](df)


def _wt_compute(df, cfg):
    """wavetrend.compute(df, {n1, n2}) — the fast copy when it is proven identical."""
    params = {"n1": _num(cfg.get("wt_n1"), 15), "n2": _num(cfg.get("wt_n2"), 27)}
    if (len(df) and _fast_math_ok()["wt"]
            and all(str(df[c].dtype) == "float64" for c in ("High", "Low", "Close"))):
        return _wt_arrays(df, params)
    return _wt().compute(df, params)


# ── small utils ────────────────────────────────────────────────────────────
def _num(v, d=0.0):
    try:
        if v is None or v == "":
            return float(d)
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def _int(v, d=0):
    try:
        if v is None or v == "":
            return int(d)
        return int(float(v))
    except (TypeError, ValueError):
        return int(d)


def _f(v):
    """float or None (for NaN-safe display of indicator values)."""
    try:
        x = float(v)
        return None if x != x else round(x, 2)
    except (TypeError, ValueError):
        return None


def _bool(v, d=False):
    if v is None:
        return bool(d)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return bool(d)


def norm_symbol(raw, exchange):
    s = (raw or "").strip().upper()
    if not s:
        return ""
    if s.endswith("_FX"):
        return s[:-3] + "=X"
    if "." in s or s.startswith("^") or s.endswith("=X") or "=" in s:
        return s
    ex = (exchange or "").strip()
    if ex == "_FX":
        return s + "=X"
    if ex == "_CRYPTO":
        return s if s.endswith("-USD") else s + "-USD"
    if ex == "":
        return s
    return s + ex


_EXCH_MAP = {"NSE": ".NS", "BSE": ".BO", "US": "", "FX": "_FX", "CRYPTO": "_CRYPTO"}

def parse_symbols(raw, exchange, sym_exch=None):
    sym_exch = sym_exch if isinstance(sym_exch, dict) else {}
    out, seen = [], set()
    if isinstance(raw, (list, tuple, set)):          # JSON arrays are accepted too
        raw = ",".join(str(x) for x in raw)
    for tok in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        _t = str(tok or '').strip()
        _ex = _EXCH_MAP.get(sym_exch.get(_t) or sym_exch.get(_t.upper()), exchange)
        s = norm_symbol(tok, _ex)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def cfg_of(body):
    cfg = dict(DEFAULTS)
    for k, v in (body or {}).items():
        if v is not None and k in cfg:
            cfg[k] = v
    return cfg


def _presets():
    try:
        return json.loads(PRESET_FILE.read_text()) or {}
    except Exception:
        return {}


def _save_presets(d):
    try:
        PRESET_FILE.write_text(json.dumps(d, indent=2))
        return True
    except Exception as e:
        print("⚠️ preset save failed:", e)
        return False


def _clean_sym(s):
    return str(s or "").strip().upper().replace(".NS", "").replace(".BO", "").replace("_FX", "")


def _alloc_maps(cfg):
    """{CLEAN_SYMBOL: amount} and {CLEAN_SYMBOL: capital} from the symbol table."""
    amt, cap = {}, {}
    for row in (cfg.get("alloc") or []):
        if not isinstance(row, dict):
            continue
        k = _clean_sym(row.get("symbol"))
        if not k:
            continue
        a = _num(row.get("amount"), 0.0)
        c = _num(row.get("capital"), 0.0)
        if a > 0:
            amt[k] = a
        if c > 0:
            cap[k] = c
    return amt, cap


def _inr(v):
    """₹1,00,000 — Indian digit grouping, same as the page shows."""
    n = int(round(_num(v, 0)))
    raw = str(abs(n))
    if len(raw) > 3:
        head, tail = raw[:-3], raw[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        raw = ",".join(groups) + "," + tail
    return "₹" + raw


def sizing_label(cfg):
    """One-line, human answer to 'which number decides the quantity?'."""
    mode = str(cfg.get("sizing_mode") or "fixed")
    if mode == "qty":
        n = _int(cfg.get("fixed_qty"), 1)
        return "%d share%s (fixed quantity)" % (n, "" if n == 1 else "s")
    if mode == "pct":
        pct = _num(cfg.get("position_pct"), 100.0)
        cap = _num(cfg.get("capital"), 100000.0)
        return "%s%% of capital = %s per trade" % (("%g" % pct), _inr(cap * pct / 100.0))
    return "%s per trade" % _inr(_num(cfg.get("position_amount"), 5000.0))


def _make_C(cfg):
    """The exact dict backtest's helpers expect."""
    return {
        "interval": str(cfg.get("interval") or "15m"),     # recorded on positions (TF)
        "capital": _num(cfg.get("capital"), 100000.0),
        "position_pct": _num(cfg.get("position_pct"), 100.0),
        "position_amount": _num(cfg.get("position_amount"), 5000.0),
        "fixed_qty": _int(cfg.get("fixed_qty"), 1),
        "sizing_mode": str(cfg.get("sizing_mode") or "fixed"),
        "entry_exec": str(cfg.get("entry_exec") or "level"),
        "buffer_pts": _num(cfg.get("buffer_pts"), 0.0),
        "commission_pct": _num(cfg.get("commission_pct"), 0.05),
        "slippage_pct": _num(cfg.get("slippage_pct"), 0.02),
        "profit_target_pct": _num(cfg.get("profit_target_pct"), 0.0),
        "profit_target_mode": str(cfg.get("profit_target_mode") or "pct"),
        "profit_target_value": _num(cfg.get("profit_target_pct"), 0.0),
        "stop_loss_pct": _num(cfg.get("stop_loss_pct"), 0.0),
        "exit_on_opposite": _bool(cfg.get("exit_on_opposite"), True),
        "side_mode": str(cfg.get("side_mode") or "both"),
        "multiplier_type": "martingale", "base_qty": 1, "max_levels": 1,
        "st_reentry_mode": "standard",
        "recovery_enabled": False, "recovery_cumulative": False,
        "recovery_buffer_amount": 0.0, "recovery_trail_enabled": False,
        "recovery_trail_premium": 0.0, "recovery_trail_step": 0.0,
        "trailing_enabled": _bool(cfg.get("trailing_enabled"), False),
        "trailing_start_pct": _num(cfg.get("trailing_start_pct"), 1.0),
        "trailing_step_pct": _num(cfg.get("trailing_step_pct"), 0.5),
        "trailing_tighten_enabled": _bool(cfg.get("trailing_tighten_enabled"), False),
        "trailing_tighten_at": _num(cfg.get("trailing_tighten_at"), 9.0),
        "trailing_tighten_step": _num(cfg.get("trailing_tighten_step"), 1.0),
        "wt_filter_enabled": _bool(cfg.get("wt_filter_enabled"), False),
        "wt_buy_max": _num(cfg.get("wt_buy_max"), -40.0),
        "wt_sell_min": _num(cfg.get("wt_sell_min"), 40.0),
        "htf_filter_enabled": _bool(cfg.get("htf_filter_enabled"), False),
        "rsi_filter_enabled": _bool(cfg.get("rsi_filter_enabled"), False),
        "rsi_period": _int(cfg.get("rsi_period"), 14),
        "vol_filter_enabled": _bool(cfg.get("vol_filter_enabled"), False),
        "vol_mult": _num(cfg.get("vol_mult"), 1.5),
        "vol_lookback": _int(cfg.get("vol_lookback"), 20),
    }


def _age_min(ts):
    """Minutes between a bar timestamp and now (IST). None if unknown."""
    if ts is None:
        return None
    try:
        import pandas as _pd
        t = _pd.Timestamp(ts)
        t = (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(IST)
        now = pytz.utc.localize(__import__("datetime").datetime.utcnow()).astimezone(IST)
        return int(max(0.0, (now - t).total_seconds()) // 60)
    except Exception:
        return None


def _ltp_from_1m(download_batch, symbols, stats=None, priority=False):
    """(prices, ages) from the 1-minute frame — the SAME source as data.get_live_ltp,
    but it also gives the timestamp of the price, so the page can show how fresh
    it is instead of silently using an old close.

    stats (dict) collects "wait" (seconds queued for DATA_LOCK) and "took".
    priority=True is the LTP poll: a full scan between candle chunks lets it go first.
    """
    px, age = {}, {}
    stats = {} if stats is None else stats
    t0 = time.time()
    if priority:
        _LTP_WAITING.set()
    try:
        with DATA_LOCK:
            if _stop_requested() or (priority and RUNTIME.get("scanning")):
                return px, age
            if priority:
                _LTP_WAITING.clear()
            t1 = time.time()
            m1 = download_batch(symbols, interval="1m", period="1d", force_refresh=True) or {}
        stats["wait"] = stats.get("wait", 0.0) + (t1 - t0)
        stats["took"] = stats.get("took", 0.0) + (time.time() - t1)
    except Exception as e:
        print("⚠️ paper_live 1m frame:", e)
        return px, age
    finally:
        if priority:
            _LTP_WAITING.clear()
    for s in symbols:
        bars = m1.get(s) or []
        if not bars:
            continue
        last = bars[-1]
        try:
            c = last.get("c") if isinstance(last, dict) else None
            if c is None:
                continue
            px[s] = float(c)
            age[s] = _age_min(last.get("t"))
        except Exception:
            continue
    return px, age


def _infer_slippage(fill, sign, slip_pct):
    return fill * (1 - sign * slip_pct / 100.0)


# ── position maths (identical to _simulate_simple) ─────────────────────────
def _open_pos(st, C, level, direction, buffer_pts, t, sig_id, ltp=None, kind=None):
    """Enter at (trigger level ± buffer) with slippage. Returns pos or None.

    entry_exec == "market": if the live price is already past the trigger, the
    fill is the live price (a human pressing BUY now), not the level.
    entry_exec == "market_now": the live price, whichever side of the level it
    is on — only "Allow already-triggered" uses it (a market order NOW).
    entry_exec == "level" (default): the level, i.e. a resting stop order that
    was already sitting at the level — this is what the backtest assumes.
    kind = the entry rule that opened it (ENTRY_KINDS); the logic check reads
    kind / fill / level / ltp_at_entry to verify the booked price.
    """
    bt = _bt()
    sign = 1 if direction == "buy" else -1
    px = float(level) + sign * float(buffer_pts)
    mode = str(C.get("entry_exec") or "level")
    if ltp is None or mode not in ("market", "market_now"):
        mode = "level"
    elif mode == "market_now":
        px = float(ltp)
    else:
        px = max(px, float(ltp)) if sign > 0 else min(px, float(ltp))
    fill = px * (1 + sign * C["slippage_pct"] / 100.0)
    qty = bt._qty_for(C, fill, st["cash"])
    if qty <= 0:
        return None
    notional = qty * fill
    comm = notional * C["commission_pct"] / 100.0
    st["cash"] += -sign * notional - comm
    pos = {"sign": sign, "qty": int(qty), "entry": fill, "entry_t": t,
           "entry_comm": comm, "best": fill, "sig_id": sig_id,
           "target": bt._target_px(C, fill, qty, sign),
           "stop": (fill * (1 - sign * C["stop_loss_pct"] / 100.0)
                    if C["stop_loss_pct"] > 0 else None),
           "kind": kind, "fill": mode, "level": float(level),
           "buffer": float(buffer_pts), "slip_pct": float(C["slippage_pct"]),
           "ltp_at_entry": float(ltp) if ltp is not None else None,
           "side_mode": str(C.get("side_mode") or "both"),
           "src": getattr(_TL, "src", None),
           "tf": C.get("interval")}                 # the timeframe the signal ran on
    st["pos"] = pos
    done = st.setdefault("sigs_traded", [])
    if sig_id and sig_id not in done:
        done.append(sig_id)
        del done[:-30]
    return pos


ENTRY_REC_KEYS = ("sign", "entry", "entry_t", "kind", "fill", "level", "buffer", "slip_pct",
                  "ltp_at_entry", "side_mode", "sig_id", "break_t", "why", "src", "tf")


def _close_pos(st, C, px, reason, t, bar_t=None, apply_slip=True):
    """Exit at px with slippage + commission. Returns the trade dict.

    apply_slip=False for the end-of-data close, because backtest._simulate_simple
    closes at the raw last Close with no slippage.
    """
    p = st["pos"]
    sign = p["sign"]
    fill = float(px) * (1 - sign * C["slippage_pct"] / 100.0) if apply_slip else float(px)
    comm = abs(p["qty"] * fill) * C["commission_pct"] / 100.0
    st["cash"] += sign * p["qty"] * fill - comm
    net = (fill - p["entry"]) * sign * p["qty"] - comm - p["entry_comm"]
    trade = {
        "symbol": st.get("symbol", ""),
        "side": "long" if sign > 0 else "short",
        "qty": p["qty"],
        "entry": p["entry"],
        "entry_t": p["entry_t"],
        "exit": fill,
        "exit_t": t,
        "bar_t": bar_t or t,
        "pnl": round(net, 2),
        "pnl_pct": round(net / (abs(p["entry"] * p["qty"]) or 1) * 100, 2),
        "costs": round(comm + p["entry_comm"], 2),
        "reason": reason,
        "entry_why": (p or {}).get("why", ""),
        # for the logic check's "Recent changes": what the exit rule asked for and
        # what the price was at that moment, plus how the entry was booked
        "sig_id": p.get("sig_id"),
        "rule_px": float(px),
        "slip_pct": float(C["slippage_pct"]) if apply_slip else 0.0,
        "ltp_at_exit": (float(st["ltp_raw"]) if isinstance(st.get("ltp_raw"), (int, float))
                        else None),
        "src": getattr(_TL, "src", None),
        "entry_rec": {k: p.get(k) for k in ENTRY_REC_KEYS},
    }
    st["pos"] = None
    st["realized"] = round(st.get("realized", 0.0) + net, 2)
    st["trade_count"] = st.get("trade_count", 0) + 1
    return trade


def _check_price_exits(st, C, ltp, t, high=None, low=None):
    """stop -> trailing -> target, in the same order as _simulate_simple.

    Pass high/low for a historical bar (intrabar), or only ltp for a live tick.
    """
    bt = _bt()
    p = st["pos"]
    sign = p["sign"]

    up = high if high is not None else ltp
    dn = low if low is not None else ltp
    p["best"] = max(p["best"], up) if sign > 0 else min(p["best"], dn)

    # 1) stop loss
    if C["stop_loss_pct"] > 0:
        stop = p["entry"] * (1 - sign * C["stop_loss_pct"] / 100.0)
        if (sign > 0 and dn <= stop) or (sign < 0 and up >= stop):
            return _close_pos(st, C, stop, "stop", t)

    # 2) trailing stop
    tsl = bt._trailing_sl_price(C, p["entry"], p["best"], sign)
    if tsl is not None:
        if (sign > 0 and dn <= tsl) or (sign < 0 and up >= tsl):
            return _close_pos(st, C, tsl, "trail_stop", t)

    # 3) profit target
    tgt = bt._target_px(C, p["entry"], p["qty"], sign)
    if tgt is not None:
        if (sign > 0 and up >= tgt) or (sign < 0 and dn <= tgt):
            return _close_pos(st, C, tgt, "target", t)
    return None


def _sig_id(ev):
    return "%s|%s" % (ev.get("direction"), ev.get("valid_t"))


# ── data prep (shared by live + replay + verify) ───────────────────────────
def _prep(df_real, cfg, C, htf_df=None):
    """Return (df, events, tradable_by_bar, volume_series) exactly as run() does."""
    bt = _bt()
    candles_to_df, heikin_ashi = _common()

    # backtest.run() applies the F&O filter BEFORE volume/HA — same order here
    df_real = apply_fno(df_real, cfg, cfg.get("symbol"))
    df = df_real.copy()
    volume_series = df["Volume"] if "Volume" in df.columns else None
    if _bool(cfg.get("heikin_ashi"), True):
        df = _heikin_ashi(df)

    params = {"n1": _num(cfg.get("wt_n1"), 15), "n2": _num(cfg.get("wt_n2"), 27)}
    events = bt.detect_wt_events(
        df, params,
        float(_num(cfg.get("buffer_pts"), 0.0)),
        max_wait=_int(cfg.get("wait_bars"), 20),
        pending_rule=str(cfg.get("wt_pending_rule") or "every"),
    )
    events = bt._apply_filters(events, df, htf_df, C, volume_series)

    by_bar = {}
    for e in events:
        if not e.get("filtered"):
            e = dict(e)                              # never mutate backtest objects
            e["valid_t"] = str(df.index[e["valid_i"]])[:16]
            e["trigger_t"] = str(df.index[e["trigger_i"]])[:16]
            by_bar.setdefault(e["trigger_i"], []).append(e)
    return df, events, by_bar, volume_series


def _pending_signal(df, cfg, comp=None):
    """Latest WT cross + the indicator values the Scanner page shows."""
    try:
        comp = comp or _wt_compute(df, cfg)
        cross = comp["cross"]
        wt1, wt2 = comp["wt1"], comp["wt2"]
        n = len(df)
        last = n - 1
        out = {"wt1": _f(wt1.iloc[last]), "wt2": _f(wt2.iloc[last]),
               "wt_trend": ("Buy Trend" if _f(wt1.iloc[last]) > _f(wt2.iloc[last])
                            else "Sell Trend")}
        try:
            out["rsi"] = _f(_bt()._rsi_series(df["Close"], _int(cfg.get("rsi_period"), 14)).iloc[last])
        except Exception:
            out["rsi"] = None
        for i in range(n - 1, 0, -1):        # include the newest CLOSED bar
            c = cross.iloc[i]
            if c in ("Bull Cross", "Bear Cross"):
                d = "buy" if c == "Bull Cross" else "sell"
                lvl = float(df["High"].iloc[i] if d == "buy" else df["Low"].iloc[i])
                out.update({"dir": d, "level": round(lvl, 4), "valid_i": i,
                            "valid_t": str(df.index[i])[:16], "age": last - i,
                            "cross": c,
                            "wt_val": _f(wt1.iloc[i] if d == "buy" else wt2.iloc[i])})
                return out
        out["dir"] = None
        return out
    except Exception:
        traceback.print_exc()
        return None


def _latest_break(pend, by_bar):
    """The breakout event of the latest cross `pend` (None = not broken out yet)."""
    if not pend or not pend.get("dir"):
        return None
    vi = pend.get("valid_i")
    for evs in (by_bar or {}).values():
        for e in evs:
            if e.get("valid_i") == vi:
                return e
    return None


def _already_triggered(st, Cs, cfg, pend, brk, ltp, t_now, allow_entry=True):
    """'Allow already-triggered if within %'.

    The LATEST cross (the one the Scanner page shows) broke out on an older candle —
    before Start, or while the engine could not take it — and the price is still
    within Max diff % of its level -> a market order now. It looks at the latest
    cross itself: v14 read the WT Action text, which names a DIFFERENT signal while
    one is still waiting to break, so e.g. CDSL's SELL 0.27% away was never seen.
    Returns the one-line outcome shown under the row ("" = nothing to say).
    """
    if not pend or not pend.get("dir") or not brk:
        return ""
    d = pend["dir"]
    lvl = float(pend["level"])
    sid = "%s|%s" % (d, pend.get("valid_t") or "")
    pos = st.get("pos")
    if pos:
        side = "LONG" if pos["sign"] > 0 else "SHORT"
        return ("→ this is the open %s" % side) if pos.get("sig_id") == sid else \
               ("→ not used: a %s is open" % side)
    if sid == st.get("last_sig") or sid in (st.get("sigs_traded") or []):
        return "→ already traded (each signal is traded once)"
    broke = str(brk.get("trigger_t") or "")[11:16]
    if not _bool(cfg.get("allow_already_triggered"), False):
        return "→ not entered: it broke at %s%s · 'Allow already-triggered' is off" % (
            broke, " (before ▶ Start)" if _pre_run(brk.get("trigger_t")) else "")
    if (Cs["side_mode"] == "long" and d == "sell") or (Cs["side_mode"] == "short" and d == "buy"):
        st["no_fill"] = "side mode = %s only" % Cs["side_mode"]
        return "→ not entered: Side = %s only" % Cs["side_mode"]
    if not allow_entry:
        return "→ not entered: stale feed — no new entries"
    if str(st.get("last_action") or "").startswith("EXIT "):
        return "→ not entered: no market re-entry right after an exit"
    mx = _num(cfg.get("already_triggered_max_pct"), 1.0)
    dpct = abs(ltp - lvl) / lvl * 100.0 if lvl else float("inf")
    if dpct > mx:
        return "→ not entered: LTP %.2f%% away > Max diff %g%%" % (dpct, mx)
    # a market order NOW: the live price, also when it has pulled back a little behind
    # the level (it used to book the level then — always the worse of level and market)
    Cm = dict(Cs)
    Cm["entry_exec"] = "market_now"
    p = _open_pos(st, Cm, lvl, d, _num(Cs.get("buffer_pts"), 0.0), t_now, sid, ltp=ltp,
                  kind="already_triggered")
    if not p:
        st["no_fill"] = "not enough cash for 1 share at this size"
        return "→ not entered: not enough cash for 1 share"
    p["why"] = "already-triggered within %.2f%% (%.2f%%)" % (mx, dpct)
    st["last_sig"] = sid
    st["no_fill"] = ""
    st["last_action"] = "ENTER %s @ %.4f (within %.2f%%)" % (d.upper(), p["entry"], mx)
    _save()
    return "→ entered at market: broke at %s, %.2f%% away ≤ Max diff %g%%" % (broke, dpct, mx)


def _pending_book(df, cfg, comp=None):
    """Signals from CLOSED candles that have NOT been broken out yet.

    Same idea as autotrader.get_active_pending_signals (what the Scanner page
    shows), but following the backtest's own three pending rules so the level
    and the expiry window are identical to detect_wt_events().
    Returns {"buy": {...}|None, "sell": {...}|None}.
    """
    out = {"buy": None, "sell": None}
    try:
        comp = comp or _wt_compute(df, cfg)
        cross = comp["cross"]
        buf = _num(cfg.get("buffer_pts"), 0.0)
        wait = _int(cfg.get("wait_bars"), 20)
        rule = str(cfg.get("wt_pending_rule") or "every")
        H = df["High"].values
        Lo = df["Low"].values
        n = len(df)

        def _lvl(i, d):
            return float(H[i] if d == "buy" else Lo[i])

        def _hit(d, i, level):
            return (H[i] > level + buf) if d == "buy" else (Lo[i] < level - buf)

        if rule in ("same_dir", "any_dir"):
            pend = {"buy": None, "sell": None}
            for i in range(1, n):
                for d in ("buy", "sell"):
                    q = pend[d]
                    if not q:
                        continue
                    if _hit(d, i, q["level"]) or i - q["valid_i"] >= wait:
                        pend[d] = None
                c = cross.iloc[i]
                if c in ("Bull Cross", "Bear Cross"):
                    d = "buy" if c == "Bull Cross" else "sell"
                    pend[d] = {"level": _lvl(i, d), "valid_i": i}
                    if rule == "any_dir":
                        pend["sell" if d == "buy" else "buy"] = None
            out = pend
        else:
            for i in range(max(1, n - wait), n):
                c = cross.iloc[i]
                if c not in ("Bull Cross", "Bear Cross"):
                    continue
                d = "buy" if c == "Bull Cross" else "sell"
                lvl = _lvl(i, d)
                if any(_hit(d, k, lvl) for k in range(i + 1, n)):
                    continue                                    # already broken out
                out[d] = {"level": lvl, "valid_i": i}           # newest live cross

        for d in out:
            q = out[d]
            if q:
                q["dir"] = d
                q["level"] = round(q["level"], 4)
                q["valid_t"] = str(df.index[q["valid_i"]])[:16]
                q["age"] = (n - 1) - q["valid_i"]
    except Exception:
        traceback.print_exc()
    return out


def wt_action_text(pend, pends, by_bar, ltp, cfg, C, has_pos, sym=""):
    """The Scanner page's "WT Action" column, same wording, same rules.

    Scanner logic (autotrader.py):
      LTP > level+buffer  -> "Buy Triggered @ <ltp>"   (break happens inside the candle)
      not yet             -> "Buy Pending (BO > <level>)"
    plus the states only this engine tracks (expired / already taken).
    """
    buf = _num(C.get("buffer_pts"), 0.0)
    live = None
    if pends:
        cands = []
        for d in ("buy", "sell"):
            q = pends.get(d)
            if not q:
                continue
            crossed = ((ltp > q["level"] + buf) if d == "buy"
                       else (ltp < q["level"] - buf))
            cands.append((crossed, _int(q.get("valid_i"), 10 ** 9), d, q))
        if cands:
            # a broken level first (oldest signal first), else the newest signal
            hit = [c for c in cands if c[0]]
            pick = (sorted(hit, key=lambda c: c[1])[0] if hit
                    else sorted(cands, key=lambda c: -c[1])[0])
            live = (pick[2], pick[3], pick[0])

    if live:
        d, q, crossed = live
        lvl = float(q["level"])
        side = d
        if crossed:
            _fmt = "%.5f" if (str(sym).endswith("=X") or str(sym).endswith("_FX")) else "%.2f"
            txt = (("Buy Triggered @ " + _fmt) if d == "buy" else ("Sell Triggered @ " + _fmt)) % ltp
            state = "live"
        else:
            _fmt = "%.5f" if (str(sym).endswith("=X") or str(sym).endswith("_FX")) else "%.2f"
            txt = ((("Buy Pending (BO > " + _fmt + ")") if d == "buy"
                    else ("Sell Pending (BO < " + _fmt + ")")) % lvl)
            state = "pending"
        diff = (ltp - lvl) if d == "buy" else (lvl - ltp)
        return {"wt_action": txt, "wt_side": side, "signal_state": state,
                "trig_lvl": lvl, "diff": round(diff, 4),
                "diff_pct": round(diff / lvl * 100.0, 2) if lvl else None,
                "signal_valid_t": q.get("valid_t"),
                "signal_age": q.get("age")}

    if not pend or not pend.get("dir"):
        return {"wt_action": "No signal", "wt_side": "", "signal_state": "none",
                "trig_lvl": None, "diff": None, "diff_pct": None,
                "signal_valid_t": "", "signal_age": None}

    d = pend["dir"]
    lvl = float(pend["level"])
    took = any(e.get("valid_i") == pend.get("valid_i")
               for evs in by_bar.values() for e in evs)
    if took:
        txt, state = ("Buy Triggered (already taken)" if d == "buy"
                      else "Sell Triggered (already taken)"), "done"
    elif pend.get("age", 0) >= _int(cfg.get("wait_bars"), 20):
        txt, state = (("Buy Timed Out (was > %.2f)" if d == "buy"
                       else "Sell Timed Out (was < %.2f)") % lvl), "expired"
    else:
        _fmt = "%.5f" if (str(sym).endswith("=X") or str(sym).endswith("_FX")) else "%.2f"
        txt, state = ((("Buy Pending (BO > " + _fmt + ")") if d == "buy"
                       else ("Sell Pending (BO < " + _fmt + ")")) % lvl), "pending"
    diff = (ltp - lvl) if d == "buy" else (lvl - ltp)
    return {"wt_action": txt, "wt_side": d, "signal_state": state,
            "trig_lvl": lvl, "diff": round(diff, 4),
            "diff_pct": round(diff / lvl * 100.0, 2) if lvl else None,
            "signal_valid_t": pend.get("valid_t"), "signal_age": pend.get("age")}


# ── historical replay of THIS engine (must match backtest) ─────────────────
def replay_trades(df_real, cfg, close_at_end=True):
    """Bar-by-bar replay using the very same helpers the live loop uses.

    This is a mirror of backtest._simulate_simple. If it does not equal
    backtest.run() on the same bars, the live loop is buggy — that is exactly
    what /api/paper-live/verify measures.
    """
    C = _make_C(cfg)
    htf = None
    if C["htf_filter_enabled"]:
        htf = htf_for(cfg.get("symbol") or "", str(cfg.get("interval") or ""))
    df, events, by_bar, _vol = _prep(df_real, cfg, C, htf)
    n = len(df)

    st = {"symbol": cfg.get("symbol") or "REPLAY", "cash": C["capital"],
          "pos": None, "realized": 0.0, "trade_count": 0}
    trades = []

    for i in range(1, n):
        t = str(df.index[i])
        hi = float(df["High"].iloc[i])
        lo = float(df["Low"].iloc[i])

        # 1) price exits (intrabar, like the backtest)
        if st["pos"]:
            tr = _check_price_exits(st, C, float(df["Close"].iloc[i]), t, high=hi, low=lo)
            if tr:
                trades.append(tr)
                st["last_exit_i"] = i

        # 2) opposite-signal exit
        if st["pos"]:
            sign = st["pos"]["sign"]
            for ev in by_bar.get(i, []):
                es = 1 if ev["direction"] == "buy" else -1
                if es != sign and C["exit_on_opposite"]:
                    trades.append(_close_pos(
                        st, C, ev["trigger_level"] + es * ev["buffer"], "opp_signal", t))
                    break

        # 3) entry
        if not st["pos"]:
            for ev in by_bar.get(i, []):
                es = 1 if ev["direction"] == "buy" else -1
                if C["side_mode"] == "long" and es < 0:
                    continue
                if C["side_mode"] == "short" and es > 0:
                    continue
                if _open_pos(st, C, ev["trigger_level"], ev["direction"],
                             ev["buffer"], t, _sig_id(ev), ltp=None, kind="replay"):
                    break        # backtest only breaks once a fill happened

    # 4) force-close the last open position (backtest does this as "eod")
    if close_at_end and st["pos"] and n > 1:
        t = str(df.index[n - 1])
        trades.append(_close_pos(st, C, float(df["Close"].iloc[n - 1]), "eod", t,
                                 apply_slip=False))

    return trades, st


# ── live step ──────────────────────────────────────────────────────────────
def _pre_run(valid_t):
    """' · armed before this run' when the cross predates the Start you pressed."""
    try:
        started = RUNTIME.get("started") or 0
        if not started or not valid_t:
            return ""
        run_t = pytz.utc.localize(
            __import__("datetime").datetime.utcfromtimestamp(started)).astimezone(IST)
        if str(valid_t)[:16] < run_t.strftime("%Y-%m-%d %H:%M"):
            return " · armed before this run"
    except Exception:
        pass
    return ""


def live_step(st, C, by_bar, last_i, ltp, t, allow_entry=True, pends=None):
    """One tick of the live state machine for one symbol.

    allow_entry=False keeps managing an open position but refuses new entries
    (used when the last candle is stale, e.g. market closed / dead feed).
    """
    acted = []

    # 1) / 2) / 3) price exits on the live LTP
    if st.get("pos"):
        tr = _check_price_exits(st, C, ltp, t)
        if tr:
            acted.append(tr)
            tr["symbol"] = st.get("symbol")
            TRADES.append(tr)
            st["last_action"] = "EXIT %s @ %s" % (tr["reason"], tr["exit"])

    # Catch breakouts since the previous completed scan, not just the last bar.
    # Otherwise a breakout one bar before last_i silently disappears forever.
    previous_bar = st.get("events_checked_through")
    recent_events = [ev for i in sorted(by_bar) if i <= last_i
                     for ev in by_bar[i]
                     if (str(ev.get("trigger_t") or "") > previous_bar
                         if previous_bar else i == last_i)]
    if st.get("pos"):
        sign = st["pos"]["sign"]
        for ev in recent_events:
            if str(ev.get("trigger_t") or "") < str(st["pos"].get("entry_t") or "")[:16]:
                continue
            es = 1 if ev["direction"] == "buy" else -1
            if es != sign and C["exit_on_opposite"]:
                px = ev["trigger_level"] + es * ev["buffer"]
                tr = _close_pos(st, C, px, "opp_signal", t)
                tr["symbol"] = st.get("symbol")
                tr["break_t"] = str(ev.get("trigger_t") or "")[:16]
                TRADES.append(tr)
                acted.append(tr)
                st["last_opp_bar"] = str(t)
                st["last_action"] = "EXIT opp_signal @ %s" % tr["exit"]
                break

    # 5) entry — only from an event on the LAST CLOSED bar, and only once per signal
    if not st.get("pos") and allow_entry:
        for ev in recent_events:
            es = 1 if ev["direction"] == "buy" else -1
            if C["side_mode"] == "long" and es < 0:
                st["no_fill"] = "side mode = long only"
                continue
            if C["side_mode"] == "short" and es > 0:
                st["no_fill"] = "side mode = short only"
                continue
            sid = _sig_id(ev)
            if st.get("last_sig") == sid:
                st["no_fill"] = "this signal was already traded"
                continue
            pos = _open_pos(st, C, ev["trigger_level"], ev["direction"],
                            ev["buffer"], t, sid, ltp=ltp, kind="closed_bar")
            if pos:
                st["last_sig"] = sid
                st["no_fill"] = ""
                pos["why"] = "closed bar %s broke %s%s" % (
                    str(ev.get("trigger_t") or "")[:16], round(float(ev["trigger_level"]), 2),
                    _pre_run(str(ev.get("valid_t") or "")))
                pos["break_t"] = str(ev.get("trigger_t") or "")[:16]
                st["last_action"] = "ENTRY %s %s @ %s" % (
                    "BUY" if es > 0 else "SELL", pos["qty"], round(pos["entry"], 2))
            else:
                st["no_fill"] = "not enough cash for 1 share at this size"
            break

    # 6) entry from a still-pending signal, broken out by the LIVE price
    #    (this is the Scanner page's "Buy Triggered @ <ltp>" case — the stop
    #    order at the level fills during the candle, you do not wait for close)
    if not st.get("pos") and allow_entry and pends:
        # BOTH directions can be waiting at the same time. The backtest takes the
        # signal that armed first, so sort by the cross bar — never "buy first".
        cands = []
        for d in ("buy", "sell"):
            q = pends.get(d)
            if not q:
                continue
            es = 1 if d == "buy" else -1
            if C["side_mode"] == "long" and es < 0:
                continue
            if C["side_mode"] == "short" and es > 0:
                continue
            buf = _num(C.get("buffer_pts"), 0.0)
            crossed = (ltp > q["level"] + buf) if d == "buy" else (ltp < q["level"] - buf)
            if not crossed:
                continue
            cands.append((_int(q.get("valid_i"), 10 ** 9), d, q, buf))
        cands.sort(key=lambda x: x[0])
        for _vi, d, q, buf in cands:
            es = 1 if d == "buy" else -1
            sid = "%s|%s" % (d, q.get("valid_t"))
            if st.get("last_sig") == sid:
                st["no_fill"] = "this signal was already traded"
                continue
            pos = _open_pos(st, C, q["level"], d, buf, t, sid, ltp=ltp, kind="ltp_break")
            if pos:
                st["last_sig"] = sid
                st["no_fill"] = ""
                pos["why"] = "live break of %s (cross %s)%s" % (
                    round(float(q["level"]), 2), q.get("valid_t"), _pre_run(q.get("valid_t")))
                st["last_action"] = "ENTRY %s %s @ %s (live break of %s)" % (
                    "BUY" if es > 0 else "SELL", pos["qty"],
                    round(pos["entry"], 2), q["level"])
            else:
                st["no_fill"] = "not enough cash for 1 share at this size"
            break

    return acted


# Named Paper sessions use a single atomic file per profile. Legacy state remains
# separate; profiles are never loaded while background work is still active.
import paper_profiles as _profiles
_PROFILE_DIR = DATA_DIR / "paper_profiles"
_PROFILE_ACTIVE_FILE = DATA_DIR / "paper_active_profile.json"
_PROFILE_LOCK = threading.RLock()
_PROFILE_SAVE_LOCK = threading.RLock()
_PROFILE_NAME = None
_PROFILE_UI = {}
_PROFILE_ERROR = ""


def _profile_snapshot():
    return {"version": _profiles.VERSION, "config": copy.deepcopy(RUNTIME.get("config") or {}),
            "state": copy.deepcopy(STATE), "trades": copy.deepcopy(TRADES),
            "ui": copy.deepcopy(_PROFILE_UI), "changes": copy.deepcopy(globals().get("CHANGES", [])),
            "saved_at": _now_ist_s()}


def _profile_restore_startup():
    global _PROFILE_NAME, _PROFILE_UI, _PROFILE_ERROR, TRADES
    if not _PROFILE_ACTIVE_FILE.exists():
        return False
    try:
        name = json.loads(_PROFILE_ACTIVE_FILE.read_text())["name"]
        if name is None:
            # Explicitly unprofiled after deletion: do not resurrect legacy state.
            STATE.clear(); TRADES = []; RUNTIME["config"] = {}
            _PROFILE_NAME, _PROFILE_UI, _PROFILE_ERROR = None, {}, ""
            return True
        snap = _profiles.load(_PROFILE_DIR, name)
        STATE.clear(); STATE.update(snap["state"])
        TRADES = snap["trades"]
        RUNTIME["config"] = cfg_of(snap["config"])
        RUNTIME["_profile_changes"] = snap.get("changes", [])
        _PROFILE_NAME, _PROFILE_UI = name, snap["ui"]
    except Exception as ex:
        # Never fall back to another account's state after a damaged profile.
        STATE.clear(); TRADES = []; RUNTIME["config"] = {}
        _PROFILE_ERROR = "Cannot restore active profile: %s" % ex
    return True


# ── state persistence ──────────────────────────────────────────────────────
def _save():
    global _PROFILE_ERROR, _STOP_EPOCH
    with _PROFILE_SAVE_LOCK:
        try:
            if _PROFILE_ERROR:
                return
            if _PROFILE_NAME:
                _profiles.save(_PROFILE_DIR, _PROFILE_NAME, _profile_snapshot())
            else:
                STATE_FILE.write_text(json.dumps(STATE, indent=2, default=str))
                TRADE_FILE.write_text(json.dumps(TRADES[-500:], indent=2, default=str))
        except Exception as e:
            _PROFILE_ERROR = "Save failed: %s" % e
            if _PROFILE_NAME:
                # Stop further trade processing if persistence is unavailable.
                _STOP_EPOCH += 1
                for job in JOBS.values(): job["stop"] = True
                RUNTIME["running"] = False
            print("⚠️ paper_live save failed:", e)


def _load():
    global TRADES
    try:
        if _profile_restore_startup():
            return
        if STATE_FILE.exists():
            STATE.update(json.loads(STATE_FILE.read_text()))
        if TRADE_FILE.exists():
            TRADES = json.loads(TRADE_FILE.read_text()) or []
    except Exception as e:
        print("⚠️ paper_live load failed:", e)


def _reset():
    global TRADES
    with RUN_LOCK:
        _note_change("reset", "🗑 Reset positions & history — cleared %d open position(s) and "
                              "%d closed trade(s)" % (sum(1 for v in STATE.values()
                                                          if (v or {}).get("pos")), len(TRADES)))
        STATE.clear()
        TRADES = []
        RUNTIME["scan_n"] = 0
        RUNTIME["cycles"] = 0
        RUNTIME["last_scan_at"] = ""
        RUNTIME["last_error"] = ""
        RUNTIME["aborted"] = False
        _save()


_load()


# ── one scan cycle ─────────────────────────────────────────────────────────
_TL = threading.local()       # which job (if any) the current thread is scanning for


def _stop_requested():
    """True once Stop was pressed on this thread's job (checked between symbols so a
    big list aborts fast). A loop whose job was replaced by a newer ▶ Start also
    stops — before, Stop -> Start let the old scan finish its old symbol list once
    its slow download returned. A manual "Scan now" has no job of its own."""
    epoch = getattr(_TL, "epoch", None)
    if epoch is not None and epoch != _STOP_EPOCH:
        return True
    jid = getattr(_TL, "job", None)
    if jid:
        j = JOBS.get(jid)
        return j is None or bool(j.get("stop"))
    # A manually requested scan/LTP has its own epoch. Old stopped jobs should
    # not cancel a new explicit preview; a subsequent Stop still changes epoch.
    if epoch is not None:
        return False
    return bool(JOBS) and any(j.get("stop") for j in JOBS.values())


MARKETS = {
    "IN":     {"label": "India (NSE/BSE)",   "exch": ".NS",     "hours": "09:15–15:30 IST, Mon–Fri"},
    "US":     {"label": "US (NASDAQ/NYSE)",  "exch": "",        "hours": "09:30–16:00 New York, Mon–Fri"},
    "FX":     {"label": "Forex / Commodity", "exch": "_FX",     "hours": "Sun 22:00 – Fri 22:00 UTC"},
    "CRYPTO": {"label": "Crypto",            "exch": "_CRYPTO", "hours": "24 × 7"},
}


def market_of(cfg):
    """Configured market code, upper-cased, defaulting to India."""
    mk = str((cfg or {}).get("market") or "IN").strip().upper()
    return mk if mk in MARKETS else "IN"


def market_label(mk):
    return (MARKETS.get(str(mk or "IN").upper()) or MARKETS["IN"])["label"]


def exch_of(cfg):
    """Ticker suffix for this run. An explicit "" means US (no suffix) and must NOT
    fall back to .NS — the old `get("exchange") or ".NS"` did exactly that, which is
    why AAPL came back as AAPL.NS and every US/US-session scan found no data."""
    cfg = cfg or {}
    ex = cfg.get("exchange")
    if ex is not None:
        return str(ex)
    return (MARKETS.get(market_of(cfg)) or {}).get("exch", ".NS")


def _utcnow():
    return pytz.utc.localize(__import__("datetime").datetime.utcnow())


def _market_open_now(cfg=None):
    """Is the configured market open right now? Each market has its own clock —
    NSE hours mean nothing for a US or forex symbol, which is why the old NSE-only
    check made the engine sit idle for the whole of a US/forex session."""
    cfg = cfg if isinstance(cfg, dict) else (RUNTIME.get("config") or {})
    mk = market_of(cfg)
    now = _utcnow()
    if mk == "CRYPTO":
        return True
    if mk == "US":
        ny = now.astimezone(pytz.timezone("America/New_York"))
        if ny.weekday() >= 5:
            return False
        mins = ny.hour * 60 + ny.minute
        return 9 * 60 + 30 <= mins <= 16 * 60
    if mk == "FX":
        # opens Sunday 22:00 UTC, runs to Friday 22:00 UTC
        wd, mins = now.weekday(), now.hour * 60 + now.minute
        if wd == 5:
            return False
        if wd == 6:
            return mins >= 22 * 60
        if wd == 4:
            return mins < 22 * 60
        return True
    ist = now.astimezone(IST)
    if ist.weekday() >= 5:
        return False
    mins = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


CANDLE_CHUNK = 8      # symbols per Yahoo candle request; DATA_LOCK is freed between them


def _chunks(symbols, size=CANDLE_CHUNK):
    out = [symbols[k:k + size] for k in range(0, len(symbols), size)]
    if len(out) > 1 and len(out[-1]) == 1:      # keep every symbol on the batch API
        last = out.pop()                        # (pop first: out[-2] would shift)
        out[-1] = out[-1] + last
    return out


def _download_candles(download_batch, symbols, tf, period, parts):
    """Candles for every symbol, CANDLE_CHUNK at a time. DATA_LOCK is released
    between chunks and a queued LTP poll goes first, so a slow Yahoo download no
    longer freezes exits / flips for the whole scan. Same data as one big batch."""
    rows = {}
    for k, part in enumerate(_chunks(list(symbols))):
        if _stop_requested():
            break
        tw = time.time()
        with DATA_LOCK:
            if _stop_requested():
                break
            tg = time.time()
            got = download_batch(part, interval=tf, period=period, force_refresh=True) or {}
        parts["candles_wait"] = parts.get("candles_wait", 0.0) + (tg - tw)
        parts["candles"] = parts.get("candles", 0.0) + (time.time() - tg)
        parts["chunks"] = parts.get("chunks", 0) + 1
        rows.update(got)
    return rows


def scan_once(cfg, kind="poll"):
    """Full scans take precedence over new LTP polls; Stop invalidates this work."""
    import data
    epoch = _STOP_EPOCH
    job = getattr(_TL, "job", None)
    _TL.epoch = epoch
    _TL.preview = kind == "preview"
    acquired = False
    try:
        while not _FULL_SCAN_LOCK.acquire(timeout=0.05):
            if _stop_requested(): return {"ok": False, "error": "scan stopped"}
        acquired = True
        if _stop_requested(): return {"ok": False, "error": "scan stopped"}
        RUNTIME["scanning"] = True
        RUNTIME["scan_started"] = time.time()
        RUNTIME["last_scan_kind"] = kind
        t0 = time.time()
        try:
            while not _WORK_GATE.acquire(timeout=0.05):
                if _stop_requested(): return {"ok": False, "error": "scan stopped"}
            try:
                if _stop_requested(): return {"ok": False, "error": "scan stopped"}
                data._CANCEL.check = lambda: epoch != _STOP_EPOCH or (job is not None and JOBS.get(job, {}).get("stop", True))
                return _scan_once(cfg)
            finally:
                data._CANCEL.check = None
                _WORK_GATE.release()
        finally:
            _TL.src = None
            RUNTIME["scanning"] = False
            RUNTIME["scan_ms"] = int((time.time() - t0) * 1000)
            RUNTIME["last_scan_at"] = pytz.utc.localize(
                __import__("datetime").datetime.utcnow()).astimezone(IST).strftime("%H:%M:%S")
    finally:
        if acquired: _FULL_SCAN_LOCK.release()
        _TL.epoch = None
        _TL.preview = False


def _pos_view(st, Cs, ltp):
    """Refresh one row's position columns from st["pos"] (or blank them).

    Shared by the full scan and the LTP poll, so a position opened / closed by
    the LTP poll shows up at once instead of waiting for the next full scan.
    """
    pos = st.get("pos")
    if pos:
        sign = pos["sign"]
        qty = int(pos["qty"])
        st["position"] = "%s %s" % ("LONG" if sign > 0 else "SHORT", qty)
        st["qty"] = qty
        st["entry"] = round(pos["entry"], 4)
        st["entry_t"] = str(pos.get("entry_t") or "")[:16]
        st["pos_why"] = pos.get("why") or ""
        # show exactly what _check_price_exits will use with the CURRENT
        # settings — Apply settings re-prices an open position, so the
        # Target / SL columns must follow it (they used to freeze at entry)
        _slp = _num(Cs.get("stop_loss_pct"), 0.0)
        _tgt = _bt()._target_px(Cs, pos["entry"], qty, sign)
        if _tgt is not None:
            pos["target"] = _tgt
        st["target"] = round(_tgt, 4) if _tgt is not None else "—"
        if _slp > 0:
            _stop = pos["entry"] * (1 - sign * _slp / 100.0)
        else:
            _stop = (_bt()._trailing_sl_price(Cs, pos["entry"], pos["best"], sign)
                     or pos.get("stop"))
        if _stop:
            pos["stop"] = _stop
        st["stop"] = round(_stop, 4) if _stop else "—"
        st["live_pnl"] = round((ltp - pos["entry"]) * sign * qty, 2)
        st["pnl_pct"] = round((ltp - pos["entry"]) / pos["entry"] * 100.0 * sign, 2)
        stop_px = st["stop"] if isinstance(st["stop"], (int, float)) else pos.get("stop")
        st["risk_amt"] = (round(abs(pos["entry"] - stop_px) * qty, 2)
                          if stop_px else None)
        tgt_px = pos.get("target")
        st["reward_amt"] = (round(abs(tgt_px - pos["entry"]) * qty, 2)
                            if tgt_px else None)
        st["rr"] = (round(st["reward_amt"] / st["risk_amt"], 2)
                    if st.get("risk_amt") and st.get("reward_amt") else None)
    else:
        st["position"] = "—"
        st["qty"] = 0
        st["entry"] = "—"
        st["entry_t"] = ""
        st["target"] = "—"
        st["stop"] = "—"
        st["live_pnl"] = 0.0
        st["pnl_pct"] = 0.0
        st["risk_amt"] = None
        st["reward_amt"] = None
        st["rr"] = None


def _scan_once(cfg):
    global LAST_SCAN, LAST_ERR
    _TL.src = "full scan"                      # recorded on every entry / exit it makes
    download_batch, get_live_ltp = _data()
    C = _make_C(cfg)
    ALLOC_AMT, ALLOC_CAP = _alloc_maps(cfg)
    tf = str(cfg.get("interval") or "15m")
    period = str(cfg.get("period") or "1mo")
    exchange = exch_of(cfg)
    symbols = parse_symbols(cfg.get("symbols"), exchange, cfg.get("symbol_exch"))
    if not symbols:
        LAST_ERR = "no symbols"
        return {"ok": False, "error": LAST_ERR}

    t0 = time.time()
    parts = {"symbols": len(symbols), "candles": 0.0, "candles_wait": 0.0, "chunks": 0,
             "prices": 0.0, "prices_wait": 0.0, "maths": 0.0, "slowest": []}
    rows = {}
    try:
        rows = _download_candles(download_batch, symbols, tf, period, parts)
        RUNTIME["download_error"] = ""
    except Exception as e:
        LAST_ERR = "download failed: %s" % e
        RUNTIME["download_error"] = LAST_ERR[:240]
        print("⚠️ paper_live download:", e)

    if _stop_requested():
        RUNTIME["aborted"] = True
        return {"ok": False, "error": "scan stopped"}

    # live price: 1m close (with its timestamp) -> data.get_live_ltp for whatever
    # the 1m frame missed -> the last candle close as the last resort
    _ps = {}
    ltps, ltp_ages = _ltp_from_1m(download_batch, symbols, stats=_ps)
    if _stop_requested():
        RUNTIME["aborted"] = True
        return {"ok": False, "error": "scan stopped"}
    _missing = [s for s in symbols if not ltps.get(s)]
    if _missing:
        try:
            _tw = time.time()
            with DATA_LOCK:                   # yfinance again — never two downloads at once
                if _stop_requested(): return {"ok": False, "error": "scan stopped"}
                _tg = time.time()
                got = get_live_ltp(_missing) or {}
            _ps["wait"] = _ps.get("wait", 0.0) + (_tg - _tw)
            _ps["took"] = _ps.get("took", 0.0) + (time.time() - _tg)
            for k, v in got.items():
                if v:
                    ltps[k] = float(v)
        except Exception as e:
            print("⚠️ paper_live ltp:", e)
    if _stop_requested():
        RUNTIME["aborted"] = True
        return {"ok": False, "error": "scan stopped"}
    ltp_fetch_at = time.time()
    parts["prices"] = _ps.get("took", 0.0)
    parts["prices_wait"] = _ps.get("wait", 0.0)

    htf_on = _bool(cfg.get("htf_filter_enabled"), False)

    now = pytz.utc.localize(__import__("datetime").datetime.utcnow()).astimezone(IST)
    t_now = now.strftime("%Y-%m-%d %H:%M:%S")

    with RUN_LOCK:
        _tm0 = time.time()
        _marks = []                            # (symbol, start time) -> slowest symbols
        for sym in symbols:
            _marks.append((sym, time.time()))
            if _stop_requested():
                RUNTIME["aborted"] = True
                break
            # per-symbol money: the table wins over the global boxes
            Cs = dict(C)
            _k = _clean_sym(sym)
            if _k in ALLOC_AMT:
                Cs["position_amount"] = ALLOC_AMT[_k]
            if _k in ALLOC_CAP:
                Cs["capital"] = ALLOC_CAP[_k]
            st = STATE.get(sym)
            if not st:
                st = STATE[sym] = {"symbol": sym, "cash": Cs["capital"], "pos": None,
                                   "realized": 0.0, "trade_count": 0, "last_sig": None,
                                   "last_action": "", "cycles": 0}
            st["cycles"] = st.get("cycles", 0) + 1
            _before_pos = copy.deepcopy(st.get("pos")) if _PROFILE_NAME else None
            _before_trades = len(TRADES)

            bars = rows.get(sym) or []
            if len(bars) < 40:
                st.update({"status": "No Data", "bars": len(bars), "updated": t_now})
                continue

            try:
                df_real = _common()[0](bars)
                if df_real.index.tz is None:
                    df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
                else:
                    df_real.index = df_real.index.tz_convert(IST)

                fno_on = fno_should_apply(cfg, sym)
                if fno_on:
                    df_real = apply_fno(df_real, cfg, sym)

                full = _heikin_ashi(df_real) if _bool(cfg.get("heikin_ashi"), True) else df_real.copy()
                closed = full.iloc[:-1]          # ← only CLOSED candles
                if len(closed) < 40:
                    st.update({"status": "Not enough closed bars", "updated": t_now})
                    continue

                sym_cfg = dict(cfg)
                sym_cfg["symbol"] = sym          # same context both sides of the F&O filter
                _hdf = htf_for(sym, tf) if htf_on else None
                _df, events, by_bar, _vol = _prep(df_real.iloc[:-1], sym_cfg, Cs, _hdf)
                # by_bar indices point into _df, so the frame used for the pending /
                # signal columns MUST be the same object — otherwise last_i reads the
                # wrong bar and the engine enters on a signal that is not the newest.
                if len(_df) != len(closed):
                    closed = _df
                last_i = len(closed) - 1
                # TF column: the timeframe this row's signal runs on, and with the HTF
                # filter on, the higher-timeframe trend gating entries right now
                st["tf"] = tf
                st["htf"] = None
                if htf_on:
                    _htf_tf = _bt()._HTF_MAP.get(tf)
                    try:
                        _up = _bt()._htf_uptrend_at(_hdf, closed.index[-1])
                    except Exception:
                        _up = None
                    st["htf"] = {"tf": _htf_tf, "up": _up}
                ltp = ltps.get(sym) or float(df_real["Close"].iloc[-1])
                ltp = float(ltp)
                # an LTP poll that finished after our price download holds a NEWER
                # price: use it — never step back to an older one
                _newer = (_num(st.get("px_at"), 0.0) > ltp_fetch_at
                          and isinstance(st.get("ltp_raw"), (int, float)) and st["ltp_raw"] > 0)
                if _newer:
                    ltp = float(st["ltp_raw"])
                else:
                    st["px_at"] = ltp_fetch_at
                    st["ltp_raw"] = ltp
                st["bar_close"] = round(float(df_real["Close"].iloc[-1]), 6)

                st["ltp"] = round(ltp, 4)
                if not _newer:
                    st["ltp_src"] = "live" if ltps.get(sym) else "last close"
                    st["ltp_age_min"] = ltp_ages.get(sym) if ltps.get(sym) else None
                st["last_bar"] = str(closed.index[-1])[:16]
                st["bar_age_min"] = round(
                    (now - closed.index[-1]).total_seconds() / 60.0, 1)

                # refuse new entries if the last candle is stale (market closed,
                # dead feed, weekend) — same protection the backtest gets for free
                tf_min = TF_MINUTES.get(tf, 60)
                stale = st.get("bar_age_min", 0) > max(2.5 * tf_min, tf_min + 15)
                allow_entry = (not stale) or _bool(cfg.get("allow_stale_entries"), False)

                try:                                  # once, shared by both helpers
                    _comp = _wt_compute(closed, cfg)
                except Exception:                     # the helpers then compute it
                    _comp = None                      # themselves (inside their own try)
                pends = _pending_book(closed, cfg, comp=_comp)
                st["pending_book"] = pends
                if _stop_requested():
                    RUNTIME["aborted"] = True
                    break
                if not getattr(_TL, "preview", False):
                    live_step(st, Cs, by_bar, last_i, ltp, t_now,
                              allow_entry=allow_entry, pends=pends)
                    st["events_checked_through"] = str(closed.index[-1])[:16]
                st["stale"] = bool(stale)

                pend = _pending_signal(closed, cfg, comp=_comp)
                st["pending"] = pend
                st["wt1"] = (pend or {}).get("wt1")
                st["wt2"] = (pend or {}).get("wt2")
                st["wt_trend"] = (pend or {}).get("wt_trend") or "—"
                st["rsi"] = (pend or {}).get("rsi")
                _lb = _latest_break(pend, by_bar)
                if not pend or not pend.get("dir"):
                    st["pending_state"] = "—"
                else:
                    if pend["age"] >= _int(cfg.get("wait_bars"), 20):
                        st["pending_state"] = "expired"
                    elif _lb:
                        st["pending_state"] = "already triggered"
                    else:
                        st["pending_state"] = "waiting break of %s" % pend["level"]
                # what the logic check needs to re-check the already-triggered rule
                st["latest_took"] = bool(_lb)
                st["latest_break_t"] = str((_lb or {}).get("trigger_t") or "")[:16]
                st["scan_ltp"] = ltp

                # Scanner-style WT Action (+ trigger level, valid time, diff)
                wa = wt_action_text(pend, pends, by_bar, ltp, cfg, C, bool(st.get("pos")), sym=sym)
                st.update(wa)
                _pos = st.get("pos")
                _opp_lvl = None
                if _pos:
                    _opp_dir = "sell" if _pos["sign"] > 0 else "buy"
                    _p_list = pends.values() if isinstance(pends, dict) else (pends or [])
                    for _pe in _p_list:
                        if isinstance(_pe, dict) and _pe.get("dir") == _opp_dir and not _pe.get("broken"):
                            _opp_lvl = _pe.get("level")
                            break
                st["opp_lvl"] = _opp_lvl

                # "Allow already-triggered": the latest cross itself (see the function)
                if _stop_requested():
                    RUNTIME["aborted"] = True
                    break
                if getattr(_TL, "preview", False):
                    st["latest_note"] = "Preview only — no trades processed"
                    # _pos_view normally recalculates the position's target/stop;
                    # preview may refresh displayed prices but must not alter it.
                    original_pos = st.get("pos")
                    st["pos"] = copy.deepcopy(original_pos)
                    try:
                        _pos_view(st, Cs, ltp)
                    finally:
                        st["pos"] = original_pos
                else:
                    st["latest_note"] = _already_triggered(st, Cs, cfg, pend, _lb, ltp, t_now,
                                                           allow_entry=allow_entry)
                    _pos_view(st, Cs, ltp)

                st["status"] = "OK"
                st["fno"] = bool(fno_on)
                st["bars"] = len(closed)
                st["signals"] = len(events)
                st["amount_used"] = round(Cs["position_amount"], 2)
                st["capital_used"] = round(Cs["capital"], 2)
                st["size_basis"] = sizing_label(Cs)
                st["updated"] = t_now
                if (_PROFILE_NAME and not getattr(_TL, "preview", False) and
                    (st.get("pos") != _before_pos or len(TRADES) != _before_trades)):
                    _save()
            except Exception as e:
                st["status"] = "Error: %s" % e
                st["updated"] = t_now
                traceback.print_exc()

        _tm1 = time.time()
        _dur = [(s_, (_marks[k + 1][1] if k + 1 < len(_marks) else _tm1) - ts)
                for k, (s_, ts) in enumerate(_marks)]
        parts["maths"] = _tm1 - _tm0
        parts["slowest"] = [[s_, round(d, 2)] for s_, d in sorted(_dur, key=lambda x: -x[1])[:3]]
        _save()

    parts["total"] = time.time() - t0
    RUNTIME["scan_parts"] = {k: (round(v, 1) if isinstance(v, float) else v)
                             for k, v in parts.items()}
    LAST_SCAN = time.time()
    LAST_ERR = ""
    RUNTIME["scan_n"] = len(symbols)
    return {"ok": True, "symbols": len(symbols), "ms": int((time.time() - t0) * 1000)}


# ── background loop ────────────────────────────────────────────────────────
DAY = 24 * 3600


def _loop(job_id):
    _TL.job = job_id
    RUNTIME["loops"] = RUNTIME.get("loops", 0) + 1
    while not JOBS.get(job_id, {}).get("stop", True):
        # read the config FRESH every cycle: ✔ Apply settings mutates this very dict
        cfg = RUNTIME.get("config") or JOBS.get(job_id, {}).get("config") or {}
        poll = _int(cfg.get("poll_seconds"), 0) or POLL_FOR_TF.get(
            str(cfg.get("interval")), 120)
        auto = _bool(cfg.get("auto_poll"), True)
        closed = not _market_open_now(cfg)
        try:
            if closed and not _bool(cfg.get("poll_when_market_closed"), False):
                # only when the user UNTICKED "poll when closed"
                RUNTIME["next_scan"] = time.time() + (poll if auto else DAY)
            else:
                if closed:
                    poll = max(poll, 300)      # after hours: no need to hammer Yahoo
                scan_once(cfg)
                RUNTIME["cycles"] += 1
                RUNTIME["next_scan"] = time.time() + (poll if auto else DAY)
        except Exception as e:
            RUNTIME["last_error"] = str(e)
            traceback.print_exc()
            RUNTIME["next_scan"] = time.time() + 30

        # Sleep in 1s slices so Stop is instant — and re-read next_scan every slice,
        # so ✔ Apply settings (which sets next_scan = now) wakes this loop at once
        # instead of waiting out the old poll interval.
        while not JOBS.get(job_id, {}).get("stop", True):
            target = RUNTIME.get("next_scan", 0)
            if time.time() >= target:
                break
            time.sleep(1)

    RUNTIME["loops"] = max(0, RUNTIME.get("loops", 1) - 1)
    if RUNTIME.get("job_id") == job_id:     # a newer ▶ Start owns these flags now
        RUNTIME["running"] = False
        RUNTIME["stopping"] = False
    JOBS.pop(job_id, None)


# ── verify: this engine vs the real backtest ───────────────────────────────
def _backtest_run(df_real, cfg, sym):
    """backtest.run() on the SAME bars, so the only variable is the engine."""
    bt = _bt()
    # Compute the F&O decision once and hand it to the backtest as "all"/"none",
    # so both engines filter the identical candles regardless of sheet formatting.
    _fno = "all" if fno_should_apply(cfg, sym) else "none"
    return bt.run(
        symbol=sym, interval=str(cfg.get("interval") or "1h"),
        period=str(cfg.get("period") or "6mo"),
        df_override=df_real,
        indicators={"wt": True},
        params={"wt": {"n1": _num(cfg.get("wt_n1"), 15),
                       "n2": _num(cfg.get("wt_n2"), 27)}},
        heikin_ashi_flag=_bool(cfg.get("heikin_ashi"), True),
        fno_mode=_fno,
        strategy_mode="simple",
        side_mode=str(cfg.get("side_mode") or "both"),
        wt_pending_rule=str(cfg.get("wt_pending_rule") or "every"),
        buffer_pts=_num(cfg.get("buffer_pts"), 0.0),
        wait_bars=_int(cfg.get("wait_bars"), 20),
        profit_target_pct=_num(cfg.get("profit_target_pct"), 0.0),
        profit_target_value=_num(cfg.get("profit_target_pct"), 0.0),
        profit_target_mode=str(cfg.get("profit_target_mode") or "pct"),
        stop_loss_pct=_num(cfg.get("stop_loss_pct"), 0.0),
        exit_on_opposite=_bool(cfg.get("exit_on_opposite"), True),
        trailing_enabled=_bool(cfg.get("trailing_enabled"), False),
        trailing_start_pct=_num(cfg.get("trailing_start_pct"), 1.0),
        trailing_step_pct=_num(cfg.get("trailing_step_pct"), 0.5),
        trailing_tighten_enabled=_bool(cfg.get("trailing_tighten_enabled"), False),
        trailing_tighten_at=_num(cfg.get("trailing_tighten_at"), 9.0),
        trailing_tighten_step=_num(cfg.get("trailing_tighten_step"), 1.0),
        capital=_num(cfg.get("capital"), 100000.0),
        position_amount=_num(cfg.get("position_amount"), 5000.0),
        position_pct=_num(cfg.get("position_pct"), 100.0),
        fixed_qty=_int(cfg.get("fixed_qty"), 1),
        sizing_mode=str(cfg.get("sizing_mode") or "fixed"),
        commission_pct=_num(cfg.get("commission_pct"), 0.05),
        slippage_pct=_num(cfg.get("slippage_pct"), 0.02),
        wt_filter_enabled=_bool(cfg.get("wt_filter_enabled"), False),
        wt_buy_max=_num(cfg.get("wt_buy_max"), -40.0),
        wt_sell_min=_num(cfg.get("wt_sell_min"), 40.0),
        htf_filter_enabled=_bool(cfg.get("htf_filter_enabled"), False),
        rsi_filter_enabled=_bool(cfg.get("rsi_filter_enabled"), False),
        rsi_period=_int(cfg.get("rsi_period"), 14),
        vol_filter_enabled=_bool(cfg.get("vol_filter_enabled"), False),
        vol_mult=_num(cfg.get("vol_mult"), 1.5),
        vol_lookback=_int(cfg.get("vol_lookback"), 20),
    )


def _norm_trade(t, symbol=""):
    """One shape for engine AND backtest trades (they use different key names)."""
    return {
        "symbol": t.get("symbol") or symbol,
        "side": t.get("side"),
        "qty": t.get("qty") or t.get("total_qty"),
        "entry_t": str(t.get("entry_t") or "")[:19],
        "entry": round(_num(t.get("entry") if t.get("entry") is not None else t.get("entry_px")), 2),
        "exit_t": str(t.get("exit_t") or "")[:19],
        "exit": round(_num(t.get("exit") if t.get("exit") is not None else t.get("exit_px")), 2),
        "pnl": round(_num(t.get("pnl")), 2),
        "pnl_pct": round(_num(t.get("pnl_pct")), 2),
        "reason": t.get("reason"),
    }


def _key(t):
    return (str(t.get("side")), str(t.get("entry_t"))[:16], str(t.get("exit_t"))[:16],
            round(_num(t.get("entry") if "entry" in t else t.get("entry_px")), 2),
            round(_num(t.get("exit") if "exit" in t else t.get("exit_px")), 2),
            round(_num(t.get("pnl")), 2))


def verify_cfg(cfg):
    """Replay the SAME candles through both engines and diff the trades."""
    download_batch, _ltp = _data()
    exchange = exch_of(cfg)
    all_syms = parse_symbols(cfg.get("symbols"), exchange, cfg.get("symbol_exch"))
    if not all_syms:
        return {"ok": False, "error": "no symbol"}
    want = str(cfg.get("verify_symbol") or "").strip()
    sym = want if want in all_syms else all_syms[0]
    cfg = dict(cfg)
    cfg["symbol"] = sym
    _amt, _cap = _alloc_maps(cfg)
    _k = _clean_sym(sym)
    if _k in _amt:
        cfg["position_amount"] = _amt[_k]
    if _k in _cap:
        cfg["capital"] = _cap[_k]

    rows = download_batch([sym], interval=str(cfg.get("interval")), 
                          period=str(cfg.get("period")), force_refresh=True)
    bars = (rows or {}).get(sym) or []
    if len(bars) < 60:
        return {"ok": False, "error": "not enough data for %s (%d bars)" % (sym, len(bars))}

    df_real = _common()[0](bars)
    if df_real.index.tz is None:
        df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
    else:
        df_real.index = df_real.index.tz_convert(IST)
    df_real = df_real.iloc[:-1]        # closed bars only — same set for both engines

    bt = _bt()
    engine_trades, st = replay_trades(df_real, cfg, close_at_end=True)
    res = _backtest_run(df_real, cfg, sym)
    if not res.get("ok"):
        return {"ok": False, "error": "backtest: %s" % res.get("error"),
                "engine_trades": len(engine_trades)}

    bt_trades = res.get("trades") or []
    a = [_key(t) for t in engine_trades]
    b = [_key(t) for t in bt_trades]

    diffs = []
    for i in range(max(len(a), len(b))):
        ea = a[i] if i < len(a) else None
        eb = b[i] if i < len(b) else None
        if ea != eb:
            diffs.append({"n": i + 1, "engine": ea, "backtest": eb})

    return {
        "ok": True,
        "symbol": sym,
        "bars": len(df_real),
        "from": str(df_real.index[0])[:16],
        "to": str(df_real.index[-1])[:16],
        "engine": {"trades": len(engine_trades),
                   "net": round(sum(t["pnl"] for t in engine_trades), 2)},
        "backtest": {"trades": len(bt_trades),
                     "net": round(sum(_num(t.get("pnl")) for t in bt_trades), 2)},
        "matched": len(a) == len(b) and not diffs,
        "diffs": diffs[:40],
        "engine_trade_list": [_norm_trade(t, sym) for t in engine_trades[:300]],
        "backtest_trade_list": [_norm_trade(t, sym) for t in bt_trades[:300]],
        "symbols_total": len(all_syms),
        "symbols": all_syms[:200],
    }


# ── routes ─────────────────────────────────────────────────────────────────
# ── Live Trade profile: dry run -> Google Sheet ────────────────────────────
# `python paper_live_engine.py --live` runs a SECOND, independent copy of this engine
# (own settings, symbols, positions and trades in live_trade_*.json, port 8021, page
# /live-trade). No broker is contacted: every signal, entry and exit the engine takes is
# written to a Google Sheet tab (and to live_trade_orders.csv), and a second tab lists
# the open positions, refreshed about once a minute. Nothing in this block decides a
# trade — it only reports what the unchanged Paper Live logic did.
SHEET_DEFAULTS = {"enabled": PROFILE == "live", "book": "", "orders_tab": "live_orders",
                  "positions_tab": "live_positions"}
SHEET_OUT = {"cfg": dict(SHEET_DEFAULTS), "seen_pos": [], "seen_trades": [], "seen_sigs": [],
             "outbox": [], "baselined": False}
SHEET_STAT = {"written": 0, "last_write": "", "last_error": "", "last_error_at": 0.0,
              "fails": 0, "next_try": 0.0, "pos_at": 0.0, "pos_sig": None, "pos_written": ""}
_SHEET_LOCK = threading.Lock()          # one Google call at a time (worker + Test button)
_SHEET_FILE_LOCK = threading.Lock()
_SHEET_THREAD = {"t": None}
_GS_WS = {}                             # (book, tab) -> (worksheet, opened_at)
ORDER_HEADERS = ["Time (IST)", "Event", "Symbol", "Exch", "TF", "Side", "Qty", "Order type",
                 "Price", "LTP", "Trigger lvl", "Target", "Stop", "P&L ₹", "P&L %", "Reason",
                 "Signal candle", "Note", "Order ID"]
POS_HEADERS = ["Symbol", "Exch", "TF", "Side", "Qty", "Entry", "Entry time", "LTP", "Live P&L ₹",
               "P&L %", "Target", "Stop / trail", "Opp lvl", "Note", "Order ID"]
SHORT_NOTE = "SHORT — needs F&O futures (an NSE/BSE cash short can't be held overnight)"
_ENTRY_KIND_WORDS = {"closed_bar": "closed-bar breakout", "ltp_break": "live breakout (LTP poll)",
                     "ltp_flip": "flip at the opposite level",
                     "already_triggered": "already-triggered signal, entered at market",
                     "replay": "replay"}
_EXIT_ORDER = {"target": "LIMIT @ target", "stop": "SL-M @ stop",
               "trail_stop": "SL-M @ trailing stop", "opp_signal": "MARKET", "eod": "MARKET"}


def _now_ist_s():
    return _utcnow().astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def _sym_exch(sym):
    s = str(sym or "").upper()
    if s.endswith(".NS"):
        return "NSE"
    if s.endswith(".BO"):
        return "BSE"
    if s.endswith("=X"):
        return "FX"
    if s.endswith("=F"):
        return "FUT"
    if s.endswith("-USD") or s.endswith("-USDT"):
        return "CRYPTO"
    m = (RUNTIME.get("config") or {}).get("symbol_exch") or {}
    return str(m.get(sym) or m.get(s) or "US")


def _is_cash_short(sym, sign):
    return _num(sign, 1) < 0 and _sym_exch(sym) in ("NSE", "BSE")


def _px_out(x, sym):
    if isinstance(x, bool):
        return ""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    if x != x:
        return ""
    return round(x, 5 if str(sym).upper().endswith("=X") else 2)


def _order_id(sym, entry_t, sign):
    base = re.sub(r"\.(NS|BO)$", "", str(sym or "").upper())
    digits = re.sub(r"\D", "", str(entry_t or ""))[4:12]          # MMDDHHMM
    return "%s-%s-%s" % (base, digits or "0", "L" if _num(sign, 1) > 0 else "S")


def _held(t0, t1):
    import datetime as _dt
    try:
        a = _dt.datetime.strptime(str(t0)[:16], "%Y-%m-%d %H:%M")
        b = _dt.datetime.strptime(str(t1)[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return ""
    m = max(0, int((b - a).total_seconds() // 60))
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    return ("%dd %dh" % (d, h)) if d else (("%dh %dm" % (h, m)) if h else "%dm" % m)


def _sig_candle(sig_id):
    s = str(sig_id or "")
    return s.split("|", 1)[1] if "|" in s else ""


def _row_entry(sym, p):
    sign = 1 if _num(p.get("sign"), 1) > 0 else -1
    lvl = p.get("level")
    trig = (float(lvl) + sign * _num(p.get("buffer"), 0.0)) if isinstance(lvl, (int, float)) else None
    kind = str(p.get("kind") or "")
    return [str(p.get("entry_t") or "")[:19], "ENTRY", sym, _sym_exch(sym), p.get("tf") or "",
            "BUY" if sign > 0 else "SELL", int(_num(p.get("qty"), 0)),
            "SL-M @ trigger" if p.get("fill") == "level" else "MARKET",
            _px_out(p.get("entry"), sym), _px_out(p.get("ltp_at_entry"), sym), _px_out(trig, sym),
            _px_out(p.get("target"), sym), _px_out(p.get("stop"), sym), "", "",
            p.get("why") or _ENTRY_KIND_WORDS.get(kind, kind), _sig_candle(p.get("sig_id")),
            SHORT_NOTE if _is_cash_short(sym, sign) else "", _order_id(sym, p.get("entry_t"), sign)]


def _row_exit(tr):
    sym = tr.get("symbol") or ""
    sign = 1 if str(tr.get("side")) == "long" else -1
    reason = str(tr.get("reason") or "")
    held = _held(tr.get("entry_t"), tr.get("exit_t"))
    return [str(tr.get("exit_t") or "")[:19], "EXIT", sym, _sym_exch(sym),
            (tr.get("entry_rec") or {}).get("tf") or "", "SELL" if sign > 0 else "BUY",
            int(_num(tr.get("qty"), 0)), _EXIT_ORDER.get(reason, "MARKET"),
            _px_out(tr.get("exit"), sym), _px_out(tr.get("ltp_at_exit"), sym), "", "", "",
            tr.get("pnl", ""), tr.get("pnl_pct", ""),
            _EXIT_WORDS.get(reason, reason) + ((" · held " + held) if held else ""),
            _sig_candle(tr.get("sig_id")),
            "covers a SHORT — needs F&O futures" if _is_cash_short(sym, sign) else "",
            _order_id(sym, tr.get("entry_t"), sign)]


def _row_signal(sym, st, q, cfg, now_t):
    sign = 1 if q.get("dir") == "buy" else -1
    trig = _num(q.get("level"), 0.0) + sign * _num(cfg.get("buffer_pts"), 0.0)
    why = "armed — enters if price %s %s" % ("rises above" if sign > 0 else "falls below",
                                             _px_out(trig, sym))
    wait = _int(cfg.get("wait_bars"), 0)
    if wait:
        why += " · valid %d more bar(s)" % max(0, wait - _int(q.get("age"), 0))
    return [now_t, "SIGNAL", sym, _sym_exch(sym), st.get("tf") or cfg.get("interval") or "",
            "BUY" if sign > 0 else "SELL", "", "", "", _px_out(st.get("ltp"), sym),
            _px_out(trig, sym), "", "", "", "", why, q.get("valid_t") or "",
            "short signal — needs F&O futures" if _is_cash_short(sym, sign) else "", ""]


def _csv_append(rows):
    try:
        import csv
        new = not ORDERS_CSV.exists()
        with open(ORDERS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(ORDER_HEADERS)
            w.writerows(rows)
    except Exception as e:
        print("⚠️ could not write %s: %s" % (ORDERS_CSV.name, e))


def _sheet_collect(now_t=None):
    """Turn what changed since the last look into sheet rows (queue + CSV copy).

    Closed trades -> EXIT, new positions -> ENTRY, new pending breakouts -> SIGNAL. A
    position that opened and closed between two looks still gets its ENTRY row (from
    the trade's entry record). The very first look only learns what is already there.
    Returns the number of new rows.
    """
    now_t = now_t or _now_ist_s()
    so = SHEET_OUT
    seen_p, seen_t, seen_s = set(so["seen_pos"]), set(so["seen_trades"]), set(so["seen_sigs"])
    cfg = RUNTIME.get("config") or {}
    events, new_p, new_t, new_s = [], [], [], []
    for tr in list(TRADES):
        if not isinstance(tr, dict):
            continue
        k = "%s|%s|%s|%s" % (tr.get("symbol"), tr.get("entry_t"), tr.get("exit_t"), tr.get("side"))
        if k in seen_t:
            continue
        new_t.append(k)
        sign = 1 if str(tr.get("side")) == "long" else -1
        pk = "%s|%s|%s" % (tr.get("symbol"), tr.get("entry_t"), sign)
        if pk not in seen_p:
            seen_p.add(pk)
            new_p.append(pk)
            p = dict(tr.get("entry_rec") or {})
            p.update(sign=sign, qty=tr.get("qty"), entry=tr.get("entry"), entry_t=tr.get("entry_t"))
            events.append((str(tr.get("entry_t") or now_t), 2, _row_entry(tr.get("symbol") or "", p)))
        events.append((str(tr.get("exit_t") or now_t), 0, _row_exit(tr)))
    for sym, st in list(STATE.items()):
        st = st or {}
        p = st.get("pos")
        if isinstance(p, dict):
            pk = "%s|%s|%s" % (sym, p.get("entry_t"), 1 if _num(p.get("sign"), 1) > 0 else -1)
            if pk not in seen_p:
                seen_p.add(pk)
                new_p.append(pk)
                events.append((str(p.get("entry_t") or now_t), 2, _row_entry(sym, p)))
        book = st.get("pending_book") if isinstance(st.get("pending_book"), dict) else {}
        for d in ("buy", "sell"):
            q = book.get(d)
            if not (isinstance(q, dict) and q.get("valid_t")):
                continue
            sk = "%s|%s|%s" % (sym, d, q.get("valid_t"))
            if sk not in seen_s:
                seen_s.add(sk)
                new_s.append(sk)
                events.append((now_t, 1, _row_signal(sym, st, q, cfg, now_t)))
    first = not so.get("baselined")
    so["baselined"] = True
    if not (new_p or new_t or new_s):
        if first:
            _sheet_save()
        return 0
    so["seen_pos"] = (so["seen_pos"] + new_p)[-3000:]
    so["seen_trades"] = (so["seen_trades"] + new_t)[-5000:]
    so["seen_sigs"] = (so["seen_sigs"] + new_s)[-5000:]
    if first:                       # first look ever: what is already there is not new
        _sheet_save()
        return 0
    events.sort(key=lambda e: (e[0], e[1]))
    rows = [e[2] for e in events]
    so["outbox"] = (so["outbox"] + rows)[-5000:]
    _csv_append(rows)
    _sheet_save()
    return len(rows)


def _positions_rows(now_t=None):
    now_t = now_t or _now_ist_s()
    rows, tot = [], 0.0
    for sym, st in sorted(list(STATE.items())):
        st = st or {}
        p = st.get("pos")
        if not isinstance(p, dict):
            continue
        sign = 1 if _num(p.get("sign"), 1) > 0 else -1
        pnl, pct = st.get("live_pnl"), st.get("pnl_pct")
        tot += _num(pnl, 0.0)
        rows.append([sym, _sym_exch(sym), p.get("tf") or st.get("tf") or "",
                     "LONG" if sign > 0 else "SHORT", int(_num(p.get("qty"), 0)),
                     _px_out(p.get("entry"), sym), str(p.get("entry_t") or "")[:16],
                     _px_out(st.get("ltp"), sym), _px_out(pnl, ""), _px_out(pct, ""),
                     _px_out(st.get("target"), sym), _px_out(st.get("stop"), sym),
                     _px_out(st.get("opp_lvl"), sym),
                     SHORT_NOTE if _is_cash_short(sym, sign) else "",
                     _order_id(sym, p.get("entry_t"), sign)])
    summary = ("updated %s IST · %d open position%s · live P&L ₹%s · dry run — no broker"
               % (now_t, len(rows), "" if len(rows) == 1 else "s", "{:,.2f}".format(tot)))
    return rows, summary


def _svc_email():
    try:
        import sheets as _sheets
        return _sheets.service_account_email() or ""
    except Exception:
        return ""


def _gs_err(e):
    s = "%s: %s" % (type(e).__name__, e)
    if "No module named" in s:
        return s + " — run:  pip install gspread oauth2client"
    if "SpreadsheetNotFound" in s or "not found" in s.lower():
        return ("Google Sheet '%s' not found — check the name and share the sheet with %s (Editor)"
                % (SHEET_OUT["cfg"].get("book"), _svc_email() or "the service account"))
    return s[:300]


def _gs_ws(book, tab, headers=None):
    """The worksheet (created when missing), cached 10 min; the header row is written once."""
    hit = _GS_WS.get((book, tab))
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    import sheets as _sheets
    ws = _sheets.ensure_tab(book, tab, min_rows=1000, min_cols=26)
    if ws is None:
        raise RuntimeError("Google Sheet '%s' not found or not shared — share it with %s (Editor)"
                           % (book, _svc_email() or "the service account"))
    if headers and not ws.row_values(1):
        ws.append_rows([headers], value_input_option="USER_ENTERED", table_range="A1")
    _GS_WS[(book, tab)] = (ws, time.time())
    return ws


def _sheet_flush(force_pos=False):
    """Send the queue to the orders tab (one call) and refresh the positions tab when a
    position opened / closed, or every 60 s while any is open. Never raises."""
    c = SHEET_OUT["cfg"]
    if not (c.get("enabled") and c.get("book")) or time.time() < SHEET_STAT["next_try"]:
        return False
    try:
        with _SHEET_LOCK:
            out = list(SHEET_OUT["outbox"][:200])
            if out:
                ws = _gs_ws(c["book"], c.get("orders_tab") or "live_orders", ORDER_HEADERS)
                ws.append_rows(out, value_input_option="USER_ENTERED", table_range="A1")
                del SHEET_OUT["outbox"][:len(out)]
                SHEET_STAT["written"] += len(out)
                SHEET_STAT["last_write"] = _now_ist_s()
                _sheet_save()
            rows, summary = _positions_rows()
            sig = tuple((r[0], r[4], r[5]) for r in rows)
            if (force_pos or sig != SHEET_STAT["pos_sig"]
                    or (rows and time.time() - SHEET_STAT["pos_at"] >= 60)):
                ws2 = _gs_ws(c["book"], c.get("positions_tab") or "live_positions")
                ws2.clear()
                ws2.append_rows([[summary], POS_HEADERS] + rows,
                                value_input_option="USER_ENTERED", table_range="A1")
                SHEET_STAT.update(pos_sig=sig, pos_at=time.time(), pos_written=_now_ist_s())
        SHEET_STAT.update(fails=0, last_error="")
        return True
    except Exception as e:
        _GS_WS.clear()
        SHEET_STAT["fails"] += 1
        SHEET_STAT["last_error"] = _gs_err(e)
        SHEET_STAT["last_error_at"] = time.time()
        SHEET_STAT["next_try"] = time.time() + min(300, 15 * (2 ** min(SHEET_STAT["fails"] - 1, 4)))
        print("⚠️ Google Sheet output: %s" % SHEET_STAT["last_error"])
        return False


def _sheet_worker():
    while True:
        try:
            _sheet_collect()
            _sheet_flush()
        except Exception as e:
            SHEET_STAT["last_error"] = "%s: %s" % (type(e).__name__, e)
            traceback.print_exc()
        time.sleep(3)


def _sheet_start():
    t = _SHEET_THREAD.get("t")
    if t is not None and t.is_alive():
        return
    t = threading.Thread(target=_sheet_worker, name="sheet-out", daemon=True)
    _SHEET_THREAD["t"] = t
    t.start()


def _sheet_status():
    c = SHEET_OUT["cfg"]
    t = _SHEET_THREAD.get("t")
    return {"enabled": bool(c.get("enabled")), "book": c.get("book") or "",
            "orders_tab": c.get("orders_tab") or "", "positions_tab": c.get("positions_tab") or "",
            "queued": len(SHEET_OUT["outbox"]), "written": SHEET_STAT["written"],
            "last_write": SHEET_STAT["last_write"], "positions_written": SHEET_STAT["pos_written"],
            "last_error": SHEET_STAT["last_error"],
            "retry_in": (max(0, int(SHEET_STAT["next_try"] - time.time()))
                         if SHEET_STAT["last_error"] else 0),
            "running": bool(t is not None and t.is_alive()), "csv": ORDERS_CSV.name}


def _sheet_save():
    with _SHEET_FILE_LOCK:
        try:
            d = {"cfg": SHEET_OUT["cfg"], "baselined": bool(SHEET_OUT.get("baselined")),
                 "seen_pos": SHEET_OUT["seen_pos"][-3000:],
                 "seen_trades": SHEET_OUT["seen_trades"][-5000:],
                 "seen_sigs": SHEET_OUT["seen_sigs"][-5000:],
                 "outbox": SHEET_OUT["outbox"][-5000:]}
            tmp = SHEET_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1, default=str))
            os.replace(str(tmp), str(SHEET_FILE))      # never a half-written file
        except Exception as e:
            print("⚠️ sheet output save failed:", e)


def _sheet_load():
    try:
        if not SHEET_FILE.exists():
            return
        d = json.loads(SHEET_FILE.read_text()) or {}
        cfg = d.get("cfg") or {}
        for k, v in SHEET_DEFAULTS.items():
            SHEET_OUT["cfg"][k] = cfg.get(k, v)
        for k in ("seen_pos", "seen_trades", "seen_sigs", "outbox"):
            SHEET_OUT[k] = list(d.get(k) or [])
        SHEET_OUT["baselined"] = bool(d.get("baselined"))
    except Exception as e:
        print("⚠️ sheet output settings unreadable (%s): %s" % (SHEET_FILE.name, e))


_sheet_load()


@paper_live_bp.route("/api/paper-live/sheet-out", methods=["GET", "POST"])
def api_sheet_out():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        c = dict(SHEET_OUT["cfg"])
        if "enabled" in body:
            c["enabled"] = _bool(body.get("enabled"), False)
        for k in ("book", "orders_tab", "positions_tab"):
            if k in body:
                c[k] = str(body.get(k) or "").strip() or SHEET_DEFAULTS[k]
        if c.get("orders_tab") == c.get("positions_tab"):
            return jsonify({"ok": False, "error": "use two different tabs — the positions tab is "
                                                  "cleared and rewritten every minute"})
        SHEET_OUT["cfg"].update(c)
        SHEET_STAT.update(next_try=0.0, fails=0, last_error="", pos_sig=None)
        _GS_WS.clear()
        _sheet_save()
        if PROFILE == "live" or c.get("enabled"):
            _sheet_start()
    return jsonify({"ok": True, "profile": PROFILE, "status": _sheet_status(),
                    "books": KNOWN_BOOKS, "service_account": _svc_email()})


@paper_live_bp.route("/api/paper-live/sheet-out/test", methods=["POST"])
def api_sheet_out_test():
    c = SHEET_OUT["cfg"]
    if not c.get("book"):
        return jsonify({"ok": False, "error": "type the Google Sheet name and press Save first"})
    row = [_now_ist_s(), "TEST"] + [""] * 13 + ["test row from the Live Trade page — safe to delete",
                                                 "", "", ""]
    try:
        with _SHEET_LOCK:
            ws = _gs_ws(c["book"], c.get("orders_tab") or "live_orders", ORDER_HEADERS)
            ws.append_rows([row], value_input_option="USER_ENTERED", table_range="A1")
    except Exception as e:
        _GS_WS.clear()
        return jsonify({"ok": False, "error": _gs_err(e)})
    return jsonify({"ok": True, "message": "wrote a TEST row to '%s' › %s"
                                           % (c["book"], c.get("orders_tab"))})


@paper_live_bp.route("/paper-live")
def paper_live_page():
    if PROFILE == "live":
        return redirect("/live/live-trade")
    return render_template("paper_trading.html", profile=PROFILE)


@paper_live_bp.route("/live-trade")
def live_trade_page():
    if PROFILE != "live":
        return ("<p style='font:14px sans-serif;padding:20px'>Live Trade runs as its own server, "
                "next to Paper Live. In a second Terminal window:<br><br>"
                "<code>cd web &amp;&amp; caffeinate -i python paper_live_engine.py --live</code>"
                "<br><br>then open <b>http://localhost:8021/live-trade</b></p>")
    return render_template("paper_trading.html", profile=PROFILE)




@paper_live_bp.route("/api/paper-live/profiles", methods=["GET", "POST"])
def api_session_profiles():
    global _PROFILE_NAME, _PROFILE_UI, _PROFILE_ERROR, _STOP_EPOCH, TRADES, LAST_SCAN, LAST_ERR
    if request.method == "GET":
        return jsonify({"ok": True, "names": _profiles.list_names(_PROFILE_DIR),
                        "active": _PROFILE_NAME, "error": _PROFILE_ERROR})
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    with _PROFILE_LOCK:
        if action == "delete":
            if RUNTIME.get("running") or RUNTIME.get("scanning"):
                return jsonify({"ok": False, "error": "Stop trading and wait for the scan to finish before deleting a profile"})
            if (body.get("active") or "") != (_PROFILE_NAME or ""):
                return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
            name = body.get("name")
            active_delete = bool(_PROFILE_NAME and name == _PROFILE_NAME)
            if active_delete and body.get("confirm_active") is not True:
                return jsonify({"ok": False, "error": "Active profile deletion requires confirmation"})
            if active_delete and not _FULL_SCAN_LOCK.acquire(blocking=False):
                return jsonify({"ok": False, "error": "Scan is still finishing; try again"})
            got_work = False
            try:
                if active_delete:
                    got_work = _WORK_GATE.acquire(blocking=False)
                    if not got_work:
                        return jsonify({"ok": False, "error": "Price request is still finishing; try again"})
                with _PROFILE_SAVE_LOCK, RUN_LOCK:
                    if active_delete:
                        # Write an explicit empty marker so restart cannot load legacy state.
                        _profiles._atomic_write(_PROFILE_ACTIVE_FILE, json.dumps({"name": None}).encode())
                    try:
                        _profiles.delete(_PROFILE_DIR, name)
                    except Exception:
                        if active_delete:
                            _profiles._atomic_write(_PROFILE_ACTIVE_FILE, json.dumps({"name": name}).encode())
                        raise
                    if active_delete:
                        _STOP_EPOCH += 1
                        STATE.clear(); TRADES = []; CHANGES.clear(); _HEALTH_SEEN.clear(); JOBS.clear()
                        LAST_SCAN, LAST_ERR = 0.0, ""
                        RUNTIME.update(running=False, stopping=False, scanning=False, config={},
                                       cycles=0, last_scan_at="", scan_n=0, scan_ms=0, scan_parts={},
                                       ltp_stats={}, last_error="", download_error="", started=0, job_id=None)
                        _PROFILE_NAME, _PROFILE_UI, _PROFILE_ERROR = None, {}, ""
                return jsonify({"ok": True, "deleted": name, "deleted_active": active_delete})
            except Exception as ex:
                return jsonify({"ok": False, "error": str(ex)})
            finally:
                if got_work:
                    _WORK_GATE.release()
                if active_delete:
                    _FULL_SCAN_LOCK.release()
        if action == "save":
            if not _PROFILE_NAME:
                return jsonify({"ok": False, "error": "Create or load a profile first"})
            if body.get("active") != _PROFILE_NAME:
                return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
            if isinstance(body.get("ui"), dict):
                _PROFILE_UI = body["ui"]
            with RUN_LOCK:
                _save()
            return jsonify({"ok": not bool(_PROFILE_ERROR), "error": _PROFILE_ERROR})
        if action not in ("create", "new", "load"):
            return jsonify({"ok": False, "error": "Unknown profile action"})
        if RUNTIME.get("running") or RUNTIME.get("scanning"):
            return jsonify({"ok": False, "error": "Stop first and wait for the current scan to finish"})
        if not _FULL_SCAN_LOCK.acquire(blocking=False):
            return jsonify({"ok": False, "error": "Scan is still finishing; try again"})
        if not _WORK_GATE.acquire(blocking=False):
            _FULL_SCAN_LOCK.release()
            return jsonify({"ok": False, "error": "Price request is still finishing; try again"})
        try:
            with _PROFILE_SAVE_LOCK, RUN_LOCK:
                if RUNTIME.get("running"):
                    return jsonify({"ok": False, "error": "Stop first"})
                name = body.get("name")
                try:
                    # Validate or create the target BEFORE changing the current state.
                    if action == "load":
                        snap = _profiles.load(_PROFILE_DIR, name)
                    else:
                        snap = _profile_snapshot() if action == "create" else {
                            "version": _profiles.VERSION, "state": {}, "trades": [],
                            "config": cfg_of(body.get("config") or {}), "ui": body.get("ui") or {},
                            "saved_at": _now_ist_s()}
                        if action == "create" and isinstance(body.get("ui"), dict):
                            snap["ui"] = body["ui"]
                        if action == "create" and not snap["config"]:
                            snap["config"] = cfg_of(body.get("config") or {})
                        _profiles.save(_PROFILE_DIR, name, snap, create_only=True)
                    # Never overwrite the old profile when its current save failed.
                    if _PROFILE_ERROR:
                        raise _profiles.ProfileError(_PROFILE_ERROR)
                    if _PROFILE_NAME:
                        if body.get("active") != _PROFILE_NAME:
                            raise _profiles.ProfileError("Profile changed in another tab; refresh")
                        if isinstance(body.get("current_ui"), dict):
                            _PROFILE_UI = body["current_ui"]
                        _save()
                        if _PROFILE_ERROR:
                            raise _profiles.ProfileError(_PROFILE_ERROR)
                        if action == "load" and name == _PROFILE_NAME:
                            snap = _profiles.load(_PROFILE_DIR, name)
                    _profiles._atomic_write(_PROFILE_ACTIVE_FILE, json.dumps({"name":name}).encode())
                    _STOP_EPOCH += 1
                    STATE.clear(); STATE.update(snap["state"])
                    TRADES = snap["trades"]
                    CHANGES[:] = snap.get("changes", [])
                    _HEALTH_SEEN.clear()
                    LAST_SCAN, LAST_ERR = 0.0, ""
                    RUNTIME.update(running=False, stopping=False, scanning=False, config=cfg_of(snap["config"]),
                                   cycles=0, last_scan_at="", scan_n=0, scan_ms=0, scan_parts={},
                                   ltp_stats={}, last_error="", download_error="", started=0, job_id=None)
                    JOBS.clear()
                    _PROFILE_NAME, _PROFILE_UI = name, snap["ui"]
                    return jsonify({"ok": True, "active": name, "config": RUNTIME["config"],
                                    "ui": _PROFILE_UI, "saved_at": snap.get("saved_at", ""),
                                    "warning": "Profile inactive: missed moves cannot be recreated. Preview before Start."})
                except Exception as ex:
                    return jsonify({"ok": False, "error": str(ex)})
        finally:
            _WORK_GATE.release(); _FULL_SCAN_LOCK.release()

@paper_live_bp.route("/api/paper-live/start", methods=["POST"])
def api_start():
    with _PROFILE_LOCK:
        return _api_start_locked()


def _api_start_locked():
    if _PROFILE_ERROR:
        return jsonify({"ok": False, "error": _PROFILE_ERROR})
    body = request.get_json(silent=True) or {}
    if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
    cfg = cfg_of(body)

    for jid in list(JOBS.keys()):
        JOBS[jid]["stop"] = True
    JOBS.clear()

    job_id = "pl_%d" % int(time.time() * 1000)
    JOBS[job_id] = {"stop": False, "config": cfg}
    RUNTIME.update({"running": True, "job_id": job_id, "config": cfg,
                    "started": time.time(), "cycles": 0, "last_error": "",
                    "stopping": False, "aborted": False, "next_scan": time.time()})
    _save()
    if _PROFILE_ERROR:
        RUNTIME["running"] = False
        JOBS[job_id]["stop"] = True
        return jsonify({"ok": False, "error": _PROFILE_ERROR})
    threading.Thread(target=_loop, args=(job_id,), daemon=True).start()

    poll = _int(cfg.get("poll_seconds"), 0) or POLL_FOR_TF.get(str(cfg.get("interval")), 120)
    _note_change("start", "▶ Start — %d symbols · %s candles · full scan every %s" % (
        len(parse_symbols(cfg.get("symbols"), exch_of(cfg), cfg.get("symbol_exch"))),
        cfg.get("interval"), ("%g min" % (poll / 60.0)) if poll >= 60 else ("%ds" % poll)))
    return jsonify({"ok": True, "job_id": job_id, "poll_seconds": poll,
                    "symbols": parse_symbols(cfg.get("symbols"), exch_of(cfg), cfg.get("symbol_exch"))})


def api_update_inner(cfg):
    exch = exch_of(cfg)
    new_syms = parse_symbols(cfg.get("symbols"), exch, cfg.get("symbol_exch"))
    if not RUNTIME["running"] or not RUNTIME["config"]:
        RUNTIME["config"] = cfg
        if _PROFILE_NAME:
            _save()
        return {"ok": not bool(_PROFILE_ERROR), "error": _PROFILE_ERROR, "running": False, "symbols": new_syms,
                "added": [], "kept": [], "note": "not running — saved only"}
    old = parse_symbols(RUNTIME["config"].get("symbols"), exch)
    kept = [x for x in old
            if x not in new_syms and (STATE.get(x) or {}).get("pos")]
    merged = new_syms + [x for x in kept if x not in new_syms]
    added = [x for x in new_syms if x not in old]
    removed = [x for x in old if x not in merged]
    changed = _settings_diff(RUNTIME["config"], cfg)
    RUNTIME["config"].update(cfg)          # same dict the loop thread reads
    RUNTIME["config"]["symbols"] = ", ".join(merged)
    RUNTIME["next_scan"] = time.time()      # scan the new names right away

    # Apply settings also re-prices positions already on the book: a new stop %
    # or target % must show up in the monitor and drive the next exit check.
    repriced = []
    try:
        bt = _bt()
        amt, cap = _alloc_maps(RUNTIME["config"])
        for x in merged:
            p = (STATE.get(x) or {}).get("pos")
            if not p:
                continue
            Cx = dict(_make_C(RUNTIME["config"]))
            k = _clean_sym(x)
            if k in amt:
                Cx["position_amount"] = amt[k]
            if k in cap:
                Cx["capital"] = cap[k]
            sign = p["sign"]
            p["target"] = bt._target_px(Cx, p["entry"], p["qty"], sign)
            slp = _num(Cx.get("stop_loss_pct"), 0.0)
            p["stop"] = (p["entry"] * (1 - sign * slp / 100.0)) if slp > 0 else None
            repriced.append(x)
    except Exception as e:
        print("⚠️ reprice on apply failed:", e)

    bits = list(changed)
    if added:
        bits.append("added " + (", ".join(added) if len(added) <= 4 else "%d symbols" % len(added)))
    if removed:
        bits.append("removed " + (", ".join(removed) if len(removed) <= 4
                                  else "%d symbols" % len(removed)))
    if kept:
        bits.append("kept %d removed symbol(s) with an open position" % len(kept))
    if bits:
        _note_change("apply", "✔ Apply settings — %s%s" % (" · ".join(bits), (
            " · re-priced %d open position(s) (new target / stop)" % len(repriced))
            if (repriced and changed) else ""))
    else:
        _note_change("apply", "✔ Apply settings — nothing changed")
    _save()
    return {"ok": True, "running": True, "symbols": merged,
            "added": added, "removed": removed, "kept": kept,
            "repriced": repriced,
            "note": ("added %d, removed %d" % (len(added), len(removed)))
                    if (added or removed) else "settings applied"}


_SETTING_LABEL = {
    "interval": "Interval", "period": "Lookback", "wt_n1": "WT n1", "wt_n2": "WT n2",
    "wt_pending_rule": "Pending rule", "buffer_pts": "Buffer", "wait_bars": "Wait bars",
    "profit_target_pct": "Target %", "profit_target_mode": "Target mode",
    "stop_loss_pct": "Stop loss %", "trailing_enabled": "Trailing SL",
    "trailing_start_pct": "Trail start %", "trailing_step_pct": "Trail step %",
    "trailing_tighten_enabled": "Tighten near target", "trailing_tighten_at": "Tighten at %",
    "trailing_tighten_step": "Tighter step %", "side_mode": "Side",
    "heikin_ashi": "Heikin-Ashi", "entry_exec": "Entry fill",
    "allow_already_triggered": "Allow already-triggered",
    "already_triggered_max_pct": "Max diff %", "sizing_mode": "Size by",
    "position_amount": "₹ per trade", "fixed_qty": "Fixed qty", "position_pct": "% of capital",
    "capital": "Capital", "commission_pct": "Commission %", "slippage_pct": "Slippage %",
    "exit_on_opposite": "Exit on opposite", "fno_mode": "F&O filter",
    "poll_seconds": "Poll", "auto_poll": "auto", "allow_stale_entries": "Allow stale entries",
    "market": "Market", "exchange": "Exchange"}


def _settings_diff(old, new):
    """['Target % 1 → 1.5', ...] for the settings Apply really changed."""
    def show(v):
        if isinstance(v, bool):
            return "on" if v else "off"
        if isinstance(v, float):
            return "%g" % v
        return str(v)
    out = []
    for k in DEFAULTS:
        if k in ("symbols", "symbol_exch", "verify_symbol") or k not in new:
            continue
        a, b = old.get(k), new.get(k)
        if k == "alloc":
            if (a or []) != (b or []):
                out.append("allocations changed")
            continue
        if show(a) != show(b):
            out.append("%s %s → %s" % (_SETTING_LABEL.get(k, k), show(a), show(b)))
    return out


@paper_live_bp.route("/api/paper-live/update", methods=["POST"])
def api_update():
    """Apply left-panel changes without stopping. New symbols join the running
    loop; open positions are never dropped; every other setting is swapped in."""
    body = request.get_json(silent=True) or {}
    if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
    cfg = cfg_of(body)
    try:
        with _PROFILE_LOCK:
            if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
                return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
            return jsonify(api_update_inner(cfg))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


@paper_live_bp.route("/api/paper-live/stop", methods=["POST"])
def api_stop():
    """Request a stop. The scan aborts at the next symbol boundary, then the
    loop thread flips running -> False (the page shows STOPPING until then)."""
    global _STOP_EPOCH
    _STOP_EPOCH += 1
    busy = bool(RUNTIME.get("scanning"))
    if RUNTIME.get("running"):
        _note_change("stop", "■ Stop — open positions are kept, no exits are checked until ▶ Start")
    for jid in list(JOBS.keys()):
        JOBS[jid]["stop"] = True
    if JOBS:
        RUNTIME["running"] = False
        RUNTIME["stopping"] = False
    else:
        RUNTIME["running"] = False
        RUNTIME["stopping"] = False
    _save()
    return jsonify({"ok": True, "stopping": RUNTIME["stopping"],
                    "was_scanning": busy})



def _ltp_tick(st, sym, ltp, age, C, cfg, alloc_amt, alloc_cap, t_now, fetch_at=None):
    """One LTP-poll tick for one symbol, between full scans.

    Same rules as live_step: price exits, then the opposite level (flip), then a
    broken pending level (entry). Returns True when a trade opened or closed.
    fetch_at = when this price was downloaded. A price OLDER than the one already
    applied (by a full scan that finished meanwhile) is ignored.
    """
    fetch_at = time.time() if fetch_at is None else fetch_at
    if _num(st.get("px_at"), 0.0) > fetch_at:
        return False
    st["px_at"] = fetch_at
    st["ltp_raw"] = float(ltp)
    st["ltp"] = round(ltp, 5 if sym.endswith("=X") else 2)
    if age is not None:
        st["ltp_age_min"] = age
        st["ltp_src"] = "live"
    Cs = dict(C)
    _k = _clean_sym(sym)
    if _k in alloc_amt:
        Cs["position_amount"] = alloc_amt[_k]
    if _k in alloc_cap:
        Cs["capital"] = alloc_cap[_k]
    allow = (not st.get("stale")) or _bool(cfg.get("allow_stale_entries"), False)
    changed = False

    pos = st.get("pos")
    if pos:
        tr = _check_price_exits(st, Cs, ltp, t_now)
        if tr:
            tr["symbol"] = sym
            TRADES.append(tr)
            st["last_action"] = "EXIT %s @ %s (LTP)" % (tr["reason"], tr["exit"])
            changed = True
        elif Cs.get("exit_on_opposite") and st.get("opp_lvl") is not None:
            ol = float(st["opp_lvl"])
            buf = _num(Cs.get("buffer_pts"), 0.0)
            sgn = pos["sign"]
            if (sgn > 0 and ltp < ol - buf) or (sgn < 0 and ltp > ol + buf):
                opp_dir = "sell" if sgn > 0 else "buy"
                tr = _close_pos(st, Cs, ol - sgn * buf, "opp_signal", t_now)
                tr["symbol"] = sym
                TRADES.append(tr)
                st["last_action"] = "EXIT opp_signal @ %s (LTP)" % tr["exit"]
                st["opp_lvl"] = None
                changed = True
                q = (st.get("pending_book") or {}).get(opp_dir) or {}
                osig = "%s|%s" % (opp_dir, q.get("valid_t"))
                side_ok = not ((Cs["side_mode"] == "long" and opp_dir == "sell") or
                               (Cs["side_mode"] == "short" and opp_dir == "buy"))
                if not side_ok:
                    st["no_fill"] = "side mode = %s only" % Cs["side_mode"]
                elif allow and osig != st.get("last_sig"):
                    opened = _open_pos(st, Cs, ol, opp_dir, buf, t_now, osig, ltp=ltp,
                                       kind="ltp_flip")
                    if opened:
                        st["last_sig"] = osig        # never re-enter this signal again
                        st["no_fill"] = ""
                        opened["why"] = "LTP broke opposite level %s (cross %s)" % (
                            ol, q.get("valid_t") or "?")
                        st["last_action"] = "FLIP %s %s @ %s (LTP)" % (
                            opp_dir.upper(), opened["qty"], round(opened["entry"], 5))
                    else:
                        st["no_fill"] = "not enough cash for 1 share at this size"

    if not st.get("pos") and allow and isinstance(st.get("pending_book"), dict):
        # step 6 of live_step only: a pending level from the last full scan, broken
        # by this LTP (by_bar is empty, so nothing else in live_step can fire)
        live_step(st, Cs, {}, -1, ltp, t_now, allow_entry=True, pends=st["pending_book"])
        if st.get("pos"):
            st["last_action"] = str(st.get("last_action") or "") + " (LTP)"
            changed = True

    _pos_view(st, Cs, ltp)
    return changed


def ltp_only_scan(cfg=None):
    """Refresh prices only while no full scan is active or waiting."""
    import data
    if RUNTIME.get("scanning") or not _LTP_RUN.acquire(blocking=False):
        return -1
    epoch = _STOP_EPOCH
    _TL.epoch = epoch
    try:
        if RUNTIME.get("scanning") or not _WORK_GATE.acquire(blocking=False):
            return -1
        try:
            if _stop_requested() or RUNTIME.get("scanning"):
                return -1
            expected = getattr(_TL, "session_profile", None)
            if expected is not None and (expected != (_PROFILE_NAME or "") or
                                         (_PROFILE_NAME and not RUNTIME.get("running"))):
                return -1
            _TL.src = "LTP poll"
            data._CANCEL.check = lambda: epoch != _STOP_EPOCH or bool(RUNTIME.get("scanning"))
            return _ltp_only_scan(cfg)
        finally:
            data._CANCEL.check = None
            _WORK_GATE.release()
    finally:
        _TL.src = None
        _TL.epoch = None
        _LTP_RUN.release()


def _note_ltp_wait(sec):
    now = time.time()
    _LTP_WAITS.append((now, float(sec)))
    while _LTP_WAITS and now - _LTP_WAITS[0][0] > 600:
        _LTP_WAITS.pop(0)
    RUNTIME["ltp_wait_max"] = round(max(w for _, w in _LTP_WAITS), 1)


def _ltp_only_scan(cfg=None):
    t0 = time.time()
    cfg = cfg or RUNTIME.get("config") or DEFAULTS
    symbols = parse_symbols(cfg.get("symbols"), exch_of(cfg), cfg.get("symbol_exch"))
    if not symbols:
        return 0
    download_batch, get_live_ltp = _data()
    stats = {}
    ltps, ages = _ltp_from_1m(download_batch, symbols, stats=stats, priority=True)
    if _stop_requested() or RUNTIME.get("scanning"): return -1
    missing = [s for s in symbols if s not in ltps]
    if missing:
        try:
            tw = time.time()
            with DATA_LOCK:                    # yfinance — never two downloads at once
                if _stop_requested() or RUNTIME.get("scanning"): return -1
                stats["wait"] = stats.get("wait", 0.0) + (time.time() - tw)
                fb = get_live_ltp(missing) or {}
            for k, v in fb.items():
                if v is not None:
                    ltps[k] = float(v)
        except Exception:
            pass
    if _stop_requested() or RUNTIME.get("scanning"): return -1
    fetch_at = time.time()
    C = _make_C(cfg)
    alloc_amt, alloc_cap = _alloc_maps(cfg)
    t_now = _utcnow().astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    n, changed = 0, False
    t_lock = time.time()
    with RUN_LOCK:
        run_wait = time.time() - t_lock
        for sym in symbols:
            if _stop_requested() or RUNTIME.get("scanning"): break
            st = STATE.get(sym)
            if sym not in ltps or not st:
                continue
            try:
                old_pos = copy.deepcopy(st.get("pos")) if _PROFILE_NAME else None
                tick_changed = _ltp_tick(st, sym, float(ltps[sym]), ages.get(sym), C, cfg,
                                         alloc_amt, alloc_cap, t_now, fetch_at=fetch_at)
                changed = tick_changed or changed
                if _PROFILE_NAME and (tick_changed or st.get("pos") != old_pos):
                    _save()
            except Exception as e:             # one bad symbol must not stop the rest
                st["ltp_error"] = "%s: %s" % (type(e).__name__, e)
                traceback.print_exc()
            else:
                st["ltp_error"] = ""
            n += 1
        RUNTIME["ltp_poll_at"] = time.time()
        if changed or (_PROFILE_NAME and n):
            _save()
    wait = stats.get("wait", 0.0) + run_wait
    _note_ltp_wait(wait)
    RUNTIME["ltp_stats"] = {"at": time.time(), "took_s": round(time.time() - t0, 1),
                            "wait_s": round(wait, 1), "n": n}
    RUNTIME["ltp_error"] = ""
    return n


@paper_live_bp.route("/api/paper-live/ltp", methods=["POST"])
def api_ltp_only():
    body = request.get_json(silent=True) or {}
    if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
    if _PROFILE_NAME and not RUNTIME.get("running"):
        return jsonify({"ok": False, "error": "Profile is stopped; press Start before LTP trade processing"})
    # while the engine runs, act with ITS settings (what ✔ Apply saved), not with
    # unsaved edits in the page — the LTP poll and the full scan must agree
    if RUNTIME.get("running") and RUNTIME.get("config"):
        cfg = RUNTIME["config"]
    else:
        cfg = cfg_of(request.get_json(silent=True) or {})
    try:
        _TL.session_profile = _PROFILE_NAME or ""
        n = ltp_only_scan(cfg)
    except Exception as e:
        traceback.print_exc()
        RUNTIME["ltp_error"] = "%s: %s" % (type(e).__name__, e)
        RUNTIME["ltp_error_at"] = time.time()
        return jsonify({"ok": False, "error": RUNTIME["ltp_error"]})
    finally:
        _TL.session_profile = None
    return jsonify({"ok": True, "count": max(n, 0), "busy": n < 0})


@paper_live_bp.route("/api/paper-live/scan", methods=["POST"])
def api_scan():
    with _PROFILE_LOCK:
        return _api_scan_locked()


def _api_scan_locked():
    body = request.get_json(silent=True) or {}
    if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
    cfg = cfg_of(body)
    if not RUNTIME["running"]:
        RUNTIME["config"] = cfg      # so /status shows the mode actually used
    try:
        kind = "preview" if _PROFILE_NAME and not RUNTIME.get("running") else "manual"
        res = scan_once(cfg, kind=kind)
        res["preview"] = kind == "preview"
        return jsonify(res)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


# ── LOGIC CONTRACT: the logic check must know every rule the engine acts on ─
# When you add or change trading logic (anything that decides an entry, an
# exit, a price, a level or a setting):
#   1. register a new exit reason / entry kind / fill rule / setting below,
#   2. teach symbol_health() / global_health() what "working" looks like for it,
#      AND what a legitimate block looks like, so correct behaviour is not flagged,
#   3. add a test to tests/test_paper_live_health.py,
#   4. re-stamp HEALTH_REVIEWED:  cd web && python paper_live_engine.py --logic-stamp --write
# Until step 4 the page's Logic check says "trading logic changed since the logic
# check was last reviewed" and tests/test_paper_live_health.py::LogicContract fails
# — on purpose: a rule the check does not know is treated as a possible problem.
LOGIC_VERSION = "v20"

EXIT_REASONS = {
    "stop": "fixed stop loss hit",
    "trail_stop": "trailing stop hit",
    "target": "profit target hit",
    "opp_signal": "the opposite WaveTrend signal broke out",
    "eod": "replay / verify only: closed at the end of the data",
}
ENTRY_KINDS = {
    "closed_bar": "a CLOSED candle broke the trigger level (full scan)",
    "ltp_break": "the live price broke a pending level (full scan or LTP poll)",
    "ltp_flip": "LTP poll: the opposite level broke -> exit and reverse at once",
    "already_triggered": "'Allow already-triggered': the latest cross broke out on an older "
                         "candle, price within Max diff % -> market entry",
    "replay": "replay / verify only",
}
FILL_MODES = {
    "level": "trigger level ± buffer — the backtest's resting stop order",
    "market": "'Market if already past': the live price if past the level, else the level",
    "market_now": "the live price, whichever side of the level (already-triggered entries)",
}
_NO_LIVE = "no live effect the check can see (same rule as the backtest)"
_SIZING = "position size / costs only — nothing to check live"
_TRAIL = "trailing stop: the past-stop check uses the stop shown in the SL column"
_FILTER = "entry filter: a filtered signal never becomes pending, so nothing is missed"
SETTINGS_KNOWN = {          # every setting -> how the logic check takes it into account
    "symbols": "orphan and not-yet-scanned symbols",
    "verify_symbol": "Verify tool only — no live effect",
    "exchange": "ticker suffix -> which market clock each symbol follows",
    "symbol_exch": "per-row exchange badge -> which market clock each symbol follows",
    "market": "market-hours clock of the polling loop (loop-stuck check)",
    "interval": "candle length: stale feed, late entry / exit, slow-scan limits, TF column "
                "(tf_stale / tf_mixed)",
    "period": "candle history length — " + _NO_LIVE,
    "wt_n1": "WaveTrend input — levels are checked against the LTP",
    "wt_n2": "WaveTrend input — levels are checked against the LTP",
    "wt_pending_rule": "which crosses stay pending — pending-break checks; new paper configs default to same_dir, explicit settings win",
    "buffer_pts": "break / opposite-level checks include the buffer",
    "wait_bars": "pending expiry — " + _NO_LIVE,
    "profit_target_pct": "past-target and target-side checks",
    "profit_target_mode": "past-target and target-side checks",
    "stop_loss_pct": "past-stop check",
    "trailing_enabled": _TRAIL,
    "trailing_start_pct": _TRAIL,
    "trailing_step_pct": _TRAIL,
    "trailing_tighten_enabled": _TRAIL,
    "trailing_tighten_at": _TRAIL,
    "trailing_tighten_step": _TRAIL,
    "side_mode": "a blocked side is a note, not a missed entry",
    "heikin_ashi": "candle type — fast Heikin-Ashi self-test",
    "entry_exec": "fill-price check",
    "allow_already_triggered": "the latest cross within Max diff % must be entered "
                               "(missed_already_triggered), at market (fill_rule)",
    "already_triggered_max_pct": "the latest cross within Max diff % must be entered "
                                 "(missed_already_triggered), at market (fill_rule)",
    "alloc": _SIZING,
    "sizing_mode": _SIZING,
    "position_amount": _SIZING,
    "fixed_qty": _SIZING,
    "position_pct": _SIZING,
    "capital": _SIZING,
    "commission_pct": _SIZING,
    "slippage_pct": "fill-price check allows for the slippage booked at entry",
    "exit_on_opposite": "opposite-level check only when ticked",
    "fno_mode": "F&O 15:15 filter — " + _NO_LIVE,
    "fno_book": "F&O 15:15 filter — " + _NO_LIVE,
    "fno_tab": "F&O 15:15 filter — " + _NO_LIVE,
    "wt_filter_enabled": _FILTER,
    "wt_buy_max": _FILTER,
    "wt_sell_min": _FILTER,
    "htf_filter_enabled": _FILTER,
    "rsi_filter_enabled": _FILTER,
    "rsi_period": _FILTER,
    "vol_filter_enabled": _FILTER,
    "vol_mult": _FILTER,
    "vol_lookback": _FILTER,
    "poll_seconds": "loop-stuck and slow-scan limits",
    "poll_when_market_closed": "loop-stuck check only when the loop should be scanning",
    "auto_poll": "loop-stuck check only when the loop should be scanning",
    "allow_stale_entries": "a stale-feed block is a note, not a missed entry",
}
HEALTH_CODES = {            # every check the logic check can report
    "orphan": "removed symbol still holds a position",
    "unscanned": "not scanned yet",
    "no_data": "Yahoo returned no candles",
    "no_data_all": "no candles for any symbol (connection / Yahoo)",
    "crash": "the scan crashed on a symbol",
    "status": "unusual row status",
    "ltp_crash": "the LTP poll crashed on a symbol",
    "stale_feed": "candles lag while the market is open",
    "mkt_closed": "market closed",
    "candle_ltp": "no live quote, LTP is a candle close",
    "old_quote": "live quote is old",
    "ltp_mixup": "LTP far from the symbol's own candle",
    "trig_mixup": "trigger level far from the LTP",
    "entry_mixup": "entry far from the LTP",
    "row_stale_view": "open position not shown in the row",
    "target_side": "target on the wrong side of the entry",
    "past_target": "LTP past the target, still open",
    "past_stop": "LTP past the stop, still open",
    "opp_broken": "opposite level broken, still open",
    "late_entry": "entry more than a candle late",
    "late_exit": "exit more than a candle late",
    "side_block": "break skipped: side mode",
    "no_fill": "break skipped: no fill",
    "stale_block": "break skipped: stale feed",
    "missed_entry": "pending level broken, no entry",
    "missed_already_triggered": "already-triggered latest cross within Max diff %, no entry",
    "tf_stale": "row not rescanned since the Interval changed",
    "tf_mixed": "open position from another timeframe",
    "sheet_out": "Live Trade: Google Sheet output off / failing / no sheet name",
    "dup_ltp": "identical LTP on two symbols",
    "unknown_logic": "an entry / exit / fill rule the check does not know",
    "exit_price": "exit not booked at its rule price",
    "exit_not_reached": "exit booked at a price the LTP had not reached",
    "pnl_math": "trade P&L does not add up",
    "side_violation": "entry on a side the Side setting blocked",
    "repeat_signal": "the same signal traded twice",
    "flip_level": "LTP flip not reversed at the same level",
    "fill_rule": "already-triggered entry not booked at market",
    "fill_price": "entry price does not follow its fill rule",
    "old_fill": "position booked by the old already-triggered rule",
    "loop_error": "engine loop error",
    "download": "candle download failed",
    "profile_save": "profile save or restore failed; trading stops",
    "scan_hung": "a scan is stuck",
    "loop_stuck": "the polling loop stopped scanning",
    "slow_scan": "full scan slower than the poll / candle",
    "ltp_paused_scan": "LTP poll paused while a full scan is running",
    "ltp_off": "LTP poll off with open positions",
    "ltp_failed": "LTP poll request failed",
    "ltp_stalled": "no LTP poll finished recently",
    "ltp_blocked": "LTP poll had to wait for Yahoo / the scan",
    "old_run": "a stopped run is still finishing",
    "aborted": "the last scan was stopped part-way",
    "logic_changed": "trading logic changed since the check was reviewed",
    "unknown_setting": "a setting the check does not know",
    "fast_math": "fast maths switched off (did not match the original)",
    "health_crash": "the logic check itself crashed",
}
# functions whose code decides trades — any edit must be reviewed by the check
LOGIC_FUNCS = {
    "data.py": ("cancelled", "download", "download_batch", "get_live_ltp"),
    "paper_engine.py": (
        "scan_once", "ltp_only_scan", "api_stop", "norm_symbol", "parse_symbols", "cfg_of", "_alloc_maps", "_make_C",
        "fno_should_apply", "apply_fno", "_ha_arrays", "_wt_arrays", "_heikin_ashi",
        "_wt_compute", "_ltp_from_1m", "_open_pos", "_close_pos", "_check_price_exits",
        "_sig_id", "_prep", "_pending_signal", "_pending_book", "wt_action_text",
        "_pre_run", "live_step", "_stop_requested", "_market_open_now", "_pos_view",
        "_chunks", "_download_candles", "_scan_once", "_loop", "api_update_inner",
        "_ltp_tick", "_ltp_only_scan", "api_ltp_only", "_latest_break",
        "_already_triggered", "_save", "_load", "_profile_snapshot", "_profile_restore_startup",
        "api_session_profiles", "api_start", "_api_start_locked", "api_scan", "_api_scan_locked", "api_update", "api_reset", "api_allocations", "global_health"),
    "paper_profiles.py": ("_path", "_validate", "load", "save", "_atomic_write", "load_backup"),
    "backtest.py": ("_qty_for", "_target_px", "_trailing_sl_price", "_rsi_series",
                    "_load_htf_trend", "detect_wt_events", "_apply_filters"),
    "indicators/common.py": ("candles_to_df", "heikin_ashi", "apply_fno_filter"),
    "indicators/wavetrend.py": ("_compute_wavetrend", "_rsi", "compute"),
}
_FP_LABEL = {"paper_profiles.py": "profiles", "paper_engine.py": "engine", "data.py": "data", "backtest.py": "backtest",
             "indicators/common.py": "common", "indicators/wavetrend.py": "wavetrend"}
HEALTH_REVIEWED = {  # stamped by: python paper_live_engine.py --logic-stamp
    'backtest:_apply_filters': '3ea422ebf8',
    'backtest:_load_htf_trend': 'd55e2aae73',
    'backtest:_qty_for': 'cbfba349ec',
    'backtest:_rsi_series': 'c4703abe77',
    'backtest:_target_px': '35ad1163fe',
    'backtest:_trailing_sl_price': '14b8e09ccd',
    'backtest:detect_wt_events': '05340f2729',
    'common:apply_fno_filter': 'd2852eb453',
    'common:candles_to_df': 'dda05dc568',
    'common:heikin_ashi': '6041605cf2',
    'data:cancelled': '4a6055fbd2',
    'data:download': '150a257f6e',
    'data:download_batch': 'a052c0544d',
    'data:get_live_ltp': '833c269f9c',
    'engine:SETTINGS': '2c2f57df06',
    'engine:_alloc_maps': '46d3860feb',
    'engine:_already_triggered': '69a5a6e16f',
    'engine:_api_scan_locked': '46e62566df',
    'engine:_api_start_locked': 'b9a4c11444',
    'engine:_check_price_exits': '87e164e290',
    'engine:_chunks': 'c1b462badc',
    'engine:_close_pos': '9dada0c940',
    'engine:_download_candles': 'ee8bfdcb9b',
    'engine:_ha_arrays': '4ab7607ece',
    'engine:_heikin_ashi': '8ffad86338',
    'engine:_latest_break': '5539b0530a',
    'engine:_load': 'be15b6a3f8',
    'engine:_loop': 'de36fea4b0',
    'engine:_ltp_from_1m': '9118a91307',
    'engine:_ltp_only_scan': '6bec62d04c',
    'engine:_ltp_tick': 'e0b855df1f',
    'engine:_make_C': '93938f2061',
    'engine:_market_open_now': 'fc1a1db535',
    'engine:_open_pos': '68c6f6a49d',
    'engine:_pending_book': '67c3bf3ae5',
    'engine:_pending_signal': 'f588238935',
    'engine:_pos_view': '32280b4b14',
    'engine:_pre_run': 'e809718243',
    'engine:_prep': 'f46beb9a40',
    'engine:_profile_restore_startup': '57a93ab1cc',
    'engine:_profile_snapshot': '9c71324afe',
    'engine:_save': 'eb3d8c6009',
    'engine:_scan_once': 'deb58d2fce',
    'engine:_sig_id': '6280d01f97',
    'engine:_stop_requested': 'bcc4a2a2dd',
    'engine:_wt_arrays': 'eac3feaf4f',
    'engine:_wt_compute': 'e21963dd46',
    'engine:api_allocations': 'b0503f9227',
    'engine:api_ltp_only': '6192e8f1be',
    'engine:api_reset': '406ce23048',
    'engine:api_scan': '8dd1cb830a',
    'engine:api_session_profiles': '8253a821e5',
    'engine:api_start': '89b59601b0',
    'engine:api_stop': '8ee4d40163',
    'engine:api_update': '498f9ce1d3',
    'engine:api_update_inner': '2abf928733',
    'engine:apply_fno': '450791dd52',
    'engine:cfg_of': '9418197d71',
    'engine:fno_should_apply': '16938225cf',
    'engine:global_health': '2d2ed04b39',
    'engine:live_step': 'a2c2d6d204',
    'engine:ltp_only_scan': 'facd0663b8',
    'engine:norm_symbol': 'd82aa123cd',
    'engine:parse_symbols': '816a18a470',
    'engine:scan_once': '4245780257',
    'engine:wt_action_text': 'c1c6543e42',
    'profiles:_atomic_write': '1482cb03a2',
    'profiles:_path': '68ba1ccb3f',
    'profiles:_validate': '7da85eb003',
    'profiles:load': '9f307faaf2',
    'profiles:load_backup': 'cebc282307',
    'profiles:save': 'd8d874cb08',
    'wavetrend:_compute_wavetrend': 'b81f5029f3',
    'wavetrend:_rsi': '9a725f5c79',
    'wavetrend:compute': '339f7d7d7f',
}  # end HEALTH_REVIEWED
_LOGIC_FP = {}


def _src_fingerprints(rel, names):
    """Short hash per top-level function: its source without blank / comment-only
    lines (plain text, so it is the same on every Python version)."""
    import ast
    import hashlib
    lab = _FP_LABEL.get(rel, rel)
    try:
        text = (Path(__file__).resolve().parent / rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
    except Exception:
        return {"%s:%s" % (lab, n): "unreadable" for n in names}
    lines = text.splitlines()
    spans = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans[node.name] = (a, getattr(node, "end_lineno", None) or a)
    out = {}
    for n in names:
        if n not in spans:
            out["%s:%s" % (lab, n)] = "missing"
            continue
        a, b = spans[n]
        body = [ln.rstrip() for ln in lines[a - 1:b]]
        body = [ln for ln in body if ln.strip() and not ln.strip().startswith("#")]
        out["%s:%s" % (lab, n)] = hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()[:10]
    return out


def logic_fingerprint():
    """{"engine:live_step": "3f2a9c01de", ...} for the code as it is on disk now."""
    if not _LOGIC_FP:
        import hashlib
        fp = {}
        for rel, names in LOGIC_FUNCS.items():
            fp.update(_src_fingerprints(rel, names))
        fp["engine:SETTINGS"] = hashlib.sha1(
            ",".join(sorted(DEFAULTS)).encode("utf-8")).hexdigest()[:10]
        _LOGIC_FP.update(fp)
    return dict(_LOGIC_FP)


def logic_changes():
    """Names whose code differs from what the logic check was last reviewed against."""
    fp = logic_fingerprint()
    return sorted(k for k in set(fp) | set(HEALTH_REVIEWED) if fp.get(k) != HEALTH_REVIEWED.get(k))


def health_coverage():
    ch = logic_changes()
    return {"version": LOGIC_VERSION, "checks": len(HEALTH_CODES),
            "entry_kinds": len(ENTRY_KINDS), "exit_reasons": len(EXIT_REASONS),
            "fill_rules": len(FILL_MODES), "settings": len(SETTINGS_KNOWN),
            "reviewed": not ch, "changed": ch[:12],
            "fast_math": (bool(FAST_MATH.get("ha") and FAST_MATH.get("wt"))
                          if FAST_MATH.get("checked") else None)}      # None = not run yet


# ── logic health: the self-check behind the "Logic check" banner ───────────
HEALTH_RANK = {"ok": 0, "info": 1, "warn": 2, "error": 3}
_HEALTH_SEEN = {}            # "SYM|code" -> epoch seconds the problem was first seen


def _sym_market(sym):
    """Which market clock one symbol follows (one list can mix NSE / US / FX)."""
    s = str(sym or "").upper()
    if s.endswith(".NS") or s.endswith(".BO") or s.startswith("^NSE") or s.startswith("^BSE"):
        return "IN"
    if s.endswith("=X") or s.endswith("_FX") or s.endswith("=F"):
        return "FX"
    if s.endswith("-USD") or s.endswith("_CRYPTO"):
        return "CRYPTO"
    return "US"


def _gap_pct(a, b):
    """abs(a - b) / b in %, or None unless both are positive numbers."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if not (a > 0 and b > 0):
        return None
    return abs(a - b) / b * 100.0


def _parse_t(v):
    try:
        return __import__("datetime").datetime.strptime(str(v)[:16], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _px(sym, v):
    if v is None:
        return "—"
    try:
        return ("%.5f" if str(sym).endswith("=X") else "%.2f") % float(v)
    except (TypeError, ValueError):
        return str(v)


def _late_min(break_t, done_t, tf_min):
    """Minutes between the breakout candle CLOSING and the engine acting on it."""
    a, b = _parse_t(break_t), _parse_t(done_t)
    if not a or not b:
        return None
    return (b - a).total_seconds() / 60.0 - tf_min


def _recent(t, now_t, minutes):
    a, b = _parse_t(t), _parse_t(now_t)
    return bool(a and b and 0 <= (b - a).total_seconds() <= minutes * 60)


def _expected_fill(pos):
    """The entry price a position MUST have under its own recorded fill rule."""
    try:
        sign = 1 if float(pos["sign"]) > 0 else -1
        px = float(pos["level"]) + sign * float(pos.get("buffer") or 0.0)
        slip = float(pos.get("slip_pct") or 0.0)
        fill, ltp = pos.get("fill"), pos.get("ltp_at_entry")
    except (KeyError, TypeError, ValueError):
        return None
    if fill == "market_now" and ltp is not None:
        px = float(ltp)
    elif fill == "market" and ltp is not None:
        px = max(px, float(ltp)) if sign > 0 else min(px, float(ltp))
    elif fill != "level":
        return None
    return px * (1 + sign * slip / 100.0)


def symbol_health(sym, st, cfg, C=None, listed=True, mk_open=None, now_t=None,
                  last_trade=None):
    """Sanity-check one monitor row. Pure function of the row state (no Yahoo),
    so every rule is unit-tested. Returns [{"lvl", "code", "msg"}]."""
    st = st or {}
    cfg = cfg or {}
    C = C or _make_C(cfg)
    out = []

    def add(lvl, code, msg):
        out.append({"lvl": lvl, "code": code, "msg": msg})

    tf_min = TF_MINUTES.get(str(cfg.get("interval") or "15m"), 60)
    far = 20.0 if tf_min < 24 * 60 else 50.0
    pos = st.get("pos") if isinstance(st.get("pos"), dict) else None
    status = str(st.get("status") or "")
    if mk_open is None:
        mk_open = _market_open_now({"market": _sym_market(sym)})
    ltp = st.get("ltp")
    ltp = float(ltp) if isinstance(ltp, (int, float)) and ltp > 0 else None

    if not listed and pos:
        add("error", "orphan",
            "removed from your symbol list but still holds %s — it is no longer scanned, "
            "so its target / stop / opposite exit will never fire. Re-add the symbol, or "
            "press Reset." % (st.get("position") or "a position"))
        return out
    if not status:
        add("info", "unscanned", "waiting for its first full scan")
        return out
    if status == "No Data":
        add("error", "no_data", "Yahoo returned no candles — check the symbol name and "
                                "its exchange badge")
        return out
    if status.startswith("Error"):
        add("error", "crash", "the scan crashed on this symbol — %s"
            % status[len("Error:"):].strip()[:160])
        return out
    if status != "OK":
        add("warn", "status", status)
        return out
    if st.get("ltp_error"):
        add("error", "ltp_crash", "the LTP poll crashed on this symbol — %s"
            % str(st["ltp_error"])[:160])

    # feed
    if st.get("stale"):
        if mk_open:
            add("warn", "stale_feed",
                "last closed candle is %s min old while its market is open — the Yahoo "
                "feed is lagging, so new entries are blocked" % _int(st.get("bar_age_min")))
        else:
            add("info", "mkt_closed", "its market is closed — no new entries%s" % (
                "; the open %s is still watched" % (
                    "LONG" if _num(pos.get("sign"), 0) > 0 else "SHORT") if pos else ""))
    if mk_open and st.get("ltp_src") == "last close":
        add("warn", "candle_ltp", "no live 1-minute quote — LTP is the last candle close "
                                  "(up to one candle old)")
    age = st.get("ltp_age_min")
    if (mk_open and st.get("ltp_src") == "live" and isinstance(age, (int, float))
            and age > max(3, tf_min)):
        add("warn", "old_quote", "live quote is %d min old — Yahoo is slow for this symbol"
            % age)

    # prices that cannot belong to this symbol
    g = _gap_pct(ltp, st.get("bar_close"))
    if g is not None and g > 5:
        add("error", "ltp_mixup",
            "LTP %s is %.1f%% away from this symbol's own latest candle (%s) — prices look "
            "mixed up with another symbol" % (_px(sym, ltp), g, _px(sym, st.get("bar_close"))))
    g = _gap_pct(st.get("trig_lvl"), ltp)
    if g is not None and g > far:
        add("error", "trig_mixup",
            "Trig Lvl %s is %.0f%% away from LTP %s — the level came from the wrong data"
            % (_px(sym, st.get("trig_lvl")), g, _px(sym, ltp)))

    # open position
    if pos:
        sign = 1 if _num(pos.get("sign"), 0) > 0 else -1
        side = "LONG" if sign > 0 else "SHORT"
        entry = _num(pos.get("entry"), 0.0)
        g = _gap_pct(ltp, entry)
        if g is not None and g > far:
            add("error", "entry_mixup",
                "entry %s vs LTP %s differ by %.0f%% — this position was opened on wrong "
                "data; Reset it" % (_px(sym, entry), _px(sym, ltp), g))
        if str(st.get("position") or "—") == "—":
            add("warn", "row_stale_view", "a %s is open but this row has not refreshed" % side)
        tgt, stp = pos.get("target"), pos.get("stop")
        if (isinstance(tgt, (int, float)) and entry
                and ((sign > 0 and tgt <= entry) or (sign < 0 and tgt >= entry))):
            add("error", "target_side", "target %s is on the wrong side of the %s entry %s"
                % (_px(sym, tgt), side, _px(sym, entry)))
        elif ltp and isinstance(tgt, (int, float)) and (
                (sign > 0 and ltp >= tgt) or (sign < 0 and ltp <= tgt)):
            add("warn", "past_target", "LTP %s is past the target %s but the %s is still open"
                % (_px(sym, ltp), _px(sym, tgt), side))
        if ltp and isinstance(stp, (int, float)) and (
                (sign > 0 and ltp <= stp) or (sign < 0 and ltp >= stp)):
            add("warn", "past_stop", "LTP %s is past the stop %s but the %s is still open"
                % (_px(sym, ltp), _px(sym, stp), side))
        ol = st.get("opp_lvl")
        if ltp and isinstance(ol, (int, float)) and C.get("exit_on_opposite"):
            buf = _num(C.get("buffer_pts"), 0.0)
            if (sign > 0 and ltp < ol - buf) or (sign < 0 and ltp > ol + buf):
                add("warn", "opp_broken",
                    "LTP %s broke the opposite level %s but the %s is still open — the LTP "
                    "poll flips it within seconds; with LTP poll off it waits for the "
                    "breakout candle to close" % (_px(sym, ltp), _px(sym, ol), side))
        late = _late_min(pos.get("break_t"), pos.get("entry_t"), tf_min)
        if late is not None and late > tf_min and _recent(pos.get("entry_t"), now_t, 60):
            add("warn", "late_entry",
                "entry came %d min after the breakout candle closed — full scans are slower "
                "than the candle" % late)

        # was the position booked the way its own entry rule says?
        kind, fill = pos.get("kind"), pos.get("fill")
        if kind is not None and kind not in ENTRY_KINDS:
            add("warn", "unknown_logic",
                "opened by entry rule '%s', which the logic check does not know — it cannot "
                "verify this position until the check is updated" % kind)
        if fill is not None and fill not in FILL_MODES:
            add("warn", "unknown_logic",
                "booked with fill rule '%s', which the logic check does not know" % fill)
        elif kind == "already_triggered" and fill and fill != "market_now":
            add("error", "fill_rule",
                "already-triggered entry was booked with the '%s' fill — it must be the market "
                "price at entry (%s)" % (fill, _px(sym, pos.get("ltp_at_entry"))))
        exp = _expected_fill(pos)
        if exp and entry and abs(entry - exp) / exp * 100.0 > 0.01:
            add("error", "fill_price",
                "entry %s does not follow its own fill rule '%s' — expected %s (level %s, LTP "
                "at entry %s)" % (_px(sym, entry), fill, _px(sym, exp),
                                  _px(sym, pos.get("level")), _px(sym, pos.get("ltp_at_entry"))))
        why = str(pos.get("why") or "")
        tl = st.get("trig_lvl")
        if (fill is None and why.startswith("already-triggered")
                and isinstance(tl, (int, float)) and tl > 0 and entry):
            slip = _num(C.get("slippage_pct"), 0.0)
            if abs(entry / (1 + sign * slip / 100.0) - tl) / tl < 1e-6:
                m = re.search(r"\(([\d.]+)%\)\s*$", why)
                add("info", "old_fill",
                    "opened by the old already-triggered rule: booked at the trigger level %s "
                    "although the market was %s%% away — this position's P&L reads worse than "
                    "a real market order (new entries use the market price)"
                    % (_px(sym, tl), m.group(1) if m else "a little"))

    # a live break that did not become a trade
    if st.get("signal_state") == "live" and not pos:
        wside = st.get("wt_side")
        if ((C.get("side_mode") == "long" and wside == "sell")
                or (C.get("side_mode") == "short" and wside == "buy")):
            add("info", "side_block", "pending level broken, but side mode = %s only"
                % C.get("side_mode"))
        elif st.get("no_fill"):
            add("info", "no_fill", "pending level broken, no entry: %s" % st["no_fill"])
        elif st.get("stale") and not _bool(cfg.get("allow_stale_entries"), False):
            add("info", "stale_block", "pending level broken, entries blocked by the stale feed")
        else:
            add("warn", "missed_entry", "LTP broke the pending level %s but no position "
                                        "was opened" % _px(sym, st.get("trig_lvl")))

    # "Allow already-triggered": the latest cross broke out on an older candle, the price
    # was within Max diff % at the scan and nothing blocks it -> it must be entered.
    # (v14 never looked at it while another signal was still waiting — CDSL.)
    lp = st.get("pending") or {}
    sl = st.get("scan_ltp")
    if (not pos and _bool(cfg.get("allow_already_triggered"), False) and st.get("latest_took")
            and lp.get("dir") in ("buy", "sell") and isinstance(sl, (int, float)) and sl > 0
            and isinstance(lp.get("level"), (int, float)) and lp["level"] > 0):
        sid = "%s|%s" % (lp["dir"], lp.get("valid_t") or "")
        dpct = abs(sl - lp["level"]) / lp["level"] * 100.0
        mx = _num(cfg.get("already_triggered_max_pct"), 1.0)
        blocked = ((C.get("side_mode") == "long" and lp["dir"] == "sell")
                   or (C.get("side_mode") == "short" and lp["dir"] == "buy")
                   or sid == st.get("last_sig") or sid in (st.get("sigs_traded") or [])
                   or (st.get("stale") and not _bool(cfg.get("allow_stale_entries"), False))
                   or str(st.get("last_action") or "").startswith("EXIT ")
                   or "cash" in str(st.get("no_fill") or ""))
        if dpct <= mx + 1e-9 and not blocked:
            add("warn", "missed_already_triggered",
                "latest cross %s @ %s broke out at %s and the price was %.2f%% away (within Max "
                "diff %g%%) with 'Allow already-triggered' on — but no position was opened"
                % (lp["dir"].upper(), _px(sym, lp["level"]),
                   str(st.get("latest_break_t") or "")[11:16] or "?", dpct, mx))

    # TF column: the row and its position must run on the current Interval
    cur_tf = str(cfg.get("interval") or "")
    if cur_tf and st.get("tf") and st["tf"] != cur_tf:
        add("warn", "tf_stale", "this row still shows its %s signal — not rescanned since the "
                                "Interval became %s" % (st["tf"], cur_tf))
    if cur_tf and pos and pos.get("tf") and pos["tf"] != cur_tf:
        add("warn", "tf_mixed",
            "this %s was opened on %s, but signals now run on %s (the Interval was changed while "
            "it was open) — its opposite-signal exit now follows %s signals, unlike either backtest"
            % ("LONG" if _num(pos.get("sign"), 0) > 0 else "SHORT", pos["tf"], cur_tf, cur_tf))

    # did the last exit react late?
    lt = last_trade or {}
    late = _late_min(lt.get("break_t"), lt.get("exit_t"), tf_min)
    if late is not None and late > tf_min and _recent(lt.get("exit_t"), now_t, 60):
        add("warn", "late_exit", "last %s exit came %d min after the breakout candle closed"
            % (lt.get("reason") or "", late))
    if (lt.get("reason") and lt["reason"] not in EXIT_REASONS
            and _recent(lt.get("exit_t"), now_t, 24 * 60)):
        add("warn", "unknown_logic",
            "the last trade was closed by '%s', an exit rule the logic check does not know"
            % lt["reason"])
    return out


# ── Recent changes: every entry / exit / flip / settings change, verified ──
CHANGES = list(RUNTIME.pop("_profile_changes", []))  # profile-specific activity
RECENT_MIN = 60       # how far back "Recent changes" looks, minutes
RECENT_LIMIT = 15     # newest changes shown
RECENT_NEW_S = 300    # younger than this = NEW
_EXIT_WORDS = {"stop": "stop loss", "trail_stop": "trailing stop", "target": "target",
               "opp_signal": "opposite signal", "eod": "end of data"}


def _note_change(kind, msg):
    """Remember an engine-level change for the logic check's Recent changes."""
    CHANGES.append({"at": time.time(), "kind": kind, "msg": msg,
                    "t": _utcnow().astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")})
    del CHANGES[:-50]


def _age_s(t, now_ist):
    """Seconds since an IST 'YYYY-MM-DD HH:MM[:SS]' time (None if unreadable)."""
    dtm = __import__("datetime").datetime
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            a = IST.localize(dtm.strptime(str(t)[:n], fmt))
            return (now_ist - a).total_seconds()
        except (TypeError, ValueError):
            continue
    return None


def _money(v):
    try:
        return "%s%s" % ("+" if float(v) > 0 else "", "{:,.2f}".format(float(v)))
    except (TypeError, ValueError):
        return str(v)


def _check_exit(tr, sym, tf_min):
    """Verify one closed trade's exit. Returns (checks passed, issues)."""
    ok, out = [], []

    def add(lvl, code, msg):
        out.append({"lvl": lvl, "code": code, "msg": msg})

    reason = tr.get("reason")
    word = _EXIT_WORDS.get(reason, str(reason))
    sign = 1 if tr.get("side") == "long" else -1
    exit_px = _num(tr.get("exit"), 0.0)
    if reason not in EXIT_REASONS:
        add("warn", "unknown_logic", "closed by '%s', an exit rule the logic check does not "
                                     "know" % reason)
    rp = tr.get("rule_px")
    if isinstance(rp, (int, float)) and rp > 0 and exit_px > 0:
        slip = _num(tr.get("slip_pct"), 0.0)
        exp = rp * (1 - sign * slip / 100.0)
        if abs(exit_px - exp) / exp * 100.0 > 0.01:
            add("error", "exit_price",
                "exit booked at %s, but the %s price %s with %.2f%% slippage gives %s"
                % (_px(sym, exit_px), word, _px(sym, rp), slip, _px(sym, exp)))
        else:
            ok.append("exit booked at the %s price %s%s" % (
                word, _px(sym, rp), (" − %.2f%% slippage" % slip) if slip else ""))
        ltp = tr.get("ltp_at_exit")
        closed_bar = reason == "opp_signal" and tr.get("break_t")
        if closed_bar:
            ok.append("closed candle %s broke the opposite level" % str(tr["break_t"])[11:16])
        elif (isinstance(ltp, (int, float)) and ltp > 0
              and reason in ("stop", "trail_stop", "target", "opp_signal")):
            up = (reason == "target") == (sign > 0)          # price had to rise to it?
            tol = abs(rp) * 1e-9
            if (ltp >= rp - tol) if up else (ltp <= rp + tol):
                ok.append("LTP %s had reached it" % _px(sym, ltp))
            else:
                add("error", "exit_not_reached",
                    "%s exit booked at %s, but the LTP at that moment was %s — the price had "
                    "not reached it" % (word, _px(sym, rp), _px(sym, ltp)))
    else:
        ok.append("(recorded before v14 — exit price not re-checked)")
    qty, entry = _num(tr.get("qty"), 0.0), _num(tr.get("entry"), 0.0)
    if qty and entry and exit_px:
        gross = (exit_px - entry) * sign * qty
        costs, pnl = _num(tr.get("costs"), 0.0), _num(tr.get("pnl"), 0.0)
        if abs((gross - costs) - pnl) > 0.02 + abs(entry * qty) * 1e-9:
            add("error", "pnl_math",
                "P&L %s does not add up: (exit − entry) × qty − costs = %s"
                % (_money(pnl), _money(gross - costs)))
        else:
            ok.append("P&L %s = (exit − entry) × qty − costs {:,.2f}".format(costs) % _money(pnl))
    late = _late_min(tr.get("break_t"), tr.get("exit_t"), tf_min)
    if late is not None and late > tf_min:
        add("warn", "late_exit", "this %s exit came %d min after the breakout candle closed"
            % (word, late))
    return ok, out


def _check_entry(rec, sym, tf_min, first_use):
    """Verify one entry (open position or the entry of a closed trade)."""
    ok, out = [], []

    def add(lvl, code, msg):
        out.append({"lvl": lvl, "code": code, "msg": msg})

    sign = 1 if _num(rec.get("sign"), 0) > 0 else -1
    side = "LONG" if sign > 0 else "SHORT"
    kind, entry = rec.get("kind"), _num(rec.get("entry"), 0.0)
    if kind is None:
        ok.append("(recorded before v14 — entry rule not re-checked)")
    elif kind not in ENTRY_KINDS:
        add("warn", "unknown_logic", "opened by entry rule '%s', which the logic check does not "
                                     "know" % kind)
    else:
        ok.append("entry rule: %s" % ENTRY_KINDS[kind])
    if kind == "already_triggered" and rec.get("fill") not in (None, "market_now"):
        add("error", "fill_rule", "already-triggered entry was booked with the '%s' fill — it "
                                  "must be the market price" % rec.get("fill"))
    exp = _expected_fill(rec)
    if exp and entry:
        if abs(entry - exp) / exp * 100.0 > 0.01:
            add("error", "fill_price", "entry %s does not follow its fill rule '%s' — expected %s"
                % (_px(sym, entry), rec.get("fill"), _px(sym, exp)))
        else:
            ok.append("entry %s follows its fill rule (%s)" % (
                _px(sym, entry), FILL_MODES.get(rec.get("fill"), rec.get("fill")).split(" — ")[0]))
    sm = rec.get("side_mode")
    if sm in ("long", "short", "both"):
        if (sm == "long" and sign < 0) or (sm == "short" and sign > 0):
            add("error", "side_violation", "opened a %s although Side was '%s only' at that "
                                           "moment" % (side, sm))
        else:
            ok.append("%s allowed (Side = %s)" % (side, sm))
    sid = rec.get("sig_id")
    if sid:
        first = first_use.get((sym, sid))
        if first and str(first) < str(rec.get("entry_t") or ""):
            add("error", "repeat_signal",
                "signal %s was already traded at %s — each signal is traded once (backtest rule)"
                % (sid, str(first)[11:19]))
        else:
            ok.append("new signal %s (not traded before)" % sid)
    late = _late_min(rec.get("break_t"), rec.get("entry_t"), tf_min)
    if late is not None and late > tf_min:
        add("warn", "late_entry", "entry came %d min after the breakout candle closed" % late)
    return ok, out


def recent_changes(cfg=None, now_ist=None, window_min=RECENT_MIN, limit=RECENT_LIMIT):
    """Every entry / exit / flip in the last window_min minutes (plus Start / Stop / Apply /
    Reset), newest first, each verified. Returns (items, issues_by_symbol)."""
    cfg = cfg or RUNTIME.get("config") or DEFAULTS
    tf_min = TF_MINUTES.get(str(cfg.get("interval") or "15m"), 60)
    now_ist = now_ist or _utcnow().astimezone(IST)
    today = now_ist.strftime("%Y-%m-%d") + " "
    trades = list(TRADES[-500:])

    first_use = {}                       # (symbol, signal) -> first time it was traded
    def _use(sym, rec):
        k = (sym, (rec or {}).get("sig_id"))
        t = str((rec or {}).get("entry_t") or "")
        if k[1] and t and (k not in first_use or t < first_use[k]):
            first_use[k] = t
    for tr in trades:
        _use(tr.get("symbol"), tr.get("entry_rec") or {"sig_id": tr.get("sig_id"),
                                                       "entry_t": tr.get("entry_t")})
    state = list(STATE.items())          # snapshot: a scan may add symbols meanwhile
    for sym, st in state:
        if isinstance((st or {}).get("pos"), dict):
            _use(sym, st["pos"])

    def fresh(t):
        a = _age_s(t, now_ist)
        return a is not None and -120 <= a <= window_min * 60

    groups = {}                          # (symbol, time) -> {"exits": [...], "entries": [...]}
    for tr in trades:
        sym = tr.get("symbol") or ""
        if fresh(tr.get("exit_t")):
            groups.setdefault((sym, str(tr.get("exit_t"))), {"exits": [], "entries": []})[
                "exits"].append(tr)
        if fresh(tr.get("entry_t")):
            rec = dict(tr.get("entry_rec") or {})
            rec.setdefault("sign", 1 if tr.get("side") == "long" else -1)
            rec.setdefault("entry", tr.get("entry"))
            rec.setdefault("entry_t", tr.get("entry_t"))
            rec.setdefault("why", tr.get("entry_why"))
            rec["qty"] = tr.get("qty")
            rec["closed"] = True
            groups.setdefault((sym, str(tr.get("entry_t"))), {"exits": [], "entries": []})[
                "entries"].append(rec)
    for sym, st in state:
        p = (st or {}).get("pos")
        if isinstance(p, dict) and fresh(p.get("entry_t")):
            rec = dict(p)
            groups.setdefault((sym, str(p.get("entry_t"))), {"exits": [], "entries": []})[
                "entries"].append(rec)

    items, issues = [], {}
    for (sym, t), g in groups.items():
        checks, probs, parts, srcs = [], [], [], []
        for tr in g["exits"]:
            ok, bad = _check_exit(tr, sym, tf_min)
            checks += ["✓ " + x if not x.startswith("(") else x for x in ok]
            probs += bad
            parts.append("%s %d closed — %s @ %s (P&L %s)" % (
                "LONG" if tr.get("side") == "long" else "SHORT", _int(tr.get("qty")),
                _EXIT_WORDS.get(tr.get("reason"), tr.get("reason")),
                _px(sym, tr.get("exit")), _money(tr.get("pnl"))))
            srcs.append(tr.get("src"))
        for rec in g["entries"]:
            ok, bad = _check_entry(rec, sym, tf_min, first_use)
            checks += ["✓ " + x if not x.startswith("(") else x for x in ok]
            probs += bad
            side = "LONG" if _num(rec.get("sign"), 0) > 0 else "SHORT"
            why = str(rec.get("why") or ENTRY_KINDS.get(rec.get("kind"), "") or "").replace(today, "")
            parts.append(("new %s %d @ %s%s" if g["exits"] else "%s %d opened @ %s%s") % (
                side, _int(rec.get("qty")), _px(sym, rec.get("entry")),
                (" — " + why) if why else "") +
                (" (closed since)" if rec.get("closed") and not g["exits"] else ""))
            srcs.append(rec.get("src"))
            if rec.get("kind") == "ltp_flip":
                sgn_new = 1 if _num(rec.get("sign"), 0) > 0 else -1
                lvl_px = _num(rec.get("level"), 0.0) + sgn_new * _num(rec.get("buffer"), 0.0)
                pair = [tr for tr in g["exits"] if tr.get("reason") == "opp_signal"
                        and (1 if tr.get("side") == "long" else -1) == -sgn_new
                        and isinstance(tr.get("rule_px"), (int, float))
                        and abs(tr["rule_px"] - lvl_px) <= abs(lvl_px) * 1e-9 + 1e-12]
                if pair:
                    checks.append("✓ reversed at the same level %s" % _px(sym, rec.get("level")))
                else:
                    probs.append({"lvl": "error", "code": "flip_level",
                                  "msg": "the LTP flip opened a %s at %s without closing the "
                                         "opposite position at that same level"
                                         % (side, _px(sym, rec.get("level")))})
        src = next((x for x in srcs if x), None)
        lvl = max([p["lvl"] for p in probs], key=lambda v: HEALTH_RANK.get(v, 0)) if probs else "ok"
        age = _age_s(t, now_ist)
        items.append({"t": t, "time": t[11:19], "sym": sym, "text": " → ".join(parts),
                      "src": src, "lvl": lvl, "checks": checks, "issues": probs,
                      "age_s": int(age) if age is not None else None,
                      "new": age is not None and age <= RECENT_NEW_S, "kind": "trade"})
        for p in probs:
            issues.setdefault(sym, []).append(dict(p, msg="change at %s: %s" % (t[11:19], p["msg"])))
    for c in CHANGES:
        if fresh(c.get("t")):
            age = _age_s(c["t"], now_ist)
            items.append({"t": c["t"], "time": c["t"][11:19], "sym": "", "text": c["msg"],
                          "src": None, "lvl": "info", "checks": [], "issues": [],
                          "age_s": int(age) if age is not None else None,
                          "new": age is not None and age <= RECENT_NEW_S, "kind": c["kind"]})
    items.sort(key=lambda i: i["t"], reverse=True)
    return items[:limit], issues


def global_health(cfg, rows, now=None, ltp_poll_s=None):
    """Engine-wide checks (loop alive, scan speed, errors, LTP poll)."""
    now = now or time.time()
    cfg = cfg or {}
    out = []

    def add(lvl, code, msg):
        out.append({"lvl": lvl, "code": code, "msg": msg})

    tf = str(cfg.get("interval") or "15m")
    tf_min = TF_MINUTES.get(tf, 60)
    poll = _int(cfg.get("poll_seconds"), 0) or POLL_FOR_TF.get(tf, 120)
    scan_s = _num(RUNTIME.get("scan_ms"), 0) / 1000.0
    running = bool(RUNTIME.get("running"))
    scanning = bool(RUNTIME.get("scanning"))
    mk_open = _market_open_now(cfg) if cfg else True

    if _PROFILE_ERROR:
        add("error", "profile_save", _PROFILE_ERROR + " — profile trading is stopped; restore or fix storage before restarting")

    # the logic contract: does this check still know every rule the engine acts on?
    changed = logic_changes()
    if changed:
        add("warn", "logic_changed",
            "trading logic changed since this logic check was last reviewed (%s%s) — the check "
            "may not know the new rule yet, so it can miss a problem or flag correct "
            "behaviour. Ask for the logic check to be updated (developer: update the checks, "
            "then python paper_live_engine.py --logic-stamp --write)"
            % (", ".join(changed[:6]), " …" if len(changed) > 6 else ""))
    new_keys = [k for k in DEFAULTS if k not in SETTINGS_KNOWN]
    if new_keys:
        add("warn", "unknown_setting",
            "setting(s) %s are new to the logic check — it does not know how they change "
            "the trading yet" % ", ".join(new_keys[:6]))
    if FAST_MATH.get("checked") and not (FAST_MATH.get("ha") and FAST_MATH.get("wt")):
        add("info", "fast_math",
            "fast %s switched off: it no longer matches the original in indicators/ exactly, "
            "so the slower original is used (results stay correct, scans are slower)"
            % (FAST_MATH.get("why") or "maths"))

    if RUNTIME.get("last_error"):
        add("error", "loop_error", "engine loop error: %s" % str(RUNTIME["last_error"])[:200])
    if RUNTIME.get("download_error"):
        add("error", "download", str(RUNTIME["download_error"]))
    started = _num(RUNTIME.get("scan_started"), 0)
    if scanning and started and now - started > max(300.0, 3 * scan_s):
        add("error", "scan_hung", "the current scan has been running for %d min — Yahoo looks "
                                  "stuck; press ■ Stop, then ▶ Start" % ((now - started) // 60))
    if (running and not scanning and LAST_SCAN and _bool(cfg.get("auto_poll"), True)
            and (mk_open or _bool(cfg.get("poll_when_market_closed"), False))):
        expect = (poll if mk_open else max(poll, 300)) + max(scan_s, 60.0) + 120.0
        gap = now - LAST_SCAN
        if gap > expect:
            add("error", "loop_stuck", "no full scan has finished for %d min (expected every "
                                       "~%d s) — the polling loop looks stuck; restart "
                                       "paper_live_engine.py" % (gap // 60, poll))
    if running and scan_s > min(poll, tf_min * 60 / 2.0):
        add("warn", "slow_scan", _slow_scan_msg(scan_s, poll, tf, ltp_poll_s))
    if ltp_poll_s is not None and running:
        # when did the page switch LTP poll on? (no "stalled" alarm right after ticking it)
        if ltp_poll_s <= 0:
            RUNTIME["ltp_on_since"] = 0
        elif not RUNTIME.get("ltp_on_since"):
            RUNTIME["ltp_on_since"] = now
        open_n = sum(1 for r in rows if str(r.get("position") or "—") != "—")
        if ltp_poll_s <= 0 and open_n:
            add("warn", "ltp_off",
                "LTP poll is off while %d position(s) are open — target / stop / opposite "
                "flips and pending breaks are only checked on full scans (every ~%ds + scan "
                "time). Tick “LTP poll”." % (open_n, poll))
        elif ltp_poll_s > 0 and scanning:
            add("warn", "ltp_paused_scan", "LTP polling is paused while the full scan runs; price exits, flips and entries can be delayed until the scan processes each symbol or polling resumes.")
        elif ltp_poll_s > 0:
            last = _num((RUNTIME.get("ltp_stats") or {}).get("at"), 0.0)
            since = now - max(last, _num(RUNTIME.get("ltp_on_since"), now),
                              _num(RUNTIME.get("started"), 0.0))
            if (RUNTIME.get("ltp_error")
                    and now - _num(RUNTIME.get("ltp_error_at"), 0.0) < 300):
                add("error", "ltp_failed", "the LTP poll request failed: %s"
                    % str(RUNTIME["ltp_error"])[:160])
            elif since > max(3 * ltp_poll_s, 60) + 15:
                add("warn", "ltp_stalled",
                    "no LTP poll has finished for %d s (set to every %d s) — exits, flips and "
                    "pending breaks wait for the full scan meanwhile. A background tab slows "
                    "the poll: keep this tab visible." % (since, ltp_poll_s))
            wmax = _num(RUNTIME.get("ltp_wait_max"), 0.0)
            if wmax > max(20.0, 2.0 * ltp_poll_s):
                add("warn", "ltp_blocked",
                    "in the last 10 min an LTP poll had to wait up to %d s for Yahoo / the "
                    "full scan — exits and flips could be that much late" % wmax)
    nod = [r["symbol"] for r in rows
           if any(i.get("code") == "no_data" for i in (r.get("health") or {}).get("issues", []))]
    if len(nod) >= 2 and len(nod) == len(rows):
        add("error", "no_data_all",
            "Yahoo returned no candles for any of the %d symbols — this is an internet / Yahoo "
            "problem (or Yahoo is blocking requests for a while), not your symbol names. Look in "
            "the terminal for SSL / connection errors." % len(nod))
    cur = parse_symbols(cfg.get("symbols"), exch_of(cfg), cfg.get("symbol_exch")) if cfg else []
    never = [x for x in cur if x not in STATE]
    if never and LAST_SCAN and not scanning:
        add("info", "unscanned", "%d symbol(s) not scanned yet: %s" % (
            len(never), ", ".join(never[:8]) + (" …" if len(never) > 8 else "")))
    if cfg and not mk_open:
        add("info", "mkt_closed", "%s market is closed — open positions are still watched"
            % market_label(market_of(cfg)))
    if RUNTIME.get("loops", 0) > (1 if running else 0):
        add("info", "old_run", "a stopped run is still waiting for Yahoo to answer its last "
                               "download — that result is thrown away")
    if RUNTIME.get("aborted"):
        add("info", "aborted", "the last scan was stopped part-way — some rows are from the "
                               "scan before it")
    if PROFILE == "live":            # Live Trade: is the Google Sheet output getting the rows?
        so = _sheet_status()
        if so["enabled"] and not so["book"]:
            add("warn", "sheet_out", "Google Sheet output is on but no sheet name is set — %d row(s) "
                                     "wait in the queue (and are saved in %s)" % (so["queued"], so["csv"]))
        elif so["enabled"] and so["last_error"]:
            add("warn", "sheet_out", "Google Sheet output is failing (next try in %ds): %s — rows "
                                     "wait in the queue and are saved in %s"
                % (so["retry_in"], so["last_error"], so["csv"]))
        elif not so["enabled"] and so["queued"]:
            add("info", "sheet_out", "Google Sheet output is off — %d row(s) wait; they are written "
                                     "when you turn it on" % so["queued"])
    return out


def _slow_scan_msg(scan_s, poll, tf, ltp_poll_s):
    """What a slow full scan really delays, measured, and what to do about it."""
    p = RUNTIME.get("scan_parts") or {}
    n = _int(p.get("symbols"), 0) or _int(RUNTIME.get("scan_n"), 0)
    dl_c = _num(p.get("candles")) + _num(p.get("candles_wait"))
    dl_p = _num(p.get("prices")) + _num(p.get("prices_wait"))
    maths = _num(p.get("maths"))
    brk = (" (Yahoo candles %ds · live prices %ds · strategy maths %ds)"
           % (round(dl_c), round(dl_p), round(maths))) if p else ""
    avg = ((scan_s + poll) / 2.0 + scan_s) / 60.0
    worst = int(math.ceil((poll + 2 * scan_s) / 60.0))
    msg = ("one full scan%s takes %ds%s, then the engine waits %ds before the next one — so a "
           "NEW WaveTrend signal on a just-closed %s candle is picked up about %.1f min after "
           "the candle closes, at worst ~%d min. "
           % (" of %d symbols" % n if n else "", scan_s, brk, poll, tf, avg, worst))
    if ltp_poll_s:
        msg += ("LTP polling pauses during a full scan; target / stop exits, known breaks and flips "
                "can be delayed until that symbol is processed or polling resumes (normally every %ds). "
                % ltp_poll_s)
    else:
        msg += ("With LTP poll off, exits, flips and pending breaks wait for these scans too "
                "— tick LTP poll. ")
    if p and maths > dl_c + dl_p:
        slow = ", ".join("%s %.1fs" % (s_, d) for s_, d in (p.get("slowest") or [])[:3])
        msg += "The strategy maths is the slow part%s." % (" (slowest: %s)" % slow if slow else "")
    else:
        msg += "To speed it up: fewer symbols per list — Yahoo downloads grow with every symbol."
    return msg


def _health_grace(cfg, ltp_poll_s=None):
    """Seconds a self-healing problem may last before it is shown as an error."""
    tf = str(cfg.get("interval") or "15m")
    poll = _int(cfg.get("poll_seconds"), 0) or POLL_FOR_TF.get(tf, 120)
    cycle = poll + _num(RUNTIME.get("scan_ms"), 0) / 1000.0
    opp = TF_MINUTES.get(tf, 60) * 60 + cycle + 60
    if ltp_poll_s:
        opp = min(opp, 3 * ltp_poll_s + 45)
    return {"past_target": 90, "past_stop": 90, "missed_entry": 90,
            "row_stale_view": 90, "opp_broken": opp,
            "missed_already_triggered": cycle + 30, "tf_stale": cycle + 30}


def _attach_health(rows, cfg, cur, ltp_poll_s=None):
    """Run every check, set row["health"] on each row, return the banner summary."""
    now = time.time()
    cfg = cfg or {}
    try:
        C = _make_C(cfg or DEFAULTS)
        now_t = _utcnow().astimezone(IST).strftime("%Y-%m-%d %H:%M")
        last_tr = {}
        for tr in TRADES[-500:]:
            last_tr[tr.get("symbol")] = tr            # newest trade per symbol wins
        mk_cache = {}
        for r in rows:
            sym = r["symbol"]
            mk = _sym_market(sym)
            if mk not in mk_cache:
                mk_cache[mk] = _market_open_now({"market": mk})
            r["health"] = {"issues": symbol_health(
                sym, STATE.get(sym) or {}, cfg, C=C, listed=(not cur) or (sym in cur),
                mk_open=mk_cache[mk], now_t=now_t, last_trade=last_tr.get(sym))}

        # every recent entry / exit / flip is verified; a problem lands on its row
        recent, ev_issues = recent_changes(cfg)
        ev_extra = []
        row_by = {r["symbol"]: r for r in rows}
        for sym, lst in ev_issues.items():
            for i in lst:
                r = row_by.get(sym)
                if r is None:
                    ev_extra.append(dict(i, msg="%s — %s" % (sym, i["msg"])))
                elif all(x.get("code") != i["code"] for x in r["health"]["issues"]):
                    r["health"]["issues"].append(i)

        by_px = {}
        for r in rows:
            v = r.get("ltp")
            if isinstance(v, (int, float)) and v > 0:
                by_px.setdefault(round(float(v), 6), []).append(r["symbol"])
        for v, syms in by_px.items():
            if len(syms) > 1:
                for r in rows:
                    if r["symbol"] in syms:
                        r["health"]["issues"].append({
                            "lvl": "warn", "code": "dup_ltp",
                            "msg": "LTP %s is identical to %s — possible price mix-up" % (
                                v, ", ".join(x for x in syms if x != r["symbol"]))})

        grace = _health_grace(cfg, ltp_poll_s)
        seen = set()
        for r in rows:
            iss = r["health"]["issues"]
            for i in iss:
                key = "%s|%s" % (r["symbol"], i.get("code"))
                seen.add(key)
                age = now - _HEALTH_SEEN.setdefault(key, now)
                i["since_s"] = int(age)
                g = grace.get(i.get("code"))
                if g is not None and age > g and i["lvl"] != "error":
                    i["lvl"] = "error"
                    i["msg"] += (" — still like this after %d min, the logic is not reacting"
                                 % max(1, age // 60))
            iss.sort(key=lambda i: -HEALTH_RANK.get(i["lvl"], 0))
            r["health"]["level"] = iss[0]["lvl"] if iss else "ok"
        for k in list(_HEALTH_SEEN):
            if k not in seen:
                _HEALTH_SEEN.pop(k, None)

        glob = global_health(cfg, rows, now=now, ltp_poll_s=ltp_poll_s) + ev_extra
        allis = glob + [i for r in rows for i in r["health"]["issues"]]
        counts = {k: sum(1 for i in allis if i["lvl"] == k) for k in ("error", "warn", "info")}
        level = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
        return {"level": level, "global": glob, "counts": counts,
                "coverage": health_coverage(),
                "recent": recent, "recent_window_min": RECENT_MIN,
                "symbols_total": len(rows),
                "symbols_bad": [r["symbol"] for r in rows
                                if r["health"]["level"] in ("warn", "error")],
                "checked_at": _utcnow().astimezone(IST).strftime("%H:%M:%S")}
    except Exception as e:                    # the check must never break /status
        traceback.print_exc()
        for r in rows:
            h = r.get("health") or {}
            h.setdefault("issues", [])
            h.setdefault("level", "ok")
            r["health"] = h
        return {"level": "error", "counts": {"error": 1, "warn": 0, "info": 0},
                "recent": [], "recent_window_min": RECENT_MIN,
                "global": [{"lvl": "error", "code": "health_crash",
                            "msg": "the logic check itself crashed: %s: %s"
                                   % (type(e).__name__, e)}],
                "symbols_total": len(rows), "symbols_bad": [], "checked_at": ""}


def _trade_display_rows(symbol=None, limit=None):
    """Display-only enrichment of recorded trades. Never predicts an order or fill."""
    history = list(TRADES)
    state = dict(STATE)
    out = []
    for idx in range(len(history) - 1, -1, -1):
        tr = history[idx]
        if symbol is not None and tr.get("symbol") != symbol:
            continue
        sym = tr.get("symbol") or ""
        exit_t = str(tr.get("exit_t") or "")
        next_trade = None
        if exit_t:
            for later in history[idx + 1:]:
                ent = str(later.get("entry_t") or "")
                if later.get("symbol") == sym and ent and ent >= exit_t:
                    next_trade = {"kind": "closed", "trade": later,
                                  "same_time": ent == exit_t}
                    break
        st = state.get(sym) or {}
        if next_trade is None:
            pos = st.get("pos")
            if pos:
                ent = str(pos.get("entry_t") or "")
                if exit_t and ent and ent >= exit_t:
                    next_trade = {"kind": "open", "trade": {
                        "side": "long" if pos.get("sign", 0) > 0 else "short",
                        "entry": pos.get("entry"), "entry_t": ent,
                        "entry_rec": {k: pos.get(k) for k in ENTRY_REC_KEYS}},
                        "same_time": ent == exit_t}
                else:
                    next_trade = {"kind": "unknown", "reason": "Open position exists; sequence cannot be confirmed"}
            elif not exit_t:
                next_trade = {"kind": "unknown", "reason": "Close time unavailable"}
            elif not st or st.get("status") != "OK":
                next_trade = {"kind": "unknown", "reason": "Current signal state unavailable"}
            else:
                book = st.get("pending_book")
                if not isinstance(book, dict):
                    next_trade = {"kind": "unknown", "reason": "Pending signals not available"}
                else:
                    signals = []
                    for direction in ("buy", "sell"):
                        q = book.get(direction)
                        if isinstance(q, dict) and q.get("level") is not None:
                            signals.append({"dir": direction, "level": q["level"],
                                            "valid_t": q.get("valid_t", "")})
                    next_trade = {"kind": "waiting" if signals else "none", "signals": signals,
                                  "updated": st.get("updated", "")}
        row = dict(tr)
        row["display_ref"] = idx
        row["next_trade"] = next_trade
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


@paper_live_bp.route("/api/paper-live/trade-history")
def api_trade_history():
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"ok": False, "error": "Symbol is required"})
    if _PROFILE_NAME and request.args.get("session_profile", "") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed; refresh the page"})
    # A scan can hold RUN_LOCK while fetching prices; do not block this display.
    profile = _PROFILE_NAME
    records = _trade_display_rows(symbol=symbol)
    if profile != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed; refresh the page"})
    return jsonify({"ok": True, "symbol": symbol, "trades": records,
                    "note": "Available recorded history only; older unsaved or removed trades cannot be recovered."})


@paper_live_bp.route("/api/paper-live/status")
def api_status():
    poll = _int(RUNTIME["config"].get("poll_seconds"), 0) or POLL_FOR_TF.get(
        str(RUNTIME["config"].get("interval")), 120)
    rows = []
    # only the symbols this run is actually scanning — a symbol you removed (and that
    # holds no position) must not keep showing an old row in the monitor
    _cfg = RUNTIME.get("config") or {}
    _cur = set(parse_symbols(_cfg.get("symbols"), exch_of(_cfg), _cfg.get("symbol_exch"))) if _cfg else set()
    _hidden = 0
    for sym, st in STATE.items():
        if _cur and sym not in _cur and not (st or {}).get("pos"):
            _hidden += 1
            continue
        rows.append({
            "symbol": sym,
            "ltp": st.get("ltp", "—"),
            "ltp_src": st.get("ltp_src", ""),
            "ltp_age_min": st.get("ltp_age_min"),
            "last_bar": st.get("last_bar", "—"),
            "bar_age_min": st.get("bar_age_min", ""),
            "position": st.get("position", "—"),
            "latest_note": st.get("latest_note") or "",
            "tf": st.get("tf") or "",
            "htf": st.get("htf"),
            "pos_tf": (st.get("pos") or {}).get("tf"),
            "entry": st.get("entry", "—"),
            "target": st.get("target", "—"),
            "stop": st.get("stop", "—"),
            "opp_lvl": st.get("opp_lvl"),
            "live_pnl": st.get("live_pnl", 0.0),
            "realized": round(_num(st.get("realized", 0.0)), 2),
            "trades": st.get("trade_count", 0),
            "pending": st.get("pending"),
            "pending_state": st.get("pending_state", "—"),
            "last_action": st.get("last_action", ""),
            "status": st.get("status", ""),
            "stale": st.get("stale", False),
            "fno": st.get("fno", False),
            "signals": st.get("signals", 0),
            "bars": st.get("bars", 0),
            "updated": st.get("updated", ""),
            "cash": round(_num(st.get("cash", 0.0)), 2),
            "size_basis": st.get("size_basis") or sizing_label(RUNTIME["config"]),
            "amount_used": st.get("amount_used"),
            "capital_used": st.get("capital_used"),
            "wt_action": st.get("wt_action", "—"),
            "wt_side": st.get("wt_side", ""),
            "no_fill": st.get("no_fill", ""),
            "signal_state": st.get("signal_state", "none"),
            "trig_lvl": st.get("trig_lvl"),
            "valid_t": st.get("signal_valid_t") or (st.get("pending") or {}).get("valid_t", ""),
            "valid_i": (st.get("pending") or {}).get("valid_i"),
            "bars_since": st.get("signal_age"),
            "diff": st.get("diff"),
            "diff_pct": st.get("diff_pct"),
            "wt1": st.get("wt1"),
            "wt2": st.get("wt2"),
            "wt_trend": st.get("wt_trend", "—"),
            "rsi": st.get("rsi"),
            "qty": st.get("qty", 0),
            "entry_t": st.get("entry_t", ""),
            "pos_why": st.get("pos_why", ""),
            "pnl_pct": st.get("pnl_pct", 0.0),
            "risk_amt": st.get("risk_amt"),
            "reward_amt": st.get("reward_amt"),
            "rr": st.get("rr"),
        })
    rows.sort(key=lambda r: r["symbol"])
    _lp = request.args.get("ltp_poll")
    _health = _attach_health(rows, _cfg, _cur,
                             ltp_poll_s=(_int(_lp, 0) if _lp is not None else None))
    _live = sum(1 for r in rows if r.get("ltp_src") == "live")
    _candle = sum(1 for r in rows if r.get("ltp_src") == "last close")
    _old = [r.get("ltp_age_min") or 0 for r in rows if r.get("ltp_src") == "live"]
    return jsonify({
        "ok": True,
        "rows": rows,
        "health": _health,
        "ltp_live": _live,                       # how many prices are a real 1m quote
        "ltp_candle": _candle,                   # how many fell back to a candle close
        "ltp_age_max": (max(_old) if _old else None),   # freshest-feed age, minutes
        "auto_poll": _bool(RUNTIME["config"].get("auto_poll"), True),
        "hidden_rows": _hidden,                  # old symbols not in this run (no position)
        "trades": _trade_display_rows(limit=200),
        "trade_history_api": 1,
        "running": RUNTIME["running"],
        "cycles": RUNTIME["cycles"],
        "poll_seconds": poll,
        "next_scan_in": max(0, int(RUNTIME.get("next_scan", 0) - time.time())),
        "last_scan": LAST_SCAN,
        "last_scan_at": RUNTIME.get("last_scan_at", ""),
        "last_scan_ms": RUNTIME.get("scan_ms", 0),
        "last_scan_n": RUNTIME.get("scan_n", 0),
        "last_scan_kind": RUNTIME.get("last_scan_kind", ""),
        "scan_parts": RUNTIME.get("scan_parts") or {},   # where the scan time goes
        "ltp_stats": RUNTIME.get("ltp_stats") or {},     # last LTP poll: took / waited
        "scanning": RUNTIME.get("scanning", False),
        "stopping": RUNTIME.get("stopping", False),
        "aborted": RUNTIME.get("aborted", False),
        "symbol_count": len(STATE),
        "scan_started": RUNTIME.get("scan_started", 0),
        "started_at": RUNTIME.get("started", 0),
        "last_error": RUNTIME.get("last_error", ""),
        "config": RUNTIME.get("config", {}),
        "profile": PROFILE,
        "session_profile": _PROFILE_NAME,
        "session_error": _PROFILE_ERROR,
        "session_ui": _PROFILE_UI,
        "sheet_out": _sheet_status() if PROFILE == "live" else None,
        "server_time": pytz.utc.localize(
            __import__("datetime").datetime.utcnow()).astimezone(IST).strftime("%H:%M:%S"),
        "market": market_of(RUNTIME["config"]),
        "market_label": market_label(market_of(RUNTIME["config"])),
        "market_hours": (MARKETS.get(market_of(RUNTIME["config"])) or {}).get("hours", ""),
        "market_open": _market_open_now(RUNTIME["config"]),
        "poll_when_market_closed": _bool(
            RUNTIME["config"].get("poll_when_market_closed"), True),
        "size_basis": sizing_label(RUNTIME["config"]),
        "sizing_mode": str(RUNTIME["config"].get("sizing_mode") or "fixed"),
        "entry_exec": str(RUNTIME["config"].get("entry_exec") or "level"),
        "fno_mode": str(RUNTIME["config"].get("fno_mode") or "none"),
        "fno_error": _FNO_CACHE.get("err", "") if str(
            RUNTIME["config"].get("fno_mode") or "") == "sheet" else "",
    })


@paper_live_bp.route("/api/paper-live/reset", methods=["POST"])
def api_reset():
    with _PROFILE_LOCK:
        body = request.get_json(silent=True) or {}
        if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
            return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
        if RUNTIME.get("scanning"):
            return jsonify({"ok": False, "error": "Stop and wait for the scan before reset"})
        _reset()
        return jsonify({"ok": not bool(_PROFILE_ERROR), "error": _PROFILE_ERROR})


@paper_live_bp.route("/api/paper-live/replay", methods=["POST"])
def api_replay():
    """Historical replay of the live engine (no polling, no state written)."""
    cfg = cfg_of(request.get_json(silent=True) or {})
    try:
        download_batch, _ = _data()
        exchange = exch_of(cfg)
        symbols = parse_symbols(cfg.get("symbols"), exchange, cfg.get("symbol_exch"))
        if not symbols:
            return jsonify({"ok": False, "error": "no symbol"})
        sym = symbols[0]
        rows = download_batch([sym], interval=str(cfg.get("interval")),
                              period=str(cfg.get("period")), force_refresh=True)
        bars = (rows or {}).get(sym) or []
        if len(bars) < 60:
            return jsonify({"ok": False, "error": "not enough data (%d bars)" % len(bars)})
        df_real = _common()[0](bars)
        if df_real.index.tz is None:
            df_real.index = df_real.index.tz_localize("UTC").tz_convert(IST)
        else:
            df_real.index = df_real.index.tz_convert(IST)

        _amt, _cap = _alloc_maps(cfg)
        _k = _clean_sym(sym)
        if _k in _amt:
            cfg = dict(cfg); cfg["position_amount"] = _amt[_k]
        if _k in _cap:
            cfg = dict(cfg); cfg["capital"] = _cap[_k]
        trades, st = replay_trades(df_real, cfg, close_at_end=True)
        return jsonify({
            "ok": True, "symbol": sym, "bars": len(df_real),
            "from": str(df_real.index[0])[:16], "to": str(df_real.index[-1])[:16],
            "trades": trades, "net": round(sum(t["pnl"] for t in trades), 2),
            "cash": round(st["cash"], 2),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


@paper_live_bp.route("/api/paper-live/verify", methods=["POST"])
def api_verify():
    cfg = cfg_of(request.get_json(silent=True) or {})
    try:
        return jsonify(verify_cfg(cfg))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


@paper_live_bp.route("/api/paper-live/allocations", methods=["GET", "POST"])
def api_allocations():
    """The working symbol set + its money — saved like Trade Setup's 'Save Allocations'."""
    if request.method == "GET":
        if _PROFILE_NAME:
            # A profile restores its own symbol rows from its saved page state.
            return jsonify({"ok": True, "exists": False, "data": None})
        try:
            data = json.loads(ALLOC_FILE.read_text())
        except Exception:
            return jsonify({"ok": True, "exists": False, "data": None})
        return jsonify({"ok": True, "exists": True, "data": data})
    body = request.get_json(silent=True) or {}
    if _PROFILE_NAME and body.get("session_profile") != _PROFILE_NAME:
        return jsonify({"ok": False, "error": "Profile changed in another tab; refresh"})
    data = body.get("data")
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "nothing to save"})
    data["saved_at"] = pytz.utc.localize(
        __import__("datetime").datetime.utcnow()).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    if not _PROFILE_NAME:
        try:
            ALLOC_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            return jsonify({"ok": False, "error": "cannot write %s: %s" % (ALLOC_FILE, e)})
    # keep the running engine in step, if one is running
    applied = False
    want_apply = body.get("apply", True) is not False   # locked-only save keeps the engine as is
    if want_apply and RUNTIME["running"] and RUNTIME["config"] and data.get("rows"):
        cfg = dict(RUNTIME["config"])
        cfg["symbols"] = ", ".join([r.get("sym") or r.get("symbol") or ""
                                    for r in data["rows"] if (r.get("sym") or r.get("symbol"))])
        cfg["alloc"] = [{"symbol": r.get("sym") or r.get("symbol"),
                         "amount": r.get("amount"), "capital": r.get("capital")}
                        for r in data["rows"]]
        try:
            api_update_inner(cfg)
            applied = True
        except Exception as e:
            print("⚠️ allocations -> running engine failed:", e)
    return jsonify({"ok": True, "count": len(data.get("rows") or []),
                    "saved_at": data["saved_at"], "applied_to_running": applied})


@paper_live_bp.route("/api/paper-live/presets", methods=["GET", "POST"])
def api_presets():
    if request.method == "GET":
        name = (request.args.get("name") or "").strip()
        all_p = _presets()
        if name:
            if name not in all_p:
                return jsonify({"ok": False, "error": "no preset named %r" % name})
            return jsonify({"ok": True, "name": name, "preset": all_p[name]})
        return jsonify({"ok": True, "presets": [
            {"name": k, "symbols": len((v or {}).get("rows") or []),
             "saved": (v or {}).get("saved_at", "")} for k, v in sorted(all_p.items())]})
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    data = body.get("data")
    if not name:
        return jsonify({"ok": False, "error": "give the preset a name"})
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "nothing to save"})
    all_p = _presets()
    all_p[name] = dict(data, saved_at=pytz.utc.localize(
        __import__("datetime").datetime.utcnow()).astimezone(IST).strftime("%Y-%m-%d %H:%M"))
    if not _save_presets(all_p):
        return jsonify({"ok": False, "error": "could not write %s" % PRESET_FILE})
    return jsonify({"ok": True, "name": name, "count": len(data.get("rows") or [])})


@paper_live_bp.route("/api/paper-live/presets/delete", methods=["POST"])
def api_preset_delete():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    all_p = _presets()
    if name in all_p:
        all_p.pop(name)
        _save_presets(all_p)
        return jsonify({"ok": True, "deleted": name})
    return jsonify({"ok": False, "error": "no preset named %r" % name})


# ── Google Sheet helpers (own routes: work standalone AND inside server.py) ─
KNOWN_BOOKS = ["paper trading", "Backtest scanner", "Stock Price Scraper", "scanner output"]


@paper_live_bp.route("/api/paper-live/sheets/books")
def api_pl_books():
    return jsonify({"ok": True, "books": KNOWN_BOOKS})


@paper_live_bp.route("/api/paper-live/sheets/tabs")
def api_pl_tabs():
    book = (request.args.get("book") or "").strip()
    if not book:
        return jsonify({"ok": False, "error": "no book", "tabs": []})
    try:
        import sheets as _sheets
        tabs = _sheets.list_tabs(book) or []
        if not tabs:
            return jsonify({"ok": False, "tabs": [],
                            "error": "no tabs / sheet not shared with %s" % (
                                _sheets.service_account_email() or "the service account")})
        return jsonify({"ok": True, "tabs": tabs})
    except Exception as e:
        return jsonify({"ok": False, "tabs": [], "error": "%s: %s" % (type(e).__name__, e)})


@paper_live_bp.route("/api/paper-live/sheets/symbols")
def api_pl_sheet_symbols():
    book = (request.args.get("book") or "").strip()
    tab = (request.args.get("tab") or "").strip()
    if not book or not tab:
        return jsonify({"ok": False, "error": "book and tab are required", "symbols": []})
    try:
        import sheets as _sheets
        syms = _sheets.read_symbols(book, tab) or []
        try:
            exch = _sheets._detect_exchange(tab)
        except Exception:
            exch = "NSE"
        if not syms:
            return jsonify({"ok": False, "symbols": [], "exchange": exch,
                            "error": "tab '%s' has no symbols, or it is not shared with %s" % (
                                tab, _sheets.service_account_email() or "the service account")})
        return jsonify({"ok": True, "symbols": syms, "exchange": exch, "count": len(syms)})
    except Exception as e:
        return jsonify({"ok": False, "symbols": [],
                        "error": "%s: %s" % (type(e).__name__, e)})


@paper_live_bp.record_once
def _enable_template_reload(state):
    """Also when hosted inside server.py: never serve an old cached .html."""
    try:
        state.app.config["TEMPLATES_AUTO_RELOAD"] = True
        state.app.jinja_env.auto_reload = True
    except Exception:
        pass


# ── standalone runner ──────────────────────────────────────────────────────
def _port_free(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port(want, tries=20):
    """First free port at or after `want` (so an old copy cannot block you)."""
    for p in range(int(want), int(want) + tries):
        if _port_free(p):
            return p
    return int(want)


def _logic_stamp_cli(write=False):
    """Print (or --write) HEALTH_REVIEWED for the code as it is now. Run it ONLY
    after the logic check was updated for the change — see LOGIC CONTRACT."""
    fp = logic_fingerprint()
    changed = logic_changes()
    body = "HEALTH_REVIEWED = {  # stamped by: python paper_live_engine.py --logic-stamp\n"
    for k in sorted(fp):
        body += "    %r: %r,\n" % (k, fp[k])
    body += "}  # end HEALTH_REVIEWED"
    print("changed since the last stamp:", ", ".join(changed) if changed else "nothing")
    if not write:
        print(body)
        return
    path = Path(__file__).resolve()
    src = path.read_text(encoding="utf-8")
    a = src.index("\nHEALTH_REVIEWED = {") + 1
    end = "}  # end HEALTH_REVIEWED"
    b = src.index(end, a) + len(end)
    path.write_text(src[:a] + body + src[b:], encoding="utf-8")
    print("written to", path.name)


if __name__ == "__main__":
    import sys as _sys
    if "--logic-stamp" in _sys.argv:
        _logic_stamp_cli(write="--write" in _sys.argv)
        raise SystemExit(0)
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(paper_live_bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    want = int(os.getenv("PAPER_LIVE_PORT", "8021" if PROFILE == "live" else "8020"))
    port = _pick_port(want)
    if port != want:
        print("⚠️  port %d is busy — an older copy of this page is still running." % want)
        print("    (to kill it:  lsof -ti:%d | xargs kill -9 )" % want)
        print("    Starting on %d instead — nothing else to do." % port)
    print()
    if PROFILE == "live":
        print("  Live Trade (dry run -> Google Sheet)  ->  http://localhost:%d/live-trade" % port)
        _sheet_start()
    else:
        print("  Paper Live  ->  http://localhost:%d/paper-live" % port)
    print("  (open that, then press Ctrl+Shift+R in the browser)")
    print("  (stop with Ctrl+C)")
    print()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)