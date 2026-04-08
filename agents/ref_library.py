import os
import json
import time
import logging
import datetime
import shared
from config import settings
from alpaca_local import client as alpaca

logger = logging.getLogger(__name__)

# ?? download path helpers ?????????????????????????????????????
def _dl(subdir: str, filename: str) -> str:
    """Return absolute path inside downloads/<subdir>/ and ensure dir exists."""
    path = os.path.join(settings.DOWNLOADS_DIR, subdir, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def _save_json(subdir: str, filename: str, data):
    """Serialise data to JSON in downloads/<subdir>/<filename>."""
    try:
        path = _dl(subdir, filename)
        with open(path, "w") as f:
            json.dump(data, f, default=str, indent=2)
        logger.debug(f"ref_library: saved {path}")
    except Exception as e:
        logger.warning(f"ref_library: could not save {subdir}/{filename}: {e}")

# ?? private fetchers ??????????????????????????????????????????

def _fetch_assets():
    try:
        assets = alpaca.get_assets()
        with shared.cache_lock:
            shared.assets = {a.symbol: a for a in assets}
        _save_json("corporate_actions", "assets.json",
                   [{"symbol": a.symbol, "name": getattr(a, "name", ""),
                     "status": getattr(a, "status", ""),
                     "tradable": getattr(a, "tradable", False)}
                    for a in shared.assets.values()])
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
        with shared.cache_lock:
            shared.calendar = list(cal)
        _save_json("corporate_actions", f"calendar_{today}.json",
                   [str(c) for c in shared.calendar])
        logger.info(f"ref_library: loaded {len(shared.calendar)} calendar entries")
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
        with shared.cache_lock:
            shared.corp_actions = list(corps)
        _save_json("corporate_actions", f"corp_actions_{today}.json",
                   [str(c) for c in shared.corp_actions])
        logger.info(f"ref_library: loaded {len(shared.corp_actions)} corporate actions")
    except Exception as e:
        logger.error(f"ref_library: GET /corporate_actions failed: {e}")

def _fetch_historical(symbols: list, limit=30):
    """Fetch OHLCV bars for a list of symbols and save to downloads/historical_bars/."""
    if not symbols:
        return
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = StockHistoricalDataClient(settings.APCA_KEY, settings.APCA_SECRET)
        end   = datetime.datetime.utcnow()
        start = end - datetime.timedelta(days=limit)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start, end=end,
            feed=settings.DATA_FEED,
        )
        bars = client.get_stock_bars(req)
        today = datetime.date.today()
        with shared.cache_lock:
            for sym, bar_list in bars.data.items():
                shared.historical_ohlcv[sym] = bar_list
                # Save each symbol's bars to its own file in downloads/historical_bars/
                _save_json("historical_bars", f"{sym}_{today}.json",
                           [str(b) for b in bar_list])
        logger.debug(f"ref_library: fetched OHLCV for {len(bars)} symbols")
    except Exception as e:
        logger.error(f"ref_library: _fetch_historical failed: {e}")

def _fetch_news(symbols: list = None, limit: int = 50):
    """Fetch historical news articles via REST and save to downloads/news/."""
    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest
        client = NewsClient(settings.APCA_KEY, settings.APCA_SECRET)
        req    = NewsRequest(symbols=symbols, limit=limit)
        news   = list(client.get_news(req))
        with shared.cache_lock:
            shared.historical_news = {"articles": news}
        today = datetime.date.today()
        _save_json("news", f"news_{today}.json",
                   [str(a) for a in news])
        logger.debug(f"ref_library: fetched {len(news)} news articles")
    except Exception as e:
        logger.error(f"ref_library: _fetch_news failed: {e}")

# ?? dirty symbol handling (diagnostic fix) ????????????????????
def _process_dirty_symbols():
    """
    Re-fetch OHLCV bars for symbols flagged dirty by account_agent
    (e.g. after a corp action like a stock split).
    """
    with shared.cache_lock:
        dirty = set(shared.dirty_symbols)
        shared.dirty_symbols.clear()
    if dirty:
        logger.info(f"ref_library: re-fetching bars for {len(dirty)} dirty symbols: {dirty}")
        _fetch_historical(list(dirty))

# ?? full load ?????????????????????????????????????????????????
def _full_load():
    logger.info(f"ref_library: starting full cache load - downloads -> {settings.DOWNLOADS_DIR}")
    _fetch_assets()
    _fetch_calendar()
    _fetch_corp_actions()
    with shared.cache_lock:
        symbols = [
            sym for sym, a in shared.assets.items()
            if getattr(a, "tradable", False)
            and str(getattr(a, "asset_class", "")) == "us_equity"
            and sym.isalpha()
        ][:100]
    _fetch_historical(symbols)
    _fetch_news()
    # Rolling window cleanup + Dropbox archive
    try:
        from storage.archiver import run_cleanup
        run_cleanup()
    except Exception as e:
        logger.warning(f"ref_library: archiver error (non-fatal): {e}")
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
            logger.info("ref_library: scheduled daily refresh")
            _full_load()
            last_refresh = time.time()

        time.sleep(settings.TICK_INTERVAL * 6)  # check dirty symbols every ~30s

    logger.info("ref_library: SHUTTING_DOWN ? exiting")
