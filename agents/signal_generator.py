"""
agents/signal_generator.py -- Signal generation with real technical indicators,
IV rank, improved sentiment, and options strategy selection.
"""

import time
import logging
import threading
import math
import datetime
from collections import defaultdict
import shared
from config import settings
from storage import database
from alpaca_local import stream as alpaca_stream
from agents import indicators, iv_engine, sentiment, options_strategies, plan_manager, strategy_factory

logger = logging.getLogger(__name__)

# ── strategy registry ───────────────────────────────────────────
_STRATEGY_REGISTRY = {}  # name -> callable(sym, ctx) -> list[dict]


def register_strategy(name):
    """Decorator to register an equity/options strategy function.
    Signature: fn(symbol: str, ctx: dict) -> list[dict]
    ctx keys: ind, bar, close, sent_score, sent_boost, iv_val, ivr_data, regime
    """
    def decorator(fn):
        _STRATEGY_REGISTRY[name] = fn
        return fn
    return decorator



# ── registered strategies ───────────────────────────────────────

@register_strategy("rsi_oversold")
def _strat_rsi_oversold(symbol, ctx):
    """RSI oversold + non-bearish EMA = buy. RSI 35→0.55, RSI 25→0.84, RSI 15→1.0"""
    rsi_val = ctx["ind"].get("rsi")
    ema_d = ctx["ind"].get("ema_cross") or {}
    if not (rsi_val and rsi_val < settings.RSI_OVERSOLD
            and ema_d.get("cross") != "bearish"):
        return []
    rsi_depth = (settings.RSI_OVERSOLD - rsi_val) / settings.RSI_OVERSOLD
    confidence = 0.55 + rsi_depth * 0.40 + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "rsi_oversold",
        "rsi":        round(rsi_val, 1),
    }]


@register_strategy("macd_cross")
def _strat_macd_cross(symbol, ctx):
    """MACD bullish crossover — scale by histogram strength."""
    macd_d = ctx["ind"].get("macd") or {}
    macd_hist = macd_d.get("histogram", 0)
    if macd_hist <= 0:
        return []
    close = ctx["close"]
    hist_strength = min(abs(macd_hist) / (close * 0.002 + 0.01), 1.0)
    confidence = 0.55 + hist_strength * 0.20 + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "macd_cross",
    }]


@register_strategy("bb_bounce")
def _strat_bb_bounce(symbol, ctx):
    """Bollinger Band bounce — deeper below band = higher confidence."""
    boll_d = ctx["ind"].get("bollinger") or {}
    pct_b = boll_d.get("pct_b", 0.5)
    if pct_b >= 0.15:
        return []
    bb_depth = (0.15 - pct_b) / 0.15
    confidence = 0.60 + bb_depth * 0.25 + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "bb_bounce",
    }]


@register_strategy("rsi_overbought")
def _strat_rsi_overbought(symbol, ctx):
    """RSI overbought = sell signal. RSI 70→0.55, RSI 80→0.72, RSI 90→0.88"""
    rsi_val = ctx["ind"].get("rsi")
    if not (rsi_val and rsi_val > settings.RSI_OVERBOUGHT):
        return []
    rsi_excess = (rsi_val - settings.RSI_OVERBOUGHT) / (100 - settings.RSI_OVERBOUGHT)
    confidence = 0.55 + rsi_excess * 0.40 - ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "sell",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "rsi_overbought",
    }]


@register_strategy("options_iv")
def _strat_options_iv(symbol, ctx):
    """Options signals based on IV regime (iron condor, covered call, CSP, calendar)."""
    regime = ctx["regime"]
    iv_val = ctx["iv_val"]
    ivr_data = ctx["ivr_data"]
    ivr = ivr_data.get("ivr")
    sent_score = ctx["sent_score"]

    # Crypto has no options on Alpaca
    if "/" in symbol:
        return []
    if regime == "unknown":
        logger.debug(f"options: {symbol} skipped — IV regime unknown (iv={iv_val})")
        return []
    if not (settings.OPTIONS_ENABLED
            and settings.OPTIONS_LEVEL >= 3
            and shared.MARKET_OPEN
            and not shared.EXTENDED_HOURS):
        return []

    close = ctx["close"]
    opt_strategy = iv_engine.select_strategy(ivr_data)
    logger.info(f"options: {symbol} IV={iv_val:.3f} regime={regime} ivr={ivr} -> {opt_strategy}")

    # Note: do NOT call _already_emitted here — the dedup loop
    # in _emit_equity_signals handles it.

    if opt_strategy == "iron_condor" and (ivr is None or ivr >= 30):
        sig = options_strategies.iron_condor(symbol, close)
        if sig:
            sig["confidence"] = min(0.5 + (ivr or 40) / 200, 0.9)
            sig["sentiment"] = round(sent_score, 3)
            sig["ivr"] = ivr
            return [sig]
        else:
            logger.info(f"options: {symbol} iron_condor selected but builder returned None")

    elif opt_strategy == "covered_call" and (ivr is None or ivr >= 20):
        with shared.positions_lock:
            holds = symbol in shared.positions
        if holds:
            sig = options_strategies.covered_call(symbol, close)
            if sig:
                sig["confidence"] = 0.70
                return [sig]
        else:
            logger.info(f"options: {symbol} covered_call skipped — not holding underlying")

    elif opt_strategy == "cash_secured_put":
        sig = options_strategies.cash_secured_put(symbol, close)
        if sig:
            sig["confidence"] = 0.65
            return [sig]
        else:
            logger.info(f"options: {symbol} cash_secured_put selected but builder returned None")

    elif opt_strategy == "calendar_spread" and (ivr is None or ivr <= 40):
        sig = options_strategies.calendar_spread(symbol, close)
        if sig:
            sig["confidence"] = 0.60
            return [sig]
        else:
            logger.info(f"options: {symbol} calendar_spread selected but builder returned None")

    return []


# ── options day-trade exit logic ─────────────────────────────────

def _check_options_exits() -> list:
    """Check open options positions for profit target, stop loss, or EOD exit.
    Returns list of close signals."""
    if not settings.OPTIONS_DAYTRADE:
        return []

    exit_signals = []
    opt_positions = options_strategies.get_options_positions()

    if not opt_positions:
        return []

    # Check EOD exit first (time-based, overrides everything)
    now = datetime.datetime.now()
    try:
        from alpaca_local import client as alpaca
        clock = alpaca.get_clock()
        if clock.next_close:
            close_time = clock.next_close.replace(tzinfo=None)
            mins_to_close = (close_time - now).total_seconds() / 60
            if 0 < mins_to_close <= settings.OPTIONS_EOD_EXIT_MINS:
                logger.info(f"options_exit: EOD exit — {mins_to_close:.0f}min to close, "
                            f"closing {len(opt_positions)} options positions")
                for pos in opt_positions:
                    sym = pos.symbol if hasattr(pos, 'symbol') else pos.get('symbol', '')
                    qty = float(pos.qty if hasattr(pos, 'qty') else pos.get('qty', 0))
                    close_side = "buy" if qty < 0 else "sell"
                    sig = options_strategies.build_close_signal(
                        sym, abs(qty), close_side, "options_exit")
                    sig["reason"] = "eod_exit"
                    exit_signals.append(sig)
                return exit_signals
    except Exception as e:
        logger.debug(f"options_exit: clock check failed: {e}")

    # Check profit target / stop loss on each position
    for pos in opt_positions:
        sym = pos.symbol if hasattr(pos, 'symbol') else pos.get('symbol', '')
        qty = float(pos.qty if hasattr(pos, 'qty') else pos.get('qty', 0))
        cost = float(pos.cost_basis if hasattr(pos, 'cost_basis') else pos.get('cost_basis', 0))
        mkt_val = float(pos.market_value if hasattr(pos, 'market_value') else pos.get('market_value', 0))
        unrealized_pl = float(pos.unrealized_pl if hasattr(pos, 'unrealized_pl') else pos.get('unrealized_pl', 0))

        if cost == 0:
            continue

        # For short positions (sold premium): profit = cost - mkt_val (both negative)
        # cost_basis for a short is negative (credit received)
        entry_credit = abs(cost)
        if entry_credit == 0:
            continue

        if qty < 0:
            # Short position: profit when current_value < entry_credit (premium decays)
            # loss when current_value > entry_credit (position moves against us)
            current_value = abs(mkt_val)
            profit_pct = 1.0 - (current_value / entry_credit) if entry_credit > 0 else 0
            # loss_pct is positive when losing (current_value grew beyond entry)
            loss_pct = max(0, (current_value / entry_credit) - 1.0) if entry_credit > 0 else 0
        else:
            # Long position: profit when market_value > cost
            profit_pct = (mkt_val - cost) / abs(cost) if cost != 0 else 0
            loss_pct = max(0, -profit_pct)

        close_side = "buy" if qty < 0 else "sell"

        # Profit target
        if profit_pct >= settings.OPTIONS_PROFIT_TARGET:
            logger.info(f"options_exit: PROFIT TARGET {sym} "
                        f"profit={profit_pct:.0%} >= {settings.OPTIONS_PROFIT_TARGET:.0%}")
            sig = options_strategies.build_close_signal(
                sym, abs(qty), close_side, "options_exit")
            sig["reason"] = "profit_target"
            exit_signals.append(sig)

        # Stop loss
        elif loss_pct >= settings.OPTIONS_STOP_LOSS:
            logger.info(f"options_exit: STOP LOSS {sym} "
                        f"loss={loss_pct:.0%} >= {settings.OPTIONS_STOP_LOSS:.0%}")
            sig = options_strategies.build_close_signal(
                sym, abs(qty), close_side, "options_exit")
            sig["reason"] = "stop_loss"
            exit_signals.append(sig)

    return exit_signals


# ── signal emission state ───────────────────────────────────────

_signals_emitted: dict = {}   # key -> timestamp of last emission
_signals_lock    = threading.Lock()
_SIGNAL_COOLDOWN         = 120  # seconds for equity signals
_OPTIONS_SIGNAL_COOLDOWN = 30   # seconds for options signals (faster cycling)
_OPTIONS_EXIT_COOLDOWN   = 10   # seconds for exit signals (very fast)

_OPTIONS_STRATEGIES = {"iron_condor", "covered_call", "cash_secured_put",
                       "calendar_spread", "auto_roll", "options_exit"}

def _clear_stale_signals():
    """Remove signals older than their cooldown so they can re-fire."""
    now = time.time()
    with _signals_lock:
        stale = [k for k, ts in _signals_emitted.items()
                 if now - ts > _cooldown_for(k)]
        for k in stale:
            del _signals_emitted[k]

def _cooldown_for(key: tuple) -> int:
    """Return cooldown seconds based on strategy type."""
    strategy = key[2] if len(key) > 2 else ""
    if strategy == "options_exit":
        return _OPTIONS_EXIT_COOLDOWN
    if strategy in _OPTIONS_STRATEGIES:
        return _OPTIONS_SIGNAL_COOLDOWN
    return _SIGNAL_COOLDOWN

def _already_emitted(symbol, side, strategy) -> bool:
    key = (symbol, side, strategy)
    now = time.time()
    cooldown = _cooldown_for(key)
    with _signals_lock:
        last_ts = _signals_emitted.get(key)
        if last_ts and (now - last_ts) < cooldown:
            return True
        _signals_emitted[key] = now
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

# -- strategy scoring feedback loop -------------------------------------------
_strategy_scores_cache: dict = {}  # strategy -> score dict (refreshed periodically)
_scores_last_refresh = 0.0

def _recency_weight(exit_ts_str) -> float:
    """Return exponential recency weight based on trade age.
    0-7 days: 1.0, 7-14 days: 0.7, 14-21 days: 0.4, older: 0.2
    """
    try:
        exit_dt = datetime.datetime.fromisoformat(str(exit_ts_str))
        if exit_dt.tzinfo is None:
            exit_dt = exit_dt.replace(tzinfo=datetime.timezone.utc)
        age_days = (datetime.datetime.now(datetime.timezone.utc) - exit_dt).total_seconds() / 86400
    except Exception:
        return 0.2  # unknown age gets lowest weight

    halflife = settings.SCORE_DECAY_HALFLIFE_DAYS
    if age_days <= halflife:
        return 1.0
    elif age_days <= halflife * 2:
        return 0.7
    elif age_days <= halflife * 3:
        return 0.4
    else:
        return 0.2


def _refresh_strategy_scores():
    """Compute recency-weighted strategy scores from closed outcomes and cache them."""
    global _strategy_scores_cache, _scores_last_refresh
    if time.time() - _scores_last_refresh < 300:  # refresh every 5 min
        return
    strategies = database.get_distinct_strategies()
    for strat in strategies:
        closed = database.get_closed_outcomes(strategy=strat, limit=50)
        if len(closed) < 2:
            continue

        # Compute recency weights
        weights = [_recency_weight(t.get("exit_ts")) for t in closed]
        total_w = sum(weights)
        if total_w <= 0:
            continue

        # Weighted win rate
        win_w = sum(w for t, w in zip(closed, weights) if (t.get("pnl") or 0) > 0)
        win_rate = win_w / total_w

        # Weighted average P&L
        returns = [t.get("pnl_pct", 0) or 0 for t in closed]
        avg_pnl = sum(r * w for r, w in zip(returns, weights)) / total_w

        # Weighted std dev -> Sharpe
        var = sum(w * (r - avg_pnl) ** 2 for r, w in zip(returns, weights)) / total_w
        std = math.sqrt(var)
        sharpe = avg_pnl / std if std > 0 else 0

        score = (0.4 * win_rate
                 + 0.3 * min(max(sharpe, 0), 2) / 2
                 + 0.3 * min(max(avg_pnl, 0), 0.1) / 0.1)
        database.write_strategy_score(strat, win_rate, avg_pnl, sharpe, len(closed), score)
        _strategy_scores_cache[strat] = {
            "win_rate": win_rate, "avg_pnl_pct": avg_pnl,
            "sharpe": sharpe, "trade_count": len(closed), "score": score,
        }
    _scores_last_refresh = time.time()

def _adjust_confidence(confidence: float, strategy: str) -> float:
    """Adjust signal confidence by historical strategy performance."""
    score_data = _strategy_scores_cache.get(strategy)
    if not score_data or score_data.get("trade_count", 0) < 10:
        return confidence  # not enough data
    # Scale confidence by 0.5x to 1.5x based on score (0-1)
    multiplier = 0.5 + score_data["score"]
    return min(round(confidence * multiplier, 3), 1.0)

# -- watchlist ----------------------------------------------------------------
def _get_watchlist() -> list:
    """Return current watchlist (set by boss from settings or Alpaca API)."""
    with shared.cache_lock:
        return list(shared.watchlist) if shared.watchlist else list(shared.ticker_list)

# -- signal emission ----------------------------------------------------------
def _build_ohlcv(symbol: str) -> dict:
    """Alias for shared.build_ohlcv — kept for backward compatibility."""
    return shared.build_ohlcv(symbol)

def _emit_equity_signals(symbols: list) -> list:
    if not (shared.MARKET_OPEN or shared.EXTENDED_HOURS):
        return []

    _clear_stale_signals()
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

        # -- Technical indicators --
        ohlcv  = _build_ohlcv(symbol)

        # Use stream bar close if available, else fall back to latest historical close
        if bar is not None:
            close = float(getattr(bar, "close", 0) or 0)
        else:
            hist_closes = ohlcv.get("closes", [])
            close = hist_closes[-1] if hist_closes else 0

        if close <= 0:
            continue
        closes = ohlcv.get("closes", [])
        highs  = ohlcv.get("highs", [])
        lows   = ohlcv.get("lows", [])
        volumes = ohlcv.get("volumes", [])

        # Append current bar for freshness (all arrays must stay same length)
        if closes:
            high = float(getattr(bar, "high", close) or close)
            low  = float(getattr(bar, "low", close) or close)
            vol  = float(getattr(bar, "volume", 0) or 0)
            closes  = closes + [close]
            highs   = highs + [high]
            lows    = lows + [low]
            volumes = volumes + [vol]

        ind = indicators.compute_all({
            "closes":  closes,
            "highs":   highs,
            "lows":    lows,
            "volumes": volumes,
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

        # -- Signal logic via strategy registry --
        signals_for_symbol = []

        # Sentiment is a confidence modifier, not a gate.
        sent_boost = sent_score * 0.15  # ±0.15 max swing

        # Build context dict for registered strategies
        ctx = {
            "ind": ind, "bar": bar, "close": close,
            "sent_score": sent_score, "sent_boost": sent_boost,
            "iv_val": iv_val, "ivr_data": ivr_data, "regime": regime,
        }

        # Dispatch all registered strategies
        for strat_name, strat_fn in _STRATEGY_REGISTRY.items():
            try:
                sigs = strat_fn(symbol, ctx)
                if sigs:
                    signals_for_symbol.extend(sigs)
            except Exception as e:
                logger.debug(f"strategy {strat_name} error for {symbol}: {e}")

        # Get active strategies for current regime
        with shared.cache_lock:
            current_regime = getattr(shared, "market_regime", "unknown")
        active_strategies = settings.REGIME_STRATEGY_MAP.get(current_regime)

        # Adjust confidence by strategy score, deduplicate, log to DB, add to batch
        for sig in signals_for_symbol:
            key_side = sig.get("side", "buy")
            key_strat = sig.get("strategy", "unknown")

            # Skip strategies not active for the current regime
            # Options strategies are exempt from regime filtering (they have their own IV-based gates)
            if (active_strategies is not None
                    and key_strat not in active_strategies
                    and key_strat not in _OPTIONS_STRATEGIES):
                logger.debug(f"signal_generator: skipping {key_strat} for {symbol} "
                             f"(not active in {current_regime} regime)")
                continue

            if not _already_emitted(symbol, key_side, key_strat):
                sig["confidence"] = _adjust_confidence(sig.get("confidence", 0.5), key_strat)
                database.write_signal(
                    ts=time.time(), symbol=symbol, strategy=key_strat,
                    side=key_side, confidence=sig.get("confidence"),
                    sentiment=sig.get("sentiment"), raw=str(sig),
                )
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


# -- options flow conviction boost --------------------------------------------
def _apply_flow_boost(signals: list):
    """Boost conviction by 0.15 for signals aligned with options flow direction."""
    with shared.cache_lock:
        alerts = list(getattr(shared, "options_flow_alerts", []))
    if not alerts:
        return

    # Build a map: symbol -> flow direction (bullish/bearish)
    flow_map = {}
    for alert in alerts:
        sym = alert.get("symbol")
        direction = alert.get("direction")
        if sym and direction:
            flow_map[sym] = direction

    for sig in signals:
        sym = sig.get("symbol")
        side = sig.get("side")
        if sym not in flow_map:
            continue
        flow_dir = flow_map[sym]
        aligned = (side == "buy" and flow_dir == "bullish") or \
                  (side == "sell" and flow_dir == "bearish")
        if aligned:
            old_conf = sig.get("confidence", 0.5)
            sig["confidence"] = min(round(old_conf + 0.15, 3), 1.0)
            sig["flow_boost"] = True
            logger.debug(f"signal_generator: flow boost {sym} {side} "
                         f"{old_conf:.3f} -> {sig['confidence']:.3f}")


# -- ensemble voting -----------------------------------------------------------
def _apply_ensemble_voting(signals: list) -> list:
    """
    Ensemble voting layer: for each symbol, aggregate signals from all strategies.

    Rules:
    - 2+ strategies agree on direction: emit composite signal with avg conviction * BONUS
    - Exactly 1 strategy fires: emit at original conviction * SOLO_PENALTY
    - Strategies disagree (bull + bear): suppress entirely
    """

    # Group signals by symbol
    by_symbol = defaultdict(list)
    for sig in signals:
        by_symbol[sig.get("symbol", "")].append(sig)

    result = []
    for symbol, sigs in by_symbol.items():
        bulls = [s for s in sigs if s.get("side") == "buy"]
        bears = [s for s in sigs if s.get("side") == "sell"]

        # Conflict: both bull and bear present — suppress
        if bulls and bears:
            logger.debug(
                f"signal_generator: ensemble conflict suppressed {symbol} "
                f"({len(bulls)} bull vs {len(bears)} bear)"
            )
            continue

        active = bulls or bears
        if not active:
            continue

        if len(active) >= settings.ENSEMBLE_MIN_AGREEMENT:
            # Agreement: composite signal with bonus
            avg_conf = sum(s.get("confidence", 0.5) for s in active) / len(active)
            composite = dict(active[0])  # base on first signal
            composite["confidence"] = min(round(avg_conf * settings.ENSEMBLE_AGREEMENT_BONUS, 3), 1.0)
            composite["ensemble"] = f"agreement_{len(active)}"
            composite["strategy"] = active[0].get("strategy", "unknown")
            # Preserve all contributing strategies in metadata
            composite["ensemble_strategies"] = [s.get("strategy") for s in active]
            result.append(composite)
        else:
            # Solo signal: apply penalty
            sig = dict(active[0])
            old_conf = sig.get("confidence", 0.5)
            sig["confidence"] = round(old_conf * settings.ENSEMBLE_SOLO_PENALTY, 3)
            sig["ensemble"] = "solo"
            result.append(sig)

    return result


# -- main loop ----------------------------------------------------------------
@shared.register_agent("signal_generator", phase=6)
def run():
    logger.info("signal_generator: starting")

    alpaca_stream.register_callback("stock",  _on_stock_data)
    alpaca_stream.register_callback("crypto", _on_crypto_data)
    alpaca_stream.register_callback("option", _on_option_data)
    alpaca_stream.register_callback("news",   _on_news)

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("signal_generator")
        if shared.RATE_LIMITED:
            time.sleep(settings.TICK_INTERVAL)
            continue

        symbols = _get_watchlist()
        _refresh_strategy_scores()

        # Refresh factory strategies (daily)
        try:
            strategy_factory.refresh_strategies()
        except Exception as e:
            logger.debug(f"signal_generator: factory refresh error: {e}")

        # Detect unusual options flow and boost aligned signals
        try:
            iv_engine.detect_unusual_flow(symbols)
        except Exception as e:
            logger.debug(f"signal_generator: options flow detection error: {e}")

        equity_signals = _emit_equity_signals(symbols)
        crypto_signals = _emit_crypto_signals()
        roll_signals   = _check_rolls() if shared.MARKET_OPEN else []

        # Options day-trade exit checks (profit target, stop loss, EOD)
        exit_signals = _check_options_exits() if shared.MARKET_OPEN else []
        if exit_signals:
            logger.info(f"signal_generator: routing {len(exit_signals)} options EXIT signals")
            from agents import order_execution
            for sig in exit_signals:
                try:
                    if not _already_emitted(sig["symbol"], sig["side"], "options_exit"):
                        order_execution.place_order(sig)
                except Exception as e:
                    logger.warning(f"signal_generator: options exit order failed [{sig.get('symbol')}]: {e}")

        # Generate factory strategy signals (live only — shadow logged internally)
        factory_signals = []
        if shared.MARKET_OPEN or shared.EXTENDED_HOURS:
            try:
                # Build market context for filter evaluation
                with shared.cache_lock:
                    regime = getattr(shared, "market_regime", "unknown")
                    flow_alerts = list(getattr(shared, "options_flow_alerts", []))
                try:
                    from agents import risk_manager as _rm
                    vix = _rm.get_last_vix()
                except Exception:
                    vix = 18.0

                flow_map = {}
                for a in flow_alerts:
                    s = a.get("symbol")
                    if s:
                        flow_map[s] = a.get("direction")

                with _data_lock:
                    bars_snap = dict(_bars)

                for symbol in symbols:
                    ohlcv = _build_ohlcv(symbol)
                    closes = ohlcv.get("closes", [])
                    if len(closes) < 14:
                        continue

                    bar = bars_snap.get(symbol)
                    close = float(getattr(bar, "close", 0) or 0) if bar else (
                        closes[-1] if closes else 0)
                    if close <= 0:
                        continue

                    ind = indicators.compute_all(ohlcv)
                    ind["_closes"] = closes

                    # Build per-symbol context
                    vol_ratio = 1.0
                    volumes = ohlcv.get("volumes", [])
                    if len(volumes) > 20:
                        avg_vol = sum(volumes[-20:]) / 20
                        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

                    ctx = {
                        "vix": vix,
                        "regime": regime,
                        "volume_ratio": vol_ratio,
                        "flow_direction": flow_map.get(symbol),
                    }

                    live_sigs, _ = strategy_factory.generate_factory_signals(
                        symbol, ind, ctx, close
                    )
                    factory_signals.extend(live_sigs)
            except Exception as e:
                logger.debug(f"signal_generator: factory signal generation error: {e}")

        all_signals = equity_signals + crypto_signals + roll_signals + factory_signals

        if all_signals:
            logger.info(f"signal_generator: {len(all_signals)} raw signals "
                        f"(eq={len(equity_signals)} crypto={len(crypto_signals)} "
                        f"roll={len(roll_signals)} factory={len(factory_signals)})")

        # Boost conviction for signals aligned with options flow
        _apply_flow_boost(all_signals)

        # Separate options signals (need direct order execution) from equity/crypto
        # (go through plan -> rebalance path)
        plan_signals = []
        options_signals = []
        for sig in all_signals:
            if sig.get("order_class") in ("mleg", "simple") and sig.get("strategy") in (
                "iron_condor", "covered_call", "cash_secured_put", "calendar_spread",
                "auto_roll", "options_exit"
            ):
                options_signals.append(sig)
            else:
                plan_signals.append(sig)

        # Apply ensemble voting before sending to plan manager
        if plan_signals:
            plan_signals = _apply_ensemble_voting(plan_signals)

        if plan_signals:
            try:
                plan_manager.update_plan(plan_signals, trigger="signal_batch")
            except Exception as e:
                logger.warning(f"signal_generator: plan_manager update failed: {e}")

        # Route options signals directly to order execution
        if options_signals:
            logger.info(f"signal_generator: routing {len(options_signals)} options signals to order_exec: "
                        f"{[(s.get('symbol'), s.get('strategy')) for s in options_signals]}")
            from agents import order_execution
            for sig in options_signals:
                try:
                    order_execution.place_order(sig)
                except Exception as e:
                    logger.warning(f"signal_generator: options order failed [{sig.get('symbol')}]: {e}")

        sleep_s = settings.TICK_INTERVAL if (shared.MARKET_OPEN or shared.EXTENDED_HOURS) \
                  else settings.OVERNIGHT_SLEEP
        time.sleep(sleep_s)

    logger.info("signal_generator: SHUTTING_DOWN - exiting")
