"""Crypto market data: Binance public endpoints (no API key) or an offline demo feed."""

from __future__ import annotations

import hashlib
import math
import random

import httpx

from .config import Settings

SPOT_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
FUTURES = "https://fapi.binance.com"
INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_TIMEOUT = 15


class MarketError(RuntimeError):
    pass


def demo_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """Seeded random walk so the UI can be tried offline. Ends at a fixed time."""
    seed = int(hashlib.sha256(f"{symbol}{interval}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    step = INTERVAL_SECONDS[interval]
    vol = 0.004 * math.sqrt(step / 900)
    price = {"BTCUSDT": 68000.0, "ETHUSDT": 3400.0, "SOLUSDT": 160.0}.get(symbol, 1 + seed % 500)
    start = 1793318400 - step * limit
    out = []
    for i in range(limit):
        o = price
        c = max(0.0001, o * (1 + math.sin(i / 23) * vol / 3 + rng.gauss(0, vol)))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 3)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append({"time": start + i * step, "open": o, "high": h, "low": l, "close": c,
                    "volume": float(rng.randint(100, 5000))})
        price = c
    return out


def binance_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    last_error: Exception | None = None
    for host in SPOT_HOSTS:
        try:
            resp = httpx.get(f"{host}/api/v3/klines",
                             params={"symbol": symbol, "interval": interval, "limit": limit},
                             timeout=_TIMEOUT)
            if resp.status_code == 400:
                raise MarketError(f"Pair {symbol} tidak dikenal Binance")
            resp.raise_for_status()
            return [
                {"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                for k in resp.json()
            ]
        except MarketError:
            raise
        except httpx.HTTPError as exc:
            last_error = exc
    raise MarketError(f"Binance tidak bisa dihubungi: {last_error}")


def get_klines(settings: Settings, symbol: str, interval: str, limit: int = 300) -> list[dict]:
    if interval not in INTERVAL_SECONDS:
        raise MarketError(f"Interval {interval} tidak didukung")
    if settings.data_mode == "demo":
        return demo_klines(symbol, interval, limit)
    return binance_klines(symbol, interval, limit)


def _get_json(url: str, params: dict) -> object | None:
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def derivatives(settings: Settings, symbol: str) -> dict:
    """Funding, 24h open-interest change and long/short ratio. Missing pieces stay None."""
    if settings.data_mode == "demo":
        return {"funding_pct": 0.01, "oi_change_24h_pct": 2.5, "long_short_ratio": 1.4}
    out: dict = {"funding_pct": None, "oi_change_24h_pct": None, "long_short_ratio": None}
    prem = _get_json(f"{FUTURES}/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(prem, dict) and "lastFundingRate" in prem:
        out["funding_pct"] = round(float(prem["lastFundingRate"]) * 100, 4)
    oi = _get_json(f"{FUTURES}/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25})
    if isinstance(oi, list) and len(oi) >= 2:
        first, last = float(oi[0]["sumOpenInterest"]), float(oi[-1]["sumOpenInterest"])
        if first:
            out["oi_change_24h_pct"] = round((last - first) / first * 100, 2)
    ls = _get_json(f"{FUTURES}/futures/data/globalLongShortAccountRatio",
                   {"symbol": symbol, "period": "1h", "limit": 1})
    if isinstance(ls, list) and ls:
        out["long_short_ratio"] = round(float(ls[-1]["longShortRatio"]), 2)
    return out


def fear_greed(settings: Settings) -> dict | None:
    if settings.data_mode == "demo":
        return {"value": 55, "label": "Greed"}
    data = _get_json("https://api.alternative.me/fng/", {"limit": 1})
    try:
        item = data["data"][0]  # type: ignore[index]
        return {"value": int(item["value"]), "label": item["value_classification"]}
    except (TypeError, KeyError, IndexError, ValueError):
        return None
