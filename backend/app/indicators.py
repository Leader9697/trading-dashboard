"""Plain-Python indicators over lists of floats. None marks the warm-up bars."""

from __future__ import annotations


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n, len(values)):
        if i > n:
            d = values[i] - values[i - 1]
            avg_g = (avg_g * (n - 1) + max(d, 0)) / n
            avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def macd_hist(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float | None]:
    f, s = ema(values, fast), ema(values, slow)
    line = [a - b for a, b in zip(f, s) if a is not None and b is not None]
    sig = ema(line, signal)
    hist = [None if g is None else m - g for m, g in zip(line, sig)]
    return [None] * (len(values) - len(hist)) + hist


def atr(candles: list[dict], n: int = 14) -> list[float | None]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out: list[float | None] = [None] * len(candles)
    if len(trs) < n:
        return out
    prev = sum(trs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(trs)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def pivots(candles: list[dict], width: int = 3, lookback: int = 150) -> tuple[list[float], list[float]]:
    """Swing highs and lows: bars whose high/low beats `width` bars on each side."""
    data = candles[-lookback:]
    highs, lows = [], []
    for i in range(width, len(data) - width):
        window = data[i - width : i + width + 1]
        if data[i]["high"] == max(c["high"] for c in window):
            highs.append(data[i]["high"])
        if data[i]["low"] == min(c["low"] for c in window):
            lows.append(data[i]["low"])
    return highs, lows
