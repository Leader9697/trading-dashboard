"""Rule research: test filter and entry variants of the setup engine out of sample.

The setup engine is run once per closed entry bar (the expensive part); every
variant then replays those precomputed setups cheaply. Variants are ranked on
the first part of history only (in-sample) and judged on the rest
(out-of-sample), so a rule is never graded on the data it was picked with.

Run: python -m app.research [swing|scalp] > result.json
"""

from __future__ import annotations

import itertools
import json
import sys
from bisect import bisect_right

from .backtest import FEE_ROUND_TRIP, RULES, WARMUP, WINDOW, _htf_slice
from .market import INTERVAL_SECONDS
from .setup import MODES, btc_regime, build_setup

IS_SHARE = 0.6
GRID = {
    "entry": ["limit", "confirm", "market"],  # pullback limit, limit + rejection candle close, next-bar open
    "regime": [False, True],                  # trade only in the direction of BTC's higher-timeframe trend
    "trend200": [False, True],                # long only above the HTF EMA200, short only below
    "wide_sl": [False, True],                 # stop 0.5 ATR further out, targets re-based on the new risk
    "threshold": [25, 40],                    # minimum |bias score|
    "vol": [False, True],                     # skip when ATR% is below its recent median
}
AUTO_RULES = {"min_trades": 30, "min_pf": 1.2, "min_coins": 3, "min_positive_folds": 3, "folds": 4}


def precompute(symbol: str, mode: str, htf: list[dict], ltf: list[dict], regime: tuple[list[int], list[int]]) -> list:
    cfg = MODES[mode]
    htf_step, ltf_step = INTERVAL_SECONDS[cfg["htf"]], INTERVAL_SECONDS[cfg["ltf"]]
    htf_open = [c["time"] for c in htf]
    reg_open, reg = regime
    pre: list = [None] * len(ltf)
    for i in range(WARMUP, len(ltf) - 1):
        close_t = ltf[i]["time"] + ltf_step
        h = _htf_slice(htf, htf_open, htf_step, close_t)
        if len(h) < 60:
            continue
        s = build_setup(symbol, mode, h, ltf[max(0, i - WINDOW + 1): i + 1], {}, None)
        tr, en = s["frames"]["trend"], s["frames"]["entry"]
        r_idx = bisect_right(reg_open, close_t - htf_step) - 1
        lv = s["levels"]
        pre[i] = {
            "score": s["score"], "side": s["plan_side"],
            "entry": lv["entry"], "sl": lv["stop_loss"], "atr": en["atr"] or 0.0,
            "above200": None if tr["ema200"] is None else tr["close"] > tr["ema200"],
            "btc": reg[r_idx] if r_idx >= 0 else 0,
            "atr_pct": (en["atr"] or 0) / en["close"],
        }
    return pre


def _manage(long: bool, entry: float, sl: float, ltf: list[dict], start: int, max_hold: int,
            tp_r: float | None = None) -> tuple[float, int, str]:
    """Half off at 2R with stop to entry, rest to 3R; or, with `tp_r`, all out at one target.
    Stop wins on a bar that touches both."""
    risk = abs(entry - sl)
    if tp_r is not None:
        tp = entry + tp_r * risk if long else entry - tp_r * risk
        end = min(len(ltf) - 1, start + max_hold)
        for j in range(start, end + 1):
            b = ltf[j]
            if (b["low"] <= sl) if long else (b["high"] >= sl):
                return -1.0, j, "stop"
            if (b["high"] >= tp) if long else (b["low"] <= tp):
                return tp_r, j, "tp"
        c = ltf[end]["close"]
        return ((c - entry) if long else (entry - c)) / risk, end, "timeout"
    tp1 = entry + 2 * risk if long else entry - 2 * risk
    tp2 = entry + 3 * risk if long else entry - 3 * risk
    half = False
    end = min(len(ltf) - 1, start + max_hold)
    for j in range(start, end + 1):
        b = ltf[j]
        stop = entry if half else sl
        if (b["low"] <= stop) if long else (b["high"] >= stop):
            return (1.0 if half else -1.0), j, ("breakeven" if half else "stop")
        if not half and ((b["high"] >= tp1) if long else (b["low"] <= tp1)):
            half = True
        if half and ((b["high"] >= tp2) if long else (b["low"] <= tp2)):
            return 2.5, j, "tp2"
    c = ltf[end]["close"]
    open_r = ((c - entry) if long else (entry - c)) / risk
    return (1.0 + 0.5 * open_r if half else open_r), end, "timeout"


def simulate(pre: list, ltf: list[dict], mode: str, v: dict, allow=None) -> list[dict]:
    """`allow(i, long)` is an optional extra filter, e.g. on funding or open interest."""
    rules = RULES[mode]
    trades = []
    i, n = WARMUP, len(ltf)
    recent_atr: list[float] = []
    while i < n - 1:
        p = pre[i]
        if p is None:
            i += 1
            continue
        recent_atr.append(p["atr_pct"])
        if len(recent_atr) > 200:
            recent_atr.pop(0)
        long = p["side"] == "long"
        ok = abs(p["score"]) >= v["threshold"]
        if ok and v["regime"]:
            ok = p["btc"] == (1 if long else -1)
        if ok and v["trend200"]:
            ok = p["above200"] is not None and p["above200"] == long
        if ok and v["vol"] and len(recent_atr) >= 50:
            ok = p["atr_pct"] >= sorted(recent_atr)[len(recent_atr) // 2]
        if ok and allow is not None:
            ok = allow(i, long)
        if not ok:
            i += 1
            continue

        entry, sl = p["entry"], p["sl"]
        if v["wide_sl"]:
            sl = sl - 0.5 * p["atr"] if long else sl + 0.5 * p["atr"]
        fill, start = None, None
        last = min(n - 1, i + rules["order_bars"])
        if v["entry"] == "market":
            entry, fill, start = ltf[i + 1]["open"], i + 1, i + 1
            if (long and entry <= sl) or (not long and entry >= sl):
                i += 1
                continue
        else:
            risk = abs(entry - sl)
            tp1 = entry + 2 * risk if long else entry - 2 * risk
            for j in range(i + 1, last + 1):
                b = ltf[j]
                touched = (b["low"] <= entry) if long else (b["high"] >= entry)
                if not touched and ((b["high"] >= tp1) if long else (b["low"] <= tp1)):
                    break
                if not touched:
                    continue
                if v["entry"] == "limit":
                    fill, start = j, j
                    break
                # confirm: the touching bar must close back in the trade direction, above/below entry
                if (b["low"] <= sl) if long else (b["high"] >= sl):
                    break
                if (long and b["close"] > entry and b["close"] > b["open"]) or (
                        not long and b["close"] < entry and b["close"] < b["open"]):
                    entry, fill, start = b["close"], j, j + 1
                    break
        if fill is None or start >= n:
            i = last + 1 if v["entry"] != "market" else i + 1
            continue
        r, exit_j, status = _manage(long, entry, sl, ltf, start, rules["max_hold"], v.get("tp_r"))
        fee_r = FEE_ROUND_TRIP * entry / abs(entry - sl)
        trades.append({"t": ltf[fill]["time"], "r": r - fee_r, "status": status})
        i = exit_j + 1
    return trades


def score(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0, "wr": None, "exp": None, "pf": None}
    wins = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r <= 0)
    return {"n": len(rs), "wr": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
            "exp": round(sum(rs) / len(rs), 3), "pf": round(wins / loss, 2) if loss else None}


def folds(trades: list[dict], t0: int, t1: int, k: int = 4) -> list[dict]:
    """Stats for k equal time slices: an edge should hold in most of them, not just one."""
    edges = [t0 + (t1 - t0) * j / k for j in range(k)] + [float("inf")]
    return [score([t["r"] for t in trades if edges[j] <= t["t"] < edges[j + 1]]) for j in range(k)]


def variants() -> list[dict]:
    keys = list(GRID)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(GRID[k] for k in keys))]


def run(data: dict[str, tuple[list[dict], list[dict]]], mode: str) -> dict:
    """data: symbol -> (htf candles, ltf candles). Must include BTCUSDT for the regime filter."""
    regime = btc_regime(data["BTCUSDT"][0])
    pres = {s: precompute(s, mode, h, l, regime) for s, (h, l) in data.items()}
    rows = []
    for v in variants():
        per = {}
        all_is, all_oos, pooled = [], [], []
        for s, (_, l) in data.items():
            trades = simulate(pres[s], l, mode, v)
            pooled += trades
            cut = l[WARMUP]["time"] + IS_SHARE * (l[-1]["time"] - l[WARMUP]["time"])
            r_is = [t["r"] for t in trades if t["t"] < cut]
            r_oos = [t["r"] for t in trades if t["t"] >= cut]
            all_is += r_is
            all_oos += r_oos
            per[s] = {"is": score(r_is), "oos": score(r_oos)}
        rows.append({"v": v, "is": score(all_is), "oos": score(all_oos), "coins": per, "_trades": pooled})
    ranked = sorted((r for r in rows if r["is"]["n"] >= 60), key=lambda r: r["is"]["exp"], reverse=True)
    best = ranked[0] if ranked else None
    l0 = data["BTCUSDT"][1]
    span = (l0[WARMUP]["time"], l0[-1]["time"])
    verdict = None
    if best:
        pos = [s for s, c in best["coins"].items() if (c["oos"]["exp"] or 0) > 0]
        fs = folds(best["_trades"], *span, AUTO_RULES["folds"])
        good_folds = sum(1 for f in fs if (f["exp"] or 0) > 0)
        verdict = {
            "passes": bool(best["oos"]["n"] >= AUTO_RULES["min_trades"]
                           and (best["oos"]["pf"] or 0) >= AUTO_RULES["min_pf"]
                           and len(pos) >= AUTO_RULES["min_coins"]
                           and good_folds >= AUTO_RULES["min_positive_folds"]),
            "oos_positive_coins": pos,
            "folds": fs,
            "positive_folds": good_folds,
        }
    for r in rows:
        r.pop("_trades")
    base = next(r for r in rows if r["v"] == {"entry": "limit", "regime": False, "trend200": False,
                                               "wide_sl": False, "threshold": 25, "vol": False})
    return {
        "mode": mode,
        "variants_tested": len(rows),
        "oos_positive_share": round(sum(1 for r in rows if (r["oos"]["exp"] or 0) > 0) / len(rows), 2),
        "baseline": base,
        "best_in_sample": best,
        "verdict": verdict,
        "top10_is": [{"v": r["v"], "is": r["is"], "oos": r["oos"]} for r in ranked[:10]],
    }


if __name__ == "__main__":
    from .config import get_settings
    from .market import get_klines

    mode = sys.argv[1] if len(sys.argv) > 1 else "swing"
    symbols = (sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    ltf_bars = int(sys.argv[3]) if len(sys.argv) > 3 else 6000
    settings, cfg = get_settings(), MODES[mode]
    htf_bars = ltf_bars * INTERVAL_SECONDS[cfg["ltf"]] // INTERVAL_SECONDS[cfg["htf"]] + 320
    data = {}
    for s in symbols:
        l = get_klines(settings, s, cfg["ltf"], ltf_bars)[:-1]
        h = get_klines(settings, s, cfg["htf"], htf_bars)
        data[s] = (h, l)
        print(f"{s}: {len(l)} {cfg['ltf']} bars", file=sys.stderr)
    print(json.dumps(run(data, mode)))
