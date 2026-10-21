# ============================================================
#  Sniper_trade – Full version with entry from crossover,
#  valid candle toggle (crossover or trigger)
# ============================================================

import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import warnings
import sys
import pytz
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from gspread.utils import rowcol_to_a1
import time

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================
#  USER CONFIGURATION – TOGGLES
# ============================================================

SELECTED_TF = "1h"
USE_HEIKIN_ASHI = False
INCLUDE_LTP = True
CASE_A_MARKET_ORDER = True
ENABLE_ZERODHA_ACTION = False
ENABLE_BUY_ORDERS = True
ENABLE_SELL_ORDERS = True
SHOW_LTP_INFO = True
USE_GOOGLEFINANCE = True
INVESTMENT_AMOUNT = 5000
PROFIT_TARGET_PERCENT = 5

# ====== SIGNAL CONFIRMATION TOGGLES ======
REQUIRE_STRONG_SCORE = True
REQUIRE_ADX_STRONG = True
REQUIRE_RSI_CONFIRM = True
REQUIRE_VWAP_CONFIRM = True
REQUIRE_VOLUME_CONFIRM = True
SCORE_THRESHOLD = 15

# ====== VALID CANDLE TOGGLE ======
# True  → valid candle = raw crossover (always)
# False → valid candle = confirmed trigger candle (if exists, else crossover)
VALID_CANDLE_USE_CROSSOVER = False

# ============================================================
#  GOOGLE SHEETS & FILE SETTINGS
# ============================================================

SHEET_NAME = "Stock Price Scraper"
INPUT_SHEET = "input2"
OUTPUT_SHEET = f"Sniper_trade__{SELECTED_TF}"
CREDENTIALS_FILE = "Json/automation-project-429417-c51140fdff86.json"

JSON_FILE = f"Sniper_trade_{SELECTED_TF}.json"
ORDER_SENT_FILE = f"order_sent_sniper_{SELECTED_TF}.json"
PENDING_ORDERS_FILE = "pending_orders.json"

SKIPPED_SYMBOLS = set()
IST = pytz.timezone("Asia/Kolkata")

CONFIG = {
    "5m":  {"interval": "5m",  "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "1h":  {"interval": "1h",  "period": "90d"},
    "4h":  {"interval": "4h",  "period": "10mo"},
    "1d":  {"interval": "1d",  "period": "10mo"},
    "1w":  {"interval": "1w",  "period": "10mo"},
    "1wk": {"interval": "1wk", "period": "10mo"},
}

# ============================================================
#  CLEAN VALUE HELPER
# ============================================================

def clean_value(v):
    if pd.isna(v):
        return None
    if isinstance(v, (np.floating, np.integer)):
        return float(v) if not pd.isna(v) else None
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, (list, tuple)):
        return [clean_value(x) for x in v]
    return v

# ============================================================
#  SNIPER INDICATOR (with full columns)
# ============================================================

def compute_sniper_indicator(df, atr_multiplier=1.5):
    df = df.copy()
    open_ = df['Open']
    high = df['High']
    low = df['Low']
    close = df['Close']
    volume = df['Volume']

    # EMA
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    # VWAP
    typical_price = (high + low + close) / 3
    vwap = (volume * typical_price).cumsum() / volume.cumsum()

    # ATR (Wilder's smoothing)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi5m = rsi

    # MACD
    exp1 = close.ewm(span=12, adjust=False).mean()
    exp2 = close.ewm(span=26, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    # ADX
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr_avg = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.ewm(span=14).mean() / tr_avg)
    minus_di = 100 * (minus_dm.ewm(span=14).mean() / tr_avg)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(14).mean()

    vol_avg = volume.rolling(20).mean()
    vol_status = volume > vol_avg

    # Scores
    bullPct = pd.Series(index=df.index, dtype=float)
    bearPct = pd.Series(index=df.index, dtype=float)

    for i in range(len(df)):
        bScore = 0
        if close.iloc[i] > vwap.iloc[i]:
            bScore += 1
        if rsi.iloc[i] > 50:
            bScore += 1
        if macd_line.iloc[i] > signal_line.iloc[i]:
            bScore += 1
        if ema9.iloc[i] > ema21.iloc[i]:
            bScore += 1
        if adx.iloc[i] > 25 and close.iloc[i] > ema9.iloc[i]:
            bScore += 1
        if volume.iloc[i] > vol_avg.iloc[i] and close.iloc[i] > open_.iloc[i]:
            bScore += 1
        if rsi5m.iloc[i] > 50:
            bScore += 1
        bullPct.iloc[i] = (bScore / 7) * 100

        rScore = 0
        if close.iloc[i] < vwap.iloc[i]:
            rScore += 1
        if rsi.iloc[i] < 50:
            rScore += 1
        if macd_line.iloc[i] < signal_line.iloc[i]:
            rScore += 1
        if ema9.iloc[i] < ema21.iloc[i]:
            rScore += 1
        if adx.iloc[i] > 25 and close.iloc[i] < ema9.iloc[i]:
            rScore += 1
        if volume.iloc[i] > vol_avg.iloc[i] and close.iloc[i] < open_.iloc[i]:
            rScore += 1
        if rsi5m.iloc[i] < 50:
            rScore += 1
        bearPct.iloc[i] = (rScore / 7) * 100

    biasText = pd.Series(index=df.index, dtype='object')
    for i in range(len(df)):
        diff = bullPct.iloc[i] - bearPct.iloc[i]
        if diff >= 40:
            biasText.iloc[i] = "STRONG BULL"
        elif -diff >= 40:
            biasText.iloc[i] = "STRONG BEAR"
        elif bullPct.iloc[i] > bearPct.iloc[i]:
            biasText.iloc[i] = "MILD BULL"
        else:
            biasText.iloc[i] = "MILD BEAR"

    # State machine with confirmations
    n = len(df)
    lastSignalState = [0] * n
    entryP = [0.0] * n
    slP = [0.0] * n
    t1 = [0.0] * n
    t2 = [0.0] * n
    t3 = [0.0] * n
    t4 = [0.0] * n
    t5 = [0.0] * n
    t1Hit = [False] * n
    t2Hit = [False] * n
    t3Hit = [False] * n
    t4Hit = [False] * n
    t5Hit = [False] * n
    triggerBuy = [False] * n
    triggerSell = [False] * n
    isRetest = [False] * n

    raw_cross_buy = [False] * n
    raw_cross_sell = [False] * n

    score_ok = [False] * n
    adx_ok = [False] * n
    rsi_ok = [False] * n
    vwap_ok = [False] * n
    volume_ok = [False] * n

    state = 0
    entry = 0.0
    sl = 0.0
    tp1, tp2, tp3, tp4, tp5 = 0.0, 0.0, 0.0, 0.0, 0.0
    hit1, hit2, hit3, hit4, hit5 = False, False, False, False, False

    for i in range(n):
        if i == 0:
            buyCond = False
            sellCond = False
        else:
            buyCond = (ema9.iloc[i] > ema21.iloc[i]) and (ema9.iloc[i-1] <= ema21.iloc[i-1])
            sellCond = (ema9.iloc[i] < ema21.iloc[i]) and (ema9.iloc[i-1] >= ema21.iloc[i-1])

        raw_cross_buy[i] = buyCond
        raw_cross_sell[i] = sellCond

        diff_score = bullPct.iloc[i] - bearPct.iloc[i]
        score_ok[i] = (diff_score > SCORE_THRESHOLD) if buyCond else (-diff_score > SCORE_THRESHOLD)
        if not buyCond and not sellCond:
            score_ok[i] = False
        adx_ok[i] = adx.iloc[i] > 25
        rsi_ok[i] = rsi.iloc[i] > 50 if buyCond else rsi.iloc[i] < 50
        vwap_ok[i] = close.iloc[i] > vwap.iloc[i] if buyCond else close.iloc[i] < vwap.iloc[i]
        volume_ok[i] = volume.iloc[i] > vol_avg.iloc[i]

        buy_confirmed = True
        sell_confirmed = True
        if REQUIRE_STRONG_SCORE:
            buy_confirmed = buy_confirmed and score_ok[i]
            sell_confirmed = sell_confirmed and score_ok[i]
        if REQUIRE_ADX_STRONG:
            buy_confirmed = buy_confirmed and adx_ok[i]
            sell_confirmed = sell_confirmed and adx_ok[i]
        if REQUIRE_RSI_CONFIRM:
            buy_confirmed = buy_confirmed and rsi_ok[i]
            sell_confirmed = sell_confirmed and rsi_ok[i]
        if REQUIRE_VWAP_CONFIRM:
            buy_confirmed = buy_confirmed and vwap_ok[i]
            sell_confirmed = sell_confirmed and vwap_ok[i]
        if REQUIRE_VOLUME_CONFIRM:
            buy_confirmed = buy_confirmed and volume_ok[i]
            sell_confirmed = sell_confirmed and volume_ok[i]

        trBuy = buyCond and state <= 0 and buy_confirmed
        trSell = sellCond and state >= 0 and sell_confirmed

        if trBuy or trSell:
            state = 1 if trBuy else -1
            entry = close.iloc[i]
            risk = atr.iloc[i] * atr_multiplier
            sl = entry - risk if trBuy else entry + risk
            tp1 = entry + risk if trBuy else entry - risk
            tp2 = entry + 2*risk if trBuy else entry - 2*risk
            tp3 = entry + 3*risk if trBuy else entry - 3*risk
            tp4 = entry + 4*risk if trBuy else entry - 4*risk
            tp5 = entry + 5*risk if trBuy else entry - 5*risk
            hit1 = hit2 = hit3 = hit4 = hit5 = False

        if state == 1:
            if high.iloc[i] >= tp1: hit1 = True
            if high.iloc[i] >= tp2: hit2 = True
            if high.iloc[i] >= tp3: hit3 = True
            if high.iloc[i] >= tp4: hit4 = True
            if high.iloc[i] >= tp5: hit5 = True
        elif state == -1:
            if low.iloc[i] <= tp1: hit1 = True
            if low.iloc[i] <= tp2: hit2 = True
            if low.iloc[i] <= tp3: hit3 = True
            if low.iloc[i] <= tp4: hit4 = True
            if low.iloc[i] <= tp5: hit5 = True

        retest = False
        if state == 1:
            retest = (low.iloc[i] <= ema9.iloc[i]) and (low.iloc[i] > ema21.iloc[i])
        elif state == -1:
            retest = (high.iloc[i] >= ema9.iloc[i]) and (high.iloc[i] < ema21.iloc[i])

        lastSignalState[i] = state
        entryP[i] = entry
        slP[i] = sl
        t1[i] = tp1
        t2[i] = tp2
        t3[i] = tp3
        t4[i] = tp4
        t5[i] = tp5
        t1Hit[i] = hit1
        t2Hit[i] = hit2
        t3Hit[i] = hit3
        t4Hit[i] = hit4
        t5Hit[i] = hit5
        triggerBuy[i] = trBuy
        triggerSell[i] = trSell
        isRetest[i] = retest

    colour = pd.Series(index=df.index, dtype='object')
    for i in range(n):
        if lastSignalState[i] == 1:
            colour.iloc[i] = 'green'
        elif lastSignalState[i] == -1:
            colour.iloc[i] = 'red'
        else:
            colour.iloc[i] = 'yellow'

    out_df = pd.DataFrame({
        'ema9': ema9, 'ema21': ema21, 'vwap': vwap, 'atr': atr,
        'rsi': rsi, 'rsi5m': rsi5m,
        'macd_line': macd_line, 'signal_line': signal_line,
        'adx': adx, 'vol_avg': vol_avg, 'vol_status': vol_status,
        'bullPct': bullPct, 'bearPct': bearPct, 'biasText': biasText,
        'lastSignalState': lastSignalState,
        'entryP': entryP, 'slP': slP,
        't1': t1, 't2': t2, 't3': t3, 't4': t4, 't5': t5,
        't1Hit': t1Hit, 't2Hit': t2Hit, 't3Hit': t3Hit, 't4Hit': t4Hit, 't5Hit': t5Hit,
        'triggerBuy': triggerBuy, 'triggerSell': triggerSell, 'isRetest': isRetest,
        'colour': colour,
        'raw_cross_buy': raw_cross_buy,
        'raw_cross_sell': raw_cross_sell,
        'score_ok': score_ok,
        'adx_ok': adx_ok,
        'rsi_ok': rsi_ok,
        'vwap_ok': vwap_ok,
        'volume_ok': volume_ok
    }, index=df.index)

    return out_df

# ============================================================
#  OTHER HELPER FUNCTIONS
# ============================================================

def heikin_ashi(df):
    ha = pd.DataFrame(index=df.index)
    ha['Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4.0
    ha['Open'] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha.loc[ha.index[i], 'Open'] = (ha.loc[ha.index[i-1], 'Open'] + ha.loc[ha.index[i-1], 'Close']) / 2.0
    ha['High'] = pd.concat([df['High'], ha['Open'], ha['Close']], axis=1).max(axis=1)
    ha['Low'] = pd.concat([df['Low'], ha['Open'], ha['Close']], axis=1).min(axis=1)
    return ha[['Open', 'High', 'Low', 'Close']]

def load_order_sent():
    if os.path.exists(ORDER_SENT_FILE):
        with open(ORDER_SENT_FILE) as f:
            data = json.load(f)
            return set(tuple(item) for item in data)
    return set()

def save_order_sent(order_set):
    with open(ORDER_SENT_FILE, 'w') as f:
        json.dump([list(item) for item in order_set], f, indent=4)

def write_pending_order(symbol, action, level, tf):
    if not ENABLE_ZERODHA_ACTION:
        return
    if "Buy" in action and not ENABLE_BUY_ORDERS:
        return
    if "Sell" in action and not ENABLE_SELL_ORDERS:
        return
    pending = {
        "symbol": symbol,
        "action": action,
        "level": level,
        "timeframe": tf,
        "timestamp": datetime.now(IST).isoformat()
    }
    try:
        if os.path.exists(PENDING_ORDERS_FILE):
            with open(PENDING_ORDERS_FILE, 'r') as f:
                data = json.load(f)
        else:
            data = []
        data.append(pending)
        with open(PENDING_ORDERS_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ Could not write to {PENDING_ORDERS_FILE}: {e}")

def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        client.timeout = 30
        return client.open(SHEET_NAME)
    except Exception as e:
        print(f"❌ Google Sheets connection failed: {e}")
        sys.exit(1)

def read_symbols(sheet):
    ws = sheet.worksheet(INPUT_SHEET)
    raw_symbols = [s.strip().upper() for s in ws.col_values(1) if s.strip()]
    processed = []
    for s in raw_symbols:
        if s.startswith("^") or ":" in s:
            processed.append(s)
        else:
            processed.append(f"{s}.NS")
    return processed

def write_output_batch(sheet, rows, summary=None, show_ltp_info=False,
                       use_googlefinance=False, investment_amount=5000,
                       target_pct=5):
    try:
        ws = sheet.worksheet(OUTPUT_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=OUTPUT_SHEET, rows=2000, cols=30)
    ws.clear()
    start_row = 1
    if summary:
        ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
        start_row = 2

    base_header = [
        "Timestamp", "Symbol", "Timeframe",
        "Indicator Value", "Indicator Colour",
        "Valid Candle Open", "Valid Candle High", "Valid Candle Low", "Valid Candle Close",
        "Valid Candle DateTime", "Indicator Value at Valid Candle", "Trend",
        "Trigger Status", "Trigger DateTime",
        "Zerodha Action", "Trigger Candle Date", "Trigger Candle Time"
    ]
    if show_ltp_info:
        ltp_header = ["LTP", "Diff from Trigger", "Diff %"]
    else:
        ltp_header = []
    profit_columns = [
        "Profit/Loss (₹)",
        "Booking Profit Status",
        "Max Profit % Achieved",
        "Max Profit Value"
    ]
    sniper_columns = [
        "Bull Score (%)", "Bear Score (%)", "Bias Text",
        "EMA9", "EMA21", "VWAP", "ATR", "RSI (14)", "RSI (5m)",
        "MACD Main", "MACD Signal", "ADX", "Volume Status", "Trend Strength",
        "Last Signal State", "Entry Price", "SL Price",
        "TP1", "TP2", "TP3", "TP4", "TP5",
        "TP1 Hit", "TP2 Hit", "TP3 Hit", "TP4 Hit", "TP5 Hit",
        "Trigger Buy", "Trigger Sell", "Is Retest"
    ]
    confirm_columns = [
        "Score OK", "ADX OK", "RSI OK", "VWAP OK", "Volume OK"
    ]
    debug_columns = [
        "Raw Crossover Buy", "Raw Crossover Sell",
        "Crossover Candle DateTime"
    ]
    header = base_header + ltp_header + ["Symbol"] + profit_columns + sniper_columns + confirm_columns + debug_columns

    cleaned_rows = []
    for row in rows:
        cleaned_row = [clean_value(v) for v in row]
        cleaned_rows.append(cleaned_row)

    ws.update(f"A{start_row}", [header] + cleaned_rows, value_input_option="USER_ENTERED")

    if show_ltp_info and use_googlefinance:
        try:
            symbol_col = header.index("Symbol") + 1
            trend_col = header.index("Trend") + 1
            high_col = header.index("Valid Candle High") + 1
            low_col = header.index("Valid Candle Low") + 1
            ltp_col = header.index("LTP") + 1
            diff_col = header.index("Diff from Trigger") + 1
            diff_pct_col = header.index("Diff %") + 1
        except ValueError:
            return ws, start_row
        batch_data = []
        for i in range(start_row + 1, start_row + 1 + len(rows)):
            symbol_letter = rowcol_to_a1(1, symbol_col)[0]
            trend_letter = rowcol_to_a1(1, trend_col)[0]
            high_letter = rowcol_to_a1(1, high_col)[0]
            low_letter = rowcol_to_a1(1, low_col)[0]
            ltp_letter = rowcol_to_a1(1, ltp_col)[0]
            diff_letter = rowcol_to_a1(1, diff_col)[0]
            diff_pct_letter = rowcol_to_a1(1, diff_pct_col)[0]
            ltp_formula = f'=GOOGLEFINANCE("NSE:" & {symbol_letter}{i})'
            diff_formula = f'=IF({trend_letter}{i}="Buy Trend", {ltp_letter}{i} - {high_letter}{i}, {low_letter}{i} - {ltp_letter}{i})'
            diff_pct_formula = f'=IF({trend_letter}{i}="Buy Trend", {diff_letter}{i} / {high_letter}{i}, {diff_letter}{i} / {low_letter}{i}) * 100'
            batch_data.append({"range": f"{ltp_letter}{i}", "values": [[ltp_formula]]})
            batch_data.append({"range": f"{diff_letter}{i}", "values": [[diff_formula]]})
            batch_data.append({"range": f"{diff_pct_letter}{i}", "values": [[diff_pct_formula]]})
        if batch_data:
            ws.batch_update(batch_data, value_input_option='USER_ENTERED')

    if show_ltp_info:
        try:
            trend_col = header.index("Trend") + 1
            high_col = header.index("Valid Candle High") + 1
            low_col = header.index("Valid Candle Low") + 1
            diff_col = header.index("Diff from Trigger") + 1
            profit_col = header.index("Profit/Loss (₹)") + 1
        except ValueError:
            return ws, start_row
        batch_data = []
        for i in range(start_row + 1, start_row + 1 + len(rows)):
            trend_letter = rowcol_to_a1(1, trend_col)[0]
            high_letter = rowcol_to_a1(1, high_col)[0]
            low_letter = rowcol_to_a1(1, low_col)[0]
            diff_letter = rowcol_to_a1(1, diff_col)[0]
            profit_letter = rowcol_to_a1(1, profit_col)[0]
            formula = f'=IF({trend_letter}{i}="Sell Trend", TRUNC({investment_amount}/{low_letter}{i}), TRUNC({investment_amount}/{high_letter}{i})) * {diff_letter}{i}'
            batch_data.append({"range": f"{profit_letter}{i}", "values": [[formula]]})
        if batch_data:
            ws.batch_update(batch_data, value_input_option='USER_ENTERED')

    return ws, start_row

def apply_color_formatting(ws, header_row, zerodha_actions):
    header = ws.row_values(header_row)
    try:
        col_idx = header.index("Zerodha Action") + 1
    except ValueError:
        return
    batch = []
    for r, action in enumerate(zerodha_actions, start=header_row + 1):
        if action and "Buy" in action:
            color = {"red": 0.72, "green": 0.88, "blue": 0.80}
        elif action and "Sell" in action:
            color = {"red": 0.96, "green": 0.80, "blue": 0.80}
        else:
            color = {"red": 1.0, "green": 1.0, "blue": 1.0}
        batch.append({
            "range": rowcol_to_a1(r, col_idx),
            "format": {"backgroundColor": color}
        })
    if batch:
        ws.batch_format(batch)

def get_batch_ltp(symbols):
    ltp_dict = {}
    try:
        normal_symbols = [s for s in symbols if not s.startswith("^")]
        if normal_symbols:
            tickers = yf.Tickers(" ".join(normal_symbols))
            for sym in normal_symbols:
                try:
                    info = tickers.tickers[sym].info
                    ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                    if ltp:
                        ltp_dict[sym] = round(ltp, 2)
                except:
                    pass
        index_symbols = [s for s in symbols if s.startswith("^")]
        for sym in index_symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                ltp = info.get('regularMarketPrice') or info.get('currentPrice')
                if ltp:
                    ltp_dict[sym] = round(ltp, 2)
            except:
                pass
    except Exception as e:
        print(f"⚠️ Batch LTP fetch failed: {e}")
    return ltp_dict

def download_symbol_with_retry(symbol, interval, period, max_retries=2):
    for attempt in range(max_retries):
        try:
            df = yf.download(symbol, interval=interval, period=period,
                             progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"⚠️ Attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(1)
    return None

def download_batch_data(symbols, interval, period):
    normal = [s for s in symbols if not s.startswith("^")]
    index = [s for s in symbols if s.startswith("^")]
    data_dict = {}
    if normal:
        try:
            df = yf.download(normal, interval=interval, period=period,
                             group_by='ticker', progress=False, auto_adjust=False)
            for sym in normal:
                if sym in df.columns.levels[0]:
                    data_dict[sym] = df[sym].dropna()
                else:
                    data_dict[sym] = download_symbol_with_retry(sym, interval, period)
        except Exception as e:
            print(f"⚠️ Batch download failed: {e}")
            for sym in normal:
                data_dict[sym] = download_symbol_with_retry(sym, interval, period)
    for sym in index:
        data_dict[sym] = download_symbol_with_retry(sym, interval, period)
    return data_dict

# ============================================================
#  MAIN
# ============================================================

def main():
    print("⏳ Connecting to Google Sheets...")
    sheet = connect_gsheet()
    print("✅ Connected.")

    symbols = read_symbols(sheet)
    total = len(symbols)
    print(f"📊 Found {total} symbols in {INPUT_SHEET}")

    start_time = datetime.now(IST)
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S %Z")

    triggered_info = {}
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE) as f:
            try:
                existing = json.load(f)
                if isinstance(existing, dict):
                    triggered_info = existing.get("triggered_info", {})
            except:
                pass

    order_sent_set = load_order_sent()

    cfg = CONFIG.get(SELECTED_TF)
    if cfg is None:
        print(f"❌ Unsupported timeframe: {SELECTED_TF}")
        sys.exit(1)

    print(f"⏳ Downloading data for {len(symbols)} symbols (batch)...")
    data_dict = download_batch_data(symbols, cfg["interval"], cfg["period"])
    print("✅ Data download complete.")

    ltp_dict = {}
    if SHOW_LTP_INFO and not USE_GOOGLEFINANCE:
        print("⏳ Fetching LTP for all symbols (batch)...")
        ltp_dict = get_batch_ltp(symbols)
        print("✅ LTP fetch complete.")

    rows = []
    zerodha_actions = []
    new_triggers = 0
    spinner = ["|", "/", "-", "\\"]

    for idx, symbol in enumerate(symbols, start=1):
        print(f"\r⏳ {spinner[idx % 4]} Processing {idx}/{total} : {symbol}", end="")
        sys.stdout.flush()

        df = data_dict.get(symbol)
        if df is None or df.empty:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        df = df.dropna()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if len(df) < 4:
            SKIPPED_SYMBOLS.add(symbol)
            continue

        if USE_HEIKIN_ASHI:
            df = heikin_ashi(df)

        sniper_df = compute_sniper_indicator(df, atr_multiplier=1.5)

        current_idx = -2
        current_colour = sniper_df['colour'].iloc[current_idx]
        current_bias = sniper_df['biasText'].iloc[current_idx]
        indicator_value = current_bias

        # --- Find most recent raw crossover ---
        crossover_index = None
        for i in range(len(sniper_df)-2, 0, -1):
            if sniper_df['raw_cross_buy'].iloc[i] or sniper_df['raw_cross_sell'].iloc[i]:
                crossover_index = i
                break

        # --- Find most recent confirmed trigger ---
        trigger_index = None
        for i in range(len(sniper_df)-2, 0, -1):
            if sniper_df['triggerBuy'].iloc[i] or sniper_df['triggerSell'].iloc[i]:
                trigger_index = i
                break

        # --- Determine valid candle index based on toggle ---
        if VALID_CANDLE_USE_CROSSOVER:
            valid_idx = crossover_index
        else:
            # Prefer trigger index if exists, else crossover
            valid_idx = trigger_index if trigger_index is not None else crossover_index

        valid_open = valid_high = valid_low = valid_close = valid_dt = valid_ind_value = ""
        crossover_close = None
        crossover_atr = None
        is_buy_cross = False

        if valid_idx is not None:
            candle = df.iloc[valid_idx]
            valid_open = clean_value(round(candle["Open"].item(), 2))
            valid_high = clean_value(round(candle["High"].item(), 2))
            valid_low = clean_value(round(candle["Low"].item(), 2))
            valid_close = clean_value(round(candle["Close"].item(), 2))
            valid_ind_value = clean_value(round(sniper_df['bullPct'].iloc[valid_idx] - sniper_df['bearPct'].iloc[valid_idx], 2))
            dt = pd.to_datetime(df.index[valid_idx])
            if dt.tzinfo is None:
                dt = dt.tz_localize("UTC").tz_convert(IST)
            else:
                dt = dt.tz_convert(IST)
            valid_dt = dt.strftime("%Y-%m-%d %H:%M")

        # --- For Entry/SL/TP, always use crossover candle ---
        if crossover_index is not None:
            cross_candle = df.iloc[crossover_index]
            crossover_close = cross_candle["Close"].item()
            crossover_atr = sniper_df['atr'].iloc[crossover_index]
            is_buy_cross = sniper_df['raw_cross_buy'].iloc[crossover_index]

        entry_price_cross = None
        sl_price_cross = None
        tp1_cross = tp2_cross = tp3_cross = tp4_cross = tp5_cross = None

        if crossover_close is not None and crossover_atr is not None:
            risk = crossover_atr * 1.5
            if is_buy_cross:
                entry_price_cross = crossover_close
                sl_price_cross = entry_price_cross - risk
                tp1_cross = entry_price_cross + risk
                tp2_cross = entry_price_cross + 2*risk
                tp3_cross = entry_price_cross + 3*risk
                tp4_cross = entry_price_cross + 4*risk
                tp5_cross = entry_price_cross + 5*risk
            else:
                entry_price_cross = crossover_close
                sl_price_cross = entry_price_cross + risk
                tp1_cross = entry_price_cross - risk
                tp2_cross = entry_price_cross - 2*risk
                tp3_cross = entry_price_cross - 3*risk
                tp4_cross = entry_price_cross - 4*risk
                tp5_cross = entry_price_cross - 5*risk

            entry_price_cross = round(entry_price_cross, 2)
            sl_price_cross = round(sl_price_cross, 2)
            tp1_cross = round(tp1_cross, 2)
            tp2_cross = round(tp2_cross, 2)
            tp3_cross = round(tp3_cross, 2)
            tp4_cross = round(tp4_cross, 2)
            tp5_cross = round(tp5_cross, 2)

        # --- Trigger level for Zerodha action ---
        trigger_level = None
        direction = ""
        trigger_candle_date = ""
        trigger_candle_time = ""

        if trigger_index is not None:
            if sniper_df['triggerBuy'].iloc[trigger_index]:
                direction = "buy"
                trigger_level = valid_high  # using valid candle high (which might be crossover or trigger)
            else:
                direction = "sell"
                trigger_level = valid_low

            dt = pd.to_datetime(df.index[trigger_index])
            if dt.tzinfo is None:
                dt = dt.tz_localize("UTC").tz_convert(IST)
            else:
                dt = dt.tz_convert(IST)
            trigger_candle_date = dt.strftime("%Y-%m-%d")
            trigger_candle_time = dt.strftime("%H:%M")
        else:
            trigger_index = None
            trigger_level = None

        # Determine trend label
        trend_label = "Sell Trend" if current_colour == 'red' else "Buy Trend" if current_colour == 'green' else "Inside Trend"

        # Trigger status
        key = f"{symbol}_{SELECTED_TF}"
        stored = triggered_info.get(key)
        trigger_status = ""
        trigger_datetime = ""

        if trigger_index is not None:
            if stored is None or stored.get('colour') != current_colour:
                trigger_status = "New Trigger"
                trigger_datetime = valid_dt if valid_idx is not None else ""
                triggered_info[key] = {
                    "colour": current_colour,
                    "datetime": trigger_datetime,
                    "indicator_value": indicator_value
                }
                new_triggers += 1
            else:
                trigger_status = ""
                trigger_datetime = stored.get('datetime', '') if stored else ""

        # Zerodha Action
        zerodha_action = ""
        if trigger_index is not None and trigger_level is not None:
            order_key = (symbol, trigger_level, direction)
            if order_key in order_sent_set:
                zerodha_action = "Already buy order sent" if direction == "buy" else "Already sell order sent"
            else:
                if CASE_A_MARKET_ORDER:
                    zerodha_action = "Buy Market Order" if direction == "buy" else "Sell Market Order"
                else:
                    zerodha_action = "Buy Long gap" if direction == "buy" else "Sell gap"
                order_sent_set.add(order_key)

                if zerodha_action and "Already" not in zerodha_action and "gap" not in zerodha_action:
                    write_pending_order(symbol, zerodha_action, trigger_level, SELECTED_TF)

        # Booking Profit Status
        booking_status = ""
        max_profit_pct = 0.0
        max_profit_value = 0.0

        if trigger_index is not None and direction in ('buy', 'sell'):
            entry_price = trigger_level
            if direction == 'buy':
                prices = df.iloc[trigger_index : len(df)-1]['High']
                current_price = df.iloc[-2]['Close']
                max_price = prices.max()
                max_profit_pct = round((max_price - entry_price) / entry_price * 100, 2)
                current_profit_pct = round((current_price - entry_price) / entry_price * 100, 2)
                max_profit_value = round(max_price - entry_price, 2)
            else:
                prices = df.iloc[trigger_index : len(df)-1]['Low']
                current_price = df.iloc[-2]['Close']
                min_price = prices.min()
                max_profit_pct = round((entry_price - min_price) / entry_price * 100, 2)
                current_profit_pct = round((entry_price - current_price) / entry_price * 100, 2)
                max_profit_value = round(entry_price - min_price, 2)

            if max_profit_pct >= PROFIT_TARGET_PERCENT:
                booking_status = f"Profit booked at {PROFIT_TARGET_PERCENT}%"
            elif max_profit_pct > 0 and current_profit_pct > 0:
                booking_status = f"Profit in progress of {max_profit_pct:.1f}%"
            elif max_profit_pct > 0 and current_profit_pct <= 0:
                booking_status = f"Booked maximum profit of {max_profit_pct:.1f}%"
            else:
                booking_status = "No profit yet"
        else:
            booking_status = ""
            max_profit_pct = ""
            max_profit_value = ""

        # LTP info
        ltp = ""
        diff = ""
        diff_pct = ""
        if SHOW_LTP_INFO and not USE_GOOGLEFINANCE:
            if valid_idx is not None and current_colour in ('green', 'red') and trigger_level is not None:
                ltp_val = ltp_dict.get(symbol)
                if ltp_val:
                    ltp = ltp_val
                    if current_colour == 'green':
                        diff = round(ltp - trigger_level, 2)
                        diff_pct = round((diff / trigger_level) * 100, 2)
                    else:
                        diff = round(trigger_level - ltp, 2)
                        diff_pct = round((diff / trigger_level) * 100, 2)

        # Current row for other values and hit statuses
        current_row = sniper_df.iloc[current_idx]

        def bool_str(val):
            return "TRUE" if val else "FALSE"

        sniper_vals = [
            clean_value(current_row['bullPct']),
            clean_value(current_row['bearPct']),
            clean_value(current_row['biasText']),
            clean_value(current_row['ema9']),
            clean_value(current_row['ema21']),
            clean_value(current_row['vwap']),
            clean_value(current_row['atr']),
            clean_value(current_row['rsi']),
            clean_value(current_row['rsi5m']),
            clean_value(current_row['macd_line']),
            clean_value(current_row['signal_line']),
            clean_value(current_row['adx']),
            "HIGH" if clean_value(current_row['vol_status']) else "LOW",
            "STRONG" if clean_value(current_row['adx'] > 25) else "WEAK",
            clean_value(current_row['lastSignalState']),
            clean_value(entry_price_cross),
            clean_value(sl_price_cross),
            clean_value(tp1_cross),
            clean_value(tp2_cross),
            clean_value(tp3_cross),
            clean_value(tp4_cross),
            clean_value(tp5_cross),
            bool_str(clean_value(current_row['t1Hit'])),
            bool_str(clean_value(current_row['t2Hit'])),
            bool_str(clean_value(current_row['t3Hit'])),
            bool_str(clean_value(current_row['t4Hit'])),
            bool_str(clean_value(current_row['t5Hit'])),
            bool_str(clean_value(current_row['triggerBuy'])),
            bool_str(clean_value(current_row['triggerSell'])),
            bool_str(clean_value(current_row['isRetest']))
        ]

        confirm_vals = [
            bool_str(clean_value(current_row['score_ok'])),
            bool_str(clean_value(current_row['adx_ok'])),
            bool_str(clean_value(current_row['rsi_ok'])),
            bool_str(clean_value(current_row['vwap_ok'])),
            bool_str(clean_value(current_row['volume_ok']))
        ]

        raw_cross_buy = bool_str(clean_value(current_row['raw_cross_buy']))
        raw_cross_sell = bool_str(clean_value(current_row['raw_cross_sell']))

        display_symbol = symbol.replace('.NS', '')
        base_row = [
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            display_symbol,
            SELECTED_TF,
            clean_value(indicator_value),
            clean_value(current_colour),
            clean_value(valid_open),
            clean_value(valid_high),
            clean_value(valid_low),
            clean_value(valid_close),
            clean_value(valid_dt),
            clean_value(valid_ind_value),
            clean_value(trend_label),
            clean_value(trigger_status),
            clean_value(trigger_datetime),
            clean_value(zerodha_action),
            clean_value(trigger_candle_date),
            clean_value(trigger_candle_time)
        ]
        if SHOW_LTP_INFO:
            base_row.extend([clean_value(ltp), clean_value(diff), clean_value(diff_pct)])
        base_row.append(display_symbol)
        base_row.append("")
        base_row.append(clean_value(booking_status))
        base_row.append(clean_value(max_profit_pct))
        base_row.append(clean_value(max_profit_value))
        base_row.extend(sniper_vals)
        base_row.extend(confirm_vals)
        base_row.extend([raw_cross_buy, raw_cross_sell, clean_value(valid_dt)])

        rows.append(base_row)
        zerodha_actions.append(zerodha_action)

    save_order_sent(order_sent_set)

    out_json = {
        "run_start": start_str,
        "duration": f"{(datetime.now(IST) - start_time).total_seconds():.1f}s",
        "triggered_info": triggered_info
    }
    with open(JSON_FILE, "w") as f:
        json.dump(out_json, f, indent=4)

    summary = (
        f"Timeframe: {SELECTED_TF} | Run start: {start_str} | "
        f"Duration: {out_json['duration']} | Total: {total} | New Triggers: {new_triggers} | "
        f"Skipped: {len(SKIPPED_SYMBOLS)} | Heikin Ashi: {USE_HEIKIN_ASHI} | "
        f"Zerodha: {ENABLE_ZERODHA_ACTION} | Buy orders: {ENABLE_BUY_ORDERS} | Sell orders: {ENABLE_SELL_ORDERS} | "
        f"LTP info: {SHOW_LTP_INFO} | GoogleFinance: {USE_GOOGLEFINANCE} | Investment: ₹{INVESTMENT_AMOUNT} | Target Profit: {PROFIT_TARGET_PERCENT}% | "
        f"Confirmations: Score={REQUIRE_STRONG_SCORE}, ADX={REQUIRE_ADX_STRONG}, RSI={REQUIRE_RSI_CONFIRM}, VWAP={REQUIRE_VWAP_CONFIRM}, Vol={REQUIRE_VOLUME_CONFIRM} | "
        f"Valid candle uses crossover: {VALID_CANDLE_USE_CROSSOVER}"
    )
    if rows:
        ws, header_row = write_output_batch(sheet, rows, summary=summary,
                                            show_ltp_info=SHOW_LTP_INFO,
                                            use_googlefinance=USE_GOOGLEFINANCE,
                                            investment_amount=INVESTMENT_AMOUNT,
                                            target_pct=PROFIT_TARGET_PERCENT)
        apply_color_formatting(ws, header_row, zerodha_actions)

    print("\n\n📊 RUN SUMMARY")
    print("────────────────────────────")
    print(f"Timeframe          : {SELECTED_TF}")
    print(f"Heikin Ashi        : {USE_HEIKIN_ASHI}")
    print(f"Run start          : {start_str}")
    print(f"Duration           : {out_json['duration']}")
    print(f"Total Symbols      : {total}")
    print(f"New Triggers       : {new_triggers}")
    print(f"Skipped Symbols    : {len(SKIPPED_SYMBOLS)}")
    print(f"Zerodha Action     : {ENABLE_ZERODHA_ACTION}")
    print(f"Buy Orders allowed : {ENABLE_BUY_ORDERS}")
    print(f"Sell Orders allowed: {ENABLE_SELL_ORDERS}")
    print(f"Show LTP info      : {SHOW_LTP_INFO}")
    print(f"Use GoogleFinance  : {USE_GOOGLEFINANCE}")
    print(f"Investment Amount  : ₹{INVESTMENT_AMOUNT}")
    print(f"Target Profit %    : {PROFIT_TARGET_PERCENT}%")
    print("Confirmations applied:")
    print(f"  Score threshold  : {SCORE_THRESHOLD}% (enabled: {REQUIRE_STRONG_SCORE})")
    print(f"  ADX > 25         : {REQUIRE_ADX_STRONG}")
    print(f"  RSI > 50 (Buy)   : {REQUIRE_RSI_CONFIRM}")
    print(f"  Price > VWAP     : {REQUIRE_VWAP_CONFIRM}")
    print(f"  Volume > average : {REQUIRE_VOLUME_CONFIRM}")
    print(f"Valid candle uses crossover: {VALID_CANDLE_USE_CROSSOVER}")
    print("────────────────────────────")
    print("✅ Sheet, JSON & order files updated successfully")

if __name__ == "__main__":
    main()