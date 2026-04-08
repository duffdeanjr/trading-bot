"""
agents/portfolio_heat.py -- Portfolio heat and VIX regime detection.

"Heat" = total directional risk exposure as % of portfolio.
When heat is high, stop adding directional positions.
VIX regime adjusts position sizing globally.

Used by risk_manager to gate new orders.
"""

import time
import logging
import threading
import shared
from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_vix = 18.0     # default moderate VIX
_last_heat = 0.0
_last_updated = 0.0

VIX_NORMAL  = 20.0   # below this = low fear, normal sizing
VIX_CAUTION = 25.0   # reduce sizing 25%
VIX_HIGH    = 35.0   # reduce sizing 50%
VIX_EXTREME = 45.0   # cash only, no new positions

HEAT_MAX = 0.80      # stop adding positions above 80% portfolio heat
HEAT_WARN = 0.60     # warn above 60%


def update_vix(vix: float):
    """Called by diagnostics or signal_generator when VIX data is available."""
    global _last_vix
    with _lock:
        _last_vix = vix
    logger.debug(f"portfolio_heat: VIX updated to {vix:.1f}")


def compute_heat() -> float:
    """
    Compute current portfolio heat = sum of position market values / total equity.
    Returns float 0.0-1.0+.
    """
    with shared.account_lock:
        acct = shared.account
    with shared.positions_lock:
        positions = dict(shared.positions)

    if acct is None:
        return 0.0

    try:
        equity = float(getattr(acct, "equity", 1) or 1)
    except Exception:
        equity = 1.0

    total_exposure = 0.0
    for sym, pos in positions.items():
        try:
            mv = float(getattr(pos, "market_value", 0) or 0)
            total_exposure += abs(mv)
        except Exception:
            pass

    heat = total_exposure / equity if equity > 0 else 0.0
    global _last_heat
    _last_heat = heat
    return heat


def get_size_multiplier() -> float:
    """
    Returns a multiplier (0.0-1.0) to apply to all order sizes.
    Based on VIX regime and portfolio heat.
    """
    with _lock:
        vix = _last_vix

    heat = compute_heat()

    # VIX-based scaling
    if vix >= VIX_EXTREME:
        return 0.0    # no new positions
    elif vix >= VIX_HIGH:
        vix_mult = 0.50
    elif vix >= VIX_CAUTION:
        vix_mult = 0.75
    else:
        vix_mult = 1.0

    # Heat-based scaling
    if heat >= HEAT_MAX:
        heat_mult = 0.0
    elif heat >= HEAT_WARN:
        heat_mult = 1.0 - (heat - HEAT_WARN) / (HEAT_MAX - HEAT_WARN)
    else:
        heat_mult = 1.0

    mult = vix_mult * heat_mult
    if mult < 1.0:
        logger.debug(f"portfolio_heat: size_mult={mult:.2f} vix={vix:.1f} heat={heat:.1%}")
    return mult


def can_add_position() -> tuple:
    """
    Returns (bool, reason) -- whether a new position can be opened.
    """
    with _lock:
        vix = _last_vix

    if vix >= VIX_EXTREME:
        return False, f"VIX={vix:.0f} extreme -- cash only"

    heat = compute_heat()
    if heat >= HEAT_MAX:
        return False, f"portfolio heat={heat:.0%} at maximum"

    return True, "ok"


def get_stop_loss_distance(atr_value: float, side: str = "buy") -> float:
    """
    Compute stop loss distance using ATR.
    Standard: 2x ATR for longs, 1.5x for shorts.
    """
    if not atr_value:
        return None
    multiplier = 2.0 if side == "buy" else 1.5
    return atr_value * multiplier


def get_status() -> dict:
    """Return current heat and VIX status for diagnostics."""
    with _lock:
        vix = _last_vix
    heat = _last_heat
    mult = get_size_multiplier()
    return {
        "vix":             round(vix, 1),
        "heat":            round(heat, 3),
        "size_multiplier": round(mult, 2),
        "regime":          "extreme" if vix >= VIX_EXTREME else
                           "high"    if vix >= VIX_HIGH else
                           "caution" if vix >= VIX_CAUTION else "normal",
    }
