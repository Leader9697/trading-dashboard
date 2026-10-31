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
from . import journal as journal_mod
from .backtest import BARS, summarize
from .indicators import ema
from .market import INTERVAL_SECONDS, MarketError, derivatives, fear_greed, get_klines
from . import backtest_v3, notify, portfolio, research
from .backtest import stats as bt_stats
from .setup import MODES, RULES_V2, btc_regime, build_setup, build_signal, compact

app = FastAPI(title="Crypto Trade Setup Gateway", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: dict[tuple, tuple[float, object]] = {}
CACHE_SECONDS = 60


def _cached(key: tuple, fn, ttl: int = CACHE_SECONDS):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
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


def _closed(rows: list[dict], interval: str, now: float) -> list[dict]:
    return [c for c in rows if c["time"] + INTERVAL_SECONDS[interval] <= now]


def _setup(settings: Settings, symbol: str, mode: str) -> dict:
    def compute():
        cfg = MODES[mode]
        now = time.time()
        # Binance returns the still-forming candle last. The backtest only ever sees closed
        # candles, so the setup and signal are built from closed candles too.
        raw_ltf = get_klines(settings, symbol, cfg["ltf"], 301)
        htf = _closed(get_klines(settings, symbol, cfg["htf"], 301), cfg["htf"], now)
        ltf = _closed(raw_ltf, cfg["ltf"], now)
        btc = htf if symbol == "BTCUSDT" else _closed(get_klines(settings, "BTCUSDT", cfg["htf"], 301), cfg["htf"], now)
        s = build_setup(symbol, mode, htf, ltf, derivatives(settings, symbol), fear_greed(settings))
        s["signal"] = build_signal(s, ltf, btc_regime(btc)[1][-1])
        s["last_price"] = raw_ltf[-1]["close"]
        return s

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
    end: int | None = Query(None, gt=0),
    settings: Settings = Depends(get_settings),
) -> dict:
    symbol = _symbol(symbol)
    try:
        rows = _cached(("k", settings.data_mode, symbol, interval, limit, end),
                       lambda: get_klines(settings, symbol, interval, limit, end),
                       ttl=86400 if end else CACHE_SECONDS)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    closes = [c["close"] for c in rows]
    lines = {
        f"ema{n}": [{"time": c["time"], "value": round(v, 6)} for c, v in zip(rows, ema(closes, n)) if v is not None]
        for n in (20, 50, 200)
    }
    return {"symbol": symbol, "interval": interval, "candles": rows, "indicators": lines}


@app.get("/api/notify")
def notify_signals(test: bool = False, settings: Settings = Depends(get_settings)) -> dict:
    """Called by the scheduled job after each 4h close; `test=true` sends a hello message."""
    saved = portfolio.load_saved() or {}
    return notify.run(settings, lambda sym: _setup(settings, sym, "swing"), test=test,
                      extra=saved.get("recommended", []), max_pos=saved.get("settings", {}).get("max_pos"))


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


@app.get("/api/backtest/{symbol}")
def backtest(symbol: str, mode: str = "swing", rules: str = "v1", settings: Settings = Depends(get_settings)) -> dict:
    """Walk-forward test of the setup rules on past candles. Heavy, so cached for 30 minutes."""
    symbol, mode = _symbol(symbol), _mode(mode)
    if rules not in ("v1", "v2"):
        raise HTTPException(status_code=422, detail="rules harus v1 atau v2")

    def compute():
        cfg = MODES[mode]
        ltf_bars = BARS[mode]
        htf_bars = ltf_bars * INTERVAL_SECONDS[cfg["ltf"]] // INTERVAL_SECONDS[cfg["htf"]] + 320
        ltf = get_klines(settings, symbol, cfg["ltf"], ltf_bars)
        htf = get_klines(settings, symbol, cfg["htf"], htf_bars)
        if settings.data_mode == "binance":
            ltf = ltf[:-1]  # the newest bar is still open
        if rules == "v1":
            out = summarize(symbol, mode, htf, ltf)
            trades = [{"t": p["time"], "r": p["value"]} for p in out["equity"]]
            out["folds"] = research.folds(_deltas(trades), ltf[research.WARMUP]["time"], ltf[-1]["time"])
            return {**out, "rules": "v1"}
        btc = htf if symbol == "BTCUSDT" else get_klines(settings, "BTCUSDT", cfg["htf"], htf_bars)
        pre = research.precompute(symbol, mode, htf, ltf, btc_regime(btc))
        trades = research.simulate(pre, ltf, mode, RULES_V2)
        equity, curve = 0.0, []
        for t in trades:
            equity += t["r"]
            curve.append({"time": t["t"], "value": round(equity, 3)})
        return {
            "symbol": symbol, "mode": mode, "rules": "v2",
            "period": {"from": ltf[research.WARMUP]["time"], "to": ltf[-1]["time"], "bars": len(ltf)},
            "signals": len(trades), "missed": 0,
            "exit_split": bt_stats([{**t, "status": t["status"], "r_split": t["r"]} for t in trades], "r_split"),
            "equity": curve,
            "folds": research.folds(trades, ltf[research.WARMUP]["time"], ltf[-1]["time"]),
        }

    try:
        return _cached(("bt", settings.data_mode, symbol, mode, rules), compute, ttl=1800)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/backtest/{symbol}/trades")
def backtest_trades(symbol: str) -> dict:
    """Past trades of the live v3 rules for one coin, precomputed by app.backtest_v3."""
    symbol = _symbol(symbol)
    saved = backtest_v3.load_saved()
    if not saved or symbol not in saved["coins"]:
        return {"symbol": symbol, "available": False}
    trades = saved["coins"][symbol]
    fee = saved.get("rules", {}).get("fee_round_trip", backtest_v3.FEE_ROUND_TRIP)
    for t in trades:
        t.setdefault("exit", round(backtest_v3.exit_price(t, fee), backtest_v3._digits(t["entry"])))
    return {
        "symbol": symbol, "available": True, "generated": saved["generated"], "rules": saved["rules"],
        "stats": bt_stats(trades, "r"),
        "below_min": sum(1 for t in trades if t["below_min"]),
        "trades": trades,
    }


@app.get("/api/portfolio")
def portfolio_view(equity: float = Query(portfolio.EQUITY, gt=0, le=1_000_000),
                   settings: Settings = Depends(get_settings)) -> dict:
    """The portfolio engine's backtest and coin list, plus today's signal on each listed coin."""
    saved = portfolio.load_saved()
    if not saved:
        return {"available": False}

    def one(sym: str) -> dict:
        try:
            return portfolio.live_plan(sym, _setup(settings, sym, "swing")["signal"], saved, equity)
        except MarketError as exc:
            return {"symbol": sym, "active": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        live = list(pool.map(one, saved["recommended"]))
    live.sort(key=lambda r: (not r["active"], -r.get("checks_ok", 0)))
    return {"available": True, **saved, "live": live, "live_equity": equity}


def _deltas(curve: list[dict]) -> list[dict]:
    """Equity-curve points back to per-trade results."""
    prev, out = 0.0, []
    for p in curve:
        out.append({"t": p["t"], "r": p["r"] - prev})
        prev = p["r"]
    return out


def _edge_item(settings: Settings, key: str):
    """Read one Edge Config item. Returns None when unconfigured or missing."""
    target = settings.edge_config_item_url
    if target is None:
        return None
    base, token = target
    try:
        resp = httpx.get(f"{base}/{key}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Edge Config gagal: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Edge Config HTTP {resp.status_code}")
    return resp.json()


@app.get("/api/debate")
def debate(settings: Settings = Depends(get_settings)) -> dict:
    item = _edge_item(settings, "debate")
    return {"available": False} if item is None else {"available": True, **item}


@app.get("/api/journal")
def journal(settings: Settings = Depends(get_settings)) -> dict:
    """AI debate calls replayed against real candles, plus ready rows for the next append."""
    rows = _edge_item(settings, "journal") or []
    interval = MODES[journal_mod.MODE]["ltf"]
    results = []
    for sym in sorted({r[1] for r in rows}):
        try:
            candles = _cached(("k", settings.data_mode, sym, interval, 300),
                              lambda: get_klines(settings, sym, interval, 300))
        except MarketError:
            candles = []
        results += [journal_mod.evaluate(r, candles) for r in rows if r[1] == sym]
    results.sort(key=lambda r: r["ts"])
    busy = {r["symbol"] for r in results if r["status"] in journal_mod.OPEN_STATES}

    candidates = {}
    now = int(time.time())
    for sym in settings.watchlist:
        if sym in busy:
            continue
        try:
            st = _setup(settings, sym, journal_mod.MODE)
        except MarketError:
            continue
        lv = st["levels"]
        candidates[sym] = [now, sym, "L" if st["plan_side"] == "long" else "S",
                           lv["entry"], lv["stop_loss"], lv["tp1"], lv["tp2"]]
    return {
        "enabled": settings.edge_config_item_url is not None,
        "max_rows": journal_mod.MAX_ROWS,
        "rows": rows,
        "results": results,
        "summary": journal_mod.summarize(results),
        "candidates": candidates,
    }


# Serve the built frontend (frontend/dist) from the same port when it exists.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
