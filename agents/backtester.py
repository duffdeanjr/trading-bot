"""
agents/backtester.py -- Backtesting engine using stored OHLCV bars from SQLite.

Replays historical data through the signal logic and computes:
  - Total return, annualized return
  - Sharpe ratio, max drawdown
  - Win rate, average win/loss
  - P&L by strategy

Run from command line:
  python -m agents.backtester --days 30 --capital 100000
"""

import math
import logging
import argparse
import datetime

logger = logging.getLogger(__name__)


def load_ohlcv(days: int = 30) -> dict:
    """
    Load OHLCV from SQLite database.
    Returns {symbol: {"closes": [...], "highs": [...], "lows": [...], "volumes": [...]}}
    """
    from storage import database
    database.init_db()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    data = database.read_bars_all(since_date=cutoff)
    logger.info(f"backtester: loaded {len(data)} symbols with {days}d of OHLCV data")
    return data


def generate_signals(symbol: str, ohlcv: dict) -> list:
    """
    Run signal logic on historical OHLCV for one symbol.
    Returns list of (date_idx, side, price, strategy) tuples.
    """
    from agents.indicators import rsi, ema

    closes  = ohlcv["closes"]
    signals = []

    for i in range(20, len(closes)):
        window = closes[:i+1]
        rsi_val = rsi(window)
        ema9    = ema(window, 9)
        ema21   = ema(window, 21)

        price = closes[i]

        if rsi_val and rsi_val < 35 and len(ema9) >= 2 and len(ema21) >= 2:
            if ema9[-1] > ema21[-1]:
                signals.append((i, "buy", price, "rsi_oversold"))

        if rsi_val and rsi_val > 70:
            signals.append((i, "sell", price, "rsi_overbought"))

    return signals


def simulate(ohlcv_data: dict, capital: float = 100_000,
             position_size_pct: float = 0.05,
             stop_loss_pct: float = 0.05,
             take_profit_pct: float = 0.10) -> dict:
    """
    Simulate trading signals across all symbols.
    Returns performance metrics dict.
    """
    trades = []
    equity_curve = [capital]
    cash = capital
    positions = {}

    all_dates = set()
    for ohlcv in ohlcv_data.values():
        all_dates.update(range(len(ohlcv["closes"])))
    n_days = max(all_dates) + 1 if all_dates else 0

    sig_map = {}
    for symbol, ohlcv in ohlcv_data.items():
        sig_map[symbol] = {}
        for day_idx, side, price, strategy in generate_signals(symbol, ohlcv):
            sig_map[symbol].setdefault(day_idx, []).append((side, price, strategy))

    for day in range(n_days):
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            ohlcv = ohlcv_data.get(symbol, {})
            closes = ohlcv.get("closes", [])
            if day >= len(closes):
                continue
            price = closes[day]

            exit_price = None
            exit_reason = None
            if pos["side"] == "buy":
                if price <= pos["stop"]:
                    exit_price, exit_reason = pos["stop"], "stop_loss"
                elif price >= pos["take_profit"]:
                    exit_price, exit_reason = pos["take_profit"], "take_profit"
            else:
                if price >= pos["stop"]:
                    exit_price, exit_reason = pos["stop"], "stop_loss"
                elif price <= pos["take_profit"]:
                    exit_price, exit_reason = pos["take_profit"], "take_profit"

            if exit_price:
                pnl = (exit_price - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "buy" else -1)
                cash += pos["qty"] * exit_price
                trades.append({
                    "symbol":   symbol,
                    "side":     pos["side"],
                    "entry":    pos["entry"],
                    "exit":     exit_price,
                    "qty":      pos["qty"],
                    "pnl":      pnl,
                    "reason":   exit_reason,
                    "strategy": pos["strategy"],
                })
                del positions[symbol]

        for symbol, day_sigs in sig_map.items():
            if day not in day_sigs:
                continue
            ohlcv = ohlcv_data.get(symbol, {})
            closes = ohlcv.get("closes", [])
            if day >= len(closes):
                continue
            price = closes[day]
            if price <= 0:
                continue

            for side, sig_price, strategy in day_sigs[day]:
                if symbol in positions:
                    continue

                notional = min(cash * position_size_pct, capital * position_size_pct)
                if notional < 1:
                    continue
                qty = notional / price

                if side == "buy":
                    stop        = price * (1 - stop_loss_pct)
                    take_profit = price * (1 + take_profit_pct)
                else:
                    stop        = price * (1 + stop_loss_pct)
                    take_profit = price * (1 - take_profit_pct)

                cash -= notional
                positions[symbol] = {
                    "side":        side,
                    "entry":       price,
                    "qty":         qty,
                    "stop":        stop,
                    "take_profit": take_profit,
                    "strategy":    strategy,
                    "day_idx":     day,
                }

        pos_value = sum(
            ohlcv_data[sym]["closes"][min(day, len(ohlcv_data[sym]["closes"])-1)] * pos["qty"]
            for sym, pos in positions.items()
            if sym in ohlcv_data
        )
        equity_curve.append(cash + pos_value)

    if not trades:
        return {"error": "no trades generated", "n_days": n_days}

    total_return  = (equity_curve[-1] - capital) / capital
    wins          = [t for t in trades if t["pnl"] > 0]
    losses        = [t for t in trades if t["pnl"] <= 0]
    win_rate      = len(wins) / len(trades) if trades else 0
    avg_win       = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss      = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    total_pnl     = sum(t["pnl"] for t in trades)

    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    daily_returns = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
                     for i in range(1, len(equity_curve)) if equity_curve[i-1] > 0]
    ann_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1 if n_days > 0 else 0
    if daily_returns:
        mean_r = sum(daily_returns) / len(daily_returns)
        std_r  = math.sqrt(sum((r - mean_r)**2 for r in daily_returns) / len(daily_returns))
        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0
    else:
        sharpe = 0

    by_strategy = {}
    for t in trades:
        s = t["strategy"]
        if s not in by_strategy:
            by_strategy[s] = {"trades": 0, "pnl": 0, "wins": 0}
        by_strategy[s]["trades"] += 1
        by_strategy[s]["pnl"]    += t["pnl"]
        if t["pnl"] > 0:
            by_strategy[s]["wins"] += 1

    return {
        "capital":       capital,
        "final_equity":  round(equity_curve[-1], 2),
        "total_return":  round(total_return * 100, 2),
        "ann_return":    round(ann_return * 100, 2),
        "sharpe":        round(sharpe, 3),
        "max_drawdown":  round(max_dd * 100, 2),
        "n_trades":      len(trades),
        "win_rate":      round(win_rate * 100, 1),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "total_pnl":     round(total_pnl, 2),
        "n_days":        n_days,
        "n_symbols":     len(ohlcv_data),
        "by_strategy":   by_strategy,
    }


def seed_strategy_scores(results: dict):
    """Pre-populate strategy_scores table from backtest results (cold-start fix)."""
    from storage import database
    by_strat = results.get("by_strategy", {})
    for strat, s in by_strat.items():
        if s["trades"] < 5:
            continue
        wr = s["wins"] / s["trades"] if s["trades"] else 0
        avg_pnl = s["pnl"] / s["trades"] if s["trades"] else 0
        # Simplified score: weight win_rate and avg_pnl
        score = 0.5 * wr + 0.5 * min(max(avg_pnl / 100, 0), 1)
        database.write_strategy_score(strat, wr, avg_pnl, 0.0, s["trades"], round(score, 3))
        print(f"  seeded score: {strat} -> {score:.3f} (wr={wr:.0%}, trades={s['trades']})")


def run_backtest(days: int = 30, capital: float = 100_000, seed_scores: bool = False):
    """Main entry point for running a backtest. Use seed_scores=True to pre-populate strategy scores."""
    print(f"\n=== Backtest: {days}d | capital=${capital:,.0f} ===\n")
    ohlcv_data = load_ohlcv(days)
    if not ohlcv_data:
        print("No OHLCV data found. Run the bot for at least 1 day first.")
        return

    results = simulate(ohlcv_data, capital=capital)
    if "error" in results:
        print(f"Error: {results['error']}")
        return

    print(f"Symbols traded:    {results['n_symbols']}")
    print(f"Days simulated:    {results['n_days']}")
    print(f"Total trades:      {results['n_trades']}")
    print(f"Win rate:          {results['win_rate']}%")
    print(f"Total P&L:         ${results['total_pnl']:,.2f}")
    print(f"Total return:      {results['total_return']}%")
    print(f"Annualized return: {results['ann_return']}%")
    print(f"Sharpe ratio:      {results['sharpe']}")
    print(f"Max drawdown:      {results['max_drawdown']}%")
    print(f"Avg win:           ${results['avg_win']:,.2f}")
    print(f"Avg loss:          ${results['avg_loss']:,.2f}")
    print(f"Final equity:      ${results['final_equity']:,.2f}")
    print()
    print("By strategy:")
    for strat, s in results["by_strategy"].items():
        wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        print(f"  {strat:<25} trades={s['trades']:3d}  pnl=${s['pnl']:>10,.2f}  win={wr:.0f}%")
    print()

    if seed_scores and results.get("by_strategy"):
        print("Seeding strategy scores from backtest results:")
        seed_strategy_scores(results)
        print()

    return results


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Backtest the trading strategy")
    parser.add_argument("--days",    type=int,   default=30,      help="Days of history to use")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital")
    parser.add_argument("--seed-scores", action="store_true",     help="Pre-populate strategy scores from backtest")
    args = parser.parse_args()

    run_backtest(days=args.days, capital=args.capital, seed_scores=args.seed_scores)
