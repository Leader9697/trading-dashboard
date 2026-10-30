"""Rule-based trade setup: bias from multi-timeframe trend, levels from structure and ATR.

The numbers here are deterministic so an LLM never has to invent prices; the AI
debate (written separately into Edge Config) only argues over these facts.
"""

from __future__ import annotations

from .indicators import atr, ema, macd_hist, pivots, rsi

MODES = {
    # higher timeframe sets the trend, entry timeframe times the trade
    "swing": {"htf": "1d", "ltf": "4h", "risk_pct": 1.0},
    "scalp": {"htf": "1h", "ltf": "15m", "risk_pct": 0.5},
}

RATINGS = [(60, "Buy"), (25, "Overweight"), (-25, "Hold"), (-60, "Underweight")]


def _last(values: list[float | None]) -> float | None:
    return values[-1] if values else None


def frame_stats(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    vol = [c["volume"] for c in candles[-21:-1]]
    stats = {
        "close": closes[-1],
        "ema20": _last(e20),
        "ema50": _last(e50),
        "ema200": _last(e200),
        "rsi": _last(rsi(closes)),
        "macd_hist": _last(macd_hist(closes)),
        "atr": _last(atr(candles)),
        "vol_ratio": candles[-1]["volume"] / (sum(vol) / len(vol)) if vol and sum(vol) else None,
    }
    stats["trend"] = trend_score(stats)
    return stats


def trend_score(s: dict) -> float:
    """-1..1: stacked EMAs and price position. EMA200 counts only when available."""
    c, e20, e50, e200 = s["close"], s["ema20"], s["ema50"], s["ema200"]
    if e20 is None or e50 is None:
        return 0.0
    score = 0.0
    score += 0.35 if c > e20 else -0.35
    score += 0.35 if e20 > e50 else -0.35
    if e200 is not None:
        score += 0.3 if e50 > e200 else -0.3
    return round(score, 2)


def trend_label(score: float) -> str:
    if score >= 0.65:
        return "naik kuat"
    if score > 0:
        return "cenderung naik"
    if score <= -0.65:
        return "turun kuat"
    if score < 0:
        return "cenderung turun"
    return "datar"


def bias_score(htf: dict, ltf: dict, deriv: dict, fng: dict | None) -> tuple[int, list[str], list[str]]:
    """Score -100..100 plus the bullish and bearish facts behind it."""
    bull, bear = [], []
    score = 60 * htf["trend"] + 40 * ltf["trend"]
    (bull if htf["trend"] > 0 else bear).append(f"Tren TF besar {trend_label(htf['trend'])}")
    (bull if ltf["trend"] > 0 else bear).append(f"Tren TF entry {trend_label(ltf['trend'])}")

    r = ltf["rsi"]
    if r is not None:
        if r >= 72:
            score -= 12
            bear.append(f"RSI TF entry {r:.0f}, jenuh beli")
        elif r <= 28:
            score += 12
            bull.append(f"RSI TF entry {r:.0f}, jenuh jual")
    m = ltf["macd_hist"]
    if m is not None:
        score += 8 if m > 0 else -8
        (bull if m > 0 else bear).append("Histogram MACD " + ("positif" if m > 0 else "negatif"))

    f = deriv.get("funding_pct")
    if f is not None:
        if f >= 0.05:
            score -= 10
            bear.append(f"Funding {f:.3f}%: long ramai, rawan long squeeze")
        elif f <= -0.02:
            score += 10
            bull.append(f"Funding {f:.3f}%: short ramai, rawan short squeeze")
    ls = deriv.get("long_short_ratio")
    if ls is not None and ls >= 2.5:
        score -= 5
        bear.append(f"Rasio long/short {ls}: posisi retail berat di long")
    if fng:
        if fng["value"] >= 75:
            score -= 5
            bear.append(f"Fear & Greed {fng['value']} (extreme greed)")
        elif fng["value"] <= 25:
            score += 5
            bull.append(f"Fear & Greed {fng['value']} (extreme fear), contrarian bullish")
    return max(-100, min(100, round(score))), bull, bear


def rating(score: int) -> str:
    for floor, label in RATINGS:
        if score >= floor:
            return label
    return "Sell"


def _round(x: float, ref: float) -> float:
    digits = 2 if ref >= 100 else 4 if ref >= 1 else 6
    return round(x, digits)


def build_levels(direction: str, price: float, ltf: dict, highs: list[float], lows: list[float]) -> dict:
    a = ltf["atr"] or price * 0.01
    e20 = ltf["ema20"] or price
    if direction == "long":
        supports = sorted([l for l in lows if l < price], reverse=True)
        anchor = max([x for x in (supports[0] if supports else None, e20) if x is not None and x <= price], default=price)
        entry = anchor + 0.15 * a
        zone = (anchor - 0.1 * a, min(price, anchor + 0.4 * a))
        structure = max([s for s in supports if s < entry - 0.5 * a], default=entry - 1.5 * a)
        sl = min(structure - 0.3 * a, entry - 0.8 * a)
        sl = max(sl, entry - 2.5 * a)
        risk = entry - sl
        tp1, tp2 = entry + 2 * risk, entry + 3 * risk
        hurdles = sorted(h for h in highs if entry < h < tp1)
        invalid = f"Close TF entry di bawah {_round(sl, price)}"
    else:
        resistances = sorted(h for h in highs if h > price)
        anchor = min([x for x in (resistances[0] if resistances else None, e20) if x is not None and x >= price], default=price)
        entry = anchor - 0.15 * a
        zone = (max(price, anchor - 0.4 * a), anchor + 0.1 * a)
        structure = min([r for r in resistances if r > entry + 0.5 * a], default=entry + 1.5 * a)
        sl = max(structure + 0.3 * a, entry + 0.8 * a)
        sl = min(sl, entry + 2.5 * a)
        risk = sl - entry
        tp1, tp2 = entry - 2 * risk, entry - 3 * risk
        hurdles = sorted((l for l in lows if tp1 < l < entry), reverse=True)
        invalid = f"Close TF entry di atas {_round(sl, price)}"
    return {
        "entry": _round(entry, price),
        "entry_zone": [_round(zone[0], price), _round(zone[1], price)],
        "stop_loss": _round(sl, price),
        "tp1": _round(tp1, price),
        "tp2": _round(tp2, price),
        "rr_tp1": 2.0,
        "rr_tp2": 3.0,
        "sl_atr": round(risk / a, 2),
        "hurdle": _round(hurdles[0], price) if hurdles else None,
        "invalidation": invalid,
    }


def build_setup(symbol: str, mode: str, htf_candles: list[dict], ltf_candles: list[dict],
                deriv: dict, fng: dict | None) -> dict:
    cfg = MODES[mode]
    htf, ltf = frame_stats(htf_candles), frame_stats(ltf_candles)
    price = ltf["close"]
    score, bull, bear = bias_score(htf, ltf, deriv, fng)
    direction = "long" if score >= 25 else "short" if score <= -25 else "wait"
    highs, lows = pivots(ltf_candles)

    # Even while waiting, show the plan for the side the score leans to.
    plan_side = direction if direction != "wait" else ("long" if score >= 0 else "short")
    levels = build_levels(plan_side, price, ltf, highs, lows)

    aligned = (htf["trend"] > 0) == (ltf["trend"] > 0) and htf["trend"] != 0
    checklist = [
        {"label": "Tren TF besar searah setup", "ok": (htf["trend"] > 0) == (plan_side == "long") and htf["trend"] != 0},
        {"label": "TF entry searah TF besar", "ok": aligned},
        {"label": "RSI tidak ekstrem", "ok": ltf["rsi"] is not None and 30 < ltf["rsi"] < 70},
        {"label": "Funding tidak terlalu ramai", "ok": deriv.get("funding_pct") is None or abs(deriv["funding_pct"]) < 0.05},
        {"label": "Volume di atas rata-rata 20 bar", "ok": (ltf["vol_ratio"] or 0) >= 1.0},
        {"label": "Tidak ada resisten/support besar sebelum TP1", "ok": levels["hurdle"] is None},
    ]
    passed = sum(1 for c in checklist if c["ok"])
    risk_pct = cfg["risk_pct"] if passed >= 4 else cfg["risk_pct"] / 2

    return {
        "symbol": symbol,
        "mode": mode,
        "timeframes": {"trend": cfg["htf"], "entry": cfg["ltf"]},
        "price": _round(price, price),
        "as_of": ltf_candles[-1]["time"],
        "direction": direction,
        "plan_side": plan_side,
        "score": score,
        "rating": rating(score),
        "levels": levels,
        "risk_pct": risk_pct,
        "checklist": checklist,
        "bull_points": bull,
        "bear_points": bear,
        "frames": {
            "trend": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in htf.items()},
            "entry": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ltf.items()},
        },
        "derivatives": deriv,
        "fear_greed": fng,
    }


def compact(setup: dict) -> dict:
    """Minimal per-coin facts for the AI debate: small enough to keep token use low."""
    lv, fr = setup["levels"], setup["frames"]
    d = setup["derivatives"]
    return {
        "s": setup["symbol"],
        "px": setup["price"],
        "dir": setup["direction"],
        "score": setup["score"],
        "trend": [trend_label(fr["trend"]["trend"]), trend_label(fr["entry"]["trend"])],
        "rsi": [round(fr["trend"]["rsi"] or 0), round(fr["entry"]["rsi"] or 0)],
        "atr_pct": round((fr["entry"]["atr"] or 0) / setup["price"] * 100, 2),
        "plan": [setup["plan_side"], lv["entry_zone"], lv["stop_loss"], lv["tp1"], lv["tp2"]],
        "hurdle": lv["hurdle"],
        "fund": d.get("funding_pct"),
        "oi24": d.get("oi_change_24h_pct"),
        "ls": d.get("long_short_ratio"),
        "ok": sum(1 for c in setup["checklist"] if c["ok"]),
    }
