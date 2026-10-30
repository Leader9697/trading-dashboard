"""Forward journal of the AI debate's calls, scored against real candles afterwards.

The scheduled debate appends entries to the Edge Config key `journal` as compact
rows `[ts, symbol, "L"|"S", entry, stop_loss, tp1, tp2]` (swing mode). Each row is
replayed with the same fill/stop/target rules as the backtest, so live results
and backtest results are comparable.
"""

from __future__ import annotations

from bisect import bisect_right

from .backtest import RULES, _run_order, stats

MAX_ROWS = 25  # Edge Config Hobby stores ~8 KB in total, shared with the debate
MODE = "swing"
OPEN_STATES = {"pending", "open"}


def evaluate(row: list, candles: list[dict]) -> dict:
    ts, symbol, side, entry, sl, tp1, tp2 = row[:7]
    rules = RULES[MODE]
    out = {"ts": ts, "symbol": symbol, "side": "long" if side == "L" else "short",
           "entry": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2}
    i = bisect_right([c["time"] for c in candles], ts) - 1
    if i < 0:
        return {**out, "status": "no_data", "r": None}
    setup = {"direction": out["side"], "score": 0, "checklist": [],
             "levels": {"entry": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2}}
    t = _run_order(setup, candles, i, rules)
    status, last = t["status"], len(candles) - 1
    if status == "missed" and t["exit_index"] == last and last < i + rules["order_bars"]:
        status = "pending"
    elif status.startswith("timeout") and last < t["fill_index"] + rules["max_hold"]:
        status = "open"  # still inside the holding window; r is marked to market
    return {**out, "status": status, "r": t["r_split"] if status != "pending" else None}


def summarize(results: list[dict]) -> dict:
    closed = [{**r, "r_split": r["r"]} for r in results
              if r["status"] not in OPEN_STATES and r["status"] != "no_data"]
    return {**stats(closed, "r_split"), "missed": sum(1 for r in closed if r["status"] == "missed")}
