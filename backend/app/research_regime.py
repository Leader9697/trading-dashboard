"""Can the engine tell trending from ranging markets, and switch what it does?

Two ideas, both judged the same strict way as the earlier research (choose on data up to
2024, check on 2025-26, account = live engine $60, 0.75% risk x 8 slots, funding paid):

  gate    v3 only trades when a regime measure says "trending"; in a range it sits out.
  switch  v3 in a trend, and a mean-reversion strategy (buy the lower Bollinger band, sell
          the upper one) when the market is ranging.

Regime measures, all known at the close of the signal bar (daily ones from the last
completed day only):
  adx4      ADX 14 on the coin's 4h candles (above 25 = trending in the textbook)
  chop4     Choppiness index 14 on 4h (above 61.8 = ranging, below 38.2 = trending)
  er4       Kaufman efficiency ratio over 30 bars: net move / path length (1 = straight line)
  bbw4      Bollinger band width (20, 2) as a percentile of the last 300 bars (squeeze = low)
  adxD      ADX 14 on the coin's daily candles
  btc_adxD  ADX 14 on BTC daily
  btc_chopD Choppiness 14 on BTC daily
  breadth   share of the 22 engine coins on our side of their daily EMA 50

Every measure gets the same tests, decided before looking at 2025-26:
  * v3 trades split in three by the measure (cuts at the 2022-24 terciles): R per trade
  * gate "skip the weakest third" (the side picked on 2022-24) and the textbook cut
  * for the switch: the mean-reversion strategy alone per trade, then in one account with v3

Two steps, both run where Binance is reachable (a Vercel Sandbox in sin1):
  python -m app.research_regime collect [symbols] [bars]   candles + v3 signals -> RAW_PATH
  python -m app.research_regime report                     compact table -> stdout
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import portfolio as P
from . import research as R
from . import research_donchian as DON
from . import research_profit as RP
from .backtest import FEE_ROUND_TRIP
from .indicators import ema, rsi

RAW_PATH = Path("/tmp/regime_raw.json.gz")
STEP = RP.STEP
DAY = 86400
LIVE = {**RP.ACCOUNT, "risk": 0.75, "max_pos": 8}
T_TRADES, T0, IS_END = RP.T_TRADES, RP.T0, RP.IS_END


# ---------------------------------------------------------------- collect

def _collect_one(args: tuple) -> tuple[str, dict]:
    from .config import get_settings
    from .market import get_klines

    symbol, bars, regime = args
    sym, coin = RP._collect_one((symbol, bars, regime))
    l = get_klines(get_settings(), symbol, "4h", bars)[:-1]
    coin["candles"] = [[b["time"], b["open"], b["high"], b["low"], b["close"]] for b in l]
    return sym, coin


def collect(symbols: list[str], bars: int) -> dict:
    from .config import get_settings
    from .market import get_klines
    from .setup import MODES, btc_regime

    s = get_settings()
    btc_d = get_klines(s, "BTCUSDT", MODES["swing"]["htf"], bars * STEP // DAY + 320)
    with ProcessPoolExecutor(max_workers=6) as pool:
        coins = dict(pool.map(_collect_one, [(x, bars, btc_regime(btc_d)) for x in symbols]))
    btc4 = get_klines(s, "BTCUSDT", "4h", bars)[:-1]
    return {"filters": P.exchange_filters(symbols), "coins": coins,
            "btc": [[b["time"], b["open"], b["high"], b["low"], b["close"]] for b in btc4]}


# ---------------------------------------------------------------- indicators on [t, o, h, l, c] rows

def _tr(c: list[list]) -> list[float]:
    return [c[0][2] - c[0][3]] + [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(c, c[1:])]


def wilder_atr(c: list[list], n: int = 14) -> list[float | None]:
    tr, out = _tr(c), [None] * len(c)
    if len(c) < n:
        return out
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for i in range(n, len(c)):
        v = (v * (n - 1) + tr[i]) / n
        out[i] = v
    return out


def adx(c: list[list], n: int = 14) -> list[float | None]:
    """Wilder's ADX."""
    out: list[float | None] = [None] * len(c)
    if len(c) < 2 * n + 1:
        return out
    tr, pdm, mdm = [0.0], [0.0], [0.0]
    for a, b in zip(c, c[1:]):
        up, dn = b[2] - a[2], a[3] - b[3]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])))
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = []
    for i in range(n, len(c)):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        pdi, mdi = (100 * s_p / s_tr, 100 * s_m / s_tr) if s_tr else (0.0, 0.0)
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
        k = len(dx)
        if k == n:
            v = sum(dx) / n
            out[i] = v
        elif k > n:
            v = (out[i - 1] * (n - 1) + dx[-1]) / n
            out[i] = v
    return out


def chop(c: list[list], n: int = 14) -> list[float | None]:
    tr, out = _tr(c), [None] * len(c)
    for i in range(n, len(c)):
        w = c[i - n + 1:i + 1]
        rng = max(b[2] for b in w) - min(b[3] for b in w)
        out[i] = 100 * math.log10(sum(tr[i - n + 1:i + 1]) / rng) / math.log10(n) if rng > 0 else None
    return out


def eff_ratio(c: list[list], n: int = 30) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    for i in range(n, len(c)):
        path = sum(abs(c[j][4] - c[j - 1][4]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i][4] - c[i - n][4]) / path if path else 0.0
    return out


def bollinger(c: list[list], n: int = 20, k: float = 2.0) -> tuple[list, list, list]:
    mid, lo, hi = [None] * len(c), [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = [b[4] for b in c[i - n + 1:i + 1]]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        mid[i], lo[i], hi[i] = m, m - k * sd, m + k * sd
    return mid, lo, hi


def bbw_rank(c: list[list], look: int = 300) -> list[float | None]:
    mid, lo, hi = bollinger(c)
    w = [None if m is None else (h - l) / m for m, l, h in zip(mid, lo, hi)]
    out: list[float | None] = [None] * len(c)
    for i in range(look + 20, len(c)):
        past = w[i - look:i]
        out[i] = sum(1 for x in past if x <= w[i]) / look
    return out


def daily(c: list[list]) -> list[list]:
    """Complete UTC days from 4h rows: [day, o, h, l, c]."""
    days: dict[int, list] = {}
    for b in c:
        d = b[0] // DAY
        days.setdefault(d, []).append(b)
    return [[d, w[0][1], max(x[2] for x in w), min(x[3] for x in w), w[-1][4]]
            for d, w in sorted(days.items()) if len(w) == 6]


# ---------------------------------------------------------------- regime features

def features(raw: dict) -> dict[str, dict[int, dict]]:
    """{symbol: {signal bar close time: {measure: value}}} for every 4h bar of every coin.
    Indexed by close time (= the time v3 signals carry as "t")."""
    def by_day(rows: list[list], vals: list) -> dict[int, float]:
        return {r[0]: v for r, v in zip(rows, vals) if v is not None}

    btc_d = daily(raw["btc"])
    b_adx, b_chop = by_day(btc_d, adx(btc_d)), by_day(btc_d, chop(btc_d))
    above: dict[int, list[int]] = {}
    per_coin_d = {}
    for s, coin in raw["coins"].items():
        d = daily(coin["candles"])
        e = ema([x[4] for x in d], 50)
        for x, v in zip(d, e):
            if v is not None:
                above.setdefault(x[0], []).append(1 if x[4] > v else 0)
        per_coin_d[s] = by_day(d, adx(d))
    breadth = {d: sum(v) / len(v) for d, v in above.items() if len(v) >= 5}

    out = {}
    for s, coin in raw["coins"].items():
        c = coin["candles"]
        a4, c4, e4, w4 = adx(c), chop(c), eff_ratio(c), bbw_rank(c)
        rows = {}
        for i, b in enumerate(c):
            t = b[0] + STEP
            d = t // DAY - 1  # last day that is complete at this bar's close
            rows[t] = {"adx4": a4[i], "chop4": c4[i], "er4": e4[i], "bbw4": w4[i], "adxD": per_coin_d[s].get(d),
                       "btc_adxD": b_adx.get(d), "btc_chopD": b_chop.get(d), "breadth": breadth.get(d)}
        out[s] = rows
    return out


MEASURES = ("adx4", "chop4", "er4", "bbw4", "adxD", "btc_adxD", "btc_chopD", "breadth")
# textbook cuts, not fitted: (measure, keep the trade when value is ..., cut)
TEXTBOOK = {"ADX 4h > 20": ("adx4", ">", 20), "ADX 4h > 25": ("adx4", ">", 25), "CHOP 4h < 61.8": ("chop4", "<", 61.8),
            "ADX daily > 20": ("adxD", ">", 20), "BTC ADX daily > 20": ("btc_adxD", ">", 20),
            "BTC CHOP daily < 61.8": ("btc_chopD", "<", 61.8)}


def value(feats: dict, s: str, x: dict, m: str) -> float | None:
    v = feats[s].get(x["t"], {}).get(m)
    if v is not None and m == "breadth" and x["side"] == "s":
        v = 1 - v  # share of coins on the short side
    return v


# ---------------------------------------------------------------- mean reversion (the "ranging" engine)

MR_SL_ATR, MR_HOLD, MR_COST = 1.5, 12, FEE_ROUND_TRIP + DON.SLIP_ROUND_TRIP


def mr_signals(c: list[list], feats: dict[int, dict], regime, tp: str) -> list[dict]:
    """Close outside the Bollinger band (20, 2) with RSI 14 past 30/70, in a ranging regime:
    fade it at the next open. SL 1.5 ATR, target the middle band or a fixed R, 12-bar limit."""
    closes = [b[4] for b in c]
    mid, lo, hi = bollinger(c)
    r14, a = rsi(closes, 14), wilder_atr(c)
    out = []
    for i in range(320, len(c) - 1):
        if mid[i] is None or r14[i] is None or a[i] is None:
            continue
        if closes[i] < lo[i] and r14[i] < 30:
            side = "l"
        elif closes[i] > hi[i] and r14[i] > 70:
            side = "s"
        else:
            continue
        if regime is not None and not regime(feats.get(c[i][0] + STEP, {})):
            continue
        long, e = side == "l", i + 1
        sign = 1 if long else -1
        entry = c[e][1]
        sl = entry - sign * MR_SL_ATR * a[i]
        risk = sign * (entry - sl)
        target = mid[i] if tp == "mid" else entry + sign * float(tp) * risk
        if sign * (target - entry) <= 0:
            continue
        last = e + MR_HOLD
        if last >= len(c):
            break
        exit_px, j, status = c[last][4], last, "timeout"
        for j in range(e, last + 1):
            b = c[j]
            if (b[3] <= sl) if long else (b[2] >= sl):
                exit_px, status = (min(b[1], sl) if long else max(b[1], sl)) if j > e else sl, "stop"
                break
            if (b[2] >= target) if long else (b[3] <= target):
                exit_px, status = target, "tp"
                break
        r = sign * (exit_px - entry) / risk - MR_COST * entry / risk
        out.append({"t": c[e][0], "side": side, "score": 0, "end": c[j][0] + STEP, "entry": entry,
                    "sl_pct": round(100 * risk / entry, 3), "r": round(r, 3), "status": status})
    return out


MR_REGIMES = {
    "any market": None,
    "ADX 4h < 20": lambda f: f.get("adx4") is not None and f["adx4"] < 20,
    "CHOP 4h > 61.8": lambda f: f.get("chop4") is not None and f["chop4"] > 61.8,
    "BTC ADX daily < 20": lambda f: f.get("btc_adxD") is not None and f["btc_adxD"] < 20,
}
MR_TPS = ("mid", "1", "1.5")
RANDOM_GATES = 5


# ---------------------------------------------------------------- report

def _split(rs: list[tuple[int, float]]) -> tuple[dict, dict]:
    a = R.score([r for t, r in rs if T_TRADES <= t < IS_END])
    b = R.score([r for t, r in rs if t >= IS_END])
    return a, b


def _short(sc: dict) -> str:
    return f"{sc['n']}:{sc['exp'] or 0:+.2f}"


def _book_r(book: dict) -> list[tuple[int, float]]:
    return [(x["t"], x["r"]) for xs in book.values() for x in xs if "r" in x]


def _acc(books: dict, filters: dict, a: int, b: int, drop: int | None = None) -> dict:
    if drop is not None:
        rng = random.Random(drop)
        books = {k: {s: [x for x in xs if rng.random() >= 0.1] for s, xs in bk.items()} for k, bk in books.items()}
    return DON.replay(books, filters, a, b, LIVE["risk"], LIVE["max_pos"])


def _acc_row(books: dict, filters: dict, end: int) -> dict:
    row = {}
    for w, (a, b) in {"is": (T0, IS_END), "oos": (IS_END, end)}.items():
        r = _acc(books, filters, a, b)
        jack = sorted(_acc(books, filters, a, b, 100 + k)["return_pct"] for k in range(RP.JACK))
        row[w] = {"ret": r["return_pct"], "dd": r["max_dd_pct"], "jack": jack[RP.JACK // 2], "n": r["trades"],
                  **({"by": r["by_strategy"]} if "by_strategy" in r else {})}
    return row


def report(raw: dict) -> dict:
    coins = {s: c for s, c in raw["coins"].items() if s in raw["filters"]}
    raw = {**raw, "coins": coins}
    filters, end = raw["filters"], max(c["last"] for c in coins.values())
    feats = features(raw)
    base_book = RP.outcomes(raw, RP.BASE)  # v3 as live, funding paid
    missing = sum(feats[s].get(x["t"]) is None for s, xs in base_book.items() for x in xs)

    # 1) v3 per trade by regime third (cuts from 2022-24 signals)
    buckets, cuts = {}, {}
    for m in MEASURES:
        vals = sorted(v for s, xs in base_book.items() for x in xs
                      if T_TRADES <= x["t"] < IS_END and (v := value(feats, s, x, m)) is not None)
        if len(vals) < 30:
            continue
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        cuts[m] = (round(lo, 3), round(hi, 3))
        groups = {"low": [], "mid": [], "high": []}
        for s, xs in base_book.items():
            for x in xs:
                v = value(feats, s, x, m)
                if "r" in x and v is not None:
                    groups["low" if v < lo else "mid" if v < hi else "high"].append((x["t"], x["r"]))
        buckets[m] = {g: _split(rs) for g, rs in groups.items()}

    # 2) gates on the live account
    rows = [{"name": "v3 live (no gate)", **RP._row("v3 live", "gate", base_book, filters, end, LIVE)}]
    for m, (lo, hi) in cuts.items():
        b = buckets[m]
        worst = "low" if (b["low"][0]["exp"] or 0) < (b["high"][0]["exp"] or 0) else "high"  # picked on 2022-24 only
        rows.append(_gate(f"skip {m} {worst} third", raw, feats, filters, end, m, worst, lo, hi))
    for name, (m, op, cut) in TEXTBOOK.items():
        rows.append(_gate(name, raw, feats, filters, end, m, op, cut, None))
    for k in range(RANDOM_GATES):  # noise check: skip a random third, no regime at all
        rng = random.Random(500 + k)
        coin_skip = {s: {x["t"] for x in c["signals"] if rng.random() < 1 / 3} for s, c in coins.items()}
        book = {}
        for s in coins:
            book.update(RP.outcomes({**raw, "coins": {s: coins[s]}}, RP.BASE, lambda x, s=s: x["t"] in coin_skip[s]))
        rows.append({"name": f"random third skipped #{k + 1}", **RP._row("rand", "gate", book, filters, end, LIVE)})
    base = rows[0]
    for r in rows:
        r["verdict"] = RP.verdict(r, base)

    # 3) the mean-reversion engine for ranging markets
    fund = {s: c["funding"] for s, c in coins.items()}
    mr = {}
    for rname, reg in MR_REGIMES.items():
        for tp in MR_TPS:
            book = {s: DON.with_funding(mr_signals(c["candles"], feats[s], reg, tp), fund[s]) for s, c in coins.items()}
            per = {s: DON.one_per_coin(xs) for s, xs in book.items()}
            is_, oos = _split([(x["t"], x["r"]) for xs in per.values() for x in xs])
            pos = sum((R.score([x["r"] for x in xs if x["t"] >= IS_END])["exp"] or 0) > 0 for xs in per.values())
            mr[f"{rname} / tp {tp}"] = {"is": is_, "oos": oos, "coins_pos_oos": pos, "book": book}

    # 4) switch: v3 always (it already needs a trend) + mean reversion when ranging, one account
    v3_books = {"v3": {s: xs for s, xs in base_book.items()}}
    sw = {"v3 only": _acc_row(v3_books, filters, end)}
    best_is = sorted(mr, key=lambda k: -(mr[k]["is"]["exp"] or -9))[:3]  # the three best on 2022-24
    for k in best_is:
        sw[f"v3 + MR [{k}]"] = _acc_row({**v3_books, "mr": mr[k]["book"]}, filters, end)
    for k in best_is:
        sw[f"MR only [{k}]"] = _acc_row({"mr": mr[k]["book"]}, filters, end)

    return {"data_until": end, "signals": sum(len(c["signals"]) for c in coins.values()), "missing_feats": missing,
            "cuts": cuts, "buckets": buckets, "gates": rows,
            "mr": {k: {kk: vv for kk, vv in v.items() if kk != "book"} for k, v in mr.items()}, "switch": sw}


def _gate(name, raw, feats, filters, end, m, how, a, b) -> dict:
    """Skip v3 signals in the weak regime. how = "low"/"high" third (cuts a, b) or ">"/"<" a."""
    def skip_of(s):
        def skip(x):
            v = value(feats, s, x, m)
            if v is None:
                return False
            if how == "low":
                return v < a
            if how == "high":
                return v >= b
            return not (v > a if how == ">" else v < a)
        return skip
    book = {}
    for s in raw["coins"]:
        one = {**raw, "coins": {s: raw["coins"][s]}}
        book.update(RP.outcomes(one, RP.BASE, skip_of(s)))
    return {"name": name, **RP._row(name, "gate", book, filters, end, LIVE)}


def table(out: dict) -> str:
    L = [f"sig {out['signals']} miss {out['missing_feats']}"]
    L.append("BUCKETS v3 R/trade IS|OOS (n:exp)")
    for m, b in out["buckets"].items():
        L.append(f"{m:9} {out['cuts'][m]} " + " ".join(f"{g}={_short(v[0])}|{_short(v[1])}" for g, v in b.items()))
    L.append("GATES (acct 0.75x8) IS ret(jack) dd | OOS | R/trade IS/OOS")
    for r in out["gates"]:
        L.append(f"{r['name'][:24]:24} {r['is']['ret']:>6}({r['is']['jack']:>6}) {r['is']['dd']:>4} | "
                 f"{r['oos']['ret']:>6}({r['oos']['jack']:>6}) {r['oos']['dd']:>4} | "
                 f"{r['pt_is']['n']}:{r['pt_is']['exp']}/{r['pt_oos']['n']}:{r['pt_oos']['exp']} {r['verdict']}")
    L.append("MR per trade IS|OOS coins+")
    for k, v in out["mr"].items():
        L.append(f"{k[:30]:30} {_short(v['is'])}|{_short(v['oos'])} {v['coins_pos_oos']}")
    L.append("SWITCH acct IS ret(jack) dd | OOS")
    for k, v in out["switch"].items():
        L.append(f"{k[:40]:40} {v['is']['ret']:>6}({v['is']['jack']:>6}) {v['is']['dd']:>4} | "
                 f"{v['oos']['ret']:>6}({v['oos']['jack']:>6}) {v['oos']['dd']:>4}")
    return "\n".join(L)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "collect":
        syms = sys.argv[2].split(",") if len(sys.argv) > 2 else (P.load_saved() or {})["recommended"]
        bars = int(sys.argv[3]) if len(sys.argv) > 3 else 10500
        with gzip.open(RAW_PATH, "wt") as f:
            json.dump(collect(syms, bars), f, separators=(",", ":"))
    else:
        with gzip.open(RAW_PATH, "rt") as f:
            out = report(json.load(f))
        Path("/tmp/regime_report.json").write_text(json.dumps(out, separators=(",", ":")))
        print(table(out))
