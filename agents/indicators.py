"""
agents/indicators.py -- Technical indicators computed from OHLCV bar data.
All functions accept a list of floats (closes, highs, lows, volumes) and
return a single float or dict. Stateless, pure functions -- no I/O.
"""

def ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def rsi(closes: list, period: int = 14) -> float:
    """Relative Strength Index. Returns float 0-100 or None if insufficient data."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, histogram. Returns dict or None."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len-i)] - ema_slow[-(min_len-i)] for i in range(min_len)]
    if len(macd_line) < signal:
        return None
    sig_line = ema(macd_line, signal)
    if not sig_line:
        return None
    hist = macd_line[-1] - sig_line[-1]
    return {"macd": macd_line[-1], "signal": sig_line[-1], "histogram": hist}

def bollinger(closes: list, period: int = 20, num_std: float = 2.0) -> dict:
    """Bollinger Bands. Returns upper, middle, lower, %B, bandwidth."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    price = closes[-1]
    pct_b = (price - lower) / (upper - lower) if upper != lower else 0.5
    bandwidth = (upper - lower) / mid if mid != 0 else 0
    return {"upper": upper, "middle": mid, "lower": lower, "pct_b": pct_b, "bandwidth": bandwidth}

def ema_cross(closes: list, fast: int = 9, slow: int = 21) -> dict:
    """EMA crossover signal. Returns fast_ema, slow_ema, cross direction."""
    if len(closes) < slow + 2:
        return None
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    if len(fast_ema) < 2 or len(slow_ema) < 2:
        return None
    cross = "bullish" if fast_ema[-2] < slow_ema[-2] and fast_ema[-1] > slow_ema[-1] else \
            "bearish" if fast_ema[-2] > slow_ema[-2] and fast_ema[-1] < slow_ema[-1] else "none"
    return {"fast": fast_ema[-1], "slow": slow_ema[-1], "cross": cross}

def atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range. Used for stop sizing."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val

def vwap(highs: list, lows: list, closes: list, volumes: list) -> float:
    """Volume Weighted Average Price for the current session."""
    if not volumes or sum(volumes) == 0:
        return None
    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    cumtp_v = sum(t * v for t, v in zip(typical, volumes))
    cumv    = sum(volumes)
    return cumtp_v / cumv

def compute_all(ohlcv: dict) -> dict:
    """
    Run all indicators on a symbol's OHLCV dict.
    ohlcv: {"closes": [...], "highs": [...], "lows": [...], "volumes": [...]}
    Returns dict of indicator results.
    """
    c = ohlcv.get("closes", [])
    h = ohlcv.get("highs",  [])
    l = ohlcv.get("lows",   [])
    v = ohlcv.get("volumes",[])
    return {
        "rsi":       rsi(c),
        "macd":      macd(c),
        "bollinger": bollinger(c),
        "ema_cross": ema_cross(c),
        "atr":       atr(h, l, c),
        "vwap":      vwap(h, l, c, v),
    }
