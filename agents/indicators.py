"""
agents/indicators.py -- Technical indicators computed from OHLCV bar data.
All functions accept a list of floats (closes, highs, lows, volumes) and
return a single float or dict. Stateless, pure functions -- no I/O.

Indicator registry: decorate with @register_indicator("name", args=[...])
and compute_all() will auto-discover. New indicators require zero edits
to existing code -- just add the function + decorator.
"""

# ── registry ────────────────────────────────────────────────────
_REGISTRY = {}  # name -> {"fn": callable, "args": ["closes"] or ["highs","lows","closes",...]}


def register_indicator(name, args=None):
    """Decorator to register a top-level indicator function.
    args: list of OHLCV keys the function needs, e.g. ["closes"] or ["highs","lows","closes"].
    Defaults to ["closes"] if omitted.
    """
    def decorator(fn):
        _REGISTRY[name] = {"fn": fn, "args": args or ["closes"]}
        return fn
    return decorator


def get_registry():
    """Return a copy of the indicator registry for introspection."""
    return dict(_REGISTRY)


# ── helpers (not registered) ────────────────────────────────────

def ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


# ── registered indicators ──────────────────────────────────────

@register_indicator("rsi")
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


@register_indicator("macd")
def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, histogram. Returns dict or None."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    if min_len == 0:
        return None
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(-min_len, 0)]
    if len(macd_line) < signal:
        return None
    sig_line = ema(macd_line, signal)
    if not sig_line:
        return None
    hist = macd_line[-1] - sig_line[-1]
    return {"macd": macd_line[-1], "signal": sig_line[-1], "histogram": hist}


@register_indicator("bollinger")
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


@register_indicator("ema_cross")
def ema_cross(closes: list, fast: int = 9, slow: int = 21) -> dict:
    """EMA crossover signal. Returns fast_ema, slow_ema, cross direction."""
    if len(closes) < slow + 2:
        return None
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    if len(fast_ema) < 2 or len(slow_ema) < 2:
        return None
    cross = "bullish" if fast_ema[-2] < slow_ema[-2] and fast_ema[-1] > slow_ema[-1] else \
            "bearish" if fast_ema[-2] > slow_ema[-2] and fast_ema[-1] < slow_ema[-1] else "none"
    return {"fast": fast_ema[-1], "slow": slow_ema[-1], "cross": cross}


@register_indicator("atr", args=["highs", "lows", "closes"])
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


@register_indicator("vwap", args=["highs", "lows", "closes", "volumes"])
def vwap(highs: list, lows: list, closes: list, volumes: list) -> float:
    """Volume Weighted Average Price for the current session."""
    if not volumes or sum(volumes) == 0:
        return None
    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    cumtp_v = sum(t * v for t, v in zip(typical, volumes))
    cumv    = sum(volumes)
    return cumtp_v / cumv


# ── dispatch ────────────────────────────────────────────────────

def compute_all(ohlcv: dict) -> dict:
    """
    Run all registered indicators on a symbol's OHLCV dict.
    ohlcv: {"closes": [...], "highs": [...], "lows": [...], "volumes": [...]}
    Returns dict of indicator results keyed by registered name.
    """
    result = {}
    for name, meta in _REGISTRY.items():
        try:
            fn_args = [ohlcv.get(k, []) for k in meta["args"]]
            result[name] = meta["fn"](*fn_args)
        except Exception:
            result[name] = None
    return result
