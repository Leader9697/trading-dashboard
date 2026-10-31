"""What else could make the live v3 portfolio engine earn more?

The signal itself is fixed (rules v3, body filter, 22 engine coins, $60, 1% risk, 6
slots). Earlier research found the signal bar can't tell winners from losers, so this
module tests what happens around the signal instead, all in one script so every try
is counted:

  management   breakeven, trailing stops, a partial take profit, other targets
  exit timing  other time limits, closing a trade that is still losing halfway
  slots        tie-break order, pending limits that don't hold a slot, more slots
  sizing       smaller size after a loss or in a drawdown, bigger after a win
  filters      skip signals when funding is crowded on our side
  costs        what Binance actually charges (maker entry, BNB discount, limit TP)

Every variant is judged the same way: chosen on 2023-24 (per trade from 2022), checked
on 2025-26. A variant only counts as better when it wins in both periods and its
drawdown is not worse; the full table is printed so nothing is hidden.

Intrabar order is unknown on 4h candles, so the stop always wins on a bar that touches
both, and a stop that moves (breakeven, trailing) only applies from the next bar.
Funding paid or received while a trade is open is taken off every variant.

Two steps, both run where Binance is reachable (a Vercel Sandbox in sin1):
  python -m app.research_profit collect [symbols] [bars]   signals + the candles after them -> RAW_PATH
  python -m app.research_profit report                     every variant -> stdout (compact JSON)
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backtest_v3 as V
from . import portfolio as P
from . import research as R
from . import research_deriv as D
from . import research_entry as E
from .market import INTERVAL_SECONDS
from .setup import LIMIT_ATR, LIMIT_BARS, MAX_HOLD_BARS, MODES, STOP_ATR, TP_R, btc_regime

RAW_PATH = Path("/tmp/profit_raw.json.gz")
STEP = INTERVAL_SECONDS[MODES["swing"]["ltf"]]
AFTER = LIMIT_BARS + 70  # candles kept after each signal: fill window + the longest hold tested
TAKER, MAKER = 0.0004, 0.0004  # what the other backtests charge per side; see COSTS for Binance's real fees
T0, IS_END = P._ts(2023), P.IS_END
T_TRADES = P._ts(2022)  # per-trade stats start a year earlier


# ---------------------------------------------------------------- collect

def _collect_one(args: tuple) -> tuple[str, dict]:
    import httpx

    from .config import get_settings
    from .market import get_klines

    symbol, bars, regime = args
    cfg, settings = MODES["swing"], get_settings()
    l = get_klines(settings, symbol, cfg["ltf"], bars)[:-1]
    h = get_klines(settings, symbol, cfg["htf"], bars * STEP // INTERVAL_SECONDS[cfg["htf"]] + 320)
    start = datetime.fromtimestamp(l[0]["time"], timezone.utc) - timedelta(days=2)
    with httpx.Client() as client:
        fund = D.funding_history(client, symbol, int(start.timestamp() * 1000))
        met = D.metrics_history(client, symbol, start, datetime.now(timezone.utc) - timedelta(days=1))
    feats = D.features(l, STEP, fund, met)
    pre = R.precompute(symbol, "swing", h, l, regime)
    sigs = [i for i in E.signals(pre, D.make_allow(feats, E.V3), len(l)) if V.body_ok(pre, l, i)]
    out = []
    for i in sigs:
        p = pre[i]
        t = E.trade(p, l, i, V.ENTRY, V.STOP, TP_R, MAX_HOLD_BARS)  # the live rules, to check sim() against
        out.append({"t": l[i]["time"] + STEP, "side": p["side"][0], "score": abs(p["score"]), "close": l[i]["close"],
                    "atr": p["atr"], "atr_pct": p["atr_pct"], "fund_rank": feats[i]["funding_rank"],
                    "bars": [[b["time"], b["open"], b["high"], b["low"], b["close"]] for b in l[i + 1:i + 1 + AFTER]],
                    "ref_r": None if t is None else round(t["r"], 4)})
    print(f"{symbol}: {len(l)} bars, {len(sigs)} signals", file=sys.stderr, flush=True)
    return symbol, {"last": l[-1]["time"] + STEP, "signals": out, "funding": fund}


def collect(symbols: list[str], bars: int) -> dict:
    from .config import get_settings
    from .market import get_klines

    btc = get_klines(get_settings(), "BTCUSDT", MODES["swing"]["htf"], bars * STEP // 86400 + 320)
    regime = btc_regime(btc)
    with ProcessPoolExecutor(max_workers=6) as pool:
        coins = dict(pool.map(_collect_one, [(s, bars, regime) for s in symbols]))
    return {"filters": P.exchange_filters(symbols), "coins": coins}


# ---------------------------------------------------------------- one trade

BASE = {"tp": TP_R, "hold": MAX_HOLD_BARS, "be_at": None, "be_lock": 0.0, "trail_at": None, "trail_atr": None,
        "lock": None, "part_at": None, "part": 0.0, "stale": None, "fees": (MAKER, TAKER, TAKER)}


def sim(sig: dict, cfg: dict) -> dict | None:
    """The trade from one signal under `cfg`, with the live limit entry and stop. None when
    the limit never filled. fees = (entry, stop/market exit, target exit) per side."""
    bars, long = sig["bars"], sig["side"] == "l"
    sign = 1 if long else -1
    close, atr = sig["close"], sig["atr"]
    limit, sl = close - sign * LIMIT_ATR * atr, close - sign * STOP_ATR * atr
    hit_sl = lambda b, s: (b[3] <= s) if long else (b[2] >= s)
    fill = None
    for j in range(min(LIMIT_BARS, len(bars))):
        b = bars[j]
        touched = (b[3] <= limit) if long else (b[2] >= limit)
        if touched:
            entry, fill = (min(b[1], limit) if long else max(b[1], limit)), j
        if touched or hit_sl(b, sl):
            break
    if fill is None:
        return None
    risk = sign * (entry - sl)
    f_in, f_out, f_tp = cfg["fees"]
    fee = lambda rate, px, size: rate * px * size / risk
    row = {"fill": bars[fill][0], "entry": entry, "sl_pct": 100 * risk / entry}
    if hit_sl(bars[fill], sl):
        return {**row, "end": bars[fill][0] + STEP, "r": -1 - fee(f_in, entry, 1) - fee(f_out, sl, 1), "status": "stop"}
    start = fill + 1
    end = start + cfg["hold"]
    if end >= len(bars):
        return None  # still open at the end of the data
    tp = entry + sign * cfg["tp"] * risk if cfg["tp"] else None
    stop, size, booked, best = sl, 1.0, -fee(f_in, entry, 1), entry
    part_px = entry + sign * cfg["part_at"] * risk if cfg["part_at"] else None
    r_of = lambda px: sign * (px - entry) / risk
    status = "timeout"
    for j in range(start, end + 1):
        b = bars[j]
        if hit_sl(b, stop):
            booked += size * r_of(stop) - fee(f_out, stop, size)
            status = "stop" if stop == sl else "trail"
            break
        if part_px is not None and size == 1.0 and ((b[2] >= part_px) if long else (b[3] <= part_px)):
            booked += cfg["part"] * r_of(part_px) - fee(f_tp, part_px, cfg["part"])
            size -= cfg["part"]
        if tp is not None and ((b[2] >= tp) if long else (b[3] <= tp)):
            booked += size * cfg["tp"] - fee(f_tp, tp, size)
            status = "tp"
            break
        if cfg["stale"] and j - start + 1 == cfg["stale"] and r_of(b[4]) < 0:
            booked += size * r_of(b[4]) - fee(f_out, b[4], size)
            status = "stale"
            break
        # stops that move only apply from the next bar
        best = max(best, b[2]) if long else min(best, b[3])
        mfe = r_of(best)
        if cfg["be_at"] is not None and mfe >= cfg["be_at"]:
            stop = _tighter(long, stop, entry + sign * cfg["be_lock"] * risk)
        if cfg["lock"]:
            for at, keep in cfg["lock"]:
                if mfe >= at:
                    stop = _tighter(long, stop, entry + sign * keep * risk)
        if cfg["trail_at"] is not None and mfe >= cfg["trail_at"]:
            stop = _tighter(long, stop, best - sign * cfg["trail_atr"] * atr)
        if part_px is not None and size < 1.0:
            stop = _tighter(long, stop, entry)
    else:
        booked += size * r_of(bars[end][4]) - fee(f_out, bars[end][4], size)
        j = end
    return {**row, "end": bars[j][0] + STEP, "r": booked, "status": status}


def _tighter(long: bool, a: float, b: float) -> float:
    return max(a, b) if long else min(a, b)


def outcomes(raw: dict, cfg: dict, skip=None) -> dict[str, list[dict]]:
    """Every signal as the account replay sees it: t, side, score, end, entry, sl_pct, r,
    plus the fill time. Funding while the trade is open is taken off its R."""
    book = {}
    for s, c in raw["coins"].items():
        f_t = [t for t, _ in c["funding"]]
        cum = [0.0]
        for _, rate in c["funding"]:
            cum.append(cum[-1] + rate)
        rows = []
        for sig in c["signals"]:
            if skip and skip(sig):
                continue
            row = {"t": sig["t"], "side": sig["side"], "score": sig["score"], "atr_pct": sig["atr_pct"]}
            o = sim(sig, cfg)
            sign = 1 if sig["side"] == "l" else -1
            if o is None:
                limit = sig["close"] - sign * LIMIT_ATR * sig["atr"]
                row.update(end=sig["t"] + LIMIT_BARS * STEP, entry=limit,
                           sl_pct=100 * (STOP_ATR - LIMIT_ATR) * sig["atr"] / limit)
            else:
                paid = cum[bisect.bisect_right(f_t, o["end"])] - cum[bisect.bisect_right(f_t, o["fill"])]
                row.update(o, r=o["r"] - sign * paid * 100 / o["sl_pct"])
            rows.append(row)
        book[s] = rows
    return book


# ---------------------------------------------------------------- account

ACCOUNT = {"risk": 1.0, "max_pos": 6, "pending_slot": True, "order": "score", "after_loss": 1.0,
           "after_win": 1.0, "dd_cut": None, "seed": 0, "drop": None}
JACK = 10  # account replays per window, each with a random 10% of signals left out


def replay(book: dict[str, list[dict]], filters: dict, start: int, end: int, acc: dict) -> dict:
    """One $60 account, as portfolio.replay: one position per coin, max_pos slots, size at the
    balance of the moment in whole lot steps, Binance minimum order and margin.

    With pending_slot False a limit order does not hold a slot: every signal gets its
    order, and a fill is only kept when a slot is free at the fill (else it is cancelled).
    """
    rng = random.Random(acc["seed"])
    order = {"score": lambda s, x: -x["score"], "tight": lambda s, x: x["sl_pct"], "wide": lambda s, x: -x["sl_pct"],
             "random": lambda s, x: rng.random()}[acc["order"]]
    keep = random.Random(acc["drop"]) if acc["drop"] is not None else None
    sigs = [(x["t"], order(s, x), s, x) for s, xs in book.items() for x in xs
            if start <= x["t"] < end and (keep is None or keep.random() >= 0.1)]
    sigs.sort(key=lambda e: e[:2])
    open_: list[list] = []  # [end, symbol, pnl, margin, r, filled]
    eq, peak, dd = P.EQUITY, P.EQUITY, 0.0
    last = {"r": 0.0}
    trades = []

    def close_until(t: int):
        nonlocal eq, peak, dd
        for pos in sorted((p for p in open_ if p[0] <= t), key=lambda p: p[0]):
            open_.remove(pos)
            if not pos[5]:
                continue
            eq += pos[2]
            last["r"] = pos[4]
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)

    def mult() -> float:
        m = acc["after_loss"] if last["r"] < 0 else acc["after_win"] if last["r"] > 0 else 1.0
        if acc["dd_cut"] and eq < peak * (1 - acc["dd_cut"][0]):
            m *= acc["dd_cut"][1]
        return m

    if acc["pending_slot"]:
        for t, _, s, x in sigs:
            close_until(t)
            if any(p[1] == s for p in open_) or len(open_) >= acc["max_pos"]:
                continue
            z = P.size(x, eq, acc["risk"] * mult(), filters[s])
            if z is None or z[1] + sum(p[3] for p in open_) > eq:
                continue
            if "r" not in x:
                open_.append([x["end"], s, 0.0, 0.0, 0.0, False])
                continue
            pnl = x["r"] * z[0] * x["entry"] * x["sl_pct"] / 100
            open_.append([x["end"], s, pnl, z[1], x["r"], True])
            trades.append({"s": s, "t": x["t"], "r": x["r"], "pnl": pnl})
    else:
        pending: dict[str, tuple] = {}  # symbol -> (fill time or order expiry, signal, qty, margin)
        events = [(t, 1, o, s, x) for t, o, s, x in sigs]
        while events:
            events.sort(key=lambda e: (e[0], e[1], e[2]))
            t, kind, _, s, x = events.pop(0)
            close_until(t)
            if kind == 1:  # a signal: place its limit unless the coin is busy
                if s in pending or any(p[1] == s for p in open_):
                    continue
                pending[s] = x
                events.append((x.get("fill", x["end"]), 0, 0, s, x))
            else:  # the limit filled (or expired)
                pending.pop(s, None)
                if "r" not in x or len(open_) >= acc["max_pos"]:
                    continue
                z = P.size(x, eq, acc["risk"] * mult(), filters[s])
                if z is None or z[1] + sum(p[3] for p in open_) > eq:
                    continue
                pnl = x["r"] * z[0] * x["entry"] * x["sl_pct"] / 100
                open_.append([x["end"], s, pnl, z[1], x["r"], True])
                trades.append({"s": s, "t": x["t"], "r": x["r"], "pnl": pnl})
    close_until(end + 10 ** 10)
    years: dict[str, float] = {}
    for tr in trades:
        y = str(datetime.fromtimestamp(tr["t"], timezone.utc).year)
        years[y] = round(years.get(y, 0.0) + tr["pnl"], 1)
    sc = R.score([tr["r"] for tr in trades])
    return {"n": sc["n"], "exp": sc["exp"], "ret": round(100 * (eq / P.EQUITY - 1), 1), "dd": round(100 * dd, 1),
            "years": years}


def per_trade(book: dict[str, list[dict]], start: int, end: int) -> dict:
    """Pooled one-position-per-coin trades, no account: is each trade better in R?"""
    rs = []
    for xs in book.values():
        free = 0
        for x in xs:
            if "r" in x and x["t"] >= free:
                if start <= x["t"] < end:
                    rs.append(x["r"])
                free = x["end"]
    s = R.score(rs)
    return {"n": s["n"], "exp": s["exp"], "wr": s["wr"], "total": round(sum(rs), 1)}


# ---------------------------------------------------------------- variants

def _m(**kw) -> dict:
    return {**BASE, **kw}


MANAGE = {
    "baseline (live)": _m(),
    "BE at 1R": _m(be_at=1.0, be_lock=0.1),
    "BE at 1.5R": _m(be_at=1.5, be_lock=0.1),
    "BE at 2R": _m(be_at=2.0, be_lock=0.1),
    "lock 1R after 2R": _m(lock=((2.0, 1.0),)),
    "lock 1R@2R, 2R@3R": _m(lock=((2.0, 1.0), (3.0, 2.0))),
    "trail 2ATR after 1R": _m(trail_at=1.0, trail_atr=2.0),
    "trail 3ATR after 2R": _m(trail_at=2.0, trail_atr=3.0),
    "trail 3ATR after 2R, no TP": _m(tp=None, trail_at=2.0, trail_atr=3.0, hold=60),
    "half at 2R, rest 4R (BE)": _m(part_at=2.0, part=0.5),
    "third at 1.5R, rest 4R (BE)": _m(part_at=1.5, part=1 / 3),
    "TP 3R": _m(tp=3.0),
    "TP 5R": _m(tp=5.0),
    "TP 6R": _m(tp=6.0),
    "TP 5R, hold 45": _m(tp=5.0, hold=45),
    "hold 20 bars": _m(hold=20),
    "hold 45 bars": _m(hold=45),
    "hold 60 bars": _m(hold=60),
    "close if losing after 12 bars": _m(stale=12),
    "close if losing after 18 bars": _m(stale=18),
}
ACCOUNTS = {
    "order: tightest stop first": {"order": "tight"},
    "order: widest stop first": {"order": "wide"},
    "order: random (5 seeds avg)": {"order": "random"},
    "pending limits hold no slot": {"pending_slot": False},
    "8 slots": {"max_pos": 8},
    "10 slots": {"max_pos": 10},
    "8 slots, 0.75% risk": {"max_pos": 8, "risk": 0.75},
    "10 slots, 0.75% risk": {"max_pos": 10, "risk": 0.75},
    "pending free + 8 slots": {"pending_slot": False, "max_pos": 8},
    "half size after a loss": {"after_loss": 0.5},
    "1.5x size after a win": {"after_win": 1.5},
    "half size in a 20% drawdown": {"dd_cut": (0.20, 0.5)},
    "1.25% risk": {"risk": 1.25},
}
FILTERS = {
    "skip crowded funding (rank>0.8 our side)": lambda x: x["fund_rank"] is not None and (
        x["fund_rank"] > 0.8 if x["side"] == "l" else x["fund_rank"] < 0.2),
    "skip calm coins (ATR% bottom third)": None,  # filled in report(): needs the cut from 2022-24 only
}
# Binance USDT-M VIP0: maker 0.02%, taker 0.05%; paying fees in BNB takes 10% off.
COSTS = {
    "backtest assumption (0.04% each side)": (0.0004, 0.0004, 0.0004),
    "Binance real: maker entry, taker exits": (0.0002, 0.0005, 0.0005),
    "real + BNB discount": (0.00018, 0.00045, 0.00045),
    "real + BNB + TP as limit (maker)": (0.00018, 0.00045, 0.00018),
}


def _row(name: str, kind: str, book: dict, filters: dict, end: int, acc: dict, trade_stats: bool = True) -> dict:
    """One account run per window, plus the median of JACK runs that each leave out a random
    10% of signals: with $60 and a minimum order, one skipped trade can change the whole path."""
    row = {"name": name, "kind": kind}
    for w, (a, b) in {"is": (T0, IS_END), "oos": (IS_END, end)}.items():
        row[w] = replay(book, filters, a, b, acc)
        jack = sorted(replay(book, filters, a, b, {**acc, "drop": 100 + k})["ret"] for k in range(JACK))
        row[w]["jack"] = jack[JACK // 2]
    if trade_stats:
        row["pt_is"], row["pt_oos"] = per_trade(book, T_TRADES, IS_END), per_trade(book, IS_END, end)
    return row


def report(raw: dict) -> dict:
    coins = {s: c for s, c in raw["coins"].items() if s in raw["filters"]}
    raw = {**raw, "coins": coins}
    filters, end = raw["filters"], max(c["last"] for c in coins.values())

    # sim() with the live rules must reproduce research_entry.trade (before funding)
    # (fees are split per side here, so small differences are expected; 9 = filled in one only)
    diffs = [abs(o["r"] - x["ref_r"]) if o and x["ref_r"] is not None else (0 if o is None and x["ref_r"] is None else 9)
             for c in coins.values() for x in c["signals"] for o in [sim(x, BASE)]]
    rows = []
    base_book = outcomes(raw, BASE)
    for name, cfg in MANAGE.items():
        book = base_book if name == "baseline (live)" else outcomes(raw, cfg)
        rows.append(_row(name, "manage", book, filters, end, ACCOUNT))
    for name, over in ACCOUNTS.items():
        if over.get("order") == "random":
            runs = [_row(name, "account", base_book, filters, end, {**ACCOUNT, **over, "seed": k}, False) for k in range(5)]
            avg = lambda w, f: round(sum(r[w][f] for r in runs) / len(runs), 1)
            rows.append({"name": name, "kind": "account",
                         **{w: {"ret": avg(w, "ret"), "dd": avg(w, "dd"), "jack": avg(w, "jack")} for w in ("is", "oos")}})
            continue
        rows.append(_row(name, "account", base_book, filters, end, {**ACCOUNT, **over}, False))
    calm = sorted(x["atr_pct"] for c in coins.values() for x in c["signals"] if x["t"] < IS_END)
    cut = calm[len(calm) // 3]
    FILTERS["skip calm coins (ATR% bottom third)"] = lambda x: x["atr_pct"] < cut
    for name, skip in FILTERS.items():
        rows.append(_row(name, "filter", outcomes(raw, BASE, skip), filters, end, ACCOUNT))
    for name, fees in COSTS.items():
        rows.append(_row(name, "cost", outcomes(raw, {**BASE, "fees": fees}), filters, end, ACCOUNT))

    base = rows[0]
    for r in rows:
        r["verdict"] = verdict(r, base)
    return {"data_until": end, "signals": sum(len(c["signals"]) for c in coins.values()),
            "check": {"diff_over_0.02r": sum(0.02 < d < 9 for d in diffs), "one_side_only": sum(d == 9 for d in diffs)}, "rows": rows}


def verdict(r: dict, base: dict) -> str:
    """Better only if it beats the live engine on 2023-24 AND on 2025-26, in the plain run and
    in the leave-10%-out median, without a deeper drawdown, and (when the trades change) per
    trade in R too."""
    if r is base:
        return "baseline"

    def win(w: str) -> bool:
        ok = r[w]["ret"] > base[w]["ret"] and r[w]["jack"] > base[w]["jack"] and r[w]["dd"] <= base[w]["dd"] + 3
        pt = "pt_" + w
        return ok and (pt not in r or r[pt]["exp"] > base[pt]["exp"])
    win_is, win_oos = win("is"), win("oos")
    if win_is and win_oos:
        return "better"
    if win_is:
        return "is only"
    if win_oos:
        return "oos only"
    return "worse"


def table(out: dict) -> str:
    lines = [f"signals {out['signals']}  check {out['check']}"]
    for r in out["rows"]:
        pt = f" | R {r['pt_is']['exp']}/{r['pt_oos']['exp']}" if "pt_is" in r else ""
        lines.append(f"{r['name'][:30]:30} IS {r['is']['ret']:>6}% ({r['is']['jack']:>6}) dd{r['is']['dd']:>5} | "
                     f"OOS {r['oos']['ret']:>6}% ({r['oos']['jack']:>6}) dd{r['oos']['dd']:>5}{pt} | {r['verdict']}")
    return "\n".join(lines)


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
        Path("/tmp/profit_report.json").write_text(json.dumps(out, separators=(",", ":")))
        print(table(out))
