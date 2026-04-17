"""
agents/bandit.py -- Contextual bandit (LinUCB) for adaptive strategy weighting.

Architecture:
  - 15-feature context vector built from live system state
  - Per-strategy LinUCB model (A matrix + b vector)
  - Shadow mode for first 30 days / 200 observations
  - Reward harvester runs every 10 minutes, matches decisions to outcomes
  - Alpha decays from 0.3 toward 0.1 as observations accumulate

Pipeline position: after Kelly sizing, before correlation discount.
"""

import os
import time
import json
import math
import logging
import datetime
import threading

import numpy as np

import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

# ── constants (configurable via settings.py) ─────────────────────
N_FEATURES = 15
DEFAULT_ALPHA = settings.BANDIT_DEFAULT_ALPHA
MIN_ALPHA = settings.BANDIT_MIN_ALPHA
ALPHA_DECAY_RATE = settings.BANDIT_ALPHA_DECAY_RATE
ALPHA_DECAY_THRESHOLD = settings.BANDIT_ALPHA_DECAY_THRESHOLD
COLD_START_THRESHOLD = 10    # observations before bandit adjusts
SHADOW_MIN_DAYS = 7          # reduced from 30 for faster learning (paper trading)
SHADOW_MIN_OBS = 50          # reduced from 200 — outcomes now flowing
REWARD_WINDOW_S = 7200       # 2 hours — match decisions to outcomes
HARVEST_INTERVAL = 600       # 10 minutes


# ── Task 1: Context vector builder ───────────────────────────────

def build_context_vector(signals: list) -> np.ndarray:
    """
    Construct a fixed-length 15-feature context vector from current system state.
    All features are normalized to roughly [0, 1] or [-1, 1] range.
    Returns np.ndarray of shape (15,).
    """
    # 1. VIX level (normalized, cap at 80)
    try:
        from agents import risk_manager as _rm
        vix = _rm.get_last_vix()
    except Exception:
        vix = 18.0
    vix_norm = min(vix, 80.0) / 80.0

    # 2. SPY 20-day momentum (% return, already roughly -1 to 1)
    spy_momentum = 0.0
    with shared.cache_lock:
        spy_data = shared.historical_ohlcv.get("SPY", {})
    closes = spy_data.get("closes", []) if isinstance(spy_data, dict) else []
    if len(closes) >= 20 and closes[-20] > 0:
        spy_momentum = (closes[-1] - closes[-20]) / closes[-20]
    spy_momentum = max(-1.0, min(1.0, spy_momentum))

    # 3-6. Regime one-hot encoding
    with shared.cache_lock:
        regime = shared.market_regime or "unknown"
    regime_bull = 1.0 if regime == "trending-bull" else 0.0
    regime_bear = 1.0 if regime == "trending-bear" else 0.0
    regime_range = 1.0 if regime == "ranging" else 0.0
    regime_hvol = 1.0 if regime == "high-vol" else 0.0

    # 7. Time of day (normalized over trading hours 9:30-16:00)
    now = datetime.datetime.now()
    hour_frac = now.hour + now.minute / 60.0
    time_norm = max(0.0, min(1.0, (hour_frac - 9.5) / 6.5))

    # 8. Day of week (0=Monday through 4=Friday, normalized)
    weekday_norm = now.weekday() / 4.0

    # 9. Portfolio heat (total market value / equity)
    portfolio_heat = 0.0
    with shared.positions_lock:
        positions = dict(shared.positions)
    with shared.account_lock:
        acct = shared.account
    total_mv = 0.0
    for sym, pos in positions.items():
        try:
            total_mv += abs(float(getattr(pos, "market_value", 0) or 0))
        except Exception:
            pass
    equity = 0.0
    if acct:
        try:
            equity = float(getattr(acct, "equity", 0) or 0)
        except Exception:
            pass
    if equity > 0:
        portfolio_heat = min(total_mv / equity, 1.0)

    # 10. Mean signal conviction
    if signals:
        convictions = [float(s.get("confidence", s.get("conviction", 0.5))) for s in signals]
        mean_conv = sum(convictions) / len(convictions)
    else:
        mean_conv = 0.5

    # 11-12. Options flow alert counts (bullish / bearish)
    with shared.cache_lock:
        flow_alerts = list(shared.options_flow_alerts) if shared.options_flow_alerts else []
    n_bullish = sum(1 for a in flow_alerts if a.get("direction") == "bullish")
    n_bearish = sum(1 for a in flow_alerts if a.get("direction") == "bearish")
    flow_bull_norm = min(n_bullish, 10) / 10.0
    flow_bear_norm = min(n_bearish, 10) / 10.0

    # 13. Max correlation among plan symbols
    corr_max = 0.0
    try:
        from agents.plan_manager import _build_correlation_matrix
        with shared.cache_lock:
            plan = shared.investment_plan or {}
        plan_syms = list(plan.get("symbols", {}).keys())
        if len(plan_syms) >= 2:
            matrix = _build_correlation_matrix(plan_syms)
            for sym_a in matrix:
                for sym_b, c in matrix[sym_a].items():
                    if sym_a != sym_b and c > corr_max:
                        corr_max = c
    except Exception:
        pass
    corr_max = min(corr_max, 1.0)

    # 14. Active live strategies count (normalized, cap at 50)
    try:
        conn = database.get_connection()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM strategy_recipes WHERE status='live'"
        ).fetchone()
        n_live = row["cnt"] if row else 0
    except Exception:
        n_live = 0
    live_strat_norm = min(n_live, 50) / 50.0

    # 15. Today's P&L as % of portfolio (clipped -0.1 to 0.1)
    pnl_today_pct = 0.0
    if equity > 0:
        try:
            today_start = datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            conn = database.get_connection()
            row = conn.execute(
                "SELECT SUM(CASE WHEN side='buy' THEN -notional ELSE notional END) as net "
                "FROM trades WHERE ts >= ?",
                (today_start,)
            ).fetchone()
            net = float(row["net"] or 0) if row else 0.0
            pnl_today_pct = net / equity
        except Exception:
            pass
    pnl_today_pct = max(-0.1, min(0.1, pnl_today_pct))

    return np.array([
        vix_norm,           # 0
        spy_momentum,       # 1
        regime_bull,        # 2
        regime_bear,        # 3
        regime_range,       # 4
        regime_hvol,        # 5
        time_norm,          # 6
        weekday_norm,       # 7
        portfolio_heat,     # 8
        mean_conv,          # 9
        flow_bull_norm,     # 10
        flow_bear_norm,     # 11
        corr_max,           # 12
        live_strat_norm,    # 13
        pnl_today_pct,      # 14
    ], dtype=np.float64)


# ── Task 2: LinUCB model ─────────────────────────────────────────

class LinUCB:
    """
    Linear Upper Confidence Bound bandit with per-strategy arms.
    Each strategy gets its own A matrix (d×d) and b vector (d,).
    """

    def __init__(self, n_features: int = N_FEATURES, alpha: float = DEFAULT_ALPHA):
        self.n_features = n_features
        self.alpha = alpha
        self.A: dict[str, np.ndarray] = {}   # strategy_id -> (n, n)
        self.b: dict[str, np.ndarray] = {}   # strategy_id -> (n,)

    def _init_strategy(self, strategy_id: str):
        if strategy_id not in self.A:
            self.A[strategy_id] = np.eye(self.n_features)
            self.b[strategy_id] = np.zeros(self.n_features)

    def get_multiplier(self, strategy_id: str, context: np.ndarray) -> float:
        """
        Compute the UCB score for a strategy given a context vector.
        Returns a multiplier clipped to [0.3, 2.0].
        """
        self._init_strategy(strategy_id)
        A_inv = np.linalg.inv(self.A[strategy_id])
        theta = A_inv @ self.b[strategy_id]
        exploration = self.alpha * np.sqrt(context @ A_inv @ context)
        score = float(theta @ context + exploration)
        # Map score to multiplier: baseline of 1.0, score shifts it
        baseline = 1.0
        multiplier = score / baseline if baseline != 0 else 1.0
        return float(np.clip(multiplier, 0.3, 2.0))

    def update(self, strategy_id: str, context: np.ndarray, reward: float):
        """Update the model for a strategy with an observed (context, reward) pair."""
        self._init_strategy(strategy_id)
        self.A[strategy_id] += np.outer(context, context)
        self.b[strategy_id] += reward * context

    def get_observation_count(self, strategy_id: str) -> int:
        """
        Estimate number of observations from trace(A) - n_features.
        (Each update adds outer(x,x) which increases trace by ||x||^2.)
        """
        if strategy_id not in self.A:
            return 0
        return int(round(np.trace(self.A[strategy_id]) - self.n_features))

    def save(self, db_path: str = None):
        """Persist A and b matrices to bandit_state table in SQLite."""
        try:
            conn = database.get_connection()
            now = time.time()
            with database._lock:
                for sid in self.A:
                    a_json = json.dumps(self.A[sid].tolist())
                    b_json = json.dumps(self.b[sid].tolist())
                    conn.execute(
                        """INSERT OR REPLACE INTO bandit_state
                           (strategy_id, A_matrix, b_vector, alpha, last_updated)
                           VALUES (?, ?, ?, ?, ?)""",
                        (sid, a_json, b_json, self.alpha, now)
                    )
                # Also save shadow_start_ts as a meta row
                conn.execute(
                    """INSERT OR REPLACE INTO bandit_state
                       (strategy_id, A_matrix, b_vector, alpha, last_updated)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("__meta_shadow_start_ts__", json.dumps(_shadow_start_ts),
                     "[]", self.alpha, now)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"bandit: save failed: {e}")

    def load(self, db_path: str = None):
        """Restore A and b matrices from bandit_state table."""
        global _shadow_start_ts
        try:
            conn = database.get_connection()
            rows = conn.execute("SELECT * FROM bandit_state").fetchall()
            for row in rows:
                sid = row["strategy_id"]
                if sid == "__meta_shadow_start_ts__":
                    try:
                        _shadow_start_ts = json.loads(row["A_matrix"])
                    except Exception:
                        pass
                    self.alpha = row["alpha"]
                    continue
                try:
                    self.A[sid] = np.array(json.loads(row["A_matrix"]))
                    self.b[sid] = np.array(json.loads(row["b_vector"]))
                    self.alpha = row["alpha"]
                except Exception:
                    continue
            n_loaded = len(self.A)
            if n_loaded > 0:
                logger.info(f"bandit: loaded {n_loaded} strategy models, alpha={self.alpha:.3f}")
        except Exception as e:
            logger.debug(f"bandit: load failed (may be first run): {e}")


# ── Module-level singleton ────────────────────────────────────────

linucb = LinUCB()
_shadow_start_ts: float = time.time()


# ── Task 3: Reward computation ────────────────────────────────────

def compute_reward(strategy_id: str, decision_ts: float,
                   context: np.ndarray) -> float | None:
    """
    Query the outcomes table for trades matching this strategy within
    2 hours of decision_ts. Compute mean normalized return.
    Returns None if no matching closed trades exist yet.
    """
    try:
        conn = database.get_connection()
        window_end = decision_ts + REWARD_WINDOW_S

        rows = conn.execute(
            """SELECT pnl, qty, entry_price, side FROM outcomes
               WHERE strategy = ? AND entry_ts >= ? AND entry_ts <= ?
               AND pnl IS NOT NULL AND status = 'closed'""",
            (strategy_id,
             datetime.datetime.fromtimestamp(decision_ts).isoformat(),
             datetime.datetime.fromtimestamp(window_end).isoformat())
        ).fetchall()

        if not rows:
            return None

        normalized_returns = []
        for row in rows:
            pnl = float(row["pnl"] or 0)
            qty = float(row["qty"] or 1)
            entry_price = float(row["entry_price"] or 1)
            notional = abs(qty * entry_price)
            if notional > 0:
                normalized_returns.append(pnl / notional)

        if not normalized_returns:
            return None

        reward = sum(normalized_returns) / len(normalized_returns)
        return float(np.clip(reward, -1.0, 1.0))

    except Exception as e:
        logger.error(f"bandit: compute_reward failed for {strategy_id}: {e}")
        return None


# ── Task 4: Decision logging ─────────────────────────────────────

def _log_decision(strategy_id: str, context: np.ndarray,
                  multiplier: float, shadow_mode: int):
    """Write a bandit decision to the bandit_decisions table."""
    try:
        conn = database.get_connection()
        with database._lock:
            conn.execute(
                """INSERT INTO bandit_decisions
                   (ts, strategy_id, context_vector, multiplier_applied,
                    shadow_mode, evaluated)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (time.time(), strategy_id, json.dumps(context.tolist()),
                 multiplier, shadow_mode)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"bandit: _log_decision failed: {e}")


# ── Task 4: Reward harvester loop ─────────────────────────────────

class BanditRewardHarvester:
    """
    Background thread that matches bandit decisions to trade outcomes.
    Runs every 10 minutes, evaluates decisions older than 2 hours.
    """

    def __init__(self, model: LinUCB):
        self.model = model

    def run(self):
        """Daemon thread entry point."""
        logger.info("bandit: reward harvester starting")

        # Wait for DB and account to be ready
        shared.ref_ready_event.wait(timeout=120)
        shared.account_ready_event.wait(timeout=60)

        while not shared.SHUTTING_DOWN:
            shared.heartbeat("bandit_harvester")
            try:
                self._harvest()
            except Exception as e:
                logger.error(f"bandit: harvest error: {e}")

            # Sleep HARVEST_INTERVAL, checking shutdown
            for _ in range(HARVEST_INTERVAL // 5):
                if shared.SHUTTING_DOWN:
                    break
                shared.heartbeat("bandit_harvester")
                time.sleep(5)

        logger.info("bandit: reward harvester SHUTTING_DOWN - exiting")

    def _harvest(self):
        """Process unevaluated decisions older than 2 hours."""
        cutoff = time.time() - REWARD_WINDOW_S
        try:
            conn = database.get_connection()
            rows = conn.execute(
                """SELECT * FROM bandit_decisions
                   WHERE evaluated = 0 AND ts < ?
                   ORDER BY ts LIMIT 100""",
                (cutoff,)
            ).fetchall()
        except Exception as e:
            logger.error(f"bandit: harvest query failed: {e}")
            return

        if not rows:
            return

        updated = 0
        for row in rows:
            row_id = row["id"]
            strategy_id = row["strategy_id"]
            ts = row["ts"]
            shadow = row["shadow_mode"]

            try:
                context = np.array(json.loads(row["context_vector"]))
            except Exception:
                # Mark as evaluated with no reward
                self._mark_evaluated(row_id, None)
                continue

            reward = compute_reward(strategy_id, ts, context)

            if reward is not None:
                self.model.update(strategy_id, context, reward)
                n_obs = self.model.get_observation_count(strategy_id)
                logger.debug(
                    f"bandit: updated {strategy_id} "
                    f"reward={reward:.4f} obs={n_obs}"
                )

                if shadow == 0:  # live mode — persist immediately
                    self.model.save()

                self._mark_evaluated(row_id, reward)
                updated += 1
            else:
                # No matching trades yet — leave for next harvest
                # But if decision is very old (>24h), mark as evaluated with 0
                if time.time() - ts > 86400:
                    self._mark_evaluated(row_id, 0.0)

        if updated > 0:
            logger.info(f"bandit: harvested {updated} rewards from {len(rows)} decisions")

        # Alpha decay: after 200+ total observations, decay toward MIN_ALPHA
        self._maybe_decay_alpha()

    def _mark_evaluated(self, row_id: int, reward: float | None):
        """Mark a decision as evaluated in the database."""
        try:
            conn = database.get_connection()
            with database._lock:
                conn.execute(
                    """UPDATE bandit_decisions
                       SET evaluated = 1, reward_computed = ?
                       WHERE id = ?""",
                    (reward, row_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"bandit: _mark_evaluated failed: {e}")

    def _maybe_decay_alpha(self):
        """Decay alpha after enough total observations accumulate."""
        total_obs = sum(
            self.model.get_observation_count(sid)
            for sid in self.model.A
        )
        if total_obs >= ALPHA_DECAY_THRESHOLD:
            old_alpha = self.model.alpha
            self.model.alpha = max(MIN_ALPHA, self.model.alpha * ALPHA_DECAY_RATE)
            if abs(old_alpha - self.model.alpha) > 0.001:
                logger.debug(
                    f"bandit: alpha decay {old_alpha:.4f} -> "
                    f"{self.model.alpha:.4f} (total_obs={total_obs})"
                )


# ── Task 5: Shadow mode + multiplier interface ────────────────────

def get_multipliers(signals: list, context: np.ndarray) -> dict:
    """
    Compute bandit multipliers for each strategy in the signal batch.

    Shadow mode (first 30 days OR < 200 total observations):
      - Multipliers are computed and logged but returned as 1.0
    Live mode:
      - Multipliers are applied for real
    Cold start (< 10 observations for a strategy):
      - Returns 1.0 (neutral) regardless of mode
    """
    global _shadow_start_ts

    # Compute total observations across all strategies
    all_strategy_ids = set(linucb.A.keys())
    for sig in signals:
        sid = sig.get("strategy", sig.get("strategy_tag", "unknown"))
        all_strategy_ids.add(sid)

    total_obs = sum(linucb.get_observation_count(s) for s in all_strategy_ids)
    days_since_start = (time.time() - _shadow_start_ts) / 86400.0
    in_shadow = total_obs < SHADOW_MIN_OBS or days_since_start < SHADOW_MIN_DAYS

    multipliers = {}
    for sig in signals:
        sid = sig.get("strategy", sig.get("strategy_tag", "unknown"))
        n_obs = linucb.get_observation_count(sid)

        if n_obs < COLD_START_THRESHOLD:
            multipliers[sid] = 1.0  # cold start — neutral
        else:
            multipliers[sid] = linucb.get_multiplier(sid, context)

    # Log every decision to bandit_decisions table
    for sid, mult in multipliers.items():
        _log_decision(sid, context, mult, shadow_mode=int(in_shadow))

    if in_shadow:
        return {sid: 1.0 for sid in multipliers}  # neutral during shadow
    return multipliers


def get_mode_info() -> dict:
    """Return bandit mode information for dashboard."""
    global _shadow_start_ts
    total_obs = sum(linucb.get_observation_count(s) for s in linucb.A)
    days_since_start = (time.time() - _shadow_start_ts) / 86400.0
    in_shadow = total_obs < SHADOW_MIN_OBS or days_since_start < SHADOW_MIN_DAYS

    days_until_live = 0
    if in_shadow:
        days_remaining = max(0, SHADOW_MIN_DAYS - days_since_start)
        days_until_live = int(math.ceil(days_remaining))

    return {
        "mode": "shadow" if in_shadow else "live",
        "total_observations": total_obs,
        "alpha": round(linucb.alpha, 4),
        "days_until_live": days_until_live,
        "days_since_start": round(days_since_start, 1),
        "shadow_min_obs": SHADOW_MIN_OBS,
        "shadow_min_days": SHADOW_MIN_DAYS,
    }


# ── Initialization (called from main.py) ─────────────────────────

_harvester = BanditRewardHarvester(linucb)


def init():
    """Load persisted bandit state. Called at startup."""
    linucb.load()


@shared.register_agent("bandit_harvester", phase=7)
def run():
    """Daemon thread entry for reward harvester."""
    init()
    _harvester.run()
