"""Rule-based trade setup: bias from multi-timeframe trend, levels from structure and ATR.

The numbers here are deterministic so an LLM never has to invent prices; the AI
debate (written separately into Edge Config) only argues over these facts.
"""

from __future__ import annotations

import math

from .indicators import atr, ema, macd_hist, pivots, rsi
from .market import INTERVAL_SECONDS

MODES = {
    # higher timeframe sets the trend, entry timeframe times the trade
    "swing": {"htf": "1d", "ltf": "4h", "risk_pct": 1.0},
    "scalp": {"htf": "1h", "ltf": "15m", "risk_pct": 0.5},
}

# Filters picked by app/research.py on 2024-03..2026-10 Binance data (10 coins, swing).
# They beat the v1 rules in all four walk-forward periods but were profitable in only
# two of them, so the signal is shown as research, not as an automatic signal.
RULES_V2 = {"entry": "market", "regime": True, "trend200": False, "wide_sl": False, "threshold": 40, "vol": True}
# v3 = v2 plus positioning filters picked by app/research_deriv.py. It met the auto criteria
# on 2024-03..2026-10 and stayed positive on unseen 2022..2024-03 data (+0.09R, PF 1.19).
RULES_V3_POSITIONING = {"oi": "up", "crowd": True, "taker": True}
# Entry and exits, from research_entry (2022–2026, 108 variants): a limit 0.25 ATR better
# than the signal close, valid 4 bars and skipped if unfilled, a stop 2 ATR from the close
# and one target at 3R beat entering at market with the structure stop (+0.19R vs +0.12R
# per trade, about the same number of trades, positive in every year).
# research_losses (2026-10-31) then found losers and winners look alike at the signal bar,
# but two changes held out of sample and in every year: skip signals whose candle closed
# against the trade (body under 0.1 ATR our way) and aim for 4R instead of 3R
# (+0.31R vs +0.19R per trade, 470 vs 576 trades, max drawdown 23R vs 32R).
LIMIT_ATR, STOP_ATR, TP_R, LIMIT_BARS = 0.25, 2.0, 4.0, 4
BODY_ATR = 0.1  # signal candle must close at least this far (in ATR) in the trade's direction
# research_regime (2026-10-31, PR #26): of 14 trending/ranging filters only this one held in
# 2023-24 and 2025-26: skip signals while BTC's last completed day is choppy (Choppiness
# Index 14 >= 61.8, the textbook cut). The trades it skips lost in every year 2022-2026.
CHOP_LEN, CHOP_MAX = 14, 61.8
MAX_HOLD_BARS = 30  # the backtest closes a trade still open after this many entry bars
MIN_NOTIONAL = {"BTCUSDT": 100, "ETHUSDT": 20}  # Binance USDT-M minimum order value, others 5 USDT
VALIDATION = {
    "swing": {"auto": False, "stage": "forward_test",
              "note": "Aturan v3 lolos uji historis 2022–2026 (rata-rata +0,17 s/d +0,41R per trade tiap tahun), tapi winrate sekitar 40%. Tahap forward test: pakai risiko kecil dulu."},
    "scalp": {"auto": False, "stage": "rejected", "note": "Semua 96 varian aturan rugi di data uji 15m. Jangan dipakai untuk entry."},
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
    # The v3 signal was tested on price alone (funding and Fear & Greed have no usable
    # history), so it uses the score without those nudges, exactly as the backtest does.
    price_score = bias_score(htf, ltf, {}, None)[0]
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
        "price_score": price_score,
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
        "px": setup.get("last_price", setup["price"]),
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
        **({"sig": [g["active"], g["side"], g["entry"], g["stop_loss"], g["take_profit"],
                    sum(1 for c in g["checks"] if c["ok"])]} if (g := setup.get("signal")) else {}),
    }


def btc_regime(btc_htf: list[dict]) -> tuple[list[int], list[int]]:
    """Per candle: +1 when BTC closes above a rising EMA50, -1 below a falling one, else 0."""
    closes = [c["close"] for c in btc_htf]
    e50 = ema(closes, 50)
    out = []
    for i, c in enumerate(closes):
        if i < 55 or e50[i] is None or e50[i - 5] is None:
            out.append(0)
        elif c > e50[i] and e50[i] > e50[i - 5]:
            out.append(1)
        elif c < e50[i] and e50[i] < e50[i - 5]:
            out.append(-1)
        else:
            out.append(0)
    return [c["time"] for c in btc_htf], out


def btc_chop(btc_htf: list[dict], n: int = CHOP_LEN) -> dict[int, float]:
    """Choppiness Index per daily candle, keyed by the candle's open time:
    100 * log10(sum of true ranges / (highest high - lowest low)) / log10(n)."""
    tr = [c["high"] - c["low"] if i == 0 else max(c["high"] - c["low"], abs(c["high"] - btc_htf[i - 1]["close"]),
                                                  abs(c["low"] - btc_htf[i - 1]["close"])) for i, c in enumerate(btc_htf)]
    out = {}
    for i in range(n, len(btc_htf)):
        w = btc_htf[i - n + 1:i + 1]
        rng = max(c["high"] for c in w) - min(c["low"] for c in w)
        if rng > 0:
            out[btc_htf[i]["time"]] = 100 * math.log10(sum(tr[i - n + 1:i + 1]) / rng) / math.log10(n)
    return out


def chop_at(chop: dict[int, float], close_time: int) -> float | None:
    """BTC's choppiness on the last day completed when a 4h bar closes at `close_time`."""
    return chop.get((close_time // 86400 - 1) * 86400)


def chop_ok(value: float | None) -> bool:
    return value is None or value < CHOP_MAX


def build_signal(setup: dict, ltf_candles: list[dict], btc_state: int, btc_chop_value: float | None = None) -> dict:
    """Rules v3 on top of a setup: v2 (stricter score, BTC regime, volatility, market entry)
    plus rising open interest, retail not crowded on our side and taker flow agreeing."""
    mode, price, score = setup["mode"], setup["price"], setup["price_score"]
    side = "long" if score >= 0 else "short"
    long = side == "long"
    atr_series = atr(ltf_candles)
    pct = [a / c["close"] for a, c in zip(atr_series, ltf_candles) if a is not None][-200:]
    vol_ok = len(pct) < 50 or pct[-1] >= sorted(pct)[len(pct) // 2]
    checks = [
        {"label": f"Skor tren minimal {RULES_V2['threshold']} (sekarang {score})",
         "ok": abs(score) >= RULES_V2["threshold"]},
        {"label": "Searah tren BTC " + MODES[mode]["htf"].upper(), "ok": btc_state == (1 if long else -1)},
        {"label": "Volatilitas di atas median 200 bar", "ok": vol_ok},
    ]
    d = setup["derivatives"]
    oi, rank, taker = d.get("oi_change_24h_pct"), d.get("ls_rank_90d"), d.get("taker_4h")
    checks += [
        {"label": "Open interest naik dalam 24 jam", "ok": oi is not None and oi > 0},
        {"label": "Retail tidak terlalu ramai di sisi yang sama",
         "ok": rank is not None and (rank <= 0.7 if long else rank >= 0.3)},
        {"label": "Taker 4 jam terakhir searah", "ok": taker is not None and (taker > 1 if long else taker < 1)},
    ]
    sign = 1 if long else -1
    a = setup["frames"]["entry"]["atr"] or 0.0
    last = ltf_candles[-1]
    body = sign * (last["close"] - last["open"]) / a if a else 0.0
    checks.append({"label": "Candle sinyal tutup searah", "ok": body >= BODY_ATR})
    chop_txt = "" if btc_chop_value is None else f" (CHOP {btc_chop_value:.0f})"
    checks.append({"label": f"BTC harian tidak sedang choppy{chop_txt}", "ok": chop_ok(btc_chop_value)})
    entry = price - sign * LIMIT_ATR * a
    sl = price - sign * STOP_ATR * a
    risk = (entry - sl) * sign
    active = all(c["ok"] for c in checks) and risk > 0
    tp = entry + sign * TP_R * risk
    return {
        "active": active,
        "side": side,
        "order": "limit",
        "valid_bars": LIMIT_BARS,
        "valid_hours": LIMIT_BARS * INTERVAL_SECONDS[MODES[mode]["ltf"]] // 3600,
        "entry": _round(entry, price),
        "stop_loss": _round(sl, price),
        "take_profit": _round(tp, price),
        "rr": TP_R,
        "max_hold_bars": MAX_HOLD_BARS,
        "max_hold_hours": MAX_HOLD_BARS * INTERVAL_SECONDS[MODES[mode]["ltf"]] // 3600,
        "sl_pct": round(100 * risk / entry, 2) if risk > 0 else None,
        "min_notional": MIN_NOTIONAL.get(setup["symbol"], 5),
        "checks": checks,
        "btc_regime": btc_state,
        "btc_chop": None if btc_chop_value is None else round(btc_chop_value, 1),
        "rules": "v3",
        **VALIDATION[mode],
    }
