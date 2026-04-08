import logging
import sys as _sys
import importlib as _importlib

def _sdk_import(dotted_name, names):
    _saved = {k: v for k, v in _sys.modules.items() if k == 'alpaca' or k.startswith('alpaca.')}
    for k in list(_saved): del _sys.modules[k]
    try:
        mod = _importlib.import_module(dotted_name)
        return {n: getattr(mod, n) for n in names}
    finally:
        _sys.modules.update(_saved)

_r = _sdk_import('alpaca.trading.client', ['TradingClient'])
TradingClient = _r['TradingClient']

_r2 = _sdk_import('alpaca.trading.requests', [
    'MarketOrderRequest','LimitOrderRequest','StopOrderRequest',
    'TrailingStopOrderRequest','GetOrdersRequest',
])
MarketOrderRequest     = _r2['MarketOrderRequest']
LimitOrderRequest      = _r2['LimitOrderRequest']
StopOrderRequest       = _r2['StopOrderRequest']
TrailingStopOrderRequest = _r2['TrailingStopOrderRequest']
GetOrdersRequest       = _r2['GetOrdersRequest']

_r3 = _sdk_import('alpaca.trading.enums', ['OrderSide','TimeInForce','OrderClass','OrderType'])
OrderSide   = _r3['OrderSide']
TimeInForce = _r3['TimeInForce']
OrderClass  = _r3['OrderClass']
OrderType   = _r3['OrderType']

from config import settings

logger = logging.getLogger(__name__)

# ?? single client instance ????????????????????????????????????
_client = TradingClient(
    api_key    = settings.APCA_KEY,
    secret_key = settings.APCA_SECRET,
    paper      = settings.IS_PAPER,
    url_override = settings.BASE_URL,
)

def get_client() -> TradingClient:
    return _client

# ?? named API helpers (diagnostic improvement) ????????????????

def get_open_orders():
    """Fetch all currently open orders. Used at startup to pre-populate pending_orders."""
    return _client.get_orders(GetOrdersRequest(status="open"))

def cancel_all_orders():
    """Cancel all open orders. Used during graceful shutdown."""
    return _client.cancel_orders()

def get_account():
    return _client.get_account()

def get_positions():
    return _client.get_all_positions()

def get_portfolio_history(**kwargs):
    return _client.get_portfolio_history(**kwargs)

def get_activities(**kwargs):
    try:
        from alpaca.trading.requests import GetAccountActivitiesRequest
        req = GetAccountActivitiesRequest(**kwargs) if kwargs else None
        if req:
            return _client.get_account_activities(req)
        return _client.get_account_activities()
    except Exception as e:
        logger.warning(f"get_activities failed: {e}")
        return []

def get_clock():
    return _client.get_clock()

def get_calendar(**kwargs):
    return _client.get_calendar(**kwargs)

def get_assets(**kwargs):
    return _client.get_all_assets(**kwargs)

def get_watchlists():
    return _client.get_all_watchlists()

def get_corporate_actions(**kwargs):
    return _client.get_corporate_announcements(**kwargs)

def get_options_contracts(**kwargs):
    return _client.get_option_contracts(**kwargs)

# ?? order builders ????????????????????????????????????????????

def build_market_order(symbol, qty, side, time_in_force=TimeInForce.DAY, **kwargs):
    return MarketOrderRequest(
        symbol=symbol, qty=qty, side=side,
        time_in_force=time_in_force, **kwargs
    )

def build_limit_order(symbol, qty, side, limit_price, time_in_force=TimeInForce.DAY, **kwargs):
    return LimitOrderRequest(
        symbol=symbol, qty=qty, side=side,
        limit_price=limit_price, time_in_force=time_in_force, **kwargs
    )

def build_stop_order(symbol, qty, side, stop_price, time_in_force=TimeInForce.DAY, **kwargs):
    return StopOrderRequest(
        symbol=symbol, qty=qty, side=side,
        stop_price=stop_price, time_in_force=time_in_force, **kwargs
    )

def build_trailing_stop_order(symbol, qty, side, trail_percent=None, trail_price=None, **kwargs):
    return TrailingStopOrderRequest(
        symbol=symbol, qty=qty, side=side,
        trail_percent=trail_percent, trail_price=trail_price, **kwargs
    )

def build_bracket_order(symbol, qty, side, limit_price, take_profit_price, stop_loss_price, **kwargs):
    """Bracket order ? limit entry with take-profit and stop-loss attached."""
    return LimitOrderRequest(
        symbol=symbol, qty=qty, side=side,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        take_profit={"limit_price": take_profit_price},
        stop_loss={"stop_price": stop_loss_price},
        time_in_force=TimeInForce.DAY,
        **kwargs
    )

def build_oco_order(symbol, qty, side, take_profit_price, stop_loss_price, **kwargs):
    """OCO ? one-cancels-other for closing an existing position."""
    return LimitOrderRequest(
        symbol=symbol, qty=qty, side=side,
        limit_price=take_profit_price,
        order_class=OrderClass.OCO,
        take_profit={"limit_price": take_profit_price},
        stop_loss={"stop_price": stop_loss_price},
        time_in_force=TimeInForce.DAY,
        **kwargs
    )

def build_fractional_order(symbol, notional, side, **kwargs):
    """Notional/fractional order ? buy $X worth of a symbol."""
    return MarketOrderRequest(
        symbol=symbol, notional=notional, side=side,
        time_in_force=TimeInForce.DAY, **kwargs
    )

def build_mleg_order(legs, qty, limit_price, time_in_force=TimeInForce.DAY):
    """Multi-leg options order (Level 3). Each leg: {symbol, side, ratio_qty, position_intent}."""
    if not settings.OPTIONS_ENABLED:
        raise ValueError("OPTIONS_ENABLED=False ? options orders are disabled")
    if settings.OPTIONS_LEVEL < 3:
        raise ValueError(f"Multi-leg orders require OPTIONS_LEVEL=3, current={settings.OPTIONS_LEVEL}")
    if settings.VWAP_TWAP:
        raise ValueError("VWAP_TWAP orders not supported ? requires Alpaca Elite Smart Router")
    return {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": time_in_force,
        "legs": legs,
    }

def submit_order(order_request):
    """Submit any order request to Alpaca."""
    return _client.submit_order(order_request)
