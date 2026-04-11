"""
agents/plan_reviewer.py -- Claude API plan review agent (Loop 3).

Once per hour during market hours, assembles an enriched context snapshot,
calls the Claude API for quantitative risk review with structured output,
auto-applies high-confidence suggestions, and tracks outcomes for
self-modifying prompt construction.
"""

import os
import time
import json
import math
import logging
import datetime

import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

REVIEW_INTERVAL = 3600  # 1 hour
OUTCOME_EVAL_INTERVAL = 14400  # 4 hours
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Confidence gates for auto-apply
AUTO_APPLY_SUGGESTION_MIN = 0.75
AUTO_APPLY_OVERALL_MIN = 0.70

# ── base system prompt ───────────────────────────────────────────
_BASE_SYSTEM_PROMPT = """You are a quantitative risk reviewer for an algorithmic trading bot.

You MUST respond with valid JSON only. No prose. No markdown. No explanation outside the JSON structure.

Every suggestion MUST use one of these exact action types:
- adjust_target: modify a symbol's plan weight (value = multiplier, e.g. 0.6 = reduce 40%)
- add_symbol: add a new symbol to the plan (value = target weight 0.01-0.15)
- remove_symbol: remove a symbol from the plan (value = null)
- change_stance: change overall stance (value = "risk-on" | "risk-off" | "neutral")
- exclude_symbol: exclude symbol for 24h (value = null)

BAD suggestion (reject this pattern):
{"action": "adjust_target", "symbol": "NVDA", "reason": "too much tech exposure"}

GOOD suggestion (required pattern):
{"action": "adjust_target", "symbol": "NVDA", "value": 0.6,
 "reason": "NVDA+AMD corr=0.91 over 14d, combined weight 18% exceeds 15% sector cap",
 "confidence": 0.82}

Reason must cite the actual metric, actual value, and actual threshold being violated.
Confidence must reflect genuine uncertainty -- do not default to 0.9 on everything.

Required response schema:
{
  "issues": ["string"],
  "suggestions": [
    {
      "action": "adjust_target | add_symbol | remove_symbol | change_stance | exclude_symbol",
      "symbol": "string or null",
      "parameter": "string or null",
      "value": "number or string or null",
      "reason": "string, max 80 chars, must cite specific metric and value",
      "confidence": 0.0
    }
  ],
  "overall_confidence": 0.0,
  "plan_quality_score": 0.0
}"""


# ── 2e: self-modifying system prompt ─────────────────────────────
def _build_system_prompt() -> str:
    """
    Construct system prompt dynamically.  Appends historical performance
    of past suggestions if >= 10 evaluated rows exist.
    """
    prompt = _BASE_SYSTEM_PROMPT

    try:
        evaluated = database.get_evaluated_review_outcomes(limit=20)
        if len(evaluated) < 10:
            return prompt  # insufficient data

        pnl_by_action = database.get_review_outcome_pnl_by_action()
        if not pnl_by_action:
            return prompt

        lines = []
        for row in pnl_by_action:
            action = row["action"]
            avg = row["avg_pnl_4h"] or 0
            count = row["count"] or 0
            sign = "+" if avg >= 0 else ""
            lines.append(f"  {action}: avg 4h P&L {sign}${avg:.2f} ({count} samples)")

        prompt += "\n\nHistorical performance of your past suggestions:\n"
        prompt += "\n".join(lines)
        prompt += "\nWeight your suggestions toward action types with positive historical P&L."

    except Exception as e:
        logger.debug(f"plan_reviewer: prompt self-mod error: {e}")

    return prompt


# ── 2a: enriched context snapshot ────────────────────────────────
def _build_review_payload() -> dict:
    """Assemble enriched context for the Claude API call."""

    # -- Current plan --
    with shared.cache_lock:
        plan = shared.investment_plan or {}
        regime = getattr(shared, "market_regime", "unknown")
        flow_alerts = list(getattr(shared, "options_flow_alerts", []))

    # -- VIX level --
    try:
        from agents import risk_manager as _rm
        vix_level = getattr(_rm, "_last_vix", None) or 18.0
    except Exception:
        vix_level = 18.0

    # -- Last 20 signals --
    try:
        conn = database.get_connection()
        signals = [dict(r) for r in conn.execute(
            "SELECT symbol, strategy, side, confidence, sentiment "
            "FROM signals ORDER BY ts DESC LIMIT 20"
        ).fetchall()]
    except Exception:
        signals = []

    # -- Open positions with unrealised P&L --
    with shared.positions_lock:
        positions_raw = dict(shared.positions)
    positions = {}
    for sym, pos in positions_raw.items():
        try:
            positions[sym] = {
                "qty": float(getattr(pos, "qty", 0) or 0),
                "market_value": float(getattr(pos, "market_value", 0) or 0),
                "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
                "unrealized_plpc": float(getattr(pos, "unrealized_plpc", 0) or 0),
            }
        except Exception:
            pass

    # -- Today's realised P&L from trades table --
    today_pl = 0.0
    with shared.account_lock:
        acct = shared.account
    if acct:
        try:
            equity = float(getattr(acct, "equity", 0) or 0)
            last_equity = float(getattr(acct, "last_equity", 0) or 0)
            today_pl = equity - last_equity
        except Exception:
            pass

    # -- Sector exposure breakdown --
    sector_exposure = {}
    total_mv = sum(p.get("market_value", 0) for p in positions.values())
    if total_mv > 0:
        with shared.cache_lock:
            assets_cache = dict(shared.assets) if shared.assets else {}
        for sym, pos_data in positions.items():
            mv = pos_data.get("market_value", 0)
            # Try to get sector from assets cache
            asset = assets_cache.get(sym)
            if asset:
                sector = getattr(asset, "sector", None) or "other"
            else:
                sector = "crypto" if "/" in sym else "other"
            sector_exposure[sector] = sector_exposure.get(sector, 0) + mv / total_mv

    # -- Strategy scores --
    try:
        scores = database.get_all_strategy_scores()
    except Exception:
        scores = []

    # -- Options flow alerts with direction/magnitude --
    flow_summary = []
    for alert in flow_alerts:
        flow_summary.append({
            "symbol": alert.get("symbol"),
            "direction": alert.get("direction"),
            "type": alert.get("type"),
            "magnitude": alert.get("magnitude"),
        })

    # -- Macro calendar: next 5 trading days --
    macro_events = []
    try:
        today = datetime.date.today()
        end = today + datetime.timedelta(days=7)
        conn = database.get_connection()
        rows = conn.execute(
            "SELECT * FROM market_calendar WHERE date >= ? AND date <= ? ORDER BY date",
            (today.isoformat(), end.isoformat())
        ).fetchall()
        macro_events = [dict(r) for r in rows[:5]]
    except Exception:
        pass

    # -- Correlation hotspots: pairs > 0.75 both in plan --
    correlation_hotspots = []
    plan_syms = list(plan.get("symbols", {}).keys())
    if len(plan_syms) >= 2:
        try:
            from agents.plan_manager import _build_correlation_matrix
            corr_matrix = _build_correlation_matrix(plan_syms)
            seen = set()
            for sym_a in plan_syms:
                for sym_b in plan_syms:
                    if sym_a >= sym_b:
                        continue
                    pair_key = (sym_a, sym_b)
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    corr = corr_matrix.get(sym_a, {}).get(sym_b)
                    if corr is not None and corr > 0.75:
                        correlation_hotspots.append({
                            "pair": [sym_a, sym_b],
                            "correlation": round(corr, 3),
                        })
        except Exception as e:
            logger.debug(f"plan_reviewer: correlation hotspot error: {e}")

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "regime": regime,
        "vix_level": round(vix_level, 1),
        "plan": {
            "stance": plan.get("stance", "unknown"),
            "cash_target_pct": plan.get("cash_target_pct", 0),
            "symbols": {
                sym: {
                    "conviction": entry.get("conviction"),
                    "target_pct": entry.get("target_pct"),
                    "strategy": entry.get("strategy"),
                    "sector": entry.get("sector"),
                }
                for sym, entry in plan.get("symbols", {}).items()
            },
            "exclusions": plan.get("exclusions", []),
        },
        "recent_signals": signals,
        "open_positions": positions,
        "today_pnl": round(today_pl, 2),
        "sector_exposure": sector_exposure,
        "strategy_scores": scores,
        "flow_alerts": flow_summary,
        "macro_calendar": macro_events,
        "correlation_hotspots": correlation_hotspots,
    }


# ── Claude API call ──────────────────────────────────────────────
def _call_claude_api(payload: dict, system_prompt: str) -> dict:
    """Call the Claude API with the review payload. Returns parsed JSON response."""
    try:
        import anthropic
    except ImportError:
        logger.warning("plan_reviewer: anthropic package not installed, skipping review")
        return {}

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("plan_reviewer: ANTHROPIC_API_KEY not set, skipping review")
        return {}

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": json.dumps(payload, default=str),
            }
        ],
    )

    # Extract text response
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text += block.text

    # Parse JSON from response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{[\s\S]*\}', response_text)
        if match:
            result = json.loads(match.group())
        else:
            logger.warning("plan_reviewer: could not parse response as JSON")
            result = {"issues": [], "suggestions": [],
                      "overall_confidence": 0.0, "plan_quality_score": 0.0,
                      "raw_response": response_text[:500]}

    return result


# ── 2c: confidence-gated auto-apply ──────────────────────────────
def _auto_apply_suggestions(result: dict):
    """
    Iterate suggestions.  For each where confidence >= 0.75 AND
    overall_confidence >= 0.70, apply the action to the live plan.
    Log every action (applied or skipped).  Write all to plan_review_outcomes.
    """
    suggestions = result.get("suggestions", [])
    overall_conf = result.get("overall_confidence", 0.0)
    now = time.time()

    for sug in suggestions:
        action = sug.get("action", "")
        symbol = sug.get("symbol")
        value = sug.get("value")
        reason = sug.get("reason", "")
        confidence = sug.get("confidence", 0.0)

        should_apply = (
            confidence >= AUTO_APPLY_SUGGESTION_MIN
            and overall_conf >= AUTO_APPLY_OVERALL_MIN
        )

        if should_apply:
            try:
                _apply_action(action, symbol, value)
                logger.info(
                    f"plan_reviewer: AUTO-APPLIED {action} {symbol} -> {value} "
                    f"(confidence={confidence:.2f}, reason={reason})"
                )
            except Exception as e:
                logger.error(f"plan_reviewer: auto-apply failed {action} {symbol}: {e}")
                should_apply = False
        else:
            logger.debug(
                f"plan_reviewer: SKIPPED {action} -- "
                f"confidence {confidence:.2f} below threshold"
            )

        # Write to outcome tracking (2d)
        database.write_review_outcome(
            ts=now, action=action, symbol=symbol, value=value,
            reason=reason, confidence=confidence, was_applied=should_apply,
        )


def _apply_action(action: str, symbol: str, value):
    """Apply a single review action to the live plan in shared state."""
    with shared.cache_lock:
        plan = shared.investment_plan or {}
        symbols = plan.get("symbols", {})

        if action == "adjust_target" and symbol and symbol in symbols:
            old_target = symbols[symbol].get("target_pct", 0)
            multiplier = float(value) if value else 1.0
            symbols[symbol]["target_pct"] = round(old_target * multiplier, 4)

        elif action == "add_symbol" and symbol:
            target = float(value) if value else 0.05
            target = max(0.01, min(target, 0.15))
            symbols[symbol] = {
                "conviction": 0.5,
                "target_pct": target,
                "sector": "other",
                "strategy": "ai_review",
                "reason": "added by plan_reviewer",
                "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        elif action == "remove_symbol" and symbol and symbol in symbols:
            symbols[symbol]["target_pct"] = 0
            symbols[symbol]["reason"] = "removed by plan_reviewer"

        elif action == "change_stance" and value:
            if value in ("risk-on", "risk-off", "neutral"):
                plan["stance"] = value

        elif action == "exclude_symbol" and symbol:
            exclusions = plan.get("exclusions", [])
            if symbol not in exclusions:
                exclusions.append(symbol)
                plan["exclusions"] = exclusions

        plan["symbols"] = symbols
        shared.investment_plan = plan


# ── 2d: outcome evaluation ───────────────────────────────────────
def _evaluate_outcomes():
    """
    Background function: find unevaluated review outcomes older than 4h,
    compute P&L change since suggestion timestamp, fill in pnl_1h and pnl_4h.
    """
    now = time.time()
    cutoff_4h = now - OUTCOME_EVAL_INTERVAL
    cutoff_1h = now - 3600

    rows = database.get_unevaluated_review_outcomes(older_than_ts=cutoff_4h)
    if not rows:
        return

    for row in rows:
        ts = row["ts"]
        symbol = row.get("symbol")
        if not symbol:
            # Non-symbol actions (stance changes) — mark evaluated with 0 P&L
            database.update_review_outcome_pnl(row["id"], 0.0, 0.0)
            continue

        # Compute P&L from trades table since suggestion time
        try:
            conn = database.get_connection()

            # P&L at 1h mark
            trades_1h = conn.execute(
                """SELECT SUM(CASE WHEN side='buy' THEN -notional ELSE notional END) as net
                   FROM trades WHERE symbol=? AND ts >= ? AND ts <= ?""",
                (symbol, ts, ts + 3600)
            ).fetchone()
            pnl_1h = float(trades_1h["net"] or 0) if trades_1h else 0.0

            # P&L at 4h mark
            trades_4h = conn.execute(
                """SELECT SUM(CASE WHEN side='buy' THEN -notional ELSE notional END) as net
                   FROM trades WHERE symbol=? AND ts >= ? AND ts <= ?""",
                (symbol, ts, ts + OUTCOME_EVAL_INTERVAL)
            ).fetchone()
            pnl_4h = float(trades_4h["net"] or 0) if trades_4h else 0.0

            # Also check unrealised P&L change via position snapshots
            snap_before = conn.execute(
                "SELECT unrealised FROM positions WHERE symbol=? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (symbol, ts)
            ).fetchone()
            snap_after = conn.execute(
                "SELECT unrealised FROM positions WHERE symbol=? AND ts >= ? ORDER BY ts ASC LIMIT 1",
                (symbol, ts + OUTCOME_EVAL_INTERVAL)
            ).fetchone()

            if snap_before and snap_after:
                unrealised_delta = (float(snap_after["unrealised"] or 0)
                                    - float(snap_before["unrealised"] or 0))
                pnl_4h += unrealised_delta

            database.update_review_outcome_pnl(row["id"], round(pnl_1h, 2), round(pnl_4h, 2))

        except Exception as e:
            logger.debug(f"plan_reviewer: outcome eval error for {symbol}: {e}")
            database.update_review_outcome_pnl(row["id"], 0.0, 0.0)

    logger.info(f"plan_reviewer: evaluated {len(rows)} review outcomes")


# ── main review cycle ────────────────────────────────────────────
def _run_review():
    """Execute a single plan review cycle."""
    if not (shared.MARKET_OPEN or shared.EXTENDED_HOURS):
        return

    logger.info("plan_reviewer: assembling enriched review payload")
    payload = _build_review_payload()
    system_prompt = _build_system_prompt()

    try:
        result = _call_claude_api(payload, system_prompt)
    except Exception as e:
        logger.error(f"plan_reviewer: Claude API call failed: {e}")
        database.write_agent_log(
            ts=time.time(), agent="plan_reviewer", level="ERROR",
            message=f"Claude API error: {e}"
        )
        return

    if not result:
        return

    # Write to shared state
    result["reviewed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with shared.cache_lock:
        shared.plan_review = result

    # Log summary
    issues = result.get("issues", [])
    suggestions = result.get("suggestions", [])
    overall_conf = result.get("overall_confidence", 0)
    quality = result.get("plan_quality_score", 0)

    logger.info(
        f"plan_reviewer: review complete -- "
        f"{len(issues)} issues, {len(suggestions)} suggestions, "
        f"overall_confidence={overall_conf:.2f}, quality={quality:.2f}"
    )
    for issue in issues:
        logger.info(f"  ISSUE: {issue}")

    # Auto-apply and track outcomes (2c + 2d)
    _auto_apply_suggestions(result)

    database.write_agent_log(
        ts=time.time(), agent="plan_reviewer", level="INFO",
        message=json.dumps(result, default=str)[:2000]
    )


# ── daemon thread entry ──────────────────────────────────────────
@shared.register_agent("plan_reviewer", phase=7)
def run():
    """Daemon thread entry point. Runs review once per hour, outcome eval every 4h."""
    logger.info("plan_reviewer: starting (hourly review during market hours)")

    shared.ref_ready_event.wait(timeout=120)
    shared.account_ready_event.wait(timeout=60)

    last_outcome_eval = 0.0

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("plan_reviewer")
        try:
            _run_review()
        except Exception as e:
            logger.error(f"plan_reviewer: unexpected error: {e}")

        # Outcome evaluation every 4 hours
        now = time.time()
        if now - last_outcome_eval >= OUTCOME_EVAL_INTERVAL:
            try:
                _evaluate_outcomes()
            except Exception as e:
                logger.error(f"plan_reviewer: outcome eval error: {e}")
            last_outcome_eval = now

        # Sleep for REVIEW_INTERVAL, checking SHUTTING_DOWN every 30s
        for _ in range(REVIEW_INTERVAL // 30):
            if shared.SHUTTING_DOWN:
                break
            time.sleep(30)

    logger.info("plan_reviewer: SHUTTING_DOWN - exiting")
