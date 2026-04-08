# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
pip install -r requirements.txt
# Create .env from env.example with Alpaca credentials
python main.py
```

Dry run mode (no real orders):
```bash
DRY_RUN=true python main.py
```

There is no formal test suite, linter, or build step. Syntax-check with:
```bash
python -m py_compile main.py
python -m py_compile agents/signal_generator.py  # etc.
```

## Architecture

Multi-threaded, event-driven trading bot built on the Alpaca Markets API (`alpaca-py`). Runs 6 daemon threads + 5 WebSocket streams, coordinated through a shared state hub (`shared.py`) and a SQLite database (`trading.db`, WAL mode).

### Startup Sequence (main.py)

1. **Config** (`config/settings.py`) -- loads `.env`, validates Alpaca creds and risk limits
2. **Database + Clock** -- SQLite WAL init, `GET /clock` sets `MARKET_OPEN`
3. **Boss** -- resolves watchlist from settings/Alpaca API, sets `shared.watchlist`
4. **Ref Library** -- fetches assets, calendar, corp actions, historical OHLCV, news -> SQLite + shared cache
5. **Streams** -- 5 WebSocket connections (trade updates, stock bars, crypto, options, news)
6. **Account Agent + Signal Generator + Diagnostics** -- polling loops start
7. **Risk Manager + Order Execution** -- market timing, order submission, risk enforcement

Each agent runs in a daemon thread with a supervisor wrapper (`_supervised()`) that restarts on crash with exponential backoff (`min(2^n, 300)` seconds).

### Data Flow

```
Alpaca WebSocket Streams (5)
        |
   Stream callbacks write to shared.py (lock-guarded dicts)
        |
   Signal Generator (5s tick) --- indicators.py (RSI, MACD, BB, EMA, ATR, VWAP)
        |                         iv_engine.py (Black-Scholes IV rank)
        |                         sentiment.py (news scoring)
        |                         strategy_scores (from DB feedback loop)
        v
   Plan Manager (60s cooldown) -- stance: risk-on / risk-off / neutral
        |                         symbol targets with conviction + reason
        |                         conviction adjusted by strategy scores
        v
   Order Execution (5s tick) ---- builds orders (market, limit, bracket, OCO, multi-leg)
        |                         deduplicates via pending set
        |                         DRY_RUN mode logs without submitting
        v
   Risk Manager veto ------------ PDT, position size, margin, options level, portfolio heat
        |
   Alpaca REST API (submit order)
        |
   Fill callback -> outcomes table -> strategy scorer -> adjusted signal confidence
```

### Self-Learning Feedback Loop

```
signals -> orders -> fills -> outcomes table (entry/exit pairs)
                                    |
                              strategy_scores table (win_rate, sharpe, score)
                                    |
                              signal confidence * score_multiplier (0.5x to 1.5x)
                              plan conviction * score_multiplier
```

### Storage: SQLite as Single Source of Truth

All data lives in `trading.db` (WAL mode). No JSON files.

| Table | Purpose |
|-------|---------|
| `trades` | Executed fills |
| `signals` | Emitted signals with confidence + sentiment |
| `positions` | Position snapshots |
| `agent_logs` | Error/warning logs |
| `investment_plans` | Versioned plans (JSON) |
| `historical_bars` | OHLCV bars (replaces downloads/historical_bars/) |
| `news` | News articles (replaces downloads/news/) |
| `market_calendar` | Market open/close dates |
| `corporate_actions` | Dividends, mergers, splits |
| `outcomes` | Trade entry/exit pairs with P&L |
| `strategy_scores` | Rolling strategy performance scores |

### Concurrency Model

State lives in `shared.py` with 4 lock groups -- **never hold two locks simultaneously**:
- `account_lock` -- guards `account`, `portfolio_history`
- `positions_lock` -- guards `positions`
- `cache_lock` -- guards `assets`, `calendar`, `corp_actions`, `historical_ohlcv`, `news`, `watchlist`, `ticker_list`, `dirty_symbols`, `investment_plan`
- `errors_lock` -- guards `AGENT_ERRORS`

GIL-safe booleans (`MARKET_OPEN`, `EXTENDED_HOURS`, `SHUTTING_DOWN`, `RATE_LIMITED`) are written without locks.

### Key Modules

| Module | Role |
|--------|------|
| `shared.py` | Thread-safe state hub (locks, events, caches) |
| `alpaca_local/client.py` | REST wrapper -- order builders, account methods, DRY_RUN |
| `alpaca_local/stream.py` | 5 WebSocket streams with reconnect, callback registry |
| `agents/signal_generator.py` | Technical analysis + sentiment + strategy scores -> buy/sell signals |
| `agents/plan_manager.py` | Converts signals to versioned investment plan (DB-persisted JSON) |
| `agents/order_execution.py` | Submits orders, retry logic, pending dedup |
| `agents/risk_manager.py` | `approve()` veto + portfolio heat + VIX regime (merged from portfolio_heat) |
| `agents/account_agent.py` | Polls account/positions, detects corp actions, tracks outcomes |
| `agents/ref_library.py` | Fetches/caches data -> SQLite + shared cache, 4-hour refresh |
| `agents/boss.py` | Market timing via `GET /clock`, watchlist resolution |
| `agents/diagnostics.py` | Stream health, agent crashes, rate limit management |
| `storage/database.py` | SQLite schema (11 tables), all CRUD helpers |

### Asset Classes

- **US Equities** -- IEX feed (free) or SIP, via StockDataStream
- **Crypto** -- BTC/USD, ETH/USD via CryptoDataStream
- **US Options** -- Level 3 multi-leg strategies when `OPTIONS_ENABLED=True`

### Configuration (config/settings.py)

Key tunables (all env-var overridable):
- `WATCHLIST` -- comma-separated symbols to trade
- `DRY_RUN` -- log orders without submitting
- `TICK_INTERVAL=5`, `MAX_POSITION_SIZE=10000`, `MAX_PORTFOLIO_PCT=0.15`
- `RSI_OVERSOLD=35`, `RSI_OVERBOUGHT=70`, `RISK_FREE_RATE=0.05`
- `VIX_CAUTION=25`, `VIX_HIGH=35`, `HEAT_MAX=0.80`
- `OPTIONS_LEVEL=3`, `DATA_FEED="iex"`, `RETENTION_DAYS=90`
- `DASHBOARD_PORT=5050`

### Graceful Shutdown

SIGTERM/SIGINT sets `SHUTTING_DOWN=True`, cancels all open orders, waits up to 5s for fill callbacks to drain, then joins threads.
