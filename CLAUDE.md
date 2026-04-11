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

Multi-threaded, event-driven trading bot built on the Alpaca Markets API (`alpaca-py`). Runs 13 daemon threads + 5 WebSocket streams, coordinated through a shared state hub (`shared.py`) and a SQLite database (`trading.db`, WAL mode, 18 tables).

### Startup Sequence (main.py)

1. **Config** (`config/settings.py`) -- loads `.env`, validates Alpaca creds and risk limits
2. **Database + Clock** -- SQLite WAL init, `GET /clock` sets `MARKET_OPEN`
3. **Alpaca connection** -- confirmed via `GET /account`
4. **Initial watchlist** -- from `settings.WATCHLIST + CRYPTO_WATCHLIST`
5. **Ref Library** -- fetches assets, calendar, corp actions, historical OHLCV, news -> SQLite + shared cache
6. **Streams** -- 5 WebSocket connections (trade updates, stock bars, crypto, options, news)
7. **Account Agent + Signal Generator + Diagnostics + Plan Manager + Screener** -- polling loops start
8. **Boss + Risk Manager + Order Execution + Walk Forward + Plan Reviewer + Strategy Factory + Bandit Harvester** -- bot is live

Each agent runs in a daemon thread with a supervisor wrapper (`_supervised()`) that restarts on crash with exponential backoff (`min(2^n, 300)` seconds).

### Data Flow

```
Alpaca WebSocket Streams (5)
        |
   Stream callbacks write to shared.py (lock-guarded dicts)
        |
   Signal Generator (5s tick) --- indicators.py (RSI, MACD, BB, EMA, ATR, VWAP)
        |                         iv_engine.py (Black-Scholes IV rank)
        |                         sentiment.py (FinBERT news scoring)
        |                         strategy_scores (recency-weighted DB feedback loop)
        |                         strategy_factory (live factory signals merged in)
        |
   Ensemble Voting -------------- 2+ agree: avg conviction × 1.2 bonus
        |                         1 solo: conviction × 0.7 penalty
        |                         bull+bear conflict: suppress entirely
        v
   Plan Manager (60s cooldown) -- stance: risk-on / risk-off / neutral
        |                         Kelly sizing -> bandit multipliers -> correlation discount
        v
   Plan Reviewer (hourly) ------- enriched context -> Claude API -> auto-apply
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
                                   -> bandit reward harvester -> LinUCB update
```

### Self-Learning Feedback Loops

```
signals -> orders -> fills -> outcomes table (entry/exit pairs with P&L)
                                    |
                              strategy_scores table (recency-weighted win_rate, sharpe, score)
                                    |
                              signal confidence * score_multiplier (0.5x to 1.5x)

plan_reviewer suggestions -> plan_review_outcomes (was_applied, pnl_1h, pnl_4h)
                                    |
                              self-modifying system prompt (avg P&L per action type)

factory strategies -> shadow_signals -> shadow eval (simulated P&L)
                                    |
                              strategy_recipes promotion/retirement

bandit decisions -> 2h reward window -> outcomes match
                                    |
                              LinUCB A/b update -> multiplier adjusts Kelly weights
```

### Storage: SQLite as Single Source of Truth

All data lives in `trading.db` (WAL mode). No JSON files. 18 tables.

| Table | Purpose |
|-------|---------|
| `trades` | Executed fills |
| `signals` | Emitted signals with confidence + sentiment |
| `positions` | Position snapshots |
| `agent_logs` | Error/warning logs |
| `investment_plans` | Versioned plans (JSON) |
| `historical_bars` | OHLCV bars |
| `news` | News articles |
| `market_calendar` | Market open/close dates |
| `corporate_actions` | Dividends, mergers, splits |
| `option_chains` | Option chain snapshots: strike, expiry, greeks, IV |
| `outcomes` | Trade entry/exit pairs with P&L + `market_context` JSON |
| `strategy_scores` | Rolling strategy performance scores |
| `screener_scores` | Screener audit trail |
| `plan_review_outcomes` | Plan reviewer suggestion outcomes (pnl_1h, pnl_4h) |
| `strategy_recipes` | Composable strategies (candidate/shadow/live/retired) |
| `shadow_signals` | Shadow strategy paper signals |
| `bandit_state` | LinUCB per-strategy A/b matrices |
| `bandit_decisions` | Bandit multiplier decisions for reward harvesting |

### Concurrency Model

State lives in `shared.py` with 4 lock groups -- **never hold two locks simultaneously**:
- `account_lock` -- guards `account`, `portfolio_history`
- `positions_lock` -- guards `positions`
- `cache_lock` -- guards `assets`, `calendar`, `corp_actions`, `historical_ohlcv`, `news`, `watchlist`, `ticker_list`, `dirty_symbols`, `investment_plan`, `screener_candidates`, `options_flow_alerts`, `plan_review`, `market_regime`
- `errors_lock` -- guards `AGENT_ERRORS`

GIL-safe booleans (`MARKET_OPEN`, `EXTENDED_HOURS`, `SHUTTING_DOWN`, `RATE_LIMITED`) are written without locks.

### Key Modules

| Module | Role |
|--------|------|
| `shared.py` | Thread-safe state hub (locks, events, caches) |
| `alpaca_local/client.py` | REST wrapper -- order builders, account methods, DRY_RUN |
| `alpaca_local/stream.py` | 5 WebSocket streams with reconnect, callback registry |
| `agents/signal_generator.py` | Technical analysis + sentiment + ensemble voting + factory signals -> buy/sell |
| `agents/plan_manager.py` | Signals -> Kelly sizing -> bandit multipliers -> correlation discount -> versioned plan |
| `agents/order_execution.py` | Submits orders, retry logic, pending dedup |
| `agents/risk_manager.py` | `approve()` veto + portfolio heat + VIX regime |
| `agents/account_agent.py` | Polls account/positions, detects corp actions, tracks outcomes + market_context |
| `agents/ref_library.py` | Fetches/caches data -> SQLite + shared cache, 4-hour refresh, VIX feed |
| `agents/boss.py` | Market timing via `GET /clock`, watchlist resolution |
| `agents/diagnostics.py` | Stream health, agent crashes, rate limit management |
| `agents/screener.py` | Universe scanner, promotes/demotes watchlist candidates |
| `agents/backtester.py` | Offline backtester + walk-forward daily auto-evaluation |
| `agents/plan_reviewer.py` | Claude API Loop 3 reviewer (auto-apply, self-modifying prompt) |
| `agents/strategy_factory.py` | Composable strategy factory with 4-state promotion lifecycle |
| `agents/bandit.py` | LinUCB contextual bandit for adaptive strategy weighting |
| `storage/database.py` | SQLite schema (18 tables), all CRUD helpers |

### Asset Classes

- **US Equities** -- IEX feed (free) or SIP, via StockDataStream
- **Crypto** -- BTC/USD, ETH/USD via CryptoDataStream
- **US Options** -- Level 3 multi-leg strategies when `OPTIONS_ENABLED=True`

### Configuration (config/settings.py)

Key tunables (all env-var overridable):
- `WATCHLIST` -- comma-separated symbols to trade
- `DRY_RUN` -- log orders without submitting
- `TICK_INTERVAL=5`, `MAX_POSITION_SIZE=10000`, `MAX_PORTFOLIO_PCT=0.10`
- `RSI_OVERSOLD=35`, `RSI_OVERBOUGHT=70`, `RISK_FREE_RATE=0.05`
- `VIX_CAUTION=25`, `VIX_HIGH=35`, `HEAT_MAX=0.70`
- `OPTIONS_LEVEL=3`, `DATA_FEED="iex"`, `RETENTION_DAYS=90`
- `SCORE_DECAY_HALFLIFE_DAYS=7`, `CORRELATION_THRESHOLD=0.70`
- `ENSEMBLE_MIN_AGREEMENT=2`, `ENSEMBLE_AGREEMENT_BONUS=1.2`, `ENSEMBLE_SOLO_PENALTY=0.7`
- `DASHBOARD_PORT=5050`

### Graceful Shutdown

SIGTERM/SIGINT sets `SHUTTING_DOWN=True`, cancels all open orders, waits up to 5s for fill callbacks to drain, then joins threads.

### Pipeline Order

```
signals → ensemble voting → Kelly sizing → bandit multipliers → correlation discount → plan targets → order execution
```
