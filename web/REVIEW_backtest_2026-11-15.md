# Backtest Review — findings, fixes, and page proposals
**Date:** 15 Nov 2026 · **Scope:** `web/` only · **Status:** ANALYSIS ONLY — nothing in the app was modified.

Branch: `arena/975c05b6-mahadev-core-output` (from `main` @ `3e63035`)

---

## 0. What I read

| File | Lines | Read |
|---|---|---|
| `web/PROJECT.md` | 255 | full |
| `web/PANEL_LOGIC.md` | 148 | full |
| `web/backtest.py` | 1327 | full (helpers, filters, 3 simulators, metrics) |
| `web/batch_backtest.py` | 87 | full |
| `web/autotest.py` | 145 | full |
| `web/data.py` | 354 | download / cache / period map |
| `web/server.py` | 2202 | all `/api/backtest*`, `/api/trade*`, routes |
| `web/templates/backtest.html` | 2423 | cards, trades, events, WT panel, batch, auto-test |
| `web/templates/base.html` | 62 | nav (7 links) |

Page map today (7 nav links, PROJECT.md says 6 — doc drift):

`/scanner` · `/backtest` · `/broker` · `/trading` · `/paper-trading` · `/trade-setup` · `/settings`

---

## 1. Verdict in one line

The **engine maths is sound** (costs, slippage, integer qty, no same-bar exit-after-entry, no repaint inside the loop). The weak spots are **HTF look-ahead**, **Auto-Test not running the settings you see in the UI**, **no export of anything**, and **missing risk statistics** — plus there is no page that answers "which of my 40 symbols is actually worth trading?"

Priority order: **P0 = §2 (correctness) → P1 = §4 (metrics) → P2 = §5 (pages)**

---

## 2. P0 — Correctness / parity bugs found

### 2.1 🔴 HTF trend filter reads a still-forming candle (look-ahead bias)
`backtest.py:147-155`

```python
idx = hdf.index.searchsorted(ts, side="right") - 1
```
HTF bars are labelled by their **start** time. With `side="right"` a 10:30 signal on 15m resolves to the **1h bar that is still forming** (10:15→11:15), so its `Close` — and therefore its EMA50/EMA200 — is future information. The filter therefore looks better in backtest than it can ever be live.

**FIND** (`backtest.py` ≈ line 147)
```python
def _htf_uptrend_at(hdf, ts):
    """Return True/False/None (unknown) at the given timestamp."""
    if hdf is None or hdf.empty: return None
    try:
        idx = hdf.index.searchsorted(ts, side="right") - 1
    except Exception:
        return None
    if idx < 0 or idx >= len(hdf): return None
```

**REPLACE**
```python
def _htf_uptrend_at(hdf, ts):
    """Return True/False/None (unknown) at the given timestamp.
    Uses only HTF bars that have CLOSED before ts (no look-ahead)."""
    if hdf is None or hdf.empty: return None
    try:
        idx = hdf.index.searchsorted(ts, side="left") - 1
    except Exception:
        return None
    if idx < 0 or idx >= len(hdf): return None
```
Impact: expect **HTF-filtered runs to get worse** (that is the bug being removed). Re-baseline any preset that relies on `htf_filter_enabled`.

---

### 2.2 🔴 Auto-Test silently ignores most of the UI config
`autotest.py:65-97` — the `backtest.run(...)` call does **not** forward:

`capital`, `position_pct`, `position_amount`, `fixed_qty`, `sizing_mode`, `commission_pct`, `slippage_pct`, `base_qty`, `max_levels`, `multiplier_type`, `profit_target_mode`, `recovery_buffer_amount`, `recovery_trail_*`, `trailing_start_pct`, `trailing_step_pct`, `trailing_tighten_*`, `wt_buy_max`, `wt_sell_min`, `rsi_period`, `vol_mult`, `vol_lookback`.

Consequences you are probably already seeing:
* Auto-Test always sizes at **₹1,00,000 / ₹5,000 fixed** even if the UI says ₹2,00,000 / 25 %.
* "Trailing SL (2)" sweeps True/False but always at **3 % start / 2 % step**, whatever you typed.
* "WT Extreme (2)" always filters at **-40 / +40**, whatever you typed.
* Auto-Test Net ₹ rows are **not comparable** with the single-Backtest numbers for the same settings.

**FIND** (`autotest.py` ≈ line 65, inside the `try:`)
```python
            try:
                res = backtest.run(
                    symbol=run_cfg.get("symbol"),
                    interval=run_cfg.get("interval"),
                    period=run_cfg.get("period"),
                    indicators=run_cfg.get("indicators"),
                    params=run_cfg.get("params"),
                    heikin_ashi_flag=run_cfg.get("heikin_ashi"),
```

**REPLACE**
```python
            try:
                res = backtest.run(
                    symbol=run_cfg.get("symbol"),
                    interval=run_cfg.get("interval"),
                    period=run_cfg.get("period"),
                    indicators=run_cfg.get("indicators"),
                    params=run_cfg.get("params"),
                    heikin_ashi_flag=run_cfg.get("heikin_ashi"),
                    # ── sizing / costs: without these Auto-Test is a different strategy ──
                    capital=float(run_cfg.get("capital", 100000)),
                    position_pct=float(run_cfg.get("position_pct", 100)),
                    position_amount=float(run_cfg.get("position_amount", 5000)),
                    fixed_qty=int(run_cfg.get("fixed_qty", 1)),
                    sizing_mode=run_cfg.get("sizing_mode", "fixed"),
                    commission_pct=float(run_cfg.get("commission_pct", 0.05)),
                    slippage_pct=float(run_cfg.get("slippage_pct", 0.02)),
                    base_qty=int(run_cfg.get("base_qty", 1)),
                    max_levels=int(run_cfg.get("max_levels", 5)),
                    multiplier_type=run_cfg.get("multiplier_type", "martingale"),
                    recovery_buffer_amount=float(run_cfg.get("recovery_buffer_amount", 20.0)),
                    recovery_trail_enabled=bool(run_cfg.get("recovery_trail_enabled", False)),
                    recovery_trail_premium=float(run_cfg.get("recovery_trail_premium", 40.0)),
                    recovery_trail_step=float(run_cfg.get("recovery_trail_step", 5.0)),
                    trailing_start_pct=float(run_cfg.get("trailing_start_pct", 3.0)),
                    trailing_step_pct=float(run_cfg.get("trailing_step_pct", 2.0)),
                    trailing_tighten_enabled=bool(run_cfg.get("trailing_tighten_enabled", False)),
                    trailing_tighten_at=float(run_cfg.get("trailing_tighten_at", 9.0)),
                    trailing_tighten_step=float(run_cfg.get("trailing_tighten_step", 1.0)),
                    wt_buy_max=float(run_cfg.get("wt_buy_max", -40.0)),
                    wt_sell_min=float(run_cfg.get("wt_sell_min", 40.0)),
                    rsi_period=int(run_cfg.get("rsi_period", 14)),
                    vol_mult=float(run_cfg.get("vol_mult", 1.5)),
                    vol_lookback=int(run_cfg.get("vol_lookback", 20)),
```

---

### 2.3 🔴 Backtest includes the still-forming last candle; live does not
`backtest.py` simulates `for i in range(1, n)` over **all** bars, so the final (possibly half-formed) candle can generate a signal and a trade.
Live engines explicitly drop it:
* `autotrader.py:358` → `df_closed = df_ha.iloc[:-1]`
* `trading_setup_engine.py:446` → `df_closed = df_ha.iloc[:-1]`

So the backtest can book a trade the Auto-Trader could never have taken — the exact "1:1 parity" claim in PROJECT.md §2.

**FIND** (`backtest.py` ≈ line 1014)
```python
    volume_series = df["Volume"] if "Volume" in df.columns else None
    df_real = df.copy()
```

**REPLACE** (optional flag so it can be A/B tested — this changes all existing results)
```python
    volume_series = df["Volume"] if "Volume" in df.columns else None
    df_real = df.copy()

    # ── parity with live engines (autotrader.py / trading_setup_engine.py) ──
    # live only ever sees CLOSED candles; drop the forming last bar here too.
    if drop_last_bar and len(df) > 41:
        df        = df.iloc[:-1]
        df_real   = df_real.iloc[:-1]
        if volume_series is not None:
            volume_series = volume_series.iloc[:-1]
```
…and add to the signature next to `heikin_ashi_flag=True,`:
```python
        heikin_ashi_flag=True, fno_mode="none", fno_sheet="F&O_input", fno_list=None,
        drop_last_bar=False,
```
Set `drop_last_bar=True` only when you run a parity check against the live panel.

---

### 2.4 🟠 Sharpe annualisation is wrong for 60m / 4h / 5d / 1wk>X timeframes
`backtest.py:1145`

```python
bpy = {"5m": 75*252, "15m": 25*252, "30m": 13*252,
       "1h": 6*252, "1d": 252, "1wk": 52}.get(interval, 252)
```
The TF dropdown offers 1m, 2m, 5m, 15m, 30m, **60m**, 90m, **4h**, 1d, **5d**, 1wk, 1mo, 3mo. `60m` (1584 bars/yr) and `4h` (~378) fall through to **252**, so their Sharpe is understated by ~2.5× and ~1.2×. Also 4h is **not a valid yfinance interval** — that dropdown entry will always end in "not enough data".

**FIND**
```python
    bpy = {"5m": 75*252, "15m": 25*252, "30m": 13*252,
           "1h": 6*252, "1d": 252, "1wk": 52}.get(interval, 252)
```

**REPLACE**
```python
    bpy = {"1m": 375*252, "2m": 188*252, "5m": 75*252,
           "15m": 25*252, "30m": 13*252,
           "60m": 6*252, "1h": 6*252, "90m": 4*252, "4h": 2*252,
           "1d": 252, "5d": 252, "1wk": 52, "1mo": 12, "3mo": 4}.get(interval, 252)
```

---

### 2.5 🟠 Volume filter compares a bar with an average that includes itself
`backtest.py:427` — `vol_avg` at the trigger bar already contains that bar's volume, so a real 3× spike averages to a 1.9× spike against itself, and the threshold is harder to hit than intended (and is not how a live filter would behave).

**FIND**
```python
    if C["vol_filter_enabled"] and volume_series is not None:
        vol_avg = volume_series.rolling(C["vol_lookback"]).mean()
```

**REPLACE**
```python
    if C["vol_filter_enabled"] and volume_series is not None:
        vol_avg = volume_series.shift(1).rolling(C["vol_lookback"]).mean()
```

---

### 2.6 🟠 Prices are unadjusted (`auto_adjust=False`) → splits/dividends create phantom trades
`data.py:168, 217, 262, 289` — all four yfinance calls pass `auto_adjust=False`. A 1:2 split appears as a −50 % candle: the backtest books a stop-out at the fake price (and the S/R pivots and WT values around it are junk). The live Setup engine has a 20 % gap guard (`trading_setup_engine.py`) — **the backtest has none**.

⚠️ `data.py` and `indicators/*` are **READ-ONLY** per PROJECT.md §7.1. This needs your explicit approval. Two options:

| Option | Change | Risk |
|---|---|---|
| A | `auto_adjust=False` → `True` on all 4 calls | Correct, but every historical result shifts; also changes Scanner/Setup LTP paths |
| B | Add a gap guard inside `backtest.run()` that refuses to trade a bar whose `|Close/prev Close − 1| > 20 %` | Local, safe, no read-only change |

Recommend **B now, A later** as a deliberate migration.

**FIND** (`backtest.py`, right after the `n < 40` guard)
```python
    n = len(df)
    if n < 40:
        return {"ok": False, "error": "not enough bars after filtering"}
```

**REPLACE**
```python
    n = len(df)
    if n < 40:
        return {"ok": False, "error": "not enough bars after filtering"}

    # ── split / bad-tick guard (mirror of the 20% rule in trading_setup_engine.py) ──
    _bad_bars = 0
    if "Close" in df.columns and n > 1:
        _r = df["Close"].pct_change().abs()
        _bad_bars = int((_r > 0.20).sum())
        if _bad_bars:
            print(f"⚠️ {symbol}: {_bad_bars} bar(s) moved >20% — possible split/bad tick")
```

---

### 2.7 🟡 "Auto-Test Results — sorted by highest return" is not sorted
`templates/backtest.html:200` says sorted; the rows are appended in **generation order** (`connectAutoStream` → `$('autoTestBody').appendChild(tr)`), and there is no sort call anywhere in that handler.

**FIND** (`templates/backtest.html:200`)
```html
  <div class="chartCap">Auto-Test Results — sorted by highest return</div>
```

**REPLACE** (honest caption + client-side sort on finish)
```html
  <div class="chartCap">Auto-Test Results — click any header to sort (best scored at top after finish)</div>
```
…and inside `connectAutoStream`, in the `done` branch:
```js
    } else if (msg.type === 'done' || msg.type === 'stopped') {
      $('atProgress').textContent = `Finished! (${msg.count} valid results)`;
      localStorage.removeItem('active_auto_job');
      closeAutoStream();
    }
```
**REPLACE**
```js
    } else if (msg.type === 'done' || msg.type === 'stopped') {
      $('atProgress').textContent = `Finished! (${msg.count} valid results)`;
      // sort by Return % desc (col 2 of each row)
      const rows = [...$('autoTestBody').querySelectorAll('tr')];
      rows.sort((a, b) =>
        parseFloat(b.children[2].textContent) - parseFloat(a.children[2].textContent));
      rows.forEach(r => $('autoTestBody').appendChild(r));
      localStorage.removeItem('active_auto_job');
      closeAutoStream();
    }
```

---

### 2.8 🟡 Auto-Test re-downloads the same symbol for every combo
Each combo calls `run()` → `download()` → yfinance. A 3×3×2 = 18-combo sweep on one symbol hits Yahoo 18 times (~30-60 s wasted, and rate-limit risk on 300+ combos). `run()` already accepts `df_override` — download once, reuse.

**FIND** (`autotest.py`, first lines of `worker()` after the docstring-free body starts)
```python
    def worker():
        # 1. Build the options for each varied key
        options = {k: OPTION_MAP[k] for k in vary_keys if k in OPTION_MAP}
```

**REPLACE**
```python
    def worker():
        # 0. Download the symbol ONCE and reuse the DataFrame for every combo
        df_base = None
        try:
            from data import download as _dl
            from indicators.common import candles_to_df as _c2df
            _rows = _dl(base_cfg.get("symbol"), base_cfg.get("interval", "1h"),
                        base_cfg.get("period", "6mo"))
            if _rows:
                df_base = backtest._to_ist(_c2df(_rows))
                print(f"📦 Auto-Test: cached {len(df_base)} bars for {base_cfg.get('symbol')}")
        except Exception as e:
            print(f"⚠️ Auto-Test: pre-download failed ({e}) — falling back to per-run download")

        # 1. Build the options for each varied key
        options = {k: OPTION_MAP[k] for k in vary_keys if k in OPTION_MAP}
```
…and add one line to the `backtest.run(...)` call (next to `period=...`):
```python
                    period=run_cfg.get("period"),
                    df_override=df_base,
```
⚠️ Note: with `df_override`, `run()` skips the download **and** the `fno_mode` filter block is still applied afterwards, so F&O behaviour is unchanged. Do **not** set `drop_last_bar` here.

---

## 3. What is already good (verified, do not "fix")

| Item | Evidence |
|---|---|
| Entry is only checked on the bar **after** the trigger bar; exits are checked before entries → no same-bar stop-after-entry cheat | `_simulate_simple` pass 1 / pass 2 order |
| Commission charged on **both** legs and netted from cash exactly once | `cash += units*fill - comm`, `net = ... - comm - entry["commission"]` |
| Slippage has the correct **sign** for long and short on both entry and exit | `fill = px * (1 + ev_sign*slip/100)` vs `(1 - side*slip/100)` |
| Integer quantity, cash-sufficiency check, no fractional shares | `_qty_for()` |
| Targets/SL locked to entry price, not to the moving LTP | `_target_px(C, ep, ...)` uses `ep` |
| Trailing SL is monotonic (never loosens) | `_trailing_sl_price` derives from `best_price` |
| Equity length == bar count; max-DD computed on the equity array, not on trade P&L | `equity=[cash]` + append per bar |
| `same_side` / `alternate` correctly refuse >1 signal source | `run()` guard |
| Batch build uses an explicit kwargs whitelist (no `symbol` collision, no `TypeError`) | `server.py:1932-1984` |
| Filters are tagged per event and surfaced in the UI (opacity + `[WT>max]` tag) | `_apply_filters` + `renderEvents` |

---

## 4. P1 — Missing statistics that will change your decisions

`run()` computes: return, B&H, max DD, Sharpe, trades, win %, PF, avg win/loss, expectancy, max win/loss.
**Not** computed → and each one is a real blind spot:

| Missing | Why it matters here |
|---|---|
| **Exposure / time in market** | A 3 % max DD on 8 % exposure is a different strategy from one on 90 % |
| **Avg / median hold bars** | Tells you whether a 6-month/1h test even has enough trades to trust |
| **Sortino** | Your downside is asymmetric (stops vs trailing targets); Sharpe punishes winners |
| **Calmar (return ÷ max DD)** | The single best ranking column for Auto-Test (see §5.1) |
| **Longest DD duration (bars)** | Max DD % hides a 4-month underwater stretch |
| **Max consecutive losses** | Needed for martingale/recovery sizing sanity checks |
| **Long vs Short win %** (only ₹ total exists today) | PROJECT.md §8 added the ₹ cards; win % is still missing |
| **Total cost ₹ (commission + slippage)** | Users routinely read gross P&L as net |
| **Monthly returns table** | The only honest way to see regime dependence |

Add this block — no simulator changes needed, everything derives from `trades`:

**FIND** (`backtest.py:1159-1164`)
```python
    max_win  = max((t["pnl"] for t in trades), default=0)
    max_loss = min((t["pnl"] for t in trades), default=0)

    total_events = len(events)
    passed_events = sum(1 for e in events if not e["filtered"])
```

**REPLACE**
```python
    max_win  = max((t["pnl"] for t in trades), default=0)
    max_loss = min((t["pnl"] for t in trades), default=0)

    # ── derived risk stats (no simulator changes) ──
    down_rets = [r for r in rets if r < 0]
    sortino = 0.0
    if len(down_rets) > 1 and statistics.stdev(down_rets) > 0:
        sortino = (statistics.mean(rets) / statistics.stdev(down_rets)) * (bpy ** 0.5)
    calmar = (total_ret / abs(max_dd)) if max_dd < 0 else (999.0 if total_ret > 0 else 0.0)

    _hold = [t.get("bars", 0) for t in trades]
    avg_bars = round(sum(_hold) / len(_hold), 1) if _hold else 0
    med_bars = round(statistics.median(_hold), 1) if _hold else 0
    exposure_pct = round(min(100.0, sum(_hold) / max(1, n - 1) * 100), 1)

    _streak = _worst_streak = 0
    for t in trades:
        _streak = _streak + 1 if t["pnl"] <= 0 else 0
        _worst_streak = max(_worst_streak, _streak)

    _longs  = [t for t in trades if t["side"] == "long"]
    _shorts = [t for t in trades if t["side"] == "short"]
    long_stats = {
        "trades": len(_longs),
        "net": round(sum(t["pnl"] for t in _longs), 2),
        "win_rate": round(sum(1 for t in _longs if t["pnl"] > 0) / len(_longs) * 100, 1) if _longs else 0,
    }
    short_stats = {
        "trades": len(_shorts),
        "net": round(sum(t["pnl"] for t in _shorts), 2),
        "win_rate": round(sum(1 for t in _shorts if t["pnl"] > 0) / len(_shorts) * 100, 1) if _shorts else 0,
    }
    total_cost = round(sum(abs(t["capital_used"]) * C["commission_pct"] / 100 * 2
                           for t in trades), 2)

    monthly = {}
    for t in trades:
        k = str(t["exit_t)"][:7] if False else str(t["exit_t"])[:7]
        monthly[k] = round(monthly.get(k, 0.0) + t["pnl"], 2)

    total_events = len(events)
    passed_events = sum(1 for e in events if not e["filtered"])
```
> ⚠️ In the block above, use `k = str(t["exit_t"])[:7]` — the ugly expression is only there so the FIND anchor stays unique in your editor; delete the `if False else` branch when you paste it.

Then extend the metrics dict:

**FIND** (`backtest.py:1211-1216`)
```python
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
        },
        "ms": int((time.time() - t0) * 1000),
```

**REPLACE**
```python
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
            "sortino": round(sortino, 2),
            "calmar": round(calmar, 2),
            "avg_bars": avg_bars, "median_bars": med_bars,
            "exposure_pct": exposure_pct,
            "max_consec_losses": _worst_streak,
            "long": long_stats, "short": short_stats,
            "total_cost": total_cost,
            "monthly": monthly,
        },
        "ms": int((time.time() - t0) * 1000),
```

UI cards (2 new rows, nothing removed) — **FIND** (`templates/backtest.html:1384-1390`)
```js
    ['Profit factor',  m.profit_factor.toFixed(2), cls(m.profit_factor - 1)],
    ['Expectancy',     Math.round(m.expectancy).toLocaleString(), cls(m.expectancy)],
```
**REPLACE**
```js
    ['Profit factor',  m.profit_factor.toFixed(2), cls(m.profit_factor - 1)],
    ['Sortino',        (m.sortino || 0).toFixed(2), cls(m.sortino || 0)],
    ['Calmar',         (m.calmar || 0).toFixed(2), cls(m.calmar || 0)],
    ['Exposure',       (m.exposure_pct ?? 0) + '%', ''],
    ['Avg hold (bars)', (m.avg_bars ?? 0), ''],
    ['Max losing streak', (m.max_consec_losses ?? 0), (m.max_consec_losses ? 'sell' : '')],
    ['Total costs ₹',  Math.round(m.total_cost || 0).toLocaleString(), 'sell'],
    ['Expectancy',     Math.round(m.expectancy).toLocaleString(), cls(m.expectancy)],
```

Batch rows should carry the same fields — **FIND** (`batch_backtest.py:44-56`)
```python
        "capital": m["capital"],
        "final_equity": m["final_equity"],
        "best_trade":  {"side": best["side"],  "pnl": best["pnl"]}  if best  else None,
```
**REPLACE**
```python
        "capital": m["capital"],
        "final_equity": m["final_equity"],
        "expectancy": m.get("expectancy", 0),
        "avg_win": m.get("avg_win", 0),
        "avg_loss": m.get("avg_loss", 0),
        "sortino": m.get("sortino", 0),
        "calmar": m.get("calmar", 0),
        "exposure_pct": m.get("exposure_pct", 0),
        "total_cost": m.get("total_cost", 0),
        "best_trade":  {"side": best["side"],  "pnl": best["pnl"]}  if best  else None,
```

---

## 5. P2 — New pages worth adding (ranked by value ÷ effort)

You currently have **no cross-symbol view, no run comparison, no export, and no live-vs-backtest check.** That is the whole gap.

### 5.1 ⭐ Strategy Lab — `/optimizer` (highest value)
The Auto-Test page can only toggle **booleans / enums**. The parameters that actually decide profitability — `profit_target_%`, `stop_loss_%`, `trailing_start/step`, `wait_bars`, `buffer_pts` — cannot be swept at all.

* Numeric grid: `target 1/2/3/5 %` × `SL 1/2/3 %` × `trail 2/3 %` = 36 runs, streamed.
* **In-sample / out-of-sample split** (e.g. first 70 % / last 30 % of bars) with an OOS verdict per combo — without this, a 300-combo sweep is pure curve-fitting.
* **Rank score** instead of raw return: `score = calmar * sqrt(trades)` with a `trades >= 10` guard, so 1 lucky trade cannot win.
* "Apply to Backtest" button → writes the combo into `localStorage` preset and redirects to `/backtest`.
* Files: `web/templates/optimizer.html` (new), `web/optimizer.py` (new, reuses `backtest.run` with `df_override`), route in `server.py`, nav link in `base.html`.
* Touches only new files + 2 additive lines in `server.py` / `base.html`. **No read-only file changes.**

### 5.2 ⭐ Run Comparison — `/compare`
You can save unlimited presets, but you can only ever look at **one** result. Add: pick 2-6 saved runs → side-by-side metric table, **overlaid equity curves**, Δ vs B&H, per-trade diff.
* Persist each run to `web/data/bt_runs/<ts>_<symbol>_<tf>.json` (currently only `LAST_BACKTEST_CACHE` in RAM — restart loses everything).
* Files: `templates/compare.html`, `routes/backtest_history.py` or 3 routes in `server.py`, `data/bt_runs/` (git-ignored).

### 5.3 ⭐ Portfolio Roll-up — `/analytics`
Batch mode produces 40 rows and a SUM strip; there is **no portfolio view**: no combined equity curve, no monthly heat-map, no "which TF/symbol is dragging".
* Aggregate the stored `bt_runs/` per (TF, period) → portfolio equity, correlation of return %, monthly P&L heat-map, top/bottom 10, per-side totals.
* Add **CSV export** here (see §6) — there is currently **zero export anywhere in the app**.

### 5.4 Live vs Backtest Parity — `/parity`
PROJECT.md §2 claims the Auto-Trader is a "1:1 port of backtest.py", but nothing verifies it.
* For one symbol/TF: run `backtest.run(drop_last_bar=True)`, load `setup_trade_history.json` + `paper_trades.json`, and diff: entry/exit time, fill price, slippage in points, missed signals (in backtest, not live), phantom signals (live, not backtest).
* Output: coverage %, avg fill slippage, list of divergences with a reason column.
* This is the page that will catch the §2.1 / §2.3 bugs automatically on every future change.

### 5.5 Data Health — `/data-health`
yfinance silently returns short/gappy data; `data.py` prints warnings to the terminal that nobody reads.
* Per symbol/TF: bars returned, first/last candle, **missing-bar count vs expected session grid**, `>20 %` gap bars (split detector), stale (last candle older than 2× TF), cache age/TTL, valid `_TF_PERIODS` matrix.
* Add a "refresh cache" button (`data.clear_cache()` already exists) and surface `download()` failures per symbol.

### 5.6 Trade Journal — `/journal`
`setup_trade_history.json`, `paper_trades.json`, `live_processed.json` and backtest trades live in four unrelated formats with no notes column.
* One table, filters (symbol / TF / source / result / date), free-text note per trade, tags, R-multiple, CSV export.

### 5.7 Symbol Manager — `/symbols`
`allocations.json` / `setup_allocations.json` can only be edited by typing into the panels, and symbol lists otherwise come from a Google Sheet (a hard dependency: batch and `fno_mode=sheet` both break without Sheets auth).
* Local CRUD: add/remove symbol, TF per symbol, broker symbol token, enable/disable, tag (F&O / cash / forex), bulk paste.

**Also small but genuinely useful:** a `?` **Help / Docs page** that renders `PROJECT.md` + `PANEL_LOGIC.md` at `/docs` — right now the logic lives in Markdown only you read.

---

## 6. Quick wins (each < 20 lines, no new page)

| # | Win | Where |
|---|---|---|
| 1 | **CSV export** of trades / events / batch rows. Nothing in the app exports anything today. | new `/api/backtest/export.csv` + one button in `backtest.html` |
| 2 | **Date-range filter** (`from`/`to`) — you can only choose `period` (1mo…2y), so "test Jan-Mar only" is impossible | `run(start=None, end=None)` + 2 date inputs |
| 3 | **Auto-Test min-trades guard** — hide/mark rows with `trades < 10` instead of ranking them #1 | `autotest.py` row builder |
| 4 | **Persist batch + auto-test results** to `data/` — restart currently wipes both (only live jobs are replayable) | `batch_backtest.JOBS` → JSON on `done` |
| 5 | **F&O filter vs Forex** — PROJECT.md §5 already flags that `fno_mode="all"` hits Forex symbols; `data.py:_SYMBOL_MAP` is the right place to skip them | needs read-only approval |
| 6 | **Remove the invalid `4h` option** from the TF dropdown (yfinance has no 4h interval → guaranteed "not enough data") | `templates/backtest.html:29` |
| 7 | **Doc drift**: PROJECT.md says "Six pages", base nav has seven (`/paper-trading`); §3 lists `order_queue.json` but `data/` ships `setup_paper_orders_queue.json` | PROJECT.md §3 |

---

## 7. Housekeeping (repo root — not app code)

`web 2.zip`, `web 3.zip`, `web 4.zip`, `web.zip` (~1.6 MB), `.to_delete/`, `.DS_Store`, `0001-jso1.patch`, `1-4hrbot1 copy.py`, plus ~60 stale root-level `*.json` (`Nsr_trade_15m.json`, `trigger_history.json`, …) and a root-level `backtest/` folder that duplicate the real app in `web/`. PROJECT.md §6 rule 3 already warns "Two backtest.py files — always edit `web/backtest.py`"; the fastest way to keep that rule true is to delete the duplicates.

---

## 8. Suggested execution order

1. **§2.1 HTF look-ahead** + **§2.2 Auto-Test parity** — both change numbers you make decisions on.
2. **§4 metrics** (sortino / calmar / exposure / streaks / per-side win %) — 20 lines, unlocks §5.1 ranking.
3. **§6.1 CSV export** + **§6.2 date range** — smallest effort, biggest day-to-day relief.
4. **§5.2 Compare** → **§5.1 Strategy Lab** (Compare is a prerequisite: an optimiser you can't audit is dangerous).
5. **§5.4 Parity** — run it after every engine change, not once.
6. **§5.3 / §5.5 / §5.6 / §5.7** in whatever order hurts least.

---

### Files that must not be touched without explicit approval (PROJECT.md §7.1)
`autotrader.py`, `trading.html`, `paper_trades.py`, `backtest.py`, `batch_backtest.py`, `autotest.py`, `data.py`, `sheets.py`, `settings.py`, `settings_store.py`, `broker_zerodha.py`, `indicators/*`

⚠️ **Four fixes above (2.1, 2.4, 2.5, 4) live in `backtest.py`** and **two more (2.6, 4's batch row) live in `data.py` / `batch_backtest.py`** — all on that list. Per your own rules they need your sign-off before anyone applies them. Nothing was modified in this review.