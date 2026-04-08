import time
import logging
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

        now_et = clock.timestamp
        if now_et is not None:
            mins = now_et.hour * 60 + now_et.minute
            shared.EXTENDED_HOURS = (
                not clock.is_open and
                (_EXTENDED_START_OPEN <= mins < _REGULAR_OPEN or
                 _EXTENDED_START_CLOSE <= mins < _EXTENDED_END_CLOSE)
            )
        else:
            shared.EXTENDED_HOURS = False
    except Exception as e:
        logger.error(f"boss: GET /clock failed: {e}")

def _resolve_watchlist():
    """Resolve watchlist from settings or Alpaca API."""
    symbols = []

    # Try Alpaca watchlist first
    if settings.WATCHLIST_ALPACA and not shared.RATE_LIMITED:
        try:
            wlists = alpaca.get_watchlists()
            for wl in wlists:
                name = getattr(wl, 'name', '')
                if name == settings.WATCHLIST_ALPACA:
                    assets = getattr(wl, 'assets', [])
                    symbols = [getattr(a, 'symbol', '') for a in assets if getattr(a, 'symbol', '')]
                    logger.info(f"boss: resolved {len(symbols)} symbols from Alpaca watchlist '{name}'")
                    break
        except Exception as e:
            logger.warning(f"boss: Alpaca watchlist fetch failed: {e}")

    # Fall back to env-configured watchlist
    if not symbols:
        symbols = list(settings.WATCHLIST)

    # Add crypto watchlist
    symbols.extend(settings.CRYPTO_WATCHLIST)

    with shared.cache_lock:
        shared.watchlist = symbols
        shared.ticker_list = symbols

    logger.info(f"boss: watchlist resolved -> {len(symbols)} symbols")

def run():
    logger.info("boss: starting")
    last_open_state = None

    # Resolve watchlist at startup
    _resolve_watchlist()

    while not shared.SHUTTING_DOWN:
        _check_clock()

        # Re-resolve watchlist on market open transition
        if shared.MARKET_OPEN and not last_open_state:
            _resolve_watchlist()
            logger.info("boss: market opened")

        last_open_state = shared.MARKET_OPEN

        sleep_s = settings.TICK_INTERVAL if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) \
                  else settings.OVERNIGHT_SLEEP
        time.sleep(sleep_s)

    logger.info("boss: SHUTTING_DOWN -> exiting")
