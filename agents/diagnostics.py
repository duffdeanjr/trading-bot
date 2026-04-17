import time
import logging
import shared
from config import settings
from alpaca_local import stream as alpaca_stream
from storage import database

logger = logging.getLogger(__name__)

_HEARTBEAT_TIMEOUT  = 60   # seconds — alert if any stream silent for this long
_WARN_COOLDOWN      = 300  # seconds — suppress repeat warnings for same stream
_last_warned: dict  = {}   # stream_name -> last warning timestamp
_RECONNECT_FLAP_MAX = 5    # reconnects in _RECONNECT_WINDOW triggers alert
_RECONNECT_WINDOW   = 3600 # seconds (1 hour)

_last_reconnect_counts = {k: 0 for k in ("trade", "stock", "crypto", "option", "news")}
_reconnect_window_start = time.time()

def _check_stream_health():
    """Check heartbeats and reconnect flap counts for all 5 streams."""
    now = time.time()
    beats  = alpaca_stream.get_heartbeats()
    recons = alpaca_stream.get_reconnect_counts()

    for name, ts in beats.items():
        if ts > 0 and (now - ts) > _HEARTBEAT_TIMEOUT:
            # Cooldown: only warn once per _WARN_COOLDOWN seconds per stream
            last = _last_warned.get(name, 0)
            if now - last < _WARN_COOLDOWN:
                continue
            _last_warned[name] = now
            msg = f"stream '{name}' silent for {now-ts:.0f}s"
            logger.warning(f"diagnostics: {msg}")
            database.write_agent_log(ts=now, agent="diagnostics", level="WARNING", message=msg)

    # Reconnect flap detection (diagnostic improvement)
    global _reconnect_window_start, _last_reconnect_counts
    window_elapsed = now - _reconnect_window_start

    if window_elapsed >= _RECONNECT_WINDOW:
        # Reset window
        _last_reconnect_counts = dict(recons)
        _reconnect_window_start = now
    else:
        for name, count in recons.items():
            delta = count - _last_reconnect_counts.get(name, 0)
            if delta >= _RECONNECT_FLAP_MAX:
                msg = (f"stream '{name}' reconnected {delta}x in "
                       f"{window_elapsed/60:.0f} min — possible connection instability")
                logger.error(f"diagnostics: {msg}")
                database.write_agent_log(ts=now, agent="diagnostics", level="ERROR", message=msg)

def _check_alpaca_status():
    """Poll status.alpaca.markets for outage indicators."""
    if shared.RATE_LIMITED:
        return
    try:
        import urllib.request
        url = "https://status.alpaca.markets/api/v2/status.json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            import json
            data = json.loads(resp.read())
            indicator = data.get("status", {}).get("indicator", "none")
            if indicator not in ("none", "minor"):
                logger.warning(f"diagnostics: Alpaca status={indicator} ? check status.alpaca.markets")
    except Exception as e:
        logger.debug(f"diagnostics: status check failed: {e}")

_rate_limit_flagged_at = 0.0

def _check_rate_limit():
    """Manage the RATE_LIMITED reset timer (429 detection is in client/stream)."""
    global _rate_limit_flagged_at
    if shared.RATE_LIMITED:
        reset_after = settings.BACKOFF_BASE ** settings.MAX_RETRIES
        if _rate_limit_flagged_at == 0.0:
            _rate_limit_flagged_at = time.time()
        if time.time() - _rate_limit_flagged_at > reset_after:
            shared.RATE_LIMITED = False
            _rate_limit_flagged_at = 0.0
            logger.info("diagnostics: RATE_LIMITED cleared after backoff window")

def _check_agent_health():
    """Alert on any agent crash recorded in AGENT_ERRORS."""
    with shared.errors_lock:
        errors = dict(shared.AGENT_ERRORS)
    for agent, info in errors.items():
        count = info.get("count", 0)
        if count > 0:
            msg = (f"agent '{agent}' crashed {count}x, "
                   f"last error: {info.get('last_error', '')} "
                   f"at {info.get('last_ts', 0):.0f}")
            logger.error(f"diagnostics: {msg}")
            database.write_agent_log(
                ts=time.time(), agent="diagnostics", level="ERROR", message=msg
            )

def _check_paper_mode():
    """Verify IS_PAPER matches account type ? prevent accidental live trading."""
    with shared.account_lock:
        acct = shared.account
    acct_type = str(getattr(acct, "account_type", "") or "").lower()
    if acct_type and settings.IS_PAPER and "live" in acct_type:
        logger.error("diagnostics: IS_PAPER=True but account appears to be LIVE ? check .env")
    elif acct_type and not settings.IS_PAPER and "paper" in acct_type:
        logger.error("diagnostics: IS_PAPER=False but account appears to be PAPER ? check .env")

_PRUNE_INTERVAL = 3600       # prune positions table every hour
_WAL_CHECKPOINT_INTERVAL = 1800  # WAL checkpoint every 30 minutes
_last_prune = 0.0
_last_wal_checkpoint = 0.0

def _prune_positions():
    """Keep only the latest 1000 position snapshots to prevent table bloat."""
    global _last_prune
    now = time.time()
    if now - _last_prune < _PRUNE_INTERVAL:
        return
    _last_prune = now
    try:
        conn = database.get_connection()
        count_before = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        if count_before > 5000:
            # Keep latest 1000 rows, delete the rest
            conn.execute("""
                DELETE FROM positions WHERE rowid NOT IN (
                    SELECT rowid FROM positions ORDER BY ts DESC LIMIT 1000
                )
            """)
            conn.commit()
            count_after = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            logger.info(f"diagnostics: pruned positions table {count_before} -> {count_after} rows")
    except Exception as e:
        logger.debug(f"diagnostics: positions prune error: {e}")

def _wal_checkpoint():
    """Periodic WAL checkpoint to keep WAL file size in check."""
    global _last_wal_checkpoint
    now = time.time()
    if now - _last_wal_checkpoint < _WAL_CHECKPOINT_INTERVAL:
        return
    _last_wal_checkpoint = now
    try:
        conn = database.get_connection()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.debug("diagnostics: WAL checkpoint completed")
    except Exception as e:
        logger.debug(f"diagnostics: WAL checkpoint error: {e}")


@shared.register_agent("diagnostics", phase=6)
def run():
    logger.info("diagnostics: starting")
    status_check_counter = 0

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("diagnostics")
        _check_stream_health()
        _check_agent_health()
        _check_rate_limit()
        _check_paper_mode()
        _prune_positions()
        _wal_checkpoint()

        # Check Alpaca status page every ~5 minutes (not every tick)
        status_check_counter += 1
        if status_check_counter >= 60:
            _check_alpaca_status()
            status_check_counter = 0

        # Diagnostics always runs at TICK_INTERVAL — never overnight sleep
        time.sleep(settings.TICK_INTERVAL)

    logger.info("diagnostics: SHUTTING_DOWN — exiting")
