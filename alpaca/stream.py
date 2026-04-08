import time
import logging
import threading
import sys as _sys
import importlib as _importlib

# Bootstrap: import SDK classes before our local 'alpaca' package
# shadows the installed alpaca-py. We temporarily clear the local
# package from sys.modules so the SDK submodule imports work.
def _sdk_import(dotted_name, names):
    """Import names from installed alpaca-py, bypassing our local alpaca/ folder."""
    # Remove local alpaca from cache temporarily
    _saved = {k: v for k, v in _sys.modules.items() if k == 'alpaca' or k.startswith('alpaca.')}
    for k in list(_saved):
        del _sys.modules[k]
    try:
        mod = _importlib.import_module(dotted_name)
        return {n: getattr(mod, n) for n in names}
    finally:
        # Restore local alpaca entries
        _sys.modules.update(_saved)

_sdk = _sdk_import('alpaca.data.live', ['StockDataStream','CryptoDataStream','OptionDataStream','NewsDataStream'])
StockDataStream   = _sdk['StockDataStream']
CryptoDataStream  = _sdk['CryptoDataStream']
OptionDataStream  = _sdk['OptionDataStream']
NewsDataStream    = _sdk['NewsDataStream']

_sdk2 = _sdk_import('alpaca.trading.stream', ['TradingStream'])
TradingStream = _sdk2['TradingStream']

import shared
from config import settings


logger = logging.getLogger(__name__)

# ?? callback registry ?????????????????????????????????????????
# Stored separately from SDK stream objects so they survive reconnect.
_registry = {
    "trade":   [],   # TradingStream fill/order events
    "stock":   [],   # StockDataStream bar/quote/trade events
    "crypto":  [],   # CryptoDataStream bar/quote/trade events
    "option":  [],   # OptionDataStream quote/trade events
    "news":    [],   # NewsDataStream news events
}
_registry_lock = threading.Lock()

# ?? heartbeat timestamps ???????????????????????????????????????
heartbeat = {k: 0.0 for k in _registry}  # updated on each event received

# ?? reconnect counters (diagnostic improvement) ???????????????
reconnect_count = {k: 0 for k in _registry}

# ?? stream instances ???????????????????????????????????????????
_streams = {}

def register_callback(stream_name: str, fn):
    """Register a callback for a given stream. Survives reconnect."""
    with _registry_lock:
        _registry[stream_name].append(fn)

def _dispatch(stream_name: str, data):
    """Fan out an event to all registered callbacks for that stream."""
    heartbeat[stream_name] = time.time()
    with _registry_lock:
        callbacks = list(_registry[stream_name])
    for fn in callbacks:
        try:
            fn(data)
        except Exception as e:
            logger.error(f"stream callback error [{stream_name}]: {e}")

def _reregister_all(stream_name: str, stream_obj):
    """Re-register all saved callbacks onto a new stream object after reconnect."""
    reconnect_count[stream_name] += 1
    logger.warning(f"stream reconnect [{stream_name}] count={reconnect_count[stream_name]}")
    with _registry_lock:
        callbacks = list(_registry[stream_name])
    for fn in callbacks:
        try:
            stream_obj.subscribe_trade_updates(fn) if stream_name == "trade" else None
        except Exception:
            pass

def _build_trading_stream():
    s = TradingStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
        paper      = settings.IS_PAPER,
    )
    @s.on("trade_updates")
    async def _on_trade(data):
        _dispatch("trade", data)
    return s

def _build_stock_stream():
    s = StockDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
        feed       = settings.DATA_FEED,
    )
    @s.on("bars")
    async def _on_bar(data):
        _dispatch("stock", data)
    @s.on("quotes")
    async def _on_quote(data):
        _dispatch("stock", data)
    return s

def _build_crypto_stream():
    s = CryptoDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
    )
    @s.on("bars")
    async def _on_bar(data):
        _dispatch("crypto", data)
    return s

def _build_option_stream():
    s = OptionDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
        feed       = settings.DATA_FEED,
    )
    @s.on("quotes")
    async def _on_quote(data):
        _dispatch("option", data)
    return s

def _build_news_stream():
    s = NewsDataStream(
        api_key    = settings.APCA_KEY,
        secret_key = settings.APCA_SECRET,
    )
    @s.on("news")
    async def _on_news(data):
        _dispatch("news", data)
    return s

def start(symbols_equity=None, symbols_crypto=None, symbols_option=None):
    """
    Build streams and start them in daemon threads, then set stream_ready_event.
    Option and news streams are optional - skipped gracefully if not available.
    """
    symbols_equity  = symbols_equity  or ["*"]
    symbols_crypto  = symbols_crypto  or ["*"]
    symbols_option  = symbols_option  or ["*"]

    # Always start trading, stock, and crypto streams
    _streams["trade"]  = _build_trading_stream()
    _streams["stock"]  = _build_stock_stream()
    _streams["crypto"] = _build_crypto_stream()

    # Option stream requires Algo Trader Plus -- skip if unavailable
    if settings.OPTIONS_ENABLED:
        try:
            _streams["option"] = _build_option_stream()
        except Exception as e:
            logger.warning(f"stream: option stream unavailable (requires Algo Trader Plus): {e}")

    # News stream -- skip if unavailable
    try:
        _streams["news"] = _build_news_stream()
    except Exception as e:
        logger.warning(f"stream: news stream unavailable: {e}")

    # Subscribe to symbols
    _streams["stock"].subscribe_bars(lambda d: None, *symbols_equity)
    _streams["stock"].subscribe_quotes(lambda d: None, *symbols_equity)
    _streams["crypto"].subscribe_bars(lambda d: None, *symbols_crypto)
    if "option" in _streams:
        _streams["option"].subscribe_quotes(lambda d: None, *symbols_option)
    if "news" in _streams:
        _streams["news"].subscribe_news(lambda d: None, "*")

    # Start each available stream in its own daemon thread
    for name, stream in _streams.items():
        try:
            t = threading.Thread(target=stream.run, name=f"stream-{name}", daemon=True)
            t.start()
            logger.info(f"stream started: {name}")
        except Exception as e:
            logger.warning(f"stream: could not start {name}: {e}")

    shared.stream_ready_event.set()
    logger.info("stream_ready_event set - streams running")

def get_heartbeats() -> dict:
    """Return last heartbeat timestamp per stream. Read by diagnostics."""
    return dict(heartbeat)

def get_reconnect_counts() -> dict:
    """Return reconnect count per stream. Read by diagnostics for flap detection."""
    return dict(reconnect_count)
