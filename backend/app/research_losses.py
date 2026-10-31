"""Why do v3 trades lose? Features at the signal bar, filters and exits tested out of sample.

Starts from the live v3 trades (backtest_v3) and asks two questions:
1. Do losing trades share something measurable at the signal bar (stretched price,
   weak score, crowded positioning, ...)? Each feature is cut at in-sample quantiles
   and the cut is judged on the later, out-of-sample period.
2. Would a different exit (target, time limit, stop to breakeven) have kept more of
   the moves that later reversed?

Only a change that holds out of sample and in most years, without dropping many
trades, is worth taking to the live rules.

Run: python -m app.research_losses [symbols] [ltf_bars] > losses.txt
"""

from __future__ import annotations

import itertools
import sys
from bisect import bisect_right
from datetime import datetime, timezone

from . import research as R
from . import research_deriv as D
from . import research_entry as E
from .backtest import FEE_ROUND_TRIP, WARMUP
from .indicators import atr, ema, rsi
from .market import INTERVAL_SECONDS
from .setup import LIMIT_ATR, LIMIT_BARS, MAX_HOLD_BARS, MODES, STOP_ATR, TP_R, btc_regime

STEP = INTERVAL_SECONDS[MODES["swing"]["ltf"]]
DAY = INTERVAL_SECONDS[MODES["swing"]["htf"]]


def manage(long: bool, entry: float, sl: float, ltf: list[dict], start: int, max_hold: int,
           tp_r: float, be_at: float | None) -> tuple[float, int, str]:
    """One target; optionally move the stop to entry once price has gone `be_at` R our way
    (checked on bar closes, so the move takes effect from the next bar)."""
    sign = 1 if long else -1
    risk = (entry - sl) * sign
    tp = entry + sign * tp_r * risk
    stop = sl
    end = min(len(ltf) - 1, start + max_hold)
    for j in range(start, end + 1):
        b = ltf[j]
        if (b["low"] <= stop) if long else (b["high"] >= stop):
            return (stop - entry) * sign / risk, j, "stop" if stop == sl else "breakeven"
        if (b["high"] >= tp) if long else (b["low"] <= tp):
            return tp_r, j, "tp"
        if be_at is not None and (b["close"] - entry) * sign >= be_at * risk:
            stop = entry
    c = ltf[end]["close"]
    return (c - entry) * sign / risk, end, "timeout"


def trade(p: dict, ltf: list[dict], i: int, tp_r: float, max_hold: int, be_at: float | None) -> dict | None:
    """Live entry (limit 0.25 ATR, 4 bars, skip) and stop (2 ATR), as research_entry.trade."""
    long = p["side"] == "long"
    sign = 1 if long else -1
    close, a = ltf[i]["close"], p["atr"]
    sl = close - sign * STOP_ATR * a
    limit = close - sign * LIMIT_ATR * a
    fill = None
    for j in range(i + 1, min(len(ltf) - 1, i + LIMIT_BARS) + 1):
        b = ltf[j]
        hit_limit = (b["low"] <= limit) if long else (b["high"] >= limit)
        if (b["low"] <= sl) if long else (b["high"] >= sl):
            if hit_limit:
                entry, fill = (min(b["open"], limit) if long else max(b["open"], limit)), j
            break
        if hit_limit:
            entry, fill = (min(b["open"], limit) if long else max(b["open"], limit)), j
            break
    if fill is None:
        return None
    risk = (entry - sl) * sign
    if risk <= 0:
        return None
    fee = FEE_ROUND_TRIP * entry / risk
    b = ltf[fill]
    if (b["low"] <= sl) if long else (b["high"] >= sl):
        return {"i": i, "t": b["time"], "r": -1 - fee, "exit": fill, "status": "stop"}
    if fill + 1 >= len(ltf):
        return None
    r, j, status = manage(long, entry, sl, ltf, fill + 1, max_hold, tp_r, be_at)
    return {"i": i, "t": b["time"], "r": r - fee, "exit": j, "status": status}


def simulate(pre, ltf, sigs, tp_r=TP_R, max_hold=MAX_HOLD_BARS, be_at=None) -> list[dict]:
    out, free = [], 0
    for i in sigs:
        if i < free:
            continue
        t = trade(pre[i], ltf, i, tp_r, max_hold, be_at)
        if t:
            out.append(t)
            free = t["exit"] + 1
    return out


def features(sym: str, pre: list, ltf: list[dict], htf: list[dict], btc_htf: list[dict], feat: list[dict]) -> dict:
    """Per signal bar, numbers signed so that higher = more in the trade's favour or more stretched that way."""
    closes = [c["close"] for c in ltf]
    e20, e50, r14, a14 = ema(closes, 20), ema(closes, 50), rsi(closes), atr(ltf)
    vol = [c["volume"] for c in ltf]
    hc = [c["close"] for c in htf]
    h20, h50, ha, hr = ema(hc, 20), ema(hc, 50), atr(htf), rsi(hc)
    h_open = [c["time"] for c in htf]
    bc = [c["close"] for c in btc_htf]
    b50 = ema(bc, 50)
    b_open = [c["time"] for c in btc_htf]

    out = {}
    for i, p in enumerate(pre):
        if p is None or i < 60:
            continue
        s = 1 if p["side"] == "long" else -1
        a = a14[i] or p["atr"]
        c = closes[i]
        close_t = ltf[i]["time"] + STEP
        k = bisect_right(h_open, close_t - DAY) - 1
        kb = bisect_right(b_open, close_t - DAY) - 1
        f = feat[i] if i < len(feat) else {}
        avg_vol = sum(vol[i - 20:i]) / 20 or 1
        row = {
            "score": abs(p["score"]),
            "atr_pct": 100 * p["atr_pct"],
            "rsi": 50 + s * ((r14[i] or 50) - 50),
            "ext20": s * (c - e20[i]) / a if e20[i] else None,
            "ext50": s * (c - e50[i]) / a if e50[i] else None,
            "ret6": s * (c - closes[i - 6]) / a,
            "ret30": s * (c - closes[i - 30]) / a,
            "body": s * (c - ltf[i]["open"]) / a,
            "vol": vol[i] / avg_vol,
            "d_ext20": s * (hc[k] - h20[k]) / ha[k] if k >= 0 and h20[k] and ha[k] else None,
            "d_ext50": s * (hc[k] - h50[k]) / ha[k] if k >= 0 and h50[k] and ha[k] else None,
            "d_rsi": 50 + s * ((hr[k] or 50) - 50) if k >= 0 else None,
            "btc_ext": s * 100 * (bc[kb] / b50[kb] - 1) if kb >= 0 and b50[kb] else None,
            "oi": f.get("oi_chg") and 100 * f["oi_chg"],
            "crowd": None if f.get("ls_rank") is None else (f["ls_rank"] if s > 0 else 1 - f["ls_rank"]),
            "taker": None if not f.get("taker") else (f["taker"] if s > 0 else 1 / f["taker"]),
            "fund": None if f.get("funding_rank") is None else (f["funding_rank"] if s > 0 else 1 - f["funding_rank"]),
        }
        out[i] = row
    return out


def stats(trades: list[dict]) -> str:
    if not trades:
        return "n0"
    rs = [t["r"] for t in trades]
    win = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r <= 0)
    return f"n{len(rs)} wr{100 * sum(r > 0 for r in rs) / len(rs):.0f} e{sum(rs) / len(rs):+.3f} T{sum(rs):+.1f} pf{win / loss if loss else 0:.2f}"


def split(trades, cut):
    return [t for t in trades if t["t"] < cut], [t for t in trades if t["t"] >= cut]


def years(trades) -> str:
    y: dict[int, list[float]] = {}
    for t in trades:
        y.setdefault(datetime.fromtimestamp(t["t"], timezone.utc).year, []).append(t["r"])
    return " ".join(f"{k % 100}:{sum(v) / len(v):+.2f}" for k, v in sorted(y.items()))


def run(data: dict, feats: dict) -> list[str]:
    regime = btc_regime(data["BTCUSDT"][0])
    pres, sigs, fx = {}, {}, {}
    for s, (h, l) in data.items():
        pres[s] = R.precompute(s, "swing", h, l, regime)
        sigs[s] = E.signals(pres[s], D.make_allow(feats[s], E.V3), len(l))
        fx[s] = features(s, pres[s], l, h, data["BTCUSDT"][0], feats[s])
    l0 = data["BTCUSDT"][1]
    t0, t1 = l0[WARMUP]["time"], l0[-1]["time"]
    cut = t0 + R.IS_SHARE * (t1 - t0)
    lines = [f"period {datetime.fromtimestamp(t0, timezone.utc):%Y-%m} .. {datetime.fromtimestamp(t1, timezone.utc):%Y-%m}, "
             f"IS until {datetime.fromtimestamp(cut, timezone.utc):%Y-%m}"]

    def pooled(keep=None, **kw):
        out = []
        for s, (_, l) in data.items():
            sg = sigs[s] if keep is None else [i for i in sigs[s] if keep(s, i)]
            out += [{**t, "s": s} for t in simulate(pres[s], l, sg, **kw)]
        return sorted(out, key=lambda t: t["t"])

    base = pooled()
    b_is, b_oos = split(base, cut)
    lines.append(f"BASE all {stats(base)} | IS {stats(b_is)} | OOS {stats(b_oos)} | yrs {years(base)}")

    # 1) losing vs winning trades: average feature value
    names = list(next(iter(fx["BTCUSDT"].values())).keys())
    rows = [(fx[t["s"]].get(t["i"], {}), t["r"]) for t in base]
    lines.append("FEATURE mean(win) mean(loss) | terciles by IS cut: exp IS / exp OOS (low, mid, high)")
    cuts = {}
    for name in names:
        vals_is = sorted(f[name] for (f, _), t in zip(rows, base) if t["t"] < cut and f.get(name) is not None)
        if len(vals_is) < 30:
            continue
        q = [vals_is[int(len(vals_is) * k)] for k in (0.2, 1 / 3, 0.5, 2 / 3, 0.8)]
        cuts[name] = q
        win = [f[name] for f, r in rows if r > 0 and f.get(name) is not None]
        loss = [f[name] for f, r in rows if r <= 0 and f.get(name) is not None]
        parts = []
        for lo, hi in ((-1e18, q[1]), (q[1], q[3]), (q[3], 1e18)):
            grp = [t for (f, _), t in zip(rows, base) if f.get(name) is not None and lo <= f[name] < hi]
            gi, go = split(grp, cut)
            ei = sum(t["r"] for t in gi) / len(gi) if gi else 0
            eo = sum(t["r"] for t in go) / len(go) if go else 0
            parts.append(f"{ei:+.2f}/{eo:+.2f}")
        lines.append(f"{name:8s} {sum(win) / len(win):7.2f} {sum(loss) / len(loss):7.2f} | {'  '.join(parts)}"
                     f" | cuts {q[1]:.2f},{q[3]:.2f}")

    # 2) one-feature filters, re-simulated (a skipped trade frees the coin for the next signal)
    cand = []
    for name, q in cuts.items():
        for v, op in itertools.product(q, (">=", "<=")):
            def keep(s, i, name=name, v=v, op=op):
                x = fx[s].get(i, {}).get(name)
                return x is None or (x >= v if op == ">=" else x <= v)
            tr = pooled(keep)
            ti, to = split(tr, cut)
            if len(ti) < 0.75 * len(b_is):
                continue  # too defensive
            cand.append((sum(t["r"] for t in ti), f"{name}{op}{v:.2f}", ti, to, tr))
    cand.sort(key=lambda c: c[0], reverse=True)
    lines.append("TOP FILTERS by IS total (keep >=75% of IS trades)")
    for tot, lab, ti, to, tr in cand[:10]:
        lines.append(f"{lab:16s} IS {stats(ti)} | OOS {stats(to)} | yrs {years(tr)}")

    # 3) exits
    ex = []
    for tp_r, hold, be in itertools.product((2.0, 2.5, 3.0, 4.0, 5.0), (30, 45, 60, 90), (None, 1.0, 1.5, 2.0)):
        tr = pooled(tp_r=tp_r, max_hold=hold, be_at=be)
        ti, to = split(tr, cut)
        ex.append((sum(t["r"] for t in ti), f"TP{tp_r} hold{hold} be{be}", ti, to, tr))
    ex.sort(key=lambda c: c[0], reverse=True)
    lines.append("TOP EXITS by IS total")
    for tot, lab, ti, to, tr in ex[:10]:
        lines.append(f"{lab:22s} IS {stats(ti)} | OOS {stats(to)} | yrs {years(tr)}")
    live = next(e for e in ex if e[1] == f"TP{TP_R} hold{MAX_HOLD_BARS} beNone")
    lines.append(f"rank of live exit: {ex.index(live) + 1}/{len(ex)}")
    return lines


if __name__ == "__main__":
    syms = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 10500
    print("\n".join(run(*E.load(syms, bars))))
