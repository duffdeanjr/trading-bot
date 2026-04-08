"""
agents/options_strategies.py -- Options strategy builders.
Each function returns a signal dict (or list of legs) ready for order_execution.

Strategies implemented:
  - iron_condor      (sell OTM call spread + OTM put spread, high IVR)
  - covered_call     (sell OTM call against long stock)
  - cash_secured_put (sell OTM put, want to own stock)
  - calendar_spread  (buy far, sell near, same strike, low IVR)
  - auto_roll        (roll expiring short options 30-45 days out)
"""

import time
import logging
import datetime
import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)


def _get_options_chain(symbol: str) -> list:
    """Get available options contracts for a symbol from database or Alpaca API."""
    # First try database (populated by ref_library._fetch_option_chains)
    chain = database.read_option_chain_latest(symbol)
    if chain:
        return chain
    # Fallback: live API call
    try:
        from alpaca_local import client as alpaca
        contracts = alpaca.get_options_contracts(
            underlying_symbols=[symbol],
            status="active",
        )
        if contracts:
            contract_list = list(contracts)
            database.write_option_chain(contract_list)
            return contract_list
    except Exception as e:
        logger.debug(f"options_strategies: chain lookup failed for {symbol}: {e}")
    return []


def _select_expiry(contracts: list, target_dte: int = 30, max_dte: int = 45) -> list:
    """Filter contracts to those expiring in target DTE range."""
    today = datetime.date.today()
    result = []
    for c in contracts:
        exp = getattr(c, 'expiration_date', None)
        if exp:
            try:
                exp_date = datetime.date.fromisoformat(str(exp))
                dte = (exp_date - today).days
                if target_dte <= dte <= max_dte:
                    result.append((dte, c))
            except Exception:
                pass
    result.sort(key=lambda x: x[0])
    return [c for _, c in result]


def _find_strike(contracts: list, underlying_price: float,
                 offset_pct: float, option_type: str) -> object:
    """
    Find contract closest to underlying_price * (1 + offset_pct).
    option_type: 'call' or 'put'
    """
    target_strike = underlying_price * (1 + offset_pct)
    candidates = [c for c in contracts
                  if str(getattr(c, 'type', '')).lower() == option_type]
    if not candidates:
        return None
    return min(candidates,
               key=lambda c: abs(float(getattr(c, 'strike_price', 0)) - target_strike))


def iron_condor(symbol: str, underlying_price: float,
                qty: int = 1, wing_width_pct: float = 0.05) -> dict:
    """
    Iron condor: sell OTM call spread + sell OTM put spread.
    Best in high IVR (>= 50) environments.
    wing_width_pct: how far OTM each short strike is (default 5%).
    Returns signal dict with order_class='mleg' or None if chain unavailable.
    """
    contracts = _get_options_chain(symbol)
    if not contracts:
        logger.debug(f"iron_condor: no options chain for {symbol}")
        return None

    chain = _select_expiry(contracts, target_dte=30, max_dte=45)
    if not chain:
        return None

    # Short strikes (closer to money, what we sell)
    short_call = _find_strike(chain, underlying_price, +0.05, "call")
    short_put  = _find_strike(chain, underlying_price, -0.05, "put")
    # Long strikes (further OTM, what we buy for protection)
    long_call  = _find_strike(chain, underlying_price, +0.05 + wing_width_pct, "call")
    long_put   = _find_strike(chain, underlying_price, -0.05 - wing_width_pct, "put")

    if not all([short_call, short_put, long_call, long_put]):
        logger.debug(f"iron_condor: could not build all legs for {symbol}")
        return None

    legs = [
        {"symbol": short_call.symbol, "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": long_call.symbol,  "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
        {"symbol": short_put.symbol,  "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": long_put.symbol,   "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
    ]

    logger.info(f"options: iron_condor {symbol} @ {underlying_price:.2f} "
                f"call_spread={getattr(short_call,'strike_price',0)}/{getattr(long_call,'strike_price',0)} "
                f"put_spread={getattr(short_put,'strike_price',0)}/{getattr(long_put,'strike_price',0)}")

    return {
        "symbol":      symbol,
        "order_class": "mleg",
        "legs":        legs,
        "qty":         qty,
        "strategy":    "iron_condor",
        "side":        "sell",
    }


def covered_call(symbol: str, underlying_price: float,
                 qty: int = 1, otm_pct: float = 0.05) -> dict:
    """
    Covered call: sell OTM call against existing long stock position.
    Generates monthly income of ~1-3% on held position.
    Only valid if plan already holds the stock.
    """
    contracts = _get_options_chain(symbol)
    if not contracts:
        return None

    chain = _select_expiry(contracts, target_dte=25, max_dte=45)
    if not chain:
        return None

    call = _find_strike(chain, underlying_price, otm_pct, "call")
    if not call:
        return None

    strike = float(getattr(call, 'strike_price', 0))
    logger.info(f"options: covered_call {symbol} sell call @ {strike:.2f}")

    return {
        "symbol":      call.symbol,
        "side":        "sell",
        "qty":         qty,
        "strategy":    "covered_call",
        "order_class": "simple",
    }


def cash_secured_put(symbol: str, underlying_price: float,
                     qty: int = 1, otm_pct: float = 0.05) -> dict:
    """
    Cash-secured put: sell OTM put to either collect premium
    or acquire the stock at a discount.
    Best for high-conviction stocks you want to own.
    """
    contracts = _get_options_chain(symbol)
    if not contracts:
        return None

    chain = _select_expiry(contracts, target_dte=25, max_dte=45)
    if not chain:
        return None

    put = _find_strike(chain, underlying_price, -otm_pct, "put")
    if not put:
        return None

    strike = float(getattr(put, 'strike_price', 0))
    logger.info(f"options: cash_secured_put {symbol} sell put @ {strike:.2f}")

    return {
        "symbol":      put.symbol,
        "side":        "sell",
        "qty":         qty,
        "strategy":    "cash_secured_put",
        "order_class": "simple",
    }


def calendar_spread(symbol: str, underlying_price: float,
                    qty: int = 1) -> dict:
    """
    Calendar spread: buy far-dated, sell near-dated at same ATM strike.
    Best in low IVR environments where you expect IV to expand.
    """
    contracts = _get_options_chain(symbol)
    if not contracts:
        return None

    near_chain = _select_expiry(contracts, target_dte=14, max_dte=21)
    far_chain  = _select_expiry(contracts, target_dte=45, max_dte=60)

    if not near_chain or not far_chain:
        return None

    near_call = _find_strike(near_chain, underlying_price, 0, "call")
    far_call  = _find_strike(far_chain,  underlying_price, 0, "call")

    if not near_call or not far_call:
        return None

    legs = [
        {"symbol": near_call.symbol, "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": far_call.symbol,  "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
    ]

    logger.info(f"options: calendar_spread {symbol} @ {underlying_price:.2f}")

    return {
        "symbol":      symbol,
        "order_class": "mleg",
        "legs":        legs,
        "qty":         qty,
        "strategy":    "calendar_spread",
        "side":        "buy",
    }


def check_rolls_needed(positions: dict) -> list:
    """
    Check all open options positions and return list of roll signals
    for those expiring within EXPIRY_WARN_DAYS.
    """
    today = datetime.date.today()
    rolls = []

    for symbol, pos in positions.items():
        if "/" not in symbol and len(symbol) < 10:
            continue  # skip equity positions, only look at options

        # Try to parse expiry from option symbol (Alpaca format: AAPL240119C00180000)
        try:
            exp_str = symbol[len(symbol)-15:len(symbol)-9]  # YYMMDD
            if len(exp_str) == 6 and exp_str.isdigit():
                exp_date = datetime.date(
                    2000 + int(exp_str[:2]),
                    int(exp_str[2:4]),
                    int(exp_str[4:6])
                )
                dte = (exp_date - today).days
                if 0 < dte <= settings.EXPIRY_WARN_DAYS:
                    rolls.append({
                        "symbol":   symbol,
                        "dte":      dte,
                        "action":   "roll",
                        "strategy": "auto_roll",
                    })
                    logger.info(f"options: {symbol} needs rolling ({dte}d to expiry)")
        except Exception:
            pass

    return rolls
