import time
import json
import os
import logging
import shared
from config import settings
from alpaca_local import stream as alpaca_stream

logger = logging.getLogger(__name__)

_HEARTBEAT_TIMEOUT    = 120  # seconds — alert if data streams silent for this long
_TRADE_STREAM_TIMEOUT = 600  # trade stream is idle between fills — longer timeout
_NEWS_STREAM_TIMEOUT  = 600  # news is bursty, long gaps between articles are normal
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
        timeout = (_TRADE_STREAM_TIMEOUT if name == "trade"
                   else _NEWS_STREAM_TIMEOUT if name == "news"
                   else _HEARTBEAT_TIMEOUT)
        if ts > 0 and (now - ts) > timeout:
            logger.warning(f"diagnostics: stream '{name}' silent for {now-ts:.0f}s")

    # Persist stream health to file for dashboard (cross-process)
    # Trade stream only fires on fills — use longer window to avoid false "idle"
    # Options stream is intentionally disabled (no wildcard subscribe)
    _connected_windows = {
        "trade": _TRADE_STREAM_TIMEOUT,
        "news": _NEWS_STREAM_TIMEOUT,
        "option": 0,  # disabled — we don't subscribe to options stream
        "stock": _HEARTBEAT_TIMEOUT,
        "crypto": _HEARTBEAT_TIMEOUT,
    }
    try:
        health = {}
        for name in ("trade", "stock", "crypto", "option", "news"):
            ts = beats.get(name, 0)
            window = _connected_windows.get(name, _HEARTBEAT_TIMEOUT)
            if window == 0:
                # Stream intentionally disabled
                health[name] = {"last_heartbeat": 0, "reconnects": 0, "connected": True, "status": "disabled"}
            elif ts > 0:
                age = now - ts
                health[name] = {
                    "last_heartbeat": ts,
                    "reconnects": recons.get(name, 0),
                    "connected": age < window,
                    "status": "active" if age < window else f"silent {int(age)}s",
                }
            else:
                # Never received data — stream may still be connecting
                health[name] = {"last_heartbeat": 0, "reconnects": recons.get(name, 0),
                                "connected": True, "status": "waiting"}
        health_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".stream_health.json")
        with open(health_path, "w") as f:
            json.dump(health, f)
    except Exception:
        pass

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
                logger.error(
                    f"diagnostics: stream '{name}' reconnected {delta}x in "
                    f"{window_elapsed/60:.0f} min ? possible connection instability"
                )

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
            logger.error(
                f"diagnostics: agent '{agent}' has crashed {count}x, "
                f"last error: {info.get('last_error', '')} "
                f"at {info.get('last_ts', 0):.0f}"
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

        # Check Alpaca status page every ~5 minutes (not every tick)
        status_check_counter += 1
        if status_check_counter >= 60:
            _check_alpaca_status()
            status_check_counter = 0

        # Diagnostics always runs at TICK_INTERVAL ? never overnight sleep
        time.sleep(settings.TICK_INTERVAL)

    logger.info("diagnostics: SHUTTING_DOWN ? exiting")
