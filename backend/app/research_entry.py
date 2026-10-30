"""Sharper entries for the v3 signal: does waiting for a pullback, or a tighter or
wider stop, earn more in total than entering at market?

The signal set is fixed (rules v3). Only how the trade is entered and protected
changes, so a variant that keeps most trades and earns more per trade is a real
improvement, not just a stricter filter. Variants are ranked on total R in the
first 60% of history and judged on the rest, plus a 2022 to March 2024 blind
window and per-year results.

Run: python -m app.research_entry [symbols] [ltf_bars] > result.json
"""

from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

from . import research as R
from . import research_deriv as D
from .backtest import FEE_ROUND_TRIP, RULES, WARMUP
from .market import INTERVAL_SECONDS
from .setup import MODES, RULES_V2, RULES_V3_POSITIONING, TP_R, btc_regime

V3 = {"funding": False, **RULES_V3_POSITIONING}
BLIND_END = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp())

ENTRIES = [("market", 0, 0, None)] + [
    ("pullback", k, n, fallback) for k in (0.25, 0.5, 1.0) for n in (2, 4) for fallback in ("market", "skip")
]
STOPS = ["struct", ("atr", 1.0), ("atr", 1.5), ("atr", 2.0)]
TARGETS = [2.0, 2.5, 3.0]


def signals(pre: list, allow, n: int) -> list[int]:
    """Bars whose close carries an active v3 signal (filters as in research.simulate)."""
    out, recent = [], []
    for i in range(WARMUP, n - 1):
        p = pre[i]
        if p is None:
            continue
        recent.append(p["atr_pct"])
        if len(recent) > 200:
            recent.pop(0)
        long = p["side"] == "long"
        if abs(p["score"]) < RULES_V2["threshold"] or p["btc"] != (1 if long else -1):
            continue
        if len(recent) >= 50 and p["atr_pct"] < sorted(recent)[len(recent) // 2]:
            continue
        if allow(i, long):
            out.append(i)
    return out


def trade(p: dict, ltf: list[dict], i: int, entry_v: tuple, stop_v, tp_r: float, max_hold: int) -> dict | None:
    long = p["side"] == "long"
    sign = 1 if long else -1
    close, atr = ltf[i]["close"], p["atr"]
    sl = p["sl"] if stop_v == "struct" else close - sign * stop_v[1] * atr
    kind, k, wait, fallback = entry_v
    fill = None
    if kind == "market":
        entry, fill = ltf[i + 1]["open"], i + 1
    else:
        limit = close - sign * k * atr
        last = min(len(ltf) - 1, i + wait)
        for j in range(i + 1, last + 1):
            b = ltf[j]
            if (b["low"] <= sl) if long else (b["high"] >= sl):
                if (b["low"] <= limit) if long else (b["high"] >= limit):
                    entry, fill = (min(b["open"], limit) if long else max(b["open"], limit)), j
                break  # stopped out before (or on) the fill bar
            if (b["low"] <= limit) if long else (b["high"] >= limit):
                entry, fill = (min(b["open"], limit) if long else max(b["open"], limit)), j
                break
        else:
            if fallback == "market" and last + 1 < len(ltf):
                entry, fill = ltf[last + 1]["open"], last + 1
        if fill is None:
            return None
    risk = (entry - sl) * sign
    if risk <= 0:
        return None
    start = fill
    if kind != "market":
        # A limit fills somewhere inside its bar and the order of that bar's high and low
        # is unknown: count a stop on the fill bar, but no target until the next bar.
        b = ltf[fill]
        if (b["low"] <= sl) if long else (b["high"] >= sl):
            return {"t": b["time"], "r": -1 - FEE_ROUND_TRIP * entry / risk, "exit": fill, "status": "stop",
                    "risk_pct": 100 * risk / entry}
        start = fill + 1
        if start >= len(ltf):
            return None
    r, exit_j, status = R._manage(long, entry, sl, ltf, start, max_hold, tp_r)
    return {"t": ltf[fill]["time"], "r": r - FEE_ROUND_TRIP * entry / risk, "exit": exit_j, "status": status,
            "risk_pct": 100 * risk / entry}


def simulate(pre: list, ltf: list[dict], sigs: list[int], entry_v, stop_v, tp_r, max_hold: int) -> list[dict]:
    out, free_from = [], 0
    for i in sigs:
        if i < free_from:
            continue
        t = trade(pre[i], ltf, i, entry_v, stop_v, tp_r, max_hold)
        if t is None:
            continue
        out.append(t)
        free_from = t["exit"] + 1
    return out


def max_dd(rs: list[float]) -> float:
    peak = eq = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 1)


def summary(trades: list[dict]) -> dict:
    trades = sorted(trades, key=lambda t: t["t"])
    rs = [t["r"] for t in trades]
    s = R.score(rs)
    s["total"] = round(sum(rs), 1)
    s["dd"] = max_dd(rs)
    risk = sorted(t["risk_pct"] for t in trades)
    s["sl_pct_median"] = round(risk[len(risk) // 2], 2) if risk else None
    return s


def label(entry_v, stop_v, tp_r) -> str:
    kind, k, n, fb = entry_v
    e = "market" if kind == "market" else f"pull {k}ATR/{n}bar/{fb}"
    s = "struct" if stop_v == "struct" else f"{stop_v[1]}ATR"
    return f"{e} | SL {s} | TP {tp_r}R"


def run(data: dict, feats: dict, mode: str = "swing") -> dict:
    regime = btc_regime(data["BTCUSDT"][0])
    max_hold = RULES[mode]["max_hold"]
    pres, sigs = {}, {}
    for s, (h, l) in data.items():
        pres[s] = R.precompute(s, mode, h, l, regime)
        sigs[s] = signals(pres[s], D.make_allow(feats[s], V3), len(l))
    l0 = data["BTCUSDT"][1]
    t0, t1 = l0[WARMUP]["time"], l0[-1]["time"]
    cut = t0 + R.IS_SHARE * (t1 - t0)
    rows = []
    for entry_v, stop_v, tp_r in itertools.product(ENTRIES, STOPS, TARGETS):
        pooled = []
        for s, (_, l) in data.items():
            pooled += simulate(pres[s], l, sigs[s], entry_v, stop_v, tp_r, max_hold)
        years: dict[str, list[float]] = {}
        for t in pooled:
            years.setdefault(str(datetime.fromtimestamp(t["t"], timezone.utc).year), []).append(t["r"])
        fs = R.folds(pooled, t0, t1)
        rows.append({
            "v": label(entry_v, stop_v, tp_r),
            "all": summary(pooled),
            "is": summary([t for t in pooled if t["t"] < cut]),
            "oos": summary([t for t in pooled if t["t"] >= cut]),
            "blind": summary([t for t in pooled if t["t"] < BLIND_END]),
            "years": {y: [len(v), round(sum(v) / len(v), 3)] for y, v in sorted(years.items())},
            "pos_folds": sum(1 for f in fs if (f["exp"] or 0) > 0),
        })
    base = next(r for r in rows if r["v"] == label(ENTRIES[0], "struct", TP_R))
    ranked = sorted(rows, key=lambda r: r["is"]["total"], reverse=True)
    return {
        "period": [t0, t1],
        "signals": {s: len(v) for s, v in sigs.items()},
        "baseline": base,
        "top_is": ranked[:12],
        "top_oos_total": sorted(rows, key=lambda r: r["oos"]["total"], reverse=True)[:8],
        "variants": len(rows),
    }


def load(symbols: list[str], ltf_bars: int, mode: str = "swing"):
    from .config import get_settings
    from .market import get_klines

    cfg, settings = MODES[mode], get_settings()
    step = INTERVAL_SECONDS[cfg["ltf"]]
    htf_bars = ltf_bars * step // INTERVAL_SECONDS[cfg["htf"]] + 320
    data, feats = {}, {}
    with httpx.Client() as client:
        for s in symbols:
            l = get_klines(settings, s, cfg["ltf"], ltf_bars)[:-1]
            h = get_klines(settings, s, cfg["htf"], htf_bars)
            start = datetime.fromtimestamp(l[0]["time"], timezone.utc) - timedelta(days=2)
            end = datetime.now(timezone.utc) - timedelta(days=1)
            fund = D.funding_history(client, s, int(start.timestamp() * 1000))
            met = D.metrics_history(client, s, start, end)
            data[s], feats[s] = (h, l), D.features(l, step, fund, met)
            print(f"{s}: {len(l)} bars, {len(met)} metrics rows", file=sys.stderr)
    return data, feats


if __name__ == "__main__":
    syms = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 10500
    print(json.dumps(run(*load(syms, bars))))
