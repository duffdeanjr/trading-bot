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
OVERNIGHT_SLEEP = 300   # seconds — sleep when market is closed (5 min)
CANCEL_TIMEOUT  = 10    # seconds — graceful shutdown cancel-all deadline

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
MAX_POSITION_SIZE = 5000  # max notional per single order in USD
MAX_PORTFOLIO_PCT = 0.10  # max % of portfolio in any one symbol (10%)

# ── resilience ────────────────────────────────────────────────
MAX_RETRIES       = 3    # order exec retry attempts on transient errors
BACKOFF_BASE      = 2    # exponential backoff multiplier (seconds)
REF_REFRESH_HOURS = 24   # how often ref library refreshes static caches

# ── logging ───────────────────────────────────────────────────
LOG_LEVEL = logging.INFO  # DEBUG | INFO | WARNING | ERROR

# ── validation ────────────────────────────────────────────────
assert OPTIONS_LEVEL in (1, 2, 3),      "OPTIONS_LEVEL must be 1, 2, or 3"
assert DATA_FEED    in ("iex", "sip"),  "DATA_FEED must be 'iex' or 'sip'"
assert 0 < MAX_PORTFOLIO_PCT <= 1.0,    "MAX_PORTFOLIO_PCT must be between 0 and 1"
assert MAX_POSITION_SIZE > 0,           "MAX_POSITION_SIZE must be positive"

if VWAP_TWAP:
    raise EnvironmentError(
        "VWAP_TWAP=True requires Alpaca Elite Smart Router ($30k deposit). "
        "Set VWAP_TWAP=False or enrol at alpaca.markets/elite"
    )

# ── dry run ───────────────────────────────────────────────────
# When True, orders are logged but never submitted to Alpaca
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ── dashboard ─────────────────────────────────────────────────
DASHBOARD_PORT = 5050

# ── risk limits (extended) ────────────────────────────────────
MARGIN_MIN_EQUITY    = 25000   # minimum equity to use margin (PDT rule)
MAX_DAILY_LOSS_PCT   = 0.05    # circuit breaker: halt if down 5% in a day
MAX_CONSECUTIVE_LOSSES = 5     # circuit breaker: halt after N straight losses

# ── signal thresholds ────────────────────────────────────────
RSI_OVERSOLD  = 30.0
RSI_OVERBOUGHT = 70.0
REBALANCE_THRESHOLD = 0.03     # 3% gap triggers rebalance

# ── VIX regime thresholds ────────────────────────────────────
VIX_CAUTION = 20.0             # reduce position sizing
VIX_HIGH    = 30.0             # defensive posture
VIX_EXTREME = 40.0             # halt new positions

# ── portfolio heat ────────────────────────────────────────────
HEAT_WARN = 0.80               # warn at 80% of max heat
HEAT_MAX  = 1.00               # max heat = 100% deployed

# ── screener ─────────────────────────────────────────────────
SCREENER_ENABLED           = True
SCREENER_INTERVAL          = 3600   # seconds between screener runs
MAX_WATCHLIST_SIZE         = 50
SCREENER_PROMOTE_THRESHOLD = 0.65   # score to promote to watchlist
SCREENER_DEMOTE_THRESHOLD  = 0.35   # score to drop from watchlist
SCREENER_BATCH_SIZE        = 100    # symbols per batch when scanning universe
SCREENER_BATCHES_PER_CYCLE = 5      # batches per screener cycle

# ── data retention ───────────────────────────────────────────
RETENTION_DAYS = 90                 # purge data older than this from DB

# ── watchlist ────────────────────────────────────────────────
_wl_str = os.getenv("WATCHLIST", "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM")
WATCHLIST = [s.strip() for s in _wl_str.split(",") if s.strip()]
_crypto_str = os.getenv("CRYPTO_WATCHLIST", "BTC/USD,ETH/USD")
CRYPTO_WATCHLIST = [s.strip() for s in _crypto_str.split(",") if s.strip()]
WATCHLIST_ALPACA = os.getenv("WATCHLIST_ALPACA", "")  # optional Alpaca watchlist name
