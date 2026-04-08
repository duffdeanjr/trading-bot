import time
import logging
import threading
from alpaca.data.live import StockDataStream, CryptoDataStream, OptionDataStream, NewsDataStream
from alpaca.trading.stream import TradingStream
from alpaca.data.enums import DataFeed
import shared
from config import settings

logger = logging.getLogger(__name__)

_registry = {
    "trade":  [],
    "stock":  [],
    "crypto": [],
    "option": [],
    "news":   [],
}
_registry_lock = threading.Lock()

heartbeat     = {}
reconnect_count = {}
_streams      = {}

def register_callback(stream_name: str, fn):
    with _registry_lock:
        if stream_name in _registry:
            _registry[stream_name].append(fn)

def _dispatch(stream_name: str, data):
    heartbeat[stream_name] = time.time()
    with _registry_lock:
        fns = list(_registry.get(stream_name, []))
    for fn in fns:
        try:
            fn(data)
        except Exception as e:
            logger.error(f"stream callback error [{stream_name}]: {e}")

def _build_trading_stream():
    s = TradingStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
        paper      = settings.IS_PAPER,
    )
    async def _on_trade(data):
        _dispatch("trade", data)
    s.subscribe_trade_updates(_on_trade)
    return s

def _build_stock_stream():
    try:
        feed = DataFeed(settings.DATA_FEED)
    except Exception:
        feed = DataFeed.IEX
    s = StockDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
        feed       = feed,
    )
    return s

def _build_crypto_stream():
    s = CryptoDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
    )
    return s

def _build_option_stream():
    s = OptionDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
    )
    return s

def _build_news_stream():
    s = NewsDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
    )
    return s

def start(symbols_equity=None, symbols_crypto=None, symbols_option=None):
    # Default to watchlist symbols instead of wildcard to avoid IEX 405 errors
    if symbols_equity is None:
        wl = [s for s in (shared.watchlist or []) if "/" not in s]
        symbols_equity = wl if wl else ["*"]
    if symbols_crypto is None:
        cl = [s for s in (shared.watchlist or []) if "/" in s]
        symbols_crypto = cl if cl else ["BTC/USD", "ETH/USD"]
    symbols_option = symbols_option or ["*"]

    _streams["trade"]  = _build_trading_stream()
    _streams["stock"]  = _build_stock_stream()
    _streams["crypto"] = _build_crypto_stream()

    if settings.OPTIONS_ENABLED:
        try:
            _streams["option"] = _build_option_stream()
        except Exception as e:
            logger.warning(f"stream: option stream unavailable: {e}")

    try:
        _streams["news"] = _build_news_stream()
    except Exception as e:
        logger.warning(f"stream: news stream unavailable: {e}")

    # Subscribe stock
    async def _on_bar(data):      _dispatch("stock", data)
    async def _on_quote(data):    _dispatch("stock", data)
    async def _on_crypto(data):   _dispatch("crypto", data)
    async def _on_option(data):   _dispatch("option", data)
    async def _on_news(data):     _dispatch("news", data)

    _streams["stock"].subscribe_bars(_on_bar, *symbols_equity)
    # Only subscribe to quotes for specific symbols — wildcard '*' for both
    # bars and quotes exceeds IEX subscription limits (405 error)
    if symbols_equity != ["*"]:
        _streams["stock"].subscribe_quotes(_on_quote, *symbols_equity)
    # Crypto stream doesn't support wildcard '*' — subscribe to specific pairs
    if symbols_crypto == ["*"]:
        symbols_crypto = ["BTC/USD", "ETH/USD", "PAXG/USD"]
    _streams["crypto"].subscribe_bars(_on_crypto, *symbols_crypto)

    if "option" in _streams:
        try:
            _streams["option"].subscribe_quotes(_on_option, *symbols_option)
        except Exception as e:
            logger.warning(f"stream: option subscribe failed: {e}")

    if "news" in _streams:
        try:
            _streams["news"].subscribe_news(_on_news, "*")
        except Exception as e:
            logger.warning(f"stream: news subscribe failed: {e}")

    import random

    def _run_with_reconnect(sname, sobj):
        while not shared.SHUTTING_DOWN:
            try:
                sobj.run()
            except Exception as e:
                logger.error(f"stream '{sname}' disconnected: {e}")
                reconnect_count[sname] = reconnect_count.get(sname, 0) + 1
                backoff = min(2 ** reconnect_count[sname], 60) + random.uniform(0, 2)
                time.sleep(backoff)
        logger.info(f"stream '{sname}' exiting (SHUTTING_DOWN)")

    for name, stream in _streams.items():
        try:
            t = threading.Thread(target=_run_with_reconnect, args=(name, stream),
                                 name=f"stream-{name}", daemon=True)
            t.start()
            logger.info(f"alpaca_local.stream: stream started: {name}")
        except Exception as e:
            logger.warning(f"stream: could not start {name}: {e}")

    shared.stream_ready_event.set()
    logger.info("alpaca_local.stream: stream_ready_event set - all streams running")

def get_heartbeats() -> dict:
    return dict(heartbeat)

def get_reconnect_counts() -> dict:
    return dict(reconnect_count)
