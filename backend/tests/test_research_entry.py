from dataclasses import replace
from app import research_entry as E
from app.config import get_settings
from app.market import get_klines


def _data():
    s = replace(get_settings(), data_mode="demo")
    return {sym: (get_klines(s, sym, "1d", 700), get_klines(s, sym, "4h", 900)) for sym in ("BTCUSDT", "ETHUSDT")}


def test_pullback_fill_beats_market_entry_price():
    ltf = [{"time": t, "open": 100, "high": 101, "low": 99, "close": 100} for t in range(10)]
    ltf[2] = {"time": 2, "open": 100, "high": 100.5, "low": 98.9, "close": 99.5}
    p = {"side": "long", "sl": 95.0, "atr": 2.0}
    t = E.trade(p, ltf, 1, ("pullback", 0.5, 2, "skip"), "struct", 2.5, 5)
    m = E.trade(p, ltf, 1, ("market", 0, 0, None), "struct", 2.5, 5)
    assert t["risk_pct"] < m["risk_pct"]                       # filled at 99, not 100
    none = E.trade(p, [{**b, "low": 99.5} for b in ltf], 1, ("pullback", 0.5, 2, "skip"), "struct", 2.5, 5)
    assert none is None


def test_run_reports_baseline_and_ranking(monkeypatch):
    monkeypatch.setattr(E.D, "make_allow", lambda feat, g: (lambda i, long: True))
    data = _data()
    out = E.run(data, {s: [] for s in data})
    assert out["variants"] == len(E.ENTRIES) * len(E.STOPS) * len(E.TARGETS)
    assert out["baseline"]["v"] == "market | SL struct | TP 2.5R" and out["top_is"]
