# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
pip install -r requirements.txt
# Create .env from env.example with Alpaca credentials
python main.py
```

There is no formal test suite, linter, or build step. Syntax-check with:
```bash
python -m py_compile main.py
python -m py_compile agents/signal_generator.py  # etc.
```

## Architecture

Multi-threaded, event-driven trading bot built on the Alpaca Markets API (`alpaca-py`). Runs 7+ daemon threads coordinated through a shared state hub (`shared.py`) and a SQLite database (`trading.db`, WAL mode).

### Startup Sequence (main.py)

1. **Config** (`config/settings.py`) — loads `.env`, validates Alpaca creds and risk limits
2. **Database + Clock** — SQLite WAL init, `GET /clock` sets `MARKET_OPEN`
3. **Ref Library** — fetches assets, calendar, corp actions, historical OHLCV, news into `downloads/`
4. **Streams** — 5 WebSocket connections (trade updates, stock bars, crypto, options, news)
5. **Account Agent + Signal Generator + Diagnostics** — polling loops start
6. **Boss + Risk Manager + Order Execution** — market timing, order submission, risk enforcement

Each agent runs in a daemon thread with a supervisor wrapper (`_supervised()`) that restarts on crash with exponential backoff (`min(2^n, 300)` seconds).

### Data Flow

```
Alpaca WebSocket Streams (5)
        │
   Stream callbacks write to shared.py (lock-guarded dicts)
        │
   Signal Generator (5s tick) ─── indicators.py (RSI, MACD, BB, EMA, ATR, VWAP)
        │                         iv_engine.py (Black-Scholes IV rank)
        │                         sentiment.py (news scoring)
        ▼
   Plan Manager (60s cooldown) ── stance: risk-on / risk-off / neutral
        │                         symbol targets with conviction + reason
        ▼
   Order Execution (5s tick) ──── builds orders (market, limit, bracket, OCO, multi-leg)
        │                         deduplicates via pending set
        ▼
   Risk Manager veto ──────────── PDT, position size, margin, options level, portfolio heat
        │
   Alpaca REST API (submit order)
```

### Concurrency Model

State lives in `shared.py` with 4 lock groups — **never hold two locks simultaneously**:
- `account_lock` — guards `account`, `portfolio_history`
- `positions_lock` — guards `positions` (written by account_agent + fill callbacks)
- `cache_lock` — guards `assets`, `calendar`, `corp_actions`, `historical_ohlcv`, `news`, `ticker_list`, `dirty_symbols`
- `errors_lock` — guards `AGENT_ERRORS`

GIL-safe booleans (`MARKET_OPEN`, `EXTENDED_HOURS`, `SHUTTING_DOWN`, `RATE_LIMITED`) are written without locks.

### Key Modules

| Module | Role |
|--------|------|
| `shared.py` | Thread-safe state hub (locks, events, caches) |
| `alpaca_local/client.py` | REST wrapper — order builders, account methods |
| `alpaca_local/stream.py` | 5 WebSocket streams, callback registry |
| `agents/signal_generator.py` | Technical analysis + sentiment → buy/sell signals |
| `agents/plan_manager.py` | Converts signals to versioned investment plan (DB-persisted JSON) |
| `agents/order_execution.py` | Submits orders, retry logic, pending dedup |
| `agents/risk_manager.py` | `approve()` veto function on every order |
| `agents/account_agent.py` | Polls account/positions, detects corp actions → dirty symbols |
| `agents/ref_library.py` | Fetches/caches static data (assets, calendar, news, OHLCV), 4-hour refresh |
| `agents/boss.py` | Market timing via `GET /clock`, watchlist fetching |
| `storage/database.py` | SQLite schema (5 tables: trades, signals, positions, agent_logs, investment_plans) |

### Asset Classes

- **US Equities** — IEX feed (free) or SIP, via StockDataStream
- **Crypto** — BTC/USD, ETH/USD, PAXG/USD via CryptoDataStream
- **US Options** — Level 3 multi-leg strategies (iron condor, covered call, CSP, calendar spread) when `OPTIONS_ENABLED=True`

### Signal Strategies

Equity: RSI oversold/overbought, MACD cross, Bollinger band bounce — all require sentiment confirmation.
Crypto: Momentum (close vs open threshold) + sentiment.
Options: IV-rank-based strategy selection (IVR ≥ 50 → iron condor, ≥ 35 → covered call, < 30 → calendar spread, 30-50 → CSP).

### Configuration (config/settings.py)

Key tunables: `TICK_INTERVAL=5` (agent loop seconds), `MAX_POSITION_SIZE=10000` (USD), `MAX_PORTFOLIO_PCT=0.15`, `REBALANCE_THRESHOLD=0.03`, `REF_REFRESH_HOURS=4`, `OPTIONS_LEVEL=3`, `DATA_FEED="iex"`.

### Graceful Shutdown

SIGTERM/SIGINT sets `SHUTTING_DOWN=True`, cancels all open orders, waits up to 5s for fill callbacks to drain, then joins threads.
