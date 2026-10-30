"""Gateway API: one place the dashboard asks for prices, decisions and research."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .providers import ProviderError, get_candles
from .signals import dsa_decision, indicator_lines, rule_decision

app = FastAPI(title="Trading Dashboard Gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 20 or not all(ch.isalnum() or ch in ".-^=" for ch in symbol):
        raise HTTPException(status_code=422, detail="Ticker tidak valid")
    return symbol


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "watchlist": list(settings.watchlist),
        "sources": {
            "candles": settings.candle_source,
            "decision": "daily_stock_analysis" if settings.decision_source == "dsa" else "rules",
            "research": "dexter" if settings.dexter_url else None,
        },
    }


@app.get("/api/candles/{symbol}")
def candles(
    symbol: str,
    days: int = Query(180, ge=60, le=500),
    settings: Settings = Depends(get_settings),
) -> dict:
    symbol = _symbol(symbol)
    try:
        rows, name, source = get_candles(settings, symbol, days)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"symbol": symbol, "name": name, "source": source, "candles": rows, "indicators": indicator_lines(rows)}


@app.get("/api/decision/{symbol}")
def decision(symbol: str, settings: Settings = Depends(get_settings)) -> dict:
    symbol = _symbol(symbol)
    try:
        if settings.decision_source == "dsa":
            result = dsa_decision(settings, symbol)
            source = "daily_stock_analysis"
        else:
            rows, _, _ = get_candles(settings, symbol, 180)
            result = rule_decision(rows)
            source = "rules"
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"symbol": symbol, "source": source, **result}


class ResearchRequest(BaseModel):
    symbol: str
    question: str = Field(min_length=3, max_length=2000)


@app.post("/api/research")
def research(req: ResearchRequest, settings: Settings = Depends(get_settings)) -> dict:
    symbol = _symbol(req.symbol)
    if not settings.dexter_url:
        raise HTTPException(
            status_code=503,
            detail="Riset mendalam belum aktif. Set DEXTER_URL ke bridge dexter (Tahap 3).",
        )
    try:
        resp = httpx.post(
            f"{settings.dexter_url}/research",
            json={"query": f"[{symbol}] {req.question}"},
            timeout=300,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"dexter gagal: {exc}") from exc
    return {"symbol": symbol, **resp.json()}


# Serve the built frontend (frontend/dist) from the same port when it exists.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
