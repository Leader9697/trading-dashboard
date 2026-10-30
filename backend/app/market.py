"""Crypto market data: Binance public endpoints (no API key) or an offline demo feed."""

from __future__ import annotations

import hashlib
import math
import random

import httpx

from .config import Settings

SPOT_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
FUTURES = "https://fapi.binance.com"
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
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
    return _klines_request({"symbol": symbol, "interval": interval, "limit": limit})


def binance_klines_paged(symbol: str, interval: str, limit: int, end_ms: int | None = None) -> list[dict]:
    """Binance caps one request at 1000 bars; walk backwards with endTime for more."""
    if limit <= 1000 and end_ms is None:
        return binance_klines(symbol, interval, limit)
    out: list[dict] = []
    while len(out) < limit:
        batch = min(1000, limit - len(out))
        params = {"symbol": symbol, "interval": interval, "limit": batch}
        if end_ms is not None:
            params["endTime"] = end_ms
        rows = _klines_request(params)
        if not rows:
            break
        out = rows + out
        end_ms = rows[0]["time"] * 1000 - 1
        if len(rows) < batch:
            break
    return out


def _klines_request(params: dict) -> list[dict]:
    last_error: Exception | None = None
    for host in SPOT_HOSTS:
        try:
            resp = httpx.get(f"{host}/api/v3/klines", params=params, timeout=_TIMEOUT)
            if resp.status_code == 400:
                raise MarketError(f"Pair {params['symbol']} tidak dikenal Binance")
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


def get_klines(settings: Settings, symbol: str, interval: str, limit: int = 300, end: int | None = None) -> list[dict]:
    """`end` (unix seconds) returns the bars that open before it, for loading older history."""
    if interval not in INTERVAL_SECONDS:
        raise MarketError(f"Interval {interval} tidak didukung")
    if settings.data_mode == "demo":
        rows = demo_klines(symbol, interval, limit)
        if end is None:
            return rows
        shift = end - INTERVAL_SECONDS[interval] - rows[-1]["time"]
        return [{**c, "time": c["time"] + shift} for c in rows] if end > 1793318400 - 3 * 365 * 86400 else []
    return binance_klines_paged(symbol, interval, limit, end * 1000 - 1 if end else None)


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
        return {"funding_pct": 0.01, "oi_change_24h_pct": 2.5, "long_short_ratio": 1.4,
                "ls_rank_90d": 0.5, "taker_4h": 1.05}
    out: dict = {"funding_pct": None, "oi_change_24h_pct": None, "long_short_ratio": None,
                 "ls_rank_90d": None, "taker_4h": None}
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
    # Where today's retail long/short sits in its ~83-day range (0 = most short, 1 = most long).
    hist = _get_json(f"{FUTURES}/futures/data/globalLongShortAccountRatio",
                     {"symbol": symbol, "period": "4h", "limit": 500})
    if isinstance(hist, list) and len(hist) >= 60:
        vals = [float(h["longShortRatio"]) for h in hist]
        now = float(ls[-1]["longShortRatio"]) if isinstance(ls, list) and ls else vals[-1]
        out["ls_rank_90d"] = round(sum(1 for v in vals if v <= now) / len(vals), 2)
    taker = _get_json(f"{FUTURES}/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": 48})
    if isinstance(taker, list) and taker:
        out["taker_4h"] = round(sum(float(t["buySellRatio"]) for t in taker) / len(taker), 3)
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
