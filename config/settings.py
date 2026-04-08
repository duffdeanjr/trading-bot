import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── credentials ───────────────────────────────────────────────
APCA_KEY    = os.getenv("APCA_API_KEY_ID", "")
APCA_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

if not APCA_KEY or not APCA_SECRET:
    raise EnvironmentError(
        "Missing Alpaca credentials. Create a .env file with:\n"
        "  APCA_API_KEY_ID=your_key\n"
        "  APCA_API_SECRET_KEY=your_secret"
    )

# ── environment ───────────────────────────────────────────────
IS_PAPER = os.getenv("IS_PAPER", "true").lower() == "true"
BASE_URL = (
    "https://paper-api.alpaca.markets" if IS_PAPER
    else "https://api.alpaca.markets"
)

# ── timing ────────────────────────────────────────────────────
TICK_INTERVAL   = 5     # seconds — all agent loop sleeps align to this
OVERNIGHT_SLEEP = 60    # seconds — sleep when market is closed
CANCEL_TIMEOUT  = 5     # seconds — graceful shutdown cancel-all deadline

# ── market data (decision: Basic = IEX feed) ──────────────────
DATA_FEED = "iex"       # "iex" (free) | "sip" (Algo Trader Plus, $99/mo)

# ── database (decision: SQLite) ───────────────────────────────
DB_PATH = "trading.db"  # swap to postgres:// URI to migrate later

# ── downloads ─────────────────────────────────────────────────
# Single folder for all data files fetched by ref_library.
# Subfolders: historical_bars/ | news/ | options/ | corporate_actions/
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "downloads")

# ── options (decision: Level 3) ───────────────────────────────
OPTIONS_LEVEL    = 3     # 1 | 2 | 3 — must match Alpaca account approval
OPTIONS_ENABLED  = True  # master kill-switch for all options orders
EXPIRY_WARN_DAYS = 2     # flag short options this many days before expiry

# ── execution (decision: no VWAP/TWAP) ───────────────────────
VWAP_TWAP = False        # requires Alpaca Elite Smart Router ($30k deposit)

# ── risk limits (NEW from diagnostic) ────────────────────────
MAX_POSITION_SIZE = 10000 # max notional per single order in USD
MAX_PORTFOLIO_PCT = 0.15  # max % of portfolio in any one symbol (15%)

# ── rebalancing ──────────────────────────────────────────────
REBALANCE_THRESHOLD = 0.03  # only rebalance when allocation gap > 3%

# ── resilience ────────────────────────────────────────────────
MAX_RETRIES       = 3    # order exec retry attempts on transient errors
BACKOFF_BASE      = 2    # exponential backoff multiplier (seconds)
REF_REFRESH_HOURS = 4    # how often ref library refreshes static caches

# ── logging ───────────────────────────────────────────────────
LOG_LEVEL = logging.INFO  # DEBUG | INFO | WARNING | ERROR

# ── validation ────────────────────────────────────────────────
assert OPTIONS_LEVEL in (1, 2, 3),      "OPTIONS_LEVEL must be 1, 2, or 3"
assert DATA_FEED    in ("iex", "sip"),  "DATA_FEED must be 'iex' or 'sip'"
assert 0 < MAX_PORTFOLIO_PCT <= 1.0,    "MAX_PORTFOLIO_PCT must be between 0 and 1"
assert MAX_POSITION_SIZE > 0,           "MAX_POSITION_SIZE must be positive"
assert 0 < REBALANCE_THRESHOLD < 1.0,  "REBALANCE_THRESHOLD must be between 0 and 1"

if VWAP_TWAP:
    raise EnvironmentError(
        "VWAP_TWAP=True requires Alpaca Elite Smart Router ($30k deposit). "
        "Set VWAP_TWAP=False or enrol at alpaca.markets/elite"
    )
