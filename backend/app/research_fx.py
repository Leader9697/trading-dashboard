"""Does the v3 edge carry over from crypto to forex and gold? Research only.

v3 has seven checks. Four are price-only and run unchanged here: trend score at least
40, volatility above its 200-bar median, the signal candle closing at least 0.1 ATR our
way, and trading with the trend regime (close above a rising daily EMA50, or below a
falling one). On crypto that regime is BTC's; a currency pair or gold has no BTC, so
each instrument uses its own daily regime ("own"), and the test is also run without it.
The other three need Binance futures data that spot FX and gold do not have: open
interest rising, retail long/short not crowded on our side and taker flow agreeing.
There is no free central volume or open interest for spot FX, so those three are left
out. That makes this "v3 price-only", the crypto equivalent of v2 plus the body check.

Entry, exits and costs follow the live engine: a limit 0.25 ATR better than the signal
close, valid 4 bars, stop 2 ATR from the close, one target at 4R, closed after 30 bars
(about 5 trading days on 4h). Costs differ from crypto: no fee per side but a spread
(paid once per round trip, a little wider than a typical standard account), a stop
slippage allowance, and an overnight swap for every night the trade is held, always
charged as a cost. A bar that opens past the stop (weekend or news gap) fills at its
open, so a gap can lose more than 1R; the same rule gives a gap past the target only the
target.

DonchianVF_v6 (Faris's MT5 strategy, see research_donchian.py) is run on the same
bars with the same costs, entry next open, stop 1.5 ATR, target 2.5R, 120-bar exit.

Data: Dukascopy hourly bid candles (free), built into UTC 4h bars and UTC daily bars
(Sunday's short session joins Monday). In-sample is up to 2024, out-of-sample 2025-26:
nothing here was tuned, so the split only shows whether the result holds after 2024.

Run where datafeed.dukascopy.com is reachable:
  python -m app.research_fx fetch [symbols] [first_year]   writes DATA_DIR/<SYM>_1h.json
  python -m app.research_fx mt5 <file.csv> <SYMBOL> [utc_offset_h]   or an MT5 H1 export instead
  python -m app.research_fx report [symbols]               prints the result as JSON
"""

from __future__ import annotations

import json
import lzma
import struct
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import research as R
from . import research_donchian as DV
from .backtest import WARMUP
from .setup import BODY_ATR, LIMIT_ATR, LIMIT_BARS, MAX_HOLD_BARS, RULES_V2, STOP_ATR, TP_R, btc_regime

DATA_DIR = Path("/tmp/fx_data")
H4, D1 = 4 * 3600, 86400
OOS_START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())

# symbol: (Dukascopy price scale, spread, stop slippage, swap per year as a share of price).
# Spread and slippage are in price units, a typical standard (no commission) account; an
# ECN account with $7/lot commission lands in the same place. Swap is charged on both
# sides as a cost: the rate differential plus the broker's markup, rounded up.
INSTRUMENTS = {
    "EURUSD": (1e5, 0.00012, 0.00005, 0.02),
    "GBPUSD": (1e5, 0.00016, 0.00005, 0.02),
    "USDJPY": (1e3, 0.014, 0.005, 0.02),
    "AUDUSD": (1e5, 0.00014, 0.00005, 0.02),
    "USDCAD": (1e5, 0.00018, 0.00005, 0.02),
    "USDCHF": (1e5, 0.00017, 0.00005, 0.02),
    "NZDUSD": (1e5, 0.00018, 0.00005, 0.02),
    "EURJPY": (1e3, 0.020, 0.005, 0.02),
    "GBPJPY": (1e3, 0.030, 0.008, 0.02),
    "XAUUSD": (1e3, 0.30, 0.10, 0.04),
}


# ---------------------------------------------------------------- data

def _month(client, symbol: str, year: int, month: int) -> list[dict]:
    """One month of Dukascopy hourly bid candles (month is 1-12; their folders count from 0)."""
    url = f"https://datafeed.dukascopy.com/datafeed/{symbol}/{year}/{month - 1:02d}/BID_candles_hour_1.bi5"
    r = client.get(url, timeout=60)
    if r.status_code == 404 or not r.content:
        return []
    r.raise_for_status()
    raw = lzma.decompress(r.content)
    scale = INSTRUMENTS[symbol][0]
    base = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    out = []
    for off, o, c, lo, hi, vol in struct.iter_unpack(">IIIIIf", raw):
        if vol <= 0:
            continue  # Dukascopy writes flat zero-volume hours when the market is shut
        out.append({"time": base + off, "open": o / scale, "high": hi / scale, "low": lo / scale,
                    "close": c / scale, "volume": float(vol)})
    return out


def fetch(symbol: str, first_year: int) -> int:
    import httpx

    now = datetime.now(timezone.utc)
    bars: list[dict] = []
    with httpx.Client() as client:
        for y in range(first_year, now.year + 1):
            for m in range(1, 13):
                if (y, m) >= (now.year, now.month):
                    break  # the running month is not published as a monthly file yet
                bars += _month(client, symbol, y, m)
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{symbol}_1h.json").write_text(json.dumps(bars, separators=(",", ":")))
    return len(bars)


def import_mt5(path: str, symbol: str, utc_offset_h: int = 0) -> int:
    """An MT5 History Center export (H1, tab separated, <DATE> <TIME> <OPEN> ...) as the same
    hourly file. MT5 server time is often UTC+2/+3: pass the offset to get UTC bars."""
    bars = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        f = line.replace(",", "\t").split("\t")
        if len(f) < 6 or not f[0][:1].isdigit():
            continue
        t = datetime.strptime(f"{f[0]} {f[1][:5]}", "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
        o, h, lo, c = map(float, f[2:6])
        bars.append({"time": int(t.timestamp()) - utc_offset_h * 3600, "open": o, "high": h, "low": lo,
                     "close": c, "volume": float(f[6]) if len(f) > 6 else 1.0})
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{symbol}_1h.json").write_text(json.dumps(bars, separators=(",", ":")))
    return len(bars)


def resample(h1: list[dict], kind: str) -> list[dict]:
    """UTC 4h bars, or UTC daily bars with Sunday's few hours added to Monday."""
    out: list[dict] = []
    for b in h1:
        if kind == "4h":
            key = b["time"] - b["time"] % H4
        else:
            key = b["time"] - b["time"] % D1
            if datetime.fromtimestamp(key, timezone.utc).weekday() == 6:
                key += D1
        if out and out[-1]["time"] == key:
            c = out[-1]
            c["high"], c["low"] = max(c["high"], b["high"]), min(c["low"], b["low"])
            c["close"] = b["close"]
            c["volume"] += b["volume"]
        else:
            out.append({**b, "time": key})
    return out


def load(symbol: str) -> tuple[list[dict], list[dict]]:
    h1 = json.loads((DATA_DIR / f"{symbol}_1h.json").read_text())
    return resample(h1, "1d"), resample(h1, "4h")


# ---------------------------------------------------------------- v3 price-only

def _gap_fill(long: bool, bar: dict, level: float) -> float:
    return min(bar["open"], level) if long else max(bar["open"], level)


def v3_trade(p: dict, l: list[dict], i: int) -> dict | None:
    """research_entry.trade with the live settings, plus gap fills on the stop."""
    long = p["side"] == "long"
    sign = 1 if long else -1
    close, a = l[i]["close"], p["atr"]
    limit, sl = close - sign * LIMIT_ATR * a, close - sign * STOP_ATR * a
    fill = None
    for j in range(i + 1, min(len(l) - 1, i + LIMIT_BARS) + 1):
        b = l[j]
        if (b["low"] <= limit) if long else (b["high"] >= limit):
            entry, fill = _gap_fill(long, b, limit), j
            break
        if (b["low"] <= sl) if long else (b["high"] >= sl):
            break
    if fill is None:
        return None
    risk = (entry - sl) * sign
    if risk <= 0:
        return None
    tp = entry + sign * TP_R * risk
    b = l[fill]
    if (b["low"] <= sl) if long else (b["high"] >= sl):
        return {"fill": fill, "exit": fill, "entry": entry, "risk": risk, "px": sl, "status": "stop"}
    end = min(len(l) - 1, fill + MAX_HOLD_BARS)
    if end < fill + MAX_HOLD_BARS:
        return None  # still open at the end of the data
    for j in range(fill + 1, end + 1):
        b = l[j]
        if (b["low"] <= sl) if long else (b["high"] >= sl):
            return {"fill": fill, "exit": j, "entry": entry, "risk": risk, "px": _gap_fill(long, b, sl), "status": "stop"}
        if (b["high"] >= tp) if long else (b["low"] <= tp):
            return {"fill": fill, "exit": j, "entry": entry, "risk": risk, "px": tp, "status": "tp"}
    return {"fill": fill, "exit": end, "entry": entry, "risk": risk, "px": l[end]["close"], "status": "timeout"}


def v3_signals(pre: list, l: list[dict], regime: bool) -> list[int]:
    """research_entry.signals without the positioning filters, plus the body check."""
    out, recent = [], []
    for i in range(WARMUP, len(l) - 1):
        p = pre[i]
        if p is None:
            continue
        recent.append(p["atr_pct"])
        if len(recent) > 200:
            recent.pop(0)
        long = p["side"] == "long"
        sign = 1 if long else -1
        if abs(p["score"]) < RULES_V2["threshold"]:
            continue
        if regime and p["btc"] != sign:
            continue
        if len(recent) >= 50 and p["atr_pct"] < sorted(recent)[len(recent) // 2]:
            continue
        if not p["atr"] or sign * (l[i]["close"] - l[i]["open"]) / p["atr"] < BODY_ATR:
            continue
        out.append(i)
    return out


def costed(symbol: str, l: list[dict], side: str, t: dict) -> dict:
    """R after spread, stop slippage and swap, plus what it needs from a small account."""
    _, spread, slip, swap = INSTRUMENTS[symbol]
    sign = 1 if side == "long" else -1
    nights = max(0, (l[t["exit"]]["time"] - l[t["fill"]]["time"]) // D1)
    cost = spread + (slip if t["status"] == "stop" else 0) + swap / 365 * t["entry"] * nights
    gross = sign * (t["px"] - t["entry"]) / t["risk"]
    return {"t": l[t["fill"]]["time"], "side": side[0], "status": t["status"], "gross": round(gross, 3),
            "r": round(gross - cost / t["risk"], 3), "risk": t["risk"], "entry": t["entry"]}


def run_v3(symbol: str, daily: list[dict], l: list[dict], regime: bool) -> list[dict]:
    pre = R.precompute(symbol, "swing", daily, l, btc_regime(daily))
    out, free_from = [], 0
    for i in v3_signals(pre, l, regime):
        if i < free_from:
            continue  # one position per instrument
        t = v3_trade(pre[i], l, i)
        if t is None:
            continue
        out.append(costed(symbol, l, pre[i]["side"], t))
        free_from = t["exit"] + 1
    return out


# ---------------------------------------------------------------- Donchian VF

def run_donchian(symbol: str, l: list[dict]) -> list[dict]:
    a = DV.atr(l)
    out, free_from = [], 0
    for i, side in DV.signals(l):
        e = i + 1
        if i < free_from or e >= len(l) or a[i] is None:
            continue
        long = side == "long"
        sign = 1 if long else -1
        sl = l[i]["close"] - sign * DV.SL_ATR * a[i]
        tp = l[i]["close"] + sign * DV.SL_ATR * a[i] * DV.RR
        entry = l[e]["open"]
        risk = sign * (entry - sl)
        if risk <= 0 or sign * (tp - entry) <= 0:
            continue
        last = e + DV.MAX_BARS
        if last >= len(l):
            break
        t = {"fill": e, "exit": last, "entry": entry, "risk": risk, "px": l[last]["close"], "status": "timeout"}
        for j in range(e, last + 1):
            b = l[j]
            if (b["low"] <= sl) if long else (b["high"] >= sl):
                t.update(exit=j, px=_gap_fill(long, b, sl), status="stop")
                break
            if (b["high"] >= tp) if long else (b["low"] <= tp):
                t.update(exit=j, px=tp, status="tp")
                break
        out.append(costed(symbol, l, side, t))
        free_from = t["exit"] + 1
    return out


# ---------------------------------------------------------------- report

def stats(trades: list[dict], key: str = "r") -> dict:
    rs = [t[key] for t in sorted(trades, key=lambda t: t["t"])]
    s = R.score(rs)
    if rs:
        from .research_entry import max_dd
        s.update(total=round(sum(rs), 1), dd=max_dd(rs))
    return s


def split(trades: list[dict]) -> dict:
    by_year: dict[int, list] = {}
    for t in trades:
        by_year.setdefault(datetime.fromtimestamp(t["t"], timezone.utc).year, []).append(t)
    return {
        "all": stats(trades), "gross": stats(trades, "gross"),
        "is": stats([t for t in trades if t["t"] < OOS_START]),
        "oos": stats([t for t in trades if t["t"] >= OOS_START]),
        "years": {y: (len(v), round(sum(t["r"] for t in v), 1)) for y, v in sorted(by_year.items())},
    }


def small_account(symbol: str, trades: list[dict]) -> dict:
    """Median dollar risk of one 0.01 lot at the stop, on a standard and a cent account,
    for trades since 2025 (contract: 100,000 units of FX, 100 oz of gold per lot)."""
    recent = [t for t in trades if t["t"] >= OOS_START] or trades
    if not recent:
        return {}
    units = 100 if symbol == "XAUUSD" else 100_000
    usd = []
    for t in recent:
        r = t["risk"] * units * 0.01
        if symbol.startswith("USD"):
            r /= t["entry"]  # quote currency to USD
        elif symbol.endswith("JPY"):
            r /= 150.0  # cross quoted in yen, near today's USDJPY
        usd.append(r)
    usd.sort()
    med = usd[len(usd) // 2]
    return {"risk_001_lot_usd": round(med, 2), "risk_001_cent_usd": round(med / 100, 3)}


def _one(symbol: str) -> tuple[str, dict]:
    daily, l = load(symbol)
    res = {"bars_4h": len(l), "first": l[0]["time"], "last": l[-1]["time"]}
    for name, trades in (("v3_own_regime", run_v3(symbol, daily, l, True)),
                         ("v3_no_regime", run_v3(symbol, daily, l, False)),
                         ("donchian_vf", run_donchian(symbol, l))):
        res[name] = {**split(trades), "account": small_account(symbol, trades)}
        res[name]["_trades"] = trades
    print(f"{symbol}: {len(l)} bars", file=sys.stderr, flush=True)
    return symbol, res


def report(symbols: list[str]) -> dict:
    with ProcessPoolExecutor() as pool:
        per = dict(pool.map(_one, symbols))
    pooled = {}
    for name in ("v3_own_regime", "v3_no_regime", "donchian_vf"):
        fx = [t for s, r in per.items() if s != "XAUUSD" for t in r[name]["_trades"]]
        pooled[name] = {"fx_all_pairs": split(fx)}
    for r in per.values():
        for name in pooled:
            del r[name]["_trades"]
    return {"pooled": pooled, "per_symbol": per}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(INSTRUMENTS)
    if cmd == "mt5":
        print(import_mt5(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 0), "hourly bars")
    elif cmd == "fetch":
        first = int(sys.argv[3]) if len(sys.argv) > 3 else 2012
        with ProcessPoolExecutor(max_workers=5) as pool:
            for s, n in zip(syms, pool.map(fetch, syms, [first] * len(syms))):
                print(f"{s}: {n} hourly bars", file=sys.stderr, flush=True)
    else:
        print(json.dumps(report(syms), indent=1))
