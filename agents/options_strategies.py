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
    """Get available options contracts for a symbol from Alpaca API.
    Always fetches live data (chains change too fast to rely on DB cache).
    In day-trade mode, fetches 0-3 DTE; otherwise 7-60 DTE."""
    try:
        from alpaca_local import client as alpaca
        today = datetime.date.today()
        if settings.OPTIONS_DAYTRADE:
            gte = today
            lte = today + datetime.timedelta(days=3)
            label = "0-3 DTE"
        else:
            gte = today + datetime.timedelta(days=7)
            lte = today + datetime.timedelta(days=60)
            label = "7-60 DTE"
        contracts = alpaca.get_options_contracts(
            underlying_symbols=[symbol],
            status="active",
            expiration_date_gte=gte,
            expiration_date_lte=lte,
        )
        if contracts:
            result = list(contracts) if not isinstance(contracts, list) else contracts
            logger.info(f"options_chain: {symbol} -> {len(result)} contracts ({label})")
            return result
        else:
            logger.info(f"options_chain: {symbol} -> 0 contracts ({label})")
    except Exception as e:
        logger.info(f"options_chain: {symbol} lookup failed: {e}")
    return []


def _get_attr(c, name, default=None):
    """Get attribute from either an object or dict."""
    if isinstance(c, dict):
        return c.get(name, default)
    return getattr(c, name, default)


def _get_type(c) -> str:
    """Get option type as lowercase string ('call' or 'put')."""
    raw = _get_attr(c, 'type', '')
    if hasattr(raw, 'value'):
        return raw.value.lower()
    return str(raw).lower()


def _get_expiry(c) -> datetime.date:
    """Get expiration date from contract (object or dict)."""
    exp = _get_attr(c, 'expiration_date') or _get_attr(c, 'expiry')
    if exp is None:
        return None
    if isinstance(exp, datetime.date):
        return exp
    try:
        return datetime.date.fromisoformat(str(exp)[:10])
    except Exception:
        return None


def _select_expiry(contracts: list, target_dte: int = 30, max_dte: int = 45) -> list:
    """Filter contracts to those expiring in target DTE range.
    In day-trade mode, overrides to 0-3 DTE."""
    if settings.OPTIONS_DAYTRADE:
        target_dte = 0
        max_dte = 3
    today = datetime.date.today()
    result = []
    for c in contracts:
        exp_date = _get_expiry(c)
        if exp_date:
            dte = (exp_date - today).days
            if target_dte <= dte <= max_dte:
                result.append((dte, c))
    result.sort(key=lambda x: x[0])
    return [c for _, c in result]


def _find_strike(contracts: list, underlying_price: float,
                 offset_pct: float, option_type: str) -> object:
    """
    Find contract closest to underlying_price * (1 + offset_pct).
    option_type: 'call' or 'put'
    """
    target_strike = underlying_price * (1 + offset_pct)
    candidates = [c for c in contracts if _get_type(c) == option_type]
    if not candidates:
        return None
    return min(candidates,
               key=lambda c: abs(float(_get_attr(c, 'strike_price', 0)) - target_strike))


def iron_condor(symbol: str, underlying_price: float,
                qty: int = 1, wing_width_pct: float = None) -> dict:
    """
    Iron condor: sell OTM call spread + sell OTM put spread.
    In day-trade mode uses tighter strikes from settings.
    Returns signal dict with order_class='mleg' or None if chain unavailable.
    """
    if wing_width_pct is None:
        wing_width_pct = settings.OPTIONS_WING_WIDTH
    otm_pct = settings.OPTIONS_OTM_PCT if settings.OPTIONS_DAYTRADE else 0.05

    contracts = _get_options_chain(symbol)
    if not contracts:
        logger.info(f"iron_condor: no options chain for {symbol}")
        return None

    chain = _select_expiry(contracts)
    if not chain:
        logger.info(f"iron_condor: no contracts in DTE range for {symbol} ({len(contracts)} total)")
        return None

    # Short strikes (closer to money, what we sell)
    short_call = _find_strike(chain, underlying_price, +otm_pct, "call")
    short_put  = _find_strike(chain, underlying_price, -otm_pct, "put")
    # Long strikes (further OTM, what we buy for protection)
    long_call  = _find_strike(chain, underlying_price, +otm_pct + wing_width_pct, "call")
    long_put   = _find_strike(chain, underlying_price, -otm_pct - wing_width_pct, "put")

    if not all([short_call, short_put, long_call, long_put]):
        logger.info(f"iron_condor: could not build all legs for {symbol} "
                     f"(sc={short_call is not None} sp={short_put is not None} "
                     f"lc={long_call is not None} lp={long_put is not None})")
        return None

    short_call_strike = float(_get_attr(short_call, 'strike_price', 0))
    long_call_strike  = float(_get_attr(long_call, 'strike_price', 0))
    short_put_strike  = float(_get_attr(short_put, 'strike_price', 0))
    long_put_strike   = float(_get_attr(long_put, 'strike_price', 0))

    # Validate all 4 legs are distinct contracts
    leg_symbols = {short_call.symbol, long_call.symbol, short_put.symbol, long_put.symbol}
    if len(leg_symbols) < 4:
        logger.info(f"iron_condor: duplicate legs for {symbol} — strikes too close "
                     f"(sc={short_call_strike} lc={long_call_strike} "
                     f"sp={short_put_strike} lp={long_put_strike})")
        return None

    # Max loss = wider wing width × 100
    call_width = abs(long_call_strike - short_call_strike)
    put_width  = abs(short_put_strike - long_put_strike)
    if call_width == 0 or put_width == 0:
        logger.info(f"iron_condor: zero-width spread for {symbol}")
        return None
    max_loss_per_contract = max(call_width, put_width) * 100

    # Size by buying power
    opt_bp = _get_options_buying_power()
    if opt_bp < max_loss_per_contract:
        logger.info(f"options: IC {symbol} needs ${max_loss_per_contract:,.0f}, "
                    f"only ${opt_bp:,.0f} available — skipping")
        return None

    max_contracts = max(1, int((opt_bp * 0.40) / max_loss_per_contract))
    qty = min(qty, max_contracts)

    legs = [
        {"symbol": short_call.symbol, "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": long_call.symbol,  "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
        {"symbol": short_put.symbol,  "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": long_put.symbol,   "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
    ]

    logger.info(f"options: iron_condor {symbol} @ {underlying_price:.2f} "
                f"call={short_call_strike}/{long_call_strike} "
                f"put={short_put_strike}/{long_put_strike} "
                f"qty={qty} max_loss=${max_loss_per_contract * qty:,.0f} bp=${opt_bp:,.0f}")

    return {
        "symbol":      symbol,
        "order_class": "mleg",
        "legs":        legs,
        "qty":         qty,
        "strategy":    "iron_condor",
        "side":        "sell",
    }


def covered_call(symbol: str, underlying_price: float,
                 qty: int = 1, otm_pct: float = None) -> dict:
    """
    Covered call: sell OTM call against existing long stock position.
    In day-trade mode uses tighter OTM from settings.
    """
    if otm_pct is None:
        otm_pct = settings.OPTIONS_OTM_PCT if settings.OPTIONS_DAYTRADE else 0.05

    contracts = _get_options_chain(symbol)
    if not contracts:
        logger.info(f"covered_call: no options chain for {symbol}")
        return None

    chain = _select_expiry(contracts)
    if not chain:
        logger.info(f"covered_call: no contracts in 7-45 DTE for {symbol}")
        return None

    call = _find_strike(chain, underlying_price, otm_pct, "call")
    if not call:
        logger.info(f"covered_call: no OTM call for {symbol} @ {underlying_price:.2f}")
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


def _get_options_buying_power() -> float:
    """Get current options buying power from account."""
    with shared.account_lock:
        acct = shared.account
    if acct is None:
        return 0.0
    try:
        return float(getattr(acct, "options_buying_power", 0) or 0)
    except Exception:
        return 0.0


def cash_secured_put(symbol: str, underlying_price: float,
                     qty: int = 1, otm_pct: float = None) -> dict:
    """
    Cash-secured put: sell OTM put.
    In day-trade mode uses tighter OTM from settings.
    """
    if otm_pct is None:
        otm_pct = settings.OPTIONS_OTM_PCT if settings.OPTIONS_DAYTRADE else 0.05

    contracts = _get_options_chain(symbol)
    if not contracts:
        logger.info(f"cash_secured_put: no options chain for {symbol}")
        return None

    chain = _select_expiry(contracts)
    if not chain:
        logger.info(f"cash_secured_put: no contracts in 7-45 DTE for {symbol} ({len(contracts)} total)")
        return None

    put = _find_strike(chain, underlying_price, -otm_pct, "put")
    if not put:
        logger.info(f"cash_secured_put: no OTM put found for {symbol} @ {underlying_price:.2f}")
        return None

    strike = float(_get_attr(put, 'strike_price', 0))
    if strike <= 0:
        return None

    # Size by buying power: CSP requires strike × 100 per contract
    opt_bp = _get_options_buying_power()
    capital_per_contract = strike * 100
    if opt_bp < capital_per_contract:
        logger.info(f"options: CSP {symbol} needs ${capital_per_contract:,.0f}, "
                    f"only ${opt_bp:,.0f} available — skipping")
        return None

    # Use at most 40% of options BP per single CSP, min 1 contract
    max_contracts = max(1, int((opt_bp * 0.40) / capital_per_contract))
    qty = min(qty, max_contracts)

    logger.info(f"options: cash_secured_put {symbol} sell {qty}x put @ {strike:.2f} "
                f"(requires ${capital_per_contract * qty:,.0f}, bp=${opt_bp:,.0f})")

    return {
        "symbol":       put.symbol,
        "side":         "sell",
        "qty":          qty,
        "strike_price": strike,
        "strategy":     "cash_secured_put",
        "order_class":  "simple",
    }


def calendar_spread(symbol: str, underlying_price: float,
                    qty: int = 1) -> dict:
    """
    Calendar spread: buy far-dated, sell near-dated at same ATM strike.
    Best in low IVR environments where you expect IV to expand.
    """
    contracts = _get_options_chain(symbol)
    if not contracts:
        logger.info(f"calendar_spread: no options chain for {symbol}")
        return None

    if settings.OPTIONS_DAYTRADE:
        near_chain = _select_expiry(contracts, target_dte=0, max_dte=1)
        far_chain  = _select_expiry(contracts, target_dte=2, max_dte=7)
    else:
        near_chain = _select_expiry(contracts, target_dte=7, max_dte=21)
        far_chain  = _select_expiry(contracts, target_dte=30, max_dte=60)

    if not near_chain or not far_chain:
        logger.info(f"calendar_spread: DTE mismatch for {symbol} "
                     f"(near={len(near_chain) if near_chain else 0}, far={len(far_chain) if far_chain else 0})")
        return None

    near_call = _find_strike(near_chain, underlying_price, 0, "call")
    far_call  = _find_strike(far_chain,  underlying_price, 0, "call")

    if not near_call or not far_call:
        logger.info(f"calendar_spread: no ATM calls for {symbol}")
        return None

    # Calendar spreads MUST have different expiries (same strike, different dates)
    if near_call.symbol == far_call.symbol:
        logger.info(f"calendar_spread: duplicate legs for {symbol} — same contract selected")
        return None

    legs = [
        {"symbol": near_call.symbol, "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        {"symbol": far_call.symbol,  "side": "buy",  "ratio_qty": 1, "position_intent": "buy_to_open"},
    ]

    logger.info(f"options: calendar_spread {symbol} @ {underlying_price:.2f} "
                f"near={near_call.symbol} far={far_call.symbol}")

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


# ── day-trade exit helpers ───────────────────────────────────────

def build_close_signal(symbol: str, qty: float, side: str, strategy: str = "options_exit") -> dict:
    """Build a signal to close an existing options position.
    side should be the CLOSING side: 'buy' to close a short, 'sell' to close a long."""
    intent = "buy_to_close" if side == "buy" else "sell_to_close"
    return {
        "symbol":          symbol,
        "side":            side,
        "qty":             abs(qty),
        "strategy":        strategy,
        "order_class":     "simple",
        "position_intent": intent,
        "time_in_force":   "day",
    }


def get_options_positions() -> list:
    """Return list of current options positions from shared state."""
    with shared.positions_lock:
        positions = dict(shared.positions)
    result = []
    for sym, pos in positions.items():
        if len(sym) > 10:  # options have long OCC symbols
            result.append(pos)
    return result
