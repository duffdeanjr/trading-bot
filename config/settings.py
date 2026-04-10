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

# ── watchlist ─────────────────────────────────────────────────
WATCHLIST        = [s.strip() for s in os.getenv("WATCHLIST", "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM").split(",") if s.strip()]
WATCHLIST_ALPACA = os.getenv("WATCHLIST_ALPACA", "")  # Alpaca watchlist name to fetch
CRYPTO_WATCHLIST = [s.strip() for s in os.getenv("CRYPTO_WATCHLIST", "BTC/USD,ETH/USD").split(",") if s.strip()]

# ── dry run ───────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# ── streams ──────────────────────────────────────────────────
STREAMS_MINIMAL = os.getenv("STREAMS_MINIMAL", "false").lower() in ("true", "1", "yes")

# ── retention ─────────────────────────────────────────────────
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))

# ── options (decision: Level 3) ───────────────────────────────
OPTIONS_LEVEL    = 3     # 1 | 2 | 3 — must match Alpaca account approval
OPTIONS_ENABLED  = True  # master kill-switch for all options orders
EXPIRY_WARN_DAYS = 2     # flag short options this many days before expiry

# ── execution (decision: no VWAP/TWAP) ───────────────────────
VWAP_TWAP = False        # requires Alpaca Elite Smart Router ($30k deposit)

# ── risk limits ───────────────────────────────────────────────
MAX_POSITION_SIZE   = float(os.getenv("MAX_POSITION_SIZE", "10000"))
MAX_PORTFOLIO_PCT   = float(os.getenv("MAX_PORTFOLIO_PCT", "0.15"))
MARGIN_MIN_EQUITY   = float(os.getenv("MARGIN_MIN_EQUITY", "2000"))

# ── circuit breaker ───────────────────────────────────────────
MAX_DAILY_LOSS_PCT      = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
MAX_CONSECUTIVE_LOSSES  = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))

# ── signal thresholds ────────────────────────────────────────
RSI_OVERSOLD    = float(os.getenv("RSI_OVERSOLD", "35"))
RSI_OVERBOUGHT  = float(os.getenv("RSI_OVERBOUGHT", "70"))
RISK_FREE_RATE  = float(os.getenv("RISK_FREE_RATE", "0.05"))

# ── portfolio heat / VIX ─────────────────────────────────────
VIX_NORMAL  = float(os.getenv("VIX_NORMAL", "20"))
VIX_CAUTION = float(os.getenv("VIX_CAUTION", "25"))
VIX_HIGH    = float(os.getenv("VIX_HIGH", "35"))
VIX_EXTREME = float(os.getenv("VIX_EXTREME", "45"))
HEAT_MAX    = float(os.getenv("HEAT_MAX", "0.80"))
HEAT_WARN   = float(os.getenv("HEAT_WARN", "0.60"))

# ── dashboard ────────────────────────────────────────────────
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5050"))

# ── rebalancing ──────────────────────────────────────────────
REBALANCE_THRESHOLD = 0.01  # only rebalance when allocation gap > 1%

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
