"""
Global constants. Things that never change at runtime.

What goes here:
    • paths (credentials, data folders)
    • timezone
    • yfinance limits (which period works with which interval)
    • tunables that are shared by every run

What does NOT go here:
    • per-indicator defaults (T3 owns its own length, WT owns its own n1…)
    • per-run settings (timeframe, symbols, sheet names) — those go in
      a preset file under presets/ (created later) or are passed by the UI.
"""
from pathlib import Path
import pytz


# ---- Paths ---------------------------------------------------------------
WEB_DIR      = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent                       # "mahadev core output/"
CREDENTIALS_FILE = PROJECT_ROOT / "Json" / "automation-project-429417-c51140fdff86.json"


# ---- Time ----------------------------------------------------------------
IST = pytz.timezone("Asia/Kolkata")


# ---- yfinance limits -----------------------------------------------------
# Yahoo restricts which period is available for each interval.
YF_CONFIG = {
    "5m":  {"default_period": "5d",  "allowed": ["1d", "5d", "1mo", "60d"]},
    "15m": {"default_period": "1mo", "allowed": ["1d", "5d", "1mo", "60d"]},
    "30m": {"default_period": "1mo", "allowed": ["1d", "5d", "1mo", "60d"]},
    "1h":  {"default_period": "6mo", "allowed": ["5d", "1mo", "3mo", "6mo", "1y", "2y"]},
    "1d":  {"default_period": "2y",  "allowed": ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"]},
    "1wk": {"default_period": "5y",  "allowed": ["1y", "2y", "5y", "10y", "max"]},
}


# ---- F&O filter ----------------------------------------------------------
# For intraday timeframes, drop candles at/after 15:15 IST for F&O symbols.
FNO_INTRADAY_TFS = {"5m", "15m", "30m", "1h"}
FNO_CUTOFF_HOUR  = 15
FNO_CUTOFF_MIN   = 15


# ---- Defaults used when a caller forgets to say --------------------------
DEFAULT_TIMEFRAME = "1h"
DEFAULT_PERIOD    = "6mo"