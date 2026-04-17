"""
agents/earnings_calendar.py -- Earnings date tracking and blackout filter.

Fetches earnings dates from Alpaca corporate actions API (already cached in
shared.corp_actions) and optionally from a lightweight web scrape.

Usage by other agents:
  - risk_manager: block new options positions near earnings
  - signal_generator: lower conviction for directional trades near earnings
  - plan_manager: auto-exclude symbols during blackout window
"""

import time
import logging
import datetime
import threading

import shared
from config import settings

logger = logging.getLogger(__name__)

# Cache: symbol -> next earnings date (refreshed every 4 hours)
_earnings_cache: dict = {}  # symbol -> datetime.date or None
_cache_lock = threading.Lock()
_last_refresh = 0.0
_REFRESH_INTERVAL = 14400  # 4 hours


def _parse_earnings_from_corp_actions():
    """
    Extract earnings dates from corporate_actions cache.
    Alpaca corporate actions include 'earnings' type events.
    """
    with shared.cache_lock:
        corp_actions = list(shared.corp_actions)

    today = datetime.date.today()
    upcoming = {}

    for action in corp_actions:
        # Corporate actions can be dicts or Alpaca SDK objects
        if isinstance(action, dict):
            action_type = action.get("ca_type", action.get("type", ""))
            symbol = action.get("symbol", "")
            date_str = action.get("effective_date", action.get("date", ""))
        else:
            action_type = str(getattr(action, "ca_type", getattr(action, "type", "")))
            symbol = str(getattr(action, "symbol", ""))
            date_str = str(getattr(action, "effective_date",
                                   getattr(action, "date", "")))

        # Look for earnings-related corporate actions
        if not symbol or "earning" not in action_type.lower():
            continue

        try:
            earn_date = datetime.date.fromisoformat(str(date_str)[:10])
        except (ValueError, TypeError):
            continue

        # Only care about upcoming dates
        if earn_date >= today:
            if symbol not in upcoming or earn_date < upcoming[symbol]:
                upcoming[symbol] = earn_date

    return upcoming


def refresh_earnings():
    """Refresh earnings cache from available data sources."""
    global _last_refresh
    now = time.time()
    if now - _last_refresh < _REFRESH_INTERVAL:
        return

    earnings = _parse_earnings_from_corp_actions()

    with _cache_lock:
        _earnings_cache.clear()
        _earnings_cache.update(earnings)
    _last_refresh = now

    if earnings:
        logger.info(f"earnings_calendar: refreshed {len(earnings)} upcoming earnings dates")


def get_next_earnings(symbol: str) -> datetime.date | None:
    """Return next earnings date for a symbol, or None if unknown."""
    with _cache_lock:
        return _earnings_cache.get(symbol)


def is_in_blackout(symbol: str) -> bool:
    """
    Check if a symbol is within the earnings blackout window.
    Returns True if earnings are within EARNINGS_BLACKOUT_DAYS.
    """
    if not settings.EARNINGS_ENABLED:
        return False

    earn_date = get_next_earnings(symbol)
    if earn_date is None:
        return False

    today = datetime.date.today()
    days_until = (earn_date - today).days
    return 0 <= days_until <= settings.EARNINGS_BLACKOUT_DAYS


def get_blackout_symbols() -> list:
    """Return list of symbols currently in earnings blackout."""
    if not settings.EARNINGS_ENABLED:
        return []

    refresh_earnings()

    today = datetime.date.today()
    blackout = []
    with _cache_lock:
        for symbol, earn_date in _earnings_cache.items():
            days_until = (earn_date - today).days
            if 0 <= days_until <= settings.EARNINGS_BLACKOUT_DAYS:
                blackout.append((symbol, earn_date, days_until))
    return blackout


def get_earnings_info() -> dict:
    """Return earnings calendar info for dashboard."""
    refresh_earnings()
    with _cache_lock:
        cache_copy = dict(_earnings_cache)

    today = datetime.date.today()
    upcoming = []
    for symbol, earn_date in sorted(cache_copy.items(), key=lambda x: x[1]):
        days_until = (earn_date - today).days
        if days_until >= 0:
            upcoming.append({
                "symbol": symbol,
                "date": earn_date.isoformat(),
                "days_until": days_until,
                "in_blackout": days_until <= settings.EARNINGS_BLACKOUT_DAYS,
            })

    return {
        "total_tracked": len(cache_copy),
        "upcoming": upcoming[:30],
        "blackout_days": settings.EARNINGS_BLACKOUT_DAYS,
    }
