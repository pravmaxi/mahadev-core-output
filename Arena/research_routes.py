"""Mahadev Scanner and Backtest routes, adapted from web/server.py for Arena.
No broker, trade execution, or automatic trading routes are registered here.
"""
import os, time, queue, threading, json
from pathlib import Path
import pandas as pd
import pytz
from flask import Blueprint, jsonify, request, render_template
from research_data import download, download_batch, get_live_ltp
from indicators import list_all, get as get_indicator
from indicators.common import candles_to_df, heikin_ashi, apply_fno_filter
import sheets
import research_backtest as backtest
from research_continuation import continuation_view
import research_batch as batch_backtest
import research_autotest as autotest

research_bp = Blueprint('research', __name__)
IST = pytz.timezone('Asia/Kolkata')
LAST_SCAN_CACHE = {"rows": [], "meta": None, "ts": 0}
LAST_BACKTEST_CACHE = {"data": None, "ts": 0}
# Serialize Continuation scans and quote requests in this single-process app.
CONTINUATION_FETCH_LOCK = threading.Lock()
SCANNER_JOBS = {}
DATA_DIR = Path(__file__).resolve().parent / 'data' / 'research'
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / 'trigger_state.json'
FNO_SYMBOLS_FALLBACK = ["HAL.NS", "WIPRO.NS", "RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS", "TATAMOTORS.NS", "VOLTAS.NS", "HDFCBANK.NS", "ICICIBANK.NS"]

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

@research_bp.route('/scanner')
def scanner_page():
    return render_template('research_scanner.html', active='scanner')

@research_bp.route('/backtest')
def backtest_page():
    return render_template('research_backtest.html', active='backtest')

@research_bp.route('/continuation')
def continuation_page():
    return render_template('research_backtest.html', active='continuation', continuation=True)

@research_bp.route("/api/sheets/tabs")
def api_sheets_tabs():
    book = request.args.get("book") or ""
    if not book:
        return jsonify({"ok": False, "tabs": []})
    return jsonify({"ok": True, "book": book, "tabs": sheets.list_tabs(book)})


@research_bp.route("/api/sheets/service_account")
def api_service_account():
    return jsonify({"email": sheets.service_account_email()})


@research_bp.route("/api/sheets/symbols")
def api_sheets_symbols():
    book = request.args.get("book") or ""
    tab = request.args.get("tab") or ""
    if not book or not tab:
        return jsonify({"ok": False, "symbols": []})
    syms = sheets.read_symbols(book, tab)
    return jsonify({"ok": True, "symbols": syms})


@research_bp.route("/api/indicators")
def api_indicators():
    return jsonify(list_all())


def _from_watchlist():
    path = str(DATA_DIR / "watchlist.json")
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


@research_bp.route("/api/scanner/run", methods=["POST"])
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
        LAST_SCAN_CACHE.update({"rows": list(SCANNER_JOBS[job_id]["results"]),
                                "meta": {"tf": tf, "total_ms": total_ms}, "ts": time.time()})

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


@research_bp.route("/api/scanner/stream")
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


@research_bp.route("/api/scanner/last")
def api_scanner_last():
    age = time.time() - LAST_SCAN_CACHE["ts"]
    if not LAST_SCAN_CACHE["rows"] or age > 1800:
        return jsonify({"ok": False, "error": "no recent scan"})
    return jsonify({"ok": True, "rows": LAST_SCAN_CACHE["rows"],
                    "meta": LAST_SCAN_CACHE["meta"], "age": int(age)})


@research_bp.route("/api/scanner/load_saved")
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


@research_bp.route("/api/backtest/last")
def api_backtest_last():
    age = time.time() - LAST_BACKTEST_CACHE["ts"]
    if not LAST_BACKTEST_CACHE["data"] or age > 1800:
        return jsonify({"ok": False, "error": "no recent backtest"})
    d = dict(LAST_BACKTEST_CACHE["data"])
    d["age"] = int(age)
    return jsonify(d)


@research_bp.route("/api/trigger_state")
def api_trigger_state():
    return jsonify({
        "ok": True,
        "file": str(STATE_FILE),
        "count": len(TRIGGER_STATE),
        "state": TRIGGER_STATE,
    })


@research_bp.route("/api/trigger_state/clear", methods=["POST"])
def api_trigger_state_clear():
    global TRIGGER_STATE
    TRIGGER_STATE = {}
    _save_trigger_state(TRIGGER_STATE)
    print("🧹 Trigger state cleared")
    return jsonify({"ok": True})


@research_bp.route("/api/backtest/sr_segments")
def api_backtest_sr_segments():
    """Diagnostic: return S/R segments (which bars a level held for) with no
    order simulation. Used to verify pivot detection against TradingView."""
    import research_backtest as bt
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


@research_bp.route("/api/backtest/events")
def api_backtest_events():
    import research_backtest as bt
    from research_data import download as dl
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


def _continuation_quote(symbol):
    import math
    from research_data import _SYMBOL_MAP
    symbol = str(symbol or "").strip().upper()
    if not symbol or len(symbol) > 100:
        return {"ltp": None, "ltp_status": "invalid_symbol", "ltp_symbol": symbol}
    if symbol.endswith(".NS") and symbol[:-3] in _SYMBOL_MAP:
        symbol = symbol[:-3]
    quote_symbol = _SYMBOL_MAP.get(symbol, symbol)
    try:
        raw = get_live_ltp([quote_symbol]).get(quote_symbol)
        price = float(raw) if raw is not None and math.isfinite(float(raw)) and float(raw) > 0 else None
        return {"ltp": price, "ltp_status": "ok" if price is not None else "unavailable", "ltp_symbol": quote_symbol}
    except Exception as exc:
        print("⚠️ Continuation quote failed for %s: %s" % (quote_symbol, exc))
        return {"ltp": None, "ltp_status": "error", "ltp_symbol": quote_symbol}


@research_bp.route("/api/continuation/ltp", methods=["POST"])
def api_continuation_ltp():
    # Skip rather than queue a poll while a scan or another quote is active.
    if not CONTINUATION_FETCH_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "busy": True})
    try:
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": True, **_continuation_quote(body.get("symbol"))})
    finally:
        CONTINUATION_FETCH_LOCK.release()


@research_bp.route("/api/backtest/run", methods=["POST"])
@research_bp.route("/api/continuation/run", methods=["POST"])
def api_backtest_run():
    if request.path.endswith("/continuation/run"):
        with CONTINUATION_FETCH_LOCK:
            return _api_backtest_run_impl()
    return _api_backtest_run_impl()


def _api_backtest_run_impl():
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
            include_in_process = request.path.endswith("/continuation/run"),
        )
        if res.get("ok"):
            if request.path.endswith("/continuation/run"):
                res = continuation_view(res, body.get("strategy_mode", "simple"))
                # Display only; the quote does not alter simulated fills or P&L.
                res["continuation"].update(_continuation_quote(body.get("symbol", "HAL.NS")))
            else:
                LAST_BACKTEST_CACHE.update({"data": res, "ts": time.time()})
        return jsonify(res)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@research_bp.route("/api/backtest/autotest/start", methods=["POST"])
def api_autotest_start():
    body = request.get_json(silent=True) or {}
    cfg = body.get("config", {})
    vary = body.get("vary", [])

    job_id = f"at_{int(time.time()*1000)}_{os.urandom(3).hex()}"

    if cfg.get("fno_mode") == "sheet":
        cfg["fno_list"] = set(sheets.read_symbols(cfg.get("sheet_name", "Stock Price Scraper"), cfg.get("fno_sheet", "F&O_input")))

    autotest.start_job(job_id, cfg, vary)
    return jsonify({"ok": True, "job_id": job_id})


@research_bp.route("/api/backtest/autotest/stream")
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


@research_bp.route("/api/backtest/autotest/stop", methods=["POST"])
def api_autotest_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    autotest.stop_job(jid)
    return jsonify({"ok": True})


@research_bp.route("/api/backtest/batch/start", methods=["POST"])
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


@research_bp.route("/api/backtest/batch/stream")
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


@research_bp.route("/api/backtest/batch/stop", methods=["POST"])
def api_backtest_batch_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    ok = batch_backtest.stop_job(jid)
    return jsonify({"ok": ok})


@research_bp.route("/api/backtest/batch/write", methods=["POST"])
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


@research_bp.route("/api/backtest/batch/results")
def api_backtest_batch_results():
    jid = request.args.get("job")
    if not jid or jid not in batch_backtest.JOBS:
        return jsonify({"ok": False, "error": "unknown job"})
    return jsonify({"ok": True, "results": batch_backtest.JOBS[jid]["results"]})


@research_bp.route("/api/backtest/batch/load")
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


@research_bp.route("/api/scanner/stop", methods=["POST"])
def api_scanner_stop():
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    if not jid: return jsonify({"ok": False, "error": "no job_id"})
    if jid in SCANNER_JOBS:
        SCANNER_JOBS[jid]["stop"] = True
    return jsonify({"ok": True})


@research_bp.route("/api/test/indicators")
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
