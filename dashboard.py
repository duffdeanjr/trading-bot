import json, sqlite3, time, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

DB_PATH   = r"C:\Users\duffd\OneDrive\Desktop\Claude IO\trading-bot\trading.db"
HTML_PATH = r"C:\Users\duffd\OneDrive\Desktop\Claude IO\trading-bot\dashboard.html"
PORT = 5050

def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def query_one(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
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

    plan_data = {}
    if plan_row and plan_row.get("plan_json"):
        try:
            plan_data = json.loads(plan_row["plan_json"])
        except Exception:
            pass

    return {
        "total_trades":  trades_summary.get("n") or 0,
        "total_volume":  round(trades_summary.get("vol") or 0, 2),
        "positions":     positions,
        "recent_trades": recent_trades,
        "plan":          plan_data,
        "plan_summary":  plan_row.get("summary", ""),
        "errors":        errors,
        "signals":       signals,
        "server_ts":     time.time(),
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

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

if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
