"""Turn candles (or a daily_stock_analysis report) into one decision for the dashboard."""

from __future__ import annotations

import httpx

from .config import Settings
from .providers import ProviderError

ACTION_LABELS = {
    "buy": "Beli",
    "add": "Tambah",
    "hold": "Tahan",
    "reduce": "Kurangi",
    "sell": "Jual",
    "watch": "Pantau",
    "avoid": "Hindari",
    "alert": "Waspada",
}


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
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


def indicator_lines(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    lines = {}
    for name, n in (("sma20", 20), ("sma50", 50)):
        lines[name] = [
            {"time": c["time"], "value": round(v, 4)}
            for c, v in zip(candles, sma(closes, n))
            if v is not None
        ]
    return lines


def rule_decision(candles: list[dict]) -> dict:
    """Simple trend-following rules used when no LLM backend is connected."""
    if len(candles) < 60:
        raise ProviderError("Butuh minimal 60 candle harian untuk sinyal berbasis aturan")
    closes = [c["close"] for c in candles]
    s20, s50, r14 = sma(closes, 20), sma(closes, 50), rsi(closes)
    close, m20, m50, r = closes[-1], s20[-1], s50[-1], r14[-1]

    markers = []
    for i in range(50, len(candles)):
        prev_up = s20[i - 1] > s50[i - 1]
        now_up = s20[i] > s50[i]
        if now_up != prev_up:
            action = "buy" if now_up else "sell"
            markers.append({"time": candles[i]["time"], "action": action, "label": ACTION_LABELS[action]})

    score = 50
    reasons, risks = [], []
    if close > m20:
        score += 15
        reasons.append(f"Harga {close:.2f} di atas MA20 ({m20:.2f})")
    else:
        score -= 15
        risks.append(f"Harga {close:.2f} di bawah MA20 ({m20:.2f})")
    if m20 > m50:
        score += 15
        reasons.append("MA20 di atas MA50: tren menengah naik")
    else:
        score -= 15
        risks.append("MA20 di bawah MA50: tren menengah turun")
    if r >= 70:
        score -= 10
        risks.append(f"RSI {r:.0f}: jenuh beli, rawan koreksi")
    elif r <= 30:
        score += 5
        reasons.append(f"RSI {r:.0f}: jenuh jual, potensi pantulan")
    else:
        reasons.append(f"RSI {r:.0f}: netral")
    score = max(0, min(100, score))

    if score >= 70:
        action, trend = "buy", "Naik"
    elif score <= 30:
        action, trend = "sell", "Turun"
    elif close > m50:
        action, trend = "hold", "Sideways cenderung naik"
    else:
        action, trend = "watch", "Sideways cenderung turun"

    recent_low = min(c["low"] for c in candles[-20:])
    recent_high = max(c["high"] for c in candles[-20:])
    stop = round(recent_low * 0.99, 2)
    take = round(close + 2 * (close - stop), 2) if close > stop else round(recent_high, 2)

    return {
        "action": action,
        "action_label": ACTION_LABELS[action],
        "score": score,
        "trend": trend,
        "summary": f"Sinyal teknikal sederhana (MA20/MA50 + RSI14), bukan analisis LLM. Skor {score}/100.",
        "reasons": reasons,
        "risks": risks,
        "strategy": {"entry": f"{close:.2f}", "stop_loss": f"{stop:.2f}", "take_profit": f"{take:.2f}"},
        "markers": markers,
        "as_of": candles[-1]["time"],
    }


def _lines(text: str | None) -> list[str]:
    if not text:
        return []
    parts = [p.strip(" -•\t") for p in str(text).replace("；", "\n").splitlines()]
    return [p for p in parts if p]


def dsa_decision(settings: Settings, symbol: str) -> dict:
    """Read the newest analysis report that daily_stock_analysis stored for this ticker."""
    base = f"{settings.dsa_url}/api/v1/history"
    try:
        listing = httpx.get(base, params={"stock_code": symbol, "limit": 30}, timeout=30)
        listing.raise_for_status()
        items = listing.json().get("items", [])
        if not items:
            raise ProviderError(
                f"Belum ada laporan untuk {symbol} di daily_stock_analysis. "
                "Jalankan analisis dulu (python main.py --stocks ...)."
            )
        latest = items[0]
        detail = httpx.get(f"{base}/{latest['id']}", timeout=30)
        detail.raise_for_status()
        report = detail.json()
    except httpx.HTTPError as exc:
        raise ProviderError(f"daily_stock_analysis gagal: {exc}") from exc

    summary = report.get("summary") or {}
    strategy = report.get("strategy") or {}
    action = summary.get("action") or latest.get("action") or "watch"
    markers = [
        {
            "time": (it.get("created_at") or "")[:10],
            "action": it.get("action") or "watch",
            "label": it.get("action_label") or ACTION_LABELS.get(it.get("action") or "watch", ""),
        }
        for it in reversed(items)
        if it.get("created_at")
    ]
    raw = (report.get("details") or {}).get("raw_result") or {}
    risks: list[str] = []
    if isinstance(raw, dict):
        intel = (raw.get("dashboard") or {}).get("intelligence") or {}
        alerts = intel.get("risk_alerts")
        risks = [str(a) for a in alerts] if isinstance(alerts, list) else _lines(alerts)
        risks = risks or _lines(raw.get("risk_warning"))
    return {
        "action": action,
        "action_label": summary.get("action_label") or ACTION_LABELS.get(action, action),
        "score": summary.get("sentiment_score"),
        "trend": summary.get("trend_prediction"),
        "summary": summary.get("analysis_summary") or "",
        "reasons": _lines(summary.get("operation_advice")),
        "risks": risks,
        "strategy": {
            "entry": strategy.get("ideal_buy"),
            "stop_loss": strategy.get("stop_loss"),
            "take_profit": strategy.get("take_profit"),
        },
        "markers": markers,
        "as_of": (report.get("meta") or {}).get("created_at"),
    }
