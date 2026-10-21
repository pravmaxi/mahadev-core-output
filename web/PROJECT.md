# Mahadev Trading Platform — PROJECT.md

Last updated: 2026-10-16 (Session 17)

---

## What this is

A Flask-based trading platform with four pages: Scanner, Backtest, Broker, Trading.
Data comes from yfinance (OHLC) and Zerodha Kite (live broker + LTP).
Results can be written to Google Sheets and reloaded later.

Run from `web/` folder only:

    cd web
    python server.py

The app runs at http://localhost:8000

---

## Folder layout

    mahadev core output/
    ├── PROJECT.md                    ← this file
    ├── NEW_INDICATOR_TEMPLATE.md     ← template for new indicators
    ├── venv/                         ← virtual env
    ├── web/                          ← THE APP (run everything from here)
    │   ├── server.py                 ← Flask entry point
    │   ├── backtest.py               ← backtest engine
    │   ├── batch_backtest.py         ← batch job runner
    │   ├── data.py                   ← yfinance wrapper + cache
    │   ├── sheets.py                 ← Google Sheets reader/writer
    │   ├── settings.py               ← env loading, IST, YF_CONFIG
    │   ├── settings_store.py         ← .env read/write
    │   ├── broker_zerodha.py         ← Kite Connect wrapper
    │   ├── paper_trades.py           ← paper trading ledger
    │   ├── .env                      ← secrets
    │   ├── data/                     ← trigger_state.json, watchlist.json
    │   ├── indicators/
    │   │   ├── __init__.py           ← REGISTRY
    │   │   ├── common.py             ← shared helpers
    │   │   ├── t3.py                 ← Tillson T3
    │   │   ├── wavetrend.py          ← WaveTrend
    │   │   ├── chandelier.py         ← Chandelier Exit
    │   │   └── support_resistance.py ← LuxAlgo S/R (ST)
    │   └── templates/
    │       ├── base.html
    │       ├── scanner.html
    │       ├── backtest.html
    │       ├── broker.html
    │       ├── trading.html
    │       └── settings.html
    └── (ignored: root-level backtest.py, server.py, etc. — stale duplicates)

**Never run Python from the project root.** Always `cd web` first.

---

## Core conventions

- **Timezone**: IST everywhere. UTC stored in data, converted at boundaries.
- **Integers only for quantity.**
- **Candle source**: yfinance daily/hourly OHLC. Heikin Ashi is applied as an
  optional transform, EXCEPT for the ST indicator which always uses real candles.
- **Cache TTL**: 300 s for OHLC batches.
- **Trigger state**: persisted to `web/data/trigger_state.json` so the scanner
  knows which signals are "New" vs already seen.

---

## Indicator registry pattern

Every indicator module in `web/indicators/` exposes:

    META = {
        "key":    "wt",
        "label":  "WaveTrend",
        "defaults": {...},
        "columns": [{"key": "...", "label": "..."}, ...],
    }

    def compute(df, params) -> dict_of_series
    def signals(df, params, computed, state=None) -> dict_of_values
        # must include `_new_state` for persistence, and `<key>_triggered` bool

`indicators/__init__.py` maps short keys to modules:

    REGISTRY = {
        "t3": t3,
        "wt": wavetrend,
        "ce": chandelier,
        "st": support_resistance,
    }

To add a new indicator, follow NEW_INDICATOR_TEMPLATE.md.

---

## The four indicators

### T3 (Tillson)
Key: `t3`. Trend-following smoothing. Columns: t3_value, t3_colour, valid candle
fields, trigger fields, action, LTP, diff, profit, booking status, max profit.

### WaveTrend (WT)
Key: `wt`. WT1/WT2 crossover indicator.
- Bull Cross (WT1 crosses above WT2) → blue valid candle, high = trigger level
- Bear Cross (WT1 crosses below WT2) → yellow valid candle, low = trigger level
- Next candle breaking trigger level = trigger candle
- Standard valid-candle → trigger-candle flow (same as ST)
- Columns: wt1, wt2, wt_diff, wt_cross, wt_trend, valid_*, perfect, trigger_*,
  ltp, trigger_diff, diff_pct, profit, booking_status, max_profit_*

### Chandelier Exit (CE)
Key: `ce`. Direction flips generate signals. Same column shape as WT.

### Support/Resistance (ST) — LuxAlgo style
Key: `st`. Also called "S/R" or "SR".

**Chart semantics (what TV shows):**
- Red line = resistance (R). Blue line = support (S).
- R is drawn at the pivot bar, but the value only becomes "usable" `right+1`
  bars later (Pine's `pivothigh(...)[1]` semantics).
- The delay is intrinsic — no code can remove it. `right=15` → 16-bar delay.

**Pivot rule:** strictly greater than left bars AND strictly greater than right
bars (Pine's `pivothigh`). Ties ignored.

**Valid candle rules:**
- Buy valid = bar where `High > R` AND `Close > R`. Trigger level = that bar's High.
- Sell valid = bar where `Low < S` AND `Close < S`. Trigger level = that bar's Low.
- Buy and sell pendings tracked independently.
- A pending buy dies when R changes value. Same for sell when S changes.
- Once set, a pending is NOT replaced by later bars — first valid candle wins.

**Modes (per-pass):**
- `strict`: valid candle first, then a later bar must break the trigger level.
- `loose`: fire on every bar where price is beyond R/S. Entry price = bar's Close.

**Entry vs exit passes:**
- Entry pass uses `st_entry_mode`.
- Exit pass uses `st_exit_mode`.
- The two passes are merged; shared events become `role="both"`.

**Re-entry modes (`st_reentry_mode`):**
- `standard`: after any close, take the next valid signal. (Default — same
  behaviour as WT/CE.)
- `one_per_opposite`: after a buy close, wait for a sell signal before the next
  buy (mirror for sell). Effect: only one buy between each sell.
- `fresh_r`: after a buy close, ignore the R value that existed at close. Only
  re-enter once R has changed to a NEW value and a valid candle forms on it.
  Mirror for sell using S.

**Critical gotcha:** ST must run with **F&O 15:15 filter OFF**. When the filter
is on, some bars are dropped before detection and pivots shift. Same in scanner
and backtest.

**ST columns** (all prefixed `st_`):
resistance, support, trend, valid_open/high/low/close/dt, valid_resistance,
prev_close, r_change_dt, s_change_dt, dist_to_res_pct, dist_to_sup_pct,
rsi, rsi_dir, perfect, trigger_status, trigger_dt, action,
trigger_candle_date, trigger_candle_time, ltp, trigger_diff, diff_pct,
profit, booking_status, max_profit_pct, max_profit_value.

**Left/Right bars default:** 15/15.

---

## Backtest engine — `backtest.py`

### Strategy modes
- `simple`: single position at a time.
- `same_side`: averaging down in one direction (multiplier martingale/incremental/constant).
- `alternate`: flip sides on opposite signal (cycle-based, tracks legs).
- **`recovery` is NOT a mode.** It's a flag `recovery_enabled` that overlays on
  any strategy mode. See below.

### Recovery overlay (any strategy mode)

Options:
- **Off** (default): normal profit target.
- **Last trade only**: after a losing close, next trade's target = loss + buffer
  in ₹. A win clears pending. Next loss REPLACES pending.
- **Cumulative**: losses accumulate; wins reduce pending down to zero.

Params: `recovery_enabled`, `recovery_cumulative`, `recovery_buffer_amount`,
`recovery_trail_enabled`, `recovery_trail_premium`, `recovery_trail_step`.

Recovery-specific trailing SL activates only once peak profit ≥ target + premium.

### Filters
- WT extreme (`wt_filter_enabled`)
- HTF trend (`htf_filter_enabled` — EMA50 vs EMA200 on higher TF)
- RSI rising/falling (`rsi_filter_enabled`)
- Volume spike (`vol_filter_enabled`)

### Key simulation order per bar
1. Manage open position: stop → trailing → target → recovery target.
2. Pass 1: exits (opposite signals with role ∈ {exit, both}).
3. Pass 2: entries (same-direction signals with role ∈ {entry, both}).
4. Append equity.

### S/R diagnostic (debug tool)
`/api/backtest/sr_segments` returns R/S segments with `start_i`, `end_i`,
`usable_from_i`. Used by the "📐 S/R diagnostic" button in backtest.html.
The diagnostic and the live ST indicator must use identical pivot logic —
this was the source of a bug where TV matched but backtest didn't.

---

## Scanner — `scanner.html`

Sources: manual symbols, Google Sheet, local watchlist, or both.

Features:
- Multi-indicator checkboxes with per-indicator settings sections.
- Multi-level sort (click header = single, shift-click = add a level).
- Filter panel + filter presets + input presets (saved in localStorage).
- Column picker (hide/show columns, per-indicator grouping).
- Auto-refresh with sound alert on new triggers.
- "Load saved" dropdown → reads previous scan output from Sheets.
- **Write results to sheet** is always ON at page load (user can toggle off).

Result columns are auto-named from ticked indicators + timeframe,
e.g. `zero_WT_ST_1h`.

**Indicator-specific gotcha:** if a tab has columns from multiple indicators
with shared labels (e.g. both WT and ST have "Valid Open"), the loader mis-maps
them. Workaround: save single-indicator tabs. Long-term fix: prefix labels with
the indicator group.

---

## Broker — `broker.html`

- Zerodha Kite Connect via `broker_zerodha.py`.
- Login flow: opens browser, captures access token, saves to `data/`.
- Shows positions, holdings, orders, tradebook, GTT, margins.
- GTT create/modify/delete gated by `ZERODHA_LIVE_TRADING=1` in `.env`.
- **Negative cash warning**: banner with pulsing red border + beep when
  available cash is negative. Requires `[hidden]{display:none !important}` in
  the style block — inline `display:flex` otherwise wins over the hidden attribute.
- Top-bar Refresh button visible only when connected.

---

## Trading — `trading.html`

- Paper trades ledger (`paper_trades.py`) + live Zerodha orders.
- LTP resolution: Zerodha → yfinance daily → fast_info.
- Paper trade positions get mark-to-market P/L from live LTP.

---

## Settings — `settings.html`

Editable `.env` keys (via `settings_store.py`):

    PORT, APP_PASSWORD
    ZERODHA_API_KEY, ZERODHA_API_SECRET
    ZERODHA_CALLBACK_PORT, ZERODHA_LIVE_TRADING

Secret is masked on read; leave blank to keep current value.

---

## Google Sheets

`sheets.py` handles all reads/writes.

- Service account email shown in Settings.
- Tabs auto-created on write if missing.
- `read_symbols()` auto-suffixes `.NS` / `.BO` / nothing based on tab name
  (`us_*` = no suffix, `*bse*` = `.BO`, else `.NS`).
- `write_results(sheet, tab, headers, rows, summary=None)` — summary writes to
  row 1, header + data below.
- `read_results(sheet, tab)` — finds the header row by looking for "Symbol"
  in the first 5 rows, then returns a list of dicts.

---

## Known issues / gotchas

1. **ST + F&O filter = wrong pivots.** Leave F&O filter OFF for ST scans and
   backtests. Strict mode relies on pivot bars that get dropped otherwise.

2. **Two backtest.py files.** One in `web/` (correct) and one in the project
   root (stale). Always `cd web` before running anything. If grep says the fix
   isn't in the file, you're probably editing the root copy.

3. **Loose mode price**. Fire on every bar beyond R/S. Entry = bar's Close
   with buffer 0 override (not R). Otherwise every entry lands at the same
   price (R doesn't change often).

4. **banner CSS.** `[hidden]{display:none !important}` must exist in any
   page with an inline `display:flex` element that also uses the `hidden`
   attribute. Otherwise the attribute is ignored.

5. **sessionStorage**. Scanner and Backtest persist form state per browser tab.
   If code changes but UI still shows old behaviour, clear sessionStorage
   (F12 → Application → Session Storage → Clear) or open a fresh tab.

6. **The `_simulate_recovery` function is deprecated.** Recovery is now a flag
   applied inside each simulator. Dead code, safe to delete.

---

## Common commands

    # Start server (always from web/)
    cd web
    python server.py

    # Run an indicator's self-test
    python indicators/support_resistance.py

    # Check something exists in the running code
    grep -n "def debug_st_bars" backtest.py

    # Force Flask to reload after a registry change
    # Ctrl+C in the Flask terminal, then run server.py again

---

## Files to keep clean

- `web/indicators/__init__.py` — registry. Adding an indicator = 2 lines here.
- `web/server.py` — `_params_for(ind_key, body)` maps request body → indicator
  params. Add a branch for each new indicator.
- `web/templates/scanner.html` — `buildColumns()`, `runSelected()`,
  `viewSelected()`, `allColumnsGrouped()`, `colsHideAll()` all need the new
  key added. Same for `backtest.html` when ST/WT/CE parity is added.

---

## Session history (brief)

- Session 17 (current): ST indicator moved into backtest. Recovery overlay
  reworked (no longer a strategy mode). ST re-entry modes (standard /
  one-per-opposite / fresh-R). Loose mode fixed to fire on bar-close price.
  S/R diagnostic added. Multiple bugfixes on pivot detection.

Earlier sessions built: base Flask app, yfinance wrapper, T3, WT, CE indicators,
scanner with filters/sort/column picker, backtest with strategies, broker with
Zerodha, paper trades, settings page.