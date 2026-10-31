"""Donchian VF (a 4h channel breakout) next to the v3 portfolio engine, in one $60 account.

The rules follow the DonchianVF_v6 Pine script as written: a close above the highest high
(below the lowest low) of the previous 10 bars, taken only when the 100-bar realised
volatility ranks at or under 0.60 among its last 500 values. Entry at the next bar's
open; stop 1.5 ATR(14) and target 2.5x that distance, both measured from the signal
close; out at the close 120 bars after entry. The size is halved after a losing trade
and back to normal after a win.

Two places where this test is stricter than the script: the stop and target are also
checked on the entry bar (the script starts on the bar after), and a bar that opens
past the stop fills at that open, not at the stop. Costs are the 0.04% taker fee per
side the rest of the backtests use, plus 0.02% slippage per side because entry and
stop are market orders, plus the real Binance funding paid or received while the
trade is open (applied to v3 trades too, so both sides carry it).

The preset was not tuned here. It was picked by its author on BTC, ETH, SOL, BNB and
XRP up to 2026, so for those five coins 2025-26 is not truly unseen; the other
engine coins are.

Two steps, both run where Binance is reachable (a Vercel Sandbox in sin1):
  python -m app.research_donchian collect [symbols] [bars]   signals, outcomes, funding -> SIGNALS_PATH
  python -m app.research_donchian report                     needs portfolio.SIGNALS_PATH too
"""

from __future__ import annotations

import bisect
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import portfolio as P
from . import research as R
from .backtest import FEE_ROUND_TRIP

SIGNALS_PATH = Path("/tmp/donchian_signals.json")
STEP = 4 * 3600
N, ATR_LEN, SL_ATR, RR, MAX_BARS = 10, 14, 1.5, 2.5, 120
VOL_LEN, RANK_LEN, VOL_MAX = 100, 500, 0.60
SLIP_ROUND_TRIP = 0.0004  # 0.02% per side on market orders
CUT = 0.5  # size after a losing trade
SANITY = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")  # the coins the preset was chosen on


# ---------------------------------------------------------------- signals

def atr(l: list[dict], n: int = ATR_LEN) -> list[float | None]:
    """Wilder's ATR, as Pine's ta.atr."""
    out: list[float | None] = [None] * len(l)
    tr = [l[0]["high"] - l[0]["low"]] + [
        max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(l, l[1:])]
    if len(l) < n:
        return out
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for i in range(n, len(l)):
        v = (v * (n - 1) + tr[i]) / n
        out[i] = v
    return out


def vol_rank(l: list[dict], vol_len: int = VOL_LEN, rank_len: int = RANK_LEN) -> list[float | None]:
    """Population stdev of 1-bar returns over `vol_len`, then Pine's ta.percentrank: the share
    of the previous `rank_len` values that are less than or equal to the current one."""
    rets = [None] + [b["close"] / a["close"] - 1 for a, b in zip(l, l[1:])]
    rv: list[float | None] = [None] * len(l)
    for i in range(vol_len, len(l)):
        w = rets[i - vol_len + 1:i + 1]
        m = sum(w) / vol_len
        rv[i] = math.sqrt(sum((x - m) ** 2 for x in w) / vol_len)
    out: list[float | None] = [None] * len(l)
    for i in range(vol_len + rank_len, len(l)):
        past = rv[i - rank_len:i]
        out[i] = sum(1 for x in past if x <= rv[i]) / rank_len
    return out


def signals(l: list[dict]) -> list[tuple[int, str]]:
    """(bar index, side) for every closed bar that breaks the channel with volatility allowed."""
    vr = vol_rank(l)
    warm = VOL_LEN + RANK_LEN + 5
    out = []
    for i in range(max(warm + 1, N), len(l)):
        if vr[i] is None or vr[i] > VOL_MAX:
            continue
        w = l[i - N:i]
        if l[i]["close"] > max(b["high"] for b in w):
            out.append((i, "long"))
        elif l[i]["close"] < min(b["low"] for b in w):
            out.append((i, "short"))
    return out


def outcome(l: list[dict], i: int, side: str, a: float) -> dict | None:
    """What the trade from the signal on bar i did, or None when there is no next bar or the
    next open is already past the stop or target."""
    e = i + 1
    if e >= len(l) or a is None:
        return None
    long = side == "long"
    sign = 1 if long else -1
    sl = l[i]["close"] - sign * SL_ATR * a
    tp = l[i]["close"] + sign * SL_ATR * a * RR
    entry = l[e]["open"]
    risk = sign * (entry - sl)
    if risk <= 0 or sign * (tp - entry) <= 0:
        return None
    last = min(len(l) - 1, e + MAX_BARS)
    exit_px, j, status = l[last]["close"], last, "timeout"
    for j in range(e, last + 1):
        b = l[j]
        if (b["low"] <= sl) if long else (b["high"] >= sl):
            exit_px, status = (min(b["open"], sl) if long else max(b["open"], sl)) if j > e else sl, "stop"
            break
        if (b["high"] >= tp) if long else (b["low"] <= tp):
            exit_px, status = (max(b["open"], tp) if long else min(b["open"], tp)) if j > e else tp, "tp"
            break
    if status == "timeout" and last < e + MAX_BARS:
        return None  # still open at the end of the data
    r = sign * (exit_px - entry) / risk - (FEE_ROUND_TRIP + SLIP_ROUND_TRIP) * entry / risk
    return {"t": l[e]["time"], "side": side[0], "score": -1, "end": l[j]["time"] + STEP, "entry": entry,
            "sl_pct": round(100 * risk / entry, 3), "r": round(r, 3), "status": status}


def coin_signals(l: list[dict]) -> list[dict]:
    a = atr(l)
    return [o for i, side in signals(l) if (o := outcome(l, i, side, a[i])) is not None]


# ---------------------------------------------------------------- collect

def _collect_one(args: tuple) -> tuple[str, dict]:
    import httpx

    from . import research_deriv as D
    from .config import get_settings
    from .market import get_klines

    symbol, bars = args
    l = get_klines(get_settings(), symbol, "4h", bars)[:-1]  # closed bars only
    with httpx.Client() as client:
        fund = D.funding_history(client, symbol, l[0]["time"] * 1000)
    sigs = coin_signals(l)
    print(f"{symbol}: {len(l)} bars, {len(sigs)} signals, {len(fund)} funding rows", file=sys.stderr, flush=True)
    return symbol, {"first": l[0]["time"], "last": l[-1]["time"] + STEP, "signals": sigs, "funding": fund}


def collect(symbols: list[str], bars: int) -> dict:
    with ProcessPoolExecutor(max_workers=6) as pool:
        return {"coins": dict(pool.map(_collect_one, [(s, bars) for s in symbols]))}


# ---------------------------------------------------------------- account replay

def with_funding(sigs: list[dict], funding: list[tuple[int, float]]) -> list[dict]:
    """Take the funding paid while each trade was open off its R (longs pay positive rates)."""
    times = [t for t, _ in funding]
    cum = [0.0]
    for _, rate in funding:
        cum.append(cum[-1] + rate)
    out = []
    for s in sigs:
        if "r" not in s:
            out.append(s)
            continue
        paid = cum[bisect.bisect_right(times, s["end"])] - cum[bisect.bisect_right(times, s["t"])]
        sign = 1 if s["side"] == "l" else -1
        out.append({**s, "r": round(s["r"] - sign * paid * 100 / s["sl_pct"], 3), "fund_r": round(-sign * paid * 100 / s["sl_pct"], 3)})
    return out


def replay(books: dict[str, dict[str, list[dict]]], filters: dict, start: int, end: int, risk_pct: float,
           max_pos: int, cut: dict[str, float] | None = None, caps: dict[str, int] | None = None,
           equity: float = P.EQUITY) -> dict:
    """One account through the signals of several strategies, books = {strategy: {symbol: signals}}.

    Same steps as portfolio.replay: one position per coin (across strategies, as Binance
    one-way mode allows), a shared slot limit, order size at the balance of the moment,
    minimum order and margin. `cut[strategy]` scales that strategy's size after its last
    closed trade lost; `caps[strategy]` optionally limits its own open positions.
    Signals at the same time are taken v3 first (it carries the higher score).
    """
    cut, caps = cut or {}, caps or {}
    events = sorted(((k, s, sig) for k, book in books.items() for s, sigs in book.items() for sig in sigs
                     if start <= sig["t"] < end), key=lambda e: (e[2]["t"], -e[2]["score"]))
    open_: list[tuple] = []  # (end, symbol, pnl, margin, strategy, r)
    last_loss = {k: False for k in books}
    eq, peak, dd = equity, equity, 0.0
    trades, skipped = [], {"busy": 0, "slots": 0, "min_order": 0, "margin": 0, "unfilled": 0}

    def close_until(t: int):
        nonlocal eq, peak, dd
        for pos in sorted(p for p in open_ if p[0] <= t):
            open_.remove(pos)
            if pos[3] == 0:
                continue
            eq += pos[2]
            last_loss[pos[4]] = pos[5] <= 0
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)

    for k, symbol, sig in events:
        close_until(sig["t"])
        if any(p[1] == symbol for p in open_):
            skipped["busy"] += 1
            continue
        if len(open_) >= max_pos or (k in caps and sum(p[4] == k for p in open_) >= caps[k]):
            skipped["slots"] += 1
            continue
        if "sl_pct" not in sig:
            skipped["unfilled"] += 1
            continue
        mult = cut.get(k, 1.0) if last_loss[k] else 1.0
        z = P.size(sig, eq, risk_pct * mult, filters[symbol])
        if z is None:
            skipped["min_order"] += 1
            continue
        if z[1] + sum(p[3] for p in open_) > eq:
            skipped["margin"] += 1
            continue
        if "r" not in sig:
            open_.append((sig["end"], symbol, 0.0, 0.0, k, 0.0))  # the pending v3 limit held the slot
            skipped["unfilled"] += 1
            continue
        qty, margin = z
        pnl = sig["r"] * qty * sig["entry"] * sig["sl_pct"] / 100
        open_.append((sig["end"], symbol, pnl, margin, k, sig["r"]))
        trades.append({"k": k, "s": symbol, "t": sig["t"], "r": sig["r"], "pnl": pnl})
    close_until(end + 10 ** 9)
    out = {"trades": len(trades), **{x: v for x, v in R.score([t["r"] for t in trades]).items() if x != "n"},
           "final": round(eq, 2), "return_pct": round(100 * (eq / equity - 1), 1), "max_dd_pct": round(100 * dd, 1),
           "skipped": skipped}
    if len(books) > 1:
        out["by_strategy"] = {k: {"trades": sum(t["k"] == k for t in trades),
                                  "pnl": round(sum(t["pnl"] for t in trades if t["k"] == k), 2)} for k in books}
    out["years"] = {}
    for t in trades:
        y = out["years"].setdefault(str(datetime.fromtimestamp(t["t"], timezone.utc).year), {"trades": 0, "pnl": 0.0})
        y["trades"] += 1
        y["pnl"] = round(y["pnl"] + t["pnl"], 2)
    return out


# ---------------------------------------------------------------- report

def _ts(y: int) -> int:
    return int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())


def one_per_coin(sigs: list[dict]) -> list[dict]:
    """The trades a single-coin bot would take: the next signal only after the last one closed."""
    free, out = 0, []
    for s in sigs:
        if "r" in s and s["t"] >= free:
            out.append(s)
            free = s["end"]
    return out


def monthly_corr(a: list[dict], b: list[dict]) -> float | None:
    def months(ts):
        m: dict[str, float] = {}
        for t in ts:
            k = datetime.fromtimestamp(t["t"], timezone.utc).strftime("%Y-%m")
            m[k] = m.get(k, 0.0) + t["r"]
        return m
    ma, mb = months(a), months(b)
    keys = sorted(set(ma) | set(mb))
    xa, xb = [ma.get(k, 0.0) for k in keys], [mb.get(k, 0.0) for k in keys]
    if len(keys) < 3:
        return None
    mx, my = sum(xa) / len(xa), sum(xb) / len(xb)
    cov = sum((x - mx) * (y - my) for x, y in zip(xa, xb))
    vx, vy = sum((x - mx) ** 2 for x in xa), sum((y - my) ** 2 for y in xb)
    return round(cov / math.sqrt(vx * vy), 2) if vx and vy else None


def report(don: dict, v3raw: dict, coins: list[str]) -> dict:
    filters = v3raw["filters"]
    fund = {s: c["funding"] for s, c in don["coins"].items()}
    t0, t_is, t_end = _ts(P.FIRST_YEAR), P.IS_END, min(c["last"] for c in don["coins"].values())

    # 1) per coin, no account: what a one-coin bot gets in R (2022 start, before and after 2025)
    per_coin = {}
    for s, c in don["coins"].items():
        tr = one_per_coin(with_funding(c["signals"], fund[s]))
        is_, oos = [t["r"] for t in tr if t["t"] < t_is], [t["r"] for t in tr if t["t"] >= t_is]
        per_coin[s] = {"is": R.score(is_), "oos": R.score(oos),
                       "fund_r": round(sum(t["fund_r"] for t in tr) / len(tr), 3) if tr else None,
                       "exits": {x: sum(t["status"] == x for t in tr) for x in ("stop", "tp", "timeout")}}
    pool = lambda keep, when: R.score([t["r"] for s in keep if s in don["coins"] for t in
                                       one_per_coin(with_funding(don["coins"][s]["signals"], fund[s])) if when(t["t"])])
    pooled = {"chosen_on_5": {"is": pool(SANITY, lambda t: t < t_is), "oos": pool(SANITY, lambda t: t >= t_is)},
              "other_coins": {"is": pool([s for s in coins if s not in SANITY], lambda t: t < t_is),
                           "oos": pool([s for s in coins if s not in SANITY], lambda t: t >= t_is)}}

    # 2) one $60 account, the 22 engine coins, 1% risk
    d_book = {s: with_funding(don["coins"][s]["signals"], fund[s]) for s in coins if s in don["coins"]}
    v_book = {s: with_funding(v3raw["coins"][s]["signals"], fund.get(s, [])) for s in coins if s in v3raw["coins"]}
    v_raw = {s: v3raw["coins"][s]["signals"] for s in coins if s in v3raw["coins"]}
    variants = {
        "v3 (as live, no funding)": ({"v3": v_raw}, {}, 6, {}),
        "v3": ({"v3": v_book}, {}, 6, {}),
        "donchian": ({"don": d_book}, {"don": CUT}, 6, {}),
        "donchian, no size cut": ({"don": d_book}, {}, 6, {}),
        "combined, 6 shared": ({"v3": v_book, "don": d_book}, {"don": CUT}, 6, {}),
        "combined, 3 + 3": ({"v3": v_book, "don": d_book}, {"don": CUT}, 6, {"v3": 3, "don": 3}),
        "combined, 8 shared": ({"v3": v_book, "don": d_book}, {"don": CUT}, 8, {}),
    }
    accounts = {}
    for name, (books, cut, max_pos, caps) in variants.items():
        accounts[name] = {w: replay(books, filters, a, b, 1.0, max_pos, cut, caps)
                          for w, (a, b) in {"is": (t0, t_is), "oos": (t_is, t_end), "all": (t0, t_end)}.items()}

    # 3) do the two strategies lose in the same months?
    v_tr = [t for s in v_book for t in one_per_coin(v_book[s])]
    d_tr = [t for s in d_book for t in one_per_coin(d_book[s])]
    return {"data_until": t_end, "is_end": t_is, "first_year": P.FIRST_YEAR, "coins": coins,
            "per_coin": per_coin, "pooled": pooled, "accounts": accounts,
            "monthly_r_corr": {"is": monthly_corr([t for t in v_tr if t["t"] < t_is], [t for t in d_tr if t["t"] < t_is]),
                               "oos": monthly_corr([t for t in v_tr if t["t"] >= t_is], [t for t in d_tr if t["t"] >= t_is])}}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    engine_coins = (P.load_saved() or {}).get("recommended") or []
    if cmd == "collect":
        syms = sys.argv[2].split(",") if len(sys.argv) > 2 else sorted(set(engine_coins) | set(SANITY))
        bars = int(sys.argv[3]) if len(sys.argv) > 3 else 11200  # 10500 like v3, plus the 605-bar warm-up
        SIGNALS_PATH.write_text(json.dumps(collect(syms, bars), separators=(",", ":")))
    else:
        out = report(json.loads(SIGNALS_PATH.read_text()), json.loads(P.SIGNALS_PATH.read_text()), engine_coins)
        print(json.dumps(out, separators=(",", ":")))
