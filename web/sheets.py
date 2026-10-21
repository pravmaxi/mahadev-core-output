"""
Google Sheets reader/writer — gspread wrapper.
Uses CREDENTIALS_FILE from settings.py.
Auto-detects exchange from tab name:
  - Tab starts with "us" / contains "us_" / ends with "_us" → US stocks (no suffix)
  - Tab contains "bse" → BSE stocks (.BO suffix)
  - Anything else (F&O, zero, ind, etc.) → NSE stocks (.NS suffix)
"""

try:
    from .settings import CREDENTIALS_FILE
except ImportError:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from settings import CREDENTIALS_FILE


_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
_SKIP = {"SYMBOL", "STOCK", "FNO", "F&O", "SYMBOLS"}


def _client():
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(CREDENTIALS_FILE), _SCOPES)
    c = gspread.authorize(creds)
    c.timeout = 30
    return c


def _detect_exchange(symbols_tab):
    """
    Decide exchange from the tab name.
      US_input   → US
      BSE_input  → BSE
      ind_input  → NSE
      F&O        → NSE
      zero_input → NSE
    """
    tab = (symbols_tab or "").strip().lower()
    if tab.startswith("us") or "us_" in tab or tab.endswith("_us"):
        return "US"
    if "bse" in tab:
        return "BSE"
    return "NSE"


def _add_suffix(symbol, exchange):
    """Apply the right suffix based on exchange. Idempotent for already-suffixed."""
    s = str(symbol).strip().upper()
    if not s:
        return None
    # leave indices, futures, forex, already-suffixed symbols alone
    if s.startswith("^") or "." in s or "=" in s:
        return s
    if exchange == "US":
        return s                     # AAPL, NVDA — no suffix
    if exchange == "BSE":
        return s + ".BO"
    return s + ".NS"                 # NSE default


def read_symbols(sheet_name, symbols_tab="F&O"):
    """
    Read column A of `symbols_tab` in `sheet_name`.
    Returns list like ['HAL.NS', 'WIPRO.NS', 'AAPL', 'NVDA', '^NSEI', ...]
    """
    import gspread
    try:
        book = _client().open(sheet_name)
        ws   = book.worksheet(symbols_tab)
        raw  = [str(s).strip().upper() for s in ws.col_values(1) if str(s).strip()]
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Sheet not found: '{sheet_name}'. Share it with the service account.")
        return []
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ Tab '{symbols_tab}' not found in '{sheet_name}'.")
        return []
    except Exception as e:
        print(f"⚠️ sheets.read_symbols failed: {e}")
        return []

    exchange = _detect_exchange(symbols_tab)

    out, seen = [], set()
    for s in raw:
        if s in _SKIP:
            continue
        sym = _add_suffix(s, exchange)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    print(f"📄 Read {len(out)} symbols from {sheet_name}::{symbols_tab} (exchange={exchange})")
    return out


def write_results(sheet_name, output_tab, headers, rows, summary=None):
    """Write rows + optional summary to `output_tab`."""
    import gspread
    try:
        book = _client().open(sheet_name)
    except Exception as e:
        print(f"⚠️ sheets.write_results: {e}")
        return False

    try:
        ws = book.worksheet(output_tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=output_tab, rows=max(200, len(rows) + 10),
                                cols=len(headers) + 5)

    ws.clear()
    start_row = 1
    if summary:
        ws.update("A1", [[summary]], value_input_option="USER_ENTERED")
        start_row = 2
    ws.update(f"A{start_row}", [headers] + rows, value_input_option="USER_ENTERED")
    print(f"📄 Wrote {len(rows)} rows to {sheet_name}::{output_tab}")
    return True


def service_account_email():
    try:
        import json
        with open(CREDENTIALS_FILE) as f:
            return json.load(f).get("client_email", "")
    except Exception:
        return ""


def list_tabs(sheet_name):
    """Return list of tab names inside a spreadsheet."""
    import gspread
    try:
        book = _client().open(sheet_name)
        return [ws.title for ws in book.worksheets()]
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Sheet not found: '{sheet_name}'")
        return []
    except Exception as e:
        print(f"⚠️ sheets.list_tabs failed: {e}")
        return []


def ensure_tab(sheet_name, tab_name, min_rows=200, min_cols=30):
    """Get or create a worksheet tab. Returns the worksheet or None."""
    import gspread
    try:
        book = _client().open(sheet_name)
    except Exception as e:
        print(f"⚠️ sheets.ensure_tab: {e}")
        return None
    try:
        return book.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return book.add_worksheet(title=tab_name, rows=min_rows, cols=min_cols)


def read_results(sheet_name, tab_name):
    """
    Read a tab written by write_results().
    Returns a list of dicts {header: value}.

    Handles:
      - an optional summary row above the header (write_results puts one there
        when summary=... is passed)
      - headers that may not start on row 1
      - trailing blank rows
    """
    import gspread
    try:
        book = _client().open(sheet_name)
        ws   = book.worksheet(tab_name)
        rows = ws.get_all_values()
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Sheet not found: '{sheet_name}'")
        return []
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ Tab '{tab_name}' not found in '{sheet_name}'")
        return []
    except Exception as e:
        print(f"⚠️ sheets.read_results failed: {e}")
        return []

    if not rows:
        return []

    # Header is the first row within the top few whose first cell says "Symbol"
    header_idx = None
    for i, r in enumerate(rows[:5]):
        if r and str(r[0]).strip().lower() == "symbol":
            header_idx = i
            break
    if header_idx is None:
        print(f"⚠️ No header row found in {sheet_name}::{tab_name}")
        return []

    headers = [str(h).strip() for h in rows[header_idx]]

    out = []
    for r in rows[header_idx + 1:]:
        if not r or not str(r[0]).strip():
            continue
        row = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            row[h] = r[i] if i < len(r) else ""
        out.append(row)
    return out


if __name__ == "__main__":
    print("\n🧪 sheets.py self-test\n" + "─" * 60)
    print(f"Credentials file : {CREDENTIALS_FILE}")
    print(f"Exists           : {CREDENTIALS_FILE.exists()}")
    print(f"Service account  : {service_account_email() or '(not found)'}")
    print(f"Tabs in Stock Price Scraper  : {list_tabs('Stock Price Scraper')}")
    print(f"Tabs in scanner output       : {list_tabs('scanner output')}")

    # quick exchange detection test
    print("\nExchange detection:")
    for tab in ["F&O", "zero_input", "ind_input", "US_input", "us_stocks", "BSE_input"]:
        print(f"  {tab:15s} → {_detect_exchange(tab)}")

    print("\nTo use a new sheet:")
    print("  1. Open the sheet in your browser")
    print("  2. Share it with the above email as Editor")
    print("  3. Add its name to the Book dropdown in the Scanner page")
    print("\n✅ sheets.py loaded.\n")