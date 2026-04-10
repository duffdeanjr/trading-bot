"""
agents/plan_reviewer.py -- Claude API plan review agent.

Once per hour during market hours, assembles a JSON summary of the current
trading state and calls the Claude API for quantitative risk review.
Writes structured feedback to shared.plan_review and logs it.
"""

import os
import time
import json
import logging
import datetime

import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

REVIEW_INTERVAL = 3600  # 1 hour
CLAUDE_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """You are a quantitative risk reviewer for an algorithmic trading system.
You will receive a JSON summary of the system's current state including:
- Current investment plan targets and stance
- Recent trading signals
- Open positions
- Today's P&L
- Current market regime

Analyze the data for:
1. Concentration risk (too much in one sector/symbol)
2. Regime misalignment (strategies that don't fit the current market regime)
3. Signal quality (low-confidence signals driving large positions)
4. P&L trajectory concerns (accelerating losses, overexposure after wins)
5. Options risk (naked exposure, expiry clustering)

Respond ONLY with valid JSON in this exact format:
{
  "issues": ["issue 1 description", "issue 2 description"],
  "suggestions": ["suggestion 1", "suggestion 2"],
  "confidence": 0.85
}

Where confidence is 0-1 indicating how confident you are in your assessment.
Keep issues and suggestions concise (1 sentence each). Max 5 issues, 5 suggestions."""


def _build_review_payload() -> dict:
    """Assemble current state summary for the Claude API call."""
    # Current plan
    with shared.cache_lock:
        plan = shared.investment_plan or {}
        regime = getattr(shared, "market_regime", "unknown")
        flow_alerts = list(getattr(shared, "options_flow_alerts", []))

    # Last 20 signals from DB
    try:
        conn = database.get_connection()
        signals = [dict(r) for r in conn.execute(
            "SELECT symbol, strategy, side, confidence, sentiment "
            "FROM signals ORDER BY ts DESC LIMIT 20"
        ).fetchall()]
    except Exception:
        signals = []

    # Open positions
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

    # Today's P&L
    with shared.account_lock:
        acct = shared.account
    today_pl = 0.0
    if acct:
        try:
            equity = float(getattr(acct, "equity", 0) or 0)
            last_equity = float(getattr(acct, "last_equity", 0) or 0)
            today_pl = equity - last_equity
        except Exception:
            pass

    # Strategy scores
    try:
        scores = database.get_all_strategy_scores()
    except Exception:
        scores = []

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "regime": regime,
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
        "strategy_scores": scores,
        "flow_alerts": flow_alerts,
    }


def _call_claude_api(payload: dict) -> dict:
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
        system=_SYSTEM_PROMPT,
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
        # Try to extract JSON from markdown code block
        import re
        match = re.search(r'\{[\s\S]*\}', response_text)
        if match:
            result = json.loads(match.group())
        else:
            logger.warning(f"plan_reviewer: could not parse response as JSON")
            result = {"issues": [], "suggestions": [], "confidence": 0.0,
                      "raw_response": response_text[:500]}

    return result


def _run_review():
    """Execute a single plan review cycle."""
    if not (shared.MARKET_OPEN or shared.EXTENDED_HOURS):
        return

    logger.info("plan_reviewer: assembling review payload")
    payload = _build_review_payload()

    try:
        result = _call_claude_api(payload)
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

    # Log the review
    issues = result.get("issues", [])
    suggestions = result.get("suggestions", [])
    confidence = result.get("confidence", 0)

    logger.info(
        f"plan_reviewer: review complete — "
        f"{len(issues)} issues, {len(suggestions)} suggestions, "
        f"confidence={confidence:.2f}"
    )
    for issue in issues:
        logger.info(f"  ISSUE: {issue}")
    for suggestion in suggestions:
        logger.info(f"  SUGGESTION: {suggestion}")

    database.write_agent_log(
        ts=time.time(), agent="plan_reviewer", level="INFO",
        message=json.dumps(result, default=str)[:2000]
    )


def run():
    """Daemon thread entry point. Runs review once per hour during market hours."""
    logger.info("plan_reviewer: starting (hourly review during market hours)")

    # Wait for other systems to be ready
    shared.ref_ready_event.wait(timeout=120)
    shared.account_ready_event.wait(timeout=60)

    while not shared.SHUTTING_DOWN:
        try:
            _run_review()
        except Exception as e:
            logger.error(f"plan_reviewer: unexpected error: {e}")

        # Sleep for REVIEW_INTERVAL, checking SHUTTING_DOWN every 30s
        for _ in range(REVIEW_INTERVAL // 30):
            if shared.SHUTTING_DOWN:
                break
            time.sleep(30)

    logger.info("plan_reviewer: SHUTTING_DOWN - exiting")
