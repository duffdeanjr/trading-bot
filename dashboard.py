import os
import json
import logging
import sqlite3
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from config import settings

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(_BASE, "dashboard.html")

# Try to import shared state (only works when bot is running)
try:
    import shared
    HAS_BOT = True
except ImportError:
    HAS_BOT = False

def _get_market_open():
    try:
        from alpaca_local import client as alpaca
        clock = alpaca.get_clock()
        return clock.is_open
    except Exception:
        return getattr(shared, "MARKET_OPEN", False) if HAS_BOT else False

def _get_stream_health():
    """Read stream health from file written by diagnostics agent (cross-process safe)."""
    now = time.time()
    names = ["trade", "stock", "crypto", "option", "news"]
    health_path = os.path.join(_BASE, ".stream_health.json")
    try:
        with open(health_path, "r") as f:
            data = json.load(f)
        result = {}
        for name in names:
            info = data.get(name, {})
            ts = info.get("last_heartbeat", 0)
            age = round(now - ts, 1) if ts > 0 else None
            connected = info.get("connected", False)
            # Derive status from data if diagnostics hasn't written it yet
            if "status" in info:
                status = info["status"]
            elif name == "option":
                status = "disabled"  # we don't subscribe to options stream
            elif name == "trade" and ts > 0:
                status = "active" if (now - ts) < 600 else "idle"
            elif connected:
                status = "active"
            else:
                status = "waiting"
            # Trade stream: consider connected if it ever received data (fills are infrequent)
            if name == "trade" and ts > 0:
                connected = True
            # Options stream: always show as OK (intentionally disabled)
            if name == "option":
                connected = True
            result[name] = {
                "last_msg_age": age,
                "reconnects": info.get("reconnects", 0),
                "connected": connected,
                "status": status,
            }
        return result
    except Exception:
        return {name: {"last_msg_age": None, "reconnects": 0, "connected": False, "status": "unknown"}
                for name in names}

def _db_path():
    p = settings.DB_PATH
    if not os.path.isabs(p):
        return os.path.join(_BASE, p)
    return p

def query(sql, params=()):
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def query_one(sql, params=()):
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()

_acct_cache = {"data": {}, "ts": 0}

def _fetch_alpaca_account():
    """Single Alpaca account call, cached 10s. Returns full account dict."""
    now = time.time()
    if now - _acct_cache["ts"] < 10 and _acct_cache["data"]:
        return _acct_cache["data"]
    try:
        import requests as _req
        headers = {'APCA-API-KEY-ID': settings.APCA_KEY, 'APCA-API-SECRET-KEY': settings.APCA_SECRET}
        r = _req.get(f'{settings.BASE_URL}/v2/account', headers=headers, timeout=5)
        if r.status_code == 200:
            _acct_cache["data"] = r.json()
            _acct_cache["ts"] = now
    except Exception:
        pass
    return _acct_cache["data"]


def _get_agent_status():
    """Build agent health status from shared state."""
    if not HAS_BOT:
        return []
    try:
        with shared.errors_lock:
            errors_snap = dict(shared.AGENT_ERRORS)
    except Exception:
        errors_snap = {}
    agent_names = ["ref_library", "account_agent", "signal_generator",
                   "plan_manager", "order_execution", "risk_manager",
                   "boss", "diagnostics", "screener", "walk_forward",
                   "plan_reviewer", "strategy_factory", "bandit_harvester"]
    now = time.time()
    result = []
    for name in agent_names:
        info = errors_snap.get(name, {})
        try:
            hb_ts = getattr(shared, 'AGENT_HEARTBEATS', {}).get(name, 0)
        except Exception:
            hb_ts = 0
        hb_age = round(now - hb_ts, 1) if hb_ts > 0 else None
        status = "running"
        if info.get("count", 0) > 0:
            status = "error"
        elif hb_ts > 0 and (now - hb_ts) > 120:
            status = "stale"
        result.append({
            "name": name, "status": status,
            "restarts": info.get("count", 0),
            "last_error": info.get("last_error"),
            "heartbeat_age_s": hb_age,
        })
    return result


def _get_heat_and_pause():
    """Return heat status dict and trading_paused flag."""
    if not HAS_BOT:
        return {}, False
    heat_status = {}
    try:
        from agents import risk_manager
        heat_status = risk_manager.get_heat_status()
    except Exception:
        pass
    trading_paused = getattr(shared, "trading_paused", False)
    return heat_status, trading_paused


_ph_cache = {"data": [], "ts": 0}

def _get_portfolio_history():
    """Fetch portfolio equity history. Cached 5 minutes (slow API, rarely changes)."""
    now = time.time()
    if now - _ph_cache["ts"] < 300 and _ph_cache["data"]:
        return _ph_cache["data"]
    try:
        import requests as _req
        _headers = {'APCA-API-KEY-ID': settings.APCA_KEY, 'APCA-API-SECRET-KEY': settings.APCA_SECRET}
        _r = _req.get(f'{settings.BASE_URL}/v2/account/portfolio/history?period=1A&timeframe=1D',
                      headers=_headers, timeout=10)
        if _r.status_code == 200:
            _ph = _r.json()
            _ts = _ph.get('timestamp', [])
            _eq = _ph.get('equity', [])
            _pnl = _ph.get('profit_loss', [])
            _ph_cache["data"] = [
                {"ts": _ts[i], "equity": _eq[i], "pnl": _pnl[i] if i < len(_pnl) else 0}
                for i in range(len(_ts)) if _eq[i] and _eq[i] > 0
            ]
            _ph_cache["ts"] = now
    except Exception:
        pass
    return _ph_cache["data"]


def _get_parameters():
    """Snapshot of all tunable settings for the dashboard."""
    return {
        "max_position_size":   settings.MAX_POSITION_SIZE,
        "max_portfolio_pct":   settings.MAX_PORTFOLIO_PCT * 100,
        "margin_min_equity":   settings.MARGIN_MIN_EQUITY,
        "max_daily_loss_pct":  settings.MAX_DAILY_LOSS_PCT * 100,
        "max_consecutive_losses": settings.MAX_CONSECUTIVE_LOSSES,
        "rsi_oversold":        settings.RSI_OVERSOLD,
        "rsi_overbought":     settings.RSI_OVERBOUGHT,
        "rebalance_threshold": settings.REBALANCE_THRESHOLD * 100,
        "vix_caution":         settings.VIX_CAUTION,
        "vix_high":            settings.VIX_HIGH,
        "vix_extreme":         settings.VIX_EXTREME,
        "heat_warn":           settings.HEAT_WARN * 100,
        "heat_max":            settings.HEAT_MAX * 100,
        "options_enabled":     settings.OPTIONS_ENABLED,
        "options_level":       settings.OPTIONS_LEVEL,
        "screener_enabled":    settings.SCREENER_ENABLED,
        "screener_interval":   settings.SCREENER_INTERVAL,
        "max_watchlist_size":  settings.MAX_WATCHLIST_SIZE,
        "screener_promote":    settings.SCREENER_PROMOTE_THRESHOLD,
        "screener_demote":     settings.SCREENER_DEMOTE_THRESHOLD,
        "dry_run":             settings.DRY_RUN,
        "tick_interval":       settings.TICK_INTERVAL,
        "data_feed":           settings.DATA_FEED,
        "options_daytrade":    settings.OPTIONS_DAYTRADE,
        "options_profit_target": settings.OPTIONS_PROFIT_TARGET * 100,
        "options_stop_loss":   settings.OPTIONS_STOP_LOSS * 100,
        "options_eod_exit_mins": settings.OPTIONS_EOD_EXIT_MINS,
        "options_wing_width":  settings.OPTIONS_WING_WIDTH * 100,
        "options_otm_pct":     settings.OPTIONS_OTM_PCT * 100,
        "daily_target_pct":    settings.DAILY_TARGET_PCT * 100,
        "daily_target_lock":   settings.DAILY_TARGET_LOCK,
    }


def _get_daily_target():
    """Daily P&L progress toward 1% target from cached account data."""
    acct = _acct_cache.get("data", {})
    equity = float(acct.get("equity", 0) or 0)
    last_equity = float(acct.get("last_equity", 0) or 0)
    if last_equity <= 0 or equity <= 0:
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
        "locked": False,  # Can't check bot state from dashboard process
    }


def _get_options_pnl(positions):
    """Separate P&L for options positions only."""
    total_cost = 0
    total_mkt = 0
    total_pnl = 0
    count = 0
    for p in positions:
        sym = p.get("symbol", "")
        if len(sym) <= 10:
            continue
        cost = float(p.get("avg_cost") or 0) * float(p.get("qty") or 0)
        mkt = float(p.get("market_val") or 0)
        pnl = float(p.get("unrealised") or 0)
        total_cost += abs(cost)
        total_mkt += mkt
        total_pnl += pnl
        count += 1
    return {
        "count": count,
        "total_cost": round(total_cost, 2),
        "total_market_value": round(total_mkt, 2),
        "unrealized_pnl": round(total_pnl, 2),
    }


_countdown_cache = {"data": None, "ts": 0}

def _get_countdown():
    """Time until market close and EOD exit. Cached for 30s."""
    now_ts = time.time()
    if now_ts - _countdown_cache["ts"] < 30 and _countdown_cache["data"]:
        # Update mins from cached close_time
        cd = dict(_countdown_cache["data"])
        if cd.get("close_time"):
            import datetime
            close = datetime.datetime.fromisoformat(cd["close_time"])
            now = datetime.datetime.now(close.tzinfo)
            mins = max(0, (close - now).total_seconds() / 60)
            cd["mins_to_close"] = round(mins, 1)
            cd["mins_to_eod_exit"] = round(max(0, mins - settings.OPTIONS_EOD_EXIT_MINS), 1)
        return cd
    try:
        import requests as _req
        headers = {'APCA-API-KEY-ID': settings.APCA_KEY, 'APCA-API-SECRET-KEY': settings.APCA_SECRET}
        r = _req.get(f'{settings.BASE_URL}/v2/clock', headers=headers, timeout=5)
        if r.status_code == 200:
            import datetime
            data = r.json()
            close_str = data.get("next_close", "")
            is_open = data.get("is_open", False)
            if close_str:
                close = datetime.datetime.fromisoformat(close_str)
                now = datetime.datetime.now(close.tzinfo)
                mins = max(0, (close - now).total_seconds() / 60)
                result = {
                    "mins_to_close": round(mins, 1),
                    "mins_to_eod_exit": round(max(0, mins - settings.OPTIONS_EOD_EXIT_MINS), 1),
                    "close_time": close_str,
                    "is_open": is_open,
                }
                _countdown_cache["data"] = result
                _countdown_cache["ts"] = now_ts
                return result
    except Exception:
        pass
    return {"mins_to_close": None, "mins_to_eod_exit": None, "close_time": None, "is_open": False}


def _get_options_tracker(positions):
    """Profit target / stop loss progress for each options position."""
    result = []
    for p in positions:
        sym = p.get("symbol", "")
        if len(sym) <= 10:
            continue
        qty = float(p.get("qty") or 0)
        cost = float(p.get("avg_cost") or 0) * abs(qty)
        mkt = float(p.get("market_val") or 0)
        entry_credit = abs(cost)
        if entry_credit == 0:
            continue
        if qty < 0:
            current_value = abs(mkt)
            profit_pct = 1.0 - (current_value / entry_credit) if entry_credit > 0 else 0
        else:
            profit_pct = (mkt - cost) / abs(cost) if cost != 0 else 0
        result.append({
            "symbol": sym,
            "qty": qty,
            "entry_credit": round(entry_credit, 2),
            "current_value": round(abs(mkt), 2),
            "profit_pct": round(profit_pct * 100, 1),
            "target_pct": settings.OPTIONS_PROFIT_TARGET * 100,
            "stop_pct": settings.OPTIONS_STOP_LOSS * 100,
            "at_target": profit_pct >= settings.OPTIONS_PROFIT_TARGET,
            "at_stop": profit_pct <= -settings.OPTIONS_STOP_LOSS,
        })
    return result


_iv_cache = {"data": [], "ts": 0}

def _get_iv_regime_map():
    """Current IV/IVR for each watchlist symbol. Cached for 60s (chain lookups are slow)."""
    now = time.time()
    if now - _iv_cache["ts"] < 60 and _iv_cache["data"]:
        return _iv_cache["data"]
    result = []
    try:
        wl = list(getattr(shared, 'watchlist', []) or []) if HAS_BOT else []
        # Read from the signal generator's latest IV data (already computed in-process)
        for sym in wl:
            if "/" in sym:
                continue
            # Pull from the signals table instead of recomputing IV (fast DB read)
            row = query_one("""SELECT confidence, sentiment, raw FROM signals
                WHERE symbol=? ORDER BY ts DESC LIMIT 1""", (sym,))
            result.append({
                "symbol": sym,
                "iv": 0,
                "ivr": None,
                "regime": "unknown",
                "strategy": row.get("raw", "").split("strategy': '")[1].split("'")[0] if "strategy'" in (row.get("raw") or "") else "none",
            })
    except Exception:
        pass
    _iv_cache["data"] = result
    _iv_cache["ts"] = now
    return result


_orders_cache = {"data": [], "ts": 0}

def _get_open_orders():
    """Fetch current open orders from Alpaca REST API. Cached 15s."""
    now = time.time()
    if now - _orders_cache["ts"] < 15 and _orders_cache["data"] is not None:
        return _orders_cache["data"]
    try:
        import requests as _req
        headers = {'APCA-API-KEY-ID': settings.APCA_KEY, 'APCA-API-SECRET-KEY': settings.APCA_SECRET}
        r = _req.get(f'{settings.BASE_URL}/v2/orders?status=open', headers=headers, timeout=5)
        if r.status_code == 200:
            _orders_cache["data"] = [{
                "symbol": o.get("symbol") or "mleg",
                "side": o.get("side", ""),
                "qty": str(o.get("qty", "")),
                "type": o.get("order_type", ""),
                "status": o.get("status", ""),
                "submitted_at": o.get("submitted_at"),
                "client_order_id": o.get("client_order_id", ""),
            } for o in r.json()]
            _orders_cache["ts"] = now
            return _orders_cache["data"]
    except Exception:
        pass
    return _orders_cache.get("data", [])




def _get_cash_flow():
    """Premium collected vs paid from recent options trades."""
    rows = query("""
        SELECT side, SUM(notional) as total, COUNT(*) as n
        FROM trades
        WHERE (length(symbol) > 10 OR strategy_tag IN ('iron_condor','covered_call','cash_secured_put','calendar_spread'))
        AND ts > ?
        GROUP BY side
    """, (time.time() - 86400,))
    collected = 0
    paid = 0
    for r in rows:
        side = r.get("side", "")
        total = abs(float(r.get("total") or 0))
        if "sell" in side.lower():
            collected += total
        else:
            paid += total
    return {
        "collected": round(collected, 2),
        "paid": round(paid, 2),
        "net": round(collected - paid, 2),
    }


def _get_execution_quality():
    """Slippage analysis on recent fills."""
    rows = query("""
        SELECT symbol, side, price, notional, strategy_tag, ts
        FROM trades ORDER BY ts DESC LIMIT 50
    """)
    if not rows:
        return {"avg_slippage_pct": 0, "fills": 0}
    fills_with_price = [r for r in rows if float(r.get("price") or 0) > 0]
    return {
        "fills": len(fills_with_price),
        "avg_price": round(sum(float(r["price"]) for r in fills_with_price) / len(fills_with_price), 2) if fills_with_price else 0,
        "recent": [{
            "symbol": r["symbol"], "side": r["side"],
            "price": float(r["price"]), "ts": r["ts"],
        } for r in fills_with_price[:10]],
    }


def _get_news_sentiment(news_articles):
    """Return news with sentiment scores from signals table (pre-computed by bot)."""
    # Don't load FinBERT here (slow, separate process). Use pre-computed sentiment from signals.
    sentiment_map = {}
    try:
        rows = query("SELECT symbol, sentiment FROM signals WHERE sentiment IS NOT NULL ORDER BY ts DESC LIMIT 50")
        for r in rows:
            sym = r.get("symbol", "")
            if sym and sym not in sentiment_map:
                sentiment_map[sym] = float(r.get("sentiment") or 0)
    except Exception:
        pass
    result = []
    for article in (news_articles or [])[:20]:
        headline = article.get("headline", "")
        symbols_str = article.get("symbols", "")
        # Match sentiment from signals for any symbol in the article
        score = 0
        if symbols_str:
            for sym in symbols_str.split(","):
                sym = sym.strip()
                if sym in sentiment_map:
                    score = sentiment_map[sym]
                    break
        result.append({
            "headline": headline,
            "symbols": symbols_str,
            "sentiment": round(score, 3),
            "ts": article.get("ts"),
        })
    return result


def _get_strategy_leaderboard():
    """Today's strategy performance from signals and outcomes."""
    rows = query("""
        SELECT strategy_tag as strategy, side,
               COUNT(*) as trades, SUM(notional) as volume,
               SUM(CASE WHEN price > 0 THEN 1 ELSE 0 END) as fills
        FROM trades WHERE ts > ?
        GROUP BY strategy_tag
        ORDER BY trades DESC
    """, (time.time() - 86400,))
    return [{
        "strategy": r.get("strategy", "unknown"),
        "trades": r.get("trades", 0),
        "volume": round(float(r.get("volume") or 0), 2),
        "fills": r.get("fills", 0),
    } for r in rows]


def api_data():
    trades_summary = query_one("SELECT COUNT(*) as n, SUM(notional) as vol FROM trades")
    positions = query("""
        SELECT p.symbol, p.ts, p.qty, p.avg_cost, p.market_val, p.unrealised, p.asset_class
        FROM positions p
        INNER JOIN (SELECT symbol, MAX(ts) as max_ts FROM positions GROUP BY symbol) latest
            ON p.symbol = latest.symbol AND p.ts = latest.max_ts
        WHERE p.qty != 0 AND p.ts > ?
        ORDER BY ABS(COALESCE(p.market_val, 0)) DESC LIMIT 30
    """, (time.time() - 300,))
    recent_trades = query("SELECT ts,symbol,side,qty,price,notional,strategy_tag FROM trades ORDER BY ts DESC LIMIT 20")
    plan_row = query_one("SELECT plan_json, summary, trigger, ts FROM investment_plans ORDER BY version DESC LIMIT 1")
    errors = query("SELECT agent,level,message,ts FROM agent_logs WHERE level IN ('ERROR','WARNING') ORDER BY ts DESC LIMIT 20")
    signals = query("SELECT ts,symbol,strategy,side,confidence,sentiment FROM signals ORDER BY ts DESC LIMIT 20")
    outcomes = query("SELECT symbol,strategy,side,pnl,pnl_pct,status FROM outcomes ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 20")
    scores = query("SELECT strategy,win_rate,avg_pnl_pct,sharpe,trade_count,score FROM strategy_scores ORDER BY score DESC")
    strategy_attribution = query("""
        SELECT strategy, date(exit_ts, 'unixepoch') as day, SUM(pnl) as total_pnl, COUNT(*) as trade_count
        FROM outcomes WHERE status = 'closed' AND pnl IS NOT NULL AND exit_ts IS NOT NULL
        GROUP BY strategy, day ORDER BY day ASC
    """)
    plan_history = query("SELECT version, ts, trigger, summary FROM investment_plans ORDER BY version DESC LIMIT 10")
    news_articles = query("SELECT headline, summary, symbols, source, ts FROM news ORDER BY ts DESC LIMIT 50")
    screener_recent = query("SELECT symbol, score, reasons, promoted, ts FROM screener_scores ORDER BY ts DESC LIMIT 30")
    options_trades = query("""SELECT ts,symbol,side,qty,price,notional,strategy_tag
        FROM trades WHERE length(symbol) > 10
        OR strategy_tag IN ('iron_condor','covered_call','cash_secured_put','calendar_spread')
        ORDER BY ts DESC LIMIT 20""")
    rejections = query("""SELECT ts, message FROM agent_logs
        WHERE message LIKE '%risk veto%' OR message LIKE '%order 422%'
        OR message LIKE '%insufficient%' OR message LIKE '%BLOCKED%'
        ORDER BY ts DESC LIMIT 30""")

    plan_data = {}
    if plan_row and plan_row.get("plan_json"):
        try:
            plan_data = json.loads(plan_row["plan_json"])
        except Exception:
            pass

    # Single Alpaca REST call for account + day trade data
    _alpaca_acct = _fetch_alpaca_account()
    account_data = {k: str(v) for k, v in _alpaca_acct.items()
                    if k in ("portfolio_value","buying_power","cash","equity",
                             "last_equity","long_market_value","short_market_value") and v is not None}

    agent_status = _get_agent_status()
    heat_status, trading_paused = _get_heat_and_pause()
    daily_target = _get_daily_target()

    # Fast local computations (no API calls)
    options_pnl = _get_options_pnl(positions)
    options_tracker = _get_options_tracker(positions)
    cash_flow = _get_cash_flow()
    exec_quality = _get_execution_quality()
    news_sentiment = _get_news_sentiment(news_articles)
    strategy_leaderboard = _get_strategy_leaderboard()
    iv_map = _get_iv_regime_map()

    # Derived from single account call
    day_trade_count = {
        "count": int(_alpaca_acct.get("daytrade_count", 0) or 0),
        "limit": 3,
        "pdt_restricted": _alpaca_acct.get("pattern_day_trader", False),
        "equity": float(_alpaca_acct.get("equity", 0) or 0),
        "pdt_threshold": 25000,
    }

    # Cached API calls (30-60s cache)
    countdown = _get_countdown()
    open_orders = _get_open_orders()

    # Allocation breakdown
    allocation = {}
    total_mv = 0
    for p in positions:
        ac = p.get("asset_class") or "unknown"
        mv = abs(float(p.get("market_val") or 0))
        allocation[ac] = allocation.get(ac, 0) + mv
        total_mv += mv
    allocation_pct = {k: round(v / total_mv * 100, 1) if total_mv > 0 else 0
                      for k, v in allocation.items()}

    # Concentration risk
    equity = float(account_data.get("equity", 0) or 0)
    max_conc = 0
    max_conc_sym = ""
    for p in positions:
        mv = abs(float(p.get("market_val") or 0))
        if equity > 0 and mv / equity > max_conc:
            max_conc = mv / equity
            max_conc_sym = p.get("symbol", "")

    return {
        "total_trades":      trades_summary.get("n") or 0,
        "total_volume":      round(trades_summary.get("vol") or 0, 2),
        "positions":         positions,
        "recent_trades":     recent_trades,
        "plan":              plan_data,
        "plan_summary":      plan_row.get("summary", ""),
        "plan_history":      plan_history,
        "errors":            errors,
        "signals":           signals,
        "outcomes":          outcomes,
        "strategy_scores":   scores,
        "strategy_attribution": strategy_attribution,
        "server_ts":         time.time(),
        "account":           account_data,
        "agent_status":      agent_status,
        "trading_paused":    trading_paused,
        "market_open":       _get_market_open(),
        "heat_status":       heat_status,
        "allocation":        allocation_pct,
        "concentration":     {"max_pct": round(max_conc * 100, 1), "symbol": max_conc_sym},
        "parameters":        _get_parameters(),
        "portfolio_history": _get_portfolio_history(),
        "screener_activity": screener_recent,
        "options_trades":    options_trades,
        "live_heat":         round(total_mv / equity * 100, 1) if equity > 0 else 0,
        "rejections":        rejections,
        "news_articles":     news_articles,
        "stream_health":     _get_stream_health(),
        "options_pnl":       options_pnl,
        "countdown":         countdown,
        "options_tracker":   options_tracker,
        "iv_map":            iv_map,
        "open_orders":       open_orders,
        "day_trade_count":   day_trade_count,
        "cash_flow":         cash_flow,
        "exec_quality":      exec_quality,
        "news_sentiment":    news_sentiment,
        "strategy_leaderboard": strategy_leaderboard,
        "daily_target": daily_target,
    }


# ── POST handlers ──

def _read_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)

def _json_response(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def exec_pause(handler):
    if HAS_BOT:
        shared.trading_paused = True
    _json_response(handler, {"status": "ok", "trading_paused": True})

def exec_resume(handler):
    if HAS_BOT:
        shared.trading_paused = False
    _json_response(handler, {"status": "ok", "trading_paused": False})

def exec_emergency_stop(handler):
    if HAS_BOT:
        shared.trading_paused = True
        shared.SHUTTING_DOWN = True
        try:
            from alpaca_local import client as alpaca
            alpaca.cancel_all_orders()
        except Exception as e:
            _json_response(handler, {"status": "partial", "error": str(e)}, 500)
            return
    _json_response(handler, {"status": "ok", "message": "Emergency stop executed"})

def exec_cancel_all(handler):
    if HAS_BOT:
        try:
            from alpaca_local import client as alpaca
            alpaca.cancel_all_orders()
        except Exception as e:
            _json_response(handler, {"error": str(e)}, 500)
            return
    _json_response(handler, {"status": "ok", "message": "All orders cancelled"})

def exec_force_rebalance(handler):
    if HAS_BOT:
        shared.force_rebalance = True
    _json_response(handler, {"status": "ok", "message": "Rebalance queued"})

def exec_update_stance(handler):
    data = _read_body(handler)
    stance = data.get("stance", "neutral")
    if HAS_BOT:
        with shared.cache_lock:
            if shared.investment_plan and isinstance(shared.investment_plan, dict):
                shared.investment_plan["stance"] = stance
    _json_response(handler, {"status": "ok", "stance": stance})

def exec_update_risk_limits(handler):
    data = _read_body(handler)
    if "max_position_size" in data:
        settings.MAX_POSITION_SIZE = float(data["max_position_size"])
    if "max_portfolio_pct" in data:
        pct = float(data["max_portfolio_pct"])
        settings.MAX_PORTFOLIO_PCT = pct / 100 if pct > 1 else pct
    _json_response(handler, {"status": "ok", "message": "Risk limits updated"})

def exec_update_plan(handler):
    data = _read_body(handler)
    if HAS_BOT and shared.investment_plan and isinstance(shared.investment_plan, dict):
        with shared.cache_lock:
            if "targets" in data:
                shared.investment_plan["targets"] = data["targets"]
            if "stance" in data:
                shared.investment_plan["stance"] = data["stance"]
            if "exclusions" in data:
                shared.investment_plan["exclusions"] = data["exclusions"]
    try:
        conn = sqlite3.connect(_db_path())
        conn.execute(
            "INSERT INTO investment_plans (plan_json, summary, trigger, ts) VALUES (?, ?, ?, ?)",
            (json.dumps(data), "Dashboard update", "dashboard_override", time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _json_response(handler, {"error": str(e)}, 500)
        return
    _json_response(handler, {"status": "ok", "message": "Plan updated"})

def exec_rollback_plan(handler):
    try:
        rows = query("SELECT plan_json FROM investment_plans ORDER BY version DESC LIMIT 2")
        if len(rows) < 2:
            _json_response(handler, {"error": "No previous plan"}, 400)
            return
        prev = rows[1]
        conn = sqlite3.connect(_db_path())
        conn.execute(
            "INSERT INTO investment_plans (plan_json, summary, trigger, ts) VALUES (?, ?, ?, ?)",
            (prev["plan_json"], "Rollback", "dashboard_rollback", time.time())
        )
        conn.commit()
        conn.close()
        if HAS_BOT:
            with shared.cache_lock:
                try:
                    shared.investment_plan = json.loads(prev["plan_json"])
                except Exception:
                    pass
        _json_response(handler, {"status": "ok", "message": "Rolled back"})
    except Exception as e:
        _json_response(handler, {"error": str(e)}, 500)


def exec_update_parameters(handler):
    """Update trading parameters at runtime."""
    data = _read_body(handler)
    updated = []
    # Map of param name -> (settings attr, transform)
    param_map = {
        "max_position_size":      ("MAX_POSITION_SIZE",      float),
        "max_portfolio_pct":      ("MAX_PORTFOLIO_PCT",       lambda v: float(v) / 100),
        "margin_min_equity":      ("MARGIN_MIN_EQUITY",       float),
        "max_daily_loss_pct":     ("MAX_DAILY_LOSS_PCT",      lambda v: float(v) / 100),
        "max_consecutive_losses": ("MAX_CONSECUTIVE_LOSSES",  int),
        "rsi_oversold":           ("RSI_OVERSOLD",            float),
        "rsi_overbought":         ("RSI_OVERBOUGHT",          float),
        "rebalance_threshold":    ("REBALANCE_THRESHOLD",     lambda v: float(v) / 100),
        "vix_caution":            ("VIX_CAUTION",             float),
        "vix_high":               ("VIX_HIGH",                float),
        "vix_extreme":            ("VIX_EXTREME",             float),
        "heat_warn":              ("HEAT_WARN",               lambda v: float(v) / 100),
        "heat_max":               ("HEAT_MAX",                lambda v: float(v) / 100),
        "options_enabled":        ("OPTIONS_ENABLED",         lambda v: v if isinstance(v, bool) else str(v).lower() == "true"),
        "options_level":          ("OPTIONS_LEVEL",           int),
        "screener_enabled":       ("SCREENER_ENABLED",        lambda v: v if isinstance(v, bool) else str(v).lower() == "true"),
        "screener_interval":      ("SCREENER_INTERVAL",       int),
        "max_watchlist_size":     ("MAX_WATCHLIST_SIZE",       int),
        "screener_promote":       ("SCREENER_PROMOTE_THRESHOLD", float),
        "screener_demote":        ("SCREENER_DEMOTE_THRESHOLD",  float),
        "tick_interval":          ("TICK_INTERVAL",           int),
    }
    for key, val in data.items():
        if key in param_map:
            attr, transform = param_map[key]
            try:
                setattr(settings, attr, transform(val))
                updated.append(key)
            except Exception as e:
                _json_response(handler, {"error": f"Invalid value for {key}: {e}"}, 400)
                return
    _json_response(handler, {"status": "ok", "updated": updated})


def api_bandit():
    """Return bandit performance data for dashboard."""
    result = {
        "mode": "shadow",
        "total_observations": 0,
        "alpha": 0.3,
        "days_until_live": 30,
        "top_strategies": [],
        "bottom_strategies": [],
        "recent_decisions": [],
    }

    try:
        from agents import bandit as bandit_mod
        info = bandit_mod.get_mode_info()
        result.update(info)
    except Exception:
        pass

    # Per-strategy stats from bandit_decisions
    try:
        strat_stats = query("""
            SELECT strategy_id,
                   AVG(reward_computed) as avg_reward,
                   COUNT(*) as observations,
                   AVG(multiplier_applied) as avg_multiplier
            FROM bandit_decisions
            WHERE evaluated = 1 AND reward_computed IS NOT NULL
            GROUP BY strategy_id
            ORDER BY avg_reward DESC
        """)

        if strat_stats:
            result["top_strategies"] = [
                {"id": s["strategy_id"], "multiplier": round(s["avg_multiplier"] or 1.0, 3),
                 "observations": s["observations"], "avg_reward": round(s["avg_reward"] or 0, 4)}
                for s in strat_stats[:5]
            ]
            result["bottom_strategies"] = [
                {"id": s["strategy_id"], "multiplier": round(s["avg_multiplier"] or 1.0, 3),
                 "observations": s["observations"], "avg_reward": round(s["avg_reward"] or 0, 4)}
                for s in strat_stats[-5:]
            ] if len(strat_stats) > 5 else []
    except Exception:
        pass

    # Recent decisions
    try:
        recent = query("""
            SELECT ts, strategy_id, multiplier_applied, reward_computed, shadow_mode
            FROM bandit_decisions
            ORDER BY ts DESC LIMIT 20
        """)
        result["recent_decisions"] = [
            {"ts": r["ts"], "strategy_id": r["strategy_id"],
             "multiplier": round(r["multiplier_applied"] or 1.0, 3),
             "reward": round(r["reward_computed"], 4) if r["reward_computed"] is not None else None,
             "shadow": bool(r["shadow_mode"])}
            for r in recent
        ]
    except Exception:
        pass

    return result


POST_ROUTES = {
    "/api/exec/pause":           exec_pause,
    "/api/exec/resume":          exec_resume,
    "/api/exec/emergency-stop":  exec_emergency_stop,
    "/api/exec/cancel-all":      exec_cancel_all,
    "/api/exec/force-rebalance": exec_force_rebalance,
    "/api/exec/stance":          exec_update_stance,
    "/api/exec/risk-limits":     exec_update_risk_limits,
    "/api/exec/plan":            exec_update_plan,
    "/api/exec/plan-rollback":   exec_rollback_plan,
    "/api/exec/parameters":      exec_update_parameters,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        """Override to catch broken pipe / connection aborted errors."""
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Client disconnected — ignore

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            try:
                body = json.dumps(api_data()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"API /api/data error:\n{tb}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(tb.encode())
        elif path == "/api/bandit":
            try:
                body = json.dumps(api_bandit()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"API /api/bandit error:\n{tb}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(tb.encode())
        elif path in ("/", "/dashboard"):
            try:
                with open(HTML_PATH, "r", encoding="utf-8") as f:
                    body = f.read().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        handler_fn = POST_ROUTES.get(path)
        if handler_fn:
            try:
                handler_fn(self)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"API POST {path} error:\n{tb}")
                _json_response(self, {"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

if __name__ == "__main__":
    server = HTTPServer(("localhost", settings.DASHBOARD_PORT), Handler)
    print(f"Dashboard running at http://localhost:{settings.DASHBOARD_PORT}")
    print(f"Exec Suite API: {len(POST_ROUTES)} endpoints active")
    print(f"Database: {_db_path()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
