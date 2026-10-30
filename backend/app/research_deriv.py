"""Does futures positioning (funding, open interest, long/short, taker flow) improve the setups?

History comes from Binance itself: funding rates through the API (full history)
and 5-minute open interest / long-short / taker metrics from the public archive
at data.binance.vision (daily files since late 2021). Each feature is read at
the close of the signal bar only, so nothing from the future leaks in.

Run: python -m app.research_deriv [symbols] [ltf_bars] > result.json
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import sys
import zipfile
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from . import research as R
from .market import INTERVAL_SECONDS
from .setup import MODES, RULES_V2, btc_regime

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
RANK_WINDOW = 90 * 6  # ~90 days of 4h bars for percentile ranks

BASES = {
    "v2": RULES_V2,
    "loose": {**RULES_V2, "threshold": 25, "vol": False},
}
GRID = {
    "funding": [False, True],              # skip longs when funding is in its top 20% (crowded), shorts in bottom 20%
    "oi": [None, "up", "down"],            # require 24h open interest change > 0 or < 0
    "crowd": [False, True],                # skip longs when retail long/short ratio is in its top 30%, shorts bottom 30%
    "taker": [False, True],                # longs need taker buy/sell > 1 over the last 4h, shorts < 1
}


def funding_history(client: httpx.Client, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    out, cursor = [], start_ms
    while True:
        rows = client.get("https://fapi.binance.com/fapi/v1/fundingRate",
                          params={"symbol": symbol, "startTime": cursor, "limit": 1000}, timeout=20).json()
        if not rows:
            break
        out += [(r["fundingTime"] // 1000, float(r["fundingRate"])) for r in rows]
        if len(rows) < 1000:
            break
        cursor = rows[-1]["fundingTime"] + 1
    return out


def _metrics_day(client: httpx.Client, symbol: str, day: str) -> list[tuple]:
    r = client.get(ARCHIVE.format(s=symbol, d=day), timeout=30)
    if r.status_code != 200:
        return []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            t = int(datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            rows.append((t, float(row["sum_open_interest"]), float(row["count_long_short_ratio"] or 0),
                         float(row["sum_taker_long_short_vol_ratio"] or 0)))
        except (KeyError, ValueError):
            continue
    return rows


def metrics_history(client: httpx.Client, symbol: str, start: datetime, end: datetime) -> list[tuple]:
    days = [(start + timedelta(days=k)).strftime("%Y-%m-%d") for k in range((end - start).days + 1)]
    with ThreadPoolExecutor(max_workers=24) as pool:
        chunks = list(pool.map(lambda d: _metrics_day(client, symbol, d), days))
    return sorted(r for c in chunks for r in c)


def _rank(values: list[float | None], i: int) -> float | None:
    window = [v for v in values[max(0, i - RANK_WINDOW): i + 1] if v is not None]
    if len(window) < 60 or values[i] is None:
        return None
    return sum(1 for v in window if v <= values[i]) / len(window)


def features(ltf: list[dict], step: int, funding: list[tuple], metrics: list[tuple]) -> list[dict]:
    """Per entry bar: funding, 24h OI change, retail L/S and 4h taker ratio as of the bar close."""
    f_t = [t for t, _ in funding]
    m_t = [m[0] for m in metrics]
    raw = []
    for c in ltf:
        close = c["time"] + step
        fi = bisect_right(f_t, close) - 1
        mi = bisect_right(m_t, close) - 1
        m24 = bisect_right(m_t, close - 86400) - 1
        m4 = bisect_right(m_t, close - 4 * 3600)
        fresh = mi >= 0 and close - metrics[mi][0] <= 1800
        raw.append({
            "funding": funding[fi][1] if fi >= 0 else None,
            "oi_chg": (metrics[mi][1] / metrics[m24][1] - 1) if fresh and m24 >= 0 and metrics[m24][1] else None,
            "ls": metrics[mi][2] if fresh else None,
            "taker": (sum(m[3] for m in metrics[m4: mi + 1]) / (mi + 1 - m4)) if fresh and mi >= m4 else None,
        })
    fund = [r["funding"] for r in raw]
    ls = [r["ls"] for r in raw]
    for i, r in enumerate(raw):
        r["funding_rank"] = _rank(fund, i)
        r["ls_rank"] = _rank(ls, i)
    return raw


def make_allow(feat: list[dict], g: dict):
    def allow(i: int, long: bool) -> bool:
        f = feat[i]
        if g["funding"]:
            if f["funding_rank"] is None or (long and f["funding_rank"] > 0.8) or (not long and f["funding_rank"] < 0.2):
                return False
        if g["oi"]:
            if f["oi_chg"] is None or (g["oi"] == "up") != (f["oi_chg"] > 0):
                return False
        if g["crowd"]:
            if f["ls_rank"] is None or (long and f["ls_rank"] > 0.7) or (not long and f["ls_rank"] < 0.3):
                return False
        if g["taker"]:
            if f["taker"] is None or (long and f["taker"] <= 1) or (not long and f["taker"] >= 1):
                return False
        return True
    return allow


def run(data: dict, feats: dict, mode: str = "swing") -> dict:
    regime = btc_regime(data["BTCUSDT"][0])
    pres = {s: R.precompute(s, mode, h, l, regime) for s, (h, l) in data.items()}
    l0 = data["BTCUSDT"][1]
    t0, t1 = l0[R.WARMUP]["time"], l0[-1]["time"]
    cut = t0 + R.IS_SHARE * (t1 - t0)
    keys = list(GRID)
    rows = []
    for base_name, base in BASES.items():
        for combo in itertools.product(*(GRID[k] for k in keys)):
            g = dict(zip(keys, combo))
            pooled, per = [], {}
            for s, (_, l) in data.items():
                tr = R.simulate(pres[s], l, mode, base, make_allow(feats[s], g))
                pooled += tr
                per[s] = R.score([t["r"] for t in tr if t["t"] >= cut])["exp"]
            fs = R.folds(pooled, t0, t1)
            rows.append({
                "base": base_name, "g": g,
                "is": R.score([t["r"] for t in pooled if t["t"] < cut]),
                "oos": R.score([t["r"] for t in pooled if t["t"] >= cut]),
                "folds": [(f["n"], f["exp"]) for f in fs],
                "pos_folds": sum(1 for f in fs if (f["exp"] or 0) > 0),
                "pos_coins_oos": sum(1 for e in per.values() if (e or 0) > 0),
            })
    ranked = sorted((r for r in rows if r["is"]["n"] >= 60), key=lambda r: r["is"]["exp"], reverse=True)
    best = ranked[0] if ranked else None
    passes = bool(best and best["oos"]["n"] >= 30 and (best["oos"]["pf"] or 0) >= 1.2
                  and best["pos_coins_oos"] >= 3 and best["pos_folds"] >= 3)
    return {
        "period": [t0, t1],
        "variants": len(rows),
        "baseline_v2": next(r for r in rows if r["base"] == "v2" and not any(r["g"].values())),
        "best_in_sample": best,
        "passes_auto": passes,
        "consistent": [r for r in rows if r["pos_folds"] >= 3 and r["oos"]["n"] >= 30],
        "top10_is": ranked[:10],
    }


if __name__ == "__main__":
    from .config import get_settings
    from .market import get_klines

    symbols = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    ltf_bars = int(sys.argv[2]) if len(sys.argv) > 2 else 6000
    mode, cfg, settings = "swing", MODES["swing"], get_settings()
    step = INTERVAL_SECONDS[cfg["ltf"]]
    htf_bars = ltf_bars * step // INTERVAL_SECONDS[cfg["htf"]] + 320
    data, feats = {}, {}
    with httpx.Client() as client:
        for s in symbols:
            l = get_klines(settings, s, cfg["ltf"], ltf_bars)[:-1]
            h = get_klines(settings, s, cfg["htf"], htf_bars)
            start = datetime.fromtimestamp(l[0]["time"], timezone.utc) - timedelta(days=2)
            end = datetime.now(timezone.utc) - timedelta(days=1)
            fund = funding_history(client, s, int(start.timestamp() * 1000))
            met = metrics_history(client, s, start, end)
            data[s], feats[s] = (h, l), features(l, step, fund, met)
            covered = sum(1 for f in feats[s] if f["oi_chg"] is not None)
            print(f"{s}: {len(l)} bars, {len(fund)} funding, {len(met)} metrics rows, {covered} bars with OI", file=sys.stderr)
    print(json.dumps(run(data, feats)))
