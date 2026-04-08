# Trading Bot — System Handoff Summary
**Last updated:** 2026-04-08  
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

**Kill the bot:**
```
taskkill /f /im python.exe
```

**Read the log:**
```
type bot.log
```

---

## Folder Structure
```
trading-bot/
├── main.py                  # Entry point, supervisor, startup sequence
├── shared.py                # In-memory state hub (all agents read/write here)
├── dashboard.py             # Flask-style dashboard API server (port 5050)
├── dashboard.html           # Dashboard frontend
├── trading.db               # SQLite database (trades, signals, positions, plans, logs)
├── bot.log                  # Log output when running with > bot.log 2>&1
├── .env                     # API credentials (APCA_API_KEY_ID, APCA_API_SECRET_KEY)
├── config/
│   └── settings.py          # All constants and configuration
├── storage/
│   ├── database.py          # SQLite read/write helpers
│   └── archiver.py          # 30-day rolling file cleanup (NEW — created this session)
├── alpaca_local/
│   ├── client.py            # Alpaca REST API wrapper
│   └── stream.py            # 5 websocket streams (trade, stock, crypto, option, news)
├── agents/
│   ├── ref_library.py       # Fetches assets, OHLCV bars, news, corp actions into cache
│   ├── account_agent.py     # Polls account, positions, fills
│   ├── signal_generator.py  # RSI, MACD, Bollinger, options signals
│   ├── plan_manager.py      # Maintains investment plan targets and stance
│   ├── order_execution.py   # Rebalances portfolio toward plan targets
│   ├── risk_manager.py      # Vetos bad orders (PDT, size limits, margin)
│   ├── boss.py              # Tracks market hours and extended hours
│   ├── diagnostics.py       # Monitors stream health and agent crashes
│   ├── indicators.py        # Pure functions: RSI, MACD, ATR, VWAP, Bollinger, EMA
│   ├── sentiment.py         # Keyword-weighted NLP sentiment scoring
│   ├── iv_engine.py         # Implied volatility rank and regime detection
│   ├── options_strategies.py# Iron condor, covered call, CSP, calendar spread
│   ├── portfolio_heat.py    # VIX regime + portfolio heat gating
│   ├── plan_manager.py      # Investment plan targets and stance
│   └── backtester.py        # Offline backtester using downloaded OHLCV
└── downloads/
    ├── historical_bars/     # OHLCV JSON files per symbol per day
    ├── news/                # News article JSON files
    └── corporate_actions/   # Assets, calendar, corp action JSON files
```

---

## Key Settings (`config/settings.py`)
| Setting | Value | Notes |
|---------|-------|-------|
| IS_PAPER | True | Paper trading mode |
| DATA_FEED | iex | Free IEX feed |
| OPTIONS_LEVEL | 3 | Full options enabled |
| MAX_POSITION_SIZE | $10,000 | Max per order |
| MAX_PORTFOLIO_PCT | 15% | Max per symbol |
| REBALANCE_THRESHOLD | 3% | Min gap to trigger rebalance |
| TICK_INTERVAL | 5s | Agent loop frequency |
| REF_REFRESH_HOURS | 4h | How often ref library refreshes |

---

## Database Schema (`trading.db`)
- **trades** — every fill: symbol, side, qty, price, notional, strategy_tag
- **signals** — every signal emitted: symbol, strategy, side, confidence, sentiment
- **positions** — periodic snapshots: symbol, qty, avg_cost, market_val, unrealised
- **agent_logs** — errors and warnings with restart counts
- **investment_plans** — versioned plan JSON with stance, targets, exclusions

---

## Bugs Fixed This Session
1. `GET /calendar failed` — `get_calendar()` in `alpaca_local/client.py` now takes explicit `start`/`end` date params
2. `GET /corporate_actions failed: NaTType` — NaT guard added to `_fetch_corp_actions()` in `agents/ref_library.py`
3. `'BarSet' object has no attribute 'items'` — fixed to use `bars.data.items()` in `_fetch_historical()`
4. `No module named 'storage.archiver'` — created `storage/archiver.py` stub with `run_cleanup()`
5. `settings.REBALANCE_THRESHOLD` missing — added to `config/settings.py`
6. `shared.investment_plan` missing — added to `shared.py`
7. Duplicate `from config import settings` import in `alpaca_local/client.py` — removed

---

## Known Issues / Next Steps
1. **Dashboard `connecting...`** — `dashboard.py` DB_PATH fix may not have saved correctly. Check with:
   ```
   python -c "print(open('dashboard.py').readlines()[17])"
   ```
   Should show hardcoded path. If not, manually edit line 18 of `dashboard.py` to:
   ```python
   DB_PATH = r"C:\Users\duffd\OneDrive\Desktop\Claude IO\trading.db"
   ```

2. **PAXG/USD trade executed** — bot bought ~2 PAXG (~$10k) on first run. This came from the investment plan. Consider reviewing plan targets before next run or adding a dry-run/confirmation mode.

3. **Options stream 405 error** — subscribing to `*` wildcard for options quotes hits IEX symbol limit. Non-critical but could be fixed by subscribing to specific symbols only.

4. **Stream heartbeat warnings** — `trade` and `news` streams show silent warnings after ~60s of inactivity. This is normal when no fills/news come in. Not a real error.

5. **Sentiment is keyword-based** — `agents/sentiment.py` uses a weighted lexicon. Could be upgraded to FinBERT for better accuracy.

6. **No dry-run mode** — bot executes orders immediately when signals fire. A `DRY_RUN=True` setting in `settings.py` that logs orders without submitting would be useful.

---

## Architecture Notes
- All agents run as daemon threads supervised by `main.py` with exponential backoff restart
- `shared.py` is the single source of truth for in-memory state — no direct agent-to-agent communication
- `ref_library` runs on a 4-hour refresh cycle, also re-fetches bars for "dirty" symbols after corp actions
- `plan_manager` maintains a versioned investment plan in SQLite — every update is persisted
- `order_execution` rebalances toward plan targets every TICK_INTERVAL when market is open
- Risk manager runs checks before every order: PDT, position size, portfolio %, margin, options level

---

## How to Continue in a New Chat
1. Upload the files you want to work on
2. Paste this document at the start of the conversation
3. Reference specific agents or bugs by name — everything is documented above
