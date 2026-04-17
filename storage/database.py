import sqlite3
import logging
import threading
import json
import time
import datetime
from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()  # serialise writes across threads
_conn: sqlite3.Connection = None

def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.row_factory = sqlite3.Row
    return _conn

def init_db():
    """Create all tables if they don't exist. Called once at startup."""
    conn = get_connection()
    with _lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                symbol          TEXT    NOT NULL,
                side            TEXT    NOT NULL,
                qty             REAL    NOT NULL,
                price           REAL    NOT NULL,
                notional        REAL,
                order_type      TEXT,
                order_class     TEXT,
                client_order_id TEXT,
                strategy_tag    TEXT,
                paper           INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                symbol       TEXT    NOT NULL,
                strategy     TEXT    NOT NULL,
                side         TEXT    NOT NULL,
                confidence   REAL,
                sentiment    REAL,
                raw          TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL    NOT NULL,
                symbol     TEXT    NOT NULL,
                qty        REAL    NOT NULL,
                avg_cost   REAL    NOT NULL,
                market_val REAL,
                unrealised REAL,
                asset_class TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL    NOT NULL,
                agent      TEXT    NOT NULL,
                level      TEXT    NOT NULL,
                message    TEXT    NOT NULL,
                restart_n  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS investment_plans (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                version   INTEGER NOT NULL DEFAULT 1,
                plan_json TEXT    NOT NULL,
                trigger   TEXT,
                summary   TEXT
            );

            -- Historical OHLCV bars (single source of truth, replaces JSON files)
            CREATE TABLE IF NOT EXISTS historical_bars (
                symbol  TEXT NOT NULL,
                ts      TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL,
                volume  INTEGER,
                vwap    REAL,
                PRIMARY KEY (symbol, ts)
            );

            -- News articles
            CREATE TABLE IF NOT EXISTS news (
                id       TEXT PRIMARY KEY,
                ts       TEXT NOT NULL,
                headline TEXT,
                summary  TEXT,
                source   TEXT,
                symbols  TEXT,
                raw      TEXT
            );

            -- Market calendar
            CREATE TABLE IF NOT EXISTS market_calendar (
                date  TEXT PRIMARY KEY,
                open  TEXT,
                close TEXT
            );

            -- Corporate actions
            CREATE TABLE IF NOT EXISTS corporate_actions (
                id          TEXT PRIMARY KEY,
                ts          TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                action_type TEXT,
                raw         TEXT
            );

            -- Option chain snapshots (for IV, greeks, strike selection)
            CREATE TABLE IF NOT EXISTS option_chains (
                symbol          TEXT NOT NULL,
                underlying      TEXT NOT NULL,
                expiry          TEXT NOT NULL,
                strike          REAL NOT NULL,
                option_type     TEXT NOT NULL,
                bid             REAL,
                ask             REAL,
                last_price      REAL,
                volume          INTEGER,
                open_interest   INTEGER,
                implied_vol     REAL,
                delta           REAL,
                gamma           REAL,
                theta           REAL,
                vega            REAL,
                fetched_at      TEXT NOT NULL,
                PRIMARY KEY (symbol, fetched_at)
            );

            CREATE INDEX IF NOT EXISTS idx_option_chains_underlying ON option_chains(underlying);
            CREATE INDEX IF NOT EXISTS idx_option_chains_expiry ON option_chains(expiry);

            -- Trade outcomes (entry/exit pairs for feedback loop)
            CREATE TABLE IF NOT EXISTS outcomes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                side           TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_ts       TEXT NOT NULL,
                exit_price     REAL,
                exit_ts        TEXT,
                qty            REAL NOT NULL,
                pnl            REAL,
                pnl_pct        REAL,
                hold_duration_s INTEGER,
                status         TEXT DEFAULT 'open',
                market_context TEXT
            );

            -- Strategy performance scores (rolling)
            CREATE TABLE IF NOT EXISTS strategy_scores (
                strategy     TEXT PRIMARY KEY,
                win_rate     REAL,
                avg_pnl_pct  REAL,
                sharpe       REAL,
                trade_count  INTEGER,
                last_updated TEXT,
                score        REAL
            );

            -- Screener scores (audit trail)
            CREATE TABLE IF NOT EXISTS screener_scores (
                symbol     TEXT NOT NULL,
                ts         REAL NOT NULL,
                score      REAL NOT NULL,
                reasons    TEXT,
                promoted   INTEGER DEFAULT 0,
                PRIMARY KEY (symbol, ts)
            );

            -- Plan review outcomes (Loop 3 self-evaluation)
            CREATE TABLE IF NOT EXISTS plan_review_outcomes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL,
                action      TEXT,
                symbol      TEXT,
                value       TEXT,
                reason      TEXT,
                confidence  REAL,
                was_applied INTEGER,
                pnl_1h      REAL,
                pnl_4h      REAL,
                evaluated   INTEGER DEFAULT 0
            );

            -- Strategy factory recipes
            CREATE TABLE IF NOT EXISTS strategy_recipes (
                id                          TEXT PRIMARY KEY,
                entry                       TEXT,
                filter                      TEXT,
                exit                        TEXT,
                params                      TEXT,
                status                      TEXT DEFAULT 'candidate',
                hypothesis                  TEXT,
                target_regime               TEXT,
                backtest_sharpe             REAL,
                backtest_win_rate           REAL,
                backtest_attempts           INTEGER DEFAULT 0,
                shadow_start_ts             REAL,
                shadow_sharpe               REAL,
                shadow_days                 INTEGER DEFAULT 0,
                consecutive_bad_evaluations INTEGER DEFAULT 0,
                retired_reason              TEXT,
                created_ts                  REAL,
                last_updated_ts             REAL
            );

            -- Bandit state (LinUCB per-strategy A/b matrices)
            CREATE TABLE IF NOT EXISTS bandit_state (
                strategy_id TEXT PRIMARY KEY,
                A_matrix    TEXT,
                b_vector    TEXT,
                alpha       REAL,
                last_updated REAL
            );

            -- Bandit decisions (every multiplier computation, for reward harvesting)
            CREATE TABLE IF NOT EXISTS bandit_decisions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                 REAL,
                strategy_id        TEXT,
                context_vector     TEXT,
                multiplier_applied REAL,
                shadow_mode        INTEGER,
                reward_computed    REAL,
                evaluated          INTEGER DEFAULT 0
            );

            -- Shadow signals for factory strategy evaluation
            CREATE TABLE IF NOT EXISTS shadow_signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL,
                strategy_id TEXT,
                symbol      TEXT,
                side        TEXT,
                conviction  REAL,
                entry_price REAL,
                exit_price  REAL,
                pnl         REAL,
                evaluated   INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_bars_symbol ON historical_bars(symbol);
            CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
            CREATE INDEX IF NOT EXISTS idx_corp_actions_symbol ON corporate_actions(symbol);
            CREATE INDEX IF NOT EXISTS idx_outcomes_strategy ON outcomes(strategy);
            CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(status);
            CREATE INDEX IF NOT EXISTS idx_screener_ts ON screener_scores(ts);
            CREATE INDEX IF NOT EXISTS idx_review_outcomes_ts ON plan_review_outcomes(ts);
            CREATE INDEX IF NOT EXISTS idx_shadow_signals_strategy ON shadow_signals(strategy_id);
            CREATE INDEX IF NOT EXISTS idx_strategy_recipes_status ON strategy_recipes(status);
            CREATE INDEX IF NOT EXISTS idx_bandit_decisions_eval ON bandit_decisions(evaluated, ts);
            CREATE INDEX IF NOT EXISTS idx_bandit_decisions_strategy ON bandit_decisions(strategy_id);
        """)
        conn.commit()

        # -- migrations for existing databases --
        try:
            conn.execute("ALTER TABLE outcomes ADD COLUMN market_context TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    logger.info(f"database initialised: {settings.DB_PATH} (WAL mode)")

# -- write helpers (all wrapped in try/except to prevent agent crashes) --

def write_trade(ts, symbol, side, qty, price, notional=None,
                order_type=None, order_class=None,
                client_order_id=None, strategy_tag=None):
    """Write a fill to the trades table."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT INTO trades
                   (ts, symbol, side, qty, price, notional, order_type,
                    order_class, client_order_id, strategy_tag, paper)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, symbol, side, qty, price, notional, order_type,
                 order_class, client_order_id, strategy_tag, int(settings.IS_PAPER))
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_trade failed: {e}")

def write_signal(ts, symbol, strategy, side, confidence=None, sentiment=None, raw=None):
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "INSERT INTO signals (ts,symbol,strategy,side,confidence,sentiment,raw) VALUES (?,?,?,?,?,?,?)",
                (ts, symbol, strategy, side, confidence, sentiment, str(raw) if raw else None)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_signal failed: {e}")

def write_position_snapshot(ts, symbol, qty, avg_cost, market_val=None, unrealised=None, asset_class=None):
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "INSERT INTO positions (ts,symbol,qty,avg_cost,market_val,unrealised,asset_class) VALUES (?,?,?,?,?,?,?)",
                (ts, symbol, qty, avg_cost, market_val, unrealised, asset_class)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_position_snapshot failed: {e}")

def write_agent_log(ts, agent, level, message, restart_n=0):
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "INSERT INTO agent_logs (ts,agent,level,message,restart_n) VALUES (?,?,?,?,?)",
                (ts, agent, level, message, restart_n)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_agent_log failed: {e}")

def write_investment_plan(ts, plan_json, trigger=None, summary=None):
    """Write a new plan version. Returns the new version number."""
    try:
        conn = get_connection()
        with _lock:
            row = conn.execute("SELECT MAX(version) FROM investment_plans").fetchone()
            next_version = (row[0] or 0) + 1
            conn.execute(
                "INSERT INTO investment_plans (ts,version,plan_json,trigger,summary) VALUES (?,?,?,?,?)",
                (ts, next_version, plan_json, trigger, summary)
            )
            conn.commit()
            return next_version
    except Exception as e:
        logger.error(f"database: write_investment_plan failed: {e}")
        return 0

def read_latest_plan():
    """Return the most recent plan row as a sqlite3.Row, or None."""
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM investment_plans ORDER BY version DESC LIMIT 1"
    ).fetchone()

# -- historical bars (replaces JSON files) --

def write_bars(symbol, bars_list):
    """Bulk upsert OHLCV bars for a symbol."""
    try:
        conn = get_connection()
        with _lock:
            conn.executemany(
                """INSERT OR REPLACE INTO historical_bars
                   (symbol, ts, open, high, low, close, volume, vwap)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(symbol,
                  str(getattr(b, 'timestamp', getattr(b, 't', ''))),
                  float(getattr(b, 'open', getattr(b, 'o', 0))),
                  float(getattr(b, 'high', getattr(b, 'h', 0))),
                  float(getattr(b, 'low', getattr(b, 'l', 0))),
                  float(getattr(b, 'close', getattr(b, 'c', 0))),
                  int(getattr(b, 'volume', getattr(b, 'v', 0))),
                  float(getattr(b, 'vwap', 0) or 0))
                 for b in bars_list]
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_bars failed for {symbol}: {e}")

def read_bars(symbol, since_date=None):
    """Return bars for a symbol as list of dicts, optionally filtered by date."""
    conn = get_connection()
    if since_date:
        rows = conn.execute(
            "SELECT * FROM historical_bars WHERE symbol=? AND ts>=? ORDER BY ts",
            (symbol, since_date)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM historical_bars WHERE symbol=? ORDER BY ts", (symbol,)
        ).fetchall()
    return [dict(r) for r in rows]

def read_bars_all(since_date=None):
    """Return bars for all symbols as {symbol: [bars]}."""
    conn = get_connection()
    if since_date:
        rows = conn.execute(
            "SELECT * FROM historical_bars WHERE ts>=? ORDER BY symbol, ts",
            (since_date,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM historical_bars ORDER BY symbol, ts"
        ).fetchall()
    result = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in result:
            result[sym] = {"dates": [], "opens": [], "highs": [],
                           "lows": [], "closes": [], "volumes": []}
        result[sym]["dates"].append(r["ts"])
        result[sym]["opens"].append(r["open"])
        result[sym]["highs"].append(r["high"])
        result[sym]["lows"].append(r["low"])
        result[sym]["closes"].append(r["close"])
        result[sym]["volumes"].append(r["volume"])
    return result

# -- news --

def write_news(articles):
    """Bulk insert news articles."""
    try:
        conn = get_connection()
        with _lock:
            conn.executemany(
                """INSERT OR IGNORE INTO news (id, ts, headline, summary, source, symbols, raw)
                   VALUES (?,?,?,?,?,?,?)""",
                [(str(getattr(a, 'id', '')),
                  str(getattr(a, 'created_at', getattr(a, 'timestamp', ''))),
                  str(getattr(a, 'headline', '')),
                  str(getattr(a, 'summary', '')),
                  str(getattr(a, 'source', '')),
                  ','.join(getattr(a, 'symbols', []) or []),
                  json.dumps(str(a)))
                 for a in articles]
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_news failed: {e}")

def read_news(symbol=None, since_date=None):
    """Query news, optionally filtered by symbol or date."""
    conn = get_connection()
    query = "SELECT * FROM news WHERE 1=1"
    params = []
    if symbol:
        query += " AND symbols LIKE ?"
        params.append(f"%{symbol}%")
    if since_date:
        query += " AND ts >= ?"
        params.append(since_date)
    query += " ORDER BY ts DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]

# -- market calendar --

def write_calendar(entries):
    """Upsert market calendar entries."""
    try:
        conn = get_connection()
        with _lock:
            conn.executemany(
                "INSERT OR REPLACE INTO market_calendar (date, open, close) VALUES (?,?,?)",
                [(str(getattr(e, 'date', e)),
                  str(getattr(e, 'open', '')),
                  str(getattr(e, 'close', '')))
                 for e in entries]
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_calendar failed: {e}")

def read_calendar():
    """Return upcoming calendar entries."""
    conn = get_connection()
    today = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM market_calendar WHERE date >= ? ORDER BY date", (today,)
    ).fetchall()
    return [dict(r) for r in rows]

# -- corporate actions --

def write_corp_actions(actions):
    """Bulk insert corporate actions."""
    try:
        conn = get_connection()
        with _lock:
            conn.executemany(
                """INSERT OR IGNORE INTO corporate_actions (id, ts, symbol, action_type, raw)
                   VALUES (?,?,?,?,?)""",
                [(str(getattr(a, 'id', '')),
                  str(getattr(a, 'date', getattr(a, 'timestamp', ''))),
                  str(getattr(a, 'symbol', getattr(a, 'target_symbol', ''))),
                  str(getattr(a, 'type', getattr(a, 'ca_type', ''))),
                  json.dumps(str(a)))
                 for a in actions]
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_corp_actions failed: {e}")

def read_corp_actions(since_date=None):
    """Query recent corporate actions."""
    conn = get_connection()
    if since_date:
        rows = conn.execute(
            "SELECT * FROM corporate_actions WHERE ts >= ? ORDER BY ts DESC",
            (since_date,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM corporate_actions ORDER BY ts DESC").fetchall()
    return [dict(r) for r in rows]

# -- option chains --

def write_option_chain(contracts):
    """Bulk insert option chain snapshot."""
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = get_connection()
        with _lock:
            conn.executemany(
                """INSERT OR REPLACE INTO option_chains
                   (symbol, underlying, expiry, strike, option_type, bid, ask,
                    last_price, volume, open_interest, implied_vol,
                    delta, gamma, theta, vega, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(str(getattr(c, 'symbol', '')),
                  str(getattr(c, 'underlying_symbol', getattr(c, 'root_symbol', ''))),
                  str(getattr(c, 'expiration_date', '')),
                  float(getattr(c, 'strike_price', 0) or 0),
                  str(getattr(c, 'type', getattr(c, 'option_type', ''))),
                  float(getattr(c, 'bid', 0) or 0),
                  float(getattr(c, 'ask', 0) or 0),
                  float(getattr(c, 'last_price', getattr(c, 'close', 0)) or 0),
                  int(getattr(c, 'volume', 0) or 0),
                  int(getattr(c, 'open_interest', 0) or 0),
                  float(getattr(c, 'implied_volatility', 0) or 0),
                  float(getattr(c, 'delta', 0) or 0),
                  float(getattr(c, 'gamma', 0) or 0),
                  float(getattr(c, 'theta', 0) or 0),
                  float(getattr(c, 'vega', 0) or 0),
                  now_iso)
                 for c in contracts]
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_option_chain failed: {e}")

def read_option_chain(underlying, expiry=None):
    """Read option chain for an underlying, optionally filtered by expiry."""
    conn = get_connection()
    query = "SELECT * FROM option_chains WHERE underlying=?"
    params = [underlying]
    if expiry:
        query += " AND expiry=?"
        params.append(expiry)
    query += " ORDER BY strike, option_type"
    return [dict(r) for r in conn.execute(query, params).fetchall()]

def read_option_chain_latest(underlying):
    """Read the most recent option chain snapshot for an underlying."""
    conn = get_connection()
    # Get the latest fetch timestamp for this underlying
    row = conn.execute(
        "SELECT MAX(fetched_at) as latest FROM option_chains WHERE underlying=?",
        (underlying,)
    ).fetchone()
    if not row or not row["latest"]:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM option_chains WHERE underlying=? AND fetched_at=? ORDER BY expiry, strike, option_type",
        (underlying, row["latest"])
    ).fetchall()]

# -- outcomes (trade entry/exit tracking for feedback loop) --

def open_outcome(symbol, strategy, side, entry_price, entry_ts, qty, market_context=None):
    """Record entry of a new trade.  market_context is an optional JSON string."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT INTO outcomes (symbol, strategy, side, entry_price, entry_ts, qty, status, market_context)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (symbol, strategy, side, entry_price, entry_ts, qty, "open",
                 market_context)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: open_outcome failed: {e}")

def close_outcome(symbol, strategy, exit_price, exit_ts):
    """Close the oldest open outcome for a symbol+strategy, computing P&L."""
    try:
        conn = get_connection()
        with _lock:
            row = conn.execute(
                """SELECT id, entry_price, entry_ts, qty, side FROM outcomes
                   WHERE symbol=? AND strategy=? AND status='open'
                   ORDER BY entry_ts ASC LIMIT 1""",
                (symbol, strategy)
            ).fetchone()
            if not row:
                # Try matching just by symbol (strategy may differ on exit)
                row = conn.execute(
                    """SELECT id, entry_price, entry_ts, qty, side FROM outcomes
                       WHERE symbol=? AND status='open'
                       ORDER BY entry_ts ASC LIMIT 1""",
                    (symbol,)
                ).fetchone()
            if not row:
                return
            entry_price = row["entry_price"]
            entry_ts = row["entry_ts"]
            qty = row["qty"]
            side = row["side"]
            if side == "buy":
                pnl = (exit_price - entry_price) * qty
            else:
                pnl = (entry_price - exit_price) * qty
            pnl_pct = pnl / (entry_price * qty) if entry_price * qty > 0 else 0
            try:
                hold_s = int((datetime.datetime.fromisoformat(str(exit_ts))
                              - datetime.datetime.fromisoformat(str(entry_ts))).total_seconds())
            except Exception:
                hold_s = 0
            conn.execute(
                """UPDATE outcomes SET exit_price=?, exit_ts=?, pnl=?, pnl_pct=?,
                   hold_duration_s=?, status='closed' WHERE id=?""",
                (exit_price, exit_ts, round(pnl, 4), round(pnl_pct, 6), hold_s, row["id"])
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: close_outcome failed: {e}")

def get_open_outcomes():
    """Return all open outcome rows."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outcomes WHERE status='open'"
    ).fetchall()]

def get_closed_outcomes(strategy=None, limit=50):
    """Return recent closed outcomes, optionally filtered by strategy."""
    conn = get_connection()
    if strategy:
        rows = conn.execute(
            "SELECT * FROM outcomes WHERE status='closed' AND strategy=? ORDER BY exit_ts DESC LIMIT ?",
            (strategy, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM outcomes WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_distinct_strategies():
    """Return list of strategy names that have closed outcomes."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT strategy FROM outcomes WHERE status='closed'"
    ).fetchall()
    return [r["strategy"] for r in rows]

# -- strategy scores --

def write_strategy_score(strategy, win_rate, avg_pnl_pct, sharpe, trade_count, score):
    """Upsert a strategy score."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT OR REPLACE INTO strategy_scores
                   (strategy, win_rate, avg_pnl_pct, sharpe, trade_count, last_updated, score)
                   VALUES (?,?,?,?,?,?,?)""",
                (strategy, win_rate, avg_pnl_pct, sharpe, trade_count,
                 datetime.datetime.now(datetime.timezone.utc).isoformat(), score)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_strategy_score failed: {e}")

def get_strategy_score(strategy):
    """Return strategy score row as dict, or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM strategy_scores WHERE strategy=?", (strategy,)
    ).fetchone()
    return dict(row) if row else None

def get_all_strategy_scores():
    """Return all strategy scores."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM strategy_scores ORDER BY score DESC"
    ).fetchall()]

# -- screener helpers --

def write_screener_score(symbol, ts, score, reasons="", promoted=False):
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "INSERT OR REPLACE INTO screener_scores (symbol, ts, score, reasons, promoted) VALUES (?,?,?,?,?)",
                (symbol, ts, score, reasons, 1 if promoted else 0),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_screener_score failed: {e}")

# -- plan review outcomes --

def write_review_outcome(ts, action, symbol, value, reason, confidence, was_applied):
    """Write a plan review suggestion (applied or skipped) for outcome tracking."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT INTO plan_review_outcomes
                   (ts, action, symbol, value, reason, confidence, was_applied)
                   VALUES (?,?,?,?,?,?,?)""",
                (ts, action, symbol, str(value) if value is not None else None,
                 reason, confidence, 1 if was_applied else 0)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_review_outcome failed: {e}")

def get_unevaluated_review_outcomes(older_than_ts):
    """Return unevaluated review outcomes older than given timestamp."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_review_outcomes WHERE evaluated=0 AND ts < ? ORDER BY ts",
        (older_than_ts,)
    ).fetchall()]

def update_review_outcome_pnl(row_id, pnl_1h, pnl_4h):
    """Fill in P&L for an evaluated review outcome."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "UPDATE plan_review_outcomes SET pnl_1h=?, pnl_4h=?, evaluated=1 WHERE id=?",
                (pnl_1h, pnl_4h, row_id)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: update_review_outcome_pnl failed: {e}")

def get_evaluated_review_outcomes(limit=20):
    """Return recent evaluated review outcomes for self-modifying prompt."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_review_outcomes WHERE evaluated=1 ORDER BY ts DESC LIMIT ?",
        (limit,)
    ).fetchall()]

def get_review_outcome_pnl_by_action():
    """Return average pnl_4h grouped by action type."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        """SELECT action, AVG(pnl_4h) as avg_pnl_4h, COUNT(*) as count
           FROM plan_review_outcomes WHERE evaluated=1
           GROUP BY action"""
    ).fetchall()]

# -- strategy recipes --

def write_strategy_recipe(recipe_id, entry, filter_cond, exit_cond, params,
                          hypothesis=None, target_regime=None,
                          backtest_sharpe=None, backtest_win_rate=None):
    """Insert a new strategy recipe as candidate."""
    try:
        conn = get_connection()
        now = time.time()
        with _lock:
            conn.execute(
                """INSERT OR IGNORE INTO strategy_recipes
                   (id, entry, filter, exit, params, status, hypothesis, target_regime,
                    backtest_sharpe, backtest_win_rate, created_ts, last_updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (recipe_id, entry, filter_cond, exit_cond,
                 json.dumps(params) if params else "{}",
                 "candidate", hypothesis, target_regime,
                 backtest_sharpe, backtest_win_rate, now, now)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_strategy_recipe failed: {e}")

def get_strategies_by_status(status):
    """Return all strategy recipes with given status."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM strategy_recipes WHERE status=? ORDER BY last_updated_ts DESC",
        (status,)
    ).fetchall()]

def update_strategy_status(recipe_id, status, **kwargs):
    """Update a strategy recipe's status and optional fields."""
    try:
        conn = get_connection()
        sets = ["status=?", "last_updated_ts=?"]
        vals = [status, time.time()]
        for k, v in kwargs.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(recipe_id)
        with _lock:
            conn.execute(
                f"UPDATE strategy_recipes SET {', '.join(sets)} WHERE id=?", vals
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: update_strategy_status failed: {e}")

def get_strategy_recipe(recipe_id):
    """Return a single strategy recipe by ID."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM strategy_recipes WHERE id=?", (recipe_id,)).fetchone()
    return dict(row) if row else None

def get_all_strategy_recipe_ids():
    """Return set of all strategy recipe IDs."""
    conn = get_connection()
    return {r["id"] for r in conn.execute("SELECT id FROM strategy_recipes").fetchall()}

# -- shadow signals --

def write_shadow_signal(ts, strategy_id, symbol, side, conviction, entry_price):
    """Record a shadow signal for later evaluation."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT INTO shadow_signals
                   (ts, strategy_id, symbol, side, conviction, entry_price)
                   VALUES (?,?,?,?,?,?)""",
                (ts, strategy_id, symbol, side, conviction, entry_price)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: write_shadow_signal failed: {e}")

def get_unevaluated_shadow_signals(older_than_ts):
    """Return unevaluated shadow signals older than given timestamp."""
    conn = get_connection()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM shadow_signals WHERE evaluated=0 AND ts < ? ORDER BY ts",
        (older_than_ts,)
    ).fetchall()]

def update_shadow_signal(signal_id, exit_price, pnl):
    """Fill in exit price and P&L for evaluated shadow signal."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                "UPDATE shadow_signals SET exit_price=?, pnl=?, evaluated=1 WHERE id=?",
                (exit_price, pnl, signal_id)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"database: update_shadow_signal failed: {e}")

def get_shadow_signal_stats(strategy_id):
    """Return aggregated stats for a strategy's evaluated shadow signals."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT COUNT(*) as count,
                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                  AVG(pnl) as avg_pnl
           FROM shadow_signals WHERE strategy_id=? AND evaluated=1""",
        (strategy_id,)
    ).fetchone()
    if not rows or rows["count"] == 0:
        return None
    count = rows["count"]
    wins = rows["wins"] or 0
    avg_pnl = rows["avg_pnl"] or 0
    returns = [dict(r)["pnl"] for r in conn.execute(
        "SELECT pnl FROM shadow_signals WHERE strategy_id=? AND evaluated=1",
        (strategy_id,)
    ).fetchall()]
    import math
    std = math.sqrt(sum((r - avg_pnl)**2 for r in returns) / len(returns)) if returns else 0
    sharpe = avg_pnl / std if std > 0 else 0
    return {"count": count, "win_rate": wins / count, "avg_pnl": avg_pnl, "sharpe": sharpe}

# -- retention cleanup --

def purge_old_data(days=90):
    """Delete data older than N days from large tables.
    Uses date-only cutoff for corporate_actions (stores date strings)
    and ISO cutoff for others (store ISO timestamps).
    """
    try:
        cutoff_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        conn = get_connection()
        with _lock:
            conn.execute("DELETE FROM historical_bars WHERE ts < ?", (cutoff_iso,))
            conn.execute("DELETE FROM news WHERE ts < ?", (cutoff_iso,))
            # corporate_actions.ts stores date strings like '2026-01-15', not ISO datetimes
            conn.execute("DELETE FROM corporate_actions WHERE ts < ?", (cutoff_date,))
            conn.commit()
        logger.info(f"database: purged data older than {days} days")
    except Exception as e:
        logger.error(f"database: purge_old_data failed: {e}")
