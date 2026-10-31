"""Walk-forward backtest of the setup rules on historical candles.

At every closed bar of the entry timeframe the engine sees only data up to that
bar, builds the same setup the dashboard shows, and places a limit order at the
entry price. Fills, stops and targets are then checked bar by bar. When a bar
touches both the stop and a target, the stop is assumed first (conservative).

Funding, open interest and Fear & Greed have no free history, so the backtest
scores trend and momentum only.
"""

from __future__ import annotations

from bisect import bisect_right

from .market import INTERVAL_SECONDS
from .setup import MAX_HOLD_BARS, MODES, build_setup

WINDOW = 300  # candles fed to the setup engine per step, same as live
WARMUP = 220  # entry-timeframe bars before the first signal
RULES = {
    # bars a limit order waits for a fill, bars a filled trade may stay open
    "swing": {"order_bars": 6, "max_hold": MAX_HOLD_BARS},
    "scalp": {"order_bars": 8, "max_hold": 48},
}
BARS = {"swing": 3000, "scalp": 6000}  # entry-timeframe history: ~16 months of 4h, ~2 months of 15m
FEE_ROUND_TRIP = 0.0008  # 0.04% taker per side, Binance futures default


def _htf_slice(htf: list[dict], htf_open: list[int], htf_step: int, ltf_close: int) -> list[dict]:
    """Higher-timeframe candles that had fully closed by `ltf_close`."""
    idx = bisect_right(htf_open, ltf_close - htf_step)
    return htf[max(0, idx - WINDOW) : idx]


def simulate(symbol: str, mode: str, htf: list[dict], ltf: list[dict]) -> list[dict]:
    cfg, rules = MODES[mode], RULES[mode]
    htf_step, ltf_step = INTERVAL_SECONDS[cfg["htf"]], INTERVAL_SECONDS[cfg["ltf"]]
    htf_open = [c["time"] for c in htf]
    trades: list[dict] = []
    i = WARMUP
    while i < len(ltf) - 1:
        h = _htf_slice(htf, htf_open, htf_step, ltf[i]["time"] + ltf_step)
        if len(h) < 60:
            i += 1
            continue
        s = build_setup(symbol, mode, h, ltf[max(0, i - WINDOW + 1) : i + 1], {}, None)
        if s["direction"] == "wait":
            i += 1
            continue
        trade = _run_order(s, ltf, i, rules)
        trades.append(trade)
        i = trade["exit_index"] + 1
    return trades


def _run_order(s: dict, ltf: list[dict], i: int, rules: dict) -> dict:
    lv, long = s["levels"], s["direction"] == "long"
    entry, sl, tp1, tp2 = lv["entry"], lv["stop_loss"], lv["tp1"], lv["tp2"]
    risk = abs(entry - sl)
    fee_r = FEE_ROUND_TRIP * entry / risk if risk else 0.0
    base = {
        "time": ltf[i]["time"], "side": s["direction"], "score": s["score"],
        "checks": sum(1 for c in s["checklist"] if c["ok"]),
        "entry": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
    }

    # 1) wait for the limit order to fill
    fill = None
    last = min(len(ltf) - 1, i + rules["order_bars"])
    for j in range(i + 1, last + 1):
        bar = ltf[j]
        if (long and bar["high"] >= tp1) or (not long and bar["low"] <= tp1):
            if not ((long and bar["low"] <= entry) or (not long and bar["high"] >= entry)):
                return {**base, "status": "missed", "exit_index": j, "r_tp1": 0.0, "r_split": 0.0}
        if (long and bar["low"] <= entry) or (not long and bar["high"] >= entry):
            fill = j
            break
    if fill is None:
        return {**base, "status": "missed", "exit_index": last, "r_tp1": 0.0, "r_split": 0.0}

    # 2) manage the open trade. Two exit styles are tracked side by side:
    #    r_tp1   = whole position closed at TP1 (2R)
    #    r_split = half closed at TP1, stop moved to entry, rest aimed at TP2
    def hit(bar, level, above):
        return bar["high"] >= level if above else bar["low"] <= level

    r_tp1 = r_split = None
    half_done = False
    status = "timeout"
    end = min(len(ltf) - 1, fill + rules["max_hold"])
    for j in range(fill, end + 1):
        bar = ltf[j]
        stop = entry if half_done else sl
        if hit(bar, stop, not long):
            if r_tp1 is None:
                r_tp1 = -1.0
            r_split = 0.5 * 2.0 if half_done else -1.0
            status = "breakeven" if half_done else "stop"
            break
        if not half_done and hit(bar, tp1, long):
            r_tp1, half_done = 2.0, True
        if half_done and hit(bar, tp2, long):
            r_split, status = 0.5 * 2.0 + 0.5 * 3.0, "tp2"
            break
    else:
        j = end
        close = ltf[end]["close"]
        open_r = ((close - entry) if long else (entry - close)) / risk if risk else 0.0
        if r_tp1 is None:
            r_tp1 = open_r
        r_split = 0.5 * 2.0 + 0.5 * open_r if half_done else open_r
        status = "timeout_after_tp1" if half_done else "timeout"
    if status == "breakeven" and r_tp1 is None:
        r_tp1 = 2.0
    return {
        **base, "status": status, "fill_time": ltf[fill]["time"], "exit_time": ltf[j]["time"],
        "fill_index": fill, "exit_index": j, "r_tp1": round(r_tp1 - fee_r, 3), "r_split": round(r_split - fee_r, 3),
    }


def stats(trades: list[dict], key: str) -> dict:
    filled = [t for t in trades if t["status"] != "missed"]
    rs = [t[key] for t in filled]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_loss = -sum(losses)
    return {
        "trades": len(rs),
        "winrate": round(len(wins) / len(rs) * 100, 1) if rs else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else None,
        "max_drawdown_r": round(max_dd, 2),
    }


def summarize(symbol: str, mode: str, htf: list[dict], ltf: list[dict]) -> dict:
    trades = simulate(symbol, mode, htf, ltf)
    filled = [t for t in trades if t["status"] != "missed"]
    by_quality = {
        "checks_4plus": stats([t for t in filled if t["checks"] >= 4], "r_split"),
        "checks_below_4": stats([t for t in filled if t["checks"] < 4], "r_split"),
    }
    equity, curve = 0.0, []
    for t in filled:
        equity += t["r_split"]
        curve.append({"time": t["exit_time"], "value": round(equity, 3)})
    return {
        "symbol": symbol,
        "mode": mode,
        "period": {"from": ltf[WARMUP]["time"] if len(ltf) > WARMUP else None, "to": ltf[-1]["time"], "bars": len(ltf)},
        "signals": len(trades),
        "missed": len(trades) - len(filled),
        "exit_tp1": stats(trades, "r_tp1"),
        "exit_split": stats(trades, "r_split"),
        "by_quality": by_quality,
        "equity": curve,
        "recent": [
            {k: t[k] for k in ("time", "side", "checks", "entry", "stop_loss", "tp1", "tp2", "status", "r_split")}
            for t in filled[-10:]
        ],
    }
