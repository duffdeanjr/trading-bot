import logging
import uuid
from types import SimpleNamespace
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from config import settings

logger = logging.getLogger(__name__)

_client = TradingClient(
    api_key    = settings.APCA_KEY,
    secret_key = settings.APCA_SECRET,
    paper      = settings.IS_PAPER,
    url_override = settings.BASE_URL,
)

# -- named API helpers ---------------------------------------------------------

def get_open_orders():
    return _client.get_orders(GetOrdersRequest(status="open"))

def cancel_all_orders():
    return _client.cancel_orders()

def get_account():
    return _client.get_account()

def get_positions():
    return _client.get_all_positions()

def get_portfolio_history(**kwargs):
    return _client.get_portfolio_history(**kwargs)

def get_activities(**kwargs):
    try:
        return _client.get_account_activities()
    except Exception as e:
        logger.debug(f"get_activities unavailable: {e}")
        return []

def get_clock():
    return _client.get_clock()

def get_calendar(**kwargs):
    from alpaca.trading.requests import GetCalendarRequest
    filters = GetCalendarRequest(**kwargs) if kwargs else None
    return _client.get_calendar(filters)

def get_assets(**kwargs):
    return _client.get_all_assets(**kwargs)

def get_watchlists():
    return _client.get_watchlists()

def get_corporate_actions(**kwargs):
    from alpaca.trading.requests import GetCorporateAnnouncementsRequest
    req = GetCorporateAnnouncementsRequest(**kwargs)
    return _client.get_corporate_announcements(req)

def get_options_contracts(**kwargs):
    from alpaca.trading.requests import GetOptionContractsRequest
    req = GetOptionContractsRequest(**kwargs)
    resp = _client.get_option_contracts(req)
    if hasattr(resp, 'option_contracts'):
        return resp.option_contracts or []
    return resp

# -- order builders ------------------------------------------------------------

def _is_crypto(symbol: str) -> bool:
    return "/" in symbol

def build_market_order(symbol, qty, side, time_in_force=None, **kwargs):
    if time_in_force is None:
        time_in_force = TimeInForce.GTC if _is_crypto(symbol) else TimeInForce.DAY
    return MarketOrderRequest(
        symbol=symbol, qty=qty, side=side,
        time_in_force=time_in_force, **kwargs
    )

def build_limit_order(symbol, qty, side, limit_price, time_in_force=None, **kwargs):
    if time_in_force is None:
        time_in_force = TimeInForce.GTC if _is_crypto(symbol) else TimeInForce.DAY
    return LimitOrderRequest(
        symbol=symbol, qty=qty, side=side,
        limit_price=limit_price, time_in_force=time_in_force, **kwargs
    )

def build_bracket_order(symbol, qty, side, limit_price, take_profit_price, stop_loss_price, **kwargs):
    return LimitOrderRequest(
        symbol=symbol, qty=qty, side=side,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        take_profit={"limit_price": take_profit_price},
        stop_loss={"stop_price": stop_loss_price},
        time_in_force=TimeInForce.DAY,
        **kwargs
    )

def build_fractional_order(symbol, notional, side, **kwargs):
    tif = TimeInForce.GTC if _is_crypto(symbol) else TimeInForce.DAY
    return MarketOrderRequest(
        symbol=symbol, notional=notional, side=side,
        time_in_force=tif, **kwargs
    )

def build_mleg_order(legs, qty, limit_price=None, time_in_force=TimeInForce.DAY):
    if not settings.OPTIONS_ENABLED:
        raise ValueError("OPTIONS_ENABLED=False — options orders are disabled")
    if settings.OPTIONS_LEVEL < 3:
        raise ValueError(f"Multi-leg orders require OPTIONS_LEVEL=3, current={settings.OPTIONS_LEVEL}")
    if settings.VWAP_TWAP:
        raise ValueError("VWAP_TWAP orders not supported — requires Alpaca Elite Smart Router")
    order = {
        "order_class": "mleg",
        "qty": str(qty),
        "time_in_force": time_in_force,
        "legs": legs,
    }
    if limit_price is not None:
        order["type"] = "limit"
        order["limit_price"] = str(limit_price)
    else:
        order["type"] = "market"
    return order

def build_options_close_order(symbol, qty, side, position_intent="buy_to_close",
                              time_in_force=TimeInForce.DAY):
    return {
        "symbol": symbol,
        "qty": str(int(qty)),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
        "position_intent": position_intent,
    }

def submit_order(order_request):
    """Submit any order request to Alpaca. Respects DRY_RUN mode."""
    if settings.DRY_RUN:
        logger.info(f"DRY_RUN: would submit order: {order_request}")
        return _mock_order(order_request)
    if isinstance(order_request, dict):
        import requests as _req
        resp = _req.post(
            f"{settings.BASE_URL}/v2/orders",
            json=order_request,
            headers={
                "APCA-API-KEY-ID": settings.APCA_KEY,
                "APCA-API-SECRET-KEY": settings.APCA_SECRET,
            },
            timeout=10,
        )
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code} {resp.text}")
        return resp.json()
    return _client.submit_order(order_request)

def _mock_order(req):
    return SimpleNamespace(
        id=f"dry-{uuid.uuid4().hex[:8]}",
        client_order_id=getattr(req, "client_order_id", None) or (req.get("client_order_id") if isinstance(req, dict) else "dry-run"),
        status="accepted",
        symbol=getattr(req, "symbol", None) or (req.get("symbol") if isinstance(req, dict) else "???"),
        qty=getattr(req, "qty", None) or (req.get("qty") if isinstance(req, dict) else 0),
        side=getattr(req, "side", None) or (req.get("side") if isinstance(req, dict) else "buy"),
        filled_avg_price=None,
        filled_qty=0,
        order_type="market",
    )
