# Mahadev Trading Platform — PROJECT.md

Last updated: October 2026 (Session 20)

---

## App Overview

A Flask-based trading platform with five pages: Scanner, Backtest, Broker, Trading, Settings.
Data comes from yfinance (OHLC) and Zerodha Kite (live broker + LTP).
Results can be written to Google Sheets and reloaded later.

Run from `web/` folder only:
    cd web
    python server.py
The app runs at http://localhost:8000

---

## Core Architecture: Background Jobs & Persistence

The app uses **Server-Sent Events (SSE)** and **localStorage** for long-running tasks. 
If you start a Scanner run, Batch Backtest, or Auto-Test, you can navigate to other pages. The Python backend keeps running in a background thread, and the browser remembers the `job_id`. When you return, the frontend reconnects to the SSE stream, replays the history, and continues showing live progress without losing data.

- **Backend:** Uses `queue.Queue` and `threading.Thread` to process jobs. Results are stored in global dictionaries (`SCANNER_JOBS`, `JOBS` for batch/autotest).
- **Frontend:** Uses `EventSource` to listen to `/api/*/stream?job=...`. Relies on `localStorage` to store `active_scan_job`, `active_batch_job`, and `active_auto_job`.

---

## Folder Layout

    mahadev core output/
    ├── PROJECT.md
    ├── NEW_INDICATOR_TEMPLATE.md
    ├── venv/
    ├── web/                          ← THE APP (run everything from here)
    │   ├── server.py                 ← Flask entry point
    │   ├── backtest.py               ← backtest engine
    │   ├── batch_backtest.py         ← batch job runner
    │   ├── autotest.py               ← Auto-Test combination engine
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
    │   │   ├── t3.py
    │   │   ├── wavetrend.py
    │   │   ├── chandelier.py
    │   │   └── support_resistance.py
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

## F&O 15:15 Filter (The "Smart" Filter)

Applies to intraday timeframes (5m, 15m, 30m, 1h). Drops candles that start at 15:15 IST or later to prevent fake late-day signals.
Has 3 modes controlled by a dropdown (`cfg_fno_mode`):
1. **All symbols:** Applies the filter to every stock blindly.
2. **From sheet:** Reads a specific Google Sheet (defined by `cfg_fno_book` and `cfg_fno_sheet`). Only applies the filter to stocks found in that tab.
3. **Off:** Disables the filter completely (uses all candles).

*Critical Note:* ST (Support/Resistance) **must** run with this filter turned OFF, otherwise the pivot detection breaks.

---

## Indicators

### T3 (Tillson)
Trend smoothing. Columns: t3_value, colour, valid candle, trigger, action, LTP, diff, profit, booking.

### WaveTrend (WT)
WT1/WT2 crossover.
- Bull Cross = valid buy candle. Bear Cross = valid sell candle.
- **Pending Candle Rule (`wt_pending_rule`):**
  - `every`: All crosses stay alive.
  - `same_dir`: New buy kills old buy. New sell kills old sell.
  - `any_dir`: New buy kills old buy AND old sell (strict trend following).

### Chandelier Exit (CE)
Direction flips generate signals.

### Support/Resistance (ST) — LuxAlgo style
Uses strictly greater left/right pivot logic. Real candles only.
- **Valid Candle:** High/Close beyond R (buy) or Low/Close below S (sell).
- **Entry/Exit modes:** `strict` (wait for later bar breakout) or `loose` (immediate fill at close).
- **Re-entry mode (`st_reentry_mode`):**
  - `standard`: Take the next signal.
  - `one_per_opposite`: Wait for a sell before the next buy.
  - `fresh_r`: Wait for R to change value before re-entering.

---

## Backtest Engine (`backtest.py`)

### Strategy Modes
- `simple`: Single position.
- `same_side`: Averaging (requires multiplier: martingale/incremental/constant).
- `alternate`: Flip sides on opposite signal.

### Recovery Overlay
*Not a strategy mode.* A flag (`recovery_enabled`) that overlays on simple/same_side/alternate.
- Modes: `off`, `last` (recover only most recent loss), `cumulative` (recover all losses).
- Sets next target to: pending loss + buffer.

### Auto-Test (`autotest.py`)
A UI panel on the Backtest page to run multiple combinations in the background.
- User ticks checkboxes (e.g., Strategy Mode, Side, WT Pending Rule).
- UI calculates total combinations (e.g., 3 × 3 × 2 = 18 runs).
- Backend loops through `itertools.product()` and streams results sorted by highest return.

---

## Scanner (`scanner.html`)

Runs on background threads. Saves `active_scan_job` to localStorage so scans survive page navigation.
- Multi-indicator checkboxes.
- Write-to-sheet always defaults to ON.
- Output tab auto-named from ticked indicators + timeframe (e.g., `zero_WT_ST_1h`).

---

## Broker & Trading

- Zerodha Kite Connect via `broker_zerodha.py`.
- Negative cash warning banner (requires `[hidden]{display:none !important}` in CSS).
- Paper trades ledger (`paper_trades.py`) + live orders.
- LTP resolution: Zerodha → yfinance → fast_info.

---

## Known Issues & Gotchas

1. **ST + F&O Filter = Broken Pivots.** Always turn the F&O filter OFF for ST.
2. **Two backtest.py files.** Ensure you are editing `web/backtest.py`, not the root one.
3. **SSE Replay Duplicates.** When reconnecting to a stream, the backend replays history, but the frontend must filter duplicates (e.g., `if (!batchRows.find(r => r.symbol === msg.row.symbol))`) to avoid alphabetical duplicates.
4. **Google Sheet Book Name.** In single backtest, the F&O Book name must be typed manually (`cfg_fno_book`) if it differs from "Stock Price Scraper".
5. **SessionStorage.** If the UI behaves weirdly after a code update, hard refresh (`Cmd+Shift+R`) and clear SessionStorage.