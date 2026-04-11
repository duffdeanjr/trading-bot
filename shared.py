import threading
import time
import queue
from collections import defaultdict

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

# ── screener state — guarded by cache_lock ────────────────────
screener_candidates = {}   # OWNER: screener  {symbol: {"score": float, "ts": float, "reasons": str}}
screener_demotions  = []   # OWNER: screener  [symbol, ...]
screener_last_run   = 0.0  # OWNER: screener  (timestamp of last completed scan)

# ── options flow alerts — guarded by cache_lock ────────────────
options_flow_alerts = []  # OWNER: iv_engine.detect_unusual_flow()

# ── plan review — guarded by cache_lock ────────────────────────
plan_review = {}  # OWNER: plan_reviewer agent

# ── market regime — guarded by cache_lock ──────────────────────
market_regime = "unknown"  # OWNER: plan_manager (trending-bull, trending-bear, ranging, high-vol)

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


def get_stale_agents(timeout_s: float = 120.0) -> list:
    """Return list of agent names that haven't heartbeated within timeout_s.
    Called by diagnostics."""
    now = time.time()
    stale = []
    for name, ts in AGENT_HEARTBEATS.items():
        if (now - ts) > timeout_s:
            stale.append((name, now - ts))
    return stale


# ─────────────────────────────────────────────────────────────
# AGENT REGISTRY — decorator-based auto-discovery
# Agents decorate their run() with @shared.register_agent("name", phase=N).
# main.py iterates the registry by phase to launch threads.
# ─────────────────────────────────────────────────────────────

_AGENT_REGISTRY = {}  # name -> {"fn": callable, "phase": int, "condition": callable|None}


def register_agent(name, phase=7, condition=None):
    """Decorator for agent run() functions.
    phase: startup phase (4=ref_library, 6=account/signal/diag, 7=boss/risk/exec)
    condition: callable returning bool, or None for always-start
    """
    def decorator(fn):
        _AGENT_REGISTRY[name] = {"fn": fn, "phase": phase, "condition": condition}
        return fn
    return decorator


def get_registered_agents():
    """Return a copy of the agent registry for introspection."""
    return dict(_AGENT_REGISTRY)


# ─────────────────────────────────────────────────────────────
# EVENT BUS — lightweight pub/sub via queue.Queue
# Agents can publish("signal_emitted", {...}) and others
# subscribe("signal_emitted") to get a Queue they poll.
# Opt-in — no existing code needs to change.
# ─────────────────────────────────────────────────────────────

_event_subscribers = defaultdict(list)  # event_name -> [queue.Queue, ...]
_event_lock = threading.Lock()


def subscribe(event_name: str) -> queue.Queue:
    """Subscribe to an event. Returns a Queue that receives payloads."""
    q = queue.Queue(maxsize=100)
    with _event_lock:
        _event_subscribers[event_name].append(q)
    return q


def publish(event_name: str, payload: dict):
    """Publish an event to all subscribers. Non-blocking; drops oldest on overflow."""
    with _event_lock:
        subs = list(_event_subscribers.get(event_name, []))
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            try:
                q.get_nowait()  # drop oldest
                q.put_nowait(payload)
            except Exception:
                pass
