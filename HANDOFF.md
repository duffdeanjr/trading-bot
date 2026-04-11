# Trading Bot — System Handoff Summary
**Last updated:** 2026-04-10 (v2 — bandit added)  
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
├── trading.db               # SQLite database (WAL mode, 18 tables)
├── bot.log                  # Log output when running with > bot.log 2>&1
├── .env                     # API credentials (APCA_API_KEY_ID, APCA_API_SECRET_KEY)
├── env.example              # Template for .env
├── requirements.txt         # alpaca-py, python-dotenv, transformers, torch, anthropic
├── CLAUDE.md                # Claude Code guidance file
├── config/
│   └── settings.py          # All constants and configuration (env-var overridable)
├── storage/
│   ├── database.py          # SQLite schema (18 tables), all CRUD helpers
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
│   ├── screener.py          # Universe scanner, promotes/demotes watchlist candidates
│   ├── backtester.py        # Offline backtester + walk-forward daily auto-evaluation
│   ├── plan_reviewer.py     # Claude API Loop 3 reviewer (auto-apply, self-modifying prompt)
│   ├── strategy_factory.py  # Composable strategy factory with 4-state promotion lifecycle
│   ├── bandit.py            # LinUCB contextual bandit for adaptive strategy weighting
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
| MAX_PORTFOLIO_PCT | 10% | Max per symbol |
| REBALANCE_THRESHOLD | 1% | Min gap to trigger rebalance |
| TICK_INTERVAL | 5s | Agent loop frequency |
| OVERNIGHT_SLEEP | 60s | Sleep when market closed |
| REF_REFRESH_HOURS | 4h | How often ref library refreshes |
| RETENTION_DAYS | 90 | SQLite data retention window |
| VIX_CAUTION / HIGH / EXTREME | 25 / 35 / 45 | VIX regime thresholds |
| HEAT_WARN / MAX | 60% / 70% | Portfolio heat thresholds |
| RSI_OVERSOLD / OVERBOUGHT | 35 / 70 | Signal thresholds |
| SCORE_DECAY_HALFLIFE_DAYS | 7 | Recency weight halflife for strategy scores |
| CORRELATION_LOOKBACK_DAYS | 30 | Days of closes for correlation matrix |
| CORRELATION_THRESHOLD | 0.70 | Correlation level that triggers discount |
| CORRELATION_DISCOUNT_FACTOR | 1.5 | Multiplier for soft weight reduction |
| KELLY_CONVICTION_OVERRIDE | 0.12 | Skip correlation discount above this weight |
| SCREENER_ENABLED | True | Enable/disable the screener agent |
| SCREENER_INTERVAL | 900s | Seconds between screener scan cycles |
| SCREENER_PROMOTE_THRESHOLD | 0.40 | Min score to promote a candidate |
| MAX_WATCHLIST_SIZE | 40 | Cap on total watchlist symbols |
| ENSEMBLE_MIN_AGREEMENT | 2 | Min strategies agreeing to get agreement bonus |
| ENSEMBLE_AGREEMENT_BONUS | 1.2 | Conviction multiplier when strategies agree |
| ENSEMBLE_SOLO_PENALTY | 0.7 | Conviction multiplier for lone signals |
| STREAMS_MINIMAL | False | Set true to skip crypto/option/news streams |
| DASHBOARD_PORT | 5050 | Dashboard HTTP port |
| WATCHLIST | AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM | Default equity watchlist |
| CRYPTO_WATCHLIST | BTC/USD,ETH/USD | Default crypto watchlist |

All settings are overridable via environment variables or `.env`.

---

## Database Schema (`trading.db`) — 17 Tables
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
| `option_chains` | Option chain snapshots: strike, expiry, greeks, IV, bid/ask |
| `outcomes` | Trade entry/exit pairs with P&L + `market_context` JSON (feedback loop / bandit pre-instrumentation) |
| `strategy_scores` | Rolling strategy performance: win_rate, sharpe, score |
| `screener_scores` | Screener audit trail: symbol, score, reasons, promoted flag |
| `plan_review_outcomes` | Tracks every plan reviewer suggestion: action, symbol, value, confidence, was_applied, pnl_1h, pnl_4h |
| `strategy_recipes` | Composable strategies: entry×filter×exit, status (candidate/shadow/live/retired), hypothesis, backtest/shadow metrics |
| `shadow_signals` | Shadow strategy paper signals: strategy_id, symbol, side, conviction, entry/exit price, pnl |
| `bandit_state` | LinUCB per-strategy A matrix, b vector, alpha; persisted across restarts |
| `bandit_decisions` | Every bandit multiplier computation: ts, strategy_id, context_vector, multiplier, shadow_mode, reward |

---

## Architecture Notes

### Startup Sequence (main.py)
1. `config/settings.py` — loads `.env`, validates credentials and risk limits
2. `database.init_db()` + `GET /clock` — SQLite WAL init, sets `MARKET_OPEN`
3. Alpaca connection confirmed via `GET /account`
4. Initial watchlist set from `settings.WATCHLIST + CRYPTO_WATCHLIST`
5. `ref_library` starts → `ref_ready_event` fires (always, even on partial error)
6. `alpaca_local/stream.py` starts 5 streams → `stream_ready_event` fires
7. `account_agent`, `diagnostics`, `signal_generator`, `plan_manager`, `screener` start → `account_ready_event` fires
8. `boss`, `risk_manager`, `order_execution`, `walk_forward`, `plan_reviewer`, `strategy_factory`, `bandit_harvester` start — bot is live

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
        │                          strategy_factory (live factory signals merged in)
        │
   Ensemble Voting ─────────────── 2+ agree: avg conviction × 1.2 bonus
        │                          1 solo: conviction × 0.7 penalty
        │                          bull+bear conflict: suppress entirely
        ▼
   Plan Manager (60s cooldown) ── stance: risk-on / risk-off / neutral
        │                          symbol targets with conviction + reason
        │                          Kelly sizing → bandit multipliers → correlation discount
        ▼
   Plan Reviewer (hourly) ──────── enriched context → Claude API → structured actions
        │                          auto-apply if confidence ≥ 0.75 AND overall ≥ 0.70
        │                          outcomes tracked → self-modifying prompt
        ▼
   Order Execution (5s tick) ──── builds orders (market, limit, bracket, OCO, mleg)
        │                          deduplicates via pending set
        │                          DRY_RUN mode logs without submitting
        ▼
   Risk Manager veto ───────────── PDT, position size, margin, options level,
        │                          portfolio heat, circuit breaker, naked short check
        ▼
   Alpaca REST API → fill callback → outcomes table → strategy scorer

Strategy Factory (background) ──  candidate → shadow → live → retired
        │                          daily promotions, 4h shadow eval, weekly hypotheses
        │                          live signals → signal_generator
        │                          shadow signals → shadow_signals table only
```

### Self-Learning Feedback Loop
```
signals → orders → fills → outcomes table (entry/exit pairs with P&L)
                                 │
                           strategy_scores table (recency-weighted win_rate, sharpe, score)
                                 │
                           signal confidence × score_multiplier (0.5x–1.5x)
                           plan conviction × score_multiplier

plan_reviewer suggestions → plan_review_outcomes (was_applied, pnl_1h, pnl_4h)
                                 │
                           self-modifying system prompt (avg P&L per action type)

factory strategies → shadow_signals → shadow eval (simulated P&L)
                                 │
                           strategy_recipes promotion/retirement

bandit decisions → 2h reward window → outcomes table match
                                 │
                           LinUCB A/b update → multiplier adjusts Kelly weights
                           alpha decays 0.3 → 0.1 over 200+ observations
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

### 6. Claude API Plan Review — Loop 3 (`agents/plan_reviewer.py`)
Full rewrite with 5 sub-systems:

**6a. Enriched Context Snapshot** — `_build_review_payload()` now assembles:
  - Current plan targets with conviction, strategy tags
  - Last 20 signals from DB
  - Open positions with unrealised P&L
  - Today's realised P&L
  - Sector exposure breakdown (GICS from assets cache)
  - Options flow alerts with direction and magnitude
  - Macro calendar (next 5 trading days from `market_calendar` table)
  - Correlation hotspots (pairs >0.75 both in plan, via `_build_correlation_matrix`)
  - VIX level, market regime
  - Strategy scores

**6b. Structured Action Types** — System prompt enforces exact action types:
  - `adjust_target`: modify a symbol's plan weight (value = multiplier, e.g. 0.6 = reduce 40%)
  - `add_symbol`: add a new symbol to the plan (value = target weight 0.01–0.15)
  - `remove_symbol`: remove a symbol from the plan (value = null)
  - `change_stance`: change overall stance (value = "risk-on" | "risk-off" | "neutral")
  - `exclude_symbol`: exclude symbol for 24h (value = null)
  - Every reason must cite specific metric, actual value, and actual threshold
  - Response schema: `{issues, suggestions, overall_confidence, plan_quality_score}`

**6c. Confidence-Gated Auto-Apply** — `_auto_apply_suggestions()`:
  - Gate: suggestion `confidence >= 0.75` AND `overall_confidence >= 0.70`
  - `_apply_action()` modifies `shared.investment_plan` directly under `cache_lock`
  - Every suggestion (applied or skipped) is logged to `plan_review_outcomes` table

**6d. Outcome Tracking** — `_evaluate_outcomes()` runs every 4 hours:
  - Finds unevaluated outcomes older than 4h
  - Computes `pnl_1h` from trades table (net notional in 1h window)
  - Computes `pnl_4h` from trades + unrealised delta from position snapshots
  - Writes back to `plan_review_outcomes` table

**6e. Self-Modifying System Prompt** — `_build_system_prompt()`:
  - Queries `plan_review_outcomes` for evaluated rows
  - If >= 10 evaluated rows exist: computes avg `pnl_4h` per action type
  - Appends performance summary to base system prompt: "Historical performance of your past suggestions: adjust_target: avg 4h P&L +$12.50 (15 samples), ..."
  - Instructs Claude to weight suggestions toward action types with positive historical P&L
  - Below 10 samples: uses base prompt only (insufficient data guard)

- Calls Claude API (`claude-sonnet-4-20250514`), hourly during market hours
- Requires `ANTHROPIC_API_KEY` env var; gracefully skips if not set
- New dependency: `anthropic>=0.30.0`

### VIX Live Feed (`agents/ref_library.py`)
- `_fetch_vix()` fetches VIXY ETF daily bars and approximates VIX (VIXY price × 1.2 ≈ VIX)
- Calls `risk_manager.update_vix()` on each full load and scheduled refresh
- Fixes Known Issue #5 (VIX was defaulting to 18.0)

### Screener (`agents/screener.py`)
- Daemon thread that scans the full Alpaca asset universe for new trading candidates
- Builds a filtered universe (~4800 symbols from ~32000 total assets) — US equities, active, tradeable
- Scans in batches of `SCREENER_BATCH_SIZE` (15) symbols per API call, `SCREENER_BATCHES_PER_CYCLE` (4) batches per cycle
- Scores each symbol by: RSI proximity to oversold, Bollinger Band %B, ATR volatility, volume
- Promotes symbols scoring above `SCREENER_PROMOTE_THRESHOLD` (0.40) into `shared.screener_candidates`
- Demotes symbols below `SCREENER_DEMOTE_THRESHOLD` (0.20) after `SCREENER_MIN_TENURE_S` (30 min)
- Respects `MAX_WATCHLIST_SIZE` (40) cap
- State stored in `shared.py`: `screener_candidates`, `screener_demotions`, `screener_last_run`
- Writes audit trail to `screener_scores` table in SQLite
- Controlled by `SCREENER_ENABLED` setting (default: true)
- Runs every `SCREENER_INTERVAL` (900s / 15 min)

### Dashboard Updates (`dashboard.py` + `dashboard.html`)
- API now returns `market_regime`, `flow_alerts`, `plan_review`, `heat_status`
- Agent status list includes `walk_forward` and `plan_reviewer`
- **New header badge**: Market regime indicator (color-coded)
- **New Dashboard row**: 3-card section with Market Regime, Options Flow Alerts, AI Plan Review
- **Alert banner**: Now shows options flow alerts (P/C deviation, large notional)
- **Risk gauges**: Added size multiplier gauge
- **Investment plan**: Shows per-symbol conviction, Kelly-sized target %, strategy tag, and regime in badge
- **Org chart**: Added Backtester and AI Review agent nodes (row 3)

### 7. Recency-Weighted Strategy Scoring (`agents/signal_generator.py`)
- `_refresh_strategy_scores()` now applies exponential recency weighting to closed outcomes
- Weight tiers based on `SCORE_DECAY_HALFLIFE_DAYS` (default 7):
  - 0–7 days old: weight 1.0
  - 7–14 days: weight 0.7
  - 14–21 days: weight 0.4
  - Older: weight 0.2
- Weighted win rate: sum of weights for winning trades / total weight (not simple count ratio)
- Weighted average P&L: weight-adjusted mean return
- Weighted Sharpe: uses weighted variance for standard deviation
- Recent trades now dominate strategy scores — a strategy that was profitable last week but poor last month scores higher than simple averages would show
- New setting: `SCORE_DECAY_HALFLIFE_DAYS = 7` in `config/settings.py`

### 8. Correlation-Aware Sizing (`agents/plan_manager.py`)
- `_build_correlation_matrix(symbols)` pulls 30-day daily closes from `shared.historical_ohlcv`, computes pairwise Pearson correlations via numpy. Falls back to 90-day lookback if 30-day data is sparse (<10 days).
- `_apply_correlation_discount(plan)` runs after Kelly sizing in `update_plan()`:
  - For each pair where correlation > `CORRELATION_THRESHOLD` (0.70), discounts the lower-conviction symbol's target_pct by `(corr - threshold) * CORRELATION_DISCOUNT_FACTOR * target_pct`
  - Conviction override: symbols with raw Kelly weight >= `KELLY_CONVICTION_OVERRIDE` (0.12) are never discounted
  - Floor: discounted weight never drops below 0.02 (2%)
  - Logs at DEBUG: `plan_manager: correlation discount {sym} {orig} -> {disc} (corr={c} with {other})`
- New settings in `config/settings.py`:
  - `CORRELATION_LOOKBACK_DAYS = 30`
  - `CORRELATION_THRESHOLD = 0.70`
  - `CORRELATION_DISCOUNT_FACTOR = 1.5`
  - `KELLY_CONVICTION_OVERRIDE = 0.12`

### 9. Context Logging for Future Bandit Training (`storage/database.py` + `agents/account_agent.py`)
- New column `market_context` (JSON text) added to the `outcomes` table
- On trade entry (via `account_agent._on_fill`), a JSON snapshot is written:
  ```json
  {
    "regime": "trending-bull",
    "vix_level": 22.5,
    "spy_momentum_20d": 0.034,
    "signal_mix": {"rsi_oversold": 0.72, "macd_cross": 0.61},
    "kelly_weight": 0.065,
    "correlation_discount_applied": false
  }
  ```
- This is pre-instrumentation for a future contextual bandit — written on every entry, not read anywhere yet
- Existing databases are auto-migrated via `ALTER TABLE outcomes ADD COLUMN market_context TEXT` in `init_db()`

### 10. Strategy Factory (`agents/strategy_factory.py`)
Composable strategy engine that generates, evaluates, and promotes algorithmic strategies automatically.

**10a. Composable Primitives** — Three primitive types combine via Cartesian product:
  - **10 Entry primitives**: `rsi_oversold`, `rsi_overbought`, `macd_crossover_bull`, `macd_crossover_bear`, `bollinger_lower_touch`, `bollinger_upper_touch`, `ema_trend_up`, `ema_trend_down`, `vwap_below`, `vwap_above`
  - **7 Filter primitives**: `vix_low`, `vix_high`, `regime_trending`, `regime_ranging`, `volume_above_avg`, `options_flow_bullish`, `options_flow_bearish`
  - **5 Exit primitives**: `rsi_recovery`, `macd_cross_reverse`, `stop_loss_pct` (-3%), `take_profit_pct` (+5%), `time_exit_eod` (3:45 PM)
  - Total combinations: 10 × 7 × 5 = 350 candidate strategies
  - Each evaluated via `check_entry()`, `check_filter()`, `check_exit()` functions

**10b. Four-State Promotion Lifecycle**:
  ```
  candidate ──→ shadow ──→ live ──→ retired
     ↑                                 │
     └────── manual reset only ────────┘
  ```
  - **candidate → shadow**: backtest Sharpe > 0.5, win_rate > 45%, min 20 trades (60-day backtest on 10 watchlist symbols)
  - **shadow → live**: 14+ days in shadow, shadow Sharpe > 0.6, shadow win_rate > 48%, min 10 signals
  - **live → retired**: rolling Sharpe < -0.5 for 3 consecutive daily evaluations
  - **retired → candidate**: `reset_strategy(recipe_id, reason)` — **manual only**, deliberate safety gate. A strategy that failed live should not re-enter without human review.

**10c. Shadow Signal Evaluator** — `_evaluate_shadow_signals()` runs every 4 hours:
  - Finds unevaluated shadow signals older than 4h
  - Computes simulated P&L: `(exit_price - entry_price) / entry_price` using latest close from `historical_ohlcv`
  - Updates `shadow_signals` table with `exit_price` and `pnl`
  - `get_shadow_signal_stats(strategy_id)` aggregates: count, win_rate, sharpe — used for promotion decisions

**10d. Claude Hypothesis Generator** — `_generate_hypotheses()` runs weekly:
  - Calls Claude API (`claude-sonnet-4-20250514`) with strategy researcher system prompt
  - Context includes: available primitives list, top 5 live strategies by Sharpe, bottom 3 retired strategies with failure reasons, current market regime
  - Claude proposes novel entry×filter×exit combinations with:
    - Specific market microstructure hypothesis (why it should work)
    - Target regime
    - Custom params
  - Insertion rules: only primitives that exist in the valid sets are accepted; duplicates rejected via `recipe_id` check
  - Inserted as `candidate` status — must still pass backtest to promote

**10e. Signal Generation** — `generate_factory_signals(symbol, indicators, context, close)`:
  - Returns `(live_signals, shadow_signals)` tuple
  - Live signals: merged into `signal_generator`'s `all_signals` list, go through ensemble voting → plan_manager
  - Shadow signals: written to `shadow_signals` table only, never reach plan_manager
  - Strategies cache refreshed daily via `refresh_strategies()`

**Factory daemon thread** (`run()`):
  - Generates all 350 candidate combinations on first start
  - Daily: `promote_candidates()` → `promote_shadows()` → `evaluate_live_strategies()` → `refresh_strategies()`
  - Every 4h: `_evaluate_shadow_signals()`
  - Weekly: `_generate_hypotheses()` via Claude API

### 11. Ensemble Voting (`agents/signal_generator.py`)
Aggregation layer applied to all signals (hand-coded + factory) before they reach plan_manager.

- `_apply_ensemble_voting(signals)` groups signals by symbol, then:
  - **Agreement** (2+ strategies same direction): emit composite signal with average conviction × `ENSEMBLE_AGREEMENT_BONUS` (1.2)
  - **Solo** (exactly 1 strategy): emit at original conviction × `ENSEMBLE_SOLO_PENALTY` (0.7)
  - **Conflict** (bull + bear signals for same symbol): suppress entirely with DEBUG log
- Composite signals carry `ensemble` metadata: `"agreement_2"`, `"agreement_3"`, or `"solo"`
- `ensemble_strategies` field preserves the list of all contributing strategy names
- Settings:
  - `ENSEMBLE_MIN_AGREEMENT = 2` — minimum strategies that must agree for bonus
  - `ENSEMBLE_AGREEMENT_BONUS = 1.2` — conviction multiplier on agreement
  - `ENSEMBLE_SOLO_PENALTY = 0.7` — conviction multiplier for lone signals

### 12. Contextual Bandit — LinUCB (`agents/bandit.py`)
Adaptive strategy weighting using a Linear Upper Confidence Bound bandit. Learns which strategies work best in which market contexts and adjusts position sizing multipliers accordingly.

**Architecture:**
- Per-strategy LinUCB arm with A matrix (15×15) and b vector (15,)
- 15-feature context vector built from live system state every tick
- Multiplier output: 0.3× to 2.0× applied to raw Kelly-sized targets
- State persisted to `bandit_state` SQLite table, survives restarts

**15-Feature Context Vector** (`build_context_vector(signals)`):
  | # | Feature | Normalization |
  |---|---------|--------------|
  | 0 | VIX level | / 80, cap at 80 |
  | 1 | SPY 20-day momentum | % return, clipped [-1, 1] |
  | 2 | Regime: trending-bull | one-hot (0 or 1) |
  | 3 | Regime: trending-bear | one-hot |
  | 4 | Regime: ranging | one-hot |
  | 5 | Regime: high-vol | one-hot |
  | 6 | Time of day | (hour - 9.5) / 6.5 |
  | 7 | Day of week | weekday / 4.0 |
  | 8 | Portfolio heat | total_mv / equity, [0, 1] |
  | 9 | Mean signal conviction | avg of batch, default 0.5 |
  | 10 | Bullish flow alerts | count / 10, cap at 10 |
  | 11 | Bearish flow alerts | count / 10, cap at 10 |
  | 12 | Max pairwise correlation | from correlation matrix, [0, 1] |
  | 13 | Active live strategies | count / 50, cap at 50 |
  | 14 | Today P&L % of portfolio | clipped [-0.1, 0.1] |

**Shadow Mode** — first 30 days OR until 200 total observations:
  - Multipliers are computed and logged to `bandit_decisions` table
  - But all strategies get neutral 1.0 multiplier (no real effect)
  - `shadow_start_ts` persisted in `bandit_state` meta row

**Cold Start** — strategies with < 10 observations:
  - Always return 1.0 multiplier regardless of shadow/live mode
  - Prevents wild swings from insufficient data

**Alpha Decay** — exploration → exploitation transition:
  - Starts at α = 0.3 (high exploration)
  - After 200+ total observations, decays by 0.995× per harvester run
  - Floors at α = 0.1 (always some exploration)

**Reward Computation** (`compute_reward()`):
  - Queries `outcomes` table for trades matching strategy_id within 2 hours of decision
  - Computes normalized return: `pnl / |notional|` per trade, averages across matches
  - Clipped to [-1.0, 1.0] to prevent outlier distortion
  - Returns None if no matching closed trades exist yet

**Reward Harvester** (`BanditRewardHarvester.run()`):
  - Background thread, runs every 10 minutes
  - Finds unevaluated decisions older than 2 hours in `bandit_decisions`
  - Calls `compute_reward()`, then `linucb.update()` if reward available
  - Persists model immediately on live-mode updates
  - Stale decisions (>24h with no trades) marked evaluated with reward=0

**Pipeline Position:**
  ```
  signals → ensemble voting → Kelly sizing → bandit multipliers → correlation discount → plan targets → order execution
  ```

**Dashboard Endpoint** — `GET /api/bandit`:
  ```json
  {
    "mode": "shadow | live",
    "total_observations": 0,
    "alpha": 0.3,
    "days_until_live": 14,
    "top_strategies": [
      {"id": "rsi_mean_reversion", "multiplier": 1.4, "observations": 45, "avg_reward": 0.023}
    ],
    "bottom_strategies": [...],
    "recent_decisions": [
      {"ts": 0.0, "strategy_id": "...", "multiplier": 1.2, "reward": 0.018, "shadow": false}
    ]
  }
  ```

### New `shared.py` Fields (all guarded by `cache_lock`)
| Field | Owner | Purpose |
|-------|-------|---------|
| `options_flow_alerts` | iv_engine | Unusual options activity alerts |
| `plan_review` | plan_reviewer | Latest Claude API review response |
| `market_regime` | plan_manager | Current detected regime |
| `screener_candidates` | screener | Dict of promoted symbols with scores |
| `screener_demotions` | screener | List of recently demoted symbols |
| `screener_last_run` | screener | Timestamp of last completed scan |

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
- `agents/signal_generator.py`, `agents/indicators.py` — signal logic + ensemble voting
- `agents/plan_manager.py`, `agents/order_execution.py` — trade execution + Kelly/bandit/correlation pipeline
- `agents/risk_manager.py` — risk rules
- `agents/bandit.py` — LinUCB contextual bandit (context vector, model, reward harvester)
- `agents/strategy_factory.py` — composable strategy lifecycle (candidate/shadow/live/retired)
- `agents/plan_reviewer.py` — Claude API Loop 3 reviewer (auto-apply, self-modifying prompt)
- `storage/database.py` — schema (18 tables) or query issues
- `dashboard.py`, `dashboard.html` — dashboard/UI issues
