import sqlite3
import logging
import threading
import json
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
                status         TEXT DEFAULT 'open'
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

            CREATE INDEX IF NOT EXISTS idx_bars_symbol ON historical_bars(symbol);
            CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
            CREATE INDEX IF NOT EXISTS idx_corp_actions_symbol ON corporate_actions(symbol);
            CREATE INDEX IF NOT EXISTS idx_outcomes_strategy ON outcomes(strategy);
            CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(status);
        """)
        conn.commit()
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

def open_outcome(symbol, strategy, side, entry_price, entry_ts, qty):
    """Record entry of a new trade."""
    try:
        conn = get_connection()
        with _lock:
            conn.execute(
                """INSERT INTO outcomes (symbol, strategy, side, entry_price, entry_ts, qty, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (symbol, strategy, side, entry_price, entry_ts, qty, "open")
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

# -- retention cleanup --

def purge_old_data(days=90):
    """Delete data older than N days from large tables."""
    try:
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        conn = get_connection()
        with _lock:
            conn.execute("DELETE FROM historical_bars WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM news WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM corporate_actions WHERE ts < ?", (cutoff,))
            conn.commit()
        logger.info(f"database: purged data older than {days} days")
    except Exception as e:
        logger.error(f"database: purge_old_data failed: {e}")
