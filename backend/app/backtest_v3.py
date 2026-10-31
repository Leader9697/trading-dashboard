"""Every past trade of the live v3 signal, for drawing on the chart.

Uses the same entry, stop, target and time limit as the live signal (setup.py) on
top of the v3 filters, through the code research_entry.py tested them with. The
positioning filters need Binance's daily metrics archive, which is too slow to
read inside a request, so the trades are written to a JSON file that ships with
the app and is refreshed by running this module again.

Run: python -m app.backtest_v3 [symbols] [ltf_bars]   (writes app/data/v3_trades.json)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import research as R
from . import research_deriv as D
from . import research_entry as E
from .backtest import FEE_ROUND_TRIP
from .market import INTERVAL_SECONDS
from .setup import LIMIT_ATR, LIMIT_BARS, MAX_HOLD_BARS, MIN_NOTIONAL, MODES, STOP_ATR, TP_R, btc_regime

PATH = Path(__file__).resolve().parent / "data" / "v3_trades.json"
ENTRY = ("pullback", LIMIT_ATR, LIMIT_BARS, "skip")
STOP = ("atr", STOP_ATR)
EQUITY, RISK_PCT = 55.0, 1.0  # the account the alerts size for


def _digits(price: float) -> int:
    return 2 if price >= 100 else 4 if price >= 1 else 6


def trades(symbol: str, pre: list, ltf: list[dict], sigs: list[int], step: int) -> list[dict]:
    out = []
    for t in E.simulate(pre, ltf, sigs, ENTRY, STOP, TP_R, MAX_HOLD_BARS):
        d = _digits(t["entry"])
        notional = EQUITY * RISK_PCT / 100 / (t["risk_pct"] / 100)
        out.append({
            "signal": ltf[t["signal"]]["time"] + step,  # signal bar close, when the alert goes out
            "fill": ltf[t["fill"]]["time"],
            "end": ltf[t["exit"]]["time"] + step,
            "side": pre[t["signal"]]["side"],
            "entry": round(t["entry"], d),
            "stop_loss": round(t["sl"], d),
            "take_profit": round(t["tp"], d),
            "status": t["status"],
            "r": round(t["r"], 3),
            "sl_pct": round(t["risk_pct"], 2),
            "below_min": notional < MIN_NOTIONAL.get(symbol, 5),
        })
    return out


def run(data: dict, feats: dict, mode: str = "swing") -> dict:
    regime = btc_regime(data["BTCUSDT"][0])
    step = INTERVAL_SECONDS[MODES[mode]["ltf"]]
    coins = {}
    for s, (h, l) in data.items():
        pre = R.precompute(s, mode, h, l, regime)
        sigs = E.signals(pre, D.make_allow(feats[s], E.V3), len(l))
        coins[s] = trades(s, pre, l, sigs, step)
    return {
        "generated": int(time.time()),
        "rules": {"entry_atr": LIMIT_ATR, "valid_bars": LIMIT_BARS, "stop_atr": STOP_ATR, "tp_r": TP_R,
                  "max_hold_bars": MAX_HOLD_BARS, "fee_round_trip": FEE_ROUND_TRIP,
                  "equity": EQUITY, "risk_pct": RISK_PCT},
        "coins": coins,
    }


def load_saved() -> dict | None:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    syms = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 10500
    out = run(*E.load(syms, bars))
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(out, separators=(",", ":")))
    print({s: len(v) for s, v in out["coins"].items()}, file=sys.stderr)
