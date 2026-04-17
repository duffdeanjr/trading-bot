import os
import json
import sqlite3
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from config import settings

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
    """Return real stream heartbeat data from alpaca_local.stream."""
    now = time.time()
    names = ["trade", "stock", "crypto", "option", "news"]
    result = {}
    hb = {}
    rc = {}
    try:
        from alpaca_local import stream as st
        hb = st.get_heartbeats()
        rc = st.get_reconnect_counts()
    except Exception:
        pass
    for name in names:
        ts = hb.get(name, 0)
        age = round(now - ts, 1) if ts > 0 else None
        reconnects = rc.get(name, 0)
        result[name] = {
            "last_msg_age": age,
            "reconnects": reconnects,
            "connected": ts > 0,
        }
    return result

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

def api_data():
    trades_summary = query_one("SELECT COUNT(*) as n, SUM(notional) as vol FROM trades")
    positions = query("""
        SELECT symbol, MAX(ts) as ts, qty, avg_cost, market_val, unrealised, asset_class
        FROM positions GROUP BY symbol ORDER BY ABS(COALESCE(market_val,0)) DESC LIMIT 20
    """)
    recent_trades = query("SELECT ts,symbol,side,qty,price,notional,strategy_tag FROM trades ORDER BY ts DESC LIMIT 20")
    plan_row = query_one("SELECT plan_json, summary, trigger, ts FROM investment_plans ORDER BY version DESC LIMIT 1")
    errors = query("SELECT agent,level,message,ts FROM agent_logs WHERE level IN ('ERROR','WARNING') ORDER BY ts DESC LIMIT 20")
    signals = query("SELECT ts,symbol,strategy,side,confidence,sentiment FROM signals ORDER BY ts DESC LIMIT 20")
    outcomes = query("""SELECT symbol, strategy, side, entry_price, exit_price,
        pnl, pnl_pct, hold_duration_s, status, entry_ts, exit_ts
        FROM outcomes ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 30""")
    scores = query("SELECT strategy,win_rate,avg_pnl_pct,sharpe,trade_count,score FROM strategy_scores ORDER BY score DESC")
    strategy_attribution = query("""
        SELECT strategy,
               date(exit_ts, 'unixepoch') as day,
               SUM(pnl) as total_pnl,
               COUNT(*) as trade_count
        FROM outcomes
        WHERE status = 'closed' AND pnl IS NOT NULL AND exit_ts IS NOT NULL
        GROUP BY strategy, day
        ORDER BY day ASC
    """)
    plan_history = query("SELECT version, ts, trigger, summary FROM investment_plans ORDER BY version DESC LIMIT 10")

    # News articles (today + recent)
    news_articles = query("""SELECT headline, summary, symbols, source, ts
        FROM news ORDER BY ts DESC LIMIT 50""")

    # Screener activity
    screener_recent = query("""SELECT symbol, score, reasons, promoted, ts
        FROM screener_scores ORDER BY ts DESC LIMIT 30""")

    # Options trades
    options_trades = query("""SELECT ts,symbol,side,qty,price,notional,strategy_tag
        FROM trades WHERE length(symbol) > 10
        OR strategy_tag IN ('iron_condor','covered_call','cash_secured_put','calendar_spread')
        ORDER BY ts DESC LIMIT 20""")

    # Rejections from agent logs
    rejections = query("""SELECT ts, message FROM agent_logs
        WHERE message LIKE '%risk veto%' OR message LIKE '%order 422%'
        OR message LIKE '%insufficient%' OR message LIKE '%BLOCKED%'
        ORDER BY ts DESC LIMIT 30""")

    # Portfolio history from Alpaca
    portfolio_history_data = []
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
            portfolio_history_data = [
                {"ts": _ts[i], "equity": _eq[i], "pnl": _pnl[i] if i < len(_pnl) else 0}
                for i in range(len(_ts)) if _eq[i] and _eq[i] > 0
            ]
    except Exception:
        pass

    plan_data = {}
    if plan_row and plan_row.get("plan_json"):
        try:
            plan_data = json.loads(plan_row["plan_json"])
        except Exception:
            pass

    account_data = {}
    agent_status = []
    trading_paused = False
    heat_status = {}

    if HAS_BOT:
        # Account — pull directly from Alpaca (dashboard is a separate process)
        try:
            from alpaca_local import client as alpaca
            acct = alpaca.get_account()
            if acct:
                for key in ("portfolio_value", "buying_power", "cash", "equity",
                            "last_equity", "long_market_value", "short_market_value"):
                    val = getattr(acct, key, None)
                    if val is not None:
                        account_data[key] = str(val)
        except Exception:
            pass

        # Agent health
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
            agent_status.append({
                "name": name,
                "status": status,
                "restarts": info.get("count", 0),
                "last_error": info.get("last_error"),
                "heartbeat_age_s": hb_age,
            })

        # Heat status
        try:
            from agents import risk_manager
            heat_status = risk_manager.get_heat_status()
        except Exception:
            heat_status = {}

        # Trading paused flag
        trading_paused = getattr(shared, "trading_paused", False)

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
        if equity > 0:
            pct = mv / equity
            if pct > max_conc:
                max_conc = pct
                max_conc_sym = p.get("symbol", "")

    # Current parameters snapshot
    params = {
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
    }

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
        "parameters":        params,
        "portfolio_history": portfolio_history_data,
        "screener_activity": screener_recent,
        "options_trades":    options_trades,
        "live_heat":         round(total_mv / equity * 100, 1) if equity > 0 else 0,
        "rejections":        rejections,
        "news_articles":     news_articles,
        "stream_health":     _get_stream_health(),
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


def api_health():
    """Return system health status."""
    import os
    result = {"status": "ok", "uptime_s": 0, "agents": {}, "db": {}}
    try:
        result["agents"] = {name: round(time.time() - ts, 1)
                           for name, ts in shared.get_heartbeats().items()} if hasattr(shared, 'get_heartbeats') else {}
    except Exception:
        pass
    try:
        db_size = os.path.getsize(_db_path())
        wal_size = os.path.getsize(_db_path() + "-wal") if os.path.exists(_db_path() + "-wal") else 0
        result["db"] = {"size_mb": round(db_size / 1e6, 1), "wal_mb": round(wal_size / 1e6, 1)}
    except Exception:
        pass
    try:
        from agents import risk_manager
        result["risk"] = risk_manager.get_heat_status()
    except Exception:
        pass
    result["market_open"] = getattr(shared, "MARKET_OPEN", False)
    result["shutting_down"] = getattr(shared, "SHUTTING_DOWN", False)
    return result


def api_signals():
    """Return recent signals."""
    try:
        rows = query("SELECT ts, symbol, strategy, side, confidence, sentiment FROM signals ORDER BY ts DESC LIMIT 50")
        return {"signals": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "signals": []}


def api_outcomes():
    """Return trade outcomes."""
    try:
        rows = query("SELECT * FROM outcomes ORDER BY entry_ts DESC LIMIT 50")
        return {"outcomes": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "outcomes": []}


def api_agent_logs():
    """Return recent agent log entries."""
    try:
        rows = query("SELECT ts, agent, level, message FROM agent_logs ORDER BY ts DESC LIMIT 50")
        return {"logs": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "logs": []}


def api_factory():
    """Return strategy factory pipeline status."""
    result = {"candidate": 0, "shadow": 0, "live": 0, "retired": 0,
              "shadow_signals": 0, "recent_promotions": []}
    try:
        for status in ["candidate", "shadow", "live", "retired"]:
            result[status] = query(f"SELECT COUNT(*) as cnt FROM strategy_recipes WHERE status=?",
                                   (status,))[0]["cnt"]
        result["shadow_signals"] = query("SELECT COUNT(*) as cnt FROM shadow_signals")[0]["cnt"]
        # Recent promotions
        recent = query("""SELECT id, status, entry, filter, exit, backtest_sharpe, backtest_win_rate
                         FROM strategy_recipes WHERE status != 'candidate'
                         ORDER BY last_updated_ts DESC LIMIT 10""")
        result["recent_promotions"] = recent
    except Exception:
        pass
    return result


def api_screener():
    """Return screener status."""
    result = {"total_scanned": 0, "promoted": 0, "max_score": 0,
              "threshold": settings.SCREENER_PROMOTE_THRESHOLD, "top_candidates": []}
    try:
        stats = query("SELECT COUNT(*) as total, SUM(CASE WHEN promoted=1 THEN 1 ELSE 0 END) as promoted, MAX(score) as max_score FROM screener_scores")
        if stats:
            result["total_scanned"] = stats[0]["total"]
            result["promoted"] = stats[0]["promoted"]
            result["max_score"] = stats[0]["max_score"]
        # Top recent scores
        top = query("SELECT symbol, score, reasons FROM screener_scores ORDER BY score DESC LIMIT 10")
        result["top_candidates"] = top
    except Exception:
        pass
    return result


def api_notifications():
    """Return recent notification alerts."""
    try:
        from agents.notifier import get_recent_notifications
        return {"notifications": get_recent_notifications(20)}
    except ImportError:
        return {"notifications": []}
    except Exception:
        return {"notifications": []}


def api_earnings():
    """Return earnings calendar information."""
    try:
        from agents.earnings_calendar import get_earnings_info
        return get_earnings_info()
    except ImportError:
        return {"total_tracked": 0, "upcoming": [], "blackout_days": 0}
    except Exception:
        return {"total_tracked": 0, "upcoming": [], "blackout_days": 0}


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
                print("API ERROR:", tb)
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
                print("BANDIT API ERROR:", tb)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(tb.encode())
        elif path in ("/api/health", "/api/signals", "/api/outcomes", "/api/logs",
                      "/api/factory", "/api/screener", "/api/notifications",
                      "/api/earnings"):
            api_map = {
                "/api/health": api_health,
                "/api/signals": api_signals,
                "/api/outcomes": api_outcomes,
                "/api/logs": api_agent_logs,
                "/api/factory": api_factory,
                "/api/screener": api_screener,
                "/api/notifications": api_notifications,
                "/api/earnings": api_earnings,
            }
            try:
                body = json.dumps(api_map[path](), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"API ERROR [{path}]:", tb)
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
                print(f"EXEC API ERROR [{path}]:", tb)
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
