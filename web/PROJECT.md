# Mahadev Trading Platform — PROJECT.md

Last updated: November 2026 (Session 27 base + post-27 Trading Setup additions)

---

## App Overview

A Flask-based trading platform. Six pages: Scanner, Backtest, Broker, Trading,
Trade Setup, Settings.

Data comes from yfinance (OHLC) and Zerodha Kite (live broker + LTP).
Results can be written to Google Sheets and reloaded later.

Run from `web/` folder only:
    cd web
    python server.py
The app runs at http://localhost:8000

Two development tracks live in this repo:

A. Existing app (Session 27 and earlier)
   Scanner, Backtest, Broker, Trading (Auto-Trader), Settings.
   Documented in sections 1–6 below. Treat as WORKING and READ-ONLY for
   Trading Setup work.

B. Trading Setup (in progress, post-Session 27)
   Independent module at /trade-setup with its own engine, template, and JSON
   state. Documented in section 7 below. This is the active development track.

---

## 1. Core Architecture: Background Jobs & Persistence

The app uses Server-Sent Events (SSE) and localStorage for long-running
tasks. If you start a Scanner run, Batch Backtest, or Auto-Test, you can
navigate to other pages. The Python backend keeps running in a background
thread, and the browser remembers the job_id. When you return, the frontend
reconnects to the SSE stream, replays the history, and continues showing
live progress.

Auto-Trader State Persistence (Session 27 Update):
- Server Restart Auto-Resume: server.py and autotrader.py save the job
  config to active_job_config.json and the last scan state to
  auto_state_cache.json. If the server restarts, it automatically reloads
  the state and resumes the auto-trader thread.
- UI Polling: the frontend polls /api/trade/auto/status every 5 seconds.
  The server responds with the cached AUTO_STATE.

NOTE: Trading Setup (section 7) does NOT participate in this auto-resume
system. Its job lifecycle is independent, and it must not be started by
server startup.

---

## 2. Trading Page & Auto-Trader Engine (Session 27 Update)

The Auto-Trader is a direct port of backtest.py, ensuring 1:1 parity with
backtests. This section describes ONLY the existing Auto-Trader
(web/autotrader.py + web/templates/trading.html). It is not the Trading
Setup engine.

### 2.1 Signal Detection
- Scans only CLOSED candles (df_ha.iloc[:-1]) to prevent repainting.
- Uses get_active_pending_signals() to find Pending and Triggered signals
  exactly like the backtest engine.
- Trigger Level: High (Buy) or Low (Sell) of the valid signal candle.
- Live Breakout: if LTP crosses the Trigger Level during the scan, it
  triggers immediately.

### 2.2 Market Order Bypass Logic
- If Allow Market Orders is UNCHECKED:
  - The bot saves the signal level to last_ignored_sig.
  - Subsequent scans check last_ignored_sig:
    - same level → ignored.
    - different level → bypass the checkbox and execute a Market order
      (unless it's the very first scan).
- Limit orders are always allowed, regardless of this checkbox.

### 2.3 Queueing & Execution
- Instant Processing: queue is processed per-symbol immediately after a
  signal is found.
- Queue Replacement: if a new signal arrives for a symbol, existing pending
  limit orders are cancelled. Exception: keeps _exit orders so it can close
  and flip positions simultaneously.
- Data Gap Protection: if Trigger Level is >20% away from LTP, the signal
  is ignored and marked "Ignored (Data Gap)".
- Averaging (same_side): tracks levels, allows new entries up to max_levels,
  appends "(Level X)" to the status.

### 2.4 Position & Exit Management
- EXIT Orders: when a position closes (Target, SL, or Opp Signal), the
  order is tagged (e.g., "Exit (Target)") and the UI shows EXIT in orange.
- Net Positions: paper_trades.py nets positions by (symbol, exchange,
  product) ignoring the tag, so a BUY and EXIT SELL cancel to 0 qty.
- Target/SL Lock: targets use the locked Trigger Level, not the fluctuating
  live LTP.

### 2.5 UI Columns (Scanner Replica)
Symbol, TF, LTP, WT Trend, Valid Candle, Trigger Lvl, WT Val, WT Action,
Overall Status (Pending / Order Placed / Position Open / Ignored (Market
Off) / Ignored (Data Gap)), Sent To (pa LIMIT / pa MARKET), Target, Stop
Loss, Trigger Time, Last Changes.

---

## 3. Folder Layout

    mahadev core output/
    ├── PROJECT.md
    ├── NEW_INDICATOR_TEMPLATE.md
    ├── venv/
    ├── web/                          ← THE APP (run everything from here)
    │   │
    │   ├── server.py                 ← Flask entry point & API routes
    │   ├── autotrader.py             ← Existing Auto-Trader engine
    │   │                                DO NOT MODIFY for Trading Setup
    │   ├── backtest.py               ← backtest engine
    │   │                                DO NOT MODIFY
    │   ├── batch_backtest.py         ← batch job runner
    │   ├── autotest.py               ← Auto-Test combination engine
    │   ├── data.py                   ← yfinance wrapper + cache
    │   │                                DO NOT MODIFY
    │   ├── sheets.py                 ← Google Sheets reader/writer
    │   │                                DO NOT MODIFY
    │   ├── settings.py               ← env loading, IST, YF_CONFIG
    │   ├── settings_store.py         ← .env read/write
    │   ├── broker_zerodha.py         ← Kite Connect wrapper
    │   ├── paper_trades.py           ← paper trading ledger
    │   │                                DO NOT MODIFY
    │   ├── paper_trading.py          ← Flask blueprint (paper routes)
    │   │                                (present in code; not in the
    │   │                                 Session-27 doc — the doc is
    │   │                                 slightly behind the code here)
    │   │
    │   ├── trading_setup_engine.py   ← ★ Trading Setup engine (post-27)
    │   │                                THIS IS THE ACTIVE DEVELOPMENT FILE
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
    │   │   │  ── Paper ledger (shared) ──
    │   │   ├── paper_trades.json     ← Orders + positions ledger used by
    │   │   │                            paper_trades.py
    │   │   │
    │   │   │  ── Trading Setup (post-27, in progress) ──
    │   │   ├── setup_allocations.json      ← Setup symbols & TF
    │   │   │                                  (written by server.py route)
    │   │   ├── setup_state_cache.json      ← Setup monitor cache
    │   │   ├── setup_trigger_state.json    ← ★ to be added in Step 2
    │   │   ├── setup_paper_processed.json  ← Setup paper processed history
    │   │   ├── order_queue.json            ← ★ Currently used as BOTH
    │   │   │                                  master intent AND paper
    │   │   │                                  pending queue (conflated —
    │   │   │                                  see §7.7 gap #3)
    │   │   ├── live_order_queue.json       ← Setup live pending queue
    │   │   ├── live_processed.json         ← Setup live processed history
    │   │   ├── setup_order_queue.json      ← ★ to be added in Step 3
    │   │   │                                  (separate paper pending queue)
    │   │   └── setup_active_job_config.json← optional restart-resume
    │   │                                      (referenced in code, not
    │   │                                       written today)
    │   │
    │   ├── indicators/
    │   │   ├── __init__.py           ← REGISTRY (list_all, get)
    │   │   ├── common.py             ← candles_to_df, heikin_ashi,
    │   │   │                            apply_fno_filter
    │   │   ├── t3.py                 ← key: t3
    │   │   ├── wavetrend.py          ← key: wt
    │   │   ├── chandelier.py         ← key: ce
    │   │   ├── support_resistance.py ← key: st
    │   │   └── ...                   ← other indicator modules
    │   │                                DO NOT MODIFY any of these
    │   │
    │   └── templates/
    │       ├── base.html
    │       ├── scanner.html
    │       ├── backtest.html
    │       ├── broker.html
    │       ├── trading.html          ← Existing Trading page
    │       │                            DO NOT MODIFY for Trading Setup
    │       ├── trading_setup.html    ← ★ Trading Setup page (post-27)
    │       └── settings.html
    │
    └── (ignored: root-level backtest.py, server.py, etc. — stale duplicates)

Never run Python from the project root. Always `cd web` first.

Note on doc/code lag: `paper_trading.py` is imported by server.py but is not
listed in the Session-27 folder layout above. Treat its existence as
confirmed and add it to any future revisions of this section.

---

## 4. Indicators

### T3 (Tillson)
Trend smoothing. Columns: t3_value, colour, valid candle, trigger, action,
LTP, diff, profit, booking.

### WaveTrend (WT)  — file: indicators/wavetrend.py, key: wt
WT1/WT2 crossover.
- Pending Candle Rule (wt_pending_rule):
  - every    = all crosses stay alive
  - same_dir = new buy kills old buy
  - any_dir  = new buy kills old buy AND sell

### Chandelier Exit (CE)  — file: indicators/chandelier.py, key: ce
Direction flips generate signals.

### Support/Resistance (ST)  — file: indicators/support_resistance.py, key: st
LuxAlgo-style pivot logic.

---

## 5. F&O 15:15 Filter (the "Smart" Filter)

Applies to intraday timeframes (5m, 15m, 30m, 1h). Drops candles that start
at 15:15 IST or later to prevent fake late-day signals. Three modes
controlled by a dropdown (cfg_fno_mode):
1. All symbols: applies the filter to every stock blindly.
2. From sheet: reads a specific Google Sheet. Skips reading the sheet
   entirely if there are no NSE/BSE stocks in allocations.json.
3. Off: disables the filter completely.

Critical Note: ST (Support/Resistance) must run with this filter OFF,
otherwise pivot detection breaks.

Trading Setup note: fno_mode == "all" currently also hits Forex symbols
(see §7.7 gap #13). Any fix must be scoped so the Auto-Trader path is not
affected.

---

## 6. Existing Known Issues & Gotchas

1. Yahoo Finance Data Gaps — 20% gap protection in Auto-Trader prevents
   bad orders.
2. ST + F&O Filter = Broken Pivots. Always turn the F&O filter OFF for ST.
3. Two backtest.py files. Always edit web/backtest.py, never the root one.
4. SSE Replay Duplicates — frontend must filter duplicates on reconnect.
5. Google Sheet Book Name — for single backtest, the F&O Book name must be
   typed manually if it differs from "Stock Price Scraper".
6. SessionStorage/LocalStorage — hard refresh (Cmd+Shift+R) and clear
   SessionStorage after code updates if the UI behaves strangely.
7. Heikin Ashi Warmup Period (Critical) — always force downloads to use the
   maximum allowed period (60d for 15m, 3mo for 1h) to match the Backtest.
8. Python 3.8+ Syntax — never use continue or break inside finally:.
9. LTP Rate-Limiting — do not fetch LTP for paper positions on a continuous
   interval. Use the manual Fetch LTP button.
10. Terminal Log Spam — server.py uses a PollingFilter to silence continuous
    5-second polling logs (/status, /positions, /orders).
11. Exact Find & Replace — when providing fixes, ALWAYS provide 1-2 lines of
    code immediately BEFORE and AFTER the target block.
12. Context Limit Warning — if the chat exceeds 80% of its context limit,
    warn the user immediately so a new chat can be started.

---

## 7. Trading Setup — Independent Module (In Progress, post-Session 27)

The Trading Setup feature is an independent scanner-driven trade
preparation and order-routing module. It uses the scanner data path and
calls paper_trades.py for paper execution, but it does not modify the
existing Auto-Trader or the existing Trading page.

### 7.1 Independence rules (non-negotiable)

1. Do NOT modify web/autotrader.py for Trading Setup work.
2. Do NOT modify web/templates/trading.html for Trading Setup work.
3. Do NOT modify web/paper_trades.py, web/paper_trading.py,
   web/backtest.py, web/batch_backtest.py, web/autotest.py, web/data.py,
   web/sheets.py, web/settings.py, web/settings_store.py,
   web/broker_zerodha.py, or any web/indicators/*. Call them, don't rewrite
   them.
4. Do NOT reuse Auto-Trader globals (AUTO_JOBS, AUTO_STATE, scan thread,
   trigger_state.json, allocations.json, paper_orders_queue.json,
   paper_processed.json, zerodha_orders_queue.json, zerodha_processed.json,
   paper execution path).
5. Do NOT auto-execute on page load, page refresh, server restart,
   allocation save, or config apply. Execution is controlled only by
   explicit start + routing switches.
6. Do NOT change WT Action, WT trigger, or WT execution logic.
7. Never assume a feature is implemented because it was discussed. Verify
   the file.
8. Prefer complete-file replacements or exact FIND / REPLACE with
   surrounding context.
9. Do not bundle unrelated changes. One fix per step, tested.
10. Keep the app on port 8000.

### 7.2 Page and page route

- Navigation label: "Trade Setup"
- Page route: GET /trade-setup
- Template:   web/templates/trading_setup.html
- Active nav: "trade-setup"

The original spec said /trading-setup. The actual route is /trade-setup.
Keep /trade-setup. Do NOT create a duplicate page.

### 7.3 Setup API routes (as actually implemented)

| Method | Path                                  | Purpose                     |
|--------|---------------------------------------|-----------------------------|
| POST   | /api/trade/setup/start                | Start independent setup job |
| POST   | /api/trade/setup/stop                 | Stop setup job              |
| GET    | /api/trade/setup/status               | Job state + scan state      |
| POST   | /api/trade/setup/update_config        | Apply Settings (also does   |
|        |                                       | instant target/SL recalc    |
|        |                                       | and pushes to paper ledger) |
| POST   | /api/trade/setup/trigger_scan         | Force Scan                  |
| POST   | /api/trade/setup/fetch_ltp            | Fetch LTP (no paper sync)   |
| GET    | /api/trade/setup/json_files           | Debug JSON view             |
| POST   | /api/trade/setup/execute_queue        | Process queues              |
| POST   | /api/trade/setup/update_value         | Edit target/SL on a record  |
| POST   | /api/trade/setup/save_allocations     | Persist setup_allocations   |
| POST   | /api/trade/setup/reset_signals        | Clear trigger memory+output |

Spec-only route names that must NOT be created as duplicates:
/api/trade/setup/apply-settings, /force-scan, /update-order, /state.
The functional equivalents above already exist.

### 7.4 Engine (web/trading_setup_engine.py)

Module-level state:
- SETUP_JOBS              dict of running jobs
- SETUP_STATE             dict of monitor rows keyed by "{symbol}_{tf}"
- SETUP_IS_SCANNING       bool
- SETUP_FORCE_SCAN        bool
- SETUP_LAST_SCAN_TIME    float (epoch)
- TRIGGER_STATE           dict of wt trigger memory (currently in-memory only)

Constants:
- DATA_DIR = Path(__file__).resolve().parent / "data"
- SETUP_ALLOC_FILE        = DATA_DIR / "setup_allocations.json"
- SETUP_STATE_FILE        = DATA_DIR / "setup_state_cache.json"
- SETUP_PROCESSED_FILE    = DATA_DIR / "setup_paper_processed.json"
- SETUP_QUEUE_FILE        = DATA_DIR / "order_queue.json"
                            (currently used as paper pending AND idle)
- LIVE_QUEUE_FILE         = DATA_DIR / "live_order_queue.json"
- LIVE_PROCESSED_FILE     = DATA_DIR / "live_processed.json"

Public functions:
- start_job(job_id, config)
- stop_job(job_id)
- execute_queue()
- sync_state_to_ledger()

Internal helpers:
- _worker(job_id)
- _process_queue_file(queue_file, processed_file, mode)
- _check_pending_limits(downloaded_data)
- get_active_pending_signals(df, params, buffer_pts, max_wait, pending_rule)

Data path per symbol:
download_batch → candles_to_df → apply_fno_filter (only for NSE/BSE — see
gap #13) → heikin_ashi (if cfg candles == "ha") → wt.compute →
wt.signals(state=last_state). Forex 1-minute close override applied after
get_live_ltp.

Target/SL/opp_sig:
trigger_lvl = High(Buy) / Low(Sell) of valid candle.
target_val  = trigger_lvl * (1 ± target_pct/100)
sl_val      = trigger_lvl * (1 ∓ stop_pct/100) or opp_sig if stop_pct = 0.

### 7.5 Order queue behaviour (current, before Step 3)

- Eligibility: wt_trigger_status == "New Trigger" AND valid == "YES" AND
  "Buy" or "Sell" in wt_action.
- sig_id = f"{raw_sym}_{tf}_{side}_{sig_time}", where
    sig_time = row.get("valid_time", str(df_real.index[-1]))
  valid_time is NOT currently placed on row, so the fallback (current bar's
  timestamp) is used. Result: sig_id changes every scan, and dedup against
  setup_paper_processed.json fails.
- paper_trade on: append to order_queue.json (deduped by sig_id) and
  execute_queue() places via paper_trades.place_order(...). The call does
  NOT pass target/sl/opp_sig, and paper_trades.json has no target/SL for
  those orders.
- live_trade on: append to live_order_queue.json (no execution).
- both off: append to order_queue.json as "idle" (defect — see gap #3).

### 7.6 Reset and Apply Settings

- reset_signals clears SETUP_STATE, TRIGGER_STATE, setup_state_cache.json,
  setup_paper_processed.json, order_queue.json, and cancels open paper
  orders. Does NOT delete live_order_queue.json or live_processed.json.
- update_config (Apply Settings) recalculates target_val / sl_val for every
  SETUP_STATE row and calls sync_state_to_ledger() to patch open paper
  orders and open paper positions in paper_trades.json.

### 7.7 Confirmed gaps vs spec (as of this revision)

Priority ordering is indicative. Do not fix these in a single step.

High priority:
1. Trigger memory lost on restart.
   start_job does TRIGGER_STATE.clear() and never reloads it. No
   setup_trigger_state.json exists.
2. sig_id unstable.
   valid_time is not placed on row, so sig_id uses the current bar's
   timestamp and changes every scan. Result: duplicate paper orders on
   restart, re-add, or re-fire.
3. No master queue.
   order_queue.json is both the "master intent" and the "paper pending"
   queue. Toggling Paper Trade ON retroactively executes prior idle
   entries.
4. Failed queue items dropped.
   _process_queue_file catches per-item exceptions, appends only successes
   to processed, then unconditionally writes "[]" to the queue file.
5. Auto-Trader ↔ Setup isolation violated.
   - server.py::api_trade_auto_reset_signals unlinks setup files and stops
     setup jobs.
   - server.py::api_trade_paper_clear stops setup jobs and unlinks
     setup_active_job_config.json (which nothing writes today).

Medium priority:
6. Market/Limit classification derived from wt_action, not computed from
   LTP vs trigger, and not locked per signal identity.
7. VALID gap check is abs(live_px - tl)/tl*100 — non-directional for Sell.
8. wt_action is rewritten via re.sub(r'\d+\.\d+', trigger_lvl, wt_action).
   This mutates WT Action (spec forbids it).
9. api_trade_paper_orders / api_trade_paper_positions look up
   SETUP_STATE by symbol only (not symbol + tf). Wrong values if the same
   symbol is allocated at two timeframes.
10. paper_trades.place_order is called without target / sl / opp_sig.
11. fetch_ltp updates SETUP_STATE[*].ltp but does not call
    sync_state_to_ledger, so paper_trades.json LTPs go stale.
12. No exit logic in the engine. cfg fields exit_opp, trailing_enabled,
    trailing_start_pct, trailing_step_pct, wt_filter_enabled,
    wt_buy_max, wt_sell_min, htf_filter_enabled, rsi_filter_enabled,
    rsi_period, allow_market_orders, write_to_sheet, show_all_signals
    are read but never used.
13. fno_mode == "all" applies the Indian-market F&O filter to Forex
    symbols, corrupting their bars.
14. Error / "No Data" rows are hidden in the monitor
    (if (r.status === 'No Data' || r.status === 'Error') continue;)
    instead of being displayed per spec §12.1.

Low priority / cleanup:
15. Duplicate @app.route("/api/trade/setup/status") decorator in server.py.
16. Duplicate jsonLiveQueue / jsonLiveProcessed DOM IDs.
17. Duplicate updatePanelValue JS function in trading_setup.html.
18. executeQueue button calls a missing JS function (dead button).
19. Show Both Signals / Allow Market Orders / Write GSheet UI toggles are
    inert.
20. stopAutoTrading JS function is defined but unused.
21. trigger_lvl, target_val, sl_val, opp_sig are stored as strings in
    SETUP_STATE while ltp / wt1 / wt2 are numbers.
22. stop_job doesn't remove the SETUP_JOBS entry.
23. Monitor missing WT1, WT2, WT_DIFF, RSI, valid OHLC, explicit Order Type
    columns.

### 7.8 Approved remediation plan (staged, one step per session)

Step 2 (NEXT) — Engine-only: trigger persistence + stable sig_id.
  File: web/trading_setup_engine.py only.
  A. Add SETUP_TRIGGER_FILE = DATA_DIR / "setup_trigger_state.json".
  B. On import: load TRIGGER_STATE from SETUP_TRIGGER_FILE if present.
  C. start_job: stop clearing TRIGGER_STATE (SETUP_STATE stays cleared).
  D. In the existing finally: block of _worker, write TRIGGER_STATE to
     SETUP_TRIGGER_FILE using a temp-file-then-rename pattern.
  E. In the most_recent block, set row["valid_time"] = sig["valid_time"];
     change sig_time = row.get("valid_time") (no df_real.index[-1]
     fallback); skip queueing (with a per-symbol log) if missing.
  Acceptance:
    - setup_trigger_state.json is created and survives restart.
    - start_job doesn't wipe trigger memory.
    - Reset Signals still clears TRIGGER_STATE and deletes
      setup_trigger_state.json.
    - Re-added symbol still starts with fresh trigger memory.
    - sig_id no longer uses df_real.index[-1] for identity.
    - No other file or route modified.

Step 3 — Queue role separation.
  order_queue.json = master only (always written first).
  New setup_order_queue.json = paper pending.
  Update server.py::api_trade_setup_json_files to expose both.
  _process_queue_file must keep failed items in the queue.
  Do not touch live queues in this step.

Step 4 — Market/Limit classification locked per signal identity
         (compute from LTP vs trigger, not from wt_action).

Step 5 — Paper metadata sync: pass target / sl / opp_sig to
         paper_trades.place_order; make fetch_ltp call
         sync_state_to_ledger; fix per-symbol-per-tf lookup in
         api_trade_paper_orders / api_trade_paper_positions.

Step 6 — Isolation fix in server.py: api_trade_auto_reset_signals and
         api_trade_paper_clear must not touch the setup engine.

Step 7 — Monitor columns reconciled with the scanner; error rows
         displayed; remove re.sub() mutation of wt_action; make the VALID
         gap directional for Sell.

Step 8 — Cleanup: duplicate route decorator, duplicate DOM IDs, duplicate
         updatePanelValue, dead executeQueue button, inert toggles,
         stop_job removing its job entry, string→number for trigger_lvl /
         target_val / sl_val / opp_sig.

Step 9 — Exit logic (target / SL / opposite / trailing) in the engine,
         driven by paper_trades fills and cfg toggles.

### 7.9 Trading Setup — Known Issues & Gotchas (new)

1. TRIGGER_STATE is a separate concept from the scanner's
   trigger_state.json. They are different files with different owners.
   Never merge them.
2. paper_trades.json is owned by paper_trades.py. Trading Setup may patch
   entries (via sync_state_to_ledger) but must not own the file.
3. The frontend polls /api/trade/setup/status every 5 seconds. Do NOT
   trigger a scan inside the status route. Scan triggers go through
   trigger_scan.
4. Toggling Paper Trade ON in the UI does NOT take effect for routing until
   update_config (Apply Settings) is clicked. This is a UX gap, not a bug.
5. The "Fetch LTP" button in the setup panel does not sync
   paper_trades.json (see gap #11). The Apply Settings button is the one
   that triggers sync_state_to_ledger.
6. If the same symbol is allocated on two timeframes, paper order /
   position display columns will show the wrong TF's target/SL until
   gap #9 is fixed. Do not rely on those columns for cross-TF symbols
   until then.
7. Execute Queue button in the UI is currently dead (gap #18). Use the
   engine's automatic per-scan execution instead.

---

## 8. Development Workflow (for all future sessions)

1. Attach only the files needed for the current step (see §7.8 for what
   each step requires). Do not attach unrelated files.
2. One step per session. Do not proceed to the next step until the current
   step is verified.
3. Provide exact FIND / REPLACE blocks with ~10 lines of surrounding
   context, OR a complete replacement file.
4. Do not assume a fix is applied because it was proposed. Base changes on
   the attached file only.
5. Do not touch read-only files (§7.1 rule 3).
6. If a fix requires a read-only file to change, stop and ask for
   explicit approval.
7. If the chat exceeds 80% of the context limit, warn immediately.

### Attachment plan by step

Step 2: PROJECT.md + web/trading_setup_engine.py
Step 3: + web/server.py
Step 4: + web/trading_setup_engine.py (re-attach with current content)
Step 5: + web/templates/trading_setup.html + signature block of
        web/paper_trades.py (place_order / list_orders / positions only)
Step 6: + web/server.py
Step 7: + web/templates/trading_setup.html + web/indicators/wavetrend.py
        (read-only, for column parity)
Step 8: + whichever file each cleanup touches
Step 9: + web/paper_trades.py (only the parts the engine needs to call)

---

## 9. Definition of Done — Trading Setup

Trading Setup is not complete until all of the following are verified:

Page & isolation
- /trade-setup loads and shows the current monitor + debug JSON.
- Existing /trading page and autotrader.py continue to work unchanged.
- Loading or refreshing /trade-setup does not start a scan or create
  orders.
- Start / Stop / Reset on Trading Setup does not affect Auto-Trader.
- api_trade_paper_clear and api_trade_auto_reset_signals do not touch
  Trading Setup state.

Allocation & scanning
- Saved symbols restore on reload.
- Adding / removing symbols while running changes the next scan.
- Removed symbols disappear from the monitor.
- Re-added symbols start with fresh trigger memory.
- Force Scan runs once without spawning duplicate workers.
- Monitor columns match the scanner output for the same symbol, tf, and
  settings.

Trigger & validation
- New Trigger appears only for a genuinely new signal identity.
- Unchanged signal values do not retrigger on every scan.
- Reset Signals clears trigger memory and monitor output without clearing
  allocations or paper / live records.
- WT Action matches Scanner / Backtest and is not mutated by VALID logic.
- Market-gap threshold is visible and applied directionally for Buy and
  Sell.
- Market / Limit classification is locked per signal identity.

Risk values & order queue
- Target, stop-loss, and opposite-signal values match backtest rules.
- Apply Settings immediately recalculates and displays target/SL/opp.
- Every eligible signal is written to order_queue.json first.
- Only New Trigger + actionable WT Action + VALID=YES is routed.
- Editing an order updates the master queue record without duplicating it.
- Open paper orders and filled paper positions receive updated risk
  values through backend reconciliation.

Paper / live routing
- Both switches off: master queue only; no execution.
- Paper on: route to paper queue, execute through paper_trades.py, record
  in setup_paper_processed.json.
- Live on: route to live queue only; live execution not implemented yet.
- Both on: independent copies, no cross-contamination.
- Repeated scans / restarts do not duplicate orders.
- Failed routing leaves a visible retryable / error record.

Error handling
- stop_job exists and start/stop endpoints work.
- Quantity is always calculated before order construction.
- API failures return JSON, not Flask HTML error pages.
- One bad symbol does not stop other symbols.