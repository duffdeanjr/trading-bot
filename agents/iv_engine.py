"""
agents/iv_engine.py -- Implied Volatility rank and percentile computation.
Uses historical option chain data from ref_library to compute IVR.

IVR = (current_IV - 52w_low_IV) / (52w_high_IV - 52w_low_IV) * 100
IVR > 50 = sell premium (iron condors, covered calls, CSPs)
IVR < 30 = buy premium (calendar spreads, debit spreads)
"""

import math
import logging
import shared

logger = logging.getLogger(__name__)

_iv_history: dict = {}  # symbol -> list of historical IV snapshots


def _black_scholes_iv(option_price: float, S: float, K: float,
                      T: float, r: float = 0.05, is_call: bool = True) -> float:
    """
    Newton-Raphson IV solver. Returns IV as decimal (0.25 = 25%).
    S=underlying, K=strike, T=time to expiry in years, r=risk-free rate.
    Returns None if no solution found.
    """
    if T <= 0 or S <= 0 or K <= 0 or option_price <= 0:
        return None

    def _norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def _bs_price(sigma):
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if is_call:
            return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

    def _vega(sigma):
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return S * math.sqrt(T) * math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)

    sigma = 0.25  # initial guess
    for _ in range(100):
        price = _bs_price(sigma)
        vega  = _vega(sigma)
        if vega < 1e-10:
            return None
        diff = option_price - price
        sigma += diff / vega
        if abs(diff) < 0.0001:
            return max(0.01, min(sigma, 5.0))
    return None


def snapshot_iv(symbol: str, iv: float):
    """Record a current IV snapshot for historical tracking."""
    if symbol not in _iv_history:
        _iv_history[symbol] = []
    _iv_history[symbol].append(iv)
    # Keep rolling 252 trading days (~1 year)
    if len(_iv_history[symbol]) > 252:
        _iv_history[symbol] = _iv_history[symbol][-252:]


def get_ivr(symbol: str, current_iv: float = None) -> dict:
    """
    Compute IV Rank and IV Percentile for a symbol.
    Returns {"iv": float, "ivr": float, "ivp": float, "regime": str}
    regime: "high" (sell premium) | "normal" | "low" (buy premium)
    """
    history = _iv_history.get(symbol, [])

    if current_iv is None:
        current_iv = history[-1] if history else None

    if current_iv is None:
        return {"iv": None, "ivr": None, "ivp": None, "regime": "unknown"}

    if len(history) < 20:
        # Not enough history -- use heuristic
        regime = "high" if current_iv > 0.40 else "low" if current_iv < 0.20 else "normal"
        return {"iv": current_iv, "ivr": None, "ivp": None, "regime": regime}

    iv_52w_high = max(history)
    iv_52w_low  = min(history)
    iv_range    = iv_52w_high - iv_52w_low

    ivr = ((current_iv - iv_52w_low) / iv_range * 100) if iv_range > 0 else 50.0
    ivp = (sum(1 for h in history if h < current_iv) / len(history)) * 100

    regime = "high" if ivr >= 50 else "low" if ivr <= 25 else "normal"

    return {
        "iv":      round(current_iv, 4),
        "ivr":     round(ivr, 1),
        "ivp":     round(ivp, 1),
        "iv_high": round(iv_52w_high, 4),
        "iv_low":  round(iv_52w_low, 4),
        "regime":  regime,
    }


def estimate_iv_from_chain(symbol: str, S: float) -> float:
    """
    Estimate current IV from the nearest ATM options in shared.historical_ohlcv.
    Falls back to VIX-based heuristic if no options data available.
    """
    # Try to get options chain data from ref library
    with shared.cache_lock:
        ohlcv = shared.historical_ohlcv.get(symbol, {})

    # If options data present with IV field, use it directly
    if "iv" in ohlcv:
        iv = ohlcv["iv"]
        snapshot_iv(symbol, iv)
        return iv

    # Fallback: use 30-day realized vol as IV proxy
    closes = ohlcv.get("closes", [])
    if len(closes) >= 20:
        returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        if returns:
            mean = sum(returns) / len(returns)
            variance = sum((r - mean)**2 for r in returns) / len(returns)
            realized_vol = math.sqrt(variance * 252)  # annualize
            # IV tends to be ~10-20% above realized vol
            iv = realized_vol * 1.15
            snapshot_iv(symbol, iv)
            return iv

    return None


def select_strategy(ivr_data: dict) -> str:
    """
    Given IV regime, recommend the best options strategy.
    Returns strategy name matching one in options_strategies.py.
    """
    if ivr_data is None:
        return "none"
    regime = ivr_data.get("regime", "unknown")
    ivr    = ivr_data.get("ivr")

    if regime == "high" or (ivr and ivr >= 60):
        return "iron_condor"
    elif regime == "high" or (ivr and ivr >= 40):
        return "covered_call"
    elif regime == "normal":
        return "cash_secured_put"
    elif regime == "low" or (ivr and ivr <= 25):
        return "calendar_spread"
    return "none"
