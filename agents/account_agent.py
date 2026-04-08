import time
import logging
import datetime
import shared
from config import settings
from alpaca_local import client as alpaca, stream as alpaca_stream
from storage import database

logger = logging.getLogger(__name__)

def _extract_strategy_tag(client_order_id):
    """Extract strategy tag from client_order_id format 'strategy_tag::timestamp'."""
    if client_order_id and "::" in str(client_order_id):
        return str(client_order_id).split("::")[0]
    return "unknown"

def _on_fill(event):
    """
    Fill callback from TradingStream.
    Writes to DB immediately, tracks outcomes for feedback loop,
    then updates shared positions.
    """
    try:
        order = event.order
        symbol = order.symbol
        side   = str(order.side)
        qty    = float(order.filled_qty or 0)
        price  = float(order.filled_avg_price or 0)
        strategy = _extract_strategy_tag(order.client_order_id)
        now_iso  = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Immediate DB write
        database.write_trade(
            ts=time.time(), symbol=symbol, side=side,
            qty=qty, price=price, notional=qty * price,
            order_type=str(order.order_type),
            client_order_id=order.client_order_id,
            strategy_tag=strategy,
        )

        # Outcome tracking: determine if this is an entry or exit
        with shared.positions_lock:
            existing_qty = 0
            pos = shared.positions.get(symbol)
            if pos is not None:
                existing_qty = float(getattr(pos, "qty", 0) or
                                     (pos.get("qty", 0) if isinstance(pos, dict) else 0))

        if side == "buy" and existing_qty <= 0:
            # New long entry
            database.open_outcome(symbol, strategy, "buy", price, now_iso, qty)
        elif side == "sell" and existing_qty <= qty:
            # Closing a long position (full or partial exit)
            database.close_outcome(symbol, strategy, price, now_iso)
        elif side == "sell" and existing_qty <= 0:
            # New short entry
            database.open_outcome(symbol, strategy, "sell", price, now_iso, qty)
        elif side == "buy" and existing_qty < 0:
            # Closing a short position
            database.close_outcome(symbol, strategy, price, now_iso)

        # Optimistic position update
        with shared.positions_lock:
            pos = shared.positions.get(symbol, {})
            eq = float(pos.get("qty", 0)) if isinstance(pos, dict) else float(getattr(pos, "qty", 0) or 0)
            if side == "buy":
                shared.positions[symbol] = {"qty": eq + qty, "avg_price": price}
            else:
                shared.positions[symbol] = {"qty": max(0, eq - qty), "avg_price": price}

    except Exception as e:
        logger.error(f"account_agent fill callback error: {e}")

def _poll_account():
    if shared.RATE_LIMITED:
        return
    try:
        acct = alpaca.get_account()
        with shared.account_lock:
            shared.account = acct
    except Exception as e:
        logger.error(f"account_agent: GET /account failed: {e}")

def _poll_positions():
    if shared.RATE_LIMITED:
        return
    try:
        pos_list = alpaca.get_positions()
        with shared.positions_lock:
            shared.positions = {p.symbol: p for p in pos_list}
    except Exception as e:
        logger.error(f"account_agent: GET /positions failed: {e}")

def _poll_portfolio_history():
    if shared.RATE_LIMITED:
        return
    try:
        hist = alpaca.get_portfolio_history()
        with shared.account_lock:
            shared.portfolio_history = hist
    except Exception as e:
        logger.error(f"account_agent: GET /portfolio/history failed: {e}")

def _poll_activities():
    """
    Poll GET /activities for options NTA events (exercise, assignment, expiry).
    Also detects corporate action events and writes dirty_symbols for ref library.
    """
    if shared.RATE_LIMITED:
        return
    try:
        activities = alpaca.get_activities()
        corp_action_types = {"DIV", "DIVCGL", "DIVCGS", "DIVNRA", "DIVTXEX", "SPLIT", "MERGER"}
        nta_types = {"OPASN", "OPTRD", "OPXRC", "OPEXP"}

        for act in activities:
            act_type = getattr(act, "activity_type", "")

            # Options NTA events
            if act_type in nta_types:
                symbol = getattr(act, "symbol", "")
                logger.info(f"account_agent: options NTA {act_type} {symbol}")

            # Corporate action ? flag symbol for cache invalidation (diagnostic fix)
            if act_type in corp_action_types:
                symbol = getattr(act, "symbol", "")
                if symbol:
                    with shared.cache_lock:
                        shared.dirty_symbols.add(symbol)
                    logger.info(f"account_agent: corp action {act_type} {symbol} ? flagged as dirty")

    except Exception as e:
        logger.error(f"account_agent: GET /activities failed: {e}")

def _has_open_crypto_positions() -> bool:
    with shared.positions_lock:
        for pos in shared.positions.values():
            ac = getattr(pos, "asset_class", "")
            if ac == "crypto":
                return True
    return False

def run():
    logger.info("account_agent: starting")

    # Register fill callback
    alpaca_stream.register_callback("trade", _on_fill)

    # Initial poll ? signal readiness after first successful cycle
    _poll_account()
    _poll_positions()
    _poll_portfolio_history()
    shared.account_ready_event.set()
    logger.info("account_agent: account_ready_event set")

    while not shared.SHUTTING_DOWN:
        _poll_account()
        _poll_positions()
        _poll_portfolio_history()
        _poll_activities()

        # Snapshot positions to DB periodically
        ts = time.time()
        with shared.positions_lock:
            for sym, pos in shared.positions.items():
                database.write_position_snapshot(
                    ts=ts,
                    symbol=sym,
                    qty=float(getattr(pos, "qty", 0) or 0),
                    avg_cost=float(getattr(pos, "avg_entry_price", 0) or 0),
                    market_val=float(getattr(pos, "market_value", 0) or 0),
                    unrealised=float(getattr(pos, "unrealized_pl", 0) or 0),
                    asset_class=getattr(pos, "asset_class", None),
                )

        # Sleep: short if market open OR if crypto positions open overnight
        if shared.MARKET_OPEN or shared.EXTENDED_HOURS or _has_open_crypto_positions():
            time.sleep(settings.TICK_INTERVAL)
        else:
            time.sleep(settings.OVERNIGHT_SLEEP)

    logger.info("account_agent: SHUTTING_DOWN ? exiting")
