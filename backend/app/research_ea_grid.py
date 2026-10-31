"""Backtest of Faris's MT5 grid/martingale EA (TrendMarti V13) against the improved V14. Research only.

V13 opens a buy basket when RSI(6) on M5 is below 40 and a sell basket when it is above 60,
both at once if needed, then adds a layer every 10 pips (x1.2 wider each time) at 1.5x the
previous lot, up to 12 layers. A basket closes at its average price + 10 pips (2 pips from
layer 4). It has no stop per basket: only a 40% floating-loss guard measured against
balance.

V14 (ea/TrendMarti_Dual_Cycle_V14_Smart.mq5) keeps both baskets but:
  - H1 EMA200 trend filter: the basket with the trend may grow to MaxLayers, the basket
    against the trend only to CounterLayers, and no layer is added against the trend.
  - grid step from ATR(H1,14), fixed for the life of the basket;
  - a hard stop one grid step past the last layer, and the starting lot is sized so that
    this worst case costs RiskPct of equity (the loss is known before the first order);
  - TP from ATR, net of swap and commission; smaller TP from RecoveryStart;
  - drawdown measured from peak equity, then a 24h pause;
  - no new baskets around the 21:00 UTC rollover or late Friday; optional Friday close.

Simulation: Binance EURUSDT 1-minute bars (the only free EURUSD minute data reachable from
the sandbox; Dukascopy and Yahoo answer 429), trimmed to FX hours. Each minute bar is
walked as open, low/high (in the likelier order), close. Wicks more than 5 pips beyond the
body are clipped, because thin stablecoin books print spikes a broker feed does not.
Costs: 1.2 pip spread, swap per lot per night long -$6 short -$1 (x3 on Wednesday), no
commission (standard account). Leverage 1:500, stop-out at 50% margin level.

Result (2023-01..2026-09, $10,000 start; 2020-22 dropped, see DATA_START):
  V13 as written, 0.01 lot   +15.7%  DD 7.3%  but ends with both baskets stuck at 12 layers
                                               (-$1,400 floating); 2025 alone -4.9%
  V13, 0.01 lot on $1,000    +49%    DD 72-80%, equity fell to $286
  V13, 0.10 lot on $10,000   -95%    blown (same lot/equity ratio as above, other path)
  V14 tuning, 2023-24        all 24 variants lost (-7% to -75%); grid 1.5 ATR lost least
  V14 2023-24 (in-sample)    -7.1%   DD 19.8%
  V14 2025-26 (out-of-sample) +2.8%  DD 7.4%; with 2x spread -9.7%
  V14 per year               2023 -5.3%, 2024 +0.4%, 2025 -0.7%, 2026 +3.4%
V14 caps the loss (largest basket loss $401, never near a blow-up), but once the tail is
capped there is no edge left: the grid wins ~95% of baskets and gives it back in the stops
and the spread. The tuning list in variants() was run with grid_atr 1.0 as its base.

Commands (run where data.binance.vision is reachable):
  python -m app.research_ea_grid fetch                         writes DATA_DIR/EURUSD_1m.npy
  python -m app.research_ea_grid report tune                   in-sample variants
  python -m app.research_ea_grid report main '{"grid_atr": 1.5}'   V13 vs V14, IS/OOS/years
"""

from __future__ import annotations

import io
import json
import math
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DATA_DIR = Path("/tmp/fx_data")
FILE = DATA_DIR / "EURUSD_1m.npy"
PIP = 0.0001
CONTRACT = 100_000
SPREAD = 1.2 * PIP
SWAP_LONG, SWAP_SHORT = -6.0, -1.0  # $ per lot per night
LEVERAGE, STOPOUT = 500, 0.5
WICK = 5 * PIP
# Binance EURUSDT before 2023 is too thin to stand in for EURUSD: tens of thousands of 1-minute
# spikes a year and strongly mean-reverting hourly returns (autocorrelation -0.23 in 2020), which
# hands any grid fake profits (a fixed 8-pip grid turned $10k into $66M on 2020-23). From 2023 the
# pair looks like EURUSD (a few hundred spikes a year, hourly autocorrelation near 0), so only
# 2023 onward is traded: in-sample 2023-2024 for choosing settings, out-of-sample 2025-2026.
DATA_START = int(datetime(2022, 10, 1, tzinfo=timezone.utc).timestamp())   # indicator warm-up
IS_START = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
IS_END = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
END = 1 << 62


# ---------------------------------------------------------------- data

def fetch(first=(2020, 1)) -> int:
    import httpx

    rows = []
    y, m = first
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=120) as c:
        while (y, m) < (now.year, now.month):
            url = f"https://data.binance.vision/data/spot/monthly/klines/EURUSDT/1m/EURUSDT-1m-{y}-{m:02d}.zip"
            r = c.get(url)
            if r.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    txt = z.read(z.namelist()[0]).decode()
                for line in txt.splitlines():
                    f = line.split(",")
                    if not f[0][:1].isdigit():
                        continue
                    t = int(f[0])
                    t = t // 1000_000 if t > 1e14 else t // 1000  # 2025+ files use microseconds
                    rows.append((t, float(f[1]), float(f[2]), float(f[3]), float(f[4])))
            print(y, m, r.status_code, len(rows), file=sys.stderr, flush=True)
            m += 1
            if m == 13:
                y, m = y + 1, 1
    a = np.array(rows)
    a = a[np.argsort(a[:, 0])]
    a = a[fx_mask(a[:, 0].astype(np.int64))]
    DATA_DIR.mkdir(exist_ok=True)
    np.save(FILE, a)
    return len(a)


def fx_mask(t: np.ndarray) -> np.ndarray:
    """Sunday 21:00 to Friday 21:00 UTC (DST ignored)."""
    wd = ((t // 86400) + 3) % 7  # Monday = 0
    h = (t % 86400) // 3600
    return ~((wd == 5) | ((wd == 4) & (h >= 21)) | ((wd == 6) & (h < 21)))


def _resample(t, o, h, l, c, sec):
    key = t - t % sec
    idx = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    end = np.r_[idx[1:], len(t)] - 1
    hi = np.maximum.reduceat(h, idx)
    lo = np.minimum.reduceat(l, idx)
    return idx, key[idx], o[idx], hi, lo, c[end]


def _rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = np.zeros_like(c), np.zeros_like(c)
    au[n], ad[n] = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    rs = au / np.where(ad == 0, 1e-12, ad)
    r = 100 - 100 / (1 + rs)
    r[:n] = 50
    return r


def _ema(c, n):
    a, out = 2 / (n + 1), np.empty_like(c)
    out[0] = c[0]
    for i in range(1, len(c)):
        out[i] = out[i - 1] + a * (c[i] - out[i - 1])
    return out


def _atr(h, l, c, n):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    out = np.empty_like(c)
    out[:n] = tr[:n].mean()
    for i in range(n, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


@dataclass
class Market:
    t: np.ndarray
    path: np.ndarray       # (n, 4) mid prices per minute
    rsi: np.ndarray        # RSI(6) of the last closed M5 bar
    m5range: np.ndarray    # range of the last closed M5 bar
    trend: np.ndarray      # +1 / -1: last closed H1 close vs H1 EMA200
    atr: np.ndarray        # ATR(14) of the last closed H1 bar
    hour: np.ndarray
    minute: np.ndarray
    wd: np.ndarray


def market(a: np.ndarray, start: int = 0, end: int = 1 << 62) -> Market:
    a = a[a[:, 0] >= DATA_START]
    t = a[:, 0].astype(np.int64)
    o, h, l, c = a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(), a[:, 4].copy()
    # a minute whose close sits more than 5 pips from the 5-minute median is a bad print: flatten it
    med = np.r_[c[:2], np.median(np.lib.stride_tricks.sliding_window_view(c, 5), axis=1), c[-2:]]
    bad = np.abs(c - med) > 5 * PIP
    o[bad] = h[bad] = l[bad] = c[bad] = med[bad]
    o[1:] = np.where(np.abs(o[1:] - c[:-1]) > 5 * PIP, c[:-1], o[1:])  # and an open that jumps away
    h = np.maximum(h, np.maximum(o, c))
    l = np.minimum(l, np.minimum(o, c))
    h = np.minimum(h, np.maximum(o, c) + WICK)
    l = np.maximum(l, np.minimum(o, c) - WICK)

    def last_closed(sec, values):
        idx, *_ = _resample(t, o, h, l, c, sec)
        bar = np.searchsorted(idx, np.arange(len(t)), side="right") - 1  # bar each minute is in
        prev = np.clip(bar - 1, 0, None)
        return [v[prev] for v in values]

    i5, k5, o5, h5, l5, c5 = _resample(t, o, h, l, c, 300)
    rsi, rng = last_closed(300, [_rsi(c5, 6), h5 - l5])
    i1, k1, o1, h1, l1, c1 = _resample(t, o, h, l, c, 3600)
    ema = _ema(c1, 200)
    trend, atr = last_closed(3600, [np.where(c1 > ema, 1, -1), _atr(h1, l1, c1, 14)])
    up = c >= o
    path = np.stack([o, np.where(up, l, h), np.where(up, h, l), c], axis=1)
    m = (t >= start) & (t < end)
    return Market(t[m], path[m], rsi[m], rng[m], trend[m], atr[m], ((t % 86400) // 3600)[m],
                  ((t % 3600) // 60)[m], (((t // 86400) + 3) % 7)[m])


# ---------------------------------------------------------------- account

@dataclass
class Basket:
    side: int                      # +1 buy, -1 sell
    lots: list = field(default_factory=list)
    prices: list = field(default_factory=list)
    swap: float = 0.0
    opened: int = 0
    step: float = 0.0              # V14: grid step fixed at basket start
    lot0: float = 0.0
    max_layers: int = 0
    stop: float = 0.0              # V14: hard stop price
    with_trend: bool = True
    eq0: float = 0.0

    @property
    def n(self):
        return len(self.lots)

    def pnl(self, bid, ask):
        px = bid if self.side > 0 else ask
        return sum((px - p) * v for p, v in zip(self.prices, self.lots)) * self.side * CONTRACT + self.swap

    def avg(self):
        return sum(p * v for p, v in zip(self.prices, self.lots)) / sum(self.lots)


class Account:
    def __init__(self, capital):
        self.balance = self.peak = self.capital = capital
        self.b: dict[int, Basket | None] = {1: None, -1: None}
        self.closed = []           # (time, side, layers, pnl, reason)
        self.min_eq = capital
        self.max_dd = 0.0
        self.blown = False
        self.curve = []

    def equity(self, bid, ask):
        return self.balance + sum(x.pnl(bid, ask) for x in self.b.values() if x)

    def open(self, side, lot, bid, ask, t):
        x = self.b[side]
        if x is None:
            x = self.b[side] = Basket(side, opened=t)
        x.lots.append(lot)
        x.prices.append(ask if side > 0 else bid)

    def close(self, side, bid, ask, t, why):
        x = self.b[side]
        p = x.pnl(bid, ask)
        self.balance += p
        self.closed.append((t, side, x.n, round(p, 2), why))
        self.b[side] = None

    def rollover(self):
        for x in self.b.values():
            if x:
                x.swap += sum(x.lots) * (SWAP_LONG if x.side > 0 else SWAP_SHORT)

    def check(self, bid, ask, t):
        eq = self.equity(bid, ask)
        lots = max((sum(x.lots) for x in self.b.values() if x), default=0)
        if lots and eq < STOPOUT * lots * CONTRACT * bid / LEVERAGE:
            for s in (1, -1):
                if self.b[s]:
                    self.close(s, bid, ask, t, "stopout")
            eq = self.balance
        self.peak = max(self.peak, eq)
        self.min_eq = min(self.min_eq, eq)
        self.max_dd = max(self.max_dd, 1 - eq / self.peak)
        if eq <= 0.05 * self.capital:
            self.blown = True
        return eq


def rlot(x):
    return max(0.01, round(x, 2))


# ---------------------------------------------------------------- V13 (original)

@dataclass
class V13:
    lot: float = 0.01
    mult: float = 1.5
    max_lot: float = 10.0
    layers: int = 12
    grid: float = 100 * 0.00001
    expansion: float = 1.2
    rsi_buy: float = 40
    rsi_sell: float = 60
    rec_start: int = 4
    rec_tp: float = 20 * 0.00001
    tp: float = 100 * 0.00001
    candle_max: float = 250 * 0.00001
    freeze: float = 10
    max_dd: float = 40.0

    def tick(self, a: Account, mk: Market, i: int, bid: float, ask: float, t: int):
        floating = a.balance - a.equity(bid, ask)
        if floating / a.balance * 100 >= self.max_dd:
            for s in (1, -1):
                if a.b[s]:
                    a.close(s, bid, ask, t, "equity_guard")
            return
        r = mk.rsi[i]
        counts = {s: (a.b[s].n if a.b[s] else 0) for s in (1, -1)}
        for s in (1, -1):
            x = a.b[s]
            if x is None:
                if (s > 0 and r < self.rsi_buy) or (s < 0 and r > self.rsi_sell):
                    a.open(s, self.lot, bid, ask, t)
            elif x.n < self.layers:
                dist = self.grid * self.expansion ** (x.n - 1)
                if mk.m5range[i] > self.candle_max:
                    continue
                if (s > 0 and r < self.freeze) or (s < 0 and r > 100 - self.freeze):
                    continue
                if (s > 0 and ask <= x.prices[-1] - dist) or (s < 0 and bid >= x.prices[-1] + dist):
                    a.open(s, rlot(min(x.lots[-1] * self.mult, self.max_lot)), bid, ask, t)
        for s in (1, -1):  # counts from before this tick, as in the EA
            x = a.b[s]
            if x is None or counts[s] == 0:
                continue
            tgt = self.rec_tp if counts[s] >= self.rec_start else self.tp
            if (s > 0 and bid >= x.avg() + tgt) or (s < 0 and ask <= x.avg() - tgt):
                a.close(s, bid, ask, t, "tp")


# ---------------------------------------------------------------- V14 (improved)

@dataclass
class V14:
    risk_pct: float = 4.0          # worst-case loss of one full trend basket, % of equity
    max_loss_pct: float = 6.0      # money cap per basket (gaps, slippage)
    mult: float = 1.3
    layers: int = 6                # with the H1 trend
    counter_layers: int = 3        # against the H1 trend
    grid_atr: float = 1.5          # grid step = grid_atr x ATR(H1,14); 1.5 lost least in 2023-24
    min_grid_pips: float = 8.0
    expansion: float = 1.2
    stop_steps: float = 1.0        # hard stop this many next-steps past the last layer
    rsi_with: float = 35           # buy pullback in an uptrend: RSI < 35
    rsi_counter: float = 20        # buy against a downtrend only at RSI < 20
    tp_atr: float = 0.8
    rec_start: int = 3
    rec_tp_atr: float = 0.25
    max_dd: float = 20.0           # from peak equity -> close all, pause 24h
    max_hours: float = 0.0         # 0 = off
    friday_close: bool = False
    pause_until: int = 0

    def offsets(self, n):
        o, d = [0.0], 1.0
        for _ in range(n - 1):
            o.append(o[-1] + d)
            d *= self.expansion
        return o, o[-1] + d * self.stop_steps

    def lot0(self, equity, step, n):
        o, stop = self.offsets(n)
        units = sum(self.mult ** k * (stop - o[k]) for k in range(n)) * step + SPREAD * sum(
            self.mult ** k for k in range(n))
        lot = equity * self.risk_pct / 100 / (units * CONTRACT)
        return math.floor(lot * 100) / 100

    def tick(self, a: Account, mk: Market, i: int, bid: float, ask: float, t: int):
        eq = a.equity(bid, ask)
        if eq <= a.peak * (1 - self.max_dd / 100):
            for s in (1, -1):
                if a.b[s]:
                    a.close(s, bid, ask, t, "dd_guard")
            a.peak = a.balance  # reset peak so the guard is not hit again right away
            self.pause_until = t + 86400
            return
        h, wd, mi = mk.hour[i], mk.wd[i], mk.minute[i]
        if self.friday_close and wd == 4 and (h > 20 or (h == 20 and mi >= 45)):
            for s in (1, -1):
                if a.b[s]:
                    a.close(s, bid, ask, t, "friday")
            return
        no_new = t < self.pause_until or h >= 20 or h == 0 or (wd == 4 and h >= 18)
        trend, atr, r = mk.trend[i], mk.atr[i], mk.rsi[i]
        for s in (1, -1):
            x = a.b[s]
            with_trend = s == trend
            if x is None:
                if no_new or ask - bid > 2.5 * PIP:
                    continue
                lvl = self.rsi_with if with_trend else self.rsi_counter
                if (s > 0 and r < lvl) or (s < 0 and r > 100 - lvl):
                    step = max(self.grid_atr * atr, self.min_grid_pips * PIP)
                    n = self.layers if with_trend else self.counter_layers
                    lot = self.lot0(eq, step, self.layers)  # sized on the full trend basket
                    if lot < 0.01:
                        continue
                    a.open(s, lot, bid, ask, t)
                    x = a.b[s]
                    x.step, x.lot0, x.max_layers, x.with_trend, x.eq0 = step, lot, n, with_trend, eq
                    o, stop = self.offsets(n)
                    x.stop = x.prices[0] - s * stop * step
                continue
            px = bid if s > 0 else ask
            if (s > 0 and px <= x.stop) or (s < 0 and px >= x.stop) or x.pnl(bid, ask) <= -x.eq0 * self.max_loss_pct / 100:
                a.close(s, bid, ask, t, "hard_stop")
                continue
            if self.max_hours and t - x.opened >= self.max_hours * 3600:
                a.close(s, bid, ask, t, "time")
                continue
            tp = (self.rec_tp_atr if x.n >= self.rec_start else self.tp_atr) * atr
            if x.pnl(bid, ask) >= sum(x.lots) * tp * CONTRACT:
                a.close(s, bid, ask, t, "tp")
                continue
            if x.n < x.max_layers and (with_trend or not x.with_trend) and not no_new:
                o, _ = self.offsets(x.n + 1)
                level = x.prices[0] - s * o[x.n] * x.step
                if (s > 0 and ask <= level) or (s < 0 and bid >= level):
                    a.open(s, rlot(x.lot0 * self.mult ** x.n), bid, ask, t)


# ---------------------------------------------------------------- runner

def run(strategy, mk: Market, capital: float) -> dict:
    a = Account(capital)
    last_day = None
    month = None
    for i in range(len(mk.t)):
        t = int(mk.t[i])
        day = (t - 21 * 3600) // 86400
        if last_day is not None and day != last_day:
            a.rollover()
            if ((day + 3) % 7) == 2:  # Wednesday rollover counts three nights
                a.rollover(); a.rollover()
        last_day = day
        for mid in mk.path[i]:
            bid, ask = mid - SPREAD / 2, mid + SPREAD / 2
            strategy.tick(a, mk, i, bid, ask, t)
            a.check(bid, ask, t)
            if a.blown:
                break
        if a.blown:
            break
        m = t // (30 * 86400)
        if m != month:
            month = m
            a.curve.append((t, round(a.equity(bid, ask), 2)))
    last = mk.path[i][-1]
    for s in (1, -1):
        if a.b[s]:
            a.close(s, last - SPREAD / 2, last + SPREAD / 2, t, "end")
    return summary(a, mk)


def summary(a: Account, mk: Market) -> dict:
    pn = np.array([c[3] for c in a.closed]) if a.closed else np.zeros(1)
    reasons = {}
    for c in a.closed:
        reasons[c[4]] = reasons.get(c[4], 0) + 1
    years = (mk.t[-1] - mk.t[0]) / (365.25 * 86400)
    final = a.balance
    by_year = {}
    for t, eq in a.curve:
        by_year[datetime.fromtimestamp(t, timezone.utc).year] = eq
    worst = sorted(a.closed, key=lambda c: c[3])[:3]
    return {
        "start": a.capital, "final": round(final, 2), "return_pct": round((final / a.capital - 1) * 100, 1),
        "cagr_pct": round(((max(final, 1) / a.capital) ** (1 / years) - 1) * 100, 1) if years > 0 else 0,
        "max_dd_pct": round(a.max_dd * 100, 1), "min_equity": round(a.min_eq, 2), "blown": a.blown,
        "baskets": len(a.closed), "win_pct": round((pn > 0).mean() * 100, 1),
        "avg_win": round(pn[pn > 0].mean(), 2) if (pn > 0).any() else 0,
        "avg_loss": round(pn[pn <= 0].mean(), 2) if (pn <= 0).any() else 0,
        "reasons": reasons, "worst": [(datetime.fromtimestamp(c[0], timezone.utc).strftime("%Y-%m-%d"), c[1], c[2], c[3], c[4]) for c in worst],
        "year_end_equity": by_year,
    }


def _job(args):
    name, strat, capital, start, end = args
    a = np.load(FILE)
    mk = market(a, start, end)
    return name, run(strat, mk, capital)


def variants():
    base = V14()
    out = {"v14_default": base}
    for risk in (3.0, 6.0):
        out[f"risk{risk:g}"] = replace(base, risk_pct=risk)
    out["mult1.5"] = replace(base, mult=1.5)
    out["layers8"] = replace(base, layers=8)
    out["counter0"] = replace(base, counter_layers=1)
    out["grid0.6"] = replace(base, grid_atr=0.6)
    out["grid1.5"] = replace(base, grid_atr=1.5)
    out["tp0.5"] = replace(base, tp_atr=0.5)
    out["tp1.2"] = replace(base, tp_atr=1.2)
    out["rsi40"] = replace(base, rsi_with=40, rsi_counter=25)
    out["fri_close"] = replace(base, friday_close=True)
    out["time72h"] = replace(base, max_hours=72)
    out["stop2"] = replace(base, stop_steps=2.0)
    return out


def variants2():
    base = V14()
    out = {}
    for g in (0.6, 0.8):
        for mn in (6.0, 8.0, 10.0):
            out[f"grid{g}_min{mn:g}"] = replace(base, grid_atr=g, min_grid_pips=mn)
    out["grid0.6_tp0.5"] = replace(base, grid_atr=0.6, tp_atr=0.5)
    out["grid0.6_risk3"] = replace(base, grid_atr=0.6, risk_pct=3.0)
    out["grid0.6_layers5"] = replace(base, grid_atr=0.6, layers=5)
    out["grid0.6_counter1"] = replace(base, grid_atr=0.6, counter_layers=1)
    out["fixed8pips"] = replace(base, grid_atr=0.0, min_grid_pips=8.0)
    out["fixed12pips"] = replace(base, grid_atr=0.0, min_grid_pips=12.0)
    return out


def report(mode: str = "main") -> dict:
    jobs = []
    if mode in ("tune", "tune2"):  # in-sample only
        for k, v in ({**variants(), **variants2()} if mode == "tune" else variants2()).items():
            jobs.append((f"IS {k}", v, 10_000, IS_START, IS_END))
            if mode == "tune2" and k.startswith("grid0.6_min8"):
                jobs.append((f"IS {k} 2x spread", "x2:" + json.dumps({"grid_atr": 0.6}), 10_000, IS_START, IS_END))
    else:
        v14 = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        best = replace(V14(), **v14)
        for cap, lot in ((10_000, 0.01), (1_000, 0.01)):
            for name, s, e in (("full", IS_START, END), ("IS", IS_START, IS_END), ("OOS", IS_END, END)):
                jobs.append((f"V13 ${cap} {name}", V13(lot=lot), cap, s, e))
        jobs.append(("V13 $10000 lot0.1 full", V13(lot=0.1), 10_000, IS_START, END))
        for name, s, e in (("full", IS_START, END), ("IS", IS_START, IS_END), ("OOS", IS_END, END)):
            jobs.append((f"V14 $10000 {name}", best, 10_000, s, e))
        jobs.append(("V14 $10000 OOS 2x spread", "x2", 10_000, IS_END, END))
        for y in range(2023, 2027):  # each year from a fresh account: is one year carrying it?
            s0 = int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())
            e0 = int(datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp())
            jobs.append((f"V14 $10000 year {y}", best, 10_000, s0, e0))
            jobs.append((f"V13 $10000 year {y}", V13(), 10_000, s0, e0))
    out = {}
    with ProcessPoolExecutor() as ex:
        for name, res in ex.map(_job2, [(j, mode) for j in jobs]):
            out[name] = res
            print(name, json.dumps(res), file=sys.stderr, flush=True)
    return out


def _job2(arg):
    (name, strat, capital, start, end), _ = arg
    global SPREAD
    if isinstance(strat, str) and strat.startswith("x2"):
        SPREAD *= 2
        v14 = json.loads(strat[3:]) if strat.startswith("x2:") else (json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
        strat = replace(V14(), **v14)
    return _job((name, strat, capital, start, end))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "fetch":
        print(fetch(), "minute bars")
    else:
        print(json.dumps(report(sys.argv[2] if len(sys.argv) > 2 else "main"), indent=1))
