import time
import logging
import datetime
import shared
from config import settings
from alpaca_local import client as alpaca
from storage import database

logger = logging.getLogger(__name__)

# -- private fetchers --

def _fetch_assets():
    try:
        assets = alpaca.get_assets()
        with shared.cache_lock:
            shared.assets = {a.symbol: a for a in assets}
        logger.info(f"ref_library: loaded {len(shared.assets)} assets")
    except Exception as e:
        logger.error(f"ref_library: GET /assets failed: {e}")
        shared.ref_load_error = True

def _fetch_calendar():
    try:
        today = datetime.date.today()
        start = today.isoformat()
        end   = (today + datetime.timedelta(days=30)).isoformat()
        cal   = alpaca.get_calendar(start=start, end=end)
        cal_list = list(cal)
        with shared.cache_lock:
            shared.calendar = cal_list
        database.write_calendar(cal_list)
        logger.info(f"ref_library: loaded {len(cal_list)} calendar entries")
    except Exception as e:
        logger.error(f"ref_library: GET /calendar failed: {e}")
        shared.ref_load_error = True

def _fetch_corp_actions():
    try:
        from alpaca.trading.enums import CorporateActionType
        today = datetime.date.today()
        since = today - datetime.timedelta(days=90)
        ca_types = [CorporateActionType.DIVIDEND, CorporateActionType.MERGER,
                    CorporateActionType.SPINOFF, CorporateActionType.SPLIT]
        corps = alpaca.get_corporate_actions(ca_types=ca_types, since=since, until=today)
        corps_list = list(corps)
        with shared.cache_lock:
            shared.corp_actions = corps_list
        database.write_corp_actions(corps_list)
        logger.info(f"ref_library: loaded {len(corps_list)} corporate actions")
    except Exception as e:
        logger.error(f"ref_library: GET /corporate_actions failed: {e}")

def _iter_barset(bars):
    """Iterate a BarSet regardless of SDK version (.data.items() or direct .items())."""
    if hasattr(bars, 'data') and hasattr(bars.data, 'items'):
        return bars.data.items()
    if hasattr(bars, 'items'):
        return bars.items()
    # Fallback: try dict-like access
    return dict(bars).items()

def _is_crypto(symbol: str) -> bool:
    return "/" in symbol

def _fetch_historical(symbols: list, limit=60):
    """Fetch OHLCV bars for equity symbols and write to database."""
    equity_syms = [s for s in symbols if not _is_crypto(s)]
    crypto_syms = [s for s in symbols if _is_crypto(s)]
    if equity_syms:
        _fetch_equity_bars(equity_syms, limit)
    if crypto_syms:
        _fetch_crypto_bars(crypto_syms, limit)

def _fetch_equity_bars(symbols: list, limit=60):
    if not symbols:
        return
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = StockHistoricalDataClient(settings.APCA_KEY, settings.APCA_SECRET)
        end   = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=limit)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start, end=end,
            feed=settings.DATA_FEED,
        )
        bars = client.get_stock_bars(req)
        count = 0
        with shared.cache_lock:
            for sym, bar_list in _iter_barset(bars):
                shared.historical_ohlcv[sym] = bar_list
                database.write_bars(sym, bar_list)
                count += 1
        logger.debug(f"ref_library: fetched equity OHLCV for {count} symbols")
    except Exception as e:
        logger.error(f"ref_library: _fetch_equity_bars failed: {e}")

def _fetch_crypto_bars(symbols: list, limit=60):
    if not symbols:
        return
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = CryptoHistoricalDataClient()
        end   = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=limit)
        req = CryptoBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start, end=end,
        )
        bars = client.get_crypto_bars(req)
        count = 0
        with shared.cache_lock:
            for sym, bar_list in _iter_barset(bars):
                shared.historical_ohlcv[sym] = bar_list
                database.write_bars(sym, bar_list)
                count += 1
        logger.debug(f"ref_library: fetched crypto OHLCV for {count} symbols")
    except Exception as e:
        logger.error(f"ref_library: _fetch_crypto_bars failed: {e}")

def _fetch_news(symbols: list = None, limit: int = 50):
    """Fetch historical news articles via REST and write to database."""
    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest
        client = NewsClient(settings.APCA_KEY, settings.APCA_SECRET)
        # Fetch news without symbol filter (API may not support list or string)
        req    = NewsRequest(limit=limit)
        news   = list(client.get_news(req))
        with shared.cache_lock:
            shared.historical_news = {"articles": news}
        database.write_news(news)
        logger.debug(f"ref_library: fetched {len(news)} news articles")
    except Exception as e:
        logger.error(f"ref_library: _fetch_news failed: {e}")

def _fetch_option_chains(symbols: list):
    """Fetch option chain snapshots for watchlist symbols (if options enabled)."""
    if not settings.OPTIONS_ENABLED:
        return
    equity_syms = [s for s in symbols if not _is_crypto(s)]
    if not equity_syms:
        return
    try:
        from alpaca_local import client as alpaca
        for sym in equity_syms[:20]:  # limit to avoid rate limits
            try:
                contracts = alpaca.get_options_contracts(
                    underlying_symbols=[sym],
                    status="active",
                )
                if contracts:
                    contract_list = list(contracts)
                    database.write_option_chain(contract_list)
                    logger.debug(f"ref_library: fetched {len(contract_list)} option contracts for {sym}")
            except Exception as e:
                logger.debug(f"ref_library: option chain fetch failed for {sym}: {e}")
    except Exception as e:
        logger.error(f"ref_library: _fetch_option_chains failed: {e}")

# -- dirty symbol handling --
def _process_dirty_symbols():
    with shared.cache_lock:
        dirty = set(shared.dirty_symbols)
        shared.dirty_symbols.clear()
    if dirty:
        logger.info(f"ref_library: re-fetching bars for {len(dirty)} dirty symbols: {dirty}")
        _fetch_historical(list(dirty))

# -- full load --
def _full_load():
    logger.info("ref_library: starting full cache load")
    _fetch_assets()
    _fetch_calendar()
    _fetch_corp_actions()
    # Use watchlist if available, otherwise fall back to screened assets
    with shared.cache_lock:
        wl = list(shared.watchlist) if shared.watchlist else []
    if not wl:
        with shared.cache_lock:
            wl = [
                sym for sym, a in shared.assets.items()
                if getattr(a, "tradable", False)
                and str(getattr(a, "asset_class", "")) == "us_equity"
                and all(c.isalpha() or c in "-." for c in sym)
            ][:100]
    _fetch_historical(wl)
    _fetch_news(wl if wl else None)
    _fetch_option_chains(wl)
    database.purge_old_data(days=settings.RETENTION_DAYS)
    logger.info("ref_library: full cache load complete")

def run():
    logger.info("ref_library: starting")
    try:
        _full_load()
    except Exception as e:
        logger.error(f"ref_library: full load error: {e}")
        shared.ref_load_error = True
    finally:
        shared.ref_ready_event.set()
        logger.info("ref_library: ref_ready_event set")

    last_refresh = time.time()

    while not shared.SHUTTING_DOWN:
        _process_dirty_symbols()

        elapsed_hours = (time.time() - last_refresh) / 3600
        if elapsed_hours >= settings.REF_REFRESH_HOURS:
            logger.info("ref_library: scheduled refresh")
            _full_load()
            last_refresh = time.time()

        time.sleep(settings.TICK_INTERVAL * 6)

    logger.info("ref_library: SHUTTING_DOWN -> exiting")
