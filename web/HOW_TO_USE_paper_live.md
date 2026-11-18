# How to use **Paper Live** — the backtest logic running on live prices

This is the page you actually asked for: it polls the market, spots the same WaveTrend
breakout entries your Backtest page trades, and manages the position with the same
target / stop / trailing / opposite-signal rules — **in paper, no broker**.

You have **2 new files** (nothing else was changed):

| File | Must stay in |
|---|---|
| `paper_live_engine.py` | `web/` |
| `paper_live.html` | `web/templates/` |

```
mahadev core output/
└── web/
    ├── paper_live_engine.py            ← new
    ├── templates/
    │   └── paper_live.html             ← new
    ├── server.py                       ← NOT touched
    └── ... everything else unchanged
```

It writes only its own two files when running:

```
web/data/paper_live_state.json     open positions
web/data/paper_live_trades.json    closed trades
```

It never touches `paper_trades.json`, `order_queue.json`, `SETUP_STATE`, or anything
belonging to the Auto-Trader / Trade Setup pages.

---

## PART 1 — Start it (nothing to edit)

### Step 1
Open your terminal, go to the `web` folder:

```
cd "path/to/mahadev core output/web"
```

### Step 2 — activate your venv (if you normally do before `python server.py`)

### Step 3 — start

```
python paper_live_engine.py
```
(`python3` if `python` is not found)

You should see:
```
Paper Live (standalone) -> http://localhost:8020/paper-live
 * Running on http://127.0.0.1:8020
```

Leave the window open. Your normal app on port 8000 keeps working — this is a separate port.

### Step 4 — open the page

```
http://localhost:8020/paper-live
```

---

### Step 5 — fill the left panel exactly like your Backtest left panel

Copy the settings that gave you profit in the backtest:

| Panel section | What to do |
|---|---|
| **Market** | symbols: quick-add or **Google Sheet** → Load; per-row ₹ + 🔒 lock + equal split (see below); Exchange **NSE**, Timeframe **15m**, Lookback **1mo** |
| **WaveTrend** | n1 / n2 / Pending rule / Buffer pts / Wait bars / Heikin Ashi — same as backtest |
| **F&O 15:15 filter** | `All symbols` / `From sheet` / `Off` — **set exactly what your backtest uses** (backtest defaults to *All symbols*). Book + tab only matter for "From sheet" |
| **Entry / Exit** | Target mode (`% of entry` / `₹ amount`), Target, Stop loss %, Exit on opposite, Trailing SL, **Tighten near target** — same as backtest |
| **Filters** | tick the same filters you use in the backtest |
| **Sizing & costs** | **Size by** → `Fixed ₹ per trade` / `Fixed quantity` / `% of capital`, then the matching box + Capital, Commission %, Slippage % — same as backtest |

> Yahoo intraday limits: **5m/15m** work best with `1mo` (max 60d). **1h** works with `3mo`–`1y`.
> If the page says "No Data", lower the lookback.

### The F&O 15:15 filter (important)

Your Backtest page defaults to **All symbols**, which drops every candle that starts at
**15:15 IST or later** on 5m / 15m / 30m / 1h. The last valid candle of the Indian session
becomes **15:00**, so no signal is ever taken from the fake last-hour candles.

Paper Live now does exactly the same, at the same point in the pipeline
(F&O filter → volume → Heikin Ashi → WaveTrend), and the row shows a small
**F&O 15:15** mark when it is active.

* `All symbols` — applies to every symbol (this includes US/forex symbols, same quirk as the
  Backtest page).
* `From sheet` — only symbols listed in your F&O book/tab. The list is cached for 10 minutes;
  if the sheet cannot be read you will see `F&O sheet: …` in the monitor bar.
* `Off` — raw candles (equivalent to F&O = Off in the backtest).
* On **1d / 1wk** the filter is a no-op, exactly like the backtest.

Verified: with F&O on, the 15m replay dropped 30 candles and both engines still produced
**identical trades** (77/77, net −533.22 each). With F&O off: 78/78. 🙂
If your backtest used `All symbols`, running Paper Live with `Off` will give you **different
signals** — that is the whole point of this setting.

### Money: ₹ per trade vs Capital — which one sizes the trade?

This is the question everyone asks, so here it is plainly. The **Size by** dropdown
decides everything, and it is the same three options your Backtest page has:

| Size by | What decides the quantity | What **Capital ₹** does |
|---|---|---|
| **Fixed ₹ per trade** (default) | qty = ⌊ **Amount ₹ / trade** ÷ fill price ⌋ | only the account balance — it must be ≥ the trade cost, otherwise the trade is skipped |
| **Fixed quantity** | qty = **Quantity** box, exactly as typed | only the account balance check |
| **% of capital** | qty = ⌊ **Capital × Position %** ÷ 100 ÷ fill price ⌋ | it **is** the sizing input — this is the mode where Capital drives the trade |

So with the default (`Fixed ₹ per trade`, ₹5 000, Capital ₹1 00 000):

* a ₹328.25 stock → ⌊5000 ÷ 328.25⌋ = **15 shares** (₹1 00 000 would only buy 304 shares in *% of capital* mode);
* Capital ₹1 00 000 is *not* divided by the price — it is just the wallet that must cover 15 × ₹328.25;
* raising Capital to ₹5 00 000 changes **nothing** for sizing; raising *Amount ₹ / trade* to ₹25 000 gives 5× the shares.

Only the box for the selected mode is shown, and a blue banner inside the card says it in plain words:

```
Mode Fixed ₹ per trade · ignored: Quantity and Position %
USING: ₹5,000 per trade — qty = ⌊₹5,000 ÷ price⌋.
Capital ₹1,00,000 is only the cash balance (the trade must fit inside it), it does not set the quantity.
Example from your feed: HAL.NS @ 328.25 → 15 shares (₹4,924)
```

The **Mode … ignored: …** line tells you which two boxes are dead, and the *Example from your feed*
line shows the real share count for your first symbol's live price. The **sizing** chip in the
Live monitor bar repeats it, and each open position shows `size: …` underneath it.

> **Same rule on the Backtest page, but it never says it.** There the three radio buttons decide and
> all three boxes stay visible — so if `% of capital` is ticked, your `Fixed ₹ amount` of 5 000 is
> **ignored** and the quantity comes from `Capital × Position %`. If `Fixed ₹ per trade` is ticked,
> `Quantity` and `Position %` are ignored. Only one of the three is ever used.

### The **Market** picker — India, US, Forex or Crypto

The first box in the **Market** card decides two things: the **suffix** added to every ticker you
type, and **which clock** the engine uses to decide whether the exchange is open.

| Market | You type | Engine uses | Session |
|---|---|---|---|
| **India (NSE/BSE)** | `HAL`, `RELIANCE` | `HAL.NS` (or `.BO` from the exchange box) | 09:15–15:30 IST, Mon–Fri |
| **US (NASDAQ/NYSE)** | `AAPL`, `MSFT` | no suffix at all | 09:30–16:00 New York, Mon–Fri |
| **Forex / Commodity** | `EURUSD`, `XAUUSD` | `EURUSD=X` | Sun 22:00 – Fri 22:00 UTC |
| **Crypto** | `BTC`, `ETH` | `BTC-USD` | 24 × 7 |

A ticker that already carries a suffix (`HAL.NS`, `EURUSD=X`, `BTC-USD`) is left alone — the picker
never doubles it. Changing the picker only affects **rows you add afterwards**; rows already in the
table keep the suffix they were created with, so nothing silently rewrites your list.

**This is the fix for "market closed, nothing scans":** the old engine asked the NSE clock for every
symbol, so a US or forex list sat idle for its whole session. Now the clock follows the market you
picked.

### Working after hours — the `Poll when closed` tick

☑ **Poll when closed** (in Sizing & Costs) is **on by default**. It means:

* the engine keeps scanning even when the session is shut — after hours it slows to **every 5 minutes**
  so Yahoo is not hammered, and the strip says `India (NSE/BSE) closed — still scanning`;
* you can watch signals, levels and pending breaks at any hour (this is how you test at 22:00);
* **entries still need a fresh bar** — a stale candle never fills a position unless you also tick
  *Allow stale entries*. So nothing "buys" on yesterday's close at midnight.

Untick it and the strip reads `closed — idle (tick "Poll when closed")`: the loop sleeps until you
press **Scan now** or **✔ Apply settings**.

> US and forex amounts are still entered in ₹ in the symbol table — this is paper money and the
> engine only needs a number to size against. Don't mix an NSE row and an AAPL row in one table:
> the market picker is per run, not per row.

### The symbol list — manual, Google Sheet, 🔒 lock, equal split (like Trade Setup)

The **Market** card now works the way your Trade Setup page does:

**1. Adding symbols**
* Type in **Quick add** (`HAL, RELIANCE, TATAMOTORS` or one per line) and press **＋ Add** — or Enter.
  Duplicates are ignored, and the exchange suffix is added from **Default exchange**.
* Or set **Symbol source = Google Sheet**, then: pick the **Book** — all four books are listed in the
  dropdown (`paper trading`, `Backtest scanner`, `Stock Price Scraper`, `scanner output`) and you can
  type any other book name in the box underneath → press **⟳ Tabs** → choose the **Tab** → press **⬇ Load symbols**.
  The exchange is detected from the tab name exactly like Trade Setup (`us…` → US, `…bse…` → BSE,
  anything else → NSE), and the symbols are **appended** (existing rows are kept).
  This needs `gspread` + your `credentials.json`, same as Trade Setup; if the sheet is not shared
  with the service account you get the reason in the small grey line under the buttons.

**2. The rows**

| Column | Meaning |
|---|---|
| Symbol | `HAL.NS` — one row per symbol |
| ₹ box | ₹ per trade **for that symbol** (or its capital, in `% of capital` mode) |
| 🔒 / 🔓 | **lock** = keep this row's ₹ even when the list changes; 🔓 = let it be re-split |
| ✕ | remove the row |

**3. Allocation — the "auto distribution"**
* **Equal distribution** (default): the **Total to split ₹** is divided over the rows.
  Locked rows keep their number; the rest share what is left; the last row takes the rounding
  remainder so the sum is exactly your total. Example: total ₹10 000 over 3 rows → **3 334 / 3 333 / 3 333**.
* **Manual override**: type any ₹ per row (typing switches the dropdown to Manual for you).
  The hint line shows `rows add up to ₹X of ₹Y`, in **red** if you have gone over the total.
* In **`% of capital`** mode the split applies to **capital**, not the ₹ amount: each symbol gets
  `total ÷ rows` of capital and `Position %` applies to that symbol's own share.
* In **`Fixed quantity`** mode money is irrelevant and the rows show 0 — the Quantity box rules.
* **🔒 Lock all / 🔓 Unlock all / 🗑 Remove unlocked** work exactly like the Trade Setup buttons.

**4. Save the set to a file (Save / Load)**
Under the table: type a name → **💾 Save**. The list is written to
`data/paper_live_presets.json` on the server, so it survives restarts and you can keep several:
`WT 15m 20 stocks`, `F&O NSE`, … Pick one from **— saved sets —** → **⬇ Load** (it restores the rows,
their ₹, the locks, the allocation mode and total), and **✕** deletes it. Loading while the engine is
running applies the new list on the next cycle, exactly like editing rows.

**5. Where it shows up**
* Live monitor: each position prints `size: ₹7,000 per trade` (or `50% of capital = ₹1,00,000 per trade`)
  — the engine's own number, not a guess.
* **🧪 Verify vs backtest** uses that symbol's row amount, so the proof tests what you will actually run.

### How a live entry fires — level vs market (the important new bit)

Your manual Zerodha habit was: *"last closed candle gave a buy signal at 500. LTP is 495 → put a
limit at 500. LTP is already above 500 → send a market order."* The page now models exactly that,
and lets you choose which half you want:

**`Entry fill` dropdown (Entry / Exit card):**

| Setting | What the fill is | When to use |
|---|---|---|
| **At breakout level (stop order)** — default | the level (500) + slippage, whatever the LTP is when the scan sees it | you keep a resting **SL / stop-limit buy at the level** with the broker. This is what the backtest assumes, so P&L matches it |
| **Market if already past level** | 500 if price hasn't crossed, **LTP** if it already has | you trade off the scan by hand and press BUY the moment you see "Buy Triggered" |

Real numbers from the test run (RELIANCE, level 328.18, LTP 333.85 when the scan fired):

| Mode | Entry | P&L right after entry |
|---|---|---|
| At breakout level | **328.25** | **+56.05** |
| Market if already past | **333.92** | **−0.67** |

Same signal, same size — the chasing mode hands back the entire breakout headroom. That is the
whole reason the backtest cannot be reproduced by hand with market orders.

**The entry no longer waits for the candle to close.** As soon as the live price crosses
`level ± buffer`, the trade opens (the Scanner page's "Buy Triggered @ …" moment), and the fill is
the level — exactly what a resting stop order would give you. It only needs one scan to *notice* the
cross instead of a whole candle, so a 90-second poll is fine: with the resting order at the broker,
even a missed poll fills at the level.

### Is it actually running? (the status strip)

Above the monitor there is now one big line that answers that in a glance:

| Strip | Meaning |
|---|---|
| ● **STOPPED** (grey) | nothing is polling. It also shows the last scan time so you know how stale the table is |
| ● **RUNNING** (green, pulsing) | `polling every 4 min · last scan 11:38:59 (1.4s, 2 symbols) · next in 37s` |
| ⟳ **SCANNING…** (amber, spinning) | a scan is happening right now (`manual scan` or `poll cycle`) |

The bar under it also has **last scan** (time · duration · how many symbols), and **next scan in Ns**
disappears into `⟳ scanning N symbols…` while a scan runs. **Scan now** shows the same spinner and
then reports `scan done · 3 symbols in 1.9s` at the bottom-left, so you can tell it really ran.

### Stop is immediate now

Press **■ Stop** and the strip turns amber **STOPPING…** with `scan is aborting at the next symbol`,
then flips to **STOPPED** within about a second. The engine checks for the stop request between
symbols, so a 119-symbol scan no longer keeps running to the end. Measured here: 23 symbols,
stop pressed mid-scan → `running:false` in **1.1 s**, `aborted:true`.

### Changing anything while it is already running — press **✔ Apply settings**

Nothing you touch reaches the running engine by itself — not the left panel, not the symbol table (this
changed in build v8: adding a symbol while running used to be picked up automatically, which is unsafe
now that the 🔒 rows decide what runs). You can type a new target, add two symbols, unlock one, change
the buffer — the engine keeps running exactly as it was started until **you** say so.

1. change whatever you want in the left panel (target %, capital, buffer, filters, symbols…),
2. a red dot appears next to **✔ Apply settings** with *"changed — press Apply"*,
3. press **✔ Apply settings**.

**It also reaches the engine immediately.** Earlier a sleeping engine waited out its whole poll interval
before noticing — at `poll 15 min` your change could sit for 15 minutes. Now Apply interrupts the wait
and the next scan starts within a second (measured: 1 scan → 2 scans in under 3 s after Apply).

What happens then:

| Change | Effect on the running engine |
|---|---|
| profit target / SL / trailing / buffer / filters / sizing | next scan uses the new value |
| symbol **added** | picked up on the very next cycle: *"engine updated · added TATAMOTORS.NS — scanned next cycle"* |
| symbol **unlocked** (with another row still 🔒) | treated as removed: it stops being scanned (kept if it has an open position) |
| symbol **deleted, no open position** | dropped immediately |
| symbol **deleted, but it has an open position** | **kept** until the position closes (the engine must manage the exit) and reported as `kept` |
| 💾 saved allocations rows (₹ per symbol, locks) | re-applied to the running engine as well |
| **profit target % / stop loss % while a position is open** | that position is **re-priced** — the Target and SL columns and the very next exit check use the new numbers, and the message says `re-priced 1 open position` |
| **timeframe / poll minutes** | still fixed for the lifetime of a run — Stop → Start to change those |

If the engine is **stopped**, **✔ Apply settings** just saves the panel (message: *"not running —
saved only"*), so pressing Start later starts with exactly what you see.

### 💾 Save allocations — the set you want to come back to

Same idea as **Save Allocations** on the Trade Setup page, for this page's symbol table.

1. build your table — 3 symbols, ₹ amounts, locks, equal or manual split,
2. press **💾 Save allocations**.

It writes the set (symbol, exchange, ₹ amount, capital, lock, split mode, total) to
`data/paper_live_allocations.json` on the server — not just in this browser. The line under the buttons
answers `saved 1 locked of 3 symbols at 13:31:08`.

**The 🔒 rows ARE the working set** — the same rows are saved, the same rows are scanned. Three symbols
with one 🔒 means one symbol is saved and one symbol runs; the other two are just sitting in the table
until you lock them.

| Situation | What is saved | What the engine scans |
|---|---|---|
| 1 of 3 locked | **that 1 row only** | **only that symbol** — saving also pushes the set into a running engine |
| all 3 locked | all 3 rows | all 3 |
| **nothing locked** | **nothing** — the button refuses: `🔒 lock the rows you want to save first …` | the whole table (so the page still works if you never lock) |

The line under the table always tells you what will run before you press anything:

* `▶ engine scans 1 of 3 — only the 🔒 rows · 2 unlocked row(s) are ignored`
* `▶ engine scans all 3 (nothing is 🔒 — lock rows to narrow it)`

Want the whole table as the working set? Press **🔒 Lock all** (one click), then **💾 Save allocations**.

When the set shrinks (3 → 1), the dropped symbols **stop appearing in the monitor** — their old rows would
otherwise sit there with frozen prices. The strip shows `hidden 2 old symbols` instead; that counter counts
symbols from an older run that this run is not scanning and that hold no position. **Reset positions &
history** clears them for good. (A dropped symbol that still has an open position stays on screen until it
exits — it must be managed to the end.)

Why it matters: start with 3 symbols today, tomorrow open the page and you get those 3 back with the
same money; then add 2, delete 1, press **💾 Save allocations** again and *that* 6 → 5 set is what the
page (and any other browser) loads next time. Order of what the page loads when you open it:

1. the allocation saved on the server (this button),
2. else the last table in this browser,
3. else the default `HAL, RELIANCE`.

### Fold the big blocks — click the heading

Click the title of **Live Monitor**, **Paper Trades**, **Engine-check** (or any other block on the
page) and that block folds down to its heading; click again and it opens. A small `▾` on the right
turns to `▸` when a block is closed. The page remembers which blocks you closed, so the next visit
opens the same way (stored in this browser, `paper_live_folds`). Nothing about the engine changes —
a folded monitor still updates behind the heading.

### Why a price can look "wrong" vs the broker (Yahoo tag under LTP)

The price in the **LTP** column is a **Yahoo Finance 1-minute close**, not a Zerodha tick: NSE data on
Yahoo normally runs **1–15 minutes behind** the broker. So the monitor can legitimately show a price and
a `Diff` that do not match what you see in Kite at that second — and the WT Action / Diff are computed
from that Yahoo price.

From this build the LTP cell says where the number came from, so you never have to guess:

| Under the LTP | Meaning |
|---|---|
| `live` | a fresh 1-minute Yahoo close, age unknown |
| `live · 2m old` | that 1-minute bar is 2 minutes old — normal |
| `live · 15m old` (amber) | the feed is lagging; the row's Diff / WT Action are that old |
| `live · stale ⚠` (amber) | older than 2 hours — the feed is broken for that symbol |
| `candle close ⚠` (amber) | Yahoo had **no** live price for that symbol, so the engine fell back to the last candle close — treat the Diff as approximate |

The strip above the table adds a summary chip: `feed 4 live · 1 candle · 12m`. If that chip is amber,
some rows are not on live prices.

What to do when prices look old: press **⟳ Scan now** (forces a fresh download). If it stays `candle
close` or `stale`, Yahoo has no intraday data for that symbol — nothing to fix on this side. Zerodha
tick data is not used anywhere on this page (paper only), so a 10-minute lag affects *when* a paper trade
fills, not whether the rule is right.

### Why a trade happened — the line under **Position**

Every open position now says where it came from, in small grey text under the position size:

* `live break of 475.00 (cross 14:05)` — the live price crossed a signal's barrier, filled at the barrier
* `live break of 475.00 (cross 14:05) · armed before this run` — same, but that signal was already waiting
  **before you pressed Start** (see the next section — this is the one case where the live engine and the
  backtest can legitimately open a different first trade)
* `closed bar 14:25 broke 475.10` — the newest closed candle broke the barrier by itself, so the engine
  took it on that tick

The same text is stored on the closed trade (`entry_why`), so history explains itself.

### What the live engine does vs the backtest — measured, not guessed

I walked 300 five-minute bars through the live loop one poll at a time (each bar becoming the "current"
one) on the same candles and settings, then compared the trades with `backtest.run()`:

| | result |
|---|---|
| direction (long/short) | **25 of 25 identical** |
| entry price | **24 of 25 identical** |
| exit price | **24 of 25 identical** |

The two differences are structural, not bugs:

1. **The first trade after a mid-session Start.** The engine starts flat while the market may already have
   live signals armed; it can take the oldest one still waiting (the backtest "took" it hours earlier).
   That trade is now labelled `· armed before this run`. Every trade after it matched.
2. **Stops/targets inside a candle.** The backtest sees the bar's High/Low, so it can be stopped out
   *inside* a bar. Live only sees the price at each poll — if the stop is only touched between polls you
   see the next opposite signal instead. That is why the engine has the `feed` / `live · Nm old` tags.

Three fixes went in with this build:

* the "latest cross" was read from `n-2`, i.e. it **skipped the newest closed candle**, so the `Valid` time
  and the pending level could describe an older signal than the row's action;
* when a buy **and** a sell were waiting at once, the engine tried `buy` first instead of the signal that
  **armed first** — it could take the wrong side;
* the frame used for the signal columns and the frame used for entry indices are now forced to be the same
  one (`_prep` vs `closed`), so a bar dropped by the F&O filter can never shift the entry check by one bar.

### Paper Live vs the Scanner page — why the same symbol shows a different signal

They are answering **different questions**, with the same candles:

| | Scanner page | Paper Live |
|---|---|---|
| Which signal does it show? | the **newest WT cross** on the closed bars | the newest cross whose level is **still unbroken** (that is the one the backtest can trade) |
| `RSI` column | RSI **at the signal candle** | RSI **at the latest closed bar** |
| `Valid WT Val`, `VALID OPEN/HIGH/LOW/CLOSE`, `VALID DT` | measured **at the signal candle** | the signal candle's time is the `Valid` column; the level is in `Trig Lvl` |
| `WT1 / WT2 / WT Trend / WT CROSS` | at the latest bar | at the latest bar (**these must match — and they do**) |

Your WIPRO example:

* Scanner: `WT1 -62.45 / WT2 -57.69`, `Sell Trend`, `VALID DT 13:20`, `RSI 36.82` — the **latest cross is
  a sell at 13:20**, and its RSI is read *on that 13:20 candle*.
* Paper Live: `WT -62.45 / -57.69` (identical), `Buy Pending (BO > 193.00)`, valid `13:05`,
  `RSI 22.5` — the 13:20 sell is **already triggered**, so it can no longer be traded; the only signal
  still waiting to break is the **13:05 buy**, whose barrier is that candle's High = **193.00**. The RSI
  here is read on the **latest closed bar** (14:05), which is why it differs.

So `193.00` is not a wrong price: it is the High of the 13:05 buy-cross candle, and 193.00 is exactly
what has to break for that (older, still live) signal to fill. The row now spells the other one out in
small grey text, so you never have to reconcile the two pages in your head:

`latest cross: SELL @ 192.66 (13:20) · already triggered`

If the two pages ever disagree on `WT1`, `WT2` or `WT Trend`, that *is* a real bug — send it to me.

### Why two symbols with the same WT Action behave differently

You will see `Sell Triggered (already taken)` on one row with a position and on another with no
position. They are not the same state:

| Row | What really happened |
|---|---|
| **VSTIND** — `Sell Triggered (already taken)`, `SHORT 85190` | the sell level (195.64) was broken **by the live price while the engine was running** → the engine entered. The text then changes to *already taken* because that signal has now been used. |
| **TMB** — `Sell Triggered (already taken)`, no position | the break at 885.05 happened **8 bars ago**, before this run started (see *Valid* = 8 bars ago). The engine only enters on a break that happens **while it is running**, or on a signal that closes on the newest closed bar. Nothing is pending any more, so there is nothing to enter. |

That is why the row now also says, in small grey text under the action,
**`broke 8 bars ago — no live entry now`**. `Diff` confirms it: TMB LTP 886.55 is **above** the 885.05
sell level, so the sell level is not broken *now* either.

Rule in one line: **live entry = fresh break of the level by the live price (or a signal on the last
closed bar). A break that already happened before you pressed Start is history, not a trade.**

The number of bars has nothing to do with **how** the order is filled — that is the **Entry fill** setting
(`At breakout level` = the backtest's assumption; `Market if already past` = what you would do by hand).
`already taken` / `broke 12 bars ago` only says **whether there is a break to act on right now**:

| Bars ago | What happens |
|---|---|
| `0 bars ago` — the newest closed candle just gave the signal | the engine takes it on that tick (fill per **Entry fill**) |
| `1+ bars ago`, level still unbroken | `Buy/Sell Pending (BO > level)` — the engine is waiting for the break |
| `1+ bars ago`, level already broken in the past | `already taken` + `broke N bars ago — no live entry now` — nothing to do |

### The WT Action column (same words as the Scanner page)

| WT Action | Meaning |
|---|---|
| `Buy Pending (BO > 500.00)` | a buy signal is armed; the level to break is 500.00 |
| `Buy Triggered @ 503.10` | the live price just broke 500.00 — the engine opens/adds nothing new, it is already in |
| `Sell Pending (BO < 480.00)` | armed sell |
| `Sell Triggered @ 477.20` | live price broke the sell level |
| `Buy Timed Out (was > 500.00)` | the signal waited `Wait bars` candles and expired, like the backtest |
| `Buy Triggered, no fill` | that signal was already used or could not be sized (check capital / cash chip) |
| `No signal` | no live cross |

Next to it: **Trig Lvl** (the level), **Valid** (which closed candle gave the signal + how many bars ago),
**Diff / Diff %** (live price vs the level), and under the action, **WT1 / WT2 / RSI**.

### Step 6 — press **▶ Start**

It will now:
1. download candles for each symbol (same source as the backtest),
2. **drop the still-forming candle** — only closed candles create signals,
3. run WaveTrend + break-out detection on the closed bars,
4. open a paper position when a breakout closes,
5. every poll: check stop → trailing → target → opposite signal on the live LTP,
6. save everything to its own JSON files.

Poll timing is automatic from the timeframe, but you can force it in **Poll every (min)**
(decimals are fine — `1.5` = 90 seconds, `0` = automatic):

| TF | polls every | in minutes |
|---|---|---|
| 5m | 60 s | 1 |
| 15m | 90 s | 1.5 |
| 30m | 150 s | 2.5 |
| 1h | 240 s | 4 |
| 1d | 900 s | 15 |

It polls during **your market's** session (see the Market table above). Outside hours it slows to
**one scan every 5 minutes** while **Poll when closed** is ticked (it is on by default); untick it and
the engine idles until you press **Scan now** or **✔ Apply settings**.

### The `auto` tick — stop the timer, scan only when you say so

Next to `poll 1 min` in the strip there is a small **`auto`** checkbox, ticked by default.

| `auto` | What the engine does |
|---|---|
| ☑ ticked (default) | scans once, waits the poll interval, scans again — forever |
| ☐ unticked | scans **once** when you press Start, then **sits still**. You drive it: **⟳ Scan now**, or **✔ Apply settings** (which always triggers a scan) |

You can untick it while the engine is running — the change goes to the engine immediately (no Apply
needed, because clicking the box *is* the action). The strip then shows `next scan in 86397s`, which is
the page's way of saying "not scheduled". Live prices, positions and exits are still checked on every
scan you trigger — only the timer is off.

---

## PART 2 — Prove the logic is the same as the backtest

This is the important button: **🧪 Verify vs backtest**.

What it does: downloads the candles once, then runs

* the **live engine's** replay, and
* **`backtest.run()`** itself,

on the exact same bars, and compares every trade (entry time, exit time, entry price,
exit price, P&L, reason).

You should see:

```
✅ 100% MATCH
symbol HAL.NS · bars 899 · engine 81 trades · net -1,204.09 · backtest 81 trades · net -1,204.09
```

**Pick which symbol to check** with the dropdown above the verdict (the engine replays one symbol per
run — every candle of it). **You can now inspect every trade, not just the verdict.** Under the verdict there is a
trade-by-trade table: `# · Symbol · Side · Engine entry · Backtest entry · Engine exit ·
Backtest exit · Engine P&L · Backtest P&L · ✓`. Each cell carries the candle time and the price, e.g.
`10-16 10:15 @ 4783.30`. A ✓ means that trade's entry time, exit time, entry price, exit price and
P&L are identical in both engines; a ✕ marks a difference. First 30 trades are shown — press
**show all N trades** to see the rest (the API returns up to 200).

If you ever see ❌, the ✕ rows are the exact trades that differ.

That check is how I verified the engine — **16 setting combinations, every trade identical**:

| What was varied | Result |
|---|---|
| baseline, wide target, long-only + trailing, short-only, any-dir rule, HA off | 81 / 53 / 32 / 36 / 75 trades — all match |
| F&O filter off / on | 78/78 and 77/77 — both match, and the filter really changes the trades |
| Size by ₹5 000 / ₹25 000 / qty 7 / 25 % of capital | same trades, qty **17 / 88 / 7 / 88** — all match |
| Target as `%` vs `₹ amount` | both match, and net result differs (so the mode is really live) |
| Trailing + tighten near target | match |
| HTF trend filter on, HTF + RSI + WT filters | 40/40 and 13/13 — match |
| Live entry on a mid-candle break (synthetic, 3 seeds) | entered **at the level** every time, never at the LTP |
| Live entry, `Market if already past level` | filled at the LTP (328.25 → 333.92 in the RELIANCE test) |
| Two scans in a row, same signal | one position only — no double entry |

Through the web page (`/api/paper-live/verify`) the same probe on HAL.NS gave
`matched: true` for **fixed ₹5 000 (18 sh) → ₹25 000 (90 sh) → qty 9 (9 sh) → 50 % of capital (180 sh)**,
plus target-in-₹ and tightening — i.e. every sizing/target box on the page reaches both engines.

---

## PART 3 — Reading the live monitor

22 columns, grouped left to right:

| Column | Meaning |
|---|---|
| **LTP** | last price. `live` = fresh quote, `last close` = fallback |
| **Last closed bar** | the candle the engine is working from, its age in minutes, `F&O 15:15` when the filter is dropping late candles |
| **WT Action** | Scanner wording — `Buy Pending (BO > 500.00)`, `Buy Triggered @ 503.10`, `Sell Pending (BO < 480.00)`, `Buy Timed Out`, `Buy Triggered, no fill`. Under it: `WT 41.20 / 33.70 · RSI 58.4` |
| **Trig Lvl** | the breakout level of that signal |
| **Valid** | the closed candle that produced the signal + `N bars ago` |
| **Diff / Diff %** | live price vs the level (green = already past it) |
| **Position** | `LONG 15` / `SHORT 12` / `—`, plus `size: ₹5,000 per trade` (which number sized it) |
| **Qty** | shares in the open position |
| **Entry** | fill price + the time it was filled |
| **Target / SL** | recalculated from the *fill* price; SL follows the trailing rule |
| **Live P&L / P&L %** | open position P&L, before costs |
| **Risk ₹ / Reward ₹ / R:R** | `qty × |entry − SL|`, `qty × |target − entry|`, and reward ÷ risk — your money at stake vs your target |
| **Realized** | banked P&L of closed paper trades (after commission + slippage) |
| **Last action** | `ENTRY BUY 15 @ 328.25 (live break of 328.1795)` / `EXIT target @ 331.46` |
| **Status** | `OK`, `No Data`, or the error |

Orange **STALE** next to the bar age means the last candle is old (market closed / dead feed).
The engine then refuses **new entries** but still manages an open position.
`Allow stale entries` is for testing only — leave it off.

Below, **Paper trades** lists every **closed** trade with costs and reason
(`target`, `stop`, `trail_stop`, `opp_signal`). An open position is *not* a trade yet — it appears
there the moment it exits, which is why the block can stay empty while the monitor shows a position
(the line above the table tells you how many are open).

### Why a symbol did not take the trade

`Buy Triggered (already taken)` means that breakout had already happened **before** the page started
watching, so there was no live crossing left to enter — the engine deliberately does not chase it.
When an entry *was* blocked for a fixable reason the row says so in small print:
`no entry: not enough cash for 1 share at this size`, `no entry: side mode = long only`,
`no entry: this signal was already traded`.

**🗑 Reset** clears positions + history and starts from zero.

---

## PART 4 — Optional: put it inside your main app (port 8000)

Back up `server.py` first (`copy server.py server_backup.py`), then in `web/server.py`:

under
```python
from paper_trading import paper_trading_bp
```
add
```python
from paper_live_engine import paper_live_bp
```

and under
```python
app.register_blueprint(paper_trading_bp)
```
add
```python
app.register_blueprint(paper_live_bp)
```

Then `python server.py` and open `http://localhost:8000/paper-live`.

Optional menu button — in `web/templates/base.html`, after the Trade Setup link:
```html
    <a href="/paper-live" class="{{ 'active' if active=='paper-live' else '' }}">Paper Live</a>
```

**One optional line that makes the page feel snappier inside your main app.** At the bottom of
`web/server.py` the app is started like this:

```python
    app.run(host="0.0.0.0", port=PORT, debug=True)
```

That built-in server answers **one request at a time**, so while a big scan is running the page can look
stuck. Add `threaded=True` to that same line:

```python
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
```

(The standalone page on port 8020 already runs threaded, so this only matters for the port-8000 route.
The page itself now gives up on any request after a few seconds and says `server busy (no answer in 8s)`
instead of spinning forever, so this line is a nicety, not a requirement.)

If the app won't start, restore `server_backup.py`. The page files themselves are fine.

---

## PART 5 — Honest limits (read once)

1. **Yahoo prices are delayed** (typically 1–15 minutes on intraday). Entries and exits therefore
   happen a little later than a real broker would fire. That is the data source, not the logic.
2. **Exits are checked on the live price, not the candle's high/low.** In a backtest a wick can
   touch your target; live you only see the price when the poll happens. So a few more stops and
   fewer targets is normal.
3. **Entry fires when the live price breaks the level, inside the running candle** — that is the
   same moment the backtest's bar high/low would have triggered it, and the fill is the level, so
   the trade matches. What *cannot* match is a poll that arrives after the price has already run
   away and you use `Entry fill = Market if already past level`: that is deliberate chasing
   (RELIANCE example above: 328.25 → 333.92, +56.05 → −0.67).
   **🧪 Verify vs backtest always uses level fills**, because that is the only definition the
   backtest has — use it as the reference, and expect the Market mode to drift from it.
4. **No broker.** Nothing is sent to Zerodha. When you later add live execution, the order type
   must be a **stop / SL-Limit** (breakout), not a plain LIMIT — see
   `REVIEW_tradesetup_vs_backtest.md` §6.
5. The engine holds **one position per symbol**, and it will not re-enter the same signal twice.
6. This does not fix the Trade Setup page. It is a clean, correct engine you can trust while you
   fix or retire Trade Setup.
7. **Not mapped yet** (the backtest has it, this page does not — tell me if you use it):
   recovery / martingale sizing (`max_levels`, multiplier, recovery buffer & trail) and the
   SuperTrend / Chandelier signal sources. The engine already carries the correct sizing,
   target-mode and trailing-tighten values, so only the UI for those two groups is missing.

---

## PART 6 — If something goes wrong

| What you see | Fix |
|---|---|
| `Address already in use` / `Port 8020 is in use` | an **older copy** of the page is still running (closing the Terminal window does not stop Python).<br>**New behaviour:** the script now skips the busy port automatically and prints the URL it actually used — just open *that* URL.<br>**Or kill the ghost:** `lsof -ti:8020 \| xargs kill -9` (macOS/Linux), then start again.<br>**Or force a port:** `PAPER_LIVE_PORT=8030 python paper_live_engine.py` → open `http://localhost:8030/paper-live` |
| `No Data` on a row | lookback too long for the timeframe (`5m`/`15m` → use `1mo`), or Yahoo hiccup — press **⟳ Scan now** |
| `backtest.py not importable` | wrong Python — activate your venv and restart |
| Rows never update | market closed (see **market closed** chip) or you pressed Stop |
| Verify says ❌ | send me the diff table — that is a real logic difference and I want to see it |
| Positions look frozen | check the **STALE** marker; if the feed died, P&L stops moving |
| **The Sizing card has no “Size by” dropdown / no blue banner** | you are looking at a cached page. Stop the server (`Ctrl+C`) and start it again, then **Ctrl+Shift+R**. The header must show the current **`build v11 · market picker + trades out of hours`** pill — if that pill is missing (or says an older build), the browser is still on the old page. |
| **▶ Start spins and nothing happens** | the button no longer stays disabled: it re-enables when the answer arrives, and after 20 s it tells you `start did not take. Press ■ Stop, then ▶ Start again.` The **ribbon is the truth, not the button** — if it says **RUNNING**, the engine *is* polling (the first scan of many symbols can take a minute to fill the table). Every call is now time-boxed too, so a busy server answers `server busy (no answer in 8s)` instead of freezing the page. Then: (1) `Ctrl+Shift+R`, (2) if the ribbon is still STOPPED, press **■ Stop**, then **▶ Start** again, (3) still nothing → look at the Terminal for a red traceback and send it to me. |
| **Nothing changes when I edit the left panel while running** | that is by design — press **✔ Apply settings** (red dot = unsaved changes). See *“Changing anything while it is already running”*. |

---

## How to remove it

Delete `web/paper_live_engine.py`, `web/templates/paper_live.html`,
`web/data/paper_live_state.json`, `web/data/paper_live_trades.json`
(and the 2 lines from PART 4 if you added them).