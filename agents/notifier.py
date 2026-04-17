"""
agents/notifier.py -- Lightweight notification system for critical trading events.

Supports:
  - Console logging (always on)
  - Discord webhook (if DISCORD_WEBHOOK_URL is set in .env)

Events that trigger notifications:
  - Circuit breaker tripped (daily loss / consecutive losses)
  - Agent crash/restart
  - Large fill (> NOTIFY_LARGE_FILL_USD)
  - Strategy auto-disabled by walk-forward
  - Short option near expiry (auto-roll)
  - Daily P&L summary (scheduled)
  - Portfolio heat warning
"""

import os
import time
import json
import logging
import datetime
import threading
from collections import deque

import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

# ── configuration ────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
NOTIFY_LARGE_FILL_USD = float(os.getenv("NOTIFY_LARGE_FILL_USD", "1000"))

# Throttling: max 1 notification per event type per cooldown period
_COOLDOWN_S = 300  # 5 minutes
_last_sent: dict = {}  # event_type -> timestamp
_lock = threading.Lock()

# Recent notifications buffer for dashboard
_recent: deque = deque(maxlen=50)


def _should_send(event_type: str) -> bool:
    """Check cooldown to prevent notification spam."""
    now = time.time()
    with _lock:
        last = _last_sent.get(event_type, 0)
        if now - last < _COOLDOWN_S:
            return False
        _last_sent[event_type] = now
    return True


def _send_discord(title: str, message: str, color: int = 0xFF0000):
    """Send a Discord embed via webhook. Non-blocking, fire-and-forget."""
    if not DISCORD_WEBHOOK_URL:
        return

    def _post():
        try:
            import urllib.request
            payload = json.dumps({
                "embeds": [{
                    "title": title,
                    "description": message[:2000],
                    "color": color,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "footer": {"text": "Trading Bot Alert"}
                }]
            }).encode("utf-8")
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.debug(f"notifier: Discord webhook failed: {e}")

    threading.Thread(target=_post, daemon=True).start()


def _record(level: str, title: str, message: str):
    """Record notification to recent buffer and DB."""
    ts = time.time()
    _recent.append({
        "ts": ts,
        "level": level,
        "title": title,
        "message": message,
    })
    try:
        database.write_agent_log(
            ts=ts, agent="notifier", level=level,
            message=f"[{title}] {message}"
        )
    except Exception:
        pass


# ── public notification functions ────────────────────────────────

def alert_circuit_breaker(reason: str):
    """Notify when circuit breaker trips (critical)."""
    if not _should_send("circuit_breaker"):
        return
    title = "🚨 CIRCUIT BREAKER TRIPPED"
    msg = f"Trading halted: {reason}"
    logger.critical(f"ALERT: {title} — {msg}")
    _record("CRITICAL", title, msg)
    _send_discord(title, msg, color=0xFF0000)


def alert_agent_crash(agent_name: str, error: str, restart_count: int):
    """Notify when an agent crashes and restarts."""
    if not _should_send(f"agent_crash_{agent_name}"):
        return
    title = f"⚠️ Agent Crash: {agent_name}"
    msg = f"Error: {error}\nRestart #{restart_count}"
    logger.error(f"ALERT: {title} — {msg}")
    _record("ERROR", title, msg)
    _send_discord(title, msg, color=0xFFA500)


def alert_large_fill(symbol: str, side: str, qty: float, price: float, notional: float):
    """Notify on large order fills."""
    if notional < NOTIFY_LARGE_FILL_USD:
        return
    if not _should_send(f"fill_{symbol}_{side}"):
        return
    title = f"💰 Large Fill: {side.upper()} {symbol}"
    msg = f"{qty} shares @ ${price:.2f} = ${notional:,.0f}"
    logger.info(f"ALERT: {title} — {msg}")
    _record("INFO", title, msg)
    _send_discord(title, msg, color=0x00FF00 if side == "buy" else 0xFF6600)


def alert_strategy_disabled(strategy: str, sharpe: float, trades: int):
    """Notify when walk-forward auto-disables a strategy."""
    if not _should_send(f"strategy_disabled_{strategy}"):
        return
    title = f"🔴 Strategy Disabled: {strategy}"
    msg = f"Rolling Sharpe={sharpe:.2f} (threshold: -0.5), trades={trades}"
    logger.warning(f"ALERT: {title} — {msg}")
    _record("WARNING", title, msg)
    _send_discord(title, msg, color=0xFF4444)


def alert_expiry_warning(symbol: str, days_left: int, unrealized: float):
    """Notify about near-expiry short options."""
    if not _should_send(f"expiry_{symbol}"):
        return
    title = f"⏰ Options Expiry: {symbol}"
    msg = f"Expires in {days_left} day(s), unrealized P&L: ${unrealized:,.0f}"
    logger.warning(f"ALERT: {title} — {msg}")
    _record("WARNING", title, msg)
    _send_discord(title, msg, color=0xFFAA00)


def alert_heat_warning(heat: float, vix: float):
    """Notify when portfolio heat exceeds warning threshold."""
    if not _should_send("heat_warning"):
        return
    title = "🔥 Portfolio Heat Warning"
    msg = f"Heat: {heat:.0%} (max: {settings.HEAT_MAX:.0%}), VIX: {vix:.1f}"
    logger.warning(f"ALERT: {title} — {msg}")
    _record("WARNING", title, msg)
    _send_discord(title, msg, color=0xFF8800)


def notify_daily_summary():
    """Generate and send a daily P&L summary."""
    if not _should_send("daily_summary"):
        return

    acct = shared.get_account_snapshot()
    if not acct:
        return

    try:
        equity = float(getattr(acct, "equity", 0) or 0)
        last_equity = float(getattr(acct, "last_equity", 0) or 0)
        cash = float(getattr(acct, "cash", 0) or 0)
    except Exception:
        return

    daily_pnl = equity - last_equity if last_equity > 0 else 0
    daily_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0

    positions = shared.get_positions_snapshot()
    n_positions = len(positions)

    # Get today's trades count
    try:
        today_start = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        conn = database.get_connection()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE ts >= ?", (today_start,)
        ).fetchone()
        n_trades = row["cnt"] if row else 0
    except Exception:
        n_trades = 0

    pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
    title = f"{pnl_emoji} Daily Summary"
    msg = (
        f"**Equity:** ${equity:,.0f}\n"
        f"**Daily P&L:** ${daily_pnl:+,.0f} ({daily_pct:+.2f}%)\n"
        f"**Cash:** ${cash:,.0f}\n"
        f"**Positions:** {n_positions}\n"
        f"**Trades today:** {n_trades}"
    )
    logger.info(f"ALERT: {title}\n{msg}")
    _record("INFO", title, msg)
    color = 0x00CC00 if daily_pnl >= 0 else 0xCC0000
    _send_discord(title, msg, color=color)


def get_recent_notifications(limit: int = 20) -> list:
    """Return recent notifications for dashboard display."""
    return list(_recent)[-limit:]


# ── daily summary scheduler (runs inside main loop) ──────────────

@shared.register_agent("notifier", phase=7)
def run():
    """Background agent that sends daily summary at market close."""
    logger.info("notifier: starting")
    last_summary_date = None

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("notifier")

        # Send daily summary once after market closes (4:15 PM ET check)
        now = datetime.datetime.now()
        today = now.date()
        if (not shared.MARKET_OPEN
                and now.hour >= 16
                and last_summary_date != today):
            try:
                notify_daily_summary()
                last_summary_date = today
            except Exception as e:
                logger.error(f"notifier: daily summary error: {e}")

        # Check portfolio heat periodically
        try:
            from agents import risk_manager
            heat = risk_manager.compute_heat()
            vix = risk_manager.get_last_vix()
            if heat >= settings.HEAT_WARN:
                alert_heat_warning(heat, vix)
        except Exception:
            pass

        time.sleep(60)  # check every minute

    logger.info("notifier: SHUTTING_DOWN - exiting")
