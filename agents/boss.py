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

    # Merge screener promotions
    if settings.SCREENER_ENABLED:
        symbols = _merge_screener(symbols)

    with shared.cache_lock:
        shared.watchlist = symbols
        shared.ticker_list = symbols

    logger.info(f"boss: watchlist resolved -> {len(symbols)} symbols")


def _merge_screener(symbols: list) -> list:
    """Merge screener candidates into watchlist and process demotions."""
    core = set(settings.WATCHLIST + settings.CRYPTO_WATCHLIST)
    max_size = settings.MAX_WATCHLIST_SIZE

    with shared.cache_lock:
        candidates = dict(shared.screener_candidates)
        demotions = list(shared.screener_demotions)
        shared.screener_demotions = []  # clear after reading

    with shared.positions_lock:
        held = set(shared.positions.keys())

    # Process demotions: remove low-scoring non-core, non-held symbols
    if demotions:
        remove_set = set()
        for sym in demotions:
            if sym not in core and sym not in held:
                remove_set.add(sym)
        before = len(symbols)
        symbols = [s for s in symbols if s not in remove_set]
        removed = before - len(symbols)
        if removed:
            logger.info(f"boss: screener demoted {removed} symbols: {remove_set}")

    # Process promotions: add high-scoring candidates up to max_size
    existing = set(symbols)
    added = []
    # Sort candidates by score descending
    sorted_cands = sorted(candidates.items(), key=lambda x: x[1].get("score", 0), reverse=True)

    for sym, info in sorted_cands:
        if sym in existing:
            continue
        if len(symbols) >= max_size:
            break
        if info.get("score", 0) >= settings.SCREENER_PROMOTE_THRESHOLD:
            symbols.append(sym)
            existing.add(sym)
            added.append(sym)
            # Flag for ref_library to fetch bars
            with shared.cache_lock:
                shared.dirty_symbols.add(sym)

    if added:
        logger.info(f"boss: screener promoted {len(added)} symbols: {added}")

    return symbols


@shared.register_agent("boss", phase=7)
def run():
    logger.info("boss: starting")
    last_open_state = None
    last_merge_ts = 0.0
    _MERGE_INTERVAL = 300  # re-merge screener output every 5 min

    # Resolve watchlist at startup
    _resolve_watchlist()

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("boss")
        _check_clock()

        # Re-resolve watchlist on market open transition
        if shared.MARKET_OPEN and not last_open_state:
            _resolve_watchlist()
            logger.info("boss: market opened")

        # Periodically re-merge screener during market hours
        now = time.time()
        if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) and (now - last_merge_ts) >= _MERGE_INTERVAL:
            if settings.SCREENER_ENABLED:
                _resolve_watchlist()
                last_merge_ts = now

        last_open_state = shared.MARKET_OPEN

        sleep_s = settings.TICK_INTERVAL if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) \
                  else settings.OVERNIGHT_SLEEP
        time.sleep(sleep_s)

    logger.info("boss: SHUTTING_DOWN -> exiting")
