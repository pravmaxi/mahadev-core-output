"""
yfinance wrapper — OHLC cached, LTP always fresh.
Handles MultiIndex columns from yfinance robustly.
"""
import time
import yfinance as yf
import pandas as pd


_BATCH_CACHE = {}
_CACHE_TTL   = 300
_MIN_BARS    = 20

_FIELD_NAMES = {"Open", "High", "Low", "Close", "Volume", "Adj Close"}


def _ck(symbols, interval, period):
    return (tuple(sorted(symbols)), interval, period)


def _get(symbols, interval, period):
    hit = _BATCH_CACHE.get(_ck(symbols, interval, period))
    if not hit:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        del _BATCH_CACHE[_ck(symbols, interval, period)]
        return None
    if any(len(data.get(s, [])) < _MIN_BARS for s in symbols):
        print(f"⚠️ Cached batch has short/missing data — ignoring & re-downloading")
        del _BATCH_CACHE[_ck(symbols, interval, period)]
        return None
    print(f"💾 Cache HIT ({int(time.time()-ts)}s old, {len(symbols)} symbols)")
    return data


def _put(symbols, interval, period, data):
    if any(len(data.get(s, [])) < _MIN_BARS for s in symbols):
        short = [s for s in symbols if len(data.get(s, [])) < _MIN_BARS]
        print(f"⚠️ Not caching — short/missing data for: {short}")
        return
    _BATCH_CACHE[_ck(symbols, interval, period)] = (time.time(), data)
    if len(_BATCH_CACHE) > 20:
        oldest = min(_BATCH_CACHE.items(), key=lambda kv: kv[1][0])
        del _BATCH_CACHE[oldest[0]]


def clear_cache():
    _BATCH_CACHE.clear()


# ── Smart flatten ──────────────────────────────────────────────────────────
def _flatten(df):
    """
    yfinance sometimes returns flat columns and sometimes MultiIndex.
    Detect where the OHLCV field names live and pick that level.

    Examples:
      flat columns            → unchanged
      MultiIndex (Ticker, Field) → take Level 1 (field)
      MultiIndex (Field, Ticker) → take Level 0 (field)
    """
    if df is None:
        return None
    if not hasattr(df.columns, "levels"):
        # ✅ FIX: Drop duplicates even if it's already flat
        return df.loc[:, ~df.columns.duplicated()]  # already flat

    lvl0 = set(str(x) for x in df.columns.get_level_values(0))
    lvl1 = set(str(x) for x in df.columns.get_level_values(1))

    if lvl1 & _FIELD_NAMES:
        df.columns = df.columns.get_level_values(1)
    elif lvl0 & _FIELD_NAMES:
        df.columns = df.columns.get_level_values(0)
    else:
        # last resort: take level 0
        df.columns = df.columns.get_level_values(0)
        
    # ✅ FIX: Drop duplicate columns (e.g., if a batch download left us with 14 'Close' columns)
    df = df.loc[:, ~df.columns.duplicated()]
    return df


# ── Rows converter ─────────────────────────────────────────────────────────
def _rows(df):
    out = []
    if df is None:
        return out
    for idx, r in df.iterrows():
        try:
            # ✅ FIX: If a column accidentally returns a Series (duplicate columns), take the first value
            def _val(col):
                v = r[col]
                if isinstance(v, pd.Series):
                    return float(v.iloc[0])
                if isinstance(v, pd.DataFrame):
                    return float(v.iloc[0, 0])
                return float(v)
                
            out.append({
                "t": idx.isoformat(),
                "o": _val("Open"),  "h": _val("High"),
                "l": _val("Low"),   "c": _val("Close"),
                "v": _val("Volume") if "Volume" in r else 0.0,
            })
        except Exception as e:
            # print once so we know if columns are misaligned
            if len(out) == 0 and not getattr(_rows, "_warned", False):
                print(f"  ⚠️ _rows: could not read OHLC — columns={list(df.columns)} err={e}")
                _rows._warned = True
            continue
    return out


_TF_PERIODS = {
    "1m":  ["1d","5d"],
    "2m":  ["1d","5d","1mo"],
    "5m":  ["1d","5d","1mo","60d"],
    "15m": ["1d","5d","1mo","60d"],
    "30m": ["1d","5d","1mo","60d"],
    "60m": ["1d","5d","1mo","3mo","6mo","1y","2y"],
    "90m": ["1d","5d","1mo","3mo","6mo","1y","2y"],
    "1h":  ["5d","1mo","3mo","6mo","1y","2y"],
    "4h":  ["1mo","3mo","6mo","1y","2y"],
    "1d":  ["1mo","3mo","6mo","1y","2y","5y","10y"],
    "5d":  ["1mo","3mo","6mo","1y","2y","5y","10y"],
    "1wk": ["1mo","3mo","6mo","1y","2y","5y","10y"],
    "1mo": ["1y","2y","5y","10y"],
    "3mo": ["2y","5y","10y"],
}


# ── Public download ────────────────────────────────────────────────────────
_SYMBOL_MAP = {
    "XAUUSD": "GC=F", "XAUUSD=X": "GC=F",
    "XAGUSD": "SI=F", "XAGUSD=X": "SI=F",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X", "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X"
}

def download(symbol, interval="1h", period="1mo"):
    # Translate common metal/forex symbols to their Yahoo Finance equivalents
    sym_upper = symbol.upper().strip()
    
    # If the Google Sheet appended .NS to a forex ticker, strip it
    if sym_upper.endswith(".NS") and sym_upper[:-3] in _SYMBOL_MAP:
        sym_upper = sym_upper[:-3]
        
    symbol = _SYMBOL_MAP.get(sym_upper, symbol)
    allowed = _TF_PERIODS.get(interval)
    if allowed and period not in allowed:
        period = allowed[-1]
    
    # Create a fallback list of periods (e.g., if '2y' fails, try '1y', '6mo', '3mo')
    periods_to_try = [period]
    if allowed:
        for p in reversed(allowed):
            if p not in periods_to_try:
                periods_to_try.append(p)
                
    for p in periods_to_try:
        for attempt in range(2):
            try:
                df = yf.download(symbol, interval=interval, period=p,
                                 progress=False, auto_adjust=False, timeout=15)
                if df is not None and not df.empty:
                    df = _flatten(df).dropna()
                    rows = _rows(df)
                    if len(rows) >= 40:
                        return rows
            except Exception as e:
                print(f"⚠️ yf.download({symbol}) attempt {attempt+1} ({p}) raised: {e}")
            
            time.sleep(0.5)

    print(f"❌ {symbol}: still no data after trying all periods.")
    return []


def download_batch(symbols, interval="1h", period="1mo", force_refresh=False):
    if not symbols:
        return {}

    # ✅ FIX: Auto-correct invalid periods before calling Yahoo Finance
    allowed = _TF_PERIODS.get(interval)
    if allowed and period not in allowed:
        period = allowed[-1] # Defaults to the max allowed period (e.g., 60d for 5m)

    # ── single symbol: batch API is unreliable for length-1 lists ──
    if len(symbols) == 1:
        s = symbols[0]
        if not force_refresh:
            hit = _get(symbols, interval, period)
            if hit is not None:
                return hit
        rows = download(s, interval=interval, period=period)
        out = {s: rows}
        if len(rows) >= _MIN_BARS:
            _put(symbols, interval, period, out)
        else:
            print(f"  ❌ {s}: single-symbol download returned {len(rows)} bars")
        return out

    # ── multiple symbols: use the batch API ──
    if not force_refresh:
        hit = _get(symbols, interval, period)
        if hit is not None:
            return hit

    df = None
    try:
        df = yf.download(symbols, interval=interval, period=period,
                         group_by="ticker", progress=False,
                         auto_adjust=False, threads=True, timeout=15)
    except Exception as e:
        print(f"⚠️ Batch download failed entirely: {e}")

    out = {}
    for s in symbols:
        sub = None
        if df is not None and not df.empty:
            try:
                if hasattr(df.columns, "levels"):
                    lvl0 = list(df.columns.get_level_values(0))
                    for cand in (s, s.replace(".NS", "").replace(".BO", ""), s.upper()):
                        if cand in lvl0:
                            sub = _flatten(df[cand].copy())
                            break
            except Exception as e:
                print(f"  ⚠️ {s}: batch slice failed ({e})")

        good = (sub is not None and not sub.empty and len(sub.dropna()) >= _MIN_BARS)
        if good:
            out[s] = _rows(sub.dropna())
        else:
            single = download(s, interval=interval, period=period)
            out[s] = single
            if len(single) >= _MIN_BARS:
                print(f"  ↻ {s}: batch missing/short → single gave {len(single)} bars")
            else:
                print(f"  ❌ {s}: single download also returned {len(single)} bars")

    _put(symbols, interval, period, out)
    return out


# ── LTP ─────────────────────────────────────────────────────────────────────
def get_live_ltp(symbols):
    """1m bars → 5d daily close → fast_info, in that order."""
    if not symbols:
        return {}
    out = {}

    # 1) try 1m bars
    df = None
    try:
        df = yf.download(symbols, period="1d", interval="1m",
                         group_by="ticker", progress=False,
                         auto_adjust=False, threads=True)
    except Exception as e:
        print(f"⚠️ 1m LTP batch failed: {e}")

    if df is not None and not df.empty:
        if len(symbols) == 1:
            s = symbols[0]
            flat = _flatten(df.copy())
            if "Close" in flat.columns:
                last = flat["Close"].dropna()
                out[s] = round(float(last.iloc[-1]), 2) if len(last) else None
        else:
            for s in symbols:
                try:
                    sub = _flatten(df[s].copy())
                    if "Close" in sub.columns:
                        close = sub["Close"].dropna()
                        out[s] = round(float(close.iloc[-1]), 2) if len(close) else None
                except Exception:
                    out[s] = None

    # 2) daily close fallback
    missing = [s for s in symbols if out.get(s) is None]
    if missing:
        try:
            df2 = yf.download(missing, period="5d", interval="1d",
                              group_by="ticker", progress=False,
                              auto_adjust=False, threads=True)
            if df2 is not None and not df2.empty:
                if len(missing) == 1:
                    s = missing[0]
                    flat = _flatten(df2.copy())
                    if "Close" in flat.columns:
                        close = flat["Close"].dropna()
                        if len(close):
                            out[s] = round(float(close.iloc[-1]), 2)
                else:
                    for s in missing:
                        try:
                            sub = _flatten(df2[s].copy())
                            if "Close" in sub.columns:
                                close = sub["Close"].dropna()
                                if len(close):
                                    out[s] = round(float(close.iloc[-1]), 2)
                        except Exception:
                            pass
        except Exception as e:
            print(f"⚠️ Daily-close LTP fallback failed: {e}")

    # 3) fast_info fallback
    still = [s for s in symbols if out.get(s) is None]
    if still:
        from concurrent.futures import ThreadPoolExecutor
        def _one(sym):
            try:
                t = yf.Ticker(sym)
                px = (getattr(t.fast_info, "last_price", None)
                      or getattr(t.fast_info, "previous_close", None))
                if px is not None:
                    return sym, round(float(px), 2)
            except Exception:
                pass
            return sym, None
        with ThreadPoolExecutor(max_workers=16) as pool:
            for sym, px in pool.map(_one, still):
                out[sym] = px

    got = sum(1 for v in out.values() if v is not None)
    print(f"💡 LTP resolved {got}/{len(symbols)}")
    return out


# ── self-test ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🧪 data.py self-test\n" + "─" * 60)
    import yfinance as yf
    print(f"yfinance version: {yf.__version__}\n")

    for sym in ("HAL.NS", "IRFC.NS", "AAPL", "AMD"):
        rows = download(sym, "1h", "6mo")
        last = rows[-1]["c"] if rows else None
        print(f"download {sym:12s} → {len(rows):>4} bars   last_close={last}")

    print("\nbatch:")
    batch = download_batch(["HAL.NS", "IRFC.NS"], "1h", "6mo")
    for s, rows in batch.items():
        print(f"  {s:12s} {len(rows)} bars")

    print("\nLTP:")
    ltp = get_live_ltp(["HAL.NS", "IRFC.NS", "AAPL"])
    for s, p in ltp.items():
        print(f"  {s:12s} {p}")

    print("\n✅ data.py loaded.\n")