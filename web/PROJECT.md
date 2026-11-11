# Mahadev Trading Platform — PROJECT.md

Last updated: November 2026 (Post-Session 27: Virtual Panel Overhaul)

---

## App Overview

A Flask-based trading platform. Six pages: Scanner, Backtest, Broker, Trading, Trade Setup, Settings.

Data comes from yfinance (OHLC) and Zerodha Kite (live broker + LTP). Results can be written to Google Sheets and reloaded later.

Run from `web/` folder only:
    cd web
    python server.py
The app runs at http://localhost:8000

Two development tracks live in this repo:

A. Existing app (Session 27 and earlier)
   Scanner, Backtest, Broker, Trading (Auto-Trader), Settings.
   Documented in sections 1–6 below. Treat as WORKING and READ-ONLY for Trading Setup work.

B. Trading Setup (Virtual Panel)
   Independent module at /trade-setup. It no longer relies on paper_trades.py for UI display. 
   Documented in sections 7–9 below. This is the active development track.

---

## 1. Core Architecture: Background Jobs & Persistence

The app uses Server-Sent Events (SSE) and localStorage for long-running tasks. If you start a Scanner run, Batch Backtest, or Auto-Test, you can navigate to other pages. The Python backend keeps running in a background thread, and the browser remembers the job_id. When you return, the frontend reconnects to the SSE stream, replays the history, and continues showing live progress.

**Auto-Trader State Persistence (Session 27 Update):**
- Server Restart Auto-Resume: `server.py` and `autotrader.py` save the job config to `active_job_config.json` and the last scan state to `auto_state_cache.json`. If the server restarts, it automatically reloads the state and resumes the auto-trader thread.
- UI Polling: the frontend polls `/api/trade/auto/status` every 5 seconds. The server responds with the cached `AUTO_STATE`.

**Trading Setup Persistence:**
- Trading Setup does NOT participate in this auto-resume system. Its job lifecycle is independent and must not be started by server startup. It relies on `setup_state_cache.json` and `setup_trigger_state.json`.

---

## 2. Trading Page & Auto-Trader Engine (Session 27 Update)

The Auto-Trader is a direct port of `backtest.py`, ensuring 1:1 parity with backtests. This section describes ONLY the existing Auto-Trader (`web/autotrader.py` + `web/templates/trading.html`). It is not the Trading Setup engine.

### 2.1 Signal Detection
- Scans only CLOSED candles (`df_ha.iloc[:-1]`) to prevent repainting.
- Uses `get_active_pending_signals()` to find Pending and Triggered signals exactly like the backtest engine.
- Trigger Level: High (Buy) or Low (Sell) of the valid signal candle.
- Live Breakout: if LTP crosses the Trigger Level during the scan, it triggers immediately.

### 2.2 Market Order Bypass Logic
- If Allow Market Orders is UNCHECKED: the bot saves the signal level to `last_ignored_sig`. Subsequent scans check `last_ignored_sig`. Same level → ignored. Different level → bypass the checkbox and execute a Market order (unless it's the very first scan). Limit orders are always allowed.

### 2.3 Queueing & Execution
- Instant Processing: queue is processed per-symbol immediately after a signal is found.
- Queue Replacement: if a new signal arrives for a symbol, existing pending limit orders are cancelled. Exception: keeps `_exit` orders so it can close and flip positions simultaneously.
- Data Gap Protection: if Trigger Level is >20% away from LTP, the signal is ignored and marked "Ignored (Data Gap)".
- Averaging (same_side): tracks levels, allows new entries up to max_levels, appends "(Level X)" to the status.

### 2.4 Position & Exit Management
- EXIT Orders: when a position closes (Target, SL, or Opp Signal), the order is tagged (e.g., "Exit (Target)") and the UI shows EXIT in orange.
- Net Positions: `paper_trades.py` nets positions by (symbol, exchange, product) ignoring the tag.
- Target/SL Lock: targets use the locked Trigger Level, not the fluctuating live LTP.

---

## 3. Folder Layout

    mahadev core output/
    ├── PROJECT.md
    ├── NEW_INDICATOR_TEMPLATE.md
    ├── venv/
    ├── web/                          ← THE APP (run everything from here)
    │   │
    │   ├── server.py                 ← Flask entry point & API routes
    │   ├── autotrader.py             ← Existing Auto-Trader engine (READ-ONLY)
    │   ├── backtest.py               ← backtest engine (READ-ONLY)
    │   ├── batch_backtest.py         ← batch job runner (READ-ONLY)
    │   ├── autotest.py               ← Auto-Test combination engine (READ-ONLY)
    │   ├── data.py                   ← yfinance wrapper + cache (READ-ONLY)
    │   ├── sheets.py                 ← Google Sheets reader/writer (READ-ONLY)
    │   ├── settings.py               ← env loading, IST, YF_CONFIG
    │   ├── settings_store.py         ← .env read/write
    │   ├── broker_zerodha.py         ← Kite Connect wrapper
    │   ├── paper_trades.py           ← paper trading ledger (READ-ONLY)
    │   ├── paper_trading.py          ← Flask blueprint (paper routes)
    │   │
    │   ├── trading_setup_engine.py   ← ★ Trading Setup Virtual Engine (ACTIVE)
    │   │
    │   ├── .env                      ← secrets
    │   │
    │   ├── data/                     ← state files
    │   │   │
    │   │   │  ── Scanner / shared ──
    │   │   ├── trigger_state.json    ← Scanner trigger memory
    │   │   ├── watchlist.json
    │   │   │
    │   │   │  ── Auto-Trader (existing) ──
    │   │   ├── allocations.json      ← Auto-Trader symbols & TF
    │   │   ├── active_job_config.json← Auto-resume config
    │   │   ├── auto_state_cache.json ← UI cache
    │   │   ├── paper_processed.json  ← Executed paper signals
    │   │   ├── paper_orders_queue.json ← Auto-Trader paper pending queue
    │   │   ├── zerodha_processed.json
    │   │   └── zerodha_orders_queue.json
    │   │
    │   │   │  ── Trading Setup (Virtual Panel) ──
    │   │   ├── setup_allocations.json      ← Setup symbols & TF
    │   │   ├── setup_state_cache.json      ← Setup monitor cache
    │   │   ├── setup_trigger_state.json    ← Trigger memory (survives restart)
    │   │   ├── setup_trade_history.json    ← Closed trades log (Accordion UI)
    │   │   ├── order_queue.json            ← Setup paper pending queue
    │   │   ├── live_order_queue.json       ← Setup live pending queue
    │   │   ├── setup_paper_processed.json  ← Setup paper processed history
    │   │   └── live_processed.json         ← Setup live processed history
    │   │
    │   │   │  ── Paper ledger (shared) ──
    │   │   ├── paper_trades.json     ← Orders + positions ledger used by
    │   │   │                            paper_trades.py
    │   │
    │   ├── indicators/
    │   │   ├── __init__.py           ← REGISTRY (list_all, get)
    │   │   ├── common.py             ← candles_to_df, heikin_ashi, apply_fno_filter
    │   │   ├── t3.py                 ← key: t3
    │   │   ├── wavetrend.py          ← key: wt
    │   │   ├── chandelier.py         ← key: ce
    │   │   ├── support_resistance.py ← key: st
    │   │   └── ...                   ← (DO NOT MODIFY any of these)
    │   │
    │   └── templates/
    │       ├── base.html
    │       ├── scanner.html
    │       ├── backtest.html
    │       ├── broker.html
    │       ├── trading.html          ← Existing Trading page (READ-ONLY)
    │       ├── trading_setup.html    ← ★ Trading Setup UI (ACTIVE)
    │       └── settings.html
    │
    └── (ignored: root-level backtest.py, server.py, etc. — stale duplicates)

---

## 4. Indicators (Summary)
- T3 (Tillson)
- WaveTrend (WT) — handles Pending Candle Rule (every, same_dir, any_dir)
- Chandelier Exit (CE) — direction flips generate signals
- Support/Resistance (ST) — LuxAlgo-style pivot logic

---

## 5. F&O 15:15 Filter
Applies to intraday timeframes. Drops candles that start at 15:15 IST or later to prevent fake late-day signals.
- Modes: All, From sheet, Off.
- **Critical:** ST must run with this filter OFF.
- **Trading Setup note:** `fno_mode == "all"` currently also hits Forex symbols. Any fix must be scoped so the Auto-Trader path is not affected.

---

## 6. Existing Known Issues & Gotchas (Auto-Trader & Core)
1. Yahoo Finance Data Gaps — 20% gap protection prevents bad orders.
2. ST + F&O Filter = Broken Pivots. Always turn F&O off for ST.
3. Two backtest.py files. Always edit `web/backtest.py`.
4. SSE Replay Duplicates — frontend must filter duplicates on reconnect.
5. Google Sheet Book Name — for single backtest, the F&O Book name must be typed manually if it differs from "Stock Price Scraper".
6. SessionStorage/LocalStorage — hard refresh and clear SessionStorage after code updates.
7. Heikin Ashi Warmup Period — always force downloads to use maximum allowed period (60d for 15m, 3mo for 1h).
8. Python 3.8+ Syntax — never use continue or break inside finally:.
9. LTP Rate-Limiting — do not fetch LTP for paper positions on a continuous interval. Use the manual Fetch LTP button.
10. Terminal Log Spam — `server.py` uses a `PollingFilter` to silence continuous polling logs.
11. Exact Find & Replace — ALWAYS provide 1-2 lines of code immediately BEFORE and AFTER the target block.
12. Context Limit Warning — if the chat exceeds 80% of its context limit, warn the user immediately.

---

## 7. Trading Setup — Virtual Panel (Active Track)

Trading Setup is now a fully self-contained "Virtual Trading Panel". It calculates positions, Target/SL, P&L, and History entirely in memory. It does not rely on `paper_trades.py` for the UI display.

### 7.1 Independence rules (non-negotiable)
1. Do NOT modify `autotrader.py`, `trading.html`, `paper_trades.py`, `backtest.py`, `batch_backtest.py`, `autotest.py`, `data.py`, `sheets.py`, `settings.py`, `settings_store.py`, `broker_zerodha.py`, or `indicators/*`.
2. Do NOT reuse Auto-Trader globals (`AUTO_JOBS`, `AUTO_STATE`, `trigger_state.json`, etc.).
3. Do NOT auto-execute on page load or server restart.
4. Never assume a feature is implemented because it was discussed. Verify the file.
5. Prefer complete-file replacements or exact FIND / REPLACE with surrounding context.
6. Keep the app on port 8000.

### 7.2 The 4-Step State Machine
1. **ORDER QUEUE:** New valid signals go here first.
2. **ORDER:** Active order (`MARKET` fills instantly, `LIMIT` waits for LTP cross). If flat and no active order exists, the queue item instantly promotes to the ORDER column.
3. **POSITION:** Once filled, shows `LONG X` or `SHORT Y`. Live P&L calculated on every tick.
4. **EXIT:** Target, SL, or Opp Signal triggers an exit. P&L is banked into `TOTAL P&L`, logged to `setup_trade_history.json`, and the next item from `ORDER QUEUE` is instantly promoted.

### 7.3 Engine Logic (`trading_setup_engine.py`)
- **Auto-Scan & Pause:** The engine checks `is_first_scan`. It runs exactly one scan on start. After that, if `paused=True` (Auto-Scan off), it sleeps and stops scanning. This prevents wiping `New Trigger` statuses.
- **Send to Queue:** If the UI checkbox is ON, it writes to `order_queue.json`, `live_order_queue.json`, and calls `paper_trades.py`. If OFF, it skips all disk I/O for ultra-fast virtual scanning.
- **20% Gap Filter:** If `LTP` vs `TRIGGER LVL` gap > 20%, it assumes a stock split/data glitch and flags the row as `Data Glitch (Gap > 20%)`, preventing false orders.
- **Target/SL Lock:** If a position is open, Target/SL is calculated from `pos_entry` (not the fluctuating LTP).
- **Stop Button:** `_worker` checks `stop_job` inside the `for task in tasks:` loop to halt immediately.

### 7.4 Server Logic (`server.py`)
- `_update_queues_risk()`: When Target/SL is edited, it checks for a pending `NEW_ORDER` in the JSON queues and patches it. If the order is already processed, it appends an `UPDATE_RISK` command.
- **DATA_DIR Monkey-Patch:** `paper_trades.py` is read-only and missing `DATA_DIR`. `server.py` injects it on startup: `paper_trades.DATA_DIR = Path(__file__).resolve().parent / "data"`. Do not remove this.
- **Polling Filter:** Silences the 5-second `/status`, `/history`, and `/json_files` logs.

### 7.5 UI Features (`trading_setup.html`)
- **Frozen Columns:** SYMBOL and LTP are frozen on the left during horizontal scroll.
- **Editable Columns:** TARGET and STOP LOSS can be edited directly in the panel.
- **Order Trigger Price:** Hidden (`—`) for pending limits, populated only when the position is active.
- **Trade History:** Accordion-style grouping by symbol. Shows Last Entry/Exit/P&L in the summary bar. Saves open state during 5-second polling.

---

## 8. Session Changelog (Fixes applied this session)
1. **UI Cleanup:** Removed duplicate JSON debug boxes. Fixed `fetchJsonFiles()` JS.
2. **Stop Button Bug:** `_worker` loop now checks for `stop_job` inside the `for task in tasks:` loop. Frontend clears `scannerLtpIntId` and `ltpIntervalId` on stop.
3. **Pause Loop Bug:** Added `is_first_scan` check to prevent infinite background scanning when Auto-Scan is off.
4. **Queue Routing:** Engine now respects `send_to_queue` toggle.
5. **Target/SL Recalculation Bug:** When a position is open, target/SL is calculated from `pos_entry` (not the fluctuating LTP). 
6. **Trade History Bug:** History now correctly saves `entry_time` and uses the `pos_side` to determine Target/SL.
7. **Accordion UI Bug:** History stays open during 5-second polling by tracking `openSymbols` in JS.
8. **Missing DATA_DIR:** Monkey-patched in `server.py` to prevent `AttributeError` crashes.

---

## 9. Development Workflow (for all future sessions)
1. Attach only the files needed for the current step. Do not attach unrelated files.
2. One step per session. Do not proceed to the next step until verified.
3. Provide exact FIND / REPLACE blocks with ~10 lines of surrounding context, OR a complete replacement file.
4. Do not assume a fix is applied because it was proposed.
5. Do not touch read-only files.
6. If a fix requires a read-only file to change, stop and ask for explicit approval.
7. If the chat exceeds 80% of the context limit, warn immediately.