"""
status.py — Print account snapshot: positions, P&L, buying power, recent orders.
Output is a plain-text table suitable for pasting into chat.

Usage:  python status.py
        python status.py | clip     (copy to clipboard on Windows)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from alpaca_local import client as alpaca
from alpaca.trading.requests import GetOrdersRequest


def _fmt_dollar(v):
    return f"${v:,.2f}"


def _fmt_pct(v):
    return f"{v:+.2f}%"


def main():
    # ── Account summary ──────────────────────────────────────────
    acct = alpaca.get_account()
    equity = float(acct.equity)
    cash = float(acct.cash)
    buying_power = float(acct.buying_power)
    last_equity = float(acct.last_equity)
    day_pnl = equity - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0
    long_mv = float(acct.long_market_value)
    short_mv = float(acct.short_market_value)

    print("=" * 78)
    print("ACCOUNT SUMMARY")
    print("=" * 78)
    print(f"  Equity:          {_fmt_dollar(equity)}")
    print(f"  Cash:            {_fmt_dollar(cash)}")
    print(f"  Buying Power:    {_fmt_dollar(buying_power)}")
    print(f"  Long MV:         {_fmt_dollar(long_mv)}")
    print(f"  Short MV:        {_fmt_dollar(short_mv)}")
    print(f"  Day P&L:         {_fmt_dollar(day_pnl)}  ({_fmt_pct(day_pnl_pct)})")
    print()

    # ── Open positions ───────────────────────────────────────────
    positions = alpaca.get_positions()
    print("=" * 78)
    print(f"OPEN POSITIONS ({len(positions)})")
    print("=" * 78)

    if not positions:
        print("  (none)")
    else:
        hdr = (f"{'Symbol':<12} {'Qty':>10} {'Avg Entry':>10} {'Current':>10}"
               f" {'Mkt Value':>12} {'P&L ($)':>10} {'P&L (%)':>8}")
        print(hdr)
        print("-" * 78)
        total_mv = 0
        total_pnl = 0
        rows = []
        for p in positions:
            qty = float(p.qty)
            avg = float(p.avg_entry_price)
            cur = float(p.current_price)
            mv = float(p.market_value)
            pnl = float(p.unrealized_pl)
            pnl_pct = float(p.unrealized_plpc) * 100
            total_mv += mv
            total_pnl += pnl
            rows.append((p.symbol, qty, avg, cur, mv, pnl, pnl_pct))

        rows.sort(key=lambda r: r[5], reverse=True)  # sort by P&L descending
        for sym, qty, avg, cur, mv, pnl, pnl_pct in rows:
            print(f"{sym:<12} {qty:>10.4f} {avg:>10.2f} {cur:>10.2f}"
                  f" {mv:>12.2f} {pnl:>+10.2f} {pnl_pct:>+7.2f}%")
        print("-" * 78)
        total_pnl_pct = (total_pnl / (total_mv - total_pnl) * 100) if (total_mv - total_pnl) else 0
        print(f"{'TOTAL':<12} {'':>10} {'':>10} {'':>10}"
              f" {total_mv:>12.2f} {total_pnl:>+10.2f} {total_pnl_pct:>+7.2f}%")
    print()

    # ── Recent orders (last 10) ──────────────────────────────────
    recent = alpaca._client.get_orders(
        GetOrdersRequest(status="all", limit=10)
    )
    print("=" * 78)
    print(f"RECENT ORDERS (last {len(recent)})")
    print("=" * 78)

    if not recent:
        print("  (none)")
    else:
        hdr = (f"{'Time':<20} {'Symbol':<12} {'Side':<6} {'Type':<8}"
               f" {'Qty':>8} {'Filled':>8} {'Price':>10} {'Status':<10}")
        print(hdr)
        print("-" * 86)
        for o in recent:
            ts = o.submitted_at.strftime("%Y-%m-%d %H:%M") if o.submitted_at else ""
            qty = str(o.qty or o.notional or "")
            filled = str(o.filled_qty or "")
            price = f"{float(o.filled_avg_price):.2f}" if o.filled_avg_price else ""
            print(f"{ts:<20} {o.symbol:<12} {str(o.side).split('.')[-1]:<6}"
                  f" {str(o.order_type).split('.')[-1]:<8}"
                  f" {qty:>8} {filled:>8} {price:>10} {str(o.status).split('.')[-1]:<10}")
    print()


if __name__ == "__main__":
    main()
