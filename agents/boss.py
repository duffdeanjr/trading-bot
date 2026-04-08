import time
import logging
import threading
import shared
from config import settings
from alpaca_local import client as alpaca

logger = logging.getLogger(__name__)

_EXTENDED_START_CLOSE  = 16 * 60   # 4:00 PM ET in minutes-since-midnight
_EXTENDED_END_CLOSE    = 20 * 60   # 8:00 PM ET
_EXTENDED_START_OPEN   = 4  * 60   # 4:00 AM ET
_REGULAR_OPEN          = 9  * 60 + 30  # 9:30 AM ET

def _check_clock():
    """GET /clock and write MARKET_OPEN + EXTENDED_HOURS to shared.py."""
    if shared.RATE_LIMITED:
        return
    try:
        clock = alpaca.get_clock()
        shared.MARKET_OPEN = clock.is_open

        # Derive extended hours from timestamp (pre-market and after-hours)
        now_et = clock.timestamp
        mins = now_et.hour * 60 + now_et.minute
        shared.EXTENDED_HOURS = (
            not clock.is_open and
            (_EXTENDED_START_OPEN <= mins < _REGULAR_OPEN or
             _EXTENDED_START_CLOSE <= mins < _EXTENDED_END_CLOSE)
        )
    except Exception as e:
        logger.error(f"boss: GET /clock failed: {e}")

def _fetch_watchlists():
    if shared.RATE_LIMITED:
        return []
    try:
        return alpaca.get_watchlists()
    except Exception as e:
        logger.error(f"boss: GET /watchlists failed: {e}")
        return []

def run():
    logger.info("boss: starting")
    watchlists = []
    last_open_state = None

    while not shared.SHUTTING_DOWN:
        _check_clock()

        # Fetch watchlists once per session open (on transition to open)
        if shared.MARKET_OPEN and not last_open_state:
            watchlists = _fetch_watchlists()
            logger.info(f"boss: market opened ? fetched {len(watchlists)} watchlists")

        last_open_state = shared.MARKET_OPEN

        sleep_s = settings.TICK_INTERVAL if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) \
                  else settings.OVERNIGHT_SLEEP
        time.sleep(sleep_s)

    logger.info("boss: SHUTTING_DOWN ? exiting")
