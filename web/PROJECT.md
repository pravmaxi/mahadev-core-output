# Mahadev Trading Platform — PROJECT.md

Last updated: October 2026 (Session 25)

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
If you start a Scanner run, Batch Backtest, or Auto-Test, you can navigate to other pages. The Python backend keeps running in a background thread, and the browser remembers the `job_id`. When you return, the frontend reconnects to the SSE stream, replays the history, and continues showing live progress.

**Trading Page Auto-Trader (Session 25 Update):**
- **Queue Architecture**: Auto-Trader does NOT place trades directly.
  1. `autotrader.py` detects a signal and writes it to `data/paper_orders_queue.json`.
  2. `_process_paper_queue()` runs in the loop, reads the queue, executes via `paper_trades.py`.
  3. It moves the record to `data/paper_processed.json` with Signal Level, Signal Time, and Exec Time.
  4. Prevents duplicate trades using a unique `sig_id`.
- **Limit Order Logic**: Limit orders fill ONLY when `LTP > Limit` (for Buy) or `LTP < Limit` (for Sell). Market orders fill instantly.
- **Price Fallback**: If queue `price` is missing/0, the bot falls back to `signal_level` or `ltp` to prevent empty orders.
- **Duplicate Prevention**: If a position is already open for a symbol, `wt_action` becomes "Position in progress" and skips the queue. If a limit order is pending, it becomes "Pending LIMIT".
- **Settings Sync**: "Apply Settings" pushes UI config directly to the running job thread.
- **Auto-Scan Pause**: Unchecking "Auto-Scan" sends `{ paused: true }` to halt the `_worker` loop.
- **Fetch LTP Button**: A lightweight `/api/trade/auto/fetch_ltp` endpoint updates the UI instantly without running a full scan. Disabled during Auto-Scan to prevent yfinance clashes.
- **F&O Filter Optimization**: The F&O Google Sheet is ONLY read if `allocations.json` contains NSE/BSE stocks.

---

## Forex & Gold Support (Session 25 Update)

Trading Forex (e.g., `EURUSD`, `XAUUSD`) required several fixes:
- **Symbol Mapping**: `autotrader.py` and `server.py` use `_SYMBOL_MAP` from `data.py` to translate `EURUSD` → `EURUSD=X`, `XAUUSD` → `GC=F`, etc.
- **5-Decimal Precision**: 
  - Backend (`autotrader.py`): `_r()` helper rounds to 5 decimals if `abs(val) < 10`.
  - Ledger (`paper_trades.py`): `positions()` rounds avg price to 5 decimals for Forex.
  - Frontend (`trading.html`): `fmtINR()` dynamically shows 5 decimals for prices < 10.
- **Yahoo Finance Limits**: `5m`, `15m`, `30m` data is limited to `60d`. `1h` is limited to `3mo`. `autotrader.py` and `data.py` now strictly enforce these periods to prevent batch download crashes.

---

## Folder Layout

    mahadev core output/
    ├── PROJECT.md
    ├── NEW_INDICATOR_TEMPLATE.md
    ├── venv/
    ├── web/                          ← THE APP (run everything from here)
    │   ├── server.py                 ← Flask entry point & API routes
    │   ├── autotrader.py             ← Background worker (Queue writer & GSheet)
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
    │   ├── data/                     ← state files
    │   │   ├── trigger_state.json
    │   │   ├── watchlist.json
    │   │   ├── allocations.json      ← Saved symbols & TF for Auto-Trader
    │   │   ├── paper_processed.json  ← Tracks executed paper signals (with times/levels)
    │   │   ├── paper_orders_queue.json ← Signals waiting to be executed
    │   │   ├── zerodha_processed.json
    │   │   └── zerodha_orders_queue.json
    │   ├── indicators/
    │   │   ├── __init__.py           ← REGISTRY
    │   │   ├── common.py             ← shared helpers (HA, F&O filter)
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
2. **From sheet:** Reads a specific Google Sheet. (Note: skips reading the sheet entirely if there are no NSE/BSE stocks in `allocations.json`).
3. **Off:** Disables the filter completely.

*Critical Note:* ST (Support/Resistance) **must** run with this filter turned OFF, otherwise the pivot detection breaks.

---

## Indicators

### T3 (Tillson) - Trend smoothing. Columns: t3_value, colour, valid candle, trigger, action, LTP, diff, profit, booking.
### WaveTrend (WT) - WT1/WT2 crossover.
- **Pending Candle Rule (`wt_pending_rule`):** `every` (all crosses alive), `same_dir` (new buy kills old buy), `any_dir` (new buy kills old buy AND sell).
### Chandelier Exit (CE) - Direction flips generate signals.
### Support/Resistance (ST) - LuxAlgo style pivot logic.

---

## Backtest Engine (`backtest.py`)

### Strategy Modes
- `simple`: Single position.
- `same_side`: Averaging (requires multiplier: martingale/incremental/constant).
- `alternate`: Flip sides on opposite signal.

### Recovery Overlay
*Not a strategy mode.* A flag (`recovery_enabled`) that overlays on simple/same_side/alternate.
- Modes: `off`, `last`, `cumulative`.

---

## Trading Page (`trading.html` & `autotrader.py`)

1. **UI Layout (Session 25)**: 
   - Start/Stop Auto-Trading and Apply Settings buttons are in the top header.
   - Auto-Trading Setup, Scanner Replica, Paper Positions, and Paper Orders are all collapsible blocks.
   - Removed "WT1", "WT2", "WT Cross" columns from Scanner Replica to reduce clutter.
   - Added multi-level sorting (up to 5 levels) for all tables by clicking headers.
2. **Scanner Replica (Live):**
   - Added a "Fetch LTP" checkbox, interval input, and manual button. Disables during Auto-Scan to prevent clashes.
   - Shows "Sent To", "Target", and "Stop Loss" columns.
3. **Paper Positions:**
   - Includes LTP, Target, and Stop columns synced from the scanner.
   - P&L is calculated dynamically based on Scanner LTP.
4. **Paper Orders:**
   - Shows both Limit Price and actual Fill Price if they differ.
   - Limit orders stay `OPEN` until triggered.
5. **JSON Debug:** A panel to view `allocations.json` and the execution queue files.

---

## Known Issues & Gotchas

1. **ST + F&O Filter = Broken Pivots.** Always turn the F&O filter OFF for ST.
2. **Two backtest.py files.** Ensure you are editing `web/backtest.py`, not the root one.
3. **SSE Replay Duplicates.** When reconnecting to a stream, the frontend must filter duplicates.
4. **Google Sheet Book Name.** In single backtest, the F&O Book name must be typed manually if it differs from "Stock Price Scraper".
5. **SessionStorage.** If the UI behaves weirdly after a code update, hard refresh (`Cmd+Shift+R`) and clear SessionStorage.
6. **Heikin Ashi Warmup Period (Critical):** Always force downloads to use the maximum allowed period (e.g., `60d` for 15m, `3mo` for 1h) to match the Backtest.
7. **Yahoo Finance Timeout/Limits:** `5m` data is strictly `60d`. `yfinance.download()` can hang. `autotrader.py` wraps downloads in a timeout.
8. **Python 3.8+ Syntax:** Never use `continue` or `break` inside a `finally:` block.
9. **LTP Rate-Limiting:** Do not fetch LTP for paper positions on a continuous interval. Use the manual Fetch LTP button.
10. **Terminal Log Spam:** `server.py` uses a `PollingFilter` to silence continuous 5-second polling logs (`/status`, `/positions`, `/orders`).
11. **Exact Find & Replace:** When providing fixes, ALWAYS provide 1-2 lines of code immediately BEFORE and AFTER the target block so I can locate it easily.
12. **Context Limit Warning:** If the chat exceeds 80% context limit, warn me immediately so I can start a new chat.