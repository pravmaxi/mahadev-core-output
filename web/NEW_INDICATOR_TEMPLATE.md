# New Indicator Request

Fill every section. Use numbers. If a section doesn't apply, write "N/A".

## 1. Identity
- **Key** (short lowercase, no spaces): e.g. `sr`, `macd`, `bb`
- **Label** (human-readable for UI): e.g. "Support/Resistance"
- **Default parameters**: e.g. `{"left": 15, "right": 15}`

## 2. Line / Level — what the indicator plots
Describe in plain English what appears on the chart. Example:
"Red resistance line above price when a pivot high confirms. Blue support
line below price when a pivot low confirms. Lines can move when a newer
pivot confirms."

## 3. Valid Candle — the setup candle
- **Buy valid candle**: what makes a candle "valid" for a buy?
  Example: "First candle after R is set where High > R AND Close > R."
- **Sell valid candle**: mirror rule.
  Example: "First candle after S is set where Low < S AND Close < S."
- **Timing**: which bar does the valid candle occur on?
  Example: "The same bar that satisfies the condition."

## 4. Trigger — when does the order fire?
- **Buy trigger level**: what value becomes the trigger price?
  Example: "Valid candle's High (e.g. 105)."
- **Buy trigger condition**: what must happen for the trigger to fire?
  Example: "A later candle with High > 105."
- **Sell trigger level / condition**: mirror rule.
- **Same-bar or later-bar trigger?**
  Example: "Later bar (strict mode). Or same bar (loose mode)."

## 5. Nullification — what kills a pending valid candle?
List every condition that invalidates a valid candle before it triggers.
Example:
- "The R value changes (new pivot confirms at a different price)."
- "A valid SELL candle forms later (opposite direction overwrites)."

## 6. Columns
Check the WT column set as a base. Modify as needed.

Base (mandatory):
- `<key>_trend`               trend label (Buy / Sell / Inside)
- `<key>_valid_open/high/low/close/dt`
- `<key>_perfect`             Perfect / Imperfect
- `<key>_trigger_status`      "New Trigger" or ""
- `<key>_trigger_dt`
- `<key>_action`              "Buy Market Order" / "Buy Limit Order (BO close X)"
- `<key>_trigger_candle_date/time`
- `<key>_ltp`, `<key>_trigger_diff`, `<key>_diff_pct`, `<key>_profit`
- `<key>_booking_status`, `<key>_max_profit_pct`, `<key>_max_profit_value`
- `<key>_rsi`, `<key>_rsi_dir`

Indicator-specific columns (list here):
- `<key>_<name>` : <description of what value it holds>

## 7. Special rules
Anything unusual:
- Does it use Heikin Ashi or REAL candles? (ST uses real; WT follows cfg_ha.)
- Opposite-direction overwrite? (Yes/No — like WT yes)
- Session filter? (F&O 15:15 cutoff, etc.)
- Any other quirk the code must handle.

## 8. Concrete numeric walkthrough
Walk through one full cycle with real numbers. Example for ST:

  Bar 0: R drawn at 100 (pivot confirmed)
  Bar 1: H=102, C=99    → skipped (close below R)
  Bar 2: H=103, C=101   → valid buy candle. Trigger level = 103.
  Bar 3: H=104, C=102   → trigger candle. Order fires at 103.
  Bar 4: ...

This example is the single most useful thing for me — it disambiguates
every edge case at once.

## 9. Reference material
- Pine script (if you have one): attach or paste
- Any prior Python code that does something similar: attach or paste
- TradingView chart screenshot showing the setup: attach

## 10. Where to deploy
- [ ] Scanner only (this session)
- [ ] Scanner + Backtest
- [ ] Scanner + Backtest + Trading (LTP live)