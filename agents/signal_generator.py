"""
agents/signal_generator.py -- Signal generation with real technical indicators,
IV rank, improved sentiment, and options strategy selection.
"""

import time
import logging
import threading
import math
import datetime
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


def get_strategy_registry():
    """Return a copy of the strategy registry for introspection."""
    return dict(_STRATEGY_REGISTRY)


# ── registered strategies ───────────────────────────────────────

def _vol_ratio(ctx) -> float:
    """Return current volume / 20-day avg volume. >1 means above average."""
    volumes = ctx.get("volumes", [])
    if len(volumes) < 21:
        return 1.0  # no data, assume normal
    avg_20 = sum(volumes[-21:-1]) / 20
    return volumes[-1] / avg_20 if avg_20 > 0 else 1.0


def _sma(closes, period):
    """Simple moving average of last N closes."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _weekly_trend(closes: list) -> str:
    """
    Determine the weekly trend from daily closes.
    Uses 5-week (25-day) momentum and 10-week (50-day) SMA direction.
    Returns: "bullish", "bearish", or "neutral"
    """
    if len(closes) < 50:
        return "neutral"

    # 5-week momentum: is price higher than 25 days ago?
    momentum_25 = (closes[-1] - closes[-25]) / closes[-25] if closes[-25] > 0 else 0

    # 50-day SMA slope: is it rising or falling?
    sma_50_now = sum(closes[-50:]) / 50
    sma_50_prev = sum(closes[-55:-5]) / 50 if len(closes) >= 55 else sma_50_now
    sma_slope = (sma_50_now - sma_50_prev) / sma_50_prev if sma_50_prev > 0 else 0

    # Price above/below 50-day SMA
    price_vs_sma = closes[-1] > sma_50_now

    if momentum_25 > 0.02 and sma_slope > 0 and price_vs_sma:
        return "bullish"
    elif momentum_25 < -0.02 and sma_slope < 0 and not price_vs_sma:
        return "bearish"
    return "neutral"


@register_strategy("rsi_oversold")
def _strat_rsi_oversold(symbol, ctx):
    """RSI oversold + non-bearish EMA + volume confirmation.
    Confidence: RSI 35→0.40, RSI 25→0.65, RSI 15→0.85, boosted by volume."""
    rsi_val = ctx["ind"].get("rsi")
    ema_d = ctx["ind"].get("ema_cross") or {}
    if not (rsi_val and rsi_val < settings.RSI_OVERSOLD
            and ema_d.get("cross") != "bearish"):
        return []
    rsi_depth = (settings.RSI_OVERSOLD - rsi_val) / settings.RSI_OVERSOLD
    vol_r = _vol_ratio(ctx)
    vol_boost = min((vol_r - 1.0) * 0.10, 0.10) if vol_r > 1.0 else 0.0
    # Wider range: 0.35 base → 0.85 max (was 0.55-0.95)
    confidence = 0.35 + rsi_depth * 0.45 + vol_boost + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "rsi_oversold",
    }]


@register_strategy("macd_cross")
def _strat_macd_cross(symbol, ctx):
    """MACD bullish crossover — requires above-average volume + ATR-scaled confidence.
    Histogram strength is normalized by ATR, not just price. Volume gate cuts spam ~50%."""
    macd_d = ctx["ind"].get("macd") or {}
    macd_hist = macd_d.get("histogram", 0)
    if macd_hist <= 0:
        return []

    # Volume gate: require volume >= 80% of 20-day average
    vol_r = _vol_ratio(ctx)
    if vol_r < 0.8:
        return []

    close = ctx["close"]
    atr_val = ctx.get("atr_val") or 0

    # ATR-scaled histogram strength (how significant is this crossover relative to volatility?)
    if atr_val and atr_val > 0:
        hist_strength = min(abs(macd_hist) / atr_val, 1.0)
    else:
        hist_strength = min(abs(macd_hist) / (close * 0.002 + 0.01), 1.0)

    # Volume bonus: strong volume adds confidence
    vol_boost = min((vol_r - 1.0) * 0.08, 0.12) if vol_r > 1.0 else 0.0

    # Wider range: 0.35 base for weak histogram, up to 0.80 for strong + high volume
    confidence = 0.35 + hist_strength * 0.30 + vol_boost + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 0.85),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "macd_cross",
    }]


@register_strategy("bb_bounce")
def _strat_bb_bounce(symbol, ctx):
    """Bollinger Band bounce — deeper below lower band = higher confidence.
    Added volume confirmation: high volume on the dip suggests institutional buying."""
    boll_d = ctx["ind"].get("bollinger") or {}
    pct_b = boll_d.get("pct_b", 0.5)
    if pct_b >= 0.15:
        return []
    bb_depth = (0.15 - pct_b) / 0.15
    vol_r = _vol_ratio(ctx)
    vol_boost = min((vol_r - 1.0) * 0.08, 0.10) if vol_r > 1.0 else 0.0
    # Wider range: 0.40 → 0.80
    confidence = 0.40 + bb_depth * 0.30 + vol_boost + ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "buy",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "bb_bounce",
    }]


@register_strategy("rsi_overbought")
def _strat_rsi_overbought(symbol, ctx):
    """RSI overbought = sell signal. Wider confidence range: RSI 70→0.35, RSI 85→0.65, RSI 95→0.85"""
    rsi_val = ctx["ind"].get("rsi")
    if not (rsi_val and rsi_val > settings.RSI_OVERBOUGHT):
        return []
    rsi_excess = (rsi_val - settings.RSI_OVERBOUGHT) / (100 - settings.RSI_OVERBOUGHT)
    # Wider range: 0.35 base for barely overbought, 0.85 for extreme
    confidence = 0.35 + rsi_excess * 0.50 - ctx["sent_boost"]
    return [{
        "symbol":     symbol,
        "side":       "sell",
        "confidence": min(max(round(confidence, 3), 0.3), 1.0),
        "sentiment":  round(ctx["sent_score"], 3),
        "strategy":   "rsi_overbought",
    }]


@register_strategy("vwap_reversion")
def _strat_vwap_reversion(symbol, ctx):
    """VWAP mean reversion — buy below VWAP, sell above.
    Trend filter: don't sell mean-reversion when EMA is bullish (fighting the trend)."""
    vwap_val = ctx["ind"].get("vwap")
    ema_d = ctx["ind"].get("ema_cross") or {}
    close = ctx["close"]
    if not vwap_val or vwap_val <= 0 or close <= 0:
        return []
    deviation = (close - vwap_val) / vwap_val

    # Buy when >2% below VWAP
    if deviation < -0.02:
        depth = min(abs(deviation) / 0.05, 1.0)
        confidence = 0.40 + depth * 0.30 + ctx["sent_boost"]
        return [{
            "symbol":     symbol,
            "side":       "buy",
            "confidence": min(max(round(confidence, 3), 0.3), 1.0),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "vwap_reversion",
        }]

    # Sell when >2% above VWAP — but NOT when EMA is bullish (don't fight the trend)
    if deviation > 0.02:
        if ema_d.get("cross") == "bullish":
            return []  # trend filter: skip sell against bullish momentum
        excess = min(deviation / 0.05, 1.0)
        confidence = 0.35 + excess * 0.25 - ctx["sent_boost"]
        return [{
            "symbol":     symbol,
            "side":       "sell",
            "confidence": min(max(round(confidence, 3), 0.3), 1.0),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "vwap_reversion",
        }]
    return []


@register_strategy("ema_trend")
def _strat_ema_trend(symbol, ctx):
    """EMA crossover trend — requires volume confirmation on the cross."""
    ema_d = ctx["ind"].get("ema_cross") or {}
    cross = ema_d.get("cross")
    if not cross or cross == "none":
        return []
    vol_r = _vol_ratio(ctx)
    if vol_r < 0.7:
        return []  # weak volume cross = not trustworthy
    vol_boost = min((vol_r - 1.0) * 0.10, 0.10) if vol_r > 1.0 else 0.0
    if cross == "bullish":
        confidence = 0.45 + vol_boost + ctx["sent_boost"]
        return [{
            "symbol":     symbol,
            "side":       "buy",
            "confidence": min(max(round(confidence, 3), 0.3), 1.0),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "ema_trend",
        }]
    elif cross == "bearish":
        confidence = 0.45 + vol_boost - ctx["sent_boost"]
        return [{
            "symbol":     symbol,
            "side":       "sell",
            "confidence": min(max(round(confidence, 3), 0.3), 1.0),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "ema_trend",
        }]
    return []


@register_strategy("momentum_confirm")
def _strat_momentum_confirm(symbol, ctx):
    """Momentum confirmation — price > 20-SMA + positive MACD + above-avg volume.
    High-conviction buy when multiple momentum factors align."""
    closes = ctx.get("closes", [])
    sma_20 = _sma(closes, 20)
    close = ctx["close"]
    if not sma_20 or close <= 0:
        return []
    macd_d = ctx["ind"].get("macd") or {}
    macd_hist = macd_d.get("histogram", 0)
    vol_r = _vol_ratio(ctx)

    # All three must confirm: price > SMA20, MACD positive, volume above average
    above_sma = close > sma_20
    macd_bullish = macd_hist > 0
    volume_strong = vol_r > 1.2

    if above_sma and macd_bullish and volume_strong:
        # Scale by how far above SMA and how strong the MACD is
        sma_pct = (close - sma_20) / sma_20
        sma_factor = min(sma_pct / 0.05, 1.0)  # 5% above SMA = max
        vol_factor = min((vol_r - 1.0) / 2.0, 0.5)
        confidence = 0.50 + sma_factor * 0.20 + vol_factor * 0.10 + ctx["sent_boost"]
        return [{
            "symbol":     symbol,
            "side":       "buy",
            "confidence": min(max(round(confidence, 3), 0.3), 0.90),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "momentum_confirm",
        }]
    return []


@register_strategy("profit_take")
def _strat_profit_take(symbol, ctx):
    """Exit signal — suggest profit-taking when RSI > 65 on a profitable long position.
    Also triggers when price is > 3x ATR above recent low (extended move)."""
    rsi_val = ctx["ind"].get("rsi")
    atr_val = ctx.get("atr_val")
    close = ctx["close"]
    closes = ctx.get("closes", [])

    if not rsi_val or not close:
        return []

    # Check if we hold this symbol
    with shared.positions_lock:
        pos = shared.positions.get(symbol)
    if pos is None:
        return []
    qty = float(getattr(pos, "qty", 0) or (pos.get("qty", 0) if isinstance(pos, dict) else 0))
    if qty <= 0:
        return []  # only exit longs
    unrealized = float(getattr(pos, "unrealized_pl", 0) or
                       (pos.get("unrealised", 0) if isinstance(pos, dict) else 0))
    if unrealized <= 0:
        return []  # only take profits, not losses

    signals = []

    # RSI profit-taking: suggest partial exit when RSI > 65 with profit
    if rsi_val > 65:
        rsi_heat = (rsi_val - 65) / 35  # 0 at 65, 1 at 100
        confidence = 0.35 + rsi_heat * 0.30
        signals.append({
            "symbol":     symbol,
            "side":       "sell",
            "confidence": min(max(round(confidence, 3), 0.3), 0.75),
            "sentiment":  round(ctx["sent_score"], 3),
            "strategy":   "profit_take",
        })

    # ATR extension: price moved > 3x ATR above 10-day low = extended, take profits
    if atr_val and atr_val > 0 and len(closes) >= 10:
        recent_low = min(closes[-10:])
        extension = (close - recent_low) / atr_val
        if extension > 3.0:
            ext_factor = min((extension - 3.0) / 3.0, 1.0)  # 6x ATR = max
            confidence = 0.40 + ext_factor * 0.25
            signals.append({
                "symbol":     symbol,
                "side":       "sell",
                "confidence": min(max(round(confidence, 3), 0.3), 0.70),
                "sentiment":  round(ctx["sent_score"], 3),
                "strategy":   "profit_take",
            })

    return signals


@register_strategy("options_iv")
def _strat_options_iv(symbol, ctx):
    """Options signals based on IV regime (iron condor, covered call, CSP, calendar)."""
    regime = ctx["regime"]
    iv_val = ctx["iv_val"]
    ivr_data = ctx["ivr_data"]
    ivr = ivr_data.get("ivr")
    sent_score = ctx["sent_score"]

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

    if opt_strategy == "iron_condor" and (ivr is None or ivr >= 50):
        sig = options_strategies.iron_condor(symbol, close)
        if sig:
            sig["confidence"] = min(0.5 + (ivr or 50) / 200, 0.9)
            sig["sentiment"] = round(sent_score, 3)
            sig["ivr"] = ivr
            return [sig]

    elif opt_strategy == "covered_call" and (ivr is None or ivr >= 35):
        with shared.positions_lock:
            holds = symbol in shared.positions
        if holds:
            sig = options_strategies.covered_call(symbol, close)
            if sig:
                sig["confidence"] = 0.70
                return [sig]

    elif opt_strategy == "cash_secured_put":
        sig = options_strategies.cash_secured_put(symbol, close)
        if sig:
            sig["confidence"] = 0.65
            return [sig]

    elif opt_strategy == "calendar_spread" and (ivr is None or ivr <= 30):
        sig = options_strategies.calendar_spread(symbol, close)
        if sig:
            sig["confidence"] = 0.60
            return [sig]

    return []


# ── signal emission state ───────────────────────────────────────

_signals_emitted: dict = {}   # key -> timestamp of last emission
_signals_lock    = threading.Lock()
_SIGNAL_COOLDOWN = 300  # seconds before same signal can fire again

def _clear_stale_signals():
    """Remove signals older than _SIGNAL_COOLDOWN so they can re-fire."""
    cutoff = time.time() - _SIGNAL_COOLDOWN
    with _signals_lock:
        stale = [k for k, ts in _signals_emitted.items() if ts < cutoff]
        for k in stale:
            del _signals_emitted[k]

def _already_emitted(symbol, side, strategy) -> bool:
    key = (symbol, side, strategy)
    now = time.time()
    with _signals_lock:
        last_ts = _signals_emitted.get(key)
        if last_ts and (now - last_ts) < _SIGNAL_COOLDOWN:
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


def _seed_news_from_db():
    """Seed _news dict from stored news so sentiment scores are non-zero at startup."""
    try:
        conn = database.get_connection()
        rows = conn.execute(
            "SELECT headline, summary, symbols FROM news ORDER BY ts DESC LIMIT 200"
        ).fetchall()
        seeded = 0

        class _NewsStub:
            """Lightweight stand-in for score_news_events compatibility."""
            def __init__(self, h, s):
                self.headline = h
                self.summary = s

        for row in rows:
            symbols_str = row["symbols"] or ""
            headline = row["headline"] or ""
            summary = row["summary"] or ""
            if not headline:
                continue
            stub = _NewsStub(headline, summary)
            for sym in symbols_str.split(","):
                sym = sym.strip()
                if not sym:
                    continue
                with _data_lock:
                    if sym not in _news:
                        _news[sym] = []
                    if len(_news[sym]) < 10:
                        _news[sym].append(stub)
                        seeded += 1
        logger.info(f"signal_generator: seeded {seeded} news items from DB for sentiment")
    except Exception as e:
        logger.debug(f"signal_generator: news seed from DB failed: {e}")

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

        # -- Sentiment (check both BTC/USD and BTCUSD formats for crypto) --
        news_events = news.get(symbol, [])
        if not news_events and "/" in symbol:
            news_events = news.get(shared.normalize_crypto_noslash(symbol), [])
        elif not news_events and symbol.endswith("USD"):
            news_events = news.get(shared.normalize_crypto(symbol), [])
        sent_score = sentiment.score_news_events(news_events)

        # -- IV / options regime --
        iv_val   = iv_engine.estimate_iv_from_chain(symbol, close)
        ivr_data = iv_engine.get_ivr(symbol, iv_val)
        regime   = ivr_data.get("regime", "unknown")
        ivr      = ivr_data.get("ivr")

        # -- Signal logic via strategy registry --
        signals_for_symbol = []

        # Sentiment is a confidence modifier, not a gate.
        sent_boost = sent_score * 0.15  # ±0.15 max swing

        # Compute weekly trend for multi-timeframe confirmation
        weekly = _weekly_trend(closes)

        # Build context dict for registered strategies
        ctx = {
            "ind": ind, "bar": bar, "close": close,
            "closes": closes, "volumes": volumes,
            "atr_val": atr_val,
            "sent_score": sent_score, "sent_boost": sent_boost,
            "iv_val": iv_val, "ivr_data": ivr_data, "regime": regime,
            "weekly_trend": weekly,
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

        # Multi-timeframe filter: adjust confidence based on weekly trend alignment
        for sig in signals_for_symbol:
            sig_side = sig.get("side", "buy")
            if weekly == "bullish" and sig_side == "buy":
                sig["confidence"] = min(round(sig.get("confidence", 0.5) * 1.10, 3), 1.0)
                sig["weekly_aligned"] = True
            elif weekly == "bearish" and sig_side == "sell":
                sig["confidence"] = min(round(sig.get("confidence", 0.5) * 1.10, 3), 1.0)
                sig["weekly_aligned"] = True
            elif weekly == "bullish" and sig_side == "sell":
                sig["confidence"] = round(sig.get("confidence", 0.5) * 0.85, 3)
                sig["weekly_aligned"] = False
            elif weekly == "bearish" and sig_side == "buy":
                sig["confidence"] = round(sig.get("confidence", 0.5) * 0.85, 3)
                sig["weekly_aligned"] = False

        # Adjust confidence by strategy score, deduplicate, log to DB, add to batch
        for sig in signals_for_symbol:
            key_side = sig.get("side", "buy")
            key_strat = sig.get("strategy", "unknown")

            # Skip strategies not active for the current regime
            if active_strategies is not None and key_strat not in active_strategies:
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

        # Check both BTC/USD and BTCUSD formats for news
        crypto_news = news.get(symbol, [])
        if not crypto_news:
            crypto_news = news.get(shared.normalize_crypto_noslash(symbol), [])
        sent = sentiment.score_news_events(crypto_news)

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
    from collections import defaultdict

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

    # Seed news from DB so sentiment is non-zero on startup
    _seed_news_from_db()

    # Force-load shadow/live strategies immediately (don't wait for hourly refresh)
    try:
        strategy_factory.refresh_strategies(force=True)
    except Exception as e:
        logger.debug(f"signal_generator: initial factory refresh error: {e}")

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

        # Boost conviction for signals aligned with options flow
        _apply_flow_boost(all_signals)

        # Separate options signals (need direct order execution) from equity/crypto
        # (go through plan -> rebalance path)
        plan_signals = []
        options_signals = []
        for sig in all_signals:
            if sig.get("order_class") in ("mleg", "simple") and sig.get("strategy") in (
                "iron_condor", "covered_call", "cash_secured_put", "calendar_spread", "auto_roll"
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
