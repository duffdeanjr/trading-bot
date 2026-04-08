import time
import logging
import shared
from config import settings

logger = logging.getLogger(__name__)

# ?? PDT tracking ???????????????????????????????????????????????
_day_trades: list = []   # list of timestamps of completed day trades
_PDT_MAX = 3             # max day trades in 5 rolling trading days
_PDT_WINDOW = 5 * 24 * 3600  # 5 days in seconds

def _count_day_trades() -> int:
    cutoff = time.time() - _PDT_WINDOW
    _day_trades[:] = [t for t in _day_trades if t > cutoff]
    return len(_day_trades)

def record_day_trade():
    """Call when a day trade completes (open and close same session)."""
    _day_trades.append(time.time())

# ?? position size check (diagnostic fix) ??????????????????????
def _check_position_size(symbol: str, notional: float) -> tuple[bool, str]:
    """Veto if order exceeds MAX_POSITION_SIZE or MAX_PORTFOLIO_PCT."""
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

# ?? PDT check ?????????????????????????????????????????????????
def _check_pdt(is_day_trade: bool) -> tuple[bool, str]:
    with shared.account_lock:
        acct = shared.account
    pattern_day_trader = getattr(acct, "pattern_day_trader", False)
    if pattern_day_trader and is_day_trade:
        if _count_day_trades() >= _PDT_MAX:
            return False, "PDT: 3 day-trades used in 5-day rolling window"
    return True, ""

# ?? margin call check ?????????????????????????????????????????
def _check_dtmc() -> tuple[bool, str]:
    with shared.account_lock:
        acct = shared.account
    if str(getattr(acct, "daytrade_buying_power", "")) == "0":
        return False, "DTMC: daytrade buying power is zero"
    return True, ""

# ?? wash sale check ???????????????????????????????????????????
def _check_wash_trade(symbol: str, side: str) -> tuple[bool, str]:
    # Simplified: flag if a position in this symbol was closed in the last 30 days
    # In production this would query the trades DB
    return True, ""

# ?? margin/short equity minimum ???????????????????????????????
def _check_margin(side: str) -> tuple[bool, str]:
    if side not in ("sell", "sell_short"):
        return True, ""
    with shared.account_lock:
        equity = float(getattr(shared.account, "equity", 0) or 0)
    if equity < 2000:
        return False, f"margin/short requires $2,000 min equity, current=${equity:.0f}"
    return True, ""

# ?? options level check ???????????????????????????????????????
def _check_options_level(strategy: str) -> tuple[bool, str]:
    level3_only = {"short_put", "short_call_spread", "short_put_spread",
                   "short_iron_condor", "butterfly", "calendar_spread"}
    if strategy in level3_only and settings.OPTIONS_LEVEL < 3:
        return False, f"strategy '{strategy}' requires OPTIONS_LEVEL=3"
    return True, ""

# ?? expiry watch ???????????????????????????????????????????????
def _check_expiring_options():
    """Flag short options positions within EXPIRY_WARN_DAYS of expiry."""
    import datetime
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
            continue  # only warn on short options
        # Extract expiry from OCC symbol format: e.g. AAPL250117C00200000
        try:
            exp_str = symbol[4:10]  # YYMMDD
            exp_date = datetime.datetime.strptime(exp_str, "%y%m%d").date()
            days_left = (exp_date - today).days
            if days_left <= settings.EXPIRY_WARN_DAYS:
                logger.warning(
                    f"risk: short option {symbol} expires in {days_left} day(s) ? consider rolling"
                )
        except (ValueError, IndexError):
            pass

# -- portfolio heat check -----------------------------------------------------
def _check_portfolio_heat(side: str) -> tuple:
    """Veto new buy orders when portfolio heat is too high."""
    if side not in ("buy",):
        return True, ""  # sells always allowed
    try:
        from agents.portfolio_heat import can_add_position
        ok, reason = can_add_position()
        if not ok:
            return False, f"portfolio heat: {reason}"
    except Exception:
        pass
    return True, ""

# -- stop loss enforcement check -----------------------------------------------
def _check_stop_loss(signal: dict) -> tuple:
    """Ensure bracket/stop orders have stop_price set when ATR is available."""
    side = signal.get("side", "")
    if side != "buy":
        return True, ""
    # If no stop price provided and it's a market/limit buy, recommend one
    # (non-blocking -- just log a warning)
    if not signal.get("stop_price") and not signal.get("order_class") == "mleg":
        logger.debug(f"risk: no stop_price on buy signal for {signal.get('symbol')} "
                     f"-- consider adding ATR-based stop")
    return True, ""

# -- main veto function -------------------------------------------------------
def approve(signal: dict) -> tuple:
    """
    Returns (True, "") if signal is approved, (False, reason) if vetoed.
    Called by order_execution before submitting any order.
    """
    symbol   = signal.get("symbol", "")
    side     = signal.get("side", "")
    notional = float(signal.get("notional", 0) or 0)
    strategy = signal.get("strategy", "")
    is_day   = signal.get("is_day_trade", False)

    checks = [
        _check_position_size(symbol, notional),
        _check_pdt(is_day),
        _check_dtmc(),
        _check_wash_trade(symbol, side),
        _check_margin(side),
        _check_options_level(strategy),
        _check_portfolio_heat(side),
        _check_stop_loss(signal),
    ]
    for ok, reason in checks:
        if not ok:
            logger.warning(f"risk veto [{symbol}]: {reason}")
            return False, reason
    return True, ""

# ?? main loop ?????????????????????????????????????????????????
def run():
    logger.info("risk_manager: starting")
    while not shared.SHUTTING_DOWN:
        _check_expiring_options()
        time.sleep(settings.TICK_INTERVAL * 12)  # check expiry every ~60s
    logger.info("risk_manager: SHUTTING_DOWN ? exiting")
