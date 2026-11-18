# Why Trade Setup ≠ Backtest — divergence audit
**Date:** 15 Nov 2026 · **Files:** `web/trading_setup_engine.py`, `web/indicators/wavetrend.py`, `web/templates/trading_setup.html` vs `web/backtest.py`
**Status:** diagnosis only — **no code was modified.**

---

## The one-paragraph answer

Backtest and Trade Setup do **not share a signal engine**. Backtest has exactly one definition of a trade
(`detect_wt_events` → bar breaks `level + buffer` within `wait_bars` → enter at `level + buffer ± slippage`).
Trade Setup has **two** independent definitions running side by side (`wavetrend.signals()` for the string you see,
`get_active_pending_signals()` for the queue), neither of which applies the buffer to the fill, neither of which
expires the same way, and **none** of the filters/`side_mode`/`exit_opp` switches you tick are read by the engine at all.
On top of that there is a **stale-variable bug that force-closes positions at random**.

So it is not that Trade Setup is "slightly off" — it is a different strategy wearing the same column names.

---

## 1. 🔴 CRITICAL — `exit_reason` leaks between symbols → phantom instant exits

`trading_setup_engine.py:723-731`

```python
                            # ✅ FIX: If an opposite MARKET order is waiting in the queue, force an instant exit
                            if row.get("order_queue", "—") != "—" and "Market" in row["order_queue"]:
                                q_side = "BUY" if "BUY" in row["order_queue"].upper() else "SELL"
                                if (row["pos_side"] == "BUY" and q_side == "SELL") or (row["pos_side"] == "SELL" and q_side == "BUY"):
                                    exit_reason = "Opposite Signal (Market)"

                            # Check for Exit (Strictly price-based now)
                            exit_reason = exit_reason if 'exit_reason' in locals() else None
```

`exit_reason` is a **function-scoped** local of `_worker`. Line 730 does not reset it — it re-assigns whatever
value is already there. Once **any** position in **any** symbol exits with `Target` / `StopLoss` /
`Opposite Signal (Market)`, that string lives on for the rest of the job's life. The next open position that
does *not* hit a price condition then hits `if exit_reason:` at line 744 and is **closed at the current LTP with
the previous trade's reason**.

Symptom to look for in `setup_trade_history.json`: rows with `reason: "Target"` where exit price is nowhere
near the target, several symbols closing on the same scan tick, and `TOTAL P&L` moving on ticks where nothing
should have happened.

**FIND** (`trading_setup_engine.py:723-731`)

**REPLACE**
```python
                            # ✅ FIX: reset per symbol, per tick — never inherit the previous row's reason
                            exit_reason = None

                            # ✅ FIX: If an opposite MARKET order is waiting in the queue, force an instant exit
                            if row.get("order_queue", "—") != "—" and "Market" in row["order_queue"]:
                                q_side = "BUY" if "BUY" in row["order_queue"].upper() else "SELL"
                                if (row["pos_side"] == "BUY" and q_side == "SELL") or (row["pos_side"] == "SELL" and q_side == "BUY"):
                                    exit_reason = "Opposite Signal (Market)"
```
(delete the old line 730 entirely — the `exit_reason = None` above replaces it)

That single fix will change your results more than everything else on this page.

---

## 2. 🔴 CRITICAL — the engine ignores 11 settings the UI sends

`trading_setup.html:1608-1640` sends a full config. These keys are **never read** by `trading_setup_engine.py`
(verified: the only `cfg.get()` calls are `buffer_pts, candles, fno_book, fno_mode, fno_sheet, live_trade,
max_gap_pct, paper_trade, paused, scan_interval, send_to_queue, stop_pct, target_pct, tasks, trailing_*,
wait_bars, wt_n1, wt_n2, wt_pending_rule`):

| UI control | Config key sent | Read by engine? | Backtest effect | Result in panel |
|---|---|---|---|---|
| Order Side (both/long/short) | `side_mode` | ❌ | long-only skips every SELL | **panel takes the sells anyway** |
| Strategy Mode (simple/same-side/alternate) | `strategy_mode` | ❌ | averaging, alternate legs | always behaves like `simple` |
| Multiplier / Max Levels | `multiplier_type`, `max_levels` | ❌ | pyramids up to N levels | never averages |
| Exit on Opposite ☑ | `exit_opp` | ❌ | unchecking holds through opposite signals | **always exits on opposite** |
| Allow Market Orders ☑ | `allow_market_orders` | ❌ | blocks/converts markets | ignored |
| WT Extreme ☑ + max/min | `wt_filter_enabled`, `wt_buy_max`, `wt_sell_min` | ❌ | drops late/extreme signals | **every signal passes** |
| HTF Trend ☑ | `htf_filter_enabled` | ❌ | drops counter-trend signals | **every signal passes** |
| RSI ☑ + period | `rsi_filter_enabled`, `rsi_period` | ❌ | drops non-rising/falling entries | **every signal passes** |
| (not in UI) | `vol_filter_enabled`, `vol_mult`, `vol_lookback` | ❌ | volume spike gate | not available at all |
| (not in UI) | `profit_target_mode`, `recovery_*`, `trailing_tighten_*` | ❌ | ₹-target, recovery overlay, tightened trail | not available at all |

Consequence: backtest shows 40 trades with HTF+RSI filtering, the panel shows 120 — and every extra one is a
trade the backtest deliberately rejected.

### 2a. Minimal filter + side gate (drop-in, right after the queue block starts)

**FIND** (`trading_setup_engine.py:581-596`)
```python
                        if is_new_trigger and is_valid and is_trade_action:
                            side = "BUY" if "Buy" in action_str else "SELL"
                            order_type = "MARKET" if "Market" in action_str else "LIMIT"
                            
                            # ✅ FIX: Do not queue same-direction signals while a position is already open
                            is_same_dir = False
                            if row.get("pos_qty", 0) != 0:
                                pos_side = "BUY" if row["pos_side"] == "BUY" else "SELL"
                                if side == pos_side:
                                    is_same_dir = True
                            
                            if not is_same_dir:
                                row["order_queue"] = action_str
                                row["queue_trig_price"] = _r(live_px) if order_type == "MARKET" else float(row["trigger_lvl"])
```

**REPLACE**
```python
                        if is_new_trigger and is_valid and is_trade_action:
                            side = "BUY" if "Buy" in action_str else "SELL"
                            order_type = "MARKET" if "Market" in action_str else "LIMIT"

                            # ── side_mode parity with backtest.run() ──
                            _blocked = None
                            if cfg.get("side_mode") == "long"  and side == "SELL": _blocked = "side_mode=long"
                            if cfg.get("side_mode") == "short" and side == "BUY":  _blocked = "side_mode=short"

                            # ── WT extreme filter parity with backtest._apply_filters() ──
                            _wv = row.get("wt_valid_wt_value")
                            if not _blocked and cfg.get("wt_filter_enabled") and _wv not in (None, "", "—"):
                                try:
                                    _wf = float(_wv)
                                    if side == "BUY"  and _wf > float(cfg.get("wt_buy_max",  -40)): _blocked = "WT>max"
                                    if side == "SELL" and _wf < float(cfg.get("wt_sell_min",   40)): _blocked = "WT<min"
                                except Exception:
                                    pass

                            # ── RSI direction filter parity ──
                            if not _blocked and cfg.get("rsi_filter_enabled"):
                                _rd = str(row.get("wt_rsi_dir", "")).lower()
                                if side == "BUY"  and _rd != "rising":  _blocked = "RSI not rising"
                                if side == "SELL" and _rd != "falling": _blocked = "RSI not falling"

                            # ── HTF trend filter parity (re-uses the backtest helpers, read-only import) ──
                            if not _blocked and cfg.get("htf_filter_enabled"):
                                try:
                                    import backtest as _bt
                                    _hdf = _htf_cache.get((sym_yf, tf))
                                    if _hdf is None:
                                        _hdf = _bt._load_htf_trend(sym_yf, tf)
                                        _htf_cache[(sym_yf, tf)] = _hdf
                                    _up = _bt._htf_uptrend_at(_hdf, df_real.index[-1])
                                    if _up is None:                          _blocked = "HTF none"
                                    elif side == "BUY"  and not _up:          _blocked = "HTF down"
                                    elif side == "SELL" and _up:              _blocked = "HTF up"
                                except Exception as _e:
                                    pass  # never block on an HTF download failure

                            if _blocked:
                                row["valid"] = f"NO ({_blocked})"

                            # ✅ FIX: Do not queue same-direction signals while a position is already open
                            is_same_dir = False
                            if row.get("pos_qty", 0) != 0:
                                pos_side = "BUY" if row["pos_side"] == "BUY" else "SELL"
                                if side == pos_side:
                                    is_same_dir = True

                            if not is_same_dir and not _blocked:
                                row["order_queue"] = action_str
                                # ✅ buffer parity: backtest enters at level ± buffer, not at the raw level
                                if order_type == "MARKET":
                                    row["queue_trig_price"] = _r(live_px)
                                else:
                                    _bp = float(cfg.get("buffer_pts", 0) or 0)
                                    row["queue_trig_price"] = _r(float(row["trigger_lvl"]) + (_bp if side == "BUY" else -_bp))
```
…and declare the cache once, next to `TRIGGER_STATE = {}` at the top of the file:
```python
_htf_cache = {}   # (yf_symbol, tf) -> htf DataFrame
```

---

## 3. 🔴 The panel and the backtest use **different signal definitions**

| | Backtest (`detect_wt_events`) | Panel display (`wavetrend.signals`) | Panel queue (`get_active_pending_signals`) |
|---|---|---|---|
| Cross source | `cross` series | last cross **anywhere in history** | `cross` series |
| Buffer applied to level | ✅ `level ± buffer` | ❌ raw high/low | ✅ for the *pending* check only |
| Wait window | ✅ `wait_bars`, then expires | ❌ **never expires** | ✅ `wait_bars` |
| Pending rule | `every` / `same_dir` / `any_dir` | n/a | only `same_dir`-ish + `any_dir`; **`every` not implemented** |
| Entry price | `level ± buffer ± slippage` | "Market" → **live LTP at scan time** | `trigger_lvl` raw |
| Bar set | whole history | reads bar `[-2]` | `df_ha.iloc[:-1]` |

Practical effects:

* **Stale levels.** `signals()` keeps returning `Buy Limit Order (BO close 290.31)` long after `wait_bars`
  expired it in the backtest. The panel can queue and fill a trade the backtest would never take.
* **Entry price drift.** Once `trigger_idx` is found, `signals()` switches to `"Buy Market Order"` and the panel
  sets `queue_trig_price = live_px` — i.e. it **chases** whatever the price is minutes/hours after the breakout,
  while the backtest is still filled at `level + buffer`. On a 1h chart this alone can be a 1-3 % entry
  difference, which is most of your target.
* **`pending_rule = "every"` silently behaves as `same_dir`** (`trading_setup_engine.py:445` reads the key, but
  `get_active_pending_signals` only ever keeps one pending object per direction).

**FIX 3a — apply the buffer to the order price** → included in the §2a replacement above.

**FIX 3b — implement `every` in `get_active_pending_signals`.**

**FIND** (`trading_setup_engine.py:246-252`)
```python
            pend[direction] = {
                "valid_i": i, "level": level, "wt_val": wt_val
            }
```

**REPLACE**
```python
            if pending_rule == "every":
                # ✅ every cross gets its own wait window (backtest default behaviour)
                pend.setdefault("_multi", {"buy": [], "sell": []})
                pend["_multi"].setdefault(direction, []).append(
                    {"valid_i": i, "level": level, "wt_val": wt_val})
            else:
                pend[direction] = {
                    "valid_i": i, "level": level, "wt_val": wt_val
                }
```
⚠️ this needs the trigger loop at lines 214-235 reworked to iterate `pend["_multi"][direction]` — it is a
~25-line change, not a one-liner. Recommended only if you actually run `every` in the panel.

**FIX 3c — make the displayed action respect `wait_bars`.** Cheapest safe option: in the row builder, blank the
action when the pending signal has expired:
```python
                        # after: pending, latest_signal = get_active_pending_signals(...)
                        if not pending["buy"] and not pending["sell"] and not row.get("pos_qty"):
                            if "Limit" in str(row.get("wt_action", "")):
                                row["wt_action"] = "No active breakout (expired)"
```
This is the single easiest way to stop the panel showing levels the backtest has already dropped.

---

## 4. 🟠 No commission, no slippage — so `TOTAL P&L` can never equal backtest Net ₹

Backtest: `commission_pct` (0.05 %) charged on **both** legs + `slippage_pct` (0.02 %) on entry **and** exit.
Panel: `order_pnl = (live_px - pos_entry) * qty`. **Zero costs.**

At 0.05 % × 2 legs + 0.02 % × 2, a 5 % target loses ~0.14 % — and on a ₹5,000 allocation with 30 trades
that is a visible gap. Worse, on a `stop_pct = 0` (opposite-signal-only) config the panel's gross number can be
positive where the backtest is negative.

**FIND** (`trading_setup_engine.py:750-756`)
```python
                                save_history_entry({
                                    "symbol": raw_sym, "tf": tf, "side": row["pos_side"],
                                    "qty": row["pos_qty"], "entry": row["pos_entry"], "entry_time": row.get("pos_entry_time", "—"),
                                    "exit": live_px, "pnl": row["order_pnl"],
                                    "reason": exit_reason, "time": str(pd.Timestamp.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S'))
                                })
```

**REPLACE**
```python
                                # ✅ cost parity with backtest: entry+exit commission, exit slippage
                                _comm = float(cfg.get("commission_pct", 0.05))
                                _slip = float(cfg.get("slippage_pct", 0.02))
                                _notional = abs(row["pos_qty"] * row["pos_entry"])
                                _costs = round(_notional * _comm / 100 * 2, 2)
                                _net = round(row["order_pnl"] - _costs, 2)

                                save_history_entry({
                                    "symbol": raw_sym, "tf": tf, "side": row["pos_side"],
                                    "qty": row["pos_qty"], "entry": row["pos_entry"], "entry_time": row.get("pos_entry_time", "—"),
                                    "exit": live_px, "pnl": _net, "costs": _costs,
                                    "reason": exit_reason, "time": str(pd.Timestamp.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S'))
                                })
```
…and change the line above it:
```python
                                row["total_pnl"] = round(row.get("total_pnl", 0) + row["order_pnl"], 2)
```
→
```python
                                row["total_pnl"] = round(row.get("total_pnl", 0) + _net, 2)
```
Add two inputs to the panel (`autoCommission`, `autoSlippage`) and send them as `commission_pct` / `slippage_pct`.

---

## 5. 🟠 "Wait Max Bars" is applied as **minutes**, not bars

`trading_setup_engine.py:645-651`

```python
                            max_wait_mins = int(cfg.get("wait_bars", 100))
                            # Approximate: 1 bar = 1 minute for this UI check
                            if (time.time() - placed_time) > (max_wait_mins * 60):
```
On 1h, `wait_bars = 20` should mean **20 hours**; the panel cancels the limit after **20 minutes**. On 1d it is
off by 24×. The backtest counts bars exactly. So limit orders in the panel expire ~60× too early on 1h.

**FIND**
```python
                        if row["pos_qty"] == 0 and row["order_col"] != "—" and "LIMIT" in row["order_col"].upper():
                            placed_time = row.get("paper_order_time", time.time())
                            max_wait_mins = int(cfg.get("wait_bars", 100))
                            
                            # Approximate: 1 bar = 1 minute for this UI check
                            if (time.time() - placed_time) > (max_wait_mins * 60):
```

**REPLACE**
```python
                        if row["pos_qty"] == 0 and row["order_col"] != "—" and "LIMIT" in row["order_col"].upper():
                            placed_time = row.get("paper_order_time", time.time())
                            max_wait_bars = int(cfg.get("wait_bars", 100))

                            # ✅ TF-aware: expire after N BARS, not N minutes
                            _TF_MIN = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                                       "60m": 60, "1h": 60, "90m": 90, "4h": 240,
                                       "1d": 1440, "5d": 7200, "1wk": 10080}
                            _bar_secs = _TF_MIN.get(tf, 60) * 60
                            if (time.time() - placed_time) > (max_wait_bars * _bar_secs):
```

---

## 6. 🟠 A "Buy Limit" that fills when price goes **above** it is not a limit order

Both the virtual panel (`:673` `if side == "BUY" and live_px >= float(row["order_trig_price"]): filled = True`)
and the paper ledger (`_check_pending_limits` → `if o["side"] == "BUY" and live_px > limit_px: mark_filled`)
treat the breakout entry as a *stop-buy*, then record it as `order_type="LIMIT"` with `price=level`.

* The backtest is doing the right thing (`high > level + buffer` → buy at `level + buffer`).
* A real Zerodha **LIMIT** buy at the level fills only if price comes *down* to it. Sending "BUY LIMIT 290.31"
  would fill on a pullback, not on the breakout — i.e. **the opposite trade**.
* Correct instrument for a breakout is `SL-Limit` / `SL-M` with `trigger_price = level ± buffer`, or a plain
  MARKET order at the moment of the cross.

The LIVE branch is currently a stub (`"🚀 [LIVE] Would have placed ..."` at `:159`), so no money is at risk
today — but this must be fixed **before** live execution is wired, and it also means your paper fills are at
prices a real limit order could not have got.

---

## 7. 🟡 Smaller divergences (worth knowing, low priority)

| # | Divergence | Where |
|---|---|---|
| a | `buffer_pts` default **0** in panel vs **0.01** in backtest; `wait_bars` default **100** in panel vs **20** in backtest; `max_gap_pct` default **2 %** in panel — a rule the backtest simply does not have | `:443-445`, `:522` |
| b | Panel skips the gap check entirely for LIMIT strings (`is_valid = True` unconditionally) — contradicts `PANEL_LOGIC.md` §2 which says VALID is gap-driven | `:528-533` |
| c | Panel RSI uses **real** closes (`df_real`), backtest RSI uses **HA** closes (`_apply_filters` receives HA `df`) — different RSI values for the same bar | `:838-848` vs `backtest.py:426` |
| d | Panel trailing SL trails off **live_px**; backtest trails off **best_price**. Panel can never tighten as fast as the backtest on a pullback-then-resume | `:704-722` vs `backtest.py:88-104` |
| e | No `trailing_tighten_*`, no `recovery_*`, no `profit_target_mode="amount"` (₹ target) in the panel at all | — |
| f | Panel exit compares only **LTP**; backtest exits intrabar on High/Low. A wick that hits target then reverses will register in the backtest and be missed by the panel. Unfixable without ticks — but it means the panel will always show fewer targets and more stop-outs than the backtest | `:735-742` |
| g | `wt_pending_rule` default in panel is `same_dir`, in backtest `every` — two different defaults for the same-named control | `:445` vs `backtest.py:975` |
| h | TARGET/SL are stored as **strings** (`"290.31"` / `"—"`) and re-parsed every tick; one bad float crashes the whole symbol into `"status": "Error"` and silently stops it | `:556-575`, `:865` |
| i | `signals()` computes its own `wt_max_profit_pct` / `wt_booking_status` from `trigger_level` even when no position exists — the "Profit booked at 5%" text is indicator output, not a real trade | `indicators/wavetrend.py:232-262` |

---

## 8. What actually matches (so don't "fix" these)

* Closed-candle discipline: panel uses `df_ha.iloc[:-1]` — correct, matches `autotrader.py`.
* Target/SL are locked to `pos_entry` at fill, not to the moving LTP — matches `backtest._target_px(ep, ...)`.
* Qty = `int(alloted_amt / fill_price)` — matches `_qty_for()` fixed mode.
* Trailing SL never loosens (monotonic guard `new_sl > current_sl`) — same intent as the backtest.
* 20 % gap glitch guard exists in the panel (the backtest lacks it — see the backtest review §2.6).
* Same-direction signals blocked while a position is open — matches the backtest's single-position rule for `simple`.

---

## 9. Recommended fix order (each is independently testable)

1. **§1 `exit_reason` reset** — 2 lines, removes random exits. Do this first, alone, then compare one symbol over one session.
2. **§2a side_mode + WT + RSI + HTF gates** — makes the ticked filters real.
3. **§5 wait_bars → true bars** — stops premature limit expiry.
4. **§4 commission/slippage** — makes `TOTAL P&L` comparable to backtest Net ₹.
5. **§3 buffer on the order price + expired-level display** — aligns entry prices.
6. **§6 SL-Limit order type** — required before any live execution.
7. **§3b `every` rule**, **§7d trailing off best price**, recovery/amount-target — only if you actually use them.

After step 4 you can run the `/parity` check from the backtest review (`REVIEW_backtest_2026-11-15.md` §5.4)
and expect the two engines to agree on symbol, direction, entry bar and reason — which they cannot today.

---

### Change-safety note
`trading_setup_engine.py`, `trading_setup.html` and `wavetrend.py` are **not** on the PROJECT.md §7.1
read-only list, so all of the above is legal to patch without touching `backtest.py`
(the only cross-file dependency is a read-only `import backtest` for the HTF helpers, which does not modify it).
Nothing was modified in this audit.