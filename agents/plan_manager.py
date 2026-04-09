import time
import json
import logging
import datetime
import threading

import shared
from config import settings
from storage.database import write_investment_plan, read_latest_plan, get_strategy_score

logger = logging.getLogger(__name__)

PLAN_REFRESH_INTERVAL = settings.PLAN_REFRESH_INTERVAL
PLAN_COOLDOWN = 60

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
    current_exclusions = plan.get("exclusions", [])
    exclusions = []
    for sym in dirty:
        if sym not in current_exclusions:
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
    symbols = plan.get("symbols", {})
    for sym, entry in symbols.items():
        if entry.get("target_pct", 0) > max_pct:
            entry["target_pct"] = max_pct
            entry["reason"] = entry.get("reason", "") + " [capped by risk limit]"
    max_invested = 1.0 - plan.get("cash_target_pct", 0.10)
    total = sum(e.get("target_pct", 0) for e in symbols.values())
    if total > max_invested and total > 0:
        scale = max_invested / total
        for entry in symbols.values():
            entry["target_pct"] = entry.get("target_pct", 0) * scale
        plan.setdefault("notes", []).append(f"scaled positions by {scale:.2f} to maintain cash buffer")
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
                    "target_pct":  min(conf * 0.05, settings.MAX_PORTFOLIO_PCT),
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
    now_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    upcoming = [str(c) for c in calendar[:5]]
    if calendar and now_str not in " ".join(upcoming):
        notes.append("market holiday approaching — raising cash target")
        plan["cash_target_pct"] = min(plan["cash_target_pct"] + 0.05, 0.25)

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
    while not shared.SHUTTING_DOWN:
        try:
            if time.time() - last_refresh >= PLAN_REFRESH_INTERVAL:
                if shared.ref_ready_event.is_set():
                    _scheduled_refresh()
                    last_refresh = time.time()
        except Exception as e:
            logger.error(f"plan_manager: scheduled refresh error: {e}")
        time.sleep(settings.TICK_INTERVAL * 6)
    logger.info("plan_manager: SHUTTING_DOWN")
