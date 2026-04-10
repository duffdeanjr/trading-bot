import time
import json
import math
import logging
import datetime
import threading

import shared
from config import settings
from storage.database import (
    write_investment_plan, read_latest_plan, get_strategy_score,
    get_closed_outcomes,
)

logger = logging.getLogger(__name__)

PLAN_REFRESH_INTERVAL = 3600
PLAN_COOLDOWN = 15
REVIEW_INTERVAL = 90  # seconds between continuous plan reviews

_lock = threading.Lock()
_last_update = 0.0


def _empty_plan() -> dict:
    return {
        "version": 0,
        "updated_at": None,
        "trigger": None,
        "stance": "neutral",
        "cash_target_pct": 0.10,
        "sector_targets": {},
        "symbols": {},
        "exclusions": [],
        "notes": [],
    }


def load_plan() -> dict:
    row = read_latest_plan()
    if row:
        try:
            plan = json.loads(row["plan_json"])
            logger.debug(f"plan_manager: loaded plan v{row['version']}")
            return plan
        except Exception as e:
            logger.warning(f"plan_manager: could not parse stored plan: {e}")
    return _empty_plan()


def save_plan(plan: dict, trigger: str, summary: str):
    ts = time.time()
    plan["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    plan["trigger"] = trigger
    plan_json = json.dumps(plan, default=str)
    version = write_investment_plan(ts, plan_json, trigger=trigger, summary=summary)
    plan["version"] = version
    with shared.cache_lock:
        shared.investment_plan = plan
    logger.info(f"plan_manager: saved plan v{version} | trigger={trigger} | {summary}")


def _get_portfolio_equity() -> float:
    with shared.account_lock:
        acct = shared.account
    if acct is None:
        return 0.0
    try:
        return float(getattr(acct, "equity", 0) or 0)
    except Exception:
        return 0.0


def _get_current_positions() -> dict:
    with shared.positions_lock:
        positions = dict(shared.positions)
    result = {}
    for sym, pos in positions.items():
        try:
            result[sym] = float(getattr(pos, "market_value", 0) or 0)
        except Exception:
            result[sym] = 0.0
    return result


def _infer_sector(symbol: str) -> str:
    tech     = {"AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","AMD","INTC","CRM","ADBE","ORCL"}
    finance  = {"JPM","BAC","WFC","GS","MS","C","BLK","AXP","V","MA"}
    energy   = {"XOM","CVX","COP","SLB","EOG","PXD","MPC","VLO"}
    health   = {"JNJ","UNH","PFE","ABBV","MRK","LLY","TMO","DHR","ABT"}
    if symbol in tech:    return "technology"
    if symbol in finance: return "financials"
    if symbol in energy:  return "energy"
    if symbol in health:  return "healthcare"
    if "/" in symbol:     return "crypto"
    return "other"


def _get_dirty_symbols() -> set:
    """Read dirty_symbols snapshot under cache_lock (for use outside _lock)."""
    with shared.cache_lock:
        return set(shared.dirty_symbols)

def _check_corp_action_exclusions(plan: dict, dirty: set = None) -> list:
    if dirty is None:
        dirty = _get_dirty_symbols()
    exclusions = []
    for sym in dirty:
        if sym not in plan["exclusions"]:
            exclusions.append(sym)
            logger.info(f"plan_manager: excluding {sym} due to pending corp action")
    return exclusions


def _compute_stance(signals: list) -> str:
    if not signals:
        return "neutral"
    buy_conf  = sum(s.get("confidence", 0.5) for s in signals if s.get("side") == "buy")
    sell_conf = sum(s.get("confidence", 0.5) for s in signals if s.get("side") == "sell")
    total = buy_conf + sell_conf
    if total == 0:
        return "neutral"
    ratio = buy_conf / total
    if ratio >= 0.65: return "risk-on"
    if ratio <= 0.35: return "risk-off"
    return "neutral"


def _apply_risk_constraints(plan: dict) -> dict:
    max_pct = settings.MAX_PORTFOLIO_PCT
    for sym, entry in plan["symbols"].items():
        if entry["target_pct"] > max_pct:
            entry["target_pct"] = max_pct
            entry["reason"] += " [capped by risk limit]"
    max_invested = 1.0 - plan["cash_target_pct"]
    total = sum(e["target_pct"] for e in plan["symbols"].values())
    if total > max_invested and total > 0:
        scale = max_invested / total
        for entry in plan["symbols"].values():
            entry["target_pct"] *= scale
        plan["notes"].append(f"scaled positions by {scale:.2f} to maintain cash buffer")
    return plan


def update_plan(signals: list, trigger: str = "signal_batch") -> dict:
    global _last_update

    # Gather shared state OUTSIDE the lock to avoid nested lock acquisitions
    now = time.time()
    with _lock:
        if now - _last_update < PLAN_COOLDOWN and trigger == "signal_batch":
            logger.debug("plan_manager: skipping update (cooldown)")
            return _get_current_plan()
        _last_update = now

    plan = load_plan()
    positions = _get_current_positions()
    dirty_snapshot = _get_dirty_symbols()
    notes = []

    old_stance = plan["stance"]
    plan["stance"] = _compute_stance(signals)
    if plan["stance"] != old_stance:
        notes.append(f"stance: {old_stance} -> {plan['stance']}")

    if plan["stance"] == "risk-off":
        plan["cash_target_pct"] = 0.20
    elif plan["stance"] == "risk-on":
        plan["cash_target_pct"] = 0.05
    else:
        plan["cash_target_pct"] = 0.10

    new_exclusions = _check_corp_action_exclusions(plan, dirty_snapshot)
    for sym in new_exclusions:
        plan["exclusions"].append(sym)
        if sym in plan["symbols"]:
            del plan["symbols"][sym]
            notes.append(f"removed {sym}: corp action")

    for sig in signals:
        sym  = sig.get("symbol")
        side = sig.get("side")
        conf = float(sig.get("confidence", 0.5))
        sentiment_val = float(sig.get("sentiment", 0.0))
        strategy  = sig.get("strategy", "unknown")
        if not sym or sym in plan["exclusions"]:
            continue

        # Adjust conviction by strategy score (feedback loop)
        score_row = get_strategy_score(strategy)
        if score_row and (score_row.get("trade_count") or 0) >= 10:
            s = score_row.get("score", 0.5)
            if s < 0.3:
                conf *= 0.5
            elif s > 0.7:
                conf *= 1.2
            conf = min(conf, 1.0)

        sector = _infer_sector(sym)
        if side == "buy":
            if sym not in plan["symbols"]:
                plan["symbols"][sym] = {
                    "conviction":  conf,
                    "target_pct":  min(conf * 0.10, settings.MAX_PORTFOLIO_PCT),
                    "sector":      sector,
                    "strategy":    strategy,
                    "reason":      f"{strategy} conf={conf:.2f} sent={sentiment_val:.2f}",
                    "added_at":    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                notes.append(f"added {sym} target={plan['symbols'][sym]['target_pct']:.1%}")
            else:
                entry = plan["symbols"][sym]
                entry["conviction"] = entry["conviction"] * 0.7 + conf * 0.3
                entry["target_pct"] = min(entry["target_pct"] * 1.1, settings.MAX_PORTFOLIO_PCT)
                entry["reason"] = f"updated: {strategy} conf={conf:.2f}"
        elif side == "sell":
            if sym in plan["symbols"]:
                entry = plan["symbols"][sym]
                entry["conviction"] *= 0.5
                entry["target_pct"] *= 0.5
                entry["reason"] = f"sell signal: {strategy}"
                if entry["target_pct"] < 0.005:
                    del plan["symbols"][sym]
                    notes.append(f"removed {sym}: low conviction")

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    stale = []
    for sym, entry in plan["symbols"].items():
        added = entry.get("added_at")
        if added:
            try:
                age_days = (now_dt - datetime.datetime.fromisoformat(added)).days
                if age_days > 7 and sym not in positions:
                    stale.append(sym)
            except Exception:
                pass
    for sym in stale:
        del plan["symbols"][sym]
        notes.append(f"pruned stale: {sym}")

    sector_weights = {}
    for entry in plan["symbols"].values():
        sec = entry.get("sector", "other")
        sector_weights[sec] = sector_weights.get(sec, 0) + entry["target_pct"]
    plan["sector_targets"] = sector_weights

    plan = _apply_risk_constraints(plan)

    if notes:
        plan["notes"] = (plan.get("notes", []) + notes)[-50:]

    n = len(plan["symbols"])
    summary = f"{n} symbols | stance={plan['stance']} | cash={plan['cash_target_pct']:.0%}"

    # Save OUTSIDE the lock
    save_plan(plan, trigger=trigger, summary=summary)
    return plan


def _get_current_plan() -> dict:
    with shared.cache_lock:
        plan = getattr(shared, "investment_plan", None)
    if plan is None:
        plan = load_plan()
        with shared.cache_lock:
            shared.investment_plan = plan
    return plan


def get_targets() -> dict:
    plan = _get_current_plan()
    return {sym: entry["target_pct"] for sym, entry in plan.get("symbols", {}).items()}


def get_exclusions() -> list:
    return _get_current_plan().get("exclusions", [])


def get_stance() -> str:
    return _get_current_plan().get("stance", "neutral")


def _scheduled_refresh():
    plan = _get_current_plan()
    notes = []

    with shared.cache_lock:
        calendar = list(shared.calendar)
    # Check if the NEXT market open date is >1 day away (actual holiday gap)
    today = datetime.date.today()
    if calendar:
        try:
            next_open = None
            for c in calendar:
                c_date = c if isinstance(c, datetime.date) else datetime.date.fromisoformat(str(c)[:10])
                if c_date >= today:
                    next_open = c_date
                    break
            if next_open and (next_open - today).days > 1:
                notes.append(f"market holiday: next open {next_open}, raising cash target")
                plan["cash_target_pct"] = min(plan["cash_target_pct"] + 0.05, 0.25)
        except Exception:
            pass  # don't let calendar parsing break the refresh

    with shared.cache_lock:
        corp_actions = list(shared.corp_actions)
    plan_symbols = set(plan.get("symbols", {}).keys())
    for action in corp_actions[:200]:
        action_str = str(action)
        for sym in list(plan_symbols):
            if sym in action_str and sym not in plan["exclusions"]:
                plan["exclusions"].append(sym)
                if sym in plan["symbols"]:
                    del plan["symbols"][sym]
                notes.append(f"excluded {sym}: corp action found in ref library")
                plan_symbols.discard(sym)
                break

    if notes:
        plan["notes"] = (plan.get("notes", []) + notes)[-50:]
        save_plan(plan, trigger="scheduled_refresh",
                  summary=f"ref refresh: {len(notes)} changes")
        logger.info(f"plan_manager: scheduled refresh — {len(notes)} adjustments")
    else:
        logger.debug("plan_manager: scheduled refresh — no changes")


def _build_ohlcv(symbol: str) -> dict:
    """Convert historical_ohlcv data (may be list of Bar objects) to indicator-ready dict."""
    with shared.cache_lock:
        hist = shared.historical_ohlcv.get(symbol, {})
    if isinstance(hist, dict) and "closes" in hist:
        return hist
    if isinstance(hist, list):
        result = {"closes": [], "highs": [], "lows": [], "volumes": []}
        for b in hist:
            try:
                result["closes"].append(float(getattr(b, "close", getattr(b, "c", 0)) or 0))
                result["highs"].append(float(getattr(b, "high", getattr(b, "h", 0)) or 0))
                result["lows"].append(float(getattr(b, "low", getattr(b, "l", 0)) or 0))
                result["volumes"].append(float(getattr(b, "volume", getattr(b, "v", 0)) or 0))
            except Exception:
                continue
        return result
    return {}


def _continuous_review():
    """
    Proactive plan review that runs every REVIEW_INTERVAL seconds.
    Re-evaluates existing positions using live indicators and P&L,
    adjusts targets, and discovers new opportunities from watchlist.
    """
    from agents import indicators, iv_engine, sentiment as sent_mod

    plan = _get_current_plan()
    if not plan.get("symbols") and not (shared.MARKET_OPEN or shared.EXTENDED_HOURS):
        return

    notes = []
    equity = _get_portfolio_equity()
    if equity <= 0:
        return
    positions = _get_current_positions()

    # -- 1. Review held positions: adjust conviction by live technicals + P&L --
    removals = []
    for sym, entry in list(plan.get("symbols", {}).items()):
        # Get live indicator data (convert bar list to dict if needed)
        ohlcv = _build_ohlcv(sym)
        closes = ohlcv.get("closes", [])
        if len(closes) < 14:
            continue

        ind = indicators.compute_all(ohlcv)

        rsi_val = ind.get("rsi")
        macd_d = ind.get("macd") or {}
        ema_d = ind.get("ema_cross") or {}
        old_conviction = entry.get("conviction", 0.5)

        # Compute a technical health score for the position (0.0 to 1.0)
        health = 0.5
        if rsi_val is not None:
            if rsi_val < 30:
                health += 0.15          # deeply oversold = opportunity
            elif rsi_val < 45:
                health += 0.05
            elif rsi_val > 75:
                health -= 0.20          # overbought = danger
            elif rsi_val > 65:
                health -= 0.05
        if macd_d.get("histogram", 0) > 0:
            health += 0.10              # bullish momentum
        elif macd_d.get("histogram", 0) < 0:
            health -= 0.10
        if ema_d.get("cross") == "bullish":
            health += 0.10
        elif ema_d.get("cross") == "bearish":
            health -= 0.10
        health = max(0.0, min(1.0, health))

        # Blend old conviction with technical health (70% old, 30% new)
        new_conviction = old_conviction * 0.7 + health * 0.3
        entry["conviction"] = round(new_conviction, 3)

        # Adjust target based on conviction change
        if new_conviction > old_conviction + 0.05:
            entry["target_pct"] = min(entry["target_pct"] * 1.15, settings.MAX_PORTFOLIO_PCT)
            notes.append(f"{sym}: conviction up {old_conviction:.2f}->{new_conviction:.2f}")
        elif new_conviction < old_conviction - 0.1:
            entry["target_pct"] *= 0.80
            notes.append(f"{sym}: conviction down {old_conviction:.2f}->{new_conviction:.2f}")

        # Check live P&L for held positions — scale winners, cut losers
        mv = positions.get(sym, 0.0)
        if mv != 0 and sym in positions:
            with shared.positions_lock:
                pos = shared.positions.get(sym)
            if pos is not None:
                try:
                    unrealized_pct = float(getattr(pos, "unrealized_plpc", 0) or 0)
                except Exception:
                    unrealized_pct = 0.0

                # Winner: scale up target slightly (let profits run)
                if unrealized_pct > 0.10:
                    entry["target_pct"] = min(entry["target_pct"] * 1.10, settings.MAX_PORTFOLIO_PCT)
                    entry["reason"] = f"winner +{unrealized_pct:.0%}, scaling up"
                # Loser beyond -8%: reduce target (cut losses)
                elif unrealized_pct < -0.08:
                    entry["target_pct"] *= 0.60
                    entry["reason"] = f"loser {unrealized_pct:.0%}, cutting"
                    if entry["target_pct"] < 0.005:
                        removals.append(sym)

        # Remove positions where conviction has collapsed
        if entry["conviction"] < 0.15:
            removals.append(sym)

    for sym in set(removals):
        if sym in plan["symbols"]:
            del plan["symbols"][sym]
            notes.append(f"removed {sym}: conviction/P&L too low")

    # -- 2. Discover new opportunities from watchlist not already in plan --
    if shared.MARKET_OPEN or shared.EXTENDED_HOURS:
        with shared.cache_lock:
            watchlist = list(shared.watchlist) if shared.watchlist else list(shared.ticker_list)
        plan_syms = set(plan.get("symbols", {}).keys())
        exclusions = set(plan.get("exclusions", []))

        for sym in watchlist:
            if sym in plan_syms or sym in exclusions:
                continue
            ohlcv = _build_ohlcv(sym)
            closes = ohlcv.get("closes", [])
            if len(closes) < 20:
                continue

            ind = indicators.compute_all(ohlcv)
            rsi_val = ind.get("rsi")
            macd_d = ind.get("macd") or {}
            boll_d = ind.get("bollinger") or {}
            ema_d = ind.get("ema_cross") or {}

            # Score the opportunity
            score = 0.0
            reasons = []
            if rsi_val and rsi_val < settings.RSI_OVERSOLD:
                score += 0.3
                reasons.append(f"RSI={rsi_val:.0f}")
            if macd_d.get("histogram", 0) > 0:
                score += 0.2
                reasons.append("MACD+")
            if boll_d.get("pct_b", 0.5) < 0.20:
                score += 0.2
                reasons.append(f"BB%={boll_d.get('pct_b',0):.2f}")
            if ema_d.get("cross") == "bullish":
                score += 0.2
                reasons.append("EMA_cross")

            # Add if strong enough (at least 2 confirming signals)
            if score >= 0.4:
                sector = _infer_sector(sym)
                target = min(score * 0.10, settings.MAX_PORTFOLIO_PCT)
                plan["symbols"][sym] = {
                    "conviction":  round(score, 3),
                    "target_pct":  target,
                    "sector":      sector,
                    "strategy":    "review_discovery",
                    "reason":      f"auto-review: {', '.join(reasons)}",
                    "added_at":    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                notes.append(f"discovered {sym} score={score:.2f} ({', '.join(reasons)})")

    # -- 3. Re-evaluate stance from current plan positions --
    if plan.get("symbols"):
        avg_conviction = sum(
            e.get("conviction", 0.5) for e in plan["symbols"].values()
        ) / len(plan["symbols"])
        if avg_conviction >= 0.6:
            new_stance = "risk-on"
        elif avg_conviction <= 0.3:
            new_stance = "risk-off"
        else:
            new_stance = "neutral"
        if new_stance != plan.get("stance"):
            notes.append(f"stance: {plan.get('stance')} -> {new_stance} (avg_conv={avg_conviction:.2f})")
            plan["stance"] = new_stance
            if new_stance == "risk-off":
                plan["cash_target_pct"] = 0.20
            elif new_stance == "risk-on":
                plan["cash_target_pct"] = 0.05
            else:
                plan["cash_target_pct"] = 0.10

    # -- 4. Apply risk constraints and save if anything changed --
    plan = _apply_risk_constraints(plan)

    if notes:
        plan["notes"] = (plan.get("notes", []) + notes)[-50:]
        n = len(plan["symbols"])
        summary = f"review: {n} symbols | {len(notes)} changes | stance={plan['stance']}"
        save_plan(plan, trigger="continuous_review", summary=summary)
        logger.info(f"plan_manager: continuous review — {len(notes)} adjustments")
    else:
        logger.debug("plan_manager: continuous review — no changes")


def run():
    logger.info("plan_manager: starting")
    plan = load_plan()
    with shared.cache_lock:
        shared.investment_plan = plan
    logger.info(
        f"plan_manager: loaded plan v{plan.get('version',0)} "
        f"with {len(plan.get('symbols',{}))} symbols | stance={plan.get('stance','neutral')}"
    )
    last_refresh = 0.0
    last_review = 0.0
    while not shared.SHUTTING_DOWN:
        now = time.time()
        try:
            # Hourly: corp action / calendar refresh
            if now - last_refresh >= PLAN_REFRESH_INTERVAL:
                if shared.ref_ready_event.is_set():
                    _scheduled_refresh()
                    last_refresh = now

            # Every REVIEW_INTERVAL: continuous analysis of positions + opportunities
            if now - last_review >= REVIEW_INTERVAL:
                if shared.ref_ready_event.is_set() and shared.account_ready_event.is_set():
                    _continuous_review()
                    last_review = now
        except Exception as e:
            logger.error(f"plan_manager: review/refresh error: {e}")
        time.sleep(settings.TICK_INTERVAL * 3)
    logger.info("plan_manager: SHUTTING_DOWN")
