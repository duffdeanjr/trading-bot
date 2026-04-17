import time
import logging
import threading
import shared
from config import settings
from alpaca_local import client as alpaca, stream as alpaca_stream
from agents import risk_manager
from storage import database

logger = logging.getLogger(__name__)

# ?? pending orders guard (diagnostic: key includes strategy tag) ??
# Key: (symbol, side, strategy_tag) ? allows intentional multi-strategy coexistence
_pending: set = set()
_pending_lock = threading.Lock()  # explicit lock since fill callback runs on different thread

def _pending_key(symbol, side, strategy_tag):
    return (symbol, side, strategy_tag or "default")

def _add_pending(symbol, side, strategy_tag):
    with _pending_lock:
        _pending.add(_pending_key(symbol, side, strategy_tag))

def _remove_pending(symbol, side, strategy_tag):
    with _pending_lock:
        _pending.discard(_pending_key(symbol, side, strategy_tag))

def _is_pending(symbol, side, strategy_tag) -> bool:
    with _pending_lock:
        return _pending_key(symbol, side, strategy_tag) in _pending

# ?? fill callback (registered with TradingStream) ?????????????
def on_fill(event):
    """
    Called by stream.py when a fill event arrives on TradingStream.
    Only manages the pending-orders set here. DB writes, positions updates,
    and outcome tracking are handled by account_agent._on_fill to avoid
    duplicate writes.
    """
    try:
        order = event.order
        symbol       = order.symbol
        side         = str(order.side)
        qty          = float(order.filled_qty or 0)
        price        = float(order.filled_avg_price or 0)
        coid         = order.client_order_id
        strategy_tag = shared.extract_strategy_tag(coid)

        # Remove from pending set so new orders for this symbol can flow
        _remove_pending(symbol, side, strategy_tag)

        logger.info(f"fill: {side} {qty} {symbol} @ {price:.4f}")

    except Exception as e:
        logger.error(f"order_exec fill callback error: {e}")

# ?? order submission ???????????????????????????????????????????
def _submit_with_retry(order_request, symbol, side, strategy_tag):
    """Submit order with exponential backoff retry on transient errors."""
    for attempt in range(settings.MAX_RETRIES):
        try:
            result = alpaca.submit_order(order_request)
            logger.info(f"order submitted: {side} {symbol} attempt={attempt+1}")
            return result
        except Exception as e:
            err_str = str(e)
            if "422" in err_str:
                # Validation error ? do not retry
                logger.error(f"order 422 [{symbol}]: {err_str}")
                _remove_pending(symbol, side, strategy_tag)
                return None
            if attempt < settings.MAX_RETRIES - 1:
                sleep_s = settings.BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"order error [{symbol}], retry in {sleep_s}s: {err_str}")
                time.sleep(sleep_s)
            else:
                logger.error(f"order failed after {settings.MAX_RETRIES} attempts [{symbol}]: {err_str}")
                _remove_pending(symbol, side, strategy_tag)
    return None

def place_order(signal: dict):
    """
    Main entry point called by boss/strategies.
    Runs risk check ? duplicate check ? build order ? submit.
    """
    if shared.SHUTTING_DOWN or shared.RATE_LIMITED:
        return

    symbol       = signal.get("symbol", "")
    side         = signal.get("side", "")
    strategy_tag = signal.get("strategy", "default")

    # Risk manager veto
    ok, reason = risk_manager.approve(signal)
    if not ok:
        logger.debug(f"order rejected by risk: {reason}")
        return

    # Duplicate guard
    if _is_pending(symbol, side, strategy_tag):
        logger.debug(f"duplicate order suppressed: {symbol} {side} [{strategy_tag}]")
        return
    _add_pending(symbol, side, strategy_tag)

    # Build client_order_id: "strategy_tag::timestamp::random" (unique per order)
    import random
    coid = f"{strategy_tag}::{int(time.time())}::{random.randint(1000,9999)}"

    # Build order request
    try:
        notional = float(signal.get("notional", 0) or 0)
        qty      = float(signal.get("qty", 0) or 0)
        limit_px = signal.get("limit_price")
        stop_px  = signal.get("stop_price")
        tp_px    = signal.get("take_profit_price")

        if signal.get("order_class") == "mleg":
            order_req = alpaca.build_mleg_order(
                legs=signal["legs"],
                qty=qty,
                limit_price=limit_px,
            )
        elif tp_px and stop_px:
            order_req = alpaca.build_bracket_order(
                symbol=symbol, qty=qty, side=side,
                limit_price=limit_px,
                take_profit_price=tp_px,
                stop_loss_price=stop_px,
                client_order_id=coid,
            )
        elif notional and not qty:
            order_req = alpaca.build_fractional_order(
                symbol=symbol, notional=notional, side=side,
                client_order_id=coid,
            )
        elif limit_px:
            order_req = alpaca.build_limit_order(
                symbol=symbol, qty=qty, side=side,
                limit_price=limit_px,
                client_order_id=coid,
            )
        else:
            order_req = alpaca.build_market_order(
                symbol=symbol, qty=qty, side=side,
                client_order_id=coid,
            )
    except Exception as e:
        logger.error(f"order build error [{symbol}]: {e}")
        _remove_pending(symbol, side, strategy_tag)
        return

    _submit_with_retry(order_req, symbol, side, strategy_tag)

# ?? startup reconciliation ????????????????????????????????????
def _load_open_orders():
    """Pre-populate pending_orders from Alpaca's live open order list."""
    try:
        orders = alpaca.get_open_orders()
        for o in orders:
            coid = o.client_order_id or ""
            tag  = shared.extract_strategy_tag(coid) or "default"
            _add_pending(o.symbol, str(o.side), tag)
        logger.info(f"order_exec: pre-populated {len(orders)} open orders into pending set")
    except Exception as e:
        logger.error(f"order_exec: failed to load open orders at startup: {e}")

# ?? graceful shutdown ?????????????????????????????????????????
def _cancel_all_on_shutdown():
    """Cancel all open orders during shutdown with CANCEL_TIMEOUT deadline."""
    logger.info("order_exec: cancelling all open orders on shutdown")
    try:
        alpaca.cancel_all_orders()
        logger.info("order_exec: cancel-all sent")
    except Exception as e:
        logger.error(f"order_exec: cancel-all failed: {e}")

    # Wait up to CANCEL_TIMEOUT for pending set to drain via fill callbacks
    deadline = time.time() + settings.CANCEL_TIMEOUT
    while time.time() < deadline:
        with _pending_lock:
            remaining = len(_pending)
        if remaining == 0:
            break
        time.sleep(0.5)

    with _pending_lock:
        remaining = len(_pending)
    if remaining > 0:
        logger.warning(f"order_exec: {remaining} orders still open after timeout ? logged to DB")
        # Log remaining open orders to database
        for key in list(_pending):
            database.write_agent_log(
                ts=time.time(), agent="order_execution",
                level="WARNING",
                message=f"uncancelled order at shutdown: {key}"
            )

# -- main loop ----------------------------------------------------------------
@shared.register_agent("order_execution", phase=7)
def run():
    logger.info("order_exec: starting")

    alpaca_stream.register_callback("trade", on_fill)
    _load_open_orders()

    while not shared.SHUTTING_DOWN:
        shared.heartbeat("order_execution")
        try:
            if shared.MARKET_OPEN and not shared.RATE_LIMITED:
                _execute_toward_targets()
                _execute_auto_rolls()
                _update_trailing_stops()
        except Exception as e:
            logger.error(f"order_exec: target execution error: {e}")
        time.sleep(settings.TICK_INTERVAL)

    _cancel_all_on_shutdown()
    logger.info("order_exec: SHUTTING_DOWN - exiting")


def _execute_auto_rolls():
    """Process auto-roll close signals from risk_manager for near-expiry options."""
    with shared.cache_lock:
        signals = getattr(shared, "auto_roll_signals", [])
        shared.auto_roll_signals = []

    for sig in signals:
        symbol = sig.get("symbol", "")
        side = sig.get("side", "buy")
        qty = sig.get("qty", 0)
        if qty <= 0:
            continue

        logger.info(f"order_exec: auto-roll close {side} {qty} {symbol}")
        place_order({
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "strategy": "auto_roll_close",
        })


def _execute_toward_targets():
    """
    Read the investment plan targets and trade toward them.
    For each symbol where current allocation differs from target by > threshold,
    emit an order to close the gap.
    """
    from agents import plan_manager, risk_manager

    # Check if heat allows new positions
    can_trade, reason = risk_manager.can_add_position()
    if not can_trade:
        logger.debug(f"order_exec: skipping target execution: {reason}")
        return

    size_mult = risk_manager.get_size_multiplier()
    if size_mult <= 0:
        return

    targets = plan_manager.get_targets()  # {symbol: target_pct}
    if not targets:
        return

    acct = shared.get_account_snapshot()
    positions = shared.get_positions_snapshot()

    if acct is None or (isinstance(acct, dict) and not acct):
        return

    try:
        equity = float(getattr(acct, "equity", 0) or 0)
    except Exception:
        return

    if equity <= 0:
        return

    # Compute current allocation per symbol
    current_alloc = {}
    for sym, pos in positions.items():
        try:
            mv = float(getattr(pos, "market_value", 0) or 0)
            current_alloc[sym] = mv / equity
        except Exception:
            current_alloc[sym] = 0.0

    # Track cumulative buys this cycle to avoid blowing through heat limit
    buy_budget = max(0, (settings.HEAT_MAX - sum(current_alloc.values())) * equity)
    bought_this_cycle = 0.0

    for symbol, target_pct in targets.items():
        current_pct = current_alloc.get(symbol, 0.0)
        gap = target_pct - current_pct

        if abs(gap) < settings.REBALANCE_THRESHOLD:
            continue

        side = "buy" if gap > 0 else "sell"

        # Cap buys to remaining heat budget
        if side == "buy" and bought_this_cycle >= buy_budget:
            logger.debug(f"order_exec: skipping {symbol} buy — heat budget exhausted "
                         f"(${bought_this_cycle:,.0f} / ${buy_budget:,.0f})")
            continue

        # Compute notional to trade
        notional = abs(gap) * equity * size_mult
        notional = min(notional, settings.MAX_POSITION_SIZE)

        if side == "buy":
            notional = min(notional, buy_budget - bought_this_cycle)

        if notional < 1.0:
            continue

        # Check not already pending
        if _is_pending(symbol, side, "target_rebalance"):
            continue

        # Compute ATR-based stop-loss and take-profit for buy orders
        stop_px = None
        tp_px = None
        order_qty = None
        if side == "buy":
            try:
                ohlcv = shared.build_ohlcv(symbol)
                from agents import indicators
                ind = indicators.compute_all(ohlcv)
                atr_val = ind.get("atr")
                hist_closes = ohlcv.get("closes", [])
                ref_price = hist_closes[-1] if hist_closes else 0
                if atr_val and atr_val > 0 and ref_price > 0:
                    stop_px = round(ref_price - (atr_val * settings.ATR_STOP_MULT), 2)
                    tp_px = round(ref_price + (atr_val * settings.ATR_TP_MULT), 2)
                    order_qty = max(1, int(notional / ref_price))
            except Exception as e:
                logger.debug(f"order_exec: ATR bracket calc error for {symbol}: {e}")

        signal = {
            "symbol":   symbol,
            "side":     side,
            "notional": round(notional, 2),
            "strategy": "target_rebalance",
        }
        # Add bracket params if computed (only for equities with integer qty)
        if stop_px and tp_px and order_qty and "/" not in symbol:
            signal["stop_price"] = stop_px
            signal["take_profit_price"] = tp_px
            signal["qty"] = order_qty
            signal.pop("notional", None)  # bracket orders use qty, not notional

        logger.info(f"order_exec: rebalance {side} {symbol} "
                    f"gap={gap:+.1%} notional=${notional:.0f}"
                    f"{f' (budget ${buy_budget-bought_this_cycle:,.0f} left)' if side=='buy' else ''}")
        place_order(signal)

        if side == "buy":
            bought_this_cycle += notional


# ── trailing stop-loss management ────────────────────────────────
# Tracks high-water mark for each position and adjusts stops upward.
_trailing_stops: dict = {}  # symbol -> {"high_water": float, "stop_price": float}
_trail_lock = threading.Lock()
_TRAIL_CHECK_INTERVAL = 30  # seconds
_last_trail_check = 0.0


def _update_trailing_stops():
    """
    True trailing stop: as price moves up, stop ratchets higher.
    Stop never moves down — only up. Uses TRAIL_ATR_MULT for distance.
    Emits a sell order when current price breaches the trailing stop.
    """
    global _last_trail_check
    now = time.time()
    if now - _last_trail_check < _TRAIL_CHECK_INTERVAL:
        return
    _last_trail_check = now

    if not settings.TRAILING_STOP:
        return

    positions = shared.get_positions_snapshot()
    if not positions:
        return

    from agents import indicators

    for symbol, pos in positions.items():
        # Only trail long equity positions (not options, not crypto)
        asset_class = getattr(pos, "asset_class", "us_equity")
        if asset_class != "us_equity":
            continue
        qty = float(getattr(pos, "qty", 0) or 0)
        if qty <= 0:
            continue  # only trail longs

        try:
            current_price = float(getattr(pos, "current_price", 0) or 0)
        except Exception:
            continue
        if current_price <= 0:
            continue

        # Compute ATR for this symbol
        ohlcv = shared.build_ohlcv(symbol)
        ind = indicators.compute_all(ohlcv)
        atr_val = ind.get("atr")
        if not atr_val or atr_val <= 0:
            continue

        trail_distance = atr_val * settings.TRAIL_ATR_MULT

        with _trail_lock:
            if symbol not in _trailing_stops:
                # Initialize: high water = current price, stop = price - trail
                _trailing_stops[symbol] = {
                    "high_water": current_price,
                    "stop_price": round(current_price - trail_distance, 2),
                }
                continue

            entry = _trailing_stops[symbol]

            # Update high-water mark
            if current_price > entry["high_water"]:
                entry["high_water"] = current_price
                new_stop = round(current_price - trail_distance, 2)
                # Stop only ratchets UP, never down
                if new_stop > entry["stop_price"]:
                    old_stop = entry["stop_price"]
                    entry["stop_price"] = new_stop
                    logger.debug(
                        f"order_exec: trailing stop {symbol} ratcheted "
                        f"${old_stop:.2f} -> ${new_stop:.2f} "
                        f"(price=${current_price:.2f}, ATR={atr_val:.2f})"
                    )

            # Check if current price has breached the trailing stop
            if current_price <= entry["stop_price"]:
                logger.warning(
                    f"order_exec: TRAILING STOP HIT {symbol} @ ${current_price:.2f} "
                    f"(stop=${entry['stop_price']:.2f}, high=${entry['high_water']:.2f})"
                )
                # Emit sell order
                if not _is_pending(symbol, "sell", "trailing_stop"):
                    place_order({
                        "symbol": symbol,
                        "side": "sell",
                        "qty": qty,
                        "strategy": "trailing_stop",
                    })
                    # Remove from tracking after sell issued
                    del _trailing_stops[symbol]

    # Clean up: remove symbols no longer in positions
    with _trail_lock:
        tracked = set(_trailing_stops.keys())
        held = set(positions.keys())
        for sym in tracked - held:
            del _trailing_stops[sym]
