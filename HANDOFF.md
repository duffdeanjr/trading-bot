# Trading Bot — System Handoff Summary
**Last updated:** 2026-04-10  
**Bot location:** `C:\Users\duffd\OneDrive\Desktop\Claude IO\trading-bot\`

---

## What This Is
An algorithmic paper trading bot built on the Alpaca Markets API. Runs continuously, generates signals, maintains an investment plan, and executes orders automatically. Currently running in **paper trading mode** (IS_PAPER=True).

---

## How to Run

**Start the bot:**
```
cd "C:\Users\duffd\OneDrive\Desktop\Claude IO\trading-bot"
python main.py > bot.log 2>&1
```

**Start the dashboard (separate terminal):**
```
cd "C:\Users\duffd\OneDrive\Desktop\Claude IO\trading-bot"
python dashboard.py
```
Then open http://localhost:5050 in browser.

**Dry run mode (no real orders):**
```
set DRY_RUN=true && python main.py
```

**Kill the bot:**
```
taskkill /f /im python.exe
```

**Read the log:**
```
type bot.log
```

**Run backtest:**
```
python -m agents.backtester --days 30 --capital 100000
python -m agents.backtester --days 30 --seed-scores   # also seeds strategy scores
```

**Check account status:**
```
python status.py
```

---

## Folder Structure
```
trading-bot/
├── main.py                  # Entry point, supervisor, startup sequence
├── shared.py                # In-memory state hub (all agents read/write here)
├── dashboard.py             # HTTP dashboard API server (port 5050) + Exec Suite endpoints
├── dashboard.html           # Dashboard frontend (dark UI, charts, Exec Suite sidebar)
├── status.py                # CLI account/position snapshot tool
├── trading.db               # SQLite database (WAL mode, 11 tables)
├── bot.log                  # Log output when running with > bot.log 2>&1
├── .env                     # API credentials (APCA_API_KEY_ID, APCA_API_SECRET_KEY)
├── env.example              # Template for .env
├── requirements.txt         # alpaca-py, python-dotenv, transformers, torch, anthropic
├── CLAUDE.md                # Claude Code guidance file
├── config/
│   └── settings.py          # All constants and configuration (env-var overridable)
├── storage/
│   ├── database.py          # SQLite schema (11 tables), all CRUD helpers
│   └── archiver.py          # Calls database.purge_old_data() for retention cleanup
├── alpaca_local/
│   ├── client.py            # Alpaca REST API wrapper (DRY_RUN support, crypto TIF fix)
│   └── stream.py            # 5 websocket streams (trade, stock, crypto, option, news)
├── agents/
│   ├── ref_library.py       # Fetches assets, OHLCV, news, corp actions → SQLite + shared cache
│   ├── account_agent.py     # Polls account, positions, fills, activities (NTA + corp actions)
│   ├── signal_generator.py  # RSI, MACD, Bollinger, EMA, options signals + feedback loop
│   ├── plan_manager.py      # Versioned investment plan, stance, targets, exclusions
│   ├── order_execution.py   # Rebalances toward plan targets, retry logic, pending dedup
│   ├── risk_manager.py      # Vetos orders: PDT, size, margin, heat, circuit breaker, options
│   ├── boss.py              # Market clock, extended hours, watchlist resolution
│   ├── diagnostics.py       # Stream health, agent crashes, rate limit management
│   ├── indicators.py        # Pure functions: RSI, MACD, ATR, VWAP, Bollinger, EMA
│   ├── sentiment.py         # FinBERT sentiment (ProsusAI/finbert) with keyword fallback
│   ├── iv_engine.py         # Black-Scholes IV solver, IVR/IVP, regime detection, options flow alerts
│   ├── options_strategies.py# Iron condor, covered call, CSP, calendar spread, auto-roll
│   ├── backtester.py        # Offline backtester + walk-forward daily auto-evaluation
│   ├── plan_reviewer.py     # Claude API plan review agent (hourly during market hours)
│   └── alpaca/              # OLD wrappers (not used — superseded by alpaca_local/)
│       ├── client.py
│       └── stream.py
```

---

## Key Settings (`config/settings.py`)
| Setting | Value | Notes |
|---------|-------|-------|
| IS_PAPER | True | Paper trading mode |
| DATA_FEED | iex | Free IEX feed |
| OPTIONS_LEVEL | 3 | Full options enabled |
| OPTIONS_ENABLED | True | Master kill-switch |
| DRY_RUN | False | Set true to log orders without submitting |
| MAX_POSITION_SIZE | $10,000 | Max notional per order |
| MAX_PORTFOLIO_PCT | 15% | Max per symbol |
| REBALANCE_THRESHOLD | 3% | Min gap to trigger rebalance |
| TICK_INTERVAL | 5s | Agent loop frequency |
| OVERNIGHT_SLEEP | 60s | Sleep when market closed |
| REF_REFRESH_HOURS | 4h | How often ref library refreshes |
| RETENTION_DAYS | 90 | SQLite data retention window |
| VIX_CAUTION / HIGH / EXTREME | 25 / 35 / 45 | VIX regime thresholds |
| HEAT_WARN / MAX | 60% / 80% | Portfolio heat thresholds |
| RSI_OVERSOLD / OVERBOUGHT | 35 / 70 | Signal thresholds |
| STREAMS_MINIMAL | False | Set true to skip crypto/option/news streams |
| DASHBOARD_PORT | 5050 | Dashboard HTTP port |
| WATCHLIST | AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM | Default equity watchlist |
| CRYPTO_WATCHLIST | BTC/USD,ETH/USD | Default crypto watchlist |

All settings are overridable via environment variables or `.env`.

---

## Database Schema (`trading.db`) — 11 Tables
| Table | Purpose |
|-------|---------|
| `trades` | Every fill: symbol, side, qty, price, notional, strategy_tag |
| `signals` | Every signal: symbol, strategy, side, confidence, sentiment |
| `positions` | Periodic snapshots: symbol, qty, avg_cost, market_val, unrealised |
| `agent_logs` | Errors and warnings with restart counts |
| `investment_plans` | Versioned plan JSON with stance, targets, exclusions |
| `historical_bars` | OHLCV bars (replaces downloads/historical_bars/) |
| `news` | News articles (replaces downloads/news/) |
| `market_calendar` | Market open/close dates |
| `corporate_actions` | Dividends, mergers, splits |
| `outcomes` | Trade entry/exit pairs with P&L (feedback loop) |
| `strategy_scores` | Rolling strategy performance: win_rate, sharpe, score |

---

## Architecture Notes

### Startup Sequence (main.py)
1. `config/settings.py` — loads `.env`, validates credentials and risk limits
2. `database.init_db()` + `GET /clock` — SQLite WAL init, sets `MARKET_OPEN`
3. Alpaca connection confirmed via `GET /account`
4. Initial watchlist set from `settings.WATCHLIST + CRYPTO_WATCHLIST`
5. `ref_library` starts → `ref_ready_event` fires (always, even on partial error)
6. `alpaca_local/stream.py` starts 5 streams → `stream_ready_event` fires
7. `account_agent`, `diagnostics`, `signal_generator`, `plan_manager` start → `account_ready_event` fires
8. `boss`, `risk_manager`, `order_execution`, `walk_forward`, `plan_reviewer` start — bot is live

### Data Flow
```
Alpaca WebSocket Streams (5)
        │
   Stream callbacks → shared.py (lock-guarded dicts)
        │
   Signal Generator (5s tick) ─── indicators.py (RSI, MACD, BB, EMA, ATR, VWAP)
        │                          iv_engine.py (Black-Scholes IV rank)
        │                          sentiment.py (news scoring)
        │                          strategy_scores (DB feedback loop)
        ▼
   Plan Manager (60s cooldown) ── stance: risk-on / risk-off / neutral
        │                          symbol targets with conviction + reason
        ▼
   Order Execution (5s tick) ──── builds orders (market, limit, bracket, OCO, mleg)
        │                          deduplicates via pending set
        │                          DRY_RUN mode logs without submitting
        ▼
   Risk Manager veto ───────────── PDT, position size, margin, options level,
        │                          portfolio heat, circuit breaker, naked short check
        ▼
   Alpaca REST API → fill callback → outcomes table → strategy scorer
```

### Self-Learning Feedback Loop
```
signals → orders → fills → outcomes table (entry/exit pairs with P&L)
                                 │
                           strategy_scores table (win_rate, sharpe, score)
                                 │
                           signal confidence × score_multiplier (0.5x–1.5x)
                           plan conviction × score_multiplier
```

### Concurrency Model
4 lock groups — **never hold two simultaneously**:
- `account_lock` — guards `account`, `portfolio_history`
- `positions_lock` — guards `positions`
- `cache_lock` — guards `assets`, `calendar`, `corp_actions`, `historical_ohlcv`, `historical_news`, `watchlist`, `ticker_list`, `dirty_symbols`, `investment_plan`
- `errors_lock` — guards `AGENT_ERRORS`

GIL-safe booleans (no lock needed): `MARKET_OPEN`, `EXTENDED_HOURS`, `SHUTTING_DOWN`, `RATE_LIMITED`

### Key Design Decisions
- **SQLite as single source of truth** — no JSON files; all data in `trading.db` (WAL mode)
- **No VWAP/TWAP** — requires Alpaca Elite Smart Router ($30k deposit)
- **IEX feed** — free; upgrade to SIP ($99/mo) for full market data
- **DRY_RUN mode** — set `DRY_RUN=true` to log orders without submitting (safe testing)
- **STREAMS_MINIMAL** — set `STREAMS_MINIMAL=true` to run only trade+stock streams (reduces connections)
- **Crypto uses GTC** — time_in_force auto-set to GTC for crypto pairs, DAY for equities

---

## Dashboard (`dashboard.py` + `dashboard.html`)
- Runs on port 5050 (configurable via `DASHBOARD_PORT`)
- Single `GET /api/data` endpoint returns account, positions, trades, signals, plan, errors, agent status
- **Exec Suite sidebar** with 9 POST endpoints:
  - `/api/exec/pause` / `/api/exec/resume` — CEO pause/resume trading
  - `/api/exec/emergency-stop` — halt everything + cancel all orders
  - `/api/exec/cancel-all` — cancel orders without stopping bot
  - `/api/exec/force-rebalance` — trigger immediate rebalance
  - `/api/exec/stance` — update market stance
  - `/api/exec/risk-limits` — override MAX_POSITION_SIZE / MAX_PORTFOLIO_PCT at runtime
  - `/api/exec/plan` — CIO update investment plan targets
  - `/api/exec/plan-rollback` — roll back to previous plan version
- Enriches data with live `shared.py` state when bot is running (HAS_BOT flag)

---

## Risk Manager (`agents/risk_manager.py`)
Checks run before every order via `approve(signal)`:
1. Circuit breaker — daily loss > 5% or 5 consecutive losses halts trading
2. Position size — notional > MAX_POSITION_SIZE or > MAX_PORTFOLIO_PCT
3. PDT — pattern day trader 3-trade rolling window
4. DTMC — daytrade buying power zero check
5. Margin minimum equity check
6. Options level check (strategy vs OPTIONS_LEVEL)
7. Naked short call block
8. Options expiry proximity block (within EXPIRY_WARN_DAYS)
9. Portfolio heat / VIX regime gate
10. Stop loss advisory (logs warning if no stop_price on buy)

---

## Recent Upgrades (2026-04-10)

### 1. FinBERT Sentiment (`agents/sentiment.py`)
- Replaced keyword-weighted scoring with ProsusAI/finbert HuggingFace model
- Model loads once at module import; falls back to keyword scoring if transformers/torch unavailable
- Same public API: `score_text()`, `score_headline()`, `score_news_events()` all return floats in [-1, 1]
- New dependencies: `transformers>=4.30.0`, `torch>=2.0.0`

### 2. Options Flow Signal (`agents/iv_engine.py` + `agents/signal_generator.py`)
- `iv_engine.detect_unusual_flow(symbols)` scans option chains for:
  - Put/call ratio >2 standard deviations from 20-day average
  - Single-trade notional exceeding $500k
- Writes alerts to `shared.options_flow_alerts` (guarded by `cache_lock`)
- `signal_generator.py` boosts conviction by 0.15 for signals aligned with flow direction

### 3. Kelly Criterion Sizing (`agents/plan_manager.py`)
- `_kelly_size(strategy, conviction)` replaces linear `conf * 0.10` normalization
- Formula: `f = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win`
- Uses half-Kelly (f * 0.5) scaled by conviction for safety
- Falls back to equal weighting if strategy has <10 trades in `strategy_scores` table
- Capped at `settings.MAX_PORTFOLIO_PCT`

### 4. Regime-Aware Plan Switching (`agents/plan_manager.py` + `config/settings.py`)
- `_detect_market_regime()` classifies from VIX level + SPY 20-day momentum:
  - `trending-bull` (SPY momentum >3%), `trending-bear` (<-3%), `ranging`, `high-vol` (VIX >= VIX_HIGH)
- `REGIME_STRATEGY_MAP` in `config/settings.py` maps each regime to active strategy tags
- `signal_generator.py` skips strategies not in the active list for the current regime
- Regime stored in `shared.market_regime` (guarded by `cache_lock`)

### 5. Walk-Forward Auto-Disable (`agents/backtester.py`)
- `walk_forward_evaluate()` runs a 30-day rolling evaluation on each strategy tag
- Computes Sharpe, win_rate from the `outcomes` table, writes to `strategy_scores`
- Auto-disables strategy (score=0) if rolling Sharpe < -0.5 for 3 consecutive evaluations
- `walk_forward_loop()` runs as a daemon thread, evaluating once per day
- Wired into `main.py` as `walk_forward` thread

### 6. Claude API Plan Review (`agents/plan_reviewer.py`)
- New agent running as daemon thread, hourly during market hours
- Assembles JSON summary: current plan targets, last 20 signals, open positions, today's P&L, regime
- Calls Claude API (`claude-sonnet-4-20250514`) with quantitative risk reviewer system prompt
- Expects structured JSON response: `{issues: [], suggestions: [], confidence: 0-1}`
- Writes response to `shared.plan_review`, logs to `agent_logs`
- Requires `ANTHROPIC_API_KEY` env var; gracefully skips if not set
- New dependency: `anthropic>=0.30.0`

### VIX Live Feed (`agents/ref_library.py`)
- `_fetch_vix()` fetches VIXY ETF daily bars and approximates VIX
- Calls `risk_manager.update_vix()` on each full load and scheduled refresh
- Fixes Known Issue #5 (VIX was defaulting to 18.0)

### Dashboard Updates (`dashboard.py` + `dashboard.html`)
- API now returns `market_regime`, `flow_alerts`, `plan_review`, `heat_status`
- Agent status list includes `walk_forward` and `plan_reviewer`
- **New header badge**: Market regime indicator (color-coded)
- **New Dashboard row**: 3-card section with Market Regime, Options Flow Alerts, AI Plan Review
- **Alert banner**: Now shows options flow alerts (P/C deviation, large notional)
- **Risk gauges**: Added size multiplier gauge
- **Investment plan**: Shows per-symbol conviction, Kelly-sized target %, strategy tag, and regime in badge
- **Org chart**: Added Backtester and AI Review agent nodes (row 3)

### New `shared.py` Fields (all guarded by `cache_lock`)
| Field | Owner | Purpose |
|-------|-------|---------|
| `options_flow_alerts` | iv_engine | Unusual options activity alerts |
| `plan_review` | plan_reviewer | Latest Claude API review response |
| `market_regime` | plan_manager | Current detected regime |

---

## Known Issues / Next Steps
1. **`alpaca/` folder** — there are two client/stream implementations: `alpaca/` (old, unused) and `alpaca_local/` (current). The `alpaca/` folder can be deleted to reduce confusion.

2. **Options stream 405 error** — subscribing to `*` wildcard for options quotes hits IEX symbol limit. Non-critical; set `STREAMS_MINIMAL=true` to skip option stream entirely.

3. **Stream heartbeat warnings** — `trade` and `news` streams show silent warnings after ~60s of inactivity. Normal when no fills/news arrive. Not a real error.

4. ~~**Sentiment is keyword-based**~~ — **RESOLVED**: Now uses ProsusAI/finbert with keyword fallback.

5. ~~**No VIX data feed**~~ — **RESOLVED**: `ref_library._fetch_vix()` now fetches VIXY ETF bars and calls `risk_manager.update_vix()` on each full load/refresh cycle.

6. **`shared.trading_paused`** — referenced in `dashboard.py` exec handlers but not declared in `shared.py`. Add `trading_paused = False` to `shared.py` if using the dashboard pause feature.

7. **`shared.force_rebalance`** — set by dashboard's force-rebalance endpoint but not read by `order_execution.py`. Wire it up in `_execute_toward_targets()` if needed.

8. **PAXG/USD** — bot may buy crypto on first run if it appears in the investment plan. Review plan targets or run with `DRY_RUN=true` first.

---

## How to Continue in a New Chat
1. Upload the files you want to work on (or add them to the Project)
2. Paste or reference this HANDOFF document
3. Reference specific agents or bugs by name — everything is documented above

**Files most commonly needed:**
- `main.py`, `shared.py`, `config/settings.py` — architecture/startup issues
- `agents/signal_generator.py`, `agents/indicators.py` — signal logic
- `agents/plan_manager.py`, `agents/order_execution.py` — trade execution
- `agents/risk_manager.py` — risk rules
- `storage/database.py` — schema or query issues
- `dashboard.py`, `dashboard.html` — dashboard/UI issues
