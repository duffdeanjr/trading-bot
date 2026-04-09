import threading

# ─────────────────────────────────────────────────────────────
# shared.py — single in-memory state hub
# All fields have a defined OWNER (the one agent that writes).
# Everyone else reads. Never acquire two locks simultaneously.
# ─────────────────────────────────────────────────────────────

# ── locks ─────────────────────────────────────────────────────
account_lock   = threading.Lock()   # guards: account, portfolio_history
positions_lock = threading.Lock()   # guards: positions
cache_lock     = threading.Lock()   # guards: all ref library caches + ticker_list
errors_lock    = threading.Lock()   # guards: AGENT_ERRORS (separate — never held with cache_lock)

# ── startup barriers ──────────────────────────────────────────
ref_ready_event    = threading.Event()  # OWNER: ref_library  — set in finally block
stream_ready_event = threading.Event()  # OWNER: stream.py    — set after all 5 streams live (NEW)
account_ready_event = threading.Event() # OWNER: account_agent — set after first successful poll

# ── market state (GIL-safe bool writes, no lock needed) ───────
MARKET_OPEN    = False  # OWNER: boss (written from GET /clock each cycle)
EXTENDED_HOURS = False  # OWNER: boss (NEW — True during pre/post market 4am-9:30am, 4pm-8pm ET)
RATE_LIMITED   = False  # OWNER: diagnostics (written on 429 detection, reset after backoff)
SHUTTING_DOWN  = False  # OWNER: main.py SIGTERM handler

# ── startup flags (written once, read-only after) ─────────────
ref_load_error = False  # OWNER: ref_library — set if any cache fails to load on startup

# ── account state — guarded by account_lock ───────────────────
account          = {}   # OWNER: account_agent (GET /account)
portfolio_history = {}  # OWNER: account_agent (GET /portfolio/history)

# ── position state — guarded by positions_lock ────────────────
positions = {}          # OWNER: account_agent (GET /positions + fill callbacks)

# ── reference caches — guarded by cache_lock ──────────────────
assets           = {}   # OWNER: ref_library (GET /assets)
calendar         = []   # OWNER: ref_library (GET /calendar)
corp_actions     = []   # OWNER: ref_library (GET /corporate_actions)
historical_ohlcv = {}   # OWNER: ref_library (_fetch_historical)
historical_news  = {}   # OWNER: ref_library (_fetch_news)

# ── watchlist — guarded by cache_lock ─────────────────────────
watchlist    = []       # OWNER: boss (resolved from settings or Alpaca API)
ticker_list  = []       # OWNER: signal_generator (= watchlist, kept for compat)
ticker_ts    = 0.0      # OWNER: signal_generator (time.time() of last screener run)

# ── investment plan — guarded by cache_lock ───────────────────
investment_plan = None  # OWNER: plan_manager (cached plan dict)

# ── dirty symbols — guarded by cache_lock (NEW) ───────────────
# account_agent writes symbol here when a corp action NTA event is detected.
# ref_library reads and re-fetches bars for that symbol, then clears the entry.
dirty_symbols = set()   # OWNER: account_agent writes, ref_library clears

# ── dashboard controls (GIL-safe bool writes, no lock needed) ─
# Written by dashboard.py POST handlers; read by order_execution.
trading_paused  = False  # OWNER: dashboard pause/resume endpoints
force_rebalance = False  # OWNER: dashboard force-rebalance endpoint; order_execution clears after acting

# ── agent health — guarded by errors_lock ─────────────────────
AGENT_ERRORS = {}       # OWNER: supervisor wrapper in main.py
                        # {agent_name: {"count": int, "last_error": str, "last_ts": float}}
