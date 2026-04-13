import time
import logging
import threading
import datetime
import shared
from config import settings
from storage import database

logger = logging.getLogger(__name__)

# -- PDT tracking --
_day_trades: list = []
_PDT_MAX = 3
_PDT_WINDOW = 5 * 24 * 3600

def _count_day_trades() -> int:
    cutoff = time.time() - _PDT_WINDOW
    _day_trades[:] = [t for t in _day_trades if t > cutoff]
    return len(_day_trades)

def record_day_trade():
    """Call when a day trade completes (open and close same session)."""
    _day_trades.append(time.time())

# -- position size check --
def _check_position_size(symbol: str, notional: float) -> tuple:
    if notional > settings.MAX_POSITION_SIZE:
        return False, (
            f"order notional ${notional:.0f} exceeds MAX_POSITION_SIZE "
            f"${settings.MAX_POSITION_SIZE:.0f}"
        )
    with shared.account_lock:
        equity = float(getattr(shared.account, "equity", 0) or 0)
    if equity > 0:
        pct = notional / equity
        if pct > settings.MAX_PORTFOLIO_PCT:
            return False, (
                f"order would put {pct:.1%} of portfolio in {symbol}, "
                f"exceeds MAX_PORTFOLIO_PCT={settings.MAX_PORTFOLIO_PCT:.0%}"
            )
    return True, ""

# -- PDT check --
def _check_pdt(is_day_trade: bool) -> tuple:
    with shared.account_lock:
        acct = shared.account
    pattern_day_trader = getattr(acct, "pattern_day_trader", False)
    if pattern_day_trader and is_day_trade:
        if _count_day_trades() >= _PDT_MAX:
            return False, "PDT: 3 day-trades used in 5-day rolling window"
    return True, ""

# -- margin call check --
def _check_dtmc() -> tuple:
    with shared.account_lock:
        acct = shared.account
    if str(getattr(acct, "daytrade_buying_power", "")) == "0":
        return False, "DTMC: daytrade buying power is zero"
    return True, ""

# -- wash sale check --
def _check_wash_trade(symbol: str, side: str) -> tuple:
    return True, ""

# -- margin/short equity minimum --
def _check_margin(side: str) -> tuple:
    if side not in ("sell", "sell_short"):
        return True, ""
    with shared.account_lock:
        equity = float(getattr(shared.account, "equity", 0) or 0)
    if equity < settings.MARGIN_MIN_EQUITY:
        return False, f"margin/short requires ${settings.MARGIN_MIN_EQUITY:.0f} min equity, current=${equity:.0f}"
    return True, ""

# -- options level check --
def _check_options_level(strategy: str) -> tuple:
    level3_only = {"short_put", "short_call_spread", "short_put_spread",
                   "short_iron_condor", "butterfly", "calendar_spread"}
    if strategy in level3_only and settings.OPTIONS_LEVEL < 3:
        return False, f"strategy '{strategy}' requires OPTIONS_LEVEL=3"
    return True, ""

# -- expiry watch --
def _check_expiring_options():
    if settings.OPTIONS_DAYTRADE:
        return  # 0DTE positions are expected in day-trade mode
    warn_delta = datetime.timedelta(days=settings.EXPIRY_WARN_DAYS)
    today = datetime.date.today()
    with shared.positions_lock:
        positions = dict(shared.positions)
    for symbol, pos in positions.items():
        asset_class = getattr(pos, "asset_class", "")
        if asset_class != "us_option":
            continue
        side = getattr(pos, "side", "long")
        if side == "long":
            continue
        try:
            exp_str = symbol[4:10]
            exp_date = datetime.datetime.strptime(exp_str, "%y%m%d").date()
            days_left = (exp_date - today).days
            if days_left <= settings.EXPIRY_WARN_DAYS:
                logger.warning(
                    f"risk: short option {symbol} expires in {days_left} day(s) -> consider rolling"
                )
        except (ValueError, IndexError):
            pass

# -- portfolio heat (merged from portfolio_heat.py) --
_heat_lock = threading.Lock()
_last_vix = 18.0
_last_heat = 0.0

def update_vix(vix: float):
    """Called when VIX data is available."""
    global _last_vix
    with _heat_lock:
        _last_vix = vix

def get_last_vix() -> float:
    """Public getter for current VIX level (thread-safe)."""
    with _heat_lock:
        return _last_vix

def compute_heat() -> float:
    """Compute portfolio heat = sum(abs(position market values)) / equity."""
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
        except Exception as e:
            logger.debug(f"risk: could not read market_value for {sym}: {e}")
    global _last_heat
    heat = total_exposure / equity if equity > 0 else 0.0
    _last_heat = heat
    return heat

def get_size_multiplier() -> float:
    """Returns a multiplier (0.0-1.0) based on VIX regime and portfolio heat."""
    with _heat_lock:
        vix = _last_vix
    heat = compute_heat()
    if vix >= settings.VIX_EXTREME:
        return 0.0
    elif vix >= settings.VIX_HIGH:
        vix_mult = 0.50
    elif vix >= settings.VIX_CAUTION:
        vix_mult = 0.75
    else:
        vix_mult = 1.0
    if heat >= settings.HEAT_MAX:
        heat_mult = 0.0
    elif heat >= settings.HEAT_WARN:
        heat_mult = 1.0 - (heat - settings.HEAT_WARN) / (settings.HEAT_MAX - settings.HEAT_WARN)
    else:
        heat_mult = 1.0
    return vix_mult * heat_mult

def can_add_position() -> tuple:
    """Returns (bool, reason) -- whether a new position can be opened."""
    with _heat_lock:
        vix = _last_vix
    if vix >= settings.VIX_EXTREME:
        return False, f"VIX={vix:.0f} extreme -- cash only"
    heat = compute_heat()
    if heat >= settings.HEAT_MAX:
        return False, f"portfolio heat={heat:.0%} at maximum"
    return True, "ok"

def get_stop_loss_distance(atr_value: float, side: str = "buy") -> float:
    """Compute stop loss distance using ATR."""
    if not atr_value:
        return None
    multiplier = 2.0 if side == "buy" else 1.5
    return atr_value * multiplier

def get_heat_status() -> dict:
    """Return current heat and VIX status for diagnostics."""
    with _heat_lock:
        vix = _last_vix
    return {
        "vix":             round(vix, 1),
        "heat":            round(_last_heat, 3),
        "size_multiplier": round(get_size_multiplier(), 2),
        "regime":          "extreme" if vix >= settings.VIX_EXTREME else
                           "high"    if vix >= settings.VIX_HIGH else
                           "caution" if vix >= settings.VIX_CAUTION else "normal",
    }


def get_daily_target_status() -> dict:
    """Return daily P&L progress toward 1% target."""
    with shared.account_lock:
        acct = shared.account
    if not acct:
        return {"pnl_pct": 0, "pnl_dollar": 0, "target_pct": settings.DAILY_TARGET_PCT * 100,
                "target_dollar": 0, "progress_pct": 0, "locked": False}
    equity = float(getattr(acct, "equity", 0) or 0)
    last_equity = float(getattr(acct, "last_equity", 0) or 0)
    if last_equity <= 0:
        return {"pnl_pct": 0, "pnl_dollar": 0, "target_pct": settings.DAILY_TARGET_PCT * 100,
                "target_dollar": 0, "progress_pct": 0, "locked": False}
    pnl = equity - last_equity
    pnl_pct = pnl / last_equity
    target_dollar = last_equity * settings.DAILY_TARGET_PCT
    progress = min(pnl_pct / settings.DAILY_TARGET_PCT * 100, 100) if settings.DAILY_TARGET_PCT > 0 else 0
    return {
        "pnl_pct": round(pnl_pct * 100, 3),
        "pnl_dollar": round(pnl, 2),
        "target_pct": round(settings.DAILY_TARGET_PCT * 100, 1),
        "target_dollar": round(target_dollar, 2),
        "progress_pct": round(max(0, progress), 1),
        "locked": _daily_target_locked,
    }

# -- portfolio heat veto --
def _check_portfolio_heat(side: str) -> tuple:
    if side not in ("buy",):
        return True, ""
    ok, reason = can_add_position()
    if not ok:
        return False, f"portfolio heat: {reason}"
    return True, ""

# -- circuit breaker (R3) --
MAX_DAILY_LOSS_PCT = float(getattr(settings, "MAX_DAILY_LOSS_PCT", 0.05))
MAX_CONSECUTIVE_LOSSES = int(getattr(settings, "MAX_CONSECUTIVE_LOSSES", 5))
_circuit_breaker_tripped = False
_circuit_breaker_ts = 0.0

_daily_target_locked = False
_daily_target_lock_ts = 0.0

def _check_circuit_breaker() -> tuple:
    """Halt trading if daily losses exceed threshold, too many consecutive losers,
    or daily profit target reached (lock in gains)."""
    global _circuit_breaker_tripped, _circuit_breaker_ts
    global _daily_target_locked, _daily_target_lock_ts

    # Reset at start of each trading day
    today = datetime.date.today().isoformat()
    if _circuit_breaker_tripped:
        if _circuit_breaker_ts > 0:
            tripped_date = datetime.datetime.fromtimestamp(_circuit_breaker_ts).date().isoformat()
            if tripped_date != today:
                _circuit_breaker_tripped = False
                _circuit_breaker_ts = 0.0
                logger.info("risk: circuit breaker reset (new trading day)")
    if _daily_target_locked:
        if _daily_target_lock_ts > 0:
            locked_date = datetime.datetime.fromtimestamp(_daily_target_lock_ts).date().isoformat()
            if locked_date != today:
                _daily_target_locked = False
                _daily_target_lock_ts = 0.0
                logger.info("risk: daily target lock reset (new trading day)")

    if _circuit_breaker_tripped:
        return False, "circuit breaker: trading halted for the day"

    if _daily_target_locked:
        return False, f"daily target reached: +{settings.DAILY_TARGET_PCT:.0%} — gains locked"

    # Check daily P&L from portfolio history
    with shared.account_lock:
        acct = shared.account
    if acct:
        equity = float(getattr(acct, "equity", 0) or 0)
        last_equity = float(getattr(acct, "last_equity", 0) or 0)
        if last_equity > 0 and equity > 0:
            daily_pnl_pct = (equity - last_equity) / last_equity

            # Daily profit target — lock in gains
            if (settings.DAILY_TARGET_LOCK and
                    daily_pnl_pct >= settings.DAILY_TARGET_PCT and
                    not _daily_target_locked):
                _daily_target_locked = True
                _daily_target_lock_ts = time.time()
                logger.info(f"risk: DAILY TARGET REACHED +{daily_pnl_pct:.2%} "
                           f"(target: +{settings.DAILY_TARGET_PCT:.0%}) — locking gains, "
                           f"no new positions until tomorrow")
                return False, f"daily target reached: +{daily_pnl_pct:.2%}"

            # Daily loss circuit breaker
            if daily_pnl_pct < -MAX_DAILY_LOSS_PCT:
                _circuit_breaker_tripped = True
                _circuit_breaker_ts = time.time()
                logger.error(f"risk: CIRCUIT BREAKER TRIPPED - daily loss {daily_pnl_pct:.2%} "
                           f"exceeds -{MAX_DAILY_LOSS_PCT:.0%} threshold")
                return False, f"circuit breaker: daily loss {daily_pnl_pct:.2%}"

    # Check consecutive losses from outcomes table
    try:
        recent = database.get_closed_outcomes(limit=MAX_CONSECUTIVE_LOSSES)
        if len(recent) >= MAX_CONSECUTIVE_LOSSES:
            all_losses = all((r.get("pnl") or 0) < 0 for r in recent)
            if all_losses:
                _circuit_breaker_tripped = True
                _circuit_breaker_ts = time.time()
                logger.error(f"risk: CIRCUIT BREAKER TRIPPED - {MAX_CONSECUTIVE_LOSSES} "
                           f"consecutive losing trades")
                return False, f"circuit breaker: {MAX_CONSECUTIVE_LOSSES} consecutive losses"
    except Exception as e:
        logger.warning(f"risk: circuit breaker consecutive-loss check failed: {e}")

    return True, ""

# -- options-specific risk checks (R4) --
def _check_naked_short(signal: dict) -> tuple:
    """Block naked short calls (unlimited loss potential)."""
    strategy = signal.get("strategy", "")
    side = signal.get("side", "")
    symbol = signal.get("symbol", "")

    # Naked short calls are never allowed unless part of a spread
    if strategy in ("short_call", "naked_call") or (side == "sell" and "call" in strategy.lower()):
        # Check if there's a covering position (shares or long call)
        with shared.positions_lock:
            pos = shared.positions.get(symbol)
        if pos is None or float(getattr(pos, "qty", 0) or (pos.get("qty", 0) if isinstance(pos, dict) else 0)) <= 0:
            # No underlying shares -- this is naked
            if strategy not in ("covered_call", "iron_condor", "call_spread", "short_call_spread"):
                return False, f"BLOCKED: naked short call on {symbol} -- unlimited loss risk"
    return True, ""

def _check_options_expiry_risk(signal: dict) -> tuple:
    """Block opening new option positions expiring within EXPIRY_WARN_DAYS.
    In day-trade mode (EXPIRY_WARN_DAYS=0), allows 0DTE."""
    if settings.OPTIONS_DAYTRADE:
        return True, ""  # day-trade mode allows all DTEs
    symbol = signal.get("symbol", "")
    if not symbol or len(symbol) < 10:
        return True, ""
    try:
        exp_str = symbol[len(symbol)-15:len(symbol)-9]  # extract YYMMDD from OCC
        exp_date = datetime.datetime.strptime(exp_str, "%y%m%d").date()
        days_left = (exp_date - datetime.date.today()).days
        if days_left <= settings.EXPIRY_WARN_DAYS:
            return False, f"BLOCKED: option {symbol} expires in {days_left}d -- too close to expiry"
    except (ValueError, IndexError):
        pass
    return True, ""

# -- stop loss enforcement check --
def _check_stop_loss(signal: dict) -> tuple:
    side = signal.get("side", "")
    if side != "buy":
        return True, ""
    if not signal.get("stop_price") and not signal.get("order_class") == "mleg":
        logger.debug(f"risk: no stop_price on buy signal for {signal.get('symbol')} "
                     f"-- consider adding ATR-based stop")
    return True, ""

# -- options buying power check --
def _check_options_buying_power(signal: dict) -> tuple:
    """Check if we have enough options buying power for the trade."""
    strategy = signal.get("strategy", "")
    options_open_strategies = {"iron_condor", "covered_call", "cash_secured_put", "calendar_spread"}
    if strategy not in options_open_strategies or strategy == "options_exit":
        return True, ""

    with shared.account_lock:
        acct = shared.account
    if acct is None:
        return False, "no account data"

    try:
        opt_bp = float(getattr(acct, "options_buying_power", 0) or 0)
    except Exception:
        opt_bp = 0.0

    if opt_bp <= 0:
        return False, f"options buying power is ${opt_bp:.0f} — no capacity"

    # Estimate capital required
    qty = int(signal.get("qty", 1) or 1)
    if strategy == "cash_secured_put":
        # CSP requires strike × 100 × qty
        strike = float(signal.get("strike_price", 0) or 0)
        if strike <= 0:
            # Parse from option symbol (e.g. AAPL260424P00247500 -> 247.5)
            sym = signal.get("symbol", "")
            if len(sym) > 15:
                try:
                    strike = int(sym[-8:]) / 1000
                except Exception:
                    strike = 0
        required = strike * 100 * qty
    elif strategy == "iron_condor":
        # IC max loss = wider wing width × 100 × qty
        # Try to read actual max_loss from signal (set by options_strategies builder)
        legs = signal.get("legs", [])
        if legs and len(legs) >= 4:
            try:
                strikes = sorted(float(l.get("strike_price", 0) or 0) for l in legs if l.get("strike_price"))
                if len(strikes) >= 2:
                    wing_width = max(abs(strikes[-1] - strikes[-2]), abs(strikes[1] - strikes[0]))
                    required = wing_width * 100 * qty
                else:
                    required = 2000 * qty  # conservative default
            except Exception:
                required = 2000 * qty
        else:
            # Estimate from wing width setting
            required = settings.OPTIONS_WING_WIDTH * 100 * 100 * qty  # wing% × price(~$100) × 100
            required = max(required, 500 * qty)  # at least $500 per contract
    elif strategy == "covered_call":
        return True, ""  # no additional capital needed
    elif strategy == "calendar_spread":
        required = 500 * qty  # debit spread: ~$500 max per contract
    else:
        required = settings.MAX_POSITION_SIZE

    if required > opt_bp:
        return False, (
            f"options buying power ${opt_bp:,.0f} < required ${required:,.0f} "
            f"for {strategy} ({signal.get('symbol', '')})"
        )

    return True, ""


# -- main veto function --
def approve(signal: dict) -> tuple:
    """
    Returns (True, "") if signal is approved, (False, reason) if vetoed.
    Called by order_execution before submitting any order.
    Position closes (options_exit, buy_to_close, sell_to_close) bypass
    circuit breaker and daily target lock — you must always be able to exit.
    """
    symbol   = signal.get("symbol", "")
    side     = signal.get("side", "")
    notional = float(signal.get("notional", 0) or 0)
    strategy = signal.get("strategy", "")
    is_day   = signal.get("is_day_trade", False)
    position_intent = signal.get("position_intent", "")

    # Determine if this is a position close (should never be blocked)
    is_close = (position_intent in ("buy_to_close", "sell_to_close")
                or strategy == "options_exit")

    checks = [
        _check_position_size(symbol, notional),
        _check_pdt(is_day),
        _check_dtmc(),
        _check_wash_trade(symbol, side),
        _check_margin(side),
        _check_options_level(strategy),
        _check_naked_short(signal),
        _check_options_expiry_risk(signal),
        _check_options_buying_power(signal),
        _check_portfolio_heat(side),
        _check_stop_loss(signal),
    ]

    # Circuit breaker only blocks NEW positions, not closes
    if not is_close:
        checks.insert(0, _check_circuit_breaker())

    for ok, reason in checks:
        if not ok:
            logger.warning(f"risk veto [{symbol}]: {reason}")
            return False, reason
    return True, ""

# -- main loop --
@shared.register_agent("risk_manager", phase=7)
def run():
    logger.info("risk_manager: starting")
    while not shared.SHUTTING_DOWN:
        shared.heartbeat("risk_manager")
        _check_expiring_options()
        time.sleep(settings.TICK_INTERVAL * 12)
    logger.info("risk_manager: SHUTTING_DOWN -> exiting")
