import sqlite3
import logging
import threading
from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()  # serialise writes across threads
_conn: sqlite3.Connection = None

def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        # WAL mode ? allows concurrent reads while writing (diagnostic fix)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster than FULL
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
        """)
        conn.commit()
    logger.info(f"database initialised: {settings.DB_PATH} (WAL mode)")

# ?? write helpers ?????????????????????????????????????????????

def write_trade(ts, symbol, side, qty, price, notional=None,
                order_type=None, order_class=None,
                client_order_id=None, strategy_tag=None):
    """Write a fill to the trades table. Called immediately on fill callback (diagnostic fix)."""
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

def write_signal(ts, symbol, strategy, side, confidence=None, sentiment=None, raw=None):
    conn = get_connection()
    with _lock:
        conn.execute(
            "INSERT INTO signals (ts,symbol,strategy,side,confidence,sentiment,raw) VALUES (?,?,?,?,?,?,?)",
            (ts, symbol, strategy, side, confidence, sentiment, str(raw) if raw else None)
        )
        conn.commit()

def write_position_snapshot(ts, symbol, qty, avg_cost, market_val=None, unrealised=None, asset_class=None):
    conn = get_connection()
    with _lock:
        conn.execute(
            "INSERT INTO positions (ts,symbol,qty,avg_cost,market_val,unrealised,asset_class) VALUES (?,?,?,?,?,?,?)",
            (ts, symbol, qty, avg_cost, market_val, unrealised, asset_class)
        )
        conn.commit()

def write_agent_log(ts, agent, level, message, restart_n=0):
    conn = get_connection()
    with _lock:
        conn.execute(
            "INSERT INTO agent_logs (ts,agent,level,message,restart_n) VALUES (?,?,?,?,?)",
            (ts, agent, level, message, restart_n)
        )
        conn.commit()

def write_investment_plan(ts, plan_json, trigger=None, summary=None):
    """Write a new plan version. Returns the new version number."""
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

def read_latest_plan():
    """Return the most recent plan row as a sqlite3.Row, or None."""
    conn = get_connection()
    return conn.execute(
        "SELECT * FROM investment_plans ORDER BY version DESC LIMIT 1"
    ).fetchone()
