"""
agents/screener.py -- Dynamic stock screener.

Scans Alpaca's full equity universe, scores candidates using technical
indicators, and feeds promotions/demotions to boss.py for watchlist
management.  Runs as a supervised daemon thread.

Two-tier pipeline:
  Tier 1 (daily)   — filter shared.assets to ~2500 tradable equities
  Tier 2 (15 min)  — batch-fetch 30-day bars, score with indicators,
                      promote/demote from active watchlist
"""

import time
import random
import logging
import datetime

import shared
from config import settings
from storage import database
from agents import indicators

logger = logging.getLogger(__name__)

# -- module state --
_universe: list = []       # filtered symbol list
_universe_ts: float = 0.0  # when universe was last built
_UNIVERSE_TTL = 4 * 3600   # rebuild every 4 hours
_scan_offset: int = 0      # rotating index into _universe

# Leveraged / inverse / SPAC exclusions
_EXCLUDED_SYMBOLS = {
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SDS", "QLD", "QID",
    "UVXY", "SVXY", "LABU", "LABD", "SOXL", "SOXS", "TNA", "TZA",
    "FNGU", "FNGD", "JNUG", "JDST", "ERX", "ERY", "NUGT", "DUST",
}


def _build_universe() -> list:
    """
    Filter shared.assets to tradable, liquid equities.
    Uses exchange + shortable + easy_to_borrow as liquidity proxy
    (no market cap data in Alpaca API).
    """
    global _universe, _universe_ts

    if _universe and (time.time() - _universe_ts) < _UNIVERSE_TTL:
        return _universe

    with shared.cache_lock:
        all_assets = list(shared.assets) if isinstance(shared.assets, list) else []
        if isinstance(shared.assets, dict):
            all_assets = list(shared.assets.values())

    if not all_assets:
        logger.warning("screener: shared.assets is empty — cannot build universe")
        return _universe or []

    valid_exchanges = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}
    candidates = []

    for a in all_assets:
        sym = getattr(a, "symbol", "") or (a.get("symbol", "") if isinstance(a, dict) else "")
        if not sym or sym in _EXCLUDED_SYMBOLS:
            continue

        # Symbol format: 1-5 uppercase letters only (no warrants, units, preferred)
        if not sym.isalpha() or len(sym) > 5:
            continue

        # Handle Alpaca enum fields (e.g. AssetExchange.NYSE -> "NYSE")
        raw_exchange = getattr(a, "exchange", "") if not isinstance(a, dict) else a.get("exchange", "")
        exchange = (raw_exchange.value if hasattr(raw_exchange, "value") else str(raw_exchange)).upper()
        if exchange not in valid_exchanges:
            continue

        tradable = getattr(a, "tradable", False) if not isinstance(a, dict) else a.get("tradable", False)
        shortable = getattr(a, "shortable", False) if not isinstance(a, dict) else a.get("shortable", False)
        raw_status = getattr(a, "status", "") if not isinstance(a, dict) else a.get("status", "")
        status = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()

        if not tradable:
            continue
        if status != "active":
            continue
        # shortable = liquidity proxy (most illiquid/micro-caps aren't shortable)
        if not shortable:
            continue

        candidates.append(sym)

    # Shuffle so we don't always screen the same subset first
    random.shuffle(candidates)
    _universe = candidates
    _universe_ts = time.time()
    logger.info(f"screener: built universe of {len(candidates)} symbols "
                f"(from {len(all_assets)} total assets)")
    return _universe


def _fetch_bars_batch(symbols: list) -> dict:
    """
    Fetch 30-day daily bars for a batch of symbols.
    Returns {symbol: {"closes": [], "highs": [], "lows": [], "volumes": []}}.
    """
    if not symbols:
        return {}

    if shared.RATE_LIMITED:
        return {}

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(settings.APCA_KEY, settings.APCA_SECRET)
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=settings.SCREENER_HISTORY_DAYS)

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=settings.DATA_FEED,
        )
        barset = client.get_stock_bars(req)
    except Exception as e:
        logger.warning(f"screener: bar fetch failed: {e}")
        return {}

    result = {}
    try:
        # barset is a BarSet keyed by symbol
        if hasattr(barset, "data"):
            items = barset.data.items()
        elif hasattr(barset, "items"):
            items = barset.items()
        else:
            items = barset if isinstance(barset, dict) else []

        for sym, bars in items:
            ohlcv = {"closes": [], "highs": [], "lows": [], "volumes": []}
            bar_list = bars if isinstance(bars, list) else list(bars)
            for b in bar_list:
                try:
                    ohlcv["closes"].append(float(getattr(b, "close", 0) or 0))
                    ohlcv["highs"].append(float(getattr(b, "high", 0) or 0))
                    ohlcv["lows"].append(float(getattr(b, "low", 0) or 0))
                    ohlcv["volumes"].append(float(getattr(b, "volume", 0) or 0))
                except Exception:
                    continue
            if len(ohlcv["closes"]) >= 14:  # minimum for RSI
                result[sym] = ohlcv
    except Exception as e:
        logger.warning(f"screener: bar parsing failed: {e}")

    return result


def _score_symbol(ohlcv: dict) -> tuple:
    """
    Score a symbol 0.0-1.0 using technical indicators.
    Returns (score, reasons_string).
    """
    ind = indicators.compute_all(ohlcv)
    closes = ohlcv.get("closes", [])
    if not closes:
        return 0.0, ""

    score = 0.0
    reasons = []

    # RSI
    rsi_val = ind.get("rsi")
    if rsi_val is not None:
        if rsi_val < 35:
            score += 0.20
            reasons.append(f"RSI={rsi_val:.0f}")
        elif rsi_val < 45:
            score += 0.10
            reasons.append(f"RSI={rsi_val:.0f}")

    # MACD
    macd_d = ind.get("macd") or {}
    hist = macd_d.get("histogram", 0)
    if hist > 0:
        score += 0.15
        reasons.append("MACD+")

    # Bollinger
    boll_d = ind.get("bollinger") or {}
    pct_b = boll_d.get("pct_b")
    if pct_b is not None and pct_b < 0.20:
        score += 0.15
        reasons.append(f"BB%={pct_b:.2f}")

    # EMA cross
    ema_d = ind.get("ema_cross") or {}
    if ema_d.get("cross") == "bullish":
        score += 0.15
        reasons.append("EMA_cross")

    # ATR volatility (want 2-8% of close)
    atr_val = ind.get("atr")
    close = closes[-1]
    if atr_val and close > 0:
        atr_pct = atr_val / close
        if 0.02 <= atr_pct <= 0.08:
            score += 0.10
            reasons.append(f"ATR={atr_pct:.1%}")

    # Volume
    volumes = ohlcv.get("volumes", [])
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:]) / 20
        if avg_vol > 500_000:
            score += 0.05
            reasons.append(f"vol={avg_vol/1e6:.1f}M")
        if volumes[-1] > avg_vol * 1.5 and avg_vol > 0:
            score += 0.05
            reasons.append("hi_vol")

    return round(score, 3), ", ".join(reasons)


def _screen_batch(symbols: list) -> list:
    """Fetch bars and score a batch. Returns [(symbol, score, reasons), ...]."""
    bars = _fetch_bars_batch(symbols)
    results = []
    for sym, ohlcv in bars.items():
        score, reasons = _score_symbol(ohlcv)
        results.append((sym, score, reasons))
        database.write_screener_score(sym, time.time(), score, reasons,
                                      promoted=(score >= settings.SCREENER_PROMOTE_THRESHOLD))
    return results


def _evaluate_current_watchlist() -> list:
    """
    Re-score current watchlist symbols using cached historical_ohlcv.
    Returns list of symbols to demote.
    """
    with shared.cache_lock:
        wl = list(shared.watchlist)
        core = set(settings.WATCHLIST + settings.CRYPTO_WATCHLIST)

    with shared.positions_lock:
        held = set(shared.positions.keys())

    plan_syms = set()
    with shared.cache_lock:
        plan = shared.investment_plan
        if plan and isinstance(plan, dict):
            plan_syms = set(plan.get("symbols", {}).keys())

    demotions = []
    for sym in wl:
        # Never demote core watchlist, held positions, or plan symbols
        if sym in core or sym in held or sym in plan_syms:
            continue

        with shared.cache_lock:
            raw = shared.historical_ohlcv.get(sym, {})

        # Convert bar list to dict if needed
        if isinstance(raw, list):
            ohlcv = {"closes": [], "highs": [], "lows": [], "volumes": []}
            for b in raw:
                try:
                    ohlcv["closes"].append(float(getattr(b, "close", 0) or 0))
                    ohlcv["highs"].append(float(getattr(b, "high", 0) or 0))
                    ohlcv["lows"].append(float(getattr(b, "low", 0) or 0))
                    ohlcv["volumes"].append(float(getattr(b, "volume", 0) or 0))
                except Exception:
                    continue
        elif isinstance(raw, dict) and "closes" in raw:
            ohlcv = raw
        else:
            continue

        if len(ohlcv.get("closes", [])) < 14:
            continue

        score, _ = _score_symbol(ohlcv)
        if score < settings.SCREENER_DEMOTE_THRESHOLD:
            # Check minimum tenure
            with shared.cache_lock:
                cand = shared.screener_candidates.get(sym, {})
            promoted_ts = cand.get("ts", 0)
            if time.time() - promoted_ts > settings.SCREENER_MIN_TENURE_S:
                demotions.append(sym)

    return demotions


@shared.register_agent("screener", phase=6, condition=lambda: settings.SCREENER_ENABLED)
def run():
    """Main screener loop."""
    logger.info("screener: starting — waiting for ref_ready_event")
    shared.ref_ready_event.wait()
    logger.info("screener: ref data available, building universe")

    global _scan_offset

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("screener")

        try:
            universe = _build_universe()
            if not universe:
                logger.warning("screener: empty universe, sleeping")
                time.sleep(60)
                continue

            # -- Tier 2: batch scoring --
            batch_size = settings.SCREENER_BATCH_SIZE
            n_batches = settings.SCREENER_BATCHES_PER_CYCLE
            promoted_this_cycle = 0
            max_promotions = 3

            for _ in range(n_batches):
                if shared.SHUTTING_DOWN or shared.RATE_LIMITED:
                    break

                # Rotate through universe
                start = _scan_offset % len(universe)
                batch = universe[start:start + batch_size]
                _scan_offset = (start + batch_size) % len(universe)

                results = _screen_batch(batch)

                for sym, score, reasons in results:
                    if score >= settings.SCREENER_PROMOTE_THRESHOLD and promoted_this_cycle < max_promotions:
                        with shared.cache_lock:
                            if sym not in shared.screener_candidates:
                                shared.screener_candidates[sym] = {
                                    "score": score,
                                    "ts": time.time(),
                                    "reasons": reasons,
                                }
                                promoted_this_cycle += 1
                                logger.info(f"screener: promoting {sym} "
                                            f"score={score:.2f} ({reasons})")
                            else:
                                # Update score for existing candidate
                                shared.screener_candidates[sym]["score"] = score
                                shared.screener_candidates[sym]["reasons"] = reasons

                # Small delay between batches to be kind to API
                time.sleep(2)

            # -- Evaluate existing watchlist for demotions --
            demotions = _evaluate_current_watchlist()
            max_demotions = 2
            if demotions:
                with shared.cache_lock:
                    shared.screener_demotions = demotions[:max_demotions]
                for sym in demotions[:max_demotions]:
                    logger.info(f"screener: demoting {sym}")

            with shared.cache_lock:
                shared.screener_last_run = time.time()

            n_cands = len(shared.screener_candidates)
            logger.info(f"screener: cycle complete — scanned {n_batches * batch_size} symbols, "
                        f"{promoted_this_cycle} promoted, {len(demotions)} demoted, "
                        f"{n_cands} total candidates")

        except Exception as e:
            logger.error(f"screener: cycle error: {e}")

        # Sleep until next cycle
        if shared.MARKET_OPEN:
            time.sleep(settings.SCREENER_INTERVAL)
        elif shared.EXTENDED_HOURS:
            time.sleep(settings.SCREENER_INTERVAL * 2)
        else:
            time.sleep(settings.SCREENER_INTERVAL * 4)

    logger.info("screener: SHUTTING_DOWN")
