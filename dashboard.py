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

# ── Try to import shared state (only works when bot is running) ──
try:
    import shared
    HAS_BOT = True
except ImportError:
    HAS_BOT = False

def query(sql, params=()):
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def query_one(sql, params=()):
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()

def api_data():
    trades_summary = query_one("SELECT COUNT(*) as n, SUM(notional) as vol FROM trades")
    positions = query("""
        SELECT symbol, MAX(ts) as ts, qty, avg_cost, market_val, unrealised
        FROM positions GROUP BY symbol ORDER BY ABS(COALESCE(market_val,0)) DESC LIMIT 20
    """)
    recent_trades = query("SELECT ts,symbol,side,qty,price,notional,strategy_tag FROM trades ORDER BY ts DESC LIMIT 20")
    plan_row = query_one("SELECT plan_json, summary, trigger, ts FROM investment_plans ORDER BY version DESC LIMIT 1")
    errors = query("SELECT agent,level,message,ts FROM agent_logs WHERE level IN ('ERROR','WARNING') ORDER BY ts DESC LIMIT 20")
    signals = query("SELECT ts,symbol,strategy,side,confidence,sentiment FROM signals ORDER BY ts DESC LIMIT 20")
    outcomes = query("SELECT symbol,strategy,side,pnl,pnl_pct,status FROM outcomes ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 20")
    scores = query("SELECT strategy,win_rate,avg_pnl_pct,sharpe,trade_count,score FROM strategy_scores ORDER BY score DESC")

    plan_data = {}
    if plan_row and plan_row.get("plan_json"):
        try:
            plan_data = json.loads(plan_row["plan_json"])
        except Exception:
            pass

    # ── Enrich with live shared state when bot is running ──
    account_data = {}
    agent_status = []
    trading_paused = False
    if HAS_BOT:
        # Account
        with shared.account_lock:
            acct = shared.account
        if acct:
            for key in ("portfolio_value", "buying_power", "cash", "equity",
                        "last_equity", "long_market_value", "short_market_value"):
                val = acct.get(key) if isinstance(acct, dict) else getattr(acct, key, None)
                if val is not None:
                    account_data[key] = str(val)

        # Agent health
        with shared.errors_lock:
            errs = dict(shared.AGENT_ERRORS)
        for name in ["ref_library", "account_agent", "signal_generator",
                      "plan_manager", "order_execution", "risk_manager",
                      "boss", "diagnostics"]:
            info = errs.get(name, {})
            agent_status.append({
                "name": name,
                "status": "error" if info.get("count", 0) > 0 else "running",
                "restarts": info.get("count", 0),
                "last_error": info.get("last_error"),
            })

        # Trading paused flag
        trading_paused = getattr(shared, "trading_paused", False)

    return {
        "total_trades":    trades_summary.get("n") or 0,
        "total_volume":    round(trades_summary.get("vol") or 0, 2),
        "positions":       positions,
        "recent_trades":   recent_trades,
        "plan":            plan_data,
        "plan_summary":    plan_row.get("summary", ""),
        "errors":          errors,
        "signals":         signals,
        "outcomes":        outcomes,
        "strategy_scores": scores,
        "server_ts":       time.time(),
        "account":         account_data,
        "agent_status":    agent_status,
        "trading_paused":  trading_paused,
        "market_open":     getattr(shared, "MARKET_OPEN", False) if HAS_BOT else False,
    }


# ═══════════════════════════════════════════════════════════
# EXEC SUITE — POST handlers
# ═══════════════════════════════════════════════════════════

def _read_body(handler):
    """Read and parse JSON body from a POST/PUT request."""
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)

def _json_response(handler, data, status=200):
    """Send a JSON response."""
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def exec_pause(handler):
    """CEO: Pause all order execution."""
    if HAS_BOT:
        shared.trading_paused = True
    _json_response(handler, {"status": "ok", "trading_paused": True})

def exec_resume(handler):
    """CEO: Resume order execution."""
    if HAS_BOT:
        shared.trading_paused = False
    _json_response(handler, {"status": "ok", "trading_paused": False})

def exec_emergency_stop(handler):
    """Emergency: halt everything and cancel all open orders."""
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
    """Cancel all open orders without stopping the bot."""
    if HAS_BOT:
        try:
            from alpaca_local import client as alpaca
            alpaca.cancel_all_orders()
        except Exception as e:
            _json_response(handler, {"error": str(e)}, 500)
            return
    _json_response(handler, {"status": "ok", "message": "All orders cancelled"})

def exec_force_rebalance(handler):
    """CIO: Trigger immediate rebalance cycle."""
    if HAS_BOT:
        shared.force_rebalance = True
    _json_response(handler, {"status": "ok", "message": "Rebalance queued"})

def exec_update_stance(handler):
    """CEO: Update market stance."""
    data = _read_body(handler)
    stance = data.get("stance", "neutral")
    if HAS_BOT:
        with shared.cache_lock:
            if shared.investment_plan and isinstance(shared.investment_plan, dict):
                shared.investment_plan["stance"] = stance
    _json_response(handler, {"status": "ok", "stance": stance})

def exec_update_risk_limits(handler):
    """CRO: Override risk limits at runtime."""
    data = _read_body(handler)
    if "max_position_size" in data:
        settings.MAX_POSITION_SIZE = float(data["max_position_size"])
    if "max_portfolio_pct" in data:
        pct = float(data["max_portfolio_pct"])
        settings.MAX_PORTFOLIO_PCT = pct / 100 if pct > 1 else pct
    _json_response(handler, {"status": "ok", "message": "Risk limits updated"})

def exec_update_plan(handler):
    """CIO: Update investment plan targets."""
    data = _read_body(handler)
    # Update in-memory
    if HAS_BOT and shared.investment_plan and isinstance(shared.investment_plan, dict):
        with shared.cache_lock:
            if "targets" in data:
                shared.investment_plan["targets"] = data["targets"]
            if "stance" in data:
                shared.investment_plan["stance"] = data["stance"]
            if "exclusions" in data:
                shared.investment_plan["exclusions"] = data["exclusions"]
    # Persist to DB
    try:
        conn = sqlite3.connect(settings.DB_PATH)
        conn.execute(
            "INSERT INTO investment_plans (plan_json, summary, trigger, ts) VALUES (?, ?, ?, ?)",
            (json.dumps(data), "Exec Suite update", "cio_override", time.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _json_response(handler, {"error": str(e)}, 500)
        return
    _json_response(handler, {"status": "ok", "message": "Plan updated"})

def exec_rollback_plan(handler):
    """CIO: Roll back to previous plan version."""
    try:
        rows = query("SELECT plan_json FROM investment_plans ORDER BY version DESC LIMIT 2")
        if len(rows) < 2:
            _json_response(handler, {"error": "No previous plan"}, 400)
            return
        prev = rows[1]
        conn = sqlite3.connect(settings.DB_PATH)
        conn.execute(
            "INSERT INTO investment_plans (plan_json, summary, trigger, ts) VALUES (?, ?, ?, ?)",
            (prev["plan_json"], "Rollback", "cio_rollback", time.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        # Update in-memory
        if HAS_BOT:
            with shared.cache_lock:
                try:
                    shared.investment_plan = json.loads(prev["plan_json"])
                except:
                    pass
        _json_response(handler, {"status": "ok", "message": "Rolled back"})
    except Exception as e:
        _json_response(handler, {"error": str(e)}, 500)


# ── Route table for POST endpoints ──
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
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
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
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
