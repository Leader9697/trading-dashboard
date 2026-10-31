"""Portfolio engine for a small account: which coins to trade, and how many at once.

The v3 signal is tested per coin elsewhere (backtest_v3). With $60 the questions are
different: several coins can signal at the same time, every position needs margin,
and Binance refuses orders below a minimum value (BTC 100 USDT, ETH 20, most others
5) or not a whole number of lot steps. This module replays every v3 signal of a
wider list of coins through one account, in time order, with those limits.

Coins are picked the way a live engine would have to: at the start of each year,
from trades that closed before it. That walk-forward pick is compared with simply
trading every coin that fits the account, so a coin list that only looks good in
hindsight shows up as no better than taking everything.

Two steps, both run where Binance is reachable (a Vercel Sandbox in sin1):
  python -m app.portfolio collect [symbols] [ltf_bars]   every signal + its outcome -> SIGNALS_PATH
  python -m app.portfolio build                          account replay -> app/data/portfolio.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backtest_v3 as V
from . import research as R
from . import research_deriv as D
from . import research_entry as E
from .market import INTERVAL_SECONDS
from .setup import LIMIT_BARS, MAX_HOLD_BARS, MODES, TP_R, btc_regime

PATH = Path(__file__).resolve().parent / "data" / "portfolio.json"
SIGNALS_PATH = Path("/tmp/portfolio_signals.json")
STEP = INTERVAL_SECONDS[MODES["swing"]["ltf"]]

# USDT-M perpetuals that also trade on spot (the candles come from spot), largest by
# volume, still listed today. Coins that died on the way (LUNA, FTT) can't be in a
# list made today, so results lean optimistic: survivorship bias.
CANDIDATES = (
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,DOTUSDT,LTCUSDT,TRXUSDT,"
    "BCHUSDT,NEARUSDT,ATOMUSDT,UNIUSDT,FILUSDT,ETCUSDT,AAVEUSDT,XLMUSDT,ALGOUSDT,SANDUSDT,AXSUSDT,"
    "ICPUSDT,HBARUSDT,INJUSDT,APTUSDT,OPUSDT,ARBUSDT,SUIUSDT"
).split(",")

EQUITY = 60.0
FIRST_YEAR = 2023  # coin picks start here; 2022 is the first year of trades to pick from
IS_END = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())  # account settings are chosen before this
MAX_LEVERAGE = 20
GRID = {"risk_pct": (1.0, 1.5, 2.0), "max_pos": (2, 3, 4, 6)}
MIN_PAST_TRADES = 8


# ---------------------------------------------------------------- collect

def signal_outcomes(pre: list, ltf: list[dict], sigs: list[int]) -> list[dict]:
    """Every v3 signal with what its limit order would have done, without the one-position
    rule: the account replay decides which signals it can take."""
    out = []
    for i in sigs:
        p = pre[i]
        row = {"t": ltf[i]["time"] + STEP, "side": p["side"][0], "score": abs(p["score"])}
        t = E.trade(p, ltf, i, V.ENTRY, V.STOP, TP_R, MAX_HOLD_BARS)
        if t is None:  # limit never filled: the order still held a slot until it lapsed
            row["end"] = ltf[min(len(ltf) - 1, i + LIMIT_BARS)]["time"] + STEP
        else:
            row.update(end=ltf[t["exit"]]["time"] + STEP, entry=t["entry"], sl_pct=round(t["risk_pct"], 3),
                       r=round(t["r"], 3), status=t["status"])
        out.append(row)
    return out


def _collect_one(args: tuple) -> tuple[str, dict]:
    import httpx

    from .config import get_settings
    from .market import get_klines

    symbol, bars, regime = args
    cfg, settings = MODES["swing"], get_settings()
    htf_bars = bars * STEP // INTERVAL_SECONDS[cfg["htf"]] + 320
    l = get_klines(settings, symbol, cfg["ltf"], bars)[:-1]
    h = get_klines(settings, symbol, cfg["htf"], htf_bars)
    start = datetime.fromtimestamp(l[0]["time"], timezone.utc) - timedelta(days=2)
    end = datetime.now(timezone.utc) - timedelta(days=1)
    with httpx.Client() as client:
        fund = D.funding_history(client, symbol, int(start.timestamp() * 1000))
        met = D.metrics_history(client, symbol, start, end)
    feats = D.features(l, STEP, fund, met)
    pre = R.precompute(symbol, "swing", h, l, regime)
    sigs = [i for i in E.signals(pre, D.make_allow(feats, E.V3), len(l)) if V.body_ok(pre, l, i)]
    print(f"{symbol}: {len(l)} bars, {len(met)} metrics rows, {len(sigs)} signals", file=sys.stderr, flush=True)
    return symbol, {"first": l[0]["time"], "last": l[-1]["time"] + STEP, "signals": signal_outcomes(pre, l, sigs)}


def exchange_filters(symbols: list[str]) -> dict:
    """Lot step, minimum order value and 24h volume from Binance USDT-M futures."""
    import httpx

    info = httpx.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=30).json()
    vol = {t["symbol"]: float(t["quoteVolume"])
           for t in httpx.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=30).json()}
    out = {}
    for s in info["symbols"]:
        if s["symbol"] not in symbols:
            continue
        f = {x["filterType"]: x for x in s["filters"]}
        out[s["symbol"]] = {
            "step": float(f["MARKET_LOT_SIZE"]["stepSize"]),
            "min_notional": float(f["MIN_NOTIONAL"]["notional"]),
            "volume_m": round(vol.get(s["symbol"], 0) / 1e6, 1),
        }
    return out


def collect(symbols: list[str], bars: int) -> dict:
    from .config import get_settings
    from .market import get_klines

    btc = get_klines(get_settings(), "BTCUSDT", MODES["swing"]["htf"], bars * STEP // 86400 + 320)
    regime = btc_regime(btc)
    with ProcessPoolExecutor(max_workers=6) as pool:
        coins = dict(pool.map(_collect_one, [(s, bars, regime) for s in symbols]))
    return {"generated": int(time.time()), "filters": exchange_filters(symbols), "coins": coins}


# ---------------------------------------------------------------- account replay

def size(sig: dict, equity: float, risk_pct: float, f: dict) -> tuple[float, float] | None:
    """(quantity, margin) for one signal, or None when Binance would refuse the order.
    Quantity is whole lot steps at the planned risk; leverage stays under 1/(2 x SL%)
    on isolated margin so liquidation sits beyond the stop."""
    dist = sig["entry"] * sig["sl_pct"] / 100
    qty = math.floor(equity * risk_pct / 100 / dist / f["step"] + 1e-9) * f["step"]
    notional = qty * sig["entry"]
    if qty <= 0 or notional < f["min_notional"]:
        return None
    lev = max(1, min(MAX_LEVERAGE, math.floor(100 / (2 * sig["sl_pct"]))))
    return qty, notional / lev


def replay(coins: dict, filters: dict, pick, start: int, end: int, risk_pct: float, max_pos: int,
           equity: float = EQUITY) -> dict:
    """One account through every signal of the picked coins in [start, end).

    `pick(symbol, t)` says whether the engine trades that coin at time t. Profit is
    booked when a trade closes; sizes use the balance at the signal. Signals arriving
    together are taken strongest score first.
    """
    events = sorted(((s, sig) for s, c in coins.items() for sig in c["signals"] if start <= sig["t"] < end),
                    key=lambda e: (e[1]["t"], -e[1]["score"]))
    open_: list[tuple[int, str, float, float]] = []  # (end, symbol, pnl, margin)
    eq, peak, dd = equity, equity, 0.0
    curve, trades, skipped = [(start, equity)], [], {"busy": 0, "slots": 0, "min_order": 0, "margin": 0, "unfilled": 0}

    def close_until(t: int):
        nonlocal eq, peak, dd
        for pos in sorted(p for p in open_ if p[0] <= t):
            open_.remove(pos)
            if pos[3] == 0:  # a limit that lapsed unfilled
                continue
            eq += pos[2]
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
            curve.append((pos[0], eq))

    for symbol, sig in events:
        close_until(sig["t"])
        if not pick(symbol, sig["t"]):
            continue
        if any(p[1] == symbol for p in open_):
            skipped["busy"] += 1
            continue
        if len(open_) >= max_pos:
            skipped["slots"] += 1
            continue
        if "r" not in sig:
            skipped["unfilled"] += 1
            open_.append((sig["end"], symbol, 0.0, 0.0))  # the pending limit held the slot
            continue
        z = size(sig, eq, risk_pct, filters[symbol])
        if z is None:
            skipped["min_order"] += 1
            continue
        qty, margin = z
        if margin + sum(p[3] for p in open_) > eq:
            skipped["margin"] += 1
            continue
        pnl = sig["r"] * qty * sig["entry"] * sig["sl_pct"] / 100
        open_.append((sig["end"], symbol, pnl, margin))
        trades.append({"s": symbol, "t": sig["t"], "r": sig["r"], "pnl": pnl, "risk": pnl / sig["r"] if sig["r"] else 0})
    close_until(end + 10 ** 9)
    rs = [t["r"] for t in trades]
    return {
        "trades": len(trades), **{k: v for k, v in R.score(rs).items() if k != "n"},
        "final": round(eq, 2), "return_pct": round(100 * (eq / equity - 1), 1), "max_dd_pct": round(100 * dd, 1),
        "skipped": skipped, "curve": curve, "list": trades,
    }


def coin_stats(coins: dict, before: int) -> dict[str, dict]:
    """Per coin, trades that closed before `before` (no look-ahead), at one position per coin."""
    out = {}
    for s, c in coins.items():
        free, rs = 0, []
        for sig in c["signals"]:
            if sig["t"] < free or "r" not in sig or sig["end"] > before:
                continue
            rs.append(sig["r"])
            free = sig["end"]
        out[s] = {"n": len(rs), "exp": sum(rs) / len(rs) if rs else None}
    return out


def tradeable(sig: dict, f: dict, equity: float, risk_pct: float) -> bool:
    return "r" in sig and size(sig, equity, risk_pct, f) is not None


def fits_account(coins: dict, filters: dict, before: int, risk_pct: float) -> dict[str, float]:
    """Share of each coin's past signals that $60 could actually place (lot step, min order)."""
    out = {}
    for s, c in coins.items():
        past = [sig for sig in c["signals"] if "r" in sig and sig["t"] < before]
        out[s] = sum(tradeable(sig, filters[s], EQUITY, risk_pct) for sig in past) / len(past) if past else 0.0
    return out


def select(coins: dict, filters: dict, cut: int, risk_pct: float) -> dict[str, set]:
    """Coin lists by each selection rule, from trades closed before `cut` only."""
    st, fit = coin_stats(coins, cut), fits_account(coins, filters, cut, risk_pct)
    ok = [s for s in coins if fit[s] >= 0.5]  # most of its orders would go through
    known = [s for s in ok if st[s]["n"] >= MIN_PAST_TRADES]
    return {
        "all": set(ok),
        "past_positive": {s for s in known if st[s]["exp"] > 0},
        "top8": set(sorted(known, key=lambda s: st[s]["exp"], reverse=True)[:8]),
    }


def pickers(coins: dict, filters: dict, risk_pct: float, years: range) -> dict:
    """Each selection rule re-run on Jan 1 of every year, as a live engine would."""
    plans: dict[str, dict[int, set]] = {"all": {}, "past_positive": {}, "top8": {}}
    for y in years:
        for name, chosen in select(coins, filters, _ts(y), risk_pct).items():
            plans[name][y] = chosen

    def make(plan):
        return lambda s, t: s in plan.get(datetime.fromtimestamp(t, timezone.utc).year, set())
    return {name: (make(plan), {y: sorted(v) for y, v in plan.items()}) for name, plan in plans.items()}


def _ts(y: int) -> int:
    return int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())


def _weekly(curve: list[tuple[int, float]]) -> list[list]:
    out, last_week = [], None
    for t, v in curve:
        w = t // (7 * 86400)
        if w == last_week:
            out[-1] = [t, round(v, 2)]
        else:
            out.append([t, round(v, 2)])
            last_week = w
    return out


def _yearly(trades: list[dict]) -> dict:
    y: dict[str, list[dict]] = {}
    for t in trades:
        y.setdefault(str(datetime.fromtimestamp(t["t"], timezone.utc).year), []).append(t)
    return {k: {"trades": len(v), "exp": round(sum(t["r"] for t in v) / len(v), 3), "pnl": round(sum(t["pnl"] for t in v), 2)}
            for k, v in sorted(y.items())}


def _brief(r: dict) -> dict:
    return {k: r[k] for k in ("trades", "wr", "exp", "pf", "final", "return_pct", "max_dd_pct", "skipped")}


def build(raw: dict) -> dict:
    coins, filters = raw["coins"], raw["filters"]
    coins = {s: c for s, c in coins.items() if s in filters}
    last = max(c["last"] for c in coins.values())
    this_year = datetime.fromtimestamp(last, timezone.utc).year
    years = range(FIRST_YEAR, this_year + 1)
    t_start, t_end = _ts(FIRST_YEAR), last

    # 1) account settings, judged on the in-sample years only, trading every coin that fits
    grid = []
    for risk_pct in GRID["risk_pct"]:
        everything = pickers(coins, filters, risk_pct, years)["all"][0]
        for max_pos in GRID["max_pos"]:
            a = replay(coins, filters, everything, t_start, IS_END, risk_pct, max_pos)
            b = replay(coins, filters, everything, IS_END, t_end, risk_pct, max_pos)
            grid.append({"risk_pct": risk_pct, "max_pos": max_pos, "is": _brief(a), "oos": _brief(b)})
    # best in-sample return per unit of drawdown; risk only grows when it pays for its drawdown
    best = max(grid, key=lambda g: g["is"]["return_pct"] / max(g["is"]["max_dd_pct"], 5))
    risk_pct, max_pos = best["risk_pct"], best["max_pos"]

    # 2) coin pickers at those settings: in-sample, out-of-sample, and the whole walk-forward
    picks = pickers(coins, filters, risk_pct, years)
    methods = []
    for name, (pick, plan) in picks.items():
        a = replay(coins, filters, pick, t_start, IS_END, risk_pct, max_pos)
        b = replay(coins, filters, pick, IS_END, t_end, risk_pct, max_pos)
        w = replay(coins, filters, pick, t_start, t_end, risk_pct, max_pos)
        methods.append({"name": name, "is": _brief(a), "oos": _brief(b), "all": _brief(w), "years": _yearly(w["list"]),
                        "plan": {str(y): v for y, v in plan.items()}})
    # a picker must beat "all" out of sample to be worth its extra hindsight risk
    by_name = {m["name"]: m for m in methods}
    chosen = "all"
    for name in ("past_positive", "top8"):
        m = by_name[name]
        if m["oos"]["return_pct"] > by_name[chosen]["oos"]["return_pct"] and m["is"]["return_pct"] > by_name["all"]["is"]["return_pct"]:
            chosen = name
    pick, plan = picks[chosen]
    full = replay(coins, filters, pick, t_start, t_end, risk_pct, max_pos)
    oos = replay(coins, filters, pick, IS_END, t_end, risk_pct, max_pos)
    recommended = sorted(select(coins, filters, last + 1, risk_pct)[chosen])  # the list to trade from now on

    # 3) per-coin table: what each coin did and why it is or isn't in the list
    now_stats = coin_stats(coins, last + 1)
    fit_now = fits_account(coins, filters, last + 1, risk_pct)
    oos_trades: dict[str, list[float]] = {}
    for t in oos["list"]:
        oos_trades.setdefault(t["s"], []).append(t["r"])
    table = []
    for s, c in sorted(coins.items(), key=lambda kv: -(now_stats[kv[0]]["exp"] or -9)):
        st, f = now_stats[s], filters[s]
        reason = None
        if s not in recommended:
            reason = ("min_order" if fit_now[s] < 0.5 else "few_trades" if st["n"] < MIN_PAST_TRADES else "weak")
        table.append({"symbol": s, "trades": st["n"], "exp": None if st["exp"] is None else round(st["exp"], 3),
                      "oos_trades": len(oos_trades.get(s, [])),
                      "oos_exp": round(sum(oos_trades[s]) / len(oos_trades[s]), 3) if s in oos_trades else None,
                      "fit_pct": round(100 * fit_now[s]), "min_notional": f["min_notional"], "volume_m": f["volume_m"],
                      "since": c["first"], "trade": s in recommended, "reason": reason})

    return {
        "generated": int(time.time()),
        "data_until": last,
        "equity": EQUITY,
        "settings": {"risk_pct": risk_pct, "max_pos": max_pos, "max_leverage": MAX_LEVERAGE, "picker": chosen,
                     "is_end": IS_END, "first_year": FIRST_YEAR, "tp_r": TP_R, "max_hold_bars": MAX_HOLD_BARS},
        "recommended": recommended,
        "result": {**_brief(full), "years": _yearly(full["list"]), "curve": _weekly(full["curve"])},
        "oos": {**_brief(oos), "curve": _weekly(oos["curve"])},
        "methods": methods,
        "grid": grid,
        "coins": table,
        "filters": {s: filters[s] for s in recommended},
        "candidates": len(coins),
    }


def live_plan(symbol: str, sig: dict, saved: dict, equity: float) -> dict:
    """Today's v3 signal for one picked coin, sized for the account like the replay does."""
    risk_pct = saved["settings"]["risk_pct"]
    f = saved.get("filters", {}).get(symbol) or {"step": 0.0, "min_notional": sig["min_notional"]}
    row = {"symbol": symbol, "active": sig["active"], "side": sig["side"], "entry": sig["entry"],
           "stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"], "sl_pct": sig["sl_pct"],
           "checks_ok": sum(1 for c in sig["checks"] if c["ok"]), "checks": len(sig["checks"]),
           "risk_usd": round(equity * risk_pct / 100, 2)}
    if sig["sl_pct"]:
        z = size({"entry": sig["entry"], "sl_pct": sig["sl_pct"]}, equity, risk_pct,
                 {**f, "step": f["step"] or 1e-12})
        notional = equity * risk_pct / sig["sl_pct"]
        row.update(notional=round(z[0] * sig["entry"] if z else notional, 2), qty=z[0] if z else None,
                   leverage=max(1, min(MAX_LEVERAGE, math.floor(100 / (2 * sig["sl_pct"])))),
                   fits=z is not None, min_notional=f["min_notional"])
    return row


def load_saved() -> dict | None:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "collect":
        syms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(CANDIDATES)
        bars = int(sys.argv[3]) if len(sys.argv) > 3 else 10500
        SIGNALS_PATH.write_text(json.dumps(collect(syms, bars), separators=(",", ":")))
    else:
        out = build(json.loads(SIGNALS_PATH.read_text()))
        PATH.parent.mkdir(exist_ok=True)
        PATH.write_text(json.dumps(out, separators=(",", ":")))
        print(json.dumps({k: out[k] for k in ("settings", "recommended")}), file=sys.stderr)
