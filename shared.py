import threading
import time

# ─────────────────────────────────────────────────────────────
# shared.py — single in-memory state hub (HARDENED)
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
stream_ready_event = threading.Event()  # OWNER: stream.py    — set after all 5 streams live
account_ready_event = threading.Event() # OWNER: account_agent — set after first successful poll

# ── market state (GIL-safe bool writes, no lock needed) ───────
MARKET_OPEN    = False  # OWNER: boss (written from GET /clock each cycle)
EXTENDED_HOURS = False  # OWNER: boss (True during pre/post market)
RATE_LIMITED   = False  # OWNER: diagnostics (written on 429 detection, reset after backoff)
SHUTTING_DOWN  = False  # OWNER: main.py SIGTERM handler

# ── dashboard control flags (GIL-safe bools) ──────────────────
trading_paused  = False  # OWNER: dashboard exec_pause/exec_resume
force_rebalance = False  # OWNER: dashboard exec_force_rebalance, cleared by order_execution

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

# ── dirty symbols — guarded by cache_lock ─────────────────────
# account_agent writes symbol here when a corp action NTA event is detected.
# ref_library reads and re-fetches bars for that symbol, then clears the entry.
dirty_symbols = set()   # OWNER: account_agent writes, ref_library clears

# ── agent health — guarded by errors_lock ─────────────────────
AGENT_ERRORS = {}       # OWNER: supervisor wrapper in main.py
                        # {agent_name: {"count": int, "last_error": str, "last_ts": float}}

# ── agent heartbeats (NEW — lock-free via GIL) ────────────────
# Each agent writes time.time() every tick. diagnostics checks for staleness.
# No lock needed: dict value assignments are GIL-atomic in CPython.
AGENT_HEARTBEATS = {}   # {agent_name: float(timestamp)}

# ─────────────────────────────────────────────────────────────
# SNAPSHOT HELPERS — safe read-only copies for agents
# Agents should use these instead of touching locks directly.
# Each acquires exactly one lock, copies, releases.
# ─────────────────────────────────────────────────────────────

def get_account_snapshot() -> dict:
    """Return a shallow copy of the account dict. Safe to read without lock."""
    with account_lock:
        acct = account
    # Return the object as-is (Alpaca SDK objects are read-only).
    # If it's a dict, return a copy.
    if isinstance(acct, dict):
        return dict(acct)
    return acct


def get_positions_snapshot() -> dict:
    """Return {symbol: position_obj} copy. Safe to iterate without lock."""
    with positions_lock:
        return dict(positions)


def get_portfolio_history_snapshot():
    """Return portfolio_history. Safe to read without lock."""
    with account_lock:
        return portfolio_history


def get_plan_snapshot() -> dict:
    """Return a copy of the current investment plan. Safe to read without lock."""
    with cache_lock:
        plan = investment_plan
    if plan is None:
        return {}
    if isinstance(plan, dict):
        # Shallow copy is fine — plan values are immutable strings/numbers/lists
        return dict(plan)
    return plan


def get_watchlist_snapshot() -> list:
    """Return a copy of the watchlist. Safe to iterate without lock."""
    with cache_lock:
        return list(watchlist)


def get_ohlcv_snapshot(symbol: str):
    """Return OHLCV data for one symbol. Safe to read without lock."""
    with cache_lock:
        return historical_ohlcv.get(symbol, {})


def get_dirty_symbols_snapshot() -> set:
    """Return a copy of dirty_symbols. Safe to iterate without lock."""
    with cache_lock:
        return set(dirty_symbols)


def get_agent_errors_snapshot() -> dict:
    """Return a copy of AGENT_ERRORS. Safe to read without lock."""
    with errors_lock:
        return dict(AGENT_ERRORS)


# ─────────────────────────────────────────────────────────────
# HEARTBEAT HELPER — agents call this every tick
# ─────────────────────────────────────────────────────────────

def heartbeat(agent_name: str):
    """Record that an agent is alive. Call once per tick in each agent's main loop."""
    AGENT_HEARTBEATS[agent_name] = time.time()


def get_stale_agents(timeout_s: float = 60.0) -> list:
    """Return list of agent names that haven't heartbeated within timeout_s.
    Called by diagnostics."""
    now = time.time()
    stale = []
    for name, ts in AGENT_HEARTBEATS.items():
        if (now - ts) > timeout_s:
            stale.append((name, now - ts))
    return stale
