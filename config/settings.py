import os
import logging
from dotenv import load_dotenv

load_dotenv(override=True)

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

# ── options (decision: Level 3) ───────────────────────────────
OPTIONS_LEVEL    = 3     # 1 | 2 | 3 — must match Alpaca account approval
OPTIONS_ENABLED  = True  # master kill-switch for all options orders
EXPIRY_WARN_DAYS = 0     # allow 0DTE day trading

# ── options day trading ──────────────────────────────────────
OPTIONS_DAYTRADE      = True   # enable 0DTE / short-DTE day trading mode
OPTIONS_PROFIT_TARGET = 0.50   # close at 50% of max profit
OPTIONS_STOP_LOSS     = 2.0    # close at 2x collected premium
OPTIONS_EOD_EXIT_MINS = 15     # close all options positions N min before close
OPTIONS_WING_WIDTH    = 0.03   # 3% wing width for iron condors
OPTIONS_OTM_PCT       = 0.03   # 3% OTM for CSPs / covered calls

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
DAILY_TARGET_PCT     = 0.01    # 1% daily profit target — lock gains when reached
DAILY_TARGET_LOCK    = True    # stop opening new positions after hitting target

# ── per-plan risk defaults (overridden by plan dict values) ──
MIN_SIGNAL_CONFIDENCE = 0.0       # 0-1; signals below this dropped pre-plan
CONVICTION_CURVE      = "linear"  # "linear" | "exponential" | "sqrt"
VIX_CEILING           = None      # float or None; emergency liquidation trigger
CASH_FLOOR_PCT        = 0.10      # 0-1; cash target never drops below this

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

# ── screener timing ──────────────────────────────────────────
SCREENER_HISTORY_DAYS  = 30        # bars of history fetched per screener scan
SCREENER_MIN_TENURE_S  = 3600      # seconds a symbol must be on watchlist before demotion

# ── ensemble voting ──────────────────────────────────────────
ENSEMBLE_MIN_AGREEMENT  = 2        # strategies that must agree for bonus
ENSEMBLE_AGREEMENT_BONUS = 1.10    # confidence multiplier on agreement
ENSEMBLE_SOLO_PENALTY    = 0.90    # confidence multiplier for solo signal

# ── strategy scoring ─────────────────────────────────────────
SCORE_DECAY_HALFLIFE_DAYS = 7      # recency decay half-life in days

# ── regime-based strategy filtering ─────────────────────────
# Maps market regime string -> set of strategy names to allow (None = all)
REGIME_STRATEGY_MAP = {
    "risk-on":  None,              # all strategies enabled
    "risk-off": {"rsi_overbought", "rsi_oversold", "target_rebalance"},
    "neutral":  None,
    "unknown":  None,
}

# ── correlation-based position sizing ────────────────────────
CORRELATION_LOOKBACK_DAYS    = 30  # bars used to compute pairwise correlation
CORRELATION_THRESHOLD        = 0.70 # above this, apply position discount
CORRELATION_DISCOUNT_FACTOR  = 0.50 # multiply excess correlation by this factor
KELLY_CONVICTION_OVERRIDE    = 0.12 # skip correlation discount if Kelly >= 12%

# ── watchlist ────────────────────────────────────────────────
_wl_str = os.getenv("WATCHLIST", "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM")
WATCHLIST = [s.strip() for s in _wl_str.split(",") if s.strip()]
_crypto_str = os.getenv("CRYPTO_WATCHLIST", "BTC/USD,ETH/USD")
CRYPTO_WATCHLIST = [s.strip() for s in _crypto_str.split(",") if s.strip()]
WATCHLIST_ALPACA = os.getenv("WATCHLIST_ALPACA", "")  # optional Alpaca watchlist name
