"""Gateway API: crypto prices, trade setups, a compact snapshot and the latest AI debate."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .indicators import ema
from .market import MarketError, derivatives, fear_greed, get_klines
from .setup import MODES, build_setup, compact

app = FastAPI(title="Crypto Trade Setup Gateway", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: dict[tuple, tuple[float, object]] = {}
CACHE_SECONDS = 60


def _cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


def _symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not (3 <= len(symbol) <= 20 and symbol.isalnum()):
        raise HTTPException(status_code=422, detail="Pair tidak valid, contoh: BTCUSDT")
    return symbol


def _mode(mode: str) -> str:
    if mode not in MODES:
        raise HTTPException(status_code=422, detail="Mode harus swing atau scalp")
    return mode


def _setup(settings: Settings, symbol: str, mode: str) -> dict:
    def compute():
        cfg = MODES[mode]
        htf = get_klines(settings, symbol, cfg["htf"], 300)
        ltf = get_klines(settings, symbol, cfg["ltf"], 300)
        return build_setup(symbol, mode, htf, ltf, derivatives(settings, symbol), fear_greed(settings))

    return _cached(("setup", settings.data_mode, symbol, mode), compute)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "watchlist": list(settings.watchlist),
        "data_mode": settings.data_mode,
        "modes": {k: {"trend": v["htf"], "entry": v["ltf"]} for k, v in MODES.items()},
        "debate_enabled": settings.edge_config_item_url is not None,
    }


@app.get("/api/candles/{symbol}")
def candles(
    symbol: str,
    interval: str = Query("4h"),
    limit: int = Query(300, ge=50, le=1000),
    settings: Settings = Depends(get_settings),
) -> dict:
    symbol = _symbol(symbol)
    try:
        rows = _cached(("k", settings.data_mode, symbol, interval, limit),
                       lambda: get_klines(settings, symbol, interval, limit))
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    closes = [c["close"] for c in rows]
    lines = {
        f"ema{n}": [{"time": c["time"], "value": round(v, 6)} for c, v in zip(rows, ema(closes, n)) if v is not None]
        for n in (20, 50, 200)
    }
    return {"symbol": symbol, "interval": interval, "candles": rows, "indicators": lines}


@app.get("/api/setup/{symbol}")
def setup(symbol: str, mode: str = "swing", settings: Settings = Depends(get_settings)) -> dict:
    symbol, mode = _symbol(symbol), _mode(mode)
    try:
        return _setup(settings, symbol, mode)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/snapshot")
def snapshot(mode: str = "swing", settings: Settings = Depends(get_settings)) -> dict:
    """Every watchlist coin in one small payload: the input for the scheduled AI debate."""
    mode = _mode(mode)

    def one(sym: str):
        try:
            return compact(_setup(settings, sym, mode))
        except MarketError as exc:
            return {"s": sym, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        coins = list(pool.map(one, settings.watchlist))
    return {"t": int(time.time()), "mode": mode, "fng": fear_greed(settings), "coins": coins}


@app.get("/api/debate")
def debate(settings: Settings = Depends(get_settings)) -> dict:
    target = settings.edge_config_item_url
    if target is None:
        return {"available": False}
    base, token = target
    try:
        resp = httpx.get(f"{base}/debate", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Edge Config gagal: {exc}") from exc
    if resp.status_code == 404:
        return {"available": False}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Edge Config HTTP {resp.status_code}")
    return {"available": True, **resp.json()}


# Serve the built frontend (frontend/dist) from the same port when it exists.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
