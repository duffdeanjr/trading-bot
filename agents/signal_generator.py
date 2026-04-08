"""
agents/signal_generator.py -- Signal generation with real technical indicators,
IV rank, improved sentiment, and options strategy selection.
"""

import time
import logging
import threading
import time
import logging
import threading
import shared
from config import settings
from alpaca_local import stream as alpaca_stream
from agents import indicators, iv_engine, sentiment, options_strategies, plan_manager

logger = logging.getLogger(__name__)

_signals_emitted: set = set()
_signals_lock    = threading.Lock()

def _clear_signals_emitted():
    with _signals_lock:
        _signals_emitted.clear()

def _already_emitted(symbol, side, strategy) -> bool:
    key = (symbol, side, strategy)
    with _signals_lock:
        if key in _signals_emitted:
            return True
        _signals_emitted.add(key)
        return False

# -- stream data stores -------------------------------------------------------
_bars:    dict = {}  # symbol -> latest bar object
_news:    dict = {}  # symbol -> list of recent news events
_options: dict = {}  # symbol -> latest option quote
_data_lock = threading.Lock()

def _on_stock_data(data):
    sym = getattr(data, "symbol", None)
    if sym:
        with _data_lock:
            _bars[sym] = data

def _on_crypto_data(data):
    sym = getattr(data, "symbol", None)
    if sym:
        with _data_lock:
            _bars[sym] = data

def _on_option_data(data):
    sym = getattr(data, "symbol", None)
    if sym:
        with _data_lock:
            _options[sym] = data

def _on_news(event):
    symbols = getattr(event, "symbols", []) or []
    for sym in symbols:
        with _data_lock:
            if sym not in _news:
                _news[sym] = []
            _news[sym].append(event)
            _news[sym] = _news[sym][-10:]  # keep last 10 per symbol

# -- screener -----------------------------------------------------------------
def _run_screener() -> list:
    with shared.cache_lock:
        assets = shared.assets
    return [
        a.symbol for a in assets.values()
        if getattr(a, "tradable", False) and getattr(a, "status", "") == "active"
    ][:200]

def _update_ticker_list(symbols: list):
    with shared.cache_lock:
        shared.ticker_list = symbols
        shared.ticker_ts   = time.time()

# -- signal emission ----------------------------------------------------------
def _build_ohlcv(symbol: str) -> dict:
    """Extract OHLCV arrays from historical cache for indicator computation."""
    with shared.cache_lock:
        hist = shared.historical_ohlcv.get(symbol, {})
    if isinstance(hist, dict):
        return hist
    return {}

def _emit_equity_signals(symbols: list) -> list:
    if not (shared.MARKET_OPEN or shared.EXTENDED_HOURS):
        return []

    _clear_signals_emitted()
    batch = []

    with _data_lock:
        bars   = dict(_bars)
        news   = dict(_news)

    # Get plan exclusions upfront
    try:
        exclusions = set(plan_manager.get_exclusions())
    except Exception:
        exclusions = set()

    for symbol in symbols:
        if symbol in exclusions:
            continue

        bar = bars.get(symbol)
        if bar is None:
            continue

        close = float(getattr(bar, "close", 0) or 0)
        if close <= 0:
            continue

        # -- Technical indicators --
        ohlcv  = _build_ohlcv(symbol)
        closes = ohlcv.get("closes", [])

        # Append current bar close for freshness
        if closes:
            closes = closes + [close]

        ind = indicators.compute_all({
            "closes":  closes,
            "highs":   ohlcv.get("highs", []),
            "lows":    ohlcv.get("lows", []),
            "volumes": ohlcv.get("volumes", []),
        })

        rsi_val  = ind.get("rsi")
        macd_d   = ind.get("macd") or {}
        boll_d   = ind.get("bollinger") or {}
        ema_d    = ind.get("ema_cross") or {}
        atr_val  = ind.get("atr")

        # -- Sentiment --
        news_events   = news.get(symbol, [])
        sent_score    = sentiment.score_news_events(news_events)

        # -- IV / options regime --
        iv_val   = iv_engine.estimate_iv_from_chain(symbol, close)
        ivr_data = iv_engine.get_ivr(symbol, iv_val)
        regime   = ivr_data.get("regime", "unknown")
        ivr      = ivr_data.get("ivr")

        # -- Signal logic --
        signals_for_symbol = []

        # 1. RSI oversold + positive sentiment + bullish EMA = buy
        if (rsi_val and rsi_val < 35
                and sent_score > 0.1
                and ema_d.get("cross") != "bearish"):
            confidence = 0.5 + (35 - rsi_val) / 70 + sent_score * 0.2
            signals_for_symbol.append({
                "symbol":     symbol,
                "side":       "buy",
                "confidence": min(round(confidence, 3), 1.0),
                "sentiment":  round(sent_score, 3),
                "strategy":   "rsi_oversold",
                "rsi":        round(rsi_val, 1),
            })

        # 2. MACD bullish crossover
        if macd_d.get("histogram", 0) > 0 and sent_score >= 0:
            signals_for_symbol.append({
                "symbol":     symbol,
                "side":       "buy",
                "confidence": 0.55,
                "sentiment":  round(sent_score, 3),
                "strategy":   "macd_cross",
            })

        # 3. Bollinger Band bounce (price near lower band with positive news)
        if boll_d.get("pct_b", 0.5) < 0.15 and sent_score > 0:
            signals_for_symbol.append({
                "symbol":     symbol,
                "side":       "buy",
                "confidence": 0.60,
                "sentiment":  round(sent_score, 3),
                "strategy":   "bb_bounce",
            })

        # 4. RSI overbought = sell signal
        if rsi_val and rsi_val > 70 and sent_score < 0:
            signals_for_symbol.append({
                "symbol":     symbol,
                "side":       "sell",
                "confidence": min(0.5 + (rsi_val - 70) / 60, 1.0),
                "sentiment":  round(sent_score, 3),
                "strategy":   "rsi_overbought",
            })

        # 5. Options signals based on IV regime
        if (settings.OPTIONS_ENABLED
                and settings.OPTIONS_LEVEL >= 3
                and shared.MARKET_OPEN
                and not shared.EXTENDED_HOURS
                and ivr is not None):

            opt_strategy = iv_engine.select_strategy(ivr_data)

            if opt_strategy == "iron_condor" and ivr >= 50:
                if not _already_emitted(symbol, "sell", "iron_condor"):
                    sig = options_strategies.iron_condor(symbol, close)
                    if sig:
                        sig["confidence"] = min(0.5 + ivr / 200, 0.9)
                        sig["sentiment"]  = round(sent_score, 3)
                        sig["ivr"]        = ivr
                        signals_for_symbol.append(sig)

            elif opt_strategy == "covered_call" and ivr >= 35:
                # Only if we hold the stock
                with shared.positions_lock:
                    holds = symbol in shared.positions
                if holds and not _already_emitted(symbol, "sell", "covered_call"):
                    sig = options_strategies.covered_call(symbol, close)
                    if sig:
                        sig["confidence"] = 0.70
                        signals_for_symbol.append(sig)

            elif opt_strategy == "cash_secured_put":
                if not _already_emitted(symbol, "sell", "cash_secured_put"):
                    sig = options_strategies.cash_secured_put(symbol, close)
                    if sig:
                        sig["confidence"] = 0.65
                        signals_for_symbol.append(sig)

            elif opt_strategy == "calendar_spread" and (ivr is None or ivr <= 30):
                if not _already_emitted(symbol, "buy", "calendar_spread"):
                    sig = options_strategies.calendar_spread(symbol, close)
                    if sig:
                        sig["confidence"] = 0.60
                        signals_for_symbol.append(sig)

        # Deduplicate and add to batch
        for sig in signals_for_symbol:
            key_side = sig.get("side", "buy")
            key_strat = sig.get("strategy", "unknown")
            if not _already_emitted(symbol, key_side, key_strat):
                batch.append(sig)

    return batch


def _emit_crypto_signals() -> list:
    batch = []
    with _data_lock:
        bars = dict(_bars)
        news = dict(_news)

    for symbol, bar in bars.items():
        if not ("/" in symbol or symbol.endswith("USD")):
            continue
        close = float(getattr(bar, "close", 0) or 0)
        open_ = float(getattr(bar, "open",  0) or 0)
        if close <= 0 or open_ <= 0:
            continue

        sent = sentiment.score_news_events(news.get(symbol, []))

        if close > open_ * 1.005 and sent >= 0:
            if not _already_emitted(symbol, "buy", "crypto_momentum"):
                batch.append({
                    "symbol":     symbol,
                    "side":       "buy",
                    "confidence": min(0.5 + (close/open_ - 1) * 5, 0.85),
                    "sentiment":  round(sent, 3),
                    "strategy":   "crypto_momentum",
                })

        elif close < open_ * 0.995 and sent < 0:
            if not _already_emitted(symbol, "sell", "crypto_momentum"):
                batch.append({
                    "symbol":     symbol,
                    "side":       "sell",
                    "confidence": min(0.5 + (1 - close/open_) * 5, 0.85),
                    "sentiment":  round(sent, 3),
                    "strategy":   "crypto_momentum",
                })

    return batch


def _check_rolls() -> list:
    """Check for options needing rolling and emit roll signals."""
    with shared.positions_lock:
        positions = dict(shared.positions)
    try:
        return options_strategies.check_rolls_needed(positions)
    except Exception as e:
        logger.debug(f"roll check error: {e}")
        return []


# -- main loop ----------------------------------------------------------------
def run():
    logger.info("signal_generator: starting")

    alpaca_stream.register_callback("stock",  _on_stock_data)
    alpaca_stream.register_callback("crypto", _on_crypto_data)
    alpaca_stream.register_callback("option", _on_option_data)
    alpaca_stream.register_callback("news",   _on_news)

    while not shared.SHUTTING_DOWN:
        if shared.RATE_LIMITED:
            time.sleep(settings.TICK_INTERVAL)
            continue

        symbols = _run_screener()
        _update_ticker_list(symbols)

        equity_signals = _emit_equity_signals(symbols)
        crypto_signals = _emit_crypto_signals()
        roll_signals   = _check_rolls() if shared.MARKET_OPEN else []
        all_signals    = equity_signals + crypto_signals + roll_signals

        if all_signals:
            try:
                plan_manager.update_plan(all_signals, trigger="signal_batch")
            except Exception as e:
                logger.warning(f"signal_generator: plan_manager update failed: {e}")

        sleep_s = settings.TICK_INTERVAL if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) \
                  else settings.OVERNIGHT_SLEEP
        time.sleep(sleep_s)

    logger.info("signal_generator: SHUTTING_DOWN - exiting")
