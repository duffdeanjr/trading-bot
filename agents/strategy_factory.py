"""
agents/strategy_factory.py -- Composable strategy factory with promotion lifecycle.

Generates candidate strategies from primitives (entry x filter x exit),
runs them through a 4-state promotion pipeline:
  candidate -> shadow -> live -> retired

Shadow strategies emit signals to shadow_signals table for paper evaluation.
Live strategies feed into signal_generator alongside hand-coded strategies.
Claude API generates novel hypotheses weekly.
"""

import os
import time
import json
import math
import logging
import datetime
import threading

import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

# ── promotion thresholds ─────────────────────────────────────────
BACKTEST_MIN_SHARPE = 0.5
BACKTEST_MIN_WIN_RATE = 0.45
BACKTEST_MIN_TRADES = 20
SHADOW_MIN_DAYS = 14
SHADOW_PROMOTE_SHARPE = 0.6
SHADOW_PROMOTE_WIN_RATE = 0.48
RETIRE_SHARPE_THRESHOLD = -0.5
RETIRE_CONSECUTIVE_BAD = 3
HYPOTHESIS_INTERVAL = 604800  # 7 days
EVAL_INTERVAL = 14400  # 4 hours

CLAUDE_MODEL = "claude-sonnet-4-20250514"

# ── 3a: strategy primitives ─────────────────────────────────────

ENTRY_PRIMITIVES = {
    "rsi_oversold": {"indicator": "rsi", "condition": "lt", "default_threshold": 35},
    "rsi_overbought": {"indicator": "rsi", "condition": "gt", "default_threshold": 70},
    "macd_crossover_bull": {"indicator": "macd_histogram", "condition": "gt", "default_threshold": 0},
    "macd_crossover_bear": {"indicator": "macd_histogram", "condition": "lt", "default_threshold": 0},
    "bollinger_lower_touch": {"indicator": "pct_b", "condition": "lt", "default_threshold": 0.15},
    "bollinger_upper_touch": {"indicator": "pct_b", "condition": "gt", "default_threshold": 0.85},
    "ema_trend_up": {"indicator": "ema_cross", "condition": "eq", "default_threshold": "bullish"},
    "ema_trend_down": {"indicator": "ema_cross", "condition": "eq", "default_threshold": "bearish"},
    "vwap_below": {"indicator": "vwap_ratio", "condition": "lt", "default_threshold": 0.99},
    "vwap_above": {"indicator": "vwap_ratio", "condition": "gt", "default_threshold": 1.01},
}

FILTER_PRIMITIVES = {
    "vix_low": {"check": "vix", "condition": "lt", "default_threshold": 20},
    "vix_high": {"check": "vix", "condition": "gt", "default_threshold": 30},
    "regime_trending": {"check": "regime", "condition": "in", "default_threshold": ["trending-bull", "trending-bear"]},
    "regime_ranging": {"check": "regime", "condition": "eq", "default_threshold": "ranging"},
    "volume_above_avg": {"check": "volume_ratio", "condition": "gt", "default_threshold": 1.2},
    "options_flow_bullish": {"check": "flow_direction", "condition": "eq", "default_threshold": "bullish"},
    "options_flow_bearish": {"check": "flow_direction", "condition": "eq", "default_threshold": "bearish"},
}

EXIT_PRIMITIVES = {
    "rsi_recovery": {"indicator": "rsi", "condition": "between", "default_threshold": [40, 60]},
    "macd_cross_reverse": {"indicator": "macd_histogram", "condition": "sign_change", "default_threshold": 0},
    "stop_loss_pct": {"indicator": "pnl_pct", "condition": "lt", "default_threshold": -0.03},
    "take_profit_pct": {"indicator": "pnl_pct", "condition": "gt", "default_threshold": 0.05},
    "time_exit_eod": {"indicator": "time", "condition": "eod", "default_threshold": None},
}


def _make_recipe_id(entry: str, filter_cond: str, exit_cond: str) -> str:
    return f"factory_{entry}_{filter_cond}_{exit_cond}"


# ── primitive evaluation functions ───────────────────────────────

def check_entry(entry_name: str, indicators: dict, params: dict = None) -> bool:
    """Evaluate an entry primitive against computed indicators."""
    prim = ENTRY_PRIMITIVES.get(entry_name)
    if not prim:
        return False

    ind_key = prim["indicator"]
    cond = prim["condition"]
    threshold = (params or {}).get(f"{entry_name}_threshold", prim["default_threshold"])

    if ind_key == "rsi":
        val = indicators.get("rsi")
        if val is None:
            return False
        return (val < threshold) if cond == "lt" else (val > threshold)

    elif ind_key == "macd_histogram":
        macd = indicators.get("macd") or {}
        val = macd.get("histogram", 0)
        return (val > threshold) if cond == "gt" else (val < threshold)

    elif ind_key == "pct_b":
        boll = indicators.get("bollinger") or {}
        val = boll.get("pct_b", 0.5)
        return (val < threshold) if cond == "lt" else (val > threshold)

    elif ind_key == "ema_cross":
        ema = indicators.get("ema_cross") or {}
        return ema.get("cross") == threshold

    elif ind_key == "vwap_ratio":
        vwap = indicators.get("vwap")
        closes = indicators.get("_closes", [])
        if not vwap or not closes or vwap <= 0:
            return False
        ratio = closes[-1] / vwap
        return (ratio < threshold) if cond == "lt" else (ratio > threshold)

    return False


def check_filter(filter_name: str, context: dict, params: dict = None) -> bool:
    """Evaluate a filter condition against market context."""
    prim = FILTER_PRIMITIVES.get(filter_name)
    if not prim:
        return True  # unknown filter = pass through

    check = prim["check"]
    cond = prim["condition"]
    threshold = (params or {}).get(f"{filter_name}_threshold", prim["default_threshold"])

    if check == "vix":
        vix = context.get("vix", 20)
        return (vix < threshold) if cond == "lt" else (vix > threshold)

    elif check == "regime":
        regime = context.get("regime", "unknown")
        if cond == "in":
            return regime in threshold
        return regime == threshold

    elif check == "volume_ratio":
        vol_ratio = context.get("volume_ratio", 1.0)
        return vol_ratio > threshold

    elif check == "flow_direction":
        flow = context.get("flow_direction")
        return flow == threshold

    return True


def check_exit(exit_name: str, indicators: dict, entry_price: float,
               current_price: float, params: dict = None) -> bool:
    """Evaluate an exit condition."""
    prim = EXIT_PRIMITIVES.get(exit_name)
    if not prim:
        return False

    ind_key = prim["indicator"]
    threshold = (params or {}).get(f"{exit_name}_threshold", prim["default_threshold"])

    if ind_key == "rsi":
        val = indicators.get("rsi")
        if val is None:
            return False
        low, high = threshold if isinstance(threshold, list) else [40, 60]
        return low <= val <= high

    elif ind_key == "macd_histogram":
        macd = indicators.get("macd") or {}
        val = macd.get("histogram", 0)
        # Sign change from entry
        return True  # simplified: any crossover triggers

    elif ind_key == "pnl_pct":
        if entry_price <= 0:
            return False
        pnl_pct = (current_price - entry_price) / entry_price
        return (pnl_pct < threshold) if prim["condition"] == "lt" else (pnl_pct > threshold)

    elif ind_key == "time":
        # EOD exit
        now = datetime.datetime.now()
        return now.hour >= 15 and now.minute >= 45  # 3:45 PM

    return False


# ── 3a: generate all valid combinations ──────────────────────────

def generate_all_candidates():
    """Generate all entry x filter x exit combinations and insert as candidates."""
    existing_ids = database.get_all_strategy_recipe_ids()
    created = 0

    for entry in ENTRY_PRIMITIVES:
        for filter_cond in FILTER_PRIMITIVES:
            for exit_cond in EXIT_PRIMITIVES:
                recipe_id = _make_recipe_id(entry, filter_cond, exit_cond)
                if recipe_id in existing_ids:
                    continue

                # Determine side from entry
                bullish_entries = {"rsi_oversold", "macd_crossover_bull",
                                   "bollinger_lower_touch", "ema_trend_up", "vwap_below"}
                side = "buy" if entry in bullish_entries else "sell"

                params = {
                    "side": side,
                    "entry": ENTRY_PRIMITIVES[entry],
                    "filter": FILTER_PRIMITIVES[filter_cond],
                    "exit": EXIT_PRIMITIVES[exit_cond],
                }

                database.write_strategy_recipe(
                    recipe_id=recipe_id,
                    entry=entry,
                    filter_cond=filter_cond,
                    exit_cond=exit_cond,
                    params=params,
                )
                created += 1

    if created > 0:
        logger.info(f"strategy_factory: generated {created} new candidate strategies")
    return created


# ── 3b: promotion lifecycle ──────────────────────────────────────

def _backtest_candidate(recipe: dict) -> dict:
    """
    Run a 60-day backtest on a candidate strategy using historical OHLCV.
    Returns {"sharpe": float, "win_rate": float, "trade_count": int}.
    """
    from agents import indicators as ind_mod

    entry_name = recipe["entry"]
    exit_name = recipe["exit"]
    params = json.loads(recipe["params"]) if isinstance(recipe["params"], str) else (recipe["params"] or {})
    side = params.get("side", "buy")

    # Get symbols from watchlist
    with shared.cache_lock:
        symbols = list(shared.watchlist) if shared.watchlist else list(shared.ticker_list)

    trades = []
    for symbol in symbols[:10]:  # limit to first 10 for speed
        with shared.cache_lock:
            hist = shared.historical_ohlcv.get(symbol, {})
        if isinstance(hist, dict):
            closes = hist.get("closes", [])
            highs = hist.get("highs", [])
            lows = hist.get("lows", [])
            volumes = hist.get("volumes", [])
        else:
            continue

        if len(closes) < 60:
            continue

        # Walk through the last 60 days of data
        in_trade = False
        entry_price = 0.0

        for i in range(30, len(closes)):
            window = {
                "closes": closes[max(0, i-30):i+1],
                "highs": highs[max(0, i-30):i+1],
                "lows": lows[max(0, i-30):i+1],
                "volumes": volumes[max(0, i-30):i+1],
            }
            computed = ind_mod.compute_all(window)
            computed["_closes"] = window["closes"]

            if not in_trade:
                if check_entry(entry_name, computed, params):
                    in_trade = True
                    entry_price = closes[i]
            else:
                if check_exit(exit_name, computed, entry_price, closes[i], params):
                    pnl_pct = (closes[i] - entry_price) / entry_price
                    if side == "sell":
                        pnl_pct = -pnl_pct
                    trades.append(pnl_pct)
                    in_trade = False

    if len(trades) < 5:
        return {"sharpe": 0, "win_rate": 0, "trade_count": len(trades)}

    wins = sum(1 for t in trades if t > 0)
    avg = sum(trades) / len(trades)
    std = math.sqrt(sum((t - avg) ** 2 for t in trades) / len(trades))
    sharpe = avg / std if std > 0 else 0

    return {
        "sharpe": round(sharpe, 3),
        "win_rate": round(wins / len(trades), 3),
        "trade_count": len(trades),
    }


def promote_candidates():
    """Evaluate candidate strategies and promote qualifying ones to shadow."""
    candidates = database.get_strategies_by_status("candidate")
    promoted = 0

    for recipe in candidates[:20]:  # batch limit
        result = _backtest_candidate(recipe)

        database.update_strategy_status(
            recipe["id"], "candidate",
            backtest_sharpe=result["sharpe"],
            backtest_win_rate=result["win_rate"],
        )

        if (result["sharpe"] >= BACKTEST_MIN_SHARPE
                and result["win_rate"] >= BACKTEST_MIN_WIN_RATE
                and result["trade_count"] >= BACKTEST_MIN_TRADES):
            database.update_strategy_status(
                recipe["id"], "shadow",
                shadow_start_ts=time.time(),
            )
            logger.info(
                f"strategy_factory: PROMOTED {recipe['id']} candidate -> shadow "
                f"(sharpe={result['sharpe']:.2f}, win_rate={result['win_rate']:.2f})"
            )
            promoted += 1

    return promoted


def promote_shadows():
    """Evaluate shadow strategies and promote qualifying ones to live."""
    shadows = database.get_strategies_by_status("shadow")
    promoted = 0

    for recipe in shadows:
        shadow_start = recipe.get("shadow_start_ts") or 0
        days_in_shadow = (time.time() - shadow_start) / 86400

        if days_in_shadow < SHADOW_MIN_DAYS:
            continue

        stats = database.get_shadow_signal_stats(recipe["id"])
        if not stats or stats["count"] < 10:
            continue

        if (stats["sharpe"] >= SHADOW_PROMOTE_SHARPE
                and stats["win_rate"] >= SHADOW_PROMOTE_WIN_RATE):
            database.update_strategy_status(
                recipe["id"], "live",
                shadow_sharpe=stats["sharpe"],
                shadow_days=int(days_in_shadow),
            )
            logger.info(
                f"strategy_factory: PROMOTED {recipe['id']} shadow -> live "
                f"(shadow_sharpe={stats['sharpe']:.2f})"
            )
            promoted += 1

    return promoted


def evaluate_live_strategies():
    """Daily walk-forward evaluation of live strategies. Retire underperformers."""
    live = database.get_strategies_by_status("live")

    for recipe in live:
        stats = database.get_shadow_signal_stats(recipe["id"])
        if not stats or stats["count"] < 5:
            continue

        bad_count = recipe.get("consecutive_bad_evaluations") or 0

        if stats["sharpe"] < RETIRE_SHARPE_THRESHOLD:
            bad_count += 1
        else:
            bad_count = 0

        if bad_count >= RETIRE_CONSECUTIVE_BAD:
            reason = f"walk_forward_sharpe_{stats['sharpe']:.2f}_x{bad_count}"
            database.update_strategy_status(
                recipe["id"], "retired",
                retired_reason=reason,
                consecutive_bad_evaluations=bad_count,
            )
            logger.warning(f"strategy_factory: RETIRED {recipe['id']} (reason={reason})")
        else:
            database.update_strategy_status(
                recipe["id"], recipe["status"],
                consecutive_bad_evaluations=bad_count,
                shadow_sharpe=stats["sharpe"],
            )


def reset_strategy(recipe_id: str, reason: str):
    """
    Manual-only: reset a retired strategy back to candidate.
    This is a deliberate safety gate -- a strategy that failed live
    should not re-enter without human review.
    """
    recipe = database.get_strategy_recipe(recipe_id)
    if not recipe:
        logger.error(f"strategy_factory: reset failed -- {recipe_id} not found")
        return
    if recipe["status"] != "retired":
        logger.error(f"strategy_factory: reset failed -- {recipe_id} is {recipe['status']}, not retired")
        return

    database.update_strategy_status(
        recipe_id, "candidate",
        retired_reason=None,
        consecutive_bad_evaluations=0,
        shadow_start_ts=None,
        shadow_sharpe=None,
        shadow_days=0,
    )
    logger.warning(f"strategy_factory: MANUAL RESET {recipe_id} -> candidate (reason={reason})")


# ── 3c: shadow signal evaluator ──────────────────────────────────

def _evaluate_shadow_signals():
    """
    Every 4 hours: evaluate unevaluated shadow signals.
    Compute simulated P&L using actual market prices from historical_ohlcv.
    """
    cutoff = time.time() - EVAL_INTERVAL
    rows = database.get_unevaluated_shadow_signals(older_than_ts=cutoff)
    if not rows:
        return

    evaluated = 0
    for row in rows:
        symbol = row["symbol"]
        entry_price = row["entry_price"]
        signal_ts = row["ts"]

        if not entry_price or entry_price <= 0:
            database.update_shadow_signal(row["id"], 0, 0)
            continue

        # Get current/recent price for exit
        with shared.cache_lock:
            hist = shared.historical_ohlcv.get(symbol, {})
        closes = hist.get("closes", []) if isinstance(hist, dict) else []

        if closes:
            exit_price = closes[-1]
        else:
            exit_price = entry_price  # no data = flat

        side = row.get("side", "buy")
        if side == "buy":
            pnl = (exit_price - entry_price) / entry_price
        else:
            pnl = (entry_price - exit_price) / entry_price

        database.update_shadow_signal(row["id"], exit_price, round(pnl, 6))
        evaluated += 1

    if evaluated > 0:
        logger.info(f"strategy_factory: evaluated {evaluated} shadow signals")


# ── 3d: Claude strategy hypothesis generator ─────────────────────

_HYPOTHESIS_PROMPT = """You are a quantitative strategy researcher. Given the indicator primitives available and the performance history provided, propose novel trading strategy combinations that might have edge in the current market regime. For each proposal explain the market microstructure hypothesis -- why this combination should work, not just that it might. Respond in JSON only:
{
  "proposals": [
    {
      "entry": "primitive_name",
      "filter": "primitive_name",
      "exit": "primitive_name",
      "params": {},
      "hypothesis": "string -- specific market mechanism explanation",
      "target_regime": "trending-bull | trending-bear | ranging | high-vol"
    }
  ]
}"""


def _generate_hypotheses():
    """
    Weekly: call Claude API to propose novel strategy combinations.
    Insert as candidate strategies with hypothesis populated.
    """
    try:
        import anthropic
    except ImportError:
        return

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return

    # Build context for Claude
    live_strategies = database.get_strategies_by_status("live")
    retired_strategies = database.get_strategies_by_status("retired")

    # Top 5 live by sharpe
    top_live = sorted(live_strategies, key=lambda x: x.get("shadow_sharpe") or 0, reverse=True)[:5]
    # Bottom 3 retired with reasons
    bottom_retired = retired_strategies[:3]

    with shared.cache_lock:
        regime = getattr(shared, "market_regime", "unknown")

    context = {
        "available_entries": list(ENTRY_PRIMITIVES.keys()),
        "available_filters": list(FILTER_PRIMITIVES.keys()),
        "available_exits": list(EXIT_PRIMITIVES.keys()),
        "top_live_strategies": [
            {"id": s["id"], "sharpe": s.get("shadow_sharpe"), "entry": s["entry"],
             "filter": s["filter"], "exit": s["exit"]}
            for s in top_live
        ],
        "retired_strategies": [
            {"id": s["id"], "reason": s.get("retired_reason"), "entry": s["entry"],
             "filter": s["filter"], "exit": s["exit"]}
            for s in bottom_retired
        ],
        "current_regime": regime,
    }

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=_HYPOTHESIS_PROMPT,
            messages=[{"role": "user", "content": json.dumps(context, default=str)}],
        )

        response_text = ""
        for block in message.content:
            if hasattr(block, "text"):
                response_text += block.text

        result = json.loads(response_text)
        proposals = result.get("proposals", [])

        existing_ids = database.get_all_strategy_recipe_ids()
        inserted = 0

        for p in proposals:
            entry = p.get("entry", "")
            filter_cond = p.get("filter", "")
            exit_cond = p.get("exit", "")

            if entry not in ENTRY_PRIMITIVES:
                continue
            if filter_cond not in FILTER_PRIMITIVES:
                continue
            if exit_cond not in EXIT_PRIMITIVES:
                continue

            recipe_id = _make_recipe_id(entry, filter_cond, exit_cond)
            if recipe_id in existing_ids:
                continue

            database.write_strategy_recipe(
                recipe_id=recipe_id,
                entry=entry,
                filter_cond=filter_cond,
                exit_cond=exit_cond,
                params=p.get("params", {}),
                hypothesis=p.get("hypothesis"),
                target_regime=p.get("target_regime"),
            )
            existing_ids.add(recipe_id)
            inserted += 1

        logger.info(f"strategy_factory: Claude proposed {len(proposals)} strategies, "
                     f"inserted {inserted} new candidates")

    except Exception as e:
        logger.error(f"strategy_factory: hypothesis generation failed: {e}")


# ── 3e: signal generation for live/shadow strategies ─────────────

_live_strategies_cache: list = []
_shadow_strategies_cache: list = []
_strategies_last_refresh = 0.0


def refresh_strategies():
    """Reload live and shadow strategies from DB. Called by signal_generator."""
    global _live_strategies_cache, _shadow_strategies_cache, _strategies_last_refresh
    if time.time() - _strategies_last_refresh < 86400:  # daily
        return
    _live_strategies_cache = database.get_strategies_by_status("live")
    _shadow_strategies_cache = database.get_strategies_by_status("shadow")
    _strategies_last_refresh = time.time()
    logger.info(f"strategy_factory: refreshed {len(_live_strategies_cache)} live, "
                f"{len(_shadow_strategies_cache)} shadow strategies")


def get_live_strategies() -> list:
    """Return cached live strategies."""
    return list(_live_strategies_cache)


def get_shadow_strategies() -> list:
    """Return cached shadow strategies."""
    return list(_shadow_strategies_cache)


def generate_factory_signals(symbol: str, indicators: dict,
                             context: dict, close: float) -> tuple:
    """
    Generate signals for a symbol from factory strategies.
    Returns (live_signals, shadow_signals) where shadow_signals
    are logged but NOT sent to plan_manager.
    """
    live_signals = []
    shadow_signals = []

    params_default = {}

    for recipe in _live_strategies_cache:
        recipe_params = json.loads(recipe["params"]) if isinstance(recipe["params"], str) else (recipe["params"] or {})
        if check_filter(recipe["filter"], context, recipe_params):
            if check_entry(recipe["entry"], indicators, recipe_params):
                side = recipe_params.get("side", "buy")
                live_signals.append({
                    "symbol": symbol,
                    "side": side,
                    "confidence": 0.55,
                    "strategy": recipe["id"],
                    "factory": True,
                })

    for recipe in _shadow_strategies_cache:
        recipe_params = json.loads(recipe["params"]) if isinstance(recipe["params"], str) else (recipe["params"] or {})
        if check_filter(recipe["filter"], context, recipe_params):
            if check_entry(recipe["entry"], indicators, recipe_params):
                side = recipe_params.get("side", "buy")
                # Log to shadow_signals table -- NOT to plan_manager
                database.write_shadow_signal(
                    ts=time.time(),
                    strategy_id=recipe["id"],
                    symbol=symbol,
                    side=side,
                    conviction=0.55,
                    entry_price=close,
                )

    return live_signals, shadow_signals


# ── main loop (background lifecycle management) ──────────────────

@shared.register_agent("strategy_factory", phase=7)
def run():
    """
    Background daemon for strategy lifecycle management.
    - Generate candidates on first run
    - Promote candidates -> shadow (daily)
    - Evaluate shadow signals (every 4h)
    - Promote shadow -> live (daily)
    - Evaluate live strategies (daily)
    - Generate Claude hypotheses (weekly)
    """
    logger.info("strategy_factory: starting")

    shared.ref_ready_event.wait(timeout=120)
    shared.account_ready_event.wait(timeout=60)

    # Generate initial candidates
    try:
        generate_all_candidates()
    except Exception as e:
        logger.error(f"strategy_factory: initial generation failed: {e}")

    last_promotion = 0.0
    last_eval = 0.0
    last_hypothesis = 0.0

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("strategy_factory")
        now = time.time()

        # Daily: promotions and lifecycle evaluation
        if now - last_promotion >= 86400:
            try:
                promote_candidates()
                promote_shadows()
                evaluate_live_strategies()
                refresh_strategies()
            except Exception as e:
                logger.error(f"strategy_factory: promotion cycle error: {e}")
            last_promotion = now

        # Every 4h: evaluate shadow signals
        if now - last_eval >= EVAL_INTERVAL:
            try:
                _evaluate_shadow_signals()
            except Exception as e:
                logger.error(f"strategy_factory: shadow eval error: {e}")
            last_eval = now

        # Weekly: Claude hypothesis generation
        if now - last_hypothesis >= HYPOTHESIS_INTERVAL:
            try:
                _generate_hypotheses()
            except Exception as e:
                logger.error(f"strategy_factory: hypothesis generation error: {e}")
            last_hypothesis = now

        # Sleep 60s, checking shutdown
        for _ in range(12):
            if shared.SHUTTING_DOWN:
                break
            time.sleep(5)

    logger.info("strategy_factory: SHUTTING_DOWN - exiting")
