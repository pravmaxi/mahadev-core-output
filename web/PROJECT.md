# Mahadev Trading Platform — PROJECT.md

Last updated: November 2026 (Session 27)

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

**Auto-Trader State Persistence (Session 27 Update):**
- **Server Restart Auto-Resume:** `server.py` and `autotrader.py` now save the job config to `active_job_config.json` and the last scan state to `auto_state_cache.json`. If the server restarts, it automatically reloads the state and resumes the auto-trader thread seamlessly.
- **UI Polling:** The frontend polls `/api/trade/auto/status` every 5 seconds. The server responds with the cached `AUTO_STATE`.

---

## Trading Page & Auto-Trader Engine (Session 27 Update)

The Auto-Trader is now a direct port of the `backtest.py` logic, ensuring 1:1 parity with backtests.

### 1. Signal Detection (Backtest Matching)
- Scans only **CLOSED candles** (`df_ha.iloc[:-1]`) to prevent repainting.
- Uses `get_active_pending_signals()` to find Pending and Triggered signals exactly like the backtest engine.
- **Trigger Level:** The High (for Buy) or Low (for Sell) of the valid signal candle.
- **Live Breakout:** If LTP crosses the Trigger Level *during* the scan, it triggers immediately.

### 2. Market Order Bypass Logic
- If `Allow Market Orders` is **UNCHECKED**:
  - The bot saves the signal level to `last_ignored_sig`.
  - Subsequent scans check `last_ignored_sig`:
    - If the new signal is the **same** level, it ignores it.
    - If the new signal is **different**, it bypasses the checkbox and executes a Market order (unless it's the very first scan).
- Limit orders are **always** allowed, regardless of this checkbox.

### 3. Queueing & Execution
- **Instant Processing:** The queue is processed per-symbol immediately after a signal is found (not waiting for the full scan loop to finish).
- **Queue Replacement:** If a new signal arrives for a symbol, existing pending limit orders are cancelled. Exception: The bot keeps `_exit` orders so it can close and flip positions simultaneously.
- **Data Gap Protection:** If the Trigger Level is >20% away from LTP (e.g., due to a stock split or bad Yahoo data), the signal is ignored and marked "Ignored (Data Gap)".
- **Averaging (`same_side`):** Tracks levels, allows new entries up to `max_levels`, and appends "(Level X)" to the status.

### 4. Position & Exit Management
- **EXIT Orders:** When a position closes (Target, Stop Loss, or Opp Signal), the order is tagged (e.g., `Exit (Target)`) and the UI shows **EXIT** in orange in the Side column.
- **Net Positions:** `paper_trades.py` nets positions by `(symbol, exchange, product)` ignoring the tag, so a BUY and EXIT SELL properly cancel out to 0 qty.
- **Target/SL Lock:** Targets are calculated using the locked Trigger Level, not the fluctuating live LTP.

### 5. UI Columns (Scanner Replica)
- **Symbol, TF, LTP:** Standard pricing.
- **WT Trend:** Current trend.
- **Valid Candle:** The timestamp of the signal candle.
- **Trigger Lvl, WT Val:** The signal's breakout price and indicator value.
- **WT Action:** Raw signal (e.g., "Buy Pending", "Sell Triggered").
- **Overall Status:** Pending, Order Placed, Position Open, Ignored (Market Off), Ignored (Data Gap).
- **Sent To:** Shows `pa (LIMIT)` or `pa (MARKET)`.
- **Target, Stop Loss:** Locked to trigger level.
- **Trigger Time:** When the breakout happened.
- **Last Changes:** Persistent log of what changed (e.g., `Target: 100 -> 105`).

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
    │   │   ├── active_job_config.json← ✅ NEW: Auto-resume state for server restarts
    │   │   ├── auto_state_cache.json ← ✅ NEW: UI cache for instant load
    │   │   ├── paper_processed.json  ← Tracks executed paper signals
    │   │   ├── paper_orders_queue.json ← Signals waiting to be executed
    │   │   ├── paper_trades.json     ← The actual paper ledger (orders + fills)
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

## Known Issues & Gotchas

1. **Yahoo Finance Data Gaps:** If Yahoo returns a bad tick (e.g., pre-split price), the bot's 20% gap protection prevents it from placing a bad order.
2. **ST + F&O Filter = Broken Pivots.** Always turn the F&O filter OFF for ST.
3. **Two backtest.py files.** Ensure you are editing `web/backtest.py`, not the root one.
4. **SSE Replay Duplicates.** When reconnecting to a stream, the frontend must filter duplicates.
5. **Google Sheet Book Name.** In single backtest, the F&O Book name must be typed manually if it differs from "Stock Price Scraper".
6. **SessionStorage/LocalStorage.** If the UI behaves weirdly after a code update, hard refresh (`Cmd+Shift+R`) and clear SessionStorage. 
7. **Heikin Ashi Warmup Period (Critical):** Always force downloads to use the maximum allowed period (e.g., `60d` for 15m, `3mo` for 1h) to match the Backtest.
8. **Python 3.8+ Syntax:** Never use `continue` or `break` inside a `finally:` block.
9. **LTP Rate-Limiting:** Do not fetch LTP for paper positions on a continuous interval. Use the manual Fetch LTP button.
10. **Terminal Log Spam:** `server.py` uses a `PollingFilter` to silence continuous 5-second polling logs (`/status`, `/positions`, `/orders`).
11. **Exact Find & Replace:** When providing fixes, ALWAYS provide 1-2 lines of code immediately BEFORE and AFTER the target block so I can locate it easily.
12. **Context Limit Warning:** If the chat exceeds 80% context limit, warn me immediately so I can start a new chat.