"""Price data sources: daily_stock_analysis, yfinance, or a deterministic demo feed."""

from __future__ import annotations

import hashlib
import math
import os
import random
from datetime import date, timedelta

import httpx

from .config import Settings


class ProviderError(RuntimeError):
    pass


def _candle(day: str, o: float, h: float, l: float, c: float, v: float | None) -> dict:
    return {
        "time": day,
        "open": round(o, 4),
        "high": round(h, 4),
        "low": round(l, 4),
        "close": round(c, 4),
        "volume": v,
    }


def demo_candles(symbol: str, days: int) -> list[dict]:
    """Seeded random walk so the UI can be tried without any API key or network."""
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    price = 50 + seed % 9000 / 10
    drift = (seed % 7 - 3) / 2000
    out: list[dict] = []
    day = date(2026, 10, 30) - timedelta(days=int(days * 1.45))
    while len(out) < days:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        wave = math.sin(len(out) / 17) / 150
        o = price
        c = max(1.0, o * (1 + drift + wave + rng.gauss(0, 0.015)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        out.append(_candle(day.isoformat(), o, h, l, c, float(rng.randint(2, 40) * 100_000)))
        price = c
    return out


def yfinance_candles(symbol: str, days: int) -> list[dict]:
    import yfinance as yf

    if os.getenv("VERCEL"):
        # Serverless filesystem is read-only except /tmp.
        yf.set_tz_cache_location("/tmp/yfinance-tz")
    period = "1y" if days <= 250 else "2y"
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ProviderError(f"yfinance tidak mengembalikan data untuk {symbol}")
    df = df.tail(days)
    return [
        _candle(idx.strftime("%Y-%m-%d"), r.Open, r.High, r.Low, r.Close, float(r.Volume))
        for idx, r in df.iterrows()
    ]


def dsa_candles(settings: Settings, symbol: str, days: int) -> tuple[list[dict], str | None]:
    url = f"{settings.dsa_url}/api/v1/stocks/{symbol}/history"
    try:
        resp = httpx.get(url, params={"period": "daily", "days": min(days, 365)}, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ProviderError(f"daily_stock_analysis gagal: {exc}") from exc
    body = resp.json()
    candles = [
        _candle(k["date"][:10], k["open"], k["high"], k["low"], k["close"], k.get("volume"))
        for k in body.get("data", [])
    ]
    return candles, body.get("stock_name")


def get_candles(settings: Settings, symbol: str, days: int) -> tuple[list[dict], str | None, str]:
    source = settings.candle_source
    if source == "demo":
        return demo_candles(symbol, days), None, "demo"
    if source == "dsa":
        candles, name = dsa_candles(settings, symbol, days)
        return candles, name, "daily_stock_analysis"
    try:
        return yfinance_candles(symbol, days), None, "yfinance"
    except ProviderError:
        raise
    except Exception as exc:  # yfinance raises many different types on network errors
        raise ProviderError(f"yfinance gagal untuk {symbol}: {exc}") from exc
